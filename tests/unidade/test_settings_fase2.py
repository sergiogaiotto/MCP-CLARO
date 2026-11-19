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
