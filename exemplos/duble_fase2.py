"""Exemplo 6: duble da Fase 2, para o "simulado do dia ruim" com os candidatos a piloto.

Imita o que o MCP-CLARO devolve a um aplicativo quando uma acao precisa de ficha:
numero de protocolo, "estamos confirmando" e ficha pendente. Serve para ver se o
aplicativo se comporta antes de ligar a Fase 2 de verdade. Nao executa nada.
"""

from __future__ import annotations

import itertools
from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from exemplos.comum import Contagem, montar_app, rodar

PREFIXO = "br.com.claro.mcp-claro/"

FERRAMENTAS = [
    {
        "name": "pagar_conta",
        "title": "Pagar conta",
        "description": "Pede o pagamento de uma conta. Precisa de confirmacao da pessoa na tela do gateway.",
        "inputSchema": {
            "type": "object",
            "properties": {"conta": {"type": "string"}, "valor": {"type": "number", "exclusiveMinimum": 0}},
            "required": ["conta", "valor"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "consultar_protocolo",
        "title": "Consultar protocolo",
        "description": "Diz o estado de uma acao pelo numero de protocolo.",
        "inputSchema": {
            "type": "object",
            "properties": {"protocolo": {"type": "string"}},
            "required": ["protocolo"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
]


def criar(contagem: Contagem | None = None) -> Starlette:
    contagem = contagem or Contagem()
    numeros = itertools.count(1)
    estados: dict[str, str] = {}
    vistos: dict[str, str] = {}

    async def listar(ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(f) for f in FERRAMENTAS])

    async def chamar(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        contagem.somar(p.name)
        args = p.arguments or {}
        if p.name == "pagar_conta":
            chave = f"{ctx.request.state.pessoa}|{args.get('conta')}|{args.get('valor')}"
            if chave in vistos:
                protocolo = vistos[chave]
                texto = f"Pedido igual ao protocolo {protocolo}: nada novo foi criado."
            else:
                protocolo = f"CLR-TESTE-{next(numeros):06d}"
                vistos[chave] = protocolo
                ciclo = ("ficha_pendente", "estamos_confirmando", "nao_confirmada")
                estados[protocolo] = ciclo[(len(estados)) % len(ciclo)]
                texto = (
                    f"Acao registrada com o protocolo {protocolo}. A pessoa precisa confirmar a ficha na "
                    "tela do gateway. Consulte o protocolo depois; nao repita o pedido."
                )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=texto)],
                structured_content={"protocolo": protocolo, "estado": estados[protocolo]},
                _meta={PREFIXO + "protocolo": protocolo, PREFIXO + "estado": estados[protocolo]},
            )
        if p.name == "consultar_protocolo":
            protocolo = str(args.get("protocolo", ""))
            estado = estados.get(protocolo)
            if estado is None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="Protocolo desconhecido.")], is_error=True
                )
            frase = {
                "ficha_pendente": "A ficha espera a confirmacao da pessoa.",
                "estamos_confirmando": "Estamos confirmando com o sistema; nao repita o pedido.",
                "nao_confirmada": "Resultado nao confirmado; a pessoa decide na tela do gateway.",
            }[estado]
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=frase)],
                structured_content={"protocolo": protocolo, "estado": estado},
            )
        raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")

    server: Server[Any] = Server("exemplo-duble-fase2", version="1.0.0", on_list_tools=listar, on_call_tool=chamar)
    return montar_app(server, contagem=contagem)


if __name__ == "__main__":
    rodar(criar, 9106)
