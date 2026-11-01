"""Caixa Postal e-CAC: indicador de novas, lista paginada e detalhe.

Marcar como lida no portal é só controle interno; nada aqui dá ciência no e-CAC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterator

from ..catalogo import SERVICOS
from ..client import SerproClient
from ._util import buscar_chave, para_data, primeiro_item

MARCADOR_VARIAVEL = "++VARIAVEL++"

# `statusLeitura` é filtro da lista e vai no payload. Não existe enum para ele
# no produto do fornecedor (procurado na decompilação: nenhum dicionário
# `STATUS_*` ou `MAP_*` de mensagem), então **os três valores abaixo seguem sem
# confirmação** — só o nome do campo e o fato de ser enviado como string estão
# confirmados. Na dúvida, use TODAS e filtre em casa: pedir tudo custa a mesma
# chamada.
TODAS, LIDAS, NAO_LIDAS = 0, 1, 2


@dataclass
class Mensagem:
    isn: str
    assunto: str
    data_envio: date | None
    hora_envio: str | None
    data_leitura: date | None
    data_ciencia: date | None
    data_expiracao: date | None
    remetente: str | None
    relevancia: str | None
    lida_no_ecac: bool
    bruto: dict = field(repr=False, default_factory=dict)


@dataclass
class DetalheMensagem:
    isn: str
    assunto: str
    corpo: str
    variaveis: list
    bruto: dict = field(repr=False, default_factory=dict)


def aplicar_variaveis(texto: str | None, variaveis: list | None) -> str:
    """Troca cada ++VARIAVEL++ pelo valor seguinte da lista, na ordem."""
    resultado = texto or ""
    for valor in variaveis or []:
        if MARCADOR_VARIAVEL not in resultado:
            break
        resultado = resultado.replace(MARCADOR_VARIAVEL, str(valor), 1)
    return resultado


def _hora(valor: Any) -> str | None:
    v = str(valor or "").strip()
    if len(v) == 6 and v.isdigit():
        return f"{v[:2]}:{v[2:4]}:{v[4:]}"
    return v or None


def _mensagem(item: dict) -> Mensagem:
    assunto = item.get("assuntoModelo") or ""
    if item.get("valorParametroAssunto"):
        assunto = assunto.replace(MARCADOR_VARIAVEL, str(item["valorParametroAssunto"]))
    return Mensagem(
        isn=str(item.get("isn")),
        assunto=assunto,
        data_envio=para_data(item.get("dataEnvio")),
        hora_envio=_hora(item.get("horaEnvio")),
        data_leitura=para_data(item.get("dataLeitura")),
        data_ciencia=para_data(item.get("dataCiencia")),
        # A lista chama de `dataValidade`; o detalhe, de `dataExpiracao`. Ler só
        # o segundo deixa a data de saída do ar sempre vazia na carga da lista —
        # que é justamente a carga que roda todo dia.
        data_expiracao=para_data(item.get("dataValidade") or item.get("dataExpiracao")),
        remetente=item.get("descricaoOrigem") or item.get("codigoSistemaRemetente"),
        relevancia=item.get("relevancia") or item.get("indRelevanciaMsg"),
        lida_no_ecac=str(item.get("indicadorLeitura", "")).upper() in ("S", "1", "TRUE"),
        bruto=item,
    )


class CaixaPostal:
    def __init__(self, client: SerproClient):
        self.client = client

    def indicador_novas(self, cnpj: str) -> int:
        resp = self.client.chamar(SERVICOS["caixa_indicador"], cnpj)
        valor = buscar_chave(resp.dados, ["indicadorMensagensNovas"])
        try:
            return int(valor or 0)
        except (TypeError, ValueError):
            return 0

    def listar(self, cnpj: str, status_leitura: int = TODAS, limite_paginas: int = 50) -> Iterator[Mensagem]:
        ponteiro = ""
        primeira = True
        for _ in range(limite_paginas):
            # Os dois campos vão como **string**, não inteiro — é assim que o
            # produto do fornecedor monta o payload (`str(status_leitura)`,
            # `str(indicador_pagina)`), e é a única referência que se sabe
            # funcionar em produção. `ponteiroPagina` só entra quando existe
            # ponteiro: na primeira página a chave fica fora do payload.
            dados = {
                "statusLeitura": str(status_leitura),
                "indicadorPagina": "0" if primeira else "1",
            }
            if not primeira and ponteiro:
                dados["ponteiroPagina"] = str(ponteiro)
            resp = self.client.chamar(SERVICOS["caixa_lista"], cnpj, dados)
            bloco = primeiro_item(buscar_chave(resp.dados, ["conteudo"]) or resp.dados)
            lista = buscar_chave(bloco, ["listaMensagens", "mensagens"]) or []
            for item in lista:
                if isinstance(item, dict) and item.get("isn"):
                    yield _mensagem(item)
            ultima = str(bloco.get("indicadorUltimaPagina", "S")).upper() == "S"
            ponteiro = bloco.get("ponteiroProximaPagina")
            if ultima or not ponteiro:
                return
            primeira = False

    def detalhe(self, cnpj: str, isn: str) -> DetalheMensagem:
        resp = self.client.chamar(SERVICOS["caixa_detalhe"], cnpj, {"isn": str(isn)})
        item = primeiro_item(buscar_chave(resp.dados, ["conteudo"]) or resp.dados)
        variaveis = item.get("variaveis") or []
        assunto = aplicar_variaveis(item.get("assuntoModelo"), [item.get("valorParametroAssunto")] if item.get("valorParametroAssunto") else [])
        return DetalheMensagem(
            isn=str(isn),
            assunto=assunto,
            corpo=aplicar_variaveis(item.get("corpoModelo"), variaveis),
            variaveis=variaveis,
            bruto=item,
        )
