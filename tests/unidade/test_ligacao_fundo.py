"""Pedido que o kit do cliente fixado nao consegue escrever (R3a, etapa 4): nada sai, e a falha e "nao saiu".

O escritor do pydantic para em 255 niveis: com argumentos de 256 niveis ou mais (contando o objeto dos argumentos),
o kit levantava ``ValueError`` no meio da chamada, com a conexao ja aberta, e a falha virava "duvida" (ou um
``ExceptionGroup``). Agora ``chamar_ferramenta`` monta o JSON antes de qualquer rede e devolve ``nao_enviado`` com
``PEDIDO_FUNDO_DEMAIS``. Pela sala de visitas, o leitor de JSON da sala para antes (200 niveis): o pedido tambem nao
chega ao terceiro, e, se o leitor da sala um dia aceitar mais, a classificacao do cliente atravessa o contrato.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.applications import Starlette

from exemplos import terceiro_falante
from mcp_claro.ligacao import cliente, sala_contrato
from mcp_claro.ligacao.cliente import PEDIDO_FUNDO_DEMAIS, Resposta
from mcp_claro.ligacao.sala_contrato import PEDIDO_FORA_DO_CONTRATO, ROTA_CHAMAR, PedidoChamar, RespostaChamar
from tests.apoio import servidor

pytestmark = pytest.mark.anyio

LOCAL_TERCEIRO = {"terceiro": True, "ambiente": "teste", "liberar_locais": True}
ULTIMO_QUE_SAI = 255
"""Niveis de argumentos (o objeto dos argumentos e o primeiro) que o kit ainda escreve."""


async def _chave() -> str:
    return "chave-ana"


def com_niveis(n: int) -> dict[str, Any]:
    """Argumentos com ``n`` niveis de objetos, um dentro do outro (o objeto dos argumentos e o primeiro)."""
    valor: dict[str, Any] = {}
    for _ in range(n - 1):
        valor = {"a": valor}
    return valor


def niveis(valor: Any) -> int:
    n = 0
    while isinstance(valor, dict):
        n += 1
        valor = next(iter(valor.values()), None)
    return n


@pytest.fixture
async def falante() -> AsyncIterator[tuple[str, Starlette]]:
    app = terceiro_falante.criar()
    async with servidor(app) as base:
        yield f"{base}/mcp", app


def test_os_niveis_do_teste_sao_os_que_o_teste_diz() -> None:
    assert niveis(com_niveis(300)) == 300
    assert niveis(com_niveis(ULTIMO_QUE_SAI)) == ULTIMO_QUE_SAI


@pytest.mark.parametrize("regras", [{}, LOCAL_TERCEIRO], ids=["proprio", "terceiro"])
async def test_pedido_fundo_demais_nao_sai_e_e_nao_enviado(
    falante: tuple[str, Starlette], regras: dict[str, Any]
) -> None:
    url, app = falante
    r = await cliente.chamar_ferramenta(url, _chave, None, "ler_mural", com_niveis(300), prazo=3, **regras)
    assert r == Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)
    assert PEDIDO_FUNDO_DEMAIS == "pedido_fundo_demais"
    assert app.state.recebidos == []


@pytest.mark.parametrize("regras", [{}, LOCAL_TERCEIRO], ids=["proprio", "terceiro"])
async def test_o_que_o_kit_escreve_continua_saindo(falante: tuple[str, Starlette], regras: dict[str, Any]) -> None:
    # O limite e o do kit, nao um teto novo: com 255 niveis o pedido sai (o falante pede a chave e responde 401);
    # com 256, o kit ja nao escreveria, e nada sai.
    url, app = falante
    r = await cliente.chamar_ferramenta(url, _chave, None, "ler_mural", com_niveis(ULTIMO_QUE_SAI), prazo=3, **regras)
    assert r.tipo != "nao_enviado"
    assert len(app.state.recebidos) == 1
    r = await cliente.chamar_ferramenta(url, _chave, None, "ler_mural", com_niveis(ULTIMO_QUE_SAI + 1), **regras)
    assert r == Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)
    assert len(app.state.recebidos) == 1


async def test_pedido_fundo_demais_nem_procura_o_nome_do_terceiro(monkeypatch: pytest.MonkeyPatch) -> None:
    # Antes de qualquer rede: o JSON e montado antes da conferencia do endereco (que procura o nome no DNS).
    procurados: list[str] = []

    async def procurar(endereco: str, *args: Any) -> str | None:
        procurados.append(endereco)
        return None

    monkeypatch.setattr(cliente, "_endereco_do_terceiro", procurar)
    r = await cliente.chamar_ferramenta(
        "https://t.exemplo/mcp", _chave, None, "x", com_niveis(300), terceiro=True, ambiente="producao"
    )
    assert r == Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)
    assert procurados == []


async def test_pela_sala_o_pedido_fundo_demais_nao_chega_ao_terceiro(falante: tuple[str, Starlette]) -> None:
    # O leitor de JSON da sala (pydantic) para em 200 niveis, antes do kit: pedido fora do contrato, nada sai.
    url, app = falante
    corpo = json.dumps({"contrato": 1, "endereco": url, "nome": "ler_mural", "argumentos": com_niveis(300)}).encode()
    status, resposta = await sala_contrato.atender(ROTA_CHAMAR, corpo, ambiente="teste", liberar_locais=True)
    r = sala_contrato.ler_resposta_chamar(status, resposta)
    assert (r.tipo, r.excecao) == ("nao_enviado", PEDIDO_FORA_DO_CONTRATO)
    assert app.state.recebidos == []


async def test_pela_sala_com_o_leitor_dela_sem_limite_a_classificacao_do_cliente_atravessa(
    falante: tuple[str, Starlette], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Se o leitor da sala aceitar mais niveis (outra versao do pydantic), o pedido chega ao cliente fixado na sala,
    # que nao o escreve: nao_enviado com PEDIDO_FUNDO_DEMAIS, pelo contrato, ate o despachante.
    url, app = falante

    def ler_sem_limite(corpo: bytes) -> PedidoChamar:
        return PedidoChamar.model_validate(json.loads(corpo))

    monkeypatch.setattr(sala_contrato.PedidoChamar, "model_validate_json", ler_sem_limite)
    pedido = {"contrato": 1, "endereco": url, "chave": "k", "nome": "ler_mural", "argumentos": com_niveis(300)}
    status, resposta = await sala_contrato.atender(
        ROTA_CHAMAR, json.dumps(pedido).encode(), ambiente="teste", liberar_locais=True
    )
    assert sala_contrato.ler_resposta_chamar(status, resposta) == Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)
    assert app.state.recebidos == []


def test_o_nome_da_excecao_cabe_no_vocabulario_do_contrato_da_sala() -> None:
    corpo = RespostaChamar.de_resposta(Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)).para_json()
    assert sala_contrato.ler_resposta_chamar(200, corpo) == Resposta("nao_enviado", excecao=PEDIDO_FUNDO_DEMAIS)
