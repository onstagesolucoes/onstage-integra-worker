"""Credenciais do contratante — uma só, porque é um só.

O pacote nunca lê segredo de arquivo nem de banco por conta própria: quem
entrega um `Credenciais` é um `CredentialProvider` implementado fora daqui
(em `onstage/segredos.py`, que busca no Supabase Vault e no bucket privado).

**Não existe `tenant_id`.** A versão original deste pacote era multi-inquilino
e pedia o tenant em toda chamada. No desenho do portal isso seria pior que
inútil: um escritório, um contratante na loja SERPRO, um e-CNPJ A1. Pior ainda,
a RLS que acompanhava o modelo era `tenant_id = (auth.jwt() ->> 'tenant_id')`
e o portal **não emite esse claim** — proteção que não protege e falha calada.

A identidade que importa aqui é outra, e é a que o portal já usa: o **CNPJ do
contribuinte** consultado. É o grão de todas as tabelas `integra_*` e é o que
`pode_ver(cnpj)` autoriza na leitura.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .erros import ConfiguracaoIncompleta


@dataclass(frozen=True)
class Procurador:
    cpf: str
    nome: str
    pfx: bytes = field(repr=False)
    senha_pfx: str = field(repr=False)


@dataclass(frozen=True)
class Credenciais:
    contratante_cnpj: str            # CNPJ do escritório (contratante na loja SERPRO)
    nome_escritorio: str
    pfx: bytes = field(repr=False)   # certificado A1 do contratante
    senha_pfx: str = field(repr=False)
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)
    procurador: Procurador | None = None

    def validar(self) -> None:
        faltando = [
            nome
            for nome, valor in (
                ("CNPJ do contratante", self.contratante_cnpj),
                ("certificado", self.pfx),
                ("senha do certificado", self.senha_pfx),
                ("consumer key", self.consumer_key),
                ("consumer secret", self.consumer_secret),
            )
            if not valor
        ]
        if faltando:
            raise ConfiguracaoIncompleta("Configuração SERPRO incompleta: " + ", ".join(faltando))


class CredentialProvider(Protocol):
    """Implementado pelo deploy. Deve buscar os segredos no cofre.

    Sem argumento, de propósito: não há o que escolher. Se algum dia houver um
    segundo contratante, o parâmetro volta — e volta junto a pergunta de qual
    certificado assina a chamada, que hoje tem resposta única.
    """

    def obter(self) -> Credenciais: ...
