"""O app do despachante liga a conferencia do cofre (terceira rodada, nucleo), com o OpenBao de verdade.

Com ``cifra`` dada (testes, cifra local), a porta de saida fica sem cliente do cofre, mas com o intervalo e os
e-mails do ADMIN dos parametros. Sem ``cifra``, o app entra no cofre e a porta que sobe no ciclo de vida
confere a saude dele: com o OpenBao selado, a saida fecha sozinha e o aviso vai ao e-mail de
``NOTIFY_ADMIN_EMAILS``; destravado, a saida reabre.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from mcp_claro.cofre import Cofre
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import app as despachante_app
from mcp_claro.saida.despachante import PortaDeSaida
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_cofre import Bao, openbao
from tests.integracao.test_saida_cofre_rodada3 import _fechada_pelo_cofre, abrir_a_saida, avisos, saida
from tests.integracao.test_saida_disputas import amb, esperar

__all__ = ["amb", "openbao"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


async def _aberta(amb: Ambiente) -> bool:
    return (await saida(amb))[0]


async def test_app_liga_a_conferencia_do_cofre_so_com_o_cofre_de_verdade(
    amb: Ambiente, openbao: Bao, banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    criadas: list[PortaDeSaida] = []

    class PortaAnotada(PortaDeSaida):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            criadas.append(self)

    await abrir_a_saida(amb)
    monkeypatch.setattr(despachante_app, "PortaDeSaida", PortaAnotada)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    vault = openbao.parametros("despachante")
    p = banco_migrado.model_copy(
        update={
            "VAULT_ADDR": vault.VAULT_ADDR,
            "VAULT_ROLE_ID_FILE": vault.VAULT_ROLE_ID_FILE,
            "VAULT_SECRET_ID_FILE": vault.VAULT_SECRET_ID_FILE,
            "VAULT_CHECK_SECONDS": 5,
            "OUTBOX_SWEEP_SECONDS": 1,
            "NOTIFY_ADMIN_EMAILS": [email],
        }
    )
    try:
        # 1. Com cifra dada: sem cliente do cofre, mas com o intervalo e os e-mails dos parametros.
        app = despachante_app.criar_app_despachante(p, amb.despachante_db, CifraFalsa())
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(criadas[0].assumiu.wait(), 10)
            assert criadas[0].cofre is None
            assert criadas[0].conferir_cofre == 5.0
            assert criadas[0].emails_admin == (email,)
        # 2. Sem cifra: o app entra no cofre e a porta confere a saude dele.
        criadas.clear()
        app = despachante_app.criar_app_despachante(p, amb.despachante_db, None)
        async with app.router.lifespan_context(app):
            porta = criadas[0]
            assert isinstance(porta.cofre, Cofre)
            await asyncio.wait_for(porta.assumiu.wait(), 10)
            assert await _aberta(amb)
            openbao.api("POST", "sys/seal")
            try:
                await esperar(lambda: _fechada_pelo_cofre(amb), prazo=20)
                assert await saida(amb) == (False, "o cofre esta trancado", "rotina:cofre")
            finally:
                openbao.destravar()
            await esperar(lambda: _aberta(amb), prazo=20)
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado", "cofre_destravado"]
    finally:
        await abrir_a_saida(amb)
