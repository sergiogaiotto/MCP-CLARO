"""Tetos de tamanho dos argumentos para servidor de terceiros (Fase 3, G4; pedido do G2): ``conferir_tamanho``.

O teto de niveis e o do esquema de terceiro (``cadastro.esquema.PROFUNDIDADE_MAXIMA``). Os testes de pedido fundo
que provam a volta sem recursao sobem esse teto (``sem_teto_de_niveis``): sem isso, o pedido nem seria aberto.
"""

from __future__ import annotations

import sys
import tracemalloc
from collections.abc import Iterator
from typing import Any

import pytest

from mcp_claro.cadastro import esquema as esquema_mod
from mcp_claro.cadastro.esquema import PROFUNDIDADE_MAXIMA
from mcp_claro.entrada import argumentos as argumentos_mod
from mcp_claro.entrada.argumentos import (
    TETO_DIGITOS,
    TETO_LETRAS,
    TETO_LISTA,
    TETO_PARTES,
    TETO_TEXTO,
    ArgumentosGrandes,
    ArgumentosInvalidos,
    conferir_tamanho,
)


@pytest.fixture
def sem_teto_de_niveis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teto de niveis bem alto, para provar a volta sem recursao com pedidos de milhares de niveis."""
    monkeypatch.setattr(esquema_mod, "PROFUNDIDADE_MAXIMA", 10**6)


def com_niveis(n: int, fundo: Any = None) -> dict[str, Any]:
    """Argumentos com ``n`` niveis de listas e objetos, um dentro do outro: o objeto dos argumentos e o primeiro, e
    a lista mais funda leva ``fundo`` (vazia sem ele)."""
    valor: Any = [] if fundo is None else [fundo]
    for _ in range(n - 2):
        valor = [valor]
    return {"l": valor}


def problemas(argumentos: Any) -> list[str]:
    with pytest.raises(ArgumentosGrandes) as erro:
        conferir_tamanho(argumentos)
    assert isinstance(erro.value, ArgumentosInvalidos)
    return erro.value.problemas


@pytest.mark.parametrize(
    "argumentos",
    [
        None,
        {},
        {"texto": "a" * TETO_TEXTO},
        {"b" * TETO_TEXTO: 1},
        {"lista": list(range(TETO_LISTA))},
        {"a": {"b": [{"c": [None, True, 1.5, "x"]}]}},
        {"n": 10**TETO_DIGITOS - 1, "m": -(10**TETO_DIGITOS - 1), "f": 1e308, "g": -1e308, "b": True},
    ],
    ids=["nada", "vazio", "texto_no_teto", "nome_no_teto", "lista_no_teto", "fundo", "numeros_no_teto"],
)
def test_dentro_dos_tetos_passa(argumentos: Any) -> None:
    conferir_tamanho(argumentos)


def test_texto_longo_em_qualquer_nivel() -> None:
    assert problemas({"texto": "a" * (TETO_TEXTO + 1)}) == [f"texto: texto com mais de {TETO_TEXTO} letras"]
    assert problemas({"a": [{"b": ["x", "c" * (TETO_TEXTO + 1)]}]}) == [
        f"a/0/b/1: texto com mais de {TETO_TEXTO} letras"
    ]


def test_nome_de_campo_longo_em_qualquer_nivel_sem_repetir_o_nome() -> None:
    nome = "n" * (TETO_TEXTO + 1)
    assert problemas({nome: 1}) == [f"(raiz): nome de campo com mais de {TETO_TEXTO} letras"]
    achados = problemas({"a": {"b": {nome: 1}}})
    assert achados == [f"a/b: nome de campo com mais de {TETO_TEXTO} letras"]
    assert "n" * 41 not in " ".join(achados)


def test_caminho_corta_nome_de_campo_comprido() -> None:
    nome = "m" * 200
    achados = problemas({nome: "a" * (TETO_TEXTO + 1)})
    assert achados == [f"{'m' * 40}...: texto com mais de {TETO_TEXTO} letras"]


def test_lista_longa_em_qualquer_nivel() -> None:
    assert problemas({"lista": [0] * (TETO_LISTA + 1)}) == [f"lista: lista com mais de {TETO_LISTA} itens"]
    assert problemas({"a": [[0] * (TETO_LISTA + 1)]}) == [f"a/0: lista com mais de {TETO_LISTA} itens"]


def test_lista_longa_nem_tem_os_itens_olhados() -> None:
    # Um so problema: os textos longos dentro da lista recusada nao entram na conta.
    assert problemas({"lista": ["a" * (TETO_TEXTO + 1)] * (TETO_LISTA + 1)}) == [
        f"lista: lista com mais de {TETO_LISTA} itens"
    ]


def test_numero_inteiro_com_digitos_demais_em_qualquer_nivel() -> None:
    assert TETO_DIGITOS == 100
    assert problemas({"n": 10**TETO_DIGITOS}) == [f"n: numero com mais de {TETO_DIGITOS} digitos"]
    assert problemas({"a": [1, {"b": -(10**TETO_DIGITOS)}]}) == [f"a/1/b: numero com mais de {TETO_DIGITOS} digitos"]
    # O maior que o leitor de JSON do Python aceita (4.300 digitos): antes, passava nos tetos.
    assert problemas({"n": [10**4299] * 3}) == [f"n/{k}: numero com mais de {TETO_DIGITOS} digitos" for k in range(3)]


@pytest.fixture
def sem_escrever_inteiro_longo() -> Iterator[None]:
    """O Python recusa escrever em texto um inteiro com mais de 640 digitos (``ValueError``) enquanto dura o teste."""
    antes = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(antes)


@pytest.mark.usefixtures("sem_escrever_inteiro_longo")
def test_numero_longo_e_recusado_sem_ser_escrito_em_texto() -> None:
    # Escrever um inteiro enorme em texto e o custo que o teto evita: a medida so compara.
    enorme = 7**5000  # mais de 4.000 digitos, feito sem passar por texto
    with pytest.raises(ValueError, match="integer string conversion"):
        str(enorme)
    assert problemas({"n": [enorme] * 900}) == [f"n/{k}: numero com mais de {TETO_DIGITOS} digitos" for k in range(5)]


def test_muitas_letras_somando_tudo() -> None:
    pedacos = {f"t{n}": "a" * TETO_TEXTO for n in range(TETO_LETRAS // TETO_TEXTO + 1)}
    assert problemas(pedacos)[-1] == f"(raiz): mais de {TETO_LETRAS} letras somando todos os textos"
    # Nomes de campo tambem somam.
    nomes = {"k" * TETO_TEXTO + str(n): 1 for n in range(TETO_LETRAS // TETO_TEXTO + 1)}
    assert problemas(nomes)[-1] == f"(raiz): mais de {TETO_LETRAS} letras somando todos os textos"


def test_muitas_partes_somando_tudo() -> None:
    # Cada lista no teto, mas muitas juntas (uniqueItems custa o quadrado do tamanho de cada lista).
    muitas = {f"l{n}": [{"x": n}] * TETO_LISTA for n in range(3)}
    assert problemas(muitas) == [f"(raiz): mais de {TETO_PARTES} partes somando todos os valores"]
    objeto = {f"k{n}": n for n in range(TETO_PARTES + 1)}
    assert problemas(objeto) == [f"(raiz): mais de {TETO_PARTES} partes somando todos os valores"]


@pytest.mark.usefixtures("sem_teto_de_niveis")
def test_aninhamento_fundo_nao_derruba_a_conferencia() -> None:
    fundo: Any = "a" * (TETO_TEXTO + 1)
    for _ in range(3000):
        fundo = [fundo]
    achados = problemas({"x": fundo})
    assert achados[0].endswith(f"texto com mais de {TETO_TEXTO} letras")


@pytest.mark.usefixtures("sem_teto_de_niveis")
def test_caminho_fundo_vai_cortado_na_frase() -> None:
    # O primeiro pedaco, "..." e os ultimos oito: a frase nao cresce com o aninhamento.
    fundo: Any = "a" * (TETO_TEXTO + 1)
    for _ in range(3000):
        fundo = [fundo]
    assert problemas({"x": fundo}) == [f"x/.../{'/'.join(['0'] * 8)}: texto com mais de {TETO_TEXTO} letras"]
    raso: Any = ["a" * (TETO_TEXTO + 1)]
    for _ in range(8):
        raso = [raso]
    # Dez pedacos ainda vao inteiros.
    assert problemas({"x": raso}) == [f"x/{'/'.join(['0'] * 9)}: texto com mais de {TETO_TEXTO} letras"]


def test_cadeia_de_listas_cheias_e_recusada_sem_gastar_memoria() -> None:
    # Cada nivel e uma lista no teto (1000 itens), e o primeiro item e o proximo nivel: cerca de 0,6 MB de JSON,
    # bem abaixo do teto do pedido. A pilha nao pode guardar os itens de todos os niveis, nem uma copia do caminho
    # inteiro em cada item (o custo cresceria com o quadrado da profundidade: centenas de MB e segundos no laco da
    # entrada). Sem relogio: o pico de memoria da conferencia, medido pelo tracemalloc.
    cadeia: Any = [0] * TETO_LISTA
    for _ in range(200):
        cadeia = [cadeia, *([0] * (TETO_LISTA - 1))]
    tracemalloc.start()
    try:
        achados = problemas({"a": cadeia})
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert achados == [f"(raiz): mais de {TETO_PARTES} partes somando todos os valores"]
    assert pico < 5 * 1024 * 1024, pico


@pytest.mark.usefixtures("sem_teto_de_niveis")
def test_cadeia_funda_dentro_dos_tetos_passa_sem_copiar_o_caminho() -> None:
    # Cada nivel e uma lista de dois itens, e o primeiro e o proximo nivel: quase TETO_PARTES partes, tudo dentro
    # dos tetos. Na pilha ficam milhares de itens, cada um com o caminho de milhares de pedacos: guardar uma copia do
    # caminho em cada item custaria o quadrado da profundidade em memoria.
    niveis = (TETO_PARTES - 2) // 2
    cadeia: Any = [0, 0]
    for _ in range(niveis - 1):
        cadeia = [cadeia, 0]
    tracemalloc.start()
    try:
        conferir_tamanho({"a": cadeia})
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert pico < 5 * 1024 * 1024, pico


def test_objeto_que_passa_do_teto_de_partes_nem_tem_as_chaves_lidas() -> None:
    # As partes do objeto sao contadas antes de ler as chaves: lidas, elas somariam mais de TETO_LETRAS letras.
    objeto = {f"k{n:05d}": n for n in range(TETO_PARTES)}
    assert sum(map(len, objeto)) > TETO_LETRAS
    assert problemas({"o": objeto}) == [f"(raiz): mais de {TETO_PARTES} partes somando todos os valores"]


def test_no_maximo_cinco_problemas() -> None:
    argumentos = {f"t{n}": [0] * (TETO_LISTA + 1) for n in range(9)}
    assert len(problemas(argumentos)) == 5


def test_caminho_so_e_montado_para_os_cinco_problemas(monkeypatch: pytest.MonkeyPatch) -> None:
    # Montar o caminho anda ate a raiz: com muitos nomes longos num objeto fundo, so os cinco da frase pagam isso.
    montados: list[object] = []
    original = argumentos_mod._caminho

    def contar(no: Any) -> str:
        montados.append(no)
        return original(no)

    monkeypatch.setattr(argumentos_mod, "_caminho", contar)
    fundo: Any = {"n" * (TETO_TEXTO + 1) + str(n): n for n in range(8)}
    for _ in range(50):
        fundo = [fundo]
    achados = problemas({"x": fundo})
    assert len(achados) == 5
    assert all(a.endswith(f"nome de campo com mais de {TETO_TEXTO} letras") for a in achados)
    assert len(montados) == 5


# --- Teto de niveis (o mesmo do esquema de terceiro) -------------------------------------------------------------


def test_os_niveis_do_teste_sao_os_que_o_teste_diz() -> None:
    def niveis(valor: Any) -> int:
        n = 0
        while isinstance(valor, dict | list):
            n += 1
            valor = next(iter(valor.values() if isinstance(valor, dict) else valor), None)
        return n

    assert niveis(com_niveis(PROFUNDIDADE_MAXIMA)) == PROFUNDIDADE_MAXIMA
    assert niveis(com_niveis(5000)) == 5000


def test_no_teto_de_niveis_passa_e_um_a_mais_e_recusado() -> None:
    assert PROFUNDIDADE_MAXIMA == 64
    conferir_tamanho(com_niveis(PROFUNDIDADE_MAXIMA))
    conferir_tamanho({"a": {"b": com_niveis(PROFUNDIDADE_MAXIMA - 2)}})
    # O nivel 65 fica em l/0/.../0 (63 zeros), cortado na frase.
    assert problemas(com_niveis(PROFUNDIDADE_MAXIMA + 1)) == [
        f"l/.../{'/'.join(['0'] * 8)}: mais de 64 niveis de listas e objetos, um dentro do outro"
    ]
    # Objetos contam como listas.
    objeto: Any = {}
    for _ in range(PROFUNDIDADE_MAXIMA):
        objeto = {"o": objeto}
    assert problemas(objeto)[0].endswith(": mais de 64 niveis de listas e objetos, um dentro do outro")


@pytest.mark.parametrize("n", [600, 5000])
def test_pedido_fundo_e_recusado_sem_abrir_o_que_esta_abaixo_do_teto(n: int) -> None:
    # Um texto longo la no fundo nem e olhado: o primeiro nivel acima do teto ja recusa.
    achados = problemas(com_niveis(n, "a" * (TETO_TEXTO + 1)))
    assert achados == [f"l/.../{'/'.join(['0'] * 8)}: mais de 64 niveis de listas e objetos, um dentro do outro"]


def test_o_teto_de_niveis_e_lido_de_cadastro_esquema(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importado, nao copiado: mudou la, mudou aqui.
    monkeypatch.setattr(esquema_mod, "PROFUNDIDADE_MAXIMA", 5)
    conferir_tamanho(com_niveis(5))
    assert problemas(com_niveis(6)) == ["l/0/0/0/0: mais de 5 niveis de listas e objetos, um dentro do outro"]


def test_texto_e_numero_no_ultimo_nivel_nao_contam_como_nivel() -> None:
    # Os niveis sao de listas e objetos: o texto dentro da lista do nivel 64 passa.
    conferir_tamanho(com_niveis(PROFUNDIDADE_MAXIMA, "texto"))
    conferir_tamanho(com_niveis(PROFUNDIDADE_MAXIMA, 1))
