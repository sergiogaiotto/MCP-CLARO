"""Telas da Fase 1 com PostgreSQL real, provedor OIDC falso, despachante de verdade e sistemas de exemplo.

As telas usam o usuario do banco da entrada (``SET ROLE mcp_claro_entrada``): se faltar
permissao na migracao, estes testes falham.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette

from exemplos import leitura_por_pessoa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine, url_asyncpg
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.extrato import registrar
from mcp_claro.telas import ConfigTelas, rotas_telas
from tests.apoio import porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_oidc_falso import IdpOidcFalso

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PUBLICO = "https://mcp-claro.teste"
SEGREDO_TELAS = "segredo-das-telas"


def engine_do_papel(p: Parametros, papel: str) -> AsyncEngine:
    """Engine que age com o usuario do banco de um papel, para provar as permissoes da migracao."""
    return create_async_engine(
        url_asyncpg(p),
        connect_args={"ssl": False, "server_settings": {"application_name": "teste-telas", "role": papel}},
        pool_size=4,
        max_overflow=4,
    )


@dataclass
class Navegador:
    """Cliente que guarda cookies na mao: os cookies ``__Host-`` sao Secure e o teste roda em http."""

    base: str
    http: httpx2.AsyncClient
    cookies: dict[str, str] = field(default_factory=dict)
    ultimos: dict[str, Any] = field(default_factory=dict)

    def _guardar(self, resposta: httpx2.Response) -> None:
        for bruto in resposta.headers.get_list("set-cookie"):
            biscoito = SimpleCookie()
            biscoito.load(bruto)
            for nome, morsel in biscoito.items():
                self.ultimos[nome] = morsel
                if morsel["max-age"] == "0" or not morsel.value:
                    self.cookies.pop(nome, None)
                else:
                    self.cookies[nome] = morsel.value

    def _cabecalhos(self, extra: dict[str, str] | None) -> dict[str, str]:
        h = dict(extra or {})
        if self.cookies:
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    async def get(self, caminho: str, headers: dict[str, str] | None = None) -> httpx2.Response:
        r = await self.http.get(self.base + caminho, headers=self._cabecalhos(headers))
        self._guardar(r)
        return r

    async def post(
        self,
        caminho: str,
        dados: dict[str, Any] | list[tuple[str, str]] | None = None,
        *,
        csrf: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        pares = list(dados.items()) if isinstance(dados, dict) else list(dados or [])
        if csrf is not None:
            pares.append(("csrf", csrf))
        cabecalhos = {"content-type": "application/x-www-form-urlencoded"} | self._cabecalhos(headers)
        r = await self.http.post(self.base + caminho, content=urlencode(pares).encode(), headers=cabecalhos)
        self._guardar(r)
        return r

    async def csrf(self) -> str:
        r = await self.get("/telas/ordens")
        return token_csrf(r.text)


def token_csrf(html: str) -> str:
    achado = re.search(r'name="csrf" value="([^"]+)"', html)
    assert achado, "pagina sem token CSRF"
    return achado.group(1)


@dataclass
class Ambiente:
    p: Parametros
    idp: IdpOidcFalso
    telas: str
    engine: AsyncEngine  # superusuario, so para preparar e conferir
    http: httpx2.AsyncClient
    url_sistema: str

    def navegador(self) -> Navegador:
        return Navegador(self.telas, self.http)

    async def entrar(self, nav: Navegador, sujeito: str, nome: str | None) -> httpx2.Response:
        self.idp.proxima_pessoa = (sujeito, nome)
        r = await nav.get("/telas/login?voltar=/telas")
        assert r.status_code == 303, r.text
        r = await self.http.get(r.headers["location"])  # o provedor devolve direto o codigo
        assert r.status_code == 302, r.text
        volta = urlsplit(r.headers["location"])
        assert f"{volta.scheme}://{volta.netloc}" == PUBLICO
        return await nav.get(f"{volta.path}?{volta.query}")

    async def pessoa(self, sujeito: str) -> Any:
        async with self.engine.connect() as c:
            return (
                await c.execute(
                    text("SELECT * FROM pessoas WHERE emissor = :e AND sujeito = :s"),
                    {"e": self.idp.emissor, "s": sujeito},
                )
            ).one()

    async def tornar_admin(self, sujeito: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(
                text("UPDATE pessoas SET papel = 'admin' WHERE emissor = :e AND sujeito = :s"),
                {"e": self.idp.emissor, "s": sujeito},
            )

    async def extrato(self, pessoa_id: uuid.UUID, tipo: str) -> list[Any]:
        async with self.engine.connect() as c:
            return list(
                (
                    await c.execute(
                        text("SELECT * FROM extrato WHERE pessoa_id = :p AND tipo = :t ORDER BY id"),
                        {"p": pessoa_id, "t": tipo},
                    )
                ).all()
            )


@contextlib.asynccontextmanager
async def subir(banco_migrado: Parametros) -> AsyncIterator[Ambiente]:
    porta_idp, porta_desp = porta_livre(), porta_livre()
    idp = IdpOidcFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro", segredo=SEGREDO_TELAS)
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
        )
    )
    engine = criar_engine(p, pool_size=3, max_overflow=3)
    engine_entrada = engine_do_papel(p, "mcp_claro_entrada")
    async with engine_entrada.connect() as c:
        assert (await c.execute(text("SELECT current_user"))).scalar_one() == "mcp_claro_entrada"
    http = httpx2.AsyncClient(follow_redirects=False, timeout=10, trust_env=False)
    despachante = ClienteDespachante(p)
    cfg = ConfigTelas(segredo=SecretStr(SEGREDO_TELAS))
    app = Starlette(routes=rotas_telas(p, engine_entrada, despachante, config=cfg, http_login=http))
    try:
        async with (
            servidor(idp.app(), porta_idp),
            servidor(criar_app_despachante(p, engine, CifraFalsa()), porta_desp),
            servidor(leitura_por_pessoa.criar()) as url_sistema,
            servidor(app) as url_telas,
        ):
            yield Ambiente(p, idp, url_telas, engine, http, f"{url_sistema}/mcp")
    finally:
        await despachante.fechar()
        await http.aclose()
        await engine_entrada.dispose()
        await engine.dispose()


@pytest.fixture
async def amb(banco_migrado: Parametros) -> AsyncIterator[Ambiente]:
    async with subir(banco_migrado) as a:
        yield a


def sufixo() -> str:
    return uuid.uuid4().hex[:8]


# --- Login ---


async def test_login_completo_cria_pessoa_sessao_com_hash_e_sair_encerra(amb: Ambiente) -> None:
    nav = amb.navegador()
    r = await nav.get("/telas")
    assert r.status_code == 200
    assert "Entrar" in r.text
    sujeito = f"ana-{sufixo()}"
    r = await amb.entrar(nav, sujeito, "Ana Teste")
    assert r.status_code == 303
    assert r.headers["location"] == "/telas"

    sessao = nav.ultimos["__Host-mcp_claro_sessao"]
    assert sessao["secure"] is True
    assert sessao["httponly"] is True
    assert sessao["samesite"].lower() == "lax"
    assert sessao["path"] == "/"
    assert sessao["domain"] == ""
    assert "__Host-mcp_claro_login" not in nav.cookies  # o cookie do login e apagado na volta
    identificador = nav.cookies["__Host-mcp_claro_sessao"]

    r = await nav.get("/telas")
    assert "Olá, Ana Teste." in r.text

    pessoa = await amb.pessoa(sujeito)  # nasceu no primeiro login
    assert pessoa.nome == "Ana Teste"
    assert pessoa.papel == "pessoa"
    async with amb.engine.connect() as c:
        linhas = (await c.execute(text("SELECT * FROM sessoes_telas WHERE pessoa_id = :p"), {"p": pessoa.id})).all()
        usados = (
            await c.execute(text("SELECT count(*) FROM logins_pendentes WHERE usado_em IS NOT NULL"))
        ).scalar_one()
    assert len(linhas) == 1
    assert bytes(linhas[0].hash_id) == hashlib.sha256(identificador.encode()).digest()
    assert identificador.encode() not in bytes(linhas[0].hash_id)
    assert linhas[0].csrf != identificador
    assert linhas[0].auth_time is not None
    assert usados >= 1
    assert await amb.extrato(pessoa.id, "telas_entrada")

    csrf = token_csrf(r.text)
    r = await nav.post("/telas/sair", csrf=csrf)
    assert r.status_code == 303
    assert "__Host-mcp_claro_sessao" not in nav.cookies
    async with amb.engine.connect() as c:
        encerrada = (
            await c.execute(text("SELECT encerrada_em FROM sessoes_telas WHERE pessoa_id = :p"), {"p": pessoa.id})
        ).scalar_one()
    assert encerrada is not None
    assert await amb.extrato(pessoa.id, "telas_saida")
    # O cookie antigo nao vale mais.
    velho = Navegador(amb.telas, amb.http, {"__Host-mcp_claro_sessao": identificador})
    r = await velho.get("/telas/ordens")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/telas/login?")


async def test_volta_do_login_e_de_uso_unico_e_presa_ao_navegador(amb: Ambiente) -> None:
    nav = amb.navegador()
    amb.idp.proxima_pessoa = (f"ana-{sufixo()}", "Ana")
    r = await nav.get("/telas/login")
    r = await amb.http.get(r.headers["location"])
    volta = urlsplit(r.headers["location"])
    caminho = f"{volta.path}?{volta.query}"

    estranho = amb.navegador()  # outro navegador, sem o cookie do login: login forcado nao passa
    r = await estranho.get(caminho)
    assert r.status_code == 400
    assert "__Host-mcp_claro_sessao" not in estranho.cookies

    biscoito = nav.cookies["__Host-mcp_claro_login"]
    r = await nav.get(caminho)
    assert r.status_code == 303
    repetido = Navegador(amb.telas, amb.http, {"__Host-mcp_claro_login": biscoito})
    r = await repetido.get(caminho)  # o mesmo estado de novo
    assert r.status_code == 400
    assert "já foi usado" in r.text
    assert "__Host-mcp_claro_sessao" not in repetido.cookies


@pytest.mark.parametrize(
    ("caso", "estragar"),
    [
        ("nonce de outro login", {"nonce": "outro-nonce"}),
        ("outro cliente", {"aud": "outro-cliente", "azp": "outro-cliente"}),
        ("azp de outro cliente", {"aud": ["mcp-claro-telas", "outro"], "azp": "outro"}),
        ("varias audiencias sem azp", {"aud": ["mcp-claro-telas", "outro"], "azp": None}),
        ("outro emissor", {"iss": "https://login-falso.exemplo/realms/x"}),
        ("vencido", {"exp": 1_000_000_000}),
        ("antigo demais", {"iat": 1_000_000_000}),
    ],
)
async def test_id_token_conferido_com_rigor(amb: Ambiente, caso: str, estragar: dict[str, Any]) -> None:
    amb.idp.estragar = estragar
    nav = amb.navegador()
    r = await amb.entrar(nav, f"ana-{sufixo()}", "Ana")
    assert r.status_code == 400, caso
    assert "Não foi possível entrar" in r.text
    assert "__Host-mcp_claro_sessao" not in nav.cookies


async def test_segredo_errado_do_cliente_nao_entra(amb: Ambiente, banco_migrado: Parametros) -> None:
    amb.idp.segredo = "outro-segredo"
    nav = amb.navegador()
    r = await amb.entrar(nav, f"ana-{sufixo()}", "Ana")
    assert r.status_code == 400
    assert "__Host-mcp_claro_sessao" not in nav.cookies


async def test_pagina_protegida_sem_sessao_manda_entrar(amb: Ambiente) -> None:
    nav = amb.navegador()
    r = await nav.get("/telas/extrato?pagina=2")
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/login?voltar=%2Ftelas%2Fextrato%3Fpagina%3D2"
    r = await nav.get("/telas/ordens", headers={"hx-request": "true"})  # navegacao pelo htmx
    assert r.status_code == 200
    assert r.headers["hx-redirect"] == "/telas/login?voltar=%2Ftelas%2Fordens"
    r = await nav.get("/telas/login?voltar=https://ataque.exemplo/")
    assert r.status_code == 303
    async with amb.engine.connect() as c:
        voltar = (
            await c.execute(text("SELECT voltar_para FROM logins_pendentes ORDER BY criado_em DESC LIMIT 1"))
        ).scalar_one()
    assert voltar == "/telas"  # sem redirecionamento aberto


# --- CSRF e area do ADMIN ---


async def test_csrf_recusa_post_sem_token_com_token_errado_e_de_outra_origem(amb: Ambiente) -> None:
    nav = amb.navegador()
    await amb.entrar(nav, f"ana-{sufixo()}", "Ana")
    csrf = await nav.csrf()
    for caso, kwargs in [
        ("sem token", {}),
        ("token errado", {"csrf": "x" * len(csrf)}),
        ("outra origem", {"csrf": csrf, "headers": {"origin": "https://ataque.exemplo"}}),
        ("site cruzado", {"csrf": csrf, "headers": {"sec-fetch-site": "cross-site"}}),
    ]:
        r = await nav.post("/telas/sair", **kwargs)
        assert r.status_code == 403, caso
        assert "nada foi feito" in r.text
    r = await nav.get("/telas/ordens")
    assert r.status_code == 200  # a sessao continua: o sair sem token nao valeu
    r = await nav.post("/telas/sair", csrf=csrf, headers={"origin": PUBLICO})
    assert r.status_code == 303


async def test_post_sem_sessao_e_recusado(amb: Ambiente) -> None:
    r = await amb.navegador().post("/telas/ordens", {"dias": "1"}, csrf="qualquer")
    assert r.status_code == 403


async def test_pessoa_comum_nao_entra_nas_telas_do_admin(amb: Ambiente) -> None:
    nav = amb.navegador()
    await amb.entrar(nav, f"bia-{sufixo()}", "Bia")
    csrf = await nav.csrf()
    for caminho in ("/telas/admin", "/telas/admin/sistemas", "/telas/admin/aplicativos", "/telas/admin/extrato"):
        r = await nav.get(caminho)
        assert r.status_code == 403, caminho
        assert "só do ADMIN" in r.text
    r = await nav.post(
        "/telas/admin/aplicativos", {"client_id": "x", "nome": "X", "familia": "x", "tipo": "assistente"}, csrf=csrf
    )
    assert r.status_code == 403
    async with amb.engine.connect() as c:
        assert (await c.execute(text("SELECT 1 FROM aplicativos WHERE client_id = 'x'"))).first() is None
    r = await nav.get("/telas")
    assert "Área do ADMIN" not in r.text


# --- Minhas ordens de servico ---


async def _cadastro_basico(amb: Ambiente, validade: int = 10) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    s = sufixo()
    async with amb.engine.begin() as c:
        app_id = await repositorio.habilitar_aplicativo(c, client_id=f"app-{s}", nome=f"Assistente {s}", familia="x")
        sistema_id = await repositorio.cadastrar_sistema(
            c, nome=f"sis-{s}", endereco="https://sistema.exemplo/mcp", tipo_chave="por_pessoa"
        )
        await c.execute(
            text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2026-07-28' WHERE id = :i"), {"i": sistema_id}
        )
        await repositorio.revisar_ferramenta(
            c,
            sistema_id=sistema_id,
            definicao_declarada=leitura_por_pessoa.FERRAMENTAS[0],
            nome_vitrine=f"fatura_{s}",
            descricao=f"Mostra a sua fatura {s}.",
            classe="le",
            revisado_por="admin-teste",
        )
        modelo_id = await repositorio.criar_modelo_ordem(
            c,
            nome=f"Ver faturas {s}",
            descricao="Deixa o aplicativo ver suas faturas.",
            ferramentas=[(sistema_id, "consultar_fatura")],
            validade_dias=validade,
            criado_por="admin-teste",
        )
    return app_id, modelo_id, f"Assistente {s}", f"Mostra a sua fatura {s}."


async def test_escolher_e_revogar_ordem_de_servico(amb: Ambiente) -> None:
    app_id, modelo_id, nome_app, descricao = await _cadastro_basico(amb, validade=10)
    nav = amb.navegador()
    sujeito = f"ana-{sufixo()}"
    await amb.entrar(nav, sujeito, "Ana")
    pessoa = await amb.pessoa(sujeito)
    r = await nav.get("/telas/ordens")
    assert "Nenhum aplicativo tem ordem sua agora" in r.text
    assert nome_app in r.text
    csrf = token_csrf(r.text)

    escolha = {"aplicativo": str(app_id), "modelo": str(modelo_id)}
    r = await nav.post("/telas/ordens", escolha | {"dias": "11"}, csrf=csrf)
    assert r.status_code == 400
    assert "O prazo vai de 1 a 10 dias." in r.text
    r = await nav.post(
        "/telas/ordens", {"aplicativo": str(uuid.uuid4()), "modelo": str(modelo_id), "dias": "5"}, csrf=csrf
    )
    assert r.status_code == 400
    assert "Escolha um dos aplicativos" in r.text

    r = await nav.post("/telas/ordens", escolha | {"dias": "5"}, csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/ordens?aviso=ordem_dada"
    r = await nav.get(r.headers["location"])
    assert "A ordem de serviço foi dada" in r.text
    assert descricao in r.text  # o que ele pode ler, com o texto do cadastro
    assert "Revogar esta ordem" in r.text
    async with amb.engine.connect() as c:
        ordem = (
            await c.execute(
                text("SELECT id, situacao, fim - inicio AS prazo FROM ordens_servico WHERE pessoa_id = :p"),
                {"p": pessoa.id},
            )
        ).one()
    assert ordem.situacao == "ativa"
    assert 4.9 < ordem.prazo.total_seconds() / 86400 <= 5.01
    dada = await amb.extrato(pessoa.id, "ordem_concedida")
    assert len(dada) == 1
    assert nome_app in dada[0].frase
    assert dada[0].aplicativo_id == app_id

    r = await nav.post("/telas/ordens", escolha | {"dias": "3"}, csrf=csrf)
    assert r.status_code == 400
    assert "já tem esta ordem valendo" in r.text

    # Outra pessoa nao revoga a ordem da Ana.
    outra = amb.navegador()
    await amb.entrar(outra, f"bia-{sufixo()}", "Bia")
    r = await outra.post(f"/telas/ordens/{ordem.id}/revogar", csrf=await outra.csrf())
    assert r.status_code == 404

    r = await nav.post(f"/telas/ordens/{ordem.id}/revogar", csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "A ordem foi revogada" in r.text
    assert "Revogar esta ordem" not in r.text
    assert "Revogada" in r.text
    async with amb.engine.connect() as c:
        situacao = (
            await c.execute(text("SELECT situacao FROM ordens_servico WHERE id = :o"), {"o": ordem.id})
        ).scalar_one()
    assert situacao == "revogada"
    revogada = await amb.extrato(pessoa.id, "ordem_revogada")
    assert len(revogada) == 1
    r = await nav.post(f"/telas/ordens/{ordem.id}/revogar", csrf=csrf)
    assert r.status_code == 404  # revogar de novo nao muda nada


async def test_prazo_da_ordem_respeita_a_politica(amb: Ambiente) -> None:
    app_id, modelo_id, _, _ = await _cadastro_basico(amb, validade=60)
    async with amb.engine.begin() as c:
        await c.execute(
            text("INSERT INTO politica (chave, valor, autor, motivo) VALUES ('WORK_ORDER_MAX_DAYS', 8, 't', 'teste')")
        )
    try:
        nav = amb.navegador()
        await amb.entrar(nav, f"ana-{sufixo()}", "Ana")
        r = await nav.get("/telas/ordens")
        assert "de 1 a 8" in r.text
        escolha = {"aplicativo": str(app_id), "modelo": str(modelo_id)}
        r = await nav.post("/telas/ordens", escolha | {"dias": "9"}, csrf=token_csrf(r.text))
        assert r.status_code == 400
        assert "O prazo vai de 1 a 8 dias." in r.text
    finally:
        async with amb.engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo)"
                    " VALUES ('WORK_ORDER_MAX_DAYS', 30, 't', 'volta')"
                )
            )


# --- Meu extrato ---


async def test_meu_extrato_mais_novas_primeiro_com_paginas(amb: Ambiente) -> None:
    nav = amb.navegador()
    sujeito = f"ana-{sufixo()}"
    await amb.entrar(nav, sujeito, "Ana")
    pessoa = await amb.pessoa(sujeito)
    async with amb.engine.begin() as c:
        for numero in range(30):
            await registrar(
                c, papel="entrada", tipo="fechamento", campos={"numero": numero, "linhas": 0}, pessoa_id=pessoa.id
            )
    r = await nav.get("/telas/extrato")
    assert r.status_code == 200
    assert r.text.index("Fechamento 29 ") < r.text.index("Fechamento 28 ")
    assert r.text.count("<tr>") == 26  # cabecalho + 25 linhas
    assert 'href="/telas/extrato?pagina=2"' in r.text
    r = await nav.get("/telas/extrato?pagina=2")
    assert "Fechamento 3 " in r.text
    assert "Entrada nas telas do MCP-CLARO." in r.text  # a linha mais antiga: o proprio login
    assert "Mais antigas" not in r.text
    assert "Fechamento 29 " not in r.text


# --- Extrato de outra pessoa ---


async def test_admin_abre_extrato_alheio_so_com_motivo_e_deixa_linha(amb: Ambiente) -> None:
    bia_nav = amb.navegador()
    bia = f"bia-{sufixo()}"
    await amb.entrar(bia_nav, bia, "Bia")
    alvo = await amb.pessoa(bia)

    nav = amb.navegador()
    admin = f"admin-{sufixo()}"
    await amb.entrar(nav, admin, "Carla Admin")
    await amb.tornar_admin(admin)

    r = await nav.get(f"/telas/admin/extrato/{alvo.id}")  # sem motivo, nao abre
    assert r.status_code == 303
    assert r.headers["location"].startswith("/telas/admin/extrato?")
    r = await nav.get(r.headers["location"])
    assert "diga antes o motivo" in r.text
    csrf = token_csrf(r.text)

    r = await nav.post("/telas/admin/extrato", {"pessoa": str(alvo.id), "motivo": ""}, csrf=csrf)
    assert r.status_code == 400
    r = await nav.post("/telas/admin/extrato", {"pessoa": str(alvo.id), "motivo": "curto"}, csrf=csrf)
    assert r.status_code == 400
    assert await amb.extrato(alvo.id, "extrato_alheio_aberto") == []

    motivo = "A Bia pediu ajuda no chamado CH-42."
    r = await nav.post("/telas/admin/extrato", {"pessoa": str(alvo.id), "motivo": motivo}, csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert r.status_code == 200
    assert "Extrato de Bia" in r.text
    assert "Entrada nas telas do MCP-CLARO." in r.text
    linhas = await amb.extrato(alvo.id, "extrato_alheio_aberto")
    assert len(linhas) == 1
    assert linhas[0].frase == f"O ADMIN Carla Admin abriu este extrato. Motivo: {motivo}"
    assert linhas[0].dados["admin_id"] == str((await amb.pessoa(admin)).id)

    r = await nav.get(f"/telas/admin/extrato/{alvo.id}?pagina=1")  # folhear nao cria linha nova
    assert r.status_code == 200
    assert len(await amb.extrato(alvo.id, "extrato_alheio_aberto")) == 1

    r = await bia_nav.get("/telas/extrato")  # a pessoa ve quem abriu e por que
    assert "O ADMIN Carla Admin abriu este extrato." in r.text
    r = await bia_nav.get(f"/telas/admin/extrato/{alvo.id}")
    assert r.status_code == 403


# --- Telas do ADMIN ---


async def test_admin_cadastra_sistema_da_geracao_antiga_e_ve_a_recusa_legivel(amb: Ambiente) -> None:
    from tests.apoio import sistema_geracao_antiga

    nav = amb.navegador()
    admin = f"admin-{sufixo()}"
    await amb.entrar(nav, admin, "Carla Admin")
    await amb.tornar_admin(admin)
    r = await nav.get("/telas/admin/sistemas")
    assert r.status_code == 200
    csrf = token_csrf(r.text)
    nome = f"antigo-{sufixo()}"
    with sistema_geracao_antiga() as url:
        r = await nav.post(
            "/telas/admin/sistemas", {"nome": nome, "endereco": url, "tipo_chave": "compartilhada"}, csrf=csrf
        )
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "Este sistema foi recusado." in r.text
    assert "Ele só fala a geração antiga do MCP. O MCP-CLARO só aceita a versão 2026-07-28." in r.text
    async with amb.engine.connect() as c:
        sistema = (await c.execute(text("SELECT * FROM sistemas WHERE nome = :n"), {"n": nome})).one()
        tipos = (
            await c.execute(
                text("SELECT tipo, frase, pessoa_id FROM extrato WHERE sistema_id = :s ORDER BY id"), {"s": sistema.id}
            )
        ).all()
    assert sistema.situacao == "recusado"
    assert [t.tipo for t in tipos] == ["admin_sistema_cadastrado", "recusa_versao_sistema", "admin_sondagem_pedida"]
    assert "por Carla Admin" in tipos[0].frase
    assert "recusado" in tipos[2].frase
    r = await nav.get("/telas/admin/sistemas")
    assert "Recusado" in r.text


async def test_admin_cadastra_sistema_bom_revisa_ferramenta_e_cria_modelo(amb: Ambiente) -> None:
    nav = amb.navegador()
    admin = f"admin-{sufixo()}"
    await amb.entrar(nav, admin, "Carla Admin")
    await amb.tornar_admin(admin)
    admin_id = (await amb.pessoa(admin)).id
    csrf = token_csrf((await nav.get("/telas/admin")).text)
    s = sufixo()

    r = await nav.post(
        "/telas/admin/sistemas",
        {"nome": f"leitura-{s}", "endereco": "ftp://x", "tipo_chave": "por_pessoa"},
        csrf=csrf,
    )
    assert r.status_code == 400
    assert "https://" in r.text

    r = await nav.post(
        "/telas/admin/sistemas",
        {
            "nome": f"leitura-{s}",
            "endereco": amb.url_sistema,
            "tipo_chave": "por_pessoa",
            "chave_sondagem": "chave-ana",
        },
        csrf=csrf,
    )
    assert r.status_code == 303
    ficha = r.headers["location"]
    r = await nav.get(ficha)
    assert "Aceito." in r.text
    assert "chave-ana" not in r.text

    r = await nav.post(f"{ficha}/declaracoes", {"chave_sondagem": "chave-ana"}, csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "consultar_fatura" in r.text
    assert "Sem revisão" in r.text

    r = await nav.get(f"{ficha}/ferramenta?nome=consultar_fatura")
    assert r.status_code == 200
    assert "Mostra o valor da fatura de um mes da propria pessoa." in r.text  # texto do sistema, marcado
    revisao = {
        "nome": "consultar_fatura",
        "nome_vitrine": f"fatura_{s}",
        "descricao": "Mostra a sua fatura de um mês.",
        "classe": "le",
        "risco": "baixo",
    }
    r = await nav.post(f"{ficha}/ferramenta", [*revisao.items(), ("campos", "cpf")], csrf=csrf)
    assert r.status_code == 303
    async with amb.engine.connect() as c:
        ferramenta = (
            await c.execute(
                text("SELECT * FROM ferramentas_revisadas WHERE nome_vitrine = :n AND situacao = 'ativa'"),
                {"n": f"fatura_{s}"},
            )
        ).one()
    assert ferramenta.campos_pessoais == ["cpf"]
    assert ferramenta.revisado_por == "Carla Admin"
    assert ferramenta.descricao == "Mostra a sua fatura de um mês."
    r = await nav.get(ficha)
    assert "Revisada" in r.text

    r = await nav.post(
        "/telas/admin/modelos",
        {"nome": f"Faturas {s}", "descricao": "Deixa ver as suas faturas.", "validade_dias": "31"},
        csrf=csrf,
    )
    assert r.status_code == 400
    assert "Escolha ao menos uma ferramenta" in r.text
    r = await nav.post(
        "/telas/admin/modelos",
        [
            ("nome", f"Faturas {s}"),
            ("descricao", "Deixa ver as suas faturas."),
            ("validade_dias", "15"),
            ("ferramentas", str(ferramenta.id)),
        ],
        csrf=csrf,
    )
    assert r.status_code == 303
    r = await nav.get("/telas/admin/modelos")
    assert f"Faturas {s}" in r.text

    tipos = {linha.tipo: linha.frase for linha in await _extrato_de(amb, admin_id)}
    assert {
        "admin_sistema_cadastrado",
        "admin_sondagem_pedida",
        "admin_declaracoes_lidas",
        "admin_ferramenta_revisada",
        "admin_modelo_criado",
    } <= tipos.keys()
    assert all("Carla Admin" in tipos[t] for t in tipos if t.startswith("admin_"))


async def _extrato_de(amb: Ambiente, pessoa_id: uuid.UUID) -> list[Any]:
    async with amb.engine.connect() as c:
        return list(
            (await c.execute(text("SELECT tipo, frase FROM extrato WHERE pessoa_id = :p"), {"p": pessoa_id})).all()
        )


async def test_admin_habilita_aplicativo_programa_exige_responsavel_e_webhook(amb: Ambiente) -> None:
    nav = amb.navegador()
    admin = f"admin-{sufixo()}"
    await amb.entrar(nav, admin, "Carla Admin")
    await amb.tornar_admin(admin)
    admin_id = (await amb.pessoa(admin)).id
    csrf = token_csrf((await nav.get("/telas/admin")).text)
    s = sufixo()

    r = await nav.post(
        "/telas/admin/aplicativos",
        {"client_id": f"goose-{s}", "nome": "Goose", "familia": "goose", "tipo": "assistente"},
        csrf=csrf,
    )
    assert r.status_code == 303
    r = await nav.post(
        "/telas/admin/aplicativos",
        {"client_id": f"goose-{s}", "nome": "Goose 2", "familia": "goose", "tipo": "assistente"},
        csrf=csrf,
    )
    assert r.status_code == 400
    assert "Já existe um aplicativo" in r.text
    r = await nav.post(
        "/telas/admin/aplicativos",
        {"client_id": f"robo-{s}", "nome": "Robo", "familia": "robo", "tipo": "programa"},
        csrf=csrf,
    )
    assert r.status_code == 400
    assert "Use a tela Programas" in r.text

    resp, subs = f"resp-{s}", f"subs-{s}"
    async with amb.engine.begin() as c:
        r_id = (await repositorio.pessoa_no_login(c, amb.idp.emissor, resp, f"Rui {s}")).id
        s_id = (await repositorio.pessoa_no_login(c, amb.idp.emissor, subs, f"Sara {s}")).id
        sem_nome = (await repositorio.pessoa_no_login(c, amb.idp.emissor, f"sem-{s}", None)).id
    programa = {
        "client_id": f"robo-{s}",
        "nome": f"Robo {s}",
        "familia": "programa",
        "finalidade": "Le os chamados abertos toda manha.",
        "fim": (date.today() + timedelta(days=200)).isoformat(),
    }
    for faltando in (
        {},
        {"responsavel": str(r_id)},
        {"responsavel": str(r_id), "substituto": str(r_id)},
        {"responsavel": str(r_id), "substituto": str(sem_nome)},
    ):
        r = await nav.post("/telas/admin/programas", programa | faltando, csrf=csrf)
        assert r.status_code == 400, faltando
    assert (
        "Sem responsável e substituto com nome, o programa não é salvo."
        in (await nav.post("/telas/admin/programas", programa, csrf=csrf)).text
    )
    async with amb.engine.connect() as c:
        assert (
            await c.execute(text("SELECT 1 FROM aplicativos WHERE client_id = :c"), {"c": f"robo-{s}"})
        ).first() is None

    r = await nav.post(
        "/telas/admin/programas", programa | {"responsavel": str(r_id), "substituto": str(s_id)}, csrf=csrf
    )
    assert r.status_code == 303
    async with amb.engine.connect() as c:
        linha = (
            await c.execute(
                text(
                    "SELECT a.tipo, p.responsavel_id, p.substituto_id FROM programas p"
                    " JOIN aplicativos a ON a.id = p.aplicativo_id WHERE a.client_id = :c"
                ),
                {"c": f"robo-{s}"},
            )
        ).one()
    assert (linha.tipo, linha.responsavel_id, linha.substituto_id) == ("programa", r_id, s_id)
    r = await nav.get("/telas/admin/programas")
    assert f"Rui {s}" in r.text

    r = await nav.post("/telas/admin/webhooks", {"nome": f"plantao-{s}", "url": "http://inseguro.exemplo/x"}, csrf=csrf)
    assert r.status_code == 400
    r = await nav.post(
        "/telas/admin/webhooks",
        {"nome": f"plantao-{s}", "url": "https://avisos.exemplo/mcp", "eventos": "bloqueio, ronda_falha"},
        csrf=csrf,
    )
    assert r.status_code == 303
    async with amb.engine.connect() as c:
        destino = (
            await c.execute(
                text("SELECT id, eventos, ativo FROM destinos_webhook WHERE nome = :n"), {"n": f"plantao-{s}"}
            )
        ).one()
    assert destino.eventos == ["bloqueio", "ronda_falha"]
    r = await nav.post(f"/telas/admin/webhooks/{destino.id}/situacao", {"ativo": "nao"}, csrf=csrf)
    assert r.status_code == 303
    async with amb.engine.connect() as c:
        ativo = (
            await c.execute(text("SELECT ativo FROM destinos_webhook WHERE id = :i"), {"i": destino.id})
        ).scalar_one()
    assert ativo is False

    frases = {linha.tipo: linha.frase for linha in await _extrato_de(amb, admin_id)}
    assert frases["admin_aplicativo_habilitado"] == "Aplicativo Goose habilitado por Carla Admin."
    assert frases["admin_programa_cadastrado"] == (
        f"Programa Robo {s} cadastrado por Carla Admin; responsavel Rui {s}, substituto Sara {s}."
    )
    assert frases["admin_webhook_cadastrado"] == f"Destino de webhook plantao-{s} cadastrado por Carla Admin."
    assert frases["admin_webhook_mudou"] == f"Destino de webhook plantao-{s} desligado por Carla Admin."


# --- Escape e cabecalhos ---


async def test_nome_com_script_aparece_escapado(amb: Ambiente) -> None:
    perigoso = "<script>alert('oi')</script>"
    nav = amb.navegador()
    admin = f"admin-{sufixo()}"
    await amb.entrar(nav, admin, perigoso)
    await amb.tornar_admin(admin)
    r = await nav.get("/telas")
    assert "&lt;script&gt;alert(&#39;oi&#39;)&lt;/script&gt;" in r.text
    assert perigoso not in r.text
    csrf = token_csrf(r.text)
    r = await nav.post(
        "/telas/admin/aplicativos",
        {"client_id": f"app-{sufixo()}", "nome": perigoso, "familia": "x", "tipo": "assistente"},
        csrf=csrf,
    )
    assert r.status_code == 303
    r = await nav.get("/telas/admin/aplicativos")
    assert "&lt;script&gt;" in r.text
    assert "<script>alert" not in r.text


async def test_cabecalhos_de_seguranca_em_toda_resposta(amb: Ambiente) -> None:
    nav = amb.navegador()
    await amb.entrar(nav, f"ana-{sufixo()}", "Ana")
    respostas = {
        "inicio": await nav.get("/telas"),
        "ordens": await nav.get("/telas/ordens"),
        "estatico": await nav.get("/telas/estaticos/htmx.min.js"),
        "nao existe": await nav.get("/telas/nao-existe"),
        "csrf recusado": await nav.post("/telas/sair"),
        "bloqueio": await nav.get("/telas/bloqueio"),
    }
    for caso, r in respostas.items():
        h = r.headers
        assert "script-src 'self'" in h["content-security-policy"], caso
        assert "frame-ancestors 'none'" in h["content-security-policy"], caso
        assert "unsafe-inline" not in h["content-security-policy"], caso
        assert "unsafe-eval" not in h["content-security-policy"], caso
        assert h["x-frame-options"] == "DENY", caso
        assert h["referrer-policy"] == "no-referrer", caso
        assert h["x-content-type-options"] == "nosniff", caso
        assert h["strict-transport-security"].startswith("max-age="), caso
    assert respostas["nao existe"].status_code == 404
    assert respostas["csrf recusado"].status_code == 403
    assert respostas["bloqueio"].status_code == 200
    assert "Bloquear todos agora" in respostas["bloqueio"].text
    assert "em breve" not in respostas["inicio"].text
    assert respostas["inicio"].headers["cache-control"] == "no-store"
    estatico = respostas["estatico"]
    assert estatico.status_code == 200
    assert "javascript" in estatico.headers["content-type"]
    assert "Licenca: 0BSD" in estatico.text[:2000]
    pagina = respostas["inicio"].text
    assert '<script src="/telas/estaticos/htmx.min.js" defer></script>' in pagina
    assert not re.search(r"<script(?![^>]*src=\"/telas/estaticos/)", pagina)  # nenhum script embutido ou de fora
    assert " style=" not in pagina
    r = await nav.get("/telas/login")
    assert r.headers["x-frame-options"] == "DENY"
    biscoito = nav.ultimos["__Host-mcp_claro_login"]
    assert (biscoito["secure"], biscoito["httponly"], biscoito["samesite"].lower()) == (True, True, "lax")


async def test_pessoa_desativada_perde_a_sessao_e_nao_entra(amb: Ambiente) -> None:
    nav = amb.navegador()
    sujeito = f"ana-{sufixo()}"
    await amb.entrar(nav, sujeito, "Ana")
    assert (await nav.get("/telas/ordens")).status_code == 200
    async with amb.engine.begin() as c:
        await c.execute(
            text("UPDATE pessoas SET situacao = 'desativada' WHERE emissor = :e AND sujeito = :s"),
            {"e": amb.idp.emissor, "s": sujeito},
        )
    r = await nav.get("/telas/ordens")
    assert r.status_code == 303  # a sessao deixa de valer na hora
    r = await amb.entrar(amb.navegador(), sujeito, "Ana")
    assert r.status_code == 403
    assert "Seu acesso está suspenso." in r.text


async def test_novo_login_no_mesmo_navegador_encerra_a_sessao_anterior(amb: Ambiente) -> None:
    nav = amb.navegador()
    sujeito = f"ana-{sufixo()}"
    await amb.entrar(nav, sujeito, "Ana")
    primeira = nav.cookies["__Host-mcp_claro_sessao"]
    await amb.entrar(nav, sujeito, "Ana")
    assert nav.cookies["__Host-mcp_claro_sessao"] != primeira
    velho = Navegador(amb.telas, amb.http, {"__Host-mcp_claro_sessao": primeira})
    assert (await velho.get("/telas/ordens")).status_code == 303
    assert (await nav.get("/telas/ordens")).status_code == 200
