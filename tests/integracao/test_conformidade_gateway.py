"""Arranjo da suite oficial atraves do gateway (conformidade/gateway.py), conferido pelo cliente oficial.

Sobe o mesmo arranjo da rodada da suite: PostgreSQL 18, provedor de login falso, o fixture
exemplos/conformidade.py como sistema, despachante, entrada e o proxy so de teste que poe o cracha da
pessoa de teste. A suite inteira roda no CI (job "suite oficial atraves do gateway"); aqui so uma
checagem rapida de que algumas ferramentas do fixture chegam pelo gateway como a suite espera.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import mcp_types as types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from conformidade.gateway import Arranjo, subir_arranjo
from exemplos import conformidade as fixture
from mcp_claro.config.settings import Parametros
from tests.apoio import cabecalhos_2026, cliente_mcp, pedido_2026

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def arranjo(banco_migrado: Parametros, tmp_path: Path) -> AsyncIterator[Arranjo]:
    async with subir_arranjo(banco_migrado, tmp_path) as a:
        yield a


def _textos(r: types.CallToolResult) -> list[str]:
    return [b.text for b in r.content if isinstance(b, types.TextContent)]


async def test_vitrine_tem_as_ferramentas_do_fixture_com_nome_e_descricao_originais(arranjo: Arranjo) -> None:
    async with cliente_mcp(arranjo.url_mcp) as c:
        ferramentas = (await c.list_tools()).tools
    assert sorted(t.name for t in ferramentas) == sorted(arranjo.ferramentas)
    assert {"test_simple_text", "test_error_handling", "test_embedded_resource"} <= set(arranjo.ferramentas)
    simples = next(t for t in ferramentas if t.name == "test_simple_text")
    assert simples.description == "Devolve um texto simples."
    assert simples.annotations is not None
    assert simples.annotations.read_only_hint is True


async def test_texto_erro_e_recurso_embutido_chegam_pelo_gateway(arranjo: Arranjo) -> None:
    async with cliente_mcp(arranjo.url_mcp) as c:
        texto = await c.call_tool("test_simple_text", {})
        erro = await c.call_tool("test_error_handling", {})
        embutido = await c.call_tool("test_embedded_resource", {})
    assert texto.is_error is False
    assert _textos(texto) == ["This is a simple text response for testing."]
    assert texto.meta
    assert texto.meta["br.com.claro.mcp-claro/sistema"].startswith("exemplo-conformidade-")
    assert erro.is_error is True
    assert _textos(erro) == ["This tool intentionally returns an error for testing"]
    recurso = embutido.content[0]
    assert isinstance(recurso, types.EmbeddedResource)
    assert isinstance(recurso.resource, types.TextResourceContents)
    assert recurso.resource.text == "This is an embedded resource content."


async def test_imagem_sai_pela_leitura_com_medida(arranjo: Arranjo) -> None:
    # Falha esperada tools-call-image, por desenho: imagem nao tem mascara e e retirada, com aviso.
    async with cliente_mcp(arranjo.url_mcp) as c:
        r = await c.call_tool("test_image_content", {})
    assert not any(isinstance(b, types.ImageContent) for b in r.content)
    assert _textos(r) == ["1 parte(s) sem texto foram retiradas pela leitura com medida."]
    assert fixture.PNG_B64 not in str(r.model_dump())


async def test_sem_o_proxy_nao_ha_cracha(arranjo: Arranjo) -> None:
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{arranjo.url_entrada}/mcp",
            json=pedido_2026("tools/list"),
            headers=cabecalhos_2026("tools/list") | {"host": "mcp-claro.localhost"},
        )
    assert r.status_code == 401


async def test_capacidade_que_o_aplicativo_nao_declarou_vira_32021(arranjo: Arranjo) -> None:
    # O fixture exige sampling; o gateway, como cliente, nao declara nada e o sistema responde -32021.
    async with cliente_mcp(arranjo.url_mcp) as c:
        with pytest.raises(MCPError) as recusa:
            await c.call_tool("test_missing_capability", {})
    assert recusa.value.error.code == types.MISSING_REQUIRED_CLIENT_CAPABILITY
    assert recusa.value.error.data == {"requiredCapabilities": {"sampling": {}}}

    async def nunca(*_: Any) -> types.CreateMessageResult:
        raise AssertionError("o MCP-CLARO nao repassa pedidos do sistema ao aplicativo")

    # Com sampling declarado, o gateway nao repassa o pedido: devolve um erro de ferramenta legivel.
    async with (
        httpx2.AsyncClient(follow_redirects=False, timeout=10) as http,
        Client(streamable_http_client(arranjo.url_mcp, http_client=http), sampling_callback=nunca) as c,
    ):
        r = await c.call_tool("test_missing_capability", {})
    assert r.is_error is True
    assert "nao repassa" in " ".join(_textos(r))


async def test_input_responses_null_e_recusado(arranjo: Arranjo) -> None:
    params = {"name": "test_simple_text", "arguments": {}, "inputResponses": None}
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            arranjo.url_mcp,
            json=pedido_2026("tools/call", params),
            headers=cabecalhos_2026("tools/call", "test_simple_text"),
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == types.INVALID_PARAMS
    assert "inputResponses" in r.json()["error"]["message"]
