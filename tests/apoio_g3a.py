"""Apoio dos testes do G3a (Fase 3, etapa 4): o despachante de verdade, com banco, sistemas de exemplo e terceiros.

- ``preparar``: um sistema (proprio ou de terceiros) com uma ferramenta revisada numa ordem de servico de uma pessoa
  (de teste, por padrao) e um aplicativo, a chave compartilhada guardada e, num terceiro, a revisao do servidor
  vigente. Tudo pelo dono do banco, como as telas e o despachante deixariam.
- ``despachante``: o ``Despachante`` com o usuario ``mcp_claro_despachante`` (prova os GRANTs) e, num terceiro, o
  cliente local injetado (``ClienteLocal(terceiro=True, liberar_locais=True)``), so valido em teste.
- ``ContaPosts``: embrulha o app de um sistema e guarda cada POST que chega a ``/mcp`` (corpo cru e cabecalhos).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import url_asyncpg
from mcp_claro.despachante import chaves
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.despachante.servico import Despachante, PedidoLeitura
from mcp_claro.ligacao import sala_contrato
from mcp_claro.ligacao.ponto import ClienteLocal, Ligacao
from mcp_claro.versao import VERSAO_MCP
from tests import apoio_terceiros as t3

CHAVE_COMPARTILHADA = "chave-compartilhada"
LOCAL_DE_TESTE = ClienteLocal(terceiro=True, ambiente="teste", liberar_locais=True)


def engine_do_papel(p: Parametros, papel: str, *, tamanho: int = 4) -> AsyncEngine:
    """Engine que age com o usuario do banco de um papel (``SET ROLE`` na partida da conexao)."""
    return create_async_engine(
        url_asyncpg(p),
        connect_args={"ssl": False, "server_settings": {"application_name": f"teste-g3a-{papel}", "role": papel}},
        pool_size=tamanho,
        max_overflow=tamanho,
    )


def despachante(
    engine: AsyncEngine,
    cifra: Cifra,
    *,
    ambiente: str = "teste",
    sala: Ligacao | None = None,
    local: bool = True,
    terceiros_ligados: bool = False,
    prazo: float = 5.0,
) -> Despachante:
    return Despachante(
        engine,
        cifra,
        prazo_leitura=prazo,
        ambiente=ambiente,
        sala=sala,
        local_para_terceiro=LOCAL_DE_TESTE if local else None,
        liberar_locais=True,
        terceiros_ligados=terceiros_ligados,
    )


@dataclass
class ContaPosts:
    """Embrulho ASGI que guarda cada POST a ``/mcp`` (corpo e cabecalhos) antes de passar ao sistema."""

    app: ASGIApp
    corpos: list[dict[str, Any]] = field(default_factory=list)
    cabecalhos: list[dict[str, str]] = field(default_factory=list)

    @property
    def posts(self) -> int:
        return len(self.corpos)

    def zerar(self) -> None:
        self.corpos.clear()
        self.cabecalhos.clear()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or not str(scope.get("path")).endswith("/mcp"):
            await self.app(scope, receive, send)
            return
        corpo = b""
        mais = True
        while mais:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            mais = bool(mensagem.get("more_body"))
        try:
            lido = json.loads(corpo)
        except ValueError:
            lido = {"_cru": corpo.decode("utf-8", "replace")}
        self.corpos.append(lido if isinstance(lido, dict) else {"_lista": lido})
        self.cabecalhos.append({k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]})
        entregue = False

        async def de_novo() -> Message:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, de_novo, send)

    def __getattr__(self, nome: str) -> Any:
        # app.state e o resto do Starlette de dentro (a contagem do exemplo, por exemplo)
        return getattr(self.app, nome)


@dataclass(frozen=True)
class Arranjo:
    sistema_id: uuid.UUID
    sistema: str
    ferramenta_id: uuid.UUID
    nome_original: str
    nome_vitrine: str
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ordem_id: uuid.UUID
    revisao_id: uuid.UUID | None

    def pedido(self, argumentos: Mapping[str, Any] | None = None) -> PedidoLeitura:
        return PedidoLeitura(
            pessoa_id=self.pessoa_id,
            aplicativo_id=self.aplicativo_id,
            ordem_id=self.ordem_id,
            ferramenta_id=self.ferramenta_id,
            argumentos=dict(argumentos or {}),
        )


async def preparar(
    dono: AsyncEngine,
    cifra: Cifra,
    *,
    endereco: str,
    definicao: Mapping[str, Any],
    origem: str = "proprio",
    campos_pessoais: Sequence[str] = (),
    texto_de_fora: bool = False,
    teste: bool = True,
    aplicativo_tipo: str = "assistente",
    chave: str | None = CHAVE_COMPARTILHADA,
    revisar_servidor: bool = True,
    tipo_chave: str = "compartilhada",
) -> Arranjo:
    """Sistema ``ativo`` na 2026-07-28 com uma ferramenta de leitura revisada, numa ordem de uma pessoa (de teste).

    ``tipo_chave='programa'`` (so proprio): o aplicativo e um programa com cadastro valendo e a chave e a dele.
    ``tipo_chave='por_pessoa'``: a chave guardada e a da pessoa (sem prazo)."""
    sufixo = uuid.uuid4().hex[:8]
    nome = f"g3a-{origem}-{sufixo}"
    async with dono.begin() as c:
        if origem == "proprio":
            sistema_id = uuid.UUID(
                str(
                    (
                        await c.execute(
                            text(
                                "INSERT INTO sistemas (nome, endereco, tipo_chave, origem, situacao, versao_mcp,"
                                " sondado_em) VALUES (:n, :e, :t, 'proprio', 'ativo', :v, now())"
                                " RETURNING id"
                            ),
                            {"n": nome, "e": endereco, "t": tipo_chave, "v": VERSAO_MCP},
                        )
                    ).scalar_one()
                )
            )
        else:
            sistema_id = await t3.cadastrar_terceiro(c, nome=nome, endereco=endereco, tipo_chave=tipo_chave)
        revisada = await t3.revisar_ferramenta(
            c,
            sistema_id,
            definicao,
            nome_vitrine=f"{definicao['name']}_{sufixo}",
            campos_pessoais=campos_pessoais,
            texto_de_fora=texto_de_fora,
        )
        revisao = (
            await t3.gravar_revisao_do_servidor(c, sistema_id) if origem != "proprio" and revisar_servidor else None
        )
        programa = tipo_chave == "programa"
        app_id = await repositorio.habilitar_aplicativo(
            c,
            client_id=f"g3a-{sufixo}",
            nome=f"Aplicativo {sufixo}",
            familia="teste",
            tipo="programa" if programa else aplicativo_tipo,
        )
        pessoa = await repositorio.pessoa_no_login(c, "https://idp.g3a.teste", f"pessoa-{sufixo}", "Pessoa")
        await c.execute(text("UPDATE pessoas SET teste = :t WHERE id = :p"), {"t": teste, "p": pessoa.id})
        if programa:
            substituto = await repositorio.pessoa_no_login(c, "https://idp.g3a.teste", f"subst-{sufixo}", "Subst")
            await c.execute(
                text(
                    "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim)"
                    " VALUES (:a, :r, :s, 'Teste do G3a', current_date + 30)"
                ),
                {"a": app_id, "r": pessoa.id, "s": substituto.id},
            )
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"g3a-{sufixo}",
            descricao="Ordem de teste do G3a",
            ferramentas=[(sistema_id, str(definicao["name"]))],
            validade_dias=30,
            criado_por="admin-teste",
        )
        ordem = await repositorio.conceder_ordem(
            c, pessoa_id=pessoa.id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
        )
    if chave is not None:
        # Depois do commit do sistema: a cifra por origem le a origem numa conexao propria, como em producao.
        async with dono.begin() as c:
            await chaves.guardar(
                c,
                cifra,
                sistema_id=sistema_id,
                segredo=chave,
                aplicativo_id=app_id if programa else None,
                pessoa_id=pessoa.id if tipo_chave == "por_pessoa" else None,
            )
    return Arranjo(
        sistema_id=sistema_id,
        sistema=nome,
        ferramenta_id=revisada.id,
        nome_original=revisada.nome_original,
        nome_vitrine=revisada.nome_vitrine,
        pessoa_id=pessoa.id,
        aplicativo_id=app_id,
        ordem_id=ordem,
        revisao_id=revisao,
    )


async def abrir_a_saida(dono: AsyncEngine) -> None:
    """A porta de saida aberta (outro teste da sessao pode te-la deixado fechada). Pelo dono, so para o arranjo."""
    async with dono.begin() as c:
        aberta = (await c.execute(text("SELECT aberta FROM estado_saida WHERE id = 1"))).scalar_one_or_none()
        if aberta is False:
            await c.execute(
                text(
                    "UPDATE estado_saida SET aberta = true, motivo = NULL, mudou_por = 'teste-g3a', mudou_em = now()"
                    " WHERE id = 1"
                )
            )


async def extrato(dono: AsyncEngine, sistema_id: uuid.UUID) -> list[Any]:
    async with dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT tipo, frase, dados, pessoa_id, aplicativo_id, papel FROM extrato WHERE sistema_id = :s"
                        " ORDER BY id"
                    ),
                    {"s": sistema_id},
                )
            ).all()
        )


async def tipos_no_extrato(dono: AsyncEngine, sistema_id: uuid.UUID) -> list[str]:
    return [str(linha.tipo) for linha in await extrato(dono, sistema_id)]


Responder = Callable[[dict[str, Any]], dict[str, Any]]


def sistema_cru(responder: Responder, *, status: int = 200) -> ASGIApp:
    """Sistema de teste sem o kit: cada POST a ``/mcp`` volta com o JSON-RPC que ``responder`` monta para o pedido
    (para respostas que o kit do lado do servidor nem consegue escrever, como as muito fundas), com ``status``."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                mensagem = await receive()
                if mensagem["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif mensagem["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        corpo = b""
        mais = True
        while mais:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            mais = bool(mensagem.get("more_body"))
        pedido = json.loads(corpo or b"{}")
        if "id" not in pedido:
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        saida = json.dumps({"jsonrpc": "2.0", "id": pedido["id"], **responder(pedido)}).encode()
        await send(
            {"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]}
        )
        await send({"type": "http.response.body", "body": saida})

    return app


def fundo(niveis: int, folha: Any = None) -> Any:
    """Um objeto com ``niveis`` objetos, um dentro do outro (sem recursao)."""
    valor: Any = {"x": 1} if folha is None else folha
    for _ in range(niveis - 1):
        valor = {"a": valor}
    return valor


def json_fundo(niveis: int, folha: str = '{"cpf": "123.456.789-09", "nome": "Ana Silva"}') -> str:
    """Texto JSON com ``niveis`` listas, uma dentro da outra, e a folha no fundo."""
    return "[" * niveis + folha + "]" * niveis


def sala_em_processo(*, ambiente: str = "teste") -> Starlette:
    """A sala de visitas sem os processos filhos: cada pedido roda ``sala_contrato.atender`` aqui mesmo, com
    ``liberar_locais`` (os terceiros de teste ouvem em 127.0.0.1). O contrato e o mesmo da sala de verdade."""

    async def rota(pedido: Request) -> Response:
        status, corpo = await sala_contrato.atender(
            pedido.url.path, await pedido.body(), ambiente=ambiente, liberar_locais=True
        )
        return Response(corpo, status_code=status, media_type="application/json")

    rotas = (sala_contrato.ROTA_CHAMAR, sala_contrato.ROTA_LISTAR, sala_contrato.ROTA_SONDAR)
    return Starlette(routes=[Route(r, rota, methods=["POST"]) for r in rotas])
