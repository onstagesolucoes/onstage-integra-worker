"""Cola entre o pacote `integra_serpro` e a infraestrutura da ON.

A separação é deliberada: `integra_serpro` conhece o protocolo do SERPRO e é
testável sem rede nem banco; `onstage` conhece o Supabase desta casa (Vault,
Storage, Postgres) e não entra em teste nenhum. Quando o SERPRO mudar um
envelope, mexe-se num lado; quando trocarmos de cofre, no outro.
"""
