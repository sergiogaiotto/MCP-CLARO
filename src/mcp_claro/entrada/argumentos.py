"""Conferencia dos argumentos contra o esquema revisado (JSON Schema Draft 2020-12).

O servidor de baixo nivel do kit nao confere argumentos. A entrada confere cada
pedido contra o esquema revisado pelo ADMIN, antes das regras e da ficha. Nao
busca referencias externas (``$ref`` de fora): um registro vazio recusa.

Ferramenta de servidor de terceiros (Fase 3): antes de conferir o esquema, a entrada mede os argumentos
(``conferir_tamanho``). A conferencia roda dentro do laco da entrada, e uma regra de formato (``pattern``) do
fornecedor, mesmo aceita na revisao, pode custar o quadrado do tamanho do texto; ``uniqueItems`` custa o quadrado
do tamanho da lista. Por isso, num terceiro, o MCP-CLARO recusa antes, com frase legivel:

- texto com mais de ``TETO_TEXTO`` letras, em qualquer nivel, inclusive nome de campo;
- lista com mais de ``TETO_LISTA`` itens;
- mais de ``TETO_LETRAS`` letras somando todos os textos e nomes de campo, ou mais de ``TETO_PARTES`` partes
  (cada valor, lista ou objeto conta uma): sem isso, muitos textos ou listas no limite, juntos, ainda travariam a
  entrada.

A propria medida tem custo preso aos tetos (memoria e tempo), qualquer que seja o formato dos argumentos: as partes
sao contadas antes de entrar na pilha, e o caminho de cada valor e uma ligacao ao pai, montado so na frase.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

_REGISTRO_VAZIO: Registry[Any] = Registry()


class EsquemaInvalido(ValueError):
    pass


class ArgumentosInvalidos(ValueError):
    def __init__(self, problemas: list[str]) -> None:
        self.problemas = problemas
        super().__init__("; ".join(problemas))


class ArgumentosGrandes(ArgumentosInvalidos):
    """Argumentos acima dos tetos de tamanho para servidor de terceiros (``conferir_tamanho``)."""


TETO_TEXTO: Final = 10_000
"""Letras de um texto (valor ou nome de campo), num argumento para servidor de terceiros."""
TETO_LISTA: Final = 1_000
"""Itens de uma lista, num argumento para servidor de terceiros."""
TETO_LETRAS: Final = 20_000
"""Letras somando todos os textos e nomes de campo dos argumentos para servidor de terceiros."""
TETO_PARTES: Final = 5_000
"""Partes (cada valor, lista ou objeto) dos argumentos para servidor de terceiros."""
_TETO_CAMINHO: Final = 40
"""Letras de cada pedaco do caminho (nome de campo) na frase da recusa."""
_PEDACOS_NO_CAMINHO: Final = 10
"""Pedacos do caminho na frase da recusa: o primeiro, ``...`` e os ultimos."""

type _No = tuple[_No, str] | None
"""Caminho ate um valor como ligacao ao pai: (caminho do pai, pedaco). Cada item da pilha guarda so uma ligacao,
nunca uma copia do caminho inteiro; o texto so e montado quando ha problema."""


def _parte_do_caminho(chave: object) -> str:
    texto = str(chave)
    return texto if len(texto) <= _TETO_CAMINHO else texto[:_TETO_CAMINHO] + "..."


def _caminho(no: _No) -> str:
    pedacos: list[str] = []
    while no is not None:
        no, pedaco = no
        pedacos.append(pedaco)
    pedacos.reverse()
    if len(pedacos) > _PEDACOS_NO_CAMINHO:
        pedacos = [pedacos[0], "...", *pedacos[2 - _PEDACOS_NO_CAMINHO :]]
    return "/".join(pedacos) or "(raiz)"


def conferir_tamanho(argumentos: Mapping[str, Any] | None) -> None:
    """Levanta ``ArgumentosGrandes`` com ate cinco problemas legiveis, sem repetir os valores.

    Anda pelos argumentos sem recursao (uma pilha), entao nem o aninhamento fundo derruba a conferencia. O custo
    fica preso aos tetos, qualquer que seja o formato: as partes de uma lista ou de um objeto sao contadas antes de
    entrar na pilha (a pilha nunca passa de ``TETO_PARTES`` itens), e cada item guarda so a ligacao ao caminho do
    pai."""
    problemas: list[str] = []
    letras = 0
    partes = 1  # a raiz; cada lista ou objeto soma as partes dele antes de empilhar
    pilha: list[tuple[Any, _No]] = [(dict(argumentos or {}), None)]
    muitas_partes = f"(raiz): mais de {TETO_PARTES} partes somando todos os valores"
    while pilha and len(problemas) < 5:
        valor, no = pilha.pop()
        if isinstance(valor, str):
            letras += len(valor)
            if len(valor) > TETO_TEXTO:
                problemas.append(f"{_caminho(no)}: texto com mais de {TETO_TEXTO} letras")
        elif isinstance(valor, Mapping | list | tuple):
            if not isinstance(valor, Mapping) and len(valor) > TETO_LISTA:
                # Os itens nem sao olhados: a lista ja e recusada.
                problemas.append(f"{_caminho(no)}: lista com mais de {TETO_LISTA} itens")
                continue
            partes += len(valor)
            if partes > TETO_PARTES:
                problemas.append(muitas_partes)
                break
            if isinstance(valor, Mapping):
                filhos: list[tuple[Any, _No]] = []
                for chave, item in valor.items():
                    nome = str(chave)
                    letras += len(nome)
                    if len(nome) > TETO_TEXTO and len(problemas) < 5:
                        problemas.append(f"{_caminho(no)}: nome de campo com mais de {TETO_TEXTO} letras")
                    filhos.append((item, (no, _parte_do_caminho(nome))))
                pilha.extend(reversed(filhos))
            else:
                pilha.extend((valor[n], (no, str(n))) for n in reversed(range(len(valor))))
        if letras > TETO_LETRAS:
            problemas.append(f"(raiz): mais de {TETO_LETRAS} letras somando todos os textos")
            break
    if problemas:
        raise ArgumentosGrandes(problemas[:5])


def conferir_esquema(esquema: Mapping[str, Any]) -> None:
    """O esquema revisado precisa ser valido e de objeto (exigencia da 2026-07-28)."""
    if esquema.get("type") != "object":
        raise EsquemaInvalido('o esquema de entrada precisa ter "type": "object"')
    try:
        Draft202012Validator.check_schema(esquema)
    except SchemaError as erro:
        raise EsquemaInvalido(f"esquema invalido: {erro.message}") from erro


@lru_cache(maxsize=1024)
def _validador(esquema_json: str) -> Draft202012Validator:
    esquema = json.loads(esquema_json)
    conferir_esquema(esquema)
    return Draft202012Validator(esquema, registry=_REGISTRO_VAZIO)


def conferir_argumentos(esquema: Mapping[str, Any], argumentos: Mapping[str, Any] | None) -> None:
    """Levanta ``ArgumentosInvalidos`` com ate cinco problemas legiveis, sem repetir os valores."""
    validador = _validador(json.dumps(esquema, sort_keys=True))
    try:
        erros = sorted(validador.iter_errors(dict(argumentos or {})), key=lambda e: list(e.absolute_path))
    except Unresolvable as erro:
        raise ArgumentosInvalidos(["o esquema cita uma referencia externa, que nao e buscada"]) from erro
    if erros:
        problemas = []
        for falha in erros[:5]:
            caminho = "/".join(str(p) for p in falha.absolute_path) or "(raiz)"
            problemas.append(f"{caminho}: regra '{falha.validator}' nao atendida")
        raise ArgumentosInvalidos(problemas)
