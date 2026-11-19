"""Vigia de fora: banco proprio (segundo banco no mesmo conteiner), entrada real, Mailpit e webhook.

Prova que o vigia busca os fechamentos so com o cracha do programa dele, guarda so acrescentando,
percebe quando um fechamento antigo foi mudado por superusuario (o encadeamento deixa de bater) e
dispara o alarme de silencio por e-mail e webhook quando o gateway para. Tambem repete a ronda de fora.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from standardwebhooks import Webhook
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_claro.avisos import ConfigEnvio
from mcp_claro.avisos.email import EnvioEmail
from mcp_claro.avisos.webhook import EnvioWebhook
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.ronda import Caso, Situacao
from mcp_claro.ronda.config import IdentidadePrograma
from mcp_claro.rotinas.fechamento import fechar_agora
from mcp_claro.vigia import ConfigVigia, Vigia, criar_app_vigia, numero_no_vigia, ultimo_no_vigia, vigia_a_frente
from mcp_claro.vigia.config import SmtpVigia
from tests.apoio import servidor
from tests.integracao import test_rotinas_apoio as apoio
from tests.integracao.test_ronda_apoio import (
    RECURSO,
    VIGIA,
    Entrada,
    FonteFalsa,
    VerificadorQueAceitaTudo,
    criar_banco_vigia,
    engine_como,
    subir_entrada,
)
from tests.integracao.test_rotinas_apoio import Mailpit, certificado_local, confia_em, servidor_https

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

mailpit = apoio.mailpit

SEGREDO_WEBHOOK = "whsec_" + base64.b64encode(b"segredo-do-vigia-com-32-bytes!!!").decode()
SEGREDO_CONTINUIDADE = "segredo-da-continuidade-com-mais-de-32-caracteres"


@pytest.fixture
def banco_vigia(banco_migrado: Parametros) -> Parametros:
    """Banco proprio do vigia, novo a cada teste, no mesmo conteiner do PostgreSQL."""
    return criar_banco_vigia(banco_migrado)


@pytest.fixture
async def engine_vigia(banco_vigia: Parametros) -> AsyncIterator[AsyncEngine]:
    """O vigia conecta ao banco dele com o usuario ``mcp_claro_vigia`` (so le e acrescenta)."""
    engine = engine_como(banco_vigia, "mcp_claro_vigia", pool_size=8, max_overflow=4)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def dono_vigia(banco_vigia: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = engine_como(banco_vigia, None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def entrada(banco_migrado: Parametros) -> AsyncIterator[Entrada]:
    async with subir_entrada(banco_migrado) as e:
        yield e


def identidade(e: Entrada) -> IdentidadePrograma:
    return IdentidadePrograma(VIGIA, None, None, None, "mcp-claro", "outro-servico", e.idp.emissor, RECURSO)


def config(e: Entrada, **kw: Any) -> ConfigVigia:
    valores: dict[str, Any] = {
        "alvo": e.url,
        "identidade": identidade(e),
        "intervalo_pulso": 0.2,
        "intervalo_ronda": 3600.0,
        "silencio": 1.0,
        "falhas_para_alarme": 2,
        "emails": ("alarme@vigia.teste",),
        "primeira_espera": 0.05,
        "token_continuidade": SecretStr(SEGREDO_CONTINUIDADE),
        "envio": ConfigEnvio(retry_max=2, espera_base=0.5, intervalo=0.2, papel="vigia", registrar_no_extrato=False),
        "smtp": SmtpVigia(host="smtp.vigia.teste", remetente="vigia@vigia.teste", starttls=False),
    }
    return ConfigVigia(**(valores | kw))


def vigia(e: Entrada, engine: AsyncEngine, cfg: ConfigVigia | None = None, **kw: Any) -> Vigia:
    return Vigia(
        cfg or config(e), engine, FonteFalsa(e.idp, client_id=VIGIA), emissor=e.idp.emissor, recurso=RECURSO, **kw
    )


async def fechar(e: Entrada, linhas: int = 2) -> int:
    """Algumas linhas no extrato e um fechamento novo no gateway, pelo papel das rotinas."""
    async with e.rotinas_db.begin() as c:
        for _ in range(linhas):
            await registrar(c, papel="rotinas", tipo="partida", campos={"papel": "rotinas", "versao": "teste"})
    f = None
    for _ in range(50):
        f = await fechar_agora(e.rotinas_db)
        if f is not None:
            break
        await asyncio.sleep(0.1)
    assert f is not None
    return f.numero


async def alarmes(engine: AsyncEngine, tipo: str | None = None) -> list[Any]:
    async with engine.connect() as c:
        return list(
            await c.execute(
                text("SELECT * FROM alarmes WHERE CAST(:t AS text) IS NULL OR tipo = :t ORDER BY id"), {"t": tipo}
            )
        )


# --- Banco do vigia ----------------------------------------------------------------------------


async def test_banco_do_vigia_so_acrescenta_e_confere_o_encadeamento(
    engine_vigia: AsyncEngine, dono_vigia: AsyncEngine
) -> None:
    h1, h2 = b"\x01" * 32, b"\x02" * 32
    sql = (
        "INSERT INTO fechamentos_guardados (numero, desde_xid, ate_xid, linhas, hash_anterior, hash)"
        " VALUES (:n, CAST(CAST(:d AS text) AS xid8), CAST(CAST(:a AS text) AS xid8), 1, :ha, :h)"
    )
    async with engine_vigia.begin() as c:
        await c.execute(text(sql), {"n": 1, "d": "0", "a": "100", "ha": None, "h": h1})
    # Fechamento que nao encadeia: o gatilho recusa, mesmo para o dono.
    for valores in (
        {"n": 2, "d": "100", "a": "200", "ha": h2, "h": h2},
        {"n": 2, "d": "99", "a": "200", "ha": h1, "h": h2},
    ):
        with pytest.raises(DBAPIError, match="nao encadeia"):
            async with dono_vigia.begin() as c:
                await c.execute(text(sql), valores)
    with pytest.raises(DBAPIError, match="sem o anterior"):
        async with engine_vigia.begin() as c:
            await c.execute(text(sql), {"n": 3, "d": "200", "a": "300", "ha": h2, "h": h2})
    async with engine_vigia.begin() as c:
        await c.execute(text(sql), {"n": 2, "d": "100", "a": "200", "ha": h1, "h": h2})
        await c.execute(text("INSERT INTO alarmes (tipo, chave, resumo) VALUES ('ronda', 'teste', 'teste')"))
        await c.execute(
            text(
                "INSERT INTO rondas_vigia (rodada, caso, esperado, obtido, defendeu)"
                " VALUES (gen_random_uuid(), 'controle', 'x', 'x', true)"
            )
        )
    for comando in (
        "UPDATE fechamentos_guardados SET linhas = 9",
        "DELETE FROM fechamentos_guardados",
        "TRUNCATE fechamentos_guardados",
        "UPDATE alarmes SET resumo = 'x'",
        "DELETE FROM rondas_vigia",
    ):
        for engine in (engine_vigia, dono_vigia):
            with pytest.raises(DBAPIError):
                async with engine.begin() as c:
                    await c.execute(text(comando))


# --- Rota da entrada ---------------------------------------------------------------------------


async def test_rota_dos_fechamentos_so_atende_o_vigia(entrada: Entrada) -> None:
    numero = await fechar(entrada)
    url = f"{entrada.url}/vigia/fechamentos"
    from cryptography.hazmat.primitives.asymmetric import rsa

    async with httpx2.AsyncClient() as http:
        sem = await http.get(url)
        assert sem.status_code == 401
        assert "resource_metadata=" in sem.headers["www-authenticate"]
        falso = entrada.idp.cracha(
            audiencia=RECURSO,
            client_id=VIGIA,
            chave=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        )
        vencido = entrada.idp.cracha(audiencia=RECURSO, client_id=VIGIA, validade=-600)
        outro = entrada.idp.cracha(audiencia="https://outro-servico.teste/api", client_id=VIGIA)
        for cracha in (falso, vencido, outro):
            r = await http.get(url, headers={"authorization": f"Bearer {cracha}"})
            assert r.status_code == 401
        ronda = await http.get(url, headers={"authorization": f"Bearer {entrada.cracha(client_id='mcp-claro-ronda')}"})
        assert ronda.status_code == 403
        cabecalho = {"authorization": f"Bearer {entrada.cracha(client_id=VIGIA)}"}
        todos = await http.get(url, headers=cabecalho)
        assert todos.status_code == 200
        assert todos.headers["cache-control"] == "no-store"
        corpo = todos.json()
        assert [f["numero"] for f in corpo["fechamentos"]] == list(range(1, numero + 1))
        assert corpo["mais"] is False
        primeiro = corpo["fechamentos"][0]
        assert primeiro["hash_anterior"] is None
        assert primeiro["desde_xid"] == "0"
        assert len(bytes.fromhex(primeiro["hash"])) == 32
        assert set(primeiro) == {"numero", "desde_xid", "ate_xid", "linhas", "hash_anterior", "hash", "calculado_em"}
        pagina = (await http.get(url, params={"desde": numero - 1, "limite": 1}, headers=cabecalho)).json()
        assert [f["numero"] for f in pagina["fechamentos"]] == [numero]
        if numero > 1:
            meio = (await http.get(url, params={"desde": 0, "limite": 1}, headers=cabecalho)).json()
            assert meio["mais"] is True
        for ruim in ({"desde": "-1"}, {"desde": "abc"}, {"limite": "0"}, {"limite": "1001"}):
            assert (await http.get(url, params=ruim, headers=cabecalho)).status_code == 400
    async with entrada.dono.connect() as c:
        frase = (
            (
                await c.execute(
                    text("SELECT frase FROM extrato WHERE tipo = 'pedido_recusado' AND dados ->> 'client_id' = :c"),
                    {"c": "mcp-claro-ronda"},
                )
            )
            .scalars()
            .all()
        )
    assert any("/vigia/fechamentos" in f for f in frase)


# --- Guarda dos fechamentos ----------------------------------------------------------------------


@pytest.fixture
async def superusuario(entrada: Entrada) -> AsyncIterator[AsyncEngine]:
    """O dono do banco de teste e superusuario: so ele consegue mudar um fechamento (sem os gatilhos)."""
    return entrada.dono


async def _mexer(dono: AsyncEngine, sql: str, valores: dict[str, Any]) -> None:
    async with dono.begin() as c:
        await c.execute(text("SET LOCAL session_replication_role = replica"))
        await c.execute(text(sql), valores)


async def test_vigia_guarda_os_fechamentos_e_percebe_adulteracao(
    entrada: Entrada, engine_vigia: AsyncEngine, superusuario: AsyncEngine
) -> None:
    v = vigia(entrada, engine_vigia)
    await fechar(entrada)
    async with httpx2.AsyncClient(timeout=5) as http:
        primeira = await v.fechamentos.volta(http)
        assert primeira.completa
        assert not primeira.problemas
        assert primeira.guardados >= 1
        # Mais um fechamento: a volta seguinte guarda so o novo e confere os ultimos de novo.
        novo = await fechar(entrada)
        segunda = await v.fechamentos.volta(http)
        assert (segunda.guardados, segunda.problemas, segunda.completa) == (1, [], False)
        assert segunda.conferidos >= 1
        async with engine_vigia.connect() as c:
            guardados = (await c.execute(text("SELECT numero FROM fechamentos_guardados ORDER BY numero"))).scalars()
            assert list(guardados) == list(range(1, novo + 1))
        assert await alarmes(engine_vigia) == []

        # Superusuario muda um fechamento antigo no gateway: o encadeamento deixa de bater.
        antigo = max(1, novo - 1)
        async with superusuario.connect() as c:
            original = bytes(
                (await c.execute(text("SELECT hash FROM fechamentos WHERE numero = :n"), {"n": antigo})).scalar_one()
            )
        try:
            await _mexer(
                superusuario, "UPDATE fechamentos SET hash = :h WHERE numero = :n", {"h": b"\xaa" * 32, "n": antigo}
            )
            terceira = await v.fechamentos.volta(http, completa=True)
            tipos = {(p.tipo, p.numero) for p in terceira.problemas}
            assert ("fechamento_mudou", antigo) in tipos
            if antigo < novo:
                assert ("encadeamento_quebrado", antigo + 1) in tipos
            assert terceira.guardados == 0
            mudou = await alarmes(engine_vigia, "fechamento_mudou")
            assert [a.chave for a in mudou] == [f"fechamento_mudou:{antigo}"]
            # A mesma adulteracao vista de novo nao repete o alarme.
            await v.fechamentos.volta(http, completa=True)
            assert len(await alarmes(engine_vigia, "fechamento_mudou")) == 1
            async with engine_vigia.connect() as c:
                avisos = list(await c.execute(text("SELECT * FROM avisos WHERE evento = 'vigia_fechamento_mudou'")))
            assert [(a.canal, a.destino) for a in avisos] == [("email", "alarme@vigia.teste")]
        finally:
            await _mexer(
                superusuario, "UPDATE fechamentos SET hash = :h WHERE numero = :n", {"h": original, "n": antigo}
            )

        # Superusuario muda o ultimo e o gateway calcula o seguinte em cima dele: o novo nao encadeia no guardado.
        async with superusuario.connect() as c:
            original = bytes(
                (await c.execute(text("SELECT hash FROM fechamentos WHERE numero = :n"), {"n": novo})).scalar_one()
            )
        falso = None
        try:
            await _mexer(
                superusuario, "UPDATE fechamentos SET hash = :h WHERE numero = :n", {"h": b"\xbb" * 32, "n": novo}
            )
            falso = await fechar(entrada)
            quarta = await v.fechamentos.volta(http)
            tipos = {(p.tipo, p.numero) for p in quarta.problemas}
            assert ("encadeamento_quebrado", falso) in tipos
            assert ("fechamento_mudou", novo) in tipos
            assert quarta.guardados == 0
            async with engine_vigia.connect() as c:
                assert (await c.execute(text("SELECT max(numero) FROM fechamentos_guardados"))).scalar_one() == novo
            assert [a.chave for a in await alarmes(engine_vigia, "encadeamento_quebrado")][-1] == (
                f"encadeamento_quebrado:{falso}"
            )
        finally:
            if falso is not None:
                await _mexer(superusuario, "DELETE FROM fechamentos WHERE numero = :n", {"n": falso})
            await _mexer(superusuario, "UPDATE fechamentos SET hash = :h WHERE numero = :n", {"h": original, "n": novo})
        # Desfeita a adulteracao, a volta seguinte confere tudo de novo, sem problema.
        assert (await v.fechamentos.volta(http, completa=True)).problemas == []


async def test_fechamentos_sem_resposta_viram_alarme(entrada: Entrada, engine_vigia: AsyncEngine) -> None:
    agora = [time.time()]
    v = vigia(entrada, engine_vigia, relogio=lambda: agora[0])
    v.fechamentos.fonte = FonteFalsa(entrada.idp, client_id="mcp-claro-ronda")  # cracha que a rota recusa (403)
    async with httpx2.AsyncClient(timeout=5) as http:
        volta = await v.fechamentos.volta(http)
        assert volta.falhou == "a entrada respondeu HTTP 403"
        assert await alarmes(engine_vigia, "fechamentos_sem_resposta") == []
        agora[0] += 5.0
        await v.fechamentos.volta(http)
    (alarme,) = await alarmes(engine_vigia, "fechamentos_sem_resposta")
    assert "nao consegue buscar" in alarme.resumo


# --- Continuidade ----------------------------------------------------------------------------------


async def test_rota_de_continuidade_diz_o_ultimo_guardado(entrada: Entrada, engine_vigia: AsyncEngine) -> None:
    cfg = config(entrada)
    app = criar_app_vigia(
        entrada.p, engine_vigia, config=cfg, envios={}, fonte=FonteFalsa(entrada.idp, client_id=VIGIA)
    )
    async with servidor(app) as url:
        segredo = SecretStr(SEGREDO_CONTINUIDADE)
        async with httpx2.AsyncClient() as http:
            assert (await http.get(f"{url}/ultimo-fechamento")).status_code == 401
            errado = {"authorization": "Bearer segredo-errado-com-mais-de-32-caracteres-xx"}
            assert (await http.get(f"{url}/ultimo-fechamento", headers=errado)).status_code == 401
            saude = (await http.get(f"{url}/saude")).json()
        assert saude["papel"] == "vigia"
        numero_gateway = await fechar(entrada)
        fim = time.monotonic() + 10
        ultimo = await ultimo_no_vigia(url, segredo)
        while ultimo.numero is None and time.monotonic() < fim:  # a primeira volta dos fechamentos roda na partida
            await asyncio.sleep(0.1)
            ultimo = await ultimo_no_vigia(url, segredo)
        assert ultimo.numero is not None
        assert ultimo.hash is not None
        assert len(ultimo.hash) == 32
        assert await numero_no_vigia(url, segredo) == ultimo.numero
        assert not vigia_a_frente(numero_gateway, ultimo)
        assert vigia_a_frente(ultimo.numero - 1, ultimo)
        assert vigia_a_frente(None, ultimo)
    assert await numero_no_vigia(url, segredo, prazo=1.0) is None  # vigia parado: sem resposta


async def test_continuidade_sem_segredo_configurado(entrada: Entrada, engine_vigia: AsyncEngine) -> None:
    cfg = config(entrada, token_continuidade=None)
    app = criar_app_vigia(entrada.p, engine_vigia, config=cfg, envios={}, fonte=None)
    async with servidor(app) as url, httpx2.AsyncClient() as http:
        r = await http.get(f"{url}/ultimo-fechamento", headers={"authorization": f"Bearer {SEGREDO_CONTINUIDADE}"})
    assert r.status_code == 503


# --- Ronda de fora -----------------------------------------------------------------------------------


async def test_ronda_de_fora_recusa_tudo(entrada: Entrada, engine_vigia: AsyncEngine) -> None:
    v = vigia(entrada, engine_vigia)
    async with httpx2.AsyncClient(timeout=5) as http:
        r = await v.ronda.rodar(http)
    assert all(t.situacao is Situacao.CONFORME for t in r.tentativas), [(t.caso, t.obtido) for t in r.tentativas]
    assert not r.alarmou
    async with engine_vigia.connect() as c:
        casos = (await c.execute(text("SELECT caso FROM rondas_vigia WHERE rodada = :r"), {"r": r.id})).scalars()
        assert set(casos) == {str(c) for c in Caso}


async def test_ronda_de_fora_alarma_com_falha_plantada(banco_migrado: Parametros, engine_vigia: AsyncEngine) -> None:
    async with subir_entrada(banco_migrado, planta=VerificadorQueAceitaTudo) as e:
        v = vigia(
            e,
            engine_vigia,
            config(e, webhooks=("https://webhook.vigia.teste/alarme",), segredo_webhook=SecretStr(SEGREDO_WEBHOOK)),
        )
        async with httpx2.AsyncClient(timeout=5) as http:
            r1 = await v.ronda.rodar(http)
            r2 = await v.ronda.rodar(http)
            r3 = await v.ronda.rodar(http)
    assert {t.caso for t in r1.resumo.passagens} == {Caso.CRACHA_FALSO, Caso.CRACHA_VENCIDO, Caso.OUTRO_CONDOMINIO}
    assert [r.seguidas for r in (r1, r2, r3)] == [1, 2, 3]
    assert [r.alarmou for r in (r1, r2, r3)] == [False, True, False]
    (alarme,) = await alarmes(engine_vigia, "ronda")
    assert "cracha falso" in alarme.resumo
    async with engine_vigia.connect() as c:
        canais = sorted((await c.execute(text("SELECT canal FROM avisos WHERE evento = 'vigia_ronda'"))).scalars())
    assert canais == ["email", "webhook"]


# --- Alarme de silencio ------------------------------------------------------------------------------


@pytest.fixture
def certificado(tmp_path: Path) -> tuple[Path, Path]:
    return certificado_local(tmp_path)


async def test_alarme_de_silencio_com_o_gateway_parado(
    banco_migrado: Parametros, engine_vigia: AsyncEngine, mailpit: Mailpit, certificado: tuple[Path, Path]
) -> None:
    """O vigia de verdade (com os lacos), o gateway para, e o alarme chega por e-mail e por webhook."""
    destinatario = f"alarme-{uuid.uuid4().hex[:8]}@vigia.teste"
    recebidos: list[dict[str, Any]] = []

    async def receber(pedido: Request) -> Response:
        corpo = await pedido.body()
        dados = Webhook(SEGREDO_WEBHOOK).verify(corpo, dict(pedido.headers))  # assinatura do vigia
        recebidos.append(dados)
        return Response(status_code=204)

    cert, chave = certificado
    destino = Starlette(routes=[Route("/alarme", receber, methods=["POST"])])
    async with servidor_https(destino, cert, chave) as url_webhook, subir_entrada(banco_migrado) as e:
        cfg = config(
            e,
            emails=(destinatario,),
            webhooks=(f"{url_webhook}/alarme",),
            segredo_webhook=SecretStr(SEGREDO_WEBHOOK),
            smtp=SmtpVigia(host=mailpit.host, porta=mailpit.smtp, remetente="vigia@vigia.teste", starttls=False),
        )
        envios = {
            "email": EnvioEmail(host=mailpit.host, porta=mailpit.smtp, remetente="vigia@vigia.teste", starttls=False),
            "webhook": EnvioWebhook(SecretStr(SEGREDO_WEBHOOK), verify=confia_em(cert)),
        }
        app = criar_app_vigia(e.p, engine_vigia, config=cfg, envios=envios, fonte=FonteFalsa(e.idp, client_id=VIGIA))
        porta_entrada = e.porta
        async with servidor(app) as url_vigia:
            # Gateway no ar: o pulso responde e nada dispara.
            fim = time.monotonic() + 10
            async with httpx2.AsyncClient() as http:
                while time.monotonic() < fim:
                    lacos = (await http.get(f"{url_vigia}/saude")).json()["lacos"]
                    if lacos.get("pulso") == "feita":
                        break
                    await asyncio.sleep(0.1)
            assert lacos.get("pulso") == "feita"
            assert app.state.vigia.pulso.alarmado is None
            # O gateway para (a entrada sai do ar) ...
            await e.parar()
            silencio = await _esperar(lambda: mailpit_com(mailpit, "MCP-CLARO: o gateway nao responde", destinatario))
            assert silencio, "o alarme de silencio nao chegou por e-mail"
            assert "nao responde" in mailpit.texto(silencio[0]["ID"])
            assert await _esperar(lambda: [r for r in recebidos if r["type"] == "vigia_silencio"])
            (alarme,) = await alarmes(engine_vigia, "silencio")
            assert str(porta_entrada) in alarme.resumo
            # ... e volta: sai o aviso de que voltou a responder.
            async with subir_entrada(banco_migrado, porta=porta_entrada):
                voltou = await _esperar(
                    lambda: mailpit_com(mailpit, "MCP-CLARO: o gateway voltou a responder", destinatario)
                )
                assert voltou
                assert await _esperar(lambda: [r for r in recebidos if r["type"] == "vigia_voltou"])
    assert len(await alarmes(engine_vigia, "silencio")) == 1


def mailpit_com(m: Mailpit, assunto: str, para: str) -> list[dict[str, Any]]:
    return [msg for msg in m.com_assunto(assunto) if any(t["Address"] == para for t in msg["To"])]


async def _esperar(condicao: Any, prazo: float = 15.0) -> Any:
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        valor = condicao()
        if valor:
            return valor
        await asyncio.sleep(0.2)
    return condicao()


async def test_pulso_com_relogio_falso(entrada: Entrada, engine_vigia: AsyncEngine) -> None:
    """Silencio contado da ultima resposta boa, um alarme por silencio, e o aviso de volta."""
    agora = [time.time()]
    v = vigia(
        entrada, engine_vigia, config(entrada, silencio=60.0, alvo="http://127.0.0.1:9"), relogio=lambda: agora[0]
    )
    async with httpx2.AsyncClient(timeout=1) as http:
        assert not await v.pulso.bater(http)
        agora[0] += 30
        assert not await v.pulso.bater(http)
        assert await alarmes(engine_vigia, "silencio") == []
        agora[0] += 31
        assert not await v.pulso.bater(http)
        assert len(await alarmes(engine_vigia, "silencio")) == 1
        agora[0] += 120
        assert not await v.pulso.bater(http)
        assert len(await alarmes(engine_vigia, "silencio")) == 1
        v.pulso.cfg = config(entrada, silencio=60.0)  # o gateway volta a responder
        assert await v.pulso.bater(http)
    (voltou,) = await alarmes(engine_vigia, "voltou")
    assert "voltou a responder" in voltou.resumo
    assert v.pulso.alarmado is None
