"""Telas da pessoa: "Bloquear agora" (de um toque, com comprovante), "Pausar" e "Retomar".

- Bloquear vale na hora, e so o ADMIN desfaz (``regras.bloqueios.pode_encerrar``).
- Pausar tambem vale na hora; a propria pessoa retoma quando quiser.
- A pessoa so liga no proprio alcance: um aplicativo so para ela (``pessoa_aplicativo``) ou todos os
  aplicativos dela (``pessoa``). A linha do extrato sai da propria regra (``bloquear`` e ``encerrar``).
- Tocar de novo no mesmo botao nao abre outro igual: mostra o comprovante do que ja vale.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Row
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.logs import logger
from mcp_claro.regras import bloqueios
from mcp_claro.regras.bloqueios import BloqueioInvalido, SemPermissao
from mcp_claro.telas import consultas
from mcp_claro.telas.nucleo import Telas, tela, uuid_ou_none, ver_depois

log = logger("mcp_claro.telas.bloqueio")

MOTIVOS: Final[dict[str, str]] = {
    "bloqueio": "Bloqueio de um toque pedido pela propria pessoa nas telas.",
    "pausa": "Pausa pedida pela propria pessoa nas telas.",
}
ALCANCES_LEGIVEIS: Final[dict[str, str]] = {
    "pessoa": "uma pessoa",
    "aplicativo": "um aplicativo",
    "pessoa_aplicativo": "uma pessoa num aplicativo",
    "sistema": "um sistema",
    "tudo": "todo o MCP-CLARO",
}


@dataclass(frozen=True, slots=True)
class Parada:
    """Um bloqueio ou uma pausa, como aparece nas telas."""

    id: uuid.UUID
    tipo: str
    alcance: str
    o_que: str
    quem: str
    desde: datetime
    sua: bool
    retomavel: bool
    encerrado_em: datetime | None
    encerrado_por: str | None


def quem_legivel(quem: str | None, pessoa_id: uuid.UUID | None = None) -> str:
    """Quem ligou ou encerrou, em palavras: "Você", "O ADMIN" ou "Uma rotina automática do MCP-CLARO"."""
    if not quem:
        return ""
    try:
        autor = bloqueios.autor_de(quem)
    except BloqueioInvalido:
        return "A instalação do MCP-CLARO"
    if autor.papel == "admin":
        return "O ADMIN"
    if autor.papel == "rotina":
        return "Uma rotina automática do MCP-CLARO"
    return "Você" if pessoa_id is not None and autor.pessoa_id == pessoa_id else "A própria pessoa"


def o_que_parou(linha: Row[Any], *, da_pessoa: bool) -> str:
    """O que ficou parado. ``da_pessoa``: texto para a propria pessoa; senao, para o ADMIN (com o nome)."""
    aplicativo = linha.aplicativo or "sem nome"
    pessoa = linha.pessoa or "sem nome"
    textos = {
        "pessoa": "Todos os seus aplicativos" if da_pessoa else f"A pessoa {pessoa}, em todos os aplicativos",
        "pessoa_aplicativo": (
            f"O aplicativo {aplicativo}, só para você"
            if da_pessoa
            else f"A pessoa {pessoa}, no aplicativo {aplicativo}"
        ),
        "aplicativo": f"O aplicativo {aplicativo}, para todas as pessoas",
        "sistema": f"O sistema {linha.sistema or 'sem nome'}, para todas as pessoas",
        "tudo": "Todo o MCP-CLARO, para todas as pessoas",
    }
    return textos.get(str(linha.alcance), str(linha.alcance))


def parada(linha: Row[Any], pessoa_id: uuid.UUID | None, *, da_pessoa: bool = True) -> Parada:
    sua = pessoa_id is not None and linha.pessoa_id == pessoa_id and linha.alcance in ("pessoa", "pessoa_aplicativo")
    return Parada(
        id=linha.id,
        tipo=linha.tipo,
        alcance=linha.alcance,
        o_que=o_que_parou(linha, da_pessoa=da_pessoa),
        quem=quem_legivel(linha.quem, pessoa_id),
        desde=linha.criado_em,
        sua=sua,
        retomavel=sua and linha.tipo == "pausa" and linha.encerrado_em is None,
        encerrado_em=linha.encerrado_em,
        encerrado_por=quem_legivel(linha.encerrado_por, pessoa_id) if linha.encerrado_por else None,
    )


class TelasDeBloqueio:
    def __init__(self, t: Telas) -> None:
        self.t = t

    def rotas(self) -> list[BaseRoute]:
        return [
            Route("/bloqueio", self.bloqueio, methods=["GET"]),
            Route("/bloqueio", self.bloquear, methods=["POST"]),
            Route("/bloqueio/{bloqueio_id:uuid}", self.comprovante, methods=["GET"]),
            Route("/pausa", self.pausa, methods=["GET"]),
            Route("/pausa", self.pausar, methods=["POST"]),
            Route("/pausa/{bloqueio_id:uuid}/retomar", self.retomar, methods=["POST"]),
        ]

    async def _pagina(
        self, pedido: Request, tipo: str, *, erros: list[str] | None = None, status: int = 200
    ) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            aplicativos = await consultas.aplicativos_para_parar(c, sessao.pessoa.id)
            linhas = await consultas.bloqueios_que_tocam(c, sessao.pessoa.id)
        paradas = [parada(linha, sessao.pessoa.id) for linha in linhas]
        return self.t.pagina(
            pedido,
            "bloqueio.html" if tipo == "bloqueio" else "pausa.html",
            {
                "aplicativos": aplicativos,
                "suas": [p for p in paradas if p.sua],
                "gerais": [p for p in paradas if not p.sua],
                "erros": erros or [],
            },
            status=status,
        )

    @tela
    async def bloqueio(self, pedido: Request) -> Response:
        return await self._pagina(pedido, "bloqueio")

    @tela
    async def pausa(self, pedido: Request) -> Response:
        return await self._pagina(pedido, "pausa")

    @tela
    async def bloquear(self, pedido: Request) -> Response:
        return await self._ligar(pedido, "bloqueio")

    @tela
    async def pausar(self, pedido: Request) -> Response:
        return await self._ligar(pedido, "pausa")

    async def _ligar(self, pedido: Request, tipo: str) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        escolha = f.texto("aplicativo")
        pessoa_id = sessao.pessoa.id
        aplicativo_id: uuid.UUID | None = None
        alcance = "pessoa"
        if escolha != "todos":
            alcance = "pessoa_aplicativo"
            aplicativo_id = uuid_ou_none(escolha)
            async with self.t.engine.connect() as c:
                validos = {a.id for a in await consultas.aplicativos_para_parar(c, pessoa_id)}
            if aplicativo_id not in validos:
                return await self._pagina(pedido, tipo, erros=["Escolha um dos aplicativos da lista."], status=400)
        try:
            async with self.t.engine.begin() as c:
                existente = await consultas.bloqueio_igual_ativo(
                    c, tipo, alcance, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id
                )
                if existente is not None:
                    return RedirectResponse(f"/telas/bloqueio/{existente}?aviso=ja_valia", status_code=303)
                feito = await bloqueios.bloquear(
                    c,
                    tipo,
                    alcance,
                    bloqueios.quem_pessoa(pessoa_id),
                    MOTIVOS[tipo],
                    pessoa_id=pessoa_id,
                    aplicativo_id=aplicativo_id,
                )
        except BloqueioInvalido, SemPermissao:
            return self.t.erro(pedido, 403, "Pedido recusado", "Você só pode parar o seu próprio acesso. Nada mudou.")
        log.info("telas_parada_ligada", tipo=tipo, alcance=alcance)
        aviso = "bloqueado" if tipo == "bloqueio" else "pausado"
        return RedirectResponse(f"/telas/bloqueio/{feito.id}?aviso={aviso}", status_code=303)

    @tela
    async def comprovante(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            linha = await consultas.bloqueio_com_nomes(c, pedido.path_params["bloqueio_id"])
        if linha is None or linha.pessoa_id != sessao.pessoa.id or linha.alcance not in ("pessoa", "pessoa_aplicativo"):
            return self.t.erro(pedido, 404, "Comprovante não encontrado", "Este comprovante não é seu ou não existe.")
        return self.t.pagina(pedido, "comprovante.html", {"p": parada(linha, sessao.pessoa.id)})

    @tela
    async def retomar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        bloqueio_id: uuid.UUID = pedido.path_params["bloqueio_id"]
        quem = bloqueios.quem_pessoa(sessao.pessoa.id)
        async with self.t.engine.connect() as c:
            antes = await bloqueios.por_id(c, bloqueio_id)
        if antes is None or antes.pessoa_id != sessao.pessoa.id:
            return self.t.erro(pedido, 404, "Não encontrado", "Esta pausa não é sua ou não existe. Nada mudou.")
        if not antes.ativo:
            return ver_depois("/telas/pausa", "ja_terminou")
        try:
            async with self.t.engine.begin() as c:
                await bloqueios.encerrar(c, bloqueio_id, quem)
        except LookupError:
            return self.t.erro(pedido, 404, "Não encontrado", "Esta pausa não existe. Nada mudou.")
        except SemPermissao:
            if antes.tipo == "bloqueio":
                mensagem = "Um bloqueio só o ADMIN desfaz. Nada mudou. Se precisar, fale com o ADMIN."
            else:
                mensagem = "Esta pausa só o ADMIN retoma. Nada mudou. Se precisar, fale com o ADMIN."
            return self.t.erro(pedido, 403, "Só o ADMIN desfaz", mensagem)
        log.info("telas_pausa_retomada", alcance=antes.alcance)
        return ver_depois("/telas/pausa", "pausa_retomada")
