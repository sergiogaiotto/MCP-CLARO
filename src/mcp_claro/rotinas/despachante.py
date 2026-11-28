"""Chamada das rotinas ao despachante, pela porta interna com mTLS.

As rotinas nunca falam com os sistemas nem abrem chaves: pedem ao despachante
(``POST /interno/declaracoes``) o que cada sistema declara hoje. O despachante le com a chave de
leitura das declaracoes guardada pelo ADMIN (a mesma que a conclusao da revisao de um servidor de
terceiros exige, critica A5).

Quando a falha e do MCP-CLARO e nao do sistema, ``declaracoes`` levanta ``ConferenciaNaoFeita``, e o
mudou-parou nao conta uma rodada sem resposta do servidor: o pedido nem chegou ao despachante (conexao
recusada, prazo da conexao, TLS), o despachante respondeu 503, disse que o sistema deixou de estar
ativo (404) ou deu um dos motivos de ``MOTIVOS_DO_MCP_CLARO``. A sala de visitas fora (``sala_fora``) e a
chave das declaracoes que o cofre nao abriu (``chave_indisponivel``) chegam assim: 503 com motivo proprio.
Qualquer outra falha conta: o despachante foi alcancado e o sistema nao entregou (por exemplo, 502
``declaracoes_indisponiveis``).

Fase 4: ``conferir`` pede ao despachante a consulta de uma operacao ao proprio sistema (``contrato.ROTA_CONSULTAR``,
``contrato.ConfereNoDespachante``), com exatamente ``{"operacao_id": "<uuid em texto>"}`` no corpo e sem "ultima": o
despachante decide pelo banco se e a vez e se e a ultima tentativa (critica M7). Respostas: 200 com o desfecho
(``Conferida``); 404 ``sem_envio`` (``SemEnvio``) e 409 ``nada_a_perguntar`` (``NadaAPerguntar``), que so pulam a
operacao; 409 ``porta_fechada`` (``PortaFechada``), nada perguntado nem gravado, para tentar na proxima rodada; 503 e
qualquer outra resposta fora do contrato da rota, ``ConferenciaNaoFeita`` (a falha e do MCP-CLARO); sem conexao,
``DespachanteFora`` (uma ``ConferenciaNaoFeita``). Os tres primeiros sao ``LookupError``, como o contrato diz.
"""

from __future__ import annotations

import ssl
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx2

from mcp_claro.config.settings import Parametros
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita
from mcp_claro.selo import contrato

MOTIVOS_DO_MCP_CLARO: Final = frozenset({"sala_fora", "chave_indisponivel", "conferencia_nao_feita"})
"""``motivo`` da resposta do despachante que diz que a falha e do MCP-CLARO, nao do sistema."""

_SEM_CONEXAO: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


class SemEnvio(LookupError):
    """404 ``sem_envio``: a operacao nao existe ou nao saiu com numero."""


class NadaAPerguntar(LookupError):
    """409 ``nada_a_perguntar``: o despachante nao perguntou (nao e a vez, a escada acabou, outra copia perguntando,
    a acao nao esta em duvida, ou outro protocolo saiu com o mesmo numero)."""


class PortaFechada(LookupError):
    """409 ``porta_fechada``: era a vez, mas a porta de saida esta fechada. Nada foi perguntado nem gravado, a escada
    nao anda; as rotinas tentam de novo na proxima rodada (decisao 2 do coordenador no retoque da etapa 3)."""


class DespachanteFora(ConferenciaNaoFeita):
    """O pedido nem chegou ao despachante (conexao recusada, prazo da conexao, TLS, grupo de conexoes cheio)."""


@dataclass(frozen=True, slots=True)
class Conferida:
    """O 200 da rota da consulta (``contrato.RespostaDaConsulta``): o desfecho e, com ``respondeu``, o estado."""

    desfecho: str
    estado: str | None


def _conferida(resposta: httpx2.Response) -> Conferida:
    """O corpo do 200, conferido contra o contrato da rota. Fora dele, a falha e do MCP-CLARO."""
    try:
        corpo = resposta.json()
    except ValueError as erro:
        raise ConferenciaNaoFeita("o despachante respondeu fora do contrato da rota") from erro
    if not isinstance(corpo, dict) or set(corpo) != {"desfecho", "estado"}:
        raise ConferenciaNaoFeita("o despachante respondeu fora do contrato da rota")
    desfecho, estado = corpo["desfecho"], corpo["estado"]
    # So ``respondeu`` traz estado (do vocabulario); os outros desfechos vem sem.
    estado_certo = estado in contrato.ESTADOS if desfecho == "respondeu" else estado is None
    if desfecho not in contrato.DESFECHOS or not estado_certo:
        raise ConferenciaNaoFeita("o despachante respondeu fora do contrato da rota")
    return Conferida(str(desfecho), None if estado is None else str(estado))


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

    async def conferir(self, operacao_id: uuid.UUID) -> Conferida:
        """Pede ao despachante a consulta da operacao ao proprio sistema (``contrato.ROTA_CONSULTAR``). Veja o
        modulo: ``SemEnvio``, ``NadaAPerguntar`` e ``PortaFechada`` sao ``LookupError``; a falha do MCP-CLARO e
        ``ConferenciaNaoFeita`` (``DespachanteFora`` quando o pedido nem chegou)."""
        try:
            resposta = await self.http.post(
                f"{self.base}{contrato.ROTA_CONSULTAR}", json={"operacao_id": str(operacao_id)}
            )
        except _SEM_CONEXAO as erro:
            raise DespachanteFora("o despachante nao atendeu") from erro
        except httpx2.HTTPError as erro:
            # Chegou, mas a resposta nao voltou inteira (prazo da leitura, conexao cortada): o despachante grava o
            # que o sistema disser; as rotinas so nao sabem agora.
            raise ConferenciaNaoFeita("a resposta do despachante nao chegou") from erro
        motivo = _motivo(resposta) if resposta.status_code >= 400 else None
        if resposta.status_code == 200:
            return _conferida(resposta)
        if resposta.status_code == 404 and motivo == "sem_envio":
            raise SemEnvio("a operacao nao saiu com numero")
        if resposta.status_code == 409 and motivo == "porta_fechada":
            raise PortaFechada("a porta de saida esta fechada")
        if resposta.status_code == 409 and motivo == "nada_a_perguntar":
            raise NadaAPerguntar("nada a perguntar agora")
        raise ConferenciaNaoFeita(f"o despachante nao consultou o sistema (HTTP {resposta.status_code})")

    async def fechar(self) -> None:
        await self.http.aclose()


_CONFERE: Final[type[contrato.ConfereNoDespachante]] = ClienteDespachante  # o mypy confere a assinatura do contrato
