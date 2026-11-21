"""Vencimento das ordens de servico, inatividade e aviso antes de vencer.

- ``ativa`` com ``fim`` no passado vira ``vencida``;
- ``ativa`` sem uso ha ``WORK_ORDER_IDLE_DAYS`` (desde o ultimo uso, ou desde o inicio
  se nunca foi usada) vira ``inativa``;
- ``ativa`` que vence nos proximos ``WORK_ORDER_WARN_DAYS`` recebe um aviso, uma vez
  por ordem (``chave_unica``). As ja avisadas ficam de fora da busca, e a rodada segue de
  lote em lote ate o fim da janela: muitas ordens avisadas que vencem antes nao seguram
  o aviso das seguintes.

Cada mudanca deixa linha no extrato, em nome da pessoa, e um aviso por e-mail a ela.
O aviso leva so o nome do modelo, o nome do aplicativo e a data: nada do que foi lido.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_pessoa
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.rotinas.politica import valor_politica

log = logger("mcp_claro.rotinas.ordens")

LOTE = 500


@dataclass(frozen=True, slots=True)
class OrdemMudou:
    id: uuid.UUID
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    modelo: str
    aplicativo: str
    fim: datetime


def _data(quando: datetime) -> str:
    return quando.strftime("%d/%m/%Y")


SQL_VENCER = """
WITH alvo AS (
  SELECT id FROM ordens_servico
  WHERE situacao = 'ativa' AND fim <= now()
  ORDER BY fim
  LIMIT :lote
  FOR UPDATE SKIP LOCKED
), mudou AS (
  UPDATE ordens_servico o SET situacao = :nova
  FROM alvo WHERE o.id = alvo.id
  RETURNING o.id, o.pessoa_id, o.aplicativo_id, o.modelo_id, o.fim
)
SELECT mudou.id, mudou.pessoa_id, mudou.aplicativo_id, mudou.fim, m.nome AS modelo, a.nome AS aplicativo
FROM mudou
JOIN modelos_ordem m ON m.id = mudou.modelo_id
JOIN aplicativos a ON a.id = mudou.aplicativo_id
"""

SQL_INATIVAR = """
WITH alvo AS (
  SELECT id FROM ordens_servico
  WHERE situacao = 'ativa' AND fim > now()
    AND coalesce(ultimo_uso, inicio) <= now() - make_interval(days => :dias)
  ORDER BY fim
  LIMIT :lote
  FOR UPDATE SKIP LOCKED
), mudou AS (
  UPDATE ordens_servico o SET situacao = :nova
  FROM alvo WHERE o.id = alvo.id
  RETURNING o.id, o.pessoa_id, o.aplicativo_id, o.modelo_id, o.fim
)
SELECT mudou.id, mudou.pessoa_id, mudou.aplicativo_id, mudou.fim, m.nome AS modelo, a.nome AS aplicativo
FROM mudou
JOIN modelos_ordem m ON m.id = mudou.modelo_id
JOIN aplicativos a ON a.id = mudou.aplicativo_id
"""


async def _mudar(c: AsyncConnection, sql: str, nova: str, extra: dict[str, object]) -> list[OrdemMudou]:
    linhas = (await c.execute(text(sql), {"nova": nova, "lote": LOTE} | extra)).all()
    return [
        OrdemMudou(
            id=uuid.UUID(str(r.id)),
            pessoa_id=uuid.UUID(str(r.pessoa_id)),
            aplicativo_id=uuid.UUID(str(r.aplicativo_id)),
            modelo=str(r.modelo),
            aplicativo=str(r.aplicativo),
            fim=r.fim,
        )
        for r in linhas
    ]


async def vencer_ordens(engine: AsyncEngine) -> dict[str, int]:
    """Uma rodada: vence as ordens no fim do prazo e inativa as paradas. Devolve as contagens."""
    async with engine.begin() as c:
        dias_parada = await valor_politica(c, "WORK_ORDER_IDLE_DAYS")
        vencidas = await _mudar(c, SQL_VENCER, "vencida", {})
        for o in vencidas:
            await registrar(
                c,
                papel="rotinas",
                tipo="ordem_vencida",
                campos={"modelo": o.modelo},
                pessoa_id=o.pessoa_id,
                aplicativo_id=o.aplicativo_id,
                dados={"ordem_id": str(o.id), "fim": o.fim.isoformat()},
            )
            await avisar_pessoa(
                c,
                o.pessoa_id,
                "ordem_vencida",
                "MCP-CLARO: sua ordem de servico venceu",
                {
                    "texto": (
                        f"A ordem de servico {o.modelo}, do aplicativo {o.aplicativo}, venceu em {_data(o.fim)}. "
                        "O aplicativo nao le mais nada em seu nome por ela. Se ainda precisar, peca outra nas telas."
                    ),
                    "ordem_id": str(o.id),
                    "modelo": o.modelo,
                    "aplicativo": o.aplicativo,
                    "fim": o.fim.isoformat(),
                },
                chave_unica=f"ordem_vencida:{o.id}",
            )
        inativas = await _mudar(c, SQL_INATIVAR, "inativa", {"dias": dias_parada})
        for o in inativas:
            await registrar(
                c,
                papel="rotinas",
                tipo="ordem_inativa",
                campos={"modelo": o.modelo, "dias": dias_parada},
                pessoa_id=o.pessoa_id,
                aplicativo_id=o.aplicativo_id,
                dados={"ordem_id": str(o.id), "dias_sem_uso": dias_parada},
            )
            await avisar_pessoa(
                c,
                o.pessoa_id,
                "ordem_inativa",
                "MCP-CLARO: sua ordem de servico ficou inativa",
                {
                    "texto": (
                        f"A ordem de servico {o.modelo}, do aplicativo {o.aplicativo}, ficou {dias_parada} dias "
                        "sem uso e foi desligada. Se ainda precisar, peca outra nas telas."
                    ),
                    "ordem_id": str(o.id),
                    "modelo": o.modelo,
                    "aplicativo": o.aplicativo,
                },
                chave_unica=f"ordem_inativa:{o.id}",
            )
    if vencidas or inativas:
        log.info("ordens_mudaram", vencidas=len(vencidas), inativas=len(inativas))
    return {"vencidas": len(vencidas), "inativas": len(inativas)}


SQL_A_VENCER = """
SELECT o.id, o.pessoa_id, o.aplicativo_id, o.fim, m.nome AS modelo, a.nome AS aplicativo
FROM ordens_servico o
JOIN modelos_ordem m ON m.id = o.modelo_id
JOIN aplicativos a ON a.id = o.aplicativo_id
WHERE o.situacao = 'ativa' AND o.fim > now() AND o.fim <= now() + make_interval(days => :d)
  AND (o.fim, o.id) > (CAST(:depois_de_fim AS timestamptz), CAST(:depois_de_id AS uuid))
  AND NOT EXISTS (SELECT 1 FROM avisos v WHERE v.chave_unica = 'ordem_a_vencer:' || o.id::text)
ORDER BY o.fim, o.id
LIMIT :lote
"""
"""Um lote das ordens ainda nao avisadas que vencem na janela, depois de ``(fim, id)`` do lote anterior."""


async def avisar_ordens_a_vencer(engine: AsyncEngine) -> int:
    """Avisa cada ordem ativa que vence em ate ``WORK_ORDER_WARN_DAYS``, uma vez por ordem.

    De lote em lote (``LOTE``, cada um na sua transacao), em ordem de ``(fim, id)``, ate a janela acabar. A busca
    deixa de fora as ja avisadas (``avisos.chave_unica``): sem isso, um lote cheio de ordens ja avisadas que vencem
    antes seguraria o aviso das outras."""
    novos = 0
    async with engine.connect() as c:
        dias = await valor_politica(c, "WORK_ORDER_WARN_DAYS")
    depois_de: tuple[datetime, uuid.UUID] = (datetime.min.replace(tzinfo=UTC), uuid.UUID(int=0))
    while True:
        async with engine.begin() as c:
            linhas = (
                await c.execute(
                    text(SQL_A_VENCER),
                    {"d": dias, "lote": LOTE, "depois_de_fim": depois_de[0], "depois_de_id": depois_de[1]},
                )
            ).all()
            for r in linhas:
                ordem_id = uuid.UUID(str(r.id))
                pessoa_id = uuid.UUID(str(r.pessoa_id))
                entrou = await avisar_pessoa(
                    c,
                    pessoa_id,
                    "ordem_a_vencer",
                    "MCP-CLARO: sua ordem de servico vence em breve",
                    {
                        "texto": (
                            f"A ordem de servico {r.modelo}, do aplicativo {r.aplicativo}, vence em {_data(r.fim)}. "
                            "Depois disso o aplicativo para de ler em seu nome. Se ainda precisar, renove nas telas."
                        ),
                        "ordem_id": str(ordem_id),
                        "modelo": r.modelo,
                        "aplicativo": r.aplicativo,
                        "fim": r.fim.isoformat(),
                    },
                    chave_unica=f"ordem_a_vencer:{ordem_id}",
                )
                if entrou:
                    novos += 1
                    await registrar(
                        c,
                        papel="rotinas",
                        tipo="ordem_a_vencer",
                        campos={"modelo": r.modelo, "fim": _data(r.fim)},
                        pessoa_id=pessoa_id,
                        aplicativo_id=uuid.UUID(str(r.aplicativo_id)),
                        dados={"ordem_id": str(ordem_id), "dias_de_aviso": dias},
                    )
        if len(linhas) < LOTE:
            return novos
        depois_de = (linhas[-1].fim, uuid.UUID(str(linhas[-1].id)))
