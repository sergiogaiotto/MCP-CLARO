"""Casos da ronda e a classificacao das respostas, contra um gateway falso (``httpx2.MockTransport``).

O gateway falso imita as portas da entrada: cracha (401), versao (400 e -32022) e vitrine
(recusa ``fora_da_vitrine`` com HTTP 400). A versao "quebrada" aceita qualquer cracha.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest

from mcp_claro.regras import programas as regras_programas
from mcp_claro.ronda.casos import (
    FERRAMENTA_INVENTADA,
    MOTIVOS_DO_CADASTRO_DO_PROGRAMA,
    Caso,
    Resposta,
    Resumo,
    Situacao,
    classificar_controle,
    classificar_cracha,
    classificar_outra_pessoa,
    classificar_versao,
    fazer_tentativas,
)
from mcp_claro.ronda.cracha import FalhaCracha
from tests.idp_falso import IdpFalso

EMISSOR = "https://login.teste/realms/mcp-claro"
RECURSO = "https://mcp-claro.teste/mcp"
ALVO = "https://mcp-claro.teste"
MOTIVO = "br.com.claro.mcp-claro/motivo"


def erro(codigo: int, motivo: str | None = None) -> dict[str, Any]:
    e: dict[str, Any] = {"code": codigo, "message": "recusado"}
    if motivo:
        e["data"] = {MOTIVO: motivo}
    return {"jsonrpc": "2.0", "id": 1, "error": e}


def resultado(valor: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": valor}


def test_classificar_cracha() -> None:
    assert classificar_cracha(Resposta(401, {"error": "invalid_token"})) is Situacao.CONFORME
    assert classificar_cracha(Resposta(200, resultado({"tools": []}))) is Situacao.PASSOU
    assert classificar_cracha(Resposta(400, erro(-32602, "fase1_desligada"))) is Situacao.PASSOU
    assert classificar_cracha(Resposta(403, {"error": "insufficient_scope"})) is Situacao.PASSOU
    assert classificar_cracha(Resposta(503, None)) is Situacao.INCONCLUSIVO
    assert classificar_cracha(Resposta(429, None)) is Situacao.INCONCLUSIVO


def test_classificar_versao() -> None:
    assert classificar_versao(Resposta(400, erro(-32022))) is Situacao.CONFORME
    assert classificar_versao(Resposta(200, resultado({"protocolVersion": "2025-11-25"}))) is Situacao.PASSOU
    assert classificar_versao(Resposta(404, erro(-32601))) is Situacao.PASSOU
    assert classificar_versao(Resposta(401, None)) is Situacao.INCONCLUSIVO
    assert classificar_versao(Resposta(502, None)) is Situacao.INCONCLUSIVO


def test_classificar_controle_e_outra_pessoa() -> None:
    assert classificar_controle(Resposta(200, resultado({"tools": []}))) is Situacao.CONFORME
    assert classificar_controle(Resposta(400, erro(-32602, "entrada_fechada"))) is Situacao.CONFORME
    assert classificar_controle(Resposta(400, erro(-32022))) is Situacao.INCONCLUSIVO
    assert classificar_controle(Resposta(401, None)) is Situacao.INCONCLUSIVO
    assert classificar_outra_pessoa(Resposta(400, erro(-32602, "fora_da_vitrine"))) is Situacao.CONFORME
    assert classificar_outra_pessoa(Resposta(400, erro(-32602, "fase1_desligada"))) is Situacao.INCONCLUSIVO
    # Recusa que so vem depois de achar a ferramenta na vitrine: a porta da vitrine deixou passar.
    assert classificar_outra_pessoa(Resposta(400, erro(-32602, "argumento_fora_do_esquema"))) is Situacao.PASSOU
    assert classificar_outra_pessoa(Resposta(200, resultado({"content": []}))) is Situacao.PASSOU
    assert classificar_outra_pessoa(Resposta(401, None)) is Situacao.INCONCLUSIVO


@pytest.mark.parametrize(
    "motivo",
    [
        "programa_sem_chave_propria",  # Fase 2: a ronda e um programa; a ferramenta alheia e de chave por pessoa
        "chave_de_programa",
        "programa_so_le",
        "acoes_desligadas",
        "entrada_so_leitura",
        "bloqueio_sistema",
        "pausa_sistema",
        "catraca",
        "teto_confirmacoes",
        "motivo_que_ainda_nao_existe",
        None,
    ],
)
def test_outra_pessoa_so_e_conforme_com_fora_da_vitrine(motivo: str | None) -> None:
    """Qualquer recusa de depois da vitrine, ou desconhecida, e passagem: a vitrine deixou a ferramenta seguir."""
    assert classificar_outra_pessoa(Resposta(400, erro(-32602, motivo))) is Situacao.PASSOU


@pytest.mark.parametrize(
    "motivo",
    [
        "app_desconhecido",
        "app_pausado",
        "pessoa_desativada",
        "fase1_desligada",
        *sorted(MOTIVOS_DO_CADASTRO_DO_PROGRAMA),
        "entrada_fechada",
        "bloqueio_tudo",
        "pausa_tudo",
        "bloqueio_aplicativo",
        "pausa_pessoa",
        "bloqueio_pessoa_aplicativo",
    ],
)
def test_outra_pessoa_inconclusivo_com_recusa_de_antes_da_vitrine(motivo: str) -> None:
    assert classificar_outra_pessoa(Resposta(400, erro(-32602, motivo))) is Situacao.INCONCLUSIVO


def test_outra_pessoa_com_erro_interno_e_inconclusivo() -> None:
    # O kit responde erro interno com HTTP 200: nao prova nada sobre a vitrine.
    assert classificar_outra_pessoa(Resposta(200, erro(-32603))) is Situacao.INCONCLUSIVO


@pytest.mark.parametrize(
    "motivo", ["programa_pausado", "programa_sem_cadastro", "programa_vencido", "programa_encerrado", "app_pausado"]
)
def test_controle_inconclusivo_quando_a_entrada_recusa_a_propria_ronda(motivo: str) -> None:
    """Ronda com o proprio programa pausado, sem cadastro ou vencido: a rodada e inconclusiva, nunca conforme."""
    assert classificar_controle(Resposta(400, erro(-32602, motivo))) is Situacao.INCONCLUSIVO
    assert classificar_controle(Resposta(400, erro(-32602, "bloqueio_tudo"))) is Situacao.CONFORME
    # O limite por minuto passa sozinho: o cracha passou pelas portas e o cadastro vale.
    assert classificar_controle(Resposta(400, erro(-32602, "programa_limite_minuto"))) is Situacao.CONFORME


def test_motivos_do_cadastro_do_programa_batem_com_as_regras() -> None:
    """Toda recusa de ``regras/programas.py`` vem antes da vitrine, menos as do tipo de chave do sistema."""
    texto = Path(regras_programas.__file__).read_text(encoding="utf-8")
    motivos = set(re.findall(r"ProgramaRecusado\(\s*\"([a-z_]+)\"", texto))
    depois_da_vitrine = {"programa_sem_chave_propria", "chave_de_programa"}
    assert depois_da_vitrine <= motivos
    assert motivos - depois_da_vitrine == MOTIVOS_DO_CADASTRO_DO_PROGRAMA


class GatewayFalso:
    """Imita as portas da entrada. ``quebrado`` aceita qualquer cracha (falha plantada)."""

    def __init__(
        self,
        idp: IdpFalso,
        *,
        quebrado: bool = False,
        versao_quebrada: bool = False,
        recusa_da_chamada: str = "fora_da_vitrine",
        recusa_da_lista: str | None = None,
    ) -> None:
        self.idp = idp
        self.quebrado = quebrado
        self.versao_quebrada = versao_quebrada
        self.recusa_da_chamada = recusa_da_chamada
        self.recusa_da_lista = recusa_da_lista
        self.pedidos = 0

    def __call__(self, pedido: httpx2.Request) -> httpx2.Response:
        self.pedidos += 1
        token = pedido.headers.get("authorization", "")[7:]
        try:
            if self.quebrado:
                jwt.decode(token, options={"verify_signature": False})
            else:
                jwt.decode(token, self.idp.chave.public_key(), algorithms=["RS256"], audience=RECURSO, issuer=EMISSOR)
        except jwt.PyJWTError:
            return httpx2.Response(401, json={"error": "invalid_token"})
        corpo = json.loads(pedido.content)
        versao = pedido.headers.get("mcp-protocol-version")
        if not self.versao_quebrada and versao != "2026-07-28":
            return httpx2.Response(400, json=erro(-32022))
        if self.recusa_da_lista and corpo["method"] in ("tools/list", "tools/call"):
            return httpx2.Response(400, json=erro(-32602, self.recusa_da_lista))
        if corpo["method"] == "tools/list":
            return httpx2.Response(200, json=resultado({"tools": [{"name": "da_ronda"}]}))
        if corpo["method"] == "tools/call":
            return httpx2.Response(400, json=erro(-32602, self.recusa_da_chamada))
        return httpx2.Response(200, json=resultado({"protocolVersion": "2025-11-25"}))


class Fonte:
    def __init__(
        self, idp: IdpFalso, *, outro_aud: str = "https://outro.teste/api", vencido: int | None = -600
    ) -> None:
        self.idp = idp
        self.outro_aud = outro_aud
        self.validade_vencido = vencido
        self.falhar = False

    def _c(self, **kw: Any) -> str:
        return self.idp.cracha(sujeito="service-account-mcp-claro-ronda", client_id="mcp-claro-ronda", **kw)

    async def valido(self) -> str:
        if self.falhar:
            raise FalhaCracha("provedor fora do ar")
        return self._c(audiencia=RECURSO)

    async def outro_condominio(self) -> str | None:
        return self._c(audiencia=self.outro_aud)

    def vencido(self) -> str | None:
        return None if self.validade_vencido is None else self._c(audiencia=RECURSO, validade=self.validade_vencido)


@pytest.fixture
def idp() -> IdpFalso:
    return IdpFalso(emissor=EMISSOR)


async def rodada(gateway: GatewayFalso, fonte: Fonte, **kw: Any) -> dict[str, Any]:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(gateway)) as http:
        tentativas = await fazer_tentativas(ALVO, fonte, http, emissor=EMISSOR, recurso=RECURSO, **kw)
    return {t.caso: t for t in tentativas}


@pytest.mark.anyio
async def test_gateway_certo_recusa_tudo(idp: IdpFalso) -> None:
    casos = await rodada(GatewayFalso(idp), Fonte(idp), ferramenta_alheia="fatura_da_ana")
    assert set(casos) == {str(c) for c in Caso}
    assert {c: t.situacao for c, t in casos.items()} == dict.fromkeys(casos, Situacao.CONFORME)
    assert casos[Caso.CONTROLE].detalhes["vitrine"] == 1
    assert casos[Caso.OUTRA_PESSOA].detalhes["ferramenta"] == "fatura_da_ana"
    resumo = Resumo(list(casos.values()))
    assert resumo.controle_ok
    assert not resumo.falhou


@pytest.mark.anyio
async def test_falha_plantada_aparece_como_passagem(idp: IdpFalso) -> None:
    casos = await rodada(GatewayFalso(idp, quebrado=True), Fonte(idp))
    passaram = {c for c, t in casos.items() if t.situacao is Situacao.PASSOU}
    assert passaram == {Caso.CRACHA_FALSO, Caso.CRACHA_VENCIDO, Caso.OUTRO_CONDOMINIO}
    assert not casos[Caso.CRACHA_FALSO].defendeu
    resumo = Resumo(list(casos.values()))
    assert resumo.motivo_legivel() == "cracha falso, cracha vencido, cracha de outro condominio"


@pytest.mark.anyio
async def test_vitrine_furada_aparece_como_passagem(idp: IdpFalso) -> None:
    # A vitrine achou a ferramenta de outra pessoa; a entrada recusou depois, pelo tipo de chave.
    casos = await rodada(GatewayFalso(idp, recusa_da_chamada="programa_sem_chave_propria"), Fonte(idp))
    assert casos[Caso.OUTRA_PESSOA].situacao is Situacao.PASSOU
    resumo = Resumo(list(casos.values()))
    assert resumo.controle_ok
    assert resumo.falhou
    assert resumo.motivo_legivel() == "ferramenta de outra pessoa"


@pytest.mark.anyio
async def test_ronda_com_o_proprio_programa_pausado_e_inconclusiva(idp: IdpFalso) -> None:
    casos = await rodada(GatewayFalso(idp, recusa_da_lista="programa_pausado"), Fonte(idp))
    assert casos[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert casos[Caso.OUTRA_PESSOA].situacao is Situacao.INCONCLUSIVO
    resumo = Resumo(list(casos.values()))
    assert not resumo.controle_ok
    assert not resumo.falhou


@pytest.mark.anyio
async def test_porta_de_versao_quebrada(idp: IdpFalso) -> None:
    casos = await rodada(GatewayFalso(idp, versao_quebrada=True), Fonte(idp))
    assert casos[Caso.VERSAO_ANTIGA_INITIALIZE].situacao is Situacao.PASSOU
    assert casos[Caso.VERSAO_ANTIGA_CABECALHO].situacao is Situacao.PASSOU
    assert casos[Caso.CONTROLE].situacao is Situacao.CONFORME


@pytest.mark.anyio
async def test_casos_que_nao_provam_nada_sao_pulados(idp: IdpFalso) -> None:
    # Cracha "de outro condominio" que cita o MCP-CLARO e cracha "vencido" que ainda vale: pulados.
    fonte = Fonte(idp, outro_aud=RECURSO, vencido=-10)
    gateway = GatewayFalso(idp, quebrado=True)
    casos = await rodada(gateway, fonte)
    assert casos[Caso.OUTRO_CONDOMINIO].situacao is Situacao.PULADO
    assert casos[Caso.CRACHA_VENCIDO].situacao is Situacao.PULADO
    assert casos[Caso.OUTRO_CONDOMINIO].defendeu
    sem_vencido = await rodada(GatewayFalso(idp), Fonte(idp, vencido=None))
    assert sem_vencido[Caso.CRACHA_VENCIDO].obtido.startswith("pulado")


@pytest.mark.anyio
async def test_sem_cracha_da_ronda_o_controle_e_inconclusivo(idp: IdpFalso) -> None:
    fonte = Fonte(idp)
    fonte.falhar = True
    casos = await rodada(GatewayFalso(idp), fonte)
    assert casos[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert casos[Caso.CRACHA_FALSO].situacao is Situacao.CONFORME  # cracha falso montado sem modelo
    for caso in (Caso.OUTRA_PESSOA, Caso.VERSAO_ANTIGA_INITIALIZE, Caso.VERSAO_ANTIGA_CABECALHO):
        assert casos[caso].situacao is Situacao.PULADO
    assert not Resumo(list(casos.values())).controle_ok


@pytest.mark.anyio
async def test_gateway_mudo_e_inconclusivo(idp: IdpFalso) -> None:
    def mudo(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("sem rede")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(mudo)) as http:
        tentativas = await fazer_tentativas(ALVO, Fonte(idp), http, emissor=EMISSOR, recurso=RECURSO)
    assert {t.situacao for t in tentativas} == {Situacao.INCONCLUSIVO}
    assert all(t.defendeu for t in tentativas)
    assert tentativas[0].obtido == "sem resposta (ConnectError)"


@pytest.mark.anyio
async def test_ferramenta_da_propria_vitrine_nao_e_usada(idp: IdpFalso) -> None:
    casos = await rodada(GatewayFalso(idp), Fonte(idp), ferramenta_alheia="da_ronda")
    assert casos[Caso.OUTRA_PESSOA].detalhes["ferramenta"] == FERRAMENTA_INVENTADA


@pytest.mark.anyio
async def test_detalhes_nao_levam_cracha(idp: IdpFalso) -> None:
    casos = await rodada(GatewayFalso(idp, quebrado=True), Fonte(idp))
    texto = json.dumps([t.para_tabela() for t in casos.values()]) + "".join(t.obtido for t in casos.values())
    assert "eyJ" not in texto


def test_tarefa_da_ronda_para_o_agendador() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from mcp_claro.config.settings import Parametros
    from mcp_claro.ronda import tarefa_da_ronda

    p = Parametros(APP_ENV="teste", PATROL_INTERVAL_SECONDS=120)
    tarefa = tarefa_da_ronda(p, create_async_engine("postgresql+asyncpg://x@localhost/x"), ["admin@exemplo.teste"])
    assert (tarefa.nome, tarefa.intervalo, tarefa.prazo) == ("ronda", 120.0, 600.0)
