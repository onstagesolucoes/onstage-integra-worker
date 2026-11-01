-- Os quatro objetos do portal que as migrações `integra_*` pressupõem.
--
-- Não é um esquema de teste inventado: é o recorte mínimo do que já existe em
-- produção, com os **mesmos tipos e as mesmas restrições**, porque é exatamente
-- aí que o worker quebra. Dois exemplos que este arquivo existe para pegar:
--
--   • `parcelamentos_fiscais.situacao` aceita quatro valores e nada mais. O
--     e-CAC escreve situação em texto livre ("Rescindido por falta de
--     pagamento"). Sem o CHECK aqui, o teste do de-para passaria e o `insert`
--     estouraria só em produção.
--   • `cnpj_norm` é `text` em `clientes` e `char(14)` nas tabelas novas. O join
--     da `v_integra_habilitados` cruza os dois; com stand-in de tipo errado o
--     teste não prova nada sobre a view real.
--
-- Fora daqui, tudo o que os testes usam é criado pelas migrações de verdade
-- (`portal-360/supabase/migrations/20261101001600..001900`), aplicadas na
-- ordem. Rodar as migrações reais contra um banco limpo é, por si só, a
-- primeira verificação de que elas são válidas e auto-consistentes.

/* ─────────────── eh_interno(): o stub mais importante do arquivo ─────────────── */

-- Em produção: `exists (select 1 from usuario_acesso where user_id = auth.uid()
-- and papel = 'interno')`. Numa conexão psycopg direta não há JWT, `auth.uid()`
-- é nulo, e a função devolve **false**.
--
-- O stub devolve false de propósito, e não true. É assim que o teste reproduz a
-- condição real do worker: para ele, `v_integra_teto_hoje` não devolve linha
-- nenhuma. Um stub que devolvesse true deixaria a view funcionar no teste e
-- esconderia o defeito — teto lido da view, "sem linha" interpretado como "sem
-- teto", e o limite de gasto só decorativo.
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$
  select null::uuid
$$;

create or replace function public.eh_interno() returns boolean
language sql stable as $$
  select auth.uid() is not null
$$;

/* ──────────────────────────────── clientes ──────────────────────────────── */

-- `cnpj_norm` é o grão canônico do portal (97 clientes ativos) e precisa ser
-- único: `parcelamentos_fiscais` tem FK para ele.
create table if not exists public.clientes (
  id           bigserial primary key,
  cnpj_norm    text unique,
  razao_social text,
  ativo        boolean default true
);

/* ─────────────────────── certificados_digitais ─────────────────────── */

-- A tabela que decide quem pode ser consultado. Atenção ao nome da coluna: aqui
-- é `cnpj` (é a view `v_certificado_cliente` que expõe `cnpj_norm`) — trocar os
-- dois faz a `v_integra_habilitados` devolver zero linha sem erro nenhum.
create table if not exists public.certificados_digitais (
  id              bigserial primary key,
  cnpj            text not null,
  tipo            text check (tipo in ('A1','A3','procuracao_ecac','nao_possui')),
  arquivo_path    text,
  data_vencimento date,
  ativo           boolean not null default true
);

/* ─────────────────────── parcelamentos_fiscais ─────────────────────── */

-- Restrições copiadas de produção, não reescritas. O índice parcial do Nibo
-- vem junto porque o do e-CAC (criado pela migração) é o espelho dele: é a
-- convivência das duas origens no mesmo grão que se quer provar.
create table if not exists public.parcelamentos_fiscais (
  id                 bigserial primary key,
  cnpj_norm          text not null references public.clientes(cnpj_norm) on delete cascade,
  orgao              text not null check (orgao in ('RFB','PGFN','SEFAZ','MUNICIPIO')),
  modalidade         text not null,
  numero             text,
  situacao           text not null default 'ativo'
                     check (situacao in ('ativo','em_atraso','quitado','rescindido')),
  parcelas_total     int,
  parcelas_pagas     int,
  valor_parcela      numeric(15,2),
  saldo_devedor      numeric(15,2),
  data_adesao        date,
  proximo_vencimento date,
  observacao         text,
  origem             text not null default 'manual'
                     check (origem in ('manual','ecac','nibo')),
  criado_em          timestamptz not null default now(),
  atualizado_em      timestamptz not null default now()
);

create index if not exists parcelamentos_fiscais_cnpj_norm_idx
  on public.parcelamentos_fiscais (cnpj_norm);

create unique index if not exists uq_parcelamento_nibo
  on public.parcelamentos_fiscais (cnpj_norm, orgao, modalidade)
  where origem = 'nibo';
