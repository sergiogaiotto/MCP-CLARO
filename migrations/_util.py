"""Ajudas das migracoes: SQL em bloco pelo protocolo simples do asyncpg."""

from __future__ import annotations

from typing import Any

from alembic import op


def sql_em_bloco(sql: str) -> None:
    """Roda varios comandos de uma vez (funcoes com $$, DO, etc.) na transacao da migracao."""
    conexao: Any = op.get_bind().connection.dbapi_connection
    conexao.run_async(lambda bruta: bruta.execute(sql))
