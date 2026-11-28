"""O aviso de bloqueio ao sistema de nivel 3 (desenho 6 e 12.1 G3; criticas A1, A2, M8 e B9).

O bloqueio continua o da Fase 2: aqui ele e o comprovante sao gravados pelo dono do banco, como a transacao do
bloqueio deixaria (``tests.apoio_selos``). O despachante (``selo.consulta.Perguntador``) deriva os avisos do
comprovante e os manda:

- um bloqueio com duas enviadas (uma com efeito adiado) e uma "nao confirmada" no duble de nivel 3 vira um aviso com
  os tres numeros; a adiada fica ``parada`` e o efeito nao acontece; cada operacao ganha uma linha de
  ``informacoes_sistema`` (origem ``aviso``) e a dona, ``sistema_informou_no_bloqueio`` no extrato;
- uma pausa nao avisa; um bloqueio encerrado antes do aviso sair nao sai mais (``bloqueio_encerrado``);
- sem resposta: tres tentativas (a vez reservada antes de chamar) e ``sem_resposta``, com a linha no extrato; o aviso
  repetido vai igual e o sistema da as mesmas respostas;
- nada sai com a porta fechada (o aviso espera, sem gastar tentativa) nem a sistema fora de ``ativo``
  (``sistema_fora``): o duble conta zero chamadas;
- sem a chave da pessoa (falha do MCP-CLARO, M7), o aviso espera a chave sem gastar tentativa; a chave recusada na
  ultima tentativa deixa o aviso ``sistema_fora``, nunca ``sem_resposta``;
- o envio em aberto do alcance fora do comprovante entra no aviso (B9); mais de 100 numeros vao em partes;
- duas copias do despachante ao mesmo tempo: uma chamada por aviso, e um bloqueio derivado uma vez so (M8), mesmo
  com a chave lenta;
- o app do despachante sobe a tarefa dos avisos no ciclo de vida.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from exemplos import selo_mentiroso, selo_nivel3
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import chaves
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.saida.despachante import ligacao_sem_sala
from mcp_claro.selo import consulta, contrato
from tests import apoio_selo_g3a as a
from tests import apoio_selos as s

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


async def bloquear(
    cn: a.Cena, operacoes: list[s.OperacaoDeTeste], *, tipo: str = "bloqueio", em_conferencia: tuple[int, ...] = ()
) -> uuid.UUID:
    """O bloqueio da pessoa e o comprovante dele (``ja_saidas`` e ``em_conferencia`` pelo protocolo), como a
    transacao do bloqueio da Fase 2 os grava."""
    ja = [{"protocolo": op.protocolo, "estado": "enviada"} for i, op in enumerate(operacoes) if i not in em_conferencia]
    conferindo = [{"protocolo": operacoes[i].protocolo, "estado": "nao_confirmada"} for i in em_conferencia]
    async with cn.dono.begin() as c:
        bloqueio = (
            await c.execute(
                text(
                    "INSERT INTO bloqueios (tipo, alcance, pessoa_id, quem, motivo)"
                    " VALUES (:t, 'pessoa', :p, :q, 'teste do aviso') RETURNING id"
                ),
                {"t": tipo, "p": cn.pessoa_id, "q": f"pessoa:{cn.pessoa_id}"},
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO comprovantes (bloqueio_id, ja_saidas, em_conferencia)"
                " VALUES (:b, CAST(:j AS jsonb), CAST(:e AS jsonb))"
            ),
            {"b": bloqueio, "j": json.dumps(ja), "e": json.dumps(conferindo)},
        )
    return uuid.UUID(str(bloqueio))


async def avisos(cn: a.Cena, bloqueio: uuid.UUID) -> list[Any]:
    async with cn.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT id, operacoes, estado, tentativas, pessoa_id, parte FROM avisos_bloqueio"
                        " WHERE bloqueio_id = :b ORDER BY parte"
                    ),
                    {"b": bloqueio},
                )
            ).all()
        )


async def derivado(cn: a.Cena, bloqueio: uuid.UUID) -> int | None:
    async with cn.dono.connect() as c:
        return (
            await c.execute(
                text("SELECT avisos FROM avisos_bloqueio_derivados WHERE bloqueio_id = :b"), {"b": bloqueio}
            )
        ).scalar_one_or_none()


async def de_novo_agora(cn: a.Cena, bloqueio: uuid.UUID) -> None:
    """A proxima vez do aviso agora (as esperas de 1, 5 e 15 minutos sao do relogio do banco)."""
    async with cn.dono.begin() as c:
        await c.execute(
            text("UPDATE avisos_bloqueio SET proxima_em = now() WHERE bloqueio_id = :b AND estado = 'pendente'"),
            {"b": bloqueio},
        )


async def tres_acoes(cn: a.Cena) -> list[s.OperacaoDeTeste]:
    """Duas enviadas (a segunda com efeito adiado, ainda em andamento) e uma "nao confirmada" (a resposta cortada)."""
    assert cn.corte is not None
    feita = await a.nova_acao(cn)
    await a.mandar(cn)
    adiada = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"texto": "adiado", "daqui_a": 600})
    await a.mandar(cn)
    cn.corte.modo, cn.corte.so_se_tiver = "sem_resposta", b'"enviar_lembrete"'
    perdida = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"texto": "perdido"})
    await a.mandar(cn)
    cn.corte.modo = "inteiro"
    assert [await a.estado_da_fila(cn, op.id) for op in (feita, adiada, perdida)] == [
        "enviada",
        "enviada",
        "nao_confirmada",
    ]
    return [feita, adiada, perdida]


async def test_um_aviso_com_os_tres_numeros_e_o_adiado_fica_parado(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3, cortar=True) as cn:
        feita, adiada, perdida = ops = await tres_acoes(cn)
        bloqueio = await bloquear(cn, ops, em_conferencia=(2,))
        perguntador = cn.perguntador()
        assert await perguntador.derivar_avisos() >= 1
        assert await derivado(cn, bloqueio) == 1
        [aviso] = await avisos(cn, bloqueio)
        assert sorted(aviso.operacoes) == sorted(op.id for op in ops)
        assert (aviso.estado, aviso.tentativas, aviso.pessoa_id) == ("pendente", 0, cn.pessoa_id)
        assert await perguntador.avisar_bloqueios() == 1
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("respondido", 1)
        esperado = {feita.id: "concluida", adiada.id: "parada", perdida.id: "concluida"}
        for op in ops:
            linhas = await a.informacoes(cn, op.id)
            [do_aviso] = [x for x in linhas if x.origem == "aviso"]
            assert (do_aviso.desfecho, do_aviso.estado, do_aviso.aviso_id, do_aviso.contradiz) == (
                "respondeu",
                esperado[op.id],
                aviso.id,
                False,
            )
            [linha] = [
                x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "sistema_informou_no_bloqueio"
            ]
            assert linha.pessoa_id == cn.pessoa_id
            assert esperado[op.id] in linha.frase
        # O efeito adiado nao vai acontecer: o duble o parou (sem esperar o tempo dele, que e longo de proposito).
        envio = await a.envio(cn, adiada.id)
        assert cn.motor.ambientes["producao"].achar(str(envio.chave)).estado == "parada"
        assert cn.aplicadas() == 2
        # O aviso ja respondeu: nada sai de novo.
        await de_novo_agora(cn, bloqueio)
        assert await perguntador.avisar_bloqueios() == 0
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1


async def test_pausa_nao_avisa_e_bloqueio_encerrado_nao_sai(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        pausa = await bloquear(cn, [op], tipo="pausa")
        perguntador = cn.perguntador()
        await perguntador.derivar_avisos()
        assert await derivado(cn, pausa) == 0
        assert await avisos(cn, pausa) == []
        async with cn.dono.begin() as c:
            await c.execute(
                text("UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste' WHERE id = :b"), {"b": pausa}
            )
        bloqueio = await bloquear(cn, [op])
        await perguntador.derivar_avisos()
        async with cn.dono.begin() as c:
            await c.execute(
                text("UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste' WHERE id = :b"),
                {"b": bloqueio},
            )
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("bloqueio_encerrado", 0)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 0
        assert [x.origem for x in await a.informacoes(cn, op.id)] == ["recibo"]
        tipos = [x.tipo for x in await a.extrato(cn, sistema=True)]
        assert "aviso_bloqueio_dispensado" in tipos


async def test_sem_resposta_tres_tentativas_e_sem_resposta(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="aviso_mudo") as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        perguntador = cn.perguntador(prazo=0.5)
        await perguntador.derivar_avisos()
        for tentativa in (1, 2, 3):
            assert await perguntador.avisar_bloqueios() == 1
            [aviso] = await avisos(cn, bloqueio)
            assert aviso.tentativas == tentativa
            assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == tentativa
            if tentativa < 3:
                assert aviso.estado == "pendente"
                assert await perguntador.avisar_bloqueios() == 0  # a proxima vez e depois (1 e 5 minutos)
                await de_novo_agora(cn, bloqueio)
        [aviso] = await avisos(cn, bloqueio)
        assert aviso.estado == "sem_resposta"
        assert [x.origem for x in await a.informacoes(cn, op.id)] == ["recibo"]
        [linha] = [x for x in await a.extrato(cn, sistema=True) if x.tipo == "aviso_bloqueio_sem_resposta"]
        assert linha.pessoa_id == cn.pessoa_id
        await de_novo_agora(cn, bloqueio)
        assert await perguntador.avisar_bloqueios() == 0
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 3


async def test_espera_entre_as_tentativas_e_do_banco(banco_migrado: Parametros) -> None:
    """A vez e reservada antes de chamar: tentativas sobe e a proxima vez fica 1 minuto depois (e 5, e 15)."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="aviso_mudo") as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        perguntador = cn.perguntador(prazo=0.3, tentativas_do_aviso=5)
        await perguntador.derivar_avisos()
        esperas = []
        for _ in range(4):
            await perguntador.avisar_bloqueios()
            async with cn.dono.connect() as c:
                esperas.append(
                    (
                        await c.execute(
                            text(
                                "SELECT round(extract(epoch FROM proxima_em - now()) / 60) FROM avisos_bloqueio"
                                " WHERE bloqueio_id = :b"
                            ),
                            {"b": bloqueio},
                        )
                    ).scalar_one()
                )
            await de_novo_agora(cn, bloqueio)
        assert [int(m) for m in esperas] == [1, 5, 15, 15]


@pytest.mark.parametrize("fora", ["porta_fechada", "quarentena"])
async def test_nada_sai_com_a_porta_fechada_nem_a_sistema_fora(banco_migrado: Parametros, fora: str) -> None:
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        perguntador = cn.perguntador()
        await perguntador.derivar_avisos()
        async with cn.dono.begin() as c:
            if fora == "porta_fechada":
                await c.execute(
                    text(
                        "UPDATE estado_saida SET aberta = false, motivo = 'teste', mudou_por = 'teste-g3a',"
                        " mudou_em = now() WHERE id = 1"
                    )
                )
            else:
                await c.execute(text("UPDATE sistemas SET situacao = 'quarentena' WHERE id = :s"), {"s": cn.sistema.id})
        vistos = await perguntador.avisar_bloqueios()
        [aviso] = await avisos(cn, bloqueio)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 0
        if fora == "porta_fechada":
            # O aviso espera a porta abrir, sem gastar tentativa; depois, sai se o bloqueio ainda vale.
            assert (vistos, aviso.estado, aviso.tentativas) == (0, "pendente", 0)
            async with cn.dono.begin() as c:
                await c.execute(text("UPDATE estado_saida SET aberta = true, motivo = NULL WHERE id = 1"))
            assert await perguntador.avisar_bloqueios() == 1
            assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
        else:
            assert (vistos, aviso.estado, aviso.tentativas) == (1, "sistema_fora", 0)
            [linha] = [x for x in await a.extrato(cn, sistema=True) if x.tipo == "aviso_bloqueio_sistema_fora"]
            assert "o sistema esta em quarentena" in linha.frase


class LigacaoDevagar:
    """A ligacao de verdade, que avisa quando a chamada comecou (``dentro``) e demora um pouco para responder."""

    dentro: asyncio.Event

    def __init__(self, sistema: Any) -> None:
        self.ligacao = ligacao_sem_sala(sistema)

    async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
        LigacaoDevagar.dentro.set()
        await asyncio.sleep(0.5)
        return await self.ligacao.chamar_ferramenta(*args, **kw)


async def test_duas_copias_do_despachante_uma_chamada_por_aviso(banco_migrado: Parametros) -> None:
    """Critica M8: a vez e reservada antes de chamar. A segunda copia procura o aviso enquanto a primeira ainda esta
    chamando o sistema: nao o acha (a proxima vez ja e depois), e o sistema recebe uma chamada so."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        copias = [cn.perguntador(), cn.perguntador()]
        for p in copias:
            p.ligacao = LigacaoDevagar  # type: ignore[assignment]
        await asyncio.gather(*(p.derivar_avisos() for p in copias))
        assert await derivado(cn, bloqueio) == 1
        assert len(await avisos(cn, bloqueio)) == 1
        LigacaoDevagar.dentro = asyncio.Event()
        primeira = asyncio.create_task(copias[0].avisar_bloqueios())
        await asyncio.wait_for(LigacaoDevagar.dentro.wait(), timeout=10)
        assert await copias[1].avisar_bloqueios() == 0
        assert await primeira == 1
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("respondido", 1)
        assert [x.origem for x in await a.informacoes(cn, op.id)] == ["recibo", "aviso"]


async def test_o_original_e_o_pedido_igual_vao_com_um_numero_so(banco_migrado: Parametros) -> None:
    """Critica B12: a original (nao confirmada) e o pedido igual que saiu com o numero dela estao os dois no
    comprovante; o aviso leva o numero uma vez so."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3, cortar=True) as cn:
        assert cn.corte is not None
        cn.corte.modo, cn.corte.so_se_tiver, cn.corte.maximo_de_cortes = "sem_resposta", b'"enviar_lembrete"', 1
        original = await a.nova_acao(cn)
        await a.mandar(cn)
        igual = await a.nova_acao(cn, repete=original.id)
        await a.mandar(cn)
        assert (await a.envio(cn, igual.id)).chave == (await a.envio(cn, original.id)).chave
        bloqueio = await bloquear(cn, [igual, original], em_conferencia=(1,))
        await cn.perguntador().derivar_avisos()
        [aviso] = await avisos(cn, bloqueio)
        assert list(aviso.operacoes) == [igual.id]  # o envio mais recente com o numero
        assert await cn.perguntador().avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert aviso.estado == "respondido"


class LigacaoSegura:
    """A ligacao de verdade, com a chamada da acao segura ate ``solta`` (a chamada "a caminho" do sistema)."""

    a_caminho: asyncio.Event
    solta: asyncio.Event

    def __init__(self, sistema: Any) -> None:
        self.ligacao = ligacao_sem_sala(sistema)

    async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
        if args[3] == a.ACAO:
            LigacaoSegura.a_caminho.set()
            await LigacaoSegura.solta.wait()
        return await self.ligacao.chamar_ferramenta(*args, **kw)


async def test_aviso_que_chega_antes_da_chamada_para_o_efeito_e_nao_contradiz(banco_migrado: Parametros) -> None:
    """Critica M2: a acao ja estava "enviando" quando veio o bloqueio, e o aviso chegou ao sistema antes da chamada. O
    sistema honesto diz ``nao_recebida`` (e queima o numero) e recusa a chamada que chega depois, com o recibo
    ``parada``: nao ha contradicao, o selo continua, e o efeito nao acontece."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        LigacaoSegura.a_caminho, LigacaoSegura.solta = asyncio.Event(), asyncio.Event()
        op = await a.nova_acao(cn)
        porta = cn.porta(ligacao=LigacaoSegura)
        envio = asyncio.create_task(porta.enviar_uma())
        try:
            await asyncio.wait_for(LigacaoSegura.a_caminho.wait(), timeout=10)
            assert await a.estado_da_fila(cn, op.id) == "enviando"
            bloqueio = await bloquear(cn, [op], em_conferencia=(0,))
            perguntador = cn.perguntador()
            await perguntador.derivar_avisos()
            assert await perguntador.avisar_bloqueios() == 1
        finally:
            LigacaoSegura.solta.set()
            await envio
        assert [(x.origem, x.estado, x.contradiz) for x in await a.informacoes(cn, op.id)] == [
            ("aviso", "nao_recebida", False),
            ("recibo", "parada", False),
        ]
        assert (await a.selo_vigente(cn)).motivo == "vigente"
        assert cn.aplicadas() == 0
        [aviso] = await avisos(cn, bloqueio)
        assert aviso.estado == "respondido"


async def test_aviso_que_o_banco_recusa_nao_prende_o_bloqueio(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cada aviso nasce num SAVEPOINT: um grupo que o banco recusa (aqui, de um sistema que nao existe) fica de fora,
    os outros saem, e o bloqueio fica derivado (nao volta a cada rodada)."""
    from mcp_claro.selo import consulta

    original = consulta._SQL_DO_AVISO
    com_grupo_ruim = (
        f"WITH certas AS ({original}) SELECT operacao_id, sistema_id, pessoa_id FROM certas"  # noqa: S608 - so o SQL do codigo
        " UNION ALL SELECT operacao_id, gen_random_uuid() AS sistema_id, pessoa_id FROM certas"
    )
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        monkeypatch.setattr(consulta, "_SQL_DO_AVISO", com_grupo_ruim)
        await cn.perguntador().derivar_avisos()
        assert await derivado(cn, bloqueio) == 1
        [aviso] = await avisos(cn, bloqueio)
        assert list(aviso.operacoes) == [op.id]


# --- Retoque da etapa 3 (verificador do G3a) ----------------------------------------------------------------------


async def _proxima_em_minutos(cn: a.Cena, bloqueio: uuid.UUID) -> int:
    async with cn.dono.connect() as c:
        return int(
            (
                await c.execute(
                    text(
                        "SELECT round(extract(epoch FROM proxima_em - now()) / 60) FROM avisos_bloqueio"
                        " WHERE bloqueio_id = :b"
                    ),
                    {"b": bloqueio},
                )
            ).scalar_one()
        )


async def test_sem_a_chave_da_pessoa_o_aviso_espera_sem_gastar_tentativa(banco_migrado: Parametros) -> None:
    """Critica M7: sem a chave da pessoa a falha e do MCP-CLARO. O aviso espera a chave (como espera a porta abrir),
    sem gastar tentativa nem ficar ``sem_resposta``; com a chave de volta, ele sai."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        perguntador = cn.perguntador()
        await perguntador.derivar_avisos()
        async with cn.dono.begin() as c:
            await c.execute(text("DELETE FROM chaves_cifradas WHERE sistema_id = :s"), {"s": cn.sistema.id})
        for _ in range(5):  # mais vezes que as tres tentativas
            assert await perguntador.avisar_bloqueios() == 1
            [aviso] = await avisos(cn, bloqueio)
            assert (aviso.estado, aviso.tentativas) == ("pendente", 0)
            assert await _proxima_em_minutos(cn, bloqueio) == consulta.ESPERA_SEM_CHAVE_MINUTOS
            await de_novo_agora(cn, bloqueio)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 0
        tipos = [x.tipo for x in await a.extrato(cn, sistema=True)]
        assert "aviso_bloqueio_sem_resposta" not in tipos
        async with cn.dono.begin() as c:
            await chaves.guardar(
                c, cn.cifra, sistema_id=cn.sistema.id, segredo=a.CHAVE_DA_PESSOA, pessoa_id=cn.pessoa_id
            )
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("respondido", 1)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1


async def test_chave_recusada_na_ultima_tentativa_nao_culpa_o_sistema(banco_migrado: Parametros) -> None:
    """O sistema responde 401 a chave da pessoa na ultima tentativa: a chave fica marcada vencida, e o aviso fica
    ``sistema_fora`` com o motivo nosso (o comprovante nao diz que o sistema nao respondeu)."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        async with cn.dono.begin() as c:
            await chaves.guardar(
                c, cn.cifra, sistema_id=cn.sistema.id, segredo="chave-que-o-sistema-nao-conhece", pessoa_id=cn.pessoa_id
            )
        perguntador = cn.perguntador(tentativas_do_aviso=1)
        await perguntador.derivar_avisos()
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("sistema_fora", 1)
        async with cn.dono.connect() as c:
            expira_em = (
                await c.execute(
                    text(
                        "SELECT expira_em FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p"
                        " AND tipo = 'acesso'"
                    ),
                    {"s": cn.sistema.id, "p": cn.pessoa_id},
                )
            ).scalar_one()
        assert expira_em is not None
        linhas = await a.extrato(cn, sistema=True)
        [linha] = [x for x in linhas if x.tipo == "aviso_bloqueio_sistema_fora"]
        assert "o sistema recusou a chave da pessoa" in linha.frase
        assert "aviso_bloqueio_sem_resposta" not in [x.tipo for x in linhas]
        assert [x.origem for x in await a.informacoes(cn, op.id)] == ["recibo"]


async def test_aviso_com_as_tentativas_gastas_nao_chama_de_novo(banco_migrado: Parametros) -> None:
    """A ultima tentativa chamou o sistema e o fim nao gravou (o banco falhou): na volta seguinte, o aviso fica
    ``sem_resposta`` sem chamar o sistema outra vez."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        perguntador = cn.perguntador()
        await perguntador.derivar_avisos()
        [aviso] = await avisos(cn, bloqueio)
        async with cn.dono.begin() as c:
            for _ in range(3):  # a guarda deixa subir uma por vez
                await c.execute(
                    text("UPDATE avisos_bloqueio SET tentativas = tentativas + 1 WHERE id = :a"), {"a": aviso.id}
                )
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("sem_resposta", 3)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 0
        assert "aviso_bloqueio_sem_resposta" in [x.tipo for x in await a.extrato(cn, sistema=True)]


async def test_envio_em_aberto_do_alcance_fora_do_comprovante_entra_no_aviso(banco_migrado: Parametros) -> None:
    """Critica B9: o comprovante so tem o que saiu nas ultimas 24 horas. Uma acao de nivel 3 da pessoa bloqueada,
    ainda em andamento e fora do comprovante, entra no aviso; uma ja concluida, fora do comprovante, nao."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        no_comprovante = await a.nova_acao(cn)
        await a.mandar(cn)
        concluida = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"texto": "concluida"})
        await a.mandar(cn)
        em_aberto = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"texto": "adiado", "daqui_a": 600})
        await a.mandar(cn)
        assert [x.estado for x in await a.informacoes(cn, em_aberto.id)] == ["em_andamento"]
        assert [x.estado for x in await a.informacoes(cn, concluida.id)] == ["concluida"]
        bloqueio = await bloquear(cn, [no_comprovante])
        perguntador = cn.perguntador()
        await perguntador.derivar_avisos()
        [aviso] = await avisos(cn, bloqueio)
        assert sorted(aviso.operacoes) == sorted([no_comprovante.id, em_aberto.id])
        assert await perguntador.avisar_bloqueios() == 1
        assert [x.estado for x in await a.informacoes(cn, em_aberto.id) if x.origem == "aviso"] == ["parada"]


class Espia:
    """Os contadores somados pelo aviso (``selo.consulta.CONTADORES``), sem deixar de somar."""

    def __init__(self, contadores: Any) -> None:
        self.contadores = contadores
        self.somados: list[tuple[str, dict[str, str]]] = []

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        self.somados.append((nome, rotulos))
        self.contadores.somar(nome, valor, **rotulos)


async def test_a_pausa_nem_tenta_o_aviso(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uma pausa nao avisa: o despachante nem tenta gravar o aviso (o banco o recusaria, e o contador diria
    ``nao_derivado``)."""
    espia = Espia(consulta.CONTADORES)
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        pausa = await bloquear(cn, [op], tipo="pausa")
        monkeypatch.setattr(consulta, "CONTADORES", espia)
        await cn.perguntador().derivar_avisos()
        assert await derivado(cn, pausa) == 0
        assert await avisos(cn, pausa) == []
        assert [r for n, r in espia.somados if n == "selo_avisos_bloqueio"] == []


class LigacaoQuePerdeAPrimeira:
    """A ligacao de verdade; a primeira resposta ao aviso chega ao despachante como perdida (o sistema a deu)."""

    argumentos: list[Any]
    respostas: list[Any]

    def __init__(self, sistema: Any) -> None:
        self.ligacao = ligacao_sem_sala(sistema)

    async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
        resposta = await self.ligacao.chamar_ferramenta(*args, **kw)
        if args[3] != contrato.FERRAMENTA_BLOQUEIO:
            return resposta
        assert resposta.resultado is not None
        LigacaoQuePerdeAPrimeira.argumentos.append(args[4])
        LigacaoQuePerdeAPrimeira.respostas.append(resposta.resultado.structured_content)
        if len(LigacaoQuePerdeAPrimeira.respostas) == 1:
            return Resposta("duvida", excecao="ReadTimeout")
        return resposta


async def test_o_aviso_repetido_vai_igual_e_da_as_mesmas_respostas(banco_migrado: Parametros) -> None:
    """SELO-3.6 pelo caminho do despachante: a resposta da primeira tentativa se perde; a segunda leva o mesmo
    ``bloqueio`` e os mesmos numeros, e o sistema da as mesmas respostas, sem efeito a mais (a adiada fica parada)."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        feita = await a.nova_acao(cn)
        await a.mandar(cn)
        adiada = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"texto": "adiado", "daqui_a": 600})
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [feita, adiada])
        LigacaoQuePerdeAPrimeira.argumentos, LigacaoQuePerdeAPrimeira.respostas = [], []
        perguntador = cn.perguntador()
        perguntador.ligacao = LigacaoQuePerdeAPrimeira  # type: ignore[assignment]
        await perguntador.derivar_avisos()
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("pendente", 1)
        await de_novo_agora(cn, bloqueio)
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("respondido", 2)
        primeiro, segundo = LigacaoQuePerdeAPrimeira.argumentos
        assert primeiro == segundo == {"bloqueio": str(aviso.id), "operacoes": primeiro["operacoes"]}
        assert sorted(primeiro["operacoes"]) == sorted(
            [str((await a.envio(cn, op.id)).chave) for op in (feita, adiada)]
        )
        assert LigacaoQuePerdeAPrimeira.respostas[0] == LigacaoQuePerdeAPrimeira.respostas[1]
        assert cn.aplicadas() == 1  # so a feita; a adiada ficou parada
        for op, estado in ((feita, "concluida"), (adiada, "parada")):
            assert [x.estado for x in await a.informacoes(cn, op.id) if x.origem == "aviso"] == [estado]


async def test_mais_de_cem_numeros_vao_em_partes(banco_migrado: Parametros) -> None:
    """Um bloqueio com 101 numeros do mesmo sistema e da mesma pessoa: dois avisos, de 100 e de 1, cada numero uma
    vez."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        ops = []
        async with cn.dono.begin() as c:
            for _ in range(contrato.MAX_OPERACOES_POR_AVISO + 1):
                op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
                await s.enviar(c, op.id, fim="enviada")
                ops.append(op)
        bloqueio = await bloquear(cn, ops)
        await cn.perguntador().derivar_avisos()
        assert await derivado(cn, bloqueio) == 2
        partes = await avisos(cn, bloqueio)
        assert [(x.parte, len(x.operacoes)) for x in partes] == [(1, 100), (2, 1)]
        assert sorted(o for x in partes for o in x.operacoes) == sorted(op.id for op in ops)


async def test_o_app_do_despachante_sobe_a_tarefa_dos_avisos(banco_migrado: Parametros) -> None:
    """O ciclo de vida do app do despachante sobe a tarefa dos avisos: o comprovante novo vira aviso, e o aviso sai,
    sem ninguem chamar o ``Perguntador`` de fora."""
    from mcp_claro.despachante.app import criar_app_despachante

    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        p = banco_migrado.model_copy(update={"APP_ENV": "teste", "OUTBOX_SWEEP_SECONDS": 1})
        app = criar_app_despachante(p, cn.desp, cn.cifra, liberar_locais=True)

        perguntador = app.state.perguntador
        avisar, avisou = perguntador.avisar_bloqueios, asyncio.Event()

        async def avisar_e_contar(*args: Any, **kw: Any) -> int:
            vistos = int(await avisar(*args, **kw))
            if vistos:
                avisou.set()
            return vistos

        perguntador.avisar_bloqueios = avisar_e_contar
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(avisou.wait(), timeout=20)
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("respondido", 1)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
        assert [x.origem for x in await a.informacoes(cn, op.id)] == ["recibo", "aviso"]


async def test_copia_que_demora_a_abrir_a_chave_nao_chama_de_novo(banco_migrado: Parametros) -> None:
    """Critica M8 com a chave lenta: a copia lenta pegou o aviso e abre a chave. Enquanto isso a vez e dela (a outra
    copia nao acha o aviso). Se a vez passa antes de ela reservar, a outra copia pega o aviso e chama; a lenta, ao
    reservar, ve que o aviso mudou (uma tentativa a mais) e nao chama."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="aviso_mudo") as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        lenta, rapida = cn.perguntador(prazo=0.3), cn.perguntador(prazo=0.3)
        await lenta.derivar_avisos()
        abrindo, solta = asyncio.Event(), asyncio.Event()
        segredo = lenta._segredo

        async def segredo_lento(*args: Any, **kw: Any) -> Any:
            abrindo.set()
            await solta.wait()
            return await segredo(*args, **kw)

        lenta._segredo = segredo_lento  # type: ignore[method-assign]
        tarefa = asyncio.create_task(lenta.avisar_bloqueios())
        try:
            await asyncio.wait_for(abrindo.wait(), timeout=10)
            # Enquanto a lenta abre a chave, a vez e dela: a outra copia nem acha o aviso (nem abre a chave).
            assert await rapida.avisar_bloqueios() == 0
            await de_novo_agora(cn, bloqueio)  # a vez que a lenta pegou passou
            assert await rapida.avisar_bloqueios() == 1
            assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
        finally:
            solta.set()
        assert await tarefa == 1
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("pendente", 1)


async def test_porta_que_fecha_enquanto_a_chave_abre_segura_o_aviso(banco_migrado: Parametros) -> None:
    """A porta fecha depois de o aviso ser pego e antes da reserva: nada sai e nenhuma tentativa e gasta; com a porta
    aberta de novo, o aviso sai."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        bloqueio = await bloquear(cn, [op])
        perguntador = cn.perguntador()
        await perguntador.derivar_avisos()
        segredo = perguntador._segredo

        async def fecha_a_porta(*args: Any, **kw: Any) -> Any:
            async with cn.dono.begin() as c:
                await c.execute(
                    text(
                        "UPDATE estado_saida SET aberta = false, motivo = 'teste', mudou_por = 'teste-g3a',"
                        " mudou_em = now() WHERE id = 1"
                    )
                )
            return await segredo(*args, **kw)

        perguntador._segredo = fecha_a_porta  # type: ignore[method-assign]
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("pendente", 0)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 0
        perguntador._segredo = segredo  # type: ignore[method-assign]
        async with cn.dono.begin() as c:
            await c.execute(text("UPDATE estado_saida SET aberta = true, motivo = NULL WHERE id = 1"))
        await de_novo_agora(cn, bloqueio)
        assert await perguntador.avisar_bloqueios() == 1
        [aviso] = await avisos(cn, bloqueio)
        assert (aviso.estado, aviso.tentativas) == ("respondido", 1)
        assert cn.chamadas(contrato.FERRAMENTA_BLOQUEIO) == 1
