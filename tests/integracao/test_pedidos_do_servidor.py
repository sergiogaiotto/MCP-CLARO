"""Pedidos do servidor barrados na leitura (G3a; i1, i2, i4, i5, i6 e i9 do mapa de pedidos).

Um sistema que pede dados, um link, uma resposta da IA, as pastas ou uma nova volta no meio da leitura (pelo
``InputRequiredResult``, pelo codigo antigo -32042 ou pelo fio antigo, em SSE) nunca tem o pedido repassado: a
leitura sai 1 POST so, sem ``inputResponses`` nem ``requestState``, e volta com um erro nosso, sem o texto, o link
nem o esquema do sistema; o resultado que vem depois de um pedido pelo fio antigo e descartado (D12). O extrato
ganha ``leitura_sistema_pediu_dados`` (ou a frase de programa) e o contador ``pedidos_do_sistema_barrados``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import mal_comportado
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante.servico import META_PEDIDOS, Despachante
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import sinais
from mcp_claro.saida.estados import Estado
from mcp_claro.versao import PREFIXO_META
from tests import apoio_g3a as g
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_entrada_terceiros_apoio import Mundo, subir_mundo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MOTIVO = PREFIXO_META + "motivo"
CONSULTAR = {
    "name": "consultar_pedido",
    "description": "Consulta a situacao de um pedido.",
    "inputSchema": {"type": "object", "properties": {"pedido": {"type": "string", "maxLength": 40}}},
}
# Modo do duble -> (categorias que o MCP-CLARO conta, texto legivel do extrato)
MODOS: dict[str, tuple[tuple[str, ...], str]] = {
    "pede_dados": (("formulario",), "dados por formulario"),
    "pede_link": (("link",), "que a pessoa abrisse um link"),
    "pede_amostragem": (("amostragem",), "uma resposta da IA do aplicativo"),
    "pede_raizes": (("raizes",), "a lista de pastas do aplicativo"),
    "pede_so_estado": (("so_estado",), "uma nova volta"),
    "link_codigo_antigo": (("link_codigo_antigo",), "que a pessoa abrisse um link"),
    "pede_pelo_fio_antigo": (("fio_antigo:elicitation/create",), "algo fora do padrao 2026-07-28"),
    "pede_pelo_fio_antigo_e_401": (("fio_antigo:elicitation/create",), "algo fora do padrao 2026-07-28"),
}
TEXTOS_DO_SISTEMA = ("Digite sua senha", "phish.exemplo", "Entre aqui", "Mande a chave", "volta-", "senha")


@dataclass
class Cena:
    p: Parametros
    dono: AsyncEngine
    desp_db: AsyncEngine
    cifra: CifraFalsa

    def despachante(self, **kw: Any) -> Despachante:
        return g.despachante(self.desp_db, self.cifra, **kw)


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    dono = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    try:
        await g.abrir_a_saida(dono)
        yield Cena(banco_migrado, dono, desp_db, CifraFalsa())
    finally:
        await dono.dispose()
        await desp_db.dispose()


def _barrados(tipo: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    return sum(
        n
        for (_, nome, rotulos), n in medidas.items()
        if nome == "pedidos_do_sistema_barrados" and json.loads(rotulos).get("tipo") == tipo
    )


@pytest.mark.parametrize("origem", ["terceiro", "proprio"])
@pytest.mark.parametrize("modo", sorted(MODOS))
async def test_pedido_do_sistema_vira_erro_nosso_com_um_post_so(cena: Cena, modo: str, origem: str) -> None:
    categorias, legivel = MODOS[modo]
    antes = _barrados(categorias[0])
    app = g.ContaPosts(mal_comportado.criar())
    async with servidor(app) as base, httpx2.AsyncClient() as http:
        await http.post(f"{base}/modo", json={"modo": modo, "atraso_fio": 0.1})
        arr = await g.preparar(cena.dono, cena.cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem=origem)
        r = await cena.despachante().ler(arr.pedido({"pedido": "1"}))
    # 1 POST so, sem nada que responda a um pedido do sistema.
    assert app.posts == 1
    assert app.state.contagem.chamadas.get("consultar_pedido") == 1
    assert app.state.contagem.chamadas.get("resposta_do_cliente", 0) == 0
    pedido = app.corpos[0]
    assert pedido["method"] == "tools/call"
    assert "inputResponses" not in pedido["params"]
    assert "requestState" not in pedido["params"]
    assert (pedido["params"].get("_meta") or {}).get(types.CLIENT_CAPABILITIES_META_KEY, {}) == {}
    # Erro nosso, sem o texto, o link nem o esquema do sistema; o resultado de depois (fio antigo) nao passa.
    bruto = json.dumps(r, ensure_ascii=False)
    assert all(t not in bruto for t in TEXTOS_DO_SISTEMA), bruto
    assert "Pedido em transito" not in bruto
    assert r["isError"] is True
    assert r["_meta"][MOTIVO] == "sistema_pediu_dados"
    assert tuple(r["_meta"][META_PEDIDOS]) == categorias
    assert not sinais.veio_do_sistema(r)
    assert r["content"] == [
        {
            "type": "text",
            "text": f"O sistema {arr.sistema} pediu {legivel} no meio da leitura. O MCP-CLARO recusou: nao repassa"
            " pedidos de dados, links ou confirmacoes de um sistema.",
        }
    ]
    linhas = await g.extrato(cena.dono, arr.sistema_id)
    assert [ln.tipo for ln in linhas] == ["leitura_sistema_pediu_dados"]
    assert linhas[0].frase == (
        f"O sistema {arr.sistema} pediu {legivel} no meio da leitura em {arr.nome_vitrine}; o MCP-CLARO recusou e"
        " nao repassou o pedido."
    )
    assert linhas[0].dados["pedidos"] == list(categorias)
    assert linhas[0].pessoa_id == arr.pessoa_id
    assert _barrados(categorias[0]) == antes + 1


async def test_programa_que_le_ganha_a_frase_de_programa(cena: Cena) -> None:
    async with servidor(mal_comportado.criar()) as base, httpx2.AsyncClient() as http:
        await http.post(f"{base}/modo", json={"modo": "pede_dados"})
        arr = await g.preparar(
            cena.dono,
            cena.cifra,
            endereco=f"{base}/mcp",
            definicao=CONSULTAR,
            tipo_chave="programa",
            chave="chave-compartilhada",
        )
        r = await cena.despachante().ler(arr.pedido({"pedido": "1"}))
    assert r["_meta"][MOTIVO] == "sistema_pediu_dados"
    linhas = await g.extrato(cena.dono, arr.sistema_id)
    assert [ln.tipo for ln in linhas] == ["programa_leitura_sistema_pediu_dados"]
    assert ", pelo programa Aplicativo " in linhas[0].frase
    assert "responsavel Pessoa" in linhas[0].frase


async def test_sondagem_e_listagem_com_fio_antigo_nao_ficam_contaminadas(cena: Cena) -> None:
    app = g.ContaPosts(mal_comportado.criar())
    async with servidor(app) as base, httpx2.AsyncClient() as http:
        await http.post(f"{base}/modo", json={"modo": "pede_pelo_fio_antigo"})
        arr = await g.preparar(cena.dono, cena.cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro")
        desp = cena.despachante()
        sondagem = await desp.sondar_sistema(arr.sistema_id, "chave-compartilhada")
        assert app.posts == 1
        declaradas = await desp.ler_declaracoes(arr.sistema_id, None)
        assert app.posts == 2
    assert sondagem.desfecho == "aceito"
    assert sondagem.pedidos == ()
    assert [d["name"] for d in declaradas] == ["consultar_pedido"]
    assert app.state.contagem.chamadas.get("resposta_do_cliente", 0) == 0
    assert [c["method"] for c in app.corpos] == ["server/discover", "tools/list"]


# --- D13: -32021 de terceiro sempre como erro legivel; proprio como test_conformidade_gateway --------------------


def _exige_amostragem(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": {
            "code": types.MISSING_REQUIRED_CLIENT_CAPABILITY,
            "message": "Digite sua senha: preciso de sampling",
            "data": {"requiredCapabilities": {"sampling": {}}},
        }
    }


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado) as m:
        yield m


@pytest.mark.parametrize("chave", ["t_ler", "p_simples"])
async def test_32021_de_terceiro_e_erro_legivel_e_de_proprio_segue_como_antes(
    mundo: Mundo, cena: Cena, chave: str
) -> None:
    terceiro = chave.startswith("t_")
    async with servidor(g.sistema_cru(_exige_amostragem, status=400)) as base:  # como o kit faz
        arr = await g.preparar(
            cena.dono,
            cena.cifra,
            endereco=f"{base}/mcp",
            definicao=CONSULTAR,
            origem="terceiro" if terceiro else "proprio",
        )
        desp = cena.despachante()

        async def ler(_: Any) -> dict[str, Any]:
            return await desp.ler(arr.pedido())

        mundo.leitor.respostas[mundo.f[chave].id] = ler
        if terceiro:
            r = await mundo.chamar("ana", chave, {"texto": "oi"})
            assert r.is_error is True
            assert "nao repassa" in r.content[0].text  # type: ignore[union-attr]
            assert "senha" not in json.dumps(r.model_dump(mode="json"))
            return
        with pytest.raises(MCPError) as recusa:
            await mundo.chamar("ana", chave, {"texto": "oi"})
    assert recusa.value.error.code == types.MISSING_REQUIRED_CLIENT_CAPABILITY
    assert recusa.value.error.data == {"requiredCapabilities": {"sampling": {}}}


# --- Regressao do defeito da Fase 2: o 401 no segundo POST do fio antigo, numa acao ----------------------------


async def test_acao_com_pedido_pelo_fio_antigo_e_401_fica_nao_confirmada_sem_vencer_a_chave(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integracao.test_autorizacao import subir
    from tests.integracao.test_saida import chamar, confirmar, esperar_estado, meta, operacao, politica

    async with subir(
        banco_migrado,
        monkeypatch,
        criar_sistema=mal_comportado.criar,
        revisoes=(("consultar_pedido", "pedido", "muda", "medio"),),
        extra={"OUTBOX_SWEEP_SECONDS": 1},
    ) as amb:
        assert (await amb.autorizar(amb.ana_id, "ana")).status_code == 200
        await politica(amb, CONFIRM_CAP_PER_HOUR=1000)
        try:
            async with httpx2.AsyncClient() as http:
                await http.post(f"{amb.url_sistema}/modo", json={"modo": "pede_pelo_fio_antigo_e_401"})
            async with amb.admin.connect() as c:
                antes = (
                    await c.execute(
                        text(
                            "SELECT cifrada, expira_em FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p"
                            " AND tipo = 'acesso'"
                        ),
                        {"s": amb.sistema_id, "p": amb.ana_id},
                    )
                ).one()
            amb.zerar_vistas()
            args = {"pedido": f"fio-{uuid.uuid4().hex[:6]}"}
            r = await chamar(amb, "consultar_pedido", args)
            protocolo = meta(r)[PREFIXO_META + "protocolo"]
            await confirmar(amb, await operacao(amb, protocolo), pessoa_id=amb.ana_id)
            op = await esperar_estado(amb, protocolo, Estado.NAO_CONFIRMADA, Estado.NAO_SAIU, Estado.ENVIADA)
            assert op.estado == Estado.NAO_CONFIRMADA
            contagem = amb.sistema_app.state.contagem.chamadas
            assert contagem.get("consultar_pedido") == 1
            assert contagem.get("resposta_do_cliente", 0) == 0  # o segundo POST (com a chave) nao saiu
            assert contagem.get("http401", 0) == 0
            async with amb.admin.connect() as c:
                depois = (
                    await c.execute(
                        text(
                            "SELECT cifrada, expira_em FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p"
                            " AND tipo = 'acesso'"
                        ),
                        {"s": amb.sistema_id, "p": amb.ana_id},
                    )
                ).one()
            assert tuple(depois) == tuple(antes)  # a chave nao foi dada por vencida
            # Pedido igual: o mesmo protocolo, nada novo sai.
            de_novo = await chamar(amb, "consultar_pedido", args)
            assert meta(de_novo)[PREFIXO_META + "protocolo"] == protocolo
            assert amb.sistema_app.state.contagem.chamadas.get("consultar_pedido") == 1
        finally:
            async with amb.admin.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = 'teste-saida'"))
