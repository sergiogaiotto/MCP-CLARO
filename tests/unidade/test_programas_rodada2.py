"""Programas, segunda rodada de correcoes da Fase 2, sem banco: frases de acao, retomar com o fim vencido e telas."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import FRASES, frase
from mcp_claro.regras import programas
from mcp_claro.regras.programas import Programa
from mcp_claro.rotinas.programas import ONDE_ENCERRAR_NO_FIM, protegidos
from mcp_claro.telas import admin_programas
from mcp_claro.telas.admin_programas import FEITOS, FIM_VENCIDO, FIM_VENCIDO_DAS_PORTAS, Prazos, recusa_para_retomar

HOJE = date(2026, 11, 19)
AGORA = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
P = Parametros(APP_ENV="teste")


def programa(**mudancas: object) -> Programa:
    base = Programa(
        aplicativo_id=uuid.uuid4(),
        client_id="robo",
        nome="Robo",
        finalidade="Le as filas.",
        situacao="pausado",
        fim=HOJE - timedelta(days=1),
        responsavel_id=uuid.uuid4(),
        responsavel="Rui",
        responsavel_ativo=True,
        substituto_id=uuid.uuid4(),
        substituto="Sara",
        substituto_ativo=True,
        revisado_em=AGORA - timedelta(days=10),
        chave_trocada_em=AGORA - timedelta(days=10),
        criado_em=AGORA - timedelta(days=20),
        visto_em=None,
        hoje=HOJE,
    )
    return dataclasses.replace(base, **mudancas)  # type: ignore[arg-type]


# --- P1 (achado 37) -----------------------------------------------------------------------------------


@pytest.mark.parametrize("base", ["acao_recusada", "acao_pedida", "acao_repetida"])
def test_acao_de_programa_tem_frase_de_programa(base: str) -> None:
    """Recusa, pedido e pedido igual de um programa dizem "programa X, responsavel Y", com os campos de acao_*."""
    de_programa = programas.PARA_PROGRAMA[base]
    assert de_programa == f"programa_{base}"
    assert FRASES[de_programa].campos() == FRASES[base].campos() | {"programa", "responsavel"}
    campos = dict.fromkeys(FRASES[base].campos(), "x") | {"programa": "Robo", "responsavel": "Rui"}
    assert "programa Robo, responsavel Rui" in frase(de_programa, **campos)


def test_frases_de_acao_de_programa() -> None:
    assert (
        frase(
            "programa_acao_recusada", ferramenta="fechar", programa="Robo", responsavel="Rui", motivo="programa_so_le"
        )
        == "Acao em fechar pelo programa Robo, responsavel Rui, recusada antes de virar protocolo: programa_so_le."
    )
    assert (
        frase("programa_acao_pedida", ferramenta="fechar", programa="Robo", responsavel="Rui", protocolo="CLR-1")
        == "Acao fechar pedida pelo programa Robo, responsavel Rui; protocolo CLR-1."
    )
    assert (
        frase("programa_acao_repetida", programa="Robo", responsavel="Rui", protocolo="CLR-1")
        == "Pedido do programa Robo, responsavel Rui, igual ao do protocolo CLR-1: devolvida a operacao original,"
        " nada novo saiu."
    )
    assert frase("admin_programa_aplicativo_reativado", programa="Robo", responsavel="Rui", quem="Carla") == (
        "Programa Robo, responsavel Rui: Carla reativou o aplicativo do programa, que estava pausado."
    )


# --- P3 (observacao 3) --------------------------------------------------------------------------------


def test_aviso_do_cadastro_de_novo_com_o_aplicativo_pausado_diz_a_verdade() -> None:
    texto = FEITOS["programa_recadastrado_app_pausado"]
    assert "O cadastro do programa voltou a valer" in texto
    assert "segue pausado" in texto
    assert "Reativar o aplicativo" in texto
    assert "não sai sozinha" in texto
    assert "aplicativo do programa foi reativado" in FEITOS["programa_aplicativo_reativado"]


def test_tela_do_programa_mostra_onde_reativar_o_aplicativo() -> None:
    modelo = Path(admin_programas.__file__).parent / "modelos" / "admin" / "programas_detalhe.html"
    texto = modelo.read_text(encoding="utf-8")
    assert "/telas/admin/programas/{{ programa.aplicativo_id }}/aplicativo" in texto
    assert "Reativar o aplicativo" in texto


# --- P4 (observacao 2) --------------------------------------------------------------------------------


def test_retomar_com_fim_vencido_diz_que_a_rotina_encerra_e_manda_estender_logo() -> None:
    assert "'pausado'" in ONDE_ENCERRAR_NO_FIM  # a rotina encerra tambem o pausado: a frase precisa dizer isso
    vencido = programa()
    (erro,) = recusa_para_retomar(vencido, Prazos.de(vencido, P, AGORA))
    assert erro == FIM_VENCIDO
    assert "Na próxima rodada da rotina dos programas, ele é encerrado." in erro
    assert "estenda a data de fim agora, nesta tela, e depois retome" in erro
    assert "cadastre-o de novo" in erro
    assert "antes de retomar" not in erro  # a frase antiga prometia que bastava estender depois


def test_retomar_a_ronda_com_fim_vencido_nao_fala_em_encerrar() -> None:
    """A rotina nunca encerra a ronda e o vigia: para eles, a frase so manda estender."""
    ronda = programa(client_id=protegidos(P)[0])
    (erro,) = recusa_para_retomar(ronda, Prazos.de(ronda, P, AGORA), das_portas=True)
    assert erro == FIM_VENCIDO_DAS_PORTAS
    assert "encerrado" not in erro
    assert "estenda a data de fim nesta tela" in erro


def test_retomar_com_fim_valendo_nao_fala_da_data() -> None:
    em_dia = programa(fim=HOJE)
    assert recusa_para_retomar(em_dia, Prazos.de(em_dia, P, AGORA)) == []
