"""Consultas ao cadastro. SQL explicito; a vitrine sai daqui, pessoa por pessoa."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.impressao import impressao
from mcp_claro.cadastro.modelos import DICAS_CONHECIDAS, Aplicativo, FerramentaVitrine, Pessoa, Sistema
from mcp_claro.entrada.argumentos import conferir_esquema


def _aplicativo(linha: Row[Any]) -> Aplicativo:
    return Aplicativo(linha.id, linha.client_id, linha.nome, linha.familia, linha.tipo, linha.situacao)


def _pessoa(linha: Row[Any]) -> Pessoa:
    return Pessoa(linha.id, linha.emissor, linha.sujeito, linha.nome, linha.papel, linha.situacao, linha.teste)


def _sistema(linha: Row[Any], prefixo: str = "") -> Sistema:
    def g(campo: str) -> Any:
        return getattr(linha, prefixo + campo)

    return Sistema(
        g("id"),
        g("nome"),
        g("endereco"),
        g("origem"),
        g("tipo_chave"),
        g("selo"),
        g("situacao"),
        g("versao_mcp"),
    )


async def aplicativo_por_client_id(c: AsyncConnection, client_id: str) -> Aplicativo | None:
    linha = (await c.execute(text("SELECT * FROM aplicativos WHERE client_id = :c"), {"c": client_id})).first()
    return _aplicativo(linha) if linha else None


async def marcar_versao_vista(c: AsyncConnection, aplicativo_id: uuid.UUID, versao: str) -> None:
    await c.execute(
        text("UPDATE aplicativos SET versao_mcp_vista = :v, visto_em = now() WHERE id = :i"),
        {"v": versao, "i": aplicativo_id},
    )


async def pessoa_no_login(c: AsyncConnection, emissor: str, sujeito: str, nome: str | None) -> Pessoa:
    """Acha a pessoa pelo emissor e sujeito do cracha; cria no primeiro login."""
    linha = (
        await c.execute(
            text(
                "INSERT INTO pessoas (emissor, sujeito, nome) VALUES (:e, :s, :n) "
                "ON CONFLICT (emissor, sujeito) DO UPDATE SET atualizado_em = pessoas.atualizado_em "
                "RETURNING *"
            ),
            {"e": emissor, "s": sujeito, "n": nome},
        )
    ).one()
    return _pessoa(linha)


async def sistema_por_id(c: AsyncConnection, sistema_id: uuid.UUID) -> Sistema | None:
    linha = (await c.execute(text("SELECT * FROM sistemas WHERE id = :i"), {"i": sistema_id})).first()
    return _sistema(linha) if linha else None


_SQL_VITRINE = """
SELECT DISTINCT ON (f.id)
       f.*, o.id AS ordem_id,
       s.id AS s_id, s.nome AS s_nome, s.endereco AS s_endereco, s.origem AS s_origem, s.tipo_chave AS s_tipo_chave,
       s.selo AS s_selo, s.situacao AS s_situacao, s.versao_mcp AS s_versao_mcp
FROM ordens_servico o
JOIN modelos_ordem m ON m.id = o.modelo_id AND m.ativo
JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = m.id
JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id AND f.nome_original = mf.nome_original
                             AND f.situacao = 'ativa'
JOIN sistemas s ON s.id = f.sistema_id AND s.situacao = 'ativo' AND s.versao_mcp = :versao
WHERE o.pessoa_id = :pessoa AND o.aplicativo_id = :aplicativo AND o.situacao = 'ativa'
  AND o.inicio <= now() AND o.fim > now()
"""


def _ferramenta(linha: Row[Any]) -> FerramentaVitrine:
    return FerramentaVitrine(
        id=linha.id,
        sistema=_sistema(linha, "s_"),
        nome_original=linha.nome_original,
        versao=linha.versao,
        nome_vitrine=linha.nome_vitrine,
        titulo=linha.titulo,
        descricao=linha.descricao,
        esquema_entrada=dict(linha.esquema_entrada),
        esquema_saida=dict(linha.esquema_saida) if linha.esquema_saida else None,
        etiquetas=dict(linha.etiquetas or {}),
        classe=linha.classe,
        risco=linha.risco,
        campos_pessoais=tuple(linha.campos_pessoais or ()),
        ordem_id=linha.ordem_id,
    )


async def vitrine(
    c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, versao: str
) -> list[FerramentaVitrine]:
    linhas = (
        await c.execute(
            text(_SQL_VITRINE + " ORDER BY f.id, o.fim DESC"),
            {"pessoa": pessoa_id, "aplicativo": aplicativo_id, "versao": versao},
        )
    ).all()
    return sorted((_ferramenta(linha) for linha in linhas), key=lambda f: f.nome_vitrine)


async def ferramenta_na_vitrine(
    c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, nome_vitrine: str, versao: str
) -> FerramentaVitrine | None:
    linha = (
        await c.execute(
            text(_SQL_VITRINE + " AND f.nome_vitrine = :nome ORDER BY f.id, o.fim DESC"),
            {"pessoa": pessoa_id, "aplicativo": aplicativo_id, "versao": versao, "nome": nome_vitrine},
        )
    ).first()
    return _ferramenta(linha) if linha else None


async def ferramenta_da_ordem(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    ordem_id: uuid.UUID,
    ferramenta_id: uuid.UUID,
    versao: str,
) -> FerramentaVitrine | None:
    """Reconferencia do despachante: a mesma regra da vitrine, presa a ordem e a versao revisada vigente."""
    linha = (
        await c.execute(
            text(_SQL_VITRINE + " AND f.id = :f AND o.id = :o ORDER BY f.id, o.fim DESC"),
            {"pessoa": pessoa_id, "aplicativo": aplicativo_id, "versao": versao, "f": ferramenta_id, "o": ordem_id},
        )
    ).first()
    return _ferramenta(linha) if linha else None


async def esquema_declarado(c: AsyncConnection, sistema_id: uuid.UUID, nome: str) -> dict[str, Any] | None:
    """Esquema de entrada que o sistema declarou por ultimo (para os cabecalhos Mcp-Param-* de saida)."""
    valor = (
        await c.execute(
            text("SELECT definicao -> 'inputSchema' FROM ferramentas_vistas WHERE sistema_id = :s AND nome = :n"),
            {"s": sistema_id, "n": nome},
        )
    ).scalar_one_or_none()
    return dict(valor) if isinstance(valor, dict) else None


async def ferramenta_por_id(c: AsyncConnection, ferramenta_id: uuid.UUID) -> FerramentaVitrine | None:
    linha = (
        await c.execute(
            text(
                "SELECT f.*, NULL::uuid AS ordem_id, s.id AS s_id, s.nome AS s_nome, s.endereco AS s_endereco,"
                " s.origem AS s_origem, s.tipo_chave AS s_tipo_chave, s.selo AS s_selo, s.situacao AS s_situacao,"
                " s.versao_mcp AS s_versao_mcp"
                " FROM ferramentas_revisadas f JOIN sistemas s ON s.id = f.sistema_id WHERE f.id = :i"
            ),
            {"i": ferramenta_id},
        )
    ).first()
    return _ferramenta(linha) if linha else None


async def ordem_valida(c: AsyncConnection, ordem_id: uuid.UUID, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID) -> bool:
    valor = (
        await c.execute(
            text(
                "SELECT 1 FROM ordens_servico WHERE id = :o AND pessoa_id = :p AND aplicativo_id = :a"
                " AND situacao = 'ativa' AND inicio <= now() AND fim > now()"
            ),
            {"o": ordem_id, "p": pessoa_id, "a": aplicativo_id},
        )
    ).first()
    return valor is not None


async def marcar_uso_da_ordem(c: AsyncConnection, ordem_id: uuid.UUID) -> None:
    await c.execute(text("UPDATE ordens_servico SET ultimo_uso = now() WHERE id = :o"), {"o": ordem_id})


# --- Operacoes do ADMIN (telas e testes). Cada uma deixa linha no extrato pelo chamador. ---


async def habilitar_aplicativo(
    c: AsyncConnection, *, client_id: str, nome: str, familia: str, tipo: str = "assistente"
) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO aplicativos (client_id, nome, familia, tipo) VALUES (:c, :n, :f, :t) "
                        "ON CONFLICT (client_id) DO UPDATE SET nome = EXCLUDED.nome, atualizado_em = now() "
                        "RETURNING id"
                    ),
                    {"c": client_id, "n": nome, "f": familia, "t": tipo},
                )
            ).scalar_one()
        )
    )


async def cadastrar_sistema(
    c: AsyncConnection, *, nome: str, endereco: str, tipo_chave: str, origem: str = "proprio"
) -> uuid.UUID:
    """Cria o sistema pendente. So vira ativo depois da sondagem de versao pelo despachante."""
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO sistemas (nome, endereco, tipo_chave, origem) VALUES (:n, :e, :t, :o) RETURNING id"
                    ),
                    {"n": nome, "e": endereco, "t": tipo_chave, "o": origem},
                )
            ).scalar_one()
        )
    )


async def revisar_ferramenta(
    c: AsyncConnection,
    *,
    sistema_id: uuid.UUID,
    definicao_declarada: dict[str, Any],
    nome_vitrine: str,
    descricao: str,
    classe: str,
    revisado_por: str,
    risco: str = "medio",
    titulo: str | None = None,
    esquema_entrada: dict[str, Any] | None = None,
    campos_pessoais: list[str] | None = None,
) -> uuid.UUID:
    """Grava nova versao revisada. A anterior vira 'substituida'. O texto e do cadastro, nao do fornecedor."""
    esquema = esquema_entrada or dict(definicao_declarada.get("inputSchema") or {})
    conferir_esquema(esquema)
    nome = str(definicao_declarada["name"])
    await c.execute(
        text(
            "UPDATE ferramentas_revisadas SET situacao = 'substituida' "
            "WHERE sistema_id = :s AND nome_original = :n AND situacao = 'ativa'"
        ),
        {"s": sistema_id, "n": nome},
    )
    versao = (
        await c.execute(
            text(
                "SELECT coalesce(max(versao), 0) + 1 FROM ferramentas_revisadas"
                " WHERE sistema_id = :s AND nome_original = :n"
            ),
            {"s": sistema_id, "n": nome},
        )
    ).scalar_one()
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, titulo,"
                        " descricao, esquema_entrada, esquema_saida, etiquetas, classe, risco, campos_pessoais,"
                        " impressao, revisado_por) VALUES (:s, :n, :v, :nv, :t, :d, CAST(:ee AS jsonb),"
                        " CAST(:es AS jsonb), CAST(:et AS jsonb), :c, :r, :cp, :i, :rp) RETURNING id"
                    ),
                    {
                        "s": sistema_id,
                        "n": nome,
                        "v": versao,
                        "nv": nome_vitrine,
                        "t": titulo,
                        "d": descricao,
                        "ee": json.dumps(esquema),
                        "es": json.dumps(definicao_declarada.get("outputSchema")),
                        "et": json.dumps(
                            {
                                k: bool(v)
                                for k, v in (definicao_declarada.get("annotations") or {}).items()
                                if k in DICAS_CONHECIDAS
                            }
                        ),
                        "c": classe,
                        "r": risco,
                        "cp": campos_pessoais or [],
                        "i": impressao(definicao_declarada),
                        "rp": revisado_por,
                    },
                )
            ).scalar_one()
        )
    )


async def criar_modelo_ordem(
    c: AsyncConnection,
    *,
    nome: str,
    descricao: str,
    ferramentas: list[tuple[uuid.UUID, str]],
    validade_dias: int,
    criado_por: str,
) -> uuid.UUID:
    modelo_id = uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO modelos_ordem (nome, descricao, validade_dias, criado_por) "
                        "VALUES (:n, :d, :v, :c) RETURNING id"
                    ),
                    {"n": nome, "d": descricao, "v": validade_dias, "c": criado_por},
                )
            ).scalar_one()
        )
    )
    for sistema_id, nome_original in ferramentas:
        await c.execute(
            text("INSERT INTO modelos_ordem_ferramentas VALUES (:m, :s, :n)"),
            {"m": modelo_id, "s": sistema_id, "n": nome_original},
        )
    return modelo_id


async def conceder_ordem(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    modelo_id: uuid.UUID,
    dias: int,
    max_dias: int,
) -> uuid.UUID:
    """Concede uma ordem de servico com prazo que vence sozinho, limitado por WORK_ORDER_MAX_DAYS."""
    fim = datetime.now(UTC) + timedelta(days=min(dias, max_dias))
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO ordens_servico (pessoa_id, aplicativo_id, modelo_id, fim) "
                        "VALUES (:p, :a, :m, :f) RETURNING id"
                    ),
                    {"p": pessoa_id, "a": aplicativo_id, "m": modelo_id, "f": fim},
                )
            ).scalar_one()
        )
    )
