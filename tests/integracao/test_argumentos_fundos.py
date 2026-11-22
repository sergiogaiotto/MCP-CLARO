"""Argumentos muito fundos pela entrada real (Fase 3, G4r): nunca erro interno, em leitura e acao, proprio e terceiro.

O pedido vai em JSON cru (``Mundo.chamar_cru``): o cliente do kit nao serializa mais de uns 250 niveis. Para cada
caso, o resultado e uma recusa legivel (-32602, com o motivo no ``data``) ou o caminho de sempre (leitura que
chega ao sistema, acao registrada), nunca -32603.

- Terceiro: o teto de niveis (``cadastro.esquema.PROFUNDIDADE_MAXIMA``, o mesmo do esquema) recusa antes do
  esquema.
- Proprio, como nas Fases 1 e 2: a leitura com esquema que nao desce segue; a acao com mais de ``NIVEIS_DA_ACAO``
  niveis, que antes dava erro interno, e recusada com frase; com um esquema recursivo, a conferencia que estoura a
  pilha vira ``argumento_fora_do_esquema``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy import text

from mcp_claro.cadastro.esquema import PROFUNDIDADE_MAXIMA
from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.argumentos import PROBLEMA_FUNDO
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import NIVEIS_DA_ACAO, RECUSA_FUNDO
from mcp_claro.versao import PREFIXO_META
from tests.integracao.test_entrada_terceiros_apoio import Extra, Mundo, subir_mundo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

ARVORE: dict[str, Any] = {
    "type": "object",
    "properties": {"lista": {"$ref": "#/$defs/no"}},
    "$defs": {"no": {"type": "array", "items": {"$ref": "#/$defs/no"}}},
}
"""Esquema recursivo (aceito num sistema proprio, como nas Fases 1 e 2): a conferencia desce um nivel por nivel."""

EXTRAS = {
    "p_arvore_ler": Extra(False, ("ler_arvore", "le", "medio", (), False), ARVORE),
    "p_arvore_mudar": Extra(False, ("mudar_arvore", "muda", "medio", (), False), ARVORE),
}


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado, extras=EXTRAS) as m:
        yield m


def com_niveis(n: int) -> dict[str, Any]:
    """Argumentos com ``n`` niveis de listas e objetos, um dentro do outro: o objeto dos argumentos e o primeiro, e a
    lista mais funda fica vazia (o esquema recursivo aceita, se couber)."""
    valor: Any = []
    for _ in range(n - 2):
        valor = [valor]
    return {"lista": valor}


def niveis(valor: Any) -> int:
    n = 0
    while isinstance(valor, dict | list):
        n += 1
        valor = next(iter(valor.values() if isinstance(valor, dict) else valor), None)
    return n


def recusa(resposta: dict[str, Any]) -> tuple[str, str]:
    """A recusa legivel: -32602 com o motivo do MCP-CLARO. Nunca -32603 (erro interno)."""
    assert "error" in resposta, resposta.get("result")
    erro = resposta["error"]
    assert erro["code"] == types.INVALID_PARAMS, erro
    return erro["data"][PREFIXO_META + "motivo"], erro["message"]


async def operacoes(m: Mundo) -> int:
    async with m.dono.connect() as c:
        return int(
            (
                await c.execute(text("SELECT count(*) FROM operacoes WHERE aplicativo_id = :a"), {"a": m.apps["A"]})
            ).scalar_one()
        )


def test_os_niveis_do_teste_cabem_no_que_o_teste_diz() -> None:
    assert niveis(com_niveis(600)) == 600
    assert niveis(com_niveis(5000)) == 5000


def _tem_nulo_de_antes(valor: Any) -> bool:
    """A ``tem_nulo`` das Fases 1 e 2, com recursao (dois quadros da pilha por nivel)."""
    if isinstance(valor, str):
        return "\x00" in valor
    if isinstance(valor, dict):
        return any(_tem_nulo_de_antes(chave) or _tem_nulo_de_antes(item) for chave, item in valor.items())
    if isinstance(valor, list | tuple):
        return any(_tem_nulo_de_antes(item) for item in valor)
    return False


def test_o_teto_de_niveis_da_acao_nao_recusa_o_que_virava_protocolo() -> None:
    """D3: com ``NIVEIS_DA_ACAO`` niveis, a ``tem_nulo`` de antes ja estourava a pilha, de qualquer altura (na entrada,
    com a pilha do servidor embaixo, estourava antes). A recusa nova so pega o que antes dava erro interno."""
    assert NIVEIS_DA_ACAO == 500
    with pytest.raises(RecursionError):
        _tem_nulo_de_antes(com_niveis(NIVEIS_DA_ACAO))


@pytest.mark.parametrize("n", [600, 5000])
async def test_terceiro_fundo_e_recusado_pelo_teto_de_niveis_em_leitura_e_acao(mundo: Mundo, n: int) -> None:
    m = mundo
    caminho = "lista/.../" + "/".join(["0"] * 8)  # o nivel 65 fica em lista/0/.../0, cortado na frase
    for chave, tipo in (("t_ler", "leitura_recusada"), ("t_enviar", "acao_recusada")):
        _, resposta = await m.chamar_cru("ana", chave, com_niveis(n))
        motivo, mensagem = recusa(resposta)
        assert motivo == "argumento_grande_demais"
        assert mensagem == (
            f"Argumentos grandes demais para um servidor de terceiros: {caminho}: mais de {PROFUNDIDADE_MAXIMA} "
            "niveis de listas e objetos, um dentro do outro. Nada foi enviado."
        )
        assert "argumento_grande_demais" in (await m.extrato(tipo, pessoa="ana"))[-1].frase
    assert m.leitor.chamadas == []
    assert await operacoes(m) == 0


@pytest.mark.parametrize("n", [600, 5000])
async def test_proprio_fundo_le_como_antes_e_a_acao_e_recusada_com_frase(mundo: Mundo, n: int) -> None:
    m = mundo
    # Leitura: o esquema nao desce pela lista, e a leitura chega ao sistema com os argumentos inteiros (D3).
    _, resposta = await m.chamar_cru("ana", "p_simples", com_niveis(n))
    assert "result" in resposta, resposta
    assert resposta["result"].get("isError") is not True
    assert [c.ferramenta_id for c in m.leitor.chamadas] == [m.f["p_simples"].id]
    assert niveis(m.leitor.chamadas[0].argumentos) == n
    # Acao: antes da Fase 3, mais de 500 niveis davam erro interno; agora, recusa legivel antes de virar protocolo.
    _, resposta = await m.chamar_cru("ana", "p_mensagem", com_niveis(n))
    motivo, mensagem = recusa(resposta)
    assert (motivo, mensagem) == ("aninhamento_fundo", RECUSA_FUNDO)
    assert "aninhamento_fundo" in (await m.extrato("acao_recusada", pessoa="ana"))[-1].frase
    assert await operacoes(m) == 0


@pytest.mark.parametrize("n", [600, 5000])
async def test_esquema_recursivo_que_estoura_a_pilha_vira_recusa_legivel(mundo: Mundo, n: int) -> None:
    m = mundo
    for chave, tipo in (("p_arvore_ler", "leitura_recusada"), ("p_arvore_mudar", "acao_recusada")):
        _, resposta = await m.chamar_cru("ana", chave, com_niveis(n))
        motivo, mensagem = recusa(resposta)
        assert motivo == "argumento_fora_do_esquema"
        assert mensagem == f"Argumentos fora do esquema revisado: {PROBLEMA_FUNDO}"
        assert "argumento_fora_do_esquema" in (await m.extrato(tipo, pessoa="ana"))[-1].frase
    assert m.leitor.chamadas == []
    assert await operacoes(m) == 0


async def test_esquema_recursivo_raso_continua_valendo(mundo: Mundo) -> None:
    m = mundo
    _, resposta = await m.chamar_cru("ana", "p_arvore_ler", com_niveis(10))
    assert "result" in resposta, resposta
    assert [c.ferramenta_id for c in m.leitor.chamadas] == [m.f["p_arvore_ler"].id]
    # E o esquema confere de verdade: um texto onde vai uma lista e recusado.
    _, resposta = await m.chamar_cru("ana", "p_arvore_ler", {"lista": [["x"]]})
    assert recusa(resposta)[0] == "argumento_fora_do_esquema"


async def test_acao_propria_no_teto_de_niveis_e_registrada_e_um_a_mais_e_recusada(mundo: Mundo) -> None:
    """No teto, a acao vira protocolo: ``tem_nulo`` sem recursao, o JSON e o banco guardam os 500 niveis. Com a
    ``tem_nulo`` de antes (recursao), o mesmo pedido dava erro interno."""
    m = mundo
    _, resposta = await m.chamar_cru("ana", "p_mensagem", com_niveis(NIVEIS_DA_ACAO))
    assert "result" in resposta, resposta
    meta = resposta["result"]["_meta"]
    op = await m.operacao(meta[PREFIXO_META + "protocolo"])
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.argumentos == com_niveis(NIVEIS_DA_ACAO)
    assert len(await m.extrato("acao_pedida", pessoa="ana")) == 1

    _, resposta = await m.chamar_cru("ana", "p_mensagem", com_niveis(NIVEIS_DA_ACAO + 1))
    assert recusa(resposta) == ("aninhamento_fundo", RECUSA_FUNDO)
    assert await operacoes(m) == 1


async def test_caractere_nulo_bem_fundo_e_achado_sem_recursao(mundo: Mundo) -> None:
    m = mundo
    argumentos = com_niveis(NIVEIS_DA_ACAO)
    valor = argumentos["lista"]
    while valor:
        valor = valor[0]
    valor.append("a\x00b")
    _, resposta = await m.chamar_cru("ana", "p_mensagem", argumentos)
    assert recusa(resposta)[0] == "caractere_nulo"
    assert await operacoes(m) == 0
