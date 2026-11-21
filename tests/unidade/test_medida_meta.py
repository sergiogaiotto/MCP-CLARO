"""Leitura com medida na Fase 3 (G3a, item 3): o ``_meta`` dos blocos e do recurso embutido nunca passa; as
``annotations`` de bloco so para sistema proprio; o aviso de partes retiradas continua o ultimo bloco, so com
``type`` e ``text``, e o criterio de retirada nao muda. Sem recursao: JSON fundo num texto nao derruba a medida.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_claro.despachante import medida
from mcp_claro.regras import estranhos
from tests.apoio_g3a import json_fundo

FORNECEDOR = "fornecedor.exemplo/x"


def _resultado() -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": "Oi ana@exemplo.test",
                "annotations": {"audience": ["user"], "priority": 0.5},
                "_meta": {FORNECEDOR: "123.456.789-09", "br.com.claro.mcp-claro/motivo": "forjado"},
            },
            {
                "type": "resource",
                "resource": {
                    "uri": "https://sistema.exemplo/r",
                    "mimeType": "text/plain",
                    "text": "Recurso",
                    "_meta": {FORNECEDOR: "dentro do recurso"},
                    "outra": "chave que o tipo nao conhece",
                },
                "_meta": {FORNECEDOR: "no bloco do recurso"},
                "annotations": {"audience": ["assistant"]},
            },
            {"type": "image", "data": "AAAA", "mimeType": "image/png", "_meta": {FORNECEDOR: "imagem"}},
            {"type": "resource_link", "uri": "https://sistema.exemplo/l", "name": "l"},
        ],
        "structuredContent": {"nome": "Ana"},
        "_meta": {FORNECEDOR: "do resultado"},
        "isError": False,
    }


def test_meta_de_bloco_e_do_recurso_embutido_nunca_passa() -> None:
    saida = medida.aplicar(_resultado(), [])
    assert all("_meta" not in b for b in saida["content"])
    assert "_meta" not in saida["content"][1]["resource"]
    assert FORNECEDOR not in json.dumps(saida["content"])
    assert saida["content"][1]["resource"] == {
        "uri": "https://sistema.exemplo/r",
        "mimeType": "text/plain",
        "text": "Recurso",
    }


def test_annotations_de_bloco_so_para_proprio() -> None:
    proprio = medida.aplicar(_resultado(), [])
    terceiro = medida.aplicar(_resultado(), [], anotacoes=False)
    assert proprio["content"][0]["annotations"] == {"audience": ["user"], "priority": 0.5}
    assert proprio["content"][1]["annotations"] == {"audience": ["assistant"]}
    assert all("annotations" not in b for b in terceiro["content"])


def test_aviso_de_retiradas_e_o_ultimo_bloco_so_com_type_e_text() -> None:
    saida = medida.aplicar(_resultado(), [])
    assert saida["content"][-1] == {"type": "text", "text": estranhos.aviso_de_retiradas(2)}
    assert [b["type"] for b in saida["content"]] == ["text", "resource", "text"]


@pytest.mark.parametrize("anotacoes", [True, False])
def test_criterio_de_retirada_nao_muda(anotacoes: bool) -> None:
    bruto = _resultado()
    retiradas = estranhos.retiradas_pela_medida(bruto)
    saida = medida.aplicar(bruto, [], anotacoes=anotacoes)
    assert retiradas == 2
    assert len(saida["content"]) == len(bruto["content"]) - retiradas + 1


def test_texto_e_campos_pessoais_continuam_mascarados() -> None:
    saida = medida.aplicar(_resultado(), ["nome"])
    assert saida["content"][0]["text"] == "Oi a***@exemplo.test"
    assert saida["structuredContent"] == {"nome": "***"}
    assert "_meta" in saida  # o _meta do resultado quem tira e o despachante (servico), nao a medida


def test_json_fundo_num_texto_com_campos_pessoais_e_mascarado_sem_recursao() -> None:
    texto = json_fundo(3000)
    saida = medida.aplicar({"content": [{"type": "text", "text": texto}]}, ["cpf", "nome"])
    resultado = saida["content"][0]["text"]
    assert "Ana Silva" not in resultado
    assert "123.456.789-09" not in resultado
    assert resultado.count("[") == 3000


def test_json_fundo_demais_ate_para_o_leitor_vira_resposta_funda_demais() -> None:
    with pytest.raises(medida.RespostaFundaDemais):
        medida.aplicar({"content": [{"type": "text", "text": json_fundo(200_000)}]}, ["cpf"])
    # Sem campos pessoais, o texto nem e aberto: passa como texto.
    saida = medida.aplicar({"content": [{"type": "text", "text": json_fundo(200_000)}]}, [])
    assert len(saida["content"]) == 1


def test_estrutura_funda_no_structured_content_e_mascarada_sem_recursao() -> None:
    fundo: Any = {"cpf": "12345678909"}
    for _ in range(5000):
        fundo = {"a": fundo}
    saida = medida.aplicar({"content": [], "structuredContent": fundo}, ["cpf"])
    no_fundo = saida["structuredContent"]
    while "a" in no_fundo:
        no_fundo = no_fundo["a"]
    assert no_fundo == {"cpf": "***09"}


def test_profundidade() -> None:
    assert medida.profundidade(1) == 0
    assert medida.profundidade({}) == 1
    assert medida.profundidade({"a": [1, {"b": 2}]}) == 3
    assert medida.profundidade(json.loads(json_fundo(500, "1"))) == 500


def test_texto_do_recurso_embutido_tambem_passa_pela_medida() -> None:
    """O recurso embutido com texto tem a mesma medida do bloco de texto: JSON pelos campos do cadastro, valores
    crus do structuredContent e CPF ou e-mail em qualquer texto."""
    resultado = {
        "content": [
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///ficha.json",
                    "mimeType": "application/json",
                    "text": '{"nome": "Ana Silva", "cpf": "123.456.789-09", "obs": "falar com ana@exemplo.com"}',
                },
            },
            {
                "type": "resource",
                "resource": {"uri": "file:///nota.txt", "mimeType": "text/plain", "text": "Cliente Bia Souza ligou."},
            },
        ],
        "structuredContent": {"cliente": {"nome": "Bia Souza"}},
    }
    saida = medida.aplicar(resultado, ["nome", "cpf"])
    ficha, nota = (b["resource"]["text"] for b in saida["content"])
    assert json.loads(ficha) == {"nome": "***va", "cpf": "***09", "obs": "falar com a***@exemplo.com"}
    assert "Bia Souza" not in nota
    assert "123.456.789-09" not in json.dumps(saida)
    assert [b["resource"]["uri"] for b in saida["content"]] == ["file:///ficha.json", "file:///nota.txt"]
