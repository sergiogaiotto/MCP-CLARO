"""Limpeza das sessoes das telas e dos pedidos de login vencidos ou ja usados."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.logs import logger

log = logger("mcp_claro.rotinas.limpeza")


async def limpar_sessoes(engine: AsyncEngine) -> dict[str, int]:
    """Apaga sessoes vencidas ou encerradas e logins pendentes vencidos ou usados. Devolve as contagens."""
    async with engine.begin() as c:
        sessoes = (
            await c.execute(text("DELETE FROM sessoes_telas WHERE expira_em <= now() OR encerrada_em IS NOT NULL"))
        ).rowcount
        logins = (
            await c.execute(text("DELETE FROM logins_pendentes WHERE expira_em <= now() OR usado_em IS NOT NULL"))
        ).rowcount
    if sessoes or logins:
        log.info("limpeza_feita", sessoes=sessoes, logins=logins)
    return {"sessoes": int(sessoes), "logins": int(logins)}
