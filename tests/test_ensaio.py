"""Testes do ensaio (`python -m onstage.ensaio`).

Só duas funções do ensaio têm lógica própria — o resto é cola de I/O, que o
próprio ensaio existe para exercitar contra o mundo real. As duas, porém,
decidem coisas que importam:

- `cnpj_do_certificado` é o que impede subir o `.pfx` errado. São 94
  certificados de clientes no bucket `certificados`; trocar um deles pelo
  e-CNPJ da ON dá um mTLS que o SERPRO aceita como aquele cliente e recusa para
  todos os outros. Sintoma confuso, causa boba.
- `presenca` descreve segredo sem revelá-lo. Se um dia alguém "melhorar" essa
  função imprimindo o valor para facilitar a depuração, o terminal e o
  histórico do shell viram um segundo lugar onde a senha do certificado mora.
  Por isso há um teste que afirma que o segredo **não** aparece na saída.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from onstage.ensaio import cnpj_do_certificado, presenca, so_digitos
from integra_serpro.certificado import info_certificado

from conftest import gerar_pfx

CNPJ_ON = "48744243000160"


# ------------------------------------------------------- CNPJ do certificado
@pytest.mark.parametrize(
    "titular, esperado",
    [
        # o formato da ICP-Brasil
        ("ONSTAGE SOLUCOES FINANCEIRAS LTDA:48744243000160", CNPJ_ON),
        ("TESTE LTDA:12345678000199", "12345678000199"),
        # razão social com dígito: é aqui que a extração ingênua erra, porque
        # juntar todos os dígitos daria 15 e os 14 primeiros seriam outro CNPJ
        ("EMPRESA 2 LTDA:48744243000160", CNPJ_ON),
        ("POSTO 24 HORAS LTDA:48744243000160", CNPJ_ON),
        ("3M DO BRASIL LTDA:48744243000160", CNPJ_ON),
        # CNPJ pontuado na cauda ainda é lido
        ("ALGUMA COISA:48.744.243/0001-60", CNPJ_ON),
        # sem CNPJ reconhecível: None, e não um chute
        ("CERTIFICADO SEM CNPJ NENHUM", None),
        ("", None),
        # cauda que não é CNPJ (CPF, 11 dígitos) não vira CNPJ
        ("JOAO DA SILVA:12345678901", None),
    ],
)
def test_cnpj_do_certificado(titular, esperado):
    assert cnpj_do_certificado(titular) == esperado


def test_nao_recorta_cnpj_de_dentro_de_sequencia_maior():
    """15 dígitos seguidos não são um CNPJ de 14 com um sobrando.

    Sem as âncoras, `\\d{14}` casaria os 14 primeiros de qualquer corrida mais
    longa e inventaria um CNPJ.
    """
    assert cnpj_do_certificado("ALGO 487442430001600 FIM") is None


def test_certificado_de_verdade_tem_o_cnpj_lido_do_binario():
    """Ida e volta pelo `.pfx`: gera, lê o titular do X.509, extrai o CNPJ.

    É o caminho exato do passo 5 do ensaio, e o que prova que a conferência
    funciona sobre um certificado real em vez de sobre uma string montada.
    """
    pfx = gerar_pfx(nome=f"ONSTAGE SOLUCOES FINANCEIRAS LTDA:{CNPJ_ON}", senha="x")
    info = info_certificado(pfx, "x")
    assert cnpj_do_certificado(info.titular) == CNPJ_ON


def test_certificado_de_outro_cnpj_e_detectado():
    """O caso que o passo 5 existe para pegar: `.pfx` de cliente no lugar do da ON."""
    pfx = gerar_pfx(nome="CLIENTE QUALQUER LTDA:12345678000199", senha="x")
    info = info_certificado(pfx, "x")
    lido = cnpj_do_certificado(info.titular)
    assert lido == "12345678000199"
    assert lido != so_digitos(CNPJ_ON)      # a comparação do ensaio acusaria


# --------------------------------------------------------------- `presenca`
def test_presenca_nunca_mostra_o_segredo():
    segredo = "senha-secreta-do-certificado-123"
    saida = presenca(segredo)
    assert segredo not in saida
    assert "senha" not in saida
    assert str(len(segredo)) in saida       # o tamanho, sim


def test_presenca_distingue_os_tres_erros_reais():
    assert presenca(None) == "ausente"
    assert "VAZIO" in presenca("")
    # espaço no fim é causa comum de "senha incorreta" e só aparece na contagem
    assert "ATENÇÃO" in presenca("senha ")
    assert "ATENÇÃO" in presenca(" senha")
    assert "ATENÇÃO" not in presenca("senha")


# ------------------------------------------- o SQL do ensaio contra o esquema
def _rodar_ensaio(dsn: str, *extra: str) -> subprocess.CompletedProcess:
    """Roda o ensaio como processo, contra o Postgres de teste.

    Como subprocesso, e não chamando `main()`, porque é assim que ele vai ser
    usado — e porque `main()` escreve em contador global de falhas.

    `SUPABASE_URL` aponta para uma porta fechada de propósito: o passo do bucket
    tem de falhar **rápido**, já que aqui o que se quer provar é o SQL, não o
    Storage.
    """
    return subprocess.run(
        [sys.executable, "-m", "onstage.ensaio", *extra],
        cwd=Path(__file__).resolve().parent.parent,
        env={
            **os.environ,
            "DATABASE_URL": dsn,
            "SUPABASE_URL": "http://127.0.0.1:1",
            "SUPABASE_SERVICE_ROLE_KEY": "chave-de-teste",
        },
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_ensaio_diz_o_que_fazer_quando_integra_credenciais_esta_vazia(banco):
    """Banco com as migrações aplicadas e nenhuma credencial: o caso do dia 1.

    Vale testar porque é literalmente a primeira coisa que alguém vê, e uma
    mensagem "não encontrei a linha" sem o `insert` ao lado manda a pessoa
    procurar o nome das colunas na migração.
    """
    r = _rodar_ensaio(banco)
    assert "Postgres conectado" in r.stdout
    assert "integra_credenciais está vazia" in r.stdout
    assert "insert into integra_credenciais" in r.stdout      # o remédio junto
    assert r.returncode == 1


def test_ensaio_percorre_todo_o_sql_contra_o_esquema_real(banco):
    """Com a linha de credenciais, o ensaio chega até `v_integra_habilitados`.

    É este o teste que importa: cada `select` do ensaio é escrito à mão contra
    o esquema do portal, e nome de coluna errado só aparece quando roda. Aqui
    roda contra as migrações de verdade.
    """
    with psycopg.connect(banco) as conn:
        conn.execute("delete from integra_credenciais")
        conn.execute(
            """insert into integra_credenciais
                 (id, contratante_cnpj, nome_escritorio, cert_bucket_path,
                  cert_senha_secret, consumer_key_secret, consumer_secret,
                  cert_valido_ate)
               values (1, %s, 'ONSTAGE Soluções Financeiras', 'onstage.pfx',
                       'integra_senha_pfx', 'integra_consumer_key',
                       'integra_consumer_secret', '2027-01-15')""",
            (CNPJ_ON,),
        )
        conn.commit()

    r = _rodar_ensaio(banco)

    # passo 2: leu a linha e mostrou as referências, não os segredos
    assert "ONSTAGE Soluções Financeiras" in r.stdout
    assert CNPJ_ON in r.stdout
    assert "sem procurador PF" in r.stdout
    # passo 6: a view respondeu — é o `select` que eu mais poderia ter errado
    assert "Quem o worker pode consultar" in r.stdout
    assert "v_integra_habilitados está VAZIA" in r.stdout
    # passo 7: sem --chamar, nada de SERPRO
    assert "nenhuma chamada feita" in r.stdout


def test_ensaio_recusa_cnpj_invalido_sem_tocar_na_rede(banco):
    r = _rodar_ensaio(banco, "--chamar", "123")
    assert "CNPJ inválido" in r.stdout
