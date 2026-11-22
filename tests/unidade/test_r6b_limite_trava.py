"""R6b-2, sem banco: criar um limite toma a trava consultiva do sistema (compartilhada) antes da trava da versao
revisada e antes de ler a vitrine, na ordem da porta de saida (as consultivas antes das linhas). A chave e a mesma
que a quarentena toma exclusiva (``saida.travas.chaves_do_alcance("sistema", ...)``)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.responses import Response

from mcp_claro.saida import travas
from mcp_claro.telas import consultas, limites
from mcp_claro.telas.seguranca import Formulario
from tests.unidade.test_telas_rodada2 import _arranjo, _pedido


class _Conexao:
    def __init__(self, passos: list[Any]) -> None:
        self.passos = passos

    async def execute(self, sql: Any, params: dict[str, Any] | None = None) -> None:
        self.passos.append((str(sql), dict(params or {})))


def test_travar_sistema_toma_a_chave_da_quarentena_compartilhada() -> None:
    sistema = uuid.uuid4()
    passos: list[Any] = []
    asyncio.run(limites.travar_sistema(_Conexao(passos), sistema))  # type: ignore[arg-type]
    assert passos == [
        ("SELECT pg_advisory_xact_lock_shared(:k)", {"k": k})
        for k in travas.chaves_do_alcance("sistema", sistema_id=sistema)
    ]
    assert [k for _, p in passos for k in p.values()] == [travas.chave("sistema", sistema)]


@pytest.mark.parametrize("achada", [True, False])
def test_criar_trava_o_sistema_antes_da_revisao_e_da_vitrine(monkeypatch: pytest.MonkeyPatch, achada: bool) -> None:
    t, _, _ = _arranjo(monkeypatch)
    ferramenta, sistema = uuid.uuid4(), uuid.uuid4()
    passos: list[Any] = []

    async def formulario(*_: Any) -> Formulario:
        escolha = f"{uuid.uuid4()}:{ferramenta}"
        return Formulario([("escolha", escolha), ("destinatarios", "qualquer"), ("por_dia", "1"), ("dias", "1")])

    async def revisao(_: Any, ferramenta_id: uuid.UUID) -> Any:
        passos.append(("revisao", ferramenta_id))
        return SimpleNamespace(id=ferramenta_id, sistema_id=sistema) if achada else None

    async def travar_sistema(_: Any, sistema_id: uuid.UUID) -> None:
        passos.append(("sistema", sistema_id))

    async def travar(_: Any, ferramenta_id: uuid.UUID) -> None:
        passos.append(("trava", ferramenta_id))

    async def escolhas(*_: Any) -> list[Any]:
        passos.append("escolhas")
        return []

    async def maximos(*_: Any) -> dict[str, int]:
        return {"ALLOWANCE_MAX_PER_DAY": 5, "ALLOWANCE_MAX_DAYS": 30}

    async def pagina(*_: Any, status: int, **__: Any) -> Response:
        return Response(status_code=status)

    monkeypatch.setattr(t, "formulario", formulario)
    monkeypatch.setattr(consultas, "revisao_por_id", revisao)
    monkeypatch.setattr(limites, "travar_sistema", travar_sistema)
    monkeypatch.setattr(consultas, "travar_revisao", travar)
    monkeypatch.setattr(consultas, "ferramentas_de_acao", escolhas)
    monkeypatch.setattr(limites.politica, "varios", maximos)
    telas = limites.TelasDeLimites(t)
    monkeypatch.setattr(telas, "_pagina_novo", pagina)
    r = asyncio.run(telas.criar(_pedido()))
    assert r.status_code == 400  # a escolha nao esta na lista (vazia aqui)
    meio = [("sistema", sistema)] if achada else []
    assert passos == [("revisao", ferramenta), *meio, ("trava", ferramenta), "escolhas"]
