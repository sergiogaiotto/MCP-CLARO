# Papel despachante: so cifra e decifra no Transit, com a chave dos sistemas proprios e com a chave so dos
# servidores de terceiros (Fase 3: VAULT_TRANSIT_KEY e VAULT_TRANSIT_KEY_THIRD_PARTY).
# Nao le, nao exporta, nao gira nem apaga nenhuma das duas, e nao le a chave do estado das confirmacoes.
path "transit/encrypt/mcp-claro-chaves" {
  capabilities = ["update"]
}

path "transit/decrypt/mcp-claro-chaves" {
  capabilities = ["update"]
}

path "transit/encrypt/mcp-claro-chaves-terceiros" {
  capabilities = ["update"]
}

path "transit/decrypt/mcp-claro-chaves-terceiros" {
  capabilities = ["update"]
}
