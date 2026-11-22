"""R6c-7: ``so_se_tiver`` e ``maximo_de_cortes`` do ``tests/cortador.py`` valem nos tres modos de corte.

Antes, so o modo ``engolir`` olhava os dois: em ``sem_resposta`` e ``meio_do_corpo`` toda conexao nova era cortada,
ao contrario do que a docstring diz. Aqui um servidor HTTP minimo responde a cada pedido com um corpo conhecido, e
cada pedido vai numa conexao propria.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from tests.cortador import Cortador, Modo

pytestmark = pytest.mark.anyio

CORPO = b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"resposta inteira do sistema"}]}}'
RESPOSTA = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n%s" % (len(CORPO), CORPO)
MODOS_DE_CORTE: tuple[Modo, ...] = ("sem_resposta", "meio_do_corpo", "engolir")


@contextlib.asynccontextmanager
async def _sistema() -> AsyncIterator[tuple[int, list[bytes]]]:
    """Le o pedido inteiro (pelo content-length) e responde ``RESPOSTA``. Devolve (porta, pedidos que chegaram)."""
    chegaram: list[bytes] = []

    async def atender(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        cabeca = await leitor.readuntil(b"\r\n\r\n")
        tamanho = next(
            int(linha.split(b":", 1)[1])
            for linha in cabeca.split(b"\r\n")
            if linha.lower().startswith(b"content-length")
        )
        chegaram.append(cabeca + await leitor.readexactly(tamanho))
        escritor.write(RESPOSTA)
        await escritor.drain()
        escritor.close()

    srv = await asyncio.start_server(atender, "127.0.0.1", 0)
    try:
        yield int(srv.sockets[0].getsockname()[1]), chegaram
    finally:
        srv.close()
        await srv.wait_closed()


async def _pedir(c: Cortador, metodo: str) -> bytes:
    """Um pedido numa conexao nova; devolve tudo o que voltou ate a conexao fechar."""
    corpo = b'{"jsonrpc":"2.0","id":1,"method":"%s"}' % metodo.encode()
    leitor, escritor = await asyncio.open_connection("127.0.0.1", c.porta)
    escritor.write(b"POST /mcp HTTP/1.1\r\nhost: x\r\ncontent-length: %d\r\n\r\n%s" % (len(corpo), corpo))
    await escritor.drain()
    try:
        return await asyncio.wait_for(leitor.read(), timeout=10)
    finally:
        escritor.close()


def _cortada(modo: Modo, volta: bytes) -> bool:
    if volta == RESPOSTA:
        return False
    if modo == "meio_do_corpo":
        cabeca, _, corpo = volta.partition(b"\r\n\r\n")
        assert RESPOSTA.startswith(cabeca + b"\r\n\r\n"), volta
        assert 0 < len(corpo) < len(CORPO), volta
    else:
        assert volta == b"", volta
    return True


@pytest.mark.parametrize("modo", MODOS_DE_CORTE)
async def test_so_se_tiver_corta_so_o_pedido_com_aqueles_bytes(modo: Modo) -> None:
    async with _sistema() as (porta, chegaram):
        c = Cortador("127.0.0.1", porta, modo=modo, so_se_tiver=b"tools/call")
        await c.iniciar()
        try:
            listar = await _pedir(c, "tools/list")
            chamar = await _pedir(c, "tools/call")
            listar_de_novo = await _pedir(c, "tools/list")
        finally:
            await c.parar()
    assert not _cortada(modo, listar)
    assert _cortada(modo, chamar)
    assert not _cortada(modo, listar_de_novo)
    assert c.cortes == 1
    assert len(chegaram) == 3  # o pedido chega inteiro, cortado ou nao: so a volta e cortada
    assert len(c.engolidas) == (1 if modo == "engolir" else 0)


@pytest.mark.parametrize("modo", MODOS_DE_CORTE)
async def test_maximo_de_cortes_deixa_passar_o_resto_inteiro(modo: Modo) -> None:
    async with _sistema() as (porta, chegaram):
        c = Cortador("127.0.0.1", porta, modo=modo, maximo_de_cortes=1)
        await c.iniciar()
        try:
            voltas = [await _pedir(c, "tools/call") for _ in range(3)]
        finally:
            await c.parar()
    assert [_cortada(modo, v) for v in voltas] == [True, False, False]
    assert c.cortes == 1
    assert len(chegaram) == 3


@pytest.mark.parametrize("modo", MODOS_DE_CORTE)
async def test_sem_filtro_corta_toda_conexao(modo: Modo) -> None:
    async with _sistema() as (porta, _):
        c = Cortador("127.0.0.1", porta, modo=modo)
        await c.iniciar()
        try:
            voltas = [await _pedir(c, "tools/call") for _ in range(2)]
        finally:
            await c.parar()
    assert [_cortada(modo, v) for v in voltas] == [True, True]
    assert c.cortes == 2


async def test_inteiro_repassa_tudo() -> None:
    async with _sistema() as (porta, _):
        c = Cortador("127.0.0.1", porta, so_se_tiver=b"tools/call", maximo_de_cortes=0)
        await c.iniciar()
        try:
            assert await _pedir(c, "tools/call") == RESPOSTA
        finally:
            await c.parar()
    assert c.cortes == 0
