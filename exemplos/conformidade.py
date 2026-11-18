"""Sistema de exemplo para a suite oficial de conformidade do MCP, na versao 2026-07-28.

Implementa o "fixture" com os nomes fixos que os cenarios de servidor da suite
``@modelcontextprotocol/conformance@0.2.0-alpha.12`` pedem em
``--requirements 2026-07-28``: ferramentas ``test_*``, recursos ``test://...``,
prompts ``test_*``, completion, ``subscriptions/listen``, resultados que pedem
dados ao cliente (``InputRequiredResult``, SEP-2322) e protecao contra DNS
rebinding. Os textos fixos que a suite descreve ficam em ingles, iguais aos dela.

Diferente dos outros exemplos, este nao usa ``exemplos.comum.montar_app``:

- Resposta em SSE (``json_response=False``). No modo JSON o kit 2.3.0 descarta
  as notificacoes do proprio pedido, entao ``test_tool_with_progress`` nunca
  mandaria progresso. No modo SSE o kit so passa a ``text/event-stream`` quando
  o tratador emite algo; o resto continua saindo em JSON com o status HTTP certo.
- Protecao contra DNS rebinding ligada: so aceita Host e Origin de localhost
  (o cenario ``dns-rebinding-protection`` manda ``Host: evil.example.com``).
- Sem chave: a suite fala sem autenticacao.
- ``requestState`` lacrado com AES-GCM pelo ``RequestStateBoundary`` do kit,
  com chave so deste processo (o cenario de estado adulterado exige recusa).
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import struct
import warnings
import wave
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.request_state import RequestStateBoundary, RequestStateSecurity
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    PromptsListChanged,
    ToolsListChanged,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError
from starlette.applications import Starlette
from starlette.routing import Route

from exemplos.comum import rodar

NOME = "exemplo-conformidade"

SEGURANCA_LOCAL = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1", "localhost", "[::1]", "127.0.0.1:*", "localhost:*", "[::1]:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
)
"""So localhost: o exemplo roda sem HTTPS e sem chave, o caso que o DNS rebinding ataca."""

# Listas podem mudar pelos gatilhos de teste: ttl 0. O resto e estatico.
DICAS_DE_CACHE: dict[CacheableMethod, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=0, scope="public"),
    "prompts/list": CacheHint(ttl_ms=0, scope="public"),
    "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/templates/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/read": CacheHint(ttl_ms=60_000, scope="public"),
}


# ---------------------------------------------------------------- midias minimas


def _png_1x1(r: int, g: int, b: int) -> bytes:
    """PNG de 1x1 pixel, montado aqui para nao carregar bytes magicos."""

    def bloco(tipo: bytes, dados: bytes) -> bytes:
        crc = zlib.crc32(tipo + dados) & 0xFFFFFFFF
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", crc)

    cabecalho = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 8 bits, RGB, sem entrelacamento
    linha = b"\x00" + bytes((r, g, b))  # filtro 0 + um pixel
    return b"\x89PNG\r\n\x1a\n" + bloco(b"IHDR", cabecalho) + bloco(b"IDAT", zlib.compress(linha)) + bloco(b"IEND", b"")


def _wav_silencio(milissegundos: int = 10, taxa: int = 8000) -> bytes:
    """WAV mono de 16 bits com silencio curto."""
    saida = io.BytesIO()
    with wave.open(saida, "wb") as arquivo:
        arquivo.setnchannels(1)
        arquivo.setsampwidth(2)
        arquivo.setframerate(taxa)
        arquivo.writeframes(b"\x00\x00" * (taxa * milissegundos // 1000))
    return saida.getvalue()


PNG_B64 = base64.b64encode(_png_1x1(255, 0, 0)).decode()
WAV_B64 = base64.b64encode(_wav_silencio()).decode()

SEM_ARGUMENTOS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def _texto(texto: str) -> types.TextContent:
    return types.TextContent(type="text", text=texto)


def _resultado(texto: str, *, erro: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[_texto(texto)], is_error=erro)


# ---------------------------------------------------------------- pedidos ao cliente (SEP-2322)

_CAPACIDADES = ("elicitation", "sampling", "roots")


def capacidades_declaradas(ctx: ServerRequestContext[Any]) -> set[str]:
    """Capacidades que o cliente declarou no ``_meta`` deste pedido."""
    caps = ctx.session.client_capabilities
    if caps is None:
        return set()
    return {nome for nome in _CAPACIDADES if getattr(caps, nome) is not None}


def exigir_capacidades(ctx: ServerRequestContext[Any], *nomes: str) -> None:
    """Recusa com -32021 quando o cliente nao declarou o que a ferramenta precisa pedir."""
    faltam = [nome for nome in nomes if nome not in capacidades_declaradas(ctx)]
    if not faltam:
        return
    dados = types.MissingRequiredClientCapabilityErrorData(
        required_capabilities=types.ClientCapabilities.model_validate({nome: {} for nome in faltam})
    )
    raise MCPError(
        code=types.MISSING_REQUIRED_CLIENT_CAPABILITY,
        message=f"O cliente nao declarou a capacidade necessaria: {', '.join(faltam)}",
        data=dados.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


def _elicitacao(mensagem: str, propriedades: dict[str, Any], obrigatorios: list[str]) -> dict[str, Any]:
    esquema = {"type": "object", "properties": propriedades, "required": obrigatorios}
    return {"method": "elicitation/create", "params": {"message": mensagem, "requestedSchema": esquema}}


def _amostragem(texto: str, max_tokens: int) -> dict[str, Any]:
    mensagem = {"role": "user", "content": {"type": "text", "text": texto}}
    return {"method": "sampling/createMessage", "params": {"messages": [mensagem], "maxTokens": max_tokens}}


_RAIZES: dict[str, Any] = {"method": "roots/list", "params": {}}

_PEDE_NOME = _elicitacao("What is your name?", {"name": {"type": "string"}}, ["name"])
_PEDE_CONFIRMACAO = _elicitacao("Please confirm", {"ok": {"type": "boolean"}}, ["ok"])


def pedir(pedidos: Mapping[str, dict[str, Any]], estado: str | None = None) -> types.InputRequiredResult:
    """Monta o ``InputRequiredResult``; o ``requestState`` sai lacrado pelo kit."""
    corpo: dict[str, Any] = {"inputRequests": dict(pedidos)}
    if estado is not None:
        corpo["requestState"] = estado
    return types.InputRequiredResult.model_validate(corpo)


def _resposta(p: types.InputResponseRequestParams, chave: str) -> types.InputResponse | None:
    return (p.input_responses or {}).get(chave)


def resposta_formulario(p: types.InputResponseRequestParams, chave: str) -> dict[str, Any] | None:
    """Conteudo de uma elicitacao aceita, ou None se ainda falta (pede de novo)."""
    r = _resposta(p, chave)
    if isinstance(r, types.ElicitResult) and r.action == "accept" and r.content is not None:
        return dict(r.content)
    return None


def resposta_amostragem(p: types.InputResponseRequestParams, chave: str) -> str | None:
    r = _resposta(p, chave)
    if isinstance(r, types.CreateMessageResult) and isinstance(r.content, types.TextContent):
        return r.content.text
    return None


def resposta_raizes(p: types.InputResponseRequestParams, chave: str) -> list[str] | None:
    r = _resposta(p, chave)
    if isinstance(r, types.ListRootsResult):
        return [str(raiz.uri) for raiz in r.roots]
    return None


# ---------------------------------------------------------------- recursos

TEXTO_ESTATICO = "This is the content of the static text resource."
MODELO_DADOS = re.compile(r"^test://template/([^/]+)/data$")

RECURSOS = [
    {
        "uri": "test://static-text",
        "name": "static-text",
        "description": "Recurso de texto fixo.",
        "mimeType": "text/plain",
    },
    {
        "uri": "test://static-binary",
        "name": "static-binary",
        "description": "Recurso binario fixo (PNG de 1x1).",
        "mimeType": "image/png",
    },
]

MODELOS = [
    {
        "uriTemplate": "test://template/{id}/data",
        "name": "template-data",
        "description": "Dados em JSON para o id pedido.",
        "mimeType": "application/json",
    }
]


def ler_recurso(uri: str) -> types.ReadResourceResult:
    if uri == "test://static-text":
        conteudo: types.TextResourceContents | types.BlobResourceContents = types.TextResourceContents(
            uri=uri, mime_type="text/plain", text=TEXTO_ESTATICO
        )
    elif uri == "test://static-binary":
        conteudo = types.BlobResourceContents(uri=uri, mime_type="image/png", blob=PNG_B64)
    elif m := MODELO_DADOS.match(uri):
        id_ = m.group(1)
        dados = {"id": id_, "templateTest": True, "data": f"Data for ID: {id_}"}
        conteudo = types.TextResourceContents(uri=uri, mime_type="application/json", text=json.dumps(dados))
    else:
        # SEP-2164: nunca lista vazia; erro -32602 com a URI em data.
        raise MCPError(code=types.INVALID_PARAMS, message="Resource not found", data={"uri": uri})
    return types.ReadResourceResult(contents=[conteudo])


# ---------------------------------------------------------------- app


def criar() -> Starlette:
    """Monta o app Starlette com o fixture completo, sem chave e com SSE."""
    barramento = InMemorySubscriptionBus()
    escuta = ListenHandler(barramento)
    geracao = {"ferramentas": 1, "prompts": 1}  # muda quando um gatilho de teste muda a lista

    def ferramentas() -> list[dict[str, Any]]:
        def f(nome: str, descricao: str) -> dict[str, Any]:
            return {"name": nome, "description": descricao, "inputSchema": SEM_ARGUMENTOS}

        return [
            f("test_simple_text", "Devolve um texto simples."),
            f("test_image_content", "Devolve uma imagem PNG."),
            f("test_audio_content", "Devolve um audio WAV."),
            f("test_embedded_resource", "Devolve um recurso embutido."),
            f("test_multiple_content_types", "Devolve texto, imagem e recurso juntos."),
            f("test_error_handling", "Sempre devolve erro de ferramenta (isError)."),
            f("test_tool_with_progress", "Manda progresso 0, 50 e 100 de 100."),
            f("test_logging_tool", "Tenta registrar log; so sai se o pedido pediu logLevel."),
            f("test_missing_capability", "Precisa de sampling do cliente; sem ele recusa com -32021."),
            f("test_streaming_elicitation", "Pede uma confirmacao ao cliente por elicitacao."),
            f(
                "test_trigger_tool_change",
                f"Muda a lista de ferramentas e avisa quem escuta (geracao {geracao['ferramentas']}).",
            ),
            f("test_trigger_prompt_change", "Muda a lista de prompts e avisa quem escuta."),
            f("test_input_required_result_elicitation", "Pede o nome por elicitacao e cumprimenta."),
            f("test_input_required_result_sampling", "Pede uma resposta de sampling ao cliente."),
            f("test_input_required_result_list_roots", "Pede as raizes do cliente."),
            f("test_input_required_result_request_state", "Pede confirmacao e confere o requestState."),
            f("test_input_required_result_multiple_inputs", "Pede elicitacao, sampling e raizes de uma vez."),
            f("test_input_required_result_multi_round", "Pede dados em duas rodadas com estado."),
            f("test_input_required_result_tampered_state", "Recusa requestState adulterado."),
            f("test_input_required_result_capabilities", "So pede o que o cliente declarou."),
        ]

    def prompts() -> list[types.Prompt]:
        arg = types.PromptArgument
        return [
            types.Prompt(name="test_simple_prompt", description="Prompt simples, sem argumentos."),
            types.Prompt(
                name="test_prompt_with_arguments",
                description="Prompt com dois argumentos.",
                arguments=[
                    arg(name="arg1", description="Primeiro argumento", required=True),
                    arg(name="arg2", description="Segundo argumento", required=True),
                ],
            ),
            types.Prompt(
                name="test_prompt_with_embedded_resource",
                description="Prompt com recurso embutido.",
                arguments=[arg(name="resourceUri", description="URI do recurso a embutir", required=True)],
            ),
            types.Prompt(name="test_prompt_with_image", description="Prompt com imagem."),
            types.Prompt(
                name="test_input_required_result_prompt",
                description=f"Pede contexto ao cliente antes do prompt (geracao {geracao['prompts']}).",
            ),
        ]

    # ---- ferramentas

    async def simples(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        return _resultado("This is a simple text response for testing.")

    async def imagem(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        return types.CallToolResult(content=[types.ImageContent(type="image", data=PNG_B64, mime_type="image/png")])

    async def audio(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        return types.CallToolResult(content=[types.AudioContent(type="audio", data=WAV_B64, mime_type="audio/wav")])

    async def embutido(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        recurso = types.TextResourceContents(
            uri="test://embedded-resource",
            mime_type="text/plain",
            text="This is an embedded resource content.",
        )
        return types.CallToolResult(content=[types.EmbeddedResource(type="resource", resource=recurso)])

    async def misto(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        recurso = types.TextResourceContents(
            uri="test://mixed-content-resource",
            mime_type="application/json",
            text=json.dumps({"test": "data", "value": 123}),
        )
        return types.CallToolResult(
            content=[
                _texto("Multiple content types test:"),
                types.ImageContent(type="image", data=PNG_B64, mime_type="image/png"),
                types.EmbeddedResource(type="resource", resource=recurso),
            ]
        )

    async def com_erro(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        return _resultado("This tool intentionally returns an error for testing", erro=True)

    async def com_progresso(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        # Sem progressToken no pedido o kit nao manda nada; as pausas acontecem igual.
        for i, valor in enumerate((0, 50, 100)):
            if i:
                await anyio.sleep(0.05)
            await ctx.session.report_progress(valor, 100)
        return _resultado("Progress test completed.")

    async def com_log(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        # Logging esta obsoleto na 2026-07-28 (SEP-2577); o kit so entrega se o pedido
        # trouxe o logLevel em _meta. Sem isso, nada vai para o fluxo da resposta.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            await ctx.session.send_log_message("info", "Tool execution started")
        return _resultado("Logging test completed.")

    async def precisa_amostragem(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "sampling")
        texto = resposta_amostragem(p, "resumo")
        if texto is None:
            return pedir({"resumo": _amostragem("Summarize this conformance test in one sentence.", 50)})
        return _resultado(f"Sampling result: {texto}")

    async def elicitacao_em_fluxo(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "elicitation")
        if resposta_formulario(p, "confirm") is None:
            return pedir({"confirm": _PEDE_CONFIRMACAO})
        return _resultado("Confirmed.")

    async def muda_ferramentas(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        geracao["ferramentas"] += 1
        await barramento.publish(ToolsListChanged())
        return _resultado(f"Tool list changed (generation {geracao['ferramentas']}).")

    async def muda_prompts(ctx: ServerRequestContext[Any], p: types.CallToolRequestParams) -> types.CallToolResult:
        geracao["prompts"] += 1
        await barramento.publish(PromptsListChanged())
        return _resultado(f"Prompt list changed (generation {geracao['prompts']}).")

    async def mrtr_elicitacao(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "elicitation")
        dados = resposta_formulario(p, "user_name")
        if dados is None or not isinstance(dados.get("name"), str):
            # Faltou a resposta certa (ou veio com outra chave): pede de novo, sem erro.
            return pedir({"user_name": _PEDE_NOME})
        return _resultado(f"Hello, {dados['name']}!")

    async def mrtr_amostragem(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "sampling")
        texto = resposta_amostragem(p, "capital_question")
        if texto is None:
            return pedir({"capital_question": _amostragem("What is the capital of France?", 100)})
        return _resultado(texto)

    async def mrtr_raizes(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "roots")
        raizes = resposta_raizes(p, "client_roots")
        if raizes is None:
            return pedir({"client_roots": _RAIZES})
        return _resultado(f"Roots: {', '.join(raizes) or '(none)'}")

    async def mrtr_estado(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "elicitation")
        estado = "confirmar:v1"
        if p.request_state != estado or resposta_formulario(p, "confirm") is None:
            return pedir({"confirm": _PEDE_CONFIRMACAO}, estado)
        return _resultado("state-ok: requestState received and validated.")

    async def mrtr_varios(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "elicitation", "sampling", "roots")
        estado = "varios:v1"
        nome = resposta_formulario(p, "user_name") if p.request_state == estado else None
        saudacao = resposta_amostragem(p, "greeting") if p.request_state == estado else None
        raizes = resposta_raizes(p, "client_roots") if p.request_state == estado else None
        if nome is not None and saudacao is not None and raizes is not None:
            return _resultado(f"{saudacao} {nome.get('name')} ({len(raizes)} root(s))")
        # Pede de novo so o que faltou, com o mesmo estado.
        faltam: dict[str, dict[str, Any]] = {}
        if nome is None:
            faltam["user_name"] = _PEDE_NOME
        if saudacao is None:
            faltam["greeting"] = _amostragem("Generate a greeting", 50)
        if raizes is None:
            faltam["client_roots"] = _RAIZES
        return pedir(faltam, estado)

    async def mrtr_rodadas(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        exigir_capacidades(ctx, "elicitation")
        passo1 = _elicitacao("Step 1: What is your name?", {"name": {"type": "string"}}, ["name"])
        passo2 = _elicitacao("Step 2: What is your favorite color?", {"color": {"type": "string"}}, ["color"])
        estado: dict[str, Any] = json.loads(p.request_state) if p.request_state else {"rodada": 1}
        if estado.get("rodada") == 1:
            nome = resposta_formulario(p, "step1")
            if nome is None:
                return pedir({"step1": passo1}, json.dumps({"rodada": 1}))
            return pedir({"step2": passo2}, json.dumps({"rodada": 2, "nome": nome.get("name")}))
        cor = resposta_formulario(p, "step2")
        if cor is None:
            return pedir({"step2": passo2}, json.dumps(estado))
        return _resultado(f"{estado.get('nome')} likes {cor.get('color')}.")

    async def mrtr_adulterado(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        # Estado adulterado nem chega aqui: o RequestStateBoundary recusa com -32602.
        exigir_capacidades(ctx, "elicitation")
        estado = "integridade:v1"
        if p.request_state != estado or resposta_formulario(p, "confirm") is None:
            return pedir({"confirm": _PEDE_CONFIRMACAO}, estado)
        return _resultado("Integrity check passed.")

    async def mrtr_capacidades(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        declaradas = capacidades_declaradas(ctx)
        if not declaradas & {"elicitation", "sampling"}:
            exigir_capacidades(ctx, "sampling")
        faltam: dict[str, dict[str, Any]] = {}
        if "elicitation" in declaradas and resposta_formulario(p, "user_name") is None:
            faltam["user_name"] = _PEDE_NOME
        if "sampling" in declaradas and resposta_amostragem(p, "greeting") is None:
            faltam["greeting"] = _amostragem("Generate a greeting", 50)
        if faltam:
            return pedir(faltam)
        return _resultado(f"Used only declared capabilities: {', '.join(sorted(declaradas))}.")

    Tratador = Callable[
        [ServerRequestContext[Any], types.CallToolRequestParams],
        Awaitable[types.CallToolResult | types.InputRequiredResult],
    ]
    tratadores: dict[str, Tratador] = {
        "test_simple_text": simples,
        "test_image_content": imagem,
        "test_audio_content": audio,
        "test_embedded_resource": embutido,
        "test_multiple_content_types": misto,
        "test_error_handling": com_erro,
        "test_tool_with_progress": com_progresso,
        "test_logging_tool": com_log,
        "test_missing_capability": precisa_amostragem,
        "test_streaming_elicitation": elicitacao_em_fluxo,
        "test_trigger_tool_change": muda_ferramentas,
        "test_trigger_prompt_change": muda_prompts,
        "test_input_required_result_elicitation": mrtr_elicitacao,
        "test_input_required_result_sampling": mrtr_amostragem,
        "test_input_required_result_list_roots": mrtr_raizes,
        "test_input_required_result_request_state": mrtr_estado,
        "test_input_required_result_multiple_inputs": mrtr_varios,
        "test_input_required_result_multi_round": mrtr_rodadas,
        "test_input_required_result_tampered_state": mrtr_adulterado,
        "test_input_required_result_capabilities": mrtr_capacidades,
    }

    async def listar_ferramentas(
        ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(f) for f in ferramentas()])

    async def chamar(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        tratador = tratadores.get(p.name)
        if tratador is None:
            raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")
        return await tratador(ctx, p)

    # ---- prompts

    async def listar_prompts(
        ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=prompts())

    def _mensagem(conteudo: types.ContentBlock) -> types.PromptMessage:
        return types.PromptMessage(role="user", content=conteudo)

    async def obter_prompt(
        ctx: ServerRequestContext[Any], p: types.GetPromptRequestParams
    ) -> types.GetPromptResult | types.InputRequiredResult:
        args = p.arguments or {}

        def argumento(nome: str) -> str:
            valor = args.get(nome)
            if not valor:
                raise MCPError(code=types.INVALID_PARAMS, message=f"Falta o argumento obrigatorio: {nome}")
            return valor

        if p.name == "test_simple_prompt":
            return types.GetPromptResult(messages=[_mensagem(_texto("This is a simple prompt for testing."))])
        if p.name == "test_prompt_with_arguments":
            texto = f"Prompt with arguments: arg1='{argumento('arg1')}', arg2='{argumento('arg2')}'"
            return types.GetPromptResult(messages=[_mensagem(_texto(texto))])
        if p.name == "test_prompt_with_embedded_resource":
            recurso = types.TextResourceContents(
                uri=argumento("resourceUri"),
                mime_type="text/plain",
                text="Embedded resource content for testing.",
            )
            return types.GetPromptResult(
                messages=[
                    _mensagem(types.EmbeddedResource(type="resource", resource=recurso)),
                    _mensagem(_texto("Please process the embedded resource above.")),
                ]
            )
        if p.name == "test_prompt_with_image":
            return types.GetPromptResult(
                messages=[
                    _mensagem(types.ImageContent(type="image", data=PNG_B64, mime_type="image/png")),
                    _mensagem(_texto("Please analyze the image above.")),
                ]
            )
        if p.name == "test_input_required_result_prompt":
            exigir_capacidades(ctx, "elicitation")
            dados = resposta_formulario(p, "user_context")
            if dados is None:
                pergunta = _elicitacao(
                    "What context should the prompt use?", {"context": {"type": "string"}}, ["context"]
                )
                return pedir({"user_context": pergunta})
            return types.GetPromptResult(messages=[_mensagem(_texto(f"Context: {dados.get('context')}"))])
        raise MCPError(code=types.INVALID_PARAMS, message=f"Prompt desconhecido: {p.name}")

    # ---- recursos e completion

    async def listar_recursos(
        ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=[types.Resource.model_validate(r) for r in RECURSOS])

    async def listar_modelos(
        ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None
    ) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(
            resource_templates=[types.ResourceTemplate.model_validate(m) for m in MODELOS]
        )

    async def ler(ctx: ServerRequestContext[Any], p: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        return ler_recurso(str(p.uri))

    sugestoes: dict[tuple[str, str], list[str]] = {
        ("test_prompt_with_arguments", "arg1"): ["paris", "park", "party", "test-value-1"],
        ("test_prompt_with_arguments", "arg2"): ["world", "test-value-2"],
        ("test://template/{id}/data", "id"): ["123", "456", "789"],
    }

    async def completar(ctx: ServerRequestContext[Any], p: types.CompleteRequestParams) -> types.CompleteResult:
        alvo = p.ref.name if isinstance(p.ref, types.PromptReference) else p.ref.uri
        valores = [v for v in sugestoes.get((alvo, p.argument.name), []) if v.startswith(p.argument.value)]
        return types.CompleteResult(completion=types.Completion(values=valores, total=len(valores), has_more=False))

    esquemas = {f["name"]: f["inputSchema"] for f in ferramentas()}
    server: Server[Any] = Server(
        NOME,
        version="1.0.0",
        cache_hints=DICAS_DE_CACHE,
        get_tool_input_schema=esquemas.get,
        on_list_tools=listar_ferramentas,
        on_call_tool=chamar,
        on_list_prompts=listar_prompts,
        on_get_prompt=obter_prompt,
        on_list_resources=listar_recursos,
        on_list_resource_templates=listar_modelos,
        on_read_resource=ler,
        on_completion=completar,
        on_subscriptions_listen=escuta,
    )
    server.middleware.append(RequestStateBoundary(RequestStateSecurity.ephemeral(), default_audience=NOME))

    sm = StreamableHTTPSessionManager(
        app=server, json_response=False, stateless=True, security_settings=SEGURANCA_LOCAL
    )

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with sm.run():
            try:
                yield
            finally:
                escuta.close()  # encerra com calma os fluxos de subscriptions/listen abertos

    return Starlette(routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(sm))], lifespan=ciclo)


if __name__ == "__main__":
    rodar(criar, 9107)
