"""Sequencias sorteadas da porta de saida (hypothesis, modo de estados), pelas funcoes publicas, contra o
PostgreSQL real com o usuario de cada papel e o sistema de exemplo ``acoes_caixa`` (conta cada chamada).

Passos sorteados: pedir (direto em ``Pedidos.pedir`` ou pelo aplicativo, via MCP), confirmar, recusar,
cancelar, seguir agora, decidir uma nao confirmada, bloquear e encerrar o bloqueio, enviar (a porta faz uma
volta), perder a resposta (o sistema executa e a resposta nao chega), queda do despachante antes ou depois de
chamar o sistema, reiniciar o despachante (assume de novo) e restauracao do banco (porta de saida fechada e
fila parada, como na conferencia de continuidade), o ADMIN fechando a porta de saida sem parar a fila, e a
reabertura pelo ADMIN.

A cada passo:

- nenhuma acao chega ao sistema mais de uma vez, e toda chamada que o sistema contou foi de uma acao do
  passeio;
- a acao so chega ao sistema com a ficha confirmada (a de risco alto, com passkey) ou com um limite;
- nada sai com bloqueio ativo que vale para a acao, nem com a porta de saida fechada;
- a nao confirmada nunca e reenviada;
- o pedido igual devolve a operacao original enquanto ela ainda pode sair (e a que ja saiu, na janela);
- nenhuma acao fica esperando dentro do alcance de um bloqueio ativo, e estado final nao muda.

Poucos exemplos a cada mudanca; ``MCP_CLARO_TESTE_LONGO=1`` roda os passeios longos (rodada diaria).
Os passos rodam no laco de eventos do teste; o hypothesis roda numa linha de execucao a parte e espera cada
passo terminar.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro import continuidade
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import Aplicativo, FerramentaVitrine
from mcp_claro.config.settings import Parametros
from mcp_claro.ligacao import cliente
from mcp_claro.regras import bloqueios, estado
from mcp_claro.saida import fila, travas
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import FINAIS, TRANSICOES, Estado
from mcp_claro.saida.fila import Operacao
from mcp_claro.saida.pedidos import AcaoRecusada, Pedido, Pedidos
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import chamar, politica
from tests.integracao.test_saida_disputas import ALCANCES, ambiente_sem_porta, limpar, sem_sobras
from tests.integracao.test_saida_disputas import _partes as travas_partes

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

LONGO = os.environ.get("MCP_CLARO_TESTE_LONGO") == "1"
EXEMPLOS = 150 if LONGO else 10
PASSOS = 60 if LONGO else 40
ESPERA_LONGA = 3600
"""Espera (segundos) das acoes liberadas que so saem com "seguir agora"."""
FERRAMENTAS: Final = ("criar_chamado", "enviar_mensagem", "cancelar_servico")
PENDENTES: Final = frozenset({Estado.AGUARDANDO_FICHA, Estado.LIBERADA, Estado.ENVIANDO})
ESPERANDO: Final = frozenset({Estado.AGUARDANDO_FICHA, Estado.LIBERADA})


class Queda(BaseException):
    """O despachante cai no meio do envio (nao e ``Exception``: passa pelos tratamentos de erro da porta)."""


def _alcancaveis() -> dict[Estado, set[Estado]]:
    """Estados alcancaveis a partir de cada estado, pela tabela de transicoes (fecho)."""
    saida: dict[Estado, set[Estado]] = {e: {e} for e in Estado}
    mudou = True
    while mudou:
        mudou = False
        for de, para in TRANSICOES:
            for destinos in saida.values():
                if de in destinos and para not in destinos:
                    destinos.add(para)
                    mudou = True
    return saida


ALCANCAVEIS: Final = _alcancaveis()


@dataclass
class Acao:
    """O que o passeio sabe de uma operacao."""

    op: Operacao
    pessoa: str
    ferramenta: str
    argumentos: dict[str, Any]
    risco_alto: bool
    liberada_por: str | None = None
    """"ficha", "ficha_com_passkey" ou "limite": quando a acao pode sair."""
    estado: Estado = Estado.AGUARDANDO_FICHA
    decisao: str | None = None
    ficha_decidida: bool = False


@dataclass
class BloqueioAtivo:
    b: bloqueios.Bloqueio

    def vale_para(self, op: Operacao) -> bool:
        b = self.b
        return {
            "pessoa": b.pessoa_id == op.pessoa_id,
            "aplicativo": b.aplicativo_id == op.aplicativo_id,
            "pessoa_aplicativo": (b.pessoa_id, b.aplicativo_id) == (op.pessoa_id, op.aplicativo_id),
            "sistema": b.sistema_id == op.sistema_id,
            "tudo": True,
        }[b.alcance]


@dataclass
class Mundo:
    """Estado compartilhado entre os passeios: arranjo, porta de saida e a contagem das chamadas."""

    amb: Ambiente
    aplicativo: Aplicativo
    ferramentas: dict[tuple[str, str], FerramentaVitrine]
    pessoas: dict[str, uuid.UUID]
    acoes: dict[uuid.UUID, Acao] = field(default_factory=dict)
    bloqueios: dict[uuid.UUID, BloqueioAtivo] = field(default_factory=dict)
    saida_aberta: bool = True
    porta: PortaDeSaida | None = None
    dono: AsyncConnection | None = None
    modo: str = "normal"
    atual: Operacao | None = None
    chamadas: Counter[uuid.UUID] = field(default_factory=Counter)
    violacoes: list[str] = field(default_factory=list)
    nao_confirmadas: set[uuid.UUID] = field(default_factory=set)
    base_do_sistema: int = 0
    notas: list[str] = field(default_factory=list)
    """O que cada passo fez, para o relato do hypothesis (``note`` so vale na linha de execucao dele)."""
    resumo: Counter[str] = field(default_factory=Counter)
    """Estados e passos vistos em todos os passeios (para conferir que o passeio nao ficou vazio)."""

    def sistema_contou(self) -> int:
        return sum(int(n) for n in self.amb.sistema_app.state.contagem.chamadas.values())

    async def ao_chamar(self, *args: Any, **kwargs: Any) -> cliente.Resposta:
        """No lugar da chamada ao sistema: confere as regras na hora em que a acao sai, e aplica o modo."""
        op = self.atual
        modo, self.modo = self.modo, "normal"
        if op is None:
            self.violacoes.append("chamada ao sistema fora de um envio da porta")
            return await cliente.chamar_ferramenta(*args, **kwargs)
        acao = self.acoes.get(op.id)
        async with self.amb.admin.connect() as c:
            agora = (
                await c.execute(text("SELECT estado FROM fila_saida WHERE id = :i"), {"i": op.fila_id})
            ).scalar_one()
        if acao is None:
            self.violacoes.append(f"{op.protocolo}: acao que o passeio nao pediu chegou ao envio")
        else:
            if acao.liberada_por is None:
                self.violacoes.append(f"{op.protocolo}: saiu sem ficha confirmada nem limite")
            if acao.risco_alto and acao.liberada_por != "ficha_com_passkey":
                self.violacoes.append(f"{op.protocolo}: risco alto saiu sem passkey")
        if agora != "enviando":
            self.violacoes.append(f"{op.protocolo}: saiu com a linha em {agora}, nao em enviando")
        if not self.saida_aberta:
            self.violacoes.append(f"{op.protocolo}: saiu com a porta de saida fechada")
        for b in self.bloqueios.values():
            if b.vale_para(op):
                self.violacoes.append(f"{op.protocolo}: saiu com o bloqueio {b.b.alcance} ativo")
        if op.id in self.nao_confirmadas:
            self.violacoes.append(f"{op.protocolo}: nao confirmada reenviada")
        if modo == "cai_antes":
            raise Queda
        self.chamadas[op.id] += 1
        if self.chamadas[op.id] > 1:
            self.violacoes.append(f"{op.protocolo}: chegou ao sistema {self.chamadas[op.id]} vezes")
        resposta = await cliente.chamar_ferramenta(*args, **kwargs)
        if modo == "perde":
            return cliente.Resposta(tipo="duvida", excecao="ReadError")
        if modo == "cai_depois":
            raise Queda
        return resposta


class PortaDoPasseio(PortaDeSaida):
    """A porta de saida de verdade; so marca qual acao esta saindo, para a contagem por acao."""

    def __init__(self, mundo: Mundo) -> None:
        super().__init__(mundo.amb.despachante_db, mundo.amb.cifra, varredura=0.2, prazo_envio=5.0)
        self.mundo = mundo
        self.chamar = mundo.ao_chamar

    async def _enviar(self, op: Operacao, *args: Any) -> Any:
        self.mundo.atual = op
        try:
            return await super()._enviar(op, *args)
        finally:
            self.mundo.atual = None


# --- Passos (rodam no laco de eventos do teste) ---------------------------------------------------


async def assumir_a_porta(m: Mundo) -> list[str]:
    """Partida do despachante: conexao propria com a trava de despachante unico, e a retomada."""
    await soltar_a_porta(m)
    dono = await m.amb.despachante_db.connect()
    pg: Any = (await dono.get_raw_connection()).driver_connection
    if not await pg.fetchval("SELECT pg_try_advisory_lock($1)", travas.DESPACHANTE_UNICO):
        await dono.invalidate()
        raise AssertionError("outra porta segura a trava de despachante unico")
    porta = PortaDoPasseio(m)
    retomadas = await porta.assumir(dono)
    m.porta, m.dono = porta, dono
    return retomadas


async def soltar_a_porta(m: Mundo) -> None:
    """A conexao da trava cai (queda do processo): a trava vai junto. Espera o banco soltar a trava (a sessao
    do servidor termina um instante depois de a conexao fechar), para o passo seguinte nao depender da hora."""
    if m.dono is not None:
        await m.dono.invalidate()
        await m.dono.close()
    m.porta, m.dono = None, None
    classe, objeto = travas_partes(travas.DESPACHANTE_UNICO)
    for _ in range(200):
        async with m.amb.admin.connect() as c:
            presa = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                        " AND classid = :c AND objid = :o AND objsubid = 1"
                    ),
                    {"c": classe, "o": objeto},
                )
            ).scalar_one()
        if not presa:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("a trava de despachante unico nao foi solta")


async def preparar(m: Mundo, tag: str) -> None:
    """Cada passeio comeca limpo: sem bloqueio, porta aberta, sem sobras na fila e com um limite novo."""
    amb = m.amb
    await soltar_a_porta(m)
    await limpar(amb)  # politica do teste, bloqueios e sobras liberadas
    await politica(amb, CONFIRM_CAP_PER_HOUR=1000, OUTBOX_HOLD_SECONDS=ESPERA_LONGA)
    if not (await ler_saida(m)):
        await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Comeco de um passeio")
    async with amb.admin.begin() as c:
        # A contagem do teto de fichas recomeca a cada passeio (como depois de o ADMIN reativar).
        await c.execute(
            text(
                "INSERT INTO bloqueios (tipo, alcance, aplicativo_id, quem, motivo, encerrado_em, encerrado_por)"
                " VALUES ('pausa', 'aplicativo', :a, :q, 'contagem do passeio', now(), 'teste')"
            ),
            {"a": amb.aplicativo_id, "q": fila.QUEM_TETO},
        )
        await c.execute(
            text(
                "UPDATE limites SET situacao = 'encerrado', motivo = 'teste'"
                " WHERE pessoa_id = :p AND situacao = 'ativo'"
            ),
            {"p": amb.ana_id},
        )
    async with amb.entrada_db.begin() as c:
        await c.execute(
            text(
                "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                " valido_ate, com_passkey) VALUES (:p, :a, :s, 'enviar_mensagem',"
                " CAST(:d AS jsonb), 2, now() + interval '1 day', true)"
            ),
            {
                "p": amb.ana_id,
                "a": amb.aplicativo_id,
                "s": amb.sistema_id,
                "d": '[{"campo": "destinatario", "valores": ["equipe"]}]',
            },
        )
    await assumir_a_porta(m)  # tambem da como nao confirmado o que um passeio anterior deixou enviando
    await sem_sobras(amb)
    m.acoes.clear()
    m.bloqueios.clear()
    m.saida_aberta = True
    m.modo, m.atual = "normal", None
    m.chamadas.clear()
    m.violacoes.clear()
    m.nao_confirmadas.clear()
    m.base_do_sistema = m.sistema_contou()
    m.notas.append(f"passeio {tag}")


async def ler_saida(m: Mundo) -> bool:
    async with m.amb.admin.connect() as c:
        return (await estado.ler_saida(c)).aberta


async def estados_no_banco(m: Mundo) -> dict[uuid.UUID, tuple[Estado, str | None]]:
    if not m.acoes:
        return {}
    async with m.amb.admin.connect() as c:
        linhas = (
            await c.execute(
                text(
                    "SELECT o.id, f.estado, d.decisao FROM operacoes o JOIN fila_saida f ON f.operacao_id = o.id"
                    " LEFT JOIN decisoes_nao_confirmada d ON d.operacao_id = o.id WHERE o.id = ANY(:i)"
                ),
                {"i": list(m.acoes)},
            )
        ).all()
    return {uuid.UUID(str(x.id)): (Estado(x.estado), x.decisao) for x in linhas}


def argumentos_de(ferramenta: str, tag: str, n: int, destinatario: str) -> dict[str, Any]:
    if ferramenta == "criar_chamado":
        return {"titulo": f"Passeio {tag} {n}"}
    if ferramenta == "enviar_mensagem":
        return {"destinatario": destinatario, "texto": f"Passeio {tag} {n}"}
    return {"servico": f"servico {tag} {n}"}


def esperado_do_pedido_igual(m: Mundo, pessoa: str, ferramenta: str, argumentos: dict[str, Any]) -> Acao | None:
    """A regra do pedido igual: a mais nova igual que ainda pode sair, ja saiu, ou ficou em duvida."""
    iguais = [a for a in m.acoes.values() if (a.pessoa, a.ferramenta, a.argumentos) == (pessoa, ferramenta, argumentos)]
    for a in sorted(iguais, key=lambda x: x.op.criada_em, reverse=True):
        if a.estado in PENDENTES or a.estado == Estado.ENVIADA:
            return a
        if a.estado == Estado.NAO_CONFIRMADA and a.decisao != "nao_aconteceu":
            return a
    return None


def bloqueio_para(m: Mundo, pessoa_id: uuid.UUID) -> BloqueioAtivo | None:
    for b in m.bloqueios.values():
        alvo = b.b
        if (
            alvo.alcance == "tudo"
            or (alvo.alcance == "sistema" and alvo.sistema_id == m.amb.sistema_id)
            or (alvo.alcance == "aplicativo" and alvo.aplicativo_id == m.amb.aplicativo_id)
            or (alvo.alcance == "pessoa" and alvo.pessoa_id == pessoa_id)
            or (
                alvo.alcance == "pessoa_aplicativo"
                and (alvo.pessoa_id, alvo.aplicativo_id) == (pessoa_id, m.amb.aplicativo_id)
            )
        ):
            return b
    return None


async def pedir(m: Mundo, pessoa: str, ferramenta: str, argumentos: dict[str, Any], pela_entrada: bool) -> None:
    amb = m.amb
    pessoa_id = m.pessoas[pessoa]
    await atualizar(m)
    bloqueado = bloqueio_para(m, pessoa_id)
    original = esperado_do_pedido_igual(m, pessoa, ferramenta, argumentos)
    recusa: str | None = None
    op: Operacao | None = None
    repetida = False
    if pela_entrada:
        try:
            r = await chamar(amb, ferramenta, argumentos, sujeito=f"pessoa-{pessoa}")
        except MCPError as erro:
            dados = erro.error.data if isinstance(erro.error.data, dict) else {}
            recusa = str(dados.get(PREFIXO_META + "motivo"))
        else:
            meta = dict(r.meta or {})
            async with amb.admin.connect() as c:
                op_id = (
                    await c.execute(
                        text("SELECT id FROM operacoes WHERE protocolo = :p"), {"p": meta[PREFIXO_META + "protocolo"]}
                    )
                ).scalar_one()
                op = await fila.por_id(c, op_id)
            repetida = bool(meta.get(PREFIXO_META + "repetido"))
    else:
        pedido = Pedido(pessoa_id, m.aplicativo, m.ferramentas[(pessoa, ferramenta)], argumentos, None)
        try:
            op, repetida = await Pedidos(amb.p, amb.entrada_db).pedir(pedido)
        except AcaoRecusada as erro:
            recusa = erro.motivo
    m.notas.append(
        f"pedir {pessoa} {ferramenta} {argumentos} pela_entrada={pela_entrada}: recusa={recusa} repetida={repetida}"
    )

    m.resumo["pedido_recusado" if recusa else "pedido_repetido" if repetida else "pedido_novo"] += 1
    if bloqueado is not None:
        assert recusa is not None, f"bloqueio {bloqueado.b.alcance} ativo e o pedido passou"
        assert recusa != "saida_fechada", f"bloqueio {bloqueado.b.alcance} ativo: {recusa}"
        return
    if not m.saida_aberta:
        assert recusa == "saida_fechada", recusa
        return
    if recusa == "teto_confirmacoes":
        # Nao deve acontecer (a contagem recomeca a cada passeio), mas, se acontecer, a pausa vale como bloqueio.
        async with amb.admin.connect() as c:
            vigentes = await bloqueios.ativos(c, pessoa_id=pessoa_id, aplicativo_id=amb.aplicativo_id)
        assert vigentes.geral is not None
        m.bloqueios[vigentes.geral.id] = BloqueioAtivo(vigentes.geral)
        return
    assert recusa is None, f"recusa inesperada: {recusa}"
    assert op is not None
    if original is not None:
        assert repetida, f"pedido igual a {original.op.protocolo} ({original.estado}) virou {op.protocolo}"
        assert op.id == original.op.id, f"pedido igual devolveu {op.protocolo}, nao {original.op.protocolo}"
        return
    assert not repetida, f"{op.protocolo} veio como repetido sem original igual"
    assert op.id not in m.acoes
    risco_alto = ferramenta == "cancelar_servico"
    acao = Acao(op, pessoa, ferramenta, argumentos, risco_alto, estado=op.estado)
    if op.limite_id is not None:
        assert not risco_alto
        assert (pessoa, ferramenta, argumentos.get("destinatario")) == ("ana", "enviar_mensagem", "equipe")
        assert op.estado == Estado.LIBERADA
        acao.liberada_por = "limite"
    else:
        assert op.estado == Estado.AGUARDANDO_FICHA, op.estado
        assert op.ficha_exige_passkey == risco_alto
    m.acoes[op.id] = acao


async def decidir(m: Mundo, acao: Acao, passo: str, pessoa: str, com_passkey: bool, espera: int, decisao: str) -> None:
    amb = m.amb
    if passo in ("confirmar", "recusar") and acao.op.ficha_id is None:
        return  # liberada por limite: nao tem ficha
    await atualizar(m)
    pessoa_id = m.pessoas[pessoa]
    antes = acao.estado
    certa = pessoa == acao.pessoa
    try:
        async with amb.entrada_db.begin() as c:
            if passo == "confirmar":
                assert acao.op.ficha_id is not None
                await fila.confirmar_ficha(
                    c,
                    acao.op.ficha_id,
                    pessoa_id=pessoa_id,
                    com_passkey=com_passkey,
                    espera_segundos=espera,
                    rapida_segundos=0,
                )
            elif passo == "recusar":
                assert acao.op.ficha_id is not None
                await fila.recusar_ficha(c, acao.op.ficha_id, pessoa_id=pessoa_id)
            elif passo == "cancelar":
                await fila.cancelar(c, acao.op.id, pessoa_id=pessoa_id)
            elif passo == "seguir_agora":
                await fila.seguir_agora(c, acao.op.id, pessoa_id=pessoa_id)
            else:
                await fila.decidir_nao_confirmada(c, acao.op.id, pessoa_id=pessoa_id, decisao=decisao, observacao=None)
        passou = True
    except fila.DecisaoRecusada as erro:
        passou = False
        m.notas.append(f"{passo} {acao.op.protocolo} ({antes}) por {pessoa}: recusado {erro.motivo}")
    else:
        m.notas.append(f"{passo} {acao.op.protocolo} ({antes}) por {pessoa}: passou")
    deve = {
        "confirmar": certa and antes == Estado.AGUARDANDO_FICHA and (com_passkey or not acao.risco_alto),
        "recusar": certa and antes == Estado.AGUARDANDO_FICHA,
        "cancelar": certa and antes in ESPERANDO,
        "seguir_agora": certa and antes == Estado.LIBERADA,
        "decidir": certa and antes == Estado.NAO_CONFIRMADA and acao.decisao is None,
    }[passo]
    if passo == "recusar" and certa and antes != Estado.AGUARDANDO_FICHA and not acao.ficha_decidida:
        # Hoje, recusar a ficha de uma acao que ja parou (bloqueio, restauracao) grava a recusa na ficha e no
        # extrato, sem mudar a fila (defeito relatado ao nucleo). Vale conferir so que a fila nao muda.
        deve = passou
        m.resumo["recusa_de_ficha_parada"] += int(passou)
    assert passou == deve, f"{passo} {acao.op.protocolo} em {antes} por {pessoa}: passou={passou}, devia {deve}"
    if passou and passo in ("confirmar", "recusar", "cancelar") and acao.op.ficha_id is not None:
        acao.ficha_decidida = True
    m.resumo[f"{passo}_passou"] += int(passou)
    if passou and passo == "confirmar":
        acao.liberada_por = "ficha_com_passkey" if com_passkey else "ficha"
    if passou and passo == "recusar" and antes != Estado.AGUARDANDO_FICHA:
        await atualizar(m)
        assert acao.estado == antes, f"{acao.op.protocolo}: recusar a ficha mudou a fila de {antes} para {acao.estado}"


async def enviar(m: Mundo, modo: str) -> None:
    if m.porta is None or m.dono is None:
        m.notas.append("enviar: a porta caiu e ainda nao voltou")
        return
    m.modo = modo
    try:
        for _ in range(4):
            if not await m.porta.enviar_uma(m.dono):
                break
    except Queda:
        m.notas.append(f"enviar: a porta caiu ({modo})")
        await soltar_a_porta(m)
    finally:
        m.modo = "normal"


async def bloquear(m: Mundo, alcance: str, pessoa: str) -> None:
    amb = m.amb
    campos: dict[str, uuid.UUID] = {
        "pessoa": {"pessoa_id": m.pessoas[pessoa]},
        "aplicativo": {"aplicativo_id": amb.aplicativo_id},
        "pessoa_aplicativo": {"pessoa_id": m.pessoas[pessoa], "aplicativo_id": amb.aplicativo_id},
        "sistema": {"sistema_id": amb.sistema_id},
        "tudo": {},
    }[alcance]
    await atualizar(m)
    async with amb.entrada_db.begin() as c:
        b = await bloqueios.bloquear(c, "bloqueio", alcance, bloqueios.quem_admin(amb.admin_id), "passeio", **campos)
    ativo = BloqueioAtivo(b)
    m.bloqueios[b.id] = ativo
    m.notas.append(f"bloquear {alcance} {pessoa}")
    antes = {i: a.estado for i, a in m.acoes.items()}
    await atualizar(m)
    for i, a in m.acoes.items():
        if ativo.vale_para(a.op) and antes[i] in ESPERANDO:
            assert a.estado == Estado.PARADA_POR_BLOQUEIO, f"{a.op.protocolo}: {antes[i]} -> {a.estado}"
        else:
            assert a.estado == antes[i], f"{a.op.protocolo}: {antes[i]} -> {a.estado} fora do bloqueio"


async def encerrar(m: Mundo, bloqueio_id: uuid.UUID) -> None:
    async with m.amb.entrada_db.begin() as c:
        await bloqueios.encerrar(c, bloqueio_id, bloqueios.quem_admin(m.amb.admin_id))
    m.notas.append(f"encerrar bloqueio {m.bloqueios[bloqueio_id].b.alcance}")
    del m.bloqueios[bloqueio_id]


async def restaurar(m: Mundo) -> None:
    """Como a conferencia de continuidade que nao confere: fecha a saida e para a fila, na mesma transacao."""
    await atualizar(m)
    antes = {i: a.estado for i, a in m.acoes.items()}
    com_dono = m.dono is not None
    async with m.amb.despachante_db.begin() as c:
        await estado.mudar_saida(c, aberta=False, quem=continuidade.QUEM, motivo="passeio", papel="despachante")
        await fila.parar_por_restauracao(c, papel="despachante")
    m.saida_aberta = False
    m.notas.append(f"restauracao (porta viva: {com_dono})")
    await atualizar(m)
    for i, a in m.acoes.items():
        if antes[i] in ESPERANDO:
            assert a.estado == Estado.PARADA_POR_RESTAURACAO, f"{a.op.protocolo}: {antes[i]} -> {a.estado}"
        elif antes[i] == Estado.ENVIANDO and not com_dono:
            assert a.estado == Estado.NAO_CONFIRMADA, f"{a.op.protocolo}: enviando sem dono -> {a.estado}"
        else:
            assert a.estado == antes[i], f"{a.op.protocolo}: {antes[i]} -> {a.estado}"


async def fechar_saida(m: Mundo) -> None:
    """O ADMIN fecha a porta de saida (sem parar a fila): o que esta liberado espera a reabertura."""
    await atualizar(m)
    antes = {i: a.estado for i, a in m.acoes.items()}
    async with m.amb.entrada_db.begin() as c:
        await estado.mudar_saida(
            c, aberta=False, quem=bloqueios.quem_admin(m.amb.admin_id), motivo="passeio", papel="entrada"
        )
    m.saida_aberta = False
    m.notas.append("o ADMIN fecha a saida")
    await atualizar(m)
    assert {i: a.estado for i, a in m.acoes.items()} == antes, "fechar a saida nao muda a fila"


async def reabrir(m: Mundo) -> None:
    await continuidade.reabrir_saida(m.amb.entrada_db, f"admin:{m.amb.admin_id}", "Conferido no passeio")
    m.saida_aberta = True
    m.notas.append("reabrir a saida")


async def reiniciar(m: Mundo) -> None:
    await atualizar(m)
    antes = {i: a.estado for i, a in m.acoes.items()}
    retomadas = await assumir_a_porta(m)
    m.notas.append(f"reiniciar o despachante: {len(retomadas)} retomadas")
    await atualizar(m)
    for i, a in m.acoes.items():
        if antes[i] == Estado.ENVIANDO:
            assert a.estado == Estado.NAO_CONFIRMADA, f"{a.op.protocolo}: enviando -> {a.estado} na retomada"


async def atualizar(m: Mundo) -> None:
    """Le o estado de cada acao do passeio e confere as transicoes."""
    no_banco = await estados_no_banco(m)
    assert set(no_banco) == set(m.acoes), "nenhuma acao some"
    for i, a in m.acoes.items():
        novo, decisao = no_banco[i]
        if a.estado in FINAIS:
            assert novo == a.estado, f"{a.op.protocolo}: {a.estado} e final, mas virou {novo}"
        assert novo in ALCANCAVEIS[a.estado], f"{a.op.protocolo}: {a.estado} -> {novo} fora da tabela"
        a.estado, a.decisao = novo, decisao
        if novo == Estado.NAO_CONFIRMADA:
            m.nao_confirmadas.add(i)


async def conferir(m: Mundo) -> None:
    await atualizar(m)
    assert m.violacoes == [], m.violacoes
    assert all(n <= 1 for n in m.chamadas.values()), m.chamadas
    assert m.sistema_contou() - m.base_do_sistema == sum(m.chamadas.values()), "o sistema contou chamada de fora"
    for a in m.acoes.values():
        n = m.chamadas[a.op.id]
        if a.estado == Estado.ENVIADA:
            assert n == 1, f"{a.op.protocolo}: enviada com {n} chamadas"
        if a.estado in ESPERANDO or a.estado in (
            Estado.CANCELADA,
            Estado.VENCIDA,
            Estado.PARADA_POR_BLOQUEIO,
            Estado.PARADA_POR_RESTAURACAO,
        ):
            assert n == 0, f"{a.op.protocolo}: {a.estado} com {n} chamadas"
        if a.estado in ESPERANDO:
            for b in m.bloqueios.values():
                assert not b.vale_para(a.op), f"{a.op.protocolo}: {a.estado} dentro do bloqueio {b.b.alcance}"


# --- Maquina ------------------------------------------------------------------------------------


PASSOS_SORTEADOS: Final = (
    # Peso de cada passo: os que fazem a acao andar aparecem mais; os que param tudo, menos. Encerrar o bloqueio
    # e reabrir a saida (que nada fazem quando nao ha o que desfazer) aparecem mais, para o passeio seguir.
    *["pedir"] * 5,
    *["confirmar"] * 5,
    *["enviar"] * 5,
    *["seguir_agora"] * 2,
    "cancelar",
    "recusar",
    "decidir",
    "perder_resposta",
    "cair_depois_de_chamar",
    "cair_antes_de_chamar",
    "bloquear",
    *["encerrar_bloqueio"] * 3,
    "reiniciar",
    "restaurar",
    "fechar_saida",
    *["reabrir"] * 3,
)
ALVO_DO_PASSO: Final = {
    "confirmar": ESPERANDO - {Estado.LIBERADA},
    "recusar": ESPERANDO - {Estado.LIBERADA},
    "cancelar": ESPERANDO,
    "seguir_agora": frozenset({Estado.LIBERADA}),
    "decidir": frozenset({Estado.NAO_CONFIRMADA}),
}
"""Estados em que o passo costuma valer: a acao e sorteada entre elas na maior parte das vezes."""


def _maquina(rodar: Callable[[Coroutine[Any, Any, Any]], Any], m: Mundo) -> type[RuleBasedStateMachine]:
    class PasseioDaPorta(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.tag = uuid.uuid4().hex[:6]
            rodar(preparar(m, self.tag))

        def teardown(self) -> None:
            for a in m.acoes.values():
                m.resumo[str(a.estado)] += 1
            m.resumo["chamadas"] += sum(m.chamadas.values())
            rodar(soltar_a_porta(m))

        def sortear_acao(self, dados: st.DataObject, passo: str) -> Acao | None:
            # Ordem de criacao (a do dicionario): o mesmo sorteio escolhe a mesma acao quando o hypothesis repete.
            todas = list(m.acoes.values())
            if not todas:
                return None
            alvo = [a for a in todas if a.estado in ALVO_DO_PASSO[passo]]
            # Quase sempre uma acao em que o passo vale; as vezes qualquer uma (a recusa tambem e conferida).
            if alvo and dados.draw(st.integers(0, 4), label="no alvo") != 0:
                return dados.draw(st.sampled_from(alvo), label="acao")
            return dados.draw(st.sampled_from(todas), label="acao")

        @rule(dados=st.data(), passo=st.sampled_from(PASSOS_SORTEADOS))
        def um_passo(self, dados: st.DataObject, passo: str) -> None:
            m.resumo[passo] += 1
            if passo == "pedir":
                pessoa = dados.draw(st.sampled_from(["ana", "bia"]), label="pessoa")
                ferramenta = dados.draw(st.sampled_from(FERRAMENTAS), label="ferramenta")
                n = dados.draw(st.integers(0, 5), label="n")
                destinatario = dados.draw(st.sampled_from(["equipe", "outro"]), label="destinatario")
                pela_entrada = dados.draw(st.integers(0, 3), label="pela entrada") == 0
                argumentos = argumentos_de(ferramenta, self.tag, n, destinatario)
                rodar(pedir(m, pessoa, ferramenta, argumentos, pela_entrada))
            elif passo in ALVO_DO_PASSO:
                acao = self.sortear_acao(dados, passo)
                if acao is None:
                    return
                mesma = dados.draw(st.integers(0, 5), label="mesma pessoa") != 0
                pessoa = acao.pessoa if mesma else ("bia" if acao.pessoa == "ana" else "ana")
                com_passkey = dados.draw(st.booleans(), label="com passkey")
                espera = dados.draw(st.sampled_from([0, 0, 0, ESPERA_LONGA]), label="espera")
                decisao = dados.draw(st.sampled_from(["aconteceu", "nao_aconteceu", "nao_sei"]), label="decisao")
                rodar(decidir(m, acao, passo, pessoa, com_passkey, espera, decisao))
            elif passo == "enviar":
                rodar(enviar(m, "normal"))
            elif passo == "perder_resposta":
                rodar(enviar(m, "perde"))
            elif passo == "cair_depois_de_chamar":
                rodar(enviar(m, "cai_depois"))
            elif passo == "cair_antes_de_chamar":
                rodar(enviar(m, "cai_antes"))
            elif passo == "bloquear":
                alcance = dados.draw(st.sampled_from(ALCANCES), label="alcance")
                pessoa = dados.draw(st.sampled_from(["ana", "bia"]), label="pessoa")
                rodar(bloquear(m, alcance, pessoa))
            elif passo == "encerrar_bloqueio":
                if m.bloqueios:
                    ids = list(m.bloqueios)  # ordem de criacao
                    rodar(encerrar(m, dados.draw(st.sampled_from(ids), label="bloqueio")))
            elif passo == "reiniciar":
                rodar(reiniciar(m))
            elif passo == "restaurar":
                rodar(restaurar(m))
            elif passo == "fechar_saida":
                if m.saida_aberta:
                    rodar(fechar_saida(m))
            elif not m.saida_aberta:
                rodar(reabrir(m))

        @invariant()
        def regras(self) -> None:
            rodar(conferir(m))

    return PasseioDaPorta


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    # Resposta rapida ao aplicativo: aqui a porta so anda nos passos "enviar".
    async with ambiente_sem_porta(banco_migrado, monkeypatch, extra={"RESPONSE_MAX_WAIT_SECONDS": 1}) as a:
        yield a


async def test_sequencias_sorteadas_com_as_operacoes_reais(amb: Ambiente) -> None:
    async with amb.entrada_db.connect() as c:
        aplicativo = await repositorio.aplicativo_por_client_id(c, "claude-code")
        assert aplicativo is not None
        ferramentas = {}
        for pessoa, pessoa_id in (("ana", amb.ana_id), ("bia", amb.bia_id)):
            for original in FERRAMENTAS:
                f = await repositorio.ferramenta_na_vitrine(
                    c, pessoa_id, amb.aplicativo_id, amb.vitrine[original], VERSAO_MCP
                )
                assert f is not None
                ferramentas[(pessoa, original)] = f
    m = Mundo(amb, aplicativo, ferramentas, {"ana": amb.ana_id, "bia": amb.bia_id})
    # Os nomes da vitrine: ``chamar`` (pela entrada) recebe o nome original e acha o da vitrine.
    laco = asyncio.get_running_loop()

    def rodar(coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return asyncio.run_coroutine_threadsafe(coro, laco).result(timeout=120)
        finally:
            for nota in m.notas:
                note(nota)
            m.notas.clear()

    try:
        await asyncio.to_thread(
            run_state_machine_as_test,
            _maquina(rodar, m),
            settings=settings(
                max_examples=EXEMPLOS,
                stateful_step_count=PASSOS,
                deadline=None,
                database=None,
                suppress_health_check=list(HealthCheck),
            ),
        )
        print(f"\nresumo dos passeios: {dict(sorted(m.resumo.items()))}")
        # O passeio nao ficou vazio: houve pedidos novos e fichas confirmadas (e, nos longos, chamadas ao sistema).
        assert m.resumo["pedido_novo"] > 0
        assert m.resumo["confirmar_passou"] > 0
        if LONGO:
            assert m.resumo["chamadas"] > 0
    finally:
        await soltar_a_porta(m)
        if not await ler_saida(m):
            await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Fim dos passeios")
