"""Parcelamentos (PARCSN, PARCMEI, PERTSN, PERTMEI, RELPSN, RELPMEI)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..catalogo import MODALIDADES, servicos_parcelamento
from ..client import SerproClient
from ..erros import IntegraErro
from ._util import buscar_chave, para_data, para_decimal, pdf_da_resposta

LIMITE_PARCELAS_API = 6  # acima disso o serviço não emite; orientar o portal da Receita


@dataclass
class Pedido:
    modalidade: str
    numero: str
    data_pedido: date | None
    situacao: str
    data_situacao: date | None
    bruto: dict = field(repr=False, default_factory=dict)

    @property
    def ativo(self) -> bool:
        return "em parcelamento" in self.situacao.lower()


@dataclass
class Parcela:
    modalidade: str
    parcela: str           # valor enviado em parcelaParaEmitir (AAAAMM)
    data_vencimento: date | None
    valor: Decimal
    bruto: dict = field(repr=False, default_factory=dict)


class Parcelamentos:
    def __init__(self, client: SerproClient):
        self.client = client

    @staticmethod
    def _servicos(modalidade: str):
        if modalidade not in MODALIDADES:
            raise ValueError(f"Modalidade desconhecida: {modalidade}")
        return servicos_parcelamento(modalidade)

    def pedidos(self, cnpj: str, modalidade: str) -> list[Pedido]:
        resp = self.client.chamar(self._servicos(modalidade)["pedidos"], cnpj)
        lista = buscar_chave(resp.dados, ["parcelamentos"]) or []
        return [
            Pedido(
                modalidade=modalidade,
                numero=str(p.get("numero")),
                data_pedido=para_data(p.get("dataDoPedido")),
                situacao=str(p.get("situacao") or ""),
                data_situacao=para_data(p.get("dataDaSituacao")),
                bruto=p,
            )
            for p in lista if isinstance(p, dict)
        ]

    def parcelas_disponiveis(self, cnpj: str, modalidade: str) -> list[Parcela]:
        resp = self.client.chamar(self._servicos(modalidade)["parcelas"], cnpj)
        lista = buscar_chave(resp.dados, ["listaParcelas"]) or []
        saida = []
        for p in lista:
            if not isinstance(p, dict):
                continue
            chave = p.get("parcelaParaEmitir") or p.get("parcela") or p.get("numeroParcela")
            saida.append(Parcela(
                modalidade=modalidade,
                parcela=str(chave),
                data_vencimento=para_data(p.get("dataVencimento") or p.get("vencimento")),
                valor=para_decimal(p.get("valor") or p.get("valorTotal")),
                bruto=p,
            ))
        return saida

    def emitir(self, cnpj: str, modalidade: str, parcela: str) -> bytes:
        resp = self.client.chamar(
            self._servicos(modalidade)["emitir"], cnpj, {"parcelaParaEmitir": int(parcela) if str(parcela).isdigit() else parcela}
        )
        return pdf_da_resposta(resp.dados, ("docArrecadacaoPdfB64", "pdf"))

    def emitir_todas(self, cnpj: str, modalidade: str) -> list[tuple[Parcela, bytes]]:
        parcelas = self.parcelas_disponiveis(cnpj, modalidade)
        if len(parcelas) > LIMITE_PARCELAS_API:
            raise IntegraErro(
                f"{len(parcelas)} parcelas disponíveis; a API emite no máximo {LIMITE_PARCELAS_API}. "
                "Emita pelo portal da Receita."
            )
        return [(p, self.emitir(cnpj, modalidade, p.parcela)) for p in parcelas]
