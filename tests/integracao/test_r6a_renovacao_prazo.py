"""R6a-2: a troca de chave (OAuth) tem prazo total, e a renovacao espera pouco pela trava do par (contrato K2).

O arranjo da prova da revisao cruzada: o endereco de troca de chave de um servidor de terceiros responde 200 e manda o
corpo um byte a cada ``PINGO`` segundos. O prazo de leitura do httpx vale por pedaco lido, entao, antes, a troca
durava ate o corpo inteiro chegar (dias, com 64 KiB). A renovacao faz a troca com a trava do par (pessoa, sistema), e a
porta de saida unica chama a renovacao depois de marcar "enviando": ela parava, e nenhuma acao de nenhum sistema saia.
Leituras repetidas da mesma pessoa esperavam a trava sem prazo, cada uma com uma conexao do despachante.

Agora:

- a troca inteira (abrir, POST e ler o corpo) tem ``autorizacao.PRAZO_TROCA``; num terceiro, a conferencia do
  endereco vem antes, com o prazo dela (``cliente.PRAZO_CONEXAO``). Esgotado o prazo da troca,
  ``TrocaIndisponivel("prazo")``. A porta volta com "nao saiu" ("nao deu para renovar a chave");
- a renovacao espera a trava de outra renovacao do mesmo par no maximo ``autorizacao.ESPERA_TRAVA_RENOVACAO``; sem
  ela, a conexao volta ao pool e a chave atual e usada se ainda vale, como quando o servidor nao responde; vencida,
  ``RenovacaoIndisponivel("trava_ocupada")``.

Os testes esperam por eventos (a troca chegou ao endereco, a tarefa terminou), com teto de tempo, sem ``sleep``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import autorizacao, chaves, renovacao
from mcp_claro.despachante.servico import LeituraRecusada
from mcp_claro.ligacao import cliente
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import FINAIS, Estado
from tests import apoio_g3a as g
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_entrada_terceiros_apoio import Mundo, subir_mundo
from tests.integracao.test_saida_terceiros import LigacaoFalsa, liberada

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

CIFRA = CifraFalsa()
PINGO = 2.0  # segundos entre um byte e outro: bem abaixo do prazo de leitura de 10 s do httpx


@dataclass
class TrocaLenta:
    """Endereco de troca de chave que responde 200 e manda o corpo um byte a cada ``PINGO`` segundos."""

    url: str = ""
    pedidos: int = 0
    pingos: list[float] = field(default_factory=list)
    chegou: asyncio.Event = field(default_factory=asyncio.Event)


@contextlib.asynccontextmanager
async def troca_a_conta_gotas() -> AsyncIterator[TrocaLenta]:
    lenta = TrocaLenta()

    async def atender(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        try:
            cabecalho = await leitor.readuntil(b"\r\n\r\n")
            tamanho = 0
            for linha in cabecalho.split(b"\r\n"):
                if linha.lower().startswith(b"content-length:"):
                    tamanho = int(linha.split(b":", 1)[1])
            await leitor.readexactly(tamanho)
            lenta.pedidos += 1
            lenta.chegou.set()
            escritor.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 60000\r\n\r\n")
            await escritor.drain()
            while True:
                escritor.write(b" ")
                await escritor.drain()
                lenta.pingos.append(asyncio.get_running_loop().time())
                await asyncio.sleep(PINGO)  # o servidor lento do arranjo, nao a espera do teste
        except ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError:
            pass
        finally:
            escritor.close()

    servidor = await asyncio.start_server(atender, "127.0.0.1", 0)
    lenta.url = f"http://127.0.0.1:{servidor.sockets[0].getsockname()[1]}/token"
    try:
        yield lenta
    finally:
        servidor.close()


def _config(token: str) -> dict[str, Any]:
    return {
        "authorization_endpoint": token.replace("/token", "/authorize"),
        "token_endpoint": token,
        "client_id": "mcp-claro",
        "escopos": ["leitura"],
    }


async def _chaves_vencidas(engine: Any, cifra: CifraFalsa, sistema_id: Any, pessoa_id: Any) -> None:
    async with engine.begin() as c:
        await chaves.guardar(
            c,
            cifra,
            sistema_id=sistema_id,
            segredo="acesso-vencido",
            pessoa_id=pessoa_id,
            expira_em=datetime.now(UTC) - timedelta(minutes=5),
        )
        await chaves.guardar(
            c, cifra, sistema_id=sistema_id, segredo="renovacao", pessoa_id=pessoa_id, tipo="renovacao"
        )


async def _travas_consultivas(engine: Any) -> int:
    async with engine.connect() as c:
        return int(
            (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                        " AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
        )


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado, terceiros_ligados=True) as m:
        yield m


async def test_porta_de_saida_volta_no_prazo_da_troca_com_nao_saiu(mundo: Mundo) -> None:
    m = mundo
    await g.abrir_a_saida(m.dono)
    ana = m.pessoas["ana"]
    async with troca_a_conta_gotas() as lenta:
        async with m.despachante_db.begin() as c:
            await autorizacao.guardar_segredo_cliente(c, CIFRA, m.terceiro_id, "segredo-do-cliente")
        async with m.dono.begin() as c:
            await autorizacao.configurar(c, m.terceiro_id, _config(lenta.url))
        await _chaves_vencidas(m.dono, CIFRA, m.terceiro_id, ana)
        op = await liberada(m, "ana", "t_enviar", {"texto": "acao no terceiro"})
        ligacao = LigacaoFalsa(cliente.Resposta("nao_enviado"))
        porta = PortaDeSaida(
            m.despachante_db,
            CIFRA,
            varredura=0.2,
            prazo_envio=5.0,
            terceiros_ligados=True,
            ligacao=lambda _s: ligacao,
            ambiente="teste",
            liberar_locais=True,
        )
        inicio = anyio.current_time()
        # A porta pega uma acao por volta: as de outros testes da sessao, se houver, saem antes pela ligacao falsa.
        with anyio.fail_after(autorizacao.PRAZO_TROCA + 5):
            while (await m.operacao(op.protocolo)).estado not in FINAIS:
                await porta.enviar_uma()
        decorrido = anyio.current_time() - inicio
        fim = await m.operacao(op.protocolo)
    assert lenta.pedidos == 1
    # O endereco seguiu mandando bytes ate o fim: foi o prazo total que cortou, nao o prazo de leitura do httpx.
    assert len(lenta.pingos) >= int(autorizacao.PRAZO_TROCA // PINGO) - 2, lenta.pingos
    assert autorizacao.PRAZO_TROCA <= decorrido < autorizacao.PRAZO_TROCA + 5
    assert (fim.estado, fim.desfecho) == (Estado.NAO_SAIU, "nao deu para renovar a chave")
    async with m.dono.connect() as c:
        linha = (await c.execute(text("SELECT estado FROM fila_saida WHERE id = :i"), {"i": op.fila_id})).scalar()
    assert linha != Estado.ENVIANDO
    assert ligacao.chamadas == []  # nada foi ao terceiro
    assert await _travas_consultivas(m.dono) == 0  # a trava do par nao ficou presa
    falhas = [ln for ln in await g.extrato(m.dono, m.terceiro_id) if ln.tipo == "renovacao_falhou"]
    assert [ln.frase for ln in falhas][-1:] == [
        f"A chave da pessoa no sistema {m.terceiro_nome} nao foi renovada: o sistema nao respondeu."
    ]


class SalaQueNaoChega:
    """``Ligacao`` do terceiro que nunca e alcancada: a leitura para antes, na renovacao da chave."""

    async def chamar_ferramenta(self, *_: Any, **__: Any) -> Any:
        raise AssertionError("a leitura nao devia chegar ao terceiro")

    async def listar_ferramentas(self, *_: Any) -> Any:
        raise AssertionError

    async def sondar(self, *_: Any, **__: Any) -> Any:
        raise AssertionError


async def test_leituras_repetidas_de_um_terceiro_lento_nao_seguram_as_conexoes_do_despachante(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quatro leituras da mesma pessoa no mesmo terceiro lento, com 4 conexoes no pool do despachante: uma faz a troca
    (ate ``PRAZO_TROCA``); as outras tres esperam a trava no maximo ``ESPERA_TRAVA_RENOVACAO`` e devolvem a conexao.
    Outro trabalho do despachante pega conexao em menos de 5 s, e so uma troca sai."""
    somas: list[tuple[str, dict[str, str]]] = []
    somar = renovacao.CONTADORES.somar

    def somar_e_guardar(nome: str, valor: int = 1, **rotulos: str) -> None:
        somas.append((nome, dict(rotulos)))
        somar(nome, valor, **rotulos)

    monkeypatch.setattr(renovacao.CONTADORES, "somar", somar_e_guardar)
    dono = criar_engine(banco_migrado, pool_size=2, max_overflow=2)
    cifra = CifraFalsa()
    try:
        async with troca_a_conta_gotas() as lenta:
            arr = await g.preparar(
                dono,
                cifra,
                endereco="https://terceiro.exemplo.teste/mcp",
                definicao={"name": "consultar", "description": "x", "inputSchema": {"type": "object"}},
                origem="terceiro",
                tipo_chave="por_pessoa",
                chave=None,
            )
            desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante", tamanho=2)  # 2 + 2 conexoes
            try:
                async with desp_db.begin() as c:
                    await autorizacao.guardar_segredo_cliente(c, cifra, arr.sistema_id, "segredo-do-cliente")
                async with dono.begin() as c:
                    await autorizacao.configurar(c, arr.sistema_id, _config(lenta.url))
                await _chaves_vencidas(dono, cifra, arr.sistema_id, arr.pessoa_id)
                await g.abrir_a_saida(dono)
                desp = g.despachante(desp_db, cifra, sala=SalaQueNaoChega())  # type: ignore[arg-type]
                leituras = [asyncio.create_task(desp.ler(arr.pedido({}))) for _ in range(4)]
                with anyio.fail_after(10):
                    await lenta.chegou.wait()  # uma leitura segura a trava e conversa com o endereco lento
                # Outro trabalho do despachante precisa de uma conexao do mesmo pool.
                inicio = anyio.current_time()
                with anyio.fail_after(5):
                    async with desp_db.connect() as c:
                        await c.execute(text("SELECT 1"))
                esperou = anyio.current_time() - inicio
                with anyio.fail_after(autorizacao.ESPERA_TRAVA_RENOVACAO + 2):
                    feitas, presas = await asyncio.wait(leituras, return_when=asyncio.FIRST_COMPLETED)
                    while len(feitas) < 3:
                        mais, presas = await asyncio.wait(presas, return_when=asyncio.FIRST_COMPLETED)
                        feitas |= mais
                assert len(presas) == 1, "so a leitura que troca a chave devia seguir esperando"
                with anyio.fail_after(autorizacao.PRAZO_TROCA + 5):
                    await asyncio.wait(presas)
            finally:
                for t in leituras:
                    t.cancel()
                await asyncio.gather(*leituras, return_exceptions=True)
                await desp_db.dispose()
    finally:
        await dono.dispose()
    assert esperou < 5
    motivos = []
    for t in leituras:
        erro = t.exception()
        assert isinstance(erro, LeituraRecusada), erro
        motivos.append(erro.motivo)
    assert motivos == ["renovacao_indisponivel"] * 4
    assert lenta.pedidos == 1  # uma troca so: as outras desistiram sem enviar
    falhas = [r.get("motivo") for nome, r in somas if nome == "renovacoes_falhas"]
    assert sorted(falhas) == ["RenovacaoIndisponivel", "trava_ocupada", "trava_ocupada", "trava_ocupada"]


async def _sessoes_do_despachante_em_transacao(dono: Any) -> int:
    async with dono.connect() as c:
        return int(
            (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE application_name = 'teste-g3a-mcp_claro_despachante'"
                        " AND state LIKE 'idle in transaction%'"
                    )
                )
            ).scalar_one()
        )


@pytest.mark.parametrize(
    ("vale_por", "esperado"),
    [(timedelta(seconds=30), "acesso-atual"), (timedelta(minutes=-5), None)],
    ids=["chave_atual_ainda_vale", "chave_atual_vencida"],
)
async def test_sem_a_trava_a_renovacao_usa_a_chave_atual_se_ela_ainda_vale(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, vale_por: timedelta, esperado: str | None
) -> None:
    """Outra renovacao do mesmo par segura a trava por mais que ``ESPERA_TRAVA_RENOVACAO`` (como a que espera um
    servidor de autorizacao lento). A chave de acesso de agora ainda vale por mais 30 s, so que dentro da ``MARGEM``:
    ela e usada, como quando o servidor nao responde. Vencida: ``RenovacaoIndisponivel("trava_ocupada")``. Nos dois
    casos, nada vai ao endereco de troca, e a conexao do despachante volta ao pool enquanto a outra segura a trava."""
    assert timedelta(0) < timedelta(seconds=30) < renovacao.MARGEM
    somas: list[tuple[str, dict[str, str]]] = []
    somar = renovacao.CONTADORES.somar

    def somar_e_guardar(nome: str, valor: int = 1, **rotulos: str) -> None:
        somas.append((nome, dict(rotulos)))
        somar(nome, valor, **rotulos)

    monkeypatch.setattr(renovacao.CONTADORES, "somar", somar_e_guardar)
    dono = criar_engine(banco_migrado, pool_size=2, max_overflow=2)
    cifra = CifraFalsa()
    motivo = ""
    try:
        async with troca_a_conta_gotas() as lenta:
            arr = await g.preparar(
                dono,
                cifra,
                endereco="https://terceiro.exemplo.teste/mcp",
                definicao={"name": "consultar", "description": "x", "inputSchema": {"type": "object"}},
                origem="terceiro",
                tipo_chave="por_pessoa",
                chave=None,
            )
            desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante", tamanho=1)
            try:
                async with desp_db.begin() as c:
                    await autorizacao.guardar_segredo_cliente(c, cifra, arr.sistema_id, "segredo-do-cliente")
                async with dono.begin() as c:
                    await autorizacao.configurar(c, arr.sistema_id, _config(lenta.url))
                    await chaves.guardar(
                        c,
                        cifra,
                        sistema_id=arr.sistema_id,
                        segredo="acesso-atual",
                        pessoa_id=arr.pessoa_id,
                        expira_em=datetime.now(UTC) + vale_por,
                    )
                    await chaves.guardar(
                        c,
                        cifra,
                        sistema_id=arr.sistema_id,
                        segredo="renovacao",
                        pessoa_id=arr.pessoa_id,
                        tipo="renovacao",
                    )
                async with dono.connect() as outra:
                    await outra.begin()
                    # Outra renovacao do mesmo par segura a trava.
                    assert await autorizacao.travar_com_prazo(outra, arr.pessoa_id, arr.sistema_id, prazo=0.0)
                    inicio = anyio.current_time()
                    with anyio.fail_after(autorizacao.ESPERA_TRAVA_RENOVACAO + 5):
                        try:
                            aberta = await renovacao.chave_pronta(
                                desp_db,
                                cifra,
                                sistema_id=arr.sistema_id,
                                pessoa_id=arr.pessoa_id,
                                ambiente="teste",
                                liberar_locais=True,
                            )
                            resultado: str | None = aberta.segredo
                            assert aberta.renovada is False
                        except renovacao.RenovacaoIndisponivel as erro:
                            resultado, motivo = None, str(erro)
                    decorrido = anyio.current_time() - inicio
                    # A outra ainda segura a trava, e nenhuma conexao do despachante ficou numa transacao aberta.
                    assert await _sessoes_do_despachante_em_transacao(dono) == 0
                    await outra.rollback()
            finally:
                await desp_db.dispose()
    finally:
        await dono.dispose()
    assert resultado == esperado, f"chave com vale_por={vale_por}: o desfecho foi {resultado or motivo}"
    if esperado is None:
        assert motivo == "trava_ocupada"
    assert autorizacao.ESPERA_TRAVA_RENOVACAO <= decorrido < autorizacao.ESPERA_TRAVA_RENOVACAO + 2
    assert lenta.pedidos == 0  # sem a trava, nada vai ao endereco de troca
    assert [r.get("motivo") for nome, r in somas if nome == "renovacoes_falhas"] == ["trava_ocupada"]


async def test_travar_com_prazo_espera_pouco_e_pega_a_trava_quando_ela_solta(banco_migrado: Parametros) -> None:
    dono = criar_engine(banco_migrado, pool_size=2, max_overflow=2)
    pessoa, sistema = uuid.uuid4(), uuid.uuid4()
    try:
        async with dono.connect() as primeira, dono.connect() as segunda:
            await primeira.begin()
            assert await autorizacao.travar_com_prazo(primeira, pessoa, sistema, prazo=0.0) is True
            await segunda.begin()
            inicio = anyio.current_time()
            assert await autorizacao.travar_com_prazo(segunda, pessoa, sistema, prazo=0.3) is False
            assert 0.3 <= anyio.current_time() - inicio < 2
            await segunda.rollback()  # sem a trava, a transacao sai sem nada feito
            # A primeira solta a trava no fim da transacao; a segunda pega dentro do prazo.
            await segunda.begin()
            espera = asyncio.create_task(autorizacao.travar_com_prazo(segunda, pessoa, sistema, prazo=5.0))
            await primeira.commit()
            with anyio.fail_after(5):
                assert await espera is True
            await segunda.commit()
    finally:
        await dono.dispose()
