"""Confere que o exemplo da geracao antiga (kit 1.30.0) NAO fala a 2026-07-28 (Etapa 0)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx2
import pytest

from tests.apoio import cabecalhos_2026, pedido_2026, sistema_geracao_antiga

pytestmark = pytest.mark.integracao


@pytest.fixture(scope="module")
def antigo() -> Iterator[str]:
    with sistema_geracao_antiga() as url:
        yield url


def test_fala_a_geracao_antiga(antigo: str) -> None:
    corpo = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "teste", "version": "0"},
        },
    }
    r = httpx2.post(antigo, json=corpo, headers={"accept": "application/json, text/event-stream"}, timeout=10)
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


def test_nao_fala_2026(antigo: str) -> None:
    r = httpx2.post(
        antigo,
        json=pedido_2026("server/discover"),
        headers=cabecalhos_2026("server/discover") | {"accept": "application/json, text/event-stream"},
        timeout=10,
    )
    corpo = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    discover_ok = r.status_code == 200 and "2026-07-28" in str(corpo.get("result", {}).get("supportedVersions", []))
    assert not discover_ok, f"o exemplo antigo nao deveria falar 2026-07-28: {r.status_code} {r.text[:300]}"
