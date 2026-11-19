"""Chave ``FEATURE_PHASE2_ACT`` para pessoas reais (``teste = false``), de ponta a ponta pelo aplicativo.

Desligada (o padrao), a vitrine da pessoa real nao mostra acoes nem a consulta pelo protocolo, e o pedido de
uma acao e recusado com ``acoes_desligadas``: nada vira protocolo e o sistema nao e chamado. Pessoas de teste
seguem com as acoes valendo. Ligada, a mesma pessoa real ve e pede as acoes.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.saida.pedidos import CONSULTA
from mcp_claro.versao import PREFIXO_META
from tests.apoio import cliente_mcp
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import ACOES, ambiente_de_acoes, chamadas, chamar, meta, politica

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MOTIVO = PREFIXO_META + "motivo"


async def pessoa_real(amb: Ambiente, pessoa_id: Any) -> None:
    async with amb.admin.begin() as c:
        await c.execute(text("UPDATE pessoas SET teste = false WHERE id = :p"), {"p": pessoa_id})


async def operacoes_de(amb: Ambiente, pessoa_id: Any) -> int:
    async with amb.admin.connect() as c:
        return int(
            (
                await c.execute(text("SELECT count(*) FROM operacoes WHERE pessoa_id = :p"), {"p": pessoa_id})
            ).scalar_one()
        )


async def recusa(amb: Ambiente, nome: str, argumentos: dict[str, Any]) -> MCPError:
    erro: MCPError | None = None
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha("pessoa-ana")) as c:
        try:
            await c.call_tool(nome, argumentos)
        except MCPError as e:
            erro = e
    assert erro is not None, f"{nome} devia ser recusada"
    return erro


@pytest.fixture(params=[False, True], ids=["acoes_desligadas", "acoes_ligadas"])
async def amb(request: pytest.FixtureRequest, banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> Any:
    extra = {"FEATURE_PHASE1_READ": True, "FEATURE_PHASE2_ACT": request.param}
    async with ambiente_de_acoes(banco_migrado, monkeypatch, extra=extra) as a:
        await politica(a, CONFIRM_CAP_PER_HOUR=1000)
        await pessoa_real(a, a.ana_id)  # a Bia segue de teste
        try:
            yield a
        finally:
            async with a.admin.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = 'teste-saida'"))


async def test_chave_da_fase2_vale_para_pessoas_reais(amb: Ambiente) -> None:
    ligada = amb.p.FEATURE_PHASE2_ACT
    acoes = {amb.vitrine[original] for original, *_ in ACOES}

    vitrine_ana = set(await amb.listar("pessoa-ana"))
    vitrine_bia = set(await amb.listar("pessoa-bia"))
    # A pessoa de teste ve as acoes e a consulta com a chave ligada ou desligada.
    assert acoes | {CONSULTA} <= vitrine_bia

    if not ligada:
        assert vitrine_ana.isdisjoint(acoes), vitrine_ana
        assert CONSULTA not in vitrine_ana

        erro = await recusa(amb, amb.vitrine["criar_chamado"], {"titulo": "Pessoa real, chave desligada"})
        assert erro.error.data[MOTIVO] == "acoes_desligadas"
        assert "Acoes ainda nao estao ligadas" in erro.error.message
        assert await operacoes_de(amb, amb.ana_id) == 0
        assert chamadas(amb, "criar_chamado") == 0
        async with amb.admin.connect() as c:
            frase = (
                await c.execute(
                    text("SELECT frase FROM extrato WHERE tipo = 'acao_recusada' AND pessoa_id = :p"),
                    {"p": amb.ana_id},
                )
            ).scalar_one()
        assert "acoes_desligadas" in frase

        # A consulta pelo protocolo tambem nao existe para ela.
        consulta = await recusa(amb, CONSULTA, {"protocolo": "CLR-2026-00000001"})
        assert consulta.error.data[MOTIVO] == "fora_da_vitrine"

        # A pessoa de teste, no mesmo aplicativo e com a chave desligada, segue pedindo.
        r = await chamar(amb, "criar_chamado", {"titulo": "Pessoa de teste"}, sujeito="pessoa-bia")
        assert meta(r)[PREFIXO_META + "estado"] == "aguardando_ficha"
        return

    assert acoes | {CONSULTA} <= vitrine_ana
    r = await chamar(amb, "criar_chamado", {"titulo": "Pessoa real, chave ligada"})
    assert not r.is_error
    assert meta(r)[PREFIXO_META + "estado"] == "aguardando_ficha"
    assert await operacoes_de(amb, amb.ana_id) == 1
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha("pessoa-ana")) as c:
        consulta = await c.call_tool(CONSULTA, {"protocolo": meta(r)[PREFIXO_META + "protocolo"]})
    assert not consulta.is_error
    assert meta(consulta)[PREFIXO_META + "estado"] == "aguardando_ficha"
