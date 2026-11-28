"""``selo/declaracao.py`` contra PostgreSQL 18.7 real (Fase 4, grupo G2): o cadastro que a declaracao confere, a
gravacao em ``declaracoes_selo`` e a leitura da mais recente.

- ``cadastro_do_sistema`` le as ferramentas de acao que o selo cobre (``selo_acoes_do_sistema``), as revisadas ativas
  que so leem, toda revisada do sistema (de qualquer classe e situacao) e a producao de todo sistema do cadastro.
- ``conferir`` e o banco (``declaracoes_selo_nasce``) concordam sobre o alvo de teste que e a producao de um sistema
  (normalizado); a tela recusa antes, com a frase para o ADMIN.
- O alvo com o nome ou a porta escritos de outro jeito (pontos de outros alfabetos, IPv4 curto, decimal ou
  hexadecimal, IPv6 com um IPv4 dentro, mapeado ou NAT64, porta com ``+``, ``_`` ou digitos de outros alfabetos) nao
  passa da tela, e o banco o recusa num INSERT direto, com a frase do alvo (tambem a porta longa demais para o
  ``integer``); a producao escrita de outro jeito no cadastro e pega pela forma de conexao, na tela e no banco
  (``selo_endereco_conexao``, que junta os mesmos enderecos que a tela, menos o nome fora de ASCII, que fica so com a
  tela). O 6to4 e o Teredo valem como IPv6 nos dois lados (o residuo escrito).
- O que a tela aceita o banco guarda: o caractere nulo, que o jsonb nao guarda, e os niveis demais, que o pydantic
  nao serializa, sao recusados com ``DeclaracaoRecusada`` antes da gravacao.
- ``salvar`` grava a versao seguinte, com as colunas iguais ao JSON, pela entrada; duas gravacoes ao mesmo tempo
  entram uma depois da outra. ``ultima`` devolve a mais recente, que o pedido de teste aceita.
- O ``cena`` grava de vez (alguns testes usam duas conexoes) e, no fim, tira as ferramentas do sistema da vitrine
  ativa: o nome de vitrine sorteado e unico entre as ativas da sessao inteira (e o sufixo agora tem 48 bits).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx2
import pytest
from pydantic_core import PydanticCustomError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.config.settings import Ambiente
from mcp_claro.selo import contrato
from mcp_claro.selo import declaracao as d
from tests import apoio_selos as apoio
from tests.apoio_terceiros import cadastrar_terceiro, definicao, revisar_ferramenta
from tests.integracao.test_migracao_0006 import ENTRADA, como, dono
from tests.unidade.test_selo_declaracao import EM_OUTRO_LUGAR, NO_MESMO_LUGAR

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture


@dataclass
class Cena:
    admin_id: uuid.UUID
    sistema: apoio.SistemaDoSelo


@asynccontextmanager
async def _cena_gravada(dono: AsyncEngine) -> AsyncIterator[Cena]:
    """Um ADMIN e um sistema com duas acoes e duas leituras, gravados de vez: alguns testes usam duas conexoes.

    No fim, as ferramentas do sistema deixam de ser ativas (etapa 3, G02rr, item 3). O nome de vitrine que
    ``apoio_terceiros.revisar_ferramenta`` sorteia e unico entre as ativas da sessao inteira (indice
    ``ferramentas_vitrine_ativa``). Com o sufixo de 24 bits de antes, cada teste que usa o ``cena`` deixava quatro
    nomes ativos de vez, e uma rodada do grupo deixava centenas de cada nome (``enviar_lembrete_xxxxxx``...): a chance
    de dois sorteios iguais era de cerca de 1% por rodada, e a colisao virava ERROR no preparo de outro teste. Agora o
    sufixo tem 48 bits (``revisar_ferramenta``), e o ``cena`` nao deixa nada ativo."""
    async with dono.begin() as c:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(
            c, acoes=("enviar_lembrete", "apagar_lembrete"), leituras=("listar_lembretes", "ver_lembrete")
        )
    try:
        yield Cena(admin, sistema)
    finally:
        async with dono.begin() as c:
            await c.execute(
                text(
                    "UPDATE ferramentas_revisadas SET situacao = 'substituida'"
                    " WHERE sistema_id = :s AND situacao = 'ativa'"
                ),
                {"s": sistema.id},
            )


@pytest.fixture
async def cena(dono: AsyncEngine) -> AsyncIterator[Cena]:
    async with _cena_gravada(dono) as gravada:
        yield gravada


async def _ativas(dono: AsyncEngine, sistema_id: uuid.UUID) -> int:
    async with dono.connect() as c:
        return int(
            (
                await c.execute(
                    text("SELECT count(*) FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa'"),
                    {"s": sistema_id},
                )
            ).scalar_one()
        )


async def test_o_cena_tira_as_ferramentas_da_vitrine_ativa_no_fim(dono: AsyncEngine) -> None:
    """O ``cena`` grava de vez e, no fim, nao deixa nome de vitrine ativo (a causa do ERROR intermitente no preparo:
    nomes sorteados com 24 bits, unicos entre as ativas da sessao). Tambem quando o teste falha."""
    async with _cena_gravada(dono) as gravada:
        assert await _ativas(dono, gravada.sistema.id) == 4  # gravadas de vez: outra conexao as ve
        revisadas = [*gravada.sistema.acoes, *gravada.sistema.leituras]
        # O sufixo sorteado do nome de vitrine tem 48 bits (12 letras hexadecimais), nao 24.
        assert all(re.fullmatch(f"{r.nome_original}_[0-9a-f]{{12}}", r.nome_vitrine) for r in revisadas), revisadas
    assert await _ativas(dono, gravada.sistema.id) == 0
    with pytest.raises(RuntimeError, match="o teste falhou"):
        async with _cena_gravada(dono) as outra:
            raise RuntimeError("o teste falhou")
    assert await _ativas(dono, outra.sistema.id) == 0


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


# --- O nome do alvo no banco (retoque G02r, item 2) -----------------------------------------------------------

FORMAS_DO_NOME: Final = (
    "cobranca.exemplo.teste",
    "COBRANCA.Exemplo.TESTE",
    "xn--cobrana-gya.exemplo.teste",
    "a_b-c.d",
    "-a.b",
    "exemplo.teste.",
    "localhost",
    "a..b",
    "a b",
    "",
    "127.0.0.1",
    "10.0.0.5",
    "255.255.255.255",
    "0.0.0.0",  # noqa: S104 - um nome do alvo, nao um endereco em que se escuta
    "256.1.1.1",
    "01.2.3.4",
    "127.1",
    "127.0.1",
    "2130706433",
    "0x7f.0.0.1",
    "0x7f000001",
    "0177.0.0.1",
    "1.2.3.4.",
    "1.2.3.4.5",
    "cobranca\u3002exemplo\u3002teste",
    "cobranca\uff0eexemplo\uff0eteste",
    "cobranca\uff61exemplo",
    "cobran\u00e7a.exemplo",
    "\u212a.exemplo",
    "[2001:db8::1]",
    "[2001:DB8::1]",
    "[2001:0db8::1]",
    "[2001:db8:0:0:1:0:0:1]",
    "[2001:db8::1:0:0:1]",
    "[::1]",
    "[::]",
    "[::ffff:10.0.0.5]",
    "[::FFFF:10.0.0.5]",
    "[::ffff:a00:5]",
    "[::ffff:127.0.0.1]",
    "[::ffff:0.0.0.0]",
    "[::ffff:0:0]",
    "[::ffff]",
    "[::ffff:0]",
    "[::ffff:0:a00:5]",
    "[64:ff9b::a00:5]",
    "[64:FF9B::A00:5]",
    "[64:ff9b::10.0.0.5]",
    "[64:ff9b::7f00:1]",
    "[64:ff9b::]",
    "[64:ff9b::1:a00:5]",
    "[64:ff9a:ffff:ffff:ffff:ffff:ffff:ffff]",
    # O 6to4 e o Teredo levam um IPv4 dentro (10.0.0.5), mas valem como IPv6 nos dois lados (o residuo escrito).
    "[2002:a00:5::1]",
    "[2001:0:4136:e378:8000:63bf:f5ff:fffa]",
    "[2001::f5ff:fffa]",
    "[::10.0.0.5]",
    "[::a00:5]",
    "[::1:0]",
    "[::100]",
    "[fe80::1%25eth0]",
    "[1.2.3.4]",
    "[v1.abc]",
    "[2001:db8::1]x",
    "a[::1]",
)
"""Nomes do alvo, do jeito comum e de outros jeitos: a tela (``declaracao._endereco``) e o banco
(``selo_alvo_bem_escrito``) tem de dizer o mesmo de cada um."""

FORMAS_DA_PORTA: Final = (
    "",
    ":",
    ":8123",
    ":0",
    ":00",
    ":0443",
    ":65535",
    ":0000000000065535",
    ":65536",
    ":99999",
    ":100000",
    ":+8123",
    ":+0443",
    ":-0",
    ":4_43",
    ": 443",
    ":443 ",
    ":\u0664\u0664\u0663",
    ":\uff14\uff14\uff13",
    ":0x1bb",
    ":8123:8123",
    ":8123:",
    "::8123",
    ":4294967296",
    ":99999999999",
    ":" + "9" * 40,
    ":" + "0" * 30 + "65536",
    ":" + "0" * 30 + "65535",
)
"""A porta do alvo (etapa 3, G02rr, item 1): vazia, so com digitos ASCII (de 0 a 65535 e fora disso) e escrita como o
``int()`` do leitor do cliente tambem le (``+``, ``_``, espaco, digitos de outros alfabetos). E as longas, com mais
digitos que o ``integer`` do banco guarda (11 e 40 digitos, e 30 zeros a esquerda): a tela as recusa (``.port`` fora
de 0 a 65535), e o banco tem de dizer ``false`` sem estourar a conta (a guarda do tamanho em ``selo_alvo_bem_escrito``;
sem ela, a consulta inteira para com ``value ... is out of range for type integer``)."""

USUARIOS: Final = ("u@", "u:s@", "a@b@", "@", "u:8123@")
"""O usuario antes do nome: a tela o recusa sempre; o banco olha o nome e a porta depois do ultimo ``@``."""


def _a_tela_aceita(endereco: str) -> bool:
    try:
        d._endereco(endereco)
    except PydanticCustomError:
        return False
    return True


async def _bem_escritos(c: AsyncConnection, enderecos: list[str]) -> list[bool]:
    return [
        bool(b)
        for b in (
            await c.execute(
                text(
                    "SELECT selo_alvo_bem_escrito(e) FROM unnest(CAST(:e AS text[])) WITH ORDINALITY AS x(e, n)"
                    " ORDER BY n"
                ),
                {"e": enderecos},
            )
        )
        .scalars()
        .all()
    ]


async def test_a_tela_e_o_banco_concordam_no_nome_do_alvo(dono: AsyncEngine) -> None:
    """A regra do nome e da porta no banco e a da tela (``_NOME_ASCII``, ``_IPV4_ESCRITO``, ``_ip_comum``,
    ``_ipv6_com_ipv4_dentro`` e ``urlsplit(...).port``), forma por forma: cada nome com cada porta, nos dois esquemas.
    Com usuario, o banco olha so o nome e a porta (depois do ultimo ``@``, como ``urlsplit``) e diz o que a tela diz do
    mesmo endereco sem o usuario; a tela recusa o usuario por outro motivo."""
    pares = [
        (endereco, endereco)
        for nome in FORMAS_DO_NOME
        for porta in FORMAS_DA_PORTA
        for endereco in (f"http://{nome}{porta}/selo/mcp", f"https://{nome}{porta}/mcp")
    ]
    for usuario in USUARIOS:
        pares += [(f"http://{usuario}{nome}:8123/mcp", f"http://{nome}:8123/mcp") for nome in FORMAS_DO_NOME]
        pares += [
            (f"https://{usuario}cobranca.exemplo.teste{p}/mcp", f"https://cobranca.exemplo.teste{p}/mcp")
            for p in FORMAS_DA_PORTA
        ]
    tela = [_a_tela_aceita(t) for _, t in pares]
    async with dono.connect() as c:
        banco = await _bem_escritos(c, [b for b, _ in pares])
        assert (await c.execute(text("SELECT selo_alvo_bem_escrito(NULL)"))).scalar_one() is True
    assert [b for (b, _), no_banco, na_tela in zip(pares, banco, tela, strict=True) if no_banco is not na_tela] == []
    # O corpo tem de tudo: aceitos e recusados, e o nome bom com a porta escrita de outro jeito, que o banco aceitava.
    assert any(tela)
    assert not all(tela)
    assert _a_tela_aceita("https://cobranca.exemplo.teste:8123/mcp")
    assert not _a_tela_aceita("https://cobranca.exemplo.teste:+8123/mcp")
    assert not _a_tela_aceita("https://cobranca.exemplo.teste:99999999999/mcp")
    assert _a_tela_aceita("https://cobranca.exemplo.teste:" + "0" * 30 + "65535/mcp")


@pytest.mark.parametrize("porta", ["99999999999", "9" * 40], ids=["11_digitos", "40_digitos"])
@pytest.mark.parametrize("papel", [None, ENTRADA])
async def test_o_banco_recusa_a_porta_longa_com_a_frase_do_alvo(
    dono: AsyncEngine, cena: Cena, porta: str, papel: str | None
) -> None:
    """Etapa 3 (G02rr, retoque): a porta com mais digitos do que o ``integer`` guarda. A tela a recusa
    (``urlsplit(...).port``), e o banco tambem, num INSERT direto, com a frase do alvo (``check_violation``), e nao
    com o erro da conta (``value ... is out of range for type integer``, que nao diz ao ADMIN o que esta errado). Sem
    a guarda do tamanho em ``selo_alvo_bem_escrito``, a conversao da porta estourava antes da comparacao."""
    alvo = f"https://alvo-{uuid.uuid4().hex[:6]}.exemplo.teste:{porta}/mcp"
    assert not _a_tela_aceita(alvo)
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo)

    async def passos(c: AsyncConnection) -> None:
        await como(c, papel)
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)

    with pytest.raises(DBAPIError, match="escrito de um jeito so") as recusa:
        await num_rascunho(dono, passos)
    assert getattr(recusa.value.orig, "sqlstate", None) == "23514"  # check_violation


@pytest.mark.parametrize(
    ("producao", "alvo"),
    [
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}\u3002exemplo\u3002teste/mcp"),
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}\uff0eexemplo\uff0eteste/mcp"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://127.1:{porta}/{nome}/mcp"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://2130706433:{porta}/{nome}/mcp"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://0x7f.0.0.1:{porta}/{nome}/mcp"),
        # Etapa 3 (G02rr, item 1; a prova do verificador, adaptada): a porta que o leitor do cliente le com int().
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}.exemplo.teste:+443/mcp"),
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}.exemplo.teste:4_43/mcp"),
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}.exemplo.teste:\u0664\u0664\u0663/mcp"),
        ("https://{nome}.exemplo.teste/mcp", "https://{nome}.exemplo.teste:443 /mcp"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://localhost:+{porta}/{nome}/mcp"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://[::1]:+{porta}/{nome}/mcp"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://127.0.0.1:+{porta}/{nome}/mcp"),
    ],
    ids=[
        "u3002",
        "uff0e",
        "ipv4_curto",
        "ipv4_decimal",
        "ipv4_hexa",
        "porta_mais",
        "porta_sublinhado",
        "porta_arabe",
        "porta_espaco",
        "porta_localhost_mais",
        "porta_ipv6_mais",
        "porta_mesmo_ip_mais",
    ],
)
@pytest.mark.parametrize("papel", [None, ENTRADA])
async def test_o_banco_recusa_o_alvo_escrito_de_outro_jeito_num_insert_direto(
    dono: AsyncEngine, cena: Cena, producao: str, alvo: str, papel: str | None
) -> None:
    """A segunda barreira (critica A4): sem a tela, o INSERT direto do alvo que e a producao escrita de outro jeito e
    recusado pelo nome ou pela porta (antes, so a comparacao de texto valia, e as cinco formas do nome eram gravadas;
    ate a etapa 3, as sete da porta tambem). O cliente conecta na producao em todas."""
    nome, porta = f"prod-{uuid.uuid4().hex[:6]}", 20000 + uuid.uuid4().int % 20000
    producao, alvo = producao.format(nome=nome, porta=porta), alvo.format(nome=nome, porta=porta)
    assert not contrato.mesmo_endereco(producao, alvo)  # o texto normalizado nao pegava
    assert d.forma_de_conexao(alvo) == d.forma_de_conexao(producao)  # o cliente conecta na producao
    assert not _a_tela_aceita(alvo)
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo)

    async def passos(c: AsyncConnection) -> None:
        await apoio.proprio_com_acoes(c, endereco=producao)
        await como(c, papel)
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)

    with pytest.raises(DBAPIError, match="escrito de um jeito so"):
        await num_rascunho(dono, passos)


def _com_caminho_unico(endereco: str, caminho: str) -> str:
    """O endereco com ``caminho`` logo depois do nome e da porta: a producao e o alvo de um par ganham o mesmo, e nenhum
    sistema que outro teste da sessao deixou gravado chega a eles."""
    comeco = endereco.index("/", endereco.index("://") + 3)
    return endereco[:comeco] + caminho + endereco[comeco:]


SO_A_TELA: Final = frozenset({NO_MESMO_LUGAR[0]})
"""O par que so a tela pega: a producao com o nome fora de ASCII e o alvo na forma ``xn--`` (IDNA, que o banco nao
calcula; veja ``selo_endereco_conexao``)."""


@pytest.mark.parametrize(
    ("producao", "alvo", "mesmo_lugar"),
    [*((p, a, True) for p, a in NO_MESMO_LUGAR), *((p, a, False) for p, a in EM_OUTRO_LUGAR)],
)
async def test_o_banco_compara_o_alvo_com_a_producao_como_a_tela(
    dono: AsyncEngine, cena: Cena, producao: str, alvo: str, mesmo_lugar: bool
) -> None:
    """Retoque 2 do G02r: os pares da unidade (``NO_MESMO_LUGAR`` e ``EM_OUTRO_LUGAR``), com a producao no cadastro e o
    alvo num INSERT direto, sem a tela. O banco recusa o alvo que chega a producao tambem pela forma de conexao (antes,
    so pela forma normalizada: localhost, [::1] e 127.0.0.2 contra 127.0.0.1, ou 10.0.0.5 contra [::ffff:10.0.0.5],
    eram gravados), e guarda o que esta em outro lugar."""
    caminho = f"/u-{uuid.uuid4().hex[:12]}"
    producao, alvo = _com_caminho_unico(producao, caminho), _com_caminho_unico(alvo, caminho)
    assert _a_tela_aceita(alvo)
    assert d.alvo_na_producao(alvo, [producao]) is mesmo_lugar
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo)

    async def passos(c: AsyncConnection) -> int:
        await apoio.proprio_com_acoes(c, endereco=producao)
        await como(c, ENTRADA)
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)
        return await _declaracoes(c, cena.sistema.id)

    if mesmo_lugar and (producao.replace(caminho, ""), alvo.replace(caminho, "")) not in SO_A_TELA:
        with pytest.raises(DBAPIError, match="a bateria nao escreve na producao"):
            await num_rascunho(dono, passos)
    else:
        assert await num_rascunho(dono, passos) == 1


@pytest.mark.parametrize(
    ("producao", "alvo", "recusa"),
    [
        (
            "http://10.{a}.{b}.5:8123/{nome}/mcp",
            "http://[::ffff:10.{a}.{b}.5]:8123/{nome}/mcp",
            "escrito de um jeito so",
        ),
        (
            "http://127.0.0.1:{porta}/{nome}/mcp",
            "http://[::ffff:127.0.0.1]:{porta}/{nome}/mcp",
            "escrito de um jeito so",
        ),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://localhost:{porta}/{nome}/mcp", "nao escreve na producao"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://[::1]:{porta}/{nome}/mcp", "nao escreve na producao"),
        ("http://127.0.0.1:{porta}/{nome}/mcp", "http://127.0.0.2:{porta}/{nome}/mcp", "nao escreve na producao"),
        (
            "http://localhost:{porta}/{nome}/mcp",
            "http://{nome}.localhost:{porta}/{nome}/mcp",
            "nao escreve na producao",
        ),
        # Etapa 3 (G02rr, item 2): o prefixo NAT64 (64:ff9b::/96) leva o IPv4 dentro, como o mapeado.
        ("http://10.{a}.{b}.5:8123/{nome}/mcp", "http://[{nat64}]:8123/{nome}/mcp", "escrito de um jeito so"),
        (
            "http://127.0.0.1:{porta}/{nome}/mcp",
            "http://[64:ff9b::7f00:1]:{porta}/{nome}/mcp",
            "escrito de um jeito so",
        ),
        ("http://[{nat64}]:8123/{nome}/mcp", "http://10.{a}.{b}.5:8123/{nome}/mcp", "nao escreve na producao"),
        (
            "http://[64:ff9b::7f00:1]:{porta}/{nome}/mcp",
            "http://localhost:{porta}/{nome}/mcp",
            "nao escreve na producao",
        ),
    ],
    ids=[
        "mapeado_10",
        "mapeado_loopback",
        "localhost",
        "ipv6_loopback",
        "outro_loopback",
        "sub_localhost",
        "nat64_10",
        "nat64_loopback",
        "producao_nat64_10",
        "producao_nat64_loopback",
    ],
)
@pytest.mark.parametrize("papel", [None, ENTRADA])
async def test_o_banco_recusa_a_producao_escrita_de_outro_jeito_num_insert_direto(
    dono: AsyncEngine, cena: Cena, producao: str, alvo: str, recusa: str, papel: str | None
) -> None:
    """As seis formas da conferencia do retoque (a producao no cadastro e o alvo que chega nela, escrito de outro
    jeito), num INSERT direto, como o dono e como a entrada: antes eram todas gravadas. O IPv6 mapeado sai pelo nome
    (``selo_alvo_bem_escrito``, como na tela), e o loopback pela forma de conexao (``selo_alvo_na_producao``). E as
    quatro do NAT64 (etapa 3, G02rr, item 2), que eram gravadas ate a etapa 3: o alvo ``64:ff9b::a.b.c.d`` sai pelo
    nome, e a producao assim escrita e pega pela forma de conexao, que conta o IPv4 de dentro."""
    sorteio = uuid.uuid4().int
    campos: dict[str, Any] = {
        "nome": f"prod-{uuid.uuid4().hex[:6]}",
        "porta": 20000 + sorteio % 20000,
        "a": sorteio % 256,
        "b": sorteio % 251,
    }
    ipv4 = ipaddress.IPv4Address(f"10.{campos['a']}.{campos['b']}.5")
    campos["nat64"] = ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::")) + int(ipv4)).compressed
    producao, alvo = producao.format(**campos), alvo.format(**campos)
    assert not contrato.mesmo_endereco(producao, alvo)  # o texto normalizado nao pegava
    assert d.alvo_na_producao(alvo, [producao])  # a tela pega (pela forma de conexao, ou antes, pelo nome)
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo)

    async def passos(c: AsyncConnection) -> None:
        await apoio.proprio_com_acoes(c, endereco=producao)
        await como(c, papel)
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)

    with pytest.raises(DBAPIError, match=recusa):
        await num_rascunho(dono, passos)


def _enderecos_de_conexao() -> list[str]:
    """Enderecos como o cadastro e a tela os aceitam (http ou https, porta de 0 a 65535, sem usuario), com o nome
    escrito de muitos jeitos: nomes, localhost, IPv4 de toda forma que o resolvedor le, IPv6 com e sem IPv4 dentro, e
    os pontos de outros alfabetos (em nomes que o IDNA aceita). Semente fixa."""
    nomes = [
        "cobranca.exemplo.teste",
        "COBRANCA.Exemplo.Teste.",
        "a_b.c",
        "a..b",
        "-a.b",
        "xn--cobrana-zxa.exemplo",
        "localhost",
        "LOCALHOST.",
        "api.localhost",
        "a.b.localhost.",
        "localhost.exemplo",
        "localhostx",
        "127.0.0.1",
        "127.1",
        "127.0.1",
        "2130706433",
        "0x7f000001",
        "0X7F.1",
        "0177.0.0.1",
        "0177.1",
        "127.255.255.255",
        "126.255.255.255",
        "128.0.0.1",
        "0.0.0.0",  # noqa: S104 - um nome no endereco, nao um endereco em que se escuta
        "0",
        "00",
        "0x0",
        "0x",
        "08",
        "010.0.0.1",
        "010.0.0.1.",
        "256.0.0.1",
        "1.2.3.4.5",
        "1.16777215",
        "1.16777216",
        "4294967295",
        "4294967296",
        "10.0.0.5",
        "10.5",
        "167772165",
        "0xa000005",
        "1.256.1",
        "256.1",
        "0x100.1",
        "1.2.256",
        "1.2.65536",
        "0x0000000000000000000a000005",
        "00000000000000000000012",
        "8.0.0.1",
        "cobranca\u3002exemplo\u3002teste",
        "cobranca\uff0eexemplo\uff61teste\u3002",
        "127\u30021",
        "127\uff0e0\uff0e0\uff0e1",
        "010\u30020\u30020\u30021",
        "localhost\uff61",
        "api\u3002localhost",
        "[::1]",
        "[::]",
        "[0:0:0:0:0:0:0:1]",
        "[::FFFF:127.0.0.1]",
        "[::ffff:7f00:2]",
        "[::ffff:10.0.0.5]",
        "[::ffff:a00:5]",
        "[::ffff:0.0.0.0]",
        "[::ffff:01.2.3.4]",
        "[::ffff:1.2.3]",
        "[::10.0.0.5]",
        "[::a00:5]",
        "[::ffff]",
        "[::ffff:0]",
        "[64:ff9b::a00:5]",
        "[64:ff9b::10.0.0.5]",
        "[64:FF9B::7F00:1]",
        "[64:ff9b::]",
        "[0064:ff9b:0:0:0:0:a00:5]",
        "[64:ff9b::1:a00:5]",
        "[64:ff9b:1::a00:5]",
        "[2002:a00:5::1]",
        "[2002:7f00:1::]",
        "[2001:0:4136:e378:8000:63bf:f5ff:fffa]",
        "[2001::f5ff:fffa]",
        "[2001:db8::1]",
        "[2001:DB8:0:0::1]",
        "[1:2:3:4:5:6:1.2.3.4]",
        "[1:2:3:4:5:6:102:304]",
        "[1::2::3]",
        "[00001::1]",
        "[10.0.0.5]",
    ]
    portas = ["", ":", ":80", ":0080", ":443", ":8123", ":0", ":00", ":65535"]
    caminhos = ["/mcp", "/MCP/", "/a/../mcp", "", "/x%41y"]
    sorte = random.Random(20261128)
    enderecos = {f"{e}{n}{p}/mcp" for e in ("http://", "https://") for n in nomes for p in portas}
    # Com usuario (a tela e o cadastro recusam, mas o banco e a segunda barreira): o nome e o que vem depois do ultimo
    # @, nos dois lados.
    enderecos |= {f"http://{u}{n}:8123/mcp" for u in ("u@", "u:s@", "a@b@", "@") for n in nomes[::3]}
    for _ in range(3000):
        if sorte.random() < 0.5:
            nome = "".join(sorte.choice("aZ09xf.-_") for _ in range(sorte.randint(1, 10)))
        else:
            rotulos = ["".join(sorte.choice("az09x") for _ in range(sorte.randint(1, 5))) for _ in range(3)]
            nome = "".join(r + sorte.choice([".", "\u3002", "\uff0e", "\uff61"]) for r in rotulos)[:-1]
        enderecos.add(sorte.choice(("http://", "https://")) + nome + sorte.choice(portas) + sorte.choice(caminhos))
    return sorted(enderecos)


async def test_a_forma_de_conexao_do_banco_junta_os_enderecos_como_a_tela(dono: AsyncEngine) -> None:
    """``selo_endereco_conexao`` e ``declaracao.forma_de_conexao`` dizem o mesmo: o cliente conecta (ou nao) nos
    mesmos enderecos, e dois enderecos chegam ao mesmo lugar num lado so se chegam no outro. O texto do IPv6 pode
    mudar (o banco escreve ``::10.0.0.5``, o Python ``::a00:5``): por isso a conferencia e pelos grupos de enderecos
    que cada lado junta. Fica de fora o nome fora de ASCII (IDNA), que so a tela le."""
    enderecos = _enderecos_de_conexao()
    async with dono.connect() as c:
        banco = (
            (
                await c.execute(
                    text(
                        "SELECT selo_endereco_conexao(e) FROM unnest(CAST(:e AS text[])) WITH ORDINALITY AS x(e, n)"
                        " ORDER BY n"
                    ),
                    {"e": enderecos},
                )
            )
            .scalars()
            .all()
        )
    grupos_banco: dict[tuple[str, ...], set[str]] = {}
    grupos_tela: dict[tuple[str, int, str], set[str]] = {}
    for endereco, forma_banco in zip(enderecos, banco, strict=True):
        forma_tela = d.forma_de_conexao(endereco)
        assert (forma_banco is None) is (forma_tela is None), (endereco, forma_banco, forma_tela)
        if forma_tela is not None:
            assert forma_banco[1:] == [str(forma_tela[1]), forma_tela[2]], endereco
            if ":" in forma_tela[0]:  # o IPv6: o mesmo endereco, talvez escrito de outro jeito
                assert ipaddress.ip_address(forma_banco[0]) == ipaddress.ip_address(forma_tela[0]), endereco
            else:
                assert forma_banco[0] == forma_tela[0], endereco
            grupos_banco.setdefault(tuple(forma_banco), set()).add(endereco)
            grupos_tela.setdefault(forma_tela, set()).add(endereco)
    juntos_banco = {frozenset(g) for g in grupos_banco.values()}
    juntos_tela = {frozenset(g) for g in grupos_tela.values()}
    assert juntos_banco == juntos_tela
    # O corpo tem de tudo: enderecos que o cliente nao le, e grupos grandes (a maquina local, o mesmo IPv4).
    assert sum(f is None for f in banco) > 50
    assert max(len(g) for g in juntos_tela) > 100
    assert len(juntos_tela) > 500


async def test_o_nome_fora_de_ascii_fica_com_a_tela(dono: AsyncEngine) -> None:
    """O que o banco nao calcula: o nome fora de ASCII, que o cliente escreve na forma ``xn--``. O banco o guarda como
    veio, e ele nunca e igual ao nome de um alvo, que e so ASCII; a tela (``alvo_na_producao``) e quem compara."""
    producao, alvo = "https://cobran\u00e7a.exemplo.teste/mcp", "https://xn--cobrana-zxa.exemplo.teste/mcp"
    assert d.forma_de_conexao(producao) == d.forma_de_conexao(alvo)
    async with dono.connect() as c:
        banco = (
            await c.execute(
                text("SELECT selo_endereco_conexao(:p), selo_endereco_conexao(:a)"), {"p": producao, "a": alvo}
            )
        ).one()
    assert banco[0] == ["cobran\u00e7a.exemplo.teste", "443", "/mcp"]
    assert banco[1] == ["xn--cobrana-zxa.exemplo.teste", "443", "/mcp"]


async def test_o_6to4_e_o_teredo_valem_como_ipv6_nos_dois_lados(dono: AsyncEngine) -> None:
    """O residuo escrito (etapa 3, G02rr, retoque): o 6to4 (``2002::/16``) e o Teredo (``2001::/32``) levam um IPv4
    dentro, que a Fase 3 le para saber se o IP e da internet (``telas.admin._ip_publico``), mas aqui valem como IPv6,
    na tela e no banco: o alvo pode ser um deles, e a forma de conexao nao os troca pelo IPv4 de dentro. Se um lado so
    passar a le-los, os testes de concordancia falham; se os dois passarem, este teste muda junto com a lista do que
    fica de fora (``selo_endereco_conexao`` e ``declaracao._ipv6_com_ipv4_dentro``)."""
    seis_para_quatro, teredo = "2002:a00:5::1", "2001:0:4136:e378:8000:63bf:f5ff:fffa"
    assert ipaddress.IPv6Address(seis_para_quatro).sixtofour == ipaddress.IPv4Address("10.0.0.5")
    assert (ipaddress.IPv6Address(teredo).teredo or ())[1:] == (ipaddress.IPv4Address("10.0.0.5"),)
    alvos = [f"http://[{seis_para_quatro}]:8123/mcp", f"http://[{teredo}]:8123/mcp"]
    for alvo in alvos:
        assert _a_tela_aceita(alvo)
        assert not d.alvo_na_producao(alvo, ["http://10.0.0.5:8123/mcp"])
    async with dono.connect() as c:
        assert await _bem_escritos(c, alvos) == [True, True]
        banco = (
            await c.execute(
                text("SELECT selo_endereco_conexao(:a), selo_endereco_conexao(:b), selo_endereco_conexao(:p)"),
                {"a": alvos[0], "b": alvos[1], "p": "http://10.0.0.5:8123/mcp"},
            )
        ).one()
    assert list(banco) == [
        [seis_para_quatro, "8123", "/mcp"],
        [teredo, "8123", "/mcp"],
        ["10.0.0.5", "8123", "/mcp"],
    ]
    assert [d.forma_de_conexao(a) for a in alvos] == [(seis_para_quatro, 8123, "/mcp"), (teredo, 8123, "/mcp")]


@pytest.mark.parametrize(
    "alvo",
    [
        "https://xn--cobrana-gya-{nome}.exemplo.teste/mcp",
        "http://[2001:db8::{n}]:8123/mcp",
        "http://10.{a}.{b}.5:8123/{nome}/mcp",
        "https://ALVO-{nome}.Exemplo.Teste./mcp",
    ],
    ids=["xn", "ipv6", "ipv4_inteiro", "maiusculas_ponto_no_fim"],
)
async def test_o_banco_guarda_o_alvo_escrito_do_jeito_comum(dono: AsyncEngine, cena: Cena, alvo: str) -> None:
    sorteio = uuid.uuid4().int
    alvo = alvo.format(nome=uuid.uuid4().hex[:6], n=format(sorteio % 0xFFFF, "x"), a=sorteio % 256, b=sorteio % 251)
    assert _a_tela_aceita(alvo)
    dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=alvo)

    async def passos(c: AsyncConnection) -> int:
        await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id, dados)
        return await _declaracoes(c, cena.sistema.id)

    assert await num_rascunho(dono, passos) == 1


async def test_o_banco_recusa_o_alvo_longo_antes_de_ler_o_texto(dono: AsyncEngine, cena: Cena) -> None:
    comeco = "https://alvo.exemplo.teste/"
    alvo = comeco + "a" * (d.TETO_ENDERECO - len(comeco))
    longo = alvo + "a"
    assert len(alvo) == d.TETO_ENDERECO

    async def passos(c: AsyncConnection, endereco: str) -> int:
        await apoio.salvar_declaracao(
            c,
            cena.sistema.id,
            cena.admin_id,
            apoio.declaracao(["apagar_lembrete", "enviar_lembrete"], endereco_teste=endereco),
        )
        return await _declaracoes(c, cena.sistema.id)

    assert await num_rascunho(dono, lambda c: passos(c, alvo)) == 1
    with pytest.raises(DBAPIError, match="ate 2048 letras"):
        await num_rascunho(dono, lambda c: passos(c, longo))


async def _com_alvo_estragado(c: AsyncConnection, declaracao_id: uuid.UUID, alvo: str) -> None:
    """Troca o alvo de uma declaracao ja gravada, por fora dos gatilhos (o dono os desliga na transacao): o que um
    defeito em outro lugar poderia deixar no banco. A regra do nome vale de novo no pedido, em rodando e no teste."""
    await c.execute(text("ALTER TABLE declaracoes_selo DISABLE TRIGGER declaracoes_selo_sem_update_delete"))
    await c.execute(
        text(
            "UPDATE declaracoes_selo SET endereco_teste = :a, declaracao = jsonb_set(declaracao, '{endereco_teste}',"
            " to_jsonb(CAST(:a AS text))) WHERE id = :d"
        ),
        {"a": alvo, "d": declaracao_id},
    )
    await c.execute(text("ALTER TABLE declaracoes_selo ENABLE TRIGGER declaracoes_selo_sem_update_delete"))


@pytest.mark.parametrize(
    "estragado",
    ["https://alvo\uff0eexemplo\uff0eteste/mcp", "https://alvo.exemplo.teste:99999999999/mcp"],
    ids=["ponto_de_outro_alfabeto", "porta_longa"],
)
@pytest.mark.parametrize("passo", ["pedido", "rodando", "teste"])
async def test_pedido_rodando_e_teste_conferem_o_nome_do_alvo_de_novo(
    dono: AsyncEngine, cena: Cena, passo: str, estragado: str
) -> None:
    """A regra do nome e da porta vale de novo no pedido, em ``rodando`` e no teste, com a frase do alvo: tambem a
    porta longa (etapa 3, G02rr, retoque), que sem a guarda do tamanho estourava a conta do ``integer``."""

    async def passos(c: AsyncConnection) -> None:
        declaracao_id = await apoio.salvar_declaracao(c, cena.sistema.id, cena.admin_id)
        if passo == "pedido":
            await _com_alvo_estragado(c, declaracao_id, estragado)
            await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, declaracao_id, rodando=False)
            return
        pedido = await apoio.pedir_teste(c, cena.sistema.id, cena.admin_id, declaracao_id, rodando=passo == "teste")
        await _com_alvo_estragado(c, declaracao_id, estragado)
        if passo == "rodando":
            await c.execute(text("UPDATE pedidos_teste_selo SET estado = 'rodando' WHERE id = :p"), {"p": pedido})
            return
        ferramentas = await apoio.acoes_do_sistema(c, cena.sistema.id)
        casos = apoio.casos_que_aprovam(ferramentas, 2)
        await c.execute(
            text(
                "INSERT INTO testes_selo (pedido_id, sistema_id, declaracao_id, contrato, bateria, nivel_pedido,"
                " resultado, nivel_obtido, sistema_endereco, sistema_tipo_chave, revisao_servidor_id, ferramentas,"
                " casos, nao_testado, chamadas, iniciado_em)"
                " SELECT CAST(:p AS uuid), s.id, CAST(:d AS uuid), 'selo/1', '1.0.0', 2, 'aprovado', 2, s.endereco,"
                " s.tipo_chave, s.revisao_servidor_id, CAST(:f AS uuid[]), CAST(:casos AS jsonb), CAST(:nt AS jsonb),"
                " CAST(:ch AS integer), now() FROM sistemas s WHERE s.id = :s"
            ),
            {
                "p": pedido,
                "s": cena.sistema.id,
                "d": declaracao_id,
                "f": ferramentas,
                "casos": json.dumps(casos),
                "nt": json.dumps(contrato.nao_testado_gravado()),
                "ch": contrato.contagem_minima_de_chamadas(casos),
            },
        )

    with pytest.raises(DBAPIError, match="escrito de um jeito so"):
        await num_rascunho(dono, passos)


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


FORMAS_DE_FUNDO: Final = ("listas", "objetos", "misto")


def _aninhado(forma: str, niveis: int) -> Any:
    valor: Any = "x"
    for i in range(niveis):
        valor = [valor] if forma == "listas" or (forma == "misto" and i % 2) else {"k": valor}
    return valor


def _texto_aninhado(forma: str, niveis: int) -> str:
    abre, fecha = [], []
    for i in reversed(range(niveis)):
        lista = forma == "listas" or (forma == "misto" and i % 2)
        abre.append("[" if lista else '{"k":')
        fecha.append("]" if lista else "}")
    return "".join(abre) + '"x"' + "".join(reversed(fecha))


@pytest.mark.parametrize("forma", FORMAS_DE_FUNDO)
async def test_niveis_ate_o_teto_sao_gravados_e_lidos_de_volta(dono: AsyncEngine, cena: Cena, forma: str) -> None:
    """O que ``validar`` aceita o banco guarda e ``modelo()`` le de volta, com listas, objetos ou os dois; acima do
    teto (65, 300 e 3000 niveis), ``DeclaracaoRecusada`` antes da gravacao, pelo ``validar`` e pelo ``ler_texto`` (sem
    ela, ``salvar`` levantava ``ValueError`` do pydantic, ao serializar; a mutacao W01 tirava o teto dos objetos)."""
    marca = "MARCA-DO-FUNDO"

    def com_fundo(niveis: int) -> dict[str, Any]:
        dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"])
        dados["ferramentas"]["enviar_lembrete"]["argumentos"]["texto"] = _aninhado(forma, niveis - 4)
        return dados

    def texto_com_fundo(niveis: int) -> str:
        dados = apoio.declaracao(["apagar_lembrete", "enviar_lembrete"])
        dados["ferramentas"]["enviar_lembrete"]["argumentos"]["texto"] = marca
        return json.dumps(dados).replace(json.dumps(marca), _texto_aninhado(forma, niveis - 4))

    async def passos(c: AsyncConnection) -> tuple[d.DeclaracaoSalva | None, d.Declaracao, str]:
        await como(c, ENTRADA)
        no_teto = d.ler_texto(texto_com_fundo(d.FUNDO_MAXIMO))
        assert d.validar(com_fundo(d.FUNDO_MAXIMO)) == no_teto
        d.conferir(no_teto, await d.cadastro_do_sistema(c, cena.sistema.id), ambiente=Ambiente.PRODUCAO)
        await d.salvar(c, sistema_id=cena.sistema.id, pessoa_id=cena.admin_id, salva_por="a", declaracao=no_teto)
        for niveis in (d.FUNDO_MAXIMO + 1, 300, 3000):
            for ler in (lambda n=niveis: d.validar(com_fundo(n)), lambda n=niveis: d.ler_texto(texto_com_fundo(n))):
                with pytest.raises(d.DeclaracaoRecusada) as erro:
                    ler()
                assert (erro.value.motivo, erro.value.mensagem) == ("json_invalido", d.FUNDO_DEMAIS)
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
