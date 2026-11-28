"""Resumo semanal do ADMIN: contagens da semana anterior, sem dado pessoal.

Semana ISO em UTC (segunda a domingo). A tarefa acorda de hora em hora; a partir de
segunda as 01:00 UTC (folga para as contagens atrasadas), poe na fila o resumo da
semana que acabou, uma vez por semana (``chave_unica``). Leva so totais:

- recusas por versao antiga, dos dois lados: aplicativos, sistemas proprios e, a parte, servidores de terceiros
  (``motivo = 'cadastro_terceiro'``, D30: a recusa de um terceiro nao dispara o alerta alto e fica no painel e
  aqui). As tres somam o total da decisao 8;
- crachas recusados;
- leituras feitas e recusadas;
- bloqueios e pausas ligados na semana, e bloqueios ainda ativos;
- Fase 4, os selos (desenho 7.4): testes do selo aprovados e nao aprovados na semana (``testes_selo``), sistemas com
  selo de nivel 2 e 3 agora (``selos_vigentes``) e, dos contadores que o despachante, a entrada e as telas ja somam
  (``medicoes.hora``; as rotinas nao os somam de novo, so os leem), as perguntas aos sistemas e como terminaram, as
  respostas invalidas, as retiradas (de todo motivo: o despachante soma as automaticas, e a tela do ADMIN, a dele), os
  avisos de bloqueio, os pedidos iguais que sairam com o mesmo numero, as acoes que sairam sem o numero devido e as
  vezes em que a pessoa registrou o contrario do que o sistema informou. Os testes saem da tabela ``testes_selo``, nao
  do contador ``selo_testes`` da bateria: a tabela e a mesma verdade das telas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.extrato import registrar
from mcp_claro.rotinas.medicoes import resumir_horas
from mcp_claro.selo import contrato

FOLGA = timedelta(hours=1)
MEDIDAS = {
    "crachas_recusados": "crachas_recusados",
    "leituras": "leituras",
    "chamadas_recusadas": "leituras_recusadas",
    "leituras_recusadas_despachante": "leituras_recusadas",
}

CONTAGENS_DO_SELO: Final = (
    "selo_testes_aprovados",
    "selo_testes_nao_aprovados",
    "selo_sistemas_nivel_2",
    "selo_sistemas_nivel_3",
    "selo_consultas",
    "selo_consultas_respondidas",
    "selo_consultas_sem_resposta",
    "selo_consultas_nao_feitas",
    "selo_desconhecido_no_fim",
    "selo_recibos",
    "selo_respostas_ao_aviso",
    "selo_respostas_invalidas",
    "selo_retiradas",
    "selo_avisos_bloqueio_respondidos",
    "selo_avisos_bloqueio_sem_resposta",
    "selo_numero_reaproveitado",
    "selo_envios_sem_numero",
    "selo_sistema_contradito",
)
"""As contagens dos selos no resumo (Fase 4), na ordem das linhas."""

MEDIDAS_DO_SELO: Final = (
    "selo_informacoes",
    "selo_respostas_invalidas",
    "selo_retiradas",
    "selo_avisos_bloqueio",
    "selo_numero_reaproveitado",
    "selo_envios",
    "selo_sistema_contradito",
)
"""Os contadores (``medicoes.contadores.CONTADORES``, por hora em ``medicoes.hora``) que o resumo le."""


def contagem_do_selo(nome: str, origem: str | None, desfecho: str | None) -> tuple[str, ...]:
    """Em que contagens do resumo entra uma linha de ``medicoes.hora`` (nome e os rotulos ``origem`` e
    ``desfecho``). ``selo_informacoes`` soma uma por pergunta gravada (``desfecho`` de ``contrato.DESFECHOS``) e uma a
    mais, ``desconhecido_no_fim``, quando a escada termina em ``desconhecido`` (critica B8): essa nao e outra
    pergunta."""
    if nome == "selo_informacoes":
        if desfecho == "desconhecido_no_fim":
            return ("selo_desconhecido_no_fim",)
        if origem == "recibo":
            return ("selo_recibos",)
        if origem == "aviso":
            return ("selo_respostas_ao_aviso",)
        if origem != "consulta":
            return ()
        if desfecho == "respondeu":
            return ("selo_consultas", "selo_consultas_respondidas")
        if desfecho in contrato.DESFECHOS_DE_FALHA_DO_SISTEMA:
            return ("selo_consultas", "selo_consultas_sem_resposta")
        return ("selo_consultas", "selo_consultas_nao_feitas")
    if nome == "selo_avisos_bloqueio":
        return {
            "respondido": ("selo_avisos_bloqueio_respondidos",),
            "sem_resposta": ("selo_avisos_bloqueio_sem_resposta",),
        }.get(desfecho or "", ())
    if nome == "selo_envios":
        return ("selo_envios_sem_numero",) if desfecho == "sem_numero" else ()
    if nome in ("selo_respostas_invalidas", "selo_retiradas", "selo_numero_reaproveitado", "selo_sistema_contradito"):
        return (nome,)
    return ()


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
        "recusas_versao_terceiros": 0,
        "crachas_recusados": 0,
        "leituras": 0,
        "leituras_recusadas": 0,
        "bloqueios_ligados": 0,
        "pausas_ligadas": 0,
        "bloqueios_ativos": 0,
    }
    for lado, de_terceiro, n in await c.execute(
        text(
            "SELECT lado, motivo = 'cadastro_terceiro', sum(contagem) FROM recusas_versao"
            " WHERE minuto >= :i AND minuto < :f GROUP BY 1, 2"
        ),
        janela,
    ):
        if lado == "aplicativo":
            total["recusas_versao_aplicativos"] += int(n)
        elif de_terceiro:
            total["recusas_versao_terceiros"] += int(n)
        else:
            total["recusas_versao_sistemas"] += int(n)
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
    total |= await contagens_do_selo(c, semana)
    return total


async def contagens_do_selo(c: AsyncConnection, semana: Semana) -> dict[str, int]:
    """As contagens dos selos na semana (``CONTAGENS_DO_SELO``)."""
    janela = {"i": semana.inicio, "f": semana.fim}
    total = dict.fromkeys(CONTAGENS_DO_SELO, 0)
    for resultado, n in await c.execute(
        text("SELECT resultado, count(*) FROM testes_selo WHERE testado_em >= :i AND testado_em < :f GROUP BY 1"),
        janela,
    ):
        total["selo_testes_aprovados" if resultado == "aprovado" else "selo_testes_nao_aprovados"] += int(n)
    for nivel, n in await c.execute(text("SELECT nivel, count(*) FROM selos_vigentes WHERE nivel >= 2 GROUP BY 1")):
        total[f"selo_sistemas_nivel_{int(nivel)}"] += int(n)
    for nome, origem, desfecho, n in await c.execute(
        text(
            "SELECT nome, rotulos ->> 'origem', rotulos ->> 'desfecho', sum(valor) FROM medicoes.hora"
            " WHERE hora >= :i AND hora < :f AND nome = ANY (CAST(:nomes AS text[])) GROUP BY 1, 2, 3"
        ),
        janela | {"nomes": list(MEDIDAS_DO_SELO)},
    ):
        for chave in contagem_do_selo(str(nome), origem, desfecho):
            total[chave] += int(n)
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
            f"Recusas por versao antiga, lado dos sistemas proprios: {numeros['recusas_versao_sistemas']}",
            f"Recusas por versao antiga, servidores de terceiros: {numeros['recusas_versao_terceiros']}",
            f"Crachas recusados: {numeros['crachas_recusados']}",
            f"Leituras feitas: {numeros['leituras']}",
            f"Leituras recusadas: {numeros['leituras_recusadas']}",
            f"Bloqueios ligados na semana: {numeros['bloqueios_ligados']}",
            f"Pausas ligadas na semana: {numeros['pausas_ligadas']}",
            f"Bloqueios ainda ativos: {numeros['bloqueios_ativos']}",
            *linhas_do_selo(numeros),
        ],
    }


def linhas_do_selo(numeros: dict[str, int]) -> list[str]:
    """As linhas dos selos no resumo (sem acento, como as outras; so totais)."""
    n = numeros
    return [
        f"Selos, testes aprovados na semana: {n['selo_testes_aprovados']}",
        f"Selos, testes nao aprovados na semana: {n['selo_testes_nao_aprovados']}",
        f"Sistemas com selo de nivel 2 agora: {n['selo_sistemas_nivel_2']}",
        f"Sistemas com selo de nivel 3 agora: {n['selo_sistemas_nivel_3']}",
        f"Consultas aos sistemas sobre acoes em duvida: {n['selo_consultas']}",
        f"Consultas respondidas pelo sistema: {n['selo_consultas_respondidas']}",
        f"Consultas sem resposta valida do sistema: {n['selo_consultas_sem_resposta']}",
        f"Consultas que o MCP-CLARO nao conseguiu fazer (sem chave, sala fora ou sistema fora de ativo):"
        f" {n['selo_consultas_nao_feitas']}",
        f"Escadas de consulta que terminaram com o sistema dizendo que nao sabe: {n['selo_desconhecido_no_fim']}",
        f"Recibos do nivel 3 lidos: {n['selo_recibos']}",
        f"Respostas ao aviso de bloqueio: {n['selo_respostas_ao_aviso']}",
        f"Respostas fora do contrato do selo (nao mostradas): {n['selo_respostas_invalidas']}",
        f"Selos retirados: {n['selo_retiradas']}",
        f"Avisos de bloqueio respondidos: {n['selo_avisos_bloqueio_respondidos']}",
        f"Avisos de bloqueio sem resposta: {n['selo_avisos_bloqueio_sem_resposta']}",
        f"Pedidos iguais enviados com o mesmo numero: {n['selo_numero_reaproveitado']}",
        f"Acoes enviadas sem o numero do selo que era devido: {n['selo_envios_sem_numero']}",
        f"Pessoas que registraram o contrario do que o sistema informou: {n['selo_sistema_contradito']}",
    ]


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
