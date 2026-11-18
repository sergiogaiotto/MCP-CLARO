"""Tarefas de fundo comuns aos papeis."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES

log = logger("mcp_claro.tarefas")


@contextlib.asynccontextmanager
async def descarregar_contadores_periodicamente(p: Parametros, engine: AsyncEngine) -> AsyncIterator[None]:
    """Leva os contadores ao banco a cada ``METRICS_FLUSH_SECONDS``. Falha so perde o resumo do momento."""

    async def laco() -> None:
        while True:
            await asyncio.sleep(p.METRICS_FLUSH_SECONDS)
            try:
                async with engine.begin() as c:
                    await CONTADORES.descarregar(c)
            except Exception:  # noqa: BLE001 - medicao nunca derruba o papel
                log.warning("contadores_nao_gravados")

    tarefa = asyncio.create_task(laco(), name="contadores")
    try:
        yield
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
        with contextlib.suppress(Exception):
            async with engine.begin() as c:
                await CONTADORES.descarregar(c)
