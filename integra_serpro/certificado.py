"""Certificado A1 (.pfx): validade, extração de chave e contexto mTLS."""
from __future__ import annotations

import os
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

from .erros import CertificadoVencido, ConfiguracaoIncompleta


@dataclass(frozen=True)
class InfoCertificado:
    titular: str
    valido_de: datetime
    valido_ate: datetime

    @property
    def dias_restantes(self) -> int:
        return (self.valido_ate - datetime.now(timezone.utc)).days


def carregar_pfx(pfx: bytes, senha: str):
    """Retorna (chave_privada, certificado, cadeia)."""
    try:
        chave, cert, cadeia = pkcs12.load_key_and_certificates(
            pfx, senha.encode() if senha else None
        )
    except ValueError as exc:  # senha errada ou arquivo corrompido
        raise ConfiguracaoIncompleta("Não foi possível abrir o certificado: senha incorreta ou arquivo inválido") from exc
    if chave is None or cert is None:
        raise ConfiguracaoIncompleta("O .pfx não contém chave privada e certificado")
    return chave, cert, cadeia or []


def info_certificado(pfx: bytes, senha: str) -> InfoCertificado:
    _, cert, _ = carregar_pfx(pfx, senha)
    cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    return InfoCertificado(
        titular=cn[0].value if cn else cert.subject.rfc4514_string(),
        valido_de=cert.not_valid_before_utc,
        valido_ate=cert.not_valid_after_utc,
    )


def garantir_validade(pfx: bytes, senha: str, dono: str = "contratante") -> InfoCertificado:
    info = info_certificado(pfx, senha)
    agora = datetime.now(timezone.utc)
    if not (info.valido_de <= agora <= info.valido_ate):
        raise CertificadoVencido(
            f"Certificado do {dono} fora da validade ({info.valido_ate:%d/%m/%Y})"
        )
    return info


def pem_chave_e_certificado(pfx: bytes, senha: str) -> tuple[bytes, bytes]:
    """PEM sem senha da chave e do certificado (com cadeia). Use só em memória."""
    chave, cert, cadeia = carregar_pfx(pfx, senha)
    chave_pem = chave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    cert_pem = cert.public_bytes(Encoding.PEM) + b"".join(c.public_bytes(Encoding.PEM) for c in cadeia)
    return chave_pem, cert_pem


def contexto_ssl(pfx: bytes, senha: str) -> ssl.SSLContext:
    """SSLContext com o certificado de cliente carregado.

    O ssl da biblioteca padrão só aceita caminhos de arquivo. A chave vai para
    um arquivo temporário cifrado com uma senha aleatória, com permissão 0600,
    e é apagado logo após a carga; depois disso ela vive só na memória.
    """
    chave, cert, cadeia = carregar_pfx(pfx, senha)
    senha_temp = os.urandom(24).hex().encode()
    chave_pem = chave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(senha_temp))
    cert_pem = cert.public_bytes(Encoding.PEM) + b"".join(c.public_bytes(Encoding.PEM) for c in cadeia)

    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    fd, caminho = tempfile.mkstemp(prefix="serpro_", suffix=".pem")
    try:
        os.chmod(caminho, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(cert_pem + chave_pem)
        ctx.load_cert_chain(caminho, password=senha_temp)
    finally:
        try:
            os.remove(caminho)
        except FileNotFoundError:
            pass
    return ctx
