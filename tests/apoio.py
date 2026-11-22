"""Ajudas dos testes: subir apps ASGI de verdade (uvicorn) numa porta livre."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

import anyio
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_claro.versao import VERSAO_MCP

PRAZO_PARTIDA: Final = 60.0
"""Segundos que ``servidor`` espera o app subir (lifespan e escuta) antes de desistir com erro."""

_LOCAL: Final = "127.0.0.1"
_FILA: Final = 2048  # o backlog padrao do uvicorn
_reservas: dict[int, socket.socket] = {}
_trava = threading.Lock()


def _reservar(porta: int = 0, *, reaproveitar: bool = False) -> socket.socket:
    """Um socket com bind em 127.0.0.1, sem listen: a porta fica presa e fechada (quem conecta e recusado).

    Sem ``SO_REUSEADDR``, nenhum outro socket faz bind nela, nem o uvicorn de outra sessao de testes (que usa
    ``SO_REUSEADDR``). ``reaproveitar`` liga o ``SO_REUSEADDR``: so para prender de novo uma porta recem-usada,
    que ainda tem conexoes em TIME_WAIT; mesmo assim o bind na porta 0 de outra sessao nao a escolhe.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if reaproveitar:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((_LOCAL, porta))
    except OSError:
        s.close()
        raise
    return s


def _guardar(s: socket.socket) -> int:
    porta = int(s.getsockname()[1])
    with _trava:
        _reservas[porta] = s
    return porta


def _tirar(porta: int) -> socket.socket | None:
    with _trava:
        return _reservas.pop(porta, None)


def _tirar_se(porta: int, reserva: socket.socket) -> bool:
    """Tira ``reserva`` do registro so se ela ainda for a reserva de ``porta``."""
    with _trava:
        if _reservas.get(porta) is not reserva:
            return False
        del _reservas[porta]
    return True


def porta_livre() -> int:
    """Escolhe uma porta livre em 127.0.0.1 e a deixa reservada (presa e fechada) ate ``servidor`` abrir.

    O numero e o mesmo de sempre; a diferenca e que, entre a escolha e o servidor, ninguem mais abre a porta.
    Quem conecta antes do servidor recebe "conexao recusada", como numa porta fechada. Para entregar a porta a
    outro programa (subprocesso, conteiner), solte a reserva antes com ``soltar``.
    """
    return _guardar(_reservar())


def soltar(porta: int) -> int:
    """Solta a reserva de ``porta_livre`` (se houver) e devolve o mesmo numero, para outro programa abrir a porta.

    Exemplo: ``porta = soltar(porta_livre())``. Entre soltar e o outro programa abrir, a porta volta a ficar
    solta: faca isso o mais perto possivel da partida dele.
    """
    reserva = _tirar(porta)
    if reserva is not None:
        reserva.close()
    return porta


def _abrir(host: str, porta: int) -> socket.socket:
    """O socket em que o servidor vai escutar, ja escutando: a propria reserva da porta, ou um bind novo.

    A reserva ganha ``SO_REUSEADDR`` logo antes do ``listen``, como o socket do uvicorn: as conexoes aceitas herdam
    a opcao, e sem ela as que o servidor fecha (TIME_WAIT) prendem a porta por um minuto, e outro servidor nao sobe
    nela depois. Escutando, ninguem mais faz bind na porta, com ou sem a opcao. Durante a partida do app, quem
    conecta espera na fila do ``listen``; a URL so sai com o servidor atendendo.
    """
    reserva = _tirar(porta)
    if reserva is not None and host == _LOCAL:
        sock = reserva
    else:
        if reserva is not None:  # outro endereco: a reserva em 127.0.0.1 sai antes do bind no endereco pedido
            reserva.close()
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sock is not reserva:
            sock.bind((host, porta))
        sock.listen(_FILA)
    except OSError as erro:
        sock.close()
        raise RuntimeError(f"o servidor de teste nao subiu em {host}:{porta}: {erro}") from erro
    return sock


async def _servir(srv: uvicorn.Server, sock: socket.socket, onde: str) -> None:
    """``srv.serve`` com a partida que falha (o uvicorn chama ``sys.exit``) virando erro legivel."""
    try:
        await srv.serve(sockets=[sock])
    except SystemExit as saida:
        raise RuntimeError(f"o servidor de teste nao subiu em {onde} (uvicorn saiu com {saida.code})") from None


async def _esperar_subir(srv: uvicorn.Server, tarefa: asyncio.Task[None], onde: str, prazo: float) -> None:
    """Espera ``srv.started``. Se a partida falha ou passa do prazo, desiste com ``RuntimeError``."""
    try:
        with anyio.fail_after(prazo):
            while not srv.started and not tarefa.done():  # noqa: ASYNC110 - o uvicorn so tem a bandeira, sem evento
                await asyncio.sleep(0.01)
    except TimeoutError:
        srv.should_exit = srv.force_exit = True
        tarefa.cancel()
        await asyncio.wait([tarefa])
        if not tarefa.cancelled():
            tarefa.exception()  # lida: o erro que importa e o do prazo
        raise RuntimeError(f"o servidor de teste nao subiu em {onde} em {prazo} s") from None
    if not srv.started:
        tarefa.result()  # a partida falhou: mostra o erro em vez de seguir com a porta fechada
        raise RuntimeError(f"o servidor de teste nao subiu em {onde}")


@contextlib.asynccontextmanager
async def servidor_paravel(
    app: Any, porta: int | None = None, *, host: str = _LOCAL, prazo: float = PRAZO_PARTIDA, **tls: Any
) -> AsyncIterator[tuple[str, Callable[[], Awaitable[None]]]]:
    """Como ``servidor``, mas devolve tambem como parar o servidor no meio do teste: ``(url, parar)``.

    Depois de ``parar()``, a porta volta a ficar reservada (presa e fechada) ate outro ``servidor`` subir nela
    ou este bloco terminar.
    """
    porta = porta_livre() if porta is None else porta
    onde = f"{host}:{porta}"
    sock = _abrir(host, porta)
    config = uvicorn.Config(app, host=host, port=porta, log_level="warning", lifespan="on", **tls)
    srv = uvicorn.Server(config)
    tarefa = asyncio.create_task(_servir(srv, sock, onde))
    de_novo: socket.socket | None = None

    async def encerrar() -> None:
        srv.should_exit = True
        try:
            await tarefa
        finally:
            sock.close()  # o uvicorn so fecha o socket se chegou a subir

    async def parar() -> None:
        nonlocal de_novo
        if tarefa.done():
            return
        await encerrar()
        if host == _LOCAL and de_novo is None:
            for reaproveitar in (False, True):  # com conexoes em TIME_WAIT, so com SO_REUSEADDR
                with contextlib.suppress(OSError):
                    de_novo = _reservar(porta, reaproveitar=reaproveitar)
                    _guardar(de_novo)
                    break

    try:
        await _esperar_subir(srv, tarefa, onde, prazo)
    except BaseException:
        if not tarefa.done():  # este teste foi cancelado no meio da partida: o servidor nao fica para tras
            srv.should_exit = srv.force_exit = True
            tarefa.cancel()
        sock.close()
        raise
    esquema = "https" if tls.get("ssl_certfile") else "http"
    try:
        yield f"{esquema}://{host}:{porta}", parar
    finally:
        try:
            if not tarefa.done():
                await encerrar()
        finally:
            if de_novo is not None and _tirar_se(porta, de_novo):
                de_novo.close()  # se outro servidor ja subiu nela, o socket e dele


@contextlib.asynccontextmanager
async def servidor(
    app: Any, porta: int | None = None, *, host: str = _LOCAL, prazo: float = PRAZO_PARTIDA, **tls: Any
) -> AsyncIterator[str]:
    """Sobe ``app`` com uvicorn e devolve a URL base (http://127.0.0.1:<porta>).

    Sem ``porta``, usa uma porta livre. Com uma porta de ``porta_livre``, escuta no proprio socket da reserva
    (``serve(sockets=[...])``): a porta nunca fica solta entre a escolha e o servidor. Noutro ``host``, a reserva
    sai e o bind e no endereco pedido. So entrega a URL com o servidor no ar; se a partida falha, ou nao termina
    em ``prazo`` segundos, levanta ``RuntimeError`` com o endereco.

    ``tls`` vai direto ao ``uvicorn.Config`` (``ssl_certfile``, ``ssl_keyfile``, ``ssl_ca_certs``,
    ``ssl_cert_reqs``); com certificado, a URL sai em https.
    """
    async with servidor_paravel(app, porta, host=host, prazo=prazo, **tls) as (url, _):
        yield url


@contextlib.asynccontextmanager
async def cliente_mcp(url: str, chave: str | None = None, *, modo: str = VERSAO_MCP) -> AsyncIterator[Client]:
    """Cliente oficial fixado na versao (padrao 2026-07-28), sem callbacks automaticos."""
    cabecalhos = {"authorization": f"Bearer {chave}"} if chave else {}
    http = httpx2.AsyncClient(headers=cabecalhos, follow_redirects=False, timeout=10)
    async with http, Client(streamable_http_client(url, http_client=http), mode=modo) as c:
        yield c


def pedido_2026(metodo: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    """Monta um pedido JSON-RPC cru na versao 2026-07-28, com o envelope em ``_meta``."""
    p = dict(params or {})
    p["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": VERSAO_MCP,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "teste-mcp-claro", "version": "0"},
    }
    return {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": p}


def cabecalhos_2026(metodo: str, nome: str | None = None, chave: str | None = None) -> dict[str, str]:
    h = {
        "content-type": "application/json",
        "accept": "application/json",
        "mcp-protocol-version": VERSAO_MCP,
        "mcp-method": metodo,
    }
    if nome:
        h["mcp-name"] = nome
    if chave:
        h["authorization"] = f"Bearer {chave}"
    return h


@contextlib.contextmanager
def sistema_geracao_antiga(*, tentativas: int = 300, pausa: float = 0.2) -> Any:
    """Sobe o exemplo da geracao antiga (kit 1.30.0) num ambiente separado do uv e devolve a URL MCP.

    So entrega a URL depois que o exemplo respondeu um GET. Espera ``tentativas`` GETs (cada um com 0,5 s de
    prazo), com ``pausa`` segundos entre eles: com o padrao, pelo menos um minuto, com folga para o uv montar o
    ambiente. Se o exemplo cai ou nao responde nesse tempo, o teste falha aqui, com o fim do que ele escreveu no
    stderr.
    """
    import os
    import shutil
    import subprocess
    import sys
    import time
    from pathlib import Path

    import pytest

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv nao encontrado")
    raiz = Path(__file__).resolve().parents[1]
    porta = soltar(porta_livre())  # quem abre a porta e o subprocesso
    ambiente = os.environ | {"PORTA": str(porta), "HOST": "127.0.0.1"}
    proc = subprocess.Popen(  # noqa: S603 - comando fixo de teste
        [
            uv,
            "run",
            "--no-project",
            "--python",
            sys.executable,
            "--with",
            "mcp==1.30.0",
            "python",
            str(raiz / "exemplos/geracao_antiga/servidor.py"),
        ],
        env=ambiente,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def fim_do_erro() -> str:
        """Para o exemplo (se ainda vive) e devolve o fim do stderr dele."""
        proc.terminate()
        try:
            _, erro = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, erro = proc.communicate()
        return (erro or b"").decode(errors="replace")[-2000:]

    try:
        for _ in range(tentativas):
            try:
                httpx2.get(f"http://127.0.0.1:{porta}/", timeout=0.5)
                break
            except httpx2.TransportError:
                if proc.poll() is not None:
                    pytest.fail(f"o exemplo antigo nao subiu: {fim_do_erro()}")
                time.sleep(pausa)
        else:  # vivo, mas sem responder: seguir com a URL so faria o teste seguinte falhar longe da causa
            pytest.fail(
                f"o exemplo antigo nao respondeu em http://127.0.0.1:{porta}/ depois de {tentativas} tentativas: "
                f"{fim_do_erro()}"
            )
        yield f"http://127.0.0.1:{porta}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
