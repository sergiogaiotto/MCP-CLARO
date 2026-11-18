"""Provedor OIDC falso, so de teste, para o login das telas.

Estende ``tests.idp_falso.IdpFalso`` (mesma chave e mesmo JWKS) com descoberta, ``/authorize`` e
``/token``. O ``/authorize`` nao mostra tela: devolve direto o codigo para a pessoa escolhida em
``proxima_pessoa``. O ``/token`` confere ``redirect_uri``, PKCE S256, uso unico do codigo e o segredo
do cliente (``client_secret_basic``), como um provedor de verdade.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlencode

import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tests.idp_falso import IdpFalso


@dataclass
class IdpOidcFalso(IdpFalso):
    client_id: str = "mcp-claro-telas"
    segredo: str | None = "segredo-das-telas"
    redirect_uri: str = "https://mcp-claro.teste/telas/login/volta"
    proxima_pessoa: tuple[str, str | None] = ("pessoa-ana", "Ana")
    # Mexe no id_token para testar o rigor da conferencia (nonce, audiencia, azp...).
    estragar: dict[str, Any] = field(default_factory=dict)
    codigos: dict[str, dict[str, Any]] = field(default_factory=dict)
    trocas: int = 0

    def descoberta(self) -> dict[str, Any]:
        return {
            "issuer": self.emissor,
            "authorization_endpoint": f"{self.emissor}/authorize",
            "token_endpoint": f"{self.emissor}/token",
            "jwks_uri": f"{self.emissor}/jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    def id_token(self, dados: dict[str, Any]) -> str:
        agora = int(time.time())
        sujeito, nome = dados["pessoa"]
        corpo = {
            "iss": self.emissor,
            "sub": sujeito,
            "aud": self.client_id,
            "azp": self.client_id,
            "iat": agora,
            "exp": agora + 300,
            "auth_time": agora,
            "nonce": dados["nonce"],
        }
        if nome:
            corpo["name"] = nome
        corpo |= self.estragar
        return jwt.encode({k: v for k, v in corpo.items() if v is not None}, self.chave, "RS256", {"kid": self.kid})

    def app(self) -> Starlette:
        async def descoberta(_: Request) -> JSONResponse:
            return JSONResponse(self.descoberta())

        async def certs(_: Request) -> JSONResponse:
            return JSONResponse(self.jwks())

        async def autorizar(pedido: Request) -> Response:
            q = pedido.query_params
            if q.get("client_id") != self.client_id or q.get("redirect_uri") != self.redirect_uri:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256":
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            if "openid" not in (q.get("scope") or "").split() or not q.get("nonce") or not q.get("state"):
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            codigo = secrets.token_urlsafe(16)
            self.codigos[codigo] = {
                "desafio": q["code_challenge"],
                "nonce": q["nonce"],
                "redirect_uri": q["redirect_uri"],
                "pessoa": self.proxima_pessoa,
            }
            volta = self.redirect_uri + "?" + urlencode({"code": codigo, "state": q["state"], "iss": self.emissor})
            return RedirectResponse(volta, status_code=302)

        async def token(pedido: Request) -> JSONResponse:
            self.trocas += 1
            form = await pedido.form()
            if self.segredo is not None:
                auth = pedido.headers.get("authorization", "")
                if not auth.startswith("Basic "):
                    return JSONResponse({"error": "invalid_client"}, status_code=401)
                cid, _, seg = base64.b64decode(auth[6:]).decode().partition(":")
                if unquote(cid) != self.client_id or unquote(seg) != self.segredo:
                    return JSONResponse({"error": "invalid_client"}, status_code=401)
            elif form.get("client_id") != self.client_id:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            dados = self.codigos.pop(str(form.get("code", "")), None)  # uso unico
            if dados is None or form.get("grant_type") != "authorization_code":
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if form.get("redirect_uri") != dados["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verificador = str(form.get("code_verifier", ""))
            desafio = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()
            if desafio != dados["desafio"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
            return JSONResponse(
                {
                    "access_token": "acesso-que-as-telas-nao-usam",
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": self.id_token(dados),
                }
            )

        prefixo = "/" + self.emissor.split("/", 3)[3] if self.emissor.count("/") >= 3 else ""
        return Starlette(
            routes=[
                Route("/jwks", certs),
                Route(f"{prefixo}/jwks", certs),
                Route(f"{prefixo}/.well-known/openid-configuration", descoberta),
                Route(f"{prefixo}/authorize", autorizar),
                Route(f"{prefixo}/token", token, methods=["POST"]),
            ]
        )
