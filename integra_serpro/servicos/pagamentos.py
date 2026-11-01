"""Consulta de pagamentos (PAGTOWEB / PAGAMENTOS71) com paginação."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterator

from ..catalogo import SERVICOS
from ..client import SerproClient
from ._util import para_data, para_decimal

TAMANHO_PAGINA = 100


def _g(obj: dict, *caminho: str, padrao: Any = None) -> Any:
    atual: Any = obj
    for chave in caminho:
        if not isinstance(atual, dict):
            return padrao
        atual = atual.get(chave)
    return padrao if atual in (None, "") else atual


@dataclass
class Desmembramento:
    sequencial: str
    receita_codigo: str
    receita_descricao: str | None
    periodo_apuracao: date | None
    data_vencimento: date | None
    valor_principal: Decimal
    valor_multa: Decimal
    valor_juros: Decimal
    valor_total: Decimal


@dataclass
class Pagamento:
    numero_documento: str
    tipo_codigo: str | None
    tipo_descricao: str | None
    periodo_apuracao: date | None
    data_arrecadacao: date | None
    data_vencimento: date | None
    receita_codigo: str
    receita_descricao: str | None
    referencia: str | None
    valor_principal: Decimal
    valor_multa: Decimal
    valor_juros: Decimal
    valor_total: Decimal
    desmembramentos: list[Desmembramento] = field(default_factory=list)
    bruto: dict = field(repr=False, default_factory=dict)


def normalizar(item: dict) -> Pagamento:
    desm = []
    for i, d in enumerate(item.get("desmembramentos") or [], start=1):
        desm.append(Desmembramento(
            sequencial=str(d.get("sequencial") or i),
            receita_codigo=str(_g(d, "receitaPrincipal", "codigo", padrao="")),
            receita_descricao=_g(d, "receitaPrincipal", "descricao"),
            periodo_apuracao=para_data(d.get("periodoApuracao")),
            data_vencimento=para_data(d.get("dataVencimento")),
            valor_principal=para_decimal(d.get("valorPrincipal")),
            valor_multa=para_decimal(d.get("valorMulta")),
            valor_juros=para_decimal(d.get("valorJuros")),
            valor_total=para_decimal(d.get("valorTotal")),
        ))
    return Pagamento(
        numero_documento=str(item.get("numeroDocumento")),
        tipo_codigo=_g(item, "tipo", "codigo"),
        tipo_descricao=_g(item, "tipo", "descricao"),
        periodo_apuracao=para_data(item.get("periodoApuracao")),
        data_arrecadacao=para_data(item.get("dataArrecadacao")),
        data_vencimento=para_data(item.get("dataVencimento")),
        receita_codigo=str(_g(item, "receitaPrincipal", "codigo", padrao=""))[:4],
        receita_descricao=_g(item, "receitaPrincipal", "descricao"),
        referencia=item.get("referencia"),
        valor_principal=para_decimal(item.get("valorPrincipal")),
        valor_multa=para_decimal(item.get("valorMulta")),
        valor_juros=para_decimal(item.get("valorJuros")),
        valor_total=para_decimal(item.get("valorTotal")),
        desmembramentos=desm,
        bruto=item,
    )


def _itens(dados: Any) -> list:
    if isinstance(dados, list):
        return dados
    if isinstance(dados, dict):
        for chave in ("items", "lista", "pagamentos", "dados"):
            if isinstance(dados.get(chave), list):
                return dados[chave]
    return []


class Pagamentos:
    def __init__(self, client: SerproClient):
        self.client = client

    def consultar(self, cnpj: str, data_inicial: date, data_final: date, tamanho: int = TAMANHO_PAGINA) -> Iterator[Pagamento]:
        inicio = 0
        while True:
            dados = {
                "intervaloDataArrecadacao": {
                    "dataInicial": data_inicial.isoformat(),
                    "dataFinal": data_final.isoformat(),
                },
                "primeiroDaPagina": inicio,
                "tamanhoDaPagina": tamanho,
            }
            resp = self.client.chamar(SERVICOS["pagamentos"], cnpj, dados)
            itens = _itens(resp.dados)
            for item in itens:
                if isinstance(item, dict) and item.get("numeroDocumento"):
                    yield normalizar(item)
            if len(itens) < tamanho:
                return
            inicio += tamanho
