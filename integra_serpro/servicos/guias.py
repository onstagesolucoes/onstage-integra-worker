"""Emissão de guias: DAS Simples (PGDASD), DAS MEI (PGMEI) e DARF DCTFWeb."""
from __future__ import annotations

from ..catalogo import SERVICOS
from ..client import SerproClient
from ._util import pdf_da_resposta, validar_aaaamm, validar_aaaammdd

CATEGORIAS_DCTFWEB = ("GERAL_MENSAL", "GERAL_13o_SALARIO")


class Guias:
    def __init__(self, client: SerproClient):
        self.client = client

    def das_simples(self, cnpj: str, periodo_apuracao: str, data_consolidacao: str | None = None) -> bytes:
        dados = {"periodoApuracao": validar_aaaamm(periodo_apuracao, "Período de apuração")}
        if data_consolidacao:
            dados["dataConsolidacao"] = validar_aaaammdd(data_consolidacao, "Data de consolidação")
        resp = self.client.chamar(SERVICOS["das_simples"], cnpj, dados)
        return pdf_da_resposta(resp.dados)

    def das_mei(self, cnpj: str, periodo_apuracao: str) -> bytes:
        dados = {"periodoApuracao": validar_aaaamm(periodo_apuracao, "Período de apuração")}
        resp = self.client.chamar(SERVICOS["das_mei"], cnpj, dados)
        return pdf_da_resposta(resp.dados)

    def darf_dctfweb(self, cnpj: str, categoria: str, competencia: str | None = None, ano_pa: str | None = None) -> bytes:
        if categoria not in CATEGORIAS_DCTFWEB:
            raise ValueError("Categoria inválida: use GERAL_MENSAL ou GERAL_13o_SALARIO")
        if categoria == "GERAL_MENSAL":
            comp = validar_aaaamm(competencia or "", "Competência")
            dados = {"categoria": categoria, "anoPA": comp[:4], "mesPA": comp[4:]}
        else:
            ano = (ano_pa or "").strip()
            if len(ano) != 4 or not ano.isdigit():
                raise ValueError("Ano PA inválido para 13º salário: use AAAA")
            dados = {"categoria": categoria, "anoPA": ano}
        resp = self.client.chamar(SERVICOS["darf_dctfweb"], cnpj, dados)
        return pdf_da_resposta(resp.dados)
