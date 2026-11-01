"""Ensaio: prova a corrente inteira antes de gastar um centavo.

    python -m onstage.ensaio                   # nada de rede com o SERPRO
    python -m onstage.ensaio --chamar <cnpj>   # uma chamada, e ela é gratuita

Por que isto existe como comando separado do worker: entre "configurei os
segredos" e "o SERPRO respondeu" há seis coisas que podem estar erradas, e
descobrir qual delas é no meio de um job significa ler log de container para
achar uma senha trocada. Pior: o worker, ao falhar, **tenta de novo** — e nos
endpoints pagos cada tentativa é cobrada, inclusive as que voltam 4xx.

O ensaio percorre as mesmas peças na mesma ordem que `ProviderSupabase`, mas
para antes da chamada paga e diz, em uma linha por etapa, onde quebrou.

**Nenhum segredo é impresso.** Para cada um sai só se existe e o tamanho — o
suficiente para distinguir "não cadastrei" de "cadastrei vazio" de "cadastrei
com um espaço no fim", que são os três erros reais, sem transformar o terminal
(e o histórico do shell) num segundo lugar onde a senha do certificado mora.

Sobre o `--chamar`: o alvo é `INNOVAMSG63`, o indicador da caixa postal, que vai
para `/Monitorar` — o **único** endpoint que o SERPRO não cobra. Isso lhe dá uma
propriedade que vale mais do que parece: **até a recusa é de graça.** Se a
procuração e-CAC daquele cliente não estiver valendo, a resposta é 4xx e custa
R$ 0,00 — enquanto a mesma pergunta feita pelo SITFIS custaria R$ 0,32 para
receber o mesmo "não". Logo o indicador é a sonda certa para as duas dúvidas de
uma vez: "a nossa autenticação funciona?" e "a procuração deste cliente está de
pé?".
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timezone

CHECK, FALHA, AVISO, PULO = "[ok]   ", "[FALHA]", "[aviso]", "[-]    "

_falhas = 0
_avisos = 0


def ok(msg: str) -> None:
    print(f"{CHECK} {msg}")


def falha(msg: str) -> None:
    global _falhas
    _falhas += 1
    print(f"{FALHA} {msg}")


def aviso(msg: str) -> None:
    global _avisos
    _avisos += 1
    print(f"{AVISO} {msg}")


def pulo(msg: str) -> None:
    print(f"{PULO} {msg} (não verificado)")


def titulo(msg: str) -> None:
    print(f"\n── {msg} {'─' * max(0, 62 - len(msg))}")


def presenca(valor: str | None) -> str:
    """Descreve um segredo sem revelá-lo.

    O tamanho não é curiosidade: senha colada com espaço no fim é causa comum
    de "senha incorreta" e só aparece na contagem. Por isso o aviso separado.
    """
    if valor is None:
        return "ausente"
    if valor == "":
        return "presente mas VAZIO"
    extra = " — ATENÇÃO: tem espaço no começo ou no fim" if valor != valor.strip() else ""
    return f"{len(valor)} caracteres{extra}"


def so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


def cnpj_do_certificado(titular: str) -> str | None:
    """Extrai o CNPJ do titular de um e-CNPJ A1 da ICP-Brasil.

    O padrão da ICP-Brasil põe `RAZAO SOCIAL:CNPJ` no Common Name, e é o
    **último** `:` que separa — por isso leio dali para a frente em vez de
    varrer a string inteira.

    O atalho de juntar todos os dígitos do titular e pegar os 14 primeiros
    parece equivalente e não é: razão social com número é comum ("EMPRESA 2
    LTDA", "POSTO 24 HORAS", "3M"), e aí o fluxo de dígitos vira
    `2` + CNPJ = 15 dígitos, dos quais os 14 primeiros são um CNPJ que não
    existe. A conferência passaria a acusar certificado trocado em cliente
    nenhum — ou, pior, a deixar passar o certificado errado.

    Sem CNPJ reconhecível devolvo None, e quem chama avisa para conferir à mão.
    Uma comparação que sempre passa é pior que comparação nenhuma.
    """
    if not titular:
        return None
    if ":" in titular:
        cauda = so_digitos(titular.rsplit(":", 1)[1])
        if len(cauda) == 14:
            return cauda
    # Sem `:` (ou com cauda estranha): procuro uma corrida de exatamente 14
    # dígitos. As âncoras impedem recortar 14 de dentro de uma sequência maior.
    achados = re.findall(r"(?<!\d)\d{14}(?!\d)", titular)
    return achados[0] if len(achados) == 1 else None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verifica a configuração do Integra Contador sem gastar chamada paga."
    )
    p.add_argument(
        "--chamar",
        metavar="CNPJ",
        help="faz UMA chamada ao SERPRO: o indicador da caixa postal, que é gratuito.",
    )
    args = p.parse_args()

    print(__doc__.split("\n\n")[0])
    print(f"\nEnsaio em {datetime.now():%d/%m/%Y %H:%M:%S}")

    # ---------------------------------------------------------------- ambiente
    titulo("1. Ambiente")
    faltando = [v for v in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
                if not os.environ.get(v)]
    for v in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        valor = os.environ.get(v)
        if not valor:
            falha(f"{v} não está definida")
        elif v == "SUPABASE_URL":
            ok(f"{v} = {valor}")        # não é segredo: é endereço público
        else:
            ok(f"{v} definida ({presenca(valor)})")
    if faltando:
        print("\nSem essas variáveis não há o que verificar. Veja .env.example.")
        return 1

    import psycopg

    # ------------------------------------------------------------- banco/tabela
    titulo("2. Banco e integra_credenciais")
    try:
        # `autocommit=True` não é detalhe: o ensaio só lê, e precisa continuar
        # lendo **depois** de uma consulta falhar. Numa transação, o primeiro
        # erro (um `vault.decrypted_secrets` inacessível, digamos) aborta o
        # bloco e todo `select` seguinte volta `InFailedSqlTransaction` — o
        # diagnóstico pararia de diagnosticar exatamente quando há o que
        # diagnosticar, e diria "não verificado" onde a resposta existia.
        conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15,
                               autocommit=True)
    except Exception as exc:
        falha(f"não conectei ao Postgres: {type(exc).__name__}: {exc}")
        return 1
    ok("Postgres conectado")

    from .segredos import SQL_CREDENCIAIS, SQL_SEGREDO

    linha = conn.execute(SQL_CREDENCIAIS).fetchone()
    if not linha:
        falha("integra_credenciais está vazia — falta a linha única (id = 1)")
        print(
            "\n  Insira assim (os valores são *nomes* de segredo no Vault, não os segredos):\n"
            "    insert into integra_credenciais (id, contratante_cnpj, nome_escritorio,\n"
            "      cert_bucket_path, cert_senha_secret, consumer_key_secret, consumer_secret,\n"
            "      cert_valido_ate)\n"
            "    values (1, '48744243000160', 'ONSTAGE Soluções Financeiras',\n"
            "      'onstage.pfx', 'integra_senha_pfx', 'integra_consumer_key',\n"
            "      'integra_consumer_secret', '2027-01-15');"
        )
        return 1
    (cnpj_tab, nome, cert_path, senha_ref, ck_ref, cs_ref, valido_ate,
     proc_cpf, proc_nome, proc_senha_ref) = linha
    ok(f"contratante: {nome} — CNPJ {cnpj_tab}")
    ok(f"certificado no bucket: certificados-on/{cert_path}")
    ok(f"referências no Vault: {senha_ref!r}, {ck_ref!r}, {cs_ref!r}")
    if proc_cpf:
        aviso(f"procurador PF configurado: {proc_nome} ({proc_cpf}) — fluxo AUTENTICAPROCURADOR ativo")
    else:
        ok("sem procurador PF (as procurações estão no CNPJ da ON) — é o caso esperado")

    # ------------------------------------------------------------------- Vault
    titulo("3. Supabase Vault")
    segredos: dict[str, str | None] = {}
    for rotulo, ref in (("senha do .pfx", senha_ref), ("consumer key", ck_ref),
                        ("consumer secret", cs_ref)):
        try:
            r = conn.execute(SQL_SEGREDO, (ref,)).fetchone()
            valor = r[0] if r else None
        except Exception as exc:
            falha(f"{rotulo} ({ref!r}): erro ao ler o Vault — {type(exc).__name__}: {exc}")
            segredos[rotulo] = None
            continue
        segredos[rotulo] = valor
        if valor is None:
            falha(f"{rotulo}: segredo {ref!r} não existe no Vault")
        elif not valor:
            falha(f"{rotulo}: segredo {ref!r} existe mas está vazio")
        else:
            desc = presenca(valor)
            (aviso if "ATENÇÃO" in desc else ok)(f"{rotulo} ({ref!r}): {desc}")
    if not all(segredos.values()):
        print(
            "\n  Cadastre o que falta com:\n"
            "    select vault.create_secret('<valor>', '<nome>');"
        )

    # ------------------------------------------------------------------ bucket
    titulo("4. Certificado no bucket privado")
    pfx: bytes | None = None
    if not cert_path:
        falha("cert_bucket_path está vazio na tabela")
    else:
        import httpx

        url = os.environ["SUPABASE_URL"].rstrip("/")
        try:
            r = httpx.get(
                f"{url}/storage/v1/object/certificados-on/{cert_path}",
                headers={"Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_ROLE_KEY']}"},
                timeout=60.0,
            )
        except Exception as exc:
            falha(f"erro de rede ao baixar o .pfx: {type(exc).__name__}: {exc}")
            r = None
        if r is None:
            pass
        elif r.status_code == 200:
            pfx = r.content
            ok(f".pfx baixado: {len(pfx)} bytes")
        elif r.status_code in (400, 404):
            falha(
                f".pfx não encontrado em certificados-on/{cert_path} (HTTP {r.status_code}) — "
                "confira cert_bucket_path, ou suba o arquivo"
            )
        elif r.status_code in (401, 403):
            falha(
                f"acesso negado ao bucket (HTTP {r.status_code}) — "
                "a SUPABASE_SERVICE_ROLE_KEY é mesmo a de service-role? "
                "`certificados-on` não tem policy nenhuma de propósito: só service-role lê."
            )
        else:
            falha(f".pfx não baixado (HTTP {r.status_code})")

    # ------------------------------------------------------- abrir o certificado
    titulo("5. O certificado abre com a senha do Vault?")
    info = None
    senha = segredos.get("senha do .pfx")
    if pfx is None:
        pulo("abertura do .pfx")
        pulo("CNPJ do certificado confere com o da tabela")
        pulo("validade do certificado")
    elif not senha:
        pulo("abertura do .pfx — a senha não veio do Vault")
    else:
        from integra_serpro.certificado import info_certificado

        try:
            info = info_certificado(pfx, senha)
        except Exception as exc:
            falha(
                f"o .pfx NÃO abriu: {exc}. "
                "É senha trocada ou arquivo corrompido — e é a falha mais comum aqui. "
                "Se o consumer secret e a senha foram girados, o .pfx no bucket pode ser o antigo."
            )
        if info:
            ok(f"abriu. Titular: {info.titular}")

            # O CNPJ do certificado tem de ser o do contratante. Subir por engano
            # o A1 de um cliente (são 94 deles no bucket `certificados`) daria um
            # mTLS que o SERPRO aceita como aquele cliente e recusa para todos os
            # outros — sintoma confuso, causa boba.
            cnpj_cert = cnpj_do_certificado(info.titular)
            if cnpj_cert is None:
                aviso(
                    "não achei um CNPJ de 14 dígitos no titular — confira à mão que "
                    f"este certificado é o da ON ({cnpj_tab}) e não o de um cliente"
                )
            elif so_digitos(cnpj_tab) == cnpj_cert:
                ok(f"o CNPJ do certificado é o do contratante ({cnpj_cert})")
            else:
                falha(
                    f"CERTIFICADO DE OUTRO CNPJ: o arquivo é de {cnpj_cert}, "
                    f"a tabela diz {so_digitos(cnpj_tab)}. "
                    "Com o .pfx errado o SERPRO autentica a empresa errada."
                )

            # Validade: o valor real vem do binário. `cert_valido_ate` na tabela
            # é cópia, serve para o portal avisar sem abrir o certificado — e
            # cópia desencontrada é pior que ausente, porque o alerta de 30 dias
            # dispara na data errada.
            dias = (info.valido_ate - datetime.now(timezone.utc)).days
            venc = f"{info.valido_ate:%d/%m/%Y}"
            if dias < 0:
                falha(f"certificado VENCIDO em {venc} — nenhuma chamada vai passar")
            elif dias <= 30:
                aviso(f"certificado vence em {dias} dias ({venc}) — renovação já é urgente")
            elif dias <= 90:
                aviso(f"certificado vence em {dias} dias ({venc}) — agende a renovação")
            else:
                ok(f"certificado válido até {venc} ({dias} dias)")

            if valido_ate and valido_ate != info.valido_ate.date():
                aviso(
                    f"cert_valido_ate na tabela diz {valido_ate:%d/%m/%Y}, mas o certificado "
                    f"vence em {venc}. Corrija a tabela: é dela que sai o alerta de 30/15/7 dias."
                )
            elif valido_ate:
                ok("cert_valido_ate na tabela confere com o certificado")

    # ------------------------------------------------------------- habilitados
    titulo("6. Quem o worker pode consultar")
    total = conn.execute("select count(*) from v_integra_habilitados").fetchone()[0]
    if total == 0:
        aviso(
            "v_integra_habilitados está VAZIA: nenhum cliente tem procuração e-CAC "
            "cadastrada e na validade, então o agendamento não enfileira nada."
        )
        print(
            "    A elegibilidade sai de certificados_digitais.tipo = 'procuracao_ecac'.\n"
            "    Cadastrar a procuração na tela do cliente é o que liga o monitoramento."
        )
    else:
        ok(f"{total} cliente(s) habilitado(s)")
        for c, razao in conn.execute(
            "select cnpj, razao_social from v_integra_habilitados order by razao_social limit 10"
        ):
            print(f"         {c}  {razao}")

    # -------------------------------------------------------- a chamada gratuita
    if not args.chamar:
        titulo("7. Chamada ao SERPRO")
        pulo("nenhuma chamada feita — use --chamar <cnpj> para o indicador gratuito")
    else:
        alvo = so_digitos(args.chamar)
        titulo(f"7. Indicador da caixa postal para {alvo} (gratuito)")
        if len(alvo) != 14:
            falha(f"CNPJ inválido: {args.chamar!r}")
        elif info is None or not pfx:
            pulo("a chamada — o certificado não chegou a abrir")
        else:
            habilitado = conn.execute(
                "select 1 from v_integra_habilitados where cnpj = %s", (alvo,)
            ).fetchone()
            if habilitado:
                ok("este CNPJ está em v_integra_habilitados")
            else:
                aviso(
                    "este CNPJ NÃO está em v_integra_habilitados. Vou chamar mesmo assim: "
                    "em /Monitorar a recusa também é gratuita, e é justamente isso que "
                    "transforma esta chamada num teste da procuração que não custa nada."
                )

            from integra_serpro.catalogo import SERVICOS
            from integra_serpro.client import SerproClient
            from integra_serpro.preco import custo
            from integra_serpro.servicos.caixa_postal import CaixaPostal
            from integra_serpro import Credenciais

            esperado = custo(SERVICOS["caixa_indicador"])
            print(f"         custo previsto desta chamada: R$ {esperado}")

            cred = Credenciais(
                contratante_cnpj=so_digitos(cnpj_tab),
                nome_escritorio=nome,
                pfx=pfx,
                senha_pfx=senha or "",
                consumer_key=segredos.get("consumer key") or "",
                consumer_secret=segredos.get("consumer secret") or "",
            )
            registros: list = []
            try:
                cliente = SerproClient(cred, registrar=registros.append)
                novas = CaixaPostal(cliente).indicador_novas(alvo)
            except Exception as exc:
                falha(f"{type(exc).__name__}: {exc}")
                print(
                    "\n         Como ler esta falha:\n"
                    "         · erro de autenticação → consumer key/secret, ou o par foi girado\n"
                    "           e o Vault ficou com o antigo;\n"
                    "         · erro de TLS/certificado → o .pfx não é o aceito pelo SERPRO;\n"
                    "         · 4xx de autorização → a procuração e-CAC deste CNPJ não está de pé\n"
                    "           para a ON. Esta é a resposta que estávamos testando, e ela foi\n"
                    "           de graça.\n"
                    "         Em nenhum destes casos houve cobrança: /Monitorar é gratuito."
                )
            else:
                ok(f"o SERPRO respondeu. Mensagens novas na caixa postal: {novas}")
                print(
                    "\n         Isto prova, de uma vez: consumer key/secret válidos, mTLS com o\n"
                    "         e-CNPJ da ON aceito, envelope no formato certo e — se o CNPJ não é\n"
                    "         o da própria ON — a procuração e-CAC valendo. Custou R$ 0,00."
                )
            if registros:
                r = registros[-1]
                print(f"         registro da chamada: {r}")

    conn.close()

    # ----------------------------------------------------------------- resumo
    titulo("Resumo")
    falhas = f"{_falhas} falha" + ("s" if _falhas != 1 else "")
    avisos = f"{_avisos} aviso" + ("s" if _avisos != 1 else "")
    if _falhas:
        print(f"{falhas} e {avisos}. O worker não vai funcionar assim.")
        return 1
    if _avisos:
        print(f"Nenhuma falha e {avisos}. Dá para rodar; leia os avisos.")
        return 0
    print("Tudo verificado, sem falha nem aviso.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
