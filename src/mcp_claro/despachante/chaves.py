"""Chaves das pessoas, dos programas e compartilhadas: cifradas pelo Transit do cofre, na tabela do gateway.

Cada chave e amarrada por contexto ao par (sistema, dono): uma chave copiada
para outra linha nao decifra. So o despachante decifra.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

SEM_DONO = uuid.UUID(int=0)


class Cifra(Protocol):
    async def cifrar(self, texto: bytes, contexto: bytes) -> str: ...
    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes: ...


class ChaveAusente(LookupError):
    pass


def contexto(
    sistema_id: uuid.UUID, pessoa_id: uuid.UUID | None, aplicativo_id: uuid.UUID | None, tipo: str = "acesso"
) -> bytes:
    """Contexto do Transit: amarra a chave cifrada ao sistema, ao dono e ao tipo (acesso, renovacao,
    declaracoes). Uma chave de renovacao copiada para a linha de acesso nao decifra."""
    dono = f"p:{pessoa_id}" if pessoa_id else (f"a:{aplicativo_id}" if aplicativo_id else "compartilhada")
    return f"mcp-claro|{sistema_id}|{dono}|{tipo}".encode()


async def guardar(
    c: AsyncConnection,
    cifra: Cifra,
    *,
    sistema_id: uuid.UUID,
    segredo: str,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    tipo: str = "acesso",
    expira_em: datetime | None = None,
) -> None:
    cifrado = await cifra.cifrar(segredo.encode(), contexto(sistema_id, pessoa_id, aplicativo_id, tipo))
    await c.execute(
        text(
            "INSERT INTO chaves_cifradas (sistema_id, pessoa_id, aplicativo_id, tipo, cifrada, expira_em) "
            "VALUES (:s, :p, :a, :t, :c, :e) "
            "ON CONFLICT (sistema_id, coalesce(pessoa_id, '00000000-0000-0000-0000-000000000000'::uuid), "
            "coalesce(aplicativo_id, '00000000-0000-0000-0000-000000000000'::uuid), tipo) "
            "DO UPDATE SET cifrada = EXCLUDED.cifrada, expira_em = EXCLUDED.expira_em, atualizada_em = now()"
        ),
        {"s": sistema_id, "p": pessoa_id, "a": aplicativo_id, "t": tipo, "c": cifrado, "e": expira_em},
    )


async def abrir(
    c: AsyncConnection,
    cifra: Cifra,
    *,
    sistema_id: uuid.UUID,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    tipo: str = "acesso",
) -> str:
    linha = (
        await c.execute(
            text(
                "SELECT cifrada FROM chaves_cifradas WHERE sistema_id = :s AND tipo = :t "
                "AND pessoa_id IS NOT DISTINCT FROM :p AND aplicativo_id IS NOT DISTINCT FROM :a"
            ),
            {"s": sistema_id, "t": tipo, "p": pessoa_id, "a": aplicativo_id},
        )
    ).first()
    if linha is None:
        raise ChaveAusente("sem chave guardada para este sistema e esta pessoa")
    return (await cifra.decifrar(linha.cifrada, contexto(sistema_id, pessoa_id, aplicativo_id, tipo))).decode()
