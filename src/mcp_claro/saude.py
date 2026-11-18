"""Conferencia de saude para o Docker: chama ``/saude`` do proprio papel, com mTLS interno."""

from __future__ import annotations

import ssl
import sys
import urllib.request

from mcp_claro.config.settings import Parametros

_PORTAS = {
    "entrada": "GATEWAY_INTERNAL_PORT",
    "despachante": "DISPATCHER_INTERNAL_PORT",
    "rotinas": "ROUTINES_INTERNAL_PORT",
}


def main() -> int:
    p = Parametros()
    papel = sys.argv[1] if len(sys.argv) > 1 else None
    portas = (
        [int(getattr(p, _PORTAS[papel]))] if papel in _PORTAS else [int(getattr(p, nome)) for nome in _PORTAS.values()]
    )
    tls = bool(p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE)
    contexto: ssl.SSLContext | None = None
    if tls:
        contexto = ssl.create_default_context(cafile=p.INTERNAL_CA_FILE)
        contexto.check_hostname = False
        # A porta interna so aceita quem pode chama-la; a conferencia de saude tem certificado proprio,
        # emitido pela autoridade de clientes desta porta e montado so neste conteiner.
        cert = p.INTERNAL_HEALTH_CERT_FILE or p.INTERNAL_CERT_FILE or ""
        chave = p.INTERNAL_HEALTH_KEY_FILE or p.INTERNAL_KEY_FILE or ""
        contexto.load_cert_chain(cert, chave)
    esquema = "https" if tls else "http"
    for porta in portas:
        try:
            url = f"{esquema}://127.0.0.1:{porta}/saude"  # so http(s) local, montado aqui
            with urllib.request.urlopen(url, context=contexto, timeout=3) as r:  # noqa: S310  # nosec B310
                if r.status == 200:
                    return 0
        except OSError:
            continue
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
