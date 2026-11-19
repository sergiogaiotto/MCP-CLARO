"""Apoio dos testes da ronda e do vigia: provedor falso de crachas, entrada real e banco do vigia.

- ``FonteFalsa``: faz o papel do provedor de login para a ronda e o vigia, com crachas JWT do
  ``IdpFalso`` (o valido, um vencido de verdade e um de outro condominio).
- ``VerificadorQueAceitaTudo``: a falha plantada, um verificador alterado que aceita qualquer cracha.
- ``subir_entrada``: provedor falso (JWKS) e entrada real, com a rota ``/vigia/fechamentos`` ligada,
  conectando ao banco com o usuario ``mcp_claro_entrada``.
- ``criar_banco_vigia``: um segundo banco no mesmo conteiner, migrado pelas migracoes do vigia.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
import jwt
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import url_asyncpg
from mcp_claro.entrada import app as entrada_app
from mcp_claro.entrada.cracha import ChavesPublicas, Cracha, VerificadorCracha
from mcp_claro.entrada.fechamentos import rotas_fechamentos
from mcp_claro.entrada.servidor import PedidoLeitura
from tests.apoio import porta_livre, servidor
from tests.idp_falso import IdpFalso

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
OUTRO_RECURSO = "https://outro-servico.teste/api"
RONDA = "mcp-claro-ronda"
VIGIA = "mcp-claro-vigia"
ADMIN = "admin-ronda@exemplo.teste"
LIMITE_DA_RONDA_NOS_TESTES = 10000  # PROGRAM_MAX_REQUESTS_PER_MINUTE da entrada nestes testes


def engine_como(p: Parametros, papel: str | None, **extra: Any) -> AsyncEngine:
    """Engine que conecta como o papel do banco (``SET ROLE`` na partida da conexao). ``None``: o dono."""
    ajustes = {"application_name": f"teste-{papel or 'dono'}"} | ({"role": papel} if papel else {})
    return create_async_engine(
        url_asyncpg(p),
        connect_args={"ssl": False, "server_settings": ajustes},
        pool_size=extra.pop("pool_size", 5),
        max_overflow=extra.pop("max_overflow", 5),
        **extra,
    )


@dataclass
class FonteFalsa:
    """``FonteCrachas`` de teste: crachas de verdade do ``IdpFalso`` para um programa."""

    idp: IdpFalso
    client_id: str = RONDA
    com_vencido: bool = True
    com_outro: bool = True
    pedidos: int = 0

    @property
    def sujeito(self) -> str:
        return f"service-account-{self.client_id}"

    def _cracha(self, **kw: Any) -> str:
        return self.idp.cracha(sujeito=self.sujeito, client_id=self.client_id, extra={"scope": "mcp-claro"}, **kw)

    async def valido(self) -> str:
        self.pedidos += 1
        return self._cracha(audiencia=RECURSO)

    async def outro_condominio(self) -> str | None:
        return self._cracha(audiencia=OUTRO_RECURSO) if self.com_outro else None

    def vencido(self) -> str | None:
        return self._cracha(audiencia=RECURSO, validade=-600) if self.com_vencido else None


class VerificadorQueAceitaTudo(VerificadorCracha):
    """Falha plantada: le o cracha sem conferir assinatura, validade, emissor nem audiencia."""

    async def verificar(self, token: str) -> Cracha:
        dados = jwt.decode(token, options={"verify_signature": False})
        return Cracha(
            client_id=str(dados.get("azp") or dados.get("client_id") or "desconhecido"),
            sujeito=str(dados.get("sub") or "desconhecido"),
            emissor=str(dados.get("iss") or "desconhecido"),
            escopos=frozenset(str(dados.get("scope", "")).split()),
            expira_em=int(dados.get("exp", 0)),
        )


async def leitor_falso(_: PedidoLeitura) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "leitura de teste"}], "isError": False}


@dataclass
class Entrada:
    p: Parametros
    idp: IdpFalso
    url: str
    dono: AsyncEngine
    entrada_db: AsyncEngine
    rotinas_db: AsyncEngine
    porta: int
    ferramenta_ana: str
    parar: Callable[[], Awaitable[None]] = field(repr=False, default=lambda: _nada())

    def cracha(self, **kw: Any) -> str:
        kw.setdefault("audiencia", RECURSO)
        return self.idp.cracha(**kw)


async def _nada() -> None:
    return None


@contextlib.asynccontextmanager
async def servidor_paravel(app: Any, porta: int) -> AsyncIterator[tuple[str, Callable[[], Awaitable[None]]]]:
    """Como ``tests.apoio.servidor``, mas devolve tambem como parar o servidor no meio do teste."""
    config = uvicorn.Config(app, host="127.0.0.1", port=porta, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    tarefa = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.02)

    async def parar() -> None:
        srv.should_exit = True
        await tarefa

    try:
        yield f"http://127.0.0.1:{porta}", parar
    finally:
        await parar()


def parametros_entrada(banco: Parametros, idp: IdpFalso, porta_idp: int) -> Parametros:
    return Parametros(
        **(
            banco.model_dump()
            | {
                "APP_ENV": "teste",
                "PUBLIC_BASE_URL": PUBLICO,
                "MCP_RESOURCE_ID": None,
                "WATCH_PUBLIC_URL": None,
                "ALLOWED_HOSTS": ["127.0.0.1:*"],
                "IDP_KIND": "oidc",
                "IDP_ISSUER_URL": idp.emissor,
                "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
                "PATROL_FAILS_TO_CLOSE": 2,
                # A ronda e o vigia sao programas: cada pedido deles conta no limite por minuto. Varias rodadas no
                # mesmo minuto, nos testes, passariam do padrao (30), e o controle no limite e inconclusivo. O teste
                # do limite enche a contagem de proposito (test_ronda_no_limite_por_minuto_e_inconclusiva).
                "PROGRAM_MAX_REQUESTS_PER_MINUTE": LIMITE_DA_RONDA_NOS_TESTES,
            }
        )
    )


async def cadastrar(dono: AsyncEngine, idp: IdpFalso) -> str:
    """Programas da ronda e do vigia (pessoas de teste, sem ordem) e Ana com uma ferramenta na vitrine dela."""
    sufixo = uuid.uuid4().hex[:8]
    async with dono.begin() as c:
        responsavel = await repositorio.pessoa_no_login(c, idp.emissor, "responsavel-ronda", "Responsavel da ronda")
        substituto = await repositorio.pessoa_no_login(c, idp.emissor, "substituto-ronda", "Substituto da ronda")
        for client_id in (RONDA, VIGIA):
            app = await repositorio.habilitar_aplicativo(
                c, client_id=client_id, nome=client_id, familia="mcp-claro", tipo="programa"
            )
            await c.execute(text("UPDATE aplicativos SET situacao = 'ativo' WHERE client_id = :c"), {"c": client_id})
            # Fase 2 (decisao 9): programa so passa com cadastro valendo, com responsavel e substituto.
            await c.execute(
                text(
                    "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim)"
                    " VALUES (:a, :r, :s, 'Ronda das portas do MCP-CLARO.', current_date + 30)"
                    " ON CONFLICT (aplicativo_id) DO UPDATE SET situacao = 'ativo', fim = EXCLUDED.fim,"
                    " responsavel_id = EXCLUDED.responsavel_id, substituto_id = EXCLUDED.substituto_id"
                ),
                {"a": app, "r": responsavel.id, "s": substituto.id},
            )
            programa = await repositorio.pessoa_no_login(c, idp.emissor, f"service-account-{client_id}", None)
            await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": programa.id})
        app_id = await repositorio.habilitar_aplicativo(
            c, client_id="claude-code", nome="Claude Code", familia="claude-code"
        )
        ana = await repositorio.pessoa_no_login(c, idp.emissor, "pessoa-ana", "Ana")
        await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": ana.id})
        sistema_id = await repositorio.cadastrar_sistema(
            c, nome=f"ronda-exemplo-{sufixo}", endereco="https://sistema.exemplo/mcp", tipo_chave="por_pessoa"
        )
        await c.execute(
            text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2026-07-28' WHERE id = :s"), {"s": sistema_id}
        )
        nome = f"fatura_ana_{sufixo}"
        await repositorio.revisar_ferramenta(
            c,
            sistema_id=sistema_id,
            definicao_declarada={
                "name": "consultar_fatura",
                "description": "Fatura do mes.",
                "inputSchema": {"type": "object", "properties": {"mes": {"type": "string"}}, "required": ["mes"]},
            },
            nome_vitrine=nome,
            descricao="Mostra a sua fatura de um mes.",
            classe="le",
            revisado_por="admin-teste",
        )
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"ana-{sufixo}",
            descricao="Ordem da Ana",
            ferramentas=[(sistema_id, "consultar_fatura")],
            validade_dias=30,
            criado_por="admin-teste",
        )
        await repositorio.conceder_ordem(
            c, pessoa_id=ana.id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
        )
    return nome


async def abrir_entrada(dono: AsyncEngine) -> None:
    """Deixa a entrada aberta, como decisao do teste (o banco e de todos os testes da sessao)."""
    async with dono.begin() as c:
        await c.execute(
            text(
                "UPDATE estado_entrada SET nivel = 'aberta', motivo = 'teste', mudou_em = clock_timestamp(),"
                " mudou_por = 'instalacao' WHERE id = 1"
            )
        )


@contextlib.asynccontextmanager
async def subir_entrada(
    banco: Parametros, *, planta: type[VerificadorCracha] | None = None, porta: int | None = None
) -> AsyncIterator[Entrada]:
    """Provedor falso e entrada real (usuario ``mcp_claro_entrada``), com a rota dos fechamentos."""
    porta_idp = porta_livre()
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    p = parametros_entrada(banco, idp, porta_idp)
    dono = engine_como(p, None)
    entrada_db = engine_como(p, "mcp_claro_entrada")
    rotinas_db = engine_como(p, "mcp_claro_rotinas")
    ferramenta = await cadastrar(dono, idp)
    await abrir_entrada(dono)
    http_login = httpx2.AsyncClient(timeout=5)
    porta_entrada = porta or porta_livre()
    try:
        async with servidor(idp.app(), porta_idp):
            with pytest.MonkeyPatch.context() as mp:
                if planta is not None:
                    mp.setattr(entrada_app, "VerificadorCracha", planta)
                app = entrada_app.criar_app_entrada(p, entrada_db, leitor=leitor_falso)
                verificador = (planta or VerificadorCracha)(
                    p, ChavesPublicas(entrada_app.url_jwks(p), cache_segundos=300, http=http_login)
                )
            app.router.routes[0:0] = rotas_fechamentos(p, entrada_db, verificador)
            async with servidor_paravel(app, porta_entrada) as (url, parar):
                yield Entrada(p, idp, url, dono, entrada_db, rotinas_db, porta_entrada, ferramenta, parar)
    finally:
        await abrir_entrada(dono)
        await http_login.aclose()
        for engine in (dono, entrada_db, rotinas_db):
            await engine.dispose()


# --- Banco do vigia: segundo banco no mesmo conteiner ------------------------------------------


def _com_banco(p: Parametros, banco: str) -> Parametros:
    partes = urlsplit(p.DATABASE_URL)
    url = urlunsplit((partes.scheme, partes.netloc, f"/{banco}", "", ""))
    return Parametros(**(p.model_dump() | {"DATABASE_URL": url}))


def criar_banco_vigia(banco: Parametros) -> Parametros:
    """Cria ``mcp_claro_vigia_<sufixo>`` no mesmo PostgreSQL e aplica as migracoes do vigia."""
    import asyncpg

    from mcp_claro.vigia.migrar import migrar_vigia

    nome = f"mcp_claro_vigia_{uuid.uuid4().hex[:8]}"
    partes = urlsplit(banco.DATABASE_URL)
    senha = banco.senha_banco.get_secret_value() if banco.senha_banco else None

    async def criar() -> None:
        conexao = await asyncpg.connect(
            user=partes.username,
            password=senha,
            host=partes.hostname,
            port=partes.port,
            database=partes.path.lstrip("/"),
            ssl=False,
        )
        try:
            await conexao.execute(f'CREATE DATABASE "{nome}"')
        finally:
            await conexao.close()

    asyncio.run(criar())
    p = _com_banco(banco, nome)
    migrar_vigia(p)
    return p
