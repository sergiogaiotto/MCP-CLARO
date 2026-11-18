"""A partida recusa combinacoes invalidas (plano, "Partida que recusa erro")."""

import pytest

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros, carregar
from mcp_claro.versao import VERSAO_MCP


def test_valores_iniciais_passam() -> None:
    p = carregar()
    assert p.MCP_ACCEPTED_VERSIONS == [VERSAO_MCP]
    assert p.MCP_UPSTREAM_VERSION == "2026-07-28"
    assert p.recurso == "https://mcp-claro.localhost/mcp"
    assert p.ALLOWED_HOSTS == ["mcp-claro.localhost"]
    assert p.FEATURE_PHASE1_READ is False
    assert p.FEATURE_PASSKEY is True


@pytest.mark.parametrize(
    ("campo", "valor", "trecho"),
    [
        ("MCP_ACCEPTED_VERSIONS", "2025-11-25", "decisao 8"),
        ("MCP_ACCEPTED_VERSIONS", "2026-07-28,2025-11-25", "decisao 8"),
        ("MCP_UPSTREAM_VERSION", "2025-11-25", "decisao 8"),
        ("EDGE_MODE", "apigee", "decisao 7"),
        ("EDGE_TOKEN_PRECHECK", "ligado", "Apigee"),
        ("MCP_RESPONSE_MODE", "fluxo", "inteira"),
        ("FEATURE_TASKS", "ligado", "decisao 13"),
        ("PUBLIC_BASE_URL", "http://mcp.exemplo", "https"),
    ],
)
def test_partida_recusa(campo: str, valor: str, trecho: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(campo, valor)
    with pytest.raises(ConfiguracaoInvalida) as erro:
        carregar()
    assert trecho in str(erro.value)


def test_producao_exige_verify_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "producao")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.claro.exemplo")
    monkeypatch.setenv("INTERNAL_CA_FILE", "/run/secrets/ca.pem")
    monkeypatch.setenv("INTERNAL_CLIENT_CA_FILE", "/run/secrets/ca_clientes.pem")
    monkeypatch.setenv("INTERNAL_CERT_FILE", "/run/secrets/cert.pem")
    monkeypatch.setenv("INTERNAL_KEY_FILE", "/run/secrets/key.pem")
    monkeypatch.setenv("DISPATCHER_INTERNAL_URL", "https://despachante:9443")
    monkeypatch.setenv("DB_SSLMODE", "require")
    with pytest.raises(ConfiguracaoInvalida, match="verify-full"):
        carregar()
    monkeypatch.setenv("DB_SSLMODE", "verify-full")
    assert carregar().em_producao


def test_fases_em_ordem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_PHASE3_THIRD_PARTY", "ligado")
    problemas = Parametros().problemas()
    assert any("decisao 10" in p for p in problemas)


def test_liga_desliga_em_portugues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_PHASE1_READ", "ligado")
    monkeypatch.setenv("PATROL_AUTOCLOSE_ENABLED", "desligado")
    p = carregar()
    assert p.FEATURE_PHASE1_READ is True
    assert p.PATROL_AUTOCLOSE_ENABLED is False


def test_liga_desliga_invalido(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_PHASE1_READ", "talvez")
    with pytest.raises(ValueError, match="liga/desliga"):
        Parametros()


def test_url_do_banco_sem_senha(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from mcp_claro.db.engine import url_asyncpg

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:segredo@db:5432/x")
    with pytest.raises(ValueError, match="DATABASE_PASSWORD_FILE"):
        url_asyncpg(Parametros())


def test_tls_interno_sem_autoridade_de_clientes_e_recusado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_CERT_FILE", "/run/secrets/cert.pem")
    monkeypatch.setenv("INTERNAL_KEY_FILE", "/run/secrets/key.pem")
    with pytest.raises(ConfiguracaoInvalida, match="falharia aberto"):
        carregar()


def test_producao_sem_tls_interno_e_recusada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "producao")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mcp.claro.exemplo")
    monkeypatch.setenv("INTERNAL_CA_FILE", "/run/secrets/ca.pem")
    monkeypatch.setenv("INTERNAL_CLIENT_CA_FILE", "/run/secrets/ca_clientes.pem")
    with pytest.raises(ConfiguracaoInvalida, match="INTERNAL_CERT_FILE"):
        carregar()
