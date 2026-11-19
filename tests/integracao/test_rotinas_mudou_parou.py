"""Mudou-parou de ponta a ponta: sistema de exemplo mal-comportado, despachante real e rotinas.

O exemplo ``exemplos/mal_comportado.py`` em modo ``muda_definicao`` passa a pedir mais
(campo novo) a cada listagem; a ferramenta revisada fica suspensa, sai da vitrine, vai ao
extrato e o ADMIN recebe aviso. Em ``muda_so_texto`` (sistema proprio) so se registra.

As rotinas pedem as declaracoes ao despachante sem chave (elas nunca guardam chaves). Por
isso o exemplo aqui aceita listar sem chave (``EXEMPLO_CHAVES`` com a chave vazia); ver o
relatorio da Fase 1 sobre qual chave o despachante deve usar nesse pedido.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import mal_comportado
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.mudou_parou import MOTIVO_SUSPENSAO, conferir_sistemas_proprios
from mcp_claro.versao import VERSAO_MCP
from tests.apoio import porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.integracao import test_rotinas_apoio as apoio
from tests.integracao.test_rotinas_apoio import cadastro_minimo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

# Fixtures compartilhadas (definidas em test_rotinas_apoio).
engine_dono = apoio.engine_dono
engine_rotinas = apoio.engine_rotinas
parametros_rotinas = apoio.parametros_rotinas

ADMIN = "admin@exemplo.teste"


@dataclass
class Montagem:
    sistema_url: str
    sistema_id: uuid.UUID
    ferramenta_id: uuid.UUID
    nome_vitrine: str
    despachante: ClienteDespachante


@contextlib.asynccontextmanager
async def montar(
    banco_migrado: Parametros, parametros_rotinas: Parametros, engine_dono: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Montagem]:
    monkeypatch.setenv("EXEMPLO_CHAVES", json.dumps({"": "*", "chave-ana": "ana"}))
    sistema_app = mal_comportado.criar()
    porta_desp = porta_livre()
    p_desp = Parametros(
        **(
            banco_migrado.model_dump()
            | {"APP_ENV": "teste", "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}"}
        )
    )
    p_rot = Parametros(
        **(parametros_rotinas.model_dump() | {"DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}"})
    )
    async with (
        servidor(sistema_app) as url_sistema,
        servidor(criar_app_despachante(p_desp, engine_dono, CifraFalsa()), porta_desp),
    ):
        sufixo = uuid.uuid4().hex[:8]
        async with engine_dono.begin() as c:
            sistema_id = await repositorio.cadastrar_sistema(
                c, nome=f"mal-{sufixo}", endereco=f"{url_sistema}/mcp", tipo_chave="por_pessoa"
            )
        cliente = ClienteDespachante(p_rot)
        async with httpx2.AsyncClient() as http:
            r = await http.post(f"http://127.0.0.1:{porta_desp}/interno/sondar", json={"sistema_id": str(sistema_id)})
            assert r.json()["aceito"], r.json()
        (declarada,) = await cliente.declaracoes(sistema_id)
        nome_vitrine = f"pedido_{sufixo}"
        async with engine_dono.begin() as c:
            ferramenta_id = await repositorio.revisar_ferramenta(
                c,
                sistema_id=sistema_id,
                definicao_declarada=declarada,
                nome_vitrine=nome_vitrine,
                descricao="Mostra a situacao de um pedido seu.",
                classe="le",
                revisado_por="admin-teste",
            )
        try:
            yield Montagem(url_sistema, sistema_id, ferramenta_id, nome_vitrine, cliente)
        finally:
            await cliente.fechar()


async def _modo(url: str, modo: str) -> None:
    async with httpx2.AsyncClient() as http:
        (await http.post(f"{url}/modo", json={"modo": modo})).raise_for_status()


async def _ferramenta(engine: AsyncEngine, ferramenta_id: uuid.UUID) -> tuple[str, str | None]:
    async with engine.connect() as c:
        situacao, motivo = (
            await c.execute(
                text("SELECT situacao, motivo FROM ferramentas_revisadas WHERE id = :i"), {"i": ferramenta_id}
            )
        ).one()
    return str(situacao), motivo


async def test_ferramenta_que_passa_a_pedir_mais_e_suspensa(
    banco_migrado: Parametros,
    parametros_rotinas: Parametros,
    engine_dono: AsyncEngine,
    engine_rotinas: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with montar(banco_migrado, parametros_rotinas, engine_dono, monkeypatch) as m:
        # Uma pessoa com ordem de servico ve a ferramenta na vitrine.
        cad = await cadastro_minimo(engine_dono)
        async with engine_dono.begin() as c:
            modelo = await repositorio.criar_modelo_ordem(
                c,
                nome=f"modelo-mp-{cad.sufixo}",
                descricao="Ordem de teste",
                ferramentas=[(m.sistema_id, "consultar_pedido")],
                validade_dias=30,
                criado_por="t",
            )
            await repositorio.conceder_ordem(
                c, pessoa_id=cad.pessoa_id, aplicativo_id=cad.aplicativo_id, modelo_id=modelo, dias=7, max_dias=30
            )
        async with engine_dono.connect() as c:
            antes = await repositorio.vitrine(c, cad.pessoa_id, cad.aplicativo_id, VERSAO_MCP)
        assert [f.nome_vitrine for f in antes] == [m.nome_vitrine]

        # Sem mudanca: nada acontece.
        resultado = await conferir_sistemas_proprios(engine_rotinas, m.despachante, [ADMIN])
        assert m.nome_vitrine not in resultado.suspensas
        assert await _ferramenta(engine_rotinas, m.ferramenta_id) == ("ativa", None)

        # O sistema passa a pedir mais: mudou, parou.
        await _modo(m.sistema_url, "muda_definicao")
        resultado = await conferir_sistemas_proprios(engine_rotinas, m.despachante, [ADMIN])
        assert resultado.suspensas == [m.nome_vitrine]
        situacao, motivo = await _ferramenta(engine_rotinas, m.ferramenta_id)
        assert situacao == "suspensa"
        assert motivo == MOTIVO_SUSPENSAO
        assert motivo.startswith("mudou, parou")

        async with engine_dono.connect() as c:
            depois = await repositorio.vitrine(c, cad.pessoa_id, cad.aplicativo_id, VERSAO_MCP)
            assert depois == []  # perdeu a autorizacao ate nova revisao
            extrato = (
                await c.execute(
                    text(
                        "SELECT frase, sistema_id, ferramenta, dados, papel FROM extrato"
                        " WHERE tipo = 'ferramenta_suspensa' AND dados ->> 'ferramenta_id' = :i"
                    ),
                    {"i": str(m.ferramenta_id)},
                )
            ).one()
            aviso = (
                await c.execute(
                    text(
                        "SELECT destino, evento, assunto, corpo FROM avisos WHERE evento = 'ferramenta_suspensa'"
                        " AND corpo ->> 'ferramenta_id' = :i"
                    ),
                    {"i": str(m.ferramenta_id)},
                )
            ).one()
            vista = (
                await c.execute(
                    text("SELECT definicao FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": m.sistema_id}
                )
            ).scalar_one()
        assert "mudou, parou" in extrato.frase
        assert extrato.sistema_id == m.sistema_id
        assert extrato.ferramenta == m.nome_vitrine
        assert extrato.papel == "rotinas"
        assert extrato.dados["impressao_nova"] != extrato.dados["impressao_revisada"]
        assert aviso.destino == ADMIN
        assert "nova revisao" in aviso.corpo["texto"]
        assert "extra_0" in vista["inputSchema"]["properties"]  # o despachante guardou o que viu

        # Rodar de novo nao repete extrato nem aviso: a ferramenta ja esta suspensa.
        await conferir_sistemas_proprios(engine_rotinas, m.despachante, [ADMIN])
        async with engine_dono.connect() as c:
            n_extrato = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM extrato WHERE tipo = 'ferramenta_suspensa'"
                        " AND dados ->> 'ferramenta_id' = :i"
                    ),
                    {"i": str(m.ferramenta_id)},
                )
            ).scalar_one()
        assert n_extrato == 1


async def test_mudanca_so_de_texto_em_sistema_proprio_so_registra(
    banco_migrado: Parametros,
    parametros_rotinas: Parametros,
    engine_dono: AsyncEngine,
    engine_rotinas: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with montar(banco_migrado, parametros_rotinas, engine_dono, monkeypatch) as m:
        await _modo(m.sistema_url, "muda_so_texto")
        resultado = await conferir_sistemas_proprios(engine_rotinas, m.despachante, [ADMIN])
        assert m.nome_vitrine in resultado.mudaram
        assert m.nome_vitrine not in resultado.suspensas
        assert await _ferramenta(engine_rotinas, m.ferramenta_id) == ("ativa", None)
        async with engine_dono.connect() as c:
            frases = (
                (
                    await c.execute(
                        text(
                            "SELECT frase FROM extrato WHERE tipo = 'ferramenta_mudou'"
                            " AND dados ->> 'ferramenta_id' = :i"
                        ),
                        {"i": str(m.ferramenta_id)},
                    )
                )
                .scalars()
                .all()
            )
            avisos = (
                await c.execute(
                    text("SELECT count(*) FROM avisos WHERE corpo ->> 'ferramenta_id' = :i"),
                    {"i": str(m.ferramenta_id)},
                )
            ).scalar_one()
        assert frases == [f"Ferramenta {m.nome_vitrine} mudou no sistema sem pedir mais; so registramos."]
        assert avisos == 0


async def test_sistema_que_nao_responde_nao_para_os_outros(
    banco_migrado: Parametros,
    parametros_rotinas: Parametros,
    engine_dono: AsyncEngine,
    engine_rotinas: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with montar(banco_migrado, parametros_rotinas, engine_dono, monkeypatch) as m:
        async with engine_dono.begin() as c:
            mudo_id = await repositorio.cadastrar_sistema(
                c, nome=f"aaa-mudo-{uuid.uuid4().hex[:6]}", endereco="http://127.0.0.1:9/mcp", tipo_chave="por_pessoa"
            )
            await c.execute(
                text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = :v WHERE id = :i"),
                {"v": VERSAO_MCP, "i": mudo_id},
            )
            await repositorio.revisar_ferramenta(
                c,
                sistema_id=mudo_id,
                definicao_declarada={"name": "x", "inputSchema": {"type": "object"}},
                nome_vitrine=f"mudo_{uuid.uuid4().hex[:6]}",
                descricao="x",
                classe="le",
                revisado_por="t",
            )
        await _modo(m.sistema_url, "muda_definicao")
        resultado = await conferir_sistemas_proprios(engine_rotinas, m.despachante, [ADMIN])
        assert any(nome.startswith("aaa-mudo-") for nome in resultado.sem_resposta)
        assert m.nome_vitrine in resultado.suspensas
        async with engine_dono.begin() as c:
            await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :i"), {"i": mudo_id})
