"""Um handler por tipo de job: chama o serviço e grava no esquema do portal.

Este é o arquivo que mais mudou em relação ao pacote de origem, por um motivo
só: o esquema do portal não é o do pacote. As diferenças que importam — cada
uma delas seria um `insert` quebrado em produção:

| pacote de origem                 | portal                                               |
| -------------------------------- | ---------------------------------------------------- |
| `tenant_id` em toda tabela       | não existe (um escritório, um contratante)           |
| `empresa_id` como grão           | `cnpj char(14)`                                      |
| `integra_caixa_monitoramento`    | dobrada em `integra_estado_cliente`                  |
| `integra_parc_guias.modalidade`  | `integra_parc_parcelas.id_sistema` (com FK)          |
| nada                             | espelho em `parcelamentos_fiscais` (`origem='ecac'`) |

**Guias, pagamentos e MIT ficam de fora de propósito.** Os serviços
(`servicos/guias.py` e companhia) continuam aqui, testados, mas não há handler
registrado, porque as tabelas de destino ainda não existem no portal. Handler
registrado sem tabela é a pior falha possível deste módulo: o SERPRO é chamado,
**a chamada é paga**, e só então o `insert` estoura em `relation does not
exist` — e como isso não é erro definitivo, a fila tentaria de novo, pagando de
novo. Fora do dicionário, o job morre antes de gastar um centavo.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime

from ..erros import AindaProcessando, IntegraErro
from ..servicos import CaixaPostal, Parcelamentos, Sitfis
from .base import Contexto, Handler, Resultado

log = logging.getLogger("integra_serpro.handlers")

# Versão do leitor do PDF do SITFIS, gravada em cada relatório. É o que permite
# re-rodar um parser corrigido só sobre o que ele leu errado, em vez de obrigar
# a **recomprar** os relatórios — cada um custa uma emissão.
PARSER_VERSAO = "sitfis-1"

# Teto de detalhes de mensagem por rodada. Cada detalhe é uma chamada paga; uma
# caixa com 200 mensagens atrasadas não pode virar 200 chamadas num job só.
MAX_DETALHES_POR_RODADA = 20

# A API emite no máximo 6 parcelas por vez; acima disso o próprio SERPRO manda
# usar o portal web, e aqui se bloqueia em vez de tentar e pagar o erro.
LIMITE_PARCELAS_API = 6


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _agora():
    return datetime.now().astimezone()


def _caminho(ctx: Contexto, tipo: str, nome: str) -> str:
    """`{cnpj}/{tipo}/{aaaamm}/{nome}` no bucket privado `integra`.

    O ano-mês no caminho não é enfeite: é o que permite aplicar retenção ("PDF
    com mais de N meses") apagando um prefixo, em vez de varrer o bucket.
    """
    return f"{ctx.job.cnpj}/{tipo}/{datetime.now():%Y%m}/{nome}"


def _marcar_estado(ctx: Contexto, **campos) -> None:
    """Upsert em `integra_estado_cliente` — a tabela que governa a cadência.

    Escrever aqui é o que impede a próxima rodada de repetir a chamada paga. Por
    isso acontece no **mesmo commit** do dado: se o PDF entrar e a data não, a
    rodada seguinte compra o mesmo relatório de novo.
    """
    colunas = ", ".join(campos)
    marcas = ", ".join(["%s"] * len(campos))
    atualiza = ", ".join(f"{c} = excluded.{c}" for c in campos)
    ctx.conn.execute(
        f"""insert into integra_estado_cliente (cnpj, {colunas}, atualizado_em)
            values (%s, {marcas}, now())
            on conflict (cnpj) do update set {atualiza}, atualizado_em = now()""",
        (ctx.job.cnpj, *campos.values()),
    )


# ------------------------------------------------------------------ SITFIS
def sitfis(ctx: Contexto) -> Resultado:
    """Situação fiscal: pede protocolo, espera, baixa o PDF.

    São **duas** chamadas pagas (`/Apoiar` para o protocolo, `/Emitir` para o
    relatório) e o protocolo vale por um tempo. Por isso ele é persistido em
    `integra_estado_cliente.sitfis_protocolo`, e não só no `estado` do job: se o
    job for cancelado ou morrer entre as duas etapas, a rodada seguinte
    reaproveita o protocolo já comprado em vez de comprar outro.
    """
    svc = Sitfis(ctx.client)
    protocolo = ctx.job.estado.get("protocolo")
    if not protocolo:
        linha = ctx.conn.execute(
            """select sitfis_protocolo from integra_estado_cliente
                where cnpj = %s and sitfis_protocolo is not null
                  and sitfis_tentar_apos > now() - interval '1 hour'""",
            (ctx.job.cnpj,),
        ).fetchone()
        protocolo = linha[0] if linha else None

    if not protocolo:
        p = svc.solicitar_protocolo(ctx.job.cnpj)
        _marcar_estado(ctx, sitfis_protocolo=p.protocolo, sitfis_tentar_apos=_agora())
        return Resultado.aguardar(
            p.espera_segundos, {"protocolo": p.protocolo}, etapa="aguardando relatório"
        )

    try:
        pdf = svc.obter_relatorio(ctx.job.cnpj, protocolo)
    except AindaProcessando as exc:
        return Resultado.aguardar(
            exc.espera_segundos or 30, {"protocolo": protocolo}, etapa="SERPRO ainda processando"
        )

    sha = hashlib.sha256(pdf).hexdigest()
    caminho = ctx.storage.salvar(_caminho(ctx, "sitfis", f"{datetime.now():%Y%m%d_%H%M%S}.pdf"), pdf)
    # `on conflict (cnpj, pdf_sha256)`: relatório idêntico ao da semana passada é
    # o caso comum (cliente sem movimento) e não deve virar uma segunda linha
    # disputando o "atual" em `v_sitfis_atual`.
    linha = ctx.conn.execute(
        """insert into integra_sitfis_relatorios (cnpj, protocolo, pdf_path, pdf_sha256, parser_versao, emitido_em)
           values (%s, %s, %s, %s, %s, now())
           on conflict (cnpj, pdf_sha256) do update set protocolo = excluded.protocolo,
             pdf_path = excluded.pdf_path, emitido_em = excluded.emitido_em
           returning id""",
        (ctx.job.cnpj, protocolo, caminho, sha, PARSER_VERSAO),
    ).fetchone()
    # Protocolo consumido: limpar evita reaproveitar na próxima rodada um
    # protocolo já gasto e levar 4xx do SERPRO.
    _marcar_estado(ctx, sitfis_protocolo=None, sitfis_tentar_apos=None, sitfis_consultado_em=_agora())
    # A leitura do PDF (pendências e débitos) é job próprio: parser erra, e errar
    # na extração não pode obrigar a recomprar o relatório.
    return Resultado.ok(relatorio_id=str(linha[0]), pdf_path=caminho, sha256=sha)


# ------------------------------------------------------------------ Caixa postal
def caixa_postal(ctx: Contexto) -> Resultado:
    """Indicador (gratuito) e, só se ele acusar novidade, a lista (paga).

    A ordem aqui **é** a economia do módulo: `INNOVAMSG63` vai para `/Monitorar`
    e não custa nada, então roda para os 97 clientes todo dia; a lista
    `MSGCONTRIBUINTE61` é `/Consultar`, paga, e só é chamada quando o indicador
    disse que há mensagem nova. Inverter a ordem multiplicaria a conta.
    """
    cp = CaixaPostal(ctx.client)
    forcar = bool(ctx.job.params.get("forcar"))
    agora = _agora()

    novas = cp.indicador_novas(ctx.job.cnpj)
    _marcar_estado(ctx, caixa_monitorada_em=agora, caixa_indicador_novas=novas)
    if not novas and not forcar:
        # A chamada gratuita foi feita e registrada: "consultamos e a Receita
        # disse que não há novidade" é informação, e é diferente de nunca ter
        # consultado.
        return Resultado.ok(novas=0, gravadas=0, detalhes=0)

    gravadas = 0
    for m in cp.listar(ctx.job.cnpj):
        ctx.conn.execute(
            """insert into integra_caixa_mensagens (cnpj, isn, assunto, data_envio, hora_envio,
                 data_leitura, data_ciencia, data_expiracao, remetente, relevancia, lida_no_ecac, lista_bruto)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (cnpj, isn) do update set data_leitura = excluded.data_leitura,
                 data_ciencia = excluded.data_ciencia, lida_no_ecac = excluded.lida_no_ecac,
                 lista_bruto = excluded.lista_bruto, updated_at = now()""",
            (ctx.job.cnpj, m.isn, m.assunto, m.data_envio, m.hora_envio, m.data_leitura,
             m.data_ciencia, m.data_expiracao, m.remetente, m.relevancia, m.lida_no_ecac, _json(m.bruto)),
        )
        gravadas += 1

    # O detalhe é outra chamada paga, uma por mensagem — daí o teto por rodada.
    # O que não couber vem na próxima: atraso é barato, surpresa na fatura não.
    teto = int(ctx.job.params.get("max_detalhes", MAX_DETALHES_POR_RODADA))
    pendentes = ctx.conn.execute(
        """select isn from integra_caixa_mensagens
            where cnpj = %s and not detalhe_baixado
            order by data_envio desc nulls last limit %s""",
        (ctx.job.cnpj, teto),
    ).fetchall()
    detalhes = 0
    for (isn,) in pendentes:
        d = cp.detalhe(ctx.job.cnpj, isn)
        ctx.conn.execute(
            """update integra_caixa_mensagens set assunto = coalesce(nullif(%s, ''), assunto), corpo = %s,
                 detalhe_bruto = %s, detalhe_baixado = true, updated_at = now()
               where cnpj = %s and isn = %s""",
            (d.assunto, d.corpo, _json(d.bruto), ctx.job.cnpj, isn),
        )
        detalhes += 1

    _marcar_estado(ctx, caixa_baixada_em=agora)
    return Resultado.ok(novas=novas, gravadas=gravadas, detalhes=detalhes)


# ------------------------------------------------------------------ Parcelamentos
# De-para da situação que o e-CAC escreve em texto livre para os quatro valores
# que `parcelamentos_fiscais.situacao` aceita. A ordem importa: "rescindido por
# falta de pagamento" casaria com "em parcelamento" se essa regra viesse antes.
#
# O que **não** casar com nada aqui não é escrito em `parcelamentos_fiscais`, e
# essa é a decisão mais importante desta seção. A linha do e-CAC tem precedência
# sobre a do Nibo na view de leitura; chutar um valor aqui faria a tela exibir,
# com a autoridade do e-CAC, um estado que ninguém leu. Situação desconhecida
# vira evento para alguém olhar, e a linha do Nibo continua respondendo — que é
# exatamente o comportamento de hoje, nem melhor nem pior.
SITUACAO_ECAC = (
    ("rescind", "rescindido"),
    ("cancelad", "rescindido"),
    ("em parcelamento", "ativo"),
    ("deferid", "ativo"),
    ("consolidad", "ativo"),
    ("liquidad", "quitado"),
    ("quitad", "quitado"),
    ("encerrad", "quitado"),
)


def _situacao_portal(texto: str | None) -> str | None:
    t = (texto or "").strip().lower()
    for agulha, valor in SITUACAO_ECAC:
        if agulha in t:
            return valor
    return None


def _modalidades_do_cliente(ctx: Contexto) -> list[str]:
    """As modalidades que vale a pena consultar para este cliente.

    Varrer 6 modalidades × 97 clientes toda semana é consulta paga em cima de
    cliente que nunca parcelou nada. O Nibo já sabe quem tem acordo (tem vaga
    mensal de guia no calendário — ver docs/PARCELAMENTOS.md), e o `join` com o
    de-para filtra de graça o que não existe nesta API: PGFN e municipal não têm
    `id_sistema`, então saem da lista sozinhos.

    O preço disso é um ponto cego honesto: **acordo que só existe no e-CAC não é
    descoberto por aqui.** Para isso existe `params.varrer_todas`, uma varredura
    completa de cadência mais larga — e é justamente ela que revela guia
    faltando no calendário do Nibo.
    """
    if ctx.job.params.get("varrer_todas"):
        linhas = ctx.conn.execute("select id_sistema from integra_depara_modalidade").fetchall()
        return [l[0] for l in linhas]
    linhas = ctx.conn.execute(
        """select d.id_sistema
             from parcelamentos_fiscais p
             join integra_depara_modalidade d
               on d.orgao = p.orgao and d.modalidade = p.modalidade
            where p.cnpj_norm = %s and p.situacao in ('ativo', 'em_atraso')
            group by d.id_sistema""",
        (ctx.job.cnpj,),
    ).fetchall()
    return [l[0] for l in linhas]


def _espelhar(ctx: Contexto, id_sistema: str, ped, parcelas) -> bool:
    """Grava a linha `origem='ecac'` que a view de precedência vai preferir.

    Não apaga nem altera a linha do Nibo: as duas convivem, e a divergência
    entre elas é o produto — Nibo diz ativo porque tem vaga de guia, e-CAC diz
    rescindido, isso é alerta. Devolve False quando a situação do e-CAC não foi
    reconhecida, caso em que nada é escrito.
    """
    situacao = _situacao_portal(ped.situacao)
    if not situacao:
        ctx.conn.execute(
            """insert into integra_eventos (job_id, cnpj, evento, detalhe)
               values (%s, %s, 'parcelamento_situacao_desconhecida', %s)""",
            (ctx.job.id, ctx.job.cnpj, _json({"id_sistema": id_sistema, "situacao": ped.situacao})),
        )
        log.warning("Situação de parcelamento não mapeada (%s): %r", id_sistema, ped.situacao)
        return False

    proxima = min((p.data_vencimento for p in parcelas if p.data_vencimento), default=None)
    valor = next((p.valor for p in parcelas if p.data_vencimento == proxima), None)
    # `on conflict ... where origem = 'ecac'` aponta para o índice parcial criado
    # na migração, espelho do que a carga do Nibo usa. É o que faz as duas
    # origens conviverem no mesmo grão sem uma atropelar a outra.
    ctx.conn.execute(
        """insert into parcelamentos_fiscais
             (cnpj_norm, orgao, modalidade, numero, situacao, valor_parcela, data_adesao,
              proximo_vencimento, observacao, origem, atualizado_em)
           select %s, d.orgao, d.modalidade, %s, %s, %s, %s, %s, %s, 'ecac', now()
             from integra_depara_modalidade d where d.id_sistema = %s
           on conflict (cnpj_norm, orgao, modalidade) where origem = 'ecac'
           do update set numero = excluded.numero, situacao = excluded.situacao,
             valor_parcela = excluded.valor_parcela, data_adesao = excluded.data_adesao,
             proximo_vencimento = excluded.proximo_vencimento, observacao = excluded.observacao,
             atualizado_em = now()""",
        (ctx.job.cnpj, ped.numero, situacao, valor, ped.data_pedido, proxima,
         f"e-CAC: {ped.situacao}", id_sistema),
    )
    return True


def parcelamentos(ctx: Contexto) -> Resultado:
    """params: {"modalidades": [...], "emitir": false, "varrer_todas": false}.

    `emitir` é **false por padrão**, ao contrário do pacote de origem: emitir
    guia é `/Emitir` (R$ 0,32 por parcela) e a guia do parcelamento já chega
    pelo Nibo no fluxo de hoje. O que faltava era o acordo, não o boleto.
    """
    modalidades = ctx.job.params.get("modalidades") or _modalidades_do_cliente(ctx)
    svc = Parcelamentos(ctx.client)
    resumo: dict[str, dict] = {}

    for mod in modalidades:
        aviso = None
        pedidos = svc.pedidos(ctx.job.cnpj, mod)
        ativos = sum(1 for p in pedidos if p.ativo)
        for ped in pedidos:
            ctx.conn.execute(
                """insert into integra_parc_pedidos (cnpj, id_sistema, numero, data_pedido, situacao,
                     data_situacao, ativo, bruto, atualizado_em)
                   values (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   on conflict (cnpj, id_sistema, numero) do update set situacao = excluded.situacao,
                     data_situacao = excluded.data_situacao, ativo = excluded.ativo,
                     bruto = excluded.bruto, atualizado_em = now()""",
                (ctx.job.cnpj, mod, ped.numero, ped.data_pedido, ped.situacao,
                 ped.data_situacao, ped.ativo, _json(ped.bruto)),
            )

        # Só busca parcelas se houver acordo ativo: é a segunda chamada paga da
        # modalidade, e para acordo rescindido não há nada a gerar.
        parcelas = svc.parcelas_disponiveis(ctx.job.cnpj, mod) if ativos else []
        for p in parcelas:
            ctx.conn.execute(
                """insert into integra_parc_parcelas (cnpj, id_sistema, parcela, data_vencimento, valor, bruto)
                   values (%s,%s,%s,%s,%s,%s)
                   on conflict (cnpj, id_sistema, parcela) do update set
                     data_vencimento = excluded.data_vencimento, valor = excluded.valor,
                     bruto = excluded.bruto""",
                (ctx.job.cnpj, mod, p.parcela, p.data_vencimento, p.valor, _json(p.bruto)),
            )

        # Espelha o acordo que explica a modalidade: o ativo, se houver; senão o
        # mais recente — é ele que diz *por que* não há acordo ativo (rescindido?
        # quitado?), e é essa explicação que a tela mostra ao lado do Nibo.
        escolhido = next((p for p in pedidos if p.ativo), None)
        if not escolhido and pedidos:
            escolhido = max(pedidos, key=lambda p: p.data_pedido or p.data_situacao or date.min)
        espelhado = bool(escolhido) and _espelhar(ctx, mod, escolhido, parcelas)

        emitidas = 0
        if parcelas and ctx.job.params.get("emitir", False):
            if len(parcelas) > LIMITE_PARCELAS_API:
                aviso = (f"{len(parcelas)} parcelas disponíveis; a API emite no máximo "
                         f"{LIMITE_PARCELAS_API}. Emitir pelo portal da Receita.")
            else:
                for p in parcelas:
                    try:
                        pdf = svc.emitir(ctx.job.cnpj, mod, p.parcela)
                    except IntegraErro as exc:
                        aviso = str(exc)
                        break
                    caminho = ctx.storage.salvar(
                        _caminho(ctx, "parcelamentos", f"{mod}_{p.parcela}.pdf"), pdf
                    )
                    ctx.conn.execute(
                        """update integra_parc_parcelas set pdf_path = %s
                            where cnpj = %s and id_sistema = %s and parcela = %s""",
                        (caminho, ctx.job.cnpj, mod, p.parcela),
                    )
                    emitidas += 1

        resumo[mod] = {"pedidos": len(pedidos), "ativos": ativos, "parcelas": len(parcelas),
                       "espelhado": espelhado, "guias": emitidas}
        if aviso:
            resumo[mod]["aviso"] = aviso

    _marcar_estado(ctx, parc_consultada_em=_agora())
    return Resultado.ok(**resumo)


# Só o que tem tabela no portal — ver o aviso no topo do arquivo.
HANDLERS: dict[str, Handler] = {
    "sitfis": sitfis,
    "caixa_postal": caixa_postal,
    "parcelamentos": parcelamentos,
}
