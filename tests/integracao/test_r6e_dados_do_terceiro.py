"""R6e-1, retoque (contrato K5 e critica A5): o que o banco nao guarda, vindo do servidor, conta contra o servidor.

O 503 ``conferencia_nao_feita`` (K5) e so para a falha nossa no banco: impasse, conexao, grupo de conexoes tomado.
Um erro do banco causado pelo que o servidor declarou e o servidor que nao entregou declaracoes que o MCP-CLARO
guarda (502 ``declaracoes_indisponiveis``), e a rodada conta (A5). Senao, um terceiro que declarasse uma ferramenta
com um nome longo demais para o indice do banco ficaria fora da regra das rodadas sem resposta, rodada apos rodada.

- primeira defesa: ``servico._declaracao_invalida`` recusa nome de ferramenta com mais de ``TETO_BYTES_NOME`` bytes
  (bem abaixo do teto de uma entrada do indice da chave primaria de ``ferramentas_vistas``, cerca de 2,7 kB), e texto
  que nao se escreve em UTF-8, como o caractere nulo de antes. Nome com mais de 128 letras que cabe no teto continua
  guardado: o ADMIN o ve, e a revisao o recusa (``telas.admin.TETO_NOME_FERRAMENTA``);
- segunda defesa: erro de dado do banco ao gravar (SQLSTATE das classes 22 e 54) e ``DeclaracoesIndisponiveis``.

Prova do verificador: ``R/v6e/repo/tests/integracao/test_v6e_nome_longo.py`` (aqui, invertida, tambem pela sala).
"""

from __future__ import annotations

import random
import string
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import servico
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.mudou_parou import ResultadoMudouParou, conferir_terceiro
from tests import apoio_g3a as g
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

CONSULTAR = {"name": "consultar", "description": "x", "inputSchema": {"type": "object"}}
_RNG = random.Random(7)
LONGO = "".join(_RNG.choice(string.ascii_letters + string.digits) for _ in range(4000))
"""Letras ao acaso, que nao se comprimem: a entrada do indice passa do teto do banco."""


def _ferramenta(nome: str) -> dict[str, Any]:
    return {"name": nome, "description": "x", "inputSchema": {"type": "object"}}


def _terceiro_que_declara(*nomes: str) -> Any:
    ferramentas = [CONSULTAR, *(_ferramenta(n) for n in nomes)]
    return g.sistema_cru(
        lambda p: {"result": {"tools": ferramentas, "resultType": "complete", "ttlMs": 0, "cacheScope": "private"}}
    )


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    e = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    yield e
    await e.dispose()


@pytest.fixture
async def desp_db(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    e = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    yield e
    await e.dispose()


async def _vistas(dono: AsyncEngine, sistema_id: Any) -> list[str]:
    async with dono.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT nome FROM ferramentas_vistas WHERE sistema_id = :s ORDER BY nome"), {"s": sistema_id}
                )
            ).scalars()
        )


async def test_nome_longo_demais_e_o_servidor_que_nao_entregou_e_a_rodada_conta(
    banco_migrado: Parametros, dono: AsyncEngine, desp_db: AsyncEngine
) -> None:
    """A prova do verificador, invertida: 502 ``declaracoes_indisponiveis`` e quarentena na segunda rodada."""
    cifra = CifraFalsa()
    rotinas_db = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    try:
        async with servidor(_terceiro_que_declara(LONGO)) as base:
            arr = await g.preparar(dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro")
            assert arr.revisao_id is not None
            app = criar_app_despachante(
                banco_migrado.model_copy(update={"APP_ENV": "teste"}),
                desp_db,
                cifra,
                local_para_terceiro=g.LOCAL_DE_TESTE,
                liberar_locais=True,
            )
            transporte = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante.teste:9443") as http:
                with capture_logs() as logs:
                    r = await http.post("/interno/declaracoes", json={"sistema_id": str(arr.sistema_id)})
                p = banco_migrado.model_copy(update={"DISPATCHER_INTERNAL_URL": "http://despachante.teste:9443"})
                fonte = ClienteDespachante(p, http=http)
                situacoes: list[tuple[str, int]] = []
                for _ in range(2):
                    await conferir_terceiro(
                        rotinas_db,
                        fonte,
                        arr.sistema_id,
                        arr.sistema,
                        arr.revisao_id,
                        [],
                        ResultadoMudouParou(),
                        intervalo=0,
                    )
                    async with dono.connect() as c:
                        s = (
                            await c.execute(
                                text("SELECT situacao, conferencias_sem_resposta FROM sistemas WHERE id = :s"),
                                {"s": arr.sistema_id},
                            )
                        ).one()
                    situacoes.append((s.situacao, s.conferencias_sem_resposta))
    finally:
        await rotinas_db.dispose()
    assert r.status_code == 502, r.text
    assert r.json()["motivo"] == "declaracoes_indisponiveis"
    assert [linha for linha in logs if linha.get("event") == "declaracoes_sem_banco"] == []
    assert situacoes[0] == ("ativo", 1)
    assert situacoes[1][0] == "quarentena"
    assert LONGO not in str(logs)  # o nome do servidor nunca vai ao log


@pytest.mark.parametrize("caminho", ["local", "sala"])
async def test_nome_longo_demais_e_declaracao_invalida_tambem_pela_sala(
    dono: AsyncEngine, desp_db: AsyncEngine, caminho: str
) -> None:
    """``types.Tool`` nao limita o tamanho do nome: a sala deixa passar, e a regra e a mesma do cliente local."""
    cifra = CifraFalsa()
    async with servidor(_terceiro_que_declara(LONGO)) as base, servidor(g.sala_em_processo()) as sala:
        arr = await g.preparar(dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro")
        if caminho == "sala":
            desp = g.despachante(desp_db, cifra, sala=ClienteSala(sala, tls=None), local=False)
        else:
            desp = g.despachante(desp_db, cifra)
        with pytest.raises(servico.DeclaracoesIndisponiveis) as falha:
            await desp.ler_declaracoes(arr.sistema_id, None)
    assert type(falha.value) is servico.DeclaracoesIndisponiveis  # conta: nao e DeclaracoesNaoConferidas
    assert await _vistas(dono, arr.sistema_id) == []  # nada gravado da declaracao recusada


async def test_nome_de_mais_de_128_letras_que_o_banco_guarda_continua_guardado(
    dono: AsyncEngine, desp_db: AsyncEngine
) -> None:
    """O teto e em bytes, no limite: 250 letras de 4 bytes (1000 bytes) passam; 251 (1004) nao."""
    assert servico.TETO_BYTES_NOME == 1000
    cifra = CifraFalsa()
    longo_ascii = "a" * 300
    no_teto = "\U0001f600" * 250
    async with servidor(_terceiro_que_declara(longo_ascii, no_teto)) as base:
        arr = await g.preparar(dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro")
        lidas = await g.despachante(desp_db, cifra).ler_declaracoes(arr.sistema_id, None)
    assert {f["name"] for f in lidas} == {"consultar", longo_ascii, no_teto}
    assert set(await _vistas(dono, arr.sistema_id)) == {"consultar", longo_ascii, no_teto}
    async with servidor(_terceiro_que_declara("\U0001f600" * 251)) as base:
        arr = await g.preparar(dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro")
        with pytest.raises(servico.DeclaracoesIndisponiveis):
            await g.despachante(desp_db, cifra).ler_declaracoes(arr.sistema_id, None)
    assert await _vistas(dono, arr.sistema_id) == []


async def test_erro_de_dado_do_banco_ao_gravar_e_o_servidor_que_nao_entregou(
    dono: AsyncEngine, desp_db: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segunda defesa, sozinha: sem o teto do nome (como se a primeira deixasse passar um caso), o banco recusa a
    linha (SQLSTATE 54000, entrada de indice grande demais), e isso e ``DeclaracoesIndisponiveis``, nunca a falha
    nossa (que viraria 503 e nao contaria). O log leva so o tipo do erro e o SQLSTATE."""
    monkeypatch.setattr(servico, "TETO_BYTES_NOME", 10**6)
    cifra = CifraFalsa()
    async with servidor(_terceiro_que_declara(LONGO)) as base:
        arr = await g.preparar(dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro")
        with capture_logs() as logs, pytest.raises(servico.DeclaracoesIndisponiveis) as falha:
            await g.despachante(desp_db, cifra).ler_declaracoes(arr.sistema_id, None)
    assert type(falha.value) is servico.DeclaracoesIndisponiveis
    recusas = [linha for linha in logs if linha.get("event") == "declaracoes_que_o_banco_nao_guarda"]
    assert recusas == [
        {
            "event": "declaracoes_que_o_banco_nao_guarda",
            "sistema": arr.sistema,
            "erro": "DBAPIError",
            "sqlstate": "54000",
            "log_level": "warning",
        }
    ]
    assert await _vistas(dono, arr.sistema_id) == []
