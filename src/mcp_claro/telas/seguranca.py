"""Protecoes comuns das telas: cabecalhos de seguranca, cookies, CSRF e leitura de formularios.

- CSP restrita: so scripts e estilos do proprio gateway (htmx servido por nos, sem CDN).
- Nada de moldura (X-Frame-Options DENY e ``frame-ancestors 'none'``) nem de ``Referer``.
- Cookies ``__Host-``: Secure, HttpOnly, SameSite=Lax, ``Path=/`` e sem ``Domain``.
- Todo POST leva o token CSRF da sessao, conferido em tempo constante, e nao pode vir de outra origem.
- O formulario tem teto de tamanho e de campos (``TETO_PADRAO``). Uma rota que precisa de mais declara o proprio
  teto (``rota_com_teto``), sem mudar o das outras; nela, ate ``FOLGA_DA_ROTA`` vezes o tamanho, quem recusa e o
  leitor, com a frase em portugues.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, unquote, urlsplit

from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE_SESSAO: Final = "__Host-mcp_claro_sessao"
COOKIE_LOGIN: Final = "__Host-mcp_claro_login"
CAMPO_CSRF: Final = "csrf"
CABECALHO_CSRF: Final = "x-csrf-token"
MAXIMO_FORMULARIO: Final = 64 * 1024
"""Teto, em bytes, do corpo de um formulario (o mesmo da montagem das telas, ``rotas.rotas_telas``)."""
MAXIMO_CAMPOS: Final = 200
"""Teto de campos de um formulario."""

# Recusas do leitor de formularios (a tela mostra com o titulo "Pedido recusado").
FORMATO_INESPERADO: Final = "Formulário em formato inesperado."
GRANDE_DEMAIS: Final = "Formulário grande demais. Nada foi feito."
ILEGIVEL: Final = "Formulário ilegível. Nada foi feito."
CAMPOS_DEMAIS: Final = "Formulário com campos demais. Nada foi feito."
CHAVE_TETO: Final = "mcp_claro.teto_formulario"
"""Onde ``ComTeto`` poe, no pedido, o teto da rota."""


@dataclass(frozen=True, slots=True)
class TetoFormulario:
    """Quanto um formulario pode trazer: ``tamanho`` em bytes e ``campos``. ``campos_demais`` e a frase da recusa
    quando passa dos campos (uma rota pode dizer o que fazer)."""

    tamanho: int
    campos: int
    campos_demais: str = CAMPOS_DEMAIS


TETO_PADRAO: Final = TetoFormulario(MAXIMO_FORMULARIO, MAXIMO_CAMPOS)
"""O teto de toda rota das telas que nao declara outro."""
FOLGA_DA_ROTA: Final = 2
"""Numa rota com teto proprio (``rota_com_teto``), o ``max_body_size`` do Starlette e ``FOLGA_DA_ROTA`` vezes o
tamanho do teto. Ate ali, quem recusa e ``ler_formulario``, com a frase da tela em portugues. Acima, o Starlette
recusa antes de ler, com o 413 dele ("Content Too Large", em ingles). A folga nao aumenta o que fica na memoria: o
leitor para de ler logo que passa do tamanho do teto."""

CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class CabecalhosSeguranca:
    """Filtro ASGI que poe os cabecalhos de seguranca em toda resposta das telas."""

    def __init__(self, app: ASGIApp, *, hsts: bool = True) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        estatico = str(scope.get("path", "")).startswith("/telas/estaticos/")

        async def enviar(mensagem: Message) -> None:
            if mensagem["type"] == "http.response.start":
                h = MutableHeaders(scope=mensagem)
                h["Content-Security-Policy"] = CSP
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "no-referrer"
                h["X-Content-Type-Options"] = "nosniff"
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                h["Cross-Origin-Resource-Policy"] = "same-origin"
                h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
                if self.hsts:
                    h["Strict-Transport-Security"] = "max-age=31536000"
                h["Cache-Control"] = "no-cache" if estatico else "no-store"
            await send(mensagem)

        await self.app(scope, receive, enviar)


def por_cookie(resposta: Response, nome: str, valor: str, *, max_age: int) -> None:
    resposta.set_cookie(nome, valor, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def apagar_cookie(resposta: Response, nome: str) -> None:
    resposta.delete_cookie(nome, path="/", secure=True, httponly=True, samesite="lax")


def iguais(a: str | None, b: str | None) -> bool:
    """Comparacao em tempo constante; vazio nunca confere."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Formulario:
    """Campos de um formulario ``application/x-www-form-urlencoded``, com varios valores por nome."""

    def __init__(self, pares: list[tuple[str, str]]) -> None:
        self._campos: dict[str, list[str]] = {}
        for nome, valor in pares:
            self._campos.setdefault(nome, []).append(valor)

    def texto(self, nome: str, padrao: str = "") -> str:
        valores = self._campos.get(nome)
        return valores[0].strip() if valores else padrao

    def lista(self, nome: str) -> list[str]:
        return [v.strip() for v in self._campos.get(nome, []) if v.strip()]

    def __contains__(self, nome: object) -> bool:
        return nome in self._campos

    def como_dict(self, sem: tuple[str, ...] = (CAMPO_CSRF,)) -> Mapping[str, str]:
        """Primeiro valor de cada campo, para devolver ao formulario quando ha erro."""
        return {k: v[0] for k, v in self._campos.items() if k not in sem and v}


class FormularioRecusado(Exception):
    def __init__(self, status: int, mensagem: str) -> None:
        self.status = status
        self.mensagem = mensagem
        super().__init__(mensagem)


class ComTeto:
    """Filtro ASGI de uma rota com teto de formulario proprio: poe o teto no pedido, e ``ler_formulario`` o usa."""

    def __init__(self, app: ASGIApp, *, teto: TetoFormulario) -> None:
        self.app = app
        self.teto = teto

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope[CHAVE_TETO] = self.teto
        await self.app(scope, receive, send)


def rota_com_teto(caminho: str, funcao: Callable[[Request], Awaitable[Response]], teto: TetoFormulario) -> Route:
    """Um POST com teto de formulario proprio. O ``max_body_size`` da rota vale no lugar do da montagem das telas
    so para ela (o Starlette usa o da rota mais de dentro); as outras rotas ficam com ``TETO_PADRAO``. Ele tem folga
    (``FOLGA_DA_ROTA``) sobre o tamanho do teto, para o leitor recusar antes do Starlette, em portugues."""
    return Route(
        caminho,
        funcao,
        methods=["POST"],
        middleware=[Middleware(ComTeto, teto=teto)],
        max_body_size=FOLGA_DA_ROTA * teto.tamanho,
    )


def teto_do_pedido(pedido: Request) -> TetoFormulario:
    teto = pedido.scope.get(CHAVE_TETO)
    return teto if isinstance(teto, TetoFormulario) else TETO_PADRAO


def _campos(corpo: bytes | bytearray) -> int:
    """A mesma conta do ``parse_qsl``: um campo a mais que os "&"."""
    return corpo.count(b"&") + 1


async def ler_formulario(pedido: Request) -> Formulario:
    """O formulario do pedido, dentro do teto da rota (``teto_do_pedido``). Recusa com ``FormularioRecusado``.

    Passar dos campos tem frase propria (a da rota diz o que fazer), tambem quando o corpo passa do tamanho e o que
    ja chegou dele passa dos campos."""
    teto = teto_do_pedido(pedido)
    tipo = pedido.headers.get("content-type", "").split(";")[0].strip().lower()
    if tipo != "application/x-www-form-urlencoded":
        raise FormularioRecusado(415, FORMATO_INESPERADO)
    corpo = bytearray()
    async for pedaco in pedido.stream():
        corpo.extend(pedaco)
        if len(corpo) > teto.tamanho:
            if _campos(corpo) > teto.campos:
                raise FormularioRecusado(400, teto.campos_demais)
            raise FormularioRecusado(413, GRANDE_DEMAIS)
    if _campos(corpo) > teto.campos:
        raise FormularioRecusado(400, teto.campos_demais)
    try:
        pares = parse_qsl(
            corpo.decode("utf-8"), keep_blank_values=True, strict_parsing=False, max_num_fields=teto.campos
        )
    except (UnicodeDecodeError, ValueError) as erro:
        raise FormularioRecusado(400, ILEGIVEL) from erro
    return Formulario(pares)


def origem_aceita(pedido: Request, base_publica: str) -> bool:
    """Recusa POST vindo de outra origem, quando o navegador diz de onde veio."""
    if pedido.headers.get("sec-fetch-site", "") == "cross-site":
        return False
    origem = pedido.headers.get("origin")
    if origem is None:
        return True
    esperada = urlsplit(base_publica)
    vinda = urlsplit(origem)
    return (vinda.scheme, vinda.netloc) == (esperada.scheme, esperada.netloc)


def caminho_seguro(caminho: str | None, padrao: str = "/telas") -> str:
    """So aceita voltar para um caminho das proprias telas (sem redirecionamento aberto)."""
    if not caminho or len(caminho) > 300:
        return padrao
    partes = urlsplit(caminho)
    if partes.scheme or partes.netloc or not (partes.path == "/telas" or partes.path.startswith("/telas/")):
        return padrao
    if "\\" in caminho or "//" in caminho or any(ord(c) < 0x21 for c in caminho):
        return padrao
    segmentos = unquote(partes.path).split("/")
    if ".." in segmentos or "." in segmentos:
        return padrao
    if caminho.startswith("/telas/login"):
        return padrao
    return caminho
