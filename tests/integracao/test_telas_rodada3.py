"""Terceira rodada de correcoes da Fase 2 nas telas, de ponta a ponta (PostgreSQL de verdade).

- O ADMIN reativa, pela tela de aplicativos, um aplicativo assistente que a conferencia do banco na partida pausou
  (antes, so por SQL). A tela das portas lembra de reativar antes de reabrir a saida.
- Quando a revisao do ADMIN termina antes de a pessoa criar o limite, a tela diz que o ADMIN acabou de revisar a
  acao e leva a "Criar limite" de novo (antes: "Esta acao nao esta nas suas ordens de servico").

As telas usam o usuario do banco da entrada (``SET ROLE mcp_claro_entrada``): se faltar GRANT, estes testes falham.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

import httpx2
import pytest
from sqlalchemy import text

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.telas import limites
from tests.integracao import test_telas, test_telas_acoes
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_telas import token_csrf
from tests.integracao.test_telas_acoes import assinar, cadastrar_passkey, desafio, entrar
from tests.integracao.test_telas_rodada2 import escolha_de, revisao_ativa, revisar

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def telas(banco_migrado: Parametros) -> AsyncIterator[test_telas.Ambiente]:
    async with test_telas.subir(banco_migrado) as a:
        yield a


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with test_telas_acoes.ambiente(banco_migrado, monkeypatch) as a:
        yield a


@pytest.fixture
async def http() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(follow_redirects=False, timeout=15, trust_env=False) as cliente:
        yield cliente


# --- T1: o ADMIN reativa o aplicativo assistente pausado ---


async def situacoes(telas: test_telas.Ambiente, *ids: uuid.UUID) -> dict[uuid.UUID, str]:
    async with telas.engine.connect() as c:
        linhas = (
            await c.execute(text("SELECT id, situacao FROM aplicativos WHERE id = ANY(:i)"), {"i": list(ids)})
        ).all()
    return {linha.id: linha.situacao for linha in linhas}


async def test_admin_reativa_o_assistente_pausado_e_so_ele(telas: test_telas.Ambiente) -> None:
    s = test_telas.sufixo()
    async with telas.engine.begin() as c:
        pausado = await repositorio.habilitar_aplicativo(
            c, client_id=f"pausado-{s}", nome=f"Assistente pausado {s}", familia="x"
        )
        ativo = await repositorio.habilitar_aplicativo(c, client_id=f"ativo-{s}", nome=f"Assistente {s}", familia="x")
        programa = await repositorio.habilitar_aplicativo(
            c, client_id=f"robo-{s}", nome=f"Robo {s}", familia="programa", tipo="programa"
        )
        # Como a conferencia do banco na partida deixa: pausados.
        await c.execute(
            text("UPDATE aplicativos SET situacao = 'pausado' WHERE id = ANY(:i)"), {"i": [pausado, programa]}
        )
    reativar = f"/telas/admin/aplicativos/{pausado}/reativar"

    # Quem nao e ADMIN recebe 403, e nada muda.
    bia = telas.navegador()
    await telas.entrar(bia, f"bia-{s}", "Bia")
    r = await bia.post(reativar, {}, csrf=await bia.csrf())
    assert r.status_code == 403
    assert "só do ADMIN" in r.text
    assert (await situacoes(telas, pausado))[pausado] == "pausado"

    nav = telas.navegador()
    admin = f"admin-{s}"
    await telas.entrar(nav, admin, "Carla Admin")
    await telas.tornar_admin(admin)
    admin_id = (await telas.pessoa(admin)).id

    # A tela explica por que esta pausado e o que acontece ao reativar; o programa volta pela tela dele.
    r = await nav.get("/telas/admin/aplicativos")
    assert r.status_code == 200
    assert '<section class="cartao" id="pausados">' in r.text
    assert "A conferência do banco, feita quando o despachante liga, pausou" in r.text
    assert "Ao reativar, os pedidos dele voltam a passar, se nenhum bloqueio ou pausa os segurar." in r.text
    assert "As ações dele que já pararam não voltam" in r.text
    assert f'action="{reativar}"' in r.text
    assert f'action="/telas/admin/aplicativos/{programa}/reativar"' not in r.text
    assert f'<a href="/telas/admin/programas/{programa}">Reative na tela do programa</a>' in r.text
    assert f'action="/telas/admin/aplicativos/{ativo}/reativar"' not in r.text
    csrf = token_csrf(r.text)

    # A tela que reabre a saida lembra de reativar antes.
    r = await nav.get("/telas/admin/portas")
    # Quantos estao pausados depende do que os outros testes deixaram: singular ou plural, a frase concorda.
    um = re.search(r"Um aplicativo está pausado pela", r.text) is not None
    if um:
        assert "Reabrir a saída não o reativa, e o que ele pediu fica parado" in r.text
        assert "Para o que espera seguir, reative o aplicativo antes de reabrir a saída." in r.text
        assert '<a href="/telas/admin/aplicativos#pausados">Reativar o aplicativo</a>' in r.text
    else:
        assert re.search(r"\d+ aplicativos estão pausados pela", r.text)
        assert "Reabrir a saída não os reativa, e o que eles pediram fica parado" in r.text
        assert "Para o que espera seguir, reative os aplicativos antes de reabrir a saída." in r.text
        assert '<a href="/telas/admin/aplicativos#pausados">Reativar os aplicativos</a>' in r.text
    assert f"Assistente pausado {s}" in r.text
    assert "a saída que a conferência do cofre fechou reabre sozinha" in r.text
    assert "Reabrir é sempre decisão registrada" not in r.text

    # Sem o token CSRF: recusado, nada muda.
    r = await nav.post(reativar, {})
    assert r.status_code == 403
    assert (await situacoes(telas, pausado))[pausado] == "pausado"

    # Aplicativo ativo e programa nao sao mexidos (o programa volta pela tela do programa).
    for alvo in (ativo, programa, uuid.uuid4()):
        r = await nav.post(f"/telas/admin/aplicativos/{alvo}/reativar", {}, csrf=csrf)
        assert r.status_code == 303
        assert r.headers["location"] == "/telas/admin/aplicativos?aviso=aplicativo_nada_mudou"
    assert await situacoes(telas, ativo, programa) == {ativo: "ativo", programa: "pausado"}
    # O aviso vale tambem para o programa pausado (que ESTA pausado): nao diz "nao estava pausado".
    r = await nav.get("/telas/admin/aplicativos?aviso=aplicativo_nada_mudou")
    assert "Nada mudou: este aplicativo não é um assistente pausado." in r.text
    assert "não estava pausado" not in r.text

    # O ADMIN reativa: o aplicativo volta a ativo, e o extrato registra quem fez.
    r = await nav.post(reativar, {}, csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/admin/aplicativos?aviso=aplicativo_reativado"
    assert (await situacoes(telas, pausado))[pausado] == "ativo"
    r = await nav.get(r.headers["location"])
    assert "Pronto. O aplicativo foi reativado." in r.text
    assert f'action="{reativar}"' not in r.text
    # Um segundo clique nao muda nada nem registra de novo.
    r = await nav.post(reativar, {}, csrf=csrf)
    assert r.headers["location"] == "/telas/admin/aplicativos?aviso=aplicativo_nada_mudou"
    linhas = await telas.extrato(admin_id, "admin_aplicativo_reativado")
    assert [(x.frase, x.aplicativo_id, x.dados) for x in linhas] == [
        (
            f"Aplicativo Assistente pausado {s} reativado por Carla Admin; estava pausado pela conferencia do banco"
            " na partida.",
            pausado,
            {"client_id": f"pausado-{s}"},
        )
    ]
    async with telas.engine.connect() as c:
        outras = (
            await c.execute(
                text(
                    "SELECT count(*) FROM extrato WHERE tipo = 'admin_aplicativo_reativado' AND aplicativo_id = ANY(:i)"
                ),
                {"i": [ativo, programa]},
            )
        ).scalar_one()
    assert outras == 0

    # Com o assistente reativado, a tela das portas so fala do programa ainda pausado.
    r = await nav.get("/telas/admin/portas")
    assert f"Assistente pausado {s}" not in r.text
    assert f"Robo {s}" in r.text


# --- T4: a revisao do ADMIN termina antes de a pessoa criar o limite ---


async def test_criar_depois_da_revisao_do_admin_leva_a_criar_de_novo(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    mensagem = amb.vitrine["enviar_mensagem"]
    admin = await entrar(amb, http, "pessoa-admin", "Admin")
    ana = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, ana)
    escolha, pagina = await escolha_de(ana, mensagem)
    csrf = token_csrf(pagina)
    form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "1", "dias": "1"}
    # A pessoa toca na passkey; antes de o POST de criar chegar, o ADMIN grava uma revisao nova da acao.
    credencial = await assinar(ana, aut, "/telas/limites/desafio", csrf, form)
    assert (await revisar(amb, admin, "enviar_mensagem", [])).status_code == 303
    nova = await revisao_ativa(amb, "enviar_mensagem")
    assert escolha != f"{amb.aplicativo_id}:{nova.id}"

    r = await ana.post("/telas/limites", {**form, "credencial": credencial}, csrf=csrf)
    assert r.status_code == 409
    assert limites.REVISADA_AGORA == "O ADMIN acabou de revisar esta ação. Abra Criar limite de novo."
    assert limites.REVISADA_AGORA in r.text
    assert "não está nas suas ordens de serviço" not in r.text
    link = "/telas/limites/novo?" + urlencode({"escolha": f"{amb.aplicativo_id}:{nova.id}"})
    assert f'<a class="botao" href="{link}" hx-boost="false">Criar limite para esta ação</a>' in r.text
    async with amb.admin.connect() as c:
        criados = (
            await c.execute(
                text("SELECT count(*) FROM limites WHERE pessoa_id = :p AND aplicativo_id = :a"),
                {"p": amb.ana_id, "a": amb.aplicativo_id},
            )
        ).scalar_one()
    assert criados == 0

    # O desafio da passkey com o formulario de antes (o caso comum: o formulario foi aberto antes da revisao e o
    # toque vem depois) diz o mesmo e manda o caminho da versao nova, que o passkey.js mostra como botao.
    r = await desafio(ana, "/telas/limites/desafio", csrf, form)
    assert r.status_code == 409
    assert r.json() == {
        "erro": limites.REVISADA_AGORA,
        "link": {"caminho": link, "texto": "Criar limite para esta ação"},
    }

    # O link abre "Criar limite" com a versao revisada nova, e o limite e criado.
    r = await ana.get(link)
    assert r.status_code == 200
    novo: dict[str, Any] = {**form, "escolha": f"{amb.aplicativo_id}:{nova.id}"}
    csrf = token_csrf(r.text)
    credencial = await assinar(ana, aut, "/telas/limites/desafio", csrf, novo)
    r = await ana.post("/telas/limites", {**novo, "credencial": credencial}, csrf=csrf)
    assert r.status_code == 303, r.text
