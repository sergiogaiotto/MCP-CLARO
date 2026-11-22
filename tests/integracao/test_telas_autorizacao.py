"""Telas "Autorizar um sistema" e a configuracao do ADMIN (OAuth, segredo e chaves), de ponta a ponta.

Sobe de verdade: PostgreSQL 18, provedor OIDC falso (login das telas e crachas do aplicativo), servidor
OAuth falso do sistema (PKCE S256 conferido), o sistema de exemplo ``leitura_por_pessoa``, o despachante
e a entrada inteira (MCP e telas). Cada papel conecta ao banco com o proprio usuario.

O arranjo (``subir`` e ``Ambiente``) tambem serve aos testes de bloqueio e pausa (``test_telas_bloqueio``).
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from exemplos import leitura_por_pessoa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import autorizacao, chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.telas import autorizar
from tests import apoio_terceiros as t3
from tests.apoio import cliente_mcp, porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_oidc_falso import IdpOidcFalso
from tests.integracao.test_autorizacao import OAuthFalso, engine_do_papel, mapa_de_chaves
from tests.integracao.test_telas import Navegador, token_csrf

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
VOLTA = f"{PUBLICO}/telas/autorizar/volta"
SEGREDO_TELAS = "segredo-das-telas"


def sufixo() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Ambiente:
    p: Parametros
    idp: IdpOidcFalso
    oauth: OAuthFalso
    admin: AsyncEngine  # dono do banco: arranjo e conferencias
    entrada_db: AsyncEngine
    despachante_db: AsyncEngine
    cifra: CifraFalsa
    entrada: str
    http: httpx2.AsyncClient
    url_oauth: str
    sistema_id: uuid.UUID
    sistema_nome: str
    aplicativo_id: uuid.UUID
    aplicativo_nome: str
    client_id: str
    modelo_id: uuid.UUID
    fatura: str

    def navegador(self) -> Navegador:
        return Navegador(self.entrada, self.http)

    async def entrar(self, nav: Navegador, sujeito: str, nome: str) -> httpx2.Response:
        self.idp.proxima_pessoa = (sujeito, nome)
        r = await nav.get("/telas/login?voltar=/telas")
        assert r.status_code == 303, r.text
        r = await self.http.get(r.headers["location"])  # o provedor devolve direto o codigo
        assert r.status_code == 302, r.text
        volta = urlsplit(r.headers["location"])
        return await nav.get(f"{volta.path}?{volta.query}")

    async def nova_pessoa(self, nome: str, *, admin: bool = False) -> tuple[Navegador, str, uuid.UUID]:
        """Entra pelas telas; a pessoa nasce de teste (a leitura para pessoas reais segue desligada)."""
        nav, sujeito = self.navegador(), f"{nome.split()[0].lower()}-{sufixo()}"
        assert (await self.entrar(nav, sujeito, nome)).status_code == 303
        async with self.admin.begin() as c:
            pessoa_id = (
                await c.execute(
                    text(
                        "UPDATE pessoas SET teste = true, papel = :papel WHERE emissor = :e AND sujeito = :s"
                        " RETURNING id"
                    ),
                    {"e": self.idp.emissor, "s": sujeito, "papel": "admin" if admin else "pessoa"},
                )
            ).scalar_one()
        return nav, sujeito, pessoa_id

    async def dar_ordem(self, pessoa_id: uuid.UUID) -> uuid.UUID:
        async with self.entrada_db.begin() as c:
            return await repositorio.conceder_ordem(
                c, pessoa_id=pessoa_id, aplicativo_id=self.aplicativo_id, modelo_id=self.modelo_id, dias=7, max_dias=30
            )

    async def configurar_direto(self) -> None:
        """O que o ADMIN faria pelas telas: a autorizacao OAuth do sistema e o segredo do cliente."""
        async with self.entrada_db.begin() as c:
            await autorizacao.configurar(
                c,
                self.sistema_id,
                {
                    "authorization_endpoint": f"{self.url_oauth}/authorize",
                    "token_endpoint": f"{self.url_oauth}/token",
                    "client_id": self.oauth.client_id,
                    "escopos": ["leitura"],
                },
            )
        despachante = ClienteDespachante(self.p)
        try:
            resposta = await despachante.post(
                "/interno/autorizacao/segredo", {"sistema_id": str(self.sistema_id), "segredo": self.oauth.segredo}
            )
        finally:
            await despachante.fechar()
        assert resposta == {"guardado": True}

    async def na_pagina_do_sistema(self, url: str, usuario: str) -> str:
        """O navegador na pagina do sistema: entra como ``usuario``, autoriza e volta. Devolve o caminho da volta."""
        r = await self.http.get(url + "&" + urlencode({"usuario": usuario}))
        assert r.status_code == 302, r.text
        volta = urlsplit(r.headers["location"])
        assert f"{volta.scheme}://{volta.netloc}{volta.path}" == VOLTA
        return f"{volta.path}?{volta.query}"

    async def link_autorizar(self, nav: Navegador) -> str:
        r = await nav.get("/telas/autorizar")
        assert r.status_code == 200, r.text
        achado = re.search(rf'href="(/telas/autorizar/{self.sistema_id}/ir\?ficha=[0-9a-f]+)"', r.text)
        assert achado, "a lista nao trouxe o botao de autorizar"
        return achado.group(1)

    async def autorizar_pela_tela(self, nav: Navegador, usuario: str) -> httpx2.Response:
        """O caminho inteiro pelo navegador. Devolve a resposta da volta (303 de volta a lista, se deu certo)."""
        r = await nav.get(await self.link_autorizar(nav))
        assert r.status_code == 303, r.text
        return await nav.get(await self.na_pagina_do_sistema(r.headers["location"], usuario))

    async def chaves_de(self, pessoa_id: uuid.UUID) -> dict[str, Any]:
        async with self.admin.connect() as c:
            linhas = (
                await c.execute(
                    text("SELECT tipo, cifrada FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p"),
                    {"s": self.sistema_id, "p": pessoa_id},
                )
            ).all()
        return {linha.tipo: linha for linha in linhas}

    async def abrir(self, pessoa_id: uuid.UUID, cifrada: str, tipo: str = "acesso") -> str:
        return (await self.cifra.decifrar(cifrada, chaves.contexto(self.sistema_id, pessoa_id, None, tipo))).decode()

    async def ler_fatura(self, sujeito: str, mes: str = "2026-10") -> Any:
        """Uma leitura pelo aplicativo. Recusa sai como ``MCPError``."""
        recusa: MCPError | None = None
        cracha = self.idp.cracha(audiencia=RECURSO, sujeito=sujeito, client_id=self.client_id)
        async with cliente_mcp(f"{self.entrada}/mcp", cracha) as c:
            try:
                return await c.call_tool(self.fatura, {"mes": mes})
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def listar(self, sujeito: str) -> list[str]:
        recusa: MCPError | None = None
        cracha = self.idp.cracha(audiencia=RECURSO, sujeito=sujeito, client_id=self.client_id)
        async with cliente_mcp(f"{self.entrada}/mcp", cracha) as c:
            try:
                return sorted(t.name for t in (await c.list_tools()).tools)
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def extrato(self, tipo: str, pessoa_id: uuid.UUID | None = None) -> list[Any]:
        async with self.admin.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT frase, dados, pessoa_id, aplicativo_id, sistema_id FROM extrato WHERE tipo = :t"
                            " AND (CAST(:p AS uuid) IS NULL OR pessoa_id = :p) ORDER BY id"
                        ),
                        {"t": tipo, "p": pessoa_id},
                    )
                ).all()
            )


@contextlib.asynccontextmanager
async def subir(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, pasta: Path) -> AsyncIterator[Ambiente]:
    monkeypatch.setenv("EXEMPLO_CHAVES", json.dumps(mapa_de_chaves()))
    porta_idp, porta_desp = porta_livre(), porta_livre()
    idp = IdpOidcFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro", segredo=SEGREDO_TELAS)
    arquivo = pasta / "segredo-telas"
    arquivo.write_text(SEGREDO_TELAS, encoding="utf-8")
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
                "SCREENS_CLIENT_SECRET_FILE": str(arquivo),
            }
        )
    )
    oauth = OAuthFalso()
    admin = criar_engine(p, pool_size=3, max_overflow=3)
    entrada_db = engine_do_papel(p, "mcp_claro_entrada")
    despachante_db = engine_do_papel(p, "mcp_claro_despachante")
    cifra = CifraFalsa()
    http = httpx2.AsyncClient(follow_redirects=False, timeout=10, trust_env=False)
    http_login = httpx2.AsyncClient(follow_redirects=False, timeout=10, trust_env=False)  # a entrada fecha
    try:
        async with (
            servidor(idp.app(), porta_idp),
            servidor(leitura_por_pessoa.criar()) as url_sistema,
            servidor(oauth.app()) as url_oauth,
            servidor(criar_app_despachante(p, despachante_db, cifra), porta_desp),
            servidor(criar_app_entrada(p, entrada_db, http_login=http_login)) as url_entrada,
        ):
            s = sufixo()
            client_id, nome_app, nome_sistema, fatura = f"app-{s}", f"Assistente {s}", f"leitura-{s}", f"fatura_{s}"
            async with entrada_db.begin() as c:
                aplicativo_id = await repositorio.habilitar_aplicativo(
                    c, client_id=client_id, nome=nome_app, familia="claude-code"
                )
                sistema_id = await repositorio.cadastrar_sistema(
                    c, nome=nome_sistema, endereco=f"{url_sistema}/mcp", tipo_chave="por_pessoa"
                )
            despachante = ClienteDespachante(p)
            try:
                corpo = {"sistema_id": str(sistema_id), "chave_sondagem": "chave-sondagem"}
                assert (await despachante.post("/interno/sondar", corpo))["aceito"]
                declaradas = (await despachante.post("/interno/declaracoes", corpo))["ferramentas"]
            finally:
                await despachante.fechar()
            por_nome = {f["name"]: f for f in declaradas}
            async with entrada_db.begin() as c:
                await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=sistema_id,
                    definicao_declarada=por_nome["consultar_fatura"],
                    nome_vitrine=fatura,
                    descricao="Mostra a sua fatura de um mes.",
                    classe="le",
                    revisado_por="admin-teste",
                )
                modelo_id = await repositorio.criar_modelo_ordem(
                    c,
                    nome=f"Faturas {s}",
                    descricao="Deixa o aplicativo ver as suas faturas.",
                    ferramentas=[(sistema_id, "consultar_fatura")],
                    validade_dias=30,
                    criado_por="admin-teste",
                )
            yield Ambiente(
                p=p,
                idp=idp,
                oauth=oauth,
                admin=admin,
                entrada_db=entrada_db,
                despachante_db=despachante_db,
                cifra=cifra,
                entrada=url_entrada,
                http=http,
                url_oauth=url_oauth,
                sistema_id=sistema_id,
                sistema_nome=nome_sistema,
                aplicativo_id=aplicativo_id,
                aplicativo_nome=nome_app,
                client_id=client_id,
                modelo_id=modelo_id,
                fatura=fatura,
            )
    finally:
        await http.aclose()
        for engine in (admin, entrada_db, despachante_db):
            await engine.dispose()


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Ambiente]:
    async with subir(banco_migrado, monkeypatch, tmp_path) as a:
        yield a


# --- Fluxo completo: o ADMIN configura pelas telas, a pessoa autoriza pelas telas, a leitura funciona ---


async def test_fluxo_completo_de_autorizar_pela_tela_ate_a_leitura(amb: Ambiente) -> None:
    sistema = f"/telas/admin/sistemas/{amb.sistema_id}"
    s = sufixo()
    chave_declaracoes = f"chave-so-declaracoes-{s}"
    with capture_logs() as logs:
        adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
        r = await adm.get(sistema)
        assert "Autorização e chaves deste sistema" in r.text
        r = await adm.get(f"{sistema}/acesso")
        assert r.status_code == 200
        assert VOLTA in r.text  # o endereco de volta que o ADMIN cadastra no sistema
        assert "Ainda não configurada" in r.text
        csrf = token_csrf(r.text)

        config = {
            "authorization_endpoint": f"{amb.url_oauth}/authorize",
            "token_endpoint": f"{amb.url_oauth}/token",
            "client_id": amb.oauth.client_id,
            "escopos": "leitura",
            "autenticacao_cliente": "client_secret_basic",
        }
        r = await adm.post(f"{sistema}/autorizacao", config | {"token_endpoint": "ftp://x"}, csrf=csrf)
        assert r.status_code == 400
        assert "https://" in r.text
        r = await adm.post(f"{sistema}/autorizacao", config, csrf=csrf)
        assert r.status_code == 303
        assert r.headers["location"] == f"{sistema}/acesso?aviso=autorizacao_configurada"
        async with amb.admin.connect() as c:
            gravada = (
                await c.execute(text("SELECT autorizacao FROM sistemas WHERE id = :s"), {"s": amb.sistema_id})
            ).scalar_one()
        assert gravada["client_id"] == amb.oauth.client_id
        assert gravada["escopos"] == ["leitura"]

        r = await adm.post(f"{sistema}/segredo", {"segredo": "tem espaco"}, csrf=csrf)
        assert r.status_code == 400
        r = await adm.post(f"{sistema}/segredo", {"segredo": amb.oauth.segredo}, csrf=csrf)
        assert r.status_code == 303
        r = await adm.post(f"{sistema}/chave", {"tipo": "compartilhada", "segredo": "x-y-z"}, csrf=csrf)
        assert r.status_code == 400  # chave compartilhada num sistema por pessoa apagaria o segredo do cliente
        assert "não combina" in r.text
        r = await adm.post(f"{sistema}/chave", {"tipo": "declaracoes", "segredo": chave_declaracoes}, csrf=csrf)
        assert r.status_code == 303
        ficha = await adm.get(f"{sistema}/acesso")
        assert "Último segredo entregue ao cofre em" in ficha.text
        assert "Última chave entregue ao cofre em" in ficha.text
        async with amb.despachante_db.connect() as c:  # o despachante abre o que as telas entregaram
            assert await autorizacao.abrir_segredo_cliente(c, amb.cifra, amb.sistema_id) == amb.oauth.segredo
            assert await chaves.abrir(c, amb.cifra, sistema_id=amb.sistema_id, tipo="declaracoes") == chave_declaracoes

        nav, sujeito, ana_id = await amb.nova_pessoa("Ana Teste")
        r = await nav.get("/telas/autorizar")
        assert "Nenhuma ordem sua valendo usa sistema" in r.text  # sem ordem, nada a autorizar
        await amb.dar_ordem(ana_id)
        r = await nav.get("/telas/autorizar")
        assert amb.sistema_nome in r.text
        assert "Você ainda não autorizou este sistema." in r.text

        r = await nav.get(await amb.link_autorizar(nav))
        assert r.status_code == 303  # redirecionamento do servidor, nao formulario para fora (CSP form-action)
        destino = r.headers["location"]
        assert destino.startswith(f"{amb.url_oauth}/authorize?")
        q = {k: v[0] for k, v in parse_qs(urlsplit(destino).query).items()}
        assert q["redirect_uri"] == VOLTA
        assert q["code_challenge_method"] == "S256"
        assert q["client_id"] == amb.oauth.client_id
        volta = await amb.na_pagina_do_sistema(destino, "ana")
        codigo = parse_qs(urlsplit(volta).query)["code"][0]
        r = await nav.get(volta)
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/telas/autorizar?aviso=autorizacao_guardada"  # o codigo sai da barra
        r = await nav.get(r.headers["location"])
        assert "Pronto. Sua autorização foi guardada, cifrada no cofre." in r.text
        assert "Você autorizou pela última vez em" in r.text
        paginas = [ficha.text, r.text]
        r = await nav.get(volta)  # recarregar a volta: o estado ja foi gasto, nada muda
        assert r.status_code == 403
        assert "já foi usado" in r.text

        guardadas = await amb.chaves_de(ana_id)
        assert set(guardadas) == {"acesso", "renovacao"}
        assert await amb.abrir(ana_id, guardadas["acesso"].cifrada) == "acesso-ana-1"
        resultado = await amb.ler_fatura(sujeito, "2026-10")
        assert resultado.is_error is False
        assert resultado.structured_content["valor"] == 99.9

        paginas.append(r.text)
        assert len(await amb.extrato("autorizacao_iniciada", ana_id)) == 1
        assert len(await amb.extrato("autorizacao_concluida", ana_id)) == 1
        frases = {linha.frase for linha in await amb.extrato("admin_segredo_cliente_entregue", admin_id)}
        assert frases == {
            f"Segredo do MCP-CLARO no sistema {amb.sistema_nome} entregue ao cofre por Carla Admin;"
            " as telas nao o guardam."
        }
        assert await amb.extrato("admin_autorizacao_configurada", admin_id)
        assert await amb.extrato("admin_chave_entregue", admin_id)

    # Nada de segredo em pagina, log ou extrato.
    segredos = [amb.oauth.segredo, chave_declaracoes, codigo, "acesso-ana-1", "renova-ana-1"]
    async with amb.admin.connect() as c:
        extrato = (
            await c.execute(
                text("SELECT string_agg(frase || ' ' || dados::text, ' ') FROM extrato WHERE sistema_id = :s"),
                {"s": amb.sistema_id},
            )
        ).scalar_one()
    texto_logs = json.dumps(logs, default=str)
    eventos = {linha.get("event") for linha in logs}
    assert {"telas_autorizacao_iniciada", "telas_autorizacao_concluida", "telas_admin_segredo_entregue"} <= eventos
    for segredo in segredos:
        assert segredo not in extrato
        assert segredo not in texto_logs
        for pagina in paginas:
            assert segredo not in pagina
    assert 'name="segredo" value' not in ficha.text  # o campo do segredo nunca vem preenchido


async def test_volta_com_estado_de_outra_pessoa_e_recusada(amb: Ambiente) -> None:
    await amb.configurar_direto()
    ana, _, ana_id = await amb.nova_pessoa("Ana")
    bia, _, bia_id = await amb.nova_pessoa("Bia")
    await amb.dar_ordem(ana_id)
    r = await ana.get(await amb.link_autorizar(ana))
    volta = await amb.na_pagina_do_sistema(r.headers["location"], "ana")

    r = await bia.get(volta)  # a volta da Ana caiu no navegador da Bia
    assert r.status_code == 403
    assert "é de outra pessoa" in r.text
    assert await amb.chaves_de(bia_id) == {}
    r = await ana.get(volta)  # a tentativa da outra pessoa gastou o estado: uso unico, conferido no despachante
    assert r.status_code == 403
    assert "já foi usado" in r.text
    assert await amb.chaves_de(ana_id) == {}
    recusas = await amb.extrato("autorizacao_recusada", bia_id)
    assert any("estado_de_outra_pessoa" in linha.frase for linha in recusas)

    r = await amb.navegador().get(volta)  # sem sessao, nada vai ao despachante
    assert r.status_code == 403
    assert "Sua sessão terminou" in r.text

    r = await ana.get("/telas/autorizar/volta?state=x&error=access_denied")
    assert r.status_code == 403
    assert "Nada foi guardado" in r.text


async def test_terceiro_com_a_chave_da_fase_3_desligada_tem_frase_propria(amb: Ambiente) -> None:
    """Servidor de terceiros com a chave da Fase 3 desligada: so pessoa de teste autoriza. A pessoa real ve a frase
    com acento (``telas.autorizar.MENSAGENS``), no comeco e na volta, e nada fica guardado."""
    frase = autorizar.MENSAGENS["terceiros_desligados"]
    assert amb.p.FEATURE_PHASE3_THIRD_PARTY is False
    async with amb.admin.begin() as c:
        terceiro_id = await t3.cadastrar_terceiro(
            c,
            nome=f"terceiro-{sufixo()}",
            tipo_chave="por_pessoa",
            autorizacao={
                "authorization_endpoint": f"{amb.url_oauth}/authorize",
                "token_endpoint": f"{amb.url_oauth}/token",
                "client_id": amb.oauth.client_id,
                "escopos": ["leitura"],
                "autenticacao_cliente": "none",
            },
        )
    async with amb.entrada_db.begin() as c:
        modelo_id = await repositorio.criar_modelo_ordem(
            c,
            nome=f"Terceiro {sufixo()}",
            descricao="Deixa o aplicativo ler no servidor de terceiros.",
            ferramentas=[(terceiro_id, "ler")],
            validade_dias=30,
            criado_por="admin-teste",
        )
    nav, _, pessoa_id = await amb.nova_pessoa("Ana")  # nasce de teste
    async with amb.entrada_db.begin() as c:
        await repositorio.conceder_ordem(
            c, pessoa_id=pessoa_id, aplicativo_id=amb.aplicativo_id, modelo_id=modelo_id, dias=7, max_dias=30
        )
    r = await nav.get("/telas/autorizar")
    achado = re.search(rf'href="(/telas/autorizar/{terceiro_id}/ir\?ficha=[0-9a-f]+)"', r.text)
    assert achado, "a lista nao trouxe o terceiro"
    link = achado.group(1)
    # De teste, comeca: o estado fica guardado no despachante.
    r = await nav.get(link)
    assert r.status_code == 303, r.text
    estado = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    async with amb.admin.begin() as c:
        await c.execute(text("UPDATE pessoas SET teste = false WHERE id = :p"), {"p": pessoa_id})
    # Pessoa real: o comeco e recusado com a frase propria.
    r = await nav.get(link)
    assert r.status_code == 403
    assert frase in r.text
    assert "estao desligados" not in r.text  # nunca a mensagem sem acento do despachante
    # E a volta do estado que ja estava guardado tambem.
    r = await nav.get(f"/telas/autorizar/volta?{urlencode({'state': estado, 'code': 'codigo-qualquer'})}")
    assert r.status_code == 403
    assert frase in r.text
    async with amb.admin.connect() as c:
        guardadas = (
            await c.execute(text("SELECT count(*) FROM chaves_cifradas WHERE sistema_id = :s"), {"s": terceiro_id})
        ).scalar_one()
    assert guardadas == 0
    recusas = await amb.extrato("autorizacao_recusada", pessoa_id)
    assert any("terceiros_desligados" in linha.frase for linha in recusas)  # a volta gastou o estado e registrou


async def test_ficha_do_link_impede_comecar_por_outro_site(amb: Ambiente) -> None:
    await amb.configurar_direto()
    ana, _, ana_id = await amb.nova_pessoa("Ana")
    bia, _, _ = await amb.nova_pessoa("Bia")
    await amb.dar_ordem(ana_id)
    link = await amb.link_autorizar(ana)
    for nav, caminho in (
        (ana, f"/telas/autorizar/{amb.sistema_id}/ir"),
        (ana, f"/telas/autorizar/{amb.sistema_id}/ir?ficha={'0' * 64}"),
        (bia, link),  # a ficha da Ana nao vale na sessao da Bia
    ):
        r = await nav.get(caminho)
        assert r.status_code in (403, 404), caminho
    async with amb.admin.connect() as c:
        pendentes = (
            await c.execute(
                text("SELECT count(*) FROM autorizacoes_pendentes WHERE sistema_id = :s"), {"s": amb.sistema_id}
            )
        ).scalar_one()
    assert pendentes == 0
    r = await amb.navegador().get(link)  # sem sessao: vai entrar, sem levar a ficha
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/login?voltar=%2Ftelas%2Fautorizar"


async def test_pessoa_retira_a_propria_autorizacao(amb: Ambiente) -> None:
    await amb.configurar_direto()
    nav, sujeito, ana_id = await amb.nova_pessoa("Ana")
    await amb.dar_ordem(ana_id)
    assert (await amb.autorizar_pela_tela(nav, "ana")).status_code == 303
    assert set(await amb.chaves_de(ana_id)) == {"acesso", "renovacao"}
    csrf = token_csrf((await nav.get("/telas/autorizar")).text)
    r = await nav.post(f"/telas/autorizar/{amb.sistema_id}/retirar")
    assert r.status_code == 403  # sem CSRF
    assert set(await amb.chaves_de(ana_id)) == {"acesso", "renovacao"}
    r = await nav.post(f"/telas/autorizar/{amb.sistema_id}/retirar", csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/autorizar?aviso=autorizacao_retirada"
    assert await amb.chaves_de(ana_id) == {}
    with pytest.raises(MCPError, match="ainda nao autorizou"):
        await amb.ler_fatura(sujeito)
    assert any("a pessoa retirou" in linha.frase for linha in await amb.extrato("autorizacao_derrubada", ana_id))


async def test_admin_desativa_pessoa_e_as_chaves_dela_somem(amb: Ambiente) -> None:
    await amb.configurar_direto()
    ana, sujeito_ana, ana_id = await amb.nova_pessoa("Ana")
    bia, sujeito_bia, bia_id = await amb.nova_pessoa("Bia")
    for nav, pessoa_id, usuario in ((ana, ana_id, "ana"), (bia, bia_id, "bia")):
        await amb.dar_ordem(pessoa_id)
        assert (await amb.autorizar_pela_tela(nav, usuario)).status_code == 303
    adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
    r = await adm.get("/telas/admin/pessoas")
    assert r.status_code == 200
    assert "Ana" in r.text
    csrf = token_csrf(r.text)
    desativar = f"/telas/admin/pessoas/{ana_id}/desativar"

    assert (await adm.post(desativar, {"confirmo": "sim"})).status_code == 403  # sem CSRF
    assert (await adm.post(desativar, csrf=csrf)).status_code == 400  # sem confirmar
    assert (
        await adm.post(f"/telas/admin/pessoas/{admin_id}/desativar", {"confirmo": "sim"}, csrf=csrf)
    ).status_code == 400
    assert set(await amb.chaves_de(ana_id)) == {"acesso", "renovacao"}
    r = await bia.post(desativar, {"confirmo": "sim"}, csrf=token_csrf((await bia.get("/telas/ordens")).text))
    assert r.status_code == 403  # pessoa comum nao desativa ninguem

    r = await adm.post(desativar, {"confirmo": "sim"}, csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/admin/pessoas?aviso=pessoa_desativada"
    assert await amb.chaves_de(ana_id) == {}
    assert set(await amb.chaves_de(bia_id)) == {"acesso", "renovacao"}
    async with amb.admin.connect() as c:
        situacao = (await c.execute(text("SELECT situacao FROM pessoas WHERE id = :p"), {"p": ana_id})).scalar_one()
        abertas = (
            await c.execute(
                text("SELECT count(*) FROM sessoes_telas WHERE pessoa_id = :p AND encerrada_em IS NULL"), {"p": ana_id}
            )
        ).scalar_one()
    assert situacao == "desativada"
    assert abertas == 0
    assert (await ana.get("/telas/ordens")).status_code == 303  # a sessao dela acabou
    linhas = await amb.extrato("pessoa_desativada", ana_id)
    assert [linha.frase for linha in linhas] == ["Conta desativada pelo ADMIN Carla Admin."]
    assert linhas[0].dados["admin_id"] == str(admin_id)
    derrubadas = await amb.extrato("autorizacao_derrubada", ana_id)
    assert [d.frase for d in derrubadas] == [
        f"Autorizacao da pessoa no sistema {amb.sistema_nome} derrubada: pessoa desativada."
    ]
    with pytest.raises(MCPError, match="desativada"):
        await amb.ler_fatura(sujeito_ana)
    assert (await amb.ler_fatura(sujeito_bia, "2026-11")).structured_content["valor"] == 59.0

    r = await adm.get("/telas/admin/pessoas")  # desativada: o botao vira "derrubar de novo", e nao muda nada
    assert "Derrubar as autorizações de novo" in r.text
    assert (await adm.post(desativar, {"confirmo": "sim"}, csrf=csrf)).status_code == 303
    assert len(await amb.extrato("pessoa_desativada", ana_id)) == 1


async def test_admin_entrega_chave_compartilhada_e_de_programa_sem_reexibir(amb: Ambiente) -> None:
    s = sufixo()
    async with amb.entrada_db.begin() as c:
        compartilhado = await repositorio.cadastrar_sistema(
            c, nome=f"compartilhado-{s}", endereco="https://compartilhado.exemplo/mcp", tipo_chave="compartilhada"
        )
        do_programa = await repositorio.cadastrar_sistema(
            c, nome=f"do-programa-{s}", endereco="https://programa.exemplo/mcp", tipo_chave="programa"
        )
        programa_id = await repositorio.habilitar_aplicativo(
            c, client_id=f"robo-{s}", nome=f"Robo {s}", familia="programa", tipo="programa"
        )
    adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
    chave_comum, chave_robo = f"chave-comum-{s}", f"chave-robo-{s}"

    r = await adm.get(f"/telas/admin/sistemas/{compartilhado}/acesso")
    assert r.status_code == 200
    assert "Chave compartilhada" in r.text
    assert "Nenhuma chave entregue ainda." in r.text
    assert "Segredo do MCP-CLARO no sistema" not in r.text
    csrf = token_csrf(r.text)
    base = f"/telas/admin/sistemas/{compartilhado}"
    r = await adm.post(f"{base}/segredo", {"segredo": "qualquer"}, csrf=csrf)
    assert r.status_code == 400
    assert "só serve para sistema com uma chave por pessoa" in r.text
    r = await adm.post(
        f"{base}/autorizacao",
        {"authorization_endpoint": "https://a.exemplo/a", "token_endpoint": "https://a.exemplo/t", "client_id": "x"},
        csrf=csrf,
    )
    assert r.status_code == 400
    r = await adm.post(f"{base}/chave", {"tipo": "programa", "segredo": chave_comum}, csrf=csrf)
    assert r.status_code == 400
    r = await adm.post(f"{base}/chave", {"tipo": "compartilhada", "segredo": chave_comum}, csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == f"{base}/acesso?aviso=chave_guardada"
    r = await adm.get(r.headers["location"])
    assert "A chave foi entregue ao cofre." in r.text
    assert "Última chave entregue ao cofre em" in r.text
    assert chave_comum not in r.text

    r = await adm.get(f"/telas/admin/sistemas/{do_programa}/acesso")
    assert r.status_code == 200
    assert f"Robo {s}" in r.text
    base = f"/telas/admin/sistemas/{do_programa}"
    r = await adm.post(f"{base}/chave", {"tipo": "programa", "segredo": chave_robo}, csrf=csrf)
    assert r.status_code == 400
    assert "Escolha o programa da lista." in r.text
    r = await adm.post(
        f"{base}/chave", {"tipo": "programa", "aplicativo": str(programa_id), "segredo": chave_robo}, csrf=csrf
    )
    assert r.status_code == 303
    assert chave_robo not in (await adm.get(f"{base}/acesso")).text

    async with amb.despachante_db.connect() as c:
        assert await chaves.abrir(c, amb.cifra, sistema_id=compartilhado) == chave_comum
        assert await chaves.abrir(c, amb.cifra, sistema_id=do_programa, aplicativo_id=programa_id) == chave_robo
    async with amb.admin.connect() as c:
        linhas = (
            await c.execute(
                text("SELECT tipo, frase, dados::text AS dados FROM extrato WHERE sistema_id = ANY(:s) ORDER BY id"),
                {"s": [compartilhado, do_programa]},
            )
        ).all()
    assert [(linha.tipo, linha.frase) for linha in linhas if linha.tipo.endswith("chave_entregue")] == [
        (
            "admin_chave_entregue",
            f"Chave compartilhada do sistema compartilhado-{s} entregue ao cofre por Carla Admin;"
            " as telas nao a guardam.",
        ),
        (
            "admin_chave_entregue",
            f"Chave de programa do sistema do-programa-{s} entregue ao cofre por Carla Admin; as telas nao a guardam.",
        ),
    ]
    assert [linha.frase for linha in linhas if linha.tipo == "chave_guardada"] == [
        "Chave compartilhada guardada cifrada no cofre por Carla Admin.",
        "Chave programa guardada cifrada no cofre por Carla Admin.",
    ]
    for linha in linhas:
        assert chave_comum not in linha.frase + linha.dados
        assert chave_robo not in linha.frase + linha.dados
    assert len(await amb.extrato("admin_chave_entregue", admin_id)) == 2
