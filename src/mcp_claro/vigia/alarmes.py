"""Alarmes do vigia: uma linha em ``alarmes`` (so de acrescentar) e avisos pelos canais proprios do vigia.

Cada alarme tem uma ``chave`` unica: o mesmo problema visto de novo na volta seguinte nao repete o
alarme. Os avisos entram na fila ``avisos`` do banco do vigia, na mesma transacao, um por e-mail de
``WATCH_ALARM_EMAILS`` e um por webhook de ``WATCH_WEBHOOK_TARGETS``. O envio e o mesmo das rotinas
(``mcp_claro.avisos``: novas tentativas, Standard Webhooks), com o SMTP e o segredo do vigia. Nada
depende do Grafana nem das rotinas do gateway.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.avisos import enfileirar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.vigia.config import ConfigVigia

log = logger("mcp_claro.vigia.alarmes")

TIPOS: Final = frozenset(
    {
        "silencio",
        "voltou",
        "ronda",
        "encadeamento_quebrado",
        "fechamento_mudou",
        "fechamento_sumiu",
        "fechamentos_sem_resposta",
    }
)

ASSUNTOS: Final[dict[str, str]] = {
    "silencio": "MCP-CLARO: o gateway nao responde",
    "voltou": "MCP-CLARO: o gateway voltou a responder",
    "ronda": "MCP-CLARO: a ronda de fora achou passagem indevida",
    "encadeamento_quebrado": "MCP-CLARO: o fechamento novo nao encadeia no guardado",
    "fechamento_mudou": "MCP-CLARO: um fechamento ja guardado mudou no gateway",
    "fechamento_sumiu": "MCP-CLARO: um fechamento ja guardado sumiu do gateway",
    "fechamentos_sem_resposta": "MCP-CLARO: o vigia nao consegue buscar os fechamentos",
}


async def alarmar(
    c: AsyncConnection,
    cfg: ConfigVigia,
    tipo: str,
    chave: str,
    resumo: str,
    dados: Mapping[str, Any] | None = None,
) -> bool:
    """Grava o alarme e poe os avisos na fila, na transacao de ``c``. ``False`` se a chave ja existia."""
    if tipo not in TIPOS:
        raise ValueError(f"tipo de alarme desconhecido: {tipo}")
    corpo_dados = dict(dados or {})
    novo = (
        await c.execute(
            text(
                "INSERT INTO alarmes (tipo, chave, resumo, dados) VALUES (:t, :k, :r, CAST(:d AS jsonb))"
                " ON CONFLICT (chave) DO NOTHING RETURNING id"
            ),
            {"t": tipo, "k": chave[:300], "r": resumo[:500], "d": json.dumps(corpo_dados, sort_keys=True)},
        )
    ).scalar_one_or_none()
    if novo is None:
        return False
    corpo = {"texto": resumo, "linhas": [f"{k}: {v}" for k, v in sorted(corpo_dados.items())], "tipo": tipo}
    destinos = [("email", e) for e in cfg.emails] + [("webhook", w) for w in cfg.webhooks]
    for canal, destino in destinos:
        await enfileirar(c, canal, destino, f"vigia_{tipo}", ASSUNTOS[tipo], corpo, f"{chave[:200]}|{destino}")
    if not destinos:
        log.warning("alarme_sem_destino", tipo=tipo)
    CONTADORES.somar("vigia_alarmes", tipo=tipo)
    log.warning("vigia_alarme", tipo=tipo, alarme=int(novo))
    return True
