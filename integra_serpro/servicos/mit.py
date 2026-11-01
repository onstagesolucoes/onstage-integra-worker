"""MIT: encerramento da apuração (ENCAPURACAO314) e consulta de situação (SITUACAOENC315).

Transmissão é ato declaratório. O portal deve exigir perfil autorizado,
confirmação explícita e, para retificadora, segunda confirmação.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..catalogo import SERVICOS
from ..client import SerproClient
from ..erros import IntegraErro
from ._util import para_decimal, primeiro_item

_RE_NOME = re.compile(r"^\s*(\d{8})\s*-\s*MIT\s*-\s*(\d{6})\.json\s*$", re.IGNORECASE)

SITUACOES_FINAIS = {"ENCERRADA", "EM_EDICAO", "EM_EDIÇÃO"}


@dataclass
class ValidacaoMit:
    raiz_cnpj: str | None
    competencia: str | None
    erros: list[str] = field(default_factory=list)
    total_debitos: int = 0
    valor_total: Decimal = Decimal("0.00")
    sem_movimento: bool = False

    @property
    def ok(self) -> bool:
        return not self.erros


def _debitos(payload: dict) -> list[dict]:
    blocos = payload.get("Debitos") or {}
    saida: list[dict] = []
    if isinstance(blocos, dict):
        for grupo in blocos.values():
            if isinstance(grupo, list):
                saida.extend(d for d in grupo if isinstance(d, dict))
            elif isinstance(grupo, dict):
                for chave in ("ListaDebitos", "ListaDebitosAposEvento"):
                    saida.extend(d for d in grupo.get(chave) or [] if isinstance(d, dict))
    return saida


def validar(nome_arquivo: str, payload: Any) -> ValidacaoMit:
    m = _RE_NOME.match(nome_arquivo or "")
    v = ValidacaoMit(raiz_cnpj=m.group(1) if m else None, competencia=m.group(2) if m else None)
    if not m:
        v.erros.append("Nome do arquivo deve seguir '<raiz CNPJ 8 dígitos> - MIT - <AAAAMM>.json'")
    if not isinstance(payload, dict):
        v.erros.append("O JSON MIT deve ter um objeto na raiz")
        return v
    pa = payload.get("PeriodoApuracao")
    if not isinstance(pa, dict):
        v.erros.append("JSON sem PeriodoApuracao")
    else:
        try:
            comp = f"{int(pa.get('AnoApuracao')):04d}{int(pa.get('MesApuracao')):02d}"
            if v.competencia and comp != v.competencia:
                v.erros.append(f"Competência divergente: arquivo {v.competencia}, JSON {comp}")
            v.competencia = v.competencia or comp
        except (TypeError, ValueError):
            v.erros.append("Período de apuração inválido no JSON")
    if "DadosIniciais" not in payload:
        v.erros.append("JSON sem DadosIniciais")
    debitos = _debitos(payload)
    v.total_debitos = len(debitos)
    v.valor_total = sum((para_decimal(d.get("ValorDebito")) for d in debitos), Decimal("0.00"))
    dados_iniciais = payload.get("DadosIniciais") or {}
    v.sem_movimento = bool(dados_iniciais.get("SemMovimento")) if isinstance(dados_iniciais, dict) else False
    if not v.sem_movimento and not debitos:
        v.erros.append("Apuração com movimento sem débitos informados")
    return v


@dataclass
class ResultadoEncerramento:
    protocolo: str
    id_apuracao: int | None
    resposta: dict


@dataclass
class SituacaoEncerramento:
    situacao: str            # textoSituacao, ex.: ENCERRADA
    codigo: int | None       # situacaoApuracao (numérico)
    data_encerramento: str | None
    avisos: list
    resposta: dict

    @property
    def final(self) -> bool:
        return self.situacao.upper().replace(" ", "_") in SITUACOES_FINAIS


class Mit:
    def __init__(self, client: SerproClient):
        self.client = client

    def encerrar(self, cnpj_matriz: str, payload: dict) -> ResultadoEncerramento:
        resp = self.client.chamar(SERVICOS["mit_encerrar"], cnpj_matriz, payload)
        item = primeiro_item(resp.dados)
        protocolo = item.get("protocoloEncerramento")
        if not protocolo:
            raise IntegraErro("SERPRO não devolveu protocolo de encerramento")
        return ResultadoEncerramento(str(protocolo), item.get("idApuracao"), resp.corpo)

    def situacao(self, cnpj_matriz: str, protocolo: str) -> SituacaoEncerramento:
        resp = self.client.chamar(SERVICOS["mit_situacao"], cnpj_matriz, {"protocoloEncerramento": protocolo})
        item = primeiro_item(resp.dados)
        return SituacaoEncerramento(
            situacao=str(item.get("textoSituacao") or ""),
            codigo=item.get("situacaoApuracao"),
            data_encerramento=item.get("dataEncerramento"),
            avisos=item.get("avisosDctf") or [],
            resposta=resp.corpo,
        )
