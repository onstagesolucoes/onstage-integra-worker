"""Mascaramento de segredos e truncamento de conteúdo grande antes de logar."""
from __future__ import annotations

import json
from typing import Any

CHAVES_SENSIVEIS = {
    "authorization", "jwt_token", "access_token", "token", "senha", "password",
    "consumer_key", "consumer_secret", "autenticar_procurador_token", "xml",
}
CHAVES_GRANDES = {
    "pdf", "pdfbytearraybase64", "docarrecadacaopdfb64", "arquivo", "base64",
}
LIMITE_TEXTO = 1200


def _mascarar_valor(valor: Any) -> str:
    texto = str(valor or "")
    return "***" + texto[-4:] if len(texto) > 8 else "***"


def mascarar(obj: Any, _chave: str | None = None) -> Any:
    chave = (_chave or "").lower()
    if chave in CHAVES_SENSIVEIS:
        return _mascarar_valor(obj)
    if chave in CHAVES_GRANDES and isinstance(obj, str):
        return f"<{len(obj)} caracteres omitidos>"
    if isinstance(obj, dict):
        return {k: mascarar(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mascarar(v) for v in obj]
    if isinstance(obj, str):
        if chave == "dados" and obj.strip().startswith(("{", "[")):
            try:
                return mascarar(json.loads(obj))
            except ValueError:
                pass
        return obj if len(obj) <= LIMITE_TEXTO else obj[:LIMITE_TEXTO] + f"... <+{len(obj) - LIMITE_TEXTO}>"
    return obj
