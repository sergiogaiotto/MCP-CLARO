"""Programas e ronda, terceira rodada de correcoes da Fase 2: textos que dizem o que o codigo faz."""

from __future__ import annotations

from mcp_claro.ronda.casos import ESPERADO, Caso
from mcp_claro.telas.admin_programas import FEITOS


def test_aviso_de_aplicativo_reativado_nao_promete_que_os_pedidos_passam() -> None:
    """Um bloqueio ou pausa de alcance aplicativo, ou o programa pausado, seguem recusando os pedidos."""
    assert FEITOS["programa_aplicativo_reativado"] == (
        "Pronto. O aplicativo do programa foi reativado. Os pedidos dele voltam a passar quando o cadastro do programa"
        " valer e nenhum bloqueio ou pausa os segurar."
    )


def test_o_controle_espera_a_vitrine_da_ronda() -> None:
    """O rotulo vai a coluna esperado das tabelas ronda e rondas_vigia. O controle so e conforme com um resultado."""
    assert ESPERADO[Caso.CONTROLE] == "vitrine da ronda (resultado do tools/list)"
    assert "cracha aceito" not in ESPERADO[Caso.CONTROLE]
