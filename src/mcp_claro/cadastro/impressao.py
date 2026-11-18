"""Impressao digital das ferramentas, base do mudou-parou.

Calculo proprio, so com a biblioteca padrao. Inclui nome, descricao, esquemas
de entrada e de saida e etiquetas: qualquer mudanca nesses campos muda a
impressao. (A receita do FastMCP deixa a descricao de fora e nao serve aqui.)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

CAMPOS: tuple[str, ...] = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")


def canonico(definicao: Mapping[str, Any]) -> str:
    recorte = {c: definicao.get(c) for c in CAMPOS}
    return json.dumps(recorte, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def impressao(definicao: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` da forma canonica da definicao declarada pelo sistema."""
    return "sha256:" + hashlib.sha256(canonico(definicao).encode("utf-8")).hexdigest()


def pede_mais(antes: Mapping[str, Any], depois: Mapping[str, Any]) -> bool:
    """Diz se a nova definicao pede mais da pessoa: campo novo, obrigatorio novo ou classe mais forte."""
    e_antes = antes.get("inputSchema") or {}
    e_depois = depois.get("inputSchema") or {}
    props_antes = set((e_antes.get("properties") or {}).keys())
    props_depois = set((e_depois.get("properties") or {}).keys())
    obrig_antes = set(e_antes.get("required") or [])
    obrig_depois = set(e_depois.get("required") or [])
    a_antes = antes.get("annotations") or {}
    a_depois = depois.get("annotations") or {}
    ficou_mais_forte = bool(a_antes.get("readOnlyHint")) and not a_depois.get("readOnlyHint")
    ficou_destrutiva = not a_antes.get("destructiveHint") and bool(a_depois.get("destructiveHint"))
    return bool(props_depois - props_antes) or bool(obrig_depois - obrig_antes) or ficou_mais_forte or ficou_destrutiva
