"""Leitura com medida: mascara campos pessoais antes de o resultado voltar ao aplicativo."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def mascarar_valor(valor: Any) -> Any:
    if isinstance(valor, str):
        if len(valor) <= 4:
            return "***"
        return "***" + valor[-2:]
    if isinstance(valor, (int, float)):
        return "***"
    return "***"


def mascarar_estrutura(dados: Any, campos: frozenset[str]) -> Any:
    if isinstance(dados, dict):
        return {k: (mascarar_valor(v) if k in campos else mascarar_estrutura(v, campos)) for k, v in dados.items()}
    if isinstance(dados, list):
        return [mascarar_estrutura(v, campos) for v in dados]
    return dados


def mascarar_texto(texto: str) -> str:
    texto = _CPF.sub("***.***.***-**", texto)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", texto)


def aplicar(resultado: dict[str, Any], campos_pessoais: Iterable[str]) -> dict[str, Any]:
    """Mascara ``structuredContent`` pelos campos do cadastro e CPF/e-mail em qualquer texto."""
    campos = frozenset(campos_pessoais)
    saida = dict(resultado)
    if campos and "structuredContent" in saida:
        saida["structuredContent"] = mascarar_estrutura(saida["structuredContent"], campos)
    conteudo = []
    for bloco in saida.get("content") or []:
        if isinstance(bloco, dict) and bloco.get("type") == "text" and isinstance(bloco.get("text"), str):
            bloco = {**bloco, "text": mascarar_texto(bloco["text"])}
        conteudo.append(bloco)
    saida["content"] = conteudo
    return saida
