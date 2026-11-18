"""Leitura dos valores de politica: vale a linha mais nova de ``politica_vigente``; sem linha, o inicial."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.config.politica import POLITICA, valor_inicial
from mcp_claro.logs import logger

log = logger("mcp_claro.rotinas.politica")


async def valor_politica(conexao: AsyncConnection, chave: str) -> int:
    """Valor vigente de uma chave de politica. Valor fora dos limites cai no inicial, com log."""
    definicao = POLITICA[chave]
    valor = (
        await conexao.execute(text("SELECT valor FROM politica_vigente WHERE chave = :c"), {"c": chave})
    ).scalar_one_or_none()
    if valor is None:
        return valor_inicial(chave)
    try:
        return definicao.conferir(int(valor))
    except ValueError:
        log.warning("politica_fora_dos_limites", chave=chave)
        return valor_inicial(chave)
