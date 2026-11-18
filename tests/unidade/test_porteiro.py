"""Porteiro de versao: o que ele responde ao ``initialize`` e como conta cada recusa."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from mcp_claro.entrada.porteiro import PorteiroDeVersao
from mcp_claro.medicoes.contadores import Contadores
from mcp_claro.versao import VERSAO_MCP

pytestmark = pytest.mark.anyio

ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": VERSAO_MCP,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "teste", "version": "0"},
}


async def _kit(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"passou": True}})(scope, receive, send)


async def _pedir(corpo: dict[str, Any], cabecalhos: dict[str, str]) -> tuple[httpx2.Response, Contadores]:
    contadores = Contadores()
    porteiro = PorteiroDeVersao(_kit, max_corpo=1024 * 1024, contadores=contadores)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=porteiro), base_url="http://teste") as http:
        resposta = await http.post("/mcp", json=corpo, headers=cabecalhos)
    return resposta, contadores


async def test_initialize_na_2026_e_metodo_retirado_404_e_conta() -> None:
    corpo = {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {"_meta": ENVELOPE}}
    resposta, contadores = await _pedir(corpo, {"mcp-protocol-version": VERSAO_MCP})
    assert resposta.status_code == 404
    erro = resposta.json()["error"]
    assert erro["code"] == -32601
    assert resposta.json()["id"] == 7
    assert "server/discover" in erro["message"]
    # Nunca "versao nao suportada" citando a propria versao aceita.
    assert "data" not in erro
    assert contadores.recusas_pendentes() == {("aplicativo", "sem-cracha", "geracao-antiga", "initialize"): 1}


async def test_initialize_da_geracao_antiga_com_cabecalho_novo_recebe_32022() -> None:
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
    resposta, contadores = await _pedir(corpo, {"mcp-protocol-version": VERSAO_MCP})
    assert resposta.status_code == 400
    erro = resposta.json()["error"]
    assert erro["code"] == -32022
    assert erro["data"] == {"supported": [VERSAO_MCP], "requested": "2025-11-25"}
    assert contadores.recusas_pendentes() == {("aplicativo", "sem-cracha", "2025-11-25", "initialize"): 1}


async def test_initialize_sem_cabecalho_segue_com_a_recusa_legivel() -> None:
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
    resposta, contadores = await _pedir(corpo, {})
    assert resposta.status_code == 400
    assert resposta.json()["error"]["data"]["supported"] == [VERSAO_MCP]
    assert contadores.recusas_pendentes() == {("aplicativo", "sem-cracha", "2025-11-25", "initialize"): 1}


async def test_pedido_da_2026_passa_ao_kit_sem_contar() -> None:
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": ENVELOPE}}
    resposta, contadores = await _pedir(corpo, {"mcp-protocol-version": VERSAO_MCP})
    assert resposta.json()["result"] == {"passou": True}
    assert contadores.recusas_pendentes() == {}
