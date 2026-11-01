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


# ------------------------------------------------------------------ varredura
def test_chamar_e_varrer_sao_mutuamente_exclusivos(banco):
    """Pedir os dois é ambíguo, e o argparse tem de recusar antes de conectar.

    Importa porque a varredura faz ~97 chamadas: um comando ambíguo que o
    programa resolve "escolhendo um" é exatamente o que ninguém quer aqui.
    """
    r = _rodar_ensaio(banco, "--chamar", CNPJ_ON, "--varrer")
    assert r.returncode == 2                      # erro de uso do argparse
    assert "not allowed with argument" in r.stderr


def test_varredura_nao_comeca_sem_o_certificado(banco):
    """Sem `.pfx` aberto não há o que varrer — e o passo 7 tem de dizer isso.

    O teste existe porque o caminho de falha é o caminho comum enquanto os
    segredos não estiverem no Vault, e "não verificado" é a resposta certa ali.
    """
    r = _rodar_ensaio(banco, "--varrer")
    assert "Varredura da carteira pelo indicador gratuito" in r.stdout
    assert "a varredura — o certificado não chegou a abrir" in r.stdout


def test_sql_da_carteira_roda_contra_o_esquema_real(banco):
    """`SQL_CARTEIRA` é escrito à mão contra `clientes` e `certificados_digitais`.

    É a mesma razão dos outros testes de SQL: nome de coluna errado só aparece
    quando roda. Aqui roda, e ainda se confirma que a varredura **não** sai de
    `v_integra_habilitados` — se saísse, não haveria alvo nenhum para descobrir.
    """
    from onstage.ensaio import SQL_CARTEIRA

    assert "v_integra_habilitados" not in SQL_CARTEIRA
    with psycopg.connect(banco) as conn:
        colunas = [d.name for d in conn.execute(SQL_CARTEIRA).description]
    assert colunas == ["cnpj_norm", "razao_social", "ja_cadastrada"]


def test_insert_de_procuracao_sugerido_e_aceito_pela_tabela(banco):
    """O `insert` que a varredura imprime é para colar — então tem de colar.

    Dois acidentes reais que este teste pega: `origem` só aceita 'manual' ou
    'clickup' (`certificados_origem_chk`), e a tabela **não** tem unicidade em
    (cnpj, tipo) — rodar duas vezes duplicaria a procuração e a view devolveria
    o cliente duas vezes. Por isso se roda o mesmo SQL duas vezes aqui.
    """
    sql = """
        insert into certificados_digitais
              (cnpj, tipo, ativo, origem, observacoes)
        select v.cnpj, 'procuracao_ecac', true, 'manual', 'teste'
          from (values ('12345678000199'), ('98765432000111')) as v(cnpj)
         where not exists (select 1 from certificados_digitais cd
                            where cd.cnpj = v.cnpj
                              and cd.tipo = 'procuracao_ecac')
    """
    with psycopg.connect(banco) as conn:
        conn.execute(sql)
        conn.execute(sql)                          # colado duas vezes
        conn.commit()
        n = conn.execute(
            "select count(*) from certificados_digitais where tipo = 'procuracao_ecac'"
        ).fetchone()[0]
    assert n == 2, "o insert sugerido duplicou ao ser rodado de novo"

    # E a restrição de `origem` tem de estar viva no banco de teste, senão o
    # teste acima passaria mesmo com o 'ensaio' que produção recusa.
    with psycopg.connect(banco) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "insert into certificados_digitais (cnpj, tipo, origem) "
                "values ('11111111000111', 'procuracao_ecac', 'ensaio')"
            )


def test_motivo_da_recusa_preserva_o_texto_do_serpro():
    """O 4xx do SERPRO vem com código e frase, e é a frase que resolve o caso.

    Resumir para "deu erro" apagaria a única informação acionável: é "sem
    procuração", "CNPJ baixado" ou "serviço indisponível para este perfil"?
    """
    from integra_serpro.erros import ErroSerpro
    from onstage.ensaio import motivo_da_recusa

    exc = ErroSerpro(403, [{"codigo": "Entrada-Incorreta", "texto": "Procuração não encontrada"}])
    categoria, detalhe = motivo_da_recusa(exc)
    assert categoria == "recusado"
    assert "403" in detalhe
    assert "Procuração não encontrada" in detalhe
    assert "Entrada-Incorreta" in detalhe


def test_erro_de_rede_nao_vira_recusa_de_procuracao():
    """Confundir os dois faria a varredura acusar procuração que está de pé.

    `recusado` é resposta sobre o cliente; `tecnico` é ruído nosso e vale
    repetir. A saída separa as duas listas justamente para não mandar ninguém
    ao e-CAC por causa de um timeout.
    """
    from onstage.ensaio import motivo_da_recusa

    categoria, detalhe = motivo_da_recusa(TimeoutError("conexão expirou"))
    assert categoria == "tecnico"
    assert "TimeoutError" in detalhe
