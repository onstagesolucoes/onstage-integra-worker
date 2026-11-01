"""Tipos usados pela fila de jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..client import SerproClient


class Storage(Protocol):
    """Implementado pelo portal (Supabase Storage, S3, disco...)."""

    def salvar(self, caminho: str, conteudo: bytes, content_type: str = "application/pdf") -> str: ...


@dataclass
class Job:
    """Uma linha de `integra_jobs`.

    O grão é o **CNPJ**, e não um `empresa_id`: o portal não tem — nem quer —
    uma terceira lista de empresas ao lado de `clientes` e
    `certificados_digitais`. Quem pode ser consultado sai de
    `v_integra_habilitados`, derivada dessas duas.

    `cnpj` segue opcional porque existe job que não é de um cliente: a varredura
    que enfileira os elegíveis, por exemplo.
    """
    id: str
    tipo: str
    params: dict
    estado: dict
    tentativas: int
    max_tentativas: int
    cnpj: str | None = None


@dataclass
class Resultado:
    """O que o handler devolve à fila.

    status:
      ok        -> job concluído
      aguardar  -> reagendar em `aguardar_s` segundos guardando `estado` (ex.: protocolo SITFIS)
    Erros são exceções: a fila decide se tenta de novo.
    """
    status: str = "ok"
    resultado: dict = field(default_factory=dict)
    estado: dict | None = None
    aguardar_s: int = 0

    @classmethod
    def ok(cls, **resultado: Any) -> "Resultado":
        return cls("ok", resultado)

    @classmethod
    def aguardar(cls, segundos: int, estado: dict, **resultado: Any) -> "Resultado":
        return cls("aguardar", resultado, estado, max(1, segundos))


@dataclass
class Contexto:
    client: SerproClient
    conn: Any             # conexão psycopg dentro da transação do job
    storage: Storage
    job: Job


Handler = Callable[[Contexto], Resultado]
