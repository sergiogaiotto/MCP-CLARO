"""Chamada da entrada ao despachante, pela porta interna com mTLS."""

from __future__ import annotations

import ssl
from typing import Any

import httpx2

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.servidor import PedidoLeitura, Recusa


def contexto_tls_interno(p: Parametros) -> ssl.SSLContext | bool:
    """TLS so para a rede interna: autoridade interna e o certificado de cliente deste papel."""
    if not p.INTERNAL_CA_FILE:
        return False
    ctx = ssl.create_default_context(cafile=p.INTERNAL_CA_FILE)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Certificado de cliente proprio para chamar outra peca; sem ele, usa o do servidor deste papel.
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
            timeout=httpx2.Timeout(connect=3.0, read=float(p.RESPONSE_MAX_WAIT_SECONDS) + 10, write=5.0, pool=3.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def ler(self, pedido: PedidoLeitura) -> dict[str, Any]:
        resposta = await self.http.post(
            f"{self.base}/interno/ler",
            json={
                "pessoa_id": str(pedido.pessoa_id),
                "aplicativo_id": str(pedido.aplicativo_id),
                "ordem_id": str(pedido.ordem_id),
                "ferramenta_id": str(pedido.ferramenta_id),
                "argumentos": pedido.argumentos,
                "trace_id": pedido.trace_id,
            },
        )
        if resposta.status_code == 403:
            corpo = resposta.json()
            raise Recusa(str(corpo.get("mensagem", "Leitura recusada.")), str(corpo.get("motivo", "recusada")))
        resposta.raise_for_status()
        dados: dict[str, Any] = resposta.json()
        return dados

    async def post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        resposta = await self.http.post(f"{self.base}{caminho}", json=corpo)
        resposta.raise_for_status()
        dados: dict[str, Any] = resposta.json()
        return dados

    async def fechar(self) -> None:
        await self.http.aclose()
