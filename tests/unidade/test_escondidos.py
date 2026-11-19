"""regras/escondidos.py: a marca dos caracteres escondidos, trazida de telas/acoes.py (critica M6).

Os mesmos casos dos testes da Fase 2 (test_telas_revisao.py e test_telas_rodada2.py), agora contra o modulo de
regras, e a prova de que as duas copias dizem o mesmo em todo o Unicode enquanto as telas nao importam daqui.
"""

from __future__ import annotations

import pytest

from mcp_claro.regras import escondidos
from mcp_claro.telas import acoes


@pytest.mark.parametrize(
    ("bruto", "marcado"),
    [
        ("12345\u202e6789", "12345[U+202E]6789"),
        ("\u2066conta\u2069", "[U+2066]conta[U+2069]"),
        ("equipe\u200b", "equipe[U+200B]"),
        ("a\u200db\u200ec\u200f", "a[U+200D]b[U+200E]c[U+200F]"),
        ("tab\taqui\r", "tab[U+0009]aqui[U+000D]"),
        ("linha\u2028outra\u2029", "linha[U+2028]outra[U+2029]"),
        ("hi\u00adfen", "hi[U+00AD]fen"),
        ("vazio\u3164", "vazio[U+3164]"),
        ("duas\nlinhas", "duas\nlinhas"),
        ("Acentuação normal: ç, ã, é", "Acentuação normal: ç, ã, é"),
    ],
)
def test_escondidos_viram_marca_visivel(bruto: str, marcado: str) -> None:
    assert escondidos.marcar_escondidos(bruto) == marcado
    assert any(escondidos.escondido(ch) for ch in bruto) is (bruto != marcado)


@pytest.mark.parametrize(
    "ponto",
    [0x2800, 0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x180B, 0x180F, 0x2060, 0x2064, 0xFEFF, 0xE0041],
)
def test_escondidos_que_faltavam_viram_marca(ponto: int) -> None:
    assert escondidos.marcar_escondidos(f"con{chr(ponto)}ta") == f"con[U+{ponto:04X}]ta"
    assert escondidos.escondido(chr(ponto))


def test_letras_de_verdade_ficam_como_estao() -> None:
    for texto in ("한국어", "ខ្មែរ", "Ação ç ã é", "ﾊﾝｶｸ", "⠁⠃", "a b"):
        assert escondidos.marcar_escondidos(texto) == texto
        assert not any(escondidos.escondido(ch) for ch in texto)


def test_quebra_de_linha_fica_e_as_outras_de_controle_nao() -> None:
    assert not escondidos.escondido("\n")
    assert escondidos.escondido("\r")
    assert escondidos.escondido("\x00")
    assert escondidos.marcar_escondidos("") == ""


def test_mesmo_veredito_das_telas_em_todo_o_unicode() -> None:
    # Ate o G6 trocar telas/acoes.py para importar daqui, as duas copias precisam dizer o mesmo para cada
    # caractere; depois da troca, o teste continua valendo (e o mesmo codigo).
    diferentes = [p for p in range(0x110000) if escondidos.escondido(chr(p)) != acoes.escondido(chr(p))]
    assert diferentes == []
    assert escondidos.ESCONDIDOS_DITOS == acoes.ESCONDIDOS_DITOS
    assert escondidos.CATEGORIAS_ESCONDIDAS == acoes.CATEGORIAS_ESCONDIDAS
