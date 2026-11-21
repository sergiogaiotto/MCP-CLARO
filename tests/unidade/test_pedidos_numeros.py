"""Acao com numero NaN ou infinito (Fase 3, G4r): ``saida.pedidos.tem_numero_nao_finito``.

O leitor de JSON do kit aceita ``NaN``, ``Infinity`` e ``1e400``, mas o JSON do protocolo (``canonico``, com
``allow_nan=False``) e o banco nao. Sem a recusa, a acao virava erro interno.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from mcp_claro.saida.pedidos import RECUSA_NUMERO, tem_numero_nao_finito
from mcp_claro.saida.protocolo import canonico


@pytest.mark.parametrize(
    "valor",
    [
        {"n": math.inf},
        {"n": -math.inf},
        {"n": math.nan},
        {"a": [1, {"b": [2.5, math.nan]}]},
        json.loads('{"n": NaN}'),
        json.loads('{"n": [Infinity]}'),
        json.loads('{"n": 1e400}'),
    ],
    ids=["infinito", "menos_infinito", "nan", "fundo", "nan_do_json", "infinito_do_json", "1e400_do_json"],
)
def test_nan_e_infinito_em_qualquer_nivel(valor: Any) -> None:
    assert tem_numero_nao_finito(valor)
    with pytest.raises(ValueError, match="Out of range float"):
        canonico(valor)  # o erro interno de antes


@pytest.mark.parametrize(
    "valor",
    [{}, {"n": 1.5}, {"n": -1e308}, {"n": 10**4000}, {"n": True}, {"inf": "Infinity", "nan": "NaN"}, [None, [0.0]]],
    ids=["vazio", "fracao", "grande", "inteiro_enorme", "booleano", "textos", "lista"],
)
def test_numeros_finitos_e_outros_valores_passam(valor: Any) -> None:
    assert not tem_numero_nao_finito(valor)


def test_sem_recursao() -> None:
    valor: Any = [math.inf]
    for _ in range(20_000):
        valor = {"a": [valor]}
    assert tem_numero_nao_finito(valor)


def test_a_frase() -> None:
    assert RECUSA_NUMERO == (
        "O pedido traz um numero que nao e um numero de verdade (NaN ou infinito), e o MCP-CLARO nao guarda pedido "
        "assim. Nada foi enviado."
    )
