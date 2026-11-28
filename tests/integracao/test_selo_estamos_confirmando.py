""" "Estamos confirmando" nos niveis 2 e 3 (Fase 4, grupo G4; desenho 5.3, 5.4 e 5.5, criticas A3.3, M4 e D19).

A porta de saida e a consulta (G3a) sao montadas pelo banco, como dono (``tests/apoio_selos.py``): o envio com numero
e as respostas do sistema. O caminho inteiro (porta, duble que conta as chamadas, consulta) e prova do G9.

- ``Operacao.informado`` traz a ultima resposta do sistema, e so o estado final do banco (``selo_estado_final``)
  resolve o "estamos confirmando": uma ``nao_recebida`` sozinha nao resolve.
- O que o sistema informa nao muda a fila, a decisao da pessoa nem o pedido igual (``conta_como_repeticao``); a
  consulta do aplicativo mostra o que ele disse, so com o vocabulario do contrato.
- O pedido igual depois da decisao (ou fora da janela) leva ``repete_operacao_id`` com a "nao confirmada" mais recente
  de mesmos argumentos, ate 7 dias, sem a decisao ``aconteceu``; o banco decide no envio se o numero e o mesmo. Um
  numero cujo desfecho ja se sabe (uma acao com ele ``enviada`` ou com ``aconteceu``) nao e repetido.
- O aplicativo ve o fim da escada ("a decisao fica com a pessoa") e, depois da decisao, nao ouve que a pessoa ainda
  pode registrar.
- "Registrar o que o sistema informou" grava a decisao da pessoa so com estado final; a pessoa que contraria o sistema
  gera ``sistema_contradito`` e aviso ao ADMIN, e o selo fica.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from tests.integracao.test_selo_g4_apoio import (
    ARGUMENTOS,
    META_INFORMADO,
    META_SELO,
    MundoSelo,
    meta,
    protocolo_de,
    subir_mundo_selo,
    textos,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

ADMIN_EMAIL = "admin-g4@exemplo.teste"


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[MundoSelo]:
    async with subir_mundo_selo(banco_migrado) as m:
        await m.gravar_teste(nivel=2)
        yield m


async def nao_confirmada(m: MundoSelo, argumentos: dict[str, Any] | None = None) -> tuple[fila.Operacao, Any]:
    """Uma acao da Ana que saiu com numero e ficou "nao confirmada" (a porta, feita pelo dono). Devolve a operacao e
    o envio (com o numero, que so o dono le)."""
    r = await m.agir(argumentos)
    op = await m.confirmar(await m.operacao(protocolo_de(r)))
    envio = await m.sair_com_numero(op)
    return await m.por_id(op.id), envio


async def nao_recebida_confirmada(m: MundoSelo, op: fila.Operacao) -> None:
    """Duas respostas ``nao_recebida`` com 2 minutos entre elas (critica A3.3)."""
    primeira = await m.informar(op, "nao_recebida")
    await m.recuar_informacao(primeira.id, 2)
    await m.informar(op, "nao_recebida")


async def envelhecer(m: MundoSelo, op: fila.Operacao, *, minutos: int) -> None:
    """Poe a operacao e a ultima mudanca da fila ``minutos`` antes de agora (fora da janela do pedido igual). O dono
    desliga a guarda da fila so nesta transacao."""
    async with m.dono.begin() as c:
        await c.execute(
            text("UPDATE operacoes SET criada_em = now() - make_interval(mins => :m) WHERE id = :o"),
            {"m": minutos, "o": op.id},
        )
        await c.execute(text("ALTER TABLE fila_saida DISABLE TRIGGER fila_saida_guarda"))
        await c.execute(
            text("UPDATE fila_saida SET mudou_em = now() - make_interval(mins => :m) WHERE operacao_id = :o"),
            {"m": minutos, "o": op.id},
        )
        await c.execute(text("ALTER TABLE fila_saida ENABLE TRIGGER fila_saida_guarda"))


async def decisoes(m: MundoSelo, op: fila.Operacao) -> list[Any]:
    async with m.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT decisao, observacao FROM decisoes_nao_confirmada WHERE operacao_id = :o"), {"o": op.id}
                )
            ).all()
        )


# --- Item 1: o que o sistema informou e o estado final ---------------------------------------------------------


async def test_informado_e_a_ultima_resposta_e_so_o_estado_final_resolve(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    assert op.envio_nivel == 2
    assert op.informado is None  # ainda nada perguntado
    assert op.nivel_do_selo == 2

    await m.informar(op, None)  # consulta sem resposta
    op = await m.por_id(op.id)
    assert op.informado is not None
    informado = op.informado
    assert (informado.estado, informado.desfecho, informado.origem, informado.estado_final) == (
        None,
        "sem_resposta",
        "consulta",
        None,
    )
    assert informado.tentativas == 1
    assert informado.ultimo_desfecho == "sem_resposta"
    assert informado.de_terceiro is False
    assert informado.gravado_em <= datetime.now(UTC)

    primeira = await m.informar(op, "nao_recebida")
    op = await m.por_id(op.id)
    assert op.informado is not None
    assert (op.informado.estado, op.informado.estado_final) == ("nao_recebida", None)  # sozinha nao resolve
    assert op.informado.tentativas == 2

    await m.recuar_informacao(primeira.id, 2)
    await m.informar(op, None, origem="consulta", desfecho="sem_chave")  # nao gasta tentativa
    op = await m.por_id(op.id)
    assert op.informado is not None
    assert (op.informado.estado, op.informado.desfecho) == ("nao_recebida", "respondeu")  # a ultima com respondeu
    assert op.informado.ultimo_desfecho == "sem_chave"
    assert op.informado.tentativas == 2

    await m.informar(op, "nao_recebida")
    op = await m.por_id(op.id)
    assert op.informado is not None
    assert op.informado.estado_final == "nao_recebida"  # duas, com intervalo: confirmada
    assert op.estado == Estado.NAO_CONFIRMADA  # a fila nao muda
    assert op.decisao_pessoa is None  # nem a decisao da pessoa


async def test_consulta_acaba_no_fim_da_escada_sem_resposta_final(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    for _ in range(3):
        await m.informar(op, "desconhecido")
    op = await m.por_id(op.id)
    assert op.informado is not None
    assert op.informado.tentativas == 3
    assert not op.consulta_acabou(4)
    assert op.consulta_acabou(3)
    textos_app = textos(await m.consultar(op.protocolo))
    assert any("ainda sem resposta final" in t for t in textos_app)


@pytest.mark.parametrize(
    ("estado", "frase"),
    [
        (None, "O MCP-CLARO perguntou ao sistema o que houve e ele nao confirmou; a decisao fica com a pessoa."),
        ("desconhecido", "informou: desconhecido, mas nao deu uma resposta final; a decisao fica com a pessoa."),
    ],
)
async def test_o_aplicativo_ve_o_fim_da_escada_na_consulta_e_no_pedido_igual(
    mundo: MundoSelo, estado: str | None, frase: str
) -> None:
    """Depois de ``BADGE_LOOKUP_MAX_ATTEMPTS`` consultas sem resposta final, a consulta pelo protocolo e o pedido igual
    (que devolve a original) dizem ao aplicativo que a decisao fica com a pessoa; uma antes, ainda nao."""
    m = mundo
    maximo = m.p.BADGE_LOOKUP_MAX_ATTEMPTS
    op, _ = await nao_confirmada(m)
    for _ in range(maximo - 1):
        await m.informar(op, estado)
    [antes] = textos(await m.consultar(op.protocolo))
    assert "a decisao fica com a pessoa" not in antes
    await m.informar(op, estado)
    [consulta] = textos(await m.consultar(op.protocolo))
    assert frase in consulta
    repetida = await m.agir()
    assert protocolo_de(repetida) == op.protocolo
    [texto] = textos(repetida)
    assert frase in texto


async def test_depois_da_decisao_o_aplicativo_nao_ouve_que_a_pessoa_ainda_pode_registrar(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await m.informar(op, "concluida")
    [antes] = textos(await m.consultar(op.protocolo))
    assert "A pessoa ainda pode registrar o que viu" in antes
    await m.decidir(op, "nao_aconteceu")
    [depois] = textos(await m.consultar(op.protocolo))
    assert "ainda pode registrar" not in depois
    assert depois.endswith("A pessoa registrou que nao aconteceu no sistema.")


# --- A resposta do sistema nao muda a fila nem o pedido igual (D15) -------------------------------------------


async def test_a_resposta_do_sistema_nao_muda_o_pedido_igual_e_aparece_na_consulta(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await nao_recebida_confirmada(m, op)
    consulta = await m.consultar(op.protocolo)
    assert meta(consulta)[META_INFORMADO] == {"estado": "nao_recebida", "final": True}
    assert meta(consulta)[META_SELO] == 2
    [texto] = textos(consulta)
    assert f"O sistema {m.sistema.nome} informou: nao_recebida." in texto
    assert "Nada sera reenviado" in texto

    repetida = await m.agir()  # o pedido igual na janela devolve a original
    assert protocolo_de(repetida) == op.protocolo
    assert meta(repetida)[PREFIXO_REPETIDO] is True
    depois = await m.por_id(op.id)
    assert depois.estado == Estado.NAO_CONFIRMADA
    assert depois.conta_como_repeticao()
    assert await decisoes(m, op) == []


async def test_nao_recebida_sozinha_aparece_como_dita_e_continua_perguntando(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await m.informar(op, "nao_recebida")
    consulta = await m.consultar(op.protocolo)
    assert meta(consulta)[META_INFORMADO] == {"estado": "nao_recebida", "final": False}
    [texto] = textos(consulta)
    assert "nao_recebida, ainda sem resposta final; o MCP-CLARO continua perguntando." in texto


async def test_sem_resposta_ainda_o_aplicativo_ve_que_o_mcp_claro_pergunta(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    consulta = await m.consultar(op.protocolo)
    assert META_INFORMADO not in meta(consulta)
    [texto] = textos(consulta)
    assert "O MCP-CLARO esta perguntando ao sistema o que houve" in texto


# --- Item 2: pedido igual com o mesmo numero (D16, D17) -------------------------------------------------------


async def test_pedido_igual_depois_da_decisao_repete_e_o_numero_queimado_e_novo(mundo: MundoSelo) -> None:
    m = mundo
    op, envio = await nao_confirmada(m)
    await nao_recebida_confirmada(m, op)
    await m.decidir(op, "nao_aconteceu")
    nova = await m.operacao(protocolo_de(await m.agir()))
    assert nova.id != op.id
    assert nova.repete_protocolo == op.protocolo
    nova = await m.confirmar(nova)
    envio_novo = await m.sair_com_numero(nova)
    assert envio_novo.repete_operacao_id is None  # o sistema disse que nao recebeu: numero queimado
    assert envio_novo.chave != envio.chave
    assert (await m.por_id(nova.id)).mesmo_numero_de is None
    assert (await m.por_id(op.id)).substituto is None


async def test_pedido_igual_sem_resposta_final_sai_com_o_mesmo_numero(mundo: MundoSelo) -> None:
    m = mundo
    op, envio = await nao_confirmada(m)
    await m.informar(op, None)  # consulta sem resposta
    await m.informar(op, "nao_recebida")  # uma so nao queima (critica A3.3)
    await m.decidir(op, "nao_aconteceu")
    nova = await m.confirmar(await m.operacao(protocolo_de(await m.agir())))
    assert nova.repete_protocolo == op.protocolo
    envio_novo = await m.sair_com_numero(nova)
    assert envio_novo.chave == envio.chave  # o banco copiou o numero da original
    assert envio_novo.repete_operacao_id == op.id
    assert (await m.por_id(nova.id)).mesmo_numero_de == op.protocolo
    original = await m.por_id(op.id)
    assert original.substituto == nova.protocolo  # o resultado aparece no pedido igual
    texto = textos(await m.consultar(op.protocolo))[0]
    assert f"O protocolo {nova.protocolo} saiu depois ao sistema com o mesmo numero" in texto


async def test_pedido_igual_fora_da_janela_sem_decisao_tambem_repete(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await envelhecer(m, op, minutos=60 * 24)  # fora da janela (30 minutos), dentro dos 7 dias
    nova = await m.operacao(protocolo_de(await m.agir()))
    assert nova.id != op.id
    assert nova.repete_protocolo == op.protocolo


async def test_pedido_igual_repete_a_nao_confirmada_mais_recente(mundo: MundoSelo) -> None:
    m = mundo
    antiga, _ = await nao_confirmada(m)
    await m.decidir(antiga, "nao_aconteceu")
    recente = await m.confirmar(await m.operacao(protocolo_de(await m.agir())))
    await m.sair_com_numero(recente)
    await m.decidir(await m.por_id(recente.id), "nao_aconteceu")
    terceira = await m.operacao(protocolo_de(await m.agir()))
    assert terceira.repete_protocolo == recente.protocolo


async def _pedido_igual_que_sai(m: MundoSelo, original: fila.Operacao, fim: str) -> tuple[fila.Operacao, Any]:
    """Um pedido igual fora da janela de ``original`` (ela fica 60 minutos para tras): confirmado e saido com numero
    ate ``fim``. Devolve a operacao e o envio."""
    await envelhecer(m, original, minutos=60)
    nova = await m.confirmar(await m.operacao(protocolo_de(await m.agir())))
    envio = await m.sair_com_numero(nova, fim)
    return await m.por_id(nova.id), envio


async def test_pedido_igual_nao_repete_o_numero_que_ja_saiu_enviado(mundo: MundoSelo) -> None:
    """O fica nao confirmada; R repete O e sai ``enviada`` com o numero de O: o sistema ja recebeu essa escrita e
    respondeu. T, outro pedido igual fora da janela de R, e um pedido novo: sai com numero novo (com o de O, um
    sistema honesto responderia "ja feito" e nao faria). O substituto de O e R, sem desempate."""
    m = mundo
    o, envio_o = await nao_confirmada(m)
    r, envio_r = await _pedido_igual_que_sai(m, o, "enviada")
    assert r.repete_protocolo == o.protocolo
    assert envio_r.chave == envio_o.chave
    t, envio_t = await _pedido_igual_que_sai(m, r, "nao_confirmada")
    assert t.repete_protocolo is None
    assert envio_t.chave != envio_o.chave
    assert envio_t.repete_operacao_id is None
    assert (await m.por_id(o.id)).substituto == r.protocolo
    assert t.mesmo_numero_de is None


async def test_pedido_igual_nao_repete_o_numero_que_a_pessoa_disse_que_aconteceu(mundo: MundoSelo) -> None:
    """O fica nao confirmada; R repete O e tambem fica nao confirmada, com o mesmo numero. A pessoa registra que O
    aconteceu: o numero ja fez a escrita. T nao repete R (a mais recente, sem decisao): sai com numero novo."""
    m = mundo
    o, envio_o = await nao_confirmada(m)
    r, envio_r = await _pedido_igual_que_sai(m, o, "nao_confirmada")
    assert envio_r.chave == envio_o.chave
    await m.decidir(o, "aconteceu")
    t, envio_t = await _pedido_igual_que_sai(m, r, "nao_confirmada")
    assert t.repete_protocolo is None
    assert envio_t.chave != envio_o.chave


async def test_pedido_igual_segue_a_cadeia_do_numero_sem_desfecho(mundo: MundoSelo) -> None:
    """Sem desfecho do numero (O e R nao confirmadas, sem decisao), T repete a mais recente (R) e leva o mesmo
    numero. A cadeia e O, R, T: o substituto de O e T."""
    m = mundo
    o, envio_o = await nao_confirmada(m)
    r, _ = await _pedido_igual_que_sai(m, o, "nao_confirmada")
    t, envio_t = await _pedido_igual_que_sai(m, r, "nao_confirmada")
    assert t.repete_protocolo == r.protocolo
    assert envio_t.chave == envio_o.chave
    assert t.mesmo_numero_de == r.protocolo
    assert (await m.por_id(o.id)).substituto == t.protocolo
    assert (await m.por_id(r.id)).substituto == t.protocolo


@pytest.mark.parametrize("caso", ["aconteceu", "mais_de_7_dias", "outros_argumentos", "sem_envio"])
async def test_pedido_igual_nao_repete_fora_da_regra(mundo: MundoSelo, caso: str) -> None:
    m = mundo
    if caso == "sem_envio":
        # Saiu sem numero (nivel 1 na hora, ou a porta nao gravou o envio): nao ha numero para repetir (critica A1).
        op = await m.confirmar(await m.operacao(protocolo_de(await m.agir())))
        async with m.dono.begin() as c:
            await c.execute(
                text("UPDATE fila_saida SET estado = 'enviando', mudou_por = 'despachante' WHERE operacao_id = :o"),
                {"o": op.id},
            )
            await c.execute(
                text("UPDATE fila_saida SET estado = 'nao_confirmada' WHERE operacao_id = :o"), {"o": op.id}
            )
    else:
        op, _ = await nao_confirmada(m)
    argumentos = dict(ARGUMENTOS)
    if caso == "aconteceu":
        await m.decidir(op, "aconteceu")
        await envelhecer(m, op, minutos=60)  # fora da janela: o pedido igual nao devolve a original
    elif caso == "mais_de_7_dias":
        await m.decidir(op, "nao_aconteceu")
        await envelhecer(m, op, minutos=60 * 24 * 7 + 5)
    elif caso == "outros_argumentos":
        await m.decidir(op, "nao_aconteceu")
        argumentos["texto"] = "outro texto"
    else:
        await m.decidir(await m.por_id(op.id), "nao_aconteceu")
    nova = await m.operacao(protocolo_de(await m.agir(argumentos)))
    assert nova.id != op.id
    assert nova.repete_protocolo is None
    async with m.entrada_db.connect() as c:  # a regra e da entrada, e nao so da guarda do banco
        linha = (
            await c.execute(
                text("SELECT chave_pedido_igual, codigo_conferencia FROM operacoes WHERE id = :o"), {"o": nova.id}
            )
        ).one()
        achada = await fila.original_que_repete(
            c,
            chave=linha.chave_pedido_igual,
            codigo=linha.codigo_conferencia,
            sistema_id=m.sistema.id,
            pessoa_id=m.pessoas["ana"],
        )
    assert achada is None


async def test_repeticao_recusada_pelo_banco_vira_operacao_sem_repeticao(
    mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guarda do banco (``operacoes_repete_confere``) recusa a repeticao (por exemplo, a decisao ``aconteceu`` que
    chegou entre a busca e o INSERT): a operacao nasce sem ela, com numero novo no envio, e o pedido nao falha."""
    m = mundo
    op, _ = await nao_confirmada(m)
    await m.decidir(op, "aconteceu")

    async def a_original_que_o_banco_recusa(*_: Any, **__: Any) -> Any:
        return op.id

    monkeypatch.setattr(fila, "original_que_repete", a_original_que_o_banco_recusa)
    await envelhecer(m, op, minutos=60)
    r = await m.agir()
    nova = await m.operacao(protocolo_de(r))
    assert nova.id != op.id
    assert nova.repete_protocolo is None
    assert nova.estado == Estado.AGUARDANDO_FICHA


# --- Item 3: "Registrar o que o sistema informou" e a pessoa que contraria o sistema (D19) -------------------


@pytest.mark.parametrize(("estado", "decisao"), [("concluida", "aconteceu"), ("recusada", "nao_aconteceu")])
async def test_registrar_o_que_o_sistema_informou_grava_a_decisao_da_pessoa(
    mundo: MundoSelo, estado: str, decisao: str
) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await m.informar(op, estado)
    async with m.entrada_db.begin() as c:
        depois = await fila.registrar_o_que_o_sistema_informou(c, op.id, pessoa_id=m.pessoas["ana"])
    assert depois.decisao_pessoa == decisao
    assert [(d.decisao, d.observacao) for d in await decisoes(m, op)] == [(decisao, fila.OBSERVACAO_DO_SISTEMA)]
    assert await m.extrato("sistema_contradito", op.protocolo) == []
    assert depois.estado == Estado.NAO_CONFIRMADA  # nada e reenviado


async def test_registrar_nao_recebida_so_com_ela_confirmada(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    primeira = await m.informar(op, "nao_recebida")
    with pytest.raises(fila.DecisaoRecusada) as recusa:
        async with m.entrada_db.begin() as c:
            await fila.registrar_o_que_o_sistema_informou(c, op.id, pessoa_id=m.pessoas["ana"])
    assert recusa.value.motivo == "sem_resposta_final"
    assert await decisoes(m, op) == []
    await m.recuar_informacao(primeira.id, 2)
    await m.informar(op, "nao_recebida")
    async with m.entrada_db.begin() as c:
        depois = await fila.registrar_o_que_o_sistema_informou(c, op.id, pessoa_id=m.pessoas["ana"])
    assert depois.decisao_pessoa == "nao_aconteceu"


@pytest.mark.parametrize("caso", ["sem_resposta", "parcial", "substituto", "outra_pessoa"])
async def test_registrar_o_que_o_sistema_informou_recusa(mundo: MundoSelo, caso: str) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    pessoa = m.pessoas["ana"]
    motivo = {
        "sem_resposta": "sem_resposta_final",
        "parcial": "decida_pelo_formulario",
        "substituto": "resultado_em_outro_protocolo",
        "outra_pessoa": "operacao_desconhecida",
    }[caso]
    if caso == "sem_resposta":
        await m.informar(op, "desconhecido")
    elif caso == "parcial":
        await m.informar(op, "parcial")
    elif caso == "substituto":
        await m.informar(op, "concluida")
        await envelhecer(m, op, minutos=60)
        nova = await m.confirmar(await m.operacao(protocolo_de(await m.agir())))
        await m.sair_com_numero(nova, "enviada")  # o pedido igual levou o numero da original
    else:
        await m.informar(op, "concluida")
        pessoa = m.pessoas["real"]
    with pytest.raises(fila.DecisaoRecusada) as recusa:
        async with m.entrada_db.begin() as c:
            await fila.registrar_o_que_o_sistema_informou(c, op.id, pessoa_id=pessoa)
    assert recusa.value.motivo == motivo
    assert recusa.value.mensagem.isascii()
    assert await decisoes(m, op) == []


async def test_a_pessoa_que_contraria_o_sistema_avisa_o_admin_e_o_selo_fica(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await m.informar(op, "concluida")
    await m.decidir(op, "nao_aconteceu", emails_admin=[ADMIN_EMAIL])
    [linha] = await m.extrato("sistema_contradito", op.protocolo)
    assert linha.dados == {"estado": "concluida", "decisao": "nao_aconteceu", "de_terceiro": False}
    assert m.sistema.nome in linha.frase
    async with m.dono.connect() as c:
        avisos = (
            await c.execute(
                text("SELECT canal, destino, evento, corpo FROM avisos WHERE chave_unica LIKE :k"),
                {"k": f"sistema_contradito:{op.id}%"},
            )
        ).all()
    assert [(a.canal, a.destino, a.evento) for a in avisos] == [("email", ADMIN_EMAIL, "sistema_contradito")]
    assert avisos[0].corpo["protocolo"] == op.protocolo
    vigente = await m.selo_vigente()
    assert (vigente.nivel, vigente.motivo) == (2, "vigente")  # o selo fica (D19)

    # Quem concorda com o sistema nao gera nada.
    outra, _ = await nao_confirmada(m, {"para": "outra@exemplo.teste", "texto": "y"})
    await m.informar(outra, "concluida")
    await m.decidir(outra, "aconteceu", emails_admin=[ADMIN_EMAIL])
    assert await m.extrato("sistema_contradito", outra.protocolo) == []


async def test_nao_sei_nunca_contraria(mundo: MundoSelo) -> None:
    m = mundo
    op, _ = await nao_confirmada(m)
    await m.informar(op, "concluida")
    await m.decidir(op, "nao_sei")
    assert await m.extrato("sistema_contradito", op.protocolo) == []


PREFIXO_REPETIDO = "br.com.claro.mcp-claro/repetido"
