"""Tres sinais sem banco (Fase 3, G4): o que conta como texto do sistema, o retrato da ficha e a janela."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mcp_claro.config.politica import POLITICA
from mcp_claro.extrato.registro import _CHAVES_PROIBIDAS
from mcp_claro.regras import sinais
from mcp_claro.regras.estranhos import marcar
from mcp_claro.versao import MOTIVO_EXIGE_CAPACIDADE, PREFIXO_META

AGORA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def do_sistema(**extra: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": "do sistema"}], "_meta": {PREFIXO_META + "sistema": "s"}} | extra


@pytest.mark.parametrize(
    "motivo", ["fora_da_ordem", "falha_nao_enviado", "sistema_pediu_dados", "chave_recusada", MOTIVO_EXIGE_CAPACIDADE]
)
def test_texto_nosso_nao_veio_do_sistema(motivo: str) -> None:
    nosso = {
        "content": [{"type": "text", "text": "nosso"}],
        "isError": True,
        "_meta": {PREFIXO_META + "motivo": motivo},
    }
    assert sinais.veio_do_sistema(nosso) is False


def test_resultado_do_sistema_mesmo_com_erro_veio_do_sistema() -> None:
    assert sinais.veio_do_sistema(do_sistema()) is True
    assert sinais.veio_do_sistema(do_sistema(isError=True)) is True
    assert sinais.veio_do_sistema({"content": []}) is True
    assert sinais.veio_do_sistema({"content": [], "_meta": None}) is True
    # A moldura de estranhos e do sistema: o _meta dela nao tem motivo.
    assert sinais.veio_do_sistema(marcar(do_sistema(), sistema="s", de_terceiro=True)) is True


def alerta(janela: int = 240) -> sinais.Alerta:
    return sinais.Alerta(
        janela,
        (
            sinais.Visto(sinais.TEXTO_DE_FORA, "ler_email", "correio", AGORA - timedelta(minutes=30)),
            sinais.Visto(sinais.DADO_PRIVADO, "ver_cadastro", "cadastro", AGORA - timedelta(minutes=5)),
        ),
    )


def test_retrato_so_com_nomes_do_cadastro_e_horas() -> None:
    retrato = alerta().retrato()
    assert retrato == {
        "janela_minutos": 240,
        "vistos": [
            {
                "sinal": "texto_de_fora",
                "ferramenta": "ler_email",
                "sistema": "correio",
                "visto_em": (AGORA - timedelta(minutes=30)).isoformat(),
            },
            {
                "sinal": "dado_privado",
                "ferramenta": "ver_cadastro",
                "sistema": "cadastro",
                "visto_em": (AGORA - timedelta(minutes=5)).isoformat(),
            },
        ],
    }
    chaves = set(retrato) | {k for v in retrato["vistos"] for k in v}
    assert not {k.lower() for k in chaves} & _CHAVES_PROIBIDAS


def test_alerta_dura_ate_o_mais_velho_sair_da_janela() -> None:
    a = alerta(60)
    assert a.ate == AGORA - timedelta(minutes=30) + timedelta(minutes=60)
    assert a.de_fora.ferramenta == "ler_email"
    assert a.privado.ferramenta == "ver_cadastro"


def test_nomes_do_retrato_para_a_linha_do_extrato() -> None:
    assert sinais.nomes_do_retrato(alerta().retrato()) == ("ler_email", "ver_cadastro")
    assert sinais.nomes_do_retrato({"vistos": []}) == ("?", "?")


def test_janela_fica_entre_1_e_a_maior() -> None:
    assert sinais.janela_valida(0) == 1
    assert sinais.janela_valida(240) == 240
    assert sinais.janela_valida(10**9) == sinais.MAIOR_JANELA_MINUTOS


def test_maior_janela_e_o_maximo_da_politica() -> None:
    assert sinais.MAIOR_JANELA_MINUTOS == POLITICA["THREE_SIGNALS_WINDOW_MINUTES"].maximo == 10080
    assert sinais.TIPOS == ("texto_de_fora", "dado_privado")
