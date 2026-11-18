"""Ambiente do Alembic: roda as migracoes com o engine assincrono do produto."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine


def _rodar(conexao: Connection) -> None:
    context.configure(connection=conexao, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    url = context.config.attributes.get("parametros")
    p = url if isinstance(url, Parametros) else Parametros()
    engine = criar_engine(p, pool_size=1, max_overflow=0)
    async with engine.connect() as conexao:
        await conexao.run_sync(_rodar)
        await conexao.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("As migracoes do MCP-CLARO rodam so contra um banco (modo online).")

asyncio.run(_online())
