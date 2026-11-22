"""R6e-1, retoque: o que separa "dado do servidor que o banco nao guarda" (conta contra o servidor) de "falha nossa
no banco" (503 ``conferencia_nao_feita``, a rodada nao conta, contrato K5)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from mcp_claro.despachante import servico


class _Orig(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("x")
        self.sqlstate = sqlstate


def _erro(sqlstate: str | None) -> DBAPIError:
    return DBAPIError("INSERT ...", {}, _Orig(sqlstate))


@pytest.mark.parametrize(
    ("sqlstate", "do_dado"),
    [
        ("22P02", True),  # texto que o jsonb nao le
        ("22P05", True),  # caractere que o banco nao escreve
        ("22001", True),  # texto longo demais para a coluna
        ("54000", True),  # entrada de indice grande demais
        ("40P01", False),  # impasse: falha nossa
        ("40001", False),  # serializacao: falha nossa
        ("08006", False),  # conexao caiu: falha nossa
        ("53300", False),  # conexoes demais no banco: falha nossa
        ("42501", False),  # sem permissao: falha nossa
        (None, False),
    ],
)
def test_so_as_classes_22_e_54_sao_dado_do_servidor(sqlstate: str | None, do_dado: bool) -> None:
    assert servico._erro_de_dado(_erro(sqlstate)) is do_dado


def test_erro_sem_sqlstate_nao_e_dado_do_servidor() -> None:
    assert servico._erro_de_dado(DBAPIError("x", {}, Exception("sem sqlstate"))) is False
    assert servico._erro_de_dado(TimeoutError()) is False  # pool tomado


def _tool(nome: Any, **extra: Any) -> dict[str, Any]:
    return {"name": nome, "inputSchema": {"type": "object"}, **extra}


@pytest.mark.parametrize(
    ("ferramentas", "invalida"),
    [
        ([_tool("consultar")], False),
        ([_tool("a" * 300)], False),  # mais de 128 letras: o banco guarda, e a revisao recusa
        ([_tool("\U0001f600" * 250)], False),  # 1000 bytes
        ([_tool("\U0001f600" * 251)], True),  # 1004 bytes
        ([_tool("a" * 1001)], True),
        ([_tool("ok"), _tool("x" * 4000)], True),
        ([_tool("a\x00b")], True),  # o caractere nulo, como antes
        ([_tool("ok", description="a\x00")], True),
        ([_tool("a\ud800")], True),  # pedaco de par UTF-16 sozinho: o UTF-8 nao escreve
        ([_tool("ok", inputSchema={"type": "object", "x": ["\udfff"]})], True),
        ([_tool("ok", inputSchema={"type": "object", "\ud800": 1})], True),
        ([_tool("")], True),
        ([_tool("  ")], True),
        ([_tool(None)], True),
    ],
)
def test_declaracao_que_o_banco_nao_guarda_e_invalida(ferramentas: list[dict[str, Any]], invalida: bool) -> None:
    assert servico._declaracao_invalida(ferramentas) is invalida
