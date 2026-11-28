"""Rotas do despachante e sondagem da Fase 4 (G3a; criticas M1, M7 e M11).

- ``/interno/chaves`` aceita so a lista fechada ``contrato.TIPOS_DE_CHAVE_DO_ADMIN``: a chave de teste do selo
  (``teste_selo``) e guardada a parte e nunca sobrescreve a de producao; tipo desconhecido e 400, e nada muda; o dono
  certo vai ao extrato (critica M11);
- ``contrato.ROTA_CONSULTAR``: so ``operacao_id`` no corpo, como texto (texto que o banco nao guarda e 400, pela
  regra de ``_so_texto_valido``; um inteiro de 32 algarismos tambem), 404 sem envio, 409 antes da vez, 409
  ``porta_fechada`` com a porta de saida fechada, 200 com o desfecho, 503 quando a falha e do banco;
- a leitura das ferramentas (``_listar``) marca ``ferramentas_vistas.sumiu_em`` do que o sistema deixou de listar e
  tira a marca quando volta: o selo cai por ``contrato_sumiu`` e volta (critica M1).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from exemplos import selo_nivel2
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.despachante.servico import Despachante
from mcp_claro.selo import contrato
from tests import apoio_selo_g3a as a
from tests import apoio_selos as s

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[tuple[a.Cena, httpx2.AsyncClient, Any]]:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        p = banco_migrado.model_copy(update={"APP_ENV": "teste", "BADGE_LOOKUP_TIMEOUT_SECONDS": 2})
        app = criar_app_despachante(p, cn.desp, cn.cifra)
        transporte = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
            yield cn, http, app


async def postar(http: httpx2.AsyncClient, rota: str, corpo: Any) -> httpx2.Response:
    return await http.post(rota, content=json.dumps(corpo), headers={"content-type": "application/json"})


async def chaves_do_sistema(cn: a.Cena) -> list[Any]:
    async with cn.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT tipo, pessoa_id, aplicativo_id, cifrada FROM chaves_cifradas WHERE sistema_id = :s"
                        " ORDER BY tipo, pessoa_id NULLS FIRST"
                    ),
                    {"s": cn.sistema.id},
                )
            ).all()
        )


# --- /interno/chaves (M11) ---------------------------------------------------------------------------------------


async def test_chave_de_teste_e_guardada_a_parte_e_nao_troca_a_de_producao(cena: Any) -> None:
    cn, http, _ = cena
    sistema = str(cn.sistema.id)
    assert (await postar(http, "/interno/chaves", {"sistema_id": sistema, "segredo": "k-producao"})).status_code == 200
    r = await postar(
        http, "/interno/chaves", {"sistema_id": sistema, "segredo": "k-teste", "tipo": contrato.TIPO_CHAVE_TESTE}
    )
    assert (r.status_code, r.json()) == (200, {"guardada": True})
    async with cn.desp.connect() as c:
        assert await chaves.abrir(c, cn.cifra, sistema_id=cn.sistema.id) == "k-producao"
        assert await chaves.abrir(c, cn.cifra, sistema_id=cn.sistema.id, tipo=contrato.TIPO_CHAVE_TESTE) == "k-teste"
    frases = [x.frase for x in await a.extrato(cn, sistema=True) if x.tipo == "chave_guardada"]
    assert frases[-2:] == [
        "Chave compartilhada guardada cifrada no cofre por admin.",
        "Chave de teste do selo guardada cifrada no cofre por admin.",
    ]


@pytest.mark.parametrize("tipo", ["renovacao", "programa", "Acesso", "teste", 1, ["acesso"], {"t": 1}])
async def test_tipo_fora_da_lista_e_400_e_nada_muda(cena: Any, tipo: Any) -> None:
    cn, http, _ = cena
    antes = await chaves_do_sistema(cn)
    r = await postar(http, "/interno/chaves", {"sistema_id": str(cn.sistema.id), "segredo": "k", "tipo": tipo})
    assert r.status_code == 400
    assert await chaves_do_sistema(cn) == antes


@pytest.mark.parametrize("tipo", ["declaracoes", contrato.TIPO_CHAVE_TESTE])
async def test_chave_do_sistema_nao_e_de_programa(cena: Any, tipo: str) -> None:
    cn, http, _ = cena
    antes = await chaves_do_sistema(cn)
    corpo = {"sistema_id": str(cn.sistema.id), "segredo": "k", "tipo": tipo, "aplicativo_id": str(cn.aplicativo_id)}
    assert (await postar(http, "/interno/chaves", corpo)).status_code == 400
    assert await chaves_do_sistema(cn) == antes


async def test_dono_certo_da_chave_das_declaracoes(cena: Any) -> None:
    cn, http, _ = cena
    corpo = {"sistema_id": str(cn.sistema.id), "segredo": "k", "tipo": "declaracoes", "quem": "Carla"}
    assert (await postar(http, "/interno/chaves", corpo)).status_code == 200
    [frase] = [x.frase for x in await a.extrato(cn, sistema=True) if x.tipo == "chave_guardada"]
    assert frase == "Chave de leitura das declaracoes guardada cifrada no cofre por Carla."


# --- Rota da consulta (contrato.ROTA_CONSULTAR) -------------------------------------------------------------------


async def test_rota_da_consulta(cena: Any) -> None:
    cn, http, _ = cena
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
        await s.enviar(c, op.id)
    rota = contrato.ROTA_CONSULTAR
    r = await postar(http, rota, {"operacao_id": str(uuid.uuid4())})
    assert (r.status_code, r.json()) == (404, {"motivo": "sem_envio"})
    r = await postar(http, rota, {"operacao_id": str(op.id)})
    assert (r.status_code, r.json()) == (409, {"motivo": "nada_a_perguntar"})
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
    await a.voltar_no_tempo(cn, [op.id])
    r = await postar(http, rota, {"operacao_id": str(op.id)})
    assert (r.status_code, r.json()) == (200, {"desfecho": "respondeu", "estado": "nao_recebida"})
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 1


@pytest.mark.parametrize(
    "corpo",
    [
        {},
        {"operacao_id": "nao-e-uuid"},
        {"operacao_id": 1},
        {"operacao_id": 12345678901234567890123456789012},  # N7: um inteiro de 32 algarismos seria lido como uuid
        {"operacao_id": None},
        {"operacao_id": "UUID", "ultima": True},
        {"operacao_id": "\x00"},
        {"operacao_id": "\ud800"},
        ["lista"],
    ],
)
async def test_rota_da_consulta_so_aceita_o_numero_da_operacao(cena: Any, corpo: Any) -> None:
    cn, http, _ = cena
    if isinstance(corpo, dict) and corpo.get("operacao_id") == "UUID":
        corpo = corpo | {"operacao_id": str(uuid.uuid4())}
    r = await postar(http, contrato.ROTA_CONSULTAR, corpo)
    assert (r.status_code, r.json()["motivo"]) == (400, "pedido_invalido")
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0


async def test_rota_da_consulta_com_a_porta_fechada_e_409_porta_fechada(cena: Any) -> None:
    """Decisao 2 do coordenador (retoque da etapa 3): era a vez, mas a porta de saida esta fechada: 409
    ``{"motivo": "porta_fechada"}``, nada perguntado e nada gravado; com a porta aberta, 200 na hora."""
    cn, http, _ = cena
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(c, cn.sistema.acoes[0], pessoa_id=cn.pessoa_id)
        await s.enviar(c, op.id)
        await c.execute(
            text(
                "UPDATE estado_saida SET aberta = false, motivo = 'teste', mudou_por = 'teste-g3ar', mudou_em = now()"
                " WHERE id = 1"
            )
        )
    await a.voltar_no_tempo(cn, [op.id])
    r = await postar(http, contrato.ROTA_CONSULTAR, {"operacao_id": str(op.id)})
    assert (r.status_code, r.json()) == (409, {"motivo": "porta_fechada"})
    assert cn.chamadas(contrato.FERRAMENTA_CONSULTAR) == 0
    assert await a.informacoes(cn, op.id) == []
    async with cn.dono.begin() as c:
        await c.execute(text("UPDATE estado_saida SET aberta = true, motivo = NULL WHERE id = 1"))
    r = await postar(http, contrato.ROTA_CONSULTAR, {"operacao_id": str(op.id)})
    assert (r.status_code, r.json()) == (200, {"desfecho": "respondeu", "estado": "nao_recebida"})


async def test_rota_da_consulta_com_o_banco_fora_e_503(cena: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, http, app = cena

    async def sem_banco(*_: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("o banco caiu"))

    monkeypatch.setattr(app.state.perguntador, "consultar", sem_banco)
    r = await postar(http, contrato.ROTA_CONSULTAR, {"operacao_id": str(uuid.uuid4())})
    assert (r.status_code, r.json()["motivo"]) == (503, "conferencia_nao_feita")


async def test_o_app_monta_o_perguntador_com_os_parametros(cena: Any) -> None:
    _, _, app = cena
    perguntador = app.state.perguntador
    assert (
        perguntador.maximo,
        perguntador.prazo,
        perguntador.falhas_para_retirar,
        perguntador.tentativas_do_aviso,
    ) == (
        8,
        2.0,
        3,
        3,
    )
    assert app.state.porta.selos_ligados is False
    assert app.state.despachante.selos_ligados is False


# --- Sondagem: o que o sistema deixou de listar (M1) --------------------------------------------------------------


async def test_ferramenta_do_contrato_que_some_derruba_o_selo_e_volta(cena: Any) -> None:
    cn, _, _ = cena
    despachante = Despachante(cn.desp, cn.cifra, ambiente="teste", liberar_locais=True)
    await despachante.ler_declaracoes(cn.sistema.id, a.CHAVE_DA_PESSOA)
    assert (await a.selo_vigente(cn)).motivo == "vigente"
    cn.motor.tirar = frozenset({contrato.FERRAMENTA_CONSULTAR})
    await despachante.ler_declaracoes(cn.sistema.id, a.CHAVE_DA_PESSOA)
    async with cn.dono.connect() as c:
        vistas = {
            str(x.nome): x.sumiu_em
            for x in await c.execute(
                text("SELECT nome, sumiu_em FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": cn.sistema.id}
            )
        }
    assert vistas[contrato.FERRAMENTA_CONSULTAR] is not None
    assert vistas[contrato.FERRAMENTA_ALVO] is None
    assert vistas[a.ACAO] is None
    assert (await a.selo_vigente(cn)).motivo == "contrato_sumiu"
    cn.motor.tirar = frozenset()
    await despachante.ler_declaracoes(cn.sistema.id, a.CHAVE_DA_PESSOA)
    assert (await a.selo_vigente(cn)).motivo == "vigente"
