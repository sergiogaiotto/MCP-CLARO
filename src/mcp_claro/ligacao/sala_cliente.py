"""Lado do despachante do salto ate a sala de visitas (desenho 3.4; critica B6).

Um POST por chamada, sem reenvio (``retries=0``), sem seguir desvio, sem proxy do ambiente. O prazo e o da chamada
mais uma folga (a sala ainda confere o endereco e espera o terceiro). Classificacao:

- conexao com a sala recusada ou sem completar (TCP ou TLS): ``nao_enviado`` (``SalaFora``);
- pedido que o escritor de JSON nao consegue montar (argumentos com 256 niveis ou mais, um dentro do outro):
  ``nao_enviado`` (``PEDIDO_FUNDO_DEMAIS``), antes de abrir conexao com a sala;
- qualquer outra falha, prazo, status que nao seja 200 ou corpo fora do contrato: ``duvida``;
- chamada com o numero da operacao (``chave_operacao``, Fase 4): ``nao_enviado`` (``NUMERO_SEM_SALA_V2``), sem
  abrir a chave nem conexao, ate o contrato da sala v2 (G7, etapa 5); numero fora da forma uuid, ``ValueError``;
- 200 com corpo do contrato: a classificacao que a sala deu. A sala cheia responde assim, na hora em que desiste
  da vaga (``visitas.app.PRAZO_VAGA``): ``nao_enviado`` com ``sala_sem_vaga``, sem nada ir a um filho, e nao uma
  espera que come o prazo do despachante e vira ``duvida``. Na listagem, a sala cheia com o cabecalho que so o
  processo pai escreve (``CABECALHO_DA_SALA``) levanta ``SalaSemVaga``; sem o cabecalho, ``FalhaNaSala``.

Com endereco https, o contexto TLS e obrigatorio (so a autoridade da sala, com o certificado de cliente do
despachante): quem monta e o despachante, a partir dos parametros.
"""

from __future__ import annotations

import ssl
from typing import Any, Final

import anyio
import httpx2
from pydantic import SecretStr, ValidationError

from mcp_claro.ligacao.cliente import (
    PEDIDO_FUNDO_DEMAIS,
    TETO_RESPOSTA_PADRAO,
    Listagem,
    Resposta,
    Segredo,
    Sondagem,
    conferir_numero,
)
from mcp_claro.ligacao.sala_contrato import (
    CABECALHO_DA_SALA,
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
    ler_resposta_listagem,
    ler_resposta_sondar,
    resposta_sala_fora,
    sondagem_sala_fora,
)

_ANTES_DE_ENVIAR = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.UnsupportedProtocol)
FOLGA_PADRAO = 10.0
PRAZO_LISTAR = 120.0
NUMERO_SEM_SALA_V2: Final = "numero_sem_sala_v2"
"""``Resposta.excecao`` (com ``nao_enviado``) de uma chamada com o numero da operacao pela sala: o contrato da sala
v1 nao o leva, e nada sai ate a v2 (G7, etapa 5; critica B1)."""


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

    async def _postar(self, rota: str, corpo: bytes, prazo: float) -> tuple[int, bytes, str | None] | None:
        """(status, corpo, marca) da sala; None quando a conexao nao se fez (nada foi enviado); (0, b"", None) na
        duvida. ``marca``: o ``CABECALHO_DA_SALA``, que so o processo pai escreve (sala cheia)."""
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
                            return 0, b"", None  # resposta grande demais: duvida
                        partes.append(pedaco)
                    return resposta.status_code, b"".join(partes), resposta.headers.get(CABECALHO_DA_SALA)
        except _ANTES_DE_ENVIAR:
            return None
        except Exception:  # noqa: BLE001 - depois de conectar, toda falha e duvida: a sala pode ter chamado
            return 0, b"", None

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
        chave_operacao: str | None = None,
    ) -> Resposta:
        if traceparent:
            raise ValueError("rastro nunca vai a servidor de terceiros")
        conferir_numero(chave_operacao)
        if chave_operacao is not None:
            # O contrato da sala v1 nao tem o numero (``PedidoChamar`` recusa campo a mais): ate a v2 (G7, etapa 5),
            # nada sai com numero para um terceiro, nem a chave abre. O selo de terceiro tambem ainda e recusado.
            return Resposta("nao_enviado", excecao=NUMERO_SEM_SALA_V2)
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
        try:
            corpo = pedido.para_json()
        except ValueError, RecursionError:
            # O escritor do pydantic para em 256 niveis (PydanticSerializationError, um ValueError): nada saiu.
            return Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)
        saida = await self._postar(ROTA_CHAMAR, corpo, prazo + self._folga)
        return resposta_sala_fora() if saida is None else ler_resposta_chamar(saida[0], saida[1])

    async def listar(self, endereco: str, segredo: Segredo, discover_json: str | None) -> Listagem:
        """A listagem pela sala, com o que o terceiro pediu no meio (``RespostaListar.pedidos``, conferido). A sala
        cheia, dita pelo pai (com ``CABECALHO_DA_SALA``), levanta ``SalaSemVaga``."""
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
        status, corpo, marca = saida
        return ler_resposta_listagem(status, corpo, marca=marca)

    async def listar_ferramentas(
        self, endereco: str, segredo: Segredo, discover_json: str | None
    ) -> list[dict[str, Any]]:
        return (await self.listar(endereco, segredo, discover_json)).ferramentas

    async def sondar(self, endereco: str, segredo: Segredo, *, prazo: float = 10.0) -> Sondagem:
        chave = await segredo()
        try:
            pedido = PedidoSondar(endereco=endereco, chave=SecretStr(chave) if chave else None, prazo=prazo)
        except ValidationError:
            return Sondagem("inalcancavel", MOTIVO_PEDIDO_FORA, excecao=PEDIDO_FORA_DO_CONTRATO)
        saida = await self._postar(ROTA_SONDAR, pedido.para_json(), prazo + self._folga)
        return sondagem_sala_fora() if saida is None else ler_resposta_sondar(saida[0], saida[1])
