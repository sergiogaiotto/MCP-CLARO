"""Partes comuns a todas as telas: sessao, CSRF, desenho das paginas e linhas do extrato."""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx2
from jinja2 import Environment
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import FOLGA_RELOGIO_SEGUNDOS
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.login import ProvedorLogin
from mcp_claro.telas.paginas import AVISOS
from mcp_claro.telas.seguranca import (
    CABECALHO_CSRF,
    CAMPO_CSRF,
    COOKIE_SESSAO,
    Formulario,
    FormularioRecusado,
    caminho_seguro,
    iguais,
    ler_formulario,
    origem_aceita,
)
from mcp_claro.telas.sessoes import Sessao, sessao_valida

log = logger("mcp_claro.telas")

CAMINHO_REFAZER: Final = "/telas/login/refazer"
MARCA_REFEITO: Final = "refeito"


def voltar_refeito(voltar: str) -> str:
    """O caminho de volta do login refeito, com a marca ``refeito=1``: a tela sabe que o login acabou de ser
    pedido de novo (e avisa se o provedor nao pediu a senha). So caminho local das telas."""
    seguro = caminho_seguro(voltar)
    partes = urlsplit(seguro)
    pares = [(k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True) if k != MARCA_REFEITO]
    pares.append((MARCA_REFEITO, "1"))
    return caminho_seguro(partes.path + "?" + urlencode(pares))


def url_refazer(voltar: str) -> str:
    return CAMINHO_REFAZER + "?" + urlencode({"voltar": caminho_seguro(voltar)})


@dataclass(frozen=True, slots=True)
class LoginRefeito:
    """O que a pagina mostra para pedir o login refeito na hora (``modelos/_login_refeito.html``)."""

    recente: bool
    url: str
    falhou: bool
    minutos: int


class PortaInterna(Protocol):
    """O que as telas usam do despachante: um POST na porta interna (``ClienteDespachante.post``)."""

    async def post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]: ...


class Interrompe(Exception):
    """Para a tela e devolve esta resposta (login exigido, area do ADMIN, CSRF...)."""

    def __init__(self, resposta: Response) -> None:
        self.resposta = resposta
        super().__init__("resposta pronta")


@dataclass(frozen=True, slots=True)
class RespostaInterna:
    """O que a porta interna do despachante respondeu. ``status`` 0: nao respondeu."""

    status: int
    dados: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def motivo(self) -> str:
        valor = self.dados.get("motivo")
        return valor if isinstance(valor, str) else ""

    @property
    def mensagem(self) -> str:
        valor = self.dados.get("mensagem")
        return valor if isinstance(valor, str) else ""


class Telas:
    def __init__(
        self,
        p: Parametros,
        cfg: ConfigTelas,
        engine: AsyncEngine,
        despachante: PortaInterna,
        provedor: ProvedorLogin,
        ambiente: Environment,
    ) -> None:
        self.p = p
        self.cfg = cfg
        self.engine = engine
        self.despachante = despachante
        self.provedor = provedor
        self.ambiente = ambiente

    # --- Paginas ---

    def pagina(
        self, pedido: Request, modelo: str, contexto: dict[str, Any] | None = None, *, status: int = 200
    ) -> HTMLResponse:
        sessao: Sessao | None = getattr(pedido.state, "sessao", None)
        aviso = AVISOS.get(pedido.query_params.get("aviso", ""))
        base: dict[str, Any] = {
            "sessao": sessao,
            "csrf": sessao.csrf if sessao else "",
            "aviso": aviso,
            "caminho": pedido.url.path,
            "fase1_ligada": self.p.FEATURE_PHASE1_READ,
            "erros": [],
            "valores": {},
        }
        base.update(contexto or {})
        corpo = self.ambiente.get_template(modelo).render(base)
        return HTMLResponse(corpo, status_code=status)

    def data(self, valor: Any) -> str:
        """Data no fuso das telas, como aparece nas paginas (dd/mm/aaaa)."""
        return str(self.ambiente.filters["data"](valor))

    def erro(self, pedido: Request, status: int, titulo: str, mensagem: str) -> HTMLResponse:
        return self.pagina(pedido, "erro.html", {"titulo": titulo, "mensagem": mensagem}, status=status)

    # --- Porta interna do despachante ---

    async def porta_interna(self, caminho: str, corpo: dict[str, Any]) -> RespostaInterna:
        """Um POST na porta interna. Recusa e falta de resposta voltam como ``RespostaInterna``, sem excecao.

        O log leva so o caminho, o status e o codigo do motivo: nunca o corpo, que pode ter chave ou codigo.
        """
        try:
            dados = await self.despachante.post(caminho, corpo)
        except httpx2.HTTPStatusError as erro:
            try:
                devolvido = erro.response.json()
            except ValueError:
                devolvido = {}
            resposta = RespostaInterna(erro.response.status_code, devolvido if isinstance(devolvido, dict) else {})
            log.info(
                "telas_porta_interna_recusou", caminho=caminho, status=resposta.status, motivo=resposta.motivo[:60]
            )
            return resposta
        except (httpx2.HTTPError, ValueError) as erro:
            log.warning("telas_porta_interna_sem_resposta", caminho=caminho, erro=type(erro).__name__)
            return RespostaInterna(0)
        return RespostaInterna(200, dados if isinstance(dados, dict) else {})

    # --- Sessao ---

    async def sessao(self, pedido: Request) -> Sessao | None:
        if hasattr(pedido.state, "sessao"):
            atual: Sessao | None = pedido.state.sessao
            return atual
        identificador = pedido.cookies.get(COOKIE_SESSAO)
        sessao: Sessao | None = None
        if identificador:
            async with self.engine.begin() as c:
                sessao = await sessao_valida(c, identificador, parada_minutos=self.cfg.parada_minutos)
        pedido.state.sessao = sessao
        return sessao

    async def exigir_sessao(self, pedido: Request) -> Sessao:
        sessao = await self.sessao(pedido)
        if sessao is not None:
            return sessao
        if pedido.method in ("GET", "HEAD"):
            voltar = pedido.url.path + (f"?{pedido.url.query}" if pedido.url.query else "")
            destino = "/telas/login?" + urlencode({"voltar": voltar})
            if pedido.headers.get("hx-request") == "true":
                # Navegacao pelo htmx: o login sai para o provedor, entao a pagina inteira precisa ir.
                raise Interrompe(Response(status_code=200, headers={"HX-Redirect": destino}))
            raise Interrompe(RedirectResponse(destino, status_code=303))
        raise Interrompe(self.erro(pedido, 403, "Sua sessão terminou", "Entre de novo para continuar. Nada foi feito."))

    def login_recente(self, sessao: Sessao) -> bool:
        """O login desta sessao foi feito, ou refeito, ha no maximo ``login_recente_segundos``.

        Vale o ``auth_time`` que o provedor assinou no id_token (a hora em que a pessoa digitou a senha), nao a
        idade da sessao. Sem ``auth_time``, nunca e recente."""
        if sessao.auth_time is None:
            return False
        idade = (datetime.now(UTC) - sessao.auth_time).total_seconds()
        return -FOLGA_RELOGIO_SEGUNDOS <= idade <= self.cfg.login_recente_segundos

    def login_refeito(self, pedido: Request, sessao: Sessao, voltar: str) -> LoginRefeito:
        """Para a pagina: se o login vale como refeito agora e, se nao, o link que o refaz e volta a ``voltar``."""
        recente = self.login_recente(sessao)
        return LoginRefeito(
            recente=recente,
            url=url_refazer(voltar),
            falhou=not recente and pedido.query_params.get(MARCA_REFEITO) == "1",
            minutos=max(1, self.cfg.login_recente_segundos // 60),
        )

    async def exigir_admin(self, pedido: Request) -> Sessao:
        sessao = await self.exigir_sessao(pedido)
        if not sessao.admin:
            log.info("telas_area_admin_recusada")
            raise Interrompe(self.erro(pedido, 403, "Área do ADMIN", "Esta área é só do ADMIN. Volte para o início."))
        return sessao

    async def formulario(self, pedido: Request, sessao: Sessao) -> Formulario:
        """Le o formulario de um POST e confere origem e token CSRF da sessao, em tempo constante."""
        if not origem_aceita(pedido, self.p.PUBLIC_BASE_URL):
            log.info("telas_csrf_recusado", motivo="origem")
            raise Interrompe(self._recusa_csrf(pedido))
        try:
            formulario = await ler_formulario(pedido)
        except FormularioRecusado as erro:
            if erro.status != 415:
                raise Interrompe(self.erro(pedido, erro.status, "Pedido recusado", erro.mensagem)) from erro
            formulario = Formulario([])  # sem formulario: so o cabecalho pode trazer o token
        enviado = formulario.texto(CAMPO_CSRF) or pedido.headers.get(CABECALHO_CSRF, "")
        if not iguais(enviado, sessao.csrf):
            log.info("telas_csrf_recusado", motivo="token")
            raise Interrompe(self._recusa_csrf(pedido))
        return formulario

    def _recusa_csrf(self, pedido: Request) -> HTMLResponse:
        return self.erro(
            pedido,
            403,
            "Pedido recusado",
            "Este pedido não veio desta tela. Por segurança, nada foi feito. Volte e tente de novo.",
        )

    # --- Extrato ---

    async def extrato(
        self,
        c: AsyncConnection,
        tipo: str,
        campos: dict[str, object],
        *,
        pessoa_id: uuid.UUID | None,
        aplicativo_id: uuid.UUID | None = None,
        sistema_id: uuid.UUID | None = None,
        ferramenta: str | None = None,
        dados: dict[str, Any] | None = None,
    ) -> None:
        await registrar(
            c,
            papel="entrada",
            tipo=tipo,
            campos=campos,
            pessoa_id=pessoa_id,
            aplicativo_id=aplicativo_id,
            sistema_id=sistema_id,
            ferramenta=ferramenta,
            dados=dados,
        )


def tela[**P](funcao: Callable[P, Awaitable[Response]]) -> Callable[P, Awaitable[Response]]:
    """Transforma ``Interrompe`` na resposta pronta (serve para funcao e para metodo)."""

    @functools.wraps(funcao)
    async def envolta(*args: P.args, **kwargs: P.kwargs) -> Response:
        try:
            return await funcao(*args, **kwargs)
        except Interrompe as parada:
            return parada.resposta

    return envolta


def ver_depois(caminho: str, aviso: str | None = None) -> RedirectResponse:
    """Depois de um POST que deu certo, volta por GET (sem reenvio ao recarregar)."""
    return RedirectResponse(caminho + (f"?aviso={aviso}" if aviso else ""), status_code=303)


def uuid_ou_none(valor: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(valor)
    except ValueError:
        return None


def numero_pagina(valor: str | None) -> int:
    try:
        numero = int(valor or "1")
    except ValueError:
        return 1
    return min(max(numero, 1), 1000)
