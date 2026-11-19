#!/usr/bin/env bash
# pgBackRest no conteiner do banco.
#   pgbackrest-banco.sh preparar   pastas do pgBackRest e .pgpass do usuario que faz as copias (root)
#   pgbackrest-banco.sh servir     servidor TLS do pgBackRest, de novo se cair (root; roda como postgres)
set -euo pipefail

: "${PGBACKREST_PG_USUARIO:=${POSTGRES_USER:-mcp_claro_migracao}}"
: "${PGBACKREST_PG_SENHA_ARQUIVO:=${POSTGRES_PASSWORD_FILE:-/run/secrets/db_migracao}}"
export PGBACKREST_PG1_USER="$PGBACKREST_PG_USUARIO"

preparar() {
  install -d -o postgres -g postgres -m 700 /var/lib/postgresql/pgbackrest /var/lib/postgresql/pgbackrest/spool
  # O pgBackRest fala com o PostgreSQL pelo socket local, que pede senha (pg_hba.conf). A senha vai num
  # .pgpass do usuario postgres, com ":" e "\" escapados como o libpq pede.
  local senha
  senha="$(tr -d '\n' < "$PGBACKREST_PG_SENHA_ARQUIVO" | sed 's/[\\:]/\\&/g')"
  umask 077
  printf '*:*:*:%s:%s\n' "$PGBACKREST_PG_USUARIO" "$senha" > /var/lib/postgresql/.pgpass
  chown postgres:postgres /var/lib/postgresql/.pgpass
  chmod 600 /var/lib/postgresql/.pgpass
}

servir() {
  while true; do
    gosu postgres pgbackrest --log-level-console=info server || true
    echo "pgbackrest-banco: o servidor TLS do pgBackRest parou; sobe de novo em 5 segundos." >&2
    sleep 5
  done
}

case "${1:-}" in
  preparar) preparar ;;
  servir) servir ;;
  *) echo "uso: $0 preparar|servir" >&2; exit 2 ;;
esac
