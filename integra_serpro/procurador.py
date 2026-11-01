"""Autenticação de procurador PF (AUTENTICAPROCURADOR / ENVIOXMLASSINADO81).

Necessário só quando a procuração e-CAC dos clientes está em CPF de um
procurador e não no CNPJ do escritório contratante.

Fluxo: gera o termoDeAutorizacao, assina com o .pfx do procurador
(XMLDSig enveloped, RSA-SHA256), envia em Apoiar e guarda o
`autenticar_procurador_token`, que vai no header das chamadas seguintes.
"""
from __future__ import annotations

import base64
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from lxml import etree
from signxml import XMLSigner, methods

from .catalogo import SERVICOS
from .certificado import garantir_validade, pem_chave_e_certificado
from .envelope import so_digitos, tipo_documento
from .erros import IntegraErro

if TYPE_CHECKING:
    from .client import SerproClient

FUSO_BR = ZoneInfo("America/Sao_Paulo")
MARGEM_S = 600  # renova 10 min antes de expirar

# Textos padrão do termo de autorização publicados pelo SERPRO.
TEXTO_TERMO = (
    "Autorizo a empresa CONTRATANTE, identificada neste termo de autorização como DESTINATÁRIO, "
    "a executar as requisições dos serviços web disponibilizados pela API INTEGRA CONTADOR, onde terei "
    "o papel de AUTOR PEDIDO DE DADOS no corpo da mensagem enviada na requisição do serviço web. Esse "
    "termo de autorização está assinado digitalmente com o certificado digital do PROCURADOR ou OUTORGADO "
    "DO CONTRIBUINTE responsável, identificado como AUTOR DO PEDIDO DE DADOS."
)
TEXTO_AVISO = (
    "O acesso a estas informações foi autorizado pelo próprio PROCURADOR ou OUTORGADO DO CONTRIBUINTE, "
    "responsável pela informação, via assinatura digital. É dever do destinatário da autorização e consumidor "
    "deste acesso observar a adoção de base legal para o tratamento dos dados recebidos conforme artigos 7º ou "
    "11º da LGPD (Lei n.º 13.709, de 14 de agosto de 2018), aos direitos do titular dos dados (art. 9º, 17 e 18, "
    "da LGPD) e aos princípios que norteiam todos os tratamentos de dados no Brasil (art. 6º, da LGPD)."
)
TEXTO_FINALIDADE = (
    "A finalidade única e exclusiva desse TERMO DE AUTORIZAÇÃO, é garantir que o CONTRATANTE apresente a API "
    "INTEGRA CONTADOR esse consentimento do PROCURADOR ou OUTORGADO DO CONTRIBUINTE assinado digitalmente, para "
    "que possa realizar as requisições dos serviços web da API INTEGRA CONTADOR em nome do AUTOR PEDIDO DE DADOS "
    "(PROCURADOR ou OUTORGADO DO CONTRIBUINTE)."
)

_RE_TOKEN_HEADER = re.compile(r"autenticar_procurador_token:([^\"\s]+)")


def gerar_termo(
    *,
    contratante_cnpj: str,
    nome_escritorio: str,
    procurador_doc: str,
    procurador_nome: str,
    hoje: datetime | None = None,
    vigencia_dias: int = 365,
) -> bytes:
    hoje = hoje or datetime.now(FUSO_BR)
    raiz = etree.Element("termoDeAutorizacao")
    dados = etree.SubElement(raiz, "dados")
    etree.SubElement(dados, "sistema").set("id", "API Integra Contador")
    etree.SubElement(dados, "termo").set("texto", TEXTO_TERMO)
    etree.SubElement(dados, "avisoLegal").set("texto", TEXTO_AVISO)
    etree.SubElement(dados, "finalidade").set("texto", TEXTO_FINALIDADE)
    etree.SubElement(dados, "dataAssinatura").set("data", hoje.strftime("%Y%m%d"))
    etree.SubElement(dados, "vigencia").set("data", (hoje + timedelta(days=vigencia_dias)).strftime("%Y%m%d"))
    dest = etree.SubElement(dados, "destinatario")
    dest.set("numero", so_digitos(contratante_cnpj))
    dest.set("nome", (nome_escritorio or "CONTRATANTE").strip())
    dest.set("tipo", "PF" if tipo_documento(contratante_cnpj) == 1 else "PJ")
    dest.set("papel", "contratante")
    ass = etree.SubElement(dados, "assinadoPor")
    ass.set("numero", so_digitos(procurador_doc))
    ass.set("nome", procurador_nome.strip())
    ass.set("tipo", "PF" if tipo_documento(procurador_doc) == 1 else "PJ")
    ass.set("papel", "autor pedido de dados")
    return etree.tostring(raiz, encoding="UTF-8", xml_declaration=True)


def assinar_termo(xml: bytes, pfx: bytes, senha: str) -> bytes:
    chave_pem, cert_pem = pem_chave_e_certificado(pfx, senha)
    raiz = etree.fromstring(xml, parser=etree.XMLParser(remove_blank_text=True))
    assinado = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    ).sign(raiz, key=chave_pem, cert=cert_pem)
    return etree.tostring(assinado, encoding="UTF-8", xml_declaration=True)


def _expiracao(valor: str | None) -> float:
    """Converte a validade informada pelo SERPRO; sem ela, 23:59 do dia seguinte (Brasília)."""
    if valor:
        try:
            return parsedate_to_datetime(valor).timestamp()  # header Expires (RFC 7231)
        except (TypeError, ValueError):
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%d/%m/%Y %H:%M:%S", "%Y%m%d%H%M%S"):
            try:
                dt = datetime.strptime(valor, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=FUSO_BR)
                return dt.timestamp()
            except ValueError:
                continue
    amanha = datetime.now(FUSO_BR).date() + timedelta(days=1)
    return datetime(amanha.year, amanha.month, amanha.day, 23, 59, tzinfo=FUSO_BR).timestamp()


class AutenticacaoProcurador:
    _cache: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()

    def __init__(self, client: "SerproClient"):
        self.client = client
        self.proc = client.cred.procurador

    @property
    def _chave(self) -> str:
        return f"{so_digitos(self.client.cred.contratante_cnpj)}:{so_digitos(self.proc.cpf)}"

    def invalidar(self) -> None:
        with self._lock:
            self._cache.pop(self._chave, None)

    def token(self) -> str:
        with self._lock:
            atual = self._cache.get(self._chave)
        if atual and time.time() < atual[1] - MARGEM_S:
            return atual[0]
        token, expira = self._solicitar()
        with self._lock:
            self._cache[self._chave] = (token, expira)
        return token

    def _solicitar(self) -> tuple[str, float]:
        garantir_validade(self.proc.pfx, self.proc.senha_pfx, "procurador")
        termo = gerar_termo(
            contratante_cnpj=self.client.cred.contratante_cnpj,
            nome_escritorio=self.client.cred.nome_escritorio,
            procurador_doc=self.proc.cpf,
            procurador_nome=self.proc.nome,
        )
        assinado = assinar_termo(termo, self.proc.pfx, self.proc.senha_pfx)
        # Conferir na documentação vigente quem vai em `contribuinte` neste serviço;
        # aqui usamos o próprio procurador (autor do pedido).
        resp = self.client.chamar(
            SERVICOS["procurador_termo"],
            contribuinte=self.proc.cpf,
            dados={"xml": base64.b64encode(assinado).decode("ascii")},
            autor_pedido=self.proc.cpf,
            aceitar_304=True,
        )
        token = None
        dados = resp.dados if isinstance(resp.dados, dict) else {}
        token = dados.get("autenticar_procurador_token") or dados.get("token")
        if not token:
            token = resp.header("autenticar_procurador_token")
        if not token:
            for valor in resp.headers.values():
                m = _RE_TOKEN_HEADER.search(str(valor))
                if m:
                    token = m.group(1)
                    break
        if not token:
            raise IntegraErro("SERPRO não devolveu autenticar_procurador_token")
        expira = _expiracao(dados.get("data_hora_expiracao") or resp.header("Expires"))
        return token, expira
