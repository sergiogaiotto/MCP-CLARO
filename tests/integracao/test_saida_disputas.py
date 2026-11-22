"""Disputas na porta de saida, com duas ou mais conexoes reais ao PostgreSQL ao mesmo tempo.

Bloquear, cancelar e enviar a mesma acao: so um acontece. Bloqueio no meio de uma fila cheia: nada segue
depois dele, e o comprovante bate com o historico. Duas portas no mesmo banco: so uma envia, e a que
herda nao reenvia. Pedidos iguais ao mesmo tempo: um protocolo so. Regras da porta por dentro: a queda da
conexao da trava impede a gravacao de "enviando", o aviso do banco (LISTEN/NOTIFY) acorda a porta sem esperar a
varredura, e a resposta que chega depois da retomada so vai ao extrato e a tela. Restauracao pela conferencia de
continuidade de verdade, com a fila cheia: a saida fecha, a fila para, e nada sai.

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

from mcp_claro import continuidade
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
from mcp_claro.telas.acoes import montar, textos_do_resultado
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
async def ambiente_sem_porta(
    banco: Parametros, monkeypatch: pytest.MonkeyPatch, *, extra: dict[str, Any] | None = None
) -> AsyncIterator[Ambiente]:
    monkeypatch.setattr(despachante_app, "PortaDeSaida", PortaDesligada)
    async with ambiente_de_acoes(banco, monkeypatch, extra=extra) as a:
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
    """(paradas, saidas) do comprovante: protocolo -> estado que o comprovante guardou.

    ``saidas`` junta as duas partes do que nao parou a tempo: o que ja tinha seguido e o que ainda esta em
    conferencia (por exemplo, "enviando" na hora do bloqueio)."""
    async with amb.entrada_db.connect() as c:
        comp = await fila.comprovante_do_bloqueio(c, b.id)
    assert comp is not None
    return (
        {p["protocolo"]: p["estava"] for p in comp.paradas},
        {s["protocolo"]: s["estado"] for s in [*comp.ja_saidas, *comp.em_conferencia]},
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
                            "SELECT frase FROM extrato WHERE tipo = 'comprovante_bloqueio_partes'"
                            " AND dados ->> 'bloqueio_id' = :b"
                        ),
                        {"b": str(b.id)},
                    )
                ).scalar_one()
            assert quem == {f"bloqueio:{b.id}"}
            assert f"{len(paradas)} acoes paradas" in frase
            async with amb.entrada_db.connect() as c:
                comp = await fila.comprovante_do_bloqueio(c, b.id)
            assert comp is not None
            assert len(comp.ja_saidas) + len(comp.em_conferencia) == len(saidas)
            assert f"{len(comp.ja_saidas)} ja tinham saido; {len(comp.em_conferencia)} ainda em conferencia" in frase

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
        pessoa_id=amb.ana_id,
        aplicativo=aplicativo,
        ferramenta=f,
        argumentos=argumentos,
        aplicativo_diz=None,
        terceiros=False,
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
            terceiros=False,
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
    await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, {"titulo": "Chega ao teto"}, None, terceiros=False))
    async with amb.admin.connect() as c:
        # A limpeza do arranjo fecha as pausas de antes: daqui em diante, o que aparecer e desta rodada.
        desde = (await c.execute(text("SELECT coalesce(max(id), 0) FROM extrato"))).scalar_one()
    lote = [
        Pedido(amb.ana_id, aplicativo, f, {"titulo": f"Passa do teto {n}"}, None, terceiros=False) for n in range(4)
    ]
    resultados = await asyncio.gather(*(pedidos.pedir(p) for p in lote), return_exceptions=True)
    recusas = [r for r in resultados if isinstance(r, AcaoRecusada) and r.motivo == "teto_confirmacoes"]
    assert len(recusas) == len(lote), [type(r).__name__ for r in resultados]
    async with amb.admin.connect() as c:
        pausas = list(
            (
                await c.execute(
                    text(
                        "SELECT id FROM bloqueios WHERE aplicativo_id = :a AND encerrado_em IS NULL"
                        " AND alcance = 'aplicativo' AND tipo = 'pausa'"
                    ),
                    {"a": amb.aplicativo_id},
                )
            ).scalars()
        )
        comprovantes = (
            await c.execute(text("SELECT count(*) FROM comprovantes WHERE bloqueio_id = ANY(:b)"), {"b": pausas})
        ).scalar_one()
        tipos = Counter(
            (
                await c.execute(
                    text("SELECT tipo FROM extrato WHERE id > :d AND aplicativo_id = :a"),
                    {"d": desde, "a": amb.aplicativo_id},
                )
            ).scalars()
        )
    # Dois pedidos no teto ao mesmo tempo ligam uma pausa so: um comprovante e uma linha "pausa ligada".
    assert len(pausas) == 1, pausas
    assert comprovantes == 1
    assert tipos["pausa_ligada"] == 1, tipos
    assert tipos["comprovante_bloqueio_partes"] == 1, tipos
    # Cada recusa fica no extrato com a frase do teto.
    assert tipos["teto_confirmacoes"] == len(lote), tipos


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


# --- (f) Regras da porta por dentro --------------------------------------------------------------


async def backend_da_trava(amb: Ambiente) -> int:
    """PID da conexao que segura a trava ``DESPACHANTE_UNICO``."""
    classe, objeto = _partes(travas.DESPACHANTE_UNICO)
    async with amb.admin.connect() as c:
        return int(
            (
                await c.execute(
                    text(
                        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
                        " AND classid = :c AND objid = :o AND objsubid = 1"
                    ),
                    {"c": classe, "o": objeto},
                )
            ).scalar_one()
        )


async def sem_trava(amb: Ambiente) -> None:
    async def livre() -> bool:
        return await travas_do_despachante(amb) == 0

    await esperar(livre, passo=0.05)


async def test_queda_da_conexao_da_trava_antes_de_marcar_enviando_nao_envia(amb: Ambiente) -> None:
    """Toda gravacao de "enviando" usa a conexao que segura a trava: se ela cai, a gravacao falha."""
    op = await nova_acao(amb, "Trava cai antes de enviando")
    # Varredura longa: depois da queda, a porta so tenta de novo daqui a 3 s.
    p = PortaComPausa(amb.despachante_db, amb.cifra, varredura=3.0, prazo_envio=5.0)
    tarefa = asyncio.create_task(p.rodar())
    try:
        await asyncio.wait_for(p.na_conferencia.wait(), 15)
        assert (await operacao(amb, op.protocolo)).estado == Estado.LIBERADA
        pid = await backend_da_trava(amb)
        async with amb.admin.begin() as c:
            assert (await c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})).scalar_one()
        await sem_trava(amb)
        p.segue.set()  # a porta tenta marcar "enviando" na conexao que caiu
        await esperar(lambda: not p.assumiu.is_set())  # o turno terminou com erro
        assert (await operacao(amb, op.protocolo)).estado == Estado.LIBERADA
        assert chamadas(amb, "criar_chamado") == 0, "a gravacao de enviando devia falhar com a trava"

        # A porta assume de novo e envia, uma vez so.
        await esperar_estado(amb, op.protocolo, Estado.ENVIADA, prazo=20)
        assert chamadas(amb, "criar_chamado") == 1
    finally:
        p.segue.set()
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa


async def test_porta_acorda_pelo_aviso_do_banco_sem_esperar_a_varredura(amb: Ambiente) -> None:
    """LISTEN/NOTIFY: com varredura de 30 s, a acao confirmada sai em menos de 2 s."""
    p = porta(amb)
    p.varredura = 30.0
    tarefa = asyncio.create_task(p.rodar())
    try:
        await asyncio.wait_for(p.assumiu.wait(), 15)
        await asyncio.sleep(1.0)  # a porta ja dorme a espera longa
        inicio = asyncio.get_running_loop().time()
        op = await nova_acao(amb, "Acorda pelo aviso")
        await esperar_estado(amb, op.protocolo, Estado.ENVIADA, prazo=2.0)
        assert asyncio.get_running_loop().time() - inicio < 2.0
        assert chamadas(amb, "criar_chamado") == 1

        # Seguir agora tambem acorda a porta: a espera de 10 minutos acaba na hora.
        espera = await nova_acao(amb, "Seguir agora acorda", espera=600)
        await asyncio.sleep(0.5)
        assert (await operacao(amb, espera.protocolo)).estado == Estado.LIBERADA
        async with amb.entrada_db.begin() as c:
            await fila.seguir_agora(c, espera.id, pessoa_id=amb.ana_id)
        await esperar_estado(amb, espera.protocolo, Estado.ENVIADA, prazo=2.0)
        assert chamadas(amb, "criar_chamado") == 2
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa


async def test_resposta_que_chega_depois_da_retomada_so_vai_ao_extrato_e_a_tela(amb: Ambiente) -> None:
    """A dona antiga perde a trava no meio do envio, mas continua viva e recebe a resposta depois de a nova
    dona dar a acao como nao confirmada: o estado nao muda, a resposta fica guardada e vai ao extrato."""
    solta = asyncio.Event()
    chegou = asyncio.Event()

    async def chamar_devagar(*args: Any, **kwargs: Any) -> cliente.Resposta:
        resposta = await cliente.chamar_ferramenta(*args, **kwargs)  # o sistema executou
        chegou.set()
        await solta.wait()  # a resposta demora a voltar a dona antiga
        return resposta

    antiga = porta(amb, chamar=chamar_devagar)
    nova = porta(amb)
    tarefas = [asyncio.create_task(antiga.rodar())]
    try:
        await asyncio.wait_for(antiga.assumiu.wait(), 15)
        op = await nova_acao(amb, "Resposta depois da retomada")
        await asyncio.wait_for(chegou.wait(), 15)
        assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIANDO
        assert chamadas(amb, "criar_chamado") == 1

        # A conexao da trava da dona antiga cai; a dona antiga segue esperando a resposta, sem ser cancelada.
        pid = await backend_da_trava(amb)
        async with amb.admin.begin() as c:
            assert (await c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})).scalar_one()
        await sem_trava(amb)
        tarefas.append(asyncio.create_task(nova.rodar()))
        await asyncio.wait_for(nova.assumiu.wait(), 15)
        nc = await esperar_estado(amb, op.protocolo, Estado.NAO_CONFIRMADA)
        assert nc.desfecho == "interrompida no envio"
        assert nc.resultado is None

        # A resposta chega a dona antiga: so entra no extrato e na tela de decisao.
        solta.set()

        async def guardada() -> bool:
            return (await operacao(amb, op.protocolo)).resultado is not None

        await esperar(guardada)
        depois = await operacao(amb, op.protocolo)
        assert depois.estado == Estado.NAO_CONFIRMADA
        assert depois.desfecho == "o sistema respondeu (resposta depois da retomada)"
        assert depois.resultado is not None
        assert "Feito: criar_chamado" in " ".join(textos_do_resultado(depois.resultado))
        assert "Feito: criar_chamado" in " ".join(montar(depois).textos)  # o que a tela da acao mostra
        async with amb.admin.connect() as c:
            enviada = (
                await c.execute(
                    text("SELECT dados FROM extrato WHERE tipo = 'acao_enviada' AND protocolo = :p"),
                    {"p": op.protocolo},
                )
            ).all()
        assert len(enviada) == 1
        assert enviada[0].dados["gravado"] is False
        await asyncio.sleep(1.0)  # cinco varreduras da nova dona
        assert chamadas(amb, "criar_chamado") == 1
        assert (await operacao(amb, op.protocolo)).estado == Estado.NAO_CONFIRMADA
    finally:
        solta.set()
        for t in tarefas:
            t.cancel()
        for t in tarefas:
            with contextlib.suppress(asyncio.CancelledError):
                await t


# --- (g) Restauracao de verdade, com a fila cheia ------------------------------------------------


async def reativar(amb: Ambiente, ids: tuple[uuid.UUID, ...]) -> None:
    if ids:
        async with amb.admin.begin() as c:
            await c.execute(text("UPDATE aplicativos SET situacao = 'ativo' WHERE id = ANY(:i)"), {"i": list(ids)})


@pytest.mark.parametrize("sinal", ["marca_restaurado", "identificador_mudou"])
async def test_restauracao_pela_conferencia_fecha_a_saida_e_nada_sai(amb: Ambiente, sinal: str) -> None:
    pausados: tuple[uuid.UUID, ...] = ()
    nunca = asyncio.Event()

    async def chamar_e_cair(*args: Any, **kwargs: Any) -> cliente.Resposta:
        resposta = await cliente.chamar_ferramenta(*args, **kwargs)
        await nunca.wait()  # o despachante cai com a resposta na mao
        return resposta

    # Partida anterior conferida: a ultima linha da continuidade e a do banco como esta.
    base = await continuidade.conferir_continuidade(amb.despachante_db, 0, exigir_vigia=False)
    pausados += base.aplicativos_pausados
    await reativar(amb, pausados)
    if not (await situacao_da_saida(amb)):
        assert await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Arranjo do teste")

    antiga = porta(amb, chamar=chamar_e_cair)
    nova = porta(amb)
    tarefas = []
    try:
        esperando = await nova_acao(amb, "Restauracao esperando a ficha", confirmada=False)
        liberada = await nova_acao(amb, "Restauracao liberada na espera", espera=600)
        tarefas.append(asyncio.create_task(antiga.rodar()))
        enviando = await nova_acao(amb, "Restauracao no meio do envio")
        await esperar_estado(amb, enviando.protocolo, Estado.ENVIANDO)
        await esperar(lambda: chamadas(amb, "criar_chamado") == 1)
        tarefas[0].cancel()  # o despachante cai; a trava vai junto
        with contextlib.suppress(asyncio.CancelledError):
            await tarefas[0]
        await sem_trava(amb)

        # O banco volta de uma copia: marca do roteiro, ou outro identificador.
        if sinal == "marca_restaurado":
            await continuidade.marcar_restaurado(amb.despachante_db, "Pessoa da operacao", "Ensaio com a fila cheia")
        else:
            async with amb.admin.begin() as c:
                tl = (await c.execute(text("SELECT timeline_id FROM continuidade_controle()"))).scalar_one()
                await c.execute(
                    text(
                        "INSERT INTO continuidade_banco (system_identifier, timeline_id, desfecho)"
                        " VALUES ('1', :t, 'confere')"
                    ),
                    {"t": tl},
                )

        # A partida do despachante: a conferencia (com o papel do despachante) e, depois, a porta.
        r = await continuidade.conferir_continuidade(amb.despachante_db, 0, exigir_vigia=False)
        pausados += r.aplicativos_pausados
        assert r.desfecho == "nao_confere"
        assert sinal in r.motivos
        assert amb.aplicativo_id in r.aplicativos_pausados
        assert not await situacao_da_saida(amb)
        for o in (esperando, liberada):
            parada = await operacao(amb, o.protocolo)
            assert parada.estado == Estado.PARADA_POR_RESTAURACAO, o.protocolo
            assert parada.desfecho == "parada por restauracao"
        duvida = await operacao(amb, enviando.protocolo)
        assert duvida.estado == Estado.NAO_CONFIRMADA
        assert duvida.desfecho == "interrompida no envio"
        async with amb.admin.connect() as c:
            linha = (
                await c.execute(
                    text(
                        "SELECT frase, dados FROM extrato WHERE tipo = 'fila_parada_restauracao'"
                        " ORDER BY id DESC LIMIT 1"
                    )
                )
            ).one()
        assert linha.dados["enviando_com_dono"] == 0

        tarefas.append(asyncio.create_task(nova.rodar()))
        await asyncio.wait_for(nova.assumiu.wait(), 15)
        await asyncio.sleep(1.5)  # varias varreduras com a saida fechada
        assert chamadas(amb, "criar_chamado") == 1
        assert esperando.ficha_id is not None
        with pytest.raises(fila.DecisaoRecusada):
            async with amb.entrada_db.begin() as c:
                await fila.confirmar_ficha(
                    c, esperando.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=0, rapida_segundos=0
                )
        with pytest.raises(fila.DecisaoRecusada):
            async with amb.entrada_db.begin() as c:
                await fila.seguir_agora(c, liberada.id, pessoa_id=amb.ana_id)

        # Reaberta a saida por decisao do ADMIN, nada do que parou sai sem nova confirmacao.
        assert await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Conferido no ensaio")
        await asyncio.sleep(1.5)
        assert not await porta(amb).enviar_uma()
        assert chamadas(amb, "criar_chamado") == 1
        estados = [(await operacao(amb, o.protocolo)).estado for o in (esperando, liberada, enviando)]
        assert estados == [Estado.PARADA_POR_RESTAURACAO, Estado.PARADA_POR_RESTAURACAO, Estado.NAO_CONFIRMADA]
    finally:
        nunca.set()
        for t in tarefas:
            t.cancel()
        for t in tarefas:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if not await situacao_da_saida(amb):
            await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Fim do teste")
        await reativar(amb, pausados)
        await apagar_continuidade_falsa(amb)


async def apagar_continuidade_falsa(amb: Ambiente) -> None:
    """Tira a linha de continuidade com identificador falso ('1') que o teste gravou: o banco de teste e o mesmo
    para a sessao inteira. A tabela e so de acrescentar (gatilho); o dono do banco de teste, que e superusuario,
    desliga os gatilhos so nesta transacao (``session_replication_role``)."""
    async with amb.admin.begin() as c:
        await c.execute(text("SET LOCAL session_replication_role = replica"))
        await c.execute(text("DELETE FROM continuidade_banco WHERE system_identifier = '1'"))


async def situacao_da_saida(amb: Ambiente) -> bool:
    async with amb.admin.connect() as c:
        return bool((await c.execute(text("SELECT aberta FROM estado_saida WHERE id = 1"))).scalar_one())
