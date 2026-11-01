"""Cliente do Integra Contador: autenticação mTLS, envelope, retentativa e registro."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

import httpx

from .auth import URL_AUTENTICACAO, CacheMemoria, TokenCache, Tokens, cabecalhos_autenticacao, tokens_da_resposta
from .catalogo import Cobranca, Servico
from .certificado import contexto_ssl, garantir_validade
from .config import Credenciais
from .envelope import decodificar_dados, montar_envelope, so_digitos
from .erros import AindaProcessando, ErroSerpro, FalhaAutenticacao
from .mascara import mascarar
from .preco import custo

log = logging.getLogger("integra_serpro.http")

URL_GATEWAY = "https://gateway.apiserpro.serpro.gov.br/integra-contador/v1"
_CACHE_PADRAO = CacheMemoria()


@dataclass
class RespostaSerpro:
    status_http: int
    dados: Any
    mensagens: list[dict]
    headers: dict[str, str]
    corpo: dict = field(default_factory=dict)

    def header(self, nome: str, padrao: str | None = None) -> str | None:
        nome = nome.lower()
        return next((v for k, v in self.headers.items() if k.lower() == nome), padrao)


@dataclass
class RegistroChamada:
    chave_servico: str
    id_sistema: str
    id_servico: str
    endpoint: str
    cobranca: str
    contribuinte: str
    http_status: int | None
    sucesso: bool
    duracao_ms: int
    request_mascarado: Any
    response_mascarado: Any
    erro: str | None = None
    # `None` = preço não conhecido (declarar/apoiar) e vira NULL em
    # `integra_chamadas.custo_estimado`; `0.00` = confirmado gratuito
    # (/Monitorar). A view do teto diário soma esta coluna — sem ela preenchida,
    # o teto soma zero e nunca dispara.
    custo_estimado: Decimal | None = None


Registrador = Callable[[RegistroChamada], None]
# Quantas chamadas da mesma família de cobrança já houve no mês. Quem sabe isso
# é o banco, não o pacote; sem o contador, o preço sai cheio — errar para cima é
# o lado seguro de um limite de gasto.
ContadorMensal = Callable[[Cobranca], int]


class SerproClient:
    """Um cliente, instância única. Reaproveite-a: ela mantém a conexão TLS.

    Antes era "um por escritório", com o cache de token indexado por tenant.
    Com um contratante só, a chave do cache passa a ser o CNPJ do contratante —
    que é o que de fato identifica o par de tokens do lado do SERPRO.
    """

    def __init__(
        self,
        credenciais: Credenciais,
        *,
        cache: TokenCache | None = None,
        registrar: Registrador | None = None,
        chamadas_no_mes: ContadorMensal | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 90.0,
        url_gateway: str = URL_GATEWAY,
        url_autenticacao: str = URL_AUTENTICACAO,
        tentativas_rede: int = 2,
    ):
        credenciais.validar()
        self.cred = credenciais
        self.cache = cache or _CACHE_PADRAO
        self.registrar = registrar
        self.chamadas_no_mes = chamadas_no_mes
        self.url_gateway = url_gateway.rstrip("/")
        self.url_autenticacao = url_autenticacao
        self.tentativas_rede = tentativas_rede
        if transport is not None:  # testes
            self._http = httpx.Client(transport=transport, timeout=timeout)
        else:
            garantir_validade(credenciais.pfx, credenciais.senha_pfx, "contratante")
            self._http = httpx.Client(verify=contexto_ssl(credenciais.pfx, credenciais.senha_pfx), timeout=timeout)
        self._procurador = None
        if credenciais.procurador:
            from .procurador import AutenticacaoProcurador
            self._procurador = AutenticacaoProcurador(self)

    # ---------------------------------------------------------------- tokens
    @property
    def _chave_cache(self) -> str:
        return f"serpro:{self.cred.contratante_cnpj}"

    def tokens(self, renovar: bool = False) -> Tokens:
        if not renovar:
            atual = self.cache.obter(self._chave_cache)
            if atual:
                return atual
        resp = self._http.post(
            self.url_autenticacao,
            headers=cabecalhos_autenticacao(self.cred.consumer_key, self.cred.consumer_secret),
            data={"grant_type": "client_credentials"},
        )
        if resp.status_code != 200:
            raise FalhaAutenticacao(f"Autenticação SERPRO recusada (HTTP {resp.status_code})")
        try:
            tokens = tokens_da_resposta(resp.json())
        except (ValueError, KeyError) as exc:
            raise FalhaAutenticacao("Resposta de autenticação sem access_token/jwt_token") from exc
        self.cache.salvar(self._chave_cache, tokens)
        return tokens

    # ---------------------------------------------------------------- chamada
    def chamar(
        self,
        servico: Servico,
        contribuinte: str,
        dados: dict | str | None = None,
        *,
        usar_procurador: bool = True,
        autor_pedido: str | None = None,
        aceitar_304: bool = False,
    ) -> RespostaSerpro:
        token_proc = None
        autor = autor_pedido or self.cred.contratante_cnpj
        if self._procurador and usar_procurador and autor_pedido is None:
            token_proc = self._procurador.token()
            autor = self.cred.procurador.cpf

        envelope = montar_envelope(
            contratante=self.cred.contratante_cnpj,
            autor_pedido=autor,
            contribuinte=contribuinte,
            id_sistema=servico.id_sistema,
            id_servico=servico.id_servico,
            versao=servico.versao,
            dados=dados,
        )
        url = f"{self.url_gateway}/{servico.endpoint.value}"

        renovou = False
        tentativa_rede = 0
        while True:
            tokens = self.tokens()
            headers = {
                "Authorization": f"Bearer {tokens.access_token}",
                "jwt_token": tokens.jwt_token,
                "Content-Type": "application/json",
                "accept": servico.accept,
            }
            if token_proc:
                headers["autenticar_procurador_token"] = token_proc
            inicio = time.perf_counter()
            try:
                resp = self._http.post(url, headers=headers, content=json.dumps(envelope, ensure_ascii=False).encode())
            except httpx.TransportError as exc:
                self._registrar(servico, contribuinte, envelope, None, None, inicio, erro=str(exc))
                # Declarar não é repetido automaticamente: poderia transmitir duas vezes.
                if servico.cobranca is not Cobranca.DECLARAR and tentativa_rede < self.tentativas_rede:
                    tentativa_rede += 1
                    time.sleep(2 ** tentativa_rede)
                    continue
                raise

            if resp.status_code in (401, 403) and not renovou:
                renovou = True
                self.cache.invalidar(self._chave_cache)
                if token_proc:
                    self._procurador.invalidar()
                    token_proc = self._procurador.token()
                self._registrar(servico, contribuinte, envelope, resp, None, inicio)
                continue
            break

        resposta = self._interpretar(resp)
        self._registrar(servico, contribuinte, envelope, resp, resposta, inicio)

        if resp.status_code == 200 or (resp.status_code == 304 and aceitar_304):
            return resposta
        if resp.status_code == 204:
            raise AindaProcessando()
        raise ErroSerpro(resp.status_code, resposta.mensagens, resp.text)

    # ---------------------------------------------------------------- apoio
    @staticmethod
    def _interpretar(resp: httpx.Response) -> RespostaSerpro:
        corpo: dict = {}
        if resp.content:
            try:
                bruto = resp.json()
                corpo = bruto if isinstance(bruto, dict) else {"dados": bruto}
            except ValueError:
                corpo = {}
        mensagens = corpo.get("mensagens") or []
        return RespostaSerpro(
            status_http=resp.status_code,
            dados=decodificar_dados(corpo.get("dados")),
            mensagens=mensagens if isinstance(mensagens, list) else [],
            headers=dict(resp.headers),
            corpo=corpo,
        )

    def _registrar(self, servico, contribuinte, envelope, resp, resposta, inicio, erro=None):
        duracao = int((time.perf_counter() - inicio) * 1000)
        status = resp.status_code if resp is not None else None
        sucesso = status in (200, 304)
        req_m = mascarar(envelope)
        resp_m = mascarar(resposta.corpo) if resposta else None
        log.info("SERPRO %s/%s %s status=%s %sms", servico.id_sistema, servico.id_servico, so_digitos(contribuinte), status, duracao)
        # Chamada que não chegou ao SERPRO não custa: falha de transporte sai
        # com custo nulo, senão uma rede instável vira gasto fantasma no painel.
        volume = 0
        if self.chamadas_no_mes:
            try:
                volume = self.chamadas_no_mes(servico.cobranca)
            except Exception:  # contador indisponível não derruba a chamada
                log.exception("Falha ao contar chamadas do mês; assumindo preço cheio")
        custo_estimado = custo(servico, volume) if status is not None else None
        if self.registrar:
            try:
                self.registrar(RegistroChamada(
                    chave_servico=servico.chave,
                    id_sistema=servico.id_sistema,
                    id_servico=servico.id_servico,
                    endpoint=servico.endpoint.value,
                    cobranca=servico.cobranca.value,
                    contribuinte=so_digitos(contribuinte),
                    http_status=status,
                    sucesso=sucesso,
                    duracao_ms=duracao,
                    request_mascarado=req_m,
                    response_mascarado=resp_m,
                    erro=erro,
                    custo_estimado=custo_estimado,
                ))
            except Exception:  # o registro nunca derruba a chamada
                log.exception("Falha ao registrar chamada SERPRO")

    def fechar(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fechar()
