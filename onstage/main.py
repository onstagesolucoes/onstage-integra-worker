"""Entrypoint do container. Um processo, um laço, nenhuma porta aberta.

**Não existe servidor HTTP aqui, e isso é a correção central em relação ao
`IntegraContadorDesktop.exe`.** O executável subia Flask em `0.0.0.0:5847` sem
autenticação, com CORS liberado e uma rota que devolvia a senha do certificado
em texto puro; o `ParaUsaremRede.zip` prova que rodou assim na rede do
escritório. Aqui o worker só fala para fora com o SERPRO e com o Supabase, e a
saúde do processo é publicada em tabela (`integra_worker_pulso`) — o portal lê
dali, em vez de alguém abrir uma porta de health check que também responde a
quem não devia.

    python -m onstage.main
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time

import psycopg

from integra_serpro.fila.worker import Worker

from .armazenamento import StorageSupabase
from .segredos import ProviderSupabase

log = logging.getLogger("onstage.main")

INTERVALO_PULSO_S = 30


def _configurar_log() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # httpx loga a URL de cada requisição em INFO; com mTLS e envelope isso não
    # vaza segredo, mas enche o log do container sem agregar nada.
    logging.getLogger("httpx").setLevel("WARNING")


class Pulso(threading.Thread):
    """Heartbeat em tabela. Thread própria para bater mesmo com job longo.

    Se o pulso só batesse entre jobs, um SITFIS esperando o protocolo pareceria
    worker morto — e alguém reiniciaria o container no meio de uma espera já
    paga.
    """

    daemon = True

    def __init__(self, dsn: str, instancia: str):
        super().__init__(name="pulso")
        self.dsn, self.instancia, self.parar = dsn, instancia, threading.Event()

    def run(self) -> None:
        conn = psycopg.connect(self.dsn, autocommit=True)
        while not self.parar.is_set():
            try:
                conn.execute(
                    """insert into integra_worker_pulso (instancia, visto_em)
                       values (%s, now())
                       on conflict (instancia) do update set visto_em = now()""",
                    (self.instancia,),
                )
            except Exception:
                log.exception("Falha ao bater o pulso")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = psycopg.connect(self.dsn, autocommit=True)
            self.parar.wait(INTERVALO_PULSO_S)


def main() -> int:
    _configurar_log()
    dsn = os.environ["DATABASE_URL"]
    instancia = os.environ.get("RAILWAY_REPLICA_ID") or socket.gethostname()

    worker = Worker(
        dsn,
        provider=ProviderSupabase(dsn),
        storage=StorageSupabase(),
        intervalo_ocioso_s=float(os.environ.get("INTERVALO_OCIOSO_S", "5")),
    )

    pulso = Pulso(dsn, instancia)
    pulso.start()

    # SIGTERM é como o Railway pede para sair. Sem tratar, o sinal mata o
    # processo no meio da transação do job: o `rollback` devolve o job para a
    # fila, mas a chamada ao SERPRO **já foi paga**. Pedir parada educada deixa
    # o job em curso terminar.
    def encerrar(*_):
        log.info("SIGTERM recebido; encerrando após o job atual")
        pulso.parar.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, encerrar)

    atingido, gasto, teto = worker.teto_atingido()
    log.info("Gasto de hoje: R$ %s de R$ %s%s", gasto, teto, " — TETO ATINGIDO" if atingido else "")

    try:
        worker.rodar()
    finally:
        pulso.parar.set()
        worker.fechar()
        log.info("Worker encerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
