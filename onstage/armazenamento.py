"""Storage: PDFs no bucket privado `integra`.

Privado, e isso não é detalhe de configuração. Um relatório SITFIS traz dívida,
omissão de declaração e endereço do contribuinte; uma URL pública de bucket é
uma URL que vaza por print, histórico de navegador e log de proxy. O portal só
gera **link assinado de 5 minutos**, dentro de `lib/integra/*`.

Layout `{cnpj}/{tipo}/{aaaamm}/{arquivo}` — o ano-mês existe para permitir
retenção por prefixo em vez de varredura do bucket.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("onstage.armazenamento")


class StorageSupabase:
    def __init__(
        self,
        supabase_url: str | None = None,
        service_key: str | None = None,
        bucket: str = "integra",
    ):
        self.url = (supabase_url or os.environ["SUPABASE_URL"]).rstrip("/")
        self.key = service_key or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.bucket = bucket

    def salvar(self, caminho: str, conteudo: bytes, content_type: str = "application/pdf") -> str:
        """Devolve o caminho dentro do bucket — nunca uma URL.

        Devolver caminho, e não URL, é o que impede uma URL assinada de longa
        duração virar o valor gravado no banco: a assinatura é gerada na hora da
        leitura, por quem tem permissão de ler.

        `x-upsert` ligado porque reprocessar o mesmo relatório é esperado (parser
        novo sobre o mesmo PDF) e falhar por arquivo existente só obrigaria a
        apagar antes — mais chamadas, mesmo resultado.
        """
        r = httpx.post(
            f"{self.url}/storage/v1/object/{self.bucket}/{caminho}",
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            content=conteudo,
            timeout=120.0,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"Falha ao gravar {self.bucket}/{caminho} (HTTP {r.status_code}): {r.text[:300]}"
            )
        log.info("PDF gravado: %s/%s (%s bytes)", self.bucket, caminho, len(conteudo))
        return caminho
