"""Telas da pessoa: "Bloquear agora" (de um toque, com comprovante), "Pausar" e "Retomar".

- Bloquear vale na hora, e so o ADMIN desfaz (``regras.bloqueios.pode_encerrar``).
- Pausar tambem vale na hora; a propria pessoa retoma quando quiser.
- A pessoa so liga no proprio alcance: um aplicativo so para ela (``pessoa_aplicativo``) ou todos os
  aplicativos dela (``pessoa``). A linha do extrato sai da propria regra (``bloquear`` e ``encerrar``).
- Tocar de novo no mesmo botao nao abre outro igual: mostra o comprovante do que ja vale.
- O comprovante mostra tambem as tres partes da porta de saida (Fase 2), como na proposta: o que paramos a
  tempo, o que ja tinha seguido e o que ainda esta em conferencia.

Fase 4 (desenho 6.3 e 7.1): o comprovante ganha a quarta parte, "O que o sistema informou (nivel 3)"
(``quatro_partes``), da leitura de agora (``Comprovante.informado`` e ``Comprovante.avisos``), nunca da linha de
``comprovantes``, que nao muda. So as categorias do contrato e a hora do gateway, marcadas como informadas pelo sistema
(ou pelo servidor de terceiros, critica M9); nunca texto, identificador ou hora do sistema. O andamento de cada aviso
nao culpa o sistema pelo que e do MCP-CLARO: ``pendente`` e "o aviso ainda nao saiu" (a chave da pessoa nao abriu, ou a
porta esta fechada), ``sistema_fora`` e "o MCP-CLARO nao avisou o sistema", ``sem_resposta`` so quando o sistema foi
chamado em todas as tentativas, e ``bloqueio_encerrado`` e "o bloqueio acabou antes de o aviso sair". A dica fixa de
"ja tinha seguido" vira uma dica por linha, pelo nivel do selo com que cada acao saiu (``envios_selo.nivel``; sem
envio com numero, nivel 1). A leitura traz tambem a ultima resposta a consulta de uma acao de nivel 2 em conferencia:
com ela, a quarta parte nao diz "nivel 3" no titulo, e cada linha diz o nivel com que a acao saiu
(``QuatroPartes.so_nivel_3``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Row
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.logs import logger
from mcp_claro.regras import bloqueios
from mcp_claro.regras.bloqueios import BloqueioInvalido, SemPermissao
from mcp_claro.saida import fila
from mcp_claro.saida.protocolo import FUSO
from mcp_claro.telas import consultas
from mcp_claro.telas.nucleo import Telas, tela, uuid_ou_none, ver_depois
from mcp_claro.telas.paginas import estado_legivel

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


ESTAVA: Final[dict[str, str]] = {
    "aguardando_ficha": "esperava a sua ficha",
    "liberada": "estava na espera para sair",
}
SAIDA: Final[dict[str, str]] = {
    "enviando": "estava saindo",
    "enviada": "entregue ao sistema",
    "nao_confirmada": "resultado não confirmado",
    "sistema_pediu_dados": "o sistema pediu mais dados",
}
# O comprovante e o retrato da hora do bloqueio. Uma acao "nao confirmada" pode nem ter saido (a retomada a da como
# nao confirmada quando o envio foi interrompido), e o fim que chega depois nao muda o comprovante: por isso "pode ter".
EM_CONFERENCIA: Final[dict[str, str]] = {
    "enviando": "estava saindo na hora do bloqueio",
    "nao_confirmada": "pode ter saído, mas a resposta do sistema não chegou inteira",
}
# Sem a parte "em conferencia" (comprovante de antes dela), estas ficam nela, e nao em "ja tinha seguido".
ESTADOS_EM_CONFERENCIA: Final = frozenset(EM_CONFERENCIA)


@dataclass(frozen=True, slots=True)
class Informou:
    """Uma linha da quarta parte do comprovante: o que um sistema informou sobre uma acao (``Comprovante.informado``),
    so com o vocabulario do contrato e a hora do gateway."""

    protocolo: str
    sistema: str
    de_terceiro: bool
    estado: str
    """O estado em palavras (``paginas.ESTADOS_LEGIVEIS``)."""
    origem: str
    """De que pergunta veio: o aviso do bloqueio, a consulta ou a resposta da propria acao (``ORIGENS``)."""
    hora: str
    """A hora em que o MCP-CLARO gravou a resposta (``gravado_em``, horario de Brasilia), nunca uma hora do sistema."""


@dataclass(frozen=True, slots=True)
class AvisoDoBloqueio:
    """O andamento do aviso do bloqueio a um sistema (``Comprovante.avisos``): ``estado`` e um de
    ``contrato.ESTADOS_DO_AVISO`` (a tela tem uma frase para cada um)."""

    sistema: str
    de_terceiro: bool
    estado: str


ORIGENS: Final[dict[str, str]] = {
    "aviso": "em resposta ao aviso do bloqueio",
    "consulta": "em resposta à consulta do MCP-CLARO",
    "recibo": "na resposta da própria ação",
}
"""De onde veio cada linha de ``informacoes_sistema`` (``contrato.ORIGENS``), como o comprovante diz."""


@dataclass(frozen=True, slots=True)
class QuatroPartes:
    """Comprovante do bloqueio (``fila.comprovante_do_bloqueio``), como a tela mostra: o que paramos a tempo, o que
    ja tinha seguido, o que ainda esta em conferencia e, na Fase 4, o que os sistemas informaram (``informado``) com o
    andamento dos avisos (``avisos``). ``niveis`` e o nivel do selo com que cada acao saiu (pelo protocolo; fora dele,
    nivel 1): a dica por linha de ``ja_saidas`` e o nivel da quarta parte (``so_nivel_3``)."""

    paradas: list[tuple[str, str]]
    ja_saidas: list[tuple[str, str, str]]
    em_conferencia: list[tuple[str, str, str]]
    informado: list[Informou] = field(default_factory=list)
    avisos: list[AvisoDoBloqueio] = field(default_factory=list)
    niveis: Mapping[str, int] = field(default_factory=dict)

    def nivel(self, protocolo: str) -> int:
        """O nivel do selo com que a acao saiu (``envios_selo.nivel``); sem envio com numero, 1."""
        return self.niveis.get(protocolo, 1)

    @property
    def tem_quarta_parte(self) -> bool:
        return bool(self.informado or self.avisos)

    @property
    def so_nivel_3(self) -> bool:
        """Toda linha da quarta parte e de uma acao que saiu no nivel 3 (o caso do desenho 6.3): o titulo diz "nivel 3".
        A leitura (``fila.comprovante_do_bloqueio``) traz tambem a ultima resposta a consulta de uma acao de nivel 2 em
        conferencia; com ela, o titulo nao diz "nivel 3" e cada linha diz o nivel com que a acao saiu."""
        return all(self.nivel(i.protocolo) == 3 for i in self.informado)


def _momento(valor: Any) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        return datetime.fromisoformat(valor)
    except ValueError:
        return None


def _quando(valor: Any) -> str:
    momento = _momento(valor)
    return momento.astimezone(ZoneInfo(FUSO)).strftime("%d/%m/%Y %H:%M") if momento else ""


def _hora(valor: Any) -> str:
    momento = _momento(valor)
    return momento.astimezone(ZoneInfo(FUSO)).strftime("%H:%M de %d/%m/%Y") if momento else ""


def _informado(c: fila.Comprovante) -> list[Informou]:
    linhas: list[Informou] = []
    for i in getattr(c, "informado", None) or []:
        if not isinstance(i, Mapping) or i.get("estado") is None:
            continue
        linhas.append(
            Informou(
                protocolo=str(i.get("protocolo", "")),
                sistema=str(i.get("sistema", "")),
                de_terceiro=i.get("de_terceiro") is True,
                estado=estado_legivel(i.get("estado")),
                origem=ORIGENS.get(str(i.get("origem")), ""),
                hora=_hora(i.get("gravado_em")),
            )
        )
    return linhas


def _avisos(c: fila.Comprovante) -> list[AvisoDoBloqueio]:
    """Um por sistema e andamento (um bloqueio pode ter varias partes de ate 100 numeros por sistema)."""
    vistos: list[AvisoDoBloqueio] = []
    for a in getattr(c, "avisos", None) or []:
        if not isinstance(a, Mapping):
            continue
        aviso = AvisoDoBloqueio(str(a.get("sistema", "")), a.get("de_terceiro") is True, str(a.get("estado", "")))
        if aviso not in vistos:
            vistos.append(aviso)
    return vistos


def quatro_partes(c: fila.Comprovante | None, niveis: Mapping[str, int] | None = None) -> QuatroPartes | None:
    if c is None:
        return None
    paradas = [(str(p.get("protocolo", "")), ESTAVA.get(str(p.get("estava")), str(p.get("estava")))) for p in c.paradas]
    seguiram = list(c.ja_saidas)
    conferindo: list[dict[str, Any]] | None = getattr(c, "em_conferencia", None)
    if conferindo is None:
        # Comprovante sem a parte "em conferencia": o que estava saindo ou ficou nao confirmado vai para ela.
        conferindo = [s for s in seguiram if str(s.get("estado")) in ESTADOS_EM_CONFERENCIA]
        seguiram = [s for s in seguiram if str(s.get("estado")) not in ESTADOS_EM_CONFERENCIA]
    ja_saidas = [
        (
            str(s.get("protocolo", "")),
            SAIDA.get(str(s.get("estado")), str(s.get("estado"))),
            _quando(s.get("enviada_em")),
        )
        for s in seguiram
    ]
    em_conferencia = [
        (
            str(s.get("protocolo", "")),
            EM_CONFERENCIA.get(str(s.get("estado")), SAIDA.get(str(s.get("estado")), str(s.get("estado")))),
            _quando(s.get("desde") or s.get("enviada_em")),
        )
        for s in conferindo
    ]
    return QuatroPartes(paradas, ja_saidas, em_conferencia, _informado(c), _avisos(c), dict(niveis or {}))


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
        niveis: dict[str, int] = {}
        async with self.t.engine.connect() as c:
            linha = await consultas.bloqueio_com_nomes(c, pedido.path_params["bloqueio_id"])
            da_pessoa = (
                linha is not None
                and linha.pessoa_id == sessao.pessoa.id
                and linha.alcance in ("pessoa", "pessoa_aplicativo")
            )
            partes = await fila.comprovante_do_bloqueio(c, linha.id) if linha is not None and da_pessoa else None
            if partes is not None:
                # O nivel com que cada acao saiu: a dica por linha de "ja tinha seguido" e o nivel da quarta parte
                # (o que o sistema informou pode ser de uma acao em conferencia, de nivel 2).
                linhas = [*partes.ja_saidas, *(partes.em_conferencia or []), *(partes.informado or [])]
                protocolos = sorted({str(s.get("protocolo", "")) for s in linhas if isinstance(s, Mapping)})
                niveis = await consultas.niveis_do_envio(c, sessao.pessoa.id, protocolos)
        if linha is None or not da_pessoa:
            return self.t.erro(pedido, 404, "Comprovante não encontrado", "Este comprovante não é seu ou não existe.")
        return self.t.pagina(
            pedido, "comprovante.html", {"p": parada(linha, sessao.pessoa.id), "partes": quatro_partes(partes, niveis)}
        )

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
