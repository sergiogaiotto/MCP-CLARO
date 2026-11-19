"""Continuidade do banco na partida do despachante, contra PostgreSQL 18.7 real.

Cada teste usa um banco proprio, copiado de um modelo ja migrado, porque a conferencia fecha a porta de
saida e pausa aplicativos: os outros testes da sessao nao podem herdar isso. A conferencia roda como o
usuario do despachante (``SET ROLE`` na conexao), entao falta de permissao aparece aqui.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mcp_claro.config.settings import Parametros
from mcp_claro.continuidade import (
    ReaberturaRecusada,
    conferir_continuidade,
    marcar_restaurado,
    reabrir_saida,
    situacao,
)
from mcp_claro.db.engine import url_asyncpg
from mcp_claro.extrato import fechar
from mcp_claro.extrato.frases import frase
from tests.conftest import RAIZ

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MODELO = "continuidade_modelo"


def parametros_do_banco(p: Parametros, banco: str) -> Parametros:
    """Os mesmos parametros do banco de teste, apontando para outro banco do mesmo servidor."""
    url = p.DATABASE_URL.rsplit("/", 1)[0] + f"/{banco}"
    return Parametros(**(p.model_dump() | {"DATABASE_URL": url}))


def dsn(p: Parametros) -> str:
    return url_asyncpg(p).replace("postgresql+asyncpg://", "postgresql://", 1)


async def executar(p: Parametros, *comandos: str) -> None:
    """Comandos fora de transacao (CREATE DATABASE, DROP DATABASE), com o dono do servidor de teste."""
    conexao = await asyncpg.connect(dsn(p))
    try:
        for comando in comandos:
            await conexao.execute(comando)
    finally:
        await conexao.close()


def migrar(p: Parametros) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.attributes["parametros"] = p
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def banco_modelo(banco_migrado: Parametros) -> Iterator[Parametros]:
    """Banco migrado do zero, sem conexoes, que serve de modelo para os bancos de cada teste."""
    asyncio.run(executar(banco_migrado, f"DROP DATABASE IF EXISTS {MODELO} WITH (FORCE)", f"CREATE DATABASE {MODELO}"))
    migrar(parametros_do_banco(banco_migrado, MODELO))
    yield banco_migrado
    asyncio.run(executar(banco_migrado, f"DROP DATABASE IF EXISTS {MODELO} WITH (FORCE)"))


def engine_como(p: Parametros, papel: str | None = None) -> AsyncEngine:
    """Engine do dono do servidor de teste ou, com ``papel``, do usuario daquele papel (``SET ROLE``)."""
    ajustes = {"application_name": f"teste-continuidade-{papel or 'dono'}"}
    if papel:
        ajustes["role"] = papel
    return create_async_engine(url_asyncpg(p), connect_args={"ssl": False, "server_settings": ajustes}, pool_size=2)


@dataclass
class Banco:
    p: Parametros
    dono: AsyncEngine
    despachante: AsyncEngine
    entrada: AsyncEngine

    async def valor(self, sql: str, **params: Any) -> Any:
        async with self.dono.connect() as c:
            return (await c.execute(text(sql), params)).scalar_one_or_none()

    async def linhas(self, sql: str, **params: Any) -> list[Any]:
        async with self.dono.connect() as c:
            return list((await c.execute(text(sql), params)).all())

    async def fazer(self, sql: str, **params: Any) -> Any:
        async with self.dono.begin() as c:
            resultado = await c.execute(text(sql), params)
            return resultado.scalar_one_or_none() if resultado.returns_rows else None

    async def aplicativo(self, client_id: str, estado: str = "ativo", tipo: str = "assistente") -> uuid.UUID:
        return uuid.UUID(
            str(
                await self.fazer(
                    "INSERT INTO aplicativos (client_id, nome, familia, tipo, situacao) "
                    "VALUES (:c, :c, 'teste', :t, :s) RETURNING id",
                    c=client_id,
                    t=tipo,
                    s=estado,
                )
            )
        )

    async def pessoa(self, papel: str = "pessoa", situacao: str = "ativa") -> uuid.UUID:
        return uuid.UUID(
            str(
                await self.fazer(
                    "INSERT INTO pessoas (emissor, sujeito, papel, situacao) "
                    "VALUES ('https://login.teste', :s, :p, :si) RETURNING id",
                    s=f"sujeito-{uuid.uuid4().hex}",
                    p=papel,
                    si=situacao,
                )
            )
        )

    async def tipos_no_extrato(self) -> list[str]:
        return [str(linha.tipo) for linha in await self.linhas("SELECT tipo FROM extrato ORDER BY id")]


@pytest.fixture
async def banco(banco_modelo: Parametros) -> AsyncIterator[Banco]:
    nome = f"continuidade_{uuid.uuid4().hex[:12]}"
    await executar(banco_modelo, f"CREATE DATABASE {nome} TEMPLATE {MODELO}")
    p = parametros_do_banco(banco_modelo, nome)
    b = Banco(p, engine_como(p), engine_como(p, "mcp_claro_despachante"), engine_como(p, "mcp_claro_entrada"))
    try:
        yield b
    finally:
        for engine in (b.dono, b.despachante, b.entrada):
            await engine.dispose()
        await executar(banco_modelo, f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)")


async def test_controle_devolve_identificador_e_linha_do_tempo_do_postgres(banco: Banco) -> None:
    esperado = await banco.linhas(
        "SELECT s.system_identifier::text AS si, c.timeline_id AS tl "
        "FROM pg_control_system() s, pg_control_checkpoint() c"
    )
    async with banco.despachante.connect() as c:
        visto = (await c.execute(text("SELECT system_identifier, timeline_id FROM continuidade_controle()"))).one()
    assert (visto.system_identifier, visto.timeline_id) == (esperado[0].si, esperado[0].tl)


async def test_primeira_partida_so_grava_a_linha(banco: Banco) -> None:
    app = await banco.aplicativo("app-primeira")
    r = await conferir_continuidade(banco.despachante, 0)
    assert r.desfecho == "primeira_partida"
    assert r.confere
    assert not r.saida_fechada
    assert r.anterior is None
    linha = (await banco.linhas("SELECT * FROM continuidade_banco"))[0]
    assert (linha.system_identifier, linha.timeline_id) == (r.atual.system_identifier, r.atual.timeline_id)
    assert (linha.origem, linha.desfecho, linha.restaurado) == ("despachante", "primeira_partida", False)
    assert await banco.valor("SELECT aberta FROM estado_saida") is True
    assert await banco.valor("SELECT situacao FROM aplicativos WHERE id = :a", a=app) == "ativo"
    assert await banco.tipos_no_extrato() == ["continuidade_primeira"]


async def test_partida_normal_confere_e_nao_mexe_em_nada(banco: Banco) -> None:
    app = await banco.aplicativo("app-normal")
    await conferir_continuidade(banco.despachante, 0)
    r = await conferir_continuidade(banco.despachante, 0)
    assert r.desfecho == "confere"
    assert r.motivos == ()
    assert r.anterior == r.atual
    assert await banco.valor("SELECT count(*) FROM continuidade_banco") == 2
    assert await banco.valor("SELECT aberta FROM estado_saida") is True
    assert await banco.valor("SELECT situacao FROM aplicativos WHERE id = :a", a=app) == "ativo"
    assert await banco.tipos_no_extrato() == ["continuidade_primeira"]


async def test_identificador_diferente_fecha_saida_e_pausa_aplicativos(banco: Banco) -> None:
    ativo = await banco.aplicativo("app-ativo")
    bloqueado = await banco.aplicativo("app-bloqueado", "bloqueado")
    ronda = await banco.aplicativo("mcp-claro-ronda", tipo="programa")
    await banco.fazer("SELECT extrato_registrar('teste', 'partida', 'linha antiga')")
    # Linha anterior com outro identificador: o banco "veio" de outra instalacao (copia logica).
    timeline = await banco.valor("SELECT timeline_id FROM continuidade_controle()")
    await banco.fazer(
        "INSERT INTO continuidade_banco (system_identifier, timeline_id, desfecho) VALUES ('1', :t, 'confere')",
        t=timeline,
    )

    r = await conferir_continuidade(banco.despachante, 0)

    assert r.desfecho == "nao_confere"
    assert r.saida_fechada
    assert r.motivos == ("identificador_mudou",)
    assert r.aplicativos_pausados == (ativo,)
    saida = (await banco.linhas("SELECT aberta, mudou_por, motivo FROM estado_saida"))[0]
    assert (saida.aberta, saida.mudou_por) == (False, "rotina:continuidade")
    assert "identificador" in saida.motivo
    situacoes = dict(await banco.linhas("SELECT id, situacao FROM aplicativos"))
    assert situacoes == {ativo: "pausado", bloqueado: "bloqueado", ronda: "ativo"}
    assert await banco.tipos_no_extrato() == [
        "partida",
        "estado_saida_mudou",
        "restauracao_detectada",
        "intervalo_perdido",
        "aplicativo_pausado_continuidade",
        # Fase 2: a fila de saida para na mesma transacao (aqui, vazia).
        "fila_parada_restauracao",
    ]
    intervalo = (
        await banco.linhas("SELECT frase, dados, aplicativo_id FROM extrato WHERE tipo = 'intervalo_perdido'")
    )[0]
    assert intervalo.frase.startswith("Pode faltar no extrato o que aconteceu entre ")
    assert intervalo.dados["desde"] < intervalo.dados["ate"]
    pausa = (await banco.linhas("SELECT aplicativo_id FROM extrato WHERE tipo = 'aplicativo_pausado_continuidade'"))[0]
    assert pausa.aplicativo_id == ativo
    ultima = (await banco.linhas("SELECT * FROM continuidade_banco ORDER BY id DESC LIMIT 1"))[0]
    assert (ultima.desfecho, ultima.motivo, ultima.system_identifier) == (
        "nao_confere",
        "identificador_mudou",
        r.atual.system_identifier,
    )

    # A partida seguinte confere, mas a saida continua fechada: reabrir e decisao humana.
    seguinte = await conferir_continuidade(banco.despachante, 0)
    assert seguinte.desfecho == "confere"
    assert await banco.valor("SELECT aberta FROM estado_saida") is False

    admin = await banco.pessoa("admin")
    assert await reabrir_saida(banco.entrada, f"admin:{admin}", "Conferido com o vigia e com o backup")
    saida = (await banco.linhas("SELECT aberta, mudou_por, motivo FROM estado_saida"))[0]
    assert (saida.aberta, saida.mudou_por, saida.motivo) == (
        True,
        f"admin:{admin}",
        "Conferido com o vigia e com o backup",
    )
    assert await banco.valor("SELECT situacao FROM aplicativos WHERE id = :a", a=ativo) == "pausado"
    assert (await banco.tipos_no_extrato())[-1] == "estado_saida_mudou"
    assert not await reabrir_saida(banco.entrada, f"admin:{admin}", "de novo")


async def test_linha_do_tempo_diferente_nao_confere(banco: Banco) -> None:
    atual = (await banco.linhas("SELECT * FROM continuidade_controle()"))[0]
    await banco.fazer(
        "INSERT INTO continuidade_banco (system_identifier, timeline_id) VALUES (:s, :t)",
        s=atual.system_identifier,
        t=atual.timeline_id + 1,
    )
    r = await conferir_continuidade(banco.despachante, 0)
    assert r.motivos == ("linha_do_tempo_mudou",)
    assert await banco.valor("SELECT aberta FROM estado_saida") is False


async def test_marca_restaurado_fecha_na_partida_seguinte(banco: Banco) -> None:
    app = await banco.aplicativo("app-restaurado")
    await conferir_continuidade(banco.despachante, 0)
    linha = await marcar_restaurado(banco.despachante, "Pessoa da operacao", "Ensaio de restauracao")
    marca = (await banco.linhas("SELECT * FROM continuidade_banco WHERE id = :i", i=linha))[0]
    assert (marca.restaurado, marca.origem, marca.desfecho, marca.quem, marca.motivo) == (
        True,
        "roteiro",
        "marca_restaurado",
        "Pessoa da operacao",
        "Ensaio de restauracao",
    )
    no_extrato = (await banco.linhas("SELECT papel, frase, dados FROM extrato WHERE tipo = 'restauracao_marcada'"))[0]
    assert no_extrato.frase == frase("restauracao_marcada")
    assert no_extrato.papel == "roteiro"
    assert "Pessoa da operacao" not in str(no_extrato.dados)

    r = await conferir_continuidade(banco.despachante, 0)
    assert r.desfecho == "nao_confere"
    assert r.motivos == ("marca_restaurado",)
    assert await banco.valor("SELECT aberta FROM estado_saida") is False
    assert await banco.valor("SELECT situacao FROM aplicativos WHERE id = :a", a=app) == "pausado"
    # O intervalo perdido comeca no ultimo registro de antes da marca, nao na linha do proprio roteiro.
    intervalo = await banco.valor("SELECT dados FROM extrato WHERE tipo = 'intervalo_perdido'")
    primeira = await banco.valor("SELECT criado_em FROM extrato WHERE tipo = 'continuidade_primeira'")
    assert intervalo["desde"] == primeira.isoformat()
    with pytest.raises(DBAPIError, match="Diga quem marcou"):
        await marcar_restaurado(banco.despachante, " ", "sem nome")


async def test_vigia_mais_novo_que_o_banco_nao_confere(banco: Banco) -> None:
    async with banco.dono.begin() as c:
        await c.execute(text("SELECT extrato_registrar('teste', 'partida', 'antes do fechamento')"))
    async with banco.dono.begin() as c:
        primeiro = await fechar(c)
    assert (await conferir_continuidade(banco.despachante, primeiro.numero)).desfecho == "primeira_partida"
    assert (await conferir_continuidade(banco.despachante, primeiro.numero)).desfecho == "confere"

    r = await conferir_continuidade(banco.despachante, primeiro.numero + 2)
    assert r.motivos == ("vigia_mais_novo",)
    assert (r.ultimo_fechamento, r.ultimo_fechamento_vigia) == (primeiro.numero, primeiro.numero + 2)
    dados = await banco.valor("SELECT dados FROM extrato WHERE tipo = 'intervalo_perdido'")
    assert dados["fechamentos_perdidos"] == 2


async def test_vigia_sem_resposta_fecha_a_saida(banco: Banco) -> None:
    await conferir_continuidade(banco.despachante, 0)
    r = await conferir_continuidade(banco.despachante, None)
    assert r.motivos == ("vigia_sem_resposta",)
    assert await banco.valor("SELECT aberta FROM estado_saida") is False
    texto = await banco.valor("SELECT frase FROM extrato WHERE tipo = 'continuidade_nao_confere'")
    assert texto == frase("continuidade_nao_confere", motivo="o vigia nao respondeu")
    assert await banco.valor("SELECT count(*) FROM extrato WHERE tipo = 'restauracao_detectada'") == 0


async def test_sem_vigia_exigido_o_silencio_do_vigia_nao_fecha(banco: Banco) -> None:
    await conferir_continuidade(banco.despachante, None, exigir_vigia=False)
    r = await conferir_continuidade(banco.despachante, None, exigir_vigia=False)
    assert r.desfecho == "confere"
    assert await banco.valor("SELECT aberta FROM estado_saida") is True


async def test_so_o_admin_ativo_reabre_a_saida(banco: Banco) -> None:
    await banco.fazer("UPDATE estado_saida SET aberta = false, motivo = 'teste'")
    comum = await banco.pessoa()
    admin_inativo = await banco.pessoa("admin", "desativada")
    for quem in (f"pessoa:{comum}", "rotina:continuidade", f"admin:{comum}", f"admin:{admin_inativo}"):
        with pytest.raises(ReaberturaRecusada):
            await reabrir_saida(banco.entrada, quem, "quero reabrir")
    admin = await banco.pessoa("admin")
    with pytest.raises(ReaberturaRecusada, match="motivo"):
        await reabrir_saida(banco.entrada, f"admin:{admin}", "   ")
    assert await banco.valor("SELECT aberta FROM estado_saida") is False
    # O despachante tambem pode reabrir pela linha de comando do roteiro.
    assert await reabrir_saida(banco.despachante, f"admin:{admin}", "Decisao registrada", papel="despachante")


async def test_continuidade_e_so_de_acrescentar(banco: Banco) -> None:
    await conferir_continuidade(banco.despachante, 0)
    for comando in ("UPDATE continuidade_banco SET timeline_id = 9", "DELETE FROM continuidade_banco"):
        with pytest.raises(DBAPIError, match="so de acrescentar"):
            await banco.fazer(comando)
    with pytest.raises(DBAPIError, match="so de acrescentar"):
        await banco.fazer("TRUNCATE continuidade_banco")


async def test_permissoes_dos_papeis(banco: Banco) -> None:
    privilegios = (
        await banco.linhas(
            "SELECT has_function_privilege('mcp_claro_despachante', 'continuidade_controle()', 'EXECUTE') AS d_ctl,"
            " has_function_privilege('mcp_claro_entrada', 'continuidade_controle()', 'EXECUTE') AS e_ctl,"
            " has_function_privilege('grafana_leitor', 'continuidade_controle()', 'EXECUTE') AS g_ctl,"
            " has_function_privilege('mcp_claro_entrada', 'continuidade_marcar_restaurado(text,text)', 'EXECUTE')"
            "   AS e_marca,"
            " has_column_privilege('mcp_claro_despachante', 'aplicativos', 'situacao', 'UPDATE') AS d_situacao,"
            " has_column_privilege('mcp_claro_despachante', 'aplicativos', 'nome', 'UPDATE') AS d_nome,"
            " has_table_privilege('mcp_claro_despachante', 'continuidade_banco', 'UPDATE') AS d_upd,"
            " has_table_privilege('mcp_claro_entrada', 'continuidade_banco', 'SELECT') AS e_sel"
        )
    )[0]
    assert dict(privilegios._mapping) == {
        "d_ctl": True,
        "e_ctl": False,
        "g_ctl": False,
        "e_marca": False,
        "d_situacao": True,
        "d_nome": False,
        "d_upd": False,
        "e_sel": True,
    }


async def test_situacao_para_o_roteiro(banco: Banco) -> None:
    await conferir_continuidade(banco.despachante, 0)
    visto = await situacao(banco.despachante)
    assert visto["saida_aberta"] is True
    assert visto["ultima_linha"]["desfecho"] == "primeira_partida"
    assert visto["timeline_id"] >= 1
