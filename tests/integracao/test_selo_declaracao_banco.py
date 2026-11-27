"""``selo/declaracao.py`` contra PostgreSQL 18.7 real (Fase 4, grupo G2): o cadastro que a declaracao confere, a
gravacao em ``declaracoes_selo`` e a leitura da mais recente.

- ``cadastro_do_sistema`` le as ferramentas de acao que o selo cobre (``selo_acoes_do_sistema``), as revisadas ativas
  que so leem, toda revisada do sistema (de qualquer classe e situacao) e a producao de todo sistema do cadastro.
- ``conferir`` e o banco (``declaracoes_selo_nasce``) concordam sobre o alvo de teste que e a producao de um sistema
  (normalizado); a tela recusa antes, com a frase para o ADMIN.
- O alvo com o nome escrito de outro jeito (pontos de outros alfabetos, IPv4 curto, decimal ou hexadecimal) nao passa
  da tela, e nada chega ao banco; a producao escrita de outro jeito no cadastro e pega pela forma de conexao.
- O que a tela aceita o banco guarda: o caractere nulo, que o jsonb nao guarda, e os niveis demais, que o pydantic
  nao serializa, sao recusados com ``DeclaracaoRecusada`` antes da gravacao.
- ``salvar`` grava a versao seguinte, com as colunas iguais ao JSON, pela entrada; duas gravacoes ao mesmo tempo
  entram uma depois da outra. ``ultima`` devolve a mais recente, que o pedido de teste aceita.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.config.settings import Ambiente
from mcp_claro.selo import contrato
from mcp_claro.selo import declaracao as d
from tests import apoio_selos as apoio
from tests.apoio_terceiros import cadastrar_terceiro, definicao, revisar_ferramenta
from tests.integracao.test_migracao_0006 import ENTRADA, como, dono

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture


@dataclass
class Cena:
    admin_id: uuid.UUID
    sistema: apoio.SistemaDoSelo


@pytest.fixture
async def cena(dono: AsyncEngine) -> Cena:
    async with dono.begin() as c:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(
            c, acoes=("enviar_lembrete", "apagar_lembrete"), leituras=("listar_lembretes", "ver_lembrete")
        )
        return Cena(admin, sistema)


async def num_rascunho(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            return await passos(c)
        finally:
            await transacao.rollback()


def modelo(**mudar: Any) -> d.Declaracao:
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"])
    dados.update(mudar)
    return d.validar(dados)


# --- Cadastro ------------------------------------------------------------------------------------------------


async def test_cadastro_do_sistema(dono: AsyncEngine, cena: Cena) -> None:
    async def montar_e_ler(c: AsyncConnection) -> d.Cadastro:
        s = cena.sistema.id
        await revisar_ferramenta(c, s, definicao("ver_lembrete", anotacoes={"readOnlyHint": True}))  # outra versao
        await revisar_ferramenta(c, s, definicao("arquivar_lembrete"), classe="tem_volta")
        await c.execute(
            text("UPDATE ferramentas_revisadas SET situacao = 'suspensa' WHERE sistema_id = :s AND nome_original = :n"),
            {"s": s, "n": "arquivar_lembrete"},
        )
        await revisar_ferramenta(c, s, definicao("ler_suspensa", anotacoes={"readOnlyHint": True}))
        await c.execute(
            text("UPDATE ferramentas_revisadas SET situacao = 'suspensa' WHERE sistema_id = :s AND nome_original = :n"),
            {"s": s, "n": "ler_suspensa"},
        )
        await como(c, ENTRADA)  # a tela le com o papel da entrada
        return await d.cadastro_do_sistema(c, s)

    cad = await num_rascunho(dono, montar_e_ler)
    assert cad.de_terceiro is False
    assert cad.tipo_chave == "por_pessoa"
    assert cad.acoes == {"enviar_lembrete", "apagar_lembrete"}
    assert cad.leituras == {"listar_lembretes", "ver_lembrete"}
    assert cad.revisadas == {
        "enviar_lembrete",
        "apagar_lembrete",
        "listar_lembretes",
        "ver_lembrete",
        "arquivar_lembrete",
        "ler_suspensa",
    }
    assert cena.sistema.endereco in cad.producoes
    async with dono.connect() as c:
        todos = (await c.execute(text("SELECT count(*) FROM sistemas"))).scalar_one()
    assert len(cad.producoes) == todos


async def test_cadastro_de_terceiro_e_de_sistema_que_nao_existe(dono: AsyncEngine) -> None:
    async def ler(c: AsyncConnection) -> d.Cadastro:
        terceiro = await cadastrar_terceiro(c, endereco=f"https://t-{uuid.uuid4().hex[:8]}.exemplo.teste/mcp")
        return await d.cadastro_do_sistema(c, terceiro)

    cad = await num_rascunho(dono, ler)
    assert cad.de_terceiro is True
    with pytest.raises(d.DeclaracaoRecusada, match="terceiros ainda não"):
        d.conferir(modelo(), cad, ambiente=Ambiente.PRODUCAO)
    async with dono.connect() as c:
        with pytest.raises(LookupError):
            await d.cadastro_do_sistema(c, uuid.uuid4())


# --- Alvo contra a producao: a tela e o banco dizem o mesmo -----------------------------------------------------


@pytest.mark.parametrize("forma", ["igual", "barra", "maiusculas", "porta"])
async def test_alvo_que_e_producao_de_outro_sistema_e_recusado_pela_tela_e_pelo_banco(
    dono: AsyncEngine, cena: Cena, forma: str
) -> None:
    async def passos(c: AsyncConnection) -> tuple[d.DeclaracaoRecusada | None, str | None]:
        outro = await apoio.proprio_com_acoes(c)
        base = outro.endereco
        alvo = {
            "igual": base,
            "barra": base + "/",
            "maiusculas": base.replace("https://", "https://").upper().replace("HTTPS://", "https://"),
            "porta": base.replace(".teste/", ".teste:443/"),
        }[forma]
        declarada = modelo(endereco_teste=alvo)
        recusa = None
        try:
            d.conferir(declarada, await d.cadastro_do_sistema(c, cena.sistema.id), ambiente=Ambiente.PRODUCAO)
        except d.DeclaracaoRecusada as e:
            recusa = e
        banco = None
        await c.execute(text("SAVEPOINT antes"))
        try:
            await d.salvar(
                c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="admin", declaracao=declarada
            )
        except DBAPIError as e:
            banco = str(e)
            await c.execute(text("ROLLBACK TO SAVEPOINT antes"))
        return recusa, banco

    recusa, banco = await num_rascunho(dono, passos)
    assert recusa is not None
    assert recusa.motivo == "alvo_na_producao"
    assert banco is not None
    assert "a bateria nao escreve na producao" in banco


async def test_alvo_que_nao_e_producao_passa_na_tela_e_no_banco(dono: AsyncEngine, cena: Cena) -> None:
    async def passos(c: AsyncConnection) -> d.DeclaracaoSalva:
        declarada = modelo(endereco_teste=f"https://teste-{uuid.uuid4().hex[:8]}.exemplo.teste/mcp")
        d.conferir(declarada, await d.cadastro_do_sistema(c, cena.sistema.id), ambiente=Ambiente.PRODUCAO)
        return await d.salvar(
            c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="a", declaracao=declarada
        )

    assert (await num_rascunho(dono, passos)).versao == 1


async def _declaracoes(c: AsyncConnection, sistema_id: uuid.UUID) -> int:
    return int(
        (
            await c.execute(text("SELECT count(*) FROM declaracoes_selo WHERE sistema_id = :s"), {"s": sistema_id})
        ).scalar_one()
    )


@pytest.mark.parametrize(
    ("producao", "alvo"),
    [
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}\u3002exemplo\u3002teste/mcp"),
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}\uff0eexemplo\uff0eteste/mcp"),
        ("http://127.0.0.1:{porta}/mcp", "http://127.1:{porta}/mcp"),
        ("http://127.0.0.1:{porta}/mcp", "http://2130706433:{porta}/mcp"),
        ("http://127.0.0.1:{porta}/mcp", "http://0x7f.0.0.1:{porta}/mcp"),
    ],
)
async def test_alvo_escrito_de_outro_jeito_nao_passa_da_tela(
    dono: AsyncEngine, cena: Cena, producao: str, alvo: str
) -> None:
    """O cliente conecta na producao (``httpx2`` troca os pontos; o resolvedor le ``127.1`` como 127.0.0.1), mas o
    texto e outro: a tela recusa na forma, e nada chega a ``declaracoes_selo``."""
    nome, porta = f"prod-{uuid.uuid4().hex[:6]}", 20000 + uuid.uuid4().int % 20000
    producao, alvo = producao.format(nome=nome, porta=porta), alvo.format(nome=nome, porta=porta)
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo)

    async def passos(c: AsyncConnection) -> tuple[d.DeclaracaoRecusada | None, int]:
        await apoio.proprio_com_acoes(c, endereco=producao)
        await como(c, ENTRADA)
        recusa = None
        try:
            declarada = d.ler_texto(json.dumps(dados))
            d.conferir(declarada, await d.cadastro_do_sistema(c, cena.sistema.id), ambiente=Ambiente.TESTE)
            await d.salvar(c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="a", declaracao=declarada)
        except d.DeclaracaoRecusada as e:
            recusa = e
        return recusa, await _declaracoes(c, cena.sistema.id)

    recusa, gravadas = await num_rascunho(dono, passos)
    assert recusa is not None
    assert recusa.motivo == "forma"
    assert "escrito de um jeito só" in recusa.mensagem
    assert gravadas == 0


@pytest.mark.parametrize(
    ("producao", "alvo"),
    [
        ("https://{nome}\u3002exemplo\u3002teste/mcp", "https://{nome}.exemplo.teste/mcp"),
        ("https://cobran\u00e7a-{nome}.exemplo.teste/mcp", "https://{idna}/mcp"),
        ("http://127.1:{porta}/mcp", "http://127.0.0.1:{porta}/mcp"),
        ("http://127.0.0.1:{porta}/mcp", "http://localhost:{porta}/mcp"),
    ],
)
async def test_producao_escrita_de_outro_jeito_no_cadastro_e_pega_pela_forma_de_conexao(
    dono: AsyncEngine, cena: Cena, producao: str, alvo: str
) -> None:
    """A producao de OUTRO sistema, gravada no cadastro com o nome escrito de outro jeito, e o alvo na forma que o
    cliente conecta: ``conferir`` le a producao do banco e recusa, embora o texto normalizado seja outro."""
    nome, porta = uuid.uuid4().hex[:6], 20000 + uuid.uuid4().int % 20000
    producao = producao.format(nome=nome, porta=porta)
    alvo = alvo.format(nome=nome, porta=porta, idna=httpx2.URL(producao).raw_host.decode("ascii"))
    assert alvo.isascii()
    assert not contrato.mesmo_endereco(producao, alvo)  # o texto normalizado (a regra do banco) e outro

    async def passos(c: AsyncConnection) -> d.DeclaracaoRecusada | None:
        await apoio.proprio_com_acoes(c, endereco=producao)
        await como(c, ENTRADA)
        declarada = d.ler_texto(
            json.dumps(apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo))
        )
        try:
            d.conferir(declarada, await d.cadastro_do_sistema(c, cena.sistema.id), ambiente=Ambiente.TESTE)
        except d.DeclaracaoRecusada as e:
            return e
        return None

    recusa = await num_rascunho(dono, passos)
    assert recusa is not None
    assert recusa.motivo == "alvo_na_producao"


async def test_caractere_nulo_nao_chega_ao_banco(dono: AsyncEngine, cena: Cena) -> None:
    """O jsonb do PostgreSQL recusa o U+0000: a tela recusa antes, com ``DeclaracaoRecusada``, e nada e gravado. Sem
    a recusa (o modelo montado direto, como era a tela antes), a gravacao estoura com um erro do banco."""
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"])
    dados["ferramentas"]["enviar_lembrete"]["argumentos"]["texto"] = "a\u0000b"

    async def passos(c: AsyncConnection) -> tuple[d.DeclaracaoRecusada | None, str | None, int]:
        await como(c, ENTRADA)
        recusa = None
        try:
            d.ler_texto(json.dumps(dados))
        except d.DeclaracaoRecusada as e:
            recusa = e
        banco = None
        await c.execute(text("SAVEPOINT antes"))
        try:
            await d.salvar(
                c,
                sistema_id=cena.sistema.id,
                pessoa_id=cena.admin_id,
                salva_por="a",
                declaracao=d.Declaracao.model_validate(dados),
            )
        except DBAPIError as e:
            banco = str(e)
            await c.execute(text("ROLLBACK TO SAVEPOINT antes"))
        return recusa, banco, await _declaracoes(c, cena.sistema.id)

    recusa, banco, gravadas = await num_rascunho(dono, passos)
    assert recusa is not None
    assert (recusa.motivo, recusa.mensagem) == ("json_invalido", d.NULO_NA_DECLARACAO)
    assert banco is not None
    assert "unsupported Unicode escape sequence" in banco  # o jsonb recusa o \\u0000
    assert gravadas == 0


async def test_niveis_ate_o_teto_sao_gravados_e_lidos_de_volta(dono: AsyncEngine, cena: Cena) -> None:
    """O que ``validar`` aceita o banco guarda e ``modelo()`` le de volta; acima do teto, ``DeclaracaoRecusada``
    antes da gravacao (sem ela, ``salvar`` levantava ``ValueError`` do pydantic, ao serializar)."""

    def com_fundo(niveis: int) -> dict[str, Any]:
        dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"])
        aninhado: Any = "x"
        for _ in range(niveis - 4):
            aninhado = [aninhado]
        dados["ferramentas"]["enviar_lembrete"]["argumentos"]["texto"] = aninhado
        return dados

    async def passos(c: AsyncConnection) -> tuple[d.DeclaracaoSalva | None, d.Declaracao, str]:
        await como(c, ENTRADA)
        no_teto = d.ler_texto(json.dumps(com_fundo(d.FUNDO_MAXIMO)))
        d.conferir(no_teto, await d.cadastro_do_sistema(c, cena.sistema.id), ambiente=Ambiente.PRODUCAO)
        await d.salvar(c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="a", declaracao=no_teto)
        with pytest.raises(d.DeclaracaoRecusada, match="níveis"):
            d.ler_texto(json.dumps(com_fundo(300)))
        with pytest.raises(ValueError, match="depth") as sem_teto:
            await d.salvar(
                c,
                sistema_id=cena.sistema.id,
                pessoa_id=cena.admin_id,
                salva_por="a",
                declaracao=d.Declaracao.model_validate(com_fundo(300)),
            )
        return await d.ultima(c, cena.sistema.id), no_teto, str(sem_teto.value)

    salva, no_teto, sem_teto = await num_rascunho(dono, passos)
    assert salva is not None
    assert (salva.versao, salva.modelo()) == (1, no_teto)
    assert "depth" in sem_teto


# --- Gravacao e leitura ---------------------------------------------------------------------------------------


async def test_salvar_pela_entrada_grava_versoes_e_ultima_devolve_a_mais_recente(dono: AsyncEngine, cena: Cena) -> None:
    primeira = modelo()
    segunda = modelo(responsavel="Outra equipe do sistema")

    async def gravar(c: AsyncConnection) -> tuple[d.DeclaracaoSalva, d.DeclaracaoSalva, d.DeclaracaoSalva | None, Any]:
        await como(c, ENTRADA)
        cad = await d.cadastro_do_sistema(c, cena.sistema.id)
        d.conferir(primeira, cad, ambiente=Ambiente.PRODUCAO)
        um = await d.salvar(
            c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="ADMIN de teste", declaracao=primeira
        )
        dois = await d.salvar(
            c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="ADMIN de teste", declaracao=segunda
        )
        ultima = await d.ultima(c, cena.sistema.id)
        colunas = (
            await c.execute(
                text(
                    "SELECT contrato, modo, endereco_teste, responsavel, salva_por, salva_em = now() AS agora"
                    " FROM declaracoes_selo WHERE id = :i"
                ),
                {"i": dois.id},
            )
        ).one()
        return um, dois, ultima, colunas

    um, dois, ultima, colunas = await num_rascunho(dono, gravar)
    assert (um.versao, dois.versao) == (1, 2)
    assert ultima == dois
    assert dois.dados == segunda.guardada()
    assert dois.modelo() == segunda
    assert um.modelo() == primeira
    assert tuple(colunas) == (
        "selo/1",
        "ambiente_de_teste",
        apoio.ENDERECO_TESTE,
        "Outra equipe do sistema",
        "ADMIN de teste",
        True,
    )


async def test_sem_declaracao_ultima_e_none(dono: AsyncEngine, cena: Cena) -> None:
    async with dono.connect() as c:
        assert await d.ultima(c, cena.sistema.id) is None


async def test_a_declaracao_salva_serve_ao_pedido_de_teste(dono: AsyncEngine, cena: Cena) -> None:
    async def passos(c: AsyncConnection) -> str:
        salva = await d.salvar(
            c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="a", declaracao=modelo()
        )
        pedido = await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, salva.id, rodando=False)
        estado = (
            await c.execute(text("SELECT estado FROM pedidos_teste_selo WHERE id = :p"), {"p": pedido})
        ).scalar_one()
        return str(estado)

    assert await num_rascunho(dono, passos) == "pedido"


async def test_so_um_admin_ativo_salva(dono: AsyncEngine, cena: Cena) -> None:
    async def passos(c: AsyncConnection) -> None:
        pessoa = await apoio.nova_pessoa(c)
        await d.salvar(c, sistema_id=cena.sistema.id, pessoa_id=pessoa, salva_por="x", declaracao=modelo())

    with pytest.raises(DBAPIError, match="So um ADMIN ativo"):
        await num_rascunho(dono, passos)


async def test_duas_gravacoes_ao_mesmo_tempo_entram_uma_depois_da_outra(dono: AsyncEngine, cena: Cena) -> None:
    """A trava consultiva da transacao: a segunda espera a primeira e grava a versao seguinte, sem erro de chave
    unica (sem a trava, as duas calculariam a mesma versao)."""
    sistema = cena.sistema.id
    async with dono.connect() as c1, dono.connect() as c2:
        t1 = await c1.begin()
        um = await d.salvar(c1, sistema_id=sistema, pessoa_id=cena.admin_id, salva_por="a", declaracao=modelo())
        t2 = await c2.begin()
        segunda = asyncio.create_task(
            d.salvar(c2, sistema_id=sistema, pessoa_id=cena.admin_id, salva_por="b", declaracao=modelo())
        )
        await asyncio.sleep(0.3)
        assert not segunda.done()  # esperando a trava da primeira
        await t1.commit()
        dois = await asyncio.wait_for(segunda, timeout=10)
        await t2.commit()
    assert (um.versao, dois.versao) == (1, 2)
