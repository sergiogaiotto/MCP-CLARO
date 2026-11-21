"""Logs em JSON sem conteudo: cada linha leva protocolo e trace_id, nunca segredos.

O kit do MCP escreve no log, pelo ``logging`` do Python, pedacos do que o sistema mandou: ``logger.exception('Error
parsing JSON response')`` traz, na excecao, um trecho da resposta; outros registros do transporte levam o tipo de
conteudo, o evento SSE, o identificador de sessao ou o nome de uma ferramenta, todos escolhidos pelo sistema. O
cliente HTTP por baixo tambem: o ``httpx2`` escreve, em INFO, a frase do status que o servidor mandou ("HTTP/1.1 200
<frase>"), e o ``httpcore2``, em DEBUG, os cabecalhos da resposta. ``calar_o_kit`` deixa so o fato:

- nos registradores do kit no caminho do cliente fixado (``LOGGERS_DO_KIT``): a mensagem fixa do kit (quando ela e
  uma das conhecidas, sem os valores) e o tipo da excecao, nunca a excecao, o rastro, os argumentos ou um texto
  montado com o que veio do sistema;
- no ``httpx2``: o metodo, o endereco (o do cadastro), a versao e o numero do status, sem a frase do status;
- nos do ``httpcore2`` (``LOGGERS_DO_HTTPCORE``): so o nome do evento ("receive_response_headers.complete"), sem os
  valores.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Final

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


LOGGERS_DO_KIT: Final = ("mcp.client.streamable_http", "client", "mcp.client.session")
"""Registradores do kit do MCP no caminho do cliente fixado (o que fala com os sistemas). O ``mcp/client/session.py``
do kit 2.3.0 escreve em ``logging.getLogger("client")``, e nao no nome do modulo: e o nome que vale.
``mcp.client.session`` fica para uma versao do kit que passe a usar o nome do modulo."""

FATOS_DO_KIT: Final = frozenset(
    {
        # mcp/client/streamable_http.py (mensagens fixas, sem nada do sistema)
        "Error parsing JSON response",
        "Error parsing SSE message",
        "Error in post_writer",
        "GET SSE connection established",
        "GET stream error",
        "Non-2xx body was not a JSON-RPC error; using fallback",
        "Received 202 Accepted",
        "Reconnected to SSE stream",
        "Resumption GET SSE connection established",
        "SSE stream disconnected, reconnecting...",
        "SSE stream ended",
        "Server does not allow session termination",
        "aborting in-flight POST for cancelled request %r",
        "read stream closed before request %r could be resolved",
        # mcp/client/session.py (os valores, que podem vir do sistema, nunca vao)
        "Failed to validate notification: %s",
        "client callback for %r returned an invalid result",
        "dropped %r: not defined at %s",
        "dropping tool %r: invalid x-mcp-header (%s)",
        "message_handler raised on transport exception",
        "notification binding for %r will never fire at %s: the core protocol defines this method",
        "notification binding handler for %r raised",
        "notification callback for %r raised",
        "notification queue for %r is full; dropped the oldest event",
    }
)
"""Mensagens do kit que ficam no log como estao (sem os valores). Qualquer outra vira ``FATO_SEM_TEXTO``: o kit
monta varias com o que o sistema mandou (``f"Unexpected content type: {content_type}"``, por exemplo)."""

FATO_SEM_TEXTO: Final = "evento do kit do MCP (o texto foi retirado do log)"


LOGGER_HTTPX: Final = "httpx2"
PEDIDO_HTTPX: Final = 'HTTP Request: %s %s "%s %d %s"'
"""A linha do ``httpx2`` a cada pedido: metodo, endereco, versao, status e a frase do status (do servidor)."""
LOGGERS_DO_HTTPCORE: Final = (
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
)


def _so_o_fato(record: logging.LogRecord, fato: str) -> None:
    if record.exc_info and record.exc_info[0] is not None:
        fato = f"{fato} ({record.exc_info[0].__name__})"
    record.msg = fato
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None


class SoOFato(logging.Filter):
    """Filtro dos registradores do kit: so o fato, sem os bytes do sistema (ver o topo do modulo)."""

    def filter(self, record: logging.LogRecord) -> bool:
        conhecida = isinstance(record.msg, str) and record.msg in FATOS_DO_KIT
        _so_o_fato(record, str(record.msg).replace("%r", "?").replace("%s", "?") if conhecida else FATO_SEM_TEXTO)
        return True


class SemFraseDoStatus(logging.Filter):
    """Filtro do ``httpx2``: a linha de cada pedido sem a frase do status, que o servidor escolhe."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg == PEDIDO_HTTPX and isinstance(record.args, tuple) and len(record.args) == 5:
            record.args = (*record.args[:4], "")
        else:  # o httpx2 2.x nao escreve outra; se escrever, vai so o fato
            _so_o_fato(record, FATO_SEM_TEXTO)
        return True


class SoOEvento(logging.Filter):
    """Filtro do ``httpcore2``: so o nome do evento (a primeira palavra), sem os valores (cabecalhos, excecoes)."""

    def filter(self, record: logging.LogRecord) -> bool:
        primeira = str(record.msg).split(" ", 1)[0] if isinstance(record.msg, str) else ""
        _so_o_fato(record, primeira[:80] or FATO_SEM_TEXTO)
        return True


def _por(nome: str, filtro: type[logging.Filter]) -> None:
    registrador = logging.getLogger(nome)
    if not any(isinstance(f, filtro) for f in registrador.filters):
        registrador.addFilter(filtro())


def calar_o_kit() -> None:
    """Poe os filtros nos registradores do kit, do ``httpx2`` e do ``httpcore2``, uma vez. Roda ao importar este
    modulo e ao importar o cliente fixado; nao depende de ``configurar``."""
    for nome in LOGGERS_DO_KIT:
        _por(nome, SoOFato)
    _por(LOGGER_HTTPX, SemFraseDoStatus)
    for nome in LOGGERS_DO_HTTPCORE:
        _por(nome, SoOEvento)


calar_o_kit()


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
