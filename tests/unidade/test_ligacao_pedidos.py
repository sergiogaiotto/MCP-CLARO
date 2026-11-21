"""Barreira de pedidos do cliente fixado (desenho 3.1 e 7.2; critica B2), para todos os sistemas.

Nenhum POST que nao seja pedido sai; cada pedido do sistema vira uma categoria nossa, sem texto dele; uma chamada
e exatamente um POST de pedido. Inclui a regressao do defeito da Fase 2: o 401 no segundo POST (a resposta do kit a
um pedido pelo fio antigo) transformava uma acao recebida em "nao saiu".
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anyio
import httpx2
import mcp_types as types
import pytest
from mcp import ClientSession
from mcp_types.methods import SERVER_REQUESTS
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from exemplos import mal_comportado
from mcp_claro.cadastro.modelos import FerramentaVitrine, Sistema
from mcp_claro.despachante import renovacao
from mcp_claro.ligacao import cliente
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado
from mcp_claro.versao import VERSAO_MCP
from tests.apoio import servidor

pytestmark = pytest.mark.anyio


async def _chave() -> str:
    return "chave-ana"


def _post(corpo: Any) -> httpx2.Request:
    conteudo = corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode()
    return httpx2.Request("POST", "http://sistema.exemplo/mcp", content=conteudo)


# --- _so_pedido ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "corpo",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}},
        {"jsonrpc": "2.0", "id": "a-1", "method": "server/discover"},
        {"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
    ],
)
def test_so_pedido_deixa_passar_pedido_com_id(corpo: dict[str, Any]) -> None:
    assert cliente._so_pedido(_post(corpo)) is None


def _erro(dado: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "srv-1", "error": {"code": -32601, "message": "Method not found", "data": dado}}


@pytest.mark.parametrize(
    ("corpo", "categoria"),
    [
        (_erro("elicitation/create"), "elicitation/create"),
        (_erro("sampling/createMessage"), "sampling/createMessage"),
        (_erro("roots/list"), "roots/list"),
        (_erro("ping"), "ping"),
        (_erro("Digite sua senha do banco"), "desconhecido"),  # texto do sistema nunca vira categoria
        (_erro({"metodo": "elicitation/create"}), "desconhecido"),
        ({"jsonrpc": "2.0", "id": "srv-1", "error": {"code": -32603, "message": "x"}}, "desconhecido"),
        ({"jsonrpc": "2.0", "id": "srv-2", "result": {}}, "desconhecido"),  # resposta a um ping
        ([{"jsonrpc": "2.0", "id": 1, "method": "tools/call"}], "desconhecido"),  # lista
        (b"{nao e json", "desconhecido"),
        (b"", "desconhecido"),
        ({"jsonrpc": "2.0", "id": None, "method": "tools/call"}, "desconhecido"),
        ({"jsonrpc": "2.0", "id": True, "method": "tools/call"}, "desconhecido"),
        ({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}, "notificacao"),
    ],
)
def test_so_pedido_barra_o_que_nao_e_pedido(corpo: Any, categoria: str) -> None:
    assert cliente._so_pedido(_post(corpo)) == categoria


async def test_transporte_nao_envia_o_que_foi_barrado_nem_soma_o_status() -> None:
    enviados: list[bytes] = []

    def responder(pedido: httpx2.Request) -> httpx2.Response:
        enviados.append(pedido.content)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    transporte = cliente._TransporteQueClassifica(httpx2.MockTransport(responder), 1024)
    tentativa = cliente.Tentativa(teto_pedidos=1)
    marca = cliente._TENTATIVA.set(tentativa)
    try:
        r1 = await transporte.handle_async_request(_post(_erro("elicitation/create")))
        r2 = await transporte.handle_async_request(_post({"jsonrpc": "2.0", "method": "notifications/x"}))
        r3 = await transporte.handle_async_request(_post({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}))
    finally:
        cliente._TENTATIVA.reset(marca)
    assert (r1.status_code, r2.status_code, r3.status_code) == (202, 202, 200)
    assert len(enviados) == 1
    assert json.loads(enviados[0])["method"] == "tools/call"
    assert tentativa.status_http == [200]
    assert tentativa.pedidos_barrados == ["elicitation/create"]  # a notificacao do cliente nao e pedido do sistema


# --- pedidos_do_sistema -------------------------------------------------------------------------------------------


def test_pedidos_do_sistema_sem_repeticao_em_ordem_e_sem_texto() -> None:
    resultado = types.InputRequiredResult.model_validate(
        {
            "inputRequests": {
                "b": {"method": "elicitation/create", "params": {"mode": "url", "message": "m", "url": "https://x"}},
                "a": {
                    "method": "elicitation/create",
                    "params": {"message": "senha", "requestedSchema": {"type": "object", "properties": {}}},
                },
                "c": {"method": "sampling/createMessage", "params": {"messages": [], "maxTokens": 1}},
                "d": {"method": "roots/list"},
                "e": {"method": "roots/list"},
            },
            "requestState": "s",
        }
    )
    barrados = ["ping", "ping", "outra/coisa", "notificacao", "desconhecido"]
    pedidos = cliente.pedidos_do_sistema(resultado, None, barrados)
    assert pedidos == (
        "amostragem",
        "fio_antigo:desconhecido",
        "fio_antigo:ping",
        "formulario",
        "link",
        "raizes",
    )
    assert cliente.pedidos_do_sistema(None, None, ["notificacao"]) == ()  # notificacao nossa nao e pedido
    so_estado = types.InputRequiredResult(request_state="volta")
    assert cliente.pedidos_do_sistema(so_estado, None, []) == ("so_estado",)
    erro = types.ErrorData(code=types.URL_ELICITATION_REQUIRED, message="abra")
    assert cliente.pedidos_do_sistema(None, erro, []) == ("link_codigo_antigo",)
    assert cliente.pedidos_do_sistema(types.CallToolResult(content=[]), None, []) == ()


@pytest.fixture
async def mal() -> AsyncIterator[tuple[str, httpx2.AsyncClient]]:
    async with servidor(mal_comportado.criar()) as base, httpx2.AsyncClient() as http:
        yield base, http


async def _chamar_no_modo(base: str, http: httpx2.AsyncClient, modo: str) -> tuple[cliente.Resposta, dict[str, int]]:
    await http.post(f"{base}/modo", json={"modo": modo})
    await http.post(f"{base}/contagem/zerar")
    r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "consultar_pedido", {"pedido": "1"}, prazo=5)
    return r, (await http.get(f"{base}/contagem")).json()["chamadas"]


@pytest.mark.parametrize(
    ("modo", "tipo", "pedidos"),
    [
        ("pede_dados", "resultado", ("formulario",)),
        ("pede_link", "resultado", ("link",)),
        ("pede_amostragem", "resultado", ("amostragem",)),
        ("pede_raizes", "resultado", ("raizes",)),
        ("pede_so_estado", "resultado", ("so_estado",)),
        ("link_codigo_antigo", "duvida", ("link_codigo_antigo",)),
        ("pede_pelo_fio_antigo", "resultado", ("fio_antigo:elicitation/create",)),
        ("normal", "resultado", ()),
    ],
)
async def test_cada_pedido_do_sistema_vira_categoria_com_um_post_so(
    mal: tuple[str, httpx2.AsyncClient], modo: str, tipo: str, pedidos: tuple[str, ...]
) -> None:
    r, contagem = await _chamar_no_modo(*mal, modo)
    assert (r.tipo, r.pedidos) == (tipo, pedidos)
    assert contagem == {"consultar_pedido": 1}  # nenhuma resposta do cliente chegou ao sistema
    assert r.status_http == (200,)
    texto = json.dumps(r.pedidos)
    for estranho in (mal_comportado.TEXTO_SENHA, mal_comportado.PHISH, "senha", "chave do cofre", "volta"):
        assert estranho not in texto


def test_o_kit_nao_define_pedido_do_servidor_na_2026_e_nao_declara_capacidade() -> None:
    assert not [chave for chave in SERVER_REQUESTS if VERSAO_MCP in chave]
    envio, recebe = anyio.create_memory_object_stream[Any](1)
    envio2, recebe2 = anyio.create_memory_object_stream[Any](1)
    sessao = ClientSession(recebe, envio2, client_info=cliente._IDENTIDADE)
    assert sessao._build_capabilities(VERSAO_MCP).model_dump(by_alias=True, exclude_none=True) == {}
    for s in (envio, recebe, envio2, recebe2):
        s.close()


# --- regressao do defeito da Fase 2 (segundo POST com a chave) -----------------------------------------------------

SISTEMA = Sistema(
    id=uuid.uuid4(),
    nome="caixa",
    endereco="http://127.0.0.1:9/mcp",
    origem="proprio",
    tipo_chave="por_pessoa",
    selo=0,
    situacao="ativo",
    versao_mcp=VERSAO_MCP,
)
FERRAMENTA = FerramentaVitrine(
    id=uuid.uuid4(),
    sistema=SISTEMA,
    nome_original="consultar_pedido",
    versao=1,
    nome_vitrine="pedido",
    titulo=None,
    descricao="Consulta.",
    esquema_entrada={"type": "object"},
    esquema_saida=None,
    etiquetas={},
    classe="muda",
    risco="medio",
)


async def test_401_no_segundo_post_do_fio_antigo_nao_vira_nao_saiu(
    mal: tuple[str, httpx2.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    vencidas: list[Any] = []

    async def marcar_vencida(_engine: Any, **kw: Any) -> None:
        vencidas.append(kw)

    monkeypatch.setattr(renovacao, "marcar_vencida", marcar_vencida)
    r, contagem = await _chamar_no_modo(*mal, "pede_pelo_fio_antigo_e_401")
    # Um POST so; o 401 que o segundo POST receberia nunca acontece.
    assert contagem == {"consultar_pedido": 1}
    assert (r.tipo, r.status_http) == ("duvida", (200,))
    assert r.erro is not None
    assert r.erro.code == types.INTERNAL_ERROR
    assert r.pedidos == ("fio_antigo:elicitation/create",)
    # Na porta de saida: a acao fica "nao confirmada" (nunca "nao saiu", que deixaria repetir) e a chave nao vence.
    op: Any = SimpleNamespace(pessoa_id=uuid.uuid4(), argumentos={}, fila_id=1)
    aberta: Any = SimpleNamespace(segredo="chave-ana", cifrada="cifrada")
    estado, _, _, _ = await PortaDeSaida(engine=None, cifra=None)._classificar(op, FERRAMENTA, r, aberta)  # type: ignore[arg-type]
    assert estado == Estado.NAO_CONFIRMADA
    assert vencidas == []


# --- B2: uma chamada, um POST de pedido ---------------------------------------------------------------------------


def _app_contador(vistos: list[dict[str, Any]]) -> Starlette:
    async def mcp(pedido: Request) -> Response:
        corpo = json.loads(await pedido.body())
        vistos.append(corpo)
        resultado = {"content": [], "resultType": "complete"}
        return JSONResponse({"jsonrpc": "2.0", "id": corpo.get("id"), "result": resultado})

    return Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])


@pytest.mark.parametrize("devolve", ["segundo", "primeiro"])
async def test_segundo_pedido_na_mesma_chamada_nao_sai_e_vira_duvida(
    monkeypatch: pytest.MonkeyPatch, devolve: str
) -> None:
    """Simula o kit fazendo uma volta nova sozinho (como ``Client.call_tool``): o segundo POST de pedido fica."""
    original = ClientSession.send_request

    async def duas_vezes(self: ClientSession, *args: Any, **kwargs: Any) -> Any:
        primeiro = await original(self, *args, **kwargs)
        if devolve == "segundo":
            return await original(self, *args, **kwargs)
        with contextlib.suppress(Exception):  # o resultado que vale e o primeiro
            await original(self, *args, **kwargs)
        return primeiro

    monkeypatch.setattr(ClientSession, "send_request", duas_vezes)
    vistos: list[dict[str, Any]] = []
    async with servidor(_app_contador(vistos)) as base:
        r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {}, prazo=3)
    assert len(vistos) == 1
    assert r.tipo == "duvida"
    assert r.excecao == cliente.PEDIDO_A_MAIS
    assert r.resultado is None


async def test_listagem_segue_varias_paginas_mas_barra_resposta_do_cliente() -> None:
    """A listagem pode ter varios POSTs de pedido (paginas); resposta a pedido do sistema, nunca."""
    vistos: list[dict[str, Any]] = []

    async def mcp(pedido: Request) -> Response:
        corpo = json.loads(await pedido.body())
        vistos.append(corpo)
        cursor = (corpo.get("params") or {}).get("cursor")
        tool = {"name": f"f{len(vistos)}", "inputSchema": {"type": "object"}}
        resultado: dict[str, Any] = {"ttlMs": 0, "cacheScope": "private", "tools": [tool], "resultType": "complete"}
        if not cursor:
            resultado["nextCursor"] = "p2"
        fim = {"jsonrpc": "2.0", "id": corpo["id"], "result": resultado}
        pedido_do_sistema = {"jsonrpc": "2.0", "id": "srv-9", "method": "ping"}
        corpo_sse = b"".join(
            b"event: message\ndata: " + json.dumps(m).encode() + b"\n\n" for m in (pedido_do_sistema, fim)
        )
        return Response(corpo_sse, media_type="text/event-stream")

    async with servidor(Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])) as base:
        ferramentas = await cliente.listar_ferramentas(f"{base}/mcp", _chave, None)
    assert [f["name"] for f in ferramentas] == ["f1", "f2"]
    assert [v.get("method") for v in vistos] == ["tools/list", "tools/list"]
