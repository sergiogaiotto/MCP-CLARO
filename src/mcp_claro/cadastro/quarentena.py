"""Mudou, parou de um servidor de terceiros: o que mudou entre a revisao vigente e o que ele declara agora.

Puro, sem banco. A base e ``revisoes_servidor.declaradas`` da revisao vigente; o agora e a leitura fresca do
``tools/list``. Qualquer diferenca (ferramenta mudada, mesmo so no texto, nova ou sumida) poe o servidor inteiro
em quarentena (D5). ``icons``, ``_meta``, ``execution`` e ``instructions`` nao contam: nao entram na impressao e
nao chegam ao aplicativo.

Os nomes (``nome_original``) sao texto do fornecedor, sem formato: quem mostra numa tela usa o filtro de
escondidos e a classe ``estranho``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from mcp_claro.cadastro.impressao import impressao

TipoDiferenca = Literal["mudou", "sumiu", "nova"]

MOTIVO_QUARENTENA: Final = (
    "mudou, parou: o servidor de terceiros mudou a definicao depois da revisao; precisa de nova revisao do ADMIN"
)
"""``sistemas.motivo`` do servidor em quarentena."""

MOTIVO_PARADA: Final = "o servidor entrou em quarentena (mudou, parou); nada seguiu"
"""Desfecho da acao que esperava na fila quando o servidor entrou em quarentena."""


@dataclass(frozen=True, slots=True)
class Diferenca:
    """Uma ferramenta que nao bate com a revisao vigente. ``impressao_antes`` e None na nova;
    ``impressao_depois`` e None na que sumiu."""

    nome_original: str
    tipo: TipoDiferenca
    impressao_antes: str | None
    impressao_depois: str | None

    def retrato(self) -> dict[str, str | None]:
        """Para ``dados.ferramentas`` do extrato: so nome e impressoes, nada do texto da definicao."""
        return {
            "ferramenta": self.nome_original,
            "mudanca": self.tipo,
            "impressao_antes": self.impressao_antes,
            "impressao_depois": self.impressao_depois,
        }


def diferencas(base: Mapping[str, str], declaradas: Mapping[str, str]) -> list[Diferenca]:
    """O que mudou de ``base`` (nome -> impressao revisada) para ``declaradas`` (nome -> impressao agora).

    Em ordem de nome. Lista vazia: o servidor declara exatamente o que foi revisado.
    """
    achadas: list[Diferenca] = []
    for nome in sorted(set(base) | set(declaradas)):
        antes, depois = base.get(nome), declaradas.get(nome)
        if antes == depois:
            continue
        tipo: TipoDiferenca = "nova" if antes is None else "sumiu" if depois is None else "mudou"
        achadas.append(Diferenca(nome, tipo, antes, depois))
    return achadas


def impressoes(definicoes: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Nome -> impressao de cada ferramenta declarada no ``tools/list`` (sem nome em texto, fica de fora).

    Um nome declarado duas vezes com definicoes diferentes vale como uma impressao que nao bate com nenhuma
    revisao: o servidor que se contradiz conta como mudado.
    """
    por_nome: dict[str, set[str]] = {}
    for definicao in definicoes:
        nome = definicao.get("name")
        if isinstance(nome, str):
            por_nome.setdefault(nome, set()).add(impressao(definicao))
    saida: dict[str, str] = {}
    for nome, vistas in por_nome.items():
        if len(vistas) == 1:
            (saida[nome],) = vistas
        else:
            juntas = "|".join(sorted(vistas)).encode("utf-8")
            saida[nome] = "sha256:" + hashlib.sha256(b"repetida|" + juntas).hexdigest()
    return saida


def declaradas(definicoes: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """``[{nome, impressao}]`` em ordem de nome, como ``revisoes_servidor.declaradas`` guarda.

    A impressao do servidor sai daqui: ``impressao_servidor((d["nome"], d["impressao"]) for d in lista)``. A
    conclusao da revisao (despachante) e o mudou-parou (rotinas) usam as duas, para comparar do mesmo jeito.
    """
    return [{"nome": nome, "impressao": imp} for nome, imp in sorted(impressoes(definicoes).items())]


def base_da_revisao(gravadas: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Nome -> impressao de ``revisoes_servidor.declaradas`` (``[{nome, impressao}]``)."""
    return {str(d["nome"]): str(d["impressao"]) for d in gravadas}
