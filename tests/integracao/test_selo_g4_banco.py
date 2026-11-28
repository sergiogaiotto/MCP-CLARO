"""Prova pelo banco do que e do G4r na 0007 (Fase 4, retoque da etapa 3; blocos ENVIO e FICHAS), contra PostgreSQL
18.7 real. Cada cena roda numa transacao que volta atras (nada fica gravado no banco da sessao).

- ``selo_substituto`` segue so os envios cuja fila esta em ``enviando``, ``enviada`` ou ``nao_confirmada`` (as chamadas
  que podem ter chegado ao sistema); um pedido igual ``nao_saiu`` ou ``sistema_pediu_dados`` nao substitui a original.
  Entre dois no mesmo passo, vale o envio mais novo (``gravado_em``), nao o maior id (decisao 5 do coordenador).
- ``envios_selo_nasce`` so queima o numero pelas respostas de origem ``recibo`` e ``consulta``; a resposta ao aviso de
  bloqueio nunca queima (decisao 1). A prova com o aviso inteiro (bloqueio, comprovante e aviso) esta em
  ``test_selo_comprovante.py``; aqui, o recibo tambem queima.
- ``fichas.numero_novo`` (critica M4.2, decisao 4): nasce falso; so a entrada o grava, so na confirmacao da ficha que
  espera, so no pedido igual cuja ultima resposta a consulta foi ``desconhecido`` e sobre cujo NUMERO nao ha estado
  final (nenhuma acao que saiu com ele, pela cadeia de ``envios_selo.repete_operacao_id``: defeito 2 do verificador
  do G4r). Com ele, o envio sai com numero novo; sem ele, com o mesmo. As rotinas e o despachante nao o mudam. A
  leitura do codigo (``fila.por_id``, ``EscadaDaOriginal.estado_final_do_numero``) segue a mesma cadeia.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.saida import fila
from tests import apoio_selos as apoio
from tests.integracao.test_migracao_0006 import DESPACHANTE, ENTRADA, ROTINAS, SEM_PERMISSAO, como, dono

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture

NUMERO_NOVO_SO_NA_CONFIRMACAO = "O numero novo so e escolhido uma vez, enquanto a ficha espera, junto com a confirmacao"
NUMERO_NOVO_SO_COM_DESCONHECIDO = "O numero novo so vale no pedido igual a uma acao sem resposta final"


@dataclass
class Cena:
    admin_id: uuid.UUID
    sistema: apoio.SistemaDoSelo


@pytest.fixture
async def cena(dono: AsyncEngine) -> Cena:
    """Um proprio com o contrato listado e um teste de nivel 3 aprovado, gravados pelo dono (fica no banco da sessao,
    como em ``test_migracao_0007``)."""
    async with dono.begin() as c:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(c)
        await apoio.gravar_teste(c, sistema.id, nivel=3, admin_id=admin)
    return Cena(admin, sistema)


async def num_rascunho(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    """Roda ``passos`` numa transacao que sempre volta atras e devolve o que ``passos`` devolveu."""
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            return await passos(c)
        finally:
            await transacao.rollback()


async def _original(c: AsyncConnection, cena: Cena) -> tuple[apoio.OperacaoDeTeste, apoio.Envio]:
    """Uma acao que saiu com numero e ficou "nao confirmada"."""
    original = await apoio.criar_operacao(c, cena.sistema.acoes[0])
    return original, await apoio.enviar(c, original.id)


async def _repetir(
    c: AsyncConnection, cena: Cena, original: apoio.OperacaoDeTeste, *, liberada: bool = True
) -> apoio.OperacaoDeTeste:
    """O pedido igual a ``original`` (mesma pessoa, aplicativo e argumentos), com ``repete_operacao_id``."""
    return await apoio.criar_operacao(
        c,
        cena.sistema.acoes[0],
        pessoa_id=original.pessoa_id,
        aplicativo_id=original.aplicativo_id,
        argumentos=original.argumentos,
        repete=original.id,
        liberada=liberada,
    )


async def _substituto(c: AsyncConnection, operacao_id: uuid.UUID) -> uuid.UUID | None:
    valor = (await c.execute(text("SELECT selo_substituto(:o)"), {"o": operacao_id})).scalar_one()
    return None if valor is None else uuid.UUID(str(valor))


# --- Decisao 5: o substituto so segue o que pode ter chegado ao sistema -----------------------------------------


@pytest.mark.parametrize(
    ("fim", "substitui"),
    [
        (None, True),  # enviando: a chamada esta a caminho
        ("enviada", True),
        ("nao_confirmada", True),
        ("nao_saiu", False),
        ("sistema_pediu_dados", False),
    ],
)
async def test_substituto_so_segue_o_pedido_igual_que_pode_ter_chegado(
    dono: AsyncEngine, cena: Cena, fim: str | None, substitui: bool
) -> None:
    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        o, envio_o = await _original(c, cena)
        r = await _repetir(c, cena, o)
        envio_r = await apoio.enviar(c, r.id, fim)
        return envio_o, envio_r, r.id, await _substituto(c, o.id)

    envio_o, envio_r, r, substituto = await num_rascunho(dono, passos)
    assert (envio_r.chave, envio_r.repete_operacao_id) == (envio_o.chave, envio_o.operacao_id)  # levou o numero
    assert substituto == (r if substitui else None)


async def test_substituto_desempata_pelo_envio_mais_novo_e_pula_o_que_nao_chegou(dono: AsyncEngine, cena: Cena) -> None:
    """Dois pedidos iguais de O no mesmo passo (R1 e R2, nao confirmadas, com o numero de O): vale o envio mais novo,
    mesmo quando o id dele e o menor. Um terceiro, mais novo ainda, que terminou ``nao_saiu``, nao conta."""

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        o, envio_o = await _original(c, cena)
        r1 = await _repetir(c, cena, o)
        await apoio.enviar(c, r1.id)
        r2 = await _repetir(c, cena, o)
        await apoio.enviar(c, r2.id)
        r3 = await _repetir(c, cena, o)
        envio_r3 = await apoio.enviar(c, r3.id, "nao_saiu")
        menor, maior = sorted((r1.id, r2.id), key=str)
        await c.execute(text("ALTER TABLE envios_selo DISABLE TRIGGER envios_selo_sem_update_delete"))
        for operacao_id, minutos in ((menor, 1), (maior, 0), (r3.id, 2)):
            await c.execute(
                text("UPDATE envios_selo SET gravado_em = now() + make_interval(mins => :m) WHERE operacao_id = :o"),
                {"m": minutos, "o": operacao_id},
            )
        await c.execute(text("ALTER TABLE envios_selo ENABLE TRIGGER envios_selo_sem_update_delete"))
        return menor, envio_r3.chave == envio_o.chave, await _substituto(c, o.id)

    menor, r3_com_o_numero, substituto = await num_rascunho(dono, passos)
    assert r3_com_o_numero
    assert substituto == menor


# --- Decisao 1: so recibo e consulta queimam o numero ------------------------------------------------------------


@pytest.mark.parametrize("origem", ["recibo", "consulta"])
async def test_o_recibo_e_a_consulta_queimam_o_numero(dono: AsyncEngine, cena: Cena, origem: str) -> None:
    async def passos(c: AsyncConnection) -> tuple[apoio.Envio, apoio.Envio]:
        o, envio_o = await _original(c, cena)
        await apoio.informar(c, o.id, "recusada", origem=origem)
        r = await _repetir(c, cena, o)
        return envio_o, await apoio.gravar_envio(c, r.id)

    envio_o, envio_r = await num_rascunho(dono, passos)
    assert envio_r.chave != envio_o.chave
    assert envio_r.repete_operacao_id is None


# --- Decisao 4 (M4.2): fichas.numero_novo ----------------------------------------------------------------------


async def _ficha(c: AsyncConnection, operacao_id: uuid.UUID) -> uuid.UUID:
    return uuid.UUID(
        str((await c.execute(text("SELECT id FROM fichas WHERE operacao_id = :o"), {"o": operacao_id})).scalar_one())
    )


async def _confirmar(c: AsyncConnection, t: apoio.OperacaoDeTeste, *, numero_novo: bool, papel: str = ENTRADA) -> None:
    """O UPDATE da confirmacao, como ``fila.confirmar_ficha`` faz, com o papel ``papel``; volta ao dono no fim."""
    await como(c, papel)
    await c.execute(
        text(
            "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), decidida_por = :p,"
            " confirmada_com = 'sessao', numero_novo = :n WHERE operacao_id = :o AND decisao IS NULL"
        ),
        {"p": t.pessoa_id, "n": numero_novo, "o": t.id},
    )
    await c.execute(text("RESET ROLE"))


async def _liberar_e_enviar(c: AsyncConnection, t: apoio.OperacaoDeTeste) -> apoio.Envio:
    await c.execute(
        text("UPDATE fila_saida SET estado = 'liberada', hora_saida = now() WHERE operacao_id = :o"), {"o": t.id}
    )
    return await apoio.gravar_envio(c, t.id)


async def _em_desconhecido(c: AsyncConnection, cena: Cena) -> tuple[apoio.Envio, apoio.OperacaoDeTeste]:
    """O saiu com numero, a consulta respondeu ``desconhecido`` (o recibo, ``em_andamento``) e T, o pedido igual,
    espera a ficha."""
    o, envio_o = await _original(c, cena)
    await apoio.informar(c, o.id, "em_andamento", origem="recibo")
    await apoio.informar(c, o.id, "desconhecido")
    return envio_o, await _repetir(c, cena, o, liberada=False)


@pytest.mark.parametrize("numero_novo", [True, False])
async def test_com_a_escolha_da_pessoa_o_envio_sai_com_numero_novo(
    dono: AsyncEngine, cena: Cena, numero_novo: bool
) -> None:
    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        envio_o, t = await _em_desconhecido(c, cena)
        await _confirmar(c, t, numero_novo=numero_novo)
        gravado = (
            await c.execute(text("SELECT numero_novo FROM fichas WHERE operacao_id = :o"), {"o": t.id})
        ).scalar_one()
        return envio_o, await _liberar_e_enviar(c, t), gravado

    envio_o, envio_t, gravado = await num_rascunho(dono, passos)
    assert gravado is numero_novo
    if numero_novo:
        assert envio_t.chave != envio_o.chave
        assert envio_t.repete_operacao_id is None
    else:
        assert (envio_t.chave, envio_t.repete_operacao_id) == (envio_o.chave, envio_o.operacao_id)


@pytest.mark.parametrize(
    ("estrago", "mensagem"),
    [
        ("sem_confirmar", NUMERO_NOVO_SO_NA_CONFIRMACAO),
        ("depois_de_decidida", NUMERO_NOVO_SO_NA_CONFIRMACAO),
        ("ficha_vencida", NUMERO_NOVO_SO_NA_CONFIRMACAO),
        ("recusando_a_ficha", NUMERO_NOVO_SO_NA_CONFIRMACAO),
        ("sem_repeticao", NUMERO_NOVO_SO_COM_DESCONHECIDO),
        ("original_com_estado_final", NUMERO_NOVO_SO_COM_DESCONHECIDO),
        ("ultima_consulta_em_andamento", NUMERO_NOVO_SO_COM_DESCONHECIDO),
        ("so_o_recibo_desconhecido", NUMERO_NOVO_SO_COM_DESCONHECIDO),
        ("sem_resposta_a_consulta", NUMERO_NOVO_SO_COM_DESCONHECIDO),
    ],
)
async def test_a_guarda_recusa_o_numero_novo_fora_das_condicoes(
    dono: AsyncEngine, cena: Cena, estrago: str, mensagem: str
) -> None:
    """Cada caso tenta o UPDATE como a entrada (que tem o GRANT): so a guarda recusa."""

    async def passos(c: AsyncConnection) -> None:
        o, _ = await _original(c, cena)
        if estrago == "so_o_recibo_desconhecido":
            await apoio.informar(c, o.id, "desconhecido", origem="recibo")
        elif estrago == "sem_resposta_a_consulta":
            await apoio.informar(c, o.id)  # consulta sem resposta: nenhuma resposta de origem consulta
        elif estrago == "original_com_estado_final":
            # O recibo disse concluida e a consulta, depois, desconhecido (passagem permitida): a ultima resposta a
            # consulta e desconhecido, mas o estado final do banco e concluida; so ele recusa.
            await apoio.informar(c, o.id, "concluida", origem="recibo")
            await apoio.informar(c, o.id, "desconhecido")
        else:
            await apoio.informar(c, o.id, "desconhecido")
        if estrago == "ultima_consulta_em_andamento":
            await apoio.informar(c, o.id, "em_andamento")
        if estrago == "sem_repeticao":
            t = await apoio.criar_operacao(c, cena.sistema.acoes[0], liberada=False)
        else:
            t = await _repetir(c, cena, o, liberada=False)
        if estrago == "ficha_vencida":
            await c.execute(
                text("UPDATE fichas SET expira_em = now() - interval '1 minute' WHERE operacao_id = :o"), {"o": t.id}
            )
        if estrago == "depois_de_decidida":
            await _confirmar(c, t, numero_novo=False)
            await como(c, ENTRADA)
            await c.execute(text("UPDATE fichas SET numero_novo = true WHERE operacao_id = :o"), {"o": t.id})
        elif estrago == "sem_confirmar":
            await como(c, ENTRADA)
            await c.execute(text("UPDATE fichas SET numero_novo = true WHERE operacao_id = :o"), {"o": t.id})
        elif estrago == "recusando_a_ficha":
            await como(c, ENTRADA)
            await c.execute(
                text(
                    "UPDATE fichas SET decisao = 'recusada', decidida_em = now(), decidida_por = :p, numero_novo = true"
                    " WHERE operacao_id = :o"
                ),
                {"p": t.pessoa_id, "o": t.id},
            )
        else:
            await _confirmar(c, t, numero_novo=True)

    async with dono.connect() as c:
        transacao = await c.begin()
        try:
            with pytest.raises(DBAPIError, match=mensagem):
                await passos(c)
        finally:
            await transacao.rollback()


@pytest.mark.parametrize(
    "final_em",
    [
        "nenhuma",  # a cadeia sozinha nao recusa
        "primeira",  # a primeira acao com o numero (O) diz concluida; R2 e T so a repetem
        "irma",  # outro pedido igual de O (R1), com o mesmo numero, diz concluida
    ],
)
async def test_o_estado_final_conta_pelo_numero_da_original(dono: AsyncEngine, cena: Cena, final_em: str) -> None:
    """Defeito 2 do verificador do G4r (etapa 3r). O sai com o numero K; R1 e R2 sao pedidos iguais de O e levam K
    (o banco copia); T repete R2 e espera a ficha. A ultima resposta de R2 a consulta e ``desconhecido``. Com um
    estado final sobre K em qualquer acao da cadeia (O, que T so alcanca subindo, ou R1, que so se alcanca descendo de
    O), a guarda recusa o numero novo, e a leitura do codigo (``fila.por_id``) mostra o estado final do numero; sem
    estado final nenhum, aceita, e o envio de T sai com numero novo."""

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        o, envio_o = await _original(c, cena)
        await apoio.informar(c, o.id, "concluida" if final_em == "primeira" else "desconhecido")
        r1 = await _repetir(c, cena, o)
        envio_r1 = await apoio.enviar(c, r1.id)
        await apoio.informar(c, r1.id, "concluida" if final_em == "irma" else "desconhecido")
        r2 = await _repetir(c, cena, o)
        envio_r2 = await apoio.enviar(c, r2.id)
        await apoio.informar(c, r2.id, "desconhecido")
        t = await _repetir(c, cena, r2, liberada=False)
        lida = await fila.por_id(c, t.id)
        copiou = (envio_r1.chave, envio_r2.chave) == (envio_o.chave, envio_o.chave)
        try:
            async with c.begin_nested():
                await _confirmar(c, t, numero_novo=True)
        except DBAPIError as erro:
            return copiou, lida, str(erro), None
        return copiou, lida, None, (envio_o, await _liberar_e_enviar(c, t))

    copiou, lida, recusa, envios = await num_rascunho(dono, passos)
    assert copiou
    assert lida is not None
    assert lida.original is not None
    assert lida.original.ultima_consulta == "desconhecido"
    if final_em == "nenhuma":
        assert lida.original.estado_final_do_numero is None
        assert recusa is None
        assert envios is not None
        envio_o, envio_t = envios
        assert (envio_t.chave != envio_o.chave, envio_t.repete_operacao_id) == (True, None)
    else:
        assert lida.original.estado_final_do_numero == "concluida"
        assert recusa is not None
        assert NUMERO_NOVO_SO_COM_DESCONHECIDO in recusa


async def test_a_ficha_nasce_sem_o_numero_novo_e_a_escolha_nao_volta(dono: AsyncEngine, cena: Cena) -> None:
    async def nascer_com(c: AsyncConnection) -> None:
        o, _ = await _original(c, cena)
        await apoio.informar(c, o.id, "desconhecido")
        t = await _repetir(c, cena, o, liberada=False)
        codigo = (
            await c.execute(text("SELECT codigo_conferencia FROM operacoes WHERE id = :o"), {"o": t.id})
        ).scalar_one()
        # Uma segunda ficha para T: a guarda do INSERT (BEFORE) recusa antes de qualquer outra regra da tabela.
        await como(c, ENTRADA)
        await c.execute(
            text(
                "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco, numero_novo)"
                " VALUES (:o, :c, now() + interval '30 minutes', 'medio', true)"
            ),
            {"o": t.id, "c": codigo},
        )

    async def desfazer(c: AsyncConnection) -> None:
        _, t = await _em_desconhecido(c, cena)
        await _confirmar(c, t, numero_novo=True)
        await c.execute(text("UPDATE fichas SET numero_novo = false WHERE operacao_id = :o"), {"o": t.id})

    for passos, mensagem in (
        (nascer_com, "A escolha do numero novo so vem na confirmacao da ficha"),
        (desfazer, NUMERO_NOVO_SO_NA_CONFIRMACAO),
    ):
        async with dono.connect() as c:
            transacao = await c.begin()
            try:
                with pytest.raises(DBAPIError, match=mensagem):
                    await passos(c)
            finally:
                await transacao.rollback()


@pytest.mark.parametrize("papel", [DESPACHANTE, ROTINAS])
async def test_so_a_entrada_grava_o_numero_novo(dono: AsyncEngine, cena: Cena, papel: str) -> None:
    async def passos(c: AsyncConnection) -> None:
        _, t = await _em_desconhecido(c, cena)
        await como(c, papel)  # so a coluna nova: a falta de GRANT nas outras colunas da decisao nao conta aqui
        await c.execute(text("UPDATE fichas SET numero_novo = true WHERE operacao_id = :o"), {"o": t.id})

    async with dono.connect() as c:
        transacao = await c.begin()
        try:
            with pytest.raises(DBAPIError, match=SEM_PERMISSAO):
                await passos(c)
        finally:
            await transacao.rollback()
    async with dono.connect() as c:
        pode = {
            p: (
                await c.execute(text("SELECT has_column_privilege(:p, 'fichas', 'numero_novo', 'UPDATE')"), {"p": p})
            ).scalar_one()
            for p in (ENTRADA, DESPACHANTE, ROTINAS)
        }
    assert pode == {ENTRADA: True, DESPACHANTE: False, ROTINAS: False}
