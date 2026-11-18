"""Aplicacao ASGI do papel ``entrada``.

Ordem no endereco MCP: cracha (401) -> porteiro de versao (405/400) -> kit oficial
(sem estado, resposta JSON inteira). Os metadados do recurso (RFC 9728) e a
descoberta do login ficam no mesmo endereco publico.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import ChavesPublicas, FiltroCracha, VerificadorCracha
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.entrada.porteiro import PorteiroDeVersao
from mcp_claro.entrada.servidor import Leitor, criar_servidor
from mcp_claro.logs import logger
from mcp_claro.tarefas import descarregar_contadores_periodicamente
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.entrada")


def url_jwks(p: Parametros) -> str:
    if p.IDP_JWKS_URL:
        return p.IDP_JWKS_URL
    if p.IDP_KIND.value == "keycloak":
        return f"{p.IDP_ISSUER_URL}/protocol/openid-connect/certs"
    return f"{p.IDP_ISSUER_URL}/.well-known/jwks.json"


def criar_app_entrada(
    p: Parametros,
    engine: AsyncEngine,
    *,
    http_login: httpx2.AsyncClient | None = None,
    leitor: Leitor | None = None,
) -> Starlette:
    p.conferir()
    http_login = http_login or httpx2.AsyncClient(timeout=httpx2.Timeout(5.0), follow_redirects=False)
    despachante = ClienteDespachante(p) if leitor is None else None
    ler: Leitor = leitor or despachante.ler  # type: ignore[union-attr]
    servidor = criar_servidor(p, engine, ler)
    sm = StreamableHTTPSessionManager(
        app=servidor,
        json_response=True,
        stateless=True,
        max_request_body_size=p.MAX_REQUEST_BYTES,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(p.ALLOWED_HOSTS),
            allowed_origins=[p.PUBLIC_BASE_URL],
        ),
    )
    verificador = VerificadorCracha(
        p, ChavesPublicas(url_jwks(p), cache_segundos=p.IDP_JWKS_CACHE_SECONDS, http=http_login)
    )
    endpoint = FiltroCracha(PorteiroDeVersao(StreamableHTTPASGIApp(sm), max_corpo=p.MAX_REQUEST_BYTES), verificador, p)

    async def metadados_recurso(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": p.recurso,
                "authorization_servers": [p.IDP_ISSUER_URL],
                "scopes_supported": list(p.MCP_SCOPES),
                "bearer_methods_supported": ["header"],
                "resource_name": "MCP-CLARO",
            },
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=300"},
        )

    async def saude(_: Request) -> JSONResponse:
        return JSONResponse({"papel": "entrada", "versao": __version__, "mcp": VERSAO_MCP, "vivo": True})

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as pilha:
            await pilha.enter_async_context(sm.run())
            await pilha.enter_async_context(descarregar_contadores_periodicamente(p, engine))
            log.info("partida", papel="entrada", versao=__version__, mcp=VERSAO_MCP)
            yield
            if despachante is not None:
                await despachante.fechar()
            await http_login.aclose()

    rotas: list[Any] = [
        Route("/mcp", endpoint=endpoint),
        Route("/.well-known/oauth-protected-resource/mcp", metadados_recurso, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", metadados_recurso, methods=["GET"]),
        Route("/saude", saude, methods=["GET"]),
    ]
    app = Starlette(routes=rotas, lifespan=ciclo)
    app.state.parametros = p
    return app
