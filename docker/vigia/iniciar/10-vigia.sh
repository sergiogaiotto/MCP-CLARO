#!/usr/bin/env bash
# Banco do vigia (segunda maquina): cria o usuario do papel vigia, que so le e acrescenta.
# O dono do banco (POSTGRES_USER, mcp_claro_vigia_migracao) roda as migracoes de migrations_vigia/.
# A senha vem de um segredo do Docker montado em /run/secrets.
set -euo pipefail

senha() { tr -d '\n' < "/run/secrets/$1"; }

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE mcp_claro_vigia LOGIN PASSWORD '$(senha db_vigia)';
REVOKE ALL ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO mcp_claro_vigia;
SQL
