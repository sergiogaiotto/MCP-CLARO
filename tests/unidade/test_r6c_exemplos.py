"""R6c-6: o exemplo do ensaio mal-intencionado tem numero e porta padrao proprios.

Antes ele dizia "Exemplo 6" e subia na 9106, como ``exemplos/duble_fase2.py``, que o ``docs/simulado-dia-ruim.md``
manda subir na 9106: os dois rodados a mao com a porta padrao colidiam.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
EXEMPLOS = RAIZ / "exemplos"
ENSAIO = EXEMPLOS / "ensaio_mal_intencionado.py"


def _porta(arquivo: Path) -> int | None:
    achado = re.search(r"^\s+rodar\(criar, (\d+)\)$", arquivo.read_text(encoding="utf-8"), re.MULTILINE)
    return int(achado.group(1)) if achado else None


def _numero(arquivo: Path) -> int | None:
    achado = re.match(r'"""Exemplo (\d+):', arquivo.read_text(encoding="utf-8"))
    return int(achado.group(1)) if achado else None


def test_ensaio_mal_intencionado_tem_porta_e_numero_que_nenhum_outro_exemplo_usa() -> None:
    outros = [a for a in sorted(EXEMPLOS.glob("*.py")) if a != ENSAIO]
    porta, numero = _porta(ENSAIO), _numero(ENSAIO)
    assert (porta, numero) == (9108, 7)
    assert [a.name for a in outros if _porta(a) == porta] == []
    assert [a.name for a in outros if _numero(a) == numero] == []
