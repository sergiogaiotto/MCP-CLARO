"""Chamada das rotinas ao despachante, pela porta interna com mTLS.

As rotinas nunca falam com os sistemas nem abrem chaves: pedem ao despachante
(``POST /interno/declaracoes``) o que cada sistema declara hoje.
"""

from __future__ import annotations

import ssl
import uuid
from typing import Any

import httpx2

from mcp_claro.config.settings import Parametros


def contexto_tls_interno(p: Parametros) -> ssl.SSLContext | bool:
    """TLS so para a rede interna: autoridade interna e o certificado de cliente deste papel."""
    if not p.INTERNAL_CA_FILE:
        return False
    ctx = ssl.create_default_context(cafile=p.INTERNAL_CA_FILE)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    cert = p.INTERNAL_CLIENT_CERT_FILE or p.INTERNAL_CERT_FILE
    chave = p.INTERNAL_CLIENT_KEY_FILE or p.INTERNAL_KEY_FILE
    if cert and chave:
        ctx.load_cert_chain(cert, chave)
    return ctx


class ClienteDespachante:
    def __init__(self, p: Parametros, http: httpx2.AsyncClient | None = None) -> None:
        self.base = p.DISPATCHER_INTERNAL_URL.rstrip("/")
        self.http = http or httpx2.AsyncClient(
            verify=contexto_tls_interno(p),
            timeout=httpx2.Timeout(connect=3.0, read=60.0, write=5.0, pool=3.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        """O que o sistema declara hoje. O despachante tambem guarda em ``ferramentas_vistas``."""
        resposta = await self.http.post(f"{self.base}/interno/declaracoes", json={"sistema_id": str(sistema_id)})
        resposta.raise_for_status()
        ferramentas = resposta.json().get("ferramentas")
        if not isinstance(ferramentas, list):
            raise TypeError("resposta do despachante sem a lista de ferramentas")
        return [f for f in ferramentas if isinstance(f, dict)]

    async def fechar(self) -> None:
        await self.http.aclose()
