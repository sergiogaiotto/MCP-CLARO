"""Telas "Bloquear agora", "Pausar" e "Retomar" da pessoa, e as telas do ADMIN de bloqueios, portas e pessoas.

Mesmo arranjo de ``test_telas_autorizacao`` (entrada inteira, despachante, sistema de exemplo, OAuth
falso): cada leitura e uma chamada MCP de verdade pela entrada.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from tests.integracao.test_telas import Navegador, token_csrf
from tests.integracao.test_telas_autorizacao import Ambiente, subir

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Ambiente]:
    async with subir(banco_migrado, monkeypatch, tmp_path) as a:
        yield a


async def pessoa_que_le(amb: Ambiente, nome: str = "Ana") -> tuple[Navegador, str, uuid.UUID]:
    """Uma pessoa com ordem valendo e autorizacao guardada: a leitura funciona."""
    await amb.configurar_direto()
    nav, sujeito, pessoa_id = await amb.nova_pessoa(nome)
    await amb.dar_ordem(pessoa_id)
    assert (await amb.autorizar_pela_tela(nav, "ana")).status_code == 303
    assert (await amb.ler_fatura(sujeito)).structured_content["valor"] == 99.9
    return nav, sujeito, pessoa_id


async def bloqueios_de(amb: Ambiente, pessoa_id: uuid.UUID) -> list[Any]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT * FROM bloqueios WHERE pessoa_id = :p ORDER BY criado_em"), {"p": pessoa_id}
                )
            ).all()
        )


# --- Pessoa: bloquear agora ---


async def test_pessoa_bloqueia_um_aplicativo_e_a_leitura_passa_a_ser_recusada(amb: Ambiente) -> None:
    nav, sujeito, ana_id = await pessoa_que_le(amb)
    r = await nav.get("/telas/bloqueio")
    assert r.status_code == 200
    assert f"Bloquear {amb.aplicativo_nome} agora" in r.text
    assert "Só o ADMIN desfaz um bloqueio." in r.text
    assert "Nada está parado para você agora." in r.text
    csrf = token_csrf(r.text)

    r = await nav.post("/telas/bloqueio", {"aplicativo": str(uuid.uuid4())}, csrf=csrf)
    assert r.status_code == 400
    assert "Escolha um dos aplicativos da lista." in r.text
    r = await nav.post("/telas/bloqueio", {"aplicativo": str(amb.aplicativo_id)}, csrf=csrf)
    assert r.status_code == 303
    comprovante = r.headers["location"]
    assert comprovante.endswith("?aviso=bloqueado")
    linhas = await bloqueios_de(amb, ana_id)
    assert [(b.tipo, b.alcance, b.aplicativo_id, b.quem) for b in linhas] == [
        ("bloqueio", "pessoa_aplicativo", amb.aplicativo_id, f"pessoa:{ana_id}")
    ]
    bloqueio_id = linhas[0].id
    r = await nav.get(comprovante)
    assert r.status_code == 200
    assert "Comprovante do bloqueio" in r.text
    assert f"O aplicativo {amb.aplicativo_nome}, só para você" in r.text
    assert "Valendo agora" in r.text
    assert str(bloqueio_id) in r.text
    assert "Está bloqueado a partir de agora. Só o ADMIN desfaz." in r.text

    with pytest.raises(MCPError, match="bloqueado"):
        await amb.ler_fatura(sujeito)
    ligado = await amb.extrato("bloqueio_ligado", ana_id)
    assert [(linha.frase, linha.aplicativo_id) for linha in ligado] == [
        ("A propria pessoa ligou um bloqueio em uma pessoa num aplicativo.", amb.aplicativo_id)
    ]
    r = await nav.get("/telas/extrato")
    assert "ligou um bloqueio" in r.text

    # Toque repetido nao abre outro: mostra o comprovante do que ja vale.
    r = await nav.post("/telas/bloqueio", {"aplicativo": str(amb.aplicativo_id)}, csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == f"/telas/bloqueio/{bloqueio_id}?aviso=ja_valia"
    assert len(await bloqueios_de(amb, ana_id)) == 1

    # A pessoa nao encerra bloqueio: so o ADMIN.
    r = await nav.post(f"/telas/pausa/{bloqueio_id}/retomar", csrf=csrf)
    assert r.status_code == 403
    assert "Um bloqueio só o ADMIN desfaz" in r.text
    assert (await bloqueios_de(amb, ana_id))[0].encerrado_em is None
    r = await nav.get("/telas/bloqueio")
    assert "Só o ADMIN desfaz." in r.text
    assert f"/telas/pausa/{bloqueio_id}/retomar" not in r.text

    # Outra pessoa nao ve o comprovante.
    bia, _, _ = await amb.nova_pessoa("Bia")
    assert (await bia.get(f"/telas/bloqueio/{bloqueio_id}")).status_code == 404

    # O ADMIN encerra, e a leitura volta.
    adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
    r = await adm.get("/telas/admin/bloqueios")
    assert f"A pessoa Ana, no aplicativo {amb.aplicativo_nome}" in r.text
    r = await adm.post(f"/telas/admin/bloqueios/{bloqueio_id}/encerrar", csrf=token_csrf(r.text))
    assert r.status_code == 303
    assert (await bloqueios_de(amb, ana_id))[0].encerrado_por == f"admin:{admin_id}"
    assert (await amb.ler_fatura(sujeito)).is_error is False
    assert await amb.extrato("bloqueio_encerrado", ana_id)
    frases = [linha.frase for linha in await amb.extrato("admin_bloqueio_encerrado", admin_id)]
    assert frases == ["Bloqueio encerrado por Carla Admin em uma pessoa num aplicativo."]


async def test_bloquear_todos_os_aplicativos_da_pessoa(amb: Ambiente) -> None:
    nav, sujeito, ana_id = await pessoa_que_le(amb)
    r = await nav.post("/telas/bloqueio", {"aplicativo": "todos"}, csrf=await nav.csrf())
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "Todos os seus aplicativos" in r.text
    assert [(b.tipo, b.alcance, b.aplicativo_id) for b in await bloqueios_de(amb, ana_id)] == [
        ("bloqueio", "pessoa", None)
    ]
    with pytest.raises(MCPError, match="Seu acesso ao MCP-CLARO esta bloqueado"):
        await amb.listar(sujeito)
    with pytest.raises(MCPError, match="bloqueado"):
        await amb.ler_fatura(sujeito)


# --- Pessoa: pausar e retomar ---


async def test_pessoa_pausa_e_retoma(amb: Ambiente) -> None:
    nav, sujeito, ana_id = await pessoa_que_le(amb)
    r = await nav.get("/telas/pausa")
    assert r.status_code == 200
    assert "Você mesmo retoma quando quiser" in r.text
    csrf = token_csrf(r.text)
    r = await nav.post("/telas/pausa", {"aplicativo": str(amb.aplicativo_id)}, csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "Comprovante da pausa" in r.text
    assert "Retomar agora" in r.text
    pausa_id = (await bloqueios_de(amb, ana_id))[0].id
    with pytest.raises(MCPError, match="pausado"):
        await amb.ler_fatura(sujeito)

    bia, _, _ = await amb.nova_pessoa("Bia")
    r = await bia.post(f"/telas/pausa/{pausa_id}/retomar", csrf=await bia.csrf())
    assert r.status_code == 404  # a pausa da Ana nao e da Bia
    r = await nav.post(f"/telas/pausa/{pausa_id}/retomar")
    assert r.status_code == 403  # sem CSRF
    assert (await bloqueios_de(amb, ana_id))[0].encerrado_em is None

    r = await nav.get("/telas/pausa")
    assert f"/telas/pausa/{pausa_id}/retomar" in r.text
    r = await nav.post(f"/telas/pausa/{pausa_id}/retomar", csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/pausa?aviso=pausa_retomada"
    r = await nav.get(r.headers["location"])
    assert "A pausa terminou." in r.text
    assert "Nada está parado para você agora." in r.text
    assert (await amb.ler_fatura(sujeito)).is_error is False
    assert (await bloqueios_de(amb, ana_id))[0].encerrado_por == f"pessoa:{ana_id}"
    assert await amb.extrato("pausa_ligada", ana_id)
    assert await amb.extrato("pausa_encerrada", ana_id)
    r = await nav.post(f"/telas/pausa/{pausa_id}/retomar", csrf=csrf)
    assert r.headers["location"] == "/telas/pausa?aviso=ja_terminou"

    # Pausa ligada pelo ADMIN so sobre ela: a propria pessoa tambem retoma.
    adm, _, _ = await amb.nova_pessoa("Carla Admin", admin=True)
    r = await adm.post(
        "/telas/admin/bloqueios",
        {
            "tipo": "pausa",
            "alcance": "pessoa_aplicativo",
            "pessoa": str(ana_id),
            "aplicativo": str(amb.aplicativo_id),
            "motivo": "Conferir um uso estranho.",
        },
        csrf=token_csrf((await adm.get("/telas/admin/bloqueios")).text),
    )
    assert r.status_code == 303
    pausa_do_admin = (await bloqueios_de(amb, ana_id))[-1]
    assert pausa_do_admin.quem.startswith("admin:")
    r = await nav.get("/telas/pausa")
    assert "O ADMIN" in r.text
    r = await nav.post(f"/telas/pausa/{pausa_do_admin.id}/retomar", csrf=csrf)
    assert r.status_code == 303
    assert (await amb.ler_fatura(sujeito)).is_error is False


# --- ADMIN: bloqueio por alcance, portas e CSRF ---


async def test_admin_bloqueia_e_encerra_por_alcance(amb: Ambiente) -> None:
    _, sujeito, ana_id = await pessoa_que_le(amb)
    adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
    r = await adm.get("/telas/admin/bloqueios")
    assert r.status_code == 200
    csrf = token_csrf(r.text)
    pedido = {"tipo": "bloqueio", "motivo": "Teste de bloqueio pelo ADMIN."}

    r = await adm.post("/telas/admin/bloqueios", pedido | {"alcance": "aplicativo"}, csrf=csrf)
    assert r.status_code == 400
    assert "Escolha o aplicativo da lista." in r.text
    r = await adm.post(
        "/telas/admin/bloqueios",
        pedido | {"alcance": "sistema", "sistema": str(amb.sistema_id), "motivo": "x"},
        csrf=csrf,
    )
    assert r.status_code == 400
    assert "Escreva o motivo" in r.text

    casos = [
        ("aplicativo", {"aplicativo": str(amb.aplicativo_id)}, "Este aplicativo esta bloqueado"),
        ("sistema", {"sistema": str(amb.sistema_id)}, "O sistema desta ferramenta esta bloqueado"),
        ("pessoa", {"pessoa": str(ana_id)}, "Seu acesso ao MCP-CLARO esta bloqueado"),
        ("tudo", {}, "O MCP-CLARO esta bloqueado pelo ADMIN"),
    ]
    for alcance, alvo, mensagem in casos:
        try:
            r = await adm.post("/telas/admin/bloqueios", pedido | {"alcance": alcance} | alvo, csrf=csrf)
            assert r.status_code == 303, (alcance, r.text)
            assert r.headers["location"] == "/telas/admin/bloqueios?aviso=bloqueio_ligado"
            async with amb.admin.connect() as c:
                linha = (
                    await c.execute(
                        text("SELECT * FROM bloqueios WHERE alcance = :a AND quem = :q AND encerrado_em IS NULL"),
                        {"a": alcance, "q": f"admin:{admin_id}"},
                    )
                ).one()
            assert linha.motivo == "Teste de bloqueio pelo ADMIN."
            with pytest.raises(MCPError, match=mensagem):
                await amb.ler_fatura(sujeito)
            r = await adm.get("/telas/admin/bloqueios")
            assert f"/telas/admin/bloqueios/{linha.id}/encerrar" in r.text
            r = await adm.post(f"/telas/admin/bloqueios/{linha.id}/encerrar", csrf=csrf)
            assert r.status_code == 303
            assert r.headers["location"] == "/telas/admin/bloqueios?aviso=parada_encerrada"
        finally:
            async with amb.admin.begin() as c:  # nada fica valendo para os outros testes
                await c.execute(
                    text(
                        "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'admin:teste' WHERE quem = :q"
                        " AND encerrado_em IS NULL"
                    ),
                    {"q": f"admin:{admin_id}"},
                )
        assert (await amb.ler_fatura(sujeito)).is_error is False, alcance
    frases = [linha.frase for linha in await amb.extrato("admin_bloqueio_ligado", admin_id)]
    assert frases == [
        "Bloqueio ligado por Carla Admin em um aplicativo.",
        "Bloqueio ligado por Carla Admin em um sistema.",
        "Bloqueio ligado por Carla Admin em uma pessoa.",
        "Bloqueio ligado por Carla Admin em todo o MCP-CLARO.",
    ]
    assert len(await amb.extrato("admin_bloqueio_encerrado", admin_id)) == 4

    bia, _, _ = await amb.nova_pessoa("Bia")
    assert (await bia.get("/telas/admin/bloqueios")).status_code == 403
    r = await bia.post("/telas/admin/bloqueios", pedido | {"alcance": "tudo"}, csrf=await bia.csrf())
    assert r.status_code == 403


async def test_admin_fecha_a_entrada_e_o_mcp_recusa(amb: Ambiente) -> None:
    _, sujeito, _ = await pessoa_que_le(amb)
    adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
    r = await adm.get("/telas/admin/portas")
    assert r.status_code == 200
    assert "aberta" in r.text
    csrf = token_csrf(r.text)
    try:
        r = await adm.post(
            "/telas/admin/portas/entrada", {"nivel": "trancada", "motivo": "Teste da entrada."}, csrf=csrf
        )
        assert r.status_code == 400
        r = await adm.post("/telas/admin/portas/entrada", {"nivel": "fechada", "motivo": "x"}, csrf=csrf)
        assert r.status_code == 400
        r = await adm.post(
            "/telas/admin/portas/entrada", {"nivel": "fechada", "motivo": "Teste da entrada."}, csrf=csrf
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/telas/admin/portas?aviso=entrada_mudou"
        async with amb.admin.connect() as c:
            entrada = (await c.execute(text("SELECT * FROM estado_entrada"))).one()
        assert (entrada.nivel, entrada.motivo, entrada.mudou_por) == (
            "fechada",
            "Teste da entrada.",
            f"admin:{admin_id}",
        )
        with pytest.raises(MCPError, match="entrada do MCP-CLARO esta fechada"):
            await amb.listar(sujeito)
        with pytest.raises(MCPError, match="entrada do MCP-CLARO esta fechada"):
            await amb.ler_fatura(sujeito)
        r = await adm.get("/telas/admin/portas")
        assert "<strong>fechada</strong>" in r.text
        assert "O ADMIN" in r.text
        r = await adm.post("/telas/admin/portas/entrada", {"nivel": "aberta", "motivo": "Fim do teste."}, csrf=csrf)
        assert r.status_code == 303
        assert (await amb.ler_fatura(sujeito)).is_error is False

        r = await adm.post("/telas/admin/portas/saida", {"acao": "fechar", "motivo": "Teste da saida."}, csrf=csrf)
        assert r.status_code == 303
        async with amb.admin.connect() as c:
            assert (await c.execute(text("SELECT aberta FROM estado_saida"))).scalar_one() is False
        with pytest.raises(MCPError, match="porta de saida"):
            await amb.ler_fatura(sujeito)
        r = await adm.post("/telas/admin/portas/saida", {"acao": "abrir", "motivo": "Fim do teste."}, csrf=csrf)
        assert r.status_code == 303
        assert (await amb.ler_fatura(sujeito)).is_error is False
    finally:
        async with amb.admin.begin() as c:
            await c.execute(text("UPDATE estado_entrada SET nivel = 'aberta', motivo = NULL"))
            await c.execute(text("UPDATE estado_saida SET aberta = true, motivo = NULL"))
    frases = [linha.frase for linha in await amb.extrato("admin_entrada_mudou", admin_id)]
    assert frases == [
        "Entrada do MCP-CLARO deixada fechada por Carla Admin.",
        "Entrada do MCP-CLARO deixada aberta por Carla Admin.",
    ]
    frases = [linha.frase for linha in await amb.extrato("admin_saida_mudou", admin_id)]
    assert frases == ["Porta de saida fechada por Carla Admin.", "Porta de saida aberta por Carla Admin."]
    assert await amb.extrato("estado_entrada_mudou")
    assert await amb.extrato("estado_saida_mudou")


async def test_csrf_em_todos_os_post_novos(amb: Ambiente) -> None:
    await amb.configurar_direto()
    adm, _, admin_id = await amb.nova_pessoa("Carla Admin", admin=True)
    _, _, bia_id = await amb.nova_pessoa("Bia")
    csrf = await adm.csrf()
    sistema = f"/telas/admin/sistemas/{amb.sistema_id}"
    async with amb.admin.begin() as c:
        alvo_id = (
            await c.execute(
                text(
                    "INSERT INTO bloqueios (tipo, alcance, pessoa_id, quem, motivo)"
                    " VALUES ('pausa', 'pessoa', :p, :q, 'teste') RETURNING id"
                ),
                {"p": admin_id, "q": f"pessoa:{admin_id}"},
            )
        ).scalar_one()
    pedidos: list[tuple[str, dict[str, str]]] = [
        ("/telas/bloqueio", {"aplicativo": "todos"}),
        ("/telas/pausa", {"aplicativo": "todos"}),
        (f"/telas/pausa/{alvo_id}/retomar", {}),
        (f"/telas/autorizar/{amb.sistema_id}/retirar", {}),
        ("/telas/admin/bloqueios", {"tipo": "bloqueio", "alcance": "tudo", "motivo": "Teste de CSRF."}),
        (f"/telas/admin/bloqueios/{alvo_id}/encerrar", {}),
        ("/telas/admin/portas/entrada", {"nivel": "fechada", "motivo": "Teste de CSRF."}),
        ("/telas/admin/portas/saida", {"acao": "fechar", "motivo": "Teste de CSRF."}),
        (f"/telas/admin/pessoas/{bia_id}/desativar", {"confirmo": "sim"}),
        (
            f"{sistema}/autorizacao",
            {
                "authorization_endpoint": "https://a.exemplo/a",
                "token_endpoint": "https://a.exemplo/t",
                "client_id": "x",
            },
        ),
        (f"{sistema}/segredo", {"segredo": "segredo-de-csrf"}),
        (f"{sistema}/chave", {"tipo": "declaracoes", "segredo": "chave-de-csrf"}),
    ]
    try:
        for caminho, dados in pedidos:
            for caso, kwargs in (
                ("sem token", {}),
                ("token errado", {"csrf": "x" * len(csrf)}),
                ("outra origem", {"csrf": csrf, "headers": {"origin": "https://ataque.exemplo"}}),
                ("site cruzado", {"csrf": csrf, "headers": {"sec-fetch-site": "cross-site"}}),
            ):
                r = await adm.post(caminho, dados, **kwargs)
                assert r.status_code == 403, (caminho, caso)
                assert "nada foi feito" in r.text, (caminho, caso)
        async with amb.admin.connect() as c:
            novos = (
                await c.execute(
                    text("SELECT count(*) FROM bloqueios WHERE quem LIKE :q OR pessoa_id = :p"),
                    {"q": f"admin:{admin_id}", "p": admin_id},
                )
            ).scalar_one()
            pausa = (
                await c.execute(text("SELECT encerrado_em FROM bloqueios WHERE id = :i"), {"i": alvo_id})
            ).scalar_one()
            entrada = (await c.execute(text("SELECT nivel FROM estado_entrada"))).scalar_one()
            saida = (await c.execute(text("SELECT aberta FROM estado_saida"))).scalar_one()
            bia = (await c.execute(text("SELECT situacao FROM pessoas WHERE id = :p"), {"p": bia_id})).scalar_one()
            config = (
                await c.execute(
                    text("SELECT autorizacao->>'client_id' FROM sistemas WHERE id = :s"), {"s": amb.sistema_id}
                )
            ).scalar_one()
        assert novos == 1  # so a pausa do arranjo
        assert pausa is None
        assert (entrada, saida, bia) == ("aberta", True, "ativa")
        assert config == amb.oauth.client_id
        assert not await amb.extrato("admin_segredo_cliente_entregue", admin_id)
        assert not await amb.extrato("admin_chave_entregue", admin_id)
    finally:
        async with amb.admin.begin() as c:
            await c.execute(
                text("UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'admin:teste' WHERE id = :i"),
                {"i": alvo_id},
            )
            await c.execute(text("UPDATE estado_entrada SET nivel = 'aberta'"))
            await c.execute(text("UPDATE estado_saida SET aberta = true"))
    # O link de autorizar (GET) tambem pede a ficha da sessao.
    r = await adm.get(f"/telas/autorizar/{amb.sistema_id}/ir?ficha=errada")
    assert r.status_code == 403
