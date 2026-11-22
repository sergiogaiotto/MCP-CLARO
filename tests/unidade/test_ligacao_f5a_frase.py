"""A frase do ponto de ligacao sobre a sala fora (etapa 5f, F5a, item 4; critica B8).

Sem a sala, nenhum pedido MCP vai ao terceiro, mas "nada vai ao terceiro" nao e verdade: a troca e a renovacao da
chave (OAuth) saem do despachante, para o endereco de troca de chave do terceiro, conferido antes. A docstring de
``ligacao/ponto.py`` diz isso inteiro, e nenhum comentario de ``src/`` ou ``tests/`` repete a frase de antes.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_claro.despachante import autorizacao, renovacao
from mcp_claro.ligacao import ponto
from mcp_claro.ligacao.sala_contrato import SALA_FORA
from mcp_claro.saida import despachante as porta

RAIZ = Path(__file__).resolve().parents[2]
FRASE = (
    "Sem sala, nenhum pedido MCP vai ao terceiro (``SemSala``: ``nao_enviado`` com ``SalaFora``; o despachante grava"
    " ``sala_fora`` e conta); so a troca e a renovacao da chave (OAuth) saem do despachante, para o endereco de troca"
    " de chave do terceiro, conferido antes."
)
# As frases de antes (e as parecidas): sem a sala, "nada" vai ao terceiro; o despachante nunca fala com ele; um
# terceiro nunca sai pelo despachante.
ANTIGAS = re.compile(
    r"(?i)\bsem (?:a )?sala\b[^.;]{0,40}\bnada (?:vai|sai|chega)\b"
    r"|\bnada (?:vai|sai) (?:a|ao) (?:um )?(?:servidor de )?terceiro"
    r"|\bnunca fala (?:direto )?com (?:o|um) (?:servidor de )?terceiro"
    r"|\bterceiros? (?:nunca|nao|não) sai(?:em)? pelo despachante"
    r"|\bnunca sai pelo despachante"
)


def _de_fora(arquivo: Path) -> bool:
    """Os testes dos documentos (``test_docs*``) procuram estas frases nos documentos e as citam; este tambem."""
    return arquivo.name.startswith("test_docs") or arquivo.name == Path(__file__).name


def _junto(texto: str) -> str:
    """O texto em uma linha so, sem o recuo nem o ``#`` dos comentarios."""
    return re.sub(r"\s+", " ", re.sub(r"\n\s*#?\s*", " ", texto))


def test_a_docstring_do_ponto_diz_a_frase_inteira() -> None:
    assert ponto.__doc__ is not None
    assert FRASE in _junto(ponto.__doc__)
    assert not ANTIGAS.search(_junto(ponto.__doc__))


@pytest.mark.anyio
async def test_a_frase_e_a_do_codigo() -> None:
    # Sem sala (fora de desenvolvimento e teste, mesmo com o cliente local injetado): SEM_SALA, que devolve
    # nao_enviado com SalaFora, e a linha do extrato e sala_fora.
    terceiro = SimpleNamespace(origem="terceiro")
    ligacao = ponto.ligacao_para(terceiro, ambiente="piloto", local_para_terceiro=ponto.PROPRIO)
    assert ligacao is ponto.SEM_SALA
    resposta = await ligacao.chamar_ferramenta("https://t.exemplo/mcp", _sem_chave, None, "f", {})
    assert (resposta.tipo, resposta.excecao) == ("nao_enviado", SALA_FORA)
    assert ponto.linha_da_sala(resposta, terceiro) == ponto.LINHA_SALA_FORA == "sala_fora"
    # A chave (e a renovacao dela, por OAuth) vem antes da ligacao; a troca vai ao endereco de troca de chave do
    # terceiro, conferido antes.
    enviar = inspect.getsource(porta.PortaDeSaida._enviar)
    assert enviar.index("await self._chave(op, ferramenta)") < enviar.index("self.ligacao(sistema)")
    assert "renovacao.chave_pronta(" in inspect.getsource(porta.PortaDeSaida._chave)
    assert "terceiro=sistema.de_terceiro" in inspect.getsource(renovacao)
    assert 'http.stream("POST", config.token_endpoint' in inspect.getsource(autorizacao.trocar)
    assert "await _conferir_troca_de_terceiro(config.token_endpoint" in inspect.getsource(autorizacao)
    assert "cliente.conferir_endereco_terceiro(" in inspect.getsource(autorizacao._conferir_troca_de_terceiro)


async def _sem_chave() -> str | None:
    return None


def test_nenhum_comentario_de_src_ou_tests_repete_a_frase_de_antes() -> None:
    achados: list[str] = []
    for pasta in ("src", "tests", "exemplos"):
        for arquivo in sorted((RAIZ / pasta).rglob("*.py")):
            if _de_fora(arquivo):
                continue
            for achado in ANTIGAS.finditer(_junto(arquivo.read_text(encoding="utf-8"))):
                achados.append(f"{arquivo.relative_to(RAIZ)}: {achado.group(0)}")
    assert achados == []
