"""Testes do instalador (`python -m onstage.instalar`).

O instalador grava segredo em três lugares, e a única propriedade dele que vale
testar é **a ordem**: conferir primeiro, escrever depois. Senha errada é o erro
mais comum deste cadastro, e um instalador que sobe o arquivo e grava dois dos
três segredos antes de descobrir isso deixa um estado meio-configurado — que
depois se diagnostica lendo log, que é exatamente o que o ensaio existe para
evitar.

Por isso os testes aqui são quase todos de recusa. Em todos, `SUPABASE_URL`
aponta para uma porta fechada e o `DATABASE_URL` para um banco que não tem
`vault`: se a validação deixasse passar, o comando quebraria na rede ou no SQL
com outra mensagem, e o teste acusa. A ausência de erro de rede **é** a prova de
que nada foi escrito.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import gerar_pfx

CNPJ_ON = "48744243000160"


def _instalar(tmp_path, pfx_bytes: bytes, senha: str, *extra: str) -> subprocess.CompletedProcess:
    arquivo = tmp_path / "cert.pfx"
    arquivo.write_bytes(pfx_bytes)
    return subprocess.run(
        [sys.executable, "-m", "onstage.instalar", str(arquivo), *extra],
        cwd=Path(__file__).resolve().parent.parent,
        env={
            **os.environ,
            # Banco e Storage inalcançáveis de propósito: nenhum teste aqui deve
            # chegar a tocá-los.
            "DATABASE_URL": "postgresql://127.0.0.1:1/nao_existe",
            "SUPABASE_URL": "http://127.0.0.1:1",
            "SUPABASE_SERVICE_ROLE_KEY": "chave-de-teste",
            "INTEGRA_SENHA_PFX": senha,
            "INTEGRA_CONSUMER_KEY": "ck-de-teste",
            "INTEGRA_CONSUMER_SECRET": "cs-de-teste",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_senha_errada_nao_grava_nada(tmp_path):
    """A falha mais comum, e a que mais importa não deixar rastro pela metade."""
    r = _instalar(tmp_path, gerar_pfx(senha="a-senha-certa"), "a-senha-errada")
    assert r.returncode == 1
    assert "o .pfx NÃO abriu com essa senha" in r.stdout
    assert "Nada foi gravado" in r.stdout
    # Não chegou ao Storage nem ao Postgres: se tivesse chegado, a porta fechada
    # apareceria como erro de conexão.
    assert "Subindo para" not in r.stdout
    assert "Vault:" not in r.stdout


def test_certificado_de_outro_cnpj_e_recusado(tmp_path):
    """O `.pfx` de um cliente no lugar do da ON.

    São 94 certificados de clientes no bucket `certificados`; subir um deles
    aqui daria um mTLS que o SERPRO aceita como aquele cliente e recusa para
    todos os outros — sintoma confuso, causa boba, e caro porque cada recusa
    nos endpoints pagos é cobrada.
    """
    pfx = gerar_pfx(nome="CLIENTE QUALQUER LTDA:12345678000199", senha="x")
    r = _instalar(tmp_path, pfx, "x", "--cnpj", CNPJ_ON)
    assert r.returncode == 1
    assert "CERTIFICADO DE OUTRO CNPJ" in r.stdout
    assert "12345678000199" in r.stdout
    assert "Subindo para" not in r.stdout


def test_certificado_vencido_e_recusado(tmp_path):
    """Vencido não é aviso, é impedimento: nenhuma chamada passaria."""
    pfx = gerar_pfx(nome=f"ONSTAGE:{CNPJ_ON}", senha="x", dias=-1, inicio_dias=-400)
    r = _instalar(tmp_path, pfx, "x", "--cnpj", CNPJ_ON)
    assert r.returncode == 1
    assert "VENCIDO" in r.stdout
    assert "Subindo para" not in r.stdout


def test_arquivo_inexistente_falha_antes_de_pedir_segredo(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "onstage.instalar", str(tmp_path / "nao-existe.pfx")],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "DATABASE_URL": "postgresql://127.0.0.1:1/x",
             "SUPABASE_URL": "http://127.0.0.1:1",
             "SUPABASE_SERVICE_ROLE_KEY": "k"},
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 1
    assert "Não achei o arquivo" in r.stdout


def test_sem_variaveis_de_ambiente_nao_pede_segredo(tmp_path):
    """Sem destino não há o que gravar — e perguntar a senha antes seria pedir
    um segredo para jogar fora."""
    arquivo = tmp_path / "cert.pfx"
    arquivo.write_bytes(gerar_pfx())
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
    r = subprocess.run(
        [sys.executable, "-m", "onstage.instalar", str(arquivo)],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 1
    assert "Falta definir" in r.stdout
    assert "senha do .pfx" not in r.stdout


def test_segredo_nunca_aparece_na_saida(tmp_path):
    """O instalador lida com os três segredos; nenhum pode ser impresso.

    O comando existe em parte para tirar a senha de um `config.json` em texto
    puro. Imprimi-la de volta no terminal recriaria o problema num lugar novo.
    """
    pfx = gerar_pfx(nome=f"ONSTAGE:{CNPJ_ON}", senha="senha-secreta-do-pfx")
    r = _instalar(tmp_path, pfx, "senha-secreta-do-pfx", "--cnpj", CNPJ_ON)
    saida = r.stdout + r.stderr
    assert "senha-secreta-do-pfx" not in saida
    assert "ck-de-teste" not in saida
    assert "cs-de-teste" not in saida
    # o tamanho, sim: é o que separa "não cadastrei" de "cadastrei com espaço"
    assert "20 caracteres" in saida


def test_falha_de_rede_no_upload_nao_vira_traceback(tmp_path):
    """Traceback aqui é pior que em quase qualquer outro lugar.

    Até este ponto a tela vinha dizendo exatamente onde estava o problema, e a
    última linha impressa antes do estouro era "Subindo para…" — o que faz
    parecer que o arquivo subiu e o erro foi depois. Com o certificado válido e
    o CNPJ certo, a execução chega mesmo ao Storage, então este é o único teste
    do arquivo que exercita o caminho de escrita.
    """
    pfx = gerar_pfx(nome=f"ONSTAGE:{CNPJ_ON}", senha="x")
    r = _instalar(tmp_path, pfx, "x", "--cnpj", CNPJ_ON)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "erro de rede" in r.stdout
    assert "Nada foi gravado" in r.stdout
    assert "Vault:" not in r.stdout


def test_nomes_dos_segredos_batem_com_o_que_o_ensaio_sugere():
    """Instalador e ensaio têm de concordar no nome do segredo no Vault.

    Se divergirem, o instalador grava em `integra_senha_pfx` e o ensaio procura
    outro nome: o diagnóstico diria "segredo não existe no Vault" sobre um
    segredo que existe, que é o tipo de pista que custa uma tarde.
    """
    from onstage.instalar import CHAVE_REF, SEGREDO_REF, SENHA_REF

    ensaio = (Path(__file__).resolve().parent.parent / "onstage" / "ensaio.py").read_text()
    for ref in (SENHA_REF, CHAVE_REF, SEGREDO_REF):
        assert ref in ensaio, f"{ref} não aparece no texto do ensaio"
