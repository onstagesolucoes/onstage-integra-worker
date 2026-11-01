"""Worker da fila `integra_jobs` — o único processo que fala com o SERPRO.

Vários workers podem rodar em paralelo: cada job é reservado com
`FOR UPDATE SKIP LOCKED`. Uso:

    worker = Worker(dsn, provider=MeuProvider(), storage=MeuStorage())
    worker.rodar()            # laço infinito
    worker.processar_um()     # útil em testes ou num cron

Três coisas aqui existem por causa de dinheiro, e não de arquitetura:

1. **O teto diário é consultado antes de puxar job.** O jeito de o custo fugir
   não é alguém clicar demais — é job em laço de nova tentativa gastando a noite
   inteira sem ninguém olhando.
2. **O teto é lido direto das tabelas, nunca da view `v_integra_teto_hoje`.**
   A view termina em `where public.eh_interno()`, e `eh_interno()` depende de
   `auth.uid()`. O worker conecta direto no Postgres, sem JWT: para ele a view
   devolve **zero linha**. Ler dali e tratar "sem linha" como "sem teto" seria
   voltar a ter um teto decorativo — o mesmo defeito que `custo_estimado` nulo
   já tinha. A view é para o portal ler; o worker lê a fonte.
3. **`custo_estimado` vai no insert.** Sem essa coluna preenchida, a soma do dia
   é zero e nada nunca atinge teto nenhum.
"""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Callable

import psycopg

from ..client import RegistroChamada, SerproClient
from ..config import CredentialProvider
from ..erros import ConfiguracaoIncompleta, CertificadoVencido, ErroSerpro, FalhaAutenticacao
from .base import Contexto, Job, Resultado, Storage
from .handlers import HANDLERS

log = logging.getLogger("integra_serpro.fila")

ERROS_DEFINITIVOS = (ConfiguracaoIncompleta, CertificadoVencido, FalhaAutenticacao, ValueError, KeyError)

SQL_RESERVAR = """
with proximo as (
  select j.id from integra_jobs j
   where j.status in ('fila', 'aguardando') and j.tentar_apos <= now()
   order by j.tentar_apos
   limit 1
   for update skip locked
)
update integra_jobs j
   set status = 'executando', tentativas = j.tentativas + 1, iniciado_em = coalesce(j.iniciado_em, now())
  from proximo
 where j.id = proximo.id
returning j.id, j.tipo, j.params, j.estado, j.tentativas, j.max_tentativas, j.cnpj
"""

# Teto do dia contra o gasto do dia. O corte é o mesmo `date_trunc('day', now())`
# da view que o portal mostra, para que as duas contas não divirjam por fuso.
SQL_TETO = """
select cr.teto_diario,
       coalesce((select sum(c.custo_estimado) from integra_chamadas c
                  where c.created_at >= date_trunc('day', now())), 0)
  from integra_credenciais cr where cr.id = 1
"""

# O acesso ao dado de cada cliente vem da **procuração e-CAC**, não do nosso
# certificado — o mTLS só prova quem é a ON. Sem procuração válida, o SERPRO
# responde 4xx, e 4xx **já foi cobrado**: a chamada aconteceu. Como o
# impedimento está no banco antes de qualquer rede, perguntar aqui troca uma
# chamada paga por um `select`.
SQL_HABILITADO = "select exists (select 1 from v_integra_habilitados where cnpj = %s)"


class Worker:
    def __init__(
        self,
        dsn: str,
        *,
        provider: CredentialProvider,
        storage: Storage,
        intervalo_ocioso_s: float = 5.0,
        fabrica_client: Callable[..., SerproClient] = SerproClient,
    ):
        self.dsn = dsn
        self.provider = provider
        self.storage = storage
        self.intervalo = intervalo_ocioso_s
        self.fabrica_client = fabrica_client
        self._client: SerproClient | None = None
        self._job_atual: str | None = None
        self._cnpj_atual: str | None = None
        self._log_conn = psycopg.connect(dsn, autocommit=True)

    def fechar(self) -> None:
        """Fecha a conexão de registro e a sessão TLS. Idempotente.

        No container isso roda uma vez, no encerramento. Quem precisa de
        verdade é o teste, que cria um worker por caso: sem fechar, cada um
        deixa a conexão de `integra_chamadas` aberta até o GC passar.
        """
        if self._client is not None:
            self._client.fechar()
            self._client = None
        if not self._log_conn.closed:
            self._log_conn.close()

    # ------------------------------------------------------------- registro
    def _registrar(self, r: RegistroChamada) -> None:
        """Grava a chamada em conexão própria, fora da transação do job.

        É de propósito: se o handler falhar e a transação do job for desfeita, a
        chamada ao SERPRO **já aconteceu e já foi cobrada**. Registrá-la na mesma
        transação faria o rollback apagar a prova do gasto.
        """
        self._log_conn.execute(
            """insert into integra_chamadas (job_id, cnpj, chave_servico, id_sistema, id_servico, endpoint,
                 cobranca, http_status, sucesso, duracao_ms, custo_estimado,
                 request_mascarado, response_mascarado, erro)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (self._job_atual, r.contribuinte or self._cnpj_atual, r.chave_servico, r.id_sistema,
             r.id_servico, r.endpoint, r.cobranca, r.http_status, r.sucesso, r.duracao_ms,
             r.custo_estimado,
             json.dumps(r.request_mascarado, ensure_ascii=False, default=str),
             json.dumps(r.response_mascarado, ensure_ascii=False, default=str), r.erro),
        )

    def _chamadas_no_mes(self, cobranca) -> int:
        """Quantas chamadas da mesma família já houve no mês — define a faixa.

        É uma contagem num índice por data; ao lado de uma chamada HTTP ao
        SERPRO, o custo é irrelevante. Falha aqui não derruba a chamada: o
        cliente assume preço cheio, que erra para cima.
        """
        cur = self._log_conn.execute(
            """select count(*) from integra_chamadas
                where cobranca = %s and created_at >= date_trunc('month', now())""",
            (getattr(cobranca, "value", cobranca),),
        )
        linha = cur.fetchone()
        return int(linha[0]) if linha else 0

    @staticmethod
    def _habilitado(conn, cnpj: str) -> bool:
        """O cliente tem procuração e-CAC válida? Lido na conexão do job.

        Vai na conexão do job, e não na de registro, de propósito: é uma
        pergunta sobre o mesmo instante em que o job é processado.
        """
        linha = conn.execute(SQL_HABILITADO, (cnpj,)).fetchone()
        return bool(linha and linha[0])

    def _obter_client(self) -> SerproClient:
        """Instância única: um contratante, um certificado, uma conexão TLS."""
        if self._client is None:
            self._client = self.fabrica_client(
                self.provider.obter(),
                registrar=self._registrar,
                chamadas_no_mes=self._chamadas_no_mes,
            )
        return self._client

    # ------------------------------------------------------------- teto
    def teto_atingido(self) -> tuple[bool, Decimal, Decimal]:
        """(atingido, gasto, teto). Sem linha de credencial, **para**.

        "Não achei a configuração" não pode virar "pode gastar à vontade": sem
        `integra_credenciais` não há nem de onde tirar segredo para chamar o
        SERPRO, então parar é o único desfecho correto.
        """
        linha = self._log_conn.execute(SQL_TETO).fetchone()
        if not linha:
            log.error("integra_credenciais vazia: worker parado (sem teto não se gasta)")
            return True, Decimal("0"), Decimal("0")
        teto, gasto = Decimal(linha[0]), Decimal(linha[1])
        return gasto >= teto, gasto, teto

    def _registrar_teto(self, gasto: Decimal, teto: Decimal) -> None:
        """Um evento por dia, não um por volta do laço.

        Sem o `not exists`, um worker ocioso gravaria um evento a cada 5 s pelo
        resto do dia e a trilha viraria lixo — justamente no dia em que alguém
        vai abri-la para entender o que aconteceu.
        """
        self._log_conn.execute(
            """insert into integra_eventos (evento, detalhe)
               select 'teto_diario_atingido', %s
                where not exists (
                  select 1 from integra_eventos
                   where evento = 'teto_diario_atingido'
                     and created_at >= date_trunc('day', now()))""",
            (json.dumps({"gasto": str(gasto), "teto": str(teto)}),),
        )

    # ------------------------------------------------------------- laço
    def rodar(self) -> None:
        log.info("Worker Integra Contador iniciado")
        while True:
            try:
                atingido, gasto, teto = self.teto_atingido()
                if atingido:
                    self._registrar_teto(gasto, teto)
                    log.warning("Teto diário atingido (R$ %s de R$ %s); sem puxar job", gasto, teto)
                    time.sleep(max(self.intervalo, 60))
                    continue
                if not self.processar_um():
                    time.sleep(self.intervalo)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("Falha inesperada no worker")
                time.sleep(self.intervalo)

    def processar_um(self) -> bool:
        with psycopg.connect(self.dsn) as conn:
            with conn.transaction():
                row = conn.execute(SQL_RESERVAR).fetchone()
            if not row:
                return False
            job = Job(str(row[0]), row[1], row[2] or {}, row[3] or {}, row[4], row[5], row[6])
            self._job_atual, self._cnpj_atual = job.id, job.cnpj
            try:
                handler = HANDLERS.get(job.tipo)
                if handler is None:
                    # ValueError é definitivo: o job morre aqui, **sem chamar o
                    # SERPRO**. Tipo inexistente não pode custar dinheiro.
                    raise ValueError(
                        f"Tipo de job não implantado: {job.tipo!r}. "
                        f"Disponíveis: {', '.join(sorted(HANDLERS))}"
                    )
                # Mesma regra, outro motivo: cliente sem procuração e-CAC válida
                # não pode custar dinheiro. O portal também barra na origem, mas
                # o job chega por três caminhos (tela, n8n, insert à mão) e o
                # gasto acontece *aqui* — então a última palavra é aqui.
                if job.cnpj and not self._habilitado(conn, job.cnpj):
                    raise ValueError(
                        f"Cliente {job.cnpj} não está em v_integra_habilitados: "
                        "sem procuração e-CAC ativa e na validade, a consulta "
                        "voltaria 4xx e seria cobrada igual"
                    )
                with conn.transaction():
                    resultado = handler(Contexto(self._obter_client(), conn, self.storage, job))
                    self._concluir(conn, job, resultado)
            except Exception as exc:
                self._falhar(conn, job, exc)
            finally:
                self._job_atual = self._cnpj_atual = None
        return True

    # ------------------------------------------------------------- desfechos
    @staticmethod
    def _concluir(conn, job: Job, r: Resultado) -> None:
        if r.status == "aguardar":
            conn.execute(
                """update integra_jobs set status = 'aguardando', estado = %s, resultado = %s, erro = null,
                     tentativas = 0, tentar_apos = now() + make_interval(secs => %s) where id = %s""",
                (json.dumps(r.estado or {}), json.dumps(r.resultado, default=str), r.aguardar_s, job.id),
            )
        else:
            conn.execute(
                """update integra_jobs set status = 'ok', resultado = %s, erro = null, concluido_em = now()
                   where id = %s""",
                (json.dumps(r.resultado, default=str), job.id),
            )

    @staticmethod
    def _falhar(conn, job: Job, exc: Exception) -> None:
        definitivo = (
            isinstance(exc, ERROS_DEFINITIVOS)
            or job.tentativas >= job.max_tentativas
            # 4xx é pedido errado, não rede instável: repetir só paga de novo
            # pelo mesmo "não". 429 é a exceção — ali repetir é o certo.
            or (isinstance(exc, ErroSerpro) and 400 <= exc.status_http < 500 and exc.status_http != 429)
        )
        log.warning("Job %s (%s) falhou: %s", job.id, job.tipo, exc)
        with conn.transaction():
            if definitivo:
                conn.execute(
                    "update integra_jobs set status = 'erro', erro = %s, concluido_em = now() where id = %s",
                    (str(exc)[:2000], job.id),
                )
            else:
                espera = min(3600, 30 * 2 ** job.tentativas)
                conn.execute(
                    """update integra_jobs set status = 'fila', erro = %s,
                         tentar_apos = now() + make_interval(secs => %s) where id = %s""",
                    (str(exc)[:2000], espera, job.id),
                )
            # O erro também marca o estado do cliente: é o que permite à tela
            # dizer "a última tentativa falhou" em vez de exibir dado velho com
            # cara de fresco. Falha silenciosa é indistinguível de dado atual.
            if job.cnpj:
                conn.execute(
                    """insert into integra_estado_cliente (cnpj, ultimo_erro, erro_em)
                       values (%s, %s, now())
                       on conflict (cnpj) do update set ultimo_erro = excluded.ultimo_erro, erro_em = now()""",
                    (job.cnpj, str(exc)[:2000]),
                )
