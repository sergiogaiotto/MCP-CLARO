"""Migracao 0007 (Fase 4), contra PostgreSQL 18.7 real: cada CHECK, gatilho, guarda e GRANT dos selos.

Os papeis entram por ``SET LOCAL ROLE`` numa transacao do dono do esquema, como em ``test_migracao_0006``. Cada teste
cria os proprios sistemas (nomes sorteados) com ``tests/apoio_selos.py``. Dois testes usam um banco proprio, migrado
ate a 0006 com dados da Fase 3 e depois ate a 0007. As listas do contrato no banco (``selo_vocabulario``,
``selo_casos``, ``selo_esquemas``, ``selo_passagem_permitida``, ``selo_esquema_essencial``,
``selo_endereco_normalizado``, ``selo_casos_problema`` e ``selo_casos_aprovam``) sao conferidas contra
``mcp_claro.selo.contrato``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Final, Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.selo import contrato
from tests import apoio_selos as apoio
from tests.apoio_terceiros import (
    cadastrar_terceiro,
    definicao,
    gravar_revisao_do_servidor,
    revisar_ferramenta,
)
from tests.conftest import RAIZ
from tests.integracao.test_migracao_0006 import (
    DESPACHANTE,
    ENTRADA,
    GRAFANA,
    ROTINAS,
    SEM_PERMISSAO,
    SEMEAR_FASE2,
    como,
    dono,
    fazer,
    linha,
    linhas,
    migrar,
    no_servidor,
    parametros_do_banco,
    recusado,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture

PAPEIS: Final = (ENTRADA, DESPACHANTE, ROTINAS)
SO_DE_ACRESCENTAR: Final = "so de acrescentar"


# --- Ajudas ---------------------------------------------------------------------------------------------------


async def no_dono(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    """Roda ``passos`` numa transacao do dono, que confirma."""
    async with engine.begin() as c:
        return await passos(c)


async def falha(
    engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]], match: str, *, papel: str | None = None
) -> None:
    """``passos`` falha com ``match``; a transacao volta atras (nada fica gravado). ``papel`` vale so para o passo
    final: quem quiser, troca de papel dentro de ``passos``."""
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            await como(c, papel)
            with pytest.raises(DBAPIError, match=match):
                await passos(c)
        finally:
            await transacao.rollback()


async def num_rascunho(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    """Roda ``passos`` numa transacao que sempre volta atras e devolve o que ``passos`` devolveu."""
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            return await passos(c)
        finally:
            await transacao.rollback()


@dataclass
class Cena:
    admin_id: uuid.UUID
    sistema: apoio.SistemaDoSelo


@pytest.fixture
async def cena(dono: AsyncEngine) -> Cena:
    async def montar(c: AsyncConnection) -> Cena:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(c, acoes=("enviar_lembrete", "apagar_lembrete"))
        return Cena(admin, sistema)

    return await no_dono(dono, montar)


async def com_teste(engine: AsyncEngine, cena: Cena, *, nivel: int = 2, aprovado: bool = True) -> apoio.TesteGravado:
    async def gravar(c: AsyncConnection) -> apoio.TesteGravado:
        return await apoio.gravar_teste(c, cena.sistema.id, nivel=nivel, aprovado=aprovado, admin_id=cena.admin_id)

    return await no_dono(engine, gravar)


async def vigente(engine: AsyncEngine, sistema_id: uuid.UUID, mais: timedelta | None = None) -> Any:
    async with engine.connect() as c:
        if mais is None:
            return await apoio.selo_vigente(c, sistema_id)
        agora = (await c.execute(text("SELECT now()"))).scalar_one()
        return await apoio.selo_vigente(c, sistema_id, agora + mais)


async def lista_sql(engine: AsyncEngine, nome: str) -> list[str]:
    return list(await fazer(engine, "SELECT selo_vocabulario(:n)", n=nome))


# --- Cabeca, downgrade e dados da Fase 3 ----------------------------------------------------------------------


async def test_a_cabeca_e_a_0007(dono: AsyncEngine) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    assert ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini"))).get_current_head() == "0007"
    assert await fazer(dono, "SELECT version_num FROM alembic_version") == "0007"


def test_downgrade_levanta_erro() -> None:
    caminho = RAIZ / "migrations" / "versions" / "0007_fase4.py"
    spec = importlib.util.spec_from_file_location("migracao_0007", caminho)
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    assert modulo.down_revision == "0006"
    with pytest.raises(RuntimeError, match="nao desfaz migracoes"):
        modulo.downgrade()


@pytest.fixture
async def banco_0006(banco_migrado: Parametros) -> AsyncIterator[tuple[Parametros, AsyncEngine]]:
    """Banco proprio, migrado so ate a 0006 (Fase 3)."""
    nome = f"fase3_{uuid.uuid4().hex[:8]}"
    await no_servidor(banco_migrado, f"CREATE DATABASE {nome}")
    q = parametros_do_banco(banco_migrado, nome)
    engine = criar_engine(q, pool_size=2, max_overflow=0)
    try:
        await asyncio.to_thread(migrar, q, "0006")
        yield q, engine
    finally:
        await engine.dispose()
        await no_servidor(banco_migrado, f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)")


SEMEAR_FASE3 = """
WITH b AS (INSERT INTO bloqueios (tipo, alcance, quem, motivo) VALUES ('bloqueio', 'tudo', 'admin', 'teste')
           RETURNING id),
c AS (INSERT INTO comprovantes (bloqueio_id) SELECT id FROM b RETURNING id),
v AS (INSERT INTO ferramentas_vistas (sistema_id, nome, definicao, impressao)
      SELECT id, 'enviar', '{"name": "enviar"}'::jsonb, 'sha256:p' FROM sistemas WHERE nome = 'proprio-f2' RETURNING 1),
k AS (INSERT INTO chaves_cifradas (sistema_id, tipo, cifrada)
      SELECT id, 'declaracoes', 'vault:v1:x' FROM sistemas WHERE nome = 'terceiro-f2' RETURNING 1),
s AS (INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp, selo)
      VALUES ('selo-a-mao', 'http://selo.teste/mcp', 'por_pessoa', 'ativo', '2026-07-28', 3) RETURNING 1)
SELECT (SELECT count(*) FROM c) + (SELECT count(*) FROM v) + (SELECT count(*) FROM k) + (SELECT count(*) FROM s)
"""


async def test_migracao_sobre_dados_da_fase_3(banco_0006: tuple[Parametros, AsyncEngine]) -> None:
    """A 0007 roda sobre um banco com dados das Fases 2 e 3: nada se perde, as colunas novas nascem vazias, o selo
    posto a mao volta a 1, os comprovantes antigos ficam derivados sem aviso e todo sistema fica no nivel 1."""
    q, engine = banco_0006
    assert await fazer(engine, SEMEAR_FASE2) == 3
    assert await fazer(engine, SEMEAR_FASE3) == 4
    await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, "SELECT version_num FROM alembic_version") == "0007"

    contagens = await linha(
        engine,
        "SELECT (SELECT count(*) FROM operacoes) AS op, (SELECT count(*) FROM fichas) AS fi,"
        " (SELECT count(*) FROM fila_saida) AS fs, (SELECT count(*) FROM limites) AS li,"
        " (SELECT count(*) FROM comprovantes) AS co, (SELECT count(*) FROM ferramentas_vistas) AS fv,"
        " (SELECT count(*) FROM chaves_cifradas) AS ch,"
        " (SELECT count(*) FROM fichas WHERE selo IS NOT NULL) AS selo_ficha,"
        " (SELECT count(*) FROM operacoes WHERE repete_operacao_id IS NOT NULL) AS repete,"
        " (SELECT count(*) FROM ferramentas_vistas WHERE sumiu_em IS NOT NULL) AS sumiu,"
        " (SELECT count(*) FROM avisos_bloqueio_derivados WHERE avisos = 0) AS derivados,"
        " (SELECT count(*) FROM sistemas WHERE selo <> 1) AS selo_cru",
    )
    assert tuple(contagens) == (1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 1, 0)
    niveis = {
        str(n): (int(nv), str(m))
        for n, nv, m in await linhas(
            engine, "SELECT s.nome, v.nivel, v.motivo FROM sistemas s CROSS JOIN LATERAL selo_vigente(s.id) v"
        )
    }
    assert set(niveis.values()) == {(1, "nunca_testado")}
    assert "selo-a-mao" in niveis
    # A ficha antiga decide como antes, e o selo dela (nao guardado) nao muda.
    await fazer(
        engine,
        "UPDATE fichas SET decisao = 'recusada', decidida_em = now(), decidida_por = (SELECT id FROM pessoas)",
        papel=ENTRADA,
    )
    await recusado(engine, "UPDATE fichas SET selo = '{\"nivel\": 1}'::jsonb", "O selo de uma ficha nao muda")
    # A chave antiga continua; a de teste e um tipo novo.
    await fazer(
        engine,
        "INSERT INTO chaves_cifradas (sistema_id, tipo, cifrada) SELECT id, 'teste_selo', 'vault:v1:t' FROM sistemas"
        " WHERE nome = 'terceiro-f2'",
    )
    tipos = await fazer(engine, "SELECT array_agg(tipo ORDER BY tipo) FROM chaves_cifradas")
    assert tipos == ["declaracoes", "teste_selo"]


async def test_migracao_para_com_ferramenta_ativa_de_nome_reservado(
    banco_0006: tuple[Parametros, AsyncEngine],
) -> None:
    q, engine = banco_0006
    await fazer(engine, SEMEAR_FASE2)
    await fazer(
        engine,
        "INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao,"
        " esquema_entrada,"
        " classe, impressao, revisado_por) SELECT id, 'selo_consultar', 1, 'selo_consultar_x', 'C', '{}'::jsonb, 'le',"
        " 'sha256:c', 'admin' FROM sistemas WHERE nome = 'proprio-f2'",
    )
    with pytest.raises(Exception, match=r"nome reservado ao contrato do selo: proprio-f2 \(selo_consultar\)"):
        await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, "SELECT version_num FROM alembic_version") == "0006"
    # Suspensa a ferramenta, a migracao passa; a suspensa fica como esta.
    await fazer(engine, "UPDATE ferramentas_revisadas SET situacao = 'suspensa' WHERE nome_original = 'selo_consultar'")
    await asyncio.to_thread(migrar, q, "head")
    assert await fazer(engine, "SELECT version_num FROM alembic_version") == "0007"


# --- CONTRATO: as listas do banco sao as do codigo ------------------------------------------------------------

VOCABULARIOS: Final[dict[str, tuple[str, ...]]] = {
    "estados": contrato.ESTADOS,
    "estados_do_recibo": contrato.ESTADOS_DO_RECIBO,
    "estados_em_aberto": contrato.ESTADOS_EM_ABERTO,
    "estados_que_queimam": contrato.ESTADOS_QUE_QUEIMAM,
    "eventos": contrato.EVENTOS_DEPOIS,
    "origens": contrato.ORIGENS,
    "desfechos": contrato.DESFECHOS,
    "desfechos_que_gastam_tentativa": contrato.DESFECHOS_QUE_GASTAM_TENTATIVA,
    "desfechos_de_falha_do_sistema": contrato.DESFECHOS_DE_FALHA_DO_SISTEMA,
    "resultados": contrato.RESULTADOS,
    "razoes": contrato.RAZOES,
    "motivos_do_selo": contrato.MOTIVOS_DO_SELO,
    "motivos_do_retrato": contrato.MOTIVOS_DO_RETRATO,
    "motivos_de_retirada": contrato.MOTIVOS_DE_RETIRADA,
    "estados_do_pedido": contrato.ESTADOS_DO_PEDIDO,
    "motivos_de_interrupcao": contrato.MOTIVOS_DE_INTERRUPCAO,
    "estados_do_aviso": contrato.ESTADOS_DO_AVISO,
    "modos": contrato.MODOS,
    "alvos": contrato.ALVOS,
    "ferramentas": contrato.FERRAMENTAS_DO_CONTRATO,
}


@pytest.mark.parametrize(("nome", "esperado"), VOCABULARIOS.items())
async def test_vocabulario_do_banco_e_o_do_codigo(dono: AsyncEngine, nome: str, esperado: tuple[str, ...]) -> None:
    assert await lista_sql(dono, nome) == list(esperado)


async def test_casos_do_banco_sao_os_do_codigo(dono: AsyncEngine) -> None:
    banco = [tuple(x) for x in await linhas(dono, "SELECT * FROM selo_casos('selo/1')")]
    codigo = [
        (c.caso, c.requisito, c.nivel, c.escopo, c.obrigatorio, c.nao_se_aplica_ate, c.maximo) for c in contrato.CASOS
    ]
    assert banco == codigo
    assert await linhas(dono, "SELECT * FROM selo_casos('selo/2')") == []
    assert await fazer(dono, "SELECT selo_nao_testados()") == contrato.nao_testado_gravado()


@pytest.mark.parametrize("nome", contrato.FERRAMENTAS_DO_CONTRATO)
async def test_esquemas_do_banco_sao_os_do_codigo(dono: AsyncEngine, nome: str) -> None:
    assert await fazer(dono, "SELECT selo_esquemas(:n)", n=nome) == contrato.ESQUEMAS[nome]
    assert await fazer(dono, "SELECT selo_esquemas('enviar_lembrete')") is None


async def test_passagens_do_banco_sao_as_do_codigo(dono: AsyncEngine) -> None:
    antes: list[str | None] = [None, *contrato.ESTADOS, "outro"]
    pares = [(a, n) for a in antes for n in [*contrato.ESTADOS, "outro"]]
    banco = await fazer(
        dono,
        "SELECT array_agg(selo_passagem_permitida(p ->> 0, p ->> 1) ORDER BY n)"
        " FROM jsonb_array_elements(CAST(:p AS jsonb)) WITH ORDINALITY AS x(p, n)",
        p=json.dumps(pares),
    )
    assert banco == [contrato.passagem_permitida(a, n) for a, n in pares]
    contas = await fazer(
        dono,
        "SELECT jsonb_object_agg(e, selo_conta_como(e)) FROM unnest(CAST(:e AS text[])) e",
        e=list(contrato.ESTADOS),
    )
    assert contas == dict(contrato.CONTA_COMO)
    # Os casos da critica M2: sem contradicao entre os "nao aconteceu", nem de parcial para concluida.
    assert contrato.passagem_permitida("nao_recebida", "recusada")
    assert contrato.passagem_permitida("parada", "nao_recebida")
    assert contrato.passagem_permitida("parcial", "concluida")
    assert not contrato.passagem_permitida("concluida", "nao_recebida")
    assert not contrato.passagem_permitida("em_andamento", "nao_recebida")


async def test_nao_recebida_confirmada_do_banco_e_a_do_codigo(dono: AsyncEngine) -> None:
    """Critica A3.3: selo_nao_recebida_confirmada e contrato.nao_recebida_confirmada dizem o mesmo, com a borda de 1
    minuto exato. A funcao e pura: todo papel (e o Grafana, pela v_fila_agora) pode chamar."""
    agora = (await linha(dono, "SELECT now()"))[0]
    segundos = [[], [0], [0, 0], [0, 59], [0, 60], [60, 0], [0, 30, 60], [0, 30], [0, 59.999], [0, 3600]]
    casos = [[agora + timedelta(seconds=x) for x in grupo] for grupo in segundos]
    for horas in casos:
        banco = await fazer(dono, "SELECT selo_nao_recebida_confirmada(CAST(:h AS timestamptz[]))", h=horas)
        assert banco is contrato.nao_recebida_confirmada(horas), horas
    assert await fazer(dono, "SELECT selo_nao_recebida_confirmada(NULL)") is False
    for papel in ("public", ENTRADA, DESPACHANTE, ROTINAS, GRAFANA):
        assert await fazer(
            dono,
            "SELECT has_function_privilege(:p, 'selo_nao_recebida_confirmada(timestamptz[])', 'EXECUTE')",
            p=papel,
        )


UUID_TEXTO: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class _Evento(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evento: Literal["concluida", "entregue", "devolvida", "desfeita", "parada"]
    em: str = Field(json_schema_extra={"format": "date-time"})


class _Consulta(BaseModel):
    contrato: Literal["selo/1"]
    operacao: str
    estado: Literal[
        "concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada", "desconhecido"
    ]
    depois: list[_Evento] = Field(default_factory=list, max_length=20)


def _esquemas_do_kit() -> list[Any]:
    """O que o kit 2.3.0 gera para ``selo_consultar`` feito do jeito comum (``title``, ``$defs``, ``$ref``)."""
    from mcp.server.mcpserver.tools.base import Tool

    def selo_consultar(operacao: Annotated[str, Field(pattern=UUID_TEXTO)]) -> _Consulta:
        raise NotImplementedError

    def sem_padrao(operacao: str) -> dict[str, Any]:
        raise NotImplementedError

    ferramenta = Tool.from_function(selo_consultar)
    return [ferramenta.parameters, ferramenta.output_schema, Tool.from_function(sem_padrao).parameters]


CORPO_ESQUEMAS: Final[list[Any]] = [
    *[e for f in contrato.ESQUEMAS.values() for e in f.values()],
    {"type": "object", "title": "x", "properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"type": "string"}}},
    {"properties": {"a": {"$ref": "#/definitions/B", "description": "d"}}, "definitions": {"B": {"enum": ["z", "a"]}}},
    {"properties": {"a": {"$ref": "https://fora.exemplo/esquema"}}},
    {"properties": {"a": {"$ref": "#/$defs/nao_existe"}}, "$defs": {}},
    {"properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"$ref": "#/$defs/A"}}},
    {"properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": "texto"}},
    {"properties": {"a": {"$ref": 5}}},
    {"enum": [3, "a", None], "type": "string"},
    {"required": ["b", "a", "b"], "type": "object", "anyOf": [{"type": "object"}], "minimum": 2},
    {"items": [{"type": "string"}], "properties": [1, 2], "const": "selo/1", "type": "string", "x-mais": {"title": 1}},
    {"type": "string", "enum": ["ção", "b", "Z", "a"]},
    "nao e objeto",
    None,
    [1, 2],
    {"properties": {"p": {"items": {"items": {"items": {"type": "string", "title": "t"}}}}}},
]


def _fundo(n: int) -> dict[str, Any]:
    no: dict[str, Any] = {"type": "string"}
    for _ in range(n):
        no = {"type": "object", "properties": {"x": no}}
    return no


async def test_forma_essencial_do_banco_e_a_do_codigo(dono: AsyncEngine) -> None:
    corpo = [*CORPO_ESQUEMAS, *_esquemas_do_kit(), _fundo(31), _fundo(40)]
    for esquema in corpo:
        banco = await fazer(dono, "SELECT selo_esquema_essencial(CAST(:e AS jsonb))", e=json.dumps(esquema))
        assert banco == contrato.forma_essencial(esquema), esquema


async def test_esquemas_que_o_kit_gera_conferem_e_sem_padrao_nao(dono: AsyncEngine) -> None:
    """Critica B4: o esquema que o kit gera (com ``title``, sem ``additionalProperties``, com ``$defs``) confere; o
    sem ``pattern`` no numero nao confere, no codigo e no banco."""
    entrada, saida, sem_padrao = _esquemas_do_kit()
    assert contrato.esquema_confere("selo_consultar", "inputSchema", entrada)
    assert contrato.esquema_confere("selo_consultar", "outputSchema", saida)
    assert not contrato.esquema_confere("selo_consultar", "inputSchema", sem_padrao)
    sql = (
        "SELECT selo_esquema_essencial(CAST(:e AS jsonb))"
        " = selo_esquema_essencial(selo_esquemas('selo_consultar') -> :campo)"
    )
    assert await fazer(dono, sql, e=json.dumps(entrada), campo="inputSchema") is True
    assert await fazer(dono, sql, e=json.dumps(saida), campo="outputSchema") is True
    assert await fazer(dono, sql, e=json.dumps(sem_padrao), campo="inputSchema") is False


CORPO_ENDERECOS: Final = [
    "https://cobranca.exemplo/mcp",
    "https://cobranca.exemplo/mcp/",
    "HTTPS://COBRANCA.Exemplo/MCP",
    "https://cobranca.exemplo:443/mcp",
    "https://cobranca.exemplo:0443/mcp",
    "http://cobranca.exemplo:80/mcp",
    "https://cobranca.exemplo:8443/mcp",
    "https://cobranca.exemplo:000/mcp",
    "https://cobranca.exemplo./mcp",
    "https://usuario:senha@cobranca.exemplo/mcp",
    "https://cobranca.exemplo/mcp?ambiente=teste#x",
    "https://cobranca.exemplo//mcp///",
    "https://cobranca.exemplo/./a/../mcp",
    "https://cobranca.exemplo/%6Dcp",
    "https://cobranca.exemplo/m%2Fcp%00%7e%2E",
    "https://cobranca.exemplo/%%41%4",
    "https://[::1]:8443/mcp",
    "https://[::1]/mcp",
    "  https://cobranca.exemplo/mcp\t",
    "https://",
    "nao e um endereco",
    "https://cobrança.exemplo/MCP",
    "ftp://ARQUIVO.exemplo:21/x",
    "https://a/../../b",
]


async def test_endereco_normalizado_do_banco_e_o_do_codigo(dono: AsyncEngine) -> None:
    banco = await fazer(
        dono,
        "SELECT array_agg(selo_endereco_normalizado(e) ORDER BY n) FROM unnest(CAST(:e AS text[])) WITH ORDINALITY"
        " AS x(e, n)",
        e=CORPO_ENDERECOS,
    )
    assert banco == [contrato.endereco_normalizado(e) for e in CORPO_ENDERECOS]
    iguais = {contrato.endereco_normalizado(e) for e in CORPO_ENDERECOS[:6]} | {
        contrato.endereco_normalizado(e) for e in CORPO_ENDERECOS[8:15] if "%2F" not in e
    }
    assert iguais == {"https://cobranca.exemplo/mcp", "http://cobranca.exemplo/mcp"}


def _corpo_de_casos(f1: str, f2: str) -> list[tuple[Any, list[str], int]]:
    base = [
        apoio.item(c.caso, None if c.escopo == "sistema" else f1, c.maximo) for c in contrato.CASOS if c.obrigatorio
    ]
    nivel2 = [i for i in base if contrato.CASOS_POR_NOME[i["caso"]].nivel == 2]

    def troca(itens: list[dict[str, Any]], nome: str, /, **campos: Any) -> list[dict[str, Any]]:
        return [i | campos if i["caso"] == nome else i for i in itens]

    return [
        (base, [f1], 3),
        (base, [f1], 2),
        (nivel2, [f1], 3),
        (nivel2, [f1, f2], 2),
        ([*nivel2, apoio.item("C-2.2d", f1, "falhou")], [f1], 2),
        ([*nivel2, apoio.item("C-2.2d", f1, "nao_observado")], [f1], 2),
        (troca(nivel2, "C-2.2b", resultado="nao_se_aplica", razao="sem_segundo_efeito", tentativas=0), [f1], 2),
        (troca(nivel2, "C-2.4b", resultado="nao_se_aplica", razao="sem_condicao_declarada", tentativas=0), [f1], 2),
        (troca(base, "C-2.4b", resultado="nao_se_aplica", razao="sem_condicao_declarada", tentativas=0), [f1], 3),
        (troca(nivel2, "C-2.1", resultado="nao_se_aplica", razao="sem_condicao_declarada", tentativas=0), [f1], 2),
        (troca(nivel2, "C-2.3", resultado="cumpriu"), [f1], 2),
        (troca(nivel2, "C-2.1", resultado="nao_testado", razao="sem_resposta", tentativas=0), [f1], 2),
        (troca(nivel2, "C-2.1", resultado="nao_testado", tentativas=0), [f1], 2),
        (troca(nivel2, "C-2.1", resultado="nao_observado"), [f1], 2),
        (troca(nivel2, "C-2.1", razao="inventada"), [f1], 2),
        (troca(nivel2, "C-2.1", resultado="passou"), [f1], 2),
        (troca(nivel2, "C-2.1", ferramenta=str(uuid.uuid4())), [f1], 2),
        (troca(nivel2, "C-0.1", ferramenta=f1), [f1], 2),
        (troca(nivel2, "C-2.1", tentativas=0), [f1], 2),
        (troca(nivel2, "C-2.1", tentativas=True), [f1], 2),
        (troca(nivel2, "C-2.1", tentativas=1.5), [f1], 2),
        (troca(nivel2, "C-2.1", tentativas="1"), [f1], 2),
        (troca(nivel2, "C-2.1", excluidas=2), [f1], 2),
        (troca(nivel2, "C-2.1", caso="C-9.9"), [f1], 2),
        (troca(nivel2, "C-2.1", caso=7), [f1], 2),
        ([*nivel2, nivel2[-1]], [f1], 2),
        ([{**nivel2[0], "texto": "do sistema"}], [f1], 2),
        ([{"caso": "C-0.1"}], [f1], 2),
        (["texto"], [f1], 2),
        ({"caso": "C-0.1"}, [f1], 2),
        ([], [f1], 2),
    ]


async def test_forma_e_criterio_dos_casos_do_banco_sao_os_do_codigo(dono: AsyncEngine) -> None:
    f1, f2 = str(uuid.uuid4()), str(uuid.uuid4())
    for casos, ferramentas, nivel in _corpo_de_casos(f1, f2):
        problema = await fazer(
            dono,
            "SELECT selo_casos_problema(CAST(:c AS jsonb), CAST(:f AS uuid[]))",
            c=json.dumps(casos),
            f=ferramentas,
        )
        assert problema == contrato.problema_nos_casos(casos, ferramentas), casos
        if problema is None:
            aprova = await fazer(
                dono,
                "SELECT selo_casos_aprovam(CAST(:c AS jsonb), CAST(:f AS uuid[]), CAST(:n AS smallint))",
                c=json.dumps(casos),
                f=ferramentas,
                n=nivel,
            )
            assert aprova is contrato.casos_aprovam(casos, ferramentas, nivel), (casos, nivel)
    # O corpo cobre os dois lados de cada regra.
    codigos = {contrato.problema_nos_casos(c, f) for c, f, _ in _corpo_de_casos(f1, f2)}
    assert codigos >= {
        None,
        "nao_e_lista",
        "item_nao_e_objeto",
        "campos_do_item",
        "caso_desconhecido",
        "ferramenta_do_caso",
        "resultado_fora",
        "razao_fora",
        "razao_faltando",
        "acima_do_maximo",
        "nao_se_aplica_sem_lugar",
        "tentativas",
        "caso_repetido",
    }
    aprovam = {
        contrato.casos_aprovam(c, f, n) for c, f, n in _corpo_de_casos(f1, f2) if not contrato.problema_nos_casos(c, f)
    }
    assert aprovam == {True, False}


async def test_funcoes_da_0007_tem_caminho_de_busca_fixo(dono: AsyncEngine) -> None:
    funcoes = {
        str(nome): list(config or [])
        for nome, config in await linhas(
            dono,
            "SELECT p.proname, p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname IN ('public', 'medicoes') AND (p.proname LIKE 'selo%' OR p.proname IN"
            " ('declaracoes_selo_nasce', 'pedidos_teste_selo_nasce', 'pedidos_teste_selo_guarda', 'testes_selo_nasce',"
            " 'selos_retirados_nasce', 'operacoes_repete_confere', 'envios_selo_nasce', 'avisos_bloqueio_nasce',"
            " 'avisos_bloqueio_guarda', 'avisos_bloqueio_derivados_nasce', 'informacoes_sistema_nasce',"
            " 'informacoes_sistema_contradicao', 'chaves_cifradas_guarda', 'contagem_selos', 'fichas_guarda',"
            " 'ferramentas_revisadas_nasce'))",
        )
    }
    # 34 da etapa 1, 4 do retoque G02r (selo_esquema_teto, selo_esquema_peso, selo_alvo_bem_escrito e
    # selo_alvo_mal_escrito) e 2 do retoque 2 (selo_ipv4_escrito e selo_endereco_conexao).
    assert len(funcoes) == 40
    assert {
        "selo_esquema_teto",
        "selo_esquema_peso",
        "selo_alvo_bem_escrito",
        "selo_alvo_mal_escrito",
        "selo_ipv4_escrito",
        "selo_endereco_conexao",
    } <= set(funcoes)
    assert [n for n, config in funcoes.items() if "search_path=pg_catalog, public, pg_temp" not in config] == []
    definidoras = {
        str(n)
        for (n,) in await linhas(
            dono,
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname IN ('public', 'medicoes') AND p.prosecdef AND (p.proname LIKE 'selo%'"
            " OR p.proname = 'contagem_selos')",
        )
    }
    assert definidoras == {
        "selo_vigente",
        "selo_acoes_do_sistema",
        "selo_contrato_listado",
        "selo_substituto",
        "contagem_selos",
    }


# --- So de acrescentar ----------------------------------------------------------------------------------------


@dataclass
class Tudo:
    """Uma linha de cada tabela nova, para as provas de "so de acrescentar"."""

    cena: Cena
    teste: apoio.TesteGravado
    operacao: apoio.OperacaoDeTeste
    aviso_id: uuid.UUID


@pytest.fixture
async def tudo(dono: AsyncEngine, cena: Cena) -> Tudo:
    teste = await com_teste(dono, cena, nivel=3)

    async def montar(c: AsyncConnection) -> Tudo:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, op.id)
        await apoio.informar(c, op.id, "em_andamento")
        bloqueio = (
            await c.execute(
                text(
                    "INSERT INTO bloqueios (tipo, alcance, pessoa_id, quem, motivo)"
                    " VALUES ('bloqueio', 'pessoa', :p, 'pessoa', 'teste') RETURNING id"
                ),
                {"p": op.pessoa_id},
            )
        ).scalar_one()
        await c.execute(
            text("INSERT INTO comprovantes (bloqueio_id, em_conferencia) VALUES (:b, CAST(:e AS jsonb))"),
            {"b": bloqueio, "e": json.dumps([{"protocolo": op.protocolo, "estado": "nao_confirmada", "desde": "x"}])},
        )
        aviso = (
            await c.execute(
                text(
                    "INSERT INTO avisos_bloqueio (bloqueio_id, sistema_id, pessoa_id, operacoes)"
                    " VALUES (:b, :s, :p, ARRAY[CAST(:o AS uuid)]) RETURNING id"
                ),
                {"b": bloqueio, "s": op.sistema_id, "p": op.pessoa_id, "o": op.id},
            )
        ).scalar_one()
        await c.execute(
            text("INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) VALUES (:b, 1)"), {"b": bloqueio}
        )
        await c.execute(
            text(
                "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                " VALUES (:s, :t, 'admin', 'teste das guardas', 'admin-teste', :a)"
            ),
            {"s": cena.sistema.id, "t": teste.id, "a": cena.admin_id},
        )
        return Tudo(cena, teste, op, uuid.UUID(str(aviso)))

    return await no_dono(dono, montar)


SO_ACRESCENTAR: Final = [
    "declaracoes_selo",
    "testes_selo",
    "selos_retirados",
    "envios_selo",
    "informacoes_sistema",
    "avisos_bloqueio_derivados",
]


@pytest.mark.parametrize("tabela", SO_ACRESCENTAR)
async def test_tabelas_so_de_acrescentar_recusam_ate_o_dono(dono: AsyncEngine, tudo: Tudo, tabela: str) -> None:
    sistema = tudo.cena.sistema.id
    alvo = {
        "declaracoes_selo": "sistema_id = :s",
        "testes_selo": "sistema_id = :s",
        "selos_retirados": "sistema_id = :s",
        "envios_selo": "sistema_id = :s",
        "informacoes_sistema": "sistema_id = :s",
        "avisos_bloqueio_derivados": "bloqueio_id IN (SELECT bloqueio_id FROM avisos_bloqueio WHERE sistema_id = :s)",
    }[tabela]
    coluna = "avisos" if tabela == "avisos_bloqueio_derivados" else "sistema_id"
    valor = "avisos" if tabela == "avisos_bloqueio_derivados" else "sistema_id"
    assert await fazer(dono, f"SELECT count(*) FROM {tabela} WHERE {alvo}", s=sistema) >= 1  # noqa: S608
    await recusado(dono, f"UPDATE {tabela} SET {coluna} = {valor} WHERE {alvo}", SO_DE_ACRESCENTAR, s=sistema)  # noqa: S608
    await recusado(dono, f"DELETE FROM {tabela} WHERE {alvo}", SO_DE_ACRESCENTAR, s=sistema)  # noqa: S608
    await recusado(dono, f"TRUNCATE {tabela} CASCADE", SO_DE_ACRESCENTAR)


@pytest.mark.parametrize("tabela", ["pedidos_teste_selo", "avisos_bloqueio"])
async def test_pedidos_e_avisos_nao_se_apagam(dono: AsyncEngine, tudo: Tudo, tabela: str) -> None:
    await recusado(dono, f"DELETE FROM {tabela} WHERE sistema_id = :s", SO_DE_ACRESCENTAR, s=tudo.cena.sistema.id)  # noqa: S608
    await recusado(dono, f"TRUNCATE {tabela} CASCADE", SO_DE_ACRESCENTAR)


# --- DECLARACOES ----------------------------------------------------------------------------------------------


async def test_declaracao_nasce_versionada_com_a_hora_do_banco(dono: AsyncEngine, cena: Cena) -> None:
    async def salvar(c: AsyncConnection) -> list[Any]:
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        return list(
            (
                await c.execute(
                    text("SELECT versao, salva_em = now() FROM declaracoes_selo WHERE sistema_id = :s ORDER BY versao"),
                    {"s": cena.sistema.id},
                )
            ).all()
        )

    assert [tuple(x) for x in await no_dono(dono, salvar)] == [(1, True), (2, True)]
    sql = (
        "INSERT INTO declaracoes_selo (sistema_id, versao, contrato, modo, endereco_teste, declaracao, responsavel,"
        " salva_por, pessoa_id) VALUES (:s, :v, 'selo/1', 'ambiente_de_teste', :e, CAST(:d AS jsonb), :r, 'x', :p)"
    )
    dados = apoio.declaracao(["enviar_lembrete", "apagar_lembrete"])
    base = {"s": cena.sistema.id, "e": apoio.ENDERECO_TESTE, "d": json.dumps(dados), "r": apoio.RESPONSAVEL}
    await recusado(dono, sql, "deveria ser a versao 3, nao 2", **base, v=2, p=cena.admin_id)
    pessoa = await no_dono(dono, apoio.nova_pessoa)
    await recusado(dono, sql, "So um ADMIN ativo salva", **base, v=3, p=pessoa)
    await recusado(dono, sql, "sao os do proprio JSON", **(base | {"r": "Outra equipe"}), v=3, p=cena.admin_id)
    await recusado(
        dono, sql, "sao os do proprio JSON", **(base | {"e": "https://outro.exemplo.teste/mcp"}), v=3, p=cena.admin_id
    )
    await recusado(dono, sql, "permission denied", **base, v=3, p=cena.admin_id, papel=ROTINAS)
    await fazer(dono, sql, **base, v=3, p=cena.admin_id, papel=ENTRADA)


@pytest.mark.parametrize(
    "variante",
    ["{e}", "{e}/", "{E}", "{h}:443{c}", "{h}{c}?ambiente=teste", "{h}{c}#teste", "{h}.{c}", "{h}//{c}/./"],
)
async def test_declaracao_recusa_o_endereco_de_producao_normalizado(
    dono: AsyncEngine, cena: Cena, variante: str
) -> None:
    """Critica A4: o alvo de teste e comparado com a producao na forma normalizada, nao como texto."""
    producao = cena.sistema.endereco  # https://<nome>.exemplo.teste/mcp
    hospedeiro, _, caminho = producao.removeprefix("https://").partition("/")
    alvo = variante.format(
        e=producao, E=producao.upper().replace("HTTPS", "https"), h=f"https://{hospedeiro}", c=f"/{caminho}"
    )
    assert contrato.mesmo_endereco(alvo, producao), alvo
    dados = apoio.declaracao(["enviar_lembrete", "apagar_lembrete"], endereco_teste=alvo)

    async def salvar(c: AsyncConnection) -> None:
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)

    await falha(dono, salvar, "o endereco de producao de um sistema do cadastro")


ALVO_E_PRODUCAO: Final = "O alvo de teste declarado e o endereco de producao de um sistema do cadastro"


async def test_declaracao_recusa_a_producao_de_outro_sistema(dono: AsyncEngine, cena: Cena) -> None:
    """Critica A4: o alvo de teste nao pode ser a producao de nenhum sistema do cadastro, nem a de outro (a bateria
    levaria la a chave de teste e faria escritas na producao dele)."""

    async def salvar(c: AsyncConnection) -> None:
        outro = await apoio.proprio_com_acoes(c)
        alvo = "https://" + outro.endereco.removeprefix("https://").upper() + "/"
        dados = apoio.declaracao(["enviar_lembrete", "apagar_lembrete"], endereco_teste=alvo)
        await c.execute(text("SET LOCAL ROLE mcp_claro_entrada"))
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)

    await falha(dono, salvar, ALVO_E_PRODUCAO)


@pytest.mark.parametrize("mudanca", ["producao_virou_o_alvo", "outro_sistema_no_alvo"])
async def test_pedido_confere_o_alvo_contra_a_producao_de_agora(dono: AsyncEngine, cena: Cena, mudanca: str) -> None:
    """Critica A4: a producao pode mudar (ou outro sistema ser cadastrado no alvo) depois de a declaracao ser salva;
    o pedido confere de novo. Tudo numa transacao que volta atras: um sistema no alvo de teste do apoio, gravado,
    recusaria as declaracoes dos outros testes."""

    async def pedir(c: AsyncConnection) -> None:
        d = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        if mudanca == "producao_virou_o_alvo":
            await c.execute(
                text("UPDATE sistemas SET endereco = :e WHERE id = :s"),
                {"e": apoio.ENDERECO_TESTE, "s": cena.sistema.id},
            )
            await _reativar(c, cena.sistema.id)
        else:
            await apoio.proprio_com_acoes(c, endereco=apoio.ENDERECO_TESTE.replace("/mcp", ":443/mcp/"))
        await c.execute(text("SET LOCAL ROLE mcp_claro_entrada"))
        await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, d, rodando=False)

    await falha(dono, pedir, ALVO_E_PRODUCAO)


async def test_declaracao_com_ferramentas_de_teste_vai_sem_alvo(dono: AsyncEngine, cena: Cena) -> None:
    dados = apoio.declaracao(["enviar_lembrete", "apagar_lembrete"], modo="ferramentas_de_teste")

    async def salvar(c: AsyncConnection) -> Any:
        d = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)
        return (
            await c.execute(text("SELECT modo, endereco_teste FROM declaracoes_selo WHERE id = :d"), {"d": d})
        ).one()

    assert tuple(await no_dono(dono, salvar)) == ("ferramentas_de_teste", None)


async def test_chave_de_teste_nunca_vira_a_de_producao(dono: AsyncEngine, cena: Cena) -> None:
    """Criticas A4 e M11: a chave teste_selo e do sistema (sem pessoa nem programa), e guardar uma nao toca a outra;
    nenhuma linha muda de tipo nem de dono."""
    sql = (
        "INSERT INTO chaves_cifradas (sistema_id, pessoa_id, tipo, cifrada) VALUES (:s, :p, :t, :c)"
        " ON CONFLICT (sistema_id, coalesce(pessoa_id, '00000000-0000-0000-0000-000000000000'::uuid),"
        " coalesce(aplicativo_id, '00000000-0000-0000-0000-000000000000'::uuid), tipo)"
        " DO UPDATE SET cifrada = EXCLUDED.cifrada"
    )
    s = cena.sistema.id
    await fazer(dono, sql, s=s, p=None, t="acesso", c="vault:v1:producao", papel=DESPACHANTE)
    await fazer(dono, sql, s=s, p=None, t="teste_selo", c="vault:v1:teste", papel=DESPACHANTE)
    await fazer(dono, sql, s=s, p=None, t="teste_selo", c="vault:v1:teste-2", papel=DESPACHANTE)
    guardadas = {
        str(t): str(c)
        for t, c in await linhas(dono, "SELECT tipo, cifrada FROM chaves_cifradas WHERE sistema_id = :s", s=s)
    }
    assert guardadas == {"acesso": "vault:v1:producao", "teste_selo": "vault:v1:teste-2"}
    await recusado(
        dono, sql, "chaves_cifradas_teste_selo_do_sistema", s=s, p=cena.admin_id, t="teste_selo", c="vault:v1:x"
    )
    await recusado(
        dono,
        "UPDATE chaves_cifradas SET tipo = 'acesso' WHERE sistema_id = :s AND tipo = 'teste_selo'",
        "nao muda de sistema, de dono nem de tipo",
        s=s,
    )
    await recusado(dono, sql, "chaves_cifradas_tipo_check", s=s, p=None, t="outra", c="vault:v1:x")


# --- TESTES: pedido e teste -----------------------------------------------------------------------------------


async def test_pedido_nasce_pedido_do_admin_com_a_declaracao_mais_recente(dono: AsyncEngine, cena: Cena) -> None:
    async def declarar(c: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
        primeira = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        return primeira, await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)

    antiga, nova = await no_dono(dono, declarar)
    pessoa = await no_dono(dono, apoio.nova_pessoa)
    sql = (
        "INSERT INTO pedidos_teste_selo (sistema_id, nivel, declaracao_id, pedido_por, estado, motivo)"
        " VALUES (:s, :n, :d, :p, :e, :m)"
    )
    base = {"s": cena.sistema.id, "n": 2, "d": nova, "p": cena.admin_id, "e": "pedido", "m": None}
    await recusado(dono, sql, "a declaracao de teste mais recente", **(base | {"d": antiga}))
    await recusado(dono, sql, "So um ADMIN ativo pede", **(base | {"p": pessoa}))
    await recusado(dono, sql, "nasce pedido", **(base | {"e": "rodando"}))
    await recusado(dono, sql, "permission denied", **base, papel=DESPACHANTE)
    await fazer(dono, sql, **base, papel=ENTRADA)
    await recusado(dono, sql, "pedidos_teste_selo_um_por_vez", **base, papel=ENTRADA)
    hora = await linha(
        dono,
        "SELECT estado, pedido_em = mudou_em, motivo FROM pedidos_teste_selo WHERE sistema_id = :s",
        s=cena.sistema.id,
    )
    assert tuple(hora) == ("pedido", True, None)


@pytest.mark.parametrize(
    ("mudanca", "erro"),
    [
        ("UPDATE sistemas SET tipo_chave = 'compartilhada' WHERE id = :s", "chave por pessoa"),
        ("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s", "sistema ativo na versao 2026-07-28"),
    ],
)
async def test_pedido_so_para_sistema_ativo_com_chave_por_pessoa(
    dono: AsyncEngine, cena: Cena, mudanca: str, erro: str
) -> None:
    async def pedir(c: AsyncConnection) -> None:
        declaracao_id = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        await c.execute(text(mudanca), {"s": cena.sistema.id})
        if "tipo_chave" in mudanca:  # a guarda poe 'pendente'; o dono volta a 'ativo' para isolar a regra da chave
            await c.execute(
                text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2026-07-28' WHERE id = :s"),
                {"s": cena.sistema.id},
            )
        await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, declaracao_id, rodando=False)

    await falha(dono, pedir, erro)


async def test_pedido_anda_so_pelos_caminhos_do_despachante(dono: AsyncEngine, cena: Cena) -> None:
    async def pedir(c: AsyncConnection) -> uuid.UUID:
        d = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        return await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, d, rodando=False)

    pedido = await no_dono(dono, pedir)
    mudar = "UPDATE pedidos_teste_selo SET estado = :e, motivo = :m WHERE id = :i"
    await recusado(dono, mudar, "permission denied", e="rodando", m=None, i=pedido, papel=ENTRADA)
    await recusado(
        dono, mudar, "Mudanca recusada no pedido de teste do selo: pedido -> terminado", e="terminado", m=None, i=pedido
    )
    await recusado(dono, mudar, "pedidos_teste_selo_check", e="interrompido", m=None, i=pedido, papel=DESPACHANTE)
    await recusado(dono, mudar, "pedidos_teste_selo_motivo_check", e="interrompido", m="inventado", i=pedido)
    # O despachante nem tem UPDATE no resto da linha; a guarda segura ate o dono.
    await recusado(
        dono, "UPDATE pedidos_teste_selo SET nivel = 3 WHERE id = :i", SEM_PERMISSAO, i=pedido, papel=DESPACHANTE
    )
    await recusado(dono, "UPDATE pedidos_teste_selo SET nivel = 3 WHERE id = :i", "so o estado muda", i=pedido)

    async def com_porta_fechada(c: AsyncConnection) -> None:
        await c.execute(text("UPDATE estado_saida SET aberta = false WHERE id = 1"))
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(text(mudar), {"e": "rodando", "m": None, "i": pedido})

    await falha(dono, com_porta_fechada, "Com a porta de saida fechada, o teste do selo nao comeca")
    await fazer(dono, mudar, e="rodando", m=None, i=pedido, papel=DESPACHANTE)
    await recusado(dono, mudar, "so termina com o teste gravado", e="terminado", m=None, i=pedido, papel=DESPACHANTE)
    await fazer(dono, mudar, e="interrompido", m="sistema_mudou", i=pedido, papel=DESPACHANTE)
    await recusado(dono, mudar, "rodando", e="rodando", m=None, i=pedido, papel=DESPACHANTE)
    estado = await linha(
        dono, "SELECT estado, motivo, mudou_em > pedido_em FROM pedidos_teste_selo WHERE id = :i", i=pedido
    )
    assert tuple(estado) == ("interrompido", "sistema_mudou", True)


@pytest.mark.parametrize(
    ("mudanca", "erro"),
    [
        ("outro_sistema_no_alvo", ALVO_E_PRODUCAO),
        ("producao_virou_o_alvo", ALVO_E_PRODUCAO),
        ("desativado", "sistema ativo na versao 2026-07-28"),
        ("versao_mcp", "sistema ativo na versao 2026-07-28"),
        ("chave_compartilhada", "chave por pessoa"),
        ("declaracao_nova", "declaracao de teste mais recente"),
    ],
)
async def test_rodando_confere_de_novo_o_pedido_antes_das_escritas(
    dono: AsyncEngine, cena: Cena, mudanca: str, erro: str
) -> None:
    """Defeito 2 da conferencia final (critica A4): as escritas reais da bateria vem entre 'rodando' e o fim, entao a
    passagem para 'rodando' confere de novo o que o pedido conferiu: o alvo contra a producao de todo sistema do
    cadastro, o sistema ativo na 2026-07-28 e a chave por pessoa. Tudo numa transacao que volta atras (um sistema no
    alvo do apoio, gravado, recusaria as declaracoes dos outros testes)."""

    async def passos(c: AsyncConnection) -> None:
        d = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        pedido = await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, d, rodando=False)
        s = {"s": cena.sistema.id}
        if mudanca == "outro_sistema_no_alvo":
            await apoio.proprio_com_acoes(c, endereco=apoio.ENDERECO_TESTE.replace("/mcp", ":443/MCP/"))
        elif mudanca == "producao_virou_o_alvo":
            await c.execute(text("UPDATE sistemas SET endereco = :e WHERE id = :s"), s | {"e": apoio.ENDERECO_TESTE})
            await _reativar(c, cena.sistema.id)
        elif mudanca == "desativado":
            await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), s)
        elif mudanca == "versao_mcp":
            await c.execute(text("UPDATE sistemas SET versao_mcp = '2025-11-25' WHERE id = :s"), s)
        elif mudanca == "declaracao_nova":  # o ADMIN salvou uma correcao depois do pedido (conferencia do G0r)
            await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        else:
            await c.execute(text("UPDATE sistemas SET tipo_chave = 'compartilhada' WHERE id = :s"), s)
            await _reativar(c, cena.sistema.id)
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(text("UPDATE pedidos_teste_selo SET estado = 'rodando' WHERE id = :i"), {"i": pedido})

    await falha(dono, passos, erro)


async def test_teste_aprovado_tem_a_data_e_a_validade_do_banco(dono: AsyncEngine, cena: Cena) -> None:
    teste = await com_teste(dono, cena)
    datas = await linha(
        dono,
        "SELECT testado_em = (SELECT pedido_em FROM pedidos_teste_selo WHERE id = :p), vale_ate - testado_em,"
        " (SELECT estado FROM pedidos_teste_selo WHERE id = :p) FROM testes_selo WHERE id = :t",
        p=teste.pedido_id,
        t=teste.id,
    )
    assert tuple(datas) == (True, timedelta(days=90), "terminado")
    assert (teste.nivel_pedido, teste.nivel_obtido) == (2, 2)


async def test_validade_sai_da_politica_vigente_na_hora(dono: AsyncEngine, cena: Cena) -> None:
    async def com_politica(c: AsyncConnection) -> Any:
        await c.execute(
            text("INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_TTL_DAYS', 7, 't', 'teste')")
        )
        teste = await apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id)
        await c.execute(
            text("INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_TTL_DAYS', 900, 't', 'fora')")
        )
        outro = await apoio.proprio_com_acoes(c)
        acima = await apoio.gravar_teste(c, outro.id, admin_id=cena.admin_id)
        return teste.vale_ate - teste.testado_em, acima.vale_ate - acima.testado_em

    assert await num_rascunho(dono, com_politica) == (timedelta(days=7), timedelta(days=365))


async def test_teste_forca_a_hora_mesmo_se_o_codigo_mandar_outra(dono: AsyncEngine, cena: Cena) -> None:
    async def gravar(c: AsyncConnection) -> Any:
        teste = await apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id)
        return await apoio.selo_vigente(c, cena.sistema.id), teste

    selo, teste = await num_rascunho(dono, gravar)
    assert (selo.nivel, selo.motivo, selo.teste_id) == (2, "vigente", teste.id)


def _casos(cena_ferramentas: list[uuid.UUID], nivel: int = 2, **troca: Any) -> list[dict[str, Any]]:
    """Os casos que aprovam, com a troca ``{caso: campos}`` no primeiro item de cada caso (``None`` tira o item)."""
    itens = apoio.casos_que_aprovam(cena_ferramentas, nivel)
    saida: list[dict[str, Any]] = []
    feitos: set[str] = set()
    for i in itens:
        chave = i["caso"].replace("-", "_").replace(".", "_")
        if chave in troca and chave not in feitos:
            feitos.add(chave)
            if troca[chave] is None:
                continue
            i = i | troca[chave]
        saida.append(i)
    return saida


@pytest.mark.parametrize(
    ("troca", "erro"),
    [
        ({"C_2_1": {"resultado": "falhou"}}, "sem todos os casos obrigatorios, ou com um caso que falhou"),
        ({"C_2_2d": {"resultado": "falhou", "razao": None, "tentativas": 1}}, "ou com um caso que falhou"),
        ({"C_2_4a": None}, "sem todos os casos obrigatorios"),
        ({"C_0_1": None}, "sem todos os casos obrigatorios"),
        ({"C_2_1": {"resultado": "nao_testado", "razao": "sem_resposta", "tentativas": 0}}, "sem todos os casos"),
        ({"C_2_1": {"resultado": "nao_observado"}}, "sem todos os casos obrigatorios"),
        ({"C_2_1": {"resultado": "nao_se_aplica", "razao": "sem_condicao_declarada"}}, r"\(nao_se_aplica_sem_lugar\)"),
        ({"C_2_3": {"resultado": "cumpriu"}}, r"\(acima_do_maximo\)"),
        ({"C_2_1": {"razao": "por_que_sim"}}, r"\(razao_fora\)"),
        ({"C_2_1": {"texto": "do sistema"}}, r"\(campos_do_item\)"),
    ],
)
async def test_teste_aprovado_so_com_todos_os_casos_obrigatorios(
    dono: AsyncEngine, cena: Cena, troca: dict[str, Any], erro: str
) -> None:
    async def gravar(c: AsyncConnection) -> None:
        ferramentas = await apoio.acoes_do_sistema(c, cena.sistema.id)
        await apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id, casos=_casos(ferramentas, **troca))

    await falha(dono, gravar, erro)


async def test_nao_se_aplica_do_c24b_vale_no_nivel_2_e_nao_no_3(dono: AsyncEngine, cena: Cena) -> None:
    troca = {"C_2_4b": {"resultado": "nao_se_aplica", "razao": "sem_condicao_declarada", "tentativas": 0}}

    async def nivel(c: AsyncConnection, n: int) -> int:
        ferramentas = await apoio.acoes_do_sistema(c, cena.sistema.id)
        casos = _casos(ferramentas, n, **troca)
        return (await apoio.gravar_teste(c, cena.sistema.id, nivel=n, admin_id=cena.admin_id, casos=casos)).nivel_obtido

    assert await num_rascunho(dono, lambda c: nivel(c, 2)) == 2
    await falha(dono, lambda c: nivel(c, 3), "sem todos os casos obrigatorios")


async def test_reprovado_fica_no_nivel_1_e_o_teste_de_nivel_3_que_falha_no_3_tambem(
    dono: AsyncEngine, cena: Cena
) -> None:
    async def gravar(c: AsyncConnection) -> Any:
        ferramentas = await apoio.acoes_do_sistema(c, cena.sistema.id)
        casos = _casos(ferramentas, 3, C_3_1a={"resultado": "falhou"})
        teste = await apoio.gravar_teste(
            c, cena.sistema.id, nivel=3, aprovado=False, admin_id=cena.admin_id, casos=casos
        )
        return teste, await apoio.selo_vigente(c, cena.sistema.id)

    teste, selo = await num_rascunho(dono, gravar)
    assert (teste.nivel_obtido, selo.nivel, selo.motivo) == (1, 1, "reprovado")

    async def aprovado_no_1(c: AsyncConnection) -> None:
        ferramentas = await apoio.acoes_do_sistema(c, cena.sistema.id)
        d = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        pedido = await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, d, nivel=3)
        await c.execute(
            text(
                "INSERT INTO testes_selo (pedido_id, sistema_id, declaracao_id, contrato, bateria, nivel_pedido,"
                " resultado, nivel_obtido, sistema_endereco, sistema_tipo_chave, ferramentas, casos, nao_testado,"
                " chamadas, iniciado_em) SELECT :p, s.id, :d, 'selo/1', '1.0.0', 3, 'reprovado', 2, s.endereco,"
                " s.tipo_chave, CAST(:f AS uuid[]), '[]', CAST(:nt AS jsonb), 0, now() FROM sistemas s WHERE s.id = :s"
            ),
            {
                "p": pedido,
                "d": d,
                "f": ferramentas,
                "s": cena.sistema.id,
                "nt": json.dumps(contrato.nao_testado_gravado()),
            },
        )

    await falha(dono, aprovado_no_1, "testes_selo_check")


@pytest.mark.parametrize(
    "estrago",
    [
        "endereco",
        "tipo_chave",
        "revisao",
        "ferramenta_a_mais",
        "ferramenta_faltando",
        "ferramenta_repetida",
        "fora_de_ativo",
        "pedido_nao_rodando",
        "nivel_do_pedido",
        "chamadas",
        "nao_testado",
        "iniciado_antes",
        "iniciado_depois",
        "alvo_virou_producao",
        "alvo_e_producao_de_outro",
        "declaracao_nova",
    ],
)
async def test_teste_so_com_o_retrato_do_sistema_de_agora(dono: AsyncEngine, cena: Cena, estrago: str) -> None:
    erros = {
        "endereco": "nao bate com o sistema de agora",
        "tipo_chave": "nao bate com o sistema de agora",
        "revisao": "nao bate com o sistema de agora",
        "ferramenta_a_mais": "as ferramentas de acao ativas do sistema agora",
        "ferramenta_faltando": "as ferramentas de acao ativas do sistema agora",
        "ferramenta_repetida": "as ferramentas de acao ativas do sistema agora",
        "fora_de_ativo": "so vale para sistema ativo",
        "pedido_nao_rodando": "so nasce de um pedido rodando",
        "nivel_do_pedido": "so nasce de um pedido rodando",
        "chamadas": "menos chamadas que tentativas",
        "nao_testado": "sempre a lista do contrato",
        "iniciado_antes": "comecou depois do pedido",
        "iniciado_depois": "comecou depois do pedido e antes de agora",
        "alvo_virou_producao": ALVO_E_PRODUCAO,
        "alvo_e_producao_de_outro": ALVO_E_PRODUCAO,
        "declaracao_nova": "declaracao de teste mais recente",
    }

    async def gravar(c: AsyncConnection) -> None:
        s = cena.sistema.id
        ferramentas = await apoio.acoes_do_sistema(c, s)
        d = await apoio.salvar_declaracao(c, s, cena.admin_id)
        pedido = await apoio.pedir_teste(c, s, cena.admin_id, d, rodando=estrago != "pedido_nao_rodando")
        lista = [str(f) for f in ferramentas]
        endereco, tipo, revisao = "s.endereco", "s.tipo_chave", "s.revisao_servidor_id"
        nivel, chamadas, iniciado = 2, None, "now()"
        nao_testado = contrato.nao_testado_gravado()
        if estrago == "endereco":
            endereco = "'https://outro.exemplo.teste/mcp'"
        elif estrago == "tipo_chave":
            tipo = "'compartilhada'"
        elif estrago == "revisao":
            terceiro = await cadastrar_terceiro(c)
            revisao = f"'{await gravar_revisao_do_servidor(c, terceiro)}'::uuid"
        elif estrago == "ferramenta_a_mais":
            lista.append(str(cena.sistema.leituras[0].id))
        elif estrago == "ferramenta_faltando":
            lista = lista[:1]
        elif estrago == "ferramenta_repetida":
            lista.append(lista[0])
        elif estrago == "fora_de_ativo":
            await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), {"s": s})
        elif estrago == "nivel_do_pedido":
            nivel = 3
        elif estrago == "chamadas":
            chamadas = 1
        elif estrago == "nao_testado":
            nao_testado = nao_testado[:1]
        elif estrago == "iniciado_antes":
            iniciado = "now() - interval '1 second'"
        elif estrago == "iniciado_depois":
            iniciado = "now() + interval '1 minute'"
        elif estrago == "alvo_virou_producao":  # a producao mudou para o alvo durante o teste (critica A4)
            await c.execute(
                text("UPDATE sistemas SET endereco = :e WHERE id = :s"), {"e": apoio.ENDERECO_TESTE, "s": s}
            )
            await _reativar(c, s)
        elif estrago == "alvo_e_producao_de_outro":
            await apoio.proprio_com_acoes(c, endereco="https://Teste.Selo.Exemplo.Teste/mcp/")
        elif estrago == "declaracao_nova":  # o ADMIN salvou uma correcao durante a bateria
            await apoio.salvar_declaracao(c, s, cena.admin_id)
        casos = apoio.casos_que_aprovam(ferramentas, nivel)
        await c.execute(
            text(
                "INSERT INTO testes_selo (pedido_id, sistema_id, declaracao_id, contrato, bateria, nivel_pedido,"  # noqa: S608
                " resultado, nivel_obtido, sistema_endereco, sistema_tipo_chave, revisao_servidor_id, ferramentas,"
                f" casos, nao_testado, chamadas, iniciado_em) SELECT :p, s.id, :d, 'selo/1', '1.0.0', :n, 'aprovado',"
                f" :n, {endereco}, {tipo}, {revisao}, CAST(:f AS uuid[]), CAST(:c AS jsonb), CAST(:nt AS jsonb), :ch,"
                f" {iniciado} FROM sistemas s WHERE s.id = :s"
            ),
            {
                "p": pedido,
                "d": d,
                "n": nivel,
                "f": lista,
                "c": json.dumps(casos),
                "nt": json.dumps(nao_testado),
                "ch": chamadas if chamadas is not None else contrato.contagem_minima_de_chamadas(casos),
                "s": s,
            },
        )

    await falha(dono, gravar, erros[estrago])


# --- VIGENTE --------------------------------------------------------------------------------------------------


async def test_selo_vigente_nunca_testado_aprovado_vencido_e_reprovado(dono: AsyncEngine, cena: Cena) -> None:
    antes = await vigente(dono, cena.sistema.id)
    assert (antes.nivel, antes.motivo, antes.teste_id, antes.vale_ate) == (1, "nunca_testado", None, None)
    teste = await com_teste(dono, cena)
    agora = await vigente(dono, cena.sistema.id)
    assert (agora.nivel, agora.motivo, agora.teste_id, agora.modo) == (2, "vigente", teste.id, "ambiente_de_teste")
    assert (agora.testado_em, agora.vale_ate) == (teste.testado_em, teste.vale_ate)
    # O contrato lido "daqui a 89 dias" (o mudou-parou continua lendo): o selo vale ate a ultima hora da validade.
    await no_dono(
        dono,
        lambda c: apoio.listar_contrato(c, cena.sistema.id, vista_em=teste.testado_em + timedelta(days=89, hours=22)),
    )
    assert (await vigente(dono, cena.sistema.id, timedelta(days=89, hours=23))).motivo == "vigente"
    vencido = await vigente(dono, cena.sistema.id, timedelta(days=91))
    assert (vencido.nivel, vencido.motivo, vencido.vale_ate) == (1, "vencido", teste.vale_ate)
    reprovado = await com_teste(dono, cena, aprovado=False)
    depois = await vigente(dono, cena.sistema.id)
    assert (depois.nivel, depois.motivo, depois.teste_id) == (1, "reprovado", reprovado.id)
    pela_visao = await linha(dono, "SELECT nivel, motivo FROM selos_vigentes WHERE sistema_id = :s", s=cena.sistema.id)
    assert tuple(pela_visao) == (1, "reprovado")


async def test_selo_de_nivel_3_e_o_reteste_de_nivel_2(dono: AsyncEngine, cena: Cena) -> None:
    await com_teste(dono, cena, nivel=3)
    assert (await vigente(dono, cena.sistema.id)).nivel == 3
    await com_teste(dono, cena, nivel=2)
    assert (await vigente(dono, cena.sistema.id)).nivel == 2


async def _reativar(c: AsyncConnection, sistema_id: uuid.UUID) -> None:
    """A guarda de sistemas poe 'pendente' quando o endereco ou o tipo de chave mudam; a sondagem (aqui, o dono)
    volta o sistema a 'ativo' na 2026-07-28."""
    await c.execute(
        text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2026-07-28' WHERE id = :s"), {"s": sistema_id}
    )


@pytest.mark.parametrize(
    ("mudanca", "motivo"),
    [
        ("endereco", "endereco_mudou"),
        ("endereco_sem_sondagem", "fora_de_ativo"),
        ("tipo_chave", "tipo_chave_mudou"),
        ("quarentena", "fora_de_ativo"),
        ("desativado", "fora_de_ativo"),
        ("versao_mcp", "fora_de_ativo"),
        ("revisada_nova", "ferramenta_nova"),
        ("acao_nova", "ferramenta_nova"),
        ("acao_suspensa", "vigente"),
        ("leitura_nova", "vigente"),
        ("contrato_sumiu", "contrato_sumiu"),
        ("contrato_mudou", "contrato_sumiu"),
        ("contrato_mudou_saida", "contrato_sumiu"),
        ("alvo_sumiu", "contrato_sumiu"),
        ("contrato_do_kit", "vigente"),
        ("retirado", "retirado"),
    ],
)
async def test_selo_volta_ao_nivel_1_pelo_motivo_certo(
    dono: AsyncEngine, cena: Cena, mudanca: str, motivo: str
) -> None:
    await com_teste(dono, cena)
    s = cena.sistema.id

    async def mudar(c: AsyncConnection) -> None:
        if mudanca.startswith("endereco"):
            await c.execute(
                text("UPDATE sistemas SET endereco = 'https://novo.exemplo.teste/mcp' WHERE id = :s"), {"s": s}
            )
            if mudanca == "endereco":
                await _reativar(c, s)
        elif mudanca == "tipo_chave":
            await c.execute(text("UPDATE sistemas SET tipo_chave = 'programa' WHERE id = :s"), {"s": s})
            await _reativar(c, s)
        elif mudanca in ("quarentena", "desativado"):
            await c.execute(text("UPDATE sistemas SET situacao = :q WHERE id = :s"), {"q": mudanca, "s": s})
        elif mudanca == "versao_mcp":  # a sondagem achou outra versao; o sistema continua 'ativo'
            await c.execute(text("UPDATE sistemas SET versao_mcp = '2025-11-25' WHERE id = :s"), {"s": s})
        elif mudanca == "revisada_nova":
            await revisar_ferramenta(c, s, cena.sistema.acoes[0].definicao | {"description": "outra"}, classe="muda")
        elif mudanca == "acao_nova":
            await revisar_ferramenta(c, s, definicao("criar_lembrete"), classe="tem_volta")
        elif mudanca == "acao_suspensa":
            await c.execute(
                text("UPDATE ferramentas_revisadas SET situacao = 'suspensa' WHERE id = :f"),
                {"f": cena.sistema.acoes[1].id},
            )
        elif mudanca == "leitura_nova":
            await revisar_ferramenta(c, s, definicao("ver_lembrete"))
        elif mudanca == "contrato_sumiu":
            await c.execute(
                text(
                    "UPDATE ferramentas_vistas SET sumiu_em = now() WHERE sistema_id = :s AND nome = 'selo_consultar'"
                ),
                {"s": s},
            )
        elif mudanca == "contrato_mudou":
            outra = apoio.definicao_do_contrato("selo_consultar")
            outra["inputSchema"] = {"type": "object", "properties": {"operacao": {"type": "integer"}}}
            await apoio.listar_contrato(c, s, 3, definicoes={"selo_consultar": outra})
        elif mudanca == "contrato_mudou_saida":  # a entrada igual; a saida perdeu um estado do vocabulario
            outra = apoio.definicao_do_contrato("selo_consultar")
            saida = json.loads(json.dumps(outra["outputSchema"]))
            saida["properties"]["estado"]["enum"] = [e for e in contrato.ESTADOS if e != "nao_recebida"]
            outra["outputSchema"] = saida
            await apoio.listar_contrato(c, s, 3, definicoes={"selo_consultar": outra})
        elif mudanca == "alvo_sumiu":  # SELO-0.3: o sistema deixou de listar selo_alvo
            await c.execute(
                text("UPDATE ferramentas_vistas SET sumiu_em = now() WHERE sistema_id = :s AND nome = 'selo_alvo'"),
                {"s": s},
            )
        elif mudanca == "contrato_do_kit":
            entrada, saida, _ = _esquemas_do_kit()
            kit = {"name": "selo_consultar", "description": "kit", "inputSchema": entrada, "outputSchema": saida}
            await apoio.listar_contrato(c, s, 3, definicoes={"selo_consultar": kit})
        elif mudanca == "retirado":
            await c.execute(
                text(
                    "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                    " SELECT :s, teste_id, 'admin', 'pedido do teste', 'admin-teste', :a FROM selo_vigente(:s)"
                ),
                {"s": s, "a": cena.admin_id},
            )

    await no_dono(dono, mudar)
    selo = await vigente(dono, s)
    assert (selo.motivo, selo.nivel) == (motivo, 2 if motivo == "vigente" else 1)


async def _mudar_o_sistema(c: AsyncConnection, cena: Cena, mudanca: str) -> None:
    """Uma das mudancas que derrubam o selo, sem reativar o sistema (quem quiser, reativa depois)."""
    s = {"s": cena.sistema.id}
    if mudanca == "reprovado":
        await apoio.gravar_teste(c, cena.sistema.id, aprovado=False, admin_id=cena.admin_id)
    elif mudanca == "retirado":
        await c.execute(
            text(
                "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                " SELECT :s, teste_id, 'admin', 'ordem dos motivos', 'admin-teste', :a FROM selo_vigente(:s)"
            ),
            s | {"a": cena.admin_id},
        )
    elif mudanca == "desativado":
        await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), s)
    elif mudanca == "endereco":
        await c.execute(text("UPDATE sistemas SET endereco = 'https://outro.exemplo.teste/mcp' WHERE id = :s"), s)
    elif mudanca == "tipo_chave":
        await c.execute(text("UPDATE sistemas SET tipo_chave = 'programa' WHERE id = :s"), s)
    elif mudanca == "acao_nova":
        await revisar_ferramenta(c, cena.sistema.id, definicao("criar_lembrete"), classe="tem_volta")
    elif mudanca == "contrato_sumiu":
        await c.execute(text("UPDATE ferramentas_vistas SET sumiu_em = now() WHERE sistema_id = :s"), s)
    else:
        raise AssertionError(mudanca)


@pytest.mark.parametrize(
    ("mudancas", "reativar", "dias", "motivo"),
    [
        (("reprovado",), False, 91, "reprovado"),
        (("retirado",), False, 91, "vencido"),
        (("retirado", "desativado"), False, 0, "retirado"),
        (("endereco",), False, 0, "fora_de_ativo"),
        (("endereco", "tipo_chave"), True, 0, "endereco_mudou"),
        (("tipo_chave", "acao_nova"), True, 0, "tipo_chave_mudou"),
        (("acao_nova", "contrato_sumiu"), False, 0, "ferramenta_nova"),
    ],
)
async def test_ordem_dos_motivos_quando_dois_valem_juntos(
    dono: AsyncEngine, cena: Cena, mudancas: tuple[str, ...], reativar: bool, dias: int, motivo: str
) -> None:
    """Mutacao que sobreviveu na conferencia final: o motivo sai na primeira regra que vale, na ordem de
    MOTIVOS_DO_SELO (desenho 4.4). Cada par seguido da ordem, com as duas regras valendo ao mesmo tempo, da a primeira:
    num teste retirado de um sistema desativado, a tela diz 'retirado', e nao 'saiu de ativo'."""
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> Any:
        for mudanca in mudancas:
            await _mudar_o_sistema(c, cena, mudanca)
        if reativar:
            await _reativar(c, cena.sistema.id)
        agora = (await c.execute(text("SELECT now()"))).scalar_one()
        return await apoio.selo_vigente(c, cena.sistema.id, agora + timedelta(days=dias))

    selo = await num_rascunho(dono, passos)
    assert (selo.nivel, selo.motivo) == (1, motivo)
    ordem = contrato.MOTIVOS_DO_SELO
    assert ordem.index("reprovado") < ordem.index("vencido") < ordem.index("retirado") < ordem.index("fora_de_ativo")


async def test_ordem_dos_motivos_num_terceiro(dono: AsyncEngine, cena: Cena) -> None:
    """Os pares da ordem que so um terceiro tem: tipo de chave antes da revisao nova, e revisao nova antes da
    ferramenta nova (uma acao nova publicada numa revisao nova)."""

    async def montar(c: AsyncConnection) -> uuid.UUID:
        terceiro = await cadastrar_terceiro(c)
        await revisar_ferramenta(c, terceiro, definicao("enviar_x"), classe="muda")
        await gravar_revisao_do_servidor(c, terceiro)
        await apoio.listar_contrato(c, terceiro, 2)
        await apoio.gravar_teste(c, terceiro, admin_id=cena.admin_id)
        return terceiro

    terceiro = await no_dono(dono, montar)

    async def revisao_e_acao_nova(c: AsyncConnection) -> Any:
        await revisar_ferramenta(c, terceiro, definicao("apagar_x"), classe="muda")
        await gravar_revisao_do_servidor(c, terceiro)
        return await apoio.selo_vigente(c, terceiro)

    async def chave_e_revisao_nova(c: AsyncConnection) -> Any:
        await gravar_revisao_do_servidor(c, terceiro)
        await c.execute(text("UPDATE sistemas SET tipo_chave = 'compartilhada' WHERE id = :s"), {"s": terceiro})
        await _reativar(c, terceiro)
        return await apoio.selo_vigente(c, terceiro)

    assert (await num_rascunho(dono, revisao_e_acao_nova)).motivo == "revisao_nova"
    assert (await num_rascunho(dono, chave_e_revisao_nova)).motivo == "tipo_chave_mudou"


async def test_contrato_lido_ha_mais_de_48_horas_derruba_o_selo(dono: AsyncEngine, cena: Cena) -> None:
    await com_teste(dono, cena)
    assert (await vigente(dono, cena.sistema.id, timedelta(hours=47))).motivo == "vigente"
    assert (await vigente(dono, cena.sistema.id, timedelta(hours=49))).motivo == "contrato_sumiu"
    assert contrato.VISTA_VALE_HORAS == 48


async def test_selo_de_nivel_3_pede_o_selo_bloqueio_listado(dono: AsyncEngine, cena: Cena) -> None:
    async def so_nivel_2(c: AsyncConnection) -> None:
        await c.execute(
            text("DELETE FROM ferramentas_vistas WHERE sistema_id = :s AND nome = 'selo_bloqueio'"),
            {"s": cena.sistema.id},
        )

    await no_dono(dono, so_nivel_2)
    await com_teste(dono, cena, nivel=3)
    assert (await vigente(dono, cena.sistema.id)).motivo == "contrato_sumiu"
    await com_teste(dono, cena, nivel=2)
    assert (await vigente(dono, cena.sistema.id)).motivo == "vigente"


async def test_terceiro_com_revisao_nova_volta_ao_nivel_1(dono: AsyncEngine, cena: Cena) -> None:
    async def montar(c: AsyncConnection) -> uuid.UUID:
        terceiro = await cadastrar_terceiro(c)
        await revisar_ferramenta(c, terceiro, definicao("enviar_x"), classe="muda")
        await gravar_revisao_do_servidor(c, terceiro)
        await apoio.listar_contrato(c, terceiro, 2)
        await apoio.gravar_teste(c, terceiro, admin_id=cena.admin_id)
        return terceiro

    terceiro = await no_dono(dono, montar)
    assert (await vigente(dono, terceiro)).motivo == "vigente"
    await no_dono(dono, lambda c: gravar_revisao_do_servidor(c, terceiro))
    assert (await vigente(dono, terceiro)).motivo == "revisao_nova"


async def test_sistemas_selo_fica_preso_em_1(dono: AsyncEngine, cena: Cena) -> None:
    await recusado(dono, "UPDATE sistemas SET selo = 2 WHERE id = :s", "sistemas_selo_fixo", s=cena.sistema.id)
    for papel in PAPEIS:
        await recusado(
            dono, "UPDATE sistemas SET selo = 2 WHERE id = :s", SEM_PERMISSAO, s=cena.sistema.id, papel=papel
        )


# --- Retiradas ------------------------------------------------------------------------------------------------


RETIRAR: Final = (
    "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
    " VALUES (:s, :t, :m, :d, :q, :p)"
)


async def test_entrada_retira_so_pelo_admin_e_so_o_selo_vigente(dono: AsyncEngine, cena: Cena) -> None:
    antigo = await com_teste(dono, cena)
    teste = await com_teste(dono, cena)
    pessoa = await no_dono(dono, apoio.nova_pessoa)
    base = {"s": cena.sistema.id, "t": teste.id, "m": "admin", "d": "o sistema mudou", "q": "Carla", "p": cena.admin_id}
    await recusado(dono, RETIRAR, "So um ADMIN ativo retira", **(base | {"p": pessoa}), papel=ENTRADA)
    await recusado(dono, RETIRAR, "So se retira o selo vigente", **(base | {"t": antigo.id}), papel=ENTRADA)
    await recusado(dono, RETIRAR, "Diga por que retirar", **(base | {"d": "  "}), papel=ENTRADA)
    await recusado(
        dono,
        RETIRAR,
        "A entrada so retira o selo pelo ADMIN",
        **(base | {"m": "se_contradisse", "p": None, "d": None}),
        papel=ENTRADA,
    )
    await recusado(dono, RETIRAR, "O despachante nao retira o selo em nome do ADMIN", **base, papel=DESPACHANTE)
    await recusado(dono, RETIRAR, SEM_PERMISSAO, **base, papel=ROTINAS)
    # Quem retirou e o ADMIN do cadastro, nunca o que o codigo manda (nem "mcp-claro", que e o das automaticas).
    await fazer(dono, RETIRAR, **(base | {"q": "mcp-claro"}), papel=ENTRADA)
    assert (await vigente(dono, cena.sistema.id)).motivo == "retirado"
    quem = await fazer(dono, "SELECT retirado_por FROM selos_retirados WHERE teste_id = :t", t=teste.id)
    assert quem == "ADMIN de teste"
    await recusado(dono, RETIRAR, "So se retira o selo vigente", **base, papel=ENTRADA)


async def test_retirada_automatica_so_com_a_prova_na_mesma_transacao(dono: AsyncEngine, cena: Cena) -> None:
    """Criticas M7 e B8: 'nao_respondeu_consultas' so com uma consulta que o sistema nao respondeu (sem resposta,
    resposta invalida ou desconhecido) gravada na mesma transacao; nunca com uma falha do MCP-CLARO."""
    teste = await com_teste(dono, cena)

    async def enviada(c: AsyncConnection) -> uuid.UUID:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, op.id)
        return op.id

    base = {"s": cena.sistema.id, "t": teste.id, "d": None, "q": "despachante", "p": None}

    async def retirar_depois(c: AsyncConnection, desfecho: str | None, estado: str | None, motivo: str) -> Any:
        op = await enviada(c)
        if desfecho is not None:
            await apoio.informar(c, op, estado, desfecho=desfecho)
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(text(RETIRAR), base | {"m": motivo})
        await c.execute(text("RESET ROLE"))
        return (
            await c.execute(text("SELECT retirado_por FROM selos_retirados WHERE teste_id = :t"), {"t": teste.id})
        ).scalar_one()

    prova = "so nasce de uma consulta que o sistema nao respondeu"
    await falha(dono, lambda c: retirar_depois(c, None, None, "nao_respondeu_consultas"), prova)
    for desfecho in ("sem_chave", "sala_fora", "sistema_fora"):
        await falha(dono, lambda c, d=desfecho: retirar_depois(c, d, None, "nao_respondeu_consultas"), prova)
    await falha(dono, lambda c: retirar_depois(c, "respondeu", "concluida", "nao_respondeu_consultas"), prova)
    for desfecho, estado in (("sem_resposta", None), ("resposta_invalida", None), ("respondeu", "desconhecido")):
        quem = await num_rascunho(
            dono, lambda c, d=desfecho, e=estado: retirar_depois(c, d, e, "nao_respondeu_consultas")
        )
        assert quem == "mcp-claro"
    await falha(
        dono, lambda c: retirar_depois(c, "respondeu", "concluida", "se_contradisse"), "nasce da resposta que contradiz"
    )


async def _enviada_com(engine: AsyncEngine, cena: Cena, estado: str | None = None) -> apoio.OperacaoDeTeste:
    """Uma acao enviada com numero pelo selo vigente agora (gravada), com a resposta ``estado`` do sistema."""

    async def passos(c: AsyncConnection) -> apoio.OperacaoDeTeste:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, op.id)
        if estado is not None:
            await apoio.informar(c, op.id, estado, origem="consulta")
        return op

    return cast(apoio.OperacaoDeTeste, await no_dono(engine, passos))


async def _retiradas(engine: AsyncEngine, sistema_id: uuid.UUID) -> set[tuple[uuid.UUID, str]]:
    return {
        (uuid.UUID(str(t)), str(m))
        for t, m in await linhas(
            engine, "SELECT teste_id, motivo FROM selos_retirados WHERE sistema_id = :s", s=sistema_id
        )
    }


async def test_contradicao_depois_do_reteste_tira_o_selo_de_agora(dono: AsyncEngine, cena: Cena) -> None:
    """D19: o sistema que se contradiz perde o selo na hora. Envio no teste T1, reteste T2 aprovado: a contradicao
    sobre o envio antigo retira T1 e tambem T2, que e o selo de agora."""
    t1 = await com_teste(dono, cena)
    op = await _enviada_com(dono, cena, "concluida")
    t2 = await com_teste(dono, cena)
    antes = await vigente(dono, cena.sistema.id)
    assert (antes.nivel, antes.teste_id) == (2, t2.id)

    async def contradizer(c: AsyncConnection) -> bool:
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        return (await apoio.informar(c, op.id, "nao_recebida")).contradiz

    assert await no_dono(dono, contradizer) is True
    depois = await vigente(dono, cena.sistema.id)
    assert (depois.nivel, depois.motivo, depois.teste_id) == (1, "retirado", t2.id)
    assert await _retiradas(dono, cena.sistema.id) == {(t1.id, "se_contradisse"), (t2.id, "se_contradisse")}


async def test_contradicao_com_o_ultimo_teste_reprovado_retira_so_o_do_envio(dono: AsyncEngine, cena: Cena) -> None:
    t1 = await com_teste(dono, cena)
    op = await _enviada_com(dono, cena, "concluida")
    await com_teste(dono, cena, aprovado=False)
    await no_dono(dono, lambda c: apoio.informar(c, op.id, "recusada"))
    assert await _retiradas(dono, cena.sistema.id) == {(t1.id, "se_contradisse")}
    assert (await vigente(dono, cena.sistema.id)).motivo == "reprovado"


async def test_falta_de_resposta_depois_do_reteste_retira_o_selo_de_agora(dono: AsyncEngine, cena: Cena) -> None:
    """D18: a consulta sem resposta sobre um envio do teste antigo prova a retirada do selo de agora (o ultimo teste)
    e do teste do envio; nunca de um teste que nao e nenhum dos dois."""
    t0 = await com_teste(dono, cena)
    t1 = await com_teste(dono, cena)
    op = await _enviada_com(dono, cena)
    t2 = await com_teste(dono, cena)
    base = {"s": cena.sistema.id, "m": "nao_respondeu_consultas", "d": None, "q": "despachante", "p": None}

    async def retirar(c: AsyncConnection, teste: uuid.UUID) -> Any:
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await apoio.informar(c, op.id)  # sem resposta, sobre o envio do teste T1
        await c.execute(text(RETIRAR), base | {"t": teste})
        return await apoio.selo_vigente(c, cena.sistema.id)

    selo = await num_rascunho(dono, lambda c: retirar(c, t2.id))
    assert (selo.nivel, selo.motivo, selo.teste_id) == (1, "retirado", t2.id)
    assert (await num_rascunho(dono, lambda c: retirar(c, t1.id))).motivo == "vigente"
    await falha(dono, lambda c: retirar(c, t0.id), "so nasce de uma consulta que o sistema nao respondeu")


@pytest.mark.parametrize("motivo", ["nao_respondeu_consultas", "se_contradisse"])
async def test_retirada_automatica_so_com_a_prova_do_proprio_sistema(
    dono: AsyncEngine, cena: Cena, motivo: str
) -> None:
    """A prova (consulta sem resposta, ou resposta que contradiz) de outro sistema, na mesma transacao, nao retira o
    selo deste."""
    teste = await com_teste(dono, cena)

    async def retirar(c: AsyncConnection) -> None:
        outro = await apoio.proprio_com_acoes(c)
        await apoio.gravar_teste(c, outro.id, admin_id=cena.admin_id)
        op = await apoio.criar_operacao(c, outro.acoes[0])
        await apoio.enviar(c, op.id)
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        if motivo == "se_contradisse":
            await apoio.informar(c, op.id, "concluida", origem="recibo")
            assert (await apoio.informar(c, op.id, "nao_recebida")).contradiz
        else:
            await apoio.informar(c, op.id)
        await c.execute(
            text(RETIRAR),
            {"s": cena.sistema.id, "t": teste.id, "m": motivo, "d": None, "q": "despachante", "p": None},
        )

    erro = "nasce da resposta que contradiz" if motivo == "se_contradisse" else "so nasce de uma consulta"
    await falha(dono, retirar, erro)


# --- RESERVADAS -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("nome", contrato.FERRAMENTAS_DO_CONTRATO)
@pytest.mark.parametrize("papel", [None, ENTRADA])
async def test_nomes_do_contrato_nao_sao_revisados(dono: AsyncEngine, cena: Cena, nome: str, papel: str | None) -> None:
    await recusado(
        dono,
        "INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao,"
        " esquema_entrada,"
        " classe, impressao, revisado_por) VALUES (:s, :n, 1, :v, 'd', '{}'::jsonb, 'le', 'sha256:x', 'admin')",
        "Este nome e reservado ao contrato do selo",
        s=cena.sistema.id,
        n=nome,
        v=f"{nome}_{uuid.uuid4().hex[:6]}",
        papel=papel,
    )


async def test_revisada_de_terceiro_continua_com_a_marca_de_estranhos(dono: AsyncEngine) -> None:
    async def revisar(c: AsyncConnection) -> Any:
        terceiro = await cadastrar_terceiro(c)
        r = await revisar_ferramenta(c, terceiro, definicao("ler_x", anotacoes={"readOnlyHint": True}))
        return (
            await c.execute(
                text("SELECT texto_de_fora, etiquetas FROM ferramentas_revisadas WHERE id = :i"), {"i": r.id}
            )
        ).one()

    assert tuple(await no_dono(dono, revisar)) == (True, {})


# --- ENVIO ----------------------------------------------------------------------------------------------------


async def test_envio_nasce_com_o_numero_do_banco_e_o_selo_vigente(dono: AsyncEngine, cena: Cena) -> None:
    teste = await com_teste(dono, cena)

    async def enviar(c: AsyncConnection) -> Any:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        envio = await apoio.gravar_envio(c, op.id)
        await c.execute(text("UPDATE fila_saida SET estado = 'enviando' WHERE operacao_id = :o"), {"o": op.id})
        gravado = (
            await c.execute(text("SELECT gravado_em = now() FROM envios_selo WHERE operacao_id = :o"), {"o": op.id})
        ).scalar_one()
        return envio, gravado

    envio, gravado = await no_dono(dono, enviar)
    assert (envio.nivel, envio.teste_id, envio.repete_operacao_id, gravado) == (2, teste.id, None, True)
    assert contrato.numero_valido(str(envio.chave))


ENVIO: Final = (
    "INSERT INTO envios_selo (operacao_id, sistema_id, nivel, teste_id, chave, repete_operacao_id)"
    " VALUES (:o, :s, :n, :t, :k, :r)"
)


async def test_envio_recusa_nivel_teste_chave_e_fila_errados(dono: AsyncEngine, cena: Cena) -> None:
    antigo = await com_teste(dono, cena)
    teste = await com_teste(dono, cena)

    async def operacao(c: AsyncConnection, liberada: bool = True) -> apoio.OperacaoDeTeste:
        return await apoio.criar_operacao(c, cena.sistema.acoes[0], liberada=liberada)

    op = await no_dono(dono, operacao)
    parada = await no_dono(dono, lambda c: operacao(c, False))
    base = {"o": op.id, "s": cena.sistema.id, "n": 2, "t": teste.id, "k": None, "r": None}
    await recusado(dono, ENVIO, "os do selo vigente do sistema agora", **(base | {"n": 3}), papel=DESPACHANTE)
    await recusado(dono, ENVIO, "os do selo vigente do sistema agora", **(base | {"t": antigo.id}), papel=DESPACHANTE)
    await recusado(dono, ENVIO, "o codigo nao o escolhe", **(base | {"k": uuid.uuid4()}), papel=DESPACHANTE)
    await recusado(dono, ENVIO, "de liberada para enviando", **(base | {"o": parada.id}), papel=DESPACHANTE)
    await recusado(dono, ENVIO, "a propria operacao repete", **(base | {"r": parada.id}), papel=DESPACHANTE)
    for papel in (ENTRADA, ROTINAS):
        await recusado(dono, ENVIO, SEM_PERMISSAO, **base, papel=papel)
    await fazer(dono, ENVIO, **base, papel=DESPACHANTE)


async def test_sem_selo_vigente_nao_ha_envio_com_numero(dono: AsyncEngine, cena: Cena) -> None:
    teste = await com_teste(dono, cena, aprovado=False)

    async def enviar(c: AsyncConnection) -> None:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await c.execute(text(ENVIO), {"o": op.id, "s": cena.sistema.id, "n": 2, "t": teste.id, "k": None, "r": None})

    await falha(dono, enviar, "os do selo vigente do sistema agora")


async def _original_nao_confirmada(c: AsyncConnection, cena: Cena) -> tuple[apoio.OperacaoDeTeste, apoio.Envio]:
    original = await apoio.criar_operacao(c, cena.sistema.acoes[0])
    envio = await apoio.enviar(c, original.id)
    return original, envio


async def _repetir(c: AsyncConnection, cena: Cena, original: apoio.OperacaoDeTeste, **kw: Any) -> apoio.OperacaoDeTeste:
    return await apoio.criar_operacao(
        c,
        cena.sistema.acoes[0],
        pessoa_id=original.pessoa_id,
        aplicativo_id=original.aplicativo_id,
        argumentos=kw.pop("argumentos", original.argumentos),
        repete=original.id,
        **kw,
    )


@pytest.mark.parametrize("estado", [None, "concluida", "em_andamento", "desconhecido", "parcial"])
async def test_pedido_igual_leva_o_mesmo_numero(dono: AsyncEngine, cena: Cena, estado: str | None) -> None:
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> tuple[apoio.Envio, apoio.Envio, uuid.UUID | None]:
        original, envio = await _original_nao_confirmada(c, cena)
        if estado is not None:
            await apoio.informar(c, original.id, estado)
        else:
            await apoio.informar(c, original.id)  # consulta sem resposta
        repetida = await _repetir(c, cena, original)
        # Decisao 5 do coordenador (retoque da etapa 3): o substituto e so o envio que pode ter chegado ao sistema
        # (fila em enviando, enviada ou nao_confirmada); aqui, a porta ja o marcou "enviando".
        novo = await apoio.enviar(c, repetida.id, fim=None)
        substituto = (await c.execute(text("SELECT selo_substituto(:o)"), {"o": original.id})).scalar_one()
        return envio, novo, substituto

    envio, novo, substituto = await num_rascunho(dono, passos)
    assert (novo.chave, novo.repete_operacao_id, substituto) == (envio.chave, envio.operacao_id, novo.operacao_id)


@pytest.mark.parametrize("estado", contrato.ESTADOS_QUE_QUEIMAM)
async def test_pedido_igual_leva_numero_novo_quando_o_sistema_queimou_o_antigo(
    dono: AsyncEngine, cena: Cena, estado: str
) -> None:
    """Critica M4: nao_recebida, recusada, parada e desfeita dao numero novo; o pedido continua marcado como pedido
    igual em operacoes, mas o envio nao repete. nao_recebida, so confirmada (critica A3.3): duas respostas com 1
    minuto entre elas."""
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> tuple[apoio.Envio, apoio.Envio, uuid.UUID | None]:
        original, envio = await _original_nao_confirmada(c, cena)
        primeira = await apoio.informar(c, original.id, estado)
        if estado == "nao_recebida":
            await _informado_em(c, primeira.id, timedelta(minutes=-1))
            await apoio.informar(c, original.id, estado)
        repetida = await _repetir(c, cena, original)
        novo = await apoio.gravar_envio(c, repetida.id)
        substituto = (await c.execute(text("SELECT selo_substituto(:o)"), {"o": original.id})).scalar_one()
        return envio, novo, substituto

    envio, novo, substituto = await num_rascunho(dono, passos)
    assert novo.chave != envio.chave
    assert (novo.repete_operacao_id, substituto) == (None, None)


@pytest.mark.parametrize(
    "estrago",
    [
        "aconteceu",
        "outros_argumentos",
        "oito_dias",
        "sem_envio",
        "ainda_saindo",
        "outra_pessoa",
        "outra_pessoa_mesma_chave",
    ],
)
async def test_pedido_igual_so_repete_uma_nao_confirmada_que_saiu_com_numero(
    dono: AsyncEngine, cena: Cena, estrago: str
) -> None:
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> None:
        if estrago == "sem_envio":
            original = await apoio.criar_operacao(c, cena.sistema.acoes[0])
            await c.execute(
                text("UPDATE fila_saida SET estado = 'enviando' WHERE operacao_id = :o"), {"o": original.id}
            )
            await c.execute(
                text("UPDATE fila_saida SET estado = 'nao_confirmada' WHERE operacao_id = :o"), {"o": original.id}
            )
        else:
            original, _ = await _original_nao_confirmada(c, cena)
        kw: dict[str, Any] = {}
        if estrago == "aconteceu":
            await c.execute(
                text(
                    "INSERT INTO decisoes_nao_confirmada (operacao_id, decisao, decidida_por)"
                    " VALUES (:o, 'aconteceu', :p)"
                ),
                {"o": original.id, "p": original.pessoa_id},
            )
        elif estrago == "outros_argumentos":
            kw["argumentos"] = {"para": "outra@exemplo.teste", "texto": "teste"}
        elif estrago == "oito_dias":
            await c.execute(
                text("UPDATE operacoes SET criada_em = now() - interval '8 days' WHERE id = :o"), {"o": original.id}
            )
        elif estrago == "ainda_saindo":
            original = await apoio.criar_operacao(c, cena.sistema.acoes[0])
            await apoio.enviar(c, original.id, fim=None)
        elif estrago == "outra_pessoa":
            await apoio.criar_operacao(c, cena.sistema.acoes[0], repete=original.id)
            return
        elif estrago == "outra_pessoa_mesma_chave":
            # A chave do pedido igual e calculada pelo codigo; aqui ela vem igual a da original, de outra pessoa, e so
            # a conferencia da pessoa no banco segura.
            chave = await c.execute(text("SELECT chave_pedido_igual FROM operacoes WHERE id = :o"), {"o": original.id})
            await apoio.criar_operacao(
                c,
                cena.sistema.acoes[0],
                aplicativo_id=original.aplicativo_id,
                argumentos=original.argumentos,
                repete=original.id,
                chave_pedido_igual=str(chave.scalar_one()),
            )
            return
        await _repetir(c, cena, original, **kw)

    await falha(dono, passos, "O pedido igual so repete o numero de uma acao nao confirmada que saiu com numero")


async def test_substituto_e_o_ultimo_da_cadeia_de_pedidos_iguais(dono: AsyncEngine, cena: Cena) -> None:
    """Mutacao que sobreviveu na conferencia final: na cadeia O -> R1 -> R2, todas com o mesmo numero, quem fala por O
    (e por R1) e R2, o ultimo; nunca R1."""
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        o, envio_o = await _original_nao_confirmada(c, cena)
        r1 = await _repetir(c, cena, o)
        envio_r1 = await apoio.enviar(c, r1.id)
        r2 = await apoio.criar_operacao(
            c,
            cena.sistema.acoes[0],
            pessoa_id=o.pessoa_id,
            aplicativo_id=o.aplicativo_id,
            argumentos=o.argumentos,
            repete=r1.id,
        )
        envio_r2 = await apoio.enviar(c, r2.id)
        substitutos = [
            (await c.execute(text("SELECT selo_substituto(:o)"), {"o": x})).scalar_one() for x in (o.id, r1.id, r2.id)
        ]
        numeros = {envio_o.chave, envio_r1.chave, envio_r2.chave}
        return substitutos, len(numeros), envio_r2.repete_operacao_id, r2.id

    substitutos, numeros, r2_repete, r2 = await num_rascunho(dono, passos)
    assert numeros == 1
    assert substitutos == [r2, r2, None]
    assert r2_repete is not None


async def test_envio_nunca_copia_o_numero_de_outro_sistema(dono: AsyncEngine, cena: Cena) -> None:
    """Mutacao que sobreviveu na conferencia final: mesmo que um pedido igual aponte para a acao de outro sistema
    (aqui, com a conferencia da entrada desligada pelo dono na transacao), o envio sai com numero novo: o numero de um
    sistema nunca vai a outro."""
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> tuple[apoio.Envio, apoio.Envio]:
        outro = await apoio.proprio_com_acoes(c)
        await apoio.gravar_teste(c, outro.id, admin_id=cena.admin_id)
        original = await apoio.criar_operacao(c, outro.acoes[0])
        envio_original = await apoio.enviar(c, original.id)
        await c.execute(text("ALTER TABLE operacoes DISABLE TRIGGER operacoes_repete_confere"))
        repetida = await _repetir(c, cena, original)
        await c.execute(text("ALTER TABLE operacoes ENABLE TRIGGER operacoes_repete_confere"))
        return envio_original, await apoio.gravar_envio(c, repetida.id)

    original, novo = await num_rascunho(dono, passos)
    assert novo.chave != original.chave
    assert novo.repete_operacao_id is None


@pytest.mark.parametrize(("idade", "mesmo"), [(timedelta(days=6, hours=23), True), (timedelta(days=7, hours=1), False)])
async def test_pedido_igual_so_copia_o_numero_de_um_envio_de_ate_7_dias(
    dono: AsyncEngine, cena: Cena, idade: timedelta, mesmo: bool
) -> None:
    """O pedido igual nasce ate 7 dias depois da original, mas pode sair depois (a pessoa confirma a ficha mais
    tarde): com o envio original de mais de 7 dias, o numero e novo (abaixo dos 30 dias de guarda do contrato)."""
    await com_teste(dono, cena)
    assert contrato.REAPROVEITAR_NUMERO_DIAS == 7

    async def passos(c: AsyncConnection) -> tuple[apoio.Envio, apoio.Envio]:
        original, envio = await _original_nao_confirmada(c, cena)
        repetida = await _repetir(c, cena, original)
        await _envio_em(c, original.id, -idade)
        return envio, await apoio.gravar_envio(c, repetida.id)

    envio, novo = await num_rascunho(dono, passos)
    assert (novo.chave == envio.chave, novo.repete_operacao_id == envio.operacao_id) == (mesmo, mesmo)


async def _informado_em(c: AsyncConnection, informacao_id: int, deslocamento: timedelta) -> None:
    """Muda a hora de uma linha de informacoes_sistema para agora mais ``deslocamento`` (o dono desliga a guarda de
    "so de acrescentar" nesta transacao, que volta atras). Na mesma transacao, now() nao anda."""
    await c.execute(text("ALTER TABLE informacoes_sistema DISABLE TRIGGER informacoes_sistema_sem_update_delete"))
    await c.execute(
        text("UPDATE informacoes_sistema SET gravado_em = now() + CAST(:d AS interval) WHERE id = :i"),
        {"d": deslocamento, "i": informacao_id},
    )
    await c.execute(text("ALTER TABLE informacoes_sistema ENABLE TRIGGER informacoes_sistema_sem_update_delete"))


@pytest.mark.parametrize(
    ("respostas", "queima"),
    [
        ([("original", "consulta", 0)], False),
        ([("original", "consulta", -0.5), ("original", "consulta", 0)], False),
        ([("original", "consulta", -59 / 60), ("original", "recibo", 0)], False),
        ([("original", "consulta", -1), ("original", "consulta", 0)], True),
        ([("original", "consulta", -1), ("original", "recibo", 0)], True),
        ([("original", "consulta", -1), ("repetida", "recibo", 0)], True),
        ([("original", "consulta", -0.5), ("repetida", "recibo", 0)], False),
        ([("repetida", "recibo", -2), ("repetida", "consulta", 0)], True),
    ],
)
async def test_uma_nao_recebida_sozinha_nao_queima_o_numero(
    dono: AsyncEngine, cena: Cena, respostas: list[tuple[str, str, float]], queima: bool
) -> None:
    """Critica A3.3 inteira (conferencia do G0r): uma nao_recebida sozinha pode ser a de um sistema que registra o
    numero so no fim da escrita. Ela nao queima o numero: o pedido igual sai com o mesmo, e o sistema honesto devolve
    o desfecho da primeira (ou nao_recebida de novo). So duas respostas nao_recebida sobre o numero, com ao menos 1
    minuto entre a primeira e a ultima, dao numero novo. Contam as respostas sobre o numero: a original O e o pedido
    igual R que saiu com ele; aqui R2 repete R."""
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> tuple[bool, bool]:
        original, envio_o = await _original_nao_confirmada(c, cena)
        repetida = await _repetir(c, cena, original)
        envio_r = await apoio.enviar(c, repetida.id)
        ops = {"original": original.id, "repetida": repetida.id}
        for quem, origem, minutos in respostas:
            info = await apoio.informar(c, ops[quem], "nao_recebida", origem=origem)
            await _informado_em(c, info.id, timedelta(minutes=minutos))
        mais_uma = await _repetir(c, cena, repetida)
        envio_r2 = await apoio.gravar_envio(c, mais_uma.id)
        return envio_r.chave == envio_o.chave, envio_r2.chave == envio_o.chave

    r_tem_o_numero, r2_tem_o_numero = await num_rascunho(dono, passos)
    assert r_tem_o_numero
    assert r2_tem_o_numero is not queima


async def test_uma_nao_recebida_sozinha_da_o_mesmo_numero_ao_pedido_igual(dono: AsyncEngine, cena: Cena) -> None:
    """A sonda do verificador do G0r: a original O recebe uma consulta nao_recebida; o pedido igual R sai depois. Antes
    da correcao, o banco ja mostrava nao_recebida como final e dava numero novo a R (a acao podia acontecer duas
    vezes). Agora O fica sem estado final e R leva o numero de O; a segunda resposta sobre O que diz concluida e
    contradicao, e o selo cai."""
    await com_teste(dono, cena, nivel=2)

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        original, envio_o = await _original_nao_confirmada(c, cena)
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await apoio.informar(c, original.id, "nao_recebida", origem="consulta")
        await c.execute(text("RESET ROLE"))
        final = (await c.execute(text("SELECT selo_estado_final(:o)"), {"o": original.id})).scalar_one()
        repetida = await _repetir(c, cena, original)
        envio_r = await apoio.gravar_envio(c, repetida.id)
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        depois = await apoio.informar(c, original.id, "concluida", origem="consulta")
        await c.execute(text("RESET ROLE"))
        selo = await apoio.selo_vigente(c, cena.sistema.id)
        return final, envio_o.chave == envio_r.chave, envio_r.repete_operacao_id == original.id, depois.contradiz, selo

    final, mesmo, repete, contradiz, selo = await num_rascunho(dono, passos)
    assert (final, mesmo, repete, contradiz) == (None, True, True, True)
    assert (selo.nivel, selo.motivo) == (1, "retirado")


async def test_original_sem_envio_vira_numero_novo_e_nunca_erro(dono: AsyncEngine, cena: Cena) -> None:
    """Critica A1: mesmo que um pedido igual aponte para uma original sem envio (aqui, com a conferencia da entrada
    desligada pelo dono na transacao), o envio sai com numero novo, sem erro: a porta nunca para por causa do selo."""
    await com_teste(dono, cena)

    async def passos(c: AsyncConnection) -> apoio.Envio:
        original = await apoio.criar_operacao(c, cena.sistema.acoes[0], liberada=False)
        await c.execute(text("ALTER TABLE operacoes DISABLE TRIGGER operacoes_repete_confere"))
        repetida = await _repetir(c, cena, original)
        await c.execute(text("ALTER TABLE operacoes ENABLE TRIGGER operacoes_repete_confere"))
        return await apoio.gravar_envio(c, repetida.id)

    envio = await num_rascunho(dono, passos)
    assert envio.repete_operacao_id is None
    assert contrato.numero_valido(str(envio.chave))


# --- INFORMACOES ----------------------------------------------------------------------------------------------


@pytest.fixture
async def enviada(dono: AsyncEngine, cena: Cena) -> apoio.OperacaoDeTeste:
    await com_teste(dono, cena, nivel=3)

    async def passos(c: AsyncConnection) -> apoio.OperacaoDeTeste:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, op.id)
        return op

    return await no_dono(dono, passos)


async def test_contradicao_retira_o_selo_na_mesma_transacao(dono: AsyncEngine, cena: Cena, enviada: Any) -> None:
    assert (await vigente(dono, cena.sistema.id)).nivel == 3

    async def informar(c: AsyncConnection) -> tuple[bool, bool, int]:
        primeira = await apoio.informar(c, enviada.id, "concluida", origem="recibo")
        segunda = await apoio.informar(c, enviada.id, "nao_recebida")
        retiradas = (
            await c.execute(
                text(
                    "SELECT count(*) FROM selos_retirados WHERE sistema_id = :s AND motivo = 'se_contradisse'"
                    " AND retirado_em = now() AND retirado_por = 'mcp-claro' AND pessoa_id IS NULL"
                ),
                {"s": cena.sistema.id},
            )
        ).scalar_one()
        return primeira.contradiz, segunda.contradiz, int(retiradas)

    assert await no_dono(dono, informar) == (False, True, 1)
    selo = await vigente(dono, cena.sistema.id)
    assert (selo.nivel, selo.motivo) == (1, "retirado")
    # Outra contradicao do mesmo teste nao grava outra retirada.
    await no_dono(dono, lambda c: apoio.informar(c, enviada.id, "em_andamento"))
    assert await fazer(dono, "SELECT count(*) FROM selos_retirados WHERE sistema_id = :s", s=cena.sistema.id) == 1


@pytest.mark.parametrize(
    "sequencia",
    [
        ["em_andamento", "concluida"],
        ["nao_recebida", "recusada"],
        ["parada", "nao_recebida", "parada"],
        ["parcial", "concluida", "desfeita"],
        ["concluida", "desconhecido", "concluida"],
        ["desconhecido", "nao_recebida"],
    ],
)
async def test_passagens_que_combinam_nao_retiram_o_selo(
    dono: AsyncEngine, cena: Cena, enviada: Any, sequencia: list[str]
) -> None:
    """Critica M2: pela coluna "conta como"; o aviso que chega antes da chamada (nao_recebida, depois recusada no
    recibo) nao e contradicao."""

    async def informar(c: AsyncConnection) -> list[bool]:
        return [(await apoio.informar(c, enviada.id, e)).contradiz for e in sequencia]

    assert await num_rascunho(dono, informar) == [False] * len(sequencia)


@pytest.mark.parametrize(
    "sequencia",
    [
        ["em_andamento", "nao_recebida"],
        ["concluida", "parada"],
        ["nao_recebida", "concluida"],
        ["desfeita", "concluida"],
        ["concluida", "desconhecido", "nao_recebida"],
        ["recusada", "em_andamento"],
    ],
)
async def test_passagens_que_nao_combinam_sao_contradicao(
    dono: AsyncEngine, cena: Cena, enviada: Any, sequencia: list[str]
) -> None:
    async def informar(c: AsyncConnection) -> list[bool]:
        return [(await apoio.informar(c, enviada.id, e)).contradiz for e in sequencia]

    assert (await num_rascunho(dono, informar))[-1] is True


Resposta = tuple[Literal["original", "repetida"], str, str]


@pytest.mark.parametrize(
    ("primeira", "segunda", "contradiz"),
    [
        (("original", "consulta", "concluida"), ("repetida", "recibo", "nao_recebida"), True),
        (("original", "consulta", "em_andamento"), ("repetida", "recibo", "nao_recebida"), True),
        (("repetida", "recibo", "concluida"), ("original", "consulta", "recusada"), True),
        (("original", "consulta", "nao_recebida"), ("repetida", "consulta", "concluida"), True),
        (("original", "consulta", "em_andamento"), ("repetida", "recibo", "concluida"), False),
        (("original", "consulta", "concluida"), ("repetida", "recibo", "concluida"), False),
    ],
)
async def test_contradicao_e_pelo_numero_entre_a_original_e_o_pedido_igual(
    dono: AsyncEngine, cena: Cena, primeira: Resposta, segunda: Resposta, contradiz: bool
) -> None:
    """Defeito 1 da conferencia final: o pedido igual R que saiu com o numero da original O (D16, critica M4) e, para
    o sistema, a mesma escrita. Uma resposta sobre R que nao combina com uma sobre O (ou o contrario) e contradicao, e
    o selo cai na hora (D19), como se fossem sobre a mesma operacao."""
    teste = await com_teste(dono, cena, nivel=3)

    async def passos(c: AsyncConnection) -> tuple[bool, bool, Any, set[tuple[str, str]]]:
        original, envio_o = await _original_nao_confirmada(c, cena)
        repetida = await _repetir(c, cena, original)
        envio_r = await apoio.enviar(c, repetida.id, "enviada")
        ops = {"original": original.id, "repetida": repetida.id}
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        quem, origem, estado = primeira
        assert not (await apoio.informar(c, ops[quem], estado, origem=origem)).contradiz
        quem, origem, estado = segunda
        resultado = (await apoio.informar(c, ops[quem], estado, origem=origem)).contradiz
        await c.execute(text("RESET ROLE"))
        retiradas = {
            (str(t), str(m))
            for t, m in await c.execute(
                text("SELECT teste_id, motivo FROM selos_retirados WHERE sistema_id = :s"), {"s": cena.sistema.id}
            )
        }
        return envio_o.chave == envio_r.chave, resultado, await apoio.selo_vigente(c, cena.sistema.id), retiradas

    mesmo_numero, resultado, selo, retiradas = await num_rascunho(dono, passos)
    assert mesmo_numero
    assert resultado is contradiz
    if contradiz:
        assert (selo.nivel, selo.motivo) == (1, "retirado")
        assert retiradas == {(str(teste.id), "se_contradisse")}
    else:
        assert (selo.nivel, selo.motivo, retiradas) == (3, "vigente", set())


async def _espera_a_trava(engine: AsyncEngine, pid: int) -> None:
    """Espera a conexao ``pid`` ficar parada numa trava consultiva (pg_locks), sem depender de quanto tempo leva."""
    async with engine.connect() as c:
        for _ in range(400):
            esperando = (
                await c.execute(
                    text("SELECT count(*) FROM pg_locks WHERE pid = :p AND locktype = 'advisory' AND NOT granted"),
                    {"p": pid},
                )
            ).scalar_one()
            if esperando:
                return
            await asyncio.sleep(0.025)
    raise AssertionError("a segunda resposta nao esperou a primeira")


@pytest.mark.parametrize(
    ("primeira", "segunda"),
    [
        (("original", "consulta", "concluida"), ("repetida", "recibo", "nao_recebida")),
        (("original", "consulta", "concluida"), ("original", "consulta", "parada")),
        (("repetida", "recibo", "concluida"), ("original", "consulta", "recusada")),
    ],
)
async def test_duas_respostas_ao_mesmo_tempo_sobre_o_numero_se_veem(
    dono: AsyncEngine, cena: Cena, primeira: Resposta, segunda: Resposta
) -> None:
    """Corrida da conferencia do G0r: duas respostas que se contradizem, sobre o mesmo numero (a original e o pedido
    igual, ou a mesma operacao, como o recibo e a resposta ao aviso da critica M2), em duas transacoes ao mesmo tempo,
    como o despachante. A segunda espera a trava do sistema ate a primeira confirmar, ve a linha dela e sai com
    contradiz; o selo cai. Sem a trava, nenhuma via a outra e o sistema ficava com o selo."""
    teste = await com_teste(dono, cena, nivel=3)

    async def montar(c: AsyncConnection) -> dict[str, uuid.UUID]:
        original, _ = await _original_nao_confirmada(c, cena)
        repetida = await _repetir(c, cena, original)
        await apoio.enviar(c, repetida.id, "enviada")
        return {"original": original.id, "repetida": repetida.id}

    ops = await no_dono(dono, montar)
    c1 = await dono.connect()
    c2 = await dono.connect()
    try:
        t1 = await c1.begin()
        t2 = await c2.begin()
        pid2 = (await c2.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await c1.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c2.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        quem, origem, estado = primeira
        a = await apoio.informar(c1, ops[quem], estado, origem=origem)
        quem, origem, estado = segunda
        tarefa = asyncio.ensure_future(apoio.informar(c2, ops[quem], estado, origem=origem))
        await _espera_a_trava(dono, pid2)
        assert not tarefa.done()
        await t1.commit()
        b = await asyncio.wait_for(tarefa, timeout=30)
        await t2.commit()
    finally:
        await c1.close()
        await c2.close()
    selo = await vigente(dono, cena.sistema.id)
    retiradas = await linhas(
        dono, "SELECT teste_id, motivo FROM selos_retirados WHERE sistema_id = :s", s=cena.sistema.id
    )
    assert (a.contradiz, b.contradiz) == (False, True)
    assert (selo.nivel, selo.motivo) == (1, "retirado")
    assert [(str(t), str(m)) for t, m in retiradas] == [(str(teste.id), "se_contradisse")]


async def test_pedido_igual_com_numero_novo_nao_contradiz_a_original(dono: AsyncEngine, cena: Cena) -> None:
    """O outro lado do defeito 1: depois de nao_recebida (o numero antigo esta queimado), o pedido igual sai com numero
    novo e e outra escrita; a resposta sobre ele nao conta contra a da original, nem a de outra operacao."""
    await com_teste(dono, cena, nivel=3)

    async def passos(c: AsyncConnection) -> tuple[bool, list[bool], Any]:
        original, envio_o = await _original_nao_confirmada(c, cena)
        primeira = await apoio.informar(c, original.id, "nao_recebida")
        await _informado_em(c, primeira.id, timedelta(minutes=-1))
        await apoio.informar(c, original.id, "nao_recebida")  # confirmada (critica A3.3)
        repetida = await _repetir(c, cena, original)
        envio_r = await apoio.enviar(c, repetida.id, "enviada")
        outra = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, outra.id, "enviada")
        contradiz = [
            (await apoio.informar(c, repetida.id, "concluida", origem="recibo")).contradiz,
            (await apoio.informar(c, outra.id, "recusada", origem="recibo")).contradiz,
        ]
        return envio_o.chave == envio_r.chave, contradiz, await apoio.selo_vigente(c, cena.sistema.id)

    mesmo_numero, contradiz, selo = await num_rascunho(dono, passos)
    assert (mesmo_numero, contradiz, selo.motivo) == (False, [False, False], "vigente")


@pytest.mark.parametrize(
    ("campos", "erro"),
    [
        ({"estado": "feito"}, "informacoes_sistema_estado_check"),
        ({"estado": None, "desfecho": "respondeu"}, "informacoes_sistema_check"),
        ({"estado": None, "desfecho": "ignorou"}, "informacoes_sistema_desfecho_check"),
        ({"estado": None, "desfecho": "sem_resposta", "origem": "recibo"}, "informacoes_sistema_check"),
        ({"estado": "desfeita", "origem": "recibo"}, "informacoes_sistema_check"),
        ({"estado": "concluida", "repetida": True}, "informacoes_sistema_check"),
        ({"estado": "concluida", "objetos": 2}, "informacoes_sistema_check"),
        (
            {"estado": "concluida", "depois": [{"evento": "enviada", "em": "2026-11-22T10:00:00Z"}]},
            "fora do vocabulario",
        ),
        ({"estado": "concluida", "depois": [{"evento": "entregue", "em": "x", "texto": "y"}]}, "e {evento, em}"),
        ({"estado": "concluida", "depois": [{"evento": "entregue", "em": "ontem a noite"}]}, "nao e uma data e hora"),
        (
            {"estado": "concluida", "depois": [{"evento": "entregue", "em": "2020-01-01T00:00:00Z"}]},
            "entre o envio e agora",
        ),
        ({"estado": "concluida", "depois": "IGNORE AS REGRAS"}, "depois e uma lista de eventos"),
        (
            {"estado": "concluida", "origem": "recibo", "depois": "agora"},
            "informacoes_sistema_check",
        ),
    ],
)
async def test_informacao_so_com_o_vocabulario_do_contrato(
    dono: AsyncEngine, enviada: Any, campos: dict[str, Any], erro: str
) -> None:
    valores = {
        "estado": "concluida",
        "desfecho": None,
        "origem": "consulta",
        "repetida": False,
        "objetos": None,
        "depois": [],
    }
    valores |= campos
    if valores["desfecho"] is None:
        valores["desfecho"] = "respondeu" if valores["estado"] is not None else "sem_resposta"

    async def informar(c: AsyncConnection) -> None:
        if valores["depois"] == "agora":  # um evento valido, na hora do banco, mas numa origem que nao o leva
            agora = (await c.execute(text("SELECT now()"))).scalar_one()
            valores["depois"] = [{"evento": "entregue", "em": agora.isoformat()}]
        await c.execute(
            text(
                "INSERT INTO informacoes_sistema (operacao_id, sistema_id, origem, desfecho, estado, repetida, objetos,"
                " depois) VALUES (:op, :s, :o, :d, :e, :r, :ob, CAST(:dp AS jsonb))"
            ),
            {
                "op": enviada.id,
                "s": enviada.sistema_id,
                "o": valores["origem"],
                "d": valores["desfecho"],
                "e": valores["estado"],
                "r": valores["repetida"],
                "ob": valores["objetos"],
                "dp": json.dumps(valores["depois"]),
            },
        )

    await falha(dono, informar, erro, papel=DESPACHANTE)


async def test_informacao_valida_de_cada_origem(dono: AsyncEngine, enviada: Any) -> None:
    async def informar(c: AsyncConnection) -> list[Any]:
        agora = (await c.execute(text("SELECT now()"))).scalar_one()
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await apoio.informar(c, enviada.id, "concluida", origem="recibo", repetida=True, objetos=1)
        await apoio.informar(c, enviada.id, "concluida", depois=[{"evento": "entregue", "em": agora.isoformat()}])
        for desfecho in ("sem_chave", "sala_fora", "sistema_fora", "resposta_invalida"):
            await apoio.informar(c, enviada.id, desfecho=desfecho)
        return list(
            (
                await c.execute(
                    text("SELECT origem, desfecho, estado FROM informacoes_sistema WHERE operacao_id = :o ORDER BY id"),
                    {"o": enviada.id},
                )
            ).all()
        )

    assert [tuple(x) for x in await num_rascunho(dono, informar)] == [
        ("recibo", "respondeu", "concluida"),
        ("consulta", "respondeu", "concluida"),
        ("consulta", "sem_chave", None),
        ("consulta", "sala_fora", None),
        ("consulta", "sistema_fora", None),
        ("consulta", "resposta_invalida", None),
    ]


async def test_estado_final_e_a_coluna_nova_da_fila_agora(dono: AsyncEngine, enviada: Any) -> None:
    async def informar(c: AsyncConnection) -> tuple[Any, ...]:
        contar = "SELECT com_resposta_do_sistema FROM medicoes.v_fila_agora WHERE estado = 'nao_confirmada'"
        antes = (await c.execute(text(contar))).scalar_one()
        await apoio.informar(c, enviada.id, "em_andamento")
        aberto = (await c.execute(text("SELECT selo_estado_final(:o)"), {"o": enviada.id})).scalar_one()
        meio = (await c.execute(text(contar))).scalar_one()
        await apoio.informar(c, enviada.id, "concluida")
        await apoio.informar(c, enviada.id, "desconhecido")
        final = (await c.execute(text("SELECT selo_estado_final(:o)"), {"o": enviada.id})).scalar_one()
        depois = (await c.execute(text(contar))).scalar_one()
        await c.execute(text("SET LOCAL ROLE grafana_leitor"))
        pelo_grafana = (await c.execute(text(contar))).scalar_one()
        return aberto, final, meio - antes, depois - antes, pelo_grafana == depois

    assert await num_rascunho(dono, informar) == (None, "concluida", 0, 1, True)


# Sequencias de respostas sobre uma operacao: (minutos depois do envio, origem, estado ou o desfecho sem estado).
SEQUENCIAS_DO_ESTADO_FINAL: Final = {
    "uma_nao_recebida": [(1, "consulta", "nao_recebida")],
    "duas_juntas": [(1, "consulta", "nao_recebida"), (1.5, "recibo", "nao_recebida")],
    "duas_a_59_segundos": [(1, "consulta", "nao_recebida"), (1 + 59 / 60, "consulta", "nao_recebida")],
    "duas_a_1_minuto": [(1, "recibo", "nao_recebida"), (2, "consulta", "nao_recebida")],
    "nao_recebida_e_sem_resposta": [(1, "consulta", "nao_recebida"), (3, "consulta", "sem_resposta")],
    "nao_recebida_e_recusada": [(1, "consulta", "nao_recebida"), (3, "consulta", "recusada")],
    "parcial_e_concluida": [(1, "recibo", "parcial"), (3, "consulta", "concluida")],
    "concluida_e_desfeita": [(1, "recibo", "concluida"), (3, "consulta", "desfeita")],
    "concluida_e_nao_recebida": [(1, "recibo", "concluida"), (3, "consulta", "nao_recebida")],
    "recusada_e_nao_recebida_confirmada": [
        (1, "recibo", "recusada"),
        (2, "consulta", "nao_recebida"),
        (4, "consulta", "nao_recebida"),
    ],
    "nao_recebida_e_em_andamento": [(1, "consulta", "nao_recebida"), (3, "consulta", "em_andamento")],
    "em_aberto": [(1, "recibo", "em_andamento"), (3, "consulta", "desconhecido")],
}


@pytest.mark.parametrize("nome", SEQUENCIAS_DO_ESTADO_FINAL)
async def test_estado_final_do_banco_e_o_que_encerra_a_escada(dono: AsyncEngine, cena: Cena, nome: str) -> None:
    """Critica A3.3 inteira (conferencia do G0r): selo_estado_final (a tela e o botao de um toque) e a coluna
    com_resposta_do_sistema de v_fila_agora (lida pelo Grafana) dizem o mesmo que contrato.resposta_que_encerra (a
    rota e as rotinas). Uma nao_recebida sozinha, ou duas com menos de 1 minuto entre elas, nao e final."""
    await com_teste(dono, cena, nivel=2)
    sequencia = SEQUENCIAS_DO_ESTADO_FINAL[nome]
    contar = "SELECT coalesce(sum(com_resposta_do_sistema), 0) FROM medicoes.v_fila_agora"

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        antes = (await c.execute(text(contar))).scalar_one()
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, op.id)
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        ids = []
        for _, origem, valor in sequencia:
            estado = None if valor in contrato.DESFECHOS else valor
            ids.append((await apoio.informar(c, op.id, estado, origem=origem)).id)
        await c.execute(text("RESET ROLE"))
        agora = (await c.execute(text("SELECT now()"))).scalar_one()
        for info, (minutos, _, _) in zip(ids, sequencia, strict=True):
            await _informado_em(c, info, timedelta(minutes=minutos - 10))
        linhas_da_escada = [
            contrato.LinhaInformada(str(x.origem), str(x.desfecho), x.estado, x.gravado_em)
            for x in await c.execute(
                text(
                    "SELECT origem, desfecho, estado, gravado_em FROM informacoes_sistema WHERE operacao_id = :o"
                    " ORDER BY gravado_em, id"
                ),
                {"o": op.id},
            )
        ]
        assert [x.gravado_em - agora for x in linhas_da_escada] == [timedelta(minutes=m - 10) for m, _, _ in sequencia]
        final = (await c.execute(text("SELECT selo_estado_final(:o)"), {"o": op.id})).scalar_one()
        na_fila = (await c.execute(text(contar))).scalar_one() - antes
        await c.execute(text("SET LOCAL ROLE grafana_leitor"))
        pelo_grafana = (await c.execute(text(contar))).scalar_one() - antes
        return final, contrato.resposta_que_encerra(linhas_da_escada), na_fila, pelo_grafana

    final, pela_escada, na_fila, pelo_grafana = await num_rascunho(dono, passos)
    esperado = {
        "duas_a_1_minuto": "nao_recebida",
        "nao_recebida_e_recusada": "recusada",
        "parcial_e_concluida": "concluida",
        "concluida_e_desfeita": "desfeita",
        "concluida_e_nao_recebida": "concluida",
        "recusada_e_nao_recebida_confirmada": "nao_recebida",
    }.get(nome)
    assert (final, pela_escada) == (esperado, esperado)
    assert (na_fila, pelo_grafana) == ((0, 0) if esperado is None else (1, 1))


async def test_estado_final_confirma_so_com_as_respostas_da_propria_operacao(dono: AsyncEngine, cena: Cena) -> None:
    """Mutacao que sobreviveu no retoque: na tela (selo_estado_final e v_fila_agora), a nao_recebida so se confirma
    com as respostas da propria operacao, como a escada (contrato.resposta_que_encerra le as linhas dela). Uma
    nao_recebida de outra operacao do mesmo sistema nunca confirma; a do pedido igual que saiu com o mesmo numero
    tambem nao (cada um tem a propria escada), mas ela queima o numero para o pedido igual seguinte, que conta pelo
    numero."""
    await com_teste(dono, cena, nivel=2)
    contar = "SELECT coalesce(sum(com_resposta_do_sistema), 0) FROM medicoes.v_fila_agora"

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        antes = (await c.execute(text(contar))).scalar_one()
        original, envio_o = await _original_nao_confirmada(c, cena)
        outra, _ = await _original_nao_confirmada(c, cena)
        primeira = await apoio.informar(c, original.id, "nao_recebida")
        await _informado_em(c, primeira.id, timedelta(minutes=-2))
        await apoio.informar(c, outra.id, "nao_recebida")
        repetida = await _repetir(c, cena, original)
        envio_r = await apoio.enviar(c, repetida.id)
        await apoio.informar(c, repetida.id, "nao_recebida", origem="recibo")
        finais = [
            (await c.execute(text("SELECT selo_estado_final(:o)"), {"o": op})).scalar_one()
            for op in (original.id, outra.id, repetida.id)
        ]
        na_fila = (await c.execute(text(contar))).scalar_one() - antes
        seguinte = await _repetir(c, cena, repetida)
        envio_s = await apoio.gravar_envio(c, seguinte.id)
        return finais, na_fila, envio_r.chave == envio_o.chave, envio_s.chave == envio_o.chave

    finais, na_fila, r_mesmo, seguinte_mesmo = await num_rascunho(dono, passos)
    assert (finais, na_fila) == ([None, None, None], 0)
    assert (r_mesmo, seguinte_mesmo) == (True, False)


# --- AVISOS_BLOQUEIO ------------------------------------------------------------------------------------------


@dataclass
class Bloqueio:
    id: uuid.UUID
    op: apoio.OperacaoDeTeste


async def _bloquear(
    c: AsyncConnection,
    op: apoio.OperacaoDeTeste,
    *,
    no_comprovante: bool = True,
    alcance: str = "pessoa",
    tipo: str = "bloqueio",
) -> uuid.UUID:
    bloqueio = (
        await c.execute(
            text(
                "INSERT INTO bloqueios (tipo, alcance, pessoa_id, sistema_id, quem, motivo)"
                " VALUES (:t, :al, :p, :s, 'pessoa', 'teste') RETURNING id"
            ),
            {
                "t": tipo,
                "al": alcance,
                "p": op.pessoa_id if alcance == "pessoa" else None,
                "s": op.sistema_id if alcance == "sistema" else None,
            },
        )
    ).scalar_one()
    em_conferencia = [{"protocolo": op.protocolo, "estado": "nao_confirmada", "desde": "x"}] if no_comprovante else []
    await c.execute(
        text("INSERT INTO comprovantes (bloqueio_id, em_conferencia) VALUES (:b, CAST(:e AS jsonb))"),
        {"b": bloqueio, "e": json.dumps(em_conferencia)},
    )
    return uuid.UUID(str(bloqueio))


AVISO: Final = (
    "INSERT INTO avisos_bloqueio (bloqueio_id, sistema_id, pessoa_id, parte, operacoes)"
    " VALUES (:b, :s, :p, :parte, CAST(:o AS uuid[])) RETURNING id"
)


async def _avisar(c: AsyncConnection, bloqueio: uuid.UUID, ops: list[apoio.OperacaoDeTeste], **kw: Any) -> uuid.UUID:
    valores = {"b": bloqueio, "s": ops[0].sistema_id, "p": ops[0].pessoa_id, "parte": 1, "o": [o.id for o in ops]} | kw
    await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
    aviso = (await c.execute(text(AVISO), valores)).scalar_one()
    await c.execute(text("RESET ROLE"))
    return uuid.UUID(str(aviso))


# A tarefa do aviso reserva a vez antes de chamar o sistema (critica M8).
RESERVAR_A_VEZ: Final = (
    "UPDATE avisos_bloqueio SET tentativas = tentativas + 1, proxima_em = now() + interval '1 minute' WHERE id = :a"
)


async def test_aviso_de_bloqueio_nasce_do_comprovante_pelo_despachante(
    dono: AsyncEngine, cena: Cena, enviada: Any
) -> None:
    async def passos(c: AsyncConnection) -> Any:
        bloqueio = await _bloquear(c, enviada)
        aviso = await _avisar(c, bloqueio, [enviada])
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(
            text("INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) VALUES (:b, 1)"), {"b": bloqueio}
        )
        await c.execute(text(RESERVAR_A_VEZ), {"a": aviso})
        await apoio.informar(c, enviada.id, "parada", origem="aviso", aviso_id=aviso)
        await c.execute(
            text("UPDATE avisos_bloqueio SET estado = 'respondido', tentativas = 1 WHERE id = :a"), {"a": aviso}
        )
        return (
            await c.execute(text("SELECT estado, tentativas FROM avisos_bloqueio WHERE id = :a"), {"a": aviso})
        ).one()

    assert tuple(await num_rascunho(dono, passos)) == ("respondido", 1)
    for papel in (ENTRADA, ROTINAS):
        assert await fazer(dono, "SELECT has_table_privilege(:p, 'avisos_bloqueio', 'INSERT')", p=papel) is False


@pytest.mark.parametrize(
    "estrago",
    [
        "nivel_2",
        "outra_pessoa",
        "fora_do_comprovante",
        "sem_comprovante",
        "repetida",
        "numero_repetido",
        "outro_sistema",
        "pausa",
    ],
)
async def test_aviso_de_bloqueio_recusa_o_que_nao_e_dele(dono: AsyncEngine, cena: Cena, estrago: str) -> None:
    """Inclui a decisao do coordenador na etapa 1: a pausa nao avisa os sistemas (so o bloqueio)."""
    await com_teste(dono, cena, nivel=2 if estrago == "nivel_2" else 3)
    erros = {
        "nivel_2": "so leva operacao de nivel 3",
        "outra_pessoa": "so leva operacao de nivel 3",
        "fora_do_comprovante": "so leva operacao de nivel 3",
        "sem_comprovante": "nasce do comprovante do bloqueio",
        "repetida": "Operacao repetida",
        "numero_repetido": "Cada numero vai uma vez so",
        "outro_sistema": "so leva operacao de nivel 3",
        "pausa": "So um bloqueio avisa os sistemas; uma pausa nao",
    }

    async def passos(c: AsyncConnection) -> None:
        op = await apoio.criar_operacao(c, cena.sistema.acoes[0])
        await apoio.enviar(c, op.id)
        if estrago == "sem_comprovante":
            bloqueio = (
                await c.execute(
                    text(
                        "INSERT INTO bloqueios (tipo, alcance, quem, motivo)"
                        " VALUES ('bloqueio', 'tudo', 'a', 'b') RETURNING id"
                    )
                )
            ).scalar_one()
        else:
            bloqueio = await _bloquear(
                c,
                op,
                no_comprovante=estrago != "fora_do_comprovante",
                tipo="pausa" if estrago == "pausa" else "bloqueio",
            )
        kw: dict[str, Any] = {}
        ops = [op]
        if estrago == "outra_pessoa":
            kw["p"] = await apoio.nova_pessoa(c)
        elif estrago == "repetida":
            ops = [op, op]
        elif estrago == "numero_repetido":
            repetida = await _repetir(c, cena, op)
            await apoio.enviar(c, repetida.id)
            # O pedido igual entra pelo alcance (em aberto), com o mesmo numero da original.
            await apoio.informar(c, repetida.id, "em_andamento")
            ops = [op, repetida]
        elif estrago == "outro_sistema":
            kw["s"] = (await apoio.proprio_com_acoes(c)).id
        await _avisar(c, bloqueio, ops, **kw)

    await falha(dono, passos, erros[estrago])


async def test_aviso_leva_tambem_o_envio_em_aberto_do_alcance(dono: AsyncEngine, cena: Cena, enviada: Any) -> None:
    """Critica B9: uma acao de nivel 3 ainda em andamento, fora da janela do comprovante, entra no aviso se for do
    alcance do bloqueio; uma concluida, nao."""

    async def passos(c: AsyncConnection, estado: str) -> uuid.UUID:
        await apoio.informar(c, enviada.id, estado)
        bloqueio = await _bloquear(c, enviada, no_comprovante=False)
        return await _avisar(c, bloqueio, [enviada])

    assert await num_rascunho(dono, lambda c: passos(c, "em_andamento"))
    await falha(dono, lambda c: passos(c, "concluida"), "so leva operacao de nivel 3")


async def _envio_em(c: AsyncConnection, operacao_id: uuid.UUID, deslocamento: timedelta) -> None:
    """Muda a hora do envio (o dono desliga a guarda de "so de acrescentar" nesta transacao, que volta atras)."""
    await c.execute(text("ALTER TABLE envios_selo DISABLE TRIGGER envios_selo_sem_update_delete"))
    await c.execute(
        text("UPDATE envios_selo SET gravado_em = now() + CAST(:d AS interval) WHERE operacao_id = :o"),
        {"d": deslocamento, "o": operacao_id},
    )
    await c.execute(text("ALTER TABLE envios_selo ENABLE TRIGGER envios_selo_sem_update_delete"))


@pytest.mark.parametrize(
    ("deslocamento", "entra"),
    [
        (-timedelta(days=29, hours=23), True),
        (-timedelta(days=30, minutes=1), False),
        (timedelta(minutes=1), False),
    ],
)
async def test_aviso_leva_o_envio_em_aberto_so_ate_30_dias_antes_do_bloqueio(
    dono: AsyncEngine, cena: Cena, enviada: Any, deslocamento: timedelta, entra: bool
) -> None:
    """Critica B9: o envio em aberto do alcance entra no aviso se saiu ate 30 dias antes do bloqueio (a guarda dos
    numeros que o contrato pede), e nunca depois dele."""
    assert contrato.AVISO_ENVIOS_EM_ABERTO_DIAS == 30

    async def passos(c: AsyncConnection) -> uuid.UUID:
        await apoio.informar(c, enviada.id, "em_andamento")
        await _envio_em(c, enviada.id, deslocamento)
        bloqueio = await _bloquear(c, enviada, no_comprovante=False)
        return await _avisar(c, bloqueio, [enviada])

    if entra:
        assert await num_rascunho(dono, passos)
    else:
        await falha(dono, passos, "so leva operacao de nivel 3")


async def test_resposta_ao_aviso_e_de_uma_operacao_do_proprio_aviso(
    dono: AsyncEngine, cena: Cena, enviada: Any
) -> None:
    async def passos(c: AsyncConnection) -> None:
        outra = await apoio.criar_operacao(c, cena.sistema.acoes[0], pessoa_id=enviada.pessoa_id)
        await apoio.enviar(c, outra.id)
        aviso = await _avisar(c, await _bloquear(c, enviada), [enviada])
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(text(RESERVAR_A_VEZ), {"a": aviso})
        await apoio.informar(c, enviada.id, "parada", origem="aviso", aviso_id=aviso)
        await apoio.informar(c, outra.id, "parada", origem="aviso", aviso_id=aviso)

    await falha(dono, passos, "A resposta ao aviso e de uma operacao do proprio aviso")


@pytest.mark.parametrize(
    ("antes", "aceita"),
    [
        ([], False),
        (["UPDATE avisos_bloqueio SET estado = 'sistema_fora' WHERE id = :a"], False),
        ([RESERVAR_A_VEZ, "UPDATE avisos_bloqueio SET estado = 'sistema_fora' WHERE id = :a"], False),
        ([RESERVAR_A_VEZ, "UPDATE avisos_bloqueio SET estado = 'bloqueio_encerrado' WHERE id = :a"], False),
        ([RESERVAR_A_VEZ], True),
        ([RESERVAR_A_VEZ, "UPDATE avisos_bloqueio SET estado = 'respondido' WHERE id = :a"], True),
        ([RESERVAR_A_VEZ, "UPDATE avisos_bloqueio SET estado = 'sem_resposta' WHERE id = :a"], True),
    ],
)
async def test_resposta_ao_aviso_so_depois_de_o_aviso_chamar_o_sistema(
    dono: AsyncEngine, cena: Cena, enviada: Any, antes: list[str], aceita: bool
) -> None:
    """Defeito 5 da conferencia final: uma resposta ao aviso so existe para um aviso que chamou o sistema, com a vez
    reservada (tentativas >= 1) e fora de 'sistema_fora' e 'bloqueio_encerrado', que nunca chamam."""

    async def passos(c: AsyncConnection) -> str:
        aviso = await _avisar(c, await _bloquear(c, enviada), [enviada])
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        for sql in antes:
            await c.execute(text(sql), {"a": aviso})
        await apoio.informar(c, enviada.id, "parada", origem="aviso", aviso_id=aviso)
        return str(
            (await c.execute(text("SELECT origem FROM informacoes_sistema WHERE aviso_id = :a"), {"a": aviso})).scalar()
        )

    if aceita:
        assert await num_rascunho(dono, passos) == "aviso"
    else:
        await falha(dono, passos, "A resposta ao aviso so vem depois de o aviso chamar o sistema")


# Sequencias com a resposta ao aviso (minutos depois do envio, origem, estado): a decisao 1 do coordenador (retoque
# da etapa 3) tira a origem 'aviso' do que e final, na tela e na escada.
SEQUENCIAS_COM_O_AVISO: Final = {
    "so_o_aviso": [(1, "aviso", "parada")],
    "recibo_em_andamento_e_aviso": [(1, "recibo", "em_andamento"), (2, "aviso", "parada")],
    "aviso_e_consulta_nao_recebida": [(1, "aviso", "nao_recebida"), (3, "consulta", "nao_recebida")],
    "aviso_e_consulta_concluida": [(1, "aviso", "parada"), (3, "consulta", "concluida")],
    "recibo_concluida_e_aviso": [(1, "recibo", "concluida"), (2, "aviso", "parada")],
}


@pytest.mark.parametrize("nome", SEQUENCIAS_COM_O_AVISO)
async def test_resposta_ao_aviso_nunca_e_final_na_tela_nem_na_fila(
    dono: AsyncEngine, cena: Cena, enviada: Any, nome: str
) -> None:
    """Decisao 1 do coordenador (D2, opcao a): ``selo_estado_final`` (a tela e o botao de um toque) e a coluna
    ``com_resposta_do_sistema`` de ``medicoes.v_fila_agora`` so contam o recibo e a consulta, como
    ``contrato.resposta_que_encerra``: o ``parada`` do aviso nao resolve o "estamos confirmando", e uma ``nao_recebida``
    do aviso nao confirma a da consulta. A resposta ao aviso continua gravada."""
    sequencia = SEQUENCIAS_COM_O_AVISO[nome]
    contar = "SELECT coalesce(sum(com_resposta_do_sistema), 0) FROM medicoes.v_fila_agora"

    async def passos(c: AsyncConnection) -> tuple[Any, ...]:
        antes = (await c.execute(text(contar))).scalar_one()
        aviso = await _avisar(c, await _bloquear(c, enviada), [enviada])
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(text(RESERVAR_A_VEZ), {"a": aviso})
        ids = []
        for _, origem, estado in sequencia:
            ids.append(
                (
                    await apoio.informar(
                        c, enviada.id, estado, origem=origem, aviso_id=aviso if origem == "aviso" else None
                    )
                ).id
            )
        await c.execute(text("RESET ROLE"))
        for info, (minutos, _, _) in zip(ids, sequencia, strict=True):
            await _informado_em(c, info, timedelta(minutes=minutos - 10))
        linhas = [
            contrato.LinhaInformada(str(x.origem), str(x.desfecho), x.estado, x.gravado_em)
            for x in await c.execute(
                text(
                    "SELECT origem, desfecho, estado, gravado_em FROM informacoes_sistema WHERE operacao_id = :o"
                    " ORDER BY gravado_em, id"
                ),
                {"o": enviada.id},
            )
        ]
        assert [x.origem for x in linhas] == [o for _, o, _ in sequencia]
        final = (await c.execute(text("SELECT selo_estado_final(:o)"), {"o": enviada.id})).scalar_one()
        na_fila = (await c.execute(text(contar))).scalar_one() - antes
        await c.execute(text("SET LOCAL ROLE grafana_leitor"))
        pelo_grafana = (await c.execute(text(contar))).scalar_one() - antes
        return final, contrato.resposta_que_encerra(linhas), na_fila, pelo_grafana

    final, pela_escada, na_fila, pelo_grafana = await num_rascunho(dono, passos)
    esperado = {"aviso_e_consulta_concluida": "concluida", "recibo_concluida_e_aviso": "concluida"}.get(nome)
    assert (final, pela_escada) == (esperado, esperado)
    assert (na_fila, pelo_grafana) == ((0, 0) if esperado is None else (1, 1))


async def test_cada_numero_vai_uma_vez_so_nos_avisos_do_bloqueio(dono: AsyncEngine, cena: Cena, enviada: Any) -> None:
    async def passos(c: AsyncConnection) -> None:
        bloqueio = await _bloquear(c, enviada)
        await _avisar(c, bloqueio, [enviada])
        await _avisar(c, bloqueio, [enviada], parte=2)

    await falha(dono, passos, "Cada numero vai uma vez so")


async def test_guarda_do_aviso_de_bloqueio(dono: AsyncEngine, cena: Cena, enviada: Any) -> None:
    async def montar(c: AsyncConnection) -> uuid.UUID:
        return await _avisar(c, await _bloquear(c, enviada), [enviada])

    aviso = await no_dono(dono, montar)
    mudar = "UPDATE avisos_bloqueio SET estado = :e, tentativas = :t WHERE id = :a"
    await recusado(dono, mudar, "so sobem, uma por vez", e="pendente", t=2, a=aviso, papel=DESPACHANTE)
    await recusado(dono, mudar, "depois de uma tentativa", e="sem_resposta", t=0, a=aviso, papel=DESPACHANTE)
    # Critica M8: so se chama o sistema depois de reservar a vez; respondido sem nenhuma tentativa e uma copia que
    # chamou sem reservar.
    await recusado(dono, mudar, "depois de uma tentativa", e="respondido", t=0, a=aviso, papel=DESPACHANTE)
    await recusado(dono, mudar, "avisos_bloqueio_estado_check", e="esquecido", t=1, a=aviso, papel=DESPACHANTE)
    await recusado(dono, "UPDATE avisos_bloqueio SET parte = 2 WHERE id = :a", "so o estado, as tentativas", a=aviso)
    await recusado(
        dono, "UPDATE avisos_bloqueio SET operacoes = '{}' WHERE id = :a", SEM_PERMISSAO, a=aviso, papel=DESPACHANTE
    )
    await recusado(dono, mudar, SEM_PERMISSAO, e="pendente", t=1, a=aviso, papel=ENTRADA)
    await fazer(dono, mudar + " AND proxima_em <= now()", e="pendente", t=1, a=aviso, papel=DESPACHANTE)
    await recusado(dono, mudar, "so sobem, uma por vez", e="pendente", t=0, a=aviso)
    await fazer(dono, mudar, e="sem_resposta", t=2, a=aviso, papel=DESPACHANTE)
    await recusado(dono, mudar, "ja terminou", e="sem_resposta", t=2, a=aviso, papel=DESPACHANTE)


async def test_derivados_contam_os_avisos_do_bloqueio(dono: AsyncEngine, cena: Cena, enviada: Any) -> None:
    async def passos(c: AsyncConnection, avisos: int) -> None:
        bloqueio = await _bloquear(c, enviada)
        await _avisar(c, bloqueio, [enviada])
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(
            text("INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) VALUES (:b, :n)"),
            {"b": bloqueio, "n": avisos},
        )
        await c.execute(
            text("INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) VALUES (:b, :n)"),
            {"b": bloqueio, "n": avisos},
        )

    await falha(dono, lambda c: passos(c, 0), "a dos avisos gravados do bloqueio")
    await falha(dono, lambda c: passos(c, 1), "avisos_bloqueio_derivados_pkey")

    async def sem_comprovante(c: AsyncConnection) -> None:
        bloqueio = (
            await c.execute(
                text(
                    "INSERT INTO bloqueios (tipo, alcance, quem, motivo) VALUES ('bloqueio', 'tudo', 'a', 'b')"
                    " RETURNING id"
                )
            )
        ).scalar_one()
        await c.execute(text("SET LOCAL ROLE mcp_claro_despachante"))
        await c.execute(
            text("INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) VALUES (:b, 0)"), {"b": bloqueio}
        )

    await falha(dono, sem_comprovante, "Os avisos de bloqueio nascem do comprovante do bloqueio")


# --- FICHAS ---------------------------------------------------------------------------------------------------


async def test_ficha_so_mostra_o_selo_vigente_e_ele_nao_muda(dono: AsyncEngine, cena: Cena) -> None:
    """Critica M3: 2 ou 3 so com o retrato do selo vigente de agora (nivel, teste, datas ate o microssegundo, modo e
    motivo); depois, nao muda."""
    sem_teste = {"nivel": 2, "teste_id": None, "testado_em": None, "vale_ate": None, "modo": None, "motivo": "vigente"}

    async def criar(c: AsyncConnection, selo: dict[str, Any]) -> uuid.UUID:
        await c.execute(text("SET LOCAL ROLE mcp_claro_entrada"))
        return (await apoio.criar_operacao(c, cena.sistema.acoes[0], liberada=False, ficha_selo=selo)).ficha_id

    vigente_agora = "so mostra selo de nivel 2 ou 3 com o selo vigente"
    await falha(dono, lambda c: criar(c, sem_teste), vigente_agora)
    teste = await com_teste(dono, cena)
    selo = apoio.retrato(await vigente(dono, cena.sistema.id))
    um_microssegundo = timedelta(microseconds=1)
    for errado, erro in (
        (selo | {"nivel": 3}, vigente_agora),
        (selo | {"nivel": "2"}, "traz o nivel 1, 2 ou 3"),
        (selo | {"teste_id": str(uuid.uuid4())}, vigente_agora),
        (selo | {"vale_ate": "2099-01-01T00:00:00+00:00"}, vigente_agora),
        (selo | {"vale_ate": "depois"}, vigente_agora),
        (selo | {"testado_em": (teste.testado_em + um_microssegundo).isoformat()}, vigente_agora),
        (selo | {"modo": "ferramentas_de_teste"}, vigente_agora),
        (selo | {"modo": None}, vigente_agora),
        (selo | {"motivo": "fase_desligada"}, vigente_agora),
        (selo | {"motivo": "chave_desligada"}, "do vocabulario do selo"),
        ({"nivel": 4}, "tem so nivel, teste_id"),
    ):
        await falha(dono, lambda c, e=errado: criar(c, e), erro)
    ficha = await no_dono(dono, lambda c: criar(c, selo))
    guardado = await fazer(dono, "SELECT selo FROM fichas WHERE id = :f", f=ficha)
    assert guardado == selo
    um = await no_dono(dono, lambda c: criar(c, selo | {"nivel": 1, "motivo": "fase_desligada"}))
    await recusado(dono, "UPDATE fichas SET selo = NULL WHERE id = :f", "O selo de uma ficha nao muda", f=um)
    await recusado(dono, "UPDATE fichas SET selo = selo WHERE id = :f", SEM_PERMISSAO, f=ficha, papel=ENTRADA)
    # O resto da guarda segue valendo.
    await recusado(
        dono, "UPDATE fichas SET sinais = '{}'::jsonb WHERE id = :f", "os sinais de uma ficha nao mudam", f=ficha
    )


async def test_ficha_confere_a_forma_do_retrato_tambem_no_nivel_1(dono: AsyncEngine, cena: Cena) -> None:
    """Critica M3: exatamente os campos do retrato (nada de texto a mais), motivo do vocabulario, e o nivel 1 nunca
    diz 'vigente' nem traz um teste que nao e do sistema, ou datas e modo que nao sao do teste."""
    teste = await com_teste(dono, cena)
    selo = apoio.retrato(await vigente(dono, cena.sistema.id))
    nada = {"nivel": 1, "teste_id": None, "testado_em": None, "vale_ate": None, "modo": None, "motivo": "nunca_testado"}

    async def criar(c: AsyncConnection, retrato: dict[str, Any]) -> Any:
        await c.execute(text("SET LOCAL ROLE mcp_claro_entrada"))
        f = (await apoio.criar_operacao(c, cena.sistema.acoes[0], liberada=False, ficha_selo=retrato)).ficha_id
        return (await c.execute(text("SELECT selo FROM fichas WHERE id = :f"), {"f": f})).scalar_one()

    um = selo | {"nivel": 1, "motivo": "fase_desligada"}
    for bom in (um, nada, selo | {"nivel": 1, "motivo": "retirado"}, nada | {"motivo": "fase_desligada"}):
        assert await num_rascunho(dono, lambda c, b=bom: criar(c, b)) == bom

    async def de_outro_sistema(c: AsyncConnection) -> Any:
        outro = await apoio.proprio_com_acoes(c)
        await apoio.gravar_teste(c, outro.id, admin_id=cena.admin_id)
        alheio = apoio.retrato(await apoio.selo_vigente(c, outro.id))
        return await criar(c, alheio | {"nivel": 1, "motivo": "fase_desligada"})

    campos = "tem so nivel, teste_id, testado_em, vale_ate, modo e motivo"
    nivel_1 = "O retrato de nivel 1 na ficha traz um teste do proprio sistema"
    for errado, erro in (
        (um | {"extra": "<b>x</b>"}, campos),
        ({k: v for k, v in um.items() if k != "modo"}, campos),
        (um | {"motivo": "vigente"}, "nunca diz vigente"),
        (nada | {"motivo": "<b>x</b>"}, "do vocabulario do selo"),
        (nada | {"motivo": None}, "do vocabulario do selo"),
        (um | {"teste_id": str(uuid.uuid4())}, nivel_1),
        (um | {"teste_id": 5}, nivel_1),
        (um | {"teste_id": "nao e uuid"}, nivel_1),
        (um | {"testado_em": (teste.testado_em + timedelta(seconds=1)).isoformat()}, nivel_1),
        (um | {"vale_ate": None}, nivel_1),
        (um | {"modo": "qualquer"}, nivel_1),
        (nada | {"testado_em": selo["testado_em"]}, nivel_1),
        (nada | {"modo": "ambiente_de_teste"}, nivel_1),
    ):
        await falha(dono, lambda c, e=errado: criar(c, e), erro)
    await falha(dono, de_outro_sistema, nivel_1)


async def test_ficha_de_nivel_1_tem_o_motivo_que_combina_com_o_teste(dono: AsyncEngine, cena: Cena) -> None:
    """Defeito 4 da conferencia final: no nivel 1, sem teste so 'nunca_testado' ou 'fase_desligada'; com teste, nunca
    'nunca_testado', e 'reprovado' so (e sempre, fora 'fase_desligada') com um teste reprovado, como selo_vigente
    diria."""
    await com_teste(dono, cena)
    aprovado = apoio.retrato(await vigente(dono, cena.sistema.id)) | {"nivel": 1}
    nada = {"nivel": 1, "teste_id": None, "testado_em": None, "vale_ate": None, "modo": None}

    async def criar(c: AsyncConnection, retrato: dict[str, Any], *, reprovar: bool = False) -> Any:
        if reprovar:
            await apoio.gravar_teste(c, cena.sistema.id, aprovado=False, admin_id=cena.admin_id)
            retrato = apoio.retrato(await apoio.selo_vigente(c, cena.sistema.id)) | retrato
        await c.execute(text("SET LOCAL ROLE mcp_claro_entrada"))
        f = (await apoio.criar_operacao(c, cena.sistema.acoes[0], liberada=False, ficha_selo=retrato)).ficha_id
        return (await c.execute(text("SELECT selo ->> 'motivo' FROM fichas WHERE id = :f"), {"f": f})).scalar_one()

    combina = "O motivo do retrato de nivel 1 na ficha nao combina com o teste dele"
    for motivo in ("nunca_testado", "fase_desligada"):
        assert await num_rascunho(dono, lambda c, m=motivo: criar(c, nada | {"motivo": m})) == motivo
    for motivo in ("reprovado", "vencido", "retirado", "fora_de_ativo", "contrato_sumiu"):
        await falha(dono, lambda c, m=motivo: criar(c, nada | {"motivo": m}), combina)
    for motivo in ("vencido", "retirado", "fora_de_ativo", "endereco_mudou", "ferramenta_nova", "fase_desligada"):
        assert await num_rascunho(dono, lambda c, m=motivo: criar(c, aprovado | {"motivo": m})) == motivo
    for motivo in ("nunca_testado", "reprovado"):
        await falha(dono, lambda c, m=motivo: criar(c, aprovado | {"motivo": m}), combina)
    # Com o ultimo teste reprovado: 'reprovado' ou 'fase_desligada'; nunca um motivo de teste aprovado.
    for motivo in ("reprovado", "fase_desligada"):
        assert await num_rascunho(dono, lambda c, m=motivo: criar(c, {"motivo": m}, reprovar=True)) == motivo
    for motivo in ("vencido", "retirado", "nunca_testado"):
        await falha(dono, lambda c, m=motivo: criar(c, {"motivo": m}, reprovar=True), combina)


# --- MEDICOES e GRANTS ----------------------------------------------------------------------------------------


async def test_grafana_le_as_visoes_novas_e_nao_as_tabelas(dono: AsyncEngine, cena: Cena) -> None:
    """Critica M10: v_selos funciona para o grafana_leitor (pela funcao de contagem que roda como o dono), sem que ele
    leia as tabelas nem chame selo_vigente."""
    await com_teste(dono, cena)
    contagem = await fazer(
        dono, "SELECT sistemas FROM medicoes.v_selos WHERE nivel = 2 AND motivo = 'vigente'", papel=GRAFANA
    )
    assert contagem >= 1
    assert await fazer(dono, "SELECT count(*) FROM medicoes.v_consultas_sistema", papel=GRAFANA) >= 0
    assert await fazer(dono, "SELECT count(*) FROM medicoes.v_fila_agora", papel=GRAFANA) >= 0
    for tabela in (
        "declaracoes_selo",
        "pedidos_teste_selo",
        "testes_selo",
        "selos_retirados",
        "envios_selo",
        "informacoes_sistema",
        "avisos_bloqueio",
        "avisos_bloqueio_derivados",
        "selos_vigentes",
    ):
        await recusado(dono, f"SELECT 1 FROM {tabela} LIMIT 1", SEM_PERMISSAO, papel=GRAFANA)  # noqa: S608
    await recusado(dono, "SELECT * FROM selo_vigente(:s)", SEM_PERMISSAO, s=cena.sistema.id, papel=GRAFANA)
    for papel in PAPEIS:
        await recusado(dono, "SELECT * FROM medicoes.contagem_selos()", SEM_PERMISSAO, papel=papel)


async def test_papeis_leem_o_selo_vigente_pela_funcao(dono: AsyncEngine, cena: Cena) -> None:
    await com_teste(dono, cena)
    for papel in PAPEIS:
        assert await fazer(dono, "SELECT nivel FROM selo_vigente(:s)", s=cena.sistema.id, papel=papel) == 2
        assert (
            await fazer(dono, "SELECT nivel FROM selos_vigentes WHERE sistema_id = :s", s=cena.sistema.id, papel=papel)
            == 2
        )
    assert (
        await fazer(dono, "SELECT has_function_privilege('public', 'selo_vigente(uuid, timestamptz)', 'EXECUTE')")
        is False
    )


async def test_entrada_e_rotinas_nao_veem_o_numero(dono: AsyncEngine, cena: Cena, enviada: Any) -> None:
    for papel in (ENTRADA, ROTINAS):
        assert await fazer(dono, "SELECT nivel FROM envios_selo WHERE operacao_id = :o", o=enviada.id, papel=papel) == 3
        await recusado(
            dono, "SELECT chave FROM envios_selo WHERE operacao_id = :o", SEM_PERMISSAO, o=enviada.id, papel=papel
        )
        # Quem sucedeu quem, sem ver o numero.
        assert await fazer(dono, "SELECT selo_substituto(:o)", o=enviada.id, papel=papel) is None
    assert await fazer(dono, "SELECT chave FROM envios_selo WHERE operacao_id = :o", o=enviada.id, papel=DESPACHANTE)


E, D, R = ENTRADA, DESPACHANTE, ROTINAS

# Retrato dos privilegios nas tabelas e visoes da 0007 (e nas que ela toca para dar GRANT): um GRANT a mais (ou a
# menos) quebra este teste, e quem o mudar escreve aqui por que o papel precisa dele.
TABELAS_DO_RETRATO_0007: Final = (
    "declaracoes_selo",
    "pedidos_teste_selo",
    "testes_selo",
    "selos_retirados",
    "envios_selo",
    "informacoes_sistema",
    "avisos_bloqueio",
    "avisos_bloqueio_derivados",
    "selos_vigentes",
    "comprovantes",
    "v_selos",
    "v_consultas_sistema",
    "v_fila_agora",
)
RETRATO_DE_TABELA_0007: Final = {
    ("grafana_leitor", "medicoes", "v_consultas_sistema", "SELECT"),
    ("grafana_leitor", "medicoes", "v_fila_agora", "SELECT"),
    ("grafana_leitor", "medicoes", "v_selos", "SELECT"),
    *{(p, "public", t, "SELECT") for p in (E, D) for t in ("declaracoes_selo", "pedidos_teste_selo")},
    *{
        (p, "public", t, "SELECT")
        for p in (E, D, R)
        for t in ("testes_selo", "selos_retirados", "informacoes_sistema", "avisos_bloqueio", "selos_vigentes")
    },
    (D, "public", "envios_selo", "SELECT"),
    (D, "public", "avisos_bloqueio_derivados", "SELECT"),
    *{(E, "public", t, "INSERT") for t in ("declaracoes_selo", "pedidos_teste_selo", "selos_retirados")},
    *{
        (D, "public", t, "INSERT")
        for t in (
            "testes_selo",
            "envios_selo",
            "informacoes_sistema",
            "avisos_bloqueio",
            "avisos_bloqueio_derivados",
            "selos_retirados",
        )
    },
    # comprovantes: a entrada e as rotinas gravam no bloqueio (0005); o despachante so le, para derivar os avisos.
    (E, "public", "comprovantes", "SELECT"),
    (E, "public", "comprovantes", "INSERT"),
    (R, "public", "comprovantes", "SELECT"),
    (R, "public", "comprovantes", "INSERT"),
    (D, "public", "comprovantes", "SELECT"),
}
RETRATO_DE_COLUNA_0007: Final = {
    (D, "pedidos_teste_selo", "estado", "UPDATE"),
    (D, "pedidos_teste_selo", "motivo", "UPDATE"),
    (D, "avisos_bloqueio", "estado", "UPDATE"),
    (D, "avisos_bloqueio", "tentativas", "UPDATE"),
    (D, "avisos_bloqueio", "proxima_em", "UPDATE"),
    *{
        (p, "envios_selo", col, "SELECT")
        for p in (E, R)
        for col in ("operacao_id", "sistema_id", "nivel", "teste_id", "repete_operacao_id", "gravado_em")
    },
}


async def test_retrato_dos_privilegios_nas_tabelas_da_0007(dono: AsyncEngine) -> None:
    papeis = [E, D, R, GRAFANA]
    async with dono.connect() as c:
        tabela = (
            await c.execute(
                text(
                    "SELECT grantee, table_schema, table_name, privilege_type FROM information_schema.role_table_grants"
                    " WHERE grantee = ANY(:p) AND table_name = ANY(:t)"
                ),
                {"p": papeis, "t": list(TABELAS_DO_RETRATO_0007)},
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
                {"p": papeis, "t": list(TABELAS_DO_RETRATO_0007)},
            )
        ).all()
    assert {tuple(x) for x in tabela} == RETRATO_DE_TABELA_0007
    assert {tuple(x) for x in coluna} == RETRATO_DE_COLUNA_0007


# Retrato do EXECUTE nas funcoes da 0007 que leem tabelas (as puras do bloco CONTRATO ficam com o padrao). Um GRANT
# a mais, ou um REVOKE FROM PUBLIC que falte, quebra este teste.
RETRATO_DE_FUNCAO_0007: Final = {
    "selo_vigente(uuid, timestamptz)": {E, D, R},
    "selo_acoes_do_sistema(uuid)": {E, D, R},
    "selo_contrato_listado(uuid, smallint, timestamptz)": {E, D, R},
    "selo_substituto(uuid)": {E, D, R},
    "selo_estado_final(uuid)": {E, D, R},
    # So nos gatilhos da declaracao e do pedido (entrada) e do teste e do 'rodando' (despachante).
    "selo_alvo_na_producao(text)": {E, D},
    "medicoes.contagem_selos()": {GRAFANA},
}


@pytest.mark.parametrize(("funcao", "papeis"), RETRATO_DE_FUNCAO_0007.items())
async def test_retrato_do_execute_nas_funcoes_da_0007(dono: AsyncEngine, funcao: str, papeis: set[str]) -> None:
    """Mutacao que sobreviveu na conferencia final: sem o REVOKE de selo_alvo_na_producao, todo papel (e PUBLIC)
    poderia chamar a funcao."""
    pode = set()
    for papel in ("public", E, D, R, GRAFANA):
        if await fazer(dono, "SELECT has_function_privilege(:p, :f, 'EXECUTE')", p=papel, f=funcao):
            pode.add(papel)
    assert pode == papeis


@pytest.mark.parametrize("tabela", [*SO_ACRESCENTAR, "pedidos_teste_selo", "avisos_bloqueio"])
@pytest.mark.parametrize("papel", PAPEIS)
async def test_ninguem_tem_update_de_tabela_inteira_nem_delete(dono: AsyncEngine, tabela: str, papel: str) -> None:
    for privilegio in ("UPDATE", "DELETE", "TRUNCATE"):
        assert await fazer(dono, "SELECT has_table_privilege(:p, :t, :pr)", p=papel, t=tabela, pr=privilegio) is False


@pytest.mark.parametrize("tabela", ["testes_selo", "envios_selo", "informacoes_sistema", "avisos_bloqueio"])
@pytest.mark.parametrize("papel", [ENTRADA, ROTINAS])
async def test_entrada_e_rotinas_nao_gravam_o_selo(dono: AsyncEngine, tabela: str, papel: str) -> None:
    assert await fazer(dono, "SELECT has_table_privilege(:p, :t, 'INSERT')", p=papel, t=tabela) is False
