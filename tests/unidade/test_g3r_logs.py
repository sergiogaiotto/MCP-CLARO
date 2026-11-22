"""Bytes do sistema nunca chegam ao log (G3r, etapa 4r, item 9).

O kit do MCP faz ``logger.exception('Error parsing JSON response')`` com um pedaco do que o sistema mandou (na
excecao do pydantic), escreve o rastro do ``ValidationError`` de uma notificacao que nao aceita (em
``logging.getLogger("client")``) e monta outras mensagens com o que veio (tipo de conteudo, evento SSE, metodo).
``mcp_claro.logs`` deixa, nos registradores do kit no caminho do cliente fixado, so o fato: a mensagem fixa e o tipo
da excecao.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from mcp.client import session as kit_sessao
from mcp.client import streamable_http as kit_http
from starlette.types import Receive, Scope, Send

from mcp_claro import logs
from mcp_claro.ligacao import cliente
from tests.apoio import servidor

pytestmark = pytest.mark.anyio

MARCA = "SEGREDO-DO-SISTEMA-7f3a"


async def _chave() -> str:
    return "chave-ana"


def _sistema_que_responde(corpo: bytes, tipo: str) -> Any:
    """Sistema de teste que responde a todo POST com ``corpo`` e o ``content-type`` dado (sem o kit)."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                mensagem = await receive()
                if mensagem["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif mensagem["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        mais = True
        while mais:
            mais = bool((await receive()).get("more_body"))
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", tipo.encode("latin-1"))]}
        )
        await send({"type": "http.response.body", "body": corpo})

    return app


def _tudo_no_log(caplog: pytest.LogCaptureFixture) -> str:
    formatador = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(formatador.format(r) + "\n" + (r.exc_text or "") for r in caplog.records)


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
async def test_resposta_json_quebrada_nao_poe_os_bytes_do_sistema_no_log(
    caplog: pytest.LogCaptureFixture, terceiro: bool
) -> None:
    corpo = b'{"jsonrpc": "2.0", "id": 0, "result": {"content": [{"type": "text", "text": "' + MARCA.encode()
    caplog.set_level(logging.DEBUG)
    async with servidor(_sistema_que_responde(corpo, "application/json")) as base:
        regras: dict[str, Any] = {"terceiro": True, "ambiente": "teste", "liberar_locais": True} if terceiro else {}
        r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "ler", {}, prazo=3, **regras)
    assert r.tipo == "duvida"  # nada mudou na classificacao
    do_kit = [rec for rec in caplog.records if rec.name == "mcp.client.streamable_http"]
    assert "Error parsing JSON response (ValidationError)" in [rec.getMessage() for rec in do_kit]
    assert all(rec.exc_info is None and rec.exc_text is None for rec in do_kit)
    assert MARCA not in _tudo_no_log(caplog)


async def test_mensagem_montada_com_o_que_o_sistema_mandou_vira_so_o_fato(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with servidor(_sistema_que_responde(b"x", f"text/{MARCA}")) as base:
        await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "ler", {}, prazo=3)
    mensagens = [rec.getMessage() for rec in caplog.records if rec.name == "mcp.client.streamable_http"]
    assert logs.FATO_SEM_TEXTO in mensagens  # era f"Unexpected content type: {content_type}"
    assert MARCA not in _tudo_no_log(caplog)


def _sistema_que_avisa_antes_de_responder() -> Any:
    """Sistema de teste que responde ao ``tools/call`` em SSE com duas notificacoes que o kit nao aceita, ambas com
    a marca, e depois o resultado: ``notifications/message`` com um ``level`` invalido (o kit escreve, em WARNING,
    "Failed to validate notification" com o rastro do ``ValidationError``, que traz o valor) e uma notificacao de
    metodo desconhecido (em DEBUG, "dropped %r", com o nome do metodo)."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
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
            parte = await receive()
            corpo += parte.get("body", b"")
            mais = bool(parte.get("more_body"))
        pedido = json.loads(corpo)
        mensagens = [
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": MARCA, "data": MARCA}},
            {"jsonrpc": "2.0", "method": f"notifications/{MARCA}", "params": {"x": MARCA}},
            {"jsonrpc": "2.0", "id": pedido["id"], "result": {"content": [], "resultType": "complete"}},
        ]
        sse = b"".join(b"event: message\ndata: " + json.dumps(m).encode() + b"\n\n" for m in mensagens)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": sse})

    return app


def test_os_registradores_filtrados_sao_os_que_o_kit_usa() -> None:
    """O nome que vale e o do registrador do kit (o de ``session.py`` e ``client``, nao o nome do modulo)."""
    assert {kit_sessao.logger.name, kit_http.logger.name} <= set(logs.LOGGERS_DO_KIT)


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
async def test_notificacao_que_o_kit_recusa_nao_poe_os_bytes_do_sistema_no_log(
    caplog: pytest.LogCaptureFixture, terceiro: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    async with servidor(_sistema_que_avisa_antes_de_responder()) as base:
        regras: dict[str, Any] = {"terceiro": True, "ambiente": "teste", "liberar_locais": True} if terceiro else {}
        r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "ler", {}, prazo=3, **regras)
    assert r.tipo == "resultado"  # nada mudou na classificacao
    da_sessao = [rec for rec in caplog.records if rec.name == "client"]
    assert [rec.getMessage() for rec in da_sessao] == [
        "Failed to validate notification: ? (ValidationError)",
        "dropped ?: not defined at ?",
    ]
    assert all(rec.exc_info is None and rec.exc_text is None and rec.stack_info is None for rec in da_sessao)
    assert MARCA not in _tudo_no_log(caplog)


def test_o_filtro_entra_uma_vez_so_e_nao_toca_os_outros_registradores(caplog: pytest.LogCaptureFixture) -> None:
    logs.calar_o_kit()
    logs.calar_o_kit()
    for nome in logs.LOGGERS_DO_KIT:
        assert sum(isinstance(f, logs.SoOFato) for f in logging.getLogger(nome).filters) == 1
    assert sum(isinstance(f, logs.SemFraseDoStatus) for f in logging.getLogger("httpx2").filters) == 1
    for nome in logs.LOGGERS_DO_HTTPCORE:
        assert sum(isinstance(f, logs.SoOEvento) for f in logging.getLogger(nome).filters) == 1
    caplog.set_level(logging.INFO)
    logging.getLogger("mcp_claro.teste").info("mensagem %s", "nossa")
    assert [rec.getMessage() for rec in caplog.records] == ["mensagem nossa"]


async def test_frase_do_status_e_cabecalhos_do_sistema_nao_vao_ao_log(caplog: pytest.LogCaptureFixture) -> None:
    """O ``httpx2`` escreve em INFO a frase do status ("HTTP/1.1 200 <frase>"), que o servidor escolhe; o
    ``httpcore2``, em DEBUG, os cabecalhos da resposta."""

    async def atender(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        cabecalho = await leitor.readuntil(b"\r\n\r\n")
        tamanho = next(
            int(linha.split(b":", 1)[1])
            for linha in cabecalho.split(b"\r\n")
            if linha.lower().startswith(b"content-") and b"length" in linha.lower()
        )
        pedido = json.loads(await leitor.readexactly(tamanho))
        corpo = json.dumps(
            {"jsonrpc": "2.0", "id": pedido["id"], "result": {"content": [], "resultType": "complete"}}
        ).encode()
        escritor.write(
            b"HTTP/1.1 200 "
            + MARCA.encode()
            + b"\r\ncontent-type: application/json\r\nx-do-sistema: "
            + MARCA.encode()
            + b"\r\ncontent-length: %d\r\nconnection: close\r\n\r\n" % len(corpo)
            + corpo
        )
        await escritor.drain()
        escritor.close()

    caplog.set_level(logging.DEBUG)
    srv = await asyncio.start_server(atender, "127.0.0.1", 0)
    try:
        url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}/mcp"
        r = await cliente.chamar_ferramenta(url, _chave, None, "ler", {}, prazo=3)
    finally:
        srv.close()
        await srv.wait_closed()
    assert r.tipo == "resultado"
    pedidos = [rec.getMessage() for rec in caplog.records if rec.name == "httpx2"]
    assert pedidos == [f'HTTP Request: POST {url} "HTTP/1.1 200 "']
    assert "receive_response_headers.complete" in [rec.getMessage() for rec in caplog.records]
    assert MARCA not in _tudo_no_log(caplog)
