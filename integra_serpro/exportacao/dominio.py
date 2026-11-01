"""Exportação de pagamentos para importação de lançamentos no Domínio.

Layout (um TXT por empresa e mês de arrecadação):
    |0000|<CNPJ>|
    |6000|X||||
    |6100|<dd/mm/aaaa>|<débito>|<crédito>|<valor>|<cód. histórico>|<histórico>|<usuário>|||

Cada pagamento gera até três pares 6000/6100: principal, juros e multa.
Juros e multa usam a conta de crédito do principal.
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from ..erros import IntegraErro


@dataclass(frozen=True)
class DePara:
    receita_codigo: str
    conta_debito_principal: str
    conta_credito_principal: str
    conta_debito_juros: str | None = None
    conta_debito_multa: str | None = None
    codigo_historico: str = ""
    historico_principal: str = ""
    historico_juros: str = ""
    historico_multa: str = ""


@dataclass(frozen=True)
class PagamentoExportavel:
    id: str
    numero_documento: str
    receita_codigo: str
    receita_descricao: str | None
    data_arrecadacao: date | None
    valor_principal: Decimal
    valor_juros: Decimal
    valor_multa: Decimal


def _valor(v: Decimal) -> str:
    return f"{v:.2f}".replace(".", ",")


def _limpar(texto: str) -> str:
    """Tira o separador | do histórico para não quebrar o layout."""
    return (texto or "").replace("|", "/").strip()


def linha_6100(data: date, debito: str, credito: str, valor: Decimal, cod_hist: str, historico: str, usuario: str) -> str:
    return f"|6100|{data:%d/%m/%Y}|{debito}|{credito}|{_valor(valor)}|{cod_hist}|{_limpar(historico)}|{usuario}|||"


def gerar_txt(cnpj: str, pagamentos: Iterable[PagamentoExportavel], depara: dict[str, DePara], usuario: str = "GERENTE") -> tuple[str, list[str]]:
    """Retorna (conteúdo do TXT, ids exportados). Levanta IntegraErro se faltar de-para."""
    cnpj_digitos = re.sub(r"\D", "", cnpj)
    linhas = [f"|0000|{cnpj_digitos}|"]
    ids: list[str] = []
    for p in pagamentos:
        dp = depara.get(p.receita_codigo)
        if not dp:
            raise IntegraErro(f"De-para não cadastrado para a receita {p.receita_codigo} (documento {p.numero_documento})")
        if not p.data_arrecadacao:
            raise IntegraErro(f"Pagamento {p.numero_documento} sem data de arrecadação")
        desc = p.receita_descricao or p.receita_codigo
        partes = [
            (p.valor_principal, dp.conta_debito_principal, dp.historico_principal or f"Pagamento {desc}", "principal"),
            (p.valor_juros, dp.conta_debito_juros, dp.historico_juros or f"Juros {desc}", "juros"),
            (p.valor_multa, dp.conta_debito_multa, dp.historico_multa or f"Multa {desc}", "multa"),
        ]
        for valor, debito, historico, nome in partes:
            if not valor or valor <= 0:
                continue
            if not debito:
                raise IntegraErro(f"Conta de débito de {nome} não cadastrada para a receita {p.receita_codigo}")
            linhas.append("|6000|X||||")
            linhas.append(linha_6100(p.data_arrecadacao, debito, dp.conta_credito_principal, valor, dp.codigo_historico, historico, usuario))
        ids.append(p.id)
    return "\r\n".join(linhas) + "\r\n", ids


def _nome_arquivo(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    t = re.sub(r'[\\/:*?"<>|]+', "_", t)
    return re.sub(r"\s+", "_", t).strip("_") or "EMPRESA"


def gerar_zip(
    empresas: Iterable[tuple[str, str, list[PagamentoExportavel], dict[str, DePara]]],
    usuario: str = "GERENTE",
    codificacao: str = "utf-8",
) -> tuple[bytes, list[str]]:
    """empresas: (nome, cnpj, pagamentos, depara). Um TXT por empresa e mês (MMAAAA)."""
    buffer = io.BytesIO()
    exportados: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for nome, cnpj, pagamentos, depara in empresas:
            por_mes: dict[str, list[PagamentoExportavel]] = defaultdict(list)
            for p in pagamentos:
                if not p.data_arrecadacao:
                    raise IntegraErro(f"Pagamento {p.numero_documento} sem data de arrecadação")
                por_mes[f"{p.data_arrecadacao:%m%Y}"].append(p)
            for mesano, lista in sorted(por_mes.items()):
                txt, ids = gerar_txt(cnpj, lista, depara, usuario)
                # O executável original gravava em UTF-8; use "cp1252" se o Domínio exibir acentos errados.
                zf.writestr(f"{_nome_arquivo(nome)}/{mesano}.txt", txt.encode(codificacao, errors="replace"))
                exportados.extend(ids)
    return buffer.getvalue(), exportados
