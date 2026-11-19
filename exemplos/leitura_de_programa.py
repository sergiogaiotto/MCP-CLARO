"""Exemplo: sistema com chave propria de programa que roda sozinho (decisao 9).

Cada programa tem a sua chave (``EXEMPLO_CHAVES`` mapeia a chave para ``programa:<nome>``). O sistema so
responde a chave de programa e diz, no resultado, qual programa leu: assim o teste prova que o MCP-CLARO
usou a chave propria do programa, e nao a de uma pessoa. Tem duas leituras e uma acao (que um programa nao
pode pedir na Fase 2).
"""

from __future__ import annotations

from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from exemplos.comum import Contagem, montar_app, rodar

PREFIXO: str = "programa:"

FILAS: dict[str, list[dict[str, str]]] = {
    "suporte": [{"id": "CH-7", "titulo": "Sem sinal"}, {"id": "CH-9", "titulo": "Fatura em dobro"}],
    "vendas": [],
}

FERRAMENTAS = [
    {
        "name": "consultar_fila",
        "title": "Consultar fila",
        "description": "Conta os chamados abertos de uma fila.",
        "inputSchema": {
            "type": "object",
            "properties": {"fila": {"type": "string", "enum": sorted(FILAS)}},
            "required": ["fila"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "status_filas",
        "title": "Status das filas",
        "description": "Diz quantas filas estao no ar.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "fechar_chamado",
        "title": "Fechar chamado",
        "description": "Fecha um chamado aberto.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "maxLength": 20}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]


def criar(contagem: Contagem | None = None) -> Starlette:
    contagem = contagem or Contagem()

    async def listar(ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(f) for f in FERRAMENTAS])

    async def chamar(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        quem = str(ctx.request.state.pessoa)
        if not quem.startswith(PREFIXO):
            raise MCPError(code=types.INVALID_PARAMS, message="Este sistema so aceita a chave propria de um programa")
        programa = quem.removeprefix(PREFIXO)
        contagem.somar(p.name)
        args = p.arguments or {}
        if p.name == "consultar_fila":
            fila = str(args.get("fila", ""))
            abertos = FILAS.get(fila, [])
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{len(abertos)} chamado(s) na fila {fila}.")],
                structured_content={"fila": fila, "abertos": len(abertos), "programa": programa},
            )
        if p.name == "status_filas":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{len(FILAS)} filas no ar.")],
                structured_content={"filas": len(FILAS), "programa": programa},
            )
        if p.name == "fechar_chamado":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Chamado {args.get('id')} fechado.")],
                structured_content={"fechado": str(args.get("id")), "programa": programa},
            )
        raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")

    server: Server[Any] = Server("exemplo-programa", version="1.0.0", on_list_tools=listar, on_call_tool=chamar)
    return montar_app(server, contagem=contagem)


if __name__ == "__main__":
    rodar(criar, 9105)
