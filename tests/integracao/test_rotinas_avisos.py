"""Avisos de verdade: e-mail chegando no Mailpit, nova tentativa depois de falha forcada e webhook assinado.

Sobe PostgreSQL 18 real e Mailpit 1.34.0 (modo de erro forcado pela API ``/api/v1/chaos``);
o webhook vai a um servidor HTTPS local que confere a assinatura com ``standardwebhooks``.
A fila roda com o usuario ``mcp_claro_rotinas`` de verdade.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from standardwebhooks import Webhook
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_claro.avisos import ConfigEnvio, avisar_admin, avisar_pessoa, enfileirar, processar_pendentes
from mcp_claro.avisos.email import EnvioEmail
from mcp_claro.avisos.webhook import EnvioWebhook
from tests.integracao import test_rotinas_apoio as apoio
from tests.integracao.test_rotinas_apoio import (
    Mailpit,
    cadastro_minimo,
    certificado_local,
    confia_em,
    servidor_https,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

# Fixtures compartilhadas (definidas em test_rotinas_apoio).
engine_dono = apoio.engine_dono
engine_rotinas = apoio.engine_rotinas
mailpit = apoio.mailpit
parametros_rotinas = apoio.parametros_rotinas

ADMIN = "admin@exemplo.teste"
SEGREDO = "whsec_" + base64.b64encode(b"segredo-de-teste-com-32-bytes!!!").decode()
CONFIG = ConfigEnvio(retry_max=3, espera_base=30, espera_teto=3600, lote=500)


def envio_email(m: Mailpit) -> EnvioEmail:
    return EnvioEmail(
        host=m.host,
        porta=m.smtp,
        remetente="mcp-claro@exemplo.teste",
        usuario="mcp-claro",
        senha=SecretStr("senha-smtp-de-teste"),
        starttls=False,
    )


async def aviso(engine: AsyncEngine, chave: str) -> Any:
    async with engine.connect() as c:
        return (await c.execute(text("SELECT * FROM avisos WHERE chave_unica = :k"), {"k": chave})).one()


async def test_email_chega_no_mailpit(engine_rotinas: AsyncEngine, mailpit: Mailpit) -> None:
    sufixo = uuid.uuid4().hex[:8]
    assunto = f"MCP-CLARO: teste de e-mail {sufixo}"
    async with engine_rotinas.begin() as c:
        assert await enfileirar(c, "email", ADMIN, "teste", assunto, {"texto": "Ola, ADMIN."}, f"teste:{sufixo}")
        assert not await enfileirar(c, "email", ADMIN, "teste", assunto, {"texto": "de novo"}, f"teste:{sufixo}")
    resumo = await processar_pendentes(engine_rotinas, {"email": envio_email(mailpit)}, CONFIG)
    assert resumo.enviados >= 1
    (msg,) = mailpit.com_assunto(assunto)
    assert msg["To"][0]["Address"] == ADMIN
    assert msg["From"]["Address"] == "mcp-claro@exemplo.teste"
    corpo = mailpit.texto(msg["ID"])
    assert "Ola, ADMIN." in corpo
    assert "aviso automatico" in corpo
    linha = await aviso(engine_rotinas, f"teste:{sufixo}")
    assert linha.situacao == "enviado"
    assert linha.tentativas == 1
    assert linha.enviado_em is not None
    # Mais uma rodada nao manda de novo.
    await processar_pendentes(engine_rotinas, {"email": envio_email(mailpit)}, CONFIG)
    assert len(mailpit.com_assunto(assunto)) == 1


async def test_aviso_a_pessoa_usa_o_email_do_cadastro(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine, mailpit: Mailpit
) -> None:
    com_email = await cadastro_minimo(engine_dono, email=f"pessoa-{uuid.uuid4().hex[:6]}@exemplo.teste")
    sem_email = await cadastro_minimo(engine_dono)
    assunto = f"MCP-CLARO: aviso a pessoa {com_email.sufixo}"
    async with engine_rotinas.begin() as c:
        await avisar_pessoa(c, com_email.pessoa_id, "teste", assunto, {"texto": "Oi."}, f"p:{com_email.sufixo}")
        await avisar_pessoa(c, sem_email.pessoa_id, "teste", assunto, {"texto": "Oi."}, f"p:{sem_email.sufixo}")
    await processar_pendentes(engine_rotinas, {"email": envio_email(mailpit)}, CONFIG)
    (msg,) = mailpit.com_assunto(assunto)
    assert msg["To"][0]["Address"] == com_email.email
    # O endereco nao fica copiado na fila: so o identificador da pessoa.
    linha = await aviso(engine_rotinas, f"p:{com_email.sufixo}")
    assert linha.destino == f"pessoa:{com_email.pessoa_id}"
    assert com_email.email not in str(dict(linha._mapping))
    # Sem e-mail conhecido: desiste na hora (tentar de novo nao adianta) e vai ao extrato.
    sem = await aviso(engine_rotinas, f"p:{sem_email.sufixo}")
    assert sem.situacao == "desistiu"
    assert sem.tentativas == 1
    async with engine_rotinas.connect() as c:
        extrato = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE tipo = 'aviso_desistiu' AND dados ->> 'aviso_id' = :i"),
                {"i": str(sem.id)},
            )
        ).scalar_one()
    assert "desistimos" in extrato


async def test_nova_tentativa_depois_de_falha_forcada(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine, mailpit: Mailpit
) -> None:
    sufixo = uuid.uuid4().hex[:8]
    assunto = f"MCP-CLARO: falha forcada {sufixo}"
    chave = f"falha:{sufixo}"
    async with engine_rotinas.begin() as c:
        await enfileirar(c, "email", ADMIN, "teste", assunto, {"texto": "Vai falhar e depois chegar."}, chave)
    mailpit.caos(destinatario=100)
    try:
        await processar_pendentes(engine_rotinas, {"email": envio_email(mailpit)}, CONFIG)
    finally:
        mailpit.caos(destinatario=0)
    falhou = await aviso(engine_rotinas, chave)
    assert falhou.situacao == "pendente"
    assert falhou.tentativas == 1
    assert "451" in falhou.ultimo_erro
    assert ADMIN not in falhou.ultimo_erro  # o erro guardado nao repete o endereco
    async with engine_rotinas.connect() as c:
        espera = (
            await c.execute(
                text("SELECT extract(epoch FROM proxima_em - now()) FROM avisos WHERE id = :i"), {"i": falhou.id}
            )
        ).scalar_one()
    assert 20 < float(espera) <= 30  # primeira espera: NOTIFY_RETRY_BASE (30 s)
    assert mailpit.com_assunto(assunto) == []
    # Antes da hora, nada sai.
    await processar_pendentes(engine_rotinas, {"email": envio_email(mailpit)}, CONFIG)
    assert (await aviso(engine_rotinas, chave)).tentativas == 1
    # Chegou a hora: a nova tentativa entrega.
    async with engine_dono.begin() as c:
        await c.execute(text("UPDATE avisos SET proxima_em = now() WHERE id = :i"), {"i": falhou.id})
    await processar_pendentes(engine_rotinas, {"email": envio_email(mailpit)}, CONFIG)
    entregue = await aviso(engine_rotinas, chave)
    assert entregue.situacao == "enviado"
    assert entregue.tentativas == 2
    assert entregue.ultimo_erro is None
    assert len(mailpit.com_assunto(assunto)) == 1


async def test_desiste_depois_de_notify_retry_max(engine_rotinas: AsyncEngine, mailpit: Mailpit) -> None:
    sufixo = uuid.uuid4().hex[:8]
    chave = f"desiste:{sufixo}"
    async with engine_rotinas.begin() as c:
        await enfileirar(c, "email", ADMIN, "teste_desiste", f"MCP-CLARO: desiste {sufixo}", {"texto": "x"}, chave)
    mailpit.caos(destinatario=100)
    try:
        # Sem espera entre tentativas: a mesma rodada tenta 1 + NOTIFY_RETRY_MAX vezes e desiste.
        await processar_pendentes(
            engine_rotinas, {"email": envio_email(mailpit)}, ConfigEnvio(retry_max=2, espera_base=0, lote=500)
        )
    finally:
        mailpit.caos(destinatario=0)
    linha = await aviso(engine_rotinas, chave)
    assert linha.situacao == "desistiu"
    assert linha.tentativas == 3
    async with engine_rotinas.connect() as c:
        frase, dados = (
            await c.execute(
                text("SELECT frase, dados FROM extrato WHERE tipo = 'aviso_desistiu' AND dados ->> 'aviso_id' = :i"),
                {"i": str(linha.id)},
            )
        ).one()
    assert frase == "Aviso teste_desiste nao foi entregue depois de 3 tentativas; desistimos."
    assert dados["canal"] == "email"
    assert ADMIN not in str(dados)


@pytest.fixture
async def destino_https(tmp_path: Path) -> AsyncIterator[tuple[str, Any, list[dict[str, Any]]]]:
    """Servidor HTTPS local que guarda o que recebe. ``/ganchos`` responde o que ``respostas`` mandar."""
    recebidos: list[dict[str, Any]] = []
    respostas: list[int] = []

    async def ganchos(pedido: Request) -> Response:
        recebidos.append({"caminho": "/ganchos", "cabecalhos": dict(pedido.headers), "corpo": await pedido.body()})
        status = respostas.pop(0) if respostas else 200
        return JSONResponse({"ok": status < 300}, status_code=status)

    async def redireciona(pedido: Request) -> Response:
        recebidos.append({"caminho": "/redireciona", "cabecalhos": dict(pedido.headers), "corpo": await pedido.body()})
        return Response(status_code=307, headers={"Location": "/ganchos"})

    app = Starlette(
        routes=[Route("/ganchos", ganchos, methods=["POST"]), Route("/redireciona", redireciona, methods=["POST"])]
    )
    cert, chave = certificado_local(tmp_path)
    async with servidor_https(app, cert, chave) as url:
        yield url, (confia_em(cert), respostas), recebidos


async def _destino(engine_dono: AsyncEngine, url: str, evento: str, *, ativo: bool = True) -> uuid.UUID:
    async with engine_dono.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO destinos_webhook (nome, url, eventos, ativo, criado_por)"
                    " VALUES (:n, :u, ARRAY[CAST(:e AS text)], :a, 'teste') RETURNING id"
                ),
                {"n": f"destino-{uuid.uuid4().hex[:8]}", "u": url, "e": evento, "a": ativo},
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def test_webhook_assinado_chega_ao_servidor_local(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine, destino_https: tuple[str, Any, list[dict[str, Any]]]
) -> None:
    url, (confianca, _), recebidos = destino_https
    evento = f"teste_webhook_{uuid.uuid4().hex[:8]}"
    destino_id = await _destino(engine_dono, f"{url}/ganchos", evento)
    async with engine_rotinas.begin() as c:
        novos = await avisar_admin(
            c, evento, "Ferramenta suspensa", {"texto": "Ferramenta x suspensa.", "ferramenta": "x"}, evento, []
        )
    assert novos >= 1  # o nosso destino, e outros que assinem todos os eventos
    envio = EnvioWebhook(SecretStr(SEGREDO), verify=confianca)
    await processar_pendentes(engine_rotinas, {"webhook": envio}, CONFIG)
    (pedido,) = [r for r in recebidos if r["caminho"] == "/ganchos"]
    cabecalhos = pedido["cabecalhos"]
    assert cabecalhos["webhook-id"].startswith("msg_")
    assert cabecalhos["content-type"] == "application/json"
    carga = Webhook(SEGREDO).verify(pedido["corpo"], cabecalhos)
    assert carga["type"] == evento
    assert carga["data"] == {"texto": "Ferramenta x suspensa.", "ferramenta": "x"}
    linha = await aviso(engine_rotinas, f"{evento}|webhook:{destino_id}")
    assert linha.situacao == "enviado"


async def test_webhook_tenta_de_novo_com_o_mesmo_id_e_nao_segue_redirecionamento(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine, destino_https: tuple[str, Any, list[dict[str, Any]]]
) -> None:
    url, (confianca, respostas), recebidos = destino_https
    envio = EnvioWebhook(SecretStr(SEGREDO), verify=confianca)
    # Primeiro destino responde 500 e depois 200: o webhook-id e o mesmo nas duas tentativas.
    evento = f"teste_retry_{uuid.uuid4().hex[:8]}"
    destino_id = await _destino(engine_dono, f"{url}/ganchos", evento)
    respostas.append(500)
    async with engine_rotinas.begin() as c:
        await avisar_admin(c, evento, "Teste", {"texto": "Teste de nova tentativa."}, evento, [])
    await processar_pendentes(engine_rotinas, {"webhook": envio}, CONFIG)
    chave = f"{evento}|webhook:{destino_id}"
    primeira = await aviso(engine_rotinas, chave)
    assert primeira.situacao == "pendente"
    assert primeira.ultimo_erro == "webhook respondeu HTTP 500"
    async with engine_dono.begin() as c:
        await c.execute(text("UPDATE avisos SET proxima_em = now() WHERE id = :i"), {"i": primeira.id})
    await processar_pendentes(engine_rotinas, {"webhook": envio}, CONFIG)
    assert (await aviso(engine_rotinas, chave)).situacao == "enviado"
    ids = [r["cabecalhos"]["webhook-id"] for r in recebidos if r["caminho"] == "/ganchos"]
    assert len(ids) == 2
    assert ids[0] == ids[1]
    for r in recebidos:
        Webhook(SEGREDO).verify(r["corpo"], r["cabecalhos"])

    # Destino que redireciona: o envio nao segue, conta como falha e /ganchos nao recebe nada novo.
    recebidos.clear()
    evento2 = f"teste_redir_{uuid.uuid4().hex[:8]}"
    redir_id = await _destino(engine_dono, f"{url}/redireciona", evento2)
    async with engine_rotinas.begin() as c:
        await avisar_admin(c, evento2, "Teste", {"texto": "Teste de redirecionamento."}, evento2, [])
    await processar_pendentes(engine_rotinas, {"webhook": envio}, CONFIG)
    redir = await aviso(engine_rotinas, f"{evento2}|webhook:{redir_id}")
    assert redir.situacao == "pendente"
    assert redir.ultimo_erro == "webhook respondeu HTTP 307"
    assert [r["caminho"] for r in recebidos] == ["/redireciona"]


async def test_webhook_de_destino_desativado_nao_sai(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine, destino_https: tuple[str, Any, list[dict[str, Any]]]
) -> None:
    url, (confianca, _), recebidos = destino_https
    evento = f"teste_desativado_{uuid.uuid4().hex[:8]}"
    destino_id = await _destino(engine_dono, f"{url}/ganchos", evento)
    async with engine_rotinas.begin() as c:
        await avisar_admin(c, evento, "Teste", {"texto": "Nao deve sair."}, evento, [])
    async with engine_dono.begin() as c:
        await c.execute(text("UPDATE destinos_webhook SET ativo = false WHERE id = :i"), {"i": destino_id})
    await processar_pendentes(engine_rotinas, {"webhook": EnvioWebhook(SecretStr(SEGREDO), verify=confianca)}, CONFIG)
    linha = await aviso(engine_rotinas, f"{evento}|webhook:{destino_id}")
    assert linha.situacao == "desistiu"
    assert recebidos == []


async def test_entrada_e_despachante_podem_enfileirar(engine_dono: AsyncEngine) -> None:
    """Com so INSERT em ``avisos``, a entrada e o despachante enfileiram (ON CONFLICT sem alvo)."""
    sufixo = uuid.uuid4().hex[:8]
    for papel in ("mcp_claro_entrada", "mcp_claro_despachante"):
        async with engine_dono.begin() as c:
            await c.execute(text(f"SET LOCAL ROLE {papel}"))
            assert await enfileirar(c, "email", ADMIN, "teste", "x", {"texto": "x"}, f"{papel}:{sufixo}")
            assert not await enfileirar(c, "email", ADMIN, "teste", "x", {"texto": "x"}, f"{papel}:{sufixo}")


async def test_aviso_com_defeito_nao_trava_a_fila(engine_rotinas: AsyncEngine) -> None:
    """Um erro inesperado no canal vira falha comum: o aviso e reagendado e os outros saem."""
    sufixo = uuid.uuid4().hex[:8]
    enviados: list[int] = []

    class CanalCaprichoso:
        async def enviar(self, conexao: Any, a: Any) -> None:
            if a.assunto.endswith("defeito"):
                await conexao.execute(text("SELECT 1 / 0"))  # erro de banco dentro do envio
            enviados.append(a.id)

    async with engine_rotinas.begin() as c:
        await enfileirar(c, "webhook", "https://x.exemplo", "teste", f"{sufixo} defeito", {"texto": "x"}, f"d:{sufixo}")
        await enfileirar(c, "webhook", "https://x.exemplo", "teste", f"{sufixo} bom", {"texto": "x"}, f"b:{sufixo}")
    await processar_pendentes(engine_rotinas, {"webhook": CanalCaprichoso()}, CONFIG)
    defeito = await aviso(engine_rotinas, f"d:{sufixo}")
    bom = await aviso(engine_rotinas, f"b:{sufixo}")
    assert defeito.situacao == "pendente"
    assert defeito.tentativas == 1
    assert defeito.ultimo_erro == "erro inesperado no envio (DBAPIError)"
    assert bom.situacao == "enviado"
    assert bom.id in enviados
