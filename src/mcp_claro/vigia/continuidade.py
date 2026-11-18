"""Conferencia de continuidade: o despachante pergunta ao vigia qual e o ultimo fechamento guardado.

Lado do vigia: ``GET /ultimo-fechamento``, com ``Authorization: Bearer <segredo>``. O segredo e o
conteudo de ``WATCH_CONTINUITY_TOKEN_FILE`` (o mesmo arquivo nos dois lados, pelo menos 32
caracteres), comparado em tempo constante. Sem segredo configurado, a rota responde 503. A resposta
diz so numero, hash (hexadecimal) e horas do ultimo fechamento guardado.

Lado do gateway: ``ultimo_no_vigia`` (para o despachante, na partida). Se o vigia guardou um
fechamento mais novo que o ultimo do banco (``vigia_a_frente``), o banco voltou no tempo: a porta de
saida fica fechada. Se o vigia nao responder, ``VigiaSemResposta``: o plano manda fechar tambem.
"""

from __future__ import annotations

import hmac
import ssl
from dataclasses import dataclass
from typing import Any

import httpx2
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_claro import __version__

CAMINHO = "/ultimo-fechamento"
_SQL_ULTIMO = "SELECT numero, hash, calculado_em, guardado_em FROM fechamentos_guardados ORDER BY numero DESC LIMIT 1"


def segredo_confere(cabecalho: str | None, segredo: str) -> bool:
    if not cabecalho or not cabecalho.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(cabecalho[7:].strip().encode("utf-8"), segredo.encode("utf-8"))


def rota_ultimo_fechamento(engine: AsyncEngine, segredo: SecretStr | None) -> Any:
    async def ultimo(pedido: Request) -> JSONResponse:
        sem_cache = {"Cache-Control": "no-store"}
        if segredo is None:
            return JSONResponse({"mensagem": "Conferencia de continuidade nao configurada."}, 503, headers=sem_cache)
        if not segredo_confere(pedido.headers.get("authorization"), segredo.get_secret_value()):
            return JSONResponse(
                {"mensagem": "Segredo da conferencia recusado."},
                401,
                headers={"WWW-Authenticate": 'Bearer realm="mcp-claro-vigia"'} | sem_cache,
            )
        async with engine.connect() as c:
            linha = (await c.execute(text(_SQL_ULTIMO))).first()
        corpo = {
            "numero": int(linha.numero) if linha else None,
            "hash": bytes(linha.hash).hex() if linha else None,
            "calculado_em": linha.calculado_em.isoformat() if linha and linha.calculado_em else None,
            "guardado_em": linha.guardado_em.isoformat() if linha else None,
        }
        return JSONResponse(corpo, headers=sem_cache)

    return ultimo


class VigiaSemResposta(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UltimoNoVigia:
    numero: int | None
    hash: bytes | None


async def ultimo_no_vigia(
    url: str,
    segredo: SecretStr,
    *,
    verify: ssl.SSLContext | None = None,
    cert: tuple[str, str] | None = None,
    prazo: float = 10.0,
    transporte: httpx2.AsyncBaseTransport | None = None,
) -> UltimoNoVigia:
    """Pergunta ao vigia (``WATCH_CONTINUITY_URL``) o ultimo fechamento guardado. Levanta ``VigiaSemResposta``."""
    contexto = verify or ssl.create_default_context()
    if cert is not None:
        contexto.load_cert_chain(*cert)
    async with httpx2.AsyncClient(
        transport=transporte,
        verify=contexto,
        timeout=httpx2.Timeout(prazo),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": f"mcp-claro/{__version__}"},
    ) as http:
        try:
            r = await http.get(
                f"{url.rstrip('/')}{CAMINHO}", headers={"authorization": f"Bearer {segredo.get_secret_value()}"}
            )
        except httpx2.HTTPError as erro:
            raise VigiaSemResposta(f"o vigia nao respondeu ({type(erro).__name__})") from None
    if r.status_code != 200:
        raise VigiaSemResposta(f"o vigia respondeu HTTP {r.status_code}")
    try:
        corpo = r.json()
        numero = corpo.get("numero")
        hash_hex = corpo.get("hash")
        return UltimoNoVigia(
            int(numero) if numero is not None else None,
            bytes.fromhex(hash_hex) if isinstance(hash_hex, str) else None,
        )
    except (ValueError, TypeError, AttributeError) as erro:
        raise VigiaSemResposta(f"resposta do vigia ilegivel ({type(erro).__name__})") from None


def vigia_a_frente(numero_no_banco: int | None, no_vigia: UltimoNoVigia) -> bool:
    """O vigia guardou fechamento mais novo que o ultimo do banco? Entao o banco voltou no tempo."""
    if no_vigia.numero is None:
        return False
    return numero_no_banco is None or no_vigia.numero > numero_no_banco


async def numero_no_vigia(
    url: str,
    segredo: SecretStr,
    *,
    verify: ssl.SSLContext | None = None,
    cert: tuple[str, str] | None = None,
    prazo: float = 10.0,
) -> int | None:
    """Forma que ``mcp_claro.continuidade`` espera: ``None`` se o vigia nao respondeu, ``0`` se nada guardou."""
    try:
        ultimo = await ultimo_no_vigia(url, segredo, verify=verify, cert=cert, prazo=prazo)
    except VigiaSemResposta:
        return None
    return ultimo.numero or 0
