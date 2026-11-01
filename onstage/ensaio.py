"""Ensaio: prova a corrente inteira antes de gastar um centavo.

    python -m onstage.ensaio                   # nada de rede com o SERPRO
    python -m onstage.ensaio --chamar <cnpj>   # uma chamada, e ela é gratuita
    python -m onstage.ensaio --varrer          # a carteira toda, e também de graça

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

E se a recusa é gratuita, perguntar por **todos** também é. É o que `--varrer`
faz: passa a carteira ativa inteira pelo indicador e separa quem respondeu de
quem foi recusado. Isso responde "quais empresas estão no trilho do Integra
Contador?" pela única fonte que decide a questão — a Receita — em vez de inferir
de uma configuração nossa em outro sistema. Configuração diz o que alguém quis;
a resposta do SERPRO diz o que vale hoje. Custo da varredura: R$ 0,00, e o
ensaio soma os custos registrados no fim para provar isso em vez de prometer.

Efeito colateral útil: a saída do `--varrer` é exatamente a lista de
`procuracao_ecac` que falta em `certificados_digitais` (hoje a tabela tem 94
linhas e **todas** são A1, nenhuma procuração), então ela serve de base para o
cadastro. O ensaio imprime o `insert` e não o executa: registrar procuração é
ato de cadastro, e cadastro tem dono.
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


def motivo_da_recusa(exc: Exception) -> tuple[str, str]:
    """Traduz a exceção de uma chamada do indicador em (categoria, detalhe).

    A categoria importa porque muda o que fazer. `recusado` é resposta sobre
    **aquele** cliente — a procuração não está de pé, e seguir para o próximo é
    o certo. `tecnico` é ruído de rede, que pode acertar só uma linha. Já
    `FalhaAutenticacao` não é sobre cliente nenhum: é a nossa credencial, e
    quem recebe isso na primeira chamada receberia nas 96 seguintes. Por isso
    ela não aparece aqui — a varredura a trata como fatal e para.

    O `texto` que o SERPRO manda junto do 4xx é o que distingue "sem procuração"
    de "CNPJ baixado" de "serviço indisponível para este perfil". Guardo-o
    inteiro em vez de resumir para "deu erro": é a frase que a pessoa vai levar
    para o e-CAC conferir.
    """
    from integra_serpro.erros import ErroSerpro

    if isinstance(exc, ErroSerpro):
        codigos = "; ".join(
            f"{m.get('codigo', '')} {m.get('texto', '')}".strip()
            for m in exc.mensagens
        ).strip()
        return "recusado", f"HTTP {exc.status_http}" + (f" — {codigos}" if codigos else "")
    return "tecnico", f"{type(exc).__name__}: {exc}"


SQL_CARTEIRA = """
select c.cnpj_norm,
       c.razao_social,
       exists (select 1 from certificados_digitais cd
                where cd.cnpj = c.cnpj_norm
                  and cd.tipo = 'procuracao_ecac'
                  and cd.ativo) as ja_cadastrada
  from clientes c
 where c.ativo
   and length(regexp_replace(c.cnpj_norm, '\\D', '', 'g')) = 14
 order by c.razao_social
"""


def varrer(conn, cnpj_tab: str, nome: str, pfx: bytes, senha: str,
           segredos: dict, pausa: float) -> None:
    """Pergunta ao SERPRO, por toda a carteira ativa, quem está no trilho.

    A lista de alvos vem de `clientes`, **não** de `v_integra_habilitados`:
    aquela view exige a linha de procuração em `certificados_digitais`, que é
    justamente o que esta varredura existe para descobrir. Ler a view aqui daria
    zero alvos e a conclusão falsa de que ninguém tem procuração.

    O CNPJ da própria ON é tratado à parte porque responde por outro motivo: o
    contratante consulta a si mesmo direto pelo e-CNPJ, sem procuração no meio.
    Um "ok" ali prova o encanamento; um "ok" num cliente prova a procuração.
    Misturar os dois na mesma contagem faria a varredura parecer um sucesso
    parcial quando o que funcionou foi só a autenticação.
    """
    import time

    from integra_serpro.catalogo import SERVICOS
    from integra_serpro.client import SerproClient
    from integra_serpro.erros import AindaProcessando, FalhaAutenticacao
    from integra_serpro.preco import custo
    from integra_serpro.servicos.caixa_postal import CaixaPostal
    from integra_serpro import Credenciais

    alvos = conn.execute(SQL_CARTEIRA).fetchall()
    if not alvos:
        falha("nenhum cliente ativo com CNPJ de 14 dígitos em `clientes`")
        return

    contratante = so_digitos(cnpj_tab)
    esperado = custo(SERVICOS["caixa_indicador"])
    print(f"         {len(alvos)} cliente(s) ativo(s) na carteira")
    print(f"         custo previsto por chamada: R$ {esperado} — "
          f"total previsto: R$ {esperado * len(alvos)}")
    if esperado != 0:
        falha(
            f"ABORTADO: o indicador deveria ser gratuito e o catálogo diz R$ {esperado}. "
            "Varrer 90+ clientes a esse preço não é teste, é conta. "
            "Confira `GRATUITOS` em integra_serpro/preco.py antes de insistir."
        )
        return
    print(f"         pausa entre chamadas: {pausa:g}s\n")

    # Os registros ficam em memória de propósito. `integra_chamadas` é o livro
    # de **gasto** do worker; lançar ali 97 chamadas de R$ 0,00 feitas à mão por
    # uma pessoa no terminal suja a série que o painel de custos usa. O que a
    # varredura precisa provar sobre dinheiro ela prova somando o que registrou.
    registros: list = []
    try:
        cred = Credenciais(
            contratante_cnpj=contratante,
            nome_escritorio=nome,
            pfx=pfx,
            senha_pfx=senha,
            consumer_key=segredos.get("consumer key") or "",
            consumer_secret=segredos.get("consumer secret") or "",
        )
        cliente = SerproClient(cred, registrar=registros.append)
        caixa = CaixaPostal(cliente)
    except Exception as exc:
        falha(f"não consegui montar o cliente do SERPRO: {type(exc).__name__}: {exc}")
        return

    de_pe: list[tuple[str, str, int, bool]] = []
    recusados: list[tuple[str, str, str]] = []
    tecnicos: list[tuple[str, str, str]] = []
    interrompida = ""

    for i, (cnpj, razao, ja_cadastrada) in enumerate(alvos, 1):
        alvo = so_digitos(cnpj)
        rotulo = f"[{i:>3}/{len(alvos)}] {alvo}  {(razao or '')[:38]:<38}"
        try:
            novas = caixa.indicador_novas(alvo)
        except AindaProcessando:
            # 204 no indicador: autorizado, sem nada a relatar. Para a pergunta
            # desta varredura — "a procuração está de pé?" — é um sim.
            de_pe.append((alvo, razao, 0, ja_cadastrada))
            print(f"{rotulo} ok (sem mensagens)")
        except FalhaAutenticacao as exc:
            # Não é sobre este cliente: é a nossa credencial. Seguir faria 96
            # chamadas para colecionar a mesma frase.
            interrompida = f"{type(exc).__name__}: {exc}"
            print(f"{rotulo} FALHA DE AUTENTICAÇÃO")
            break
        except Exception as exc:
            categoria, detalhe = motivo_da_recusa(exc)
            (recusados if categoria == "recusado" else tecnicos).append(
                (alvo, razao, detalhe)
            )
            print(f"{rotulo} {'recusado' if categoria == 'recusado' else 'erro'}: {detalhe[:60]}")
        else:
            de_pe.append((alvo, razao, novas, ja_cadastrada))
            print(f"{rotulo} ok — {novas} mensagem(ns) nova(s)")
        if i < len(alvos) and pausa > 0:
            time.sleep(pausa)

    # ------------------------------------------------------------------ leitura
    print()
    if interrompida:
        falha(
            f"varredura interrompida na {len(de_pe) + len(recusados) + len(tecnicos) + 1}ª "
            f"chamada: {interrompida}. "
            "Isto é credencial nossa, não procuração de cliente — o resultado parcial "
            "acima não diz nada sobre quem está ou não no trilho."
        )
    gasto = sum(r.custo_estimado or 0 for r in registros)
    print(f"         {len(registros)} chamada(s) registrada(s), custo somado: R$ {gasto}")
    if gasto != 0:
        falha(f"a varredura custou R$ {gasto} — era para ser gratuita. Investigue antes de repetir.")

    proprio = [t for t in de_pe if t[0] == contratante]
    clientes_ok = [t for t in de_pe if t[0] != contratante]
    if proprio:
        ok(f"a própria ON respondeu ({contratante}) — encanamento de pé: "
           "consumer key/secret, mTLS e envelope. Sem procuração no meio.")

    if clientes_ok:
        ok(f"{len(clientes_ok)} cliente(s) com procuração e-CAC de pé hoje:")
        for cnpj, razao, novas, ja in clientes_ok:
            marca = "" if ja else "   ← falta cadastrar em certificados_digitais"
            print(f"           {cnpj}  {razao}  ({novas} msg){marca}")
    elif not interrompida:
        aviso(
            "nenhum cliente respondeu. Se o CNPJ da ON respondeu e os clientes não, "
            "a autenticação está certa e o que falta são as procurações no e-CAC — "
            "o que é um trabalho de cadastro na Receita, não de código."
        )

    if recusados:
        aviso(f"{len(recusados)} recusado(s) pelo SERPRO — leia o motivo de cada um:")
        for cnpj, razao, detalhe in recusados:
            print(f"           {cnpj}  {(razao or '')[:34]:<34} {detalhe}")
    if tecnicos:
        aviso(f"{len(tecnicos)} com erro técnico (vale repetir só estes):")
        for cnpj, razao, detalhe in tecnicos:
            print(f"           {cnpj}  {(razao or '')[:34]:<34} {detalhe}")

    # -------------------------------------------------- o cadastro que falta
    faltam = [(c, r) for c, r, _, ja in clientes_ok if not ja]
    if faltam:
        print(
            f"\n         {len(faltam)} cliente(s) respondem no e-CAC mas não têm a linha de\n"
            "         procuração no portal — e sem ela `v_integra_habilitados` não os\n"
            "         devolve, logo o agendamento não os enfileira. Para cadastrar:\n"
        )
        valores = ",\n                      ".join(f"('{c}')" for c, _ in faltam)
        # `origem` aceita só 'manual' ou 'clickup' (certificados_origem_chk), e
        # 'manual' é o rótulo honesto: o ensaio não é via de ingestão, é uma
        # pessoa decidindo depois de ler a saída. A procedência fica em
        # `observacoes`, que é texto livre.
        #
        # O `where not exists` existe porque a tabela não tem unicidade em
        # (cnpj, tipo): este é SQL para colar, e SQL para colar é colado duas
        # vezes. Sem a guarda, a segunda vez dá duas procurações para o mesmo
        # cliente e a view passa a devolvê-lo em duplicata.
        print(
            "           insert into certificados_digitais\n"
            "                 (cnpj, tipo, ativo, origem, observacoes)\n"
            "           select v.cnpj, 'procuracao_ecac', true, 'manual',\n"
            f"                  'procuração confirmada no e-CAC pela varredura de "
            f"{date.today():%d/%m/%Y}'\n"
            f"             from (values\n                      {valores}\n"
            "                  ) as v(cnpj)\n"
            "            where not exists (select 1 from certificados_digitais cd\n"
            "                               where cd.cnpj = v.cnpj\n"
            "                                 and cd.tipo = 'procuracao_ecac');\n"
        )
        print(
            "         `data_vencimento` fica nula porque /Monitorar não devolve o prazo\n"
            "         da procuração, e inventar data seria pior que não ter: o alerta de\n"
            "         30 dias passaria a disparar na data errada. Nulo a view aceita, e\n"
            "         quem vigia o vencimento na prática é esta mesma chamada gratuita,\n"
            "         que passa a recusar no dia em que a procuração morrer."
        )


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verifica a configuração do Integra Contador sem gastar chamada paga."
    )
    alvo_grupo = p.add_mutually_exclusive_group()
    alvo_grupo.add_argument(
        "--chamar",
        metavar="CNPJ",
        help="faz UMA chamada ao SERPRO: o indicador da caixa postal, que é gratuito.",
    )
    alvo_grupo.add_argument(
        "--varrer",
        action="store_true",
        help="passa a carteira ativa inteira pelo indicador gratuito e diz quem "
             "tem procuração e-CAC de pé. Também custa R$ 0,00.",
    )
    p.add_argument(
        "--pausa",
        type=float,
        default=1.0,
        metavar="SEG",
        help="espera entre as chamadas da varredura (padrão: 1,0 s). É educação "
             "com a Receita, não exigência nossa.",
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
    if not args.chamar and not args.varrer:
        titulo("7. Chamada ao SERPRO")
        pulo("nenhuma chamada feita — use --chamar <cnpj> para o indicador gratuito, "
             "ou --varrer para a carteira toda")
    elif args.varrer:
        titulo("7. Varredura da carteira pelo indicador gratuito")
        if info is None or not pfx:
            pulo("a varredura — o certificado não chegou a abrir")
        else:
            varrer(conn, cnpj_tab, nome, pfx, senha or "", segredos, args.pausa)
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
