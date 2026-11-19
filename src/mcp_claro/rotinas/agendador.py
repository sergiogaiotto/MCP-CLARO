"""Agendador das rotinas: cada tarefa no seu laco, isolada, uma vez por instalacao.

- Cada tarefa roda num laco proprio do ``asyncio``. A falha (ou demora) de uma nao
  derruba nem segura as outras: o erro vai ao log e a uma contagem, e a tarefa tenta
  de novo no proximo intervalo.
- Antes de cada rodada, a tarefa pega uma trava consultiva do PostgreSQL
  (``pg_try_advisory_lock``) com o nome dela. Se outro processo das rotinas ja estiver
  rodando a mesma tarefa, esta rodada e pulada. A trava e de sessao, numa conexao
  parada (fora de transacao), e sai no fim da rodada ou se a conexao cair.
- Um semaforo limita quantas tarefas rodam ao mesmo tempo, para caber no pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES

log = logger("mcp_claro.rotinas")

ESPACO_DAS_TRAVAS = "mcp_claro.rotinas"


class Desfecho(enum.StrEnum):
    FEITA = "feita"
    OCUPADA = "ocupada"
    FALHOU = "falhou"
    ESTOUROU = "estourou_prazo"


@dataclass(frozen=True, slots=True)
class Tarefa:
    nome: str
    intervalo: float
    rodar: Callable[[], Awaitable[object]]
    prazo: float = 600.0


@contextlib.asynccontextmanager
async def trava_consultiva(engine: AsyncEngine, nome: str) -> AsyncIterator[bool]:
    """Tenta a trava consultiva ``(mcp_claro.rotinas, nome)``. Entrega ``True`` se ficou com ela."""
    async with engine.connect() as c:
        minha = bool(
            (
                await c.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:espaco), hashtext(:nome))"),
                    {"espaco": ESPACO_DAS_TRAVAS, "nome": nome},
                )
            ).scalar_one()
        )
        await c.commit()  # a trava e de sessao: a conexao fica parada, sem transacao aberta
        try:
            yield minha
        finally:
            if minha:
                solta = False
                try:
                    await c.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:espaco), hashtext(:nome))"),
                        {"espaco": ESPACO_DAS_TRAVAS, "nome": nome},
                    )
                    await c.commit()
                    solta = True
                except Exception:  # noqa: BLE001 - abaixo, fechar a conexao solta a trava
                    log.warning("trava_nao_solta", tarefa=nome)
                finally:
                    if not solta:
                        # Conexao que volta ao pool com a trava presa bloquearia os outros processos.
                        with contextlib.suppress(Exception):
                            await c.invalidate()


class Agendador:
    def __init__(
        self,
        engine: AsyncEngine,
        tarefas: Sequence[Tarefa],
        *,
        primeira_espera: float = 5.0,
        ao_mesmo_tempo: int = 3,
    ) -> None:
        nomes = [t.nome for t in tarefas]
        if len(set(nomes)) != len(nomes):
            raise ValueError("cada tarefa precisa de um nome proprio")
        self.engine = engine
        self.tarefas = list(tarefas)
        self.primeira_espera = primeira_espera
        self._vez = asyncio.Semaphore(max(1, ao_mesmo_tempo))
        self.ultimos: dict[str, Desfecho] = {}

    async def rodar_uma(self, tarefa: Tarefa) -> Desfecho:
        """Uma rodada de uma tarefa, com trava, prazo e isolamento de erro."""
        async with self._vez:
            try:
                async with trava_consultiva(self.engine, tarefa.nome) as minha:
                    if not minha:
                        desfecho = Desfecho.OCUPADA
                    else:
                        async with asyncio.timeout(tarefa.prazo):
                            await tarefa.rodar()
                        desfecho = Desfecho.FEITA
            except TimeoutError:
                desfecho = Desfecho.ESTOUROU
                log.warning("rotina_estourou_prazo", tarefa=tarefa.nome, prazo=tarefa.prazo)
            except Exception as erro:  # noqa: BLE001 - uma tarefa nunca derruba as outras
                desfecho = Desfecho.FALHOU
                log.warning("rotina_falhou", tarefa=tarefa.nome, erro=type(erro).__name__)
        self.ultimos[tarefa.nome] = desfecho
        CONTADORES.somar("rotinas_rodadas", tarefa=tarefa.nome, desfecho=str(desfecho))
        return desfecho

    async def _laco(self, tarefa: Tarefa) -> None:
        await asyncio.sleep(self.primeira_espera)
        while True:
            await self.rodar_uma(tarefa)
            await asyncio.sleep(tarefa.intervalo)

    @contextlib.asynccontextmanager
    async def rodando(self) -> AsyncIterator[Agendador]:
        lacos = [asyncio.create_task(self._laco(t), name=f"rotina:{t.nome}") for t in self.tarefas]
        try:
            yield self
        finally:
            for laco in lacos:
                laco.cancel()
            for laco in lacos:
                with contextlib.suppress(asyncio.CancelledError):
                    await laco
