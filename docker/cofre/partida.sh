#!/bin/sh
# Partida do cofre (OpenBao 2.7.2) no Compose de desenvolvimento e teste.
# - Confere a chave do selo static: 32 bytes em hexadecimal (64 caracteres), sem quebra de linha. O OpenBao le o
#   arquivo como esta e recusa qualquer outro tamanho com uma mensagem pouco clara.
# - Poe a senha do banco (segredo db_cofre) em PGPASSWORD: o pgx do OpenBao junta a variavel a connection_url,
#   que fica sem senha no config.hcl.
set -eu
ulimit -c 0

SELO=/run/secrets/cofre_selo.key
if [ "$(wc -c <"$SELO" | tr -d ' ')" != 64 ] || ! grep -Eq '^[0-9a-fA-F]{64}$' "$SELO"; then
  echo "cofre: $SELO deve ter 64 caracteres hexadecimais, sem quebra de linha (scripts/gerar-segredos-dev.sh)." >&2
  exit 1
fi

PGPASSWORD=$(cat /run/secrets/db_cofre)
export PGPASSWORD
exec bao server -config=/openbao/mcp-claro/config.hcl
