"""Entrada dos alertas do Grafana: formato aceito, cortes e senha em tempo constante."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from mcp_claro.avisos.fila import conferir_corpo
from mcp_claro.avisos.grafana import ALERTAS_POR_PEDIDO, AlertaInvalido, ler_alertas, senha_confere


def carga_grafana(**alerta: Any) -> dict[str, Any]:
    """Corpo como o ponto de contato "Webhook" do Grafana Alerting manda (versao 1)."""
    base = {
        "status": "firing",
        "labels": {"alertname": "Recusas por versao", "severity": "alta", "grafana_folder": "MCP-CLARO"},
        "annotations": {"summary": "Recusas por versao antiga subiram.", "description": "Mais detalhes."},
        "startsAt": "2026-11-18T10:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://grafana:3000/alerting/grafana/abc/view",
        "fingerprint": "c6eadffa33fcdf37",
        "silenceURL": "http://grafana:3000/alerting/silence/new",
        "values": {"A": 42},
    }
    return {
        "receiver": "mcp-claro-rotinas",
        "status": "firing",
        "orgId": 1,
        "alerts": [base | alerta],
        "groupLabels": {},
        "commonLabels": {"alertname": "Recusas por versao"},
        "externalURL": "http://grafana:3000/",
        "version": "1",
        "groupKey": "{}:{}",
        "truncatedAlerts": 0,
        "title": "[FIRING:1] Recusas por versao",
        "state": "alerting",
        "message": "**Firing**",
    }


def test_le_alerta_padrao_do_grafana() -> None:
    alertas, cortados = ler_alertas(carga_grafana())
    assert cortados == 0
    (a,) = alertas
    assert a.nome == "Recusas por versao"
    assert a.situacao == "firing"
    assert a.severidade == "alta"
    assert a.chave_unica == "grafana:c6eadffa33fcdf37:firing:2026-11-18T10:00:00Z"
    assert "disparou" in a.assunto()
    corpo = conferir_corpo(a.corpo())
    assert corpo["texto"].startswith("O alerta Recusas por versao disparou.")
    assert "generatorURL" not in str(corpo)  # so os campos escolhidos entram no aviso
    assert "values" not in corpo


def test_resolvido_tem_outra_chave() -> None:
    (disparou,), _ = ler_alertas(carga_grafana())
    (voltou,), _ = ler_alertas(carga_grafana(status="resolved"))
    assert voltou.situacao_legivel == "voltou ao normal"
    assert voltou.chave_unica != disparou.chave_unica


def test_corta_alertas_demais() -> None:
    carga = carga_grafana()
    carga["alerts"] = carga["alerts"] * (ALERTAS_POR_PEDIDO + 5)
    alertas, cortados = ler_alertas(carga)
    assert len(alertas) == ALERTAS_POR_PEDIDO
    assert cortados == 5


@pytest.mark.parametrize(
    "carga",
    [
        [],
        {"alerts": "nao e lista"},
        {"alerts": [1]},
        {"status": "x", "alerts": [{"status": "talvez"}]},
        carga_grafana(fingerprint="abc; DROP"),
    ],
)
def test_corpo_invalido(carga: Any) -> None:
    with pytest.raises(AlertaInvalido):
        ler_alertas(carga)


def test_senha_bearer_e_basic() -> None:
    senha = "senha-do-webhook-interno"
    assert senha_confere(f"Bearer {senha}", senha)
    basic = base64.b64encode(f"grafana:{senha}".encode()).decode()
    assert senha_confere(f"Basic {basic}", senha)
    assert not senha_confere(f"Bearer {senha}x", senha)
    assert not senha_confere("Basic " + base64.b64encode(b"grafana:outra").decode(), senha)
    assert not senha_confere("Basic ###", senha)
    assert not senha_confere("Basic " + base64.b64encode(b"sem-dois-pontos").decode(), senha)
    assert not senha_confere(None, senha)
    assert not senha_confere("Token " + senha, senha)
    assert not senha_confere("Bearer ", "")
