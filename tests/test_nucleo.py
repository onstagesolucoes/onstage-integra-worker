import json
import ssl

import httpx
import pytest
from lxml import etree
from signxml import XMLVerifier

from integra_serpro import SERVICOS, SerproClient
from integra_serpro.auth import CacheMemoria
from integra_serpro.certificado import contexto_ssl, garantir_validade, info_certificado
from integra_serpro.envelope import montar_envelope, tipo_documento
from integra_serpro.erros import AindaProcessando, CertificadoVencido, ConfiguracaoIncompleta, ErroSerpro
from integra_serpro.mascara import mascarar
from integra_serpro.procurador import assinar_termo, gerar_termo

from conftest import gerar_pfx


def test_envelope_tipos_e_dados_como_string():
    env = montar_envelope(contratante="12.345.678/0001-99", autor_pedido="12345678000199",
                          contribuinte="123.456.789-09", id_sistema="PGDASD", id_servico="GERARDAS12",
                          versao="1.0", dados={"periodoApuracao": "202409"})
    assert env["contratante"] == {"numero": "12345678000199", "tipo": 2}
    assert env["contribuinte"] == {"numero": "12345678909", "tipo": 1}
    assert env["pedidoDados"]["dados"] == '{"periodoApuracao":"202409"}'
    with pytest.raises(ValueError):
        tipo_documento("123")


def test_token_em_cache_e_headers(client_fake, serpro):
    serpro.responder("GERARDAS12", dados=[{"pdf": "JVBERi0="}])
    client_fake.chamar(SERVICOS["das_simples"], "11222333000181", {"periodoApuracao": "202409"})
    client_fake.chamar(SERVICOS["das_simples"], "11222333000181", {"periodoApuracao": "202409"})
    assert serpro.auth_chamadas == 1
    h = serpro.requisicoes[0]["headers"]
    assert h["authorization"] == "Bearer AT1" and h["jwt_token"] == "JWT"
    assert serpro.requisicoes[0]["url"].endswith("/integra-contador/v1/Emitir")


def test_401_renova_token_uma_vez(client_fake, serpro):
    serpro.responder("GERARDAS12", status=401)
    serpro.responder("GERARDAS12", dados=[{"pdf": "x"}])
    r = client_fake.chamar(SERVICOS["das_simples"], "11222333000181", {"periodoApuracao": "202409"})
    assert r.status_http == 200 and serpro.auth_chamadas == 2


def test_204_e_erros(client_fake, serpro):
    serpro.responder("RELATORIOSITFIS92", status=204)
    with pytest.raises(AindaProcessando):
        client_fake.chamar(SERVICOS["sitfis_relatorio"], "11222333000181", {"protocoloRelatorio": "p"})
    serpro.responder("GERARDASPDF21", status=400, mensagens=[{"codigo": "Erro-1", "texto": "PA inválido"}])
    with pytest.raises(ErroSerpro) as e:
        client_fake.chamar(SERVICOS["das_mei"], "11222333000181", {"periodoApuracao": "209913"})
    assert "PA inválido" in str(e.value) and e.value.status_http == 400


def test_registro_mascara_segredos(client_fake, serpro):
    serpro.responder("ENVIOXMLASSINADO81", dados={"autenticar_procurador_token": "SEGREDO123456"})
    client_fake.chamar(SERVICOS["procurador_termo"], "12345678909", {"xml": "PD94bWwgdmVyc2lvbj0iMS4wIj8+" * 10})
    reg = client_fake.registros[-1]
    texto = json.dumps([reg.request_mascarado, reg.response_mascarado])
    assert "SEGREDO123456" not in texto and "PD94bWwg" not in texto
    # "apoiar", não "apoio": o valor tem de passar pelo `check` de
    # `integra_chamadas.cobranca` no portal, senão o insert do worker é recusado.
    assert reg.sucesso and reg.cobranca == "apoiar"
    # /Apoiar não tem preço conhecido — e NULL ("não sei") não é 0 ("não custou").
    assert reg.custo_estimado is None


def test_mascara_trunca_pdf():
    m = mascarar({"dados": json.dumps([{"pdf": "A" * 5000}]), "access_token": "abcdefghijk"})
    assert m["access_token"] == "***hijk"
    assert m["dados"][0]["pdf"].startswith("<5000")


def test_certificado(pfx):
    info = garantir_validade(pfx, "1234")
    assert info.dias_restantes >= 363
    assert isinstance(contexto_ssl(pfx, "1234"), ssl.SSLContext)
    with pytest.raises(ConfiguracaoIncompleta):
        info_certificado(pfx, "errada")
    with pytest.raises(CertificadoVencido):
        garantir_validade(gerar_pfx(dias=-1, inicio_dias=-30), "1234")


def test_credenciais_incompletas(credenciais):
    from dataclasses import replace
    with pytest.raises(ConfiguracaoIncompleta, match="consumer secret"):
        SerproClient(replace(credenciais, consumer_secret=""), transport=httpx.MockTransport(lambda r: None))


def test_termo_assinado_valido():
    pfx_proc = gerar_pfx("FULANO:12345678909")
    xml = gerar_termo(contratante_cnpj="12345678000199", nome_escritorio="ON", procurador_doc="12345678909", procurador_nome="FULANO")
    assinado = assinar_termo(xml, pfx_proc, "1234")
    raiz = etree.fromstring(assinado)
    assert raiz.find(".//{http://www.w3.org/2000/09/xmldsig#}Signature") is not None
    from integra_serpro.certificado import pem_chave_e_certificado
    _, cert_pem = pem_chave_e_certificado(pfx_proc, "1234")
    XMLVerifier().verify(assinado, x509_cert=cert_pem.decode().split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n")
    assert raiz.find("dados/assinadoPor").get("papel") == "autor pedido de dados"


def test_client_com_procurador(credenciais_procurador, serpro):
    serpro.responder("ENVIOXMLASSINADO81", dados={"autenticar_procurador_token": "TKPROC"})
    serpro.responder("GERARDAS12", dados=[{"pdf": "x"}])
    c = SerproClient(credenciais_procurador, cache=CacheMemoria(), transport=httpx.MockTransport(serpro))
    c.chamar(SERVICOS["das_simples"], "11222333000181", {"periodoApuracao": "202409"})
    c.chamar(SERVICOS["das_simples"], "11222333000181", {"periodoApuracao": "202410"})
    termo, das1, das2 = serpro.requisicoes
    assert termo["corpo"]["pedidoDados"]["idServico"] == "ENVIOXMLASSINADO81"
    assert das1["headers"]["autenticar_procurador_token"] == "TKPROC"
    assert das1["corpo"]["autorPedidoDados"] == {"numero": "12345678909", "tipo": 1}
    assert len(serpro.requisicoes) == 3  # token do procurador reaproveitado
