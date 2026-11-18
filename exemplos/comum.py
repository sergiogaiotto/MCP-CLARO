"""Montagem comum dos sistemas de exemplo: kit oficial 2.3.0, sem estado, resposta JSON inteira.

Cada exemplo cria um ``Server`` de baixo nivel e chama ``montar_app``. O crachá de
cada pessoa e uma chave estatica so de teste (``EXEMPLO_CHAVES``), nunca um segredo real.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

CHAVES_PADRAO = {"chave-ana": "ana", "chave-bia": "bia", "chave-compartilhada": "*"}

pessoa_atual: contextvars.ContextVar[str | None] = contextvars.ContextVar("pessoa_atual", default=None)


@dataclass
class Contagem:
    """Conta chamadas por ferramenta, para os testes provarem 'uma chamada so'."""

    chamadas: dict[str, int] = field(default_factory=dict)
    autorizacoes_vistas: list[str] = field(default_factory=list)

    def somar(self, nome: str) -> None:
        self.chamadas[nome] = self.chamadas.get(nome, 0) + 1


def chaves() -> dict[str, str]:
    bruto = os.environ.get("EXEMPLO_CHAVES")
    return json.loads(bruto) if bruto else dict(CHAVES_PADRAO)


class FiltroChave:
    """Confere a chave estatica de teste; guarda a pessoa no contexto do pedido."""

    def __init__(self, app: ASGIApp, contagem: Contagem, mapa: dict[str, str]) -> None:
        self.app, self.contagem, self.mapa = app, contagem, mapa

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), "")
        self.contagem.autorizacoes_vistas.append(auth)
        chave = auth[7:] if auth.lower().startswith("bearer ") else ""
        pessoa = self.mapa.get(chave)
        if pessoa is None:
            resposta = JSONResponse(
                {"error": "invalid_token", "error_description": "Chave de teste desconhecida"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            await resposta(scope, receive, send)
            return
        scope.setdefault("state", {})["pessoa"] = pessoa
        token = pessoa_atual.set(pessoa)
        try:
            await self.app(scope, receive, send)
        finally:
            pessoa_atual.reset(token)


def montar_app(
    server: Server[Any],
    *,
    contagem: Contagem | None = None,
    exigir_chave: bool = True,
    rotas_extras: list[Route] | None = None,
    envolver: Callable[[ASGIApp], ASGIApp] | None = None,
) -> Starlette:
    contagem = contagem or Contagem()
    sm = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    endpoint: ASGIApp = StreamableHTTPASGIApp(sm)
    if envolver is not None:
        endpoint = envolver(endpoint)
    if exigir_chave:
        endpoint = FiltroChave(endpoint, contagem, chaves())

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with sm.run():
            yield

    async def ver_contagem(_: Request) -> JSONResponse:
        return JSONResponse(
            {"chamadas": contagem.chamadas, "autorizacoes": contagem.autorizacoes_vistas[-50:]}
        )

    async def zerar(_: Request) -> JSONResponse:
        contagem.chamadas.clear()
        contagem.autorizacoes_vistas.clear()
        return JSONResponse({"ok": True})

    rotas = [
        Route("/mcp", endpoint=endpoint),
        Route("/contagem", ver_contagem),
        Route("/contagem/zerar", zerar, methods=["POST"]),
        *(rotas_extras or []),
    ]
    app = Starlette(routes=rotas, lifespan=ciclo)
    app.state.contagem = contagem
    return app


def rodar(fabrica: Callable[[], Starlette], porta_padrao: int) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--porta", type=int, default=porta_padrao)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(fabrica(), host=args.host, port=args.porta, log_level="warning")
