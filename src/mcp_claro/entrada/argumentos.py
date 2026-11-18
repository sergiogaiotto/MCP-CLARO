"""Conferencia dos argumentos contra o esquema revisado (JSON Schema Draft 2020-12).

O servidor de baixo nivel do kit nao confere argumentos. A entrada confere cada
pedido contra o esquema revisado pelo ADMIN, antes das regras e da ficha. Nao
busca referencias externas (``$ref`` de fora): um registro vazio recusa.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

_REGISTRO_VAZIO: Registry[Any] = Registry()


class EsquemaInvalido(ValueError):
    pass


class ArgumentosInvalidos(ValueError):
    def __init__(self, problemas: list[str]) -> None:
        self.problemas = problemas
        super().__init__("; ".join(problemas))


def conferir_esquema(esquema: Mapping[str, Any]) -> None:
    """O esquema revisado precisa ser valido e de objeto (exigencia da 2026-07-28)."""
    if esquema.get("type") != "object":
        raise EsquemaInvalido('o esquema de entrada precisa ter "type": "object"')
    try:
        Draft202012Validator.check_schema(esquema)
    except SchemaError as erro:
        raise EsquemaInvalido(f"esquema invalido: {erro.message}") from erro


@lru_cache(maxsize=1024)
def _validador(esquema_json: str) -> Draft202012Validator:
    esquema = json.loads(esquema_json)
    conferir_esquema(esquema)
    return Draft202012Validator(esquema, registry=_REGISTRO_VAZIO)


def conferir_argumentos(esquema: Mapping[str, Any], argumentos: Mapping[str, Any] | None) -> None:
    """Levanta ``ArgumentosInvalidos`` com ate cinco problemas legiveis, sem repetir os valores."""
    validador = _validador(json.dumps(esquema, sort_keys=True))
    try:
        erros = sorted(validador.iter_errors(dict(argumentos or {})), key=lambda e: list(e.absolute_path))
    except Unresolvable as erro:
        raise ArgumentosInvalidos(["o esquema cita uma referencia externa, que nao e buscada"]) from erro
    if erros:
        problemas = []
        for falha in erros[:5]:
            caminho = "/".join(str(p) for p in falha.absolute_path) or "(raiz)"
            problemas.append(f"{caminho}: regra '{falha.validator}' nao atendida")
        raise ArgumentosInvalidos(problemas)
