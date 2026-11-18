"""Ajudas dos testes: subir apps ASGI de verdade (uvicorn) numa porta livre."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_claro.versao import VERSAO_MCP


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def servidor(app: Any, porta: int | None = None, *, host: str = "127.0.0.1", **tls: Any) -> AsyncIterator[str]:
    """Sobe ``app`` com uvicorn e devolve a URL base (http://127.0.0.1:<porta>).

    ``tls`` vai direto ao ``uvicorn.Config`` (``ssl_certfile``, ``ssl_keyfile``, ``ssl_ca_certs``,
    ``ssl_cert_reqs``); com certificado, a URL sai em https.
    """
    porta = porta or porta_livre()
    config = uvicorn.Config(app, host=host, port=porta, log_level="warning", lifespan="on", **tls)
    srv = uvicorn.Server(config)
    tarefa = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started or tarefa.done():
            break
        await asyncio.sleep(0.02)
    if tarefa.done():
        tarefa.result()  # a partida falhou: mostra o erro em vez de seguir com a porta fechada
        raise RuntimeError(f"o servidor de teste nao subiu em {host}:{porta}")
    esquema = "https" if tls.get("ssl_certfile") else "http"
    try:
        yield f"{esquema}://{host}:{porta}"
    finally:
        srv.should_exit = True
        await tarefa


@contextlib.asynccontextmanager
async def cliente_mcp(url: str, chave: str | None = None, *, modo: str = VERSAO_MCP) -> AsyncIterator[Client]:
    """Cliente oficial fixado na versao (padrao 2026-07-28), sem callbacks automaticos."""
    cabecalhos = {"authorization": f"Bearer {chave}"} if chave else {}
    http = httpx2.AsyncClient(headers=cabecalhos, follow_redirects=False, timeout=10)
    async with http, Client(streamable_http_client(url, http_client=http), mode=modo) as c:
        yield c


def pedido_2026(metodo: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    """Monta um pedido JSON-RPC cru na versao 2026-07-28, com o envelope em ``_meta``."""
    p = dict(params or {})
    p["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": VERSAO_MCP,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "teste-mcp-claro", "version": "0"},
    }
    return {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": p}


def cabecalhos_2026(metodo: str, nome: str | None = None, chave: str | None = None) -> dict[str, str]:
    h = {
        "content-type": "application/json",
        "accept": "application/json",
        "mcp-protocol-version": VERSAO_MCP,
        "mcp-method": metodo,
    }
    if nome:
        h["mcp-name"] = nome
    if chave:
        h["authorization"] = f"Bearer {chave}"
    return h


@contextlib.contextmanager
def sistema_geracao_antiga() -> Any:
    """Sobe o exemplo da geracao antiga (kit 1.30.0) num ambiente separado do uv e devolve a URL MCP."""
    import os
    import shutil
    import subprocess
    import sys
    import time
    from pathlib import Path

    import pytest

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv nao encontrado")
    raiz = Path(__file__).resolve().parents[1]
    porta = porta_livre()
    ambiente = os.environ | {"PORTA": str(porta), "HOST": "127.0.0.1"}
    proc = subprocess.Popen(  # noqa: S603 - comando fixo de teste
        [
            uv,
            "run",
            "--no-project",
            "--python",
            sys.executable,
            "--with",
            "mcp==1.30.0",
            "python",
            str(raiz / "exemplos/geracao_antiga/servidor.py"),
        ],
        env=ambiente,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(300):
            try:
                httpx2.get(f"http://127.0.0.1:{porta}/", timeout=0.5)
                break
            except httpx2.TransportError:
                if proc.poll() is not None:
                    erro = proc.stderr.read().decode()[-2000:] if proc.stderr else ""
                    pytest.fail(f"o exemplo antigo nao subiu: {erro}")
                time.sleep(0.2)
        yield f"http://127.0.0.1:{porta}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
