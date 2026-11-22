"""R6e-2, achado junto: o pedido do sistema que chega colado ao resultado tambem e visto (D12).

O kit responde a um pedido do sistema pelo fio antigo (SSE) numa tarefa a parte. Quando o sistema manda o pedido e o
resultado no mesmo corpo, sem esperar, o resultado chega antes de a resposta do kit sair (e ser barrada): os pedidos
da ``Resposta`` (e da ``Sondagem`` e da ``Listagem``) eram lidos cedo demais e vinham vazios, e uma leitura passava o
resultado ao aplicativo como se nada tivesse sido pedido. Agora sao lidos depois de a sessao fechar.

O exemplo ``mal_comportado`` espera 0,3 s entre o pedido e o resultado (``atraso_fio``); aqui, nada de espera.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.types import Receive, Scope, Send

from mcp_claro.ligacao import cliente, sala_contrato
from tests.apoio import servidor

pytestmark = pytest.mark.anyio

VEZES = 20
"""A corrida dependia da ordem das tarefas do kit: varias chamadas seguidas, para nao passar por sorte."""


class ColaPedidoNoResultado:
    """Responde todo pedido em SSE com um ``elicitation/create`` e, no mesmo corpo, o resultado."""

    def __init__(self) -> None:
        self.respostas_do_cliente = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                mensagem = await receive()
                if mensagem["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif mensagem["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        corpo = b""
        mais = True
        while mais:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            mais = bool(mensagem.get("more_body"))
        pedido = json.loads(corpo or b"{}")
        if "method" not in pedido or "id" not in pedido:
            self.respostas_do_cliente += 1
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        resultados = {
            "tools/call": {"content": [{"type": "text", "text": "feito"}], "resultType": "complete"},
            "tools/list": {"tools": [], "resultType": "complete", "ttlMs": 0, "cacheScope": "private"},
        }
        eventos = [
            {
                "jsonrpc": "2.0",
                "id": "srv-1",
                "method": "elicitation/create",
                "params": {"mode": "form", "message": "senha", "requestedSchema": {"type": "object", "properties": {}}},
            },
            {"jsonrpc": "2.0", "id": pedido["id"], "result": resultados[pedido["method"]]},
        ]
        saida = b"".join(b"event: message\ndata: " + json.dumps(e).encode() + b"\n\n" for e in eventos)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": saida})


async def _sem_chave() -> str | None:
    return None


REGRAS: dict[str, Any] = {"terceiro": True, "ambiente": "teste", "liberar_locais": True}


async def test_chamada_com_o_pedido_colado_ao_resultado_conta_o_pedido() -> None:
    app = ColaPedidoNoResultado()
    async with servidor(app) as base:
        respostas = [
            await cliente.chamar_ferramenta(f"{base}/mcp", _sem_chave, None, "x", {}, **REGRAS) for _ in range(VEZES)
        ]
    assert {(r.tipo, r.pedidos) for r in respostas} == {("resultado", ("fio_antigo:elicitation/create",))}
    assert app.respostas_do_cliente == 0  # a barreira segurou todas


async def test_listagem_com_o_pedido_colado_ao_resultado_conta_o_pedido() -> None:
    app = ColaPedidoNoResultado()
    async with servidor(app) as base:
        listagens = [await cliente.listar(f"{base}/mcp", _sem_chave, None, **REGRAS) for _ in range(VEZES)]
    assert {lst.pedidos for lst in listagens} == {("fio_antigo:elicitation/create",)}
    assert app.respostas_do_cliente == 0


async def test_pela_sala_a_chamada_com_o_pedido_colado_leva_o_pedido_no_contrato() -> None:
    """O que o filho da sala roda (``sala_contrato.atender``): o pedido vai no corpo do contrato."""
    app = ColaPedidoNoResultado()
    async with servidor(app) as base:
        pedido = sala_contrato.PedidoChamar(endereco=f"{base}/mcp", nome="x", argumentos={}).para_json()
        status, corpo = await sala_contrato.atender(
            sala_contrato.ROTA_CHAMAR, pedido, ambiente="teste", liberar_locais=True
        )
    r = sala_contrato.ler_resposta_chamar(status, corpo)
    assert (r.tipo, r.pedidos) == ("resultado", ("fio_antigo:elicitation/create",))
    assert app.respostas_do_cliente == 0
