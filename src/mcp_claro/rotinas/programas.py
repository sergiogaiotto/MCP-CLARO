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
nome do programa, o motivo e datas: nada do que o programa leu. O aviso ao ADMIN leva o link da tela do ADMIN
(``/telas/admin/programas/<id>``); o do responsavel, o da tela dele (``/telas/programas``).

A ronda e o vigia (``PATROL_CLIENT_ID`` e ``WATCH_CLIENT_ID``) sao programas que testam as portas: as rotinas
nunca os pausam nem os encerram. Quando um deles cumpre a condicao de um passo, a rotina so avisa, com
destaque, o ADMIN, o responsavel e o substituto, uma vez por situacao, e registra no extrato. O ADMIN resolve
pela tela do programa (confirmar a revisao, registrar a troca da chave, trocar o responsavel ou estender a
data de fim). Enquanto o cadastro nao vale, a ronda da inconclusiva (``ronda/casos.py``).
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
from mcp_claro.ronda.config import CLIENTE_RONDA, CLIENTE_VIGIA, ler
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
    """Muda a situacao (``:nova``) dos programas que cumprem ``onde``, uma condicao fixa deste modulo.

    Nunca muda a ronda nem o vigia (``:protegidos``): para eles, ``_achar_das_portas`` acha e a rotina avisa.
    """
    return (
        "WITH alvo AS (SELECT pr.aplicativo_id FROM programas pr JOIN aplicativos a ON a.id = pr.aplicativo_id"  # noqa: S608
        f" WHERE {onde} AND a.client_id <> ALL (CAST(:protegidos AS text[]))"
        " ORDER BY pr.aplicativo_id LIMIT :lote FOR UPDATE OF pr SKIP LOCKED),"
        " mudou AS (UPDATE programas p SET situacao = :nova FROM alvo WHERE p.aplicativo_id = alvo.aplicativo_id"
        " RETURNING p.aplicativo_id, p.responsavel_id, p.substituto_id, p.fim, p.revisado_em, p.chave_trocada_em)"
        + _DEVOLVE
    )


def _achar_das_portas(onde: str) -> str:
    """A ronda e o vigia (``:protegidos``) que cumprem ``onde``: so le, nao muda nada."""
    return (
        "SELECT pr.aplicativo_id, pr.responsavel_id, pr.substituto_id, pr.fim, pr.revisado_em,"  # noqa: S608
        " pr.chave_trocada_em, a.nome AS programa, coalesce(r.nome, r.sujeito) AS responsavel, a.visto_em"
        " FROM programas pr JOIN aplicativos a ON a.id = pr.aplicativo_id JOIN pessoas r ON r.id = pr.responsavel_id"
        f" WHERE {onde} AND a.client_id = ANY (CAST(:protegidos AS text[])) ORDER BY pr.aplicativo_id LIMIT :lote"
    )


ONDE_ENCERRAR_NO_FIM = "pr.situacao IN ('ativo', 'pausado') AND pr.fim < (now() AT TIME ZONE :fuso)::date"
ONDE_SEM_RESPONSAVEL = (
    "pr.situacao = 'ativo' AND EXISTS (SELECT 1 FROM pessoas x WHERE x.id IN (pr.responsavel_id, pr.substituto_id)"
    " AND x.situacao <> 'ativa')"
)
ONDE_REVISAO = "pr.situacao = 'ativo' AND pr.revisado_em <= now() - make_interval(days => :dias)"
ONDE_CHAVE = "pr.situacao = 'ativo' AND pr.chave_trocada_em <= now() - make_interval(days => :dias)"
ONDE_SEM_USO = (
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


EVENTO_DAS_PORTAS: Final = "programa_das_portas_atencao"
ASSUNTO_DAS_PORTAS: Final = "MCP-CLARO: IMPORTANTE - a ronda ou o vigia precisa do ADMIN"

# O que o ADMIN faz quando a ronda ou o vigia cumpre a condicao de um passo (a rotina nao pausa nem encerra).
_O_QUE_FAZER_DAS_PORTAS: Final[dict[str, str]] = {
    "encerrados": (
        "Estenda a data de fim na tela do programa. Ate la, a entrada recusa os pedidos dele e a ronda das portas"
        " fica inconclusiva."
    ),
    "sem_responsavel": (
        "Troque o responsavel e o substituto na tela do programa. Ate la, a entrada recusa os pedidos dele e a"
        " ronda das portas fica inconclusiva."
    ),
    "revisao_vencida": "Confirme na tela do programa que ele segue valendo.",
    "chave_vencida": "Troque a chave no provedor de login e registre a troca na tela do programa.",
    "sem_uso": "Confira se a ronda e o vigia estao rodando: este programa parou de fazer pedidos.",
}


@dataclass(frozen=True, slots=True)
class Passo:
    """Um passo de pausa ou encerramento: a condicao, a linha do extrato e o aviso."""

    nome: str
    onde: str
    tipo_extrato: str
    evento: str
    assunto: str
    nova: str

    @property
    def sql(self) -> str:
        """Muda a situacao dos programas que cumprem a condicao, menos a ronda e o vigia."""
        return _mudar(self.onde)

    @property
    def sql_das_portas(self) -> str:
        """Acha a ronda e o vigia que cumprem a condicao, sem mudar nada."""
        return _achar_das_portas(self.onde)

    def motivo(self, m: ProgramaMudou, dias: int) -> str:
        return {
            "programa_encerrado_fim": f"o cadastro dele valia ate {_data(m.fim)}",
            "programa_pausado_responsavel": "o responsavel ou o substituto nao esta mais ativo no MCP-CLARO",
            "programa_pausado_revisao": f"a revisao periodica, a cada {dias} dias, venceu sem confirmacao",
            "programa_pausado_chave": f"a chave dele passou de {dias} dias sem troca",
            "programa_pausado_sem_uso": f"ele ficou {dias} dias sem fazer nenhum pedido",
        }[self.tipo_extrato]

    def texto(self, m: ProgramaMudou, dias: int) -> str:
        situacao = "foi encerrado" if self.nova == "encerrado" else "foi pausado"
        depois = (
            "Nada mais passa por ele. Se ainda for preciso, o ADMIN cadastra o programa de novo, com o mesmo client_id."
            if self.nova == "encerrado"
            else "Ele nao le mais nada ate o ADMIN retomar, depois de conferir."
        )
        return f"O programa {m.programa} {situacao} no MCP-CLARO: {self.motivo(m, dias)}. {depois}"

    @property
    def acao(self) -> str:
        """O que a rotina faria (e nao faz) com a ronda ou o vigia: "pausou" ou "encerrou"."""
        return "encerrou" if self.nova == "encerrado" else "pausou"

    def texto_das_portas(self, m: ProgramaMudou, dias: int) -> str:
        return (
            f"IMPORTANTE: o programa {m.programa} testa as portas do MCP-CLARO (ronda ou vigia), por isso a rotina"
            f" nao o {self.acao}: {self.motivo(m, dias)}. {_O_QUE_FAZER_DAS_PORTAS[self.nome]}"
        )

    def situacao_das_portas(self, m: ProgramaMudou) -> str:
        """O que identifica esta situacao da ronda ou do vigia: muda quando o ADMIN resolve. Um aviso por situacao."""
        if self.nome == "encerrados":
            return m.fim.isoformat()
        if self.nome == "sem_responsavel":
            return f"{m.responsavel_id}:{m.substituto_id}"
        if self.nome == "chave_vencida":
            return m.chave_trocada_em.isoformat()
        if self.nome == "sem_uso":
            return f"{m.visto_em.isoformat() if m.visto_em else 'nunca'}:{m.revisado_em.isoformat()}"
        return m.revisado_em.isoformat()


PASSOS: Final[tuple[Passo, ...]] = (
    Passo(
        "encerrados",
        ONDE_ENCERRAR_NO_FIM,
        "programa_encerrado_fim",
        "programa_encerrado",
        "MCP-CLARO: um programa seu chegou ao fim",
        "encerrado",
    ),
    Passo(
        "sem_responsavel",
        ONDE_SEM_RESPONSAVEL,
        "programa_pausado_responsavel",
        "programa_pausado",
        "MCP-CLARO: um programa seu foi pausado",
        "pausado",
    ),
    Passo(
        "revisao_vencida",
        ONDE_REVISAO,
        "programa_pausado_revisao",
        "programa_revisao_vencida",
        "MCP-CLARO: a revisao de um programa seu venceu",
        "pausado",
    ),
    Passo(
        "chave_vencida",
        ONDE_CHAVE,
        "programa_pausado_chave",
        "programa_chave_vencida",
        "MCP-CLARO: a chave de um programa seu venceu",
        "pausado",
    ),
    Passo(
        "sem_uso",
        ONDE_SEM_USO,
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


def protegidos(p: Parametros) -> list[str]:
    """client_id da ronda e do vigia: as rotinas nunca os pausam nem os encerram."""
    return [str(ler(p, "PATROL_CLIENT_ID", CLIENTE_RONDA)), str(ler(p, "WATCH_CLIENT_ID", CLIENTE_VIGIA))]


def link_do_responsavel(p: Parametros) -> str:
    return f"{p.PUBLIC_BASE_URL.rstrip('/')}/telas/programas"


def link_do_admin(p: Parametros, aplicativo_id: uuid.UUID) -> str:
    return f"{p.PUBLIC_BASE_URL.rstrip('/')}/telas/admin/programas/{aplicativo_id}"


def _valores(passo: Passo, p: Parametros, sql: str) -> dict[str, object]:
    valores: dict[str, object] = {"lote": LOTE, "protegidos": protegidos(p)}
    if ":nova" in sql:
        valores["nova"] = passo.nova
    if ":dias" in sql:
        valores["dias"] = _dias_do_passo(passo, p)
    if ":fuso" in sql:
        valores["fuso"] = FUSO
    return valores


def _corpo(m: ProgramaMudou, passo: Passo, texto: str, link: str, situacao: str) -> dict[str, Any]:
    return {
        "texto": f"{texto} Veja em {link}.",
        "programa": m.programa,
        "aplicativo_id": str(m.aplicativo_id),
        "motivo": passo.nome,
        "situacao": situacao,
    }


async def _rodar_passo(
    c: AsyncConnection, passo: Passo, p: Parametros, emails_admin: Sequence[str]
) -> list[ProgramaMudou]:
    dias = _dias_do_passo(passo, p)
    sql = passo.sql
    mudados = [ProgramaMudou.de_linha(r) for r in (await c.execute(text(sql), _valores(passo, p, sql))).all()]
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
        marca = f"programa:{passo.nome}:{m.aplicativo_id}:{m.revisado_em.isoformat()}"
        corpo = _corpo(m, passo, texto, link_do_responsavel(p), passo.nova)
        await _avisar_dupla(c, m, passo.evento, passo.assunto, corpo, marca)
        corpo_admin = _corpo(m, passo, texto, link_do_admin(p, m.aplicativo_id), passo.nova)
        await avisar_admin(c, passo.evento, passo.assunto, corpo_admin, marca, emails_admin)
    return mudados


async def _avisar_das_portas(
    c: AsyncConnection, passo: Passo, p: Parametros, emails_admin: Sequence[str]
) -> list[ProgramaMudou]:
    """A ronda e o vigia que cumprem a condicao do passo: nao muda nada, so avisa com destaque e registra.

    Um aviso e uma linha do extrato por situacao (``Passo.situacao_das_portas``): a rodada seguinte nao repete.
    Devolve os que foram avisados agora.
    """
    dias = _dias_do_passo(passo, p)
    sql = passo.sql_das_portas
    achados = [ProgramaMudou.de_linha(r) for r in (await c.execute(text(sql), _valores(passo, p, sql))).all()]
    avisados: list[ProgramaMudou] = []
    for m in achados:
        texto = passo.texto_das_portas(m, dias)
        marca = f"programa:portas:{passo.nome}:{m.aplicativo_id}:{passo.situacao_das_portas(m)}"
        situacao = "sem_mudanca"
        corpo = _corpo(m, passo, texto, link_do_responsavel(p), situacao)
        novos = await _avisar_dupla(c, m, EVENTO_DAS_PORTAS, ASSUNTO_DAS_PORTAS, corpo, marca)
        corpo_admin = _corpo(m, passo, texto, link_do_admin(p, m.aplicativo_id), situacao)
        novos += await avisar_admin(c, EVENTO_DAS_PORTAS, ASSUNTO_DAS_PORTAS, corpo_admin, marca, emails_admin)
        if not novos:
            continue  # ja avisado nesta situacao
        avisados.append(m)
        await registrar(
            c,
            papel="rotinas",
            tipo="programa_das_portas_mantido",
            campos=m.campos() | {"acao": passo.acao, "motivo": passo.motivo(m, dias)},
            aplicativo_id=m.aplicativo_id,
            pessoa_id=m.responsavel_id,
            dados={"motivo": passo.nome, "dias": dias, "marca": marca},
        )
        log.warning("programa_das_portas_precisa_do_admin", passo=passo.nome)
    return avisados


async def _avisar_chave_a_vencer(c: AsyncConnection, p: Parametros) -> int:
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
                f"Veja em {link_do_responsavel(p)}."
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
    """Uma rodada das rotinas dos programas. Devolve as contagens de cada passo.

    ``das_portas`` conta os avisos novos sobre a ronda e o vigia, que as rotinas nunca pausam nem encerram.
    """
    contagens: dict[str, int] = {"das_portas": 0}
    async with engine.begin() as c:
        for passo in PASSOS:
            contagens[passo.nome] = len(await _rodar_passo(c, passo, p, emails_admin))
            contagens["das_portas"] += len(await _avisar_das_portas(c, passo, p, emails_admin))
        contagens["chave_a_vencer"] = await _avisar_chave_a_vencer(c, p)
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
