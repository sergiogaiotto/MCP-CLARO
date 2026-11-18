"""Os sistemas de exemplo falam a 2026-07-28 e se comportam como o plano descreve."""

from __future__ import annotations

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from exemplos import acoes_caixa, chave_compartilhada, duble_fase2, leitura_por_pessoa, mal_comportado
from tests.apoio import cabecalhos_2026, cliente_mcp, pedido_2026, servidor

pytestmark = pytest.mark.anyio


async def test_leitura_por_pessoa_cada_um_ve_o_seu() -> None:
    async with servidor(leitura_por_pessoa.criar()) as base:
        async with cliente_mcp(f"{base}/mcp", "chave-ana") as c:
            nomes = {t.name for t in (await c.list_tools()).tools}
            assert {"consultar_fatura", "listar_chamados", "ver_cadastro"} <= nomes
            r = await c.call_tool("consultar_fatura", {"mes": "2026-10"})
            assert r.structured_content["valor"] == 99.9
        async with cliente_mcp(f"{base}/mcp", "chave-bia") as c:
            r = await c.call_tool("ver_cadastro", {})
            assert r.structured_content["nome"] == "Bia Teste"


async def test_sem_chave_recebe_401() -> None:
    async with servidor(leitura_por_pessoa.criar()) as base, httpx2.AsyncClient() as http:
        r = await http.post(
            f"{base}/mcp", json=pedido_2026("tools/list"), headers=cabecalhos_2026("tools/list")
        )
        assert r.status_code == 401


async def test_discover_anuncia_so_2026() -> None:
    async with servidor(chave_compartilhada.criar()) as base, httpx2.AsyncClient() as http:
        r = await http.post(
            f"{base}/mcp",
            json=pedido_2026("server/discover"),
            headers=cabecalhos_2026("server/discover", chave="chave-compartilhada"),
        )
        assert r.status_code == 200
        assert r.json()["result"]["supportedVersions"] == ["2026-07-28"]


async def test_acoes_contam_uma_chamada_e_nao_veem_cracha_de_aplicativo() -> None:
    app = acoes_caixa.criar()
    async with servidor(app) as base:
        async with cliente_mcp(f"{base}/mcp", "chave-ana") as c:
            r = await c.call_tool("criar_chamado", {"titulo": "Teste de caixa"})
            assert r.structured_content["registro"].startswith("CX-")
        async with httpx2.AsyncClient() as http:
            contagem = (await http.get(f"{base}/contagem")).json()
        assert contagem["chamadas"] == {"criar_chamado": 1}


async def test_mal_comportado_muda_definicao() -> None:
    async with servidor(mal_comportado.criar()) as base:
        async with httpx2.AsyncClient() as http:
            await http.post(f"{base}/modo", json={"modo": "muda_definicao"})
        async with cliente_mcp(f"{base}/mcp", "chave-ana") as c:
            a = (await c.list_tools(cache_mode="refresh")).tools[0].input_schema
            b = (await c.list_tools(cache_mode="refresh")).tools[0].input_schema
            assert a != b


async def test_mal_comportado_erro500() -> None:
    async with servidor(mal_comportado.criar()) as base, httpx2.AsyncClient() as http:
        await http.post(f"{base}/modo", json={"modo": "erro500"})
        r = await http.post(
            f"{base}/mcp",
            json=pedido_2026("tools/list"),
            headers=cabecalhos_2026("tools/list", chave="chave-ana"),
        )
        assert r.status_code == 500


async def test_duble_devolve_protocolo_e_pedido_igual_nao_cria_outro() -> None:
    async with servidor(duble_fase2.criar()) as base, cliente_mcp(f"{base}/mcp", "chave-ana") as c:
        r1 = await c.call_tool("pagar_conta", {"conta": "123", "valor": 10})
        r2 = await c.call_tool("pagar_conta", {"conta": "123", "valor": 10})
        assert r1.structured_content["protocolo"] == r2.structured_content["protocolo"]
        estado = await c.call_tool("consultar_protocolo", {"protocolo": r1.structured_content["protocolo"]})
        assert estado.structured_content["estado"] == "ficha_pendente"
        with pytest.raises(MCPError):
            await c.call_tool("nao_existe", {})
