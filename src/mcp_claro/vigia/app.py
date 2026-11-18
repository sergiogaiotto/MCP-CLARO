"""Aplicacao ASGI do papel ``vigia``, na segunda maquina, com banco proprio.

No ciclo de vida sobem quatro lacos, cada um isolado (o agendador das rotinas, com trava consultiva
no banco do vigia):

- ``pulso`` (``WATCH_PING_SECONDS``): o gateway responde no endereco publico? Alarme de silencio
  depois de ``WATCH_SILENCE_ALARM_SECONDS``;
- ``ronda_de_fora`` (``WATCH_PROBE_INTERVAL_SECONDS``): a ronda das portas, de fora;
- ``fechamentos`` (``WATCH_PROBE_INTERVAL_SECONDS``): busca e guarda os fechamentos, conferindo o
  encadeamento;
- o envio dos avisos, pelo SMTP e pelos webhooks do proprio vigia.

Rotas (porta ``WATCH_INTERNAL_PORT``, 9445):

- ``GET /saude``: vivo, com o ultimo desfecho de cada laco;
- ``GET /ultimo-fechamento``: ultimo fechamento guardado, para o despachante conferir na partida
  (``mcp_claro.vigia.continuidade``), com o segredo ``WATCH_CONTINUITY_TOKEN_FILE``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Final

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.avisos import laco_de_envio
from mcp_claro.avisos.email import EnvioEmail
from mcp_claro.avisos.fila import Envio
from mcp_claro.avisos.webhook import EnvioWebhook
from mcp_claro.config.settings import Parametros
from mcp_claro.logs import logger
from mcp_claro.ronda.cracha import FonteCrachas, fonte_do_programa
from mcp_claro.ronda.rodada import cliente_publico, contexto_tls
from mcp_claro.rotinas.agendador import Agendador, Tarefa
from mcp_claro.versao import VERSAO_MCP
from mcp_claro.vigia.config import ConfigVigia
from mcp_claro.vigia.continuidade import CAMINHO, rota_ultimo_fechamento
from mcp_claro.vigia.fechamentos import GuardaFechamentos
from mcp_claro.vigia.ronda import RondaDeFora
from mcp_claro.vigia.silencio import Pulso

log = logger("mcp_claro.vigia")

DOS_PARAMETROS: Final[Any] = object()
"""Valor padrao de ``fonte``: montar a identidade do vigia pelos parametros ``WATCH_*``."""


def montar_envios(cfg: ConfigVigia) -> dict[str, Envio]:
    """Canais proprios do vigia. Sem SMTP ou sem segredo de webhook, o canal fica de fora."""
    envios: dict[str, Envio] = {}
    if cfg.smtp.configurado and cfg.smtp.host and cfg.smtp.remetente:
        envios["email"] = EnvioEmail(
            host=cfg.smtp.host,
            porta=cfg.smtp.porta,
            remetente=cfg.smtp.remetente,
            usuario=cfg.smtp.usuario,
            senha=cfg.smtp.senha,
            starttls=cfg.smtp.starttls,
        )
    else:
        log.warning("vigia_canal_email_desligado")
    if cfg.segredo_webhook is not None:
        envios["webhook"] = EnvioWebhook(cfg.segredo_webhook)
    else:
        log.warning("vigia_canal_webhook_desligado")
    return envios


class Vigia:
    """As tres partes do vigia, cada uma chamavel sozinha (os testes chamam direto)."""

    def __init__(
        self,
        cfg: ConfigVigia,
        engine: AsyncEngine,
        fonte: FonteCrachas | None,
        *,
        emissor: str,
        recurso: str,
        relogio: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.pulso = Pulso(cfg, engine, recurso, relogio=relogio)
        self.ronda = RondaDeFora(cfg, engine, fonte, emissor=emissor, recurso=recurso)
        self.fechamentos = GuardaFechamentos(cfg, engine, fonte, relogio=relogio)

    def tarefas(self, http: httpx2.AsyncClient) -> list[Tarefa]:
        prazo = max(60.0, self.cfg.intervalo_ronda)
        return [
            Tarefa(
                "pulso", self.cfg.intervalo_pulso, lambda: self.pulso.bater(http), max(5.0, self.cfg.intervalo_pulso)
            ),
            Tarefa("ronda_de_fora", self.cfg.intervalo_ronda, lambda: self.ronda.rodar(http), prazo),
            Tarefa("fechamentos", self.cfg.intervalo_ronda, lambda: self.fechamentos.volta(http), prazo),
        ]


def criar_app_vigia(
    p: Parametros,
    engine_vigia: AsyncEngine,
    *,
    config: ConfigVigia | None = None,
    envios: Mapping[str, Envio] | None = None,
    fonte: Any = DOS_PARAMETROS,
    http: httpx2.AsyncClient | None = None,
    relogio: Callable[[], float] = time.time,
) -> Starlette:
    """Papel ``vigia``. ``engine_vigia`` conecta ao banco proprio do vigia, nunca ao do gateway.

    Os argumentos opcionais servem aos testes; na partida, tudo sai dos parametros.
    """
    p.conferir()
    cfg = (config or ConfigVigia.de_parametros(p)).conferir()
    if fonte is DOS_PARAMETROS:
        fonte = fonte_do_programa(cfg.identidade, verify=contexto_tls(cfg.arquivo_ca) if cfg.arquivo_ca else None)
    canais: Mapping[str, Envio] = envios if envios is not None else montar_envios(cfg)
    vigia = Vigia(cfg, engine_vigia, fonte, emissor=p.IDP_ISSUER_URL, recurso=p.recurso, relogio=relogio)
    estado: dict[str, Any] = {"agendador": None}

    async def saude(_: Request) -> JSONResponse:
        agendador: Agendador | None = estado["agendador"]
        ultimos = {nome: str(d) for nome, d in agendador.ultimos.items()} if agendador else {}
        return JSONResponse(
            {"papel": "vigia", "versao": __version__, "mcp": VERSAO_MCP, "vivo": True, "lacos": ultimos}
        )

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as pilha:
            cliente = http
            if cliente is None:
                cliente = cliente_publico(cfg.arquivo_ca, cfg.prazo_tentativa, "mcp-claro-vigia")
                pilha.push_async_callback(cliente.aclose)
            agendador = Agendador(engine_vigia, vigia.tarefas(cliente), primeira_espera=cfg.primeira_espera)
            await pilha.enter_async_context(laco_de_envio(engine_vigia, canais, cfg.envio))
            estado["agendador"] = await pilha.enter_async_context(agendador.rodando())
            log.info("partida", papel="vigia", versao=__version__, mcp=VERSAO_MCP, alvo=cfg.alvo)
            yield
            log.info("parada", papel="vigia")

    app = Starlette(
        routes=[
            Route("/saude", saude, methods=["GET"]),
            Route(CAMINHO, rota_ultimo_fechamento(engine_vigia, cfg.token_continuidade), methods=["GET"]),
        ],
        lifespan=ciclo,
    )
    app.state.vigia = vigia
    app.state.parametros = p
    return app
