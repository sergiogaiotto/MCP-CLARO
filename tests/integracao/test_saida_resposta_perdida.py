"""Resposta perdida de proposito: o pedido chega ao sistema, a resposta nao volta. Nada e reenviado.

Um intermediario TCP de teste (``tests/cortador.py``) fica entre o despachante e o sistema de exemplo
``acoes_caixa`` (que conta cada chamada). Ele deixa o pedido chegar inteiro e corta so o caminho da
resposta: fecha a conexao antes de repassar qualquer byte, ou no meio do corpo.

Tambem aqui: o despachante que cai entre marcar "enviando" e a resposta, e os argumentos trocados no banco
depois da ficha confirmada.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro.ligacao import cliente
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.protocolo import codigo_conferencia
from mcp_claro.versao import PREFIXO_META
from tests.cortador import Cortador, Modo, cortador
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import (
    ambiente_de_acoes,
    chamadas,
    chamar,
    confirmar,
    esperar_estado,
    meta,
    operacao,
    politica,
)
from tests.integracao.test_saida_disputas import ambiente_sem_porta, limpar, nova_acao, porta, sem_sobras

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PROTOCOLO = PREFIXO_META + "protocolo"
ESTADO = PREFIXO_META + "estado"
VARREDURAS = 3.5
"""Segundos de espera para "varias varreduras": a porta do app varre a cada segundo neste arranjo."""


@pytest.fixture
async def amb(banco_migrado: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    """Arranjo com a porta de saida do proprio app do despachante, como na instalacao."""
    async with ambiente_de_acoes(banco_migrado, monkeypatch) as a:
        await politica(a, CONFIRM_CAP_PER_HOUR=1000)
        await sem_sobras(a)
        try:
            yield a
        finally:
            await limpar(a)


@pytest.fixture
async def amb_sem_porta(banco_migrado: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with ambiente_sem_porta(banco_migrado, monkeypatch) as a:
        yield a


@contextlib.asynccontextmanager
async def na_frente_do_sistema(amb: Ambiente) -> AsyncIterator[Cortador]:
    """Poe o cortador entre o despachante e o sistema: o cadastro passa a apontar para ele.

    Mudar o endereco exige nova sondagem (gatilho do cadastro); aqui o dono do banco devolve o resultado
    da sondagem que ja foi feita, como se ela tivesse sido refeita pelo cortador em modo inteiro.
    """
    async with cortador(amb.url_sistema) as corte:
        async with amb.admin.begin() as c:
            antes = (
                await c.execute(
                    text("SELECT situacao, versao_mcp, sondagem, sondado_em FROM sistemas WHERE id = :s"),
                    {"s": amb.sistema_id},
                )
            ).one()
            await c.execute(
                text("UPDATE sistemas SET endereco = :e WHERE id = :s"), {"e": f"{corte.url}/mcp", "s": amb.sistema_id}
            )
            await c.execute(
                text(
                    "UPDATE sistemas SET situacao = :si, versao_mcp = :v, sondagem = CAST(:so AS jsonb),"
                    " sondado_em = :em WHERE id = :s"
                ),
                {
                    "si": antes.situacao,
                    "v": antes.versao_mcp,
                    "so": json.dumps(antes.sondagem) if antes.sondagem is not None else None,
                    "em": antes.sondado_em,
                    "s": amb.sistema_id,
                },
            )
        yield corte


async def pedir_e_perder(amb: Ambiente, corte: Cortador, argumentos: dict[str, Any], modo: Modo) -> fila.Operacao:
    """Pede pelo aplicativo, confirma a ficha e corta a resposta do sistema. Devolve a operacao nao confirmada."""
    r = await chamar(amb, "criar_chamado", argumentos)
    assert meta(r)[ESTADO] == "aguardando_ficha"
    op = await operacao(amb, meta(r)[PROTOCOLO])
    antes = chamadas(amb, "criar_chamado")
    corte.modo = modo
    try:
        await confirmar(amb, op, pessoa_id=amb.ana_id)
        final = await esperar_estado(amb, op.protocolo, Estado.NAO_CONFIRMADA, Estado.ENVIADA, Estado.NAO_SAIU)
    finally:
        corte.modo = "inteiro"
    assert final.estado == Estado.NAO_CONFIRMADA, final.desfecho
    assert chamadas(amb, "criar_chamado") == antes + 1
    return final


async def extrato(amb: Ambiente, protocolo: str) -> list[str]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(text("SELECT tipo FROM extrato WHERE protocolo = :p ORDER BY id"), {"p": protocolo})
            ).scalars()
        )


@pytest.mark.parametrize("modo", ["sem_resposta", "meio_do_corpo"])
async def test_resposta_perdida_vira_nao_confirmada_e_nada_e_reenviado(amb: Ambiente, modo: Modo) -> None:
    async with na_frente_do_sistema(amb) as corte:
        argumentos = {"titulo": f"Resposta perdida {modo} {uuid.uuid4().hex[:6]}"}
        op = await pedir_e_perder(amb, corte, argumentos, modo)
        assert op.desfecho == "a resposta do sistema nao chegou inteira"
        assert op.resultado is None
        assert corte.cortes == 1
        assert corte.conexoes == 1
        assert b"tools/call" in corte.pedidos[0]  # o pedido chegou inteiro ao sistema

        await asyncio.sleep(VARREDURAS)
        assert chamadas(amb, "criar_chamado") == 1
        assert corte.conexoes == 1, "nada foi reenviado ao sistema"
        assert (await operacao(amb, op.protocolo)).estado == Estado.NAO_CONFIRMADA
        tipos = await extrato(amb, op.protocolo)
        assert tipos.count("acao_nao_confirmada") == 1
        assert "acao_enviada" not in tipos

        # O aplicativo repete o pedido: volta o protocolo original, "estamos confirmando", nada sai.
        de_novo = await chamar(amb, "criar_chamado", argumentos)
        assert meta(de_novo)[PROTOCOLO] == op.protocolo
        assert meta(de_novo)[ESTADO] == "nao_confirmada"
        assert meta(de_novo)[PREFIXO_META + "repetido"] is True
        assert "Estamos confirmando" in de_novo.content[0].text
        assert "Nada sera reenviado" in de_novo.content[0].text
        await asyncio.sleep(1.5)
        assert chamadas(amb, "criar_chamado") == 1
        assert corte.conexoes == 1


@pytest.mark.parametrize(
    ("decisao", "mesmo_protocolo"), [("nao_aconteceu", False), ("aconteceu", True), ("nao_sei", True)]
)
async def test_decisao_da_pessoa_diz_se_o_pedido_igual_e_novo(
    amb: Ambiente, decisao: str, mesmo_protocolo: bool
) -> None:
    async with na_frente_do_sistema(amb) as corte:
        argumentos = {"titulo": f"Decisao {decisao} {uuid.uuid4().hex[:6]}"}
        op = await pedir_e_perder(amb, corte, argumentos, "sem_resposta")
        async with amb.entrada_db.begin() as c:
            decidida = await fila.decidir_nao_confirmada(
                c, op.id, pessoa_id=amb.ana_id, decisao=decisao, observacao="conferi no sistema"
            )
        assert decidida.estado == Estado.NAO_CONFIRMADA  # a decisao nao muda a linha da fila
        assert decidida.decisao_pessoa == decisao

        r = await chamar(amb, "criar_chamado", argumentos)
        if mesmo_protocolo:
            assert meta(r)[PROTOCOLO] == op.protocolo
            assert meta(r)[ESTADO] == "nao_confirmada"
        else:
            assert meta(r)[PROTOCOLO] != op.protocolo
            assert meta(r)[ESTADO] == "aguardando_ficha"
        await asyncio.sleep(1.5)
        assert chamadas(amb, "criar_chamado") == 1, "nem a decisao nem o pedido igual reenviam"
        assert corte.conexoes == 1
        assert (await operacao(amb, op.protocolo)).estado == Estado.NAO_CONFIRMADA


async def test_queda_do_despachante_entre_enviando_e_a_resposta(amb_sem_porta: Ambiente) -> None:
    amb = amb_sem_porta
    nunca = asyncio.Event()

    async def chamar_e_cair(*args: Any, **kwargs: Any) -> cliente.Resposta:
        resposta = await cliente.chamar_ferramenta(*args, **kwargs)  # o sistema executou
        await nunca.wait()  # e o despachante cai antes de gravar o fim
        return resposta

    primeira = porta(amb, chamar=chamar_e_cair)
    tarefa = asyncio.create_task(primeira.rodar())
    segunda = porta(amb)
    tarefa2: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(primeira.assumiu.wait(), 15)
        op = await nova_acao(amb, "Queda no meio do envio")
        await esperar_estado(amb, op.protocolo, Estado.ENVIANDO)
        for _ in range(100):
            if chamadas(amb, "criar_chamado") == 1:
                break
            await asyncio.sleep(0.05)
        assert chamadas(amb, "criar_chamado") == 1
        tarefa.cancel()  # a queda: a tarefa some e a conexao da trava e descartada
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
        await asyncio.sleep(0.5)
        assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIANDO  # ninguem mexeu ainda

        # Na volta, a porta assume e, antes de enviar qualquer coisa, da a acao como nao confirmada.
        tarefa2 = asyncio.create_task(segunda.rodar())
        await asyncio.wait_for(segunda.assumiu.wait(), 15)
        nc = await esperar_estado(amb, op.protocolo, Estado.NAO_CONFIRMADA)
        assert nc.desfecho == "interrompida no envio"
        await asyncio.sleep(1.5)  # varias varreduras (0,2 s cada)
        assert chamadas(amb, "criar_chamado") == 1
        assert (await operacao(amb, op.protocolo)).estado == Estado.NAO_CONFIRMADA

        # O pedido igual devolve a operacao original; nada sai de novo.
        r = await chamar(amb, "criar_chamado", {"titulo": "Queda no meio do envio"})
        assert meta(r)[PROTOCOLO] == op.protocolo
        assert meta(r)[ESTADO] == "nao_confirmada"
        await asyncio.sleep(0.6)
        assert chamadas(amb, "criar_chamado") == 1
    finally:
        nunca.set()
        for t in (tarefa, tarefa2):
            if t is not None:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t


async def _liberar_com_espera(amb: Ambiente, titulo: str) -> fila.Operacao:
    r = await chamar(amb, "criar_chamado", {"titulo": titulo})
    op = await operacao(amb, meta(r)[PROTOCOLO])
    assert op.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        await fila.confirmar_ficha(
            c, op.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=60, rapida_segundos=3
        )
    return await operacao(amb, op.protocolo)


async def _seguir_agora(amb: Ambiente, op: fila.Operacao) -> None:
    async with amb.entrada_db.begin() as c:
        await fila.seguir_agora(c, op.id, pessoa_id=amb.ana_id)


async def test_argumentos_trocados_no_banco_depois_da_ficha_nao_saem(amb: Ambiente) -> None:
    op = await _liberar_com_espera(amb, "Pedido conferido")
    assert op.estado == Estado.LIBERADA
    async with amb.admin.begin() as c:
        await c.execute(
            text("UPDATE operacoes SET argumentos = CAST(:a AS jsonb) WHERE id = :o"),
            {"a": '{"titulo": "Pedido trocado"}', "o": op.id},
        )
    await _seguir_agora(amb, op)
    parada = await esperar_estado(amb, op.protocolo, Estado.CANCELADA, Estado.ENVIADA)
    assert parada.estado == Estado.CANCELADA
    assert parada.desfecho == "o codigo de conferencia nao bate"
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 0
    assert "acao_parada" in await extrato(amb, op.protocolo)


async def test_argumentos_e_codigo_trocados_no_banco_nao_batem_com_a_ficha(amb: Ambiente) -> None:
    """Quem troca tambem o codigo da operacao ainda esbarra no codigo que a pessoa confirmou na ficha."""
    op = await _liberar_com_espera(amb, "Pedido conferido de novo")
    trocados = {"titulo": "Pedido trocado com codigo"}
    async with amb.admin.begin() as c:
        await c.execute(
            text("UPDATE operacoes SET argumentos = CAST(:a AS jsonb), codigo_conferencia = :c WHERE id = :o"),
            {
                "a": '{"titulo": "Pedido trocado com codigo"}',
                "c": codigo_conferencia(amb.sistema_id, "criar_chamado", trocados),
                "o": op.id,
            },
        )
    await _seguir_agora(amb, op)
    parada = await esperar_estado(amb, op.protocolo, Estado.CANCELADA, Estado.ENVIADA)
    assert parada.estado == Estado.CANCELADA
    assert parada.desfecho == "a ficha nao foi confirmada para este pedido"
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 0
