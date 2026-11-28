"""A consulta ao proprio sistema (desenho 5.2, 5.5 e 12.1 G3; criticas A2, A3.3, M7, M8 e B8).

Com o duble de verdade e o cortador (``tests/cortador.py``, so usado) entre o despachante e ele:

- resposta perdida de proposito: a acao fica "nao confirmada", a consulta pergunta, ``concluida`` fica gravada e o
  extrato da pessoa ganha ``sistema_informou``; o duble conta uma chamada so da acao;
- a vez e do banco: antes da hora, 409 e nada e perguntado; uma ``nao_recebida`` sozinha nao encerra (A3.3);
- sem resposta: 8 tentativas, ``sistema_nao_respondeu_consulta`` no fim; com 3 operacoes seguidas assim (ou com
  ``desconhecido`` no fim, B8), o selo e retirado e o ADMIN e avisado; depois de um reteste, so contam as que
  terminaram depois do teste novo, e as paradas em ``sistema_fora`` nunca contam;
- respostas invalidas (instrucao no estado, resposta grande, outro numero) dao ``resposta_invalida``; o membro a mais
  e ignorado; nada do texto do sistema chega ao banco nem ao extrato;
- chave apagada, ou recusada pelo sistema (401, a chave fica marcada vencida): ``sem_chave``, sem gastar tentativa
  nem contar contra o sistema (M7); erro JSON-RPC e evento fora da janela da hora sao ``resposta_invalida``;
- fora de ``ativo`` ou com a porta fechada, nada e perguntado (``sistema_fora``, A2), e a escada para;
- contradicao: o sistema que se contradiz perde o selo, e o extrato e o ADMIN ficam sabendo (depois de um reteste,
  o aviso ao ADMIN e do selo de agora);
- duas copias do despachante ao mesmo tempo: uma pergunta so (M8).

O relogio da escada e o do banco: ``voltar_no_tempo`` poe o envio e as respostas para tras.
"""

from __future__ import annotations

import asyncio
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
from mcp_claro.saida.despachante import ligacao_sem_sala
from mcp_claro.selo import contrato
from mcp_claro.selo.consulta import NadaAPerguntar, SemEnvio
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


@pytest.mark.parametrize("fora", ["quarentena", "desativado", "porta_fechada"])
async def test_fora_de_ativo_ou_porta_fechada_nada_e_perguntado(banco_migrado: Parametros, fora: str) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
            await s.enviar(c, op.id)
            if fora == "porta_fechada":
                await c.execute(
                    text(
                        "UPDATE estado_saida SET aberta = false, motivo = 'teste', mudou_por = 'teste-g3a',"
                        " mudou_em = now() WHERE id = 1"
                    )
                )
            else:
                await c.execute(
                    text("UPDATE sistemas SET situacao = :f WHERE id = :s"), {"f": fora, "s": cn.sistema.id}
                )
        await a.voltar_no_tempo(cn, [op.id])
        perguntador = cn.perguntador()
        lido = await perguntador.consultar(op.id)
        assert (lido.desfecho, lido.estado) == ("sistema_fora", None)
        assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
        [linha] = [x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "sistema_fora_consulta"]
        esperado = {
            "quarentena": "o sistema esta em quarentena",
            "desativado": "o sistema nao esta ativo",
            "porta_fechada": "a porta de saida esta fechada",
        }[fora]
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


async def test_contradicao_depois_do_reteste_avisa_o_admin_do_selo_de_agora(banco_migrado: Parametros) -> None:
    """Duas operacoes do teste antigo com o recibo ``em_andamento``. A primeira se contradiz (o sistema diz que nao
    recebeu): o selo cai e o ADMIN e avisado. Vem um reteste aprovado. A segunda tambem se contradiz: o banco retira o
    selo de agora, e o ADMIN e avisado dele (um aviso por teste retirado)."""
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
