"""Resposta de uma acao enviada, sem banco (Fase 3, G4): ``saida.pedidos.resposta`` com texto de estranhos."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from mcp_claro.regras import estranhos
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import META_RETIRADAS, Operacao
from mcp_claro.saida.pedidos import resposta
from mcp_claro.versao import PREFIXO_META

AGORA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def operacao(**mudar: Any) -> Operacao:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "protocolo": "CLR-2026-00000077",
        "codigo_conferencia": "ABCD2346EFGH",
        "pessoa_id": uuid.uuid4(),
        "aplicativo_id": uuid.uuid4(),
        "ordem_id": uuid.uuid4(),
        "ferramenta_id": uuid.uuid4(),
        "sistema_id": uuid.uuid4(),
        "argumentos": {"texto": "oi"},
        "aplicativo_diz": None,
        "resultado": {
            "content": [{"type": "text", "text": "Feito pelo sistema."}],
            "structuredContent": {"ok": True},
            "isError": False,
        },
        "desfecho": "o sistema respondeu",
        "criada_em": AGORA,
        "fila_id": 1,
        "estado": Estado.ENVIADA,
        "ficha_id": None,
        "limite_id": uuid.uuid4(),
        "hora_saida": None,
        "enviada_em": AGORA,
        "mudou_em": AGORA,
        "mudou_por": "despachante",
        "nome_vitrine": "enviar_x",
        "sistema_nome": "fornecedor-x",
    }
    base.update(mudar)
    return Operacao(**base)


def test_operacao_sabe_se_e_texto_de_estranhos() -> None:
    assert operacao().de_estranhos is False
    assert operacao(sistema_origem="terceiro").de_estranhos is True
    assert operacao(sistema_origem="terceiro").de_terceiro is True
    assert operacao(texto_de_fora=True).de_estranhos is True
    assert operacao(texto_de_fora=True).de_terceiro is False


def test_enviada_de_terceiro_tem_o_protocolo_fora_da_moldura_e_o_resto_dentro() -> None:
    op = operacao(sistema_origem="terceiro")
    r = resposta(op, selo=1)
    marca = r["_meta"][estranhos.META_MARCA]
    blocos = [b["text"] for b in r["content"]]
    assert blocos[0] == "Protocolo CLR-2026-00000077: enviada uma vez ao sistema."
    assert blocos[1] == estranhos.aviso(marca, sistema="fornecedor-x", de_terceiro=True)
    assert blocos[2] == f"[[estranhos {marca}]]\nFeito pelo sistema.\n[[/estranhos {marca}]]"
    assert blocos[3] == f"[[estranhos {marca}]]\n{json.dumps({'ok': True})}\n[[/estranhos {marca}]]"
    assert "structuredContent" not in r
    assert r["isError"] is False
    assert r["_meta"][PREFIXO_META + "protocolo"] == op.protocolo
    assert r["_meta"][PREFIXO_META + "estado"] == "enviada"
    assert r["_meta"][PREFIXO_META + "selo"] == 1
    assert r["_meta"][estranhos.META_ORIGEM] == "terceiro"
    # O resultado guardado nao muda (a moldura e so na resposta).
    assert op.resultado == operacao().resultado


def test_enviada_de_ferramenta_propria_marcada_tambem_vem_emoldurada() -> None:
    r = resposta(operacao(texto_de_fora=True))
    assert r["_meta"][estranhos.META_ORIGEM] == "proprio"
    assert "e texto de gente de fora, no sistema fornecedor-x" in r["content"][1]["text"]


def test_enviada_de_propria_sem_marca_como_na_fase_2() -> None:
    r = resposta(operacao())
    assert r["content"] == [
        {"type": "text", "text": "Protocolo CLR-2026-00000077: enviada uma vez ao sistema."},
        {"type": "text", "text": "Feito pelo sistema."},
    ]
    assert r["structuredContent"] == {"ok": True}
    assert estranhos.META_MARCA not in r["_meta"]


def com_aviso_de_retiradas(quantas: int, conta: Any) -> dict[str, Any]:
    """Resultado guardado depois de ``medida.aplicar``, que pos o aviso no fim, com a conta da porta de saida."""
    resultado: dict[str, Any] = {
        "content": [
            {"type": "text", "text": "Feito pelo sistema."},
            {"type": "text", "text": estranhos.aviso_de_retiradas(quantas)},
        ],
        "isError": False,
    }
    if conta is not None:
        resultado["_meta"] = {META_RETIRADAS: conta}
    return resultado


def test_aviso_de_partes_retiradas_sai_fora_da_moldura_com_a_conta_guardada() -> None:
    # Critica B4: o aviso e nosso. Com a conta guardada pela porta de saida, sai da moldura, logo depois do aviso
    # da marca; a conta nao vai ao aplicativo.
    op = operacao(sistema_origem="terceiro", resultado=com_aviso_de_retiradas(2, 2))
    assert op.retiradas == 2
    r = resposta(op)
    marca = r["_meta"][estranhos.META_MARCA]
    assert [b["text"] for b in r["content"]] == [
        "Protocolo CLR-2026-00000077: enviada uma vez ao sistema.",
        estranhos.aviso(marca, sistema="fornecedor-x", de_terceiro=True),
        estranhos.aviso_de_retiradas(2),
        f"[[estranhos {marca}]]\nFeito pelo sistema.\n[[/estranhos {marca}]]",
    ]
    assert META_RETIRADAS not in r["_meta"]
    assert META_RETIRADAS not in json.dumps(r)


@pytest.mark.parametrize("conta", [None, 0, -2, True, "2", 2.0], ids=repr)
def test_sem_a_conta_certa_o_bloco_igual_ao_aviso_fica_dentro_da_moldura(conta: Any) -> None:
    # Sem a conta (ou com valor que nao e numero inteiro positivo), o ultimo bloco e texto do sistema, mesmo igual
    # ao nosso aviso: vai emoldurado.
    r = resposta(operacao(sistema_origem="terceiro", resultado=com_aviso_de_retiradas(2, conta)))
    marca = r["_meta"][estranhos.META_MARCA]
    blocos = [b["text"] for b in r["content"]]
    assert len(blocos) == 4
    assert blocos[3] == f"[[estranhos {marca}]]\n{estranhos.aviso_de_retiradas(2)}\n[[/estranhos {marca}]]"
    assert META_RETIRADAS not in json.dumps(r)


def test_conta_guardada_nao_vai_ao_aplicativo_na_propria_sem_marca() -> None:
    r = resposta(operacao(resultado=com_aviso_de_retiradas(1, 1)))
    assert [b["text"] for b in r["content"]][1:] == ["Feito pelo sistema.", estranhos.aviso_de_retiradas(1)]
    assert META_RETIRADAS not in json.dumps(r)


def test_erro_do_terceiro_continua_erro_e_emoldurado() -> None:
    r = resposta(
        operacao(sistema_origem="terceiro", resultado={"content": [{"type": "text", "text": "x"}], "isError": True})
    )
    assert r["isError"] is True
    assert r["content"][2]["text"].startswith("[[estranhos ")


@pytest.mark.parametrize("estado", [Estado.AGUARDANDO_FICHA, Estado.LIBERADA, Estado.NAO_SAIU, Estado.CANCELADA])
def test_estado_sem_resultado_e_so_texto_nosso(estado: Estado) -> None:
    r = resposta(operacao(sistema_origem="terceiro", estado=estado, resultado=None))
    assert len(r["content"]) == 1
    assert estranhos.META_MARCA not in r["_meta"]
    assert "[[estranhos" not in r["content"][0]["text"]
