"""Provas de saida de "Provar a base": um pedido de leitura de ponta a ponta, e todas as recusas.

Sobe de verdade: PostgreSQL 18, provedor de login falso, sistema de exemplo, despachante e
entrada, e fala com o cliente oficial do MCP fixado em 2026-07-28.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from exemplos import leitura_por_pessoa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.medicoes.contadores import CONTADORES
from tests.apoio import cabecalhos_2026, cliente_mcp, pedido_2026, porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_falso import IdpFalso

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"


@dataclass
class Ambiente:
    p: Parametros
    idp: IdpFalso
    entrada: str
    despachante: str
    sistema: str
    sistema_app: Any
    sistema_id: uuid.UUID
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    modelo_id: uuid.UUID
    ordem_id: uuid.UUID
    cifra: Any
    client_id: str

    def cracha(self, **kw: Any) -> str:
        kw.setdefault("audiencia", RECURSO)
        kw.setdefault("client_id", self.client_id)
        return self.idp.cracha(**kw)


META_DO_SISTEMA = {"sistema.exemplo/contato": "ana@exemplo.test", "sistema.exemplo/documento": "123.456.789-09"}


def com_meta_nos_blocos(app: Any) -> Any:
    """O sistema de exemplo, com um ``_meta`` de bloco cheio de dado pessoal em cada resposta de ``tools/call``
    (Fase 3: o ``_meta`` de bloco nunca passa ao aplicativo)."""

    async def embrulho(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await app(scope, receive, send)
            return
        inicio: dict[str, Any] = {}
        partes: list[bytes] = []

        async def guardar(mensagem: dict[str, Any]) -> None:
            if mensagem["type"] == "http.response.start":
                inicio.update(mensagem)
                return
            partes.append(mensagem.get("body", b""))
            if mensagem.get("more_body"):
                return
            corpo = b"".join(partes)
            try:
                dados = json.loads(corpo)
                for bloco in dados["result"]["content"]:
                    bloco["_meta"] = dict(META_DO_SISTEMA)
                corpo = json.dumps(dados).encode()
            except ValueError, KeyError, TypeError:
                pass
            cabecalhos = [(k, v) for k, v in inicio.get("headers", []) if k.lower() != b"content-length"]
            await send({**inicio, "headers": [*cabecalhos, (b"content-length", str(len(corpo)).encode())]})
            await send({"type": "http.response.body", "body": corpo})

        await app(scope, receive, guardar)

    return embrulho


@contextlib.asynccontextmanager
async def subir(banco_migrado: Parametros) -> AsyncIterator[Ambiente]:
    porta_idp = porta_livre()
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    base = banco_migrado.model_dump()
    sistema_app = leitura_por_pessoa.criar()
    porta_desp = porta_livre()
    p = Parametros(
        **(
            base
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
    engine = criar_engine(p, pool_size=5, max_overflow=5)
    cifra = CifraFalsa()
    async with (
        servidor(idp.app(), porta_idp),
        servidor(com_meta_nos_blocos(sistema_app)) as url_sistema,
        servidor(criar_app_despachante(p, engine, cifra), porta_desp) as url_desp,
    ):
        despachante = ClienteDespachante(p)
        async with servidor(criar_app_entrada(p, engine)) as url_entrada:
            sufixo = uuid.uuid4().hex[:8]
            async with engine.begin() as c:
                app_id = await repositorio.habilitar_aplicativo(
                    c, client_id=f"claude-code-{sufixo}", nome="Claude Code", familia="claude-code"
                )
                sistema_id = await repositorio.cadastrar_sistema(
                    c, nome=f"leitura-{sufixo}", endereco=f"{url_sistema}/mcp", tipo_chave="por_pessoa"
                )
                pessoa = await repositorio.pessoa_no_login(c, idp.emissor, "pessoa-ana", "Ana")
                await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": pessoa.id})
            sondagem = await despachante.post(
                "/interno/sondar", {"sistema_id": str(sistema_id), "chave_sondagem": "chave-ana"}
            )
            assert sondagem["aceito"], sondagem
            declaradas = (
                await despachante.post(
                    "/interno/declaracoes", {"sistema_id": str(sistema_id), "chave_sondagem": "chave-ana"}
                )
            )["ferramentas"]
            por_nome = {f["name"]: f for f in declaradas}
            async with engine.begin() as c:
                await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=sistema_id,
                    definicao_declarada=por_nome["consultar_fatura"],
                    nome_vitrine=f"fatura_{sufixo}",
                    descricao="Mostra a sua fatura de um mes.",
                    classe="le",
                    revisado_por="admin-teste",
                    risco="baixo",
                    campos_pessoais=["cpf"],
                )
                await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=sistema_id,
                    definicao_declarada=por_nome["ver_cadastro"],
                    nome_vitrine=f"cadastro_{sufixo}",
                    descricao="Mostra seus dados de cadastro.",
                    classe="le",
                    revisado_por="admin-teste",
                    campos_pessoais=["cpf", "email"],
                )
                modelo = await repositorio.criar_modelo_ordem(
                    c,
                    nome=f"leitura-teste-{sufixo}",
                    descricao="Ordem de servico de teste",
                    ferramentas=[(sistema_id, "consultar_fatura"), (sistema_id, "ver_cadastro")],
                    validade_dias=30,
                    criado_por="admin-teste",
                )
                ordem = await repositorio.conceder_ordem(
                    c, pessoa_id=pessoa.id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
                )
            # Chave de pessoa nasce da autorizacao dela no sistema (Fase 1); aqui, direto, como o despachante faria.
            from mcp_claro.despachante import chaves

            async with engine.begin() as c:
                await chaves.guardar(c, cifra, sistema_id=sistema_id, pessoa_id=pessoa.id, segredo="chave-ana")
            await despachante.fechar()
            try:
                yield Ambiente(
                    p,
                    idp,
                    url_entrada,
                    url_desp,
                    url_sistema,
                    sistema_app,
                    sistema_id,
                    pessoa.id,
                    app_id,
                    modelo,
                    ordem,
                    cifra,
                    f"claude-code-{sufixo}",
                )
            finally:
                await engine.dispose()


@pytest.fixture
async def amb(banco_migrado: Parametros) -> AsyncIterator[Ambiente]:
    async with subir(banco_migrado) as a:
        yield a


async def test_leitura_de_ponta_a_ponta(amb: Ambiente) -> None:
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        nomes = sorted(t.name for t in (await c.list_tools()).tools)
        assert len(nomes) == 2
        assert all("_" in n for n in nomes)
        fatura = next(n for n in nomes if n.startswith("fatura_"))
        r = await c.call_tool(fatura, {"mes": "2026-10"})
        assert r.is_error is False
        assert r.structured_content["valor"] == 99.9
        assert r.structured_content["cpf"] == "***09"  # leitura com medida
        assert r.meta
        assert r.meta["br.com.claro.mcp-claro/sistema"].startswith("leitura-")


async def test_vitrine_usa_textos_do_cadastro(amb: Ambiente) -> None:
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        ferramentas = (await c.list_tools()).tools
        descricoes = {t.description for t in ferramentas}
        assert "Mostra a sua fatura de um mes." in descricoes
        assert not any("propria pessoa" in (d or "") for d in descricoes)


async def test_cracha_do_aplicativo_nao_chega_ao_sistema(amb: Ambiente) -> None:
    cracha = amb.cracha()
    async with cliente_mcp(f"{amb.entrada}/mcp", cracha) as c:
        nome = next(t.name for t in (await c.list_tools()).tools if t.name.startswith("fatura_"))
        await c.call_tool(nome, {"mes": "2026-10"})
    vistas = amb.sistema_app.state.contagem.autorizacoes_vistas
    assert vistas
    assert all(v == "Bearer chave-ana" for v in vistas)
    assert not any(cracha in v for v in vistas)


@pytest.mark.parametrize(
    ("caso", "kwargs"),
    [
        ("outro condominio", {"audiencia": "https://outro-servico.exemplo/mcp"}),
        ("vencido", {"validade": -120}),
        ("outro emissor", {"emissor": "https://login-falso.exemplo/realms/x"}),
    ],
)
async def test_cracha_recusado(amb: Ambiente, caso: str, kwargs: dict[str, Any]) -> None:
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{amb.entrada}/mcp",
            json=pedido_2026("tools/list"),
            headers=cabecalhos_2026("tools/list", chave=amb.cracha(**kwargs)),
        )
    assert r.status_code == 401, caso
    assert "resource_metadata=" in r.headers["www-authenticate"]


async def test_cracha_assinado_por_outra_chave(amb: Ambiente) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    falso = amb.cracha(chave=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{amb.entrada}/mcp",
            json=pedido_2026("tools/list"),
            headers=cabecalhos_2026("tools/list", chave=falso),
        )
    assert r.status_code == 401


async def test_sem_cracha_401_com_metadados(amb: Ambiente) -> None:
    async with httpx2.AsyncClient() as http:
        r = await http.post(f"{amb.entrada}/mcp", json=pedido_2026("tools/list"), headers=cabecalhos_2026("tools/list"))
        assert r.status_code == 401
        meta = (await http.get(f"{amb.entrada}/.well-known/oauth-protected-resource/mcp")).json()
    assert meta["resource"] == RECURSO
    assert meta["authorization_servers"] == [amb.idp.emissor]


async def test_aplicativo_da_geracao_antiga_recebe_recusa_legivel_e_conta(amb: Ambiente) -> None:
    antes = CONTADORES.recusas_pendentes().get(("aplicativo", amb.client_id, "2025-11-25", "initialize"), 0)
    with pytest.raises(BaseException) as erro:  # noqa: PT011 - o kit embrulha em ExceptionGroup
        async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(), modo="legacy") as c:
            await c.list_tools()
    assert "-32022" in repr(erro.value) or "Unsupported protocol version" in repr(erro.value)
    depois = CONTADORES.recusas_pendentes().get(("aplicativo", amb.client_id, "2025-11-25", "initialize"), 0)
    assert depois == antes + 1


async def test_pedido_na_versao_2025_11_25_recebe_32022(amb: Ambiente) -> None:
    async with httpx2.AsyncClient() as http:
        h = cabecalhos_2026("tools/list", chave=amb.cracha()) | {"mcp-protocol-version": "2025-11-25"}
        r = await http.post(f"{amb.entrada}/mcp", json=pedido_2026("tools/list"), headers=h)
        assert r.status_code == 400
        erro = r.json()["error"]
        assert erro["code"] == -32022
        assert erro["data"] == {"supported": ["2026-07-28"], "requested": "2025-11-25"}
        assert "2026-07-28" in erro["message"]
        for metodo in ("GET", "DELETE"):
            r = await http.request(metodo, f"{amb.entrada}/mcp", headers={"authorization": f"Bearer {amb.cracha()}"})
            assert r.status_code == 405


async def test_ferramenta_fora_da_vitrine_e_argumento_fora_do_esquema(amb: Ambiente) -> None:
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        with pytest.raises(MCPError, match="nao esta na sua vitrine"):
            await c.call_tool("listar_chamados", {})
        nome = next(t.name for t in (await c.list_tools()).tools if t.name.startswith("fatura_"))
        with pytest.raises(MCPError, match="fora do esquema"):
            await c.call_tool(nome, {"mes": "outubro"})
        with pytest.raises(MCPError, match="fora do esquema"):
            await c.call_tool(nome, {"mes": "2026-10", "extra": 1})


async def test_pessoa_que_nao_e_de_teste_com_fase1_desligada_e_recusada(amb: Ambiente) -> None:
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(sujeito="pessoa-sem-teste")) as c:
        with pytest.raises(MCPError, match="Fase 1"):
            await c.list_tools()


async def test_uma_pessoa_nao_alcanca_o_que_e_de_outra(amb: Ambiente) -> None:
    from mcp_claro.despachante import chaves

    engine = criar_engine(amb.p, pool_size=1, max_overflow=0)
    async with engine.begin() as c:
        bia = await repositorio.pessoa_no_login(c, amb.idp.emissor, "pessoa-bia", "Bia")
        await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": bia.id})
        modelo_bia = await repositorio.criar_modelo_ordem(
            c,
            nome=f"so-cadastro-{uuid.uuid4().hex[:6]}",
            descricao="So cadastro",
            ferramentas=[(amb.sistema_id, "ver_cadastro")],
            validade_dias=30,
            criado_por="admin-teste",
        )
        await repositorio.conceder_ordem(
            c, pessoa_id=bia.id, aplicativo_id=amb.aplicativo_id, modelo_id=modelo_bia, dias=7, max_dias=30
        )
        await chaves.guardar(c, amb.cifra, sistema_id=amb.sistema_id, pessoa_id=bia.id, segredo="chave-bia")
    await engine.dispose()
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(sujeito="pessoa-bia")) as c:
        nomes = [t.name for t in (await c.list_tools()).tools]
        assert len(nomes) == 1
        assert nomes[0].startswith("cadastro_")
        r = await c.call_tool(nomes[0], {})
        assert r.structured_content["nome"] == "Bia Teste"
        assert r.structured_content["cpf"] != "987.654.321-00"
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        fatura_da_ana = next(t.name for t in (await c.list_tools()).tools if t.name.startswith("fatura_"))
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(sujeito="pessoa-bia")) as c:
        with pytest.raises(MCPError, match="nao esta na sua vitrine"):
            await c.call_tool(fatura_da_ana, {"mes": "2026-10"})


@pytest.mark.parametrize(
    "mudanca",
    [
        "UPDATE ordens_servico SET fim = now() - interval '1 second', inicio = now() - interval '2 days'"
        " WHERE id = :ordem",
        "UPDATE ordens_servico SET situacao = 'revogada' WHERE id = :ordem",
        "UPDATE modelos_ordem SET ativo = false WHERE id = :modelo",
        "UPDATE sistemas SET situacao = 'quarentena' WHERE id = :sistema",
        "UPDATE sistemas SET situacao = 'recusado' WHERE id = :sistema",
        "UPDATE ferramentas_revisadas SET situacao = 'suspensa' WHERE sistema_id = :sistema",
    ],
)
async def test_vitrine_vazia_quando_nada_mais_vale(amb: Ambiente, mudanca: str) -> None:
    engine = criar_engine(amb.p, pool_size=1, max_overflow=0)
    async with engine.begin() as c:
        await c.execute(text(mudanca), {"ordem": amb.ordem_id, "modelo": amb.modelo_id, "sistema": amb.sistema_id})
    await engine.dispose()
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        assert (await c.list_tools()).tools == []


async def test_aplicativo_pausado_e_recusado(amb: Ambiente) -> None:
    engine = criar_engine(amb.p, pool_size=1, max_overflow=0)
    async with engine.begin() as c:
        await c.execute(text("UPDATE aplicativos SET situacao = 'pausado' WHERE id = :a"), {"a": amb.aplicativo_id})
    await engine.dispose()
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        with pytest.raises(MCPError, match="pausado"):
            await c.list_tools()


async def test_despachante_recusa_versao_substituida_da_ferramenta(amb: Ambiente) -> None:
    engine = criar_engine(amb.p, pool_size=1, max_overflow=0)
    async with engine.begin() as c:
        antiga = (
            await c.execute(
                text(
                    "SELECT id FROM ferramentas_revisadas WHERE sistema_id = :s AND nome_original = 'consultar_fatura'"
                    " AND situacao = 'ativa'"
                ),
                {"s": amb.sistema_id},
            )
        ).scalar_one()
        definicao = dict(
            (
                await c.execute(
                    text(
                        "SELECT definicao FROM ferramentas_vistas WHERE sistema_id = :s AND nome = 'consultar_fatura'"
                    ),
                    {"s": amb.sistema_id},
                )
            ).scalar_one()
        )
        await repositorio.revisar_ferramenta(
            c,
            sistema_id=amb.sistema_id,
            definicao_declarada=definicao,
            nome_vitrine=f"fatura_v2_{uuid.uuid4().hex[:4]}",
            descricao="Agora e acao.",
            classe="muda",
            revisado_por="admin-teste",
        )
    await engine.dispose()
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{amb.despachante}/interno/ler",
            json={
                "pessoa_id": str(amb.pessoa_id),
                "aplicativo_id": str(amb.aplicativo_id),
                "ordem_id": str(amb.ordem_id),
                "ferramenta_id": str(antiga),
                "argumentos": {"mes": "2026-10"},
            },
        )
    assert r.status_code == 403
    assert amb.sistema_app.state.contagem.chamadas.get("consultar_fatura", 0) == 0


async def test_entrada_nao_ativa_sistema_nem_muda_classe(amb: Ambiente) -> None:
    from sqlalchemy.exc import DBAPIError

    engine = criar_engine(amb.p, pool_size=1, max_overflow=0)
    async with engine.connect() as c:
        await c.execute(text("SET ROLE mcp_claro_entrada"))
        with pytest.raises(DBAPIError):
            await c.execute(
                text("UPDATE ferramentas_revisadas SET classe = 'le' WHERE sistema_id = :s"), {"s": amb.sistema_id}
            )
        await c.rollback()
        await c.execute(text("SET ROLE mcp_claro_entrada"))
        with pytest.raises(DBAPIError):
            await c.execute(text("UPDATE sistemas SET versao_mcp = '2026-07-28' WHERE id = :s"), {"s": amb.sistema_id})
        await c.rollback()
        await c.execute(text("SET ROLE mcp_claro_entrada"))
        await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), {"s": amb.sistema_id})
        with pytest.raises(DBAPIError, match="sondagem"):
            await c.execute(text("UPDATE sistemas SET situacao = 'ativo' WHERE id = :s"), {"s": amb.sistema_id})
        await c.rollback()
    await engine.dispose()


async def test_dado_pessoal_nao_vaza_por_texto_nem_por_meta(amb: Ambiente) -> None:
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha()) as c:
        nome = next(t.name for t in (await c.list_tools()).tools if t.name.startswith("cadastro_"))
        r = await c.call_tool(nome, {})
    textos = " ".join(b.text for b in r.content if hasattr(b, "text"))
    assert "ana@exemplo.test" not in textos
    assert "123.456.789-09" not in str(r.model_dump())
    # O sistema pos um _meta com dado pessoal em cada bloco: nenhum passa (Fase 3).
    assert r.content
    assert all(b.meta is None for b in r.content)
    assert "sistema.exemplo/" not in str(r.model_dump())
    # So chaves do proprio MCP-CLARO: as nossas e o carimbo serverInfo que o kit poe com o nome do gateway.
    meta = dict(r.meta or {})
    assert meta.pop("io.modelcontextprotocol/serverInfo", {}).get("name") == "mcp-claro"
    assert all(k.startswith("br.com.claro.mcp-claro/") for k in meta)


async def test_acao_pela_porta_de_leitura_e_recusada_pelo_despachante(amb: Ambiente, banco_migrado: Parametros) -> None:
    engine = criar_engine(amb.p, pool_size=1, max_overflow=0)
    async with engine.begin() as c:
        ferramenta = (
            await c.execute(
                text(
                    "SELECT id FROM ferramentas_revisadas WHERE sistema_id = :s AND nome_original = 'consultar_fatura'"
                    " AND situacao = 'ativa'"
                ),
                {"s": amb.sistema_id},
            )
        ).scalar_one()
        ordem = (
            await c.execute(
                text("SELECT id FROM ordens_servico WHERE pessoa_id = :p ORDER BY criado_em DESC"),
                {"p": amb.pessoa_id},
            )
        ).scalar()
        # Alteracao de proposito no cadastro: a entrada acharia que e leitura, mas o despachante reconfere.
        await c.execute(text("UPDATE ferramentas_revisadas SET classe = 'muda' WHERE id = :f"), {"f": ferramenta})
    await engine.dispose()
    async with httpx2.AsyncClient() as http:
        r = await http.post(
            f"{amb.despachante}/interno/ler",
            json={
                "pessoa_id": str(amb.pessoa_id),
                "aplicativo_id": str(amb.aplicativo_id),
                "ordem_id": str(ordem),
                "ferramenta_id": str(ferramenta),
                "argumentos": {"mes": "2026-10"},
            },
        )
    assert r.status_code == 403
    assert r.json()["motivo"] == "acao_pela_porta_de_leitura"
    assert amb.sistema_app.state.contagem.chamadas.get("consultar_fatura", 0) == 0


async def test_sistema_que_pede_dados_conta_uma_chamada_so(banco_migrado: Parametros) -> None:
    from exemplos import mal_comportado
    from mcp_claro.ligacao import chamar_ferramenta
    from tests.apoio_g3a import ContaPosts

    app = ContaPosts(mal_comportado.criar())

    async def chave() -> str:
        return "chave-ana"

    posts: dict[str, int] = {}
    async with servidor(app) as base, httpx2.AsyncClient() as http:
        for modo in ("pede_dados", "recusa_chave", "pede_pelo_fio_antigo_e_401", "pede_link"):
            await http.post(f"{base}/modo", json={"modo": modo, "atraso_fio": 0.1})
            app.zerar()
            await chamar_ferramenta(f"{base}/mcp", chave, None, "consultar_pedido", {"pedido": "1"})
            posts[modo] = app.posts
        contagem = (await http.get(f"{base}/contagem")).json()["chamadas"]
    # Todos os POSTs que chegaram ao sistema, nao so as chamadas: um por chamada, em todo modo.
    assert posts == {"pede_dados": 1, "recusa_chave": 1, "pede_pelo_fio_antigo_e_401": 1, "pede_link": 1}
    assert contagem.get("consultar_pedido", 0) == 3  # pede_dados, fio antigo e link: sem segunda volta
    assert contagem.get("http401", 0) == 1  # 401: sem reenvio; o do fio antigo nem chegou
    assert contagem.get("resposta_do_cliente", 0) == 0


async def test_sistema_da_geracao_antiga_e_recusado_no_cadastro_e_contado(banco_migrado: Parametros) -> None:
    from tests.apoio import sistema_geracao_antiga

    engine = criar_engine(banco_migrado, pool_size=1, max_overflow=0)
    from mcp_claro.despachante.servico import Despachante

    despachante = Despachante(engine, CifraFalsa())
    with sistema_geracao_antiga() as url:
        async with engine.begin() as c:
            sistema_id = await repositorio.cadastrar_sistema(
                c, nome=f"antigo-{uuid.uuid4().hex[:6]}", endereco=url, tipo_chave="compartilhada"
            )
        antes = sum(n for (lado, *_), n in CONTADORES.recusas_pendentes().items() if lado == "sistema")
        sondagem = await despachante.sondar_sistema(sistema_id, None)
    assert not sondagem.aceito
    assert not sondagem.inalcancavel
    assert "2026-07-28" in sondagem.motivo
    depois = sum(n for (lado, *_), n in CONTADORES.recusas_pendentes().items() if lado == "sistema")
    assert depois == antes + 1
    async with engine.connect() as c:
        linha = (await c.execute(text("SELECT situacao, motivo FROM sistemas WHERE id = :i"), {"i": sistema_id})).one()
        frase = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE sistema_id = :i AND tipo = 'recusa_versao_sistema'"),
                {"i": sistema_id},
            )
        ).scalar_one()
    await engine.dispose()
    assert linha.situacao == "recusado"
    assert "so aceitamos 2026-07-28" in frase
