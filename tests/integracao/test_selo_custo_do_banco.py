"""Custo das funcoes da 0007 que andam por JSON vindo do sistema (retoque G02r), contra PostgreSQL 18.7 real.

- A forma essencial do esquema tem orcamento de nos, com os mesmos numeros no codigo e no banco
  (``contrato.TETO_NOS_DO_ESQUEMA`` e ``selo_esquema_teto``): o custo de cada esquema e o mesmo nos dois lados, um
  esquema no teto passa e um no acima dele vira ``grande_demais``, e os esquemas que explodem (o ciclo de 129 bytes, a
  cadeia de 32 definicoes) ou que sao grandes demais respondem em menos de 1 s.
- O peso de um valor JSON (``selo_esquema_peso``) e o do codigo (``contrato.peso_do_json``), e o corte pelo tamanho
  guardado, que evita abrir um valor grande, nunca corta um valor que caberia (o tamanho do jsonb fica abaixo de 67
  bytes por no, mais 8).
- A conferencia do contrato em ``ferramentas_vistas`` (``contrato_confere``) e calculada ao gravar a definicao, e
  ninguem a escreve: ``selo_vigente()`` nao le mais a definicao guardada a cada leitura.
- ``selo_endereco_normalizado`` tem custo linear (antes, 32 mil letras levavam 4 s), como ``selo_endereco_conexao``; e
  ``depois`` tem teto antes do laco do gatilho.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.selo import contrato, declaracao
from tests import apoio_selos as apoio
from tests.integracao.test_migracao_0006 import DESPACHANTE, como, dono
from tests.integracao.test_migracao_0007 import CORPO_ESQUEMAS, _esquemas_do_kit, _fundo
from tests.unidade.test_selo_contrato import CICLO_QUE_EXPLODE, _kit, cadeia_que_explode, no_teto

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["dono"]  # fixture

TEMPO_MAXIMO: Final = 1.0
"""Segundos que uma chamada pode levar com o esquema mais hostil (o pedido do coordenador)."""


async def num_rascunho(engine: AsyncEngine, passos: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async with engine.connect() as c:
        transacao = await c.begin()
        try:
            return await passos(c)
        finally:
            await transacao.rollback()


async def _essencial(c: AsyncConnection, esquema: Any) -> tuple[Any, int]:
    """A forma essencial do banco e o gasto do orcamento (acima do teto quando passou)."""
    linha = (
        await c.execute(
            text(
                "SELECT selo_esquema_essencial(CAST(:e AS jsonb)) AS forma,"
                " (selo_esquema_no(CAST(:e AS jsonb), CAST(:e AS jsonb), 0, 0)).gasto AS gasto"
            ),
            {"e": json.dumps(esquema)},
        )
    ).one()
    return linha.forma, int(linha.gasto)


# --- Os numeros do orcamento ----------------------------------------------------------------------------------


async def test_tetos_do_banco_sao_os_do_codigo(dono: AsyncEngine) -> None:
    async with dono.connect() as c:
        tetos = {
            nome: (await c.execute(text("SELECT selo_esquema_teto(:n)"), {"n": nome})).scalar_one()
            for nome in ("nos", "bytes_por_no", "fundo")
        }
    assert tetos == {
        "nos": contrato.TETO_NOS_DO_ESQUEMA,
        "bytes_por_no": contrato.BYTES_POR_NO,
        "fundo": contrato.FUNDO_MAXIMO_DO_ESQUEMA,
    }


def _esquema_sorteado(sorte: random.Random, fundo: int = 0) -> Any:
    """Um esquema qualquer, com referencias, anotacoes, listas de textos e palavras de fora do contrato."""
    if fundo > 4 or sorte.random() < 0.2:
        return sorte.choice([{"type": "string"}, {"$ref": "#/$defs/d"}, {"enum": ["b", "a"]}, True, "t", 3])
    no: dict[str, Any] = {"type": sorte.choice(["object", "array", "string"])}
    if sorte.random() < 0.6:
        no["properties"] = {f"p{i}": _esquema_sorteado(sorte, fundo + 1) for i in range(sorte.randint(0, 3))}
    if sorte.random() < 0.3:
        no["items"] = _esquema_sorteado(sorte, fundo + 1)
    if sorte.random() < 0.3:
        no["required"] = sorte.sample(["c", "b", "a"], sorte.randint(0, 3))
    if sorte.random() < 0.3:
        no["description"] = "d" * sorte.choice([0, 63, 64, 200])
    if sorte.random() < 0.2:
        no["anyOf"] = [{"minimum": sorte.random()}]
    if sorte.random() < 0.2:
        no["$ref"] = "#/$defs/d"
    return no


def _corpo_do_orcamento() -> list[Any]:
    sorte = random.Random(20261127)
    sorteados = []
    for _ in range(60):
        esquema = _esquema_sorteado(sorte)
        if isinstance(esquema, dict):
            esquema["$defs"] = {"d": _esquema_sorteado(sorte, 3)}
        sorteados.append(esquema)
    base = contrato.ESQUEMAS["selo_consultar"]["outputSchema"]
    return [
        *CORPO_ESQUEMAS,
        *_esquemas_do_kit(),
        *[d[campo] for d in _kit().values() for campo in ("inputSchema", "outputSchema")],
        _fundo(31),
        _fundo(40),
        CICLO_QUE_EXPLODE,
        cadeia_que_explode(),
        no_teto(base),
        no_teto(base, a_mais=1),
        *sorteados,
    ]


async def test_custo_e_forma_do_banco_sao_os_do_codigo(dono: AsyncEngine) -> None:
    """O mesmo custo, no; a no, e a mesma forma, nos dois lados, inclusive no teto, um no acima dele e nos esquemas que
    explodem. O banco anda nas chaves em outra ordem (jsonb), entao o custo nao depende da ordem."""
    corpo = _corpo_do_orcamento()
    passaram = 0
    async with dono.connect() as c:
        for esquema in corpo:
            forma, gasto = await _essencial(c, esquema)
            custo = contrato.custo_da_forma_essencial(esquema)
            assert forma == contrato.forma_essencial(esquema), esquema
            if custo is None:
                assert gasto > contrato.TETO_NOS_DO_ESQUEMA, esquema
                assert forma == contrato.GRANDE_DEMAIS
            else:
                assert gasto == custo, esquema
                passaram += 1
    assert 0 < passaram < len(corpo)


async def test_no_teto_passa_e_um_no_depois_nao_no_banco(dono: AsyncEngine) -> None:
    base = contrato.ESQUEMAS["selo_consultar"]["outputSchema"]
    sql = (
        "SELECT selo_esquema_essencial(CAST(:e AS jsonb))"
        " = selo_esquema_essencial(selo_esquemas('selo_consultar') -> 'outputSchema')"
    )
    async with dono.connect() as c:
        assert (await _essencial(c, no_teto(base)))[1] == contrato.TETO_NOS_DO_ESQUEMA
        assert (await c.execute(text(sql), {"e": json.dumps(no_teto(base))})).scalar_one() is True
        assert (await _essencial(c, no_teto(base, a_mais=1)))[0] == contrato.GRANDE_DEMAIS
        assert (await c.execute(text(sql), {"e": json.dumps(no_teto(base, a_mais=1))})).scalar_one() is False


async def _chamadas(c: AsyncConnection, funcao: str) -> int:
    """Quantas vezes ``funcao`` rodou nesta transacao (com ``track_functions = 'all'``)."""
    return int(
        (
            await c.execute(
                text("SELECT coalesce(sum(calls), 0) FROM pg_stat_xact_user_functions WHERE funcname = :f"),
                {"f": funcao},
            )
        ).scalar_one()
    )


# --- O peso e o corte ------------------------------------------------------------------------------------------


def _json_sorteado(sorte: random.Random, fundo: int = 0) -> Any:
    escolha = sorte.random()
    if fundo > 3 or escolha < 0.45:
        return sorte.choice(
            [
                None,
                True,
                False,
                0,
                -7,
                int("9" * sorte.randint(1, 300)),
                sorte.uniform(-1e300, 1e300),
                5e-324,
                1e16,
                -0.0,
                "",
                "a" * sorte.choice([1, 63, 64, 65, 127, 128, 1000]),
                "ç" * sorte.choice([31, 32, 33]),
                "\U0001f600" * sorte.choice([15, 16, 17]),
                "。．",
            ]
        )
    if escolha < 0.7:
        return [_json_sorteado(sorte, fundo + 1) for _ in range(sorte.randint(0, 6))]
    # Nomes de campo longos em letras de 2 e de 4 bytes: o peso conta bytes, e nao letras (32 a 40 "ç" sao 64 a 80
    # bytes; 16 emojis, 64).
    nomes = ["k", "ç", "x" * 64, "a" * 63, "ç" * sorte.randint(32, 40), "\U0001f600" * sorte.choice([15, 16, 17])]
    return {sorte.choice(nomes) + str(i): _json_sorteado(sorte, fundo + 1) for i in range(sorte.randint(0, 6))}


async def test_peso_do_banco_e_o_do_codigo_e_o_corte_e_seguro(dono: AsyncEngine) -> None:
    """``selo_esquema_peso`` da o peso do codigo; com o que resta abaixo do peso, devolve um numero acima do que resta
    (pelo laco ou pelo corte); e o corte pelo tamanho guardado e seguro: nenhum valor ocupa mais de 67 bytes por no
    no jsonb, mais 8, entao um valor maior que 68 por no que resta nunca caberia."""
    sorte = random.Random(7)
    valores = [_json_sorteado(sorte) for _ in range(300)]
    valores += [{"x" * 63: "y" * 63}, ["\U0001f600" * 15] * 50, [int("9" * 4000)] * 3, [[[[[]]]]]]
    valores += [{"ç" * 32: 1, "ç" * 40: [], "\U0001f600" * 16: {"\U0001f600" * 32: None}}]
    async with dono.connect() as c:
        for valor in valores:
            texto = json.dumps(valor)
            peso = contrato.peso_do_json(valor)
            linha = (
                await c.execute(
                    text(
                        "SELECT selo_esquema_peso(CAST(:v AS jsonb), 1000000000) AS todo,"
                        " selo_esquema_peso(CAST(:v AS jsonb), :p) AS no_peso,"
                        " selo_esquema_peso(CAST(:v AS jsonb), :p - 1) AS abaixo,"
                        " pg_column_size(CAST(:v AS jsonb)) AS guardado"
                    ),
                    {"v": texto, "p": peso},
                )
            ).one()
            assert linha.todo == linha.no_peso == peso, valor
            assert linha.abaixo > peso - 1, valor
            assert linha.guardado <= 67 * peso + 8, valor


@pytest.mark.parametrize("forma", ["listas", "objetos"])
@pytest.mark.parametrize(
    ("niveis", "bytes_no_fundo"), [(1, 2 * 1024 * 1024), (100, 3 * 1024 * 1024), (1000, 1024 * 1024)]
)
async def test_corte_pelo_tamanho_nao_abre_o_valor_grande(
    dono: AsyncEngine, forma: str, niveis: int, bytes_no_fundo: int
) -> None:
    """Um valor grande e cortado sem ser aberto, numa lista ou num objeto: o peso volta logo, ja na raiz, com o que
    resta mais 1 (o sinal do corte: aberto, o valor pesaria o texto do fundo inteiro, mais de 16 mil). Sem o corte,
    cada nivel de uma lista ou de um objeto dentro do outro copiava o resto inteiro: com 1000 objetos e 1 MiB no fundo,
    a forma essencial passava de uns 4 ms para uns 600 ms (retoque 2 do G02r: o corte dos objetos nao tinha teste).
    O texto JSON e escrito a mao, sem o teto de niveis do ``json.dumps``."""
    abre, fecha = ("[", "]") if forma == "listas" else ('{"k":', "}")
    texto = abre * niveis + json.dumps("d" * bytes_no_fundo) + fecha * niveis

    # O peso inteiro (1 por lista; 2 por objeto, com o nome "k"; o texto do fundo, 1 a cada 64 bytes): aberto ate o
    # fundo, o laco devolveria isso, e nao o que resta mais 1.
    inteiro = niveis * (1 if forma == "listas" else 2) + 1 + bytes_no_fundo // 64
    assert inteiro > 16_000

    async def medir(c: AsyncConnection) -> tuple[int, float]:
        await c.execute(text("CREATE TEMP TABLE grande (v jsonb) ON COMMIT DROP"))
        await c.execute(text("INSERT INTO grande VALUES (CAST(:v AS jsonb))"), {"v": texto})
        inicio = time.monotonic()
        peso = (await c.execute(text("SELECT selo_esquema_peso(v, 2000) FROM grande"))).scalar_one()
        return int(peso), time.monotonic() - inicio

    peso, levou = await num_rascunho(dono, medir)
    assert peso == 2001
    assert levou < 0.1


# --- Esquemas hostis -----------------------------------------------------------------------------------------


def _profundo() -> Any:
    lista: Any = "x"
    for _ in range(150):
        lista = [lista]
    return {"items": {"default": lista, "description": "d" * 1_000_000}}


def _itens(niveis: int) -> Any:
    no: Any = {"type": "string"}
    for _ in range(niveis):
        no = {"items": no}
    return no


HOSTIS: Final[dict[str, Callable[[], Any]]] = {
    "ciclo": lambda: CICLO_QUE_EXPLODE,
    "cadeia": cadeia_que_explode,
    "definicao_gorda": lambda: {
        "$defs": {
            "a": {
                "description": "d" * 500_000,
                "type": "object",
                "properties": {"x": {"$ref": "#/$defs/a"}, "y": {"$ref": "#/$defs/a"}},
            },
            "lixo": {"z": list(range(50_000))},
        },
        "$ref": "#/$defs/a",
    },
    "largo": lambda: {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(50_000)}},
    "escolhas": lambda: {"enum": ["v"] * 200_000},
    "numeros": lambda: {
        "$defs": {
            "a": {
                "examples": [[int("9" * 4000)] * 3] * 100,
                "properties": {"x": {"$ref": "#/$defs/a"}, "y": {"$ref": "#/$defs/a"}},
            }
        },
        "$ref": "#/$defs/a",
    },
    "profundo": _profundo,
    "itens60": lambda: _itens(60),
}
"""Esquemas que explodem (ciclo, cadeia) ou que sao grandes, largos ou fundos demais, um por vez."""


@pytest.mark.parametrize("nome", list(HOSTIS))
async def test_esquema_hostil_responde_depressa_no_banco_e_no_codigo(dono: AsyncEngine, nome: str) -> None:
    """Cada esquema que explode ou que e grande demais vira ``grande_demais`` em menos de 1 s nos dois lados, com um
    ``statement_timeout`` curto de garantia (sem o orcamento, o ciclo e a cadeia nao terminavam)."""
    esquema = HOSTIS[nome]()
    inicio = time.monotonic()
    assert contrato.forma_essencial(esquema) == contrato.GRANDE_DEMAIS
    assert time.monotonic() - inicio < TEMPO_MAXIMO

    async def medir(c: AsyncConnection) -> tuple[Any, float]:
        await c.execute(text("SET LOCAL statement_timeout = '5s'"))
        await c.execute(text("CREATE TEMP TABLE hostil (v jsonb) ON COMMIT DROP"))
        await c.execute(text("INSERT INTO hostil VALUES (CAST(:v AS jsonb))"), {"v": json.dumps(esquema)})
        inicio = time.monotonic()
        forma = (await c.execute(text("SELECT selo_esquema_essencial(v) FROM hostil"))).scalar_one()
        return forma, time.monotonic() - inicio

    forma, levou = await num_rascunho(dono, medir)
    assert forma == contrato.GRANDE_DEMAIS
    assert levou < TEMPO_MAXIMO


# --- contrato_confere, calculado ao gravar -------------------------------------------------------------------


async def test_contrato_confere_e_calculado_ao_gravar_e_ninguem_o_escreve(dono: AsyncEngine) -> None:
    async def passos(c: AsyncConnection) -> dict[str, Any]:
        sistema = await apoio.proprio_com_acoes(c, nivel_do_contrato=None)
        kit = _kit()
        explode = dict(apoio.definicao_do_contrato(contrato.FERRAMENTA_CONSULTAR), inputSchema=CICLO_QUE_EXPLODE)
        await apoio.listar_contrato(
            c,
            sistema.id,
            3,
            definicoes={contrato.FERRAMENTA_ALVO: kit["selo_alvo"], contrato.FERRAMENTA_CONSULTAR: explode},
        )
        await c.execute(
            text(
                "INSERT INTO ferramentas_vistas (sistema_id, nome, definicao, impressao)"
                " VALUES (:s, 'outra', CAST(:d AS jsonb), 'sha256:0')"
            ),
            {"s": sistema.id, "d": json.dumps(apoio.definicao_do_contrato(contrato.FERRAMENTA_ALVO))},
        )
        antes = {
            str(n): v
            for n, v in await c.execute(
                text("SELECT nome, contrato_confere FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": sistema.id}
            )
        }
        # A definicao corrigida (o despachante a grava de novo) recalcula a coluna.
        await c.execute(
            text("UPDATE ferramentas_vistas SET definicao = CAST(:d AS jsonb) WHERE sistema_id = :s AND nome = :n"),
            {
                "s": sistema.id,
                "n": contrato.FERRAMENTA_CONSULTAR,
                "d": json.dumps(kit[contrato.FERRAMENTA_CONSULTAR]),
            },
        )
        depois = (
            await c.execute(
                text("SELECT contrato_confere FROM ferramentas_vistas WHERE sistema_id = :s AND nome = :n"),
                {"s": sistema.id, "n": contrato.FERRAMENTA_CONSULTAR},
            )
        ).scalar_one()
        return {"antes": antes, "depois": depois, "sistema": sistema.id}

    resultado = await num_rascunho(dono, passos)
    assert resultado["antes"] == {"selo_alvo": True, "selo_consultar": False, "selo_bloqueio": True, "outra": None}
    assert resultado["depois"] is True
    for papel in (None, DESPACHANTE):

        async def forjar(c: AsyncConnection, papel: str | None = papel) -> None:
            sistema = await apoio.proprio_com_acoes(c)
            await como(c, papel)
            await c.execute(
                text("UPDATE ferramentas_vistas SET contrato_confere = true WHERE sistema_id = :s"), {"s": sistema.id}
            )

        with pytest.raises(DBAPIError, match="can only be updated to DEFAULT"):
            await num_rascunho(dono, forjar)


async def test_selo_vigente_nao_le_a_definicao_guardada(dono: AsyncEngine) -> None:
    """Uma definicao de 2 MiB de selo_consultar (descricao enorme e esquema que explode) custa uma vez, ao gravar, e
    selo_vigente() continua rapido: o selo cai em contrato_sumiu, sem ler a definicao de novo."""

    async def passos(c: AsyncConnection) -> tuple[float, float, Any, int]:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(c, nivel_do_contrato=2)
        await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=admin)
        enorme = dict(
            apoio.definicao_do_contrato(contrato.FERRAMENTA_CONSULTAR),
            description="d" * (2 * 1024 * 1024),
            inputSchema=CICLO_QUE_EXPLODE,
        )
        await c.execute(text("SET LOCAL statement_timeout = '5s'"))
        await c.execute(text("SET LOCAL track_functions = 'all'"))
        inicio = time.monotonic()
        await apoio.listar_contrato(c, sistema.id, 2, definicoes={contrato.FERRAMENTA_CONSULTAR: enorme})
        gravar = time.monotonic() - inicio
        antes = await _chamadas(c, "selo_esquema_essencial")
        inicio = time.monotonic()
        for _ in range(20):
            selo = await apoio.selo_vigente(c, sistema.id)
        ler = (time.monotonic() - inicio) / 20
        return gravar, ler, selo, await _chamadas(c, "selo_esquema_essencial") - antes

    gravar, ler, selo, conferencias = await num_rascunho(dono, passos)
    assert (selo.nivel, selo.motivo) == (1, "contrato_sumiu")
    assert gravar < TEMPO_MAXIMO
    assert ler < 0.05
    assert conferencias == 0  # a leitura do selo nao confere o esquema de novo: le contrato_confere


# --- Endereco e depois ----------------------------------------------------------------------------------------


async def test_endereco_normalizado_tem_custo_linear(dono: AsyncEngine) -> None:
    """64 mil letras no caminho, com %XX e "..", respondem em menos de 1 s e dao o mesmo que o codigo (antes, com
    substr letra por letra, 32 mil letras levavam 4 s)."""
    endereco = "HTTPS://Alvo.Exemplo.Teste:443/" + "a/%41/../b%2f" * 5000 + "/" + "c" * 1000
    async with dono.connect() as c:
        inicio = time.monotonic()
        banco = (await c.execute(text("SELECT selo_endereco_normalizado(:e)"), {"e": endereco})).scalar_one()
        levou = time.monotonic() - inicio
    assert banco == contrato.endereco_normalizado(endereco)
    assert len(endereco) > 64_000
    assert levou < TEMPO_MAXIMO


async def test_alvo_na_producao_calcula_as_formas_do_alvo_uma_vez(dono: AsyncEngine) -> None:
    """``selo_alvo_na_producao`` calcula as duas formas (normalizada e de conexao) da producao de cada sistema uma vez
    e as do alvo uma vez so, tambem no plano generico (que o banco usa depois de algumas chamadas da mesma funcao): sem
    as subconsultas, o alvo era calculado de novo a cada sistema do cadastro. A forma de conexao usa o caminho
    normalizado: cada uma chama ``selo_endereco_normalizado`` mais uma vez."""

    async def contar(c: AsyncConnection) -> tuple[int, int, int, bool]:
        for _ in range(3):
            await apoio.proprio_com_acoes(c, nivel_do_contrato=None)
        sistemas = int((await c.execute(text("SELECT count(*) FROM sistemas"))).scalar_one())
        await c.execute(text("SET LOCAL track_functions = 'all'"))
        await c.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
        na_producao = (
            await c.execute(text("SELECT selo_alvo_na_producao('https://alvo.exemplo.teste/' || repeat('a', 2000))"))
        ).scalar_one()
        normalizado = await _chamadas(c, "selo_endereco_normalizado")
        return sistemas, normalizado, await _chamadas(c, "selo_endereco_conexao"), bool(na_producao)

    sistemas, normalizado, conexao, na_producao = await num_rascunho(dono, contar)
    assert not na_producao
    assert conexao == sistemas + 1
    assert normalizado == 2 * (sistemas + 1)


async def test_forma_de_conexao_tem_custo_linear(dono: AsyncEngine) -> None:
    """O alvo mais longo (2048 letras, com o nome no teto do que o banco aceita) e uma producao de 64 mil letras
    respondem em menos de 1 s, com a forma da tela."""
    alvo = "https://" + "a." * 500 + "exemplo.teste/" + "b/%41/../" * 100
    alvo = alvo + "c" * (2048 - len(alvo))
    producao = "http://[::ffff:127.0.0.1]:8123/" + "a/%41/../b%2f" * 5000
    async with dono.connect() as c:
        inicio = time.monotonic()
        banco = (
            await c.execute(
                text("SELECT selo_endereco_conexao(:a), selo_endereco_conexao(:p)"), {"a": alvo, "p": producao}
            )
        ).one()
        levou = time.monotonic() - inicio
    assert len(alvo) == 2048
    assert levou < TEMPO_MAXIMO
    tela = [declaracao.forma_de_conexao(e) for e in (alvo, producao)]
    assert [list(b) for b in banco] == [[f[0], str(f[1]), f[2]] for f in tela if f is not None]


async def test_depois_tem_teto_antes_do_laco_do_gatilho(dono: AsyncEngine) -> None:
    """O gatilho de informacoes_sistema confere ``depois`` evento por evento; os tetos (20 eventos, 16 KiB) valem antes
    do laco, e nao so no CHECK, que roda depois do gatilho."""

    async def com_depois(c: AsyncConnection, depois: list[Any]) -> None:
        admin = await apoio.nova_pessoa(c, admin=True)
        sistema = await apoio.proprio_com_acoes(c)
        await apoio.gravar_teste(c, sistema.id, nivel=3, admin_id=admin)
        op = await apoio.criar_operacao(c, sistema.acoes[0])
        await apoio.enviar(c, op.id)
        await apoio.informar(c, op.id, "concluida", depois=depois)

    evento = {"evento": "entregue", "em": datetime.now(UTC).isoformat()}
    await num_rascunho(dono, lambda c: com_depois(c, [evento] * 20))
    with pytest.raises(DBAPIError, match=r"lista de eventos \(ate 20, com ate 16 KiB\)"):
        await num_rascunho(dono, lambda c: com_depois(c, [evento] * 21))
    # 20 eventos que, somados, passam de 16 KiB: recusados pelo tamanho antes de o laco abrir o primeiro.
    with pytest.raises(DBAPIError, match=r"lista de eventos \(ate 20, com ate 16 KiB\)"):
        await num_rascunho(dono, lambda c: com_depois(c, [{"evento": "entregue", "em": "x" * 1000}] * 20))


def test_os_corpos_sorteados_tem_semente_fixa() -> None:
    """Os corpos sorteados usam sementes fixas: o teste da o mesmo resultado em toda maquina."""
    assert _corpo_do_orcamento() == _corpo_do_orcamento()
