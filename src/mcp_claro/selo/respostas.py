"""Validacao estrita das respostas do contrato do selo (desenho 2.2, 2.6 e 5.5; criticas A3, B4, B8 e B9).

O que um sistema responde as perguntas do gateway passa por aqui antes de qualquer gravacao: a consulta
(``selo_consultar``), a resposta ao aviso de bloqueio (``selo_bloqueio``) e o recibo do nivel 3 no ``_meta`` do
resultado de uma acao (``contrato.CHAVE_RECIBO``). A porta de saida, a consulta, o aviso e a bateria usam as mesmas
funcoes. As regras:

- so os membros do contrato sao lidos. Um membro a mais e ignorado e nunca guardado: o que sai daqui so tem
  categorias do vocabulario do contrato, contagens e horas ja convertidas pelo MCP-CLARO, nunca texto do sistema;
- o numero e conferido: a resposta fala do numero perguntado (e do aviso perguntado), e de nenhum outro;
- o vocabulario e fechado. Um estado ou um evento fora dele torna a resposta invalida, inclusive um "IGNORE AS
  REGRAS" escondido no estado. A hora de um evento e uma data e hora com fuso (RFC 3339, com o segundo bissexto lido
  como o segundo seguinte) e, quando quem chama diz a janela, fica entre o envio menos 5 minutos e agora mais 5 minutos
  (``contrato.MARGEM_EVENTO_MINUTOS``, a regra do banco);
- o teto e de 16 KiB (``contrato.TETO_RESPOSTA_BYTES``) por resposta do contrato, medido sem recursao e parando
  assim que passa (nem um JSON de muitos niveis estoura a pilha);
- a resposta da consulta e do aviso vem em ``structuredContent``, sem ``isError``; o recibo combina com o ``isError``
  da chamada (``concluida`` e ``em_andamento`` sem erro; os outros estados com erro, SELO-3.1).

Nada daqui levanta excecao por causa do que o sistema mandou: o resultado e a resposta lida ou ``Invalida``, com um
motivo da lista fechada ``MOTIVOS_DA_INVALIDA`` (para o log e o contador, nunca para a pessoa). A resposta invalida
conta como falha do sistema (``resposta_invalida``) e nada dela e mostrado nem guardado alem do desfecho.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import mcp_types as types

from mcp_claro.selo import contrato

MOTIVOS_DA_INVALIDA: Final = (
    "nao_e_resultado",
    "com_erro",
    "sem_estruturado",
    "grande_demais",
    "forma",
    "contrato",
    "outro_numero",
    "outro_aviso",
    "estado_fora",
    "depois_fora",
    "evento_fora",
    "hora_fora",
    "numero_a_mais",
    "numero_repetido",
    "numero_faltando",
    "recibo_ausente",
    "incoerente",
    "objetos",
    "itens",
    "repetida",
)
"""Por que uma resposta do contrato e invalida. So vai ao log e ao contador: a pessoa e o aplicativo nunca veem."""

ESTADOS_DE_SUCESSO: Final = frozenset({"concluida", "em_andamento"})
"""O recibo com um destes estados vem sem erro (``isError`` falso); com os outros, com erro (SELO-3.1, 3.3, 3.4)."""

FUNDO_MAXIMO: Final = 32
"""Niveis de listas e objetos que uma resposta do contrato pode ter: a mais funda (a consulta com ``depois``) tem 3.
Mais fundo, a resposta e invalida, sem andar o resto."""

MAX_OBJETOS: Final = 1000
"""Objetos que um recibo pode citar: o teto de ``informacoes_sistema.objetos`` (o gateway guarda so a contagem)."""

_DATA_HORA: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
"""Data e hora com fuso, a forma ``date-time`` do esquema (RFC 3339, secao 5.6). So algarismos ASCII."""
_TETO_DA_HORA: Final = 40


@dataclass(frozen=True, slots=True)
class Invalida:
    """A resposta nao cumpre o contrato. ``motivo`` e de ``MOTIVOS_DA_INVALIDA``."""

    motivo: str


@dataclass(frozen=True, slots=True)
class Evento:
    """Um evento de ``depois`` (SELO-3.5): do vocabulario, com a hora ja convertida pelo MCP-CLARO."""

    evento: str
    em: datetime

    def guardado(self) -> dict[str, str]:
        """Como o evento vai a ``informacoes_sistema.depois``: o evento e a hora escrita pelo MCP-CLARO."""
        return {"evento": self.evento, "em": self.em.isoformat()}


@dataclass(frozen=True, slots=True)
class Consulta:
    """A resposta valida de ``selo_consultar``."""

    estado: str
    depois: tuple[Evento, ...] = ()


@dataclass(frozen=True, slots=True)
class RespostaAoAviso:
    """A resposta valida de ``selo_bloqueio``: o estado de cada numero pedido, na ordem do pedido."""

    estados: tuple[tuple[str, str], ...]

    def estado_de(self, numero: str) -> str:
        return dict(self.estados)[numero]


@dataclass(frozen=True, slots=True)
class Recibo:
    """O recibo valido do nivel 3 (SELO-3.2). ``objetos`` e so a contagem dos objetos citados (D21); ``itens``,
    as contagens (aplicados, nao aplicados) de uma escrita de varios itens (SELO-3.3)."""

    estado: str
    repetida: bool = False
    objetos: int | None = None
    itens: tuple[int, int] | None = None


# --- Tamanho e fundo, sem recursao -------------------------------------------------------------------------------


def _tamanho_do_texto(texto: str) -> int:
    """Bytes do texto como o JSON o escreve em UTF-8, com as aspas e os escapes."""
    escapes = sum(1 if letra in '"\\\b\f\n\r\t' else 5 for letra in texto if letra in '"\\' or letra < " ")
    return len(texto.encode("utf-8", "surrogatepass")) + 2 + escapes


def tamanho_json(valor: Any, teto: int = contrato.TETO_RESPOSTA_BYTES) -> int | None:
    """Os bytes de ``valor`` escrito em JSON compacto (UTF-8), ou ``None`` se passa de ``teto``, se tem mais de
    ``FUNDO_MAXIMO`` niveis ou se nao e JSON (inclusive um numero infinito ou NaN). Sem recursao, e para assim que
    passa do teto."""
    total = 0
    pilha: list[tuple[Any, int]] = [(valor, 1)]
    while pilha:
        atual, nivel = pilha.pop()
        if isinstance(atual, dict):
            if nivel > FUNDO_MAXIMO:
                return None
            total += 2 + max(0, len(atual) - 1)
            for chave, filho in atual.items():
                if not isinstance(chave, str):
                    return None
                total += _tamanho_do_texto(chave) + 1
                pilha.append((filho, nivel + 1))
        elif isinstance(atual, list):
            if nivel > FUNDO_MAXIMO:
                return None
            total += 2 + max(0, len(atual) - 1)
            pilha.extend((filho, nivel + 1) for filho in atual)
        elif isinstance(atual, str):
            total += _tamanho_do_texto(atual)
        elif atual is None or isinstance(atual, bool):
            total += 4 if atual in (None, True) else 5
        elif isinstance(atual, int) or (isinstance(atual, float) and math.isfinite(atual)):
            total += len(repr(atual))
        else:
            return None
        if total > teto:
            return None
    return total


# --- Pecas ------------------------------------------------------------------------------------------------------


def _texto_igual(valor: Any, esperado: str) -> bool:
    return isinstance(valor, str) and valor == esperado


def _contagem(valor: Any) -> int | None:
    if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
        return valor
    return None


def hora_do_evento(valor: Any) -> datetime | None:
    """A hora de um evento de ``depois``: data e hora RFC 3339 com fuso, ou ``None``. O ``t`` e o ``z`` minusculos
    valem como no RFC 3339 (secao 5.6, nota); o ``fromisoformat`` so le o ``Z`` maiusculo, e ele vai assim.

    O segundo 60 (o segundo bissexto, que o RFC 3339 aceita, secao 5.7) vale e e lido como o segundo seguinte, como o
    PostgreSQL le (``23:59:60Z`` e ``00:00:00Z`` do dia seguinte). Um sistema honesto que segue o relogio com o segundo
    bissexto nao tem a resposta dada como invalida (o que contaria contra ele na retirada); a hora guardada e a do
    MCP-CLARO, ja convertida. Do 61 em diante, ``hora_fora``."""
    if not isinstance(valor, str) or len(valor) > _TETO_DA_HORA or _DATA_HORA.fullmatch(valor) is None:
        return None
    bissexto = valor[17:19] == "60"
    texto = valor[:17] + "59" + valor[19:] if bissexto else valor
    try:
        hora = datetime.fromisoformat(texto[:-1] + "Z" if texto.endswith("z") else texto)
    except ValueError:
        return None
    if hora.utcoffset() is None:
        return None
    try:
        return hora + timedelta(seconds=1) if bissexto else hora
    except OverflowError:  # 9999-12-31T23:59:60: nao ha segundo seguinte
        return None


def _eventos(valor: Any, *, desde: datetime | None, ate: datetime | None) -> tuple[Evento, ...] | Invalida:
    if not isinstance(valor, list) or len(valor) > contrato.MAX_EVENTOS_DEPOIS:
        return Invalida("depois_fora")
    margem = timedelta(minutes=contrato.MARGEM_EVENTO_MINUTOS)
    eventos: list[Evento] = []
    for item in valor:
        if not isinstance(item, dict) or set(item) != {"evento", "em"}:
            return Invalida("depois_fora")
        evento = item["evento"]
        if not isinstance(evento, str) or evento not in contrato.EVENTOS_DEPOIS:
            return Invalida("evento_fora")
        hora = hora_do_evento(item["em"])
        if hora is None:
            return Invalida("hora_fora")
        if (desde is not None and hora < desde - margem) or (ate is not None and hora > ate + margem):
            return Invalida("hora_fora")
        eventos.append(Evento(evento, hora))
    return tuple(eventos)


def _estruturado(resultado: Any) -> dict[str, Any] | Invalida:
    """O ``structuredContent`` de uma ferramenta do contrato que leu (consulta ou aviso): sem erro e presente."""
    if not isinstance(resultado, types.CallToolResult):
        return Invalida("nao_e_resultado")
    if resultado.is_error:
        return Invalida("com_erro")
    dados = resultado.structured_content
    if dados is None:
        return Invalida("sem_estruturado")
    return dados if isinstance(dados, dict) else Invalida("forma")


# --- Consulta ---------------------------------------------------------------------------------------------------


def conferir_consulta(
    dados: Any, *, numero: str, nivel: int, desde: datetime | None = None, ate: datetime | None = None
) -> Consulta | Invalida:
    """O ``structuredContent`` de ``selo_consultar(numero)`` pelo esquema do contrato (``contrato.ESQUEMAS``):
    ``contrato`` ``selo/1``, ``operacao`` igual a ``numero``, ``estado`` do vocabulario e, no nivel 3 (exigido) ou
    quando vier, ``depois`` com ate 20 eventos ``{evento, em}`` do vocabulario. ``desde`` e ``ate`` (a hora do envio
    e a de agora, do banco): a janela da hora dos eventos, com a margem do contrato."""
    if tamanho_json(dados) is None:
        return Invalida("grande_demais")
    if not isinstance(dados, dict):
        return Invalida("forma")
    if not _texto_igual(dados.get("contrato"), contrato.CONTRATO):
        return Invalida("contrato")
    if not _texto_igual(dados.get("operacao"), numero):
        return Invalida("outro_numero")
    estado = dados.get("estado")
    if not isinstance(estado, str) or estado not in contrato.ESTADOS:
        return Invalida("estado_fora")
    if "depois" not in dados:
        return Invalida("depois_fora") if nivel >= 3 else Consulta(estado)
    eventos = _eventos(dados["depois"], desde=desde, ate=ate)
    if isinstance(eventos, Invalida):
        return eventos
    return Consulta(estado, eventos)


def ler_consulta(
    resultado: Any, *, numero: str, nivel: int, desde: datetime | None = None, ate: datetime | None = None
) -> Consulta | Invalida:
    """O resultado de ``selo_consultar(numero)`` (um ``CallToolResult``): sem erro, com ``structuredContent`` valido
    (``conferir_consulta``). Um ``InputRequiredResult`` ou qualquer outra coisa e invalido."""
    dados = _estruturado(resultado)
    if isinstance(dados, Invalida):
        return dados
    return conferir_consulta(dados, numero=numero, nivel=nivel, desde=desde, ate=ate)


# --- Aviso de bloqueio ------------------------------------------------------------------------------------------


def conferir_bloqueio(dados: Any, *, bloqueio: str, numeros: Sequence[str]) -> RespostaAoAviso | Invalida:
    """O ``structuredContent`` de ``selo_bloqueio(bloqueio, numeros)``: ``contrato`` ``selo/1``, ``bloqueio`` igual ao
    perguntado e ``operacoes`` com cada numero pedido uma vez so (nenhum faltando, nenhum a mais), cada item so
    ``{operacao, estado}`` com o estado do vocabulario."""
    if tamanho_json(dados) is None:
        return Invalida("grande_demais")
    if not isinstance(dados, dict):
        return Invalida("forma")
    if not _texto_igual(dados.get("contrato"), contrato.CONTRATO):
        return Invalida("contrato")
    if not _texto_igual(dados.get("bloqueio"), bloqueio):
        return Invalida("outro_aviso")
    itens = dados.get("operacoes")
    if not isinstance(itens, list) or len(itens) > contrato.MAX_OPERACOES_POR_AVISO:
        return Invalida("forma")
    pedidos = set(numeros)
    vistos: dict[str, str] = {}
    for item in itens:
        if not isinstance(item, dict) or set(item) != {"operacao", "estado"}:
            return Invalida("forma")
        numero, estado = item["operacao"], item["estado"]
        if not isinstance(numero, str) or numero not in pedidos:
            return Invalida("numero_a_mais")
        if numero in vistos:
            return Invalida("numero_repetido")
        if not isinstance(estado, str) or estado not in contrato.ESTADOS:
            return Invalida("estado_fora")
        vistos[numero] = estado
    if set(vistos) != pedidos:
        return Invalida("numero_faltando")
    return RespostaAoAviso(tuple((n, vistos[n]) for n in dict.fromkeys(numeros)))


def ler_bloqueio(resultado: Any, *, bloqueio: str, numeros: Sequence[str]) -> RespostaAoAviso | Invalida:
    """O resultado de ``selo_bloqueio`` (um ``CallToolResult``): sem erro, com ``structuredContent`` valido
    (``conferir_bloqueio``)."""
    dados = _estruturado(resultado)
    if isinstance(dados, Invalida):
        return dados
    return conferir_bloqueio(dados, bloqueio=bloqueio, numeros=numeros)


# --- Recibo do nivel 3 ------------------------------------------------------------------------------------------


def conferir_recibo(dados: Any, *, numero: str, com_erro: bool) -> Recibo | Invalida:
    """O recibo (``_meta[contrato.CHAVE_RECIBO]``) de uma acao que levou ``numero``: ``contrato`` ``selo/1``,
    ``operacao`` igual a ``numero``, ``estado`` de ``contrato.ESTADOS_DO_RECIBO`` que combina com ``com_erro`` (o
    ``isError`` da chamada), ``repetida`` booleano (falso se nao vier), ``objetos`` uma lista de ``{"id": texto}`` (so a
    contagem fica) e ``itens`` ``{aplicados, nao_aplicados}`` com contagens."""
    if tamanho_json(dados) is None:
        return Invalida("grande_demais")
    if not isinstance(dados, dict):
        return Invalida("forma")
    if not _texto_igual(dados.get("contrato"), contrato.CONTRATO):
        return Invalida("contrato")
    if not _texto_igual(dados.get("operacao"), numero):
        return Invalida("outro_numero")
    estado = dados.get("estado")
    if not isinstance(estado, str) or estado not in contrato.ESTADOS_DO_RECIBO:
        return Invalida("estado_fora")
    if (estado in ESTADOS_DE_SUCESSO) == com_erro:
        return Invalida("incoerente")
    repetida = dados.get("repetida", False)
    if not isinstance(repetida, bool):
        return Invalida("repetida")
    objetos: int | None = None
    if "objetos" in dados:
        lista = dados["objetos"]
        if not isinstance(lista, list) or len(lista) > MAX_OBJETOS:
            return Invalida("objetos")
        if not all(isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"] for o in lista):
            return Invalida("objetos")
        objetos = len(lista)
    itens: tuple[int, int] | None = None
    if "itens" in dados:
        bruto = dados["itens"]
        aplicados = _contagem(bruto.get("aplicados")) if isinstance(bruto, dict) else None
        nao_aplicados = _contagem(bruto.get("nao_aplicados")) if isinstance(bruto, dict) else None
        if aplicados is None or nao_aplicados is None:
            return Invalida("itens")
        itens = (aplicados, nao_aplicados)
    return Recibo(estado, repetida, objetos, itens)


def recibo_do_meta(meta: Any) -> Any:
    """O recibo cru no ``_meta`` de um resultado (``None`` sem ele). Lido antes de o ``_meta`` do sistema sair."""
    return meta.get(contrato.CHAVE_RECIBO) if isinstance(meta, Mapping) else None


def ler_recibo(resultado: Any, *, numero: str) -> Recibo | Invalida:
    """O recibo do resultado de uma acao com ``numero`` (um ``CallToolResult``). Sem recibo, ``recibo_ausente``."""
    if not isinstance(resultado, types.CallToolResult):
        return Invalida("nao_e_resultado")
    recibo = recibo_do_meta(resultado.meta)
    if recibo is None:
        return Invalida("recibo_ausente")
    return conferir_recibo(recibo, numero=numero, com_erro=bool(resultado.is_error))
