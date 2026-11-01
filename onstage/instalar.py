"""Instala as credenciais: Vault, bucket e `integra_credenciais`, de uma vez.

    python -m onstage.instalar ~/Downloads/48744243000160.pfx

Pede os três segredos sem eco, confere o certificado **antes** de gravar nada e
só então escreve. Depois disso, `python -m onstage.ensaio` tem o que verificar.

Por que isto é um comando e não quatro passos no painel do Supabase:

- **Segredo digitado no editor de SQL fica no histórico do editor.** Rodar
  `select vault.create_secret('<a senha>', 'integra_senha_pfx')` no painel grava
  a senha, em texto puro, numa lista que persiste e que qualquer pessoa com
  acesso ao dashboard abre depois. O mesmo vale para o shell: segredo em `argv`
  entra no histórico e aparece em `ps` para outros usuários da máquina. Aqui os
  três entram por `getpass`, que não ecoa e não guarda. Seria irônico vazar por
  descuido nosso a credencial que estamos tirando de um `config.json` em texto
  puro.
- **`cert_valido_ate` é lido do binário, nunca digitado.** A coluna é cópia do
  que está dentro do `.pfx`; existe para o portal avisar do vencimento sem abrir
  o certificado. Cópia digitada erra, e cópia que discorda é pior que ausente:
  o alerta de 30/15/7 dias passa a disparar no dia errado, que é precisamente o
  dia em que ele não serve para nada.
- **Nada é gravado antes de o `.pfx` abrir.** Senha errada é o erro mais comum
  deste cadastro. Validando primeiro, a senha errada não deixa um Vault com dois
  segredos certos e um errado — estado meio-configurado que depois se diagnostica
  lendo log.

O comando é idempotente: rodar de novo atualiza em vez de duplicar. Isso importa
porque a razão mais provável para rodá-lo duas vezes é justamente ter girado o
consumer secret ou a senha do certificado.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .ensaio import cnpj_do_certificado, presenca, so_digitos

# Os nomes dos segredos no Vault. São referência, não segredo: quem lê
# `integra_credenciais` só descobre como o segredo se chama.
SENHA_REF = "integra_senha_pfx"
CHAVE_REF = "integra_consumer_key"
SEGREDO_REF = "integra_consumer_secret"

CNPJ_ON = "48744243000160"
NOME_ON = "ONSTAGE Soluções Financeiras"


def pedir(rotulo: str, variavel: str) -> str:
    """Lê um segredo de variável de ambiente ou, na falta, do terminal sem eco.

    A variável de ambiente existe para quem automatiza; o `getpass` é o caminho
    de uma pessoa no terminal. Em nenhum dos dois o valor passa por `argv`.
    """
    valor = os.environ.get(variavel)
    if valor:
        print(f"  {rotulo}: lido de {variavel} ({presenca(valor)})")
        return valor
    valor = getpass.getpass(f"  {rotulo} (não aparece na tela): ")
    # Não dou `strip()` por conta própria: se a senha do certificado tiver
    # espaço de verdade, apagá-lo em silêncio faz o `.pfx` não abrir e manda a
    # pessoa procurar o erro em todo lugar menos aqui. Aviso e deixo escolher.
    if valor != valor.strip():
        print(f"      ATENÇÃO: tem espaço no começo ou no fim ({presenca(valor)}).")
        if input("      Era isso mesmo? [s/N] ").strip().lower() != "s":
            valor = valor.strip()
            print(f"      ok, usando sem os espaços ({presenca(valor)}).")
    return valor


def gravar_segredo(conn, nome: str, valor: str) -> str:
    """Cria o segredo no Vault, ou atualiza se o nome já existir.

    `vault.create_secret` estoura em nome repetido, e o caso de rodar duas vezes
    é o caso normal aqui (girou o consumer secret, roda de novo). Por isso a
    busca por nome antes.
    """
    atual = conn.execute("select id from vault.secrets where name = %s", (nome,)).fetchone()
    if atual:
        conn.execute("select vault.update_secret(%s, %s)", (atual[0], valor))
        return "atualizado"
    conn.execute("select vault.create_secret(%s, %s)", (valor, nome))
    return "criado"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Grava as credenciais do Integra Contador no Vault, no bucket "
                    "e em integra_credenciais. Os segredos são pedidos sem eco."
    )
    p.add_argument("pfx", type=Path, help="caminho do e-CNPJ A1 da ON (.pfx)")
    p.add_argument("--cnpj", default=CNPJ_ON, help=f"CNPJ do contratante (padrão: {CNPJ_ON})")
    p.add_argument("--nome", default=NOME_ON, help="nome do escritório")
    p.add_argument("--caminho-bucket", default="onstage.pfx",
                   help="nome do arquivo em certificados-on (padrão: onstage.pfx)")
    args = p.parse_args()

    faltando = [v for v in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
                if not os.environ.get(v)]
    if faltando:
        print(f"Falta definir: {', '.join(faltando)}. Veja .env.example.")
        return 1

    if not args.pfx.is_file():
        print(f"Não achei o arquivo: {args.pfx}")
        return 1
    pfx = args.pfx.read_bytes()
    print(f"\nCertificado: {args.pfx}  ({len(pfx)} bytes)")

    # ------------------------------------------------- os segredos, sem eco
    print("\nOs três segredos (nada aparece na tela, nada entra no histórico):")
    senha = pedir("senha do .pfx", "INTEGRA_SENHA_PFX")
    chave = pedir("consumer key", "INTEGRA_CONSUMER_KEY")
    segredo = pedir("consumer secret", "INTEGRA_CONSUMER_SECRET")
    if not (senha and chave and segredo):
        print("\nAlgum dos três veio vazio. Nada foi gravado.")
        return 1

    # ------------------------------- conferir ANTES de escrever em qualquer lugar
    from integra_serpro.certificado import info_certificado

    print("\nConferindo o certificado antes de gravar:")
    try:
        info = info_certificado(pfx, senha)
    except Exception as exc:
        print(f"  o .pfx NÃO abriu com essa senha: {exc}")
        print("  Nada foi gravado. Confira a senha e rode de novo.")
        return 1
    print(f"  abriu. Titular: {info.titular}")

    cnpj_cert = cnpj_do_certificado(info.titular)
    cnpj_alvo = so_digitos(args.cnpj)
    if cnpj_cert and cnpj_cert != cnpj_alvo:
        print(
            f"  CERTIFICADO DE OUTRO CNPJ: o arquivo é de {cnpj_cert}, você pediu "
            f"{cnpj_alvo}.\n"
            "  Nada foi gravado. Há 94 certificados de clientes no bucket "
            "`certificados`;\n"
            "  subir um deles aqui daria um mTLS que o SERPRO aceita como aquele "
            "cliente\n  e recusa para todos os outros."
        )
        return 1
    if cnpj_cert is None:
        print(f"  aviso: não achei CNPJ no titular — confira à mão que é o da ON ({cnpj_alvo})")
    else:
        print(f"  é o CNPJ do contratante ({cnpj_cert})")

    from datetime import datetime, timezone

    dias = (info.valido_ate - datetime.now(timezone.utc)).days
    print(f"  válido até {info.valido_ate:%d/%m/%Y} ({dias} dias)")
    if dias < 0:
        print("  VENCIDO. Nada foi gravado: nenhuma chamada passaria.")
        return 1
    if dias <= 90:
        print("  atenção: renovação já deveria estar encaminhada.")

    import httpx
    import psycopg

    # ------------------------------------------------------------- o bucket
    url = os.environ["SUPABASE_URL"].rstrip("/")
    chave_servico = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    destino = f"{url}/storage/v1/object/certificados-on/{args.caminho_bucket}"
    print(f"\nSubindo para certificados-on/{args.caminho_bucket}:")
    try:
        r = httpx.post(
            destino,
            headers={
                "Authorization": f"Bearer {chave_servico}",
                "Content-Type": "application/x-pkcs12",
                # `upsert` porque trocar o certificado (renovação, giro de senha) é o
                # motivo normal de rodar isto de novo, e falhar com "já existe"
                # obrigaria a apagar à mão antes.
                "x-upsert": "true",
            },
            content=pfx,
            timeout=120.0,
        )
    except Exception as exc:
        # Sem o `except`, uma `SUPABASE_URL` errada despeja traceback de httpx
        # numa tela que até aqui vinha dizendo exatamente onde estava o problema.
        # Pior: a última linha impressa seria "Subindo para...", o que faz
        # parecer que o arquivo subiu.
        print(f"  erro de rede: {type(exc).__name__}: {exc}")
        print(f"  Confira SUPABASE_URL ({url}). Nada foi gravado.")
        return 1
    if r.status_code not in (200, 201):
        print(f"  falhou (HTTP {r.status_code}): {r.text[:300]}")
        if r.status_code in (401, 403):
            print("  A SUPABASE_SERVICE_ROLE_KEY é mesmo a de service-role? "
                  "`certificados-on` não tem policy nenhuma de propósito.")
        print("  Nada foi gravado no Vault nem na tabela.")
        return 1
    print("  ok")

    # -------------------------------------------------------- Vault e tabela
    try:
        conn_ctx = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    except Exception as exc:
        print(f"\nnão conectei ao Postgres: {type(exc).__name__}: {exc}")
        print("O .pfx subiu, mas o Vault e a tabela não foram escritos. "
              "Corrija o DATABASE_URL e rode de novo: o comando é idempotente.")
        return 1
    with conn_ctx as conn:
        print("\nVault:")
        for rotulo, ref, valor in (("senha do .pfx", SENHA_REF, senha),
                                   ("consumer key", CHAVE_REF, chave),
                                   ("consumer secret", SEGREDO_REF, segredo)):
            acao = gravar_segredo(conn, ref, valor)
            print(f"  {ref}: {acao} ({presenca(valor)})")

        print("\nintegra_credenciais:")
        # `cert_valido_ate` sai de `info`, não de parâmetro: é o único jeito de a
        # cópia na tabela não poder discordar do binário.
        conn.execute(
            """insert into integra_credenciais
                 (id, contratante_cnpj, nome_escritorio, cert_bucket_path,
                  cert_senha_secret, consumer_key_secret, consumer_secret,
                  cert_valido_ate)
               values (1, %s, %s, %s, %s, %s, %s, %s)
               on conflict (id) do update set
                 contratante_cnpj    = excluded.contratante_cnpj,
                 nome_escritorio     = excluded.nome_escritorio,
                 cert_bucket_path    = excluded.cert_bucket_path,
                 cert_senha_secret   = excluded.cert_senha_secret,
                 consumer_key_secret = excluded.consumer_key_secret,
                 consumer_secret     = excluded.consumer_secret,
                 cert_valido_ate     = excluded.cert_valido_ate""",
            (cnpj_alvo, args.nome, args.caminho_bucket, SENHA_REF, CHAVE_REF,
             SEGREDO_REF, info.valido_ate.date()),
        )
        conn.commit()
        print(f"  linha 1 gravada: {args.nome} — CNPJ {cnpj_alvo}")
        print(f"  cert_valido_ate = {info.valido_ate:%d/%m/%Y} (lido do certificado)")

    print(
        "\nPronto. Agora, nesta ordem:\n"
        "  python -m onstage.ensaio                      # confere tudo, sem rede\n"
        f"  python -m onstage.ensaio --chamar {cnpj_alvo}  # a ON consultando a si mesma\n"
        "  python -m onstage.ensaio --chamar <cnpj-de-um-cliente>\n"
        "\nOs três custam R$ 0,00: o indicador da caixa postal é o único endpoint\n"
        "que o SERPRO não cobra, e nele até a recusa é de graça."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
