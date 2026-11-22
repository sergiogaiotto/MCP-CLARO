"""Fixtures comuns. Testes de integracao sobem peças reais em Docker."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

IMAGEM_POSTGRES = os.environ.get("MCP_CLARO_TESTE_POSTGRES", "postgres:18.7")
RAIZ = Path(__file__).resolve().parents[1]
REVOGAR = "REVOKE TEMPORARY ON DATABASE mcp_claro FROM PUBLIC"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """Sobe um PostgreSQL 18 real e devolve (url sem senha, arquivo da senha)."""
    testcontainers = pytest.importorskip("testcontainers.postgres")
    with testcontainers.PostgresContainer(
        IMAGEM_POSTGRES, username="mcp_claro_migracao", password="senha-de-teste", dbname="mcp_claro"
    ) as pg:
        # Como o docker/postgres/iniciar/10-papeis.sh: nenhum papel cria tabela temporaria pelo PUBLIC.
        revogar = pg.exec(
            ["psql", "-v", "ON_ERROR_STOP=1", "-U", "mcp_claro_migracao", "-d", "mcp_claro", "-c", REVOGAR]
        )
        assert revogar.exit_code == 0, revogar.output
        host = pg.get_container_host_ip()
        porta = pg.get_exposed_port(5432)
        senha = tmp_path_factory.mktemp("segredos") / "db"
        senha.write_text("senha-de-teste", encoding="utf-8")
        yield f"postgresql://mcp_claro_migracao@{host}:{porta}/mcp_claro", senha


@pytest.fixture(scope="session")
def parametros_banco(postgres_url: tuple[str, Path]) -> Iterator[object]:
    from mcp_claro.config.settings import Parametros

    url, senha = postgres_url
    p = Parametros(APP_ENV="teste", DATABASE_URL=url, DATABASE_PASSWORD_FILE=str(senha), DB_SSLMODE="disable")
    return p


@pytest.fixture(scope="session")
def banco_migrado(parametros_banco: object) -> object:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.attributes["parametros"] = parametros_banco
    command.upgrade(cfg, "head")
    return parametros_banco


@pytest.fixture
async def conexao(banco_migrado: object) -> AsyncIterator[object]:
    from mcp_claro.db.engine import criar_engine

    engine = criar_engine(banco_migrado, pool_size=2, max_overflow=0)  # type: ignore[arg-type]
    try:
        async with engine.connect() as c:
            yield c
    finally:
        await engine.dispose()
