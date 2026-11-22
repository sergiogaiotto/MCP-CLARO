"""Provas de saida da Fase 3 (desenho 6.2, G9a), uma por linha da tabela, pelo gateway inteiro.

Arranjo de ``tests/apoio_g9.py``: entrada de verdade (cliente do kit com o cracha), despachante de verdade com a
sala de visitas de verdade (um processo filho por chamada, mTLS), banco com os usuarios de cada papel, telas do
ADMIN e da pessoa, e servidores de teste (os de terceiros atras de um gravador de bytes, ``tests/cortador.py`` em
modo ``inteiro``). A chave da Fase 3 fica ligada pelo construtor de ``Parametros``, sem conferir: a partida ainda a
recusa (D6). O ADMIN faz o percurso pelas telas; as rotinas conferem pelo despachante, como em producao.

1. Cada servidor de terceiros revisado e registrado: invariante sobre o banco e sobre a vitrine que o aplicativo
   ve, depois de cada passo de um percurso inteiro, e o painel ``medicoes.v_terceiros`` mostrando o mesmo.
2. Sala isolada (I2): o terceiro recebe so o pedido do teste (cada byte gravado), sem chave alheia, sem cracha,
   sem rastro e sem resultado de outro sistema.
3. Cracha nao repassado e chave por pessoa, refeitos com terceiro.
4. Mudanca so de texto leva a quarentena, pela sala.
5. Tres sinais suspendem o limite e a ficha mostra o motivo.
6. Pedido de senha vindo do servidor nao chega a pessoa (i8), e o texto comum com senha e link tambem nao (A4).
7. D31: a resposta de terceiro chega inteira dentro da marca, com erro e dados estruturados, e nada passa fora.

No fim, uma guarda das fontes destas provas: os caracteres escondidos que elas mandam ficam escritos como escape.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from exemplos import acoes_caixa, chave_compartilhada, leitura_de_programa, leitura_por_pessoa, mal_comportado
from exemplos import terceiro_provas as tp
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.quarentena import MOTIVO_QUARENTENA
from mcp_claro.config.politica import POLITICA
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.regras import estranhos, politica, sinais
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import CONSULTA
from mcp_claro.saida.protocolo import FUSO
from mcp_claro.telas import acoes
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests import apoio_g9 as g9
from tests.apoio_g9 import Aplicativo, Gateway, Pessoa, Servido

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MOTIVO = PREFIXO_META + "motivo"
AVISO_A4 = "O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos."


@pytest.fixture
async def gw(banco_migrado: Parametros, tmp_path: Path) -> AsyncIterator[Gateway]:
    async with g9.subir_gateway(banco_migrado, tmp_path / "certificados") as gateway:
        yield gateway


def _chave(nome: str) -> str:
    return f"k-{nome}-{uuid.uuid4().hex}"


def _chaves(monkeypatch: pytest.MonkeyPatch, mapa: dict[str, str]) -> None:
    """As chaves de teste que o proximo exemplo criado aceita (``EXEMPLO_CHAVES`` e lido na criacao)."""
    monkeypatch.setenv("EXEMPLO_CHAVES", json.dumps(mapa))


async def _operacao(gw: Gateway, protocolo: str) -> fila.Operacao:
    async with gw.dono.connect() as c:
        i = (await c.execute(text("SELECT id FROM operacoes WHERE protocolo = :p"), {"p": protocolo})).scalar_one()
        op = await fila.por_id(c, i)
    assert op is not None
    return op


_LINHA_DE_PEDIDO = re.compile(rb"^[A-Z]+ /\S* HTTP/1\.1\r$", re.MULTILINE)


def _pedidos(servido: Servido) -> int:
    """Quantos pedidos HTTP chegaram ao servidor, em todas as conexoes (contando as linhas de pedido)."""
    return sum(len(_LINHA_DE_PEDIDO.findall(c)) for c in servido.conexoes())


def _posts_de_chamada(servido: Servido) -> list[bytes]:
    """Os POSTs de ``tools/call`` que chegaram ao terceiro. Cada filho da sala abre a propria conexao: cada uma
    leva um pedido so (conferido aqui, para que contar conexoes seja contar pedidos)."""
    for c in servido.conexoes():
        assert len(_LINHA_DE_PEDIDO.findall(c)) == 1, c[:400]
    return [c for c in servido.conexoes() if c.startswith(b"POST ") and b'"tools/call"' in c]


# --- 1. Cada servidor de terceiros revisado e registrado ----------------------------------------------------------


async def _v_terceiros(gw: Gateway) -> Counter[tuple[str, bool]]:
    """O painel, lido como o Grafana le (usuario ``grafana_leitor``)."""
    async with gw.grafana_db.connect() as c:
        assert (await c.execute(text("SELECT current_user"))).scalar_one() == "grafana_leitor"
        linhas = (await c.execute(text("SELECT situacao, revisado, sistemas FROM medicoes.v_terceiros"))).all()
    return Counter({(str(s), bool(r)): int(n) for s, r, n in linhas})


async def _conferir_revisada_na_vitrine(gw: Gateway, ferramenta_id: uuid.UUID, *, nossos: set[uuid.UUID]) -> uuid.UUID:
    """Uma ferramenta de terceiro na vitrine: o servidor esta ativo, tem revisao vigente registrada (impressao,
    fornecedor, quem revisou) que publica esta ferramenta e lista a definicao dela entre as declaradas. Nos
    terceiros deste teste (``nossos``), revisados pelas telas, a revisao vigente tem tambem a linha
    ``admin_servidor_revisado`` no extrato (os de outros testes da sessao podem ter sido gravados direto no banco,
    sem o extrato). Devolve o id do sistema."""
    async with gw.dono.connect() as c:
        linha = (
            await c.execute(
                text(
                    "SELECT f.id, f.nome_original, f.impressao, f.situacao AS f_situacao, s.id AS sistema_id,"
                    " s.situacao, s.revisao_servidor_id, rs.id AS rs_id, rs.sistema_id AS rs_sistema,"
                    " rs.publicadas, rs.declaradas, rs.impressao AS rs_impressao, rs.fornecedor, rs.revisado_por"
                    " FROM ferramentas_revisadas f JOIN sistemas s ON s.id = f.sistema_id"
                    " LEFT JOIN revisoes_servidor rs ON rs.id = s.revisao_servidor_id WHERE f.id = :f"
                ),
                {"f": ferramenta_id},
            )
        ).one()
        registrada = (
            await c.execute(
                text(
                    "SELECT count(*) FROM extrato WHERE sistema_id = :s AND tipo = 'admin_servidor_revisado'"
                    " AND dados ->> 'revisao' = :r"
                ),
                {"s": linha.sistema_id, "r": str(linha.rs_id)},
            )
        ).scalar_one()
    assert linha.situacao == "ativo", linha
    assert linha.f_situacao == "ativa", linha
    assert linha.rs_id is not None, f"terceiro na vitrine sem revisao do servidor vigente: {linha}"
    assert linha.rs_sistema == linha.sistema_id
    assert linha.id in linha.publicadas, f"ferramenta na vitrine fora das publicadas da revisao vigente: {linha}"
    assert {"nome": linha.nome_original, "impressao": linha.impressao} in linha.declaradas
    assert linha.fornecedor
    assert linha.revisado_por
    if linha.sistema_id in nossos:
        assert str(linha.rs_impressao).startswith("sha256:")
        assert registrada == 1, "revisao vigente sem a linha admin_servidor_revisado no extrato"
    return uuid.UUID(str(linha.sistema_id))


async def _invariante(
    gw: Gateway,
    pares: Iterable[tuple[Pessoa, Aplicativo]],
    base: Counter[tuple[str, bool]],
    nosso: tuple[str, bool] | None,
    nossos: set[uuid.UUID],
) -> dict[tuple[str, str], list[str]]:
    """O invariante, de tres lados. (a) O que o aplicativo ve pela entrada: cada ferramenta de terceiro na lista
    tem a revisao do servidor vigente que a contem. (b) O banco inteiro: toda ordem ativa de qualquer pessoa, com a
    vitrine montada pelo usuario da entrada com terceiros, obedece a mesma regra. (c) O painel: cada terceiro com
    ferramenta numa vitrine conta como ativo e revisado, e o terceiro deste teste mudou o painel so na linha
    esperada (``nosso``) em relacao ao comeco (``base``). Devolve as listas do aplicativo por pessoa."""
    vistas: dict[tuple[str, str], list[str]] = {}
    for pessoa, app in pares:
        nomes = await gw.listar(pessoa, app)
        vistas[(pessoa.apelido, app.client_id)] = nomes
        for nome in nomes:
            async with gw.dono.connect() as c:
                f = (
                    await c.execute(
                        text(
                            "SELECT f.id, s.origem FROM ferramentas_revisadas f JOIN sistemas s ON s.id = f.sistema_id"
                            " WHERE f.nome_vitrine = :n AND f.situacao = 'ativa'"
                        ),
                        {"n": nome},
                    )
                ).one()
            if f.origem != "proprio":
                await _conferir_revisada_na_vitrine(gw, uuid.UUID(str(f.id)), nossos=nossos)
    com_vitrine: set[uuid.UUID] = set()
    async with gw.entrada_db.connect() as c:
        ordens = (
            await c.execute(
                text(
                    "SELECT DISTINCT pessoa_id, aplicativo_id FROM ordens_servico"
                    " WHERE situacao = 'ativa' AND inicio <= now() AND fim > now()"
                )
            )
        ).all()
        vitrines = [
            await repositorio.vitrine(c, o.pessoa_id, o.aplicativo_id, VERSAO_MCP, terceiros=True) for o in ordens
        ]
    for vitrine in vitrines:
        for f in vitrine:
            if f.sistema.origem != "proprio":
                com_vitrine.add(await _conferir_revisada_na_vitrine(gw, f.id, nossos=nossos))
    painel = await _v_terceiros(gw)
    async with gw.dono.connect() as c:
        direto = Counter(
            {
                (str(s), bool(r)): int(n)
                for s, r, n in (
                    await c.execute(
                        text(
                            "SELECT situacao, revisao_servidor_id IS NOT NULL, count(*) FROM sistemas"
                            " WHERE origem = 'terceiro' GROUP BY 1, 2"
                        )
                    )
                ).all()
            }
        )
        revisados_ativos = (
            await c.execute(
                text(
                    "SELECT count(*) FROM sistemas WHERE id = ANY(:i) AND situacao = 'ativo'"
                    " AND revisao_servidor_id IS NOT NULL"
                ),
                {"i": list(com_vitrine)},
            )
        ).scalar_one()
    assert painel == direto
    assert revisados_ativos == len(com_vitrine)
    assert painel[("ativo", True)] >= len(com_vitrine)
    esperado = Counter(base)
    if nosso is not None:
        esperado[nosso] += 1
    assert +painel == +esperado, (painel, esperado)
    return vistas


async def test_cada_terceiro_na_vitrine_tem_a_revisao_do_servidor_vigente_registrada_em_todo_o_percurso(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Percurso inteiro pelas telas e pelas rotinas, com o invariante conferido depois de cada passo: cadastro e
    sondagem, leitura, revisao de cada ferramenta, conclusao com as duas, revisao nova de uma (sai da vitrine ate a
    proxima conclusao), conclusao de novo, conclusao so com uma (a outra sai da vitrine) e de novo com as duas,
    mudanca so de texto, quarentena pelas rotinas e nova revisao."""
    k_comp, k_decl = _chave("compartilhada"), _chave("declaracoes")
    _chaves(monkeypatch, {k_comp: "*", k_decl: "*"})
    terceiro = await gw.servir(mal_comportado.criar(segunda_ferramenta=True), gravar=True)
    admin = await gw.admin()
    app = await gw.aplicativo("Assistente")
    ana = await gw.pessoa("ana")
    pares = [(ana, app)]
    nossos: set[uuid.UUID] = set()
    base = await _v_terceiros(gw)
    await _invariante(gw, pares, base, None, nossos)

    # Cadastro: a sondagem pela sala aceita a versao; nenhuma ferramenta dele vale ainda.
    nome = f"g9-terceiro-{uuid.uuid4().hex[:8]}"
    sistema_id = await admin.cadastrar(
        nome, terceiro.endereco, origem="terceiro", tipo_chave="compartilhada", chave_sondagem=k_decl
    )
    nossos.add(sistema_id)
    assert (await gw.sistema(sistema_id)).sondagem_desfecho == "aceito"
    assert "/interno/visitas/sondar" in gw.pela_sala
    await _invariante(gw, pares, base, ("ativo", False), nossos)

    # Leitura e revisao de cada ferramenta: revisadas, mas fora de qualquer vitrine (e de qualquer modelo).
    await admin.ler(sistema_id, k_decl)
    await admin.revisar(sistema_id, "consultar_pedido")
    await admin.revisar(sistema_id, "consultar_entrega")
    revisadas = [(await gw.revisada(sistema_id, n)).id for n in ("consultar_pedido", "consultar_entrega")]
    r = await admin.post(
        "/telas/admin/modelos",
        [("nome", f"g9-cedo-{gw.sufixo}"), ("descricao", "Modelo antes da conclusao."), ("validade_dias", "30")]
        + [("ferramentas", str(i)) for i in revisadas],
    )
    assert r.status_code == 400  # o modelo so leva o que a revisao do servidor vigente publicou
    await _invariante(gw, pares, base, ("ativo", False), nossos)

    # Conclusao com as duas publicadas: registrada, e as duas entram na vitrine da Ana pela ordem dela.
    await admin.guardar_chave(sistema_id, "declaracoes", k_decl)
    await admin.guardar_chave(sistema_id, "compartilhada", k_comp)
    assert (await admin.concluir(sistema_id, "consultar_pedido", "consultar_entrega")).status_code == 303
    modelo = await admin.modelo(revisadas)
    await gw.dar_ordem(ana, app, modelo)
    pedido_vitrine = (await gw.revisada(sistema_id, "consultar_pedido")).nome_vitrine
    entrega_vitrine = (await gw.revisada(sistema_id, "consultar_entrega")).nome_vitrine
    vistas = await _invariante(gw, pares, base, ("ativo", True), nossos)
    assert vistas[("ana", app.client_id)] == sorted([pedido_vitrine, entrega_vitrine])
    lida = await gw.chamar(ana, app, pedido_vitrine, {"pedido": "P-1"})
    assert not isinstance(lida, MCPError), lida
    assert len(_posts_de_chamada(terceiro)) == 1

    # Revisao nova de uma ferramenta: outra versao, fora da revisao vigente, sai da vitrine ate a proxima conclusao.
    await admin.revisar(sistema_id, "consultar_entrega", nome_vitrine=entrega_vitrine)
    vistas = await _invariante(gw, pares, base, ("ativo", True), nossos)
    assert vistas[("ana", app.client_id)] == [pedido_vitrine]
    recusa = await gw.chamar(ana, app, entrega_vitrine, {"pedido": "P-1"})
    assert isinstance(recusa, MCPError)
    assert (await admin.concluir(sistema_id, "consultar_pedido", "consultar_entrega")).status_code == 303
    vistas = await _invariante(gw, pares, base, ("ativo", True), nossos)
    assert vistas[("ana", app.client_id)] == sorted([pedido_vitrine, entrega_vitrine])

    # Conclusao so com uma: a outra continua revisada e ativa, mas sai da vitrine, porque a revisao vigente nao a
    # publica (a publicada numa revisao anterior nao vale mais). A conclusao com as duas a traz de volta.
    assert (await admin.concluir(sistema_id, "consultar_pedido")).status_code == 303
    assert (await gw.revisada(sistema_id, "consultar_entrega")).nome_vitrine == entrega_vitrine
    vistas = await _invariante(gw, pares, base, ("ativo", True), nossos)
    assert vistas[("ana", app.client_id)] == [pedido_vitrine]
    recusa = await gw.chamar(ana, app, entrega_vitrine, {"pedido": "P-1"})
    assert isinstance(recusa, MCPError)
    assert (await admin.concluir(sistema_id, "consultar_pedido", "consultar_entrega")).status_code == 303
    vistas = await _invariante(gw, pares, base, ("ativo", True), nossos)
    assert vistas[("ana", app.client_id)] == sorted([pedido_vitrine, entrega_vitrine])

    # Mudanca so de texto e a rodada das rotinas (pela sala): quarentena, nada na vitrine.
    await terceiro.modo("muda_so_texto")
    antes = len(gw.pela_sala)
    resultado = await gw.conferir(sistema_id)
    assert resultado.quarentenas == [nome]
    assert "/interno/visitas/listar" in gw.pela_sala[antes:]
    vistas = await _invariante(gw, pares, base, ("quarentena", False), nossos)
    assert vistas[("ana", app.client_id)] == []

    # Nova revisao: le de novo, revisa a que mudou e conclui. So a conclusao tira da quarentena.
    await terceiro.modo("normal")
    await admin.ler(sistema_id, k_decl)
    await admin.revisar(sistema_id, "consultar_pedido", nome_vitrine=pedido_vitrine)
    await _invariante(gw, pares, base, ("quarentena", False), nossos)
    assert (await admin.concluir(sistema_id, "consultar_pedido", "consultar_entrega")).status_code == 303
    assert (await gw.sistema(sistema_id)).situacao == "ativo"
    vistas = await _invariante(gw, pares, base, ("ativo", True), nossos)
    assert vistas[("ana", app.client_id)] == sorted([pedido_vitrine, entrega_vitrine])
    tipos = [ln.tipo for ln in await gw.extrato(sistema_id=sistema_id)]
    assert tipos.count("admin_servidor_revisado") == 5
    assert "sistema_em_quarentena" in tipos
    assert "sistema_saiu_da_quarentena" in tipos


# --- 2. Sala isolada (I2) -----------------------------------------------------------------------------------------


def _cabecalhos(conexao: bytes) -> list[tuple[bytes, bytes]]:
    cabeca = conexao.split(b"\r\n\r\n", 1)[0]
    pares = []
    for linha in cabeca.split(b"\r\n")[1:]:
        nome, _, valor = linha.partition(b":")
        pares.append((nome.strip().lower(), valor.strip()))
    return pares


def _corpo(conexao: bytes) -> dict[str, Any]:
    corpo: dict[str, Any] = json.loads(conexao.split(b"\r\n\r\n", 1)[1])
    return corpo


ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": VERSAO_MCP,
    "io.modelcontextprotocol/clientInfo": {"name": "mcp-claro", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _pedido_exato(conexao: bytes, *, host: str, chave: str) -> tuple[str, dict[str, Any]]:
    """Confere cada byte de um pedido que chegou ao terceiro: a linha do pedido, cada cabecalho (nenhum fora da
    lista, cada valor esperado), o corpo (so o envelope do MCP-CLARO, o nome e os argumentos) e nada depois dele.
    Devolve o metodo e os ``params``."""
    cabeca, corpo_bruto = conexao.split(b"\r\n\r\n", 1)
    linhas = cabeca.split(b"\r\n")
    assert linhas[0] == b"POST /mcp HTTP/1.1", linhas[0]
    corpo = json.loads(corpo_bruto)
    metodo = corpo["method"]
    params = corpo["params"]
    esperados = {
        b"host": host.encode(),
        b"connection": b"keep-alive",
        b"user-agent": b"mcp-claro",
        b"accept": b"application/json, text/event-stream",
        b"content-type": b"application/json",
        b"mcp-protocol-version": VERSAO_MCP.encode(),
        b"mcp-method": metodo.encode(),
        b"content-length": str(len(corpo_bruto)).encode(),
        b"authorization": f"Bearer {chave}".encode(),
    }
    if metodo == "tools/call":
        esperados[b"mcp-name"] = params["name"].encode()
    cabecalhos = _cabecalhos(conexao)
    assert len(cabecalhos) == len({n for n, _ in cabecalhos}), cabecalhos  # nenhum repetido
    vistos = dict(cabecalhos)
    assert set(vistos) == {*esperados, b"accept-encoding"}, sorted(vistos)
    for nome, valor in esperados.items():
        assert vistos[nome] == valor, (nome, vistos[nome])
    assert re.fullmatch(rb"[a-z]+(, [a-z]+)*", vistos[b"accept-encoding"]), vistos[b"accept-encoding"]
    assert set(corpo) == {"jsonrpc", "id", "method", "params"}
    assert corpo["jsonrpc"] == "2.0"
    assert isinstance(corpo["id"], int)
    assert params["_meta"] == ENVELOPE  # sem traceparent, sem baggage, sem nada do aplicativo
    assert set(params) == ({"name", "arguments", "_meta"} if metodo == "tools/call" else {"_meta"}), params
    return str(metodo), dict(params)


async def test_sala_isolada_o_terceiro_recebe_so_o_pedido_sem_chave_alheia_cracha_rastro_nem_resultado_alheio(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I2. Proprios com chave por pessoa (Ana e Bia), compartilhada e de programa, e um terceiro de chave por
    pessoa (mais um segundo terceiro, atendido na mesma sala). Cada pessoa le dos proprios e depois do terceiro, com
    rastro no pedido (``_meta.traceparent`` e os cabecalhos ``traceparent``, ``tracestate`` e ``baggage``); a Ana le
    tambem do segundo terceiro. Cada byte que chega a cada terceiro fica gravado e e conferido: nenhuma chave alheia
    (cada chamada leva so a chave de quem chamou, no proprio terceiro), nenhum cracha, nenhum rastro, nenhum
    resultado nem argumento de outro sistema e nada que identifique o aplicativo ou a pessoa no MCP-CLARO.

    A excecao documentada: o que o proprio teste poe nos argumentos chega ao terceiro. No fim, a Ana manda ao
    terceiro, como termo de busca, o nome de um plano que leu do sistema de chave compartilhada: ele chega, so ali,
    no argumento ``termo`` daquele pedido. O MCP-CLARO nao separa o que o aplicativo escolhe mandar (desenho 1, "o
    que fica fora")."""
    nomes = ("ana_p", "bia_p", "sond_p", "comp", "prog", "sond_prog", "ana_t", "bia_t", "decl_t", "ana_t2", "decl_t2")
    k = {n: _chave(n) for n in nomes}
    _chaves(monkeypatch, {k["ana_p"]: "ana", k["bia_p"]: "bia", k["sond_p"]: "ana"})
    por_pessoa = await gw.servir(leitura_por_pessoa.criar(), gravar=True)
    _chaves(monkeypatch, {k["comp"]: "*"})
    compartilhado = await gw.servir(chave_compartilhada.criar())
    robo_client_id = gw.client_id("Robo")  # o programa e cadastrado depois, pela tela, com este client_id
    _chaves(monkeypatch, {k["prog"]: f"programa:{robo_client_id}", k["sond_prog"]: "sondagem"})
    de_programa = await gw.servir(leitura_de_programa.criar())
    _chaves(monkeypatch, {k["ana_t"]: "ana", k["bia_t"]: "bia", k["decl_t"]: "admin"})
    terceiro = await gw.servir(tp.criar(), gravar=True)
    _chaves(monkeypatch, {k["ana_t2"]: "ana", k["decl_t2"]: "admin"})
    outro = await gw.servir(tp.criar(), gravar=True)  # um segundo terceiro, atendido na mesma sala

    admin = await gw.admin()
    app = await gw.aplicativo("Assistente")
    ana, bia = await gw.pessoa("ana"), await gw.pessoa("bia")
    robo = await admin.programa(robo_client_id, f"Robo {gw.sufixo}", responsavel=ana, substituto=bia)
    s_pessoa = await admin.proprio(
        por_pessoa,
        {"ver_cadastro": {"campos": ["cpf", "email"]}, "consultar_fatura": {"campos": ["cpf"]}},
        tipo_chave="por_pessoa",
        chave_sondagem=k["sond_p"],
    )
    s_comp = await admin.proprio(
        compartilhado, {"consultar_catalogo": {}}, tipo_chave="compartilhada", chave_sondagem=k["comp"]
    )
    await admin.guardar_chave(s_comp, "compartilhada", k["comp"])
    s_prog = await admin.proprio(
        de_programa, {"consultar_fila": {}}, tipo_chave="programa", chave_sondagem=k["sond_prog"]
    )
    await admin.guardar_chave(s_prog, "programa", k["prog"], aplicativo=robo.id)
    s_terceiro = await admin.terceiro(
        terceiro, {"ver_ficha": {"campos": ["cpf", "email"]}, "procurar": {}}, chave_declaracoes=k["decl_t"]
    )
    s_outro = await admin.terceiro(outro, {"ver_ficha": {"campos": ["cpf", "email"]}}, chave_declaracoes=k["decl_t2"])
    for sistema_id, pessoa, chave in (
        (s_pessoa, ana, "ana_p"),
        (s_pessoa, bia, "bia_p"),
        (s_terceiro, ana, "ana_t"),
        (s_terceiro, bia, "bia_t"),
        (s_outro, ana, "ana_t2"),
    ):
        await gw.chave_da_pessoa(sistema_id, pessoa, k[chave])

    f = {
        "cadastro": await gw.revisada(s_pessoa, "ver_cadastro"),
        "fatura": await gw.revisada(s_pessoa, "consultar_fatura"),
        "catalogo": await gw.revisada(s_comp, "consultar_catalogo"),
        "fila": await gw.revisada(s_prog, "consultar_fila"),
        "ficha": await gw.revisada(s_terceiro, "ver_ficha"),
        "procurar": await gw.revisada(s_terceiro, "procurar"),
        "ficha2": await gw.revisada(s_outro, "ver_ficha"),
    }
    modelo_pessoas = await admin.modelo(
        [f[n].id for n in ("cadastro", "fatura", "catalogo", "ficha", "procurar", "ficha2")]
    )
    await gw.dar_ordem(ana, app, modelo_pessoas)
    await gw.dar_ordem(bia, app, modelo_pessoas)
    conta = await admin.ordem_do_programa(robo, await admin.modelo([f["fila"].id]), "robo-conta")

    # As leituras dos proprios, com as chaves de cada um: os resultados ficam no MCP-CLARO e no aplicativo.
    resultados: list[str] = []
    for pessoa in (ana, bia):
        for nome, argumentos in (("cadastro", {}), ("fatura", {"mes": "2026-11"}), ("catalogo", {"termo": "plano"})):
            r = await gw.chamar(pessoa, app, f[nome].nome_vitrine, argumentos)
            assert not isinstance(r, MCPError), (pessoa, nome, r)
            resultados.append(json.dumps(r.model_dump(by_alias=True, mode="json"), ensure_ascii=False))
    r = await gw.chamar(conta, robo, f["fila"].nome_vitrine, {"fila": "suporte"})
    assert not isinstance(r, MCPError), r
    assert r.structured_content["programa"] == robo.client_id  # o programa leu com a chave propria
    resultados.append(json.dumps(r.model_dump(by_alias=True, mode="json"), ensure_ascii=False))
    assert {f"Bearer {k['ana_p']}", f"Bearer {k['bia_p']}"} <= {
        v.decode() for c in por_pessoa.conexoes() for n, v in _cabecalhos(c) if n == b"authorization"
    }  # os proprios receberam as chaves deles: sao essas que nao podem chegar ao terceiro

    # Agora o terceiro, com rastro no pedido. Cada chamada tem um marcador proprio nos argumentos.
    trace_id = uuid.uuid4().hex
    rastro = {"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01", "tracestate": "fornecedor=segredo-do-rastro"}
    cabecalhos = rastro | {"baggage": "pessoa=segredo-da-bagagem"}
    marcadores = {}
    for pessoa in (ana, bia):
        marcadores[pessoa.apelido] = f"busca-da-{pessoa.apelido}-{uuid.uuid4().hex[:8]}"
        r = await gw.chamar(
            pessoa,
            app,
            f["procurar"].nome_vitrine,
            {"termo": marcadores[pessoa.apelido]},
            meta=rastro | {"baggage": "pessoa=segredo-da-bagagem"},
            cabecalhos=cabecalhos,
        )
        assert not isinstance(r, MCPError), r
        r = await gw.chamar(pessoa, app, f["ficha"].nome_vitrine, cabecalhos=cabecalhos)
        assert not isinstance(r, MCPError), r
        assert any(f"{pessoa.apelido.capitalize()} Terceira" in t for t in g9.dentro_e_fora(r)[0])
    # Depois de ler do primeiro terceiro, a Ana le do segundo, na mesma sala.
    r = await gw.chamar(ana, app, f["ficha2"].nome_vitrine, cabecalhos=cabecalhos)
    assert not isinstance(r, MCPError), r
    chamadas = _posts_de_chamada(terceiro)
    assert len(chamadas) == 4  # uma conexao, um POST por chamada, cada uma num filho da sala
    assert gw.pela_sala.count("/interno/visitas/chamar") >= 4

    crachas = [gw.cracha(p, a) for p, a in ((ana, app), (bia, app), (conta, robo))]
    dos_proprios = [k[n] for n in ("ana_p", "bia_p", "sond_p", "comp", "prog", "sond_prog")]
    vistos_pelo_aplicativo = ["Ana Teste", "Bia Teste", "Plano 10 GB", "Plano 50 GB", "R$ 104.50", "na fila suporte"]
    for trecho in vistos_pelo_aplicativo:
        assert any(trecho in r for r in resultados), trecho  # o aplicativo recebeu: o resultado existe
    # Os dados crus dos proprios (antes da mascara), que so o despachante viu.
    crus = ["123.456.789-09", "987.654.321-00", "ana@exemplo.test", "bia@exemplo.test"]
    de_outros = [*vistos_pelo_aplicativo, *crus]
    identidade = [
        *(app.client_id, robo.client_id, app.nome, robo.nome),
        *(ana.sujeito, bia.sujeito, conta.sujeito, str(ana.id), str(bia.id), str(conta.id)),
    ]
    comuns = [
        *crachas,
        *(c.split(".")[1] for c in crachas),
        *de_outros,
        *identidade,
        trace_id,
        "segredo-do-rastro",
        "segredo-da-bagagem",
    ]
    proibidos = [*dos_proprios, k["ana_t2"], k["decl_t2"], *comuns]
    host = terceiro.base.split("://", 1)[1]
    conexoes = terceiro.conexoes()
    # Todas as conexoes, do cadastro ate aqui: a sondagem e as listagens do ADMIN, com a chave das declaracoes, e as
    # quatro chamadas, cada uma com a chave de quem chamou e o argumento que o teste pos. Byte a byte.
    vistos: list[tuple[str, str, dict[str, Any]]] = []
    for conexao in conexoes:
        for proibido in proibidos:
            assert proibido.encode() not in conexao, (proibido, conexao[:400])
        assert b"eyJ" not in conexao  # nenhum JWT
        for palavra in (b"traceparent", b"tracestate", b"baggage", b"cookie", b"forwarded"):
            assert palavra not in conexao.lower(), palavra
        autorizacao = dict(_cabecalhos(conexao))[b"authorization"].decode().removeprefix("Bearer ")
        dona = next(n for n in ("ana_t", "bia_t", "decl_t") if k[n] == autorizacao)
        metodo, params = _pedido_exato(conexao, host=host, chave=k[dona])
        vistos.append((dona, metodo, params))
    assert [(d, m) for d, m, _ in vistos if m != "tools/call"] == [
        ("decl_t", "server/discover"),
        ("decl_t", "tools/list"),
        ("decl_t", "tools/list"),
    ]  # o cadastro (sondagem), "Ler as ferramentas agora" e a conclusao da revisao do servidor
    esperadas = [
        (f"{p}_t", "tools/call", {"name": n, "arguments": a, "_meta": ENVELOPE})
        for p in ("ana", "bia")
        for n, a in (("procurar", {"termo": marcadores[p]}), ("ver_ficha", {}))
    ]
    assert [v for v in vistos if v[1] == "tools/call"] == esperadas
    # O segundo terceiro: nada do primeiro (chaves, argumentos da Ana e da Bia), nem dos proprios. Byte a byte.
    do_primeiro = [k["ana_t"], k["bia_t"], k["decl_t"], *marcadores.values()]
    outros_vistos = []
    for conexao in outro.conexoes():
        for proibido in [*dos_proprios, *do_primeiro, *comuns]:
            assert proibido.encode() not in conexao, (proibido, conexao[:400])
        assert b"eyJ" not in conexao
        autorizacao = dict(_cabecalhos(conexao))[b"authorization"].decode().removeprefix("Bearer ")
        dona = next(n for n in ("ana_t2", "decl_t2") if k[n] == autorizacao)
        outros_vistos.append((dona, *_pedido_exato(conexao, host=outro.base.split("://", 1)[1], chave=k[dona])))
    assert outros_vistos == [
        ("decl_t2", "server/discover", {"_meta": ENVELOPE}),
        ("decl_t2", "tools/list", {"_meta": ENVELOPE}),
        ("decl_t2", "tools/list", {"_meta": ENVELOPE}),
        ("ana_t2", "tools/call", {"name": "ver_ficha", "arguments": {}, "_meta": ENVELOPE}),
    ]
    # O rastro chegou ao MCP-CLARO (fica no extrato dele), mas nao passou dali.
    feitas = await gw.extrato(sistema_id=s_terceiro, tipo="leitura_feita")
    assert trace_id in {ln.trace_id for ln in feitas}

    # A excecao documentada: o que o aplicativo poe no argumento vai ao terceiro, so ali.
    r = await gw.chamar(ana, app, f["procurar"].nome_vitrine, {"termo": "Plano 10 GB"})
    assert not isinstance(r, MCPError), r
    (conexao,) = terceiro.conexoes()[len(conexoes) :]
    assert conexao.count(b"Plano 10 GB") == 1
    assert _pedido_exato(conexao, host=host, chave=k["ana_t"]) == (
        "tools/call",
        {"name": "procurar", "arguments": {"termo": "Plano 10 GB"}, "_meta": ENVELOPE},
    )


# --- 3. Cracha nao repassado e chave por pessoa, com terceiro -----------------------------------------------------


async def test_cracha_do_aplicativo_nao_chega_ao_terceiro_e_uma_pessoa_nao_alcanca_o_que_e_de_outra(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``test_cracha_do_aplicativo_nao_chega_ao_sistema`` e ``test_uma_pessoa_nao_alcanca_o_que_e_de_outra`` (de
    ``test_provar_a_base.py``) refeitos com um servidor de terceiros de chave por pessoa, pela sala."""
    k_ana, k_bia, k_decl = _chave("ana"), _chave("bia"), _chave("declaracoes")
    _chaves(monkeypatch, {k_ana: "ana", k_bia: "bia", k_decl: "admin"})
    terceiro = await gw.servir(tp.criar(), gravar=True)
    admin = await gw.admin()
    sistema_id = await admin.terceiro(
        terceiro, {"ver_ficha": {"campos": ["cpf", "email"]}, "procurar": {}}, chave_declaracoes=k_decl
    )
    ficha, procura = await gw.revisada(sistema_id, "ver_ficha"), await gw.revisada(sistema_id, "procurar")
    app = await gw.aplicativo("Assistente")
    ana, bia = await gw.pessoa("ana"), await gw.pessoa("bia")
    await gw.dar_ordem(ana, app, await admin.modelo([ficha.id, procura.id]))
    await gw.dar_ordem(bia, app, await admin.modelo([ficha.id]))
    await gw.chave_da_pessoa(sistema_id, ana, k_ana)
    await gw.chave_da_pessoa(sistema_id, bia, k_bia)

    # O cracha da Ana nao chega ao terceiro: so a chave dela, pela sala.
    terceiro.zerar()
    cracha = gw.cracha(ana, app)
    antes = gw.pela_sala.count("/interno/visitas/chamar")
    r = await gw.chamar(ana, app, ficha.nome_vitrine)
    assert not isinstance(r, MCPError), r
    assert gw.pela_sala.count("/interno/visitas/chamar") == antes + 1
    dentro, _ = g9.dentro_e_fora(r)
    assert any("Ana Terceira" in t for t in dentro)
    recebido = terceiro.recebido()
    assert cracha.encode() not in recebido
    assert cracha.split(".")[1].encode() not in recebido  # nem o corpo do cracha
    vistas = [v for c in terceiro.conexoes() for n, v in _cabecalhos(c) if n == b"authorization"]
    assert vistas
    assert set(vistas) == {f"Bearer {k_ana}".encode()}

    # A Bia ve so o que e dela: a ficha dela, com a chave dela; a ferramenta da ordem da Ana nao esta na vitrine dela.
    assert await gw.listar(bia, app) == [ficha.nome_vitrine]
    terceiro.zerar()
    r = await gw.chamar(bia, app, ficha.nome_vitrine)
    assert not isinstance(r, MCPError), r
    dentro, _ = g9.dentro_e_fora(r)
    juntos = " ".join(dentro)
    assert "Bia Terceira" in juntos
    assert "Ana Terceira" not in juntos
    assert "222.555.888-46" not in juntos  # o CPF da Bia vai mascarado (campo pessoal revisado)
    vistas = [v for c in terceiro.conexoes() for n, v in _cabecalhos(c) if n == b"authorization"]
    assert set(vistas) == {f"Bearer {k_bia}".encode()}
    assert k_ana.encode() not in terceiro.recebido()
    assert await gw.listar(ana, app) == sorted([ficha.nome_vitrine, procura.nome_vitrine])
    terceiro.zerar()
    recusa = await gw.chamar(bia, app, procura.nome_vitrine, {"termo": "da ana"})
    assert isinstance(recusa, MCPError)
    assert "nao esta na sua vitrine" in recusa.error.message
    assert terceiro.conexoes() == []  # nada chegou ao terceiro
    assert _pedidos(terceiro) == 0
    assert terceiro.chamadas("procurar") == 0


# --- 4. Mudanca so de texto leva a quarentena, pela sala ---------------------------------------------------------


async def test_mudanca_so_de_texto_em_terceiro_leva_o_servidor_inteiro_a_quarentena_pela_sala(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prova de saida do G5 (``test_rotinas_terceiros``), agora pelo gateway com a sala: o ADMIN revisa e publica
    as duas ferramentas pelas telas; as rotinas leem as declaracoes pelo despachante, que lista pela sala."""
    k_comp, k_decl = _chave("compartilhada"), _chave("declaracoes")
    _chaves(monkeypatch, {k_comp: "*", k_decl: "*"})
    duble = await gw.servir(mal_comportado.criar(segunda_ferramenta=True), gravar=True)
    admin = await gw.admin()
    sistema_id = await admin.terceiro(
        duble, {"consultar_pedido": {}, "consultar_entrega": {}}, tipo_chave="compartilhada", chave_declaracoes=k_decl
    )
    await admin.guardar_chave(sistema_id, "compartilhada", k_comp)
    nome = (await gw.sistema(sistema_id)).nome
    pedido, entrega = (
        await gw.revisada(sistema_id, "consultar_pedido"),
        await gw.revisada(sistema_id, "consultar_entrega"),
    )
    app = await gw.aplicativo("Assistente")
    ana = await gw.pessoa("ana")
    await gw.dar_ordem(ana, app, await admin.modelo([pedido.id, entrega.id]))
    assert await gw.listar(ana, app) == sorted([pedido.nome_vitrine, entrega.nome_vitrine])
    lida = await gw.chamar(ana, app, pedido.nome_vitrine, {"pedido": "P-1"})
    assert not isinstance(lida, MCPError), lida
    dentro, _ = g9.dentro_e_fora(lida)
    assert any("Pedido em transito." in t for t in dentro)
    chamadas_antes = duble.chamadas("consultar_pedido")
    revisao = (await gw.sistema(sistema_id)).revisao_servidor_id

    # Sem mudanca: nada. A listagem das rotinas passa pela sala, com a chave das declaracoes.
    duble.zerar()
    antes = len(gw.pela_sala)
    resultado = await gw.conferir(sistema_id)
    assert nome not in resultado.quarentenas
    assert nome not in resultado.sem_resposta
    assert gw.pela_sala[antes:].count("/interno/visitas/listar") == 1
    autorizacoes = {v for c in duble.conexoes() for n, v in _cabecalhos(c) if n == b"authorization"}
    assert autorizacoes == {f"Bearer {k_decl}".encode()}
    assert (await gw.sistema(sistema_id)).situacao == "ativo"

    # So o texto de uma das duas muda: mudou, parou, o servidor inteiro.
    await duble.modo("muda_so_texto")
    resultado = await gw.conferir(sistema_id)
    assert resultado.quarentenas == [nome]
    depois = await gw.sistema(sistema_id)
    assert (depois.situacao, depois.motivo, depois.revisao_servidor_id) == ("quarentena", MOTIVO_QUARENTENA, None)

    # Pela entrada: nenhuma das duas na lista, e a leitura e recusada sem chegar ao servidor.
    assert await gw.listar(ana, app) == []
    duble.zerar()
    recusa = await gw.chamar(ana, app, entrega.nome_vitrine, {"pedido": "P-1"})
    assert isinstance(recusa, MCPError), recusa
    assert recusa.error.code == -32602
    assert "nao esta na sua vitrine" in recusa.error.message
    assert duble.chamadas("consultar_pedido") == chamadas_antes
    assert duble.chamadas("consultar_entrega") == 0
    assert _posts_de_chamada(duble) == []

    (linha,) = await gw.extrato(sistema_id=sistema_id, tipo="sistema_em_quarentena")
    assert linha.papel == "rotinas"
    assert nome in linha.frase
    assert [(x["ferramenta"], x["mudanca"]) for x in linha.dados["ferramentas"]] == [("consultar_pedido", "mudou")]
    assert linha.dados["revisao"] == str(revisao)
    (aviso,) = await _avisos_de_quarentena(gw, sistema_id)
    assert (aviso.canal, aviso.destino) == ("email", g9.ADMIN_EMAIL)
    # O assunto e o texto sao os fixos do aviso, so com o nome do cadastro e a quantidade.
    assert (aviso.assunto, aviso.corpo["texto"]) == aviso_admin("sistema_em_quarentena", sistema=nome, quantas=1)
    assert aviso.corpo["sistema"] == nome
    assert aviso.corpo["mudancas"] == {"mudou": 1, "nova": 0, "sumiu": 0}
    # Nada do texto do fornecedor: nem a descricao, nem o nome das ferramentas no servidor (rotinas/mudou_parou.py).
    tudo = aviso.assunto + json.dumps(aviso.corpo, ensure_ascii=False)
    for do_fornecedor in ("Consulta a situacao", "consultar_pedido", "consultar_entrega"):
        assert do_fornecedor not in tudo, do_fornecedor

    # Segunda rodada: nada se repete (o servidor nao esta mais ativo): nem a linha do extrato, nem o aviso.
    resultado = await gw.conferir(sistema_id)
    assert nome not in resultado.quarentenas
    assert len(await gw.extrato(sistema_id=sistema_id, tipo="sistema_em_quarentena")) == 1
    assert await _avisos_de_quarentena(gw, sistema_id) == [aviso]


async def _avisos_de_quarentena(gw: Gateway, sistema_id: uuid.UUID) -> list[Any]:
    """Os avisos (e-mail e webhook) de ``sistema_em_quarentena`` deste sistema."""
    async with gw.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT canal, destino, assunto, corpo FROM avisos WHERE evento = 'sistema_em_quarentena'"
                        " AND corpo ->> 'sistema_id' = :s"
                    ),
                    {"s": str(sistema_id)},
                )
            ).all()
        )


# --- 5. Tres sinais suspendem o limite e a ficha mostra o motivo ------------------------------------------------


async def test_tres_sinais_suspendem_o_limite_e_a_ficha_mostra_o_motivo(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limite "so equipe" no ``enviar_mensagem`` de um sistema proprio. Pela entrada de verdade e pelo despachante
    de verdade: ler de um terceiro (pela sala), ler ferramenta com campos pessoais e pedir ``enviar_mensagem``
    coberta pelo limite. A acao vai para a ficha (``aguardando_ficha``), o limite nao e gasto, a rota e
    ``tres_sinais``, o extrato tem a linha e a ficha, nas telas, mostra o alerta com as duas ferramentas e as horas,
    sem argumento nem resultado das leituras."""
    k = {n: _chave(n) for n in ("ana_p", "sond_p", "ana_c", "sond_c", "ana_t", "decl_t")}
    _chaves(monkeypatch, {k["ana_p"]: "ana", k["sond_p"]: "ana"})
    por_pessoa = await gw.servir(leitura_por_pessoa.criar())
    _chaves(monkeypatch, {k["ana_c"]: "ana", k["sond_c"]: "ana"})
    caixa = await gw.servir(acoes_caixa.criar())
    _chaves(monkeypatch, {k["ana_t"]: "ana", k["decl_t"]: "admin"})
    terceiro = await gw.servir(tp.criar(), gravar=True)
    admin = await gw.admin()
    s_pessoa = await admin.proprio(
        por_pessoa, {"ver_cadastro": {"campos": ["cpf", "email"]}}, tipo_chave="por_pessoa", chave_sondagem=k["sond_p"]
    )
    s_caixa = await admin.proprio(
        caixa,
        {"enviar_mensagem": {"classe": "muda", "destinatario": ["destinatario"]}},
        tipo_chave="por_pessoa",
        chave_sondagem=k["sond_c"],
    )
    s_terceiro = await admin.terceiro(terceiro, {"procurar": {}}, chave_declaracoes=k["decl_t"])
    cadastro = await gw.revisada(s_pessoa, "ver_cadastro")
    mensagem = await gw.revisada(s_caixa, "enviar_mensagem")
    procurar = await gw.revisada(s_terceiro, "procurar")
    app = await gw.aplicativo("Assistente")
    ana = await gw.pessoa("ana")
    modelo = await admin.modelo([cadastro.id, mensagem.id, procurar.id])
    await gw.dar_ordem(ana, app, modelo)
    await gw.fora_da_experiencia(ana)
    await gw.chave_da_pessoa(s_pessoa, ana, k["ana_p"])
    await gw.chave_da_pessoa(s_caixa, ana, k["ana_c"])
    await gw.chave_da_pessoa(s_terceiro, ana, k["ana_t"])
    # O limite "so equipe", criado pela Ana na tela de limites, com a passkey.
    limite = await gw.limite(ana, app, s_caixa, "enviar_mensagem", campo="destinatario", valores=["equipe"])
    async with gw.dono.connect() as c:
        linha = (
            await c.execute(
                text(
                    "SELECT destinatarios, com_passkey, situacao, usados_hoje,"
                    " dia = (now() AT TIME ZONE :fuso)::date AS de_hoje FROM limites WHERE id = :l"
                ),
                {"l": limite, "fuso": FUSO},
            )
        ).one()
    assert (linha.destinatarios, linha.com_passkey, linha.situacao, linha.usados_hoje, linha.de_hoje) == (
        [{"campo": "destinatario", "valores": ["equipe"]}],
        True,
        "ativo",
        0,
        True,
    )

    async def usados() -> int:
        async with gw.dono.connect() as c:
            return int(
                (await c.execute(text("SELECT usados_hoje FROM limites WHERE id = :l"), {"l": limite})).scalar_one()
            )

    # Sem alerta, o limite cobre o pedido: liberada, com o limite gasto. A espera antes de sair fica no maximo da
    # politica durante o teste: a acao liberada continua liberada ate o fim, seja qual for a velocidade da maquina.
    await gw.pilha.enter_async_context(gw.politica(OUTBOX_HOLD_SECONDS=POLITICA["OUTBOX_HOLD_SECONDS"].maximo))
    antes = await gw.chamar(ana, app, mensagem.nome_vitrine, {"destinatario": "equipe", "texto": "antes do alerta"})
    op_antes = await _operacao(gw, g9.protocolo(antes))
    assert (op_antes.estado, op_antes.limite_id) == (Estado.LIBERADA, limite)
    assert await usados() == 1

    # Os tres sinais, pelo gateway: texto de fora (terceiro, pela sala) e dado pessoal (proprio).
    lida_de_fora = await gw.chamar(ana, app, procurar.nome_vitrine, {"termo": "SEGREDO-DA-LEITURA"})
    assert not isinstance(lida_de_fora, MCPError), lida_de_fora
    assert len(_posts_de_chamada(terceiro)) == 1
    lida_pessoal = await gw.chamar(ana, app, cadastro.nome_vitrine)
    assert not isinstance(lida_pessoal, MCPError), lida_pessoal
    depois = await gw.chamar(ana, app, mensagem.nome_vitrine, {"destinatario": "equipe", "texto": "SEGREDO-DO-PEDIDO"})
    prot = g9.protocolo(depois)
    op = await _operacao(gw, prot)
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.limite_id is None
    assert await usados() == 1  # nada gasto
    assert "Nada saiu ainda" in g9.textos(depois)[0]  # o aplicativo nao fica sabendo do alerta (D24)
    assert "sinais" not in json.dumps(depois.model_dump(by_alias=True)).lower()

    pedida = await gw.extrato(pessoa=ana, tipo="acao_pedida")
    assert [ln.dados["rota"] for ln in pedida if ln.protocolo == prot] == ["tres_sinais"]
    (alerta,) = [ln for ln in await gw.extrato(pessoa=ana, tipo="tres_sinais") if ln.protocolo == prot]
    assert alerta.frase == (
        f"Alerta dos tres sinais no protocolo {prot}: o aplicativo leu texto de fora em {procurar.nome_vitrine} e"
        f" viu dados pessoais em {cadastro.nome_vitrine}; nenhum limite valeu, e a acao foi para a ficha."
    )
    assert "SEGREDO" not in json.dumps(alerta.dados)

    # O retrato gravado na ficha (``fichas.sinais``) e o das duas linhas de sinais que as leituras gravaram, exato: a
    # janela da politica vigente e, de cada sinal, o tipo, a ferramenta (nome na vitrine), o sistema (nome do
    # cadastro) e a hora, ao microssegundo.
    sistema_terceiro = (await gw.sistema(s_terceiro)).nome
    sistema_pessoa = (await gw.sistema(s_pessoa)).nome
    vistos = await _sinais(gw, ana, app)
    assert sorted(vistos) == sorted(sinais.TIPOS)  # uma linha por leitura, nenhuma da acao
    async with gw.dono.connect() as c:
        retrato = (await c.execute(text("SELECT sinais FROM fichas WHERE id = :f"), {"f": op.ficha_id})).scalar_one()
        janela = await politica.vigente(c, sinais.CHAVE_JANELA)
    assert retrato["janela_minutos"] == janela
    assert [v | {"visto_em": datetime.fromisoformat(v["visto_em"])} for v in retrato["vistos"]] == [
        {
            "sinal": sinais.TEXTO_DE_FORA,
            "ferramenta": procurar.nome_vitrine,
            "sistema": sistema_terceiro,
            "visto_em": vistos[sinais.TEXTO_DE_FORA],
        },
        {
            "sinal": sinais.DADO_PRIVADO,
            "ferramenta": cadastro.nome_vitrine,
            "sistema": sistema_pessoa,
            "visto_em": vistos[sinais.DADO_PRIVADO],
        },
    ]

    # A ficha nas telas da Ana.
    nav = await gw.entrar(ana)
    r = await nav.get(f"/telas/fichas/{op.ficha_id}")
    assert r.status_code == 200
    html = r.text
    cartao = html.split('id="cartao-tres-sinais"', 1)[1].split("</section>", 1)[0]
    assert '<h2 id="alerta-tres-sinais">Alerta dos três sinais</h2>' in cartao
    assert "<strong>Seus limites com este aplicativo não valem agora.</strong>" in cartao
    assert _linhas_do_cartao(cartao) == [
        _linha_de_fora(procurar.nome_vitrine, sistema_terceiro, vistos[sinais.TEXTO_DE_FORA]),
        _linha_privado(cadastro.nome_vitrine, sistema_pessoa, vistos[sinais.DADO_PRIVADO]),
        "e agora quer enviar algo.",
    ]
    assert "SEGREDO-DO-PEDIDO" not in cartao  # o argumento da acao fica so em "Com estes dados"
    assert "SEGREDO-DO-PEDIDO" in html
    for leitura in ("SEGREDO-DA-LEITURA", "Nada encontrado", tp.MARCA, "Ana Teste", "ana@exemplo.test", "***09"):
        assert leitura not in html, leitura  # nada das leituras: nem argumento, nem resultado
    # A acao liberada antes do alerta continua como estava: o alerta vale para o que for pedido depois.
    assert (await _operacao(gw, op_antes.protocolo)).estado == Estado.LIBERADA

    # Cada hora no cartao fica junto do seu sinal. As duas leituras de cima caem no mesmo minuto, e o cartao mostra a
    # hora ao minuto: ali, horas trocadas passariam. Num segundo aplicativo da Ana, o sinal "leu texto de fora" e de
    # 37 minutos atras, gravado pelo dono das tabelas (a guarda de ``sinais`` so deixa o dono gravar outra hora, para
    # os testes; nada de esperar o relogio), e a leitura dos dados pessoais e de agora, pelo gateway.
    outro = await gw.aplicativo("Assistente Dois")
    await gw.dar_ordem(ana, outro, modelo)
    await gw.fora_da_experiencia(ana)
    async with gw.dono.begin() as c:
        await c.execute(
            text(
                "INSERT INTO sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id, visto_em)"
                " VALUES (:p, :a, :t, :s, :f, now() - interval '37 minutes')"
            ),
            {"p": ana.id, "a": outro.id, "t": sinais.TEXTO_DE_FORA, "s": s_terceiro, "f": procurar.id},
        )
    lida_pessoal = await gw.chamar(ana, outro, cadastro.nome_vitrine)
    assert not isinstance(lida_pessoal, MCPError), lida_pessoal
    op_outro = await _operacao(
        gw,
        g9.protocolo(await gw.chamar(ana, outro, mensagem.nome_vitrine, {"destinatario": "equipe", "texto": "dois"})),
    )
    assert op_outro.estado == Estado.AGUARDANDO_FICHA
    pedida = await gw.extrato(pessoa=ana, tipo="acao_pedida")
    assert [ln.dados["rota"] for ln in pedida if ln.protocolo == op_outro.protocolo] == ["tres_sinais"]
    vistos_outro = await _sinais(gw, ana, outro)
    de_fora, privado = vistos_outro[sinais.TEXTO_DE_FORA], vistos_outro[sinais.DADO_PRIVADO]
    assert acoes.hora_e_dia(de_fora) != acoes.hora_e_dia(privado)
    r = await nav.get(f"/telas/fichas/{op_outro.ficha_id}")
    assert r.status_code == 200
    cartao = r.text.split('id="cartao-tres-sinais"', 1)[1].split("</section>", 1)[0]
    assert _linhas_do_cartao(cartao) == [
        _linha_de_fora(procurar.nome_vitrine, sistema_terceiro, de_fora),
        _linha_privado(cadastro.nome_vitrine, sistema_pessoa, privado),
        "e agora quer enviar algo.",
    ]


async def _sinais(gw: Gateway, pessoa: Pessoa, app: Aplicativo) -> dict[str, datetime]:
    """A hora de cada sinal da pessoa com o aplicativo (uma linha por tipo, conferido)."""
    async with gw.dono.connect() as c:
        linhas = (
            await c.execute(
                text("SELECT tipo, visto_em FROM sinais WHERE pessoa_id = :p AND aplicativo_id = :a"),
                {"p": pessoa.id, "a": app.id},
            )
        ).all()
    vistos = {str(tipo): visto for tipo, visto in linhas}
    assert len(vistos) == len(linhas), linhas
    return vistos


def _linhas_do_cartao(cartao: str) -> list[str]:
    return re.findall(r"<li>(.*?)</li>", cartao, re.DOTALL)


def _linha_de_fora(ferramenta: str, sistema: str, visto: datetime) -> str:
    return (
        f"leu texto escrito por quem está fora: <bdi>{ferramenta}</bdi>, no sistema <bdi>{sistema}</bdi>,"
        f" às {acoes.hora_e_dia(visto)};"
    )


def _linha_privado(ferramenta: str, sistema: str, visto: datetime) -> str:
    return (
        f"viu dados pessoais seus: <bdi>{ferramenta}</bdi>, no sistema <bdi>{sistema}</bdi>,"
        f" às {acoes.hora_e_dia(visto)};"
    )


# --- 6. Pedido de senha vindo do servidor nao chega a pessoa -----------------------------------------------------


def _sem_pedido_de_senha(texto: str) -> None:
    for trecho in (mal_comportado.TEXTO_SENHA, "senha do banco", "Digite sua senha", "phish.exemplo", tp.MARCA):
        assert trecho.lower() not in texto.lower(), trecho


async def _telas_da_pessoa(gw: Gateway, pessoa: Pessoa, *ops: fila.Operacao) -> list[str]:
    nav = await gw.entrar(pessoa)
    paginas = []
    caminhos = ["/telas", "/telas/extrato", "/telas/acoes", "/telas/pendencias", "/telas/limites", "/telas/ordens"]
    caminhos += [f"/telas/acoes/{op.id}" for op in ops] + [f"/telas/acoes/{op.id}/estado" for op in ops]
    for caminho in caminhos:
        r = await nav.get(caminho)
        assert r.status_code == 200, caminho
        paginas.append(r.text)
    return paginas


async def test_pedido_de_senha_vindo_do_terceiro_nao_chega_a_pessoa(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """i8. Um terceiro que pede "Digite sua senha do banco" no meio da chamada (``pede_dados``), numa leitura e numa
    acao (por um limite, sem a espera): nada no resultado do aplicativo, no extrato nem nas telas da pessoa; nenhuma
    linha nova em ``avisos``; um POST so por chamada. E o mesmo para um texto comum de resposta que pede a senha num
    link (critica A4): o aplicativo recebe o texto emoldurado, e nada dele aparece nas telas da pessoa."""
    k_ana_d, k_decl, k_ana_t, k_decl_t = _chave("ana_d"), _chave("decl"), _chave("ana_t"), _chave("decl_t")
    _chaves(monkeypatch, {k_ana_d: "ana", k_decl: "admin"})
    duble = await gw.servir(mal_comportado.criar(segunda_ferramenta=True), gravar=True)
    _chaves(monkeypatch, {k_ana_t: "ana", k_decl_t: "admin"})
    falante = await gw.servir(tp.criar(), gravar=True)
    admin = await gw.admin()
    s_duble = await admin.terceiro(
        duble,
        {"consultar_pedido": {}, "consultar_entrega": {"classe": "muda"}},
        chave_declaracoes=k_decl,
    )
    s_falante = await admin.terceiro(
        falante, {"procurar": {}, "enviar_recado": {"classe": "muda"}}, chave_declaracoes=k_decl_t
    )
    ler, agir = await gw.revisada(s_duble, "consultar_pedido"), await gw.revisada(s_duble, "consultar_entrega")
    procurar, recado = await gw.revisada(s_falante, "procurar"), await gw.revisada(s_falante, "enviar_recado")
    app = await gw.aplicativo("Assistente")
    ana = await gw.pessoa("ana")
    await gw.dar_ordem(ana, app, await admin.modelo([ler.id, agir.id, procurar.id, recado.id]))
    await gw.fora_da_experiencia(ana)
    await gw.chave_da_pessoa(s_duble, ana, k_ana_d)
    await gw.chave_da_pessoa(s_falante, ana, k_ana_t)
    await gw.limite(ana, app, s_duble, "consultar_entrega")
    await gw.limite(ana, app, s_falante, "enviar_recado")
    ultimo_aviso = await gw.ultimo_aviso()
    extrato_antes = await gw.ultimo_extrato()

    await duble.modo("pede_dados")
    async with gw.politica(OUTBOX_HOLD_SECONDS=0):
        duble.zerar()
        lida = await gw.chamar(ana, app, ler.nome_vitrine, {"pedido": "P-1"})
        assert not isinstance(lida, MCPError), lida
        assert lida.is_error is True
        assert lida.meta[MOTIVO] == "sistema_pediu_dados"
        assert _pedidos(duble) == 1  # um POST so, sem segunda volta
        assert len(_posts_de_chamada(duble)) == 1
        duble.zerar()
        acao = await gw.chamar(ana, app, agir.nome_vitrine, {"pedido": "P-2"})
        assert not isinstance(acao, MCPError), acao
        op_acao = await _operacao(gw, g9.protocolo(acao))
        assert op_acao.estado == Estado.SISTEMA_PEDIU_DADOS
        assert op_acao.resultado is None  # nada do pedido (mensagem, link, esquema) fica guardado na operacao
        assert _pedidos(duble) == 1
        assert len(_posts_de_chamada(duble)) == 1
        # A4: texto comum de resposta com a senha e um link, numa leitura e numa acao que sai.
        await falante.modo("senha_no_texto")
        lida_comum = await gw.chamar(ana, app, procurar.nome_vitrine, {"termo": "oi"})
        assert not isinstance(lida_comum, MCPError), lida_comum
        enviada = await gw.chamar(ana, app, recado.nome_vitrine, {"destinatario": "equipe", "texto": "oi"})
        assert not isinstance(enviada, MCPError), enviada
    op_recado = await _operacao(gw, g9.protocolo(enviada))
    assert (op_recado.estado, op_recado.de_terceiro) == (Estado.ENVIADA, True)
    assert duble.chamadas("consultar_pedido") == 1
    assert duble.chamadas("consultar_entrega") == 1

    # O aplicativo: nada do pedido de senha (o texto comum chega, so dentro da moldura).
    for r in (lida, acao):
        _sem_pedido_de_senha(json.dumps(r.model_dump(by_alias=True, mode="json"), ensure_ascii=False))
    for r in (lida_comum, enviada):
        dentro, fora = g9.dentro_e_fora(r)
        assert any("senha do banco" in t for t in dentro)
        for t in fora:
            _sem_pedido_de_senha(t)
    # O extrato: as linhas do pedido barrado, sem o texto nem o link (todas as linhas desde o comeco das chamadas que
    # sao da Ana, do aplicativo dela ou destes dois servidores).
    linhas = [
        ln
        for ln in await gw.extrato(depois_de=extrato_antes)
        if ln.pessoa_id == ana.id or ln.aplicativo_id == app.id or ln.sistema_id in (s_duble, s_falante)
    ]
    tipos = [ln.tipo for ln in linhas]
    assert "leitura_sistema_pediu_dados" in tipos
    for ln in linhas:
        _sem_pedido_de_senha(ln.frase + json.dumps(ln.dados, ensure_ascii=False))
    # As telas da pessoa: nada do pedido de senha, nem do texto comum; a acao enviada diz que foi entregue marcada.
    paginas = await _telas_da_pessoa(gw, ana, op_acao, op_recado)
    for html in paginas:
        _sem_pedido_de_senha(html)
    assert any(AVISO_A4 in html for html in paginas)
    # Nenhum aviso novo (e-mail ou webhook) por causa disso.
    assert await gw.avisos_depois(ultimo_aviso) == []


# --- 7. D31: a resposta de terceiro chega inteira dentro da marca ------------------------------------------------


def _so_dentro(resultado: Any, *trechos: str) -> None:
    """Cada trecho aparece na resposta, e so dentro da moldura; nada do sistema fica fora dela."""
    dentro, fora = g9.dentro_e_fora(resultado)
    assert dentro
    for trecho in trechos:
        assert any(trecho in t for t in dentro), trecho
        assert all(trecho not in t for t in fora), (trecho, fora)
    for t in fora:
        assert tp.MARCA not in t
        assert "phish.exemplo" not in t


# As chaves do _meta que o MCP-CLARO escreve, conforme o codigo de hoje: na leitura (moldura e despachante), na
# resposta de uma acao enviada (mais o protocolo) e na consulta pelo protocolo (sem o selo).
META_LEITURA = frozenset({"origem", "texto_de_estranhos", "marca", "sistema", "selo"})
META_ACAO = META_LEITURA | {"protocolo", "estado", "codigo_conferencia"}
META_CONSULTA = META_ACAO - {"selo"}


def _nada_do_sistema_fora_dos_blocos(resultado: Any, sistema: str, chaves: frozenset[str] = META_LEITURA) -> None:
    """Nada do sistema fora dos blocos de texto, e o ``_meta`` com exatamente as nossas ``chaves``: o terceiro forja
    chaves do nosso prefixo que nao escrevemos (``tp.FORJADO``), e conferir so o prefixo deixaria passar."""
    bruto = resultado.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert "structuredContent" not in bruto
    for bloco in bruto["content"]:
        assert set(bloco) == {"type", "text"}, bloco  # sem annotations, _meta, uri, imagem ou link
        assert bloco["type"] == "text"
    meta = dict(bruto.get("_meta") or {})
    meta.pop("io.modelcontextprotocol/serverInfo", None)
    assert set(meta) == {PREFIXO_META + k for k in chaves}, meta
    assert tp.MARCA not in json.dumps(meta)
    for chave, forjado in tp.FORJADO.items():
        assert meta.get(chave) != forjado, chave
    assert meta[PREFIXO_META + "sistema"] == sistema
    assert meta[estranhos.META_MARCA] != "0123456789ab"  # a marca forjada pelo terceiro nao vale
    assert meta[estranhos.META_ORIGEM] == "terceiro"
    assert meta[estranhos.META_TEXTO_DE_ESTRANHOS] is True
    texto = json.dumps(bruto, ensure_ascii=False)
    assert "111.444.777-35" not in texto  # CPF mascarado tambem dentro da moldura
    assert "ana.terceira@fornecedor.test" not in texto
    assert tp.IMAGEM not in texto


async def test_resposta_de_terceiro_chega_inteira_dentro_da_marca_com_erro_e_dados_estruturados(
    gw: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D31 (provas 8 a 10 do mapa de estranhos), pela entrada, pelo despachante e pela sala: (8) resultado com erro
    do proprio sistema (``isError``) chega com o erro e o texto emoldurado; (9) ``structuredContent`` chega como
    texto emoldurado e o campo some; (10) nada do sistema passa fora da moldura: nem fim de moldura forjado, nem
    caractere escondido, nem ``_meta``, ``annotations``, ``uri``, imagem ou link de recurso; o mesmo na resposta de
    uma acao enviada, depois do bloco do protocolo."""
    k_ana_t, k_decl_t = _chave("ana_t"), _chave("decl_t")
    _chaves(monkeypatch, {k_ana_t: "ana", k_decl_t: "admin"})
    terceiro = await gw.servir(tp.criar(), gravar=True)
    admin = await gw.admin()
    sistema_id = await admin.terceiro(
        terceiro,
        # Sem campos pessoais na leitura: com eles, a leitura daria o segundo sinal e a acao iria para a ficha.
        {"procurar": {}, "enviar_recado": {"classe": "muda"}},
        chave_declaracoes=k_decl_t,
    )
    nome = (await gw.sistema(sistema_id)).nome
    procurar, recado = await gw.revisada(sistema_id, "procurar"), await gw.revisada(sistema_id, "enviar_recado")
    app = await gw.aplicativo("Assistente")
    ana = await gw.pessoa("ana")
    await gw.dar_ordem(ana, app, await admin.modelo([procurar.id, recado.id]))
    await gw.fora_da_experiencia(ana)
    await gw.chave_da_pessoa(sistema_id, ana, k_ana_t)
    await gw.limite(ana, app, sistema_id, "enviar_recado")

    # (8) e (9): erro do sistema, com dados estruturados e imagem.
    await terceiro.modo("erro_estruturado")
    r = await gw.chamar(ana, app, procurar.nome_vitrine, {"termo": "oi"})
    assert not isinstance(r, MCPError), r
    assert r.is_error is True
    assert g9.textos(r)[0].startswith(f"MCP-CLARO: o texto entre [[estranhos {g9.marca_de(r)}]]")
    assert "servidor de terceiros" in g9.textos(r)[0]
    _so_dentro(r, "Falhou do lado do fornecedor.", tp.INSTRUCAO, '"erro"')
    _nada_do_sistema_fora_dos_blocos(r, nome)
    assert MOTIVO not in (r.meta or {})  # erro do sistema, nao nosso

    # (10) tudo o que o sistema tenta por fora da moldura.
    await terceiro.modo("tudo_fora")
    r = await gw.chamar(ana, app, procurar.nome_vitrine, {"termo": "oi"})
    assert not isinstance(r, MCPError), r
    assert r.is_error is False
    _so_dentro(r, "Resposta do fornecedor.", "Recurso embutido.", '"recado"', "[U+202E]", "[U+E0041]")
    _nada_do_sistema_fora_dos_blocos(r, nome)
    juntos = "\n".join(g9.textos(r))
    assert tp.FIM_FORJADO not in juntos  # o fim de moldura forjado foi neutralizado
    assert "\u202e" not in juntos
    assert "\U000e0041" not in juntos
    # O fim de moldura de verdade so aparece no aviso nosso e no fim de cada bloco emoldurado.
    assert juntos.count(f"[[/estranhos {g9.marca_de(r)}]]") == 1 + len(g9.dentro_e_fora(r)[0])
    marcas = {g9.marca_de(r)}

    # A mesma coisa numa acao enviada (por um limite, sem espera): o protocolo primeiro, depois o aviso e a moldura.
    async with gw.politica(OUTBOX_HOLD_SECONDS=0):
        r = await gw.chamar(ana, app, recado.nome_vitrine, {"destinatario": "equipe", "texto": "oi"})
    assert not isinstance(r, MCPError), r
    op = await _operacao(gw, g9.protocolo(r))
    assert op.estado == Estado.ENVIADA
    textos = g9.textos(r)
    assert textos[0] == f"Protocolo {op.protocolo}: enviada uma vez ao sistema."
    assert textos[1].startswith("MCP-CLARO: o texto entre [[estranhos ")
    _so_dentro(r, "Resposta do fornecedor.", "Recurso embutido.", '"recado"', "[U+202E]")
    _nada_do_sistema_fora_dos_blocos(r, nome, META_ACAO)
    assert (r.meta[PREFIXO_META + "protocolo"], r.meta[PREFIXO_META + "estado"]) == (op.protocolo, "enviada")
    marcas.add(g9.marca_de(r))
    assert len(marcas) == 2  # uma marca nova a cada resposta
    # A consulta pelo protocolo devolve o mesmo, emoldurado de novo, com outra marca.
    async with gw.cliente(ana, app) as c:
        consulta = await c.call_tool(CONSULTA, {"protocolo": op.protocolo})
    _so_dentro(consulta, "Resposta do fornecedor.", '"recado"')
    _nada_do_sistema_fora_dos_blocos(consulta, nome, META_CONSULTA)
    assert consulta.meta is not None
    assert (consulta.meta[PREFIXO_META + "protocolo"], consulta.meta[PREFIXO_META + "estado"]) == (
        op.protocolo,
        "enviada",
    )
    assert g9.marca_de(consulta) not in marcas
    # Guardado sem moldura (a moldura nasce a cada entrega).
    assert "[[estranhos" not in json.dumps(op.resultado)
    assert re.search(r"\[\[estranhos [0-9a-f]{12}\]\]", json.dumps(r.model_dump(by_alias=True)))


# --- Fontes destas provas ---------------------------------------------------------------------------------------


def test_fontes_das_provas_sem_caractere_escondido_cru() -> None:
    """Os caracteres escondidos que as provas mandam (U+202E, U+E0041) ficam escritos como escape no codigo: cru,
    um RLO inverte a linha no editor e na revisao do diff (Trojan Source)."""
    raiz = Path(__file__).resolve().parents[2]
    for caminho in (
        "tests/integracao/test_fase3_provas_de_saida.py",
        "tests/apoio_g9.py",
        "exemplos/terceiro_provas.py",
    ):
        for numero, linha in enumerate((raiz / caminho).read_text(encoding="utf-8").splitlines(), 1):
            crus = [f"U+{ord(ch):04X}" for ch in linha if unicodedata.category(ch) in ("Cf", "Co", "Cs", "Cn")]
            assert not crus, (caminho, numero, crus)
