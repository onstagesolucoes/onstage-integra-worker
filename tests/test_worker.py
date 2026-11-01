"""O worker contra o esquema real do portal, em Postgres de verdade.

Nada aqui é simulado do lado do banco: as tabelas vêm das migrações que vão
para o Supabase (`conftest.banco`), com os CHECKs, os índices parciais e as
views como estão em produção. Só o SERPRO é falso — e tem de ser, porque cada
chamada de verdade é cobrada.

O que estes testes existem para pegar, em uma frase cada:

* a view do teto é **invisível** para o worker (`eh_interno()` → false), então
  o limite de gasto não pode ser lido dela;
* tipo de job sem handler tem de morrer **antes** da chamada paga;
* situação de parcelamento que o de-para não conhece não pode ser escrita em
  `parcelamentos_fiscais`, porque a linha do e-CAC tem precedência na view;
* o indicador gratuito da caixa postal é o que autoriza a lista paga;
* protocolo de SITFIS já comprado é reaproveitado, nunca recomprado.
"""
import json
import uuid

import httpx
import pytest
import psycopg

from conftest import PDF_B64

CNPJ = "11222333000181"
CNPJ_SEM_PROCURACAO = "99888777000166"


class StorageMemoria:
    def __init__(self):
        self.arquivos = {}

    def salvar(self, caminho, conteudo, content_type="application/pdf"):
        self.arquivos[caminho] = conteudo
        return caminho


@pytest.fixture
def conn(banco):
    with psycopg.connect(banco, autocommit=True) as c:
        yield c


@pytest.fixture
def ambiente(banco, conn, credenciais, serpro):
    """Banco limpo, dois clientes (um sem procuração) e um worker pronto.

    `integra_depara_modalidade` **não** é limpa: é dado de seed da migração, e
    apagá-la faria o teste de parcelamentos passar por motivo errado (sem
    de-para, não há modalidade a consultar e o job não gasta nada).
    """
    from integra_serpro import SerproClient
    from integra_serpro.auth import CacheMemoria
    from integra_serpro.fila.worker import Worker

    conn.execute("""
        truncate integra_chamadas, integra_eventos, integra_jobs, integra_estado_cliente,
                 integra_caixa_mensagens, integra_sitfis_relatorios, integra_sitfis_pendencias,
                 integra_sitfis_debitos, integra_parc_pedidos, integra_parc_parcelas,
                 integra_credenciais, integra_worker_pulso, parcelamentos_fiscais, clientes,
                 certificados_digitais restart identity cascade
    """)
    conn.execute(
        """insert into clientes (cnpj_norm, razao_social, ativo) values (%s, %s, true), (%s, %s, true)""",
        (CNPJ, "Clínica Teste", CNPJ_SEM_PROCURACAO, "Sem Procuração ME"),
    )
    conn.execute(
        "insert into certificados_digitais (cnpj, tipo, ativo) values (%s, 'procuracao_ecac', true)",
        (CNPJ,),
    )
    conn.execute(
        """insert into integra_credenciais (id, contratante_cnpj, nome_escritorio, cert_bucket_path,
             cert_senha_secret, consumer_key_secret, consumer_secret, teto_diario)
           values (1, %s, 'ON Soluções', 'on.pfx', 's1', 's2', 's3', 60.00)""",
        ("12345678000199",),
    )

    class Provider:
        def obter(self):
            return credenciais

    storage = StorageMemoria()
    fabrica = lambda c, **kw: SerproClient(
        c, cache=CacheMemoria(), transport=httpx.MockTransport(serpro), **kw
    )
    w = Worker(banco, provider=Provider(), storage=storage, fabrica_client=fabrica)
    yield w, storage
    w.fechar()


def _job(conn, tipo, params=None, cnpj=CNPJ):
    return str(conn.execute(
        "insert into integra_jobs (cnpj, tipo, params) values (%s,%s,%s) returning id",
        (cnpj, tipo, json.dumps(params or {})),
    ).fetchone()[0])


def _ler(conn, job_id):
    return conn.execute(
        "select status, estado, resultado, erro, tentativas from integra_jobs where id = %s",
        (job_id,),
    ).fetchone()


def _antecipar(conn, job_id):
    conn.execute("update integra_jobs set tentar_apos = now() where id = %s", (job_id,))


def _servicos_chamados(serpro):
    return [r["corpo"]["pedidoDados"]["idServico"] for r in serpro.requisicoes]


# ───────────────────────────── esquema e elegibilidade ─────────────────────────────
def test_habilitados_sai_da_procuracao_e_nao_de_cadastro_novo(ambiente, conn):
    """A lista de quem pode ser consultado é derivada, não cadastrada.

    Também prova o join entre `clientes.cnpj_norm` (text) e as tabelas novas
    (`char(14)`): se os tipos não conversassem, a view devolveria zero linha
    sem erro nenhum — a falha mais silenciosa possível neste módulo.
    """
    linhas = conn.execute("select cnpj, razao_social, caixa_a_monitorar, caixa_a_baixar from v_integra_habilitados").fetchall()
    assert [l[0] for l in linhas] == [CNPJ]
    # Nunca consultado: o indicador (gratuito) está liberado, a lista (paga) não.
    assert linhas[0][2] is True and linhas[0][3] is False


def test_cliente_sem_procuracao_morre_antes_de_gastar(ambiente, conn, serpro):
    """O que autoriza a consulta é a procuração do cliente, não o nosso certificado.

    O mTLS só prova quem é a ON. O acesso ao dado de cada CNPJ vem da procuração
    e-CAC, e sem ela o SERPRO devolve 4xx — **cobrando a chamada do mesmo
    jeito**. Como o impedimento já está no banco antes de qualquer rede, o job
    tem de morrer num `ValueError` aqui, e não num 4xx pago lá.

    Vale para os três caminhos de entrada (tela, n8n, `insert` à mão): o portal
    barra na origem, mas o gasto sai do worker, e é por isso que a última
    palavra é dele.
    """
    w, _ = ambiente
    jid = _job(conn, "caixa_postal", cnpj=CNPJ_SEM_PROCURACAO)
    w.processar_um()
    status, _, _, erro, _ = _ler(conn, jid)
    assert status == "erro" and "não está em v_integra_habilitados" in erro
    assert serpro.requisicoes == []
    assert conn.execute("select count(*) from integra_chamadas").fetchone()[0] == 0


def test_procuracao_vencida_sai_dos_habilitados(ambiente, conn, serpro):
    """Procuração vence, e vencer é o caso comum — não a exceção.

    `ativo` é marcação de gente; a data é fato. Um cliente habilitado hoje pode
    estar vencido em três meses, e com a caixa postal rodando **todo dia** isso
    seria pagar diariamente para ouvir "não". Por isso a view filtra pelos dois,
    e o worker obedece à view sem precisar saber por que o cliente saiu dela.
    """
    w, _ = ambiente
    conn.execute(
        "update certificados_digitais set data_vencimento = current_date - 1 where cnpj = %s",
        (CNPJ,),
    )
    assert conn.execute("select count(*) from v_integra_habilitados").fetchone()[0] == 0

    jid = _job(conn, "caixa_postal")
    w.processar_um()
    status, _, _, erro, _ = _ler(conn, jid)
    assert status == "erro" and "procuração e-CAC" in erro
    assert serpro.requisicoes == []

    # Renovada, o mesmo cliente volta à lista sem intervenção nenhuma: quem
    # manda é a data, e a tela de configuração do cliente é o onboarding.
    conn.execute(
        "update certificados_digitais set data_vencimento = current_date + 30 where cnpj = %s",
        (CNPJ,),
    )
    assert conn.execute("select count(*) from v_integra_habilitados").fetchone()[0] == 1


def test_teto_da_view_e_invisivel_para_o_worker(ambiente, conn):
    """`v_integra_teto_hoje` devolve zero linha para quem não é interno.

    Este é o teste que justifica o worker ler `integra_credenciais` direto. A
    view termina em `where public.eh_interno()`, e `eh_interno()` depende de
    `auth.uid()`; numa conexão psycopg sem JWT isso é nulo. Ler o teto dali e
    tratar "sem linha" como "sem teto" devolveria o limite de gasto ao estado
    decorativo.
    """
    w, _ = ambiente
    assert conn.execute("select count(*) from v_integra_teto_hoje").fetchone()[0] == 0
    atingido, gasto, teto = w.teto_atingido()
    assert (atingido, str(gasto), str(teto)) == (False, "0", "60.00")


def test_sem_credencial_o_worker_para(ambiente, conn):
    """"Não achei a configuração" não pode virar "pode gastar à vontade"."""
    w, _ = ambiente
    conn.execute("delete from integra_credenciais")
    atingido, _, _ = w.teto_atingido()
    assert atingido is True


def test_teto_atingido_grava_um_evento_por_dia(ambiente, conn):
    w, _ = ambiente
    for _ in range(2):
        conn.execute(
            """insert into integra_chamadas (chave_servico, id_sistema, id_servico, endpoint,
                 cobranca, sucesso, custo_estimado)
               values ('x','SITFIS','RELATORIOSITFIS92','/Emitir','emitir', true, 30.00)"""
        )
    atingido, gasto, teto = w.teto_atingido()
    assert atingido and str(gasto) == "60.00"
    w._registrar_teto(gasto, teto)
    w._registrar_teto(gasto, teto)
    assert conn.execute(
        "select count(*) from integra_eventos where evento = 'teto_diario_atingido'"
    ).fetchone()[0] == 1


def test_chamada_nao_precificada_nao_vira_zero(ambiente, conn, serpro):
    """`/Apoiar` sai como NULL, não como 0.00 — "não sei" ≠ "não custou"."""
    w, _ = ambiente
    serpro.responder("SOLICITARPROTOCOLO91", dados={"protocoloRelatorio": "P1", "tempoEspera": 5000})
    _job(conn, "sitfis")
    assert w.processar_um()
    custo, endpoint, cobranca = conn.execute(
        "select custo_estimado, endpoint, cobranca from integra_chamadas where id_servico = 'SOLICITARPROTOCOLO91'"
    ).fetchone()
    assert custo is None and endpoint == "Apoiar"
    # `cobranca` tem CHECK no banco: o enum do pacote escreve minúsculo porque é
    # o que o CHECK aceita. Qualquer divergência aqui é insert recusado.
    assert cobranca == "apoiar"


# ───────────────────────────────────── SITFIS ─────────────────────────────────────
def test_sitfis_em_duas_etapas(ambiente, conn, serpro):
    w, storage = ambiente
    serpro.responder("SOLICITARPROTOCOLO91", dados={"protocoloRelatorio": "P1", "tempoEspera": 5000})
    serpro.responder("RELATORIOSITFIS92", dados={"pdf": PDF_B64})

    jid = _job(conn, "sitfis")
    assert w.processar_um()
    status, estado, *_ = _ler(conn, jid)
    assert status == "aguardando" and estado == {"protocolo": "P1"}
    assert not w.processar_um(), "a espera do protocolo tem de prender o job, não o worker"

    _antecipar(conn, jid)
    assert w.processar_um()
    assert _ler(conn, jid)[0] == "ok"

    caminho, = list(storage.arquivos)
    assert caminho.startswith(f"{CNPJ}/sitfis/")
    pdf_path, sha, versao = conn.execute(
        "select pdf_path, pdf_sha256, parser_versao from integra_sitfis_relatorios where cnpj = %s", (CNPJ,)
    ).fetchone()
    assert pdf_path == caminho and len(sha) == 64 and versao == "sitfis-1"

    # Protocolo consumido tem de sair do estado: reaproveitá-lo levaria 4xx.
    protocolo, consultado = conn.execute(
        "select sitfis_protocolo, sitfis_consultado_em from integra_estado_cliente where cnpj = %s", (CNPJ,)
    ).fetchone()
    assert protocolo is None and consultado is not None
    assert conn.execute("select count(*) from integra_chamadas where cnpj = %s", (CNPJ,)).fetchone()[0] == 2


def test_sitfis_reaproveita_protocolo_ja_comprado(ambiente, conn, serpro):
    """Job novo com protocolo recente na mão não compra outro protocolo.

    É o caso do job cancelado ou morto entre as duas etapas. Sem isso, cada
    reinício do container recomeçaria o SITFIS do zero — e a primeira etapa é
    chamada paga (preço desconhecido, o que é pior).
    """
    w, _ = ambiente
    conn.execute(
        """insert into integra_estado_cliente (cnpj, sitfis_protocolo, sitfis_tentar_apos)
           values (%s, 'P-ANTIGO', now())""",
        (CNPJ,),
    )
    serpro.responder("RELATORIOSITFIS92", dados={"pdf": PDF_B64})
    jid = _job(conn, "sitfis")
    assert w.processar_um()
    assert _ler(conn, jid)[0] == "ok"
    assert _servicos_chamados(serpro) == ["RELATORIOSITFIS92"]
    assert conn.execute(
        "select protocolo from integra_sitfis_relatorios where cnpj = %s", (CNPJ,)
    ).fetchone()[0] == "P-ANTIGO"


def test_sitfis_204_reagenda_sem_perder_o_protocolo(ambiente, conn, serpro):
    """204 do SERPRO é "ainda processando", não erro — e o protocolo continua valendo."""
    w, _ = ambiente
    serpro.responder("SOLICITARPROTOCOLO91", dados={"protocoloRelatorio": "P1", "tempoEspera": 1000})
    serpro.responder("RELATORIOSITFIS92", status=204)
    jid = _job(conn, "sitfis")
    w.processar_um()
    _antecipar(conn, jid)
    w.processar_um()
    status, estado, _, erro, tentativas = _ler(conn, jid)
    assert status == "aguardando" and estado == {"protocolo": "P1"} and erro is None
    assert tentativas == 0, "espera não é tentativa fracassada; zerar evita estourar max_tentativas"


def test_pdf_identico_nao_cria_segundo_relatorio(ambiente, conn, serpro):
    """Cliente sem movimento devolve o mesmo PDF; `v_sitfis_atual` não pode duplicar."""
    w, _ = ambiente
    for _ in range(2):
        serpro.responder("SOLICITARPROTOCOLO91", dados={"protocoloRelatorio": "P1", "tempoEspera": 1000})
        serpro.responder("RELATORIOSITFIS92", dados={"pdf": PDF_B64})
    for _ in range(2):
        jid = _job(conn, "sitfis")
        w.processar_um()
        _antecipar(conn, jid)
        w.processar_um()
    assert conn.execute("select count(*) from integra_sitfis_relatorios where cnpj = %s", (CNPJ,)).fetchone()[0] == 1
    # A quinta fonte de `lib/situacao-fiscal.ts` devolve uma linha por cliente.
    # (RLS não filtra aqui: o teste conecta como dono da tabela, igual ao worker
    # em produção. O que ainda filtra é `where eh_interno()` escrito no corpo da
    # view — ver `test_teto_da_view_e_invisivel_para_o_worker`.)
    assert conn.execute("select count(*) from v_sitfis_atual where cnpj = %s", (CNPJ,)).fetchone()[0] == 1


# ─────────────────────────────────── caixa postal ───────────────────────────────────
def test_indicador_gratuito_sem_novidade_nao_puxa_a_lista_paga(ambiente, conn, serpro):
    """A ordem das chamadas **é** a economia do módulo.

    `INNOVAMSG63` vai para /Monitorar e não custa nada, então roda todo dia para
    todos; `MSGCONTRIBUINTE61` é /Consultar, paga, e só quando o indicador
    acusou novidade. Inverter isso multiplicaria a conta por 97.
    """
    w, _ = ambiente
    serpro.responder("INNOVAMSG63", dados=[{"indicadorMensagensNovas": "0"}])
    jid = _job(conn, "caixa_postal")
    w.processar_um()

    assert _ler(conn, jid)[2] == {"novas": 0, "gravadas": 0, "detalhes": 0}
    assert _servicos_chamados(serpro) == ["INNOVAMSG63"]
    custo, = conn.execute("select custo_estimado from integra_chamadas").fetchone()
    assert str(custo) == "0.00", "gratuita e verificada: 0.00, não NULL"
    # A consulta gratuita ficou registrada: "a Receita disse que não há novidade"
    # é informação, e é diferente de nunca ter consultado.
    monitorada, baixada = conn.execute(
        "select caixa_monitorada_em, caixa_baixada_em from integra_estado_cliente where cnpj = %s", (CNPJ,)
    ).fetchone()
    assert monitorada is not None and baixada is None


def test_caixa_postal_grava_mensagens_e_detalhe(ambiente, conn, serpro):
    w, _ = ambiente
    serpro.responder("INNOVAMSG63", dados=[{"indicadorMensagensNovas": "1"}])
    serpro.responder("MSGCONTRIBUINTE61", dados={"conteudo": [{
        "indicadorUltimaPagina": "S",
        "listaMensagens": [{"isn": "77", "assuntoModelo": "Aviso", "dataEnvio": "20261020",
                            "horaEnvio": "143000", "dataValidade": "20261120",
                            "indicadorLeitura": "N", "descricaoOrigem": "RFB"}],
    }]})
    serpro.responder("MSGDETALHAMENTO62", dados={"conteudo": [
        {"corpoModelo": "Prezado ++VARIAVEL++", "variaveis": ["contribuinte"]}
    ]})

    jid = _job(conn, "caixa_postal")
    w.processar_um()
    assert _ler(conn, jid)[2] == {"novas": 1, "gravadas": 1, "detalhes": 1}

    corpo, baixado, triagem, lida, expira, hora = conn.execute(
        """select corpo, detalhe_baixado, triagem, lida_no_ecac, data_expiracao, hora_envio
             from integra_caixa_mensagens where cnpj = %s and isn = '77'""",
        (CNPJ,),
    ).fetchone()
    assert corpo == "Prezado contribuinte" and baixado
    assert triagem == "nova", "a triagem é do time; a carga nunca a toca"
    assert lida is False
    # `dataValidade` na lista × `dataExpiracao` no detalhe: ler só o segundo
    # deixaria vazia justamente a carga que roda todo dia.
    assert str(expira) == "2026-11-20" and hora == "14:30:00"


def test_caixa_postal_relida_nao_duplica_nem_repaga_o_detalhe(ambiente, conn, serpro):
    """A lista é relida todo dia; o ISN dá a idempotência e o detalhe não repete."""
    w, _ = ambiente
    for _ in range(2):
        serpro.responder("INNOVAMSG63", dados=[{"indicadorMensagensNovas": "1"}])
        serpro.responder("MSGCONTRIBUINTE61", dados={"conteudo": [{
            "indicadorUltimaPagina": "S",
            "listaMensagens": [{"isn": "77", "assuntoModelo": "Aviso"}],
        }]})
    serpro.responder("MSGDETALHAMENTO62", dados={"conteudo": [{"corpoModelo": "Corpo"}]})

    _job(conn, "caixa_postal"); w.processar_um()
    jid = _job(conn, "caixa_postal"); w.processar_um()

    assert conn.execute("select count(*) from integra_caixa_mensagens where cnpj = %s", (CNPJ,)).fetchone()[0] == 1
    assert _ler(conn, jid)[2]["detalhes"] == 0
    assert _servicos_chamados(serpro).count("MSGDETALHAMENTO62") == 1


def test_teto_de_detalhes_por_rodada(ambiente, conn, serpro):
    """Caixa com 200 mensagens atrasadas não pode virar 200 chamadas num job só."""
    w, _ = ambiente
    serpro.responder("INNOVAMSG63", dados=[{"indicadorMensagensNovas": "5"}])
    serpro.responder("MSGCONTRIBUINTE61", dados={"conteudo": [{
        "indicadorUltimaPagina": "S",
        "listaMensagens": [{"isn": str(i), "assuntoModelo": "A"} for i in range(5)],
    }]})
    serpro.responder("MSGDETALHAMENTO62", dados={"conteudo": [{"corpoModelo": "C"}]})
    jid = _job(conn, "caixa_postal", {"max_detalhes": 2})
    w.processar_um()
    assert _ler(conn, jid)[2] == {"novas": 5, "gravadas": 5, "detalhes": 2}
    assert _servicos_chamados(serpro).count("MSGDETALHAMENTO62") == 2


# ─────────────────────────────────── parcelamentos ───────────────────────────────────
def _nibo(conn, modalidade="Parcelamento do Simples Nacional", situacao="ativo"):
    conn.execute(
        """insert into parcelamentos_fiscais (cnpj_norm, orgao, modalidade, situacao, origem)
           values (%s, 'RFB', %s, %s, 'nibo')""",
        (CNPJ, modalidade, situacao),
    )


def test_consulta_so_a_modalidade_que_o_nibo_indica(ambiente, conn, serpro):
    """Varrer 6 modalidades × 97 clientes é consulta paga em quem nunca parcelou.

    O Nibo já sabe quem tem acordo (tem vaga mensal de guia no calendário), e o
    join com o de-para corta de graça o que não existe nesta API.
    """
    w, _ = ambiente
    _nibo(conn)
    serpro.responder("PEDIDOSPARC163", dados={"parcelamentos": [
        {"numero": "9001", "situacao": "Em parcelamento", "dataDoPedido": "20250310"}
    ]})
    serpro.responder("PARCELASPARAGERAR162", dados={"listaParcelas": [
        {"parcelaParaEmitir": "202611", "dataVencimento": "20261120", "valor": 310.55}
    ]})

    jid = _job(conn, "parcelamentos")
    w.processar_um()
    assert _ler(conn, jid)[0] == "ok"
    # PARCSN e só: nada de PERTSN, RELPMEI e companhia.
    assert _servicos_chamados(serpro) == ["PEDIDOSPARC163", "PARCELASPARAGERAR162"]
    assert _ler(conn, jid)[2]["PARCSN"] == {"pedidos": 1, "ativos": 1, "parcelas": 1,
                                            "espelhado": True, "guias": 0}
    # `emitir` é false por padrão: a guia do parcelamento já chega pelo Nibo.
    assert conn.execute("select pdf_path from integra_parc_parcelas").fetchone()[0] is None
    assert conn.execute(
        "select id_sistema, numero, ativo from integra_parc_pedidos where cnpj = %s", (CNPJ,)
    ).fetchone() == ("PARCSN", "9001", True)


def test_espelho_ecac_convive_com_o_nibo_e_marca_divergencia(ambiente, conn, serpro):
    """O produto é a divergência: Nibo diz ativo (tem vaga de guia), e-CAC diz rescindido."""
    w, _ = ambiente
    _nibo(conn)
    serpro.responder("PEDIDOSPARC163", dados={"parcelamentos": [
        {"numero": "9001", "situacao": "Rescindido por falta de pagamento", "dataDoPedido": "20240110"}
    ]})
    _job(conn, "parcelamentos")
    w.processar_um()

    # Nada apagado: as duas linhas convivem no mesmo grão, uma por índice parcial.
    origens = conn.execute(
        "select origem, situacao from parcelamentos_fiscais where cnpj_norm = %s order by origem", (CNPJ,)
    ).fetchall()
    assert origens == [("ecac", "rescindido"), ("nibo", "ativo")]

    efetivo = conn.execute(
        """select origem, situacao, numero, tem_divergencia, origens
             from v_parcelamento_efetivo where cnpj_norm = %s""",
        (CNPJ,),
    ).fetchone()
    assert efetivo[0] == "ecac" and efetivo[1] == "rescindido" and efetivo[2] == "9001"
    assert efetivo[3] is True and sorted(efetivo[4]) == ["ecac", "nibo"]
    # Acordo rescindido não tem parcela a gerar: a segunda chamada paga não sai.
    assert _servicos_chamados(serpro) == ["PEDIDOSPARC163"]


def test_situacao_desconhecida_nao_escreve_e_vira_evento(ambiente, conn, serpro):
    """`parcelamentos_fiscais.situacao` aceita quatro valores; o e-CAC escreve prosa.

    Chutar um deles faria a tela afirmar, com a autoridade do e-CAC, um estado
    que ninguém leu — e a linha do e-CAC tem precedência na view. Então nada é
    escrito, alguém é avisado, e a linha do Nibo continua respondendo.
    """
    w, _ = ambiente
    _nibo(conn)
    serpro.responder("PEDIDOSPARC163", dados={"parcelamentos": [
        {"numero": "9001", "situacao": "Aguardando análise da autoridade fiscal"}
    ]})
    jid = _job(conn, "parcelamentos")
    w.processar_um()

    assert _ler(conn, jid)[0] == "ok", "situação nova não é falha do job"
    assert _ler(conn, jid)[2]["PARCSN"]["espelhado"] is False
    assert conn.execute(
        "select count(*) from parcelamentos_fiscais where cnpj_norm = %s and origem = 'ecac'", (CNPJ,)
    ).fetchone()[0] == 0
    detalhe, = conn.execute(
        "select detalhe from integra_eventos where evento = 'parcelamento_situacao_desconhecida'"
    ).fetchone()
    assert detalhe["situacao"] == "Aguardando análise da autoridade fiscal"
    # O acordo bruto fica gravado do mesmo jeito: o de-para corrigido depois
    # reprocessa daqui, sem pagar a consulta de novo.
    assert conn.execute("select situacao from integra_parc_pedidos").fetchone()[0].startswith("Aguardando")
    # E a tela continua respondendo pelo Nibo, nem melhor nem pior que hoje.
    assert conn.execute(
        "select origem from v_parcelamento_efetivo where cnpj_norm = %s", (CNPJ,)
    ).fetchone()[0] == "nibo"


def test_cliente_sem_acordo_no_nibo_nao_gasta_nada(ambiente, conn, serpro):
    w, _ = ambiente
    jid = _job(conn, "parcelamentos")
    w.processar_um()
    assert _ler(conn, jid)[0] == "ok" and _ler(conn, jid)[2] == {}
    assert serpro.requisicoes == []
    assert conn.execute("select count(*) from integra_chamadas").fetchone()[0] == 0


def test_varrer_todas_e_a_saida_para_o_ponto_cego(ambiente, conn, serpro):
    """Acordo que só existe no e-CAC não é descoberto pela regra do Nibo.

    `varrer_todas` é a cadência larga que acha esses casos — e é o que revela
    guia faltando no calendário.
    """
    w, _ = ambiente
    for id_servico in ("PEDIDOSPARC163", "PEDIDOSPARC203", "PEDIDOSPARC183",
                       "PEDIDOSPARC223", "PEDIDOSPARC173", "PEDIDOSPARC233"):
        serpro.responder(id_servico, dados={"parcelamentos": []})
    jid = _job(conn, "parcelamentos", {"varrer_todas": True})
    w.processar_um()
    assert _ler(conn, jid)[0] == "ok"
    assert len(_ler(conn, jid)[2]) == 6
    # RELP-SN é a modalidade fora do padrão (PEDIDOSPARC173 + PARCELASPARAGERAR192,
    # confirmado na decompilação); se o catálogo regredir, é aqui que aparece.
    assert "PEDIDOSPARC173" in _servicos_chamados(serpro)


# ──────────────────────────────── desfechos de falha ────────────────────────────────
def test_tipo_sem_handler_morre_antes_de_gastar(ambiente, conn, serpro):
    """Handler registrado sem tabela de destino é a falha mais cara possível.

    O SERPRO seria chamado, **a chamada seria paga**, e só então o `insert`
    estouraria em `relation does not exist` — e como isso não é erro definitivo,
    a fila tentaria de novo, pagando de novo. Fora do dicionário, o job morre
    antes de abrir conexão.
    """
    w, _ = ambiente
    jid = _job(conn, "das_simples", {"periodo_apuracao": "202409"})
    w.processar_um()
    status, _, _, erro, _ = _ler(conn, jid)
    assert status == "erro" and "não implantado" in erro
    assert "caixa_postal, parcelamentos, sitfis" in erro
    assert serpro.requisicoes == []
    assert conn.execute("select count(*) from integra_chamadas").fetchone()[0] == 0


def test_erro_4xx_e_definitivo(ambiente, conn, serpro):
    """4xx é pedido errado, não rede instável: repetir só paga de novo pelo mesmo "não"."""
    w, _ = ambiente
    _nibo(conn)
    serpro.responder("PEDIDOSPARC163", status=400,
                     mensagens=[{"codigo": "E1", "texto": "Procuração eletrônica inválida"}])
    jid = _job(conn, "parcelamentos")
    w.processar_um()
    status, _, _, erro, tentativas = _ler(conn, jid)
    assert status == "erro" and "Procuração" in erro and tentativas == 1
    # A chamada falhou e foi paga igual: tem de constar no custo.
    assert conn.execute("select count(*) from integra_chamadas where sucesso = false").fetchone()[0] == 1
    # E o cliente fica marcado: falha silenciosa é indistinguível de dado atual.
    assert "Procuração" in conn.execute(
        "select ultimo_erro from integra_estado_cliente where cnpj = %s", (CNPJ,)
    ).fetchone()[0]


def test_erro_5xx_reagenda(ambiente, conn, serpro):
    w, _ = ambiente
    _nibo(conn)
    serpro.responder("PEDIDOSPARC163", status=503)
    jid = _job(conn, "parcelamentos")
    w.processar_um()
    status, _, _, _, tentativas = _ler(conn, jid)
    assert status == "fila" and tentativas == 1
    assert not w.processar_um(), "a espera crescente tem de valer já na primeira falha"


def test_registro_de_custo_sobrevive_ao_rollback(ambiente, conn, serpro):
    """A transação do job é desfeita; a chamada ao SERPRO não.

    Por isso `integra_chamadas` é escrita em conexão própria, em autocommit. Na
    mesma transação, o rollback apagaria a prova do gasto — e o teto do dia
    passaria a somar menos do que a fatura.
    """
    w, _ = ambiente
    _nibo(conn)
    serpro.responder("PEDIDOSPARC163", dados={"parcelamentos": [
        {"numero": "9001", "situacao": "Em parcelamento"}
    ]})
    # Sem resposta para PARCELASPARAGERAR162: o fake devolve 500 e o job falha
    # **depois** de a consulta de pedidos ter sido paga.
    jid = _job(conn, "parcelamentos")
    w.processar_um()
    assert _ler(conn, jid)[0] == "fila"
    assert conn.execute("select count(*) from integra_parc_pedidos").fetchone()[0] == 0, "rollback do dado"
    assert conn.execute(
        "select count(*) from integra_chamadas where id_servico = 'PEDIDOSPARC163'"
    ).fetchone()[0] == 1, "o gasto permanece registrado"


def test_job_travado_por_outra_replica_e_pulado_nao_esperado(ambiente, banco, conn, serpro):
    """`for update skip locked` é o que permite 2 réplicas sem job duplicado.

    O teste trava a linha numa segunda conexão (é o que a outra réplica faz) e
    exige que o worker **devolva vazio na hora**, em vez de ficar bloqueado no
    `select`. Se fosse `for update` sem `skip locked`, este teste penduraria —
    e em produção duas réplicas se serializariam em fila indiana.
    """
    w, _ = ambiente
    serpro.responder("INNOVAMSG63", dados=[{"indicadorMensagensNovas": "0"}])
    jid = _job(conn, "caixa_postal")

    with psycopg.connect(banco) as outra:
        outra.execute("select id from integra_jobs where id = %s for update", (jid,))
        assert not w.processar_um(), "job travado por outra réplica tem de ser pulado"
        outra.rollback()

    assert w.processar_um()
    assert _ler(conn, jid)[0] == "ok"


def test_idem_key_impede_job_repetido(ambiente, conn):
    """Clique duplo e agendamento repetido não podem virar duas consultas pagas."""
    chave = f"sitfis:{CNPJ}:{uuid.uuid4()}"
    conn.execute("insert into integra_jobs (cnpj, tipo, idem_key) values (%s,'sitfis',%s)", (CNPJ, chave))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into integra_jobs (cnpj, tipo, idem_key) values (%s,'sitfis',%s)", (CNPJ, chave))
