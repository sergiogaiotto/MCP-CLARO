"""Numeros que a entrada nao consegue conferir ou guardar, pela entrada real (Fase 3, G4r): nunca erro interno.

O pedido vai em JSON cru (``Mundo.chamar_cru``). O leitor de JSON do kit aceita ``NaN``, ``Infinity`` e inteiros de
ate 4.300 digitos. Antes, cada caso abaixo dava -32603 (erro interno):

- ``multipleOf`` fracionario com inteiro enorme ou infinito (``OverflowError``) ou com NaN (``ValueError``), em
  leitura e acao, proprio e terceiro: agora ``argumento_fora_do_esquema`` (``PROBLEMA_NUMERO``);
- acao com NaN ou infinito, mesmo sem ``multipleOf`` (o JSON do protocolo e o banco nao guardam): agora
  ``numero_invalido`` (``RECUSA_NUMERO``), em proprio e terceiro.

Num terceiro, o inteiro com mais de ``TETO_DIGITOS`` digitos ja para nos tetos (``argumento_grande_demais``). A
leitura de sistema proprio continua como nas Fases 1 e 2 (D3): sem ``multipleOf``, o pedido segue ao sistema.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.argumentos import PROBLEMA_NUMERO, TETO_DIGITOS
from mcp_claro.saida.pedidos import RECUSA_NUMERO
from mcp_claro.versao import PREFIXO_META
from tests.integracao.test_entrada_terceiros_apoio import Extra, Mundo, subir_mundo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MULTIPLO = {"type": "object", "properties": {"n": {"type": "number", "multipleOf": 0.5}}}
LIVRE = {"type": "object", "properties": {"n": {"type": "number"}}}

EXTRAS = {
    f"{lado}_{classe}_{nome}": Extra(lado == "t", (f"{lado}_{classe}_{nome}_n", classe, "medio", (), False), esquema)
    for lado in ("p", "t")
    for classe in ("le", "muda")
    for nome, esquema in (("multiplo", MULTIPLO), ("livre", LIVRE))
}
"""Leitura e acao, proprio (``p_``) e terceiro (``t_``), com e sem ``multipleOf``."""

ENORME = 10**4000
NAO_FINITOS = {"infinito": math.inf, "menos_infinito": -math.inf, "nan": math.nan}

FORA_DO_ESQUEMA = ("argumento_fora_do_esquema", f"Argumentos fora do esquema revisado: {PROBLEMA_NUMERO}")
NUMERO_INVALIDO = ("numero_invalido", RECUSA_NUMERO)
GRANDE_DEMAIS = (
    "argumento_grande_demais",
    f"Argumentos grandes demais para um servidor de terceiros: n: numero com mais de {TETO_DIGITOS} digitos. "
    "Nada foi enviado.",
)


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado, extras=EXTRAS) as m:
        yield m


async def operacoes(m: Mundo) -> int:
    async with m.dono.connect() as c:
        return int(
            (
                await c.execute(text("SELECT count(*) FROM operacoes WHERE aplicativo_id = :a"), {"a": m.apps["A"]})
            ).scalar_one()
        )


def desfecho(resposta: dict[str, Any]) -> tuple[str, str] | str:
    """("motivo", "mensagem") de uma recusa legivel (-32602), ou "segue" quando o pedido passou. Nunca -32603."""
    if "error" not in resposta:
        assert not resposta["result"].get("isError"), resposta["result"]
        return "segue"
    erro = resposta["error"]
    assert erro["code"] == types.INVALID_PARAMS, erro
    return erro["data"][PREFIXO_META + "motivo"], erro["message"]


def esperado(chave: str, valor: Any) -> tuple[str, str] | str:
    lado, classe, nome = chave.split("_")
    if lado == "t" and isinstance(valor, int):
        return GRANDE_DEMAIS  # os tetos vem antes do esquema
    if nome == "multiplo":
        return FORA_DO_ESQUEMA
    if classe == "muda" and isinstance(valor, float):
        return NUMERO_INVALIDO
    return "segue"


@pytest.mark.parametrize("valor", [ENORME, *NAO_FINITOS.values()], ids=["inteiro_enorme", *NAO_FINITOS])
async def test_numero_que_nao_da_para_conferir_ou_guardar_nunca_vira_erro_interno(mundo: Mundo, valor: Any) -> None:
    m = mundo
    achados = {}
    for chave in EXTRAS:
        status, resposta = await m.chamar_cru("ana", chave, {"n": valor})
        assert status in (200, 400), (chave, status)
        achados[chave] = desfecho(resposta)
    assert achados == {chave: esperado(chave, valor) for chave in EXTRAS}
    # So as acoes que seguiram viraram protocolo: o inteiro enorme num sistema proprio, sem multipleOf.
    assert await operacoes(m) == (1 if valor is ENORME else 0)
    # Cada acao recusada tem a linha dela no extrato, com o motivo.
    frases = [linha.frase for linha in await m.extrato("acao_recusada", pessoa="ana")]
    recusadas = [achados[chave] for chave in EXTRAS if "_muda_" in chave and achados[chave] != "segue"]
    assert len(frases) == len(recusadas)
    for motivo, _ in recusadas:
        assert any(motivo in frase for frase in frases), (motivo, frases)


async def test_numeros_que_dao_para_conferir_seguem_como_antes(mundo: Mundo) -> None:
    m = mundo
    # Inclusive -1e308, cuja divisao por 0,5 passa do maior float: a biblioteca refaz a conta com fracoes exatas.
    # Acoes so com dois valores cada: o teto de fichas por hora do aplicativo e 10.
    for chave in EXTRAS:
        valores = (1.5, 10**TETO_DIGITOS - 1, -1e308) if "_le_" in chave else (10**TETO_DIGITOS - 1, -1e308)
        for valor in valores:
            _, resposta = await m.chamar_cru("ana", chave, {"n": valor})
            assert desfecho(resposta) == "segue", (chave, valor)
