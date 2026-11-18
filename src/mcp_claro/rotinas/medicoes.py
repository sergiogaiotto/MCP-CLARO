"""Particoes mensais e resumos das medicoes.

- Particoes: ``garantir_particao_mensal`` (funcao SQL da migracao 0001) cria, para o
  extrato e para ``medicoes.minuto``, a particao do mes corrente e dos proximos meses.
- Resumo: ``medicoes.minuto`` -> ``medicoes.hora`` -> ``medicoes.dia``, em UTC. Cada
  rodada refaz as horas e os dias recentes ja completos (soma, sobrescrevendo), o que
  absorve contagens que chegam atrasadas. Nunca refaz um trecho que a guarda ja apagou.
- Guarda: apaga minutos, horas e dias mais velhos que ``METRICS_*_RETENTION_*``.

As medicoes nao tem pessoa nem conteudo: so nome, rotulos e contagem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.logs import logger

log = logger("mcp_claro.rotinas.medicoes")

TABELAS_PARTICIONADAS = ("extrato", "medicoes.minuto")
HORAS_REFEITAS = 48
DIAS_REFEITOS = 3


def _meses(hoje: date, a_frente: int) -> list[date]:
    meses = []
    ano, mes = hoje.year, hoje.month
    for _ in range(a_frente + 1):
        meses.append(date(ano, mes, 1))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return meses


async def garantir_particoes(engine: AsyncEngine, meses_a_frente: int = 3, hoje: date | None = None) -> list[str]:
    """Garante as particoes do mes corrente e dos proximos ``meses_a_frente``. Devolve os nomes."""
    hoje = hoje or datetime.now(UTC).date()
    nomes: list[str] = []
    async with engine.begin() as c:
        for tabela in TABELAS_PARTICIONADAS:
            for mes in _meses(hoje, meses_a_frente):
                nome = (
                    await c.execute(
                        text("SELECT garantir_particao_mensal(CAST(:t AS regclass), :d)"), {"t": tabela, "d": mes}
                    )
                ).scalar_one()
                nomes.append(str(nome))
    return nomes


_SQL_HORAS = """
WITH janela AS (
  SELECT greatest(
           least(
             coalesce((SELECT max(hora) FROM medicoes.hora) - interval '2 hours',
                      (SELECT date_trunc('hour', min(minuto), 'UTC') FROM medicoes.minuto)),
             date_trunc('hour', now(), 'UTC') - make_interval(hours => :horas)
           ),
           date_trunc('hour', now() - make_interval(days => :guarda_dias), 'UTC') + interval '1 hour'
         ) AS desde,
         date_trunc('hour', now(), 'UTC') AS ate
)
INSERT INTO medicoes.hora (hora, nome, rotulos, valor)
SELECT date_trunc('hour', m.minuto, 'UTC'), m.nome, m.rotulos, sum(m.valor)::bigint
FROM medicoes.minuto m, janela j
WHERE m.minuto >= j.desde AND m.minuto < j.ate
GROUP BY 1, 2, 3
ON CONFLICT (hora, nome, rotulos) DO UPDATE SET valor = EXCLUDED.valor
"""

_SQL_DIAS = """
WITH janela AS (
  SELECT greatest(
           least(
             coalesce((SELECT max(dia) FROM medicoes.dia) - 1,
                      (SELECT (min(hora) AT TIME ZONE 'UTC')::date FROM medicoes.hora)),
             (now() AT TIME ZONE 'UTC')::date - CAST(:dias AS integer)
           ),
           ((now() - make_interval(months => :guarda_meses)) AT TIME ZONE 'UTC')::date + 1
         ) AS desde,
         (now() AT TIME ZONE 'UTC')::date AS ate
)
INSERT INTO medicoes.dia (dia, nome, rotulos, valor)
SELECT (h.hora AT TIME ZONE 'UTC')::date, h.nome, h.rotulos, sum(h.valor)::bigint
FROM medicoes.hora h, janela j
WHERE h.hora >= (j.desde::timestamp AT TIME ZONE 'UTC') AND h.hora < (j.ate::timestamp AT TIME ZONE 'UTC')
GROUP BY 1, 2, 3
ON CONFLICT (dia, nome, rotulos) DO UPDATE SET valor = EXCLUDED.valor
"""


@dataclass(frozen=True, slots=True)
class ResumoMedicoes:
    horas: int
    dias: int
    minutos_apagados: int
    horas_apagadas: int
    dias_apagados: int


async def resumir_horas(c: AsyncConnection, guarda_minuto_dias: int) -> int:
    return int(
        (await c.execute(text(_SQL_HORAS), {"horas": HORAS_REFEITAS, "guarda_dias": guarda_minuto_dias})).rowcount
    )


async def resumir_dias(c: AsyncConnection, guarda_hora_meses: int) -> int:
    return int((await c.execute(text(_SQL_DIAS), {"dias": DIAS_REFEITOS, "guarda_meses": guarda_hora_meses})).rowcount)


async def aplicar_guarda(c: AsyncConnection, minuto_dias: int, hora_meses: int, dia_anos: int) -> tuple[int, int, int]:
    minutos = (
        await c.execute(
            text("DELETE FROM medicoes.minuto WHERE minuto < now() - make_interval(days => :d)"), {"d": minuto_dias}
        )
    ).rowcount
    horas = (
        await c.execute(
            text("DELETE FROM medicoes.hora WHERE hora < now() - make_interval(months => :m)"), {"m": hora_meses}
        )
    ).rowcount
    dias = (
        await c.execute(
            text(
                "DELETE FROM medicoes.dia WHERE dia < ((now() - make_interval(years => :a)) AT TIME ZONE 'UTC')::date"
            ),
            {"a": dia_anos},
        )
    ).rowcount
    return int(minutos), int(horas), int(dias)


async def resumir_medicoes(
    engine: AsyncEngine, *, minuto_dias: int = 31, hora_meses: int = 13, dia_anos: int = 5
) -> ResumoMedicoes:
    """Uma rodada: minuto -> hora -> dia, e depois a guarda. Uma transacao por passo."""
    async with engine.begin() as c:
        horas = await resumir_horas(c, minuto_dias)
    async with engine.begin() as c:
        dias = await resumir_dias(c, hora_meses)
    async with engine.begin() as c:
        apagados = await aplicar_guarda(c, minuto_dias, hora_meses, dia_anos)
    return ResumoMedicoes(horas, dias, *apagados)
