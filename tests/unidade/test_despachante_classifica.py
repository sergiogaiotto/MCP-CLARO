"""Separacao "nao saiu" x "nao confirmada" no fim do envio (``PortaDeSaida._classificar`` e ``_enviar``).

A regra do plano: so vira "nao saiu" o que comprovadamente nao foi executado (nao chegou, recusado na porta,
erro de protocolo antes de executar). Qualquer duvida vira "nao confirmada", e nada e reenviado. A unica
volta ao banco (marcar a chave vencida depois de um 401) e trocada por uma falsa aqui.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import mcp_types as types
import pytest

from mcp_claro.cadastro.modelos import FerramentaVitrine, Sistema
from mcp_claro.despachante import renovacao
from mcp_claro.ligacao import cliente
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado

pytestmark = pytest.mark.anyio

SISTEMA = Sistema(
    id=uuid.uuid4(),
    nome="caixa",
    endereco="http://127.0.0.1:9/mcp",
    origem="http://127.0.0.1:9",
    tipo_chave="por_pessoa",
    selo=0,
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
OP: Any = SimpleNamespace(pessoa_id=uuid.uuid4(), argumentos={"titulo": "x"}, fila_id=1)
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
    return await porta()._classificar(OP, FERRAMENTA, resposta, aberta)


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
    estado, corpo, desfecho = await p._enviar(OP, FERRAMENTA, None, None)
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
    estado, _, desfecho = await p._enviar(OP, FERRAMENTA, None, None)
    assert (estado, desfecho) == (Estado.NAO_SAIU, "a pessoa ainda nao autorizou o sistema")
    assert chamadas == []
