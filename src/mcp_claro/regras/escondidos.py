"""Caracteres escondidos: os que nao aparecem na tela ou mudam a ordem do texto.

O escape do HTML nao mexe neles, e o navegador e o modelo os obedecem (U+202E inverte o que vem depois). Onde um
texto de fora aparece (tela do ADMIN, ficha, resposta emoldurada de estranhos), cada um vira uma marca visivel
como ``[U+202E]``, no lugar exato onde estava.

Copia fiel de ``telas/acoes.py`` (Fase 2), trazida para ``regras`` porque ``regras/estranhos.py`` precisa dela e
``regras`` nao importa ``telas`` (o despachante carregaria as telas). As telas passam a importar daqui.
"""

from __future__ import annotations

import unicodedata
from typing import Final

CATEGORIAS_ESCONDIDAS: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})
# Os de direcao (bidi) e de largura zero, ditos um a um (ja sao Cf, ficam aqui por clareza): U+061C, U+200B a
# U+200F, U+202A a U+202E, U+2066 a U+2069. E os que o Unicode chama de letra, marca ou simbolo, mas nao desenham
# nada ou desenham um branco: os "ignoraveis" do Unicode (preenchedores do hangul U+115F, U+1160, U+3164 e
# U+FFA0; U+034F, que junta letras; U+17B4 e U+17B5, vogais do khmer que nao aparecem; seletores de variacao,
# inclusive os do mongol, U+180B a U+180F) e U+2800, o braille em branco.
ESCONDIDOS_DITOS: Final = frozenset(
    {
        0x034F,
        0x061C,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        *range(0x180B, 0x1810),
        *range(0x200B, 0x2010),
        *range(0x202A, 0x202F),
        *range(0x2060, 0x2070),
        0x2800,
        0x3164,
        *range(0xFE00, 0xFE10),
        0xFEFF,
        0xFFA0,
        *range(0xE0000, 0xE1000),
    }
)


def escondido(ch: str) -> bool:
    """A quebra de linha fica: a tela a mostra como quebra (``white-space: pre-wrap``)."""
    return ch != "\n" and (ord(ch) in ESCONDIDOS_DITOS or unicodedata.category(ch) in CATEGORIAS_ESCONDIDAS)


def marcar_escondidos(texto: str) -> str:
    """Troca cada caractere escondido por uma marca visivel, no lugar exato onde ele esta: "12[U+202E]34"."""
    return "".join(f"[U+{ord(ch):04X}]" if escondido(ch) else ch for ch in texto)
