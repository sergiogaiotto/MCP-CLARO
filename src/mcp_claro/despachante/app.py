"""Aplicacao ASGI do papel ``despachante``: porta interna, so com mTLS da entrada.

Rotas internas (nunca expostas ao portao):

- ``POST /interno/ler``: leitura reconferida, uma chamada ao sistema;
- ``POST /interno/sondar``: sondagem de versao no cadastro de um sistema (com o desfecho e as versoes anunciadas;
  sistema desconhecido: 404);
- ``POST /interno/declaracoes``: le o que o sistema declara (revisao e mudou-parou). 502
  ``declaracoes_indisponiveis`` quando o sistema nao entregou; 503 com motivo proprio quando a falha e do
  MCP-CLARO (``sala_fora``: a sala de visitas fora; ``chave_indisponivel``: o cofre nao abriu a chave das
  declaracoes), para as rotinas nao contarem a rodada como "sem resposta" do servidor (critica A5);
- ``POST /interno/revisao_servidor``: conclusao da revisao de um servidor de terceiros pelo ADMIN (texto que o
  banco nao guarda, com caractere nulo ou com um pedaco de par UTF-16 sozinho, e pedido invalido: 400);
- ``POST /interno/chaves``: guarda uma chave cifrada (vinda da autorizacao da pessoa ou do ADMIN);
- ``POST /interno/autorizacao/*``: autorizacao OAuth por pessoa (``despachante.autorizacao``).

Nenhuma rota ``/interno/*`` recebe texto que o banco nao guarda (``_texto_valido``): um texto de primeiro nivel do
corpo com caractere nulo (U+0000) ou com um pedaco de par UTF-16 sozinho (``\\ud800``, que o JSON aceita) e pedido
invalido, 400 ``pedido_invalido``, antes de a rota rodar, e nunca erro 500 (``_so_texto_valido``). Cada rota ainda
confere o tipo dos proprios campos (``trace_id``, ``chave_sondagem`` e ``quem`` so texto) e o texto dentro de
listas (``publicar``). Os argumentos de uma leitura nao entram nessa regra: vao ao sistema, nao ao banco.

Toda conversa com um sistema sai por um ponto so (``Despachante.ligacao``, ``ligacao.ponto.ligacao_para``), o
mesmo da porta de saida: sistema proprio pelo cliente fixado; servidor de terceiros pela sala de visitas
(``ClienteSala``, montado aqui a partir de ``VISITS_INTERNAL_URL``; sem sala, nada vai ao terceiro). A cifra e por
origem (``cifra_origem.CifraPorOrigem``): a chave Transit dos terceiros e outra.

Na partida, depois da conferencia de continuidade, sobe a porta unica de saida (``saida.despachante``):
a fila de acoes e enviada por um despachante so, mesmo com varias copias do papel no ar. Quando o papel entra
no cofre de verdade, ele confere a chave dos terceiros (existe e e ``derived``; senao a partida recusa), e a
porta tambem confere a saude do cofre a cada ``VAULT_CHECK_SECONDS`` e fecha a saida enquanto ele estiver
trancado (``saida.cofre``).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, TypeIs

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import chaves
from mcp_claro.despachante.autorizacao import rotas_autorizacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.despachante.cifra_origem import CifraPorOrigem, conferir_chave_de_terceiros
from mcp_claro.despachante.servico import (
    TETO_PUBLICADAS,
    DeclaracoesIndisponiveis,
    DeclaracoesNaoConferidas,
    Despachante,
    LeituraRecusada,
    PedidoLeitura,
    RevisaoRecusada,
)
from mcp_claro.extrato import registrar
from mcp_claro.ligacao.ponto import Ligacao
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.logs import logger
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.tarefas import descarregar_contadores_periodicamente
from mcp_claro.versao import VERSAO_MCP
from mcp_claro.visitas.tls import contexto_do_despachante

log = logger("mcp_claro.despachante.app")


def _uuid(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor)) if valor else None
    except ValueError:
        return None


def _texto_valido(valor: Any) -> TypeIs[str]:
    """Texto que o banco guarda: ``str`` sem caractere nulo (U+0000), que o PostgreSQL recusa em texto e jsonb, e
    que se escreve em UTF-8 (o JSON aceita ``\\ud800`` sozinho, um pedaco de par que o UTF-8 nao escreve)."""
    if not isinstance(valor, str) or "\x00" in valor:
        return False
    try:
        valor.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


async def _corpo(pedido: Request) -> dict[str, Any]:
    try:
        corpo = await pedido.json()
    except ValueError:
        return {}
    return corpo if isinstance(corpo, dict) else {}


def _pedido_invalido() -> JSONResponse:
    return JSONResponse({"motivo": "pedido_invalido", "mensagem": "Pedido interno invalido."}, status_code=400)


def _so_texto_valido(rota: Route) -> Route:
    """A ``rota`` interna que recusa (400 ``pedido_invalido``), antes de rodar, o corpo JSON com um texto de primeiro
    nivel que o banco nao guarda. Vale tambem para as rotas de autorizacao (``despachante.autorizacao``), que levam
    texto da pessoa (``estado``, ``codigo``, ``erro``) e do ADMIN (``motivo``, ``segredo``): ``estado`` e ``motivo``
    com caractere nulo davam erro 500. O corpo lido fica guardado no pedido, e a rota le o mesmo."""
    rodar = rota.endpoint

    async def conferida(pedido: Request) -> Response:
        try:
            corpo = await pedido.json()
        except ValueError:
            corpo = None  # a rota decide o que fazer com o corpo que nao e JSON
        if isinstance(corpo, dict) and not all(_texto_valido(v) for v in corpo.values() if isinstance(v, str)):
            return _pedido_invalido()
        resposta: Response = await rodar(pedido)
        return resposta

    return Route(rota.path, conferida, methods=sorted(rota.methods or ()), name=rota.name)


def montar_sala(p: Parametros) -> ClienteSala | None:
    """A sala de visitas do despachante, ou None sem ``VISITS_INTERNAL_URL`` (desenvolvimento e teste; fora deles a
    partida ja exige a sala). Com https, o contexto TLS confia so na ``ca_visitas`` e leva o certificado de cliente
    do despachante; faltando arquivo, ``ConfiguracaoInvalida`` e a partida recusa."""
    if not p.VISITS_INTERNAL_URL:
        return None
    return ClienteSala(p.VISITS_INTERNAL_URL, tls=contexto_do_despachante(p))


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

    from mcp_claro.continuidade import conferir_continuidade, poupados_de
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
    resultado = await conferir_continuidade(
        engine, numero, exigir_vigia=bool(p.CONTINUITY_REQUIRE_WATCH), poupados=poupados_de(p)
    )
    log.info("continuidade", desfecho=str(getattr(resultado, "desfecho", resultado)))


async def _parar_tarefa(tarefa: asyncio.Task[None]) -> None:
    tarefa.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await tarefa


def criar_app_despachante(
    p: Parametros,
    engine: AsyncEngine,
    cifra: Cifra | None = None,
    *,
    local_para_terceiro: Ligacao | None = None,
    liberar_locais: bool = False,
) -> Starlette:
    """Sem ``cifra`` dada, o papel entra no cofre (OpenBao, AppRole do despachante) na partida e cifra por origem.

    So para testes: ``local_para_terceiro`` fala com servidores de terceiros sem a sala, e ``liberar_locais``
    aceita endereco local na troca de chave OAuth de um terceiro. Os dois so valem em desenvolvimento e teste.
    """
    p.conferir()
    sala = montar_sala(p)
    do_cofre: _CifraDoCofre | None = None
    if cifra is None:
        do_cofre = _CifraDoCofre()
        cifra = do_cofre
    ambiente = str(p.APP_ENV)
    terceiros_ligados = bool(p.FEATURE_PHASE3_THIRD_PARTY)
    despachante = Despachante(
        engine,
        cifra,
        prazo_leitura=float(p.RESPONSE_MAX_WAIT_SECONDS),
        ambiente=ambiente,
        sala=sala,
        local_para_terceiro=local_para_terceiro,
        liberar_locais=liberar_locais,
        terceiros_ligados=terceiros_ligados,
    )
    porta = PortaDeSaida(
        engine,
        cifra,
        prazo_envio=float(p.RESPONSE_MAX_WAIT_SECONDS),
        varredura=float(p.OUTBOX_SWEEP_SECONDS),
        acoes_de_programas=p.PROGRAM_ACTIONS_ENABLED,
        limite_exige_passkey=p.FEATURE_PASSKEY,
        terceiros_ligados=terceiros_ligados,
        ligacao=despachante.ligacao,
        ambiente=ambiente,
        liberar_locais=liberar_locais,
        conferir_cofre=float(p.VAULT_CHECK_SECONDS),
        emails_admin=p.NOTIFY_ADMIN_EMAILS,
    )

    async def ler(pedido: Request) -> JSONResponse:
        corpo = await _corpo(pedido)
        trace_id = corpo.get("trace_id")
        if not (trace_id is None or isinstance(trace_id, str)):
            return _pedido_invalido()  # vai a coluna de texto do extrato: um numero ou uma lista dariam erro 500
        try:
            leitura = PedidoLeitura.de_json(corpo)
        except ValueError, KeyError, TypeError:
            return _pedido_invalido()
        try:
            resultado = await despachante.ler(leitura)
        except LeituraRecusada as erro:
            return JSONResponse({"motivo": erro.motivo, "mensagem": erro.mensagem}, status_code=403)
        return JSONResponse(resultado)

    async def sondar(pedido: Request) -> JSONResponse:
        corpo = await _corpo(pedido)
        sistema_id = _uuid(corpo.get("sistema_id"))
        chave = corpo.get("chave_sondagem")
        if sistema_id is None or not (chave is None or isinstance(chave, str)):
            return JSONResponse({"mensagem": "sistema_id e obrigatorio"}, status_code=400)
        try:
            sondagem = await despachante.sondar_sistema(sistema_id, chave or None)
        except LookupError:
            return JSONResponse({"mensagem": "sistema desconhecido"}, status_code=404)
        return JSONResponse(
            {
                "aceito": sondagem.aceito,
                "desfecho": sondagem.desfecho,
                "motivo": sondagem.motivo,
                "versoes": list(sondagem.versoes),
                "versoes_anunciadas": list(sondagem.versoes),
                "inalcancavel": sondagem.inalcancavel,
                "codigo": sondagem.codigo,
            }
        )

    async def revisao_servidor(pedido: Request) -> JSONResponse:
        """Conclusao da revisao do servidor: 200 com ``revisado`` verdadeiro, ou falso com ``motivo``, ``mensagem``
        (para o ADMIN) e ``ferramentas`` [{ferramenta, problema}]; 404 sistema desconhecido; 400 pedido invalido
        (inclusive texto que o banco nao guarda em ``fornecedor``, ``quem`` ou num nome de ``publicar``, com
        caractere nulo ou com ``\\ud800`` sozinho: nunca erro 500)."""
        corpo = await _corpo(pedido)
        sistema_id = _uuid(corpo.get("sistema_id"))
        publicar = corpo.get("publicar")
        fornecedor, quem = corpo.get("fornecedor"), corpo.get("quem")
        pessoa_id = _uuid(corpo.get("pessoa_id"))
        if (
            sistema_id is None
            or not isinstance(publicar, list)
            or len(publicar) > TETO_PUBLICADAS
            or not all(_texto_valido(n) and n for n in publicar)
            or not _texto_valido(fornecedor)
            or not _texto_valido(quem)
            or (corpo.get("pessoa_id") and pessoa_id is None)
        ):
            return _pedido_invalido()
        try:
            feito = await despachante.revisar_servidor(
                sistema_id, publicar=publicar, fornecedor=fornecedor, quem=quem, pessoa_id=pessoa_id
            )
        except LookupError:
            return JSONResponse({"mensagem": "sistema desconhecido"}, status_code=404)
        except RevisaoRecusada as recusa:
            return JSONResponse(recusa.como_json())
        return JSONResponse(feito)

    async def declaracoes(pedido: Request) -> JSONResponse:
        corpo = await _corpo(pedido)
        sistema_id = _uuid(corpo.get("sistema_id"))
        chave = corpo.get("chave_sondagem")
        if sistema_id is None:
            return JSONResponse({"mensagem": "sistema_id e obrigatorio"}, status_code=400)
        if not (chave is None or isinstance(chave, str)):
            return _pedido_invalido()  # a chave de teste do ADMIN e texto; outra coisa nao e "o sistema nao entregou"
        try:
            ferramentas = await despachante.ler_declaracoes(sistema_id, chave)
        except LookupError as erro:
            return JSONResponse({"mensagem": str(erro)}, status_code=404)
        except DeclaracoesNaoConferidas as erro:
            return JSONResponse({"motivo": erro.motivo, "mensagem": str(erro)}, status_code=503)
        except DeclaracoesIndisponiveis as erro:
            return JSONResponse({"motivo": "declaracoes_indisponiveis", "mensagem": str(erro)}, status_code=502)
        return JSONResponse({"ferramentas": ferramentas})

    async def guardar_chave(pedido: Request) -> JSONResponse:
        corpo = await _corpo(pedido)
        sistema_id = _uuid(corpo.get("sistema_id"))
        segredo = corpo.get("segredo")
        if sistema_id is None or not isinstance(segredo, str) or not segredo:
            return JSONResponse({"mensagem": "sistema_id e segredo sao obrigatorios"}, status_code=400)
        if corpo.get("pessoa_id"):
            # Chave de pessoa so nasce da autorizacao dela na pagina do sistema (troca do codigo no despachante).
            return JSONResponse({"mensagem": "chave de pessoa so pela autorizacao no sistema"}, status_code=403)
        aplicativo_id = _uuid(corpo.get("aplicativo_id"))
        quem = corpo.get("quem") or "admin"
        if not _texto_valido(quem):
            return _pedido_invalido()  # vai ao extrato (jsonb)
        async with engine.begin() as c:
            existe = (await c.execute(text("SELECT 1 FROM sistemas WHERE id = :s"), {"s": sistema_id})).first()
            if existe is None:  # sem isto, a cifra (origem) ou a chave estrangeira davam erro 500
                return JSONResponse({"mensagem": "sistema desconhecido"}, status_code=404)
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
                # Critica B9: a chave dos terceiros existe e e derived; senao a partida recusa.
                await conferir_chave_de_terceiros(cofre, p.VAULT_TRANSIT_KEY_THIRD_PARTY)
                por_origem = CifraPorOrigem.do_banco(
                    engine, propria=cofre.cifra(), terceiros=cofre.cifra(p.VAULT_TRANSIT_KEY_THIRD_PARTY)
                )
                pilha.push_async_callback(por_origem.fechar)
                # A origem de todos os sistemas de uma vez, antes de atender: so um sistema novo e buscado depois
                # (por um engine sem pool, nunca pelo pool que a leitura ou a porta estao segurando).
                await por_origem.carregar()
                do_cofre.alvo = por_origem
                # So com o cofre de verdade: a porta confere a saude dele e fecha a saida enquanto estiver trancado.
                porta.cofre = cofre
            await pilha.enter_async_context(descarregar_contadores_periodicamente(p, engine))
            await _conferir_continuidade_na_partida(p, engine)
            # Porta unica de saida (Fase 2): so depois da conferencia de continuidade, que para a fila se o
            # banco voltou de um backup. Varias copias do papel podem subir; so uma envia por vez.
            tarefa = asyncio.create_task(porta.rodar(), name="porta-de-saida")
            pilha.push_async_callback(_parar_tarefa, tarefa)
            log.info("partida", papel="despachante", versao=__version__, mcp=VERSAO_MCP)
            yield

    internas = [
        Route("/interno/ler", ler, methods=["POST"]),
        Route("/interno/sondar", sondar, methods=["POST"]),
        Route("/interno/declaracoes", declaracoes, methods=["POST"]),
        Route("/interno/revisao_servidor", revisao_servidor, methods=["POST"]),
        Route("/interno/chaves", guardar_chave, methods=["POST"]),
        *rotas_autorizacao(
            engine, cifra, ambiente=ambiente, liberar_locais=liberar_locais, terceiros_ligados=terceiros_ligados
        ),
    ]
    app = Starlette(
        routes=[*(_so_texto_valido(r) for r in internas), Route("/saude", saude, methods=["GET"])],
        lifespan=ciclo,
    )
    # Para as provas: o despachante e a porta de saida montados aqui (a mesma ligacao nos dois).
    app.state.despachante = despachante
    app.state.porta = porta
    return app
