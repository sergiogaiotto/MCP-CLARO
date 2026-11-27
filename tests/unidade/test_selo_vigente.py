"""``selo/vigente.py`` (desenho 4.4 e 9): o selo lido, o efetivo, o retrato e os motivos legiveis, sem banco.

A parte que le do banco (``ler``, ``ler_todos``, ``detalhes``, ``motivo_no_banco``) esta em
``tests/integracao/test_selo_vigente.py``, com cada motivo de ``selo_vigente()`` provocado de verdade.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from mcp_claro.config.settings import Parametros
from mcp_claro.selo import contrato, vigente
from mcp_claro.selo.vigente import FASE_DESLIGADA, NIVEL_1_SEM_TESTE, Detalhes, SeloVigente, efetivo

TESTE = uuid.UUID("3f0c9a52-6d1e-4c41-9b7a-2f6c1e0d8a44")
# 02:30 UTC de 22/11 ainda e 21/11 em Sao Paulo (UTC-3): a data legivel e a do fuso.
TESTADO = datetime(2026, 11, 22, 2, 30, 15, 123456, tzinfo=UTC)
VALE = TESTADO + timedelta(days=90)


def vigente_de(nivel: int = 2, motivo: str = "vigente", modo: str = "ambiente_de_teste") -> SeloVigente:
    return SeloVigente(nivel, TESTE, TESTADO, VALE, modo, motivo)


# --- O selo lido -----------------------------------------------------------------------------------------------


def test_o_dataclass_cumpre_o_protocolo_do_contrato() -> None:
    lido: contrato.SeloLido = vigente_de()
    assert (lido.nivel, lido.teste_id, lido.motivo) == (2, TESTE, "vigente")
    calcula: contrato.CalculaEfetivo = efetivo
    assert calcula(lido, ligada=True, pessoa_teste=False) is lido


@pytest.mark.parametrize("motivo", [m for m in contrato.MOTIVOS_DO_SELO if m not in ("vigente", "nunca_testado")])
def test_motivos_de_nivel_1_com_teste(motivo: str) -> None:
    selo = vigente_de(1, motivo)
    assert (selo.nivel, selo.motivo) == (1, motivo)


@pytest.mark.parametrize(
    ("campos", "trecho"),
    [
        ((0, TESTE, TESTADO, VALE, "ambiente_de_teste", "vigente"), "fora de 1 a 3"),
        ((4, TESTE, TESTADO, VALE, "ambiente_de_teste", "vigente"), "fora de 1 a 3"),
        ((True, TESTE, TESTADO, VALE, "ambiente_de_teste", "vigente"), "fora de 1 a 3"),
        ((2, TESTE, TESTADO, VALE, "ambiente_de_teste", "vencido"), "so o selo vigente"),
        ((1, TESTE, TESTADO, VALE, "ambiente_de_teste", "vigente"), "so o selo vigente"),
        ((1, None, None, None, None, "outro_motivo"), "vocabulario"),
        ((2, None, None, None, None, "vigente"), "vem sempre de um teste"),
        ((2, TESTE, None, VALE, "ambiente_de_teste", "vigente"), "datas e o modo"),
        ((2, TESTE, TESTADO, VALE, None, "vigente"), "datas e o modo"),
        ((2, TESTE, TESTADO, VALE, "producao", "vigente"), "modo do selo"),
        ((1, TESTE, TESTADO, VALE, "ambiente_de_teste", "nunca_testado"), "nunca_testado nao tem teste"),
        ((1, None, TESTADO, None, None, "nunca_testado"), "sem teste nao tem datas"),
        ((1, None, None, None, None, "reprovado"), "vem sempre de um teste"),
        ((2, TESTE, TESTADO.replace(tzinfo=None), VALE, "ambiente_de_teste", "vigente"), "com fuso"),
        ((2, str(TESTE), TESTADO, VALE, "ambiente_de_teste", "vigente"), "uuid"),
    ],
)
def test_o_construtor_recusa_o_que_o_banco_nunca_devolveria(campos: tuple[Any, ...], trecho: str) -> None:
    with pytest.raises(ValueError, match=trecho):
        SeloVigente(*campos)


def test_padroes() -> None:
    assert SeloVigente(1, None, None, None, None, "nunca_testado") == NIVEL_1_SEM_TESTE
    assert SeloVigente(1, None, None, None, None, "fase_desligada") == FASE_DESLIGADA
    # fase_desligada vale com ou sem teste (a ficha aceita os dois).
    assert vigente_de(1, "fase_desligada").motivo == "fase_desligada"


def test_de_linha_aceita_o_uuid_do_driver_e_o_nivel_smallint() -> None:
    @dataclass
    class Linha:
        nivel: int
        teste_id: Any
        testado_em: datetime | None
        vale_ate: datetime | None
        modo: str | None
        motivo: str

    selo = SeloVigente.de_linha(Linha(3, str(TESTE), TESTADO, VALE, "ferramentas_de_teste", "vigente"))
    assert selo == SeloVigente(3, TESTE, TESTADO, VALE, "ferramentas_de_teste", "vigente")
    assert SeloVigente.de_linha(Linha(1, None, None, None, None, "nunca_testado")) == NIVEL_1_SEM_TESTE


# --- Efetivo ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("nivel", [2, 3])
def test_chave_desligada_pessoa_real_fica_no_nivel_1(nivel: int) -> None:
    selo = efetivo(vigente_de(nivel), ligada=False, pessoa_teste=False)
    assert selo is FASE_DESLIGADA
    assert (selo.nivel, selo.motivo, selo.teste_id, selo.testado_em, selo.vale_ate, selo.modo) == (
        1,
        "fase_desligada",
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("nivel", [2, 3])
def test_chave_desligada_pessoa_de_teste_fica_com_o_vigente(nivel: int) -> None:
    v = vigente_de(nivel)
    assert efetivo(v, ligada=False, pessoa_teste=True) is v


def test_chave_ligada_pelo_construtor_da_o_vigente_a_todos() -> None:
    p = Parametros(
        FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=True, FEATURE_PHASE3_THIRD_PARTY=True, FEATURE_PHASE4_BADGES=True
    )
    v = vigente_de(3)
    for pessoa_teste in (False, True):
        assert efetivo(v, ligada=p.FEATURE_PHASE4_BADGES, pessoa_teste=pessoa_teste) is v
    desligada = Parametros()
    assert efetivo(v, ligada=desligada.FEATURE_PHASE4_BADGES, pessoa_teste=False) is FASE_DESLIGADA


@pytest.mark.parametrize("motivo", [m for m in contrato.MOTIVOS_DO_SELO if m != "vigente"])
@pytest.mark.parametrize(("ligada", "pessoa_teste"), [(True, False), (False, True), (True, True), (False, False)])
def test_efetivo_nunca_sobe_o_nivel(motivo: str, ligada: bool, pessoa_teste: bool) -> None:
    v = NIVEL_1_SEM_TESTE if motivo == "nunca_testado" else vigente_de(1, motivo)
    assert efetivo(v, ligada=ligada, pessoa_teste=pessoa_teste).nivel == 1


@pytest.mark.parametrize("valor", [1, "sim", "ligado", object(), None])
def test_so_true_liga(valor: Any) -> None:
    v = vigente_de(3)
    assert efetivo(v, ligada=valor, pessoa_teste=False) is FASE_DESLIGADA
    assert efetivo(v, ligada=False, pessoa_teste=valor) is FASE_DESLIGADA


def test_efetivo_de_outro_selo_lido_vira_selo_vigente() -> None:
    @dataclass(frozen=True)
    class Outro:
        nivel: int
        teste_id: uuid.UUID | None
        testado_em: datetime | None
        vale_ate: datetime | None
        modo: str | None
        motivo: str

    outro = Outro(2, TESTE, TESTADO, VALE, "ambiente_de_teste", "vigente")
    assert efetivo(outro, ligada=False, pessoa_teste=True) == vigente_de(2)
    with pytest.raises(ValueError, match="so o selo vigente"):  # um selo lido fora da forma nao passa
        efetivo(Outro(3, TESTE, TESTADO, VALE, "ambiente_de_teste", "vencido"), ligada=True, pessoa_teste=True)


# --- Retrato ---------------------------------------------------------------------------------------------------


def test_retrato_tem_exatamente_os_campos_do_contrato_com_microssegundos() -> None:
    r = vigente_de(3).retrato()
    assert tuple(r) == contrato.RETRATO_DO_SELO
    assert r == {
        "nivel": 3,
        "teste_id": str(TESTE),
        "testado_em": "2026-11-22T02:30:15.123456+00:00",
        "vale_ate": "2027-02-20T02:30:15.123456+00:00",
        "modo": "ambiente_de_teste",
        "motivo": "vigente",
    }
    assert json.loads(json.dumps(r)) == r
    assert FASE_DESLIGADA.retrato() == {
        "nivel": 1,
        "teste_id": None,
        "testado_em": None,
        "vale_ate": None,
        "modo": None,
        "motivo": "fase_desligada",
    }


@pytest.mark.parametrize(
    "selo", [vigente_de(3), vigente_de(1, "reprovado"), NIVEL_1_SEM_TESTE, FASE_DESLIGADA], ids=lambda s: s.motivo
)
def test_de_retrato_volta_ao_mesmo_selo(selo: SeloVigente) -> None:
    assert SeloVigente.de_retrato(selo.retrato()) == selo
    assert SeloVigente.de_retrato(json.loads(json.dumps(selo.retrato()))) == selo


def test_retrato_com_outro_fuso_continua_o_mesmo_instante() -> None:
    menos3 = timezone(timedelta(hours=-3))
    selo = SeloVigente(2, TESTE, TESTADO.astimezone(menos3), VALE.astimezone(menos3), "ambiente_de_teste", "vigente")
    volta = SeloVigente.de_retrato(selo.retrato())
    assert volta.testado_em == TESTADO  # mesmo instante
    assert selo.retrato()["testado_em"] == "2026-11-21T23:30:15.123456-03:00"


@pytest.mark.parametrize(
    "mudar",
    [
        {"extra": 1},
        {"nivel": True},
        {"nivel": "2"},
        {"teste_id": 5},
        {"testado_em": 1.5},
        {"motivo": "aprovado"},
    ],
    ids=repr,
)
def test_de_retrato_recusa_o_que_nao_e_retrato(mudar: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"retrato|vocabulario"):
        SeloVigente.de_retrato(vigente_de(2).retrato() | mudar)
    sem = vigente_de(2).retrato()
    del sem["modo"]
    with pytest.raises(ValueError, match="exatamente os campos"):
        SeloVigente.de_retrato(sem)


# --- Motivos legiveis ------------------------------------------------------------------------------------------


def test_os_motivos_legiveis_cobrem_todos_os_motivos_do_retrato() -> None:
    assert set(vigente.MOTIVOS_LEGIVEIS) == set(contrato.MOTIVOS_DO_RETRATO)
    assert set(vigente.RETIRADAS_LEGIVEIS) == set(contrato.MOTIVOS_DE_RETIRADA)
    for texto in [*vigente.MOTIVOS_LEGIVEIS.values(), *vigente.RETIRADAS_LEGIVEIS.values()]:
        baixo = texto.lower()
        for proibida in ("garante", "certificado", "conforme", "aprovado no", "mws", "write-safety"):
            assert proibida not in baixo, texto


@pytest.mark.parametrize("motivo", contrato.MOTIVOS_DO_RETRATO)
def test_todo_motivo_tem_texto_que_termina_com_ponto(motivo: str) -> None:
    if motivo == "vigente":
        selo = vigente_de(2)
    elif motivo in ("nunca_testado", "fase_desligada"):
        selo = SeloVigente(1, None, None, None, None, motivo)
    else:
        selo = vigente_de(1, motivo)
    texto = vigente.motivo_legivel(selo)
    assert texto.endswith(".")
    assert texto[0].isupper()
    assert "{" not in texto


def test_textos_exatos_do_desenho_com_a_data_do_fuso() -> None:
    assert vigente.motivo_legivel(vigente_de(2)) == "Teste de 21/11/2026, vale até 19/02/2027."
    assert vigente.motivo_legivel(vigente_de(2), fuso="UTC") == "Teste de 22/11/2026, vale até 20/02/2027."
    assert vigente.motivo_legivel(vigente_de(1, "vencido")) == "O teste venceu em 19/02/2027."
    assert vigente.motivo_legivel(NIVEL_1_SEM_TESTE) == "Nunca testado."
    assert vigente.motivo_legivel(vigente_de(1, "endereco_mudou")) == (
        "Voltou ao nível 1: o endereço mudou depois do teste."
    )
    assert vigente.motivo_legivel(vigente_de(1, "revisao_nova")) == "Voltou ao nível 1: a revisão do servidor é nova."
    assert vigente.motivo_legivel(vigente_de(1, "fora_de_ativo")) == "Voltou ao nível 1: o sistema saiu de ativo."
    assert vigente.motivo_legivel(vigente_de(1, "contrato_sumiu")) == (
        "Voltou ao nível 1: o sistema não lista mais as ferramentas do contrato."
    )
    assert vigente.selo_agora_legivel(vigente_de(3)) == "Selo agora: nível 3. Teste de 21/11/2026, vale até 19/02/2027."
    assert vigente.selo_agora_legivel(NIVEL_1_SEM_TESTE) == "Selo agora: nível 1. Nunca testado."


def test_detalhes_completam_o_texto() -> None:
    reprovado = vigente_de(1, "reprovado")
    assert vigente.motivo_legivel(reprovado) == "O último teste não foi aprovado."
    assert vigente.motivo_legivel(reprovado, Detalhes(falhas=1)) == "O último teste não foi aprovado: 1 caso falhou."
    assert vigente.motivo_legivel(reprovado, Detalhes(falhas=4)) == (
        "O último teste não foi aprovado: 4 casos falharam."
    )
    nova = vigente_de(1, "ferramenta_nova")
    assert vigente.motivo_legivel(nova, Detalhes(ferramentas=("criar_lembrete",))) == (
        "Voltou ao nível 1: a ferramenta criar_lembrete é nova ou mudou depois do teste."
    )
    assert vigente.motivo_legivel(nova, Detalhes(ferramentas=("a", "b"))) == (
        "Voltou ao nível 1: as ferramentas a, b são novas ou mudaram depois do teste."
    )
    assert vigente.motivo_legivel(nova) == "Voltou ao nível 1: uma ferramenta de ação é nova ou mudou depois do teste."
    retirado = vigente_de(1, "retirado")
    quando = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)  # 30/11 em Sao Paulo
    for motivo, por in vigente.RETIRADAS_LEGIVEIS.items():
        texto = vigente.motivo_legivel(retirado, Detalhes(retirado_em=quando, retirada=motivo))
        assert texto == f"Retirado em 30/11/2026: {por}."
    assert vigente.motivo_legivel(retirado) == "O selo foi retirado."
    # Detalhe de outro motivo nao muda o texto.
    assert vigente.motivo_legivel(vigente_de(2), Detalhes(falhas=3)) == "Teste de 21/11/2026, vale até 19/02/2027."


def test_motivo_desconhecido_e_recusado() -> None:
    @dataclass(frozen=True)
    class Torto:
        nivel: int = 1
        teste_id: uuid.UUID | None = None
        testado_em: datetime | None = None
        vale_ate: datetime | None = None
        modo: str | None = None
        motivo: str = "aprovado_no_rascunho"

    with pytest.raises(ValueError, match="fora do vocabulario"):
        vigente.motivo_legivel(Torto())
