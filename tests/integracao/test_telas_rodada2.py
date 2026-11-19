"""Segunda rodada de correcoes da Fase 2 nas telas, de ponta a ponta (PostgreSQL de verdade, entrada,
despachante e o sistema de exemplo ``acoes_caixa``).

- O ADMIN marca, na revisao da ferramenta, os campos que dizem o destinatario; o limite "so estes
  destinatarios" confere so esses campos, e o resto do pedido fica livre.
- Com a passkey ligada, um limite criado sem ela aparece como "nao vale" e nao e ampliado.
- A cada passkey cadastrada ou desativada, a pessoa recebe um aviso; falha no aviso nao desfaz o cadastro.
- O inicio leva quem cuida de um programa a "Meus programas".

Arranjo de ``test_telas_acoes`` (login das telas pelo provedor OIDC falso, passkey do autenticador de software).
Aqui o sistema de exemplo declara tambem ``enviar_email`` (para, copia e texto).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import text

from exemplos import acoes_caixa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.protocolo import FUSO
from mcp_claro.telas import admin as admin_telas
from mcp_claro.telas import consultas, passkeys
from mcp_claro.versao import PREFIXO_META
from tests.autenticador_falso import AutenticadorFalso
from tests.integracao import test_telas_acoes
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import ACOES, chamadas, esperar_estado, operacao, politica
from tests.integracao.test_telas import Navegador, token_csrf
from tests.integracao.test_telas_acoes import (
    assinar,
    cadastrar_passkey,
    desafio,
    entrar,
    extrato,
    pedir,
    protocolo_de,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

EMAIL: dict[str, Any] = {
    "name": "enviar_email",
    "title": "Enviar e-mail",
    "description": "Envia um e-mail da caixa de teste, com copia.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "para": {"type": "string", "maxLength": 120},
            "copia": {"type": "array", "items": {"type": "string", "maxLength": 120}},
            "texto": {"type": "string", "maxLength": 500},
        },
        "required": ["para", "texto"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
}


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    # O sistema de exemplo declara tambem enviar_email, e ela entra na vitrine e na ordem de servico.
    monkeypatch.setattr(acoes_caixa, "FERRAMENTAS", [*acoes_caixa.FERRAMENTAS, EMAIL])
    monkeypatch.setattr(test_telas_acoes, "ACOES", (*ACOES, ("enviar_email", "email", "muda", "medio")))
    async with test_telas_acoes.ambiente(banco_migrado, monkeypatch) as a:
        yield a


@pytest.fixture
async def http() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(follow_redirects=False, timeout=15, trust_env=False) as cliente:
        yield cliente


async def escolha_de(nav: Navegador, nome_vitrine: str) -> tuple[str, str]:
    """A escolha (aplicativo:ferramenta) de "Criar limite" para a acao, e a pagina de criar."""
    r = await nav.get("/telas/limites")
    assert r.status_code == 200
    for e in re.findall(r"/telas/limites/novo\?escolha=([0-9a-f-]+:[0-9a-f-]+)", r.text):
        pagina = await nav.get(f"/telas/limites/novo?escolha={e}")
        if f"<h2>{nome_vitrine}</h2>" in pagina.text:
            return e, pagina.text
    raise AssertionError(f"{nome_vitrine} nao esta entre as acoes que podem entrar num limite")


def formulario_de_revisao(amb: Ambiente, original: str, destinatario: list[str]) -> list[tuple[str, str]]:
    return [
        ("nome", original),
        ("nome_vitrine", amb.vitrine[original]),
        ("descricao", "Acao de teste, revisada de novo."),
        ("classe", "muda"),
        ("risco", "medio"),
        *(("destinatario", campo) for campo in destinatario),
    ]


async def revisar(amb: Ambiente, admin: Navegador, original: str, destinatario: list[str]) -> httpx2.Response:
    """O ADMIN grava uma revisao nova da ferramenta, com os campos de destinatario marcados."""
    ficha = f"/telas/admin/sistemas/{amb.sistema_id}/ferramenta"
    pagina = await admin.get(f"{ficha}?nome={original}")
    assert pagina.status_code == 200, pagina.text
    return await admin.post(ficha, formulario_de_revisao(amb, original, destinatario), csrf=token_csrf(pagina.text))


async def revisao_ativa(amb: Ambiente, original: str) -> Any:
    async with amb.admin.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT id, versao, campos_destinatario FROM ferramentas_revisadas"
                    " WHERE sistema_id = :s AND nome_original = :n AND situacao = 'ativa'"
                ),
                {"s": amb.sistema_id, "n": original},
            )
        ).one()


async def criar_so_estes(
    nav: Navegador, aut: AutenticadorFalso, escolha: str, pagina: str, campo: str, valores: str
) -> str:
    """A pessoa cria um limite "so estes destinatarios" com a passkey. Devolve o id do limite."""
    csrf = token_csrf(pagina)
    form = {
        "escolha": escolha,
        "destinatarios": "estes",
        "campo": campo,
        "valores": valores,
        "por_dia": "5",
        "dias": "1",
    }
    credencial = await assinar(nav, aut, "/telas/limites/desafio", csrf, form)
    r = await nav.post("/telas/limites", {**form, "credencial": credencial}, csrf=csrf)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/")[3].split("?")[0]


async def destinatarios_do_limite(amb: Ambiente, limite_id: str) -> Any:
    async with amb.admin.connect() as c:
        return (await c.execute(text("SELECT destinatarios FROM limites WHERE id = :i"), {"i": limite_id})).scalar_one()


def estado_de(meta: dict[str, Any]) -> str:
    return str(meta[PREFIXO_META + "estado"])


# --- T1: o ADMIN marca os campos de destinatario; o limite confere so eles ---


async def test_admin_marca_o_destinatario_e_o_limite_so_estes_confere_so_ele(
    amb: Ambiente, http: httpx2.AsyncClient
) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    mensagem, email = amb.vitrine["enviar_mensagem"], amb.vitrine["enviar_email"]
    ana = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, ana)

    # Antes da marca do ADMIN: so "qualquer destinatario" e oferecido, e a tela diz por que.
    escolha, pagina = await escolha_de(ana, mensagem)
    assert "O ADMIN ainda não marcou qual campo desta ação diz o destinatário." in pagina
    assert 'value="estes"' not in pagina
    csrf = token_csrf(pagina)
    form = {
        "escolha": escolha,
        "destinatarios": "estes",
        "campo": "destinatario",
        "valores": "equipe",
        "por_dia": "1",
        "dias": "1",
    }
    r = await desafio(ana, "/telas/limites/desafio", csrf, form)
    assert r.status_code == 400
    assert "O ADMIN ainda não marcou" in r.json()["erro"]

    # O ADMIN ve os campos do pedido, a explicacao, e marca so o destinatario.
    admin = await entrar(amb, http, "pessoa-admin", "Admin")
    r = await admin.get(f"/telas/admin/sistemas/{amb.sistema_id}/ferramenta?nome=enviar_mensagem")
    assert "Campos do pedido que dizem o destinatário" in r.text
    assert "O MCP-CLARO confere só os campos marcados aqui" in r.text
    assert '<input type="checkbox" name="destinatario" value="destinatario" >' in r.text
    assert '<input type="checkbox" name="destinatario" value="texto" >' in r.text
    antes = await revisao_ativa(amb, "enviar_mensagem")
    # Campo fora do esquema de entrada: recusado, nada gravado.
    r = await revisar(amb, admin, "enviar_mensagem", ["destinatario", "bcc"])
    assert r.status_code == 400
    assert "Marque como destinatário só campos que a ferramenta pede" in r.text
    assert (await revisao_ativa(amb, "enviar_mensagem")).id == antes.id
    r = await revisar(amb, admin, "enviar_mensagem", ["destinatario"])
    assert r.status_code == 303, r.text
    nova = await revisao_ativa(amb, "enviar_mensagem")
    assert (nova.versao, nova.campos_destinatario) == (antes.versao + 1, ["destinatario"])
    r = await admin.get(f"/telas/admin/sistemas/{amb.sistema_id}/ferramenta?nome=enviar_mensagem")
    assert '<input type="checkbox" name="destinatario" value="destinatario" checked>' in r.text
    async with amb.admin.connect() as c:
        revisada = (
            await c.execute(
                text(
                    "SELECT dados FROM extrato WHERE tipo = 'admin_ferramenta_revisada' AND pessoa_id = :p"
                    " ORDER BY id DESC LIMIT 1"
                ),
                {"p": amb.admin_id},
            )
        ).scalar_one()
    assert revisada["campos_destinatario"] == ["destinatario"]
    assert revisada["ferramenta_id"] == str(nova.id)

    # A pessoa cria "so estes" com a passkey; a tela diz o que e conferido e que o resto fica livre.
    escolha, pagina = await escolha_de(ana, mensagem)
    assert "<strong>destinatario</strong>. O limite confere só ele. O resto do pedido, como o texto, fica livre." in (
        pagina
    )
    assert '<option value="texto"' not in pagina
    limite_id = await criar_so_estes(ana, aut, escolha, pagina, "destinatario", "equipe")

    # O destinatario certo, com texto qualquer, sai sem ficha. Outro destinatario vai para a ficha.
    certo = await pedir(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": "Reunião às 10h; externo@fora.com"})
    assert estado_de(certo) in ("liberada", "enviando", "enviada")
    assert (await operacao(amb, protocolo_de(certo))).ficha_id is None
    await esperar_estado(amb, protocolo_de(certo), Estado.ENVIADA)
    outro = await pedir(amb, "enviar_mensagem", {"destinatario": "estranho", "texto": "oi"})
    assert estado_de(outro) == "aguardando_ficha"
    assert (await operacao(amb, protocolo_de(outro))).ficha_id is not None
    assert chamadas(amb, "enviar_mensagem") == 1
    # So o campo marcado entra no limite: o texto fica livre.
    assert await destinatarios_do_limite(amb, limite_id) == [{"campo": "destinatario", "valores": ["equipe"]}]

    # enviar_email com para e copia marcados: copia preenchida vai para a ficha; vazia ou ausente, nao.
    r = await revisar(amb, admin, "enviar_email", ["para", "copia"])
    assert r.status_code == 303, r.text
    assert (await revisao_ativa(amb, "enviar_email")).campos_destinatario == ["para", "copia"]
    escolha, pagina = await escolha_de(ana, email)
    assert "<strong>para, copia</strong>, menos o que você escolher acima) precisam vir vazios" in pagina
    limite_id = await criar_so_estes(ana, aut, escolha, pagina, "para", "chefe@org")
    com_copia = await pedir(amb, "enviar_email", {"para": "chefe@org", "copia": ["externo@fora.com"], "texto": "x"})
    assert estado_de(com_copia) == "aguardando_ficha"
    assert (await operacao(amb, protocolo_de(com_copia))).ficha_id is not None
    sem_copia = await pedir(amb, "enviar_email", {"para": "chefe@org", "copia": [], "texto": "Relatório do mês"})
    assert (await operacao(amb, protocolo_de(sem_copia))).ficha_id is None
    sem_campo = await pedir(amb, "enviar_email", {"para": "chefe@org", "texto": "Outro texto qualquer"})
    assert (await operacao(amb, protocolo_de(sem_campo))).ficha_id is None
    for p in (sem_copia, sem_campo):
        await esperar_estado(amb, protocolo_de(p), Estado.ENVIADA)
    assert chamadas(amb, "enviar_email") == 2
    # A copia, marcada pelo ADMIN e nao escolhida, entra vazia: tem de vir vazia ou faltar.
    assert await destinatarios_do_limite(amb, limite_id) == [
        {"campo": "para", "valores": ["chefe@org"]},
        {"campo": "copia", "valores": []},
    ]


async def test_campo_novo_marcado_encerra_o_limite_que_nao_o_confere(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=600)
    admin = await entrar(amb, http, "pessoa-admin", "Admin")
    assert (await revisar(amb, admin, "enviar_email", ["para"])).status_code == 303
    ana = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, ana)
    escolha, pagina = await escolha_de(ana, amb.vitrine["enviar_email"])
    limite_id = await criar_so_estes(ana, aut, escolha, pagina, "para", "chefe@org")
    # So "para" estava marcado: a copia ainda nao e conferida, e este pedido espera a hora de sair, sem ficha.
    antes = await pedir(amb, "enviar_email", {"para": "chefe@org", "copia": ["externo@fora.com"], "texto": "x"})
    assert estado_de(antes) == "liberada"

    # O ADMIN marca tambem a copia: o limite que nao a confere termina, e o que ele liberou e cancelado.
    r = await revisar(amb, admin, "enviar_email", ["para", "copia"])
    assert r.status_code == 303, r.text
    async with amb.admin.connect() as c:
        limite = (await c.execute(text("SELECT situacao, motivo FROM limites WHERE id = :i"), {"i": limite_id})).one()
    assert (limite.situacao, limite.motivo) == ("encerrado", admin_telas.MOTIVO_CAMPOS_MUDARAM)
    parada = await operacao(amb, protocolo_de(antes))
    assert (parada.estado, parada.desfecho, parada.mudou_por) == (
        Estado.CANCELADA,
        admin_telas.MOTIVO_CANCELADA_CAMPOS,
        "admin",
    )
    encerrado = await extrato(amb, "limite_encerrado", amb.ana_id)
    assert [(x.dados["limite_id"], x.dados["canceladas"], x.dados["por"]) for x in encerrado] == [
        (limite_id, 1, "admin")
    ]
    # Agora a copia para fora vai para a ficha; nada chegou ao sistema.
    depois = await pedir(amb, "enviar_email", {"para": "chefe@org", "copia": ["externo@fora.com"], "texto": "x"})
    assert estado_de(depois) == "aguardando_ficha"
    assert chamadas(amb, "enviar_email") == 0
    # Os motivos aparecem para a pessoa, escritos com acento, em Meus limites, no limite e na acao cancelada.
    motivo = "o ADMIN marcou outro campo de destinatário nesta ação; crie outro limite se precisar"
    r = await ana.get("/telas/limites")
    assert f"({motivo})." in r.text
    r = await ana.get(f"/telas/limites/{limite_id}")
    assert f"<dd>Encerrado ({motivo})</dd>" in r.text
    r = await ana.get(f"/telas/acoes/{parada.id}")
    assert r.status_code == 200
    assert "Detalhe: cancelada porque o limite que a liberou terminou: o ADMIN mudou os campos de destinatário." in (
        r.text
    )


async def terminou_ou_espera_trava(amb: Ambiente, tarefa: asyncio.Task[httpx2.Response]) -> str:
    """Espera a revisao do ADMIN terminar ("terminou") ou ficar parada numa trava do banco ("esperando")."""
    fim = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < fim:
        if tarefa.done():
            return "terminou"
        async with amb.admin.connect() as c:
            parada = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND wait_event_type = 'Lock' AND query LIKE 'UPDATE ferramentas_revisadas%'"
                    )
                )
            ).scalar_one()
        if parada:
            return "esperando"
        await asyncio.sleep(0.05)
    raise AssertionError("a revisao do ADMIN nem terminou nem parou numa trava")


async def test_criar_limite_enquanto_o_admin_marca_a_copia_nao_se_cruzam(
    amb: Ambiente, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pessoa cria "so estes" com so 'para' marcado. No meio da transacao dela (campos ja lidos, limite ainda
    nao gravado), o ADMIN grava a revisao que marca tambem a copia. A revisao espera o COMMIT do limite e o
    encerra: o limite nunca fica valendo sem conferir a copia."""
    await politica(amb, OUTBOX_HOLD_SECONDS=600)
    admin = await entrar(amb, http, "pessoa-admin", "Admin")
    assert (await revisar(amb, admin, "enviar_email", ["para"])).status_code == 303
    ana = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, ana)
    escolha, pagina = await escolha_de(ana, amb.vitrine["enviar_email"])
    csrf = token_csrf(pagina)
    form = {
        "escolha": escolha,
        "destinatarios": "estes",
        "campo": "para",
        "valores": "chefe@org",
        "por_dia": "5",
        "dias": "1",
    }
    credencial = await assinar(ana, aut, "/telas/limites/desafio", csrf, form)
    ficha = f"/telas/admin/sistemas/{amb.sistema_id}/ferramenta"
    pagina_admin = (await admin.get(f"{ficha}?nome=enviar_email")).text
    revisao = formulario_de_revisao(amb, "enviar_email", ["para", "copia"])

    original = consultas.limite_igual_ativo
    tarefas: list[asyncio.Task[httpx2.Response]] = []
    como: list[str] = []

    async def no_meio(c: Any, **kw: Any) -> bool:
        # Dentro da transacao de criar, depois de ler os campos marcados e antes de gravar o limite.
        tarefas.append(asyncio.create_task(admin.post(ficha, revisao, csrf=token_csrf(pagina_admin))))
        como.append(await terminou_ou_espera_trava(amb, tarefas[0]))
        return await original(c, **kw)

    monkeypatch.setattr(consultas, "limite_igual_ativo", no_meio)
    r = await ana.post("/telas/limites", {**form, "credencial": credencial}, csrf=csrf)
    monkeypatch.setattr(consultas, "limite_igual_ativo", original)
    assert r.status_code == 303, r.text
    limite_id = r.headers["location"].split("/")[3].split("?")[0]
    assert (await tarefas[0]).status_code == 303
    # A revisao do ADMIN esperou o limite ser gravado, viu que ele nao confere a copia e o encerrou.
    assert (await revisao_ativa(amb, "enviar_email")).campos_destinatario == ["para", "copia"]
    async with amb.admin.connect() as c:
        limite = (await c.execute(text("SELECT situacao, motivo FROM limites WHERE id = :i"), {"i": limite_id})).one()
    assert (limite.situacao, limite.motivo) == ("encerrado", admin_telas.MOTIVO_CAMPOS_MUDARAM)
    assert [x.dados["limite_id"] for x in await extrato(amb, "limite_encerrado", amb.ana_id)] == [limite_id]
    com_copia = await pedir(amb, "enviar_email", {"para": "chefe@org", "copia": ["externo@fora.com"], "texto": "x"})
    assert estado_de(com_copia) == "aguardando_ficha"
    assert chamadas(amb, "enviar_email") == 0
    # E esperou de fato: parada na trava da versao revisada ate o COMMIT do limite.
    assert como == ["esperando"]


# --- T2: com a passkey ligada, um limite criado sem ela nao vale e nao e ampliado ---


async def test_limite_sem_passkey_nao_vale_com_a_passkey_ligada_e_nao_amplia(
    amb: Ambiente, http: httpx2.AsyncClient
) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    ana = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, ana)
    # Um limite criado quando a passkey estava desligada (com_passkey = false), "qualquer destinatario".
    async with amb.admin.begin() as c:
        sem_passkey = await consultas.criar_limite(
            c,
            pessoa_id=amb.ana_id,
            aplicativo_id=amb.aplicativo_id,
            sistema_id=amb.sistema_id,
            nome_original="enviar_mensagem",
            destinatarios="[]",
            por_dia=2,
            dias=3,
            com_passkey=False,
            fuso=FUSO,
        )
    r = await ana.get("/telas/limites")
    assert "<strong>Não vale com a passkey ligada.</strong>" in r.text
    assert f'href="/telas/limites/{sem_passkey}" hx-boost="false">Ver</a>' in r.text
    pagina = f"/telas/limites/{sem_passkey}"
    r = await ana.get(pagina)
    assert "<dt>Situação</dt><dd>Não vale com a passkey ligada</dd>" in r.text
    assert "encerre este e crie outro, com a passkey" in r.text
    assert f'action="{pagina}/ampliar"' not in r.text
    csrf = token_csrf(r.text)

    # O nucleo nao gasta o limite: o pedido vai para a ficha.
    pedido = await pedir(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": "oi"})
    assert estado_de(pedido) == "aguardando_ficha"

    # Ampliar e recusado com a frase que diz o que fazer: nem desafio de passkey, nem POST.
    ampliar = {"por_dia": "3", "dias": "5"}
    r = await desafio(ana, f"{pagina}/desafio", csrf, ampliar)
    assert r.status_code == 409
    assert "Encerre este limite e crie outro, com a passkey." in r.json()["erro"]
    r = await ana.post(f"{pagina}/ampliar", {**ampliar, "credencial": "{}"}, csrf=csrf)
    assert r.status_code == 409
    assert "não vale com a passkey ligada" in r.text
    # Nem pelo banco: a ampliacao com passkey de um limite sem passkey nao acontece.
    async with amb.entrada_db.begin() as c:
        assert (
            await consultas.ampliar_limite(
                c,
                pessoa_id=amb.ana_id,
                limite_id=sem_passkey,
                por_dia=3,
                dias=5,
                teto_dia=5,
                teto_dias=30,
                com_passkey=True,
            )
        ) is None
    async with amb.admin.connect() as c:
        linha = (
            await c.execute(text("SELECT por_dia, com_passkey FROM limites WHERE id = :i"), {"i": sem_passkey})
        ).one()
    assert (linha.por_dia, linha.com_passkey) == (2, False)
    assert await extrato(amb, "limite_ampliado", amb.ana_id) == []

    # Encerrar segue valendo; um limite novo, com a passkey, vale e amplia com o extrato certo.
    r = await ana.post(f"{pagina}/encerrar", csrf=csrf)
    assert r.status_code == 303
    escolha, pagina_novo = await escolha_de(ana, amb.vitrine["enviar_mensagem"])
    csrf = token_csrf(pagina_novo)
    form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "2", "dias": "1"}
    r = await ana.post(
        "/telas/limites",
        {**form, "credencial": await assinar(ana, aut, "/telas/limites/desafio", csrf, form)},
        csrf=csrf,
    )
    assert r.status_code == 303, r.text
    novo = r.headers["location"].split("/")[3].split("?")[0]
    r = await ana.get(f"/telas/limites/{novo}")
    assert "Não vale com a passkey ligada" not in r.text
    csrf = token_csrf(r.text)
    credencial = await assinar(ana, aut, f"/telas/limites/{novo}/desafio", csrf, ampliar)
    r = await ana.post(f"/telas/limites/{novo}/ampliar", {**ampliar, "credencial": credencial}, csrf=csrf)
    assert r.status_code == 303, r.text
    ampliados = await extrato(amb, "limite_ampliado", amb.ana_id)
    assert [x.frase for x in ampliados] == [
        f"A pessoa ampliou um limite de {amb.vitrine['enviar_mensagem']} para 3 por dia, com passkey."
    ]
    assert ampliados[0].dados["com_passkey"] is True


async def test_limite_igual_que_nao_vale_nao_impede_criar_outro_com_a_passkey(
    amb: Ambiente, http: httpx2.AsyncClient
) -> None:
    """Com a passkey ligada, um limite igual criado sem ela nao vale: nao manda "Amplie o que existe" (ampliar e
    recusado), e a pessoa cria o mesmo limite com a passkey. Um igual que vale segue recusado."""
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    ana = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, ana)
    async with amb.admin.begin() as c:
        sem_passkey = await consultas.criar_limite(
            c,
            pessoa_id=amb.ana_id,
            aplicativo_id=amb.aplicativo_id,
            sistema_id=amb.sistema_id,
            nome_original="enviar_mensagem",
            destinatarios="[]",
            por_dia=2,
            dias=3,
            com_passkey=False,
            fuso=FUSO,
        )
    escolha, pagina = await escolha_de(ana, amb.vitrine["enviar_mensagem"])
    csrf = token_csrf(pagina)
    form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "2", "dias": "1"}
    r = await ana.post(
        "/telas/limites",
        {**form, "credencial": await assinar(ana, aut, "/telas/limites/desafio", csrf, form)},
        csrf=csrf,
    )
    assert r.status_code == 303, r.text
    novo = r.headers["location"].split("/")[3].split("?")[0]
    async with amb.admin.connect() as c:
        linhas = {
            str(x.id): (x.situacao, x.com_passkey)
            for x in await c.execute(
                text("SELECT id, situacao, com_passkey FROM limites WHERE pessoa_id = :p"), {"p": amb.ana_id}
            )
        }
    assert linhas == {str(sem_passkey): ("ativo", False), novo: ("ativo", True)}
    # O novo vale: o pedido sai sem ficha, gastando dele. O antigo segue marcado "Não vale com a passkey ligada".
    pedido = await pedir(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": "oi"})
    op = await operacao(amb, protocolo_de(pedido))
    assert (op.ficha_id, str(op.limite_id)) == (None, novo)
    r = await ana.get("/telas/limites")
    assert r.text.count("<strong>Não vale com a passkey ligada.</strong>") == 1

    # Outro igual, agora que um vale: recusado, e a frase manda ampliar o que vale.
    r = await ana.get(f"/telas/limites/novo?escolha={escolha}")
    csrf = token_csrf(r.text)
    r = await ana.post(
        "/telas/limites",
        {**form, "credencial": await assinar(ana, aut, "/telas/limites/desafio", csrf, form)},
        csrf=csrf,
    )
    assert r.status_code == 409
    assert "Você já tem um limite igual valendo para esta ação. Amplie o que existe." in r.text


# --- T4: aviso a cada passkey cadastrada ou desativada ---


async def avisos_de(amb: Ambiente, pessoa_id: uuid.UUID) -> list[Any]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT canal, evento, assunto, corpo FROM avisos WHERE destino = :d ORDER BY id"),
                    {"d": f"pessoa:{pessoa_id}"},
                )
            ).all()
        )


async def test_passkey_cadastrada_ou_desativada_avisa_a_pessoa(
    amb: Ambiente, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bia = await entrar(amb, http, "pessoa-bia", "Bia")
    aut = await cadastrar_passkey(amb, bia)
    avisos = await avisos_de(amb, amb.bia_id)
    assert [(a.canal, a.evento, a.assunto) for a in avisos] == [
        ("email", "passkey_cadastrada", "MCP-CLARO: uma passkey foi cadastrada na sua conta")
    ]
    corpo = avisos[0].corpo
    assert list(corpo) == ["texto"]
    assert re.fullmatch(
        r"Uma passkey foi cadastrada na sua conta em \d\d:\d\d de \d\d/\d\d/\d{4} \(hora de Brasília\)\."
        r" Se não foi você, fale com o ADMIN\.",
        corpo["texto"],
    )
    async with amb.admin.connect() as c:
        passkey_id = (
            await c.execute(text("SELECT id FROM passkeys WHERE pessoa_id = :p"), {"p": amb.bia_id})
        ).scalar_one()
    bruto = json.dumps(corpo)
    for sensivel in (str(passkey_id), "Celular"):
        assert sensivel not in bruto
    assert aut.credencial_id.hex() not in bruto

    r = await bia.get("/telas/passkeys")
    achado = re.search(r'action="(/telas/passkeys/[0-9a-f-]+/desativar)"', r.text)
    assert achado
    r = await bia.post(achado.group(1), csrf=token_csrf(r.text))
    assert r.status_code == 303
    avisos = await avisos_de(amb, amb.bia_id)
    assert [a.evento for a in avisos] == ["passkey_cadastrada", "passkey_desativada"]
    assert avisos[1].corpo["texto"].startswith("Uma passkey foi desativada na sua conta em ")

    # O aviso nao entra (a fila falhou): a passkey fica cadastrada mesmo assim, e o log nao leva conteudo.
    async def falha(*_: Any, **__: Any) -> bool:
        raise OSError("fila de avisos fora do ar")

    monkeypatch.setattr(passkeys, "avisar_pessoa", falha)
    ana = await entrar(amb, http)
    await cadastrar_passkey(amb, ana)
    async with amb.admin.connect() as c:
        ativas = (
            await c.execute(text("SELECT count(*) FROM passkeys WHERE pessoa_id = :p AND ativa"), {"p": amb.ana_id})
        ).scalar_one()
    assert ativas == 1
    assert await extrato(amb, "passkey_cadastrada", amb.ana_id)
    assert await avisos_de(amb, amb.ana_id) == []


# --- T6: quem cuida de um programa chega a "Meus programas" pelo inicio ---


async def test_inicio_leva_o_responsavel_aos_seus_programas(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    nome = f"Robo {uuid.uuid4().hex[:6]}"
    async with amb.admin.begin() as c:
        programa = await repositorio.habilitar_aplicativo(
            c, client_id=f"robo-{uuid.uuid4().hex[:8]}", nome=nome, familia="programa", tipo="programa"
        )
        await c.execute(
            text(
                "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim)"
                " VALUES (:a, :r, :s, 'Le os chamados abertos toda manha.', :f)"
            ),
            {"a": programa, "r": amb.ana_id, "s": amb.bia_id, "f": date.today() + timedelta(days=90)},
        )
    try:
        for sujeito, nome_pessoa in (("pessoa-ana", "Ana"), ("pessoa-bia", "Bia")):
            nav = await entrar(amb, http, sujeito, nome_pessoa)
            r = await nav.get("/telas")
            assert '<a href="/telas/programas">Meus programas</a>' in r.text, sujeito
            r = await nav.get("/telas/programas")
            assert r.status_code == 200
            assert nome in r.text
        admin = await entrar(amb, http, "pessoa-admin", "Admin")
        assert "/telas/programas" not in (await admin.get("/telas")).text
        # Encerrado, o programa nao leva mais ninguem ate la pelo inicio.
        async with amb.admin.begin() as c:
            await c.execute(
                text("UPDATE programas SET situacao = 'encerrado' WHERE aplicativo_id = :a"), {"a": programa}
            )
        nav = await entrar(amb, http)
        assert "/telas/programas" not in (await nav.get("/telas")).text
    finally:
        async with amb.admin.begin() as c:
            await c.execute(text("DELETE FROM programas WHERE aplicativo_id = :a"), {"a": programa})
