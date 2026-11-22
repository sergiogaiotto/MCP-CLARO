"""R6b-1 (contrato K3), contra o banco: os nomes neutros que o aplicativo ve no tools/list sao os que a revisao gravou
e os que a previa do ADMIN mostrou, qualquer que seja a ordem das chaves na definicao.

O PostgreSQL devolve as chaves de um ``jsonb`` na ordem dele (a mais curta antes, em bytes, e depois a menor em
bytes), e a vitrine troca os nomes de novo no esquema lido do banco. ``esquema.nomes_neutros`` conta as definicoes
nessa ordem; aqui se confere a ordem contra o banco e o caminho inteiro: revisao, banco, vitrine e previa.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import AsyncIterator
from typing import Any

import mcp_types as types
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.cadastro import esquema as modulo
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.entrada.argumentos import ArgumentosInvalidos, conferir_argumentos
from mcp_claro.telas import admin
from mcp_claro.versao import VERSAO_MCP
from tests.apoio_terceiros import cadastrar_terceiro, gravar_revisao_do_servidor
from tests.unidade.test_r6b_nomes_neutros import _como_o_banco

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MARCA = "MARCA_FORNECEDOR"


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(banco_migrado, pool_size=3, max_overflow=2)
    try:
        yield engine
    finally:
        await engine.dispose()


def _chaves_dificeis() -> list[str]:
    """Chaves de tamanhos e alfabetos variados (letras de 1, 2 e 3 bytes em UTF-8), em ordem sorteada."""
    chaves = ["", "b", "aa", "é", "ab", "Z", "a", "中", "~", "%2F", "$defs", "definitions", "d9", "d10", "d1"]
    sorteio = random.Random(20261122)
    alfabeto = "ab~/%$dZ0 éãß中"
    chaves += ["".join(sorteio.choice(alfabeto) for _ in range(sorteio.randint(0, 6))) for _ in range(300)]
    chaves = list(dict.fromkeys(chaves))
    sorteio.shuffle(chaves)
    return chaves


async def test_a_ordem_das_chaves_e_a_do_banco(dono: AsyncEngine) -> None:
    objeto = {chave: i for i, chave in enumerate(_chaves_dificeis())}
    async with dono.connect() as c:
        lido = (await c.execute(text("SELECT CAST(:j AS jsonb)"), {"j": json.dumps(objeto)})).scalar_one()
        como_texto = (
            await c.execute(text("SELECT CAST(CAST(:j AS jsonb) AS text)"), {"j": json.dumps(objeto)})
        ).scalar_one()
    assert lido == objeto
    assert list(json.loads(como_texto)) == list(lido)
    # A ordem que nomes_neutros usa e a que a simulacao dos testes de unidade usa sao as do banco.
    assert sorted(objeto, key=modulo._ordem_do_banco) == list(lido)
    assert list(_como_o_banco(objeto)) == list(lido)


def _declarada() -> dict[str, Any]:
    """Definicao com as chaves fora da ordem do banco: ``definitions`` antes de ``$defs``, nomes do fornecedor em
    ordem sorteada, mais de nove definicoes e definicoes dentro de partes."""
    nomes = [f"{MARCA}_{letra}" for letra in "qwertyuiopas"]
    random.Random(11).shuffle(nomes)
    propriedades: dict[str, Any] = {
        "n": {"$ref": "#/definitions/" + f"{MARCA}/numero".replace("/", "~1")},
        "dentro": {"type": "object", "$defs": {f"{MARCA} z": {"type": "string"}, f"{MARCA} a": {"type": "integer"}}},
    }
    propriedades |= {f"campo_{i}": {"$ref": f"#/$defs/{nome}"} for i, nome in enumerate(nomes)}
    return {
        "name": "consultar",
        "description": f"{MARCA} descricao",
        "inputSchema": {
            "$schema": f"{MARCA} esquema",
            "properties": propriedades,
            "definitions": {f"{MARCA}/numero": {"type": "integer", "minimum": 1}},
            "type": "object",
            "$defs": {nome: {"type": "string", "minLength": i} for i, nome in enumerate(nomes)},
            "required": ["n"],
        },
    }


async def test_a_vitrine_e_a_previa_mostram_os_nomes_que_a_revisao_gravou(dono: AsyncEngine) -> None:
    declarada = _declarada()
    sufixo = uuid.uuid4().hex[:8]
    async with dono.begin() as c:
        terceiro_id = await cadastrar_terceiro(c)
        # A definicao como veio (a ordem do fornecedor), como as regras e os testes chamam a revisao.
        ferramenta_id = await repositorio.revisar_ferramenta(
            c,
            sistema_id=terceiro_id,
            definicao_declarada=declarada,
            nome_vitrine=f"consultar_{sufixo}",
            descricao="Consulta um numero no servidor do fornecedor.",
            classe="le",
            revisado_por="admin-teste",
        )
        app_id = await repositorio.habilitar_aplicativo(c, client_id=f"app-{sufixo}", nome=f"App {sufixo}", familia="t")
        pessoa = await repositorio.pessoa_no_login(c, "https://login.teste", f"pessoa-{sufixo}", None)
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"modelo-{sufixo}",
            descricao="Ordem de teste",
            ferramentas=[(terceiro_id, "consultar")],
            validade_dias=30,
            criado_por="t",
        )
        await repositorio.conceder_ordem(
            c, pessoa_id=pessoa.id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
        )
        await gravar_revisao_do_servidor(c, terceiro_id, [ferramenta_id])
        gravada = (
            await c.execute(
                text("SELECT esquema_entrada, definicao_declarada FROM ferramentas_revisadas WHERE id = :i"),
                {"i": ferramenta_id},
            )
        ).one()
    gravado = gravada.esquema_entrada
    assert MARCA not in json.dumps(gravado, ensure_ascii=False)

    # O que a entrada manda ao aplicativo em tools/list: os mesmos nomes que estao gravados.
    async with dono.connect() as c:
        vitrine = await repositorio.vitrine(c, pessoa.id, app_id, VERSAO_MCP, terceiros=True)
        para_chamar = await repositorio.ferramenta_na_vitrine(
            c, pessoa.id, app_id, f"consultar_{sufixo}", VERSAO_MCP, terceiros=True
        )
    assert [f.id for f in vitrine] == [ferramenta_id]
    tool = types.Tool.model_validate(vitrine[0].como_tool()).model_dump(by_alias=True, exclude_none=True, mode="json")
    assert MARCA not in json.dumps(tool, ensure_ascii=False)
    assert tool["inputSchema"] == gravado

    # A previa do ADMIN (da definicao como veio e como a tela a le do banco) mostra o mesmo.
    for entrada in (declarada["inputSchema"], gravada.definicao_declarada["inputSchema"]):
        previa, recusa = admin._previa_de_terceiro(entrada, {})
        assert recusa is None
        assert previa is not None
        assert json.loads(previa) == gravado

    # Contados na ordem do banco: na raiz, "$defs" (os 12 nomes do fornecedor, do menor ao maior) antes de
    # "definitions"; depois as definicoes dentro de "dentro" (" a" antes de " z").
    em_ordem = sorted(declarada["inputSchema"]["$defs"], key=modulo._ordem_do_banco)
    assert {campo: parte["$ref"] for campo, parte in gravado["properties"].items() if campo.startswith("campo_")} == {
        campo: f"#/$defs/d{em_ordem.index(parte['$ref'].removeprefix('#/$defs/')) + 1}"
        for campo, parte in declarada["inputSchema"]["properties"].items()
        if campo.startswith("campo_")
    }
    assert gravado["definitions"] == {"d13": {"type": "integer", "minimum": 1}}
    assert gravado["properties"]["n"] == {"$ref": "#/definitions/d13"}
    assert gravado["properties"]["dentro"]["$defs"] == {"d15": {"type": "string"}, "d14": {"type": "integer"}}

    # A entrada confere como antes.
    assert para_chamar is not None
    conferir_argumentos(para_chamar.esquema_entrada, {"n": 2, "campo_3": "x" * 20}, terceiro=True)
    for errado in ({"n": 0}, {"n": 1, "campo_0": 1}, {"n": 1, "campo_3": "xy"}, {}):
        with pytest.raises(ArgumentosInvalidos):
            conferir_argumentos(para_chamar.esquema_entrada, errado, terceiro=True)
