"""Tokens SERPRO (access_token + jwt_token) com cache por escritório.

O executável original pedia um token novo a cada operação. Aqui o par é
reaproveitado até 60 s antes do vencimento informado em `expires_in`.
Para vários processos (vários workers), implemente TokenCache com Redis ou
uma tabela, mantendo a mesma interface.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Protocol

URL_AUTENTICACAO = "https://autenticacao.sapi.serpro.gov.br/authenticate"
VALIDADE_PADRAO_S = 1800   # usada se o SERPRO não informar expires_in
MARGEM_S = 60


@dataclass(frozen=True)
class Tokens:
    access_token: str
    jwt_token: str
    expira_em: float  # epoch

    def valido(self, agora: float | None = None) -> bool:
        return (agora or time.time()) < self.expira_em - MARGEM_S


class TokenCache(Protocol):
    def obter(self, chave: str) -> Tokens | None: ...
    def salvar(self, chave: str, tokens: Tokens) -> None: ...
    def invalidar(self, chave: str) -> None: ...


class CacheMemoria:
    """Cache em memória, seguro entre threads de um mesmo processo."""

    def __init__(self):
        self._dados: dict[str, Tokens] = {}
        self._lock = threading.Lock()

    def obter(self, chave: str) -> Tokens | None:
        with self._lock:
            t = self._dados.get(chave)
            return t if t and t.valido() else None

    def salvar(self, chave: str, tokens: Tokens) -> None:
        with self._lock:
            self._dados[chave] = tokens

    def invalidar(self, chave: str) -> None:
        with self._lock:
            self._dados.pop(chave, None)


def cabecalhos_autenticacao(consumer_key: str, consumer_secret: str) -> dict[str, str]:
    basico = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    return {
        "Authorization": f"Basic {basico}",
        "role-type": "TERCEIROS",
        "content-type": "application/x-www-form-urlencoded",
    }


def tokens_da_resposta(corpo: dict, agora: float | None = None) -> Tokens:
    agora = agora or time.time()
    try:
        expira = float(corpo.get("expires_in") or VALIDADE_PADRAO_S)
    except (TypeError, ValueError):
        expira = VALIDADE_PADRAO_S
    return Tokens(
        access_token=corpo["access_token"],
        jwt_token=corpo["jwt_token"],
        expira_em=agora + expira,
    )
