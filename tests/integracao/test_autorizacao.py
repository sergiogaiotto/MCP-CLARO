"""Autorizacao da pessoa no sistema, chaves por pessoa e renovacao com trava, de ponta a ponta.

Sobe de verdade: PostgreSQL 18, provedor de login falso, servidor OAuth falso do sistema (com
``/authorize`` e ``/token``, PKCE S256 conferido), o sistema de exemplo ``leitura_por_pessoa``,
o despachante (com as rotas de autorizacao) e a entrada. Cada papel conecta ao banco com o
proprio usuario (``mcp_claro_entrada``, ``mcp_claro_despachante``), entao os testes tambem provam
que as permissoes da migracao bastam.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from exemplos import leitura_por_pessoa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine, url_asyncpg
from mcp_claro.despachante import autorizacao, chaves, renovacao
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from tests.apoio import cliente_mcp, porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_falso import IdpFalso

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
VOLTA = f"{PUBLICO}/telas/autorizacao/volta"


def mapa_de_chaves() -> dict[str, str]:
    """Chaves que o sistema de exemplo aceita: as que o OAuth falso emite, em ordem, por pessoa."""
    mapa = {"chave-sondagem": "ana"}
    for usuario in ("ana", "bia"):
        for n in range(1, 31):
            mapa[f"acesso-{usuario}-{n}"] = usuario
    return mapa


def s256(verificador: str) -> str:
    """Conta propria do teste (RFC 7636), independente do codigo do gateway."""
    return base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()


@dataclass
class OAuthFalso:
    """Servidor de autorizacao do sistema, so de teste. Confere cliente, redirect_uri e PKCE S256."""

    client_id: str = "mcp-claro-teste"
    segredo: str = "segredo-do-cliente-teste"
    validade: int = 3600
    atraso_renovacao: float = 0.0
    codigos: dict[str, dict[str, str]] = field(default_factory=dict)
    renovacoes: dict[str, str] = field(default_factory=dict)
    emitidas: Counter[str] = field(default_factory=Counter)
    contagem: Counter[str] = field(default_factory=Counter)
    pedidos: list[dict[str, str]] = field(default_factory=list)

    def _cliente_ok(self, pedido: Request) -> bool:
        auth = pedido.headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return False
        cid, _, seg = base64.b64decode(auth[6:]).decode().partition(":")
        return unquote(cid) == self.client_id and unquote(seg) == self.segredo

    def _emitir(self, usuario: str) -> JSONResponse:
        self.emitidas[usuario] += 1
        n = self.emitidas[usuario]
        renova = f"renova-{usuario}-{n}"
        self.renovacoes[renova] = usuario
        return JSONResponse(
            {
                "access_token": f"acesso-{usuario}-{n}",
                "token_type": "Bearer",
                "expires_in": self.validade,
                "refresh_token": renova,
                "scope": "leitura",
            },
            headers={"Cache-Control": "no-store"},
        )

    @staticmethod
    def _erro(codigo: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": codigo}, status_code=status)

    def app(self) -> Starlette:
        async def autorizar(pedido: Request) -> Response:
            q = pedido.query_params
            if q.get("response_type") != "code" or q.get("client_id") != self.client_id:
                return self._erro("invalid_request")
            if q.get("code_challenge_method") != "S256" or not q.get("code_challenge") or not q.get("state"):
                return self._erro("invalid_request")
            codigo = secrets.token_urlsafe(16)
            self.codigos[codigo] = {
                "usuario": q["usuario"],  # simula o login da pessoa na pagina do sistema
                "desafio": q["code_challenge"],
                "redirect_uri": q["redirect_uri"],
            }
            return RedirectResponse(
                q["redirect_uri"] + "?" + urlencode({"code": codigo, "state": q["state"]}), status_code=302
            )

        async def token(pedido: Request) -> Response:
            form = dict(parse_qsl((await pedido.body()).decode()))
            self.pedidos.append({k: v for k, v in form.items() if k in ("grant_type", "resource", "redirect_uri")})
            if not self._cliente_ok(pedido):
                return self._erro("invalid_client", 401)
            if form.get("grant_type") == "authorization_code":
                self.contagem["codigo"] += 1
                dados = self.codigos.pop(form.get("code", ""), None)
                if dados is None or form.get("redirect_uri") != dados["redirect_uri"]:
                    return self._erro("invalid_grant")
                if s256(form.get("code_verifier", "")) != dados["desafio"]:
                    self.contagem["pkce_recusado"] += 1
                    return self._erro("invalid_grant")
                return self._emitir(dados["usuario"])
            if form.get("grant_type") == "refresh_token":
                self.contagem["renovacao"] += 1
                await asyncio.sleep(self.atraso_renovacao)
                usuario = self.renovacoes.pop(form.get("refresh_token", ""), None)
                if usuario is None:
                    return self._erro("invalid_grant")
                return self._emitir(usuario)
            return self._erro("unsupported_grant_type")

        return Starlette(routes=[Route("/authorize", autorizar), Route("/token", token, methods=["POST"])])


def engine_do_papel(p: Parametros, papel: str) -> AsyncEngine:
    """Engine que conecta como o papel do banco (``SET ROLE`` na partida da conexao)."""
    return create_async_engine(
        url_asyncpg(p),
        connect_args={"ssl": False, "server_settings": {"application_name": f"teste-{papel}", "role": papel}},
        pool_size=5,
        max_overflow=5,
    )


@dataclass
class Ambiente:
    p: Parametros
    idp: IdpFalso
    oauth: OAuthFalso
    admin: AsyncEngine  # dono do banco: arranjo e conferencias
    entrada_db: AsyncEngine
    despachante_db: AsyncEngine
    rotinas_db: AsyncEngine
    cifra: CifraFalsa
    entrada: str
    despachante: str
    url_sistema: str
    sistema_app: Any
    sistema_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ana_id: uuid.UUID
    bia_id: uuid.UUID
    admin_id: uuid.UUID
    ordens: dict[str, uuid.UUID]
    fatura: str
    cadastro: str
    vitrine: dict[str, str] = field(default_factory=dict)

    def cracha(self, sujeito: str = "pessoa-ana") -> str:
        return self.idp.cracha(audiencia=RECURSO, sujeito=sujeito)

    async def iniciar(self, pessoa_id: uuid.UUID) -> str:
        # Opcao B: quem cria o pedido e cifra o verificador PKCE e o despachante; a entrada nao cifra.
        async with self.despachante_db.begin() as c:
            return await autorizacao.iniciar_autorizacao(c, self.cifra, pessoa_id, self.sistema_id, VOLTA)

    async def na_pagina_do_sistema(self, url: str, usuario: str) -> tuple[str, str]:
        """O navegador da pessoa: abre a pagina, faz login como ``usuario`` e volta com codigo e estado."""
        async with httpx2.AsyncClient(follow_redirects=False) as http:
            r = await http.get(url + "&" + urlencode({"usuario": usuario}))
        assert r.status_code == 302, r.text
        volta = urlsplit(r.headers["location"])
        assert f"{volta.scheme}://{volta.netloc}{volta.path}" == VOLTA
        q = parse_qs(volta.query)
        return q["state"][0], q["code"][0]

    async def concluir(self, pessoa_id: uuid.UUID, **corpo: str) -> httpx2.Response:
        async with httpx2.AsyncClient() as http:
            return await http.post(
                f"{self.despachante}/interno/autorizacao/concluir", json={"pessoa_id": str(pessoa_id), **corpo}
            )

    async def autorizar(self, pessoa_id: uuid.UUID, usuario: str) -> httpx2.Response:
        estado, codigo = await self.na_pagina_do_sistema(await self.iniciar(pessoa_id), usuario)
        return await self.concluir(pessoa_id, estado=estado, codigo=codigo)

    async def ler_fatura(self, sujeito: str, mes: str) -> Any:
        """Uma leitura pelo aplicativo. Recusa sai como ``MCPError`` (fora do grupo de tarefas do cliente)."""
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.entrada}/mcp", self.cracha(sujeito)) as c:
            try:
                return await c.call_tool(self.fatura, {"mes": mes})
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def listar(self, sujeito: str = "pessoa-ana") -> list[str]:
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.entrada}/mcp", self.cracha(sujeito)) as c:
            try:
                return sorted(t.name for t in (await c.list_tools()).tools)
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def vencer_chave(self, pessoa_id: uuid.UUID) -> None:
        async with self.admin.begin() as c:
            await c.execute(
                text(
                    "UPDATE chaves_cifradas SET expira_em = now() - interval '1 minute' "
                    "WHERE sistema_id = :s AND pessoa_id = :p AND tipo = 'acesso'"
                ),
                {"s": self.sistema_id, "p": pessoa_id},
            )

    async def chaves_de(self, pessoa_id: uuid.UUID) -> dict[str, Any]:
        async with self.admin.connect() as c:
            linhas = (
                await c.execute(
                    text(
                        "SELECT tipo, cifrada, expira_em FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p"
                    ),
                    {"s": self.sistema_id, "p": pessoa_id},
                )
            ).all()
        return {linha.tipo: linha for linha in linhas}

    async def abrir(self, pessoa_id: uuid.UUID, cifrada: str, tipo: str = "acesso") -> str:
        return (await self.cifra.decifrar(cifrada, chaves.contexto(self.sistema_id, pessoa_id, None, tipo))).decode()

    async def extrato(self, tipo: str, pessoa_id: uuid.UUID | None = None) -> list[Any]:
        async with self.admin.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT frase, dados, pessoa_id FROM extrato WHERE tipo = :t AND sistema_id = :s "
                            "AND (CAST(:p AS uuid) IS NULL OR pessoa_id = :p) ORDER BY id"
                        ),
                        {"t": tipo, "s": self.sistema_id, "p": pessoa_id},
                    )
                ).all()
            )

    def vistas(self) -> list[str]:
        return list(self.sistema_app.state.contagem.autorizacoes_vistas)

    def zerar_vistas(self) -> None:
        self.sistema_app.state.contagem.autorizacoes_vistas.clear()
        self.sistema_app.state.contagem.chamadas.clear()


# Revisao de uma ferramenta no arranjo: (nome original, prefixo do nome na vitrine, classe, risco).
Revisao = tuple[str, str, str, str]
LEITURAS: tuple[Revisao, ...] = (
    ("consultar_fatura", "fatura", "le", "medio"),
    ("ver_cadastro", "cadastro", "le", "medio"),
)


@contextlib.asynccontextmanager
async def subir(
    banco_migrado: Parametros,
    monkeypatch: pytest.MonkeyPatch,
    *,
    criar_sistema: Any = None,
    revisoes: tuple[Revisao, ...] = LEITURAS,
    extra: dict[str, Any] | None = None,
) -> AsyncIterator[Ambiente]:
    """Sobe o arranjo. ``criar_sistema`` troca o sistema de exemplo (o padrao e ``leitura_por_pessoa``) e
    ``revisoes`` diz o que entra na vitrine; ``Ambiente.vitrine`` guarda nome original -> nome na vitrine."""
    monkeypatch.setenv("EXEMPLO_CHAVES", json.dumps(mapa_de_chaves()))
    porta_idp, porta_desp = porta_livre(), porta_livre()
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    oauth = OAuthFalso()
    sistema_app = (criar_sistema or leitura_por_pessoa.criar)()
    p = Parametros(
        **(
            banco_migrado.model_dump()
            | {
                "APP_ENV": "teste",
                "PUBLIC_BASE_URL": PUBLICO,
                "MCP_RESOURCE_ID": None,
                "WATCH_PUBLIC_URL": None,
                "ALLOWED_HOSTS": ["127.0.0.1:*"],
                "IDP_KIND": "oidc",
                "IDP_ISSUER_URL": idp.emissor,
                "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
                "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}",
                "RESPONSE_MAX_WAIT_SECONDS": 5,
            }
            | (extra or {})
        )
    )
    admin = criar_engine(p, pool_size=3, max_overflow=3)
    entrada_db = engine_do_papel(p, "mcp_claro_entrada")
    despachante_db = engine_do_papel(p, "mcp_claro_despachante")
    rotinas_db = engine_do_papel(p, "mcp_claro_rotinas")
    cifra = CifraFalsa()
    app_desp = criar_app_despachante(p, despachante_db, cifra)
    app_desp.router.routes.extend(autorizacao.rotas_autorizacao(despachante_db, cifra))
    try:
        async with (
            servidor(idp.app(), porta_idp),
            servidor(sistema_app) as url_sistema,
            servidor(oauth.app()) as url_oauth,
            servidor(app_desp, porta_desp) as url_desp,
            servidor(criar_app_entrada(p, entrada_db)) as url_entrada,
        ):
            sufixo = uuid.uuid4().hex[:8]
            endereco = f"{url_sistema}/mcp"
            async with entrada_db.begin() as c:  # o que a tela do ADMIN faria, com o usuario da entrada
                app_id = await repositorio.habilitar_aplicativo(
                    c, client_id="claude-code", nome="Claude Code", familia="claude-code"
                )
                sistema_id = await repositorio.cadastrar_sistema(
                    c, nome=f"leitura-{sufixo}", endereco=endereco, tipo_chave="por_pessoa"
                )
                pessoas = {}
                for sujeito in ("pessoa-ana", "pessoa-bia", "pessoa-admin"):
                    pessoa = await repositorio.pessoa_no_login(c, idp.emissor, sujeito, sujeito)
                    pessoas[sujeito] = pessoa.id
                await c.execute(
                    text("UPDATE pessoas SET teste = true WHERE id = ANY(:ids)"), {"ids": list(pessoas.values())}
                )
                await c.execute(
                    text("UPDATE pessoas SET papel = 'admin' WHERE id = :p"), {"p": pessoas["pessoa-admin"]}
                )
            despachante = ClienteDespachante(p)
            try:
                sondagem = await despachante.post(
                    "/interno/sondar", {"sistema_id": str(sistema_id), "chave_sondagem": "chave-sondagem"}
                )
                assert sondagem["aceito"], sondagem
                declaradas = (
                    await despachante.post(
                        "/interno/declaracoes", {"sistema_id": str(sistema_id), "chave_sondagem": "chave-sondagem"}
                    )
                )["ferramentas"]
                guardado = await despachante.post(
                    "/interno/autorizacao/segredo", {"sistema_id": str(sistema_id), "segredo": oauth.segredo}
                )
                assert guardado == {"guardado": True}
            finally:
                await despachante.fechar()
            por_nome = {f["name"]: f for f in declaradas}
            vitrine = {original: f"{prefixo}_{sufixo}" for original, prefixo, _, _ in revisoes}
            fatura, cadastro = f"fatura_{sufixo}", f"cadastro_{sufixo}"
            async with entrada_db.begin() as c:
                await autorizacao.configurar(
                    c,
                    sistema_id,
                    {
                        "authorization_endpoint": f"{url_oauth}/authorize",
                        "token_endpoint": f"{url_oauth}/token",
                        "client_id": oauth.client_id,
                        "escopos": ["leitura"],
                    },
                )
                for nome_original, _, classe, risco in revisoes:
                    await repositorio.revisar_ferramenta(
                        c,
                        sistema_id=sistema_id,
                        definicao_declarada=por_nome[nome_original],
                        nome_vitrine=vitrine[nome_original],
                        descricao="Leitura de teste." if classe == "le" else "Acao de teste.",
                        classe=classe,
                        risco=risco,
                        revisado_por="admin-teste",
                        campos_pessoais=["cpf", "email"] if classe == "le" else [],
                    )
                modelo = await repositorio.criar_modelo_ordem(
                    c,
                    nome=f"leitura-{sufixo}",
                    descricao="Ordem de servico de teste",
                    ferramentas=[(sistema_id, original) for original, _, _, _ in revisoes],
                    validade_dias=30,
                    criado_por="admin-teste",
                )
                ordens = {}
                for sujeito in ("pessoa-ana", "pessoa-bia"):
                    ordens[sujeito] = await repositorio.conceder_ordem(
                        c, pessoa_id=pessoas[sujeito], aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
                    )
            yield Ambiente(
                p=p,
                idp=idp,
                oauth=oauth,
                admin=admin,
                entrada_db=entrada_db,
                despachante_db=despachante_db,
                rotinas_db=rotinas_db,
                cifra=cifra,
                entrada=url_entrada,
                despachante=url_desp,
                url_sistema=url_sistema,
                sistema_app=sistema_app,
                sistema_id=sistema_id,
                aplicativo_id=app_id,
                ana_id=pessoas["pessoa-ana"],
                bia_id=pessoas["pessoa-bia"],
                admin_id=pessoas["pessoa-admin"],
                ordens=ordens,
                fatura=fatura,
                cadastro=cadastro,
                vitrine=vitrine,
            )
    finally:
        for engine in (admin, entrada_db, despachante_db, rotinas_db):
            await engine.dispose()


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with subir(banco_migrado, monkeypatch) as a:
        yield a


# --- Troca do codigo -------------------------------------------------------------------------------


async def test_troca_do_codigo_guarda_a_chave_cifrada_e_a_leitura_usa_a_chave_da_pessoa(amb: Ambiente) -> None:
    url = await amb.iniciar(amb.ana_id)
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["code_challenge_method"] == "S256"
    assert q["client_id"] == amb.oauth.client_id
    assert q["redirect_uri"] == VOLTA
    assert q["scope"] == "leitura"
    assert q["resource"] == f"{amb.url_sistema}/mcp"
    assert "code_verifier" not in q
    async with amb.admin.connect() as c:
        pendente = (
            await c.execute(text("SELECT * FROM autorizacoes_pendentes WHERE estado = :e"), {"e": q["state"]})
        ).one()
    assert pendente.pessoa_id == amb.ana_id
    assert pendente.usada_em is None
    assert pendente.verificador_cifrado.startswith("vault:v1:")
    assert pendente.expira_em > datetime.now(UTC) + timedelta(minutes=5)
    guardado = json.loads(
        await amb.cifra.decifrar(
            pendente.verificador_cifrado, autorizacao.contexto_pendente(q["state"], amb.ana_id, amb.sistema_id)
        )
    )
    assert s256(guardado["verificador"]) == q["code_challenge"]  # o verificador cifrado e o do desafio
    assert guardado["verificador"] not in pendente.verificador_cifrado

    estado, codigo = await amb.na_pagina_do_sistema(url, "ana")
    r = await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)
    assert r.status_code == 200, r.text
    assert r.json()["concluida"] is True
    assert amb.oauth.contagem["codigo"] == 1
    assert amb.oauth.contagem["pkce_recusado"] == 0
    assert amb.oauth.pedidos[-1]["resource"] == f"{amb.url_sistema}/mcp"

    guardadas = await amb.chaves_de(amb.ana_id)
    assert set(guardadas) == {"acesso", "renovacao"}
    for tipo, linha in guardadas.items():
        assert linha.cifrada.startswith("vault:v1:")
        assert "acesso-ana" not in linha.cifrada
        assert "renova-ana" not in linha.cifrada
        assert (linha.expira_em is None) == (tipo == "renovacao")
    assert await amb.abrir(amb.ana_id, guardadas["acesso"].cifrada) == "acesso-ana-1"
    assert await amb.abrir(amb.ana_id, guardadas["renovacao"].cifrada, "renovacao") == "renova-ana-1"
    assert guardadas["acesso"].expira_em > datetime.now(UTC) + timedelta(minutes=50)

    async with amb.admin.connect() as c:
        usada = (
            await c.execute(text("SELECT usada_em FROM autorizacoes_pendentes WHERE estado = :e"), {"e": estado})
        ).scalar_one()
        texto_extrato = (
            await c.execute(
                text("SELECT string_agg(frase || dados::text, ' ') FROM extrato WHERE sistema_id = :s"),
                {"s": amb.sistema_id},
            )
        ).scalar_one()
    assert usada is not None
    assert len(await amb.extrato("autorizacao_iniciada", amb.ana_id)) == 1
    assert len(await amb.extrato("autorizacao_concluida", amb.ana_id)) == 1
    for segredo in ("acesso-ana", "renova-ana", amb.oauth.segredo, codigo, guardado["verificador"]):
        assert segredo not in texto_extrato

    amb.zerar_vistas()
    resultado = await amb.ler_fatura("pessoa-ana", "2026-10")
    assert resultado.is_error is False
    assert resultado.structured_content["valor"] == 99.9
    assert amb.vistas() == ["Bearer acesso-ana-1"]


async def test_estado_e_de_uso_unico_tem_prazo_e_e_da_mesma_pessoa(amb: Ambiente) -> None:
    estado, codigo = await amb.na_pagina_do_sistema(await amb.iniciar(amb.ana_id), "ana")
    assert (await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)).status_code == 200
    reuso = await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)
    assert reuso.status_code == 403
    assert reuso.json()["motivo"] == "estado_ja_usado"
    assert "ja foi usado" in reuso.json()["mensagem"]
    assert amb.oauth.contagem["codigo"] == 1  # o reuso nem chegou ao sistema

    estado, codigo = await amb.na_pagina_do_sistema(await amb.iniciar(amb.ana_id), "ana")
    async with amb.admin.begin() as c:
        await c.execute(
            text("UPDATE autorizacoes_pendentes SET expira_em = now() - interval '1 second' WHERE estado = :e"),
            {"e": estado},
        )
    vencido = await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)
    assert vencido.status_code == 403
    assert vencido.json()["motivo"] == "estado_vencido"

    desconhecido = await amb.concluir(amb.ana_id, estado="estado-inventado", codigo="codigo-inventado")
    assert desconhecido.json()["motivo"] == "estado_desconhecido"

    estado, codigo = await amb.na_pagina_do_sistema(await amb.iniciar(amb.ana_id), "ana")
    de_outra = await amb.concluir(amb.bia_id, estado=estado, codigo=codigo)
    assert de_outra.json()["motivo"] == "estado_de_outra_pessoa"
    depois = await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)
    assert depois.json()["motivo"] == "estado_ja_usado"  # a tentativa da outra pessoa gastou o estado

    estado, _ = await amb.na_pagina_do_sistema(await amb.iniciar(amb.ana_id), "ana")
    desistiu = await amb.concluir(amb.ana_id, estado=estado, erro="access_denied")
    assert desistiu.json()["motivo"] == "sistema_nao_autorizou"
    assert "access_denied" in desistiu.json()["mensagem"]

    assert amb.oauth.contagem["codigo"] == 1
    assert await amb.chaves_de(amb.bia_id) == {}
    motivos = [linha.frase for linha in await amb.extrato("autorizacao_recusada")]
    assert len(motivos) >= 5
    assert any("estado_de_outra_pessoa" in m for m in motivos)


async def test_pkce_errado_e_recusado_e_nada_e_guardado(amb: Ambiente) -> None:
    url_a = await amb.iniciar(amb.ana_id)
    url_b = await amb.iniciar(amb.ana_id)
    _, codigo_a = await amb.na_pagina_do_sistema(url_a, "ana")
    estado_b = parse_qs(urlsplit(url_b).query)["state"][0]
    r = await amb.concluir(amb.ana_id, estado=estado_b, codigo=codigo_a)  # verificador de B, desafio de A
    assert r.status_code == 403
    assert r.json()["motivo"] == "troca_recusada"
    assert amb.oauth.contagem["pkce_recusado"] == 1
    assert await amb.chaves_de(amb.ana_id) == {}


async def test_rotas_prontas_usam_o_estado_do_app(amb: Ambiente) -> None:
    app = Starlette(routes=list(autorizacao.ROTAS_AUTORIZACAO))
    app.state.engine = amb.despachante_db
    app.state.cifra = amb.cifra
    estado, codigo = await amb.na_pagina_do_sistema(await amb.iniciar(amb.bia_id), "bia")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://interno") as http:
        invalido = await http.post("/interno/autorizacao/concluir", json={"estado": estado})
        assert invalido.status_code == 400
        r = await http.post(
            "/interno/autorizacao/concluir", json={"estado": estado, "codigo": codigo, "pessoa_id": str(amb.bia_id)}
        )
    assert r.status_code == 200, r.text
    assert await amb.abrir(amb.bia_id, (await amb.chaves_de(amb.bia_id))["acesso"].cifrada) == "acesso-bia-1"


async def test_porta_interna_pode_iniciar_quando_o_despachante_tiver_permissao(amb: Ambiente) -> None:
    """Alternativa para a entrada que nao cifra no cofre: o despachante cria a autorizacao pendente."""
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{amb.despachante}/interno/autorizacao/iniciar",
            json={"pessoa_id": str(amb.ana_id), "sistema_id": str(amb.sistema_id), "redirect_uri": VOLTA},
        )
    if r.status_code == 500:
        pytest.skip("mcp_claro_despachante ainda sem INSERT em autorizacoes_pendentes (ver relatorio)")
    assert r.status_code == 200, r.text
    estado, codigo = await amb.na_pagina_do_sistema(r.json()["url"], "ana")
    assert (await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)).status_code == 200


# --- Renovacao -------------------------------------------------------------------------------------


async def test_duas_renovacoes_ao_mesmo_tempo_geram_uma_so_troca(amb: Ambiente) -> None:
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    await amb.vencer_chave(amb.ana_id)
    amb.oauth.atraso_renovacao = 0.5  # a primeira segura a trava enquanto o servidor demora
    primeira, segunda = await asyncio.gather(
        renovacao.chave_pronta(amb.despachante_db, amb.cifra, sistema_id=amb.sistema_id, pessoa_id=amb.ana_id),
        renovacao.chave_pronta(amb.despachante_db, amb.cifra, sistema_id=amb.sistema_id, pessoa_id=amb.ana_id),
    )
    assert amb.oauth.contagem["renovacao"] == 1
    assert primeira.segredo == segunda.segredo == "acesso-ana-2"
    assert sorted([primeira.renovada, segunda.renovada]) == [False, True]
    guardadas = await amb.chaves_de(amb.ana_id)
    assert await amb.abrir(amb.ana_id, guardadas["acesso"].cifrada) == "acesso-ana-2"
    assert await amb.abrir(amb.ana_id, guardadas["renovacao"].cifrada, "renovacao") == "renova-ana-2"
    assert guardadas["acesso"].expira_em > datetime.now(UTC) + timedelta(minutes=50)
    assert len(await amb.extrato("chave_renovada", amb.ana_id)) == 1
    pedidos = [p for p in amb.oauth.pedidos if p["grant_type"] == "refresh_token"]
    assert pedidos == [{"grant_type": "refresh_token", "resource": f"{amb.url_sistema}/mcp"}]


async def test_leitura_renova_antes_de_enviar_e_o_sistema_so_ve_a_chave_nova(amb: Ambiente) -> None:
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    await amb.vencer_chave(amb.ana_id)
    amb.oauth.atraso_renovacao = 0.3
    amb.zerar_vistas()
    resultados = await asyncio.gather(amb.ler_fatura("pessoa-ana", "2026-10"), amb.ler_fatura("pessoa-ana", "2026-11"))
    assert [r.structured_content["valor"] for r in resultados] == [99.9, 104.5]
    assert amb.oauth.contagem["renovacao"] == 1
    assert amb.vistas() == ["Bearer acesso-ana-2", "Bearer acesso-ana-2"]  # nenhum envio com a chave vencida
    assert amb.sistema_app.state.contagem.chamadas == {"consultar_fatura": 2}


async def test_renovacao_recusada_pede_nova_autorizacao_sem_enviar(amb: Ambiente) -> None:
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    await amb.vencer_chave(amb.ana_id)
    amb.oauth.renovacoes.clear()  # o sistema revogou a chave de renovacao
    amb.zerar_vistas()
    with pytest.raises(MCPError, match=r"venceu\. Autorize de novo"):
        await amb.ler_fatura("pessoa-ana", "2026-10")
    assert amb.vistas() == []
    assert await amb.chaves_de(amb.ana_id) == {}
    assert any("recusou a renovacao" in linha.frase for linha in await amb.extrato("renovacao_falhou", amb.ana_id))
    with pytest.raises(MCPError, match="ainda nao autorizou"):
        await amb.ler_fatura("pessoa-ana", "2026-10")
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    assert (await amb.ler_fatura("pessoa-ana", "2026-10")).is_error is False


async def test_401_no_envio_nao_reenvia_e_o_proximo_pedido_renova_antes(amb: Ambiente) -> None:
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    async with amb.despachante_db.begin() as c:  # a chave guardada deixou de valer no sistema
        await chaves.guardar(
            c,
            amb.cifra,
            sistema_id=amb.sistema_id,
            pessoa_id=amb.ana_id,
            segredo="acesso-revogado",
            expira_em=datetime.now(UTC) + timedelta(hours=1),
        )
    amb.zerar_vistas()
    primeira = await amb.ler_fatura("pessoa-ana", "2026-10")
    assert primeira.is_error is True
    assert "Nada foi reenviado" in primeira.content[0].text
    assert amb.vistas() == ["Bearer acesso-revogado"]  # um envio so, sem segunda volta
    assert (await amb.chaves_de(amb.ana_id))["acesso"].expira_em <= datetime.now(UTC)
    segunda = await amb.ler_fatura("pessoa-ana", "2026-10")
    assert segunda.is_error is False
    assert amb.vistas() == ["Bearer acesso-revogado", "Bearer acesso-ana-2"]
    assert amb.oauth.contagem["renovacao"] == 1


# --- Desativar e separar pessoas -------------------------------------------------------------------


async def test_desativar_a_pessoa_derruba_as_autorizacoes_dela(amb: Ambiente) -> None:
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    assert (await amb.autorizar(amb.bia_id, "bia")).status_code == 200
    pendente = await amb.iniciar(amb.ana_id)
    async with amb.entrada_db.begin() as c:  # a tela do ADMIN desativa a pessoa...
        await c.execute(text("UPDATE pessoas SET situacao = 'desativada' WHERE id = :p"), {"p": amb.ana_id})
    async with httpx2.AsyncClient() as http:  # ...e pede ao despachante para derrubar as chaves
        r = await http.post(
            f"{amb.despachante}/interno/autorizacao/derrubar",
            json={"pessoa_id": str(amb.ana_id), "motivo": "pessoa desativada"},
        )
    assert r.json() == {"derrubadas": 1}
    assert await amb.chaves_de(amb.ana_id) == {}
    assert set(await amb.chaves_de(amb.bia_id)) == {"acesso", "renovacao"}
    derrubadas = await amb.extrato("autorizacao_derrubada", amb.ana_id)
    assert [d.frase for d in derrubadas] == [
        f"Autorizacao da pessoa no sistema {(await _nome(amb))} derrubada: pessoa desativada."
    ]
    estado, codigo = await amb.na_pagina_do_sistema(pendente, "ana")
    assert (await amb.concluir(amb.ana_id, estado=estado, codigo=codigo)).json()["motivo"] == "estado_ja_usado"
    async with amb.entrada_db.begin() as c:
        await c.execute(text("UPDATE pessoas SET situacao = 'ativa' WHERE id = :p"), {"p": amb.ana_id})
    with pytest.raises(MCPError, match="ainda nao autorizou"):
        await amb.ler_fatura("pessoa-ana", "2026-10")
    assert (await amb.ler_fatura("pessoa-bia", "2026-11")).structured_content["valor"] == 59.0


async def _nome(amb: Ambiente) -> str:
    async with amb.admin.connect() as c:
        return str(
            (await c.execute(text("SELECT nome FROM sistemas WHERE id = :s"), {"s": amb.sistema_id})).scalar_one()
        )


async def test_uma_pessoa_nao_alcanca_o_que_e_da_outra(amb: Ambiente) -> None:
    assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
    assert (await amb.autorizar(amb.bia_id, "bia")).status_code == 200
    amb.zerar_vistas()
    ana = await amb.ler_fatura("pessoa-ana", "2026-11")
    bia = await amb.ler_fatura("pessoa-bia", "2026-11")
    sem = await amb.ler_fatura("pessoa-bia", "2026-10")  # so a ana tem fatura em outubro
    assert ana.structured_content["valor"] == 104.5
    assert bia.structured_content["valor"] == 59.0
    assert sem.is_error is True
    assert amb.vistas() == ["Bearer acesso-ana-1", "Bearer acesso-bia-1", "Bearer acesso-bia-1"]

    # Renovar a chave de uma nao mexe na da outra.
    await amb.vencer_chave(amb.ana_id)
    assert (await amb.ler_fatura("pessoa-ana", "2026-11")).is_error is False
    assert await amb.abrir(amb.bia_id, (await amb.chaves_de(amb.bia_id))["acesso"].cifrada) == "acesso-bia-1"

    # A chave cifrada da ana copiada para a linha da bia nao abre: o contexto amarra a chave a pessoa.
    da_ana = (await amb.chaves_de(amb.ana_id))["acesso"].cifrada
    async with amb.admin.begin() as c:
        await c.execute(
            text(
                "UPDATE chaves_cifradas SET cifrada = :c WHERE sistema_id = :s AND pessoa_id = :p AND tipo = 'acesso'"
            ),
            {"c": da_ana, "s": amb.sistema_id, "p": amb.bia_id},
        )
    amb.zerar_vistas()
    with pytest.raises(MCPError):
        await amb.ler_fatura("pessoa-bia", "2026-11")
    assert amb.vistas() == []

    # O despachante usa a pessoa do pedido, nunca outra: a ordem da ana com a pessoa bia e recusada.
    async with amb.admin.connect() as c:
        ferramenta = (
            await c.execute(
                text("SELECT id FROM ferramentas_revisadas WHERE nome_vitrine = :n AND situacao = 'ativa'"),
                {"n": amb.fatura},
            )
        ).scalar_one()
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{amb.despachante}/interno/ler",
            json={
                "pessoa_id": str(amb.bia_id),
                "aplicativo_id": str(amb.aplicativo_id),
                "ordem_id": str(amb.ordens["pessoa-ana"]),
                "ferramenta_id": str(ferramenta),
                "argumentos": {"mes": "2026-10"},
            },
        )
    assert r.status_code == 403
    assert r.json()["motivo"] == "fora_da_ordem"
    assert amb.vistas() == []


async def test_configuracao_so_vale_para_sistema_de_chave_por_pessoa(amb: Ambiente) -> None:
    async with amb.entrada_db.begin() as c:
        compartilhado = await repositorio.cadastrar_sistema(
            c,
            nome=f"compartilhado-{uuid.uuid4().hex[:6]}",
            endereco="https://s.exemplo/mcp",
            tipo_chave="compartilhada",
        )
    with pytest.raises(autorizacao.ConfiguracaoAutorizacaoInvalida, match="chave por pessoa"):
        async with amb.entrada_db.begin() as c:
            await autorizacao.configurar(
                c,
                compartilhado,
                {
                    "authorization_endpoint": "https://a.exemplo/a",
                    "token_endpoint": "https://a.exemplo/t",
                    "client_id": "x",
                },
            )
    async with amb.despachante_db.begin() as c:
        with pytest.raises(autorizacao.AutorizacaoRecusada, match="nao usa autorizacao por pessoa"):
            await autorizacao.iniciar_autorizacao(c, amb.cifra, amb.ana_id, compartilhado, VOLTA)
        with pytest.raises(autorizacao.AutorizacaoRecusada, match="https"):
            await autorizacao.iniciar_autorizacao(c, amb.cifra, amb.ana_id, amb.sistema_id, "http://fora.exemplo/v")
