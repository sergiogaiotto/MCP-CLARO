"""O numero da operacao no pedido (Fase 4, contrato do selo; desenho 2.2, 5.1 e 8; critica B1; ajustes D5).

- ``chamar_ferramenta(chave_operacao=...)`` poe o numero so em ``params._meta[contrato.CHAVE_OPERACAO]``, com o
  prefixo de ``versao.PREFIXO_META`` e o valor recebido; nunca nos argumentos nem em cabecalho;
- sem numero, o corpo e o de antes da Fase 4, byte a byte (o que o kit escreve sozinho);
- valor fora da forma uuid e recusado antes de qualquer rede (nem o nome do terceiro e procurado);
- num terceiro, o transporte deixa passar so essa chave, so com o numero que o despachante mandou e so na forma
  uuid; as outras chaves do prefixo do MCP-CLARO continuam saindo, e ``traceparent`` continua recusado;
- ``Ligacao``, ``ClienteLocal``, ``SemSala`` e ``ClienteSala`` tem o parametro; a sala ainda nao leva o numero ate o
  contrato da sala v2 (G7, etapa 5): ``nao_enviado`` sem abrir a chave nem conexao.
"""

from __future__ import annotations

import inspect
import json
import socket
import uuid
from typing import Any

import httpx2
import mcp_types as types
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_claro.ligacao import cliente, ponto, sala_cliente
from mcp_claro.ligacao.sala_cliente import NUMERO_SEM_SALA_V2, ClienteSala
from mcp_claro.ligacao.sala_contrato import ROTA_CHAMAR, SALA_FORA
from mcp_claro.selo import contrato
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests.apoio import servidor

pytestmark = pytest.mark.anyio

LOCAL = {"terceiro": True, "ambiente": "teste", "liberar_locais": True}
NUMERO = "3f0c9a52-6d1e-4c41-9b7a-2f6c1e0d8a44"
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion",
    "io.modelcontextprotocol/clientInfo",
    "io.modelcontextprotocol/clientCapabilities",
}
ARGUMENTOS = {"para": "caixa-de-teste@exemplo.teste", "texto": "teste do selo"}

NUMEROS_FORA = [
    "",
    "CLR-2026-00000001",
    NUMERO.upper(),
    "{" + NUMERO + "}",
    " " + NUMERO,
    NUMERO + "\n",
    NUMERO[:-1],
    NUMERO.replace("-", ""),
    "urn:uuid:" + NUMERO,
    "3f0c9a52-6d1e-4c41-9b7a-2f6c1e0d8a4٤",  # digito de outra escrita
    uuid.UUID(NUMERO),  # o objeto, e nao o texto: quem chama manda str(envio.chave)
    1,
]


async def _chave() -> str:
    return "chave-ana"


def _gravador(recebidos: list[dict[str, Any]]) -> Starlette:
    """Um sistema que guarda o corpo cru (bytes) e os cabecalhos de cada POST e responde um resultado vazio."""

    async def mcp(pedido: Request) -> Response:
        corpo = await pedido.body()
        recebidos.append({"corpo": corpo, "cabecalhos": list(pedido.headers.items())})
        ident = json.loads(corpo).get("id")
        return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": {"content": [], "resultType": "complete"}})

    return Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])


async def _chamar(**kw: Any) -> list[dict[str, Any]]:
    recebidos: list[dict[str, Any]] = []
    async with servidor(_gravador(recebidos)) as base:
        r = await cliente.chamar_ferramenta(f"{base}/mcp", _chave, None, "enviar_lembrete", dict(ARGUMENTOS), **kw)
    assert r.tipo == "resultado", r
    return recebidos


def _meta(recebido: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = json.loads(recebido["corpo"])["params"]["_meta"]
    return meta


# --- onde o numero vai -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("regras", [{}, LOCAL], ids=["proprio", "terceiro"])
async def test_o_numero_vai_so_no_meta_com_o_nome_exato_e_o_valor_recebido(regras: dict[str, Any]) -> None:
    (recebido,) = await _chamar(chave_operacao=NUMERO, **regras)
    corpo = json.loads(recebido["corpo"])
    meta = corpo["params"]["_meta"]
    assert contrato.CHAVE_OPERACAO == PREFIXO_META + "operacao" == "br.com.claro.mcp-claro/operacao"
    assert meta[contrato.CHAVE_OPERACAO] == NUMERO
    assert set(meta) == ENVELOPE | {contrato.CHAVE_OPERACAO}
    # Nunca nos argumentos, nem em lugar nenhum do corpo fora do _meta, nem em cabecalho.
    assert corpo["params"]["arguments"] == ARGUMENTOS
    assert recebido["corpo"].count(NUMERO.encode()) == 1
    sem_meta = {k: v for k, v in corpo["params"].items() if k != "_meta"}
    assert NUMERO not in json.dumps(corpo | {"params": sem_meta})
    for nome, valor in recebido["cabecalhos"]:
        assert NUMERO not in valor, nome
        assert "operacao" not in nome.lower()


async def test_o_numero_nao_vira_cabecalho_nem_com_esquema_que_marca_cabecalhos() -> None:
    """Os cabecalhos ``Mcp-Param-*`` saem so dos argumentos (``x-mcp-header`` do esquema declarado)."""
    esquema = {
        "type": "object",
        "properties": {"para": {"type": "string", "x-mcp-header": "Para"}, "texto": {"type": "string"}},
    }
    (recebido,) = await _chamar(chave_operacao=NUMERO, esquema_declarado=esquema)
    cabecalhos = dict(recebido["cabecalhos"])
    assert cabecalhos["mcp-param-para"] == ARGUMENTOS["para"]
    assert all(NUMERO not in v for v in cabecalhos.values())


async def test_com_rastro_e_numero_num_proprio_os_dois_vao_no_meta() -> None:
    (recebido,) = await _chamar(chave_operacao=NUMERO, traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01")
    meta = _meta(recebido)
    assert meta[contrato.CHAVE_OPERACAO] == NUMERO
    assert meta["traceparent"].startswith("00-aaaa")


# --- sem numero: o pedido de antes, byte a byte --------------------------------------------------------------------


async def _corpo_do_kit(meta: dict[str, Any] | None) -> bytes:
    """O corpo que o kit escreve sozinho para o mesmo pedido (a referencia de "antes da Fase 4")."""
    recebidos: list[dict[str, Any]] = []
    async with (
        servidor(_gravador(recebidos)) as base,
        cliente.http_para_sistema(_chave) as http,
        streamable_http_client(f"{base}/mcp", http_client=http, terminate_on_close=False) as (leitura, escrita),
        ClientSession(leitura, escrita, client_info=cliente._IDENTIDADE) as sessao,
    ):
        sessao.adopt(cliente._discover(None))
        params = types.CallToolRequestParams(name="enviar_lembrete", arguments=dict(ARGUMENTOS), _meta=meta)
        await sessao.send_request(types.CallToolRequest(params=params), cliente._RESULTADO_CHAMADA)
    (recebido,) = recebidos
    corpo: bytes = recebido["corpo"]
    return corpo


@pytest.mark.parametrize("passa_none", [False, True], ids=["sem_o_parametro", "com_none"])
async def test_sem_numero_o_corpo_e_o_de_antes_byte_a_byte(passa_none: bool) -> None:
    kw: dict[str, Any] = {"chave_operacao": None} if passa_none else {}
    (recebido,) = await _chamar(**kw)
    assert recebido["corpo"] == await _corpo_do_kit(None)
    assert set(_meta(recebido)) == ENVELOPE
    assert b"br.com.claro.mcp-claro" not in recebido["corpo"]


async def test_sem_numero_com_rastro_o_corpo_e_o_de_antes() -> None:
    rastro = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"
    (recebido,) = await _chamar(traceparent=rastro, chave_operacao=None)
    assert recebido["corpo"] == await _corpo_do_kit({"traceparent": rastro})


def test_meta_do_pedido_sem_rastro_nem_numero_e_none() -> None:
    """O kit escreve ``_meta`` so com o envelope quando o MCP-CLARO nao poe nada: nunca uma chave nossa vazia."""
    assert cliente._meta_do_pedido(None, None) is None
    assert cliente._meta_do_pedido("", None) is None
    assert cliente._meta_do_pedido(None, NUMERO) == {contrato.CHAVE_OPERACAO: NUMERO}


# --- numero fora da forma: recusado antes da rede -------------------------------------------------------------------


@pytest.mark.parametrize("numero", NUMEROS_FORA)
@pytest.mark.parametrize("regras", [{}, LOCAL, {"terceiro": True, "ambiente": "piloto"}], ids=["p", "t", "tp"])
async def test_numero_fora_da_forma_e_recusado_antes_da_rede(
    monkeypatch: pytest.MonkeyPatch, numero: Any, regras: dict[str, Any]
) -> None:
    procurados: list[str] = []
    abertas: list[str] = []

    async def getaddrinfo(host: str, porta: Any, **_: Any) -> list[Any]:
        procurados.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", porta))]

    async def chave() -> str:
        abertas.append("aberta")
        return "chave-ana"

    monkeypatch.setattr(cliente.anyio, "getaddrinfo", getaddrinfo)
    recebidos: list[dict[str, Any]] = []
    async with servidor(_gravador(recebidos)) as base:
        with pytest.raises(ValueError, match="numero da operacao"):
            await cliente.chamar_ferramenta(
                f"{base}/mcp", chave, None, "enviar_lembrete", {}, chave_operacao=numero, **regras
            )
    assert recebidos == []
    assert procurados == []
    assert abertas == []


@pytest.mark.parametrize("numero", [NUMERO, str(uuid.uuid4()), "00000000-0000-0000-0000-000000000000"])
def test_numero_na_forma_do_contrato_passa(numero: str) -> None:
    cliente.conferir_numero(numero)
    cliente.conferir_numero(None)


# --- terceiro: so essa chave passa --------------------------------------------------------------------------------


async def test_terceiro_recebe_o_numero_e_so_o_envelope_alem_dele() -> None:
    (recebido,) = await _chamar(chave_operacao=NUMERO, **LOCAL)
    assert set(_meta(recebido)) == ENVELOPE | {contrato.CHAVE_OPERACAO}
    assert dict(recebido["cabecalhos"])["user-agent"] == "mcp-claro"


async def test_terceiro_continua_recusando_traceparent_com_numero() -> None:
    recebidos: list[dict[str, Any]] = []
    async with servidor(_gravador(recebidos)) as base:
        with pytest.raises(ValueError, match="rastro"):
            await cliente.chamar_ferramenta(
                f"{base}/mcp", _chave, None, "x", {}, traceparent="00-x", chave_operacao=NUMERO, **LOCAL
            )
    assert recebidos == []


def _pedido(meta: dict[str, Any]) -> httpx2.Request:
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "_meta": meta}}
    return httpx2.Request("POST", "https://t.exemplo/mcp", json=corpo)


async def _depois_do_transporte(meta: dict[str, Any], *, terceiro: bool, numero: str | None) -> dict[str, Any]:
    enviados: list[httpx2.Request] = []

    def responder(pedido: httpx2.Request) -> httpx2.Response:
        enviados.append(pedido)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    transporte = cliente._TransporteQueClassifica(httpx2.MockTransport(responder), 1024, terceiro=terceiro)
    marca = cliente._TENTATIVA.set(cliente.Tentativa(teto_pedidos=1, numero=numero))
    try:
        await transporte.handle_async_request(_pedido(meta))
    finally:
        cliente._TENTATIVA.reset(marca)
    (enviado,) = enviados
    assert int(enviado.headers["content-length"]) == len(enviado.content)
    saida: dict[str, Any] = json.loads(enviado.content)["params"]["_meta"]
    return saida


_ENVELOPE_CRU = {"io.modelcontextprotocol/protocolVersion": VERSAO_MCP}
_OUTRAS_DO_PREFIXO = {
    PREFIXO_META + "recibo": {"estado": "concluida"},
    PREFIXO_META + "alvo": "teste",
    PREFIXO_META + "selo": 3,
    PREFIXO_META + "operacao_outra": NUMERO,
    PREFIXO_META.upper() + "operacao": NUMERO,
    "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
    "baggage": "usuario=ana",
}


async def test_transporte_do_terceiro_deixa_passar_so_o_numero_mandado() -> None:
    meta = _ENVELOPE_CRU | _OUTRAS_DO_PREFIXO | {contrato.CHAVE_OPERACAO: NUMERO}
    saida = await _depois_do_transporte(meta, terceiro=True, numero=NUMERO)
    assert saida == _ENVELOPE_CRU | {contrato.CHAVE_OPERACAO: NUMERO}


@pytest.mark.parametrize(
    ("valor", "numero"),
    [
        (NUMERO, None),  # o despachante nao mandou numero nesta chamada
        (NUMERO, str(uuid.uuid4())),  # mandou outro
        (NUMERO.upper(), NUMERO.upper()),  # fora da forma, mesmo se fosse o "mandado"
        ("CLR-2026-00000001", "CLR-2026-00000001"),
        ([NUMERO], NUMERO),
        ({"numero": NUMERO}, NUMERO),
    ],
)
async def test_transporte_do_terceiro_tira_numero_que_nao_foi_o_mandado(valor: Any, numero: str | None) -> None:
    meta = _ENVELOPE_CRU | {contrato.CHAVE_OPERACAO: valor}
    assert await _depois_do_transporte(meta, terceiro=True, numero=numero) == _ENVELOPE_CRU


async def test_transporte_do_proprio_deixa_o_meta_como_o_kit_montou() -> None:
    meta = _ENVELOPE_CRU | _OUTRAS_DO_PREFIXO | {contrato.CHAVE_OPERACAO: NUMERO}
    saida = await _depois_do_transporte(meta, terceiro=False, numero=None)
    assert saida == meta


# --- as ligacoes (ponto e sala) -----------------------------------------------------------------------------------


def _parametros(funcao: Any) -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(funcao).parameters)


@pytest.mark.parametrize(
    "funcao",
    [
        ponto.Ligacao.chamar_ferramenta,
        ponto.ClienteLocal.chamar_ferramenta,
        ponto.SemSala.chamar_ferramenta,
        ClienteSala.chamar_ferramenta,
        cliente.chamar_ferramenta,
    ],
    ids=["Ligacao", "ClienteLocal", "SemSala", "ClienteSala", "cliente"],
)
def test_toda_ligacao_tem_o_parametro_do_contrato(funcao: Any) -> None:
    esperado = _parametros(contrato.LigacaoComNumero.chamar_ferramenta)
    vistos = _parametros(funcao)
    for nome, p in esperado.items():
        if nome == "self" and "self" not in vistos:
            continue
        assert nome in vistos, nome
        assert vistos[nome].kind is p.kind, nome
        if p.default is not inspect.Parameter.empty and p.default is not Ellipsis:
            assert vistos[nome].default == p.default, nome
    assert vistos["chave_operacao"].default is None
    assert vistos["chave_operacao"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_cliente_local_passa_o_numero_ao_cliente_fixado() -> None:
    recebidos: list[dict[str, Any]] = []
    async with servidor(_gravador(recebidos)) as base:
        for ligacao in (ponto.PROPRIO, ponto.ClienteLocal(**LOCAL)):
            r = await ligacao.chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {}, chave_operacao=NUMERO)
            assert r.tipo == "resultado"
            with pytest.raises(ValueError, match="numero da operacao"):
                await ligacao.chamar_ferramenta(f"{base}/mcp", _chave, None, "x", {}, chave_operacao="1")
    assert [_meta(x)[contrato.CHAVE_OPERACAO] for x in recebidos] == [NUMERO, NUMERO]


async def test_sem_sala_confere_o_numero_e_nada_sai() -> None:
    r = await ponto.SEM_SALA.chamar_ferramenta("https://t.exemplo/mcp", _chave, None, "x", {}, chave_operacao=NUMERO)
    assert (r.tipo, r.excecao) == ("nao_enviado", SALA_FORA)
    with pytest.raises(ValueError, match="numero da operacao"):
        await ponto.SEM_SALA.chamar_ferramenta("https://t.exemplo/mcp", _chave, None, "x", {}, chave_operacao="x")


async def test_sala_ainda_nao_leva_o_numero_e_nao_abre_a_chave() -> None:
    """Ate o contrato da sala v2 (G7): ``nao_enviado`` com ``NUMERO_SEM_SALA_V2``, nada vai a sala e a chave nao
    abre. Sem numero, a sala recebe o pedido como antes."""
    na_sala: list[dict[str, Any]] = []
    abertas: list[str] = []

    async def chave() -> str:
        abertas.append("aberta")
        return "chave-ana"

    async def chamar(pedido: Request) -> Response:
        na_sala.append(json.loads(await pedido.body()))
        return JSONResponse({"tipo": "duvida"}, status_code=500)

    app = Starlette(routes=[Route(ROTA_CHAMAR, chamar, methods=["POST"])])
    async with servidor(app) as base:
        sala = ClienteSala(base, tls=None, folga=0.5)
        r = await sala.chamar_ferramenta("https://t.exemplo/mcp", chave, None, "x", {}, chave_operacao=NUMERO)
        assert (r.tipo, r.excecao, r.status_http) == ("nao_enviado", NUMERO_SEM_SALA_V2, ())
        assert (na_sala, abertas) == ([], [])
        with pytest.raises(ValueError, match="numero da operacao"):
            await sala.chamar_ferramenta("https://t.exemplo/mcp", chave, None, "x", {}, chave_operacao=NUMERO.upper())
        assert (na_sala, abertas) == ([], [])
        sem_numero = await sala.chamar_ferramenta("https://t.exemplo/mcp", chave, None, "x", {}, prazo=2)
    assert sem_numero.tipo == "duvida"  # a sala respondeu 500: chegou la, como antes da Fase 4
    assert len(na_sala) == 1
    assert "chave_operacao" not in na_sala[0]
    assert abertas == ["aberta"]
    assert sala_cliente.NUMERO_SEM_SALA_V2 not in {SALA_FORA, cliente.ENDERECO_RECUSADO}
