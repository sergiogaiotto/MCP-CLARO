"""Parametros da Fase 3 (desenho 4.4; ajustes D1, D6 e A2; prova U6).

- ``FEATURE_PHASE3_THIRD_PARTY`` ligada recusa a partida (D6), como ``PROGRAM_ACTIONS_ENABLED``; os testes ligam
  pelo construtor, sem ``conferir``;
- a sala de visitas: ``VISITS_INTERNAL_PORT`` com padrao e faixa, diferente das outras portas; fora de
  desenvolvimento e teste, ``VISITS_INTERNAL_URL`` com https e obrigatorio sempre, mesmo com a chave desligada (A2);
- ``VAULT_TRANSIT_KEY_THIRD_PARTY`` com padrao, nome valido e diferente da chave dos sistemas proprios;
- nenhum valor de ``OBS_TRACE_PROPAGATE_DOWNSTREAM`` libera rastro a terceiros (D10).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros, PropagacaoRastro, carregar

_FASES = {"FEATURE_PHASE1_READ": True, "FEATURE_PHASE2_ACT": True}


def _producao(ambiente: str = "producao", **extra: Any) -> Parametros:
    """Uma instalacao de piloto ou producao que passa em tudo o que nao e da Fase 3."""
    valores: dict[str, Any] = {
        "APP_ENV": ambiente,
        "PUBLIC_BASE_URL": "https://mcp.claro.exemplo",
        "INTERNAL_CA_FILE": "/run/secrets/ca.pem",
        "INTERNAL_CLIENT_CA_FILE": "/run/secrets/ca_clientes.pem",
        "INTERNAL_CERT_FILE": "/run/secrets/cert.pem",
        "INTERNAL_KEY_FILE": "/run/secrets/key.pem",
        "SCREENS_CLIENT_SECRET_FILE": "/run/secrets/telas_cliente",
        "NOTIFY_ADMIN_EMAILS": ["admin@claro.exemplo"],
        "GRAFANA_WEBHOOK_PASSWORD_FILE": "/run/secrets/grafana_webhook",
        "WATCH_CONTINUITY_URL": "https://vigia.claro.exemplo:9445",
        "WATCH_CONTINUITY_TOKEN_FILE": "/run/secrets/vigia_continuidade",
        "VISITS_INTERNAL_URL": "https://visitas:9446",
    }
    return Parametros(**(valores | extra))


def _da_sala(p: Parametros) -> list[str]:
    return [e for e in p.problemas() if "VISITS_INTERNAL_URL" in e]


# --- D6: a chave da Fase 3 nao liga na partida ---------------------------------------------------------------------


@pytest.mark.parametrize("fase2", [True, False])
def test_chave_da_fase3_ligada_recusa_a_partida(fase2: bool) -> None:
    p = Parametros(FEATURE_PHASE3_THIRD_PARTY=True, FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=fase2)
    with pytest.raises(ConfiguracaoInvalida) as erro:
        p.conferir()
    texto = str(erro.value)
    assert "FEATURE_PHASE3_THIRD_PARTY ainda nao liga" in texto
    assert "D6" in texto
    assert "pessoas de teste" in texto
    # A ordem das fases continua valendo (test_parametros.py::test_fases_em_ordem).
    assert ("decisao 10" in texto) is (not fase2)


def test_chave_da_fase3_ligada_pelo_ambiente_tambem_recusa(monkeypatch: pytest.MonkeyPatch) -> None:
    for nome in ("FEATURE_PHASE1_READ", "FEATURE_PHASE2_ACT", "FEATURE_PHASE3_THIRD_PARTY"):
        monkeypatch.setenv(nome, "ligado")
    with pytest.raises(ConfiguracaoInvalida, match="FEATURE_PHASE3_THIRD_PARTY ainda nao liga"):
        carregar()


def test_chave_da_fase3_ligada_tambem_em_producao_recusa() -> None:
    p = _producao(FEATURE_PHASE3_THIRD_PARTY=True, **_FASES)
    assert [e for e in p.problemas() if "FEATURE_PHASE3" in e] == [
        next(e for e in p.problemas() if e.startswith("FEATURE_PHASE3_THIRD_PARTY ainda nao liga"))
    ]


def test_testes_ligam_a_chave_pelo_construtor_sem_conferir() -> None:
    p = Parametros(FEATURE_PHASE3_THIRD_PARTY=True, **_FASES)
    assert p.FEATURE_PHASE3_THIRD_PARTY is True


def test_chave_da_fase3_desligada_passa() -> None:
    p = Parametros(**_FASES)
    assert p.FEATURE_PHASE3_THIRD_PARTY is False
    assert not any("FEATURE_PHASE3" in e for e in p.problemas())
    assert _producao(**_FASES).problemas() == []


# --- a sala de visitas ---------------------------------------------------------------------------------------------


def test_porta_da_sala_tem_padrao_e_faixa() -> None:
    assert Parametros().VISITS_INTERNAL_PORT == 9446
    assert Parametros(VISITS_INTERNAL_PORT=1024).VISITS_INTERNAL_PORT == 1024
    assert Parametros(VISITS_INTERNAL_PORT=65535).VISITS_INTERNAL_PORT == 65535
    for fora in (0, 443, 1023, 65536):
        with pytest.raises(ValidationError):
            Parametros(VISITS_INTERNAL_PORT=fora)


@pytest.mark.parametrize("outra", ["GATEWAY_INTERNAL_PORT", "DISPATCHER_INTERNAL_PORT", "ROUTINES_INTERNAL_PORT"])
def test_porta_da_sala_e_diferente_das_outras(outra: str) -> None:
    p = Parametros(**{outra: 9446})
    assert any("sala de visitas devem ser diferentes" in e for e in p.problemas())
    portas = {Parametros().GATEWAY_INTERNAL_PORT, Parametros().DISPATCHER_INTERNAL_PORT}
    assert Parametros().VISITS_INTERNAL_PORT not in portas | {Parametros().ROUTINES_INTERNAL_PORT}


def test_porta_da_sala_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISITS_INTERNAL_PORT", "9550")
    assert carregar().VISITS_INTERNAL_PORT == 9550


@pytest.mark.parametrize("ambiente", ["desenvolvimento", "teste"])
def test_sem_sala_em_desenvolvimento_e_teste_passa(ambiente: str) -> None:
    p = Parametros(APP_ENV=ambiente)
    assert p.VISITS_INTERNAL_URL is None
    assert _da_sala(p) == []
    assert _da_sala(Parametros(APP_ENV=ambiente, VISITS_INTERNAL_URL="http://127.0.0.1:9446")) == []


@pytest.mark.parametrize("ambiente", ["piloto", "producao"])
@pytest.mark.parametrize("sala", [None, "", "   ", "http://visitas:9446"])
def test_fora_de_desenvolvimento_e_teste_a_sala_com_https_e_obrigatoria_mesmo_com_a_chave_desligada(
    ambiente: str, sala: str | None
) -> None:
    p = _producao(ambiente, VISITS_INTERNAL_URL=sala, **_FASES)
    assert p.FEATURE_PHASE3_THIRD_PARTY is False
    problemas = _da_sala(p)
    assert len(problemas) == 1
    assert "obrigatorio e usa https" in problemas[0]
    assert "mesmo com FEATURE_PHASE3_THIRD_PARTY desligada" in problemas[0]
    with pytest.raises(ConfiguracaoInvalida, match="VISITS_INTERNAL_URL"):
        p.conferir()


@pytest.mark.parametrize("ambiente", ["piloto", "producao"])
def test_fora_de_desenvolvimento_e_teste_com_a_sala_https_passa(ambiente: str) -> None:
    assert _producao(ambiente).conferir().VISITS_INTERNAL_URL == "https://visitas:9446"


def test_sala_vazia_no_ambiente_e_nenhuma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISITS_INTERNAL_URL", "")
    assert carregar().VISITS_INTERNAL_URL is None
    monkeypatch.setenv("VISITS_INTERNAL_URL", "https://visitas:9446/")
    assert carregar().VISITS_INTERNAL_URL == "https://visitas:9446"


@pytest.mark.parametrize(
    "sala",
    [
        "visitas:9446",
        "ftp://visitas:9446",
        "https://:9446",
        "https://visitas:99999",
        "https://ana@visitas:9446",
        "https://visitas:9446/interno/visitas",
        "https://visitas:9446?x=1",
        "https://visitas:9446#x",
    ],
)
def test_endereco_da_sala_e_so_esquema_nome_e_porta(sala: str) -> None:
    for p in (Parametros(VISITS_INTERNAL_URL=sala), _producao(VISITS_INTERNAL_URL=sala)):
        assert any("so o esquema, o nome e a porta da sala" in e for e in p.problemas())


# --- chave separada no cofre --------------------------------------------------------------------------------------


def test_chave_de_terceiros_tem_padrao_e_e_diferente_da_dos_proprios() -> None:
    p = Parametros()
    assert p.VAULT_TRANSIT_KEY_THIRD_PARTY == "mcp-claro-chaves-terceiros"
    assert p.VAULT_TRANSIT_KEY_THIRD_PARTY != p.VAULT_TRANSIT_KEY
    assert not any("VAULT_TRANSIT_KEY" in e for e in p.problemas())


def test_chave_de_terceiros_igual_a_dos_proprios_e_recusada() -> None:
    p = Parametros(VAULT_TRANSIT_KEY_THIRD_PARTY="mcp-claro-chaves")
    assert any("deve ser diferente de VAULT_TRANSIT_KEY" in e for e in p.problemas())
    p = Parametros(VAULT_TRANSIT_KEY="outra", VAULT_TRANSIT_KEY_THIRD_PARTY="outra")
    assert any("deve ser diferente de VAULT_TRANSIT_KEY" in e for e in p.problemas())


@pytest.mark.parametrize("nome", ["", "-chave", "chave/../outra", "chave..x", "chave com espaco", "chave/x", "a" * 129])
def test_nome_da_chave_de_terceiros_e_conferido(nome: str) -> None:
    assert any(
        "VAULT_TRANSIT_KEY_THIRD_PARTY aceita" in e for e in Parametros(VAULT_TRANSIT_KEY_THIRD_PARTY=nome).problemas()
    )


@pytest.mark.parametrize("nome", ["mcp-claro-chaves-terceiros", "terceiros.v2", "T_1", "a" * 128])
def test_nome_valido_da_chave_de_terceiros_passa(nome: str) -> None:
    assert not any("VAULT_TRANSIT_KEY" in e for e in Parametros(VAULT_TRANSIT_KEY_THIRD_PARTY=nome).problemas())


# --- rastro --------------------------------------------------------------------------------------------------------


def test_nenhum_valor_da_propagacao_de_rastro_libera_terceiros() -> None:
    """D10: ``traceparent``, ``tracestate`` e ``baggage`` nunca vao a terceiros, sem valor que libere."""
    assert {str(v) for v in PropagacaoRastro} == {"nunca", "sistemas_proprios"}
    for valor in ("terceiros", "todos", "sempre"):
        with pytest.raises(ValidationError):
            Parametros(OBS_TRACE_PROPAGATE_DOWNSTREAM=valor)
