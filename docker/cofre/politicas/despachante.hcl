# Papel despachante: so cifra e decifra com a chave do gateway no Transit.
# Nao le, nao exporta, nao gira nem apaga a chave, e nao le a chave do estado das confirmacoes.
path "transit/encrypt/mcp-claro-chaves" {
  capabilities = ["update"]
}

path "transit/decrypt/mcp-claro-chaves" {
  capabilities = ["update"]
}
