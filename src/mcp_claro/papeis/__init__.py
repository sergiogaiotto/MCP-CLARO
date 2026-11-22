"""Os cinco papeis da mesma imagem: entrada, despachante, rotinas, vigia e visitas (a sala de visitas)."""

from __future__ import annotations

import enum


class Papel(enum.StrEnum):
    ENTRADA = "entrada"
    DESPACHANTE = "despachante"
    ROTINAS = "rotinas"
    VIGIA = "vigia"
    VISITAS = "visitas"
