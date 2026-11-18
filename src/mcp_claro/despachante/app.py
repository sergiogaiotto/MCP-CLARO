"""Aplicacao ASGI do papel ``despachante``: porta interna, so com mTLS da entrada.

Rotas internas (nunca expostas ao portao):

- ``POST /interno/ler``: leitura reconferida, uma chamada ao sistema;
- ``POST /interno/sondar``: sondagem de versao no cadastro de um sistema;
- ``POST /interno/declaracoes``: le o que o sistema declara (revisao e mudou-parou);
- ``POST /interno/chaves``: guarda uma chave cifrada (vinda da autorizacao da pessoa ou do ADMIN).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import chaves
from mcp_claro.despachante.autorizacao import rotas_autorizacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.despachante.servico import DeclaracoesIndisponiveis, Despachante, LeituraRecusada, PedidoLeitura
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.tarefas import descarregar_contadores_periodicamente
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.despachante.app")


def _uuid(valor: Any) -> uuid.UUID | None:
    return uuid.UUID(str(valor)) if valor else None


class _CifraDoCofre:
    """Cifra que so fica pronta quando o papel entra no cofre (na partida). Antes disso, recusa."""

    def __init__(self) -> None:
        self.alvo: Cifra | None = None

    def _pronta(self) -> Cifra:
        if self.alvo is None:
            from mcp_claro.cofre import CofreIndisponivel

            raise CofreIndisponivel("O despachante ainda nao entrou no cofre.")
        return self.alvo

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        return await self._pronta().cifrar(texto, contexto)

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        return await self._pronta().decifrar(cifrado, contexto)


async def _conferir_continuidade_na_partida(p: Parametros, engine: AsyncEngine) -> None:
    """Antes de atender: confere se o banco e o mesmo (identificador, linha do tempo, marca restaurado e o
    fechamento guardado no vigia). Se nao conferir, a saida fica fechada e os aplicativos pausados."""
    import asyncio
    import ssl

    from mcp_claro.continuidade import conferir_continuidade
    from mcp_claro.vigia.continuidade import numero_no_vigia

    numero: int | None = None
    if p.WATCH_CONTINUITY_URL and p.WATCH_CONTINUITY_TOKEN_FILE:
        from pathlib import Path

        from pydantic import SecretStr

        # Leitura unica e pequena, na partida, antes de atender qualquer pedido.
        segredo = SecretStr(Path(p.WATCH_CONTINUITY_TOKEN_FILE).read_text(encoding="utf-8").strip())  # noqa: ASYNC240
        contexto = ssl.create_default_context(cafile=p.WATCH_CONTINUITY_CA_FILE) if p.WATCH_CONTINUITY_CA_FILE else None
        for tentativa in range(3):
            numero = await numero_no_vigia(p.WATCH_CONTINUITY_URL, segredo, verify=contexto)
            if numero is not None:
                break
            await asyncio.sleep(2 * (tentativa + 1))
    resultado = await conferir_continuidade(engine, numero, exigir_vigia=bool(p.CONTINUITY_REQUIRE_WATCH))
    log.info("continuidade", desfecho=str(getattr(resultado, "desfecho", resultado)))


def criar_app_despachante(p: Parametros, engine: AsyncEngine, cifra: Cifra | None = None) -> Starlette:
    """Sem ``cifra`` dada, o papel entra no cofre (OpenBao, AppRole do despachante) na partida."""
    p.conferir()
    do_cofre: _CifraDoCofre | None = None
    if cifra is None:
        do_cofre = _CifraDoCofre()
        cifra = do_cofre
    despachante = Despachante(engine, cifra, prazo_leitura=float(p.RESPONSE_MAX_WAIT_SECONDS))

    async def ler(pedido: Request) -> JSONResponse:
        try:
            leitura = PedidoLeitura.de_json(await pedido.json())
        except ValueError, KeyError, TypeError:
            return JSONResponse({"motivo": "pedido_invalido", "mensagem": "Pedido interno invalido."}, status_code=400)
        try:
            resultado = await despachante.ler(leitura)
        except LeituraRecusada as erro:
            return JSONResponse({"motivo": erro.motivo, "mensagem": erro.mensagem}, status_code=403)
        return JSONResponse(resultado)

    async def sondar(pedido: Request) -> JSONResponse:
        corpo = await pedido.json()
        sistema_id = _uuid(corpo.get("sistema_id"))
        if sistema_id is None:
            return JSONResponse({"mensagem": "sistema_id e obrigatorio"}, status_code=400)
        sondagem = await despachante.sondar_sistema(sistema_id, corpo.get("chave_sondagem"))
        return JSONResponse(
            {
                "aceito": sondagem.aceito,
                "motivo": sondagem.motivo,
                "versoes": list(sondagem.versoes),
                "inalcancavel": sondagem.inalcancavel,
                "codigo": sondagem.codigo,
            }
        )

    async def declaracoes(pedido: Request) -> JSONResponse:
        corpo = await pedido.json()
        sistema_id = _uuid(corpo.get("sistema_id"))
        if sistema_id is None:
            return JSONResponse({"mensagem": "sistema_id e obrigatorio"}, status_code=400)
        try:
            ferramentas = await despachante.ler_declaracoes(sistema_id, corpo.get("chave_sondagem"))
        except LookupError as erro:
            return JSONResponse({"mensagem": str(erro)}, status_code=404)
        except DeclaracoesIndisponiveis as erro:
            return JSONResponse({"motivo": "declaracoes_indisponiveis", "mensagem": str(erro)}, status_code=502)
        return JSONResponse({"ferramentas": ferramentas})

    async def guardar_chave(pedido: Request) -> JSONResponse:
        corpo = await pedido.json()
        sistema_id = _uuid(corpo.get("sistema_id"))
        segredo = corpo.get("segredo")
        if sistema_id is None or not isinstance(segredo, str) or not segredo:
            return JSONResponse({"mensagem": "sistema_id e segredo sao obrigatorios"}, status_code=400)
        if corpo.get("pessoa_id"):
            # Chave de pessoa so nasce da autorizacao dela na pagina do sistema (troca do codigo no despachante).
            return JSONResponse({"mensagem": "chave de pessoa so pela autorizacao no sistema"}, status_code=403)
        aplicativo_id = _uuid(corpo.get("aplicativo_id"))
        quem = str(corpo.get("quem") or "admin")
        async with engine.begin() as c:
            tipo = "declaracoes" if corpo.get("tipo") == "declaracoes" else "acesso"
            await chaves.guardar(
                c, cifra, sistema_id=sistema_id, segredo=segredo, aplicativo_id=aplicativo_id, tipo=tipo
            )
            await registrar(
                c,
                papel="despachante",
                tipo="chave_guardada",
                campos={"dono": "programa" if aplicativo_id else "compartilhada", "quem": quem[:80]},
                sistema_id=sistema_id,
                aplicativo_id=aplicativo_id,
            )
        return JSONResponse({"guardada": True})

    async def saude(_: Request) -> JSONResponse:
        return JSONResponse({"papel": "despachante", "versao": __version__, "mcp": VERSAO_MCP, "vivo": True})

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as pilha:
            if do_cofre is not None:
                from mcp_claro.cofre import Cofre

                cofre = await pilha.enter_async_context(Cofre(p))
                do_cofre.alvo = cofre.cifra()
            await pilha.enter_async_context(descarregar_contadores_periodicamente(p, engine))
            await _conferir_continuidade_na_partida(p, engine)
            log.info("partida", papel="despachante", versao=__version__, mcp=VERSAO_MCP)
            yield

    return Starlette(
        routes=[
            Route("/interno/ler", ler, methods=["POST"]),
            Route("/interno/sondar", sondar, methods=["POST"]),
            Route("/interno/declaracoes", declaracoes, methods=["POST"]),
            Route("/interno/chaves", guardar_chave, methods=["POST"]),
            Route("/saude", saude, methods=["GET"]),
            *rotas_autorizacao(engine, cifra),
        ],
        lifespan=ciclo,
    )
