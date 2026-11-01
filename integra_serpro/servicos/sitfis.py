"""Relatório de situação fiscal (SITFIS) em duas etapas.

1. solicitar_protocolo: devolve o protocolo e quantos segundos esperar.
2. obter_relatorio: depois da espera, devolve o PDF; levanta AindaProcessando
   se o SERPRO responder 204.

As etapas são separadas de propósito: a fila grava o protocolo e agenda a
segunda etapa (`tentar_apos`), sem prender um worker dormindo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..catalogo import SERVICOS
from ..client import SerproClient
from ..erros import AindaProcessando, IntegraErro
from ._util import buscar_chave, pdf_da_resposta, primeiro_item

ESPERA_PADRAO_S = 30
_RE_ETAG = re.compile(r'protocoloRelatorio:([^"\n]+)')


@dataclass(frozen=True)
class ProtocoloSitfis:
    protocolo: str
    espera_segundos: int


class Sitfis:
    def __init__(self, client: SerproClient):
        self.client = client

    def solicitar_protocolo(self, cnpj: str) -> ProtocoloSitfis:
        resp = self.client.chamar(SERVICOS["sitfis_protocolo"], cnpj, aceitar_304=True)
        # O protocolo vem no **cabeçalho `etag`**; o corpo é só o segundo lugar
        # onde procurar. Isso não é detalhe de implementação: quem lê apenas o
        # JSON nunca acha o protocolo, e o sintoma é "o SERPRO não devolveu o
        # protocolo" com uma resposta 200 na mão. O produto do fornecedor extrai
        # do cabeçalho por regex, nos dois casos (200 e 304).
        do_etag = _RE_ETAG.search(resp.header("etag", "") or "")
        if resp.status_http == 304:
            # 304 é normal, não erro: o protocolo anterior ainda vale e o corpo
            # vem vazio — aqui o cabeçalho é a **única** fonte.
            if not do_etag:
                raise IntegraErro("SERPRO respondeu 304 sem protocolo no header etag")
            return ProtocoloSitfis(do_etag.group(1).strip(), ESPERA_PADRAO_S)
        item = primeiro_item(resp.dados)
        protocolo = (
            (do_etag.group(1).strip() if do_etag else None)
            or item.get("protocoloRelatorio")
            or buscar_chave(resp.dados, ["protocoloRelatorio"])
        )
        if not protocolo:
            raise IntegraErro("SERPRO não devolveu o protocolo do relatório (nem no header etag, nem no corpo)")
        try:
            espera_ms = int(item.get("tempoEspera") or ESPERA_PADRAO_S * 1000)
        except (TypeError, ValueError):
            espera_ms = ESPERA_PADRAO_S * 1000
        return ProtocoloSitfis(str(protocolo), max(1, espera_ms // 1000))

    def obter_relatorio(self, cnpj: str, protocolo: str) -> bytes:
        resp = self.client.chamar(SERVICOS["sitfis_relatorio"], cnpj, {"protocoloRelatorio": protocolo})
        item = primeiro_item(resp.dados)
        if not buscar_chave(item or resp.dados, ["pdf"]):
            espera = item.get("tempoEspera")
            raise AindaProcessando(espera_segundos=int(espera) // 1000 if espera else ESPERA_PADRAO_S)
        return pdf_da_resposta(resp.dados, ("pdf",))
