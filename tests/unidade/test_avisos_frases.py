"""Frases-padrao novas das rotinas e dos avisos."""

from __future__ import annotations

from mcp_claro.extrato.frases import FRASES, frase

NOVAS = {
    "ordem_vencida": {"modelo": "leitura-faturas"},
    "ordem_inativa": {"modelo": "leitura-faturas", "dias": 14},
    "ordem_a_vencer": {"modelo": "leitura-faturas", "fim": "25/11/2026"},
    "ferramenta_suspensa": {"ferramenta": "fatura_x"},
    "ferramenta_mudou": {"ferramenta": "fatura_x"},
    "aviso_desistiu": {"evento": "ordem_vencida", "tentativas": 6},
    "resumo_semanal": {"semana": "2026-W46"},
    "alerta_grafana": {"alerta": "Recusas por versao", "situacao": "disparou"},
}


def test_frases_novas_montam_com_os_campos_exatos() -> None:
    for tipo, campos in NOVAS.items():
        assert tipo in FRASES, tipo
        texto = frase(tipo, **campos)
        for valor in campos.values():
            assert str(valor) in texto


def test_mudou_parou_aparece_na_frase() -> None:
    assert "mudou, parou" in frase("ferramenta_suspensa", ferramenta="x")
    assert "so registramos" in frase("ferramenta_mudou", ferramenta="x")


def test_frases_novas_sem_segredo_nem_conteudo() -> None:
    for tipo in NOVAS:
        modelo = FRASES[tipo].modelo.lower()
        for proibida in ("token", "senha", "argumento", "resultado", "e-mail", "cpf"):
            assert proibida not in modelo, (tipo, proibida)
