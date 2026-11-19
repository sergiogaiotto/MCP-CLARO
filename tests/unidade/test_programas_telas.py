"""Telas e rotinas dos programas, sem banco: regras do cadastro, prazos e textos dos avisos."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from mcp_claro.config.settings import Parametros
from mcp_claro.regras.programas import Programa
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.programas import PASSOS, ProgramaMudou, tarefas_dos_programas
from mcp_claro.telas import admin_programas
from mcp_claro.telas.admin_programas import (
    MESMA_PESSOA,
    SEM_RESPONSAVEL,
    Prazos,
    conferir_fim,
    conferir_responsaveis,
    recusa_para_retomar,
)

HOJE = date(2026, 11, 19)
AGORA = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
P = Parametros(
    APP_ENV="teste",
    PROGRAM_OWNER_RECERT_DAYS=90,
    PROGRAM_KEY_MAX_AGE_DAYS=90,
    PROGRAM_IDLE_PAUSE_DAYS=30,
)


def programa(**mudancas: object) -> Programa:
    base = Programa(
        aplicativo_id=uuid.uuid4(),
        client_id="robo",
        nome="Robo",
        finalidade="Le as filas.",
        situacao="pausado",
        fim=HOJE + timedelta(days=10),
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


def test_responsavel_e_substituto_obrigatorios_e_diferentes() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert conferir_responsaveis(None, None) == [SEM_RESPONSAVEL]
    assert conferir_responsaveis(a, None) == [SEM_RESPONSAVEL]
    assert conferir_responsaveis(None, b) == [SEM_RESPONSAVEL]
    assert conferir_responsaveis(a, a) == [MESMA_PESSOA]
    assert conferir_responsaveis(a, b) == []


@pytest.mark.parametrize(
    ("valor", "vale"),
    [
        ("2026-11-19", False),  # hoje nao serve: precisa ser depois de hoje
        ("2026-11-20", True),
        ("2027-11-20", True),  # 366 dias
        ("2027-11-21", False),
        ("19/11/2027", False),
        ("", False),
    ],
)
def test_data_de_fim_depois_de_hoje_e_em_ate_um_ano(valor: str, vale: bool) -> None:
    fim, erros = conferir_fim(valor, HOJE)
    assert (fim is not None) is vale
    assert (erros == []) is vale


def test_prazos_contam_da_revisao_da_chave_e_do_ultimo_pedido() -> None:
    pr = programa(visto_em=AGORA - timedelta(days=2))
    prazos = Prazos.de(pr, P, AGORA)
    assert prazos.revisao_vence == pr.revisado_em + timedelta(days=90)
    assert prazos.chave_vence == pr.chave_trocada_em + timedelta(days=90)
    assert prazos.pausa_sem_uso == AGORA - timedelta(days=2) + timedelta(days=30)
    assert not prazos.revisao_vencida
    assert not prazos.chave_vencida
    # Sem pedido ainda, conta da revisao (a mais nova entre revisao e criacao).
    assert Prazos.de(programa(), P, AGORA).pausa_sem_uso == AGORA - timedelta(days=10) + timedelta(days=30)
    vencido = Prazos.de(programa(revisado_em=AGORA - timedelta(days=91)), P, AGORA)
    assert vencido.revisao_vencida


def test_retomar_so_pausado_com_fim_responsaveis_e_chave_em_dia() -> None:
    assert recusa_para_retomar(programa(), Prazos.de(programa(), P, AGORA)) == []
    assert recusa_para_retomar(programa(situacao="ativo"), Prazos.de(programa(), P, AGORA))
    assert recusa_para_retomar(programa(situacao="encerrado"), Prazos.de(programa(), P, AGORA))
    vencido = programa(fim=HOJE - timedelta(days=1))
    assert "data de fim" in recusa_para_retomar(vencido, Prazos.de(vencido, P, AGORA))[0]
    sem_substituto = programa(substituto_ativo=False)
    assert "substituto" in recusa_para_retomar(sem_substituto, Prazos.de(sem_substituto, P, AGORA))[0]
    chave_velha = programa(chave_trocada_em=AGORA - timedelta(days=91))
    assert "chave" in recusa_para_retomar(chave_velha, Prazos.de(chave_velha, P, AGORA))[0]


def test_passos_da_rotina_em_ordem_e_com_texto_para_quem_nao_e_tecnico() -> None:
    assert [p.nome for p in PASSOS] == ["encerrados", "sem_responsavel", "revisao_vencida", "chave_vencida", "sem_uso"]
    m = ProgramaMudou(
        aplicativo_id=uuid.uuid4(),
        responsavel_id=uuid.uuid4(),
        substituto_id=uuid.uuid4(),
        programa="Robo",
        responsavel="Rui",
        fim=HOJE,
        revisado_em=AGORA,
        chave_trocada_em=AGORA,
        visto_em=None,
    )
    textos = {p.nome: p.texto(m, 30) for p in PASSOS}
    assert textos["encerrados"].startswith(
        "O programa Robo foi encerrado no MCP-CLARO: o cadastro dele valia ate 19/11"
    )
    assert "ficou 30 dias sem fazer nenhum pedido" in textos["sem_uso"]
    assert all("ADMIN" in t for t in textos.values())
    for t in textos.values():
        for proibida in ("token", "senha", "garantido", "100%"):
            assert proibida not in t.lower()


def test_tarefa_dos_programas_no_formato_do_agendador() -> None:
    cfg = ConfigRotinas(intervalo_curto=120.0, prazo_tarefa=60.0)
    tarefas = tarefas_dos_programas(None, P, cfg)  # type: ignore[arg-type]
    assert [(t.nome, t.intervalo, t.prazo) for t in tarefas] == [("programas", 120.0, 60.0)]


def test_todo_aviso_destas_telas_existe() -> None:
    texto = Path(admin_programas.__file__).read_text(encoding="utf-8")
    usados = set(re.findall(r"feito=\"([a-z_]+)\"", texto))
    for linha in texto.splitlines():
        if "voltar(" in linha:
            usados |= set(re.findall(r"\"([a-z_]+)\"", linha))
    assert {"programa_pausado", "programa_retomado", "programa_nada_mudou", "programa_recertificado"} <= usados
    assert usados <= set(admin_programas.FEITOS)
