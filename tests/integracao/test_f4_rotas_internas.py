"""Texto que o banco nao guarda nas rotas internas do despachante (F4, etapa 4f; pendencia 2 do G3r).

O caractere nulo (U+0000), que o PostgreSQL recusa em texto e jsonb, e um pedaco de par UTF-16 sozinho (``\\ud800``,
que o JSON aceita e o UTF-8 nao escreve) num texto de primeiro nivel do corpo de qualquer rota ``/interno/*`` do
despachante: 400 ``pedido_invalido``, antes de a rota rodar, nunca erro 500, e nada muda (nem chave, nem extrato, nem
pedido ao sistema). Antes, davam 500: ``quem`` e ``segredo`` em ``/interno/chaves``, ``trace_id`` em
``/interno/ler``, ``estado`` em ``/interno/autorizacao/concluir`` e ``motivo`` em ``/interno/autorizacao/derrubar``.
Um ``trace_id``, uma ``chave_sondagem`` ou um ``quem`` que nem e texto tambem e pedido invalido.

Os argumentos de uma leitura ficam fora da regra: vao ao sistema, nao ao banco.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import mal_comportado
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante.app import criar_app_despachante
from tests import apoio_g3a as g
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

CONSULTAR = {
    "name": "consultar_pedido",
    "description": "Consulta a situacao de um pedido.",
    "inputSchema": {"type": "object", "properties": {"pedido": {"type": "string", "maxLength": 40}}},
}
ESTRANHOS = pytest.mark.parametrize("estranho", ["\x00", "\ud800"], ids=["nulo", "surrogate"])


@dataclass
class Cena:
    dono: AsyncEngine
    http: httpx2.AsyncClient
    sistema: g.Arranjo
    com_autorizacao: g.Arranjo  # proprio por pessoa, com a chave da pessoa guardada
    contagem: g.ContaPosts

    def ler(self, **campos: Any) -> dict[str, Any]:
        return {
            "pessoa_id": str(self.sistema.pessoa_id),
            "aplicativo_id": str(self.sistema.aplicativo_id),
            "ordem_id": str(self.sistema.ordem_id),
            "ferramenta_id": str(self.sistema.ferramenta_id),
            "argumentos": {"pedido": "1"},
            **campos,
        }

    async def postar(self, rota: str, corpo: dict[str, Any]) -> httpx2.Response:
        # json.dumps escreve o pedaco sozinho como o escape \ud800 (ASCII), como um cliente qualquer mandaria.
        return await self.http.post(rota, content=json.dumps(corpo), headers={"content-type": "application/json"})

    async def retrato(self) -> tuple[int, int, int]:
        """Linhas do extrato e chaves guardadas dos dois sistemas e das duas pessoas, e os POSTs ao sistema."""
        sistemas = [self.sistema.sistema_id, self.com_autorizacao.sistema_id]
        pessoas = [self.sistema.pessoa_id, self.com_autorizacao.pessoa_id]
        async with self.dono.connect() as c:
            extrato = (
                await c.execute(
                    text("SELECT count(*) FROM extrato WHERE sistema_id = ANY(:s) OR pessoa_id = ANY(:p)"),
                    {"s": sistemas, "p": pessoas},
                )
            ).scalar_one()
            guardadas = (
                await c.execute(
                    text("SELECT count(*) FROM chaves_cifradas WHERE sistema_id = ANY(:s)"), {"s": sistemas}
                )
            ).scalar_one()
        return int(extrato), int(guardadas), self.contagem.posts


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    dono = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    cifra = CifraFalsa()
    contagem = g.ContaPosts(mal_comportado.criar())
    try:
        await g.abrir_a_saida(dono)
        async with servidor(contagem) as base:
            endereco = f"{base}/mcp"
            sistema = await g.preparar(dono, cifra, endereco=endereco, definicao=CONSULTAR)
            com_autorizacao = await g.preparar(
                dono, cifra, endereco=endereco, definicao=CONSULTAR, tipo_chave="por_pessoa"
            )
            app = criar_app_despachante(banco_migrado.model_copy(update={"APP_ENV": "teste"}), desp_db, cifra)
            # Sem levantar o erro do app: o erro interno chega como 500, como chegaria a quem chamou.
            transporte = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
                contagem.zerar()
                yield Cena(dono, http, sistema, com_autorizacao, contagem)
    finally:
        await dono.dispose()
        await desp_db.dispose()


Corpo = Callable[[Cena, str], tuple[str, dict[str, Any]]]

CASOS: dict[str, Corpo] = {
    # Davam 500 antes: o texto ia ao extrato (jsonb), a coluna de texto do extrato ou a uma busca no banco.
    "chaves.quem": lambda c, x: (
        "/interno/chaves",
        {"sistema_id": str(c.sistema.sistema_id), "segredo": "k", "quem": x},
    ),
    "chaves.segredo": lambda c, x: ("/interno/chaves", {"sistema_id": str(c.sistema.sistema_id), "segredo": f"k{x}"}),
    "ler.trace_id": lambda c, x: ("/interno/ler", c.ler(trace_id=f"t{x}")),
    "concluir.estado": lambda c, x: (
        "/interno/autorizacao/concluir",
        {"pessoa_id": str(c.sistema.pessoa_id), "estado": f"e{x}", "codigo": "abc"},
    ),
    "derrubar.motivo": lambda c, x: (
        "/interno/autorizacao/derrubar",
        {"pessoa_id": str(c.com_autorizacao.pessoa_id), "motivo": f"m{x}"},
    ),
    # Nao davam 500, mas o texto nao serve a ninguem: agora tambem pedido invalido, sem nada pedido ao sistema.
    "chaves.tipo": lambda c, x: (
        "/interno/chaves",
        {"sistema_id": str(c.sistema.sistema_id), "segredo": "k", "tipo": x},
    ),
    "sondar.chave": lambda c, x: ("/interno/sondar", {"sistema_id": str(c.sistema.sistema_id), "chave_sondagem": x}),
    "declaracoes.chave": lambda c, x: (
        "/interno/declaracoes",
        {"sistema_id": str(c.sistema.sistema_id), "chave_sondagem": f"k{x}"},
    ),
    "concluir.codigo": lambda c, x: (
        "/interno/autorizacao/concluir",
        {"pessoa_id": str(c.sistema.pessoa_id), "estado": "e1", "codigo": f"c{x}"},
    ),
    "concluir.erro": lambda c, x: (
        "/interno/autorizacao/concluir",
        {"pessoa_id": str(c.sistema.pessoa_id), "estado": "e1", "erro": f"access_denied{x}"},
    ),
    "iniciar.redirect_uri": lambda c, x: (
        "/interno/autorizacao/iniciar",
        {
            "pessoa_id": str(c.com_autorizacao.pessoa_id),
            "sistema_id": str(c.com_autorizacao.sistema_id),
            "redirect_uri": f"https://telas.exemplo/volta{x}",
        },
    ),
    "segredo.segredo": lambda c, x: (
        "/interno/autorizacao/segredo",
        {"sistema_id": str(c.sistema.sistema_id), "segredo": f"s{x}"},
    ),
}


@ESTRANHOS
@pytest.mark.parametrize("caso", list(CASOS))
async def test_texto_que_o_banco_nao_guarda_e_pedido_invalido_e_nada_muda(cena: Cena, caso: str, estranho: str) -> None:
    rota, corpo = CASOS[caso](cena, estranho)
    antes = await cena.retrato()
    r = await cena.postar(rota, corpo)
    assert (r.status_code, r.json().get("motivo")) == (400, "pedido_invalido"), r.text
    assert await cena.retrato() == antes


@pytest.mark.parametrize("valor", [5, {"a": 1}, ["a"]], ids=["numero", "objeto", "lista"])
@pytest.mark.parametrize(
    "caso",
    [
        lambda c, v: ("/interno/ler", c.ler(trace_id=v)),  # dava 500
        lambda c, v: ("/interno/declaracoes", {"sistema_id": str(c.sistema.sistema_id), "chave_sondagem": v}),
        lambda c, v: ("/interno/chaves", {"sistema_id": str(c.sistema.sistema_id), "segredo": "k", "quem": v}),
    ],
    ids=["ler.trace_id", "declaracoes.chave", "chaves.quem"],
)
async def test_campo_de_texto_que_nem_e_texto_e_pedido_invalido(cena: Cena, caso: Corpo, valor: Any) -> None:
    """``trace_id`` dava 500 (coluna de texto do extrato); a ``chave_sondagem`` que nao e texto dava 502, como se o
    servidor nao tivesse entregado; ``quem`` virava texto com ``str()`` e ia ao extrato."""
    rota, corpo = caso(cena, valor)
    antes = await cena.retrato()
    r = await cena.postar(rota, corpo)
    assert (r.status_code, r.json().get("motivo")) == (400, "pedido_invalido"), r.text
    assert await cena.retrato() == antes


async def test_texto_que_o_banco_guarda_continua_passando(cena: Cena) -> None:
    extrato, guardadas, _ = await cena.retrato()
    r = await cena.postar(
        "/interno/chaves", {"sistema_id": str(cena.sistema.sistema_id), "segredo": "k-nova", "quem": "admin-ção"}
    )
    assert (r.status_code, r.json()) == (200, {"guardada": True})
    r = await cena.postar("/interno/ler", cena.ler(trace_id="0af7651916cd43dd8448eb211c80319c"))
    assert r.status_code == 200
    assert cena.contagem.posts == 1
    async with cena.dono.connect() as c:
        frase = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE tipo = 'chave_guardada' AND sistema_id = :s ORDER BY id DESC"),
                {"s": cena.sistema.sistema_id},
            )
        ).first()
        trace = (
            await c.execute(
                text("SELECT trace_id FROM extrato WHERE pessoa_id = :p ORDER BY id DESC LIMIT 1"),
                {"p": cena.sistema.pessoa_id},
            )
        ).scalar_one()
    assert trace == "0af7651916cd43dd8448eb211c80319c"
    assert (await cena.retrato())[:2] == (extrato + 2, guardadas)  # a chave trocou (mesma linha) e a leitura
    assert frase is not None
    assert "por admin-ção" in frase.frase


async def test_quem_vazio_continua_sendo_admin(cena: Cena) -> None:
    r = await cena.postar("/interno/chaves", {"sistema_id": str(cena.sistema.sistema_id), "segredo": "k", "quem": ""})
    assert r.status_code == 200
    async with cena.dono.connect() as c:
        frase = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE tipo = 'chave_guardada' AND sistema_id = :s ORDER BY id DESC"),
                {"s": cena.sistema.sistema_id},
            )
        ).first()
    assert frase is not None
    assert "por admin" in frase.frase


async def test_chave_de_sistema_que_nao_existe_e_404_sem_erro_interno(cena: Cena) -> None:
    extrato, guardadas, posts = await cena.retrato()
    r = await cena.postar("/interno/chaves", {"sistema_id": str(uuid.uuid4()), "segredo": "k", "quem": "admin"})
    assert r.status_code == 404
    assert await cena.retrato() == (extrato, guardadas, posts)


@ESTRANHOS
async def test_argumentos_da_leitura_ficam_fora_da_regra(cena: Cena, estranho: str) -> None:
    """Os argumentos vao ao sistema, nao ao banco: a rota nao os recusa (o que o sistema faz com eles e com ele)."""
    r = await cena.postar("/interno/ler", cena.ler(argumentos={"pedido": f"a{estranho}b"}))
    assert r.status_code == 200
    assert r.json().get("motivo") != "pedido_invalido"


ROTAS_INTERNAS = (
    "/interno/ler",
    "/interno/sondar",
    "/interno/declaracoes",
    "/interno/revisao_servidor",
    "/interno/chaves",
    "/interno/autorizacao/iniciar",
    "/interno/autorizacao/concluir",
    "/interno/autorizacao/derrubar",
    "/interno/autorizacao/segredo",
)


@pytest.mark.parametrize("rota", ROTAS_INTERNAS)
async def test_corpo_que_nao_e_json_ou_nao_e_objeto_continua_com_a_resposta_de_cada_rota(cena: Cena, rota: str) -> None:
    for corpo in (b"{nao e json", b'["\\u0000"]', b'"\\ud800"'):
        r = await cena.http.post(rota, content=corpo, headers={"content-type": "application/json"})
        assert r.status_code == 400, (rota, corpo, r.text)
