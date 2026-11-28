"""``selo/declaracao.py`` (desenho 2.5; SELO-0.2; criticas A4, A3 e M6): a declaracao de teste, sem banco.

Recusa (desenho 12.1, G2): ``ambiente_de_teste`` com o endereco de producao (tambem escrito de outro jeito, e o de
outro sistema); http fora de desenvolvimento e teste; ferramenta de acao sem entrada; ferramenta de teste com o nome
de uma ferramenta de producao; rastro sem ponteiro (e rastro que nao le); mais de 64 KiB; terceiro (ate o G7). E a
forma: nada a mais, tipos estritos, JSON sem chave repetida, sem o caractere nulo e sem niveis demais.

O alvo escrito de outro jeito (critica A4): o nome do alvo vai em ASCII e o IP por inteiro (pontos de outros
alfabetos e IPv4 curto, decimal ou hexadecimal sao recusados na forma), e a comparacao com a producao usa tambem o
nome que o cliente conecta (``forma_de_conexao``), para a producao escrita de outro jeito no cadastro.

A parte que le e grava no banco (``cadastro_do_sistema``, ``salvar``, ``ultima``) esta em
``tests/integracao/test_selo_declaracao_banco.py``.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx2
import pytest

from mcp_claro.config.settings import Ambiente
from mcp_claro.selo import contrato
from mcp_claro.selo import declaracao as d
from tests import apoio_selos as apoio

PRODUCAO = "https://cobranca.exemplo.teste/mcp"
OUTRO_SISTEMA = "https://outro.exemplo.teste/api/mcp"
ACOES = frozenset({"enviar_lembrete", "apagar_lembrete"})
LEITURAS = frozenset({"listar_lembretes", "ver_lembrete"})
INVERTE = chr(0x202E)  # inverte a direcao do texto (categoria Cf)


def cadastro(**mudar: Any) -> d.Cadastro:
    base: dict[str, Any] = {
        "de_terceiro": False,
        "tipo_chave": "por_pessoa",
        "acoes": ACOES,
        "leituras": LEITURAS,
        "revisadas": ACOES | LEITURAS | {"antiga_retirada"},
        "producoes": (PRODUCAO, OUTRO_SISTEMA),
    }
    return d.Cadastro(**(base | mudar))


def dados(modo: str = "ambiente_de_teste", **mudar: Any) -> dict[str, Any]:
    base = apoio.declaracao(sorted(ACOES), modo=modo)
    base.update(mudar)
    return base


def recusa(
    motivo: str, dados_: dict[str, Any], *, cad: d.Cadastro | None = None, ambiente: Ambiente = Ambiente.PRODUCAO
) -> d.DeclaracaoRecusada:
    with pytest.raises(d.DeclaracaoRecusada) as erro:
        d.conferir(d.validar(dados_), cad or cadastro(), ambiente=ambiente)
    assert erro.value.motivo == motivo, erro.value.mensagem
    assert erro.value.motivo in d.MOTIVOS
    return erro.value


def recusa_na_forma(dados_: dict[str, Any], trecho: str) -> d.DeclaracaoRecusada:
    with pytest.raises(d.DeclaracaoRecusada) as erro:
        d.validar(dados_)
    assert erro.value.motivo == "forma"
    assert trecho in erro.value.mensagem, erro.value.mensagem
    return erro.value


def mudar_ferramenta(base: dict[str, Any], nome: str, mudanca: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    novo = copy.deepcopy(base)
    mudanca(novo["ferramentas"][nome])
    return novo


# --- O que passa -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("modo", contrato.MODOS)
@pytest.mark.parametrize("ambiente", list(Ambiente))
def test_declaracao_certa_passa(modo: str, ambiente: Ambiente) -> None:
    modelo = d.validar(dados(modo))
    d.conferir(modelo, cadastro(), ambiente=ambiente)
    assert modelo.guardada() == dados(modo)  # o que vai ao banco e o que foi declarado
    assert d.validar(modelo.guardada()) == modelo
    assert d.ler_texto(json.dumps(dados(modo), indent=2)) == modelo


def test_campos_opcionais_completos_passam() -> None:
    base = dados()
    entrada = base["ferramentas"]["enviar_lembrete"]
    entrada["efeito_adiado"] = {
        "argumentos": {"para": "x@exemplo.teste", "texto": "t", "daqui_a": 120},
        "segundos": 120,
    }
    entrada["escrita_lenta"] = {"argumentos": {"para": "x@exemplo.teste", "texto": "lento"}, "segundos": 5}
    entrada["varios_itens"] = {"argumentos": {"itens": [{"para": "a"}, {"para": ""}]}, "itens": 2}
    entrada["pede_dados"] = {"argumentos": {"para": "x@exemplo.teste"}}
    entrada["idempotente"] = {"rastro": {"ferramenta": "ver_lembrete", "argumentos": {"id": "1"}, "conta": "/lidos"}}
    entrada["conflito"] = {
        "leitura": {
            "ferramenta": "ver_lembrete",
            "argumentos": {"id": "1"},
            "versao": "/versao",
            "campo": "/titulo",
            "campo_concorrente": "/texto",
        },
        "argumento_versao": "versao",
        "argumentos": {"id": "1", "titulo": "A"},
        "argumentos_concorrente": {"id": "1", "texto": "B"},
    }
    modelo = d.validar(base)
    d.conferir(modelo, cadastro(), ambiente=Ambiente.PRODUCAO)
    assert modelo.ferramentas["enviar_lembrete"].leituras() == ["listar_lembretes", "ver_lembrete", "ver_lembrete"]
    assert modelo.guardada() == base


# --- Desenho 12.1 G2 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alvo",
    [
        PRODUCAO,
        PRODUCAO + "/",
        "https://COBRANCA.Exemplo.TESTE/mcp",
        "https://cobranca.exemplo.teste:443/mcp",
        "https://cobranca.exemplo.teste:0443/mcp",
        "https://cobranca.exemplo.teste./mcp",
        "https://cobranca.exemplo.teste//mcp//",
        "https://cobranca.exemplo.teste/MCP",
        "https://cobranca.exemplo.teste/%6dcp",
        "https://cobranca.exemplo.teste/teste/../mcp",
        OUTRO_SISTEMA,
        "https://outro.exemplo.teste/api/./mcp",
    ],
)
def test_ambiente_de_teste_com_o_endereco_de_producao_e_recusado(alvo: str) -> None:
    erro = recusa("alvo_na_producao", dados(endereco_teste=alvo))
    assert "nunca pode ser a produção" in erro.mensagem


def test_alvo_parecido_mas_outro_passa() -> None:
    for alvo in ("https://teste.cobranca.exemplo.teste/mcp", "https://cobranca.exemplo.teste:8443/mcp"):
        d.conferir(d.validar(dados(endereco_teste=alvo)), cadastro(), ambiente=Ambiente.PRODUCAO)


@pytest.mark.parametrize("ambiente", [Ambiente.PILOTO, Ambiente.PRODUCAO])
def test_http_fora_de_desenvolvimento_e_teste_e_recusado(ambiente: Ambiente) -> None:
    erro = recusa("alvo_sem_https", dados(endereco_teste="http://teste.exemplo.teste/mcp"), ambiente=ambiente)
    assert erro.mensagem == "Fora de desenvolvimento e teste, o alvo de teste usa https."


@pytest.mark.parametrize("ambiente", [Ambiente.DESENVOLVIMENTO, Ambiente.TESTE])
def test_http_vale_em_desenvolvimento_e_teste(ambiente: Ambiente) -> None:
    d.conferir(d.validar(dados(endereco_teste="http://127.0.0.1:8123/mcp")), cadastro(), ambiente=ambiente)


def test_http_da_producao_continua_recusado_em_teste() -> None:
    cad = cadastro(producoes=("http://127.0.0.1:8123/selo-x/mcp",))
    recusa(
        "alvo_na_producao",
        dados(endereco_teste="http://127.0.0.1:8123/selo-x/mcp/"),
        cad=cad,
        ambiente=Ambiente.TESTE,
    )


# --- Alvo escrito de outro jeito (critica A4): o cliente conecta no mesmo lugar --------------------------------

PONTOS_DE_OUTROS_ALFABETOS = ("\u3002", "\uff0e", "\uff61")


@pytest.mark.parametrize(
    "alvo",
    [
        *(PRODUCAO.replace(".exemplo.", ponto + "exemplo" + ponto) for ponto in PONTOS_DE_OUTROS_ALFABETOS),
        "https://cobran\u00e7a.exemplo.teste/mcp",
        "https://\uff43obranca.exemplo.teste/mcp",
        "https://cobranca.ex\u0435mplo.teste/mcp",
        "http://127.1:8123/mcp",
        "http://2130706433:8123/mcp",
        "http://0x7f.0.0.1:8123/mcp",
        "http://0x7f000001:8123/mcp",
        "http://0177.0.0.1:8123/mcp",
        "http://127.000.000.001:8123/mcp",
        "http://127.0.0.1.:8123/mcp",
        "https://10.5/mcp",
        "https://0x0a000005/mcp",
        "http://[0:0:0:0:0:0:0:1]:8123/mcp",
        "http://[fe80::1%25eth0]:8123/mcp",
        "http://127%2e0.0.1:8123/mcp",
        "https://cobranca.exemplo.teste%2e/mcp",
        "https://cobranca!exemplo.teste/mcp",
        # Retoque G02r: a mesma regra do banco (selo_alvo_bem_escrito). O sinal de kelvin vira "k" nas minusculas de
        # urlsplit; o IPv6 da forma antiga ::a.b.c.d, o Python e o banco escrevem de jeitos diferentes.
        "https://\u212a.exemplo.teste/mcp",
        "https://\u212aobranca.exemplo.teste:8443/mcp",
        "http://[::a00:5]:8123/mcp",
        "http://[::10.0.0.5]:8123/mcp",
        "http://[::1:0]:8123/mcp",
        # Retoque 2 do G02r: o IPv6 mapeado leva um IPv4 dentro, e o cliente conecta no proprio IPv4 (a producao
        # 10.0.0.5, ou a maquina local, escrita de outro jeito). O banco comparava o texto e o guardava.
        "http://[::ffff:10.0.0.5]/mcp",
        "http://[::ffff:127.0.0.1]:8123/mcp",
        "http://[::ffff:0.0.0.0]:8123/mcp",
    ],
)
def test_alvo_com_o_nome_escrito_de_outro_jeito_e_recusado_na_forma(alvo: str) -> None:
    """O texto guardado tem de ser o nome que o cliente conecta: ``httpx2`` troca os pontos de outros alfabetos e o
    resolvedor le ``127.1`` como 127.0.0.1. Sem esta regra, o banco (que compara texto) via outro endereco."""
    erro = recusa_na_forma(dados(endereco_teste=alvo), "escrito de um jeito só")
    assert "Campo: endereco_teste." in erro.mensagem
    with pytest.raises(d.DeclaracaoRecusada, match="escrito de um jeito só"):
        d.ler_texto(json.dumps(dados(endereco_teste=alvo)))


@pytest.mark.parametrize(
    "alvo",
    [
        "https://teste.cobranca.exemplo.teste/mcp",
        "https://xn--cobrana-zxa.exemplo.teste/mcp",
        "https://Teste.Cobranca.Exemplo.Teste./mcp",
        "https://teste_1.exemplo.teste:8443/mcp",
        "http://127.0.0.1:8124/mcp",
        "http://10.0.0.5/mcp",
        "http://[::1]:8124/mcp",
        "http://[2001:db8::1]:8124/mcp",
        "http://[::ffff]/mcp",
        "http://[::ffff:0:a00:5]/mcp",
        "http://[::100]/mcp",
        "http://[::]:8124/mcp",
    ],
)
def test_alvo_com_o_nome_escrito_de_um_jeito_so_passa(alvo: str) -> None:
    d.conferir(
        d.validar(dados(endereco_teste=alvo)),
        cadastro(producoes=(PRODUCAO, "http://127.0.0.1:8123/mcp")),
        ambiente=Ambiente.TESTE,
    )
    # O nome do texto e o que o cliente conecta (o leitor do ``httpx2``).
    assert httpx2.URL(alvo).raw_host.decode("ascii").lower() == urlsplit(alvo).hostname


NO_MESMO_LUGAR: Final = (
    # A producao escrita de outro jeito no cadastro (a tela do cadastro aceita nome com acento num proprio) e o
    # alvo na forma que o cliente conecta: a comparacao de texto nao pega, a forma de conexao pega.
    ("https://cobran\u00e7a.exemplo.teste/mcp", "https://xn--cobrana-zxa.exemplo.teste/mcp"),
    ("https://cobranca\u3002exemplo\u3002teste/mcp", PRODUCAO),
    ("https://cobranca\uff0eexemplo\uff0eteste/mcp/", PRODUCAO),
    ("http://127.1:8123/mcp", "http://127.0.0.1:8123/mcp"),
    ("http://2130706433:8123/mcp", "http://127.0.0.1:8123/mcp"),
    ("http://0x7f.0.0.1:8123/mcp", "http://127.0.0.1:8123/mcp"),
    ("https://10.5/mcp", "https://10.0.0.5/mcp"),
    ("https://0x0a000005:443/mcp", "https://10.0.0.5/mcp"),
    ("http://[0:0:0:0:0:0:0:1]:8123/mcp", "http://[::1]:8123/mcp"),
    ("http://[::ffff:10.0.0.5]:8123/mcp", "http://10.0.0.5:8123/mcp"),
    # A mesma maquina: localhost, todo loopback e o IP nao especificado.
    ("http://127.0.0.1:8123/mcp", "http://localhost:8123/mcp"),
    ("http://127.0.0.1:8123/mcp", "http://api.localhost:8123/mcp"),
    ("http://127.0.0.1:8123/mcp", "http://127.0.0.2:8123/mcp"),
    ("http://127.0.0.1:8123/mcp", "http://[::1]:8123/mcp"),
    ("http://127.0.0.1:8123/mcp", "http://0.0.0.0:8123/mcp"),
    ("http://127.0.0.1:8123/mcp", "http://[::]:8123/mcp"),
    ("http://[::ffff:127.0.0.1]:8123/mcp", "http://localhost:8123/mcp"),
    ("http://localhost:8123/mcp", "http://x.localhost:8123/mcp"),
    ("http://0x7f000001:8123/mcp", "http://127.0.0.1:8123/mcp"),
    # O mesmo nome na mesma porta, com o outro esquema: o mesmo servico.
    ("https://cobranca.exemplo.teste/mcp", "http://cobranca.exemplo.teste:443/mcp"),
    ("http://127.0.0.1:8123/mcp", "https://127.0.0.1:8123/mcp"),
)
"""Producao e alvo (escrito de um jeito so) que chegam ao mesmo servico. O banco confere a mesma lista
(``test_selo_declaracao_banco.py``): ele pega todos, menos o primeiro (o nome fora de ASCII, que so a tela le)."""

EM_OUTRO_LUGAR: Final = (
    ("http://127.0.0.1:8123/mcp", "http://127.0.0.1:8124/mcp"),
    ("http://127.0.0.1:8123/selo-x/mcp", "http://127.0.0.1:8123/selo-x/teste/mcp"),
    ("http://localhost:8123/mcp", "http://10.0.0.5:8123/mcp"),
    ("https://cobranca.exemplo.teste/mcp", "https://cobranca.exemplo.teste:8443/mcp"),
    ("https://10.0.0.5/mcp", "https://10.0.0.6/mcp"),
    ("https://cobranca.exemplo.teste/mcp", "http://cobranca.exemplo.teste/mcp"),
    ("http://localhost:8123/mcp", "http://localhost.exemplo.teste:8123/mcp"),
    ("http://10.0.0.5:8123/mcp", "http://[::ffff]:8123/mcp"),
    ("http://10.0.0.5:8123/mcp", "http://[::ffff:0:a00:5]:8123/mcp"),
)
"""Producao e alvo em lugares diferentes: a tela e o banco deixam passar."""


@pytest.mark.parametrize(("producao", "alvo"), NO_MESMO_LUGAR)
def test_alvo_que_conecta_na_producao_e_recusado(producao: str, alvo: str) -> None:
    assert not contrato.mesmo_endereco(producao, alvo)  # a comparacao de texto, sozinha, deixava passar
    erro = recusa(
        "alvo_na_producao", dados(endereco_teste=alvo), cad=cadastro(producoes=(producao,)), ambiente=Ambiente.TESTE
    )
    assert "jeito de escrever o nome ou o IP" in erro.mensagem
    assert d.alvo_na_producao(alvo, [producao])


@pytest.mark.parametrize(("producao", "alvo"), EM_OUTRO_LUGAR)
def test_alvo_em_outro_lugar_passa(producao: str, alvo: str) -> None:
    assert not d.alvo_na_producao(alvo, [producao])
    d.conferir(d.validar(dados(endereco_teste=alvo)), cadastro(producoes=(producao,)), ambiente=Ambiente.TESTE)


def test_forma_de_conexao() -> None:
    assert d.forma_de_conexao("https://Cobranca\u3002Exemplo.TESTE./a//b/") == ("cobranca.exemplo.teste", 443, "/a/b")
    assert d.forma_de_conexao("http://0x0a000005:0080/mcp") == ("10.0.0.5", 80, "/mcp")
    assert d.forma_de_conexao("http://[::ffff:127.0.0.1]:8123") == ("localhost", 8123, "")
    assert d.forma_de_conexao("http://cobran\u00e7a.exemplo:8123/") == ("xn--cobrana-zxa.exemplo", 8123, "")
    assert d.forma_de_conexao("http://\u2603.exemplo/mcp") is None  # o IDNA recusa: o cliente nao conecta
    assert d.forma_de_conexao("https://\uff23obranca.exemplo/mcp") is None
    assert d.forma_de_conexao("ftp://cobranca.exemplo/mcp") is None


def test_ferramenta_de_acao_sem_entrada_e_recusada() -> None:
    base = dados()
    del base["ferramentas"]["enviar_lembrete"]
    erro = recusa("selo_declaracao_incompleta", base)
    assert erro.ferramenta == "enviar_lembrete"
    assert erro.mensagem == (
        "A declaração não cobre a ferramenta enviar_lembrete. Toda ferramenta de ação precisa estar nela."
    )


@pytest.mark.parametrize("nome", ["listar_lembretes", "nao_existe"])
def test_entrada_que_nao_e_ferramenta_de_acao_e_recusada(nome: str) -> None:
    base = dados()
    base["ferramentas"][nome] = copy.deepcopy(base["ferramentas"]["enviar_lembrete"])
    erro = recusa("ferramenta_que_nao_e_acao", base)
    assert erro.ferramenta == nome


@pytest.mark.parametrize("de_teste", ["listar_lembretes", "ver_lembrete", "antiga_retirada", "apagar_lembrete"])
def test_ferramenta_de_teste_com_nome_de_ferramenta_de_producao_e_recusada(de_teste: str) -> None:
    base = mudar_ferramenta(
        dados("ferramentas_de_teste"), "enviar_lembrete", lambda f: f.update(ferramenta_de_teste=de_teste)
    )
    if de_teste == "apagar_lembrete":  # o nome de outra ferramenta de acao da declaracao: a forma ja recusa
        recusa_na_forma(base, "tem o nome de uma ferramenta de ação")
        return
    erro = recusa("ferramenta_de_teste_real", base)
    assert de_teste in erro.mensagem
    assert erro.ferramenta == "enviar_lembrete"


def test_ferramenta_de_teste_com_o_proprio_nome_e_recusada() -> None:
    base = mudar_ferramenta(
        dados("ferramentas_de_teste"), "enviar_lembrete", lambda f: f.update(ferramenta_de_teste="enviar_lembrete")
    )
    recusa_na_forma(base, "tem o nome de uma ferramenta de ação: enviar_lembrete")


def test_ferramenta_de_teste_repetida_e_recusada() -> None:
    base = dados("ferramentas_de_teste")
    for f in base["ferramentas"].values():
        f["ferramenta_de_teste"] = "uma_so_de_teste"
    recusa_na_forma(base, "têm a mesma ferramenta de teste")


@pytest.mark.parametrize(
    ("mudanca", "trecho"),
    [
        (lambda r: r.pop("conta"), "falta o campo ferramentas.enviar_lembrete.rastro.conta"),
        (lambda r: r.update(conta="itens"), "ponteiro JSON"),
        (lambda r: r.update(conta=""), "ponteiro JSON"),
        (lambda r: r.update(conta="/a/~2b"), "ponteiro JSON"),
        (lambda r: r.update(conta="/" + "a" * 1024), "ponteiro JSON"),
        (lambda r: r.update(conta=3), "tipo errado"),
    ],
)
def test_rastro_sem_ponteiro_e_recusado(mudanca: Callable[[dict[str, Any]], Any], trecho: str) -> None:
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: mudanca(f["rastro"]))
    erro = recusa_na_forma(base, trecho)
    assert "rastro" in erro.mensagem


def test_ponteiros_validos_com_escapes_passam() -> None:
    for conta in ("/itens", "/a~1b/~0c", "/0", "/", "//"):
        base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f, c=conta: f["rastro"].update(conta=c))
        assert d.validar(base).ferramentas["enviar_lembrete"].rastro.conta == conta


@pytest.mark.parametrize("leitura", ["enviar_lembrete", "apagar_lembrete", "nao_existe", "antiga_retirada"])
def test_rastro_que_nao_e_uma_revisada_que_le_e_recusado(leitura: str) -> None:
    """Critica A4.2: uma ferramenta de acao declarada como rastro rodaria antes e depois de cada escrita."""
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f["rastro"].update(ferramenta=leitura))
    erro = recusa("leitura_que_nao_le", base)
    assert leitura in erro.mensagem
    assert "só lê" in erro.mensagem


@pytest.mark.parametrize("onde", ["conflito", "idempotente"])
def test_as_outras_leituras_tambem_so_leem(onde: str) -> None:
    def por(f: dict[str, Any]) -> None:
        if onde == "conflito":
            f["conflito"] = {
                "leitura": {"ferramenta": "apagar_lembrete", "versao": "/v", "campo": "/a", "campo_concorrente": "/b"},
                "argumento_versao": "versao",
                "argumentos": {"a": 1},
                "argumentos_concorrente": {"b": 2},
            }
        else:
            f["idempotente"] = {"rastro": {"ferramenta": "apagar_lembrete", "conta": "/n"}}

    recusa("leitura_que_nao_le", mudar_ferramenta(dados(), "enviar_lembrete", por))


def test_mais_de_64_kib_e_recusado() -> None:
    teto = contrato.TETO_DECLARACAO_BYTES
    base = dados()
    nomes = sorted(ACOES)
    # Cada argumento ate 16 KiB; varias entradas somam mais de 64 KiB.
    for i in range(5):
        base["ferramentas"][f"a{i}"] = copy.deepcopy(base["ferramentas"][nomes[0]])
        base["ferramentas"][f"a{i}"]["argumentos"] = {"texto": "x" * 15000}
    assert len(json.dumps(base).encode()) > teto
    with pytest.raises(d.DeclaracaoRecusada) as erro:
        d.validar(base)
    assert erro.value.motivo == "grande_demais"
    assert "64 KiB" in erro.value.mensagem
    with pytest.raises(d.DeclaracaoRecusada) as erro:
        d.ler_texto(json.dumps(base))
    assert erro.value.motivo == "grande_demais"


def test_no_teto_exato_passa_e_um_byte_a_mais_nao() -> None:
    teto = contrato.TETO_DECLARACAO_BYTES
    base = dados()
    for i in range(6):
        base["ferramentas"][f"a{i}"] = copy.deepcopy(base["ferramentas"]["enviar_lembrete"])
        base["ferramentas"][f"a{i}"]["argumentos"] = {"texto": ""}

    def tamanho() -> int:
        return len(json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode())

    for i in range(6):  # enche cada argumento ate 15000 letras, sem passar do teto da declaracao
        falta = min(15000, teto - tamanho())
        base["ferramentas"][f"a{i}"]["argumentos"]["texto"] = "x" * falta
    assert tamanho() == teto
    texto = json.dumps(base, ensure_ascii=False, separators=(",", ":"))
    assert d.ler_texto(texto) == d.validar(base)
    base["ferramentas"]["a5"]["argumentos"]["texto"] += "x"
    with pytest.raises(d.DeclaracaoRecusada, match="64 KiB"):
        d.validar(base)
    # No texto colado, espacos tambem contam (o teto e do que o ADMIN cola).
    with pytest.raises(d.DeclaracaoRecusada, match="64 KiB"):
        d.ler_texto(texto[:-1] + " }")


def test_argumentos_de_mais_de_16_kib_sao_recusados() -> None:
    teto = contrato.TETO_ARGUMENTOS_BYTES
    no_teto = {"t": "x" * (teto - len('{"t":""}'))}
    assert len(json.dumps(no_teto, separators=(",", ":")).encode()) == teto
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f.update(argumentos=no_teto))
    d.validar(base)
    acima = {"t": no_teto["t"] + "x"}
    for campo in ("argumentos", "argumentos_outros", "argumentos_recusados"):
        base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f, c=campo: f.update({c: acima}))
        recusa_na_forma(base, "16 KiB")
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f["rastro"].update(argumentos=acima))
    recusa_na_forma(base, "16 KiB")


def test_terceiro_e_recusado_ate_o_g7() -> None:
    erro = recusa("selo_terceiro_ainda_nao", dados(), cad=cadastro(de_terceiro=True))
    assert erro.mensagem == "O selo de servidores de terceiros ainda não está disponível."
    assert erro.mensagem == d.TERCEIRO_AINDA_NAO
    # E a primeira recusa: mesmo com o resto errado, o ADMIN le que o selo de terceiro nao existe ainda.
    base = dados(endereco_teste=PRODUCAO)
    del base["ferramentas"]["enviar_lembrete"]
    recusa("selo_terceiro_ainda_nao", base, cad=cadastro(de_terceiro=True))
    with pytest.raises(d.DeclaracaoRecusada, match="terceiros ainda não"):
        d.recusar_terceiro(True)
    d.recusar_terceiro(False)


def test_selo_so_para_chave_por_pessoa_e_com_acao() -> None:
    for tipo in ("compartilhada", "programa"):
        recusa("sem_chave_por_pessoa", dados(), cad=cadastro(tipo_chave=tipo))
    recusa("sem_acoes", dados(), cad=cadastro(acoes=frozenset()))


# --- Modo, alvo e forma ----------------------------------------------------------------------------------------


def test_modo_ambiente_sem_alvo_e_recusado() -> None:
    base = dados()
    del base["endereco_teste"]
    recusa_na_forma(base, "traz o endereço do alvo de teste")
    recusa_na_forma(dados(endereco_teste=None), "traz o endereço do alvo de teste")


def test_modo_ferramentas_com_alvo_e_recusado() -> None:
    recusa_na_forma(dados("ferramentas_de_teste", endereco_teste="https://t.exemplo.teste/mcp"), "não traz endereço")


def test_modo_ferramentas_exige_a_ferramenta_de_teste_e_o_ambiente_a_recusa() -> None:
    base = mudar_ferramenta(dados("ferramentas_de_teste"), "enviar_lembrete", lambda f: f.pop("ferramenta_de_teste"))
    recusa_na_forma(base, "traz a ferramenta de teste dela")
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f.update(ferramenta_de_teste="enviar_teste"))
    recusa_na_forma(base, "não traz ferramenta de teste")


@pytest.mark.parametrize(
    "alvo",
    [
        "ftp://teste.exemplo.teste/mcp",
        "HTTPS://teste.exemplo.teste/mcp",
        "https://teste.exemplo.teste/mcp?ambiente=teste",
        "https://teste.exemplo.teste/mcp#x",
        "https://usuario:senha@teste.exemplo.teste/mcp",
        "https:///mcp",
        "https://teste.exemplo.teste:99999/mcp",
        "https://teste.exemplo.teste/m cp",
        "https://teste.exemplo.teste/mcp" + INVERTE,
        " https://teste.exemplo.teste/mcp",
        "https://" + "a" * 2048 + ".teste/mcp",
    ],
)
def test_alvo_fora_da_forma_e_recusado(alvo: str) -> None:
    recusa_na_forma(dados(endereco_teste=alvo), "alvo de teste é um endereço")


@pytest.mark.parametrize(
    ("onde", "trecho"),
    [
        ("raiz", "o contrato não tem o campo extra"),
        ("ferramenta", "o contrato não tem o campo ferramentas.enviar_lembrete.extra"),
        ("rastro", "o contrato não tem o campo ferramentas.enviar_lembrete.rastro.extra"),
    ],
)
def test_campo_a_mais_e_recusado_em_todo_nivel(onde: str, trecho: str) -> None:
    base = dados()
    alvo = {
        "raiz": base,
        "ferramenta": base["ferramentas"]["enviar_lembrete"],
        "rastro": base["ferramentas"]["enviar_lembrete"]["rastro"],
    }[onde]
    alvo["extra"] = 1
    recusa_na_forma(base, trecho)


@pytest.mark.parametrize(
    ("mudar", "trecho"),
    [
        ({"contrato": "selo/2"}, "não é um dos do contrato, no campo contrato"),
        ({"modo": "producao"}, "não é um dos do contrato, no campo modo"),
        ({"guarda_dias": 29}, "pequeno demais no campo guarda_dias"),
        ({"guarda_dias": "30"}, "tipo errado, no campo guarda_dias"),
        ({"guarda_dias": True}, "tipo errado, no campo guarda_dias"),
        ({"guarda_dias": 30.0}, "tipo errado, no campo guarda_dias"),
        ({"guarda_dias": 3651}, "grande demais no campo guarda_dias"),
        ({"responsavel": "ab"}, "O responsável tem de 3 a 200"),
        ({"responsavel": "   ab   "}, "O responsável tem de 3 a 200"),
        ({"responsavel": "x" * 201}, "O responsável tem de 3 a 200"),
        ({"responsavel": "Equipe\ndo sistema"}, "O responsável tem de 3 a 200"),
        ({"responsavel": "Equipe " + INVERTE + "do sistema"}, "O responsável tem de 3 a 200"),
        ({"ferramentas": {}}, "falta conteúdo no campo ferramentas"),
        ({"ferramentas": []}, "tipo errado, no campo ferramentas"),
    ],
)
def test_forma_da_raiz(mudar: dict[str, Any], trecho: str) -> None:
    recusa_na_forma(dados(**mudar), trecho)


def test_teto_de_ferramentas() -> None:
    base = dados()
    modelo = base["ferramentas"]["enviar_lembrete"]
    base["ferramentas"] = {f"f{i}": modelo for i in range(contrato.MAX_FERRAMENTAS_DECLARACAO)}
    d.validar(base)
    base["ferramentas"]["mais_uma"] = modelo
    recusa_na_forma(base, "há itens demais no campo ferramentas")


@pytest.mark.parametrize("nome", sorted(contrato.NOMES_RESERVADOS))
def test_ferramentas_do_contrato_nao_entram_na_declaracao(nome: str) -> None:
    base = dados()
    base["ferramentas"][nome] = base["ferramentas"].pop("enviar_lembrete")
    recusa_na_forma(base, "é uma ferramenta do contrato do selo")
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f["rastro"].update(ferramenta=nome))
    recusa_na_forma(base, "é uma ferramenta do contrato do selo")


def test_nome_de_ferramenta_fora_da_forma_e_recusado() -> None:
    base = dados()
    base["ferramentas"]["com espaço"] = base["ferramentas"].pop("enviar_lembrete")
    recusa_na_forma(base, "O nome de ferramenta tem de 1 a 128")


@pytest.mark.parametrize("campo", ["argumentos_outros", "argumentos_recusados"])
def test_argumentos_iguais_aos_de_teste_sao_recusados(campo: str) -> None:
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f.update({campo: dict(f["argumentos"])}))
    recusa_na_forma(base, f"Os {campo} precisam ser diferentes dos argumentos")


@pytest.mark.parametrize(
    ("campo", "valor", "trecho"),
    [
        ("efeito_adiado", {"argumentos": {}, "segundos": 0}, "pequeno demais"),
        ("efeito_adiado", {"argumentos": {}, "segundos": d.SEGUNDOS_MAXIMOS + 1}, "grande demais"),
        ("escrita_lenta", {"argumentos": {}}, "falta o campo"),
        ("varios_itens", {"argumentos": {}, "itens": 1}, "pequeno demais"),
        ("pede_dados", {}, "falta o campo"),
        ("idempotente", {"rastro": {"ferramenta": "ver_lembrete"}}, "falta o campo"),
        (
            "conflito",
            {
                "leitura": {"ferramenta": "ver_lembrete", "versao": "/v", "campo": "/a", "campo_concorrente": "/b"},
                "argumento_versao": "versao",
                "argumentos": {"a": 1, "versao": 3},
                "argumentos_concorrente": {"b": 2},
            },
            "posto pela bateria",
        ),
        (
            "conflito",
            {
                "leitura": {"ferramenta": "ver_lembrete", "versao": "/v", "campo": "/a", "campo_concorrente": "/b"},
                "argumento_versao": "versao",
                "argumentos": {"a": 1},
                "argumentos_concorrente": {"a": 1},
            },
            "campos diferentes",
        ),
    ],
)
def test_campos_opcionais_fora_da_forma(campo: str, valor: dict[str, Any], trecho: str) -> None:
    recusa_na_forma(mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f.update({campo: valor})), trecho)


# --- Texto colado ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "trecho"),
    [
        ("{", "não é um JSON válido"),
        ("", "não é um JSON válido"),
        ("[]", "objeto JSON"),
        ('"selo/1"', "objeto JSON"),
        ('{"contrato": "selo/1", "contrato": "selo/2"}', "repete a chave 'contrato'"),
        ('{"guarda_dias": NaN}', "(NaN)"),
        ('{"guarda_dias": Infinity}', "(Infinity)"),
        ("[" * 50000 + "]" * 50000, "objeto JSON"),
    ],
)
def test_texto_colado_que_nao_vale(texto: str, trecho: str) -> None:
    with pytest.raises(d.DeclaracaoRecusada) as erro:
        d.ler_texto(texto)
    assert erro.value.motivo in ("json_invalido", "grande_demais")
    assert trecho in erro.value.mensagem or erro.value.motivo == "grande_demais", erro.value.mensagem


def test_chave_repetida_num_nivel_de_baixo_tambem_e_recusada() -> None:
    texto = json.dumps(dados()).replace('"conta": "/itens"', '"conta": "/itens", "conta": "/outra"', 1)
    with pytest.raises(d.DeclaracaoRecusada, match="repete a chave 'conta'"):
        d.ler_texto(texto)


def test_json_fundo_demais_e_recusado_sem_erro_do_python() -> None:
    fundo = '{"contrato": "selo/1", "x": ' + "[" * 20000 + "]" * 20000 + "}"
    assert len(fundo) < contrato.TETO_DECLARACAO_BYTES
    with pytest.raises(d.DeclaracaoRecusada) as erro:
        d.ler_texto(fundo)
    assert erro.value.motivo in ("json_invalido", "forma")


@pytest.mark.parametrize(
    "mudar",
    [
        lambda f: f["argumentos"].update(texto="a\u0000b"),
        lambda f: f["argumentos"].update({"a\u0000": "x"}),
        lambda f: f["argumentos_outros"].update(texto={"fundo": ["x", "\u0000"]}),
        lambda f: f["rastro"].update(conta="/\u0000"),
        lambda f: f["rastro"]["argumentos"].update(id="\u0000"),
        lambda f: f.update(
            conflito={
                "leitura": {"ferramenta": "ver_lembrete", "versao": "/v", "campo": "/a", "campo_concorrente": "/b"},
                "argumento_versao": "versao\u0000",
                "argumentos": {"a": 1},
                "argumentos_concorrente": {"b": 2},
            }
        ),
    ],
)
def test_caractere_nulo_e_recusado_em_todo_lugar(mudar: Callable[[dict[str, Any]], None]) -> None:
    """O jsonb do PostgreSQL nao guarda o U+0000: sem esta recusa, a tela aceitava e a gravacao estourava fora de
    ``DeclaracaoRecusada`` (o ADMIN via um erro do servidor)."""
    base = mudar_ferramenta(dados(), "enviar_lembrete", mudar)
    for ler in (d.validar, lambda x: d.ler_texto(json.dumps(x))):
        with pytest.raises(d.DeclaracaoRecusada) as erro:
            ler(base)
        assert (erro.value.motivo, erro.value.mensagem) == ("json_invalido", d.NULO_NA_DECLARACAO)
    assert "\\u0000" in d.NULO_NA_DECLARACAO


def test_barra_invertida_seguida_de_u0000_nao_e_o_caractere_nulo() -> None:
    base = mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f["argumentos"].update(texto="\\u0000"))
    texto = json.dumps(base)
    assert "\\\\u0000" in texto  # no JSON, a barra vem dobrada
    assert d.ler_texto(texto).ferramentas["enviar_lembrete"].argumentos["texto"] == "\\u0000"


FORMAS_DE_FUNDO: Final = ("listas", "objetos", "misto")
"""Como os niveis se aninham: so listas, so objetos ({"k": ...}) ou objetos e listas alternados."""


def _aninhado(forma: str, niveis: int) -> Any:
    """``niveis`` niveis de lista e objeto, um dentro do outro, em volta de um texto, sem recursao."""
    valor: Any = "x"
    for i in range(niveis):
        valor = [valor] if forma == "listas" or (forma == "misto" and i % 2) else {"k": valor}
    return valor


def _texto_aninhado(forma: str, niveis: int) -> str:
    """O JSON de ``_aninhado`` escrito a mao (o json.dumps do Python tem o proprio teto de niveis)."""
    abre, fecha = [], []
    for i in reversed(range(niveis)):
        if forma == "listas" or (forma == "misto" and i % 2):
            abre.append("[")
            fecha.append("]")
        else:
            abre.append('{"k":')
            fecha.append("}")
    return "".join(abre) + '"x"' + "".join(reversed(fecha))


def _com_fundo(niveis: int, forma: str = "listas") -> dict[str, Any]:
    """A declaracao com ``niveis`` niveis de objeto e lista, contando a raiz (os argumentos estao no nivel 4)."""
    return mudar_ferramenta(
        dados(), "enviar_lembrete", lambda f: f["argumentos"].update(texto=_aninhado(forma, niveis - 4))
    )


def _texto_com_fundo(niveis: int, forma: str) -> str:
    marca = "MARCA-DO-FUNDO"
    base = json.dumps(mudar_ferramenta(dados(), "enviar_lembrete", lambda f: f["argumentos"].update(texto=marca)))
    return base.replace(json.dumps(marca), _texto_aninhado(forma, niveis - 4))


def test_o_aninhado_tem_os_niveis_pedidos() -> None:
    for forma in FORMAS_DE_FUNDO:
        assert json.loads(_texto_aninhado(forma, 7)) == _aninhado(forma, 7)
        assert d._problema_no_json(_com_fundo(d.FUNDO_MAXIMO, forma)) is None
        assert d._problema_no_json(_com_fundo(d.FUNDO_MAXIMO + 1, forma)) == "fundo"


@pytest.mark.parametrize("forma", FORMAS_DE_FUNDO)
def test_niveis_ate_o_teto_passam_e_sao_guardados(forma: str) -> None:
    """No teto (64 niveis), com listas, objetos ou os dois: ``validar``, ``ler_texto``, ``guardada`` e a leitura do
    que foi guardado passam, e o guardado e o mesmo."""
    no_teto = d.validar(_com_fundo(d.FUNDO_MAXIMO, forma))
    d.conferir(no_teto, cadastro(), ambiente=Ambiente.PRODUCAO)
    assert d.ler_texto(_texto_com_fundo(d.FUNDO_MAXIMO, forma)) == no_teto
    guardado = no_teto.guardada()
    assert d.validar(guardado) == no_teto
    salva = d.DeclaracaoSalva(uuid.uuid4(), uuid.uuid4(), 1, guardado, "admin", uuid.uuid4(), datetime.now(UTC))
    assert salva.modelo() == no_teto


@pytest.mark.parametrize("niveis", [d.FUNDO_MAXIMO + 1, 300, 3000])
@pytest.mark.parametrize("forma", FORMAS_DE_FUNDO)
def test_niveis_demais_sao_recusados_so_com_declaracao_recusada(forma: str, niveis: int) -> None:
    """Acima do teto (65, 300, 3000), com listas, objetos (a mutacao W01 tirava o teto do ramo dos objetos) ou os
    dois: ``validar`` e ``ler_texto`` recusam com ``DeclaracaoRecusada`` e a frase do fundo, e nada levanta fora dela
    (sem o teto, ``guardada`` levantava ``ValueError`` do pydantic, que nao serializa mais de 255 niveis)."""
    for ler in (lambda: d.validar(_com_fundo(niveis, forma)), lambda: d.ler_texto(_texto_com_fundo(niveis, forma))):
        with pytest.raises(d.DeclaracaoRecusada) as erro:
            ler()
        assert (erro.value.motivo, erro.value.mensagem) == ("json_invalido", d.FUNDO_DEMAIS)


def test_metade_de_par_unicode_e_recusada() -> None:
    texto = json.dumps(dados(responsavel="Equipe \ud800 do sistema"))
    with pytest.raises(d.DeclaracaoRecusada):
        d.ler_texto(texto)


def test_mensagens_com_acento_e_sem_palavras_proibidas() -> None:
    """Todas as recusas vao a tela do ADMIN: com acento, sem prometer nada nem citar o rascunho."""
    casos: list[d.DeclaracaoRecusada] = []
    for motivo, base, cad in [
        ("selo_terceiro_ainda_nao", dados(), cadastro(de_terceiro=True)),
        ("alvo_na_producao", dados(endereco_teste=PRODUCAO), cadastro()),
        ("alvo_sem_https", dados(endereco_teste="http://t.exemplo.teste/mcp"), cadastro()),
        ("sem_acoes", dados(), cadastro(acoes=frozenset())),
        ("sem_chave_por_pessoa", dados(), cadastro(tipo_chave="compartilhada")),
    ]:
        casos.append(recusa(motivo, base, cad=cad))
    # Frases de tela, com acento (nem toda frase precisa de um, mas as de tela os tem onde a lingua pede).
    com_acento = [e.mensagem for e in casos if any(ch in e.mensagem for ch in "áâãéêíóôõúç")]
    assert len(com_acento) == len(casos) - 1, [e.mensagem for e in casos]
    for erro in casos:
        baixo = erro.mensagem.lower()
        for proibida in ("garante", "certificado", "conforme", "mws", "write-safety"):
            assert proibida not in baixo, erro.mensagem
