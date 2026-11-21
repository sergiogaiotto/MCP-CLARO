"""Consultas ao cadastro. SQL explicito; a vitrine sai daqui, pessoa por pessoa.

Servidor de terceiros (Fase 3): so aparece na vitrine com ``terceiros`` verdadeiro (a chave da Fase 3 ligada, ou
pessoa de teste: quem chama decide) e so com as ferramentas publicadas na revisao do servidor vigente. A revisao
de uma ferramenta de terceiro grava o esquema limpo (sem texto do fornecedor), sem as dicas do fornecedor e com
"traz texto de fora". Para chamar (``ferramenta_na_vitrine``, ``ferramenta_da_ordem``), o esquema de entrada de
um terceiro vem sem ``pattern`` nem ``uniqueItems`` (``esquema.para_conferir``): quem confere isso e o servidor do
terceiro. A vitrine (``vitrine``) mostra o esquema revisado inteiro.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.esquema import com_descricoes, conferir_terceiro, limpar_textos, para_conferir
from mcp_claro.cadastro.impressao import impressao
from mcp_claro.cadastro.modelos import DICAS_CONHECIDAS, ORIGENS, Aplicativo, FerramentaVitrine, Pessoa, Sistema
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
        g("revisao_servidor_id"),
        g("sondagem_desfecho"),
    )


async def aplicativo_por_client_id(c: AsyncConnection, client_id: str) -> Aplicativo | None:
    linha = (await c.execute(text("SELECT * FROM aplicativos WHERE client_id = :c"), {"c": client_id})).first()
    return _aplicativo(linha) if linha else None


async def marcar_versao_vista(c: AsyncConnection, aplicativo_id: uuid.UUID, versao: str) -> None:
    await c.execute(
        text("UPDATE aplicativos SET versao_mcp_vista = :v, visto_em = now() WHERE id = :i"),
        {"v": versao, "i": aplicativo_id},
    )


async def pessoa_no_login(
    c: AsyncConnection, emissor: str, sujeito: str, nome: str | None, *, email: str | None = None
) -> Pessoa:
    """Acha a pessoa pelo emissor e sujeito do cracha; cria no primeiro login. Guarda o e-mail verificado."""
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
    if email and email != getattr(linha, "email", None):
        await c.execute(text("UPDATE pessoas SET email = :m WHERE id = :i"), {"m": email, "i": linha.id})
    return _pessoa(linha)


async def sistema_por_id(c: AsyncConnection, sistema_id: uuid.UUID) -> Sistema | None:
    linha = (await c.execute(text("SELECT * FROM sistemas WHERE id = :i"), {"i": sistema_id})).first()
    return _sistema(linha) if linha else None


# Um terceiro so entra com ``terceiros`` e so com as ferramentas publicadas na revisao do servidor vigente (a
# guarda de sistemas so deixa a revisao vigente num servidor 'ativo'). Rever uma ferramenta grava outro id: ela sai
# da vitrine ate a proxima conclusao. A revisada de terceiro tambem precisa da definicao declarada, que so a
# revisao da Fase 3 grava (``revisar_ferramenta``, com o esquema limpo e as descricoes do ADMIN): uma linha de
# antes dela nunca leva o texto do fornecedor a vitrine. Sistema proprio: como nas Fases 1 e 2.
_SQL_VITRINE = """
SELECT DISTINCT ON (f.id)
       f.*, o.id AS ordem_id,
       s.id AS s_id, s.nome AS s_nome, s.endereco AS s_endereco, s.origem AS s_origem, s.tipo_chave AS s_tipo_chave,
       s.selo AS s_selo, s.situacao AS s_situacao, s.versao_mcp AS s_versao_mcp,
       s.revisao_servidor_id AS s_revisao_servidor_id, s.sondagem_desfecho AS s_sondagem_desfecho
FROM ordens_servico o
JOIN modelos_ordem m ON m.id = o.modelo_id AND m.ativo
JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = m.id
JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id AND f.nome_original = mf.nome_original
                             AND f.situacao = 'ativa'
JOIN sistemas s ON s.id = f.sistema_id AND s.situacao = 'ativo' AND s.versao_mcp = :versao
LEFT JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id AND rs.sistema_id = s.id
WHERE o.pessoa_id = :pessoa AND o.aplicativo_id = :aplicativo AND o.situacao = 'ativa'
  AND o.inicio <= now() AND o.fim > now()
  AND (s.origem = 'proprio'
       OR (CAST(:terceiros AS boolean) AND f.id = ANY (rs.publicadas) AND f.definicao_declarada IS NOT NULL))
"""


def _ferramenta(linha: Row[Any], *, para_chamar: bool = False) -> FerramentaVitrine:
    """``para_chamar``: num terceiro, ``esquema_entrada`` e a copia com que a entrada confere os argumentos, sem
    ``pattern`` nem ``uniqueItems`` (critica B3). Sem ele, o esquema revisado inteiro, que a vitrine mostra."""
    esquema_entrada = dict(linha.esquema_entrada)
    if para_chamar and linha.s_origem != "proprio":
        esquema_entrada = para_conferir(esquema_entrada)
    return FerramentaVitrine(
        id=linha.id,
        sistema=_sistema(linha, "s_"),
        nome_original=linha.nome_original,
        versao=linha.versao,
        nome_vitrine=linha.nome_vitrine,
        titulo=linha.titulo,
        descricao=linha.descricao,
        esquema_entrada=esquema_entrada,
        esquema_saida=dict(linha.esquema_saida) if linha.esquema_saida else None,
        etiquetas=dict(linha.etiquetas or {}),
        classe=linha.classe,
        risco=linha.risco,
        campos_pessoais=tuple(linha.campos_pessoais or ()),
        ordem_id=linha.ordem_id,
        texto_de_fora=bool(linha.texto_de_fora),
    )


async def vitrine(
    c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, versao: str, *, terceiros: bool = False
) -> list[FerramentaVitrine]:
    """A vitrine da pessoa no aplicativo, como o aplicativo a ve (``como_tool``). ``terceiros``: mostra servidores
    de terceiros (chave da Fase 3 ligada ou pessoa de teste). O padrao e o lado que esconde."""
    linhas = (
        await c.execute(
            text(_SQL_VITRINE + " ORDER BY f.id, o.fim DESC"),
            {"pessoa": pessoa_id, "aplicativo": aplicativo_id, "versao": versao, "terceiros": terceiros},
        )
    ).all()
    return sorted((_ferramenta(linha) for linha in linhas), key=lambda f: f.nome_vitrine)


async def ferramenta_na_vitrine(
    c: AsyncConnection,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    nome_vitrine: str,
    versao: str,
    *,
    terceiros: bool = False,
) -> FerramentaVitrine | None:
    """A ferramenta da vitrine que o aplicativo quer chamar. Num terceiro, ``esquema_entrada`` e a copia para
    conferir os argumentos (``esquema.para_conferir``)."""
    linha = (
        await c.execute(
            text(_SQL_VITRINE + " AND f.nome_vitrine = :nome ORDER BY f.id, o.fim DESC"),
            {
                "pessoa": pessoa_id,
                "aplicativo": aplicativo_id,
                "versao": versao,
                "nome": nome_vitrine,
                "terceiros": terceiros,
            },
        )
    ).first()
    return _ferramenta(linha, para_chamar=True) if linha else None


async def ferramenta_da_ordem(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    ordem_id: uuid.UUID,
    ferramenta_id: uuid.UUID,
    versao: str,
    terceiros: bool = False,
) -> FerramentaVitrine | None:
    """Reconferencia do despachante: a mesma regra da vitrine, presa a ordem e a versao revisada vigente. Como em
    ``ferramenta_na_vitrine``, o esquema de entrada de um terceiro e a copia para conferir."""
    linha = (
        await c.execute(
            text(_SQL_VITRINE + " AND f.id = :f AND o.id = :o ORDER BY f.id, o.fim DESC"),
            {
                "pessoa": pessoa_id,
                "aplicativo": aplicativo_id,
                "versao": versao,
                "f": ferramenta_id,
                "o": ordem_id,
                "terceiros": terceiros,
            },
        )
    ).first()
    return _ferramenta(linha, para_chamar=True) if linha else None


async def esquema_declarado(c: AsyncConnection, sistema_id: uuid.UUID, nome: str) -> dict[str, Any] | None:
    """Esquema de entrada que o sistema declarou por ultimo (para os cabecalhos Mcp-Param-* de saida)."""
    valor = (
        await c.execute(
            text("SELECT definicao -> 'inputSchema' FROM ferramentas_vistas WHERE sistema_id = :s AND nome = :n"),
            {"s": sistema_id, "n": nome},
        )
    ).scalar_one_or_none()
    return dict(valor) if isinstance(valor, dict) else None


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
    """Cria o sistema pendente. So vira ativo depois da sondagem de versao pelo despachante.

    ``origem``: ``proprio`` ou ``terceiro`` (fixa para sempre; para mudar, cadastra-se de novo)."""
    if origem not in ORIGENS:
        raise ValueError(f"origem invalida: escolha {' ou '.join(ORIGENS)}")
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


class CampoForaDoEsquema(ValueError):
    """Campo de destinatario que o esquema de entrada da ferramenta nao tem."""

    def __init__(self, campos: list[str]) -> None:
        self.campos = campos
        super().__init__("campos fora do esquema de entrada: " + ", ".join(campos))


def campos_de_entrada(esquema: Any) -> tuple[str, ...]:
    """Os nomes das propriedades do esquema de entrada, na ordem do esquema."""
    propriedades = esquema.get("properties") if isinstance(esquema, dict) else None
    return tuple(str(nome) for nome in propriedades) if isinstance(propriedades, dict) else ()


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
    campos_destinatario: list[str] | None = None,
    descricoes_campos: Mapping[str, str] | None = None,
    texto_de_fora: bool = False,
) -> uuid.UUID:
    """Grava nova versao revisada. A anterior vira 'substituida'. O texto e do cadastro, nao do fornecedor.

    ``campos_destinatario``: os campos do pedido que dizem para quem a acao vai, marcados pelo ADMIN. So valem
    nomes do esquema de entrada (``CampoForaDoEsquema`` se nao). O limite "so estes destinatarios" confere estes.

    ``descricoes_campos``: descricao escrita pelo ADMIN para campos do primeiro nivel (ate 300 letras; campo que
    o esquema nao tem levanta ``esquema.CampoDesconhecido``). ``texto_de_fora``: a resposta traz texto escrito por
    gente de fora (e-mail, pagina, comentario) e vai emoldurada ao aplicativo.

    Servidor de terceiros (``sistemas.origem``, lida nesta transacao): o esquema de entrada e o de saida sao
    gravados limpos (``esquema.limpar_textos``, depois as descricoes do ADMIN), sem as dicas do fornecedor e com
    ``texto_de_fora`` sempre verdadeiro (o banco tambem forca as duas coisas). O ``pattern`` fica gravado (a
    vitrine mostra), mas a entrada nunca o confere (``esquema.para_conferir``). A revisao recusa, com a frase para
    o ADMIN (``esquema.EsquemaRecusado``, um ``EsquemaInvalido``), ``patternProperties``, ``$id``, ``$anchor``,
    ``$dynamicAnchor``, ``$dynamicRef``, ``unevaluatedProperties`` e ``unevaluatedItems`` em qualquer lugar,
    ``$schema`` fora da raiz, referencia que nao seja ``#/$defs/<nome>`` ou ``#/definitions/<nome>``, esquema em
    ciclo e esquema grande ou fundo demais, contando todas as definicoes e seguindo as referencias
    (``esquema.conferir_terceiro``). A ``impressao`` e sempre a da definicao declarada, e ``definicao_declarada``
    fica guardada para o antes e depois.
    """
    declarada_entrada = esquema_entrada or dict(definicao_declarada.get("inputSchema") or {})
    # Antes do banco, como nas Fases 1 e 2: esquema invalido e campo de destinatario fora dele. Limpar e por as
    # descricoes nao muda os nomes dos campos, entao a conferencia vale para o esquema que sera gravado.
    conferir_esquema(declarada_entrada)
    destinatario = list(dict.fromkeys(campos_destinatario or []))
    fora = [nome for nome in destinatario if nome not in campos_de_entrada(declarada_entrada)]
    if fora:
        raise CampoForaDoEsquema(fora)
    origem = (
        await c.execute(text("SELECT origem FROM sistemas WHERE id = :s"), {"s": sistema_id})
    ).scalar_one_or_none()
    de_terceiro = origem is not None and origem != "proprio"
    saida_declarada = definicao_declarada.get("outputSchema")
    if de_terceiro:
        esquema = com_descricoes(limpar_textos(declarada_entrada), descricoes_campos or {})
        esquema_saida: Any = limpar_textos(saida_declarada) if isinstance(saida_declarada, dict) else None
        etiquetas: dict[str, bool] = {}
        texto_de_fora = True
    else:
        esquema = com_descricoes(declarada_entrada, descricoes_campos or {})
        esquema_saida = saida_declarada
        etiquetas = {
            k: bool(v) for k, v in (definicao_declarada.get("annotations") or {}).items() if k in DICAS_CONHECIDAS
        }
    conferir_esquema(esquema)
    if de_terceiro:
        conferir_terceiro(esquema)
        conferir_esquema(para_conferir(esquema))  # a copia da entrada tambem e um esquema valido
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
                        " campos_destinatario, impressao, revisado_por, texto_de_fora, definicao_declarada)"
                        " VALUES (:s, :n, :v, :nv, :t, :d, CAST(:ee AS jsonb), CAST(:es AS jsonb), CAST(:et AS jsonb),"
                        " :c, :r, :cp, :cd, :i, :rp, :tf, CAST(:dd AS jsonb)) RETURNING id"
                    ),
                    {
                        "s": sistema_id,
                        "n": nome,
                        "v": versao,
                        "nv": nome_vitrine,
                        "t": titulo,
                        "d": descricao,
                        "ee": json.dumps(esquema),
                        "es": json.dumps(esquema_saida),
                        "et": json.dumps(etiquetas),
                        "c": classe,
                        "r": risco,
                        "cp": campos_pessoais or [],
                        "cd": destinatario,
                        "i": impressao(definicao_declarada),
                        "rp": revisado_por,
                        "tf": texto_de_fora,
                        "dd": json.dumps(definicao_declarada),
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
