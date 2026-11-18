# Papel entrada: so le a chave comum do estado das confirmacoes (a atual e as versoes guardadas).
# Nao cifra, nao decifra e nao troca a chave.
path "kv/data/mcp-claro/estado-confirmacoes" {
  capabilities = ["read"]
}
