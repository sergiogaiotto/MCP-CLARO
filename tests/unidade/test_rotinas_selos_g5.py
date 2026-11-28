"""Rotinas dos selos sem banco (G5, Fase 4): a vez da consulta pela regra da rota, o cliente da rota da consulta, as
contagens do aviso e do resumo semanal e os textos dos avisos.

- ``selos.vale_perguntar`` e a mesma regra de ``selo.consulta._vale_perguntar`` (a rota), em toda combinacao de fila
  e linhas; ``selos.vez_de_perguntar`` e ``contrato.proxima_consulta`` mais ``contrato.escada_vencida`` com o
  ``agora`` dado (o ``now()`` do banco), e a resposta ao aviso de bloqueio nunca encerra;
- ``ClienteDespachante.conferir`` manda exatamente ``{"operacao_id": "<uuid>"}`` a ``contrato.ROTA_CONSULTAR`` e
  separa as respostas do contrato da rota (``contrato.ConfereNoDespachante``);
- os motivos legiveis dos avisos sao os do despachante e nao trazem palavra que o projeto nao usa perto do selo
  (critica B6).
"""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx2
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.rotinas import resumo_semanal, selos
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.despachante import (
    ClienteDespachante,
    Conferida,
    DespachanteFora,
    NadaAPerguntar,
    PortaFechada,
    SemEnvio,
)
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita
from mcp_claro.selo import consulta, contrato

pytestmark = pytest.mark.anyio

OPERACAO = uuid.UUID("00000000-0000-4000-8000-00000000abcd")
ENVIO = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
PALAVRAS_PROIBIDAS = ("garante", "certificado", "conforme", "aprovado no", "mws")


def linha(origem: str, desfecho: str, estado: str | None, minutos: float) -> contrato.LinhaInformada:
    return contrato.LinhaInformada(origem, desfecho, estado, ENVIO + timedelta(minutes=minutos))


# --- A vez da consulta ---------------------------------------------------------------------------------------

_LINHAS = st.lists(
    st.tuples(
        st.sampled_from(contrato.ORIGENS),
        st.sampled_from(contrato.DESFECHOS),
        st.sampled_from((None, *contrato.ESTADOS)),
    ),
    max_size=6,
)


@settings(max_examples=400, deadline=None)
@given(fila=st.sampled_from(("nao_confirmada", "enviada", "enviando", "nao_saiu", "liberada")), brutas=_LINHAS)
def test_vale_perguntar_e_a_regra_da_rota(fila: str, brutas: list[tuple[str, str, str | None]]) -> None:
    linhas = [linha(o, d, e if d == "respondeu" else None, i) for i, (o, d, e) in enumerate(brutas)]
    assert selos.vale_perguntar(fila, linhas) == consulta._vale_perguntar(fila, linhas)


def test_vale_perguntar_nos_casos_do_desenho() -> None:
    """Desenho 5.2 e critica B9: a ``nao_confirmada`` e a ``enviada`` com o ultimo estado dito pelo recibo ou pela
    consulta em aberto; a resposta ao aviso nao conta (decisao 1 do coordenador no retoque da etapa 3)."""
    assert selos.vale_perguntar("nao_confirmada", [])
    assert not selos.vale_perguntar("enviada", [])
    for estado in contrato.ESTADOS:
        recibo = [linha("recibo", "respondeu", estado, 0)]
        assert selos.vale_perguntar("enviada", recibo) is (estado in ("em_andamento", "desconhecido"))
    aberto_e_aviso = [linha("recibo", "respondeu", "em_andamento", 0), linha("aviso", "respondeu", "parada", 1)]
    assert selos.vale_perguntar("enviada", aberto_e_aviso)
    so_aviso = [linha("aviso", "respondeu", "em_andamento", 1)]
    assert not selos.vale_perguntar("enviada", so_aviso)
    assert not selos.vale_perguntar("enviando", [])


def _vez(fila: str, linhas: list[contrato.LinhaInformada], agora: datetime) -> datetime | None:
    return selos.vez_de_perguntar(fila, linhas, enviado_em=ENVIO, agora=agora, maximo=8, prazo_envio_segundos=20.0)


def test_primeira_consulta_so_depois_do_prazo_do_envio_e_da_margem() -> None:
    """Critica A3.3: o piso e o envio mais ``RESPONSE_MAX_WAIT_SECONDS`` mais ``MARGEM_DA_CONSULTA_SEGUNDOS``."""
    piso = ENVIO + timedelta(seconds=20 + contrato.MARGEM_DA_CONSULTA_SEGUNDOS)
    assert _vez("nao_confirmada", [], piso - timedelta(seconds=1)) is None
    assert _vez("nao_confirmada", [], piso) == piso


def test_escada_por_degrau_a_partir_da_ultima_linha() -> None:
    linhas = [linha("consulta", "sem_resposta", None, 5)]
    vez = ENVIO + timedelta(minutes=5 + contrato.ESCADA_DA_CONSULTA_MINUTOS[1])
    assert _vez("nao_confirmada", linhas, vez - timedelta(seconds=1)) is None
    assert _vez("nao_confirmada", linhas, vez) == vez


def test_nao_recebida_sozinha_nao_encerra_e_a_segunda_encerra() -> None:
    uma = [linha("consulta", "respondeu", "nao_recebida", 5)]
    assert _vez("nao_confirmada", uma, ENVIO + timedelta(hours=1)) is not None
    duas = [*uma, linha("consulta", "respondeu", "nao_recebida", 8)]
    assert _vez("nao_confirmada", duas, ENVIO + timedelta(hours=6)) is None


def test_resposta_ao_aviso_nunca_encerra() -> None:
    linhas = [linha("aviso", "respondeu", "parada", 3)]
    assert _vez("nao_confirmada", linhas, ENVIO + timedelta(hours=1)) is not None


@pytest.mark.parametrize(
    "linhas",
    [
        [linha("consulta", "respondeu", "concluida", 3)],
        [linha("recibo", "respondeu", "recusada", 0)],
        [linha("consulta", "sistema_fora", None, 3)],
        [linha("consulta", "sem_resposta", None, i) for i in range(8)],
        [linha("consulta", "sistema_fora", None, 3), linha("aviso", "respondeu", "em_andamento", 4)],
    ],
    ids=["concluida", "recibo_final", "sistema_fora", "maximo", "aviso_nao_reabre"],
)
def test_escada_acabada_nao_tem_vez(linhas: list[contrato.LinhaInformada]) -> None:
    assert _vez("nao_confirmada", linhas, ENVIO + timedelta(days=1)) is None


def test_escada_vencida_pelo_tempo_nao_tem_vez() -> None:
    fim = ENVIO + timedelta(days=contrato.REAPROVEITAR_NUMERO_DIAS)
    perto_do_fim = [linha("consulta", "sem_chave", None, 60 * 24 * contrato.REAPROVEITAR_NUMERO_DIAS - 1)]
    assert _vez("nao_confirmada", perto_do_fim, fim + timedelta(hours=1)) is None  # a vez passaria do fim
    assert _vez("nao_confirmada", [], fim) is None
    assert _vez("nao_confirmada", [], fim - timedelta(seconds=1)) is not None


@settings(max_examples=300, deadline=None)
@given(brutas=_LINHAS, minutos=st.integers(min_value=0, max_value=60 * 24 * 8))
def test_vez_de_perguntar_e_a_proxima_consulta_do_contrato(
    brutas: list[tuple[str, str, str | None]], minutos: int
) -> None:
    linhas = [linha(o, d, e if d == "respondeu" else None, i) for i, (o, d, e) in enumerate(brutas)]
    agora = ENVIO + timedelta(minutes=minutos)
    esperado = contrato.proxima_consulta(linhas, enviado_em=ENVIO, maximo=8, prazo_envio_segundos=20.0)
    vez = _vez("nao_confirmada", linhas, agora)
    if esperado is None or esperado > agora or contrato.escada_vencida(ENVIO, agora):
        assert vez is None
    else:
        assert vez == esperado


# --- O cliente da rota da consulta ---------------------------------------------------------------------------


def _cliente(responder: Callable[[httpx2.Request], httpx2.Response]) -> ClienteDespachante:
    p = Parametros(DISPATCHER_INTERNAL_URL="http://despachante:9443")
    return ClienteDespachante(p, http=httpx2.AsyncClient(transport=httpx2.MockTransport(responder)))


def _resposta(status: int, corpo: object, vistos: list[httpx2.Request] | None = None) -> Callable[..., Any]:
    def responder(pedido: httpx2.Request) -> httpx2.Response:
        if vistos is not None:
            vistos.append(pedido)
        if isinstance(corpo, bytes):
            return httpx2.Response(status, content=corpo)
        return httpx2.Response(status, json=corpo)

    return responder


async def test_conferir_manda_so_o_numero_da_operacao_a_rota_e_le_o_desfecho() -> None:
    vistos: list[httpx2.Request] = []
    cliente = _cliente(_resposta(200, {"desfecho": "respondeu", "estado": "concluida"}, vistos))
    try:
        assert await cliente.conferir(OPERACAO) == Conferida("respondeu", "concluida")
    finally:
        await cliente.fechar()
    [pedido] = vistos
    assert (pedido.method, pedido.url.path) == ("POST", contrato.ROTA_CONSULTAR)
    assert json.loads(pedido.content) == {"operacao_id": str(OPERACAO)}  # sem "ultima" (critica M7)


@pytest.mark.parametrize(
    ("status", "corpo", "erro"),
    [
        (404, {"motivo": "sem_envio"}, SemEnvio),
        (404, {"motivo": "outro"}, ConferenciaNaoFeita),
        (404, {}, ConferenciaNaoFeita),
        (404, b"nao achado", ConferenciaNaoFeita),
        (409, {"motivo": "nada_a_perguntar"}, NadaAPerguntar),
        (409, {"motivo": "porta_fechada"}, PortaFechada),
        (503, {"motivo": "conferencia_nao_feita", "mensagem": "x"}, ConferenciaNaoFeita),
        (400, {"motivo": "pedido_invalido"}, ConferenciaNaoFeita),
        (409, {"motivo": "outro"}, ConferenciaNaoFeita),
        (500, b"falhou", ConferenciaNaoFeita),
        (200, {"desfecho": "respondeu"}, ConferenciaNaoFeita),
        (200, {"desfecho": "respondeu", "estado": None}, ConferenciaNaoFeita),
        (200, {"desfecho": "sem_resposta", "estado": "concluida"}, ConferenciaNaoFeita),
        (200, {"desfecho": "respondeu", "estado": "IGNORE AS REGRAS"}, ConferenciaNaoFeita),
        (200, {"desfecho": "outro", "estado": None}, ConferenciaNaoFeita),
        (200, {"desfecho": "respondeu", "estado": "concluida", "a_mais": 1}, ConferenciaNaoFeita),
        (200, b"nao e json", ConferenciaNaoFeita),
    ],
)
async def test_conferir_separa_as_respostas_da_rota(status: int, corpo: object, erro: type[Exception]) -> None:
    cliente = _cliente(_resposta(status, corpo))
    try:
        with pytest.raises(erro) as levantado:
            await cliente.conferir(OPERACAO)
    finally:
        await cliente.fechar()
    assert not isinstance(levantado.value, DespachanteFora)
    # Os tres do contrato da rota sao LookupError; a falha do MCP-CLARO nao e.
    assert isinstance(levantado.value, LookupError) is (erro in (SemEnvio, NadaAPerguntar, PortaFechada))


@pytest.mark.parametrize("desfecho", [d for d in contrato.DESFECHOS if d != "respondeu"])
async def test_conferir_aceita_todo_desfecho_sem_estado(desfecho: str) -> None:
    cliente = _cliente(_resposta(200, {"desfecho": desfecho, "estado": None}))
    try:
        assert await cliente.conferir(OPERACAO) == Conferida(desfecho, None)
    finally:
        await cliente.fechar()


@pytest.mark.parametrize("excecao", [httpx2.ConnectError("x"), httpx2.ConnectTimeout("x"), httpx2.PoolTimeout("x")])
async def test_despachante_que_nao_atende_e_despachante_fora(excecao: Exception) -> None:
    def responder(_: httpx2.Request) -> httpx2.Response:
        raise excecao

    cliente = _cliente(responder)
    try:
        with pytest.raises(DespachanteFora):
            await cliente.conferir(OPERACAO)
    finally:
        await cliente.fechar()


async def test_resposta_que_nao_chega_inteira_e_conferencia_nao_feita_e_nao_para_a_rodada() -> None:
    def responder(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("x")

    cliente = _cliente(responder)
    try:
        with pytest.raises(ConferenciaNaoFeita) as levantado:
            await cliente.conferir(OPERACAO)
    finally:
        await cliente.fechar()
    assert not isinstance(levantado.value, DespachanteFora)


# --- Avisos ---------------------------------------------------------------------------------------------------


def test_motivos_da_retirada_sao_os_do_despachante() -> None:
    """O aviso da mesma retirada sai uma vez so, com a mesma chave; quem mandar primeiro, manda o mesmo texto."""
    assert set(selos.MOTIVOS_DA_RETIRADA) == set(contrato.MOTIVOS_DE_RETIRADA)
    for motivo, texto in consulta.MOTIVOS_DA_RETIRADA.items():
        assert selos.MOTIVOS_DA_RETIRADA[motivo] == texto


def test_motivos_da_queda_sao_os_de_mudanca_do_sistema() -> None:
    assert set(selos.MOTIVOS_DA_QUEDA) == set(contrato.MOTIVOS_DO_SELO) - {
        "nunca_testado",
        "reprovado",
        "vencido",
        "retirado",
        "vigente",
    }


def test_queda_que_se_desfaz_diz_que_o_selo_volta_sozinho() -> None:
    """O aviso ``selo_retirado`` diz "peca um teste novo"; o selo que caiu porque o sistema mudou volta sozinho quando
    a mudanca se desfaz (``selo_vigente`` e calculado na hora), e o motivo diz isso. A revisao nova nao volta."""
    for motivo, texto in selos.MOTIVOS_DA_QUEDA.items():
        assert texto.endswith(selos.VOLTA_SOZINHO) is (motivo != "revisao_nova"), motivo
    _, corpo = aviso_admin("selo_retirado", sistema="Cobranca", motivo=selos.MOTIVOS_DA_QUEDA["endereco_mudou"])
    assert "mudou depois do teste; se a mudanca se desfizer, o selo volta sozinho, sem teste novo." in corpo


def test_chave_do_nao_aprovado_e_a_da_bateria() -> None:
    """Defeito S2 do verificador da etapa 4: a bateria avisa com ``selo_nao_aprovado|{teste}``; as rotinas, com a
    mesma chave, nao mandam o segundo aviso (a prova pelo caminho de verdade esta em test_rotinas_selos)."""
    teste = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
    assert selos.chave_do_nao_aprovado(teste) == f"selo_nao_aprovado|{teste}"


def test_textos_dos_avisos_sem_acento_e_sem_palavra_proibida() -> None:
    textos = [*selos.MOTIVOS_DA_RETIRADA.values(), *selos.MOTIVOS_DA_QUEDA.values()]
    for texto in textos:
        assert texto.isascii(), texto
        assert not any(p in texto.lower() for p in PALAVRAS_PROIBIDAS), texto
        _, corpo = aviso_admin("selo_retirado", sistema="Cobranca", motivo=texto)
        assert texto in corpo


def test_casos_que_nao_aprovam_contam_falhou_e_obrigatorio_sem_aprovar() -> None:
    ferramenta = str(uuid.uuid4())
    itens: list[dict[str, Any]] = []
    for caso in contrato.CASOS:
        if caso.nivel > 2:
            continue
        alvo = None if caso.escopo == "sistema" else ferramenta
        resultado = caso.maximo if caso.obrigatorio else "nao_testado"
        itens.append({"caso": caso.caso, "ferramenta": alvo, "resultado": resultado})
    assert contrato.casos_aprovam(cast(Any, itens), [ferramenta], 2)
    assert selos.casos_que_nao_aprovam(itens, 2) == 0
    obrigatorios = [i for i in itens if contrato.CASOS_POR_NOME[i["caso"]].obrigatorio]
    obrigatorios[0]["resultado"] = "falhou"
    obrigatorios[1]["resultado"] = "nao_testado"
    so_informa = next(i for i in itens if not contrato.CASOS_POR_NOME[i["caso"]].obrigatorio)
    assert selos.casos_que_nao_aprovam(itens, 2) == 2
    so_informa["resultado"] = "falhou"  # um caso que so informa e falhou tambem reprova (casos_aprovam)
    assert selos.casos_que_nao_aprovam(itens, 2) == 3
    assert not contrato.casos_aprovam(cast(Any, itens), [ferramenta], 2)
    # Num teste de nivel 3, um caso 3.x obrigatorio que nao aprova conta; num de nivel 2, nao.
    caso_3 = next(c for c in contrato.CASOS if c.nivel == 3 and c.obrigatorio)
    item_3 = {"caso": caso_3.caso, "ferramenta": None, "resultado": "nao_testado"}
    assert selos.casos_que_nao_aprovam([item_3], 3) == 1
    assert selos.casos_que_nao_aprovam([item_3], 2) == 0


def test_dias_ate_arredonda_para_cima() -> None:
    agora = datetime(2026, 11, 28, 12, tzinfo=UTC)
    assert selos.dias_ate(agora + timedelta(days=13, hours=12), agora) == 14
    assert selos.dias_ate(agora + timedelta(days=14), agora) == 14
    assert selos.dias_ate(agora + timedelta(minutes=1), agora) == 1
    assert selos.dias_ate(agora - timedelta(minutes=1), agora) == 0


# --- Resumo semanal ------------------------------------------------------------------------------------------


def test_contagens_do_selo_no_resumo() -> None:
    casos = {
        ("selo_informacoes", "consulta", "respondeu"): ("selo_consultas", "selo_consultas_respondidas"),
        ("selo_informacoes", "consulta", "sem_resposta"): ("selo_consultas", "selo_consultas_sem_resposta"),
        ("selo_informacoes", "consulta", "resposta_invalida"): ("selo_consultas", "selo_consultas_sem_resposta"),
        ("selo_informacoes", "consulta", "sem_chave"): ("selo_consultas", "selo_consultas_nao_feitas"),
        ("selo_informacoes", "consulta", "sala_fora"): ("selo_consultas", "selo_consultas_nao_feitas"),
        ("selo_informacoes", "consulta", "sistema_fora"): ("selo_consultas", "selo_consultas_nao_feitas"),
        ("selo_informacoes", "consulta", "desconhecido_no_fim"): ("selo_desconhecido_no_fim",),
        ("selo_informacoes", "recibo", "respondeu"): ("selo_recibos",),
        ("selo_informacoes", "aviso", "respondeu"): ("selo_respostas_ao_aviso",),
        ("selo_avisos_bloqueio", None, "respondido"): ("selo_avisos_bloqueio_respondidos",),
        ("selo_avisos_bloqueio", None, "sem_resposta"): ("selo_avisos_bloqueio_sem_resposta",),
        ("selo_avisos_bloqueio", None, "derivado"): (),
        ("selo_envios", None, "sem_numero"): ("selo_envios_sem_numero",),
        ("selo_envios", None, "com_numero"): (),
        ("selo_retiradas", None, None): ("selo_retiradas",),
        ("selo_numero_reaproveitado", None, None): ("selo_numero_reaproveitado",),
        ("selo_sistema_contradito", None, None): ("selo_sistema_contradito",),
        ("selo_respostas_invalidas", "recibo", None): ("selo_respostas_invalidas",),
        ("leituras", None, None): (),
    }
    for (nome, origem, desfecho), esperado in casos.items():
        assert resumo_semanal.contagem_do_selo(nome, origem, desfecho) == esperado, (nome, origem, desfecho)
    todas = {c for e in casos.values() for c in e}
    feitas_por_tabela = {"selo_testes_aprovados", "selo_testes_nao_aprovados"} | {
        f"selo_sistemas_nivel_{n}" for n in contrato.NIVEIS
    }
    assert todas | feitas_por_tabela == set(resumo_semanal.CONTAGENS_DO_SELO)


def test_linhas_do_selo_no_resumo() -> None:
    numeros = {k: i + 1 for i, k in enumerate(resumo_semanal.CONTAGENS_DO_SELO)}
    linhas = resumo_semanal.linhas_do_selo(numeros)
    assert len(linhas) == len(resumo_semanal.CONTAGENS_DO_SELO)
    for linha_, (_, n) in zip(linhas, numeros.items(), strict=True):
        assert linha_.endswith(f": {n}"), linha_
        assert linha_.isascii()
        assert not any(p in linha_.lower() for p in PALAVRAS_PROIBIDAS), linha_


# --- Tarefas ---------------------------------------------------------------------------------------------------


def test_tarefas_dos_selos_com_os_parametros() -> None:
    p = Parametros(BADGE_LOOKUP_INTERVAL_SECONDS=45, BADGE_LOOKUP_MAX_ATTEMPTS=5, RESPONSE_MAX_WAIT_SECONDS=30)
    cfg = ConfigRotinas(intervalo_aviso_vencer=1800.0, prazo_tarefa=123.0)
    tarefas = selos.tarefas_dos_selos(cast(Any, object()), p, cfg, cast(Any, object()))
    assert [(t.nome, t.intervalo, t.prazo) for t in tarefas] == [
        ("conferir_pelo_sistema", 45.0, 123.0),
        ("avisar_selos", 1800.0, 123.0),
    ]


async def test_tarefa_da_consulta_passa_o_maximo_e_o_prazo_do_envio(monkeypatch: pytest.MonkeyPatch) -> None:
    """O despachante e as rotinas contam a escada com os mesmos parametros (``BADGE_LOOKUP_MAX_ATTEMPTS`` e
    ``RESPONSE_MAX_WAIT_SECONDS``)."""
    vistos: list[dict[str, Any]] = []

    async def conferir(*args: Any, **kwargs: Any) -> None:
        vistos.append({"args": args, **kwargs})

    monkeypatch.setattr(selos, "conferir_pelo_sistema", conferir)
    p = Parametros(BADGE_LOOKUP_MAX_ATTEMPTS=5, RESPONSE_MAX_WAIT_SECONDS=30)
    engine, cliente = cast(Any, object()), cast(Any, object())
    (tarefa, _) = selos.tarefas_dos_selos(engine, p, ConfigRotinas(), cliente)
    await tarefa.rodar()
    [visto] = vistos
    assert visto == {"args": (engine, cliente), "maximo": 5, "prazo_envio_segundos": 30.0}


def test_combinacoes_da_fila_sem_linhas() -> None:
    """Fila fora de ``nao_confirmada`` e ``enviada`` nunca tem vez (as rotinas nao perguntam o que nao saiu)."""
    for fila, minutos in itertools.product(("aguardando_ficha", "liberada", "enviando", "nao_saiu"), (0, 600)):
        assert _vez(fila, [], ENVIO + timedelta(minutes=minutos)) is None


# --- O relogio e o do banco -------------------------------------------------------------------------------------


class _Resultado:
    def __init__(self, linhas: list[Any]) -> None:
        self.linhas = linhas

    def scalar_one(self) -> Any:
        [linha] = self.linhas
        return linha

    def all(self) -> list[Any]:
        return self.linhas

    def __iter__(self) -> Any:
        return iter(self.linhas)


@dataclass
class _Linha:
    operacao_id: uuid.UUID
    enviado_em: datetime
    fila: str = "nao_confirmada"
    substituto: uuid.UUID | None = None


@dataclass
class _BancoFalso:
    """Um banco com outro relogio: ``SELECT now()`` devolve ``agora``, que nao e a hora da maquina."""

    agora: datetime
    candidatas: list[_Linha]
    sqls: list[str] = field(default_factory=list)

    async def execute(self, sql: Any, params: Any = None) -> _Resultado:
        texto = str(sql)
        self.sqls.append(texto)
        if texto == "SELECT now()":
            return _Resultado([self.agora])
        if texto == selos.SQL_CANDIDATAS:
            return _Resultado(list(self.candidatas))
        if texto == selos.SQL_LINHAS:
            return _Resultado([])
        raise AssertionError(texto)


async def test_a_vez_e_pelo_relogio_do_banco_lido_na_mesma_transacao() -> None:
    """A vez sai do ``now()`` do banco (o mesmo relogio de ``gravado_em``), nunca do relogio da maquina: num banco
    cujo relogio esta anos a frente, a operacao enviada ha duas horas por esse relogio esta na vez, e a enviada agora
    por esse relogio nao esta."""
    agora = datetime(2031, 1, 1, 12, tzinfo=UTC)
    na_vez, cedo = uuid.uuid4(), uuid.uuid4()
    banco = _BancoFalso(agora, [_Linha(na_vez, agora - timedelta(hours=2)), _Linha(cedo, agora)])
    prontas = await selos.operacoes_na_vez(cast(Any, banco), maximo=8, prazo_envio_segundos=20.0)
    assert [x.operacao_id for x in prontas] == [na_vez]
    assert banco.sqls[0] == "SELECT now()"
    # E a operacao com outro protocolo depois, com o mesmo numero (selo_substituto), fica de fora.
    banco = _BancoFalso(agora, [_Linha(na_vez, agora - timedelta(hours=2), substituto=uuid.uuid4())])
    assert await selos.operacoes_na_vez(cast(Any, banco), maximo=8, prazo_envio_segundos=20.0) == []
