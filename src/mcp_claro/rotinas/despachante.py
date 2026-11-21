"""Chamada das rotinas ao despachante, pela porta interna com mTLS.

As rotinas nunca falam com os sistemas nem abrem chaves: pedem ao despachante
(``POST /interno/declaracoes``) o que cada sistema declara hoje. O despachante le com a chave de
leitura das declaracoes guardada pelo ADMIN (a mesma que a conclusao da revisao de um servidor de
terceiros exige, critica A5).

Quando a falha e do MCP-CLARO e nao do sistema, ``declaracoes`` levanta ``ConferenciaNaoFeita``, e o
mudou-parou nao conta uma rodada sem resposta do servidor: o pedido nem chegou ao despachante (conexao
recusada, prazo da conexao, TLS), o despachante respondeu 503, disse que o sistema deixou de estar
ativo (404) ou deu um dos motivos de ``MOTIVOS_DO_MCP_CLARO``. Qualquer outra falha conta: o despachante
foi alcancado e o sistema nao entregou.

Hoje o despachante ainda responde 502 ``declaracoes_indisponiveis`` tambem com a sala de visitas fora e com
a chave das declaracoes que o cofre nao abriu; esses casos contam como rodada sem resposta (o lado seguro)
ate ele responder 503 ou com o motivo ``sala_fora`` ou ``chave_indisponivel`` (pedido ao G3a). As rotinas
ja tratam esses motivos como conferencia nao feita.
"""

from __future__ import annotations

import ssl
import uuid
from typing import Any, Final

import httpx2

from mcp_claro.config.settings import Parametros
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita

MOTIVOS_DO_MCP_CLARO: Final = frozenset({"sala_fora", "chave_indisponivel", "conferencia_nao_feita"})
"""``motivo`` da resposta do despachante que diz que a falha e do MCP-CLARO, nao do sistema."""

_SEM_CONEXAO: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


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


def _motivo(resposta: httpx2.Response) -> str | None:
    try:
        corpo = resposta.json()
    except ValueError:
        return None
    motivo = corpo.get("motivo") if isinstance(corpo, dict) else None
    return motivo if isinstance(motivo, str) else None


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
        try:
            resposta = await self.http.post(f"{self.base}/interno/declaracoes", json={"sistema_id": str(sistema_id)})
        except _SEM_CONEXAO as erro:
            raise ConferenciaNaoFeita("o despachante nao atendeu") from erro
        if resposta.status_code in (404, 503) or (
            resposta.status_code >= 400 and _motivo(resposta) in MOTIVOS_DO_MCP_CLARO
        ):
            raise ConferenciaNaoFeita(f"o despachante nao conferiu o sistema (HTTP {resposta.status_code})")
        resposta.raise_for_status()
        ferramentas = resposta.json().get("ferramentas")
        if not isinstance(ferramentas, list):
            raise TypeError("resposta do despachante sem a lista de ferramentas")
        return [f for f in ferramentas if isinstance(f, dict)]

    async def fechar(self) -> None:
        await self.http.aclose()
