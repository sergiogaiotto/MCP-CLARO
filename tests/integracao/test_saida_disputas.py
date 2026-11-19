"""Disputas na porta de saida, com duas ou mais conexoes reais ao PostgreSQL ao mesmo tempo.

Bloquear, cancelar e enviar a mesma acao: so um acontece. Bloqueio no meio de uma fila cheia: nada segue
depois dele, e o comprovante bate com o historico. Duas portas no mesmo banco: so uma envia, e a que
herda nao reenvia. Pedidos iguais ao mesmo tempo: um protocolo so.

O app do despachante sobe aqui sem a porta dele: cada teste usa ``PortaDeSaida`` direto (papel
``mcp_claro_despachante`` e ``CifraFalsa``), para escolher o momento de cada passo. As pausas forcadas
ficam dentro da transacao do envio (depois da reconferencia, antes de marcar "enviando") ou dentro da
transacao do bloqueio e do cancelamento, antes do COMMIT.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import FerramentaVitrine
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import app as despachante_app
from mcp_claro.ligacao import cliente
from mcp_claro.regras import bloqueios
from mcp_claro.saida import fila, travas
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import Operacao
from mcp_claro.saida.pedidos import AcaoRecusada, Pedido, Pedidos
from mcp_claro.saida.protocolo import chave_pedido_igual
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import ambiente_de_acoes, chamadas, chamar, esperar_estado, meta, operacao, politica

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

RODADAS = 30
ALCANCES = ("pessoa", "aplicativo", "pessoa_aplicativo", "sistema", "tudo")


class PortaDesligada:
    """No lugar da porta do app do despachante: aqui cada teste sobe as proprias portas."""

    def __init__(self, *_: object, **__: object) -> None:
        pass

    async def rodar(self) -> None:
        await asyncio.Event().wait()


async def sem_sobras(amb: Ambiente) -> None:
    """A fila de saida e uma so no banco de teste: o que outro teste deixou liberado nao entra na disputa."""
    async with amb.admin.begin() as c:
        await c.execute(
            text("UPDATE fila_saida SET estado = 'cancelada', mudou_por = 'teste:sobra' WHERE estado = 'liberada'")
        )


async def limpar(amb: Ambiente) -> None:
    async with amb.admin.begin() as c:
        await c.execute(text("DELETE FROM politica WHERE autor = 'teste-saida'"))
        await c.execute(
            text(
                "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste' WHERE encerrado_em IS NULL"
                " AND (alcance IN ('tudo', 'sistema') OR aplicativo_id = :a OR pessoa_id = ANY(:p))"
            ),
            {"a": amb.aplicativo_id, "p": [amb.ana_id, amb.bia_id]},
        )
    await sem_sobras(amb)


@contextlib.asynccontextmanager
async def ambiente_sem_porta(banco: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    monkeypatch.setattr(despachante_app, "PortaDeSaida", PortaDesligada)
    async with ambiente_de_acoes(banco, monkeypatch) as a:
        await politica(a, CONFIRM_CAP_PER_HOUR=1000)
        await sem_sobras(a)
        try:
            yield a
        finally:
            await limpar(a)


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with ambiente_sem_porta(banco_migrado, monkeypatch) as a:
        yield a


# --- Ajudas ----------------------------------------------------------------------------------


async def esperar(condicao: Callable[[], Awaitable[bool] | bool], prazo: float = 15.0, passo: float = 0.05) -> None:
    fim = asyncio.get_running_loop().time() + prazo
    while True:
        valor = condicao()
        if isinstance(valor, Awaitable):
            valor = await valor
        if valor:
            return
        if asyncio.get_running_loop().time() > fim:
            raise AssertionError("a condicao nao chegou no prazo")
        await asyncio.sleep(passo)


async def vitrine(
    amb: Ambiente, original: str = "criar_chamado", pessoa_id: uuid.UUID | None = None
) -> FerramentaVitrine:
    async with amb.entrada_db.connect() as c:
        f = await repositorio.ferramenta_na_vitrine(
            c, pessoa_id or amb.ana_id, amb.aplicativo_id, amb.vitrine[original], VERSAO_MCP
        )
    assert f is not None
    assert f.ordem_id is not None
    return f


async def nova_acao(
    amb: Ambiente,
    titulo: str,
    *,
    pessoa_id: uuid.UUID | None = None,
    confirmada: bool = True,
    espera: int = 0,
) -> Operacao:
    """Uma acao com ficha (como a entrada registra) e, se pedido, ja confirmada e liberada."""
    pessoa = pessoa_id or amb.ana_id
    f = await vitrine(amb, pessoa_id=pessoa)
    assert f.ordem_id is not None
    async with amb.entrada_db.begin() as c:
        op = await fila.registrar_pedido(
            c,
            pessoa_id=pessoa,
            aplicativo_id=amb.aplicativo_id,
            ordem_id=f.ordem_id,
            ferramenta=f,
            argumentos={"titulo": titulo},
            aplicativo_diz=None,
            rota=fila.Rota(),
            prazo_ficha_minutos=30,
            espera_segundos=0,
        )
    if not confirmada:
        return op
    assert op.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        return await fila.confirmar_ficha(
            c, op.ficha_id, pessoa_id=pessoa, com_passkey=False, espera_segundos=espera, rapida_segundos=0
        )


def campos_do_alcance(amb: Ambiente, alcance: str) -> dict[str, uuid.UUID]:
    return {
        "pessoa": {"pessoa_id": amb.ana_id},
        "aplicativo": {"aplicativo_id": amb.aplicativo_id},
        "pessoa_aplicativo": {"pessoa_id": amb.ana_id, "aplicativo_id": amb.aplicativo_id},
        "sistema": {"sistema_id": amb.sistema_id},
        "tudo": {},
    }[alcance]


async def bloquear(amb: Ambiente, alcance: str = "pessoa") -> bloqueios.Bloqueio:
    async with amb.entrada_db.begin() as c:
        return await bloqueios.bloquear(
            c,
            "bloqueio",
            alcance,
            bloqueios.quem_admin(amb.admin_id),
            "teste de disputa",
            **campos_do_alcance(amb, alcance),
        )


async def desbloquear(amb: Ambiente, b: bloqueios.Bloqueio) -> None:
    async with amb.entrada_db.begin() as c:
        await bloqueios.encerrar(c, b.id, bloqueios.quem_admin(amb.admin_id))


async def comprovante(amb: Ambiente, b: bloqueios.Bloqueio) -> tuple[dict[str, str], dict[str, str]]:
    """(paradas, ja_saidas) do comprovante: protocolo -> estado que o comprovante guardou."""
    async with amb.entrada_db.connect() as c:
        comp = await fila.comprovante_do_bloqueio(c, b.id)
    assert comp is not None
    return (
        {p["protocolo"]: p["estava"] for p in comp.paradas},
        {s["protocolo"]: s["estado"] for s in comp.ja_saidas},
    )


async def cancelar(amb: Ambiente, op: Operacao) -> Operacao | fila.DecisaoRecusada:
    try:
        async with amb.entrada_db.begin() as c:
            return await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)
    except fila.DecisaoRecusada as recusa:
        return recusa


def porta(amb: Ambiente, **extra: Any) -> PortaDeSaida:
    return PortaDeSaida(amb.despachante_db, amb.cifra, varredura=0.2, prazo_envio=5.0, **extra)


class PortaComPausa(PortaDeSaida):
    """Para dentro da transacao do envio: travas do alcance e linha da fila ja tomadas, reconferencia feita,
    "enviando" ainda nao gravado."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.na_conferencia = asyncio.Event()
        self.segue = asyncio.Event()

    async def _conferir(self, c: Any, op: Operacao) -> Any:
        conferida = await super()._conferir(c, op)
        self.na_conferencia.set()
        await self.segue.wait()
        return conferida


async def caixa(amb: Ambiente) -> list[str]:
    """Titulos que chegaram ao sistema de exemplo, na ordem em que chegaram."""
    async with httpx2.AsyncClient() as http:
        r = await http.get(f"{amb.url_sistema}/caixa")
    return [item["argumentos"].get("titulo", "") for item in r.json()["caixa"]]


async def extrato_do_protocolo(amb: Ambiente, tipo: str, protocolos: set[str]) -> list[str]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT protocolo FROM extrato WHERE tipo = :t AND protocolo = ANY(:p) ORDER BY id"),
                    {"t": tipo, "p": sorted(protocolos)},
                )
            ).scalars()
        )


# --- (a) Bloquear e enviar a mesma acao -------------------------------------------------------


async def test_bloqueio_que_entra_primeiro_para_a_acao(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Bloqueio entra primeiro")
    p = porta(amb)
    async with amb.entrada_db.begin() as c:
        b = await bloqueios.bloquear(
            c, "bloqueio", "pessoa", bloqueios.quem_admin(amb.admin_id), "disputa", pessoa_id=amb.ana_id
        )
        envio = asyncio.create_task(p.enviar_uma())
        await asyncio.sleep(0.5)
        assert not envio.done(), "o despachante devia esperar a trava do alcance"
    try:
        assert await asyncio.wait_for(envio, 10) is True
        assert (await operacao(amb, op.protocolo)).estado == Estado.PARADA_POR_BLOQUEIO
        paradas, saidas = await comprovante(amb, b)
        assert paradas.get(op.protocolo) == "liberada"
        assert op.protocolo not in saidas
        assert chamadas(amb, "criar_chamado") == 0
        assert not await p.enviar_uma()
        assert chamadas(amb, "criar_chamado") == 0
    finally:
        await desbloquear(amb, b)


async def test_envio_que_entra_primeiro_aparece_como_ja_saido(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Envio entra primeiro")
    p = PortaComPausa(amb.despachante_db, amb.cifra, varredura=0.2, prazo_envio=5.0)
    envio = asyncio.create_task(p.enviar_uma())
    await asyncio.wait_for(p.na_conferencia.wait(), 10)
    bloqueio = asyncio.create_task(bloquear(amb, "pessoa"))
    await asyncio.sleep(0.5)
    assert not bloqueio.done(), "o bloqueio devia esperar o despachante gravar enviando"
    p.segue.set()
    assert await asyncio.wait_for(envio, 15) is True
    b = await asyncio.wait_for(bloqueio, 10)
    try:
        assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIADA
        paradas, saidas = await comprovante(amb, b)
        assert saidas.get(op.protocolo) in ("enviando", "enviada")
        assert op.protocolo not in paradas
        assert chamadas(amb, "criar_chamado") == 1
    finally:
        await desbloquear(amb, b)


async def test_bloquear_e_enviar_ao_mesmo_tempo_so_um_acontece(amb: Ambiente) -> None:
    sorteio = random.Random(20261119)
    p = porta(amb)
    desfechos: Counter[Estado] = Counter()
    for n in range(RODADAS):
        op = await nova_acao(amb, f"Disputa de bloqueio {n}")
        antes = chamadas(amb, "criar_chamado")
        alcance = ALCANCES[n % len(ALCANCES)]
        folga_b, folga_e = sorteio.uniform(0, 0.02), sorteio.uniform(0, 0.02)

        async def bloquear_logo(folga: float = folga_b, alcance: str = alcance) -> bloqueios.Bloqueio:
            await asyncio.sleep(folga)
            return await bloquear(amb, alcance)

        async def enviar_logo(folga: float = folga_e) -> bool:
            await asyncio.sleep(folga)
            return await p.enviar_uma()

        b, _ = await asyncio.gather(bloquear_logo(), enviar_logo())
        try:
            atual = await operacao(amb, op.protocolo)
            paradas, saidas = await comprovante(amb, b)
            depois = chamadas(amb, "criar_chamado")
            if atual.estado == Estado.ENVIADA:
                assert depois == antes + 1, f"rodada {n}: enviada sem chamada contada"
                assert op.protocolo in saidas, f"rodada {n} ({alcance})"
                assert op.protocolo not in paradas, f"rodada {n} ({alcance})"
            else:
                assert atual.estado == Estado.PARADA_POR_BLOQUEIO, f"rodada {n}: ficou {atual.estado}"
                assert depois == antes, f"rodada {n}: parada, mas o sistema contou a chamada"
                assert op.protocolo in paradas, f"rodada {n} ({alcance})"
                assert op.protocolo not in saidas, f"rodada {n} ({alcance})"
            desfechos[atual.estado] += 1
        finally:
            await desbloquear(amb, b)
    assert sum(desfechos.values()) == RODADAS
    assert chamadas(amb, "criar_chamado") == desfechos[Estado.ENVIADA]


# --- (b) Cancelar e enviar a mesma acao -------------------------------------------------------


async def test_envio_que_entra_primeiro_recusa_o_cancelamento(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Envio antes de cancelar")
    p = PortaComPausa(amb.despachante_db, amb.cifra, varredura=0.2, prazo_envio=5.0)
    envio = asyncio.create_task(p.enviar_uma())
    await asyncio.wait_for(p.na_conferencia.wait(), 10)
    pedido = asyncio.create_task(cancelar(amb, op))
    await asyncio.sleep(0.5)
    assert not pedido.done(), "o cancelamento devia esperar a linha travada pelo despachante"
    p.segue.set()
    assert await asyncio.wait_for(envio, 15) is True
    recusa = await asyncio.wait_for(pedido, 10)
    assert isinstance(recusa, fila.DecisaoRecusada)
    assert recusa.motivo == "ja_saiu"
    assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIADA
    assert chamadas(amb, "criar_chamado") == 1
    assert await extrato_do_protocolo(amb, "acao_cancelada", {op.protocolo}) == []


async def test_cancelamento_que_entra_primeiro_nao_chega_ao_sistema(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Cancelar antes de enviar")
    p = porta(amb)
    async with amb.entrada_db.begin() as c:
        await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)
        # A linha esta travada pelo cancelamento: o despachante pula, sem esperar e sem enviar.
        assert await asyncio.wait_for(p.enviar_uma(), 10) is True
        assert chamadas(amb, "criar_chamado") == 0
    assert (await operacao(amb, op.protocolo)).estado == Estado.CANCELADA
    assert not await p.enviar_uma()
    assert chamadas(amb, "criar_chamado") == 0


async def test_cancelar_e_enviar_ao_mesmo_tempo_so_um_acontece(amb: Ambiente) -> None:
    sorteio = random.Random(1119)
    p = porta(amb)
    desfechos: Counter[Estado] = Counter()
    for n in range(RODADAS):
        op = await nova_acao(amb, f"Disputa de cancelamento {n}")
        antes = chamadas(amb, "criar_chamado")
        folga_c, folga_e = sorteio.uniform(0, 0.02), sorteio.uniform(0, 0.02)

        async def cancelar_logo(folga: float = folga_c, op: Operacao = op) -> Operacao | fila.DecisaoRecusada:
            await asyncio.sleep(folga)
            return await cancelar(amb, op)

        async def enviar_logo(folga: float = folga_e) -> bool:
            await asyncio.sleep(folga)
            return await p.enviar_uma()

        pedido, _ = await asyncio.gather(cancelar_logo(), enviar_logo())
        atual = await operacao(amb, op.protocolo)
        depois = chamadas(amb, "criar_chamado")
        if atual.estado == Estado.ENVIADA:
            assert isinstance(pedido, fila.DecisaoRecusada), f"rodada {n}"
            assert pedido.motivo == "ja_saiu", f"rodada {n}"
            assert depois == antes + 1, f"rodada {n}: enviada sem chamada contada"
        else:
            assert atual.estado == Estado.CANCELADA, f"rodada {n}: ficou {atual.estado}"
            assert isinstance(pedido, Operacao), f"rodada {n}"
            assert pedido.estado == Estado.CANCELADA, f"rodada {n}"
            assert depois == antes, f"rodada {n}: cancelada, mas o sistema contou a chamada"
            # A acao cancelada nao volta a ser pega.
            assert not await p.enviar_uma()
        desfechos[atual.estado] += 1
    assert sum(desfechos.values()) == RODADAS
    assert chamadas(amb, "criar_chamado") == desfechos[Estado.ENVIADA]


# --- (c) Bloqueio no meio de uma fila cheia ---------------------------------------------------


async def test_bloqueio_no_meio_da_fila_cheia_para_o_resto_e_o_comprovante_bate(amb: Ambiente) -> None:
    total = 40

    async def chamar_devagar(*args: Any, **kwargs: Any) -> cliente.Resposta:
        await asyncio.sleep(0.02)
        return await cliente.chamar_ferramenta(*args, **kwargs)

    titulos = {f"Fila cheia {n:02d}": n for n in range(total)}
    ops = {op.protocolo: op for op in [await nova_acao(amb, t) for t in titulos]}
    titulo_de = {op.protocolo: op.argumentos["titulo"] for op in ops.values()}
    p = porta(amb, chamar=chamar_devagar)
    tarefa = asyncio.create_task(p.rodar())

    async def estados() -> dict[str, Estado]:
        async with amb.admin.connect() as c:
            linhas = (
                await c.execute(
                    text(
                        "SELECT o.protocolo, f.estado FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"
                        " WHERE o.protocolo = ANY(:p)"
                    ),
                    {"p": sorted(ops)},
                )
            ).all()
        return {linha.protocolo: Estado(linha.estado) for linha in linhas}

    try:

        async def cinco_enviadas() -> bool:
            return sum(e == Estado.ENVIADA for e in (await estados()).values()) >= 5

        await esperar(cinco_enviadas, passo=0.01)
        b = await bloquear(amb, "pessoa")
        try:

            async def nada_enviando() -> bool:
                return Estado.ENVIANDO not in (await estados()).values()

            await esperar(nada_enviando)
            na_hora = chamadas(amb, "criar_chamado")
            await asyncio.sleep(1.0)  # cinco varreduras da porta: nada mais sai
            assert chamadas(amb, "criar_chamado") == na_hora

            finais = await estados()
            enviadas = {p_ for p_, e in finais.items() if e == Estado.ENVIADA}
            paradas_f = {p_ for p_, e in finais.items() if e == Estado.PARADA_POR_BLOQUEIO}
            assert enviadas | paradas_f == set(ops), "toda acao terminou enviada ou parada"
            assert enviadas, "o bloqueio devia cair no meio da fila"
            assert paradas_f, "o bloqueio devia cair no meio da fila"

            paradas, saidas = await comprovante(amb, b)
            # O comprovante cobre o alcance inteiro (24 horas); aqui contam as acoes deste teste.
            assert {x for x in paradas if x in ops} == paradas_f
            assert {x for x in saidas if x in ops} == enviadas
            assert all(saidas[x] in ("enviando", "enviada") for x in enviadas)
            async with amb.admin.connect() as c:
                quem = set(
                    (
                        await c.execute(
                            text(
                                "SELECT DISTINCT f.mudou_por FROM fila_saida f JOIN operacoes o"
                                " ON o.id = f.operacao_id WHERE o.protocolo = ANY(:p)"
                            ),
                            {"p": sorted(paradas_f)},
                        )
                    ).scalars()
                )
                frase = (
                    await c.execute(
                        text(
                            "SELECT frase FROM extrato WHERE tipo = 'comprovante_bloqueio'"
                            " AND dados ->> 'bloqueio_id' = :b"
                        ),
                        {"b": str(b.id)},
                    )
                ).scalar_one()
            assert quem == {f"bloqueio:{b.id}"}
            assert f"{len(paradas)} acoes paradas" in frase
            assert f"{len(saidas)} ja tinham saido" in frase

            # O historico: o sistema recebeu exatamente as que o comprovante da como saidas, uma vez cada.
            chegaram = [t for t in await caixa(amb) if t in titulos]
            assert sorted(chegaram) == sorted(titulo_de[x] for x in enviadas)
            assert chamadas(amb, "criar_chamado") == len(enviadas)
            assert sorted(await extrato_do_protocolo(amb, "acao_enviada", set(ops))) == sorted(enviadas)
        finally:
            await desbloquear(amb, b)
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa


# --- (d) Duas portas no mesmo banco -----------------------------------------------------------


def _partes(chave: int) -> tuple[int, int]:
    sem_sinal = chave & 0xFFFFFFFFFFFFFFFF
    return sem_sinal >> 32, sem_sinal & 0xFFFFFFFF


async def travas_do_despachante(amb: Ambiente) -> int:
    classe, objeto = _partes(travas.DESPACHANTE_UNICO)
    async with amb.admin.connect() as c:
        return int(
            (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                        " AND classid = :c AND objid = :o AND objsubid = 1"
                    ),
                    {"c": classe, "o": objeto},
                )
            ).scalar_one()
        )


async def test_duas_portas_so_uma_envia_e_a_que_herda_nao_reenvia(amb: Ambiente) -> None:
    quem_enviou: list[tuple[str, str]] = []
    nunca = asyncio.Event()  # a dona "cai" com a resposta na mao, antes de gravar o fim

    def chamador(nome: str) -> Callable[..., Awaitable[cliente.Resposta]]:
        async def chamar_ferramenta(*args: Any, **kwargs: Any) -> cliente.Resposta:
            titulo = str(args[4].get("titulo"))
            quem_enviou.append((nome, titulo))
            resposta = await cliente.chamar_ferramenta(*args, **kwargs)
            if titulo.startswith("Presa"):
                await nunca.wait()
            return resposta

        return chamar_ferramenta

    portas = {nome: porta(amb, chamar=chamador(nome)) for nome in ("A", "B")}
    tarefas = {nome: asyncio.create_task(p.rodar()) for nome, p in portas.items()}
    try:
        await esperar(lambda: any(p.assumiu.is_set() for p in portas.values()))
        await asyncio.sleep(1.0)  # a reserva tenta a trava varias vezes
        donas = [nome for nome, p in portas.items() if p.assumiu.is_set()]
        assert len(donas) == 1, donas
        dona = donas[0]
        reserva = "B" if dona == "A" else "A"
        assert await travas_do_despachante(amb) == 1

        ops = [await nova_acao(amb, f"Duas portas {n}") for n in range(10)]
        for op in ops:
            await esperar_estado(amb, op.protocolo, Estado.ENVIADA)
        assert sorted(t for _, t in quem_enviou) == sorted(f"Duas portas {n}" for n in range(10))
        assert {nome for nome, _ in quem_enviou} == {dona}
        assert sorted(await caixa(amb)) == sorted(f"Duas portas {n}" for n in range(10))

        # A dona cai no meio do envio: o pedido chegou ao sistema, o fim nao foi gravado.
        presa = await nova_acao(amb, "Presa no envio")
        await esperar_estado(amb, presa.protocolo, Estado.ENVIANDO)
        await esperar(lambda: chamadas(amb, "criar_chamado") == 11)
        tarefas[dona].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefas[dona]
        await asyncio.wait_for(portas[reserva].assumiu.wait(), 15)
        nao_confirmada = await esperar_estado(amb, presa.protocolo, Estado.NAO_CONFIRMADA)
        assert nao_confirmada.desfecho == "interrompida no envio"
        await asyncio.sleep(1.0)  # cinco varreduras da nova dona
        assert chamadas(amb, "criar_chamado") == 11
        assert [t for _, t in quem_enviou].count("Presa no envio") == 1
        assert (await operacao(amb, presa.protocolo)).estado == Estado.NAO_CONFIRMADA
        assert await travas_do_despachante(amb) == 1
        async with amb.admin.connect() as c:
            frase = (
                await c.execute(
                    text("SELECT frase FROM extrato WHERE tipo = 'despachante_assumiu' ORDER BY id DESC LIMIT 1")
                )
            ).scalar_one()
        assert "; 1 acoes em envio ficaram nao confirmadas" in frase

        # A nova dona segue com o resto da fila.
        depois = await nova_acao(amb, "Depois da troca")
        await esperar_estado(amb, depois.protocolo, Estado.ENVIADA)
        assert (reserva, "Depois da troca") in quem_enviou
        assert chamadas(amb, "criar_chamado") == 12
    finally:
        nunca.set()
        for t in tarefas.values():
            t.cancel()
        for t in tarefas.values():
            with contextlib.suppress(asyncio.CancelledError):
                await t


# --- (e) Pedidos iguais ao mesmo tempo, e o ultimo uso de um limite ---------------------------


async def test_pedidos_iguais_ao_mesmo_tempo_viram_um_protocolo_so(amb: Ambiente) -> None:
    pedidos = Pedidos(amb.p, amb.entrada_db)
    async with amb.entrada_db.connect() as c:
        aplicativo = await repositorio.aplicativo_por_client_id(c, "claude-code")
    assert aplicativo is not None
    f = await vitrine(amb)
    argumentos = {"titulo": f"Pedido igual {uuid.uuid4().hex[:8]}"}
    pedido = Pedido(
        pessoa_id=amb.ana_id, aplicativo=aplicativo, ferramenta=f, argumentos=argumentos, aplicativo_diz=None
    )
    resultados = await asyncio.gather(*(pedidos.pedir(pedido) for _ in range(8)))
    assert len({op.id for op, _ in resultados}) == 1
    assert sorted(repetida for _, repetida in resultados) == [False] + [True] * 7
    async with amb.admin.connect() as c:
        n = (
            await c.execute(
                text("SELECT count(*) FROM operacoes WHERE chave_pedido_igual = :k"),
                {"k": chave_pedido_igual(amb.ana_id, aplicativo.id, f.id, argumentos)},
            )
        ).scalar_one()
    assert n == 1
    protocolo = resultados[0][0].protocolo
    assert len(await extrato_do_protocolo(amb, "acao_repetida", {protocolo})) == 7

    # Pelo aplicativo, tres clientes MCP ao mesmo tempo.
    outros = {"titulo": f"Pedido igual pelo MCP {uuid.uuid4().hex[:8]}"}
    respostas = await asyncio.gather(*(chamar(amb, "criar_chamado", outros) for _ in range(3)))
    assert len({meta(r)[PREFIXO_META + "protocolo"] for r in respostas}) == 1
    assert sorted(bool(meta(r).get(PREFIXO_META + "repetido")) for r in respostas) == [False, True, True]
    assert chamadas(amb, "criar_chamado") == 0


async def test_dois_pedidos_pelo_ultimo_uso_de_um_limite_so_um_passa(amb: Ambiente) -> None:
    async with amb.entrada_db.begin() as c:
        limite = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate,"
                    " com_passkey) VALUES (:p, :a, :s, 'enviar_mensagem', 1, now() + interval '1 day', true)"
                    " RETURNING id"
                ),
                {"p": amb.ana_id, "a": amb.aplicativo_id, "s": amb.sistema_id},
            )
        ).scalar_one()
    pedidos = Pedidos(amb.p, amb.entrada_db)
    async with amb.entrada_db.connect() as c:
        aplicativo = await repositorio.aplicativo_por_client_id(c, "claude-code")
    assert aplicativo is not None
    f = await vitrine(amb, "enviar_mensagem")
    lote = [
        Pedido(
            pessoa_id=amb.ana_id,
            aplicativo=aplicativo,
            ferramenta=f,
            argumentos={"destinatario": "equipe", "texto": f"ultimo uso {n}"},
            aplicativo_diz=None,
        )
        for n in range(6)
    ]
    resultados = await asyncio.gather(*(pedidos.pedir(p) for p in lote))
    pelo_limite = [op for op, _ in resultados if op.limite_id is not None]
    assert len(pelo_limite) == 1
    assert pelo_limite[0].estado == Estado.LIBERADA
    assert all(op.estado == Estado.AGUARDANDO_FICHA for op, _ in resultados if op.limite_id is None)
    async with amb.admin.connect() as c:
        usados = (await c.execute(text("SELECT usados_hoje FROM limites WHERE id = :i"), {"i": limite})).scalar_one()
    assert usados == 1


# --- Defeitos achados pelas provas (corrigidos; os testes ficam) ---------------------------------


async def test_acao_antiga_saindo_na_hora_do_bloqueio_entra_no_comprovante(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Pedida ha mais de um dia")
    async with amb.admin.begin() as c:  # como uma ficha de programa confirmada 30 horas depois do pedido
        await c.execute(
            text("UPDATE operacoes SET criada_em = now() - interval '30 hours' WHERE id = :o"), {"o": op.id}
        )
    p = PortaComPausa(amb.despachante_db, amb.cifra, varredura=0.2, prazo_envio=5.0)
    envio = asyncio.create_task(p.enviar_uma())
    await asyncio.wait_for(p.na_conferencia.wait(), 10)
    bloqueio = asyncio.create_task(bloquear(amb, "pessoa"))
    await asyncio.sleep(0.3)
    p.segue.set()
    assert await asyncio.wait_for(envio, 15) is True
    b = await asyncio.wait_for(bloqueio, 10)
    try:
        assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIADA
        assert chamadas(amb, "criar_chamado") == 1
        paradas, saidas = await comprovante(amb, b)
        assert op.protocolo not in paradas
        assert op.protocolo in saidas, "a acao saiu, mas o comprovante nao a mostra"
    finally:
        await desbloquear(amb, b)


async def test_pedidos_ao_mesmo_tempo_no_teto_de_fichas_recusam_sem_impasse(amb: Ambiente) -> None:
    async with amb.admin.connect() as c:
        ja = await fila.fichas_na_ultima_hora(c, amb.aplicativo_id)
    await politica(amb, CONFIRM_CAP_PER_HOUR=ja + 1)
    pedidos = Pedidos(amb.p, amb.entrada_db)
    async with amb.entrada_db.connect() as c:
        aplicativo = await repositorio.aplicativo_por_client_id(c, "claude-code")
    assert aplicativo is not None
    f = await vitrine(amb)
    await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, {"titulo": "Chega ao teto"}, None))
    lote = [Pedido(amb.ana_id, aplicativo, f, {"titulo": f"Passa do teto {n}"}, None) for n in range(4)]
    resultados = await asyncio.gather(*(pedidos.pedir(p) for p in lote), return_exceptions=True)
    recusas = [r for r in resultados if isinstance(r, AcaoRecusada) and r.motivo == "teto_confirmacoes"]
    assert len(recusas) == len(lote), [type(r).__name__ for r in resultados]
    async with amb.admin.connect() as c:
        pausas = (
            await c.execute(
                text(
                    "SELECT count(*) FROM bloqueios WHERE aplicativo_id = :a AND encerrado_em IS NULL"
                    " AND alcance = 'aplicativo' AND tipo = 'pausa'"
                ),
                {"a": amb.aplicativo_id},
            )
        ).scalar_one()
    assert pausas >= 1


async def test_linha_travada_por_outra_transacao_nao_segura_a_fila(amb: Ambiente) -> None:
    travada = await nova_acao(amb, "Linha travada")
    livre = await nova_acao(amb, "Linha livre atras")
    p = porta(amb)
    voltas = 0
    enviar_uma = p.enviar_uma

    async def contando(dono: Any = None) -> bool:
        nonlocal voltas
        voltas += 1
        return await enviar_uma(dono)

    p.enviar_uma = contando  # type: ignore[method-assign]
    tarefa: asyncio.Task[None] | None = None
    async with amb.admin.begin() as c:
        # Outra transacao segura a primeira linha da fila (por exemplo, um cancelamento demorado).
        await c.execute(text("SELECT id FROM fila_saida WHERE id = :i FOR UPDATE"), {"i": travada.fila_id})
        tarefa = asyncio.create_task(p.rodar())
        try:
            await asyncio.sleep(3.0)
            estado_livre = (await operacao(amb, livre.protocolo)).estado
            voltas_travada = voltas
        finally:
            await c.rollback()
    try:
        assert estado_livre == Estado.ENVIADA, (
            f"a linha livre ficou {estado_livre} atras da travada; a porta deu {voltas_travada} voltas em 3 s"
        )
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
