"""O fixture da suite de conformidade responde pelo cliente oficial, sem chave, na 2026-07-28.

A suite completa roda em ``conformidade/rodar.sh``; aqui so uma checagem rapida
de que o exemplo sobe e de que algumas ferramentas devolvem o que a suite espera.
"""

from __future__ import annotations

import pytest
from mcp_types import TextContent

from exemplos import conformidade
from tests.apoio import cliente_mcp, servidor

pytestmark = pytest.mark.anyio


async def test_lista_e_texto_simples() -> None:
    async with servidor(conformidade.criar()) as base, cliente_mcp(f"{base}/mcp") as c:
        nomes = [t.name for t in (await c.list_tools()).tools]
        assert {"test_simple_text", "test_error_handling", "test_tool_with_progress"} <= set(nomes)
        assert len(nomes) == len(set(nomes))
        r = await c.call_tool("test_simple_text", {})
        assert not r.is_error
        assert isinstance(r.content[0], TextContent)
        assert r.content[0].text == "This is a simple text response for testing."


async def test_erro_de_ferramenta_vem_como_resultado() -> None:
    async with servidor(conformidade.criar()) as base, cliente_mcp(f"{base}/mcp") as c:
        r = await c.call_tool("test_error_handling", {})
        assert r.is_error is True
        assert isinstance(r.content[0], TextContent)
        assert r.content[0].text


async def test_progresso_chega_em_ordem_pelo_sse() -> None:
    vistos: list[tuple[float, float | None]] = []

    async def ao_progredir(progresso: float, total: float | None, mensagem: str | None) -> None:
        vistos.append((progresso, total))

    async with servidor(conformidade.criar()) as base, cliente_mcp(f"{base}/mcp") as c:
        r = await c.call_tool("test_tool_with_progress", {}, progress_callback=ao_progredir)
        assert not r.is_error
    assert vistos == [(0, 100), (50, 100), (100, 100)]
