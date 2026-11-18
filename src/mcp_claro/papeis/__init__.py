"""Os quatro papeis da mesma imagem: entrada, despachante, rotinas e vigia."""

from __future__ import annotations

import enum


class Papel(enum.StrEnum):
    ENTRADA = "entrada"
    DESPACHANTE = "despachante"
    ROTINAS = "rotinas"
    VIGIA = "vigia"
