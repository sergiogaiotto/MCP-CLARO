"""Leitura com medida: mascara campos pessoais antes de o resultado voltar ao aplicativo."""

from __future__ import annotations

import json
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


def _valores_crus(dados: Any, campos: frozenset[str], achados: set[str]) -> None:
    if isinstance(dados, dict):
        for k, v in dados.items():
            if k in campos and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                texto = str(v)
                if len(texto) >= 3:
                    achados.add(texto)
            else:
                _valores_crus(v, campos, achados)
    elif isinstance(dados, list):
        for v in dados:
            _valores_crus(v, campos, achados)


def _mascarar_bloco_texto(texto: str, campos: frozenset[str], crus: set[str]) -> str:
    if campos:
        try:
            dados = json.loads(texto)
        except ValueError:
            dados = None
        if isinstance(dados, (dict, list)):
            texto = json.dumps(mascarar_estrutura(dados, campos), ensure_ascii=False)
    for valor in sorted(crus, key=len, reverse=True):
        texto = texto.replace(valor, mascarar_valor(valor))
    return mascarar_texto(texto)


def aplicar(resultado: dict[str, Any], campos_pessoais: Iterable[str]) -> dict[str, Any]:
    """Leitura com medida, antes de o resultado voltar ao aplicativo.

    - ``structuredContent``: mascara os campos pessoais do cadastro, em qualquer nivel;
    - blocos de texto e recursos embutidos: mascara o JSON pelos mesmos campos, troca os valores crus
      desses campos onde aparecerem e mascara CPF e e-mail em qualquer texto;
    - imagem, audio, blob e link de recurso nao tem medida: sao retirados e o resultado avisa.
    """
    campos = frozenset(campos_pessoais)
    saida = dict(resultado)
    crus: set[str] = set()
    if campos and "structuredContent" in saida:
        _valores_crus(saida["structuredContent"], campos, crus)
        saida["structuredContent"] = mascarar_estrutura(saida["structuredContent"], campos)
    conteudo: list[Any] = []
    retirados = 0
    for bloco in saida.get("content") or []:
        tipo = bloco.get("type") if isinstance(bloco, dict) else None
        if tipo == "text" and isinstance(bloco.get("text"), str):
            conteudo.append({**bloco, "text": _mascarar_bloco_texto(bloco["text"], campos, crus)})
        elif tipo == "resource" and isinstance(bloco.get("resource"), dict) and "text" in bloco["resource"]:
            recurso = dict(bloco["resource"])
            recurso["text"] = _mascarar_bloco_texto(str(recurso["text"]), campos, crus)
            conteudo.append({**bloco, "resource": recurso})
        else:
            retirados += 1
    if retirados:
        conteudo.append(
            {"type": "text", "text": f"{retirados} parte(s) sem texto foram retiradas pela leitura com medida."}
        )
    saida["content"] = conteudo
    return saida
