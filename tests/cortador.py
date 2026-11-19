"""Intermediario TCP so de teste: deixa o pedido chegar inteiro ao sistema e corta so o caminho da resposta.

Fica entre o despachante e o sistema de exemplo, como um Toxiproxy de bolso. O modo vale para cada
conexao nova, lido na hora em que ela chega:

- ``inteiro``: repassa tudo, nos dois sentidos;
- ``sem_resposta``: o pedido chega ao sistema; quando a resposta comeca a voltar, a conexao com o
  despachante e fechada sem repassar nenhum byte;
- ``meio_do_corpo``: repassa o cabecalho e metade do corpo da resposta, e fecha.

Nos modos de corte, o sistema ja executou quando a resposta comeca: o corte acontece depois do efeito.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

Modo = Literal["inteiro", "sem_resposta", "meio_do_corpo"]

_TAMANHO = re.compile(rb"^content-length:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Cortador:
    destino_host: str
    destino_porta: int
    modo: Modo = "inteiro"
    conexoes: int = 0
    cortes: int = 0
    pedidos: list[bytes] = field(default_factory=list)
    _servidor: asyncio.Server | None = None
    _tarefas: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def porta(self) -> int:
        assert self._servidor is not None
        return int(self._servidor.sockets[0].getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    async def iniciar(self) -> None:
        self._servidor = await asyncio.start_server(self._atender, "127.0.0.1", 0)

    async def parar(self) -> None:
        if self._servidor is not None:
            self._servidor.close()
            with contextlib.suppress(Exception):
                await self._servidor.wait_closed()
        for t in list(self._tarefas):
            t.cancel()
        for t in list(self._tarefas):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _atender(self, leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        tarefa = asyncio.current_task()
        if tarefa is not None:
            self._tarefas.add(tarefa)
        modo = self.modo
        self.conexoes += 1
        recebido = bytearray()
        self.pedidos.append(b"")
        indice = len(self.pedidos) - 1
        try:
            up_leitor, up_escritor = await asyncio.open_connection(self.destino_host, self.destino_porta)
        except OSError:
            escritor.close()
            return

        async def ida() -> None:
            try:
                while pedaco := await leitor.read(65536):
                    recebido.extend(pedaco)
                    self.pedidos[indice] = bytes(recebido)
                    up_escritor.write(pedaco)
                    await up_escritor.drain()
            except ConnectionError, OSError:
                pass
            finally:
                with contextlib.suppress(Exception):
                    up_escritor.write_eof()

        envio = asyncio.create_task(ida())
        try:
            if modo == "inteiro":
                while pedaco := await up_leitor.read(65536):
                    escritor.write(pedaco)
                    await escritor.drain()
            elif modo == "sem_resposta":
                await up_leitor.read(1)  # o sistema comecou a responder: ja executou
                self.cortes += 1
            else:
                await self._metade_do_corpo(up_leitor, escritor)
                self.cortes += 1
        except ConnectionError, OSError:
            pass
        finally:
            envio.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await envio
            for w in (escritor, up_escritor):
                with contextlib.suppress(Exception):
                    w.close()
            if tarefa is not None:
                self._tarefas.discard(tarefa)

    async def _metade_do_corpo(self, up_leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        bruto = bytearray()
        while b"\r\n\r\n" not in bruto:
            pedaco = await up_leitor.read(65536)
            if not pedaco:
                return
            bruto.extend(pedaco)
        cabecalho, _, corpo = bytes(bruto).partition(b"\r\n\r\n")
        achado = _TAMANHO.search(cabecalho)
        esperado = int(achado.group(1)) if achado else None
        corpo_lido = bytearray(corpo)
        while esperado is not None and len(corpo_lido) < esperado:
            pedaco = await up_leitor.read(65536)
            if not pedaco:
                break
            corpo_lido.extend(pedaco)
        metade = max(1, len(corpo_lido) // 2)
        escritor.write(cabecalho + b"\r\n\r\n" + bytes(corpo_lido[:metade]))
        await escritor.drain()


@contextlib.asynccontextmanager
async def cortador(url_destino: str) -> AsyncIterator[Cortador]:
    """Sobe o intermediario na frente de ``http://host:porta`` e o derruba no fim."""
    sem_esquema = url_destino.split("://", 1)[-1].split("/", 1)[0]
    host, _, porta = sem_esquema.rpartition(":")
    c = Cortador(host, int(porta))
    await c.iniciar()
    try:
        yield c
    finally:
        await c.parar()
