"""Logs em JSON sem conteudo: cada linha leva protocolo e trace_id, nunca segredos."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_PROIBIDAS = frozenset(
    {
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "cracha",
        "chave",
        "senha",
        "password",
        "secret",
        "segredo",
        "argumentos",
        "arguments",
        "resultado",
        "result",
        "cookie",
        "set-cookie",
    }
)


def _tirar_conteudo(_: Any, __: str, evento: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for chave in list(evento):
        if chave.lower() in _PROIBIDAS:
            evento[chave] = "[retirado]"
    return evento


def configurar(nivel: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=nivel.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _tirar_conteudo,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(nivel.upper())),
        cache_logger_on_first_use=True,
    )


def logger(nome: str) -> Any:
    return structlog.get_logger(nome)
