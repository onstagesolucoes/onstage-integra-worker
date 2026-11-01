from __future__ import annotations

import base64
import binascii
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from ..erros import IntegraErro


def primeiro_item(dados: Any) -> dict:
    """Muitos serviços devolvem `dados` como lista com um único objeto."""
    if isinstance(dados, list):
        return dados[0] if dados and isinstance(dados[0], dict) else {}
    return dados if isinstance(dados, dict) else {}


def buscar_chave(obj: Any, nomes: Iterable[str]) -> Any:
    """Procura a primeira chave presente (sem diferenciar maiúsculas) em dict ou lista de dicts."""
    alvos = {n.lower() for n in nomes}
    pilha = [obj]
    while pilha:
        atual = pilha.pop(0)
        if isinstance(atual, dict):
            for k, v in atual.items():
                if k.lower() in alvos and v not in (None, ""):
                    return v
            pilha.extend(v for v in atual.values() if isinstance(v, (dict, list)))
        elif isinstance(atual, list):
            pilha.extend(atual)
    return None


def pdf_da_resposta(dados: Any, nomes=("pdf", "PDFByteArrayBase64", "docArrecadacaoPdfB64")) -> bytes:
    b64 = buscar_chave(dados, nomes)
    if not b64:
        raise IntegraErro("A resposta do SERPRO não trouxe o PDF")
    try:
        pdf = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise IntegraErro("PDF devolvido pelo SERPRO não está em base64 válido") from exc
    if not pdf.startswith(b"%PDF"):
        raise IntegraErro("Conteúdo devolvido não é um PDF")
    return pdf


def validar_aaaamm(valor: str, campo: str = "período") -> str:
    v = (valor or "").strip()
    if len(v) != 6 or not v.isdigit() or not 1 <= int(v[4:]) <= 12:
        raise ValueError(f"{campo} inválido: use AAAAMM (ex.: 202409)")
    return v


def validar_aaaammdd(valor: str, campo: str = "data") -> str:
    v = (valor or "").strip()
    try:
        datetime.strptime(v, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{campo} inválida: use AAAAMMDD (ex.: 20240930)") from exc
    return v


def para_decimal(valor: Any) -> Decimal:
    if valor in (None, ""):
        return Decimal("0.00")
    if isinstance(valor, (int, float, Decimal)):
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    texto = str(valor).strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def para_data(valor: Any) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    texto = str(valor).strip()
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y%m%d", "%d/%m/%Y", "%m/%Y", "%Y%m"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            continue
    return None
