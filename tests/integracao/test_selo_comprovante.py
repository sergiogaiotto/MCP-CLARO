"""Comprovante do bloqueio no nivel 3 (Fase 4, grupo G4; desenho 6.3 com a correcao A1 da critica).

- A transacao do bloqueio e a da Fase 2: nao cria aviso de bloqueio, e a linha de ``comprovantes`` continua com
  ``informado`` vazio. Os avisos sao derivados depois pelo despachante (G3a); aqui o dono grava o aviso e as respostas
  como ele gravaria (``tests/apoio_selos.py``), e o caminho inteiro e prova do G9.
- ``comprovante_do_bloqueio`` junta, na leitura, o que os sistemas informaram sobre as operacoes do comprovante
  (``{protocolo, sistema, estado, origem, gravado_em, de_terceiro}``): a resposta ao aviso DESTE bloqueio e a ultima de
  recibo ou de consulta, so com ``respondeu``. A linha de ``comprovantes`` nao muda.
- As operacoes do aviso deste bloqueio que nao estao no comprovante (em andamento ha mais de 24 horas, critica B9)
  tambem entram; e so a consulta gasta a vez da escada (o recibo e a resposta ao aviso, nao).
- Decisao 1 do coordenador (retoque da etapa 3): a resposta ao aviso nunca e final. Um ``parada`` so do aviso nao
  resolve o "estamos confirmando" e nao queima o numero do pedido igual; pela consulta, resolve e queima.
- Conferencia final da etapa 3r: a resposta ao aviso que diz que a escrita foi feita (``concluida`` ou ``parcial``)
  impede o numero novo do pedido igual, no codigo e no banco, sem deixar de nao ser final.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.regras import bloqueios
from mcp_claro.regras.bloqueios import Bloqueio
from mcp_claro.saida import fila
from mcp_claro.selo import contrato
from tests.integracao.test_selo_g4_apoio import MundoSelo, protocolo_de, subir_mundo_selo, textos

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[MundoSelo]:
    async with subir_mundo_selo(banco_migrado) as m:
        await m.gravar_teste(nivel=3)
        yield m


async def _acao(m: MundoSelo, n: int, fim: str | None) -> fila.Operacao:
    """Uma acao da Ana: ``fim`` None fica esperando a ficha; senao sai com numero de nivel 3 e termina em ``fim``."""
    op = await m.operacao(protocolo_de(await m.agir({"para": f"bloqueio-{n}@exemplo.teste", "texto": "t"})))
    if fim is None:
        return op
    op = await m.confirmar(op)
    await m.sair_com_numero(op, fim)
    return await m.por_id(op.id)


async def _bloquear(m: MundoSelo) -> Bloqueio:
    async with m.entrada_db.begin() as c:
        return await bloqueios.bloquear(
            c, "bloqueio", "pessoa", bloqueios.quem_pessoa(m.pessoas["ana"]), "teste do G4", pessoa_id=m.pessoas["ana"]
        )


async def _encerrar(m: MundoSelo, b: Bloqueio) -> None:
    async with m.entrada_db.begin() as c:
        await bloqueios.encerrar(c, b.id, bloqueios.quem_admin(m.admin_id))


async def _linha_do_comprovante(m: MundoSelo, b: Bloqueio) -> Any:
    async with m.dono.connect() as c:
        return (
            await c.execute(
                text("SELECT paradas, ja_saidas, em_conferencia, informado FROM comprovantes WHERE bloqueio_id = :b"),
                {"b": b.id},
            )
        ).one()


async def _avisos(m: MundoSelo, b: Bloqueio) -> int:
    async with m.dono.connect() as c:
        return int(
            (
                await c.execute(text("SELECT count(*) FROM avisos_bloqueio WHERE bloqueio_id = :b"), {"b": b.id})
            ).scalar_one()
        )


async def _avisar(m: MundoSelo, b: Bloqueio, respostas: dict[uuid.UUID, str]) -> uuid.UUID:
    """O que a tarefa do despachante (G3a) faria: o aviso do bloqueio com os numeros, a vez reservada, uma resposta
    por operacao e o aviso respondido. Pelo dono."""
    async with m.dono.begin() as c:
        aviso = (
            await c.execute(
                text(
                    "INSERT INTO avisos_bloqueio (bloqueio_id, sistema_id, pessoa_id, operacoes)"
                    " VALUES (:b, :s, :p, CAST(:o AS uuid[])) RETURNING id"
                ),
                {"b": b.id, "s": m.sistema.id, "p": m.pessoas["ana"], "o": list(respostas)},
            )
        ).scalar_one()
        await c.execute(
            text("UPDATE avisos_bloqueio SET tentativas = 1, proxima_em = now() + interval '1 minute' WHERE id = :a"),
            {"a": aviso},
        )
    for operacao_id, estado in respostas.items():
        async with m.dono.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO informacoes_sistema (operacao_id, sistema_id, origem, aviso_id, desfecho, estado)"
                    " VALUES (:o, :s, 'aviso', :a, 'respondeu', :e)"
                ),
                {"o": operacao_id, "s": m.sistema.id, "a": aviso, "e": estado},
            )
    async with m.dono.begin() as c:
        await c.execute(text("UPDATE avisos_bloqueio SET estado = 'respondido' WHERE id = :a"), {"a": aviso})
    return uuid.UUID(str(aviso))


def _sem_hora(informado: list[dict[str, Any]]) -> list[tuple[str, str, str | None, str, bool]]:
    return [(i["protocolo"], i["sistema"], i["estado"], i["origem"], i["de_terceiro"]) for i in informado]


async def test_bloqueio_nao_cria_aviso_e_o_comprovante_junta_o_que_o_sistema_informou(mundo: MundoSelo) -> None:
    m = mundo
    enviada = await _acao(m, 1, "enviada")
    conferindo = await _acao(m, 2, "nao_confirmada")
    esperando = await _acao(m, 3, None)
    await m.informar(enviada, "em_andamento", origem="recibo")
    await m.informar(enviada, None)  # uma consulta sem resposta nao entra
    await m.informar(conferindo, "concluida")

    b = await _bloquear(m)
    assert await _avisos(m, b) == 0  # A1: a transacao do bloqueio nao cria aviso
    linha = await _linha_do_comprovante(m, b)
    assert linha.informado == []
    async with m.entrada_db.connect() as c:
        antes = await fila.comprovante_do_bloqueio(c, b.id)
    assert antes is not None
    assert [p["protocolo"] for p in antes.paradas] == [esperando.protocolo]
    assert {s["protocolo"] for s in antes.ja_saidas} == {enviada.protocolo}
    assert {s["protocolo"] for s in antes.em_conferencia} == {conferindo.protocolo}
    assert _sem_hora(antes.informado) == [
        (enviada.protocolo, m.sistema.nome, "em_andamento", "recibo", False),
        (conferindo.protocolo, m.sistema.nome, "concluida", "consulta", False),
    ]
    assert antes.avisos == []

    await _avisar(m, b, {enviada.id: "parada", conferindo.id: "concluida"})
    async with m.entrada_db.connect() as c:
        depois = await fila.comprovante_do_bloqueio(c, b.id)
    assert depois is not None
    assert _sem_hora(depois.informado) == [
        (enviada.protocolo, m.sistema.nome, "em_andamento", "recibo", False),
        (enviada.protocolo, m.sistema.nome, "parada", "aviso", False),
        (conferindo.protocolo, m.sistema.nome, "concluida", "consulta", False),
        (conferindo.protocolo, m.sistema.nome, "concluida", "aviso", False),
    ]
    assert all(
        set(i) == {"protocolo", "sistema", "estado", "origem", "gravado_em", "de_terceiro"} for i in depois.informado
    )
    assert depois.avisos == [{"sistema": m.sistema.nome, "estado": "respondido", "de_terceiro": False}]
    assert (depois.paradas, depois.ja_saidas, depois.em_conferencia) == (
        antes.paradas,
        antes.ja_saidas,
        antes.em_conferencia,
    )
    assert await _linha_do_comprovante(m, b) == linha  # a linha de comprovantes nao muda
    await _encerrar(m, b)


async def test_comprovante_so_mostra_a_resposta_ao_aviso_do_proprio_bloqueio(mundo: MundoSelo) -> None:
    m = mundo
    enviada = await _acao(m, 1, "enviada")
    primeiro = await _bloquear(m)
    await _avisar(m, primeiro, {enviada.id: "concluida"})
    await _encerrar(m, primeiro)
    segundo = await _bloquear(m)
    async with m.entrada_db.connect() as c:
        comprovante = await fila.comprovante_do_bloqueio(c, segundo.id)
        do_primeiro = await fila.comprovante_do_bloqueio(c, primeiro.id)
    assert comprovante is not None
    assert do_primeiro is not None
    assert {s["protocolo"] for s in comprovante.ja_saidas} == {enviada.protocolo}
    assert comprovante.informado == []  # a resposta ao aviso do primeiro bloqueio nao e deste
    assert comprovante.avisos == []
    assert _sem_hora(do_primeiro.informado) == [(enviada.protocolo, m.sistema.nome, "concluida", "aviso", False)]
    await _encerrar(m, segundo)


async def test_bloqueio_de_operacoes_sem_numero_fica_como_na_fase_2(mundo: MundoSelo) -> None:
    m = mundo
    op = await m.operacao(protocolo_de(await m.agir(pessoa="real")))  # pessoa real: nivel 1, sem numero
    op = await m.confirmar(op, "real")
    async with m.dono.begin() as c:
        await c.execute(
            text("UPDATE fila_saida SET estado = 'enviando', mudou_por = 'despachante' WHERE operacao_id = :o"),
            {"o": op.id},
        )
        await c.execute(
            text("UPDATE fila_saida SET estado = 'enviada', enviada_em = now() WHERE operacao_id = :o"), {"o": op.id}
        )
    async with m.entrada_db.begin() as c:
        b = await bloqueios.bloquear(
            c, "bloqueio", "pessoa", bloqueios.quem_pessoa(m.pessoas["real"]), "teste", pessoa_id=m.pessoas["real"]
        )
    async with m.entrada_db.connect() as c:
        comprovante = await fila.comprovante_do_bloqueio(c, b.id)
    assert comprovante is not None
    assert {s["protocolo"] for s in comprovante.ja_saidas} == {op.protocolo}
    assert (comprovante.informado, comprovante.avisos) == ([], [])
    assert await _avisos(m, b) == 0
    async with m.entrada_db.begin() as c:
        await bloqueios.encerrar(c, b.id, bloqueios.quem_admin(m.admin_id))


async def _envelhecer_a_fila(m: MundoSelo, op: fila.Operacao, *, horas: int) -> None:
    """Poe a ultima mudanca da fila ``horas`` antes de agora (o dono desliga a guarda da fila so nesta transacao)."""
    async with m.dono.begin() as c:
        await c.execute(text("ALTER TABLE fila_saida DISABLE TRIGGER fila_saida_guarda"))
        await c.execute(
            text("UPDATE fila_saida SET mudou_em = now() - make_interval(hours => :h) WHERE operacao_id = :o"),
            {"h": horas, "o": op.id},
        )
        await c.execute(text("ALTER TABLE fila_saida ENABLE TRIGGER fila_saida_guarda"))


async def test_o_aviso_que_leva_uma_acao_fora_do_comprovante_tambem_entra_no_informado(mundo: MundoSelo) -> None:
    """Critica B9: uma acao de nivel 3 em andamento ha mais de 24 horas nao esta no comprovante (``ja_saidas`` e das
    ultimas 24 horas), mas o aviso deste bloqueio a leva. O que o sistema informou sobre ela tambem aparece."""
    m = mundo
    antiga = await _acao(m, 1, "enviada")
    await m.informar(antiga, "em_andamento", origem="recibo")
    await _envelhecer_a_fila(m, antiga, horas=25)
    b = await _bloquear(m)
    async with m.entrada_db.connect() as c:
        antes = await fila.comprovante_do_bloqueio(c, b.id)
    assert antes is not None
    assert (antes.ja_saidas, antes.em_conferencia) == ([], [])  # fora das tres partes
    assert antes.informado == []
    await _avisar(m, b, {antiga.id: "concluida"})
    async with m.entrada_db.connect() as c:
        depois = await fila.comprovante_do_bloqueio(c, b.id)
    assert depois is not None
    assert _sem_hora(depois.informado) == [
        (antiga.protocolo, m.sistema.nome, "em_andamento", "recibo", False),
        (antiga.protocolo, m.sistema.nome, "concluida", "aviso", False),
    ]
    await _encerrar(m, b)


async def test_o_comprovante_leva_so_a_ultima_resposta_de_recibo_ou_consulta(mundo: MundoSelo) -> None:
    """Desenho 6.3: de recibo e de consulta, so a ultima resposta de cada acao (as respostas ao aviso, todas)."""
    m = mundo
    enviada = await _acao(m, 1, "enviada")
    await m.informar(enviada, "em_andamento", origem="recibo")
    await m.informar(enviada, "em_andamento")
    await m.informar(enviada, "concluida")
    conferindo = await _acao(m, 2, "nao_confirmada")
    await m.informar(conferindo, "em_andamento")
    await m.informar(conferindo, "parcial")
    await m.informar(conferindo, None)  # sem resposta: nao entra e nao apaga a anterior
    b = await _bloquear(m)
    async with m.entrada_db.connect() as c:
        comprovante = await fila.comprovante_do_bloqueio(c, b.id)
    assert comprovante is not None
    assert _sem_hora(comprovante.informado) == [
        (enviada.protocolo, m.sistema.nome, "concluida", "consulta", False),
        (conferindo.protocolo, m.sistema.nome, "parcial", "consulta", False),
    ]
    await _encerrar(m, b)


async def test_so_a_consulta_gasta_tentativa(mundo: MundoSelo) -> None:
    """``contrato.tentativas_da_consulta``: o recibo e a resposta ao aviso nunca gastam a vez da consulta; so as linhas
    de origem ``consulta`` com desfecho em ``DESFECHOS_QUE_GASTAM_TENTATIVA``."""
    m = mundo
    op = await _acao(m, 1, "enviada")
    await m.informar(op, "em_andamento", origem="recibo")
    await m.informar(op, None, origem="recibo", desfecho="resposta_invalida")
    b = await _bloquear(m)
    await _avisar(m, b, {op.id: "em_andamento"})
    atual = await m.por_id(op.id)
    assert atual.informado is not None
    assert (atual.informado.origem, atual.informado.tentativas) == ("aviso", 0)
    assert not atual.consulta_acabou(1)
    await m.informar(op, None)  # a primeira consulta, sem resposta
    atual = await m.por_id(op.id)
    assert atual.informado is not None
    assert atual.informado.tentativas == 1
    assert atual.consulta_acabou(1)
    await _encerrar(m, b)


@pytest.mark.parametrize("origem", ["aviso", "consulta"])
async def test_parada_so_do_aviso_nao_queima_o_numero_nem_resolve(mundo: MundoSelo, origem: str) -> None:
    """Decisao 1 do coordenador (retoque da etapa 3): a resposta ao aviso de bloqueio aparece como o que o sistema
    disse, mas nunca e final. Um ``parada`` so do aviso nao resolve o "estamos confirmando" (o botao de um toque recusa
    e o aplicativo ouve que o MCP-CLARO continua perguntando) e nao queima o numero: o pedido igual depois da decisao
    leva o mesmo numero. O mesmo ``parada`` pela consulta resolve e queima (numero novo)."""
    m = mundo
    op = await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA)))
    op = await m.confirmar(op)
    envio = await m.sair_com_numero(op, "nao_confirmada")
    b = await _bloquear(m)
    if origem == "aviso":
        await _avisar(m, b, {op.id: "parada"})
    else:
        await m.informar(op, "parada")
    await _encerrar(m, b)
    atual = await m.por_id(op.id)
    assert atual.informado is not None
    assert (atual.informado.origem, atual.informado.estado) == (origem, "parada")
    [texto] = textos(await m.consultar(op.protocolo))
    if origem == "aviso":
        assert atual.informado.estado_final is None
        assert "informou: parada, ainda sem resposta final; o MCP-CLARO continua perguntando." in texto
        with pytest.raises(fila.DecisaoRecusada) as recusa:
            async with m.entrada_db.begin() as c:
                await fila.registrar_o_que_o_sistema_informou(
                    c, op.id, pessoa_id=m.pessoas["ana"], estado_visto="parada"
                )
        assert recusa.value.motivo == "sem_resposta_final"
    else:
        assert atual.informado.estado_final == "parada"
        assert f"O sistema {m.sistema.nome} informou: parada." in texto
    await m.decidir(atual, "nao_aconteceu")
    nova = await m.confirmar(await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA))))
    assert nova.repete_protocolo == op.protocolo
    envio_novo = await m.sair_com_numero(nova)
    if origem == "aviso":
        assert (envio_novo.chave, envio_novo.repete_operacao_id) == (envio.chave, op.id)
    else:
        assert envio_novo.chave != envio.chave
        assert envio_novo.repete_operacao_id is None


ARGUMENTOS_DA_PARADA = {"para": "parada@exemplo.teste", "texto": "t"}


async def test_nao_recebida_do_aviso_nao_confirma_a_da_consulta(mundo: MundoSelo) -> None:
    """Decisao 1, a segunda metade da queima: ``nao_recebida`` so queima confirmada por duas respostas com intervalo, e
    so contam as de recibo e consulta. Uma pela consulta e outra pelo aviso, dois minutos depois, nao confirmam: o
    pedido igual leva o mesmo numero, e o "estamos confirmando" continua."""
    m = mundo
    op = await m.confirmar(await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA))))
    envio = await m.sair_com_numero(op, "nao_confirmada")
    primeira = await m.informar(op, "nao_recebida")
    await m.recuar_informacao(primeira.id, 2)
    b = await _bloquear(m)
    await _avisar(m, b, {op.id: "nao_recebida"})
    await _encerrar(m, b)
    atual = await m.por_id(op.id)
    assert atual.informado is not None
    assert (atual.informado.origem, atual.informado.estado, atual.informado.estado_final) == (
        "aviso",
        "nao_recebida",
        None,
    )
    await m.decidir(atual, "nao_aconteceu")
    nova = await m.confirmar(await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA))))
    envio_novo = await m.sair_com_numero(nova)
    assert (envio_novo.chave, envio_novo.repete_operacao_id) == (envio.chave, op.id)


async def test_a_resposta_ao_aviso_nao_conta_para_o_numero_novo(mundo: MundoSelo) -> None:
    """Critica M4.2 com a decisao 1: o numero novo so vale com a ultima resposta A CONSULTA ``desconhecido``. A escada
    da original acaba em ``em_andamento``, e depois o aviso de bloqueio diz ``desconhecido``: a ficha do pedido igual
    nao oferece o numero novo, e a confirmacao com ele e recusada (pelo codigo e pelo banco)."""
    m = mundo
    maximo = m.p.BADGE_LOOKUP_MAX_ATTEMPTS
    op = await m.confirmar(await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA))))
    await m.sair_com_numero(op, "nao_confirmada")
    for _ in range(maximo):
        await m.informar(op, "em_andamento")
    b = await _bloquear(m)
    await _avisar(m, b, {op.id: "desconhecido"})
    await _encerrar(m, b)
    await m.decidir(await m.por_id(op.id), "nao_aconteceu")
    t = await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA)))
    assert t.repete_protocolo == op.protocolo
    assert t.original is not None
    assert (t.original.ultima_consulta, t.original.tentativas) == ("em_andamento", maximo)
    assert not t.original_ficou_em_desconhecido(maximo)
    assert t.ficha_id is not None
    with pytest.raises(fila.DecisaoRecusada) as recusa:
        async with m.entrada_db.begin() as c:
            await fila.confirmar_ficha(
                c,
                t.ficha_id,
                pessoa_id=m.pessoas["ana"],
                com_passkey=True,
                espera_segundos=0,
                rapida_segundos=0,
                numero_novo=True,
                maximo_consultas=maximo,
            )
    assert recusa.value.motivo == "numero_novo_nao_cabe"


@pytest.mark.parametrize("dito_no_aviso", contrato.ESTADOS_QUE_IMPEDEM_NUMERO_NOVO)
async def test_o_aviso_que_diz_que_fez_impede_o_numero_novo(
    mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch, dito_no_aviso: str
) -> None:
    """Conferencia final da etapa 3r (decisao do coordenador). A consulta da original O termina em ``desconhecido``
    em todas as tentativas; depois um bloqueio, e o sistema responde ao aviso que a escrita de O foi feita
    (``concluida`` ou ``parcial``). A resposta ao aviso nao e final (O continua sem estado final, e a pessoa decide
    ``nao_aconteceu``), mas o pedido igual T nao oferece o numero novo: a ficha diria "o sistema nao sabe se o primeiro
    foi feito", e ele disse que foi. O codigo recusa, a guarda do banco tambem (com o codigo enganado), e, sem a
    escolha, T leva o mesmo numero."""
    m = mundo
    maximo = m.p.BADGE_LOOKUP_MAX_ATTEMPTS
    o = await m.confirmar(await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA))))
    envio_o = await m.sair_com_numero(o, "nao_confirmada")
    for _ in range(maximo):
        await m.informar(o, "desconhecido")
    b = await _bloquear(m)
    await _avisar(m, b, {o.id: dito_no_aviso})
    await _encerrar(m, b)
    atual = await m.por_id(o.id)
    assert atual.informado is not None
    assert (atual.informado.origem, atual.informado.estado, atual.informado.estado_final) == (
        "aviso",
        dito_no_aviso,
        None,
    )
    await m.decidir(atual, "nao_aconteceu")
    t = await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA)))
    assert t.repete_protocolo == o.protocolo
    assert t.original is not None
    assert (t.original.estado_final_do_numero, t.original.ultima_consulta) == (None, "desconhecido")
    assert t.original.dito_feito_no_numero is True
    assert not t.original_ficou_em_desconhecido(maximo)
    assert t.ficha_id is not None

    async def numero_novo() -> fila.Operacao:
        assert t.ficha_id is not None
        async with m.entrada_db.begin() as c:
            return await fila.confirmar_ficha(
                c,
                t.ficha_id,
                pessoa_id=m.pessoas["ana"],
                com_passkey=True,
                espera_segundos=0,
                rapida_segundos=0,
                numero_novo=True,
                maximo_consultas=maximo,
            )

    with pytest.raises(fila.DecisaoRecusada) as recusa:
        await numero_novo()
    assert recusa.value.motivo == "numero_novo_nao_cabe"
    with monkeypatch.context() as mp:
        mp.setattr(fila.Operacao, "original_ficou_em_desconhecido", lambda self, maximo: True)
        with pytest.raises(fila.DecisaoRecusada) as recusa:
            await numero_novo()
    assert recusa.value.motivo == "numero_novo_nao_cabe"
    depois = await m.por_id(t.id)
    assert (depois.ficha_decisao, depois.ficha_numero_novo) == (None, False)
    envio_t = await m.sair_com_numero(await m.confirmar(depois))
    assert (envio_t.chave, envio_t.repete_operacao_id) == (envio_o.chave, o.id)


async def test_a_resposta_ao_aviso_depois_de_sistema_fora_nao_reabre_a_escada_da_original(mundo: MundoSelo) -> None:
    """Conferencia final da etapa 3r (decisao do coordenador): a escada da original conta o ``sistema_fora`` pela
    ultima linha de origem ``consulta``. A consulta de O diz ``desconhecido`` e depois para em ``sistema_fora``; a
    resposta ao aviso de bloqueio que entra depois (``em_andamento``, que nao diz que a escrita foi feita) nao reabre
    a escada: O segue em "o sistema nao confirmou; a decisao e sua", e o pedido igual T oferece o numero novo."""
    m = mundo
    maximo = m.p.BADGE_LOOKUP_MAX_ATTEMPTS
    o = await m.confirmar(await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA))))
    await m.sair_com_numero(o, "nao_confirmada")
    await m.informar(o, "desconhecido")
    await m.informar(o, None, desfecho="sistema_fora")
    b = await _bloquear(m)
    await _avisar(m, b, {o.id: "em_andamento"})
    await _encerrar(m, b)
    atual = await m.por_id(o.id)
    assert atual.informado is not None
    assert (atual.informado.origem, atual.informado.ultimo_desfecho, atual.informado.tentativas) == (
        "aviso",
        "sistema_fora",
        1,
    )
    assert atual.consulta_acabou(maximo)
    await m.decidir(atual, "nao_aconteceu")
    t = await m.operacao(protocolo_de(await m.agir(ARGUMENTOS_DA_PARADA)))
    assert t.repete_protocolo == o.protocolo
    assert t.original is not None
    assert (t.original.ultimo_desfecho, t.original.dito_feito_no_numero) == ("sistema_fora", False)
    assert t.original_ficou_em_desconhecido(maximo)
