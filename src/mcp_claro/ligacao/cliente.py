"""Cliente MCP para os sistemas conectados, fixado na 2026-07-28 (decisao 8).

Regras do plano, provadas por teste:

- nunca faz ``initialize`` nem cai para a geracao antiga: a sessao adota o
  ``server/discover`` guardado no cadastro, sem trafego ao abrir;
- uma chamada e exatamente um POST: nao usa ``Client.call_tool`` (que refaz
  voltas de ``InputRequiredResult`` e de ``HEADER_MISMATCH``) nem o OAuth do kit
  (que reenvia depois de 401). A chave vem do cofre por um ``Auth`` de passo unico;
- sem ``elicitation_callback`` e sem ``sampling_callback``: nao responde sozinho a
  pedidos do sistema; ``InputRequiredResult`` volta cru;
- nao segue redirecionamento (``max_redirects=0``), nao le variaveis de proxy do
  ambiente, recusa cookies e so fala com o endereco cadastrado;
- separa "falha comprovada antes do envio" (conexao recusada) de "duvida"
  (qualquer falha depois disso): so a primeira pode virar "nao saiu".
"""

from __future__ import annotations

import contextvars
import http.cookiejar
import ssl
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import httpx2
import mcp_types as types
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.inbound import mcp_param_headers, x_mcp_header_map
from pydantic import TypeAdapter, ValidationError

from mcp_claro import __version__
from mcp_claro.versao import VERSAO_MCP

Segredo = Callable[[], Awaitable[str | None]]
Fase = Literal["nao_enviado", "duvida"]
TipoResultado = Literal["resultado", "nao_enviado", "duvida", "erro_rpc", "recusado_http"]

_RESULTADO_CHAMADA: TypeAdapter[types.CallToolResult | types.InputRequiredResult] = TypeAdapter(
    types.CallToolResult | types.InputRequiredResult
)
_RESULTADO_LISTA: TypeAdapter[types.ListToolsResult] = TypeAdapter(types.ListToolsResult)
_NAO_ENVIADO = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.UnsupportedProtocol)
_META_PERMITIDA = ("traceparent",)
_SEM_VEREDITO = frozenset({401, 403, 407, 408, 429})
_IDENTIDADE = types.Implementation(name="mcp-claro", version=__version__)


@dataclass
class Tentativa:
    status_http: list[int] = field(default_factory=list)
    excecao: str | None = None
    fase: Fase | None = None


_TENTATIVA: contextvars.ContextVar[Tentativa | None] = contextvars.ContextVar("mcp_claro_tentativa", default=None)


TETO_RESPOSTA_PADRAO = 8 * 1024 * 1024


class RespostaGrandeDemais(httpx2.StreamError):
    pass


class _CorpoVigiado(httpx2.AsyncByteStream):
    """Le o corpo da resposta com teto de tamanho; falha no meio do corpo e sempre "duvida"."""

    def __init__(self, interno: httpx2.AsyncByteStream, tentativa: Tentativa | None, teto: int) -> None:
        self._interno = interno
        self._tentativa = tentativa
        self._teto = teto

    async def __aiter__(self) -> AsyncGenerator[bytes]:
        lidos = 0
        try:
            async for pedaco in self._interno:
                lidos += len(pedaco)
                if lidos > self._teto:
                    if self._tentativa is not None:
                        self._tentativa.fase, self._tentativa.excecao = "duvida", "RespostaGrandeDemais"
                    raise RespostaGrandeDemais(f"resposta acima de {self._teto} bytes")
                yield pedaco
        except httpx2.TransportError as erro:
            if self._tentativa is not None:
                self._tentativa.fase, self._tentativa.excecao = "duvida", type(erro).__name__
            raise httpx2.StreamError(f"corpo interrompido: {type(erro).__name__}") from erro

    async def aclose(self) -> None:
        await self._interno.aclose()


def _erro_sintetico(request: httpx2.Request, status: int, fase: Fase) -> httpx2.Response:
    return httpx2.Response(
        status,
        request=request,
        json={"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "transporte", "data": {"fase": fase}}},
    )


class _TransporteQueClassifica(httpx2.AsyncBaseTransport):
    """Converte falha de rede em resposta JSON-RPC marcada, para a sessao sobreviver e a fase ficar exata.

    Regras: so o primeiro pedido da tentativa pode virar "nao_enviado"; depois de qualquer resposta, ou
    se a fase ja for "duvida", toda falha e "duvida" (nunca rebaixa). Pedido que nao seja POST (o GET de
    retomada de stream do kit) nem sai: na 2026-07-28 o gateway so fala por POST.
    """

    def __init__(self, interno: httpx2.AsyncBaseTransport, teto_resposta: int) -> None:
        self._interno = interno
        self._teto = teto_resposta

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        tentativa = _TENTATIVA.get()
        if request.method != "POST":
            if tentativa is not None and tentativa.status_http:
                tentativa.fase = "duvida"
            return _erro_sintetico(request, 405, "duvida")
        ja_respondeu = tentativa is not None and (bool(tentativa.status_http) or tentativa.fase == "duvida")
        fase: Fase
        excecao: httpx2.TransportError
        try:
            resposta = await self._interno.handle_async_request(request)
        except _NAO_ENVIADO as erro:
            fase, excecao = ("duvida" if ja_respondeu else "nao_enviado"), erro
        except httpx2.TransportError as erro:
            fase, excecao = "duvida", erro
        else:
            if tentativa is not None:
                tentativa.status_http.append(resposta.status_code)
            tamanho = resposta.headers.get("content-length")
            if tamanho is not None and tamanho.isdigit() and int(tamanho) > self._teto:
                await resposta.aclose()
                if tentativa is not None:
                    tentativa.fase, tentativa.excecao = "duvida", "RespostaGrandeDemais"
                return _erro_sintetico(request, 502, "duvida")
            return httpx2.Response(
                status_code=resposta.status_code,
                headers=resposta.headers,
                stream=_CorpoVigiado(resposta.stream, tentativa, self._teto),  # type: ignore[arg-type]
                extensions=resposta.extensions,
                request=request,
            )
        if tentativa is not None:
            tentativa.excecao, tentativa.fase = type(excecao).__name__, fase
        return _erro_sintetico(request, 502, fase)

    async def aclose(self) -> None:
        await self._interno.aclose()


class _ChaveDoCofre(httpx2.Auth):
    """Passo unico: poe a chave e envia. Nao ha segundo ``yield``, entao nunca reenvia."""

    def __init__(self, segredo: Segredo) -> None:
        self._segredo = segredo

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        chave = await self._segredo()
        if chave:
            request.headers["Authorization"] = f"Bearer {chave}"
        yield request


def _cookies_recusados() -> http.cookiejar.CookieJar:
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def http_para_sistema(
    segredo: Segredo,
    *,
    prazo_leitura: float = 35.0,
    teto_resposta: int = TETO_RESPOSTA_PADRAO,
    cabecalhos: dict[str, str] | None = None,
) -> httpx2.AsyncClient:
    """Cliente HTTP de saida, um por chamada: TLS do sistema operacional, sem proxy do ambiente."""
    return httpx2.AsyncClient(
        transport=_TransporteQueClassifica(
            httpx2.AsyncHTTPTransport(retries=0, verify=ssl.create_default_context()), teto_resposta
        ),
        auth=_ChaveDoCofre(segredo),
        headers={"User-Agent": f"mcp-claro/{__version__}"} | (cabecalhos or {}),
        timeout=httpx2.Timeout(connect=3.0, read=prazo_leitura, write=10.0, pool=3.0),
        follow_redirects=False,
        max_redirects=0,
        trust_env=False,
        cookies=_cookies_recusados(),
    )


@dataclass(frozen=True, slots=True)
class Sondagem:
    """Resultado da sondagem de versao no cadastro de um sistema."""

    aceito: bool
    motivo: str
    versoes: tuple[str, ...] = ()
    discover_json: str | None = None
    codigo: int | None = None
    inalcancavel: bool = False


@dataclass(frozen=True, slots=True)
class Resposta:
    tipo: TipoResultado
    resultado: types.CallToolResult | types.InputRequiredResult | None = None
    erro: types.ErrorData | None = None
    status_http: tuple[int, ...] = ()
    excecao: str | None = None


def _discover(discover_json: str | None) -> types.DiscoverResult:
    if discover_json:
        return types.DiscoverResult.model_validate_json(discover_json)
    return types.DiscoverResult(supported_versions=[VERSAO_MCP], capabilities=types.ServerCapabilities())


async def sondar(endereco: str, segredo: Segredo, *, prazo: float = 10.0) -> Sondagem:
    """Pergunta ``server/discover`` na 2026-07-28. So aceita ``DiscoverResult`` valido com a versao.

    Qualquer erro JSON-RPC quer dizer "nao fala 2026-07-28". Falha de rede ou prazo
    esgotado nao e veredito de versao: volta como ``inalcancavel``.
    """
    tentativa = Tentativa()
    marca = _TENTATIVA.set(tentativa)
    try:
        async with http_para_sistema(segredo, prazo_leitura=prazo) as http:
            with anyio.fail_after(prazo + 2):
                async with (
                    streamable_http_client(endereco, http_client=http, terminate_on_close=False) as (
                        leitura,
                        escrita,
                    ),
                    ClientSession(leitura, escrita, client_info=_IDENTIDADE) as sessao,
                ):
                    try:
                        bruto = await sessao.send_discover(VERSAO_MCP)
                    except MCPError as erro:
                        status = tentativa.status_http[-1] if tentativa.status_http else None
                        if tentativa.fase is not None or erro.code == types.REQUEST_TIMEOUT:
                            return Sondagem(False, "sistema inalcancavel", codigo=erro.code, inalcancavel=True)
                        if status in _SEM_VEREDITO or (status is not None and status >= 500):
                            # O sistema respondeu, mas nao disse nada sobre versao: sem acesso ou fora do ar.
                            return Sondagem(
                                False, f"sem resposta de versao (HTTP {status})", codigo=erro.code, inalcancavel=True
                            )
                        return Sondagem(False, "o sistema nao fala a versao 2026-07-28", codigo=erro.code)
                    try:
                        resultado = types.DiscoverResult.model_validate(bruto)
                    except ValidationError:
                        return Sondagem(False, "a resposta a server/discover nao segue a 2026-07-28")
                    versoes = tuple(resultado.supported_versions)
                    if VERSAO_MCP not in versoes:
                        return Sondagem(False, "o sistema nao anuncia a versao 2026-07-28", versoes=versoes)
                    return Sondagem(
                        True,
                        "fala a 2026-07-28",
                        versoes=versoes,
                        discover_json=resultado.model_dump_json(by_alias=True, exclude_none=True),
                    )
    except TimeoutError:
        return Sondagem(False, "sistema inalcancavel (prazo esgotado)", inalcancavel=True)
    finally:
        _TENTATIVA.reset(marca)


async def listar_ferramentas(endereco: str, segredo: Segredo, discover_json: str | None) -> list[dict[str, Any]]:
    """Lista o que o sistema declara hoje (para revisao e mudou-parou). Uma pagina por POST."""
    ferramentas: list[dict[str, Any]] = []
    cursor: str | None = None
    async with (
        http_para_sistema(segredo) as http,
        streamable_http_client(endereco, http_client=http, terminate_on_close=False) as (
            leitura,
            escrita,
        ),
        ClientSession(leitura, escrita, client_info=_IDENTIDADE) as sessao,
    ):
        sessao.adopt(_discover(discover_json))
        for _ in range(50):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            pagina = await sessao.send_request(types.ListToolsRequest(params=params), _RESULTADO_LISTA)
            ferramentas.extend(t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in pagina.tools)
            cursor = pagina.next_cursor
            if not cursor:
                break
    return ferramentas


async def chamar_ferramenta(
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
    """Exatamente um POST ``tools/call``. Devolve o resultado cru ou a classificacao da falha."""
    tentativa = Tentativa()
    marca = _TENTATIVA.set(tentativa)
    meta = {"traceparent": traceparent} if traceparent else None
    # Cabecalhos Mcp-Param-* que o sistema exige quando o esquema dele marca x-mcp-header.
    cabecalhos = mcp_param_headers(x_mcp_header_map(esquema_declarado), argumentos) if esquema_declarado else {}
    try:
        async with (
            http_para_sistema(
                segredo, prazo_leitura=prazo + 5, teto_resposta=teto_resposta, cabecalhos=cabecalhos
            ) as http,
            streamable_http_client(endereco, http_client=http, terminate_on_close=False) as (
                leitura,
                escrita,
            ),
            ClientSession(leitura, escrita, client_info=_IDENTIDADE) as sessao,
        ):
            sessao.adopt(_discover(discover_json))
            pedido = types.CallToolRequest(
                params=types.CallToolRequestParams(name=nome, arguments=argumentos, _meta=meta)
            )
            try:
                resultado = await sessao.send_request(pedido, _RESULTADO_CHAMADA, request_read_timeout_seconds=prazo)
            except MCPError as erro:
                status = tuple(tentativa.status_http)
                if tentativa.fase is not None:
                    return Resposta(tentativa.fase, erro=erro.error, status_http=status, excecao=tentativa.excecao)
                if erro.code in (types.REQUEST_TIMEOUT, -32000):
                    return Resposta("duvida", erro=erro.error, status_http=status)
                if status and (status[-1] >= 500 or 200 <= status[-1] < 300):
                    # 5xx, 202, corpo invalido ou erro dentro de um 2xx: o sistema pode ter executado.
                    return Resposta("duvida", erro=erro.error, status_http=status)
                if status and (status[-1] in (401, 403) or 300 <= status[-1] < 400):
                    return Resposta("recusado_http", erro=erro.error, status_http=status)
                return Resposta("erro_rpc", erro=erro.error, status_http=status)
            return Resposta("resultado", resultado=resultado, status_http=tuple(tentativa.status_http))
    except Exception as erro:  # noqa: BLE001 - toda falha fora do protocolo vira classificacao, nunca reenvio
        status = tuple(tentativa.status_http)
        if status and 300 <= status[-1] < 400:
            return Resposta("recusado_http", status_http=status, excecao=type(erro).__name__)
        return Resposta(
            tentativa.fase or "duvida", status_http=status, excecao=tentativa.excecao or type(erro).__name__
        )
    finally:
        _TENTATIVA.reset(marca)
