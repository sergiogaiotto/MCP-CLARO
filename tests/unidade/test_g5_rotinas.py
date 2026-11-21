"""Rotinas da Fase 3 sem banco (G5): quando a conferencia do mudou-parou conta como "o sistema nao respondeu".

``rotinas.despachante.ClienteDespachante.declaracoes`` separa a falha do MCP-CLARO (``ConferenciaNaoFeita``: nao
conta rodada sem resposta de um servidor de terceiros) da falha do sistema (qualquer outra: conta).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import AVISOS_ADMIN, aviso_admin
from mcp_claro.regras.sinais import MAIOR_JANELA_MINUTOS
from mcp_claro.rotinas import acoes, mudou_parou
from mcp_claro.rotinas import app as app_rotinas
from mcp_claro.rotinas import despachante as rotinas_despachante
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.despachante import MOTIVOS_DO_MCP_CLARO, ClienteDespachante
from mcp_claro.rotinas.mudou_parou import RODADAS_SEM_RESPOSTA, ConferenciaNaoFeita

pytestmark = pytest.mark.anyio

RAIZ = Path(__file__).resolve().parents[2]
SISTEMA = uuid.UUID(int=7)


def _cliente(responder: Callable[[httpx2.Request], httpx2.Response]) -> ClienteDespachante:
    p = Parametros(DISPATCHER_INTERNAL_URL="http://despachante:9443")
    return ClienteDespachante(p, http=httpx2.AsyncClient(transport=httpx2.MockTransport(responder)))


def _resposta(status: int, corpo: object) -> Callable[[httpx2.Request], httpx2.Response]:
    def responder(pedido: httpx2.Request) -> httpx2.Response:
        assert pedido.url.path == "/interno/declaracoes"
        assert json.loads(pedido.content) == {"sistema_id": str(SISTEMA)}  # nunca uma chave: as rotinas nao tem
        return httpx2.Response(status, json=corpo)

    return responder


async def test_declaracoes_devolve_a_lista_do_despachante() -> None:
    cliente = _cliente(_resposta(200, {"ferramentas": [{"name": "a"}, "lixo", {"name": "b"}]}))
    try:
        assert await cliente.declaracoes(SISTEMA) == [{"name": "a"}, {"name": "b"}]
    finally:
        await cliente.fechar()


@pytest.mark.parametrize(
    ("status", "corpo"),
    [
        (503, {"mensagem": "o despachante esta subindo"}),
        # A sala de visitas fora e a chave que o cofre nao abriu: 503 com motivo proprio (o despachante, G3r).
        (503, {"motivo": "sala_fora", "mensagem": "x"}),
        (503, {"motivo": "chave_indisponivel", "mensagem": "x"}),
        (404, {"mensagem": "sistema inativo"}),
        *((502, {"motivo": m, "mensagem": "x"}) for m in sorted(MOTIVOS_DO_MCP_CLARO)),
        (500, {"motivo": "sala_fora"}),
    ],
)
async def test_falha_do_mcp_claro_e_conferencia_nao_feita(status: int, corpo: object) -> None:
    cliente = _cliente(_resposta(status, corpo))
    try:
        with pytest.raises(ConferenciaNaoFeita):
            await cliente.declaracoes(SISTEMA)
    finally:
        await cliente.fechar()


@pytest.mark.parametrize(
    "erro", [httpx2.ConnectError("recusada"), httpx2.ConnectTimeout("prazo"), httpx2.PoolTimeout("pool")]
)
async def test_despachante_que_nao_atende_e_conferencia_nao_feita(erro: Exception) -> None:
    def responder(_: httpx2.Request) -> httpx2.Response:
        raise erro

    cliente = _cliente(responder)
    try:
        with pytest.raises(ConferenciaNaoFeita):
            await cliente.declaracoes(SISTEMA)
    finally:
        await cliente.fechar()


@pytest.mark.parametrize(
    ("status", "corpo"),
    [
        (502, {"motivo": "declaracoes_indisponiveis", "mensagem": "o sistema nao entregou as declaracoes"}),
        (500, "erro interno"),
        (400, {"mensagem": "sistema_id e obrigatorio"}),
        (200, {"sem": "lista"}),
    ],
)
async def test_sistema_que_nao_entregou_conta_como_sem_resposta(status: int, corpo: object) -> None:
    cliente = _cliente(_resposta(status, corpo))
    try:
        with pytest.raises((httpx2.HTTPStatusError, TypeError)) as erro:
            await cliente.declaracoes(SISTEMA)
        assert not isinstance(erro.value, ConferenciaNaoFeita)
    finally:
        await cliente.fechar()


async def test_despachante_lento_depois_de_receber_conta_como_sem_resposta() -> None:
    """O pedido chegou ao despachante e ele nao respondeu a tempo (lendo um servidor lento): conta."""

    def responder(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("prazo da leitura")

    cliente = _cliente(responder)
    try:
        with pytest.raises(httpx2.ReadTimeout):
            await cliente.declaracoes(SISTEMA)
    finally:
        await cliente.fechar()


def test_duas_rodadas_seguidas_levam_a_quarentena() -> None:
    assert RODADAS_SEM_RESPOSTA == 2


def test_limpar_sinais_usa_a_maior_janela_do_alerta_e_o_gatilho_do_banco() -> None:
    """A limpeza apaga so o que o gatilho ``sinais_guarda`` deixa (mais velho que a maior janela, 7 dias)."""
    assert MAIOR_JANELA_MINUTOS == 10080
    assert "make_interval(mins => :minutos)" in acoes.SQL_LIMPAR_SINAIS
    migracao = (RAIZ / "migrations" / "versions" / "0006_fase3.py").read_text("utf-8")
    assert re.search(rf"interval '{MAIOR_JANELA_MINUTOS} minutes'", migracao)


def test_alerta_alto_de_sistema_recusado_nao_conta_terceiro() -> None:
    """D30: a recusa de um servidor de terceiros (motivo cadastro_terceiro) fica fora do alerta alto."""
    regras = yaml.safe_load((RAIZ / "grafana" / "provisionamento" / "alerting" / "regras.yaml").read_text("utf-8"))
    (regra,) = (
        r for grupo in regras["groups"] for r in grupo["rules"] if r["uid"] == "mcp-claro-sistema-recusado-versao"
    )
    assert regra["labels"]["severity"] == "alta"
    (consulta,) = (p["model"]["rawSql"] for p in regra["data"] if p["refId"] == "A")
    assert "medicoes.v_recusas_versao_sistema" in consulta
    assert "motivo <> 'cadastro_terceiro'" in consulta


def test_apelido_de_antes_da_fase_3_faz_a_rodada_inteira() -> None:
    from mcp_claro.rotinas import app, mudou_parou

    assert mudou_parou.conferir_sistemas_proprios is mudou_parou.conferir_sistemas
    assert app.conferir_sistemas is mudou_parou.conferir_sistemas


async def test_tarefa_do_mudou_parou_passa_o_proprio_intervalo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A soma das rodadas sem resposta compara com a metade do intervalo da tarefa ``mudou_parou``: o app passa o
    intervalo da configuracao (``TOOL_DRIFT_CHECK_SECONDS``), e nao outro."""
    vistos: list[dict[str, Any]] = []

    async def conferir(*args: Any, **kwargs: Any) -> None:
        vistos.append({"args": args, **kwargs})

    monkeypatch.setattr(app_rotinas, "conferir_sistemas", conferir)
    cfg = ConfigRotinas(intervalo_mudou_parou=1234.0, intervalo_curto=7.0, emails_admin=("a@exemplo.teste",))
    engine = cast(AsyncEngine, object())
    (tarefa,) = (t for t in app_rotinas.montar_tarefas(engine, cfg, cast(Any, object())) if t.nome == "mudou_parou")
    assert tarefa.intervalo == 1234.0
    await tarefa.rodar()
    ((visto,),) = [vistos]
    assert visto["intervalo"] == 1234.0
    assert visto["args"][0] is engine
    assert visto["args"][2] == ("a@exemplo.teste",)


def test_conferir_sistemas_exige_o_intervalo() -> None:
    """Sem valor padrao: quem chama a rodada diz o intervalo (um esquecido daria soma a cada rodada de cada copia)."""
    import inspect

    parametro = inspect.signature(mudou_parou.conferir_sistemas).parameters["intervalo"]
    assert parametro.kind is inspect.Parameter.KEYWORD_ONLY
    assert parametro.default is inspect.Parameter.empty
    assert "make_interval(secs => :meio)" in mudou_parou.SQL_SOMAR_SEM_RESPOSTA
    assert "conferencia_sem_resposta_em = now()" in mudou_parou.SQL_SOMAR_SEM_RESPOSTA


def test_aviso_da_quarentena_por_revisao_que_nao_confere() -> None:
    """Frase propria para a quarentena por revisao gravada fora da receita (no lugar de "mudou 0 ferramentas")."""
    evento = "sistema_em_quarentena_revisao_inconsistente"
    assert AVISOS_ADMIN[evento].campos() == {"sistema"}
    assert aviso_admin(evento, sistema="Fornecedor CRM") == (
        "MCP-CLARO: servidor de terceiros em quarentena (revisao que nao confere)",
        "A revisao do servidor de terceiros Fornecedor CRM guardada no MCP-CLARO nao confere com a lista de"
        " ferramentas dela. Ele entrou em quarentena: nada dele chega as pessoas, o que esperava para sair foi"
        " cancelado e os limites das pessoas com ele foram suspensos. Leia as ferramentas e conclua a revisao do"
        " servidor de novo nas telas do ADMIN; se acontecer outra vez, fale com a equipe do MCP-CLARO.",
    )


def test_docstrings_das_rotinas_dizem_que_sala_fora_e_cofre_nao_contam() -> None:
    """O despachante responde 503 com motivo proprio para a sala de visitas fora e para a chave que o cofre nao abriu
    (G3r): as docstrings nao dizem mais que esses casos chegam como 502 e contam."""
    for modulo in (mudou_parou, rotinas_despachante):
        texto = " ".join((modulo.__doc__ or "").split())
        assert "ainda chegam como 502" not in texto
        assert "ainda responde 502" not in texto
        assert "G3a" not in texto
        assert "sala_fora" in texto
        assert "chave_indisponivel" in texto


# --- F4 (etapa 4f): a metade do intervalo, sem banco e sem relogio --------------------------------------------------


class _Resultado:
    def scalar_one_or_none(self) -> None:
        return None  # nada somado: a rodada para aqui, sem quarentena


class _Conexao:
    def __init__(self, vistos: list[tuple[str, dict[str, Any]]]) -> None:
        self.vistos = vistos

    async def execute(self, sql: Any, params: dict[str, Any] | None = None) -> _Resultado:
        self.vistos.append((str(sql), dict(params or {})))
        return _Resultado()


class _Transacao:
    def __init__(self, vistos: list[tuple[str, dict[str, Any]]]) -> None:
        self.vistos = vistos

    async def __aenter__(self) -> _Conexao:
        return _Conexao(self.vistos)

    async def __aexit__(self, *_: object) -> bool:
        return False


class _EngineQueGrava:
    """Engine falso: cada ``begin()`` da uma conexao que grava o SQL e os parametros de cada ``execute``."""

    def __init__(self) -> None:
        self.vistos: list[tuple[str, dict[str, Any]]] = []

    def begin(self) -> _Transacao:
        return _Transacao(self.vistos)


@pytest.mark.parametrize("intervalo", [900.0, 60.0, 1234.0, 61.0])
async def test_meio_passado_ao_sql_e_a_metade_exata_do_intervalo(intervalo: float) -> None:
    """``_sem_resposta_de_terceiro`` passa ao SQL a metade exata do intervalo. Os testes com banco deixam folga
    contra CI lento e nao veem fatores perto de 0,5 (0,45 ou 1/2,2); este ve, sem banco e sem relogio."""
    engine = _EngineQueGrava()
    resultado = mudou_parou.ResultadoMudouParou()
    sistema_id, revisao_id = uuid.uuid4(), uuid.uuid4()
    await mudou_parou._sem_resposta_de_terceiro(
        cast(AsyncEngine, engine), sistema_id, "x", revisao_id, [], resultado, intervalo=intervalo
    )
    ((sql, params),) = engine.vistos
    assert sql == mudou_parou.SQL_SOMAR_SEM_RESPOSTA
    assert params == {"i": sistema_id, "b": revisao_id, "meio": intervalo / 2}
    assert resultado.sem_resposta == ["x"]
