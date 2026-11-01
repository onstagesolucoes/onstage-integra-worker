"""Catálogo dos serviços Integra Contador usados pelo portal.

Conferido contra o produto do fornecedor em 01/11/2026 (decompilação do
`IntegraContadorDesktop.exe`, Python 3.12 — ver a seção "Confirmações da
decompilação" do mapa técnico). Os códigos abaixo são os que uma aplicação em
produção de fato envia, não leitura de documentação.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Endpoint(str, Enum):
    APOIAR = "Apoiar"
    CONSULTAR = "Consultar"
    EMITIR = "Emitir"
    DECLARAR = "Declarar"
    MONITORAR = "Monitorar"


class Cobranca(str, Enum):
    """Família de cobrança: é o endpoint em minúsculas, e nada mais.

    Os valores espelham o `check` de `integra_chamadas.cobranca` no portal
    (`consultar`, `emitir`, `declarar`, `apoiar`, `monitorar`) — escrever aqui
    um valor fora dessa lista faz o insert do worker ser recusado pelo Postgres.
    """

    CONSULTAR = "consultar"
    EMITIR = "emitir"
    DECLARAR = "declarar"
    APOIAR = "apoiar"
    MONITORAR = "monitorar"


@dataclass(frozen=True)
class Servico:
    chave: str
    id_sistema: str
    id_servico: str
    versao: str
    endpoint: Endpoint
    accept: str = "application/json"

    @property
    def cobranca(self) -> Cobranca:
        """Derivada do endpoint, de propósito.

        Era um campo à parte, e por isso podia discordar do endpoint — e
        discordava em dois dos doze serviços: o indicador da caixa postal
        (`/Monitorar`, **gratuito**) estava marcado como consulta paga, e a
        situação do MIT (`/Apoiar`) também. Um campo que pode mentir sobre o
        outro não tem razão de existir: a cobrança **é** o endpoint.
        """
        return Cobranca(self.endpoint.value.lower())


def _s(chave, sistema, servico, versao, endpoint, accept="application/json") -> Servico:
    return Servico(chave, sistema, servico, versao, endpoint, accept)


E = Endpoint

SERVICOS: dict[str, Servico] = {s.chave: s for s in [
    # Situação fiscal. Duas etapas, dois endpoints — e o relatório é **emissão**
    # (R$ 0,32), não consulta: tratar SITFIS como consulta subestima o custo em
    # cerca de um terço.
    _s("sitfis_protocolo", "SITFIS", "SOLICITARPROTOCOLO91", "2.0", E.APOIAR, "text/plain"),
    _s("sitfis_relatorio", "SITFIS", "RELATORIOSITFIS92", "2.0", E.EMITIR, "text/plain"),
    # Guias
    _s("das_simples", "PGDASD", "GERARDAS12", "1.0", E.EMITIR),
    _s("das_mei", "PGMEI", "GERARDASPDF21", "1.0", E.EMITIR),
    _s("darf_dctfweb", "DCTFWEB", "GERARGUIA31", "1.0", E.EMITIR),
    # Caixa postal. O indicador vai para `/Monitorar`, que é **gratuito** — é a
    # única chamada que o fornecedor não registra na própria tabela de uso. É
    # ele que autoriza a lista, que é a chamada paga.
    _s("caixa_indicador", "CAIXAPOSTAL", "INNOVAMSG63", "1.0", E.MONITORAR),
    _s("caixa_lista", "CAIXAPOSTAL", "MSGCONTRIBUINTE61", "1.0", E.CONSULTAR),
    _s("caixa_detalhe", "CAIXAPOSTAL", "MSGDETALHAMENTO62", "1.0", E.CONSULTAR),
    # Pagamentos
    _s("pagamentos", "PAGTOWEB", "PAGAMENTOS71", "1.0", E.CONSULTAR),
    # MIT
    _s("mit_encerrar", "MIT", "ENCAPURACAO314", "1.0", E.DECLARAR),
    _s("mit_situacao", "MIT", "SITUACAOENC315", "1.0", E.APOIAR),
    # Procurador
    _s("procurador_termo", "AUTENTICAPROCURADOR", "ENVIOXMLASSINADO81", "1.0", E.APOIAR),
]}


@dataclass(frozen=True)
class ModalidadeParcelamento:
    codigo: str          # chave interna e valor salvo nos jobs do portal
    nome: str
    id_sistema: str
    pedidos: str
    parcelas: str
    emitir: str


# As seis modalidades, cada uma com **três** serviços. O padrão é uma trinca
# consecutiva (`X1` emitir / `X2` parcelas / `X3` pedidos) — e o RELP-SN é a
# exceção confirmada: segue a trinca em dois serviços e tem o de pedidos noutro
# bloco numérico. Normalizar `173` para `193` quebra só o RELP-SN **sem erro
# visível**: a chamada passa a consultar outro serviço e volta vazia.
MODALIDADES: dict[str, ModalidadeParcelamento] = {m.codigo: m for m in [
    ModalidadeParcelamento("PARCSN", "Parcelamento Simples Nacional", "PARCSN", "PEDIDOSPARC163", "PARCELASPARAGERAR162", "GERARDAS161"),
    ModalidadeParcelamento("PARCMEI", "Parcelamento MEI", "PARCMEI", "PEDIDOSPARC203", "PARCELASPARAGERAR202", "GERARDAS201"),
    ModalidadeParcelamento("PERTSN", "PERT Simples Nacional", "PERTSN", "PEDIDOSPARC183", "PARCELASPARAGERAR182", "GERARDAS181"),
    ModalidadeParcelamento("PERTMEI", "PERT MEI", "PERTMEI", "PEDIDOSPARC223", "PARCELASPARAGERAR222", "GERARDAS221"),
    ModalidadeParcelamento("RELPSN", "RELP Simples Nacional", "RELPSN", "PEDIDOSPARC173", "PARCELASPARAGERAR192", "GERARDAS191"),
    ModalidadeParcelamento("RELPMEI", "RELP MEI", "RELPMEI", "PEDIDOSPARC233", "PARCELASPARAGERAR232", "GERARDAS231"),
]}


def servicos_parcelamento(codigo: str) -> dict[str, Servico]:
    """Os três serviços (pedidos, parcelas, emitir) de uma modalidade."""
    m = MODALIDADES[codigo]
    return {
        "pedidos": _s(f"{codigo}_pedidos", m.id_sistema, m.pedidos, "1.0", E.CONSULTAR, "text/plain"),
        "parcelas": _s(f"{codigo}_parcelas", m.id_sistema, m.parcelas, "1.0", E.CONSULTAR, "text/plain"),
        "emitir": _s(f"{codigo}_emitir", m.id_sistema, m.emitir, "1.0", E.EMITIR, "text/plain"),
    }


def total_servicos() -> int:
    return len(SERVICOS) + 3 * len(MODALIDADES)
