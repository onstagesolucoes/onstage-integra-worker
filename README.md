# onstage-integra-worker

Worker do **Integra Contador (SERPRO)** do portal ON 360. É o único processo da
casa que carrega o e-CNPJ A1 em memória e o único que fala com o SERPRO.

Existe como repositório separado por um motivo só: o raio de alcance de um
vazamento. Dentro do `onstage-apis`, qualquer deploy passaria a carregar o
certificado que autentica a ON junto à Receita Federal para 97 CNPJs. Aqui, o
que vaza é este container.

```
portal (server action)  ─┐
                         ├─→ insert em integra_jobs ─→ worker ─→ SERPRO (mTLS)
n8n (3 agendamentos)    ─┘                              │
                                                        ├─→ tabelas integra_*
                                                        └─→ Storage (PDFs)
portal ←── lê views ────────────────────────────────────┘
```

O portal e o n8n **só fazem INSERT em `integra_jobs`**. Nenhum dos dois fala com
o SERPRO, nenhum dos dois vê o certificado.

## O que este worker não faz, de propósito

| O executável comprado (`IntegraContadorDesktop.exe`) | Aqui |
| --- | --- |
| Flask em `0.0.0.0:5847`, sem autenticação | **nenhuma porta aberta** |
| `GET /api/settings` devolvia senha do `.pfx` e consumer secret em texto puro | nenhuma rota, logo nenhuma rota que devolva segredo |
| CORS liberado | não se aplica |
| Health check por HTTP | heartbeat em `integra_worker_pulso` |
| Monousuário, roda na máquina de alguém | 2 réplicas no Railway, fila com `skip locked` |

O `ParaUsaremRede.zip` que acompanhava o produto tem um `config.json` apontando
para `http://192.168.1.108:5847` — ou seja, aquilo **rodou exposto na rede do
escritório**. Health check por porta foi exatamente o que tornou o produto
inseguro; por isso a saúde aqui é publicada em tabela, e o painel
`/admin/integra` calcula "vivo" pela idade de `visto_em`.

## Estrutura

```
integra_serpro/      protocolo do SERPRO: envelope, mTLS, catálogo de 30
                     serviços, procurador XMLDSig, preços, fila e worker.
                     Testável sem rede e sem banco.
onstage/             cola com a infraestrutura desta casa: Vault, Storage,
                     entrypoint. Só o que não precisa de rede entra em teste.
tests/               92 testes. O SERPRO é simulado; o Postgres é real.
```

A separação é o que permite mexer num lado só: quando o SERPRO mudar um
envelope, muda `integra_serpro`; quando trocarmos de cofre, muda `onstage`.

**O SQL não vive aqui.** As tabelas são do portal e as migrações estão em
`portal-360/supabase/migrations/20261101001600..002000`. Duas fontes de verdade
para o mesmo esquema é como se chega a um `insert` que funciona no teste e
quebra em produção.

## Tipos de job

Só três, e isso é deliberado:

| `tipo` | Chamadas | Cadência |
| --- | --- | --- |
| `caixa_postal` | indicador **gratuito**; a lista paga só se ele acusar novidade | diária, todos |
| `sitfis` | protocolo (`/Apoiar`) + relatório (`/Emitir`) | semanal, escalonado por `hash(cnpj) % 5` |
| `parcelamentos` | pedidos + parcelas, só nas modalidades que o Nibo já indica | semanal |

**Guias DAS/DARF, pagamentos e MIT ficam fora do dicionário `HANDLERS`.** Os
serviços estão implementados e testados em `integra_serpro/servicos/`, mas não há
handler registrado porque as tabelas de destino ainda não existem no portal.
Handler registrado sem tabela é a falha mais cara possível deste módulo: o SERPRO
é chamado, **a chamada é paga**, e só então o `insert` estoura em `relation does
not exist` — e como isso não é erro definitivo, a fila tenta de novo, pagando de
novo. Fora do dicionário, o job morre num `ValueError` antes de abrir conexão.

## Quem pode ser consultado

O mTLS prova **só quem é a ON**. O acesso ao dado de cada cliente vem da
**procuração e-CAC** registrada na Receita, que o SERPRO valida do lado dele —
não há senha de cliente em lugar nenhum, e não precisa haver.

Logo a lista de habilitados não é cadastro novo: é
`certificados_digitais.tipo = 'procuracao_ecac'`, exposta em
`v_integra_habilitados`. Cadastrar a procuração na tela do cliente **é** ligar o
monitoramento. A view filtra por `ativo` e por `data_vencimento`, e o worker
recusa job de quem não está nela — consultar sem procuração devolve 4xx, e 4xx
é cobrado igual.

Quando a procuração está no **CPF de um procurador** em vez do CNPJ do
escritório, entra o fluxo `AUTENTICAPROCURADOR`: termo de autorização assinado
em XMLDSig com o `.pfx` do procurador, token reaproveitado no header das
chamadas seguintes (`integra_serpro/procurador.py`).

## Dinheiro

Toda requisição ao SERPRO é cobrada (R$ 0,24→0,21 consulta; R$ 0,32→0,29
emissão, com faixas em 300 e 500 chamadas/mês). A primeira defesa é a procuração
acima, porque ela evita a chamada inteira. As outras quatro, nesta ordem:

1. **A ordem das chamadas.** `INNOVAMSG63` (indicador da caixa postal) vai para
   `/Monitorar` e é gratuito — roda todo dia para os 97 clientes. A lista
   `MSGCONTRIBUINTE61` é `/Consultar`, paga, e só sai quando o indicador disse
   que há mensagem nova. Inverter isso multiplica a conta.
2. **`integra_estado_cliente`.** Protocolo de SITFIS comprado é reaproveitado;
   data de última consulta governa a cadência. Escrito no **mesmo commit** do
   dado — se o PDF entrar e a data não, a rodada seguinte recompra o relatório.
3. **Teto diário** (`integra_credenciais.teto_diario`, R$ 60). O jeito de o custo
   fugir não é alguém clicar demais: é job em laço de nova tentativa gastando a
   noite inteira sem ninguém olhando.
4. **`integra_chamadas` em conexão própria, em autocommit.** Se o handler falhar
   e a transação do job for desfeita, a chamada já aconteceu e já foi cobrada;
   registrá-la na mesma transação faria o rollback apagar a prova do gasto.

O teto é lido **direto das tabelas**, nunca de `v_integra_teto_hoje`: a view
termina em `where public.eh_interno()`, que depende de `auth.uid()`, e o worker
conecta sem JWT — para ele a view devolve zero linha. Ler dali e tratar "sem
linha" como "sem teto" deixaria o limite decorativo.

## Segredos

Nada de segredo neste repositório e nada de segredo em variável de ambiente além
do endereço e da chave para alcançá-los:

- `consumer_key`, `consumer_secret` e a senha do `.pfx` → **Supabase Vault**
  (`vault.decrypted_secrets`), referenciados por nome em `integra_credenciais`.
- o `.pfx` (binário) → bucket privado **`certificados-on`**, lido só pelo
  service-role.
- PDFs gerados → bucket privado **`integra`**, layout
  `{cnpj}/{tipo}/{aaaamm}/{id}.pdf`. `StorageSupabase.salvar()` devolve o
  **caminho, nunca uma URL** — link assinado de 5 min é gerado no portal, na
  hora de exibir. URL de longa duração em coluna de banco é vazamento com data
  marcada.

`.gitignore` bloqueia `*.pfx`, `*.p12`, `*.pem`, `*.key`, `.env` e `config.json`.

## Rodar

```bash
pip install -e ".[dev]"
pytest                                   # 92 testes, ~4 s
```

Os testes do worker precisam de Postgres de verdade — são SQL (`for update skip
locked`, upsert em índice parcial, CHECK de situação, view com `eh_interno()`), e
nada disso tem como ser simulado. Duas formas, nesta ordem:

1. `INTEGRA_TEST_DSN=postgresql://...` aponta para um Postgres seu;
2. sem isso, `pgserver` sobe um **PostgreSQL 17 embutido** — sem container, sem
   privilégio administrativo.

Em qualquer das duas, `tests/conftest.py` derruba o schema `public`, cria os
quatro objetos do portal que as migrações pressupõem (`tests/esquema_portal.sql`:
`clientes`, `certificados_digitais`, `parcelamentos_fiscais`, `eh_interno()`) e
**aplica as migrações reais do portal**, lidas de
`../portal-360/supabase/migrations` (ou de `PORTAL_MIGRATIONS`). Rodar as
migrações de verdade contra um banco vazio é, por si só, a verificação de que
elas aplicam limpo e são auto-consistentes.

O stub de `eh_interno()` devolve **false** de propósito: é assim que o teste
reproduz a condição real do worker, para quem a view do teto não existe. Um stub
que devolvesse true esconderia o defeito.

### Ensaio: a configuração está de pé?

```bash
python -m onstage.ensaio                   # nada de rede com o SERPRO
python -m onstage.ensaio --chamar <cnpj>   # uma chamada, e ela é gratuita
python -m onstage.ensaio --varrer          # a carteira toda, e também de graça
```

`pytest` prova o código; o ensaio prova **esta instalação**. Entre "cadastrei os
segredos" e "o SERPRO respondeu" há seis coisas que podem estar erradas, e
descobrir qual no meio de um job significa ler log de container para achar uma
senha com espaço no fim. Pior: o worker, ao falhar, **tenta de novo** — e nos
endpoints pagos cada tentativa é cobrada, inclusive as que voltam 4xx.

Sete passos, na ordem em que `ProviderSupabase` os percorre: variáveis de
ambiente, `integra_credenciais`, os três segredos no Vault, o `.pfx` no bucket,
abrir o `.pfx` com a senha, `v_integra_habilitados` e — só com `--chamar` — o
SERPRO. Nenhum segredo é impresso: de cada um sai se existe e o tamanho, o
suficiente para separar "não cadastrei" de "cadastrei vazio" de "cadastrei com
um espaço no fim", que são os três erros reais.

Dois detalhes que são o ponto do comando, não implementação:

- **A conexão é `autocommit`.** O ensaio só lê, e tem de continuar lendo depois
  de um `select` falhar: numa transação o primeiro erro aborta o bloco e tudo
  depois volta `InFailedSqlTransaction` — o diagnóstico pararia de diagnosticar
  exatamente quando há o que diagnosticar.
- **O `--chamar` aponta para `INNOVAMSG63`**, o indicador da caixa postal, que
  vai para `/Monitorar` — o único endpoint que o SERPRO não cobra. Isso dá uma
  propriedade que vale mais do que parece: **até a recusa é de graça.** Se a
  procuração e-CAC daquele cliente não estiver valendo, a resposta é 4xx e custa
  R$ 0,00, enquanto a mesma pergunta feita pelo SITFIS custaria R$ 0,32 para
  receber o mesmo "não". É a sonda certa para as duas dúvidas de uma vez: "a
  nossa autenticação funciona?" e "a procuração deste cliente está de pé?".

Por isso a ordem do primeiro teste em produção é: ensaio sem rede → ensaio com
`--chamar` no CNPJ da própria ON (que não depende de procuração de ninguém) →
ensaio com `--chamar` num cliente. O terceiro passo é o que prova a procuração,
e os três somados custam R$ 0,00.

### `--varrer`: quem está no trilho, perguntado à Receita

Se a recusa é gratuita, perguntar por **todos** também é. `--varrer` passa a
carteira ativa inteira pelo indicador e separa quem respondeu de quem foi
recusado — com o código e a frase que o SERPRO mandou, porque é a frase que
distingue "sem procuração" de "CNPJ baixado" de "serviço indisponível para este
perfil", e é ela que a pessoa leva para o e-CAC conferir.

Isso responde "quais empresas estão no trilho do Integra Contador?" pela única
fonte que decide a questão. Dá para inferir de configuração nossa em outro
sistema, mas configuração diz o que alguém quis em algum momento; a resposta do
SERPRO diz o que vale hoje. São coisas diferentes, e a segunda é a que o worker
vai encontrar quando rodar.

Três decisões que importam:

- **Os alvos saem de `clientes`, não de `v_integra_habilitados`.** A view exige a
  linha de procuração em `certificados_digitais` — que é justamente o que a
  varredura existe para descobrir. Ler a view daria zero alvos e a conclusão
  falsa de que ninguém tem procuração.
- **O CNPJ da própria ON conta separado.** O contratante consulta a si mesmo
  direto pelo e-CNPJ, sem procuração no meio: um "ok" ali prova o encanamento,
  um "ok" num cliente prova a procuração. Somados, fariam a varredura parecer
  sucesso parcial quando o que funcionou foi só a autenticação.
- **`FalhaAutenticacao` interrompe.** Não é resposta sobre cliente nenhum, é a
  nossa credencial — quem recebe isso na primeira chamada receberia nas 96
  seguintes. O resultado parcial é marcado como não conclusivo.

O ensaio **soma os custos registrados no fim** em vez de prometer que foi de
graça, e aborta antes de começar se o catálogo disser que o indicador custa algo:
varrer 90+ clientes a R$ 0,24 não é teste, é conta.

Efeito colateral útil: a saída é exatamente a lista de `procuracao_ecac` que
falta em `certificados_digitais` — hoje a tabela tem 94 linhas e **todas** são
A1, nenhuma procuração, o que é o motivo de `v_integra_habilitados` voltar vazia.
O ensaio imprime o `insert` e **não** o executa: registrar procuração é ato de
cadastro, e cadastro tem dono. `data_vencimento` sai nula porque `/Monitorar` não
devolve o prazo, e inventar data seria pior que não ter — o alerta de 30 dias
passaria a disparar no dia errado. Quem vigia o vencimento na prática é essa
mesma chamada gratuita, que passa a recusar no dia em que a procuração morrer.

### Em produção

```bash
docker build -t integra-worker .
docker run --env-file .env integra-worker        # python -m onstage.main
```

Variáveis em `.env.example`. Nenhuma delas é segredo do SERPRO.

## Verificação contra o e-CAC

`pytest` prova que o worker grava onde deve; **não** prova que o SERPRO devolve o
que esperamos. Para isso, a conferência é manual, com amostra de 5 clientes de
perfis diferentes (Simples com parcelamento, MEI, inapto, limpo, com dívida
ativa), lado a lado com o e-CAC, registrada em
`portal-360/docs/MONITORAMENTO-FISCAL.md`. Repetir após cada mudança de parser.

## Riscos

- **O e-CNPJ A1 da ON vencer derruba tudo.** O alerta de 30/15/7 dias vive no
  portal, sobre `certificados_digitais`. Aqui há duas últimas linhas de defesa:
  `ProviderSupabase` loga o vencimento na partida do container, e o
  `SerproClient` recusa certificado fora da validade com `CertificadoVencido` —
  erro **definitivo**, para o job morrer com a mensagem certa em vez de todas as
  chamadas falharem em erro de TLS sem dizer o motivo.
- **Procuração e-CAC expirada por cliente** geraria 4xx em massa, e 4xx **é
  cobrado**. Por isso o worker consulta `v_integra_habilitados` antes de chamar:
  cliente sem procuração ativa e na validade morre num `ValueError`, sem rede.
  A view filtra por `ativo` **e** por `data_vencimento` — `ativo` é marcação de
  gente, a data é fato. Quando o 4xx vem por outro motivo, é definitivo e fica
  em `integra_estado_cliente.ultimo_erro`, para a tela dizer "a última tentativa
  falhou" em vez de exibir dado velho com cara de fresco.
- **`204` e `304` do SERPRO são normais** (processando / cache ainda válido), não
  erro.
- **CND não é Integra Contador** — é contrato e preço separados. O SITFIS permite
  *indício* de regularidade (`v_sitfis_atual.sem_pendencia`), e a tela precisa
  chamar de indício.
- **Ponto cego dos parcelamentos:** consultar só o que o Nibo indica significa
  que acordo que existe apenas no e-CAC não é descoberto. Para isso existe
  `params.varrer_todas`, de cadência mais larga — e é ela que revela guia
  faltando no calendário do Nibo.
