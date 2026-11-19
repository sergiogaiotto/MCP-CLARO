"""Cofre trancado fecha a porta de saida (N1 da terceira rodada), contra o PostgreSQL real.

Um cofre falso responde ``saude()`` trancado ou destravado e, como o de verdade, nao abre chave enquanto
estiver trancado (a cifra levanta ``CofreTrancado``). A porta e a de verdade (``PortaDeSaida.rodar``): a saida
fecha, a acao liberada espera sem sair (e nao vira "nao saiu"), a entrada recusa pedido novo, o aviso vai ao
ADMIN, e com o cofre destravado a saida reabre e a acao sai uma vez so. Fechada pelo ADMIN, a saida continua
fechada com o cofre destravado.

Rodada 3b: com o cofre trancado ou sem resposta logo antes de "enviando", a acao continua liberada (nao vira "nao
saiu"); rede que vai e volta nao abre e fecha a porta a cada poucas conferencias; com a saida ja fechada por
outro motivo, o ADMIN recebe um aviso de cofre trancado e um de cofre de volta, uma vez cada. Quem fecha de novo a
saida ja fechada nao recomeca a conta: o aviso seguinte continua de onde parou.

Mesmo arranjo de ``test_saida_disputas``: o app do despachante sobe sem a porta, e cada teste sobe a sua.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro import continuidade
from mcp_claro.cofre import CofreIndisponivel, CofreTrancado, EstadoCofre
from mcp_claro.regras import estado
from mcp_claro.regras.bloqueios import quem_admin
from mcp_claro.saida import cofre as saida_cofre
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import AcaoRecusada, Pedido
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import chamadas, esperar_estado, operacao
from tests.integracao.test_saida_correcoes import extrato, pedidos_de
from tests.integracao.test_saida_disputas import amb, esperar, nova_acao, vitrine
from tests.integracao.test_saida_rodada2 import tipos_depois, ultimo_id_do_extrato

__all__ = ["amb"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


class CofreFalso:
    """Saude do cofre que o teste muda, e a cifra do ambiente, que so abre com o cofre respondendo e destravado."""

    def __init__(self, amb: Ambiente) -> None:
        self.trancado = True
        self.fora_do_ar = False
        self._cifra = amb.cifra
        self.perguntas = 0

    async def saude(self) -> EstadoCofre:
        self.perguntas += 1
        if self.fora_do_ar:
            raise CofreIndisponivel("O cofre nao respondeu dentro do tempo limite.")
        return EstadoCofre(inicializado=True, trancado=self.trancado, versao="2.7.0")

    def _conferir(self) -> None:
        if self.fora_do_ar:
            raise CofreIndisponivel("O cofre nao respondeu dentro do tempo limite.")
        if self.trancado:
            raise CofreTrancado("O cofre esta trancado (sealed); a porta de saida fica fechada.")

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        self._conferir()
        return await self._cifra.cifrar(texto, contexto)

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        self._conferir()
        return await self._cifra.decifrar(cifrado, contexto)


def porta_com_cofre(
    amb: Ambiente, cofre: CofreFalso, email: str, *, conferir: float = 0.3, varredura: float = 0.2
) -> PortaDeSaida:
    return PortaDeSaida(
        amb.despachante_db,
        cofre,
        varredura=varredura,
        prazo_envio=5.0,
        cofre=cofre,
        conferir_cofre=conferir,
        emails_admin=[email],
    )


async def saida(amb: Ambiente) -> tuple[bool, str | None, str]:
    async with amb.admin.connect() as c:
        linha = (await c.execute(text("SELECT aberta, motivo, mudou_por FROM estado_saida WHERE id = 1"))).one()
    return bool(linha.aberta), linha.motivo, str(linha.mudou_por)


async def avisos(amb: Ambiente, email: str) -> list[Any]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT evento, assunto, corpo, chave_unica FROM avisos WHERE destino = :d ORDER BY id"),
                    {"d": email},
                )
            ).all()
        )


@contextlib.asynccontextmanager
async def rodando(p: PortaDeSaida) -> AsyncIterator[asyncio.Task[None]]:
    tarefa = asyncio.create_task(p.rodar())
    try:
        yield tarefa
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa


async def abrir_a_saida(amb: Ambiente) -> None:
    """Comeco e fim de cada teste: a saida aberta pelo ADMIN (outro teste pode te-la deixado fechada)."""
    if not (await saida(amb))[0]:
        await continuidade.reabrir_saida(amb.entrada_db, quem_admin(amb.admin_id), "Arranjo do teste do cofre")


async def test_cofre_trancado_fecha_a_saida_e_destravado_reabre_e_a_acao_sai_uma_vez(amb: Ambiente) -> None:
    marca = uuid.uuid4().hex[:8]
    email = f"admin-{marca}@exemplo.org"
    cofre = CofreFalso(amb)
    await abrir_a_saida(amb)
    antes = chamadas(amb, "criar_chamado")
    liberada = await nova_acao(amb, f"Cofre trancado {marca}")
    assert liberada.estado == Estado.LIBERADA
    desde = await ultimo_id_do_extrato(amb)
    try:
        async with rodando(porta_com_cofre(amb, cofre, email)):
            # 1. Trancado: a primeira volta confere o cofre antes de enviar, e a saida fecha.
            await esperar(lambda: _fechada_pelo_cofre(amb), prazo=10)
            assert await saida(amb) == (False, "o cofre esta trancado", "rotina:cofre")
            # Varias voltas e conferencias depois, a acao liberada continua esperando: nada saiu, nada virou
            # "nao saiu", e o cofre nao foi chamado para abrir chave.
            perguntas = cofre.perguntas
            await esperar(lambda: cofre.perguntas >= perguntas + 3, prazo=10)
            atual = await operacao(amb, liberada.protocolo)
            assert (atual.estado, atual.desfecho) == (Estado.LIBERADA, None)
            assert chamadas(amb, "criar_chamado") == antes
            assert await extrato(amb, "acao_nao_saiu", protocolo=liberada.protocolo) == []
            # A entrada nao da protocolo novo com a saida fechada.
            pedidos, aplicativo = await pedidos_de(amb)
            f = await vitrine(amb)
            with pytest.raises(AcaoRecusada) as recusa:
                await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, {"titulo": f"Novo com cofre {marca}"}, None))
            assert recusa.value.motivo == "saida_fechada"
            # Aviso ao ADMIN, uma vez, sem nada do cofre alem do motivo.
            fechou = await avisos(amb, email)
            assert [a.evento for a in fechou] == ["cofre_trancado"]
            assert fechou[0].corpo["motivo"] == "o cofre esta trancado"
            assert "trancado" in fechou[0].assunto
            assert fechou[0].chave_unica.startswith("saida-cofre:cofre_trancado:")

            # 2. Destravado: a saida reabre sozinha e a acao sai, uma vez.
            cofre.trancado = False
            enviada = await esperar_estado(amb, liberada.protocolo, Estado.ENVIADA, prazo=15)
            assert enviada.desfecho == "o sistema respondeu"
            assert await saida(amb) == (True, "o cofre voltou destravado", "rotina:cofre")
            perguntas = cofre.perguntas
            await esperar(lambda: cofre.perguntas >= perguntas + 3, prazo=10)
            assert chamadas(amb, "criar_chamado") == antes + 1
            assert len(await extrato(amb, "acao_enviada", protocolo=liberada.protocolo)) == 1
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado", "cofre_destravado"]
        tipos = await tipos_depois(amb, desde)
        assert tipos.count("cofre_fechou_saida") == 1
        assert tipos.count("cofre_reabriu_saida") == 1
        assert tipos.count("estado_saida_mudou") == 2
        assert "acao_nao_saiu" not in tipos
    finally:
        await abrir_a_saida(amb)


async def _fechada_pelo_cofre(amb: Ambiente) -> bool:
    aberta, _, quem = await saida(amb)
    return not aberta and quem == saida_cofre.QUEM


async def test_fechada_pelo_admin_continua_fechada_com_o_cofre_destravado(amb: Ambiente) -> None:
    await abrir_a_saida(amb)
    marca = uuid.uuid4().hex[:8]
    email = f"admin-{marca}@exemplo.org"
    cofre = CofreFalso(amb)
    p = porta_com_cofre(amb, cofre, email)
    try:
        assert await p.conferir_o_cofre() == saida_cofre.Decisao(saida_cofre.Passo.FECHAR, "o cofre esta trancado")
        # Depois do cofre, o ADMIN fecha a saida com o motivo dele: a decisao passa a ser dele.
        async with amb.entrada_db.begin() as c:
            await estado.mudar_saida(
                c, aberta=False, quem=quem_admin(amb.admin_id), motivo="manutencao do sistema", papel="entrada"
            )
        cofre.trancado = False
        # O ADMIN ja sabia do cofre trancado (o aviso de fechar): recebe so o aviso da volta, e a saida continua
        # fechada pelo motivo dele.
        assert await p.conferir_o_cofre() == saida_cofre.Decisao(
            saida_cofre.Passo.AVISAR_DESTRAVADO, saida_cofre.MOTIVO_REABRIU
        )
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        assert await saida(amb) == (False, "manutencao do sistema", quem_admin(amb.admin_id))
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado", "cofre_destravado"]
        # Fechada pela conferencia do banco na partida: tambem continua fechada, e nada a dizer de novo.
        async with amb.despachante_db.begin() as c:
            await estado.mudar_saida(c, aberta=False, quem=continuidade.QUEM, motivo="o vigia nao respondeu")
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        assert not (await saida(amb))[0]
        assert len(await avisos(amb, email)) == 2
    finally:
        await abrir_a_saida(amb)


async def test_cofre_fora_do_ar_fecha_na_segunda_conferencia_e_reabre_quando_volta(amb: Ambiente) -> None:
    await abrir_a_saida(amb)
    marca = uuid.uuid4().hex[:8]
    email = f"admin-{marca}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    cofre.fora_do_ar = True
    p = porta_com_cofre(amb, cofre, email)
    try:
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        assert (await saida(amb))[0], "uma conferencia sem resposta nao fecha"
        # Voltou a responder: a contagem recomeca.
        cofre.fora_do_ar = False
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        cofre.fora_do_ar = True
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        assert await p.conferir_o_cofre() == saida_cofre.Decisao(saida_cofre.Passo.FECHAR, "o cofre nao responde")
        assert await saida(amb) == (False, "o cofre nao responde", "rotina:cofre")
        assert await p.conferir_o_cofre() == saida_cofre.NADA  # ja fechada: nada de novo, nenhum aviso a mais
        cofre.fora_do_ar = False
        # Fechada porque o cofre nao respondia: reabre so na segunda conferencia destravada seguida.
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        assert not (await saida(amb))[0]
        assert await p.conferir_o_cofre() == saida_cofre.Decisao(saida_cofre.Passo.REABRIR, "o cofre voltou destravado")
        assert (await saida(amb))[0]
        fechou, reabriu = await avisos(amb, email)
        assert (fechou.evento, fechou.corpo["motivo"]) == ("cofre_trancado", "o cofre nao responde")
        assert reabriu.evento == "cofre_destravado"
    finally:
        await abrir_a_saida(amb)


class CofreQueTrancaNaChave(CofreFalso):
    """A saude diz destravado, mas a chave responde trancado: o cofre trancou entre a pergunta e a chave."""

    def __init__(self, amb: Ambiente) -> None:
        super().__init__(amb)
        self.trancado = False
        self.chave_trancada = False

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        if self.chave_trancada:
            raise CofreTrancado("O cofre esta trancado (sealed); a porta de saida fica fechada.")
        return await super().decifrar(cifrado, contexto)


async def test_cofre_que_tranca_entre_conferencias_nenhuma_acao_vira_nao_saiu(amb: Ambiente) -> None:
    """O cofre tranca entre duas conferencias, com tres acoes liberadas: a porta pergunta ao cofre antes de cada
    "enviando", nenhuma vira "nao saiu", a saida fecha na hora, e com o cofre de volta as tres saem uma vez."""
    await abrir_a_saida(amb)
    marca = uuid.uuid4().hex[:8]
    email = f"admin-{marca}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    antes = chamadas(amb, "criar_chamado")
    p = porta_com_cofre(amb, cofre, email, conferir=600)
    try:
        async with rodando(p):
            await asyncio.wait_for(p.assumiu.wait(), 10)
            await esperar(lambda: cofre.perguntas >= 1, prazo=10)
            assert (await saida(amb))[0]
            cofre.trancado = True  # a conferencia regular so viria daqui a 10 minutos
            ops = [await nova_acao(amb, f"Cofre tranca no meio {i} {marca}") for i in range(3)]
            await esperar(lambda: _fechada_pelo_cofre(amb), prazo=10)
            assert await saida(amb) == (False, "o cofre esta trancado", "rotina:cofre")
            await asyncio.sleep(0.5)
            for op in ops:
                atual = await operacao(amb, op.protocolo)
                assert (atual.estado, atual.desfecho) == (Estado.LIBERADA, None)
                assert await extrato(amb, "acao_nao_saiu", protocolo=op.protocolo) == []
            assert chamadas(amb, "criar_chamado") == antes
            # O cofre volta: a conferencia seguinte (antecipada aqui) reabre, e as tres saem uma vez cada.
            cofre.trancado = False
            p._cofre_conferido_em = None
            p._acordar.set()
            for op in ops:
                assert (await esperar_estado(amb, op.protocolo, Estado.ENVIADA, prazo=15)).desfecho == (
                    "o sistema respondeu"
                )
            assert chamadas(amb, "criar_chamado") == antes + 3
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado", "cofre_destravado"]
    finally:
        await abrir_a_saida(amb)


async def test_cofre_sem_resposta_antes_de_enviando_a_acao_espera_e_sai_quando_volta(amb: Ambiente) -> None:
    """O cofre deixa de responder entre duas conferencias: a acao espera liberada (nao vira "nao saiu"), sem
    chamar o sistema; quando o cofre volta, ela sai uma vez. Uma falha so nao fecha a saida."""
    await abrir_a_saida(amb)
    marca = uuid.uuid4().hex[:8]
    email = f"admin-{marca}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    antes = chamadas(amb, "criar_chamado")
    p = porta_com_cofre(amb, cofre, email, conferir=600)
    try:
        async with rodando(p):
            await asyncio.wait_for(p.assumiu.wait(), 10)
            await esperar(lambda: cofre.perguntas >= 1, prazo=10)
            cofre.fora_do_ar = True
            op = await nova_acao(amb, f"Cofre mudo no meio {marca}")
            perguntas = cofre.perguntas
            await esperar(lambda: cofre.perguntas >= perguntas + 3, prazo=10)
            atual = await operacao(amb, op.protocolo)
            assert (atual.estado, atual.desfecho) == (Estado.LIBERADA, None)
            assert chamadas(amb, "criar_chamado") == antes
            assert (await saida(amb))[0], "so a conferencia regular conta as falhas para fechar"
            cofre.fora_do_ar = False
            assert (await esperar_estado(amb, op.protocolo, Estado.ENVIADA, prazo=15)).desfecho == "o sistema respondeu"
            assert chamadas(amb, "criar_chamado") == antes + 1
        assert await avisos(amb, email) == []
    finally:
        await abrir_a_saida(amb)


async def test_chave_que_responde_trancado_fecha_a_saida_sem_esperar_o_intervalo(amb: Ambiente) -> None:
    """O cofre tranca entre a pergunta de antes de "enviando" e a chave: so a acao daquele instante nao sai, e a
    saida fecha na volta seguinte, sem esperar a conferencia regular nem perguntar a saude de novo."""
    await abrir_a_saida(amb)
    marca = uuid.uuid4().hex[:8]
    email = f"admin-{marca}@exemplo.org"
    cofre = CofreQueTrancaNaChave(amb)
    antes = chamadas(amb, "criar_chamado")
    p = porta_com_cofre(amb, cofre, email, conferir=600)
    try:
        async with rodando(p):
            await asyncio.wait_for(p.assumiu.wait(), 10)
            await esperar(lambda: cofre.perguntas >= 1, prazo=10)
            assert (await saida(amb))[0]
            cofre.chave_trancada = True
            op = await nova_acao(amb, f"Cofre tranca na chave {marca}")
            atual = await esperar_estado(amb, op.protocolo, Estado.NAO_SAIU, prazo=10)
            assert atual.desfecho == "o cofre de chaves esta trancado"
            await esperar(lambda: _fechada_pelo_cofre(amb), prazo=10)
            # Uma pergunta na conferencia da partida e uma antes de "enviando"; o fechamento usou a resposta da chave.
            assert cofre.perguntas == 2
            assert await saida(amb) == (False, "o cofre esta trancado", "rotina:cofre")
            assert chamadas(amb, "criar_chamado") == antes
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado"]
    finally:
        await abrir_a_saida(amb)


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


async def test_rede_que_vai_e_volta_nao_abre_e_fecha_a_porta_a_cada_poucas_conferencias(amb: Ambiente) -> None:
    """Sem resposta duas vezes e de volta uma (ou duas): a saida fecha e espera o cofre firmar antes de reabrir.

    Conferencias a cada 15 s (relogio do teste). Duas falhas, uma resposta: nao reabre nunca (precisa de duas
    seguidas). Duas falhas, duas respostas: reabre uma vez; se fechar de novo na mesma hora, so reabre depois de
    30 minutos seguidos de cofre destravado. Depois de uma hora sem fechar, volta a reabrir com duas respostas.
    """
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    relogio = Relogio()
    p = porta_com_cofre(amb, cofre, email)
    p._relogio = relogio
    F, R, N = saida_cofre.Passo.FECHAR, saida_cofre.Passo.REABRIR, saida_cofre.Passo.NADA

    async def conferir(fora: bool) -> saida_cofre.Passo:
        relogio.agora += 15
        cofre.fora_do_ar = fora
        return (await p.conferir_o_cofre()).passo

    try:
        passos = [await conferir(fora) for fora in (True, True, False) * 6]
        assert passos == [N, F, N] + [N, N, N] * 5
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado"]
        # Duas respostas seguidas: reabre.
        assert [await conferir(True), await conferir(False), await conferir(False)] == [N, N, R]
        # Fecha de novo na mesma hora: duas, dez respostas nao bastam; 30 minutos seguidos bastam.
        passos = [await conferir(fora) for fora in (True, True, False, False, True, True)]
        assert passos == [N, F, N, N, N, N]
        passos = [await conferir(False) for _ in range(120)]
        assert set(passos) == {N}
        assert await conferir(False) is R
        fechou_de_novo = await avisos(amb, email)
        assert [a.evento for a in fechou_de_novo] == [
            "cofre_trancado",
            "cofre_destravado",
            "cofre_trancado",
            "cofre_destravado",
        ]
        assert "fechou de novo" in fechou_de_novo[2].assunto
        assert "30 minutos seguidos" in fechou_de_novo[2].corpo["texto"]
        assert "fechou de novo" not in fechou_de_novo[0].assunto
        # Uma hora sem fechar: a proxima queda volta a reabrir com duas respostas.
        relogio.agora += saida_cofre.JANELA_INSTAVEL
        passos = [await conferir(fora) for fora in (True, True, False, False)]
        assert passos == [N, F, N, R]
        assert len(await avisos(amb, email)) == 6
    finally:
        await abrir_a_saida(amb)


async def test_ciclos_repetidos_de_cofre_trancado_um_aviso_por_mudanca(amb: Ambiente) -> None:
    """Fecha, reabre, fecha, reabre: quatro avisos com chaves diferentes; conferencias sem mudanca nao avisam."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    p = porta_com_cofre(amb, cofre, email)
    F, R, N = saida_cofre.Passo.FECHAR, saida_cofre.Passo.REABRIR, saida_cofre.Passo.NADA
    try:
        passos = []
        for trancado in (True, True, False, False, True, True, False):
            cofre.trancado = trancado
            passos.append((await p.conferir_o_cofre()).passo)
        assert passos == [F, N, R, N, F, N, R]
        todos = await avisos(amb, email)
        assert [a.evento for a in todos] == ["cofre_trancado", "cofre_destravado", "cofre_trancado", "cofre_destravado"]
        assert len({a.chave_unica for a in todos}) == 4
        # Reaberta depois de "trancado", nao conta como rede que vai e volta: sem resposta logo depois, a saida
        # fecha com o aviso de sempre e reabre com duas respostas seguidas.
        cofre.trancado = False
        passos = []
        for fora in (True, True, False, False):
            cofre.fora_do_ar = fora
            passos.append((await p.conferir_o_cofre()).passo)
        assert passos == [N, F, N, R]
        assert "fechou de novo" not in (await avisos(amb, email))[4].assunto
    finally:
        await abrir_a_saida(amb)


async def _fechar_pela_continuidade(amb: Ambiente) -> None:
    async with amb.despachante_db.begin() as c:
        await estado.mudar_saida(c, aberta=False, quem=continuidade.QUEM, motivo="o vigia nao respondeu")


async def test_cofre_trancado_com_a_saida_ja_fechada_avisa_uma_vez_e_avisa_a_volta(amb: Ambiente) -> None:
    """Reinicio comum: a conferencia do banco fecha a saida na partida e o cofre volta trancado. O ADMIN recebe
    o aviso de cofre trancado na hora, uma vez, e o de cofre de volta; a saida continua fechada (decisao dele).
    Outra porta (reinicio do despachante) no mesmo fechamento nao repete o aviso: o extrato guarda o que foi dito.
    """
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)  # trancado
    p = porta_com_cofre(amb, cofre, email)
    A_T, A_D, N = saida_cofre.Passo.AVISAR_TRANCADO, saida_cofre.Passo.AVISAR_DESTRAVADO, saida_cofre.Passo.NADA
    try:
        await _fechar_pela_continuidade(amb)
        fechada = await saida(amb)
        desde = await ultimo_id_do_extrato(amb)
        assert [(await p.conferir_o_cofre()).passo for _ in range(4)] == [A_T, N, N, N]
        assert await saida(amb) == fechada
        trancado = await avisos(amb, email)
        assert [a.evento for a in trancado] == ["cofre_trancado"]
        assert trancado[0].assunto == "MCP-CLARO: o cofre esta trancado"
        assert "nao reabre sozinha" in trancado[0].corpo["texto"]
        assert set(trancado[0].corpo) == {"texto", "motivo"}
        # Reinicio do despachante com o cofre ainda trancado: a porta nova le o extrato e nao repete.
        outra = porta_com_cofre(amb, cofre, email)
        assert await outra.conferir_o_cofre() == saida_cofre.NADA
        # O cofre volta: um aviso, e a saida continua fechada pela conferencia do banco.
        cofre.trancado = False
        assert [(await outra.conferir_o_cofre()).passo for _ in range(3)] == [A_D, N, N]
        assert await p.conferir_o_cofre() == saida_cofre.NADA  # a primeira porta tambem ve que ja foi dito
        assert await saida(amb) == fechada
        # Tranca de novo no mesmo fechamento: outro aviso.
        cofre.trancado = True
        assert (await outra.conferir_o_cofre()).passo is A_T
        assert [a.evento for a in await avisos(amb, email)] == ["cofre_trancado", "cofre_destravado", "cofre_trancado"]
        tipos = await tipos_depois(amb, desde)
        assert tipos.count("cofre_trancado_saida_fechada") == 2
        assert tipos.count("cofre_destravado_saida_fechada") == 1
        assert "estado_saida_mudou" not in tipos
        assert "cofre_fechou_saida" not in tipos
        # O ADMIN fecha de novo, com o motivo dele: o ultimo aviso ja foi o de cofre trancado, e nada se repete.
        async with amb.entrada_db.begin() as c:
            await estado.mudar_saida(
                c, aberta=False, quem=quem_admin(amb.admin_id), motivo="manutencao", papel="entrada"
            )
        assert (await outra.conferir_o_cofre()).passo is N
        assert len(await avisos(amb, email)) == 3
        # O cofre volta: o aviso da volta sai, no fechamento do ADMIN.
        cofre.trancado = False
        assert [(await outra.conferir_o_cofre()).passo for _ in range(2)] == [A_D, N]
        assert [a.evento for a in await avisos(amb, email)][-1] == "cofre_destravado"
        assert len(await avisos(amb, email)) == 4
    finally:
        await abrir_a_saida(amb)


async def test_saida_fechada_pelo_admin_com_o_cofre_bem_nao_avisa(amb: Ambiente) -> None:
    """Fechada pelo ADMIN e o cofre destravado o tempo todo: nada a dizer (nenhum aviso, nenhuma linha)."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    p = porta_com_cofre(amb, cofre, email)
    try:
        async with amb.entrada_db.begin() as c:
            await estado.mudar_saida(
                c, aberta=False, quem=quem_admin(amb.admin_id), motivo="manutencao", papel="entrada"
            )
        desde = await ultimo_id_do_extrato(amb)
        for _ in range(3):
            assert await p.conferir_o_cofre() == saida_cofre.NADA
        assert await avisos(amb, email) == []
        assert "cofre_destravado_saida_fechada" not in await tipos_depois(amb, desde)
    finally:
        await abrir_a_saida(amb)


async def test_admin_fecha_de_novo_e_o_aviso_da_volta_nao_depende_da_hora_da_conferencia(amb: Ambiente) -> None:
    """O cofre fecha a saida e o ADMIN fecha de novo com o motivo dele. Com ou sem conferencia entre as duas coisas,
    o ADMIN recebe o mesmo: um aviso de cofre trancado (o de fechar) e um de cofre de volta, nenhum repetido."""
    A_D, N, F = saida_cofre.Passo.AVISAR_DESTRAVADO, saida_cofre.Passo.NADA, saida_cofre.Passo.FECHAR
    resultados = {}
    try:
        for caso, conferencia_no_meio in (("sem conferencia no meio", False), ("com conferencia no meio", True)):
            await abrir_a_saida(amb)
            email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
            cofre = CofreFalso(amb)
            p = porta_com_cofre(amb, cofre, email)
            assert (await p.conferir_o_cofre()).passo is F
            async with amb.entrada_db.begin() as c:
                await estado.mudar_saida(
                    c, aberta=False, quem=quem_admin(amb.admin_id), motivo="o ADMIN assumiu", papel="entrada"
                )
            passos = []
            if conferencia_no_meio:
                passos.append((await p.conferir_o_cofre()).passo)
            cofre.trancado = False
            passos.append((await p.conferir_o_cofre()).passo)
            passos.append((await p.conferir_o_cofre()).passo)
            resultados[caso] = (passos, [a.evento for a in await avisos(amb, email)])
        assert resultados["sem conferencia no meio"] == ([A_D, N], ["cofre_trancado", "cofre_destravado"])
        assert resultados["com conferencia no meio"] == ([N, A_D, N], ["cofre_trancado", "cofre_destravado"])
    finally:
        await abrir_a_saida(amb)


async def test_porta_que_assume_de_novo_rele_o_extrato_e_avisa_a_volta(amb: Ambiente) -> None:
    """Duas copias do despachante: a trava passa da porta A para a B e volta. A B avisou "trancado" no meio; a A,
    ao assumir de novo, esquece o que lembrava e rele o extrato: o aviso da volta do cofre sai."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    a = porta_com_cofre(amb, cofre, email)
    b = porta_com_cofre(amb, cofre, email)
    A_T, A_D = saida_cofre.Passo.AVISAR_TRANCADO, saida_cofre.Passo.AVISAR_DESTRAVADO
    try:
        await _fechar_pela_continuidade(amb)
        assert (await a.conferir_o_cofre()).passo is A_T
        cofre.trancado = False
        assert (await a.conferir_o_cofre()).passo is A_D
        await b.assumir()
        cofre.trancado = True
        assert (await b.conferir_o_cofre()).passo is A_T
        await a.assumir()
        cofre.trancado = False
        assert (await a.conferir_o_cofre()).passo is A_D
        eventos = [x.evento for x in await avisos(amb, email)]
        assert eventos == ["cofre_trancado", "cofre_destravado", "cofre_trancado", "cofre_destravado"]
    finally:
        await abrir_a_saida(amb)


async def test_cofre_mudo_antes_de_enviando_nao_pergunta_sem_parar(amb: Ambiente) -> None:
    """Cofre sem resposta com uma acao na hora: a porta pergunta uma vez por varredura (1 s), nao a cada 50 ms.
    Cada pergunta grava um log; sem a espera, seria uma volta quase sem pausa."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    p = porta_com_cofre(amb, cofre, email, varredura=1.0, conferir=600)
    try:
        async with rodando(p):
            await asyncio.wait_for(p.assumiu.wait(), 10)
            await esperar(lambda: _perguntou(cofre, 1), prazo=10)
            cofre.fora_do_ar = True
            op = await nova_acao(amb, f"Cofre mudo conta perguntas {uuid.uuid4().hex[:6]}")
            await esperar(lambda: _perguntou(cofre, 2), prazo=10)
            antes = cofre.perguntas
            await asyncio.sleep(3.0)
            assert cofre.perguntas - antes <= 6, cofre.perguntas - antes
            assert (await operacao(amb, op.protocolo)).estado == Estado.LIBERADA
            cofre.fora_do_ar = False
            await esperar_estado(amb, op.protocolo, Estado.ENVIADA, prazo=15)
    finally:
        await abrir_a_saida(amb)


async def test_saida_fechada_acorda_na_hora_da_conferencia_do_cofre(amb: Ambiente) -> None:
    """Varredura longa (3 s) e conferencia curta (0,3 s): com a saida fechada pelo cofre, a porta confere a cada
    0,3 s, nao so a cada varredura, e reabre logo que o cofre volta."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)  # trancado
    p = porta_com_cofre(amb, cofre, email, varredura=3.0, conferir=0.3)
    try:
        async with rodando(p):
            await esperar(lambda: _fechada_pelo_cofre(amb), prazo=10)
            antes = cofre.perguntas
            await asyncio.sleep(2.0)
            assert cofre.perguntas - antes >= 4, cofre.perguntas - antes
            cofre.trancado = False
            await esperar(lambda: _aberta(amb), prazo=10)
    finally:
        await abrir_a_saida(amb)


async def _perguntou(cofre: CofreFalso, vezes: int) -> bool:
    return cofre.perguntas >= vezes


async def _aberta(amb: Ambiente) -> bool:
    return (await saida(amb))[0]


async def test_sem_resposta_e_o_admin_fecha_de_novo_o_aviso_de_trancado_ainda_sai(amb: Ambiente) -> None:
    """O cofre nao respondeu e a saida fechou; o ADMIN fecha de novo com o motivo dele. O ADMIN so sabia que o cofre
    nao respondia: quando ele aparece trancado, o aviso de trancado sai (uma vez), e depois o da volta."""
    A_T, A_D, N, F = (
        saida_cofre.Passo.AVISAR_TRANCADO,
        saida_cofre.Passo.AVISAR_DESTRAVADO,
        saida_cofre.Passo.NADA,
        saida_cofre.Passo.FECHAR,
    )
    resultados = {}
    try:
        for caso, depois in (("trancado", True), ("destravado", False)):
            await abrir_a_saida(amb)
            email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
            cofre = CofreFalso(amb)
            cofre.trancado = False
            cofre.fora_do_ar = True
            p = porta_com_cofre(amb, cofre, email)
            passos = [(await p.conferir_o_cofre()).passo for _ in range(2)]
            async with amb.entrada_db.begin() as c:
                await estado.mudar_saida(
                    c, aberta=False, quem=quem_admin(amb.admin_id), motivo="o ADMIN assumiu", papel="entrada"
                )
            cofre.fora_do_ar = False
            cofre.trancado = depois
            passos += [(await p.conferir_o_cofre()).passo for _ in range(2)]
            if depois:
                cofre.trancado = False
                passos += [(await p.conferir_o_cofre()).passo for _ in range(2)]
            resultados[caso] = (passos, [(a.evento, a.assunto) for a in await avisos(amb, email)])
        assert resultados["trancado"] == (
            [N, F, A_T, N, A_D, N],
            [
                ("cofre_trancado", "MCP-CLARO: o cofre nao responde e a porta de saida fechou"),
                ("cofre_trancado", "MCP-CLARO: o cofre esta trancado"),
                ("cofre_destravado", "MCP-CLARO: o cofre voltou, e a porta de saida continua fechada"),
            ],
        )
        assert resultados["destravado"] == (
            [N, F, A_D, N],
            [
                ("cofre_trancado", "MCP-CLARO: o cofre nao responde e a porta de saida fechou"),
                ("cofre_destravado", "MCP-CLARO: o cofre voltou, e a porta de saida continua fechada"),
            ],
        )
    finally:
        await abrir_a_saida(amb)


async def test_sem_resposta_que_passa_a_trancado_nao_diz_que_a_porta_fechou_de_novo(amb: Ambiente) -> None:
    """Fechada porque o cofre nao respondia, e ele volta a responder trancado: a porta continua fechada desde a mesma
    hora, o motivo passa a "trancado", e o extrato e o aviso dizem que ela continua fechada, sem outro "fechou".
    Destravado, reabre na primeira conferencia (regra do cofre trancado)."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    cofre.fora_do_ar = True
    p = porta_com_cofre(amb, cofre, email)
    try:
        desde = await ultimo_id_do_extrato(amb)
        assert [(await p.conferir_o_cofre()).passo for _ in range(2)] == [
            saida_cofre.Passo.NADA,
            saida_cofre.Passo.FECHAR,
        ]
        async with amb.admin.connect() as c:
            fechada_em = (await c.execute(text("SELECT mudou_em FROM estado_saida WHERE id = 1"))).scalar_one()
        cofre.fora_do_ar = False
        cofre.trancado = True
        assert await p.conferir_o_cofre() == saida_cofre.Decisao(saida_cofre.Passo.FECHAR, saida_cofre.MOTIVO_TRANCADO)
        assert await p.conferir_o_cofre() == saida_cofre.NADA
        async with amb.admin.connect() as c:
            linha = (await c.execute(text("SELECT aberta, motivo, mudou_em FROM estado_saida WHERE id = 1"))).one()
        assert (linha.aberta, linha.motivo, linha.mudou_em) == (False, saida_cofre.MOTIVO_TRANCADO, fechada_em)
        tipos = await tipos_depois(amb, desde)
        assert tipos.count("cofre_fechou_saida") == 1
        assert tipos.count("estado_saida_mudou") == 1
        assert tipos.count("cofre_passou_a_trancado") == 1
        assert [a.assunto for a in await avisos(amb, email)] == [
            "MCP-CLARO: o cofre nao responde e a porta de saida fechou",
            "MCP-CLARO: o cofre voltou a responder, mas esta trancado",
        ]
        cofre.trancado = False
        assert (await p.conferir_o_cofre()).passo is saida_cofre.Passo.REABRIR
        assert (await saida(amb))[0]
    finally:
        await abrir_a_saida(amb)


async def test_porta_que_assume_de_novo_recomeca_as_contagens_do_cofre(amb: Ambiente) -> None:
    """A porta A conferiu o cofre bem muitas vezes; a B assume, o cofre para de responder e a B fecha a saida. A A
    assume de novo: uma resposta destravada so nao reabre (a regra das duas seguidas vale de novo). E uma falta de
    resposta antiga da A nao conta junto com a primeira depois de assumir."""
    await abrir_a_saida(amb)
    email = f"admin-{uuid.uuid4().hex[:8]}@exemplo.org"
    cofre = CofreFalso(amb)
    cofre.trancado = False
    a = porta_com_cofre(amb, cofre, email)
    b = porta_com_cofre(amb, cofre, email)
    N, F, R = saida_cofre.Passo.NADA, saida_cofre.Passo.FECHAR, saida_cofre.Passo.REABRIR
    try:
        assert [(await a.conferir_o_cofre()).passo for _ in range(5)] == [N] * 5
        await b.assumir()
        cofre.fora_do_ar = True
        assert [(await b.conferir_o_cofre()).passo for _ in range(2)] == [N, F]
        await a.assumir()
        cofre.fora_do_ar = False
        assert [(await a.conferir_o_cofre()).passo for _ in range(2)] == [N, R]
        # Uma falta de resposta, a troca de porta e outra falta: a saida continua aberta.
        cofre.fora_do_ar = True
        assert (await a.conferir_o_cofre()).passo is N
        await a.assumir()
        assert (await a.conferir_o_cofre()).passo is N
        assert (await saida(amb))[0]
    finally:
        await abrir_a_saida(amb)


async def test_indice_dos_avisos_do_cofre_vale_em_todas_as_particoes_do_extrato(amb: Ambiente) -> None:
    """A busca dos avisos do cofre com a saida fechada (com a trava exclusiva de "tudo") passa so pelas linhas do
    indice parcial: ele existe no extrato e em cada particao."""
    async with amb.admin.connect() as c:
        definicao = (
            await c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'extrato_saida_cofre_idx'"))
        ).scalar_one()
        particoes = (
            await c.execute(text("SELECT count(*) FROM pg_inherits WHERE inhparent = 'extrato'::regclass"))
        ).scalar_one()
        indices = (
            await c.execute(
                text("SELECT count(*) FROM pg_inherits WHERE inhparent = 'extrato_saida_cofre_idx'::regclass")
            )
        ).scalar_one()
    assert "(criado_em, id)" in definicao
    for tipo in (
        "cofre_trancado_saida_fechada",
        "cofre_destravado_saida_fechada",
        "estado_saida_mudou",
        "intervalo_perdido",
        "aplicativo_pausado_continuidade",
    ):
        assert tipo in definicao
    assert particoes > 0
    assert indices == particoes
