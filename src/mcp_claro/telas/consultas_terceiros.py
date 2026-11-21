"""Consultas das telas do ADMIN para servidores de terceiros (Fase 3), com o usuario do banco da entrada.

So leem: a revisao do servidor vigente e a ultima, as ferramentas declaradas com a revisao de cada uma, os pedidos
que o sistema fez e o MCP-CLARO recusou (contagem de 30 dias e as ultimas linhas, sem texto do servidor) e o que
a ultima quarentena registrou. A ficha do sistema e a revisao de cada ferramenta juntam isto ao que
``telas/consultas.py`` ja traz.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

TIPOS_PEDIDO_BARRADO: Final = (
    "leitura_sistema_pediu_dados",
    "programa_leitura_sistema_pediu_dados",
    "acao_sistema_pediu_dados",
)
"""Linhas do extrato de pedido barrado (o indice ``extrato_pedido_barrado_idx`` da 0006 cobre estas)."""

TIPOS_QUARENTENA: Final = (
    "sistema_em_quarentena",
    "sistema_em_quarentena_sem_resposta",
)
DIAS_PEDIDOS: Final = 30
ULTIMOS_PEDIDOS: Final = 10
DIAS_QUARENTENA: Final = 90
"""Ate quantos dias para tras a ficha procura a linha da ultima quarentena (o extrato e particionado por data)."""

# Uma acao que saiu tambem pode ter tido pedidos barrados no meio (``acao_enviada`` com ``dados.pedidos``); o
# indice nao cobre essa linha, por isso o filtro pelos dados.
_SQL_PEDIDOS: Final = (
    " FROM extrato WHERE sistema_id = :s AND criado_em > now() - make_interval(days => :dias)"
    " AND (tipo = ANY (CAST(:tipos AS text[]))"
    " OR (tipo = 'acao_enviada' AND jsonb_typeof(dados -> 'pedidos') = 'array' AND dados -> 'pedidos' <> '[]'))"
)


@dataclass(frozen=True, slots=True)
class PedidosRecusados:
    """ "Pedidos recusados deste sistema": quantos nos ultimos ``DIAS_PEDIDOS`` dias e as ultimas linhas."""

    contagem: int
    linhas: Sequence[Row[Any]]


async def detalhe(c: AsyncConnection, sistema_id: uuid.UUID) -> Row[Any] | None:
    """O que a ficha de um servidor de terceiros mostra alem de ``consultas.sistema``: desfecho da sondagem, versoes
    anunciadas, rodadas sem resposta, a revisao vigente (``rs_*``) e quantas revisoes ja houve, com o fornecedor da
    ultima (para o campo "Quem fornece este servidor")."""
    return (
        await c.execute(
            text(
                "SELECT s.revisao_servidor_id, s.sondagem_desfecho, s.versoes_anunciadas, s.conferencias_sem_resposta,"
                " rs.versao AS rs_versao, rs.revisado_em AS rs_revisado_em, rs.revisado_por AS rs_revisado_por,"
                " rs.fornecedor AS rs_fornecedor, rs.publicadas AS rs_publicadas,"
                " (SELECT count(*) FROM revisoes_servidor r WHERE r.sistema_id = s.id) AS revisoes,"
                " (SELECT r.fornecedor FROM revisoes_servidor r WHERE r.sistema_id = s.id"
                " ORDER BY r.versao DESC LIMIT 1) AS ultimo_fornecedor"
                " FROM sistemas s LEFT JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id"
                " AND rs.sistema_id = s.id WHERE s.id = :s"
            ),
            {"s": sistema_id},
        )
    ).first()


async def ferramentas(c: AsyncConnection, sistema_id: uuid.UUID) -> Sequence[Row[Any]]:
    """O que o sistema declarou na ultima leitura, com a revisada ativa de cada nome (se houver) e o que foi
    declarado na hora dessa revisao (``definicao_declarada``, para o antes e depois)."""
    return (
        await c.execute(
            text(
                "SELECT v.nome, v.definicao, v.impressao, v.vista_em, f.id AS revisada_id, f.nome_vitrine, f.classe,"
                " f.versao, f.impressao AS impressao_revisada, f.definicao_declarada, f.texto_de_fora"
                " FROM ferramentas_vistas v LEFT JOIN ferramentas_revisadas f ON f.sistema_id = v.sistema_id"
                " AND f.nome_original = v.nome AND f.situacao = 'ativa' WHERE v.sistema_id = :s ORDER BY v.nome"
            ),
            {"s": sistema_id},
        )
    ).all()


async def publicadas(c: AsyncConnection, sistema_id: uuid.UUID) -> Sequence[Row[Any]]:
    """As revisadas publicadas na revisao do servidor vigente (``id``, ``nome_original``, ``situacao`` e
    ``impressao`` de agora: uma revista depois tem outra linha ativa e esta fica ``substituida``)."""
    return (
        await c.execute(
            text(
                "SELECT f.id, f.nome_original, f.situacao, f.impressao FROM sistemas s"
                " JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id AND rs.sistema_id = s.id"
                " JOIN ferramentas_revisadas f ON f.sistema_id = s.id AND f.id = ANY (rs.publicadas)"
                " WHERE s.id = :s ORDER BY f.nome_original"
            ),
            {"s": sistema_id},
        )
    ).all()


async def pedidos_recusados(c: AsyncConnection, sistema_id: uuid.UUID) -> PedidosRecusados:
    """Os pedidos que o sistema fez no meio de uma chamada e o MCP-CLARO recusou: contagem dos ultimos 30 dias e as
    10 ultimas linhas (hora, ferramenta da vitrine, tipo da linha e as categorias do nosso vocabulario). Nada do
    texto, link ou esquema do servidor: o extrato nao guarda isso."""
    parametros = {"s": sistema_id, "dias": DIAS_PEDIDOS, "tipos": list(TIPOS_PEDIDO_BARRADO)}
    contagem = int((await c.execute(text("SELECT count(*)" + _SQL_PEDIDOS), parametros)).scalar_one())
    linhas = (
        await c.execute(
            text(
                "SELECT criado_em, tipo, ferramenta, dados -> 'pedidos' AS pedidos"
                + _SQL_PEDIDOS
                + " ORDER BY criado_em DESC, id DESC LIMIT :n"
            ),
            parametros | {"n": ULTIMOS_PEDIDOS},
        )
    ).all()
    return PedidosRecusados(contagem, linhas)


async def ultima_quarentena(c: AsyncConnection, sistema_id: uuid.UUID) -> Row[Any] | None:
    """A linha do extrato da quarentena mais recente do sistema (nos ultimos ``DIAS_QUARENTENA`` dias), com as
    ferramentas que mudaram (``dados.ferramentas``: nome, mudanca e impressoes; nada do texto da definicao)."""
    return (
        await c.execute(
            text(
                "SELECT criado_em, tipo, dados -> 'ferramentas' AS ferramentas FROM extrato"
                " WHERE sistema_id = :s AND tipo = ANY (CAST(:tipos AS text[]))"
                " AND criado_em > now() - make_interval(days => :dias) ORDER BY criado_em DESC, id DESC LIMIT 1"
            ),
            {"s": sistema_id, "tipos": list(TIPOS_QUARENTENA), "dias": DIAS_QUARENTENA},
        )
    ).first()


async def revisao_ativa(c: AsyncConnection, sistema_id: uuid.UUID, nome: str) -> Row[Any] | None:
    """A revisada ativa de uma ferramenta, com o esquema gravado (as descricoes de campo do ADMIN estao nele), a
    marca de texto de fora e o que o servidor declarou na hora da revisao."""
    return (
        await c.execute(
            text(
                "SELECT id, nome_vitrine, titulo, descricao, classe, risco, campos_pessoais, campos_destinatario,"
                " versao, impressao, esquema_entrada, texto_de_fora, definicao_declarada FROM ferramentas_revisadas"
                " WHERE sistema_id = :s AND nome_original = :n AND situacao = 'ativa'"
            ),
            {"s": sistema_id, "n": nome},
        )
    ).first()


async def ferramentas_para_modelos(c: AsyncConnection) -> Sequence[Row[Any]]:
    """As revisadas ativas, com a origem do sistema e se cada uma pode entrar num modelo: de sistema proprio, sempre;
    de terceiros, so a publicada na revisao do servidor vigente com a definicao declarada guardada (a mesma condicao
    da vitrine, ``cadastro.repositorio._SQL_VITRINE``, sem a chave da Fase 3: com ela desligada, o ADMIN prepara os
    modelos para as pessoas de teste)."""
    return (
        await c.execute(
            text(
                "SELECT f.id, f.sistema_id, f.nome_original, f.nome_vitrine, f.descricao, f.classe, f.risco,"
                " s.nome AS sistema, s.origem,"
                " (s.origem = 'proprio' OR (rs.id IS NOT NULL AND f.id = ANY (rs.publicadas)"
                " AND f.definicao_declarada IS NOT NULL)) AS publicada"
                " FROM ferramentas_revisadas f JOIN sistemas s ON s.id = f.sistema_id"
                " LEFT JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id AND rs.sistema_id = s.id"
                " WHERE f.situacao = 'ativa' ORDER BY s.nome, f.nome_vitrine"
            )
        )
    ).all()
