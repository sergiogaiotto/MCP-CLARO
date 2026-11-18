"""Suite oficial de conformidade do MCP (2026-07-28) ATRAVES do gateway MCP-CLARO.

Prova de saida de "Provar a base" (docs/plano.md): suite oficial verde direto na entrada e atraves do
portao proprio (Caddy), com o sistema de exemplo da suite atras e uma ordem de servico de teste. O que
o desenho deixa de fora fica em ``conformidade/falhas-esperadas-gateway.yml``, com o motivo escrito.

Arranjo, tudo so de teste, subido por este roteiro::

    suite --> proxy de teste --> entrada --> despachante --> exemplos/conformidade.py
              (poe o cracha)     (cracha, porteiro, vitrine)  (cifra falsa)

    com --portao:
    suite --> proxy de teste --> Caddy do produto (Docker, HTTPS) --mTLS--> entrada --mTLS--> despachante --> ...

- PostgreSQL 18.7 real (testcontainers), migracoes do produto e um usuario do banco por papel, como no Compose.
- Provedor de login falso (``tests/idp_falso.py``) e cifra falsa (``tests/cifra_falsa.py``) no lugar do Keycloak
  e do OpenBao.
- Cadastro pelas mesmas portas do ADMIN: aplicativo habilitado, sistema sondado pelo despachante, chave
  compartilhada guardada pela porta interna, TODAS as ferramentas do fixture revisadas como "le" com o nome
  e a descricao originais (a suite chama pelos nomes ``test_*``), modelo e ordem de servico para a pessoa de
  teste, marcada como teste (a chave FEATURE_PHASE1_READ continua desligada).
- O proxy so de teste injeta o cracha da pessoa de teste em todo pedido ("Regras dos testes", no plano).
  Ele troca o proprio endereco pelo endereco publico do gateway; qualquer outro Host ou Origin passa como
  veio (o cenario de DNS rebinding manda ``evil.example.com`` e precisa chegar ao gateway assim).
- Com ``--portao``, o Caddy e a imagem do produto (``docker/caddy``) com o Caddyfile do repositorio, so com
  variaveis trocadas: o endereco da entrada e o limite grosso por origem (a suite passa de 120 pedidos por
  minuto). Os certificados internos vem de ``scripts/gerar-segredos-dev.sh``, rodado numa pasta temporaria.
  A entrada e o despachante sobem com mTLS nas portas internas, como no Compose.

Uso, a partir da raiz do repositorio (precisa de Docker e de Node 22 com npx)::

    uv run --frozen python -m conformidade.gateway            # direto na entrada
    uv run --frozen python -m conformidade.gateway --portao   # atraves do Caddy

Saida em ``conformidade/resultados-gateway/<direto|portao>/``: saida da suite, ``checks.json`` de cada
cenario, resumo dos exigidos e logs do gateway e do portao. O codigo de saida e o da suite: 0 quando todo
cenario exigido passa ou falha so como previsto na lista de falhas esperadas.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import httpx2
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from exemplos import conformidade as fixture
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.logs import configurar
from tests.apoio import porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_falso import IdpFalso

# Versao FIXADA de proposito, a mesma de conformidade/rodar.sh. Trocar a versao e mudanca revisada.
SUITE: Final = "@modelcontextprotocol/conformance@0.2.0-alpha.12"
REQUISITOS: Final = "2026-07-28"

RAIZ: Final = Path(__file__).resolve().parents[1]
FALHAS_PADRAO: Final = RAIZ / "conformidade" / "falhas-esperadas-gateway.yml"
RESULTADOS_PADRAO: Final = RAIZ / "conformidade" / "resultados-gateway"
IMAGEM_POSTGRES: Final = os.environ.get("MCP_CLARO_TESTE_POSTGRES", "postgres:18.7")
IMAGEM_PORTAO: Final = os.environ.get("CONFORMIDADE_IMAGEM_PORTAO", "mcp-claro-portao:dev")

PUBLICO: Final = "https://mcp-claro.localhost"
HOST_PUBLICO: Final = "mcp-claro.localhost"
RECURSO: Final = f"{PUBLICO}/mcp"
LIMITE_PORTAO_CONFORMIDADE: Final = 100_000
"""Limite grosso por origem no portao durante a suite: todos os pedidos dela saem do mesmo endereco."""

ARQUIVOS_DO_PORTAO: Final = ("portao_cliente_cert.pem", "portao_cliente_key.pem", "ca_interna.pem")
_SALTO: Final = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_TROCADOS_NO_PEDIDO: Final = _SALTO | {b"host", b"origin", b"authorization", b"content-length"}

log = logging.getLogger("conformidade.gateway")


# ---------------------------------------------------------------- arranjo


@dataclass(frozen=True, slots=True)
class Portao:
    """Onde achar a imagem do Caddy do produto e os certificados internos de teste."""

    imagem: str
    segredos: Path


@dataclass(frozen=True, slots=True)
class Arranjo:
    url_mcp: str
    """Endereco MCP do proxy de teste: o que a suite (ou o cliente do teste) usa."""
    url_entrada: str
    url_despachante: str
    url_sistema: str
    url_portao: str | None
    client_id: str
    sujeito: str
    ferramentas: tuple[str, ...]
    sistema_id: uuid.UUID
    ordem_id: uuid.UUID


def _url_com_usuario(url: str, usuario: str) -> str:
    partes = urlsplit(url)
    porta = f":{partes.port}" if partes.port else ""
    return urlunsplit((partes.scheme, f"{usuario}@{partes.hostname}{porta}", partes.path, "", ""))


async def _ligar_usuarios_dos_papeis(banco: Parametros, pasta: Path) -> dict[str, Path]:
    """Da login e senha aos usuarios da entrada e do despachante (no Compose, quem faz e o banco na partida)."""
    engine = criar_engine(banco, pool_size=1, max_overflow=0)
    arquivos: dict[str, Path] = {}
    try:
        async with engine.begin() as c:
            for papel in ("mcp_claro_entrada", "mcp_claro_despachante"):
                senha = secrets.token_hex(24)  # so hexadecimal: entra literal no ALTER ROLE sem escapar nada
                await c.execute(text(f"ALTER ROLE {papel} WITH LOGIN PASSWORD '{senha}'"))
                arquivo = pasta / f"db_{papel}"
                arquivo.write_text(senha, encoding="utf-8")
                arquivo.chmod(0o600)
                arquivos[papel] = arquivo
    finally:
        await engine.dispose()
    return arquivos


def _tls_do_servidor(certificado: Path, chave: Path, autoridade_clientes: Path) -> dict[str, Any]:
    """Porta interna com mTLS: so entra quem tem certificado da autoridade de clientes dela."""
    return {
        "ssl_certfile": str(certificado),
        "ssl_keyfile": str(chave),
        "ssl_ca_certs": str(autoridade_clientes),
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def _docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("docker nao encontrado: o banco de teste e o portao rodam em Docker.")
    return docker


def _ip_da_ponte_do_docker() -> str:
    """Endereco deste computador na rede padrao do Docker: e onde o ``host-gateway`` do conteiner chega."""
    formato = "{{(index .IPAM.Config 0).Gateway}}"
    resultado = subprocess.run(  # noqa: S603 - comando fixo
        [_docker(), "network", "inspect", "bridge", "--format", formato], check=True, capture_output=True, text=True
    )
    return resultado.stdout.strip()


async def _cadastrar(
    engine_admin: AsyncEngine,
    despachante: ClienteDespachante,
    *,
    emissor: str,
    url_sistema: str,
    sufixo: str,
) -> tuple[str, str, uuid.UUID, uuid.UUID, tuple[str, ...]]:
    """Cadastro do ADMIN, pelas mesmas permissoes das telas (usuario da entrada) e da porta interna."""
    client_id = f"conformidade-{sufixo}"
    sujeito = f"pessoa-conformidade-{sufixo}"
    async with engine_admin.begin() as c:
        aplicativo_id = await repositorio.habilitar_aplicativo(
            c, client_id=client_id, nome="Suite oficial de conformidade", familia="conformidade"
        )
        sistema_id = await repositorio.cadastrar_sistema(
            c, nome=f"exemplo-conformidade-{sufixo}", endereco=f"{url_sistema}/mcp", tipo_chave="compartilhada"
        )
        pessoa = await repositorio.pessoa_no_login(c, emissor, sujeito, "Pessoa de teste da conformidade")
        await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": pessoa.id})
    # O fixture nao confere chave; a chave compartilhada so existe para o caminho ser o de producao.
    await despachante.post(
        "/interno/chaves", {"sistema_id": str(sistema_id), "segredo": "chave-compartilhada", "quem": "conformidade"}
    )
    sondagem = await despachante.post("/interno/sondar", {"sistema_id": str(sistema_id)})
    if not sondagem["aceito"]:
        raise RuntimeError(f"o despachante recusou o fixture na sondagem: {sondagem}")
    lidas = await despachante.post("/interno/declaracoes", {"sistema_id": str(sistema_id)})
    declaradas: list[dict[str, Any]] = lidas["ferramentas"]
    nomes = tuple(str(f["name"]) for f in declaradas)
    async with engine_admin.begin() as c:
        for definicao in declaradas:
            # Todas as ferramentas do fixture sao de teste e o gateway ainda nao tem acoes: revisadas como "le",
            # com o nome e a descricao originais, porque a suite chama pelos nomes test_*.
            await repositorio.revisar_ferramenta(
                c,
                sistema_id=sistema_id,
                definicao_declarada=definicao,
                nome_vitrine=str(definicao["name"]),
                descricao=str(definicao.get("description") or definicao["name"]),
                classe="le",
                revisado_por="conformidade",
                risco="baixo",
            )
        modelo_id = await repositorio.criar_modelo_ordem(
            c,
            nome=f"conformidade-{sufixo}",
            descricao="Ordem de servico de teste da suite oficial de conformidade",
            ferramentas=[(sistema_id, nome) for nome in nomes],
            validade_dias=1,
            criado_por="conformidade",
        )
        ordem_id = await repositorio.conceder_ordem(
            c, pessoa_id=pessoa.id, aplicativo_id=aplicativo_id, modelo_id=modelo_id, dias=1, max_dias=1
        )
    return client_id, sujeito, sistema_id, ordem_id, nomes


async def _desfazer_cadastro(engine_admin: AsyncEngine, sistema_id: uuid.UUID) -> None:
    """Tira as ferramentas da vitrine e desativa o sistema: o nome test_* fica livre para a proxima rodada."""
    async with engine_admin.begin() as c:
        await c.execute(
            text(
                "UPDATE ferramentas_revisadas SET situacao = 'suspensa', motivo = 'fim da rodada de conformidade'"
                " WHERE sistema_id = :s AND situacao = 'ativa'"
            ),
            {"s": sistema_id},
        )
        await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), {"s": sistema_id})


@contextlib.asynccontextmanager
async def subir_arranjo(banco: Parametros, pasta: Path, *, portao: Portao | None = None) -> AsyncIterator[Arranjo]:
    """Sobe provedor falso, fixture, despachante, entrada, (portao) e proxy; cadastra tudo; desfaz ao sair.

    ``banco`` aponta para o PostgreSQL ja migrado, com o usuario dono do esquema (o das migracoes).
    """
    senhas = await _ligar_usuarios_dos_papeis(banco, pasta)
    porta_idp, porta_desp, porta_entrada, porta_proxy = (porta_livre() for _ in range(4))
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    comum: dict[str, Any] = {
        "APP_ENV": "teste",
        "PUBLIC_BASE_URL": PUBLICO,
        "IDP_KIND": "oidc",
        "IDP_ISSUER_URL": idp.emissor,
        "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
        "RESPONSE_MAX_WAIT_SECONDS": 10,
        "DB_SSLMODE": banco.DB_SSLMODE,
        "DB_SSLROOTCERT": banco.DB_SSLROOTCERT,
    }
    host_entrada = "127.0.0.1"
    tls_entrada: dict[str, Any] = {}
    tls_desp: dict[str, Any] = {}
    if portao is not None:
        s = portao.segredos
        host_entrada = await asyncio.to_thread(_ip_da_ponte_do_docker)
        comum |= {
            "DISPATCHER_INTERNAL_URL": f"https://127.0.0.1:{porta_desp}",
            "INTERNAL_CA_FILE": str(s / "ca_interna.pem"),
        }
        p_entrada = Parametros(
            **comum,
            DATABASE_URL=_url_com_usuario(banco.DATABASE_URL, "mcp_claro_entrada"),
            DATABASE_PASSWORD_FILE=str(senhas["mcp_claro_entrada"]),
            INTERNAL_CERT_FILE=str(s / "entrada_cert.pem"),
            INTERNAL_KEY_FILE=str(s / "entrada_key.pem"),
            INTERNAL_CLIENT_CA_FILE=str(s / "ca_clientes_entrada.pem"),
            INTERNAL_CLIENT_CERT_FILE=str(s / "entrada_cliente_cert.pem"),
            INTERNAL_CLIENT_KEY_FILE=str(s / "entrada_cliente_key.pem"),
        )
        p_desp = Parametros(
            **comum,
            DATABASE_URL=_url_com_usuario(banco.DATABASE_URL, "mcp_claro_despachante"),
            DATABASE_PASSWORD_FILE=str(senhas["mcp_claro_despachante"]),
            INTERNAL_CERT_FILE=str(s / "despachante_cert.pem"),
            INTERNAL_KEY_FILE=str(s / "despachante_key.pem"),
            INTERNAL_CLIENT_CA_FILE=str(s / "ca_clientes_despachante.pem"),
        )
        # A porta da entrada aceita so o certificado do portao; a do despachante, so o da entrada.
        tls_entrada = _tls_do_servidor(s / "entrada_cert.pem", s / "entrada_key.pem", s / "ca_clientes_entrada.pem")
        tls_desp = _tls_do_servidor(
            s / "despachante_cert.pem", s / "despachante_key.pem", s / "ca_clientes_despachante.pem"
        )
    else:
        comum["DISPATCHER_INTERNAL_URL"] = f"http://127.0.0.1:{porta_desp}"
        p_entrada = Parametros(
            **comum,
            DATABASE_URL=_url_com_usuario(banco.DATABASE_URL, "mcp_claro_entrada"),
            DATABASE_PASSWORD_FILE=str(senhas["mcp_claro_entrada"]),
        )
        p_desp = Parametros(
            **comum,
            DATABASE_URL=_url_com_usuario(banco.DATABASE_URL, "mcp_claro_despachante"),
            DATABASE_PASSWORD_FILE=str(senhas["mcp_claro_despachante"]),
        )

    async with contextlib.AsyncExitStack() as pilha:
        engine_entrada = criar_engine(p_entrada, pool_size=10, max_overflow=10)
        pilha.push_async_callback(engine_entrada.dispose)
        engine_desp = criar_engine(p_desp, pool_size=10, max_overflow=10)
        pilha.push_async_callback(engine_desp.dispose)

        await pilha.enter_async_context(servidor(idp.app(), porta_idp))
        url_sistema = await pilha.enter_async_context(servidor(fixture.criar()))
        url_desp = await pilha.enter_async_context(
            servidor(criar_app_despachante(p_desp, engine_desp, CifraFalsa()), porta_desp, **tls_desp)
        )
        url_entrada = await pilha.enter_async_context(
            servidor(criar_app_entrada(p_entrada, engine_entrada), porta_entrada, host=host_entrada, **tls_entrada)
        )

        despachante = ClienteDespachante(p_entrada)
        try:
            client_id, sujeito, sistema_id, ordem_id, nomes = await _cadastrar(
                engine_entrada, despachante, emissor=idp.emissor, url_sistema=url_sistema, sufixo=uuid.uuid4().hex[:8]
            )
        finally:
            await despachante.fechar()
        pilha.push_async_callback(_desfazer_cadastro, engine_entrada, sistema_id)

        url_portao: str | None = None
        destino: str = url_entrada
        confianca: ssl.SSLContext | bool = False
        sni: str | None = None
        if portao is not None:
            # O proxy fala com o portao pelo endereco publico (SNI e Host), confiando so na CA local do Caddy.
            destino, raiz = await pilha.enter_async_context(subir_portao(portao, porta_entrada, pasta / "portao.log"))
            url_portao, confianca, sni = destino, ssl.create_default_context(cadata=raiz), HOST_PUBLICO

        def cracha() -> str:
            return idp.cracha(audiencia=RECURSO, sujeito=sujeito, client_id=client_id)

        proxy = criar_proxy(destino, cracha, porta_propria=porta_proxy, verificar=confianca, sni=sni)
        url_proxy = await pilha.enter_async_context(servidor(proxy, porta_proxy))
        yield Arranjo(
            url_mcp=f"{url_proxy}/mcp",
            url_entrada=url_entrada,
            url_despachante=url_desp,
            url_sistema=url_sistema,
            url_portao=url_portao,
            client_id=client_id,
            sujeito=sujeito,
            ferramentas=nomes,
            sistema_id=sistema_id,
            ordem_id=ordem_id,
        )


# ---------------------------------------------------------------- proxy so de teste


def criar_proxy(
    destino: str,
    cracha: Callable[[], str],
    *,
    porta_propria: int,
    verificar: ssl.SSLContext | bool = True,
    sni: str | None = None,
) -> Starlette:
    """Proxy so de teste: poe o cracha da pessoa de teste em todo pedido e repassa ao gateway, sem repetir.

    - Tira qualquer ``Authorization`` que chegue e poe um cracha novo, emitido agora pelo provedor falso.
    - ``Host`` e ``Origin`` com o proprio endereco do proxy viram o endereco publico do gateway (o aplicativo
      fala com o endereco publico); qualquer outro valor passa como veio.
    - Repassa o corpo e a resposta sem mexer, em fluxo, inclusive a compressao.
    """
    proprios = {f"{host}:{porta_propria}" for host in ("127.0.0.1", "localhost", "[::1]")}
    http = httpx2.AsyncClient(
        verify=verificar,
        timeout=httpx2.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0),
        follow_redirects=False,
        trust_env=False,
    )
    extensoes: dict[str, Any] = {"sni_hostname": sni} if sni else {}

    async def repassar(pedido: Request) -> Response:
        cabecalhos = [(k, v) for k, v in pedido.headers.raw if k not in _TROCADOS_NO_PEDIDO]
        host = pedido.headers.get("host", "")
        cabecalhos.append((b"host", (HOST_PUBLICO if host in proprios else host).encode("latin-1")))
        for origem in pedido.headers.getlist("origin"):
            proprio = urlsplit(origem).netloc in proprios and origem.startswith("http://")
            cabecalhos.append((b"origin", (PUBLICO if proprio else origem).encode("latin-1")))
        cabecalhos.append((b"authorization", f"Bearer {cracha()}".encode("ascii")))
        alvo = destino + pedido.url.path + (f"?{pedido.url.query}" if pedido.url.query else "")
        enviado = http.build_request(
            pedido.method, alvo, headers=cabecalhos, content=await pedido.body(), extensions=extensoes
        )
        try:
            resposta = await http.send(enviado, stream=True)
        except httpx2.TransportError as erro:
            return JSONResponse(
                {"error": "proxy_de_teste", "error_description": f"gateway inalcancavel: {type(erro).__name__}"},
                status_code=502,
            )
        saida = StreamingResponse(
            resposta.aiter_raw(), status_code=resposta.status_code, background=BackgroundTask(resposta.aclose)
        )
        saida.raw_headers = [(k.lower(), v) for k, v in resposta.headers.raw if k.lower() not in _SALTO]
        return saida

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()

    metodos = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    return Starlette(routes=[Route("/{caminho:path}", repassar, methods=metodos)], lifespan=ciclo)


# ---------------------------------------------------------------- portao (Caddy do produto)


def gerar_segredos(pasta: Path) -> Path:
    """Roda ``scripts/gerar-segredos-dev.sh`` numa copia dentro de ``pasta``; devolve a ``.segredos`` gerada."""
    copia = pasta / "scripts"
    copia.mkdir(parents=True, exist_ok=True)
    roteiro = copia / "gerar-segredos-dev.sh"
    shutil.copy2(RAIZ / "scripts" / "gerar-segredos-dev.sh", roteiro)
    bash = shutil.which("bash") or "/bin/bash"
    subprocess.run([bash, str(roteiro)], check=True, capture_output=True)  # noqa: S603 - roteiro do repositorio
    return pasta / ".segredos"


def garantir_imagem_do_portao(imagem: str) -> None:
    """Constroi a imagem do portao a partir de ``docker/caddy`` se ela ainda nao existir nesta maquina."""
    existe = subprocess.run([_docker(), "image", "inspect", imagem], check=False, capture_output=True)  # noqa: S603
    if existe.returncode != 0:
        log.warning("Imagem %s ausente: construindo de docker/caddy (precisa de rede).", imagem)
        subprocess.run([_docker(), "build", "-t", imagem, str(RAIZ / "docker" / "caddy")], check=True)  # noqa: S603


@contextlib.asynccontextmanager
async def subir_portao(portao: Portao, porta_entrada: int, arquivo_log: Path) -> AsyncIterator[tuple[str, str]]:
    """Sobe o Caddy do produto na frente da entrada, com mTLS ate ela; devolve (URL HTTPS, raiz da CA do Caddy)."""
    from testcontainers.core.container import DockerContainer

    await asyncio.to_thread(garantir_imagem_do_portao, portao.imagem)
    conteiner = (
        DockerContainer(portao.imagem)
        .with_env("PUBLIC_HOST", HOST_PUBLICO)
        .with_env("GATEWAY_INTERNAL_PORT", str(porta_entrada))
        .with_env("EDGE_RATE_PER_IP_PER_MINUTE", str(LIMITE_PORTAO_CONFORMIDADE))
        .with_bind_ports(443, None)
        # No Compose a entrada se chama "entrada" na rede interna; aqui ela roda neste computador.
        .with_kwargs(extra_hosts={"entrada": "host-gateway"})
    )
    for nome in ARQUIVOS_DO_PORTAO:
        conteiner = conteiner.with_volume_mapping(portao.segredos / nome, f"/run/secrets/{nome}", "ro")
    # O Caddyfile do repositorio por cima do da imagem: a rodada prova o Caddyfile desta mudanca.
    conteiner = conteiner.with_volume_mapping(RAIZ / "docker" / "caddy" / "Caddyfile", "/etc/caddy/Caddyfile", "ro")
    await asyncio.to_thread(conteiner.start)
    try:
        raiz = await asyncio.to_thread(_esperar_raiz_do_caddy, conteiner)
        porta = int(await asyncio.to_thread(conteiner.get_exposed_port, 443))
        url = f"https://127.0.0.1:{porta}"
        await _esperar_portao(url, raiz)
        yield url, raiz
    finally:
        with contextlib.suppress(Exception):
            saida, erros = await asyncio.to_thread(conteiner.get_logs)
            await asyncio.to_thread(arquivo_log.write_bytes, saida + erros)
        await asyncio.to_thread(conteiner.stop)


def _esperar_raiz_do_caddy(conteiner: Any, prazo: float = 60.0) -> str:
    """O Caddy emite o certificado de ``*.localhost`` pela CA local dele; a raiz fica em /data."""
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        codigo, saida = conteiner.exec(["cat", "/data/caddy/pki/authorities/local/root.crt"])
        if codigo == 0 and b"BEGIN CERTIFICATE" in saida:
            return str(saida.decode("ascii"))
        time.sleep(0.5)
    raise RuntimeError("o Caddy nao criou a autoridade local a tempo")


async def _esperar_portao(url: str, raiz: str, prazo: float = 60.0) -> None:
    """Espera o portao responder pela porta publica, com o certificado do endereco publico."""
    contexto = ssl.create_default_context(cadata=raiz)
    fim = time.monotonic() + prazo
    async with httpx2.AsyncClient(verify=contexto, trust_env=False, timeout=5.0) as http:
        while time.monotonic() < fim:
            try:
                r = await http.get(
                    f"{url}/saude", headers={"host": HOST_PUBLICO}, extensions={"sni_hostname": HOST_PUBLICO}
                )
                if r.status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError("o portao nao chegou a entrada a tempo")


# ---------------------------------------------------------------- banco


@contextlib.contextmanager
def banco_de_teste(pasta: Path) -> Iterator[Parametros]:
    """PostgreSQL 18.7 real num conteiner descartavel, ja migrado; devolve os parametros do dono do esquema."""
    from alembic import command
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    senha = secrets.token_hex(16)
    arquivo = pasta / "db_mcp_claro_migracao"
    arquivo.write_text(senha, encoding="utf-8")
    arquivo.chmod(0o600)
    with PostgresContainer(IMAGEM_POSTGRES, username="mcp_claro_migracao", password=senha, dbname="mcp_claro") as pg:
        url = f"postgresql://mcp_claro_migracao@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/mcp_claro"
        p = Parametros(APP_ENV="teste", DATABASE_URL=url, DATABASE_PASSWORD_FILE=str(arquivo), DB_SSLMODE="disable")
        cfg = Config(str(RAIZ / "alembic.ini"))
        cfg.attributes["parametros"] = p
        command.upgrade(cfg, "head")
        yield p


# ---------------------------------------------------------------- suite e resumo


def _npx() -> str:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("npx nao encontrado: a suite precisa de Node 22 com npx.")
    return npx


async def rodar_suite(url: str, falhas: Path, saida: Path) -> int:
    """Roda a suite fixada contra ``url``; copia a saida para a tela e para ``saida/saida.txt``."""
    cenarios = saida / "cenarios"
    cenarios.mkdir(parents=True, exist_ok=True)
    processo = await asyncio.create_subprocess_exec(
        _npx(),
        "--yes",
        SUITE,
        "server",
        "--url",
        url,
        "--requirements",
        REQUISITOS,
        "--expected-failures",
        str(falhas),
        "--output-dir",
        str(cenarios),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert processo.stdout is not None  # noqa: S101 - pedido com PIPE logo acima
    with (saida / "saida.txt").open("wb") as arquivo:
        async for linha in processo.stdout:
            sys.stdout.buffer.write(linha)
            sys.stdout.buffer.flush()
            arquivo.write(linha)
    return await processo.wait()


def cenarios_exigidos() -> list[str]:
    """Cenarios de servidor que a suite exige na 2026-07-28 (a lista dela, nao uma copia nossa)."""
    resultado = subprocess.run(  # noqa: S603 - comando fixo
        [_npx(), "--yes", SUITE, "list", "--requirements", REQUISITOS], check=True, capture_output=True, text=True
    )
    nomes: list[str] = []
    dentro = False
    for linha in resultado.stdout.splitlines():
        if linha.startswith("Server scenarios"):
            dentro = True
        elif not linha.strip():
            dentro = False
        elif dentro and linha.startswith("  - "):
            nomes.append(linha.split()[1])
    return nomes


def falhas_esperadas(arquivo: Path) -> set[str]:
    """Entradas da lista ``server`` do arquivo de falhas esperadas (cenario ou cenario:verificacao)."""
    entradas: set[str] = set()
    dentro = False
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if linha.startswith("server:"):
            dentro = True
        elif re.match(r"^[A-Za-z]", linha):
            dentro = False
        elif dentro and (m := re.match(r"^\s+-\s+\"?([^\"#\s]+)\"?", linha)):
            entradas.add(m.group(1))
    return entradas


@dataclass(frozen=True, slots=True)
class Resumo:
    passaram: list[str]
    previstos: list[str]
    inesperados: list[str]
    velhas: list[str]
    """Entradas da lista de falhas esperadas que passaram: a suite falha ate alguem apaga-las."""
    linhas: list[str]


def resumir(cenarios: Path, exigidos: list[str], previstas: set[str]) -> Resumo:
    """Conta os cenarios exigidos como a suite decide o codigo: aviso (WARNING) tambem e falha."""
    pastas = [p for p in cenarios.iterdir() if p.is_dir()] if cenarios.is_dir() else []
    passaram: list[str] = []
    previstos: list[str] = []
    inesperados: list[str] = []
    velhas: list[str] = []
    linhas: list[str] = []
    for nome in exigidos:
        padrao = re.compile(rf"^server-{re.escape(nome)}-\d{{4}}-\d{{2}}-\d{{2}}T")
        achadas = sorted(p for p in pastas if padrao.match(p.name))
        checks: list[Mapping[str, Any]] = (
            json.loads((achadas[-1] / "checks.json").read_text(encoding="utf-8")) if achadas else []
        )
        ruins = sorted({str(c["id"]) for c in checks if c.get("status") in ("FAILURE", "WARNING")})
        boas = {str(c["id"]) for c in checks if c.get("status") == "SUCCESS"} - set(ruins)
        velhas += [f"{nome}:{b}" for b in sorted(boas) if f"{nome}:{b}" in previstas]
        if checks and not ruins:
            passaram.append(nome)
            if nome in previstas:
                velhas.append(nome)
            continue
        cobertas = nome in previstas or (bool(ruins) and all(f"{nome}:{r}" in previstas for r in ruins))
        (previstos if cobertas else inesperados).append(nome)
        marca = "previsto" if cobertas else "INESPERADO"
        linhas.append(f"  {marca}: {nome}: {', '.join(ruins) or 'sem resultado'}")
    linhas += [f"  PASSOU (apague da lista de falhas esperadas): {v}" for v in velhas]
    return Resumo(passaram, previstos, inesperados, velhas, linhas)


# ---------------------------------------------------------------- partida


def _logs_em_arquivo(arquivo: Path) -> Any:
    """Logs do gateway vao para um arquivo, para a saida da suite ficar legivel na tela."""
    configurar("INFO")
    fluxo = arquivo.open("a", encoding="utf-8")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=fluxo))
    raiz = logging.getLogger()
    for tratador in list(raiz.handlers):
        raiz.removeHandler(tratador)
    raiz.addHandler(logging.StreamHandler(fluxo))
    return fluxo


async def _principal(args: argparse.Namespace, banco: Parametros, pasta: Path, saida: Path) -> int:
    portao = Portao(args.imagem_portao, gerar_segredos(pasta)) if args.portao else None
    async with subir_arranjo(banco, pasta, portao=portao) as arranjo:
        modo = f"pelo portao {arranjo.url_portao}" if arranjo.url_portao else f"direto na entrada {arranjo.url_entrada}"
        print(f"Gateway no ar {modo}; proxy de teste com cracha em {arranjo.url_mcp}", flush=True)
        print(f"Vitrine da pessoa de teste: {len(arranjo.ferramentas)} ferramentas do fixture.", flush=True)
        if args.so_subir:
            print("Arranjo no ar (--so-subir). Ctrl+C encerra.", flush=True)
            await asyncio.Event().wait()
        return await rodar_suite(arranjo.url_mcp, args.falhas, saida)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m conformidade.gateway",
        description="Roda a suite oficial de conformidade do MCP atraves do gateway MCP-CLARO.",
    )
    parser.add_argument("--portao", action="store_true", help="poe o Caddy do produto na frente da entrada, com mTLS")
    parser.add_argument("--imagem-portao", default=IMAGEM_PORTAO, help="imagem do Caddy do produto (docker/caddy)")
    parser.add_argument("--falhas", type=Path, default=FALHAS_PADRAO, help="lista de falhas esperadas")
    parser.add_argument("--resultados", type=Path, default=RESULTADOS_PADRAO, help="pasta dos resultados")
    parser.add_argument("--so-subir", action="store_true", help="so sobe o arranjo e espera (para explorar a mao)")
    args = parser.parse_args(argv)

    saida = args.resultados / ("portao" if args.portao else "direto")
    shutil.rmtree(saida, ignore_errors=True)
    saida.mkdir(parents=True)
    fluxo = _logs_em_arquivo(saida / "gateway.log")
    try:
        with tempfile.TemporaryDirectory(prefix="conformidade-gateway-") as temporaria:
            pasta = Path(temporaria)
            with banco_de_teste(pasta) as banco:
                try:
                    codigo = asyncio.run(_principal(args, banco, pasta, saida))
                except KeyboardInterrupt:
                    return 130
                with contextlib.suppress(FileNotFoundError):
                    shutil.copy2(pasta / "portao.log", saida / "portao.log")
    finally:
        fluxo.close()

    exigidos = cenarios_exigidos()
    resumo = resumir(saida / "cenarios", exigidos, falhas_esperadas(args.falhas))
    texto = [
        f"Modo: {'atraves do portao (Caddy)' if args.portao else 'direto na entrada'}.",
        f"Cenarios de servidor exigidos pela {REQUISITOS}: {len(exigidos)}.",
        f"  passaram: {len(resumo.passaram)}",
        f"  falharam como previsto: {len(resumo.previstos)}",
        f"  falharam sem previsao: {len(resumo.inesperados)}",
        f"  entradas da lista que ja passam: {len(resumo.velhas)}",
        *resumo.linhas,
        f"Codigo de saida da suite: {codigo}.",
    ]
    (saida / "resumo.txt").write_text("\n".join(texto) + "\n", encoding="utf-8")
    print("\n".join(texto), flush=True)
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
