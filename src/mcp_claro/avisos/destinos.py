"""Para quem vai cada aviso, e como o destino vira endereco na hora do envio.

Destinos aceitos na coluna ``avisos.destino``:

- ``pessoa:<uuid>``: e-mail da pessoa, lido do cadastro na hora do envio. Assim o
  endereco nao fica copiado na fila, e a pessoa sem e-mail conhecido nao recebe;
- ``<endereco de e-mail>``: e-mail direto (o ADMIN, por ``NOTIFY_ADMIN_EMAILS``);
- ``webhook:<uuid>``: destino de webhook cadastrado pelo ADMIN em ``destinos_webhook``.
  Se o ADMIN desativar o destino, o que estava na fila nao sai;
- ``https://...``: webhook direto, para destinos fixos de instalacao.

Um aviso ao ADMIN sai para cada e-mail do ADMIN e para cada webhook ativo que assina
o evento (lista de eventos vazia quer dizer "todos").
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.avisos.fila import enfileirar
from mcp_claro.logs import logger

log = logger("mcp_claro.avisos.destinos")

PREFIXO_PESSOA = "pessoa:"
PREFIXO_WEBHOOK = "webhook:"


def destino_pessoa(pessoa_id: uuid.UUID) -> str:
    return f"{PREFIXO_PESSOA}{pessoa_id}"


def destino_webhook(destino_id: uuid.UUID) -> str:
    return f"{PREFIXO_WEBHOOK}{destino_id}"


def parece_email(valor: str) -> bool:
    """Forma minima de e-mail: um @, sem espaco nem quebra de linha, com dominio."""
    if any(ch.isspace() for ch in valor) or valor.count("@") != 1:
        return False
    local, dominio = valor.split("@")
    return bool(local) and "." in dominio and not dominio.startswith(".") and not dominio.endswith(".")


async def email_da_pessoa(conexao: AsyncConnection, pessoa_id: uuid.UUID) -> str | None:
    """E-mail da pessoa ativa, se o cadastro o tiver.

    Le por ``to_jsonb`` para funcionar antes e depois de a coluna ``pessoas.email``
    existir: sem a coluna, a pessoa simplesmente nao tem e-mail conhecido.
    """
    valor = (
        await conexao.execute(
            text("SELECT to_jsonb(p) ->> 'email' FROM pessoas p WHERE p.id = :p AND p.situacao = 'ativa'"),
            {"p": pessoa_id},
        )
    ).scalar_one_or_none()
    if not valor or not parece_email(str(valor)):
        return None
    return str(valor)


async def url_do_webhook(conexao: AsyncConnection, destino_id: uuid.UUID) -> str | None:
    """URL do destino de webhook ativo; ``None`` se sumiu ou foi desativado."""
    valor = (
        await conexao.execute(
            text("SELECT url FROM destinos_webhook WHERE id = :i AND ativo"),
            {"i": destino_id},
        )
    ).scalar_one_or_none()
    return str(valor) if valor else None


async def avisar_pessoa(
    conexao: AsyncConnection,
    pessoa_id: uuid.UUID,
    evento: str,
    assunto: str,
    corpo: Mapping[str, Any],
    chave_unica: str | None = None,
) -> bool:
    """Aviso por e-mail a uma pessoa. O endereco e resolvido so na hora do envio."""
    return await enfileirar(conexao, "email", destino_pessoa(pessoa_id), evento, assunto, corpo, chave_unica)


async def avisar_admin(
    conexao: AsyncConnection,
    evento: str,
    assunto: str,
    corpo: Mapping[str, Any],
    chave_unica: str | None,
    emails_admin: Sequence[str],
) -> int:
    """Aviso ao ADMIN: um por e-mail do ADMIN e um por webhook que assina o evento. Devolve quantos entraram."""
    novos = 0
    alvos = 0
    for email in dict.fromkeys(e.strip() for e in emails_admin if e.strip()):
        if not parece_email(email):
            log.warning("email_admin_invalido")
            continue
        alvos += 1
        chave = f"{chave_unica}|{email}" if chave_unica else None
        novos += await enfileirar(conexao, "email", email, evento, assunto, corpo, chave)
    destinos = (
        await conexao.execute(
            text(
                "SELECT id FROM destinos_webhook WHERE ativo AND (cardinality(eventos) = 0 OR :e = ANY (eventos))"
                " ORDER BY nome"
            ),
            {"e": evento},
        )
    ).scalars()
    for destino_id in destinos:
        alvo = destino_webhook(uuid.UUID(str(destino_id)))
        alvos += 1
        chave = f"{chave_unica}|{alvo}" if chave_unica else None
        novos += await enfileirar(conexao, "webhook", alvo, evento, assunto, corpo, chave)
    if alvos == 0:
        log.warning("aviso_admin_sem_destino", evento=evento)
    return novos
