"""Confere que o exemplo da geracao antiga (kit 1.30.0) NAO fala a 2026-07-28 (Etapa 0)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from tests.apoio import cabecalhos_2026, pedido_2026, porta_livre

RAIZ = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integracao


@pytest.fixture(scope="module")
def antigo() -> Iterator[str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv nao encontrado")
    porta = porta_livre()
    ambiente = os.environ | {"PORTA": str(porta), "HOST": "127.0.0.1"}
    proc = subprocess.Popen(  # noqa: S603 - comando fixo de teste
        [
            uv,
            "run",
            "--no-project",
            "--python",
            sys.executable,
            "--with",
            "mcp==1.30.0",
            "python",
            str(RAIZ / "exemplos/geracao_antiga/servidor.py"),
        ],
        env=ambiente,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{porta}/mcp"
    try:
        for _ in range(300):
            try:
                httpx2.get(f"http://127.0.0.1:{porta}/", timeout=0.5)
                break
            except httpx2.TransportError:
                if proc.poll() is not None:
                    pytest.fail(f"o exemplo antigo nao subiu: {proc.stderr.read().decode()[-2000:]}")  # type: ignore[union-attr]
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


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
    discover_ok = r.status_code == 200 and "2026-07-28" in str(
        corpo.get("result", {}).get("supportedVersions", [])
    )
    assert not discover_ok, f"o exemplo antigo nao deveria falar 2026-07-28: {r.status_code} {r.text[:300]}"
