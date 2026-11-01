"""Envelope JSON comum a todas as chamadas do Integra Contador."""
from __future__ import annotations

import json
import re
from typing import Any

_NAO_DIGITO = re.compile(r"\D")


def so_digitos(valor: str | None) -> str:
    return _NAO_DIGITO.sub("", valor or "")


def tipo_documento(numero: str) -> int:
    """1 = CPF (11 dígitos), 2 = CNPJ (14 dígitos)."""
    n = so_digitos(numero)
    if len(n) == 11:
        return 1
    if len(n) == 14:
        return 2
    raise ValueError(f"CPF/CNPJ inválido: {numero!r}")


def identificacao(numero: str) -> dict[str, Any]:
    n = so_digitos(numero)
    return {"numero": n, "tipo": tipo_documento(n)}


def serializar_dados(dados: dict | str | None) -> str:
    """O campo `dados` do pedido é uma STRING com JSON dentro, não um objeto."""
    if dados is None or dados == "":
        return ""
    if isinstance(dados, str):
        return dados
    return json.dumps(dados, ensure_ascii=False, separators=(",", ":"))


def montar_envelope(
    *,
    contratante: str,
    autor_pedido: str,
    contribuinte: str,
    id_sistema: str,
    id_servico: str,
    versao: str,
    dados: dict | str | None = None,
) -> dict[str, Any]:
    return {
        "contratante": identificacao(contratante),
        "autorPedidoDados": identificacao(autor_pedido),
        "contribuinte": identificacao(contribuinte),
        "pedidoDados": {
            "idSistema": id_sistema,
            "idServico": id_servico,
            "versaoSistema": versao,
            "dados": serializar_dados(dados),
        },
    }


def decodificar_dados(valor: Any) -> Any:
    """A resposta também traz `dados` como string JSON; devolve o objeto."""
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return None
        try:
            return json.loads(texto)
        except ValueError:
            return valor
    return valor
