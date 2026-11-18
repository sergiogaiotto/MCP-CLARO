"""O que a revisao do ADMIN recusa, antes de gravar uma ferramenta ou mudar um sistema.

A tela do ADMIN chama estas funcoes antes de ``cadastro.repositorio.revisar_ferramenta`` e antes de
mudar o tipo de chave de um sistema. A regra principal: sistema de chave compartilhada e so de
leitura, porque ele nao distingue as pessoas (a leitura ja leva aviso no resultado).
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CLASSES: Final = ("le", "muda", "tem_volta")
TIPOS_CHAVE: Final = ("por_pessoa", "compartilhada", "programa")


class RevisaoRecusada(ValueError):
    """A revisao nao pode ser gravada; a mensagem e legivel para o ADMIN."""


def classe_permitida(tipo_chave: str, classe: str) -> bool:
    """Sistema de chave compartilhada so aceita ferramentas da classe "le"."""
    return classe in CLASSES and (tipo_chave != "compartilhada" or classe == "le")


def conferir_classe(tipo_chave: str, classe: str) -> None:
    if classe not in CLASSES:
        raise RevisaoRecusada(f"Classe desconhecida: {classe}. Use le, muda ou tem_volta.")
    if not classe_permitida(tipo_chave, classe):
        raise RevisaoRecusada(
            "Este sistema usa chave compartilhada: ele nao distingue as pessoas. "
            "Por isso, as ferramentas dele so podem entrar como leitura (classe le)."
        )


async def conferir_revisao(c: AsyncConnection, sistema_id: uuid.UUID, classe: str) -> None:
    """Para a tela do ADMIN, antes de gravar a revisao de uma ferramenta deste sistema."""
    tipo_chave = (
        await c.execute(text("SELECT tipo_chave FROM sistemas WHERE id = :s"), {"s": sistema_id})
    ).scalar_one_or_none()
    if tipo_chave is None:
        raise LookupError("Sistema desconhecido.")
    conferir_classe(tipo_chave, classe)


async def conferir_troca_tipo_chave(c: AsyncConnection, sistema_id: uuid.UUID, novo_tipo: str) -> None:
    """Para a tela do ADMIN, antes de mudar o tipo de chave de um sistema.

    - virar ``compartilhada`` exige que nenhuma ferramenta ativa seja de outra classe que nao "le";
    - autorizacao por pessoa (``sistemas.autorizacao``) so vale com ``por_pessoa``.
    """
    if novo_tipo not in TIPOS_CHAVE:
        raise RevisaoRecusada(f"Tipo de chave desconhecido: {novo_tipo}.")
    linha = (await c.execute(text("SELECT autorizacao FROM sistemas WHERE id = :s"), {"s": sistema_id})).first()
    if linha is None:
        raise LookupError("Sistema desconhecido.")
    if novo_tipo != "por_pessoa" and linha.autorizacao is not None:
        raise RevisaoRecusada(
            "Este sistema tem autorizacao por pessoa configurada. Tire a configuracao antes de mudar o tipo de chave."
        )
    if novo_tipo == "compartilhada":
        acoes = (
            await c.execute(
                text(
                    "SELECT count(*) FROM ferramentas_revisadas "
                    "WHERE sistema_id = :s AND situacao = 'ativa' AND classe <> 'le'"
                ),
                {"s": sistema_id},
            )
        ).scalar_one()
        if acoes:
            raise RevisaoRecusada(
                f"Este sistema tem {acoes} ferramenta(s) que mudam dados. Com chave compartilhada, "
                "so leitura e aceita: suspenda ou revise essas ferramentas antes."
            )
