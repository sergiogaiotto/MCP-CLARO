"""Programas que rodam sozinhos, terceira rodada de correcoes da Fase 2.

Leva para o repositorio as provas do verificador da segunda rodada que nenhum teste do grupo pegava:

- Reativar o aplicativo de um programa encerrado nao vale (a guarda ``pr.situacao <> 'encerrado'`` do UPDATE de
  ``TelasDosProgramas.reativar_aplicativo``).
- Retomar a ronda ou o vigia com a data de fim vencida usa a frase das portas (``retomar`` liga ``das_portas`` pelo
  ``client_id`` lido de ``PATROL_CLIENT_ID`` e ``WATCH_CLIENT_ID``, em ``rotinas.programas.protegidos``).

E o aviso depois de reativar o aplicativo: os pedidos voltam a passar quando o cadastro valer e nenhum bloqueio
ou pausa os segurar (o programa pausado segue pausado).

Mesmo arranjo de ``test_programas`` (``telas``, ``subir_telas``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.telas.admin_programas import FIM_VENCIDO, FIM_VENCIDO_DAS_PORTAS
from tests.integracao.test_programas import Telinhas, _cadastro, subir_telas, telas  # noqa: F401 (fixture)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

REATIVADO = (
    "Pronto. O aplicativo do programa foi reativado. Os pedidos dele voltam a passar quando o cadastro do programa"
    " valer e nenhum bloqueio ou pausa os segurar."
)


async def _situacao_do_aplicativo(t: Telinhas, aplicativo_id: Any) -> str:
    async with t.dono.connect() as c:
        return str(
            (await c.execute(text("SELECT situacao FROM aplicativos WHERE id = :a"), {"a": aplicativo_id})).scalar_one()
        )


async def _pausar_aplicativo(t: Telinhas, aplicativo_id: Any) -> None:
    """Como a conferencia do banco faz depois de uma restauracao."""
    async with t.dono.begin() as c:
        await c.execute(text("UPDATE aplicativos SET situacao = 'pausado' WHERE id = :a"), {"a": aplicativo_id})


async def _cadastrar(t: Telinhas, **extra: str) -> tuple[Any, str, Any, str]:
    nav, csrf = await t.navegador(t.admin_id)
    dados = _cadastro(t, responsavel=str(t.rui_id), substituto=str(t.sara_id)) | extra
    r = await nav.post("/telas/admin/programas", dados, csrf=csrf)
    assert r.status_code == 303, r.text
    programa = await t.programa(dados["client_id"])
    return nav, csrf, programa, f"/telas/admin/programas/{programa.aplicativo_id}"


async def _pausado_com_fim_vencido(t: Telinhas, aplicativo_id: Any) -> None:
    async with t.dono.begin() as c:
        await c.execute(
            text(
                "UPDATE programas SET situacao = 'pausado', fim = (now() AT TIME ZONE 'America/Sao_Paulo')::date - 1"
                " WHERE aplicativo_id = :a"
            ),
            {"a": aplicativo_id},
        )


# --- Reativar o aplicativo --------------------------------------------------------------------------


async def test_aplicativo_de_programa_encerrado_nao_se_reativa(telas: Telinhas) -> None:  # noqa: F811
    """Prova (b) do verificador: com o programa encerrado, o UPDATE nao vale, nem por POST direto."""
    nav, csrf, programa, base = await _cadastrar(telas)
    assert (await nav.post(f"{base}/encerrar", {"motivo": "Contrato acabou"}, csrf=csrf)).status_code == 303
    await _pausar_aplicativo(telas, programa.aplicativo_id)
    pagina = await nav.get(base)
    assert "Reativar o aplicativo" not in pagina.text
    r = await nav.post(f"{base}/aplicativo", {}, csrf=csrf)
    assert (r.status_code, r.headers["location"]) == (303, f"{base}?feito=programa_nada_mudou")
    assert await _situacao_do_aplicativo(telas, programa.aplicativo_id) == "pausado"
    assert await telas.extrato("admin_programa_aplicativo_reativado", programa.aplicativo_id) == []


async def test_reativar_com_o_programa_pausado_nao_promete_que_os_pedidos_passam(
    telas: Telinhas,  # noqa: F811
) -> None:
    """O aplicativo volta, o programa segue pausado, e o aviso diz o que ainda segura os pedidos."""
    nav, csrf, programa, base = await _cadastrar(telas)
    assert (await nav.post(f"{base}/pausar", {"motivo": "Conferir"}, csrf=csrf)).status_code == 303
    await _pausar_aplicativo(telas, programa.aplicativo_id)
    r = await nav.post(f"{base}/aplicativo", {}, csrf=csrf)
    assert (r.status_code, r.headers["location"]) == (303, f"{base}?feito=programa_aplicativo_reativado")
    assert await _situacao_do_aplicativo(telas, programa.aplicativo_id) == "ativo"
    assert (await telas.programa(f"robo-tela-{telas.sufixo}")).situacao == "pausado"  # o programa segue pausado
    pagina = await nav.get(r.headers["location"])
    assert REATIVADO in pagina.text
    assert "enquanto o cadastro do programa valer" not in pagina.text


async def test_programa_encerrado_com_o_aplicativo_pausado_sai_das_listas_de_pausados(
    telas: Telinhas,  # noqa: F811
) -> None:
    """Programa encerrado nao tem o que reativar: o aplicativo dele, pausado pela conferencia do banco, sai da lista
    de pausados (``consultas.aplicativos_pausados``), que alimenta o cartao da tela Aplicativos e o aviso da tela
    Portas. Antes de encerrar, ele esta la."""
    from mcp_claro.telas import consultas

    async def pausados() -> set[Any]:
        async with telas.dono.connect() as c:
            return {linha.id for linha in await consultas.aplicativos_pausados(c)}

    nav, csrf, programa, base = await _cadastrar(telas)
    await _pausar_aplicativo(telas, programa.aplicativo_id)
    assert programa.aplicativo_id in await pausados()
    assert (await nav.post(f"{base}/encerrar", {"motivo": "Contrato acabou"}, csrf=csrf)).status_code == 303
    assert programa.aplicativo_id not in await pausados()
    assert await _situacao_do_aplicativo(telas, programa.aplicativo_id) == "pausado"


# --- Retomar a ronda ou o vigia com a data de fim vencida -------------------------------------------


@pytest.mark.parametrize("parametro", ["PATROL_CLIENT_ID", "WATCH_CLIENT_ID"])
async def test_retomar_a_ronda_ou_o_vigia_vencidos_usa_a_frase_das_portas(
    banco_migrado: Parametros, parametro: str
) -> None:
    """Prova (b) do verificador para P4: ``retomar`` reconhece a ronda e o vigia pelo ``client_id`` dos parametros.

    Cada teste usa um ``client_id`` proprio para a ronda e o vigia: as fixtures da ronda e do vigia cadastram os
    valores padrao no mesmo banco.
    """
    portas = {"PATROL_CLIENT_ID": f"ronda-{uuid.uuid4().hex[:8]}", "WATCH_CLIENT_ID": f"vigia-{uuid.uuid4().hex[:8]}"}
    async with subir_telas(banco_migrado, **portas) as t:
        nav, csrf, porta, base = await _cadastrar(t, client_id=portas[parametro], nome=f"Porta {t.sufixo}")
        await _pausado_com_fim_vencido(t, porta.aplicativo_id)
        r = await nav.post(f"{base}/retomar", {}, csrf=csrf)
        assert r.status_code == 400
        assert FIM_VENCIDO_DAS_PORTAS in r.text
        assert "Na próxima rodada da rotina" not in r.text
        assert (await t.programa(portas[parametro])).situacao == "pausado"

        # O programa comum, na mesma tela, segue com a frase que fala do encerramento.
        nav, csrf, comum, base = await _cadastrar(t)
        await _pausado_com_fim_vencido(t, comum.aplicativo_id)
        r = await nav.post(f"{base}/retomar", {}, csrf=csrf)
        assert r.status_code == 400
        assert FIM_VENCIDO in r.text
        assert FIM_VENCIDO_DAS_PORTAS not in r.text
