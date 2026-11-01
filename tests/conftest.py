import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import NameOID

from integra_serpro import Credenciais, Procurador
from integra_serpro.auth import CacheMemoria

PDF_FAKE = b"%PDF-1.4 teste"
PDF_B64 = base64.b64encode(PDF_FAKE).decode()


def gerar_pfx(nome="TESTE LTDA:12345678000199", senha="1234", dias=365, inicio_dias=-1) -> bytes:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    agora = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(sujeito).issuer_name(sujeito).public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora + timedelta(days=inicio_dias))
        .not_valid_after(agora + timedelta(days=dias))
        .sign(chave, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(b"teste", chave, cert, None, BestAvailableEncryption(senha.encode()))


@pytest.fixture(scope="session")
def pfx():
    return gerar_pfx()


@pytest.fixture
def credenciais(pfx):
    return Credenciais(
        contratante_cnpj="12.345.678/0001-99", nome_escritorio="ON Soluções",
        pfx=pfx, senha_pfx="1234", consumer_key="ck", consumer_secret="cs",
    )


@pytest.fixture
def credenciais_procurador(pfx):
    return Credenciais(
        contratante_cnpj="12345678000199", nome_escritorio="ON Soluções",
        pfx=pfx, senha_pfx="1234", consumer_key="ck", consumer_secret="cs",
        procurador=Procurador(cpf="123.456.789-09", nome="FULANO", pfx=gerar_pfx("FULANO:12345678909"), senha_pfx="1234"),
    )


class SerproFake:
    """Simula /authenticate e o gateway. `respostas` mapeia idServico -> lista de (status, corpo, headers)."""

    def __init__(self):
        self.auth_chamadas = 0
        self.requisicoes: list[dict] = []
        self.respostas: dict[str, list] = {}

    def responder(self, id_servico, status=200, dados=None, headers=None, mensagens=None):
        corpo = None
        if status != 204:
            corpo = {"status": status, "mensagens": mensagens or [], "dados": json.dumps(dados) if dados is not None else ""}
        self.respostas.setdefault(id_servico, []).append((status, corpo, headers or {}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authenticate"):
            self.auth_chamadas += 1
            return httpx.Response(200, json={"access_token": f"AT{self.auth_chamadas}", "jwt_token": "JWT", "expires_in": 2008})
        corpo = json.loads(request.content)
        self.requisicoes.append({"url": str(request.url), "headers": dict(request.headers), "corpo": corpo})
        fila = self.respostas.get(corpo["pedidoDados"]["idServico"]) or []
        if not fila:
            return httpx.Response(500, json={"mensagens": [{"codigo": "X", "texto": "sem resposta configurada"}]})
        status, resp, headers = fila.pop(0) if len(fila) > 1 else fila[0]
        return httpx.Response(status, json=resp, headers=headers) if resp is not None else httpx.Response(status, headers=headers)


@pytest.fixture
def serpro():
    return SerproFake()


@pytest.fixture
def client_fake(credenciais, serpro):
    from integra_serpro import SerproClient
    registros = []
    c = SerproClient(credenciais, cache=CacheMemoria(), registrar=registros.append, transport=httpx.MockTransport(serpro))
    c.registros = registros
    yield c
    c.fechar()


# ───────────────────────────── Postgres de verdade ─────────────────────────────
# Os testes do worker são SQL: `for update skip locked`, upsert em índice
# parcial, CHECK de situação, view com `eh_interno()`. Nada disso tem como ser
# simulado — ou roda em Postgres, ou não é testado.
#
# Duas formas de chegar lá, nesta ordem:
#   1. INTEGRA_TEST_DSN aponta para um Postgres que você já tem;
#   2. o pacote `pgserver` (PyPI), que embute um PostgreSQL 17 e sobe sem
#      nenhum privilégio administrativo nem container — foi o que destravou
#      estes testes, que viviam marcados como `skip`.
# Sem nenhuma das duas, os testes do worker são pulados com motivo explícito.

MIGRACOES = ("20261101001600_integra_base.sql", "20261101001700_integra_caixa_postal.sql",
             "20261101001800_integra_sitfis.sql", "20261101001900_integra_parcelamentos.sql")


def _dir_migracoes() -> Path | None:
    """Onde estão as migrações do portal. `PORTAL_MIGRATIONS` tem a palavra final."""
    candidatos = [Path(p) for p in (os.environ.get("PORTAL_MIGRATIONS"),) if p]
    candidatos.append(Path(__file__).resolve().parents[2] / "portal-360" / "supabase" / "migrations")
    for d in candidatos:
        if (d / MIGRACOES[0]).exists():
            return d
    return None


@pytest.fixture(scope="session")
def dsn() -> str:
    externo = os.environ.get("INTEGRA_TEST_DSN")
    if externo:
        return externo
    try:
        import pgserver
    except ImportError:
        pytest.skip("defina INTEGRA_TEST_DSN ou instale pgserver (pip install pgserver)")
    destino = Path(os.environ.get("INTEGRA_PGDATA", "/tmp/pgdata_integra"))
    destino.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(destino).get_uri()


@pytest.fixture(scope="session")
def banco(dsn) -> str:
    """Banco limpo com o esquema do portal aplicado: stand-ins + migrações reais.

    As migrações **não** são reescritas aqui. São lidas do arquivo que vai para o
    Supabase, na ordem do nome, com `drop schema public cascade` antes. Assim o
    teste responde a duas perguntas de uma vez: o worker grava onde deve, e as
    migrações aplicam limpo num banco vazio.
    """
    import psycopg

    dir_mig = _dir_migracoes()
    if not dir_mig:
        pytest.skip("migrações do portal não encontradas (defina PORTAL_MIGRATIONS)")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade; create schema public")
        conn.execute("drop schema if exists auth cascade")
        conn.execute((Path(__file__).parent / "esquema_portal.sql").read_text())
        for nome in MIGRACOES:
            conn.execute((dir_mig / nome).read_text())
    return dsn
