"""Ajudas dos testes da Fase 4: montar no banco um sistema com selo, a declaracao de teste, um teste gravado e um
envio com numero, para os grupos seguintes (G1 a G9).

Tudo aqui usa a conexao do DONO do esquema (o usuario das migracoes, nos testes) e nao passa pelo despachante, pela
bateria nem pelas telas. Os gatilhos da migracao 0007 valem do mesmo jeito:

- a declaracao e a versao seguinte, de um ADMIN ativo, com o alvo de teste diferente da producao de todo sistema do
  cadastro (normalizado; de novo no pedido e no teste). Por isso nenhum teste grava (confirma) um sistema cuja
  producao seja ``ENDERECO_TESTE``: as declaracoes de todos os outros testes seriam recusadas. Quem precisar disso usa
  uma transacao que volta atras;
- o teste so nasce de um pedido ``rodando`` (com a porta de saida aberta), com o retrato do sistema de agora, as
  ferramentas de acao ativas e, aprovado, com todos os casos obrigatorios; a data e a validade sao do banco;
- o selo vigente sai de ``selo_vigente()``: o contrato tem de estar listado em ``ferramentas_vistas`` (ha menos de 48
  horas), por isso ``proprio_com_acoes`` ja grava as ferramentas do contrato como lidas agora;
- o envio nasce com a fila em ``liberada`` e o numero e do banco (copiado da original no pedido igual, quando pode).

Enderecos de duble (G1, G3, G9): ``selo_alvo_na_producao`` compara o alvo de teste com a producao de TODO sistema do
banco da sessao, de qualquer situacao, inclusive os que testes anteriores deixaram gravados. Um duble escuta em
``127.0.0.1`` numa porta efemera, que o sistema operacional reaproveita na mesma sessao: um sistema antigo com
producao ``http://127.0.0.1:P/mcp`` faria a declaracao de um teste seguinte, cujo alvo caia na mesma porta P, ser
recusada. Por isso todo endereco de duble (producao e alvo) sai de ``endereco_de_duble``, com um trecho de caminho
unico (``caminho_unico``), e o duble serve nesse caminho. A forma normalizada guarda o caminho, entao dois enderecos
na mesma porta nunca colidem. Quem nao puder servir num caminho unico roda o teste numa transacao que volta atras.

A conexao ``c`` e uma ``AsyncConnection`` do dono; quem chama faz o commit (``engine.begin()``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.saida.protocolo import chave_pedido_igual as calcular_chave_pedido_igual
from mcp_claro.saida.protocolo import codigo_conferencia
from mcp_claro.selo import contrato
from mcp_claro.versao import VERSAO_MCP
from tests.apoio_terceiros import Revisada, definicao, revisar_ferramenta

ENDERECO_TESTE = "https://teste.selo.exemplo.teste/mcp"
"""O alvo de teste das declaracoes do apoio. Nunca a producao de um sistema gravado (veja acima)."""
RESPONSAVEL = "Equipe do sistema de teste"
SALVA_POR = "admin-teste"


# --- Enderecos de duble ----------------------------------------------------------------------------------------


def caminho_unico(papel: str = "mcp") -> str:
    """Um caminho que nenhum outro teste da sessao usa: ``/selo-<uuid sem tracos>/<papel>`` (so minusculas, letras
    sem reserva: a forma normalizada fica igual ao texto)."""
    if not papel or not set(papel) <= set("abcdefghijklmnopqrstuvwxyz0123456789-_"):
        raise ValueError("O papel do caminho e um nome curto em minusculas")
    return f"/selo-{uuid.uuid4().hex}/{papel}"


def endereco_de_duble(base: str, caminho: str | None = None) -> str:
    """O endereco de um duble: ``base`` (esquema, nome e porta, sem caminho, como ``http://127.0.0.1:8123``) com um
    caminho unico (``caminho_unico()``, ou o ``caminho`` dado). Use para a producao e para o alvo de teste."""
    partes = urlsplit(base)
    if not partes.scheme or not partes.netloc or partes.path.strip("/") or partes.query or partes.fragment:
        raise ValueError("A base do duble e so esquema, nome e porta, sem caminho")
    caminho = caminho or caminho_unico()
    if not caminho.startswith("/"):
        raise ValueError("O caminho do duble comeca com /")
    return f"{partes.scheme}://{partes.netloc}{caminho}"


# --- Pessoas e aplicativo --------------------------------------------------------------------------------------


async def nova_pessoa(c: AsyncConnection, *, admin: bool = False, teste: bool = False) -> uuid.UUID:
    """Uma pessoa ativa (ADMIN com ``admin``; de teste com ``teste``)."""
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO pessoas (emissor, sujeito, nome, papel, teste)"
                        " VALUES ('https://login.teste', :s, :n, :p, :t) RETURNING id"
                    ),
                    {
                        "s": f"sujeito-{uuid.uuid4().hex}",
                        "n": "ADMIN de teste" if admin else "Pessoa de teste",
                        "p": "admin" if admin else "pessoa",
                        "t": teste,
                    },
                )
            ).scalar_one()
        )
    )


async def novo_aplicativo(c: AsyncConnection) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text("INSERT INTO aplicativos (client_id, nome, familia) VALUES (:c, :c, 'teste') RETURNING id"),
                    {"c": f"app-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
        )
    )


# --- Sistema com as ferramentas do contrato --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SistemaDoSelo:
    id: uuid.UUID
    nome: str
    endereco: str
    acoes: list[Revisada]
    leituras: list[Revisada]


def definicao_do_contrato(nome: str) -> dict[str, Any]:
    """A definicao de uma ferramenta do contrato como um sistema que adotou o contrato a lista no ``tools/list``."""
    esquemas = contrato.ESQUEMAS[nome]
    return {
        "name": nome,
        "description": f"Ferramenta do contrato do selo; guarda os numeros por {contrato.GUARDA_MINIMA_DIAS} dias.",
        "inputSchema": esquemas["inputSchema"],
        "outputSchema": esquemas["outputSchema"],
    }


async def listar_contrato(
    c: AsyncConnection,
    sistema_id: uuid.UUID,
    nivel: int = 3,
    *,
    definicoes: Mapping[str, Mapping[str, Any]] | None = None,
    vista_em: datetime | None = None,
) -> None:
    """Grava em ``ferramentas_vistas`` as ferramentas do contrato do nivel, como o despachante faria depois de ler o
    sistema (sem sumir, lidas agora ou em ``vista_em``). ``definicoes`` troca a definicao de alguma delas."""
    for nome in contrato.ferramentas_do_nivel(nivel):
        d = dict((definicoes or {}).get(nome) or definicao_do_contrato(nome))
        await c.execute(
            text(
                "INSERT INTO ferramentas_vistas (sistema_id, nome, definicao, impressao, vista_em, sumiu_em)"
                " VALUES (:s, :n, CAST(:d AS jsonb), :i, coalesce(CAST(:v AS timestamptz), now()), NULL)"
                " ON CONFLICT (sistema_id, nome) DO UPDATE SET definicao = EXCLUDED.definicao,"
                " impressao = EXCLUDED.impressao, vista_em = EXCLUDED.vista_em, sumiu_em = NULL"
            ),
            {"s": sistema_id, "n": nome, "d": json.dumps(d), "i": "sha256:" + "0" * 64, "v": vista_em},
        )


async def proprio_com_acoes(
    c: AsyncConnection,
    *,
    acoes: Sequence[str] = ("enviar_lembrete",),
    leituras: Sequence[str] = ("listar_lembretes",),
    endereco: str | None = None,
    nivel_do_contrato: int | None = 3,
) -> SistemaDoSelo:
    """Um sistema proprio ``ativo`` na 2026-07-28, com chave por pessoa, as ferramentas de acao (classe ``muda``) e de
    leitura revisadas e, com ``nivel_do_contrato``, as ferramentas do contrato desse nivel listadas agora."""
    nome = f"proprio-selo-{uuid.uuid4().hex[:8]}"
    endereco = endereco or f"https://{nome}.exemplo.teste/mcp"
    sistema_id = uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp, sondagem,"
                        " sondagem_desfecho, versoes_anunciadas) VALUES (:n, :e, 'por_pessoa', 'ativo', :v,"
                        " '{\"ok\": true}'::jsonb, 'aceito', ARRAY['2026-07-28']) RETURNING id"
                    ),
                    {"n": nome, "e": endereco, "v": VERSAO_MCP},
                )
            ).scalar_one()
        )
    )
    revisadas_acao = [
        await revisar_ferramenta(
            c,
            sistema_id,
            definicao(
                a,
                esquema={"type": "object", "properties": {"para": {"type": "string"}, "texto": {"type": "string"}}},
                anotacoes={"destructiveHint": False, "readOnlyHint": False},
            ),
            classe="muda",
        )
        for a in acoes
    ]
    revisadas_leitura = [
        await revisar_ferramenta(c, sistema_id, definicao(nome_leitura, anotacoes={"readOnlyHint": True}))
        for nome_leitura in leituras
    ]
    if nivel_do_contrato is not None:
        await listar_contrato(c, sistema_id, nivel_do_contrato)
    return SistemaDoSelo(sistema_id, nome, endereco, revisadas_acao, revisadas_leitura)


# --- Declaracao de teste ---------------------------------------------------------------------------------------


def declaracao(
    acoes: Sequence[str],
    *,
    modo: str = "ambiente_de_teste",
    endereco_teste: str | None = ENDERECO_TESTE,
    responsavel: str = RESPONSAVEL,
    leitura: str = "listar_lembretes",
) -> dict[str, Any]:
    """Uma declaracao de teste no formato do desenho (secao 2.5) que cobre as ferramentas de acao ``acoes``."""
    ferramentas: dict[str, Any] = {}
    for nome in acoes:
        entrada: dict[str, Any] = {
            "argumentos": {"para": "caixa-de-teste@exemplo.teste", "texto": "teste do selo"},
            "argumentos_outros": {"para": "caixa-de-teste@exemplo.teste", "texto": "outro texto"},
            "argumentos_recusados": {"para": "nao-e-um-endereco"},
            "rastro": {"ferramenta": leitura, "argumentos": {}, "conta": "/itens"},
        }
        if modo == "ferramentas_de_teste":
            entrada["ferramenta_de_teste"] = f"{nome}_teste"
        ferramentas[nome] = entrada
    d: dict[str, Any] = {
        "contrato": contrato.CONTRATO,
        "modo": modo,
        "responsavel": responsavel,
        "guarda_dias": contrato.GUARDA_MINIMA_DIAS,
        "ferramentas": ferramentas,
    }
    if modo == "ambiente_de_teste":
        d["endereco_teste"] = endereco_teste
    return d


async def salvar_declaracao(
    c: AsyncConnection,
    sistema_id: uuid.UUID,
    admin_id: uuid.UUID,
    dados: Mapping[str, Any] | None = None,
    *,
    salva_por: str = SALVA_POR,
) -> uuid.UUID:
    """Grava a proxima versao de ``declaracoes_selo`` (por padrao, uma que cobre as acoes ativas do sistema)."""
    if dados is None:
        nomes = [
            str(n)
            for (n,) in await c.execute(
                text(
                    "SELECT nome_original FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa'"
                    " AND classe IN ('muda', 'tem_volta') ORDER BY nome_original"
                ),
                {"s": sistema_id},
            )
        ]
        dados = declaracao(nomes)
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO declaracoes_selo (sistema_id, versao, contrato, modo, endereco_teste, declaracao,"
                        " responsavel, salva_por, pessoa_id)"
                        " SELECT CAST(:s AS uuid), coalesce(max(versao), 0) + 1, CAST(:c AS text), CAST(:m AS text),"
                        " CAST(:e AS text), CAST(:d AS jsonb), CAST(:r AS text), CAST(:q AS text), CAST(:p AS uuid)"
                        " FROM declaracoes_selo WHERE sistema_id = :s RETURNING id"
                    ),
                    {
                        "s": sistema_id,
                        "c": dados.get("contrato"),
                        "m": dados.get("modo"),
                        "e": dados.get("endereco_teste"),
                        "d": json.dumps(dados),
                        "r": dados.get("responsavel"),
                        "q": salva_por,
                        "p": admin_id,
                    },
                )
            ).scalar_one()
        )
    )


# --- Teste gravado ---------------------------------------------------------------------------------------------


def item(
    caso: str,
    ferramenta: uuid.UUID | str | None,
    resultado: str,
    *,
    razao: str | None = None,
    tentativas: int | None = None,
    excluidas: int = 0,
) -> dict[str, Any]:
    """Um item de ``testes_selo.casos``. Tentativas: 1 nos resultados que pedem chamada, 0 nos outros."""
    if tentativas is None:
        tentativas = 1 if resultado in ("cumpriu", "falhou", "nao_observado") else 0
    return {
        "caso": caso,
        "ferramenta": None if ferramenta is None else str(ferramenta),
        "resultado": resultado,
        "razao": razao,
        "tentativas": tentativas,
        "excluidas": excluidas,
    }


def casos_que_aprovam(ferramentas: Sequence[uuid.UUID], nivel: int) -> list[dict[str, Any]]:
    """Os itens de um teste aprovado no nivel ``nivel``: cada caso obrigatorio com o melhor resultado do metodo e o
    C-2.2d (so informa) como ``nao_testado`` por poucos pares, como sai com ``BADGE_TEST_PAIRS`` padrao."""
    itens: list[dict[str, Any]] = []
    for caso in contrato.CASOS:
        if caso.nivel > nivel:
            continue
        alvos: list[uuid.UUID | None] = [None] if caso.escopo == "sistema" else list(ferramentas)
        for alvo in alvos:
            if caso.obrigatorio:
                itens.append(item(caso.caso, alvo, caso.maximo))
            else:
                itens.append(item(caso.caso, alvo, "nao_testado", razao="poucos_pares"))
    return itens


@dataclass(frozen=True, slots=True)
class TesteGravado:
    id: uuid.UUID
    pedido_id: uuid.UUID
    declaracao_id: uuid.UUID
    nivel_pedido: int
    nivel_obtido: int
    testado_em: datetime
    vale_ate: datetime


async def acoes_do_sistema(c: AsyncConnection, sistema_id: uuid.UUID) -> list[uuid.UUID]:
    """``selo_acoes_do_sistema``: as ferramentas de acao que o teste cobre, em ordem."""
    valor = (await c.execute(text("SELECT selo_acoes_do_sistema(:s)"), {"s": sistema_id})).scalar_one()
    return [uuid.UUID(str(v)) for v in valor]


async def pedir_teste(
    c: AsyncConnection,
    sistema_id: uuid.UUID,
    admin_id: uuid.UUID,
    declaracao_id: uuid.UUID,
    *,
    nivel: int = 2,
    rodando: bool = True,
) -> uuid.UUID:
    """O pedido de teste do ADMIN e, com ``rodando``, a vez do despachante (a porta de saida precisa estar aberta)."""
    pedido = uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO pedidos_teste_selo (sistema_id, nivel, declaracao_id, pedido_por)"
                        " VALUES (:s, :n, :d, :p) RETURNING id"
                    ),
                    {"s": sistema_id, "n": nivel, "d": declaracao_id, "p": admin_id},
                )
            ).scalar_one()
        )
    )
    if rodando:
        await c.execute(text("UPDATE pedidos_teste_selo SET estado = 'rodando' WHERE id = :i"), {"i": pedido})
    return pedido


async def gravar_teste(
    c: AsyncConnection,
    sistema_id: uuid.UUID,
    *,
    nivel: int = 2,
    aprovado: bool = True,
    casos: Sequence[Mapping[str, Any]] | None = None,
    admin_id: uuid.UUID | None = None,
    declaracao_id: uuid.UUID | None = None,
    chamadas: int | None = None,
) -> TesteGravado:
    """Grava um teste de selo como o despachante faria no fim da bateria: pedido, ``rodando``, ``testes_selo`` com o
    retrato do sistema de agora e o pedido ``terminado``, na transacao de ``c``. Aprovado, o nivel obtido e o
    pedido; reprovado, 1 (com um C-2.1 ``falhou`` na primeira ferramenta, se ``casos`` nao vier)."""
    admin_id = admin_id or await nova_pessoa(c, admin=True)
    declaracao_id = declaracao_id or await salvar_declaracao(c, sistema_id, admin_id)
    pedido = await pedir_teste(c, sistema_id, admin_id, declaracao_id, nivel=nivel)
    ferramentas = await acoes_do_sistema(c, sistema_id)
    if casos is None:
        lista = casos_que_aprovam(ferramentas, nivel)
        if not aprovado:
            alvo = str(ferramentas[0])
            lista = [
                item("C-2.1", alvo, "falhou") if (i["caso"], i["ferramenta"]) == ("C-2.1", alvo) else i for i in lista
            ]
    else:
        lista = [dict(i) for i in casos]
    linha = (
        await c.execute(
            text(
                "INSERT INTO testes_selo (pedido_id, sistema_id, declaracao_id, contrato, bateria, nivel_pedido,"
                " resultado, nivel_obtido, sistema_endereco, sistema_tipo_chave, revisao_servidor_id, ferramentas,"
                " casos, nao_testado, chamadas, iniciado_em)"
                " SELECT CAST(:p AS uuid), s.id, CAST(:d AS uuid), CAST(:c AS text), CAST(:b AS text),"
                " CAST(:n AS smallint), CAST(:r AS text), CAST(:o AS smallint), s.endereco, s.tipo_chave,"
                " s.revisao_servidor_id, CAST(:f AS uuid[]), CAST(:casos AS jsonb), CAST(:nt AS jsonb),"
                " CAST(:ch AS integer), now() FROM sistemas s WHERE s.id = :s"
                " RETURNING id, nivel_pedido, nivel_obtido, testado_em, vale_ate"
            ),
            {
                "p": pedido,
                "s": sistema_id,
                "d": declaracao_id,
                "c": contrato.CONTRATO,
                "b": contrato.VERSAO_BATERIA,
                "n": nivel,
                "r": "aprovado" if aprovado else "reprovado",
                "o": nivel if aprovado else 1,
                "f": ferramentas,
                "casos": json.dumps(lista),
                "nt": json.dumps(contrato.nao_testado_gravado()),
                "ch": chamadas if chamadas is not None else contrato.contagem_minima_de_chamadas(lista),
            },
        )
    ).one()
    await c.execute(text("UPDATE pedidos_teste_selo SET estado = 'terminado' WHERE id = :i"), {"i": pedido})
    return TesteGravado(
        uuid.UUID(str(linha.id)),
        pedido,
        declaracao_id,
        int(linha.nivel_pedido),
        int(linha.nivel_obtido),
        linha.testado_em,
        linha.vale_ate,
    )


async def selo_vigente(c: AsyncConnection, sistema_id: uuid.UUID, agora: datetime | None = None) -> Any:
    """A linha de ``selo_vigente`` (nivel, teste_id, testado_em, vale_ate, modo, motivo)."""
    if agora is None:
        return (await c.execute(text("SELECT * FROM selo_vigente(:s)"), {"s": sistema_id})).one()
    return (await c.execute(text("SELECT * FROM selo_vigente(:s, :a)"), {"s": sistema_id, "a": agora})).one()


# --- Operacao e envio com numero -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperacaoDeTeste:
    id: uuid.UUID
    protocolo: str
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ferramenta_id: uuid.UUID
    sistema_id: uuid.UUID
    ficha_id: uuid.UUID
    argumentos: dict[str, Any]


async def criar_operacao(
    c: AsyncConnection,
    ferramenta: Revisada,
    *,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    argumentos: Mapping[str, Any] | None = None,
    repete: uuid.UUID | None = None,
    liberada: bool = True,
    ficha_selo: Mapping[str, Any] | None = None,
    chave_pedido_igual: str | None = None,
) -> OperacaoDeTeste:
    """Uma acao pedida na ``ferramenta``: ordem de servico, operacao (com ``repete_operacao_id`` = ``repete``), ficha
    (com o retrato ``ficha_selo``) e linha da fila; com ``liberada``, a fila passa a ``liberada`` (a pessoa confirmou
    e a espera acabou). Pessoa e aplicativo novos quando nao vierem. ``chave_pedido_igual`` troca a chave que o codigo
    calcularia (so para provar a guarda do banco)."""
    pessoa_id = pessoa_id or await nova_pessoa(c, teste=True)
    aplicativo_id = aplicativo_id or await novo_aplicativo(c)
    args = dict(argumentos or {"para": "caixa-de-teste@exemplo.teste", "texto": "teste"})
    modelo = (
        await c.execute(
            text(
                "INSERT INTO modelos_ordem (nome, descricao, validade_dias, criado_por) VALUES (:n, 'M', 30, 'admin')"
                " RETURNING id"
            ),
            {"n": f"modelo-{uuid.uuid4().hex[:8]}"},
        )
    ).scalar_one()
    ordem = (
        await c.execute(
            text(
                "INSERT INTO ordens_servico (pessoa_id, aplicativo_id, modelo_id, fim)"
                " VALUES (:p, :a, :m, now() + interval '7 days') RETURNING id"
            ),
            {"p": pessoa_id, "a": aplicativo_id, "m": modelo},
        )
    ).scalar_one()
    codigo = codigo_conferencia(ferramenta.sistema_id, ferramenta.nome_original, args)
    linha = (
        await c.execute(
            text(
                "INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
                " ferramenta_id, sistema_id, argumentos, chave_pedido_igual, repete_operacao_id)"
                " VALUES ('CLR-2026-' || lpad(nextval('protocolo_seq')::text, 8, '0'), :c, :p, :a, :o, :f, :s,"
                " CAST(:g AS jsonb), :k, :r) RETURNING id, protocolo"
            ),
            {
                "c": codigo,
                "p": pessoa_id,
                "a": aplicativo_id,
                "o": ordem,
                "f": ferramenta.id,
                "s": ferramenta.sistema_id,
                "g": json.dumps(args),
                "k": chave_pedido_igual or calcular_chave_pedido_igual(pessoa_id, aplicativo_id, ferramenta.id, args),
                "r": repete,
            },
        )
    ).one()
    ficha = (
        await c.execute(
            text(
                "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco, selo)"
                " VALUES (:o, :c, now() + interval '30 minutes', 'medio', CAST(:selo AS jsonb)) RETURNING id"
            ),
            {"o": linha.id, "c": codigo, "selo": None if ficha_selo is None else json.dumps(ficha_selo)},
        )
    ).scalar_one()
    await c.execute(text("INSERT INTO fila_saida (operacao_id, ficha_id) VALUES (:o, :f)"), {"o": linha.id, "f": ficha})
    if liberada:
        await c.execute(
            text("UPDATE fila_saida SET estado = 'liberada', hora_saida = now() WHERE operacao_id = :o"),
            {"o": linha.id},
        )
    return OperacaoDeTeste(
        uuid.UUID(str(linha.id)),
        str(linha.protocolo),
        pessoa_id,
        aplicativo_id,
        ferramenta.id,
        ferramenta.sistema_id,
        uuid.UUID(str(ficha)),
        args,
    )


@dataclass(frozen=True, slots=True)
class Envio:
    operacao_id: uuid.UUID
    chave: uuid.UUID
    nivel: int
    teste_id: uuid.UUID
    repete_operacao_id: uuid.UUID | None


async def gravar_envio(c: AsyncConnection, operacao_id: uuid.UUID) -> Envio:
    """O envio com numero, como a porta de saida grava (com a fila em ``liberada``): nivel e teste do selo vigente;
    o numero e do banco."""
    linha = (
        await c.execute(
            text(
                "INSERT INTO envios_selo (operacao_id, sistema_id, nivel, teste_id)"
                " SELECT o.id, o.sistema_id, v.nivel, v.teste_id FROM operacoes o"
                " CROSS JOIN LATERAL selo_vigente(o.sistema_id) v WHERE o.id = :o"
                " RETURNING operacao_id, chave, nivel, teste_id, repete_operacao_id"
            ),
            {"o": operacao_id},
        )
    ).one()
    return Envio(
        uuid.UUID(str(linha.operacao_id)),
        uuid.UUID(str(linha.chave)),
        int(linha.nivel),
        uuid.UUID(str(linha.teste_id)),
        None if linha.repete_operacao_id is None else uuid.UUID(str(linha.repete_operacao_id)),
    )


async def enviar(c: AsyncConnection, operacao_id: uuid.UUID, fim: str | None = "nao_confirmada") -> Envio:
    """O envio com numero, a fila em ``enviando`` e, com ``fim``, o estado final (``nao_confirmada``, ``enviada``...),
    como o despachante (o dono pode, como ele)."""
    envio = await gravar_envio(c, operacao_id)
    await c.execute(
        text("UPDATE fila_saida SET estado = 'enviando', mudou_por = 'despachante' WHERE operacao_id = :o"),
        {"o": operacao_id},
    )
    if fim is not None:
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = :e, enviada_em = CASE WHEN :e = 'enviada' THEN now() END"
                " WHERE operacao_id = :o"
            ),
            {"e": fim, "o": operacao_id},
        )
    return envio


@dataclass(frozen=True, slots=True)
class Informacao:
    id: int
    contradiz: bool


async def informar(
    c: AsyncConnection,
    operacao_id: uuid.UUID,
    estado: str | None = None,
    *,
    origem: str = "consulta",
    desfecho: str | None = None,
    aviso_id: uuid.UUID | None = None,
    depois: Sequence[Mapping[str, Any]] = (),
    repetida: bool = False,
    objetos: int | None = None,
) -> Informacao:
    """Uma linha de ``informacoes_sistema``. Com ``estado``, o desfecho padrao e ``respondeu``; sem, o padrao e
    ``sem_resposta``."""
    if desfecho is None:
        desfecho = "respondeu" if estado is not None else "sem_resposta"
    linha = (
        await c.execute(
            text(
                "INSERT INTO informacoes_sistema (operacao_id, sistema_id, origem, aviso_id, desfecho, estado,"
                " repetida, objetos, depois) SELECT e.operacao_id, e.sistema_id, :o, :a, :d, :e, :r, :ob,"
                " CAST(:dp AS jsonb) FROM envios_selo e WHERE e.operacao_id = :op RETURNING id, contradiz"
            ),
            {
                "op": operacao_id,
                "o": origem,
                "a": aviso_id,
                "d": desfecho,
                "e": estado,
                "r": repetida,
                "ob": objetos,
                "dp": json.dumps(list(depois)),
            },
        )
    ).one()
    return Informacao(int(linha.id), bool(linha.contradiz))


def retrato(vigente: Any) -> dict[str, Any]:
    """O retrato do selo (``fichas.selo``, ``dados.selo``) de uma linha de ``selo_vigente``."""
    return {
        "nivel": int(vigente.nivel),
        "teste_id": None if vigente.teste_id is None else str(vigente.teste_id),
        "testado_em": None if vigente.testado_em is None else vigente.testado_em.isoformat(),
        "vale_ate": None if vigente.vale_ate is None else vigente.vale_ate.isoformat(),
        "modo": vigente.modo,
        "motivo": vigente.motivo,
    }
