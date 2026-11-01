import io
import json
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from integra_serpro.erros import IntegraErro
from integra_serpro.exportacao.dominio import DePara, PagamentoExportavel, gerar_txt, gerar_zip
from integra_serpro.servicos import CaixaPostal, Guias, Mit, Pagamentos, Parcelamentos, Sitfis
from integra_serpro.servicos import mit as mit_mod
from integra_serpro.servicos.caixa_postal import aplicar_variaveis

from conftest import PDF_B64, PDF_FAKE

CNPJ = "11222333000181"


def _dados(serpro, i=-1):
    return json.loads(serpro.requisicoes[i]["corpo"]["pedidoDados"]["dados"] or "null")


def test_sitfis_duas_etapas(client_fake, serpro):
    serpro.responder("SOLICITARPROTOCOLO91", dados={"protocoloRelatorio": "ABC", "tempoEspera": 4000})
    p = Sitfis(client_fake).solicitar_protocolo(CNPJ)
    assert (p.protocolo, p.espera_segundos) == ("ABC", 4)
    serpro.responder("RELATORIOSITFIS92", dados={"pdf": PDF_B64})
    assert Sitfis(client_fake).obter_relatorio(CNPJ, "ABC") == PDF_FAKE
    assert _dados(serpro) == {"protocoloRelatorio": "ABC"}
    assert serpro.requisicoes[-1]["headers"]["accept"] == "text/plain"


def test_sitfis_304_reaproveita_protocolo(client_fake, serpro):
    serpro.responder("SOLICITARPROTOCOLO91", status=304, headers={"etag": '"protocoloRelatorio:XYZ"'})
    assert Sitfis(client_fake).solicitar_protocolo(CNPJ).protocolo == "XYZ"


def test_sitfis_protocolo_vem_do_header_mesmo_no_200(client_fake, serpro):
    """O protocolo mora no cabeçalho `etag`; o corpo é o segundo lugar a olhar.

    Quem lê só o JSON recebe 200 e conclui "o SERPRO não devolveu o protocolo".
    """
    serpro.responder("SOLICITARPROTOCOLO91", dados={"tempoEspera": 4000},
                     headers={"etag": '"protocoloRelatorio:DO-HEADER"'})
    p = Sitfis(client_fake).solicitar_protocolo(CNPJ)
    assert (p.protocolo, p.espera_segundos) == ("DO-HEADER", 4)


def test_sitfis_relatorio_e_emissao(client_fake, serpro):
    """SITFIS é /Emitir: tratar como consulta subestima o custo em um terço."""
    serpro.responder("RELATORIOSITFIS92", dados={"pdf": PDF_B64})
    Sitfis(client_fake).obter_relatorio(CNPJ, "ABC")
    reg = client_fake.registros[-1]
    assert reg.cobranca == "emitir" and reg.custo_estimado == Decimal("0.32")


def test_guias(client_fake, serpro):
    serpro.responder("GERARDAS12", dados=[{"pdf": PDF_B64}])
    assert Guias(client_fake).das_simples(CNPJ, "202409", "20241031") == PDF_FAKE
    assert _dados(serpro) == {"periodoApuracao": "202409", "dataConsolidacao": "20241031"}
    serpro.responder("GERARGUIA31", dados=[{"PDFByteArrayBase64": PDF_B64}])
    Guias(client_fake).darf_dctfweb(CNPJ, "GERAL_MENSAL", competencia="202409")
    assert _dados(serpro) == {"categoria": "GERAL_MENSAL", "anoPA": "2024", "mesPA": "09"}
    with pytest.raises(ValueError):
        Guias(client_fake).das_mei(CNPJ, "202413")


def test_caixa_postal_paginacao(client_fake, serpro):
    pag1 = {"conteudo": [{"indicadorUltimaPagina": "N", "ponteiroProximaPagina": "P2",
                          "listaMensagens": [{"isn": "1", "assuntoModelo": "Intimação ++VARIAVEL++", "valorParametroAssunto": "123", "dataEnvio": "20241001", "horaEnvio": "093000", "dataValidade": "20241231"}]}]}
    pag2 = {"conteudo": [{"indicadorUltimaPagina": "S", "listaMensagens": [{"isn": "2", "assuntoModelo": "Aviso"}]}]}
    serpro.responder("MSGCONTRIBUINTE61", dados=pag1)
    serpro.responder("MSGCONTRIBUINTE61", dados=pag2)
    msgs = list(CaixaPostal(client_fake).listar(CNPJ))
    assert [m.isn for m in msgs] == ["1", "2"]
    assert msgs[0].assunto == "Intimação 123" and msgs[0].hora_envio == "09:30:00" and msgs[0].data_envio == date(2024, 10, 1)
    # A lista chama de `dataValidade` (só o detalhe usa `dataExpiracao`): ler
    # apenas o segundo deixava esta data vazia na carga que roda todo dia.
    assert msgs[0].data_expiracao == date(2024, 12, 31)
    # Os três campos vão como string, como o produto do fornecedor envia.
    assert _dados(serpro) == {"statusLeitura": "0", "indicadorPagina": "1", "ponteiroPagina": "P2"}
    # Primeira página não manda ponteiro nenhum.
    assert "ponteiroPagina" not in _dados(serpro, 0)


def test_caixa_indicador_e_gratuito(client_fake, serpro):
    """O indicador vai para /Monitorar e custa zero — é ele que autoriza o gasto."""
    serpro.responder("INNOVAMSG63", dados={"indicadorMensagensNovas": "2"})
    assert CaixaPostal(client_fake).indicador_novas(CNPJ) == 2
    assert serpro.requisicoes[-1]["url"].endswith("/Monitorar")
    reg = client_fake.registros[-1]
    assert reg.cobranca == "monitorar" and reg.custo_estimado == Decimal("0.00")


def test_aplicar_variaveis():
    assert aplicar_variaveis("A ++VARIAVEL++ e ++VARIAVEL++", ["x", "y", "z"]) == "A x e y"


def test_pagamentos_paginacao(client_fake, serpro):
    item = lambda n: {"numeroDocumento": str(n), "dataArrecadacao": "2024-09-20", "receitaPrincipal": {"codigo": "1708", "descricao": "IRPJ"},
                      "valorPrincipal": 100, "valorMulta": "1,50", "valorJuros": 0.5, "valorTotal": 102,
                      "desmembramentos": [{"sequencial": "1", "receitaPrincipal": {"codigo": "1708"}, "valorPrincipal": 100}]}
    serpro.responder("PAGAMENTOS71", dados=[item(1), item(2)])
    serpro.responder("PAGAMENTOS71", dados=[item(3)])
    pgs = list(Pagamentos(client_fake).consultar(CNPJ, date(2024, 1, 1), date(2024, 9, 30), tamanho=2))
    assert [p.numero_documento for p in pgs] == ["1", "2", "3"]
    assert pgs[0].valor_multa == Decimal("1.50") and pgs[0].receita_codigo == "1708"
    assert _dados(serpro)["primeiroDaPagina"] == 2


def test_parcelamentos(client_fake, serpro):
    serpro.responder("PEDIDOSPARC163", dados={"parcelamentos": [{"numero": 9, "situacao": "Em parcelamento", "dataDoPedido": 20230105}]})
    peds = Parcelamentos(client_fake).pedidos(CNPJ, "PARCSN")
    assert peds[0].ativo and peds[0].data_pedido == date(2023, 1, 5)
    serpro.responder("PARCELASPARAGERAR162", dados={"listaParcelas": [{"parcelaParaEmitir": 202409 + i, "valor": 10} for i in range(7)]})
    with pytest.raises(IntegraErro, match="no máximo 6"):
        Parcelamentos(client_fake).emitir_todas(CNPJ, "PARCSN")


def test_mit_validacao():
    payload = {"PeriodoApuracao": {"MesApuracao": 9, "AnoApuracao": 2024}, "DadosIniciais": {"SemMovimento": False},
               "Debitos": {"Irpj": {"ListaDebitos": [{"ValorDebito": "1500.10"}]}}}
    v = mit_mod.validar("12345678 - MIT - 202409.json", payload)
    assert v.ok and v.total_debitos == 1 and v.valor_total == Decimal("1500.10")
    v2 = mit_mod.validar("12345678 - MIT - 202408.json", payload)
    assert any("divergente" in e for e in v2.erros)


def test_mit_situacao(client_fake, serpro):
    serpro.responder("SITUACAOENC315", dados={"situacaoApuracao": 3, "textoSituacao": "ENCERRADA", "avisosDctf": []})
    s = Mit(client_fake).situacao("12345678000199", "PROT")
    assert s.final and s.codigo == 3


def test_dominio_txt_e_zip():
    dp = {"1708": DePara("1708", "100", "200", conta_debito_juros="300", conta_debito_multa="400", codigo_historico="15")}
    pg = PagamentoExportavel("a", "DOC1", "1708", "IRPJ", date(2024, 9, 20), Decimal("100"), Decimal("0.5"), Decimal("1.5"))
    txt, ids = gerar_txt("11.222.333/0001-81", [pg], dp)
    linhas = txt.strip().split("\r\n")
    assert linhas[0] == "|0000|11222333000181|"
    assert linhas[2] == "|6100|20/09/2024|100|200|100,00|15|Pagamento IRPJ|GERENTE|||"
    assert linhas[4].startswith("|6100|20/09/2024|300|200|0,50|") and ids == ["a"]
    with pytest.raises(IntegraErro, match="De-para"):
        gerar_txt("11222333000181", [pg], {})
    conteudo, exportados = gerar_zip([("Clínica São José", "11222333000181", [pg], dp)])
    assert zipfile.ZipFile(io.BytesIO(conteudo)).namelist() == ["Clinica_Sao_Jose/092024.txt"]
