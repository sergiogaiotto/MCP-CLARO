"""Leitura dos valores de politica vigentes (decisao 11).

Vale a linha mais nova de cada chave (visao ``politica_vigente``); sem linha, o valor inicial. Valor fora
dos limites aceitos e trazido para dentro, nunca recusado na hora do pedido.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.config.politica import POLITICA


async def vigente(c: AsyncConnection, chave: str) -> int:
    regra = POLITICA[chave]
    valor = (
        await c.execute(text("SELECT valor FROM politica_vigente WHERE chave = :k"), {"k": chave})
    ).scalar_one_or_none()
    if valor is None:
        return regra.inicial
    return max(regra.minimo, min(regra.maximo, int(valor)))


async def varios(c: AsyncConnection, *chaves: str) -> dict[str, int]:
    return {k: await vigente(c, k) for k in chaves}
