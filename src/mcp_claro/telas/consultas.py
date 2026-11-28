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
from mcp_claro.selo import contrato

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
    """As ferramentas de cada modelo, com a origem do sistema (a tela marca "de terceiros")."""
    if not ids:
        return {}
    linhas = (
        await c.execute(
            text(
                "SELECT mf.modelo_id, mf.nome_original, s.nome AS sistema, s.origem, f.nome_vitrine, f.descricao,"
                " f.classe FROM modelos_ordem_ferramentas mf JOIN sistemas s ON s.id = mf.sistema_id"
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
    """Linhas do extrato da pessoa, mais novas primeiro. Devolve (linhas, ha_mais_antigas).

    Com a origem do sistema de cada linha (``sistema_origem``): a tela marca "de terceiros" e da a dica fixa.

    Fase 4 (desenho 7.1 e D25): a coluna "Selo" sai de ``dados.selo`` da PROPRIA linha (o retrato da hora do passo),
    nunca do selo de agora do sistema nem de ``sistemas.selo``: ``selo_nivel`` e o nivel do retrato (so quando e um
    numero no JSON). Critica M3: nivel 2 ou 3 so vale com o teste do retrato aprovado em ``testes_selo``, do mesmo
    sistema da linha (``selo_teste_nivel``, o ``nivel_obtido`` dele, e ``selo_testado_em``, a data do banco); um
    ``teste_id`` fora da forma de uuid nao acha teste (nada e convertido sem conferir a forma). Os filtros
    ``selo_da_linha`` e ``informado_pela_linha`` de ``paginas.criar_ambiente`` montam o texto."""
    linhas = (
        await c.execute(
            text(
                "SELECT e.id, e.criado_em, e.tipo, e.frase, e.protocolo, e.ferramenta, a.nome AS aplicativo,"
                " s.nome AS sistema, s.origem AS sistema_origem,"
                " CASE e.dados #> '{selo,nivel}' WHEN '1'::jsonb THEN 1 WHEN '2'::jsonb THEN 2"
                " WHEN '3'::jsonb THEN 3 END AS selo_nivel,"
                " t.nivel_obtido AS selo_teste_nivel, t.testado_em AS selo_testado_em FROM extrato e"
                " LEFT JOIN aplicativos a ON a.id = e.aplicativo_id"
                " LEFT JOIN sistemas s ON s.id = e.sistema_id"
                " LEFT JOIN testes_selo t ON t.id = CASE WHEN (e.dados #>> '{selo,teste_id}') ~ :forma"
                " THEN CAST(e.dados #>> '{selo,teste_id}' AS uuid) END"
                " AND t.sistema_id = e.sistema_id AND t.resultado = 'aprovado'"
                " WHERE e.pessoa_id = :p"
                " ORDER BY e.criado_em DESC, e.id DESC LIMIT :n OFFSET :o"
            ),
            {"p": pessoa_id, "n": tamanho + 1, "o": (pagina - 1) * tamanho, "forma": contrato.PADRAO_UUID},
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


async def pessoas(c: AsyncConnection) -> Sequence[Row[Any]]:
    """Pessoas para escolher numa lista (pessoas do ADMIN, extrato de outra pessoa)."""
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


async def aplicativos_pausados(c: AsyncConnection) -> Sequence[Row[Any]]:
    """Aplicativos pausados que ainda podem voltar (``aplicativos.situacao``; hoje so a conferencia do banco na
    partida pausa assim). O de um programa encerrado fica de fora: nao ha o que reativar (se o programa for
    cadastrado de novo, ele volta a aparecer)."""
    return (
        await c.execute(
            text(
                "SELECT a.id, a.nome, a.client_id, a.tipo FROM aplicativos a WHERE a.situacao = 'pausado'"
                " AND NOT EXISTS (SELECT 1 FROM programas pr WHERE pr.aplicativo_id = a.id"
                " AND pr.situacao = 'encerrado') ORDER BY a.tipo, a.nome"
            )
        )
    ).all()


async def intervalo_que_pode_faltar(c: AsyncConnection) -> Row[Any] | None:
    """Entre quando e quando pode faltar algo no extrato, para os aplicativos ainda pausados (menos o de programa
    encerrado): junta as linhas ``intervalo_perdido`` da conferencia do banco desde a pausa mais antiga que ainda
    vale (a linha ``aplicativo_pausado_continuidade`` mais nova de cada um; a do intervalo e da mesma transacao) e
    as das partidas seguintes que tambem nao conferiram. ``None``: sem a linha.

    As duas buscas passam pelo indice parcial ``extrato_saida_cofre_idx`` (migracao 0005), que cobre esses tipos:
    sao linhas raras, e a tela nao percorre o resto do extrato."""
    linha = (
        await c.execute(
            text(
                "WITH pausa AS ("
                " SELECT e.criado_em, e.xid FROM aplicativos a"
                " CROSS JOIN LATERAL (SELECT criado_em, xid FROM extrato"
                "   WHERE tipo = 'aplicativo_pausado_continuidade' AND aplicativo_id = a.id"
                "   ORDER BY criado_em DESC, id DESC LIMIT 1) e"
                " WHERE a.situacao = 'pausado' AND NOT EXISTS (SELECT 1 FROM programas pr"
                "   WHERE pr.aplicativo_id = a.id AND pr.situacao = 'encerrado')"
                " ORDER BY e.criado_em LIMIT 1)"
                " SELECT min((x.dados->>'desde')::timestamptz) AS desde, max((x.dados->>'ate')::timestamptz) AS ate"
                " FROM extrato x, pausa"
                " WHERE x.tipo = 'intervalo_perdido' AND x.pessoa_id IS NULL"
                " AND (x.criado_em >= pausa.criado_em OR x.xid = pausa.xid)"
                " AND x.criado_em >= pausa.criado_em - interval '1 hour'"
            )
        )
    ).first()
    return linha if linha is not None and linha.desde is not None else None


async def reativar_assistente(c: AsyncConnection, aplicativo_id: uuid.UUID) -> Row[Any] | None:
    """Volta a 'ativo' um aplicativo assistente pausado. So vale se ele ainda estiver pausado e for assistente:
    dois cliques ao mesmo tempo mudam uma vez so, e um programa volta pela tela do programa."""
    return (
        await c.execute(
            text(
                "UPDATE aplicativos SET situacao = 'ativo', atualizado_em = now()"
                " WHERE id = :a AND tipo = 'assistente' AND situacao = 'pausado' RETURNING id, nome, client_id"
            ),
            {"a": aplicativo_id},
        )
    ).first()


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
                "SELECT id, nome_vitrine, titulo, descricao, classe, risco, campos_pessoais, campos_destinatario,"
                " versao, impressao FROM ferramentas_revisadas"
                " WHERE sistema_id = :s AND nome_original = :n AND situacao = 'ativa'"
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


async def cuida_de_programa(c: AsyncConnection, pessoa_id: uuid.UUID) -> bool:
    """A pessoa e responsavel ou substituta de algum programa que nao foi encerrado (o inicio mostra o link)."""
    linha = (
        await c.execute(
            text(
                "SELECT 1 FROM programas WHERE (responsavel_id = :p OR substituto_id = :p)"
                " AND situacao <> 'encerrado' LIMIT 1"
            ),
            {"p": pessoa_id},
        )
    ).first()
    return linha is not None


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


# --- Bloqueio de um toque e pausa ---

_SQL_BLOQUEIOS = (
    "SELECT b.id, b.tipo, b.alcance, b.pessoa_id, b.aplicativo_id, b.sistema_id, b.quem, b.criado_em,"
    " b.encerrado_em, b.encerrado_por, a.nome AS aplicativo, s.nome AS sistema, p.nome AS pessoa"
    " FROM bloqueios b LEFT JOIN aplicativos a ON a.id = b.aplicativo_id"
    " LEFT JOIN sistemas s ON s.id = b.sistema_id LEFT JOIN pessoas p ON p.id = b.pessoa_id"
)


async def aplicativos_para_parar(c: AsyncConnection, pessoa_id: uuid.UUID) -> Sequence[Row[Any]]:
    """Aplicativos de IA que a pessoa pode bloquear ou pausar; os que tem ordem dela valendo vem primeiro."""
    return (
        await c.execute(
            text(
                "SELECT a.id, a.nome, a.situacao, EXISTS (SELECT 1 FROM ordens_servico o WHERE o.aplicativo_id = a.id"
                " AND o.pessoa_id = :p AND o.situacao = 'ativa' AND o.fim > now()) AS com_ordem"
                " FROM aplicativos a WHERE a.tipo = 'assistente' AND a.situacao IN ('ativo', 'pausado')"
                " ORDER BY com_ordem DESC, a.nome"
            ),
            {"p": pessoa_id},
        )
    ).all()


async def bloqueios_que_tocam(c: AsyncConnection, pessoa_id: uuid.UUID) -> Sequence[Row[Any]]:
    """Bloqueios e pausas ativos da pessoa e os gerais (aplicativo, sistema, tudo), mais novos primeiro."""
    return (
        await c.execute(
            text(
                _SQL_BLOQUEIOS + " WHERE b.encerrado_em IS NULL AND (b.pessoa_id = :p"
                " OR b.alcance IN ('aplicativo', 'sistema', 'tudo')) ORDER BY b.criado_em DESC, b.id LIMIT 200"
            ),
            {"p": pessoa_id},
        )
    ).all()


async def bloqueio_com_nomes(c: AsyncConnection, bloqueio_id: uuid.UUID) -> Row[Any] | None:
    return (await c.execute(text(_SQL_BLOQUEIOS + " WHERE b.id = :i"), {"i": bloqueio_id})).first()


async def bloqueio_igual_ativo(
    c: AsyncConnection,
    tipo: str,
    alcance: str,
    *,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    sistema_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Toque repetido: o mesmo bloqueio (ou pausa) ja ativo, para nao abrir outro igual."""
    valor = (
        await c.execute(
            text(
                "SELECT id FROM bloqueios WHERE encerrado_em IS NULL AND tipo = :t AND alcance = :al"
                " AND pessoa_id IS NOT DISTINCT FROM CAST(:p AS uuid)"
                " AND aplicativo_id IS NOT DISTINCT FROM CAST(:a AS uuid)"
                " AND sistema_id IS NOT DISTINCT FROM CAST(:s AS uuid) ORDER BY criado_em LIMIT 1"
            ),
            {"t": tipo, "al": alcance, "p": pessoa_id, "a": aplicativo_id, "s": sistema_id},
        )
    ).scalar_one_or_none()
    return uuid.UUID(str(valor)) if valor is not None else None


async def todos_bloqueios_ativos(c: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await c.execute(text(_SQL_BLOQUEIOS + " WHERE b.encerrado_em IS NULL ORDER BY b.criado_em DESC LIMIT 500"))
    ).all()


async def estado_das_portas(c: AsyncConnection) -> tuple[Row[Any] | None, Row[Any] | None]:
    entrada = (await c.execute(text("SELECT nivel, motivo, mudou_em, mudou_por FROM estado_entrada"))).first()
    saida = (await c.execute(text("SELECT aberta, motivo, mudou_em, mudou_por FROM estado_saida"))).first()
    return entrada, saida


# --- Autorizar sistema (pessoa) ---


async def sistemas_para_autorizar(
    c: AsyncConnection, pessoa_id: uuid.UUID, sistema_id: uuid.UUID | None = None
) -> Sequence[Row[Any]]:
    """Sistemas de chave por pessoa que entram nas ordens valendo da pessoa, com a ultima autorizacao feita e a origem
    (o endereco de volta de um servidor de terceiros e so dele)."""
    return (
        await c.execute(
            text(
                "SELECT s.id, s.nome, s.situacao, s.origem, s.autorizacao IS NOT NULL AS configurado,"
                " (SELECT max(e.criado_em) FROM extrato e WHERE e.pessoa_id = :p AND e.sistema_id = s.id"
                " AND e.tipo = 'autorizacao_concluida') AS autorizado_em"
                " FROM sistemas s WHERE s.tipo_chave = 'por_pessoa'"
                " AND (CAST(:s AS uuid) IS NULL OR s.id = :s) AND EXISTS (SELECT 1 FROM ordens_servico o"
                " JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = o.modelo_id"
                " WHERE mf.sistema_id = s.id AND o.pessoa_id = :p AND o.situacao = 'ativa' AND o.fim > now())"
                " ORDER BY s.nome"
            ),
            {"p": pessoa_id, "s": sistema_id},
        )
    ).all()


# --- Pessoas, autorizacao e chaves (ADMIN) ---


async def desativar_pessoa(c: AsyncConnection, pessoa_id: uuid.UUID) -> bool:
    """Desativa a pessoa e encerra as sessoes dela nas telas. ``False`` se ja estava desativada."""
    mudou = (
        await c.execute(
            text(
                "UPDATE pessoas SET situacao = 'desativada', atualizado_em = now()"
                " WHERE id = :p AND situacao <> 'desativada' RETURNING id"
            ),
            {"p": pessoa_id},
        )
    ).first()
    await c.execute(
        text("UPDATE sessoes_telas SET encerrada_em = now() WHERE pessoa_id = :p AND encerrada_em IS NULL"),
        {"p": pessoa_id},
    )
    return mudou is not None


async def autorizacao_do_sistema(c: AsyncConnection, sistema_id: uuid.UUID) -> dict[str, Any] | None:
    valor = (
        await c.execute(text("SELECT autorizacao FROM sistemas WHERE id = :s"), {"s": sistema_id})
    ).scalar_one_or_none()
    return dict(valor) if isinstance(valor, dict) else None


async def ultimas_entregas(c: AsyncConnection, sistema_id: uuid.UUID) -> dict[str, Any]:
    """Quando o ADMIN entregou ao cofre, pela ultima vez, cada tipo de chave deste sistema (o valor nunca volta)."""
    linhas = (
        await c.execute(
            text(
                "SELECT dados->>'entrega' AS entrega, max(criado_em) AS em FROM extrato WHERE sistema_id = :s"
                " AND tipo IN ('admin_segredo_cliente_entregue', 'admin_chave_entregue') GROUP BY 1"
            ),
            {"s": sistema_id},
        )
    ).all()
    return {linha.entrega: linha.em for linha in linhas if linha.entrega}


async def programas_cadastrados(c: AsyncConnection) -> Sequence[Row[Any]]:
    """Programas que rodam sozinhos, para guardar a chave de programa de um sistema."""
    return (
        await c.execute(
            text("SELECT id, client_id, nome, situacao FROM aplicativos WHERE tipo = 'programa' ORDER BY nome")
        )
    ).all()


async def alvos_do_bloqueio(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID | None,
    aplicativo_id: uuid.UUID | None,
    sistema_id: uuid.UUID | None,
) -> dict[str, uuid.UUID | None]:
    """Confere no cadastro cada alvo pedido; o que nao existe volta como ``None``."""
    linha = (
        await c.execute(
            text(
                "SELECT (SELECT id FROM pessoas WHERE id = CAST(:p AS uuid)) AS pessoa_id,"
                " (SELECT id FROM aplicativos WHERE id = CAST(:a AS uuid)) AS aplicativo_id,"
                " (SELECT id FROM sistemas WHERE id = CAST(:s AS uuid)) AS sistema_id"
            ),
            {"p": pessoa_id, "a": aplicativo_id, "s": sistema_id},
        )
    ).one()
    return {"pessoa_id": linha.pessoa_id, "aplicativo_id": linha.aplicativo_id, "sistema_id": linha.sistema_id}


# --- Acoes, fichas e limites da pessoa (Fase 2) ---


async def operacao_da_ficha(c: AsyncConnection, ficha_id: uuid.UUID) -> uuid.UUID | None:
    valor = (
        await c.execute(text("SELECT operacao_id FROM fichas WHERE id = :f"), {"f": ficha_id})
    ).scalar_one_or_none()
    return uuid.UUID(str(valor)) if valor is not None else None


async def niveis_do_envio(c: AsyncConnection, pessoa_id: uuid.UUID, protocolos: Sequence[str]) -> dict[str, int]:
    """O nivel do selo com que cada acao da pessoa saiu ao sistema (``envios_selo.nivel``, 2 ou 3), pelo protocolo
    (Fase 4, a dica por linha do comprovante). Uma acao que saiu sem numero (nivel 1) nao tem envio e fica de fora:
    quem chama a conta como nivel 1. So as colunas que a entrada le de ``envios_selo`` (critica M10), nunca o
    numero."""
    if not protocolos:
        return {}
    linhas = (
        await c.execute(
            text(
                "SELECT o.protocolo, e.nivel FROM operacoes o JOIN envios_selo e ON e.operacao_id = o.id"
                " WHERE o.pessoa_id = :pe AND o.protocolo = ANY (CAST(:ps AS text[]))"
            ),
            {"pe": pessoa_id, "ps": list(protocolos)},
        )
    ).all()
    return {str(linha.protocolo): int(linha.nivel) for linha in linhas}


async def operacao_do_protocolo(c: AsyncConnection, pessoa_id: uuid.UUID, protocolo: str) -> uuid.UUID | None:
    """So as operacoes da propria pessoa: para os outros, o protocolo nao existe."""
    valor = (
        await c.execute(
            text("SELECT id FROM operacoes WHERE protocolo = :p AND pessoa_id = :pe"),
            {"p": protocolo, "pe": pessoa_id},
        )
    ).scalar_one_or_none()
    return uuid.UUID(str(valor)) if valor is not None else None


async def detalhes_da_operacao(c: AsyncConnection, operacao_id: uuid.UUID) -> Row[Any] | None:
    """O que a ficha mostra alem da operacao: o aplicativo e a ferramenta como o ADMIN revisou."""
    return (
        await c.execute(
            text(
                "SELECT a.nome AS aplicativo, r.titulo, r.descricao, r.classe, r.risco"
                " FROM operacoes o JOIN aplicativos a ON a.id = o.aplicativo_id"
                " JOIN ferramentas_revisadas r ON r.id = o.ferramenta_id WHERE o.id = :o"
            ),
            {"o": operacao_id},
        )
    ).first()


async def ferramentas_de_acao(
    c: AsyncConnection, pessoa_id: uuid.UUID, versao: str, terceiros: bool
) -> Sequence[Row[Any]]:
    """Ferramentas de acao (classe diferente de "le") das ordens de servico que valem agora para a pessoa,
    em sistemas de chave por pessoa: as unicas que podem entrar num limite por aproximacao.

    Servidor de terceiros: a mesma condicao da vitrine (``cadastro.repositorio._SQL_VITRINE``). So com
    ``terceiros`` (a chave da Fase 3 ligada, ou pessoa de teste), so com a ferramenta publicada na revisao do
    servidor que vale agora e so com a definicao declarada gravada pela revisao da Fase 3. Assim o limite nunca
    oferece um terceiro escondido nem uma ferramenta fora da vitrine."""
    return (
        await c.execute(
            text(
                "SELECT DISTINCT ON (o.aplicativo_id, f.id) o.aplicativo_id, a.nome AS aplicativo,"
                " f.id AS ferramenta_id, f.sistema_id, s.nome AS sistema, s.origem, f.nome_original, f.nome_vitrine,"
                " f.titulo, f.descricao, f.campos_destinatario, f.classe, f.risco"
                " FROM ordens_servico o JOIN aplicativos a ON a.id = o.aplicativo_id AND a.situacao = 'ativo'"
                " JOIN modelos_ordem m ON m.id = o.modelo_id AND m.ativo"
                " JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = m.id"
                " JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id AND f.nome_original = mf.nome_original"
                " AND f.situacao = 'ativa' AND f.classe <> 'le'"
                " JOIN sistemas s ON s.id = f.sistema_id AND s.situacao = 'ativo' AND s.versao_mcp = :v"
                " AND s.tipo_chave = 'por_pessoa'"
                " LEFT JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id AND rs.sistema_id = s.id"
                " WHERE o.pessoa_id = :p AND o.situacao = 'ativa' AND o.inicio <= now() AND o.fim > now()"
                " AND (s.origem = 'proprio'"
                " OR (CAST(:t AS boolean) AND f.id = ANY (rs.publicadas) AND f.definicao_declarada IS NOT NULL))"
                " ORDER BY o.aplicativo_id, f.id"
            ),
            {"p": pessoa_id, "v": versao, "t": terceiros},
        )
    ).all()


_SQL_LIMITES = (
    "SELECT l.id, l.pessoa_id, l.aplicativo_id, l.sistema_id, l.nome_original, l.destinatarios, l.por_dia,"
    " CASE WHEN l.dia = (now() AT TIME ZONE :fuso)::date THEN l.usados_hoje ELSE 0 END AS usados_hoje,"
    " l.valido_ate, l.com_passkey, l.motivo, l.criado_em,"
    " CASE WHEN l.situacao = 'ativo' AND l.valido_ate <= now() THEN 'vencido' ELSE l.situacao END AS situacao,"
    " a.nome AS aplicativo, s.nome AS sistema, s.origem AS sistema_origem,"
    " coalesce(f.nome_vitrine, l.nome_original) AS ferramenta, f.nome_vitrine IS NULL AS sem_revisao"
    " FROM limites l JOIN aplicativos a ON a.id = l.aplicativo_id JOIN sistemas s ON s.id = l.sistema_id"
    " LEFT JOIN ferramentas_revisadas f ON f.sistema_id = l.sistema_id AND f.nome_original = l.nome_original"
    " AND f.situacao = 'ativa'"
)


async def limites_da_pessoa(c: AsyncConnection, pessoa_id: uuid.UUID, fuso: str) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(_SQL_LIMITES + " WHERE l.pessoa_id = :p ORDER BY l.criado_em DESC LIMIT 200"),
            {"p": pessoa_id, "fuso": fuso},
        )
    ).all()


async def limite_da_pessoa(
    c: AsyncConnection, pessoa_id: uuid.UUID, limite_id: uuid.UUID, fuso: str
) -> Row[Any] | None:
    return (
        await c.execute(
            text(_SQL_LIMITES + " WHERE l.id = :i AND l.pessoa_id = :p"), {"i": limite_id, "p": pessoa_id, "fuso": fuso}
        )
    ).first()


async def limite_igual_ativo(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    sistema_id: uuid.UUID,
    nome_original: str,
    destinatarios: str,
    ligada: bool,
) -> bool:
    """Ha um limite igual que vale agora? ``ligada``: a passkey esta ligada. Com ela ligada, um limite criado ou
    ampliado sem a passkey nao vale (o nucleo nao o gasta) e nao conta como igual: a pessoa pode criar este."""
    linha = (
        await c.execute(
            text(
                "SELECT 1 FROM limites WHERE pessoa_id = :p AND aplicativo_id = :a AND sistema_id = :s"
                " AND nome_original = :n AND situacao = 'ativo' AND valido_ate > now()"
                " AND (com_passkey OR NOT :pk) AND destinatarios = CAST(:d AS jsonb) LIMIT 1"
            ),
            {
                "p": pessoa_id,
                "a": aplicativo_id,
                "s": sistema_id,
                "n": nome_original,
                "d": destinatarios,
                "pk": ligada,
            },
        )
    ).first()
    return linha is not None


async def travar_revisao(c: AsyncConnection, ferramenta_id: uuid.UUID) -> None:
    """Trava (FOR SHARE) a versao revisada da ferramenta ate o fim da transacao que cria o limite.

    O ADMIN, ao gravar uma revisao nova, muda esta linha para 'substituida' e, na mesma transacao, encerra os
    limites que nao conferem os campos de destinatario marcados agora. Com a trava, as duas transacoes nao se
    cruzam: ou o limite e gravado antes (e a revisao do ADMIN o ve e o encerra, se for o caso), ou a revisao
    termina antes (e a pessoa le os campos novos, ou a acao ja nao esta na lista)."""
    await c.execute(text("SELECT 1 FROM ferramentas_revisadas WHERE id = :f FOR SHARE"), {"f": ferramenta_id})


async def revisao_por_id(c: AsyncConnection, ferramenta_id: uuid.UUID) -> Row[Any] | None:
    """Sistema e nome original de uma versao revisada, em qualquer situacao (ativa, suspensa ou substituida)."""
    return (
        await c.execute(
            text("SELECT id, sistema_id, nome_original, situacao FROM ferramentas_revisadas WHERE id = :f"),
            {"f": ferramenta_id},
        )
    ).first()


async def criar_limite(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    sistema_id: uuid.UUID,
    nome_original: str,
    destinatarios: str,
    por_dia: int,
    dias: int,
    com_passkey: bool,
    fuso: str,
) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios,"
                        " por_dia, dia, valido_ate, com_passkey) VALUES (:p, :a, :s, :n, CAST(:d AS jsonb), :q,"
                        " (now() AT TIME ZONE :fuso)::date, now() + make_interval(days => :dias), :pk) RETURNING id"
                    ),
                    {
                        "p": pessoa_id,
                        "a": aplicativo_id,
                        "s": sistema_id,
                        "n": nome_original,
                        "d": destinatarios,
                        "q": por_dia,
                        "fuso": fuso,
                        "dias": dias,
                        "pk": com_passkey,
                    },
                )
            ).scalar_one()
        )
    )


async def ampliar_limite(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    limite_id: uuid.UUID,
    por_dia: int,
    dias: int,
    teto_dia: int,
    teto_dias: int,
    com_passkey: bool,
) -> Row[Any] | None:
    """Amplia so um limite ativo da propria pessoa, dentro do teto vigente.

    Abaixo do teto, nada diminui: a quantidade e o prazo so crescem. Um limite que ficou acima do teto (a
    politica baixou) renova no maximo ate ``teto_dia`` por dia, e o prazo nunca passa de ``teto_dias`` dias a
    contar de agora.

    ``com_passkey`` diz como foi ampliado agora (com a passkey ou, com ela desligada, com o login refeito), e e o
    que fica gravado. Com a passkey ligada, um limite criado sem ela nao vale e nao e ampliado (``None``): a
    pessoa encerra e cria outro, com a passkey."""
    return (
        await c.execute(
            text(
                "UPDATE limites SET por_dia = least(greatest(por_dia, :q), :td),"
                " valido_ate = least(greatest(valido_ate, now() + make_interval(days => :dias)),"
                " now() + make_interval(days => :tdias)),"
                " com_passkey = :pk"
                " WHERE id = :i AND pessoa_id = :p AND situacao = 'ativo' AND valido_ate > now()"
                " AND (com_passkey OR NOT :pk)"
                " RETURNING id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate, com_passkey"
            ),
            {
                "i": limite_id,
                "p": pessoa_id,
                "q": por_dia,
                "td": teto_dia,
                "dias": dias,
                "tdias": teto_dias,
                "pk": com_passkey,
            },
        )
    ).first()


async def liberadas_pelo_limite(c: AsyncConnection, limite_id: uuid.UUID) -> list[int]:
    """Linhas da fila que o limite liberou e que ainda esperam a hora de sair (as mais antigas primeiro)."""
    return [
        int(i)
        for i in (
            await c.execute(
                text("SELECT id FROM fila_saida WHERE limite_id = :l AND estado = 'liberada' ORDER BY id"),
                {"l": limite_id},
            )
        ).scalars()
    ]


SQL_ENCERRAR_SEM_OS_CAMPOS = """
WITH alvo AS (
  SELECT id FROM limites
  WHERE sistema_id = :s AND nome_original = :n AND situacao IN ('ativo', 'suspenso')
    AND jsonb_array_length(destinatarios) > 0
    AND NOT (CAST(:campos AS text[]) <@ ARRAY(SELECT item ->> 'campo' FROM jsonb_array_elements(destinatarios) AS item))
  ORDER BY id FOR UPDATE
), mudou AS (
  UPDATE limites l SET situacao = 'encerrado', motivo = :m
  FROM alvo WHERE l.id = alvo.id AND l.situacao IN ('ativo', 'suspenso')
  RETURNING l.id, l.pessoa_id, l.aplicativo_id, l.sistema_id
)
SELECT id, pessoa_id, aplicativo_id, sistema_id FROM mudou ORDER BY id
"""


async def encerrar_limites_sem_os_campos(
    c: AsyncConnection, *, sistema_id: uuid.UUID, nome_original: str, campos: list[str], motivo: str
) -> Sequence[Row[Any]]:
    """Encerra os limites "so estes destinatarios" desta acao que nao conferem algum campo marcado agora.

    O ADMIN marcou um campo de destinatario novo (por exemplo, a copia): um limite criado antes nao o fecha, e
    um pedido com copia para fora sairia sem ficha. Ele termina aqui; a pessoa cria outro, ja com o campo novo.
    Limite "qualquer destinatario" (lista vazia) nao muda. Desmarcar um campo tambem nao: o limite segue mais
    fechado do que precisa.

    Trava os limites em ordem de ``id`` antes de mudar (``SQL_ENCERRAR_SEM_OS_CAMPOS``), como a quarentena
    (``saida.fila.SQL_SUSPENDER_LIMITES``): a revisao do ADMIN e a quarentena do mesmo servidor ao mesmo tempo
    nao se travam uma a outra. Devolve em ordem de ``id``; quem chama cancela, depois, o que cada um liberou (a
    linha do limite antes da linha da fila, a ordem escrita em ``saida/fila.py``)."""
    return (
        await c.execute(
            text(SQL_ENCERRAR_SEM_OS_CAMPOS), {"s": sistema_id, "n": nome_original, "campos": campos, "m": motivo}
        )
    ).all()


async def encerrar_limite(
    c: AsyncConnection, *, pessoa_id: uuid.UUID, limite_id: uuid.UUID, motivo: str
) -> Row[Any] | None:
    return (
        await c.execute(
            text(
                "UPDATE limites SET situacao = 'encerrado', motivo = :m"
                " WHERE id = :i AND pessoa_id = :p AND situacao IN ('ativo', 'suspenso')"
                " RETURNING id, aplicativo_id, sistema_id, nome_original"
            ),
            {"i": limite_id, "p": pessoa_id, "m": motivo},
        )
    ).first()
