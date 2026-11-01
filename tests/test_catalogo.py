"""Catálogo e preço: o que a decompilação do produto do fornecedor confirmou.

Estes testes existem para travar o que custou trabalho descobrir — códigos de
serviço que não estão em documentação nenhuma e a conta de custo que faz o teto
diário do portal funcionar.
"""
from decimal import Decimal

import httpx
import pytest

from integra_serpro import SERVICOS, SerproClient
from integra_serpro.auth import CacheMemoria
from integra_serpro.catalogo import MODALIDADES, Cobranca, Endpoint, servicos_parcelamento, total_servicos
from integra_serpro.preco import custo

# Exatamente o `check` de `integra_chamadas.cobranca` na migração do portal.
# Valor fora desta lista faz o insert do worker ser recusado pelo Postgres, e o
# sintoma aparece longe daqui.
COBRANCA_NO_PORTAL = {"consultar", "emitir", "declarar", "apoiar", "monitorar"}


def test_cobranca_e_sempre_o_endpoint_em_minusculas():
    todos = list(SERVICOS.values()) + [
        s for codigo in MODALIDADES for s in servicos_parcelamento(codigo).values()
    ]
    assert len(todos) == total_servicos() == 30
    for s in todos:
        assert s.cobranca.value in COBRANCA_NO_PORTAL
        assert s.cobranca.value == s.endpoint.value.lower()


def test_indicador_da_caixa_vai_para_monitorar():
    """A chamada gratuita é a que autoriza a paga — não podem se confundir."""
    assert SERVICOS["caixa_indicador"].endpoint is Endpoint.MONITORAR
    assert SERVICOS["caixa_lista"].endpoint is Endpoint.CONSULTAR


def test_sitfis_relatorio_e_emissao_nao_consulta():
    assert SERVICOS["sitfis_protocolo"].endpoint is Endpoint.APOIAR
    assert SERVICOS["sitfis_relatorio"].endpoint is Endpoint.EMITIR


def test_relp_sn_tem_pedidos_fora_da_trinca():
    """A exceção confirmada no produto: `173`, não `193`.

    Normalizar para `193` quebra só o RELP-SN, sem erro visível — a chamada
    passa a consultar outro serviço e volta vazia.
    """
    assert servicos_parcelamento("RELPSN")["pedidos"].id_servico == "PEDIDOSPARC173"
    assert servicos_parcelamento("RELPSN")["parcelas"].id_servico == "PARCELASPARAGERAR192"
    assert servicos_parcelamento("RELPSN")["emitir"].id_servico == "GERARDAS191"
    # As outras cinco seguem a trinca consecutiva.
    for codigo in ("PARCSN", "PARCMEI", "PERTSN", "PERTMEI", "RELPMEI"):
        s = servicos_parcelamento(codigo)
        base = int(s["emitir"].id_servico[-3:])
        assert int(s["parcelas"].id_servico[-3:]) == base + 1
        assert int(s["pedidos"].id_servico[-3:]) == base + 2


@pytest.mark.parametrize("chave,volume,esperado", [
    ("caixa_lista", 0, Decimal("0.24")),      # consulta, faixa cheia
    ("caixa_lista", 299, Decimal("0.24")),
    ("caixa_lista", 300, Decimal("0.21")),    # faixa de volume: 300 consultas/mês
    ("sitfis_relatorio", 0, Decimal("0.32")),  # emissão, faixa cheia
    ("sitfis_relatorio", 499, Decimal("0.32")),
    ("sitfis_relatorio", 500, Decimal("0.29")),  # 500 emissões/mês
    ("caixa_indicador", 0, Decimal("0.00")),   # /Monitorar é gratuito, e isso é verificado
])
def test_preco_por_faixa(chave, volume, esperado):
    assert custo(SERVICOS[chave], volume) == esperado


def test_declarar_e_apoiar_nao_tem_preco_conhecido():
    """`None` vira NULL no banco: "não sei" não é "não custou".

    O produto do fornecedor não tem função de custo para estas duas rotas, então
    o painel dele subestima quando são usadas. Devolver 0 aqui repetiria o erro
    com cara de certeza.
    """
    assert custo(SERVICOS["mit_encerrar"]) is None     # /Declarar
    assert custo(SERVICOS["mit_situacao"]) is None     # /Apoiar
    assert custo(SERVICOS["procurador_termo"]) is None


def test_sem_contador_o_preco_sai_cheio(credenciais, serpro):
    """Errar para cima é o lado seguro de um limite de gasto."""
    serpro.responder("MSGCONTRIBUINTE61", dados={"conteudo": [{"indicadorUltimaPagina": "S"}]})
    registros = []
    c = SerproClient(credenciais, cache=CacheMemoria(), registrar=registros.append,
                     transport=httpx.MockTransport(serpro))
    c.chamar(SERVICOS["caixa_lista"], "11222333000181", {"statusLeitura": "0"})
    assert registros[-1].custo_estimado == Decimal("0.24")
    c.fechar()


def test_com_contador_entra_a_faixa_de_volume(credenciais, serpro):
    serpro.responder("MSGCONTRIBUINTE61", dados={"conteudo": [{"indicadorUltimaPagina": "S"}]})
    registros = []
    c = SerproClient(credenciais, cache=CacheMemoria(), registrar=registros.append,
                     chamadas_no_mes=lambda cobranca: 1200 if cobranca is Cobranca.CONSULTAR else 0,
                     transport=httpx.MockTransport(serpro))
    c.chamar(SERVICOS["caixa_lista"], "11222333000181", {"statusLeitura": "0"})
    assert registros[-1].custo_estimado == Decimal("0.21")
    c.fechar()


def test_falha_de_rede_nao_custa(credenciais):
    """Rede instável não pode virar gasto fantasma no painel."""
    def cai(request):
        # A autenticação passa; é a chamada ao gateway que não sai. Se a rede
        # cair já no /authenticate não há registro nenhum para avaliar.
        if request.url.path.endswith("/authenticate"):
            return httpx.Response(200, json={"access_token": "AT", "jwt_token": "JWT", "expires_in": 2008})
        raise httpx.ConnectError("sem rede")

    registros = []
    c = SerproClient(credenciais, cache=CacheMemoria(), registrar=registros.append,
                     transport=httpx.MockTransport(cai), tentativas_rede=0)
    with pytest.raises(httpx.TransportError):
        c.chamar(SERVICOS["caixa_lista"], "11222333000181", {"statusLeitura": "0"})
    assert registros and all(r.custo_estimado is None for r in registros)
    c.fechar()
