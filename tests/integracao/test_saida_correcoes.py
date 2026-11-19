"""Correcoes da revisao da porta de saida (nucleo), contra o PostgreSQL real com o usuario de cada papel.

Cada teste falha sem a correcao: impasse entre vencer fichas e bloquear (ou restaurar), limite sem passkey,
cadastro do programa, pedido igual pendente, saida fechada, teto de fichas (reativar e pedidos ao mesmo
tempo), protocolo depois da restauracao, comprovante em tres partes, espera com a porta fechada, envio de
outro despachante na restauracao, virada do dia no limite, fim do envio que falha ao gravar, limite
encerrado, guarda das fichas e da fila no banco, indices, limpeza dos desafios de passkey e recusa de acao
no extrato.

Mesmo arranjo de ``test_saida_disputas``: o app do despachante sobe sem a porta, e cada teste usa
``PortaDeSaida`` direto, para escolher o momento de cada passo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import Aplicativo
from mcp_claro.regras import bloqueios, estado
from mcp_claro.rotinas import acoes
from mcp_claro.saida import despachante as despachante_mod
from mcp_claro.saida import fila, travas
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import Operacao
from mcp_claro.saida.pedidos import AcaoRecusada, Pedido, Pedidos
from mcp_claro.saida.protocolo import codigo_conferencia, novo_protocolo, protocolo_valido
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import chamadas, chamar, operacao, politica
from tests.integracao.test_saida_disputas import amb, bloquear, desbloquear, nova_acao, porta, vitrine

__all__ = ["amb"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

RECUSA_CHECK = "23514"
SEM_PERMISSAO = "42501"


# --- Ajudas ----------------------------------------------------------------------------------


def sqlstate(erro: DBAPIError) -> str | None:
    return getattr(erro.orig, "sqlstate", None)


async def recusa_do_banco(engine: Any, sql: str, **params: Any) -> str | None:
    """Roda o comando como o papel da engine. Devolve ``None`` se passou, ou o SQLSTATE da recusa."""
    try:
        async with engine.begin() as c:
            await c.execute(text(sql), params)
    except DBAPIError as erro:
        return sqlstate(erro)
    return None


async def pedidos_de(amb: Ambiente) -> tuple[Pedidos, Aplicativo]:
    async with amb.entrada_db.connect() as c:
        aplicativo = await repositorio.aplicativo_por_client_id(c, "claude-code")
    assert aplicativo is not None
    return Pedidos(amb.p, amb.entrada_db), aplicativo


async def sem_limites(amb: Ambiente) -> None:
    """Os limites de outros testes nao entram: so vale o que o teste criar."""
    async with amb.admin.begin() as c:
        await c.execute(
            text("UPDATE limites SET situacao = 'encerrado', motivo = 'teste' WHERE situacao = 'ativo'"),
        )


async def novo_limite(
    amb: Ambiente, *, nome: str = "enviar_mensagem", com_passkey: bool = True, por_dia: int = 5
) -> uuid.UUID:
    async with amb.entrada_db.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate,"
                    " com_passkey) VALUES (:p, :a, :s, :n, :d, now() + interval '1 day', :pk) RETURNING id"
                ),
                {
                    "p": amb.ana_id,
                    "a": amb.aplicativo_id,
                    "s": amb.sistema_id,
                    "n": nome,
                    "d": por_dia,
                    "pk": com_passkey,
                },
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


@pytest.fixture(autouse=True)
async def _sem_limites_depois(amb: Ambiente) -> AsyncIterator[None]:
    """Limite criado aqui nao fica valendo para os testes seguintes (o banco e o mesmo)."""
    yield
    await sem_limites(amb)


async def usados(amb: Ambiente, limite_id: uuid.UUID) -> int:
    async with amb.admin.connect() as c:
        return int(
            (await c.execute(text("SELECT usados_hoje FROM limites WHERE id = :i"), {"i": limite_id})).scalar_one()
        )


async def mudar_estado(amb: Ambiente, op: Operacao, *estados: Estado) -> None:
    """Leva a linha pelos estados dados, como o dono do banco (passa pelo gatilho de transicoes)."""
    async with amb.admin.begin() as c:
        for e in estados:
            await c.execute(text("UPDATE fila_saida SET estado = :e WHERE id = :i"), {"e": str(e), "i": op.fila_id})


async def extrato(amb: Ambiente, tipo: str, **filtro: Any) -> list[Any]:
    onde = "".join(f" AND {k} = :{k}" for k in filtro)
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(text(f"SELECT * FROM extrato WHERE tipo = :t{onde} ORDER BY id"), {"t": tipo, **filtro})  # noqa: S608
            ).all()
        )


@contextlib.asynccontextmanager
async def saida_fechada(amb: Ambiente) -> AsyncIterator[None]:
    quem = bloqueios.quem_admin(amb.admin_id)
    async with amb.admin.begin() as c:
        await estado.mudar_saida(c, aberta=False, quem=quem, motivo="teste da saida fechada", papel="entrada")
    try:
        yield
    finally:
        async with amb.admin.begin() as c:
            await estado.mudar_saida(c, aberta=True, quem=quem, motivo="fim do teste", papel="entrada")


async def pausar_no_primeiro_vencimento(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Faz ``vencer_fichas`` parar (segurando as travas) logo depois de vencer as linhas da fila."""
    original = fila.registrar
    parou = asyncio.Event()
    segue = asyncio.Event()

    async def registrar_com_pausa(c: Any, **kw: Any) -> int:
        if kw.get("tipo") == "ficha_vencida" and not parou.is_set():
            parou.set()
            await segue.wait()
        return await original(c, **kw)

    monkeypatch.setattr(fila, "registrar", registrar_com_pausa)
    return parou, segue


async def duas_fichas_vencidas_fora_de_ordem(amb: Ambiente) -> tuple[Operacao, Operacao]:
    """A foi criada antes, mas B vence antes (ficha de assistente perto de ficha de programa)."""
    a = await nova_acao(amb, f"Impasse A {uuid.uuid4().hex[:6]}", confirmada=False)
    b = await nova_acao(amb, f"Impasse B {uuid.uuid4().hex[:6]}", confirmada=False)
    async with amb.admin.begin() as c:
        await c.execute(
            text("UPDATE fichas SET expira_em = now() - interval '1 minute' WHERE id = :f"), {"f": a.ficha_id}
        )
        await c.execute(
            text("UPDATE fichas SET expira_em = now() - interval '9 years' WHERE id = :f"), {"f": b.ficha_id}
        )
    return a, b


# --- [2, 43] Impasse entre vencer fichas e bloquear ou restaurar ------------------------------


async def test_vencer_fichas_e_bloquear_tudo_ao_mesmo_tempo_sem_impasse(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await duas_fichas_vencidas_fora_de_ordem(amb)
    parou, segue = await pausar_no_primeiro_vencimento(monkeypatch)

    async def vencer() -> list[str]:
        async with amb.rotinas_db.begin() as c:
            return await fila.vencer_fichas(c)

    tv = asyncio.create_task(vencer())
    await asyncio.wait_for(parou.wait(), 10)
    tb = asyncio.create_task(bloquear(amb, "tudo"))
    await asyncio.sleep(1.0)
    assert not tb.done(), "o bloqueio de tudo espera a rotina soltar a trava compartilhada"
    segue.set()
    vencidas, b_tudo = await asyncio.wait_for(asyncio.gather(tv, tb, return_exceptions=True), 20)
    try:
        assert not isinstance(vencidas, BaseException), vencidas
        assert not isinstance(b_tudo, BaseException), b_tudo
        assert {a.protocolo, b.protocolo} <= set(vencidas)
        assert (await operacao(amb, a.protocolo)).estado == Estado.VENCIDA
        assert (await operacao(amb, b.protocolo)).estado == Estado.VENCIDA
    finally:
        if isinstance(b_tudo, bloqueios.Bloqueio):
            await desbloquear(amb, b_tudo)


async def test_vencer_fichas_e_restauracao_ao_mesmo_tempo_sem_impasse(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await duas_fichas_vencidas_fora_de_ordem(amb)
    parou, segue = await pausar_no_primeiro_vencimento(monkeypatch)

    async def vencer() -> list[str]:
        async with amb.rotinas_db.begin() as c:
            return await fila.vencer_fichas(c)

    async def restaurar() -> tuple[int, int]:
        async with amb.despachante_db.begin() as c:
            return await fila.parar_por_restauracao(c)

    tv = asyncio.create_task(vencer())
    await asyncio.wait_for(parou.wait(), 10)
    tr = asyncio.create_task(restaurar())
    await asyncio.sleep(1.0)
    assert not tr.done(), "a restauracao espera a rotina soltar a trava compartilhada de tudo"
    segue.set()
    vencidas, restauracao = await asyncio.wait_for(asyncio.gather(tv, tr, return_exceptions=True), 20)
    assert not isinstance(vencidas, BaseException), vencidas
    assert not isinstance(restauracao, BaseException), restauracao
    assert {a.protocolo, b.protocolo} <= set(vencidas)


# --- [3, 8, 12] Limite sem passkey com a passkey ligada ------------------------------------------


async def test_limite_sem_passkey_nao_e_gasto_com_a_passkey_ligada(amb: Ambiente) -> None:
    await sem_limites(amb)
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    limite = await novo_limite(amb, com_passkey=False)
    pedidos, aplicativo = await pedidos_de(amb)
    f = await vitrine(amb, "enviar_mensagem")
    assert amb.p.FEATURE_PASSKEY is True
    op, _ = await pedidos.pedir(
        Pedido(amb.ana_id, aplicativo, f, {"destinatario": "equipe", "texto": "limite sem passkey"}, None)
    )
    # Vai para a ficha, sem gastar o limite (antes: liberada pelo limite, gasta, e cancelada no despachante).
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.limite_id is None
    assert await usados(amb, limite) == 0
    # Com a passkey desligada na instalacao, o mesmo limite vale.
    async with amb.entrada_db.begin() as c:
        gasto = await fila.gastar_limite(
            c,
            pessoa_id=amb.ana_id,
            aplicativo_id=amb.aplicativo_id,
            sistema_id=amb.sistema_id,
            nome_original="enviar_mensagem",
            argumentos={"destinatario": "equipe", "texto": "sem passkey"},
            exige_passkey=False,
        )
    assert gasto == limite


# --- [4] Cadastro do programa reconferido no despachante ---------------------------------------


async def acao_de_programa(amb: Ambiente, aplicativo_id: uuid.UUID, titulo: str) -> str:
    """Uma acao liberada de um programa, gravada direto no banco (a ordem e a ferramenta sao as do arranjo)."""
    f = await vitrine(amb)
    argumentos = {"titulo": titulo}
    codigo = codigo_conferencia(amb.sistema_id, "criar_chamado", argumentos)
    async with amb.admin.begin() as c:
        protocolo = await novo_protocolo(c)
        op_id = (
            await c.execute(
                text(
                    "INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
                    " ferramenta_id, sistema_id, argumentos, chave_pedido_igual)"
                    " VALUES (:pr, :co, :pe, :a, :o, :f, :s, CAST(:ar AS jsonb), :k) RETURNING id"
                ),
                {
                    "pr": protocolo,
                    "co": codigo,
                    "pe": amb.ana_id,
                    "a": aplicativo_id,
                    "o": f.ordem_id,
                    "f": f.id,
                    "s": amb.sistema_id,
                    "ar": json.dumps(argumentos),
                    "k": uuid.uuid4().hex,
                },
            )
        ).scalar_one()
        ficha = (
            await c.execute(
                text(
                    "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco)"
                    " VALUES (:o, :co, now() + interval '1 day', 'medio') RETURNING id"
                ),
                {"o": op_id, "co": codigo},
            )
        ).scalar_one()
        await c.execute(
            text("INSERT INTO fila_saida (operacao_id, estado, ficha_id) VALUES (:o, 'aguardando_ficha', :f)"),
            {"o": op_id, "f": ficha},
        )
        await c.execute(
            text(
                "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), decidida_por = :p,"
                " confirmada_com = 'sessao' WHERE id = :f"
            ),
            {"f": ficha, "p": amb.ana_id},
        )
        await c.execute(
            text("UPDATE fila_saida SET estado = 'liberada', hora_saida = now() WHERE operacao_id = :o"), {"o": op_id}
        )
    return protocolo


async def test_despachante_cancela_acao_de_programa_pausado_encerrado_ou_vencido(amb: Ambiente) -> None:
    sufixo = uuid.uuid4().hex[:8]
    async with amb.admin.begin() as c:
        robo = await repositorio.habilitar_aplicativo(
            c, client_id=f"robo-{sufixo}", nome=f"Robo {sufixo}", familia="teste", tipo="programa"
        )
        await c.execute(
            text(
                "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim, situacao)"
                " VALUES (:a, :r, :s, 'teste', current_date + 30, 'pausado')"
            ),
            {"a": robo, "r": amb.ana_id, "s": amb.bia_id},
        )
    p = porta(amb, acoes_de_programas=True)
    motivo_cadastro = "o cadastro do programa nao esta valendo (pausado, encerrado ou vencido)"

    pausado = await acao_de_programa(amb, robo, "programa pausado")
    assert await p.enviar_uma() is True
    atual = await operacao(amb, pausado)
    assert (atual.estado, atual.desfecho) == (Estado.CANCELADA, motivo_cadastro)

    for mudanca in ("situacao = 'encerrado'", "situacao = 'ativo', fim = current_date - 2"):
        async with amb.admin.begin() as c:
            await c.execute(text(f"UPDATE programas SET {mudanca} WHERE aplicativo_id = :a"), {"a": robo})  # noqa: S608
        protocolo = await acao_de_programa(amb, robo, f"programa {mudanca}")
        assert await p.enviar_uma() is True
        atual = await operacao(amb, protocolo)
        assert (atual.estado, atual.desfecho) == (Estado.CANCELADA, motivo_cadastro), mudanca

    # Cadastro valendo: passa desta conferencia (e para na seguinte, a ordem, que e de outro aplicativo).
    async with amb.admin.begin() as c:
        await c.execute(text("UPDATE programas SET fim = current_date + 30 WHERE aplicativo_id = :a"), {"a": robo})
    valendo = await acao_de_programa(amb, robo, "programa valendo")
    assert await p.enviar_uma() is True
    atual = await operacao(amb, valendo)
    assert (atual.estado, atual.desfecho) == (Estado.CANCELADA, "a ordem de servico ou a ferramenta mudou")
    assert chamadas(amb, "criar_chamado") == 0


# --- [10] Pedido igual ainda pendente conta em qualquer idade ----------------------------------


async def test_pedido_igual_pendente_conta_fora_da_janela_e_a_janela_vale_depois_de_sair(amb: Ambiente) -> None:
    pedidos, aplicativo = await pedidos_de(amb)
    f = await vitrine(amb)
    pedido = Pedido(amb.ana_id, aplicativo, f, {"titulo": f"Igual {uuid.uuid4().hex[:6]}"}, None)
    original, repetida = await pedidos.pedir(pedido)
    assert (original.estado, repetida) == (Estado.AGUARDANDO_FICHA, False)
    async with amb.admin.begin() as c:  # pedido feito ha dois dias, ficha ainda esperando (como a de um programa)
        await c.execute(
            text("UPDATE operacoes SET criada_em = now() - interval '2 days' WHERE id = :o"), {"o": original.id}
        )
    de_novo, repetida = await pedidos.pedir(pedido)
    assert repetida is True
    assert de_novo.protocolo == original.protocolo

    # Liberada (espera longa) e ainda velha: continua a mesma.
    assert original.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        await fila.confirmar_ficha(
            c, original.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=3600, rapida_segundos=0
        )
    de_novo, repetida = await pedidos.pedir(pedido)
    assert (de_novo.protocolo, repetida) == (original.protocolo, True)

    # Saiu agora: a janela conta da saida, nao do pedido de dois dias atras.
    await mudar_estado(amb, original, Estado.ENVIANDO, Estado.ENVIADA)
    de_novo, repetida = await pedidos.pedir(pedido)
    assert (de_novo.protocolo, repetida) == (original.protocolo, True)

    # Saiu ha mais que a janela: e um pedido novo.
    async with amb.admin.begin() as c:
        await c.execute(text("SET LOCAL session_replication_role = replica"))  # so para envelhecer a linha final
        await c.execute(
            text("UPDATE fila_saida SET mudou_em = now() - interval '2 days' WHERE id = :i"), {"i": original.fila_id}
        )
    novo, repetida = await pedidos.pedir(pedido)
    assert repetida is False
    assert novo.protocolo != original.protocolo


# --- [11, 37] Saida fechada recusa a acao antes do protocolo; recusa de acao no extrato ----------


async def test_saida_fechada_recusa_a_acao_antes_do_protocolo_sem_gastar_limite(amb: Ambiente) -> None:
    await sem_limites(amb)
    limite = await novo_limite(amb)
    pedidos, aplicativo = await pedidos_de(amb)
    f = await vitrine(amb, "enviar_mensagem")
    titulo = f"saida fechada {uuid.uuid4().hex[:6]}"
    async with saida_fechada(amb):
        with pytest.raises(AcaoRecusada) as erro:
            await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, {"destinatario": "equipe", "texto": titulo}, None))
        assert erro.value.motivo == "saida_fechada"
        assert erro.value.mensagem == estado.MENSAGEM_SAIDA_FECHADA
        assert await usados(amb, limite) == 0

        # Pelo aplicativo: recusa legivel, e o extrato diz que foi uma acao recusada.
        with pytest.raises(MCPError) as recusa:
            await chamar(amb, "criar_chamado", {"titulo": titulo})
        assert "porta de saida do MCP-CLARO esta fechada" in str(recusa.value)
    async with amb.admin.connect() as c:
        criadas = (
            await c.execute(
                text(
                    "SELECT count(*) FROM operacoes WHERE argumentos ->> 'titulo' = :t OR argumentos ->> 'texto' = :t"
                ),
                {"t": titulo},
            )
        ).scalar_one()
    assert criadas == 0
    linhas = [x for x in await extrato(amb, "acao_recusada", pessoa_id=amb.ana_id) if "saida_fechada" in x.frase]
    assert linhas
    assert (
        linhas[-1].frase == f"Acao em {amb.vitrine['criar_chamado']} recusada antes de virar protocolo: saida_fechada."
    )
    assert not [x for x in await extrato(amb, "leitura_recusada", pessoa_id=amb.ana_id) if "saida_fechada" in x.frase]


async def test_acao_recusada_por_bloqueio_vai_ao_extrato_como_acao(amb: Ambiente) -> None:
    b = await bloquear(amb, "pessoa")
    try:
        with pytest.raises(MCPError):
            await chamar(amb, "criar_chamado", {"titulo": "bloqueada"})
    finally:
        await desbloquear(amb, b)
    frase = f"Acao em {amb.vitrine['criar_chamado']} recusada antes de virar protocolo: bloqueio_pessoa."
    assert frase in [x.frase for x in await extrato(amb, "acao_recusada", pessoa_id=amb.ana_id)]


# --- [13] Reativar depois do teto vale --------------------------------------------------------------


async def test_reativar_depois_do_teto_nao_pausa_de_novo(amb: Ambiente) -> None:
    pedidos, aplicativo = await pedidos_de(amb)
    async with amb.admin.connect() as c:
        ja = await fila.fichas_na_ultima_hora(c, amb.aplicativo_id)
    await politica(amb, CONFIRM_CAP_PER_HOUR=ja + 2)
    f = await vitrine(amb)
    for n in range(2):
        await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, {"titulo": f"Teto {n} {uuid.uuid4().hex[:6]}"}, None))
    with pytest.raises(AcaoRecusada) as erro:
        await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, {"titulo": "Teto demais"}, None))
    assert erro.value.motivo == "teto_confirmacoes"
    async with amb.admin.connect() as c:
        pausa = (
            await c.execute(
                text(
                    "SELECT id FROM bloqueios WHERE aplicativo_id = :a AND encerrado_em IS NULL AND quem = :q"
                    " AND alcance = 'aplicativo'"
                ),
                {"a": amb.aplicativo_id, "q": fila.QUEM_TETO},
            )
        ).scalar_one()
    async with amb.entrada_db.begin() as c:
        await bloqueios.encerrar(c, pausa, bloqueios.quem_admin(amb.admin_id))
    # Depois de o ADMIN reativar, o proximo pedido de outra pessoa passa (antes: pausava de novo).
    f_bia = await vitrine(amb, pessoa_id=amb.bia_id)
    op, _ = await pedidos.pedir(Pedido(amb.bia_id, aplicativo, f_bia, {"titulo": "Depois de reativar"}, None))
    assert op.estado == Estado.AGUARDANDO_FICHA
    async with amb.admin.connect() as c:
        ativas = (
            await c.execute(
                text("SELECT count(*) FROM bloqueios WHERE aplicativo_id = :a AND encerrado_em IS NULL"),
                {"a": amb.aplicativo_id},
            )
        ).scalar_one()
    assert ativas == 0


# --- [28, 38] Teto de fichas atomico com pedidos ao mesmo tempo --------------------------------


async def test_teto_de_fichas_nao_passa_com_pedidos_ao_mesmo_tempo(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    pedidos, aplicativo = await pedidos_de(amb)
    async with amb.admin.connect() as c:
        ja = await fila.fichas_na_ultima_hora(c, amb.aplicativo_id)
    await politica(amb, CONFIRM_CAP_PER_HOUR=ja + 1)
    original = fila.fichas_na_ultima_hora

    async def contagem_lenta(c: Any, aplicativo_id: uuid.UUID) -> int:
        # Conta e demora a montar a ficha: sem a trava do teto, os outros contam o mesmo numero.
        n = await original(c, aplicativo_id)
        await asyncio.sleep(0.4)
        return n

    monkeypatch.setattr(fila, "fichas_na_ultima_hora", contagem_lenta)
    f = await vitrine(amb)
    marca = uuid.uuid4().hex[:6]
    lote = [Pedido(amb.ana_id, aplicativo, f, {"titulo": f"Ao mesmo tempo {marca} {n}"}, None) for n in range(3)]
    resultados = await asyncio.wait_for(asyncio.gather(*(pedidos.pedir(p) for p in lote), return_exceptions=True), 30)
    recusas = [r for r in resultados if isinstance(r, AcaoRecusada)]
    assert len(recusas) == 2, resultados
    assert all(r.motivo == "teto_confirmacoes" for r in recusas)
    async with amb.admin.connect() as c:
        criadas = (
            await c.execute(
                text("SELECT count(*) FROM operacoes WHERE argumentos ->> 'titulo' LIKE :t"),
                {"t": f"Ao mesmo tempo {marca} %"},
            )
        ).scalar_one()
    assert criadas == 1


# --- [15] Protocolo nao se repete depois da restauracao ----------------------------------------


def numero(protocolo: str) -> int:
    return int(protocolo.rsplit("-", 1)[1])


async def test_restauracao_adianta_o_protocolo(amb: Ambiente) -> None:
    async with amb.entrada_db.begin() as c:
        dado_antes = await novo_protocolo(c)
    # A copia volta a sequencia para antes do ultimo numero dado (como num pg_dump ou recuperacao no tempo).
    async with amb.admin.begin() as c:
        await c.execute(text("SELECT setval('protocolo_seq', :n)"), {"n": numero(dado_antes) - 5})
    async with amb.despachante_db.begin() as c:
        await fila.parar_por_restauracao(c)
    async with amb.entrada_db.begin() as c:
        depois = await novo_protocolo(c)
    assert numero(depois) > numero(dado_antes)
    assert numero(depois) >= numero(dado_antes) - 5 + fila.SALTO_NA_RESTAURACAO
    assert protocolo_valido(depois)


# --- [24] Comprovante em tres partes ---------------------------------------------------------------


async def test_comprovante_do_bloqueio_em_tres_partes(amb: Ambiente) -> None:
    marca = uuid.uuid4().hex[:6]
    esperando = await nova_acao(amb, f"Comprovante esperando {marca}", espera=3600)
    enviando = await nova_acao(amb, f"Comprovante enviando {marca}")
    enviada = await nova_acao(amb, f"Comprovante enviada {marca}")
    pediu = await nova_acao(amb, f"Comprovante pediu dados {marca}")
    sem_decisao = await nova_acao(amb, f"Comprovante sem decisao {marca}")
    aconteceu = await nova_acao(amb, f"Comprovante aconteceu {marca}")
    nao_aconteceu = await nova_acao(amb, f"Comprovante nao aconteceu {marca}")
    nao_sei = await nova_acao(amb, f"Comprovante nao sei {marca}")
    await mudar_estado(amb, enviando, Estado.ENVIANDO)
    await mudar_estado(amb, enviada, Estado.ENVIANDO, Estado.ENVIADA)
    await mudar_estado(amb, pediu, Estado.ENVIANDO, Estado.SISTEMA_PEDIU_DADOS)
    for op in (sem_decisao, aconteceu, nao_aconteceu, nao_sei):
        await mudar_estado(amb, op, Estado.ENVIANDO, Estado.NAO_CONFIRMADA)
    for op, decisao in ((aconteceu, "aconteceu"), (nao_aconteceu, "nao_aconteceu"), (nao_sei, "nao_sei")):
        async with amb.entrada_db.begin() as c:
            await fila.decidir_nao_confirmada(c, op.id, pessoa_id=amb.ana_id, decisao=decisao, observacao=None)
    meus = {
        op.protocolo for op in (esperando, enviando, enviada, pediu, sem_decisao, aconteceu, nao_aconteceu, nao_sei)
    }

    b = await bloquear(amb, "pessoa")
    try:
        async with amb.entrada_db.connect() as c:
            comp = await fila.comprovante_do_bloqueio(c, b.id)
        assert comp is not None
        paradas = {x["protocolo"]: x["estava"] for x in comp.paradas if x["protocolo"] in meus}
        ja_saidas = {x["protocolo"]: x["estado"] for x in comp.ja_saidas if x["protocolo"] in meus}
        conferindo = {x["protocolo"]: x["estado"] for x in comp.em_conferencia if x["protocolo"] in meus}
        assert paradas == {esperando.protocolo: "liberada"}
        assert ja_saidas == {
            enviada.protocolo: "enviada",
            pediu.protocolo: "sistema_pediu_dados",
            aconteceu.protocolo: "nao_confirmada",
        }
        assert conferindo == {
            enviando.protocolo: "enviando",
            sem_decisao.protocolo: "nao_confirmada",
            nao_sei.protocolo: "nao_confirmada",
        }
        assert nao_aconteceu.protocolo not in {**paradas, **ja_saidas, **conferindo}
        assert comp.informado == []
        assert all(x["desde"] for x in comp.em_conferencia)
        linha = (await extrato(amb, "comprovante_bloqueio_partes", pessoa_id=amb.ana_id))[-1]
        assert linha.frase == (
            f"Comprovante do bloqueio: {len(comp.paradas)} acoes paradas antes de sair; {len(comp.ja_saidas)} ja"
            f" tinham saido; {len(comp.em_conferencia)} ainda em conferencia."
        )
    finally:
        await desbloquear(amb, b)


# --- [25] Porta de saida fechada: o despachante espera a varredura ------------------------------


async def test_porta_fechada_o_despachante_espera_a_varredura(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Na hora, com a porta fechada")
    assert op.estado == Estado.LIBERADA
    p = PortaDeSaida(amb.despachante_db, amb.cifra, varredura=0.8, prazo_envio=5.0)
    async with saida_fechada(amb):
        assert await p.enviar_uma() is False
        inicio = time.monotonic()
        await p._esperar()
        assert time.monotonic() - inicio >= 0.6, "com a porta fechada, a espera e a varredura (nao 50 ms)"
    assert (await operacao(amb, op.protocolo)).estado == Estado.LIBERADA
    assert chamadas(amb, "criar_chamado") == 0


# --- [26] Restauracao nao mexe no envio de um despachante vivo -----------------------------------


async def test_restauracao_deixa_o_envio_do_despachante_vivo(amb: Ambiente) -> None:
    op = await nova_acao(amb, f"Envio do dono vivo {uuid.uuid4().hex[:6]}", espera=3600)
    await mudar_estado(amb, op, Estado.ENVIANDO)
    async with amb.admin.connect() as dono:
        assert (await dono.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": travas.DESPACHANTE_UNICO})).scalar()
        try:
            async with amb.despachante_db.begin() as c:
                await fila.parar_por_restauracao(c)
            assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIANDO
            frases = [x.frase for x in await extrato(amb, "fila_restauracao_envio_com_dono")]
            assert frases
            assert frases[-1].endswith("ele mesmo grava o fim delas.")
        finally:
            await dono.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": travas.DESPACHANTE_UNICO})
            await dono.commit()
    # Sem dono vivo, o "enviando" que sobrou fica nao confirmado.
    async with amb.despachante_db.begin() as c:
        await fila.parar_por_restauracao(c)
    assert (await operacao(amb, op.protocolo)).estado == Estado.NAO_CONFIRMADA


# --- [27] Virada do dia no gasto do limite -----------------------------------------------------------

# Dois fusos com 26 h de diferenca: o "hoje" de um e o "ontem" do outro, no mesmo instante.
DEPOIS = "Etc/GMT-14"
ANTES = "Etc/GMT+12"


async def test_virada_do_dia_nao_volta_o_dia_nem_passa_do_teto(amb: Ambiente, monkeypatch: pytest.MonkeyPatch) -> None:
    async with amb.admin.begin() as c:
        ontem = (await c.execute(text("SELECT (now() AT TIME ZONE :f)::date"), {"f": ANTES})).scalar_one()
        hoje = (await c.execute(text("SELECT (now() AT TIME ZONE :f)::date"), {"f": DEPOIS})).scalar_one()
        assert hoje != ontem
        limite = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate,"
                    " com_passkey, usados_hoje, dia) VALUES (:p, :a, :s, 'virada_teste', 2, now() + interval '2 day',"
                    " true, 2, :d) RETURNING id"
                ),
                {"p": amb.ana_id, "a": amb.aplicativo_id, "s": amb.sistema_id, "d": ontem},
            )
        ).scalar_one()

    async def gastar(fuso: str) -> bool:
        monkeypatch.setattr(fila, "FUSO", fuso)
        async with amb.entrada_db.begin() as c:
            gasto = await fila.gastar_limite(
                c,
                pessoa_id=amb.ana_id,
                aplicativo_id=amb.aplicativo_id,
                sistema_id=amb.sistema_id,
                nome_original="virada_teste",
                argumentos={},
            )
        return gasto is not None

    assert await gastar(DEPOIS) is True  # primeiro pedido do dia novo
    assert await gastar(ANTES) is True  # comecou antes da meia-noite: conta no dia novo, que ainda tem saldo
    async with amb.admin.connect() as c:
        linha = (await c.execute(text("SELECT dia, usados_hoje FROM limites WHERE id = :i"), {"i": limite})).one()
    assert (linha.dia, linha.usados_hoje) == (hoje, 2), "o dia gravado nunca volta"
    # No dia novo, nunca mais que o teto (antes: o dia voltava, e cabiam mais dois).
    assert [await gastar(DEPOIS) for _ in range(3)] == [False, False, False]
    assert await gastar(ANTES) is False


# --- [29] Fim do envio que falha ao gravar --------------------------------------------------------


def falha_do_banco() -> OperationalError:
    return OperationalError("UPDATE fila_saida", {}, OSError("conexao caiu"))


async def test_fim_do_envio_tenta_de_novo_quando_o_banco_falha(amb: Ambiente, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(despachante_mod, "ESPERA_DO_FIM", 0.01)
    op = await nova_acao(amb, f"Fim com soluco {uuid.uuid4().hex[:6]}")
    antes = chamadas(amb, "criar_chamado")
    original = fila.finalizar
    falhas: list[int] = []

    async def instavel(c: Any, *args: Any, **kwargs: Any) -> bool:
        if len(falhas) < 2:
            falhas.append(1)
            raise falha_do_banco()
        return await original(c, *args, **kwargs)

    monkeypatch.setattr(fila, "finalizar", instavel)
    assert await porta(amb).enviar_uma() is True
    atual = await operacao(amb, op.protocolo)
    assert atual.estado == Estado.ENVIADA
    assert atual.desfecho == "o sistema respondeu"
    assert len(falhas) == 2
    assert chamadas(amb, "criar_chamado") == antes + 1


async def test_fim_que_nao_grava_deixa_enviando_e_a_retomada_decide(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(despachante_mod, "ESPERA_DO_FIM", 0.01)
    op = await nova_acao(amb, f"Fim que nao grava {uuid.uuid4().hex[:6]}")
    antes = chamadas(amb, "criar_chamado")
    original = fila.finalizar
    tentativas: list[int] = []

    async def sempre_falha(*_: Any, **__: Any) -> bool:
        tentativas.append(1)
        raise falha_do_banco()

    monkeypatch.setattr(fila, "finalizar", sempre_falha)
    p = porta(amb)
    assert await p.enviar_uma() is True  # nao derruba a porta
    assert len(tentativas) == despachante_mod.TENTATIVAS_DO_FIM
    assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIANDO
    assert chamadas(amb, "criar_chamado") == antes + 1
    monkeypatch.setattr(fila, "finalizar", original)
    await p.assumir()
    atual = await operacao(amb, op.protocolo)
    assert (atual.estado, atual.desfecho) == (Estado.NAO_CONFIRMADA, "interrompida no envio")
    assert chamadas(amb, "criar_chamado") == antes + 1  # nada reenviado


# --- [30] Limite encerrado para a acao que ele liberou ------------------------------------------


async def test_limite_encerrado_para_a_acao_liberada_e_vencido_pelo_prazo_nao(amb: Ambiente) -> None:
    await sem_limites(amb)
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    pedidos, aplicativo = await pedidos_de(amb)
    f = await vitrine(amb, "enviar_mensagem")
    p = porta(amb)
    antes = chamadas(amb, "enviar_mensagem")
    for situacao, fim in (("encerrado", Estado.CANCELADA), ("suspenso", Estado.CANCELADA), ("vencido", Estado.ENVIADA)):
        limite = await novo_limite(amb)
        op, _ = await pedidos.pedir(
            Pedido(amb.ana_id, aplicativo, f, {"destinatario": "equipe", "texto": f"limite {situacao}"}, None)
        )
        assert (op.estado, op.limite_id) == (Estado.LIBERADA, limite)
        async with amb.admin.begin() as c:
            await c.execute(text("UPDATE limites SET situacao = :s WHERE id = :i"), {"s": situacao, "i": limite})
        assert await p.enviar_uma() is True
        atual = await operacao(amb, op.protocolo)
        assert atual.estado == fim, situacao
        if fim == Estado.CANCELADA:
            assert atual.desfecho == "o limite foi encerrado ou suspenso antes de a acao sair"
    assert chamadas(amb, "enviar_mensagem") == antes + 1


# --- [41] Guarda das fichas no banco ---------------------------------------------------------------


async def acao_de_risco_alto(amb: Ambiente, titulo: str, *, exige_passkey: bool) -> Operacao:
    f = await vitrine(amb, "cancelar_servico")
    assert f.risco == "alto"
    assert f.ordem_id is not None
    async with amb.entrada_db.begin() as c:
        return await fila.registrar_pedido(
            c,
            pessoa_id=amb.ana_id,
            aplicativo_id=amb.aplicativo_id,
            ordem_id=f.ordem_id,
            ferramenta=f,
            argumentos={"servico": titulo},
            aplicativo_diz=None,
            rota=fila.Rota(exige_passkey=exige_passkey, motivo="risco_alto"),
            prazo_ficha_minutos=30,
            espera_segundos=0,
        )


async def confirmar(amb: Ambiente, op: Operacao, **kw: Any) -> Operacao:
    assert op.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        return await fila.confirmar_ficha(
            c, op.ficha_id, pessoa_id=amb.ana_id, espera_segundos=0, rapida_segundos=0, **kw
        )


async def test_decisao_da_ficha_vale_uma_vez_e_as_rotinas_so_vencem(amb: Ambiente) -> None:
    op = await nova_acao(amb, "Guarda da ficha", confirmada=False)
    ficha = op.ficha_id
    confirma = "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), confirmada_com = 'sessao' WHERE id = :f"
    assert await recusa_do_banco(amb.rotinas_db, confirma, f=ficha) == SEM_PERMISSAO
    assert (
        await recusa_do_banco(
            amb.rotinas_db, "UPDATE fichas SET decisao = 'recusada', decidida_em = now() WHERE id = :f", f=ficha
        )
        == SEM_PERMISSAO
    )
    vence = "UPDATE fichas SET decisao = 'vencida', decidida_em = now() WHERE id = :f"
    assert await recusa_do_banco(amb.rotinas_db, vence, f=ficha) is None
    # Decidida, nao muda mais: nem pela entrada, nem pelo dono do banco, nem de volta a vazia.
    assert await recusa_do_banco(amb.entrada_db, confirma, f=ficha) == RECUSA_CHECK
    assert (
        await recusa_do_banco(amb.admin, "UPDATE fichas SET decisao = NULL, decidida_em = NULL WHERE id = :f", f=ficha)
        == RECUSA_CHECK
    )
    assert await recusa_do_banco(amb.admin, "UPDATE fichas SET risco = 'baixo' WHERE id = :f", f=ficha) == RECUSA_CHECK
    # A ficha nasce sem decisao.
    nasce_decidida = (
        "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco, decisao, decidida_em)"
        " SELECT id, codigo_conferencia, now(), 'medio', 'confirmada', now() FROM operacoes WHERE id = :o"
    )
    assert await recusa_do_banco(amb.entrada_db, nasce_decidida, o=op.id) == RECUSA_CHECK


async def test_risco_alto_so_confirma_com_passkey_ou_login_refeito(amb: Ambiente) -> None:
    # Ficha montada com a passkey desligada (sem exigir passkey), de risco alto.
    op = await acao_de_risco_alto(amb, f"alto {uuid.uuid4().hex[:6]}", exige_passkey=False)
    so_sessao = "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), confirmada_com = 'sessao' WHERE id = :f"
    assert await recusa_do_banco(amb.entrada_db, so_sessao, f=op.ficha_id) == RECUSA_CHECK
    with pytest.raises(fila.DecisaoRecusada) as erro:
        await confirmar(amb, op, com_passkey=False)
    assert erro.value.motivo == "falta_login_refeito"
    liberada = await confirmar(amb, op, com_passkey=False, login_refeito=True)
    assert liberada.estado == Estado.LIBERADA
    async with amb.admin.connect() as c:
        como = (
            await c.execute(text("SELECT confirmada_com FROM fichas WHERE id = :f"), {"f": op.ficha_id})
        ).scalar_one()
    assert como == "login_refeito"
    frases = [x.frase for x in await extrato(amb, "ficha_confirmada", protocolo=op.protocolo)]
    assert frases == [f"Ficha do protocolo {op.protocolo} confirmada pela pessoa (com o login refeito)."]
    assert await porta(amb).enviar_uma() is True
    assert (await operacao(amb, op.protocolo)).estado == Estado.ENVIADA

    # Ficha que pede passkey: com a passkey ligada, so ela; desligada depois, o login refeito vale.
    pede = await acao_de_risco_alto(amb, f"pede passkey {uuid.uuid4().hex[:6]}", exige_passkey=True)
    with pytest.raises(fila.DecisaoRecusada) as erro:
        await confirmar(amb, pede, com_passkey=False, login_refeito=True)
    assert erro.value.motivo == "falta_passkey"
    with pytest.raises(fila.DecisaoRecusada) as erro:
        await confirmar(amb, pede, com_passkey=False, passkey_ligada=False)
    assert erro.value.motivo == "falta_login_refeito"
    confirmada = await confirmar(amb, pede, com_passkey=False, login_refeito=True, passkey_ligada=False)
    assert confirmada.estado == Estado.LIBERADA


# --- [42, 45] Guarda da fila e permissoes sem uso ----------------------------------------------------


async def test_linha_da_fila_nao_troca_ficha_nem_limite_e_nao_muda_depois_do_fim(amb: Ambiente) -> None:
    a = await nova_acao(amb, "Fila guardada A", confirmada=False)
    b = await nova_acao(amb, "Fila guardada B", confirmada=False)
    limite = await novo_limite(amb, nome="criar_chamado")
    # A entrada nem tem permissao nessas colunas; o dono esbarra no gatilho.
    assert await recusa_do_banco(
        amb.entrada_db, "UPDATE fila_saida SET ficha_id = NULL WHERE id = :i", i=a.fila_id
    ) == (SEM_PERMISSAO)
    for sql, valores in (
        ("UPDATE fila_saida SET ficha_id = NULL WHERE id = :i", {}),
        ("UPDATE fila_saida SET limite_id = :l WHERE id = :i", {"l": limite}),
    ):
        assert await recusa_do_banco(amb.admin, sql, i=a.fila_id, **valores) == RECUSA_CHECK, sql
    # Linha num estado final: nada mais muda, nem a hora nem quem mudou.
    async with amb.entrada_db.begin() as c:
        await fila.cancelar(c, a.id, pessoa_id=amb.ana_id)
    for papel in (amb.entrada_db, amb.admin):
        for sql in (
            "UPDATE fila_saida SET mudou_por = 'outro' WHERE id = :i",
            "UPDATE fila_saida SET hora_saida = now() WHERE id = :i",
        ):
            assert await recusa_do_banco(papel, sql, i=a.fila_id) == RECUSA_CHECK, sql
    # A outra linha, ainda esperando a ficha, continua mudando pelo caminho de sempre.
    assert b.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        await fila.recusar_ficha(c, b.ficha_id, pessoa_id=amb.ana_id)
    assert (await operacao(amb, b.protocolo)).estado == Estado.CANCELADA

    # Permissoes que ninguem usa sairam.
    destinatarios = "UPDATE limites SET destinatarios = '[]'::jsonb WHERE id = :l"
    assert await recusa_do_banco(amb.entrada_db, destinatarios, l=limite) == SEM_PERMISSAO
    comprovante = "INSERT INTO comprovantes (bloqueio_id) SELECT id FROM bloqueios ORDER BY criado_em DESC LIMIT 1"
    assert await recusa_do_banco(amb.despachante_db, comprovante) == SEM_PERMISSAO


# --- [44] Indices do teto e das varreduras ------------------------------------------------------------


async def test_indices_do_teto_e_das_varreduras(amb: Ambiente) -> None:
    async with amb.admin.connect() as c:
        nomes = set(
            (
                await c.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename IN ('fichas', 'operacoes', 'fila_saida')")
                )
            ).scalars()
        )
    assert {"fichas_montada", "operacoes_aplicativo", "fila_em_aberto", "fila_mudou"} <= nomes


# --- [34, 45] Limpeza dos desafios de passkey -----------------------------------------------------


async def test_rotinas_limpam_desafios_vencidos_ou_usados(amb: Ambiente) -> None:
    desafios = {nome: uuid.uuid4().bytes for nome in ("vencido", "usado", "valendo")}
    async with amb.entrada_db.begin() as c:
        for nome, (expira, usado) in {
            "vencido": ("-1 minute", False),
            "usado": ("5 minutes", True),
            "valendo": ("5 minutes", False),
        }.items():
            await c.execute(
                text(
                    "INSERT INTO desafios_passkey (desafio, pessoa_id, finalidade, expira_em, usado_em)"
                    " VALUES (:d, :p, 'teste', now() + CAST(CAST(:e AS text) AS interval),"
                    " CASE WHEN :u THEN now() END)"
                ),
                {"d": desafios[nome], "p": amb.ana_id, "e": expira, "u": usado},
            )
    assert await acoes.limpar_desafios_passkey(amb.rotinas_db) >= 2
    async with amb.admin.connect() as c:
        restam = set(
            (
                await c.execute(
                    text("SELECT desafio FROM desafios_passkey WHERE desafio = ANY(:d)"), {"d": list(desafios.values())}
                )
            ).scalars()
        )
    assert restam == {desafios["valendo"]}
