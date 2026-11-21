"""Nenhum papel do app cria tabela temporaria (``REVOKE TEMPORARY ON DATABASE ... FROM PUBLIC``).

Uma tabela temporaria com o nome de uma tabela do esquema e o jeito de enganar uma funcao sem caminho de busca fixo
(``pg_temp`` vem antes de ``public`` e de ``pg_catalog``). As funcoes da 0006 tem caminho fixo (bloco CAMINHO de
``test_migracao_0006``); esta e a defesa de cima: os papeis do app nem chegam a criar a sombra.

- O ``docker/postgres/iniciar/10-papeis.sh`` de verdade, num PostgreSQL novo, como na primeira partida do banco:
  ``mcp_claro_entrada``, ``mcp_claro_despachante``, ``mcp_claro_rotinas`` e ``grafana_leitor`` entram no banco com a
  propria senha, mas ``CREATE TEMP TABLE`` e recusado (42501). O PUBLIC fica so com o CONNECT.
- O banco dos testes (``tests/conftest.py``, que nao roda o script: os papeis nascem na 0001) e montado igual: os
  mesmos papeis, pelo ``SET ROLE``, tambem recebem 42501.
"""

from __future__ import annotations

import secrets
import shutil
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from tests.conftest import IMAGEM_POSTGRES, RAIZ

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

BANCO = "mcp_claro"
DONO = "mcp_claro_migracao"
# Segredo que o 10-papeis.sh le em /run/secrets -> papel que ele cria com essa senha.
SEGREDOS = {
    "db_entrada": "mcp_claro_entrada",
    "db_despachante": "mcp_claro_despachante",
    "db_rotinas": "mcp_claro_rotinas",
    "db_grafana_leitor": "grafana_leitor",
    "db_cofre": "cofre",
    "db_login": "login",
    "db_grafana": "grafana",
}
PAPEIS_DO_APP = ("mcp_claro_entrada", "mcp_claro_despachante", "mcp_claro_rotinas", "grafana_leitor")
SOMBRA = "CREATE TEMP TABLE sistemas (id uuid, origem text)"
SEM_PERMISSAO = "42501"


@dataclass(frozen=True)
class BancoDoScript:
    host: str
    porta: int
    senhas: dict[str, str]  # papel -> senha


@pytest.fixture(scope="module")
def banco_do_script(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BancoDoScript]:
    """Um PostgreSQL novo que roda, na partida, o ``10-papeis.sh`` do repositorio, com segredos de teste."""
    testcontainers = pytest.importorskip("testcontainers.postgres")
    pasta = tmp_path_factory.mktemp("papeis")
    iniciar, segredos = pasta / "iniciar", pasta / "segredos"
    iniciar.mkdir()
    segredos.mkdir()
    shutil.copy2(RAIZ / "docker" / "postgres" / "iniciar" / "10-papeis.sh", iniciar / "10-papeis.sh")
    senhas: dict[str, str] = {}
    for arquivo, papel in SEGREDOS.items():
        senhas[papel] = secrets.token_hex(16)
        (segredos / arquivo).write_text(senhas[papel] + "\n", encoding="utf-8")
        (segredos / arquivo).chmod(0o644)
    for caminho in (iniciar, segredos):
        caminho.chmod(0o755)  # o script roda como o usuario postgres do conteiner
    (iniciar / "10-papeis.sh").chmod(0o755)
    pg = testcontainers.PostgresContainer(IMAGEM_POSTGRES, username=DONO, password=secrets.token_hex(16), dbname=BANCO)
    pg.with_volume_mapping(str(iniciar), "/docker-entrypoint-initdb.d", "ro")
    pg.with_volume_mapping(str(segredos), "/run/secrets", "ro")
    with pg:
        yield BancoDoScript(pg.get_container_host_ip(), int(pg.get_exposed_port(5432)), senhas)


@pytest.mark.parametrize("papel", PAPEIS_DO_APP)
async def test_papel_do_script_entra_no_banco_mas_nao_cria_tabela_temporaria(
    banco_do_script: BancoDoScript, papel: str
) -> None:
    b = banco_do_script
    c = await asyncpg.connect(
        host=b.host, port=b.porta, user=papel, password=b.senhas[papel], database=BANCO, ssl=False
    )
    try:
        assert await c.fetchval("SELECT current_user") == papel  # entrou: o CONNECT continua
        assert await c.fetchval("SELECT has_database_privilege(current_user, current_database(), 'TEMPORARY')") is False
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as recusa:
            await c.execute(SOMBRA)
        assert recusa.value.sqlstate == SEM_PERMISSAO
    finally:
        await c.close()


async def test_o_public_fica_so_com_o_connect_no_banco_do_gateway(banco_do_script: BancoDoScript) -> None:
    b = banco_do_script
    c = await asyncpg.connect(
        host=b.host, port=b.porta, user="mcp_claro_entrada", password=b.senhas["mcp_claro_entrada"], database=BANCO
    )
    try:
        do_public = await c.fetch(
            "SELECT a.privilege_type FROM pg_database d, aclexplode(d.datacl) a"
            " WHERE d.datname = $1 AND a.grantee = 0 ORDER BY 1",
            BANCO,
        )
    finally:
        await c.close()
    assert [linha["privilege_type"] for linha in do_public] == ["CONNECT"]


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(banco_migrado, pool_size=1, max_overflow=0)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("papel", PAPEIS_DO_APP)
async def test_banco_dos_testes_e_montado_igual_ao_script(dono: AsyncEngine, papel: str) -> None:
    async with dono.connect() as c:
        transacao = await c.begin()
        try:
            await c.execute(text("SET LOCAL ROLE " + papel))
            with pytest.raises(DBAPIError) as recusa:
                await c.execute(text(SOMBRA))
        finally:
            await transacao.rollback()
    assert getattr(recusa.value.orig, "sqlstate", None) == SEM_PERMISSAO
