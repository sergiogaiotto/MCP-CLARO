"""Servidor MCP de baixo nivel da entrada: a vitrine sai do cadastro, pessoa por pessoa.

- ``tools/list`` devolve so as ferramentas revisadas da ordem de servico da
  pessoa, para aquele aplicativo, com nomes e textos do cadastro (nunca os do
  fornecedor) e ``cacheScope`` privado.
- ``tools/call`` recusa ferramenta fora da vitrine, confere os argumentos
  contra o esquema revisado e manda leituras ao despachante pela porta
  interna. Acoes so entram com a Fase 2.
- O MCP-CLARO nunca pede dados ao aplicativo (sem ``InputRequiredResult``):
  ``inputResponses`` e ``requestState`` nao seguem ao sistema. Quando o sistema
  exige uma capacidade que o aplicativo nao declarou, a resposta e -32021.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro import __version__
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import Aplicativo, FerramentaVitrine, Pessoa
from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.argumentos import ArgumentosInvalidos, conferir_argumentos
from mcp_claro.entrada.cracha import Cracha, cracha_atual
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.versao import CAPACIDADES_DO_CLIENTE, MOTIVO_EXIGE_CAPACIDADE, PREFIXO_META, VERSAO_MCP

log = logger("mcp_claro.entrada.servidor")

CODIGO_RECUSA = types.INVALID_PARAMS


@dataclass(frozen=True, slots=True)
class Quem:
    cracha: Cracha
    aplicativo: Aplicativo
    pessoa: Pessoa


@dataclass(frozen=True, slots=True)
class PedidoLeitura:
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ordem_id: uuid.UUID
    ferramenta_id: uuid.UUID
    argumentos: dict[str, Any]
    trace_id: str | None


Leitor = Callable[[PedidoLeitura], Awaitable[dict[str, Any]]]


class Recusa(MCPError):
    """Recusa legivel ao aplicativo, como erro JSON-RPC."""

    def __init__(self, mensagem: str, motivo: str) -> None:
        super().__init__(code=CODIGO_RECUSA, message=mensagem, data={PREFIXO_META + "motivo": motivo})
        self.motivo = motivo


def _campos_nulos(params: types.CallToolRequestParams) -> list[str]:
    """``inputResponses`` e ``requestState`` sao opcionais, mas, se vierem, nao podem ser ``null``."""
    campos = (("input_responses", "inputResponses"), ("request_state", "requestState"))
    return [fio for campo, fio in campos if campo in params.model_fields_set and getattr(params, campo) is None]


def capacidades_nao_declaradas(ctx: ServerRequestContext[Any], exigidas: Any) -> list[str]:
    """Das capacidades que o sistema exige, as que o aplicativo nao declarou no ``_meta`` deste pedido."""
    if not isinstance(exigidas, list):
        return []
    declaradas = ctx.session.client_capabilities
    return [
        nome
        for nome in exigidas
        if nome in CAPACIDADES_DO_CLIENTE and (declaradas is None or getattr(declaradas, nome, None) is None)
    ]


def capacidade_ausente(faltam: list[str]) -> MCPError:
    dados = types.MissingRequiredClientCapabilityErrorData(
        required_capabilities=types.ClientCapabilities.model_validate({nome: {} for nome in faltam})
    )
    return MCPError(
        code=types.MISSING_REQUIRED_CLIENT_CAPABILITY,
        message=f"Esta ferramenta precisa que o aplicativo declare a capacidade: {', '.join(faltam)}.",
        data=dados.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


def _traceparent(ctx: ServerRequestContext[Any], p: Parametros) -> str | None:
    if not p.OBS_TRACE_ACCEPT_INBOUND:
        return None
    meta = ctx.meta or {}
    valor = meta.get("traceparent")
    if isinstance(valor, str) and len(valor) == 55 and valor.count("-") == 3:
        return valor.split("-")[1]
    return None


def criar_servidor(p: Parametros, engine: AsyncEngine, ler: Leitor) -> Server[Any]:
    async def quem(ctx: ServerRequestContext[Any]) -> Quem:
        cracha = cracha_atual.get()
        if cracha is None:
            raise Recusa("Cracha ausente.", "sem_cracha")
        async with engine.begin() as c:
            aplicativo = await repositorio.aplicativo_por_client_id(c, cracha.client_id)
            if aplicativo is None:
                raise Recusa("Este aplicativo nao esta habilitado no MCP-CLARO. Fale com o ADMIN.", "app_desconhecido")
            if aplicativo.situacao != "ativo":
                raise Recusa(f"Este aplicativo esta {aplicativo.situacao} no MCP-CLARO.", "app_" + aplicativo.situacao)
            pessoa = await repositorio.pessoa_no_login(c, cracha.emissor, cracha.sujeito, cracha.nome)
            if pessoa.situacao != "ativa":
                raise Recusa(f"Sua conta esta {pessoa.situacao} no MCP-CLARO.", "pessoa_" + pessoa.situacao)
            if not p.FEATURE_PHASE1_READ and not pessoa.teste:
                raise Recusa("A leitura ainda nao foi ligada para pessoas reais (Fase 1).", "fase1_desligada")
            await repositorio.marcar_versao_vista(c, aplicativo.id, VERSAO_MCP)
        return Quem(cracha, aplicativo, pessoa)

    async def listar(ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        q = await quem(ctx)
        async with engine.connect() as c:
            ferramentas = await repositorio.vitrine(c, q.pessoa.id, q.aplicativo.id, VERSAO_MCP)
        CONTADORES.somar("vitrine_listada")
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(f.como_tool()) for f in ferramentas],
            ttl_ms=0,
            cache_scope="private",
        )

    async def recusar(q: Quem, ferramenta: str, motivo: str, mensagem: str, trace: str | None) -> Recusa:
        async with engine.begin() as c:
            await registrar(
                c,
                papel="entrada",
                tipo="leitura_recusada",
                campos={"ferramenta": ferramenta[:128], "motivo": motivo},
                aplicativo_id=q.aplicativo.id,
                pessoa_id=q.pessoa.id,
                ferramenta=ferramenta[:128],
                trace_id=trace,
            )
        CONTADORES.somar("chamadas_recusadas", motivo=motivo)
        return Recusa(mensagem, motivo)

    async def chamar(ctx: ServerRequestContext[Any], params: types.CallToolRequestParams) -> types.CallToolResult:
        if nulos := _campos_nulos(params):
            raise MCPError(code=types.INVALID_PARAMS, message=f"{', '.join(nulos)} nao pode ser null.")
        q = await quem(ctx)
        trace = _traceparent(ctx, p)
        async with engine.connect() as c:
            ferramenta: FerramentaVitrine | None = await repositorio.ferramenta_na_vitrine(
                c, q.pessoa.id, q.aplicativo.id, params.name, VERSAO_MCP
            )
        if ferramenta is None or ferramenta.ordem_id is None:
            raise await recusar(
                q,
                params.name,
                "fora_da_vitrine",
                f"A ferramenta {params.name} nao esta na sua vitrine.",
                trace,
            )
        try:
            conferir_argumentos(ferramenta.esquema_entrada, params.arguments)
        except ArgumentosInvalidos as erro:
            raise await recusar(
                q,
                params.name,
                "argumento_fora_do_esquema",
                f"Argumentos fora do esquema revisado: {erro}",
                trace,
            ) from erro
        if ferramenta.classe != "le":
            raise await recusar(
                q,
                params.name,
                "acoes_desligadas",
                "Acoes ainda nao estao ligadas neste MCP-CLARO (Fase 2). Nada foi enviado.",
                trace,
            )
        resultado = await ler(
            PedidoLeitura(
                pessoa_id=q.pessoa.id,
                aplicativo_id=q.aplicativo.id,
                ordem_id=ferramenta.ordem_id,
                ferramenta_id=ferramenta.id,
                argumentos=dict(params.arguments or {}),
                trace_id=trace,
            )
        )
        async with engine.begin() as c:
            await repositorio.marcar_uso_da_ordem(c, ferramenta.ordem_id)
        CONTADORES.somar("leituras", sistema=ferramenta.sistema.nome)
        meta = resultado.get("_meta") or {}
        exige = meta.get(PREFIXO_META + "motivo") == MOTIVO_EXIGE_CAPACIDADE
        if exige and (faltam := capacidades_nao_declaradas(ctx, meta.get(PREFIXO_META + "capacidades"))):
            # O sistema so responde pedindo dados ao aplicativo, e o aplicativo nem declarou a capacidade.
            raise capacidade_ausente(faltam)
        return types.CallToolResult.model_validate(resultado)

    def esquema_da_ferramenta(nome: str) -> dict[str, Any] | None:
        # O kit pede o esquema so para conferir cabecalhos Mcp-Param-*; a vitrine revisada nao os usa.
        return None

    return Server(
        "mcp-claro",
        version=__version__,
        title="MCP-CLARO",
        description="Gateway MCP independente: ponto comum para aplicativos de IA usarem ferramentas.",
        on_list_tools=listar,
        on_call_tool=chamar,
        get_tool_input_schema=esquema_da_ferramenta,
    )
