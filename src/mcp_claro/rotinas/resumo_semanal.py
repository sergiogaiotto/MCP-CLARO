"""Resumo semanal do ADMIN: contagens da semana anterior, sem dado pessoal.

Semana ISO em UTC (segunda a domingo). A tarefa acorda de hora em hora; a partir de
segunda as 01:00 UTC (folga para as contagens atrasadas), poe na fila o resumo da
semana que acabou, uma vez por semana (``chave_unica``). Leva so totais:

- recusas por versao antiga, dos dois lados (aplicativos e sistemas);
- crachas recusados;
- leituras feitas e recusadas;
- bloqueios e pausas ligados na semana, e bloqueios ainda ativos.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.extrato import registrar
from mcp_claro.rotinas.medicoes import resumir_horas

FOLGA = timedelta(hours=1)
MEDIDAS = {
    "crachas_recusados": "crachas_recusados",
    "leituras": "leituras",
    "chamadas_recusadas": "leituras_recusadas",
    "leituras_recusadas_despachante": "leituras_recusadas",
}


@dataclass(frozen=True, slots=True)
class Semana:
    inicio: datetime
    fim: datetime

    @property
    def rotulo(self) -> str:
        ano, numero, _ = self.inicio.isocalendar()
        return f"{ano}-W{numero:02d}"


def semana_anterior(agora: datetime) -> Semana:
    agora = agora.astimezone(UTC)
    segunda = (agora - timedelta(days=agora.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return Semana(segunda - timedelta(days=7), segunda)


async def contagens(c: AsyncConnection, semana: Semana) -> dict[str, int]:
    janela = {"i": semana.inicio, "f": semana.fim}
    total: dict[str, int] = {
        "recusas_versao_aplicativos": 0,
        "recusas_versao_sistemas": 0,
        "crachas_recusados": 0,
        "leituras": 0,
        "leituras_recusadas": 0,
        "bloqueios_ligados": 0,
        "pausas_ligadas": 0,
        "bloqueios_ativos": 0,
    }
    for lado, n in await c.execute(
        text("SELECT lado, sum(contagem) FROM recusas_versao WHERE minuto >= :i AND minuto < :f GROUP BY lado"),
        janela,
    ):
        total["recusas_versao_aplicativos" if lado == "aplicativo" else "recusas_versao_sistemas"] += int(n)
    for nome, n in await c.execute(
        text(
            "SELECT nome, sum(valor) FROM medicoes.hora WHERE hora >= :i AND hora < :f"
            " AND nome = ANY (CAST(:nomes AS text[])) GROUP BY nome"
        ),
        janela | {"nomes": list(MEDIDAS)},
    ):
        total[MEDIDAS[str(nome)]] += int(n)
    for tipo, n in await c.execute(
        text("SELECT tipo, count(*) FROM bloqueios WHERE criado_em >= :i AND criado_em < :f GROUP BY tipo"), janela
    ):
        total["bloqueios_ligados" if tipo == "bloqueio" else "pausas_ligadas"] += int(n)
    total["bloqueios_ativos"] = int(
        (await c.execute(text("SELECT count(*) FROM bloqueios WHERE encerrado_em IS NULL"))).scalar_one()
    )
    return total


def corpo_do_resumo(semana: Semana, numeros: dict[str, int]) -> dict[str, Any]:
    fim_legivel = (semana.fim - timedelta(days=1)).strftime("%d/%m/%Y")
    return {
        "texto": (
            f"Resumo da semana {semana.rotulo}, de {semana.inicio.strftime('%d/%m/%Y')} a {fim_legivel} (UTC). "
            "So totais: nenhum dado de pessoa."
        ),
        "semana": semana.rotulo,
        "inicio": semana.inicio.isoformat(),
        "fim": semana.fim.isoformat(),
        "contagens": numeros,
        "linhas": [
            f"Recusas por versao antiga, lado dos aplicativos: {numeros['recusas_versao_aplicativos']}",
            f"Recusas por versao antiga, lado dos sistemas: {numeros['recusas_versao_sistemas']}",
            f"Crachas recusados: {numeros['crachas_recusados']}",
            f"Leituras feitas: {numeros['leituras']}",
            f"Leituras recusadas: {numeros['leituras_recusadas']}",
            f"Bloqueios ligados na semana: {numeros['bloqueios_ligados']}",
            f"Pausas ligadas na semana: {numeros['pausas_ligadas']}",
            f"Bloqueios ainda ativos: {numeros['bloqueios_ativos']}",
        ],
    }


async def resumo_semanal(
    engine: AsyncEngine,
    emails_admin: Sequence[str],
    *,
    guarda_minuto_dias: int = 31,
    agora: datetime | None = None,
) -> dict[str, Any] | None:
    """Poe o resumo da semana anterior na fila, uma vez. Devolve o corpo quando algo entrou."""
    agora = agora or datetime.now(UTC)
    semana = semana_anterior(agora)
    if agora < semana.fim + FOLGA:
        return None
    async with engine.begin() as c:
        await resumir_horas(c, guarda_minuto_dias)
    async with engine.begin() as c:
        numeros = await contagens(c, semana)
        corpo = corpo_do_resumo(semana, numeros)
        novos = await avisar_admin(
            c,
            "resumo_semanal",
            f"MCP-CLARO: resumo da semana {semana.rotulo}",
            corpo,
            f"resumo_semanal:{semana.rotulo}",
            emails_admin,
        )
        if not novos:
            return None
        await registrar(
            c,
            papel="rotinas",
            tipo="resumo_semanal",
            campos={"semana": semana.rotulo},
            dados={"semana": semana.rotulo, "destinos": novos} | {f"n_{k}": v for k, v in numeros.items()},
        )
    return corpo
