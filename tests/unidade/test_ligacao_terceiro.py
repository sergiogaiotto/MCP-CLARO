"""Regras do cliente fixado para servidores de terceiros (desenho 3.1; critica A3; ajustes D1/A2).

- Rastro (U1 e A3): nem ``traceparent``, nem ``tracestate``, nem ``baggage`` chegam ao terceiro, no corpo cru ou nos
  cabecalhos, mesmo com um provedor OpenTelemetry instalado (o kit cria um span proprio e injeta o contexto dele);
- endereco combinado (U4): so https e IP publico fora de desenvolvimento e teste, conferido antes de conectar;
- sem desvio (U2) e sem cookie (U5), com nome e versao genericos (D11);
- ``ligacao_para``: terceiro so pela sala; sem sala, nenhum pedido MCP sai; cliente local para terceiro so por
  injecao em teste.
"""

from __future__ import annotations

import json
import random
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
import httpx2
import pytest
from mcp.shared import _otel as kit_otel
from opentelemetry import baggage, context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from exemplos import terceiro_falante
from mcp_claro import __version__
from mcp_claro.cadastro.modelos import Sistema
from mcp_claro.ligacao import cliente, ponto
from mcp_claro.ligacao.sala_cliente import FOLGA_PADRAO
from mcp_claro.ligacao.sala_contrato import SALA_FORA
from mcp_claro.versao import VERSAO_MCP
from tests.apoio import servidor

pytestmark = pytest.mark.anyio

LOCAL = {"terceiro": True, "ambiente": "teste", "liberar_locais": True}
RASTRO = ("traceparent", "tracestate", "baggage")


async def _chave() -> str:
    return "chave-ana"


# --- um provedor de rastros de verdade para o kit (a prova da critica, sem o SDK) --------------------------------


class _Tracer(trace.Tracer):
    def start_span(self, name: str, context: Any = None, *args: Any, **kwargs: Any) -> trace.Span:
        pai = trace.get_current_span(context).get_span_context()
        trace_id = pai.trace_id if pai.is_valid else random.getrandbits(128)
        return NonRecordingSpan(SpanContext(trace_id, random.getrandbits(64), False, TraceFlags(1)))

    @contextmanager
    def start_as_current_span(
        self, name: str, context: Any = None, *args: Any, end_on_exit: bool = True, **kwargs: Any
    ) -> Iterator[trace.Span]:
        with trace.use_span(self.start_span(name, context), end_on_exit=end_on_exit) as span:
            yield span


class _Provedor(trace.TracerProvider):
    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _Tracer()


@pytest.fixture
def provedor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instala o provedor como o global do OpenTelemetry e no tracer do kit, e desfaz no fim."""
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", _Provedor())
    monkeypatch.setattr(kit_otel, "_tracer", trace.get_tracer("mcp-python-sdk"))


@contextmanager
def contexto_com_rastro() -> Iterator[None]:
    sc = SpanContext(0x0AF7651916CD43DD8448EB211C80319C, 0xB7AD6B7169203331, True, TraceFlags(1))
    ctx = baggage.set_baggage("usuario", "ana@exemplo.test", context=trace.set_span_in_context(NonRecordingSpan(sc)))
    marca = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(marca)


async def _tudo_com_o_falante(**regras: Any) -> list[dict[str, Any]]:
    app = terceiro_falante.criar()
    async with servidor(app) as base:
        url = f"{base}/mcp"
        with contexto_com_rastro():
            s = await cliente.sondar(url, _chave, prazo=3, **regras)
            await cliente.listar_ferramentas(url, _chave, s.discover_json, **regras)
            r = await cliente.chamar_ferramenta(url, _chave, s.discover_json, "ler_mural", {"assunto": "x"}, **regras)
    assert s.aceito
    assert r.tipo == "resultado"
    return list(app.state.recebidos)


def _sem_rastro(recebidos: list[dict[str, Any]]) -> None:
    assert [json.loads(p["corpo"])["method"] for p in recebidos] == ["server/discover", "tools/list", "tools/call"]
    for p in recebidos:
        assert not {k.lower() for k, _ in p["cabecalhos"]} & set(RASTRO)
        corpo = p["corpo"].lower()
        for chave in RASTRO:
            assert chave not in corpo
        assert "0af7651916cd43dd8448eb211c80319c" not in corpo
        assert "ana@exemplo.test" not in corpo


async def test_terceiro_nao_recebe_rastro_com_provedor_instalado(provedor: None) -> None:
    _sem_rastro(await _tudo_com_o_falante(**LOCAL))


async def test_terceiro_nao_recebe_rastro_sem_provedor() -> None:
    _sem_rastro(await _tudo_com_o_falante(**LOCAL))


async def test_proprio_nao_recebe_o_rastro_de_quem_chamou() -> None:
    """Contexto OpenTelemetry vazio em toda chamada: o rastro e o baggage do despachante nao vao a ninguem."""
    _sem_rastro(await _tudo_com_o_falante())


async def test_com_provedor_o_kit_poe_traceparent_e_so_a_barreira_do_terceiro_tira(provedor: None) -> None:
    """Prova de que o provedor vale: num sistema proprio o kit injeta um traceparent novo (raiz, sem ligacao com
    quem chamou); no terceiro, a barreira do transporte tira do corpo."""
    proprio = await _tudo_com_o_falante()
    assert all("traceparent" in json.loads(p["corpo"])["params"]["_meta"] for p in proprio)
    assert all("0af7651916cd43dd8448eb211c80319c" not in p["corpo"] for p in proprio)
    _sem_rastro(await _tudo_com_o_falante(**LOCAL))


async def test_terceiro_recusa_traceparent_dado() -> None:
    app = terceiro_falante.criar()
    async with servidor(app) as base:
        with pytest.raises(ValueError, match="rastro"):
            await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "ler_mural", {}, traceparent="00-x", **LOCAL)
    assert app.state.recebidos == []


def _pedido(meta: dict[str, Any], **cabecalhos: str) -> httpx2.Request:
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "_meta": meta}}
    return httpx2.Request("POST", "https://t.exemplo/mcp", json=corpo, headers=cabecalhos)


@pytest.mark.parametrize("terceiro", [True, False])
async def test_transporte_tira_rastro_dos_cabecalhos_e_do_meta_do_terceiro(terceiro: bool) -> None:
    enviados: list[httpx2.Request] = []

    def responder(pedido: httpx2.Request) -> httpx2.Response:
        enviados.append(pedido)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    meta = {
        "io.modelcontextprotocol/protocolVersion": VERSAO_MCP,
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "fornecedor=1",
        "baggage": "usuario=ana",
        "Traceparent": "00-maiusculas",
        "outra.chave/qualquer": "x",
    }
    cab = {"traceparent": "00-a", "tracestate": "b", "baggage": "c", "x-outro": "fica"}
    transporte = cliente._TransporteQueClassifica(httpx2.MockTransport(responder), 1024, terceiro=terceiro)
    await transporte.handle_async_request(_pedido(meta, **cab))
    (enviado,) = enviados
    assert not {k.lower() for k in enviado.headers} & set(RASTRO)
    assert enviado.headers["x-outro"] == "fica"
    corpo = json.loads(enviado.content)
    assert int(enviado.headers["content-length"]) == len(enviado.content)
    if terceiro:
        assert corpo["params"]["_meta"] == {"io.modelcontextprotocol/protocolVersion": VERSAO_MCP}
    else:
        assert corpo["params"]["_meta"] == meta  # sistema proprio: o corpo segue como o kit montou


# --- endereco combinado (U4) ---------------------------------------------------------------------------------------


def _resolve_para(monkeypatch: pytest.MonkeyPatch, *ips: str) -> list[str]:
    pedidos: list[str] = []

    async def getaddrinfo(host: str, porta: Any, **_: Any) -> list[Any]:
        pedidos.append(host)
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, porta)) for ip in ips
        ]

    monkeypatch.setattr(cliente.anyio, "getaddrinfo", getaddrinfo)
    return pedidos


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "::1",
        "10.1.2.3",
        "172.16.0.9",
        "172.31.255.1",
        "192.168.0.10",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104 - endereco de teste
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 de 127.0.0.1
        "2002:a00:1::1",  # 6to4 de 10.0.0.1
    ],
)
async def test_nome_que_resolve_para_endereco_nao_publico_e_recusado(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    _resolve_para(monkeypatch, "8.8.8.8", ip)  # basta um endereco nao publico
    with pytest.raises(cliente.EnderecoRecusado):
        await cliente.conferir_endereco_terceiro("https://servidor.fornecedor.exemplo/mcp", "piloto")


@pytest.mark.parametrize("ip", ["8.8.8.8", "2001:4860:4860::8888", "::ffff:8.8.8.8"])
async def test_nome_publico_e_aceito(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    pedidos = _resolve_para(monkeypatch, ip)
    await cliente.conferir_endereco_terceiro("https://servidor.fornecedor.exemplo:8443/mcp", "producao")
    assert pedidos == ["servidor.fornecedor.exemplo"]


@pytest.mark.parametrize(
    "endereco",
    [
        "http://servidor.fornecedor.exemplo/mcp",
        "ftp://servidor.fornecedor.exemplo/mcp",
        "https://ana:senha@servidor.fornecedor.exemplo/mcp",
        "https://ana@servidor.fornecedor.exemplo/mcp",
        "https://servidor.fornecedor.exemplo/mcp#fragmento",
        "https:///mcp",
        "https://servidor.fornecedor.exemplo:99999/mcp",
        "nao e endereco",
        "https://\u2603\u2603.exemplo/mcp",  # o IDNA recusa (o resolvedor levantaria InvalidCodepoint)
        "https://a\u200d.exemplo/mcp",  # juntor de largura zero fora de contexto
        "https://\uff41.exemplo/mcp",  # letra de largura cheia: o httpx nao conectaria a esse nome
        "https://a\x00b.exemplo/mcp",
    ],
)
async def test_endereco_fora_do_combinado_e_recusado_sem_resolver(
    monkeypatch: pytest.MonkeyPatch, endereco: str
) -> None:
    pedidos = _resolve_para(monkeypatch, "8.8.8.8")
    with pytest.raises(cliente.EnderecoRecusado):
        await cliente.conferir_endereco_terceiro(endereco, "piloto")
    assert pedidos == []


async def test_o_nome_conferido_e_o_nome_que_o_cliente_http_conecta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nome internacional: confere a forma ASCII (IDNA) que o httpx procura e conecta, nao outra leitura."""
    pedidos = _resolve_para(monkeypatch, "8.8.8.8")
    endereco = "https://B\u00fccher.Fornecedor.Exemplo/mcp"
    await cliente.conferir_endereco_terceiro(endereco, "piloto")
    assert pedidos == [httpx2.URL(endereco).raw_host.decode()] == ["xn--bcher-kva.fornecedor.exemplo"]


async def test_resolvedor_que_recusa_o_nome_vira_endereco_recusado(monkeypatch: pytest.MonkeyPatch) -> None:
    async def getaddrinfo(host: str, porta: Any, **_: Any) -> list[Any]:
        raise UnicodeError("label invalido")

    monkeypatch.setattr(cliente.anyio, "getaddrinfo", getaddrinfo)
    with pytest.raises(cliente.EnderecoRecusado):
        await cliente.conferir_endereco_terceiro("https://servidor.fornecedor.exemplo/mcp", "piloto")
    r = await cliente.chamar_ferramenta(
        "https://servidor.fornecedor.exemplo/mcp", _chave, None, "x", {}, terceiro=True, ambiente="piloto"
    )
    assert (r.tipo, r.excecao) == ("nao_enviado", cliente.ENDERECO_RECUSADO)


async def test_conferencia_que_levanta_valueerror_e_nao_enviado_e_a_sondagem_nao_levanta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Segunda linha: qualquer ``ValueError`` da conferencia (antes de abrir conexao) e endereco recusado."""

    async def conferir(*_: Any, **__: Any) -> None:
        raise UnicodeError("nome que nenhum leitor aceita")

    monkeypatch.setattr(cliente, "conferir_endereco_terceiro", conferir)
    url = "https://servidor.fornecedor.exemplo/mcp"
    r = await cliente.chamar_ferramenta(url, _chave, None, "x", {}, terceiro=True, ambiente="piloto")
    s = await cliente.sondar(url, _chave, prazo=2, terceiro=True, ambiente="piloto")
    assert (r.tipo, r.excecao) == ("nao_enviado", cliente.ENDERECO_RECUSADO)
    assert (s.desfecho, s.excecao) == ("inalcancavel", cliente.ENDERECO_RECUSADO)


@pytest.mark.parametrize(
    "regras",
    [{"ambiente": "piloto"}, {"ambiente": "teste", "liberar_locais": True}],
    ids=["piloto", "teste_liberado"],
)
@pytest.mark.parametrize("endereco", ["https://\u2603\u2603.exemplo/mcp", "https://a\u200d.exemplo/mcp"])
async def test_nome_que_o_idna_recusa_nao_sai_e_nao_levanta(endereco: str, regras: dict[str, Any]) -> None:
    """Sem o resolvedor falso: o kit e o resolvedor de verdade. Nada sai, e a sondagem nunca levanta."""
    r = await cliente.chamar_ferramenta(endereco, _chave, None, "x", {}, terceiro=True, **regras)
    s = await cliente.sondar(endereco, _chave, prazo=2, terceiro=True, **regras)
    with pytest.raises(cliente.EnderecoRecusado):
        await cliente.listar_ferramentas(endereco, _chave, None, terceiro=True, **regras)
    assert (r.tipo, r.excecao, r.status_http) == ("nao_enviado", cliente.ENDERECO_RECUSADO, ())
    assert (s.desfecho, s.excecao, s.status_http) == ("inalcancavel", cliente.ENDERECO_RECUSADO, ())


async def test_liberacao_de_locais_so_vale_em_desenvolvimento_e_teste() -> None:
    for ambiente in ("teste", "desenvolvimento"):
        await cliente.conferir_endereco_terceiro("http://127.0.0.1:9/mcp", ambiente, liberar_locais=True)
        with pytest.raises(cliente.EnderecoRecusado):  # sem a liberacao explicita, o IP e conferido
            await cliente.conferir_endereco_terceiro("http://127.0.0.1:9/mcp", ambiente)
    for ambiente in ("piloto", "producao", "outro"):
        with pytest.raises(cliente.EnderecoRecusado):
            await cliente.conferir_endereco_terceiro("http://127.0.0.1:9/mcp", ambiente, liberar_locais=True)
        with pytest.raises(cliente.EnderecoRecusado):
            await cliente.conferir_endereco_terceiro("https://127.0.0.1:9/mcp", ambiente, liberar_locais=True)


@pytest.mark.parametrize("ambiente", ["piloto", "producao", "teste"])
async def test_endereco_recusado_nao_abre_conexao(ambiente: str) -> None:
    app = terceiro_falante.criar()
    async with servidor(app) as base:
        url = f"{base}/mcp".replace("http://", "https://") if ambiente != "teste" else f"{base}/mcp"
        regras = {"terceiro": True, "ambiente": ambiente}
        r = await cliente.chamar_ferramenta(url, _chave, None, "ler_mural", {}, **regras)
        s = await cliente.sondar(url, _chave, prazo=2, **regras)
        with pytest.raises(cliente.EnderecoRecusado):
            await cliente.listar_ferramentas(url, _chave, None, **regras)
    assert (r.tipo, r.excecao) == ("nao_enviado", cliente.ENDERECO_RECUSADO)
    assert (s.desfecho, s.excecao, s.aceito) == ("inalcancavel", cliente.ENDERECO_RECUSADO, False)
    assert app.state.recebidos == []


async def test_resolvedor_lento_nao_come_o_prazo_da_chamada(monkeypatch: pytest.MonkeyPatch) -> None:
    """A conferencia do nome tem prazo curto e fixo (o mesmo connect de 3 s), nunca o prazo da chamada.

    Com um resolvedor que nunca responde, chamar, sondar e listar desistem em ``PRAZO_CONEXAO``, sem enviar nada.
    Assim o tempo na sala (nome + conexao + prazo da chamada) cabe no prazo mais a folga que o salto espera.
    """

    async def getaddrinfo(*_: Any, **__: Any) -> list[Any]:
        await anyio.sleep(3600)
        return []

    monkeypatch.setattr(cliente.anyio, "getaddrinfo", getaddrinfo)
    url, prazo, regras = "https://servidor.fornecedor.exemplo/mcp", 8.0, {"terceiro": True, "ambiente": "piloto"}
    teto = cliente.PRAZO_CONEXAO + 1
    inicio = anyio.current_time()
    r = await cliente.chamar_ferramenta(url, _chave, None, "x", {}, prazo=prazo, **regras)
    depois_de_chamar = anyio.current_time()
    s = await cliente.sondar(url, _chave, prazo=prazo, **regras)
    depois_de_sondar = anyio.current_time()
    with anyio.fail_after(teto + 1), pytest.raises(TimeoutError):  # TimeoutError e OSError: nome nao resolvido
        await cliente.listar_ferramentas(url, _chave, None, **regras)
    fim = anyio.current_time()
    assert (r.tipo, r.excecao, r.status_http) == ("nao_enviado", cliente.NOME_NAO_RESOLVIDO, ())
    assert (s.desfecho, s.excecao) == ("inalcancavel", cliente.NOME_NAO_RESOLVIDO)
    assert depois_de_chamar - inicio < teto
    assert depois_de_sondar - depois_de_chamar < teto
    assert fim - depois_de_sondar < teto
    # Nome e conexao, cada um no seu prazo fixo, cabem juntos na folga do salto ate a sala.
    assert 2 * cliente.PRAZO_CONEXAO < FOLGA_PADRAO


async def test_nome_que_nao_resolve_e_nao_enviado() -> None:
    r = await cliente.chamar_ferramenta(
        "https://nao-existe.invalid/mcp", _chave, None, "x", {}, terceiro=True, ambiente="piloto"
    )
    assert (r.tipo, r.excecao) == ("nao_enviado", cliente.NOME_NAO_RESOLVIDO)


# --- desvio (U2), cookie (U5) e nome generico (D11) ---------------------------------------------------------------


def _app_desvio(status: int, destino: str, contagem: dict[str, int]) -> Starlette:
    async def desvio(pedido: Request) -> Response:
        contagem["origem"] = contagem.get("origem", 0) + 1
        return Response(status_code=status, headers={"Location": destino})

    async def alvo(pedido: Request) -> Response:
        contagem["alvo"] = contagem.get("alvo", 0) + 1
        corpo = await pedido.json()
        return JSONResponse({"jsonrpc": "2.0", "id": corpo["id"], "result": {"content": [], "resultType": "complete"}})

    return Starlette(routes=[Route("/mcp", desvio, methods=["POST"]), Route("/mcp/", alvo, methods=["POST"])])


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("regras", [{}, LOCAL], ids=["proprio", "terceiro"])
async def test_desvio_da_mesma_origem_nao_e_seguido(status: int, regras: dict[str, Any]) -> None:
    contagem: dict[str, int] = {}
    async with servidor(_app_desvio(status, "/mcp/", contagem)) as base:
        r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {}, prazo=3, **regras)
        s = await cliente.sondar(f"{base}/mcp", _chave, prazo=3, **regras)
        with pytest.raises(Exception):  # noqa: B017, PT011 - a listagem so precisa nao seguir
            await cliente.listar_ferramentas(f"{base}/mcp", _chave, None, **regras)
    assert (r.tipo, r.status_http) == ("recusado_http", (status,))
    assert s.desfecho == "desvio"
    assert contagem == {"origem": 3}  # nada chegou ao destino do desvio


@pytest.mark.parametrize("regras", [{}, LOCAL], ids=["proprio", "terceiro"])
async def test_desvio_para_outra_origem_nao_chega_ao_destino(regras: dict[str, Any]) -> None:
    destino: dict[str, int] = {}
    async with servidor(_app_desvio(200, "", destino)) as base_destino:
        origem: dict[str, int] = {}
        async with servidor(_app_desvio(307, f"{base_destino}/mcp/", origem)) as base:
            r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {}, prazo=3, **regras)
            s = await cliente.sondar(f"{base}/mcp", _chave, prazo=3, **regras)
            with pytest.raises(Exception):  # noqa: B017, PT011
                await cliente.listar_ferramentas(f"{base}/mcp", _chave, None, **regras)
    assert (r.tipo, s.desfecho) == ("recusado_http", "desvio")
    assert origem == {"origem": 3}
    assert destino == {}


@pytest.mark.parametrize("terceiro", [False, True])
async def test_cliente_de_saida_nao_segue_desvio_nem_le_o_ambiente(terceiro: bool) -> None:
    """``max_redirects=0`` e a linha que garante: o kit segue 307/308 da mesma origem ate esse teto, mesmo com
    ``follow_redirects`` desligado (mcp/shared/_httpx_utils.py, ``stream_within_origin``)."""
    async with cliente.http_para_sistema(_chave, terceiro=terceiro) as http:
        assert http.max_redirects == 0
        assert http.follow_redirects is False
        assert http.trust_env is False


async def test_cookie_do_sistema_nao_volta() -> None:
    vistos: list[dict[str, str]] = []

    async def mcp(pedido: Request) -> Response:
        vistos.append(dict(pedido.headers))
        corpo = await pedido.json()
        cursor = (corpo.get("params") or {}).get("cursor")
        resultado: dict[str, Any] = {"ttlMs": 0, "cacheScope": "private", "tools": [], "resultType": "complete"}
        if not cursor:
            resultado["nextCursor"] = "p2"
        resposta = JSONResponse({"jsonrpc": "2.0", "id": corpo["id"], "result": resultado})
        resposta.set_cookie("sessao", "do-fornecedor", path="/")
        return resposta

    async with servidor(Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])) as base:
        for regras in ({}, LOCAL):
            vistos.clear()
            await cliente.listar_ferramentas(f"{base}/mcp", _chave, None, **regras)
            assert len(vistos) == 2
            assert all("cookie" not in v for v in vistos)


async def test_terceiro_recebe_nome_e_versao_genericos() -> None:
    def identidade(recebidos: list[dict[str, Any]]) -> set[tuple[str, str]]:
        saida = set()
        for p in recebidos:
            info = json.loads(p["corpo"])["params"]["_meta"]["io.modelcontextprotocol/clientInfo"]
            saida.add((dict(p["cabecalhos"])["user-agent"], f"{info['name']}/{info['version']}"))
        return saida

    assert identidade(await _tudo_com_o_falante(**LOCAL)) == {("mcp-claro", "mcp-claro/1")}
    assert identidade(await _tudo_com_o_falante()) == {(f"mcp-claro/{__version__}", f"mcp-claro/{__version__}")}


async def test_terceiro_nao_declara_capacidade_nem_manda_chave_alheia() -> None:
    recebidos = await _tudo_com_o_falante(**LOCAL)
    for p in recebidos:
        meta = json.loads(p["corpo"])["params"]["_meta"]
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}
        assert set(meta) <= {
            "io.modelcontextprotocol/protocolVersion",
            "io.modelcontextprotocol/clientInfo",
            "io.modelcontextprotocol/clientCapabilities",
        }
        assert dict(p["cabecalhos"])["authorization"] == "Bearer chave-ana"


# --- ligacao_para (ajustes D1/A2) ---------------------------------------------------------------------------------


def _sistema(origem: str, endereco: str = "https://t.exemplo/mcp") -> Sistema:
    return Sistema(
        id=uuid.uuid4(),
        nome="s",
        endereco=endereco,
        origem=origem,
        tipo_chave="por_pessoa",
        selo=0,
        situacao="ativo",
        versao_mcp=VERSAO_MCP,
    )


class _SalaFalsa:
    pass


def test_ligacao_para_escolhe_pela_origem() -> None:
    sala: Any = _SalaFalsa()
    local = ponto.ClienteLocal(terceiro=True, ambiente="teste", liberar_locais=True)
    for ambiente in ("desenvolvimento", "teste", "piloto", "producao"):
        assert ponto.ligacao_para(_sistema("proprio"), ambiente=ambiente, sala=sala) is ponto.PROPRIO
        assert ponto.ligacao_para(_sistema("terceiro"), ambiente=ambiente, sala=sala) is sala
        assert ponto.ligacao_para(_sistema("terceiro"), ambiente=ambiente) is ponto.SEM_SALA
    assert ponto.PROPRIO.terceiro is False
    # Cliente local para terceiro so por injecao, e so em desenvolvimento e teste.
    for ambiente in ("desenvolvimento", "teste"):
        assert ponto.ligacao_para(_sistema("terceiro"), ambiente=ambiente, local_para_terceiro=local) is local
    for ambiente in ("piloto", "producao", "qualquer"):
        assert ponto.ligacao_para(_sistema("terceiro"), ambiente=ambiente, local_para_terceiro=local) is ponto.SEM_SALA
    # Origem desconhecida vale como terceiro.
    assert ponto.ligacao_para(_sistema("outra"), ambiente="producao") is ponto.SEM_SALA


async def test_sem_sala_nada_chega_ao_terceiro() -> None:
    app = terceiro_falante.criar()
    async with servidor(app) as base:
        url = f"{base}/mcp"
        ligacao = ponto.ligacao_para(_sistema("terceiro", url), ambiente="piloto")
        r = await ligacao.chamar_ferramenta(url, _chave, None, "ler_mural", {})
        s = await ligacao.sondar(url, _chave)
        with pytest.raises(Exception, match="sala"):
            await ligacao.listar_ferramentas(url, _chave, None)
    assert (r.tipo, r.excecao) == ("nao_enviado", SALA_FORA)
    assert (s.desfecho, s.excecao) == ("inalcancavel", SALA_FORA)
    assert app.state.recebidos == []


async def test_cliente_local_injetado_aplica_as_regras_de_terceiro() -> None:
    app = terceiro_falante.criar()
    local = ponto.ClienteLocal(terceiro=True, ambiente="teste", liberar_locais=True)
    async with servidor(app) as base:
        url = f"{base}/mcp"
        ligacao = ponto.ligacao_para(_sistema("terceiro", url), ambiente="teste", local_para_terceiro=local)
        s = await ligacao.sondar(url, _chave, prazo=3)
        nomes = [f["name"] for f in await ligacao.listar_ferramentas(url, _chave, s.discover_json)]
        r = await ligacao.chamar_ferramenta(url, _chave, s.discover_json, "ler_mural", {"assunto": "x"})
    assert (s.desfecho, r.tipo, nomes) == ("aceito", "resultado", ["ler_mural", "apagar_mural"])
    assert {dict(p["cabecalhos"])["user-agent"] for p in app.state.recebidos} == {"mcp-claro"}
