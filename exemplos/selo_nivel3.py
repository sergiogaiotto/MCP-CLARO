"""Exemplo 9: sistema que adotou o contrato do selo (``selo/1``) no nivel 3, honesto, para a bateria e as provas da
Fase 4. Nada sai deste processo.

E o motor de ``exemplos.selo_nivel2`` no nivel 3: tudo o que o de nivel 2 faz e mais o recibo no ``_meta`` do
resultado de toda acao com numero (``contrato.CHAVE_RECIBO``: numero, estado, ``repetida``, objetos criados e, no
lote, as contagens de ``itens``), o ``depois`` na consulta (com a hora de cada evento) e ``selo_bloqueio``, que para o
que ainda nao terminou e queima os numeros que nao chegaram. Num teste de nivel 3 ele cumpre todos os casos com a
declaracao dele (``selo_nivel2.declaracao``). Rotas de controle, ferramentas e argumentos: os de ``selo_nivel2``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from starlette.applications import Starlette

from exemplos.comum import rodar
from exemplos.selo_nivel2 import ACOES, CAMINHO_PRODUCAO, CAMINHO_TESTE, Motor, montar


def criar(
    *,
    producao: str = CAMINHO_PRODUCAO,
    teste: str | None = CAMINHO_TESTE,
    ferramentas_de_teste: bool = False,
    esquemas: Literal["contrato", "kit"] = "contrato",
    acoes: Sequence[str] = ACOES,
    mapa_de_chaves: Mapping[str, str] | None = None,
) -> Starlette:
    """O duble honesto de nivel 3. Os parametros sao os de ``selo_nivel2.criar`` (menos ``nivel``), inclusive
    ``mapa_de_chaves`` (chave de teste -> pessoa)."""
    return montar(
        Motor(
            nivel=3,
            producao=producao,
            teste=teste,
            ferramentas_de_teste=ferramentas_de_teste,
            esquemas=esquemas,
            acoes=acoes,
            mapa_de_chaves=mapa_de_chaves,
        )
    )


if __name__ == "__main__":
    rodar(criar, 9111)
