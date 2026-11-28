"""Rotinas dos selos (desenho 5.2, 7.4 e 12.1 G5; criticas A2, A3.3, M7, M8, B8 e B9) contra PostgreSQL real, com o
usuario ``mcp_claro_rotinas`` (prova os GRANTs).

- ``conferir_pelo_sistema`` pega so as acoes em duvida com envio (a ``nao_confirmada`` sem resposta que encerra, e a
  ``enviada`` com o ultimo estado dito pelo recibo ou pela consulta em aberto), nunca a que tem outro protocolo com o
  mesmo numero depois, a parada em ``sistema_fora``, a do maximo de tentativas nem a do fim pelo tempo; respeita a
  escada pelo relogio do banco (o piso do prazo do envio mais a margem e o degrau), uma ``nao_recebida`` sozinha nao
  encerra, ate 20 por rodada (a vez mais antiga primeiro), duas copias fazem uma rodada so por vez, e a porta
  fechada, o despachante fora e o 503 nao dao a escada por acabada; o ``sem_envio`` e o erro inesperado so pulam a
  operacao; nenhuma transacao das rotinas fica aberta durante o pedido; as escadas que ja acabaram nao tomam o lote do
  SQL (500 delas, com o teto de verdade), que le primeiro as mais paradas; a tarefa rodada pelo agendador pergunta;
- pela rota de verdade do despachante (``criar_app_despachante``) e o duble de nivel 2: a escada inteira ate a
  segunda ``nao_recebida`` e a porta fechada (nada perguntado nem gravado, e a rodada seguinte pergunta);
- ``avisar_selos_a_vencer``: um aviso por teste, a 14 dias de vencer (``BADGE_WARN_DAYS`` da politica vigente), so
  no nivel 2 ou 3, na data do fuso das telas, com a linha no extrato; avisos quando o selo cai por reprovacao (o da
  bateria de verdade nao sai de novo), retirada (a do despachante nao sai de novo) ou mudanca do sistema, sem repetir,
  so dos ultimos 7 dias;
- o resumo semanal traz as contagens dos selos.

O relogio da escada e o do banco: as provas poem o envio e as respostas para tras (``atrasar``, como dono, numa
transacao curta que desliga e religa a guarda de "so de acrescentar"), com folga de dezenas de segundos de cada lado da
vez. Nada depende do relogio da maquina, e as esperas tem teto. O banco da sessao e de todos os testes: no comeco de
cada prova, as escadas que outros testes deixaram abertas param (``encerrar_escadas``, uma linha ``sistema_fora``),
e os avisos sao conferidos so pelo sistema da prova.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import selo_mentiroso, selo_nivel2
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.rotinas import selos
from mcp_claro.rotinas.agendador import Agendador, Desfecho
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.despachante import (
    ClienteDespachante,
    Conferida,
    DespachanteFora,
    NadaAPerguntar,
    PortaFechada,
    SemEnvio,
)
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita
from mcp_claro.rotinas.resumo_semanal import resumo_semanal, semana_anterior
from mcp_claro.selo import contrato
from mcp_claro.versao import VERSAO_MCP
from tests import apoio_g3a as g
from tests import apoio_selo_g3a as a
from tests import apoio_selos as s
from tests.integracao import test_selo_bateria as bateria

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PRAZO_ENVIO = 20.0
"""``RESPONSE_MAX_WAIT_SECONDS`` das provas (o padrao)."""
PISO = PRAZO_ENVIO + contrato.MARGEM_DA_CONSULTA_SEGUNDOS
"""Segundos depois do envio antes da primeira consulta (critica A3.3)."""
FOLGA = 20
"""Segundos de folga de cada lado da vez: a prova nunca depende de quanto a maquina demora entre um passo e outro."""
ADMIN = a.EMAIL_ADMIN


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    try:
        await g.abrir_a_saida(engine)
        await encerrar_escadas(engine)
        yield engine
    finally:
        await g.abrir_a_saida(engine)
        await engine.dispose()


@pytest.fixture
async def rotinas(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    try:
        yield engine
    finally:
        await engine.dispose()


# --- Apoio -----------------------------------------------------------------------------------------------------


async def encerrar_escadas(dono: AsyncEngine) -> None:
    """As escadas que outros testes da sessao deixaram abertas param (uma linha ``sistema_fora`` de consulta, como a
    rota grava quando o sistema sai de ativo): elas nao entram nas rodadas desta prova."""
    async with dono.begin() as c:
        await c.execute(
            text(
                "INSERT INTO informacoes_sistema (operacao_id, sistema_id, origem, desfecho)"
                " SELECT e.operacao_id, e.sistema_id, 'consulta', 'sistema_fora' FROM envios_selo e"
                " JOIN fila_saida f ON f.operacao_id = e.operacao_id"
                " WHERE f.estado IN ('nao_confirmada', 'enviada') AND coalesce((SELECT i.desfecho"
                " FROM informacoes_sistema i WHERE i.operacao_id = e.operacao_id AND i.origem = 'consulta'"
                " ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1), '') <> 'sistema_fora'"
            )
        )


async def atrasar(dono: AsyncEngine, segundos: Mapping[uuid.UUID, float]) -> None:
    """O envio e cada informacao de cada operacao ``segundos[op]`` para tras, como se o tempo tivesse passado."""
    async with dono.begin() as c:
        await c.execute(text("ALTER TABLE envios_selo DISABLE TRIGGER envios_selo_sem_update_delete"))
        await c.execute(text("ALTER TABLE informacoes_sistema DISABLE TRIGGER informacoes_sistema_sem_update_delete"))
        for operacao_id, quanto in segundos.items():
            for sql in (
                "UPDATE envios_selo SET gravado_em = gravado_em - make_interval(secs => :d) WHERE operacao_id = :o",
                "UPDATE informacoes_sistema SET gravado_em = gravado_em - make_interval(secs => :d)"
                " WHERE operacao_id = :o",
            ):
                await c.execute(text(sql), {"d": quanto, "o": operacao_id})
        await c.execute(text("ALTER TABLE informacoes_sistema ENABLE TRIGGER informacoes_sistema_sem_update_delete"))
        await c.execute(text("ALTER TABLE envios_selo ENABLE TRIGGER envios_selo_sem_update_delete"))


async def faltam(dono: AsyncEngine, teste_id: uuid.UUID) -> timedelta:
    """Quanto falta, pelo relogio do banco, para o teste vencer."""
    async with dono.connect() as c:
        valor: timedelta = (
            await c.execute(text("SELECT vale_ate - now() FROM testes_selo WHERE id = :t"), {"t": teste_id})
        ).scalar_one()
    return valor


async def mudar_endereco(dono: AsyncEngine, sistema_id: uuid.UUID, endereco: str) -> None:
    """O endereco muda (a guarda do cadastro poe o sistema em ``pendente``) e a sondagem o ativa de novo, como o
    despachante faria: o selo cai por ``endereco_mudou`` (ou volta, se o endereco e o do teste)."""
    async with dono.begin() as c:
        await c.execute(text("UPDATE sistemas SET endereco = :e WHERE id = :s"), {"e": endereco, "s": sistema_id})
        await c.execute(
            text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = :v WHERE id = :s"),
            {"v": VERSAO_MCP, "s": sistema_id},
        )


async def atrasar_teste(dono: AsyncEngine, teste_id: uuid.UUID, intervalo: timedelta) -> Any:
    """O teste ``intervalo`` para tras (as tres horas dele), como dono. Devolve ``testado_em`` e ``vale_ate``."""
    async with dono.begin() as c:
        await c.execute(text("ALTER TABLE testes_selo DISABLE TRIGGER testes_selo_sem_update_delete"))
        linha = (
            await c.execute(
                text(
                    "UPDATE testes_selo SET iniciado_em = iniciado_em - CAST(:d AS interval),"
                    " testado_em = testado_em - CAST(:d AS interval), vale_ate = vale_ate - CAST(:d AS interval)"
                    " WHERE id = :t RETURNING testado_em, vale_ate"
                ),
                {"d": intervalo, "t": teste_id},
            )
        ).one()
        await c.execute(text("ALTER TABLE testes_selo ENABLE TRIGGER testes_selo_sem_update_delete"))
    return linha


@dataclass(frozen=True, slots=True)
class Cenario:
    sistema: s.SistemaDoSelo
    teste: s.TesteGravado | None
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID


async def com_selo(dono: AsyncEngine, nivel: int | None = 2) -> Cenario:
    """Um sistema proprio com o teste aprovado de ``nivel`` (um dia para tras: as provas poem horas para tras e
    continuam depois dele), ou sem teste com ``None``, e uma pessoa de teste."""
    async with dono.begin() as c:
        sistema = await s.proprio_com_acoes(c, nivel_do_contrato=nivel or 3)
        teste = await s.gravar_teste(c, sistema.id, nivel=nivel) if nivel is not None else None
        if teste is not None:
            teste = await a.teste_de_ontem(c, teste)
        pessoa = await s.nova_pessoa(c, teste=True)
        aplicativo = await s.novo_aplicativo(c)
    return Cenario(sistema, teste, pessoa, aplicativo)


async def acao(
    dono: AsyncEngine,
    cn: Cenario,
    fim: str | None = "nao_confirmada",
    *,
    informar: Sequence[tuple[str | None, str, str]] = (),
    repete: s.OperacaoDeTeste | None = None,
    com_envio: bool = True,
) -> s.OperacaoDeTeste:
    """Uma acao da pessoa do cenario, enviada com numero (``com_envio``) e com a fila em ``fim``, e as linhas de
    ``informacoes_sistema`` ``informar`` (estado, origem, desfecho), todas na mesma transacao."""
    async with dono.begin() as c:
        argumentos = (
            repete.argumentos if repete else {"para": "caixa-de-teste@exemplo.teste", "texto": uuid.uuid4().hex}
        )
        op = await s.criar_operacao(
            c,
            cn.sistema.acoes[0],
            pessoa_id=cn.pessoa_id,
            aplicativo_id=cn.aplicativo_id,
            argumentos=argumentos,
            repete=repete.id if repete else None,
        )
        if com_envio:
            await s.enviar(c, op.id, fim=fim)
        elif fim is not None:
            for estado in ("enviando", fim):
                await c.execute(
                    text("UPDATE fila_saida SET estado = :e, mudou_por = 'despachante' WHERE operacao_id = :o"),
                    {"e": estado, "o": op.id},
                )
        for estado, origem, desfecho in informar:
            await s.informar(c, op.id, estado, origem=origem, desfecho=desfecho)
    return op


@dataclass
class DespachanteFalso:
    """O despachante, do lado das rotinas: conta os pedidos e, como a rota, grava a pergunta em
    ``informacoes_sistema`` (pelo dono) e devolve o desfecho. ``erros`` por operacao (ou ``erro`` para todas)
    levantam no lugar; ``solta`` segura a resposta ate ser ligada (com teto); ``durante`` roda no meio do pedido,
    enquanto as rotinas esperam a resposta."""

    dono: AsyncEngine
    desfecho: str = "sem_resposta"
    estado: str | None = None
    erro: Exception | None = None
    erros: dict[uuid.UUID, Exception] = field(default_factory=dict)
    comecou: asyncio.Event = field(default_factory=asyncio.Event)
    solta: asyncio.Event | None = None
    durante: Callable[[uuid.UUID], Awaitable[None]] | None = None
    pedidos: list[uuid.UUID] = field(default_factory=list)

    async def conferir(self, operacao_id: uuid.UUID) -> Conferida:
        self.pedidos.append(operacao_id)
        self.comecou.set()
        if self.solta is not None:
            await asyncio.wait_for(self.solta.wait(), 10)
        if self.durante is not None:
            await self.durante(operacao_id)
        erro = self.erros.get(operacao_id, self.erro)
        if erro is not None:
            raise erro
        async with self.dono.begin() as c:
            await s.informar(c, operacao_id, self.estado, origem="consulta", desfecho=self.desfecho)
        return Conferida(self.desfecho, self.estado)


async def rodada(
    rotinas: AsyncEngine, despachante: Any, *, limite: int = selos.LIMITE_POR_RODADA
) -> selos.RodadaDaConsulta:
    return await selos.conferir_pelo_sistema(
        rotinas, despachante, maximo=8, prazo_envio_segundos=PRAZO_ENVIO, limite=limite
    )


async def avisos(dono: AsyncEngine, sistema_id: uuid.UUID, evento: str | None = None) -> list[Any]:
    async with dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT evento, assunto, corpo, chave_unica, destino FROM avisos"
                        " WHERE corpo ->> 'sistema_id' = :s AND (CAST(:e AS text) IS NULL OR evento = :e) ORDER BY id"
                    ),
                    {"s": str(sistema_id), "e": evento},
                )
            ).all()
        )


async def extrato_do_sistema(dono: AsyncEngine, sistema_id: uuid.UUID, tipo: str) -> list[Any]:
    async with dono.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT frase, dados, papel FROM extrato WHERE sistema_id = :s AND tipo = :t ORDER BY id"),
                    {"s": sistema_id, "t": tipo},
                )
            ).all()
        )


class Espia:
    """Os contadores somados pelas rotinas dos selos, sem deixar de somar."""

    def __init__(self, contadores: Any) -> None:
        self.contadores = contadores
        self.somados: list[tuple[str, dict[str, str]]] = []

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        self.somados.append((nome, rotulos))
        self.contadores.somar(nome, valor, **rotulos)


# --- conferir_pelo_sistema: quem entra ---------------------------------------------------------------------------


async def test_pega_so_as_acoes_em_duvida_com_envio(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """Desenho 5.2 e critica B9: a "nao confirmada" com envio e sem resposta que encerra, e a "enviada" com o ultimo
    estado dito pelo recibo ou pela consulta em aberto. Fora: sem envio, resposta que encerra (inclusive a segunda
    ``nao_recebida``), outro protocolo com o mesmo numero depois, ``sistema_fora`` na ultima consulta, o maximo de
    tentativas e o fim pelo tempo."""
    n2, n3, sem_selo = await com_selo(dono, 2), await com_selo(dono, 3), await com_selo(dono, None)
    entram = {
        "nao_confirmada": await acao(dono, n2),
        "recibo_em_andamento": await acao(dono, n3, "enviada", informar=[("em_andamento", "recibo", "respondeu")]),
        "recibo_desconhecido": await acao(dono, n3, "enviada", informar=[("desconhecido", "recibo", "respondeu")]),
        "consulta_em_andamento": await acao(
            dono,
            n3,
            "enviada",
            informar=[("em_andamento", "recibo", "respondeu"), ("em_andamento", "consulta", "respondeu")],
        ),
        "uma_nao_recebida": await acao(dono, n2, informar=[("nao_recebida", "consulta", "respondeu")]),
        "sem_chave": await acao(dono, n2, informar=[(None, "consulta", "sem_chave")]),
    }
    original = await acao(dono, n2)
    ficam = {
        "sem_envio": await acao(dono, sem_selo, com_envio=False),
        "recibo_concluida": await acao(dono, n3, "enviada", informar=[("concluida", "recibo", "respondeu")]),
        "enviada_sem_recibo": await acao(dono, n2, "enviada"),
        "consulta_concluida": await acao(dono, n2, informar=[("concluida", "consulta", "respondeu")]),
        "recibo_e_consulta_final": await acao(
            dono,
            n3,
            "enviada",
            informar=[("em_andamento", "recibo", "respondeu"), ("concluida", "consulta", "respondeu")],
        ),
        "substituida": original,
        "pedido_igual_enviado": await acao(dono, n2, "enviada", repete=original),
        "sistema_fora": await acao(dono, n2, informar=[(None, "consulta", "sistema_fora")]),
        "maximo": await acao(dono, n2, informar=[(None, "consulta", "sem_resposta")] * 8),
        "fim_pelo_tempo": await acao(dono, n2),
    }
    duas_nao_recebidas = await acao(dono, n2, informar=[("nao_recebida", "consulta", "respondeu")])
    await atrasar(dono, {duas_nao_recebidas.id: 120})
    async with dono.begin() as c:
        await s.informar(c, duas_nao_recebidas.id, "nao_recebida")
    ficam["duas_nao_recebidas"] = duas_nao_recebidas
    todas = [*entram.values(), *ficam.values()]
    await atrasar(dono, {op.id: 6 * 3600 for op in todas})
    await atrasar(dono, {ficam["fim_pelo_tempo"].id: contrato.REAPROVEITAR_NUMERO_DIAS * 86400})
    async with dono.connect() as c:
        substituto = (await c.execute(text("SELECT selo_substituto(:o)"), {"o": original.id})).scalar_one()
    assert substituto == ficam["pedido_igual_enviado"].id

    falso = DespachanteFalso(dono)
    feita = await rodada(rotinas, falso)
    nomes = {op.id: nome for nome, op in [*entram.items(), *ficam.items()]}
    assert sorted(nomes.get(x, str(x)) for x in falso.pedidos) == sorted(entram)
    assert feita.na_vez == len(entram)
    assert set(feita.perguntadas) == {op.id for op in entram.values()}


# --- conferir_pelo_sistema: a escada pelo relogio do banco -------------------------------------------------------


async def test_escada_pelo_relogio_do_banco(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """A primeira consulta so depois do prazo do envio mais a margem (critica A3.3); depois, o degrau conta da ultima
    linha. A comparacao e com o ``now()`` do banco: o envio e as linhas vao para tras, com folga dos dois lados."""
    cn = await com_selo(dono)
    op = await acao(dono, cn)
    falso = DespachanteFalso(dono)
    assert (await rodada(rotinas, falso)).na_vez == 0
    await atrasar(dono, {op.id: PISO - FOLGA})
    assert (await rodada(rotinas, falso)).na_vez == 0
    assert falso.pedidos == []
    await atrasar(dono, {op.id: 2 * FOLGA})
    assert set((await rodada(rotinas, falso)).perguntadas) == {op.id}
    # A pergunta gravou uma linha agora: a vez seguinte e o segundo degrau depois dela.
    degrau = contrato.ESCADA_DA_CONSULTA_MINUTOS[1] * 60
    assert (await rodada(rotinas, falso)).na_vez == 0
    await atrasar(dono, {op.id: degrau - FOLGA})
    assert (await rodada(rotinas, falso)).na_vez == 0
    await atrasar(dono, {op.id: 2 * FOLGA})
    assert set((await rodada(rotinas, falso)).perguntadas) == {op.id}
    assert falso.pedidos == [op.id, op.id]


async def test_nao_recebida_sozinha_nao_encerra(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """Critica A3.3: depois de uma ``nao_recebida``, as rotinas perguntam de novo no degrau seguinte; a segunda, com
    intervalo, encerra a escada."""
    cn = await com_selo(dono)
    op = await acao(dono, cn)
    falso = DespachanteFalso(dono, desfecho="respondeu", estado="nao_recebida")
    await atrasar(dono, {op.id: 6 * 3600})
    await rodada(rotinas, falso)
    await atrasar(dono, {op.id: 6 * 3600})
    await rodada(rotinas, falso)
    assert falso.pedidos == [op.id, op.id]
    await atrasar(dono, {op.id: 6 * 3600})
    assert (await rodada(rotinas, falso)).na_vez == 0
    assert falso.pedidos == [op.id, op.id]


async def test_ate_vinte_por_rodada_da_vez_mais_antiga(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """Desenho 5.2: ate 20 operacoes por rodada (o padrao de ``conferir_pelo_sistema``); as outras ficam para a
    rodada seguinte."""
    cn = await com_selo(dono)
    ops = [await acao(dono, cn) for _ in range(23)]
    # Quanto mais para tras, mais antiga a vez: a ultima da lista e a primeira a ser perguntada.
    await atrasar(dono, {op.id: PISO + 60 + 10 * i for i, op in enumerate(ops)})
    falso = DespachanteFalso(dono)
    primeira = await selos.conferir_pelo_sistema(rotinas, falso, maximo=8, prazo_envio_segundos=PRAZO_ENVIO)
    esperadas = [op.id for op in reversed(ops)]
    assert (primeira.na_vez, falso.pedidos) == (20, esperadas[:20])
    segunda = await selos.conferir_pelo_sistema(rotinas, falso, maximo=8, prazo_envio_segundos=PRAZO_ENVIO)
    assert segunda.na_vez == 3
    assert falso.pedidos[20:] == esperadas[20:]


async def test_a_vez_mais_antiga_vem_primeiro_mesmo_com_degrau_maior(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """A ordem e a da vez (``contrato.proxima_consulta``), nao a da ultima linha: uma operacao com a ultima resposta
    mais antiga, mas num degrau mais alto, pode ter a vez mais nova."""
    cn = await com_selo(dono)
    degrau_alto = await acao(dono, cn, informar=[(None, "consulta", "sem_resposta")] * 3)  # proxima: 15 minutos
    sem_linhas = await acao(dono, cn)  # proxima: o piso do envio
    await atrasar(dono, {degrau_alto.id: 20 * 60, sem_linhas.id: 10 * 60})
    falso = DespachanteFalso(dono)
    assert (await rodada(rotinas, falso, limite=1)).na_vez == 1
    assert falso.pedidos == [sem_linhas.id]  # vez ha 8 minutos e meio; a outra, ha 5
    await rodada(rotinas, falso, limite=1)
    assert falso.pedidos == [sem_linhas.id, degrau_alto.id]


async def test_duas_copias_fazem_uma_rodada_por_vez(
    dono: AsyncEngine, rotinas: AsyncEngine, banco_migrado: Parametros
) -> None:
    """Critica M8 e desenho 5.2: com duas copias das rotinas no ar, a trava consultiva da rodada deixa uma so
    perguntar; a outra nao pede nada."""
    cn = await com_selo(dono)
    ops = [await acao(dono, cn) for _ in range(2)]
    await atrasar(dono, {op.id: 6 * 3600 for op in ops})
    falso = DespachanteFalso(dono, solta=asyncio.Event())
    outra_copia = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    try:
        primeira = asyncio.create_task(rodada(rotinas, falso))
        await asyncio.wait_for(falso.comecou.wait(), 10)
        segunda = await rodada(outra_copia, falso)
        assert segunda.ocupada
        assert segunda.na_vez == 0
        assert falso.solta is not None
        falso.solta.set()
        feita = await asyncio.wait_for(primeira, 30)
    finally:
        await outra_copia.dispose()
    assert not feita.ocupada
    assert sorted(falso.pedidos) == sorted(op.id for op in ops)
    assert not (await rodada(rotinas, falso)).ocupada  # a trava saiu no fim da rodada


async def test_porta_fechada_e_despachante_fora_param_a_rodada_sem_acabar_a_escada(
    dono: AsyncEngine, rotinas: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decisao 2 do coordenador no retoque da etapa 3: o 409 ``porta_fechada`` deixa a operacao para a proxima
    rodada, sem contar e sem dar a escada por acabada; o despachante fora tambem para a rodada. O 503 so pula aquela
    operacao. ``nada_a_perguntar`` pula."""
    espia = Espia(selos.CONTADORES)
    monkeypatch.setattr(selos, "CONTADORES", espia)
    cn = await com_selo(dono)
    ops = [await acao(dono, cn) for _ in range(3)]
    await atrasar(dono, {op.id: 6 * 3600 + 10 * i for i, op in enumerate(ops)})
    for erro, motivo in ((PortaFechada("x"), "porta_fechada"), (DespachanteFora("x"), "despachante_fora")):
        falso = DespachanteFalso(dono, erro=erro)
        parada = await rodada(rotinas, falso)
        assert (parada.parou, parada.na_vez, len(falso.pedidos), parada.perguntadas) == (motivo, 3, 1, {})
    # Nada foi gravado: a proxima rodada acha as tres na vez e pergunta todas. Um 503 so pula a dele.
    falso = DespachanteFalso(dono, erros={ops[1].id: ConferenciaNaoFeita("503")})
    seguiu = await rodada(rotinas, falso)
    assert (seguiu.parou, falso.pedidos) == (None, [op.id for op in reversed(ops)])
    assert seguiu.nao_feitas == [ops[1].id]
    assert set(seguiu.perguntadas) == {ops[0].id, ops[2].id}
    falso = DespachanteFalso(dono, erro=NadaAPerguntar("x"))
    pulou = await rodada(rotinas, falso)
    assert (pulou.puladas, falso.pedidos) == ([ops[1].id], [ops[1].id])
    resultados = [r["resultado"] for n, r in espia.somados if n == "selo_consultas_pedidas"]
    assert resultados == ["porta_fechada", "despachante_fora", "feita", "nao_feita", "feita", "nada_a_perguntar"]


async def test_sem_envio_e_erro_inesperado_so_pulam_a_operacao(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """O 404 ``sem_envio`` e uma falha que ninguem esperava ficam so naquela operacao: as outras da rodada ainda sao
    perguntadas, e a rodada nao para."""
    cn = await com_selo(dono)
    ops = [await acao(dono, cn) for _ in range(3)]
    await atrasar(dono, {op.id: 6 * 3600 + 10 * i for i, op in enumerate(ops)})
    falso = DespachanteFalso(dono, erros={ops[2].id: SemEnvio("x"), ops[1].id: RuntimeError("x")})
    feita = await rodada(rotinas, falso)
    assert (feita.parou, falso.pedidos) == (None, [op.id for op in reversed(ops)])
    assert (feita.puladas, feita.nao_feitas, set(feita.perguntadas)) == ([ops[2].id], [ops[1].id], {ops[0].id})


async def test_nenhuma_transacao_fica_aberta_enquanto_o_despachante_pergunta(
    dono: AsyncEngine, rotinas: AsyncEngine
) -> None:
    """Pedido do G0r: a leitura da rodada fecha a transacao antes do primeiro pedido. No meio de cada pedido, nenhuma
    conexao a mais das rotinas esta em transacao (``pg_stat_activity``, lido pelo dono, que ve todas, pelo
    ``application_name`` do engine do papel); a da trava da rodada esta la, parada."""
    cn = await com_selo(dono)
    ops = [await acao(dono, cn) for _ in range(2)]
    await atrasar(dono, {op.id: 6 * 3600 for op in ops})

    async def conexoes_das_rotinas() -> tuple[int, int]:
        async with dono.connect() as c:
            linha = (
                await c.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE xact_start IS NOT NULL), count(*) FILTER (WHERE state = 'idle')"
                        " FROM pg_stat_activity WHERE application_name = 'teste-g3a-mcp_claro_rotinas'"
                        " AND datname = current_database()"
                    )
                )
            ).one()
        return int(linha[0]), int(linha[1])

    vistas: list[tuple[int, int]] = []

    async def olhar(_: uuid.UUID) -> None:
        vistas.append(await conexoes_das_rotinas())

    em_transacao_antes, _ = await conexoes_das_rotinas()
    falso = DespachanteFalso(dono, durante=olhar)
    assert set((await rodada(rotinas, falso)).perguntadas) == {op.id for op in ops}
    assert len(vistas) == 2
    assert all(em_transacao == em_transacao_antes and paradas >= 1 for em_transacao, paradas in vistas), vistas


async def test_escadas_acabadas_nao_tomam_o_lote(
    dono: AsyncEngine, rotinas: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defeito S1 do verificador da etapa 4: o SQL tira, antes do ``LIMIT``, toda escada que ja acabou (resposta que
    encerra, a segunda ``nao_recebida``, ``sistema_fora`` na ultima consulta, outro protocolo com o mesmo numero
    depois, o maximo, o fim pelo tempo, a ``enviada`` sem duvida). Elas nunca ganham linha nova e, se entrassem,
    ficariam na frente do lote (as mais paradas primeiro). Com o lote de uma so, a acao na vez ainda e perguntada."""
    monkeypatch.setattr(selos, "CANDIDATAS_LIDAS", 1)
    cn = await com_selo(dono)
    n3 = await com_selo(dono, 3)
    original = await acao(dono, cn)
    acabadas = [
        await acao(dono, cn, informar=[("concluida", "consulta", "respondeu")]),
        await acao(dono, cn, informar=[(None, "consulta", "sistema_fora")]),
        await acao(dono, cn, informar=[(None, "consulta", "sem_resposta"), (None, "consulta", "sistema_fora")]),
        await acao(dono, cn, informar=[(None, "consulta", "sem_resposta")] * 8),
        await acao(dono, n3, "enviada", informar=[("concluida", "recibo", "respondeu")]),
        await acao(dono, cn, "enviada"),
        original,
        await acao(dono, cn, "enviada", repete=original),
    ]
    duas_nao_recebidas = await acao(dono, cn, informar=[("nao_recebida", "consulta", "respondeu")])
    await atrasar(dono, {duas_nao_recebidas.id: 120})
    async with dono.begin() as c:
        await s.informar(c, duas_nao_recebidas.id, "nao_recebida")
    acabadas.append(duas_nao_recebidas)
    vencida = await acao(dono, cn)
    ativa = await acao(dono, cn)
    await atrasar(dono, {op.id: 3 * 3600 for op in acabadas} | {ativa.id: 2 * 3600})
    await atrasar(dono, {vencida.id: contrato.REAPROVEITAR_NUMERO_DIAS * 86400 + 3600})
    falso = DespachanteFalso(dono)
    feita = await rodada(rotinas, falso)
    assert (falso.pedidos, feita.na_vez) == ([ativa.id], 1)


async def test_quinhentas_escadas_acabadas_e_a_acao_na_vez_ainda_e_perguntada(
    dono: AsyncEngine, rotinas: AsyncEngine
) -> None:
    """Sonda S1b do verificador da etapa 4, com o teto de verdade (``CANDIDATAS_LIDAS``): uma semana com 500 acoes
    "nao confirmadas" cuja escada ja acabou (o sistema respondeu ``concluida``) e uma acao na vez ha horas. A rodada
    pergunta a acao na vez."""
    cn = await com_selo(dono)
    async with dono.begin() as c:
        acabadas = []
        for i in range(selos.CANDIDATAS_LIDAS):
            op = await s.criar_operacao(
                c,
                cn.sistema.acoes[0],
                pessoa_id=cn.pessoa_id,
                aplicativo_id=cn.aplicativo_id,
                argumentos={"para": "caixa-de-teste@exemplo.teste", "texto": f"acabada {i}"},
            )
            await s.enviar(c, op.id, fim="nao_confirmada")
            await s.informar(c, op.id, "concluida", origem="consulta", desfecho="respondeu")
            acabadas.append(op.id)
    ativa = await acao(dono, cn)
    await atrasar(dono, dict.fromkeys(acabadas, 3 * 3600) | {ativa.id: 2 * 3600})
    falso = DespachanteFalso(dono)
    feita = await rodada(rotinas, falso)
    assert (falso.pedidos, feita.na_vez) == ([ativa.id], 1)


async def test_lote_cheio_le_primeiro_as_mais_paradas(
    dono: AsyncEngine, rotinas: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Com mais escadas abertas que o lote, o SQL le primeiro as paradas ha mais tempo (a hora base da escada); a
    pergunta poe a operacao no fim, e a rodada seguinte le a outra."""
    monkeypatch.setattr(selos, "CANDIDATAS_LIDAS", 1)
    cn = await com_selo(dono)
    mais_parada, menos_parada = await acao(dono, cn), await acao(dono, cn)
    await atrasar(dono, {mais_parada.id: 3 * 3600, menos_parada.id: 2 * 3600})
    falso = DespachanteFalso(dono)
    await rodada(rotinas, falso)
    await rodada(rotinas, falso)
    assert falso.pedidos == [mais_parada.id, menos_parada.id]


async def test_tarefa_pelo_agendador_chega_ao_despachante(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """Sonda S4 do verificador da etapa 4: a tarefa ``conferir_pelo_sistema`` de ``tarefas_dos_selos``, rodada pelo
    agendador (que segura a trava com o nome da tarefa noutra conexao), pergunta. A trava da rodada tem outro nome
    (``TRAVA_DA_RODADA``): com o mesmo, a rodada ficaria sempre ocupada."""
    cn = await com_selo(dono)
    op = await acao(dono, cn)
    await atrasar(dono, {op.id: 6 * 3600})
    falso = DespachanteFalso(dono)
    [tarefa, _] = selos.tarefas_dos_selos(rotinas, Parametros(), ConfigRotinas(), falso)
    assert tarefa.nome == "conferir_pelo_sistema"
    assert await Agendador(rotinas, [tarefa]).rodar_uma(tarefa) == Desfecho.FEITA
    assert falso.pedidos == [op.id]


# --- conferir_pelo_sistema pela rota de verdade -------------------------------------------------------------------


@pytest.fixture
async def pela_rota(banco_migrado: Parametros) -> AsyncIterator[tuple[a.Cena, ClienteDespachante, Parametros]]:
    """O duble de nivel 2, o despachante de verdade (``criar_app_despachante``, com o usuario do despachante) e o
    cliente das rotinas falando com ele (sem mTLS: o G9 prova o caminho com o cliente mTLS)."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        p = banco_migrado.model_copy(update={"APP_ENV": "teste", "BADGE_LOOKUP_TIMEOUT_SECONDS": 2})
        app = criar_app_despachante(p, cn.desp, cn.cifra)
        transporte = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        http = httpx2.AsyncClient(transport=transporte, base_url="http://despachante")
        cliente = ClienteDespachante(p, http=http)
        try:
            yield cn, cliente, p
        finally:
            await cliente.fechar()


async def test_escada_inteira_pela_rota_do_despachante(pela_rota: Any, rotinas: AsyncEngine) -> None:
    """O sistema nunca recebeu o numero: diz ``nao_recebida``. A primeira nao encerra; a segunda, com intervalo,
    encerra. Antes da vez, as rotinas nao pedem nada (e o duble nao e chamado)."""
    cn, cliente, p = pela_rota
    await encerrar_escadas(cn.dono)
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id, aplicativo_id=cn.aplicativo_id)
        await s.enviar(c, op.id)

    async def uma() -> selos.RodadaDaConsulta:
        return await selos.conferir_pelo_sistema(
            rotinas, cliente, maximo=p.BADGE_LOOKUP_MAX_ATTEMPTS, prazo_envio_segundos=p.RESPONSE_MAX_WAIT_SECONDS
        )

    assert (await uma()).na_vez == 0
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
    await a.voltar_no_tempo(cn, [op.id])
    assert (await uma()).perguntadas == {op.id: Conferida("respondeu", "nao_recebida")}
    assert (await uma()).na_vez == 0  # o degrau seguinte conta desta resposta
    await a.voltar_no_tempo(cn, [op.id])
    assert (await uma()).perguntadas == {op.id: Conferida("respondeu", "nao_recebida")}
    await a.voltar_no_tempo(cn, [op.id])
    assert (await uma()).na_vez == 0  # duas nao_recebida com intervalo: encerrou
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 2
    assert [x.estado for x in await a.informacoes(cn, op.id)] == ["nao_recebida", "nao_recebida"]
    assert cn.chamadas(a.ACAO) == 0  # nada foi reenviado


async def test_porta_fechada_pela_rota_nao_grava_e_a_rodada_seguinte_pergunta(
    pela_rota: Any, rotinas: AsyncEngine
) -> None:
    cn, cliente, p = pela_rota
    await encerrar_escadas(cn.dono)
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id, aplicativo_id=cn.aplicativo_id)
        await s.enviar(c, op.id)
        await c.execute(
            text("UPDATE estado_saida SET aberta = false, motivo = 'teste', mudou_por = 'teste-g5' WHERE id = 1")
        )
    await a.voltar_no_tempo(cn, [op.id])
    kw = {"maximo": p.BADGE_LOOKUP_MAX_ATTEMPTS, "prazo_envio_segundos": p.RESPONSE_MAX_WAIT_SECONDS}
    try:
        fechada = await selos.conferir_pelo_sistema(rotinas, cliente, **kw)
    finally:
        await g.abrir_a_saida(cn.dono)
    assert (fechada.parou, fechada.perguntadas) == ("porta_fechada", {})
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
    assert await a.informacoes(cn, op.id) == []
    aberta = await selos.conferir_pelo_sistema(rotinas, cliente, **kw)
    assert aberta.perguntadas == {op.id: Conferida("respondeu", "nao_recebida")}
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


# --- Avisos ao ADMIN sobre os selos --------------------------------------------------------------------------


async def test_aviso_de_selo_a_vencer_um_por_teste_a_14_dias(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    cn = await com_selo(dono, 3)
    assert cn.teste is not None
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert await avisos(dono, cn.sistema.id, "selo_a_vencer") == []
    # A 15 dias de vencer: ainda nao (BADGE_WARN_DAYS = 14).
    await atrasar_teste(dono, cn.teste.id, await faltam(dono, cn.teste.id) - timedelta(days=15))
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert await avisos(dono, cn.sistema.id, "selo_a_vencer") == []
    # A 13 dias: o aviso, uma vez, com a linha do extrato.
    linha = await atrasar_teste(dono, cn.teste.id, timedelta(days=2))
    for _ in range(2):
        await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    [aviso] = await avisos(dono, cn.sistema.id, "selo_a_vencer")
    assert aviso.chave_unica == f"selo_a_vencer:{cn.teste.id}|{ADMIN}"
    assert aviso.destino == ADMIN
    assert aviso.assunto == "MCP-CLARO: selo de sistema perto de vencer"
    data = linha.vale_ate.astimezone(ZoneInfo(selos.FUSO_PADRAO)).strftime("%d/%m/%Y")
    assert f"O selo de nivel 3 do sistema {cn.sistema.nome} vence em 13 dias ({data})." in aviso.corpo["texto"]
    assert aviso.corpo["nivel"] == 3
    [extrato] = await extrato_do_sistema(dono, cn.sistema.id, "selo_a_vencer")
    assert extrato.frase == f"O selo de nivel 3 do sistema {cn.sistema.nome} vence em {data}; o ADMIN foi avisado."
    assert extrato.papel == "rotinas"
    assert extrato.dados == {"teste_id": str(cn.teste.id), "dias_de_aviso": 14, "destinos": 1}
    # Um teste novo do mesmo sistema tem o seu aviso (um por teste, nao por sistema).
    async with dono.begin() as c:
        novo = await s.gravar_teste(c, cn.sistema.id, nivel=3)
    await atrasar_teste(dono, novo.id, await faltam(dono, novo.id) - timedelta(days=13))
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert [x.chave_unica for x in await avisos(dono, cn.sistema.id, "selo_a_vencer")] == [
        f"selo_a_vencer:{cn.teste.id}|{ADMIN}",
        f"selo_a_vencer:{novo.id}|{ADMIN}",
    ]


async def test_politica_vigente_muda_o_aviso_do_selo(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    cn = await com_selo(dono, 2)
    assert cn.teste is not None
    await atrasar_teste(dono, cn.teste.id, await faltam(dono, cn.teste.id) - timedelta(days=20))
    async with dono.begin() as c:
        await c.execute(
            text("INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_WARN_DAYS', 25, 't', 'teste')")
        )
    try:
        await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    finally:
        async with dono.begin() as c:
            await c.execute(
                text("INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_WARN_DAYS', 14, 't', 'volta')")
            )
    [aviso] = await avisos(dono, cn.sistema.id, "selo_a_vencer")
    assert "vence em 20 dias" in aviso.corpo["texto"]


async def test_avisos_quando_o_selo_cai_sem_repetir(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """Desenho 7.4: o teste que nao foi aprovado, a retirada pelo ADMIN e a mudanca do sistema avisam o ADMIN, uma
    vez por teste e motivo. O selo vencido nao tem aviso de queda (teve o de antes de vencer)."""
    reprovado = await com_selo(dono, 2)
    async with dono.begin() as c:
        reteste = await s.gravar_teste(c, reprovado.sistema.id, nivel=2, aprovado=False)
    retirado, mudou, vencido = await com_selo(dono, 2), await com_selo(dono, 2), await com_selo(dono, 2)
    assert retirado.teste is not None
    assert mudou.teste is not None
    assert vencido.teste is not None
    async with dono.begin() as c:
        admin = await s.nova_pessoa(c, admin=True)
        await c.execute(
            text(
                "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                " VALUES (:s, :t, 'admin', 'o sistema vai mudar', 'x', :p)"
            ),
            {"s": retirado.sistema.id, "t": retirado.teste.id, "p": admin},
        )
    endereco = mudou.sistema.endereco
    await mudar_endereco(dono, mudou.sistema.id, endereco.replace("/mcp", "/mcp2"))
    await atrasar_teste(dono, vencido.teste.id, timedelta(days=100))
    for _ in range(2):
        await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))

    [nao_aprovado] = await avisos(dono, reprovado.sistema.id)
    assert (nao_aprovado.evento, nao_aprovado.chave_unica) == (
        "selo_nao_aprovado",
        f"selo_nao_aprovado|{reteste.id}|{ADMIN}",
    )
    assert nao_aprovado.corpo["falhas"] == 1
    assert (
        f"O teste de selo nivel 2 do sistema {reprovado.sistema.nome} nao foi aprovado" in nao_aprovado.corpo["texto"]
    )
    [pelo_admin] = await avisos(dono, retirado.sistema.id)
    assert pelo_admin.evento == "selo_retirado"
    assert pelo_admin.chave_unica == f"selo_retirado:admin:{retirado.sistema.id}:{retirado.teste.id}|{ADMIN}"
    assert "foi retirado: o ADMIN retirou o selo." in pelo_admin.corpo["texto"]
    assert "o sistema vai mudar" not in json.dumps(pelo_admin.corpo)  # o texto livre do ADMIN fica na tela
    [caiu] = await avisos(dono, mudou.sistema.id)
    assert (caiu.evento, caiu.corpo["motivo"]) == ("selo_retirado", "endereco_mudou")
    assert (
        "foi retirado: o endereco do sistema mudou depois do teste; se a mudanca se desfizer, o selo volta sozinho,"
        " sem teste novo." in caiu.corpo["texto"]
    )
    assert await avisos(dono, vencido.sistema.id) == []
    # O endereco volta (o selo volta a valer) e muda de novo: o mesmo motivo nao avisa outra vez. Outro motivo, sim.
    await mudar_endereco(dono, mudou.sistema.id, endereco)
    async with dono.connect() as c:
        assert (await s.selo_vigente(c, mudou.sistema.id)).motivo == "vigente"
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    await mudar_endereco(dono, mudou.sistema.id, endereco.replace("/mcp", "/mcp2"))
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert len(await avisos(dono, mudou.sistema.id)) == 1
    await mudar_endereco(dono, mudou.sistema.id, endereco)
    async with dono.begin() as c:
        await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), {"s": mudou.sistema.id})
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert [x.corpo["motivo"] for x in await avisos(dono, mudou.sistema.id)] == ["endereco_mudou", "fora_de_ativo"]


async def test_retirada_ja_avisada_pelo_despachante_nao_sai_de_novo(
    banco_migrado: Parametros, rotinas: AsyncEngine
) -> None:
    """O sistema se contradiz pela consulta de verdade: o despachante retira o selo e avisa o ADMIN
    (``selo.consulta.avisar_retirada``). A rodada dos avisos das rotinas ve a retirada e nao a avisa outra vez."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="contradiz") as cn:
        op = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"daqui_a": 600})
        await a.mandar(cn)
        await a.voltar_no_tempo(cn, [op.id])
        assert (await cn.perguntador().consultar(op.id)).estado == "nao_recebida"
        assert (await a.selo_vigente(cn)).motivo == "retirado"
        assert len(await a.avisos_ao_admin(cn, "selo_retirado")) == 1
        for _ in range(2):
            await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
        [aviso] = await a.avisos_ao_admin(cn, "selo_retirado")
        assert aviso.corpo["motivo"] == "se_contradisse"


async def atrasar_retirada(dono: AsyncEngine, teste_id: uuid.UUID, intervalo: timedelta) -> None:
    """A retirada do teste ``intervalo`` para tras, como dono (desligando a guarda de "so de acrescentar")."""
    async with dono.begin() as c:
        await c.execute(text("ALTER TABLE selos_retirados DISABLE TRIGGER selos_retirados_sem_update_delete"))
        await c.execute(
            text("UPDATE selos_retirados SET retirado_em = retirado_em - CAST(:d AS interval) WHERE teste_id = :t"),
            {"d": intervalo, "t": teste_id},
        )
        await c.execute(text("ALTER TABLE selos_retirados ENABLE TRIGGER selos_retirados_sem_update_delete"))


async def retirar_pelo_admin(dono: AsyncEngine, cn: Cenario) -> None:
    assert cn.teste is not None
    async with dono.begin() as c:
        admin = await s.nova_pessoa(c, admin=True)
        await c.execute(
            text(
                "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                " VALUES (:s, :t, 'admin', 'o sistema vai mudar', 'x', :p)"
            ),
            {"s": cn.sistema.id, "t": cn.teste.id, "p": admin},
        )


async def test_nao_aprovado_e_retirada_de_mais_de_sete_dias_nao_avisam(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """``JANELA_DOS_AVISOS``: o teste nao aprovado e a retirada de ate 7 dias avisam (as rotinas paradas uns dias nao
    perdem o aviso); os de mais tempo, nao (nenhum aviso de meses atras)."""
    janela = selos.JANELA_DOS_AVISOS
    novos, velhos = await com_selo(dono, 2), await com_selo(dono, 2)
    for cn, quanto in ((novos, janela - timedelta(days=1)), (velhos, janela + timedelta(days=1))):
        assert cn.teste is not None
        await atrasar_teste(dono, cn.teste.id, quanto)
        await retirar_pelo_admin(dono, cn)
        await atrasar_retirada(dono, cn.teste.id, quanto)
        async with dono.begin() as c:
            nao = await s.gravar_teste(c, cn.sistema.id, nivel=2, aprovado=False)
        await atrasar_teste(dono, nao.id, quanto - timedelta(hours=1))
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert sorted(x.evento for x in await avisos(dono, novos.sistema.id)) == ["selo_nao_aprovado", "selo_retirado"]
    assert await avisos(dono, velhos.sistema.id) == []


async def test_aviso_a_vencer_so_de_selo_de_nivel_2_ou_3(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """O teste aprovado perto de vencer, mas com o selo no nivel 1 porque o sistema mudou (``selos_vigentes`` guarda o
    ``vale_ate`` dele), nao tem aviso de "vence em": so o da queda."""
    cn = await com_selo(dono, 2)
    assert cn.teste is not None
    await atrasar_teste(dono, cn.teste.id, await faltam(dono, cn.teste.id) - timedelta(days=13))
    await mudar_endereco(dono, cn.sistema.id, cn.sistema.endereco.replace("/mcp", "/mcp2"))
    async with dono.connect() as c:
        selo = await s.selo_vigente(c, cn.sistema.id)
    assert (selo.nivel, selo.motivo) == (1, "endereco_mudou")
    await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
    assert [(x.evento, x.corpo["motivo"]) for x in await avisos(dono, cn.sistema.id)] == [
        ("selo_retirado", "endereco_mudou")
    ]


async def test_tarefa_dos_avisos_usa_o_fuso_das_telas(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    """``tarefas_dos_selos`` passa ``SCREENS_TIMEZONE`` a ``avisar_selos_a_vencer``: a data do aviso e a do fuso das
    telas. O fuso da prova e um em que a data de ``vale_ate`` e outra que a de ``FUSO_PADRAO``."""
    cn = await com_selo(dono, 2)
    assert cn.teste is not None
    linha = await atrasar_teste(dono, cn.teste.id, await faltam(dono, cn.teste.id) - timedelta(days=13))

    def data(fuso: str) -> str:
        return str(linha.vale_ate.astimezone(ZoneInfo(fuso)).strftime("%d/%m/%Y"))

    [fuso] = [f for f in ("Pacific/Kiritimati", "Etc/GMT+12") if data(f) != data(selos.FUSO_PADRAO)][:1]
    p = Parametros(SCREENS_TIMEZONE=fuso)
    [_, tarefa] = selos.tarefas_dos_selos(rotinas, p, ConfigRotinas(emails_admin=(ADMIN,)), DespachanteFalso(dono))
    assert tarefa.nome == "avisar_selos"
    await tarefa.rodar()
    [aviso] = await avisos(dono, cn.sistema.id, "selo_a_vencer")
    assert f"vence em 13 dias ({data(fuso)})" in aviso.corpo["texto"]
    [extrato] = await extrato_do_sistema(dono, cn.sistema.id, "selo_a_vencer")
    assert f"vence em {data(fuso)};" in extrato.frase


async def test_nao_aprovado_pela_bateria_avisa_o_admin_uma_vez_so(
    banco_migrado: Parametros, rotinas: AsyncEngine
) -> None:
    """Defeito S2 do verificador da etapa 4: a bateria (``selo.bateria``) avisa o teste nao aprovado na transacao que
    o grava; a rodada dos avisos das rotinas ve o mesmo teste e usa a mesma ``chave_unica``
    (``selos.chave_do_nao_aprovado``). O ADMIN recebe um aviso so, com a conta de casos das rotinas
    (``casos_que_nao_aprovam``)."""
    async with bateria.cena(banco_migrado, nivel=3) as cn:
        assert await cn.corredor().uma_volta()
        [teste] = await cn.testes()
        assert teste.resultado == "reprovado"
        for _ in range(2):
            await selos.avisar_selos_a_vencer(rotinas, (ADMIN,))
        [aviso] = await avisos(cn.dono, cn.sistema.id, "selo_nao_aprovado")
    assert (aviso.chave_unica, aviso.destino) == (f"{selos.chave_do_nao_aprovado(teste.id)}|{ADMIN}", ADMIN)
    falhas = selos.casos_que_nao_aprovam(teste.casos, 3)
    assert falhas > 0
    assert f"nao foi aprovado pelo criterio do MCP-CLARO: {falhas} casos" in aviso.corpo["texto"]


# --- Resumo semanal --------------------------------------------------------------------------------------------


async def test_resumo_semanal_traz_as_contagens_dos_selos(dono: AsyncEngine, rotinas: AsyncEngine) -> None:
    agora = datetime(2032, 3, 10, 15, 0, tzinfo=UTC)
    semana = semana_anterior(agora)
    no_meio = semana.inicio + timedelta(days=2)
    aprovado, reprovado = await com_selo(dono, 3), await com_selo(dono, 2)
    async with dono.begin() as c:
        nao = await s.gravar_teste(c, reprovado.sistema.id, nivel=2, aprovado=False)
    assert aprovado.teste is not None
    for teste_id in (aprovado.teste.id, nao.id):
        async with dono.connect() as c:
            testado_em = (
                await c.execute(text("SELECT testado_em FROM testes_selo WHERE id = :t"), {"t": teste_id})
            ).scalar_one()
        await atrasar_teste(dono, teste_id, testado_em - no_meio)
    contagens = [
        ("selo_informacoes", {"origem": "consulta", "desfecho": "respondeu"}, 5),
        ("selo_informacoes", {"origem": "consulta", "desfecho": "sem_resposta"}, 3),
        ("selo_informacoes", {"origem": "consulta", "desfecho": "resposta_invalida"}, 1),
        ("selo_informacoes", {"origem": "consulta", "desfecho": "sem_chave"}, 2),
        ("selo_informacoes", {"origem": "consulta", "desfecho": "desconhecido_no_fim"}, 1),
        ("selo_informacoes", {"origem": "recibo", "desfecho": "respondeu"}, 7),
        ("selo_informacoes", {"origem": "aviso", "desfecho": "respondeu"}, 4),
        ("selo_respostas_invalidas", {"origem": "consulta", "motivo": "esquema"}, 1),
        ("selo_retiradas", {"motivo": "se_contradisse"}, 2),
        ("selo_retiradas", {"motivo": "admin"}, 1),  # a tela do ADMIN soma a dela
        ("selo_avisos_bloqueio", {"desfecho": "respondido"}, 3),
        ("selo_avisos_bloqueio", {"desfecho": "sem_resposta"}, 1),
        ("selo_avisos_bloqueio", {"desfecho": "derivado"}, 9),
        ("selo_numero_reaproveitado", {}, 2),
        ("selo_envios", {"desfecho": "sem_numero"}, 1),
        ("selo_envios", {"desfecho": "com_numero", "nivel": "2"}, 30),
        ("selo_sistema_contradito", {}, 1),
    ]
    async with dono.begin() as c:
        for nome, rotulos, valor in contagens:
            await c.execute(
                text("INSERT INTO medicoes.hora (hora, nome, rotulos, valor) VALUES (:h, :n, CAST(:r AS jsonb), :v)"),
                {"h": no_meio, "n": nome, "r": json.dumps(rotulos), "v": valor},
            )
        # Fora da semana: nao conta.
        await c.execute(
            text("INSERT INTO medicoes.hora (hora, nome, rotulos, valor) VALUES (:h, 'selo_retiradas', '{}', 50)"),
            {"h": semana.fim + timedelta(hours=2)},
        )
        agora_niveis = dict(
            (await c.execute(text("SELECT nivel, count(*) FROM selos_vigentes WHERE nivel >= 2 GROUP BY 1"))).all()
        )
    corpo = await resumo_semanal(rotinas, [ADMIN], agora=agora)
    assert corpo is not None
    n = corpo["contagens"]
    assert {k: v for k, v in n.items() if k.startswith("selo_")} == {
        "selo_testes_aprovados": 1,
        "selo_testes_nao_aprovados": 1,
        "selo_sistemas_nivel_2": agora_niveis.get(2, 0),
        "selo_sistemas_nivel_3": agora_niveis.get(3, 0),
        "selo_consultas": 11,
        "selo_consultas_respondidas": 5,
        "selo_consultas_sem_resposta": 4,
        "selo_consultas_nao_feitas": 2,
        "selo_desconhecido_no_fim": 1,
        "selo_recibos": 7,
        "selo_respostas_ao_aviso": 4,
        "selo_respostas_invalidas": 1,
        "selo_retiradas": 3,
        "selo_avisos_bloqueio_respondidos": 3,
        "selo_avisos_bloqueio_sem_resposta": 1,
        "selo_numero_reaproveitado": 2,
        "selo_envios_sem_numero": 1,
        "selo_sistema_contradito": 1,
    }
    assert "Selos, testes aprovados na semana: 1" in corpo["linhas"]
    assert "Consultas aos sistemas sobre acoes em duvida: 11" in corpo["linhas"]
    assert "Selos retirados: 3" in corpo["linhas"]
    texto = json.dumps(corpo)
    assert aprovado.sistema.nome not in texto
    assert str(aprovado.pessoa_id) not in texto
    async with dono.connect() as c:
        dados = (
            await c.execute(
                text("SELECT dados FROM extrato WHERE tipo = 'resumo_semanal' AND dados ->> 'semana' = :s"),
                {"s": semana.rotulo},
            )
        ).scalar_one()
    assert dados["n_selo_consultas"] == 11
