"""Exemplo 1: sistema so de leitura, com chave por pessoa.

Cada pessoa ve so os proprios dados. Os resultados trazem campos pessoais
(CPF e e-mail) para provar a mascara da leitura com medida.
"""

from __future__ import annotations

from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from exemplos.comum import Contagem, montar_app, rodar

DADOS: dict[str, dict[str, Any]] = {
    "ana": {
        "nome": "Ana Teste",
        "cpf": "123.456.789-09",
        "email": "ana@exemplo.test",
        "faturas": {"2026-10": 99.9, "2026-11": 104.5},
        "chamados": [{"id": "CH-1", "titulo": "Sinal fraco", "situacao": "aberto"}],
    },
    "bia": {
        "nome": "Bia Teste",
        "cpf": "987.654.321-00",
        "email": "bia@exemplo.test",
        "faturas": {"2026-11": 59.0},
        "chamados": [],
    },
}

FERRAMENTAS = [
    {
        "name": "consultar_fatura",
        "title": "Consultar fatura",
        "description": "Mostra o valor da fatura de um mes da propria pessoa.",
        "inputSchema": {
            "type": "object",
            "properties": {"mes": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}},
            "required": ["mes"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"mes": {"type": "string"}, "valor": {"type": "number"}, "cpf": {"type": "string"}},
            "required": ["mes", "valor"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "listar_chamados",
        "title": "Listar chamados",
        "description": "Lista os chamados abertos da propria pessoa.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "ver_cadastro",
        "title": "Ver cadastro",
        "description": "Mostra nome, CPF e e-mail da propria pessoa.",
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
        pessoa = ctx.request.state.pessoa
        contagem.somar(p.name)
        dados = DADOS.get(pessoa)
        if dados is None:
            raise MCPError(code=types.INVALID_PARAMS, message="Pessoa sem cadastro neste sistema")
        args = p.arguments or {}
        if p.name == "consultar_fatura":
            mes = str(args.get("mes", ""))
            valor = dados["faturas"].get(mes)
            if valor is None:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=f"Sem fatura em {mes}.")], is_error=True
                )
            saida = {"mes": mes, "valor": valor, "cpf": dados["cpf"]}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Fatura de {mes}: R$ {valor:.2f}")],
                structured_content=saida,
            )
        if p.name == "listar_chamados":
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{len(dados['chamados'])} chamado(s).")],
                structured_content={"chamados": dados["chamados"]},
            )
        if p.name == "ver_cadastro":
            saida = {k: dados[k] for k in ("nome", "cpf", "email")}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{saida['nome']}, {saida['email']}")],
                structured_content=saida,
            )
        raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")

    server: Server[Any] = Server(
        "exemplo-leitura", version="1.0.0", on_list_tools=listar, on_call_tool=chamar
    )
    return montar_app(server, contagem=contagem)


if __name__ == "__main__":
    rodar(criar, 9101)
