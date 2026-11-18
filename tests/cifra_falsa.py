"""Cifra local so de teste, no formato do Transit (``vault:v1:``), com contexto como dado associado."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CifraFalsa:
    def __init__(self) -> None:
        self._aes = AESGCM(AESGCM.generate_key(bit_length=256))

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        nonce = os.urandom(12)
        return "vault:v1:" + base64.b64encode(nonce + self._aes.encrypt(nonce, texto, contexto)).decode()

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        bruto = base64.b64decode(cifrado.removeprefix("vault:v1:"))
        return self._aes.decrypt(bruto[:12], bruto[12:], contexto)
