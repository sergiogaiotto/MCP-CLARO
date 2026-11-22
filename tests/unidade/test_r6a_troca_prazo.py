"""R6a-2, sem banco: a troca de chave (OAuth) inteira tem prazo total (``autorizacao.PRAZO_TROCA``, contrato K2).

O prazo de leitura do httpx vale para cada pedaco lido. Um endereco de troca que manda um byte por vez seguraria a
troca sem fim. Aqui, com o prazo encurtado, a troca volta com ``TrocaIndisponivel("prazo")`` logo depois do prazo,
para proprio e para terceiro. A conferencia do endereco de um terceiro vem antes, com o prazo dela.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest

from mcp_claro.despachante import autorizacao
from mcp_claro.ligacao import cliente

pytestmark = pytest.mark.anyio

PRAZO = 0.5


@contextlib.asynccontextmanager
async def troca_a_conta_gotas(chegou: asyncio.Event) -> AsyncIterator[str]:
    """Responde 200 e manda o corpo um byte a cada 0,1 s: o prazo de leitura do httpx (10 s) nunca vence."""

    async def atender(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        try:
            cabecalho = await leitor.readuntil(b"\r\n\r\n")
            tamanho = 0
            for linha in cabecalho.split(b"\r\n"):
                if linha.lower().startswith(b"content-length:"):
                    tamanho = int(linha.split(b":", 1)[1])
            await leitor.readexactly(tamanho)
            chegou.set()
            escritor.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 60000\r\n\r\n")
            while True:
                escritor.write(b" ")
                await escritor.drain()
                await asyncio.sleep(0.1)  # o servidor lento do arranjo, nao a espera do teste
        except ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError:
            pass
        finally:
            escritor.close()

    servidor = await asyncio.start_server(atender, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{servidor.sockets[0].getsockname()[1]}/token"
    finally:
        servidor.close()


def _config(token: str, autenticacao: str = "client_secret_basic") -> autorizacao.ConfigAutorizacao:
    return autorizacao.ConfigAutorizacao.de_json(
        {
            "authorization_endpoint": token.replace("/token", "/authorize"),
            "token_endpoint": token,
            "client_id": "mcp-claro",
            "autenticacao_cliente": autenticacao,
        }
    )


def test_prazo_e_espera_do_contrato() -> None:
    assert autorizacao.PRAZO_TROCA == 15.0
    assert autorizacao.ESPERA_TRAVA_RENOVACAO == 3.0


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
async def test_troca_lenta_volta_no_prazo_total(monkeypatch: pytest.MonkeyPatch, terceiro: bool) -> None:
    monkeypatch.setattr(autorizacao, "PRAZO_TROCA", PRAZO)
    chegou = asyncio.Event()
    async with troca_a_conta_gotas(chegou) as token:
        inicio = anyio.current_time()
        # O teto do proprio teste: sem o prazo da troca, ela nao voltaria nunca.
        with anyio.fail_after(PRAZO + 5), pytest.raises(autorizacao.TrocaIndisponivel, match=r"^prazo$"):
            await autorizacao.trocar(
                _config(token),
                "segredo",
                {"grant_type": "refresh_token", "refresh_token": "r"},
                terceiro=terceiro,
                ambiente="teste",
                liberar_locais=True,
            )
        decorrido = anyio.current_time() - inicio
    assert chegou.is_set()  # o pedido chegou e o corpo vinha vindo: foi o prazo total que cortou
    assert PRAZO <= decorrido < PRAZO + 2


async def test_conferencia_do_endereco_de_terceiro_tem_o_prazo_dela_antes_da_troca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conferencia do endereco de um terceiro vem antes do prazo da troca, com o prazo dela
    (``cliente.PRAZO_CONEXAO``): uma conferencia que nao volta da ``TrocaIndisponivel`` nesse prazo, sem nada enviado.
    No pior caso, a troca de um terceiro leva ``PRAZO_CONEXAO + PRAZO_TROCA``."""
    monkeypatch.setattr(cliente, "PRAZO_CONEXAO", PRAZO)
    nunca = anyio.Event()
    chegou = asyncio.Event()

    async def conferir_sem_fim(*_: Any, **__: Any) -> None:
        await nunca.wait()

    monkeypatch.setattr(cliente, "conferir_endereco_terceiro", conferir_sem_fim)
    async with troca_a_conta_gotas(chegou) as token:
        inicio = anyio.current_time()
        with anyio.fail_after(PRAZO + 5), pytest.raises(autorizacao.TrocaIndisponivel, match=r"^nome_nao_resolvido$"):
            await autorizacao.trocar(_config(token), "segredo", {"grant_type": "refresh_token"}, terceiro=True)
        decorrido = anyio.current_time() - inicio
    assert not chegou.is_set()  # nada foi enviado
    assert PRAZO <= decorrido < PRAZO + 2
