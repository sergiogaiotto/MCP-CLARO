"""Exemplo 8: sistema que adotou o contrato do selo (``selo/1``) no nivel 2, honesto, para a bateria e as provas da
Fase 4. Nada sai deste processo: as "escritas" ficam em listas na memoria.

Este modulo tem o motor dos tres dubles do selo. ``criar()`` monta o de nivel 2; ``selo_nivel3.criar()`` monta o de
nivel 3 (o mesmo motor, com o recibo no ``_meta`` do resultado, o ``depois`` na consulta e ``selo_bloqueio``); e
``selo_mentiroso.criar(modo=...)`` monta um que erra de proposito, um defeito por modo.

O que o duble honesto faz (desenho 2.2 e 3.3; criticas A3, M2, M4, M6, B4, B8 e B9):

- guarda cada numero da operacao (``params._meta[contrato.CHAVE_OPERACAO]``) ASSIM QUE a chamada chega, antes de
  escrever, por pessoa (a identidade da chave, nao o texto dela: uma chave renovada da mesma pessoa ve os mesmos
  numeros); a mesma chamada com o mesmo numero nao aplica de novo e responde o desfecho da primeira (se a primeira
  ainda roda, espera por ela): o que de fato houve com ela, com um texto que combina com o ``isError`` mesmo quando a
  primeira resposta nao sabia (``sem_saber``) ou o estado mudou depois (efeito adiado, aviso); o mesmo numero com
  outros argumentos e recusado com erro JSON-RPC, sem aplicar;
- ``selo_consultar`` diz o estado verdadeiro; de numero que nunca chegou, ``nao_recebida``, e o numero fica queimado:
  a chamada que chegar depois com ele e recusada sem aplicar (recibo ``nao_recebida``);
- ``selo_bloqueio`` (nivel 3) para o que ainda nao terminou (``parada``), queima os numeros que nao chegaram e da as
  mesmas respostas ao mesmo aviso (guardadas: mesmo que o estado de um numero mude depois); a chamada que chegar
  depois com um desses numeros e recusada (recibo ``parada``);
- ``selo_alvo`` diz ``teste`` no alvo de teste e ``producao`` na producao; no modo de ferramentas de teste, cada
  ferramenta de teste (``<acao>_teste``) escreve so no espaco de teste e devolve ``_meta[contrato.CHAVE_ALVO] =
  "teste"``;
- o que ele recusa fica sem efeito: o numero com outros argumentos ou queimado, os argumentos ruins, o item ruim do
  lote e a escrita com a versao velha (``recusa_sem_aplicar``);
- a escrita termina mesmo se quem chamou cortar a conexao (a resposta perdida da critica A3 nao vira
  ``nao_recebida``).

Ferramentas de acao (todas aceitam o numero; ``ACOES``):

- ``enviar_lembrete``: cria um lembrete (objeto ``lembrete-N``). Recusa endereco sem ``@``. Argumentos que forcam os
  casos: ``daqui_a`` (efeito adiado, C-3.6b: responde ``em_andamento`` e so aplica ``daqui_a`` segundos depois, se o
  aviso de bloqueio nao parar antes), ``demora`` (escrita lenta, C-2.6b: segura a resposta ``demora`` segundos, ou
  ate ``POST /soltar``, e so entao aplica), ``pedir_confirmacao`` (C-2.3: responde ``input_required`` sem aplicar nada
  e sem guardar o numero) e ``sem_saber`` (critica B9: aplica, mas responde erro com recibo ``desconhecido``; a
  consulta depois diz ``concluida``);
- ``enviar_lote``: varios lembretes numa chamada (C-3.3). Com um item ruim, aplica os bons e responde erro com recibo
  ``parcial`` e as contagens em ``itens``;
- ``marcar_lido``: idempotente de verdade (``idempotentHint: true``, critica M6): com numero novo e os mesmos
  argumentos, conta uma aplicacao a mais e nao muda o conteudo;
- ``trocar_perfil``: muda ``nome`` ou ``telefone`` do perfil, com ``versao`` opcional; versao velha e conflito
  (C-3.4): erro, recibo ``recusada``, nada aplicado.

Leituras do rastro (so leem; ``espaco`` ``teste`` le o espaco das ferramentas de teste): ``listar_lembretes``
(``/itens`` e ``/aplicacoes``), ``listar_lidos`` (``/lidos`` e ``/aplicacoes``) e ``ler_perfil`` (``/versao``,
``/nome``, ``/telefone`` e ``/mudancas``). ``declaracao()`` monta a declaracao de teste deste duble (desenho 2.5, no
formato de ``mcp_claro.selo.declaracao``).

Enderecos: ``producao`` e ``teste`` sao os caminhos do MCP na producao e no alvo de teste, cada um com os seus dados,
numeros e contagens (ambientes separados, como num sistema de verdade). Nos testes, cada caminho sai de
``tests.apoio_selos.caminho_unico`` e o endereco de ``endereco_de_duble``. ``teste=None`` serve so a producao (modo de
ferramentas de teste). ``esquemas="kit"`` gera os esquemas das ferramentas do contrato pelo caminho comum do kit
(funcao com tipos; critica B4), com ``title`` e ``$defs``.

Rotas de controle (fora do MCP, so de teste):

- ``GET /contagem``: por alvo (``producao``, ``teste``), as chamadas recebidas por ferramenta (``chamadas``), as
  escritas aplicadas (``aplicadas``), as chamadas por pessoa da chave (``pessoas``: ``bateria`` e a chave de teste,
  ``declaracoes`` a de leitura das declaracoes) e os efeitos adiados que ainda nao terminaram (``adiados``: o que o
  aviso parou termina na hora, sem esperar o relogio; o que ele nao parou, e o que o mentiroso ``escreve_escondido``
  poe mesmo parado, termina no fim dos ``daqui_a`` segundos); ``POST /contagem/zerar`` zera as contagens (nao os
  dados);
- ``POST /soltar``: ``{"operacao": K}`` termina agora a espera da escrita lenta com o numero K (a escrita acontece
  do mesmo jeito, so mais cedo; 404 se nao ha uma em curso). So a bateria de referencia usa, para nao esperar o
  relogio depois de consultar durante a escrita (C-2.6b);
- ``GET /numeros``: por alvo, cada numero guardado, a ferramenta e o estado; ``GET /metas``: o ``_meta`` de cada
  chamada de acao recebida (as ultimas 200);
- ``POST /ajustes``: ``{"segurar_segundos": s}`` segura cada chamada de acao ``s`` segundos antes de ela "chegar"
  (o numero ainda nao foi recebido: e o aviso que chega antes da chamada, critica M2); ``{"listar_contrato": false}``
  tira as ferramentas do contrato do ``tools/list`` (critica M1); ``{"tirar": ["selo_consultar"]}`` tira do
  ``tools/list`` so as ferramentas com esses nomes (``[]`` volta a listar todas);
- ``POST /operacao``: ``{"operacao": K, "acao": "desfazer" | "desconhecer", "alvo": "producao"}``: o sistema desfez o
  que o numero fez (estado ``desfeita``, evento ``desfeita``) ou perdeu o registro e nao sabe mais (``desconhecido``;
  critica M4).

O crachá de cada pessoa e uma chave estatica so de teste (``exemplos.comum``), mais ``CHAVES_DO_SELO``:
``chave-ana-renovada`` (a mesma pessoa ``ana``), ``chave-teste-selo`` (a pessoa ``bateria``),
``chave-teste-selo-renovada`` (a mesma pessoa ``bateria``) e ``chave-declaracoes-selo`` (a leitura das declaracoes).
``criar(mapa_de_chaves=...)`` troca o mapa inteiro, nos tres dubles (``selo_nivel3.criar`` e
``selo_mentiroso.criar`` tambem).
"""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

import anyio
import mcp_types as types
from anyio.abc import TaskGroup
from mcp.server import Server, ServerRequestContext
from mcp.server.mcpserver.tools.base import Tool as ToolDoKit
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from exemplos.comum import Contagem, FiltroChave, chaves, rodar
from mcp_claro.selo import contrato

CAMINHO_PRODUCAO: Final = "/mcp"
CAMINHO_TESTE: Final = "/teste/mcp"
CHAVES_DO_SELO: Final = {
    "chave-ana-renovada": "ana",
    "chave-teste-selo": "bateria",
    "chave-teste-selo-renovada": "bateria",
    "chave-declaracoes-selo": "declaracoes",
}
"""Chaves estaticas so de teste, alem das de ``exemplos.comum``: a renovada de ``ana`` (a mesma pessoa), a da bateria
(``teste_selo``, no alvo de teste), a renovada da bateria (a mesma pessoa: so a prova do comportamento contra o
duble usa; pela decisao 1 do coordenador na etapa 2r, a bateria nao tem segunda chave da mesma identidade, critica
A3.4) e a de leitura das declaracoes (na producao, so ``tools/list`` e a consulta de controle, critica A4)."""
SUFIXO_TESTE: Final = "_teste"
ESPACO_REAL: Final = "real"
ESPACO_TESTE: Final = "teste"
GUARDA_DIAS: Final = 30

ESTADOS_DE_SUCESSO: Final = frozenset({"concluida", "em_andamento"})
"""Recibo com um destes estados vem com ``isError: false``; com os outros, ``isError: true`` (SELO-3.1)."""

TEXTOS_DA_REPETICAO: Final[Mapping[str, str]] = {
    "concluida": "Este pedido ja foi feito{objetos}; nada foi feito de novo.",
    "em_andamento": "Este pedido ja foi recebido e ainda esta em andamento; nada foi feito de novo.",
    "parcial": "Este pedido ja foi feito so em parte{objetos}; nada foi feito de novo.",
    "recusada": "Este pedido ja foi recusado; nada foi feito.",
    "parada": "Este pedido foi parado pelo aviso de bloqueio antes de terminar; nada foi feito de novo.",
    "desconhecido": (
        "O sistema nao sabe se este pedido foi feito; nada foi feito de novo. Consulte o numero antes de pedir de novo."
    ),
}
"""O texto da repeticao quando o estado do numero mudou desde a primeira resposta (a primeira nao sabia, o efeito
adiado terminou, o aviso parou ou o sistema perdeu o registro): o texto da primeira ja nao combina com o desfecho."""


def texto_da_repeticao(estado: str, objetos: Sequence[str]) -> str:
    """O texto da repeticao no ``estado`` de agora, com os objetos criados (``TEXTOS_DA_REPETICAO``)."""
    ditos = f" ({', '.join(objetos)})" if objetos else ""
    return TEXTOS_DA_REPETICAO.get(estado, "Pedido ja recebido; nada foi feito de novo.").format(objetos=ditos)


ACOES: Final[tuple[str, ...]] = ("enviar_lembrete", "enviar_lote", "marcar_lido", "trocar_perfil")
LEITURAS: Final[tuple[str, ...]] = ("listar_lembretes", "listar_lidos", "ler_perfil")

FALHA_NO_NIVEL_3: Final[tuple[str, ...]] = (
    "C-0.1",
    "C-0.3",
    "C-2.2a",
    "C-2.4a",
    "C-2.4b",
    "C-2.5",
    "C-2.6a",
    "C-2.6b",
    "C-3.1b",
    "C-3.2",
    "C-3.3",
    "C-3.5",
    "C-3.6a",
    "C-3.6b",
)
"""Os casos que o duble de NIVEL 2 falha num teste de nivel 3, com a declaracao dele (``declaracao()``): ele nao lista
``selo_bloqueio`` (C-0.1), nao poe recibo (C-3.1b, C-3.2, C-3.3; e o C-2.2a, que num teste de nivel 3 tambem pede o
recibo da repeticao com ``repetida`` e os mesmos objetos, desenho 3.3) e nao atende o aviso (C-3.6a, C-3.6b). E a
consulta dele nao traz ``depois``, que o nivel 3 exige, mesmo vazio (desenho 2.2, como ``selo/respostas.py``): num
teste de nivel 3, toda consulta dele e invalida, e falham tambem os casos que dependem da consulta (C-0.3, C-2.2a,
C-2.4a, C-2.4b, C-2.5, C-2.6a, C-2.6b e C-3.5). Os que nao dependem do recibo, do aviso nem da consulta (C-0.2, C-0.4,
C-2.1, C-2.2b, C-2.2c, C-2.2d, C-2.3, C-2.7, C-3.1a e C-3.4) ele cumpre. Num teste de nivel 2 ele cumpre todos
(referencia do G3)."""

_ENDERECO: Final = {"type": "string", "maxLength": 120}
_TEXTO: Final = {"type": "string", "maxLength": 500}
_ESPACO: Final = {"type": "string", "enum": [ESPACO_REAL, ESPACO_TESTE]}
_DICA_DO_NUMERO: Final = (
    " Uma repeticao deste pedido deve levar o mesmo numero da operacao (contrato do selo); o sistema guarda os"
    f" numeros por {GUARDA_DIAS} dias."
)

DEFINICOES_DE_ACAO: Final[Mapping[str, dict[str, Any]]] = {
    "enviar_lembrete": {
        "name": "enviar_lembrete",
        "title": "Enviar lembrete",
        "description": "Envia um lembrete a um endereco da caixa de teste." + _DICA_DO_NUMERO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "para": _ENDERECO,
                "texto": _TEXTO,
                "daqui_a": {"type": "integer", "minimum": 1, "maximum": 600},
                "demora": {"type": "number", "minimum": 0, "maximum": 60},
                "pedir_confirmacao": {"type": "boolean"},
                "sem_saber": {"type": "boolean"},
            },
            "required": ["para", "texto"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    "enviar_lote": {
        "name": "enviar_lote",
        "title": "Enviar lote de lembretes",
        "description": "Envia varios lembretes numa chamada; diz quantos foram e quantos nao." + _DICA_DO_NUMERO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {"para": _ENDERECO, "texto": _TEXTO},
                        "required": ["para", "texto"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["itens"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    "marcar_lido": {
        "name": "marcar_lido",
        "title": "Marcar como lido",
        "description": "Marca um lembrete como lido. Marcar de novo nao muda nada." + _DICA_DO_NUMERO,
        "inputSchema": {
            "type": "object",
            "properties": {"lembrete": {"type": "string", "maxLength": 40}},
            "required": ["lembrete"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    "trocar_perfil": {
        "name": "trocar_perfil",
        "title": "Trocar o perfil",
        "description": "Muda o nome ou o telefone do perfil; com a versao lida, recusa se o perfil mudou."
        + _DICA_DO_NUMERO,
        "inputSchema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string", "maxLength": 80},
                "telefone": {"type": "string", "maxLength": 20},
                "versao": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
}

DEFINICOES_DE_LEITURA: Final[Mapping[str, dict[str, Any]]] = {
    "listar_lembretes": {
        "name": "listar_lembretes",
        "description": "Lista os lembretes enviados e quantas escritas de lembrete foram aplicadas.",
        "inputSchema": {"type": "object", "properties": {"espaco": _ESPACO}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    "listar_lidos": {
        "name": "listar_lidos",
        "description": "Lista os lembretes marcados como lidos e quantas marcacoes foram aplicadas.",
        "inputSchema": {"type": "object", "properties": {"espaco": _ESPACO}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    "ler_perfil": {
        "name": "ler_perfil",
        "description": "Le o perfil, a versao e quantas mudancas foram aplicadas.",
        "inputSchema": {"type": "object", "properties": {"espaco": _ESPACO}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
}


def agora() -> str:
    """A hora de um evento de ``depois``, em ISO 8601 com fuso."""
    return datetime.now(UTC).isoformat()


# --- Esquemas das ferramentas do contrato pelo caminho comum do kit (critica B4) -----------------------------

_Numero = Annotated[str, Field(pattern=contrato.PADRAO_UUID)]
_EstadoDoContrato = Literal[
    "concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada", "desconhecido"
]


class _Evento(BaseModel):
    evento: Literal["concluida", "entregue", "devolvida", "desfeita", "parada"]
    em: datetime


class _RespostaAlvo(BaseModel):
    contrato: Literal["selo/1"]
    alvo: Literal["teste", "producao"]


class _RespostaConsulta(BaseModel):
    contrato: Literal["selo/1"]
    operacao: str
    estado: _EstadoDoContrato
    depois: Annotated[list[_Evento], Field(max_length=contrato.MAX_EVENTOS_DEPOIS)] = []


class _EstadoDaOperacao(BaseModel):
    operacao: str
    estado: _EstadoDoContrato


class _RespostaBloqueio(BaseModel):
    contrato: Literal["selo/1"]
    bloqueio: str
    operacoes: Annotated[list[_EstadoDaOperacao], Field(max_length=contrato.MAX_OPERACOES_POR_AVISO)]


async def selo_alvo() -> _RespostaAlvo:
    """Diz se este endereco e o alvo de teste ou a producao."""
    raise NotImplementedError  # so o esquema importa: quem responde e o motor


async def selo_consultar(operacao: _Numero) -> _RespostaConsulta:
    """Diz o que houve com o numero da operacao. Guarda os numeros por 30 dias."""
    raise NotImplementedError


async def selo_bloqueio(
    bloqueio: _Numero,
    operacoes: Annotated[list[_Numero], Field(min_length=1, max_length=contrato.MAX_OPERACOES_POR_AVISO)],
) -> _RespostaBloqueio:
    """Aviso de bloqueio: para o que ainda nao terminou."""
    raise NotImplementedError


_FUNCOES_DO_KIT: Final[Mapping[str, Callable[..., Awaitable[Any]]]] = {
    contrato.FERRAMENTA_ALVO: selo_alvo,
    contrato.FERRAMENTA_CONSULTAR: selo_consultar,
    contrato.FERRAMENTA_BLOQUEIO: selo_bloqueio,
}


def definicao_pelo_kit(nome: str) -> dict[str, Any]:
    """A definicao de uma ferramenta do contrato como o kit a gera a partir da funcao com tipos (``title`` em cada
    campo, ``$defs`` nos objetos de dentro, sem ``additionalProperties``)."""
    ferramenta = ToolDoKit.from_function(_FUNCOES_DO_KIT[nome])
    return {
        "name": nome,
        "description": ferramenta.description,
        "inputSchema": ferramenta.parameters,
        "outputSchema": ferramenta.output_schema,
    }


def definicao_publicada(nome: str) -> dict[str, Any]:
    """A definicao de uma ferramenta do contrato copiada do contrato publicado (``contrato.ESQUEMAS``)."""
    esquemas = contrato.ESQUEMAS[nome]
    return {
        "name": nome,
        "description": f"Ferramenta do contrato do selo. O sistema guarda os numeros por {GUARDA_DIAS} dias.",
        "inputSchema": copy.deepcopy(esquemas["inputSchema"]),
        "outputSchema": copy.deepcopy(esquemas["outputSchema"]),
    }


# --- Dados e numeros -----------------------------------------------------------------------------------------


@dataclass
class Dados:
    """Um espaco de dados do sistema (o real ou o de teste)."""

    lembretes: list[str] = field(default_factory=list)
    aplicacoes_lembretes: int = 0
    lidos: set[str] = field(default_factory=set)
    aplicacoes_lidos: int = 0
    perfil: dict[str, str] = field(default_factory=lambda: {"nome": "", "telefone": ""})
    versao: int = 0
    mudancas: int = 0
    criados: int = 0
    """Quantos lembretes este espaco ja criou (o numero do proximo identificador). Um inteiro, e nao um contador do
    ``itertools``, para o espaco poder ser copiado (``copy.deepcopy``; o mentiroso ``sucesso_sem_efeito`` escreve numa
    copia)."""

    def criar_lembrete(self) -> str:
        self.criados += 1
        ident = f"lembrete-{self.criados}"
        self.lembretes.append(ident)
        self.aplicacoes_lembretes += 1
        return ident


@dataclass
class Registro:
    """O que o sistema guarda de um numero da operacao."""

    numero: str
    assinatura: str | None
    estado: str
    queimado_por: str | None = None
    """``consulta`` ou ``aviso``: o numero nunca chegou e ficou queimado por uma dessas perguntas."""
    ferramenta: str | None = None
    espaco: str = ESPACO_REAL
    objetos: list[str] = field(default_factory=list)
    itens: dict[str, int] | None = None
    depois: list[dict[str, str]] = field(default_factory=list)
    pronto: anyio.Event = field(default_factory=anyio.Event)
    parou: anyio.Event = field(default_factory=anyio.Event)
    """Acende quando o aviso de bloqueio para a operacao: o efeito adiado acorda na hora, sem esperar o relogio (menos
    no mentiroso ``escreve_escondido``, que nao olha o aviso: o efeito sai no horario marcado)."""
    texto: str = ""
    erro: bool = False
    estado_da_resposta: str = ""
    """O desfecho da primeira chamada (texto, ``isError`` e o estado que ela disse), para a repeticao responder igual
    enquanto o estado nao muda."""
    disse: list[str] = field(default_factory=list)
    """O que o sistema ja disse deste numero (recibo e consultas): so o mentiroso ``contradiz`` usa."""

    @classmethod
    def queimado(cls, numero: str, por: str) -> Registro:
        reg = cls(numero, None, "nao_recebida", queimado_por=por)
        reg.pronto.set()
        return reg


@dataclass
class Ambiente:
    """A producao ou o alvo de teste: dados, numeros e contagens proprios."""

    alvo: Literal["producao", "teste"]
    caminho: str
    contagem: Contagem = field(default_factory=Contagem)
    aplicadas: dict[str, int] = field(default_factory=dict)
    espacos: dict[str, Dados] = field(default_factory=lambda: {ESPACO_REAL: Dados(), ESPACO_TESTE: Dados()})
    registros: dict[tuple[str, str], Registro] = field(default_factory=dict)
    bloqueios: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    metas: list[dict[str, Any]] = field(default_factory=list)
    pessoas: dict[str, int] = field(default_factory=dict)

    def aplicar(self, ferramenta: str) -> None:
        self.aplicadas[ferramenta] = self.aplicadas.get(ferramenta, 0) + 1

    def achar(self, numero: str) -> Registro | None:
        """O registro do numero, de qualquer pessoa (so para as rotas de controle)."""
        return next((r for (_, n), r in self.registros.items() if n == numero), None)


@dataclass(frozen=True)
class Saida:
    """O desfecho de uma escrita: texto, ``isError``, estado do recibo, objetos criados e contagens de itens."""

    texto: str
    erro: bool
    estado: str
    objetos: tuple[str, ...] = ()
    itens: dict[str, int] | None = None


class PedeDados(Exception):
    pass


def _estruturado(dados: Mapping[str, Any], *, erro: bool = False) -> types.CallToolResult:
    texto = json.dumps(dados, ensure_ascii=False)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=texto)], structured_content=dict(dados), is_error=erro
    )


def _erro_da_ferramenta(texto: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=texto)], is_error=True)


async def _sempre_aberto() -> None:
    return None


# --- O motor -------------------------------------------------------------------------------------------------


class Motor:
    """O sistema honesto do contrato, nos niveis 2 e 3. ``selo_mentiroso.Mentiroso`` muda os ganchos marcados."""

    def __init__(
        self,
        *,
        nivel: int = 2,
        producao: str = CAMINHO_PRODUCAO,
        teste: str | None = CAMINHO_TESTE,
        ferramentas_de_teste: bool = False,
        esquemas: Literal["contrato", "kit"] = "contrato",
        acoes: Sequence[str] = ACOES,
        mapa_de_chaves: Mapping[str, str] | None = None,
    ) -> None:
        if nivel not in contrato.NIVEIS:
            raise ValueError("o duble do selo e de nivel 2 ou 3")
        if teste is not None and teste == producao:
            raise ValueError("o alvo de teste tem outro caminho")
        desconhecidas = set(acoes) - set(DEFINICOES_DE_ACAO)
        if desconhecidas:
            raise ValueError(f"acoes desconhecidas: {sorted(desconhecidas)}")
        self.nivel = nivel
        self.ferramentas_de_teste = ferramentas_de_teste
        self.esquemas = esquemas
        self.acoes = tuple(acoes)
        self.mapa_de_chaves = dict(mapa_de_chaves) if mapa_de_chaves is not None else chaves() | CHAVES_DO_SELO
        self.ambientes: dict[str, Ambiente] = {"producao": Ambiente("producao", producao)}
        if teste is not None:
            self.ambientes["teste"] = Ambiente("teste", teste)
        self.segurar_segundos = 0.0
        self.listar_contrato = True
        self.tirar: frozenset[str] = frozenset()
        self.tarefas: TaskGroup | None = None
        self.adiados: dict[str, int] = {}
        """Por alvo, quantos efeitos adiados ainda nao terminaram (``/contagem``)."""
        self.lentas: dict[str, anyio.Event] = {}
        """Por numero, as escritas lentas em curso; ``POST /soltar`` termina uma antes da hora."""

    # --- ganchos (o mentiroso muda) ---------------------------------------------------------------------------

    def deduplica(self) -> bool:
        return True

    def queima(self) -> bool:
        return True

    def obedece_bloqueio(self) -> bool:
        return True

    def registra_ao_chegar(self) -> bool:
        return True

    def recusa_outros_argumentos(self) -> bool:
        """O mesmo numero com outros argumentos e recusado sem aplicar (SELO-2.2)."""
        return True

    def responde_outros_como_repeticao(self) -> bool:
        """O mesmo numero com outros argumentos e respondido como repeticao da primeira (o mentiroso
        ``outros_argumentos_como_repeticao``; o honesto recusa, SELO-2.2)."""
        return False

    def recusa_o_queimado(self) -> bool:
        """A chamada com um numero queimado (pela consulta ou pelo aviso) volta como erro (SELO-2.5 e 3.6). O
        mentiroso ``queimado_como_sucesso`` responde sucesso, sem aplicar."""
        return True

    def repeticao_aplica(self) -> bool:
        """A repeticao aplica de novo, com a resposta do honesto (o mentiroso ``escreve_escondido``)."""
        return False

    def saida_da_repeticao(self, saida: Saida) -> Saida:
        """O desfecho que a repeticao conta (o honesto conta o da primeira, SELO-2.2)."""
        return saida

    def responde_pedido_de_dados(self) -> bool:
        """Com ``pedir_confirmacao``, a resposta e ``input_required`` (SELO-2.3). O mentiroso ``condicoes_falsas``
        responde erro, sem pedir dados."""
        return True

    def nome_muda_o_telefone(self) -> bool:
        """``trocar_perfil`` com o nome reescreve tambem o telefone (o mentiroso ``condicoes_falsas``: os dois campos
        do conflito, C-3.4, nao sao independentes). O honesto guarda os dois separados."""
        return False

    def para_o_efeito_adiado(self) -> bool:
        """O efeito adiado parado pelo aviso nao acontece, e a espera dele termina na hora (SELO-3.6). O mentiroso
        ``escreve_escondido`` o aplica do mesmo jeito, no fim dos segundos marcados, como se o aviso nao tivesse
        chegado (o registro continua dizendo ``parada``)."""
        return True

    def estado_do_parado_no_aviso(self, reg: Registro) -> str:
        """O que o aviso diz de um numero que ele acabou de parar (SELO-3.6)."""
        return "parada"

    def estado_do_novo_no_aviso(self) -> str:
        """O que o aviso diz de um numero que nunca chegou (e que ele queima)."""
        return "nao_recebida"

    def consulta_de_numero_novo(self, amb: Ambiente) -> None:
        """O que a consulta de um numero que nunca chegou faz alem de queima-lo: nada (SELO-2.4). O mentiroso
        ``consulta_escreve`` escreve, so no alvo de teste."""
        return None

    def escondidas(self) -> frozenset[str]:
        """Ferramentas que o sistema atende, mas nao lista em ``tools/list`` (o mentiroso ``lista_incompleta``)."""
        return frozenset()

    def ajustar_ferramenta_de_teste(self, definicao: dict[str, Any]) -> dict[str, Any]:
        """A definicao de uma ferramenta de teste: a da acao real, com outro nome (SELO-0.1)."""
        return definicao

    def recusa_sem_aplicar(self) -> bool:
        """O que o sistema recusa fica sem efeito: o numero com outros argumentos (SELO-2.2), o numero queimado pela
        consulta ou pelo aviso (SELO-2.5 e 3.6), os argumentos ruins (SELO-3.1), o item ruim do lote (SELO-3.3) e a
        escrita com a versao velha (SELO-3.4). O mentiroso ``recusa_mas_aplica`` aplica e responde a mesma recusa."""
        return True

    def pede_dados_sem_aplicar(self) -> bool:
        """A resposta ``input_required`` nao pos nada em efeito (SELO-2.3)."""
        return True

    def marcar_de_novo_desmarca(self) -> bool:
        """``marcar_lido`` de um lembrete ja lido o desmarca (o honesto nao: marcar de novo nao muda nada, SELO-2.7)."""
        return False

    def onde_escrever(self, amb: Ambiente) -> Ambiente:
        """Onde a escrita acontece: o honesto escreve nos dados do proprio ambiente."""
        return amb

    def ajustar_saida(self, saida: Saida, reg: Registro) -> Saida:
        """O desfecho que o sistema conta depois de escrever (o honesto conta o que houve)."""
        return saida

    def marca_de_teste(self) -> bool:
        return True

    def guarda_o_aviso(self) -> bool:
        """O mesmo aviso (a mesma pessoa e o mesmo ``bloqueio``) repetido da as respostas guardadas (SELO-3.6)."""
        return True

    def estado_do_queimado_no_aviso(self, reg: Registro) -> str:
        """O que o aviso diz de um numero que nunca chegou e ja estava queimado (por consulta ou por outro aviso)."""
        return "nao_recebida"

    def chave_do_registro(self, ctx: ServerRequestContext[Any], numero: str) -> tuple[str, str]:
        """Por pessoa (a identidade da chave), nunca pelo texto da chave (recomendacao do contrato)."""
        return (str(ctx.request.state.pessoa), numero)

    def alvo_declarado(self, amb: Ambiente) -> str:
        return amb.alvo

    def definicoes_de_acao(self, amb: Ambiente) -> list[dict[str, Any]]:
        return [copy.deepcopy(DEFINICOES_DE_ACAO[a]) for a in self.acoes]

    def definicoes_do_contrato(self) -> list[dict[str, Any]]:
        fazer = definicao_pelo_kit if self.esquemas == "kit" else definicao_publicada
        return [fazer(n) for n in contrato.ferramentas_do_nivel(self.nivel)]

    def estado_na_consulta(self, reg: Registro) -> str:
        return reg.estado

    def resposta_da_consulta(self, dados: dict[str, Any]) -> dict[str, Any]:
        return dados

    def recibo_final(self, recibo: dict[str, Any], erro: bool) -> tuple[dict[str, Any], bool]:
        return recibo, erro

    async def antes_da_consulta(self, ctx: ServerRequestContext[Any]) -> None:
        return None

    async def antes_do_aviso(self, ctx: ServerRequestContext[Any]) -> None:
        return None

    async def depois_de_aplicar(
        self, amb: Ambiente, ctx: ServerRequestContext[Any], chave: tuple[str, str], reg: Registro
    ) -> None:
        return None

    # --- listagem ---------------------------------------------------------------------------------------------

    def ferramentas(self, amb: Ambiente) -> list[dict[str, Any]]:
        lista = self.definicoes_de_acao(amb)
        if self.ferramentas_de_teste and amb.alvo == "producao":
            for definicao in self.definicoes_de_acao(amb):
                de_teste = copy.deepcopy(definicao)
                de_teste["name"] = definicao["name"] + SUFIXO_TESTE
                de_teste["description"] = "Ferramenta de teste: escreve so no espaco de teste. " + str(
                    definicao.get("description", "")
                )
                lista.append(self.ajustar_ferramenta_de_teste(de_teste))
        lista.extend(copy.deepcopy(d) for d in DEFINICOES_DE_LEITURA.values())
        if self.listar_contrato:
            lista.extend(self.definicoes_do_contrato())
        fora = self.tirar | self.escondidas()
        return [f for f in lista if f["name"] not in fora]

    # --- chamadas ---------------------------------------------------------------------------------------------

    def _acao_e_espaco(self, nome: str) -> tuple[str, str] | None:
        if nome in self.acoes:
            return nome, ESPACO_REAL
        if self.ferramentas_de_teste and nome.endswith(SUFIXO_TESTE) and nome[: -len(SUFIXO_TESTE)] in self.acoes:
            return nome[: -len(SUFIXO_TESTE)], ESPACO_TESTE
        return None

    async def chamar(
        self, amb: Ambiente, ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        amb.contagem.somar(p.name)
        pessoa = str(ctx.request.state.pessoa)
        amb.pessoas[pessoa] = amb.pessoas.get(pessoa, 0) + 1
        args = dict(p.arguments or {})
        if p.name in DEFINICOES_DE_LEITURA:
            return self._ler(amb, p.name, args)
        if p.name in contrato.ferramentas_do_nivel(self.nivel):
            return await self._contrato(amb, ctx, p.name, args)
        acao = self._acao_e_espaco(p.name)
        if acao is None:
            raise MCPError(code=types.INVALID_PARAMS, message=f"Ferramenta desconhecida: {p.name}")
        meta = dict(p.meta or {})
        amb.metas[:] = [*amb.metas[-199:], json.loads(json.dumps(meta, default=str))]
        resultado = await self._acao(amb, ctx, acao[0], acao[1], args, meta.get(contrato.CHAVE_OPERACAO))
        if acao[1] == ESPACO_TESTE and self.marca_de_teste() and isinstance(resultado, types.CallToolResult):
            resultado.meta = dict(resultado.meta or {}) | {contrato.CHAVE_ALVO: "teste"}
        return resultado

    def _com_recibo(self, saida: Saida, numero: str | None, *, repetida: bool = False) -> types.CallToolResult:
        resultado = types.CallToolResult(
            content=[types.TextContent(type="text", text=saida.texto)], is_error=saida.erro
        )
        if self.nivel == 3 and numero is not None:
            recibo: dict[str, Any] = {
                "contrato": contrato.CONTRATO,
                "operacao": numero,
                "estado": saida.estado,
                "repetida": repetida,
                "objetos": [{"id": o} for o in saida.objetos],
            }
            if saida.itens is not None:
                recibo["itens"] = dict(saida.itens)
            recibo, erro = self.recibo_final(recibo, saida.erro)
            resultado.is_error = erro
            resultado.meta = {contrato.CHAVE_RECIBO: recibo}
        return resultado

    async def _acao(
        self,
        amb: Ambiente,
        ctx: ServerRequestContext[Any],
        acao: str,
        espaco: str,
        args: dict[str, Any],
        numero: Any,
    ) -> types.CallToolResult | types.InputRequiredResult:
        assinatura = json.dumps([acao, espaco, args], sort_keys=True, ensure_ascii=False)
        if numero is None:
            # Sem numero (pessoa de nivel 1): escreve como qualquer sistema, sem guardar nada e sem recibo.
            reg = Registro("", assinatura, "em_andamento", ferramenta=acao, espaco=espaco)
            try:
                saida = await self._executar(self.onde_escrever(amb), acao, espaco, args, reg)
            except PedeDados:
                return self._resposta_ao_pedir_dados(None)
            return types.CallToolResult(content=[types.TextContent(type="text", text=saida.texto)], is_error=saida.erro)
        if not contrato.numero_valido(numero):
            return _erro_da_ferramenta("O numero da operacao nao tem a forma do contrato; nada foi feito.")
        if self.segurar_segundos:
            await anyio.sleep(self.segurar_segundos)  # a chamada "a caminho": o numero ainda nao chegou
        chave = self.chave_do_registro(ctx, numero)
        anterior = amb.registros.get(chave)
        if anterior is not None and anterior.queimado_por is not None and self.queima():
            estado = "parada" if anterior.queimado_por == "aviso" else "nao_recebida"
            texto = "Este numero ja foi encerrado pelo sistema; nada foi feito. Peca de novo com outro numero."
            if not self.recusa_o_queimado():
                return self._com_recibo(Saida("Pedido feito.", False, "concluida"), numero)  # sem aplicar nada
            if not self.recusa_sem_aplicar():
                await self._aplicar_mesmo_recusando(amb, acao, espaco, args, numero, assinatura)
            return self._com_recibo(Saida(texto, True, estado), numero)
        if anterior is not None and anterior.queimado_por is None:
            if anterior.assinatura != assinatura and self.responde_outros_como_repeticao():
                await anterior.pronto.wait()
                return self._repeticao(anterior, numero)  # sem aplicar, como se fossem os mesmos argumentos
            if anterior.assinatura != assinatura:
                if self.recusa_outros_argumentos():
                    if not self.recusa_sem_aplicar():
                        await self._aplicar_mesmo_recusando(amb, acao, espaco, args, numero, assinatura)
                    raise MCPError(
                        code=types.INVALID_PARAMS,
                        message="Este numero ja foi usado com outros argumentos; nada foi feito.",
                    )
            elif self.deduplica():
                await anterior.pronto.wait()
                if self.repeticao_aplica():
                    await self._aplicar_mesmo_recusando(amb, acao, espaco, args, numero, assinatura)
                return self._repeticao(anterior, numero)
        reg = Registro(numero, assinatura, "em_andamento", ferramenta=acao, espaco=espaco)
        if self.registra_ao_chegar():
            amb.registros[chave] = reg
        try:
            saida = await self._executar(self.onde_escrever(amb), acao, espaco, args, reg)
        except PedeDados:
            # Nada foi feito e nada fica guardado: o numero nao conta como recebido (SELO-2.3).
            amb.registros.pop(chave, None)
            return self._resposta_ao_pedir_dados(numero)
        finally:
            reg.pronto.set()
        saida = self.ajustar_saida(saida, reg)
        if not self.registra_ao_chegar():
            amb.registros[chave] = reg
        reg.texto, reg.erro, reg.estado_da_resposta = saida.texto, saida.erro, saida.estado
        if self.nivel == 3:
            reg.disse.append(saida.estado)  # o recibo tambem e uma resposta do sistema sobre o numero
        await self.depois_de_aplicar(amb, ctx, chave, reg)
        return self._com_recibo(saida, numero)

    async def _aplicar_mesmo_recusando(
        self, amb: Ambiente, acao: str, espaco: str, args: dict[str, Any], numero: str, assinatura: str
    ) -> None:
        """So os mentirosos ``recusa_mas_aplica`` e ``escreve_escondido``: a escrita pedida acontece, com os argumentos
        que vieram, e quem chamou recebe a resposta de sempre (a recusa, ou a da repeticao). O registro do numero fica
        como estava."""
        reg = Registro(numero, assinatura, "em_andamento", ferramenta=acao, espaco=espaco)
        with contextlib.suppress(PedeDados):
            await self._executar(self.onde_escrever(amb), acao, espaco, args, reg)

    def _repeticao(self, reg: Registro, numero: str) -> types.CallToolResult:
        """O desfecho da primeira chamada, sem aplicar de novo (SELO-2.2): o que de fato houve com ela, no estado de
        agora, com os mesmos objetos e ``repetida``. Enquanto o estado e o que a primeira resposta disse, o mesmo
        texto; se mudou, o texto do estado de agora (``texto_da_repeticao``), que combina com o ``isError``. Depois de
        desfeito, nao ha desfecho a repetir: erro JSON-RPC."""
        if reg.estado == "desfeita":
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="O que este numero fez ja foi desfeito; peca de novo com outro numero.",
            )
        erro = reg.estado not in ESTADOS_DE_SUCESSO
        mesmo = reg.texto and reg.estado == reg.estado_da_resposta
        texto = reg.texto if mesmo else texto_da_repeticao(reg.estado, reg.objetos)
        saida = self.saida_da_repeticao(Saida(texto, erro, reg.estado, tuple(reg.objetos), reg.itens))
        if self.nivel == 3:
            reg.disse.append(saida.estado)
        return self._com_recibo(saida, numero, repetida=True)

    def _resposta_ao_pedir_dados(self, numero: str | None) -> types.CallToolResult | types.InputRequiredResult:
        """O que volta quando a ferramenta precisa de dados no meio: ``input_required`` (o honesto), sem nada aplicado;
        ou, no mentiroso ``condicoes_falsas``, um erro de execucao (recibo ``recusada``)."""
        if self.responde_pedido_de_dados():
            return self._pede_dados()
        return self._com_recibo(Saida("E preciso confirmar o envio; nada foi enviado.", True, "recusada"), numero)

    def _pede_dados(self) -> types.InputRequiredResult:
        return types.InputRequiredResult.model_validate(
            {
                "resultType": "input_required",
                "inputRequests": {
                    "confirmar": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Confirma o envio do lembrete?",
                            "requestedSchema": {"type": "object", "properties": {"sim": {"type": "boolean"}}},
                        },
                    }
                },
                "requestState": "confirmar-lembrete",
            }
        )

    async def _executar(self, amb: Ambiente, acao: str, espaco: str, args: dict[str, Any], reg: Registro) -> Saida:
        dados = amb.espacos[espaco]
        if acao == "enviar_lembrete":
            return await self._enviar_lembrete(amb, dados, args, reg)
        if acao == "enviar_lote":
            return self._enviar_lote(amb, dados, args, reg)
        if acao == "marcar_lido":
            return self._marcar_lido(amb, dados, args, reg)
        return self._trocar_perfil(amb, dados, args, reg)

    def _concluir(self, reg: Registro, saida: Saida) -> Saida:
        reg.estado = "concluida" if saida.estado in ("concluida", "desconhecido") else saida.estado
        reg.objetos = list(saida.objetos)
        reg.itens = saida.itens
        return saida

    async def _enviar_lembrete(self, amb: Ambiente, dados: Dados, args: dict[str, Any], reg: Registro) -> Saida:
        if args.get("pedir_confirmacao"):
            if not self.pede_dados_sem_aplicar():
                dados.criar_lembrete()
                amb.aplicar("enviar_lembrete")
            raise PedeDados
        if "@" not in str(args.get("para", "")):
            if not self.recusa_sem_aplicar():
                dados.criar_lembrete()
                amb.aplicar("enviar_lembrete")
            return self._concluir(reg, Saida("O endereco nao existe; nada foi enviado.", True, "recusada"))
        daqui_a = args.get("daqui_a")
        if isinstance(daqui_a, int | float) and daqui_a > 0:
            if self.tarefas is None:
                raise RuntimeError("o duble do selo roda dentro do ciclo de vida do app")
            self.adiados[amb.alvo] = self.adiados.get(amb.alvo, 0) + 1  # antes da resposta: /contagem ja o ve
            self.tarefas.start_soon(self._efeito_adiado, amb, dados, reg, float(daqui_a))
            return Saida(f"O lembrete sai em {daqui_a} segundos.", False, "em_andamento")
        demora = args.get("demora")
        # A escrita termina mesmo se quem chamou cortar a conexao (A3). O kit 2.3.0 nao cancela a chamada quando a
        # conexao cai (o teste da escrita lenta cortada prova o comportamento); o escudo vale se um dia cancelar.
        with anyio.CancelScope(shield=True):
            if isinstance(demora, int | float) and demora > 0:
                await self._escrita_lenta(reg.numero, float(demora))
                if reg.estado == "parada":
                    return Saida("O pedido foi parado pelo aviso de bloqueio; nada foi enviado.", True, "parada")
            ident = dados.criar_lembrete()
            amb.aplicar("enviar_lembrete")
            if args.get("sem_saber"):
                texto = (
                    "O sistema por tras nao respondeu a tempo: nao se sabe se o lembrete foi enviado. Consulte o"
                    " numero antes de pedir de novo."
                )
                return self._concluir(reg, Saida(texto, True, "desconhecido", (ident,)))
            return self._concluir(reg, Saida(f"Lembrete enviado ({ident}).", False, "concluida", (ident,)))

    async def _escrita_lenta(self, numero: str, segundos: float) -> None:
        """Segura a escrita ``segundos`` antes de aplicar. Com numero, a rota de controle ``POST /soltar`` termina a
        espera antes da hora (so os testes usam: a escrita continua sendo a mesma, so mais curta)."""
        solta = anyio.Event()
        if numero:
            self.lentas[numero] = solta
        try:
            with anyio.move_on_after(segundos):
                await solta.wait()
        finally:
            if numero and self.lentas.get(numero) is solta:
                del self.lentas[numero]

    async def _efeito_adiado(self, amb: Ambiente, dados: Dados, reg: Registro, segundos: float) -> None:
        """O efeito adiado acontece ``segundos`` depois, se o aviso de bloqueio nao parar antes. Parado, acorda na hora
        (``reg.parou``): o efeito nao acontece, e ``/contagem`` mostra o fim sem esperar o relogio. O mentiroso
        ``escreve_escondido`` nao acorda com o aviso: poe o efeito no fim dos ``segundos``, mesmo parado. Quem le o
        rastro logo depois do aviso, sem esperar o prazo, nao ve esse efeito (C-3.6b)."""
        try:
            if self.para_o_efeito_adiado():
                with anyio.move_on_after(segundos):
                    await reg.parou.wait()
            else:
                await anyio.sleep(segundos)  # o mentiroso nao olha o aviso: o efeito sai no horario marcado
            if reg.estado == "parada" and not self.para_o_efeito_adiado():
                dados.criar_lembrete()  # o mentiroso escreve mesmo parado; o registro continua dizendo parada
                amb.aplicar("enviar_lembrete")
                return
            if reg.estado != "em_andamento":
                return  # parado pelo aviso de bloqueio (ou desfeito): o efeito nao acontece
            ident = dados.criar_lembrete()
            amb.aplicar("enviar_lembrete")
            reg.estado = "concluida"
            reg.objetos = [ident]
            reg.depois.append({"evento": "concluida", "em": agora()})
        finally:
            self.adiados[amb.alvo] -= 1

    def _enviar_lote(self, amb: Ambiente, dados: Dados, args: dict[str, Any], reg: Registro) -> Saida:
        itens = args.get("itens")
        if not isinstance(itens, list) or not itens:
            return self._concluir(reg, Saida("O lote veio vazio; nada foi enviado.", True, "recusada"))
        bons = [i for i in itens if isinstance(i, dict) and "@" in str(i.get("para", ""))]
        criados = tuple(dados.criar_lembrete() for _ in bons)
        ruins_aplicados = 0 if self.recusa_sem_aplicar() else len(itens) - len(bons)
        for _ in range(ruins_aplicados):
            dados.criar_lembrete()  # os itens ruins tambem saem, e a resposta diz que nao
        if criados or ruins_aplicados:
            amb.aplicar("enviar_lote")
        contagem = {"aplicados": len(criados), "nao_aplicados": len(itens) - len(criados)}
        if not criados:
            texto = "Nenhum lembrete do lote foi enviado: os enderecos nao existem."
            return self._concluir(reg, Saida(texto, True, "recusada", (), contagem))
        if contagem["nao_aplicados"]:
            texto = f"So {len(criados)} de {len(itens)} lembretes foram enviados; os outros tinham endereco ruim."
            return self._concluir(reg, Saida(texto, True, "parcial", criados, contagem))
        return self._concluir(reg, Saida(f"{len(criados)} lembretes enviados.", False, "concluida", criados, contagem))

    def _marcar_lido(self, amb: Ambiente, dados: Dados, args: dict[str, Any], reg: Registro) -> Saida:
        lembrete = str(args.get("lembrete", "")).strip()
        if not lembrete:
            if not self.recusa_sem_aplicar():
                dados.lidos.add(lembrete)
                dados.aplicacoes_lidos += 1
                amb.aplicar("marcar_lido")
            return self._concluir(reg, Saida("Sem lembrete para marcar; nada foi feito.", True, "recusada"))
        if lembrete in dados.lidos and self.marcar_de_novo_desmarca():
            dados.lidos.discard(lembrete)
        else:
            dados.lidos.add(lembrete)
        dados.aplicacoes_lidos += 1
        amb.aplicar("marcar_lido")
        return self._concluir(reg, Saida("Marcado como lido.", False, "concluida"))

    def _trocar_perfil(self, amb: Ambiente, dados: Dados, args: dict[str, Any], reg: Registro) -> Saida:
        campos: dict[str, str] = {k: str(args[k]) for k in ("nome", "telefone") if k in args}
        if not campos or any(not v.strip() for v in campos.values()):
            if campos and not self.recusa_sem_aplicar():
                self._mudar_perfil(amb, dados, campos)
            return self._concluir(reg, Saida("Nada para mudar no perfil; nada foi feito.", True, "recusada"))
        versao = args.get("versao")
        if versao is not None and versao != dados.versao:
            texto = f"Conflito: o perfil mudou depois da leitura (versao {dados.versao}); nada foi aplicado."
            if not self.recusa_sem_aplicar():
                self._mudar_perfil(amb, dados, campos)  # a escrita velha passa por cima da nova
            return self._concluir(reg, Saida(texto, True, "recusada"))
        self._mudar_perfil(amb, dados, campos)
        return self._concluir(reg, Saida(f"Perfil atualizado (versao {dados.versao}).", False, "concluida"))

    def _mudar_perfil(self, amb: Ambiente, dados: Dados, campos: Mapping[str, str]) -> None:
        if "nome" in campos and "telefone" not in campos and self.nome_muda_o_telefone():
            campos = dict(campos) | {"telefone": f"v{dados.versao + 1}"}  # o mentiroso grava o telefone junto
        dados.perfil.update(campos)
        dados.versao += 1
        dados.mudancas += 1
        amb.aplicar("trocar_perfil")

    def _ler(self, amb: Ambiente, nome: str, args: dict[str, Any]) -> types.CallToolResult:
        espaco = ESPACO_TESTE if args.get("espaco") == ESPACO_TESTE else ESPACO_REAL
        dados = amb.espacos[espaco]
        if nome == "listar_lembretes":
            return _estruturado({"itens": list(dados.lembretes), "aplicacoes": dados.aplicacoes_lembretes})
        if nome == "listar_lidos":
            return _estruturado({"lidos": sorted(dados.lidos), "aplicacoes": dados.aplicacoes_lidos})
        return _estruturado(dados.perfil | {"versao": dados.versao, "mudancas": dados.mudancas})

    # --- ferramentas do contrato ------------------------------------------------------------------------------

    async def _contrato(
        self, amb: Ambiente, ctx: ServerRequestContext[Any], nome: str, args: dict[str, Any]
    ) -> types.CallToolResult:
        if nome == contrato.FERRAMENTA_ALVO:
            return _estruturado({"contrato": contrato.CONTRATO, "alvo": self.alvo_declarado(amb)})
        if nome == contrato.FERRAMENTA_CONSULTAR:
            await self.antes_da_consulta(ctx)
            return self._consultar(amb, ctx, args)
        await self.antes_do_aviso(ctx)
        return self._bloqueio(amb, ctx, args)

    def _consultar(self, amb: Ambiente, ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        operacao = args.get("operacao")
        if set(args) != {"operacao"} or not isinstance(operacao, str) or not contrato.numero_valido(operacao):
            return _erro_da_ferramenta("O numero da operacao nao tem a forma do contrato.")
        chave = self.chave_do_registro(ctx, operacao)
        reg = amb.registros.get(chave)
        if reg is None:
            reg = Registro.queimado(operacao, "consulta")
            if self.queima():
                amb.registros[chave] = reg
            self.consulta_de_numero_novo(amb)
        estado = self.estado_na_consulta(reg)
        reg.disse.append(estado)
        dados: dict[str, Any] = {"contrato": contrato.CONTRATO, "operacao": operacao, "estado": estado}
        if self.nivel == 3:
            dados["depois"] = [dict(e) for e in reg.depois]
        return _estruturado(self.resposta_da_consulta(dados))

    def _bloqueio(self, amb: Ambiente, ctx: ServerRequestContext[Any], args: dict[str, Any]) -> types.CallToolResult:
        bloqueio, operacoes = args.get("bloqueio"), args.get("operacoes")
        if (
            set(args) != {"bloqueio", "operacoes"}
            or not isinstance(bloqueio, str)
            or not contrato.numero_valido(bloqueio)
            or not isinstance(operacoes, list)
            or not 1 <= len(operacoes) <= contrato.MAX_OPERACOES_POR_AVISO
            or not all(contrato.numero_valido(o) for o in operacoes)
        ):
            return _erro_da_ferramenta("O aviso de bloqueio nao tem a forma do contrato.")
        chave_do_aviso = (str(ctx.request.state.pessoa), bloqueio)
        if self.guarda_o_aviso() and chave_do_aviso in amb.bloqueios:
            return _estruturado(amb.bloqueios[chave_do_aviso])  # o mesmo aviso: as mesmas respostas
        respostas: list[dict[str, str]] = []
        for operacao in dict.fromkeys(operacoes):  # cada numero uma vez, na ordem
            chave = self.chave_do_registro(ctx, operacao)
            reg = amb.registros.get(chave)
            if reg is None:
                if self.obedece_bloqueio():
                    amb.registros[chave] = Registro.queimado(operacao, "aviso")
                estado = self.estado_do_novo_no_aviso()
            elif reg.queimado_por is not None:
                estado = self.estado_do_queimado_no_aviso(reg)
            elif reg.estado == "em_andamento":
                if self.obedece_bloqueio():
                    reg.estado = "parada"
                    reg.depois.append({"evento": "parada", "em": agora()})
                    reg.parou.set()
                estado = self.estado_do_parado_no_aviso(reg)
            else:
                estado = reg.estado
            if reg is not None:
                reg.disse.append(estado)
            respostas.append({"operacao": operacao, "estado": estado})
        dados = {"contrato": contrato.CONTRATO, "bloqueio": bloqueio, "operacoes": respostas}
        amb.bloqueios[chave_do_aviso] = dados
        return _estruturado(dados)

    # --- controle (rotas de teste) ----------------------------------------------------------------------------

    def contagem(self) -> dict[str, Any]:
        return {
            alvo: {
                "chamadas": dict(a.contagem.chamadas),
                "aplicadas": dict(a.aplicadas),
                "pessoas": dict(a.pessoas),
                "adiados": self.adiados.get(alvo, 0),
            }
            for alvo, a in self.ambientes.items()
        }

    def soltar(self, numero: str) -> bool:
        """Termina agora a espera da escrita lenta com ``numero`` (``POST /soltar``). False se nao ha uma em curso."""
        solta = self.lentas.get(numero)
        if solta is None:
            return False
        solta.set()
        return True

    def zerar(self) -> None:
        for a in self.ambientes.values():
            a.contagem.chamadas.clear()
            a.contagem.autorizacoes_vistas.clear()
            a.aplicadas.clear()
            a.metas.clear()
            a.pessoas.clear()

    def numeros(self) -> dict[str, Any]:
        return {
            alvo: [
                {"numero": n, "ferramenta": r.ferramenta, "estado": r.estado, "queimado_por": r.queimado_por}
                for (_, n), r in a.registros.items()
            ]
            for alvo, a in self.ambientes.items()
        }

    def mexer(self, numero: str, acao: str, alvo: str) -> bool:
        amb = self.ambientes.get(alvo)
        reg = amb.achar(numero) if amb is not None else None
        if amb is None or reg is None or reg.queimado_por is not None:
            return False
        if acao == "desfazer":
            dados = amb.espacos[reg.espaco]
            for o in reg.objetos:
                if o in dados.lembretes:
                    dados.lembretes.remove(o)
            reg.estado = "desfeita"
            reg.depois.append({"evento": "desfeita", "em": agora()})
            return True
        if acao == "desconhecer":
            reg.estado = "desconhecido"
            return True
        return False

    def modos_aceitos(self) -> frozenset[str]:
        return frozenset()

    def mudar_modo(self, modo: str) -> None:
        raise ValueError("este duble e honesto e nao tem modos")


# --- O app ---------------------------------------------------------------------------------------------------


async def _ler_corpo(receive: Receive) -> tuple[bytes, bool]:
    corpo = b""
    while True:
        mensagem = await receive()
        if mensagem["type"] != "http.request":
            return corpo, True
        corpo += mensagem.get("body", b"")
        if not mensagem.get("more_body"):
            return corpo, False


class Vigia:
    """Le o corpo do pedido, entrega ao MCP e vigia a conexao: ``scope["state"]["desconexao"]`` e um ``anyio.Event``
    que acende quando quem chamou fecha a conexao antes da resposta. O honesto nao olha (a escrita termina do mesmo
    jeito); o mentiroso ``esquece_ao_desconectar`` olha, e os mudos (``consulta_muda``, ``aviso_mudo``) esperam por
    ele: uma resposta que nunca vem nao pode segurar o servidor depois que quem chamou desistiu."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        corpo, caiu_no_corpo = await _ler_corpo(receive)
        caiu = anyio.Event()
        if caiu_no_corpo:
            caiu.set()
        scope.setdefault("state", {})["desconexao"] = caiu
        entregue = False

        async def receber() -> Message:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            await caiu.wait()
            return {"type": "http.disconnect"}

        async def vigiar() -> None:
            while not caiu.is_set():
                if (await receive())["type"] == "http.disconnect":
                    caiu.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(vigiar)
            try:
                await self.app(scope, receber, send)
            finally:
                tg.cancel_scope.cancel()


def _servidor(motor: Motor, amb: Ambiente) -> Server[Any]:
    async def listar(ctx: ServerRequestContext[Any], _: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(f) for f in motor.ferramentas(amb)])

    async def chamar(
        ctx: ServerRequestContext[Any], p: types.CallToolRequestParams
    ) -> types.CallToolResult | types.InputRequiredResult:
        return await motor.chamar(amb, ctx, p)

    return Server(f"duble-selo-{amb.alvo}", version="1.0.0", on_list_tools=listar, on_call_tool=chamar)


def montar(motor: Motor) -> Starlette:
    """O app do duble: o MCP em cada caminho (producao e, se houver, o alvo de teste) e as rotas de controle."""
    gerentes: list[StreamableHTTPSessionManager] = []
    rotas: list[Route] = []
    for amb in motor.ambientes.values():
        sm = StreamableHTTPSessionManager(
            app=_servidor(motor, amb),
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        gerentes.append(sm)
        ponto: ASGIApp = FiltroChave(StreamableHTTPASGIApp(sm), amb.contagem, motor.mapa_de_chaves)
        rotas.append(Route(amb.caminho, endpoint=Vigia(ponto)))

    @contextlib.asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as pilha:
            for sm in gerentes:
                await pilha.enter_async_context(sm.run())
            async with anyio.create_task_group() as tg:
                motor.tarefas = tg
                try:
                    yield
                finally:
                    motor.tarefas = None
                    tg.cancel_scope.cancel()

    async def ver_contagem(_: Request) -> JSONResponse:
        return JSONResponse(motor.contagem())

    async def zerar(_: Request) -> JSONResponse:
        motor.zerar()
        return JSONResponse({"ok": True})

    async def ver_numeros(_: Request) -> JSONResponse:
        return JSONResponse(motor.numeros())

    async def ver_metas(_: Request) -> JSONResponse:
        return JSONResponse({alvo: list(a.metas) for alvo, a in motor.ambientes.items()})

    async def ajustar(pedido: Request) -> JSONResponse:
        corpo = await pedido.json()
        if "segurar_segundos" in corpo:
            motor.segurar_segundos = max(0.0, float(corpo["segurar_segundos"]))
        if "listar_contrato" in corpo:
            motor.listar_contrato = bool(corpo["listar_contrato"])
        if "tirar" in corpo:
            motor.tirar = frozenset(str(n) for n in corpo["tirar"])
        return JSONResponse(
            {
                "segurar_segundos": motor.segurar_segundos,
                "listar_contrato": motor.listar_contrato,
                "tirar": sorted(motor.tirar),
            }
        )

    async def operacao(pedido: Request) -> JSONResponse:
        corpo = await pedido.json()
        feito = motor.mexer(str(corpo.get("operacao")), str(corpo.get("acao")), str(corpo.get("alvo", "producao")))
        return JSONResponse({"ok": feito}, status_code=200 if feito else 404)

    async def soltar(pedido: Request) -> JSONResponse:
        feito = motor.soltar(str((await pedido.json()).get("operacao")))
        return JSONResponse({"ok": feito}, status_code=200 if feito else 404)

    async def mudar_modo(pedido: Request) -> JSONResponse:
        modo = str((await pedido.json()).get("modo"))
        if modo not in motor.modos_aceitos():
            return JSONResponse({"erro": f"modo desconhecido: {modo}"}, status_code=400)
        motor.mudar_modo(modo)
        return JSONResponse({"modo": modo})

    rotas += [
        Route("/contagem", ver_contagem),
        Route("/contagem/zerar", zerar, methods=["POST"]),
        Route("/numeros", ver_numeros),
        Route("/metas", ver_metas),
        Route("/ajustes", ajustar, methods=["POST"]),
        Route("/operacao", operacao, methods=["POST"]),
        Route("/soltar", soltar, methods=["POST"]),
        Route("/modo", mudar_modo, methods=["POST"]),
    ]
    app = Starlette(routes=rotas, lifespan=ciclo)
    app.state.motor = motor
    return app


def criar(
    *,
    nivel: int = 2,
    producao: str = CAMINHO_PRODUCAO,
    teste: str | None = CAMINHO_TESTE,
    ferramentas_de_teste: bool = False,
    esquemas: Literal["contrato", "kit"] = "contrato",
    acoes: Sequence[str] = ACOES,
    mapa_de_chaves: Mapping[str, str] | None = None,
) -> Starlette:
    """O duble honesto do contrato (nivel 2 por padrao). ``mapa_de_chaves``: chave de teste -> pessoa (sem ele, as de
    ``exemplos.comum``, ou ``EXEMPLO_CHAVES``, mais ``CHAVES_DO_SELO``). ``selo_nivel3.criar`` e
    ``selo_mentiroso.criar`` aceitam os mesmos parametros (menos ``nivel``, e o mentiroso tem ``modo``)."""
    return montar(
        Motor(
            nivel=nivel,
            producao=producao,
            teste=teste,
            ferramentas_de_teste=ferramentas_de_teste,
            esquemas=esquemas,
            acoes=acoes,
            mapa_de_chaves=mapa_de_chaves,
        )
    )


# --- A declaracao de teste deste duble -----------------------------------------------------------------------

PARA: Final = "caixa-de-teste@exemplo.teste"


def entrada_da_declaracao(acao: str, *, de_teste: bool, segundos: int = 1) -> dict[str, Any]:
    """A entrada de ``acao`` na declaracao de teste (desenho 2.5): os argumentos de cada caso e o rastro."""
    leitura: dict[str, Any] = {"espaco": ESPACO_TESTE} if de_teste else {}
    entrada: dict[str, Any]
    if acao == "enviar_lembrete":
        base = {"para": PARA, "texto": "teste do selo"}
        entrada = {
            "argumentos": base,
            "argumentos_outros": {"para": PARA, "texto": "outro texto"},
            "argumentos_recusados": {"para": "nao-e-um-endereco", "texto": "teste do selo"},
            "rastro": {"ferramenta": "listar_lembretes", "argumentos": leitura, "conta": "/itens"},
            "efeito_adiado": {"argumentos": base | {"texto": "adiado", "daqui_a": segundos}, "segundos": segundos},
            "escrita_lenta": {"argumentos": base | {"texto": "lento", "demora": segundos}, "segundos": segundos},
            "pede_dados": {"argumentos": base | {"pedir_confirmacao": True}},
        }
    elif acao == "enviar_lote":
        bom = {"para": PARA, "texto": "lote"}
        entrada = {
            # Um item so nos argumentos de sempre: o rastro conta lembretes, e os casos C-2.x esperam +1 por escrita.
            # O lote de verdade (varios itens, um ruim) fica em varios_itens (C-3.3).
            "argumentos": {"itens": [bom]},
            "argumentos_outros": {"itens": [{"para": PARA, "texto": "outro lote"}]},
            "argumentos_recusados": {"itens": [{"para": "nao-e-um-endereco", "texto": "lote"}]},
            "rastro": {"ferramenta": "listar_lembretes", "argumentos": leitura, "conta": "/itens"},
            "varios_itens": {
                "argumentos": {"itens": [bom, {"para": "nao-e-um-endereco", "texto": "ruim"}, bom]},
                "itens": 3,
            },
        }
    elif acao == "marcar_lido":
        entrada = {
            "argumentos": {"lembrete": "lembrete-de-teste"},
            "argumentos_outros": {"lembrete": "outro-lembrete"},
            "argumentos_recusados": {"lembrete": ""},
            "rastro": {"ferramenta": "listar_lidos", "argumentos": leitura, "conta": "/aplicacoes"},
            "idempotente": {"rastro": {"ferramenta": "listar_lidos", "argumentos": leitura, "conta": "/lidos"}},
        }
    elif acao == "trocar_perfil":
        entrada = {
            "argumentos": {"nome": "Pessoa de teste"},
            "argumentos_outros": {"nome": "Outro nome"},
            "argumentos_recusados": {"nome": " "},
            "rastro": {"ferramenta": "ler_perfil", "argumentos": leitura, "conta": "/mudancas"},
            "conflito": {
                "leitura": {
                    "ferramenta": "ler_perfil",
                    "argumentos": leitura,
                    "versao": "/versao",
                    "campo": "/nome",
                    "campo_concorrente": "/telefone",
                },
                "argumento_versao": "versao",
                "argumentos": {"nome": "Escritor A"},
                "argumentos_concorrente": {"telefone": "0000-0000"},
            },
        }
    else:
        raise ValueError(f"acao desconhecida: {acao}")
    if de_teste:
        entrada["ferramenta_de_teste"] = acao + SUFIXO_TESTE
    return entrada


def declaracao(
    *,
    endereco_teste: str | None,
    modo: Literal["ambiente_de_teste", "ferramentas_de_teste"] = "ambiente_de_teste",
    acoes: Sequence[str] = ACOES,
    responsavel: str = "Equipe do duble do selo",
    segundos: int = 1,
) -> dict[str, Any]:
    """A declaracao de teste deste duble (desenho 2.5): no modo ambiente, com ``endereco_teste``; no modo de
    ferramentas de teste, sem ele, com ``<acao>_teste`` e o rastro no espaco de teste."""
    de_teste = modo == "ferramentas_de_teste"
    dados: dict[str, Any] = {
        "contrato": contrato.CONTRATO,
        "modo": modo,
        "responsavel": responsavel,
        "guarda_dias": GUARDA_DIAS,
        "ferramentas": {a: entrada_da_declaracao(a, de_teste=de_teste, segundos=segundos) for a in acoes},
    }
    if not de_teste:
        dados["endereco_teste"] = endereco_teste
    return dados


if __name__ == "__main__":
    rodar(criar, 9110)
