#!/usr/bin/env bash
# Usuario da copia logica (pg_dump): le tudo (pg_read_all_data) e nao escreve nada.
# A senha vem do segredo do Docker db_backup. Sem o segredo, o usuario nao e criado aqui; a migracao 0004
# cria o papel sem login, e a copia logica fica desligada ate alguem dar a senha (docs/restauracao.md).
set -euo pipefail

if [ ! -s /run/secrets/db_backup ]; then
  echo "20-backup: sem o segredo db_backup; o usuario da copia logica fica para depois."
  exit 0
fi
senha() { tr -d '\n' < "/run/secrets/$1"; }

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE mcp_claro_backup LOGIN PASSWORD '$(senha db_backup)';
GRANT pg_read_all_data TO mcp_claro_backup;
GRANT CONNECT ON DATABASE ${POSTGRES_DB}, cofre, login, grafana TO mcp_claro_backup;
SQL
