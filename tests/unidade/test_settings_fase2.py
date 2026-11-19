"""Parametros da Fase 2: o que ainda nao pode ligar recusa a partida, com frase legivel."""

from __future__ import annotations

import pytest

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros


@pytest.mark.parametrize("fase2", [True, False])
def test_acoes_de_programas_nao_ligam_ainda(fase2: bool) -> None:
    p = Parametros(PROGRAM_ACTIONS_ENABLED=True, FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=fase2)
    with pytest.raises(ConfiguracaoInvalida) as erro:
        p.conferir()
    texto = str(erro.value)
    assert "PROGRAM_ACTIONS_ENABLED ainda nao liga" in texto
    assert "pendencias do responsavel" in texto
    assert "reconferencia completa" in texto
    assert "decisao 9" in texto


def test_acoes_de_programas_desligadas_passam() -> None:
    p = Parametros(FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=True)
    assert p.PROGRAM_ACTIONS_ENABLED is False
    assert not any("PROGRAM_ACTIONS_ENABLED" in e for e in p.problemas())


def test_login_recente_e_parametro_da_instalacao() -> None:
    """``SCREENS_RECENT_LOGIN_SECONDS`` existe em ``Parametros`` (padrao 300), e as telas o leem dali."""
    from mcp_claro.telas.config import ConfigTelas

    assert Parametros().SCREENS_RECENT_LOGIN_SECONDS == 300
    assert not any("SCREENS_RECENT_LOGIN_SECONDS" in e for e in Parametros().problemas())
    for valor in (60, 900):
        assert not any(
            "SCREENS_RECENT_LOGIN_SECONDS" in e for e in Parametros(SCREENS_RECENT_LOGIN_SECONDS=valor).problemas()
        )
    for valor in (59, 901, 0):
        with pytest.raises(ConfiguracaoInvalida) as erro:
            Parametros(SCREENS_RECENT_LOGIN_SECONDS=valor).conferir()
        assert "SCREENS_RECENT_LOGIN_SECONDS aceita de 60 a 900." in str(erro.value)
    assert ConfigTelas.de_parametros(Parametros(SCREENS_RECENT_LOGIN_SECONDS=120)).login_recente_segundos == 120


def test_login_recente_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENS_RECENT_LOGIN_SECONDS", "600")
    assert Parametros().SCREENS_RECENT_LOGIN_SECONDS == 600
