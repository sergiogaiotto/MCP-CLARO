"""Exemplo 2: sistema com uma chave compartilhada, que nao distingue as pessoas."""

from __future__ import annotations

from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from exemplos.comum import Contagem, montar_app, rodar

CATALOGO = [
    {"codigo": "P-10", "nome": "Plano 10 GB", "preco": 49.9},
    {"codigo": "P-50", "nome": "Plano 50 GB", "preco": 89.9},
]

FERRAMENTAS = [
    {
        "name": "consultar_catalogo",
        "title": "Consultar catalogo",
        "description": "Procura planos pelo nome.",
        "inputSchema": {
            "type": "object",
            "properties": {"termo": {"type": "string", "maxLength": 50}},
            "required": ["termo"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "status_servico",
        "title": "Status do servico",
        "description": "Diz se o servico esta no ar.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def criar(contagem: Contagem | None = None) -> Starlette:
    contagem = contagem or Contagem()

    async def listar(
        ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(f) for f in FERRAMENTAS])

    async def chamar(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        if ctx.request.state.pessoa != "*":
            raise MCPError(code=types.INVALID_PARAMS, message="Este sistema so aceita a chave compartilhada")
        contagem.somar(p.name)
        if p.name == "consultar_catalogo":
            termo = str((p.arguments or {}).get("termo", "")).lower()
            achados = [c for c in CATALOGO if termo in c["nome"].lower()]
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{len(achados)} plano(s).")],
                structured_content={"planos": achados},
            )
        if p.name == "status_servico":
            return types.CallToolResult(content=[types.TextContent(type="text", text="No ar.")])
        raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")

    server: Server[Any] = Server(
        "exemplo-compartilhada", version="1.0.0", on_list_tools=listar, on_call_tool=chamar
    )
    return montar_app(server, contagem=contagem)


if __name__ == "__main__":
    rodar(criar, 9102)
