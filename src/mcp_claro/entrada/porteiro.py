"""Porteiro de versao: filtro ASGI na frente do kit, que so deixa passar a 2026-07-28.

O kit oficial atende as duas geracoes no mesmo endereco e nao tem chave para
recusar a antiga. Ele decide so pelo cabecalho ``MCP-Protocol-Version``. Por
isso o MCP-CLARO tem este porteiro proprio (decisao 8). Ele roda depois do
verificador de cracha (sem cracha, o aplicativo recebe 401 e faz login) e:

- recusa metodo diferente de POST com 405 (GET de stream e DELETE de sessao
  so existem na geracao antiga; o cabecalho de sessao e ignorado);
- recusa pedido sem cabecalho de versao, como o ``initialize``, com -32022 e a
  lista de versoes aceitas (ou -32020 se o corpo ja for do envelope novo);
- recusa outra versao com HTTP 400, erro -32022 e ``data.supported``;
- recusa ``initialize`` mesmo com cabecalho 2026 (o kit daria -32601);
- conta cada recusa em memoria, ja atribuida ao aplicativo do cracha.
"""

from __future__ import annotations

import json
from typing import Any, Final

from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS, MCP_PROTOCOL_VERSION_HEADER
from mcp_types import (
    DEFAULT_NEGOTIATED_VERSION,
    HEADER_MISMATCH,
    PROTOCOL_VERSION_META_KEY,
    UNSUPPORTED_PROTOCOL_VERSION,
    UnsupportedProtocolVersionErrorData,
)
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_claro.medicoes.contadores import CONTADORES, Contadores
from mcp_claro.versao import VERSAO_MCP

VERSOES_ACEITAS: Final = (VERSAO_MCP,)
_CABECALHO: Final = MCP_PROTOCOL_VERSION_HEADER.lower().encode("latin-1")

MENSAGEM_VERSAO: Final = (
    f"Unsupported protocol version. O MCP-CLARO aceita so a versao {VERSAO_MCP} do MCP; "
    "atualize o aplicativo para essa versao."
)


def erro_jsonrpc(id_: Any, codigo: int, mensagem: str, dados: Any = None) -> JSONResponse:
    erro: dict[str, Any] = {"code": codigo, "message": mensagem}
    if dados is not None:
        erro["data"] = dados
    return JSONResponse(
        {"jsonrpc": "2.0", "id": id_, "error": erro}, status_code=ERROR_CODE_HTTP_STATUS.get(codigo, 400)
    )


def versao_nao_suportada(id_: Any, pedida: str) -> JSONResponse:
    dados = UnsupportedProtocolVersionErrorData(supported=list(VERSOES_ACEITAS), requested=pedida)
    return erro_jsonrpc(id_, UNSUPPORTED_PROTOCOL_VERSION, MENSAGEM_VERSAO, dados.model_dump(mode="json"))


def origem_do_pedido(scope: Scope) -> str:
    """Aplicativo do cracha, gravado pelo filtro de cracha em ``scope['state']``."""
    cracha = scope.get("state", {}).get("cracha")
    client_id = getattr(cracha, "client_id", None)
    return str(client_id) if client_id else "sem-cracha"


class PorteiroDeVersao:
    def __init__(self, app: ASGIApp, *, max_corpo: int, contadores: Contadores = CONTADORES) -> None:
        self.app = app
        self.max_corpo = max_corpo
        self.contadores = contadores

    def _recusar(self, scope: Scope, pedida: str, motivo: str) -> None:
        self.contadores.recusa_versao("aplicativo", origem_do_pedido(scope), pedida, motivo)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] != "POST":
            if scope["method"] == "GET":
                self._recusar(scope, "geracao-antiga", "get_stream")
            elif scope["method"] == "DELETE":
                self._recusar(scope, "geracao-antiga", "delete_sessao")
            await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, send)
            return

        versoes = [v.decode("latin-1") for k, v in scope["headers"] if k == _CABECALHO]

        corpo = bytearray()
        sobra: Message | None = None
        while True:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                sobra = mensagem
                break
            corpo += mensagem.get("body", b"")
            if len(corpo) > self.max_corpo:
                await Response("Request body too large", status_code=413)(scope, receive, send)
                return
            if not mensagem.get("more_body", False):
                break

        try:
            decodificado = json.loads(corpo)
        except ValueError, RecursionError:
            decodificado = None
        pedido = decodificado if isinstance(decodificado, dict) else {}
        id_ = pedido.get("id")
        metodo = pedido.get("method")
        params = pedido.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        envelope_novo = isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta
        pedida = params.get("protocolVersion") if isinstance(params, dict) else None

        resposta: Response | None = None
        if len(versoes) > 1:
            resposta = erro_jsonrpc(id_, HEADER_MISMATCH, "mcp-protocol-version header appears more than once")
        elif not versoes:
            if metodo == "initialize":
                versao = pedida if isinstance(pedida, str) else DEFAULT_NEGOTIATED_VERSION
                self._recusar(scope, versao, "initialize")
                resposta = versao_nao_suportada(id_, versao)
            elif envelope_novo:
                resposta = erro_jsonrpc(
                    id_,
                    HEADER_MISMATCH,
                    "mcp-protocol-version header is required and must match the envelope",
                )
            else:
                self._recusar(scope, DEFAULT_NEGOTIATED_VERSION, "sem_cabecalho")
                resposta = versao_nao_suportada(id_, DEFAULT_NEGOTIATED_VERSION)
        elif versoes[0] not in VERSOES_ACEITAS:
            self._recusar(scope, versoes[0], "cabecalho")
            resposta = versao_nao_suportada(id_, versoes[0])
        elif metodo == "initialize":
            versao = pedida if isinstance(pedida, str) else versoes[0]
            self._recusar(scope, versao, "initialize")
            resposta = versao_nao_suportada(id_, versao)

        if resposta is not None:
            await resposta(scope, receive, send)
            return

        entregue = False

        async def receive_de_novo() -> Message:
            nonlocal entregue, sobra
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": bytes(corpo), "more_body": False}
            if sobra is not None:
                m, sobra = sobra, None
                return m
            return await receive()

        await self.app(scope, receive_de_novo, send)
