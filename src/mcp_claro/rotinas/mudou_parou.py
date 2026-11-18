"""Mudou-parou para sistemas proprios: ferramenta que passa a pedir mais perde a autorizacao.

Para cada sistema proprio ativo, as rotinas pedem ao despachante o que o sistema
declara hoje e comparam a impressao digital de cada ferramenta revisada ativa com a
declarada:

- igual: nada a fazer;
- diferente e pedindo mais (campo novo, obrigatorio novo, deixou de ser so leitura,
  ficou destrutiva; ver ``cadastro.impressao.pede_mais``): a revisada fica ``suspensa``
  com motivo "mudou, parou", sai da vitrine, vai ao extrato e o ADMIN recebe aviso.
  So volta com nova revisao do ADMIN;
- diferente sem pedir mais (por exemplo, so o texto): so registra no extrato, uma vez
  por nova definicao. A quarentena por texto e da Fase 3 (servidores de terceiros).

A base da comparacao e o que o ADMIN revisou (impressao, esquema e etiquetas
guardados na revisao), nao a ultima leitura.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.cadastro.impressao import impressao, pede_mais
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES

log = logger("mcp_claro.rotinas.mudou_parou")

MOTIVO_SUSPENSAO = "mudou, parou: o sistema passou a pedir mais que o revisado; precisa de nova revisao do ADMIN"


class FonteDeclaracoes(Protocol):
    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class ResultadoMudouParou:
    sistemas: int = 0
    suspensas: list[str] = field(default_factory=list)
    mudaram: list[str] = field(default_factory=list)
    sumiram: list[str] = field(default_factory=list)
    sem_resposta: list[str] = field(default_factory=list)


async def conferir_sistema(
    engine: AsyncEngine,
    fonte: FonteDeclaracoes,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    emails_admin: Sequence[str],
    resultado: ResultadoMudouParou,
) -> None:
    async with engine.connect() as c:
        revisadas = (
            await c.execute(
                text(
                    "SELECT id, nome_original, nome_vitrine, esquema_entrada, esquema_saida, etiquetas, impressao"
                    " FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa' ORDER BY nome_original"
                ),
                {"s": sistema_id},
            )
        ).all()
        vistas_antes = {
            str(nome): str(imp)
            for nome, imp in await c.execute(
                text("SELECT nome, impressao FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": sistema_id}
            )
        }
    if not revisadas:
        return
    declaradas = await fonte.declaracoes(sistema_id)
    por_nome = {str(d["name"]): d for d in declaradas if isinstance(d.get("name"), str)}
    async with engine.begin() as c:
        for r in revisadas:
            declarada = por_nome.get(r.nome_original)
            if declarada is None:
                resultado.sumiram.append(r.nome_vitrine)
                log.info("ferramenta_sumiu", sistema=sistema_nome, ferramenta=r.nome_vitrine)
                continue
            nova = impressao(declarada)
            if nova == r.impressao:
                continue
            revisada = {
                "name": r.nome_original,
                "inputSchema": r.esquema_entrada,
                "outputSchema": r.esquema_saida,
                "annotations": r.etiquetas,
            }
            ferramenta_id = uuid.UUID(str(r.id))
            if pede_mais(revisada, declarada):
                mudou = await c.execute(
                    text(
                        "UPDATE ferramentas_revisadas SET situacao = 'suspensa', motivo = :m"
                        " WHERE id = :i AND situacao = 'ativa'"
                    ),
                    {"m": MOTIVO_SUSPENSAO, "i": ferramenta_id},
                )
                if not mudou.rowcount:
                    continue
                resultado.suspensas.append(r.nome_vitrine)
                CONTADORES.somar("mudou_parou_suspensas")
                await registrar(
                    c,
                    papel="rotinas",
                    tipo="ferramenta_suspensa",
                    campos={"ferramenta": r.nome_vitrine},
                    sistema_id=sistema_id,
                    ferramenta=r.nome_vitrine,
                    dados={
                        "ferramenta_id": str(ferramenta_id),
                        "impressao_revisada": r.impressao,
                        "impressao_nova": nova,
                        "motivo": "pede_mais",
                    },
                )
                await avisar_admin(
                    c,
                    "ferramenta_suspensa",
                    "MCP-CLARO: ferramenta suspensa (mudou, parou)",
                    {
                        "texto": (
                            f"A ferramenta {r.nome_vitrine}, do sistema {sistema_nome}, passou a pedir mais do que "
                            "foi revisado. Ela saiu da vitrine de todos ate uma nova revisao nas telas do ADMIN."
                        ),
                        "sistema": sistema_nome,
                        "ferramenta": r.nome_vitrine,
                        "ferramenta_id": str(ferramenta_id),
                        "impressao_revisada": r.impressao,
                        "impressao_nova": nova,
                    },
                    f"ferramenta_suspensa:{ferramenta_id}:{nova}",
                    emails_admin,
                )
                log.warning("ferramenta_suspensa", sistema=sistema_nome, ferramenta=r.nome_vitrine)
            else:
                if vistas_antes.get(r.nome_original) == nova:
                    continue  # esta mudanca ja foi vista e registrada numa rodada anterior
                resultado.mudaram.append(r.nome_vitrine)
                CONTADORES.somar("mudou_parou_mudou_sem_pedir_mais")
                await registrar(
                    c,
                    papel="rotinas",
                    tipo="ferramenta_mudou",
                    campos={"ferramenta": r.nome_vitrine},
                    sistema_id=sistema_id,
                    ferramenta=r.nome_vitrine,
                    dados={
                        "ferramenta_id": str(ferramenta_id),
                        "impressao_revisada": r.impressao,
                        "impressao_nova": nova,
                    },
                )


async def conferir_sistemas_proprios(
    engine: AsyncEngine, fonte: FonteDeclaracoes, emails_admin: Sequence[str]
) -> ResultadoMudouParou:
    """Uma rodada do mudou-parou. Um sistema que nao responde nao segura os outros."""
    resultado = ResultadoMudouParou()
    async with engine.connect() as c:
        sistemas = (
            await c.execute(
                text("SELECT id, nome FROM sistemas WHERE origem = 'proprio' AND situacao = 'ativo' ORDER BY nome")
            )
        ).all()
    for s in sistemas:
        resultado.sistemas += 1
        try:
            await conferir_sistema(engine, fonte, uuid.UUID(str(s.id)), str(s.nome), emails_admin, resultado)
        except Exception as erro:  # noqa: BLE001 - um sistema mudo nao para o mudou-parou dos outros
            resultado.sem_resposta.append(str(s.nome))
            CONTADORES.somar("mudou_parou_sem_resposta")
            log.warning("mudou_parou_sem_resposta", sistema=str(s.nome), erro=type(erro).__name__)
    return resultado
