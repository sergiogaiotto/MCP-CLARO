"""Segunda rodada de correcoes da porta de saida (nucleo), contra o PostgreSQL real com o usuario de cada papel.

Cada teste falha sem a correcao: recusa da ficha de uma acao que ja parou, caractere nulo no resultado, fim do
envio que nunca grava, tentativa nova depois de um COMMIT que deu certo, conferencia com o vigia sem resposta,
limite "so estes" com argumento fora do esquema e extrato da acao de um programa.

Mesmo arranjo de ``test_saida_disputas``: o app do despachante sobe sem a porta, e cada teste usa
``PortaDeSaida`` direto, para escolher o momento de cada passo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from mcp_claro import continuidade
from mcp_claro.cadastro import repositorio
from mcp_claro.extrato.frases import frase
from mcp_claro.ligacao import cliente
from mcp_claro.saida import despachante as despachante_mod
from mcp_claro.saida import fila
from mcp_claro.saida.despachante import TROCA_DO_NULO, PortaDeSaida
from mcp_claro.saida.estados import FRASE, Estado
from mcp_claro.saida.fila import DecisaoRecusada, Rota
from mcp_claro.saida.pedidos import Pedido
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import chamadas, esperar_estado, operacao, politica
from tests.integracao.test_saida_correcoes import extrato, pedidos_de, sem_limites, usados
from tests.integracao.test_saida_disputas import amb, bloquear, desbloquear, nova_acao, porta, reativar, vitrine

__all__ = ["amb"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


def falha_do_banco() -> OperationalError:
    return OperationalError("UPDATE fila_saida", {}, OSError("conexao caiu"))


def resposta_ok(texto: str) -> cliente.Resposta:
    return cliente.Resposta(
        "resultado", resultado=types.CallToolResult(content=[types.TextContent(type="text", text=texto)])
    )


# --- N1: recusar a ficha de uma acao que ja parou -------------------------------------------------


async def test_recusar_ficha_de_acao_parada_nao_grava_decisao_nem_extrato(amb: Ambiente) -> None:
    marca = uuid.uuid4().hex[:6]
    por_bloqueio = await nova_acao(amb, f"Recusa depois do bloqueio {marca}", confirmada=False)
    b = await bloquear(amb, "pessoa")
    await desbloquear(amb, b)
    assert (await operacao(amb, por_bloqueio.protocolo)).estado == Estado.PARADA_POR_BLOQUEIO
    # A restauracao para so esta linha, com o papel do despachante (o mesmo estado da conferencia).
    por_restauracao = await nova_acao(amb, f"Recusa depois da restauracao {marca}", confirmada=False)
    async with amb.despachante_db.begin() as c:
        assert await fila.parar(
            c, por_restauracao, Estado.PARADA_POR_RESTAURACAO, "parada por restauracao", quem="restauracao"
        )

    for op, estado_parado in (
        (por_bloqueio, Estado.PARADA_POR_BLOQUEIO),
        (por_restauracao, Estado.PARADA_POR_RESTAURACAO),
    ):
        assert op.ficha_id is not None
        with pytest.raises(DecisaoRecusada) as recusa:
            async with amb.entrada_db.begin() as c:
                await fila.recusar_ficha(c, op.ficha_id, pessoa_id=amb.ana_id)
        assert recusa.value.motivo == "nao_esta_aguardando"
        assert FRASE[estado_parado] in recusa.value.mensagem
        assert "nada mudou" in recusa.value.mensagem
        atual = await operacao(amb, op.protocolo)
        assert (atual.estado, atual.ficha_decisao) == (estado_parado, None)
        assert await extrato(amb, "ficha_recusada", protocolo=op.protocolo) == []

    # A ficha que ainda espera continua recusavel, como antes.
    viva = await nova_acao(amb, f"Recusa que vale {marca}", confirmada=False)
    assert viva.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        recusada = await fila.recusar_ficha(c, viva.ficha_id, pessoa_id=amb.ana_id)
    assert (recusada.estado, recusada.ficha_decisao) == (Estado.CANCELADA, "recusada")
    assert len(await extrato(amb, "ficha_recusada", protocolo=viva.protocolo)) == 1


# --- N2: fim do envio com caractere nulo, e fim que nunca grava -------------------------------------


async def test_resultado_com_caractere_nulo_grava_com_o_caractere_de_troca(amb: Ambiente) -> None:
    op = await nova_acao(amb, f"Resultado com nulo {uuid.uuid4().hex[:6]}")
    enviados: list[str] = []

    async def chamar(*args: Any, **_: Any) -> cliente.Resposta:
        enviados.append(args[3])
        return resposta_ok("ok\x00fim")

    assert await porta(amb, chamar=chamar).enviar_uma() is True
    atual = await operacao(amb, op.protocolo)
    assert atual.estado == Estado.ENVIADA
    assert atual.resultado is not None
    assert atual.resultado["content"][0]["text"] == f"ok{TROCA_DO_NULO}fim"
    assert enviados == ["criar_chamado"]
    assert len(await extrato(amb, "acao_enviada", protocolo=op.protocolo)) == 1


async def test_fim_que_nunca_grava_encerra_o_turno_e_a_retomada_da_nao_confirmada(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(despachante_mod, "ESPERA_DO_FIM", 0.01)
    op = await nova_acao(amb, f"Fim que nunca grava {uuid.uuid4().hex[:6]}")
    enviados: list[str] = []
    tentativas: list[int] = []

    async def chamar(*args: Any, **_: Any) -> cliente.Resposta:
        enviados.append(args[3])
        return resposta_ok("feito")

    async def sempre_falha(*_: Any, **__: Any) -> bool:
        tentativas.append(1)
        raise falha_do_banco()

    monkeypatch.setattr(fila, "finalizar", sempre_falha)
    p = PortaDeSaida(amb.despachante_db, amb.cifra, varredura=0.2, prazo_envio=5.0, chamar=chamar)
    tarefa = asyncio.create_task(p.rodar())
    try:
        # O turno acaba com o erro, a trava cai com a conexao, e a porta assume de novo: a retomada decide.
        atual = await esperar_estado(amb, op.protocolo, Estado.NAO_CONFIRMADA, prazo=20)
        assert atual.desfecho == "interrompida no envio"
        assert len(tentativas) == despachante_mod.TENTATIVAS_DO_FIM
        await asyncio.wait_for(p.assumiu.wait(), 10)
        await asyncio.sleep(1.0)  # varias varreduras com a porta de novo no ar
        assert enviados == ["criar_chamado"]  # uma chamada so: nada reenviado
        assert not tarefa.done()
        assert (await operacao(amb, op.protocolo)).estado == Estado.NAO_CONFIRMADA
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa


# --- N3: tentativa nova depois de um COMMIT que deu certo -----------------------------------------


async def test_tentativa_nova_reconhece_o_proprio_fim_e_nao_grava_de_novo(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(despachante_mod, "ESPERA_DO_FIM", 0.01)
    op = await nova_acao(amb, f"Commit sem confirmacao {uuid.uuid4().hex[:6]}")
    antes = chamadas(amb, "criar_chamado")
    original = fila.finalizar
    chamadas_do_fim: list[int] = []

    async def commit_sem_confirmacao(c: Any, *args: Any, **kwargs: Any) -> bool:
        chamadas_do_fim.append(1)
        if len(chamadas_do_fim) == 1:
            # O fim grava e o COMMIT passa, mas a resposta do banco se perde: o despachante ve um erro.
            async with amb.despachante_db.begin() as outra:
                await original(outra, *args, **kwargs)
            raise falha_do_banco()
        return await original(c, *args, **kwargs)

    monkeypatch.setattr(fila, "finalizar", commit_sem_confirmacao)
    assert await porta(amb).enviar_uma() is True
    assert len(chamadas_do_fim) == 2
    atual = await operacao(amb, op.protocolo)
    assert (atual.estado, atual.desfecho) == (Estado.ENVIADA, "o sistema respondeu")
    enviadas = await extrato(amb, "acao_enviada", protocolo=op.protocolo)
    assert len(enviadas) == 1
    assert enviadas[0].dados["gravado"] is True
    assert chamadas(amb, "criar_chamado") == antes + 1


async def test_resposta_depois_de_uma_retomada_de_verdade_continua_marcada(amb: Ambiente) -> None:
    """A tentativa nova so reconhece o proprio fim: depois de uma retomada, a resposta fica marcada como hoje."""
    op = await nova_acao(amb, f"Resposta depois da retomada {uuid.uuid4().hex[:6]}")
    p = porta(amb)
    async with amb.despachante_db.begin() as c:
        assert await fila.marcar_enviando(c, op)
    assert op.protocolo in await p.assumir()
    async with amb.despachante_db.begin() as c:
        assert await fila.finalizar(c, op, Estado.ENVIADA, {"content": []}, "o sistema respondeu") is False
    atual = await operacao(amb, op.protocolo)
    assert (atual.estado, atual.desfecho) == (
        Estado.NAO_CONFIRMADA,
        "o sistema respondeu (resposta depois da retomada)",
    )


# --- N4: conferencia com o vigia sem resposta, sem sinal de restauracao ------------------------------


async def situacao_da_saida(amb: Ambiente) -> bool:
    async with amb.admin.connect() as c:
        return bool((await c.execute(text("SELECT aberta FROM estado_saida WHERE id = 1"))).scalar_one())


async def ultimo_id_do_extrato(amb: Ambiente) -> int:
    async with amb.admin.connect() as c:
        return int((await c.execute(text("SELECT coalesce(max(id), 0) FROM extrato"))).scalar_one())


async def tipos_depois(amb: Ambiente, desde: int) -> list[str]:
    async with amb.admin.connect() as c:
        return list(
            (await c.execute(text("SELECT tipo FROM extrato WHERE id > :i ORDER BY id"), {"i": desde})).scalars()
        )


async def test_vigia_sem_resposta_fecha_a_saida_e_pausa_sem_parar_a_fila(amb: Ambiente) -> None:
    pausados: tuple[uuid.UUID, ...] = ()
    solta = asyncio.Event()
    enviados: list[str] = []

    async def chamar_e_esperar(*args: Any, **kwargs: Any) -> cliente.Resposta:
        enviados.append(args[3])
        if len(enviados) == 1:
            await solta.wait()  # a primeira acao fica no meio do envio durante a conferencia
        return await cliente.chamar_ferramenta(*args, **kwargs)

    tarefa: asyncio.Task[bool] | None = None
    try:
        # Partida anterior conferida: a ultima linha da continuidade e a do banco como esta.
        base = await continuidade.conferir_continuidade(amb.despachante_db, 0, exigir_vigia=False)
        pausados += base.aplicativos_pausados
        await reativar(amb, pausados)
        if not await situacao_da_saida(amb):
            assert await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Arranjo do teste")

        marca = uuid.uuid4().hex[:6]
        enviando = await nova_acao(amb, f"Vigia mudo, no meio do envio {marca}")
        tarefa = asyncio.create_task(porta(amb, chamar=chamar_e_esperar).enviar_uma())
        await esperar_estado(amb, enviando.protocolo, Estado.ENVIANDO)
        esperando = await nova_acao(amb, f"Vigia mudo, esperando a ficha {marca}", confirmada=False)
        liberada = await nova_acao(amb, f"Vigia mudo, liberada {marca}", espera=600)
        desde = await ultimo_id_do_extrato(amb)

        r = await continuidade.conferir_continuidade(amb.despachante_db, None, exigir_vigia=True)
        pausados += r.aplicativos_pausados
        assert (r.desfecho, r.motivos) == ("nao_confere", ("vigia_sem_resposta",))
        assert amb.aplicativo_id in r.aplicativos_pausados
        assert not await situacao_da_saida(amb)

        # A fila nao para: nada muda de estado e nada fala em restauracao.
        estados = [(await operacao(amb, o.protocolo)) for o in (esperando, liberada, enviando)]
        assert [o.estado for o in estados] == [Estado.AGUARDANDO_FICHA, Estado.LIBERADA, Estado.ENVIANDO]
        assert [o.desfecho for o in estados] == [None, None, None]
        tipos = await tipos_depois(amb, desde)
        assert "fila_parada_restauracao" not in tipos
        assert "restauracao_detectada" not in tipos
        assert "acao_nao_confirmada" not in tipos
        assert {"estado_saida_mudou", "continuidade_nao_confere", "aplicativo_pausado_continuidade"} <= set(tipos)
        motivo = f"o vigia nao respondeu e {continuidade.SEM_SINAL_DE_RESTAURACAO}"
        async with amb.admin.connect() as c:
            linha = (
                await c.execute(
                    text("SELECT frase, dados FROM extrato WHERE tipo = 'continuidade_nao_confere' AND id > :i"),
                    {"i": desde},
                )
            ).one()
            saida_motivo = (await c.execute(text("SELECT motivo FROM estado_saida WHERE id = 1"))).scalar_one()
        assert linha.frase == frase("continuidade_nao_confere", motivo=motivo)
        assert linha.dados["fila_parada"] is False
        assert saida_motivo == motivo

        # O envio que ja tinha saido termina e grava o fim (ninguem o deu como nao confirmado).
        solta.set()
        assert await tarefa is True
        assert (await operacao(amb, enviando.protocolo)).estado == Estado.ENVIADA

        # Com a saida fechada, nada sai: nem a liberada na hora, nem a ficha confirmada agora.
        assert esperando.ficha_id is not None
        async with amb.entrada_db.begin() as c:
            await fila.seguir_agora(c, liberada.id, pessoa_id=amb.ana_id)
            await fila.confirmar_ficha(
                c, esperando.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=0, rapida_segundos=0
            )
        p = porta(amb, chamar=chamar_e_esperar)
        assert await p.enviar_uma() is False
        assert len(enviados) == 1
        for o in (esperando, liberada):
            assert (await operacao(amb, o.protocolo)).estado == Estado.LIBERADA

        # O ADMIN reativa o aplicativo e reabre a saida: a fila segue, sem nova confirmacao.
        await reativar(amb, pausados)
        pausados = ()
        assert await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Vigia conferido")
        assert await p.enviar_uma() is True
        assert await p.enviar_uma() is True
        for o in (esperando, liberada):
            assert (await operacao(amb, o.protocolo)).estado == Estado.ENVIADA
        assert len(enviados) == 3
    finally:
        solta.set()
        if tarefa is not None and not tarefa.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tarefa, 15)
        if not await situacao_da_saida(amb):
            await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Fim do teste")
        await reativar(amb, pausados)


# --- N8: limite "so estes" com argumento fora do esquema -------------------------------------------


async def limite_so_estes(amb: Ambiente) -> uuid.UUID:
    async with amb.entrada_db.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                    " valido_ate, com_passkey) VALUES (:p, :a, :s, 'enviar_mensagem', CAST(:d AS jsonb), 5,"
                    " now() + interval '1 day', true) RETURNING id"
                ),
                {
                    "p": amb.ana_id,
                    "a": amb.aplicativo_id,
                    "s": amb.sistema_id,
                    "d": json.dumps([{"campo": "destinatario", "valores": ["equipe"]}]),
                },
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def test_limite_so_estes_com_argumento_fora_do_esquema_vai_para_a_ficha(amb: Ambiente) -> None:
    await sem_limites(amb)
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    try:
        limite = await limite_so_estes(amb)
        pedidos, aplicativo = await pedidos_de(amb)
        f = await vitrine(amb, "enviar_mensagem")
        marca = uuid.uuid4().hex[:6]
        antes = chamadas(amb, "enviar_mensagem")

        # O esquema de enviar_mensagem tem destinatario e texto: "copia_oculta" fica fora dele.
        fora = {"destinatario": "equipe", "texto": f"fora {marca}", "copia_oculta": "fora@exemplo"}
        op, _ = await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, fora, None))
        assert (op.estado, op.limite_id) == (Estado.AGUARDANDO_FICHA, None)
        assert await usados(amb, limite) == 0
        dentro, _ = await pedidos.pedir(
            Pedido(amb.ana_id, aplicativo, f, {"destinatario": "equipe", "texto": f"dentro {marca}"}, None)
        )
        assert (dentro.estado, dentro.limite_id) == (Estado.LIBERADA, limite)
        assert await usados(amb, limite) == 1

        # Limite ja gasto para um pedido com argumento fora do esquema (gravado antes desta regra): o
        # despachante reconfere com o mesmo esquema e cancela.
        assert f.ordem_id is not None
        async with amb.entrada_db.begin() as c:
            gasto = await fila.registrar_pedido(
                c,
                pessoa_id=amb.ana_id,
                aplicativo_id=amb.aplicativo_id,
                ordem_id=f.ordem_id,
                ferramenta=f,
                argumentos={"destinatario": "equipe", "texto": f"gasto {marca}", "copia_oculta": "fora@exemplo"},
                aplicativo_diz=None,
                rota=Rota(limite_id=limite, motivo="limite"),
                prazo_ficha_minutos=30,
                espera_segundos=0,
            )
        assert gasto.estado == Estado.LIBERADA
        p = porta(amb)
        assert await p.enviar_uma() is True
        assert await p.enviar_uma() is True
        assert (await operacao(amb, dentro.protocolo)).estado == Estado.ENVIADA
        cancelada = await operacao(amb, gasto.protocolo)
        assert (cancelada.estado, cancelada.desfecho) == (Estado.CANCELADA, "o limite nao cobre este pedido")
        assert chamadas(amb, "enviar_mensagem") == antes + 1
        async with amb.entrada_db.begin() as c:
            await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)
    finally:
        await sem_limites(amb)


# --- N9: acao de programa no extrato passa por programas.linha ---------------------------------------


async def test_acao_pedida_e_repetida_de_programa_passam_por_programas_linha(amb: Ambiente) -> None:
    # Com as frases de verdade (programa_acao_pedida e programa_acao_repetida), sem troca por monkeypatch.
    sufixo = uuid.uuid4().hex[:8]
    async with amb.admin.begin() as c:
        robo_id = await repositorio.habilitar_aplicativo(
            c, client_id=f"robo-linha-{sufixo}", nome=f"Robo linha {sufixo}", familia="teste", tipo="programa"
        )
        await c.execute(
            text(
                "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim, situacao)"
                " VALUES (:a, :r, :s, 'teste', current_date + 30, 'ativo')"
            ),
            {"a": robo_id, "r": amb.ana_id, "s": amb.bia_id},
        )
        robo = await repositorio.aplicativo_por_client_id(c, f"robo-linha-{sufixo}")
    assert robo is not None
    f = await vitrine(amb)
    assert f.ordem_id is not None
    argumentos = {"titulo": f"Programa no extrato {sufixo}"}
    async with amb.entrada_db.begin() as c:
        op = await fila.registrar_pedido(
            c,
            pessoa_id=amb.ana_id,
            aplicativo_id=robo_id,
            ordem_id=f.ordem_id,
            ferramenta=f,
            argumentos=argumentos,
            aplicativo_diz=None,
            rota=Rota(),
            prazo_ficha_minutos=30,
            espera_segundos=0,
        )
    pedidos, _ = await pedidos_de(amb)
    repetida, foi_repetida = await pedidos.pedir(Pedido(amb.ana_id, robo, f, argumentos, None))
    assert (repetida.id, foi_repetida) == (op.id, True)
    # Pedido novo pelo caminho da entrada (com o Aplicativo na mao, sem consulta a mais para um assistente).
    outro, foi_outro = await pedidos.pedir(Pedido(amb.ana_id, robo, f, {"titulo": f"Outro {sufixo}"}, None))
    assert not foi_outro
    try:
        async with amb.admin.connect() as c:
            linhas = (
                await c.execute(
                    text("SELECT tipo, frase FROM extrato WHERE protocolo = ANY(:p) ORDER BY id"),
                    {"p": [op.protocolo, outro.protocolo]},
                )
            ).all()
        assert [linha.tipo for linha in linhas] == [
            "programa_acao_pedida",
            "programa_acao_repetida",
            "programa_acao_pedida",
        ]
        async with amb.admin.connect() as c:
            responsavel = (
                await c.execute(text("SELECT coalesce(nome, sujeito) FROM pessoas WHERE id = :p"), {"p": amb.ana_id})
            ).scalar_one()
        programa = f"Robo linha {sufixo}"
        assert [linha.frase for linha in linhas] == [
            frase(
                "programa_acao_pedida",
                ferramenta=f.nome_vitrine,
                programa=programa,
                responsavel=responsavel,
                protocolo=op.protocolo,
            ),
            frase("programa_acao_repetida", programa=programa, responsavel=responsavel, protocolo=op.protocolo),
            frase(
                "programa_acao_pedida",
                ferramenta=f.nome_vitrine,
                programa=programa,
                responsavel=responsavel,
                protocolo=outro.protocolo,
            ),
        ]
        # O assistente continua com a frase de sempre (linha() so muda a de um programa).
        do_assistente = await nova_acao(amb, f"Assistente no extrato {sufixo}", confirmada=False)
        assert len(await extrato(amb, "acao_pedida", protocolo=do_assistente.protocolo)) == 1
        async with amb.entrada_db.begin() as c:
            await fila.cancelar(c, do_assistente.id, pessoa_id=amb.ana_id)
    finally:
        async with amb.entrada_db.begin() as c:
            await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)
            await fila.cancelar(c, outro.id, pessoa_id=amb.ana_id)
