"""Catraca: leituras por ordem de servico por dia (politica ``READS_CAP_PER_DAY``).

Passar pela catraca e um UPDATE que so passa se houver saldo: a contagem nunca ultrapassa o teto,
mesmo com pedidos ao mesmo tempo. A leitura conta antes de seguir ao despachante, entao uma
leitura que falha depois tambem gasta a vez (o lado seguro). O dia vira a meia-noite de Brasilia.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.config.politica import POLITICA

CHAVE_POLITICA: Final = "READS_CAP_PER_DAY"
FUSO: Final = "America/Sao_Paulo"


@dataclass(frozen=True, slots=True)
class Passagem:
    passou: bool
    contagem: int
    teto: int

    def mensagem(self) -> str:
        return mensagem(self.teto)


def mensagem(teto: int) -> str:
    return (
        f"Voce chegou ao limite de {teto} leituras por dia nesta ordem de servico. Nada foi enviado. "
        "A contagem volta a zero a meia-noite."
    )


async def teto_diario(c: AsyncConnection) -> int:
    """Valor vigente da politica; sem linha, o valor inicial. Valor fora dos limites e trazido para dentro."""
    regra = POLITICA[CHAVE_POLITICA]
    valor = (
        await c.execute(text("SELECT valor FROM politica_vigente WHERE chave = :k"), {"k": CHAVE_POLITICA})
    ).scalar_one_or_none()
    if valor is None:
        return regra.inicial
    return max(regra.minimo, min(regra.maximo, int(valor)))


async def passar(c: AsyncConnection, ordem_id: uuid.UUID, *, teto: int | None = None) -> Passagem:
    """Conta uma leitura da ordem hoje, se houver saldo. Grava na transacao do chamador."""
    limite = teto if teto is not None else await teto_diario(c)
    contagem = (
        await c.execute(
            text(
                "INSERT INTO leituras_diarias AS l (ordem_id, dia, contagem) "
                "VALUES (:o, (now() AT TIME ZONE :fuso)::date, 1) "
                "ON CONFLICT (ordem_id, dia) DO UPDATE SET contagem = l.contagem + 1 "
                "WHERE l.contagem < :teto RETURNING l.contagem"
            ),
            {"o": ordem_id, "fuso": FUSO, "teto": limite},
        )
    ).scalar_one_or_none()
    if contagem is not None and contagem <= limite:
        return Passagem(True, int(contagem), limite)
    atual = await usadas_hoje(c, ordem_id)
    return Passagem(False, atual, limite)


async def usadas_hoje(c: AsyncConnection, ordem_id: uuid.UUID) -> int:
    valor = (
        await c.execute(
            text(
                "SELECT contagem FROM leituras_diarias WHERE ordem_id = :o AND dia = (now() AT TIME ZONE :fuso)::date"
            ),
            {"o": ordem_id, "fuso": FUSO},
        )
    ).scalar_one_or_none()
    return int(valor or 0)
