"""Assinatura dos webhooks pelo Standard Webhooks, conferida com a propria biblioteca."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from standardwebhooks import Webhook
from standardwebhooks.webhooks import WebhookVerificationError

from mcp_claro.avisos.fila import Aviso
from mcp_claro.avisos.webhook import EnvioWebhook, SegredoInvalido, assinar, conferir_segredo, corpo_do_webhook

CHAVE = bytes(range(32))
SEGREDO = "whsec_" + base64.b64encode(CHAVE).decode()


def _aviso(**kw: object) -> Aviso:
    base: dict[str, object] = {
        "id": 7,
        "canal": "webhook",
        "destino": "https://destino.exemplo/ganchos",
        "evento": "ferramenta_suspensa",
        "assunto": "MCP-CLARO: ferramenta suspensa",
        "corpo": {"texto": "Ferramenta suspensa.", "ferramenta": "fatura_x"},
        "tentativas": 0,
        "criado_em": datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
    }
    return Aviso(**(base | kw))  # type: ignore[arg-type]


def test_assinatura_confere_com_a_biblioteca() -> None:
    aviso = _aviso()
    corpo = corpo_do_webhook(aviso)
    cabecalhos = assinar(CHAVE, aviso.identificador, datetime.now(UTC), corpo)
    assert set(cabecalhos) == {"webhook-id", "webhook-timestamp", "webhook-signature"}
    assert cabecalhos["webhook-signature"].startswith("v1,")
    carga = Webhook(SEGREDO).verify(corpo, cabecalhos)
    assert carga == {
        "type": "ferramenta_suspensa",
        "timestamp": "2026-11-18T12:00:00Z",
        "data": {"texto": "Ferramenta suspensa.", "ferramenta": "fatura_x"},
    }


def test_corpo_alterado_ou_outro_segredo_nao_confere() -> None:
    aviso = _aviso()
    corpo = corpo_do_webhook(aviso)
    cabecalhos = assinar(CHAVE, aviso.identificador, datetime.now(UTC), corpo)
    with pytest.raises(WebhookVerificationError):
        Webhook(SEGREDO).verify(corpo.replace(b"fatura_x", b"fatura_y"), cabecalhos)
    with pytest.raises(WebhookVerificationError):
        Webhook(base64.b64encode(b"x" * 32).decode()).verify(corpo, cabecalhos)
    trocado = cabecalhos | {"webhook-id": "msg_outro"}
    with pytest.raises(WebhookVerificationError):
        Webhook(SEGREDO).verify(corpo, trocado)


def test_assinatura_velha_e_recusada_pelo_destino() -> None:
    aviso = _aviso()
    corpo = corpo_do_webhook(aviso)
    cabecalhos = assinar(CHAVE, aviso.identificador, datetime.now(UTC) - timedelta(minutes=10), corpo)
    with pytest.raises(WebhookVerificationError, match="too old"):
        Webhook(SEGREDO).verify(corpo, cabecalhos)


def test_identificador_estavel_entre_tentativas() -> None:
    primeira = _aviso(tentativas=0)
    terceira = _aviso(tentativas=2)
    assert primeira.identificador == terceira.identificador
    assert corpo_do_webhook(primeira) == corpo_do_webhook(terceira)
    assert _aviso(id=8).identificador != primeira.identificador


def test_corpo_e_json_compacto_e_ordenado() -> None:
    corpo = corpo_do_webhook(_aviso())
    assert b" " not in corpo.replace(b"Ferramenta suspensa.", b"")
    assert json.loads(corpo)["type"] == "ferramenta_suspensa"


@pytest.mark.parametrize(
    "segredo",
    [SEGREDO, SEGREDO.removeprefix("whsec_"), base64.b64encode(b"a" * 30).decode().rstrip("=")],
)
def test_segredo_aceito(segredo: str) -> None:
    assert len(conferir_segredo(segredo)) >= 24
    EnvioWebhook(SecretStr(segredo))


@pytest.mark.parametrize(
    ("segredo", "trecho"),
    [
        ("nao e base64 !!", "base64"),
        (base64.b64encode(b"curto").decode(), "de 24 a 64 bytes"),
        (base64.b64encode(b"x" * 65).decode(), "de 24 a 64 bytes"),
    ],
)
def test_segredo_recusado(segredo: str, trecho: str) -> None:
    with pytest.raises(SegredoInvalido, match=trecho):
        conferir_segredo(segredo)
