"""Sessoes das telas e logins em andamento, no PostgreSQL.

O banco guarda so o hash do identificador da sessao: quem le a tabela nao consegue entrar
como ninguem. O identificador cru fica so no cookie ``__Host-`` do navegador. Cada sessao tem
o seu token CSRF, prazo total e prazo parado. Sair encerra a sessao no banco.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.modelos import Pessoa


def novo_segredo(bytes_: int = 32) -> str:
    return secrets.token_urlsafe(bytes_)


def hash_sessao(identificador: str) -> bytes:
    return hashlib.sha256(identificador.encode("utf-8")).digest()


def desafio_pkce(verificador: str) -> str:
    """``code_challenge`` S256 (RFC 7636): base64url do SHA-256, sem preenchimento."""
    return base64.urlsafe_b64encode(hashlib.sha256(verificador.encode("ascii")).digest()).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class Sessao:
    hash_id: bytes
    pessoa: Pessoa
    csrf: str
    expira_em: datetime
    # Quando a pessoa digitou a senha no provedor (``auth_time`` do id_token). Sem ele, o login nunca conta como
    # recente: as acoes que pedem login refeito na hora pedem de novo.
    auth_time: datetime | None = None

    @property
    def admin(self) -> bool:
        return self.pessoa.papel == "admin"

    @property
    def quem(self) -> str:
        """Como a pessoa aparece no extrato e nas telas."""
        return self.pessoa.nome or self.pessoa.sujeito


@dataclass(frozen=True, slots=True)
class LoginPendente:
    estado: str
    nonce: str
    verificador: str
    voltar_para: str


async def criar_login(c: AsyncConnection, *, voltar_para: str, prazo_segundos: int) -> LoginPendente:
    login = LoginPendente(novo_segredo(), novo_segredo(), novo_segredo(64), voltar_para)
    await c.execute(
        text(
            "INSERT INTO logins_pendentes (estado, nonce, verificador, voltar_para, expira_em)"
            " VALUES (:e, :n, :v, :p, now() + make_interval(secs => :s))"
        ),
        {"e": login.estado, "n": login.nonce, "v": login.verificador, "p": voltar_para, "s": prazo_segundos},
    )
    return login


async def usar_login(c: AsyncConnection, estado: str) -> LoginPendente | None:
    """Gasta o estado de uma vez: um segundo uso, ou depois do prazo, nao acha nada."""
    linha = (
        await c.execute(
            text(
                "UPDATE logins_pendentes SET usado_em = now()"
                " WHERE estado = :e AND usado_em IS NULL AND expira_em > now()"
                " RETURNING estado, nonce, verificador, voltar_para"
            ),
            {"e": estado},
        )
    ).first()
    if linha is None:
        return None
    return LoginPendente(linha.estado, linha.nonce, linha.verificador, linha.voltar_para)


async def abrir_sessao(
    c: AsyncConnection, pessoa_id: uuid.UUID, *, prazo_minutos: int, auth_time: int | None
) -> tuple[str, str]:
    """Cria a sessao e devolve (identificador cru para o cookie, token CSRF)."""
    identificador, csrf = novo_segredo(), novo_segredo()
    momento = datetime.fromtimestamp(auth_time, UTC) if auth_time else None
    await c.execute(
        text(
            "INSERT INTO sessoes_telas (hash_id, pessoa_id, csrf, expira_em, auth_time)"
            " VALUES (:h, :p, :c, now() + make_interval(mins => :m), :a)"
        ),
        {"h": hash_sessao(identificador), "p": pessoa_id, "c": csrf, "m": prazo_minutos, "a": momento},
    )
    return identificador, csrf


async def sessao_valida(c: AsyncConnection, identificador: str, *, parada_minutos: int) -> Sessao | None:
    """Acha a sessao viva pelo hash. Sessao vencida, encerrada, parada demais ou de pessoa inativa nao vale."""
    if not identificador or len(identificador) > 200:
        return None
    hash_id = hash_sessao(identificador)
    linha = (
        await c.execute(
            text(
                "SELECT s.hash_id, s.csrf, s.expira_em, s.ultimo_uso, s.auth_time, p.id, p.emissor, p.sujeito, p.nome,"
                " p.papel, p.situacao, p.teste FROM sessoes_telas s JOIN pessoas p ON p.id = s.pessoa_id"
                " WHERE s.hash_id = :h AND s.encerrada_em IS NULL AND s.expira_em > now()"
                " AND s.ultimo_uso > now() - make_interval(mins => :m)"
            ),
            {"h": hash_id, "m": parada_minutos},
        )
    ).first()
    if linha is None or linha.situacao != "ativa":
        return None
    if datetime.now(UTC) - linha.ultimo_uso > timedelta(seconds=60):
        await c.execute(text("UPDATE sessoes_telas SET ultimo_uso = now() WHERE hash_id = :h"), {"h": hash_id})
    pessoa = Pessoa(linha.id, linha.emissor, linha.sujeito, linha.nome, linha.papel, linha.situacao, linha.teste)
    return Sessao(bytes(linha.hash_id), pessoa, linha.csrf, linha.expira_em, linha.auth_time)


async def encerrar_sessao(c: AsyncConnection, hash_id: bytes) -> None:
    await c.execute(
        text("UPDATE sessoes_telas SET encerrada_em = now() WHERE hash_id = :h AND encerrada_em IS NULL"),
        {"h": hash_id},
    )
