"""Provider de credenciais: Supabase Vault + bucket privado.

Como os segredos se dividem, e por quê:

- **`integra_credenciais`** (tabela, linha única) guarda só *referências*: o nome
  de cada segredo no Vault e o caminho do `.pfx` no bucket. Quem lê essa tabela
  não ganha nada com isso.
- **Supabase Vault** guarda consumer key, consumer secret e a senha do
  certificado. É a primeira credencial do portal que, vazada, **gasta dinheiro**
  e expõe dado fiscal de 97 CNPJs — uma tabela comum no estilo `contas_cora`
  não serve aqui.
- **Bucket privado `certificados-on`** guarda o binário do `.pfx`, que não é
  texto e não cabe no Vault. Só o service-role deste worker o alcança.

Nada disso toca disco. O `.pfx` fica em memória pelo tempo de vida do processo,
que é o motivo de o worker ser um container isolado e não uma rota do portal.

Referência do que **não** fazer: o `IntegraContadorDesktop.exe` guardava a senha
do `.pfx` e o consumer secret num `config.json` em texto puro e os devolvia por
`GET /api/settings`, sem autenticação, com CORS liberado, em `0.0.0.0:5847`.
"""
from __future__ import annotations

import functools
import logging
import os

import httpx
import psycopg

from integra_serpro import Credenciais, Procurador

log = logging.getLogger("onstage.segredos")

SQL_CREDENCIAIS = """
select contratante_cnpj, nome_escritorio, cert_bucket_path, cert_senha_secret,
       consumer_key_secret, consumer_secret, cert_valido_ate,
       procurador_cpf, procurador_nome, procurador_senha_secret
  from integra_credenciais where id = 1
"""

# `vault.decrypted_secrets` é uma view: a descriptografia acontece na leitura e
# exige a chave do projeto. Por isso o DSN do worker é service-role e não o
# `anon` que o portal usa.
SQL_SEGREDO = "select decrypted_secret from vault.decrypted_secrets where name = %s"


class ProviderSupabase:
    """Monta `Credenciais` lendo tabela + Vault + bucket.

    O resultado é memoizado: são três viagens de rede e o certificado não muda
    durante a vida do processo. Reinício do container é o jeito de recarregar —
    e trocar certificado é, de propósito, um evento com deploy.
    """

    def __init__(
        self,
        dsn: str | None = None,
        supabase_url: str | None = None,
        service_key: str | None = None,
        bucket_certificados: str = "certificados-on",
    ):
        self.dsn = dsn or os.environ["DATABASE_URL"]
        self.url = (supabase_url or os.environ["SUPABASE_URL"]).rstrip("/")
        self.key = service_key or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.bucket = bucket_certificados

    # ------------------------------------------------------------------ peças
    @staticmethod
    def _segredo(conn, nome: str) -> str:
        linha = conn.execute(SQL_SEGREDO, (nome,)).fetchone()
        if not linha or not linha[0]:
            raise RuntimeError(
                f"Segredo {nome!r} não existe no Supabase Vault. "
                "Cadastre com: select vault.create_secret('<valor>', '<nome>');"
            )
        return linha[0]

    def _baixar(self, caminho: str) -> bytes:
        r = httpx.get(
            f"{self.url}/storage/v1/object/{self.bucket}/{caminho}",
            headers={"Authorization": f"Bearer {self.key}"},
            timeout=60.0,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Certificado não baixado de {self.bucket}/{caminho} (HTTP {r.status_code}). "
                "Confira o caminho em integra_credenciais.cert_bucket_path."
            )
        return r.content

    # ------------------------------------------------------------------ provider
    @functools.cached_property
    def _credenciais(self) -> Credenciais:
        with psycopg.connect(self.dsn) as conn:
            linha = conn.execute(SQL_CREDENCIAIS).fetchone()
            if not linha:
                raise RuntimeError(
                    "integra_credenciais está vazia. Insira a linha única (id = 1) com as "
                    "referências do cofre antes de subir o worker."
                )
            (cnpj, nome, cert_path, senha_ref, ck_ref, cs_ref, valido_ate,
             proc_cpf, proc_nome, proc_senha_ref) = linha

            # Aviso de vencimento no log de partida: o e-CNPJ A1 da ON vencendo
            # derruba **tudo** de uma vez, e o sintoma (todas as chamadas em
            # erro de TLS) não diz o motivo. O alerta de 30/15/7 dias vive no
            # portal, em `certificados_digitais`; aqui é a última linha de
            # defesa, para quem está lendo o log do container.
            if valido_ate:
                from datetime import date
                dias = (valido_ate - date.today()).days
                if dias < 0:
                    log.error("e-CNPJ do contratante VENCIDO em %s — nenhuma chamada vai passar", valido_ate)
                elif dias <= 30:
                    log.warning("e-CNPJ do contratante vence em %s dia(s) (%s)", dias, valido_ate)

            procurador = None
            if proc_cpf and proc_senha_ref:
                # Procurador PF: só existe se alguma procuração e-CAC estiver no
                # CPF de um sócio em vez do CNPJ da ON. Hoje não está.
                procurador = Procurador(
                    cpf=proc_cpf,
                    nome=proc_nome or "",
                    pfx=self._baixar(f"procurador/{proc_cpf}.pfx"),
                    senha_pfx=self._segredo(conn, proc_senha_ref),
                )

            cred = Credenciais(
                contratante_cnpj=cnpj,
                nome_escritorio=nome,
                pfx=self._baixar(cert_path),
                senha_pfx=self._segredo(conn, senha_ref),
                consumer_key=self._segredo(conn, ck_ref),
                consumer_secret=self._segredo(conn, cs_ref),
                procurador=procurador,
            )
        cred.validar()
        log.info("Credenciais carregadas: %s (%s)", nome, cnpj)
        return cred

    def obter(self) -> Credenciais:
        return self._credenciais
