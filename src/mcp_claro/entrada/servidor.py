"""Servidor MCP de baixo nivel da entrada: a vitrine sai do cadastro, pessoa por pessoa.

- ``tools/list`` devolve so as ferramentas revisadas da ordem de servico da
  pessoa, para aquele aplicativo, com nomes e textos do cadastro (nunca os do
  fornecedor) e ``cacheScope`` privado.
- ``tools/call`` recusa ferramenta fora da vitrine, confere os argumentos
  contra o esquema revisado e manda leituras ao despachante pela porta
  interna. Acoes (Fase 2) viram protocolo na porta unica de saida: a entrada
  so registra e olha o estado; quem envia e o despachante unico.
- ``mcp_claro_consultar_protocolo``: estado e resultado de uma acao, so para a
  mesma pessoa pelo mesmo aplicativo.
- O MCP-CLARO nunca pede dados ao aplicativo (sem ``InputRequiredResult``):
  ``inputResponses`` e ``requestState`` nao seguem ao sistema. Quando o sistema
  exige uma capacidade que o aplicativo nao declarou, a resposta e -32021.

Antes de cada pedido, a entrada consulta o estado da entrada (fechada recusa
tudo; so de leitura aceita so a classe "le") e os bloqueios ativos (pessoa,
aplicativo, par, sistema ou tudo). A vitrine esconde as ferramentas de sistema
bloqueado ou pausado. Cada leitura passa pela catraca diaria da ordem de
servico. Toda recusa vai ao extrato com frase legivel.
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
from mcp_claro.regras import bloqueios, catraca, estado, programas, revisao
from mcp_claro.regras.bloqueios import Vigentes
from mcp_claro.regras.estado import EstadoEntrada
from mcp_claro.saida import pedidos as saida_pedidos
from mcp_claro.saida.pedidos import AcaoRecusada, Pedido, Pedidos
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


def _meta_do_pedido(params: types.CallToolRequestParams) -> Any:
    return params.model_dump(by_alias=True, exclude_none=True).get("_meta")


def criar_servidor(p: Parametros, engine: AsyncEngine, ler: Leitor) -> Server[Any]:
    pedidos = Pedidos(p, engine)

    def acoes_ligadas(q: Quem) -> bool:
        return p.FEATURE_PHASE2_ACT or q.pessoa.teste

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
        if aplicativo.tipo == "programa":
            # Programas que rodam sozinhos (decisao 9): cadastro valendo e limite por minuto. A recusa vai ao extrato.
            try:
                await programas.conferir_pedido(engine, p, aplicativo, pessoa.id)
            except programas.ProgramaRecusado as erro:
                raise Recusa(erro.mensagem, erro.motivo) from erro
        return Quem(cracha, aplicativo, pessoa)

    async def portas(q: Quem) -> tuple[EstadoEntrada, Vigentes]:
        async with engine.connect() as c:
            entrada = await estado.ler_entrada(c)
            vigentes = await bloqueios.ativos(c, pessoa_id=q.pessoa.id, aplicativo_id=q.aplicativo.id)
        return entrada, vigentes

    async def recusar_pedido(q: Quem, metodo: str, motivo: str, mensagem: str) -> Recusa:
        async with engine.begin() as c:
            linha = await programas.linha(c, q.aplicativo, "pedido_recusado", {"metodo": metodo, "motivo": motivo})
            await registrar(
                c,
                papel="entrada",
                tipo=linha.tipo,
                campos=linha.campos,
                aplicativo_id=q.aplicativo.id,
                pessoa_id=q.pessoa.id,
            )
        CONTADORES.somar("pedidos_recusados", motivo=motivo)
        return Recusa(mensagem, motivo)

    async def listar(ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        q = await quem(ctx)
        entrada, vigentes = await portas(q)
        if entrada.fechada:
            raise await recusar_pedido(q, "tools/list", entrada.motivo_recusa(), entrada.mensagem())
        if vigentes.geral is not None:
            raise await recusar_pedido(q, "tools/list", vigentes.geral.motivo_recusa(), vigentes.geral.mensagem())
        async with engine.connect() as c:
            ferramentas = await repositorio.vitrine(c, q.pessoa.id, q.aplicativo.id, VERSAO_MCP)
        ferramentas = [f for f in ferramentas if entrada.permite(f.classe) and vigentes.para(f.sistema.id) is None]
        # Programa so ve sistema de chave propria; assistente nao ve sistema de chave de programa (decisao 9).
        ferramentas = programas.filtrar_vitrine(q.aplicativo.tipo, ferramentas)
        if not acoes_ligadas(q) or (q.aplicativo.tipo == "programa" and not p.PROGRAM_ACTIONS_ENABLED):
            ferramentas = [f for f in ferramentas if f.classe == "le"]
        tools = [types.Tool.model_validate(f.como_tool()) for f in ferramentas]
        if acoes_ligadas(q) and any(f.classe != "le" for f in ferramentas):
            # A consulta pelo protocolo entra na vitrine junto com as acoes (e segue valendo na chamada).
            tools.append(types.Tool.model_validate(saida_pedidos.FERRAMENTA_CONSULTA))
        CONTADORES.somar("vitrine_listada")
        return types.ListToolsResult(tools=tools, ttl_ms=0, cache_scope="private")

    async def recusar(q: Quem, ferramenta: str, motivo: str, mensagem: str, trace: str | None) -> Recusa:
        async with engine.begin() as c:
            linha = await programas.linha(
                c, q.aplicativo, "leitura_recusada", {"ferramenta": ferramenta[:128], "motivo": motivo}
            )
            await registrar(
                c,
                papel="entrada",
                tipo=linha.tipo,
                campos=linha.campos,
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
        entrada, vigentes = await portas(q)
        if entrada.fechada:
            raise await recusar(q, params.name, entrada.motivo_recusa(), entrada.mensagem(), trace)
        if vigentes.geral is not None:
            b = vigentes.geral
            raise await recusar(q, params.name, b.motivo_recusa(), b.mensagem(), trace)
        if params.name == saida_pedidos.CONSULTA and acoes_ligadas(q):
            return await consultar(q, params)
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
        if (recusa_chave := programas.recusa_da_chave(q.aplicativo.tipo, ferramenta.sistema.tipo_chave)) is not None:
            raise await recusar(q, params.name, recusa_chave.motivo, recusa_chave.mensagem, trace)
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
        if not entrada.permite(ferramenta.classe):
            raise await recusar(q, params.name, entrada.motivo_recusa(), entrada.mensagem(), trace)
        if not revisao.classe_permitida(ferramenta.sistema.tipo_chave, ferramenta.classe):
            raise await recusar(
                q,
                params.name,
                "acao_em_sistema_compartilhado",
                "Este sistema usa chave compartilhada e so aceita leitura. Nada foi enviado.",
                trace,
            )
        if ferramenta.classe != "le" and not acoes_ligadas(q):
            raise await recusar(
                q,
                params.name,
                "acoes_desligadas",
                "Acoes ainda nao estao ligadas neste MCP-CLARO (Fase 2). Nada foi enviado.",
                trace,
            )
        if ferramenta.classe != "le" and q.aplicativo.tipo == "programa" and not p.PROGRAM_ACTIONS_ENABLED:
            raise await recusar(
                q,
                params.name,
                "programa_so_le",
                "Programas que rodam sozinhos so leem nesta instalacao (decisao 9). Nada foi enviado.",
                trace,
            )
        bloqueio = vigentes.para(ferramenta.sistema.id)
        if bloqueio is not None:
            raise await recusar(q, params.name, bloqueio.motivo_recusa(), bloqueio.mensagem(), trace)
        if ferramenta.classe != "le":
            return await agir(q, ferramenta, params, trace)
        async with engine.begin() as c:
            passagem = await catraca.passar(c, ferramenta.ordem_id)
        if not passagem.passou:
            raise await recusar(q, params.name, "catraca", passagem.mensagem(), trace)
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

    async def agir(
        q: Quem, ferramenta: FerramentaVitrine, params: types.CallToolRequestParams, trace: str | None
    ) -> types.CallToolResult:
        pedido = Pedido(
            pessoa_id=q.pessoa.id,
            aplicativo=q.aplicativo,
            ferramenta=ferramenta,
            argumentos=dict(params.arguments or {}),
            aplicativo_diz=saida_pedidos.aplicativo_diz(_meta_do_pedido(params)),
            trace_id=trace,
        )
        try:
            op, repetida = await pedidos.pedir(pedido)
        except AcaoRecusada as erro:
            raise await recusar(q, params.name, erro.motivo, erro.mensagem, trace) from erro
        if ferramenta.ordem_id is not None:
            async with engine.begin() as c:
                await repositorio.marcar_uso_da_ordem(c, ferramenta.ordem_id)
        op = await pedidos.aguardar(op)
        return types.CallToolResult.model_validate(
            saida_pedidos.resposta(op, selo=ferramenta.sistema.selo, repetida=repetida)
        )

    async def consultar(q: Quem, params: types.CallToolRequestParams) -> types.CallToolResult:
        protocolo = (params.arguments or {}).get("protocolo")
        op = await pedidos.consultar(protocolo, pessoa_id=q.pessoa.id, aplicativo_id=q.aplicativo.id)
        CONTADORES.somar("consultas_protocolo", achou=str(op is not None).lower())
        if op is None:
            return types.CallToolResult.model_validate(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "Protocolo nao encontrado para voce neste aplicativo. Confira o numero.",
                        }
                    ],
                    "isError": True,
                }
            )
        return types.CallToolResult.model_validate(saida_pedidos.resposta(op))

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
