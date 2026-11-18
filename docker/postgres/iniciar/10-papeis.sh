#!/usr/bin/env bash
# Cria bancos e usuarios separados: gateway (um usuario por papel), cofre, login e Grafana.
# As senhas vem de segredos do Docker montados em /run/secrets.
set -euo pipefail

senha() { tr -d '\n' < "/run/secrets/$1"; }

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE mcp_claro_entrada     LOGIN PASSWORD '$(senha db_entrada)';
CREATE ROLE mcp_claro_despachante LOGIN PASSWORD '$(senha db_despachante)';
CREATE ROLE mcp_claro_rotinas     LOGIN PASSWORD '$(senha db_rotinas)';
CREATE ROLE grafana_leitor        LOGIN PASSWORD '$(senha db_grafana_leitor)';
CREATE ROLE cofre                 LOGIN PASSWORD '$(senha db_cofre)';
CREATE ROLE login                 LOGIN PASSWORD '$(senha db_login)';
CREATE ROLE grafana               LOGIN PASSWORD '$(senha db_grafana)';
CREATE DATABASE cofre   OWNER cofre;
CREATE DATABASE login   OWNER login;
CREATE DATABASE grafana OWNER grafana;
REVOKE ALL ON DATABASE cofre, login, grafana FROM PUBLIC;
GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas, grafana_leitor;
SQL
