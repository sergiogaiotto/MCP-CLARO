"""Migracao 0006 (Fase 3), contra PostgreSQL 18.7 real: cada CHECK, gatilho, guarda e GRANT.

Os papeis entram por ``SET LOCAL ROLE`` numa transacao do dono do esquema: o ``current_user`` que as guardas olham e
as permissoes que o banco confere sao as do papel. Cada teste cria os proprios sistemas (nomes sorteados), entao
roda junto com os outros testes da sessao. Dois testes usam um banco proprio, migrado ate a 0005 com dados da
Fase 2 e depois ate a 0006.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.config.politica import POLITICA
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine, url_asyncpg
from mcp_claro.versao import VERSAO_MCP
from tests.apoio_terceiros import (
    Revisada,
    cadastrar_terceiro,
    declaradas_revisadas,
    definicao,
    gravar_revisao_do_servidor,
    gravar_sinal,
    impressao_do_servidor,
    por_em_quarentena,
    revisar_ferramenta,
)
from tests.conftest import RAIZ

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

ENTRADA = "mcp_claro_entrada"
DESPACHANTE = "mcp_claro_despachante"
ROTINAS = "mcp_claro_rotinas"
GRAFANA = "grafana_leitor"
PAPEIS = (ENTRADA, DESPACHANTE, ROTINAS)
SEM_PERMISSAO = "permission denied"
QUARENTENA_SO_SAI = "Servidor em quarentena so sai pela nova revisao do ADMIN"


# --- Ajudas ---------------------------------------------------------------------------------------------------


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(banco_migrado, pool_size=3, max_overflow=2)
    try:
        yield engine
    finally:
        await engine.dispose()


async def como(c: AsyncConnection, papel: str | None) -> None:
    if papel:
        assert re.fullmatch(r"[a-z_]+", papel)
        await c.execute(text("SET LOCAL ROLE " + papel))


async def fazer(engine: AsyncEngine, sql: str, *, papel: str | None = None, **params: Any) -> Any:
    """Roda numa transacao propria (como ``papel``, se dado). Devolve a primeira coluna da primeira linha, ou o
    numero de linhas mudadas."""
    async with engine.begin() as c:
        await como(c, papel)
        r = await c.execute(text(sql), params)
        if r.returns_rows:
            linha = r.first()
            return None if linha is None else linha[0]
        return r.rowcount


async def linha(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(text(sql), params)).one()


async def linhas(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as c:
        return list((await c.execute(text(sql), params)).all())


async def recusado(engine: AsyncEngine, sql: str, match: str, *, papel: str | None = None, **params: Any) -> None:
    """O comando e recusado com ``match``. A transacao sempre volta atras: se passar, nada fica gravado."""
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            await como(c, papel)
            with pytest.raises(DBAPIError, match=match):
                await c.execute(text(sql), params)
        finally:
            await transacao.rollback()


@dataclass
class Cena:
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    terceiro_id: uuid.UUID
    t1: Revisada
    t2: Revisada
    proprio_id: uuid.UUID
    p1: Revisada


async def nova_pessoa(c: AsyncConnection) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text("INSERT INTO pessoas (emissor, sujeito) VALUES ('https://login.teste', :s) RETURNING id"),
                    {"s": f"sujeito-{uuid.uuid4().hex}"},
                )
            ).scalar_one()
        )
    )


async def novo_proprio(c: AsyncConnection, situacao: str = "ativo", tipo_chave: str = "por_pessoa") -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp, sondagem,"
                        " sondagem_desfecho, versoes_anunciadas) VALUES (:n, 'http://proprio.teste/mcp', :t, :s, :v,"
                        " '{\"ok\": true}'::jsonb, 'aceito', ARRAY['2026-07-28']) RETURNING id"
                    ),
                    {"n": f"proprio-{uuid.uuid4().hex[:8]}", "t": tipo_chave, "s": situacao, "v": VERSAO_MCP},
                )
            ).scalar_one()
        )
    )


@pytest.fixture
async def cena(dono: AsyncEngine) -> Cena:
    async with dono.begin() as c:
        pessoa = await nova_pessoa(c)
        aplicativo = (
            await c.execute(
                text("INSERT INTO aplicativos (client_id, nome, familia) VALUES (:c, :c, 'teste') RETURNING id"),
                {"c": f"app-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()
        terceiro = await cadastrar_terceiro(c)
        t1 = await revisar_ferramenta(
            c, terceiro, definicao("ler_a", anotacoes={"readOnlyHint": True}), campos_pessoais=["cpf"]
        )
        t2 = await revisar_ferramenta(c, terceiro, definicao("ler_b"))
        proprio = await novo_proprio(c)
        p1 = await revisar_ferramenta(c, proprio, definicao("ler_p", anotacoes={"readOnlyHint": True}))
    return Cena(pessoa, uuid.UUID(str(aplicativo)), terceiro, t1, t2, proprio, p1)


async def revisao_vigente(engine: AsyncEngine, sistema_id: uuid.UUID) -> uuid.UUID:
    async with engine.begin() as c:
        return await gravar_revisao_do_servidor(c, sistema_id)


SQL_REVISAO = (
    "INSERT INTO revisoes_servidor (sistema_id, versao, impressao, declaradas, publicadas, fornecedor, revisado_por)"
    " VALUES (:s, :v, :i, CAST(:d AS jsonb), CAST(:p AS uuid[]), :f, :r) RETURNING id"
)


def params_revisao(
    sistema_id: uuid.UUID,
    declaradas: list[dict[str, Any]] | Any,
    publicadas: list[uuid.UUID | None],
    *,
    versao: int = 1,
    impressao: str | None = None,
    fornecedor: str = "Fornecedor X",
    revisado_por: str = "admin-teste",
) -> dict[str, Any]:
    lista = declaradas if isinstance(declaradas, list) else []
    pares = [(str(d.get("nome")), str(d.get("impressao"))) for d in lista if isinstance(d, dict)]
    return {
        "s": sistema_id,
        "v": versao,
        "i": impressao or impressao_do_servidor(pares),
        "d": json.dumps(declaradas),
        "p": publicadas,
        "f": fornecedor,
        "r": revisado_por,
    }


def declarada(r: Revisada, impressao: str | None = None) -> dict[str, Any]:
    return {"nome": r.nome_original, "impressao": impressao or r.impressao}


# --- A migracao -----------------------------------------------------------------------------------------------


def migrar(p: Parametros, alvo: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.attributes["parametros"] = p
    command.upgrade(cfg, alvo)


def parametros_do_banco(p: Parametros, banco: str) -> Parametros:
    url = p.DATABASE_URL.rsplit("/", 1)[0] + f"/{banco}"
    return Parametros(**(p.model_dump() | {"DATABASE_URL": url}))


async def no_servidor(p: Parametros, comando: str) -> None:
    """CREATE e DROP DATABASE, fora de transacao, com o dono do servidor de teste."""
    conexao = await asyncpg.connect(url_asyncpg(p).replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conexao.execute(comando)
    finally:
        await conexao.close()


@pytest.fixture
async def banco_0005(banco_migrado: Parametros) -> AsyncIterator[tuple[Parametros, AsyncEngine]]:
    """Banco proprio, migrado so ate a 0005 (Fase 2)."""
    nome = f"fase2_{uuid.uuid4().hex[:8]}"
    await no_servidor(banco_migrado, f"CREATE DATABASE {nome}")
    q = parametros_do_banco(banco_migrado, nome)
    engine = criar_engine(q, pool_size=2, max_overflow=0)
    try:
        await asyncio.to_thread(migrar, q, "0005")
        yield q, engine
    finally:
        await engine.dispose()
        await no_servidor(banco_migrado, f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)")


async def test_a_cabeca_e_a_0006(dono: AsyncEngine) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini"))).get_current_head() == "0006"
    assert await fazer(dono, "SELECT version_num FROM alembic_version") == "0006"


def test_downgrade_levanta_erro() -> None:
    caminho = RAIZ / "migrations" / "versions" / "0006_fase3.py"
    spec = importlib.util.spec_from_file_location("migracao_0006", caminho)
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    assert modulo.down_revision == "0005"
    with pytest.raises(RuntimeError, match="nao desfaz migracoes"):
        modulo.downgrade()


SEMEAR_FASE2 = """
WITH p AS (INSERT INTO pessoas (emissor, sujeito) VALUES ('https://login.teste', 'ana') RETURNING id),
a AS (INSERT INTO aplicativos (client_id, nome, familia) VALUES ('app-f2', 'App', 'teste') RETURNING id),
pr AS (INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp)
       VALUES ('proprio-f2', 'http://proprio.teste/mcp', 'por_pessoa', 'ativo', '2026-07-28') RETURNING id),
te AS (INSERT INTO sistemas (nome, endereco, origem, tipo_chave, situacao, versao_mcp)
       VALUES ('terceiro-f2', 'https://terceiro.teste/mcp', 'terceiro', 'compartilhada', 'ativo', '2026-07-28')
       RETURNING id),
qu AS (INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp)
       VALUES ('quarentena-f2', 'http://quarentena.teste/mcp', 'por_pessoa', 'quarentena', '2026-07-28') RETURNING id),
fp AS (INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao, esquema_entrada,
         etiquetas, classe, impressao, revisado_por)
       SELECT id, 'enviar', 1, 'enviar_f2', 'Envia', '{}'::jsonb, '{"destructiveHint": true}'::jsonb, 'muda',
              'sha256:p', 'admin' FROM pr RETURNING id, sistema_id),
ft AS (INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao, esquema_entrada,
         etiquetas, classe, impressao, revisado_por)
       SELECT id, 'ler', 1, 'ler_f2', 'Le', '{}'::jsonb, '{"readOnlyHint": true}'::jsonb, 'le', 'sha256:t', 'admin'
       FROM te RETURNING id),
m AS (INSERT INTO modelos_ordem (nome, descricao, validade_dias, criado_por) VALUES ('m-f2', 'M', 30, 'admin')
      RETURNING id),
o AS (INSERT INTO ordens_servico (pessoa_id, aplicativo_id, modelo_id, fim)
      SELECT p.id, a.id, m.id, now() + interval '7 days' FROM p, a, m RETURNING id, pessoa_id, aplicativo_id),
op AS (INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id, ferramenta_id,
         sistema_id, argumentos, chave_pedido_igual)
       SELECT 'CLR-2026-00000001', 'c1', o.pessoa_id, o.aplicativo_id, o.id, fp.id, fp.sistema_id, '{}'::jsonb, 'k1'
       FROM o, fp RETURNING id),
fi AS (INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco)
       SELECT id, 'c1', now() + interval '30 minutes', 'medio' FROM op RETURNING id, operacao_id),
fs AS (INSERT INTO fila_saida (operacao_id, ficha_id) SELECT operacao_id, id FROM fi RETURNING id),
li AS (INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate)
       SELECT o.pessoa_id, o.aplicativo_id, fp.sistema_id, 'enviar', 3, now() + interval '7 days' FROM o, fp
       RETURNING id)
SELECT (SELECT count(*) FROM fs) + (SELECT count(*) FROM li) + (SELECT count(*) FROM ft)
"""


async def test_migracao_sobre_dados_da_fase_2(banco_0005: tuple[Parametros, AsyncEngine]) -> None:
    """A 0006 roda sobre um banco com dados da Fase 2; antes dela, as tres saidas indevidas da quarentena existem."""
    q, engine = banco_0005
    assert await fazer(engine, SEMEAR_FASE2) == 3

    # Na 0005, as tres saidas indevidas da quarentena passam (a critica as confirmou no codigo).
    async with engine.begin() as c:
        ids = [
            (
                await c.execute(
                    text(
                        "INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp)"
                        " VALUES (:n, 'http://buraco.teste/mcp', 'por_pessoa', 'quarentena', '2026-07-28') RETURNING id"
                    ),
                    {"n": f"buraco-{i}"},
                )
            ).scalar_one()
            for i in range(3)
        ]
    await fazer(engine, "UPDATE sistemas SET situacao = 'pendente' WHERE id = :i", papel=ENTRADA, i=ids[0])
    await fazer(engine, "UPDATE sistemas SET situacao = 'recusado' WHERE id = :i", papel=DESPACHANTE, i=ids[1])
    await fazer(engine, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :i", papel=DESPACHANTE, i=ids[1])
    await fazer(
        engine, "UPDATE sistemas SET endereco = 'http://outro.teste/mcp' WHERE id = :i", papel=ENTRADA, i=ids[2]
    )
    situacoes = await fazer(engine, "SELECT array_agg(situacao ORDER BY nome) FROM sistemas WHERE nome LIKE 'buraco-%'")
    assert situacoes == ["pendente", "ativo", "pendente"]

    await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, "SELECT version_num FROM alembic_version") == "0006"

    # Dados da Fase 2 inteiros; colunas novas vazias; a revisada antiga de terceiro igual as novas.
    contagens = await linha(
        engine,
        "SELECT (SELECT count(*) FROM operacoes) AS op, (SELECT count(*) FROM fichas) AS fi,"
        " (SELECT count(*) FROM fila_saida) AS fs, (SELECT count(*) FROM limites) AS li,"
        " (SELECT count(*) FROM sistemas WHERE revisao_servidor_id IS NOT NULL OR sondagem_desfecho IS NOT NULL"
        "  OR versoes_anunciadas IS NOT NULL OR conferencias_sem_resposta <> 0) AS novas,"
        " (SELECT count(*) FROM fichas WHERE sinais IS NOT NULL) AS sinais",
    )
    assert tuple(contagens) == (1, 1, 1, 1, 0, 0)
    revisadas = {
        str(n): (bool(t), e, d)
        for n, t, e, d in await linhas(
            engine, "SELECT nome_original, texto_de_fora, etiquetas, definicao_declarada FROM ferramentas_revisadas"
        )
    }
    assert revisadas == {"enviar": (False, {"destructiveHint": True}, None), "ler": (True, {}, None)}

    # A ficha antiga segue as regras de sempre, com os sinais fixos.
    await fazer(
        engine,
        "UPDATE fichas SET decisao = 'recusada', decidida_em = now(), decidida_por = (SELECT id FROM pessoas)",
        papel=ENTRADA,
    )
    await recusado(engine, "UPDATE fichas SET sinais = '{}'::jsonb", "os sinais de uma ficha nao mudam")

    # O sistema que ja estava em quarentena antes da 0006 agora so sai pela revisao nova.
    quarentena = await fazer(engine, "SELECT id FROM sistemas WHERE nome = 'quarentena-f2'")
    await recusado(
        engine,
        "UPDATE sistemas SET situacao = 'pendente' WHERE id = :i",
        QUARENTENA_SO_SAI,
        papel=ENTRADA,
        i=quarentena,
    )
    await recusado(
        engine,
        "UPDATE sistemas SET situacao = 'recusado' WHERE id = :i",
        QUARENTENA_SO_SAI,
        papel=DESPACHANTE,
        i=quarentena,
    )
    await fazer(
        engine, "UPDATE sistemas SET endereco = 'http://novo.teste/mcp' WHERE id = :i", papel=ENTRADA, i=quarentena
    )
    assert await fazer(engine, "SELECT situacao FROM sistemas WHERE id = :i", i=quarentena) == "quarentena"


async def test_migracao_para_com_terceiro_antigo_de_chave_de_programa(
    banco_0005: tuple[Parametros, AsyncEngine],
) -> None:
    q, engine = banco_0005
    await fazer(
        engine,
        "INSERT INTO sistemas (nome, endereco, origem, tipo_chave) VALUES ('robo-de-fora', 'https://r.teste/mcp',"
        " 'terceiro', 'programa')",
    )
    with pytest.raises(Exception, match="Servidores de terceiros com chave de programa: robo-de-fora"):
        await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, "SELECT version_num FROM alembic_version") == "0005"
    # Com o tipo de chave trocado, a migracao passa.
    await fazer(engine, "UPDATE sistemas SET tipo_chave = 'compartilhada' WHERE nome = 'robo-de-fora'")
    await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, "SELECT version_num FROM alembic_version") == "0006"


# --- REVISAO_SERVIDOR -----------------------------------------------------------------------------------------


async def test_despachante_grava_a_revisao_e_a_torna_vigente(dono: AsyncEngine, cena: Cena) -> None:
    """O caminho da conclusao: numa transacao so, o despachante grava a revisao (o gatilho le com as permissoes
    dele) e a torna vigente. A hora da revisao e a do banco, mesmo que o comando traga outra."""
    declaradas = [declarada(cena.t1), declarada(cena.t2)]
    async with dono.begin() as c:
        await como(c, DESPACHANTE)
        revisao, na_hora = (
            await c.execute(
                text(
                    "INSERT INTO revisoes_servidor (sistema_id, versao, impressao, declaradas, publicadas, fornecedor,"
                    " revisado_por, revisado_em) VALUES (:s, :v, :i, CAST(:d AS jsonb), CAST(:p AS uuid[]), :f, :r,"
                    " now() - interval '3 days') RETURNING id, revisado_em = now()"
                ),
                params_revisao(cena.terceiro_id, declaradas, [cena.t1.id]),
            )
        ).one()
        await c.execute(
            text("UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s"), {"r": revisao, "s": cena.terceiro_id}
        )
    assert na_hora is True
    r = await linha(
        dono,
        "SELECT s.revisao_servidor_id, rs.versao, rs.publicadas, rs.declaradas FROM sistemas s"
        " JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id WHERE s.id = :s",
        s=cena.terceiro_id,
    )
    assert (r.revisao_servidor_id, r.versao, r.publicadas, r.declaradas) == (revisao, 1, [cena.t1.id], declaradas)
    for papel in (ENTRADA, ROTINAS):
        await recusado(
            dono, SQL_REVISAO, SEM_PERMISSAO, papel=papel, **params_revisao(cena.terceiro_id, declaradas, [], versao=2)
        )


@pytest.mark.parametrize("papel", [None, *PAPEIS])
async def test_revisao_do_servidor_e_so_de_acrescentar(dono: AsyncEngine, cena: Cena, papel: str | None) -> None:
    revisao = await revisao_vigente(dono, cena.terceiro_id)
    esperado = "so de acrescentar" if papel is None else SEM_PERMISSAO
    await recusado(
        dono, "UPDATE revisoes_servidor SET fornecedor = 'Outro' WHERE id = :r", esperado, papel=papel, r=revisao
    )
    await recusado(
        dono, "UPDATE revisoes_servidor SET publicadas = '{}' WHERE id = :r", esperado, papel=papel, r=revisao
    )
    await recusado(dono, "DELETE FROM revisoes_servidor WHERE id = :r", esperado, papel=papel, r=revisao)
    await recusado(dono, "TRUNCATE revisoes_servidor CASCADE", esperado, papel=papel)


async def test_gatilhos_so_de_acrescentar_estao_no_catalogo(dono: AsyncEngine) -> None:
    """TRUNCATE de revisoes_servidor ja para na chave estrangeira de sistemas (ou, com CASCADE, nos gatilhos da
    fila): o gatilho proprio fica como barreira da tabela e e conferido aqui."""
    gatilhos = {
        str(nome): str(definicao)
        for nome, definicao in await linhas(
            dono,
            "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger"
            " WHERE tgrelid IN ('revisoes_servidor'::regclass, 'sinais'::regclass) AND NOT tgisinternal",
        )
    }
    assert "BEFORE TRUNCATE ON public.revisoes_servidor" in gatilhos["revisoes_servidor_sem_truncate"]
    assert "BEFORE DELETE OR UPDATE ON public.revisoes_servidor" in gatilhos["revisoes_servidor_sem_update_delete"]
    assert "BEFORE TRUNCATE ON public.sinais" in gatilhos["sinais_sem_truncate"]
    for nome in ("revisoes_servidor_sem_truncate", "revisoes_servidor_sem_update_delete", "sinais_sem_truncate"):
        assert "extrato_recusa_mudanca()" in gatilhos[nome]


async def test_revisao_do_servidor_recusa_o_que_nao_foi_revisto_ou_mudou(dono: AsyncEngine, cena: Cena) -> None:
    certas = [declarada(cena.t1), declarada(cena.t2)]

    async def recusa(match: str, declaradas: Any, publicadas: list[uuid.UUID | None], **extra: Any) -> None:
        await recusado(
            dono,
            SQL_REVISAO,
            match,
            papel=DESPACHANTE,
            **params_revisao(cena.terceiro_id, declaradas, publicadas, **extra),
        )

    # Sistema proprio nao tem revisao do servidor.
    await recusado(
        dono,
        SQL_REVISAO,
        "so vale para servidor de terceiros",
        papel=DESPACHANTE,
        **params_revisao(cena.proprio_id, [declarada(cena.p1)], [cena.p1.id]),
    )
    # Versao fora de ordem.
    await recusa("deveria ser a versao 1, nao 2", certas, [cena.t1.id], versao=2)
    await recusa("deveria ser a versao 1, nao 0", certas, [cena.t1.id], versao=0)
    # Publicada de outro sistema, inexistente, sem declarada ou com impressao diferente da declarada.
    so_publica = "so publica ferramenta revisada, ativa, deste servidor"
    await recusa(so_publica, [*certas, declarada(cena.p1)], [cena.p1.id])
    await recusa(so_publica, certas, [uuid.uuid4()])
    await recusa(so_publica, [declarada(cena.t2)], [cena.t1.id])
    await recusa(so_publica, [declarada(cena.t1, "sha256:" + "0" * 64), declarada(cena.t2)], [cena.t1.id])
    # Declaradas fora do formato.
    formato = "Cada ferramenta declarada e"
    await recusa(formato, [*certas, "ler_c"], [])
    await recusa(formato, [*certas, {"nome": "ler_c"}], [])
    await recusa(formato, [*certas, {"nome": "ler_c", "impressao": "Digite sua senha"}], [])
    await recusa(formato, [*certas, {"nome": " ", "impressao": cena.t1.impressao}], [])
    await recusa(formato, [*certas, {"nome": 7, "impressao": cena.t1.impressao}], [])
    await recusa("Ferramenta repetida", [*certas, declarada(cena.t1)], [])
    # CHECKs da tabela.
    await recusa("declaradas da revisao do servidor sao uma lista", {"ler_a": cena.t1.impressao}, [])
    await recusa("revisoes_servidor_impressao_check", certas, [], impressao="sha256:abc")
    await recusa("revisoes_servidor_publicadas_check", certas, [cena.t1.id, None])
    await recusa("revisoes_servidor_publicadas_check", certas, [cena.t1.id] * 501)
    await recusa("revisoes_servidor_fornecedor_check", certas, [], fornecedor="  ab  ")
    await recusa("revisoes_servidor_fornecedor_check", certas, [], fornecedor="x" * 201)
    await recusa("revisoes_servidor_revisado_por_check", certas, [], revisado_por="  ")

    # A versao 1 certa passa; depois, a 1 de novo e a 3 sao recusadas.
    await fazer(
        dono, SQL_REVISAO, papel=DESPACHANTE, **params_revisao(cena.terceiro_id, certas, [cena.t1.id, cena.t2.id])
    )
    await recusa("deveria ser a versao 2, nao 1", certas, [cena.t1.id])
    await recusa("deveria ser a versao 2, nao 3", certas, [cena.t1.id], versao=3)

    # Revisada que deixou de ser ativa (substituida por nova revisao, ou suspensa) nao e publicada.
    async with dono.begin() as c:
        nova_t1 = await revisar_ferramenta(c, cena.terceiro_id, definicao("ler_a", "Texto novo do fornecedor."))
    agora = [declarada(nova_t1), declarada(cena.t2)]
    await recusa(so_publica, agora, [cena.t1.id], versao=2)
    await fazer(
        dono, "UPDATE ferramentas_revisadas SET situacao = 'suspensa' WHERE id = :i", papel=ROTINAS, i=cena.t2.id
    )
    await recusa(so_publica, agora, [cena.t2.id], versao=2)
    await fazer(dono, SQL_REVISAO, papel=DESPACHANTE, **params_revisao(cena.terceiro_id, agora, [nova_t1.id], versao=2))


async def test_revisao_vigente_e_do_proprio_sistema_e_a_mais_recente(dono: AsyncEngine, cena: Cena) -> None:
    v1 = await revisao_vigente(dono, cena.terceiro_id)
    v2 = await revisao_vigente(dono, cena.terceiro_id)
    assert await fazer(dono, "SELECT revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id) == v2
    mais_recente = "sempre a mais recente dele"
    # Uma revisao antiga nao volta a valer, nem pelo dono.
    for papel in (None, DESPACHANTE):
        await recusado(
            dono,
            "UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s",
            mais_recente,
            papel=papel,
            r=v1,
            s=cena.terceiro_id,
        )
    # Revisao de outro terceiro, ou que nao existe, tambem nao.
    async with dono.begin() as c:
        outro = await cadastrar_terceiro(c)
        await revisar_ferramenta(c, outro, definicao("ler_x"))
        do_outro = await gravar_revisao_do_servidor(c, outro, vigente=False)
    for alvo in (do_outro, uuid.uuid4()):
        await recusado(
            dono,
            "UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s",
            mais_recente,
            papel=DESPACHANTE,
            r=alvo,
            s=cena.terceiro_id,
        )
    # No cadastro (INSERT, sem a guarda), a chave estrangeira prende ao proprio sistema e o CHECK, a terceiros.
    await recusado(
        dono,
        "INSERT INTO sistemas (nome, endereco, origem, tipo_chave, revisao_servidor_id)"
        " VALUES (:n, 'https://t.teste/mcp', 'terceiro', 'por_pessoa', :r)",
        "sistemas_revisao_do_proprio_sistema",
        n=f"t-{uuid.uuid4().hex[:8]}",
        r=do_outro,
    )
    await recusado(
        dono,
        "INSERT INTO sistemas (nome, endereco, origem, tipo_chave, revisao_servidor_id)"
        " VALUES (:n, 'http://p.teste/mcp', 'proprio', 'por_pessoa', :r)",
        "sistemas_revisao_so_de_terceiro",
        n=f"p-{uuid.uuid4().hex[:8]}",
        r=do_outro,
    )
    # Nulo passa (so esconde). Depois, a mais recente nao volta a valer noutra transacao, nem pelo dono: so a
    # conclusao, que grava a revisao e a torna vigente de uma vez, publica de novo.
    await fazer(
        dono, "UPDATE sistemas SET revisao_servidor_id = NULL WHERE id = :s", papel=DESPACHANTE, s=cena.terceiro_id
    )
    for papel in (None, DESPACHANTE):
        await recusado(
            dono,
            "UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s",
            "gravada na mesma transacao",
            papel=papel,
            r=v2,
            s=cena.terceiro_id,
        )
    v3 = await revisao_vigente(dono, cena.terceiro_id)
    assert await fazer(dono, "SELECT revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id) == v3
    # Na mesma transacao, duas revisoes gravadas: so a mais recente vale.
    async with dono.connect() as c:
        transacao = await c.begin()
        try:
            v4 = await gravar_revisao_do_servidor(c, cena.terceiro_id, vigente=False)
            await gravar_revisao_do_servidor(c, cena.terceiro_id, vigente=False)
            await como(c, DESPACHANTE)
            with pytest.raises(DBAPIError, match=mais_recente):
                await c.execute(
                    text("UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s"), {"r": v4, "s": cena.terceiro_id}
                )
        finally:
            await transacao.rollback()


async def test_checks_que_a_guarda_ja_cobre_estao_no_catalogo(dono: AsyncEngine) -> None:
    """O gatilho de nascimento recusa antes (versao fora de ordem, declaradas que nao sao lista); os CHECKs ficam
    como segunda barreira e estao no catalogo."""
    checks = {
        str(nome): str(corpo)
        for nome, corpo in await linhas(
            dono,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'revisoes_servidor'::regclass AND contype = 'c'",
        )
    }
    assert "(versao >= 1)" in checks["revisoes_servidor_versao_check"]
    assert "jsonb_typeof(declaradas) = 'array'" in checks["revisoes_servidor_declaradas_check"]


# --- ESTRANHOS ------------------------------------------------------------------------------------------------

SQL_REVISADA = (
    "INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao, esquema_entrada,"
    " etiquetas, classe, impressao, revisado_por, texto_de_fora, definicao_declarada)"
    " VALUES (:s, :n, 1, :nv, 'Descricao do ADMIN', '{}'::jsonb, '{\"readOnlyHint\": true}'::jsonb, 'le',"
    " 'sha256:x', 'admin', :tf, CAST(:dd AS jsonb)) RETURNING texto_de_fora, etiquetas, definicao_declarada"
)


async def test_revisada_de_terceiro_nasce_com_texto_de_fora_e_sem_dicas_do_fornecedor(
    dono: AsyncEngine, cena: Cena
) -> None:
    assert tuple(
        await linha(dono, "SELECT texto_de_fora, etiquetas FROM ferramentas_revisadas WHERE id = :i", i=cena.t1.id)
    ) == (True, {})
    assert tuple(
        await linha(dono, "SELECT texto_de_fora, etiquetas FROM ferramentas_revisadas WHERE id = :i", i=cena.p1.id)
    ) == (False, {"readOnlyHint": True})

    async def revisar(sistema_id: uuid.UUID, texto_de_fora: bool) -> Any:
        # A entrada grava a revisao (INSERT da tabela inteira, que cobre as colunas novas).
        async with dono.begin() as c:
            await como(c, ENTRADA)
            nome = f"f_{uuid.uuid4().hex[:8]}"
            return (
                await c.execute(
                    text(SQL_REVISADA),
                    {"s": sistema_id, "n": nome, "nv": nome, "tf": texto_de_fora, "dd": json.dumps({"name": nome})},
                )
            ).one()

    n = await revisar(cena.terceiro_id, False)
    assert (n.texto_de_fora, n.etiquetas, n.definicao_declarada["name"].startswith("f_")) == (True, {}, True)
    n = await revisar(cena.proprio_id, False)
    assert (n.texto_de_fora, n.etiquetas) == (False, {"readOnlyHint": True})
    n = await revisar(cena.proprio_id, True)  # D17: o ADMIN marca a propria que traz texto de fora
    assert n.texto_de_fora is True
    await recusado(
        dono,
        SQL_REVISADA,
        "texto_de_fora",
        papel=ENTRADA,
        s=cena.proprio_id,
        n="h",
        nv=f"h_{uuid.uuid4().hex[:8]}",
        tf=None,
        dd=None,
    )
    await recusado(
        dono,
        SQL_REVISADA,
        "ferramentas_revisadas_definicao_declarada_check",
        s=cena.proprio_id,
        n="g",
        nv=f"g_{uuid.uuid4().hex[:8]}",
        tf=False,
        dd="[]",
    )
    # Ninguem muda a marca nem a definicao guardada: mudar e revisar de novo.
    for papel in PAPEIS:
        for sql in (
            "UPDATE ferramentas_revisadas SET texto_de_fora = false WHERE id = :i",
            "UPDATE ferramentas_revisadas SET definicao_declarada = NULL WHERE id = :i",
            "UPDATE ferramentas_revisadas SET etiquetas = '{}' WHERE id = :i",
        ):
            await recusado(dono, sql, SEM_PERMISSAO, papel=papel, i=cena.t1.id)


# --- TRES_SINAIS ----------------------------------------------------------------------------------------------

SQL_SINAL = (
    "INSERT INTO sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id, visto_em)"
    " VALUES (:p, :a, :t, :s, :f, now() - interval '3 days') RETURNING id, visto_em, (visto_em = now()) AS agora"
)


async def test_entrada_grava_e_le_sinais_com_a_hora_do_banco(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.begin() as c:
        await como(c, ENTRADA)
        r = (
            await c.execute(
                text(SQL_SINAL),
                {
                    "p": cena.pessoa_id,
                    "a": cena.aplicativo_id,
                    "t": "texto_de_fora",
                    "s": cena.terceiro_id,
                    "f": cena.t1.id,
                },
            )
        ).one()
        assert r.agora is True  # a entrada nao grava sinal com hora antiga
        vistos = await c.execute(text("SELECT count(*) FROM sinais WHERE pessoa_id = :p"), {"p": cena.pessoa_id})
        assert vistos.scalar_one() == 1
    await recusado(
        dono,
        SQL_SINAL,
        "sinais_tipo_check",
        papel=ENTRADA,
        p=cena.pessoa_id,
        a=cena.aplicativo_id,
        t="outro",
        s=cena.terceiro_id,
        f=cena.t1.id,
    )
    await recusado(
        dono,
        SQL_SINAL,
        "ferramenta do proprio sistema",
        papel=ENTRADA,
        p=cena.pessoa_id,
        a=cena.aplicativo_id,
        t="dado_privado",
        s=cena.terceiro_id,
        f=cena.p1.id,
    )


async def test_entrada_grava_os_sinais_de_uma_leitura_num_comando(dono: AsyncEngine, cena: Cena) -> None:
    """O INSERT ... SELECT de ``regras/sinais.registrar`` (G4a), como a entrada: os dois sinais de uma leitura de
    terceiro com campos pessoais."""
    async with dono.begin() as c:
        await como(c, ENTRADA)
        gravados = (
            await c.execute(
                text(
                    "INSERT INTO sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id)"
                    " SELECT :p, :a, t.tipo, r.sistema_id, r.id FROM ferramentas_revisadas r"
                    " JOIN sistemas s ON s.id = r.sistema_id"
                    " CROSS JOIN LATERAL (VALUES ('texto_de_fora', s.origem = 'terceiro' OR r.texto_de_fora),"
                    "  ('dado_privado', cardinality(r.campos_pessoais) > 0)) AS t (tipo, vale)"
                    " WHERE r.id = :f AND t.vale RETURNING tipo"
                ),
                {"p": cena.pessoa_id, "a": cena.aplicativo_id, "f": cena.t1.id},
            )
        ).all()
        alerta = (
            await c.execute(
                text(
                    "SELECT DISTINCT ON (tipo) tipo, f.nome_vitrine, s.nome, visto_em FROM sinais"
                    " JOIN ferramentas_revisadas f ON f.id = sinais.ferramenta_id"
                    " JOIN sistemas s ON s.id = sinais.sistema_id"
                    " WHERE pessoa_id = :p AND aplicativo_id = :a AND visto_em > now() - make_interval(mins => 240)"
                    " ORDER BY tipo, visto_em DESC"
                ),
                {"p": cena.pessoa_id, "a": cena.aplicativo_id},
            )
        ).all()
    assert sorted(t for (t,) in gravados) == ["dado_privado", "texto_de_fora"]
    assert [a.tipo for a in alerta] == ["dado_privado", "texto_de_fora"]


async def test_sinal_nao_muda_e_so_sai_depois_da_maior_janela(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.begin() as c:
        ids = {}
        for nome, dias in (("agora", 0), ("seis_dias", 6), ("oito_dias", 8)):
            ids[nome] = await gravar_sinal(
                c,
                pessoa_id=cena.pessoa_id,
                aplicativo_id=cena.aplicativo_id,
                tipo="dado_privado",
                ferramenta_id=cena.t1.id,
                visto_em=(await c.execute(text("SELECT now() - make_interval(days => :d)"), {"d": dias})).scalar_one(),
            )
    for papel, esperado in ((None, "nao muda"), (ENTRADA, SEM_PERMISSAO), (ROTINAS, SEM_PERMISSAO)):
        await recusado(dono, "UPDATE sinais SET visto_em = now() WHERE id = :i", esperado, papel=papel, i=ids["agora"])
    await recusado(dono, "TRUNCATE sinais", "so de acrescentar")
    for papel in (None, ROTINAS):
        for nome in ("agora", "seis_dias"):
            await recusado(dono, "DELETE FROM sinais WHERE id = :i", "maior janela", papel=papel, i=ids[nome])
    await recusado(dono, "DELETE FROM sinais WHERE id = :i", SEM_PERMISSAO, papel=ENTRADA, i=ids["oito_dias"])
    apagados = await fazer(
        dono,
        "DELETE FROM sinais WHERE visto_em < now() - interval '10080 minutes' AND pessoa_id = :p",
        papel=None,
        p=cena.pessoa_id,
    )
    assert apagados == 1
    restam = await fazer(dono, "SELECT array_agg(id ORDER BY id) FROM sinais WHERE pessoa_id = :p", p=cena.pessoa_id)
    assert restam == sorted([ids["agora"], ids["seis_dias"]])


async def test_rotinas_apagam_o_sinal_velho(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.begin() as c:
        velho = await gravar_sinal(
            c,
            pessoa_id=cena.pessoa_id,
            aplicativo_id=cena.aplicativo_id,
            tipo="texto_de_fora",
            ferramenta_id=cena.t2.id,
            visto_em=(await c.execute(text("SELECT now() - interval '8 days'"))).scalar_one(),
        )
    assert (
        await fazer(
            dono, "DELETE FROM sinais WHERE id = :i AND visto_em < now() - interval '7 days'", papel=ROTINAS, i=velho
        )
        == 1
    )


@pytest.mark.parametrize(
    ("papel", "sql"),
    [
        (DESPACHANTE, "SELECT count(*) FROM sinais"),
        (DESPACHANTE, "DELETE FROM sinais WHERE id = 0"),
        (DESPACHANTE, SQL_SINAL),
        (ROTINAS, "SELECT pessoa_id FROM sinais"),
        (ROTINAS, SQL_SINAL),
        (GRAFANA, "SELECT count(*) FROM public.sinais"),
    ],
)
async def test_quem_nao_mexe_nos_sinais(dono: AsyncEngine, cena: Cena, papel: str, sql: str) -> None:
    await recusado(
        dono,
        sql,
        SEM_PERMISSAO,
        papel=papel,
        p=cena.pessoa_id,
        a=cena.aplicativo_id,
        t="texto_de_fora",
        s=cena.terceiro_id,
        f=cena.t1.id,
    )


async def test_janela_do_gatilho_e_a_maior_da_politica(dono: AsyncEngine) -> None:
    corpo = await fazer(dono, "SELECT pg_get_functiondef('sinais_guarda'::regproc)")
    minutos = re.findall(r"interval '(\d+) minutes'", corpo)
    assert minutos == [str(POLITICA["THREE_SIGNALS_WINDOW_MINUTES"].maximo)] == ["10080"]


# --- fichas.sinais --------------------------------------------------------------------------------------------


async def operacao(dono: AsyncEngine, cena: Cena) -> uuid.UUID:
    return uuid.UUID(
        str(
            await fazer(
                dono,
                "WITH m AS (INSERT INTO modelos_ordem (nome, descricao, validade_dias, criado_por)"
                "  VALUES (:m, 'M', 30, 'admin') RETURNING id),"
                " o AS (INSERT INTO ordens_servico (pessoa_id, aplicativo_id, modelo_id, fim)"
                "  SELECT :p, :a, m.id, now() + interval '7 days' FROM m RETURNING id)"
                " INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
                "  ferramenta_id, sistema_id, argumentos, chave_pedido_igual)"
                " SELECT :pr, 'c1', :p, :a, o.id, :f, :s, '{}'::jsonb, 'k' FROM o RETURNING id",
                m=f"m-{uuid.uuid4().hex[:8]}",
                p=cena.pessoa_id,
                a=cena.aplicativo_id,
                pr=f"CLR-T-{uuid.uuid4().hex[:10]}",
                f=cena.t1.id,
                s=cena.terceiro_id,
            )
        )
    )


SQL_FICHA = (
    "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco, sinais)"
    " VALUES (:o, 'c1', now() + interval '30 minutes', 'medio', CAST(:si AS jsonb)) RETURNING id"
)
RETRATO = {"janela_minutos": 240, "vistos": [{"sinal": "texto_de_fora", "ferramenta": "ler_a", "sistema": "t"}]}


async def test_ficha_nasce_com_o_retrato_dos_sinais_e_ele_nao_muda(dono: AsyncEngine, cena: Cena) -> None:
    ficha = await fazer(dono, SQL_FICHA, papel=ENTRADA, o=await operacao(dono, cena), si=json.dumps(RETRATO))
    assert await fazer(dono, "SELECT sinais FROM fichas WHERE id = :i", i=ficha) == RETRATO
    await recusado(dono, SQL_FICHA, "fichas_sinais_check", papel=ENTRADA, o=await operacao(dono, cena), si="[]")
    for papel, esperado in (
        (None, "os sinais de uma ficha nao mudam"),
        (ENTRADA, SEM_PERMISSAO),
        (ROTINAS, SEM_PERMISSAO),
    ):
        await recusado(dono, "UPDATE fichas SET sinais = NULL WHERE id = :i", esperado, papel=papel, i=ficha)
    # O resto da guarda da 0005 continua valendo.
    await recusado(dono, "UPDATE fichas SET risco = 'baixo' WHERE id = :i", "nao mudam", i=ficha)
    await recusado(
        dono,
        "UPDATE fichas SET decisao = 'confirmada', decidida_em = now() WHERE id = :i",
        "As rotinas so vencem",
        papel=ROTINAS,
        i=ficha,
    )
    await fazer(
        dono,
        "UPDATE fichas SET decisao = 'recusada', decidida_em = now(), decidida_por = :p WHERE id = :i",
        papel=ENTRADA,
        p=cena.pessoa_id,
        i=ficha,
    )
    await recusado(dono, "UPDATE fichas SET decisao = 'vencida' WHERE id = :i", "a decisao nao muda", i=ficha)


# --- PEDIDOS --------------------------------------------------------------------------------------------------


async def test_indice_dos_pedidos_barrados(dono: AsyncEngine) -> None:
    definicao_indice = await fazer(
        dono, "SELECT indexdef FROM pg_indexes WHERE indexname = 'extrato_pedido_barrado_idx'"
    )
    assert "(sistema_id, criado_em DESC)" in definicao_indice
    for tipo in ("leitura_sistema_pediu_dados", "programa_leitura_sistema_pediu_dados", "acao_sistema_pediu_dados"):
        assert f"'{tipo}'" in definicao_indice
    particoes = await fazer(
        dono, "SELECT count(*) FROM pg_inherits WHERE inhparent = 'extrato_pedido_barrado_idx'::regclass"
    )
    assert particoes >= 4  # o mes passado, o atual e os proximos (0001)


# --- GUARDA_SISTEMAS ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("papel", [None, *PAPEIS])
async def test_origem_nao_muda_nem_pelo_dono(dono: AsyncEngine, cena: Cena, papel: str | None) -> None:
    esperado = "A origem de um sistema nao muda" if papel is None else SEM_PERMISSAO
    await recusado(
        dono, "UPDATE sistemas SET origem = 'proprio' WHERE id = :s", esperado, papel=papel, s=cena.terceiro_id
    )
    await recusado(
        dono, "UPDATE sistemas SET origem = 'terceiro' WHERE id = :s", esperado, papel=papel, s=cena.proprio_id
    )


async def test_terceiro_endereco_e_autorizacao_so_mudam_de_vazio_para_um_valor(dono: AsyncEngine, cena: Cena) -> None:
    """Critica M1: mudar o destino de um terceiro mandaria as chaves guardadas a outro lugar."""
    revisao = await revisao_vigente(dono, cena.terceiro_id)
    fixo = "o endereco e a autorizacao nao mudam depois de preenchidos"
    for papel in (None, ENTRADA):
        await recusado(
            dono,
            "UPDATE sistemas SET endereco = 'https://outro.teste/mcp' WHERE id = :s",
            fixo,
            papel=papel,
            s=cena.terceiro_id,
        )
    # Primeira configuracao (de vazio para um valor): aceita, sem voltar a pendente nem perder a revisao.
    config = json.dumps({"authorization_endpoint": "https://login.terceiro.teste/auth"})
    await fazer(
        dono,
        "UPDATE sistemas SET autorizacao = CAST(:a AS jsonb) WHERE id = :s",
        papel=ENTRADA,
        a=config,
        s=cena.terceiro_id,
    )
    r = await linha(dono, "SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id)
    assert tuple(r) == ("ativo", revisao)
    # Depois, nem outro valor nem vazio (vazio e depois outro valor seria o mesmo desvio em dois passos).
    for papel in (None, ENTRADA):
        for valor in ('{"authorization_endpoint": "https://outro.teste/auth"}', None):
            await recusado(
                dono,
                "UPDATE sistemas SET autorizacao = CAST(:a AS jsonb) WHERE id = :s",
                fixo,
                papel=papel,
                a=valor,
                s=cena.terceiro_id,
            )
    # JSON null conta como vazio.
    async with dono.begin() as c:
        outro = await cadastrar_terceiro(c)
        await c.execute(text("UPDATE sistemas SET autorizacao = 'null'::jsonb WHERE id = :s"), {"s": outro})
    await fazer(
        dono, "UPDATE sistemas SET autorizacao = CAST(:a AS jsonb) WHERE id = :s", papel=ENTRADA, a=config, s=outro
    )
    # Num sistema proprio, nada disso muda (Fases 1 e 2).
    await fazer(
        dono,
        "UPDATE sistemas SET autorizacao = CAST(:a AS jsonb) WHERE id = :s",
        papel=ENTRADA,
        a=config,
        s=cena.proprio_id,
    )
    await fazer(dono, "UPDATE sistemas SET autorizacao = NULL WHERE id = :s", papel=ENTRADA, s=cena.proprio_id)


async def test_proprio_com_endereco_novo_volta_a_pendente_e_apaga_a_sondagem(dono: AsyncEngine, cena: Cena) -> None:
    await fazer(
        dono, "UPDATE sistemas SET endereco = 'http://novo.teste/mcp' WHERE id = :s", papel=ENTRADA, s=cena.proprio_id
    )
    r = await linha(
        dono,
        "SELECT situacao, versao_mcp, sondagem, sondagem_desfecho, versoes_anunciadas FROM sistemas WHERE id = :s",
        s=cena.proprio_id,
    )
    assert tuple(r) == ("pendente", None, None, None, None)


async def test_terceiro_com_tipo_de_chave_novo_volta_a_pendente_e_perde_a_revisao(
    dono: AsyncEngine, cena: Cena
) -> None:
    await revisao_vigente(dono, cena.terceiro_id)
    await fazer(
        dono, "UPDATE sistemas SET conferencias_sem_resposta = 1 WHERE id = :s", papel=ROTINAS, s=cena.terceiro_id
    )
    await fazer(
        dono, "UPDATE sistemas SET tipo_chave = 'compartilhada' WHERE id = :s", papel=ENTRADA, s=cena.terceiro_id
    )
    r = await linha(
        dono,
        "SELECT situacao, versao_mcp, sondagem_desfecho, revisao_servidor_id, conferencias_sem_resposta FROM sistemas"
        " WHERE id = :s",
        s=cena.terceiro_id,
    )
    assert tuple(r) == ("pendente", None, None, None, 0)


@pytest.mark.parametrize("papel", [None, DESPACHANTE, ROTINAS])
async def test_entrar_em_quarentena_apaga_a_revisao(dono: AsyncEngine, cena: Cena, papel: str | None) -> None:
    """A guarda apaga a revisao vigente sem GRANT de coluna: as rotinas nao tem UPDATE em revisao_servidor_id."""
    base = await revisao_vigente(dono, cena.terceiro_id)
    await fazer(
        dono, "UPDATE sistemas SET conferencias_sem_resposta = 1 WHERE id = :s", papel=ROTINAS, s=cena.terceiro_id
    )
    # O UPDATE de por_em_quarentena, com a revisao comparada (critica B1).
    assert await fazer(
        dono,
        "UPDATE sistemas SET situacao = 'quarentena', motivo = 'mudou, parou' WHERE id = :s AND situacao = 'ativo'"
        " AND revisao_servidor_id = :b RETURNING revisao_servidor_id IS NULL AND conferencias_sem_resposta = 0",
        papel=papel,
        s=cena.terceiro_id,
        b=base,
    )
    # Em quarentena, nenhuma revisao vale, nem posta pelo despachante sem sair da quarentena.
    revisao = await fazer(
        dono,
        SQL_REVISAO,
        papel=DESPACHANTE,
        **params_revisao(cena.terceiro_id, await declaradas_atuais(dono, cena.terceiro_id), [cena.t1.id], versao=2),
    )
    await fazer(
        dono,
        "UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s",
        papel=DESPACHANTE,
        r=revisao,
        s=cena.terceiro_id,
    )
    assert await fazer(dono, "SELECT revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id) is None


async def declaradas_atuais(dono: AsyncEngine, sistema_id: uuid.UUID) -> list[dict[str, str]]:
    async with dono.connect() as c:
        return await declaradas_revisadas(c, sistema_id)


async def em_quarentena(dono: AsyncEngine, sistema_id: uuid.UUID) -> uuid.UUID:
    """Terceiro revisado (versao 1) que entrou em quarentena; devolve a revisao que valia antes."""
    antiga = await revisao_vigente(dono, sistema_id)
    async with dono.begin() as c:
        await por_em_quarentena(c, sistema_id)
    return antiga


async def recusado_com_revisao_nova(
    dono: AsyncEngine, sistema_id: uuid.UUID, sql: str, match: str, *, papel: str | None, **params: Any
) -> None:
    """Grava uma revisao nova (``:r``) e, na MESMA transacao, tenta ``sql`` como ``papel``: assim a guarda so pode
    recusar pela regra que o teste quer, e nao porque a revisao veio de outra transacao. Tudo volta atras."""
    async with dono.connect() as c:
        transacao = await c.begin()
        try:
            nova = await gravar_revisao_do_servidor(c, sistema_id, vigente=False)
            await como(c, papel)
            with pytest.raises(DBAPIError, match=match):
                await c.execute(text(sql), {"r": nova, "s": sistema_id, **params})
        finally:
            await transacao.rollback()


async def test_tres_saidas_indevidas_da_quarentena_fechadas(dono: AsyncEngine, cena: Cena) -> None:
    """As tres saidas de hoje (0002): a entrada grava pendente; a sondagem grava recusado e depois ativo; o endereco
    novo volta a pendente."""
    await em_quarentena(dono, cena.terceiro_id)
    await recusado(
        dono,
        "UPDATE sistemas SET situacao = 'pendente' WHERE id = :s",
        QUARENTENA_SO_SAI,
        papel=ENTRADA,
        s=cena.terceiro_id,
    )
    await recusado(
        dono,
        "UPDATE sistemas SET situacao = 'recusado', versao_mcp = NULL WHERE id = :s",
        QUARENTENA_SO_SAI,
        papel=DESPACHANTE,
        s=cena.terceiro_id,
    )
    await recusado(
        dono,
        "UPDATE sistemas SET situacao = 'ativo', versao_mcp = :v WHERE id = :s",
        QUARENTENA_SO_SAI,
        papel=DESPACHANTE,
        v=VERSAO_MCP,
        s=cena.terceiro_id,
    )
    # Endereco novo: num terceiro, recusado (M1); num proprio em quarentena, continua em quarentena.
    await recusado(
        dono,
        "UPDATE sistemas SET endereco = 'https://x.teste/mcp' WHERE id = :s",
        "nao mudam depois de preenchidos",
        papel=ENTRADA,
        s=cena.terceiro_id,
    )
    await fazer(dono, "UPDATE sistemas SET situacao = 'quarentena' WHERE id = :s", s=cena.proprio_id)
    await fazer(
        dono, "UPDATE sistemas SET endereco = 'http://x.teste/mcp' WHERE id = :s", papel=ENTRADA, s=cena.proprio_id
    )
    assert tuple(await linha(dono, "SELECT situacao, versao_mcp FROM sistemas WHERE id = :s", s=cena.proprio_id)) == (
        "quarentena",
        None,
    )
    assert await fazer(dono, "SELECT situacao FROM sistemas WHERE id = :s", s=cena.terceiro_id) == "quarentena"
    # Endereco novo e outra situacao no mesmo UPDATE: 'pendente' nao tira da quarentena; 'desativado' (D27) tira.
    await fazer(
        dono,
        "UPDATE sistemas SET endereco = 'http://y.teste/mcp', situacao = 'pendente' WHERE id = :s",
        papel=ENTRADA,
        s=cena.proprio_id,
    )
    assert await fazer(dono, "SELECT situacao FROM sistemas WHERE id = :s", s=cena.proprio_id) == "quarentena"
    await fazer(
        dono,
        "UPDATE sistemas SET endereco = 'http://z.teste/mcp', situacao = 'desativado' WHERE id = :s",
        papel=ENTRADA,
        s=cena.proprio_id,
    )
    r = await linha(dono, "SELECT situacao, endereco, versao_mcp FROM sistemas WHERE id = :s", s=cena.proprio_id)
    assert tuple(r) == ("desativado", "http://z.teste/mcp", None)


@pytest.mark.parametrize(
    ("papel", "sql"),
    [
        (ROTINAS, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s"),
        (None, "UPDATE sistemas SET situacao = 'ativo', revisao_servidor_id = :r WHERE id = :s"),
        (ENTRADA, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s"),
        (
            DESPACHANTE,
            "UPDATE sistemas SET situacao = 'ativo', versao_mcp = NULL, revisao_servidor_id = :r WHERE id = :s",
        ),
        (
            DESPACHANTE,
            "UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2025-11-25', revisao_servidor_id = :r WHERE id = :s",
        ),
        (DESPACHANTE, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s"),
        (DESPACHANTE, "UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"),
        (ROTINAS, "UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"),
        (DESPACHANTE, "UPDATE sistemas SET situacao = 'pendente', revisao_servidor_id = :r WHERE id = :s"),
    ],
)
async def test_quarentena_nao_sai_por_outro_caminho(dono: AsyncEngine, cena: Cena, papel: str | None, sql: str) -> None:
    await em_quarentena(dono, cena.terceiro_id)
    await recusado_com_revisao_nova(dono, cena.terceiro_id, sql, QUARENTENA_SO_SAI, papel=papel)
    assert await fazer(dono, "SELECT situacao FROM sistemas WHERE id = :s", s=cena.terceiro_id) == "quarentena"


async def test_quarentena_nao_sai_com_revisao_antiga(dono: AsyncEngine, cena: Cena) -> None:
    antiga = await em_quarentena(dono, cena.terceiro_id)
    sair = "UPDATE sistemas SET situacao = 'ativo', revisao_servidor_id = :a WHERE id = :s"
    # Sem revisao nova, a que valia antes da quarentena (ainda a mais recente) nao volta a valer.
    await recusado(dono, sair, "gravada na mesma transacao", papel=DESPACHANTE, a=antiga, s=cena.terceiro_id)
    # Com uma revisao nova gravada, a antiga tambem deixa de ser a mais recente.
    await recusado_com_revisao_nova(dono, cena.terceiro_id, sair, "mais recente", papel=DESPACHANTE, a=antiga)
    assert tuple(
        await linha(dono, "SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id)
    ) == ("quarentena", None)


async def test_quarentena_sai_pela_conclusao_da_revisao_do_despachante(dono: AsyncEngine, cena: Cena) -> None:
    """A conclusao, como o despachante a faz: grava a revisao e sai da quarentena na mesma transacao."""
    await em_quarentena(dono, cena.terceiro_id)
    async with dono.begin() as c:
        await como(c, DESPACHANTE)
        nova = (
            await c.execute(
                text(SQL_REVISAO),
                params_revisao(
                    cena.terceiro_id, await declaradas_revisadas(c, cena.terceiro_id), [cena.t1.id], versao=2
                ),
            )
        ).scalar_one()
        r = (
            await c.execute(
                text(
                    "UPDATE sistemas SET situacao = 'ativo', motivo = NULL, revisao_servidor_id = :r WHERE id = :s"
                    " RETURNING situacao, revisao_servidor_id"
                ),
                {"r": nova, "s": cena.terceiro_id},
            )
        ).one()
    assert tuple(r) == ("ativo", nova)


@pytest.mark.parametrize(
    ("papel", "sql"),
    [
        (DESPACHANTE, "UPDATE sistemas SET situacao = 'recusado', versao_mcp = NULL WHERE id = :s"),
        (DESPACHANTE, "UPDATE sistemas SET situacao = 'recusado', revisao_servidor_id = :r WHERE id = :s"),
        (ENTRADA, "UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"),
        (ENTRADA, "UPDATE sistemas SET situacao = 'pendente' WHERE id = :s"),
        (ROTINAS, "UPDATE sistemas SET situacao = 'quarentena' WHERE id = :s"),
    ],
)
async def test_terceiro_que_sai_de_ativo_perde_a_revisao(dono: AsyncEngine, cena: Cena, papel: str, sql: str) -> None:
    """Regra 4 (D27, D29 tambem no banco): fora de 'ativo' o mudou-parou nao confere o servidor; a revisao vigente
    cai, e a volta a 'ativo' pela sondagem nao publica nada ate uma conclusao nova."""
    antiga = await revisao_vigente(dono, cena.terceiro_id)
    await fazer(
        dono, "UPDATE sistemas SET conferencias_sem_resposta = 1 WHERE id = :s", papel=ROTINAS, s=cena.terceiro_id
    )
    await fazer(dono, sql, papel=papel, s=cena.terceiro_id, r=antiga)
    r = await linha(
        dono,
        "SELECT situacao, revisao_servidor_id, conferencias_sem_resposta FROM sistemas WHERE id = :s",
        s=cena.terceiro_id,
    )
    assert (r.revisao_servidor_id, r.conferencias_sem_resposta) == (None, 0)
    if r.situacao != "quarentena":
        if r.situacao == "desativado":
            await fazer(
                dono, "UPDATE sistemas SET situacao = 'pendente' WHERE id = :s", papel=ENTRADA, s=cena.terceiro_id
            )
        await fazer(
            dono,
            "UPDATE sistemas SET situacao = 'ativo', versao_mcp = :v WHERE id = :s",
            papel=DESPACHANTE,
            v=VERSAO_MCP,
            s=cena.terceiro_id,
        )
        r = await linha(dono, "SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id)
        assert tuple(r) == ("ativo", None)
        await recusado(
            dono,
            "UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s",
            "gravada na mesma transacao",
            papel=DESPACHANTE,
            r=antiga,
            s=cena.terceiro_id,
        )
    # So uma conclusao nova publica de novo.
    nova = await revisao_vigente(dono, cena.terceiro_id)
    r = await linha(dono, "SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id)
    assert tuple(r) == ("ativo", nova)


@pytest.mark.parametrize("papel", [None, ENTRADA])
async def test_quarentena_pode_ir_a_desativado_pela_entrada_ou_pelo_dono(
    dono: AsyncEngine, cena: Cena, papel: str | None
) -> None:
    await em_quarentena(dono, cena.terceiro_id)
    await fazer(dono, "UPDATE sistemas SET situacao = 'desativado' WHERE id = :s", papel=papel, s=cena.terceiro_id)
    r = await linha(dono, "SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id)
    assert tuple(r) == ("desativado", None)
    # De desativado, volta so por nova sondagem (pendente, depois ativo) e nova revisao: nada publicado ate la.
    await fazer(dono, "UPDATE sistemas SET situacao = 'pendente' WHERE id = :s", papel=ENTRADA, s=cena.terceiro_id)
    await fazer(dono, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s", papel=DESPACHANTE, s=cena.terceiro_id)
    r = await linha(dono, "SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :s", s=cena.terceiro_id)
    assert tuple(r) == ("ativo", None)


async def test_regras_da_0002_seguem_valendo(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.begin() as c:
        pendente = await novo_proprio(c, situacao="pendente")
    await recusado(
        dono, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s", "So a sondagem", papel=ENTRADA, s=pendente
    )
    await recusado(
        dono, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s", "So a sondagem", papel=ROTINAS, s=pendente
    )
    for situacao in ("recusado", "quarentena"):
        await recusado(
            dono,
            "UPDATE sistemas SET situacao = :x WHERE id = :s",
            "A entrada so desativa",
            papel=ENTRADA,
            x=situacao,
            s=pendente,
        )
    await fazer(dono, "UPDATE sistemas SET situacao = 'ativo' WHERE id = :s", papel=DESPACHANTE, s=pendente)
    await fazer(dono, "UPDATE sistemas SET situacao = 'quarentena' WHERE id = :s", papel=ROTINAS, s=pendente)


async def test_rodadas_sem_resposta_so_as_rotinas_contam(dono: AsyncEngine, cena: Cena) -> None:
    revisao = await revisao_vigente(dono, cena.terceiro_id)
    assert (
        await fazer(
            dono,
            "UPDATE sistemas SET conferencias_sem_resposta = conferencias_sem_resposta + 1 WHERE id = :s"
            " RETURNING conferencias_sem_resposta",
            papel=ROTINAS,
            s=cena.terceiro_id,
        )
        == 1
    )
    for papel in (ENTRADA, DESPACHANTE):
        await recusado(
            dono,
            "UPDATE sistemas SET conferencias_sem_resposta = 0 WHERE id = :s",
            SEM_PERMISSAO,
            papel=papel,
            s=cena.terceiro_id,
        )
    await recusado(
        dono,
        "UPDATE sistemas SET conferencias_sem_resposta = -1 WHERE id = :s",
        "sistemas_conferencias_sem_resposta_check",
        papel=ROTINAS,
        s=cena.terceiro_id,
    )
    # Revisao nova zera a contagem (a mesma revisao, nao).
    await fazer(
        dono,
        "UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s",
        papel=DESPACHANTE,
        r=revisao,
        s=cena.terceiro_id,
    )
    assert await fazer(dono, "SELECT conferencias_sem_resposta FROM sistemas WHERE id = :s", s=cena.terceiro_id) == 1
    await revisao_vigente(dono, cena.terceiro_id)
    assert await fazer(dono, "SELECT conferencias_sem_resposta FROM sistemas WHERE id = :s", s=cena.terceiro_id) == 0


# --- CHAVE_TERCEIRO -------------------------------------------------------------------------------------------


async def test_terceiro_so_com_chave_por_pessoa_ou_compartilhada(dono: AsyncEngine, cena: Cena) -> None:
    await recusado(
        dono,
        "INSERT INTO sistemas (nome, endereco, origem, tipo_chave) VALUES (:n, 'https://t.teste/mcp', 'terceiro',"
        " 'programa')",
        "sistemas_chave_de_terceiro",
        papel=ENTRADA,
        n=f"t-{uuid.uuid4().hex[:8]}",
    )
    await recusado(
        dono,
        "UPDATE sistemas SET tipo_chave = 'programa' WHERE id = :s",
        "sistemas_chave_de_terceiro",
        papel=ENTRADA,
        s=cena.terceiro_id,
    )
    for tipo in ("compartilhada", "por_pessoa"):
        await fazer(
            dono,
            "INSERT INTO sistemas (nome, endereco, origem, tipo_chave)"
            " VALUES (:n, 'https://t.teste/mcp', 'terceiro', :t)",
            papel=ENTRADA,
            n=f"t-{uuid.uuid4().hex[:8]}",
            t=tipo,
        )
    await fazer(dono, "UPDATE sistemas SET tipo_chave = 'programa' WHERE id = :s", papel=ENTRADA, s=cena.proprio_id)


# --- VERSAO ---------------------------------------------------------------------------------------------------


async def test_desfecho_e_versoes_da_sondagem(dono: AsyncEngine, cena: Cena) -> None:
    for desfecho in ("aceito", "versao", "desvio", "nao_mcp", "sem_acesso", "inalcancavel"):
        await fazer(
            dono,
            "UPDATE sistemas SET sondagem_desfecho = :d WHERE id = :s",
            papel=DESPACHANTE,
            d=desfecho,
            s=cena.terceiro_id,
        )
    await recusado(
        dono,
        "UPDATE sistemas SET sondagem_desfecho = 'outro' WHERE id = :s",
        "sistemas_sondagem_desfecho_check",
        papel=DESPACHANTE,
        s=cena.terceiro_id,
    )
    await fazer(
        dono,
        "UPDATE sistemas SET versoes_anunciadas = CAST(:v AS text[]) WHERE id = :s",
        papel=DESPACHANTE,
        v=["2026-07-28", "2025-11-25", "outra"],
        s=cena.terceiro_id,
    )
    await fazer(
        dono, "UPDATE sistemas SET versoes_anunciadas = '{}' WHERE id = :s", papel=DESPACHANTE, s=cena.terceiro_id
    )
    # Digitos que o \d do Python aceita e o [0-9] do banco nao (arabe-indicos e de largura cheia): o despachante
    # filtra com [0-9] ou re.ASCII, senao a sondagem inteira para neste CHECK.
    nao_ascii = [
        "٢٠٢٦-٠٧-٢٨",
        "２０２６-０７-２８",
    ]
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in nao_ascii)
    ruins: list[list[str | None]] = [
        ["2025-11-25"] * 11,
        ["2025-11-25", None],
        ["Digite sua senha"],
        [""],
        ["2025-11-25x"],
        ["outras"],
        *[[v] for v in nao_ascii],
    ]
    for ruim in ruins:
        await recusado(
            dono,
            "UPDATE sistemas SET versoes_anunciadas = CAST(:v AS text[]) WHERE id = :s",
            "sistemas_versoes_anunciadas_check",
            papel=DESPACHANTE,
            v=ruim,
            s=cena.terceiro_id,
        )
    for papel in (ENTRADA, ROTINAS):
        for sql in (
            "UPDATE sistemas SET sondagem_desfecho = 'aceito' WHERE id = :s",
            "UPDATE sistemas SET versoes_anunciadas = '{}' WHERE id = :s",
        ):
            await recusado(dono, sql, SEM_PERMISSAO, papel=papel, s=cena.terceiro_id)


async def test_grafana_le_as_visoes_e_nao_as_tabelas(dono: AsyncEngine, cena: Cena) -> None:
    await revisao_vigente(dono, cena.terceiro_id)
    await fazer(
        dono,
        "INSERT INTO recusas_versao (minuto, lado, origem, versao_pedida, motivo, contagem)"
        " VALUES (date_trunc('minute', now()), 'sistema', :n, 'geracao-antiga', 'cadastro_terceiro', 1)"
        " ON CONFLICT DO NOTHING",
        n=f"antigo-{uuid.uuid4().hex[:6]}",
    )
    esperado = sorted(
        tuple(r)
        for r in await linhas(
            dono,
            "SELECT situacao, revisao_servidor_id IS NOT NULL, count(*) FROM sistemas WHERE origem = 'terceiro'"
            " GROUP BY 1, 2",
        )
    )
    async with dono.begin() as c:
        await como(c, GRAFANA)
        visto = sorted(
            tuple(r)
            for r in (await c.execute(text("SELECT situacao, revisado, sistemas FROM medicoes.v_terceiros"))).all()
        )
        recusas = (
            await c.execute(
                text("SELECT count(*) FROM medicoes.v_recusas_versao_sistema WHERE motivo = 'cadastro_terceiro'")
            )
        ).scalar_one()
    assert visto == esperado
    assert ("ativo", True) in {(s, r) for s, r, _ in visto}
    assert recusas >= 1
    for sql in (
        "SELECT count(*) FROM public.recusas_versao",
        "SELECT count(*) FROM public.sistemas",
        "SELECT count(*) FROM public.revisoes_servidor",
    ):
        await recusado(dono, sql, SEM_PERMISSAO, papel=GRAFANA)
    # Sem visao propria de recusas de terceiros (critica B12).
    assert await fazer(dono, "SELECT to_regclass('medicoes.v_recusas_versao_terceiros')") is None


# --- Caminho de busca fixo ------------------------------------------------------------------------------------

CAMINHO = "search_path=pg_catalog, public, pg_temp"


TEMPORARIA = "SELECT has_database_privilege(:papel, current_database(), 'TEMPORARY')"


async def com_sombra(dono: AsyncEngine, papel: str, sombras: list[str], sql: str, **params: Any) -> Any:
    """Como ``papel``, cria tabelas temporarias com o nome de tabelas do esquema (``sombras``) e roda ``sql``, numa
    transacao que sempre volta atras. Sem caminho de busca fixo, ``pg_temp`` vem antes de ``public`` e de
    ``pg_catalog`` e a guarda leria a sombra. Devolve a primeira linha.

    Os papeis do app nao criam tabela temporaria (``REVOKE TEMPORARY ... FROM PUBLIC``, como no
    ``docker/postgres/iniciar/10-papeis.sh``). Para provar a defesa de baixo, o caminho de busca fixo, como se essa
    primeira falhasse, o dono da o TEMPORARY a ``papel`` so dentro desta transacao; ela volta atras e leva a concessao
    junto (conferido no fim)."""
    assert re.fullmatch(r"[a-z_]+", papel)
    async with dono.connect() as c:
        transacao = await c.begin()
        antes = (await c.execute(text(TEMPORARIA), {"papel": papel})).scalar_one()
        try:
            banco = (await c.execute(text("SELECT quote_ident(current_database())"))).scalar_one()
            await c.execute(text(f"GRANT TEMPORARY ON DATABASE {banco} TO {papel}"))
            await como(c, papel)
            for sombra in sombras:
                await c.execute(text(sombra), params)
            return (await c.execute(text(sql), params)).first()
        finally:
            await transacao.rollback()
            assert (await c.execute(text(TEMPORARIA), {"papel": papel})).scalar_one() == antes


async def test_sombra_de_sistemas_nao_tira_a_marca_de_estranhos(dono: AsyncEngine, cena: Cena) -> None:
    """A entrada nao grava revisada de terceiro sem a marca de texto de fora, nem com as dicas do fornecedor."""
    r = await com_sombra(
        dono,
        ENTRADA,
        ["CREATE TEMP TABLE sistemas (id uuid, origem text)"],
        SQL_REVISADA.replace("INSERT INTO ferramentas_revisadas", "INSERT INTO public.ferramentas_revisadas"),
        s=cena.terceiro_id,
        n="f_sombra",
        nv=f"f_{uuid.uuid4().hex[:8]}",
        tf=False,
        dd=None,
    )
    assert (r.texto_de_fora, r.etiquetas) == (True, {})


async def test_sombra_de_pg_class_nao_deixa_a_entrada_gravar_sinal_com_hora_antiga(
    dono: AsyncEngine, cena: Cena
) -> None:
    r = await com_sombra(
        dono,
        ENTRADA,
        [
            "CREATE TEMP TABLE pg_class (oid oid, relowner oid)",
            "INSERT INTO pg_temp.pg_class SELECT 'public.sinais'::regclass, oid FROM pg_catalog.pg_roles"
            " WHERE rolname = current_user",
        ],
        "INSERT INTO public.sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id, visto_em)"
        " VALUES (:p, :a, 'texto_de_fora', :s, :f, now() - interval '30 days') RETURNING visto_em = now()",
        p=cena.pessoa_id,
        a=cena.aplicativo_id,
        s=cena.terceiro_id,
        f=cena.t1.id,
    )
    assert r[0] is True


async def test_sombra_de_ferramentas_nao_deixa_gravar_sinal_de_outro_sistema(dono: AsyncEngine, cena: Cena) -> None:
    with pytest.raises(DBAPIError, match="ferramenta do proprio sistema"):
        await com_sombra(
            dono,
            ENTRADA,
            [
                "CREATE TEMP TABLE ferramentas_revisadas (id uuid, sistema_id uuid)",
                "INSERT INTO pg_temp.ferramentas_revisadas VALUES (:f, :s)",
            ],
            "INSERT INTO public.sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id)"
            " VALUES (:p, :a, 'dado_privado', :s, :f) RETURNING id",
            p=cena.pessoa_id,
            a=cena.aplicativo_id,
            s=cena.terceiro_id,
            f=cena.p1.id,
        )


async def test_sombra_de_sistemas_nao_deixa_o_despachante_revisar_um_proprio(dono: AsyncEngine, cena: Cena) -> None:
    with pytest.raises(DBAPIError, match="so vale para servidor de terceiros"):
        await com_sombra(
            dono,
            DESPACHANTE,
            [
                "CREATE TEMP TABLE sistemas (id uuid, origem text)",
                "INSERT INTO pg_temp.sistemas VALUES (:s, 'terceiro')",
            ],
            SQL_REVISAO.replace("INSERT INTO revisoes_servidor", "INSERT INTO public.revisoes_servidor"),
            **params_revisao(cena.proprio_id, [declarada(cena.p1)], []),
        )


async def test_sombra_de_revisoes_nao_deixa_voltar_a_revisao_antiga(dono: AsyncEngine, cena: Cena) -> None:
    antiga = await revisao_vigente(dono, cena.terceiro_id)
    await fazer(
        dono, "UPDATE sistemas SET revisao_servidor_id = NULL WHERE id = :s", papel=DESPACHANTE, s=cena.terceiro_id
    )
    with pytest.raises(DBAPIError, match="sempre a mais recente dele"):
        await com_sombra(
            dono,
            DESPACHANTE,
            [
                "CREATE TEMP TABLE revisoes_servidor"
                " (id uuid, sistema_id uuid, versao integer, revisado_em timestamptz)",
                "INSERT INTO pg_temp.revisoes_servidor VALUES (:r, :s, 1, now())",
            ],
            "UPDATE public.sistemas SET revisao_servidor_id = :r WHERE id = :s",
            r=antiga,
            s=cena.terceiro_id,
        )


async def test_sombra_de_transicoes_nao_leva_a_acao_direto_a_enviada(dono: AsyncEngine, cena: Cena) -> None:
    """Bloco CAMINHO: a guarda da porta de saida (0005) tambem le a tabela de verdade."""
    ficha = await fazer(dono, SQL_FICHA, papel=ENTRADA, o=await operacao(dono, cena), si=None)
    fila = await fazer(
        dono,
        "INSERT INTO fila_saida (operacao_id, ficha_id) SELECT operacao_id, id FROM fichas WHERE id = :f RETURNING id",
        papel=ENTRADA,
        f=ficha,
    )
    with pytest.raises(DBAPIError, match="Mudanca de estado recusada na porta de saida"):
        await com_sombra(
            dono,
            ENTRADA,
            [
                "CREATE TEMP TABLE transicoes_saida (de text, para text)",
                "INSERT INTO pg_temp.transicoes_saida VALUES ('aguardando_ficha', 'enviada')",
            ],
            "UPDATE public.fila_saida SET estado = 'enviada' WHERE id = :i RETURNING estado",
            i=fila,
        )
    assert await fazer(dono, "SELECT estado FROM fila_saida WHERE id = :i", i=fila) == "aguardando_ficha"


async def test_toda_funcao_de_gatilho_tem_caminho_de_busca_fixo(dono: AsyncEngine) -> None:
    funcoes = {
        str(nome): list(config or [])
        for nome, config in await linhas(
            dono,
            "SELECT DISTINCT p.proname, p.proconfig FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
            " WHERE NOT t.tgisinternal",
        )
    }
    assert {
        "sistemas_guarda",
        "fichas_guarda",
        "fila_saida_guarda",
        "ferramentas_guarda",
        "extrato_recusa_mudanca",
        "revisoes_servidor_nasce",
        "ferramentas_revisadas_nasce",
        "sinais_nasce",
        "sinais_guarda",
        "limites_guarda",
    } <= set(funcoes)
    assert [nome for nome, config in funcoes.items() if CAMINHO not in config] == []
    # A funcao do CHECK das versoes anunciadas tambem.
    config = await fazer(dono, "SELECT proconfig FROM pg_proc WHERE proname = 'versoes_anunciadas_validas'")
    assert CAMINHO in config


# --- TEMPORARIA -----------------------------------------------------------------------------------------------

# O que o PUBLIC tem no banco atual (sem ACL gravada, vale o padrao do PostgreSQL: CONNECT e TEMPORARY).
DO_PUBLIC = (
    "SELECT array_agg(a.privilege_type ORDER BY a.privilege_type) FROM pg_database d,"
    " aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a WHERE d.datname = current_database()"
    " AND a.grantee = 0"
)
PAPEIS_DO_APP = (*PAPEIS, GRAFANA)
PODE = "SELECT has_database_privilege(:quem, current_database(), :privilegio)"


async def test_migracao_tira_o_temporary_do_public_num_banco_que_ja_existe(
    banco_0005: tuple[Parametros, AsyncEngine],
) -> None:
    """Bloco TEMPORARIA: num banco que nao passou pelo ``10-papeis.sh`` (como um que ja existia antes da Fase 3), o
    PUBLIC tem o TEMPORARY, o padrao do PostgreSQL, ate a 0005, e os papeis do app o herdam; a 0006 o tira. O
    CONNECT fica, e nenhum papel do app cria tabela temporaria."""
    q, engine = banco_0005
    assert await fazer(engine, DO_PUBLIC) == ["CONNECT", "TEMPORARY"]
    for papel in PAPEIS_DO_APP:
        assert await fazer(engine, PODE, quem=papel, privilegio="TEMPORARY") is True, papel
    await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, DO_PUBLIC) == ["CONNECT"]
    for papel in PAPEIS_DO_APP:
        assert await fazer(engine, PODE, quem=papel, privilegio="TEMPORARY") is False, papel
        assert await fazer(engine, PODE, quem=papel, privilegio="CONNECT") is True, papel
        await recusado(
            engine,
            "CREATE TEMP TABLE sistemas (id uuid, origem text)",
            "permission denied to create temporary tables",
            papel=papel,
        )


# --- GRANTS ---------------------------------------------------------------------------------------------------

E, D, R = ENTRADA, DESPACHANTE, ROTINAS

# (papel, objeto, coluna ou None, privilegio, esperado)
GRANTS: list[tuple[str, str, str | None, str, bool]] = [
    *[(p, "revisoes_servidor", None, "SELECT", True) for p in (E, D, R)],
    (D, "revisoes_servidor", None, "INSERT", True),
    (E, "revisoes_servidor", None, "INSERT", False),
    (R, "revisoes_servidor", None, "INSERT", False),
    *[(p, "revisoes_servidor", None, priv, False) for p in (E, D, R) for priv in ("UPDATE", "DELETE", "TRUNCATE")],
    *[
        (D, "sistemas", col, "UPDATE", True)
        for col in ("revisao_servidor_id", "sondagem_desfecho", "versoes_anunciadas")
    ],
    *[
        (p, "sistemas", col, "UPDATE", False)
        for p in (E, R)
        for col in ("revisao_servidor_id", "sondagem_desfecho", "versoes_anunciadas")
    ],
    *[(p, "sistemas", "origem", "UPDATE", False) for p in (E, D, R)],
    (R, "sistemas", "conferencias_sem_resposta", "UPDATE", True),
    (E, "sistemas", "conferencias_sem_resposta", "UPDATE", False),
    (D, "sistemas", "conferencias_sem_resposta", "UPDATE", False),
    (E, "sistemas", "origem", "INSERT", True),
    (E, "sistemas", "revisao_servidor_id", "INSERT", False),
    (E, "sinais", None, "SELECT", True),
    (E, "sinais", None, "INSERT", True),
    (E, "sinais", None, "UPDATE", False),
    (E, "sinais", None, "DELETE", False),
    (R, "sinais", None, "DELETE", True),
    (R, "sinais", "id", "SELECT", True),
    (R, "sinais", "visto_em", "SELECT", True),
    *[
        (R, "sinais", col, "SELECT", False)
        for col in ("pessoa_id", "aplicativo_id", "tipo", "sistema_id", "ferramenta_id")
    ],
    (R, "sinais", None, "INSERT", False),
    (R, "sinais", None, "UPDATE", False),
    *[(D, "sinais", None, priv, False) for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")],
    (D, "limites", "situacao", "UPDATE", True),
    (D, "limites", "motivo", "UPDATE", True),
    *[(D, "limites", col, "UPDATE", False) for col in ("usados_hoje", "por_dia", "valido_ate", "destinatarios")],
    (D, "limites", None, "SELECT", True),
    (R, "limites", "situacao", "UPDATE", True),
    *[(E, "ferramentas_revisadas", col, "INSERT", True) for col in ("texto_de_fora", "definicao_declarada")],
    *[
        (p, "ferramentas_revisadas", col, "UPDATE", False)
        for p in (E, D, R)
        for col in ("texto_de_fora", "definicao_declarada", "etiquetas")
    ],
    (E, "fichas", "sinais", "INSERT", True),
    *[(p, "fichas", "sinais", "UPDATE", False) for p in (E, D, R)],
    (GRAFANA, "medicoes.v_terceiros", None, "SELECT", True),
    (GRAFANA, "medicoes.v_recusas_versao_sistema", None, "SELECT", True),
    *[(GRAFANA, t, None, "SELECT", False) for t in ("recusas_versao", "sistemas", "revisoes_servidor", "sinais")],
]


@pytest.mark.parametrize(("papel", "objeto", "coluna", "privilegio", "esperado"), GRANTS)
async def test_grants_por_papel(
    dono: AsyncEngine, papel: str, objeto: str, coluna: str | None, privilegio: str, esperado: bool
) -> None:
    if coluna is None:
        sql = "SELECT has_table_privilege(:p, :o, :pr)"
    else:
        sql = "SELECT has_column_privilege(:p, :o, :c, :pr)"
    assert await fazer(dono, sql, p=papel, o=objeto, c=coluna, pr=privilegio) is esperado


async def test_despachante_suspende_os_limites_do_sistema(dono: AsyncEngine, cena: Cena) -> None:
    """Critica M2: a quarentena pela sondagem (despachante) suspende os limites ativos do sistema."""
    await fazer(
        dono,
        "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate)"
        " VALUES (:p, :a, :s, 'ler_a', 3, now() + interval '7 days')",
        p=cena.pessoa_id,
        a=cena.aplicativo_id,
        s=cena.terceiro_id,
    )
    for papel in (DESPACHANTE, ROTINAS):
        suspensos = await fazer(
            dono,
            "UPDATE limites SET situacao = 'suspenso', motivo = 'quarentena' WHERE sistema_id = :s RETURNING pessoa_id",
            papel=papel,
            s=cena.terceiro_id,
        )
        assert suspensos == cena.pessoa_id
    await recusado(
        dono,
        "UPDATE limites SET usados_hoje = 0 WHERE sistema_id = :s",
        SEM_PERMISSAO,
        papel=DESPACHANTE,
        s=cena.terceiro_id,
    )


async def test_despachante_so_suspende_limite_ativo(dono: AsyncEngine, cena: Cena) -> None:
    """Bloco LIMITES: com o UPDATE do M2, o despachante so leva um limite de 'ativo' a 'suspenso'."""

    async def limite(situacao: str) -> uuid.UUID:
        return uuid.UUID(
            str(
                await fazer(
                    dono,
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate,"
                    " situacao, motivo) VALUES (:p, :a, :s, 'ler_a', 3, now() + interval '7 days', :x, 'antes')"
                    " RETURNING id",
                    p=cena.pessoa_id,
                    a=cena.aplicativo_id,
                    s=cena.terceiro_id,
                    x=situacao,
                )
            )
        )

    ativo, encerrado, suspenso = await limite("ativo"), await limite("encerrado"), await limite("suspenso")
    so_suspende = "O despachante so suspende um limite ativo"
    for sql, alvo in (
        ("UPDATE limites SET situacao = 'ativo', motivo = NULL WHERE id = :i", suspenso),  # reativar
        ("UPDATE limites SET situacao = 'ativo' WHERE id = :i", encerrado),
        ("UPDATE limites SET motivo = 'outro' WHERE id = :i", suspenso),  # reescrever o motivo
        ("UPDATE limites SET motivo = 'outro' WHERE id = :i", ativo),
        ("UPDATE limites SET situacao = 'suspenso' WHERE id = :i", encerrado),  # um encerrado nao volta
        ("UPDATE limites SET situacao = 'encerrado' WHERE id = :i", ativo),  # encerrar e da pessoa
    ):
        await recusado(dono, sql, so_suspende, papel=DESPACHANTE, i=alvo)
    # O UPDATE de por_em_quarentena (WHERE situacao = 'ativo') suspende so o ativo; o mesmo valor de novo nao muda nada.
    assert (
        await fazer(
            dono,
            "UPDATE limites SET situacao = 'suspenso', motivo = 'quarentena'"
            " WHERE sistema_id = :s AND situacao = 'ativo'",
            papel=DESPACHANTE,
            s=cena.terceiro_id,
        )
        == 1
    )
    await fazer(dono, "UPDATE limites SET situacao = situacao WHERE id = :i", papel=DESPACHANTE, i=suspenso)
    situacoes = await fazer(
        dono, "SELECT array_agg(situacao ORDER BY situacao) FROM limites WHERE sistema_id = :s", s=cena.terceiro_id
    )
    assert situacoes == ["encerrado", "suspenso", "suspenso"]
    # A entrada e as rotinas seguem como na Fase 2.
    await fazer(
        dono, "UPDATE limites SET situacao = 'encerrado', motivo = 'pessoa' WHERE id = :i", papel=ENTRADA, i=ativo
    )
    await fazer(
        dono, "UPDATE limites SET situacao = 'vencido', motivo = 'prazo' WHERE id = :i", papel=ROTINAS, i=suspenso
    )


@pytest.mark.parametrize("papel", [DESPACHANTE, ROTINAS])
async def test_fila_trava_com_o_update_de_coluna_que_o_papel_tem(dono: AsyncEngine, cena: Cena, papel: str) -> None:
    """``parar_por_quarentena``: SELECT ... FOR UPDATE OF f das linhas que esperam, sem GRANT novo."""
    assert (
        await fazer(
            dono,
            "SELECT count(*) FROM (SELECT f.id FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"
            " WHERE o.sistema_id = :s AND f.estado IN ('aguardando_ficha', 'liberada') FOR UPDATE OF f) x",
            papel=papel,
            s=cena.terceiro_id,
        )
        == 0
    )


# --- tests/apoio_terceiros.py ---------------------------------------------------------------------------------


async def test_apoio_grava_a_revisao_e_tira_da_quarentena_como_a_conclusao(dono: AsyncEngine, cena: Cena) -> None:
    await revisao_vigente(dono, cena.terceiro_id)
    async with dono.begin() as c:
        await por_em_quarentena(c, cena.terceiro_id)
        nova = await gravar_revisao_do_servidor(c, cena.terceiro_id, [cena.t2.id], fornecedor="Fornecedor Y")
        assert (await c.execute(text("SELECT current_user"))).scalar_one() != DESPACHANTE  # o papel voltou ao dono
    r = await linha(
        dono,
        "SELECT s.situacao, s.motivo, s.revisao_servidor_id, rs.versao, rs.publicadas, rs.fornecedor, rs.impressao"
        " FROM sistemas s JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id WHERE s.id = :s",
        s=cena.terceiro_id,
    )
    assert (r.situacao, r.motivo, r.revisao_servidor_id, r.versao, r.publicadas, r.fornecedor) == (
        "ativo",
        None,
        nova,
        2,
        [cena.t2.id],
        "Fornecedor Y",
    )
    pares = [(cena.t2.nome_original, cena.t2.impressao), (cena.t1.nome_original, cena.t1.impressao)]
    assert r.impressao == impressao_do_servidor(pares) == impressao_do_servidor(reversed(pares))
    assert impressao_do_servidor(pares) != impressao_do_servidor(pares[:1])


# Vetor fixo da receita do contrato (G2: cadastro.impressao.impressao_servidor), calculado fora do codigo: sha256 do
# texto [["agenda","sha256:22..."],["relatório","sha256:11..."]] em UTF-8 (pares ordenados, sem espacos, acento
# sem escape).
IMPRESSAO_DO_SERVIDOR_FIXA = "sha256:35160fa4ef315f4d28c6117e94ca214a5035ba6c4c6093ffbf0470e9a69aec44"


def test_impressao_do_servidor_bate_com_o_vetor_fixo() -> None:
    pares = [("relatório", "sha256:" + "1" * 64), ("agenda", "sha256:" + "2" * 64)]
    assert impressao_do_servidor(pares) == IMPRESSAO_DO_SERVIDOR_FIXA
    assert impressao_do_servidor(reversed(pares)) == IMPRESSAO_DO_SERVIDOR_FIXA


async def test_apoio_deixa_subir_o_erro_da_guarda(dono: AsyncEngine, cena: Cena) -> None:
    """Em quarentena sem a versao 2026-07-28, a guarda recusa a saida, e o erro dela chega inteiro a quem chamou."""
    await em_quarentena(dono, cena.terceiro_id)
    await fazer(dono, "UPDATE sistemas SET versao_mcp = NULL WHERE id = :s", s=cena.terceiro_id)
    with pytest.raises(DBAPIError, match=QUARENTENA_SO_SAI):
        async with dono.begin() as c:
            await gravar_revisao_do_servidor(c, cena.terceiro_id)
    assert await fazer(dono, "SELECT count(*) FROM revisoes_servidor WHERE sistema_id = :s", s=cena.terceiro_id) == 1


async def test_apoio_recusa_revisao_vigente_fora_de_ativo(dono: AsyncEngine) -> None:
    async with dono.begin() as c:
        pendente = await cadastrar_terceiro(c, situacao="pendente", versao_mcp=None)
        await revisar_ferramenta(c, pendente, definicao("ler_z"))
    with pytest.raises(ValueError, match="so fica vigente com ele ativo ou em quarentena, nao pendente"):
        async with dono.begin() as c:
            await gravar_revisao_do_servidor(c, pendente)
    async with dono.begin() as c:
        await gravar_revisao_do_servidor(c, pendente, vigente=False)
    assert await fazer(dono, "SELECT count(*) FROM revisoes_servidor WHERE sistema_id = :s", s=pendente) == 1


@pytest.mark.parametrize(
    ("antes", "versao", "depois"),
    [
        ("recusado", None, "ativo"),
        ("pendente", None, "ativo"),
        ("desativado", VERSAO_MCP, "ativo"),
        ("ativo", None, "ativo"),
        ("pendente", VERSAO_MCP, "pendente"),
    ],
)
async def test_revisao_nova_so_vale_para_servidor_ativo_na_versao_certa(
    dono: AsyncEngine, cena: Cena, antes: str, versao: str | None, depois: str
) -> None:
    """Regra 5: a conclusao nao ativa um servidor recusado, pendente ou desativado, nem um que nao fala a
    2026-07-28; e uma revisao nova num servidor que nao fica ativo e recusada, em vez de sumir em silencio."""
    await fazer(
        dono,
        "UPDATE sistemas SET situacao = :a, versao_mcp = CAST(:v AS text) WHERE id = :s",
        a=antes,
        v=versao,
        s=cena.terceiro_id,
    )
    async with dono.connect() as c:
        transacao = await c.begin()
        try:
            await como(c, DESPACHANTE)
            nova = (
                await c.execute(
                    text(SQL_REVISAO),
                    params_revisao(cena.terceiro_id, await declaradas_revisadas(c, cena.terceiro_id), [cena.t1.id]),
                )
            ).scalar_one()
            with pytest.raises(DBAPIError, match="A revisao do servidor so vale para servidor ativo"):
                await c.execute(
                    text("UPDATE sistemas SET situacao = :d, revisao_servidor_id = :r WHERE id = :s"),
                    {"d": depois, "r": nova, "s": cena.terceiro_id},
                )
        finally:
            await transacao.rollback()


async def test_revisao_nova_de_servidor_ativo_na_versao_certa_vale(dono: AsyncEngine, cena: Cena) -> None:
    """O caminho normal da primeira conclusao: servidor ativo (sondado na 2026-07-28) fica ativo e com a revisao."""
    async with dono.begin() as c:
        await como(c, DESPACHANTE)
        nova = (
            await c.execute(
                text(SQL_REVISAO),
                params_revisao(cena.terceiro_id, await declaradas_revisadas(c, cena.terceiro_id), [cena.t1.id]),
            )
        ).scalar_one()
        r = (
            await c.execute(
                text(
                    "UPDATE sistemas SET situacao = 'ativo', revisao_servidor_id = :r WHERE id = :s"
                    " RETURNING situacao, revisao_servidor_id"
                ),
                {"r": nova, "s": cena.terceiro_id},
            )
        ).one()
    assert tuple(r) == ("ativo", nova)


# Retrato dos privilegios nas tabelas e visoes que a 0006 toca: um GRANT a mais (ou a menos) quebra este teste, e
# quem o mudar escreve aqui por que o papel precisa dele.
TABELAS_DO_RETRATO: Final = (
    "sistemas",
    "limites",
    "sinais",
    "revisoes_servidor",
    "ferramentas_revisadas",
    "fichas",
    "v_terceiros",
    "v_recusas_versao_sistema",
)
PAPEIS_DO_RETRATO: Final = ("mcp_claro_entrada", "mcp_claro_despachante", "mcp_claro_rotinas", "grafana_leitor")
RETRATO_DE_TABELA: Final = {
    ("grafana_leitor", "medicoes", "v_recusas_versao_sistema", "SELECT"),
    ("grafana_leitor", "medicoes", "v_terceiros", "SELECT"),
    ("mcp_claro_despachante", "public", "ferramentas_revisadas", "SELECT"),
    ("mcp_claro_despachante", "public", "fichas", "SELECT"),
    ("mcp_claro_despachante", "public", "limites", "SELECT"),
    ("mcp_claro_despachante", "public", "revisoes_servidor", "INSERT"),
    ("mcp_claro_despachante", "public", "revisoes_servidor", "SELECT"),
    ("mcp_claro_despachante", "public", "sistemas", "SELECT"),
    ("mcp_claro_entrada", "public", "ferramentas_revisadas", "INSERT"),
    ("mcp_claro_entrada", "public", "ferramentas_revisadas", "SELECT"),
    ("mcp_claro_entrada", "public", "fichas", "INSERT"),
    ("mcp_claro_entrada", "public", "fichas", "SELECT"),
    ("mcp_claro_entrada", "public", "limites", "INSERT"),
    ("mcp_claro_entrada", "public", "limites", "SELECT"),
    ("mcp_claro_entrada", "public", "revisoes_servidor", "SELECT"),
    ("mcp_claro_entrada", "public", "sinais", "INSERT"),
    ("mcp_claro_entrada", "public", "sinais", "SELECT"),
    ("mcp_claro_entrada", "public", "sistemas", "SELECT"),
    ("mcp_claro_rotinas", "public", "ferramentas_revisadas", "SELECT"),
    ("mcp_claro_rotinas", "public", "fichas", "SELECT"),
    ("mcp_claro_rotinas", "public", "limites", "SELECT"),
    ("mcp_claro_rotinas", "public", "revisoes_servidor", "SELECT"),
    ("mcp_claro_rotinas", "public", "sinais", "DELETE"),
    ("mcp_claro_rotinas", "public", "sistemas", "SELECT"),
}
RETRATO_DE_COLUNA: Final = {
    ("mcp_claro_despachante", "limites", "motivo", "UPDATE"),
    ("mcp_claro_despachante", "limites", "situacao", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "atualizado_em", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "motivo", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "revisao_servidor_id", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "situacao", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "sondado_em", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "sondagem", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "sondagem_desfecho", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "versao_mcp", "UPDATE"),
    ("mcp_claro_despachante", "sistemas", "versoes_anunciadas", "UPDATE"),
    ("mcp_claro_entrada", "ferramentas_revisadas", "motivo", "UPDATE"),
    ("mcp_claro_entrada", "ferramentas_revisadas", "situacao", "UPDATE"),
    ("mcp_claro_entrada", "fichas", "com_passkey", "UPDATE"),
    ("mcp_claro_entrada", "fichas", "confirmada_com", "UPDATE"),
    ("mcp_claro_entrada", "fichas", "decidida_em", "UPDATE"),
    ("mcp_claro_entrada", "fichas", "decidida_por", "UPDATE"),
    ("mcp_claro_entrada", "fichas", "decisao", "UPDATE"),
    ("mcp_claro_entrada", "fichas", "segundos_ate_decidir", "UPDATE"),
    ("mcp_claro_entrada", "limites", "com_passkey", "UPDATE"),
    ("mcp_claro_entrada", "limites", "dia", "UPDATE"),
    ("mcp_claro_entrada", "limites", "motivo", "UPDATE"),
    ("mcp_claro_entrada", "limites", "por_dia", "UPDATE"),
    ("mcp_claro_entrada", "limites", "situacao", "UPDATE"),
    ("mcp_claro_entrada", "limites", "usados_hoje", "UPDATE"),
    ("mcp_claro_entrada", "limites", "valido_ate", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "atualizado_em", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "autorizacao", "INSERT"),
    ("mcp_claro_entrada", "sistemas", "autorizacao", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "endereco", "INSERT"),
    ("mcp_claro_entrada", "sistemas", "endereco", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "motivo", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "nome", "INSERT"),
    ("mcp_claro_entrada", "sistemas", "nome", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "origem", "INSERT"),
    ("mcp_claro_entrada", "sistemas", "situacao", "UPDATE"),
    ("mcp_claro_entrada", "sistemas", "tipo_chave", "INSERT"),
    ("mcp_claro_entrada", "sistemas", "tipo_chave", "UPDATE"),
    ("mcp_claro_rotinas", "ferramentas_revisadas", "motivo", "UPDATE"),
    ("mcp_claro_rotinas", "ferramentas_revisadas", "situacao", "UPDATE"),
    ("mcp_claro_rotinas", "fichas", "avisada_em", "UPDATE"),
    ("mcp_claro_rotinas", "fichas", "decidida_em", "UPDATE"),
    ("mcp_claro_rotinas", "fichas", "decisao", "UPDATE"),
    ("mcp_claro_rotinas", "limites", "motivo", "UPDATE"),
    ("mcp_claro_rotinas", "limites", "situacao", "UPDATE"),
    ("mcp_claro_rotinas", "sinais", "id", "SELECT"),
    ("mcp_claro_rotinas", "sinais", "visto_em", "SELECT"),
    ("mcp_claro_rotinas", "sistemas", "atualizado_em", "UPDATE"),
    ("mcp_claro_rotinas", "sistemas", "conferencias_sem_resposta", "UPDATE"),
    ("mcp_claro_rotinas", "sistemas", "motivo", "UPDATE"),
    ("mcp_claro_rotinas", "sistemas", "situacao", "UPDATE"),
}


async def test_retrato_dos_privilegios_nas_tabelas_da_0006(dono: AsyncEngine) -> None:
    """Privilegio de tabela inteira e de coluna, por papel, nas tabelas que a 0006 toca: igual ao retrato."""
    async with dono.connect() as c:
        tabela = (
            await c.execute(
                text(
                    "SELECT grantee, table_schema, table_name, privilege_type FROM information_schema.role_table_grants"
                    " WHERE grantee = ANY(:p) AND table_name = ANY(:t)"
                ),
                {"p": list(PAPEIS_DO_RETRATO), "t": list(TABELAS_DO_RETRATO)},
            )
        ).all()
        coluna = (
            await c.execute(
                text(
                    "SELECT k.grantee, k.table_name, k.column_name, k.privilege_type"
                    " FROM information_schema.role_column_grants k WHERE k.grantee = ANY(:p) AND k.table_name = ANY(:t)"
                    " AND NOT EXISTS (SELECT 1 FROM information_schema.role_table_grants g WHERE g.grantee = k.grantee"
                    " AND g.table_name = k.table_name AND g.privilege_type = k.privilege_type)"
                ),
                {"p": list(PAPEIS_DO_RETRATO), "t": list(TABELAS_DO_RETRATO)},
            )
        ).all()
    assert {tuple(x) for x in tabela} == RETRATO_DE_TABELA
    assert {tuple(x) for x in coluna} == RETRATO_DE_COLUNA
