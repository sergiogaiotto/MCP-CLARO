"""Maquina de estados da porta de saida contra o PostgreSQL real, com o usuario de cada papel.

- a tabela ``transicoes_saida`` do banco e a mesma de ``estados.TRANSICOES``;
- em passeios sorteados (``hypothesis``, modo de estados), cada mudanca tentada direto no banco passa se e
  somente se ``estados.pode(de, para)`` e o papel pode mexer naquele estado; nenhuma linha some e a
  operacao de uma linha nunca muda;
- o gatilho de nascimento so aceita "esperando a ficha" (com ficha) ou "liberada" (com limite);
- o gatilho de guarda recusa a entrada e as rotinas mexendo em "enviando" e "parada por restauracao".

O dono do banco (usuario das migracoes) passa pelo gatilho de transicoes, mas nao pela regra de papel.
``MCP_CLARO_TESTE_LONGO=1`` roda os passeios longos (todo dia, no plano de testes).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import acoes_caixa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.saida import estados
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.protocolo import codigo_conferencia, novo_protocolo
from tests.integracao.test_autorizacao import engine_do_papel

pytestmark = [pytest.mark.integracao]

DONO: Final = "dono"
ENTRADA: Final = "mcp_claro_entrada"
DESPACHANTE: Final = "mcp_claro_despachante"
ROTINAS: Final = "mcp_claro_rotinas"
PAPEIS: Final = (DONO, ENTRADA, DESPACHANTE, ROTINAS)
PODEM_MARCAR_HORA: Final = {DONO, ENTRADA}
"""Quem tem UPDATE em ``hora_saida`` (a linha liberada precisa da hora de saida)."""

RECUSA_CHECK: Final = "23514"
SEM_PERMISSAO: Final = "42501"
SO_DO_DESPACHANTE: Final = frozenset({Estado.ENVIANDO, Estado.PARADA_POR_RESTAURACAO})

LONGO = os.environ.get("MCP_CLARO_TESTE_LONGO") == "1"


def esperado(de: Estado, para: Estado, papel: str, tem_hora: bool) -> str | None:
    """O que o banco deve responder: ``None`` (passa) ou o SQLSTATE da recusa."""
    if not estados.pode(de, para):
        return RECUSA_CHECK
    if papel in (ENTRADA, ROTINAS) and (para in SO_DO_DESPACHANTE or de == Estado.ENVIANDO):
        return SEM_PERMISSAO
    if para == Estado.LIBERADA and not tem_hora and papel not in PODEM_MARCAR_HORA:
        return RECUSA_CHECK  # liberada sem hora de saida: restricao da tabela
    return None


def sqlstate(erro: DBAPIError) -> str | None:
    return getattr(erro.orig, "sqlstate", None)


# --- Arranjo ---------------------------------------------------------------------------------


@dataclass
class Mundo:
    p: Parametros
    engines: dict[str, AsyncEngine]
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    sistema_id: uuid.UUID
    ferramenta_id: uuid.UUID
    ordem_id: uuid.UUID
    limite_id: uuid.UUID
    sobra_id: uuid.UUID
    """Operacao sem linha na fila, para tentar trocar a operacao de uma linha."""
    criadas: list[int] = field(default_factory=list)

    @property
    def dono(self) -> AsyncEngine:
        return self.engines[DONO]

    async def fechar(self) -> None:
        for e in self.engines.values():
            await e.dispose()


async def criar_mundo(p: Parametros) -> Mundo:
    engines = {DONO: criar_engine(p, pool_size=2, max_overflow=2)}
    for papel in (ENTRADA, DESPACHANTE, ROTINAS):
        engines[papel] = engine_do_papel(p, papel)
    sufixo = uuid.uuid4().hex[:8]
    async with engines[DONO].begin() as c:
        aplicativo_id = await repositorio.habilitar_aplicativo(
            c, client_id=f"estados-{sufixo}", nome=f"Estados {sufixo}", familia="teste"
        )
        pessoa = await repositorio.pessoa_no_login(c, "https://login.teste", f"estados-{sufixo}", None)
        await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": pessoa.id})
        sistema_id = await repositorio.cadastrar_sistema(
            c, nome=f"estados-{sufixo}", endereco="http://127.0.0.1:9/mcp", tipo_chave="por_pessoa"
        )
        ferramenta_id = await repositorio.revisar_ferramenta(
            c,
            sistema_id=sistema_id,
            definicao_declarada=acoes_caixa.FERRAMENTAS[0],
            nome_vitrine=f"chamado_{sufixo}",
            descricao="Acao de teste.",
            classe="muda",
            revisado_por="teste",
        )
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"estados-{sufixo}",
            descricao="Ordem de teste",
            ferramentas=[(sistema_id, "criar_chamado")],
            validade_dias=30,
            criado_por="teste",
        )
        ordem_id = await repositorio.conceder_ordem(
            c, pessoa_id=pessoa.id, aplicativo_id=aplicativo_id, modelo_id=modelo, dias=7, max_dias=30
        )
        limite_id = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate,"
                    " com_passkey) VALUES (:p, :a, :s, 'criar_chamado', 5, now() + interval '1 day', true)"
                    " RETURNING id"
                ),
                {"p": pessoa.id, "a": aplicativo_id, "s": sistema_id},
            )
        ).scalar_one()
    mundo = Mundo(
        p,
        engines,
        pessoa.id,
        aplicativo_id,
        sistema_id,
        ferramenta_id,
        ordem_id,
        uuid.UUID(str(limite_id)),
        uuid.uuid4(),
    )
    mundo.sobra_id = await nova_operacao(mundo)
    return mundo


async def nova_operacao(m: Mundo) -> uuid.UUID:
    argumentos = {"titulo": f"estado {uuid.uuid4().hex[:8]}"}
    async with m.dono.begin() as c:
        protocolo = await novo_protocolo(c)
        valor = (
            await c.execute(
                text(
                    "INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
                    " ferramenta_id, sistema_id, argumentos, chave_pedido_igual)"
                    " VALUES (:pr, :co, :pe, :a, :o, :f, :s, CAST(:ar AS jsonb), :k) RETURNING id"
                ),
                {
                    "pr": protocolo,
                    "co": codigo_conferencia(m.sistema_id, "criar_chamado", argumentos),
                    "pe": m.pessoa_id,
                    "a": m.aplicativo_id,
                    "o": m.ordem_id,
                    "f": m.ferramenta_id,
                    "s": m.sistema_id,
                    "ar": json.dumps(argumentos),
                    "k": uuid.uuid4().hex,
                },
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def nova_ficha(m: Mundo, operacao_id: uuid.UUID) -> uuid.UUID:
    async with m.dono.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco)"
                    " SELECT id, codigo_conferencia, now() + interval '1 day', 'medio' FROM operacoes WHERE id = :o"
                    " RETURNING id"
                ),
                {"o": operacao_id},
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def inserir(
    m: Mundo,
    papel: str,
    operacao_id: uuid.UUID,
    estado: Estado,
    ficha_id: uuid.UUID | None,
    limite_id: uuid.UUID | None,
    *,
    hora: bool = True,
) -> tuple[int | None, str | None]:
    """Tenta criar a linha na fila. Devolve (id, None) ou (None, SQLSTATE da recusa).

    ``hora`` poe a hora de saida (amanha, para nenhum despachante pegar a linha)."""
    try:
        async with m.engines[papel].begin() as c:
            valor = (
                await c.execute(
                    text(
                        "INSERT INTO fila_saida (operacao_id, estado, ficha_id, limite_id, hora_saida, mudou_por)"
                        " VALUES (:o, :e, :f, :l, CASE WHEN :h THEN now() + interval '1 day' END, 'teste')"
                        " RETURNING id"
                    ),
                    {"o": operacao_id, "e": str(estado), "f": ficha_id, "l": limite_id, "h": hora},
                )
            ).scalar_one()
    except DBAPIError as erro:
        return None, sqlstate(erro)
    m.criadas.append(int(valor))
    return int(valor), None


async def nascer(m: Mundo, via: str) -> tuple[int, uuid.UUID]:
    """Uma linha nova, como a entrada cria: esperando a ficha, ou liberada por um limite ja gasto."""
    op = await nova_operacao(m)
    if via == "ficha":
        ficha = await nova_ficha(m, op)
        linha, erro = await inserir(m, ENTRADA, op, Estado.AGUARDANDO_FICHA, ficha, None, hora=False)
    else:
        linha, erro = await inserir(m, ENTRADA, op, Estado.LIBERADA, None, m.limite_id)
    assert erro is None, erro
    assert linha is not None
    return linha, op


async def mudar(m: Mundo, papel: str, linha: int, para: Estado) -> str | None:
    """Uma mudanca direto no banco, como o papel faria. Liberar marca a hora de saida quando o papel pode."""
    if para == Estado.LIBERADA and papel in PODEM_MARCAR_HORA:
        sql = (
            "UPDATE fila_saida SET estado = :e, mudou_por = 'teste',"
            " hora_saida = coalesce(hora_saida, now() + interval '1 day')"
        )
    else:
        sql = "UPDATE fila_saida SET estado = :e, mudou_por = 'teste'"
    try:
        async with m.engines[papel].begin() as c:
            r = await c.execute(text(sql + " WHERE id = :i"), {"e": str(para), "i": linha})
            assert r.rowcount == 1
    except DBAPIError as erro:
        return sqlstate(erro)
    return None


async def apagar(m: Mundo, papel: str, linha: int) -> str | None:
    try:
        async with m.engines[papel].begin() as c:
            await c.execute(text("DELETE FROM fila_saida WHERE id = :i"), {"i": linha})
    except DBAPIError as erro:
        return sqlstate(erro)
    return None


async def trocar_operacao(m: Mundo, papel: str, linha: int) -> str | None:
    try:
        async with m.engines[papel].begin() as c:
            await c.execute(text("UPDATE fila_saida SET operacao_id = :o WHERE id = :i"), {"o": m.sobra_id, "i": linha})
    except DBAPIError as erro:
        return sqlstate(erro)
    return None


async def no_banco(m: Mundo, linhas: list[int]) -> dict[int, tuple[Estado, uuid.UUID, bool]]:
    async with m.dono.connect() as c:
        achadas = (
            await c.execute(
                text(
                    "SELECT id, estado, operacao_id, hora_saida IS NOT NULL AS hora FROM fila_saida WHERE id = ANY(:i)"
                ),
                {"i": linhas},
            )
        ).all()
    return {int(r.id): (Estado(r.estado), uuid.UUID(str(r.operacao_id)), bool(r.hora)) for r in achadas}


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    m = await criar_mundo(banco_migrado)
    try:
        yield m
    finally:
        await m.fechar()


# --- Tabela de transicoes ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_tabela_do_banco_e_a_do_modulo(mundo: Mundo) -> None:
    for papel in (DONO, ENTRADA, DESPACHANTE, ROTINAS):
        async with mundo.engines[papel].connect() as c:
            no_banco = {
                (Estado(r.de), Estado(r.para)) for r in await c.execute(text("SELECT de, para FROM transicoes_saida"))
            }
        assert no_banco == set(estados.TRANSICOES), papel
    assert not any(de in estados.FINAIS for de, _ in estados.TRANSICOES), "de um estado final nada sai"
    assert {de for de, _ in estados.TRANSICOES} == set(Estado) - estados.FINAIS


# --- Passeios sorteados (hypothesis, modo de estados) -----------------------------------------


def test_passeios_sorteados_seguem_a_tabela(banco_migrado: Parametros) -> None:
    with asyncio.Runner() as runner:
        m = runner.run(criar_mundo(banco_migrado))
        try:
            run_state_machine_as_test(
                _maquina(runner, m),
                settings=settings(
                    max_examples=300 if LONGO else 40,
                    stateful_step_count=60 if LONGO else 30,
                    deadline=None,
                    database=None,
                    suppress_health_check=list(HealthCheck),
                ),
            )
        finally:
            runner.run(m.fechar())


def _maquina(runner: asyncio.Runner, m: Mundo) -> type[RuleBasedStateMachine]:
    def rodar(coro: Any) -> Any:
        return runner.run(coro)

    class MaquinaDaFila(RuleBasedStateMachine):
        linhas = Bundle("linhas")

        def __init__(self) -> None:
            super().__init__()
            self.modelo: dict[int, tuple[Estado, uuid.UUID, bool]] = {}

        @rule(target=linhas, via=st.sampled_from(["ficha", "limite"]))
        def nasce(self, via: str) -> int:
            linha, op = rodar(nascer(m, via))
            self.modelo[linha] = (Estado.AGUARDANDO_FICHA if via == "ficha" else Estado.LIBERADA, op, via == "limite")
            note(f"nasce {linha} por {via}")
            return linha

        @rule(linha=linhas, papel=st.sampled_from(PAPEIS), dados=st.data())
        def muda(self, linha: int, papel: str, dados: st.DataObject) -> None:
            de, op, tem_hora = self.modelo[linha]
            proximos = sorted(para for (d, para) in estados.TRANSICOES if d == de)
            if proximos and dados.draw(st.booleans(), label="pela tabela"):
                para = dados.draw(st.sampled_from(proximos), label="para")
            else:
                para = dados.draw(st.sampled_from([e for e in Estado if e != de]), label="para")
            quer = esperado(de, para, papel, tem_hora)
            veio = rodar(mudar(m, papel, linha, para))
            note(f"{papel}: {linha} {de} -> {para}: esperado {quer}, veio {veio}")
            assert veio == quer, f"{papel}: {de} -> {para}: esperado {quer}, o banco respondeu {veio}"
            if veio is None:
                self.modelo[linha] = (para, op, tem_hora or (para == Estado.LIBERADA and papel in PODEM_MARCAR_HORA))

        @rule(linha=linhas, papel=st.sampled_from(PAPEIS))
        def apaga(self, linha: int, papel: str) -> None:
            assert rodar(apagar(m, papel, linha)) == SEM_PERMISSAO

        @rule(linha=linhas, papel=st.sampled_from((DONO, ENTRADA)))
        def troca_a_operacao(self, linha: int, papel: str) -> None:
            # O dono esbarra no gatilho de guarda; a entrada nem tem permissao na coluna.
            assert rodar(trocar_operacao(m, papel, linha)) == (RECUSA_CHECK if papel == DONO else SEM_PERMISSAO)

        @invariant()
        def banco_igual_ao_modelo(self) -> None:
            if not self.modelo:
                return
            achadas = rodar(no_banco(m, list(self.modelo)))
            assert set(achadas) == set(self.modelo), "nenhuma linha some"
            for linha, (estado, op, _) in self.modelo.items():
                assert achadas[linha][:2] == (estado, op), linha

    return MaquinaDaFila


# --- Nascimento --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_linha_so_nasce_esperando_a_ficha_ou_liberada_por_limite(mundo: Mundo) -> None:
    erradas = []
    for estado, com_ficha, com_limite, papel in itertools.product(
        Estado, (True, False), (True, False), (DONO, ENTRADA)
    ):
        op = await nova_operacao(mundo)
        ficha = await nova_ficha(mundo, op) if com_ficha else None
        _, erro = await inserir(mundo, papel, op, estado, ficha, mundo.limite_id if com_limite else None)
        valida = (estado == Estado.AGUARDANDO_FICHA and com_ficha and not com_limite) or (
            estado == Estado.LIBERADA and com_limite and not com_ficha
        )
        if (erro is None) != valida or (erro is not None and erro != RECUSA_CHECK):
            erradas.append((papel, estado, com_ficha, com_limite, erro))
    assert erradas == []


@pytest.mark.anyio
async def test_linha_nao_nasce_com_ficha_de_outra_linha(mundo: Mundo) -> None:
    primeira = await nova_operacao(mundo)
    ficha = await nova_ficha(mundo, primeira)
    _, erro = await inserir(mundo, ENTRADA, primeira, Estado.AGUARDANDO_FICHA, ficha, None)
    assert erro is None
    segunda = await nova_operacao(mundo)
    _, erro = await inserir(mundo, ENTRADA, segunda, Estado.AGUARDANDO_FICHA, ficha, None)
    assert erro == "23505"  # cada ficha libera uma linha so
    _, erro = await inserir(mundo, ENTRADA, primeira, Estado.LIBERADA, None, mundo.limite_id)
    assert erro == "23505"  # uma linha por operacao


# --- Guarda dos papeis -------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("papel", [ENTRADA, ROTINAS])
async def test_guarda_recusa_entrada_e_rotinas_em_enviando_e_restauracao(mundo: Mundo, papel: str) -> None:
    liberada, _ = await nascer(mundo, "limite")
    esperando, _ = await nascer(mundo, "ficha")
    assert await mudar(mundo, papel, liberada, Estado.ENVIANDO) == SEM_PERMISSAO
    assert await mudar(mundo, papel, liberada, Estado.PARADA_POR_RESTAURACAO) == SEM_PERMISSAO
    assert await mudar(mundo, papel, esperando, Estado.PARADA_POR_RESTAURACAO) == SEM_PERMISSAO

    enviando, _ = await nascer(mundo, "limite")
    assert await mudar(mundo, DESPACHANTE, enviando, Estado.ENVIANDO) is None
    for fim in (Estado.ENVIADA, Estado.NAO_SAIU, Estado.SISTEMA_PEDIU_DADOS, Estado.NAO_CONFIRMADA):
        assert await mudar(mundo, papel, enviando, fim) == SEM_PERMISSAO, fim

    # Nada mudou: as tres linhas seguem onde estavam.
    achadas = await no_banco(mundo, [liberada, esperando, enviando])
    assert [achadas[i][0] for i in (liberada, esperando, enviando)] == [
        Estado.LIBERADA,
        Estado.AGUARDANDO_FICHA,
        Estado.ENVIANDO,
    ]

    # O que nao toca nesses estados continua permitido ao papel (cancelar, parar por bloqueio).
    assert await mudar(mundo, papel, liberada, Estado.PARADA_POR_BLOQUEIO) is None
    assert await mudar(mundo, papel, esperando, Estado.CANCELADA) is None


@pytest.mark.anyio
async def test_despachante_entra_e_sai_de_enviando_e_para_por_restauracao(mundo: Mundo) -> None:
    for fim in (Estado.ENVIADA, Estado.NAO_SAIU, Estado.SISTEMA_PEDIU_DADOS, Estado.NAO_CONFIRMADA):
        linha, _ = await nascer(mundo, "limite")
        assert await mudar(mundo, DESPACHANTE, linha, Estado.ENVIANDO) is None
        assert await mudar(mundo, DESPACHANTE, linha, fim) is None
        # E de um fim nada mais sai, nem para o despachante.
        for outro in Estado:
            if outro != fim:
                assert await mudar(mundo, DESPACHANTE, linha, outro) == RECUSA_CHECK, (fim, outro)
    for via in ("ficha", "limite"):
        linha, _ = await nascer(mundo, via)
        assert await mudar(mundo, DESPACHANTE, linha, Estado.PARADA_POR_RESTAURACAO) is None


@pytest.mark.anyio
async def test_nenhuma_linha_some(mundo: Mundo) -> None:
    linhas = [(await nascer(mundo, via))[0] for via in ("ficha", "limite")]
    for papel in PAPEIS:
        for linha in linhas:
            assert await apagar(mundo, papel, linha) == SEM_PERMISSAO, papel
    assert set(await no_banco(mundo, linhas)) == set(linhas)
