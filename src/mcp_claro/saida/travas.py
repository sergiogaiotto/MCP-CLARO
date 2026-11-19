"""Travas consultivas da porta de saida.

Bloquear, cancelar e enviar disputam a mesma acao, e so um deles acontece. A disputa se resolve em
duas camadas, sempre na mesma ordem (por isso sem impasse):

1. Travas consultivas de transacao por alcance: o despachante toma, compartilhadas e em ordem, as de
   ``tudo``, do sistema, do aplicativo e da pessoa; quem liga um bloqueio (ou fecha a saida) toma,
   exclusiva, a do alcance dele. Assim, ou o bloqueio entra antes e o despachante ja o enxerga, ou o
   despachante marca "enviando" antes e o comprovante mostra a acao como ja saida.
2. Trava da linha da fila (``FOR UPDATE``), tomada depois das consultivas. Cancelar e confirmar so
   tocam a linha, e o ``UPDATE ... WHERE estado = ...`` decide quem chegou primeiro.

A trava de sessao ``DESPACHANTE_UNICO`` garante um so despachante enviando por vez, mesmo com varias
copias do papel no ar.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def chave(tipo: str, ident: object = "") -> int:
    """Numero de 64 bits com sinal para ``pg_advisory_*``, estavel entre versoes."""
    resumo = hashlib.sha256(f"mcp-claro|saida|{tipo}|{ident}".encode()).digest()
    return int.from_bytes(resumo[:8], "big", signed=True)


DESPACHANTE_UNICO: Final = chave("despachante-unico")
TUDO: Final = chave("tudo")


def chaves_do_envio(pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, sistema_id: uuid.UUID) -> list[int]:
    return sorted({TUDO, chave("sistema", sistema_id), chave("aplicativo", aplicativo_id), chave("pessoa", pessoa_id)})


def chaves_do_alcance(
    alcance: str,
    *,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    sistema_id: uuid.UUID | None = None,
) -> list[int]:
    if alcance == "pessoa":
        return [chave("pessoa", pessoa_id)]
    if alcance == "aplicativo":
        return [chave("aplicativo", aplicativo_id)]
    if alcance == "pessoa_aplicativo":
        return sorted({chave("pessoa", pessoa_id), chave("aplicativo", aplicativo_id)})
    if alcance == "sistema":
        return [chave("sistema", sistema_id)]
    return [TUDO]


async def compartilhar_envio(
    c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, sistema_id: uuid.UUID
) -> None:
    for k in chaves_do_envio(pessoa_id, aplicativo_id, sistema_id):
        await c.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": k})


async def exclusiva(c: AsyncConnection, chaves: list[int]) -> None:
    for k in sorted(set(chaves)):
        await c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})
