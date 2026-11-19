"""Tarefas das rotinas contra PostgreSQL real, rodando com o usuario ``mcp_claro_rotinas``.

Vencimento e inatividade das ordens, aviso antes de vencer, fechamento encadeado,
particoes, resumo e guarda das medicoes, limpeza, resumo semanal e o agendador
(trava consultiva, isolamento e prazo).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.rotinas.agendador import Agendador, Desfecho, Tarefa
from mcp_claro.rotinas.fechamento import fechar_agora, fechar_se_devido
from mcp_claro.rotinas.limpeza import limpar_sessoes
from mcp_claro.rotinas.medicoes import garantir_particoes, resumir_medicoes
from mcp_claro.rotinas.ordens import avisar_ordens_a_vencer, vencer_ordens
from mcp_claro.rotinas.politica import valor_politica
from mcp_claro.rotinas.resumo_semanal import resumo_semanal, semana_anterior
from tests.integracao import test_rotinas_apoio as apoio
from tests.integracao.test_rotinas_apoio import (
    cadastro_minimo,
    ordem,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

# Fixtures compartilhadas (definidas em test_rotinas_apoio).
engine_dono = apoio.engine_dono
engine_rotinas = apoio.engine_rotinas
parametros_rotinas = apoio.parametros_rotinas


async def _situacao(engine: AsyncEngine, ordem_id: uuid.UUID) -> str:
    async with engine.connect() as c:
        return str(
            (await c.execute(text("SELECT situacao FROM ordens_servico WHERE id = :o"), {"o": ordem_id})).scalar_one()
        )


async def _contar(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as c:
        return int((await c.execute(text(sql), params)).scalar_one())


# --- Ordens de servico -----------------------------------------------------------------------


async def test_vence_e_inativa_ordens_com_extrato_e_aviso(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine
) -> None:
    cad = await cadastro_minimo(engine_dono, email="dona@exemplo.teste")
    vencida = await ordem(engine_dono, cad, inicio="-10 days", fim="-1 hour")
    parada = await ordem(engine_dono, cad, inicio="-20 days", fim="10 days", ultimo_uso="-15 days")
    nunca_usada = await ordem(engine_dono, cad, inicio="-15 days", fim="10 days")
    em_uso = await ordem(engine_dono, cad, inicio="-20 days", fim="10 days", ultimo_uso="-1 day")

    contagens = await vencer_ordens(engine_rotinas)
    assert contagens["vencidas"] >= 1
    assert contagens["inativas"] >= 2
    assert await _situacao(engine_rotinas, vencida) == "vencida"
    assert await _situacao(engine_rotinas, parada) == "inativa"
    assert await _situacao(engine_rotinas, nunca_usada) == "inativa"
    assert await _situacao(engine_rotinas, em_uso) == "ativa"

    async with engine_rotinas.connect() as c:
        linha = (
            await c.execute(
                text(
                    "SELECT frase, pessoa_id, aplicativo_id, papel FROM extrato"
                    " WHERE tipo = 'ordem_vencida' AND dados ->> 'ordem_id' = :o"
                ),
                {"o": str(vencida)},
            )
        ).one()
        assert linha.frase == f"A ordem de servico modelo-{cad.sufixo} chegou ao fim do prazo e deixou de valer."
        assert linha.pessoa_id == cad.pessoa_id
        assert linha.aplicativo_id == cad.aplicativo_id
        assert linha.papel == "rotinas"
        inativa = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE tipo = 'ordem_inativa' AND dados ->> 'ordem_id' = :o"),
                {"o": str(parada)},
            )
        ).scalar_one()
        assert "14 dias sem uso" in inativa
        aviso = (
            await c.execute(
                text("SELECT destino, evento, corpo FROM avisos WHERE chave_unica = :k"),
                {"k": f"ordem_vencida:{vencida}"},
            )
        ).one()
        assert aviso.destino == f"pessoa:{cad.pessoa_id}"
        assert aviso.evento == "ordem_vencida"
        assert "dona@exemplo.teste" not in json.dumps(aviso.corpo)
        assert f"Aplicativo {cad.sufixo}" in aviso.corpo["texto"]

    # Uma segunda rodada nao repete nada.
    await vencer_ordens(engine_rotinas)
    n = await _contar(
        engine_rotinas,
        "SELECT count(*) FROM extrato WHERE tipo IN ('ordem_vencida', 'ordem_inativa') AND pessoa_id = :p",
        p=cad.pessoa_id,
    )
    assert n == 3
    assert (
        await _contar(engine_rotinas, "SELECT count(*) FROM avisos WHERE destino = :d", d=f"pessoa:{cad.pessoa_id}")
        == 3
    )


async def test_avisa_uma_vez_antes_de_vencer(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    cad = await cadastro_minimo(engine_dono)
    perto = await ordem(engine_dono, cad, inicio="-1 day", fim="3 days", ultimo_uso="-1 hour")
    longe = await ordem(engine_dono, cad, inicio="-1 day", fim="20 days", ultimo_uso="-1 hour")
    await avisar_ordens_a_vencer(engine_rotinas)
    await avisar_ordens_a_vencer(engine_rotinas)
    async with engine_rotinas.connect() as c:
        chaves = {
            str(k)
            for (k,) in await c.execute(
                text("SELECT chave_unica FROM avisos WHERE destino = :d"), {"d": f"pessoa:{cad.pessoa_id}"}
            )
        }
    assert chaves == {f"ordem_a_vencer:{perto}"}
    assert f"ordem_a_vencer:{longe}" not in chaves
    n = await _contar(
        engine_rotinas,
        "SELECT count(*) FROM extrato WHERE tipo = 'ordem_a_vencer' AND dados ->> 'ordem_id' = :o",
        o=str(perto),
    )
    assert n == 1


async def test_politica_vigente_muda_o_aviso(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    async with engine_rotinas.connect() as c:
        assert await valor_politica(c, "WORK_ORDER_WARN_DAYS") == 7
    async with engine_dono.begin() as c:
        await c.execute(
            text("INSERT INTO politica (chave, valor, autor, motivo) VALUES ('WORK_ORDER_WARN_DAYS', 25, 't', 'teste')")
        )
    try:
        async with engine_rotinas.connect() as c:
            assert await valor_politica(c, "WORK_ORDER_WARN_DAYS") == 25
        cad = await cadastro_minimo(engine_dono)
        vinte = await ordem(engine_dono, cad, inicio="-1 day", fim="20 days", ultimo_uso="-1 hour")
        await avisar_ordens_a_vencer(engine_rotinas)
        assert (
            await _contar(
                engine_rotinas, "SELECT count(*) FROM avisos WHERE chave_unica = :k", k=f"ordem_a_vencer:{vinte}"
            )
            == 1
        )
    finally:
        async with engine_dono.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo)"
                    " VALUES ('WORK_ORDER_WARN_DAYS', 7, 't', 'volta')"
                )
            )


# --- Fechamento ------------------------------------------------------------------------------


async def test_fechamento_encadeado_com_frase(engine_rotinas: AsyncEngine) -> None:
    from mcp_claro.extrato import conferir_fechamento, registrar

    async with engine_rotinas.begin() as c:
        await registrar(c, papel="rotinas", tipo="partida", campos={"papel": "rotinas", "versao": "teste"})
    f1 = await fechar_agora(engine_rotinas)
    assert f1 is not None
    # Logo depois, nao esta devido: o intervalo e CLOSING_INTERVAL_HOURS.
    assert await fechar_se_devido(engine_rotinas, 6) is None
    f2 = await fechar_se_devido(engine_rotinas, 0)
    assert f2 is not None
    assert f2.numero == f1.numero + 1
    async with engine_rotinas.connect() as c:
        anterior, hash_anterior, desde, ate = (
            await c.execute(
                text(
                    "SELECT anterior, hash_anterior, desde_xid::text, ate_xid::text FROM fechamentos WHERE numero = :n"
                ),
                {"n": f2.numero},
            )
        ).one()
        assert anterior == f1.numero
        assert bytes(hash_anterior) == f1.hash
        assert await conferir_fechamento(c, f1.numero)
        assert await conferir_fechamento(c, f2.numero)
        # A frase do fechamento 1 entrou no extrato e ficou coberta pelo fechamento 2.
        frase, xid = (
            await c.execute(
                text("SELECT frase, xid::text FROM extrato WHERE tipo = 'fechamento' AND dados ->> 'numero' = :n"),
                {"n": str(f1.numero)},
            )
        ).one()
    assert frase == f"Fechamento {f1.numero} calculado com {f1.linhas} linhas."
    assert int(desde) <= int(xid) < int(ate)
    assert f2.linhas >= 1


# --- Particoes e medicoes --------------------------------------------------------------------


def _mais_meses(d: date, n: int) -> date:
    ano, mes = divmod(d.month - 1 + n, 12)
    return date(d.year + ano, mes + 1, 1)


async def test_particoes_dos_proximos_meses(engine_rotinas: AsyncEngine) -> None:
    hoje = datetime.now(UTC).date()
    nomes = await garantir_particoes(engine_rotinas, meses_a_frente=6)
    alvo = _mais_meses(hoje, 6)
    assert f"public.extrato_{alvo:%Y_%m}" in nomes
    assert f"medicoes.minuto_{alvo:%Y_%m}" in nomes
    assert (
        await _contar(
            engine_rotinas,
            "SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
            " WHERE i.inhparent = 'extrato'::regclass AND c.relname = :n",
            n=f"extrato_{alvo:%Y_%m}",
        )
        == 1
    )
    # De novo: nada muda, nada quebra.
    assert await garantir_particoes(engine_rotinas, meses_a_frente=6) == nomes


async def test_resumo_minuto_hora_dia_e_guarda(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    sufixo = uuid.uuid4().hex[:8]
    nome_h, nome_d, nome_v = f"teste_h_{sufixo}", f"teste_d_{sufixo}", f"teste_v_{sufixo}"
    async with engine_dono.begin() as c:
        await c.execute(
            text(
                "INSERT INTO medicoes.minuto (minuto, nome, rotulos, valor) VALUES"
                " (date_trunc('hour', now(), 'UTC') - interval '2 hours' + interval '5 minutes', :h, '{}', 3),"
                " (date_trunc('hour', now(), 'UTC') - interval '2 hours' + interval '10 minutes', :h, '{}', 4),"
                " (date_trunc('hour', now(), 'UTC'), :h, '{}', 1),"
                " (date_trunc('day', now(), 'UTC') - interval '1 day' + interval '10 hours 1 minute', :d, '{}', 2),"
                " (date_trunc('day', now(), 'UTC') - interval '1 day' + interval '11 hours 1 minute', :d,"
                '  \'{"lado": "a"}\', 5),'
                " (date_trunc('day', now(), 'UTC') - interval '1 day' + interval '12 hours 1 minute', :d,"
                '  \'{"lado": "a"}\', 6),'
                " (now() - interval '2 days', :v, '{}', 9)"
            ),
            {"h": nome_h, "d": nome_d, "v": nome_v},
        )
        await c.execute(
            text(
                "INSERT INTO medicoes.hora (hora, nome, rotulos, valor)"
                " VALUES (now() - interval '14 months', :v, '{}', 1)"
            ),
            {"v": nome_v},
        )
        await c.execute(
            text("INSERT INTO medicoes.dia (dia, nome, rotulos, valor) VALUES (current_date - 2200, :v, '{}', 1)"),
            {"v": nome_v},
        )
    primeiro = await resumir_medicoes(engine_rotinas, minuto_dias=31)
    assert primeiro.horas_apagadas >= 1  # hora de 14 meses atras (guarda de 13)
    assert primeiro.dias_apagados >= 1  # dia de 6 anos atras (guarda de 5)
    async with engine_rotinas.connect() as c:
        horas = (
            await c.execute(text("SELECT hora, valor FROM medicoes.hora WHERE nome = :h ORDER BY hora"), {"h": nome_h})
        ).all()
        assert [v for _, v in horas] == [7]  # a hora corrente ainda nao fechou
        dias = (
            await c.execute(
                text("SELECT dia, rotulos, valor FROM medicoes.dia WHERE nome = :d ORDER BY valor"), {"d": nome_d}
            )
        ).all()
    ontem = datetime.now(UTC).date() - timedelta(days=1)
    assert [(d, r, v) for d, r, v in dias] == [(ontem, {}, 2), (ontem, {"lado": "a"}, 11)]

    # Contagem que chega atrasada entra na hora ao refazer.
    async with engine_dono.begin() as c:
        await c.execute(
            text(
                "INSERT INTO medicoes.minuto (minuto, nome, rotulos, valor) VALUES"
                " (date_trunc('hour', now(), 'UTC') - interval '2 hours' + interval '59 minutes', :h, '{}', 10)"
            ),
            {"h": nome_h},
        )
    resumo = await resumir_medicoes(engine_rotinas, minuto_dias=1)
    assert resumo.minutos_apagados >= 1  # minuto de 2 dias atras, com guarda de 1 dia
    assert await _contar(engine_rotinas, "SELECT valor FROM medicoes.hora WHERE nome = :h", h=nome_h) == 17
    # O minuto velho ja tinha entrado na hora dele antes de sair; o que passou da guarda sumiu.
    assert await _contar(engine_rotinas, "SELECT count(*) FROM medicoes.minuto WHERE nome = :v", v=nome_v) == 0
    assert await _contar(engine_rotinas, "SELECT sum(valor) FROM medicoes.hora WHERE nome = :v", v=nome_v) == 9
    velhas = "SELECT count(*) FROM medicoes.hora WHERE nome = :v AND hora < now() - interval '13 months'"
    assert await _contar(engine_rotinas, velhas, v=nome_v) == 0
    velhos = "SELECT count(*) FROM medicoes.dia WHERE nome = :v AND dia < current_date - interval '5 years'"
    assert await _contar(engine_rotinas, velhos, v=nome_v) == 0


# --- Limpeza ---------------------------------------------------------------------------------


async def test_limpa_sessoes_e_logins_vencidos(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    cad = await cadastro_minimo(engine_dono)
    s = uuid.uuid4().hex[:8]
    async with engine_dono.begin() as c:
        for nome, expira, encerrada in (
            ("vencida", "-1 minute", None),
            ("encerrada", "1 hour", "-1 minute"),
            ("valida", "1 hour", None),
        ):
            await c.execute(
                text(
                    "INSERT INTO sessoes_telas (hash_id, pessoa_id, csrf, expira_em, encerrada_em) VALUES"
                    " (convert_to(:h, 'UTF8'), :p, 'csrf', now() + CAST(CAST(:e AS text) AS interval),"
                    "  now() + CAST(CAST(:f AS text) AS interval))"
                ),
                {"h": f"{nome}-{s}", "p": cad.pessoa_id, "e": expira, "f": encerrada},
            )
        for nome, expira, usado in (
            ("vencido", "-1 minute", None),
            ("usado", "5 minutes", "-1 second"),
            ("vale", "5 minutes", None),
        ):
            await c.execute(
                text(
                    "INSERT INTO logins_pendentes (estado, nonce, verificador, expira_em, usado_em) VALUES"
                    " (:e, 'n', 'v', now() + CAST(CAST(:x AS text) AS interval),"
                    "  now() + CAST(CAST(:u AS text) AS interval))"
                ),
                {"e": f"{nome}-{s}", "x": expira, "u": usado},
            )
    contagens = await limpar_sessoes(engine_rotinas)
    assert contagens["sessoes"] >= 2
    assert contagens["logins"] >= 2
    async with engine_dono.connect() as c:
        sessoes = {
            bytes(h).decode()
            for (h,) in await c.execute(
                text("SELECT hash_id FROM sessoes_telas WHERE pessoa_id = :p"), {"p": cad.pessoa_id}
            )
        }
        logins = {
            e
            for (e,) in await c.execute(
                text("SELECT estado FROM logins_pendentes WHERE estado LIKE :s"), {"s": f"%-{s}"}
            )
        }
    assert sessoes == {f"valida-{s}"}
    assert logins == {f"vale-{s}"}


# --- Resumo semanal --------------------------------------------------------------------------


async def test_resumo_semanal_do_admin_sem_dado_pessoal(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    # Uma semana no futuro, so com os dados deste teste.
    agora = datetime(2031, 3, 5, 15, 0, tzinfo=UTC)
    semana = semana_anterior(agora)
    assert semana.inicio == datetime(2031, 2, 24, tzinfo=UTC)
    no_meio = semana.inicio + timedelta(days=2)
    cad = await cadastro_minimo(engine_dono, email="ninguem@exemplo.teste")
    async with engine_dono.begin() as c:
        await c.execute(
            text(
                "INSERT INTO recusas_versao (minuto, lado, origem, versao_pedida, motivo, contagem) VALUES"
                " (:m, 'aplicativo', 'app-velho', '2025-11-25', 'initialize', 3),"
                " (:m, 'sistema', 'sistema-velho', 'geracao-antiga', 'cadastro', 2),"
                " (:fora, 'aplicativo', 'app-velho', '2025-11-25', 'initialize', 100)"
            ),
            {"m": no_meio, "fora": semana.fim + timedelta(hours=2)},
        )
        await c.execute(
            text(
                "INSERT INTO medicoes.hora (hora, nome, rotulos, valor) VALUES"
                " (:h, 'crachas_recusados', '{\"motivo\": \"audiencia\"}', 5),"
                " (:h, 'leituras', '{\"sistema\": \"s\"}', 7),"
                " (:h, 'chamadas_recusadas', '{\"motivo\": \"x\"}', 2),"
                " (:h, 'leituras_recusadas_despachante', '{\"motivo\": \"y\"}', 1)"
            ),
            {"h": no_meio},
        )
        await c.execute(
            text(
                "INSERT INTO bloqueios (tipo, alcance, pessoa_id, quem, motivo, criado_em) VALUES"
                " ('bloqueio', 'pessoa', :p, 'teste', 'teste', :m), ('pausa', 'pessoa', :p, 'teste', 'teste', :m)"
            ),
            {"p": cad.pessoa_id, "m": no_meio},
        )
    # Segunda-feira cedo ainda e cedo demais (folga para contagens atrasadas).
    assert (
        await resumo_semanal(engine_rotinas, ["admin@exemplo.teste"], agora=semana.fim + timedelta(minutes=30)) is None
    )
    corpo = await resumo_semanal(engine_rotinas, ["admin@exemplo.teste"], agora=agora)
    assert corpo is not None
    n = corpo["contagens"]
    assert n["recusas_versao_aplicativos"] == 3
    assert n["recusas_versao_sistemas"] == 2
    assert n["crachas_recusados"] == 5
    assert n["leituras"] == 7
    assert n["leituras_recusadas"] == 3
    assert n["bloqueios_ligados"] == 1
    assert n["pausas_ligadas"] == 1
    assert n["bloqueios_ativos"] >= 2
    texto = json.dumps(corpo)
    assert str(cad.pessoa_id) not in texto
    assert "ninguem@exemplo.teste" not in texto
    # Uma vez por semana.
    assert await resumo_semanal(engine_rotinas, ["admin@exemplo.teste"], agora=agora + timedelta(days=1)) is None
    chave = f"resumo_semanal:{semana.rotulo}|admin@exemplo.teste"
    assert await _contar(engine_rotinas, "SELECT count(*) FROM avisos WHERE chave_unica = :k", k=chave) == 1
    assert (
        await _contar(
            engine_rotinas,
            "SELECT count(*) FROM extrato WHERE tipo = 'resumo_semanal' AND dados ->> 'semana' = :s",
            s=semana.rotulo,
        )
        == 1
    )


# --- Agendador -------------------------------------------------------------------------------


async def test_trava_consultiva_uma_vez_por_instalacao(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine, parametros_rotinas: Parametros
) -> None:
    nome = f"teste_trava_{uuid.uuid4().hex[:6]}"
    rodou: list[str] = []

    async def tarefa() -> None:
        rodou.append("x")

    agendador = Agendador(engine_rotinas, [Tarefa(nome, 60, tarefa)])
    async with engine_dono.connect() as outro_processo:
        await outro_processo.execute(
            text("SELECT pg_advisory_lock(hashtext('mcp_claro.rotinas'), hashtext(:n))"), {"n": nome}
        )
        assert await agendador.rodar_uma(agendador.tarefas[0]) is Desfecho.OCUPADA
        assert rodou == []
        await outro_processo.execute(
            text("SELECT pg_advisory_unlock(hashtext('mcp_claro.rotinas'), hashtext(:n))"), {"n": nome}
        )
    assert await agendador.rodar_uma(agendador.tarefas[0]) is Desfecho.FEITA
    assert rodou == ["x"]

    # Dois processos (dois engines) disputando a mesma tarefa lenta: so um roda.
    comecou = asyncio.Event()
    solta = asyncio.Event()

    async def lenta() -> None:
        rodou.append("lenta")
        comecou.set()
        await solta.wait()

    outro_engine = criar_engine(parametros_rotinas, pool_size=2, max_overflow=0)
    try:
        a = Agendador(engine_rotinas, [Tarefa(nome, 60, lenta)])
        b = Agendador(outro_engine, [Tarefa(nome, 60, lenta)])
        primeira = asyncio.create_task(a.rodar_uma(a.tarefas[0]))
        await asyncio.wait_for(comecou.wait(), 5)
        assert await b.rodar_uma(b.tarefas[0]) is Desfecho.OCUPADA
        solta.set()
        assert await primeira is Desfecho.FEITA
    finally:
        await outro_engine.dispose()
    assert rodou.count("lenta") == 1


async def test_falha_de_uma_tarefa_nao_derruba_as_outras(engine_rotinas: AsyncEngine) -> None:
    contagem = {"boa": 0, "quebrada": 0}

    async def boa() -> None:
        contagem["boa"] += 1

    async def quebrada() -> None:
        contagem["quebrada"] += 1
        raise RuntimeError("falha de proposito")

    async def demorada() -> None:
        await asyncio.sleep(10)

    s = uuid.uuid4().hex[:6]
    agendador = Agendador(
        engine_rotinas,
        [
            Tarefa(f"boa_{s}", 0.05, boa),
            Tarefa(f"quebrada_{s}", 0.05, quebrada),
            Tarefa(f"demorada_{s}", 0.05, demorada, prazo=0.2),
        ],
        primeira_espera=0,
        ao_mesmo_tempo=3,
    )
    async with agendador.rodando():
        await asyncio.sleep(1.0)
    assert contagem["quebrada"] >= 2  # tentou de novo a cada intervalo
    assert contagem["boa"] >= 3
    assert agendador.ultimos[f"quebrada_{s}"] is Desfecho.FALHOU
    assert agendador.ultimos[f"boa_{s}"] is Desfecho.FEITA
    assert agendador.ultimos[f"demorada_{s}"] is Desfecho.ESTOUROU
    # A trava saiu junto com cada rodada, mesmo na falha e no prazo estourado.
    assert (
        await _contar(
            engine_rotinas,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = hashtext('mcp_claro.rotinas')::oid"
            " AND objid = ANY (ARRAY[hashtext(:a)::oid, hashtext(:b)::oid, hashtext(:c)::oid])",
            a=f"boa_{s}",
            b=f"quebrada_{s}",
            c=f"demorada_{s}",
        )
        == 0
    )
