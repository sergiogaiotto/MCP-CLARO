"""Gravacao no extrato e fechamento, pelas funcoes proprias do banco."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.extrato.frases import frase

_CHAVES_PROIBIDAS = frozenset(
    {
        "argumentos",
        "arguments",
        "resultado",
        "result",
        "token",
        "cracha",
        "chave",
        "senha",
        "password",
        "authorization",
        "secret",
        "segredo",
        "conteudo",
        "content",
    }
)


def _sem_conteudo(dados: dict[str, Any]) -> dict[str, Any]:
    proibidas = {k for k in dados if k.lower() in _CHAVES_PROIBIDAS}
    if proibidas:
        raise ValueError(f"O extrato nao guarda conteudo: chaves recusadas {sorted(proibidas)}")
    return dados


async def registrar(
    conexao: AsyncConnection,
    *,
    papel: str,
    tipo: str,
    campos: dict[str, object] | None = None,
    protocolo: str | None = None,
    aplicativo_id: uuid.UUID | None = None,
    pessoa_id: uuid.UUID | None = None,
    sistema_id: uuid.UUID | None = None,
    ferramenta: str | None = None,
    dados: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> int:
    """Acrescenta uma linha ao extrato, na transacao da conexao dada."""
    texto = frase(tipo, **(campos or {}))
    resultado = await conexao.execute(
        text(
            "SELECT extrato_registrar(:papel, :tipo, :frase, :protocolo, :aplicativo, :pessoa, :sistema,"
            " :ferramenta, CAST(:dados AS jsonb), :trace)"
        ),
        {
            "papel": papel,
            "tipo": tipo,
            "frase": texto,
            "protocolo": protocolo,
            "aplicativo": aplicativo_id,
            "pessoa": pessoa_id,
            "sistema": sistema_id,
            "ferramenta": ferramenta,
            "dados": json.dumps(_sem_conteudo(dados or {}), ensure_ascii=False, sort_keys=True),
            "trace": trace_id,
        },
    )
    return int(resultado.scalar_one())


@dataclass(frozen=True, slots=True)
class Fechamento:
    numero: int
    linhas: int
    hash: bytes


async def fechar(conexao: AsyncConnection) -> Fechamento:
    """Calcula o proximo fechamento. Um por vez, por trava consultiva no banco."""
    linha = (await conexao.execute(text("SELECT numero, linhas, hash FROM extrato_fechar()"))).one()
    return Fechamento(numero=int(linha.numero), linhas=int(linha.linhas), hash=bytes(linha.hash))


async def conferir_fechamento(conexao: AsyncConnection, numero: int) -> bool:
    """Refaz a conta de um fechamento e diz se ainda bate com o guardado."""
    valor = (await conexao.execute(text("SELECT extrato_conferir(:n)"), {"n": numero})).scalar_one_or_none()
    return bool(valor)
