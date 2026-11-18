"""Verificador de cracha (JWT do provedor de login) e filtro ASGI que o exige.

O kit nao valida JWT: assinatura, emissor, audiencia, validade e algoritmos
ficam aqui. A audiencia precisa ser o recurso do MCP-CLARO
(``PUBLIC_BASE_URL/mcp``): cracha emitido para outro servico ("outro
condominio") e recusado. A identidade do aplicativo vem do cracha, nunca do
endereco de origem.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2
import jwt
from jwt import PyJWK, PyJWKSet
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_claro.config.settings import Parametros
from mcp_claro.logs import logger

log = logger("mcp_claro.entrada.cracha")

FOLGA_RELOGIO_SEGUNDOS: Final = 30
ALGORITMOS_PROIBIDOS: Final = frozenset({"none", "HS256", "HS384", "HS512"})


@dataclass(frozen=True, slots=True)
class Cracha:
    """O que o MCP-CLARO aceita saber de um cracha valido. Nunca guarda o token cru."""

    client_id: str
    sujeito: str
    emissor: str
    escopos: frozenset[str]
    expira_em: int
    emitido_em: int | None = None
    jti: str | None = None
    nome: str | None = None
    reivindicacoes: dict[str, Any] = field(default_factory=dict, repr=False)


class CrachaRecusado(Exception):
    """Cracha invalido. O motivo vai ao extrato e a ronda; a resposta ao aplicativo e generica."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo
        super().__init__(motivo)


class ProvedorIndisponivel(Exception):
    """O provedor de login nao respondeu e nao ha chaves guardadas validas."""


cracha_atual: contextvars.ContextVar[Cracha | None] = contextvars.ContextVar("cracha_atual", default=None)


class ChavesPublicas:
    """Lista JWKS do provedor, guardada por ``IDP_JWKS_CACHE_SECONDS``; busca de novo se aparecer kid novo."""

    def __init__(self, url: str, *, cache_segundos: int, http: httpx2.AsyncClient) -> None:
        self.url = url
        self.cache_segundos = cache_segundos
        self.http = http
        self._chaves: dict[str, PyJWK] = {}
        self._buscado_em = 0.0
        self._trava = asyncio.Lock()
        self._ultima_busca_forcada = 0.0
        self._ultima_falha = -1e9

    async def _buscar(self) -> None:
        resposta = await self.http.get(self.url)
        resposta.raise_for_status()
        conjunto = PyJWKSet.from_dict(resposta.json())
        self._chaves = {k.key_id: k for k in conjunto.keys if k.key_id}
        self._buscado_em = time.monotonic()

    async def chave(self, kid: str) -> PyJWK:
        async with self._trava:
            agora = time.monotonic()
            vencido = agora - self._buscado_em > self.cache_segundos
            kid_novo = kid not in self._chaves and agora - self._ultima_busca_forcada > 30
            espera_ok = agora - self._ultima_falha > 5
            if (vencido or not self._chaves or kid_novo) and espera_ok:
                if kid_novo:
                    # Rodizio de chaves no provedor: busca de novo, no maximo a cada 30 s.
                    self._ultima_busca_forcada = agora
                try:
                    await self._buscar()
                except (httpx2.HTTPError, ValueError, jwt.PyJWKSetError) as erro:
                    self._ultima_falha = agora
                    # Provedor fora do ar: segue com as chaves guardadas por ate 24 h (stale-if-error).
                    if not self._chaves or agora - self._buscado_em > 86400:
                        raise ProvedorIndisponivel("lista de chaves do provedor indisponivel") from erro
                    log.warning("jwks_desatualizada_em_uso")
        try:
            return self._chaves[kid]
        except KeyError as erro:
            raise CrachaRecusado("chave de assinatura desconhecida") from erro


class VerificadorCracha:
    def __init__(self, p: Parametros, chaves: ChavesPublicas) -> None:
        self.emissor = p.IDP_ISSUER_URL
        self.audiencia = p.recurso
        self.algoritmos = [a for a in p.IDP_ALGORITHMS if a not in ALGORITMOS_PROIBIDOS]
        self.chaves = chaves

    async def verificar(self, token: str) -> Cracha:
        try:
            cabecalho = jwt.get_unverified_header(token)
        except jwt.PyJWTError as erro:
            raise CrachaRecusado("cracha mal formado") from erro
        alg = cabecalho.get("alg")
        if alg not in self.algoritmos:
            raise CrachaRecusado("algoritmo nao aceito")
        kid = cabecalho.get("kid")
        if not isinstance(kid, str) or not kid:
            raise CrachaRecusado("cracha sem identificador de chave")
        chave = await self.chaves.chave(kid)
        if chave.algorithm_name and chave.algorithm_name != alg:
            raise CrachaRecusado("algoritmo nao bate com a chave")
        try:
            dados: dict[str, Any] = jwt.decode(
                token,
                key=chave,
                algorithms=[alg],
                audience=self.audiencia,
                issuer=self.emissor,
                leeway=FOLGA_RELOGIO_SEGUNDOS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"], "strict_aud": False},
            )
        except jwt.ExpiredSignatureError as erro:
            raise CrachaRecusado("cracha vencido") from erro
        except jwt.InvalidAudienceError as erro:
            raise CrachaRecusado("cracha de outro destino (audiencia)") from erro
        except jwt.InvalidIssuerError as erro:
            raise CrachaRecusado("cracha de outro emissor") from erro
        except jwt.ImmatureSignatureError as erro:
            raise CrachaRecusado("cracha ainda nao vale") from erro
        except jwt.PyJWTError as erro:
            raise CrachaRecusado("cracha invalido") from erro
        client_id = dados.get("azp") or dados.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise CrachaRecusado("cracha sem aplicativo (azp ou client_id)")
        escopos = dados.get("scope", "")
        return Cracha(
            client_id=client_id,
            sujeito=str(dados["sub"]),
            emissor=str(dados["iss"]),
            escopos=frozenset(escopos.split()) if isinstance(escopos, str) else frozenset(),
            expira_em=int(dados["exp"]),
            emitido_em=int(dados["iat"]) if "iat" in dados else None,
            jti=dados.get("jti"),
            nome=dados.get("name") or dados.get("preferred_username"),
            reivindicacoes={k: v for k, v in dados.items() if k in {"acr", "amr", "auth_time", "sid"}},
        )


def desafio_401(p: Parametros, erro: str = "invalid_token", descricao: str = "Authentication required") -> JSONResponse:
    """Resposta 401 com ``WWW-Authenticate`` e o endereco dos metadados do recurso (RFC 9728)."""
    metadados = f"{p.PUBLIC_BASE_URL}/.well-known/oauth-protected-resource/mcp"
    partes = [f'error="{erro}"', f'error_description="{descricao}"', f'resource_metadata="{metadados}"']
    if p.MCP_SCOPES:
        partes.append(f'scope="{" ".join(p.MCP_SCOPES)}"')
    return JSONResponse(
        {"error": erro, "error_description": descricao},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer " + ", ".join(partes), "Cache-Control": "no-store"},
    )


class FiltroCracha:
    """Exige cracha valido em todo pedido ao endereco MCP. Grava o cracha em ``scope['state']``."""

    def __init__(self, app: ASGIApp, verificador: VerificadorCracha, p: Parametros) -> None:
        self.app = app
        self.verificador = verificador
        self.p = p

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        autorizacoes = [v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"]
        if len(autorizacoes) != 1 or not autorizacoes[0].lower().startswith("bearer "):
            await desafio_401(self.p)(scope, receive, send)
            return
        token = autorizacoes[0][7:].strip()
        try:
            cracha = await self.verificador.verificar(token)
        except CrachaRecusado as erro:
            log.info("cracha_recusado", motivo=erro.motivo)
            from mcp_claro.medicoes.contadores import CONTADORES

            CONTADORES.somar("crachas_recusados", motivo=erro.motivo)
            await desafio_401(self.p)(scope, receive, send)
            return
        except ProvedorIndisponivel:
            log.warning("jwks_indisponivel")
            await JSONResponse(
                {"error": "temporarily_unavailable", "error_description": "Provedor de login indisponivel"},
                status_code=503,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
            return
        if self.p.MCP_SCOPES and not set(self.p.MCP_SCOPES) <= cracha.escopos:
            escopos = " ".join(self.p.MCP_SCOPES)
            await JSONResponse(
                {"error": "insufficient_scope", "error_description": "Cracha sem o escopo do MCP-CLARO"},
                status_code=403,
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{escopos}"'},
            )(scope, receive, send)
            return
        scope.setdefault("state", {})["cracha"] = cracha
        marca = cracha_atual.set(cracha)
        try:
            await self.app(scope, receive, send)
        finally:
            cracha_atual.reset(marca)
