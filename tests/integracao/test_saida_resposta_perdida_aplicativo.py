"""Resposta perdida entre o aplicativo e a entrada: o aplicativo repete o pedido e nada sai duas vezes.

E o cenario do teste publico da discussao sobre repeticao (modelcontextprotocol/modelcontextprotocol, issue
3394): a resposta do primeiro ``tools/call`` se perde depois de o efeito acontecer (o servidor ja terminou e
respondeu), o aplicativo desiste, cancela aquele pedido (na 2026-07-28 por HTTP, o
``notifications/cancelled`` vira o fechamento do proprio POST) e repete uma vez, com os mesmos argumentos e
um id JSON-RPC novo.

Para o teste nao depender da velocidade da maquina, o aplicativo desiste so depois de o cortador engolir a
resposta inteira (``tentar_ate_engolir``): desistir e o mesmo cancelamento que o cliente oficial faz no fim do
prazo dele, e o prazo do cliente (``PRAZO_MAXIMO``) fica so de teto. La, com uma ferramenta que nao repete com
seguranca, o efeito acontecia duas vezes (20 de 20 rodadas). Aqui, a acao e contada no sistema de exemplo
``acoes_caixa``: o efeito tem de acontecer uma vez so, e a repeticao devolve o mesmo protocolo.

O corte e feito pelo intermediario de teste ``tests/cortador.py``, entre o aplicativo e a entrada (o mesmo
que corta entre o despachante e o sistema em ``test_saida_resposta_perdida``). No fim, um teste opcional faz o
mesmo com o Toxiproxy 2.13.0, quando a imagem existe na maquina (ela nao baixa em todo ambiente: ver
docs/decisoes.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from mcp_claro.saida.estados import Estado
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests.apoio import porta_livre
from tests.cortador import Cortador, cortador
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import ambiente_de_acoes, chamadas, confirmar, esperar_estado, meta, operacao, politica
from tests.integracao.test_saida_disputas import esperar, limpar, sem_sobras

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PROTOCOLO = PREFIXO_META + "protocolo"
ESTADO = PREFIXO_META + "estado"
REPETIDO = PREFIXO_META + "repetido"
LONGO = os.environ.get("MCP_CLARO_TESTE_LONGO") == "1"
RODADAS = 20 if LONGO else 3
"""O teste publico rodou 20 vezes; aqui, 20 so nos passeios longos."""
PRAZO_MAXIMO = 60.0
"""Teto do prazo do aplicativo (``read_timeout_seconds``). Nos cortes, ele desiste antes, logo depois de a
resposta ser engolida; com a conexao fechada (``queda``), o erro chega na hora."""
PRAZO_DO_TOXIPROXY = 5.0
"""No teste opcional do Toxiproxy a conexao fica muda sem o cortador ver nada: o aplicativo desiste pelo prazo."""
IMAGEM_TOXIPROXY = "ghcr.io/shopify/toxiproxy:2.13.0"


@pytest.fixture
async def amb(banco_migrado: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    """Arranjo com a porta de saida do proprio app do despachante, como na instalacao."""
    async with ambiente_de_acoes(banco_migrado, monkeypatch) as a:
        await politica(a, CONFIRM_CAP_PER_HOUR=1000, OUTBOX_HOLD_SECONDS=0)
        await sem_sobras(a)
        try:
            yield a
        finally:
            await limpar(a)


@contextlib.asynccontextmanager
async def aplicativo(url: str, cracha: str) -> AsyncIterator[Client]:
    """Cliente oficial fixado na 2026-07-28, uma conexao TCP por pedido (o corte escolhe o pedido certo)."""
    http = httpx2.AsyncClient(
        headers={"authorization": f"Bearer {cracha}"},
        follow_redirects=False,
        timeout=30,
        limits=httpx2.Limits(max_keepalive_connections=0),
    )
    async with http, Client(streamable_http_client(url, http_client=http), mode=VERSAO_MCP) as c:
        yield c


async def limite_para_mensagens(amb: Ambiente, por_dia: int) -> uuid.UUID:
    async with amb.entrada_db.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                    " valido_ate, com_passkey) VALUES (:p, :a, :s, 'enviar_mensagem', CAST(:d AS jsonb), :n,"
                    " now() + interval '1 day', true) RETURNING id"
                ),
                {
                    "p": amb.ana_id,
                    "a": amb.aplicativo_id,
                    "s": amb.sistema_id,
                    "d": json.dumps([{"campo": "destinatario", "valores": ["equipe"]}]),
                    "n": por_dia,
                },
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def operacoes_iguais(amb: Ambiente, argumentos: dict[str, Any]) -> list[str]:
    """Protocolos gravados para estes argumentos da Ana (cada teste usa argumentos unicos)."""
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT protocolo FROM operacoes WHERE pessoa_id = :p AND argumentos = CAST(:a AS jsonb)"),
                    {"p": amb.ana_id, "a": json.dumps(argumentos)},
                )
            ).scalars()
        )


async def tentar(c: Client, nome: str, argumentos: dict[str, Any], *, prazo: float = PRAZO_MAXIMO) -> Any:
    """Uma tentativa do aplicativo. Devolve o resultado, ou a excecao se ele desistiu (prazo ou queda)."""
    try:
        return await c.call_tool(nome, argumentos, read_timeout_seconds=prazo)
    except Exception as erro:  # o aplicativo desiste de qualquer jeito (prazo ou queda)
        return erro


async def tentar_ate_engolir(c: Client, corte: Cortador, nome: str, argumentos: dict[str, Any]) -> Any:
    """Uma tentativa cuja resposta o cortador engole com a conexao muda (``segurar``). O aplicativo desiste so
    depois de a resposta inteira ter sido engolida (o servidor ja respondeu), seja qual for a velocidade da
    maquina: cancela o pedido, como o cliente oficial faz no fim do prazo, e na 2026-07-28 isso fecha o proprio
    POST. Devolve o resultado (se a resposta passou) ou a excecao do cancelamento."""
    engolidas, desistencias = len(corte.engolidas), corte.desistencias
    tarefa = asyncio.create_task(tentar(c, nome, argumentos))
    await esperar(lambda: len(corte.engolidas) > engolidas or tarefa.done(), prazo=PRAZO_MAXIMO)
    if tarefa.done():
        return tarefa.result()
    tarefa.cancel()
    await asyncio.wait([tarefa])
    await esperar(lambda: corte.desistencias > desistencias)  # o cortador viu o POST fechado
    return asyncio.CancelledError("o aplicativo desistiu do pedido")


async def tentar_de_novo_do_zero(url: str, cracha: str, nome: str, argumentos: dict[str, Any]) -> Any:
    """Uma tentativa num cliente novo: a queda da conexao derruba o cliente oficial inteiro (o erro sai do
    grupo de tarefas dele), e o aplicativo volta com outro. Devolve o resultado ou a excecao."""
    try:
        async with aplicativo(url, cracha) as app:
            return await tentar(app, nome, argumentos)
    except BaseException as erro:  # grupo de excecoes do cliente
        if isinstance(erro, asyncio.CancelledError):
            raise
        return erro


def pedidos_jsonrpc(brutos: list[bytes]) -> list[dict[str, Any]]:
    """Corpos JSON-RPC dos pedidos que passaram pelo cortador (um por conexao)."""
    saida = []
    for bruto in brutos:
        _, _, corpo = bruto.partition(b"\r\n\r\n")
        with contextlib.suppress(ValueError):
            valor = json.loads(corpo)
            if isinstance(valor, dict):
                saida.append(valor)
    return saida


@pytest.mark.parametrize("forma", ["prazo", "queda"])
async def test_resposta_perdida_ao_aplicativo_e_repeticao_nao_duplicam_o_efeito(amb: Ambiente, forma: str) -> None:
    """Acao coberta por limite, sem espera: o efeito acontece durante o primeiro pedido, cuja resposta se perde.

    ``prazo``: a conexao fica muda e o aplicativo desiste (como no teste publico, onde desiste pelo prazo), so
    depois de a resposta ter sido engolida. ``queda``: a conexao e fechada sem resposta."""
    await limite_para_mensagens(amb, por_dia=RODADAS + 1)
    nome = amb.vitrine["enviar_mensagem"]
    async with cortador(amb.entrada) as corte:
        corte.modo = "engolir"
        corte.so_se_tiver = b'"tools/call"'
        corte.segurar = forma == "prazo"
        url, cracha = f"{corte.url}/mcp", amb.cracha("pessoa-ana")
        async with contextlib.AsyncExitStack() as pilha:
            app = await pilha.enter_async_context(aplicativo(url, cracha)) if forma == "prazo" else None

            async def pedir(argumentos: dict[str, Any], *, cortado: bool) -> Any:
                if app is not None:
                    if cortado:
                        return await tentar_ate_engolir(app, corte, nome, argumentos)
                    return await tentar(app, nome, argumentos)
                return await tentar_de_novo_do_zero(url, cracha, nome, argumentos)

            for rodada in range(RODADAS):
                antes = chamadas(amb, "enviar_mensagem")
                argumentos = {"destinatario": "equipe", "texto": f"Pedido {forma} {rodada} {uuid.uuid4().hex[:6]}"}
                corte.maximo_de_cortes = corte.cortes + 1

                primeira = await pedir(argumentos, cortado=True)
                assert isinstance(primeira, BaseException), f"rodada {rodada}: a resposta devia se perder"
                assert corte.cortes == rodada + 1
                # O efeito ja aconteceu: a resposta engolida era a do resultado.
                assert chamadas(amb, "enviar_mensagem") == antes + 1, f"rodada {rodada}"
                engolida = corte.engolidas[-1]
                assert b"Feito: enviar_mensagem" in engolida, engolida[:300]
                [protocolo] = await operacoes_iguais(amb, argumentos)
                assert (await operacao(amb, protocolo)).estado == Estado.ENVIADA

                # O aplicativo repete uma vez, com os mesmos argumentos (id JSON-RPC novo).
                segunda = await pedir(argumentos, cortado=False)
                assert not isinstance(segunda, BaseException), f"rodada {rodada}: {segunda!r}"
                assert meta(segunda)[PROTOCOLO] == protocolo
                assert meta(segunda)[REPETIDO] is True
                assert meta(segunda)[ESTADO] == "enviada"
                assert "Feito: enviar_mensagem" in " ".join(b.text for b in segunda.content if hasattr(b, "text"))
                await asyncio.sleep(0.3)
                assert chamadas(amb, "enviar_mensagem") == antes + 1, f"rodada {rodada}: o efeito duplicou"
                assert await operacoes_iguais(amb, argumentos) == [protocolo]

        chamadas_tool = [p for p in pedidos_jsonrpc(corte.pedidos) if p.get("method") == "tools/call"]
        assert len(chamadas_tool) == 2 * RODADAS
        if forma == "prazo":
            ids = [p.get("id") for p in chamadas_tool]
            assert len(set(ids)) == len(ids), "cada tentativa leva um id JSON-RPC novo"
        if forma == "prazo":
            # Cada pedido perdido foi abandonado pelo aplicativo: na 2026-07-28 por HTTP, o
            # ``notifications/cancelled`` vira o fechamento do proprio POST.
            assert corte.desistencias == RODADAS
    assert chamadas(amb, "enviar_mensagem") == RODADAS


async def test_resposta_perdida_da_ficha_a_repeticao_devolve_o_mesmo_protocolo(amb: Ambiente) -> None:
    """Acao com ficha: a resposta "esperando a ficha" se perde, o aplicativo repete antes e depois da ficha."""
    nome = amb.vitrine["criar_chamado"]
    argumentos = {"titulo": f"Ficha perdida {uuid.uuid4().hex[:6]}"}
    async with cortador(amb.entrada) as corte:
        corte.modo = "engolir"
        corte.so_se_tiver = b'"tools/call"'
        corte.segurar = True
        corte.maximo_de_cortes = 1
        async with aplicativo(f"{corte.url}/mcp", amb.cracha("pessoa-ana")) as app:
            assert isinstance(await tentar_ate_engolir(app, corte, nome, argumentos), BaseException)
            [protocolo] = await operacoes_iguais(amb, argumentos)
            de_novo = await tentar(app, nome, argumentos)
            assert meta(de_novo)[PROTOCOLO] == protocolo
            assert meta(de_novo)[ESTADO] == "aguardando_ficha"
            assert meta(de_novo)[REPETIDO] is True
            assert chamadas(amb, "criar_chamado") == 0

            await confirmar(amb, await operacao(amb, protocolo), pessoa_id=amb.ana_id)
            await esperar_estado(amb, protocolo, Estado.ENVIADA)
            depois = await tentar(app, nome, argumentos)
            assert meta(depois)[PROTOCOLO] == protocolo
            assert meta(depois)[ESTADO] == "enviada"
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 1
    assert await operacoes_iguais(amb, argumentos) == [protocolo]


# --- Opcional: o mesmo corte com o Toxiproxy 2.13.0 ----------------------------------------------


def _imagem_toxiproxy() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    r = subprocess.run(  # noqa: S603 - comando fixo de teste
        [docker, "image", "inspect", IMAGEM_TOXIPROXY], capture_output=True, check=False, timeout=30
    )
    return r.returncode == 0


@contextlib.asynccontextmanager
async def toxiproxy(destino: str) -> AsyncIterator[tuple[str, httpx2.AsyncClient]]:
    """Sobe o Toxiproxy na rede da maquina, com um proxy na frente de ``destino``. Devolve (url do proxy, API)."""
    docker = shutil.which("docker")
    assert docker is not None
    api, escuta = porta_livre(), porta_livre()
    nome = f"mcp-claro-toxiproxy-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(
        subprocess.run,
        [
            docker,
            "run",
            "-d",
            "--rm",
            "--name",
            nome,
            "--network",
            "host",
            IMAGEM_TOXIPROXY,
            "-host=127.0.0.1",
            f"-port={api}",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{api}", timeout=10) as http:
            for _ in range(100):
                with contextlib.suppress(httpx2.TransportError):
                    if (await http.get("/version")).status_code == 200:
                        break
                await asyncio.sleep(0.1)
            sem_esquema = destino.split("://", 1)[-1].split("/", 1)[0]
            r = await http.post(
                "/proxies",
                json={"name": "entrada", "listen": f"127.0.0.1:{escuta}", "upstream": sem_esquema, "enabled": True},
            )
            assert r.status_code in (200, 201), r.text
            yield f"http://127.0.0.1:{escuta}", http
    finally:
        await asyncio.to_thread(
            subprocess.run, [docker, "rm", "-f", nome], capture_output=True, check=False, timeout=60
        )


async def test_resposta_perdida_pelo_toxiproxy(amb: Ambiente) -> None:
    if not _imagem_toxiproxy():
        pytest.skip(
            f"a imagem {IMAGEM_TOXIPROXY} nao esta nesta maquina (ghcr.io nao baixa aqui); o mesmo corte roda com"
            " tests/cortador.py nos testes acima"
        )
    await limite_para_mensagens(amb, por_dia=2)
    nome = amb.vitrine["enviar_mensagem"]
    argumentos = {"destinatario": "equipe", "texto": f"Pelo Toxiproxy {uuid.uuid4().hex[:6]}"}
    async with toxiproxy(amb.entrada) as (url, api), aplicativo(f"{url}/mcp", amb.cracha("pessoa-ana")) as app:
        # "timeout" com prazo 0 no caminho de volta: o pedido chega, nada da resposta volta, a conexao fica muda.
        r = await api.post(
            "/proxies/entrada/toxics",
            json={"name": "sem_volta", "type": "timeout", "stream": "downstream", "attributes": {"timeout": 0}},
        )
        assert r.status_code in (200, 201), r.text
        primeira = await tentar(app, nome, argumentos, prazo=PRAZO_DO_TOXIPROXY)
        assert isinstance(primeira, BaseException)
        await esperar(lambda: chamadas(amb, "enviar_mensagem") == 1)  # o efeito aconteceu
        assert (await api.delete("/proxies/entrada/toxics/sem_volta")).status_code in (200, 204)
        segunda = await tentar(app, nome, argumentos)
        assert not isinstance(segunda, BaseException), repr(segunda)
        [protocolo] = await operacoes_iguais(amb, argumentos)
        assert meta(segunda)[PROTOCOLO] == protocolo
        assert meta(segunda)[REPETIDO] is True
    await asyncio.sleep(0.5)
    assert chamadas(amb, "enviar_mensagem") == 1
