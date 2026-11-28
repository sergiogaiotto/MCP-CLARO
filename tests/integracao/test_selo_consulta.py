"""A consulta ao proprio sistema (desenho 5.2, 5.5 e 12.1 G3; criticas A2, A3.3, M7, M8 e B8).

Com o duble de verdade e o cortador (``tests/cortador.py``, so usado) entre o despachante e ele:

- resposta perdida de proposito: a acao fica "nao confirmada", a consulta pergunta, ``concluida`` fica gravada e o
  extrato da pessoa ganha ``sistema_informou``; o duble conta uma chamada so da acao;
- a vez e do banco: antes da hora, 409 e nada e perguntado; uma ``nao_recebida`` sozinha nao encerra (A3.3);
- sem resposta: 8 tentativas, ``sistema_nao_respondeu_consulta`` no fim; com 3 operacoes seguidas assim (ou com
  ``desconhecido`` no fim, B8), o selo e retirado e o ADMIN e avisado; depois de um reteste, so contam as que
  terminaram depois do teste novo, e as paradas em ``sistema_fora`` nunca contam; a conta le as 50 que terminaram
  mais perto de agora, e as que ainda estao na escada saem antes do limite; a acao confirmada so pelo recibo nao
  entra na conta;
- respostas invalidas (instrucao no estado, resposta grande, outro numero) dao ``resposta_invalida``; o membro a mais
  e ignorado; nada do texto do sistema chega ao banco nem ao extrato;
- chave apagada, ou recusada pelo sistema (401, a chave fica marcada vencida): ``sem_chave``, sem gastar tentativa
  nem contar contra o sistema (M7); erro JSON-RPC e evento fora da janela da hora sao ``resposta_invalida``;
- fora de ``ativo``, nada e perguntado (``sistema_fora``, A2), e a escada para de vez: uma resposta ao aviso de
  bloqueio depois nao a reabre (conferencia final da etapa 3r); com a porta fechada, nada e
  perguntado nem gravado (409 ``porta_fechada``, decisao 2 do coordenador), e a escada nao anda;
- contradicao: o sistema que se contradiz perde o selo, e o extrato e o ADMIN ficam sabendo (depois de um reteste,
  o aviso ao ADMIN e do selo de agora);
- duas copias do despachante ao mesmo tempo: uma pergunta so (M8).

O relogio da escada e o do banco: ``voltar_no_tempo`` poe o envio e as respostas para tras.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy import text

from exemplos import selo_mentiroso, selo_nivel2, selo_nivel3
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import chaves
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.saida import fila
from mcp_claro.saida.despachante import ligacao_sem_sala
from mcp_claro.selo import consulta, contrato
from mcp_claro.selo.consulta import NadaAPerguntar, PortaFechada, SemEnvio
from tests import apoio_selo_g3a as a
from tests import apoio_selos as s

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

INSTRUCAO = selo_mentiroso.INSTRUCAO


async def perdida(cn: a.Cena, **argumentos: Any) -> s.OperacaoDeTeste:
    """Uma acao cuja resposta o cortador corta: chegou ao sistema, a resposta nao voltou."""
    assert cn.corte is not None
    cn.corte.modo, cn.corte.so_se_tiver, cn.corte.maximo_de_cortes = "sem_resposta", b'"enviar_lembrete"', None
    op = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | argumentos if argumentos else None)
    await a.mandar(cn)
    cn.corte.modo = "inteiro"
    assert await a.estado_da_fila(cn, op.id) == "nao_confirmada"
    return op


async def test_resposta_perdida_e_consultada_e_o_sistema_informa(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2, cortar=True) as cn:
        op = await perdida(cn)
        assert cn.aplicadas() == 1
        perguntador = cn.perguntador()
        with pytest.raises(NadaAPerguntar):  # antes do prazo do envio e do primeiro degrau
            await perguntador.consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        await a.voltar_no_tempo(cn, [op.id])
        desfecho = await perguntador.consultar(op.id)
        assert (desfecho.desfecho, desfecho.estado) == ("respondeu", "concluida")
        [info] = await a.informacoes(cn, op.id)
        assert (info.origem, info.desfecho, info.estado) == ("consulta", "respondeu", "concluida")
        linhas = await a.extrato(cn, protocolo=op.protocolo)
        [informou] = [x for x in linhas if x.tipo == "sistema_informou"]
        assert "concluida" in informou.frase
        assert informou.pessoa_id == cn.pessoa_id
        assert informou.dados == {"origem": "consulta", "estado": "concluida"}
        # Nada foi reenviado: o duble conta uma chamada so da acao.
        assert cn.chamadas(a.ACAO) == 1
        assert cn.aplicadas() == 1
        # Com a resposta que encerra, nada mais a perguntar, mesmo com o tempo passado.
        await a.voltar_no_tempo(cn, [op.id])
        with pytest.raises(NadaAPerguntar):
            await perguntador.consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


async def test_nao_recebida_sozinha_nao_encerra(banco_migrado: Parametros) -> None:
    """Critica A3.3: a primeira ``nao_recebida`` nao encerra a escada; a segunda, com intervalo, confirma."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)  # saiu com numero e nao chegou: o duble nunca viu o numero
        perguntador = cn.perguntador()
        await a.voltar_no_tempo(cn, [op.id])
        assert (await perguntador.consultar(op.id)).estado == "nao_recebida"
        await a.voltar_no_tempo(cn, [op.id])
        assert (await perguntador.consultar(op.id)).estado == "nao_recebida"
        await a.voltar_no_tempo(cn, [op.id])
        with pytest.raises(NadaAPerguntar):  # confirmada: encerrou
            await perguntador.consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 2


async def test_sem_envio_e_404(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        with pytest.raises(SemEnvio):
            await cn.perguntador().consultar(uuid.uuid4())


async def test_sem_resposta_oito_tentativas_e_a_decisao_fica_com_a_pessoa(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        perguntador = cn.perguntador(prazo=0.5)
        for _ in range(8):
            await a.voltar_no_tempo(cn, [op.id])
            assert (await perguntador.consultar(op.id)).desfecho == "sem_resposta"
        await a.voltar_no_tempo(cn, [op.id])
        with pytest.raises(NadaAPerguntar):  # a escada acabou
            await perguntador.consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 8
        tipos = [x.tipo for x in await a.extrato(cn, protocolo=op.protocolo)]
        assert tipos.count("sistema_nao_respondeu_consulta") == 1
        # Uma operacao so nao retira o selo.
        assert (await a.selo_vigente(cn)).motivo == "vigente"


async def _escada_quase_cheia(cn: a.Cena, desfecho: str = "sem_resposta", estado: str | None = None) -> uuid.UUID:
    """Uma operacao com envio e 7 consultas falhas gravadas pelo dono: a proxima e a ultima da escada."""
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
        await s.enviar(c, op.id)
    for _ in range(7):
        async with cn.dono.begin() as c:
            await s.informar(c, op.id, estado, desfecho=desfecho)
        await a.voltar_no_tempo(cn, [op.id], minutos=1)
    return op.id


async def _escada_cheia(cn: a.Cena, desfecho: str = "sem_resposta", estado: str | None = None) -> uuid.UUID:
    operacao_id = await _escada_quase_cheia(cn, desfecho, estado)
    async with cn.dono.begin() as c:
        await s.informar(c, operacao_id, estado, desfecho=desfecho)
    return operacao_id


@pytest.mark.parametrize(
    ("fabrica", "modo", "desfecho"),
    [
        (selo_mentiroso.criar, "consulta_muda", "sem_resposta"),
        (selo_mentiroso.criar, "sempre_desconhecido", "respondeu"),
        (selo_mentiroso.criar, "resposta_grande", "resposta_invalida"),
    ],
    ids=["sem_resposta", "desconhecido_no_fim", "resposta_invalida"],
)
async def test_tres_operacoes_seguidas_sem_resposta_retiram_o_selo(
    banco_migrado: Parametros, fabrica: Any, modo: str, desfecho: str
) -> None:
    """D18, B8 e M7: as duas primeiras terminaram a escada sem resposta (gravadas pelo dono); a terceira termina agora,
    pela consulta de verdade: o selo de agora e retirado, com a linha do sistema e o aviso ao ADMIN."""
    async with a.cena(banco_migrado, fabrica, nivel=2, modo=modo) as cn:
        primeiras = [await _escada_cheia(cn) for _ in range(2)]
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [*primeiras, terceira])
        lido = await cn.perguntador(prazo=0.5).consultar(terceira)
        assert lido.desfecho == desfecho
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo) == (1, "retirado")
        async with cn.dono.connect() as c:
            retirada = (
                await c.execute(
                    text("SELECT motivo, retirado_por FROM selos_retirados WHERE sistema_id = :s"),
                    {"s": cn.sistema.id},
                )
            ).all()
        assert [(r.motivo, r.retirado_por) for r in retirada] == [("nao_respondeu_consultas", "mcp-claro")]
        [linha] = [x for x in await a.extrato(cn, sistema=True) if x.tipo == "selo_retirado"]
        assert "nao respondeu as consultas" in linha.frase
        [aviso] = await a.avisos_ao_admin(cn, "selo_retirado")
        assert aviso.corpo["motivo"] == "nao_respondeu_consultas"
        assert INSTRUCAO not in a.textos(await a.extrato(cn, sistema=True))


async def test_uma_resposta_no_meio_interrompe_a_sequencia(banco_migrado: Parametros) -> None:
    """Da mais recente para tras: a terceira sem resposta, a do meio com resposta que encerra, e duas sem resposta
    antes dela. A sequencia para na do meio: sao so uma seguidas, e o selo fica."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        antigas = [await _escada_cheia(cn) for _ in range(2)]
        await a.voltar_no_tempo(cn, antigas, minutos=10)
        async with cn.dono.begin() as c:  # a do meio teve resposta que encerra
            meio = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, meio.id)
            await s.informar(c, meio.id, "concluida")
        await a.voltar_no_tempo(cn, [meio.id], minutos=5)
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [terceira])
        await a.voltar_no_tempo(cn, [*antigas, meio.id], minutos=300)
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        assert (await a.selo_vigente(cn)).motivo == "vigente"


async def test_erro_na_retirada_nao_desfaz_a_consulta(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retirada vai num SAVEPOINT da transacao da consulta: um erro do banco nela deixa a linha da consulta."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        op = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [op])
        perguntador = cn.perguntador(prazo=0.5)

        async def retirada_que_falha(c: Any, sistema: Any) -> bool:
            await c.execute(text("SELECT 1/0"))
            return False

        monkeypatch.setattr(perguntador, "_talvez_retirar", retirada_que_falha)
        assert (await perguntador.consultar(op)).desfecho == "sem_resposta"
        assert len(await a.informacoes(cn, op)) == 8
        assert "sistema_nao_respondeu_consulta" in [x.tipo for x in await a.extrato(cn, sistema=True)]


async def test_nenhuma_transacao_fica_aberta_durante_a_pergunta(banco_migrado: Parametros) -> None:
    """A trava consultiva do sistema e de quem grava ``informacoes_sistema``: chamar primeiro, gravar depois. Durante
    a chamada, nenhuma conexao do despachante fica numa transacao aberta."""
    vistos: list[int] = []

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:

        class LigacaoQueOlha:
            def __init__(self, sistema: Any) -> None:
                self.dentro = ligacao_sem_sala(sistema)

            async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
                async with cn.dono.connect() as c:
                    vistos.append(
                        int(
                            (
                                await c.execute(
                                    text(
                                        "SELECT count(*) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%'"
                                        " AND application_name = 'teste-g3a-mcp_claro_despachante'"
                                    )
                                )
                            ).scalar_one()
                        )
                    )
                return await self.dentro.chamar_ferramenta(*args, **kw)

        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        await a.voltar_no_tempo(cn, [op.id])
        perguntador = cn.perguntador()
        perguntador.ligacao = LigacaoQueOlha  # type: ignore[assignment]
        assert (await perguntador.consultar(op.id)).desfecho == "respondeu"
        assert vistos == [0]


async def test_sem_chave_nao_gasta_tentativa_nem_conta_contra_o_sistema(banco_migrado: Parametros) -> None:
    """Critica M7: oito vezes sem a chave da pessoa (mais que a escada inteira) e nada perguntado; com a chave de
    volta, a consulta ainda vai: nenhuma tentativa foi gasta, e o selo continua."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
            await c.execute(text("DELETE FROM chaves_cifradas WHERE sistema_id = :s"), {"s": cn.sistema.id})
        perguntador = cn.perguntador()
        for _ in range(8):
            await a.voltar_no_tempo(cn, [op.id])
            lido = await perguntador.consultar(op.id)
            assert (lido.desfecho, lido.estado) == ("sem_chave", None)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        [linha, *_] = [x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "consulta_nao_feita"]
        assert "a pessoa ainda nao autorizou o sistema" in linha.frase
        assert "A falha e do MCP-CLARO, nao do sistema" in linha.frase
        tipos = [x.tipo for x in await a.extrato(cn, protocolo=op.protocolo)]
        assert "sistema_nao_respondeu_consulta" not in tipos
        async with cn.dono.begin() as c:
            await chaves.guardar(
                c, cn.cifra, sistema_id=cn.sistema.id, segredo=a.CHAVE_DA_PESSOA, pessoa_id=cn.pessoa_id
            )
        await a.voltar_no_tempo(cn, [op.id])
        lido = await perguntador.consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("respondeu", "nao_recebida")
        assert (await a.selo_vigente(cn)).motivo == "vigente"


@pytest.mark.parametrize("modo", ["estado_com_instrucao", "resposta_grande"])
async def test_resposta_invalida_e_nada_do_texto_fica(banco_migrado: Parametros, modo: str) -> None:
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo=modo) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        await a.voltar_no_tempo(cn, [op.id])
        lido = await cn.perguntador().consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("resposta_invalida", None)
        linhas = await a.informacoes(cn, op.id)
        assert [(x.desfecho, x.estado) for x in linhas] == [("resposta_invalida", None)]
        tudo = a.textos(linhas) + a.textos(await a.extrato(cn, protocolo=op.protocolo))
        assert INSTRUCAO not in tudo
        assert "xxxx" not in tudo


async def test_membro_a_mais_e_ignorado_e_nunca_guardado(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="membro_com_instrucao") as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        await a.voltar_no_tempo(cn, [op.id])
        lido = await cn.perguntador().consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("respondeu", "nao_recebida")
        async with cn.dono.connect() as c:
            despejo = (
                await c.execute(
                    text(
                        "SELECT coalesce(string_agg(to_jsonb(i)::text, ''), '') FROM informacoes_sistema i"
                        " WHERE i.sistema_id = :s"
                    ),
                    {"s": cn.sistema.id},
                )
            ).scalar_one()
            extrato = (
                await c.execute(
                    text(
                        "SELECT coalesce(string_agg(frase || dados::text, ''), '') FROM extrato WHERE sistema_id = :s"
                    ),
                    {"s": cn.sistema.id},
                )
            ).scalar_one()
        assert INSTRUCAO not in despejo + extrato


class LigacaoQueResponde:
    """Uma ligacao que responde a consulta com o ``structuredContent`` dado (outro numero, por exemplo)."""

    def __init__(self, dados: dict[str, Any]) -> None:
        self.dados = dados

    async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
        resultado = types.CallToolResult(
            content=[types.TextContent(type="text", text="x")], structured_content=self.dados
        )
        return Resposta("resultado", resultado=resultado)


async def test_resposta_com_outro_numero_e_invalida(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        outro = {"contrato": contrato.CONTRATO, "operacao": str(uuid.uuid4()), "estado": "concluida"}
        perguntador = cn.perguntador()
        perguntador.ligacao = lambda _sistema: LigacaoQueResponde(outro)  # type: ignore[assignment, return-value]
        await a.voltar_no_tempo(cn, [op.id])
        assert (await perguntador.consultar(op.id)).desfecho == "resposta_invalida"


@pytest.mark.parametrize("fora", ["quarentena", "desativado"])
async def test_fora_de_ativo_nada_e_perguntado(banco_migrado: Parametros, fora: str) -> None:
    """Critica A2: fora de ``ativo``, ``sistema_fora`` e a escada para. A porta fechada saiu daqui (decisao 2 do
    coordenador, retoque da etapa 3): ela nao grava nada (``test_porta_fechada_nada_e_perguntado_nem_gravado``)."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
            await c.execute(text("UPDATE sistemas SET situacao = :f WHERE id = :s"), {"f": fora, "s": cn.sistema.id})
        await a.voltar_no_tempo(cn, [op.id])
        perguntador = cn.perguntador()
        lido = await perguntador.consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("sistema_fora", None)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        [linha] = [x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "sistema_fora_consulta"]
        esperado = {"quarentena": "o sistema esta em quarentena", "desativado": "o sistema nao esta ativo"}[fora]
        assert esperado in linha.frase
        assert "a decisao fica com a pessoa" in linha.frase
        # A escada para: mesmo com o tempo passado, nada mais e perguntado.
        await a.voltar_no_tempo(cn, [op.id])
        with pytest.raises(NadaAPerguntar):
            await perguntador.consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0


async def test_o_sistema_que_se_contradiz_perde_o_selo(banco_migrado: Parametros) -> None:
    """O recibo disse ``em_andamento`` (efeito adiado); a consulta diz ``nao_recebida`` do mesmo numero."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="contradiz") as cn:
        op = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"daqui_a": 600})
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        assert [x.estado for x in await a.informacoes(cn, op.id)] == ["em_andamento"]
        await a.voltar_no_tempo(cn, [op.id])
        lido = await cn.perguntador().consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("respondeu", "nao_recebida")
        assert [x.contradiz for x in await a.informacoes(cn, op.id)] == [False, True]
        assert (await a.selo_vigente(cn)).motivo == "retirado"
        tipos = [x.tipo for x in await a.extrato(cn, protocolo=op.protocolo)]
        assert "sistema_se_contradisse" in tipos
        [aviso] = await a.avisos_ao_admin(cn, "selo_retirado")
        assert aviso.corpo["motivo"] == "se_contradisse"


async def test_enviada_com_desfecho_nao_e_perguntada(banco_migrado: Parametros) -> None:
    """Critica B9: so se pergunta a "nao confirmada" ou a "enviada" com o ultimo estado em aberto."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        await a.voltar_no_tempo(cn, [op.id])
        with pytest.raises(NadaAPerguntar):
            await cn.perguntador().consultar(op.id)
        sem_saber = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"sem_saber": True})
        await a.mandar(cn)
        assert [x.estado for x in await a.informacoes(cn, sem_saber.id)] == ["desconhecido"]
        await a.voltar_no_tempo(cn, [sem_saber.id])
        lido = await cn.perguntador().consultar(sem_saber.id)
        assert (lido.desfecho, lido.estado) == ("respondeu", "concluida")
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


async def test_enviada_no_nivel_2_e_nao_saiu_nao_sao_perguntadas(banco_migrado: Parametros) -> None:
    """No nivel 2 nao ha recibo: a "enviada" nao tem linha nenhuma do sistema, e mesmo assim nao se pergunta (o
    sistema respondeu). A que nao saiu (sem a chave da pessoa) tambem nao: nada chegou ao sistema."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        enviada = await a.nova_acao(cn)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, enviada.id) == "enviada"
        assert await a.informacoes(cn, enviada.id) == []
        async with cn.dono.begin() as c:
            await c.execute(text("DELETE FROM chaves_cifradas WHERE sistema_id = :s"), {"s": cn.sistema.id})
        nao_saiu = await a.nova_acao(cn, argumentos=a.ARGUMENTOS | {"texto": "sem chave"})
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, nao_saiu.id) == "nao_saiu"
        assert await a.envio(cn, nao_saiu.id) is not None
        await a.voltar_no_tempo(cn, [enviada.id, nao_saiu.id])
        for op in (enviada, nao_saiu):
            with pytest.raises(NadaAPerguntar):
                await cn.perguntador().consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0


async def test_a_original_para_de_ser_consultada_quando_o_pedido_igual_sai_com_o_numero_dela(
    banco_migrado: Parametros,
) -> None:
    """Critica M4: depois que o pedido igual saiu com o numero da original, a original nao e mais consultada (o que
    o sistema informar fica no pedido igual); o pedido igual, se ficar em duvida, e o consultado."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2, cortar=True) as cn:
        original = await perdida(cn)
        assert cn.corte is not None
        cn.corte.modo = "sem_resposta"
        igual = await a.nova_acao(cn, repete=original.id)
        await a.mandar(cn)
        cn.corte.modo = "inteiro"
        assert await a.estado_da_fila(cn, igual.id) == "nao_confirmada"
        assert (await a.envio(cn, igual.id)).chave == (await a.envio(cn, original.id)).chave
        await a.voltar_no_tempo(cn, [original.id, igual.id])
        with pytest.raises(NadaAPerguntar):
            await cn.perguntador().consultar(original.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        lido = await cn.perguntador().consultar(igual.id)
        assert (lido.desfecho, lido.estado) == ("respondeu", "concluida")
        assert cn.aplicadas() == 1


class LigacaoDevagar:
    """A ligacao de verdade, que avisa quando a chamada comecou (``dentro``) e demora um pouco para responder."""

    dentro: asyncio.Event

    def __init__(self, sistema: Any) -> None:
        self.ligacao = ligacao_sem_sala(sistema)

    async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
        LigacaoDevagar.dentro.set()
        await asyncio.sleep(0.5)
        return await self.ligacao.chamar_ferramenta(*args, **kw)


async def test_duas_copias_do_despachante_perguntam_uma_vez(banco_migrado: Parametros) -> None:
    """Critica M8: a segunda copia pede a mesma consulta enquanto a primeira ainda esta perguntando: 409, e o sistema
    recebe uma pergunta so."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        await a.voltar_no_tempo(cn, [op.id])
        copias = [cn.perguntador(), cn.perguntador()]
        for p in copias:
            p.ligacao = LigacaoDevagar  # type: ignore[assignment]
        LigacaoDevagar.dentro = asyncio.Event()
        primeira = asyncio.create_task(copias[0].consultar(op.id))
        await asyncio.wait_for(LigacaoDevagar.dentro.wait(), timeout=10)
        with pytest.raises(NadaAPerguntar):
            await copias[1].consultar(op.id)
        assert (await primeira).desfecho == "respondeu"
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1
        assert len(await a.informacoes(cn, op.id)) == 1


# --- Retoque da etapa 3 (verificador do G3a) ----------------------------------------------------------------------


async def test_depois_de_um_reteste_so_as_falhas_novas_contam(banco_migrado: Parametros) -> None:
    """D18 e B8 com um reteste: tres operacoes sem resposta retiram o selo; o ADMIN pede outro teste, aprovado. Uma
    operacao sem resposta depois disso nao derruba o selo novo (as falhas de antes do reteste ja contaram contra o
    antigo); tres seguidas depois do reteste derrubam."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        perguntador = cn.perguntador(prazo=0.5)
        primeiras = [await _escada_cheia(cn) for _ in range(2)]
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [*primeiras, terceira])
        assert (await perguntador.consultar(terceira)).desfecho == "sem_resposta"
        assert (await a.selo_vigente(cn)).motivo == "retirado"
        async with cn.dono.begin() as c:
            novo = await s.gravar_teste(c, cn.sistema.id, nivel=2)
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo, vigente.teste_id) == (2, "vigente", novo.id)
        quarta = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [quarta])
        assert (await perguntador.consultar(quarta)).desfecho == "sem_resposta"
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo) == (2, "vigente"), "o selo novo caiu com uma falha so"
        # Mais duas, depois do reteste: agora sao tres seguidas, e o selo novo cai.
        await _escada_cheia(cn)
        sexta = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [sexta])
        assert (await perguntador.consultar(sexta)).desfecho == "sem_resposta"
        assert (await a.selo_vigente(cn)).motivo == "retirado"
        async with cn.dono.connect() as c:
            retirados = (
                await c.execute(
                    text("SELECT teste_id FROM selos_retirados WHERE sistema_id = :s ORDER BY retirado_em"),
                    {"s": cn.sistema.id},
                )
            ).scalars()
            assert [str(t) for t in retirados] == [str(cn.teste.id if cn.teste else None), str(novo.id)]
        assert len(await a.avisos_ao_admin(cn, "selo_retirado")) == 2


class Espia:
    """Os contadores somados pela consulta (``selo.consulta.CONTADORES``), sem deixar de somar."""

    def __init__(self, contadores: Any) -> None:
        self.contadores = contadores
        self.somados: list[tuple[str, dict[str, str]]] = []

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        self.somados.append((nome, rotulos))
        self.contadores.somar(nome, valor, **rotulos)


async def test_contradicao_depois_do_reteste_avisa_o_admin_do_selo_de_agora(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duas operacoes do teste antigo com o recibo ``em_andamento``. A primeira se contradiz (o sistema diz que nao
    recebeu): o selo cai e o ADMIN e avisado. Vem um reteste aprovado. A segunda tambem se contradiz: o banco retira o
    selo de agora, e o ADMIN e avisado dele (um aviso por teste retirado). O contador ``selo_retiradas`` soma uma vez
    por teste retirado (D10 da conferencia da etapa 3): a segunda contradicao nao conta de novo o teste antigo."""
    espia = Espia(consulta.CONTADORES)
    monkeypatch.setattr(consulta, "CONTADORES", espia)
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        ops = []
        async with cn.dono.begin() as c:
            for _ in range(2):
                op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
                await s.enviar(c, op.id, fim="enviada")
                await s.informar(c, op.id, "em_andamento", origem="recibo")
                ops.append(op)
        await a.voltar_no_tempo(cn, [op.id for op in ops])
        perguntador = cn.perguntador()
        assert (await perguntador.consultar(ops[0].id)).estado == "nao_recebida"
        assert (await a.selo_vigente(cn)).motivo == "retirado"
        assert len(await a.avisos_ao_admin(cn, "selo_retirado")) == 1
        async with cn.dono.begin() as c:
            novo = await s.gravar_teste(c, cn.sistema.id, nivel=3)
        assert (await a.selo_vigente(cn)).motivo == "vigente"
        assert (await perguntador.consultar(ops[1].id)).estado == "nao_recebida"
        assert (await a.selo_vigente(cn)).motivo == "retirado"
        avisos = await a.avisos_ao_admin(cn, "selo_retirado")
        assert len(avisos) == 2, "o ADMIN nao foi avisado da retirada do selo de agora"
        assert {x.corpo["motivo"] for x in avisos} == {"se_contradisse"}
        assert [r for n, r in espia.somados if n == "selo_retiradas"] == [{"motivo": "se_contradisse"}] * 2
        async with cn.dono.connect() as c:
            retirados = (
                await c.execute(
                    text("SELECT teste_id FROM selos_retirados WHERE sistema_id = :s ORDER BY retirado_em"),
                    {"s": cn.sistema.id},
                )
            ).scalars()
            assert [str(t) for t in retirados] == [str(cn.teste.id if cn.teste else None), str(novo.id)]


async def _expira_em(cn: a.Cena) -> Any:
    async with cn.dono.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT expira_em FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p"
                    " AND aplicativo_id IS NULL AND tipo = 'acesso'"
                ),
                {"s": cn.sistema.id, "p": cn.pessoa_id},
            )
        ).scalar_one()


async def test_chave_recusada_pelo_sistema_e_falha_do_mcp_claro(banco_migrado: Parametros) -> None:
    """Critica M7: o sistema responde 401 a chave da pessoa. A chave fica marcada vencida (a proxima pergunta renova
    antes), a consulta grava ``sem_chave``, sem gastar tentativa nem contar contra o sistema."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
            await chaves.guardar(
                c, cn.cifra, sistema_id=cn.sistema.id, segredo="chave-que-o-sistema-nao-conhece", pessoa_id=cn.pessoa_id
            )
        assert await _expira_em(cn) is None
        await a.voltar_no_tempo(cn, [op.id])
        lido = await cn.perguntador().consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("sem_chave", None)
        assert await _expira_em(cn) is not None
        assert [(x.origem, x.desfecho) for x in await a.informacoes(cn, op.id)] == [("consulta", "sem_chave")]
        [linha] = [x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "consulta_nao_feita"]
        assert "o sistema recusou a chave da pessoa" in linha.frase
        assert "A falha e do MCP-CLARO, nao do sistema" in linha.frase


class LigacaoQueDaErroRpc:
    """O sistema responde a consulta com um erro JSON-RPC (fora do contrato)."""

    def __init__(self, _sistema: Any) -> None:
        pass

    async def chamar_ferramenta(self, *args: Any, **kw: Any) -> Resposta:
        return Resposta("erro_rpc", erro=types.ErrorData(code=-32603, message=INSTRUCAO), status_http=(200,))


async def test_erro_json_rpc_na_consulta_e_resposta_invalida(banco_migrado: Parametros) -> None:
    """O sistema respondeu, mas fora do contrato: ``resposta_invalida`` (conta contra ele), e a mensagem dele nunca
    fica."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
        perguntador = cn.perguntador()
        perguntador.ligacao = LigacaoQueDaErroRpc  # type: ignore[assignment]
        await a.voltar_no_tempo(cn, [op.id])
        lido = await perguntador.consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("resposta_invalida", None)
        linhas = await a.informacoes(cn, op.id)
        assert [(x.origem, x.desfecho) for x in linhas] == [("consulta", "resposta_invalida")]
        assert INSTRUCAO not in a.textos(linhas) + a.textos(await a.extrato(cn, protocolo=op.protocolo))


@pytest.mark.parametrize("quando", ["antes_do_envio", "depois_de_agora"])
async def test_evento_fora_da_janela_e_resposta_invalida_e_nao_erro(banco_migrado: Parametros, quando: str) -> None:
    """Um evento de ``depois`` com hora fora da janela (antes do envio menos 5 minutos, ou depois de agora mais 5) e
    ``resposta_invalida``, conferido antes de gravar: o banco recusaria a linha, e a consulta viraria erro (503)."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            envio = await s.enviar(c, op.id)
        await a.voltar_no_tempo(cn, [op.id])
        enviado_em = (await a.envio(cn, op.id)).gravado_em
        em = enviado_em - timedelta(hours=1) if quando == "antes_do_envio" else enviado_em + timedelta(days=2)
        dados = {
            "contrato": contrato.CONTRATO,
            "operacao": str(envio.chave),
            "estado": "concluida",
            "depois": [{"evento": "entregue", "em": em.isoformat()}],
        }
        perguntador = cn.perguntador()
        perguntador.ligacao = lambda _sistema: LigacaoQueResponde(dados)  # type: ignore[assignment, return-value]
        lido = await perguntador.consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("resposta_invalida", None)
        assert [(x.origem, x.desfecho, x.depois) for x in await a.informacoes(cn, op.id)] == [
            ("consulta", "resposta_invalida", [])
        ]


async def test_operacao_parada_em_sistema_fora_nao_conta_na_retirada(banco_migrado: Parametros) -> None:
    """Duas operacoes cuja escada parou em ``sistema_fora`` (o MCP-CLARO nao perguntou) e uma que termina agora sem
    resposta: so uma conta, e o selo fica."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        paradas = []
        for _ in range(2):
            op = await _escada_quase_cheia(cn)
            async with cn.dono.begin() as c:
                await s.informar(c, op, None, desfecho="sistema_fora")
            paradas.append(op)
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [*paradas, terceira])
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        assert (await a.selo_vigente(cn)).motivo == "vigente"
        assert await a.avisos_ao_admin(cn, "selo_retirado") == []


# --- Retoque da etapa 3, G3ar (decisoes 1 a 3 do coordenador e provas que faltavam) ----------------------------


async def _porta(cn: a.Cena, *, aberta: bool) -> None:
    async with cn.dono.begin() as c:
        if aberta:
            await c.execute(text("UPDATE estado_saida SET aberta = true, motivo = NULL WHERE id = 1"))
        else:
            await c.execute(
                text(
                    "UPDATE estado_saida SET aberta = false, motivo = 'teste', mudou_por = 'teste-g3ar',"
                    " mudou_em = now() WHERE id = 1"
                )
            )


async def _nao_confirmada(cn: a.Cena) -> s.OperacaoDeTeste:
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
        await s.enviar(c, op.id)
    return op


async def test_porta_fechada_nada_e_perguntado_nem_gravado(banco_migrado: Parametros) -> None:
    """Decisao 2 do coordenador: com a porta fechada, ``PortaFechada`` (409 ``porta_fechada``), nada perguntado e nada
    gravado (nem ``informacoes_sistema`` nem extrato), quantas vezes for. A escada nao anda: aberta a porta, a consulta
    vai na hora, sem esperar degrau nenhum. Antes, a consulta gravava ``sistema_fora`` e a escada parava para sempre
    (D9 da conferencia da etapa 3)."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await _nao_confirmada(cn)
        await a.voltar_no_tempo(cn, [op.id])
        antes = await a.extrato(cn, protocolo=op.protocolo)
        await _porta(cn, aberta=False)
        perguntador = cn.perguntador()
        segredo, aberturas = perguntador._segredo, []

        async def segredo_contado(*args: Any, **kw: Any) -> Any:
            aberturas.append(1)
            return await segredo(*args, **kw)

        perguntador._segredo = segredo_contado  # type: ignore[method-assign]
        for _ in range(3):
            with pytest.raises(PortaFechada):
                await perguntador.consultar(op.id)
        assert aberturas == []  # com a porta fechada, nem a chave e aberta (nem renovada)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        assert await a.informacoes(cn, op.id) == []
        assert await a.extrato(cn, protocolo=op.protocolo) == antes
        await _porta(cn, aberta=True)
        lido = await perguntador.consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("respondeu", "nao_recebida")
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


@pytest.mark.parametrize("mudanca", ["porta_fecha", "quarentena"])
async def test_porta_ou_quarentena_enquanto_a_chave_abre_nada_e_perguntado(
    banco_migrado: Parametros, mudanca: str
) -> None:
    """A chave pode demorar a abrir (a renovacao chama o servidor de autorizacao): a porta e a situacao do sistema sao
    conferidas de novo antes de chamar. A porta que fecha nesse meio tempo nao grava nada; a quarentena grava
    ``sistema_fora``. Nos dois, o duble conta zero consultas (critica A2)."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await _nao_confirmada(cn)
        await a.voltar_no_tempo(cn, [op.id])
        perguntador = cn.perguntador()
        segredo = perguntador._segredo

        async def chave_que_demora(*args: Any, **kw: Any) -> Any:
            if mudanca == "porta_fecha":
                await _porta(cn, aberta=False)
            else:
                async with cn.dono.begin() as c:
                    await c.execute(
                        text("UPDATE sistemas SET situacao = 'quarentena' WHERE id = :s"), {"s": cn.sistema.id}
                    )
            return await segredo(*args, **kw)

        perguntador._segredo = chave_que_demora  # type: ignore[method-assign]
        if mudanca == "porta_fecha":
            with pytest.raises(PortaFechada):
                await perguntador.consultar(op.id)
            assert await a.informacoes(cn, op.id) == []
        else:
            assert (await perguntador.consultar(op.id)).desfecho == "sistema_fora"
            assert [(x.origem, x.desfecho) for x in await a.informacoes(cn, op.id)] == [("consulta", "sistema_fora")]
            [linha] = [x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "sistema_fora_consulta"]
            assert "o sistema esta em quarentena" in linha.frase
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0


async def _ate_a_vez(cn: a.Cena, operacao_id: uuid.UUID, maximo: int = 8) -> float | None:
    """Os minutos ate a vez da proxima consulta, pela mesma funcao do contrato e com o relogio do banco (um segundo a
    mais, para ja ser a vez), ou ``None`` sem vez."""
    async with cn.dono.connect() as c:
        enviado_em, agora = (
            await c.execute(
                text("SELECT gravado_em, now() FROM envios_selo WHERE operacao_id = :o"), {"o": operacao_id}
            )
        ).one()
        linhas = [
            contrato.LinhaInformada(str(x.origem), str(x.desfecho), x.estado, x.gravado_em)
            for x in await c.execute(
                text(
                    "SELECT origem, desfecho, estado, gravado_em FROM informacoes_sistema WHERE operacao_id = :o"
                    " ORDER BY gravado_em, id"
                ),
                {"o": operacao_id},
            )
        ]
    vez = contrato.proxima_consulta(linhas, enviado_em=enviado_em, maximo=maximo, prazo_envio_segundos=a.PRAZO_ENVIO)
    return None if vez is None else max(0.0, (vez - agora).total_seconds() / 60) + 1 / 60


async def test_sem_a_chave_poucas_linhas_por_dia_e_a_escada_acaba_pelo_tempo(banco_migrado: Parametros) -> None:
    """Decisao 3 do coordenador e a sonda SONDA_SEM_CHAVE_CONSULTA do verificador (30 linhas em meia hora, sem fim):
    com a chave da pessoa apagada, cada consulta grava ``sem_chave`` e o degrau sobe mesmo assim. Um minuto antes da
    vez, 409. Em 7 dias pelo relogio do banco sao 48 perguntas (12 no primeiro dia e 6 por dia depois), uma linha
    ``consulta_nao_feita`` so no extrato da pessoa, nenhuma chamada ao sistema, e a escada acaba pelo tempo
    (``contrato.escada_vencida``), sem contar contra o selo."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await _nao_confirmada(cn)
        async with cn.dono.begin() as c:
            await c.execute(text("DELETE FROM chaves_cifradas WHERE sistema_id = :s"), {"s": cn.sistema.id})
        perguntador = cn.perguntador()
        feitas = 0
        while (minutos := await _ate_a_vez(cn, op.id)) is not None:
            if feitas in (3, 9) and minutos > 2:
                await a.voltar_no_tempo(cn, [op.id], minutos=minutos - 1)
                with pytest.raises(NadaAPerguntar):  # ainda nao e a vez: o degrau subiu sem gastar tentativa
                    await perguntador.consultar(op.id)
                minutos = 1.0
            await a.voltar_no_tempo(cn, [op.id], minutos=minutos)
            lido = await perguntador.consultar(op.id)
            assert (lido.desfecho, lido.estado) == ("sem_chave", None)
            feitas += 1
            assert feitas <= 48, "a escada sem a chave nao acaba"
        assert feitas == 48
        await a.voltar_no_tempo(cn, [op.id], minutos=300)
        with pytest.raises(NadaAPerguntar):
            await perguntador.consultar(op.id)
        linhas = await a.informacoes(cn, op.id)
        assert [(x.origem, x.desfecho) for x in linhas] == [("consulta", "sem_chave")] * 48
        tipos = [x.tipo for x in await a.extrato(cn, protocolo=op.protocolo)]
        assert tipos.count("consulta_nao_feita") == 1
        assert "sistema_nao_respondeu_consulta" not in tipos
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        assert (await a.selo_vigente(cn)).motivo == "vigente"


async def test_nada_e_perguntado_depois_do_fim_pelo_tempo(banco_migrado: Parametros) -> None:
    """Decisao 3: nenhuma consulta depois de ``envios_selo.gravado_em`` mais ``REAPROVEITAR_NUMERO_DIAS`` dias, mesmo
    que a vez pela escada tenha passado ha muito (as rotinas ficaram fora): a rota confere ``escada_vencida`` com o
    ``now()`` de agora. Dez minutos antes do fim, a consulta ainda vai."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        dentro, fora = await _nao_confirmada(cn), await _nao_confirmada(cn)
        await a.voltar_no_tempo(cn, [dentro.id], minutos=contrato.REAPROVEITAR_NUMERO_DIAS * 24 * 60 - 10)
        await a.voltar_no_tempo(cn, [fora.id], minutos=contrato.REAPROVEITAR_NUMERO_DIAS * 24 * 60)
        perguntador = cn.perguntador()
        with pytest.raises(NadaAPerguntar):
            await perguntador.consultar(fora.id)
        assert await a.informacoes(cn, fora.id) == []
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        assert (await perguntador.consultar(dentro.id)).desfecho == "respondeu"
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


async def test_linha_consulta_nao_feita_so_volta_com_outro_motivo(banco_migrado: Parametros) -> None:
    """Decisao 3: a linha ``consulta_nao_feita`` so e gravada quando a ultima desse tipo da operacao nao tem o mesmo
    motivo; ``informacoes_sistema`` continua com uma linha por pergunta."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await _nao_confirmada(cn)
        async with cn.dono.begin() as c:
            await c.execute(text("DELETE FROM chaves_cifradas WHERE sistema_id = :s"), {"s": cn.sistema.id})
        perguntador = cn.perguntador()
        segredo = perguntador._segredo
        trocas: list[str] = []

        async def segredo_com_troca(*args: Any, **kw: Any) -> Any:
            if trocas:
                return None, None, trocas.pop()
            return await segredo(*args, **kw)

        perguntador._segredo = segredo_com_troca  # type: ignore[method-assign]
        cofre = "o cofre de chaves esta trancado"
        for troca in (None, None, cofre, cofre, None):
            if troca is not None:
                trocas.append(troca)
            await a.voltar_no_tempo(cn, [op.id])
            assert (await perguntador.consultar(op.id)).desfecho == "sem_chave"
        frases = [x.frase for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "consulta_nao_feita"]
        assert [("autorizou" in f, cofre in f) for f in frases] == [(True, False), (False, True), (True, False)]
        assert len(await a.informacoes(cn, op.id)) == 5


async def test_nivel_3_sem_depois_na_consulta_e_resposta_invalida(banco_migrado: Parametros) -> None:
    """D7 da conferencia da etapa 3 (mutacao N3): a consulta confere a resposta pelo nivel do envio. No nivel 3, o
    ``depois`` e exigido: o sistema que responde sem ele (``consulta_sem_depois``) da ``resposta_invalida``."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="consulta_sem_depois") as cn:
        op = await _nao_confirmada(cn)
        assert (await a.envio(cn, op.id)).nivel == 3
        await a.voltar_no_tempo(cn, [op.id])
        lido = await cn.perguntador().consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("resposta_invalida", None)
        assert [(x.origem, x.desfecho, x.estado) for x in await a.informacoes(cn, op.id)] == [
            ("consulta", "resposta_invalida", None)
        ]
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


async def _na_escada(cn: a.Cena, quantas: int) -> list[uuid.UUID]:
    """Operacoes "nao confirmadas" com envio e UMA consulta sem resposta: ainda na escada."""
    ops: list[uuid.UUID] = []
    async with cn.dono.begin() as c:
        for _ in range(quantas):
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
            await s.informar(c, op.id, None, desfecho="sem_resposta")
            ops.append(op.id)
    return ops


async def test_operacoes_ainda_na_escada_nao_contam_contra_o_selo(banco_migrado: Parametros) -> None:
    """D2 (a) da conferencia da etapa 3: duas operacoes ainda na escada (uma consulta sem resposta cada) e a terceira
    termina agora sem resposta: uma so terminou assim, e o selo fica. E a prova de ponta a ponta, pelas duas camadas:
    as que ainda estao na escada saem no SQL (``_SQL_DA_RETIRADA``) e, se chegassem, ``terminou_sem_resposta`` daria
    ``None``. Este teste so falha quando as duas camadas falham juntas. Com o filtro do SQL, elas nunca chegam a
    ``terminou_sem_resposta``, entao ele NAO mata a mutacao N2 (``True`` na escada) sozinha: quem a mata e o teste
    de unidade ``test_selo_consulta_g3ar::test_operacao_ainda_na_escada_nao_conta_na_retirada``. O SQL sem o
    filtro, sozinho, morre por ``test_cinquenta_operacoes_na_escada_nao_seguram_a_retirada``."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        na_escada = await _na_escada(cn, 2)
        await a.voltar_no_tempo(cn, na_escada, minutos=100)
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [terceira])
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        assert (await a.selo_vigente(cn)).motivo == "vigente"
        assert await a.avisos_ao_admin(cn, "selo_retirado") == []


async def test_as_que_nao_contam_sao_puladas_e_a_sequencia_segue(banco_migrado: Parametros) -> None:
    """D2 (b) da conferencia da etapa 3 (mutacao N1): duas terminaram sem resposta; depois delas, uma ainda na escada
    e uma que chegou ao maximo e parou em ``sistema_fora`` (o maximo, um parametro, pode ter baixado). A terceira
    termina agora sem resposta: as duas do meio nao contam e sao puladas, sao tres seguidas, e o selo cai."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        primeiras = [await _escada_cheia(cn) for _ in range(2)]
        await a.voltar_no_tempo(cn, primeiras, minutos=600)
        [na_escada] = await _na_escada(cn, 1)
        parada = await _escada_cheia(cn)
        async with cn.dono.begin() as c:
            await s.informar(c, parada, None, desfecho="sistema_fora")
        await a.voltar_no_tempo(cn, [na_escada, parada], minutos=100)
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [terceira])
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo) == (1, "retirado")
        [aviso] = await a.avisos_ao_admin(cn, "selo_retirado")
        assert aviso.corpo["motivo"] == "nao_respondeu_consultas"


async def test_cinquenta_operacoes_na_escada_nao_seguram_a_retirada(banco_migrado: Parametros) -> None:
    """D4 da conferencia da etapa 3 (a sonda SONDA_JANELA: com 50 operacoes ainda na escada, consultadas depois do fim
    das duas primeiras, o selo ficava): as que ainda estao na escada saem no SQL, antes do limite de operacoes lidas, e
    tres seguidas sem resposta derrubam o selo."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        primeiras = [await _escada_cheia(cn) for _ in range(2)]
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [*primeiras, terceira])
        outras = await _na_escada(cn, consulta.OPERACOES_NA_CONTA_DA_RETIRADA)
        await a.voltar_no_tempo(cn, outras, minutos=100)
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo) == (1, "retirado")


async def test_com_mais_de_cinquenta_terminadas_a_conta_le_as_mais_recentes(banco_migrado: Parametros) -> None:
    """A conta da retirada le as ``OPERACOES_NA_CONTA_DA_RETIRADA`` (50) que terminaram mais perto de agora (``ORDER
    BY r.ultima DESC`` antes do ``LIMIT``, sonda SONDA_MAIS_DE_50 do verificador da etapa 3r). Ha 50 que terminaram
    ha mais tempo com resposta que encerra e, depois delas, tres seguidas sem resposta (a terceira agora, pela
    consulta de verdade): sao 53 terminadas, e as 50 mais recentes comecam pelas tres falhas, entao o selo cai. Lendo
    as 50 mais antigas (mutacao V4a), so as respostas que encerram entrariam, e as tres falhas de agora nunca
    derrubariam o selo."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=2, modo="consulta_muda") as cn:
        antigas: list[uuid.UUID] = []
        async with cn.dono.begin() as c:
            for _ in range(consulta.OPERACOES_NA_CONTA_DA_RETIRADA):
                op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
                await s.enviar(c, op.id)
                await s.informar(c, op.id, "concluida")
                antigas.append(op.id)
        await a.voltar_no_tempo(cn, antigas, minutos=900)  # ainda depois do teste do selo (um dia atras)
        primeiras = [await _escada_cheia(cn) for _ in range(2)]
        await a.voltar_no_tempo(cn, primeiras, minutos=300)
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [terceira])
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo) == (1, "retirado")
        [aviso] = await a.avisos_ao_admin(cn, "selo_retirado")
        assert aviso.corpo["motivo"] == "nao_respondeu_consultas"


# --- Conferencia final da etapa 3r (decisoes do coordenador e provas que faltavam) -----------------------------


async def _bloquear_o_sistema(cn: a.Cena, operacoes: list[Any], em_conferencia: tuple[int, ...] = ()) -> uuid.UUID:
    """Um bloqueio de alcance ``sistema`` (do ADMIN) e o comprovante dele, como a transacao do bloqueio os grava."""
    ja = [{"protocolo": op.protocolo, "estado": "enviada"} for i, op in enumerate(operacoes) if i not in em_conferencia]
    conferindo = [{"protocolo": operacoes[i].protocolo, "estado": "nao_confirmada"} for i in em_conferencia]
    async with cn.dono.begin() as c:
        bloqueio = (
            await c.execute(
                text(
                    "INSERT INTO bloqueios (tipo, alcance, sistema_id, quem, motivo)"
                    " VALUES ('bloqueio', 'sistema', :s, 'admin:teste', 'teste da consulta') RETURNING id"
                ),
                {"s": cn.sistema.id},
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


async def test_a_resposta_ao_aviso_depois_de_sistema_fora_nao_reabre_a_escada(banco_migrado: Parametros) -> None:
    """Defeito 3 do verificador do G3ar (decisao do coordenador): com o sistema desativado, a consulta grava
    ``sistema_fora`` e a escada para (critica A2). O sistema volta a ``ativo``, um bloqueio o avisa, e a resposta ao
    aviso entra depois do ``sistema_fora``: a rota continua dizendo 409 ``nada_a_perguntar``, o duble nao e chamado de
    novo, e a tela segue em "o sistema nao confirmou; a decisao e sua" (``Operacao.consulta_acabou``)."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await _nao_confirmada(cn)  # o duble nunca viu o numero
        await a.voltar_no_tempo(cn, [op.id])
        async with cn.dono.begin() as c:
            await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), {"s": cn.sistema.id})
        perguntador = cn.perguntador()
        assert (await perguntador.consultar(op.id)).desfecho == "sistema_fora"
        async with cn.dono.begin() as c:
            await c.execute(text("UPDATE sistemas SET situacao = 'ativo' WHERE id = :s"), {"s": cn.sistema.id})
        await _bloquear_o_sistema(cn, [op], em_conferencia=(0,))
        await perguntador.derivar_avisos()
        assert await perguntador.avisar_bloqueios() == 1
        linhas = [(x.origem, x.desfecho, x.estado) for x in await a.informacoes(cn, op.id)]
        assert linhas == [("consulta", "sistema_fora", None), ("aviso", "respondeu", "nao_recebida")]
        await a.voltar_no_tempo(cn, [op.id])
        antes = cn.chamadas(contrato.FERRAMENTA_CONSULTAR)
        with pytest.raises(NadaAPerguntar):
            await perguntador.consultar(op.id)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == antes
        assert len(await a.informacoes(cn, op.id)) == 2
        async with cn.dono.connect() as c:
            lida = await fila.por_id(c, op.id)
        assert lida is not None
        assert lida.informado is not None
        assert (lida.informado.origem, lida.informado.ultimo_desfecho) == ("aviso", "sistema_fora")
        assert lida.consulta_acabou(8)


async def test_o_recibo_no_meio_nao_interrompe_a_sequencia_da_retirada(banco_migrado: Parametros) -> None:
    """Mutacao X9 do verificador do G3ar (o filtro ``origem = 'consulta'`` de ``recentes`` na conta da retirada). No
    nivel 3 quase toda acao termina pelo recibo, sem consulta: duas escadas cheias sem resposta, uma acao confirmada so
    pelo recibo ``concluida`` e a terceira que termina agora sem resposta retiram o selo. A acao do recibo nao entra
    na conta (o selo nao e dela), e a sequencia segue."""
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="consulta_muda") as cn:
        primeiras = [await _escada_cheia(cn) for _ in range(2)]
        await a.voltar_no_tempo(cn, primeiras, minutos=300)
        async with cn.dono.begin() as c:
            pelo_recibo = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, pelo_recibo.id, fim="enviada")
            await s.informar(c, pelo_recibo.id, "concluida", origem="recibo")
        await a.voltar_no_tempo(cn, [pelo_recibo.id], minutos=100)
        terceira = await _escada_quase_cheia(cn)
        await a.voltar_no_tempo(cn, [terceira])
        assert (await cn.perguntador(prazo=0.5).consultar(terceira)).desfecho == "sem_resposta"
        vigente = await a.selo_vigente(cn)
        assert (vigente.nivel, vigente.motivo) == (1, "retirado")
