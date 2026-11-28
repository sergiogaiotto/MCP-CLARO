"""Telas da pessoa na Fase 2, de ponta a ponta: caixa de pendencias, ficha, espera visivel, cancelar, seguir
agora, "estamos confirmando", passkey e limites por aproximacao.

Arranjo de ``test_saida`` (entrada inteira, despachante unico, sistema de exemplo ``acoes_caixa`` que conta
cada chamada; cada papel conecta com o proprio usuario do banco). O login das telas e o de
``test_telas_bloqueio``: pelo provedor OIDC falso, que tambem emite os crachas do aplicativo (aqui ele
substitui o provedor so de crachas do arranjo). A passkey e a do autenticador de software de teste.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from sqlalchemy import text

from exemplos import acoes_caixa
from mcp_claro.config.settings import Parametros
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.versao import PREFIXO_META
from tests.apoio import cliente_mcp
from tests.autenticador_falso import AutenticadorFalso
from tests.idp_oidc_falso import IdpOidcFalso
from tests.integracao import test_autorizacao
from tests.integracao.test_autorizacao import Ambiente, subir
from tests.integracao.test_saida import ACOES, chamadas, esperar_estado, operacao, politica
from tests.integracao.test_telas import Navegador, token_csrf

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PUBLICO = "https://mcp-claro.teste"


@contextlib.asynccontextmanager
async def ambiente(banco: Parametros, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> AsyncIterator[Ambiente]:
    # O mesmo arranjo de test_saida, com o provedor OIDC falso (login das telas e crachas do aplicativo).
    monkeypatch.setattr(test_autorizacao, "IdpFalso", functools.partial(IdpOidcFalso, segredo=None))
    async with subir(
        banco,
        monkeypatch,
        criar_sistema=acoes_caixa.criar,
        revisoes=ACOES,
        extra={"OUTBOX_SWEEP_SECONDS": 1, **extra},
    ) as a:
        assert (await a.autorizar(a.ana_id, "ana")).status_code == 200
        assert (await a.autorizar(a.bia_id, "bia")).status_code == 200
        async with a.admin.begin() as c:
            # Fora do periodo de experiencia, para os limites valerem.
            await c.execute(
                text("UPDATE ordens_servico SET criado_em = now() - interval '60 days' WHERE aplicativo_id = :a"),
                {"a": a.aplicativo_id},
            )
        a.zerar_vistas()
        await politica(a, CONFIRM_CAP_PER_HOUR=1000)
        try:
            yield a
        finally:
            async with a.admin.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = 'teste-saida'"))
                await c.execute(
                    text(
                        "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste'"
                        " WHERE encerrado_em IS NULL AND (alcance IN ('tudo', 'sistema') OR aplicativo_id = :a)"
                    ),
                    {"a": a.aplicativo_id},
                )


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with ambiente(banco_migrado, monkeypatch) as a:
        yield a


@pytest.fixture
async def http() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(follow_redirects=False, timeout=15, trust_env=False) as cliente:
        yield cliente


async def entrar(amb: Ambiente, http: httpx2.AsyncClient, sujeito: str = "pessoa-ana", nome: str = "Ana") -> Navegador:
    """Login pelas telas, como em test_telas_bloqueio. ``pessoa-ana`` e a mesma pessoa do arranjo."""
    nav = Navegador(amb.entrada, http)
    amb.idp.proxima_pessoa = (sujeito, nome)  # type: ignore[attr-defined]
    r = await nav.get("/telas/login?voltar=/telas")
    assert r.status_code == 303, r.text
    r = await http.get(r.headers["location"])  # o provedor devolve direto o codigo
    assert r.status_code == 302, r.text
    volta = urlsplit(r.headers["location"])
    r = await nav.get(f"{volta.path}?{volta.query}")
    assert r.status_code == 303, r.text
    return nav


async def envelhecer_login(amb: Ambiente, pessoa_id: uuid.UUID, minutos: int = 10) -> None:
    """O login das sessoes abertas da pessoa passa a ser de ``minutos`` atras (a sessao segue valendo)."""
    async with amb.admin.begin() as c:
        await c.execute(
            text(
                "UPDATE sessoes_telas SET auth_time = now() - make_interval(mins => :m)"
                " WHERE pessoa_id = :p AND encerrada_em IS NULL"
            ),
            {"p": pessoa_id, "m": minutos},
        )


async def refazer_login(amb: Ambiente, nav: Navegador, voltar: str, sujeito: str = "pessoa-ana") -> httpx2.Response:
    """O botao "Entrar de novo agora": vai ao provedor com prompt=login e max_age=0 e volta para ``voltar``.

    Devolve a pagina de volta (GET), ja com a sessao nova."""
    amb.idp.proxima_pessoa = (sujeito, "Ana" if sujeito == "pessoa-ana" else "Bia")  # type: ignore[attr-defined]
    r = await nav.get("/telas/login/refazer?" + urlencode({"voltar": voltar}))
    assert r.status_code == 303, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["prompt"] == ["login"]
    assert q["max_age"] == ["0"]
    r = await nav.http.get(r.headers["location"])
    assert r.status_code == 302, r.text
    volta = urlsplit(r.headers["location"])
    r = await nav.get(f"{volta.path}?{volta.query}")
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith(voltar)
    assert r.headers["location"].endswith("refeito=1")
    return await nav.get(r.headers["location"])


async def pedir(
    amb: Ambiente, original: str, argumentos: dict[str, Any], *, diz: str | None = None, sujeito: str = "pessoa-ana"
) -> dict[str, Any]:
    """Uma acao pelo aplicativo. Devolve o ``_meta`` do MCP-CLARO (protocolo, estado...)."""
    meta = {PREFIXO_META + "aplicativo_diz": diz} if diz else None
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(sujeito)) as c:
        r = await c.call_tool(amb.vitrine[original], argumentos, meta=meta)
    return dict(r.meta or {})


def protocolo_de(meta: dict[str, Any]) -> str:
    return str(meta[PREFIXO_META + "protocolo"])


async def ficha_de(amb: Ambiente, protocolo: str) -> fila.Operacao:
    op = await operacao(amb, protocolo)
    assert op.ficha_id is not None
    return op


async def extrato(amb: Ambiente, tipo: str, pessoa_id: uuid.UUID) -> list[Any]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT frase, dados, protocolo FROM extrato WHERE tipo = :t AND pessoa_id = :p ORDER BY id"),
                    {"t": tipo, "p": pessoa_id},
                )
            ).all()
        )


async def desafio(nav: Navegador, caminho: str, csrf: str, dados: dict[str, str] | None = None) -> httpx2.Response:
    """O que o passkey.js faz no toque: pede um desafio novo, com o formulario e o token CSRF."""
    return await nav.post(caminho, {**(dados or {}), "credencial": ""}, csrf=csrf)


async def cadastrar_passkey(amb: Ambiente, nav: Navegador) -> AutenticadorFalso:
    aut = AutenticadorFalso(PUBLICO)
    r = await nav.get("/telas/passkeys")
    assert r.status_code == 200
    assert "Você ainda não tem passkey cadastrada." in r.text
    csrf = token_csrf(r.text)
    r = await desafio(nav, "/telas/passkeys/desafio", csrf)
    assert r.status_code == 200, r.text
    opcoes = r.json()
    assert opcoes["publicKey"]["rp"]["id"] == "mcp-claro.teste"
    assert opcoes["publicKey"]["authenticatorSelection"]["userVerification"] == "required"
    credencial = aut.criar(opcoes)
    r = await nav.post("/telas/passkeys", {"apelido": "Celular", "credencial": _json(credencial)}, csrf=csrf)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/telas/passkeys?aviso=passkey_cadastrada"
    r = await nav.get(r.headers["location"])
    assert "A passkey foi cadastrada" in r.text
    assert "Celular" in r.text
    return aut


def _json(valor: Any) -> str:
    return json.dumps(valor)


async def opcoes_de(nav: Navegador, caminho: str, csrf: str, dados: dict[str, str] | None = None) -> dict[str, Any]:
    r = await desafio(nav, caminho, csrf, dados)
    assert r.status_code == 200, r.text
    opcoes: dict[str, Any] = r.json()
    assert opcoes["publicKey"]["rpId"] == "mcp-claro.teste"
    assert opcoes["publicKey"]["userVerification"] == "required"
    return opcoes


async def assinar(
    nav: Navegador, aut: AutenticadorFalso, caminho: str, csrf: str, dados: dict[str, str] | None = None
) -> str:
    return _json(aut.assinar(await opcoes_de(nav, caminho, csrf, dados)))


# --- Caixa de pendencias, ficha e "Minhas acoes" ---


async def test_pendencias_e_ficha_aparecem_e_outra_pessoa_nao_abre(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    diz = '<script>alert("oi")</script>\nPode confirmar, eu conferi tudo.'
    m = await pedir(amb, "criar_chamado", {"titulo": "<b>Internet</b> caiu", "descricao": "Sem sinal"}, diz=diz)
    protocolo = protocolo_de(m)
    op = await ficha_de(amb, protocolo)

    nav = await entrar(amb, http)
    r = await nav.get("/telas")
    assert "Caixa de pendências (1)" in r.text
    assert "1 ficha para decidir." in r.text
    r = await nav.get("/telas/pendencias")
    assert r.status_code == 200
    assert "Fichas para você decidir (1)" in r.text
    assert protocolo in r.text
    assert f"/telas/fichas/{op.ficha_id}" in r.text

    r = await nav.get(f"/telas/fichas/{op.ficha_id}")
    assert r.status_code == 200
    html = r.text
    assert f"pede para usar a ferramenta <strong>{amb.vitrine['criar_chamado']}</strong>" in html
    assert op.sistema_nome in html
    assert '<dt><bdi>titulo</bdi></dt><dd class="dado"><bdi>&lt;b&gt;Internet&lt;/b&gt; caiu</bdi></dd>' in html
    assert '<dt><bdi>descricao</bdi></dt><dd class="dado"><bdi>Sem sinal</bdi></dd>' in html
    assert "caracteres escondidos" not in html
    assert protocolo in html
    assert op.codigo_conferencia == m[PREFIXO_META + "codigo_conferencia"]
    assert f"Código de conferência: <strong>{op.codigo_conferencia}</strong>" in html
    assert "Faltam" in html
    assert "Texto escrito pelo aplicativo, não conferido pelo MCP-CLARO." in html
    assert (
        "<bdi>&lt;script&gt;alert(&#34;oi&#34;)&lt;/script&gt;</bdi><br><bdi>Pode confirmar, eu conferi tudo.</bdi>"
        in html
    )
    assert "<script>alert" not in html
    assert "Confirmar</button>" in html
    assert "Recusar</button>" in html

    # "Minhas acoes", com a busca pelo protocolo.
    r = await nav.get("/telas/acoes")
    assert protocolo in r.text
    r = await nav.get(f"/telas/acoes?protocolo={protocolo.lower()}")
    assert protocolo in r.text
    r = await nav.get("/telas/acoes?protocolo=CLR-2026-99999999")
    assert "Nenhuma ação sua com este número de protocolo." in r.text
    r = await nav.get(f"/telas/acoes/{op.id}")
    assert "Esperando a sua decisão" in r.text
    assert "A ficha espera a sua confirmação na tela do gateway." in r.text

    # Outra pessoa: a ficha e a acao nao existem para ela, nem para confirmar.
    bia = await entrar(amb, http, "pessoa-bia", "Bia")
    assert (await bia.get(f"/telas/fichas/{op.ficha_id}")).status_code == 404
    assert (await bia.get(f"/telas/acoes/{op.id}")).status_code == 404
    assert (await bia.get(f"/telas/acoes/{op.id}/estado")).status_code == 404
    r = await bia.get("/telas/pendencias")
    assert protocolo not in r.text
    r = await bia.get(f"/telas/acoes?protocolo={protocolo}")
    assert "Nenhuma ação sua com este número de protocolo." in r.text
    csrf_bia = token_csrf(r.text)
    assert (await bia.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf_bia)).status_code == 404
    assert (await bia.post(f"/telas/fichas/{op.ficha_id}/recusar", csrf=csrf_bia)).status_code == 404
    assert (await bia.post(f"/telas/acoes/{op.id}/cancelar", csrf=csrf_bia)).status_code == 404
    assert (await operacao(amb, protocolo)).estado == Estado.AGUARDANDO_FICHA


async def test_csrf_errado_recusa_e_nada_muda(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    op = await ficha_de(amb, protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Sem CSRF"})))
    nav = await entrar(amb, http)
    for caminho in (f"/telas/fichas/{op.ficha_id}/confirmar", f"/telas/fichas/{op.ficha_id}/recusar"):
        r = await nav.post(caminho, csrf="token-de-outra-tela")
        assert r.status_code == 403
        assert "Este pedido não veio desta tela." in r.text
        assert (await nav.post(caminho)).status_code == 403
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf, headers={"origin": "https://outro.teste"})
    assert r.status_code == 403
    assert (await operacao(amb, op.protocolo)).estado == Estado.AGUARDANDO_FICHA
    assert await extrato(amb, "ficha_confirmada", amb.ana_id) == []


async def test_confirmar_libera_e_a_acao_sai_uma_vez(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    protocolo = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Confirmar pela tela"}))
    op = await ficha_de(amb, protocolo)
    nav = await entrar(amb, http)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/telas/acoes/{op.id}?aviso=confirmada"
    enviada = await esperar_estado(amb, protocolo, Estado.ENVIADA)
    assert chamadas(amb, "criar_chamado") == 1
    assert enviada.ficha_decisao == "confirmada"

    r = await nav.get(f"/telas/acoes/{op.id}")
    assert "Enviada uma vez ao sistema." in r.text
    assert "O que o sistema respondeu" in r.text
    assert "Feito: criar_chamado, registro CX-" in r.text
    assert "hx-trigger" not in r.text  # terminou: a parte do estado para de se atualizar

    # Confirmar de novo nao faz nada.
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)
    assert r.status_code == 409
    assert "Esta ficha já foi decidida antes. Nada mudou." in r.text
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 1
    linhas = await extrato(amb, "ficha_confirmada", amb.ana_id)
    assert [linha.frase for linha in linhas] == [
        f"Ficha do protocolo {protocolo} confirmada pela pessoa (sem passkey)."
    ]
    r = await nav.get("/telas/pendencias")
    assert "Nada espera por você agora." in r.text


async def test_recusar_nao_chega_ao_sistema(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    protocolo = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Vou recusar"}))
    op = await ficha_de(amb, protocolo)
    nav = await entrar(amb, http)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/recusar", csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "A ficha foi recusada. Nada foi para o sistema." in r.text
    assert "Cancelada antes de sair. Nada seguiu." in r.text
    atual = await operacao(amb, protocolo)
    assert (atual.estado, atual.ficha_decisao) == (Estado.CANCELADA, "recusada")
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 0
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)
    assert r.status_code == 409
    assert chamadas(amb, "criar_chamado") == 0
    assert await extrato(amb, "ficha_recusada", amb.ana_id)


async def test_espera_visivel_e_cancelar_antes_de_sair(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=60)
    protocolo = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Vou desistir na espera"}))
    op = await ficha_de(amb, protocolo)
    nav = await entrar(amb, http)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "A ficha foi confirmada." in r.text
    assert "Na espera para sair" in r.text
    assert re.search(r"Sai às <strong>\d\d:\d\d:\d\d de \d\d/\d\d/\d{4}</strong> \(horário de Brasília\)", r.text)
    assert re.search(r"Faltam <strong>(1 minuto|\d+ segundos)</strong> para sair", r.text)
    relogio = f"/telas/acoes/{op.id}/estado?visto=liberada"
    assert f'hx-get="{relogio}" hx-trigger="every 2s"' in r.text
    assert "Cancelar antes de sair" in r.text
    assert "Seguir agora" in r.text
    # O htmx troca so o relogio; os botoes ficam fora dele.
    r = await nav.get(relogio, headers={"hx-request": "true"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<div id="relogio"')
    assert "Faltam <strong>" in r.text
    assert "<html" not in r.text
    assert "Cancelar antes de sair" not in r.text
    r = await nav.get("/telas/pendencias")
    assert "Ações na espera para sair (1)" in r.text

    r = await nav.post(f"/telas/acoes/{op.id}/cancelar", csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "A ação foi cancelada antes de sair. Nada chegou ao sistema." in r.text
    assert (await operacao(amb, protocolo)).estado == Estado.CANCELADA
    # O estado mudou: o relogio pede ao htmx que recarregue a pagina inteira.
    r = await nav.get(relogio, headers={"hx-request": "true"})
    assert r.headers.get("hx-refresh") == "true"
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 0
    r = await nav.post(f"/telas/acoes/{op.id}/cancelar", csrf=csrf)
    assert r.status_code == 409
    assert "Não deu para cancelar" in r.text
    r = await nav.post(f"/telas/acoes/{op.id}/seguir", csrf=csrf)
    assert r.status_code == 409
    assert chamadas(amb, "criar_chamado") == 0


async def test_seguir_agora_sai_antes(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=600)
    protocolo = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Pode ir logo"}))
    op = await ficha_de(amb, protocolo)
    nav = await entrar(amb, http)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    assert (await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)).status_code == 303
    liberada = await operacao(amb, protocolo)
    assert liberada.estado == Estado.LIBERADA
    assert liberada.hora_saida is not None
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 0  # dez minutos de espera: nao saiu sozinha
    r = await nav.post(f"/telas/acoes/{op.id}/seguir", csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"].endswith("?aviso=seguir_agora")
    enviada = await esperar_estado(amb, protocolo, Estado.ENVIADA, prazo=10)
    assert enviada.enviada_em is not None
    assert enviada.enviada_em < liberada.hora_saida
    assert chamadas(amb, "criar_chamado") == 1
    assert await extrato(amb, "seguir_agora", amb.ana_id)


# --- Risco alto e passkey ---


async def test_risco_alto_sem_passkey_recusa_e_com_passkey_valida_confirma(
    amb: Ambiente, http: httpx2.AsyncClient
) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    protocolo = protocolo_de(await pedir(amb, "cancelar_servico", {"servico": "tv"}))
    op = await ficha_de(amb, protocolo)
    assert op.ficha_exige_passkey
    nav = await entrar(amb, http)
    r = await nav.get(f"/telas/fichas/{op.ficha_id}")
    assert "Confirmar com a passkey" in r.text
    assert "Você ainda não tem passkey." in r.text
    assert "/telas/estaticos/passkey.js" in r.text
    csrf = token_csrf(r.text)
    # Sem passkey cadastrada, nem o desafio sai.
    r = await desafio(nav, f"/telas/fichas/{op.ficha_id}/desafio", csrf)
    assert r.status_code == 409
    assert "Você ainda não tem passkey cadastrada." in r.json()["erro"]

    aut = await cadastrar_passkey(amb, nav)
    assert await extrato(amb, "passkey_cadastrada", amb.ana_id)

    # Sem a resposta da passkey: recusada, nada muda.
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)
    assert r.status_code == 403
    assert "Esta ação é de risco alto: confirme com a sua passkey." in r.text
    assert (await operacao(amb, protocolo)).estado == Estado.AGUARDANDO_FICHA

    # Passkey de outro aparelho (nao cadastrado): recusada.
    estranho = AutenticadorFalso(PUBLICO)
    r = await desafio(nav, f"/telas/fichas/{op.ficha_id}/desafio", csrf)
    r = await nav.post(
        f"/telas/fichas/{op.ficha_id}/confirmar", {"credencial": _json(estranho.assinar(r.json()))}, csrf=csrf
    )
    assert r.status_code == 403
    assert "não está cadastrada para você" in r.text
    assert (await operacao(amb, protocolo)).estado == Estado.AGUARDANDO_FICHA

    # Com a passkey valida: confirma no mesmo POST, e a acao sai uma vez.
    credencial = await assinar(nav, aut, f"/telas/fichas/{op.ficha_id}/desafio", csrf)
    r = await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", {"credencial": credencial}, csrf=csrf)
    assert r.status_code == 303, r.text
    enviada = await esperar_estado(amb, protocolo, Estado.ENVIADA)
    assert chamadas(amb, "cancelar_servico") == 1
    async with amb.admin.connect() as c:
        ficha = (
            await c.execute(text("SELECT decisao, com_passkey FROM fichas WHERE id = :f"), {"f": enviada.ficha_id})
        ).one()
        passkey = (
            await c.execute(text("SELECT contador, ultimo_uso FROM passkeys WHERE pessoa_id = :p"), {"p": amb.ana_id})
        ).one()
    assert (ficha.decisao, ficha.com_passkey) == ("confirmada", True)
    assert passkey.contador == aut.contador
    assert passkey.ultimo_uso is not None
    frases = [linha.frase for linha in await extrato(amb, "ficha_confirmada", amb.ana_id)]
    assert frases == [f"Ficha do protocolo {protocolo} confirmada pela pessoa (com passkey)."]

    # Desativada, a passkey nao serve mais: nem o desafio sai.
    r = await nav.get("/telas/passkeys")
    achado = re.search(r'action="(/telas/passkeys/[0-9a-f-]+/desativar)"', r.text)
    assert achado
    r = await nav.post(achado.group(1), csrf=token_csrf(r.text))
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/passkeys?aviso=passkey_desativada"
    assert (await nav.post(achado.group(1), csrf=csrf)).status_code == 404
    assert [linha.frase for linha in await extrato(amb, "passkey_desativada", amb.ana_id)] == [
        "Uma passkey da pessoa foi desativada."
    ]
    outra = await ficha_de(amb, protocolo_de(await pedir(amb, "cancelar_servico", {"servico": "fixo"})))
    r = await desafio(nav, f"/telas/fichas/{outra.ficha_id}/desafio", csrf)
    assert r.status_code == 409


async def test_desafio_reaproveitado_vencido_ou_de_outra_ficha_recusa(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=60)
    a = await ficha_de(amb, protocolo_de(await pedir(amb, "cancelar_servico", {"servico": "tv"})))
    b = await ficha_de(amb, protocolo_de(await pedir(amb, "cancelar_servico", {"servico": "internet"})))
    nav = await entrar(amb, http)
    aut = await cadastrar_passkey(amb, nav)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{a.ficha_id}")).text)

    # Desafio vencido.
    credencial = await assinar(nav, aut, f"/telas/fichas/{a.ficha_id}/desafio", csrf)
    async with amb.admin.begin() as c:
        await c.execute(
            text(
                "UPDATE desafios_passkey SET expira_em = now() - interval '1 second'"
                " WHERE pessoa_id = :p AND usado_em IS NULL"
            ),
            {"p": amb.ana_id},
        )
    r = await nav.post(f"/telas/fichas/{a.ficha_id}/confirmar", {"credencial": credencial}, csrf=csrf)
    assert r.status_code == 403
    assert "Este pedido de passkey venceu, já foi usado ou era para outra coisa." in r.text

    # Desafio de outra ficha: nao serve para esta.
    da_b = await assinar(nav, aut, f"/telas/fichas/{b.ficha_id}/desafio", csrf)
    r = await nav.post(f"/telas/fichas/{a.ficha_id}/confirmar", {"credencial": da_b}, csrf=csrf)
    assert r.status_code == 403

    # Assinatura de outra chave, com o mesmo id de credencial: o desafio fica gasto, e a resposta boa nao serve mais.
    opcoes = await opcoes_de(nav, f"/telas/fichas/{a.ficha_id}/desafio", csrf)
    boa = _json(aut.assinar(opcoes))
    impostor = AutenticadorFalso(PUBLICO, credencial_id=aut.credencial_id, contador=aut.contador)
    estragada = _json(impostor.assinar(opcoes))
    r = await nav.post(f"/telas/fichas/{a.ficha_id}/confirmar", {"credencial": estragada}, csrf=csrf)
    assert r.status_code == 403
    assert "A passkey não foi conferida." in r.text
    r = await nav.post(f"/telas/fichas/{a.ficha_id}/confirmar", {"credencial": boa}, csrf=csrf)
    assert r.status_code == 403
    assert "já foi usado" in r.text
    assert (await operacao(amb, a.protocolo)).estado == Estado.AGUARDANDO_FICHA

    # O desafio da ficha B segue valendo para ela, uma vez.
    r = await nav.post(f"/telas/fichas/{b.ficha_id}/confirmar", {"credencial": da_b}, csrf=csrf)
    assert r.status_code == 303, r.text
    assert (await operacao(amb, b.protocolo)).estado == Estado.LIBERADA
    async with amb.admin.connect() as c:
        usados = (
            await c.execute(
                text("SELECT count(*) FROM desafios_passkey WHERE pessoa_id = :p AND usado_em IS NOT NULL"),
                {"p": amb.ana_id},
            )
        ).scalar_one()
    # Gastos: o do cadastro, o da A (pela assinatura de outra chave) e o da B. O vencido e o da B levado a A
    # nao chegaram a ser gastos: nao acharam desafio valido para aquele alvo.
    assert usados == 3
    assert chamadas(amb, "cancelar_servico") == 0


# --- Limites por aproximacao ---


async def test_limite_criado_com_passkey_libera_o_pedido_coberto_sem_ficha(
    amb: Ambiente, http: httpx2.AsyncClient
) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    mensagem, cancelar = amb.vitrine["enviar_mensagem"], amb.vitrine["cancelar_servico"]
    # O ADMIN marcou, na revisao, o campo que diz o destinatario (a tela do ADMIN tem teste em test_telas_rodada2).
    async with amb.admin.begin() as c:
        await c.execute(
            text(
                "UPDATE ferramentas_revisadas SET campos_destinatario = ARRAY['destinatario']"
                " WHERE nome_vitrine = :n AND situacao = 'ativa'"
            ),
            {"n": mensagem},
        )
    nav = await entrar(amb, http)
    r = await nav.get("/telas/limites")
    assert r.status_code == 200
    achado = re.search(r'href="/telas/limites/novo\?escolha=([0-9a-f-]+:[0-9a-f-]+)"[^>]*>Criar limite', r.text)
    assert achado
    assert f"Estas ações são de risco alto e sempre passam pela ficha: {cancelar}." in r.text
    escolhas = re.findall(r"/telas/limites/novo\?escolha=([0-9a-f-]+:[0-9a-f-]+)", r.text)
    assert len(escolhas) == 2  # criar_chamado e enviar_mensagem; o de risco alto nao entra
    por_nome = {}
    for e in escolhas:
        pagina = await nav.get(f"/telas/limites/novo?escolha={e}")
        assert pagina.status_code == 200
        por_nome[re.search(r"<h2>([^<]+)</h2>", pagina.text).group(1)] = (e, pagina.text)  # type: ignore[union-attr]
    escolha, pagina = por_nome[mensagem]
    assert '<option value="destinatario"' in pagina
    assert "Qualquer destinatário." in pagina
    assert 'data-desafio="/telas/limites/desafio"' in pagina
    csrf = token_csrf(pagina)
    aut = await cadastrar_passkey(amb, nav)

    form = {
        "escolha": escolha,
        "destinatarios": "estes",
        "campo": "destinatario",
        "valores": "equipe\nchefe",
        "por_dia": "2",
        "dias": "1",
    }
    # Sem passkey: recusado.
    r = await nav.post("/telas/limites", form, csrf=csrf)
    assert r.status_code == 403
    # Passkey aprovada para outro pedido (outra quantidade): nao serve.
    outra = await assinar(nav, aut, "/telas/limites/desafio", csrf, {**form, "por_dia": "5"})
    r = await nav.post("/telas/limites", {**form, "credencial": outra}, csrf=csrf)
    assert r.status_code == 403
    # Acima do teto da politica: recusado antes da passkey.
    r = await desafio(nav, "/telas/limites/desafio", csrf, {**form, "por_dia": "6"})
    assert r.status_code == 400
    assert "A quantidade por dia vai de 1 a 5." in r.json()["erro"]

    # So o campo marcado pelo ADMIN e conferido; o texto da mensagem fica livre, e a tela diz isso.
    assert "<strong>destinatario</strong>. O limite confere só ele. O resto do pedido, como o texto, fica livre." in (
        pagina
    )
    assert "precisam vir vazios" not in pagina
    assert '<option value="texto"' not in pagina
    credencial_criar = await assinar(nav, aut, "/telas/limites/desafio", csrf, form)
    r = await nav.post("/telas/limites", {**form, "credencial": credencial_criar}, csrf=csrf)
    assert r.status_code == 303, r.text
    limite_id = r.headers["location"].split("/")[3].split("?")[0]
    r = await nav.get(r.headers["location"])
    assert "O limite foi criado" in r.text
    assert "<dd>Só destinatario: equipe, chefe</dd>" in r.text
    # A mesma resposta de novo, com o limite valendo: o igual ja existe (o reaproveitamento do desafio e provado
    # depois de encerrar, quando nada mais barra antes da passkey).
    r = await nav.post("/telas/limites", {**form, "credencial": credencial_criar}, csrf=csrf)
    assert r.status_code == 409
    async with amb.admin.connect() as c:
        linhas = (
            await c.execute(
                text("SELECT id, por_dia, destinatarios, com_passkey, situacao FROM limites WHERE pessoa_id = :p"),
                {"p": amb.ana_id},
            )
        ).all()
    assert len(linhas) == 1
    assert (str(linhas[0].id), linhas[0].por_dia, linhas[0].com_passkey, linhas[0].situacao) == (
        limite_id,
        2,
        True,
        "ativo",
    )
    # So o campo marcado entra no limite: o texto fica livre.
    assert linhas[0].destinatarios == [{"campo": "destinatario", "valores": ["equipe", "chefe"]}]
    frases = [linha.frase for linha in await extrato(amb, "limite_criado", amb.ana_id)]
    assert frases == [f"A pessoa criou um limite de 2 por dia para {mensagem}, com passkey."]

    # Risco alto nunca entra em limite, nem pedido direto.
    async with amb.admin.connect() as c:
        ferramenta_alta = (
            await c.execute(
                text("SELECT id FROM ferramentas_revisadas WHERE nome_vitrine = :n AND situacao = 'ativa'"),
                {"n": cancelar},
            )
        ).scalar_one()
    alta = f"{amb.aplicativo_id}:{ferramenta_alta}"
    assert alta not in escolhas
    r = await nav.get(f"/telas/limites/novo?escolha={alta}")
    assert r.status_code == 400
    assert "Ações de risco alto nunca entram em limite: sempre passam pela ficha." in r.text
    r = await desafio(nav, "/telas/limites/desafio", csrf, {**form, "escolha": alta})
    assert r.status_code == 400
    r = await nav.post("/telas/limites", {**form, "escolha": alta, "credencial": credencial_criar}, csrf=csrf)
    assert r.status_code == 400
    assert "sempre passam pela ficha" in r.text

    # O pedido coberto sai sem ficha, com texto qualquer; o de fora do limite pede ficha.
    coberto = await pedir(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": "Texto livre: externo@fora.com"})
    assert coberto[PREFIXO_META + "estado"] in ("liberada", "enviando", "enviada")
    assert (await operacao(amb, protocolo_de(coberto))).ficha_id is None
    await esperar_estado(amb, protocolo_de(coberto), Estado.ENVIADA)
    assert chamadas(amb, "enviar_mensagem") == 1
    fora = await pedir(amb, "enviar_mensagem", {"destinatario": "estranho", "texto": ""})
    assert fora[PREFIXO_META + "estado"] == "aguardando_ficha"
    assert (await operacao(amb, protocolo_de(fora))).ficha_id is not None

    # Ampliar pede passkey; encerrar nao.
    csrf = token_csrf((await nav.get(f"/telas/limites/{limite_id}")).text)
    ampliar = {"por_dia": "3", "dias": "5"}
    r = await nav.post(f"/telas/limites/{limite_id}/ampliar", ampliar, csrf=csrf)
    assert r.status_code == 403
    credencial = await assinar(nav, aut, f"/telas/limites/{limite_id}/desafio", csrf, ampliar)
    r = await nav.post(f"/telas/limites/{limite_id}/ampliar", {**ampliar, "credencial": credencial}, csrf=csrf)
    assert r.status_code == 303, r.text
    frases = [linha.frase for linha in await extrato(amb, "limite_ampliado", amb.ana_id)]
    assert frases == [f"A pessoa ampliou um limite de {mensagem} para 3 por dia, com passkey."]

    # Outra pessoa nao ve, nao amplia nem encerra o limite, que segue valendo.
    bia = await entrar(amb, http, "pessoa-bia", "Bia")
    assert (await bia.get(f"/telas/limites/{limite_id}")).status_code == 404
    csrf_bia = token_csrf((await bia.get("/telas/limites")).text)
    r = await bia.post(f"/telas/limites/{limite_id}/encerrar", csrf=csrf_bia)
    assert r.status_code == 404
    r = await bia.post(f"/telas/limites/{limite_id}/ampliar", {"por_dia": "5", "dias": "30"}, csrf=csrf_bia)
    assert r.status_code == 404
    async with amb.admin.connect() as c:
        valendo = (await c.execute(text("SELECT por_dia, situacao FROM limites WHERE id = :i"), {"i": limite_id})).one()
    assert (valendo.por_dia, valendo.situacao) == (3, "ativo")

    r = await nav.post(f"/telas/limites/{limite_id}/encerrar", csrf=csrf)
    assert r.status_code == 303
    assert r.headers["location"] == "/telas/limites?aviso=limite_encerrado&canceladas=0"
    async with amb.admin.connect() as c:
        final = (await c.execute(text("SELECT por_dia, situacao FROM limites WHERE id = :i"), {"i": limite_id})).one()
    assert (final.por_dia, final.situacao) == (3, "encerrado")
    assert await extrato(amb, "limite_encerrado", amb.ana_id)
    depois = await pedir(amb, "enviar_mensagem", {"destinatario": "chefe", "texto": ""})
    assert depois[PREFIXO_META + "estado"] == "aguardando_ficha"
    assert chamadas(amb, "enviar_mensagem") == 1

    # A mesma resposta da passkey de novo, agora sem limite igual valendo: chega a conferencia da passkey, e o
    # desafio ja foi usado. Nenhum limite novo nasce.
    r = await nav.post("/telas/limites", {**form, "credencial": credencial_criar}, csrf=csrf)
    assert r.status_code == 403
    assert "Este pedido de passkey venceu, já foi usado ou era para outra coisa." in r.text
    async with amb.admin.connect() as c:
        quantos = (
            await c.execute(text("SELECT count(*) FROM limites WHERE pessoa_id = :p"), {"p": amb.ana_id})
        ).scalar_one()
    assert quantos == 1


# --- "Estamos confirmando" ---


async def test_estamos_confirmando_decisao_registrada_uma_vez(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=60)
    args = {"titulo": "Resposta perdida"}
    protocolo = protocolo_de(await pedir(amb, "criar_chamado", args))
    op = await ficha_de(amb, protocolo)
    nav = await entrar(amb, http)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    assert (await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)).status_code == 303
    # O envio comecou e a resposta se perdeu: o despachante que assume deixa a acao nao confirmada.
    liberada = await operacao(amb, protocolo)
    async with amb.despachante_db.begin() as c:
        assert await fila.marcar_enviando(c, liberada)
        assert protocolo in await fila.retomar_enviando(c)
    assert (await operacao(amb, protocolo)).estado == Estado.NAO_CONFIRMADA

    r = await nav.get("/telas/pendencias")
    assert "Estamos confirmando (1)" in r.text
    r = await nav.get(f"/telas/acoes/{op.id}")
    assert "Estamos confirmando" in r.text
    assert "Ele pode ter chegado ao sistema." in r.text
    assert "Nada será reenviado." in r.text
    assert 'Não aconteceu.</strong> <span class="dica">Um novo pedido igual fica liberado' in r.text
    assert r.text.count("Um pedido igual devolve esta mesma ação. Nada novo sai.") == 2
    assert "hx-trigger" not in r.text  # o formulario nao se apaga sozinho

    r = await nav.post(f"/telas/acoes/{op.id}/decidir", {"decisao": "talvez"}, csrf=csrf)
    assert r.status_code == 400
    r = await nav.post(f"/telas/acoes/{op.id}/decidir", {"decisao": "nao_sei", "observacao": "Vou ligar"}, csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    assert "Sua decisão foi registrada." in r.text
    assert "Você registrou: <strong>Ainda não sei</strong>." in r.text
    r = await nav.post(f"/telas/acoes/{op.id}/decidir", {"decisao": "aconteceu"}, csrf=csrf)
    assert r.status_code == 409
    assert "Esta decisão já tinha sido registrada." in r.text
    async with amb.admin.connect() as c:
        decisoes = (
            await c.execute(
                text("SELECT decisao, observacao FROM decisoes_nao_confirmada WHERE operacao_id = :o"), {"o": op.id}
            )
        ).all()
    assert [(d.decisao, d.observacao) for d in decisoes] == [("nao_sei", "Vou ligar")]
    assert len(await extrato(amb, "nao_confirmada_decidida", amb.ana_id)) == 1
    r = await nav.get("/telas/pendencias")
    assert "Nada espera por você agora." in r.text

    # "Nao sei": o pedido igual devolve a mesma operacao, e nada sai.
    repetido = await pedir(amb, "criar_chamado", args)
    assert protocolo_de(repetido) == protocolo
    assert repetido[PREFIXO_META + "repetido"] is True
    assert chamadas(amb, "criar_chamado") == 0


# --- Comprovante de bloqueio em tres partes ---


async def test_comprovante_do_bloqueio_em_tres_partes(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    saiu = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Saiu antes"}))
    nav = await entrar(amb, http)
    op = await ficha_de(amb, saiu)
    csrf = token_csrf((await nav.get(f"/telas/fichas/{op.ficha_id}")).text)
    assert (await nav.post(f"/telas/fichas/{op.ficha_id}/confirmar", csrf=csrf)).status_code == 303
    await esperar_estado(amb, saiu, Estado.ENVIADA)
    # Outra saiu, mas a resposta se perdeu: fica "em conferencia", nao "ja seguiu".
    await politica(amb, OUTBOX_HOLD_SECONDS=600)
    duvida = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Resposta perdida"}))
    op_duvida = await ficha_de(amb, duvida)
    assert (await nav.post(f"/telas/fichas/{op_duvida.ficha_id}/confirmar", csrf=csrf)).status_code == 303
    async with amb.despachante_db.begin() as c:
        assert await fila.marcar_enviando(c, await operacao(amb, duvida))
        assert duvida in await fila.retomar_enviando(c)
    esperando = protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Esperando ficha"}))

    r = await nav.post("/telas/bloqueio", {"aplicativo": "todos"}, csrf=csrf)
    assert r.status_code == 303
    r = await nav.get(r.headers["location"])
    html = r.text
    um, dois, tres = (
        html.index("1. O que paramos a tempo"),
        html.index("2. O que já tinha seguido"),
        html.index("3. O que ainda está em conferência"),
    )
    assert f">{esperando}</a>: esperava a sua ficha." in html[um:dois]
    assert re.search(rf">{saiu}</a>: entregue ao sistema, em \d\d/\d\d/\d{{4}} \d\d:\d\d\.", html[dois:tres])
    # Fase 4: a dica e por linha, pelo nivel do selo com que a acao saiu (aqui sem numero: nivel 1).
    assert "Neste sistema (nível 1), “seguiu” quer dizer entregue ao sistema, não concluído por ele." in html[dois:tres]
    assert f">{duvida}</a>: pode ter saído, mas a resposta do sistema não chegou inteira" in html[tres:]
    assert duvida not in html[:tres]
    assert "O que o sistema informou" not in html
    assert (await operacao(amb, esperando)).estado == Estado.PARADA_POR_BLOQUEIO
    assert chamadas(amb, "criar_chamado") == 1


# --- Passkey desligada nesta instalacao: o reforco e o login refeito na hora ---


async def test_passkey_desligada_risco_alto_pede_login_refeito(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, http: httpx2.AsyncClient
) -> None:
    async with ambiente(banco_migrado, monkeypatch, FEATURE_PASSKEY=False) as amb:
        await politica(amb, OUTBOX_HOLD_SECONDS=0)
        protocolo = protocolo_de(await pedir(amb, "cancelar_servico", {"servico": "tv"}))
        op = await ficha_de(amb, protocolo)
        assert not op.ficha_exige_passkey
        assert op.ficha_risco == "alto"
        nav = await entrar(amb, http)
        r = await nav.get("/telas/passkeys")
        assert "A passkey está desligada nesta instalação." in r.text
        assert "Cadastrar passkey" not in r.text

        # Login de dez minutos atras: a sessao vale, mas a ficha de risco alto nao confirma com ela.
        await envelhecer_login(amb, amb.ana_id)
        ficha = f"/telas/fichas/{op.ficha_id}"
        r = await nav.get(ficha)
        assert "no lugar dela, confirmar pede um login refeito na hora." in r.text
        assert f'href="/telas/login/refazer?{urlencode({"voltar": ficha})}"' in r.text
        assert "Confirmar</button>" not in r.text
        assert "Confirmar com a passkey" not in r.text
        csrf = token_csrf(r.text)
        r = await nav.post(f"{ficha}/confirmar", csrf=csrf)
        assert r.status_code == 403
        assert "para confirmar sem a passkey, entre de novo agora e confirme logo depois." in r.text
        r = await desafio(nav, f"{ficha}/desafio", csrf)
        assert r.status_code == 409
        await asyncio.sleep(1.5)
        assert (await operacao(amb, protocolo)).estado == Estado.AGUARDANDO_FICHA
        assert chamadas(amb, "cancelar_servico") == 0
        assert await extrato(amb, "ficha_confirmada", amb.ana_id) == []

        # O provedor que nao pede a senha de novo (devolve o auth_time antigo): a tela avisa e segue pedindo.
        amb.idp.estragar = {"auth_time": int(time.time()) - 3600}  # type: ignore[attr-defined]
        r = await refazer_login(amb, nav, ficha)
        assert "O provedor de login não pediu a sua senha de novo." in r.text
        assert "Confirmar</button>" not in r.text
        amb.idp.estragar = {}  # type: ignore[attr-defined]

        # Login refeito de verdade: volta para a ficha e confirma, uma vez.
        r = await refazer_login(amb, nav, ficha)
        assert "Você entrou de novo há pouco: já dá para confirmar." in r.text
        assert "Confirmar</button>" in r.text
        csrf = token_csrf(r.text)
        r = await nav.post(f"{ficha}/confirmar", csrf=csrf)
        assert r.status_code == 303, r.text
        await esperar_estado(amb, protocolo, Estado.ENVIADA)
        assert chamadas(amb, "cancelar_servico") == 1
        frases = [linha.frase for linha in await extrato(amb, "ficha_confirmada", amb.ana_id)]
        assert len(frases) == 1
        assert "login refeito" in frases[0]

        # Ficha montada quando a passkey estava ligada (pedia passkey): sem ela, vale o login refeito.
        antiga = await ficha_de(amb, protocolo_de(await pedir(amb, "cancelar_servico", {"servico": "fixo"})))
        async with amb.admin.begin() as c:
            # Como se a ficha tivesse nascido com a passkey ligada (a guarda do banco nao deixa mudar depois).
            await c.execute(text("SET LOCAL session_replication_role = replica"))
            await c.execute(text("UPDATE fichas SET exige_passkey = true WHERE id = :f"), {"f": antiga.ficha_id})
        await envelhecer_login(amb, amb.ana_id)
        r = await nav.get(f"/telas/fichas/{antiga.ficha_id}")
        assert "Esta ficha pedia a passkey, que foi desligada nesta instalação." in r.text
        assert "Cadastre uma passkey" not in r.text
        assert "passkey.js" not in r.text
        r = await nav.post(f"/telas/fichas/{antiga.ficha_id}/confirmar", csrf=token_csrf(r.text))
        assert r.status_code == 403
        assert (await operacao(amb, antiga.protocolo)).estado == Estado.AGUARDANDO_FICHA
        r = await refazer_login(amb, nav, f"/telas/fichas/{antiga.ficha_id}")
        r = await nav.post(f"/telas/fichas/{antiga.ficha_id}/confirmar", csrf=token_csrf(r.text))
        assert r.status_code == 303, r.text
        await esperar_estado(amb, antiga.protocolo, Estado.ENVIADA)
        assert chamadas(amb, "cancelar_servico") == 2

        # Risco medio segue confirmando so pela ficha, com o login antigo.
        await envelhecer_login(amb, amb.ana_id)
        medio = await ficha_de(amb, protocolo_de(await pedir(amb, "criar_chamado", {"titulo": "Risco medio"})))
        r = await nav.get(f"/telas/fichas/{medio.ficha_id}")
        assert "login refeito" not in r.text
        r = await nav.post(f"/telas/fichas/{medio.ficha_id}/confirmar", csrf=token_csrf(r.text))
        assert r.status_code == 303, r.text


async def test_passkey_desligada_limite_pede_login_refeito_e_ampliar_segue_o_teto(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, http: httpx2.AsyncClient
) -> None:
    async with ambiente(banco_migrado, monkeypatch, FEATURE_PASSKEY=False) as amb:
        await politica(amb, OUTBOX_HOLD_SECONDS=0)
        nav = await entrar(amb, http)
        r = await nav.get("/telas/limites")
        assert "No lugar dela, criar e ampliar um limite pedem um login refeito na hora." in r.text
        escolha = ""
        for e in re.findall(r"/telas/limites/novo\?escolha=([0-9a-f-]+:[0-9a-f-]+)", r.text):
            if amb.vitrine["enviar_mensagem"] in (await nav.get(f"/telas/limites/novo?escolha={e}")).text:
                escolha = e
        assert escolha
        form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "5", "dias": "1"}

        # Login antigo: a tela pede o login refeito, e criar e recusado.
        await envelhecer_login(amb, amb.ana_id)
        novo = f"/telas/limites/novo?{urlencode({'escolha': escolha})}"
        r = await nav.get(novo)
        assert "Criar um limite pede um login refeito na hora" in r.text
        assert "Entrar de novo agora" in r.text
        csrf = token_csrf(r.text)
        r = await nav.post("/telas/limites", form, csrf=csrf)
        assert r.status_code == 403
        assert "criar ou ampliar um limite pede um login refeito na hora" in r.text
        async with amb.admin.connect() as c:
            assert (
                await c.execute(text("SELECT count(*) FROM limites WHERE pessoa_id = :p"), {"p": amb.ana_id})
            ).scalar_one() == 0

        # Refeito o login, cria.
        r = await refazer_login(amb, nav, novo)
        assert "Entrar de novo agora" not in r.text
        csrf = token_csrf(r.text)
        r = await nav.post("/telas/limites", form, csrf=csrf)
        assert r.status_code == 303, r.text
        limite_id = r.headers["location"].split("/")[3].split("?")[0]
        async with amb.admin.connect() as c:
            limite = (await c.execute(text("SELECT com_passkey FROM limites WHERE id = :i"), {"i": limite_id})).one()
        assert limite.com_passkey is False
        # O extrato diz como foi criado: com o login refeito na hora (a passkey esta desligada).
        criados = await extrato(amb, "limite_criado_login_refeito", amb.ana_id)
        assert [x.frase for x in criados] == [
            f"A pessoa criou um limite de 5 por dia para {amb.vitrine['enviar_mensagem']}, com o login refeito na"
            " hora (passkey desligada nesta instalacao)."
        ]
        coberto = protocolo_de(await pedir(amb, "enviar_mensagem", {"destinatario": "qualquer um", "texto": "oi"}))
        await esperar_estado(amb, coberto, Estado.ENVIADA)
        assert chamadas(amb, "enviar_mensagem") == 1

        # O ADMIN baixa o teto: o limite de 5 por dia nao renova com 5, so ate o teto novo.
        await politica(amb, ALLOWANCE_MAX_PER_DAY=2, ALLOWANCE_MAX_DAYS=10)
        pagina = f"/telas/limites/{limite_id}"
        r = await nav.get(pagina)
        assert "O teto mudou: agora um limite vai até 2 vezes por dia e até 10 dias." in r.text
        assert 'min="2" max="2" value="2"' in r.text
        csrf = token_csrf(r.text)
        r = await nav.post(f"{pagina}/ampliar", {"por_dia": "5", "dias": "10"}, csrf=csrf)
        assert r.status_code == 400
        assert "O teto mudou: agora vai até 2 por dia." in r.text
        r = await nav.post(f"{pagina}/ampliar", {"por_dia": "2", "dias": "30"}, csrf=csrf)
        assert r.status_code == 400
        # Ampliar com o login antigo: recusado, nada muda.
        await envelhecer_login(amb, amb.ana_id)
        r = await nav.post(f"{pagina}/ampliar", {"por_dia": "2", "dias": "10"}, csrf=csrf)
        assert r.status_code == 403
        async with amb.admin.connect() as c:
            antes = (await c.execute(text("SELECT por_dia FROM limites WHERE id = :i"), {"i": limite_id})).one()
        assert antes.por_dia == 5
        r = await refazer_login(amb, nav, pagina)
        r = await nav.post(f"{pagina}/ampliar", {"por_dia": "2", "dias": "10"}, csrf=token_csrf(r.text))
        assert r.status_code == 303, r.text
        async with amb.admin.connect() as c:
            depois = (
                await c.execute(
                    text(
                        "SELECT por_dia, valido_ate > now() + interval '9 days' AS longe,"
                        " valido_ate <= now() + interval '10 days' AS no_teto FROM limites WHERE id = :i"
                    ),
                    {"i": limite_id},
                )
            ).one()
        assert (depois.por_dia, depois.longe, depois.no_teto) == (2, True, True)
        # O extrato diz a verdade: ampliado com o login refeito, nao "com passkey".
        assert await extrato(amb, "limite_ampliado", amb.ana_id) == []
        ampliados = await extrato(amb, "limite_ampliado_login_refeito", amb.ana_id)
        assert [x.frase for x in ampliados] == [
            f"A pessoa ampliou um limite de {amb.vitrine['enviar_mensagem']} para 2 por dia, com o login refeito na"
            " hora (passkey desligada nesta instalacao)."
        ]
        assert ampliados[0].dados["com_passkey"] is False


async def test_encerrar_limite_cancela_o_que_ele_liberou_e_ainda_espera(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, http: httpx2.AsyncClient
) -> None:
    async with ambiente(banco_migrado, monkeypatch, FEATURE_PASSKEY=False) as amb:
        await politica(amb, OUTBOX_HOLD_SECONDS=600)
        nav = await entrar(amb, http)
        r = await nav.get("/telas/limites")
        escolha = ""
        for e in re.findall(r"/telas/limites/novo\?escolha=([0-9a-f-]+:[0-9a-f-]+)", r.text):
            if amb.vitrine["enviar_mensagem"] in (await nav.get(f"/telas/limites/novo?escolha={e}")).text:
                escolha = e
        csrf = token_csrf(r.text)
        form = {"escolha": escolha, "destinatarios": "qualquer", "por_dia": "5", "dias": "1"}
        r = await nav.post("/telas/limites", form, csrf=csrf)
        assert r.status_code == 303, r.text
        limite_id = r.headers["location"].split("/")[3].split("?")[0]

        # O aplicativo dispara pedidos cobertos; eles esperam a hora de sair (dez minutos).
        protocolos = [
            protocolo_de(await pedir(amb, "enviar_mensagem", {"destinatario": f"pessoa {i}", "texto": "oi"}))
            for i in range(3)
        ]
        for p in protocolos:
            assert (await operacao(amb, p)).estado == Estado.LIBERADA
        # Uma ja saiu ao sistema antes (o despachante marcou "enviando"): encerrar nao mexe nela.
        async with amb.despachante_db.begin() as c:
            assert await fila.marcar_enviando(c, await operacao(amb, protocolos[2]))

        r = await nav.post(f"/telas/limites/{limite_id}/encerrar", csrf=csrf)
        assert r.status_code == 303
        assert r.headers["location"] == "/telas/limites?aviso=limite_encerrado&canceladas=2"
        r = await nav.get(r.headers["location"])
        assert "2 ações liberadas por ele ainda esperavam para sair e foram canceladas" in r.text
        for p in protocolos[:2]:
            parada = await operacao(amb, p)
            assert parada.estado == Estado.CANCELADA
            assert parada.desfecho == "cancelada porque a pessoa encerrou o limite que a liberou"
            assert parada.mudou_por == "pessoa"
        assert (await operacao(amb, protocolos[2])).estado == Estado.ENVIANDO
        assert len(await extrato(amb, "acao_parada", amb.ana_id)) == 2
        encerrado = await extrato(amb, "limite_encerrado", amb.ana_id)
        assert encerrado[0].dados["canceladas"] == 2
        await asyncio.sleep(1.5)
        assert chamadas(amb, "enviar_mensagem") == 0


# --- Passkey: cadastrar e desativar pedem o login refeito na hora ---


async def test_cadastrar_e_desativar_passkey_pedem_login_refeito(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    nav = await entrar(amb, http)
    await envelhecer_login(amb, amb.ana_id)
    r = await nav.get("/telas/passkeys")
    assert "Cadastrar ou desativar uma passkey pede um login refeito na hora, como cuidado a mais." in r.text
    assert 'href="/telas/login/refazer?voltar=%2Ftelas%2Fpasskeys"' in r.text
    csrf = token_csrf(r.text)
    # Quem so tem o cookie de uma sessao antiga nao pede nem o desafio, nem cadastra.
    r = await desafio(nav, "/telas/passkeys/desafio", csrf)
    assert r.status_code == 403
    assert "entre de novo agora" in r.json()["erro"]
    falsa = {"type": "public-key", "id": "x", "rawId": "x", "response": {"clientDataJSON": "e30"}}
    r = await nav.post("/telas/passkeys", {"apelido": "Do intruso", "credencial": _json(falsa)}, csrf=csrf)
    assert r.status_code == 403
    async with amb.admin.connect() as c:
        assert (
            await c.execute(text("SELECT count(*) FROM passkeys WHERE pessoa_id = :p"), {"p": amb.ana_id})
        ).scalar_one() == 0
        assert (
            await c.execute(text("SELECT count(*) FROM desafios_passkey WHERE pessoa_id = :p"), {"p": amb.ana_id})
        ).scalar_one() == 0

    # Refeito o login, cadastra.
    r = await refazer_login(amb, nav, "/telas/passkeys")
    assert "Entrar de novo agora" not in r.text
    aut = await cadastrar_passkey(amb, nav)
    assert aut.contador >= 0

    # Desativar com o login antigo: recusado, a passkey segue ativa.
    await envelhecer_login(amb, amb.ana_id)
    r = await nav.get("/telas/passkeys")
    achado = re.search(r'action="(/telas/passkeys/[0-9a-f-]+/desativar)"', r.text)
    assert achado
    r = await nav.post(achado.group(1), csrf=token_csrf(r.text))
    assert r.status_code == 403
    assert "Para cadastrar ou desativar uma passkey, entre de novo agora" in r.text
    async with amb.admin.connect() as c:
        assert (
            await c.execute(text("SELECT ativa FROM passkeys WHERE pessoa_id = :p"), {"p": amb.ana_id})
        ).scalar_one() is True
    assert await extrato(amb, "passkey_desativada", amb.ana_id) == []
    r = await refazer_login(amb, nav, "/telas/passkeys")
    r = await nav.post(achado.group(1), csrf=token_csrf(r.text))
    assert r.status_code == 303
    assert await extrato(amb, "passkey_desativada", amb.ana_id)


# --- Caracteres escondidos na ficha e no resumo da acao ---


async def test_ficha_mostra_caracteres_escondidos_marcados(amb: Ambiente, http: httpx2.AsyncClient) -> None:
    titulo = "Conta 12345\u202e6789"
    m = await pedir(amb, "criar_chamado", {"titulo": titulo, "descricao": "para equipe\u200b\u2066x\u2069"})
    op = await ficha_de(amb, protocolo_de(m))
    assert op.argumentos["titulo"] == titulo  # o que segue para o sistema e o valor verdadeiro
    nav = await entrar(amb, http)
    for caminho in (f"/telas/fichas/{op.ficha_id}", f"/telas/acoes/{op.id}"):
        html = (await nav.get(caminho)).text
        for ch in ("\u202e", "\u200b", "\u2066", "\u2069"):
            assert ch not in html, caminho
        assert "<bdi>Conta 12345[U+202E]6789</bdi>" in html
        assert "<bdi>para equipe[U+200B][U+2066]x[U+2069]</bdi>" in html
        assert "Atenção: este pedido tem caracteres escondidos." in html
