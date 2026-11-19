"""Fechamento do extrato a cada ``CLOSING_INTERVAL_HOURS``.

A tarefa acorda com frequencia e so fecha quando o ultimo fechamento ficou mais
velho que o intervalo. Assim o ritmo vale mesmo com reinicios e com mais de um
processo. O calculo e o encadeamento sao do banco (``extrato_fechar``, com trava
propria); aqui fica a frase ``fechamento`` no extrato.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.extrato import fechar, registrar
from mcp_claro.extrato.registro import Fechamento
from mcp_claro.logs import logger

log = logger("mcp_claro.rotinas.fechamento")


async def fechar_agora(engine: AsyncEngine) -> Fechamento | None:
    """Calcula o proximo fechamento e registra a frase. ``None`` se outro fechamento estava em andamento."""
    try:
        async with engine.begin() as c:
            f = await fechar(c)
            await registrar(
                c,
                papel="rotinas",
                tipo="fechamento",
                campos={"numero": f.numero, "linhas": f.linhas},
                dados={"numero": f.numero, "linhas": f.linhas, "hash": f.hash.hex()},
            )
    except DBAPIError as erro:
        if "Outro fechamento" in str(erro.orig):
            log.info("fechamento_ocupado")
            return None
        raise
    log.info("fechamento_feito", numero=f.numero, linhas=f.linhas)
    return f


async def fechar_se_devido(engine: AsyncEngine, intervalo_horas: int) -> Fechamento | None:
    """Fecha se o ultimo fechamento tem pelo menos ``intervalo_horas`` (ou se nunca houve um)."""
    async with engine.connect() as c:
        idade = (await c.execute(text("SELECT now() - max(calculado_em) FROM fechamentos"))).scalar_one_or_none()
    if idade is not None and idade < timedelta(hours=intervalo_horas):
        return None
    return await fechar_agora(engine)
