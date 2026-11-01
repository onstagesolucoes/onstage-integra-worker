"""Preço de uma chamada ao SERPRO.

Os números saem da decompilação do produto do fornecedor (01/11/2026,
`services/api_usage_service.py`): `_get_consulta_cost` devolve `0.24` ou `0.21`,
`_get_emitir_cost` devolve `0.32` ou `0.29`, e os únicos inteiros plausíveis no
módulo são `300` e `500` — as faixas de volume mensal.

Por que isto vive no pacote e não no portal: é o worker que grava
`integra_chamadas.custo_estimado`, e sem esse valor a view do teto diário soma
zero — ou seja, **o teto nunca dispara**. Um teto que não dispara é pior que não
ter teto, porque passa a sensação de proteção.

A faixa depende de quantas chamadas da mesma família já houve **no mês**, que é
uma conta do banco, não do pacote. Então o volume entra como parâmetro: quem
chama informa; quem não informa paga o preço cheio. O erro assim é sempre para
cima, que é o lado seguro de um limite de gasto.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .catalogo import Cobranca, Servico


@dataclass(frozen=True)
class Faixa:
    cheio: Decimal
    volume: Decimal
    a_partir_de: int          # chamadas no mês a partir das quais vale `volume`

    def preco(self, chamadas_no_mes: int) -> Decimal:
        return self.volume if chamadas_no_mes >= self.a_partir_de else self.cheio


FAIXAS: dict[Cobranca, Faixa] = {
    Cobranca.CONSULTAR: Faixa(Decimal("0.24"), Decimal("0.21"), 300),
    Cobranca.EMITIR: Faixa(Decimal("0.32"), Decimal("0.29"), 500),
}

# Gratuito de fato, e isso é verificado: `/Monitorar` é a única chamada que o
# fornecedor não registra na própria tabela de uso (`register_usage` aparece no
# caminho da lista, nunca no do indicador).
GRATUITOS: frozenset[Cobranca] = frozenset({Cobranca.MONITORAR})

# Sem preço conhecido — o que **não** é o mesmo que grátis. O produto do
# fornecedor simplesmente não tem função de custo para `/Declarar` e `/Apoiar`,
# então o painel dele subestima quando essas rotas são usadas. Aqui o custo sai
# como `None` para virar NULL no banco: em SQL, NULL é "não sei" e 0 é "não
# custou", e confundir os dois é o começo de uma fatura surpresa.
SEM_PRECO: frozenset[Cobranca] = frozenset({Cobranca.DECLARAR, Cobranca.APOIAR})


def custo(servico: Servico, chamadas_no_mes: int = 0) -> Decimal | None:
    """Custo estimado da chamada. `None` quando o preço não é conhecido."""
    cobranca = servico.cobranca
    if cobranca in GRATUITOS:
        return Decimal("0.00")
    faixa = FAIXAS.get(cobranca)
    if faixa is None:
        return None
    return faixa.preco(chamadas_no_mes)
