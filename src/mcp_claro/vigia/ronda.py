"""Ronda de fora: o vigia repete a ronda das portas pelo endereco publico (``WATCH_PUBLIC_URL``).

Os casos sao os mesmos da ronda das rotinas (``mcp_claro.ronda.casos``), com a identidade do vigia
(``mcp-claro-vigia``) e a chave propria dele para o cracha falso. Cada tentativa vai a
``rondas_vigia`` (so de acrescentar). O vigia nao fecha a entrada (fica em outra maquina, sem acesso
ao banco do gateway): com ``PATROL_FAILS_TO_CLOSE`` rondas seguidas com passagem e controle em dia,
dispara o alarme ``ronda``, uma vez por sequencia.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.ronda.casos import Caso, Resumo, Situacao, Tentativa, fazer_tentativas
from mcp_claro.ronda.cracha import ChavesProprias, FonteCrachas
from mcp_claro.ronda.rodada import SQL_RODADAS_VIGIA, rodadas_seguidas
from mcp_claro.vigia.alarmes import alarmar
from mcp_claro.vigia.config import ConfigVigia

log = logger("mcp_claro.vigia.ronda")

_SQL_GUARDAR = (
    "INSERT INTO rondas_vigia (rodada, caso, esperado, obtido, defendeu, detalhes)"
    " VALUES (:r, :c, :e, :o, :d, CAST(:x AS jsonb))"
)


@dataclass(frozen=True, slots=True)
class RondaVigia:
    id: uuid.UUID
    tentativas: list[Tentativa]
    seguidas: int
    alarmou: bool

    @property
    def resumo(self) -> Resumo:
        return Resumo(self.tentativas)


class RondaDeFora:
    def __init__(
        self, cfg: ConfigVigia, engine: AsyncEngine, fonte: FonteCrachas | None, *, emissor: str, recurso: str
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.fonte = fonte
        self.emissor = emissor
        self.recurso = recurso
        self.chaves = ChavesProprias()

    async def rodar(self, http: httpx2.AsyncClient) -> RondaVigia:
        rodada = uuid.uuid4()
        if self.fonte is None:
            tentativas = [
                Tentativa(
                    Caso.CONTROLE,
                    "cracha aceito (resposta JSON-RPC)",
                    "sem identidade do vigia (WATCH_KEY_FILE)",
                    Situacao.INCONCLUSIVO,
                    {"por_que": "sem identidade do vigia"},
                )
            ]
        else:
            tentativas = await fazer_tentativas(
                self.cfg.alvo,
                self.fonte,
                http,
                emissor=self.emissor,
                recurso=self.recurso,
                ferramenta_alheia=self.cfg.ferramenta_alheia,
                chaves=self.chaves,
            )
        resumo = Resumo(tentativas)
        alarmou = False
        async with self.engine.begin() as c:
            for t in tentativas:
                await c.execute(
                    text(_SQL_GUARDAR),
                    {
                        "r": rodada,
                        "c": t.caso,
                        "e": t.esperado[:200],
                        "o": t.obtido[:200],
                        "d": t.defendeu,
                        "x": json.dumps(t.para_tabela(), ensure_ascii=False, sort_keys=True),
                    },
                )
        async with self.engine.begin() as c:
            seguidas = await rodadas_seguidas(c, None, sql=SQL_RODADAS_VIGIA)
            if seguidas == self.cfg.falhas_para_alarme:
                alarmou = await alarmar(
                    c,
                    self.cfg,
                    "ronda",
                    f"ronda:{rodada}",
                    f"A ronda de fora achou passagem indevida em {seguidas} rondas seguidas: "
                    f"{resumo.motivo_legivel()}. O vigia nao fecha a entrada; a equipe precisa apurar.",
                    {"rondas": seguidas, "casos": ", ".join(t.caso for t in resumo.passagens)},
                )
        desfecho = "passou" if resumo.falhou else ("conforme" if resumo.controle_ok else "inconclusiva")
        CONTADORES.somar("vigia_rondas", desfecho=desfecho)
        if resumo.falhou:
            log.warning("vigia_ronda_passagem_indevida", casos=[t.caso for t in resumo.passagens], seguidas=seguidas)
        elif not resumo.controle_ok:
            # Sem controle em dia: a entrada nao devolveu a vitrine do vigia (programa dele pausado ou sem cadastro,
            # limite por minuto, entrada fechada, bloqueio ou pausa geral). A vitrine nao foi testada.
            log.warning("vigia_ronda_inconclusiva")
        return RondaVigia(rodada, tentativas, seguidas, alarmou)
