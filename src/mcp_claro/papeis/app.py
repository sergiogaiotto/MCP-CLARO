"""Aplicacao ASGI de cada papel.

Toda papel expoe ``/saude`` (vivo) e ``/pronto`` (pronto para atender). As
partes de cada papel entram nas etapas seguintes; aqui fica o esqueleto que
recusa configuracao invalida na partida.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.config.settings import Parametros
from mcp_claro.logs import logger
from mcp_claro.papeis import Papel
from mcp_claro.versao import VERSAO_MCP


def criar_app(papel: Papel, parametros: Parametros) -> Starlette:
    parametros.conferir()
    log = logger(f"mcp_claro.{papel}")

    async def saude(_: Request) -> JSONResponse:
        return JSONResponse({"papel": str(papel), "versao": __version__, "mcp": VERSAO_MCP, "vivo": True})

    async def pronto(_: Request) -> JSONResponse:
        return JSONResponse({"papel": str(papel), "pronto": True})

    @asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        log.info("partida", papel=str(papel), versao=__version__, mcp=VERSAO_MCP, ambiente=str(parametros.APP_ENV))
        yield
        log.info("parada", papel=str(papel))

    app = Starlette(routes=[Route("/saude", saude), Route("/pronto", pronto)], lifespan=ciclo)
    app.state.parametros = parametros
    app.state.papel = papel
    return app
