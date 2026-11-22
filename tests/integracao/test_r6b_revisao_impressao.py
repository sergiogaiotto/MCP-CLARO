"""R6b-3: a revisao da ferramenta pela tela grava so a definicao que o ADMIN viu.

A pagina da ferramenta manda, num campo escondido (``impressao_vista``), a impressao da definicao que ela mostrou. Se
uma leitura das declaracoes gravar outra definicao antes do POST, a revisao e recusada (409), nada e gravado, e a
pagina volta com a definicao nova e a frase. Com a mesma impressao, ou sem o campo (formulario antigo), grava como
antes.

A prova do revisor (concorrencia), invertida: o fornecedor muda a ferramenta entre a pagina e o POST.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro.cadastro.impressao import impressao as impressao_da
from mcp_claro.config.settings import Parametros
from mcp_claro.telas import admin
from tests.integracao.test_telas_fase3_admin import Cena, subir

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

FERRAMENTA = "consultar_pedido"


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    async with subir(banco_migrado) as c:
        yield c


def _impressao_da_pagina(html: str) -> str:
    achado = re.search(r'name="impressao_vista" value="([^"]+)"', html)
    assert achado, "a pagina da ferramenta nao leva a impressao que mostrou"
    return achado.group(1)


async def _revisadas(cena: Cena, sistema_id: uuid.UUID) -> list[Any]:
    async with cena.amb.engine.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT impressao, esquema_entrada, definicao_declarada, situacao FROM ferramentas_revisadas"
                        " WHERE sistema_id = :s AND nome_original = :n ORDER BY versao"
                    ),
                    {"s": sistema_id, "n": FERRAMENTA},
                )
            ).all()
        )


async def _vista(cena: Cena, sistema_id: uuid.UUID) -> Any:
    async with cena.amb.engine.connect() as c:
        return (
            await c.execute(
                text("SELECT definicao, impressao FROM ferramentas_vistas WHERE sistema_id = :s AND nome = :n"),
                {"s": sistema_id, "n": FERRAMENTA},
            )
        ).one()


async def test_definicao_que_mudou_depois_da_pagina_nao_e_gravada(cena: Cena) -> None:
    sistema_id, _ = await cena.terceiro()
    await cena.ler(sistema_id)  # o servidor declara consultar_pedido com o campo "pedido"
    pagina = await cena.nav.get(f"/telas/admin/sistemas/{sistema_id}/ferramenta?nome={FERRAMENTA}")
    assert pagina.status_code == 200
    assert "extra_0" not in pagina.text  # o que o ADMIN tem na tela
    viu = _impressao_da_pagina(pagina.text)
    assert viu == (await _vista(cena, sistema_id)).impressao

    # Enquanto o ADMIN preenche o formulario, o fornecedor muda a ferramenta e uma leitura grava a definicao nova.
    await cena.modo("muda_definicao")
    await cena.ler(sistema_id)
    await cena.modo("normal")
    nova = await _vista(cena, sistema_id)
    assert nova.impressao != viu

    r = await cena.revisar(sistema_id, FERRAMENTA, classe="le", risco="baixo", impressao_vista=viu)
    assert r.status_code == 409, "a revisada nasceu com um campo que o ADMIN nao viu"
    assert admin.FERRAMENTA_MUDOU in r.text
    assert await _revisadas(cena, sistema_id) == []  # nada gravado
    # A pagina volta com a definicao nova (o campo extra_0) e a impressao dela.
    assert "extra_0" in r.text
    assert _impressao_da_pagina(r.text) == nova.impressao

    # O ADMIN confere a definicao nova e revisa de novo: grava a que ele viu agora.
    r = await cena.revisar(sistema_id, FERRAMENTA, classe="le", risco="baixo", impressao_vista=nova.impressao)
    assert r.status_code == 303, r.text
    (revisada,) = await _revisadas(cena, sistema_id)
    assert revisada.impressao == nova.impressao == impressao_da(nova.definicao)
    assert revisada.definicao_declarada == nova.definicao
    assert "extra_0" in revisada.esquema_entrada["properties"]


async def test_com_a_mesma_impressao_ou_sem_o_campo_grava_como_antes(cena: Cena) -> None:
    sistema_id, _ = await cena.terceiro()
    await cena.ler(sistema_id)
    pagina = await cena.nav.get(f"/telas/admin/sistemas/{sistema_id}/ferramenta?nome={FERRAMENTA}")
    viu = _impressao_da_pagina(pagina.text)
    r = await cena.revisar(sistema_id, FERRAMENTA, impressao_vista=viu)
    assert r.status_code == 303, r.text
    # Sem o campo (formulario de antes desta versao), como hoje: grava a definicao da ultima leitura.
    await cena.modo("muda_definicao")
    await cena.ler(sistema_id)
    await cena.modo("normal")
    r = await cena.revisar(sistema_id, FERRAMENTA)
    assert r.status_code == 303, r.text
    primeira, segunda = await _revisadas(cena, sistema_id)
    assert (primeira.impressao, primeira.situacao) == (viu, "substituida")
    assert segunda.impressao == (await _vista(cena, sistema_id)).impressao
    assert segunda.situacao == "ativa"


async def test_a_pagina_de_um_proprio_tambem_leva_a_impressao(cena: Cena) -> None:
    async with cena.amb.engine.begin() as c:
        sistema_id = (
            await c.execute(
                text("INSERT INTO sistemas (nome, endereco, tipo_chave) VALUES (:n, :e, 'compartilhada') RETURNING id"),
                {"n": f"proprio-{uuid.uuid4().hex[:8]}", "e": cena.endereco},
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO ferramentas_vistas (sistema_id, nome, definicao, impressao)"
                " VALUES (:s, :n, CAST(:d AS jsonb), :i)"
            ),
            {
                "s": sistema_id,
                "n": FERRAMENTA,
                "d": '{"name": "consultar_pedido", "inputSchema": {"type": "object"}}',
                "i": impressao_da({"name": "consultar_pedido", "inputSchema": {"type": "object"}}),
            },
        )
    pagina = await cena.nav.get(f"/telas/admin/sistemas/{sistema_id}/ferramenta?nome={FERRAMENTA}")
    assert pagina.status_code == 200
    viu = _impressao_da_pagina(pagina.text)
    outra = "sha256:" + "0" * 64
    r = await cena.revisar(sistema_id, FERRAMENTA, impressao_vista=outra)
    assert r.status_code == 409
    assert admin.FERRAMENTA_MUDOU in r.text
    assert await _revisadas(cena, sistema_id) == []
    r = await cena.revisar(sistema_id, FERRAMENTA, impressao_vista=viu)
    assert r.status_code == 303, r.text
