# Papel rotinas: so grava versao nova da chave do estado das confirmacoes (rodizio a cada
# REQUEST_STATE_KEY_ROTATION_DAYS) e le os metadados para saber quando girou. Nao le a chave.
path "kv/data/mcp-claro/estado-confirmacoes" {
  capabilities = ["create", "update"]
}

path "kv/metadata/mcp-claro/estado-confirmacoes" {
  capabilities = ["read"]
}
