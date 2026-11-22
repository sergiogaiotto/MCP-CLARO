"""R6b-5 (contrato K9): "Pedidos recusados deste sistema" conta toda linha de pedido barrado.

Uma acao em que o sistema pediu algo no meio e que termina "nao confirmada" ou "nao saiu" leva as categorias em
``dados.pedidos`` (``fila.finalizar``), e a tela do ADMIN a conta, como a acao que saiu. As linhas de pedido barrado
fora de uma chamada (contrato K4: na leitura das ferramentas e na sondagem, sem pessoa e sem ferramenta) tambem
contam, com rotulo proprio. Aqui o teste as grava pelo dono do banco: quem as grava e o despachante (R6e).

A prova do revisor (estranhos), invertida, num sistema proprio (``mal_comportado`` pede pelo fio antigo e responde
401) e num terceiro pela porta de saida.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from exemplos import mal_comportado
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente
from mcp_claro.saida.estados import Estado
from mcp_claro.telas import consultas_terceiros, paginas
from mcp_claro.versao import PREFIXO_META
from tests.integracao.test_entrada_terceiros_apoio import Mundo, subir_mundo
from tests.integracao.test_saida_terceiros import (
    LigacaoFalsa,
    Ligacoes,
    ate_o_fim,
    guardar_chave,
    liberada,
    porta,
)
from tests.integracao.test_telas_fase3_admin import Cena, _sem_tags, subir

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PEDIDOS = ("fio_antigo:elicitation/create", "link")


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado, terceiros_ligados=True) as m:
        yield m


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    async with subir(banco_migrado) as c:
        yield c


def _dados(linha: Any) -> Any:
    return json.loads(linha.dados) if isinstance(linha.dados, str) else linha.dados


@pytest.mark.parametrize(
    ("resposta", "estado", "tipo"),
    [
        (cliente.Resposta("duvida", pedidos=PEDIDOS), Estado.NAO_CONFIRMADA, "acao_nao_confirmada"),
        (cliente.Resposta("recusado_http", status_http=(401,), pedidos=PEDIDOS), Estado.NAO_SAIU, "acao_nao_saiu"),
        (cliente.Resposta("nao_enviado", pedidos=PEDIDOS), Estado.NAO_SAIU, "acao_nao_saiu"),
    ],
)
async def test_acao_de_terceiro_com_pedido_barrado_que_nao_terminou_bem_entra_na_tela(
    mundo: Mundo, resposta: cliente.Resposta, estado: Estado, tipo: str
) -> None:
    m = mundo
    await guardar_chave(m, m.terceiro_id, "ana", "chave-ana")
    op = await liberada(m, "ana", "t_enviar", {"texto": "com pedido barrado"})
    fim = await ate_o_fim(m, porta(m, Ligacoes({m.terceiro_id: LigacaoFalsa(resposta)}), terceiros_ligados=True), op)
    assert fim.estado == estado
    assert fim.desfecho is not None
    assert "MCP-CLARO recusou" in fim.desfecho, fim.desfecho
    async with m.dono.connect() as c:
        (linha,) = (
            await c.execute(
                text("SELECT dados FROM extrato WHERE tipo = :t AND protocolo = :p"), {"t": tipo, "p": op.protocolo}
            )
        ).all()
    assert _dados(linha)["pedidos"] == list(PEDIDOS)
    async with m.entrada_db.connect() as c:  # o usuario do banco das telas
        pedidos = await consultas_terceiros.pedidos_recusados(c, m.terceiro_id)
    assert pedidos.contagem == 1, f"desfecho {fim.desfecho!r}, mas a tela conta {pedidos.contagem}"
    (vista,) = pedidos.linhas
    assert (vista.tipo, vista.pedidos) == (tipo, list(PEDIDOS))
    assert tipo in paginas.ONDE_O_PEDIDO


async def test_acao_sem_pedido_que_nao_terminou_bem_nao_leva_pedidos_nem_conta(mundo: Mundo) -> None:
    m = mundo
    await guardar_chave(m, m.terceiro_id, "ana", "chave-ana")
    op = await liberada(m, "ana", "t_enviar", {"texto": "sem pedido"})
    ligacao = LigacaoFalsa(cliente.Resposta("duvida"))
    fim = await ate_o_fim(m, porta(m, Ligacoes({m.terceiro_id: ligacao}), terceiros_ligados=True), op)
    assert fim.estado == Estado.NAO_CONFIRMADA
    async with m.dono.connect() as c:
        (linha,) = (
            await c.execute(
                text("SELECT dados FROM extrato WHERE tipo = 'acao_nao_confirmada' AND protocolo = :p"),
                {"p": op.protocolo},
            )
        ).all()
    assert "pedidos" not in _dados(linha)
    async with m.entrada_db.connect() as c:
        assert (await consultas_terceiros.pedidos_recusados(c, m.terceiro_id)).contagem == 0


async def test_acao_de_proprio_que_pediu_pelo_fio_antigo_e_ficou_sem_confirmacao_entra_na_tela(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prova do revisor, como ele a rodou: sistema proprio, o pedido pelo fio antigo e depois o 401 (D15).
    from tests.integracao.test_autorizacao import subir as subir_autorizacao
    from tests.integracao.test_saida import chamar, confirmar, esperar_estado, meta, operacao, politica

    async with subir_autorizacao(
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
            r = await chamar(amb, "consultar_pedido", {"pedido": f"fio-{uuid.uuid4().hex[:6]}"})
            protocolo = meta(r)[PREFIXO_META + "protocolo"]
            await confirmar(amb, await operacao(amb, protocolo), pessoa_id=amb.ana_id)
            op = await esperar_estado(amb, protocolo, Estado.NAO_CONFIRMADA, Estado.NAO_SAIU, Estado.ENVIADA)
            assert op.estado == Estado.NAO_CONFIRMADA
            assert op.desfecho is not None
            assert "MCP-CLARO recusou" in op.desfecho, op.desfecho
            async with amb.admin.connect() as c:
                pedidos = await consultas_terceiros.pedidos_recusados(c, amb.sistema_id)
            assert pedidos.contagem >= 1, f"desfecho {op.desfecho!r}, mas a tela conta {pedidos.contagem}"
            assert {linha.tipo for linha in pedidos.linhas} == {"acao_nao_confirmada"}
        finally:
            async with amb.admin.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = 'teste-saida'"))


async def _gravar_pelo_dono(c: Any, tipo: str, sistema_id: uuid.UUID, pedidos: list[str]) -> None:
    """Uma linha de pedido barrado fora da chamada (K4), sem pessoa nem ferramenta, como o despachante grava. A
    frase e do teste: quem a escreve (``extrato/frases.py``) e o R6e."""
    await c.execute(
        text(
            "SELECT extrato_registrar('despachante', :t, 'Pedido do sistema recusado.', NULL, NULL, NULL, :s, NULL,"
            " CAST(:d AS jsonb), NULL)"
        ),
        {"t": tipo, "s": sistema_id, "d": json.dumps({"pedidos": pedidos})},
    )


async def test_cada_tipo_de_linha_aparece_com_o_rotulo_e_as_de_fora_da_chamada_sem_ferramenta(cena: Cena) -> None:
    sistema_id, _ = await cena.terceiro()
    async with cena.amb.engine.begin() as c:
        await _gravar_pelo_dono(c, "declaracoes_sistema_pediu_dados", sistema_id, ["link"])
        await _gravar_pelo_dono(c, "sondagem_sistema_pediu_dados", sistema_id, ["amostragem"])
        for tipo, campos, dados in [
            ("acao_nao_confirmada", {"protocolo": "P-1"}, {"pedidos": ["formulario"]}),
            ("acao_nao_saiu", {"protocolo": "P-2", "motivo": "recusou"}, {"pedidos": ["raizes"]}),
            ("acao_nao_saiu", {"protocolo": "P-3", "motivo": "recusou"}, {}),  # sem pedido: nao conta
            ("acao_nao_confirmada", {"protocolo": "P-4"}, {"pedidos": []}),  # lista vazia: nao conta
        ]:
            await registrar(
                c,
                papel="despachante",
                tipo=tipo,
                campos=campos,
                sistema_id=sistema_id,
                ferramenta="acao_r6b",
                dados=dados,
            )
    r = await cena.nav.get(f"/telas/admin/sistemas/{sistema_id}")
    assert r.status_code == 200
    secao = r.text.split("Pedidos recusados deste sistema", 1)[1].split("</section>", 1)[0]
    assert "Nos últimos 30 dias, este servidor fez 4 pedidos recusados." in _sem_tags(secao)
    linhas = secao.split("<tr><td>")[1:]
    assert len(linhas) == 4
    por_rotulo: dict[str, str] = {}
    for linha in linhas:
        colunas = linha.split("</td><td>")
        por_rotulo[colunas[1]] = colunas[2]
    assert set(por_rotulo) == {
        "Na leitura das ferramentas",
        "Na sondagem",
        "Numa ação sem confirmação",
        "Numa ação, que não saiu",
    }
    # Fora da chamada, a linha nao tem ferramenta: a coluna fica vazia.
    assert por_rotulo["Na leitura das ferramentas"] == "<code></code>"
    assert por_rotulo["Na sondagem"] == "<code></code>"
    assert por_rotulo["Numa ação sem confirmação"] == "<code>acao_r6b</code>"
    assert "que a pessoa abrisse um link" in secao
