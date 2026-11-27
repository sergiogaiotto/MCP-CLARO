"""``config/settings.py`` na Fase 4 (desenho 9; ajustes, D24): a chave dos selos e os parametros novos.

- ``FEATURE_PHASE4_BADGES`` ligada e recusada na partida pela regra da D6, mesmo com a Fase 3 (e as anteriores)
  ligadas pelo construtor; a mensagem cita a D24. A ordem das fases continua: a Fase 4 exige a Fase 3 (decisao 10).
- Os testes ligam a chave pelo construtor de ``Parametros``, sem ``conferir``, como na Fase 3.
- Os parametros novos tem o padrao e a faixa da tabela do desenho 9; fora da faixa, a leitura recusa.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mcp_claro.config.settings import FASE4_AINDA_NAO_LIGA, ConfiguracaoInvalida, Parametros, carregar

TODAS_LIGADAS: dict[str, Any] = {
    "FEATURE_PHASE1_READ": True,
    "FEATURE_PHASE2_ACT": True,
    "FEATURE_PHASE3_THIRD_PARTY": True,
    "FEATURE_PHASE4_BADGES": True,
}

DECISAO_10 = "FEATURE_PHASE4_BADGES exige FEATURE_PHASE3_THIRD_PARTY (decisao 10)."


def test_a_recusa_propria_cita_a_d24_a_regra_da_d6_e_as_mudancas_deliberadas() -> None:
    assert FASE4_AINDA_NAO_LIGA == (
        "FEATURE_PHASE4_BADGES ainda nao liga: ate a decisao registrada do dono, depois das provas que dependem de"
        " gente e de ambiente real, so pessoas de teste usam os selos de nivel 2 e 3 (D24 da Fase 4, pela regra da"
        " D6; decisoes.md, Mudancas deliberadas da Fase 4)."
    )
    assert FASE4_AINDA_NAO_LIGA.isascii()  # textos da partida sem acento


def test_ligada_e_recusada_mesmo_com_a_fase_3_ligada_pelo_construtor() -> None:
    p = Parametros(**TODAS_LIGADAS)  # o construtor liga, sem conferir
    assert p.FEATURE_PHASE4_BADGES is True
    problemas = p.problemas()
    assert FASE4_AINDA_NAO_LIGA in problemas
    assert DECISAO_10 not in problemas  # a ordem esta certa: a recusa e a propria
    with pytest.raises(ConfiguracaoInvalida) as erro:
        p.conferir()
    assert "D24 da Fase 4" in str(erro.value)


def test_a_recusa_da_fase_4_nao_depende_da_recusa_da_fase_3() -> None:
    """Se um dia a D6 cair (a Fase 3 liga), a Fase 4 continua recusada ate uma decisao propria."""
    p = Parametros(**TODAS_LIGADAS)
    sem_a_d6 = [x for x in p.problemas() if not x.startswith("FEATURE_PHASE3_THIRD_PARTY ainda nao liga")]
    assert FASE4_AINDA_NAO_LIGA in sem_a_d6
    sem_fase4 = Parametros(**(TODAS_LIGADAS | {"FEATURE_PHASE4_BADGES": False})).problemas()
    assert not [x for x in sem_fase4 if "FEATURE_PHASE4_BADGES" in x]


def test_ligada_sem_a_fase_3_da_as_duas_recusas() -> None:
    problemas = Parametros(FEATURE_PHASE4_BADGES=True).problemas()
    assert DECISAO_10 in problemas
    assert FASE4_AINDA_NAO_LIGA in problemas


def test_pelo_ambiente_a_partida_recusa(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome in TODAS_LIGADAS:
        monkeypatch.setenv(nome, "ligado")
    with pytest.raises(ConfiguracaoInvalida, match="D24 da Fase 4"):
        carregar()


def test_desligada_por_padrao_e_sem_recusa() -> None:
    p = carregar()
    assert p.FEATURE_PHASE4_BADGES is False
    assert not [x for x in p.problemas() if "PHASE4" in x]


def test_a_ordem_das_fases_continua_valendo() -> None:
    assert (
        "FEATURE_PHASE3_THIRD_PARTY exige FEATURE_PHASE2_ACT (decisao 10)."
        in Parametros(FEATURE_PHASE3_THIRD_PARTY=True).problemas()
    )
    assert "FEATURE_PHASE2_ACT exige FEATURE_PHASE1_READ." in Parametros(FEATURE_PHASE2_ACT=True).problemas()


# --- Parametros novos (desenho 9) ------------------------------------------------------------------------------

PARAMETROS: list[tuple[str, int, int, int]] = [
    ("BADGE_LOOKUP_INTERVAL_SECONDS", 60, 30, 3600),
    ("BADGE_LOOKUP_MAX_ATTEMPTS", 8, 1, 20),
    ("BADGE_LOOKUP_TIMEOUT_SECONDS", 10, 1, 60),
    ("BADGE_LOOKUP_FAILS_TO_WITHDRAW", 3, 1, 20),
    ("BADGE_BLOCK_NOTICE_ATTEMPTS", 3, 1, 10),
    ("BADGE_TEST_MAX_CALLS", 300, 50, 2000),
    ("BADGE_TEST_MAX_SECONDS", 900, 60, 3600),
    ("BADGE_TEST_PAIRS", 10, 0, 100),
]


@pytest.mark.parametrize(("nome", "padrao", "minimo", "maximo"), PARAMETROS)
def test_padrao_e_faixa(nome: str, padrao: int, minimo: int, maximo: int) -> None:
    assert getattr(carregar(), nome) == padrao
    assert getattr(Parametros(**{nome: minimo}), nome) == minimo
    assert getattr(Parametros(**{nome: maximo}), nome) == maximo
    for fora in (minimo - 1, maximo + 1):
        with pytest.raises(ValidationError):
            Parametros(**{nome: fora})


@pytest.mark.parametrize(("nome", "padrao", "minimo", "maximo"), PARAMETROS)
def test_pelo_ambiente(nome: str, padrao: int, minimo: int, maximo: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(nome, str(maximo))
    assert getattr(carregar(), nome) == maximo
    monkeypatch.setenv(nome, str(maximo + 1))
    with pytest.raises(ValidationError):
        carregar()


def test_a_tabela_tem_todos_os_parametros_novos_e_nenhum_outro() -> None:
    novos = {n for n in Parametros.model_fields if n.startswith("BADGE_")}
    assert novos == {n for n, *_ in PARAMETROS}
    # BADGE_TTL_DAYS e BADGE_WARN_DAYS sao da politica (o banco le a validade na hora do teste), nao daqui.
    assert "BADGE_TTL_DAYS" not in Parametros.model_fields
