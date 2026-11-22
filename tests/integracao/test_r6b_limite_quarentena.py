"""R6b-2: limite criado pela tela enquanto a quarentena do servidor entra.

A quarentena (``saida.quarentena.por_em_quarentena``) toma a trava consultiva do sistema, exclusiva, e suspende os
limites ativos que enxerga. A tela que cria o limite (``telas/limites.TelasDeLimites.criar``) toma a mesma trava,
compartilhada, antes de ler a vitrine: ou a quarentena entra antes (e a acao ja nao esta na lista), ou ela espera o
COMMIT da tela e suspende o limite novo. Nos dois casos, depois da nova revisao do servidor, a acao vai para a
ficha, como em ``test_saida_quarentena::test_depois_da_nova_revisao_a_acao_vai_para_a_ficha_e_nao_pelo_limite``.

A prova do revisor (concorrencia), invertida: o teste usa o handler de verdade (POST /telas/limites) e poe a
quarentena inteira, noutra conexao, logo antes do INSERT do limite.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.quarentena import MOTIVO_LIMITE, por_em_quarentena
from mcp_claro.telas import consultas
from mcp_claro.versao import PREFIXO_META
from tests import apoio_terceiros as t3
from tests.idp_oidc_falso import IdpOidcFalso
from tests.integracao import test_entrada_terceiros_apoio as apoio
from tests.integracao.test_entrada_terceiros_apoio import Mundo, subir_mundo
from tests.integracao.test_telas import token_csrf
from tests.integracao.test_telas_fase3_pessoa import entrar

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def mundo(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Mundo]:
    monkeypatch.setattr(apoio, "IdpFalso", functools.partial(IdpOidcFalso, segredo=None))
    # Sem passkey: a tela cria o limite com o login recente (o mesmo caminho do INSERT).
    async with subir_mundo(banco_migrado, extra={"FEATURE_PASSKEY": False}) as m:
        yield m


@pytest.fixture
async def http() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(follow_redirects=False, timeout=60, trust_env=False) as c:
        yield c


async def _limites(m: Mundo) -> list[Any]:
    async with m.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT id, situacao, motivo FROM limites WHERE pessoa_id = :p AND sistema_id = :s"),
                    {"p": m.pessoas["ana"], "s": m.terceiro_id},
                )
            ).all()
        )


async def _parada_ou_feita(m: Mundo, pid: int, tarefa: asyncio.Task[Any]) -> None:
    """Ate a quarentena (processo ``pid`` do banco) terminar ou ficar esperando uma trava (``pg_locks.granted``
    falso). Sem tempo fixo: olha o banco, no maximo 20 s pelo relogio do laco (monotonico)."""
    fim = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < fim and not tarefa.done():
        async with m.dono.connect() as c:
            esperando = (
                await c.execute(text("SELECT count(*) FROM pg_locks WHERE pid = :p AND NOT granted"), {"p": pid})
            ).scalar_one()
        if esperando:
            return
        await asyncio.sleep(0.01)


async def _depois_da_nova_revisao_vai_para_a_ficha(m: Mundo) -> None:
    async with m.dono.begin() as c:
        await t3.gravar_revisao_do_servidor(c, m.terceiro_id)
    resposta = await m.chamar("ana", "t_enviar", {"texto": "depois da nova revisao"})
    op = await m.operacao(str((resposta.meta or {})[PREFIXO_META + "protocolo"]))
    assert op.estado == Estado.AGUARDANDO_FICHA, f"saiu pelo limite de antes da quarentena: {op.estado}"
    assert op.limite_id is None


async def test_quarentena_no_meio_da_criacao_espera_e_suspende_o_limite_novo(
    mundo: Mundo, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = mundo
    ana = await entrar(m, http)
    escolha = f"{m.apps['A']}:{m.f['t_enviar'].id}"
    pagina = await ana.get(f"/telas/limites/novo?escolha={escolha}")
    assert pagina.status_code == 200, pagina.text
    csrf = token_csrf(pagina.text)

    original = consultas.criar_limite
    tarefas: list[asyncio.Task[Any]] = []

    async def quarentena_no_meio(c: Any, **kw: Any) -> Any:
        # A tela ja leu a vitrine (sistema ativo, ferramenta publicada) nesta transacao. A quarentena comeca agora,
        # noutra conexao (rotinas): com a trava do sistema na tela, ela espera o COMMIT.
        pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def por() -> Any:
            async with m.rotinas_db.begin() as q:
                pid.set_result(int((await q.execute(text("SELECT pg_backend_pid()"))).scalar_one()))
                return await por_em_quarentena(q, sistema_id=m.terceiro_id, base=m.revisao_id, papel="rotinas")

        tarefa = asyncio.create_task(por())
        tarefas.append(tarefa)
        await _parada_ou_feita(m, await asyncio.wait_for(pid, 20), tarefa)
        return await original(c, **kw)

    monkeypatch.setattr(consultas, "criar_limite", quarentena_no_meio)
    form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "5", "dias": "5"}
    r = await ana.post("/telas/limites", form, csrf=csrf)
    assert r.status_code == 303, r.text
    (tarefa,) = tarefas
    feita = await asyncio.wait_for(tarefa, 30)
    assert feita is not None

    async with m.dono.connect() as c:
        sistema = (
            await c.execute(text("SELECT situacao FROM sistemas WHERE id = :s"), {"s": m.terceiro_id})
        ).scalar_one()
    assert sistema == "quarentena"
    limites = await _limites(m)
    # A quarentena esperou a tela e viu o limite novo: suspenso, com o motivo, como os outros.
    assert [(x.situacao, x.motivo) for x in limites] == [("suspenso", MOTIVO_LIMITE)], "nasceu ativo na quarentena"
    assert feita.limites_suspensos == 1
    await _depois_da_nova_revisao_vai_para_a_ficha(m)


async def test_quarentena_antes_da_criacao_tira_a_acao_da_lista(mundo: Mundo, http: httpx2.AsyncClient) -> None:
    m = mundo
    ana = await entrar(m, http)
    escolha = f"{m.apps['A']}:{m.f['t_enviar'].id}"
    pagina = await ana.get(f"/telas/limites/novo?escolha={escolha}")
    assert pagina.status_code == 200, pagina.text
    csrf = token_csrf(pagina.text)
    async with m.rotinas_db.begin() as q:
        assert await por_em_quarentena(q, sistema_id=m.terceiro_id, base=m.revisao_id, papel="rotinas") is not None
    form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "5", "dias": "5"}
    r = await ana.post("/telas/limites", form, csrf=csrf)
    # A acao saiu da vitrine com a quarentena: a tela diz que ela nao esta nas ordens que valem agora.
    assert r.status_code == 404, r.text
    assert "Esta ação não está nas suas ordens de serviço que valem agora." in r.text
    assert await _limites(m) == []
    await _depois_da_nova_revisao_vai_para_a_ficha(m)
