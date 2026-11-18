"""Provedor de login falso, so de teste: emite crachas JWT e serve a lista JWKS."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


@dataclass
class IdpFalso:
    emissor: str
    kid: str = "chave-teste-1"
    chave: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )

    def jwks(self) -> dict[str, Any]:
        publica = RSAAlgorithm.to_jwk(self.chave.public_key(), as_dict=True)
        publica.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return {"keys": [publica]}

    def cracha(
        self,
        *,
        audiencia: str,
        sujeito: str = "pessoa-ana",
        client_id: str = "claude-code",
        validade: int = 300,
        emissor: str | None = None,
        chave: rsa.RSAPrivateKey | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        agora = int(time.time())
        dados = {
            "iss": emissor or self.emissor,
            "sub": sujeito,
            "aud": audiencia,
            "azp": client_id,
            "iat": agora,
            "nbf": agora,
            "exp": agora + validade,
            "jti": str(uuid.uuid4()),
            "name": sujeito,
        } | (extra or {})
        return jwt.encode(dados, chave or self.chave, algorithm="RS256", headers={"kid": self.kid})

    def app(self) -> Starlette:
        async def certs(_: Request) -> JSONResponse:
            return JSONResponse(self.jwks())

        return Starlette(routes=[Route("/jwks", certs)])
