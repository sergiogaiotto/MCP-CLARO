"""Consultas e mudancas das telas. SQL explicito; quem chama poe a linha no extrato na mesma transacao."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.modelos import Pessoa
from mcp_claro.config.politica import POLITICA, valor_inicial

TAMANHO_PAGINA = 25


async def valor_politica(c: AsyncConnection, chave: str) -> int:
    """Vale a linha mais nova da tabela de politica; sem linha, o valor inicial."""
    valor = (
        await c.execute(text("SELECT valor FROM politica_vigente WHERE chave = :c"), {"c": chave})
    ).scalar_one_or_none()
    if valor is None:
        return valor_inicial(chave)
    return POLITICA[chave].conferir(int(valor))


# --- Ordens de servico da pessoa ---


@dataclass
class ModeloComFerramentas:
    id: uuid.UUID
    nome: str
    descricao: str
    validade_dias: int
    ferramentas: list[Row[Any]] = field(default_factory=list)


async def aplicativos_habilitados(c: AsyncConnection) -> Sequence[Row[Any]]:
    """Aplicativos de IA que uma pessoa pode escolher (programas tem cadastro proprio)."""
    return (
        await c.execute(
            text(
                "SELECT id, client_id, nome, familia FROM aplicativos"
                " WHERE situacao = 'ativo' AND tipo = 'assistente' ORDER BY nome"
            )
        )
    ).all()


async def _ferramentas_dos_modelos(c: AsyncConnection, ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Row[Any]]]:
    if not ids:
        return {}
    linhas = (
        await c.execute(
            text(
                "SELECT mf.modelo_id, mf.nome_original, s.nome AS sistema, f.nome_vitrine, f.descricao, f.classe"
                " FROM modelos_ordem_ferramentas mf JOIN sistemas s ON s.id = mf.sistema_id"
                " LEFT JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id"
                " AND f.nome_original = mf.nome_original AND f.situacao = 'ativa'"
                " WHERE mf.modelo_id = ANY(:ids) ORDER BY s.nome, f.nome_vitrine NULLS LAST, mf.nome_original"
            ),
            {"ids": ids},
        )
    ).all()
    por_modelo: dict[uuid.UUID, list[Row[Any]]] = defaultdict(list)
    for linha in linhas:
        por_modelo[linha.modelo_id].append(linha)
    return por_modelo


async def modelos_ativos(c: AsyncConnection) -> list[ModeloComFerramentas]:
    linhas = (
        await c.execute(text("SELECT id, nome, descricao, validade_dias FROM modelos_ordem WHERE ativo ORDER BY nome"))
    ).all()
    modelos = [ModeloComFerramentas(m.id, m.nome, m.descricao, m.validade_dias) for m in linhas]
    ferramentas = await _ferramentas_dos_modelos(c, [m.id for m in modelos])
    for m in modelos:
        m.ferramentas = ferramentas.get(m.id, [])
    return modelos


@dataclass
class OrdemDaPessoa:
    id: uuid.UUID
    aplicativo: str
    modelo: str
    descricao: str
    inicio: Any
    fim: Any
    situacao: str
    ultimo_uso: Any
    ferramentas: list[Row[Any]] = field(default_factory=list)


async def ordens_da_pessoa(c: AsyncConnection, pessoa_id: uuid.UUID) -> tuple[list[OrdemDaPessoa], list[OrdemDaPessoa]]:
    """(ordens que valem agora, as dez ultimas que terminaram)."""
    linhas = (
        await c.execute(
            text(
                "SELECT o.id, o.modelo_id, a.nome AS aplicativo, m.nome AS modelo, m.descricao, o.inicio, o.fim,"
                " CASE WHEN o.situacao = 'ativa' AND o.fim <= now() THEN 'vencida' ELSE o.situacao END AS situacao,"
                " o.ultimo_uso FROM ordens_servico o JOIN aplicativos a ON a.id = o.aplicativo_id"
                " JOIN modelos_ordem m ON m.id = o.modelo_id WHERE o.pessoa_id = :p"
                " ORDER BY o.fim DESC LIMIT 200"
            ),
            {"p": pessoa_id},
        )
    ).all()
    ferramentas = await _ferramentas_dos_modelos(c, list({linha.modelo_id for linha in linhas}))
    ativas: list[OrdemDaPessoa] = []
    terminadas: list[OrdemDaPessoa] = []
    for linha in linhas:
        ordem = OrdemDaPessoa(
            linha.id,
            linha.aplicativo,
            linha.modelo,
            linha.descricao,
            linha.inicio,
            linha.fim,
            linha.situacao,
            linha.ultimo_uso,
            ferramentas.get(linha.modelo_id, []),
        )
        (ativas if ordem.situacao == "ativa" else terminadas).append(ordem)
    return ativas, terminadas[:10]


async def ordem_ativa_igual(
    c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, modelo_id: uuid.UUID
) -> bool:
    linha = (
        await c.execute(
            text(
                "SELECT 1 FROM ordens_servico WHERE pessoa_id = :p AND aplicativo_id = :a AND modelo_id = :m"
                " AND situacao = 'ativa' AND fim > now()"
            ),
            {"p": pessoa_id, "a": aplicativo_id, "m": modelo_id},
        )
    ).first()
    return linha is not None


async def fim_da_ordem(c: AsyncConnection, ordem_id: uuid.UUID) -> Any:
    return (await c.execute(text("SELECT fim FROM ordens_servico WHERE id = :o"), {"o": ordem_id})).scalar_one()


async def revogar_ordem(c: AsyncConnection, pessoa_id: uuid.UUID, ordem_id: uuid.UUID) -> Row[Any] | None:
    """Revoga so uma ordem ativa da propria pessoa. Devolve o que o extrato precisa, ou None."""
    return (
        await c.execute(
            text(
                "UPDATE ordens_servico o SET situacao = 'revogada' FROM aplicativos a, modelos_ordem m"
                " WHERE o.id = :o AND o.pessoa_id = :p AND o.situacao = 'ativa'"
                " AND a.id = o.aplicativo_id AND m.id = o.modelo_id"
                " RETURNING o.id, o.aplicativo_id, a.nome AS aplicativo, m.nome AS modelo"
            ),
            {"o": ordem_id, "p": pessoa_id},
        )
    ).first()


# --- Extrato ---


async def linhas_extrato(
    c: AsyncConnection, pessoa_id: uuid.UUID, pagina: int, tamanho: int = TAMANHO_PAGINA
) -> tuple[Sequence[Row[Any]], bool]:
    """Linhas do extrato da pessoa, mais novas primeiro. Devolve (linhas, ha_mais_antigas)."""
    linhas = (
        await c.execute(
            text(
                "SELECT e.id, e.criado_em, e.tipo, e.frase, e.protocolo, e.ferramenta, a.nome AS aplicativo,"
                " s.nome AS sistema FROM extrato e LEFT JOIN aplicativos a ON a.id = e.aplicativo_id"
                " LEFT JOIN sistemas s ON s.id = e.sistema_id WHERE e.pessoa_id = :p"
                " ORDER BY e.criado_em DESC, e.id DESC LIMIT :n OFFSET :o"
            ),
            {"p": pessoa_id, "n": tamanho + 1, "o": (pagina - 1) * tamanho},
        )
    ).all()
    return linhas[:tamanho], len(linhas) > tamanho


async def acesso_alheio_recente(
    c: AsyncConnection, alvo_id: uuid.UUID, admin_id: uuid.UUID, janela_minutos: int
) -> bool:
    """O ADMIN deu o motivo ha pouco? A prova e a propria linha do extrato da pessoa."""
    linha = (
        await c.execute(
            text(
                "SELECT 1 FROM extrato WHERE pessoa_id = :alvo AND tipo = 'extrato_alheio_aberto'"
                " AND dados->>'admin_id' = :admin AND criado_em > now() - make_interval(mins => :j) LIMIT 1"
            ),
            {"alvo": alvo_id, "admin": str(admin_id), "j": janela_minutos},
        )
    ).first()
    return linha is not None


async def pessoa_por_id(c: AsyncConnection, pessoa_id: uuid.UUID) -> Pessoa | None:
    linha = (await c.execute(text("SELECT * FROM pessoas WHERE id = :i"), {"i": pessoa_id})).first()
    if linha is None:
        return None
    return Pessoa(linha.id, linha.emissor, linha.sujeito, linha.nome, linha.papel, linha.situacao, linha.teste)


async def pessoas(c: AsyncConnection, *, so_ativas_com_nome: bool = False) -> Sequence[Row[Any]]:
    """Pessoas para escolher numa lista. Com ``so_ativas_com_nome``, so quem pode ser responsavel."""
    if so_ativas_com_nome:
        sql = (
            "SELECT id, nome, sujeito, papel, situacao, teste, criado_em FROM pessoas"
            " WHERE situacao = 'ativa' AND coalesce(btrim(nome), '') <> '' ORDER BY nome, criado_em LIMIT 2000"
        )
    else:
        sql = (
            "SELECT id, nome, sujeito, papel, situacao, teste, criado_em FROM pessoas"
            " ORDER BY nome NULLS LAST, criado_em LIMIT 2000"
        )
    return (await c.execute(text(sql))).all()


# --- Cadastro do ADMIN ---


async def todos_aplicativos(c: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(
                "SELECT id, client_id, nome, familia, tipo, situacao, versao_mcp_vista, visto_em, criado_em"
                " FROM aplicativos ORDER BY tipo, nome"
            )
        )
    ).all()


async def aplicativo_existe(c: AsyncConnection, client_id: str) -> bool:
    linha = (await c.execute(text("SELECT 1 FROM aplicativos WHERE client_id = :c"), {"c": client_id})).first()
    return linha is not None


async def todos_sistemas(c: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(
                "SELECT id, nome, endereco, origem, tipo_chave, situacao, versao_mcp, motivo, sondado_em, criado_em"
                " FROM sistemas ORDER BY nome"
            )
        )
    ).all()


async def sistema(c: AsyncConnection, sistema_id: uuid.UUID) -> Row[Any] | None:
    return (
        await c.execute(
            text(
                "SELECT id, nome, endereco, origem, tipo_chave, situacao, versao_mcp, motivo, sondado_em, criado_em"
                " FROM sistemas WHERE id = :i"
            ),
            {"i": sistema_id},
        )
    ).first()


async def ferramentas_vistas(c: AsyncConnection, sistema_id: uuid.UUID) -> Sequence[Row[Any]]:
    """O que o sistema declarou, com a revisao ativa (se houver) e se mudou desde ela."""
    return (
        await c.execute(
            text(
                "SELECT v.nome, v.definicao, v.impressao, v.vista_em, f.id AS revisada_id, f.nome_vitrine,"
                " f.classe, f.risco, f.versao, f.impressao AS impressao_revisada"
                " FROM ferramentas_vistas v LEFT JOIN ferramentas_revisadas f ON f.sistema_id = v.sistema_id"
                " AND f.nome_original = v.nome AND f.situacao = 'ativa' WHERE v.sistema_id = :s ORDER BY v.nome"
            ),
            {"s": sistema_id},
        )
    ).all()


async def ferramenta_vista(c: AsyncConnection, sistema_id: uuid.UUID, nome: str) -> Row[Any] | None:
    return (
        await c.execute(
            text(
                "SELECT nome, definicao, impressao, vista_em FROM ferramentas_vistas"
                " WHERE sistema_id = :s AND nome = :n"
            ),
            {"s": sistema_id, "n": nome},
        )
    ).first()


async def revisao_ativa(c: AsyncConnection, sistema_id: uuid.UUID, nome: str) -> Row[Any] | None:
    return (
        await c.execute(
            text(
                "SELECT id, nome_vitrine, titulo, descricao, classe, risco, campos_pessoais, versao, impressao"
                " FROM ferramentas_revisadas WHERE sistema_id = :s AND nome_original = :n AND situacao = 'ativa'"
            ),
            {"s": sistema_id, "n": nome},
        )
    ).first()


async def ferramentas_revisadas_ativas(c: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(
                "SELECT f.id, f.sistema_id, f.nome_original, f.nome_vitrine, f.descricao, f.classe, f.risco,"
                " s.nome AS sistema FROM ferramentas_revisadas f JOIN sistemas s ON s.id = f.sistema_id"
                " WHERE f.situacao = 'ativa' ORDER BY s.nome, f.nome_vitrine"
            )
        )
    ).all()


async def todos_modelos(c: AsyncConnection) -> list[ModeloComFerramentas]:
    linhas = (
        await c.execute(text("SELECT id, nome, descricao, validade_dias, ativo FROM modelos_ordem ORDER BY nome"))
    ).all()
    modelos = [ModeloComFerramentas(m.id, m.nome, m.descricao, m.validade_dias) for m in linhas]
    ferramentas = await _ferramentas_dos_modelos(c, [m.id for m in modelos])
    for m in modelos:
        m.ferramentas = ferramentas.get(m.id, [])
    return modelos


async def modelo_existe(c: AsyncConnection, nome: str) -> bool:
    return (await c.execute(text("SELECT 1 FROM modelos_ordem WHERE nome = :n"), {"n": nome})).first() is not None


async def todos_programas(c: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(
                "SELECT a.client_id, a.nome, a.situacao AS situacao_aplicativo, pr.finalidade, pr.fim, pr.situacao,"
                " r.nome AS responsavel, s.nome AS substituto FROM programas pr"
                " JOIN aplicativos a ON a.id = pr.aplicativo_id JOIN pessoas r ON r.id = pr.responsavel_id"
                " JOIN pessoas s ON s.id = pr.substituto_id ORDER BY a.nome"
            )
        )
    ).all()


async def cadastrar_programa(
    c: AsyncConnection,
    *,
    aplicativo_id: uuid.UUID,
    responsavel_id: uuid.UUID,
    substituto_id: uuid.UUID,
    finalidade: str,
    fim: Any,
) -> None:
    await c.execute(
        text(
            "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim)"
            " VALUES (:a, :r, :s, :f, :d)"
        ),
        {"a": aplicativo_id, "r": responsavel_id, "s": substituto_id, "f": finalidade, "d": fim},
    )


async def destinos_webhook(c: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text("SELECT id, nome, url, eventos, ativo, criado_por, criado_em FROM destinos_webhook ORDER BY nome")
        )
    ).all()


async def cadastrar_destino(
    c: AsyncConnection, *, nome: str, url: str, eventos: list[str], criado_por: str
) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO destinos_webhook (nome, url, eventos, criado_por) VALUES (:n, :u, :e, :c)"
                        " RETURNING id"
                    ),
                    {"n": nome, "u": url, "e": eventos, "c": criado_por},
                )
            ).scalar_one()
        )
    )


async def mudar_destino(c: AsyncConnection, destino_id: uuid.UUID, ativo: bool) -> str | None:
    return (
        await c.execute(
            text("UPDATE destinos_webhook SET ativo = :a WHERE id = :i AND ativo <> :a RETURNING nome"),
            {"a": ativo, "i": destino_id},
        )
    ).scalar_one_or_none()
