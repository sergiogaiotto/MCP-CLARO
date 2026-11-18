"""Exemplo 3: acoes numa caixa de teste. Nada sai deste processo.

Cada acao fica numa lista em memoria e e contada, para os testes provarem que
o gateway envia uma vez so, que "cancelado antes de sair" nao chega aqui e que
o crachá do aplicativo nunca chega (so a chave do cofre).
"""

from __future__ import annotations

import itertools
from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from exemplos.comum import Contagem, montar_app, rodar

FERRAMENTAS = [
    {
        "name": "criar_chamado",
        "title": "Criar chamado",
        "description": "Abre um chamado de suporte em nome da pessoa.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string", "minLength": 3, "maxLength": 80},
                "descricao": {"type": "string", "maxLength": 1000},
            },
            "required": ["titulo"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "enviar_mensagem",
        "title": "Enviar mensagem",
        "description": "Envia uma mensagem a um destinatario da caixa de teste.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "destinatario": {"type": "string", "maxLength": 120},
                "texto": {"type": "string", "maxLength": 500},
            },
            "required": ["destinatario", "texto"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "cancelar_servico",
        "title": "Cancelar servico",
        "description": "Cancela um servico da pessoa. Nao tem volta.",
        "inputSchema": {
            "type": "object",
            "properties": {"servico": {"type": "string", "maxLength": 40}},
            "required": ["servico"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
    },
]


def criar(contagem: Contagem | None = None) -> Starlette:
    contagem = contagem or Contagem()
    caixa: list[dict[str, Any]] = []
    numeros = itertools.count(1)

    async def listar(ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(f) for f in FERRAMENTAS])

    async def chamar(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        nomes = {f["name"] for f in FERRAMENTAS}
        if p.name not in nomes:
            raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")
        contagem.somar(p.name)
        numero = f"CX-{next(numeros):05d}"
        caixa.append(
            {
                "numero": numero,
                "ferramenta": p.name,
                "pessoa": ctx.request.state.pessoa,
                "argumentos": p.arguments or {},
                "meta": dict(ctx.meta or {}),
            }
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Feito: {p.name}, registro {numero}.")],
            structured_content={"registro": numero},
        )

    async def ver_caixa(_: Request) -> JSONResponse:
        return JSONResponse({"caixa": caixa})

    server: Server[Any] = Server("exemplo-acoes", version="1.0.0", on_list_tools=listar, on_call_tool=chamar)
    return montar_app(server, contagem=contagem, rotas_extras=[Route("/caixa", ver_caixa)])


if __name__ == "__main__":
    rodar(criar, 9103)
