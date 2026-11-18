"""Separacao "nao_enviado" x "duvida" no cliente de saida, com servidores locais de verdade."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from mcp_claro.ligacao import chamar_ferramenta, sondar
from tests.apoio import servidor

pytestmark = pytest.mark.anyio


async def _chave() -> str:
    return "chave-teste"


def _app(resposta: Any, vistos: list[dict[str, Any]]) -> Starlette:
    async def mcp(pedido: Request) -> Response:
        vistos.append({"metodo": pedido.method, "cabecalhos": dict(pedido.headers)})
        return resposta() if callable(resposta) else resposta

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])])


@pytest.mark.parametrize(
    ("resposta", "tipo"),
    [
        (lambda: Response('{"jsonrpc": "2.0", "id": 1, "res', media_type="application/json"), "duvida"),
        (lambda: Response(status_code=202), "duvida"),
        (lambda: PlainTextResponse("<html>ok</html>", media_type="text/html"), "duvida"),
        (lambda: PlainTextResponse("erro", status_code=500), "duvida"),
        (lambda: JSONResponse({"error": "invalid_token"}, status_code=401), "recusado_http"),
        (lambda: Response(status_code=307, headers={"Location": "https://outro.exemplo/mcp"}), "recusado_http"),
    ],
)
async def test_classificacao(resposta: Any, tipo: str) -> None:
    vistos: list[dict[str, Any]] = []
    async with servidor(_app(resposta, vistos)) as base:
        r = await chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {"a": 1}, prazo=3)
    assert r.tipo == tipo
    assert [v["metodo"] for v in vistos] == ["POST"]  # um POST so, sem GET de retomada nem reenvio


async def test_porta_fechada_e_nao_enviado() -> None:
    r = await chamar_ferramenta("http://127.0.0.1:1/mcp", _chave, None, "x", {})
    assert r.tipo == "nao_enviado"


async def test_resposta_grande_demais_vira_duvida() -> None:
    grande = '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"' + "x" * 50_000 + '"}]}}'
    vistos: list[dict[str, Any]] = []
    async with servidor(_app(lambda: Response(grande, media_type="application/json"), vistos)) as base:
        r = await chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {}, teto_resposta=10_000)
    assert r.tipo == "duvida"


async def test_cabecalhos_mcp_param_vao_para_o_sistema() -> None:
    vistos: list[dict[str, Any]] = []
    corpo = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "resultType": "complete"}}
    async with servidor(_app(lambda: JSONResponse(corpo), vistos)) as base:
        esquema = {"type": "object", "properties": {"regiao": {"type": "string", "x-mcp-header": "Regiao"}}}
        await chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {"regiao": "sul"}, esquema_declarado=esquema)
    assert vistos[0]["cabecalhos"].get("mcp-param-regiao") == "sul"
    assert vistos[0]["cabecalhos"]["authorization"] == "Bearer chave-teste"


@pytest.mark.parametrize("status", [401, 403, 429, 502, 503])
async def test_sondagem_sem_resposta_de_versao_nao_recusa_por_versao(status: int) -> None:
    async with servidor(_app(lambda: PlainTextResponse("x", status_code=status), [])) as base:
        s = await sondar(f"{base}/mcp", _chave)
    assert s.aceito is False
    assert s.inalcancavel is True
