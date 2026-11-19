"""Autenticador de passkey so de teste, em software: chave ES256 (P-256) com ``cryptography``.

Monta as respostas do jeito do WebAuthn nivel 3, em JSON (``RegistrationResponseJSON`` e
``AuthenticationResponseJSON``), sem usar a biblioteca do servidor:

- ``clientDataJSON``: tipo, desafio (base64url), origem e ``crossOrigin``;
- ``authenticatorData``: hash do RP id, flags (UP, UV e, no cadastro, AT), contador e, no cadastro, os dados da
  credencial (AAGUID, id e chave publica COSE);
- cadastro: ``attestationObject`` com formato "none" (CBOR);
- entrada: assinatura ECDSA SHA-256 (DER) sobre ``authenticatorData || SHA-256(clientDataJSON)``.

O CBOR e escrito a mao (so inteiros, bytes, texto e mapas), para nao depender do codigo conferido.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, AT = 0x01, 0x04, 0x40


def b64url(dado: bytes) -> str:
    return base64.urlsafe_b64encode(dado).rstrip(b"=").decode("ascii")


def de_b64url(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _cabeca(tipo: int, valor: int) -> bytes:
    if valor < 24:
        return bytes([tipo << 5 | valor])
    if valor < 0x100:
        return bytes([tipo << 5 | 24, valor])
    if valor < 0x10000:
        return bytes([tipo << 5 | 25]) + struct.pack(">H", valor)
    return bytes([tipo << 5 | 26]) + struct.pack(">I", valor)


def cbor(valor: Any) -> bytes:
    if isinstance(valor, bool):
        raise TypeError("sem booleano neste CBOR")
    if isinstance(valor, int):
        return _cabeca(0, valor) if valor >= 0 else _cabeca(1, -1 - valor)
    if isinstance(valor, bytes):
        return _cabeca(2, len(valor)) + valor
    if isinstance(valor, str):
        dado = valor.encode("utf-8")
        return _cabeca(3, len(dado)) + dado
    if isinstance(valor, dict):
        return _cabeca(5, len(valor)) + b"".join(cbor(k) + cbor(v) for k, v in valor.items())
    raise TypeError(f"tipo sem CBOR: {type(valor).__name__}")


@dataclass
class AutenticadorFalso:
    origem: str
    chave: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    credencial_id: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    aaguid: bytes = field(default_factory=lambda: bytes(16))
    contador: int = 0
    verifica_pessoa: bool = True
    user_handle: bytes | None = None

    def chave_cose(self) -> bytes:
        numeros = self.chave.public_key().public_numbers()
        # COSE_Key EC2: kty 2, alg -7 (ES256), crv 1 (P-256), x, y.
        return cbor({1: 2, 3: -7, -1: 1, -2: numeros.x.to_bytes(32, "big"), -3: numeros.y.to_bytes(32, "big")})

    def _client_data(self, tipo: str, desafio: str, origem: str | None) -> bytes:
        return json.dumps(
            {"type": tipo, "challenge": desafio, "origin": origem or self.origem, "crossOrigin": False},
            separators=(",", ":"),
        ).encode("utf-8")

    def _flags(self, extra: int = 0) -> int:
        return UP | (UV if self.verifica_pessoa else 0) | extra

    def criar(self, opcoes: dict[str, Any], *, origem: str | None = None) -> dict[str, Any]:
        """Resposta a ``navigator.credentials.create`` (as opcoes vem como o servidor manda, em JSON)."""
        pk = opcoes.get("publicKey", opcoes)
        rp_id = pk["rp"]["id"]
        self.user_handle = de_b64url(pk["user"]["id"])
        client_data = self._client_data("webauthn.create", pk["challenge"], origem)
        dados_credencial = self.aaguid + struct.pack(">H", len(self.credencial_id)) + self.credencial_id
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([self._flags(AT)])
            + struct.pack(">I", self.contador)
            + dados_credencial
            + self.chave_cose()
        )
        atestado = cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credencial_id),
            "rawId": b64url(self.credencial_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(atestado),
                "transports": ["internal"],
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }

    def assinar(self, opcoes: dict[str, Any], *, origem: str | None = None, rp_id: str | None = None) -> dict[str, Any]:
        """Resposta a ``navigator.credentials.get``. Cada uso sobe o contador, como num autenticador de verdade."""
        pk = opcoes.get("publicKey", opcoes)
        self.contador += 1
        client_data = self._client_data("webauthn.get", pk["challenge"], origem)
        auth_data = (
            hashlib.sha256((rp_id or pk["rpId"]).encode()).digest()
            + bytes([self._flags()])
            + struct.pack(">I", self.contador)
        )
        assinatura = self.chave.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        resposta: dict[str, Any] = {
            "clientDataJSON": b64url(client_data),
            "authenticatorData": b64url(auth_data),
            "signature": b64url(assinatura),
        }
        if self.user_handle is not None:
            resposta["userHandle"] = b64url(self.user_handle)
        return {
            "id": b64url(self.credencial_id),
            "rawId": b64url(self.credencial_id),
            "type": "public-key",
            "response": resposta,
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }
