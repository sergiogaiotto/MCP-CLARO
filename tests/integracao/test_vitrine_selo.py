"""Vitrine com o selo (Fase 4, grupo G2), contra PostgreSQL 18.7 real.

- Toda ferramenta da vitrine traz o sistema com ``selo_vigente`` lido da funcao do banco, na mesma consulta; o
  ``_meta`` da vitrine leva o selo EFETIVO da pessoa (``efetivo`` sobre esse vigente, passado a ``como_tool``):
  chave desligada, nivel 1 para a pessoa real e o vigente para a de teste; ligada pelo construtor, o vigente.
- O selo que cai (retirada) cai na vitrine na hora, tambem para a pessoa de teste.
- As ferramentas do contrato do selo (``selo_alvo``, ``selo_consultar``, ``selo_bloqueio``) nunca aparecem na vitrine
  nem podem ser chamadas por ela (``ferramenta_na_vitrine``, ``ferramenta_da_ordem``), nem quando uma linha com esse
  nome chega ao banco por fora da revisao.
- A entrada e o despachante leem a vitrine com o selo (EXECUTE em ``selo_vigente``).

Quem liga isso na entrada (``entrada/servidor.py``, ``como_tool(selo_efetivo=...)``) e o G4 na etapa 3: aqui se prova
o cadastro e o modelo.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import FerramentaVitrine
from mcp_claro.config.settings import Parametros
from mcp_claro.selo import contrato, vigente
from mcp_claro.selo.vigente import efetivo
from mcp_claro.versao import VERSAO_MCP
from tests import apoio_selos as apoio
from tests.apoio_terceiros import definicao, revisar_ferramenta
from tests.integracao.test_migracao_0006 import DESPACHANTE, ENTRADA, como, dono

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture

META_SELO = "br.com.claro.mcp-claro/selo"
DESLIGADA = Parametros()
LIGADA = Parametros(
    FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=True, FEATURE_PHASE3_THIRD_PARTY=True, FEATURE_PHASE4_BADGES=True
)


@dataclass
class Cena:
    admin_id: uuid.UUID
    sistema: apoio.SistemaDoSelo
    aplicativo_id: uuid.UUID
    modelo_id: uuid.UUID
    real: uuid.UUID
    de_teste: uuid.UUID


async def _ordem(c: AsyncConnection, pessoa: uuid.UUID, aplicativo: uuid.UUID, modelo: uuid.UUID) -> None:
    await repositorio.conceder_ordem(
        c, pessoa_id=pessoa, aplicativo_id=aplicativo, modelo_id=modelo, dias=7, max_dias=30
    )


@pytest.fixture
async def cena(dono: AsyncEngine) -> Cena:
    async with dono.begin() as c:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(c)
        aplicativo = await apoio.novo_aplicativo(c)
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"modelo-selo-{uuid.uuid4().hex[:8]}",
            descricao="Lembretes",
            ferramentas=[(sistema.id, r.nome_original) for r in (*sistema.acoes, *sistema.leituras)],
            validade_dias=30,
            criado_por="admin-teste",
        )
        real = await apoio.nova_pessoa(c)
        de_teste = await apoio.nova_pessoa(c, teste=True)
        for pessoa in (real, de_teste):
            await _ordem(c, pessoa, aplicativo, modelo)
        return Cena(admin, sistema, aplicativo, modelo, real, de_teste)


async def num_rascunho(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            return await passos(c)
        finally:
            await transacao.rollback()


async def _vitrine(c: AsyncConnection, cena: Cena, pessoa: uuid.UUID) -> list[FerramentaVitrine]:
    return await repositorio.vitrine(c, pessoa, cena.aplicativo_id, VERSAO_MCP)


def _meta_de(ferramentas: list[FerramentaVitrine], *, ligada: bool, pessoa_teste: bool) -> dict[str, int]:
    """O selo do ``_meta`` de cada ferramenta, como a entrada deve montar (G4): o efetivo da pessoa."""
    saida = {}
    for f in ferramentas:
        selo = efetivo(f.sistema.selo_vigente, ligada=ligada, pessoa_teste=pessoa_teste)
        tool = f.como_tool(selo_efetivo=selo.nivel)
        types.Tool.model_validate(tool)  # o fio aceita
        saida[f.nome_original] = int(tool["_meta"][META_SELO])
    return saida


async def test_vitrine_traz_o_selo_vigente_lido_da_funcao(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.begin() as c:
        await apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id)
    async with dono.connect() as c:
        selo = await vigente.ler(c, cena.sistema.id)
        ferramentas = await _vitrine(c, cena, cena.real)
    assert {f.nome_original for f in ferramentas} == {"enviar_lembrete", "listar_lembretes"}
    assert {f.sistema.selo_vigente for f in ferramentas} == {selo}
    assert selo.nivel == 3
    assert {f.sistema.selo for f in ferramentas} == {1}  # a coluna antiga, presa em 1


@pytest.mark.parametrize(
    ("parametros", "real", "de_teste"),
    [(DESLIGADA, 1, 3), (LIGADA, 3, 3)],
    ids=["chave_desligada", "chave_ligada_pelo_construtor"],
)
async def test_meta_da_vitrine_leva_o_selo_efetivo_da_pessoa(
    dono: AsyncEngine, cena: Cena, parametros: Parametros, real: int, de_teste: int
) -> None:
    async with dono.begin() as c:
        await apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id)
    async with dono.connect() as c:
        do_real = await _vitrine(c, cena, cena.real)
        do_teste = await _vitrine(c, cena, cena.de_teste)
    ligada = parametros.FEATURE_PHASE4_BADGES
    assert _meta_de(do_real, ligada=ligada, pessoa_teste=False) == {"enviar_lembrete": real, "listar_lembretes": real}
    assert _meta_de(do_teste, ligada=ligada, pessoa_teste=True) == {
        "enviar_lembrete": de_teste,
        "listar_lembretes": de_teste,
    }


async def test_sistema_sem_teste_fica_no_nivel_1_para_todos(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.connect() as c:
        ferramentas = await _vitrine(c, cena, cena.de_teste)
    assert {f.sistema.selo_vigente.motivo for f in ferramentas} == {"nunca_testado"}
    assert set(_meta_de(ferramentas, ligada=True, pessoa_teste=True).values()) == {1}
    with pytest.raises(ValueError, match="nunca passa do selo vigente"):
        ferramentas[0].como_tool(selo_efetivo=2)


async def test_selo_que_cai_cai_na_vitrine_na_hora(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.begin() as c:
        await apoio.gravar_teste(c, cena.sistema.id, nivel=2, admin_id=cena.admin_id)

    async def retirar_e_ler(c: AsyncConnection) -> list[FerramentaVitrine]:
        await c.execute(
            text(
                "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                " SELECT :s, teste_id, 'admin', 'teste da vitrine', 'admin-teste', :a FROM selo_vigente(:s)"
            ),
            {"s": cena.sistema.id, "a": cena.admin_id},
        )
        return await _vitrine(c, cena, cena.de_teste)

    ferramentas = await num_rascunho(dono, retirar_e_ler)
    assert {f.sistema.selo_vigente.motivo for f in ferramentas} == {"retirado"}
    assert set(_meta_de(ferramentas, ligada=True, pessoa_teste=True).values()) == {1}


@pytest.mark.parametrize("papel", [ENTRADA, DESPACHANTE])
async def test_entrada_e_despachante_leem_a_vitrine_com_o_selo(dono: AsyncEngine, cena: Cena, papel: str) -> None:
    async with dono.begin() as c:
        await apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id)
    ordem = await _uma_ordem(dono, cena, cena.de_teste)
    acao = cena.sistema.acoes[0]

    async def ler(c: AsyncConnection) -> tuple[list[FerramentaVitrine], Any, Any, Any]:
        await como(c, papel)
        return (
            await _vitrine(c, cena, cena.de_teste),
            await repositorio.ferramenta_na_vitrine(
                c, cena.de_teste, cena.aplicativo_id, acao.nome_vitrine, VERSAO_MCP
            ),
            await repositorio.ferramenta_da_ordem(
                c,
                pessoa_id=cena.de_teste,
                aplicativo_id=cena.aplicativo_id,
                ordem_id=ordem,
                ferramenta_id=acao.id,
                versao=VERSAO_MCP,
            ),
            await repositorio.sistema_por_id(c, cena.sistema.id, com_selo=True),
        )

    ferramentas, na_vitrine, da_ordem, sistema = await num_rascunho(dono, ler)
    assert {f.sistema.selo_vigente.nivel for f in ferramentas} == {3}
    assert na_vitrine.sistema.selo_vigente.nivel == 3
    assert da_ordem.sistema.selo_vigente.nivel == 3
    assert sistema.selo_vigente.nivel == 3


async def _uma_ordem(engine: AsyncEngine, cena: Cena, pessoa: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as c:
        return uuid.UUID(
            str(
                (
                    await c.execute(
                        text("SELECT id FROM ordens_servico WHERE pessoa_id = :p AND aplicativo_id = :a"),
                        {"p": pessoa, "a": cena.aplicativo_id},
                    )
                ).scalar_one()
            )
        )


# --- As ferramentas do contrato nunca na vitrine ---------------------------------------------------------------


@pytest.mark.parametrize("nome", contrato.FERRAMENTAS_DO_CONTRATO)
async def test_ferramentas_do_contrato_nunca_aparecem_nem_sao_chamadas(
    dono: AsyncEngine, cena: Cena, nome: str
) -> None:
    """O banco recusa revisar esses nomes; aqui uma linha com o nome chega por fora (o dono muda o nome de uma
    revisada ativa), com ordem de servico para ela. A vitrine a deixa de fora, e nada a chama pela vitrine. A mesma
    linha com outro nome aparece (o teste nao passa a toa)."""
    ordem = await _uma_ordem(dono, cena, cena.de_teste)

    async def montar_e_ler(c: AsyncConnection, *, renomear: bool) -> tuple[set[str], Any, Any]:
        r = await revisar_ferramenta(c, cena.sistema.id, definicao(f"quase_{nome}"))
        final = nome if renomear else r.nome_original
        if renomear:
            await c.execute(
                text("UPDATE ferramentas_revisadas SET nome_original = :n WHERE id = :i"), {"n": nome, "i": r.id}
            )
        await c.execute(
            text("INSERT INTO modelos_ordem_ferramentas VALUES (:m, :s, :n)"),
            {"m": cena.modelo_id, "s": cena.sistema.id, "n": final},
        )
        await como(c, ENTRADA)
        vistas = {f.nome_original for f in await _vitrine(c, cena, cena.de_teste)}
        na_vitrine = await repositorio.ferramenta_na_vitrine(
            c, cena.de_teste, cena.aplicativo_id, r.nome_vitrine, VERSAO_MCP
        )
        da_ordem = await repositorio.ferramenta_da_ordem(
            c,
            pessoa_id=cena.de_teste,
            aplicativo_id=cena.aplicativo_id,
            ordem_id=ordem,
            ferramenta_id=r.id,
            versao=VERSAO_MCP,
        )
        return vistas, na_vitrine, da_ordem

    vistas, na_vitrine, da_ordem = await num_rascunho(dono, lambda c: montar_e_ler(c, renomear=True))
    assert nome not in vistas
    assert vistas == {"enviar_lembrete", "listar_lembretes"}
    assert na_vitrine is None
    assert da_ordem is None
    controle, na_vitrine, da_ordem = await num_rascunho(dono, lambda c: montar_e_ler(c, renomear=False))
    assert f"quase_{nome}" in controle
    assert na_vitrine is not None
    assert da_ordem is not None


@pytest.mark.parametrize("nome", contrato.FERRAMENTAS_DO_CONTRATO)
async def test_o_banco_recusa_revisar_o_nome_reservado(dono: AsyncEngine, cena: Cena, nome: str) -> None:
    async def revisar(c: AsyncConnection) -> None:
        await repositorio.revisar_ferramenta(
            c,
            sistema_id=cena.sistema.id,
            definicao_declarada=apoio.definicao_do_contrato(nome),
            nome_vitrine=f"{nome}_{uuid.uuid4().hex[:6]}",
            descricao="Nao entra.",
            classe="le",
            revisado_por="admin-teste",
        )

    with pytest.raises(DBAPIError, match="reservado ao contrato do selo"):
        await num_rascunho(dono, revisar)
