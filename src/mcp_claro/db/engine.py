"""Acesso ao PostgreSQL, o unico banco do produto.

SQL explicito com SQLAlchemy Core sobre ``asyncpg``. Cada papel conecta com o
proprio usuario. A senha vem de ``DATABASE_PASSWORD_FILE`` (segredo do Docker),
nunca da URL. Em piloto e producao, a conexao exige ``verify-full``.
"""

from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mcp_claro.config.settings import Parametros, SslBanco


def _contexto_ssl(p: Parametros) -> ssl.SSLContext | bool:
    if p.DB_SSLMODE is SslBanco.DISABLE:
        return False
    ctx = ssl.create_default_context(cafile=p.DB_SSLROOTCERT or p.INTERNAL_CA_FILE)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if p.DB_SSLMODE is SslBanco.REQUIRE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif p.DB_SSLMODE is SslBanco.VERIFY_CA:
        ctx.check_hostname = False
    if p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE:
        ctx.load_cert_chain(p.INTERNAL_CERT_FILE, p.INTERNAL_KEY_FILE)
    return ctx


def url_asyncpg(p: Parametros, *, usuario: str | None = None) -> str:
    """Monta a URL ``postgresql+asyncpg`` com a senha lida do segredo."""
    partes = urlsplit(p.DATABASE_URL)
    if partes.password:
        raise ValueError("DATABASE_URL nao pode trazer senha; use DATABASE_PASSWORD_FILE.")
    user = usuario or partes.username or ""
    senha = p.senha_banco.get_secret_value() if p.senha_banco else None
    credencial = quote(user, safe="")
    if senha:
        credencial += ":" + quote(senha, safe="")
    host = partes.hostname or "localhost"
    porta = f":{partes.port}" if partes.port else ""
    netloc = f"{credencial}@{host}{porta}" if credencial else f"{host}{porta}"
    return urlunsplit(("postgresql+asyncpg", netloc, partes.path, "", ""))


def criar_engine(p: Parametros, **extra: Any) -> AsyncEngine:
    """Cria o engine assincrono do papel. Sem PgBouncer em modo de transacao."""
    return create_async_engine(
        url_asyncpg(p),
        connect_args={"ssl": _contexto_ssl(p), "server_settings": {"application_name": "mcp-claro"}},
        pool_pre_ping=True,
        pool_size=extra.pop("pool_size", 5),
        max_overflow=extra.pop("max_overflow", 5),
        **extra,
    )
