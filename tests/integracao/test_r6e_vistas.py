"""R6e-1 (contrato K5): as declaracoes de um terceiro lidas ao mesmo tempo, e a falha nossa no banco.

- ``Despachante._listar`` grava ``ferramentas_vistas`` numa ordem fixa (pelo nome), e nao na ordem em que o servidor
  listou: duas leituras ao mesmo tempo (duas copias das rotinas, ou a rodada e a conclusao da revisao pelo ADMIN),
  com o servidor listando em ordens opostas, nao travam linhas em ordem inversa e nao dao impasse no banco;
- erro do banco ou do grupo de conexoes do despachante em ``/interno/declaracoes`` (o impasse, o pool tomado do
  R6a-2) e ``503 {"motivo": "conferencia_nao_feita"}``, e nunca 500: as rotinas nao contam a rodada como "sem
  resposta" do servidor, e duas rodadas assim nao levam o terceiro a quarentena;
- o log da falha leva so o tipo do erro.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine, url_asyncpg
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.ligacao import cliente
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.mudou_parou import ResultadoMudouParou, conferir_terceiro
from tests import apoio_g3a as g
from tests.cifra_falsa import CifraFalsa

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

N = 150


def _ferramentas(ordem: int) -> list[dict[str, Any]]:
    lista = [{"name": f"ferramenta_{i:03d}", "description": "x", "inputSchema": {"type": "object"}} for i in range(N)]
    return lista if ordem > 0 else list(reversed(lista))


class SalaQueLista:
    """``Ligacao`` do terceiro: a primeira listagem sai em ordem, a segunda ao contrario; as duas so voltam juntas."""

    def __init__(self, juntas: int = 2) -> None:
        self.vez = 0
        self.juntas = asyncio.Barrier(juntas)

    async def listar_ferramentas(self, *_: Any) -> list[dict[str, Any]]:
        self.vez += 1
        ordem = 1 if self.vez % 2 else -1
        await self.juntas.wait()
        return _ferramentas(ordem)

    async def listar(self, *a: Any) -> Any:
        # Lido na hora da chamada: o teste roda tambem sobre o codigo de antes da Listagem (que so chama o de cima).
        return cliente.Listagem(await self.listar_ferramentas(*a))

    async def chamar_ferramenta(self, *_: Any, **__: Any) -> Any:
        raise AssertionError

    async def sondar(self, *_: Any, **__: Any) -> Any:
        raise AssertionError


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    e = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    yield e
    await e.dispose()


async def _terceiro(dono: AsyncEngine, cifra: CifraFalsa, nome: str = "ferramenta_000") -> g.Arranjo:
    return await g.preparar(
        dono,
        cifra,
        endereco="https://terceiro.exemplo.teste/mcp",
        definicao={"name": nome, "description": "x", "inputSchema": {"type": "object"}},
        origem="terceiro",
    )


async def test_duas_leituras_ao_mesmo_tempo_em_ordens_opostas_nao_dao_impasse(
    banco_migrado: Parametros, dono: AsyncEngine
) -> None:
    """A prova da revisao (``test_rev_conc_vistas``), invertida: as duas leituras gravam, sem impasse."""
    cifra = CifraFalsa()
    arr = await _terceiro(dono, cifra)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    try:
        # Primeira leitura: as linhas de ferramentas_vistas passam a existir (o caso de todo dia: ON CONFLICT).
        primeira = g.despachante(desp_db, cifra, sala=SalaQueLista(juntas=1))  # type: ignore[arg-type]
        await primeira.ler_declaracoes(arr.sistema_id, None)
        for _ in range(3):  # o impasse dependia da hora de cada INSERT: tres vezes, para nao passar por sorte
            desp = g.despachante(desp_db, cifra, sala=SalaQueLista())  # type: ignore[arg-type]
            desfechos = await asyncio.gather(
                desp.ler_declaracoes(arr.sistema_id, None),
                desp.ler_declaracoes(arr.sistema_id, None),
                return_exceptions=True,
            )
            tipos = [type(d).__name__ if isinstance(d, BaseException) else len(d) for d in desfechos]
            assert tipos == [N, N], tipos
    finally:
        await desp_db.dispose()
    async with dono.connect() as c:
        vistas = (
            await c.execute(
                text("SELECT count(*) FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": arr.sistema_id}
            )
        ).scalar_one()
    assert vistas == N


def _engine_de_uma_conexao(p: Parametros) -> AsyncEngine:
    """O despachante com uma conexao so e espera curta por ela: com a conexao tomada, o pool esgota (R6a-2)."""
    return create_async_engine(
        url_asyncpg(p),
        connect_args={
            "ssl": False,
            "server_settings": {"application_name": "teste-r6e", "role": "mcp_claro_despachante"},
        },
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
    )


@pytest.fixture
async def pool_tomado(banco_migrado: Parametros, dono: AsyncEngine) -> AsyncIterator[tuple[Any, g.Arranjo, Any]]:
    """O app do despachante de verdade, com a unica conexao do pool tomada por outro trabalho."""
    cifra = CifraFalsa()
    arr = await _terceiro(dono, cifra, "consultar")
    desp_db = _engine_de_uma_conexao(banco_migrado)
    app = criar_app_despachante(
        banco_migrado.model_copy(update={"APP_ENV": "teste"}),
        desp_db,
        cifra,
        local_para_terceiro=g.LOCAL_DE_TESTE,
        liberar_locais=True,
    )
    transporte = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with (
            desp_db.connect() as tomada,
            httpx2.AsyncClient(transport=transporte, base_url="http://despachante.teste:9443") as http,
        ):
            await tomada.execute(text("SELECT 1"))
            yield http, arr, tomada
    finally:
        await desp_db.dispose()


async def test_pool_tomado_nas_declaracoes_e_503_conferencia_nao_feita(
    pool_tomado: tuple[httpx2.AsyncClient, g.Arranjo, Any],
) -> None:
    http, arr, _ = pool_tomado
    with capture_logs() as logs:
        resposta = await http.post("/interno/declaracoes", json={"sistema_id": str(arr.sistema_id)})
    assert resposta.status_code == 503, resposta.text
    corpo = resposta.json()
    assert corpo["motivo"] == "conferencia_nao_feita"
    assert corpo["mensagem"] == "o MCP-CLARO nao conseguiu usar o banco; a rodada nao conta"
    falhas = [linha for linha in logs if linha.get("event") == "declaracoes_sem_banco"]
    assert falhas == [{"event": "declaracoes_sem_banco", "erro": "TimeoutError", "log_level": "warning"}]


async def test_duas_rodadas_com_o_banco_do_despachante_fora_nao_levam_a_quarentena(
    banco_migrado: Parametros, dono: AsyncEngine, pool_tomado: tuple[httpx2.AsyncClient, g.Arranjo, Any]
) -> None:
    """A segunda prova da revisao, invertida, com a falha de verdade (o pool do despachante tomado), e nao um 500
    escrito no teste: as rotinas leem 503 ``conferencia_nao_feita`` e nao contam a rodada."""
    http, arr, _ = pool_tomado
    assert arr.revisao_id is not None
    p = banco_migrado.model_copy(update={"DISPATCHER_INTERNAL_URL": "http://despachante.teste:9443"})
    fonte = ClienteDespachante(p, http=http)
    rotinas_db = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    resultados: list[ResultadoMudouParou] = []
    try:
        for _ in range(2):
            resultado = ResultadoMudouParou()
            await conferir_terceiro(
                rotinas_db, fonte, arr.sistema_id, arr.sistema, arr.revisao_id, [], resultado, intervalo=0
            )
            resultados.append(resultado)
    finally:
        await rotinas_db.dispose()
    async with dono.connect() as c:
        s = (
            await c.execute(
                text("SELECT situacao, conferencias_sem_resposta FROM sistemas WHERE id = :s"), {"s": arr.sistema_id}
            )
        ).one()
    assert [r.quarentenas for r in resultados] == [[], []]
    assert [r.sem_resposta for r in resultados] == [[arr.sistema], [arr.sistema]]  # nao conferido, nao contado
    assert (s.situacao, s.conferencias_sem_resposta) == ("ativo", 0)


_ = uuid
