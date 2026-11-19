"""Correcoes da revisao da porta de saida que nao precisam de banco: limite que fecha campos de destinatario,
protocolo com mais de oito digitos e a frase de "nao saiu"."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_claro.saida.estados import FRASE, Estado
from mcp_claro.saida.fila import cobre
from mcp_claro.saida.pedidos import FERRAMENTA_CONSULTA
from mcp_claro.saida.protocolo import PADRAO_PROTOCOLO, protocolo_valido

SO_CHEFE = [{"campo": "para", "valores": ["chefe@org"]}, {"campo": "copia", "valores": []}]


@pytest.mark.parametrize(
    "argumentos",
    [
        {"para": "chefe@org"},
        {"para": "chefe@org", "copia": None},
        {"para": "chefe@org", "copia": ""},
        {"para": "chefe@org", "copia": []},
        {"para": ["chefe@org"], "copia": [], "texto": "oi"},
    ],
)
def test_campo_com_valores_vazio_aceita_so_ausente_ou_vazio(argumentos: dict[str, Any]) -> None:
    assert cobre(SO_CHEFE, argumentos)


@pytest.mark.parametrize(
    "argumentos",
    [
        {"para": "chefe@org", "copia": "externo@fora.com"},
        {"para": "chefe@org", "copia": ["externo@fora.com"]},
        {"para": "chefe@org", "copia": [""]},
        {"para": "chefe@org", "copia": 0},
        {"para": "chefe@org", "copia": {}},
        {"para": "outro@org"},
        {"copia": []},
    ],
)
def test_campo_com_valores_vazio_recusa_qualquer_valor(argumentos: dict[str, Any]) -> None:
    assert not cobre(SO_CHEFE, argumentos)


def test_lista_vazia_continua_qualquer_destinatario() -> None:
    assert cobre([], {"para": "qualquer@lugar", "copia": ["outro@lugar"]})
    assert cobre("[]", {})


def test_protocolo_aceita_oito_digitos_ou_mais() -> None:
    assert protocolo_valido("CLR-2026-00000012")
    assert protocolo_valido("CLR-2026-101000012")
    assert not protocolo_valido("CLR-2026-0000012")
    padrao = FERRAMENTA_CONSULTA["inputSchema"]["properties"]["protocolo"]["pattern"]
    assert padrao == PADRAO_PROTOCOLO.pattern


def test_frase_de_nao_saiu_cobre_a_recusa_na_porta() -> None:
    assert FRASE[Estado.NAO_SAIU] == (
        "Nao saiu: o sistema nao executou o pedido (nao recebeu ou recusou na porta). Nada aconteceu."
    )
