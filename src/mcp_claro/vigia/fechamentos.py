"""Guarda dos fechamentos no vigia: buscar na entrada, conferir o encadeamento e guardar so acrescentando.

A cada volta, o vigia pede ao gateway (``GET /vigia/fechamentos``, com o cracha do programa
``mcp-claro-vigia``) os fechamentos a partir dos ultimos que ja guardou (``SOBREPOSICAO``); uma vez
por dia, e na primeira volta depois da partida, pede todos. Confere:

- os que ja guardou precisam vir iguais (``fechamento_mudou``) e nao podem faltar (``fechamento_sumiu``);
- no que o gateway mandou, cada um precisa encadear no anterior: ``hash_anterior`` igual ao ``hash``
  do anterior e ``desde_xid`` igual ao ``ate_xid`` dele (``encadeamento_quebrado``);
- o primeiro novo precisa encadear no ultimo guardado. O que nao encadeia nao e guardado.

Qualquer problema vira alarme. O banco do vigia confere o encadeamento de novo, por gatilho, e
recusa mudar ou apagar o que ja guardou.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any, Final

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.ronda.cracha import FalhaCracha, FonteCrachas
from mcp_claro.vigia.alarmes import alarmar
from mcp_claro.vigia.config import ConfigVigia

log = logger("mcp_claro.vigia.fechamentos")

SOBREPOSICAO: Final = 4
"""Quantos dos ultimos guardados sao conferidos de novo em cada volta."""
AUDITORIA_COMPLETA_SEGUNDOS: Final = 86400.0
PAGINA: Final = 500


class RespostaInvalida(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Fechamento:
    numero: int
    desde_xid: str
    ate_xid: str
    linhas: int
    hash_anterior: bytes | None
    hash: bytes
    calculado_em: str | None = None

    def mesmo_conteudo(self, outro: Fechamento) -> bool:
        return (
            self.numero == outro.numero
            and self.desde_xid == outro.desde_xid
            and self.ate_xid == outro.ate_xid
            and self.linhas == outro.linhas
            and self.hash_anterior == outro.hash_anterior
            and self.hash == outro.hash
        )

    def encadeia_em(self, anterior: Fechamento | None) -> bool:
        if anterior is None:
            return self.numero == 1 and self.hash_anterior is None and self.desde_xid == "0"
        return (
            self.numero == anterior.numero + 1
            and self.hash_anterior == anterior.hash
            and self.desde_xid == anterior.ate_xid
        )

    @classmethod
    def do_json(cls, item: Any) -> Fechamento:
        if not isinstance(item, dict):
            raise RespostaInvalida("item que nao e objeto")
        try:
            numero = int(item["numero"])
            desde = str(item["desde_xid"])
            ate = str(item["ate_xid"])
            linhas = int(item["linhas"])
            anterior = item.get("hash_anterior")
            hash_ = bytes.fromhex(str(item["hash"]))
            hash_anterior = bytes.fromhex(anterior) if isinstance(anterior, str) else None
        except (KeyError, TypeError, ValueError) as erro:
            raise RespostaInvalida(f"fechamento mal formado ({type(erro).__name__})") from None
        if numero < 1 or not desde.isdigit() or not ate.isdigit() or linhas < 0 or len(hash_) != 32:
            raise RespostaInvalida("fechamento com valores fora do esperado")
        calculado = item.get("calculado_em")
        return cls(numero, desde, ate, linhas, hash_anterior, hash_, calculado if isinstance(calculado, str) else None)


@dataclass(frozen=True, slots=True)
class Problema:
    tipo: str
    numero: int
    resumo: str


@dataclass(slots=True)
class Conferencia:
    novos: list[Fechamento] = field(default_factory=list)
    problemas: list[Problema] = field(default_factory=list)
    conferidos: int = 0


def conferir(
    guardados: dict[int, Fechamento], ultimo: Fechamento | None, recebidos: Sequence[Fechamento], desde: int
) -> Conferencia:
    """Compara o que o gateway mandou (numero > ``desde``) com o que o vigia guardou. Sem efeitos."""
    resultado = Conferencia()
    por_numero = {f.numero: f for f in recebidos}
    ordenados = sorted(recebidos, key=lambda f: f.numero)
    # 1. O que ja estava guardado precisa vir igual e nao pode sumir.
    if ultimo is not None:
        for numero in range(desde + 1, ultimo.numero + 1):
            guardado = guardados.get(numero)
            recebido = por_numero.get(numero)
            if guardado is None:
                continue
            if recebido is None:
                resultado.problemas.append(
                    Problema("fechamento_sumiu", numero, f"O fechamento {numero}, ja guardado, sumiu do gateway.")
                )
            elif not recebido.mesmo_conteudo(guardado):
                resultado.problemas.append(
                    Problema(
                        "fechamento_mudou",
                        numero,
                        f"O fechamento {numero} no gateway nao e mais igual ao guardado no vigia.",
                    )
                )
            else:
                resultado.conferidos += 1
    # 2. O que o gateway mandou precisa encadear, um no outro.
    for anterior, atual in pairwise(ordenados):
        if not atual.encadeia_em(anterior):
            resultado.problemas.append(
                Problema(
                    "encadeamento_quebrado",
                    atual.numero,
                    f"No gateway, o fechamento {atual.numero} nao encadeia no {anterior.numero}.",
                )
            )
    # 3. Os novos precisam encadear no ultimo guardado; o primeiro que nao encadeia para a guarda.
    base = ultimo
    for f in ordenados:
        if base is not None and f.numero <= base.numero:
            continue
        if not f.encadeia_em(base):
            anterior_txt = f"no {base.numero} guardado" if base else "no comeco da cadeia"
            problema = Problema(
                "encadeamento_quebrado",
                f.numero,
                f"O fechamento {f.numero} nao encadeia {anterior_txt}; ele nao foi guardado.",
            )
            if all((x.tipo, x.numero) != (problema.tipo, problema.numero) for x in resultado.problemas):
                resultado.problemas.append(problema)
            break
        resultado.novos.append(f)
        base = f
    return resultado


def _da_linha(linha: Any) -> Fechamento:
    calculado = linha.calculado_em
    return Fechamento(
        int(linha.numero),
        str(linha.desde_xid),
        str(linha.ate_xid),
        int(linha.linhas),
        bytes(linha.hash_anterior) if linha.hash_anterior is not None else None,
        bytes(linha.hash),
        calculado.isoformat() if isinstance(calculado, datetime) else None,
    )


_SQL_ULTIMO = """
SELECT numero, desde_xid::text AS desde_xid, ate_xid::text AS ate_xid, linhas, hash_anterior, hash, calculado_em
FROM fechamentos_guardados ORDER BY numero DESC LIMIT 1
"""
_SQL_DESDE = """
SELECT numero, desde_xid::text AS desde_xid, ate_xid::text AS ate_xid, linhas, hash_anterior, hash, calculado_em
FROM fechamentos_guardados WHERE numero > :d ORDER BY numero
"""


async def ultimo_guardado(c: AsyncConnection) -> Fechamento | None:
    linha = (await c.execute(text(_SQL_ULTIMO))).first()
    return _da_linha(linha) if linha else None


async def guardados_desde(c: AsyncConnection, desde: int) -> dict[int, Fechamento]:
    linhas = (await c.execute(text(_SQL_DESDE), {"d": desde})).all()
    return {int(linha.numero): _da_linha(linha) for linha in linhas}


async def guardar(c: AsyncConnection, novos: Sequence[Fechamento]) -> None:
    for f in novos:
        await c.execute(
            text(
                "INSERT INTO fechamentos_guardados (numero, desde_xid, ate_xid, linhas, hash_anterior, hash,"
                " calculado_em) VALUES (:n, CAST(CAST(:d AS text) AS xid8), CAST(CAST(:a AS text) AS xid8),"
                " :l, :ha, :h, CAST(CAST(:c AS text) AS timestamptz))"
            ),
            {
                "n": f.numero,
                "d": f.desde_xid,
                "a": f.ate_xid,
                "l": f.linhas,
                "ha": f.hash_anterior,
                "h": f.hash,
                "c": f.calculado_em,
            },
        )


async def buscar(http: httpx2.AsyncClient, alvo: str, cracha: str, desde: int) -> list[Fechamento]:
    """Todas as paginas de fechamentos de numero maior que ``desde``. Levanta ``RespostaInvalida``."""
    recebidos: list[Fechamento] = []
    cursor = desde
    for _ in range(10_000):
        try:
            r = await http.get(
                f"{alvo.rstrip('/')}/vigia/fechamentos",
                params={"desde": str(cursor), "limite": str(PAGINA)},
                headers={"authorization": f"Bearer {cracha}", "accept": "application/json"},
            )
        except httpx2.HTTPError as erro:
            raise RespostaInvalida(f"sem resposta ({type(erro).__name__})") from None
        if r.status_code != 200:
            raise RespostaInvalida(f"a entrada respondeu HTTP {r.status_code}")
        try:
            corpo = r.json()
        except ValueError:
            raise RespostaInvalida("resposta que nao e JSON") from None
        itens = corpo.get("fechamentos") if isinstance(corpo, dict) else None
        if not isinstance(itens, list):
            raise RespostaInvalida("resposta sem lista de fechamentos")
        pagina = [Fechamento.do_json(i) for i in itens]
        if any(f.numero <= cursor for f in pagina):
            raise RespostaInvalida("a entrada devolveu fechamento fora da ordem pedida")
        recebidos.extend(pagina)
        if not corpo.get("mais") or not pagina:
            return recebidos
        cursor = pagina[-1].numero
    raise RespostaInvalida("paginas demais")


@dataclass(frozen=True, slots=True)
class Volta:
    guardados: int
    conferidos: int
    problemas: list[Problema]
    completa: bool
    falhou: str | None = None


class GuardaFechamentos:
    """Uma volta por vez. Guarda em memoria so a hora da ultima auditoria completa e da ultima busca boa."""

    def __init__(
        self,
        cfg: ConfigVigia,
        engine: AsyncEngine,
        fonte: FonteCrachas | None,
        *,
        relogio: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.fonte = fonte
        self.relogio = relogio
        self.ultima_auditoria: float | None = None
        self.ultima_busca_boa = relogio()
        self.falhas_desde: float | None = None

    async def _sem_resposta(self, motivo: str) -> Volta:
        agora = self.relogio()
        if self.falhas_desde is None:
            self.falhas_desde = agora
        CONTADORES.somar("vigia_fechamentos_falhou")
        log.warning("vigia_fechamentos_sem_resposta", motivo=motivo)
        if agora - self.ultima_busca_boa >= self.cfg.silencio:
            async with self.engine.begin() as c:
                await alarmar(
                    c,
                    self.cfg,
                    "fechamentos_sem_resposta",
                    f"fechamentos_sem_resposta:{int(self.falhas_desde)}",
                    f"O vigia nao consegue buscar os fechamentos ha {int(agora - self.ultima_busca_boa)} segundos.",
                    {"motivo": motivo},
                )
        return Volta(0, 0, [], False, motivo)

    async def volta(self, http: httpx2.AsyncClient, *, completa: bool | None = None) -> Volta:
        if self.fonte is None:
            return await self._sem_resposta("vigia sem identidade (WATCH_KEY_FILE)")
        agora = self.relogio()
        if completa is None:
            completa = self.ultima_auditoria is None or agora - self.ultima_auditoria >= AUDITORIA_COMPLETA_SEGUNDOS
        try:
            cracha = await self.fonte.valido()
        except FalhaCracha as erro:
            return await self._sem_resposta(f"sem cracha do vigia: {erro.motivo}")
        async with self.engine.connect() as c:
            ultimo = await ultimo_guardado(c)
            desde = 0 if completa or ultimo is None else max(0, ultimo.numero - SOBREPOSICAO)
            guardados = await guardados_desde(c, desde)
        try:
            recebidos = await buscar(http, self.cfg.alvo, cracha, desde)
        except RespostaInvalida as erro:
            return await self._sem_resposta(str(erro))
        self.ultima_busca_boa = self.relogio()
        self.falhas_desde = None
        if completa:
            self.ultima_auditoria = agora
        resultado = conferir(guardados, ultimo, recebidos, desde)
        async with self.engine.begin() as c:
            await guardar(c, resultado.novos)
            for problema in resultado.problemas:
                await alarmar(
                    c,
                    self.cfg,
                    problema.tipo,
                    f"{problema.tipo}:{problema.numero}",
                    problema.resumo,
                    {"numero": problema.numero},
                )
        CONTADORES.somar("vigia_fechamentos_guardados", valor=len(resultado.novos))
        if resultado.problemas:
            log.warning("vigia_fechamentos_nao_batem", problemas=len(resultado.problemas))
        return Volta(len(resultado.novos), resultado.conferidos, resultado.problemas, completa)
