"""Alarme de silencio: o vigia ve, de fora, se o gateway responde.

A cada ``WATCH_PING_SECONDS``, o vigia pede os metadados do recurso no endereco publico
(``/.well-known/oauth-protected-resource/mcp``). So a entrada do gateway responde isso: o portao
sozinho daria erro. Responde se vier 200 com o ``resource`` do MCP-CLARO.

Se o gateway ficar sem responder por ``WATCH_SILENCE_ALARM_SECONDS`` (contados da ultima resposta
boa, ou da partida do vigia), dispara o alarme ``silencio``, uma vez por silencio. Quando ele volta a
responder depois do alarme, sai o aviso ``voltou``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.vigia.alarmes import alarmar
from mcp_claro.vigia.config import ConfigVigia

log = logger("mcp_claro.vigia.silencio")

CAMINHO_METADADOS: Final = "/.well-known/oauth-protected-resource/mcp"


def _hora(instante: float) -> str:
    return datetime.fromtimestamp(instante, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class Pulso:
    def __init__(
        self, cfg: ConfigVigia, engine: AsyncEngine, recurso: str, *, relogio: Callable[[], float] = time.time
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.recurso = recurso
        self.relogio = relogio
        self.ultimo_ok = relogio()
        self.primeira_falha: float | None = None
        self.alarmado: float | None = None
        """Inicio do silencio que ja disparou alarme; ``None`` quando nao ha alarme aberto."""

    async def responde(self, http: httpx2.AsyncClient) -> bool:
        try:
            r = await http.get(f"{self.cfg.alvo}{CAMINHO_METADADOS}", headers={"accept": "application/json"})
        except httpx2.HTTPError:
            return False
        if r.status_code != 200:
            return False
        try:
            corpo = r.json()
        except ValueError:
            return False
        return isinstance(corpo, dict) and corpo.get("resource") == self.recurso

    async def bater(self, http: httpx2.AsyncClient) -> bool:
        """Um pulso. Devolve se o gateway respondeu; dispara ou encerra o alarme de silencio."""
        ok = await self.responde(http)
        agora = self.relogio()
        if ok:
            CONTADORES.somar("vigia_pulso", desfecho="responde")
            if self.alarmado is not None:
                inicio = self.alarmado
                async with self.engine.begin() as c:
                    await alarmar(
                        c,
                        self.cfg,
                        "voltou",
                        f"voltou:{int(inicio)}",
                        f"O gateway voltou a responder em {_hora(agora)}, depois de "
                        f"{int(agora - self.ultimo_ok)} segundos sem resposta.",
                        {"silencio_desde": _hora(inicio), "segundos": int(agora - self.ultimo_ok)},
                    )
                log.info("vigia_gateway_voltou")
            self.ultimo_ok = agora
            self.primeira_falha = None
            self.alarmado = None
            return True
        CONTADORES.somar("vigia_pulso", desfecho="mudo")
        if self.primeira_falha is None:
            self.primeira_falha = agora
        mudo_ha = agora - self.ultimo_ok
        if self.alarmado is None and mudo_ha >= self.cfg.silencio:
            inicio = self.primeira_falha
            async with self.engine.begin() as c:
                await alarmar(
                    c,
                    self.cfg,
                    "silencio",
                    f"silencio:{int(inicio)}",
                    f"O gateway nao responde em {self.cfg.alvo} ha {int(mudo_ha)} segundos.",
                    {"desde": _hora(self.ultimo_ok), "segundos": int(mudo_ha)},
                )
            self.alarmado = inicio
        return False
