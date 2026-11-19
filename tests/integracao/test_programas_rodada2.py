"""Programas que rodam sozinhos, segunda rodada de correcoes da Fase 2.

- Achado 37: a recusa, o pedido e o pedido igual de uma acao de um programa vao ao extrato com "programa X,
  responsavel Y" (``regras/programas.PARA_PROGRAMA`` e ``linha``).
- Observacao 3 da primeira rodada: o cadastro de novo de um programa encerrado nao reativa o aplicativo
  pausado. A tela diz que o aplicativo segue pausado e onde o ADMIN o reativa.
- Observacao 2: a recusa de retomar um programa pausado com a data de fim vencida diz que a rotina o encerra.

Mesmos arranjos de ``test_programas`` (``prog`` para a entrada de ponta a ponta, ``telas`` para as telas).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from mcp_claro.cadastro import repositorio
from mcp_claro.saida import fila
from mcp_claro.saida.pedidos import Pedido, Pedidos
from mcp_claro.versao import VERSAO_MCP
from tests.integracao.test_programas import (  # noqa: F401 (fixtures)
    Programas,
    Telinhas,
    _cadastro,
    motivo,
    prog,
    telas,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


# --- P1 (achado 37): acoes de programa no extrato --------------------------------------------------


async def test_recusa_de_acao_de_programa_diz_programa_e_responsavel(prog: Programas) -> None:  # noqa: F811
    """A prova do verificador, ao contrario: a recusa sai como acao de programa, com o programa e o responsavel."""
    acao = prog.vitrine["fechar_chamado"]
    with pytest.raises(MCPError) as erro:
        await prog.chamar(acao, {"id": "CH-7"})
    assert motivo(erro.value) == "programa_so_le"
    recusas = await prog.extrato("programa_acao_recusada")
    assert [x.frase for x in recusas] == [
        f"Acao em {acao} pelo programa {prog.nome}, responsavel pessoa-ana, recusada antes de virar protocolo:"
        " programa_so_le."
    ]
    assert recusas[0].pessoa_id == prog.conta_id
    # Nem a frase de acao sem o programa, nem a de leitura.
    assert await prog.extrato("acao_recusada") == []
    assert await prog.extrato("programa_leitura_recusada") == []


async def test_acao_pedida_e_repetida_de_programa_dizem_programa_e_responsavel(prog: Programas) -> None:  # noqa: F811
    """Com as frases de verdade: o pedido e o pedido igual de um programa passam por ``programas.linha``.

    Hoje a entrada nao deixa um programa agir (``PROGRAM_ACTIONS_ENABLED`` nao liga): o pedido entra direto pela
    porta de saida (``Pedidos.pedir``), como faria a entrada, e e cancelado no fim.
    """
    async with prog.amb.entrada_db.connect() as c:
        robo = await repositorio.aplicativo_por_client_id(c, prog.client_id)
        ferramenta = await repositorio.ferramenta_na_vitrine(
            c, prog.conta_id, prog.robo_id, prog.vitrine["fechar_chamado"], VERSAO_MCP
        )
    assert robo is not None
    assert ferramenta is not None
    pedidos = Pedidos(prog.amb.p, prog.amb.entrada_db)
    argumentos = {"id": f"CH-{prog.sufixo}"}
    op, repetida = await pedidos.pedir(Pedido(prog.conta_id, robo, ferramenta, argumentos, None))
    try:
        assert repetida is False
        igual, repetida = await pedidos.pedir(Pedido(prog.conta_id, robo, ferramenta, argumentos, None))
        assert (igual.id, repetida) == (op.id, True)
        async with prog.amb.admin.connect() as c:
            linhas = (
                await c.execute(
                    text("SELECT tipo, frase FROM extrato WHERE protocolo = :p ORDER BY id"), {"p": op.protocolo}
                )
            ).all()
        nome = prog.vitrine["fechar_chamado"]
        assert [(x.tipo, x.frase) for x in linhas] == [
            (
                "programa_acao_pedida",
                f"Acao {nome} pedida pelo programa {prog.nome}, responsavel pessoa-ana; protocolo {op.protocolo}.",
            ),
            (
                "programa_acao_repetida",
                f"Pedido do programa {prog.nome}, responsavel pessoa-ana, igual ao do protocolo {op.protocolo}:"
                " devolvida a operacao original, nada novo saiu.",
            ),
        ]
    finally:
        async with prog.amb.entrada_db.begin() as c:
            await fila.cancelar(c, op.id, pessoa_id=prog.conta_id)
    assert "fechar_chamado" not in prog.sistema_app.state.contagem.chamadas


# --- P3 (observacao 3): cadastro de novo com o aplicativo pausado ----------------------------------


async def _situacao_do_aplicativo(t: Telinhas, aplicativo_id: Any) -> str:
    async with t.dono.connect() as c:
        return str(
            (await c.execute(text("SELECT situacao FROM aplicativos WHERE id = :a"), {"a": aplicativo_id})).scalar_one()
        )


async def test_cadastro_de_novo_nao_reativa_o_aplicativo_pausado_e_a_tela_diz_onde_reativar(
    telas: Telinhas,  # noqa: F811
) -> None:
    s = telas.sufixo
    client_id = f"robo-tela-{s}"
    nav, csrf = await telas.navegador(telas.admin_id)
    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id)),
        csrf=csrf,
    )
    assert r.status_code == 303
    antes = await telas.programa(client_id)
    base = f"/telas/admin/programas/{antes.aplicativo_id}"
    r = await nav.post(f"{base}/encerrar", {"motivo": "Contrato do robo acabou"}, csrf=csrf)
    assert r.status_code == 303
    # O aplicativo volta pausado, como depois da conferencia do banco (continuidade).
    async with telas.dono.begin() as c:
        await c.execute(text("UPDATE aplicativos SET situacao = 'pausado' WHERE id = :a"), {"a": antes.aplicativo_id})

    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(
            telas,
            fim=(date.today() + timedelta(days=100)).isoformat(),
            responsavel=str(telas.rui_id),
            substituto=str(telas.sara_id),
        ),
        csrf=csrf,
    )
    assert (r.status_code, r.headers["location"]) == (303, f"{base}?feito=programa_recadastrado_app_pausado")
    assert (await telas.programa(client_id)).situacao == "ativo"
    # Pausa e decisao do ADMIN: o cadastro de novo nao reativa o aplicativo sozinho.
    assert await _situacao_do_aplicativo(telas, antes.aplicativo_id) == "pausado"
    pagina = await nav.get(r.headers["location"])
    assert "O cadastro do programa voltou a valer" in pagina.text
    assert "segue pausado" in pagina.text
    assert "voltou a valer. Confira abaixo" not in pagina.text  # a frase de quando tudo volta
    assert "Reativar o aplicativo" in pagina.text
    assert f'action="{base}/aplicativo"' in pagina.text

    # Quem nao e ADMIN nao reativa.
    rui, csrf_rui = await telas.navegador(telas.rui_id)
    r = await rui.post(f"{base}/aplicativo", {}, csrf=csrf_rui)
    assert r.status_code == 403
    assert await _situacao_do_aplicativo(telas, antes.aplicativo_id) == "pausado"

    # O ADMIN reativa na tela do programa: fica no extrato, com quem fez.
    r = await nav.post(f"{base}/aplicativo", {}, csrf=csrf)
    assert (r.status_code, r.headers["location"]) == (303, f"{base}?feito=programa_aplicativo_reativado")
    assert await _situacao_do_aplicativo(telas, antes.aplicativo_id) == "ativo"
    linhas = await telas.extrato("admin_programa_aplicativo_reativado", antes.aplicativo_id)
    assert [(x.frase, x.pessoa_id) for x in linhas] == [
        (
            f"Programa Robo da tela {s}, responsavel Rui {s}: Carla Admin reativou o aplicativo do programa, que"
            " estava pausado.",
            telas.admin_id,
        )
    ]
    pagina = await nav.get(base)
    assert "Reativar o aplicativo" not in pagina.text
    # Reativar de novo nao muda nada nem vai ao extrato.
    r = await nav.post(f"{base}/aplicativo", {}, csrf=csrf)
    assert r.headers["location"] == f"{base}?feito=programa_nada_mudou"
    assert len(await telas.extrato("admin_programa_aplicativo_reativado", antes.aplicativo_id)) == 1


async def test_cadastro_de_novo_com_o_aplicativo_ativo_diz_que_voltou_a_valer(telas: Telinhas) -> None:  # noqa: F811
    nav, csrf = await telas.navegador(telas.admin_id)
    dados = _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id))
    assert (await nav.post("/telas/admin/programas", dados, csrf=csrf)).status_code == 303
    antes = await telas.programa(f"robo-tela-{telas.sufixo}")
    base = f"/telas/admin/programas/{antes.aplicativo_id}"
    assert (await nav.post(f"{base}/encerrar", {"motivo": "Contrato acabou"}, csrf=csrf)).status_code == 303
    r = await nav.post("/telas/admin/programas", dados, csrf=csrf)
    assert r.headers["location"] == f"{base}?feito=programa_recadastrado"
    pagina = await nav.get(r.headers["location"])
    assert "cadastrado de novo e voltou a valer" in pagina.text
    assert "Reativar o aplicativo" not in pagina.text


async def test_cadastro_de_novo_com_o_aplicativo_bloqueado_nao_fala_em_pausa(telas: Telinhas) -> None:  # noqa: F811
    nav, csrf = await telas.navegador(telas.admin_id)
    dados = _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id))
    assert (await nav.post("/telas/admin/programas", dados, csrf=csrf)).status_code == 303
    antes = await telas.programa(f"robo-tela-{telas.sufixo}")
    base = f"/telas/admin/programas/{antes.aplicativo_id}"
    assert (await nav.post(f"{base}/encerrar", {"motivo": "Contrato acabou"}, csrf=csrf)).status_code == 303
    async with telas.dono.begin() as c:
        await c.execute(text("UPDATE aplicativos SET situacao = 'bloqueado' WHERE id = :a"), {"a": antes.aplicativo_id})
    r = await nav.post("/telas/admin/programas", dados, csrf=csrf)
    assert r.headers["location"] == f"{base}?feito=programa_recadastrado_app_parado"
    pagina = await nav.get(r.headers["location"])
    assert "O cadastro do programa voltou a valer. Mas o aplicativo dele não está ativo" in pagina.text
    assert "O aplicativo deste programa está bloqueado." in pagina.text
    assert "Reativar o aplicativo" not in pagina.text  # so o pausado se reativa nesta tela
    r = await nav.post(f"{base}/aplicativo", {}, csrf=csrf)
    assert r.headers["location"] == f"{base}?feito=programa_nada_mudou"
    assert await _situacao_do_aplicativo(telas, antes.aplicativo_id) == "bloqueado"


# --- P4 (observacao 2): retomar com a data de fim vencida -----------------------------------------


async def test_retomar_com_o_fim_vencido_diz_que_a_rotina_encerra(telas: Telinhas) -> None:  # noqa: F811
    nav, csrf = await telas.navegador(telas.admin_id)
    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id)),
        csrf=csrf,
    )
    assert r.status_code == 303
    antes = await telas.programa(f"robo-tela-{telas.sufixo}")
    base = f"/telas/admin/programas/{antes.aplicativo_id}"
    async with telas.dono.begin() as c:
        await c.execute(
            text(
                "UPDATE programas SET situacao = 'pausado', fim = (now() AT TIME ZONE 'America/Sao_Paulo')::date - 1"
                " WHERE aplicativo_id = :a"
            ),
            {"a": antes.aplicativo_id},
        )
    r = await nav.post(f"{base}/retomar", {}, csrf=csrf)
    assert r.status_code == 400
    assert "Na próxima rodada da rotina dos programas, ele é encerrado." in r.text
    assert "Para mantê-lo, estenda a data de fim agora, nesta tela, e depois retome." in r.text
    assert "Estender a data de fim" in r.text  # a tela de estender esta ali, para o programa pausado
