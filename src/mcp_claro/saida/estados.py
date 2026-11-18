"""Estados de uma acao na porta de saida e as mudancas permitidas.

A mesma tabela vive no banco (``transicoes_saida``) como segunda barreira: cada UPDATE exige o estado
anterior, e o gatilho recusa qualquer mudanca fora da tabela.
"""

from __future__ import annotations

import enum
from typing import Final


class Estado(enum.StrEnum):
    AGUARDANDO_FICHA = "aguardando_ficha"
    LIBERADA = "liberada"
    ENVIANDO = "enviando"
    ENVIADA = "enviada"
    NAO_SAIU = "nao_saiu"
    SISTEMA_PEDIU_DADOS = "sistema_pediu_dados"
    NAO_CONFIRMADA = "nao_confirmada"
    VENCIDA = "vencida"
    CANCELADA = "cancelada"
    PARADA_POR_BLOQUEIO = "parada_por_bloqueio"
    PARADA_POR_RESTAURACAO = "parada_por_restauracao"


TRANSICOES: Final[frozenset[tuple[Estado, Estado]]] = frozenset(
    {
        (Estado.AGUARDANDO_FICHA, Estado.LIBERADA),
        (Estado.AGUARDANDO_FICHA, Estado.VENCIDA),
        (Estado.AGUARDANDO_FICHA, Estado.CANCELADA),
        (Estado.AGUARDANDO_FICHA, Estado.PARADA_POR_BLOQUEIO),
        (Estado.AGUARDANDO_FICHA, Estado.PARADA_POR_RESTAURACAO),
        (Estado.LIBERADA, Estado.ENVIANDO),
        (Estado.LIBERADA, Estado.CANCELADA),
        (Estado.LIBERADA, Estado.PARADA_POR_BLOQUEIO),
        (Estado.LIBERADA, Estado.PARADA_POR_RESTAURACAO),
        (Estado.ENVIANDO, Estado.ENVIADA),
        (Estado.ENVIANDO, Estado.NAO_SAIU),
        (Estado.ENVIANDO, Estado.SISTEMA_PEDIU_DADOS),
        (Estado.ENVIANDO, Estado.NAO_CONFIRMADA),
    }
)

FINAIS: Final[frozenset[Estado]] = frozenset(
    {
        Estado.VENCIDA,
        Estado.ENVIADA,
        Estado.NAO_SAIU,
        Estado.SISTEMA_PEDIU_DADOS,
        Estado.CANCELADA,
        Estado.PARADA_POR_BLOQUEIO,
        Estado.PARADA_POR_RESTAURACAO,
        Estado.NAO_CONFIRMADA,
    }
)
"""Estados de onde nada mais sai. "Nao confirmada" so muda pela decisao registrada da pessoa (outra tabela)."""

ANTES_DE_SAIR: Final[frozenset[Estado]] = frozenset({Estado.AGUARDANDO_FICHA, Estado.LIBERADA})
"""Estados em que cancelar e bloquear ainda impedem a saida."""

FRASE: Final[dict[Estado, str]] = {
    Estado.AGUARDANDO_FICHA: "A ficha espera a sua confirmacao na tela do gateway.",
    Estado.LIBERADA: "Confirmada. A acao espera a hora de sair; ainda da para cancelar.",
    Estado.ENVIANDO: "Estamos enviando ao sistema. Nao repita o pedido.",
    Estado.ENVIADA: "Enviada uma vez ao sistema.",
    Estado.NAO_SAIU: "Nao saiu: o sistema nao recebeu o pedido. Nada aconteceu.",
    Estado.SISTEMA_PEDIU_DADOS: "O sistema pediu mais dados no meio da acao; o MCP-CLARO nao repassa esses pedidos.",
    Estado.NAO_CONFIRMADA: "Resultado nao confirmado: o pedido pode ter chegado ao sistema. Nada sera reenviado; "
    "a pessoa decide na tela do gateway.",
    Estado.VENCIDA: "A ficha venceu sem resposta. Nada seguiu.",
    Estado.CANCELADA: "Cancelada antes de sair. Nada seguiu.",
    Estado.PARADA_POR_BLOQUEIO: "Parada por um bloqueio. Nada seguiu.",
    Estado.PARADA_POR_RESTAURACAO: "Parada porque o banco foi restaurado. Nada seguiu sem nova confirmacao.",
}


def pode(de: Estado, para: Estado) -> bool:
    return (de, para) in TRANSICOES
