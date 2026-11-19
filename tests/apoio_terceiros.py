"""Ajudas dos testes da Fase 3: cadastrar um servidor de terceiros e gravar a revisao do servidor direto no banco.

Tudo aqui usa a conexao do DONO do esquema (o usuario das migracoes, nos testes) e nao passa pelo despachante nem
pelas telas: serve para preparar o banco dos testes de cada grupo. Os gatilhos da migracao 0006 valem do mesmo jeito:

- a revisao do servidor so publica revisada ATIVA do sistema com a mesma impressao da declarada de mesmo nome;
- a revisada de um terceiro nasce com ``texto_de_fora`` verdadeiro e sem etiquetas do fornecedor;
- a revisao so vale com o sistema ``ativo`` e so passa a valer na transacao que a grava;
- em quarentena, so o despachante (com a versao 2026-07-28 e uma revisao nova) poe o sistema ``ativo``: por isso
  ``gravar_revisao_do_servidor`` troca para o papel do despachante so nesse caso, dentro da mesma transacao.

A conexao ``c`` e uma ``AsyncConnection`` do dono; quem chama faz o commit (``engine.begin()``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.impressao import impressao
from mcp_claro.versao import VERSAO_MCP

FORNECEDOR = "Fornecedor de teste"
REVISOR = "admin-teste"


def definicao(
    nome: str,
    descricao: str = "Texto do fornecedor.",
    *,
    esquema: Mapping[str, Any] | None = None,
    anotacoes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Definicao de ferramenta como um servidor a declara no ``tools/list``."""
    d: dict[str, Any] = {
        "name": nome,
        "description": descricao,
        "inputSchema": dict(esquema or {"type": "object", "properties": {}}),
    }
    if anotacoes is not None:
        d["annotations"] = dict(anotacoes)
    return d


def impressao_do_servidor(declaradas: Iterable[tuple[str, str]]) -> str:
    """Impressao do servidor inteiro: sha256 da lista canonica, ordenada, de ``[nome, impressao]``.

    E a receita de ``cadastro.impressao.impressao_servidor`` (grupo G2). Quando ela existir, vale a de la; a copia
    abaixo so serve ate la, e as duas precisam dar o mesmo valor.
    """
    from mcp_claro.cadastro import impressao as modulo

    pares = [(str(nome), str(imp)) for nome, imp in declaradas]
    receita = getattr(modulo, "impressao_servidor", None)
    if receita is not None:
        return str(receita(pares))
    canonico = json.dumps(sorted([n, i] for n, i in pares), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()


async def cadastrar_terceiro(
    c: AsyncConnection,
    *,
    nome: str | None = None,
    endereco: str = "https://terceiro.exemplo.teste/mcp",
    tipo_chave: str = "por_pessoa",
    situacao: str = "ativo",
    versao_mcp: str | None = VERSAO_MCP,
    autorizacao: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """Cria um servidor de terceiros ja sondado (``ativo`` na 2026-07-28, por padrao), sem revisao do servidor."""
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO sistemas (nome, endereco, origem, tipo_chave, situacao, versao_mcp,"
                        " sondagem_desfecho, sondado_em, autorizacao)"
                        " VALUES (:n, :e, 'terceiro', :t, :s, CAST(:v AS text), :d,"
                        " CASE WHEN CAST(:v AS text) IS NULL THEN NULL ELSE now() END, CAST(:a AS jsonb)) RETURNING id"
                    ),
                    {
                        "n": nome or f"terceiro-{uuid.uuid4().hex[:8]}",
                        "e": endereco,
                        "t": tipo_chave,
                        "s": situacao,
                        "v": versao_mcp,
                        "d": "aceito" if versao_mcp == VERSAO_MCP else None,
                        "a": json.dumps(autorizacao) if autorizacao is not None else None,
                    },
                )
            ).scalar_one()
        )
    )


@dataclass(frozen=True, slots=True)
class Revisada:
    """Uma ferramenta revisada gravada pelos testes."""

    id: uuid.UUID
    sistema_id: uuid.UUID
    nome_original: str
    nome_vitrine: str
    versao: int
    impressao: str
    definicao: dict[str, Any]


async def revisar_ferramenta(
    c: AsyncConnection,
    sistema_id: uuid.UUID,
    definicao_declarada: Mapping[str, Any],
    *,
    nome_vitrine: str | None = None,
    descricao: str = "Descricao escrita pelo ADMIN.",
    classe: str = "le",
    risco: str = "medio",
    campos_pessoais: Sequence[str] = (),
    texto_de_fora: bool = False,
    revisado_por: str = REVISOR,
) -> Revisada:
    """Grava uma nova versao revisada (a ativa anterior vira ``substituida``), como ``repositorio.revisar_ferramenta``.

    Serve para terceiros e proprios. A impressao e a da definicao declarada; ``definicao_declarada`` fica guardada.
    Num terceiro, o banco forca ``texto_de_fora`` e tira as etiquetas, seja qual for o valor dado aqui.
    """
    d = dict(definicao_declarada)
    nome = str(d["name"])
    await c.execute(
        text(
            "UPDATE ferramentas_revisadas SET situacao = 'substituida'"
            " WHERE sistema_id = :s AND nome_original = :n AND situacao = 'ativa'"
        ),
        {"s": sistema_id, "n": nome},
    )
    linha = (
        await c.execute(
            text(
                "INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao,"
                " esquema_entrada, esquema_saida, etiquetas, classe, risco, campos_pessoais, impressao, revisado_por,"
                " texto_de_fora, definicao_declarada)"
                " SELECT CAST(:s AS uuid), CAST(:n AS text), coalesce(max(versao), 0) + 1, CAST(:nv AS text),"
                " CAST(:d AS text), CAST(:ee AS jsonb), CAST(:es AS jsonb), CAST(:et AS jsonb), CAST(:c AS text),"
                " CAST(:r AS text), CAST(:cp AS text[]), CAST(:i AS text), CAST(:rp AS text), CAST(:tf AS boolean),"
                " CAST(:dd AS jsonb) FROM ferramentas_revisadas WHERE sistema_id = :s AND nome_original = :n"
                " RETURNING id, versao"
            ),
            {
                "s": sistema_id,
                "n": nome,
                "nv": nome_vitrine or f"{nome}_{uuid.uuid4().hex[:6]}",
                "d": descricao,
                "ee": json.dumps(d.get("inputSchema") or {"type": "object", "properties": {}}),
                "es": json.dumps(d.get("outputSchema")),
                "et": json.dumps({k: bool(v) for k, v in (d.get("annotations") or {}).items()}),
                "c": classe,
                "r": risco,
                "cp": list(campos_pessoais),
                "i": impressao(d),
                "rp": revisado_por,
                "tf": texto_de_fora,
                "dd": json.dumps(d),
            },
        )
    ).one()
    vitrine = (
        await c.execute(text("SELECT nome_vitrine FROM ferramentas_revisadas WHERE id = :i"), {"i": linha.id})
    ).scalar_one()
    return Revisada(uuid.UUID(str(linha.id)), sistema_id, nome, str(vitrine), int(linha.versao), impressao(d), d)


async def declaradas_revisadas(c: AsyncConnection, sistema_id: uuid.UUID) -> list[dict[str, str]]:
    """``[{nome, impressao}]`` das revisadas ativas do sistema: um servidor que declara hoje o que foi revisado."""
    linhas = await c.execute(
        text(
            "SELECT nome_original, impressao FROM ferramentas_revisadas"
            " WHERE sistema_id = :s AND situacao = 'ativa' ORDER BY nome_original"
        ),
        {"s": sistema_id},
    )
    return [{"nome": str(n), "impressao": str(i)} for n, i in linhas]


async def gravar_revisao_do_servidor(
    c: AsyncConnection,
    sistema_id: uuid.UUID,
    publicar: Sequence[uuid.UUID] | None = None,
    *,
    declaradas: Sequence[Mapping[str, str]] | None = None,
    fornecedor: str = FORNECEDOR,
    revisado_por: str = REVISOR,
    pessoa_id: uuid.UUID | None = None,
    vigente: bool = True,
) -> uuid.UUID:
    """Grava a proxima versao de ``revisoes_servidor`` e, com ``vigente``, a torna a revisao vigente do sistema.

    ``declaradas``: o que o servidor declara agora (padrao: as revisadas ativas, tal como foram revisadas).
    ``publicar``: ids das revisadas na vitrine (padrao: todas as ativas). Sistema em quarentena sai para ``ativo``
    pelo papel do despachante, como a conclusao da revisao faz (a guarda so aceita esse caminho). Com ``vigente``,
    o sistema precisa estar ``ativo`` ou em quarentena: em outra situacao a revisao nao vale (``ValueError``).
    Sem ``vigente``, a revisao fica so gravada e nao passa a valer depois: a guarda so torna vigente a revisao
    gravada na mesma transacao (a conclusao).
    """
    situacao = (await c.execute(text("SELECT situacao FROM sistemas WHERE id = :s"), {"s": sistema_id})).scalar_one()
    if vigente and situacao not in ("ativo", "quarentena"):
        # A guarda apagaria a revisao em silencio: ela so vale com o servidor ativo (regra 4 da 0006).
        raise ValueError(f"A revisao do servidor so fica vigente com ele ativo ou em quarentena, nao {situacao}")
    lista = [dict(d) for d in declaradas] if declaradas is not None else await declaradas_revisadas(c, sistema_id)
    if publicar is None:
        publicar = [
            uuid.UUID(str(i))
            for (i,) in await c.execute(
                text("SELECT id FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa'"),
                {"s": sistema_id},
            )
        ]
    revisao = uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO revisoes_servidor (sistema_id, versao, impressao, declaradas, publicadas,"
                        " fornecedor, revisado_por, pessoa_id)"
                        " SELECT CAST(:s AS uuid), coalesce(max(versao), 0) + 1, CAST(:i AS text), CAST(:d AS jsonb),"
                        " CAST(:p AS uuid[]), CAST(:f AS text), CAST(:r AS text), CAST(:pe AS uuid)"
                        " FROM revisoes_servidor WHERE sistema_id = :s RETURNING id"
                    ),
                    {
                        "s": sistema_id,
                        "i": impressao_do_servidor((d["nome"], d["impressao"]) for d in lista),
                        "d": json.dumps(lista),
                        "p": list(publicar),
                        "f": fornecedor,
                        "r": revisado_por,
                        "pe": pessoa_id,
                    },
                )
            ).scalar_one()
        )
    )
    if not vigente:
        return revisao
    if situacao == "quarentena":
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        # Sem try/finally: se a guarda recusar, o erro dela sobe inteiro (um RESET ROLE na transacao ja abortada o
        # esconderia), e o SET LOCAL acaba com a transacao.
        await c.execute(
            text(
                "UPDATE sistemas SET revisao_servidor_id = :r, situacao = 'ativo', motivo = NULL,"
                " atualizado_em = now() WHERE id = :s"
            ),
            {"r": revisao, "s": sistema_id},
        )
        await c.execute(text("RESET ROLE"))
    else:
        await c.execute(
            text("UPDATE sistemas SET revisao_servidor_id = :r, atualizado_em = now() WHERE id = :s"),
            {"r": revisao, "s": sistema_id},
        )
    return revisao


async def por_em_quarentena(c: AsyncConnection, sistema_id: uuid.UUID, motivo: str = "teste: quarentena") -> None:
    """Poe o sistema em quarentena pelo dono (a guarda apaga a revisao vigente)."""
    await c.execute(
        text("UPDATE sistemas SET situacao = 'quarentena', motivo = :m, atualizado_em = now() WHERE id = :s"),
        {"m": motivo, "s": sistema_id},
    )


async def gravar_sinal(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    tipo: str,
    ferramenta_id: uuid.UUID,
    visto_em: datetime | None = None,
) -> int:
    """Grava um sinal pelo dono, com a hora dada (so o dono grava hora diferente de agora)."""
    return int(
        (
            await c.execute(
                text(
                    "INSERT INTO sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id, visto_em)"
                    " SELECT :p, :a, :t, sistema_id, id, coalesce(CAST(:v AS timestamptz), now())"
                    " FROM ferramentas_revisadas WHERE id = :f RETURNING id"
                ),
                {"p": pessoa_id, "a": aplicativo_id, "t": tipo, "f": ferramenta_id, "v": visto_em},
            )
        ).scalar_one()
    )
