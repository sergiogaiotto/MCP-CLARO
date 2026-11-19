"""Rotinas dos programas que rodam sozinhos (decisao 9).

Uma rodada (``cuidar_dos_programas``), nesta ordem, cada passo so sobre o que ainda esta ``ativo``:

1. encerra o programa (ativo ou pausado) cuja data de fim ja passou, no fuso de Brasilia;
2. pausa o programa cujo responsavel ou substituto nao esta mais ativo (regra fixa do plano);
3. pausa o programa cuja revisao periodica venceu: ``PROGRAM_OWNER_RECERT_DAYS`` desde ``revisado_em``;
4. pausa o programa cuja chave passou do prazo: ``PROGRAM_KEY_MAX_AGE_DAYS`` desde ``chave_trocada_em``;
5. pausa o programa sem pedido ha ``PROGRAM_IDLE_PAUSE_DAYS``. Conta desde o ultimo pedido
   (``aplicativos.visto_em``) ou desde a ultima revisao, o que for mais novo: retomar conta como revisao;
6. avisa o responsavel e o substituto ``PROGRAM_KEY_EXPIRY_WARN_DAYS`` antes de a chave vencer, uma vez por
   troca de chave (``chave_unica``);
7. apaga as contagens por minuto com mais de uma hora.

Cada mudanca vai ao extrato em nome do responsavel ("programa X, responsavel Y"), na mesma transacao, e vira
aviso por e-mail ao responsavel e ao substituto (e ao ADMIN, nas pausas e no encerramento). O aviso leva so o
nome do programa, o motivo e datas: nada do que o programa leu.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_admin, avisar_pessoa
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.regras.programas import FUSO
from mcp_claro.rotinas.agendador import Tarefa
from mcp_claro.rotinas.config import ConfigRotinas

log = logger("mcp_claro.rotinas.programas")

LOTE: Final = 200
GUARDA_CONTAGEM: Final = timedelta(hours=1)

# Colunas devolvidas por cada mudanca: o que o extrato e o aviso precisam.
_DEVOLVE = """
SELECT mudou.aplicativo_id, mudou.responsavel_id, mudou.substituto_id, mudou.fim, mudou.revisado_em,
       mudou.chave_trocada_em, a.nome AS programa, coalesce(r.nome, r.sujeito) AS responsavel, a.visto_em
FROM mudou
JOIN aplicativos a ON a.id = mudou.aplicativo_id
JOIN pessoas r ON r.id = mudou.responsavel_id
"""


def _mudar(onde: str) -> str:
    """Muda a situacao (``:nova``) dos programas que cumprem ``onde``, uma condicao fixa deste modulo."""
    return (
        "WITH alvo AS (SELECT pr.aplicativo_id FROM programas pr JOIN aplicativos a ON a.id = pr.aplicativo_id"  # noqa: S608
        f" WHERE {onde} ORDER BY pr.aplicativo_id LIMIT :lote FOR UPDATE OF pr SKIP LOCKED),"
        " mudou AS (UPDATE programas p SET situacao = :nova FROM alvo WHERE p.aplicativo_id = alvo.aplicativo_id"
        " RETURNING p.aplicativo_id, p.responsavel_id, p.substituto_id, p.fim, p.revisado_em, p.chave_trocada_em)"
        + _DEVOLVE
    )


SQL_ENCERRAR_NO_FIM = _mudar("pr.situacao IN ('ativo', 'pausado') AND pr.fim < (now() AT TIME ZONE :fuso)::date")
SQL_PAUSAR_SEM_RESPONSAVEL = _mudar(
    "pr.situacao = 'ativo' AND EXISTS (SELECT 1 FROM pessoas x WHERE x.id IN (pr.responsavel_id, pr.substituto_id)"
    " AND x.situacao <> 'ativa')"
)
SQL_PAUSAR_REVISAO = _mudar("pr.situacao = 'ativo' AND pr.revisado_em <= now() - make_interval(days => :dias)")
SQL_PAUSAR_CHAVE = _mudar("pr.situacao = 'ativo' AND pr.chave_trocada_em <= now() - make_interval(days => :dias)")
SQL_PAUSAR_SEM_USO = _mudar(
    "pr.situacao = 'ativo'"
    " AND greatest(a.visto_em, pr.revisado_em, pr.criado_em) <= now() - make_interval(days => :dias)"
)
SQL_CHAVE_A_VENCER = """
SELECT pr.aplicativo_id, pr.responsavel_id, pr.substituto_id, pr.fim, pr.revisado_em, pr.chave_trocada_em,
       a.nome AS programa, coalesce(r.nome, r.sujeito) AS responsavel, a.visto_em
FROM programas pr
JOIN aplicativos a ON a.id = pr.aplicativo_id
JOIN pessoas r ON r.id = pr.responsavel_id
WHERE pr.situacao = 'ativo'
  AND pr.chave_trocada_em + make_interval(days => :maximo) > now()
  AND pr.chave_trocada_em + make_interval(days => :maximo) <= now() + make_interval(days => :aviso)
ORDER BY pr.chave_trocada_em
LIMIT :lote
"""


@dataclass(frozen=True, slots=True)
class ProgramaMudou:
    aplicativo_id: uuid.UUID
    responsavel_id: uuid.UUID
    substituto_id: uuid.UUID
    programa: str
    responsavel: str
    fim: date
    revisado_em: datetime
    chave_trocada_em: datetime
    visto_em: datetime | None

    @classmethod
    def de_linha(cls, r: Row[Any]) -> ProgramaMudou:
        return cls(
            aplicativo_id=uuid.UUID(str(r.aplicativo_id)),
            responsavel_id=uuid.UUID(str(r.responsavel_id)),
            substituto_id=uuid.UUID(str(r.substituto_id)),
            programa=str(r.programa),
            responsavel=str(r.responsavel),
            fim=r.fim,
            revisado_em=r.revisado_em,
            chave_trocada_em=r.chave_trocada_em,
            visto_em=r.visto_em,
        )

    def campos(self) -> dict[str, object]:
        return {"programa": self.programa, "responsavel": self.responsavel}


def _data(valor: date | datetime) -> str:
    return valor.strftime("%d/%m/%Y")


@dataclass(frozen=True, slots=True)
class Passo:
    """Um passo de pausa ou encerramento: o SQL, a linha do extrato e o aviso."""

    nome: str
    sql: str
    tipo_extrato: str
    evento: str
    assunto: str
    nova: str

    def texto(self, m: ProgramaMudou, dias: int) -> str:
        motivos = {
            "programa_encerrado_fim": f"o cadastro dele valia ate {_data(m.fim)}",
            "programa_pausado_responsavel": "o responsavel ou o substituto nao esta mais ativo no MCP-CLARO",
            "programa_pausado_revisao": f"a revisao periodica, a cada {dias} dias, venceu sem confirmacao",
            "programa_pausado_chave": f"a chave dele passou de {dias} dias sem troca",
            "programa_pausado_sem_uso": f"ele ficou {dias} dias sem fazer nenhum pedido",
        }
        situacao = "foi encerrado" if self.nova == "encerrado" else "foi pausado"
        depois = (
            "Nada mais passa por ele. Se ainda for preciso, o ADMIN cadastra de novo."
            if self.nova == "encerrado"
            else "Ele nao le mais nada ate o ADMIN retomar, depois de conferir."
        )
        return f"O programa {m.programa} {situacao} no MCP-CLARO: {motivos[self.tipo_extrato]}. {depois}"


PASSOS: Final[tuple[Passo, ...]] = (
    Passo(
        "encerrados",
        SQL_ENCERRAR_NO_FIM,
        "programa_encerrado_fim",
        "programa_encerrado",
        "MCP-CLARO: um programa seu chegou ao fim",
        "encerrado",
    ),
    Passo(
        "sem_responsavel",
        SQL_PAUSAR_SEM_RESPONSAVEL,
        "programa_pausado_responsavel",
        "programa_pausado",
        "MCP-CLARO: um programa seu foi pausado",
        "pausado",
    ),
    Passo(
        "revisao_vencida",
        SQL_PAUSAR_REVISAO,
        "programa_pausado_revisao",
        "programa_revisao_vencida",
        "MCP-CLARO: a revisao de um programa seu venceu",
        "pausado",
    ),
    Passo(
        "chave_vencida",
        SQL_PAUSAR_CHAVE,
        "programa_pausado_chave",
        "programa_chave_vencida",
        "MCP-CLARO: a chave de um programa seu venceu",
        "pausado",
    ),
    Passo(
        "sem_uso",
        SQL_PAUSAR_SEM_USO,
        "programa_pausado_sem_uso",
        "programa_sem_uso",
        "MCP-CLARO: um programa seu foi pausado por falta de uso",
        "pausado",
    ),
)


def _dias_do_passo(passo: Passo, p: Parametros) -> int:
    return {
        "revisao_vencida": int(p.PROGRAM_OWNER_RECERT_DAYS),
        "chave_vencida": int(p.PROGRAM_KEY_MAX_AGE_DAYS),
        "sem_uso": int(p.PROGRAM_IDLE_PAUSE_DAYS),
    }.get(passo.nome, 0)


def _campos_do_passo(passo: Passo, m: ProgramaMudou, dias: int) -> dict[str, object]:
    if passo.tipo_extrato == "programa_encerrado_fim":
        return m.campos() | {"fim": _data(m.fim)}
    if passo.tipo_extrato in ("programa_pausado_revisao", "programa_pausado_chave", "programa_pausado_sem_uso"):
        return m.campos() | {"dias": dias}
    return m.campos()


async def _avisar_dupla(
    c: AsyncConnection, m: ProgramaMudou, evento: str, assunto: str, corpo: dict[str, Any], chave: str
) -> int:
    """Aviso ao responsavel e ao substituto, uma vez por ``chave``. Devolve quantos entraram."""
    novos = 0
    for pessoa_id in dict.fromkeys((m.responsavel_id, m.substituto_id)):
        novos += await avisar_pessoa(c, pessoa_id, evento, assunto, corpo, chave_unica=f"{chave}|{pessoa_id}")
    return novos


async def _rodar_passo(
    c: AsyncConnection, passo: Passo, p: Parametros, emails_admin: Sequence[str], link: str
) -> list[ProgramaMudou]:
    dias = _dias_do_passo(passo, p)
    valores: dict[str, object] = {"lote": LOTE, "nova": passo.nova}
    if ":dias" in passo.sql:
        valores["dias"] = dias
    if ":fuso" in passo.sql:
        valores["fuso"] = FUSO
    mudados = [ProgramaMudou.de_linha(r) for r in (await c.execute(text(passo.sql), valores)).all()]
    for m in mudados:
        await registrar(
            c,
            papel="rotinas",
            tipo=passo.tipo_extrato,
            campos=_campos_do_passo(passo, m, dias),
            aplicativo_id=m.aplicativo_id,
            pessoa_id=m.responsavel_id,
            dados={"situacao": passo.nova, "motivo": passo.nome, "dias": dias},
        )
        texto = passo.texto(m, dias)
        corpo: dict[str, Any] = {
            "texto": f"{texto} Veja em {link}.",
            "programa": m.programa,
            "aplicativo_id": str(m.aplicativo_id),
            "motivo": passo.nome,
            "situacao": passo.nova,
        }
        marca = f"programa:{passo.nome}:{m.aplicativo_id}:{m.revisado_em.isoformat()}"
        await _avisar_dupla(c, m, passo.evento, passo.assunto, corpo, marca)
        await avisar_admin(c, passo.evento, passo.assunto, corpo, marca, emails_admin)
    return mudados


async def _avisar_chave_a_vencer(c: AsyncConnection, p: Parametros, link: str) -> int:
    maximo = int(p.PROGRAM_KEY_MAX_AGE_DAYS)
    aviso = int(p.PROGRAM_KEY_EXPIRY_WARN_DAYS)
    linhas = (await c.execute(text(SQL_CHAVE_A_VENCER), {"maximo": maximo, "aviso": aviso, "lote": LOTE})).all()
    avisados = 0
    for r in linhas:
        m = ProgramaMudou.de_linha(r)
        vence = m.chave_trocada_em + timedelta(days=maximo)
        corpo: dict[str, Any] = {
            "texto": (
                f"A chave do programa {m.programa} vence em {_data(vence)}. Troque a chave no provedor de login e "
                f"peca ao ADMIN para registrar a troca no MCP-CLARO. Sem a troca, o programa pausa nesse dia. "
                f"Veja em {link}."
            ),
            "programa": m.programa,
            "aplicativo_id": str(m.aplicativo_id),
            "vence_em": vence.isoformat(),
        }
        marca = f"programa:chave_a_vencer:{m.aplicativo_id}:{m.chave_trocada_em.isoformat()}"
        novos = await _avisar_dupla(
            c, m, "programa_chave_a_vencer", "MCP-CLARO: a chave de um programa seu vence em breve", corpo, marca
        )
        if novos:
            avisados += 1
            await registrar(
                c,
                papel="rotinas",
                tipo="programa_chave_a_vencer",
                campos=m.campos() | {"fim": _data(vence)},
                aplicativo_id=m.aplicativo_id,
                pessoa_id=m.responsavel_id,
                dados={"vence_em": vence.isoformat(), "dias_de_aviso": aviso},
            )
    return avisados


async def limpar_contagens(c: AsyncConnection) -> int:
    """Apaga as contagens por minuto antigas: so o minuto corrente conta para o limite."""
    resultado = await c.execute(
        text("DELETE FROM programas_pedidos_minuto WHERE minuto < now() - make_interval(secs => :s)"),
        {"s": int(GUARDA_CONTAGEM.total_seconds())},
    )
    return int(resultado.rowcount or 0)


async def cuidar_dos_programas(engine: AsyncEngine, p: Parametros, emails_admin: Sequence[str] = ()) -> dict[str, int]:
    """Uma rodada das rotinas dos programas. Devolve as contagens de cada passo."""
    link = f"{p.PUBLIC_BASE_URL}/telas/programas"
    contagens: dict[str, int] = {}
    async with engine.begin() as c:
        for passo in PASSOS:
            contagens[passo.nome] = len(await _rodar_passo(c, passo, p, emails_admin, link))
        contagens["chave_a_vencer"] = await _avisar_chave_a_vencer(c, p, link)
        contagens["contagens_apagadas"] = await limpar_contagens(c)
    if any(v for k, v in contagens.items() if k != "contagens_apagadas"):
        log.info("programas_mudaram", **{k: v for k, v in contagens.items() if v})
    return contagens


def tarefas_dos_programas(engine: AsyncEngine, p: Parametros, config: ConfigRotinas) -> list[Tarefa]:
    """As tarefas dos programas, no formato do agendador (para ``rotinas/app.py``)."""
    return [
        Tarefa(
            "programas",
            config.intervalo_curto,
            lambda: cuidar_dos_programas(engine, p, config.emails_admin),
            config.prazo_tarefa,
        )
    ]
