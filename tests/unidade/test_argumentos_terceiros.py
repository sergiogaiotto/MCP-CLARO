"""Tetos de tamanho dos argumentos para servidor de terceiros (Fase 3, G4; pedido do G2): ``conferir_tamanho``."""

from __future__ import annotations

import tracemalloc
from typing import Any

import pytest

from mcp_claro.entrada import argumentos as argumentos_mod
from mcp_claro.entrada.argumentos import (
    TETO_LETRAS,
    TETO_LISTA,
    TETO_PARTES,
    TETO_TEXTO,
    ArgumentosGrandes,
    ArgumentosInvalidos,
    conferir_tamanho,
)


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
    ],
    ids=["nada", "vazio", "texto_no_teto", "nome_no_teto", "lista_no_teto", "fundo"],
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


def test_aninhamento_fundo_nao_derruba_a_conferencia() -> None:
    fundo: Any = "a" * (TETO_TEXTO + 1)
    for _ in range(3000):
        fundo = [fundo]
    achados = problemas({"x": fundo})
    assert achados[0].endswith(f"texto com mais de {TETO_TEXTO} letras")


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
