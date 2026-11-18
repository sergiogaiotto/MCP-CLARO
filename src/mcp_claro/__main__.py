"""Comando de partida do MCP-CLARO.

mcp-claro conferir            confere os parametros e sai
mcp-claro migrar              aplica as migracoes (usuario dono do esquema)
mcp-claro <papel>             sobe um papel: entrada, despachante, rotinas ou vigia
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.logs import configurar
from mcp_claro.papeis import Papel

_PORTAS = {
    Papel.ENTRADA: "GATEWAY_INTERNAL_PORT",
    Papel.DESPACHANTE: "DISPATCHER_INTERNAL_PORT",
    Papel.ROTINAS: "ROUTINES_INTERNAL_PORT",
}


def _conferir() -> Parametros:
    try:
        return Parametros().conferir()
    except ConfiguracaoInvalida as erro:
        print(str(erro), file=sys.stderr)
        raise SystemExit(2) from erro


def _migrar(p: Parametros) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.attributes["parametros"] = p
    command.upgrade(cfg, "head")


def _subir(papel: Papel, p: Parametros) -> None:
    import ssl

    import uvicorn

    from mcp_claro.papeis.app import criar_app

    porta = int(getattr(p, _PORTAS[papel])) if papel in _PORTAS else 9445
    opcoes: dict[str, Any] = {}
    if p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE:
        opcoes = {"ssl_certfile": p.INTERNAL_CERT_FILE, "ssl_keyfile": p.INTERNAL_KEY_FILE}
        if p.INTERNAL_CA_FILE:
            # mTLS: so entra quem tem certificado da autoridade interna.
            opcoes |= {"ssl_ca_certs": p.INTERNAL_CA_FILE, "ssl_cert_reqs": ssl.CERT_REQUIRED}
    uvicorn.run(
        criar_app(papel, p),
        # Escuta so na rede interna do Docker; o portao e a unica peca exposta.
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=porta,
        proxy_headers=True,
        forwarded_allow_ips=",".join(p.FORWARDED_ALLOW_IPS),
        server_header=False,
        date_header=False,
        log_config=None,
        **opcoes,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-claro", description="MCP-CLARO, o gateway MCP independente")
    parser.add_argument("comando", choices=["conferir", "migrar", *[str(p) for p in Papel]])
    args = parser.parse_args(argv)
    p = _conferir()
    configurar(p.LOG_LEVEL)
    if args.comando == "conferir":
        print("Parametros conferidos: nenhuma combinacao invalida.")
        return
    if args.comando == "migrar":
        _migrar(p)
        return
    _subir(Papel(args.comando), p)


if __name__ == "__main__":
    main()
