"""``selo/vigente.py`` contra PostgreSQL 18.7 real (desenho 4.4 e 9; grupo G2 da Fase 4).

- ``ler`` e ``ler_todos`` devolvem o que a funcao do banco ``selo_vigente()`` (e a visao ``selos_vigentes``)
  devolve, pelos tres papeis que tem EXECUTE; o cadastro (``sistema_por_id(..., com_selo=True)``) traz o mesmo em
  ``Sistema.selo_vigente``. Sem ``com_selo`` (os caminhos do despachante que nao usam o selo), a consulta nem chama
  ``selo_vigente()``: um defeito nela (como a explosao da forma essencial do esquema, que e do G0) nao para a
  sondagem, a releitura das declaracoes nem a revisao do servidor.
- O retrato (``SeloVigente.retrato``) do vigente e do efetivo passa pela guarda da ficha (``fichas_guarda``, que
  compara as datas por igualdade, com microssegundos).
- O efetivo: chave desligada, a pessoa real fica no nivel 1 e a de teste com o vigente; ligada pelo construtor de
  ``Parametros``, o vigente para as duas.
- Cada motivo de ``selo_vigente()`` (mais ``fase_desligada``) e provocado de verdade, e ``motivo_no_banco`` diz o texto
  certo, com o que o banco sabe (quantos casos falharam, qual ferramenta e nova, quando e por que foi retirado).
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.selo import contrato, vigente
from mcp_claro.selo.vigente import FASE_DESLIGADA, NIVEL_1_SEM_TESTE, SeloVigente, efetivo
from tests import apoio_selos as apoio
from tests.apoio_terceiros import cadastrar_terceiro, definicao, gravar_revisao_do_servidor, revisar_ferramenta
from tests.integracao.test_migracao_0006 import DESPACHANTE, ENTRADA, ROTINAS, como, dono

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture

PAPEIS: Final = (ENTRADA, DESPACHANTE, ROTINAS)
FUSO: Final = ZoneInfo(vigente.FUSO_PADRAO)


@dataclass
class Cena:
    admin_id: uuid.UUID
    sistema: apoio.SistemaDoSelo


@pytest.fixture
async def cena(dono: AsyncEngine) -> Cena:
    async with dono.begin() as c:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(c, acoes=("enviar_lembrete", "apagar_lembrete"))
        return Cena(admin, sistema)


async def no_dono(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async with engine.begin() as c:
        return await passos(c)


async def num_rascunho(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    """Roda ``passos`` numa transacao que sempre volta atras."""
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            return await passos(c)
        finally:
            await transacao.rollback()


def data(momento: Any) -> str:
    return str(momento.astimezone(FUSO).strftime("%d/%m/%Y"))


# --- Leitura -------------------------------------------------------------------------------------------------


async def test_ler_devolve_o_que_a_funcao_do_banco_devolve(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.connect() as c:
        antes = await vigente.ler(c, cena.sistema.id)
    assert antes == SeloVigente(1, None, None, None, None, "nunca_testado")
    teste = await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id))
    async with dono.connect() as c:
        selo = await vigente.ler(c, cena.sistema.id)
        linha = await apoio.selo_vigente(c, cena.sistema.id)
        todos = await vigente.ler_todos(c)
        sistema = await repositorio.sistema_por_id(c, cena.sistema.id, com_selo=True)
        sem_selo = await repositorio.sistema_por_id(c, cena.sistema.id)
    assert selo == SeloVigente(3, teste.id, teste.testado_em, teste.vale_ate, "ambiente_de_teste", "vigente")
    assert selo == SeloVigente.de_linha(linha)
    assert selo.testado_em is not None
    assert selo.testado_em.microsecond == teste.testado_em.microsecond
    assert todos[cena.sistema.id] == selo
    assert sistema is not None
    assert sistema.selo_vigente == selo
    assert sistema.selo == 1  # a coluna antiga fica presa em 1 (D8)
    assert sem_selo is not None
    assert sem_selo.selo_vigente is NIVEL_1_SEM_TESTE  # nao lido: o nivel 1, que nao promete nada
    assert dataclasses.replace(sem_selo, selo_vigente=selo) == sistema  # o resto e o mesmo


async def test_ler_todos_traz_cada_sistema_do_cadastro(dono: AsyncEngine, cena: Cena) -> None:
    async def montar(c: AsyncConnection) -> uuid.UUID:
        outro = await apoio.proprio_com_acoes(c)
        await apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id)
        return outro.id

    outro = await no_dono(dono, montar)
    async with dono.connect() as c:
        todos = await vigente.ler_todos(c)
        quantos = (await c.execute(text("SELECT count(*) FROM sistemas"))).scalar_one()
        for sistema_id in (cena.sistema.id, outro):
            assert todos[sistema_id] == await vigente.ler(c, sistema_id)
    assert len(todos) == quantos
    assert (todos[cena.sistema.id].nivel, todos[outro].nivel) == (2, 1)


@pytest.mark.parametrize("papel", PAPEIS)
async def test_os_tres_papeis_leem_o_selo_pela_funcao(dono: AsyncEngine, cena: Cena, papel: str) -> None:
    await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id))

    async def ler_como(c: AsyncConnection) -> tuple[SeloVigente, SeloVigente, str]:
        await como(c, papel)
        return (
            await vigente.ler(c, cena.sistema.id),
            (await vigente.ler_todos(c))[cena.sistema.id],
            await vigente.motivo_no_banco(c, cena.sistema.id),
        )

    selo, de_todos, texto = await num_rascunho(dono, ler_como)
    assert selo == de_todos
    assert (selo.nivel, selo.motivo) == (2, "vigente")
    assert texto.startswith("Selo agora: nível 2. Teste de ")


async def test_cadastro_le_o_selo_pela_entrada_e_pelo_despachante(dono: AsyncEngine, cena: Cena) -> None:
    await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id))
    for papel in (ENTRADA, DESPACHANTE):

        async def ler_como(c: AsyncConnection, p: str = papel) -> Any:
            await como(c, p)
            return await repositorio.sistema_por_id(c, cena.sistema.id, com_selo=True)

        sistema = await num_rascunho(dono, ler_como)
        assert sistema.selo_vigente.nivel == 3


_SELO_VIGENTE_QUE_FALHA: Final = """
CREATE OR REPLACE FUNCTION selo_vigente(p_sistema uuid, p_agora timestamptz DEFAULT now())
RETURNS TABLE (nivel smallint, teste_id uuid, testado_em timestamptz, vale_ate timestamptz, modo text, motivo text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'selo_vigente chamada';
END $$
"""


@pytest.mark.parametrize("papel", [DESPACHANTE, ENTRADA])
async def test_sistema_por_id_sem_selo_nem_chama_a_funcao_do_selo(dono: AsyncEngine, cena: Cena, papel: str) -> None:
    """Com ``selo_vigente()`` trocada (so nesta transacao, que volta atras) por uma que falha, ``sistema_por_id``
    sem ``com_selo`` responde, e com ``com_selo`` falha: e a juncao, e so ela, que chama a funcao."""

    async def passos(c: AsyncConnection) -> tuple[Any, str]:
        await c.execute(text(_SELO_VIGENTE_QUE_FALHA))
        await como(c, papel)
        sistema = await repositorio.sistema_por_id(c, cena.sistema.id)
        await c.execute(text("SAVEPOINT com_selo"))
        with pytest.raises(DBAPIError) as erro:
            await repositorio.sistema_por_id(c, cena.sistema.id, com_selo=True)
        await c.execute(text("ROLLBACK TO SAVEPOINT com_selo"))
        return sistema, str(erro.value)

    sistema, erro = await num_rascunho(dono, passos)
    assert sistema.id == cena.sistema.id
    assert sistema.selo_vigente is NIVEL_1_SEM_TESTE
    assert "selo_vigente chamada" in erro


ESQUEMA_QUE_EXPLODE: Final = {
    "$defs": {"a": {"type": "object", "properties": {"x": {"$ref": "#/$defs/a"}, "y": {"$ref": "#/$defs/a"}}}},
    "$ref": "#/$defs/a",
}
"""129 bytes cuja forma essencial cresce em exponencial (achado do verificador; a correcao e do G0, em
``contrato.forma_essencial`` e ``selo_esquema_no``)."""


async def test_sistema_por_id_do_despachante_responde_com_o_esquema_que_explode(dono: AsyncEngine, cena: Cena) -> None:
    """O cenario do verificador: um proprio com selo vigente lista ``selo_consultar`` com um esquema que explode, e o
    despachante grava a definicao em ``ferramentas_vistas``. O despachante ainda le o sistema (para sondar, reler o
    que ele declara e revisar), com um tempo curto: sem a juncao do selo, a leitura nao passa por
    ``selo_esquema_essencial``."""

    async def montar(c: AsyncConnection) -> None:
        await apoio.gravar_teste(c, cena.sistema.id, nivel=2, admin_id=cena.admin_id)
        explode = dict(apoio.definicao_do_contrato(contrato.FERRAMENTA_CONSULTAR), inputSchema=ESQUEMA_QUE_EXPLODE)
        await apoio.listar_contrato(c, cena.sistema.id, 2, definicoes={contrato.FERRAMENTA_CONSULTAR: explode})

    await no_dono(dono, montar)

    async def ler(c: AsyncConnection) -> tuple[Any, float]:
        await como(c, DESPACHANTE)
        await c.execute(text("SET LOCAL statement_timeout = '3s'"))
        inicio = time.monotonic()
        sistema = await repositorio.sistema_por_id(c, cena.sistema.id)
        return sistema, time.monotonic() - inicio

    sistema, levou = await num_rascunho(dono, ler)
    assert sistema.id == cena.sistema.id
    assert levou < 3


# --- Retrato e efetivo -----------------------------------------------------------------------------------------


async def _ficha_com(c: AsyncConnection, cena: Cena, retrato: dict[str, Any], *, teste: bool = True) -> Any:
    pessoa = await apoio.nova_pessoa(c, teste=teste)
    return await apoio.criar_operacao(c, cena.sistema.acoes[0], pessoa_id=pessoa, ficha_selo=retrato, liberada=False)


async def test_o_retrato_do_vigente_e_do_efetivo_passa_pela_guarda_da_ficha(dono: AsyncEngine, cena: Cena) -> None:
    await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id))

    async def gravar(c: AsyncConnection) -> list[Any]:
        selo = await vigente.ler(c, cena.sistema.id)
        retratos = [
            selo.retrato(),
            efetivo(selo, ligada=False, pessoa_teste=False).retrato(),
            efetivo(selo, ligada=False, pessoa_teste=True).retrato(),
        ]
        for r in retratos:
            await _ficha_com(c, cena, r)
        guardados = (
            await c.execute(
                text(
                    "SELECT f.selo FROM fichas f JOIN operacoes o ON o.id = f.operacao_id"
                    " WHERE o.sistema_id = :s ORDER BY o.protocolo"
                ),
                {"s": cena.sistema.id},
            )
        ).scalars()
        return [dict(g) for g in guardados]

    guardados = await num_rascunho(dono, gravar)
    assert [g["nivel"] for g in guardados] == [3, 1, 3]
    assert guardados[1] == FASE_DESLIGADA.retrato()
    assert SeloVigente.de_retrato(guardados[0]) == SeloVigente.de_retrato(guardados[2])


async def test_retrato_com_a_data_cortada_seria_recusado_pela_ficha(dono: AsyncEngine, cena: Cena) -> None:
    """A prova de que os microssegundos importam: o banco compara por igualdade (contrato.RETRATO_DO_SELO)."""
    await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, nivel=2, admin_id=cena.admin_id))

    async def gravar(c: AsyncConnection) -> None:
        selo = await vigente.ler(c, cena.sistema.id)
        assert selo.testado_em is not None
        cortado = selo.retrato() | {"testado_em": selo.testado_em.replace(microsecond=0).isoformat()}
        await _ficha_com(c, cena, cortado)

    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="selo vigente do sistema agora"):
        await num_rascunho(dono, gravar)


async def test_efetivo_com_pessoas_do_banco(dono: AsyncEngine, cena: Cena) -> None:
    async def montar(c: AsyncConnection) -> tuple[bool, bool]:
        await apoio.gravar_teste(c, cena.sistema.id, nivel=3, admin_id=cena.admin_id)
        real = await apoio.nova_pessoa(c)
        de_teste = await apoio.nova_pessoa(c, teste=True)
        lidas = {
            p: bool((await c.execute(text("SELECT teste FROM pessoas WHERE id = :p"), {"p": p})).scalar_one())
            for p in (real, de_teste)
        }
        return lidas[real], lidas[de_teste]

    real_teste, de_teste_teste = await no_dono(dono, montar)
    assert (real_teste, de_teste_teste) == (False, True)
    async with dono.connect() as c:
        selo = await vigente.ler(c, cena.sistema.id)
    desligada = Parametros()
    ligada = Parametros(
        FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=True, FEATURE_PHASE3_THIRD_PARTY=True, FEATURE_PHASE4_BADGES=True
    )
    assert efetivo(selo, ligada=desligada.FEATURE_PHASE4_BADGES, pessoa_teste=real_teste) == FASE_DESLIGADA
    assert efetivo(selo, ligada=desligada.FEATURE_PHASE4_BADGES, pessoa_teste=de_teste_teste) == selo
    assert efetivo(selo, ligada=ligada.FEATURE_PHASE4_BADGES, pessoa_teste=real_teste) == selo
    assert efetivo(selo, ligada=ligada.FEATURE_PHASE4_BADGES, pessoa_teste=de_teste_teste) == selo


# --- Motivos legiveis, cada um provocado -----------------------------------------------------------------------


async def _reativar(c: AsyncConnection, sistema_id: uuid.UUID) -> None:
    """A guarda de sistemas poe 'pendente' quando o endereco ou o tipo de chave mudam; a sondagem volta a 'ativo'."""
    await c.execute(
        text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2026-07-28' WHERE id = :s"), {"s": sistema_id}
    )


async def _retirar(c: AsyncConnection, cena: Cena) -> None:
    await c.execute(
        text(
            "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
            " SELECT :s, teste_id, 'admin', 'o sistema mudou de equipe', 'admin-teste', :a FROM selo_vigente(:s)"
        ),
        {"s": cena.sistema.id, "a": cena.admin_id},
    )


async def _terceiro_com_revisao_nova(c: AsyncConnection, cena: Cena) -> uuid.UUID:
    terceiro = await cadastrar_terceiro(c, endereco=f"https://terceiro-{uuid.uuid4().hex[:8]}.exemplo.teste/mcp")
    await revisar_ferramenta(c, terceiro, definicao("enviar_x"), classe="muda")
    await gravar_revisao_do_servidor(c, terceiro)
    await apoio.listar_contrato(c, terceiro, 2)
    await apoio.gravar_teste(c, terceiro, admin_id=cena.admin_id)
    await gravar_revisao_do_servidor(c, terceiro)
    return terceiro


async def _provocar(c: AsyncConnection, cena: Cena, motivo: str) -> uuid.UUID:
    """Deixa o sistema da cena (ou um terceiro novo) com o ``motivo`` e devolve o sistema."""
    s = cena.sistema.id
    if motivo == "nunca_testado":
        return s
    if motivo == "revisao_nova":
        return await _terceiro_com_revisao_nova(c, cena)
    if motivo == "reprovado":
        # Um teste so: dois testes na mesma transacao teriam o mesmo testado_em (now() da transacao).
        ferramentas = await apoio.acoes_do_sistema(c, s)
        casos = apoio.casos_que_aprovam(ferramentas, 2)
        falhas = {("C-2.1", str(ferramentas[0])), ("C-2.5", str(ferramentas[1]))}
        casos = [
            apoio.item(i["caso"], i["ferramenta"], "falhou") if (i["caso"], i["ferramenta"]) in falhas else i
            for i in casos
        ]
        await apoio.gravar_teste(c, s, aprovado=False, casos=casos, admin_id=cena.admin_id)
        return s
    await apoio.gravar_teste(c, s, admin_id=cena.admin_id)
    if motivo == "retirado":
        await _retirar(c, cena)
    elif motivo == "fora_de_ativo":
        await c.execute(text("UPDATE sistemas SET situacao = 'desativado' WHERE id = :s"), {"s": s})
    elif motivo == "endereco_mudou":
        novo = f"https://novo-{uuid.uuid4().hex[:8]}.exemplo.teste/mcp"
        await c.execute(text("UPDATE sistemas SET endereco = :e WHERE id = :s"), {"e": novo, "s": s})
        await _reativar(c, s)
    elif motivo == "tipo_chave_mudou":
        await c.execute(text("UPDATE sistemas SET tipo_chave = 'programa' WHERE id = :s"), {"s": s})
        await _reativar(c, s)
    elif motivo == "ferramenta_nova":
        await revisar_ferramenta(c, s, definicao("criar_lembrete"), classe="tem_volta")
        await revisar_ferramenta(c, s, cena.sistema.acoes[1].definicao | {"description": "outra"}, classe="muda")
    elif motivo == "contrato_sumiu":
        await c.execute(
            text("UPDATE ferramentas_vistas SET sumiu_em = now() WHERE sistema_id = :s AND nome = 'selo_consultar'"),
            {"s": s},
        )
    elif motivo != "vigente":
        raise AssertionError(motivo)
    return s


TEXTOS: Final[dict[str, Callable[[SeloVigente], str]]] = {
    "nunca_testado": lambda _: "Selo agora: nível 1. Nunca testado.",
    "vigente": lambda v: f"Selo agora: nível 2. Teste de {data(v.testado_em)}, vale até {data(v.vale_ate)}.",
    "reprovado": lambda _: "Selo agora: nível 1. O último teste não foi aprovado: 2 casos falharam.",
    "retirado": lambda _: "Selo agora: nível 1. Retirado em {hoje}: retirado pelo ADMIN.",
    "fora_de_ativo": lambda _: "Selo agora: nível 1. Voltou ao nível 1: o sistema saiu de ativo.",
    "endereco_mudou": lambda _: "Selo agora: nível 1. Voltou ao nível 1: o endereço mudou depois do teste.",
    "tipo_chave_mudou": lambda _: "Selo agora: nível 1. Voltou ao nível 1: o tipo de chave mudou depois do teste.",
    "revisao_nova": lambda _: "Selo agora: nível 1. Voltou ao nível 1: a revisão do servidor é nova.",
    "ferramenta_nova": lambda _: (
        "Selo agora: nível 1. Voltou ao nível 1: as ferramentas apagar_lembrete, criar_lembrete são novas ou mudaram"
        " depois do teste."
    ),
    "contrato_sumiu": lambda _: (
        "Selo agora: nível 1. Voltou ao nível 1: o sistema não lista mais as ferramentas do contrato."
    ),
}
"""O texto de cada motivo que ``ler`` pode devolver. ``vencido`` (so com ``p_agora``) e ``fase_desligada`` (do
efetivo) tem testes proprios abaixo."""


def test_a_tabela_dos_testes_cobre_todo_motivo() -> None:
    assert set(TEXTOS) | {"vencido", "fase_desligada"} == set(contrato.MOTIVOS_DO_RETRATO)


@pytest.mark.parametrize("motivo", sorted(TEXTOS))
async def test_cada_motivo_do_banco_tem_o_texto_certo(dono: AsyncEngine, cena: Cena, motivo: str) -> None:
    """Lido como a entrada, o papel das telas do ADMIN: ``detalhes`` le ``testes_selo``, ``selos_retirados`` e
    ``selo_acoes_do_sistema`` com os GRANTs dela."""

    async def passos(c: AsyncConnection) -> tuple[SeloVigente, str, Any]:
        sistema_id = await _provocar(c, cena, motivo)
        agora = (await c.execute(text("SELECT now()"))).scalar_one()
        await como(c, ENTRADA)
        return await vigente.ler(c, sistema_id), await vigente.motivo_no_banco(c, sistema_id), agora

    selo, texto, agora = await num_rascunho(dono, passos)
    assert selo.motivo == motivo
    assert selo.nivel == (2 if motivo == "vigente" else 1)
    assert texto == TEXTOS[motivo](selo).replace("{hoje}", data(agora))


async def test_detalhes_do_retirado_trazem_o_texto_do_admin(dono: AsyncEngine, cena: Cena) -> None:
    async def passos(c: AsyncConnection) -> vigente.Detalhes:
        await _provocar(c, cena, "retirado")
        return await vigente.detalhes(c, cena.sistema.id, await vigente.ler(c, cena.sistema.id))

    detalhes = await num_rascunho(dono, passos)
    assert (detalhes.retirada, detalhes.detalhe) == ("admin", "o sistema mudou de equipe")
    assert detalhes.retirado_em is not None


async def test_vencido_pelo_relogio_do_banco(dono: AsyncEngine, cena: Cena) -> None:
    teste = await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id))
    async with dono.connect() as c:
        agora = (await c.execute(text("SELECT now()"))).scalar_one()
        selo = SeloVigente.de_linha(await apoio.selo_vigente(c, cena.sistema.id, agora + timedelta(days=91)))
        detalhes = await vigente.detalhes(c, cena.sistema.id, selo)
    assert (selo.nivel, selo.motivo, selo.vale_ate) == (1, "vencido", teste.vale_ate)
    assert vigente.selo_agora_legivel(selo, detalhes) == (
        f"Selo agora: nível 1. O teste venceu em {data(teste.vale_ate)}."
    )


async def test_fase_desligada_para_a_pessoa_real(dono: AsyncEngine, cena: Cena) -> None:
    await no_dono(dono, lambda c: apoio.gravar_teste(c, cena.sistema.id, admin_id=cena.admin_id))
    async with dono.connect() as c:
        selo = efetivo(await vigente.ler(c, cena.sistema.id), ligada=False, pessoa_teste=False)
        detalhes = await vigente.detalhes(c, cena.sistema.id, selo)
    assert detalhes == vigente.Detalhes()
    assert vigente.selo_agora_legivel(selo) == (
        "Selo agora: nível 1. Nível 1 para as pessoas reais: os selos de nível 2 e 3 ainda não estão ligados; só"
        " pessoas de teste usam o selo deste sistema."
    )


async def test_o_vocabulario_do_banco_e_o_dos_textos(dono: AsyncEngine) -> None:
    async with dono.connect() as c:
        do_banco = (await c.execute(text("SELECT selo_vocabulario('motivos_do_retrato')"))).scalar_one()
        retiradas = (await c.execute(text("SELECT selo_vocabulario('motivos_de_retirada')"))).scalar_one()
    assert set(do_banco) == set(vigente.MOTIVOS_LEGIVEIS)
    assert set(retiradas) == set(vigente.RETIRADAS_LEGIVEIS)
