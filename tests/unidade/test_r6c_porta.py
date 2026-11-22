"""R6c-1: a porta de ``tests.apoio.porta_livre()`` fica presa ate o servidor de teste abrir.

Antes, ``porta_livre()`` escolhia a porta e a soltava; o uvicorn so abria depois. Nesse meio tempo, outra sessao
de testes na mesma maquina (ou o Docker, que publica portas na mesma faixa) podia abrir o mesmo numero, e o
servidor caia com "address already in use", como ``SystemExit`` (o ramo da mensagem nunca rodava). Um servidor
lento para subir era entregue com a porta fechada.

Aqui: a outra sessao e um socket com ``SO_REUSEADDR`` que faz bind e listen, como o uvicorn faz.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

import httpx2
import pytest
from starlette.applications import Starlette

from tests.apoio import porta_livre, servidor, servidor_paravel, soltar

pytestmark = pytest.mark.anyio


def _outra_sessao_abre(porta: int, host: str = "127.0.0.1") -> socket.socket:
    """O que o uvicorn de outra sessao faz quando o porta_livre() dela devolve o mesmo numero."""
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, porta))
        s.listen()
    except OSError:
        s.close()
        raise
    return s


def _fechada(porta: int) -> bool:
    """A porta recusa conexao (ninguem escuta nela)."""
    with socket.socket() as s:
        s.settimeout(2)
        try:
            s.connect(("127.0.0.1", porta))
        except ConnectionRefusedError:
            return True
    return False


@contextlib.asynccontextmanager
async def _partida_de(segundos: float, _: Starlette) -> AsyncIterator[None]:
    await asyncio.sleep(segundos)
    yield


def test_porta_escolhida_fica_presa_e_fechada_ate_o_servidor_abrir() -> None:
    porta = porta_livre()
    with pytest.raises(OSError, match="in use"):  # a outra sessao nao abre a porta reservada
        _outra_sessao_abre(porta).close()
    assert _fechada(porta)  # quem usa porta_livre() como porta fechada continua recebendo "conexao recusada"


def test_soltar_deixa_outro_programa_abrir_a_porta() -> None:
    porta = soltar(porta_livre())
    _outra_sessao_abre(porta).close()
    assert soltar(porta) == porta  # soltar de novo, ou uma porta sem reserva, nao faz nada


async def test_servidor_sobe_na_porta_reservada_e_a_outra_sessao_nao_entra() -> None:
    porta = porta_livre()
    with pytest.raises(OSError, match="in use"):
        _outra_sessao_abre(porta).close()
    async with servidor(Starlette(), porta) as url, httpx2.AsyncClient() as http:
        assert url == f"http://127.0.0.1:{porta}"
        assert (await http.get(url + "/")).status_code == 404
        with pytest.raises(OSError, match="in use"):
            _outra_sessao_abre(porta).close()
    assert _fechada(porta)


async def test_porta_ocupada_por_outro_da_erro_legivel_e_nao_system_exit() -> None:
    outra = _outra_sessao_abre(0)
    porta = int(outra.getsockname()[1])
    try:
        with pytest.raises(RuntimeError, match=rf"o servidor de teste nao subiu em 127\.0\.0\.1:{porta}"):
            async with servidor(Starlette(), porta):
                pytest.fail("subiu numa porta ocupada")
    finally:
        outra.close()


async def test_partida_que_falha_da_erro_legivel_e_nao_system_exit() -> None:
    @contextlib.asynccontextmanager
    async def quebra(_: Starlette) -> AsyncIterator[None]:
        raise ValueError("partida quebrada de proposito")
        yield  # pragma: no cover

    porta = porta_livre()
    with pytest.raises(RuntimeError, match=rf"o servidor de teste nao subiu em 127\.0\.0\.1:{porta}"):
        async with servidor(Starlette(lifespan=quebra), porta):
            pytest.fail("subiu com a partida quebrada")
    assert _fechada(porta)


async def test_o_teste_seguinte_no_mesmo_laco_continua_rodando() -> None:
    async with servidor(Starlette()) as url, httpx2.AsyncClient() as http:
        assert url.startswith("http://127.0.0.1:")
        assert (await http.get(url + "/")).status_code == 404


async def test_servidor_lento_para_subir_nao_e_entregue_fechado() -> None:
    """Uma partida (lifespan) de 5 s, como numa maquina carregada: a URL so sai depois que a partida terminou.

    A porta escuta desde antes da partida, entao um pedido feito no meio dela so espera na fila e e atendido
    depois; por isso o pedido sozinho nao mostra se a URL saiu cedo. A marca da partida mostra: a espera antiga,
    de 200 x 0,02 s, entregava a URL antes de 5 s, com o app ainda subindo.
    """
    partiu = asyncio.Event()

    @contextlib.asynccontextmanager
    async def partida(_: Starlette) -> AsyncIterator[None]:
        await asyncio.sleep(5)
        partiu.set()
        yield

    async with servidor(Starlette(lifespan=partida)) as url, httpx2.AsyncClient() as http:
        assert partiu.is_set(), "a URL saiu antes de a partida do app terminar"
        r = await http.get(url + "/")
    assert r.status_code == 404


async def test_servidor_que_passa_do_prazo_da_erro_claro() -> None:
    def partida(app: Starlette) -> contextlib.AbstractAsyncContextManager[None]:
        return _partida_de(30, app)

    porta = porta_livre()
    with pytest.raises(RuntimeError, match=rf"nao subiu em 127\.0\.0\.1:{porta} em 0\.3 s"):
        async with servidor(Starlette(lifespan=partida), porta, prazo=0.3):
            pytest.fail("entregou a URL antes de subir")
    assert _fechada(porta)


async def test_a_mesma_porta_serve_dois_servidores_um_depois_do_outro() -> None:
    porta = porta_livre()
    for _ in range(2):
        async with servidor(Starlette(), porta) as url, httpx2.AsyncClient() as http:
            assert (await http.get(url + "/")).status_code == 404


async def test_servidor_que_fecha_conexoes_abertas_deixa_subir_outro_na_mesma_porta() -> None:
    """O servidor para com o cliente ainda conectado (quem fecha e o servidor: TIME_WAIT do lado dele). O socket da
    reserva escuta com ``SO_REUSEADDR``, como o do uvicorn; sem isso, as conexoes herdam a falta dele e prendem a
    porta por um minuto (o que derrubou o teste do vigia que para e sobe a entrada de novo)."""
    porta = porta_livre()
    async with httpx2.AsyncClient() as http:
        async with servidor_paravel(Starlette(), porta) as (url, parar):
            assert (await http.get(url + "/")).status_code == 404
            await parar()
            async with servidor(Starlette(), porta):
                assert (await http.get(url + "/")).status_code == 404
        async with servidor(Starlette(), porta):
            assert (await http.get(url + "/")).status_code == 404


async def test_servidor_paravel_para_e_a_porta_continua_presa_ate_subir_de_novo() -> None:
    porta = porta_livre()
    async with servidor_paravel(Starlette(), porta) as (url, parar):
        async with httpx2.AsyncClient() as http:
            assert (await http.get(url + "/")).status_code == 404
        await parar()
        assert _fechada(porta)
        with pytest.raises(OSError, match="in use"):  # parado no meio do teste, ninguem mais toma a porta
            _outra_sessao_abre(porta).close()
        async with servidor(Starlette(), porta) as de_novo, httpx2.AsyncClient() as http:
            assert de_novo == url
            assert (await http.get(url + "/")).status_code == 404
    _outra_sessao_abre(porta).close()  # no fim, nada fica preso


async def test_outro_endereco_solta_a_reserva_da_porta() -> None:
    porta = porta_livre()
    async with servidor(Starlette(), porta, host="127.0.0.2") as url, httpx2.AsyncClient() as http:
        assert url == f"http://127.0.0.2:{porta}"
        assert (await http.get(url + "/")).status_code == 404
    _outra_sessao_abre(porta).close()  # a reserva em 127.0.0.1 saiu
