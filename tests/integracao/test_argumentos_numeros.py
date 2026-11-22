"""Numeros que a entrada nao consegue conferir ou guardar, pela entrada real (Fase 3, G4r): nunca erro interno.

O pedido vai em JSON cru (``Mundo.chamar_cru``; na leitura com numero nao finito, ``corpo_com_literal``, com o numero
escrito como o aplicativo o escreveu, ``1e400`` inclusive). O leitor de JSON do kit aceita ``NaN``, ``Infinity``,
``1e400`` (infinito) e inteiros de ate 4.300 digitos. Antes, cada caso abaixo dava -32603 (erro interno):

- ``multipleOf`` fracionario com inteiro enorme ou infinito (``OverflowError``) ou com NaN (``ValueError``), em
  leitura e acao, proprio e terceiro: agora ``argumento_fora_do_esquema`` (``PROBLEMA_NUMERO``);
- acao com NaN ou infinito, mesmo sem ``multipleOf`` (o JSON do protocolo e o banco nao guardam): agora
  ``numero_invalido`` (``RECUSA_NUMERO``), em proprio e terceiro;
- leitura com NaN ou infinito, mesmo sem ``multipleOf`` (o pedido ao despachante e JSON sem NaN nem infinito: o
  ``ClienteDespachante`` levantava ``ValueError``): agora ``numero_invalido`` (``RECUSA_NUMERO_LEITURA``), antes da
  catraca, em proprio e terceiro (R3a). O ``LeitorFalso`` escondia esse erro; a prova usa o ``ClienteDespachante`` de
  verdade, com o despachante falso no transporte (``httpx2.MockTransport``).

Num terceiro, o inteiro com mais de ``TETO_DIGITOS`` digitos ja para nos tetos (``argumento_grande_demais``). Num
sistema proprio, o inteiro enorme sem ``multipleOf`` segue como nas Fases 1 e 2 (D3).
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import mcp_types as types
import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.argumentos import PROBLEMA_NUMERO, TETO_DIGITOS
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.entrada.servidor import RECUSA_NUMERO_LEITURA
from mcp_claro.regras import catraca
from mcp_claro.saida.pedidos import RECUSA_NUMERO
from mcp_claro.versao import PREFIXO_META
from tests.apoio import cabecalhos_2026, pedido_2026
from tests.integracao.test_entrada_terceiros_apoio import Extra, Mundo, resultado_do_sistema, subir_mundo

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
NUMERO_INVALIDO_LEITURA = ("numero_invalido", RECUSA_NUMERO_LEITURA)
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


async def leituras_de_hoje(m: Mundo) -> int:
    """Leituras que a Ana gastou hoje na catraca da ordem (uma ordem para todas as ferramentas do mundo)."""
    async with m.dono.connect() as c:
        return await catraca.usadas_hoje(c, m.ordens[("ana", "A")])


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
    if isinstance(valor, float):
        return NUMERO_INVALIDO if classe == "muda" else NUMERO_INVALIDO_LEITURA
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
    # So a leitura que seguiu chegou ao despachante (o leitor falso): a mesma, o inteiro enorme no proprio.
    seguiram = [m.f[chave].id for chave in EXTRAS if "_le_" in chave and achados[chave] == "segue"]
    assert [c.ferramenta_id for c in m.leitor.chamadas] == seguiram
    # E so ela gastou a cota do dia: as recusas vem antes da catraca (o NaN da leitura inclusive).
    assert await leituras_de_hoje(m) == len(seguiram)
    # Cada recusa tem a linha dela no extrato, com o motivo: acao_recusada para a acao, leitura_recusada para a leitura.
    for classe, tipo in (("_muda_", "acao_recusada"), ("_le_", "leitura_recusada")):
        frases = [linha.frase for linha in await m.extrato(tipo, pessoa="ana")]
        recusadas = [achados[chave] for chave in EXTRAS if classe in chave and achados[chave] != "segue"]
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


MARCA = "__numero_como_o_aplicativo_escreveu__"


def corpo_com_literal(nome: str, literal: str) -> str:
    """O corpo de ``tools/call`` com o numero escrito como o aplicativo escreveu: montado em texto, trocando uma marca
    pelo literal. Ler e escrever de novo nao serve: ``json.dumps(json.loads('{"n": 1e400}'))`` da ``Infinity``."""
    pedido = json.dumps(pedido_2026("tools/call", {"name": nome, "arguments": {"n": MARCA}}))
    corpo = pedido.replace(json.dumps(MARCA), literal)
    assert corpo.count(literal) == 1
    assert MARCA not in corpo
    return corpo


async def chamar_com_literal(m: Mundo, chave: str, literal: str) -> tuple[str, dict[str, Any]]:
    """Como ``Mundo.chamar_cru`` (Ana, aplicativo A), com o corpo de ``corpo_com_literal``. Devolve o corpo que de fato
    foi ao fio (``r.request.content``: os bytes que o cliente HTTP postou, nao a variavel montada antes) e a resposta
    JSON-RPC inteira."""
    nome = m.f[chave].nome
    corpo = corpo_com_literal(nome, literal)
    async with httpx2.AsyncClient(follow_redirects=False, timeout=30) as http:
        r = await http.post(f"{m.url}/mcp", content=corpo, headers=cabecalhos_2026("tools/call", nome, m.cracha("ana")))
    return r.request.content.decode(), r.json()


def test_o_corpo_leva_o_literal_como_o_aplicativo_escreveu() -> None:
    corpo = corpo_com_literal("ler", "1e400")
    assert '"arguments": {"n": 1e400}' in corpo
    assert "Infinity" not in corpo
    assert json.loads(corpo)["params"]["arguments"] == {"n": math.inf}


@pytest.mark.parametrize("chave", ["p_le_livre", "t_le_livre"], ids=["proprio", "terceiro"])
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
async def test_leitura_com_numero_nao_finito_pelo_cliente_do_despachante_de_verdade_e_recusada_com_frase(
    mundo: Mundo, chave: str, literal: str
) -> None:
    """Prova test_verif2_nan_leitura.py do verificador do G4r: com o ``ClienteDespachante`` de verdade (o JSON dele
    nao leva NaN nem infinito), a leitura dava -32603 e nada chegava ao despachante. Agora, recusa legivel, antes da
    catraca: a recusa nao gasta a cota do dia da ordem (``leituras_diarias``). O numero vai ao fio como o aplicativo o
    escreveu (``1e400``, e nao ``Infinity``)."""
    m = mundo
    chegaram: list[Any] = []

    def despachante(pedido: httpx2.Request) -> httpx2.Response:
        chegaram.append(json.loads(pedido.content))
        return httpx2.Response(200, json=resultado_do_sistema("sistema"))

    real = ClienteDespachante(m.p, http=httpx2.AsyncClient(transport=httpx2.MockTransport(despachante)))
    m.leitor.respostas[m.f[chave].id] = real.ler
    # Como chega do aplicativo em JSON cru: o leitor de JSON do kit aceita NaN, Infinity e 1e400 (infinito).
    enviado, resposta = await chamar_com_literal(m, chave, literal)
    assert enviado == corpo_com_literal(m.f[chave].nome, literal)  # os bytes postados, sem ler e escrever de novo
    assert f'"n": {literal}' in enviado
    assert desfecho(resposta) == NUMERO_INVALIDO_LEITURA
    assert "nao guarda pedido" not in RECUSA_NUMERO_LEITURA
    assert chegaram == []
    assert m.leitor.chamadas == []  # recusada antes da catraca e do despachante
    assert await leituras_de_hoje(m) == 0  # sem gastar a cota do dia
    assert "numero_invalido" in (await m.extrato("leitura_recusada", pessoa="ana"))[-1].frase
    # O numero de verdade segue pelo mesmo caminho, gasta a sua vez e chega ao despachante.
    _, resposta = await m.chamar_cru("ana", chave, {"n": 1.5})
    assert desfecho(resposta) == "segue"
    assert [c["argumentos"] for c in chegaram] == [{"n": 1.5}]
    assert await leituras_de_hoje(m) == 1
