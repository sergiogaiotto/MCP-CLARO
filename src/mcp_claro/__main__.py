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

    from mcp_claro.db.engine import criar_engine
    from mcp_claro.papeis.app import criar_app

    porta = int(getattr(p, _PORTAS[papel])) if papel in _PORTAS else 9445
    opcoes: dict[str, Any] = {}
    if p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE:
        opcoes = {"ssl_certfile": p.INTERNAL_CERT_FILE, "ssl_keyfile": p.INTERNAL_KEY_FILE}
        autoridade_clientes = p.INTERNAL_CLIENT_CA_FILE or p.INTERNAL_CA_FILE
        if autoridade_clientes:
            # mTLS: cada porta interna aceita so o certificado de quem pode chama-la.
            opcoes |= {"ssl_ca_certs": autoridade_clientes, "ssl_cert_reqs": ssl.CERT_REQUIRED}
    app: Any
    if papel is Papel.ENTRADA:
        from mcp_claro.entrada.app import criar_app_entrada

        app = criar_app_entrada(p, criar_engine(p))
    elif papel is Papel.DESPACHANTE:
        from mcp_claro.despachante.app import criar_app_despachante

        app = criar_app_despachante(p, criar_engine(p))
    else:
        app = criar_app(papel, p)
    uvicorn.run(
        app,
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
