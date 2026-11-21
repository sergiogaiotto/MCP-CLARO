"""Comando de partida do MCP-CLARO.

mcp-claro conferir            confere os parametros e sai
mcp-claro migrar              aplica as migracoes (usuario dono do esquema)
mcp-claro <papel>             sobe um papel: entrada, despachante, rotinas, vigia ou visitas

A sala de visitas (``visitas``) confere so o que e dela (``visitas.partida``): sem banco, sem cofre, so o proprio
certificado e mTLS obrigatorio. Os outros papeis conferem todos os parametros.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.logs import configurar
from mcp_claro.papeis import Papel

_PORTAS = {
    Papel.ENTRADA: "GATEWAY_INTERNAL_PORT",
    Papel.DESPACHANTE: "DISPATCHER_INTERNAL_PORT",
    Papel.ROTINAS: "ROUTINES_INTERNAL_PORT",
    Papel.VIGIA: "WATCH_INTERNAL_PORT",
    Papel.VISITAS: "VISITS_INTERNAL_PORT",
}


def _conferir() -> Parametros:
    try:
        return Parametros().conferir()
    except ConfiguracaoInvalida as erro:
        print(str(erro), file=sys.stderr)
        raise SystemExit(2) from erro


def _conferir_sala() -> Parametros:
    from mcp_claro.visitas.partida import conferir_sala

    try:
        return conferir_sala(Parametros(), os.environ)
    except ConfiguracaoInvalida as erro:
        print(str(erro), file=sys.stderr)
        raise SystemExit(2) from erro


def _migrar(p: Parametros) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.attributes["parametros"] = p
    command.upgrade(cfg, "head")


def opcoes_tls(p: Parametros) -> dict[str, Any]:
    """Opcoes de TLS do uvicorn para a porta interna de um papel (vazio sem certificado)."""
    import ssl

    if not (p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE):
        return {}
    # mTLS: cada porta interna aceita so o certificado de quem pode chama-la. Sem volta para a autoridade comum: a
    # partida ja recusou TLS sem INTERNAL_CLIENT_CA_FILE (a da sala recusa subir sem mTLS).
    return {
        "ssl_certfile": p.INTERNAL_CERT_FILE,
        "ssl_keyfile": p.INTERNAL_KEY_FILE,
        "ssl_ca_certs": p.INTERNAL_CLIENT_CA_FILE,
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def _subir(papel: Papel, p: Parametros) -> None:
    import uvicorn

    from mcp_claro.db.engine import criar_engine
    from mcp_claro.papeis.app import criar_app

    porta = int(getattr(p, _PORTAS[papel], 9445))
    opcoes = opcoes_tls(p)
    app: Any
    if papel is Papel.VISITAS:
        from mcp_claro.visitas.app import criar_app_visitas
        from mcp_claro.visitas.filho import nao_depuravel

        # O pai guarda os pedidos das chamadas em andamento: nenhum filho (do mesmo usuario) le a memoria dele.
        nao_depuravel()
        app = criar_app_visitas(p)
    elif papel is Papel.ENTRADA:
        from mcp_claro.entrada.app import criar_app_entrada

        app = criar_app_entrada(p, criar_engine(p))
    elif papel is Papel.DESPACHANTE:
        from mcp_claro.despachante.app import criar_app_despachante

        app = criar_app_despachante(p, criar_engine(p))
    elif papel is Papel.VIGIA:
        from mcp_claro.vigia.app import criar_app_vigia

        app = criar_app_vigia(p, criar_engine(p))
    elif papel is Papel.ROTINAS:
        from mcp_claro.rotinas.app import criar_app_rotinas

        # Cada tarefa rodando usa ate duas conexoes: uma para a trava e outra para o trabalho.
        app = criar_app_rotinas(p, criar_engine(p, pool_size=8, max_overflow=4))
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
    parser.add_argument("comando", choices=["conferir", "migrar", "migrar-vigia", *[str(p) for p in Papel]])
    args = parser.parse_args(argv)
    p = _conferir_sala() if args.comando == Papel.VISITAS else _conferir()
    configurar(p.LOG_LEVEL)
    if args.comando == "conferir":
        print("Parametros conferidos: nenhuma combinacao invalida.")
        return
    if args.comando == "migrar":
        _migrar(p)
        return
    if args.comando == "migrar-vigia":
        from mcp_claro.vigia.migrar import migrar_vigia

        migrar_vigia(p)
        return
    _subir(Papel(args.comando), p)


if __name__ == "__main__":
    main()
