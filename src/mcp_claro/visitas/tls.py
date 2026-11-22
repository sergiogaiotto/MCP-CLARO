"""Certificados da sala de visitas (critica M4).

- A sala tem autoridade propria (``ca_visitas``) para o certificado de servidor dela, so ``serverAuth``: a chave
  que fica no conteiner exposto ao terceiro nao passa no TLS do cofre nem na porta 9443 do despachante, que aceitam
  outras autoridades.
- O despachante confia so na ``ca_visitas`` para falar com a sala e leva o certificado de cliente emitido pela
  ``ca_clientes_visitas``, que so emite esse certificado.

``contexto_do_despachante`` monta o contexto TLS do ``ClienteSala`` (``ligacao/sala_cliente.py``) a partir dos
parametros ``VISITS_CA_FILE``, ``VISITS_CLIENT_CERT_FILE`` e ``VISITS_CLIENT_KEY_FILE``.
"""

from __future__ import annotations

import ssl

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros

ARQUIVOS_DO_DESPACHANTE = ("VISITS_CA_FILE", "VISITS_CLIENT_CERT_FILE", "VISITS_CLIENT_KEY_FILE")


def contexto_do_despachante(p: Parametros) -> ssl.SSLContext | None:
    """Contexto TLS do despachante para a sala; None quando a sala nao usa https (desenvolvimento e teste).

    Confia so na ``ca_visitas`` (nada da autoridade interna nem do sistema operacional) e confere o nome da sala.
    Sala por https sem os tres arquivos levanta ``ConfiguracaoInvalida`` (a partida do despachante recusa).
    """
    url = p.VISITS_INTERNAL_URL
    if not url or not url.lower().startswith("https://"):
        return None
    faltam = [nome for nome in ARQUIVOS_DO_DESPACHANTE if not getattr(p, nome)]
    if faltam:
        raise ConfiguracaoInvalida(
            [f"Sala de visitas por https exige {', '.join(faltam)} (mTLS do despachante com a sala, critica M4)."]
        )
    contexto = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=p.VISITS_CA_FILE)
    contexto.minimum_version = ssl.TLSVersion.TLSv1_2
    contexto.load_cert_chain(str(p.VISITS_CLIENT_CERT_FILE), str(p.VISITS_CLIENT_KEY_FILE))
    return contexto
