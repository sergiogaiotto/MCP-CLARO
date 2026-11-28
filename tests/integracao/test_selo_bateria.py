"""A bateria do selo do produto (``selo/alvo.py``, ``selo/casos.py`` e ``selo/bateria.py``; desenho 3 e 12.1 G3;
criticas A3, A4, B5, M8 e M11; decisao 7 do coordenador na etapa 3r).

Duas partes:

- a bateria contra a tabela de referencia do G1 (``tests/integracao/test_dubles_selo.py``), sem banco: o duble de
  nivel 2 sai aprovado no 2 e, num teste de nivel 3, nao aprovado exatamente em ``selo_nivel2.FALHA_NO_NIVEL_3``; o de
  nivel 3 sai aprovado no 3 (no modo ambiente, no de ferramentas de teste e com os esquemas do kit); cada modo do
  mentiroso nao aprova (``falhou`` ou ``nao_testado``) exatamente os casos de ``selo_mentiroso.CASOS_QUE_FALHAM``, e
  cada parte forcada (``selo_mentiroso.PARTES_FORCADAS``) e a unica que falha o caso. Os tres modos da critica A4
  (``alvo_diz_producao``, ``producao_diz_teste`` e ``ferramenta_de_teste_sem_marca``) param a bateria antes da
  escrita (ou na primeira resposta sem a marca): ``falhou`` e exatamente a tabela (o C-0.4), e o resto fica
  ``nao_testado`` com ``fora_do_alvo``, com zero escritas no alvo (uma so, a que veio sem a marca). Em todo modo, a
  producao do duble nao tem escrita aplicada (no modo ambiente, so a consulta de controle e o ``selo_alvo``, com a
  chave das declaracoes; no de ferramentas de teste, nenhuma acao real). O plano (critica B5) e o que a bateria chama;
- a tarefa do despachante (``selo.bateria.Corredor``), pelo banco, com o usuario ``mcp_claro_despachante``: aprovado
  (o duble de nivel 2 no nivel 2; o de nivel 3 no nivel 3, no modo ambiente com duas acoes e no de ferramentas de
  teste) e reprovado gravados na transacao que fecha o pedido, com a linha do extrato, o aviso ao ADMIN e os
  contadores ``selo_testes{resultado,nivel}`` e ``selo_testes_interrompidos{motivo}``; a chave de teste e a de leitura
  das declaracoes, nunca a de uma pessoa; o plano acima do teto, sem chave de teste, porta fechada ou cofre trancado:
  o teste nao comeca; duas copias rodam um pedido uma vez so; a partida marca ``rodando`` sem dono como
  ``interrompido``; uma queda no meio nao grava teste; o sistema que muda no meio, ou o alvo que vira producao (pelo
  codigo ou pelo banco), deixa o pedido ``interrompido`` com ``sistema_mudou``; declaracoes erradas de proposito
  (critica A4.5) nao comecam, barradas pela conferencia da declaracao ou, sem ela, pelo ``AlvoDoTeste`` (A4.2), e o
  duble conta zero chamadas de acao.

A espera pela chegada da chamada (C-2.6a e C-2.6b) e o evento do duble (a contagem dele, no processo), com teto: nada
depende do relogio da maquina. Todo endereco de duble sai de ``tests.apoio_selos.endereco_de_duble``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool
from starlette.applications import Starlette

from exemplos import selo_mentiroso, selo_nivel2, selo_nivel3
from mcp_claro.cofre import Cofre, CofreTrancado
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.ligacao.ponto import ClienteLocal
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.saida import travas
from mcp_claro.saida.despachante import ligacao_sem_sala
from mcp_claro.selo import bateria, contrato
from mcp_claro.selo import declaracao as declaracao_do_selo
from mcp_claro.selo.alvo import AlvoDoTeste
from mcp_claro.selo.bateria import Corredor, Rodada, plano, rodar_bateria
from mcp_claro.selo.casos import Chegada
from mcp_claro.selo.declaracao import validar
from tests import apoio_g3a as g
from tests import apoio_selos as s
from tests.apoio import servidor
from tests.apoio_terceiros import definicao, revisar_ferramenta
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_cofre import Bao, openbao

__all__ = ["openbao"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

CHAVE_DE_TESTE = "chave-teste-selo"
CHAVE_DAS_DECLARACOES = "chave-declaracoes-selo"
CHAVE_DA_PESSOA = "chave-ana"
PARES = 5
SEGUNDOS = 2
"""O ``segundos`` da declaracao (efeito adiado e escrita lenta)."""
INTERVALO = 0.6
"""C-2.6a: a segunda consulta, este tanto depois da primeira (mais que ``selo_mentiroso.ESPERA_DA_DESCONEXAO``)."""
DEPOIS_DA_CHEGADA = 0.2
PRAZO_DA_CHEGADA = 10.0
PRAZO_DO_CONTRATO = 1.0
"""Prazo de cada ferramenta do contrato: o duble esta na mesma maquina, e os mudos so terminam quando quem chamou
desiste."""
MODOS_A4 = frozenset({"alvo_diz_producao", "producao_diz_teste", "ferramenta_de_teste_sem_marca"})
"""Os modos da critica A4: sem a marca positiva, a bateria nao escreve (e para na primeira resposta sem ela)."""
MODOS_MUDOS = frozenset({"consulta_muda", "aviso_mudo"})
"""Os modos em que uma ferramenta do contrato nunca responde."""
GRUPOS = 6
FECHAR_A_SAIDA = "UPDATE estado_saida SET aberta = false, motivo = 'prova', mudou_por = 'teste', mudou_em = now()"
"""Os modos do mentiroso rodam em grupos, cada grupo ao mesmo tempo (a bateria espera quase sempre o relogio)."""


# --- O duble no ar -----------------------------------------------------------------------------------------------


@dataclass
class Duble:
    base: str
    producao: str
    teste: str | None
    app: Starlette

    @property
    def motor(self) -> Any:
        return self.app.state.motor

    def ambiente(self, alvo: str) -> Any:
        return self.motor.ambientes[alvo]

    def chamadas(self, alvo: str) -> dict[str, int]:
        return dict(self.ambiente(alvo).contagem.chamadas)

    def chamadas_de_acao(self, alvo: str) -> int:
        acoes = set(selo_nivel2.ACOES) | {a + selo_nivel2.SUFIXO_TESTE for a in selo_nivel2.ACOES}
        return sum(n for nome, n in self.chamadas(alvo).items() if nome in acoes)


@contextlib.asynccontextmanager
async def no_ar(
    fabrica: Callable[..., Starlette], *, ferramentas_de_teste: bool = False, **kw: Any
) -> AsyncIterator[Duble]:
    producao = s.caminho_unico("producao")
    teste = None if ferramentas_de_teste else s.caminho_unico("teste")
    app = fabrica(producao=producao, teste=teste, ferramentas_de_teste=ferramentas_de_teste, **kw)
    async with servidor(app) as base:
        yield Duble(
            base,
            s.endereco_de_duble(base, producao),
            None if teste is None else s.endereco_de_duble(base, teste),
            app,
        )


def chegada_do_duble(duble: Duble, alvo: str) -> Chegada:
    """A espera da chegada pelo evento do duble: a contagem da ferramenta no alvo sobe (com teto), e um pouco mais."""

    @contextlib.asynccontextmanager
    async def chegada(ferramenta: str, _lenta: float | None) -> AsyncIterator[None]:
        antes = duble.chamadas(alvo).get(ferramenta, 0)
        yield
        with anyio.fail_after(PRAZO_DA_CHEGADA):
            while True:  # a contagem e do duble, no processo: nao ha evento a esperar
                if duble.chamadas(alvo).get(ferramenta, 0) > antes:
                    break
                await anyio.sleep(0.01)
        await anyio.sleep(DEPOIS_DA_CHEGADA)

    return chegada


def declaracao_do_duble(duble: Duble, *, modo: str, acoes: Sequence[str] = selo_nivel2.ACOES) -> dict[str, Any]:
    return selo_nivel2.declaracao(
        endereco_teste=duble.teste if modo == "ambiente_de_teste" else None,
        modo=modo,  # type: ignore[arg-type]
        acoes=acoes,
        segundos=SEGUNDOS,
    )


@dataclass
class Rodou:
    rodada: Rodada
    ferramentas: dict[str, str]
    alvo: AlvoDoTeste


async def bateria_no_duble(
    duble: Duble, nivel: int, *, modo: str = "ambiente_de_teste", acoes: Sequence[str] = selo_nivel2.ACOES
) -> Rodou:
    """A bateria do produto contra o duble, sem banco: o alvo fechado com a ligacao do cliente fixado, as chaves do
    duble (a de teste no alvo, a das declaracoes na producao)."""
    modelo = validar(declaracao_do_duble(duble, modo=modo, acoes=acoes))
    alvo = AlvoDoTeste(
        modelo,
        producao=duble.producao,
        producoes=[duble.producao],
        nivel=nivel,
        ligacao=ClienteLocal(),
        segredo_teste=CHAVE_DE_TESTE,
        segredo_producao=CHAVE_DAS_DECLARACOES,
        teto=2000,
        prazo=5.0,
        prazo_do_contrato=PRAZO_DO_CONTRATO,
    )
    ferramentas = {nome: str(uuid.uuid4()) for nome in modelo.ferramentas}
    lado = "teste" if modo == "ambiente_de_teste" else "producao"
    rodada = await rodar_bateria(
        alvo,
        modelo,
        ferramentas=ferramentas,
        nivel=nivel,
        pares=PARES,
        chegada=chegada_do_duble(duble, lado),
        intervalo_do_corte=INTERVALO,
    )
    return Rodou(rodada, ferramentas, alvo)


def nao_aprovados(itens: Sequence[dict[str, Any]], nivel: int) -> set[str]:
    """Os casos que nao aprovam: algum item ``falhou``, ou, obrigatorio, sem o resultado que aprova."""
    saida = set()
    for i in itens:
        caso = contrato.CASOS_POR_NOME[i["caso"]]
        if i["resultado"] == "falhou" or (
            caso.obrigatorio and not contrato.resultado_aprova(caso, i["resultado"], nivel)
        ):
            saida.add(i["caso"])
    return saida


def falhas(itens: Sequence[dict[str, Any]]) -> set[str]:
    return {i["caso"] for i in itens if i["resultado"] == "falhou"}


def producao_sem_escrita(duble: Duble, *, de_teste: bool, acoes: Sequence[str] = selo_nivel2.ACOES) -> None:
    """Critica A4: no modo ambiente, a producao do duble so viu a consulta de controle e o ``selo_alvo``, com a chave
    das declaracoes, e nenhuma escrita foi aplicada; o alvo so viu a chave de teste. No de ferramentas de teste,
    nenhuma acao real foi chamada."""
    producao = duble.ambiente("producao")
    chamadas = set(producao.contagem.chamadas)
    if de_teste:
        permitidas = {a + selo_nivel2.SUFIXO_TESTE for a in acoes} | set(selo_nivel2.LEITURAS)
        assert not chamadas & set(selo_nivel2.ACOES), chamadas
        assert chamadas <= permitidas | contrato.NOMES_RESERVADOS, chamadas
        assert set(producao.pessoas) <= {"bateria", "declaracoes"}
    else:
        assert chamadas <= {contrato.FERRAMENTA_CONSULTAR, contrato.FERRAMENTA_ALVO}, chamadas
        assert producao.aplicadas == {}
        assert set(producao.pessoas) == {"declaracoes"}
        assert set(duble.ambiente("teste").pessoas) <= {"bateria"}


def conferir_partes(rodou: Rodou) -> None:
    """Todo caso julgado conferiu exatamente as partes da tabela (``selo_mentiroso.PARTES``)."""
    for (caso, _), partes in rodou.rodada.partes.items():
        assert set(partes) == set(selo_mentiroso.PARTES[caso]), (caso, sorted(partes))


# --- A bateria contra a tabela do G1 ----------------------------------------------------------------------------


async def test_duble_de_nivel_2_aprovado_no_2_e_no_3_nao_aprova_a_tabela() -> None:
    async with no_ar(selo_nivel2.criar) as duble:
        no_2 = await bateria_no_duble(duble, 2)
        no_3 = await bateria_no_duble(duble, 3)
        producao_sem_escrita(duble, de_teste=False)
    assert falhas(no_2.rodada.itens) == set()
    assert no_2.rodada.aprovado
    assert nao_aprovados(no_3.rodada.itens, 3) == set(selo_nivel2.FALHA_NO_NIVEL_3)
    assert falhas(no_3.rodada.itens) == set(selo_nivel2.FALHA_NO_NIVEL_3)
    assert not no_3.rodada.aprovado
    for rodou in (no_2, no_3):
        conferir_partes(rodou)
        assert contrato.problema_nos_casos(rodou.rodada.itens, list(rodou.ferramentas.values())) is None


@pytest.mark.parametrize(
    ("ferramentas_de_teste", "esquemas"),
    [(False, "contrato"), (True, "contrato"), (False, "kit")],
    ids=["ambiente", "ferramentas_de_teste", "esquemas_do_kit"],
)
async def test_duble_de_nivel_3_aprovado_no_3(ferramentas_de_teste: bool, esquemas: str) -> None:
    modo = "ferramentas_de_teste" if ferramentas_de_teste else "ambiente_de_teste"
    async with no_ar(selo_nivel3.criar, ferramentas_de_teste=ferramentas_de_teste, esquemas=esquemas) as duble:
        rodou = await bateria_no_duble(duble, 3, modo=modo)
        producao_sem_escrita(duble, de_teste=ferramentas_de_teste)
    assert nao_aprovados(rodou.rodada.itens, 3) == set()
    assert rodou.rodada.aprovado
    conferir_partes(rodou)
    por_caso = {(i["caso"], i["ferramenta"]): (i["resultado"], i["razao"]) for i in rodou.rodada.itens}
    lido = rodou.ferramentas["marcar_lido"]
    assert por_caso[("C-2.2b", lido)] == ("nao_se_aplica", "sem_segundo_efeito")  # critica M6
    assert por_caso[("C-2.7", lido)] == ("nao_observado", None)
    assert por_caso[("C-2.2d", rodou.ferramentas["enviar_lembrete"])] == ("nao_testado", "poucos_pares")  # M5


def _grupos() -> list[tuple[str, ...]]:
    modos = list(selo_mentiroso.MODOS)
    return [tuple(modos[i::GRUPOS]) for i in range(GRUPOS)]


async def _um_modo(modo: str) -> tuple[Rodou, Duble]:
    de_teste = modo in selo_mentiroso.MODOS_COM_FERRAMENTAS_DE_TESTE
    acoes = selo_mentiroso.ACOES_DO_MODO[modo]
    async with no_ar(selo_mentiroso.criar, modo=modo, ferramentas_de_teste=de_teste, acoes=acoes) as duble:
        rodou = await bateria_no_duble(
            duble, 3, modo="ferramentas_de_teste" if de_teste else "ambiente_de_teste", acoes=acoes
        )
        producao_sem_escrita(duble, de_teste=de_teste, acoes=acoes)
    return rodou, duble


@pytest.mark.parametrize("grupo", _grupos(), ids=lambda g: "+".join(g))
async def test_cada_modo_do_mentiroso_nao_aprova_os_casos_da_tabela(grupo: tuple[str, ...]) -> None:
    """Decisao 7 da etapa 3r: ``CASOS_QUE_FALHAM`` quer dizer "nao aprovado" (``falhou`` ou ``nao_testado``). Cada
    parte forcada pelo modo e a unica que falha o caso, em toda ferramenta em que ele falha."""
    resultados: dict[str, tuple[Rodou, Duble]] = {}

    async def rodar(modo: str) -> None:
        resultados[modo] = await _um_modo(modo)

    async with anyio.create_task_group() as tg:
        for modo in grupo:
            tg.start_soon(rodar, modo)
    for modo in grupo:
        rodou, duble = resultados[modo]
        itens = rodou.rodada.itens
        esperado = set(selo_mentiroso.CASOS_QUE_FALHAM[modo])
        conferir_partes(rodou)
        assert contrato.problema_nos_casos(itens, list(rodou.ferramentas.values())) is None, modo
        assert rodou.rodada.aprovado == (not esperado), modo
        if modo in MODOS_A4:
            # A marca positiva vale sobre a tabela: sem ela a bateria nao escreve, e o resto fica sem teste.
            assert falhas(itens) == esperado, modo
            resto = [i for i in itens if i["caso"] not in esperado and i["resultado"] != "cumpriu"]
            assert resto, modo
            assert {(i["resultado"], i["razao"]) for i in resto} == {("nao_testado", "fora_do_alvo")}, modo
            assert rodou.rodada.parou == "fora_do_alvo", modo
            lado = "producao" if modo == "ferramenta_de_teste_sem_marca" else "teste"
            assert duble.chamadas_de_acao(lado) == (1 if modo == "ferramenta_de_teste_sem_marca" else 0), modo
            continue
        assert nao_aprovados(itens, 3) == esperado, (modo, sorted(nao_aprovados(itens, 3)))
        if modo in MODOS_MUDOS:
            # Decisao 7: sem resposta do sistema, nao_testado com sem_resposta, nunca falhou (nem nas partes que
            # dependiam da resposta: a chamada depois da consulta ou do aviso que nao voltou).
            assert falhas(itens) == set(), modo
            sem_aprovar = [i for i in itens if i["caso"] in esperado and i["resultado"] != "cumpriu"]
            assert {i["razao"] for i in sem_aprovar} == {"sem_resposta"}, modo
        for (caso, parte), modos in selo_mentiroso.PARTES_FORCADAS.items():
            if modo not in modos:
                continue
            por_ferramenta = {
                f: falharam
                for (c, f), partes in rodou.rodada.partes.items()
                if c == caso and (falharam := {p for p, ok in partes.items() if ok is False})
            }
            assert por_ferramenta, (modo, caso, parte)
            assert all(ps == {parte} for ps in por_ferramenta.values()), (modo, caso, parte, por_ferramenta)


@pytest.mark.parametrize(
    ("fabrica", "nivel", "modo"),
    [
        (selo_nivel2.criar, 2, "ambiente_de_teste"),
        (selo_nivel3.criar, 3, "ambiente_de_teste"),
        (selo_nivel3.criar, 3, "ferramentas_de_teste"),
    ],
    ids=["nivel_2", "nivel_3", "ferramentas_de_teste"],
)
async def test_o_plano_e_o_que_a_bateria_chama(fabrica: Callable[..., Starlette], nivel: int, modo: str) -> None:
    """Critica B5: o plano pela declaracao (com as dicas de idempotencia que o alvo lista) e exatamente o que a bateria
    chama e escreve; sem as dicas (a tela, antes do teste), e o maximo."""
    async with no_ar(fabrica, ferramentas_de_teste=modo == "ferramentas_de_teste") as duble:
        rodou = await bateria_no_duble(duble, nivel, modo=modo)
    modelo = validar(declaracao_do_duble(duble, modo=modo))
    previsto = plano(modelo, nivel, pares=PARES, idempotentes={"marcar_lido"})
    assert (rodou.rodada.chamadas, rodou.rodada.escritas) == (previsto.chamadas, previsto.escritas)
    maximo = plano(modelo, nivel, pares=PARES)
    assert maximo.chamadas >= previsto.chamadas
    assert maximo.escritas >= previsto.escritas
    assert rodou.rodada.chamadas >= contrato.contagem_minima_de_chamadas(rodou.rodada.itens)


async def test_teto_de_chamadas_para_a_rodada() -> None:
    """``BADGE_TEST_MAX_CALLS`` na rodada: no teto, nada mais sai; o que falta fica ``nao_testado`` com ``teto`` (e o
    teste nao aprova)."""
    async with no_ar(selo_nivel3.criar) as duble:
        modelo = validar(declaracao_do_duble(duble, modo="ambiente_de_teste"))
        alvo = AlvoDoTeste(
            modelo,
            producao=duble.producao,
            producoes=[duble.producao],
            nivel=3,
            ligacao=ClienteLocal(),
            segredo_teste=CHAVE_DE_TESTE,
            segredo_producao=CHAVE_DAS_DECLARACOES,
            teto=30,
            prazo_do_contrato=PRAZO_DO_CONTRATO,
        )
        ferramentas = {nome: str(uuid.uuid4()) for nome in modelo.ferramentas}
        rodada = await rodar_bateria(
            alvo, modelo, ferramentas=ferramentas, nivel=3, pares=PARES, chegada=chegada_do_duble(duble, "teste")
        )
        recebidas = sum(duble.chamadas("teste").values()) + sum(duble.chamadas("producao").values())
    assert rodada.parou == "teto"
    assert rodada.chamadas == 30
    assert recebidas <= 30  # as listagens nao entram na contagem do duble
    assert not rodada.aprovado
    assert {i["razao"] for i in rodada.itens if i["tentativas"] == 0 and i["resultado"] == "nao_testado"} >= {"teto"}
    assert contrato.problema_nos_casos(rodada.itens, list(ferramentas.values())) is None


# --- A tarefa do despachante, pelo banco --------------------------------------------------------------------------


@dataclass
class Cena:
    dono: AsyncEngine
    desp: AsyncEngine
    cifra: CifraFalsa
    duble: Duble
    sistema: s.SistemaDoSelo
    admin_id: uuid.UUID
    declaracao_id: uuid.UUID
    pedido_id: uuid.UUID | None

    def corredor(self, **kw: Any) -> Corredor:
        opcoes: dict[str, Any] = {
            "max_chamadas": 2000,
            "max_segundos": 120.0,
            "pares": 2,
            "prazo_da_escrita": 5.0,
            "prazo_do_contrato": PRAZO_DO_CONTRATO,
            "ambiente": "teste",
            "emails_admin": ("admin@exemplo.teste",),
            "intervalo": 0.2,
            "chegada": chegada_do_duble(self.duble, "teste"),
            "intervalo_do_corte": INTERVALO,
        }
        return Corredor(self.desp, self.cifra, ligacao_sem_sala, **(opcoes | kw))

    async def pedido(self) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text("SELECT estado, motivo FROM pedidos_teste_selo WHERE id = :p"), {"p": self.pedido_id}
                )
            ).one()

    async def testes(self) -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT id, resultado, nivel_pedido, nivel_obtido, casos, chamadas, nao_testado,"
                            " iniciado_em, testado_em, vale_ate, sistema_endereco FROM testes_selo WHERE pedido_id = :p"
                        ),
                        {"p": self.pedido_id},
                    )
                ).all()
            )

    async def extrato(self) -> list[Any]:
        return await g.extrato(self.dono, self.sistema.id)


ACOES_DA_CENA = ("enviar_lembrete",)
LEITURAS_DA_CENA = ("listar_lembretes",)


async def _sem_pedidos_de_outros(dono: AsyncEngine) -> None:
    """Os pedidos de teste sao uns so no banco da sessao: o que outro teste deixou esperando ou rodando nao entra na
    tarefa desta prova. Pelo dono, so para o arranjo."""
    async with dono.begin() as c:
        await c.execute(
            text(
                "UPDATE pedidos_teste_selo SET estado = 'interrompido', motivo = 'erro_interno'"
                " WHERE estado IN ('pedido', 'rodando')"
            )
        )


@contextlib.asynccontextmanager
async def cena(
    banco: Parametros,
    fabrica: Callable[..., Starlette] = selo_nivel2.criar,
    *,
    nivel: int = 2,
    dados: dict[str, Any] | None = None,
    chave_de_teste: bool = True,
    pedir: bool = True,
    acoes: Sequence[str] = ("enviar_lembrete",),
    leituras: Sequence[str] = ("listar_lembretes",),
    **do_duble: Any,
) -> AsyncIterator[Cena]:
    """Um duble no ar e, no banco, o sistema proprio na frente da producao dele, a declaracao com o alvo de teste do
    duble, as chaves (de teste, das declaracoes e a de uma pessoa, que a bateria nunca usa) e o pedido de teste."""
    dono = criar_engine(banco, pool_size=3, max_overflow=3)
    desp = g.engine_do_papel(banco, "mcp_claro_despachante")
    cifra = CifraFalsa()
    try:
        await g.abrir_a_saida(dono)
        await _sem_pedidos_de_outros(dono)
        async with no_ar(fabrica, **do_duble) as duble:
            async with dono.begin() as c:
                sistema = await s.proprio_com_acoes(
                    c, acoes=acoes, leituras=leituras, endereco=duble.producao, nivel_do_contrato=3
                )
                await c.execute(text("UPDATE sistemas SET sondagem = NULL WHERE id = :s"), {"s": sistema.id})
                admin_id = await s.nova_pessoa(c, admin=True)
                if dados is None:
                    dados = declaracao_do_duble(duble, modo="ambiente_de_teste", acoes=acoes)
                declaracao_id = await s.salvar_declaracao(c, sistema.id, admin_id, dados)
                if chave_de_teste:
                    await chaves.guardar(
                        c, cifra, sistema_id=sistema.id, segredo=CHAVE_DE_TESTE, tipo=contrato.TIPO_CHAVE_TESTE
                    )
                await chaves.guardar(c, cifra, sistema_id=sistema.id, segredo=CHAVE_DAS_DECLARACOES, tipo="declaracoes")
                pessoa_id = await s.nova_pessoa(c, teste=True)
                await chaves.guardar(c, cifra, sistema_id=sistema.id, segredo=CHAVE_DA_PESSOA, pessoa_id=pessoa_id)
                pedido_id = (
                    await s.pedir_teste(c, sistema.id, admin_id, declaracao_id, nivel=nivel, rodando=False)
                    if pedir
                    else None
                )
            yield Cena(dono, desp, cifra, duble, sistema, admin_id, declaracao_id, pedido_id)
    finally:
        await _sem_pedidos_de_outros(dono)
        await g.abrir_a_saida(dono)
        await dono.dispose()
        await desp.dispose()


def contado(nome: str, **rotulos: str) -> int:
    """Quanto o contador ``nome`` com ``rotulos`` somou ate agora no processo (sem tirar nada do que vai ao banco)."""
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    chave = json.dumps(rotulos, sort_keys=True, ensure_ascii=False)
    return sum(n for (_, nome_medido, r), n in medidas.items() if nome_medido == nome and r == chave)


def _texto(linhas: Sequence[Any]) -> str:
    return json.dumps([[str(x) for x in linha] for linha in linhas], ensure_ascii=False)


async def _contagens(dono: AsyncEngine) -> list[int]:
    contagens = []
    async with dono.connect() as c:
        for tabela in ("operacoes", "fila_saida", "fichas", "envios_selo", "informacoes_sistema"):
            contagens.append(int((await c.execute(text(f"SELECT count(*) FROM {tabela}"))).scalar_one()))  # noqa: S608
    return contagens


async def test_corredor_aprova_e_grava_o_teste_com_a_chave_de_teste(banco_migrado: Parametros) -> None:
    """O teste aprovado, gravado na transacao que fecha o pedido, com a chave de teste. A bateria roda com a trava da
    porta de saida (``DESPACHANTE_UNICO``) nas maos de outra conexao: ela nunca a segura. E nunca toca ``operacoes``,
    ``fila_saida``, ``fichas`` nem o que a porta grava (desenho 3.2, regra 6)."""
    async with cena(banco_migrado) as cn:
        antes = await _contagens(cn.dono)
        aprovados = contado("selo_testes", resultado="aprovado", nivel="2")
        corredor = cn.corredor()
        async with cn.desp.connect() as porta:
            assert (
                await porta.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": travas.DESPACHANTE_UNICO})
            ).scalar_one()
            await porta.commit()
            assert await corredor.uma_volta()
            await porta.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": travas.DESPACHANTE_UNICO})
            await porta.commit()
        assert await _contagens(cn.dono) == antes
        assert contado("selo_testes", resultado="aprovado", nivel="2") == aprovados + 1
        rodada = corredor.ultima
        assert rodada is not None
        assert rodada.aprovado
        assert (await cn.pedido()).estado == "terminado"
        [teste] = await cn.testes()
        assert (teste.resultado, teste.nivel_pedido, teste.nivel_obtido) == ("aprovado", 2, 2)
        assert teste.casos == rodada.itens
        assert teste.chamadas == rodada.chamadas
        assert teste.nao_testado == contrato.nao_testado_gravado()
        assert teste.sistema_endereco == cn.sistema.endereco
        assert teste.iniciado_em <= teste.testado_em < teste.vale_ate
        async with cn.dono.connect() as c:
            vigente = await s.selo_vigente(c, cn.sistema.id)
        assert (vigente.nivel, vigente.motivo) == (2, "vigente")
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_testado"]
        assert "aprovado pelo criterio do MCP-CLARO" in linha.frase
        # A chave de teste no alvo e a das declaracoes na producao; a da pessoa nunca.
        assert set(cn.duble.ambiente("teste").pessoas) == {"bateria"}
        assert set(cn.duble.ambiente("producao").pessoas) == {"declaracoes"}
        producao_sem_escrita(cn.duble, de_teste=False)
        # Registro sem conteudo: nada dos argumentos nem do sistema nos casos gravados.
        gravado = json.dumps(teste.casos)
        for pedaco in ("caixa-de-teste", "teste do selo", "lembrete-", selo_nivel2.PARA):
            assert pedaco not in gravado
        assert set().union(*(set(i) for i in teste.casos)) == contrato.CAMPOS_DO_ITEM


async def test_corredor_reprova_no_nivel_3_e_avisa_o_admin(banco_migrado: Parametros) -> None:
    async with cena(banco_migrado, nivel=3) as cn:
        reprovados = contado("selo_testes", resultado="reprovado", nivel="3")
        corredor = cn.corredor()
        assert await corredor.uma_volta()
        assert contado("selo_testes", resultado="reprovado", nivel="3") == reprovados + 1
        [teste] = await cn.testes()
        assert (teste.resultado, teste.nivel_obtido) == ("reprovado", 1)
        # So enviar_lembrete, que nao declara varios itens: o C-3.3 nao se aplica.
        esperado = set(selo_nivel2.FALHA_NO_NIVEL_3) - {"C-3.3"}
        assert falhas(teste.casos) == esperado
        assert nao_aprovados(teste.casos, 3) == esperado
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_nao_aprovado"]
        assert f"{bateria.casos_que_reprovam(teste.casos, 3)} casos falharam" in linha.frase
        async with cn.dono.connect() as c:
            avisos = (
                await c.execute(
                    text(
                        "SELECT evento FROM avisos WHERE evento = 'selo_nao_aprovado' AND corpo ->> 'sistema_id' = :s"
                    ),
                    {"s": str(cn.sistema.id)},
                )
            ).all()
            vigente = await s.selo_vigente(c, cn.sistema.id)
        assert len(avisos) == 1
        assert (vigente.nivel, vigente.motivo) == (1, "reprovado")


@pytest.mark.parametrize(
    ("modo", "acoes", "leituras"),
    [
        ("ambiente_de_teste", ("enviar_lembrete", "marcar_lido"), ("listar_lembretes", "listar_lidos")),
        ("ferramentas_de_teste", ("enviar_lembrete",), ("listar_lembretes",)),
    ],
    ids=["ambiente_com_duas_acoes", "ferramentas_de_teste"],
)
async def test_corredor_aprova_o_duble_de_nivel_3_no_nivel_3(
    banco_migrado: Parametros, modo: str, acoes: tuple[str, ...], leituras: tuple[str, ...]
) -> None:
    """O caminho inteiro do nivel 3 pelo banco: o duble de nivel 3 sai aprovado no nivel 3 (o banco confere os casos
    obrigatorios de cada ferramenta, ``testes_selo_nasce``), com o selo vigente 3, no modo ambiente com duas acoes (uma
    idempotente) e no de ferramentas de teste (com a chave de teste na producao, so nas ferramentas de teste)."""
    de_teste = modo == "ferramentas_de_teste"
    dados = (
        selo_nivel2.declaracao(endereco_teste=None, modo="ferramentas_de_teste", acoes=acoes, segundos=SEGUNDOS)
        if de_teste
        else None
    )
    async with cena(
        banco_migrado,
        selo_nivel3.criar,
        nivel=3,
        dados=dados,
        acoes=acoes,
        leituras=leituras,
        ferramentas_de_teste=de_teste,
    ) as cn:
        aprovados = contado("selo_testes", resultado="aprovado", nivel="3")
        corredor = cn.corredor(chegada=chegada_do_duble(cn.duble, "producao" if de_teste else "teste"))
        assert await corredor.uma_volta()
        assert corredor.ultima is not None
        assert nao_aprovados(corredor.ultima.itens, 3) == set()
        assert (await cn.pedido()).estado == "terminado"
        [teste] = await cn.testes()
        assert (teste.resultado, teste.nivel_pedido, teste.nivel_obtido) == ("aprovado", 3, 3)
        assert contado("selo_testes", resultado="aprovado", nivel="3") == aprovados + 1
        async with cn.dono.connect() as c:
            vigente = await s.selo_vigente(c, cn.sistema.id)
        assert (vigente.nivel, vigente.motivo) == (3, "vigente")
        producao_sem_escrita(cn.duble, de_teste=de_teste, acoes=acoes)


async def test_plano_acima_do_teto_nao_comeca(banco_migrado: Parametros) -> None:
    """Critica B5: o plano passa do teto, e o pedido fica interrompido antes de qualquer chamada."""
    async with cena(banco_migrado) as cn:
        # Com 20 pares (40 escritas so no C-2.2d), o plano passa das 50 chamadas, o menor teto que a partida aceita.
        assert await cn.corredor(max_chamadas=50, pares=20).uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "teto_de_chamadas")
        assert await cn.testes() == []
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        modelo = validar(declaracao_do_duble(cn.duble, modo="ambiente_de_teste", acoes=ACOES_DA_CENA))
        assert f"precisa de {plano(modelo, 2, pares=20).chamadas} chamadas e o teto e 50" in linha.frase
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})


async def test_sem_chave_de_teste_nao_comeca_e_nao_usa_outra_chave(banco_migrado: Parametros) -> None:
    """Critica M11: so a chave ``teste_selo``. Sem ela, o teste nao comeca, mesmo com a chave de uma pessoa e a das
    declaracoes guardadas."""
    async with cena(banco_migrado, chave_de_teste=False) as cn:
        interrompidos = contado("selo_testes_interrompidos", motivo="sem_chave_de_teste")
        testados = sum(contado("selo_testes", resultado=r, nivel="2") for r in ("aprovado", "reprovado"))
        assert await cn.corredor().uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "sem_chave_de_teste")
        assert contado("selo_testes_interrompidos", motivo="sem_chave_de_teste") == interrompidos + 1
        assert sum(contado("selo_testes", resultado=r, nivel="2") for r in ("aprovado", "reprovado")) == testados
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert "falta a chave de teste do selo" in linha.frase


async def test_porta_fechada_nao_comeca_e_o_pedido_vence(banco_migrado: Parametros) -> None:
    async with cena(banco_migrado) as cn:
        async with cn.dono.begin() as c:
            await c.execute(text(FECHAR_A_SAIDA))
        corredor = cn.corredor()
        assert not await corredor.uma_volta()
        assert tuple(await cn.pedido()) == ("pedido", None)
        assert cn.duble.chamadas("teste") == {}
        # Passado BADGE_TEST_MAX_SECONDS desde o pedido (aqui, zero), o pedido que espera fica interrompido.
        assert await cn.corredor(max_segundos=0.0).expirar() == 1
        assert tuple(await cn.pedido()) == ("interrompido", "prazo")


async def test_cofre_trancado_nao_comeca(banco_migrado: Parametros) -> None:
    class Trancado:
        async def saude(self) -> Any:
            raise CofreTrancado("trancado")

    async with cena(banco_migrado) as cn:
        corredor = cn.corredor()
        corredor.cofre = Trancado()
        assert not await corredor.uma_volta()
        assert tuple(await cn.pedido()) == ("pedido", None)
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})


async def test_duas_copias_rodam_o_pedido_uma_vez(banco_migrado: Parametros) -> None:
    """Critica M8: duas copias do despachante na mesma volta; uma roda o pedido, a outra nao o acha nem o marca
    interrompido (a trava do pedido esta com a primeira)."""
    async with cena(banco_migrado) as cn:
        espera = chegada_do_duble(cn.duble, "teste")
        marcados_no_meio: list[int] = []

        @contextlib.asynccontextmanager
        async def e_confere(ferramenta: str, lenta: float | None) -> AsyncIterator[None]:
            async with espera(ferramenta, lenta):
                yield
            if not marcados_no_meio:  # no meio do teste, a outra copia olha os pedidos rodando
                marcados_no_meio.append(await cn.corredor().marcar_interrompidos())

        uma, outra = cn.corredor(chegada=e_confere), cn.corredor(chegada=e_confere)
        rodaram = await asyncio.gather(uma.uma_volta(), outra.uma_volta())
        assert sorted(rodaram) == [False, True]
        assert marcados_no_meio == [0]
        assert (await cn.pedido()).estado == "terminado"
        assert len(await cn.testes()) == 1
        assert cn.duble.chamadas("teste")[contrato.FERRAMENTA_ALVO] == 1
        assert [x.tipo for x in await cn.extrato() if x.tipo.startswith("selo_")] == ["selo_testado"]


async def test_a_tarefa_roda_o_pedido_sozinha(banco_migrado: Parametros) -> None:
    """A tarefa (``Corredor.rodar``) acha o pedido e o roda, sem ninguem chamar a volta de fora."""
    async with cena(banco_migrado) as cn:
        tarefa = asyncio.create_task(cn.corredor(intervalo=0.1).rodar())
        try:
            with anyio.fail_after(60):
                while True:  # o estado do pedido so se le no banco: nao ha evento a esperar
                    if (await cn.pedido()).estado == "terminado":
                        break
                    await anyio.sleep(0.1)
        finally:
            tarefa.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarefa
        assert len(await cn.testes()) == 1


async def test_partida_marca_rodando_sem_dono_como_interrompido(banco_migrado: Parametros) -> None:
    async with cena(banco_migrado, pedir=False) as cn:
        async with cn.dono.begin() as c:
            orfao = await s.pedir_teste(c, cn.sistema.id, cn.admin_id, cn.declaracao_id, nivel=2, rodando=True)
        cn.pedido_id = orfao
        # Com a trava ocupada (outra copia rodando), fica como esta.
        async with cn.desp.connect() as outra:
            assert (
                await outra.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:t), hashtext(:p))"),
                    {"t": bateria.TRAVA_DA_BATERIA, "p": str(orfao)},
                )
            ).scalar_one()
            await outra.commit()
            assert await cn.corredor().marcar_interrompidos() == 0
            assert (await cn.pedido()).estado == "rodando"
            await outra.execute(
                text("SELECT pg_advisory_unlock(hashtext(:t), hashtext(:p))"),
                {"t": bateria.TRAVA_DA_BATERIA, "p": str(orfao)},
            )
            await outra.commit()
        assert await cn.corredor().marcar_interrompidos() == 1
        assert tuple(await cn.pedido()) == ("interrompido", "despachante_caiu")
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert "o despachante parou no meio do teste; nada mudou no selo" in linha.frase
        assert linha.dados["motivo"] == "despachante_caiu"


async def test_a_partida_do_app_marca_o_rodando_sem_dono(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partida do app do despachante (``despachante/app.py``) marca o pedido ``rodando`` sem dono antes de subir a
    tarefa da bateria, e monta a tarefa com os parametros."""
    from mcp_claro.despachante.app import criar_app_despachante

    async with cena(banco_migrado, pedir=False) as cn:
        async with cn.dono.begin() as c:
            cn.pedido_id = await s.pedir_teste(c, cn.sistema.id, cn.admin_id, cn.declaracao_id, nivel=2, rodando=True)
        p = banco_migrado.model_copy(
            update={"APP_ENV": "teste", "BADGE_TEST_MAX_CALLS": 77, "BADGE_TEST_MAX_SECONDS": 66, "BADGE_TEST_PAIRS": 3}
        )
        app = criar_app_despachante(p, cn.desp, cn.cifra)
        corredor = app.state.corredor
        assert (corredor.max_chamadas, corredor.max_segundos, corredor.pares) == (77, 66.0, 3)
        subiu = asyncio.Event()

        async def tarefa_que_so_anota() -> None:
            subiu.set()  # a volta da tarefa tambem marcaria: aqui so a partida marca
            await asyncio.Event().wait()

        monkeypatch.setattr(corredor, "rodar", tarefa_que_so_anota)
        async with app.router.lifespan_context(app):
            # Um grupo de conexoes proprio, sem pool: nunca espera pelas conexoes da porta ou da leitura.
            assert corredor.engine is not cn.desp
            assert isinstance(corredor.engine.sync_engine.pool, NullPool)
            assert tuple(await cn.pedido()) == ("interrompido", "despachante_caiu")
            await asyncio.wait_for(subiu.wait(), timeout=10)
            assert any(t.get_name() == "bateria-do-selo" for t in asyncio.all_tasks())


async def test_o_app_da_a_bateria_o_cofre_de_verdade(banco_migrado: Parametros, openbao: Bao) -> None:
    """Sem ``cifra``, o app do despachante entra no cofre na partida e da a bateria o mesmo cliente do cofre que a
    porta usa: com o cofre trancado, o teste nao comeca (``test_cofre_trancado_nao_comeca``)."""
    from mcp_claro.despachante.app import criar_app_despachante

    with contextlib.suppress(AssertionError):  # a chave dos terceiros (derived) ja pode existir na sessao
        openbao.api(
            "POST",
            f"transit/keys/{banco_migrado.VAULT_TRANSIT_KEY_THIRD_PARTY}",
            {"type": "aes256-gcm96", "derived": True},
        )
    vault = openbao.parametros("despachante")
    p = banco_migrado.model_copy(
        update={
            "VAULT_ADDR": vault.VAULT_ADDR,
            "VAULT_ROLE_ID_FILE": vault.VAULT_ROLE_ID_FILE,
            "VAULT_SECRET_ID_FILE": vault.VAULT_SECRET_ID_FILE,
            "OUTBOX_SWEEP_SECONDS": 60,
        }
    )
    desp = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    try:
        app = criar_app_despachante(p, desp, None)
        assert app.state.corredor.cofre is None
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.corredor.cofre, Cofre)
            assert app.state.corredor.cofre is app.state.porta.cofre
    finally:
        await desp.dispose()


async def test_queda_no_meio_nao_grava_teste(banco_migrado: Parametros) -> None:
    """Uma queda no meio (a tarefa cancelada): a trava do pedido vai junto com a conexao, e a volta seguinte, de
    qualquer copia, marca o pedido ``interrompido``, sem ``testes_selo``."""
    async with cena(banco_migrado) as cn:
        corredor = cn.corredor()
        tarefa = asyncio.create_task(corredor.rodar_um())
        with anyio.fail_after(30):
            while True:  # a primeira escrita chegou ao duble (a contagem dele, no processo)
                if cn.duble.chamadas_de_acao("teste"):
                    break
                await anyio.sleep(0.01)
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
        assert (await cn.pedido()).estado == "rodando"
        assert await cn.corredor().marcar_interrompidos() == 1
        assert tuple(await cn.pedido()) == ("interrompido", "despachante_caiu")
        assert await cn.testes() == []


@pytest.mark.parametrize("mudanca", ["declaracao", "revisao"])
async def test_sistema_que_muda_no_meio_fica_interrompido(banco_migrado: Parametros, mudanca: str) -> None:
    """Critica M8: uma declaracao nova salva durante a bateria vence a do pedido, e a acao revisada de novo muda as
    ferramentas do retrato do comeco; o banco recusa o fim, e o pedido fica ``interrompido`` com ``sistema_mudou``,
    sem teste gravado."""
    async with cena(banco_migrado) as cn:
        espera = chegada_do_duble(cn.duble, "teste")
        mudou = False

        @contextlib.asynccontextmanager
        async def e_muda(ferramenta: str, lenta: float | None) -> AsyncIterator[None]:
            nonlocal mudou
            async with espera(ferramenta, lenta):
                yield
            if not mudou:
                mudou = True
                async with cn.dono.begin() as c:
                    if mudanca == "declaracao":
                        await s.salvar_declaracao(
                            c,
                            cn.sistema.id,
                            cn.admin_id,
                            declaracao_do_duble(cn.duble, modo="ambiente_de_teste", acoes=ACOES_DA_CENA),
                        )
                    else:
                        await revisar_ferramenta(
                            c,
                            cn.sistema.id,
                            definicao("enviar_lembrete", esquema={"type": "object", "properties": {}}),
                            classe="muda",
                        )

        assert await cn.corredor(chegada=e_muda).uma_volta()
        assert mudou
        assert tuple(await cn.pedido()) == ("interrompido", "sistema_mudou")
        assert await cn.testes() == []
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert "o sistema mudou durante o teste" in linha.frase


async def test_porta_que_fecha_no_meio_para_o_teste(banco_migrado: Parametros) -> None:
    """A porta de saida conferida antes de cada ferramenta: fechada no meio, nada mais sai, e o pedido fica
    ``interrompido`` com ``porta_fechada``, sem teste gravado."""
    async with cena(
        banco_migrado, acoes=("enviar_lembrete", "marcar_lido"), leituras=("listar_lembretes", "listar_lidos")
    ) as cn:
        espera = chegada_do_duble(cn.duble, "teste")

        @contextlib.asynccontextmanager
        async def e_fecha(ferramenta: str, lenta: float | None) -> AsyncIterator[None]:
            async with espera(ferramenta, lenta):
                yield
            async with cn.dono.begin() as c:
                await c.execute(text(FECHAR_A_SAIDA))

        assert await cn.corredor(chegada=e_fecha).uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "porta_fechada")
        assert await cn.testes() == []
        # A ordem das ferramentas e a da declaracao guardada: so a primeira chegou ao duble.
        chamadas = cn.duble.chamadas("teste")
        assert sorted(chamadas.get(a, 0) > 0 for a in ("enviar_lembrete", "marcar_lido")) == [False, True]


async def test_teste_que_passa_do_tempo_fica_interrompido(banco_migrado: Parametros) -> None:
    """``BADGE_TEST_MAX_SECONDS`` na rodada: o teste de nivel 3 leva mais que o teto (o C-3.6b espera os segundos
    declarados), e o pedido fica ``interrompido`` com ``prazo``, sem teste gravado."""
    async with cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        assert await cn.corredor(max_segundos=SEGUNDOS).rodar_um()
        assert tuple(await cn.pedido()) == ("interrompido", "prazo")
        assert await cn.testes() == []


async def test_declaracao_nova_antes_da_vez_nao_comeca(banco_migrado: Parametros) -> None:
    """Pedido do G0r: ``rodando`` so com a declaracao mais recente do sistema. Uma declaracao salva depois do pedido
    (e antes da vez do despachante) vence: o banco recusa ``rodando``, e o pedido fica ``interrompido`` com
    ``sistema_mudou``, sem nenhuma chamada."""
    async with cena(banco_migrado) as cn:
        async with cn.dono.begin() as c:
            await s.salvar_declaracao(
                c,
                cn.sistema.id,
                cn.admin_id,
                declaracao_do_duble(cn.duble, modo="ambiente_de_teste", acoes=ACOES_DA_CENA),
            )
        assert await cn.corredor().uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "sistema_mudou")
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert bateria.MOTIVOS_LEGIVEIS["sistema_mudou"] in linha.frase
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})


ALVOS_ERRADOS = {
    "maiusculas": lambda alvo: alvo.upper().replace("HTTP://", "http://"),
    "barra_no_fim": lambda alvo: alvo + "/",
    "porta_com_zero": lambda alvo: alvo.replace("127.0.0.1:", "127.0.0.1:0"),
    "outro_nome": lambda alvo: alvo.replace("127.0.0.1", "localhost"),
}


@pytest.mark.parametrize("forma", sorted(ALVOS_ERRADOS))
@pytest.mark.parametrize("pelo", ["codigo", "banco"])
async def test_alvo_que_virou_producao_nao_comeca(
    banco_migrado: Parametros, forma: str, pelo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critica A4.1 e A4.5: depois do pedido, outro sistema e cadastrado com a producao igual ao alvo de teste escrita
    de outro jeito. A conferencia da declaracao (codigo) recusa antes de ``rodando``; sem ela, o banco recusa a
    passagem para ``rodando`` ("a bateria nao escreve na producao"). Nos dois, o pedido fica ``interrompido`` com
    ``sistema_mudou`` e a mesma frase, e o duble conta zero chamadas."""
    async with cena(banco_migrado) as cn:
        assert cn.duble.teste is not None
        producao_nova = ALVOS_ERRADOS[forma](cn.duble.teste)
        async with cn.dono.begin() as c:
            await s.proprio_com_acoes(c, endereco=producao_nova, nivel_do_contrato=None)
        if pelo == "banco":
            monkeypatch.setattr(declaracao_do_selo, "conferir", lambda *a, **k: None)
        assert await cn.corredor().uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "sistema_mudou")
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert bateria.ALVO_NA_PRODUCAO in linha.frase
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})


def _no_modo_de_teste(d: dict[str, Any], **entrada: Any) -> None:
    d.pop("endereco_teste")
    d.update(modo="ferramentas_de_teste")
    d["ferramentas"]["enviar_lembrete"].update({"ferramenta_de_teste": "enviar_lembrete_teste"} | entrada)


DECLARACOES_ERRADAS = {
    "rastro_que_e_acao": lambda d: d["ferramentas"]["enviar_lembrete"]["rastro"].update(ferramenta="enviar_lembrete"),
    "rastro_que_e_acao_no_modo_de_teste": lambda d: (
        _no_modo_de_teste(d),
        d["ferramentas"]["enviar_lembrete"]["rastro"].update(ferramenta="enviar_lembrete"),
    ),
    "ferramenta_de_teste_real": lambda d: _no_modo_de_teste(d, ferramenta_de_teste="listar_lembretes"),
    "ferramenta_de_teste_que_e_revisada": lambda d: _no_modo_de_teste(d, ferramenta_de_teste="listar_lidos"),
}


@pytest.mark.parametrize("pelo", ["declaracao", "alvo"])
@pytest.mark.parametrize("erro", sorted(DECLARACOES_ERRADAS))
async def test_declaracao_errada_de_proposito_nao_comeca(
    banco_migrado: Parametros, erro: str, pelo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critica A4.5: a declaracao gravada sem a tela (pelo dono) com o rastro numa acao (nos dois modos; no de
    ferramentas de teste, a leitura iria a producao), ou com a ferramenta de teste que e uma revisada de producao (a
    do rastro, ou outra). A bateria confere de novo antes de ``rodando`` e nao comeca. Sem essa conferencia (``alvo``),
    o ``AlvoDoTeste`` recusa antes da rede, com o cadastro de agora (A4.2 e A4.3). Nos dois, o pedido fica
    ``interrompido`` com ``sistema_mudou`` e a frase da declaracao, e o duble de producao conta zero chamadas de acao
    (e zero chamadas)."""
    async with cena(banco_migrado, leituras=("listar_lembretes", "listar_lidos")) as cn:
        dados = declaracao_do_duble(cn.duble, modo="ambiente_de_teste", acoes=ACOES_DA_CENA)
        DECLARACOES_ERRADAS[erro](dados)
        if pelo == "alvo":
            monkeypatch.setattr(declaracao_do_selo, "conferir", lambda *a, **k: None)
        async with cn.dono.begin() as c:
            await c.execute(
                text("UPDATE pedidos_teste_selo SET estado = 'interrompido', motivo = 'erro_interno' WHERE id = :p"),
                {"p": cn.pedido_id},
            )
            nova = await s.salvar_declaracao(c, cn.sistema.id, cn.admin_id, dados)
            cn.pedido_id = await s.pedir_teste(c, cn.sistema.id, cn.admin_id, nova, nivel=2, rodando=False)
        assert await cn.corredor().uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "sistema_mudou")
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert bateria.DECLARACAO_RECUSADA in linha.frase
        assert cn.duble.chamadas_de_acao("producao") == 0
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})


async def test_declaracao_incompleta_nao_comeca(banco_migrado: Parametros) -> None:
    """A declaracao que nao cobre uma acao do sistema (gravada sem a tela): a bateria confere de novo contra o
    cadastro antes de ``rodando`` e nao comeca, sem nenhuma escrita (o banco so recusaria no fim, depois delas)."""
    async with cena(
        banco_migrado,
        acoes=("enviar_lembrete", "marcar_lido"),
        leituras=("listar_lembretes", "listar_lidos"),
        pedir=False,
    ) as cn:
        dados = declaracao_do_duble(cn.duble, modo="ambiente_de_teste", acoes=("enviar_lembrete",))
        async with cn.dono.begin() as c:
            nova = await s.salvar_declaracao(c, cn.sistema.id, cn.admin_id, dados)
            cn.pedido_id = await s.pedir_teste(c, cn.sistema.id, cn.admin_id, nova, nivel=2, rodando=False)
        assert await cn.corredor().uma_volta()
        assert tuple(await cn.pedido()) == ("interrompido", "sistema_mudou")
        [linha] = [x for x in await cn.extrato() if x.tipo == "selo_teste_interrompido"]
        assert bateria.DECLARACAO_RECUSADA in linha.frase
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})


async def test_declaracao_com_o_alvo_na_producao_nem_e_salva(banco_migrado: Parametros) -> None:
    """Critica A4.5, o caso mais direto: a declaracao com o alvo igual a producao (escrito de outro jeito) nem e
    gravada pelo banco, entao nenhum pedido existe para a bateria rodar."""
    async with cena(banco_migrado, pedir=False) as cn:
        dados = declaracao_do_duble(cn.duble, modo="ambiente_de_teste", acoes=ACOES_DA_CENA)
        dados["endereco_teste"] = cn.sistema.endereco + "/"
        with pytest.raises(DBAPIError, match="a bateria nao escreve na producao"):
            async with cn.dono.begin() as c:
                await s.salvar_declaracao(c, cn.sistema.id, cn.admin_id, dados)
        assert not await cn.corredor().uma_volta()
        assert (cn.duble.chamadas("teste"), cn.duble.chamadas("producao")) == ({}, {})
