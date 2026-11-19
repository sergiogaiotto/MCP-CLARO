"""Rotinas da Fase 2: fichas que vencem, aviso de ficha pendente, limites que vencem e desafios de passkey.

- ``vencer_fichas``: ficha sem resposta no prazo vence, e nada segue (``fila.vencer_fichas``);
- ``avisar_fichas_pendentes``: ficha sem decisao ha mais de ``NOTIFY_PENDING_AFTER_MINUTES`` recebe um
  aviso a pessoa (e-mail, se a pessoa tiver, e os webhooks cadastrados que assinam o evento), uma vez por
  ficha. O aviso leva protocolo, ferramenta, sistema, prazo e o link da ficha; nunca argumentos nem
  resultado. O extrato so diz que a pessoa foi avisada quando o e-mail dela entrou na fila; aviso que foi
  so aos webhooks da instalacao tem frase propria;
- ``vencer_limites``: limite ativo com prazo no passado vira ``vencido``;
- ``limpar_desafios_passkey``: apaga os desafios de passkey vencidos ou ja usados (cada um vale uma vez).

"Uma vez" vale mesmo com duas copias das rotinas no ar: alem da trava do agendador, cada linha e pega com
``FOR UPDATE SKIP LOCKED`` e mudada com ``UPDATE ... WHERE`` o estado de antes, na mesma transacao do aviso
e do extrato. Quem chega depois nao acha nada para mudar.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos.destinos import avisar_pessoa, destino_webhook, email_da_pessoa
from mcp_claro.avisos.fila import enfileirar
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.rotinas.agendador import Tarefa
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.saida import fila
from mcp_claro.saida.protocolo import FUSO

log = logger("mcp_claro.rotinas.acoes")

LOTE: Final = 500
EVENTO_FICHA_PENDENTE: Final = "ficha_pendente"
MOTIVO_LIMITE_VENCIDO: Final = "o prazo do limite acabou"

INTERVALO_FICHAS: Final = 30.0
"""Vencer fichas: a cada volta curta, para a ficha vencida sair logo das pendencias."""
INTERVALO_AVISO_FICHAS: Final = 60.0
"""Aviso de ficha pendente: o prazo do aviso e contado em minutos."""
INTERVALO_LIMITES: Final = 300.0
"""Vencer limites: o pedido (``fila.gastar_limite``) ja recusa limite fora do prazo; esta rotina so poe a
situacao em dia. A acao ja liberada por um limite que depois venceu pelo prazo segue a espera visivel e sai,
a menos que a pessoa cancele ou bloqueie; o despachante so a para se o limite foi encerrado ou suspenso."""
INTERVALO_DESAFIOS: Final = 3600.0
"""Limpar desafios de passkey: uma vez por hora basta, cada desafio vale poucos minutos."""


# --- Fichas que vencem -----------------------------------------------------------------------


async def vencer_fichas(engine: AsyncEngine) -> list[str]:
    """Uma rodada: fichas sem resposta no prazo vencem. Devolve os protocolos vencidos."""
    async with engine.begin() as c:
        vencidas = await fila.vencer_fichas(c, papel="rotinas")
    if vencidas:
        log.info("fichas_vencidas", fichas=len(vencidas))
    return vencidas


# --- Aviso de ficha pendente -----------------------------------------------------------------

SQL_MARCAR_AVISADAS = """
WITH alvo AS (
  SELECT fi.id FROM fichas fi
  JOIN fila_saida f ON f.ficha_id = fi.id
  WHERE fi.decisao IS NULL AND fi.avisada_em IS NULL AND fi.expira_em > now()
    AND fi.montada_em <= now() - make_interval(mins => :minutos)
    AND f.estado = 'aguardando_ficha'
  ORDER BY fi.montada_em, fi.id
  LIMIT :lote
  FOR UPDATE OF fi SKIP LOCKED
), marcada AS (
  UPDATE fichas fi SET avisada_em = now()
  FROM alvo WHERE fi.id = alvo.id AND fi.avisada_em IS NULL
  RETURNING fi.id, fi.operacao_id, fi.expira_em
)
SELECT m.id AS ficha_id, m.expira_em, o.protocolo, o.pessoa_id, o.aplicativo_id, o.sistema_id,
       r.nome_vitrine, s.nome AS sistema_nome, a.nome AS aplicativo_nome
FROM marcada m
JOIN operacoes o ON o.id = m.operacao_id
JOIN ferramentas_revisadas r ON r.id = o.ferramenta_id
JOIN sistemas s ON s.id = o.sistema_id
JOIN aplicativos a ON a.id = o.aplicativo_id
ORDER BY m.expira_em, m.id
"""


def link_da_ficha(base_url: str, ficha_id: uuid.UUID) -> str:
    return f"{base_url.rstrip('/')}/telas/fichas/{ficha_id}"


def _prazo_legivel(quando: datetime) -> str:
    return quando.astimezone(ZoneInfo(FUSO)).strftime("%d/%m/%Y as %H:%M")


async def _webhooks_do_evento(c: AsyncConnection, evento: str) -> list[uuid.UUID]:
    """Destinos de webhook ativos que assinam o evento (lista de eventos vazia quer dizer "todos")."""
    linhas = await c.execute(
        text(
            "SELECT id FROM destinos_webhook WHERE ativo AND (cardinality(eventos) = 0 OR :e = ANY (eventos))"
            " ORDER BY nome"
        ),
        {"e": evento},
    )
    return [uuid.UUID(str(i)) for i in linhas.scalars()]


async def avisar_fichas_pendentes(engine: AsyncEngine, *, apos_minutos: int, base_url: str) -> int:
    """Uma rodada: avisa cada ficha sem decisao ha mais de ``apos_minutos``, uma vez por ficha.

    Marca ``avisada_em`` e poe os avisos na fila na mesma transacao. A ficha sem destino nenhum (pessoa
    sem e-mail e nenhum webhook) tambem fica marcada, para nao ser procurada de novo, mas nao vai ao
    extrato como avisada. Devolve quantos avisos entraram na fila.
    """
    novos = 0
    fichas = 0
    async with engine.begin() as c:
        linhas = (await c.execute(text(SQL_MARCAR_AVISADAS), {"minutos": apos_minutos, "lote": LOTE})).all()
        webhooks = await _webhooks_do_evento(c, EVENTO_FICHA_PENDENTE) if linhas else []
        for r in linhas:
            ficha_id = uuid.UUID(str(r.ficha_id))
            pessoa_id = uuid.UUID(str(r.pessoa_id))
            prazo = _prazo_legivel(r.expira_em)
            link = link_da_ficha(base_url, ficha_id)
            assunto = "MCP-CLARO: uma acao espera a sua confirmacao"
            corpo = {
                "texto": (
                    f"O aplicativo {r.aplicativo_nome} pediu a acao {r.nome_vitrine} no sistema {r.sistema_nome},"
                    f" protocolo {r.protocolo}. Nada saiu ainda. Confira e decida na ficha ate {prazo}"
                    f" (horario de Brasilia): {link}. Se ninguem decidir ate la, a ficha vence e nada segue."
                ),
                "linhas": [
                    f"Protocolo: {r.protocolo}",
                    f"Ferramenta: {r.nome_vitrine}",
                    f"Sistema: {r.sistema_nome}",
                    f"Prazo: {prazo} (horario de Brasilia)",
                    f"Ficha: {link}",
                ],
                "protocolo": r.protocolo,
                "ferramenta": r.nome_vitrine,
                "sistema": r.sistema_nome,
                "prazo": r.expira_em.isoformat(),
                "link": link,
                "ficha_id": str(ficha_id),
            }
            chave = f"{EVENTO_FICHA_PENDENTE}:{ficha_id}"
            pessoa_avisada = 0
            if await email_da_pessoa(c, pessoa_id) is not None:
                pessoa_avisada = await avisar_pessoa(c, pessoa_id, EVENTO_FICHA_PENDENTE, assunto, corpo, chave)
            entraram = pessoa_avisada
            for destino_id in webhooks:
                alvo = destino_webhook(destino_id)
                entraram += await enfileirar(
                    c, "webhook", alvo, EVENTO_FICHA_PENDENTE, assunto, corpo, f"{chave}|{alvo}"
                )
            if entraram == 0:
                log.info("ficha_pendente_sem_destino")
                continue
            novos += entraram
            fichas += 1
            # "A pessoa foi avisada" so quando o aviso foi a ela (e-mail). Os webhooks sao da instalacao e nao
            # dizem de quem e a ficha: ai a frase e outra.
            await registrar(
                c,
                papel="rotinas",
                tipo="ficha_avisada" if pessoa_avisada else "ficha_avisada_webhooks",
                campos={"protocolo": r.protocolo},
                protocolo=r.protocolo,
                pessoa_id=pessoa_id,
                aplicativo_id=uuid.UUID(str(r.aplicativo_id)),
                sistema_id=uuid.UUID(str(r.sistema_id)),
                ferramenta=r.nome_vitrine,
                dados={"ficha_id": str(ficha_id), "avisos": entraram},
            )
    if fichas:
        log.info("fichas_avisadas", fichas=fichas, avisos=novos)
    return novos


# --- Limites que vencem ----------------------------------------------------------------------

SQL_VENCER_LIMITES = """
WITH alvo AS (
  SELECT id FROM limites
  WHERE situacao = 'ativo' AND valido_ate <= now()
  ORDER BY valido_ate, id
  LIMIT :lote
  FOR UPDATE SKIP LOCKED
), mudou AS (
  UPDATE limites l SET situacao = 'vencido', motivo = :motivo
  FROM alvo WHERE l.id = alvo.id AND l.situacao = 'ativo'
  RETURNING l.id, l.pessoa_id, l.aplicativo_id, l.sistema_id, l.nome_original, l.valido_ate
)
SELECT mudou.id, mudou.pessoa_id, mudou.aplicativo_id, mudou.sistema_id, mudou.valido_ate,
       coalesce(
         (SELECT r.nome_vitrine FROM ferramentas_revisadas r
          WHERE r.sistema_id = mudou.sistema_id AND r.nome_original = mudou.nome_original
          ORDER BY (r.situacao = 'ativa') DESC, r.versao DESC LIMIT 1),
         mudou.nome_original) AS ferramenta
FROM mudou
ORDER BY mudou.valido_ate, mudou.id
"""


async def vencer_limites(engine: AsyncEngine) -> int:
    """Uma rodada: limite ativo com prazo no passado vira ``vencido``. Devolve quantos venceram."""
    async with engine.begin() as c:
        linhas = (await c.execute(text(SQL_VENCER_LIMITES), {"lote": LOTE, "motivo": MOTIVO_LIMITE_VENCIDO})).all()
        for r in linhas:
            await registrar(
                c,
                papel="rotinas",
                tipo="limite_encerrado",
                campos={"ferramenta": r.ferramenta, "motivo": MOTIVO_LIMITE_VENCIDO},
                pessoa_id=uuid.UUID(str(r.pessoa_id)),
                aplicativo_id=uuid.UUID(str(r.aplicativo_id)),
                sistema_id=uuid.UUID(str(r.sistema_id)),
                ferramenta=r.ferramenta,
                dados={"limite_id": str(r.id), "valido_ate": r.valido_ate.isoformat(), "situacao": "vencido"},
            )
    if linhas:
        log.info("limites_vencidos", limites=len(linhas))
    return len(linhas)


# --- Desafios de passkey vencidos ou usados --------------------------------------------------

SQL_LIMPAR_DESAFIOS = """
DELETE FROM desafios_passkey
WHERE desafio IN (
  SELECT desafio FROM desafios_passkey
  WHERE expira_em < now() OR usado_em IS NOT NULL
  LIMIT :lote
)
"""


async def limpar_desafios_passkey(engine: AsyncEngine) -> int:
    """Uma rodada: apaga desafios de passkey vencidos ou ja usados. Devolve quantos foram apagados.

    Um desafio vale uma vez e por poucos minutos: depois disso nao serve para nada, e a cerimonia que o
    usar e recusada por nao acha-lo. Nada vai ao extrato (o desafio nao e um passo da pessoa)."""
    async with engine.begin() as c:
        apagados = (await c.execute(text(SQL_LIMPAR_DESAFIOS), {"lote": LOTE * 10})).rowcount
    if apagados:
        log.info("desafios_passkey_limpos", desafios=apagados)
    return int(apagados or 0)


# --- Tarefas do agendador --------------------------------------------------------------------


def tarefas_de_acoes(engine: AsyncEngine, p: Parametros, config: ConfigRotinas) -> list[Tarefa]:
    """As tarefas da Fase 2 no formato do agendador. Rodam com ou sem ``FEATURE_PHASE2_ACT``: fichas de
    pessoas de teste tambem vencem e avisam."""
    prazo = config.prazo_tarefa
    minutos = int(p.NOTIFY_PENDING_AFTER_MINUTES)
    base_url = str(p.PUBLIC_BASE_URL)
    return [
        Tarefa("vencer_fichas", INTERVALO_FICHAS, lambda: vencer_fichas(engine), prazo),
        Tarefa(
            "avisar_fichas_pendentes",
            INTERVALO_AVISO_FICHAS,
            lambda: avisar_fichas_pendentes(engine, apos_minutos=minutos, base_url=base_url),
            prazo,
        ),
        Tarefa("vencer_limites", min(config.intervalo_curto, INTERVALO_LIMITES), lambda: vencer_limites(engine), prazo),
        Tarefa(
            "limpar_desafios_passkey",
            min(config.intervalo_limpeza, INTERVALO_DESAFIOS),
            lambda: limpar_desafios_passkey(engine),
            prazo,
        ),
    ]
