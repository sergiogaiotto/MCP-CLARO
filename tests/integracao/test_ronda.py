"""Ronda das portas de ponta a ponta: PostgreSQL 18 real, provedor falso e entrada real.

A ronda roda com o usuario ``mcp_claro_rotinas`` de verdade e entra pelo endereco da entrada, como
um aplicativo de fora. Prova que toda tentativa indevida e recusada e que uma falha plantada (um
verificador de cracha alterado que aceita qualquer cracha, ou uma vitrine que acha a ferramenta de
qualquer pessoa) fecha a entrada por etapas. Na Fase 2, a ronda e um programa com cadastro: sem cadastro
valendo, a rodada e inconclusiva, e a rotina dos programas nunca pausa nem encerra a ronda.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.ronda import Caso, ConfigRonda, Situacao, rodar_ronda
from mcp_claro.rotinas.programas import EVENTO_DAS_PORTAS, cuidar_dos_programas
from tests.apoio import cabecalhos_2026, pedido_2026
from tests.integracao.test_ronda_apoio import (
    ADMIN,
    RONDA,
    Entrada,
    FonteFalsa,
    VerificadorQueAceitaTudo,
    subir_entrada,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def entrada(banco_migrado: Parametros) -> AsyncIterator[Entrada]:
    async with subir_entrada(banco_migrado) as e:
        yield e


@pytest.fixture
async def entrada_com_falha(banco_migrado: Parametros) -> AsyncIterator[Entrada]:
    async with subir_entrada(banco_migrado, planta=VerificadorQueAceitaTudo) as e:
        yield e


def config(e: Entrada, **kw: Any) -> ConfigRonda:
    return ConfigRonda(alvo=e.url, falhas_para_fechar=2, prazo_tentativa=5.0, **kw)


async def linhas(e: Entrada, rodada: uuid.UUID) -> dict[str, Any]:
    async with e.dono.connect() as c:
        resultado = await c.execute(text("SELECT * FROM ronda WHERE rodada = :r ORDER BY id"), {"r": rodada})
        return {linha.caso: linha for linha in resultado}


async def nivel(e: Entrada) -> str:
    async with e.dono.connect() as c:
        return str((await c.execute(text("SELECT nivel FROM estado_entrada WHERE id = 1"))).scalar_one())


async def extrato(e: Entrada, tipo: str, rodada: uuid.UUID | None = None) -> list[Any]:
    async with e.dono.connect() as c:
        return list(
            await c.execute(
                text(
                    "SELECT frase, dados FROM extrato WHERE tipo = :t"
                    " AND (CAST(:r AS text) IS NULL OR dados ->> 'rodada' = :r) ORDER BY id"
                ),
                {"t": tipo, "r": str(rodada) if rodada else None},
            )
        )


async def avisos(e: Entrada, rodada: uuid.UUID) -> list[Any]:
    async with e.dono.connect() as c:
        return list(
            await c.execute(
                text("SELECT * FROM avisos WHERE chave_unica LIKE :k ORDER BY id"), {"k": f"ronda:{rodada}%"}
            )
        )


async def test_ronda_toda_tentativa_recusada(entrada: Entrada) -> None:
    fonte = FonteFalsa(entrada.idp)
    r = await rodar_ronda(entrada.p, entrada.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=config(entrada))
    por_caso = {t.caso: t for t in r.tentativas}
    assert set(por_caso) == {str(c) for c in Caso}
    assert all(t.situacao is Situacao.CONFORME for t in r.tentativas), [
        (str(t.caso), t.obtido, str(t.situacao)) for t in r.tentativas
    ]
    assert por_caso[Caso.CRACHA_FALSO].obtido == "401"
    assert por_caso[Caso.CRACHA_VENCIDO].obtido == "401"
    assert por_caso[Caso.OUTRO_CONDOMINIO].obtido == "401"
    assert por_caso[Caso.OUTRA_PESSOA].detalhes["motivo"] == "fora_da_vitrine"
    # A ronda escolhe uma ferramenta que esta na vitrine de outra pessoa.
    assert por_caso[Caso.OUTRA_PESSOA].detalhes["ferramenta"].startswith("fatura_ana_")
    assert por_caso[Caso.VERSAO_ANTIGA_INITIALIZE].obtido == "400 erro -32022"
    assert por_caso[Caso.VERSAO_ANTIGA_CABECALHO].obtido == "400 erro -32022"
    assert r.resumo.controle_ok
    assert not r.resumo.falhou
    assert r.nivel_antes == r.nivel_depois == "aberta"
    guardadas = await linhas(entrada, r.id)
    assert set(guardadas) == set(por_caso)
    assert all(linha.defendeu for linha in guardadas.values())
    assert guardadas["controle"].detalhes["situacao"] == "conforme"
    assert guardadas["controle"].esperado == "vitrine da ronda (resultado do tools/list)"
    assert guardadas["cracha_falso"].esperado == "401"
    assert await extrato(entrada, "ronda_falha", r.id) == []
    # Nada de cracha na tabela.
    assert not any("eyJ" in str(dict(linha._mapping)) for linha in guardadas.values())


async def test_falha_plantada_fecha_a_entrada_por_etapas(entrada_com_falha: Entrada) -> None:
    e = entrada_com_falha
    fonte = FonteFalsa(e.idp)
    cfg = config(e)
    r1 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    passaram = {t.caso for t in r1.resumo.passagens}
    assert passaram == {Caso.CRACHA_FALSO, Caso.CRACHA_VENCIDO, Caso.OUTRO_CONDOMINIO}
    assert r1.resumo.controle_ok
    assert (r1.seguidas, r1.nivel_depois) == (1, "aberta")
    guardadas = await linhas(e, r1.id)
    assert not guardadas["cracha_falso"].defendeu
    assert guardadas["cracha_falso"].detalhes["situacao"] == "passou"
    (falha,) = await extrato(e, "ronda_falha", r1.id)
    assert "cracha falso" in falha.frase
    assert "cracha vencido" in falha.frase

    # Segunda rodada seguida: a entrada fica so de leitura, com frase no extrato e aviso ao ADMIN.
    r2 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r2.seguidas, r2.nivel_antes, r2.nivel_depois) == (2, "aberta", "so_leitura")
    assert await nivel(e) == "so_leitura"
    (fechou,) = await extrato(e, "ronda_fechou_entrada", r2.id)
    assert "2 rondas seguidas" in fechou.frase
    assert "so de leitura" in fechou.frase
    mudou = await extrato(e, "estado_entrada_mudou")
    assert mudou[-1].frase == "A rotina ronda deixou a entrada so de leitura."
    (aviso,) = await avisos(e, r2.id)
    assert (aviso.canal, aviso.destino, aviso.evento) == ("email", ADMIN, "ronda_fechou_entrada")
    assert aviso.corpo["nivel"] == "so_leitura"
    async with e.dono.connect() as c:
        motivo = (await c.execute(text("SELECT mudou_por, motivo FROM estado_entrada"))).one()
    assert motivo.mudou_por == "rotina:ronda"
    assert "cracha falso" in motivo.motivo

    # A contagem recomeca depois da mudanca: mais duas rodadas para o proximo degrau.
    r3 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r3.seguidas, r3.nivel_depois) == (1, "so_leitura")
    r4 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r4.seguidas, r4.nivel_antes, r4.nivel_depois) == (2, "so_leitura", "fechada")
    assert await nivel(e) == "fechada"
    assert len(await avisos(e, r4.id)) == 1

    # Fechada, a entrada recusa ate a lista de ferramentas; a ronda nao desce mais nada.
    async with httpx2.AsyncClient() as http:
        resposta = await http.post(
            f"{e.url}/mcp",
            json=pedido_2026("tools/list"),
            headers=cabecalhos_2026("tools/list", chave=e.cracha(sujeito="pessoa-ana")),
        )
    assert resposta.json()["error"]["data"]["br.com.claro.mcp-claro/motivo"] == "entrada_fechada"
    r5 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert r5.nivel_depois == "fechada"
    # Fechada, a entrada recusa tambem o controle: a vitrine nao foi testada (inconclusivo).
    assert {t.caso: t for t in r5.tentativas}[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert await avisos(e, r5.id) == []

    # O ADMIN reabre depois de apurar: a contagem zera.
    async with e.dono.begin() as c:
        await c.execute(text("UPDATE estado_entrada SET nivel = 'aberta', mudou_em = clock_timestamp()"))
    r6 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r6.seguidas, r6.nivel_depois) == (1, "aberta")


async def test_fechamento_sozinho_desligado_so_avisa(entrada_com_falha: Entrada) -> None:
    e = entrada_com_falha
    fonte = FonteFalsa(e.idp)
    cfg = config(e, fechar_sozinha=False)
    r1 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    r2 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    r3 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert [r.seguidas for r in (r1, r2, r3)] == [1, 2, 3]
    assert all(r.nivel_depois == "aberta" for r in (r1, r2, r3))
    assert await nivel(e) == "aberta"
    (frase,) = await extrato(e, "ronda_sem_fechamento", r2.id)
    assert "desligado" in frase.frase
    assert len(await avisos(e, r2.id)) == 1
    assert await avisos(e, r3.id) == []  # um aviso por sequencia


async def test_sem_controle_a_rodada_nao_conta(entrada_com_falha: Entrada) -> None:
    """Se o cracha da ronda nao passa (controle fora do ar), a rodada e inconclusiva e nao fecha nada."""
    e = entrada_com_falha
    cfg = config(e)

    class FonteQueFalha(FonteFalsa):
        async def valido(self) -> str:
            from mcp_claro.ronda.cracha import FalhaCracha

            raise FalhaCracha("provedor fora do ar")

    fonte = FonteQueFalha(e.idp)
    for _ in range(3):
        r = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
        assert not r.resumo.controle_ok
        assert r.seguidas == 0
    assert await nivel(e) == "aberta"
    por_caso = {t.caso: t for t in r.tentativas}
    assert por_caso[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.VERSAO_ANTIGA_CABECALHO].situacao is Situacao.PULADO


async def test_ronda_sem_identidade_grava_controle_inconclusivo(entrada: Entrada) -> None:
    r = await rodar_ronda(entrada.p, entrada.rotinas_db, fonte=None, config=config(entrada))
    (t,) = r.tentativas
    assert t.caso == Caso.CONTROLE
    assert t.situacao is Situacao.INCONCLUSIVO
    guardadas = await linhas(entrada, r.id)
    assert set(guardadas) == {"controle"}
    # O que o controle espera e a vitrine (um resultado), nao so o cracha aceito.
    assert guardadas["controle"].esperado == "vitrine da ronda (resultado do tools/list)"
    assert guardadas["controle"].obtido == "sem identidade da ronda (PATROL_KEY_FILE)"


async def test_tabela_ronda_so_de_acrescentar(entrada: Entrada) -> None:
    r = await rodar_ronda(entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), config=config(entrada))
    for comando in ("UPDATE ronda SET obtido = 'x' WHERE rodada = :r", "DELETE FROM ronda WHERE rodada = :r"):
        for engine in (entrada.rotinas_db, entrada.dono):
            with pytest.raises(DBAPIError):
                async with engine.begin() as c:
                    await c.execute(text(comando), {"r": r.id})


# --- Fase 2: vitrine furada e a ronda como programa ------------------------------------------------


_vitrine_de_verdade = repositorio.ferramenta_na_vitrine


async def _vitrine_furada(
    c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, nome_vitrine: str, versao: str
) -> Any:
    """Falha plantada: acha a ferramenta na ordem de qualquer pessoa, como se fosse de quem pediu."""
    dona = (
        await c.execute(
            text(
                "SELECT o.pessoa_id, o.aplicativo_id FROM ordens_servico o"
                " JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = o.modelo_id"
                " JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id AND f.nome_original = mf.nome_original"
                " WHERE f.nome_vitrine = :n AND o.situacao = 'ativa' LIMIT 1"
            ),
            {"n": nome_vitrine},
        )
    ).first()
    if dona is None:
        return await _vitrine_de_verdade(c, pessoa_id, aplicativo_id, nome_vitrine, versao)
    return await _vitrine_de_verdade(c, dona.pessoa_id, dona.aplicativo_id, nome_vitrine, versao)


async def test_vitrine_furada_a_ronda_acusa_e_fecha_a_entrada(
    entrada: Entrada, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Com a vitrine furada, a entrada recusa depois, pelo tipo de chave (a ronda e um programa). Isso e passagem."""
    monkeypatch.setattr(repositorio, "ferramenta_na_vitrine", _vitrine_furada)
    cfg = config(entrada)
    r1 = await rodar_ronda(
        entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=cfg
    )
    outra = {t.caso: t for t in r1.tentativas}[Caso.OUTRA_PESSOA]
    assert outra.detalhes["ferramenta"] == entrada.ferramenta_ana
    assert outra.detalhes["motivo"] == "programa_sem_chave_propria"
    assert outra.situacao is Situacao.PASSOU
    assert r1.resumo.controle_ok
    assert {t.caso for t in r1.resumo.passagens} == {Caso.OUTRA_PESSOA}
    guardadas = await linhas(entrada, r1.id)
    assert not guardadas["outra_pessoa"].defendeu
    (falha,) = await extrato(entrada, "ronda_falha", r1.id)
    assert "ferramenta de outra pessoa" in falha.frase
    r2 = await rodar_ronda(
        entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=cfg
    )
    assert (r2.seguidas, r2.nivel_antes, r2.nivel_depois) == (2, "aberta", "so_leitura")
    assert await nivel(entrada) == "so_leitura"


async def _ronda_id(e: Entrada) -> uuid.UUID:
    async with e.dono.connect() as c:
        valor = (await c.execute(text("SELECT id FROM aplicativos WHERE client_id = :c"), {"c": RONDA})).scalar_one()
    return uuid.UUID(str(valor))


async def _mudar_ronda(e: Entrada, sql: str) -> None:
    async with e.dono.begin() as c:
        await c.execute(text(sql), {"a": await _ronda_id(e)})


@pytest.mark.parametrize(
    ("mudanca", "motivo"),
    [
        ("UPDATE programas SET situacao = 'pausado' WHERE aplicativo_id = :a", "programa_pausado"),
        ("DELETE FROM programas WHERE aplicativo_id = :a", "programa_sem_cadastro"),
        (
            "UPDATE programas SET fim = (now() AT TIME ZONE 'America/Sao_Paulo')::date - 1 WHERE aplicativo_id = :a",
            "programa_vencido",
        ),
    ],
)
async def test_ronda_com_o_proprio_programa_sem_valer_e_inconclusiva(
    entrada: Entrada, mudanca: str, motivo: str
) -> None:
    """Programa da ronda pausado, sem cadastro ou vencido: a vitrine nao e testada, e a rodada nunca e conforme."""
    await _mudar_ronda(entrada, mudanca)  # a proxima subida da entrada refaz o cadastro (test_ronda_apoio)
    r = await rodar_ronda(
        entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=config(entrada)
    )
    por_caso = {t.caso: t for t in r.tentativas}
    assert por_caso[Caso.CONTROLE].detalhes["motivo"] == motivo
    assert por_caso[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.OUTRA_PESSOA].detalhes["motivo"] == motivo
    assert por_caso[Caso.OUTRA_PESSOA].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.CRACHA_FALSO].situacao is Situacao.CONFORME  # as portas de antes seguem testadas
    assert not r.resumo.controle_ok
    assert not r.resumo.falhou
    assert r.seguidas == 0
    guardadas = await linhas(entrada, r.id)
    assert guardadas["controle"].detalhes["situacao"] == "inconclusivo"


async def _avisos_das_portas(e: Entrada, aplicativo_id: uuid.UUID, motivo: str) -> list[Any]:
    async with e.dono.connect() as c:
        return list(
            await c.execute(
                text(
                    "SELECT destino, assunto, corpo FROM avisos WHERE evento = :e"
                    " AND corpo ->> 'aplicativo_id' = :a AND corpo ->> 'motivo' = :m ORDER BY id"
                ),
                {"e": EVENTO_DAS_PORTAS, "a": str(aplicativo_id), "m": motivo},
            )
        )


async def _situacao_da_ronda(e: Entrada) -> str:
    async with e.dono.connect() as c:
        return str(
            (
                await c.execute(
                    text("SELECT situacao FROM programas WHERE aplicativo_id = :a"), {"a": await _ronda_id(e)}
                )
            ).scalar_one()
        )


async def _ultimo_do_extrato(e: Entrada) -> int:
    async with e.dono.connect() as c:
        return int((await c.execute(text("SELECT coalesce(max(id), 0) FROM extrato"))).scalar_one())


async def _extrato_desde(e: Entrada, tipo: str, aplicativo_id: uuid.UUID, desde: int) -> list[str]:
    async with e.dono.connect() as c:
        resultado = await c.execute(
            text("SELECT frase FROM extrato WHERE tipo = :t AND aplicativo_id = :a AND id > :d ORDER BY id"),
            {"t": tipo, "a": aplicativo_id, "d": desde},
        )
        return [str(frase) for frase in resultado.scalars()]


async def test_rotina_dos_programas_nao_pausa_nem_encerra_a_ronda_e_avisa_o_admin(entrada: Entrada) -> None:
    ronda_id = await _ronda_id(entrada)
    try:
        antes = await _ultimo_do_extrato(entrada)
        # Revisao vencida: um programa comum pausaria. A ronda segue ativa e o ADMIN e avisado, uma vez.
        await _mudar_ronda(
            entrada, "UPDATE programas SET revisado_em = now() - interval '91 days' WHERE aplicativo_id = :a"
        )
        await cuidar_dos_programas(entrada.rotinas_db, entrada.p, [ADMIN])
        await cuidar_dos_programas(entrada.rotinas_db, entrada.p, [ADMIN])
        assert await _situacao_da_ronda(entrada) == "ativo"
        avisos = await _avisos_das_portas(entrada, ronda_id, "revisao_vencida")
        para_admin = [a for a in avisos if a.destino == ADMIN]
        assert len(para_admin) == 1
        assert para_admin[0].assunto.startswith("MCP-CLARO: IMPORTANTE")
        assert para_admin[0].corpo["texto"].startswith(f"IMPORTANTE: o programa {RONDA} testa as portas")
        assert f"/telas/admin/programas/{ronda_id}" in para_admin[0].corpo["texto"]
        pessoas = [a for a in avisos if a.destino.startswith("pessoa:")]
        assert len(pessoas) == 2  # responsavel e substituto, uma vez
        assert all("/telas/programas." in a.corpo["texto"] for a in pessoas)
        mantido = await _extrato_desde(entrada, "programa_das_portas_mantido", ronda_id, antes)
        assert len(mantido) == 1
        assert "a rotina nao o pausou porque ele testa as portas do MCP-CLARO" in mantido[0]
        assert "revisao periodica" in mantido[0]
        r = await rodar_ronda(
            entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=config(entrada)
        )
        assert r.resumo.controle_ok
        assert all(t.situacao is Situacao.CONFORME for t in r.tentativas)

        # Data de fim vencida: a rotina nao encerra; a entrada recusa, a ronda fica inconclusiva e o ADMIN sabe.
        await _mudar_ronda(
            entrada,
            "UPDATE programas SET fim = (now() AT TIME ZONE 'America/Sao_Paulo')::date - 1 WHERE aplicativo_id = :a",
        )
        await cuidar_dos_programas(entrada.rotinas_db, entrada.p, [ADMIN])
        assert await _situacao_da_ronda(entrada) == "ativo"
        (aviso_fim,) = [a for a in await _avisos_das_portas(entrada, ronda_id, "encerrados") if a.destino == ADMIN][-1:]
        assert "Estenda a data de fim" in aviso_fim.corpo["texto"]
        r = await rodar_ronda(
            entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=config(entrada)
        )
        assert not r.resumo.controle_ok
        assert {t.caso: t for t in r.tentativas}[Caso.CONTROLE].detalhes["motivo"] == "programa_vencido"
    finally:
        await _mudar_ronda(
            entrada,
            "UPDATE programas SET revisado_em = now(), fim = current_date + 30, situacao = 'ativo'"
            " WHERE aplicativo_id = :a",
        )


# --- Rodada 2 (observacao 1): controle recusado antes da vitrine -----------------------------------


@pytest.mark.parametrize(
    ("ligar", "desligar", "motivo"),
    [
        (
            "INSERT INTO bloqueios (tipo, alcance, aplicativo_id, quem, motivo)"
            " VALUES ('bloqueio', 'aplicativo', :a, 'admin:teste', 'teste da ronda')",
            "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'admin:teste'"
            " WHERE aplicativo_id = :a AND encerrado_em IS NULL",
            "bloqueio_aplicativo",
        ),
        (
            "INSERT INTO bloqueios (tipo, alcance, quem, motivo)"
            " VALUES ('pausa', 'tudo', 'admin:teste', 'teste da ronda')",
            "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'admin:teste'"
            " WHERE alcance = 'tudo' AND motivo = 'teste da ronda' AND encerrado_em IS NULL",
            "pausa_tudo",
        ),
        (
            "UPDATE estado_entrada SET nivel = 'fechada', motivo = 'teste', mudou_em = clock_timestamp(),"
            " mudou_por = 'instalacao' WHERE id = 1",
            "UPDATE estado_entrada SET nivel = 'aberta', motivo = 'teste', mudou_em = clock_timestamp(),"
            " mudou_por = 'instalacao' WHERE id = 1",
            "entrada_fechada",
        ),
    ],
)
async def test_ronda_recusada_antes_da_vitrine_e_inconclusiva(
    entrada: Entrada, ligar: str, desligar: str, motivo: str
) -> None:
    """Bloqueio do aplicativo da ronda, pausa geral ou entrada fechada: a rodada nao diz "conforme".

    Antes, o controle recebia a recusa e saia conforme, e a rodada saia "conforme" sem ter testado a vitrine.
    """
    try:
        await _mudar_ronda(entrada, ligar)
        r = await rodar_ronda(
            entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=config(entrada)
        )
    finally:
        await _mudar_ronda(entrada, desligar)
    por_caso = {t.caso: t for t in r.tentativas}
    assert por_caso[Caso.CONTROLE].detalhes["motivo"] == motivo
    assert por_caso[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.OUTRA_PESSOA].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.CRACHA_FALSO].situacao is Situacao.CONFORME  # as portas de antes seguem testadas
    assert not r.resumo.controle_ok
    assert not r.resumo.falhou
    assert r.seguidas == 0
    guardadas = await linhas(entrada, r.id)
    assert guardadas["controle"].detalhes["situacao"] == "inconclusivo"


async def test_ronda_no_limite_por_minuto_e_inconclusiva(entrada: Entrada) -> None:
    """Com o limite por minuto da ronda gasto, o controle e recusado antes da vitrine: inconclusivo."""
    cheio = (
        "INSERT INTO programas_pedidos_minuto (aplicativo_id, minuto, contagem)"
        " SELECT :a, date_trunc('minute', now()) + make_interval(mins => m), :limite FROM generate_series(0, 1) m"
        " ON CONFLICT (aplicativo_id, minuto) DO UPDATE SET contagem = EXCLUDED.contagem"
    )
    ronda_id = await _ronda_id(entrada)
    try:
        async with entrada.dono.begin() as c:  # este minuto e o seguinte: o teste nao depende da virada
            await c.execute(text(cheio), {"a": ronda_id, "limite": entrada.p.PROGRAM_MAX_REQUESTS_PER_MINUTE})
        r = await rodar_ronda(
            entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), emails_admin=[ADMIN], config=config(entrada)
        )
    finally:
        await _mudar_ronda(entrada, "DELETE FROM programas_pedidos_minuto WHERE aplicativo_id = :a")
    por_caso = {t.caso: t for t in r.tentativas}
    assert por_caso[Caso.CONTROLE].detalhes["motivo"] == "programa_limite_minuto"
    assert por_caso[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.OUTRA_PESSOA].situacao is Situacao.INCONCLUSIVO
    assert not r.resumo.controle_ok
    assert not r.resumo.falhou
