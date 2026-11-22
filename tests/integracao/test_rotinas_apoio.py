"""Apoio dos testes das rotinas e dos avisos: banco com o papel real, Mailpit e servidor HTTPS local.

As rotinas rodam nos testes com o usuario ``mcp_claro_rotinas`` de verdade (login
ligado so no banco de teste), para provar que as permissoes bastam. ``AJUSTES_0003``
traz as permissoes que faltavam na migracao 0003 (ver o relatorio da Fase 1); o
fixture as aplica, e elas viram no-op quando a 0003 ja as tiver.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from tests.apoio import servidor

IMAGEM_MAILPIT = "axllent/mailpit:v1.34.0"

AJUSTES_0003 = ""
"""As permissoes que faltavam entraram na migracao 0003; os testes provam que ela basta."""


def comandos(sql: str) -> list[str]:
    linhas = [linha for linha in sql.splitlines() if linha.strip() and not linha.strip().startswith("--")]
    return [c.strip() for c in "\n".join(linhas).split(";") if c.strip()]


@pytest.fixture(scope="session")
async def parametros_rotinas(banco_migrado: Parametros) -> Parametros:
    """Parametros do papel ``rotinas`` com login real no banco de teste e os ajustes da 0003."""
    dono = criar_engine(banco_migrado, pool_size=1, max_overflow=0)
    senha = banco_migrado.senha_banco
    assert senha is not None
    try:
        async with dono.begin() as c:
            for comando in comandos(AJUSTES_0003):
                await c.execute(text(comando))
            await c.execute(text(f"ALTER ROLE mcp_claro_rotinas WITH LOGIN PASSWORD '{senha.get_secret_value()}'"))
    finally:
        await dono.dispose()
    url = banco_migrado.DATABASE_URL.replace("mcp_claro_migracao@", "mcp_claro_rotinas@")
    return Parametros(**(banco_migrado.model_dump() | {"DATABASE_URL": url}))


@pytest.fixture
async def engine_rotinas(parametros_rotinas: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(parametros_rotinas, pool_size=6, max_overflow=4)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def engine_dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    """Engine do dono do esquema, so para preparar e conferir o banco nos testes."""
    engine = criar_engine(banco_migrado, pool_size=3, max_overflow=2)
    try:
        yield engine
    finally:
        await engine.dispose()


# --- Mailpit -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mailpit:
    host: str
    smtp: int
    api: str

    def mensagens(self) -> list[dict[str, Any]]:
        r = httpx2.get(f"{self.api}/api/v1/messages", params={"limit": 500}, timeout=5)
        r.raise_for_status()
        return list(r.json()["messages"])

    def com_assunto(self, assunto: str) -> list[dict[str, Any]]:
        return [m for m in self.mensagens() if m["Subject"] == assunto]

    def texto(self, id_: str) -> str:
        r = httpx2.get(f"{self.api}/api/v1/message/{id_}", timeout=5)
        r.raise_for_status()
        return str(r.json()["Text"])

    def caos(self, *, destinatario: int = 0, codigo: int = 451) -> None:
        """Modo de erro forcado do Mailpit: recusa destinatarios com a probabilidade dada (0 a 100)."""
        r = httpx2.put(
            f"{self.api}/api/v1/chaos",
            json={"Recipient": {"ErrorCode": codigo, "Probability": destinatario}},
            timeout=5,
        )
        r.raise_for_status()

    async def esperar(self, assunto: str, prazo: float = 15.0) -> list[dict[str, Any]]:
        fim = time.monotonic() + prazo
        while time.monotonic() < fim:
            achadas = self.com_assunto(assunto)
            if achadas:
                return achadas
            await asyncio.sleep(0.2)
        return []


@pytest.fixture(scope="session")
def mailpit() -> Iterator[Mailpit]:
    container_mod = pytest.importorskip("testcontainers.core.container")
    c = (
        container_mod.DockerContainer(IMAGEM_MAILPIT)
        .with_exposed_ports(1025, 8025)
        .with_command("--enable-chaos --smtp-auth-accept-any --smtp-auth-allow-insecure")
    )
    with c:
        host = c.get_container_host_ip()
        m = Mailpit(host, int(c.get_exposed_port(1025)), f"http://{host}:{c.get_exposed_port(8025)}")
        for _ in range(100):
            try:
                if httpx2.get(f"{m.api}/api/v1/info", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            time.sleep(0.1)
        yield m


# --- Servidor HTTPS local para os webhooks ---------------------------------------------------


def certificado_local(pasta: Path) -> tuple[Path, Path]:
    """Certificado autoassinado para 127.0.0.1, so de teste."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    chave = ec.generate_private_key(ec.SECP256R1())
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "destino-webhook-teste")])
    agora = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(minutes=5))
        .not_valid_after(agora + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(chave, hashes.SHA256())
    )
    caminho_cert = pasta / "cert.pem"
    caminho_chave = pasta / "chave.pem"
    caminho_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    caminho_chave.write_bytes(
        chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return caminho_cert, caminho_chave


@contextlib.asynccontextmanager
async def servidor_https(app: Any, cert: Path, chave: Path) -> AsyncIterator[str]:
    """Sobe ``app`` em https://127.0.0.1:<porta> com o certificado dado, por ``tests.apoio.servidor``: a porta fica
    reservada desde a escolha ate o servidor escutar nela (outra sessao de testes nao a toma no meio), e a URL so
    sai com o servidor no ar. Se ele nao sobe, o teste para aqui, com o erro da partida ou um ``RuntimeError`` com
    o endereco, em vez de seguir com a porta fechada."""
    async with servidor(app, ssl_certfile=str(cert), ssl_keyfile=str(chave)) as url:
        yield url


def confia_em(cert: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(cert))


# --- Cadastro minimo -------------------------------------------------------------------------


@dataclass(frozen=True)
class Cadastro:
    aplicativo_id: uuid.UUID
    pessoa_id: uuid.UUID
    modelo_id: uuid.UUID
    email: str | None
    sufixo: str


async def cadastro_minimo(engine_dono: AsyncEngine, *, email: str | None = None) -> Cadastro:
    from mcp_claro.cadastro import repositorio

    sufixo = uuid.uuid4().hex[:8]
    async with engine_dono.begin() as c:
        app_id = await repositorio.habilitar_aplicativo(
            c, client_id=f"app-{sufixo}", nome=f"Aplicativo {sufixo}", familia="teste"
        )
        pessoa = await repositorio.pessoa_no_login(c, "https://login.teste", f"pessoa-{sufixo}", None)
        await c.execute(text("UPDATE pessoas SET teste = true, email = :e WHERE id = :p"), {"e": email, "p": pessoa.id})
        modelo = await repositorio.criar_modelo_ordem(
            c, nome=f"modelo-{sufixo}", descricao="Ordem de teste", ferramentas=[], validade_dias=30, criado_por="t"
        )
    return Cadastro(app_id, pessoa.id, modelo, email, sufixo)


async def ordem(
    engine_dono: AsyncEngine,
    cad: Cadastro,
    *,
    inicio: str,
    fim: str,
    ultimo_uso: str | None = None,
) -> uuid.UUID:
    """Cria uma ordem com datas relativas ao agora do banco (ex.: ``'-10 days'``, ``'3 days'``)."""
    async with engine_dono.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO ordens_servico (pessoa_id, aplicativo_id, modelo_id, inicio, fim, ultimo_uso)"
                    " VALUES (:p, :a, :m, now() + CAST(CAST(:i AS text) AS interval),"
                    " now() + CAST(CAST(:f AS text) AS interval), now() + CAST(CAST(:u AS text) AS interval))"
                    " RETURNING id"
                ),
                {
                    "p": cad.pessoa_id,
                    "a": cad.aplicativo_id,
                    "m": cad.modelo_id,
                    "i": inicio,
                    "f": fim,
                    "u": ultimo_uso,
                },
            )
        ).scalar_one()
    return uuid.UUID(str(valor))
