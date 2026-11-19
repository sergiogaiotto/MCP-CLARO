"""Rotinas da Fase 2 contra PostgreSQL real, com o usuario ``mcp_claro_rotinas``: fichas que vencem, aviso
de ficha pendente (uma vez por ficha, mesmo com duas copias no ar) e limites que vencem."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import acoes_caixa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.rotinas import acoes
from mcp_claro.rotinas.agendador import Agendador, Desfecho
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.saida.protocolo import codigo_conferencia, novo_protocolo
from tests.integracao import test_rotinas_apoio as apoio
from tests.integracao.test_rotinas_apoio import Cadastro, cadastro_minimo, ordem

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

engine_dono = apoio.engine_dono
engine_rotinas = apoio.engine_rotinas
parametros_rotinas = apoio.parametros_rotinas

BASE = "https://mcp-claro.teste"


@dataclass(frozen=True)
class Arranjo:
    cad: Cadastro
    sistema_id: uuid.UUID
    sistema_nome: str
    ferramenta_id: uuid.UUID
    nome_vitrine: str
    ordem_id: uuid.UUID


async def arranjo(engine_dono: AsyncEngine, *, email: str | None = None) -> Arranjo:
    """Pessoa, aplicativo, sistema, ferramenta revisada e ordem: o minimo para uma operacao existir."""
    cad = await cadastro_minimo(engine_dono, email=email)
    async with engine_dono.begin() as c:
        sistema_id = await repositorio.cadastrar_sistema(
            c, nome=f"caixa-{cad.sufixo}", endereco="http://127.0.0.1:9/mcp", tipo_chave="por_pessoa"
        )
        ferramenta_id = await repositorio.revisar_ferramenta(
            c,
            sistema_id=sistema_id,
            definicao_declarada=acoes_caixa.FERRAMENTAS[0],
            nome_vitrine=f"chamado_{cad.sufixo}",
            descricao="Acao de teste.",
            classe="muda",
            revisado_por="teste",
        )
    ordem_id = await ordem(engine_dono, cad, inicio="-1 day", fim="10 days")
    return Arranjo(cad, sistema_id, f"caixa-{cad.sufixo}", ferramenta_id, f"chamado_{cad.sufixo}", ordem_id)


@dataclass(frozen=True)
class FichaCriada:
    ficha_id: uuid.UUID
    operacao_id: uuid.UUID
    protocolo: str


async def nova_ficha(
    engine_dono: AsyncEngine,
    a: Arranjo,
    *,
    montada: str = "-10 minutes",
    expira: str = "20 minutes",
    argumentos: dict[str, Any] | None = None,
) -> FichaCriada:
    """Uma acao esperando a ficha, com horarios relativos ao agora do banco."""
    args = argumentos or {"titulo": f"segredo-{uuid.uuid4().hex[:8]}"}
    async with engine_dono.begin() as c:
        protocolo = await novo_protocolo(c)
        codigo = codigo_conferencia(a.sistema_id, "criar_chamado", args)
        op = (
            await c.execute(
                text(
                    "INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
                    " ferramenta_id, sistema_id, argumentos, chave_pedido_igual)"
                    " VALUES (:pr, :co, :pe, :a, :o, :f, :s, CAST(:ar AS jsonb), :k) RETURNING id"
                ),
                {
                    "pr": protocolo,
                    "co": codigo,
                    "pe": a.cad.pessoa_id,
                    "a": a.cad.aplicativo_id,
                    "o": a.ordem_id,
                    "f": a.ferramenta_id,
                    "s": a.sistema_id,
                    "ar": json.dumps(args),
                    "k": uuid.uuid4().hex,
                },
            )
        ).scalar_one()
        ficha = (
            await c.execute(
                text(
                    "INSERT INTO fichas (operacao_id, codigo_conferencia, montada_em, expira_em, risco)"
                    " VALUES (:o, :co, now() + CAST(CAST(:m AS text) AS interval),"
                    " now() + CAST(CAST(:e AS text) AS interval), 'medio') RETURNING id"
                ),
                {"o": op, "co": codigo, "m": montada, "e": expira},
            )
        ).scalar_one()
        await c.execute(
            text("INSERT INTO fila_saida (operacao_id, estado, ficha_id) VALUES (:o, 'aguardando_ficha', :f)"),
            {"o": op, "f": ficha},
        )
    return FichaCriada(uuid.UUID(str(ficha)), uuid.UUID(str(op)), protocolo)


async def um(engine: AsyncEngine, sql: str, **params: object) -> Any:
    async with engine.connect() as c:
        return (await c.execute(text(sql), params)).one()


async def contar(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as c:
        return int((await c.execute(text(sql), params)).scalar_one())


async def webhook_de_teste(engine_dono: AsyncEngine) -> uuid.UUID:
    """Destino de webhook que so assina o aviso de ficha pendente (para nao mexer nos outros testes)."""
    async with engine_dono.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO destinos_webhook (nome, url, eventos, criado_por)"
                    " VALUES (:n, 'https://127.0.0.1:9/gancho', ARRAY['ficha_pendente'], 'teste') RETURNING id"
                ),
                {"n": f"fichas-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def desligar_webhook(engine_dono: AsyncEngine, destino_id: uuid.UUID) -> None:
    async with engine_dono.begin() as c:
        await c.execute(text("UPDATE destinos_webhook SET ativo = false WHERE id = :i"), {"i": destino_id})


async def webhooks_que_assinam(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as c:
        ids = (
            await c.execute(
                text(
                    "SELECT id FROM destinos_webhook WHERE ativo"
                    " AND (cardinality(eventos) = 0 OR 'ficha_pendente' = ANY (eventos))"
                )
            )
        ).scalars()
        return {f"webhook:{i}" for i in ids}


async def avisos_da_ficha(engine: AsyncEngine, ficha_id: uuid.UUID) -> list[Any]:
    async with engine.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT * FROM avisos WHERE chave_unica LIKE :k ORDER BY id"),
                    {"k": f"ficha_pendente:{ficha_id}%"},
                )
            ).all()
        )


# --- Fichas que vencem -----------------------------------------------------------------------


async def test_ficha_sem_resposta_vence_e_nada_segue(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    a = await arranjo(engine_dono)
    vencida = await nova_ficha(engine_dono, a, montada="-40 minutes", expira="-1 minute")
    no_prazo = await nova_ficha(engine_dono, a, montada="-1 minute", expira="20 minutes")
    parada = await nova_ficha(engine_dono, a, montada="-40 minutes", expira="-1 minute")
    async with engine_dono.begin() as c:  # parada antes de vencer (por exemplo, por um bloqueio)
        await c.execute(
            text("UPDATE fila_saida SET estado = 'parada_por_bloqueio' WHERE operacao_id = :o"),
            {"o": parada.operacao_id},
        )

    protocolos = await acoes.vencer_fichas(engine_rotinas)
    assert vencida.protocolo in protocolos
    assert no_prazo.protocolo not in protocolos
    assert parada.protocolo not in protocolos

    linha = await um(
        engine_rotinas,
        "SELECT f.estado, f.mudou_por, fi.decisao, fi.decidida_em FROM fila_saida f"
        " JOIN fichas fi ON fi.id = f.ficha_id WHERE f.operacao_id = :o",
        o=vencida.operacao_id,
    )
    assert (linha.estado, linha.mudou_por, linha.decisao) == ("vencida", "rotina:vencimento", "vencida")
    assert linha.decidida_em is not None
    assert (
        await um(engine_rotinas, "SELECT estado FROM fila_saida WHERE operacao_id = :o", o=no_prazo.operacao_id)
    ).estado == "aguardando_ficha"
    assert (
        await um(engine_rotinas, "SELECT estado FROM fila_saida WHERE operacao_id = :o", o=parada.operacao_id)
    ).estado == "parada_por_bloqueio"

    extrato = await um(
        engine_rotinas,
        "SELECT frase, papel, pessoa_id FROM extrato WHERE tipo = 'ficha_vencida' AND protocolo = :p",
        p=vencida.protocolo,
    )
    assert extrato.frase == f"A ficha do protocolo {vencida.protocolo} venceu sem resposta; nada saiu."
    assert (extrato.papel, extrato.pessoa_id) == ("rotinas", a.cad.pessoa_id)

    # Outra rodada nao repete nada.
    assert vencida.protocolo not in await acoes.vencer_fichas(engine_rotinas)
    n = await contar(
        engine_rotinas,
        "SELECT count(*) FROM extrato WHERE tipo = 'ficha_vencida' AND protocolo = :p",
        p=vencida.protocolo,
    )
    assert n == 1


# --- Aviso de ficha pendente -----------------------------------------------------------------


async def test_aviso_de_ficha_pendente_uma_vez_por_email_e_webhook(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine
) -> None:
    email = f"dona-{uuid.uuid4().hex[:6]}@exemplo.teste"
    a = await arranjo(engine_dono, email=email)
    segredo = f"segredo-do-argumento-{uuid.uuid4().hex[:8]}"
    pendente = await nova_ficha(engine_dono, a, montada="-10 minutes", argumentos={"titulo": segredo})
    recente = await nova_ficha(engine_dono, a, montada="-1 minute")
    decidida = await nova_ficha(engine_dono, a, montada="-10 minutes")
    vencendo = await nova_ficha(engine_dono, a, montada="-30 minutes", expira="-1 minute")
    async with engine_dono.begin() as c:
        await c.execute(
            text("UPDATE fichas SET decisao = 'recusada', decidida_em = now() WHERE id = :f"),
            {"f": decidida.ficha_id},
        )
    destino = await webhook_de_teste(engine_dono)
    try:
        novos = await acoes.avisar_fichas_pendentes(engine_rotinas, apos_minutos=5, base_url=BASE + "/")
        assinam = await webhooks_que_assinam(engine_rotinas)
        assert f"webhook:{destino}" in assinam
        avisos = await avisos_da_ficha(engine_rotinas, pendente.ficha_id)
        assert novos >= len(avisos) == 1 + len(assinam)
        por_canal = {(av.canal, av.destino) for av in avisos}
        assert ("email", f"pessoa:{a.cad.pessoa_id}") in por_canal
        assert {d for canal, d in por_canal if canal == "webhook"} == assinam
        link = f"{BASE}/telas/fichas/{pendente.ficha_id}"
        for av in avisos:
            assert av.evento == "ficha_pendente"
            assert av.situacao == "pendente"
            corpo = av.corpo
            assert corpo["protocolo"] == pendente.protocolo
            assert corpo["ferramenta"] == a.nome_vitrine
            assert corpo["sistema"] == a.sistema_nome
            assert corpo["link"] == link
            assert corpo["prazo"]
            assert link in corpo["texto"]
            assert pendente.protocolo in corpo["texto"]
            # Nunca o argumento, nunca o endereco da pessoa.
            assert segredo not in json.dumps(corpo, ensure_ascii=False) + av.assunto
            assert email not in json.dumps(corpo, ensure_ascii=False) + av.destino
        for outra in (recente, decidida, vencendo):
            assert await avisos_da_ficha(engine_rotinas, outra.ficha_id) == []

        marcada = await um(engine_rotinas, "SELECT avisada_em FROM fichas WHERE id = :f", f=pendente.ficha_id)
        assert marcada.avisada_em is not None
        assert (
            await um(engine_rotinas, "SELECT avisada_em FROM fichas WHERE id = :f", f=recente.ficha_id)
        ).avisada_em is None
        extrato = await um(
            engine_rotinas,
            "SELECT frase, dados, papel, ferramenta FROM extrato WHERE tipo = 'ficha_avisada' AND protocolo = :p",
            p=pendente.protocolo,
        )
        assert extrato.frase == f"A pessoa foi avisada da ficha pendente do protocolo {pendente.protocolo}."
        assert (extrato.papel, extrato.ferramenta) == ("rotinas", a.nome_vitrine)
        assert segredo not in json.dumps(extrato.dados)

        # Outra rodada nao avisa de novo a mesma ficha.
        await acoes.avisar_fichas_pendentes(engine_rotinas, apos_minutos=5, base_url=BASE)
        assert len(await avisos_da_ficha(engine_rotinas, pendente.ficha_id)) == len(avisos)
        n = await contar(
            engine_rotinas,
            "SELECT count(*) FROM extrato WHERE tipo = 'ficha_avisada' AND protocolo = :p",
            p=pendente.protocolo,
        )
        assert n == 1

        # A ficha recente fica velha: agora sim, um aviso.
        async with engine_dono.begin() as c:
            await c.execute(
                text("UPDATE fichas SET montada_em = now() - interval '6 minutes' WHERE id = :f"),
                {"f": recente.ficha_id},
            )
        await acoes.avisar_fichas_pendentes(engine_rotinas, apos_minutos=5, base_url=BASE)
        assert len(await avisos_da_ficha(engine_rotinas, recente.ficha_id)) == 1 + len(assinam)
    finally:
        await desligar_webhook(engine_dono, destino)


async def test_duas_copias_das_rotinas_avisam_uma_vez(parametros_rotinas: Parametros, engine_dono: AsyncEngine) -> None:
    """Duas copias das rotinas, cada uma com o proprio pool, rodando a mesma rodada ao mesmo tempo."""
    from mcp_claro.db.engine import criar_engine

    a = await arranjo(engine_dono, email=f"dupla-{uuid.uuid4().hex[:6]}@exemplo.teste")
    fichas = [await nova_ficha(engine_dono, a, montada="-10 minutes") for _ in range(12)]
    copias = [criar_engine(parametros_rotinas, pool_size=2, max_overflow=0) for _ in range(2)]
    try:
        for _ in range(3):
            await asyncio.gather(
                *(acoes.avisar_fichas_pendentes(e, apos_minutos=5, base_url=BASE) for e in copias for _ in range(2))
            )
    finally:
        for e in copias:
            await e.dispose()
    assinam = await webhooks_que_assinam(engine_dono)
    for f in fichas:
        avisos = await avisos_da_ficha(engine_dono, f.ficha_id)
        assert len(avisos) == 1 + len(assinam)
        assert len({av.chave_unica for av in avisos}) == len(avisos)
        n = await contar(
            engine_dono, "SELECT count(*) FROM extrato WHERE tipo = 'ficha_avisada' AND protocolo = :p", p=f.protocolo
        )
        assert n == 1


async def test_pessoa_sem_email_nem_webhook_fica_marcada_sem_extrato(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine
) -> None:
    a = await arranjo(engine_dono)
    f = await nova_ficha(engine_dono, a, montada="-10 minutes")
    assinam = await webhooks_que_assinam(engine_rotinas)
    await acoes.avisar_fichas_pendentes(engine_rotinas, apos_minutos=5, base_url=BASE)
    avisos = await avisos_da_ficha(engine_rotinas, f.ficha_id)
    assert {av.canal for av in avisos} <= {"webhook"}
    assert len(avisos) == len(assinam)
    assert (await um(engine_rotinas, "SELECT avisada_em FROM fichas WHERE id = :f", f=f.ficha_id)).avisada_em
    # A pessoa nao recebeu nada: o extrato nunca diz "a pessoa foi avisada". O aviso so aos webhooks da
    # instalacao tem frase propria.
    n = await contar(
        engine_rotinas, "SELECT count(*) FROM extrato WHERE tipo = 'ficha_avisada' AND protocolo = :p", p=f.protocolo
    )
    assert n == 0
    so_webhooks = await contar(
        engine_rotinas,
        "SELECT count(*) FROM extrato WHERE tipo = 'ficha_avisada_webhooks' AND protocolo = :p",
        p=f.protocolo,
    )
    assert so_webhooks == (1 if assinam else 0)


async def test_pessoa_sem_email_com_webhook_tem_a_frase_dos_webhooks(
    engine_rotinas: AsyncEngine, engine_dono: AsyncEngine
) -> None:
    a = await arranjo(engine_dono)
    f = await nova_ficha(engine_dono, a, montada="-10 minutes")
    destino = await webhook_de_teste(engine_dono)
    try:
        await acoes.avisar_fichas_pendentes(engine_rotinas, apos_minutos=5, base_url=BASE)
    finally:
        await desligar_webhook(engine_dono, destino)
    assert {av.canal for av in await avisos_da_ficha(engine_rotinas, f.ficha_id)} == {"webhook"}
    n = await contar(
        engine_rotinas, "SELECT count(*) FROM extrato WHERE tipo = 'ficha_avisada' AND protocolo = :p", p=f.protocolo
    )
    assert n == 0
    linha = await um(
        engine_rotinas,
        "SELECT frase FROM extrato WHERE tipo = 'ficha_avisada_webhooks' AND protocolo = :p",
        p=f.protocolo,
    )
    assert linha.frase == (
        f"O aviso da ficha pendente do protocolo {f.protocolo} foi so aos webhooks da instalacao; nao houve aviso"
        " por e-mail a pessoa."
    )


# --- Limites que vencem ----------------------------------------------------------------------


async def novo_limite(engine_dono: AsyncEngine, a: Arranjo, *, valido: str, situacao: str = "ativo") -> uuid.UUID:
    async with engine_dono.begin() as c:
        valor = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, por_dia, valido_ate,"
                    " com_passkey, situacao) VALUES (:p, :a, :s, 'criar_chamado', 3,"
                    " now() + CAST(CAST(:v AS text) AS interval), true, :si) RETURNING id"
                ),
                {"p": a.cad.pessoa_id, "a": a.cad.aplicativo_id, "s": a.sistema_id, "v": valido, "si": situacao},
            )
        ).scalar_one()
    return uuid.UUID(str(valor))


async def test_limite_com_prazo_no_passado_vence(engine_rotinas: AsyncEngine, engine_dono: AsyncEngine) -> None:
    a = await arranjo(engine_dono)
    passado = await novo_limite(engine_dono, a, valido="-1 minute")
    futuro = await novo_limite(engine_dono, a, valido="1 day")
    suspenso = await novo_limite(engine_dono, a, valido="-1 minute", situacao="suspenso")

    assert await acoes.vencer_limites(engine_rotinas) >= 1
    linha = await um(engine_rotinas, "SELECT situacao, motivo FROM limites WHERE id = :i", i=passado)
    assert (linha.situacao, linha.motivo) == ("vencido", acoes.MOTIVO_LIMITE_VENCIDO)
    assert (await um(engine_rotinas, "SELECT situacao FROM limites WHERE id = :i", i=futuro)).situacao == "ativo"
    assert (await um(engine_rotinas, "SELECT situacao FROM limites WHERE id = :i", i=suspenso)).situacao == "suspenso"
    extrato = await um(
        engine_rotinas,
        "SELECT frase, papel, pessoa_id, ferramenta FROM extrato WHERE tipo = 'limite_encerrado'"
        " AND dados ->> 'limite_id' = :i",
        i=str(passado),
    )
    assert extrato.frase == f"O limite de {a.nome_vitrine} foi encerrado: {acoes.MOTIVO_LIMITE_VENCIDO}."
    assert (extrato.papel, extrato.pessoa_id, extrato.ferramenta) == ("rotinas", a.cad.pessoa_id, a.nome_vitrine)

    # Duas copias ao mesmo tempo, e de novo: uma linha so no extrato.
    await novo_limite(engine_dono, a, valido="-2 minutes")
    await asyncio.gather(acoes.vencer_limites(engine_rotinas), acoes.vencer_limites(engine_rotinas))
    await acoes.vencer_limites(engine_rotinas)
    n = await contar(
        engine_rotinas,
        "SELECT count(*) FROM extrato WHERE tipo = 'limite_encerrado' AND pessoa_id = :p",
        p=a.cad.pessoa_id,
    )
    assert n == 2


# --- Tarefas no agendador --------------------------------------------------------------------


async def test_tarefas_de_acoes_rodam_no_agendador(engine_rotinas: AsyncEngine, banco_migrado: Parametros) -> None:
    tarefas = acoes.tarefas_de_acoes(engine_rotinas, banco_migrado, ConfigRotinas())
    assert [(t.nome, t.intervalo) for t in tarefas] == [
        ("vencer_fichas", acoes.INTERVALO_FICHAS),
        ("avisar_fichas_pendentes", acoes.INTERVALO_AVISO_FICHAS),
        ("vencer_limites", acoes.INTERVALO_LIMITES),
        ("limpar_desafios_passkey", acoes.INTERVALO_DESAFIOS),
    ]
    agendador = Agendador(engine_rotinas, tarefas, primeira_espera=0)
    for t in tarefas:
        assert await agendador.rodar_uma(t) == Desfecho.FEITA
