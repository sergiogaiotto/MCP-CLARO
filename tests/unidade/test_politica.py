import pytest

from mcp_claro.config.politica import POLITICA, nunca_mais_frouxo, valor_inicial


def test_valores_iniciais_do_plano() -> None:
    assert valor_inicial("CARD_TTL_MINUTES") == 30
    assert valor_inicial("OUTBOX_HOLD_SECONDS") == 60
    assert valor_inicial("READS_CAP_PER_DAY") == 200
    assert len(POLITICA) == 15


def test_limites() -> None:
    with pytest.raises(ValueError, match="aceita"):
        POLITICA["CARD_TTL_MINUTES"].conferir(0)


def test_programa_nunca_afrouxa() -> None:
    assert nunca_mais_frouxo("READS_CAP_PER_DAY", 200, 500) == 200
    assert nunca_mais_frouxo("READS_CAP_PER_DAY", 200, 50) == 50
    assert nunca_mais_frouxo("OUTBOX_HOLD_SECONDS", 60, 10) == 60
    assert nunca_mais_frouxo("OUTBOX_HOLD_SECONDS", 60, 120) == 120
