"""Rotinas da Fase 3 sem banco (G5): quando a conferencia do mudou-parou conta como "o sistema nao respondeu".

``rotinas.despachante.ClienteDespachante.declaracoes`` separa a falha do MCP-CLARO (``ConferenciaNaoFeita``: nao
conta rodada sem resposta de um servidor de terceiros) da falha do sistema (qualquer outra: conta).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
import yaml

from mcp_claro.config.settings import Parametros
from mcp_claro.regras.sinais import MAIOR_JANELA_MINUTOS
from mcp_claro.rotinas import acoes
from mcp_claro.rotinas.despachante import MOTIVOS_DO_MCP_CLARO, ClienteDespachante
from mcp_claro.rotinas.mudou_parou import RODADAS_SEM_RESPOSTA, ConferenciaNaoFeita

pytestmark = pytest.mark.anyio

RAIZ = Path(__file__).resolve().parents[2]
SISTEMA = uuid.UUID(int=7)


def _cliente(responder: Callable[[httpx2.Request], httpx2.Response]) -> ClienteDespachante:
    p = Parametros(DISPATCHER_INTERNAL_URL="http://despachante:9443")
    return ClienteDespachante(p, http=httpx2.AsyncClient(transport=httpx2.MockTransport(responder)))


def _resposta(status: int, corpo: object) -> Callable[[httpx2.Request], httpx2.Response]:
    def responder(pedido: httpx2.Request) -> httpx2.Response:
        assert pedido.url.path == "/interno/declaracoes"
        assert json.loads(pedido.content) == {"sistema_id": str(SISTEMA)}  # nunca uma chave: as rotinas nao tem
        return httpx2.Response(status, json=corpo)

    return responder


async def test_declaracoes_devolve_a_lista_do_despachante() -> None:
    cliente = _cliente(_resposta(200, {"ferramentas": [{"name": "a"}, "lixo", {"name": "b"}]}))
    try:
        assert await cliente.declaracoes(SISTEMA) == [{"name": "a"}, {"name": "b"}]
    finally:
        await cliente.fechar()


@pytest.mark.parametrize(
    ("status", "corpo"),
    [
        (503, {"mensagem": "o despachante esta subindo"}),
        (404, {"mensagem": "sistema inativo"}),
        *((502, {"motivo": m, "mensagem": "x"}) for m in sorted(MOTIVOS_DO_MCP_CLARO)),
        (500, {"motivo": "sala_fora"}),
    ],
)
async def test_falha_do_mcp_claro_e_conferencia_nao_feita(status: int, corpo: object) -> None:
    cliente = _cliente(_resposta(status, corpo))
    try:
        with pytest.raises(ConferenciaNaoFeita):
            await cliente.declaracoes(SISTEMA)
    finally:
        await cliente.fechar()


@pytest.mark.parametrize(
    "erro", [httpx2.ConnectError("recusada"), httpx2.ConnectTimeout("prazo"), httpx2.PoolTimeout("pool")]
)
async def test_despachante_que_nao_atende_e_conferencia_nao_feita(erro: Exception) -> None:
    def responder(_: httpx2.Request) -> httpx2.Response:
        raise erro

    cliente = _cliente(responder)
    try:
        with pytest.raises(ConferenciaNaoFeita):
            await cliente.declaracoes(SISTEMA)
    finally:
        await cliente.fechar()


@pytest.mark.parametrize(
    ("status", "corpo"),
    [
        (502, {"motivo": "declaracoes_indisponiveis", "mensagem": "o sistema nao entregou as declaracoes"}),
        (500, "erro interno"),
        (400, {"mensagem": "sistema_id e obrigatorio"}),
        (200, {"sem": "lista"}),
    ],
)
async def test_sistema_que_nao_entregou_conta_como_sem_resposta(status: int, corpo: object) -> None:
    cliente = _cliente(_resposta(status, corpo))
    try:
        with pytest.raises((httpx2.HTTPStatusError, TypeError)) as erro:
            await cliente.declaracoes(SISTEMA)
        assert not isinstance(erro.value, ConferenciaNaoFeita)
    finally:
        await cliente.fechar()


async def test_despachante_lento_depois_de_receber_conta_como_sem_resposta() -> None:
    """O pedido chegou ao despachante e ele nao respondeu a tempo (lendo um servidor lento): conta."""

    def responder(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("prazo da leitura")

    cliente = _cliente(responder)
    try:
        with pytest.raises(httpx2.ReadTimeout):
            await cliente.declaracoes(SISTEMA)
    finally:
        await cliente.fechar()


def test_duas_rodadas_seguidas_levam_a_quarentena() -> None:
    assert RODADAS_SEM_RESPOSTA == 2


def test_limpar_sinais_usa_a_maior_janela_do_alerta_e_o_gatilho_do_banco() -> None:
    """A limpeza apaga so o que o gatilho ``sinais_guarda`` deixa (mais velho que a maior janela, 7 dias)."""
    assert MAIOR_JANELA_MINUTOS == 10080
    assert "make_interval(mins => :minutos)" in acoes.SQL_LIMPAR_SINAIS
    migracao = (RAIZ / "migrations" / "versions" / "0006_fase3.py").read_text("utf-8")
    assert re.search(rf"interval '{MAIOR_JANELA_MINUTOS} minutes'", migracao)


def test_alerta_alto_de_sistema_recusado_nao_conta_terceiro() -> None:
    """D30: a recusa de um servidor de terceiros (motivo cadastro_terceiro) fica fora do alerta alto."""
    regras = yaml.safe_load((RAIZ / "grafana" / "provisionamento" / "alerting" / "regras.yaml").read_text("utf-8"))
    (regra,) = (
        r for grupo in regras["groups"] for r in grupo["rules"] if r["uid"] == "mcp-claro-sistema-recusado-versao"
    )
    assert regra["labels"]["severity"] == "alta"
    (consulta,) = (p["model"]["rawSql"] for p in regra["data"] if p["refId"] == "A")
    assert "medicoes.v_recusas_versao_sistema" in consulta
    assert "motivo <> 'cadastro_terceiro'" in consulta


def test_apelido_de_antes_da_fase_3_faz_a_rodada_inteira() -> None:
    from mcp_claro.rotinas import app, mudou_parou

    assert mudou_parou.conferir_sistemas_proprios is mudou_parou.conferir_sistemas
    assert app.conferir_sistemas is mudou_parou.conferir_sistemas
