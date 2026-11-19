"""Lado do despachante do salto ate a sala de visitas (desenho 3.4; critica B6).

Um POST por chamada, sem reenvio (``retries=0``), sem seguir desvio, sem proxy do ambiente. O prazo e o da chamada
mais uma folga (a sala ainda confere o endereco e espera o terceiro). Classificacao:

- conexao com a sala recusada ou sem completar (TCP ou TLS): ``nao_enviado`` (``SalaFora``);
- qualquer outra falha, prazo, status que nao seja 200 ou corpo fora do contrato: ``duvida``;
- 200 com corpo do contrato: a classificacao que a sala deu.

Com endereco https, o contexto TLS e obrigatorio (so a autoridade da sala, com o certificado de cliente do
despachante): quem monta e o despachante, a partir dos parametros.
"""

from __future__ import annotations

import ssl
from typing import Any

import anyio
import httpx2
from pydantic import SecretStr, ValidationError

from mcp_claro.ligacao.cliente import TETO_RESPOSTA_PADRAO, Resposta, Segredo, Sondagem
from mcp_claro.ligacao.sala_contrato import (
    MOTIVO_PEDIDO_FORA,
    PEDIDO_FORA_DO_CONTRATO,
    ROTA_CHAMAR,
    ROTA_LISTAR,
    ROTA_SONDAR,
    SALA_FORA,
    TETO_SALTO,
    FalhaNaSala,
    PedidoChamar,
    PedidoListar,
    PedidoSondar,
    ler_resposta_chamar,
    ler_resposta_listar,
    ler_resposta_sondar,
    resposta_sala_fora,
    sondagem_sala_fora,
)

_ANTES_DE_ENVIAR = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.UnsupportedProtocol)
FOLGA_PADRAO = 10.0
PRAZO_LISTAR = 120.0


class ClienteSala:
    """``Ligacao`` com servidores de terceiros pela sala de visitas."""

    def __init__(
        self, base: str, *, tls: ssl.SSLContext | None, folga: float = FOLGA_PADRAO, prazo_listar: float = PRAZO_LISTAR
    ) -> None:
        if base.lower().startswith("https://") and tls is None:
            raise ValueError("sala por https precisa do contexto TLS da sala")
        self._base = base.rstrip("/")
        self._tls = tls
        self._folga = folga
        self._prazo_listar = prazo_listar

    def _http(self, prazo: float) -> httpx2.AsyncClient:
        transporte = (
            httpx2.AsyncHTTPTransport(retries=0, verify=self._tls)
            if self._tls is not None
            else httpx2.AsyncHTTPTransport(retries=0)
        )
        return httpx2.AsyncClient(
            transport=transporte,
            timeout=httpx2.Timeout(connect=3.0, read=prazo, write=10.0, pool=3.0),
            follow_redirects=False,
            max_redirects=0,
            trust_env=False,
        )

    async def _postar(self, rota: str, corpo: bytes, prazo: float) -> tuple[int, bytes] | None:
        """(status, corpo) da sala; None quando a conexao nao se fez (nada foi enviado); (0, b"") na duvida."""
        try:
            with anyio.fail_after(prazo + 5):
                async with (
                    self._http(prazo) as http,
                    http.stream(
                        "POST", self._base + rota, content=corpo, headers={"content-type": "application/json"}
                    ) as resposta,
                ):
                    partes: list[bytes] = []
                    lidos = 0
                    async for pedaco in resposta.aiter_bytes():
                        lidos += len(pedaco)
                        if lidos > TETO_SALTO:
                            return 0, b""  # resposta grande demais: duvida
                        partes.append(pedaco)
                    return resposta.status_code, b"".join(partes)
        except _ANTES_DE_ENVIAR:
            return None
        except Exception:  # noqa: BLE001 - depois de conectar, toda falha e duvida: a sala pode ter chamado
            return 0, b""

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Segredo,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: dict[str, Any] | None = None,
        teto_resposta: int = TETO_RESPOSTA_PADRAO,
    ) -> Resposta:
        if traceparent:
            raise ValueError("rastro nunca vai a servidor de terceiros")
        chave = await segredo()
        try:
            pedido = PedidoChamar(
                endereco=endereco,
                chave=SecretStr(chave) if chave else None,
                discover_json=discover_json,
                nome=nome,
                argumentos=argumentos,
                prazo=prazo,
                esquema_declarado=esquema_declarado,
                teto_resposta=teto_resposta,
            )
        except ValidationError:
            return Resposta("nao_enviado", excecao=PEDIDO_FORA_DO_CONTRATO)
        saida = await self._postar(ROTA_CHAMAR, pedido.para_json(), prazo + self._folga)
        return resposta_sala_fora() if saida is None else ler_resposta_chamar(*saida)

    async def listar_ferramentas(
        self, endereco: str, segredo: Segredo, discover_json: str | None
    ) -> list[dict[str, Any]]:
        chave = await segredo()
        try:
            pedido = PedidoListar(
                endereco=endereco, chave=SecretStr(chave) if chave else None, discover_json=discover_json
            )
        except ValidationError:
            raise FalhaNaSala("nao_enviado", PEDIDO_FORA_DO_CONTRATO) from None
        saida = await self._postar(ROTA_LISTAR, pedido.para_json(), self._prazo_listar)
        if saida is None:
            raise FalhaNaSala("nao_enviado", SALA_FORA)
        return ler_resposta_listar(*saida)

    async def sondar(self, endereco: str, segredo: Segredo, *, prazo: float = 10.0) -> Sondagem:
        chave = await segredo()
        try:
            pedido = PedidoSondar(endereco=endereco, chave=SecretStr(chave) if chave else None, prazo=prazo)
        except ValidationError:
            return Sondagem("inalcancavel", MOTIVO_PEDIDO_FORA, excecao=PEDIDO_FORA_DO_CONTRATO)
        saida = await self._postar(ROTA_SONDAR, pedido.para_json(), prazo + self._folga)
        return sondagem_sala_fora() if saida is None else ler_resposta_sondar(*saida)
