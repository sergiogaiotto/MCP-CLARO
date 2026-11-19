"""Regras dos programas que rodam sozinhos (decisao 9). Na Fase 2, programas so leem.

Regras fixas no codigo (plano, "Programas que rodam sozinhos: nossa sugestao"):

- um aplicativo do tipo ``programa`` so passa com cadastro em ``programas``: situacao ``ativo``, data de
  fim igual ou depois de hoje (no fuso de Brasilia), responsavel e substituto ativos. Sem isso, o pedido e
  recusado com frase legivel, e a recusa vai ao extrato;
- nos sistemas, o programa usa so a chave propria, guardada no cofre (``sistemas.tipo_chave = 'programa'``).
  Sistema de chave por pessoa ou compartilhada nao entra na vitrine do programa, e a chamada e recusada
  (plano, passo 4). Do outro lado, nenhuma pessoa usa a chave de um programa: sistema de chave de programa
  nao entra na vitrine de um assistente;
- cada programa tem limite de pedidos por minuto (``PROGRAM_MAX_REQUESTS_PER_MINUTE``), contado no
  PostgreSQL num so comando: o pedido de numero N+1 no mesmo minuto e recusado, mesmo com pedidos ao mesmo
  tempo. A regra de um programa nunca afrouxa o padrao da instalacao (``config.politica.nunca_mais_frouxo``);
- no extrato, as linhas de um programa dizem "programa X, responsavel Y" (``linha``).

Acoes de programas: com ``PROGRAM_ACTIONS_ENABLED`` desligado, a entrada ja recusa (motivo ``programa_so_le``).

Quem usa: a entrada (``conferir_pedido`` em cada pedido de programa, ``filtrar_vitrine`` na vitrine,
``recusa_da_chave`` na chamada, ``linha`` nas recusas) e o despachante (``reconferir_leitura`` antes de abrir
a chave, ``linha`` no extrato da leitura).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Protocol

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.cadastro.modelos import Aplicativo
from mcp_claro.config.politica import nunca_mais_frouxo
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES

log = logger("mcp_claro.regras.programas")

FUSO: Final = "America/Sao_Paulo"
TIPO_PROGRAMA: Final = "programa"
CHAVE_LIMITE: Final = "PROGRAM_MAX_REQUESTS_PER_MINUTE"

# Tipo da linha do extrato -> o mesmo passo, dito para um programa ("programa X, responsavel Y").
PARA_PROGRAMA: Final[dict[str, str]] = {
    "leitura_feita": "programa_leitura_feita",
    "leitura_recusada": "programa_leitura_recusada",
    "pedido_recusado": "programa_pedido_recusado",
}

_TIPOS_CHAVE_LEGIVEIS: Final = {"por_pessoa": "uma chave por pessoa", "compartilhada": "uma chave compartilhada"}

_SQL_PROGRAMA = """
SELECT a.id AS aplicativo_id, a.client_id, a.nome, a.tipo, a.situacao AS situacao_aplicativo, a.visto_em,
       pr.aplicativo_id AS tem_cadastro, pr.finalidade, pr.situacao, pr.fim, pr.revisado_em, pr.chave_trocada_em,
       pr.criado_em, pr.responsavel_id, coalesce(r.nome, r.sujeito) AS responsavel,
       r.situacao = 'ativa' AS responsavel_ativo, pr.substituto_id, coalesce(s.nome, s.sujeito) AS substituto,
       s.situacao = 'ativa' AS substituto_ativo, (now() AT TIME ZONE :fuso)::date AS hoje
FROM aplicativos a
LEFT JOIN programas pr ON pr.aplicativo_id = a.id
LEFT JOIN pessoas r ON r.id = pr.responsavel_id
LEFT JOIN pessoas s ON s.id = pr.substituto_id
"""


@dataclass(frozen=True, slots=True)
class Programa:
    """O cadastro de um programa, como as regras o veem. ``hoje`` e a data de hoje em Brasilia, lida do banco."""

    aplicativo_id: uuid.UUID
    client_id: str
    nome: str
    finalidade: str
    situacao: str
    fim: date
    responsavel_id: uuid.UUID
    responsavel: str
    responsavel_ativo: bool
    substituto_id: uuid.UUID
    substituto: str
    substituto_ativo: bool
    revisado_em: datetime
    chave_trocada_em: datetime
    criado_em: datetime
    visto_em: datetime | None
    hoje: date

    @property
    def vencido(self) -> bool:
        """A data de fim ja passou: o cadastro vale ate o fim do dia ``fim``, no fuso de Brasilia."""
        return self.fim < self.hoje

    def campos(self) -> dict[str, object]:
        """Campos das frases do extrato que falam de um programa."""
        return {"programa": self.nome, "responsavel": self.responsavel}


class ProgramaRecusado(Exception):
    """Pedido de programa recusado. ``mensagem`` vai ao programa; ``motivo`` e o codigo curto, sem acento."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


@dataclass(frozen=True, slots=True)
class Linha:
    """Tipo e campos de uma linha do extrato, prontos para ``registrar``."""

    tipo: str
    campos: dict[str, object]


class _ComSistema(Protocol):
    @property
    def sistema(self) -> Any: ...


def _programa(linha: Row[Any]) -> Programa | None:
    if linha.tem_cadastro is None:
        return None
    return Programa(
        aplicativo_id=uuid.UUID(str(linha.aplicativo_id)),
        client_id=str(linha.client_id),
        nome=str(linha.nome),
        finalidade=str(linha.finalidade),
        situacao=str(linha.situacao),
        fim=linha.fim,
        responsavel_id=uuid.UUID(str(linha.responsavel_id)),
        responsavel=str(linha.responsavel),
        responsavel_ativo=bool(linha.responsavel_ativo),
        substituto_id=uuid.UUID(str(linha.substituto_id)),
        substituto=str(linha.substituto),
        substituto_ativo=bool(linha.substituto_ativo),
        revisado_em=linha.revisado_em,
        chave_trocada_em=linha.chave_trocada_em,
        criado_em=linha.criado_em,
        visto_em=linha.visto_em,
        hoje=linha.hoje,
    )


async def _linha(c: AsyncConnection, aplicativo_id: uuid.UUID) -> Row[Any] | None:
    return (await c.execute(text(_SQL_PROGRAMA + " WHERE a.id = :a"), {"a": aplicativo_id, "fuso": FUSO})).first()


async def ler(c: AsyncConnection, aplicativo_id: uuid.UUID) -> Programa | None:
    """O cadastro do programa deste aplicativo; ``None`` se nao ha linha em ``programas``."""
    linha = await _linha(c, aplicativo_id)
    return _programa(linha) if linha is not None else None


async def todos(c: AsyncConnection) -> list[Programa]:
    """Todos os programas cadastrados, por nome (telas do ADMIN)."""
    linhas = (
        await c.execute(text(_SQL_PROGRAMA + " WHERE pr.aplicativo_id IS NOT NULL ORDER BY a.nome"), {"fuso": FUSO})
    ).all()
    return [p for p in (_programa(linha) for linha in linhas) if p is not None]


def sem_cadastro() -> ProgramaRecusado:
    return ProgramaRecusado(
        "programa_sem_cadastro",
        "Este programa nao tem cadastro de programa no MCP-CLARO (responsavel, substituto e data de fim). "
        "Fale com o ADMIN. Nada foi enviado.",
    )


def recusa_do_cadastro(programa: Programa | None) -> ProgramaRecusado | None:
    """Por que o cadastro nao deixa o programa passar agora; ``None`` se ele vale."""
    if programa is None:
        return sem_cadastro()
    if programa.situacao == "encerrado":
        return ProgramaRecusado("programa_encerrado", "Este programa foi encerrado no MCP-CLARO. Nada foi enviado.")
    if programa.situacao != "ativo":
        return ProgramaRecusado(
            "programa_pausado",
            "Este programa esta pausado no MCP-CLARO. O ADMIN retoma depois de conferir. Nada foi enviado.",
        )
    if programa.vencido:
        return ProgramaRecusado(
            "programa_vencido",
            f"O cadastro deste programa valia ate {programa.fim:%d/%m/%Y}. Fale com o ADMIN. Nada foi enviado.",
        )
    if not programa.responsavel_ativo or not programa.substituto_ativo:
        return ProgramaRecusado(
            "programa_sem_responsavel",
            "O responsavel ou o substituto deste programa nao esta mais ativo. O programa fica parado ate o ADMIN "
            "corrigir o cadastro. Nada foi enviado.",
        )
    return None


def chave_permitida(tipo_aplicativo: str, tipo_chave: str) -> bool:
    """Programa so usa sistema de chave propria; nenhuma pessoa usa a chave de um programa."""
    if tipo_aplicativo == TIPO_PROGRAMA:
        return tipo_chave == "programa"
    return tipo_chave != "programa"


def recusa_da_chave(tipo_aplicativo: str, tipo_chave: str) -> ProgramaRecusado | None:
    """A recusa da chamada quando o tipo de chave do sistema nao serve a este aplicativo; ``None`` se serve."""
    if chave_permitida(tipo_aplicativo, tipo_chave):
        return None
    if tipo_aplicativo == TIPO_PROGRAMA:
        return ProgramaRecusado(
            "programa_sem_chave_propria",
            "Programas que rodam sozinhos so usam sistemas com chave propria do programa. Este sistema usa "
            f"{_TIPOS_CHAVE_LEGIVEIS.get(tipo_chave, 'outra chave')}. Nada foi enviado.",
        )
    return ProgramaRecusado(
        "chave_de_programa",
        "Este sistema usa a chave de um programa que roda sozinho; pessoas nao usam essa chave. Nada foi enviado.",
    )


def filtrar_vitrine[F: _ComSistema](tipo_aplicativo: str, ferramentas: Sequence[F]) -> list[F]:
    """Tira da vitrine as ferramentas de sistema cujo tipo de chave nao serve a este aplicativo."""
    return [f for f in ferramentas if chave_permitida(tipo_aplicativo, str(f.sistema.tipo_chave))]


def limite_por_minuto(p: Parametros, proprio: int | None = None) -> int:
    """Pedidos por minuto deste programa: o padrao da instalacao, ou o do programa se for mais apertado."""
    padrao = int(p.PROGRAM_MAX_REQUESTS_PER_MINUTE)
    if proprio is None:
        return padrao
    return max(1, nunca_mais_frouxo(CHAVE_LIMITE, padrao, int(proprio)))


async def contar_pedido(c: AsyncConnection, aplicativo_id: uuid.UUID, limite: int) -> bool:
    """Conta um pedido do programa neste minuto, se houver saldo. Um comando so: nunca passa do limite."""
    contagem = (
        await c.execute(
            text(
                "INSERT INTO programas_pedidos_minuto AS m (aplicativo_id, minuto, contagem)"
                " VALUES (:a, date_trunc('minute', now()), 1)"
                " ON CONFLICT (aplicativo_id, minuto) DO UPDATE SET contagem = m.contagem + 1"
                " WHERE m.contagem < :limite RETURNING m.contagem"
            ),
            {"a": aplicativo_id, "limite": limite},
        )
    ).scalar_one_or_none()
    return contagem is not None and int(contagem) <= limite


async def conferir_pedido(engine: AsyncEngine, p: Parametros, aplicativo: Aplicativo, pessoa_id: uuid.UUID) -> Programa:
    """Cada pedido de um programa na entrada: cadastro valendo e limite por minuto. Recusa vai ao extrato.

    Levanta ``ProgramaRecusado``. Pedido recusado pelo cadastro nao gasta a contagem do minuto.
    """
    recusa: ProgramaRecusado | None
    async with engine.begin() as c:
        programa = await ler(c, aplicativo.id)
        if programa is None:
            recusa = sem_cadastro()
        else:
            recusa = recusa_do_cadastro(programa)
            if recusa is None:
                limite = limite_por_minuto(p)
                if await contar_pedido(c, aplicativo.id, limite):
                    return programa
                recusa = ProgramaRecusado(
                    "programa_limite_minuto",
                    f"Este programa passou do limite de {limite} pedidos por minuto. Nada foi enviado. "
                    "Tente de novo no proximo minuto.",
                )
    async with engine.begin() as c:
        if programa is None:
            await registrar(
                c,
                papel="entrada",
                tipo="programa_sem_cadastro",
                campos={"aplicativo": aplicativo.nome},
                aplicativo_id=aplicativo.id,
                pessoa_id=pessoa_id,
                dados={"motivo": recusa.motivo},
            )
        else:
            await registrar(
                c,
                papel="entrada",
                tipo="programa_recusado",
                campos=programa.campos() | {"motivo": recusa.motivo},
                aplicativo_id=aplicativo.id,
                pessoa_id=pessoa_id,
                dados={"motivo": recusa.motivo, "responsavel_id": str(programa.responsavel_id)},
            )
    CONTADORES.somar("programas_recusados", motivo=recusa.motivo)
    log.info("programa_recusado", motivo=recusa.motivo)
    raise recusa


async def reconferir_leitura(c: AsyncConnection, aplicativo_id: uuid.UUID, tipo_chave: str) -> ProgramaRecusado | None:
    """Reconferencia do despachante antes de abrir a chave: a mesma regra da entrada, sem gastar a contagem."""
    linha = await _linha(c, aplicativo_id)
    if linha is None:
        return None  # aplicativo desconhecido: o despachante ja recusa por outra regra
    if linha.tipo != TIPO_PROGRAMA:
        return recusa_da_chave(str(linha.tipo), tipo_chave)
    return recusa_do_cadastro(_programa(linha)) or recusa_da_chave(TIPO_PROGRAMA, tipo_chave)


async def linha(c: AsyncConnection, aplicativo: Aplicativo | uuid.UUID, tipo: str, campos: dict[str, object]) -> Linha:
    """Tipo e campos da linha do extrato. Para um programa com cadastro, a frase diz "programa X, responsavel Y"."""
    if tipo not in PARA_PROGRAMA:
        return Linha(tipo, campos)
    if isinstance(aplicativo, Aplicativo):
        if aplicativo.tipo != TIPO_PROGRAMA:
            return Linha(tipo, campos)
        aplicativo_id = aplicativo.id
    else:
        aplicativo_id = aplicativo
    nomes = (
        await c.execute(
            text(
                "SELECT a.nome, coalesce(r.nome, r.sujeito) AS responsavel FROM aplicativos a"
                " JOIN programas pr ON pr.aplicativo_id = a.id JOIN pessoas r ON r.id = pr.responsavel_id"
                " WHERE a.id = :a AND a.tipo = 'programa'"
            ),
            {"a": aplicativo_id},
        )
    ).first()
    if nomes is None:
        return Linha(tipo, campos)
    return Linha(PARA_PROGRAMA[tipo], campos | {"programa": nomes.nome, "responsavel": nomes.responsavel})
