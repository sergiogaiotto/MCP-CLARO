"""Aplicacao ASGI do papel ``rotinas``: tarefas periodicas, fila de avisos e alertas do Grafana.

Rotas (porta interna ``ROUTINES_INTERNAL_PORT``, so na rede interna, com mTLS):

- ``GET /saude``: vivo, com o ultimo desfecho de cada tarefa;
- ``POST /interno/alertas-grafana``: alertas do Grafana por webhook interno, com senha
  propria; o formato esperado esta em ``mcp_claro.avisos.grafana``.

No ciclo de vida sobem: o agendador das tarefas (cada uma isolada, com trava
consultiva), o laco de envio dos avisos e a descarga periodica dos contadores.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.avisos import avisar_admin, laco_de_envio
from mcp_claro.avisos.email import EnvioEmail
from mcp_claro.avisos.fila import Envio
from mcp_claro.avisos.grafana import TAMANHO_MAXIMO, AlertaInvalido, ler_alertas, senha_confere
from mcp_claro.avisos.webhook import EnvioWebhook
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.rotinas.acoes import tarefas_de_acoes
from mcp_claro.rotinas.agendador import Agendador, Tarefa
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.fechamento import fechar_se_devido
from mcp_claro.rotinas.limpeza import limpar_sessoes
from mcp_claro.rotinas.medicoes import garantir_particoes, resumir_medicoes
from mcp_claro.rotinas.mudou_parou import FonteDeclaracoes, conferir_sistemas
from mcp_claro.rotinas.ordens import avisar_ordens_a_vencer, vencer_ordens
from mcp_claro.rotinas.programas import tarefas_dos_programas
from mcp_claro.rotinas.resumo_semanal import resumo_semanal
from mcp_claro.tarefas import descarregar_contadores_periodicamente
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.rotinas.app")


def montar_tarefas(engine: AsyncEngine, config: ConfigRotinas, fonte: FonteDeclaracoes) -> list[Tarefa]:
    """As tarefas periodicas do papel, com os intervalos da configuracao."""
    prazo = config.prazo_tarefa
    return [
        Tarefa("vencer_ordens", config.intervalo_curto, lambda: vencer_ordens(engine), prazo),
        Tarefa("avisar_ordens_a_vencer", config.intervalo_aviso_vencer, lambda: avisar_ordens_a_vencer(engine), prazo),
        Tarefa(
            "fechamento",
            min(config.intervalo_curto, config.fechamento_horas * 3600.0),
            lambda: fechar_se_devido(engine, config.fechamento_horas),
            prazo,
        ),
        Tarefa(
            "particoes", config.intervalo_particoes, lambda: garantir_particoes(engine, config.meses_a_frente), prazo
        ),
        Tarefa(
            "resumo_medicoes",
            config.intervalo_curto,
            lambda: resumir_medicoes(
                engine,
                minuto_dias=config.retencao_minuto_dias,
                hora_meses=config.retencao_hora_meses,
                dia_anos=config.retencao_dia_anos,
            ),
            prazo,
        ),
        Tarefa("limpeza", config.intervalo_limpeza, lambda: limpar_sessoes(engine), prazo),
        Tarefa(
            "resumo_semanal",
            config.intervalo_resumo_semanal,
            lambda: resumo_semanal(engine, config.emails_admin, guarda_minuto_dias=config.retencao_minuto_dias),
            prazo,
        ),
        Tarefa(
            "mudou_parou",
            config.intervalo_mudou_parou,
            lambda: conferir_sistemas(engine, fonte, config.emails_admin),
            prazo,
        ),
    ]


def montar_envios(p: Parametros) -> dict[str, Envio]:
    """Canais ligados pelos parametros. Sem ``SMTP_HOST`` ou sem segredo de webhook, o canal fica de fora."""
    envios: dict[str, Envio] = {}
    email = EnvioEmail.de_parametros(p)
    if email is not None:
        envios["email"] = email
    else:
        log.warning("canal_email_desligado")
    webhook = EnvioWebhook.de_parametros(p)
    if webhook is not None:
        envios["webhook"] = webhook
    else:
        log.warning("canal_webhook_desligado")
    return envios


async def _corpo_limitado(pedido: Request, maximo: int) -> bytes | None:
    declarado = pedido.headers.get("content-length")
    if declarado is not None and declarado.isdigit() and int(declarado) > maximo:
        return None
    partes: list[bytes] = []
    total = 0
    async for pedaco in pedido.stream():
        total += len(pedaco)
        if total > maximo:
            return None
        partes.append(pedaco)
    return b"".join(partes)


def criar_app_rotinas(
    p: Parametros,
    engine: AsyncEngine,
    *,
    config: ConfigRotinas | None = None,
    envios: Mapping[str, Envio] | None = None,
    fonte: FonteDeclaracoes | None = None,
    tarefas: list[Tarefa] | None = None,
) -> Starlette:
    """Papel ``rotinas``. Os argumentos opcionais servem aos testes; na partida, tudo sai dos parametros."""
    p.conferir()
    cfg = config or ConfigRotinas.de_parametros(p)
    canais: Mapping[str, Envio] = envios if envios is not None else montar_envios(p)
    estado: dict[str, Any] = {"agendador": None}
    # A ronda das portas nasce aqui: em piloto ou producao sem PATROL_KEY_FILE, a partida recusa.
    ronda: Tarefa | None = None
    if tarefas is None:
        from mcp_claro.ronda.rodada import tarefa_da_ronda

        ronda = tarefa_da_ronda(p, engine, cfg.emails_admin, prazo=cfg.prazo_tarefa)

    async def saude(_: Request) -> JSONResponse:
        agendador: Agendador | None = estado["agendador"]
        ultimos = {nome: str(d) for nome, d in agendador.ultimos.items()} if agendador else {}
        return JSONResponse(
            {"papel": "rotinas", "versao": __version__, "mcp": VERSAO_MCP, "vivo": True, "tarefas": ultimos}
        )

    async def alertas_grafana(pedido: Request) -> JSONResponse:
        if cfg.senha_grafana is None:
            return JSONResponse({"mensagem": "Entrada de alertas do Grafana nao configurada."}, status_code=503)
        if not senha_confere(pedido.headers.get("authorization"), cfg.senha_grafana.get_secret_value()):
            CONTADORES.somar("alertas_grafana_recusados")
            return JSONResponse(
                {"mensagem": "Senha do webhook interno recusada."},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="mcp-claro-rotinas"'},
            )
        bruto = await _corpo_limitado(pedido, TAMANHO_MAXIMO)
        if bruto is None:
            return JSONResponse({"mensagem": "Corpo grande demais."}, status_code=413)
        try:
            alertas, cortados = ler_alertas(json.loads(bruto))
        except (ValueError, AlertaInvalido) as erro:
            return JSONResponse({"mensagem": f"Corpo do Grafana invalido: {erro}"[:200]}, status_code=400)
        novos = 0
        async with engine.begin() as c:
            for alerta in alertas:
                entrou = await avisar_admin(
                    c, "alerta_grafana", alerta.assunto(), alerta.corpo(), alerta.chave_unica, cfg.emails_admin
                )
                if entrou:
                    novos += entrou
                    await registrar(
                        c,
                        papel="rotinas",
                        tipo="alerta_grafana",
                        campos={"alerta": alerta.nome, "situacao": alerta.situacao_legivel},
                        dados={"impressao": alerta.impressao, "inicio": alerta.inicio, "destinos": entrou},
                    )
        CONTADORES.somar("alertas_grafana_recebidos", valor=len(alertas))
        if cortados:
            log.warning("alertas_grafana_cortados", cortados=cortados)
        return JSONResponse({"recebidos": len(alertas), "cortados": cortados, "avisos": novos}, status_code=202)

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as pilha:
            origem = fonte
            if origem is None:
                cliente = ClienteDespachante(p)
                pilha.push_async_callback(cliente.fechar)
                origem = cliente
            lista = (
                tarefas
                if tarefas is not None
                else [
                    *montar_tarefas(engine, cfg, origem),
                    *tarefas_de_acoes(engine, p, cfg),
                    *tarefas_dos_programas(engine, p, cfg),
                    *([ronda] if ronda else []),
                ]
            )
            agendador = Agendador(
                engine, lista, primeira_espera=cfg.primeira_espera, ao_mesmo_tempo=cfg.tarefas_ao_mesmo_tempo
            )
            await pilha.enter_async_context(descarregar_contadores_periodicamente(p, engine))
            await pilha.enter_async_context(laco_de_envio(engine, canais, cfg.envio))
            estado["agendador"] = await pilha.enter_async_context(agendador.rodando())
            log.info("partida", papel="rotinas", versao=__version__, mcp=VERSAO_MCP, tarefas=len(lista))
            yield
            log.info("parada", papel="rotinas")

    return Starlette(
        routes=[
            Route("/saude", saude, methods=["GET"]),
            Route("/interno/alertas-grafana", alertas_grafana, methods=["POST"]),
        ],
        lifespan=ciclo,
    )
