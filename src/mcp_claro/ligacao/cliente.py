"""Cliente MCP para os sistemas conectados, fixado na 2026-07-28 (decisao 8).

Regras do plano, provadas por teste:

- nunca faz ``initialize`` nem cai para a geracao antiga: a sessao adota o
  ``server/discover`` guardado no cadastro, sem trafego ao abrir;
- uma chamada e exatamente um POST de pedido: nao usa ``Client.call_tool`` (que refaz
  voltas de ``InputRequiredResult`` e de ``HEADER_MISMATCH``) nem o OAuth do kit
  (que reenvia depois de 401). A chave vem do cofre por um ``Auth`` de passo unico.
  A ``Tentativa`` conta os pedidos: o segundo pedido de uma chamada nao sai e a
  chamada vira "duvida";
- barreira de pedidos (vale para todos os sistemas): nenhum POST que nao seja pedido
  sai. A resposta que o kit daria a um pedido do sistema pelo fio antigo (SSE), uma
  notificacao, uma lista ou um corpo ilegivel ficam no transporte, com um 202 que nao
  conta como status. Cada pedido barrado vira uma categoria do nosso vocabulario
  (``Resposta.pedidos``), nunca com texto, link ou nome de campo do sistema;
- sem ``elicitation_callback`` e sem ``sampling_callback``: nao responde sozinho a
  pedidos do sistema; ``InputRequiredResult`` volta cru;
- rastro: toda chamada ao kit roda num contexto OpenTelemetry vazio; o transporte tira
  ``traceparent``, ``tracestate`` e ``baggage`` dos cabecalhos e, num servidor de
  terceiros, deixa no ``_meta`` do corpo so o envelope do MCP (com ou sem provedor de
  rastros instalado);
- nao segue redirecionamento (``max_redirects=0``), nao le variaveis de proxy do
  ambiente, recusa cookies e so fala com o endereco cadastrado; num servidor de
  terceiros, so https e so IP publico (``conferir_endereco_terceiro``, que confere o
  mesmo nome que o cliente HTTP conecta, na forma IDNA), com nome e versao genericos,
  e do ``server/discover`` guarda so uma lista do que fica (``discover_para_guardar``);
- separa "falha comprovada antes do envio" (conexao recusada) de "duvida"
  (qualquer falha depois disso): so a primeira pode virar "nao saiu".
"""

from __future__ import annotations

import contextvars
import http.cookiejar
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import httpx2
import mcp_types as types
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.inbound import mcp_param_headers, x_mcp_header_map
from opentelemetry import context as otel_context
from opentelemetry import propagate
from pydantic import TypeAdapter, ValidationError

from mcp_claro import __version__
from mcp_claro.versao import VERSAO_MCP

Segredo = Callable[[], Awaitable[str | None]]
Fase = Literal["nao_enviado", "duvida"]
TipoResultado = Literal["resultado", "nao_enviado", "duvida", "erro_rpc", "recusado_http"]
Desfecho = Literal["aceito", "versao", "desvio", "nao_mcp", "sem_acesso", "inalcancavel"]

_RESULTADO_CHAMADA: TypeAdapter[types.CallToolResult | types.InputRequiredResult] = TypeAdapter(
    types.CallToolResult | types.InputRequiredResult
)
_RESULTADO_LISTA: TypeAdapter[types.ListToolsResult] = TypeAdapter(types.ListToolsResult)
_NAO_ENVIADO = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.UnsupportedProtocol)
_IDENTIDADE = types.Implementation(name="mcp-claro", version=__version__)
# Para terceiros, nome e versao genericos (D11): o servidor de fora nao fica sabendo qual versao do produto roda.
_IDENTIDADE_TERCEIRO = types.Implementation(name="mcp-claro", version="1")
_AGENTE_TERCEIRO = "mcp-claro"

# Pedidos que um sistema pode fazer ao cliente. O kit poe o metodo em ``error.data`` quando recusa um pedido que a
# 2026-07-28 nao tem (mcp/client/session.py, ``_on_request``); qualquer outra coisa vira "desconhecido".
METODOS_DO_SISTEMA = frozenset({"elicitation/create", "sampling/createMessage", "roots/list", "ping"})
DESCONHECIDO = "desconhecido"
NOTIFICACAO = "notificacao"  # notificacao do proprio cliente: barrada, mas nao e pedido do sistema
# Categorias de ``Resposta.pedidos`` (vocabulario nosso; as telas e o extrato traduzem).
PEDIDO_FORMULARIO = "formulario"
PEDIDO_LINK = "link"
PEDIDO_AMOSTRAGEM = "amostragem"
PEDIDO_RAIZES = "raizes"
PEDIDO_SO_ESTADO = "so_estado"
PEDIDO_LINK_CODIGO_ANTIGO = "link_codigo_antigo"
PREFIXO_FIO_ANTIGO = "fio_antigo:"

# Nomes de excecao que o chamador pode reconhecer em ``Resposta.excecao`` e ``Sondagem.excecao``.
PEDIDO_A_MAIS = "PedidoAMais"
ENDERECO_RECUSADO = "EnderecoRecusado"
NOME_NAO_RESOLVIDO = "NomeNaoResolvido"

_CABECALHOS_DE_RASTRO = frozenset({"traceparent", "tracestate", "baggage"})
_PREFIXO_ENVELOPE = "io.modelcontextprotocol/"
_PAGINAS = 50

AMBIENTES_LOCAIS = frozenset({"desenvolvimento", "teste"})
VERSAO_ANUNCIADA = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # nunca \d: aceitaria digitos de outras escritas
MAX_VERSOES = 10
OUTRA = "outra"

# Motivos da sondagem: so textos nossos (vao a sistemas.motivo e a tela do ADMIN; nada do servidor entra).
MOTIVO_ACEITO = "fala a 2026-07-28"
MOTIVO_INALCANCAVEL = "sistema inalcancavel"
MOTIVO_PRAZO = "sistema inalcancavel (prazo esgotado)"
MOTIVO_ENDERECO_RECUSADO = "endereco do servidor de terceiros recusado; nada foi enviado"
MOTIVO_NAO_FALA = "o sistema nao fala a versao 2026-07-28"
MOTIVO_NAO_SEGUE = "a resposta a server/discover nao segue a 2026-07-28"
MOTIVO_NAO_ANUNCIA = "o sistema nao anuncia a versao 2026-07-28"
MOTIVO_TRANSPORTE_ANTIGO = "o sistema usa o transporte antigo do MCP e nao fala a versao 2026-07-28 (HTTP 405)"
MOTIVO_DESVIO = "o sistema pediu para ir a outro endereco (HTTP {status}); o MCP-CLARO nao segue desvios"
MOTIVO_SEM_ACESSO = "sem resposta de versao: o sistema pediu uma chave (HTTP {status})"
MOTIVO_SEM_RESPOSTA = "sem resposta de versao (HTTP {status})"
MOTIVO_NAO_MCP = "o endereco nao respondeu como um servidor MCP (HTTP {status})"
MOTIVOS_DA_SONDAGEM = frozenset(
    {
        MOTIVO_ACEITO,
        MOTIVO_INALCANCAVEL,
        MOTIVO_PRAZO,
        MOTIVO_ENDERECO_RECUSADO,
        MOTIVO_NAO_FALA,
        MOTIVO_NAO_SEGUE,
        MOTIVO_NAO_ANUNCIA,
        MOTIVO_TRANSPORTE_ANTIGO,
    }
)
MOTIVOS_COM_STATUS = (MOTIVO_DESVIO, MOTIVO_SEM_ACESSO, MOTIVO_SEM_RESPOSTA, MOTIVO_NAO_MCP)


@dataclass
class Tentativa:
    """O que o transporte viu numa chamada: status, tipo de conteudo, pedidos que sairam e os barrados."""

    status_http: list[int] = field(default_factory=list)
    excecao: str | None = None
    fase: Fase | None = None
    teto_pedidos: int | None = None
    pedidos: int = 0
    pedidos_a_mais: int = 0
    pedidos_barrados: list[str] = field(default_factory=list)
    tipos: list[str] = field(default_factory=list)
    espiar: bool = False
    corpos_jsonrpc: list[bool] = field(default_factory=list)


_TENTATIVA: contextvars.ContextVar[Tentativa | None] = contextvars.ContextVar("mcp_claro_tentativa", default=None)


TETO_RESPOSTA_PADRAO = 8 * 1024 * 1024


class RespostaGrandeDemais(httpx2.StreamError):
    pass


class EnderecoRecusado(ValueError):
    """Endereco de servidor de terceiros fora do combinado (https, sem usuario nem fragmento, IP publico)."""


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


_ILEGIVEL = object()


def _corpo_json(request: httpx2.Request) -> Any:
    try:
        return json.loads(request.content)
    except httpx2.RequestNotRead, ValueError, RecursionError:
        return _ILEGIVEL


def _e_pedido(corpo: Any) -> bool:
    if not isinstance(corpo, dict) or not isinstance(corpo.get("method"), str):
        return False
    ident = corpo.get("id")
    return isinstance(ident, str | int) and not isinstance(ident, bool)


def _categoria_barrada(corpo: Any) -> str | None:
    """None se o corpo e um pedido (sai). Senao, a categoria do que foi barrado."""
    if _e_pedido(corpo):
        return None
    if isinstance(corpo, dict):
        if isinstance(corpo.get("method"), str) and "id" not in corpo:
            return NOTIFICACAO
        erro = corpo.get("error")
        dado = erro.get("data") if isinstance(erro, dict) else None
        if isinstance(dado, str) and dado in METODOS_DO_SISTEMA:
            return dado
    return DESCONHECIDO


def _so_pedido(request: httpx2.Request) -> str | None:
    """Barreira de pedidos: None quando o POST e um pedido (objeto com ``method`` e ``id``) e pode sair.

    Qualquer outro corpo (resposta ou erro a um pedido do sistema, notificacao, lista, JSON invalido) nao sai e
    devolve a categoria: o metodo que o kit pos em ``error.data``, se conhecido; ``notificacao`` para notificacao
    do proprio cliente; senao ``desconhecido``.
    """
    return _categoria_barrada(_corpo_json(request))


def _campos_de_rastro() -> frozenset[str]:
    return _CABECALHOS_DE_RASTRO | {c.lower() for c in propagate.get_global_textmap().fields}


def _sem_rastro(request: httpx2.Request, corpo: Any, *, terceiro: bool) -> httpx2.Request:
    """Tira os cabecalhos de rastro de todo POST e, num terceiro, tudo do ``_meta`` que nao e envelope do MCP."""
    campos = _campos_de_rastro()
    itens = request.headers.multi_items()
    cabecalhos = [(k, v) for k, v in itens if k.lower() not in campos]
    conteudo: bytes | None = None
    if terceiro and isinstance(corpo, dict):
        params = corpo.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if isinstance(params, dict) and isinstance(meta, dict):
            limpo = {k: v for k, v in meta.items() if isinstance(k, str) and k.startswith(_PREFIXO_ENVELOPE)}
            if limpo != meta:
                novo = corpo | {"params": params | {"_meta": limpo}}
                conteudo = json.dumps(novo, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if conteudo is None and len(cabecalhos) == len(itens):
        return request
    if conteudo is not None:
        cabecalhos = [(k, v) for k, v in cabecalhos if k.lower() != "content-length"]
    return httpx2.Request(
        request.method,
        request.url,
        headers=cabecalhos,
        content=conteudo if conteudo is not None else request.content,
        extensions=request.extensions,
    )


def _e_jsonrpc(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("jsonrpc") == "2.0" and any(k in obj for k in ("result", "error"))


def _dados_jsonrpc(dados: bytes) -> bool:
    try:
        return _e_jsonrpc(json.loads(dados))
    except ValueError, RecursionError:
        return False


def _tem_jsonrpc(cabecalhos: httpx2.Headers, bruto: bytes) -> bool:
    """O corpo JSON traz uma resposta JSON-RPC? (so na sondagem: separa "versao" de "nao e MCP")."""
    if not cabecalhos.get("content-type", "").lower().startswith("application/json"):
        return False
    codificacao = cabecalhos.get("content-encoding")
    try:
        conteudo = httpx2.Response(
            200, headers={"content-encoding": codificacao} if codificacao else None, content=bruto
        ).read()
        return _e_jsonrpc(json.loads(conteudo))
    except httpx2.DecodingError, ValueError, RecursionError:
        return False


class _SseFarejado(_CorpoVigiado):
    """Passa o SSE adiante sem esperar o fim (o stream pode ficar aberto) e marca se veio JSON-RPC nele.

    Le os eventos como o SSE manda: as linhas ``data:`` de um evento se juntam (com quebra de linha) ate a linha
    vazia que fecha o evento; so entao o JSON do evento e conferido.
    """

    def __init__(self, interno: httpx2.AsyncByteStream, tentativa: Tentativa, teto: int) -> None:
        super().__init__(interno, tentativa, teto)
        self._marcas = tentativa.corpos_jsonrpc
        self._indice = len(self._marcas)
        self._marcas.append(False)
        self._pendente: list[bytes] = []  # pedacos da linha ainda sem fim
        self._dados: list[bytes] = []  # linhas data: do evento aberto

    async def __aiter__(self) -> AsyncGenerator[bytes]:
        async for pedaco in super().__aiter__():
            if not self._marcas[self._indice]:
                self._marcas[self._indice] = self._farejar(pedaco)
            yield pedaco

    def _farejar(self, pedaco: bytes) -> bool:
        partes = pedaco.split(b"\n")
        if len(partes) == 1:
            self._pendente.append(pedaco)
            return False
        linhas = [b"".join([*self._pendente, partes[0]]), *partes[1:-1]]
        self._pendente = [partes[-1]]
        return any(self._linha(linha) for linha in linhas)

    def _linha(self, linha: bytes) -> bool:
        """Uma linha do SSE; True quando ela fecha um evento cujo JSON e uma resposta JSON-RPC."""
        linha = linha.removesuffix(b"\r")
        if not linha:
            dados, self._dados = self._dados, []
            return bool(dados) and _dados_jsonrpc(b"\n".join(dados))
        campo, _, valor = linha.partition(b":")
        if campo == b"data":
            self._dados.append(valor)  # o espaco depois de "data:" nao muda o JSON
        return False


class _TransporteQueClassifica(httpx2.AsyncBaseTransport):
    """Converte falha de rede em resposta JSON-RPC marcada, para a sessao sobreviver e a fase ficar exata.

    Regras: so o primeiro pedido da tentativa pode virar "nao_enviado"; depois de qualquer resposta, ou
    se a fase ja for "duvida", toda falha e "duvida" (nunca rebaixa). Pedido que nao seja POST (o GET de
    retomada de stream do kit) nem sai: na 2026-07-28 o gateway so fala por POST. POST que nao seja pedido
    tambem nao sai (barreira de pedidos), e pedido alem do teto da tentativa vira "duvida" sem sair.
    """

    def __init__(self, interno: httpx2.AsyncBaseTransport, teto_resposta: int, *, terceiro: bool = False) -> None:
        self._interno = interno
        self._teto = teto_resposta
        self._terceiro = terceiro

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        tentativa = _TENTATIVA.get()
        if request.method != "POST":
            if tentativa is not None and tentativa.status_http:
                tentativa.fase = "duvida"
            return _erro_sintetico(request, 405, "duvida")
        corpo = _corpo_json(request)
        barrado = _categoria_barrada(corpo)
        if barrado is not None:
            # Nao sai e nao soma status: o 202 so encerra o envio do kit.
            if tentativa is not None and barrado != NOTIFICACAO:
                tentativa.pedidos_barrados.append(barrado)
            return httpx2.Response(202, request=request)
        if tentativa is not None:
            if tentativa.teto_pedidos is not None and tentativa.pedidos >= tentativa.teto_pedidos:
                tentativa.pedidos_a_mais += 1
                tentativa.fase, tentativa.excecao = "duvida", PEDIDO_A_MAIS
                return _erro_sintetico(request, 502, "duvida")
            tentativa.pedidos += 1
        request = _sem_rastro(request, corpo, terceiro=self._terceiro)
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
                tentativa.tipos.append(resposta.headers.get("content-type", ""))
            tamanho = resposta.headers.get("content-length")
            if tamanho is not None and tamanho.isdigit() and int(tamanho) > self._teto:
                await resposta.aclose()
                if tentativa is not None:
                    tentativa.fase, tentativa.excecao = "duvida", "RespostaGrandeDemais"
                return _erro_sintetico(request, 502, "duvida")
            if tentativa is not None and tentativa.espiar:
                return await self._espiar(request, resposta, tentativa)
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

    async def _espiar(
        self, request: httpx2.Request, resposta: httpx2.Response, tentativa: Tentativa
    ) -> httpx2.Response:
        """Marca se a resposta traz JSON-RPC e a devolve igual ao kit: o SSE e farejado no caminho, sem esperar o
        fim; o resto e lido inteiro (com teto), como o kit leria."""
        if resposta.headers.get("content-type", "").lower().startswith("text/event-stream"):
            return httpx2.Response(
                status_code=resposta.status_code,
                headers=resposta.headers,
                stream=_SseFarejado(resposta.stream, tentativa, self._teto),  # type: ignore[arg-type]
                extensions=resposta.extensions,
                request=request,
            )
        try:
            partes = [p async for p in _CorpoVigiado(resposta.stream, tentativa, self._teto)]  # type: ignore[arg-type]
        except httpx2.StreamError:
            tentativa.corpos_jsonrpc.append(False)
            return _erro_sintetico(request, 502, "duvida")
        finally:
            await resposta.aclose()
        bruto = b"".join(partes)
        tentativa.corpos_jsonrpc.append(_tem_jsonrpc(resposta.headers, bruto))
        return httpx2.Response(
            status_code=resposta.status_code,
            headers=resposta.headers,
            stream=httpx2.ByteStream(bruto),
            extensions=resposta.extensions,
            request=request,
        )

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
    terceiro: bool = False,
) -> httpx2.AsyncClient:
    """Cliente HTTP de saida, um por chamada: TLS do sistema operacional, sem proxy do ambiente."""
    agente = _AGENTE_TERCEIRO if terceiro else f"mcp-claro/{__version__}"
    return httpx2.AsyncClient(
        transport=_TransporteQueClassifica(
            httpx2.AsyncHTTPTransport(retries=0, verify=ssl.create_default_context()), teto_resposta, terceiro=terceiro
        ),
        auth=_ChaveDoCofre(segredo),
        headers={"User-Agent": agente} | (cabecalhos or {}),
        timeout=httpx2.Timeout(connect=3.0, read=prazo_leitura, write=10.0, pool=3.0),
        follow_redirects=False,
        max_redirects=0,
        trust_env=False,
        cookies=_cookies_recusados(),
    )


def _ip_publico(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # Enderecos IPv6 que carregam um IPv4 dentro (mapeado, 6to4, Teredo, NAT64): vale o IPv4 de dentro.
        dentro: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            dentro.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            dentro.append(ip.sixtofour)
        if ip.teredo is not None:
            dentro.extend(ip.teredo)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            dentro.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        return all(d.is_global for d in dentro)
    return True


def _nome_de_conexao(endereco: str) -> str:
    """O nome que o cliente HTTP vai de fato procurar e conectar, lido pelo mesmo leitor de endereco do ``httpx``.

    Nome internacional sai na forma ASCII (IDNA), como o ``httpx`` manda. Nome que o IDNA recusa ou com caractere
    de controle levanta ``EnderecoRecusado``. Conferir este nome, e nao o de outro leitor, fecha a diferenca entre
    o nome conferido e o nome conectado.
    """
    try:
        return httpx2.URL(endereco).raw_host.decode("ascii")
    except httpx2.InvalidURL, UnicodeError:
        raise EnderecoRecusado("nome de servidor invalido") from None


async def conferir_endereco_terceiro(endereco: str, ambiente: str, *, liberar_locais: bool = False) -> None:
    """O endereco combinado de um servidor de terceiros, antes de abrir conexao.

    https (http so em desenvolvimento e teste), sem usuario, senha nem fragmento, nome valido (IDNA), e todo IP
    do nome publico. ``liberar_locais`` so vale em desenvolvimento e teste (servidores de teste em 127.0.0.1);
    fora deles, nao muda nada. Levanta ``EnderecoRecusado``; nome que nao resolve levanta ``OSError`` (nada foi
    enviado).
    """
    local = ambiente in AMBIENTES_LOCAIS
    try:
        partes = urllib.parse.urlsplit(endereco)
        porta = partes.port
    except ValueError:
        raise EnderecoRecusado("endereco invalido") from None
    esquema = partes.scheme.lower()
    if esquema != "https" and not (local and esquema == "http"):
        raise EnderecoRecusado("servidor de terceiros so por https")
    if partes.username is not None or partes.password is not None or partes.fragment or "@" in partes.netloc:
        raise EnderecoRecusado("endereco com usuario, senha ou fragmento")
    if not partes.hostname:
        raise EnderecoRecusado("endereco sem nome de servidor")
    host = _nome_de_conexao(endereco)
    if local and liberar_locais:
        return
    try:
        infos = await anyio.getaddrinfo(host, porta or (443 if esquema == "https" else 80), type=socket.SOCK_STREAM)
    except ValueError:  # UnicodeError do IDNA, caractere nulo: o nome nao serve, nada foi enviado
        raise EnderecoRecusado("nome de servidor invalido") from None
    if not infos:
        raise OSError("nome sem endereco")
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            raise EnderecoRecusado("endereco que nao e IP") from None
        if not _ip_publico(ip):
            raise EnderecoRecusado("endereco que nao e publico")


async def _endereco_do_terceiro(endereco: str, ambiente: str, liberar_locais: bool, prazo: float) -> str | None:
    """None se o endereco pode ser chamado; senao, o nome da recusa (nada foi enviado)."""
    try:
        with anyio.fail_after(prazo):
            await conferir_endereco_terceiro(endereco, ambiente, liberar_locais=liberar_locais)
    except ValueError:  # EnderecoRecusado e qualquer outro nome que nao serve
        return ENDERECO_RECUSADO
    except OSError, TimeoutError:
        return NOME_NAO_RESOLVIDO
    return None


def versoes_anunciadas(bruto: Any) -> tuple[str, ...]:
    """Versoes que o sistema anunciou, so no formato AAAA-MM-DD (digitos ASCII), ate 10; o resto vira "outra"."""
    if not isinstance(bruto, list | tuple):
        return ()
    vistas: list[str] = []
    for v in bruto:
        item = v if isinstance(v, str) and VERSAO_ANUNCIADA.fullmatch(v) else OUTRA
        if item not in vistas:
            vistas.append(item)
    if len(vistas) > MAX_VERSOES:
        vistas = [*[v for v in vistas if v != OUTRA][: MAX_VERSOES - 1], OUTRA]
    return tuple(vistas)


@dataclass(frozen=True, slots=True)
class Sondagem:
    """Resultado da sondagem de versao no cadastro de um sistema.

    ``desfecho``: ``aceito``; ``versao`` (respondeu como MCP e nao fala a 2026-07-28: so este conta como versao
    antiga); ``desvio`` (3xx: o MCP-CLARO nao segue); ``nao_mcp`` (nao respondeu como servidor MCP);
    ``sem_acesso`` (pediu chave); ``inalcancavel`` (rede, prazo, 408, 429, 5xx).
    """

    desfecho: Desfecho
    motivo: str
    versoes: tuple[str, ...] = ()
    discover_json: str | None = None
    codigo: int | None = None
    status_http: tuple[int, ...] = ()
    excecao: str | None = None
    pedidos: tuple[str, ...] = ()

    @property
    def aceito(self) -> bool:
        return self.desfecho == "aceito"

    @property
    def inalcancavel(self) -> bool:
        """Sem veredito de versao (desvio, nao e MCP, sem acesso ou inalcancavel): nao conta como versao antiga."""
        return self.desfecho not in ("aceito", "versao")


@dataclass(frozen=True, slots=True)
class Resposta:
    tipo: TipoResultado
    resultado: types.CallToolResult | types.InputRequiredResult | None = None
    erro: types.ErrorData | None = None
    status_http: tuple[int, ...] = ()
    excecao: str | None = None
    pedidos: tuple[str, ...] = ()


def pedidos_do_sistema(
    resultado: types.CallToolResult | types.InputRequiredResult | None,
    erro: types.ErrorData | None,
    barrados: Iterable[str],
) -> tuple[str, ...]:
    """O que o sistema pediu no meio da chamada, no nosso vocabulario: sem repeticao, em ordem, sem texto dele."""
    achados: set[str] = set()
    if isinstance(resultado, types.InputRequiredResult):
        for pedido in (resultado.input_requests or {}).values():
            if isinstance(pedido, types.ElicitRequest):
                url = isinstance(pedido.params, types.ElicitRequestURLParams)
                achados.add(PEDIDO_LINK if url else PEDIDO_FORMULARIO)
            elif isinstance(pedido, types.CreateMessageRequest):
                achados.add(PEDIDO_AMOSTRAGEM)
            elif isinstance(pedido, types.ListRootsRequest):
                achados.add(PEDIDO_RAIZES)
        if not resultado.input_requests:
            achados.add(PEDIDO_SO_ESTADO)
    if erro is not None and erro.code == types.URL_ELICITATION_REQUIRED:
        achados.add(PEDIDO_LINK_CODIGO_ANTIGO)
    for metodo in barrados:
        if metodo == NOTIFICACAO:
            continue
        achados.add(PREFIXO_FIO_ANTIGO + (metodo if metodo in METODOS_DO_SISTEMA else DESCONHECIDO))
    return tuple(sorted(achados))


def _discover(discover_json: str | None) -> types.DiscoverResult:
    if discover_json:
        return types.DiscoverResult.model_validate_json(discover_json)
    return types.DiscoverResult(supported_versions=[VERSAO_MCP], capabilities=types.ServerCapabilities())


def discover_para_guardar(resultado: types.DiscoverResult, *, terceiro: bool) -> str:
    """O ``server/discover`` como fica no cadastro (e como volta ao kit no ``adopt``).

    Sistema proprio: como veio (Fase 1). Num terceiro, so uma lista do que fica, sem texto livre do fornecedor:
    ``ttlMs``, ``cacheScope``, ``supportedVersions`` so com a 2026-07-28 (a unica que o MCP-CLARO fala; as
    anunciadas ficam em ``Sondagem.versoes``), as capacidades sem ``experimental`` nem ``extensions`` e
    ``resultType`` fixo. Saem ``instructions``, ``_meta`` (nome, titulo, descricao e site do fornecedor) e
    qualquer campo que o tipo nao conhece.
    """
    if not terceiro:
        return resultado.model_dump_json(by_alias=True, exclude_none=True)
    capacidades = resultado.capabilities.model_dump(
        by_alias=True, exclude_none=True, mode="json", exclude={"experimental", "extensions"}
    )
    limpo = types.DiscoverResult(
        ttl_ms=resultado.ttl_ms,
        cache_scope=resultado.cache_scope,
        supported_versions=[VERSAO_MCP],
        capabilities=types.ServerCapabilities.model_validate(capacidades),
        result_type="complete",
    )
    return limpo.model_dump_json(by_alias=True, exclude_none=True)


def _desfecho_por_status(status: int) -> tuple[Desfecho, str] | None:
    if 300 <= status < 400:
        return "desvio", MOTIVO_DESVIO.format(status=status)
    if status in (401, 403, 407):
        return "sem_acesso", MOTIVO_SEM_ACESSO.format(status=status)
    if status in (408, 429) or status >= 500:
        return "inalcancavel", MOTIVO_SEM_RESPOSTA.format(status=status)
    if status == 405:
        # Servidor MCP de transporte antigo (HTTP+SSE da 2024-11-05) costuma recusar o POST: versao antiga.
        return "versao", MOTIVO_TRANSPORTE_ANTIGO
    if status in (406, 415):
        return "nao_mcp", MOTIVO_NAO_MCP.format(status=status)
    return None


def _suportadas(erro: types.ErrorData) -> Any:
    return erro.data.get("supported") if isinstance(erro.data, dict) else None


def _sondagem_com_erro(tentativa: Tentativa, erro: MCPError) -> Sondagem:
    status_http = tuple(tentativa.status_http)
    pedidos = pedidos_do_sistema(None, None, tentativa.pedidos_barrados)
    comum: dict[str, Any] = {"codigo": erro.code, "status_http": status_http, "pedidos": pedidos}
    if tentativa.fase is not None or erro.code == types.REQUEST_TIMEOUT or not status_http:
        return Sondagem("inalcancavel", MOTIVO_INALCANCAVEL, excecao=tentativa.excecao, **comum)
    por_status = _desfecho_por_status(status_http[-1])
    if por_status is not None:
        return Sondagem(*por_status, **comum)
    if not (tentativa.corpos_jsonrpc and tentativa.corpos_jsonrpc[-1]):
        # 404 sem corpo JSON-RPC, HTML, JSON que nao e JSON-RPC, 202: nao e veredito de versao.
        return Sondagem("nao_mcp", MOTIVO_NAO_MCP.format(status=status_http[-1]), **comum)
    versoes = versoes_anunciadas(_suportadas(erro.error)) if erro.code == types.UNSUPPORTED_PROTOCOL_VERSION else ()
    return Sondagem("versao", MOTIVO_NAO_FALA, versoes=versoes, **comum)


def _sondagem_com_resultado(tentativa: Tentativa, bruto: dict[str, Any], *, terceiro: bool) -> Sondagem:
    status_http = tuple(tentativa.status_http)
    pedidos = pedidos_do_sistema(None, None, tentativa.pedidos_barrados)
    comum: dict[str, Any] = {"status_http": status_http, "pedidos": pedidos}
    try:
        resultado = types.DiscoverResult.model_validate(bruto)
    except ValidationError:
        return Sondagem("versao", MOTIVO_NAO_SEGUE, **comum)
    suportadas = list(resultado.supported_versions)
    versoes = versoes_anunciadas(suportadas)
    if VERSAO_MCP not in suportadas:
        return Sondagem("versao", MOTIVO_NAO_ANUNCIA, versoes=versoes, **comum)
    guardado = discover_para_guardar(resultado, terceiro=terceiro)
    return Sondagem("aceito", MOTIVO_ACEITO, versoes=versoes, discover_json=guardado, **comum)


async def sondar(
    endereco: str,
    segredo: Segredo,
    *,
    prazo: float = 10.0,
    terceiro: bool = False,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> Sondagem:
    """Pergunta ``server/discover`` na 2026-07-28 e separa o desfecho (ver ``Sondagem``). Nunca levanta.

    So ``aceito`` vem de um ``DiscoverResult`` valido com a versao. So ``versao`` e veredito de versao antiga:
    erro JSON-RPC do proprio servidor (400 do kit 1.x, -32022, -32601), 405, ou ``DiscoverResult`` sem a versao.
    """
    if terceiro:
        recusa = await _endereco_do_terceiro(endereco, ambiente, liberar_locais, prazo)
        if recusa is not None:
            return Sondagem("inalcancavel", MOTIVO_ENDERECO_RECUSADO, excecao=recusa)
    tentativa = Tentativa(teto_pedidos=1, espiar=True)
    marca = _TENTATIVA.set(tentativa)
    vazio = otel_context.attach(otel_context.Context())
    try:
        async with http_para_sistema(segredo, prazo_leitura=prazo, terceiro=terceiro) as http:
            with anyio.fail_after(prazo + 2):
                async with (
                    streamable_http_client(endereco, http_client=http, terminate_on_close=False) as (
                        leitura,
                        escrita,
                    ),
                    ClientSession(
                        leitura, escrita, client_info=_IDENTIDADE_TERCEIRO if terceiro else _IDENTIDADE
                    ) as sessao,
                ):
                    try:
                        bruto = await sessao.send_discover(VERSAO_MCP)
                    except MCPError as erro:
                        return _sondagem_com_erro(tentativa, erro)
                    return _sondagem_com_resultado(tentativa, bruto, terceiro=terceiro)
    except TimeoutError:
        return Sondagem("inalcancavel", MOTIVO_PRAZO, status_http=tuple(tentativa.status_http))
    except Exception as erro:  # noqa: BLE001 - a sondagem nunca derruba o cadastro: falha fora do protocolo
        status_http = tuple(tentativa.status_http)
        por_status = _desfecho_por_status(status_http[-1]) if status_http else None
        desfecho, motivo = por_status or ("inalcancavel", MOTIVO_INALCANCAVEL)
        return Sondagem(desfecho, motivo, status_http=status_http, excecao=tentativa.excecao or type(erro).__name__)
    finally:
        otel_context.detach(vazio)
        _TENTATIVA.reset(marca)


async def listar_ferramentas(
    endereco: str,
    segredo: Segredo,
    discover_json: str | None,
    *,
    terceiro: bool = False,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> list[dict[str, Any]]:
    """Lista o que o sistema declara hoje (para revisao e mudou-parou). Uma pagina por POST, ate 50.

    Num terceiro, endereco fora do combinado levanta ``EnderecoRecusado`` (nome que nao resolve, ``OSError``).
    """
    if terceiro:
        await conferir_endereco_terceiro(endereco, ambiente, liberar_locais=liberar_locais)
    ferramentas: list[dict[str, Any]] = []
    cursor: str | None = None
    tentativa = Tentativa(teto_pedidos=_PAGINAS)
    marca = _TENTATIVA.set(tentativa)
    vazio = otel_context.attach(otel_context.Context())
    try:
        async with (
            http_para_sistema(segredo, terceiro=terceiro) as http,
            streamable_http_client(endereco, http_client=http, terminate_on_close=False) as (
                leitura,
                escrita,
            ),
            ClientSession(leitura, escrita, client_info=_IDENTIDADE_TERCEIRO if terceiro else _IDENTIDADE) as sessao,
        ):
            sessao.adopt(_discover(discover_json))
            for _ in range(_PAGINAS):
                params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
                pagina = await sessao.send_request(types.ListToolsRequest(params=params), _RESULTADO_LISTA)
                ferramentas.extend(t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in pagina.tools)
                cursor = pagina.next_cursor
                if not cursor:
                    break
    finally:
        otel_context.detach(vazio)
        _TENTATIVA.reset(marca)
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
    terceiro: bool = False,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> Resposta:
    """Exatamente um POST ``tools/call``. Devolve o resultado cru ou a classificacao da falha.

    ``Resposta.pedidos`` diz o que o sistema pediu no meio (``InputRequiredResult``, -32042 ou pedido barrado no
    fio antigo). Num terceiro, ``traceparent`` e recusado e o endereco e conferido antes de abrir conexao.
    """
    if terceiro and traceparent:
        raise ValueError("rastro nunca vai a servidor de terceiros")
    if terceiro:
        recusa = await _endereco_do_terceiro(endereco, ambiente, liberar_locais, prazo)
        if recusa is not None:
            return Resposta("nao_enviado", excecao=recusa)
    tentativa = Tentativa(teto_pedidos=1)
    marca = _TENTATIVA.set(tentativa)
    vazio = otel_context.attach(otel_context.Context())
    meta = {"traceparent": traceparent} if traceparent else None
    # Cabecalhos Mcp-Param-* que o sistema exige quando o esquema dele marca x-mcp-header.
    cabecalhos = mcp_param_headers(x_mcp_header_map(esquema_declarado), argumentos) if esquema_declarado else {}
    try:
        async with (
            http_para_sistema(
                segredo, prazo_leitura=prazo + 5, teto_resposta=teto_resposta, cabecalhos=cabecalhos, terceiro=terceiro
            ) as http,
            streamable_http_client(endereco, http_client=http, terminate_on_close=False) as (
                leitura,
                escrita,
            ),
            ClientSession(leitura, escrita, client_info=_IDENTIDADE_TERCEIRO if terceiro else _IDENTIDADE) as sessao,
        ):
            sessao.adopt(_discover(discover_json))
            pedido = types.CallToolRequest(
                params=types.CallToolRequestParams(name=nome, arguments=argumentos, _meta=meta)
            )
            try:
                resultado = await sessao.send_request(pedido, _RESULTADO_CHAMADA, request_read_timeout_seconds=prazo)
            except MCPError as erro:
                return _resposta_com_erro(tentativa, erro)
            pedidos = pedidos_do_sistema(resultado, None, tentativa.pedidos_barrados)
            status = tuple(tentativa.status_http)
            if tentativa.pedidos_a_mais:
                # Um segundo pedido nesta chamada (volta nova do kit, desvio seguido) nao saiu: sem certeza.
                return Resposta("duvida", status_http=status, excecao=PEDIDO_A_MAIS, pedidos=pedidos)
            return Resposta("resultado", resultado=resultado, status_http=status, pedidos=pedidos)
    except Exception as erro:  # noqa: BLE001 - toda falha fora do protocolo vira classificacao, nunca reenvio
        status = tuple(tentativa.status_http)
        pedidos = pedidos_do_sistema(None, None, tentativa.pedidos_barrados)
        if status and 300 <= status[-1] < 400:
            return Resposta("recusado_http", status_http=status, excecao=type(erro).__name__, pedidos=pedidos)
        return Resposta(
            tentativa.fase or "duvida",
            status_http=status,
            excecao=tentativa.excecao or type(erro).__name__,
            pedidos=pedidos,
        )
    finally:
        otel_context.detach(vazio)
        _TENTATIVA.reset(marca)


def recusado_por_status(status: int) -> bool:
    """O status com que um sistema recusa a chamada sem executar (chave recusada ou desvio nao seguido)."""
    return status in (401, 403) or 300 <= status < 400


def _resposta_com_erro(tentativa: Tentativa, erro: MCPError) -> Resposta:
    status = tuple(tentativa.status_http)
    pedidos = pedidos_do_sistema(None, erro.error, tentativa.pedidos_barrados)
    if tentativa.fase is not None:
        return Resposta(tentativa.fase, erro=erro.error, status_http=status, excecao=tentativa.excecao, pedidos=pedidos)
    if erro.code in (types.REQUEST_TIMEOUT, -32000):
        return Resposta("duvida", erro=erro.error, status_http=status, pedidos=pedidos)
    if status and (status[-1] >= 500 or 200 <= status[-1] < 300):
        # 5xx, 202, corpo invalido ou erro dentro de um 2xx: o sistema pode ter executado.
        return Resposta("duvida", erro=erro.error, status_http=status, pedidos=pedidos)
    if status and recusado_por_status(status[-1]):
        return Resposta("recusado_http", erro=erro.error, status_http=status, pedidos=pedidos)
    return Resposta("erro_rpc", erro=erro.error, status_http=status, pedidos=pedidos)
