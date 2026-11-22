"""R6e-3, retoque: a sala de visitas cheia, dita pelo processo pai, pelo despachante de verdade e pelas rotinas.

Com a unica vaga da sala tomada por um terceiro lento (X), o terceiro Y, que nada fez:

- na leitura das declaracoes, ``POST /interno/declaracoes`` responde 503 com o motivo ``sala_sem_vaga``, e duas
  rodadas do mudou-parou nao o levam a quarentena: a rodada nao foi feita (defeito 3 do verificador). Antes, era 502
  ``declaracoes_indisponiveis``, e duas rodadas com a sala cheia levavam Y a quarentena ("nao respondeu a
  conferencia");
- na sondagem de versao, ``sala_sem_vaga{onde="sondagem"}`` soma, a situacao nao muda e nao ha linha da sala
  (defeito 4 do verificador).

Nada de Y chega a um filho. A sala e a de verdade (processo pai, mTLS, filhos por ``spawn``), com uma vaga so e
``prazo_vaga`` curto, so para o teste nao demorar.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import app as app_despachante
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.ligacao import sala_contrato
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.mudou_parou import ResultadoMudouParou, conferir_terceiro
from tests import apoio_g3a as g
from tests import apoio_visitas
from tests.apoio_visitas import Certificados
from tests.cifra_falsa import CifraFalsa
from tests.unidade.test_r6e_sala_vagas import _ocupar
from tests.unidade.test_visitas_app import _pedidos_do_espiao, _sala_no_ar

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

Y = "https://outro-terceiro.exemplo.teste/mcp"
CONSULTAR = {"name": "consultar", "description": "x", "inputSchema": {"type": "object"}}


@pytest.fixture(scope="module")
def certificados(tmp_path_factory: pytest.TempPathFactory) -> Certificados:
    return apoio_visitas.gerar_certificados(tmp_path_factory.mktemp("certificados_r6e_cheia_integracao"))


@pytest.fixture
def espiao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pasta = tmp_path / "espiao"
    pasta.mkdir()
    monkeypatch.setenv(apoio_visitas.ESPIAO, str(pasta))
    return pasta


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    e = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    yield e
    await e.dispose()


def _contador(nome: str, **rotulos: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    alvo = json.dumps(rotulos, sort_keys=True, ensure_ascii=False)
    return sum(n for (_, nome_, r), n in medidas.items() if nome_ == nome and r == alvo)


async def _situacao(dono: AsyncEngine, sistema_id: Any) -> tuple[str, int]:
    async with dono.connect() as c:
        s = (
            await c.execute(
                text("SELECT situacao, conferencias_sem_resposta FROM sistemas WHERE id = :s"), {"s": sistema_id}
            )
        ).one()
    return s.situacao, s.conferencias_sem_resposta


async def test_sala_cheia_nas_declaracoes_e_na_sondagem_nao_conta_contra_o_terceiro(
    banco_migrado: Parametros,
    dono: AsyncEngine,
    certificados: Certificados,
    espiao: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cifra = CifraFalsa()
    arr = await g.preparar(dono, cifra, endereco=Y, definicao=CONSULTAR, origem="terceiro")
    assert arr.revisao_id is not None
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    rotinas_db = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    try:
        async with _sala_no_ar(
            certificados, alvo=apoio_visitas.atende_devagar, max_chamadas=1, prontos=1, prazo_vaga=0.3
        ) as (cs, p, _sala):
            # O despachante de verdade, com a sala de verdade (a que ``VISITS_INTERNAL_URL`` montaria).
            monkeypatch.setattr(app_despachante, "montar_sala", lambda _p: cs)
            app = criar_app_despachante(banco_migrado.model_copy(update={"APP_ENV": "teste"}), desp_db, cifra)
            primeira = await _ocupar(cs, p, espiao, 6.0)
            antes = _contador("sala_sem_vaga", onde="declaracoes")
            transporte = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante.teste:9443") as http:
                r = await http.post("/interno/declaracoes", json={"sistema_id": str(arr.sistema_id)})
                fonte = ClienteDespachante(
                    banco_migrado.model_copy(update={"DISPATCHER_INTERNAL_URL": "http://despachante.teste:9443"}),
                    http=http,
                )
                resultados: list[ResultadoMudouParou] = []
                for _ in range(2):
                    resultado = ResultadoMudouParou()
                    await conferir_terceiro(
                        rotinas_db, fonte, arr.sistema_id, arr.sistema, arr.revisao_id, [], resultado, intervalo=0
                    )
                    resultados.append(resultado)
            declaracoes = _contador("sala_sem_vaga", onde="declaracoes") - antes
            # A sondagem de versao, com a sala ainda cheia.
            antes_sondagem = _contador("sala_sem_vaga", onde="sondagem")
            sondagem = await g.despachante(desp_db, cifra, sala=cs, local=False).sondar_sistema(arr.sistema_id, None)
            sondagens = _contador("sala_sem_vaga", onde="sondagem") - antes_sondagem
            assert await primeira == 200
    finally:
        await desp_db.dispose()
        await rotinas_db.dispose()
    assert r.status_code == 503, r.text
    assert r.json() == {
        "motivo": sala_contrato.SALA_SEM_VAGA,
        "mensagem": f"a sala de visitas estava cheia; nada foi pedido ao sistema {arr.sistema}",
    }
    assert [x.quarentenas for x in resultados] == [[], []]
    assert [x.sem_resposta for x in resultados] == [[arr.sistema], [arr.sistema]]  # nao conferido, nao contado
    assert await _situacao(dono, arr.sistema_id) == ("ativo", 0)
    assert declaracoes == 3  # o POST e as duas rodadas
    assert (sondagem.desfecho, sondagem.excecao, sondagem.motivo) == (
        "inalcancavel",
        sala_contrato.SALA_SEM_VAGA,
        sala_contrato.MOTIVO_SALA_SEM_VAGA,
    )
    assert sondagens == 1
    linhas = [linha.tipo for linha in await g.extrato(dono, arr.sistema_id)]
    assert not any(t.startswith("sala_") for t in linhas), linhas  # sala cheia nao e linha da sala
    assert _pedidos_do_espiao(espiao) == {}  # nada de Y chegou a um filho
