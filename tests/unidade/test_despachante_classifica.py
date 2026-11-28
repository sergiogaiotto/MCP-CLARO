"""Separacao "nao saiu" x "nao confirmada" no fim do envio (``PortaDeSaida._classificar`` e ``_enviar``).

A regra do plano: so vira "nao saiu" o que comprovadamente nao foi executado (nao chegou, recusado na porta,
erro de protocolo antes de executar). Qualquer duvida vira "nao confirmada", e nada e reenviado. As voltas ao
banco (marcar a chave vencida depois de um 401, a linha da sala no extrato) sao trocadas por falsas aqui.

Fase 3 (G3b): o envio sai sempre por ``ligacao(sistema)``, nunca pelo cliente local direto; o que o sistema pediu
no meio vai ao desfecho so com o nosso vocabulario (sem link nem texto dele); a conta de partes retiradas pela
medida e a unica chave nossa no ``_meta`` guardado; pela sala, so a falha de conexao antes de enviar e "nao saiu".
Argumentos com niveis demais para o kit escrever nao saem por caminho nenhum (nem pela sala): "nao saiu", sem abrir
a chave. O app do despachante monta a porta com a chave da Fase 3 que recebeu.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from exemplos import mal_comportado
from mcp_claro.cadastro.modelos import FerramentaVitrine, Sistema
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import renovacao, servico
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.ligacao import cliente, ponto, sala_contrato
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import estranhos
from mcp_claro.saida import despachante as porta_de_saida
from mcp_claro.saida import fila
from mcp_claro.saida.despachante import MOTIVO_TERCEIROS, PortaDeSaida, linha_da_sala, pedidos_legiveis
from mcp_claro.saida.estados import Estado
from mcp_claro.selo import contrato, respostas
from tests.apoio import porta_livre
from tests.cifra_falsa import CifraFalsa

pytestmark = pytest.mark.anyio

SISTEMA = Sistema(
    id=uuid.uuid4(),
    nome="caixa",
    endereco="http://127.0.0.1:9/mcp",
    origem="proprio",
    tipo_chave="por_pessoa",
    selo=1,
    situacao="ativo",
    versao_mcp="2026-07-28",
)
FERRAMENTA = FerramentaVitrine(
    id=uuid.uuid4(),
    sistema=SISTEMA,
    nome_original="criar_chamado",
    versao=1,
    nome_vitrine="chamado",
    titulo=None,
    descricao="Abre um chamado.",
    esquema_entrada={"type": "object"},
    esquema_saida=None,
    etiquetas={},
    classe="muda",
    risco="medio",
)
TERCEIRO = dataclasses.replace(
    SISTEMA, id=uuid.uuid4(), nome="fornecedor", endereco="https://t.exemplo/mcp", origem="terceiro"
)
FERRAMENTA_T = dataclasses.replace(FERRAMENTA, id=uuid.uuid4(), sistema=TERCEIRO, texto_de_fora=True)
OP: Any = SimpleNamespace(
    pessoa_id=uuid.uuid4(),
    aplicativo_id=uuid.uuid4(),
    argumentos={"titulo": "x"},
    fila_id=1,
    protocolo="MC-TESTE-1",
    nome_vitrine="chamado",
)
ABERTA: Any = SimpleNamespace(segredo="chave", cifrada="cifrada-da-chave")


@pytest.fixture
def vencidas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    marcadas: list[dict[str, Any]] = []

    async def marcar_vencida(_engine: Any, **kw: Any) -> None:
        marcadas.append(kw)

    monkeypatch.setattr(renovacao, "marcar_vencida", marcar_vencida)
    return marcadas


def porta() -> PortaDeSaida:
    return PortaDeSaida(engine=None, cifra=None)  # type: ignore[arg-type]


async def classificar(resposta: cliente.Resposta, aberta: Any = None) -> tuple[Estado, Any, str]:
    estado, corpo, desfecho, _ = await porta()._classificar(OP, FERRAMENTA, resposta, aberta)
    return estado, corpo, desfecho


async def test_resultado_vira_enviada_sem_o_meta_do_sistema(vencidas: list[dict[str, Any]]) -> None:
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="ok")], _meta={"x": 1})
    estado, corpo, desfecho = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert estado == Estado.ENVIADA
    assert corpo is not None
    assert "_meta" not in corpo
    assert "resultType" not in corpo
    assert desfecho == "o sistema respondeu"
    assert vencidas == []


async def test_resultado_com_erro_tambem_saiu() -> None:
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="falhou")], isError=True)
    estado, _, desfecho = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert estado == Estado.ENVIADA
    assert desfecho == "o sistema respondeu com erro"


async def test_input_required_vira_sistema_pediu_dados_sem_segunda_volta() -> None:
    estado, corpo, _ = await classificar(
        cliente.Resposta("resultado", resultado=types.InputRequiredResult(request_state="s1"))
    )
    assert estado == Estado.SISTEMA_PEDIU_DADOS
    assert corpo is None


async def test_nao_enviado_vira_nao_saiu() -> None:
    estado, _, desfecho = await classificar(cliente.Resposta("nao_enviado", excecao="ConnectError"))
    assert estado == Estado.NAO_SAIU
    assert desfecho == "o pedido nao chegou ao sistema"


async def test_401_com_chave_por_pessoa_marca_a_chave_vencida(vencidas: list[dict[str, Any]]) -> None:
    estado, _, desfecho = await classificar(cliente.Resposta("recusado_http", status_http=(401,)), ABERTA)
    assert estado == Estado.NAO_SAIU
    assert "HTTP 401" in desfecho
    assert vencidas == [{"sistema_id": SISTEMA.id, "pessoa_id": OP.pessoa_id, "cifrada": "cifrada-da-chave"}]


async def test_401_sem_chave_por_pessoa_nao_marca_nada(vencidas: list[dict[str, Any]]) -> None:
    estado, _, _ = await classificar(cliente.Resposta("recusado_http", status_http=(401,)), None)
    assert estado == Estado.NAO_SAIU
    assert vencidas == []


async def test_401_com_falha_ao_marcar_a_chave_continua_nao_saiu(monkeypatch: pytest.MonkeyPatch) -> None:
    async def falha(_engine: Any, **_: Any) -> None:
        raise OSError("banco fora do ar")

    monkeypatch.setattr(renovacao, "marcar_vencida", falha)
    estado, _, _ = await classificar(cliente.Resposta("recusado_http", status_http=(401,)), ABERTA)
    assert estado == Estado.NAO_SAIU


@pytest.mark.parametrize("codigos", [(403,), (307,), (302, 401)])
async def test_recusa_na_porta_e_redirecionamento_viram_nao_saiu(
    codigos: tuple[int, ...], vencidas: list[dict[str, Any]]
) -> None:
    estado, _, desfecho = await classificar(cliente.Resposta("recusado_http", status_http=codigos))
    assert estado == Estado.NAO_SAIU
    assert desfecho == f"o sistema recusou na porta (HTTP {','.join(str(s) for s in codigos)})"
    assert vencidas == []  # sem chave por pessoa aberta, nada a marcar


@pytest.mark.parametrize("codigos", [(403,), (307,), (308,), (401, 403)])
async def test_so_o_401_vence_a_chave_por_pessoa(codigos: tuple[int, ...], vencidas: list[dict[str, Any]]) -> None:
    estado, _, _ = await classificar(cliente.Resposta("recusado_http", status_http=codigos), ABERTA)
    assert estado == Estado.NAO_SAIU
    assert vencidas == []


@pytest.mark.parametrize(
    "codigo",
    [
        types.METHOD_NOT_FOUND,
        types.INVALID_PARAMS,
        types.INVALID_REQUEST,
        types.PARSE_ERROR,
        types.MISSING_REQUIRED_CLIENT_CAPABILITY,
    ],
)
async def test_erro_rpc_antes_de_executar_vira_nao_saiu(codigo: int) -> None:
    erro = types.ErrorData(code=codigo, message="recusado")
    estado, _, desfecho = await classificar(cliente.Resposta("erro_rpc", erro=erro))
    assert estado == Estado.NAO_SAIU
    assert desfecho == f"o sistema recusou o pedido (codigo {codigo})"


@pytest.mark.parametrize("codigo", [types.INTERNAL_ERROR, -32000, 1234])
async def test_erro_rpc_depois_de_receber_vira_nao_confirmada(codigo: int) -> None:
    # Erro interno ou do proprio sistema: ele pode ter executado parte do pedido. Nunca "nao saiu".
    erro = types.ErrorData(code=codigo, message="falhou no meio")
    estado, _, desfecho = await classificar(cliente.Resposta("erro_rpc", erro=erro))
    assert estado == Estado.NAO_CONFIRMADA
    assert desfecho == f"o sistema devolveu erro depois de receber (codigo {codigo})"


async def test_erro_rpc_sem_corpo_vira_nao_confirmada() -> None:
    estado, _, _ = await classificar(cliente.Resposta("erro_rpc"))
    assert estado == Estado.NAO_CONFIRMADA


async def test_duvida_vira_nao_confirmada() -> None:
    estado, _, desfecho = await classificar(cliente.Resposta("duvida", excecao="ReadTimeout"))
    assert estado == Estado.NAO_CONFIRMADA
    assert desfecho == "a resposta do sistema nao chegou inteira"


async def test_resultado_vazio_tambem_e_duvida() -> None:
    estado, _, _ = await classificar(cliente.Resposta("resultado", resultado=None))
    assert estado == Estado.NAO_CONFIRMADA


async def test_excecao_no_envio_vira_nao_confirmada(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas: list[str] = []

    async def chamar(*_: Any, **__: Any) -> cliente.Resposta:
        chamadas.append("uma")
        raise RuntimeError("caiu no meio")

    p = PortaDeSaida(engine=None, cifra=None, chamar=chamar)  # type: ignore[arg-type]

    async def chave(*_: Any) -> tuple[str | None, Any, str | None]:
        return "segredo", None, None

    monkeypatch.setattr(p, "_chave", chave)
    estado, corpo, desfecho, _ = await p._enviar(OP, FERRAMENTA, None, None)
    assert estado == Estado.NAO_CONFIRMADA
    assert corpo is None
    assert desfecho == "falha inesperada durante o envio"
    assert chamadas == ["uma"]  # uma chamada so, sem nova tentativa


async def test_sem_chave_nao_sai_e_nao_chama_o_sistema(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas: list[str] = []

    async def chamar(*_: Any, **__: Any) -> cliente.Resposta:
        chamadas.append("uma")
        return cliente.Resposta("nao_enviado")

    p = PortaDeSaida(engine=None, cifra=None, chamar=chamar)  # type: ignore[arg-type]

    async def chave(*_: Any) -> tuple[str | None, Any, str | None]:
        return None, None, "a pessoa ainda nao autorizou o sistema"

    monkeypatch.setattr(p, "_chave", chave)
    estado, _, desfecho, _ = await p._enviar(OP, FERRAMENTA, None, None)
    assert (estado, desfecho) == (Estado.NAO_SAIU, "a pessoa ainda nao autorizou o sistema")
    assert chamadas == []


# --- Fase 3 (G3b) ------------------------------------------------------------------------------------------------


@pytest.fixture
def contados(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    somados: list[tuple[str, dict[str, str]]] = []

    def somar(nome: str, valor: int = 1, **rotulos: str) -> None:
        somados.append((nome, rotulos))

    monkeypatch.setattr(CONTADORES, "somar", somar)
    return somados


def _resultado(modo: str) -> types.InputRequiredResult:
    return types.InputRequiredResult.model_validate(
        {"resultType": "input_required", **mal_comportado._PEDIDOS_EMBUTIDOS[modo]}
    )


def _com_pedidos(resultado: types.CallToolResult | types.InputRequiredResult, *barrados: str) -> cliente.Resposta:
    """A ``Resposta`` como o cliente fixado monta: as categorias saem de ``pedidos_do_sistema``."""
    return cliente.Resposta(
        "resultado",
        resultado=resultado,
        status_http=(200,),
        pedidos=cliente.pedidos_do_sistema(resultado, None, barrados),
    )


TEXTOS_DO_SISTEMA = (mal_comportado.PHISH, mal_comportado.TEXTO_SENHA, "Entre aqui", "Mande a chave", "volta-link")


@pytest.mark.parametrize(
    ("modo", "o_que"),
    [
        ("pede_link", "que a pessoa abrisse um link"),
        ("pede_dados", "dados por formulario"),
        ("pede_amostragem", "uma resposta da IA do aplicativo"),
        ("pede_raizes", "a lista de pastas do aplicativo"),
        ("pede_so_estado", "uma nova volta"),
    ],
)
async def test_pedido_no_meio_da_acao_diz_o_que_sem_link_nem_texto_do_sistema(
    modo: str, o_que: str, vencidas: list[dict[str, Any]]
) -> None:
    estado, corpo, desfecho = await classificar(_com_pedidos(_resultado(modo)), ABERTA)
    assert estado == Estado.SISTEMA_PEDIU_DADOS
    assert corpo is None
    assert desfecho == f"o sistema pediu {o_que} no meio da acao"
    for texto in TEXTOS_DO_SISTEMA:
        assert texto not in desfecho
    assert "http" not in desfecho
    assert vencidas == []


async def test_resultado_normal_com_pedido_barrado_saiu_e_o_desfecho_conta_o_pedido(
    contados: list[tuple[str, dict[str, str]]],
) -> None:
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="Pedido em transito.")])
    resposta = _com_pedidos(resultado, "elicitation/create")
    assert resposta.pedidos == ("fio_antigo:elicitation/create",)
    estado, corpo, desfecho = await classificar(resposta)
    assert estado == Estado.ENVIADA
    assert corpo is not None
    assert corpo["content"] == [{"type": "text", "text": "Pedido em transito."}]
    assert desfecho == "o sistema respondeu; antes pediu algo fora do padrao 2026-07-28 e o MCP-CLARO recusou"
    assert ("pedidos_do_sistema_barrados", {"tipo": "fio_antigo:elicitation/create", "onde": "acao"}) in contados


async def test_resultado_com_erro_e_pedido_barrado_continua_enviada() -> None:
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="falhou")], isError=True)
    estado, _, desfecho = await classificar(_com_pedidos(resultado, "ping", "nao-sei"))
    assert estado == Estado.ENVIADA
    # Pelo fio antigo, qualquer metodo (conhecido ou nao) e o mesmo texto, uma vez so.
    assert desfecho == "o sistema respondeu com erro; antes pediu algo fora do padrao 2026-07-28 e o MCP-CLARO recusou"


async def test_link_pelo_codigo_antigo_continua_nao_confirmada_sem_o_link(vencidas: list[dict[str, Any]]) -> None:
    # D15: o -32042 chega depois de o sistema receber o pedido; nunca "nao saiu".
    erro = types.ErrorData(
        code=types.URL_ELICITATION_REQUIRED,
        message="Abra o link para continuar",
        data={
            "elicitations": [{"mode": "url", "message": "Entre", "url": mal_comportado.PHISH, "elicitationId": "e1"}]
        },
    )
    resposta = cliente.Resposta(
        "erro_rpc", erro=erro, status_http=(200,), pedidos=cliente.pedidos_do_sistema(None, erro, [])
    )
    estado, corpo, desfecho = await classificar(resposta, ABERTA)
    assert (estado, corpo) == (Estado.NAO_CONFIRMADA, None)
    assert desfecho == (
        f"o sistema devolveu erro depois de receber (codigo {types.URL_ELICITATION_REQUIRED}); o sistema pediu"
        " que a pessoa abrisse um link e o MCP-CLARO recusou"
    )
    assert mal_comportado.PHISH not in desfecho
    assert vencidas == []


@pytest.mark.parametrize(
    "resposta",
    [
        # O defeito da Fase 2 (fio antigo, 401 no segundo POST): o cliente de hoje devolve isto.
        cliente.Resposta(
            "duvida",
            erro=types.ErrorData(code=types.INTERNAL_ERROR, message="falhou depois de executar"),
            status_http=(200,),
            pedidos=("fio_antigo:elicitation/create",),
        ),
        cliente.Resposta(
            "erro_rpc", erro=types.ErrorData(code=types.INTERNAL_ERROR, message="falhou"), status_http=(200,)
        ),
        # Defesa: recusa "na porta" com status de sucesso nao e recusa.
        cliente.Resposta(
            "recusado_http", erro=types.ErrorData(code=types.INTERNAL_ERROR, message="x"), status_http=(200,)
        ),
        cliente.Resposta("recusado_http", status_http=(401, 200)),
        cliente.Resposta("recusado_http"),
    ],
)
async def test_status_200_com_erro_interno_nunca_vira_nao_saiu_nem_vence_a_chave(
    resposta: cliente.Resposta, vencidas: list[dict[str, Any]]
) -> None:
    estado, corpo, _ = await classificar(resposta, ABERTA)
    assert (estado, corpo) == (Estado.NAO_CONFIRMADA, None)
    assert vencidas == []


async def test_partes_retiradas_vao_so_na_conta_nossa_do_meta(vencidas: list[dict[str, Any]]) -> None:
    resultado = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="Feito.", _meta={"do.sistema/x": 1}),
            types.ImageContent(type="image", data="aGVsbG8=", mimeType="image/png"),
        ],
        _meta={"do.sistema/rastreio": "abc"},
    )
    estado, corpo, _ = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert estado == Estado.ENVIADA
    assert corpo is not None
    assert corpo["_meta"] == {fila.META_RETIRADAS: 1}
    assert corpo["content"][-1] == {"type": "text", "text": estranhos.aviso_de_retiradas(1)}
    assert all(b["type"] == "text" for b in corpo["content"])
    assert "do.sistema/rastreio" not in json.dumps(corpo)


async def test_bloco_do_sistema_igual_ao_aviso_nao_vira_conta_de_retiradas() -> None:
    # O sistema escreve um bloco igual ao nosso aviso, sem parte nenhuma sem texto: nada foi retirado.
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text=estranhos.aviso_de_retiradas(3))])
    _, corpo, _ = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert corpo is not None
    assert "_meta" not in corpo


@pytest.mark.parametrize(
    ("excecao", "desfecho"),
    [
        (
            cliente.PEDIDO_FUNDO_DEMAIS,
            "o pedido tem niveis demais, um dentro do outro, para o MCP-CLARO escrever; o pedido nao chegou ao sistema",
        ),
        (
            sala_contrato.SALA_FORA,
            "a sala de visitas nao esta disponivel; o pedido nao chegou ao servidor de terceiros",
        ),
        (
            cliente.ENDERECO_RECUSADO,
            "o endereco do servidor de terceiros nao e publico ou nao usa https; o pedido nao chegou ao sistema",
        ),
        (
            sala_contrato.PEDIDO_FORA_DO_CONTRATO,
            "o pedido nao cabe no contrato da sala de visitas; o pedido nao chegou ao sistema",
        ),
        (cliente.NOME_NAO_RESOLVIDO, "o pedido nao chegou ao sistema"),
        (None, "o pedido nao chegou ao sistema"),
    ],
)
async def test_nao_enviado_vira_nao_saiu_com_desfecho_legivel_e_sem_vencer_a_chave(
    excecao: str | None, desfecho: str, vencidas: list[dict[str, Any]]
) -> None:
    estado, corpo, texto = await classificar(cliente.Resposta("nao_enviado", excecao=excecao), ABERTA)
    assert (estado, corpo, texto) == (Estado.NAO_SAIU, None, desfecho)
    assert vencidas == []


@pytest.mark.parametrize(
    ("status", "corpo", "estado"),
    [
        (500, b"", Estado.NAO_CONFIRMADA),
        (502, b'{"tipo": "nao_enviado"}', Estado.NAO_CONFIRMADA),
        (200, b"nao e json", Estado.NAO_CONFIRMADA),
        (200, b'{"tipo": "resultado"}', Estado.NAO_CONFIRMADA),
        (200, b'{"tipo": "nao_enviado", "excecao": "Ponto e virgula;"}', Estado.NAO_CONFIRMADA),
        (200, b'{"tipo": "nao_enviado", "excecao": "EnderecoRecusado"}', Estado.NAO_SAIU),
        (200, b'{"tipo": "recusado_http", "status_http": [307]}', Estado.NAO_SAIU),
    ],
)
async def test_resposta_da_sala_so_e_nao_saiu_com_200_no_contrato(
    status: int, corpo: bytes, estado: Estado, vencidas: list[dict[str, Any]]
) -> None:
    # Critica B6: um 500 da sala ou um corpo fora do contrato e duvida (a sala pode ter chamado o terceiro).
    resposta = sala_contrato.ler_resposta_chamar(status, corpo)
    final, _, _, _ = await porta()._classificar(OP, FERRAMENTA_T, resposta, ABERTA)
    assert final == estado
    assert vencidas == []


async def test_sala_que_nao_atende_e_nao_saiu() -> None:
    # Porta fechada: a conexao com a sala nao se faz, nada foi enviado ao terceiro.
    sala = ClienteSala(f"http://127.0.0.1:{porta_livre()}", tls=None)

    async def obter() -> str | None:
        return "chave"

    resposta = await sala.chamar_ferramenta("https://t.exemplo/mcp", obter, None, "criar_chamado", {}, prazo=2.0)
    assert (resposta.tipo, resposta.excecao) == ("nao_enviado", sala_contrato.SALA_FORA)
    final, _, _, _ = await porta()._classificar(OP, FERRAMENTA_T, resposta, None)
    assert final == Estado.NAO_SAIU


def test_pedidos_legiveis_so_com_o_nosso_vocabulario() -> None:
    # O mesmo vocabulario da leitura (``ligacao.ponto``): a porta e o despachante dizem o mesmo.
    from mcp_claro.despachante import servico
    from mcp_claro.ligacao import ponto

    assert pedidos_legiveis is ponto.pedidos_legiveis is servico.pedidos_legiveis
    assert linha_da_sala is ponto.linha_da_sala is servico.linha_da_sala
    assert pedidos_legiveis([]) == "algo fora do padrao 2026-07-28"
    assert pedidos_legiveis(["link", "link", "link_codigo_antigo"]) == "que a pessoa abrisse um link"
    assert pedidos_legiveis(["formulario", "link", "raizes"]) == (
        "dados por formulario, que a pessoa abrisse um link e a lista de pastas do aplicativo"
    )
    assert pedidos_legiveis(["fio_antigo:roots/list", "fio_antigo:ping"]) == "algo fora do padrao 2026-07-28"
    # Categoria fora do vocabulario (nunca deveria chegar) nao e repetida no texto.
    assert pedidos_legiveis(["<a href=x>clique</a>", "fio_antigo:<b>"]) == "algo fora do padrao 2026-07-28"


@pytest.mark.parametrize(
    ("sistema", "resposta", "tipo"),
    [
        (TERCEIRO, cliente.Resposta("nao_enviado", excecao=sala_contrato.SALA_FORA), "sala_fora"),
        (TERCEIRO, cliente.Resposta("nao_enviado", excecao=cliente.ENDERECO_RECUSADO), "sala_endereco_recusado"),
        (TERCEIRO, cliente.Resposta("recusado_http", status_http=(307,)), "sala_desvio_recusado"),
        (TERCEIRO, cliente.Resposta("recusado_http", status_http=(308,)), "sala_desvio_recusado"),
        (TERCEIRO, cliente.Resposta("recusado_http", status_http=(401,)), None),
        (TERCEIRO, cliente.Resposta("nao_enviado", excecao=cliente.NOME_NAO_RESOLVIDO), None),
        (TERCEIRO, cliente.Resposta("duvida", excecao=sala_contrato.SALA_FORA), None),
        (SISTEMA, cliente.Resposta("recusado_http", status_http=(307,)), None),
        (SISTEMA, cliente.Resposta("nao_enviado", excecao=sala_contrato.SALA_FORA), None),
    ],
)
def test_linha_da_sala_so_para_terceiro(sistema: Sistema, resposta: cliente.Resposta, tipo: str | None) -> None:
    assert linha_da_sala(resposta, sistema) == tipo


class LigacaoFalsa:
    """``Ligacao`` que grava cada chamada (endereco, nome e chave) e devolve a resposta dada."""

    def __init__(self, resposta: cliente.Resposta) -> None:
        self.resposta = resposta
        self.chamadas: list[tuple[str, str, str | None]] = []

    async def chamar_ferramenta(
        self, endereco: str, segredo: cliente.Segredo, discover_json: str | None, nome: str, argumentos: Any, **kw: Any
    ) -> cliente.Resposta:
        self.chamadas.append((endereco, nome, await segredo()))
        assert kw.get("traceparent") is None
        return self.resposta

    async def listar_ferramentas(self, *_: Any) -> list[dict[str, Any]]:
        raise AssertionError("a porta de saida nao lista")

    async def sondar(self, *_: Any, **__: Any) -> cliente.Sondagem:
        raise AssertionError("a porta de saida nao sonda")


class _Conexao:
    pass


class EngineFalso:
    def __init__(self) -> None:
        self.transacoes = 0

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[_Conexao]:
        self.transacoes += 1
        yield _Conexao()


@pytest.fixture
def sem_cliente_local(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """O cliente fixado local, se chamado, so anota: a porta tem de ir pela ligacao."""
    locais: list[str] = []

    async def local(endereco: str, *_: Any, **__: Any) -> cliente.Resposta:
        locais.append(endereco)
        return cliente.Resposta("resultado", resultado=types.CallToolResult(content=[]))

    monkeypatch.setattr(cliente, "chamar_ferramenta", local)
    return locais


@pytest.fixture
def linhas(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    gravadas: list[dict[str, Any]] = []

    async def registrar(_c: Any, **kw: Any) -> int:
        gravadas.append(kw)
        return len(gravadas)

    monkeypatch.setattr(porta_de_saida, "registrar", registrar)
    return gravadas


def _com_chave(p: PortaDeSaida, monkeypatch: pytest.MonkeyPatch) -> None:
    async def chave(*_: Any) -> tuple[str | None, Any, str | None]:
        return "chave-ana", None, None

    monkeypatch.setattr(p, "_chave", chave)


@pytest.mark.parametrize("ferramenta", [FERRAMENTA, FERRAMENTA_T], ids=["proprio", "terceiro"])
async def test_envio_sai_pela_ligacao_do_sistema_e_nunca_pelo_cliente_local(
    monkeypatch: pytest.MonkeyPatch,
    sem_cliente_local: list[str],
    linhas: list[dict[str, Any]],
    ferramenta: FerramentaVitrine,
) -> None:
    falsa = LigacaoFalsa(cliente.Resposta("resultado", resultado=types.CallToolResult(content=[])))
    pedidas: list[Sistema] = []

    def ligacao(sistema: Sistema) -> Any:
        pedidas.append(sistema)
        return falsa

    p = PortaDeSaida(engine=None, cifra=None, ligacao=ligacao)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, _, _, _ = await p._enviar(OP, ferramenta, None, None)
    assert estado == Estado.ENVIADA
    assert pedidas == [ferramenta.sistema]
    assert falsa.chamadas == [(ferramenta.sistema.endereco, "criar_chamado", "chave-ana")]
    assert sem_cliente_local == []
    assert linhas == []


async def test_os_pedidos_do_sistema_vao_do_envio_ao_fim_gravado(
    monkeypatch: pytest.MonkeyPatch, sem_cliente_local: list[str], linhas: list[dict[str, Any]]
) -> None:
    resposta = cliente.Resposta(
        "resultado", resultado=types.CallToolResult(content=[]), pedidos=("fio_antigo:elicitation/create",)
    )
    p = PortaDeSaida(engine=EngineFalso(), cifra=None, ligacao=lambda _s: LigacaoFalsa(resposta))  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, corpo, desfecho, pedidos = await p._enviar(OP, FERRAMENTA_T, None, None)
    assert (estado, pedidos) == (Estado.ENVIADA, ("fio_antigo:elicitation/create",))
    gravados: list[tuple[Estado, tuple[str, ...]]] = []

    async def finalizar(_c: Any, _op: Any, estado: Estado, *_: Any, pedidos: Any = ()) -> bool:
        gravados.append((estado, tuple(pedidos)))
        return True

    monkeypatch.setattr(porta_de_saida.fila, "finalizar", finalizar)
    await p._gravar_fim(OP, estado, corpo, desfecho, pedidos=pedidos)
    assert gravados == [(Estado.ENVIADA, ("fio_antigo:elicitation/create",))]


@pytest.mark.parametrize(
    ("ambiente", "liberar_locais"), [("producao", False), ("teste", True)], ids=["padrao", "teste"]
)
async def test_renovacao_da_chave_por_pessoa_leva_o_ambiente_da_porta(
    monkeypatch: pytest.MonkeyPatch, ambiente: str, liberar_locais: bool
) -> None:
    pedidos: list[dict[str, Any]] = []

    async def chave_pronta(_engine: Any, _cifra: Any, **kw: Any) -> Any:
        pedidos.append(kw)
        return SimpleNamespace(segredo="chave-ana", cifrada="vault:v1:x")

    monkeypatch.setattr(porta_de_saida.renovacao, "chave_pronta", chave_pronta)
    extra = {} if ambiente == "producao" else {"ambiente": ambiente, "liberar_locais": liberar_locais}
    p = PortaDeSaida(engine=None, cifra=None, **extra)  # type: ignore[arg-type]
    segredo, _, sem_chave = await p._chave(OP, FERRAMENTA_T)
    assert (segredo, sem_chave) == ("chave-ana", None)
    assert [(k["ambiente"], k["liberar_locais"]) for k in pedidos] == [(ambiente, liberar_locais)]


async def test_origem_que_nao_se_le_e_nao_saiu_com_a_frase_da_chave(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_claro.despachante.cifra_origem import CifraPorOrigem, OrigemIndisponivel

    async def falha(_: Any) -> str | None:
        raise OSError("sem conexao nova")

    cifra = CifraPorOrigem(propria=None, terceiros=None, origem_de=falha)  # type: ignore[arg-type]

    async def chave_pronta(_engine: Any, c: Any, **kw: Any) -> Any:
        await c.decifrar("vault:v1:x", b"mcp-claro|" + str(kw["sistema_id"]).encode() + b"|p:x")
        raise AssertionError("nao chega aqui")

    monkeypatch.setattr(porta_de_saida.renovacao, "chave_pronta", chave_pronta)
    p = PortaDeSaida(engine=None, cifra=cifra)  # type: ignore[arg-type]
    with pytest.raises(OrigemIndisponivel):
        await cifra.origem(uuid.uuid4())
    assert await p._chave(OP, FERRAMENTA_T) == (None, None, "nao deu para abrir a chave no cofre")


async def test_sem_a_ligacao_do_app_o_terceiro_fica_sem_sala_e_nada_sai(
    monkeypatch: pytest.MonkeyPatch,
    sem_cliente_local: list[str],
    linhas: list[dict[str, Any]],
    contados: list[tuple[str, dict[str, str]]],
) -> None:
    engine = EngineFalso()
    p = PortaDeSaida(engine=engine, cifra=None)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, corpo, desfecho, _ = await p._enviar(OP, FERRAMENTA_T, None, None)
    assert (estado, corpo) == (Estado.NAO_SAIU, None)
    assert "sala de visitas" in desfecho
    assert sem_cliente_local == []  # nunca o cliente local para um terceiro
    assert [(linha["tipo"], linha["campos"], linha["protocolo"]) for linha in linhas] == [
        ("sala_fora", {"sistema": "fornecedor"}, OP.protocolo)
    ]
    assert engine.transacoes == 1
    assert ("sala_fora", {"onde": "acao"}) in contados


async def test_sem_a_ligacao_do_app_o_proprio_vai_pelo_cliente_fixado(
    monkeypatch: pytest.MonkeyPatch, sem_cliente_local: list[str], linhas: list[dict[str, Any]]
) -> None:
    p = PortaDeSaida(engine=None, cifra=None)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, _, _, _ = await p._enviar(OP, FERRAMENTA, None, None)
    assert estado == Estado.ENVIADA
    assert sem_cliente_local == [SISTEMA.endereco]
    assert linhas == []


def test_a_ligacao_de_reserva_e_o_ponto_unico_sem_sala() -> None:
    assert porta_de_saida.ligacao_sem_sala(SISTEMA) is ponto.PROPRIO
    assert porta_de_saida.ligacao_sem_sala(TERCEIRO) is ponto.SEM_SALA


async def test_desvio_de_terceiro_grava_a_linha_da_sala_antes_do_fim(
    monkeypatch: pytest.MonkeyPatch, linhas: list[dict[str, Any]]
) -> None:
    falsa = LigacaoFalsa(cliente.Resposta("recusado_http", status_http=(307,)))
    p = PortaDeSaida(engine=EngineFalso(), cifra=None, ligacao=lambda _s: falsa)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, _, desfecho, _ = await p._enviar(OP, FERRAMENTA_T, None, None)
    assert (estado, desfecho) == (Estado.NAO_SAIU, "o sistema recusou na porta (HTTP 307)")
    assert [linha["tipo"] for linha in linhas] == ["sala_desvio_recusado"]
    assert linhas[0]["dados"] == {"onde": "acao", "status_http": [307]}


async def test_linha_da_sala_que_nao_grava_nao_muda_o_fim(monkeypatch: pytest.MonkeyPatch) -> None:
    async def registrar(_c: Any, **_: Any) -> int:
        raise OSError("banco fora do ar")

    monkeypatch.setattr(porta_de_saida, "registrar", registrar)
    falsa = LigacaoFalsa(cliente.Resposta("nao_enviado", excecao=cliente.ENDERECO_RECUSADO))
    p = PortaDeSaida(engine=EngineFalso(), cifra=None, ligacao=lambda _s: falsa)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, _, _, _ = await p._enviar(OP, FERRAMENTA_T, None, None)
    assert estado == Estado.NAO_SAIU


def test_motivo_de_terceiros_e_o_do_desenho() -> None:
    assert MOTIVO_TERCEIROS == "servidores de terceiros desligados ou servidor sem revisao"


def _fundo(niveis: int) -> dict[str, Any]:
    valor: dict[str, Any] = {"fim": 1}
    for _ in range(niveis):
        valor = {"a": valor}
    return valor


@pytest.mark.parametrize("ferramenta", [FERRAMENTA, FERRAMENTA_T], ids=["proprio", "terceiro"])
async def test_resultado_fundo_demais_saiu_sem_resultado_e_nunca_vira_erro_interno(
    ferramenta: FerramentaVitrine, vencidas: list[dict[str, Any]]
) -> None:
    # 300 niveis no structuredContent: o kit nao escreve (ValueError no model_dump). A resposta chegou inteira.
    resultado = types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")], structured_content=_fundo(300)
    )
    estado, corpo, desfecho, _ = await porta()._classificar(
        OP, ferramenta, cliente.Resposta("resultado", resultado=resultado), ABERTA
    )
    assert (estado, corpo, desfecho) == (Estado.ENVIADA, None, porta_de_saida.RESULTADO_FUNDO_DEMAIS)
    assert vencidas == []


@pytest.mark.parametrize("erro", [RecursionError, ValueError])
async def test_medida_que_nao_da_conta_do_resultado_tambem_fica_enviada_sem_resultado(
    monkeypatch: pytest.MonkeyPatch, erro: type[BaseException]
) -> None:
    def aplicar(*_: Any, **__: Any) -> dict[str, Any]:
        raise erro("fundo demais")

    monkeypatch.setattr(porta_de_saida.medida, "aplicar", aplicar)
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="[[[[")])
    estado, corpo, desfecho = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert (estado, corpo, desfecho) == (Estado.ENVIADA, None, porta_de_saida.RESULTADO_FUNDO_DEMAIS)


async def test_annotations_de_bloco_so_ficam_para_sistema_proprio() -> None:
    resultado = types.CallToolResult(
        content=[types.TextContent(type="text", text="ok", annotations=types.Annotations(audience=["assistant"]))]
    )
    _, proprio, _, _ = await porta()._classificar(
        OP, FERRAMENTA, cliente.Resposta("resultado", resultado=resultado), None
    )
    _, terceiro, _, _ = await porta()._classificar(
        OP, FERRAMENTA_T, cliente.Resposta("resultado", resultado=resultado), None
    )
    assert proprio is not None
    assert terceiro is not None
    assert proprio["content"] == [{"type": "text", "text": "ok", "annotations": {"audience": ["assistant"]}}]
    assert terceiro["content"] == [{"type": "text", "text": "ok"}]


async def test_resultado_que_diz_input_required_e_pedido_de_dados() -> None:
    # Defesa: o tipo do resultado e aberto no kit; "input_required" no resultado nunca vira acao enviada.
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="x")], result_type="input_required")
    estado, corpo, _ = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert (estado, corpo) == (Estado.SISTEMA_PEDIU_DADOS, None)


async def test_pedido_que_a_resposta_nao_listou_e_lido_do_proprio_resultado() -> None:
    # Uma resposta (da sala, por exemplo) sem a lista de pedidos nao esconde o link pedido no resultado.
    resposta = cliente.Resposta("resultado", resultado=_resultado("pede_link"), status_http=(200,))
    assert resposta.pedidos == ()
    estado, _, desfecho = await classificar(resposta)
    assert (estado, desfecho) == (
        Estado.SISTEMA_PEDIU_DADOS,
        "o sistema pediu que a pessoa abrisse um link no meio da acao",
    )


@pytest.mark.parametrize(("folga", "guardado"), [(0, True), (1, False)])
async def test_teto_de_niveis_do_resultado_guardado(folga: int, guardado: bool) -> None:
    # O resultado com o teto exato de niveis e guardado; com um a mais, a acao fica enviada sem o resultado.
    estrutura = _fundo(porta_de_saida.TETO_NIVEIS_RESULTADO - 2 + folga)
    resultado = types.CallToolResult(content=[types.TextContent(type="text", text="ok")], structured_content=estrutura)
    bruto = resultado.model_dump(by_alias=True, exclude_none=True, mode="json")
    assert porta_de_saida.medida.profundidade(bruto) == porta_de_saida.TETO_NIVEIS_RESULTADO + folga
    estado, corpo, desfecho = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert estado == Estado.ENVIADA
    assert (corpo is not None) is guardado
    assert (desfecho == porta_de_saida.RESULTADO_FUNDO_DEMAIS) is not guardado


def test_teto_de_niveis_e_o_mesmo_da_leitura() -> None:
    from mcp_claro.despachante import servico

    assert porta_de_saida.TETO_NIVEIS_RESULTADO == servico.TETO_NIVEIS_RESPOSTA


async def test_pedido_de_dados_fundo_demais_continua_pedido_de_dados() -> None:
    # O tipo do resultado decide antes de escrever: um pedido do sistema com _meta de 300 niveis nunca vira acao
    # enviada (o kit nao o escreveria).
    resultado = types.InputRequiredResult.model_validate(
        {"resultType": "input_required", "requestState": "volta", "_meta": _fundo(300)}
    )
    estado, corpo, desfecho = await classificar(cliente.Resposta("resultado", resultado=resultado))
    assert (estado, corpo, desfecho) == (
        Estado.SISTEMA_PEDIU_DADOS,
        None,
        "o sistema pediu uma nova volta no meio da acao",
    )


# --- Pedido com niveis demais para escrever (verificador do G3b, etapa 4) ---------------------------------------


def _argumentos_com_niveis(n: int) -> dict[str, Any]:
    """Argumentos com ``n`` niveis de objetos (o objeto dos argumentos e o primeiro), como ``test_ligacao_fundo``."""
    valor: dict[str, Any] = {}
    for _ in range(n - 1):
        valor = {"a": valor}
    return valor


def test_teto_de_niveis_do_pedido_e_o_que_o_kit_e_a_sala_escrevem() -> None:
    teto = porta_de_saida.TETO_NIVEIS_PEDIDO
    assert porta_de_saida.medida.profundidade(_argumentos_com_niveis(teto)) == teto
    # O que a porta deixa passar, o cliente fixado escreve; um nivel a mais, nao (e o mesmo limite do kit).
    assert cliente._pedido_que_sai("criar_chamado", _argumentos_com_niveis(teto), None) is not None
    assert cliente._pedido_que_sai("criar_chamado", _argumentos_com_niveis(teto + 1), None) is None
    # E o contrato da sala tambem escreve o que a porta deixa passar.
    pedido = sala_contrato.PedidoChamar(
        endereco="https://t.exemplo/mcp", nome="criar_chamado", argumentos=_argumentos_com_niveis(teto)
    )
    assert pedido.para_json()


def _op_com(argumentos: dict[str, Any]) -> Any:
    return SimpleNamespace(**{**vars(OP), "argumentos": argumentos})


FUNDO_DEMAIS = (
    "o pedido tem niveis demais, um dentro do outro, para o MCP-CLARO escrever; o pedido nao chegou ao sistema"
)


def _chave_contada(p: PortaDeSaida, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    abertas: list[object] = []

    async def chave(*_: Any) -> tuple[str | None, Any, str | None]:
        abertas.append(object())
        return "chave-ana", None, None

    monkeypatch.setattr(p, "_chave", chave)
    return abertas


@pytest.mark.parametrize(("folga", "chamada"), [(0, True), (1, False)])
async def test_pedido_com_niveis_demais_nao_chama_a_ligacao_nem_abre_a_chave(
    monkeypatch: pytest.MonkeyPatch, linhas: list[dict[str, Any]], folga: int, chamada: bool
) -> None:
    falsa = LigacaoFalsa(cliente.Resposta("resultado", resultado=types.CallToolResult(content=[])))
    p = PortaDeSaida(engine=EngineFalso(), cifra=None, ligacao=lambda _s: falsa)  # type: ignore[arg-type]
    abertas = _chave_contada(p, monkeypatch)
    op = _op_com(_argumentos_com_niveis(porta_de_saida.TETO_NIVEIS_PEDIDO + folga))
    estado, corpo, desfecho, _ = await p._enviar(op, FERRAMENTA_T, None, None)
    if chamada:
        assert estado == Estado.ENVIADA
        assert len(falsa.chamadas) == len(abertas) == 1
    else:
        assert (estado, corpo, desfecho) == (Estado.NAO_SAIU, None, FUNDO_DEMAIS)
        assert falsa.chamadas == []
        assert abertas == []
    assert linhas == []


@pytest.mark.parametrize("ferramenta", [FERRAMENTA, FERRAMENTA_T], ids=["proprio", "terceiro_pela_sala"])
async def test_pedido_com_niveis_demais_e_nao_saiu_pelo_cliente_fixado_e_pela_sala(
    monkeypatch: pytest.MonkeyPatch,
    linhas: list[dict[str, Any]],
    vencidas: list[dict[str, Any]],
    ferramenta: FerramentaVitrine,
) -> None:
    # As ligacoes de verdade: o proprio pelo cliente fixado, o terceiro pela sala (que nao atende: se o pedido saisse,
    # o desfecho seria "sala fora"). Com 300 niveis, o ClienteSala levanta um erro ao escrever o pedido, antes de
    # conectar: sem a conferencia da porta, a acao ficava "nao confirmada", embora nada tivesse saido.
    sala = ClienteSala(f"http://127.0.0.1:{porta_livre()}", tls=None)
    p = PortaDeSaida(
        engine=EngineFalso(),  # type: ignore[arg-type]
        cifra=None,  # type: ignore[arg-type]
        ligacao=lambda s: ponto.ligacao_para(s, ambiente="producao", sala=sala),
    )
    abertas = _chave_contada(p, monkeypatch)
    estado, corpo, desfecho, _ = await p._enviar(_op_com(_argumentos_com_niveis(300)), ferramenta, None, None)
    assert (estado, corpo, desfecho) == (Estado.NAO_SAIU, None, FUNDO_DEMAIS)
    assert abertas == []
    assert linhas == []
    assert vencidas == []


def test_app_do_despachante_da_a_porta_a_chave_da_fase_3(monkeypatch: pytest.MonkeyPatch) -> None:
    # A partida recusa a chave ligada (D6) ate o dono decidir; o teste a liga sem a conferencia, como os da entrada.
    monkeypatch.setattr(Parametros, "conferir", lambda self: self)
    engine = create_async_engine("postgresql+asyncpg://ninguem@127.0.0.1:1/nada")
    for ligada in (True, False):
        p = Parametros(APP_ENV="teste", FEATURE_PHASE2_ACT=True, FEATURE_PHASE3_THIRD_PARTY=ligada)
        app = criar_app_despachante(p, engine, CifraFalsa())
        assert app.state.porta.terceiros_ligados is ligada
        assert app.state.despachante.terceiros_ligados is ligada
        assert app.state.porta.ligacao == app.state.despachante.ligacao


# --- Fase 4: o numero do selo, o recibo e o selo da leitura (G3a, etapa 3) ----------------------------------------

NUMERO = str(uuid.uuid4())


class ChamarSemNumero:
    """O chamador falso dos testes antigos: nao tem o parametro ``chave_operacao``."""

    def __init__(self, resposta: cliente.Resposta) -> None:
        self.resposta = resposta
        self.vezes = 0

    async def __call__(
        self,
        endereco: str,
        segredo: Any,
        discover_json: Any,
        nome: str,
        argumentos: Any,
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: Any = None,
    ) -> cliente.Resposta:
        self.vezes += 1
        return self.resposta


async def test_sem_envio_com_numero_o_chamador_antigo_continua_servindo(monkeypatch: pytest.MonkeyPatch) -> None:
    chamar = ChamarSemNumero(cliente.Resposta("resultado", resultado=types.CallToolResult(content=[])))
    p = PortaDeSaida(engine=None, cifra=None, chamar=chamar)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    estado, _, _, _ = await p._enviar(OP, FERRAMENTA, None, None)
    assert (estado, chamar.vezes) == (Estado.ENVIADA, 1)


async def test_com_envio_o_numero_vai_como_chave_operacao(monkeypatch: pytest.MonkeyPatch) -> None:
    vistos: list[Any] = []

    async def chamar(*_: Any, **kw: Any) -> cliente.Resposta:
        vistos.append(kw.get("chave_operacao", "sem"))
        return cliente.Resposta("resultado", resultado=types.CallToolResult(content=[]))

    p = PortaDeSaida(engine=None, cifra=None, chamar=chamar)  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    await p._enviar(OP, FERRAMENTA, None, None)
    p._selos[OP.fila_id] = porta_de_saida.SeloDoEnvio(nivel=2, chave=NUMERO, teste_id=uuid.uuid4())
    await p._enviar(OP, FERRAMENTA, None, None)
    assert vistos == ["sem", NUMERO]


def _recibo(estado: str = "concluida", *, numero: str = NUMERO, erro: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        is_error=erro,
        _meta={contrato.CHAVE_RECIBO: {"contrato": contrato.CONTRATO, "operacao": numero, "estado": estado}},
    )


async def test_o_recibo_e_lido_antes_de_o_meta_sair_e_nunca_vai_ao_resultado(monkeypatch: pytest.MonkeyPatch) -> None:
    resposta = cliente.Resposta("resultado", resultado=_recibo())
    p = PortaDeSaida(engine=None, cifra=None, ligacao=lambda _s: LigacaoFalsa(resposta))  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    p._selos[OP.fila_id] = porta_de_saida.SeloDoEnvio(nivel=3, chave=NUMERO, teste_id=uuid.uuid4())
    estado, corpo, _, _ = await p._enviar(OP, FERRAMENTA, None, None)
    assert estado == Estado.ENVIADA
    assert corpo is not None
    assert "_meta" not in corpo
    assert p._recibos[OP.fila_id] == respostas.Recibo("concluida")


async def test_no_nivel_2_o_recibo_nao_e_lido(monkeypatch: pytest.MonkeyPatch) -> None:
    resposta = cliente.Resposta("resultado", resultado=_recibo())
    p = PortaDeSaida(engine=None, cifra=None, ligacao=lambda _s: LigacaoFalsa(resposta))  # type: ignore[arg-type]
    _com_chave(p, monkeypatch)
    p._selos[OP.fila_id] = porta_de_saida.SeloDoEnvio(nivel=2, chave=NUMERO, teste_id=uuid.uuid4())
    await p._enviar(OP, FERRAMENTA, None, None)
    assert OP.fila_id not in p._recibos


@pytest.mark.parametrize(
    ("resposta", "esperado"),
    [
        (cliente.Resposta("resultado", resultado=_recibo()), respostas.Recibo("concluida")),
        (cliente.Resposta("resultado", resultado=_recibo(erro=True)), respostas.Invalida("incoerente")),
        (
            cliente.Resposta("resultado", resultado=_recibo(numero=str(uuid.uuid4()))),
            respostas.Invalida("outro_numero"),
        ),
        (
            cliente.Resposta("resultado", resultado=types.CallToolResult(content=[])),
            respostas.Invalida("recibo_ausente"),
        ),
        (cliente.Resposta("duvida", excecao="ReadError"), None),
        (cliente.Resposta("nao_enviado"), None),
    ],
    ids=["certo", "incoerente", "outro_numero", "ausente", "sem_resposta", "nao_saiu"],
)
def test_recibo_da_resposta(resposta: cliente.Resposta, esperado: Any) -> None:
    assert porta_de_saida._recibo_da_resposta(resposta, NUMERO) == esperado


async def test_nenhum_numero_vai_a_servidor_de_terceiros() -> None:
    """Ate o contrato da sala v2 (G7), o passo do selo nem le o banco para um terceiro: sem numero, sem linha."""
    selo = await porta()._passo_do_selo(None, OP, FERRAMENTA_T)  # type: ignore[arg-type]
    assert selo is porta_de_saida.SEM_SELO


@pytest.mark.parametrize(
    ("retrato", "nivel"),
    [({"nivel": 2}, 2), (json.dumps({"nivel": 3}), 3), (None, None), ({"nivel": True}, None), ({"nivel": "2"}, None)],
)
def test_nivel_do_retrato_da_ficha(retrato: Any, nivel: int | None) -> None:
    assert porta_de_saida._nivel_do_retrato(retrato) == nivel


@pytest.mark.parametrize("selo", [1, 2, 3])
def test_a_leitura_leva_o_selo_dado_e_nunca_a_coluna_antiga(selo: int) -> None:
    resposta = cliente.Resposta("resultado", resultado=types.CallToolResult(content=[]))
    leitura = servico.interpretar(resposta, FERRAMENTA, com_chave_da_pessoa=False, selo=selo)
    assert leitura.resultado["_meta"][servico.PREFIXO_META + "selo"] == selo
    sem = servico.interpretar(resposta, FERRAMENTA, com_chave_da_pessoa=False)
    assert sem.resultado["_meta"][servico.PREFIXO_META + "selo"] == 1


def test_app_do_despachante_da_a_chave_da_fase_4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Parametros, "conferir", lambda self: self)
    engine = create_async_engine("postgresql+asyncpg://ninguem@127.0.0.1:1/nada")
    for ligada in (True, False):
        p = Parametros(
            APP_ENV="teste", FEATURE_PHASE2_ACT=True, FEATURE_PHASE3_THIRD_PARTY=True, FEATURE_PHASE4_BADGES=ligada
        )
        app = criar_app_despachante(p, engine, CifraFalsa())
        assert app.state.porta.selos_ligados is ligada
        assert app.state.despachante.selos_ligados is ligada
        assert app.state.perguntador.ligacao == app.state.despachante.ligacao


async def test_a_tarefa_dos_avisos_deriva_avisa_e_sobrevive_a_uma_volta_que_falha() -> None:
    from mcp_claro.despachante import app as despachante_app

    passos: list[str] = []

    class Perguntador:
        async def derivar_avisos(self) -> int:
            passos.append("derivar")
            if len(passos) == 1:
                raise RuntimeError("o banco caiu")
            return 0

        async def avisar_bloqueios(self) -> int:
            passos.append("avisar")
            if passos.count("avisar") >= 2:
                raise asyncio.CancelledError
            return 0

    with pytest.raises(asyncio.CancelledError):
        await despachante_app.rodar_avisos_de_bloqueio(Perguntador(), 0.0)  # type: ignore[arg-type]
    assert passos == ["derivar", "derivar", "avisar", "derivar", "avisar"]
