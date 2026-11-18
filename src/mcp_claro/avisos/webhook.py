"""Webhook assinado pelo padrao Standard Webhooks (biblioteca ``standardwebhooks`` 1.3.0).

Cada envio leva os cabecalhos ``webhook-id`` (estavel por aviso: o destino descarta
repeticoes), ``webhook-timestamp`` (hora da tentativa, em segundos) e
``webhook-signature`` (``v1,<HMAC-SHA256 em base64>`` de ``id.timestamp.corpo``).
O corpo e JSON compacto: ``{"type": <evento>, "timestamp": <criado_em>, "data": <corpo>}``.

O segredo vem de ``WEBHOOK_SECRET_FILE`` (``Parametros.segredo_webhook``), em base64,
com ou sem o prefixo ``whsec_``, de 24 a 64 bytes. O HTTP de saida e ``httpx2``,
sem seguir redirecionamento, sem proxy do ambiente e com TLS conferido.
"""

from __future__ import annotations

import base64
import binascii
import json
import ssl
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx2
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection
from standardwebhooks import Webhook

from mcp_claro import __version__
from mcp_claro.avisos.destinos import PREFIXO_WEBHOOK, url_do_webhook
from mcp_claro.avisos.fila import Aviso, FalhaEnvio
from mcp_claro.config.settings import Parametros

PREFIXO_SEGREDO = "whsec_"
BYTES_MINIMOS = 24
BYTES_MAXIMOS = 64


class SegredoInvalido(ValueError):
    pass


def conferir_segredo(segredo: str) -> bytes:
    """Decodifica o segredo e confere o tamanho. Levanta ``SegredoInvalido`` com frase legivel."""
    bruto = segredo.strip()
    if bruto.startswith(PREFIXO_SEGREDO):
        bruto = bruto[len(PREFIXO_SEGREDO) :]
    try:
        chave = base64.b64decode(bruto + "=" * (-len(bruto) % 4), validate=True)
    except binascii.Error, ValueError:
        raise SegredoInvalido("O segredo do webhook deve estar em base64 (com ou sem whsec_).") from None
    if not BYTES_MINIMOS <= len(chave) <= BYTES_MAXIMOS:
        raise SegredoInvalido(f"O segredo do webhook deve ter de {BYTES_MINIMOS} a {BYTES_MAXIMOS} bytes.")
    return chave


def corpo_do_webhook(aviso: Aviso) -> bytes:
    """Corpo JSON compacto e estavel do aviso. E exatamente o que se assina."""
    carga: dict[str, Any] = {
        "type": aviso.evento,
        "timestamp": aviso.criado_em.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "data": aviso.corpo,
    }
    return json.dumps(carga, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def assinar(chave: bytes, msg_id: str, quando: datetime, corpo: bytes) -> dict[str, str]:
    """Cabecalhos do Standard Webhooks para um corpo. ``quando`` vira segundos inteiros."""
    assinatura = Webhook(chave).sign(msg_id=msg_id, timestamp=quando, data=corpo)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(quando.timestamp())),
        "webhook-signature": assinatura,
    }


class EnvioWebhook:
    """Canal ``webhook``. So ``https://``; 2xx e entrega, qualquer outra coisa e falha."""

    def __init__(
        self,
        segredo: SecretStr,
        *,
        prazo: float = 10.0,
        verify: ssl.SSLContext | None = None,
        transporte: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._chave = conferir_segredo(segredo.get_secret_value())
        self.prazo = prazo
        self.verify = verify or ssl.create_default_context()
        self.transporte = transporte

    @classmethod
    def de_parametros(cls, p: Parametros) -> EnvioWebhook | None:
        """``None`` sem ``WEBHOOK_SECRET_FILE``: sem segredo, nenhum webhook sai sem assinatura."""
        segredo = p.segredo_webhook
        if segredo is None:
            return None
        return cls(segredo)

    async def url(self, conexao: AsyncConnection, aviso: Aviso) -> str:
        destino = aviso.destino
        if destino.startswith(PREFIXO_WEBHOOK):
            try:
                destino_id = uuid.UUID(destino[len(PREFIXO_WEBHOOK) :])
            except ValueError:
                raise FalhaEnvio("destino de webhook invalido", permanente=True) from None
            url = await url_do_webhook(conexao, destino_id)
            if url is None:
                raise FalhaEnvio("destino de webhook desativado ou removido", permanente=True)
            destino = url
        if not destino.startswith("https://"):
            raise FalhaEnvio("webhook so sai para https://", permanente=True)
        return destino

    def _cliente(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=self.transporte,
            verify=self.verify,
            timeout=httpx2.Timeout(connect=5.0, read=self.prazo, write=self.prazo, pool=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": f"mcp-claro/{__version__}"},
        )

    async def enviar(self, conexao: AsyncConnection, aviso: Aviso) -> None:
        url = await self.url(conexao, aviso)
        corpo = corpo_do_webhook(aviso)
        cabecalhos = assinar(self._chave, aviso.identificador, datetime.now(UTC), corpo)
        cabecalhos["content-type"] = "application/json"
        try:
            async with self._cliente() as http:
                resposta = await http.post(url, content=corpo, headers=cabecalhos)
        except httpx2.HTTPError as erro:
            raise FalhaEnvio(f"webhook sem resposta ({type(erro).__name__})") from None
        if not 200 <= resposta.status_code < 300:
            raise FalhaEnvio(f"webhook respondeu HTTP {resposta.status_code}")
