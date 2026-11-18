"""Login das pessoas nas telas: OIDC com codigo de autorizacao, PKCE S256 e nonce.

O ``id_token`` e conferido com o mesmo rigor do cracha (``entrada/cracha.py``): assinatura pela
lista JWKS do provedor (a mesma ``ChavesPublicas``), algoritmos aceitos, emissor, audiencia (o
cliente das telas), validade e, alem disso, ``nonce``, ``azp`` e frescor. O ``access_token`` que o
provedor devolve nao e usado nem guardado.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlencode

import httpx2
import jwt

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import ALGORITMOS_PROIBIDOS, FOLGA_RELOGIO_SEGUNDOS, ChavesPublicas, CrachaRecusado
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.seguranca import iguais
from mcp_claro.telas.sessoes import LoginPendente, desafio_pkce

IDADE_MAXIMA_ID_TOKEN: Final = 600
DESCOBERTA_SEGUNDOS: Final = 3600


class LoginRecusado(Exception):
    """O login nao fecha. O motivo vai ao log (sem conteudo); a pessoa ve uma mensagem simples."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo
        super().__init__(motivo)


@dataclass(frozen=True, slots=True)
class Identidade:
    emissor: str
    sujeito: str
    nome: str | None
    auth_time: int | None


class ProvedorLogin:
    def __init__(self, p: Parametros, cfg: ConfigTelas, http: httpx2.AsyncClient, chaves: ChavesPublicas) -> None:
        self.p = p
        self.cfg = cfg
        self.http = http
        self.chaves = chaves
        self.emissor = p.IDP_ISSUER_URL
        self.algoritmos = [a for a in p.IDP_ALGORITHMS if a not in ALGORITMOS_PROIBIDOS]
        self._enderecos: tuple[str, str] | None = None
        self._descoberto_em = 0.0

    async def enderecos(self) -> tuple[str, str]:
        """(login no navegador, troca do codigo). Dos parametros, do tipo de provedor ou da descoberta."""
        autorizacao, troca = self.cfg.enderecos_por_tipo(self.p)
        if autorizacao and troca:
            return autorizacao, troca
        if self._enderecos is None or time.monotonic() - self._descoberto_em > DESCOBERTA_SEGUNDOS:
            resposta = await self.http.get(f"{self.emissor}/.well-known/openid-configuration")
            resposta.raise_for_status()
            doc = resposta.json()
            if doc.get("issuer") != self.emissor:
                raise LoginRecusado("descoberta de outro emissor")
            a, t = doc.get("authorization_endpoint"), doc.get("token_endpoint")
            if not isinstance(a, str) or not isinstance(t, str):
                raise LoginRecusado("descoberta sem enderecos de login")
            self._enderecos = (a, t)
            self._descoberto_em = time.monotonic()
        return autorizacao or self._enderecos[0], troca or self._enderecos[1]

    async def url_login(self, login: LoginPendente) -> str:
        autorizacao, _ = await self.enderecos()
        parametros = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_uri(self.p),
            "scope": " ".join(self.cfg.escopos),
            "state": login.estado,
            "nonce": login.nonce,
            "code_challenge": desafio_pkce(login.verificador),
            "code_challenge_method": "S256",
        }
        separador = "&" if "?" in autorizacao else "?"
        return autorizacao + separador + urlencode(parametros)

    async def trocar_codigo(self, codigo: str, login: LoginPendente) -> Identidade:
        _, troca = await self.enderecos()
        corpo = {
            "grant_type": "authorization_code",
            "code": codigo,
            "redirect_uri": self.cfg.redirect_uri(self.p),
            "code_verifier": login.verificador,
        }
        cabecalhos = {"Accept": "application/json"}
        if self.cfg.segredo is not None:
            # Cliente confidencial: client_secret_basic (RFC 6749, 2.3.1), com nome e segredo codificados.
            credencial = f"{quote(self.cfg.client_id, safe='')}:{quote(self.cfg.segredo.get_secret_value(), safe='')}"
            cabecalhos["Authorization"] = "Basic " + base64.b64encode(credencial.encode("utf-8")).decode("ascii")
        else:
            corpo["client_id"] = self.cfg.client_id
        resposta = await self.http.post(troca, data=corpo, headers=cabecalhos)
        if resposta.status_code != 200:
            raise LoginRecusado(f"troca do codigo recusada ({resposta.status_code})")
        try:
            dados = resposta.json()
        except ValueError as erro:
            raise LoginRecusado("troca do codigo sem JSON") from erro
        id_token = dados.get("id_token") if isinstance(dados, dict) else None
        if not isinstance(id_token, str) or not id_token:
            raise LoginRecusado("troca do codigo sem id_token")
        return await self.conferir_id_token(id_token, login.nonce)

    async def conferir_id_token(self, id_token: str, nonce: str) -> Identidade:
        try:
            cabecalho = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as erro:
            raise LoginRecusado("id_token mal formado") from erro
        alg = cabecalho.get("alg")
        if alg not in self.algoritmos:
            raise LoginRecusado("algoritmo nao aceito")
        kid = cabecalho.get("kid")
        if not isinstance(kid, str) or not kid:
            raise LoginRecusado("id_token sem identificador de chave")
        try:
            chave = await self.chaves.chave(kid)
        except CrachaRecusado as erro:
            raise LoginRecusado(erro.motivo) from erro
        if chave.algorithm_name and chave.algorithm_name != alg:
            raise LoginRecusado("algoritmo nao bate com a chave")
        try:
            dados: dict[str, Any] = jwt.decode(
                id_token,
                key=chave,
                algorithms=[alg],
                audience=self.cfg.client_id,
                issuer=self.emissor,
                leeway=FOLGA_RELOGIO_SEGUNDOS,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"], "strict_aud": False},
            )
        except jwt.ExpiredSignatureError as erro:
            raise LoginRecusado("id_token vencido") from erro
        except jwt.InvalidAudienceError as erro:
            raise LoginRecusado("id_token de outro destino (audiencia)") from erro
        except jwt.InvalidIssuerError as erro:
            raise LoginRecusado("id_token de outro emissor") from erro
        except jwt.ImmatureSignatureError as erro:
            raise LoginRecusado("id_token ainda nao vale") from erro
        except jwt.PyJWTError as erro:
            raise LoginRecusado("id_token invalido") from erro
        if not iguais(str(dados.get("nonce", "")), nonce):
            raise LoginRecusado("nonce nao confere")
        audiencia = dados["aud"]
        azp = dados.get("azp")
        if isinstance(audiencia, list) and len(audiencia) > 1 and azp is None:
            raise LoginRecusado("id_token com varias audiencias e sem azp")
        if azp is not None and azp != self.cfg.client_id:
            raise LoginRecusado("id_token emitido para outro cliente (azp)")
        if int(dados["iat"]) < time.time() - IDADE_MAXIMA_ID_TOKEN:
            raise LoginRecusado("id_token antigo demais")
        sujeito = dados["sub"]
        if not isinstance(sujeito, str) or not sujeito:
            raise LoginRecusado("id_token sem sujeito")
        nome = dados.get("name") or dados.get("preferred_username")
        auth_time = dados.get("auth_time")
        return Identidade(
            emissor=str(dados["iss"]),
            sujeito=sujeito,
            nome=str(nome)[:200] if nome else None,
            auth_time=int(auth_time) if isinstance(auth_time, int | float) else None,
        )
