"""Despachante, falhas que sao do MCP-CLARO e nao do servidor (G3r, etapa 4r; critica A5).

- ``/interno/declaracoes`` com a sala de visitas fora ou com o cofre que nao abre a chave das declaracoes responde
  503 com motivo proprio (``sala_fora``, ``chave_indisponivel``): as rotinas de verdade, pela porta interna, nao
  contam essas rodadas como "sem resposta" do servidor, e duas delas seguidas nao o poem em quarentena. O 502
  ``declaracoes_indisponiveis`` fica so para "o servidor nao entregou".
- O endereco de terceiro que nao e publico (``sala_endereco_recusado``) e problema do servidor: continua 502 e conta.
- A busca da origem do sistema que falha (``CifraPorOrigem``, conexao propria) vira a recusa ``chave_indisponivel``
  na leitura e 503 ``chave_indisponivel`` nas declaracoes, nunca erro interno.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from exemplos import mal_comportado
from mcp_claro.cofre import CofreIndisponivel
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.despachante.cifra_origem import CifraPorOrigem
from mcp_claro.despachante.servico import Despachante, LeituraRecusada
from mcp_claro.ligacao import sala_contrato
from mcp_claro.ligacao.ponto import ClienteLocal
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita, FonteDeclaracoes, conferir_sistemas
from mcp_claro.saida.estados import Estado
from mcp_claro.versao import PREFIXO_META
from tests import apoio_g3a as g
from tests import apoio_terceiros as t3
from tests.apoio import porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_entrada_terceiros_apoio import Mundo
from tests.integracao.test_saida_terceiros import (  # noqa: F401 - mundo e a fixture
    Ligacoes,
    ate_o_fim,
    guardar_chave,
    liberada,
    linhas_da_sala,
    mundo,
    porta,
    protocolo,
    sala_falsa,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

ADMIN = "admin-g3r@exemplo.teste"
CONSULTAR = {
    "name": "consultar_pedido",
    "description": "Consulta a situacao de um pedido.",
    "inputSchema": {"type": "object", "properties": {"pedido": {"type": "string", "maxLength": 40}}},
}


@dataclass
class FonteSo:
    """So o sistema deste teste; os outros do banco da sessao ficam como conferencia nao feita (sem efeito)."""

    fonte: FonteDeclaracoes
    sistema_id: uuid.UUID
    chamadas: int = 0

    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        if sistema_id != self.sistema_id:
            raise ConferenciaNaoFeita("fora deste teste")
        self.chamadas += 1
        return await self.fonte.declaracoes(sistema_id)


@dataclass
class Cena:
    p: Parametros
    dono: AsyncEngine
    desp_db: AsyncEngine
    rotinas_db: AsyncEngine
    cifra: CifraFalsa

    async def terceiro_revisado(self, *, chave_das_declaracoes: bool, **kw: Any) -> uuid.UUID:
        """Terceiro ativo, com uma ferramenta revisada e a revisao do servidor vigente (o mudou-parou o confere)."""
        async with self.dono.begin() as c:
            sistema_id = await t3.cadastrar_terceiro(c, nome=f"g3r-{uuid.uuid4().hex[:8]}", **kw)
            await t3.revisar_ferramenta(c, sistema_id, t3.definicao("ler_caixa", "Le a caixa de mensagens."))
            await t3.gravar_revisao_do_servidor(c, sistema_id)
        if chave_das_declaracoes:
            async with self.dono.begin() as c:
                await chaves.guardar(c, self.cifra, sistema_id=sistema_id, segredo="k", tipo="declaracoes")
        return sistema_id

    async def situacao(self, sistema_id: uuid.UUID) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text("SELECT situacao, conferencias_sem_resposta FROM sistemas WHERE id = :s"), {"s": sistema_id}
                )
            ).one()


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    porta = porta_livre()
    p = banco_migrado.model_copy(update={"APP_ENV": "teste", "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta}"})
    dono = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    rotinas_db = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    try:
        await g.abrir_a_saida(dono)
        yield Cena(p, dono, desp_db, rotinas_db, CifraFalsa())
    finally:
        await dono.dispose()
        await desp_db.dispose()
        await rotinas_db.dispose()


class _CifraTrancada(CifraFalsa):
    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        raise CofreIndisponivel("cofre trancado")


async def _rodadas(cena: Cena, sistema_id: uuid.UUID, cifra: Any, **kw: Any) -> tuple[httpx2.Response, int, list[str]]:
    """O despachante de verdade numa porta, e duas rodadas das rotinas de verdade pela porta interna. Devolve a
    resposta de um pedido direto a ``/interno/declaracoes``, quantas vezes as rotinas pediram e as quarentenas."""
    app = criar_app_despachante(cena.p, cena.desp_db, cifra, **kw)
    cliente = ClienteDespachante(cena.p)
    porta = int(cena.p.DISPATCHER_INTERNAL_URL.rsplit(":", 1)[1])
    quarentenas: list[str] = []
    try:
        async with servidor(app, porta) as url:
            async with httpx2.AsyncClient() as http:
                direto = await http.post(f"{url}/interno/declaracoes", json={"sistema_id": str(sistema_id)})
            fonte = FonteSo(cliente, sistema_id)
            for _ in range(2):
                resultado = await conferir_sistemas(cena.rotinas_db, fonte, [ADMIN], intervalo=0.0)
                quarentenas.extend(resultado.quarentenas)
    finally:
        await cliente.fechar()
    return direto, fonte.chamadas, quarentenas


async def _duas_rodadas(cena: Cena, sistema_id: uuid.UUID, cifra: Any, **kw: Any) -> tuple[httpx2.Response, int]:
    """Duas rodadas que nao podem por o servidor em quarentena (a falha e do MCP-CLARO)."""
    direto, chamadas, quarentenas = await _rodadas(cena, sistema_id, cifra, **kw)
    assert quarentenas == []
    return direto, chamadas


async def test_sala_fora_por_duas_rodadas_nao_conta_como_sem_resposta_do_servidor(cena: Cena) -> None:
    sistema_id = await cena.terceiro_revisado(chave_das_declaracoes=False)
    # Sem VISITS_INTERNAL_URL e sem o cliente local injetado: o despachante nunca fala com o terceiro (sem sala).
    direto, chamadas = await _duas_rodadas(cena, sistema_id, CifraFalsa())
    assert direto.status_code == 503
    assert direto.json()["motivo"] == "sala_fora"
    assert chamadas == 2
    situacao = await cena.situacao(sistema_id)
    assert (situacao.situacao, situacao.conferencias_sem_resposta) == ("ativo", 0)
    assert set(await g.tipos_no_extrato(cena.dono, sistema_id)) == {"sala_fora"}


async def test_cofre_trancado_por_duas_rodadas_nao_conta_como_sem_resposta_do_servidor(cena: Cena) -> None:
    sistema_id = await cena.terceiro_revisado(chave_das_declaracoes=True)
    direto, chamadas = await _duas_rodadas(cena, sistema_id, _CifraTrancada(), local_para_terceiro=g.LOCAL_DE_TESTE)
    assert direto.status_code == 503
    assert direto.json()["motivo"] == "chave_indisponivel"
    assert chamadas == 2
    situacao = await cena.situacao(sistema_id)
    assert (situacao.situacao, situacao.conferencias_sem_resposta) == ("ativo", 0)


async def test_servidor_que_nao_entrega_continua_502(cena: Cena) -> None:
    """O contraponto: o servidor de verdade fora do ar (porta fechada) e falha do servidor, 502, e conta."""
    sistema_id = await cena.terceiro_revisado(chave_das_declaracoes=True, endereco="http://127.0.0.1:1/mcp")
    app = criar_app_despachante(cena.p, cena.desp_db, cena.cifra, local_para_terceiro=g.LOCAL_DE_TESTE)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://d") as http:
        r = await http.post("/interno/declaracoes", json={"sistema_id": str(sistema_id)})
    assert (r.status_code, r.json()["motivo"]) == (502, "declaracoes_indisponiveis")


async def test_endereco_recusado_do_terceiro_e_do_servidor_continua_502_e_conta(cena: Cena) -> None:
    """Outro contraponto: o endereco do terceiro que nao e publico (aqui, local sem a liberacao dos testes) e
    problema do servidor cadastrado, nao do MCP-CLARO. Nada sai; vai ao extrato como ``sala_endereco_recusado``, a
    porta responde 502 e as rotinas contam: duas rodadas sem resposta o poem em quarentena."""
    sistema_id = await cena.terceiro_revisado(chave_das_declaracoes=True, endereco="http://127.0.0.1:1/mcp")
    estrito = ClienteLocal(terceiro=True, ambiente="teste")  # sem liberar_locais
    direto, chamadas, quarentenas = await _rodadas(cena, sistema_id, cena.cifra, local_para_terceiro=estrito)
    assert (direto.status_code, direto.json()["motivo"]) == (502, "declaracoes_indisponiveis")
    assert chamadas == 2
    assert len(quarentenas) == 1  # os nomes dos que entraram, nesta rodada
    assert (await cena.situacao(sistema_id)).situacao == "quarentena"
    tipos = await g.tipos_no_extrato(cena.dono, sistema_id)
    assert tipos.count("sala_endereco_recusado") == 3  # o pedido direto e as duas rodadas
    assert "sala_fora" not in tipos


ERROS_DA_BUSCA = pytest.mark.parametrize(
    "erro",
    [OSError("sem conexao nova: too many connections"), TimeoutError(), OperationalError("SELECT", {}, OSError())],
    ids=["oserror", "prazo", "banco"],
)


@ERROS_DA_BUSCA
async def test_busca_da_origem_que_falha_nas_declaracoes_e_chave_indisponivel_e_nao_conta(
    cena: Cena, erro: Exception
) -> None:
    """A cifra que nao acha a origem do sistema (a busca falha por rede, prazo ou banco) na chave das declaracoes:
    503 ``chave_indisponivel``, nunca 500, e as rotinas nao contam a rodada contra o servidor."""
    sistema_id = await cena.terceiro_revisado(chave_das_declaracoes=True)

    async def falha(_: uuid.UUID) -> str | None:
        raise erro

    cifra = CifraPorOrigem(propria=cena.cifra, terceiros=cena.cifra, origem_de=falha)
    direto, chamadas = await _duas_rodadas(cena, sistema_id, cifra, local_para_terceiro=g.LOCAL_DE_TESTE)
    assert (direto.status_code, direto.json()["motivo"]) == (503, "chave_indisponivel")
    assert chamadas == 2
    situacao = await cena.situacao(sistema_id)
    assert (situacao.situacao, situacao.conferencias_sem_resposta) == ("ativo", 0)


@ERROS_DA_BUSCA
async def test_busca_da_origem_que_falha_vira_chave_indisponivel_na_leitura(cena: Cena, erro: Exception) -> None:
    async with servidor(mal_comportado.criar()) as base:
        eco = CifraFalsa()
        arr = await g.preparar(cena.dono, eco, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="proprio")

        async def falha(_: uuid.UUID) -> str | None:
            raise erro

        cifra = CifraPorOrigem(propria=eco, terceiros=eco, origem_de=falha)
        desp = Despachante(cena.desp_db, cifra, ambiente="teste", prazo_leitura=5)
        with pytest.raises(LeituraRecusada) as recusa:
            await desp.ler(arr.pedido({"pedido": "1"}))
        assert recusa.value.motivo == "chave_indisponivel"
        # Pela porta interna, a mesma recusa (403 com o motivo), nunca 500; e nada foi guardado: a proxima busca vai
        # ao banco de novo.
        app = criar_app_despachante(cena.p, cena.desp_db, cifra)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://d") as http:
            corpo = {
                "pessoa_id": str(arr.pessoa_id),
                "aplicativo_id": str(arr.aplicativo_id),
                "ordem_id": str(arr.ordem_id),
                "ferramenta_id": str(arr.ferramenta_id),
                "argumentos": {"pedido": "1"},
            }
            r = await http.post("/interno/ler", json=corpo)
    assert (r.status_code, r.json()["motivo"]) == (403, "chave_indisponivel")


# --- F4 (etapa 4f): nomes que so o despachante da, vindos da sala ------------------------------------------------


def _sala_tomada(nome: str) -> Starlette:
    """Uma sala (um filho tomado pelo terceiro) que responde 200 com um nome que so o despachante da: na listagem e
    na chamada, ``nao_enviado`` com ``nome``; na sondagem, ``inalcancavel`` com o motivo da sala fora."""
    corpos = {
        sala_contrato.ROTA_LISTAR: {"contrato": 1, "tipo": "nao_enviado", "excecao": nome},
        sala_contrato.ROTA_CHAMAR: {"contrato": 1, "tipo": "nao_enviado", "excecao": nome},
        sala_contrato.ROTA_SONDAR: {
            "contrato": 1,
            "desfecho": "inalcancavel",
            "motivo": sala_contrato.MOTIVO_SALA_FORA,
            "excecao": nome,
        },
    }

    async def rota(pedido: Request) -> Response:
        await pedido.body()
        return Response(json.dumps(corpos[pedido.url.path]).encode(), media_type="application/json")

    return Starlette(routes=[Route(r, rota, methods=["POST"]) for r in corpos])


NOMES_DO_DESPACHANTE = pytest.mark.parametrize("nome", [sala_contrato.SALA_FORA, sala_contrato.SALA_RESPOSTA_INVALIDA])


@NOMES_DO_DESPACHANTE
async def test_sala_que_diz_sala_fora_na_listagem_e_502_e_as_rotinas_contam(cena: Cena, nome: str) -> None:
    """A prova do verificador (test_v3a): a sala responde 200 com ``SalaFora`` no corpo da listagem. A sala respondeu,
    entao nao esta fora: 502 ``declaracoes_indisponiveis``, e duas rodadas das rotinas de verdade, pela porta interna,
    poem o servidor em quarentena (critica A5: quem falha so no ``tools/list`` nao escapa)."""
    sistema_id = await cena.terceiro_revisado(chave_das_declaracoes=True)
    porta_sala = porta_livre()
    p = cena.p.model_copy(update={"VISITS_INTERNAL_URL": f"http://127.0.0.1:{porta_sala}"})
    app = criar_app_despachante(p, cena.desp_db, cena.cifra)
    cliente = ClienteDespachante(p)
    porta = int(p.DISPATCHER_INTERNAL_URL.rsplit(":", 1)[1])
    quarentenas: list[str] = []
    try:
        async with servidor(_sala_tomada(nome), porta_sala), servidor(app, porta) as url:
            async with httpx2.AsyncClient() as http:
                direto = await http.post(f"{url}/interno/declaracoes", json={"sistema_id": str(sistema_id)})
            fonte = FonteSo(cliente, sistema_id)
            for _ in range(2):
                resultado = await conferir_sistemas(cena.rotinas_db, fonte, [ADMIN], intervalo=0.0)
                quarentenas.extend(resultado.quarentenas)
    finally:
        await cliente.fechar()
    assert (direto.status_code, direto.json()["motivo"]) == (502, "declaracoes_indisponiveis")
    assert fonte.chamadas == 2
    assert len(quarentenas) == 1
    assert (await cena.situacao(sistema_id)).situacao == "quarentena"
    assert not [t for t in await g.tipos_no_extrato(cena.dono, sistema_id) if t.startswith("sala_")]


@NOMES_DO_DESPACHANTE
async def test_sala_que_diz_sala_fora_na_leitura_e_na_sondagem_nao_vira_nada_foi_enviado(cena: Cena, nome: str) -> None:
    async with servidor(_sala_tomada(nome)) as sala:
        arr = await g.preparar(
            cena.dono, cena.cifra, endereco="https://t.exemplo/mcp", definicao=CONSULTAR, origem="terceiro"
        )
        desp = g.despachante(cena.desp_db, cena.cifra, sala=ClienteSala(sala, tls=None), local=False)
        resultado = await desp.ler(arr.pedido({"pedido": "1"}))
        sondagem = await desp.sondar_sistema(arr.sistema_id, None)
    assert resultado["_meta"][PREFIXO_META + "motivo"] == "falha_duvida"
    assert "nada foi enviado" not in resultado["content"][0]["text"]
    assert (sondagem.desfecho, sondagem.motivo, sondagem.excecao) == (
        "inalcancavel",
        sala_contrato.MOTIVO_SALA_INVALIDA,
        sala_contrato.SALA_RESPOSTA_INVALIDA,
    )
    assert not [t for t in await g.tipos_no_extrato(cena.dono, arr.sistema_id) if t.startswith("sala_")]


@NOMES_DO_DESPACHANTE
async def test_sala_que_diz_sala_fora_na_acao_e_nao_confirmada_e_nao_nao_saiu(
    mundo: Mundo,  # noqa: F811 - a fixture importada
    nome: str,
) -> None:
    """Na acao, a reserva e "nao confirmada": pode ter saido, e o pedido igual devolve a mesma operacao."""
    m = mundo
    await guardar_chave(m, m.terceiro_id, "ana", "chave-ana")
    op = await liberada(m, "ana", "t_enviar", {"texto": f"sala tomada {nome}"})
    corpo = json.dumps({"contrato": 1, "tipo": "nao_enviado", "excecao": nome}).encode()
    async with sala_falsa(200, corpo) as sala:
        fim = await ate_o_fim(m, porta(m, Ligacoes({m.terceiro_id: sala}), terceiros_ligados=True), op)
    assert (fim.estado, fim.desfecho) == (Estado.NAO_CONFIRMADA, "a resposta do sistema nao chegou inteira")
    assert await linhas_da_sala(m, op.protocolo) == []
    r = await m.chamar("ana", "t_enviar", {"texto": f"sala tomada {nome}"})
    assert protocolo(r) == op.protocolo
