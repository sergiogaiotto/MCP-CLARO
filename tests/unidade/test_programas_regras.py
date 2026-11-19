"""Regras dos programas que rodam sozinhos (decisao 9), sem banco: cadastro, tipo de chave, limite e frases."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import FRASES, frase
from mcp_claro.regras import programas
from mcp_claro.regras.programas import Programa

HOJE = date(2026, 11, 19)
AGORA = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def programa(**mudancas: object) -> Programa:
    base = Programa(
        aplicativo_id=uuid.uuid4(),
        client_id="robo",
        nome="Robo",
        finalidade="Le as filas.",
        situacao="ativo",
        fim=HOJE + timedelta(days=10),
        responsavel_id=uuid.uuid4(),
        responsavel="Rui",
        responsavel_ativo=True,
        substituto_id=uuid.uuid4(),
        substituto="Sara",
        substituto_ativo=True,
        revisado_em=AGORA,
        chave_trocada_em=AGORA,
        criado_em=AGORA,
        visto_em=None,
        hoje=HOJE,
    )
    return dataclasses.replace(base, **mudancas)  # type: ignore[arg-type]


def test_programa_com_cadastro_valendo_passa() -> None:
    assert programas.recusa_do_cadastro(programa()) is None


def test_vale_ate_o_fim_do_dia_de_fim() -> None:
    assert programas.recusa_do_cadastro(programa(fim=HOJE)) is None
    recusa = programas.recusa_do_cadastro(programa(fim=HOJE - timedelta(days=1)))
    assert recusa is not None
    assert recusa.motivo == "programa_vencido"
    assert "18/11/2026" in recusa.mensagem


@pytest.mark.parametrize(
    ("mudancas", "motivo"),
    [
        ({"situacao": "pausado"}, "programa_pausado"),
        ({"situacao": "encerrado"}, "programa_encerrado"),
        ({"responsavel_ativo": False}, "programa_sem_responsavel"),
        ({"substituto_ativo": False}, "programa_sem_responsavel"),
        # A situacao vem antes da data: encerrado e vencido diz "encerrado".
        ({"situacao": "encerrado", "fim": HOJE - timedelta(days=3)}, "programa_encerrado"),
    ],
)
def test_cadastro_que_nao_vale_recusa_com_motivo(mudancas: dict[str, object], motivo: str) -> None:
    recusa = programas.recusa_do_cadastro(programa(**mudancas))
    assert recusa is not None
    assert recusa.motivo == motivo
    assert "Nada foi enviado." in recusa.mensagem


def test_sem_cadastro_recusa() -> None:
    recusa = programas.recusa_do_cadastro(None)
    assert recusa is not None
    assert recusa.motivo == "programa_sem_cadastro"
    assert "Fale com o ADMIN" in recusa.mensagem


@pytest.mark.parametrize(
    ("tipo_app", "tipo_chave", "pode"),
    [
        ("programa", "programa", True),
        ("programa", "por_pessoa", False),
        ("programa", "compartilhada", False),
        ("assistente", "por_pessoa", True),
        ("assistente", "compartilhada", True),
        ("assistente", "programa", False),
    ],
)
def test_programa_so_usa_chave_propria_e_pessoa_nao_usa_chave_de_programa(
    tipo_app: str, tipo_chave: str, pode: bool
) -> None:
    assert programas.chave_permitida(tipo_app, tipo_chave) is pode
    recusa = programas.recusa_da_chave(tipo_app, tipo_chave)
    assert (recusa is None) is pode


def test_recusa_da_chave_diz_o_motivo() -> None:
    recusa = programas.recusa_da_chave("programa", "por_pessoa")
    assert recusa is not None
    assert recusa.motivo == "programa_sem_chave_propria"
    assert "uma chave por pessoa" in recusa.mensagem
    recusa = programas.recusa_da_chave("assistente", "programa")
    assert recusa is not None
    assert recusa.motivo == "chave_de_programa"


def test_filtrar_vitrine_tira_sistema_de_outro_tipo_de_chave() -> None:
    def ferramenta(nome: str, tipo_chave: str) -> SimpleNamespace:
        return SimpleNamespace(nome=nome, sistema=SimpleNamespace(tipo_chave=tipo_chave))

    lista = [ferramenta("a", "programa"), ferramenta("b", "por_pessoa"), ferramenta("c", "compartilhada")]
    assert [f.nome for f in programas.filtrar_vitrine("programa", lista)] == ["a"]
    assert [f.nome for f in programas.filtrar_vitrine("assistente", lista)] == ["b", "c"]


def test_limite_por_minuto_nunca_afrouxa_o_padrao() -> None:
    p = Parametros(APP_ENV="teste", PROGRAM_MAX_REQUESTS_PER_MINUTE=30)
    assert programas.limite_por_minuto(p) == 30
    assert programas.limite_por_minuto(p, 10) == 10
    assert programas.limite_por_minuto(p, 500) == 30
    assert programas.limite_por_minuto(p, 0) == 1


def test_campos_do_programa_para_o_extrato() -> None:
    assert programa().campos() == {"programa": "Robo", "responsavel": "Rui"}


FRASES_DOS_PROGRAMAS = {
    "programa_leitura_feita": {"ferramenta": "fila_x", "programa": "Robo", "responsavel": "Rui"},
    "programa_leitura_recusada": {"ferramenta": "fila_x", "programa": "Robo", "responsavel": "Rui", "motivo": "m"},
    "programa_pedido_recusado": {"metodo": "tools/list", "programa": "Robo", "responsavel": "Rui", "motivo": "m"},
    "programa_recusado": {"programa": "Robo", "responsavel": "Rui", "motivo": "programa_pausado"},
    "programa_sem_cadastro": {"aplicativo": "Robo"},
    "admin_programa_pausado": {"programa": "Robo", "responsavel": "Rui", "quem": "Carla", "motivo": "alerta"},
    "admin_programa_retomado": {"programa": "Robo", "responsavel": "Rui", "quem": "Carla"},
    "admin_programa_encerrado": {"programa": "Robo", "responsavel": "Rui", "quem": "Carla", "motivo": "fim"},
    "admin_programa_responsaveis": {"programa": "Robo", "quem": "Carla", "responsavel": "Rui", "substituto": "Sara"},
    "admin_programa_chave_trocada": {"programa": "Robo", "responsavel": "Rui", "quem": "Carla"},
    "admin_programa_ordem": {"programa": "Robo", "responsavel": "Rui", "quem": "Carla", "modelo": "m", "fim": "1/1"},
    "programa_recertificado": {"programa": "Robo", "responsavel": "Rui", "quem": "Rui"},
    "programa_pausado_sem_uso": {"programa": "Robo", "responsavel": "Rui", "dias": 30},
    "programa_pausado_revisao": {"programa": "Robo", "responsavel": "Rui", "dias": 90},
    "programa_pausado_chave": {"programa": "Robo", "responsavel": "Rui", "dias": 90},
    "programa_pausado_responsavel": {"programa": "Robo", "responsavel": "Rui"},
    "programa_chave_a_vencer": {"programa": "Robo", "responsavel": "Rui", "fim": "01/12/2026"},
    "programa_encerrado_fim": {"programa": "Robo", "responsavel": "Rui", "fim": "18/11/2026"},
    "programa_das_portas_mantido": {
        "programa": "Robo",
        "responsavel": "Rui",
        "acao": "pausou",
        "motivo": "a revisao periodica, a cada 90 dias, venceu sem confirmacao",
    },
    "admin_programa_recadastrado": {
        "programa": "Robo",
        "responsavel": "Rui",
        "quem": "Carla",
        "substituto": "Sara",
        "fim": "01/06/2027",
    },
    "admin_programa_fim_estendido": {
        "programa": "Robo",
        "responsavel": "Rui",
        "quem": "Carla",
        "antes": "01/12/2026",
        "fim": "01/06/2027",
    },
    "admin_programa_aplicativo_reativado": {"programa": "Robo", "responsavel": "Rui", "quem": "Carla"},
    "programa_acao_recusada": {"ferramenta": "fechar_x", "programa": "Robo", "responsavel": "Rui", "motivo": "m"},
    "programa_acao_pedida": {"ferramenta": "fechar_x", "programa": "Robo", "responsavel": "Rui", "protocolo": "C-1"},
    "programa_acao_repetida": {"programa": "Robo", "responsavel": "Rui", "protocolo": "C-1"},
}


@pytest.mark.parametrize(("tipo", "campos"), FRASES_DOS_PROGRAMAS.items())
def test_frases_dos_programas_montam_e_dizem_programa_e_responsavel(tipo: str, campos: dict[str, object]) -> None:
    assert tipo in FRASES
    texto = frase(tipo, **campos)
    for valor in campos.values():
        assert str(valor) in texto
    if "responsavel" in campos and tipo != "admin_programa_responsaveis":
        assert "programa robo, responsavel rui" in texto.lower()
    for proibida in ("token", "senha", "argumento", "resultado", "e-mail", "cpf"):
        assert proibida not in FRASES[tipo].modelo.lower()


def test_cada_passo_do_extrato_tem_a_versao_de_programa() -> None:
    for base, de_programa in programas.PARA_PROGRAMA.items():
        assert FRASES[de_programa].campos() == FRASES[base].campos() | {"programa", "responsavel"}
