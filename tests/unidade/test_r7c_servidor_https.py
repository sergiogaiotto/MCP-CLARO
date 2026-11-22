"""R7c (pedido 1 do R6c): ``servidor_https`` dos testes das rotinas sobe pela reserva de ``tests.apoio``.

Antes, ele escolhia a porta com um ``_porta_livre`` proprio (bind na porta 0 e fecha), e o uvicorn so abria a porta
depois. Nesse meio tempo, outra sessao de testes na mesma maquina podia abrir o mesmo numero, e o servidor caia com
"address already in use" (``SystemExit``). E ele esperava 200 x 0,02 s e entregava a URL mesmo com o servidor fora
do ar. Aqui: a outra sessao tenta abrir a porta logo antes de o uvicorn partir (o pior momento), e uma partida que
falha (certificado ruim) para o teste em vez de entregar a URL.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
import uvicorn
from starlette.applications import Starlette

from tests.integracao.test_rotinas_apoio import certificado_local, confia_em, servidor_https

pytestmark = pytest.mark.anyio


def _outra_sessao_abre(porta: int) -> socket.socket:
    """O que o uvicorn de outra sessao faz quando escolhe o mesmo numero: bind, com ``SO_REUSEADDR``, e listen."""
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", porta))
        s.listen()
    except OSError:
        s.close()
        raise
    return s


@pytest.fixture
def outra_sessao_na_partida(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Logo antes de cada uvicorn partir (``Server.startup``), outra sessao tenta abrir a porta dele. Devolve o que
    aconteceu a cada tentativa: ``recusada`` (a porta ja estava presa) ou ``entrou``."""
    vistas: list[str] = []
    abertas: list[socket.socket] = []
    original = uvicorn.Server.startup

    async def startup(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        try:
            abertas.append(_outra_sessao_abre(self.config.port))
        except OSError:
            vistas.append("recusada")
        else:
            vistas.append("entrou")
        await original(self, sockets=sockets)

    monkeypatch.setattr(uvicorn.Server, "startup", startup)
    try:
        yield vistas
    finally:
        for s in abertas:
            s.close()


async def test_outra_sessao_nao_toma_a_porta_antes_de_o_servidor_https_abrir(
    tmp_path: Path, outra_sessao_na_partida: list[str]
) -> None:
    cert, chave = certificado_local(tmp_path)
    async with servidor_https(Starlette(), cert, chave) as url:
        assert outra_sessao_na_partida == ["recusada"]
        assert url.startswith("https://127.0.0.1:")
        async with httpx2.AsyncClient(verify=confia_em(cert), trust_env=False, timeout=5) as http:
            assert (await http.get(url + "/")).status_code == 404  # o Starlette vazio, pelo TLS com o certificado


async def test_partida_que_falha_para_o_teste_e_nao_entrega_a_url(tmp_path: Path) -> None:
    ruim = tmp_path / "ruim.pem"
    ruim.write_text("nao e um certificado\n", encoding="utf-8")
    entregue: list[str] = []
    with pytest.raises(ssl.SSLError):
        async with servidor_https(Starlette(), ruim, ruim) as url:
            entregue.append(url)
    assert entregue == [], "entregou a URL de um servidor que nao subiu"
