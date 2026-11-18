#!/usr/bin/env bash
# Partida do PostgreSQL do MCP-CLARO: TLS sempre; com PGBACKREST_LIGADO=ligado (fragmento
# docker/backup/compose.backup.yml), tambem o servidor TLS do pgBackRest e o archive_command, que manda
# cada WAL ao conteiner de backup.
set -euo pipefail
/usr/local/bin/preparar-tls.sh

extra=()
if [ "${PGBACKREST_LIGADO:-desligado}" = "ligado" ]; then
  /usr/local/bin/pgbackrest-banco.sh preparar
  stanza="${PGBACKREST_STANZA:-mcp-claro}"
  extra+=(
    -c archive_mode=on
    -c "archive_command=pgbackrest --stanza=${stanza} archive-push %p"
    -c "archive_timeout=${PGBACKREST_ARCHIVE_TIMEOUT:-300}"
  )
  # Servidor TLS do pgBackRest (porta 8432): o conteiner de backup chega ao banco por ele. Sai de um
  # subshell para virar filho do init (PID 1), nunca do PostgreSQL: o postmaster nao deve ver o fim dele.
  ( /usr/local/bin/pgbackrest-banco.sh servir & )
fi

exec docker-entrypoint.sh "$@" \
  -c ssl=on \
  -c ssl_cert_file=/var/lib/postgresql/tls/cert.pem \
  -c ssl_key_file=/var/lib/postgresql/tls/key.pem \
  -c ssl_ca_file=/var/lib/postgresql/tls/ca.pem \
  -c ssl_min_protocol_version=TLSv1.2 \
  "${extra[@]}"
