"""Quarentena de um servidor de terceiros na porta de saida (Fase 3, G4): ``saida.quarentena.por_em_quarentena``.

Contra PostgreSQL real, com os usuarios ``mcp_claro_rotinas`` (mudou-parou) e ``mcp_claro_despachante`` (sondagem,
D29): o que espera a ficha ou esta liberado vira ``cancelada`` com ``acao_parada`` no extrato de cada pessoa; o que
esta "enviando" segue; os limites ativos do sistema ficam suspensos (correcao M2); outro sistema nao muda; a
quarentena contra uma revisao que ja nao vale nao faz nada (correcao B1); quarentena e envio ao mesmo tempo, so um
acontece; pedido durante a quarentena e recusado sem operacao nova; depois da nova revisao, a acao vai para a ficha;
a tela que encerra um limite e a quarentena do mesmo servidor ao mesmo tempo nao dao impasse no banco.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro.cadastro.quarentena import MOTIVO_PARADA, MOTIVO_QUARENTENA, Diferenca
from mcp_claro.config.settings import Parametros
from mcp_claro.saida import fila, travas
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import RECUSA_QUARENTENA, AcaoRecusada, Pedidos
from mcp_claro.saida.quarentena import MOTIVO_LIMITE, MOTIVO_SEM_RESPOSTA, por_em_quarentena
from mcp_claro.telas import consultas
from mcp_claro.telas.limites import cancelar_liberadas
from mcp_claro.versao import PREFIXO_META
from tests import apoio_terceiros as t3
from tests.integracao.test_entrada_terceiros_apoio import Mundo, subir_mundo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

PAPEIS = {"rotinas": "rotinas_db", "despachante": "despachante_db"}


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado) as m:
        yield m


def protocolo(r: Any) -> str:
    return str((r.meta or {})[PREFIXO_META + "protocolo"])


async def quarentena(m: Mundo, papel: str = "rotinas", **kw: Any) -> Any:
    engine = getattr(m, PAPEIS[papel])
    kw.setdefault("base", m.revisao_id)
    async with engine.begin() as c:
        return await por_em_quarentena(c, sistema_id=m.terceiro_id, papel=papel, **kw)


async def sistema(m: Mundo) -> Any:
    async with m.dono.connect() as c:
        return (
            await c.execute(
                text("SELECT situacao, motivo, revisao_servidor_id FROM sistemas WHERE id = :s"), {"s": m.terceiro_id}
            )
        ).one()


async def limite(m: Mundo, i: uuid.UUID) -> Any:
    async with m.dono.connect() as c:
        return (await c.execute(text("SELECT situacao, motivo FROM limites WHERE id = :l"), {"l": i})).one()


async def contar_operacoes(m: Mundo) -> int:
    async with m.dono.connect() as c:
        return int(
            (
                await c.execute(
                    text("SELECT count(*) FROM operacoes WHERE aplicativo_id = ANY(:a)"), {"a": list(m.apps.values())}
                )
            ).scalar_one()
        )


async def preparar(m: Mundo) -> dict[str, Any]:
    """Na fila do terceiro: uma acao esperando a ficha (Ana) e uma liberada por limite, na espera (Bia). No
    proprio: uma esperando a ficha (Ana). Limites ativos do terceiro (Ana e Bia) e do proprio (Ana)."""
    # Nenhuma porta de saida roda: a liberada fica na espera (OUTBOX_HOLD_SECONDS) ate o fim do teste.
    limite_bia = await m.limite("bia", "t_enviar")
    so_equipe = [{"campo": "destinatario", "valores": ["equipe"]}]
    limite_ana = await m.limite("ana", "t_enviar", destinatarios=so_equipe)
    limite_proprio = await m.limite("ana", "p_mensagem", destinatarios=so_equipe)
    ficha = await m.chamar("ana", "t_enviar", {"destinatario": "fora", "texto": "espera a ficha"})
    liberada = await m.chamar("bia", "t_enviar", {"texto": "liberada"})
    do_proprio = await m.chamar("ana", "p_mensagem", {"destinatario": "outra", "texto": "do proprio"})
    ops = {
        "ficha": await m.operacao(protocolo(ficha)),
        "liberada": await m.operacao(protocolo(liberada)),
        "proprio": await m.operacao(protocolo(do_proprio)),
    }
    assert ops["ficha"].estado == Estado.AGUARDANDO_FICHA
    assert ops["liberada"].estado == Estado.LIBERADA
    assert ops["liberada"].limite_id == limite_bia
    assert ops["proprio"].estado == Estado.AGUARDANDO_FICHA
    return {"ops": ops, "limites": {"ana": limite_ana, "bia": limite_bia, "proprio": limite_proprio}}


@pytest.mark.parametrize("papel", ["rotinas", "despachante"])
async def test_quarentena_cancela_o_que_espera_e_suspende_os_limites(mundo: Mundo, papel: str) -> None:
    m = mundo
    arranjo = await preparar(m)
    ops, limites = arranjo["ops"], arranjo["limites"]
    diferencas = [Diferenca("enviar_resposta", "mudou", "sha256:" + "1" * 64, "sha256:" + "2" * 64)]
    feito = await quarentena(m, papel, diferencas=diferencas)
    assert feito is not None
    assert feito.nome == m.terceiro_nome
    assert set(feito.paradas) == {ops["ficha"].protocolo, ops["liberada"].protocolo}
    assert feito.limites_suspensos == 2

    s = await sistema(m)
    assert (s.situacao, s.motivo, s.revisao_servidor_id) == ("quarentena", MOTIVO_QUARENTENA, None)
    for chave in ("ficha", "liberada"):
        op = await m.operacao(ops[chave].protocolo)
        assert op.estado == Estado.CANCELADA
        assert op.desfecho == MOTIVO_PARADA
        assert op.mudou_por == f"quarentena:{papel}"
        linhas = await m.extrato("acao_parada", protocolo=op.protocolo)
        assert len(linhas) == 1
        assert linhas[0].pessoa_id == op.pessoa_id
        assert linhas[0].papel == papel
        assert MOTIVO_PARADA in linhas[0].frase
    # Outro sistema nao muda.
    assert (await m.operacao(ops["proprio"].protocolo)).estado == Estado.AGUARDANDO_FICHA
    assert (await limite(m, limites["proprio"])).situacao == "ativo"
    for pessoa in ("ana", "bia"):
        lim = await limite(m, limites[pessoa])
        assert (lim.situacao, lim.motivo) == ("suspenso", MOTIVO_LIMITE)
        linhas = await m.extrato("limite_suspenso", pessoa=pessoa)
        assert len(linhas) == 1
        assert linhas[0].frase == f"O limite de {m.f['t_enviar'].nome} foi suspenso: {MOTIVO_LIMITE}."
        assert linhas[0].dados == {"limite_id": str(limites[pessoa]), "situacao": "suspenso"}

    linhas = await m.extrato("sistema_em_quarentena")
    assert len(linhas) == 1
    assert linhas[0].papel == papel
    assert linhas[0].pessoa_id is None
    assert linhas[0].frase.startswith(f"Servidor {m.terceiro_nome} em quarentena: mudou, parou.")
    dados = linhas[0].dados
    assert dados["revisao"] == str(m.revisao_id)
    assert dados["ferramentas"] == [d.retrato() for d in diferencas]
    assert dados["mudancas"] == 1
    assert sorted(dados["paradas"]) == sorted(feito.paradas)
    assert dados["limites_suspensos"] == 2

    # A ficha da acao cancelada nao confirma mais.
    with pytest.raises(fila.DecisaoRecusada) as erro:
        await m.confirmar(ops["ficha"])
    assert erro.value.motivo == "nao_esta_aguardando"
    assert (await m.operacao(ops["ficha"].protocolo)).estado == Estado.CANCELADA


async def test_quarentena_contra_revisao_que_ja_nao_vale_nao_faz_nada(mundo: Mundo) -> None:
    m = mundo
    arranjo = await preparar(m)
    # O ADMIN concluiu uma revisao nova depois de as rotinas lerem a antiga (correcao B1).
    async with m.dono.begin() as c:
        nova = await t3.gravar_revisao_do_servidor(c, m.terceiro_id)
    assert nova != m.revisao_id
    assert await quarentena(m, base=m.revisao_id) is None
    s = await sistema(m)
    assert (s.situacao, s.revisao_servidor_id) == ("ativo", nova)
    assert (await m.operacao(arranjo["ops"]["ficha"].protocolo)).estado == Estado.AGUARDANDO_FICHA
    assert (await limite(m, arranjo["limites"]["bia"])).situacao == "ativo"
    assert await m.extrato("sistema_em_quarentena") == []
    assert await m.extrato("limite_suspenso") == []
    # Contra a revisao nova, vale.
    assert await quarentena(m, base=nova) is not None


async def test_segunda_quarentena_nao_repete(mundo: Mundo) -> None:
    m = mundo
    assert await quarentena(m) is not None
    assert await quarentena(m, "despachante") is None
    assert len(await m.extrato("sistema_em_quarentena")) == 1


async def test_sistema_proprio_nunca_entra_por_aqui(mundo: Mundo) -> None:
    m = mundo
    async with m.rotinas_db.begin() as c:
        assert await por_em_quarentena(c, sistema_id=m.proprio_id, base=m.revisao_id, papel="rotinas") is None
    async with m.dono.connect() as c:
        situacao = (
            await c.execute(text("SELECT situacao FROM sistemas WHERE id = :s"), {"s": m.proprio_id})
        ).scalar_one()
    assert situacao == "ativo"


async def test_quarentena_sem_resposta_grava_as_rodadas(mundo: Mundo) -> None:
    m = mundo
    feito = await quarentena(m, motivo=MOTIVO_SEM_RESPOSTA, rodadas=2)
    assert feito is not None
    assert (await sistema(m)).motivo == MOTIVO_SEM_RESPOSTA
    linhas = await m.extrato("sistema_em_quarentena_sem_resposta")
    assert [linha.frase for linha in linhas] == [
        f"Servidor {m.terceiro_nome} em quarentena: nao respondeu a conferencia em 2 rodadas seguidas; nada dele"
        " segue ate nova revisao do ADMIN."
    ]
    assert await m.extrato("sistema_em_quarentena") == []


async def test_quarentena_sem_resposta_sem_motivo_grava_o_motivo_dela(mundo: Mundo) -> None:
    m = mundo
    assert await quarentena(m, rodadas=2) is not None
    assert (await sistema(m)).motivo == MOTIVO_SEM_RESPOSTA
    assert len(await m.extrato("sistema_em_quarentena_sem_resposta")) == 1


@pytest.mark.parametrize(
    ("kw", "erro"),
    [({"rodadas": 2, "motivo": MOTIVO_QUARENTENA}, "MOTIVO_SEM_RESPOSTA"), ({"rodadas": 0}, "a partir de 1")],
    ids=["motivo_de_mudou", "zero_rodadas"],
)
async def test_quarentena_sem_resposta_com_motivo_que_nao_bate_e_recusada(
    mundo: Mundo, kw: dict[str, Any], erro: str
) -> None:
    with pytest.raises(ValueError, match=erro):
        await quarentena(mundo, **kw)
    assert (await sistema(mundo)).situacao == "ativo"


async def test_papel_que_nao_poe_em_quarentena_e_recusado(mundo: Mundo) -> None:
    async with mundo.entrada_db.begin() as c:
        with pytest.raises(ValueError, match="rotinas ou despachante"):
            await por_em_quarentena(c, sistema_id=mundo.terceiro_id, base=mundo.revisao_id, papel="entrada")
    assert (await sistema(mundo)).situacao == "ativo"


async def test_o_que_esta_enviando_segue(mundo: Mundo) -> None:
    m = mundo
    r = await m.chamar("ana", "t_enviar", {"texto": "ja saindo"})
    op = await m.operacao(protocolo(r))
    await m.confirmar(op)
    async with m.despachante_db.begin() as c:
        atual = await fila.por_id(c, op.id)
        assert atual is not None
        assert await fila.marcar_enviando(c, atual)
    feito = await quarentena(m)
    assert feito is not None
    assert feito.paradas == ()
    assert (await m.operacao(op.protocolo)).estado == Estado.ENVIANDO


async def envio_simulado(m: Mundo, op: fila.Operacao, *, segura: float, comecou: asyncio.Event) -> bool:
    """O que o despachante faz na transacao do envio: trava compartilhada, linha da fila e "enviando"."""
    async with m.despachante_db.begin() as c:
        await travas.compartilhar_envio(c, op.pessoa_id, op.aplicativo_id, op.sistema_id)
        comecou.set()
        await asyncio.sleep(segura)
        travada = (
            await c.execute(
                text("SELECT id FROM fila_saida WHERE id = :i AND estado = 'liberada' FOR UPDATE"), {"i": op.fila_id}
            )
        ).scalar_one_or_none()
        if travada is None:
            return False
        atual = await fila.por_fila(c, op.fila_id)
        assert atual is not None
        return await fila.marcar_enviando(c, atual)


async def quarentena_segurando(m: Mundo, *, segura: float, comecou: asyncio.Event) -> Any:
    async with m.rotinas_db.begin() as c:
        await travas.exclusiva(c, travas.chaves_do_alcance("sistema", sistema_id=m.terceiro_id))
        comecou.set()
        await asyncio.sleep(segura)
        return await por_em_quarentena(c, sistema_id=m.terceiro_id, base=m.revisao_id, papel="rotinas")


@pytest.mark.parametrize("primeiro", ["envio", "quarentena"])
async def test_quarentena_e_envio_ao_mesmo_tempo_so_um_acontece(mundo: Mundo, primeiro: str) -> None:
    m = mundo
    r = await m.chamar("ana", "t_enviar", {"texto": f"disputa {primeiro}"})
    op = await m.operacao(protocolo(r))
    await m.confirmar(op)
    assert (await m.operacao(op.protocolo)).estado == Estado.LIBERADA
    comecou = asyncio.Event()
    if primeiro == "envio":
        envio = asyncio.create_task(envio_simulado(m, op, segura=0.5, comecou=comecou))
        await asyncio.wait_for(comecou.wait(), 5)
        feito = await quarentena(m)
        enviou = await envio
        assert enviou is True
        assert feito is not None
        assert feito.paradas == ()
        assert (await m.operacao(op.protocolo)).estado == Estado.ENVIANDO
    else:
        tarefa = asyncio.create_task(quarentena_segurando(m, segura=0.5, comecou=comecou))
        await asyncio.wait_for(comecou.wait(), 5)
        enviou = await envio_simulado(m, op, segura=0, comecou=asyncio.Event())
        feito = await tarefa
        assert enviou is False
        assert feito is not None
        assert feito.paradas == (op.protocolo,)
        assert (await m.operacao(op.protocolo)).estado == Estado.CANCELADA


async def test_pedido_durante_a_quarentena_e_recusado_sem_operacao_nova(mundo: Mundo) -> None:
    m = mundo
    pedido = await m.pedido("ana", "t_enviar", {"texto": "depois da quarentena"})
    antes = await contar_operacoes(m)
    assert await quarentena(m) is not None
    with pytest.raises(AcaoRecusada) as erro:
        await Pedidos(m.p, m.entrada_db).pedir(pedido)
    assert (erro.value.motivo, erro.value.mensagem) == ("sistema_em_quarentena", RECUSA_QUARENTENA)
    assert await contar_operacoes(m) == antes
    # Pela entrada a ferramenta sumiu da vitrine.
    assert m.f["t_enviar"].nome not in await m.listar("ana")


async def test_pedido_de_terceiro_sem_a_chave_e_recusado_mesmo_entrando_direto(mundo: Mundo) -> None:
    m = mundo
    # A ferramenta lida quando a pessoa ainda podia (a chave ligada), e o pedido ja sem ela.
    pedido = dataclasses.replace(await m.pedido("real", "t_enviar", {"texto": "pessoa real"}), terceiros=False)
    antes = await contar_operacoes(m)
    with pytest.raises(AcaoRecusada) as erro:
        await Pedidos(m.p, m.entrada_db).pedir(pedido)
    assert erro.value.motivo == "fora_da_vitrine"
    assert await contar_operacoes(m) == antes


async def test_pedido_e_quarentena_ao_mesmo_tempo_nada_fica_esperando(mundo: Mundo) -> None:
    """A quarentena segura a trava exclusiva do sistema: o pedido espera e, depois, ve a quarentena."""
    m = mundo
    pedido = await m.pedido("ana", "t_enviar", {"texto": "no meio da quarentena"})
    comecou = asyncio.Event()
    tarefa = asyncio.create_task(quarentena_segurando(m, segura=0.5, comecou=comecou))
    await asyncio.wait_for(comecou.wait(), 5)
    with pytest.raises(AcaoRecusada) as erro:
        await Pedidos(m.p, m.entrada_db).pedir(pedido)
    assert erro.value.motivo == "sistema_em_quarentena"
    assert await tarefa is not None


async def test_depois_da_nova_revisao_a_acao_vai_para_a_ficha_e_nao_pelo_limite(mundo: Mundo) -> None:
    m = mundo
    lim = await m.limite("ana", "t_enviar")
    assert await quarentena(m) is not None
    async with m.dono.begin() as c:
        await t3.gravar_revisao_do_servidor(c, m.terceiro_id)
    r = await m.chamar("ana", "t_enviar", {"texto": "depois da revisao"})
    op = await m.operacao(protocolo(r))
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.limite_id is None
    assert (await limite(m, lim)).situacao == "suspenso"


async def test_nomes_do_fornecedor_no_extrato_vao_com_escondidos_marcados_e_cortados(mundo: Mundo) -> None:
    m = mundo
    diferencas = [
        Diferenca("ler\u202eetnem", "mudou", "sha256:" + "1" * 64, "sha256:" + "2" * 64),
        Diferenca("x" * 300, "nova", None, "sha256:" + "3" * 64),
    ]
    assert await quarentena(m, diferencas=diferencas) is not None
    (linha,) = await m.extrato("sistema_em_quarentena")
    nomes = [f["ferramenta"] for f in linha.dados["ferramentas"]]
    assert nomes == ["ler[U+202E]etnem", "x" * 128]
    assert linha.dados["ferramentas"][1]["mudanca"] == "nova"


async def esperando_trava_de_linha(m: Mundo, pid: int) -> None:
    """Ate o processo ``pid`` do banco estar parado esperando uma trava (``pg_locks.granted = false``). Sem tempo
    fixo: olha o banco ate ver a espera, no maximo 20 s pelo relogio do laco (monotonico)."""
    fim = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < fim:
        async with m.dono.connect() as c:
            esperando = (
                await c.execute(text("SELECT count(*) FROM pg_locks WHERE pid = :p AND NOT granted"), {"p": pid})
            ).scalar_one()
        if esperando:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a quarentena nao chegou a esperar a trava do limite")


async def test_tela_que_encerra_o_limite_e_quarentena_ao_mesmo_tempo_sem_impasse(mundo: Mundo) -> None:
    """A tela (``telas/limites.py``, sem trava consultiva) trava a linha do limite e depois a da fila. A quarentena
    trava as duas na mesma ordem (``suspender_limites`` antes de ``parar_por_quarentena``): ela espera a tela
    terminar, e nenhuma das duas e desfeita pelo banco com "deadlock detected"."""
    m = mundo
    lim = await m.limite("bia", "t_enviar")
    op = await m.operacao(protocolo(await m.chamar("bia", "t_enviar", {"texto": "liberada pelo limite"})))
    assert (op.estado, op.limite_id) == (Estado.LIBERADA, lim)
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def por_em_quarentena_agora() -> Any:
        async with m.rotinas_db.begin() as c:
            pid.set_result(int((await c.execute(text("SELECT pg_backend_pid()"))).scalar_one()))
            return await por_em_quarentena(c, sistema_id=m.terceiro_id, base=m.revisao_id, papel="rotinas")

    async with m.entrada_db.begin() as c:
        # O que a tela faz ao encerrar o limite, na mesma transacao: encerra e cancela o que ele liberou.
        feito = await consultas.encerrar_limite(c, pessoa_id=m.pessoas["bia"], limite_id=lim, motivo="encerrado")
        assert feito is not None
        tarefa = asyncio.create_task(por_em_quarentena_agora())
        await esperando_trava_de_linha(m, await asyncio.wait_for(pid, 20))
        # A quarentena ja espera a linha do limite: a tela segue e cancela o que o limite liberou.
        assert await cancelar_liberadas(c, lim, "limite encerrado", quem="pessoa") == 1
    feito_q = await asyncio.wait_for(tarefa, 30)
    assert feito_q is not None
    assert (feito_q.paradas, feito_q.limites_suspensos) == ((), 0)
    assert (await sistema(m)).situacao == "quarentena"
    assert (await limite(m, lim)).situacao == "encerrado"
    depois = await m.operacao(op.protocolo)
    assert (depois.estado, depois.desfecho, depois.mudou_por) == (Estado.CANCELADA, "limite encerrado", "pessoa")
