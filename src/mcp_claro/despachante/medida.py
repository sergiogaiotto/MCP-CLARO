"""Leitura com medida: mascara campos pessoais antes de o resultado voltar ao aplicativo.

Sem recursao: a mascara e a busca dos valores crus andam com uma pilha, entao um JSON muito fundo (dentro de um
bloco de texto, por exemplo) nao derruba o despachante. O que nem o leitor de JSON consegue abrir levanta
``RespostaFundaDemais``, um erro nosso que quem chama transforma em recusa legivel.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Final

_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

_CHAVES_DO_RECURSO: Final = ("uri", "mimeType", "text")
"""Do recurso embutido com texto, so estas chaves passam: o ``_meta`` do recurso nunca passa (Fase 3)."""


class RespostaFundaDemais(ValueError):
    """O resultado tem partes demais umas dentro das outras para a leitura com medida."""


def profundidade(dados: Any) -> int:
    """Quantos niveis de listas e objetos, um dentro do outro (0 para um valor simples). Sem recursao."""
    maior = 0
    pilha: list[tuple[Any, int]] = [(dados, 1)]
    while pilha:
        valor, nivel = pilha.pop()
        if isinstance(valor, dict):
            filhos: Iterable[Any] = valor.values()
        elif isinstance(valor, list):
            filhos = valor
        else:
            continue
        maior = max(maior, nivel)
        pilha.extend((filho, nivel + 1) for filho in filhos if isinstance(filho, (dict, list)))
    return maior


def mascarar_valor(valor: Any) -> Any:
    if isinstance(valor, str):
        if len(valor) <= 4:
            return "***"
        return "***" + valor[-2:]
    if isinstance(valor, (int, float)):
        return "***"
    return "***"


def _vazio_como(valor: dict[str, Any] | list[Any]) -> dict[str, Any] | list[Any]:
    return {} if isinstance(valor, dict) else []


def mascarar_estrutura(dados: Any, campos: frozenset[str]) -> Any:
    """Copia ``dados`` com o valor de cada campo pessoal mascarado, em qualquer nivel (sem recursao)."""
    if not isinstance(dados, (dict, list)):
        return dados
    raiz = _vazio_como(dados)
    pilha: list[tuple[dict[str, Any] | list[Any], dict[str, Any] | list[Any]]] = [(dados, raiz)]
    while pilha:
        origem, destino = pilha.pop()
        if isinstance(origem, dict) and isinstance(destino, dict):
            for k, v in origem.items():
                if k in campos:
                    destino[k] = mascarar_valor(v)
                elif isinstance(v, (dict, list)):
                    destino[k] = novo = _vazio_como(v)
                    pilha.append((v, novo))
                else:
                    destino[k] = v
        elif isinstance(origem, list) and isinstance(destino, list):
            for v in origem:
                if isinstance(v, (dict, list)):
                    novo = _vazio_como(v)
                    destino.append(novo)
                    pilha.append((v, novo))
                else:
                    destino.append(v)
    return raiz


def mascarar_texto(texto: str) -> str:
    texto = _CPF.sub("***.***.***-**", texto)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", texto)


def _valores_crus(dados: Any, campos: frozenset[str], achados: set[str]) -> None:
    pilha: list[Any] = [dados]
    while pilha:
        valor = pilha.pop()
        if isinstance(valor, dict):
            for k, v in valor.items():
                if k in campos and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    texto = str(v)
                    if len(texto) >= 3:
                        achados.add(texto)
                else:
                    pilha.append(v)
        elif isinstance(valor, list):
            pilha.extend(valor)


def _mascarar_bloco_texto(texto: str, campos: frozenset[str], crus: set[str]) -> str:
    if campos:
        try:
            dados = json.loads(texto)
        except RecursionError:
            # JSON fundo demais ate para o leitor: sem abrir, os campos pessoais de dentro nao seriam mascarados.
            raise RespostaFundaDemais("texto com JSON fundo demais para a leitura com medida") from None
        except ValueError:
            dados = None
        if isinstance(dados, (dict, list)):
            try:
                texto = json.dumps(mascarar_estrutura(dados, campos), ensure_ascii=False)
            except RecursionError:
                raise RespostaFundaDemais("texto com JSON fundo demais para a leitura com medida") from None
    for valor in sorted(crus, key=len, reverse=True):
        texto = texto.replace(valor, mascarar_valor(valor))
    return mascarar_texto(texto)


def aplicar(resultado: dict[str, Any], campos_pessoais: Iterable[str], *, anotacoes: bool = True) -> dict[str, Any]:
    """Leitura com medida, antes de o resultado voltar ao aplicativo.

    - ``structuredContent``: mascara os campos pessoais do cadastro, em qualquer nivel;
    - blocos de texto e recursos embutidos: mascara o JSON pelos mesmos campos, troca os valores crus
      desses campos onde aparecerem e mascara CPF e e-mail em qualquer texto;
    - imagem, audio, blob e link de recurso nao tem medida: sao retirados e o resultado avisa, no ultimo bloco,
      so com ``type`` e ``text``.

    Cada bloco que fica sai so com o que e dele: ``type``, o texto (ou o recurso com ``uri``, ``mimeType`` e
    ``text``) e, com ``anotacoes``, as ``annotations``. O ``_meta`` do bloco e o do recurso embutido nunca passam
    (Fase 3: podiam levar dado pessoal ou instrucao do sistema). ``anotacoes=False`` (servidor de terceiros) tira
    tambem as ``annotations``.

    Levanta ``RespostaFundaDemais`` quando um texto JSON, com campos pessoais a mascarar, e fundo demais ate para
    o leitor de JSON.
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
        novo: dict[str, Any]
        if tipo == "text" and isinstance(bloco.get("text"), str):
            novo = {"type": "text", "text": _mascarar_bloco_texto(bloco["text"], campos, crus)}
        elif tipo == "resource" and isinstance(bloco.get("resource"), dict) and "text" in bloco["resource"]:
            recurso = {k: bloco["resource"][k] for k in _CHAVES_DO_RECURSO if k in bloco["resource"]}
            recurso["text"] = _mascarar_bloco_texto(str(recurso["text"]), campos, crus)
            novo = {"type": "resource", "resource": recurso}
        else:
            retirados += 1
            continue
        if anotacoes and "annotations" in bloco:
            novo["annotations"] = bloco["annotations"]
        conteudo.append(novo)
    if retirados:
        conteudo.append(
            {"type": "text", "text": f"{retirados} parte(s) sem texto foram retiradas pela leitura com medida."}
        )
    saida["content"] = conteudo
    return saida
