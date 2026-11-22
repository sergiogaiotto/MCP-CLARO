"""Conferencia de saude da sala para o Docker: ``python -m mcp_claro.visitas.saude``.

A porta da sala so aceita o certificado de cliente do despachante, e a ``ca_clientes_visitas`` nao emite outro
(critica M4): a conferencia nao tem certificado de cliente. Ela abre TLS 1.3 com a porta e confere o certificado
da sala pela ``ca_visitas`` (``INTERNAL_CA_FILE`` no conteiner da sala). No TLS 1.3 o lado do cliente termina o
aperto de maos antes de a sala recusar a falta de certificado: chegar ate ai prova que a sala escuta e se apresenta
com o certificado certo, sem que nenhum pedido seja atendido.
"""

from __future__ import annotations

import socket
import ssl

from mcp_claro.config.settings import Parametros


def main() -> int:
    p = Parametros()
    if not p.INTERNAL_CA_FILE:
        return 1
    contexto = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=p.INTERNAL_CA_FILE)
    contexto.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        with (
            socket.create_connection(("127.0.0.1", p.VISITS_INTERNAL_PORT), timeout=3) as bruto,
            contexto.wrap_socket(bruto, server_hostname="localhost"),
        ):
            return 0
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
