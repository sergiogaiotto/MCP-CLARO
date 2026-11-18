"""Exemplo 5: sistema que so fala a geracao antiga do MCP, com o kit 1.x.

Roda em imagem de teste separada (geracao_antiga/Dockerfile), nunca no ambiente
do produto. Serve para provar que o MCP-CLARO recusa no cadastro, com mensagem
legivel, sistema que nao fala a 2026-07-28 (decisao 8).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

servidor = FastMCP(
    "exemplo-geracao-antiga",
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORTA", "9105")),
    stateless_http=True,
    json_response=True,
)


@servidor.tool()
def consultar_saldo(conta: str) -> str:
    """Consulta o saldo de uma conta de teste."""
    return f"Saldo da conta {conta}: R$ 10,00"


if __name__ == "__main__":
    servidor.run(transport="streamable-http")
