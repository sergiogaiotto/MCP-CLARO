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


# --- Revisao final: o cartao de pausados mostra o intervalo, e a saida fechada aparece ---


async def test_cartao_de_pausados_mostra_o_intervalo_e_a_saida_fechada(telas: test_telas.Ambiente) -> None:
    """A conferencia do banco pausa e grava "intervalo_perdido" (sem pessoa) na mesma transacao; uma partida
    seguinte que tambem nao confere grava outro intervalo, sem pausar ninguem. O cartao junta os dois, desde a pausa
    (um intervalo de antes da pausa fica de fora), e leva a Bloqueios e pausas. O aplicativo de um programa
    encerrado nao entra no cartao. Com a saida fechada, a pagina diz isso e leva a Portas, tambem depois de
    reativar."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from mcp_claro import continuidade
    from mcp_claro.extrato import registrar
    from mcp_claro.regras import estado
    from mcp_claro.regras.bloqueios import quem_admin

    async def intervalo(c: Any, desde: datetime, ate: datetime) -> None:
        await registrar(
            c,
            papel="despachante",
            tipo="intervalo_perdido",
            campos={"desde": desde.isoformat(), "ate": ate.isoformat()},
            dados={"desde": desde.isoformat(), "ate": ate.isoformat(), "fechamentos_perdidos": 1},
        )

    async def pausar(c: Any, aplicativo_id: uuid.UUID, nome: str) -> None:
        await c.execute(
            text("UPDATE aplicativos SET situacao = 'pausado', atualizado_em = now() WHERE id = :i"),
            {"i": aplicativo_id},
        )
        await registrar(
            c,
            papel="despachante",
            tipo="aplicativo_pausado_continuidade",
            campos={"aplicativo": nome},
            aplicativo_id=aplicativo_id,
        )

    s = test_telas.sufixo()
    agora = datetime.now(UTC).replace(microsecond=0)
    antigo, desde, ate_1, ate_2 = (
        agora - timedelta(days=800),
        agora - timedelta(days=400),
        agora - timedelta(hours=2),
        agora,
    )
    async with telas.engine.begin() as c:
        await intervalo(c, antigo, agora - timedelta(days=799))  # de outra vez, antes desta pausa
    async with telas.engine.begin() as c:
        pausado = await repositorio.habilitar_aplicativo(
            c, client_id=f"pausado-int-{s}", nome=f"Pausado intervalo {s}", familia="x"
        )
        robo = await repositorio.habilitar_aplicativo(
            c, client_id=f"robo-enc-{s}", nome=f"Robo encerrado {s}", familia="programa", tipo="programa"
        )
        pessoas = [
            (
                await c.execute(
                    text(
                        "INSERT INTO pessoas (emissor, sujeito, papel, situacao)"
                        " VALUES ('https://login.teste', :s, 'pessoa', 'ativa') RETURNING id"
                    ),
                    {"s": f"resp-{n}-{s}"},
                )
            ).scalar_one()
            for n in (1, 2)
        ]
        await c.execute(
            text(
                "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim, situacao)"
                " VALUES (:a, :r, :s, 'teste', current_date + 30, 'encerrado')"
            ),
            {"a": robo, "r": pessoas[0], "s": pessoas[1]},
        )
        # A primeira partida que nao confere: intervalo e pausa na mesma transacao.
        await intervalo(c, desde, ate_1)
        await pausar(c, pausado, f"Pausado intervalo {s}")
        await pausar(c, robo, f"Robo encerrado {s}")
        await estado.mudar_saida(c, aberta=False, quem=continuidade.QUEM, motivo="teste do cartao de pausados")
    async with telas.engine.begin() as c:
        await intervalo(c, ate_1, ate_2)  # a partida seguinte tambem nao confere e nao pausa ninguem

    nav = telas.navegador()
    admin = f"admin-int-{s}"
    await telas.entrar(nav, admin, "Carla Admin")
    await telas.tornar_admin(admin)
    admin_id = (await telas.pessoa(admin)).id
    try:
        r = await nav.get("/telas/admin/aplicativos")
        assert r.status_code == 200
        cartao = re.search(r'<section class="cartao" id="pausados">(.*?)</section>', r.text, re.S)
        assert cartao is not None
        zona = ZoneInfo("America/Sao_Paulo")
        assert (
            "A conferência registrou que pode faltar no extrato o que aconteceu entre "
            f"{desde.astimezone(zona):%d/%m/%Y %H:%M} e {ate_2.astimezone(zona):%d/%m/%Y %H:%M}."
        ) in cartao.group(1)
        assert f"Pausado intervalo {s}" in cartao.group(1)
        assert f"Robo encerrado {s}" not in cartao.group(1)
        assert '<a href="/telas/admin/bloqueios">Bloqueios e pausas</a>' in cartao.group(1)
        assert "confira no extrato o que pode ter se perdido" not in r.text
        assert '<p class="atencao" id="saida-fechada">' in r.text
        assert '<a href="/telas/admin/portas">Portas</a>' in r.text
        portas = await nav.get("/telas/admin/portas")
        assert f"Robo encerrado {s}" not in portas.text

        r = await nav.post(f"/telas/admin/aplicativos/{pausado}/reativar", {}, csrf=token_csrf(r.text))
        r = await nav.get(r.headers["location"])
        assert "As ações dele só saem com a porta de saída aberta." in r.text
        assert '<p class="atencao" id="saida-fechada">' in r.text
    finally:
        async with telas.engine.begin() as c:
            await estado.mudar_saida(c, aberta=True, quem=quem_admin(admin_id), motivo="fim do teste do cartao")
    r = await nav.get("/telas/admin/aplicativos")
    assert 'id="saida-fechada"' not in r.text


async def test_acao_na_hora_com_a_saida_fechada_diz_que_espera(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    """Com a porta de saida fechada, a acao liberada na hora nao diz "sai em instantes": diz que espera a porta
    reabrir. "Seguir agora" tambem diz isso. Reaberta, ela sai uma vez."""
    from mcp_claro import continuidade
    from mcp_claro.regras import estado
    from mcp_claro.regras.bloqueios import quem_admin
    from mcp_claro.saida.estados import Estado
    from tests.integracao.test_saida import chamadas, esperar_estado, operacao, politica
    from tests.integracao.test_telas_acoes import ficha_de, pedir, protocolo_de

    await politica(amb, OUTBOX_HOLD_SECONDS=600)
    protocolo = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Espera a porta"}))
    op = await ficha_de(amb, protocolo)
    nav = await entrar(amb, http)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    assert (await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)).status_code == 303
    antes = chamadas(amb, "criar_chamado")
    async with amb.despachante_db.begin() as c:
        await estado.mudar_saida(c, aberta=False, quem=continuidade.QUEM, motivo="teste da tela da acao")
    try:
        # Ainda com tempo faltando: a tela ja diz que a porta esta fechada, sem prometer a hora.
        pagina = await nav.get(f"/telas/acoes/{op.id}")
        assert "A porta de saída está fechada agora." in pagina.text
        assert "e não antes da hora marcada" in pagina.text
        assert "para sair." not in pagina.text
        assert "só sai quando a porta de saída reabrir" in pagina.text
        r = await nav.post(f"/telas/acoes/{op.id}/seguir", csrf=csrf)
        assert r.status_code == 303
        assert r.headers["location"].endswith("?aviso=seguir_agora_saida_fechada")
        pagina = await nav.get(r.headers["location"])
        assert "A porta de saída está fechada agora: ela sai quando a porta reabrir" in pagina.text
        assert "A porta de saída está fechada agora. A ação espera e sai quando ela reabrir" in pagina.text
        assert "sai em instantes" not in pagina.text
        parte = await nav.get(f"/telas/acoes/{op.id}/estado")
        assert "A porta de saída está fechada agora." in parte.text
        assert "sai em instantes" not in parte.text
        assert (await operacao(amb, protocolo)).estado == Estado.LIBERADA
        assert chamadas(amb, "criar_chamado") == antes
    finally:
        await continuidade.reabrir_saida(amb.entrada_db, quem_admin(amb.admin_id), "Fim do teste da tela da acao")
    await esperar_estado(amb, protocolo, Estado.ENVIADA, prazo=15)
    assert chamadas(amb, "criar_chamado") == antes + 1
