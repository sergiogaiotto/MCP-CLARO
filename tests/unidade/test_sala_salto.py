"""O salto despachante -> sala de visitas, do lado do despachante (G3a, U8; criticas A2 e B6).

- sala fora (conexao recusada antes de enviar): ``nao_enviado`` e a linha ``sala_fora``;
- qualquer outra falha da sala (caiu depois de receber, 500, corpo fora do contrato): ``duvida``;
- ``nao_enviado``, ``duvida`` e ``recusado_http`` do terceiro atravessam a sala iguais, com o ``status_http``;
- sem sala, um terceiro nunca sai pelo despachante (nem com o cliente local injetado, fora de desenvolvimento e
  teste); o app monta a sala a partir de ``VISITS_INTERNAL_URL`` e da a mesma ligacao a porta de saida.
"""

from __future__ import annotations

import asyncio
import ssl
import uuid
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from mcp_claro.cadastro.modelos import FerramentaVitrine, Sistema
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.despachante.servico import Despachante, interpretar, linha_da_sala, resposta_da_listagem
from mcp_claro.ligacao import cliente, ponto, sala_contrato
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa

pytestmark = pytest.mark.anyio

MOTIVO = PREFIXO_META + "motivo"


def _sistema(origem: str = "terceiro", tipo_chave: str = "por_pessoa") -> Sistema:
    return Sistema(
        uuid.uuid4(),
        f"sis-{origem}",
        "https://t.exemplo/mcp",
        origem,
        tipo_chave,
        1,
        "ativo",
        VERSAO_MCP,  # type: ignore[arg-type]
    )


def _ferramenta(sistema: Sistema) -> FerramentaVitrine:
    return FerramentaVitrine(
        id=uuid.uuid4(),
        sistema=sistema,
        nome_original="ler",
        versao=1,
        nome_vitrine="ler_x",
        titulo=None,
        descricao="Ler.",
        esquema_entrada={"type": "object"},
        esquema_saida=None,
        etiquetas={},
        classe="le",
        risco="baixo",
    )


async def _chave() -> str:
    return "chave-ana"


async def _pela_sala(sala: str) -> Resposta:
    return await ClienteSala(sala, tls=None).chamar_ferramenta(
        "https://t.exemplo/mcp", _chave, None, "ler", {}, prazo=2
    )


def _sala_que_responde(resposta: Response) -> Starlette:
    async def rota(pedido: Request) -> Response:
        await pedido.body()
        return resposta

    return Starlette(routes=[Route(sala_contrato.ROTA_CHAMAR, rota, methods=["POST"])])


def _corpo(**campos: Any) -> Response:
    return Response(sala_contrato.RespostaChamar(**campos).para_json(), media_type="application/json")


async def test_sala_fora_e_nao_enviado_com_a_linha_sala_fora() -> None:
    r = await _pela_sala("http://127.0.0.1:1")  # porta que nunca escuta
    assert (r.tipo, r.excecao) == ("nao_enviado", sala_contrato.SALA_FORA)
    leitura = interpretar(r, _ferramenta(_sistema()), com_chave_da_pessoa=True)
    assert leitura.sala == "sala_fora"
    assert leitura.resultado["_meta"][MOTIVO] == "sala_fora"
    assert leitura.resultado["content"][0]["text"].startswith("A sala de visitas nao esta disponivel; nada foi")
    assert ("sala_fora", {"onde": "leitura"}) in leitura.contadores
    assert leitura.marcar_vencida is False


async def test_sala_que_cai_depois_de_receber_e_duvida() -> None:
    recebidos: list[bytes] = []

    async def atender(leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        recebidos.append(await leitor.read(65536))
        escritor.close()

    srv = await asyncio.start_server(atender, "127.0.0.1", 0)
    try:
        r = await _pela_sala(f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}")
    finally:
        srv.close()
        await srv.wait_closed()
    assert recebidos
    leitura = interpretar(r, _ferramenta(_sistema()), com_chave_da_pessoa=True)
    assert (r.tipo, leitura.sala) == ("duvida", None)
    assert leitura.resultado["_meta"][MOTIVO] == "falha_duvida"


@pytest.mark.parametrize(
    "resposta",
    [
        PlainTextResponse("erro interno do filho", status_code=500),
        Response(b'{"contrato": 1, "tipo": "resultado"}', media_type="application/json"),
        Response(b'{"contrato": 1, "tipo": "nao_enviado", "extra": true}', media_type="application/json"),
        Response(b"nao e json", media_type="application/json"),
    ],
    ids=["500", "resultado_sem_resultado", "campo_a_mais", "nao_e_json"],
)
async def test_resposta_da_sala_que_nao_e_200_do_contrato_e_duvida(resposta: Response) -> None:
    async with servidor(_sala_que_responde(resposta)) as sala:
        r = await _pela_sala(sala)
    assert (r.tipo, r.excecao) == ("duvida", sala_contrato.SALA_RESPOSTA_INVALIDA)
    leitura = interpretar(r, _ferramenta(_sistema()), com_chave_da_pessoa=True)
    assert leitura.resultado["_meta"][MOTIVO] == "falha_duvida"
    assert leitura.sala is None


@pytest.mark.parametrize(
    ("campos", "motivo", "sala", "vencida"),
    [
        ({"tipo": "nao_enviado", "excecao": "ConnectError"}, "falha_nao_enviado", None, False),
        ({"tipo": "duvida", "status_http": (200,)}, "falha_duvida", None, False),
        ({"tipo": "recusado_http", "status_http": (401,)}, "chave_recusada", None, True),
        ({"tipo": "recusado_http", "status_http": (307,)}, "sala_desvio_recusado", "sala_desvio_recusado", False),
        (
            {"tipo": "nao_enviado", "excecao": cliente.ENDERECO_RECUSADO},
            "sala_endereco_recusado",
            "sala_endereco_recusado",
            False,
        ),
    ],
    ids=["nao_enviado", "duvida", "401", "desvio", "endereco"],
)
async def test_classificacao_do_terceiro_atravessa_a_sala_igual(
    campos: dict[str, Any], motivo: str, sala: str | None, vencida: bool
) -> None:
    async with servidor(_sala_que_responde(_corpo(**campos))) as base:
        r = await _pela_sala(base)
    assert r.tipo == campos["tipo"]
    assert r.status_http == tuple(campos.get("status_http", ()))  # o status nao se perde no salto
    leitura = interpretar(r, _ferramenta(_sistema()), com_chave_da_pessoa=True)
    assert leitura.resultado["_meta"][MOTIVO] == motivo
    assert leitura.sala == sala
    assert leitura.marcar_vencida is vencida


async def test_sem_sala_o_terceiro_nao_sai_e_a_leitura_conta_sala_fora() -> None:
    r = await ponto.SEM_SALA.chamar_ferramenta("https://t.exemplo/mcp", _chave, None, "ler", {})
    leitura = interpretar(r, _ferramenta(_sistema()), com_chave_da_pessoa=False)
    assert leitura.sala == "sala_fora"
    assert leitura.resultado["isError"] is True


def _despachante(**kw: Any) -> Despachante:
    engine = create_async_engine("postgresql+asyncpg://ninguem@127.0.0.1:1/nada")  # nunca conecta aqui
    return Despachante(engine, CifraFalsa(), **kw)


@pytest.mark.parametrize("ambiente", ["piloto", "producao"])
def test_fora_de_desenvolvimento_e_teste_o_cliente_local_injetado_nunca_fala_com_terceiro(ambiente: str) -> None:
    local = ponto.ClienteLocal(terceiro=True, ambiente="teste", liberar_locais=True)
    d = _despachante(ambiente=ambiente, local_para_terceiro=local)
    assert d.ligacao(_sistema("terceiro")) is ponto.SEM_SALA
    assert d.ligacao(_sistema("proprio")) is ponto.PROPRIO
    sala = ClienteSala("https://visitas:9446", tls=ssl.create_default_context())
    com_sala = _despachante(ambiente=ambiente, sala=sala, local_para_terceiro=local)
    assert com_sala.ligacao(_sistema("terceiro")) is sala
    assert com_sala.ligacao(_sistema("proprio")) is ponto.PROPRIO


def test_em_teste_o_cliente_local_so_entra_por_injecao() -> None:
    local = ponto.ClienteLocal(terceiro=True, ambiente="teste", liberar_locais=True)
    assert _despachante(ambiente="teste").ligacao(_sistema()) is ponto.SEM_SALA
    assert _despachante(ambiente="teste", local_para_terceiro=local).ligacao(_sistema()) is local


def _parametros(**extra: Any) -> Parametros:
    return Parametros(APP_ENV="teste", **extra)


def test_app_monta_a_sala_e_a_porta_usa_a_mesma_ligacao() -> None:
    engine = create_async_engine("postgresql+asyncpg://ninguem@127.0.0.1:1/nada")
    app = criar_app_despachante(_parametros(VISITS_INTERNAL_URL="http://127.0.0.1:9446"), engine, CifraFalsa())
    despachante, porta = app.state.despachante, app.state.porta
    assert isinstance(despachante.sala, ClienteSala)
    terceiro = _sistema("terceiro")
    assert despachante.ligacao(terceiro) is despachante.sala
    assert porta.ligacao(terceiro) is despachante.sala
    assert porta.ligacao(_sistema("proprio")) is ponto.PROPRIO
    assert porta.terceiros_ligados is False
    sem_sala = criar_app_despachante(_parametros(), engine, CifraFalsa())
    assert sem_sala.state.despachante.sala is None
    assert sem_sala.state.porta.ligacao(terceiro) is ponto.SEM_SALA


def test_sala_por_https_sem_os_certificados_faz_a_partida_recusar() -> None:
    engine = create_async_engine("postgresql+asyncpg://ninguem@127.0.0.1:1/nada")
    with pytest.raises(ConfiguracaoInvalida):
        criar_app_despachante(_parametros(VISITS_INTERNAL_URL="https://visitas:9446"), engine, CifraFalsa())


@pytest.mark.parametrize("origem", ["proprio", "terceiro"])
def test_pedido_que_o_kit_nao_escreve_volta_como_erro_nosso(origem: str) -> None:
    r = Resposta("nao_enviado", excecao="pedido_fundo_demais")  # o cliente fixado (R3a): nada foi enviado
    leitura = interpretar(r, _ferramenta(_sistema(origem)), com_chave_da_pessoa=False)
    assert leitura.resultado["_meta"][MOTIVO] == "pedido_fundo_demais"
    assert "partes demais umas dentro das outras" in leitura.resultado["content"][0]["text"]
    assert leitura.sala is None


def test_pedido_no_resultado_conta_mesmo_sem_a_lista_da_sala() -> None:
    """Uma resposta da sala (ou de um filho tomado) que diga ``pedidos=()`` nao passa um pedido do sistema."""
    pede_link = types.InputRequiredResult.model_validate(
        {
            "resultType": "input_required",
            "inputRequests": {
                "l": {"method": "elicitation/create", "params": {"mode": "url", "message": "x", "url": "https://p.ex"}}
            },
        }
    )
    codigo_antigo = types.ErrorData(code=types.URL_ELICITATION_REQUIRED, message="abra o link")
    for resposta, esperado in (
        (Resposta("resultado", resultado=pede_link, status_http=(200,)), ["link"]),
        (Resposta("erro_rpc", erro=codigo_antigo, status_http=(400,)), ["link_codigo_antigo"]),
    ):
        leitura = interpretar(resposta, _ferramenta(_sistema()), com_chave_da_pessoa=False)
        assert leitura.resultado["_meta"][MOTIVO] == "sistema_pediu_dados"
        assert leitura.resultado["_meta"][PREFIXO_META + "pedidos"] == esperado
        assert "https://p.ex" not in str(leitura.resultado)
        assert leitura.tipo == "leitura_sistema_pediu_dados"


class _ComStatus(sala_contrato.FalhaNaSala):
    """``FalhaNaSala`` com ``status_http``, como o contrato pedido ao G1 para a listagem que recebe um 3xx."""

    def __init__(self, tipo: str, excecao: str | None, status_http: tuple[int, ...]) -> None:
        super().__init__(tipo, excecao)
        self.status_http = status_http


class _LocalComStatus(Exception):
    status_http = (308,)


@pytest.mark.parametrize(
    ("erro", "linha"),
    [
        (sala_contrato.FalhaNaSala("nao_enviado", sala_contrato.SALA_FORA), "sala_fora"),
        (sala_contrato.FalhaNaSala("nao_enviado", cliente.ENDERECO_RECUSADO), "sala_endereco_recusado"),
        (cliente.EnderecoRecusado("http nao"), "sala_endereco_recusado"),
        (_ComStatus("recusado_http", "desvio", (307,)), "sala_desvio_recusado"),
        (_LocalComStatus(), "sala_desvio_recusado"),
        (_ComStatus("recusado_http", None, (401,)), None),
        (sala_contrato.FalhaNaSala("duvida", "ExceptionGroup"), None),
        (ExceptionGroup("kit", [ValueError("x")]), None),
    ],
    ids=["sala_fora", "endereco_pela_sala", "endereco_local", "desvio_sala", "desvio_local", "401", "duvida", "kit"],
)
def test_linha_da_sala_na_listagem(erro: Exception, linha: str | None) -> None:
    assert linha_da_sala(resposta_da_listagem(erro), _sistema()) == linha
    assert linha_da_sala(resposta_da_listagem(erro), _sistema("proprio")) is None
