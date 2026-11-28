"""A bateria do selo e a tarefa do despachante que a roda (desenho 3.1, 3.2 e 3.4; criticas A4, B5, M8 e M11).

Quem pede e o ADMIN (``pedidos_teste_selo``, pelas telas); quem roda e o despachante, o unico papel que abre chaves e
fala com sistemas, numa tarefa propria (``Corredor``):

- um pedido por vez, pego com ``FOR UPDATE SKIP LOCKED`` numa conexao propria, que segura, enquanto o teste roda, a
  trava consultiva de sessao do pedido (``TRAVA_DA_BATERIA``, critica M8). Com varias copias do despachante, um pedido
  roda uma vez so; a partida (e cada volta da tarefa) so marca ``interrompido`` o pedido ``rodando`` cuja trava esta
  livre (``despachante_caiu``, com a linha ``selo_teste_interrompido`` no extrato). A bateria usa um grupo de conexoes
  proprio (o app do despachante monta um sem pool, ``cifra_origem.engine_sem_pool``) e nunca segura a porta de saida;
- com a porta de saida fechada ou o cofre trancado, o teste nao comeca (o pedido espera; passado
  ``BADGE_TEST_MAX_SECONDS`` desde o pedido, fica ``interrompido`` com ``prazo``). A porta e conferida de novo antes
  de cada ferramenta: fechada no meio, o teste para (``porta_fechada``);
- antes de ``rodando``: a declaracao do pedido e lida de novo (``declaracao.validar``) e conferida contra o cadastro
  de agora (``declaracao.conferir``; o mesmo cadastro vai ao ``AlvoDoTeste``, que recusa de novo, antes da rede, a
  leitura que e uma acao e a ferramenta de teste com nome de revisada, critica A4.2 e A4.3); o plano e calculado
  pela declaracao (``plano``, critica B5) e, passando de ``BADGE_TEST_MAX_CALLS``, o pedido fica ``interrompido`` com
  ``teto_de_chamadas`` ("o teste precisa de N chamadas e o teto e M"), sem nenhuma escrita; a chave de teste
  (``teste_selo``, critica M11) e aberta, nunca a de uma pessoa nem a compartilhada de producao (sem ela,
  ``sem_chave_de_teste``); a producao usa a chave de leitura das declaracoes;
- as recusas do banco a ``rodando`` (a declaracao ja nao e a mais recente, o sistema saiu de ``ativo``, o alvo virou a
  producao de algum sistema: "a bateria nao escreve na producao", ``check_violation``) viram ``interrompido`` com
  ``sistema_mudou`` (``porta_fechada`` se foi a porta), com a frase legivel no extrato;
- tetos da rodada: ``BADGE_TEST_MAX_CALLS`` (``selo.alvo.TetoDeChamadas``: o que falta fica ``nao_testado`` com
  ``teto``), ``BADGE_TEST_MAX_SECONDS`` (``interrompido`` com ``prazo``) e ``BADGE_TEST_PAIRS`` (C-2.2d);
- o registro nao tem conteudo: casos, contagens, horas e razoes do vocabulario (``contrato.CAMPOS_DO_ITEM``), nunca
  argumento nem texto do sistema. A bateria nunca toca ``operacoes``, ``fila_saida`` nem ``fichas``;
- o fim: ``testes_selo`` gravado na transacao que fecha o pedido (``terminado``), com o retrato do sistema do comeco
  do teste. O banco confere tudo de novo (``testes_selo_nasce``): se o sistema mudou no meio, ou o alvo virou producao,
  o INSERT e recusado e o pedido fica ``interrompido`` com ``sistema_mudou`` (critica M8), sem teste gravado. Antes do
  INSERT, ``contrato.problema_nos_casos`` e ``contrato.casos_aprovam`` (o mesmo criterio do banco).

O que vai ao extrato: ``selo_testado`` (aprovado), ``selo_nao_aprovado`` (com o aviso ao ADMIN) e
``selo_teste_interrompido``. Contadores: ``selo_testes{resultado,nivel}`` e ``selo_testes_interrompidos{motivo}``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import Sistema
from mcp_claro.cofre import CofreIndisponivel, CofreTrancado
from mcp_claro.config.settings import Ambiente
from mcp_claro.despachante import chaves
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.extrato import registrar
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.ligacao.ponto import Ligacao
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import estado
from mcp_claro.saida import cofre as saida_cofre
from mcp_claro.saida.protocolo import FUSO
from mcp_claro.selo import contrato
from mcp_claro.selo import declaracao as declaracao_do_selo
from mcp_claro.selo.alvo import AlvoDoTeste, Conectar, ForaDoAlvo, SemMarca, TetoDeChamadas, conectar_tcp
from mcp_claro.selo.casos import (
    FOLGA_DO_EFEITO,
    INTERVALO_DO_CORTE,
    Casos,
    Chegada,
    Juiz,
)
from mcp_claro.selo.declaracao import Declaracao, DeclaracaoRecusada, FerramentaDeclarada

log = logger("mcp_claro.selo.bateria")

Ligador = Callable[[Sistema], Ligacao]

TRAVA_DA_BATERIA: Final = "mcp_claro.selo.bateria"
"""A trava consultiva de sessao de um pedido rodando (com o id do pedido): ``pg_try_advisory_lock(hashtext(TRAVA),
hashtext(id))``. Livre com o pedido ``rodando``, a copia que o rodava caiu."""

MOTIVOS_LEGIVEIS: Final[Mapping[str, str]] = {
    "despachante_caiu": "o despachante parou no meio do teste",
    "sistema_mudou": "o sistema mudou durante o teste",
    "prazo": "o teste passou do tempo maximo",
    "porta_fechada": "a porta de saida estava fechada",
    "cofre_trancado": "o cofre de chaves estava trancado",
    "sem_chave_de_teste": "falta a chave de teste do selo",
    "teto_de_chamadas": "o teste precisa de mais chamadas que o teto",
    "erro_interno": "o MCP-CLARO falhou no meio do teste",
}
"""O ``{motivo}`` de ``selo_teste_interrompido`` (sem acento, como o extrato), um por motivo de
``contrato.MOTIVOS_DE_INTERRUPCAO``."""

ALVO_NA_PRODUCAO: Final = (
    "o alvo de teste e o endereco de producao de um sistema do cadastro, e a bateria nao escreve na producao"
)
ALVO_MAL_ESCRITO: Final = "o alvo de teste nao esta escrito de um jeito so"
DECLARACAO_RECUSADA: Final = "a declaracao de teste nao vale para o sistema de agora"
TERCEIRO_AINDA_NAO: Final = "o selo de servidores de terceiros ainda nao esta disponivel"

PRAZO_DA_ESCRITA: Final = 20.0
"""Prazo de cada acao e leitura da bateria (o app usa ``RESPONSE_MAX_WAIT_SECONDS``)."""
INTERVALO_DA_TAREFA: Final = 5.0


def frase_do_teto(plano: Plano, teto: int) -> str:
    """A frase da tela (com acento) quando o plano passa do teto (critica B5)."""
    return f"Este teste precisa de {plano.chamadas} chamadas; o teto é {teto}."


def _motivo_do_teto(plano: Plano, teto: int) -> str:
    return f"o teste precisa de {plano.chamadas} chamadas e o teto e {teto}"


# --- O plano ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plano:
    """Quantas chamadas o teste faz (``chamadas``: toda conexao, como o ``AlvoDoTeste`` conta) e quantas delas sao
    escritas reais no alvo (``escritas``), calculadas pela declaracao antes de comecar (critica B5)."""

    chamadas: int
    escritas: int


class _Conta:
    """Simula as chamadas de ``selo.casos.Casos`` (com a leitura do rastro reaproveitada quando nada foi chamado no
    meio), sem rede."""

    def __init__(self) -> None:
        self.chamadas = 0
        self.escritas = 0
        self._lido: set[tuple[str, str]] = set()

    def ler(self, ferramenta: str, argumentos: Mapping[str, Any]) -> None:
        chave = (ferramenta, json.dumps(dict(argumentos), sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        if chave not in self._lido:
            self.chamadas += 1
            self._lido.add(chave)

    def escrever(self, vezes: int = 1) -> None:
        self.chamadas += vezes
        self.escritas += vezes
        self._lido.clear()

    def perguntar(self, vezes: int = 1) -> None:
        self.chamadas += vezes
        self._lido.clear()

    def esquecer(self) -> None:
        self._lido.clear()


def _conta_da_ferramenta(conta: _Conta, e: FerramentaDeclarada, nivel: int, pares: int, dica: bool | None) -> None:
    r = e.rastro

    def rastro() -> None:
        conta.ler(r.ferramenta, r.argumentos)

    def escrita_entre_rastros() -> None:
        rastro()
        conta.escrever()
        rastro()

    escrita_entre_rastros()  # C-2.1
    rastro()  # C-2.2a
    conta.escrever()
    rastro()
    conta.perguntar()
    if e.idempotente is None and dica is not True:  # C-2.2b
        escrita_entre_rastros()
    escrita_entre_rastros()  # C-2.2c
    if pares > 0:  # C-2.2d
        rastro()
        conta.escrever(2 * pares)
        rastro()
    if e.pede_dados is not None:  # C-2.3
        escrita_entre_rastros()
    rastro()  # C-2.4a
    conta.perguntar(2)
    rastro()
    escrita_entre_rastros()  # C-2.4b
    conta.perguntar()
    conta.perguntar()  # C-2.5
    escrita_entre_rastros()
    rastro()  # C-2.6a
    conta.escrever()
    conta.perguntar(2)
    rastro()
    conta.escrever()
    rastro()
    if e.escrita_lenta is not None:  # C-2.6b
        rastro()
        conta.escrever()
        conta.perguntar()
        rastro()
    if e.idempotente is not None or dica is not False:  # C-2.7
        conteudo = (e.idempotente.rastro if e.idempotente is not None else None) or r
        for _ in range(2):
            conta.ler(conteudo.ferramenta, conteudo.argumentos)
            conta.escrever()
        conta.ler(conteudo.ferramenta, conteudo.argumentos)
    if nivel < 3:
        return
    escrita_entre_rastros()  # C-3.1a
    escrita_entre_rastros()  # C-3.1b
    if e.varios_itens is not None:  # C-3.3
        escrita_entre_rastros()
    if e.conflito is not None:  # C-3.4
        leitura = e.conflito.leitura
        conta.ler(leitura.ferramenta, leitura.argumentos)
        rastro()
        conta.escrever()
        conta.ler(leitura.ferramenta, leitura.argumentos)
        escrita_entre_rastros()
    conta.perguntar()  # C-3.5
    conta.perguntar(2)  # C-3.6a
    escrita_entre_rastros()
    if e.efeito_adiado is not None:  # C-3.6b
        rastro()
        conta.escrever()
        conta.perguntar()
        conta.esquecer()  # depois da espera dos segundos declarados, a leitura e nova
        rastro()
        conta.perguntar()


def plano(declaracao: Declaracao, nivel: int, *, pares: int, idempotentes: Iterable[str] | None = None) -> Plano:
    """O plano do teste pela declaracao (critica B5): as chamadas e as escritas reais no alvo, na ordem de
    ``selo.casos.Casos``. ``pares``: ``BADGE_TEST_PAIRS``. ``idempotentes``: as acoes que se declaram idempotentes na
    lista do alvo (``idempotentHint``); sem elas (a tela, antes do teste), o plano conta o maximo (o C-2.7 e o C-2.2b
    em toda acao)."""
    if nivel not in contrato.NIVEIS:
        raise ValueError(f"O contrato do selo so tem os niveis 2 e 3, nao {nivel}")
    conta = _Conta()
    conta.chamadas += 2 + 1 + 1  # as duas listas (C-0.1, C-0.2), a consulta de controle (C-0.3) e a producao (C-0.4)
    if declaracao.modo == "ambiente_de_teste":
        conta.chamadas += 1  # o alvo se declara de teste (C-0.4)
    conjunto = None if idempotentes is None else frozenset(idempotentes)
    for nome, entrada in declaracao.ferramentas.items():
        dica = None if conjunto is None else nome in conjunto
        _conta_da_ferramenta(conta, entrada, nivel, max(0, pares), dica)
    return Plano(conta.chamadas, conta.escritas)


# --- A rodada --------------------------------------------------------------------------------------------------


class PortaFechadaNoTeste(Exception):
    """A porta de saida fechou no meio do teste: nada mais sai."""


@dataclass(frozen=True, slots=True)
class Rodada:
    """O que a bateria deu: os itens de ``testes_selo.casos``, as chamadas e escritas feitas, se aprova e, se parou
    no meio, a razao dos casos que ficaram sem teste (``fora_do_alvo`` ou ``teto``)."""

    itens: list[dict[str, Any]]
    chamadas: int
    escritas: int
    aprovado: bool
    parou: str | None
    partes: dict[tuple[str, str | None], dict[str, bool | None]]


def _parada(erro: BaseException) -> str | None:
    """A razao de parar a bateria no meio (as excecoes podem vir num grupo, de dentro de um grupo de tarefas)."""
    if isinstance(erro, SemMarca | ForaDoAlvo):
        return "fora_do_alvo"
    if isinstance(erro, TetoDeChamadas):
        return "teto"
    if isinstance(erro, BaseExceptionGroup):
        razoes = {_parada(e) for e in erro.exceptions}
        if None in razoes:
            return None
        return "fora_do_alvo" if "fora_do_alvo" in razoes else "teto"
    return None


async def rodar_bateria(
    alvo: AlvoDoTeste,
    declaracao: Declaracao,
    *,
    ferramentas: Mapping[str, str],
    nivel: int,
    pares: int,
    revisadas: Iterable[str] = (),
    desde: datetime | None = None,
    chegada: Chegada | None = None,
    intervalo_do_corte: float = INTERVALO_DO_CORTE,
    folga_do_efeito: float = FOLGA_DO_EFEITO,
    antes_da_ferramenta: Callable[[], Awaitable[None]] | None = None,
) -> Rodada:
    """Roda os casos (``selo.casos``) contra o alvo fechado. ``ferramentas``: o nome de cada acao da declaracao e o
    uuid da revisada. Sem a marca positiva (ou sem resposta a ``selo_alvo``), nenhuma escrita sai e os casos por
    ferramenta ficam ``nao_testado`` com ``fora_do_alvo``; na primeira resposta de ferramenta de teste sem a marca, a
    bateria para ali (o mesmo). No teto de chamadas, o que falta fica com ``teto``. ``antes_da_ferramenta`` confere a
    porta de saida (levanta ``PortaFechadaNoTeste``, que sobe)."""
    juiz = Juiz(ferramentas, nivel)
    casos = Casos(
        alvo,
        declaracao,
        juiz,
        pares=pares,
        revisadas=revisadas,
        desde=desde,
        chegada=chegada,
        intervalo_do_corte=intervalo_do_corte,
        folga_do_efeito=folga_do_efeito,
    )
    parou: str | None = None
    try:
        await casos.sistema()
        if not alvo.pode_escrever:
            parou = "fora_do_alvo"
        else:
            for nome in declaracao.ferramentas:
                if antes_da_ferramenta is not None:
                    await antes_da_ferramenta()
                await casos.ferramenta(nome)
    except BaseException as erro:
        parou = _parada(erro)
        if parou is None:
            raise
        log.warning("bateria_parou", razao=parou, erro=type(erro).__name__)
    casos.alvos(parou)
    if parou is not None:
        juiz.completar(parou)
    itens = juiz.itens
    aprovado = contrato.casos_aprovam(itens, list(ferramentas.values()), nivel)
    return Rodada(itens, alvo.chamadas, alvo.escritas, aprovado, parou, juiz.partes)


def casos_que_reprovam(itens: Sequence[Mapping[str, Any]], nivel: int) -> int:
    """Quantos itens falharam ou, num caso obrigatorio do nivel, ficaram sem aprovar (o ``{falhas}`` das frases): a
    conta unica de ``contrato.casos_que_nao_aprovam``."""
    return contrato.casos_que_nao_aprovam(list(itens), nivel)


# --- A tarefa do despachante -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pego:
    pedido_id: uuid.UUID
    sistema: Sistema
    nivel: int
    declaracao_id: uuid.UUID
    modelo: Declaracao
    cadastro: declaracao_do_selo.Cadastro
    ferramentas: dict[str, str]
    segredo_teste: str
    segredo_producao: str | None
    discover: str | None
    iniciado_em: datetime


class _Interromper(Exception):
    def __init__(self, motivo: str, legivel: str | None = None) -> None:
        super().__init__(motivo)
        self.motivo = motivo
        self.legivel = legivel or MOTIVOS_LEGIVEIS[motivo]


def _recusa_do_banco(erro: DBAPIError) -> _Interromper:
    """A recusa do banco a ``rodando`` ou ao teste: a porta fechada, o alvo na producao ou mal escrito, ou o sistema
    que mudou (a declaracao ja nao e a mais recente, saiu de ``ativo``, mudou o retrato)."""
    mensagem = str(getattr(erro, "orig", erro))
    if "porta de saida fechada" in mensagem:
        return _Interromper("porta_fechada")
    if "a bateria nao escreve na producao" in mensagem:
        return _Interromper("sistema_mudou", ALVO_NA_PRODUCAO)
    if "escrito de um jeito so" in mensagem:
        return _Interromper("sistema_mudou", ALVO_MAL_ESCRITO)
    return _Interromper("sistema_mudou")


def _recusa_da_declaracao(erro: DeclaracaoRecusada) -> _Interromper:
    if erro.motivo == "selo_terceiro_ainda_nao":
        return _Interromper("sistema_mudou", TERCEIRO_AINDA_NAO)
    if erro.motivo == "alvo_na_producao":
        return _Interromper("sistema_mudou", ALVO_NA_PRODUCAO)
    return _Interromper("sistema_mudou", DECLARACAO_RECUSADA)


async def _travar(c: AsyncConnection, pedido_id: uuid.UUID) -> bool:
    return bool(
        (
            await c.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:t), hashtext(:p))"),
                {"t": TRAVA_DA_BATERIA, "p": str(pedido_id)},
            )
        ).scalar_one()
    )


async def _soltar(c: AsyncConnection, pedido_id: uuid.UUID) -> None:
    await c.execute(
        text("SELECT pg_advisory_unlock(hashtext(:t), hashtext(:p))"), {"t": TRAVA_DA_BATERIA, "p": str(pedido_id)}
    )


async def _interromper(
    c: AsyncConnection, pedido_id: uuid.UUID, sistema_id: uuid.UUID, motivo: _Interromper, *, de: Sequence[str]
) -> bool:
    """``interrompido`` com o motivo (de um dos estados ``de``), com a linha do extrato, na transacao de ``c``."""
    linha = (
        await c.execute(
            text(
                "UPDATE pedidos_teste_selo SET estado = 'interrompido', motivo = :m WHERE id = :p AND estado = ANY(:de)"
                " RETURNING (SELECT nome FROM sistemas WHERE id = :s)"
            ),
            {"m": motivo.motivo, "p": pedido_id, "de": list(de), "s": sistema_id},
        )
    ).first()
    if linha is None:
        return False
    await registrar(
        c,
        papel="despachante",
        tipo="selo_teste_interrompido",
        campos={"sistema": str(linha[0]), "motivo": motivo.legivel},
        sistema_id=sistema_id,
        dados={"pedido": str(pedido_id), "motivo": motivo.motivo},
    )
    CONTADORES.somar("selo_testes_interrompidos", motivo=motivo.motivo)
    return True


class Corredor:
    """A tarefa da bateria no despachante (veja o cabecalho do modulo).

    ``engine``: o grupo de conexoes da bateria, separado do do despachante. ``ligacao``: o ponto unico de ligacao
    (``Despachante.ligacao``). ``cofre``: o cliente do cofre com ``saude()`` (o app liga na partida). ``chegada``,
    ``intervalo_do_corte``, ``folga_do_efeito`` e ``conectar``: os ganchos dos testes (veja ``selo.casos``)."""

    def __init__(
        self,
        engine: AsyncEngine,
        cifra: Cifra,
        ligacao: Ligador,
        *,
        max_chamadas: int = 300,
        max_segundos: float = 900.0,
        pares: int = 10,
        prazo_da_escrita: float = PRAZO_DA_ESCRITA,
        prazo_do_contrato: float = 10.0,
        ambiente: str = "producao",
        emails_admin: Sequence[str] = (),
        intervalo: float = INTERVALO_DA_TAREFA,
        chegada: Chegada | None = None,
        intervalo_do_corte: float = INTERVALO_DO_CORTE,
        folga_do_efeito: float = FOLGA_DO_EFEITO,
        conectar: Conectar = conectar_tcp,
    ) -> None:
        self.engine = engine
        self.cifra = cifra
        self.ligacao = ligacao
        self.max_chamadas = max_chamadas
        self.max_segundos = max_segundos
        self.pares = pares
        self.prazo_da_escrita = prazo_da_escrita
        self.prazo_do_contrato = prazo_do_contrato
        self.ambiente = str(ambiente)
        self.emails_admin = tuple(emails_admin)
        self.intervalo = intervalo
        self.chegada = chegada
        self.intervalo_do_corte = intervalo_do_corte
        self.folga_do_efeito = folga_do_efeito
        self.conectar = conectar
        self.cofre: saida_cofre.SaudeDoCofre | None = None
        self._travado: tuple[uuid.UUID, uuid.UUID] | None = None
        self.ultima: Rodada | None = None
        """A ultima rodada desta copia (para as provas)."""

    # --- Partida e pedidos velhos ------------------------------------------------------------------------------

    async def marcar_interrompidos(self) -> int:
        """Todo pedido ``rodando`` cuja trava esta livre (a copia que o rodava caiu) fica ``interrompido`` com
        ``despachante_caiu``, com a linha do extrato. O que outra copia esta rodando (trava ocupada) fica como esta
        (critica M8)."""
        async with self.engine.connect() as c:
            rodando = list(
                (
                    await c.execute(
                        text("SELECT id, sistema_id FROM pedidos_teste_selo WHERE estado = 'rodando' ORDER BY id")
                    )
                ).all()
            )
            await c.commit()
        marcados = 0
        for pedido in rodando:
            pedido_id, sistema_id = uuid.UUID(str(pedido.id)), uuid.UUID(str(pedido.sistema_id))
            async with self.engine.connect() as c:
                if not await _travar(c, pedido_id):
                    await c.commit()
                    continue
                try:
                    await c.commit()
                    async with c.begin():
                        motivo = _Interromper("despachante_caiu")
                        if await _interromper(c, pedido_id, sistema_id, motivo, de=("rodando",)):
                            marcados += 1
                finally:
                    await _soltar(c, pedido_id)
                    await c.commit()
        return marcados

    async def expirar(self) -> int:
        """O pedido que espera (``pedido``) ha mais de ``BADGE_TEST_MAX_SECONDS`` (a porta fechada, o cofre trancado)
        fica ``interrompido`` com ``prazo``."""
        async with self.engine.begin() as c:
            velhos = list(
                (
                    await c.execute(
                        text(
                            "SELECT id, sistema_id FROM pedidos_teste_selo WHERE estado = 'pedido'"
                            " AND pedido_em < now() - make_interval(secs => :s) ORDER BY pedido_em, id"
                            " FOR UPDATE SKIP LOCKED"
                        ),
                        {"s": float(self.max_segundos)},
                    )
                ).all()
            )
            total = 0
            for v in velhos:
                pedido_id, sistema_id = uuid.UUID(str(v.id)), uuid.UUID(str(v.sistema_id))
                if await _interromper(c, pedido_id, sistema_id, _Interromper("prazo"), de=("pedido",)):
                    total += 1
        return total

    # --- Um pedido ---------------------------------------------------------------------------------------------

    async def _pode_comecar(self) -> bool:
        """A porta de saida aberta e o cofre destravado (sem cliente do cofre, nos testes, so a porta)."""
        async with self.engine.connect() as c:
            aberta = (await estado.ler_saida(c)).aberta
        if not aberta:
            return False
        if self.cofre is not None:
            return await saida_cofre.ler(self.cofre) is saida_cofre.Leitura.DESTRAVADO
        return True

    async def _pegar(self, c: AsyncConnection) -> _Pego | None:
        """Na transacao de ``c``: o proximo pedido (``FOR UPDATE SKIP LOCKED``), a trava dele, a declaracao e o
        cadastro conferidos, o plano, as chaves e ``rodando``. Levanta ``_Interromper`` com o pedido ja travado."""
        linha = (
            await c.execute(
                text(
                    "SELECT id, sistema_id, nivel, declaracao_id FROM pedidos_teste_selo WHERE estado = 'pedido'"
                    " ORDER BY pedido_em, id LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
            )
        ).first()
        if linha is None:
            return None
        pedido_id, sistema_id = uuid.UUID(str(linha.id)), uuid.UUID(str(linha.sistema_id))
        if not await _travar(c, pedido_id):
            return None
        self._travado = (pedido_id, sistema_id)
        sistema = await repositorio.sistema_por_id(c, sistema_id)
        if sistema is None:
            raise _Interromper("sistema_mudou")
        dados = (
            await c.execute(text("SELECT declaracao FROM declaracoes_selo WHERE id = :d"), {"d": linha.declaracao_id})
        ).scalar_one()
        try:
            modelo = declaracao_do_selo.validar(dict(dados))
            cadastro = await declaracao_do_selo.cadastro_do_sistema(c, sistema_id)
            declaracao_do_selo.conferir(modelo, cadastro, ambiente=Ambiente(self.ambiente))
        except DeclaracaoRecusada as erro:
            raise _recusa_da_declaracao(erro) from None
        nivel = int(linha.nivel)
        previsto = plano(modelo, nivel, pares=self.pares)
        if previsto.chamadas > self.max_chamadas:
            raise _Interromper("teto_de_chamadas", _motivo_do_teto(previsto, self.max_chamadas))
        acoes = {
            str(r.nome_original): str(r.id)
            for r in await c.execute(
                text("SELECT id, nome_original FROM ferramentas_revisadas WHERE id = ANY (selo_acoes_do_sistema(:s))"),
                {"s": sistema_id},
            )
        }
        ferramentas = {nome: acoes[nome] for nome in modelo.ferramentas if nome in acoes}
        if len(ferramentas) != len(modelo.ferramentas):  # conferir ja confere; por defesa
            raise _Interromper("sistema_mudou", DECLARACAO_RECUSADA)
        try:
            segredo_teste = await chaves.abrir(c, self.cifra, sistema_id=sistema_id, tipo=contrato.TIPO_CHAVE_TESTE)
        except chaves.ChaveAusente:
            raise _Interromper("sem_chave_de_teste") from None
        except (CofreTrancado, CofreIndisponivel) as erro:
            log.warning("bateria_cofre", erro=type(erro).__name__)
            raise _Interromper("cofre_trancado") from None
        try:
            segredo_producao: str | None = await chaves.abrir(c, self.cifra, sistema_id=sistema_id, tipo="declaracoes")
        except chaves.ChaveAusente:
            segredo_producao = None
        except (CofreTrancado, CofreIndisponivel) as erro:
            log.warning("bateria_cofre", erro=type(erro).__name__)
            raise _Interromper("cofre_trancado") from None
        sondagem = (
            await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema_id})
        ).scalar_one_or_none()
        try:
            async with c.begin_nested():
                iniciado_em = (
                    await c.execute(
                        text(
                            "UPDATE pedidos_teste_selo SET estado = 'rodando' WHERE id = :p AND estado = 'pedido'"
                            " RETURNING mudou_em"
                        ),
                        {"p": pedido_id},
                    )
                ).scalar_one()
        except DBAPIError as erro:
            raise _recusa_do_banco(erro) from None
        return _Pego(
            pedido_id,
            sistema,
            nivel,
            uuid.UUID(str(linha.declaracao_id)),
            modelo,
            cadastro,
            ferramentas,
            segredo_teste,
            segredo_producao,
            json.dumps(sondagem) if isinstance(sondagem, dict) else sondagem,
            iniciado_em,
        )

    async def rodar_um(self) -> bool:
        """Pega e roda um pedido, se houver e se a porta e o cofre deixarem. Devolve se rodou um (ou o deixou
        ``interrompido`` antes de comecar)."""
        if not await self._pode_comecar():
            return False
        dono = await self.engine.connect()
        self._travado = None
        try:
            try:
                async with dono.begin():
                    pego = await self._pegar(dono)
            except _Interromper as motivo:
                if self._travado is None:  # so levantado com o pedido travado; por defesa
                    raise
                pedido_id, sistema_id = self._travado
                async with dono.begin():
                    marcado = await _interromper(dono, pedido_id, sistema_id, motivo, de=("pedido",))
                return marcado
            if pego is None:
                return False
            await self._rodar(dono, pego)
            return True
        finally:
            if self._travado is not None:
                with contextlib.suppress(SQLAlchemyError, OSError):
                    await _soltar(dono, self._travado[0])
                    await dono.commit()
            with contextlib.suppress(SQLAlchemyError, OSError):
                await dono.invalidate()
            with contextlib.suppress(SQLAlchemyError, OSError):
                await dono.close()

    async def _rodar(self, dono: AsyncConnection, pego: _Pego) -> None:
        sistema = pego.sistema

        async def antes_da_ferramenta() -> None:
            async with dono.begin():
                aberta = (await estado.ler_saida(dono)).aberta
            if not aberta:
                raise PortaFechadaNoTeste

        try:
            alvo = AlvoDoTeste(
                pego.modelo,
                producao=sistema.endereco,
                producoes=pego.cadastro.producoes,
                nivel=pego.nivel,
                ligacao=self.ligacao(sistema),
                segredo_teste=pego.segredo_teste,
                segredo_producao=pego.segredo_producao,
                teto=self.max_chamadas,
                prazo=self.prazo_da_escrita,
                prazo_do_contrato=self.prazo_do_contrato,
                discover_producao=pego.discover,
                conectar=self.conectar,
                cadastro=pego.cadastro,
            )
            with anyio.fail_after(self.max_segundos):
                rodada = await rodar_bateria(
                    alvo,
                    pego.modelo,
                    ferramentas=pego.ferramentas,
                    nivel=pego.nivel,
                    pares=self.pares,
                    revisadas=pego.cadastro.revisadas,
                    desde=pego.iniciado_em,
                    chegada=self.chegada,
                    intervalo_do_corte=self.intervalo_do_corte,
                    folga_do_efeito=self.folga_do_efeito,
                    antes_da_ferramenta=antes_da_ferramenta,
                )
        except ForaDoAlvo as erro:  # o alvo que e a producao, ou a declaracao fora da lista fechada, antes da rede
            log.warning("bateria_fora_do_alvo", motivo=erro.motivo)
            motivo = _Interromper(
                "sistema_mudou", ALVO_NA_PRODUCAO if erro.motivo == "alvo_na_producao" else DECLARACAO_RECUSADA
            )
            await self._fechar_interrompido(dono, pego, motivo)
            return
        except PortaFechadaNoTeste:
            await self._fechar_interrompido(dono, pego, _Interromper("porta_fechada"))
            return
        except TimeoutError:
            await self._fechar_interrompido(dono, pego, _Interromper("prazo"))
            return
        except asyncio.CancelledError:
            raise
        except Exception as erro:  # noqa: BLE001 - toda outra falha e nossa: o pedido fica interrompido, nada gravado
            log.warning("bateria_falhou", erro=type(erro).__name__)
            await self._fechar_interrompido(dono, pego, _Interromper("erro_interno"))
            return
        self.ultima = rodada
        await self._gravar(dono, pego, rodada)

    async def _fechar_interrompido(self, dono: AsyncConnection, pego: _Pego, motivo: _Interromper) -> None:
        try:
            async with dono.begin():
                await _interromper(dono, pego.pedido_id, pego.sistema.id, motivo, de=("rodando",))
        except (SQLAlchemyError, OSError) as erro:
            log.warning("bateria_fim_nao_gravado", erro=type(erro).__name__)

    async def _gravar(self, dono: AsyncConnection, pego: _Pego, rodada: Rodada) -> None:
        """``testes_selo`` na transacao que fecha o pedido (``terminado``), com o retrato do comeco do teste. O banco
        recusa um sistema que mudou no meio: o pedido fica ``interrompido`` com ``sistema_mudou``."""
        ferramentas = list(pego.ferramentas.values())
        problema = contrato.problema_nos_casos(rodada.itens, ferramentas)
        if problema is not None or rodada.chamadas < contrato.contagem_minima_de_chamadas(rodada.itens):
            log.warning("bateria_casos_fora_da_forma", problema=problema or "chamadas")
            await self._fechar_interrompido(dono, pego, _Interromper("erro_interno"))
            return
        nivel_obtido = pego.nivel if rodada.aprovado else 1
        sistema = pego.sistema
        try:
            async with dono.begin():
                linha = (
                    await dono.execute(
                        text(
                            "INSERT INTO testes_selo (pedido_id, sistema_id, declaracao_id, contrato, bateria,"
                            " nivel_pedido, resultado, nivel_obtido, sistema_endereco, sistema_tipo_chave,"
                            " revisao_servidor_id, ferramentas, casos, nao_testado, chamadas, iniciado_em)"
                            " VALUES (:p, :s, :d, :c, :b, :n, :r, :o, :e, :t, :rv, CAST(:f AS uuid[]),"
                            " CAST(:casos AS jsonb), CAST(:nt AS jsonb), :ch, :i) RETURNING id, vale_ate"
                        ),
                        {
                            "p": pego.pedido_id,
                            "s": sistema.id,
                            "d": pego.declaracao_id,
                            "c": contrato.CONTRATO,
                            "b": contrato.VERSAO_BATERIA,
                            "n": pego.nivel,
                            "r": "aprovado" if rodada.aprovado else "reprovado",
                            "o": nivel_obtido,
                            "e": sistema.endereco,
                            "t": sistema.tipo_chave,
                            "rv": sistema.revisao_servidor_id,
                            "f": sorted(ferramentas),
                            "casos": json.dumps(rodada.itens),
                            "nt": json.dumps(contrato.nao_testado_gravado()),
                            "ch": rodada.chamadas,
                            "i": pego.iniciado_em,
                        },
                    )
                ).one()
                await dono.execute(
                    text("UPDATE pedidos_teste_selo SET estado = 'terminado' WHERE id = :p"), {"p": pego.pedido_id}
                )
                teste_id = uuid.UUID(str(linha.id))
                dados = {"pedido": str(pego.pedido_id), "teste": str(teste_id), "chamadas": rodada.chamadas}
                if rodada.aprovado:
                    vale_ate = linha.vale_ate.astimezone(ZoneInfo(FUSO)).strftime("%d/%m/%Y")
                    await registrar(
                        dono,
                        papel="despachante",
                        tipo="selo_testado",
                        campos={"nivel": pego.nivel, "sistema": sistema.nome, "vale_ate": vale_ate},
                        sistema_id=sistema.id,
                        dados=dados,
                    )
                else:
                    falhas = casos_que_reprovam(rodada.itens, pego.nivel)
                    await registrar(
                        dono,
                        papel="despachante",
                        tipo="selo_nao_aprovado",
                        campos={"nivel": pego.nivel, "sistema": sistema.nome, "falhas": falhas},
                        sistema_id=sistema.id,
                        dados=dados,
                    )
                    assunto, texto_aviso = aviso_admin(
                        "selo_nao_aprovado", nivel=pego.nivel, sistema=sistema.nome, falhas=falhas
                    )
                    await avisar_admin(
                        dono,
                        "selo_nao_aprovado",
                        assunto,
                        {"texto": texto_aviso, "sistema": sistema.nome, "sistema_id": str(sistema.id)},
                        f"selo_nao_aprovado|{teste_id}",
                        self.emails_admin,
                    )
        except DBAPIError as erro:
            log.warning("bateria_teste_recusado", sqlstate=getattr(erro.orig, "sqlstate", None))
            await self._fechar_interrompido(dono, pego, _recusa_do_banco(erro))
            return
        CONTADORES.somar("selo_testes", resultado="aprovado" if rodada.aprovado else "reprovado", nivel=str(pego.nivel))

    # --- A volta da tarefa -------------------------------------------------------------------------------------

    async def uma_volta(self) -> bool:
        """Uma volta: os pedidos velhos e os rodando sem dono ficam interrompidos, e um pedido roda, se houver."""
        await self.expirar()
        await self.marcar_interrompidos()
        return await self.rodar_um()

    async def rodar(self) -> None:
        """A tarefa: uma volta a cada ``intervalo`` segundos (de novo na hora, se rodou um pedido), ate ser cancelada.
        A primeira volta espera o intervalo: a partida ja marcou os pedidos sem dono (``despachante.app``). Uma volta
        que falha (o banco fora) so espera a proxima."""
        while True:
            await asyncio.sleep(self.intervalo)
            try:
                while await self.uma_volta():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as erro:  # noqa: BLE001 - a tarefa nunca morre por uma volta que falhou
                log.warning("bateria_volta_falhou", erro=type(erro).__name__)
