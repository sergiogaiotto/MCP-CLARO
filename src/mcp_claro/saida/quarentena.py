"""Quarentena de um servidor de terceiros: mudou, parou.

``por_em_quarentena`` e o unico caminho do codigo que poe um servidor em quarentena. Quem chama: as rotinas (o
mudou-parou, quando o que o servidor declara nao bate com a revisao vigente, ou quando ele nao responde a
conferencia em rodadas seguidas) e o despachante (sondagem que recusa um terceiro ativo e revisado, D29). O aviso
ao ADMIN por e-mail e webhook fica com as rotinas, depois desta funcao (os avisos dizem que os limites foram
suspensos).

Numa transacao so, a de quem chama:

1. toma a trava exclusiva do sistema (a mesma que o envio, o pedido de acao e a liberacao da ficha tomam
   compartilhada): ou o envio marcou "enviando" antes, e a acao segue, ou a quarentena entra antes, e nada sai;
2. ``UPDATE sistemas ... WHERE id = :i AND situacao = 'ativo' AND revisao_servidor_id = :base`` (critica B1): a
   quarentena so vale contra a revisao que quem chama comparou. Se outra copia ja pos em quarentena, ou se o ADMIN
   concluiu uma revisao nova no meio, nao ha linha, e nada muda. A guarda do banco apaga a revisao vigente;
3. suspende os limites ativos do sistema (critica M2), com ``limite_suspenso`` no extrato de cada pessoa;
4. cancela o que espera na fila (``fila.parar_por_quarentena``), com ``acao_parada`` no extrato de cada pessoa.
   Nesta ordem: a linha do limite antes da linha da fila, como a tela que encerra um limite e cancela o que ele
   liberou (``telas/limites.py``), que nao toma a trava do sistema. Na ordem inversa, as duas esperariam uma pela
   outra, e o banco desfaria uma delas (impasse);
5. grava ``sistema_em_quarentena`` (ou ``sistema_em_quarentena_sem_resposta``) no extrato, com as ferramentas que
   mudaram, os protocolos parados e quantos limites foram suspensos.

So uma nova conclusao da revisao do servidor, pelo ADMIN, o poe de volta em servico (no banco, alem dela, so a
desativacao tira da quarentena).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.quarentena import MOTIVO_PARADA, MOTIVO_QUARENTENA, Diferenca
from mcp_claro.extrato import registrar
from mcp_claro.regras.escondidos import marcar_escondidos
from mcp_claro.saida import fila, travas

MOTIVO_SEM_RESPOSTA: Final = (
    "mudou, parou: o servidor de terceiros nao respondeu a conferencia em rodadas seguidas; precisa de nova"
    " revisao do ADMIN"
)
"""``sistemas.motivo`` do servidor que foi a quarentena por nao responder a conferencia (critica A5)."""

MOTIVO_LIMITE: Final = "o servidor entrou em quarentena (mudou, parou); faca o limite de novo depois da nova revisao"
"""Motivo do limite suspenso pela quarentena."""

PAPEIS: Final = ("rotinas", "despachante")
TETO_FERRAMENTAS_NO_EXTRATO: Final = 50
TETO_NOME: Final = 128


@dataclass(frozen=True, slots=True)
class Quarentena:
    """O que a quarentena fez: o servidor, os protocolos parados e quantos limites foram suspensos."""

    sistema_id: uuid.UUID
    nome: str
    paradas: tuple[str, ...]
    limites_suspensos: int


def _ferramentas(diferencas: Sequence[Diferenca]) -> list[dict[str, Any]]:
    """``dados.ferramentas`` do extrato: nome (texto do fornecedor, com escondidos marcados e cortado), mudanca e
    impressoes. Nada do texto da definicao. No maximo ``TETO_FERRAMENTAS_NO_EXTRATO``."""
    saida: list[dict[str, Any]] = []
    for d in diferencas[:TETO_FERRAMENTAS_NO_EXTRATO]:
        retrato: dict[str, Any] = dict(d.retrato())
        retrato["ferramenta"] = marcar_escondidos(d.nome_original[:TETO_NOME])
        saida.append(retrato)
    return saida


async def por_em_quarentena(
    c: AsyncConnection,
    *,
    sistema_id: uuid.UUID,
    base: uuid.UUID,
    papel: str,
    motivo: str | None = None,
    diferencas: Sequence[Diferenca] = (),
    rodadas: int | None = None,
) -> Quarentena | None:
    """Poe o servidor em quarentena, na transacao de quem chama. Devolve None quando nada mudou (o servidor nao
    estava ``ativo`` com a revisao ``base`` vigente).

    ``base``: a revisao do servidor (``revisoes_servidor.id``) que quem chama comparou. ``papel``: ``rotinas`` ou
    ``despachante`` (o usuario do banco). ``diferencas``: o que mudou (``cadastro.quarentena.diferencas``).
    ``rodadas``: quantas rodadas seguidas sem resposta (grava ``sistema_em_quarentena_sem_resposta``).
    ``motivo`` (``sistemas.motivo``): sem ele, ``MOTIVO_SEM_RESPOSTA`` com ``rodadas`` e ``MOTIVO_QUARENTENA`` sem;
    com ``rodadas``, o motivo de "mudou a definicao" e recusado (o extrato diria outra coisa).
    """
    if papel not in PAPEIS:
        raise ValueError(f"Quem poe em quarentena e rotinas ou despachante, nao {papel}.")
    if rodadas is not None and rodadas < 1:
        raise ValueError("rodadas sem resposta conta a partir de 1.")
    if motivo is None:
        motivo = MOTIVO_SEM_RESPOSTA if rodadas is not None else MOTIVO_QUARENTENA
    elif rodadas is not None and motivo == MOTIVO_QUARENTENA:
        raise ValueError("Servidor que nao respondeu a conferencia vai a quarentena com MOTIVO_SEM_RESPOSTA.")
    await travas.exclusiva(c, travas.chaves_do_alcance("sistema", sistema_id=sistema_id))
    linha = (
        await c.execute(
            text(
                "UPDATE sistemas SET situacao = 'quarentena', motivo = :m, atualizado_em = now()"
                " WHERE id = :i AND situacao = 'ativo' AND revisao_servidor_id = :b RETURNING id, nome"
            ),
            {"i": sistema_id, "b": base, "m": motivo},
        )
    ).first()
    if linha is None:
        return None
    # O limite antes da fila (ver o topo do modulo): a ordem das travas de linha da tela que encerra um limite.
    suspensos = await fila.suspender_limites(c, sistema_id, motivo=MOTIVO_LIMITE, papel=papel)
    paradas = await fila.parar_por_quarentena(
        c, sistema_id, motivo=MOTIVO_PARADA, papel=papel, quem=f"quarentena:{papel}"
    )
    nome = str(linha.nome)
    campos: dict[str, object] = {"sistema": nome}
    tipo = "sistema_em_quarentena"
    if rodadas is not None:
        tipo = "sistema_em_quarentena_sem_resposta"
        campos["rodadas"] = rodadas
    await registrar(
        c,
        papel=papel,
        tipo=tipo,
        campos=campos,
        sistema_id=sistema_id,
        dados={
            "revisao": str(base),
            "ferramentas": _ferramentas(diferencas),
            "mudancas": len(diferencas),
            "paradas": paradas,
            "limites_suspensos": suspensos,
        },
    )
    return Quarentena(sistema_id, nome, tuple(paradas), suspensos)
