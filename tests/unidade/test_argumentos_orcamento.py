"""Conferencia do esquema com orcamento de passos e so os cinco primeiros erros (Fase 3, G4r; risco de largura do G2b).

Os esquemas daqui passam na revisao de terceiro (``cadastro.esquema.conferir_terceiro``) e os pedidos passam nos
tetos de tamanho (``conferir_tamanho``): so o orcamento e os cinco primeiros erros prendem o custo. A excecao e o enum
de 200 mil valores medido pelo G2b, que a revisao agora recusa (``ESCOLHAS_MAXIMAS``) e que o orcamento sozinho
tambem recusa. A prova principal e a conta de passos, deterministica, e a memoria (``tracemalloc``). O tempo e o da
CPU deste processo (``time.process_time``, que nao conta o tempo em que a maquina carregada atende outros
processos), com folga sobre o medido, numa chamada sem ``tracemalloc`` (ele multiplica a CPU medida de 3 a 12 vezes).
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import time
import tracemalloc
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema._utils import equal as igual_da_biblioteca
from jsonschema.exceptions import SchemaError

from mcp_claro.cadastro.esquema import ESCOLHAS_MAXIMAS, EsquemaRecusado, conferir_terceiro, para_conferir
from mcp_claro.entrada import argumentos as argumentos_mod
from mcp_claro.entrada.argumentos import (
    ORCAMENTO_PASSOS,
    PROBLEMA_FUNDO,
    PROBLEMA_NUMERO,
    RECUSA_ORCAMENTO,
    TETO_LISTA,
    ArgumentosGrandes,
    ArgumentosInvalidos,
    EsquemaInvalido,
    OrcamentoEsgotado,
    conferir_argumentos,
    conferir_esquema,
    conferir_tamanho,
)

SEGUNDOS: float = 1.0
# Medido sem tracemalloc, nesta maquina, em 7 rodadas de 5 medidas por caso: o maior tempo foi de cerca de 0,3 s no
# anyOf de 1990 const (0,2 a 0,28 s; mediana de 0,07 s) e de 0,07 s no enum de 200 mil (mediana de 0,04 s); os outros
# casos ficaram abaixo de 0,15 s. Sem o orcamento, de 2 a 28 s, 7,7 s e 8,5 s (G2b). Com o tracemalloc ligado na mesma
# chamada, chegou a 1,196 s numa suite carregada. E maximo de poucas medidas, nao um limite: a prova e a dos passos.
MEGAS: int = 50


@pytest.fixture(autouse=True)
def validadores_novos() -> Iterator[None]:
    """Cada teste comeca sem validador guardado: a conta nao depende do que outro teste ja montou."""
    argumentos_mod._validador.cache_clear()
    yield
    argumentos_mod._validador.cache_clear()


def objeto(propriedades: dict[str, Any], **mais: Any) -> dict[str, Any]:
    return {"type": "object", "properties": propriedades, **mais}


def de_terceiro(esquema: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
    """A copia que a entrada usa, depois de conferir que o esquema passa na revisao e o pedido nos tetos."""
    conferir_esquema(esquema)
    conferir_terceiro(esquema)
    conferir_tamanho(pedido)
    copia = para_conferir(esquema)
    with contextlib.suppress(ArgumentosInvalidos):
        conferir_argumentos(copia, {}, terceiro=True)  # o validador fica pronto, como depois da primeira chamada
    return copia


def _conferir(esquema: dict[str, Any], pedido: dict[str, Any], terceiro: bool) -> Any:
    try:
        return conferir_argumentos(esquema, pedido, terceiro=terceiro)
    except ArgumentosInvalidos as erro:
        return erro


def medir(esquema: dict[str, Any], pedido: dict[str, Any], *, terceiro: bool = True) -> tuple[Any, float, int]:
    """(desfecho, segundos de CPU, pico em bytes): o desfecho e a excecao levantada ou os passos gastos.

    Duas chamadas iguais. A primeira da o desfecho e o tempo, sem ``tracemalloc``. A segunda, com o validador ja
    pronto, so o pico de memoria.
    """
    inicio = time.process_time()
    desfecho = _conferir(esquema, pedido, terceiro)
    segundos = time.process_time() - inicio
    tracemalloc.start()
    try:
        _conferir(esquema, pedido, terceiro)
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return desfecho, segundos, pico


def passos_gastos(esquema: dict[str, Any], pedido: dict[str, Any]) -> int:
    try:
        return conferir_argumentos(esquema, pedido, terceiro=True)
    except OrcamentoEsgotado as erro:
        return -erro.passos


def recusado_pelo_orcamento(esquema: dict[str, Any], pedido: dict[str, Any]) -> None:
    """O pedido para no orcamento, em menos de ``SEGUNDOS`` de CPU e ``MEGAS`` de memoria, e o mesmo pedido para
    sempre no mesmo passo."""
    desfecho, segundos, pico = medir(esquema, pedido)
    assert isinstance(desfecho, OrcamentoEsgotado), desfecho
    assert isinstance(desfecho, ArgumentosGrandes)
    assert desfecho.passos > ORCAMENTO_PASSOS
    assert segundos < SEGUNDOS, segundos
    assert pico < MEGAS * 1024 * 1024, pico
    assert passos_gastos(esquema, pedido) == -desfecho.passos


def veredito(esquema: dict[str, Any], pedido: dict[str, Any], *, terceiro: bool) -> tuple[str, ...] | None:
    """Os problemas da recusa (None se o pedido passa). Uma recusa pelo orcamento nao e veredito: o teste falha."""
    try:
        conferir_argumentos(esquema, pedido, terceiro=terceiro)
    except OrcamentoEsgotado:
        pytest.fail("o orcamento parou a conferencia")
    except ArgumentosInvalidos as erro:
        return tuple(erro.problemas)
    return None


def em_json(valor: Any) -> Any:
    """O valor como chega de um JSON: sem nenhuma parte repetida por referencia (a comparacao pula partes que sao o
    mesmo objeto)."""
    return json.loads(json.dumps(valor))


# --- Os dois casos medidos pelo G2b ------------------------------------------------------------------------------

ANYOF_CONST = objeto({"l": {"type": "array", "items": {"anyOf": [{"const": n} for n in range(1990)]}}})
ENUM_GRANDE = objeto({"l": {"type": "array", "items": {"enum": list(range(200_000))}}})
ENUM_NO_TETO = objeto({"l": {"type": "array", "items": {"enum": list(range(ESCOLHAS_MAXIMAS))}}})


def test_anyOf_de_1990_const_com_300_itens_e_recusado_em_menos_de_1_s_e_50_mb() -> None:
    # Antes do orcamento: 8,5 s e 1,7 GB, medidos pelo G2b.
    pedido = {"l": [-1] * 300}
    recusado_pelo_orcamento(de_terceiro(ANYOF_CONST, pedido), pedido)


def test_enum_de_200_mil_valores_com_100_itens_e_recusado_na_revisao_e_no_orcamento() -> None:
    # Antes do orcamento: 7,7 s, medidos pelo G2b. Agora a revisao de terceiro ja recusa o esquema (G2br); se um
    # esquema assim chegasse a entrada, o orcamento sozinho tambem recusaria.
    with pytest.raises(EsquemaRecusado) as erro:
        conferir_terceiro(ENUM_GRANDE)
    assert erro.value.motivo == "escolhas"
    pedido = {"l": [-1] * 100}
    conferir_tamanho(pedido)
    recusado_pelo_orcamento(ENUM_GRANDE, pedido)


def test_enum_no_teto_da_revisao_com_lista_valida_cabe_ate_49_itens() -> None:
    # O efeito que fica: com um enum de ESCOLHAS_MAXIMAS valores, cada item custa 1 + 1.001 passos (items e enum),
    # e uma lista valida com mais de 49 itens passa do orcamento. O fornecedor precisa simplificar o esquema.
    assert ESCOLHAS_MAXIMAS == 1000
    copia = de_terceiro(ENUM_NO_TETO, {"l": [0] * TETO_LISTA})
    assert conferir_argumentos(copia, {"l": [ESCOLHAS_MAXIMAS - 1] * 49}, terceiro=True) == 5 + 49 * 1002
    pedido = {"l": [ESCOLHAS_MAXIMAS - 1] * 50}
    with pytest.raises(OrcamentoEsgotado):
        conferir_argumentos(copia, pedido, terceiro=True)
    recusado_pelo_orcamento(copia, {"l": [ESCOLHAS_MAXIMAS - 1] * TETO_LISTA})
    # Com itens fora do enum, a conferencia para nos cinco primeiros erros, antes do orcamento.
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(copia, {"l": [-1] * TETO_LISTA}, terceiro=True)
    assert not isinstance(erro.value, OrcamentoEsgotado)


# --- Referencia de texto longo (R3a, defeito 1 da ultima conferencia do G4r) -------------------------------------


def ref_longa(letras: int, chamadas: int = 600) -> dict[str, Any]:
    """A prova do verificador: uma definicao de nome curto (``s``) que chama outra de nome com ``letras`` letras, e um
    ``allOf`` com ``chamadas`` referencias a ``s`` em cada item. Cada busca de referencia le o texto inteiro."""
    longo = "n" * letras
    return em_json(
        objeto(
            {"l": {"type": "array", "items": {"allOf": [{"$ref": "#/$defs/s"}] * chamadas}}},
            **{"$defs": {"s": {"$ref": "#/$defs/" + longo}, longo: {}}},
        )
    )


PEDIDO_REF = {"l": [0] * TETO_LISTA}
PASSOS_COM_NOME_CURTO = 50_213
"""Onde a conta para com o nome de 1 letra. Antes do custo fixo por referencia (R3ar), parava em 50.231 (medido pelo
verificador do G4r), depois de seguir umas 49 mil referencias; agora, depois de umas 12 mil."""


def sem_a_revisao(esquema: dict[str, Any], pedido: dict[str, Any]) -> dict[str, Any]:
    """A copia que a entrada usaria se a revisao de terceiro deixasse o esquema passar (``de_terceiro`` sem ela)."""
    conferir_esquema(esquema)
    conferir_tamanho(pedido)
    copia = para_conferir(esquema)
    with contextlib.suppress(ArgumentosInvalidos):
        conferir_argumentos(copia, {}, terceiro=True)
    return copia


def test_referencia_seguida_conta_3_passos_fixos_e_um_a_cada_100_letras() -> None:
    assert argumentos_mod.LETRAS_POR_PASSO == 100
    assert argumentos_mod.PASSOS_POR_REFERENCIA == 3
    # A palavra (1), o custo fixo de seguir a referencia (3) e as letras do texto dela (1 a cada 100).
    assert argumentos_mod._passos("$ref", "#/$defs/s", {}) == 1 + 3
    assert argumentos_mod._passos("$ref", "#/$defs/" + "n" * 91, {}) == 1 + 3
    assert argumentos_mod._passos("$ref", "#/$defs/" + "n" * 92, {}) == 1 + 3 + 1
    assert argumentos_mod._passos("$ref", "#/$defs/" + "n" * 120_000, {}) == 1 + 3 + 1200
    # $dynamicRef e recusada na revisao; conta por defesa, como $ref.
    assert argumentos_mod._passos("$dynamicRef", "#" + "n" * 299, []) == 1 + 3 + 3
    assert argumentos_mod._passos("$ref", 7, {}) == 1  # nao e texto: a revisao e a conferencia do esquema recusam
    assert argumentos_mod._passos("const", "n" * 1000, {}) == 1  # so as referencias leem o texto a cada chamada
    assert argumentos_mod._passos("const", "#/$defs/s", {}) == 1  # e so elas pagam o custo fixo


def test_referencia_de_nome_longo_e_recusada_na_revisao_e_sem_ela_pelo_orcamento_em_milissegundos() -> None:
    # Antes: passava na revisao (custo 1803, 253.926 bytes), e o pedido valido gastava 4,3 s de CPU antes de parar
    # no orcamento, porque cada busca de referencia custa o tamanho do texto e contava 1 passo.
    esquema = ref_longa(120_000)
    with pytest.raises(EsquemaRecusado) as erro:
        conferir_terceiro(esquema)
    assert (erro.value.motivo, erro.value.palavra) == ("referencia", "$defs")
    assert erro.value.onde == "$defs/" + "n" * 40 + "..."
    # Com a revisao contornada, o orcamento conta as letras de cada busca e para logo.
    copia = sem_a_revisao(esquema, PEDIDO_REF)
    recusado_pelo_orcamento(copia, PEDIDO_REF)


def test_referencia_de_nome_curto_para_no_orcamento_sempre_no_mesmo_passo() -> None:
    # Nome curto: passa na revisao, e o pedido para no orcamento sempre no mesmo passo.
    copia = de_terceiro(ref_longa(1), PEDIDO_REF)
    assert passos_gastos(copia, PEDIDO_REF) == -PASSOS_COM_NOME_CURTO
    # No teto da revisao (o texto da referencia com 256 letras), cada busca soma 2 passos pelas letras e passa na
    # revisao.
    no_teto = ref_longa(256 - len("#/$defs/"))
    conferir_terceiro(no_teto)
    with pytest.raises(EsquemaRecusado) as erro:
        conferir_terceiro(ref_longa(256 - len("#/$defs/") + 1))
    assert (erro.value.motivo, erro.value.palavra, erro.value.onde) == ("referencia", "$ref", "$defs/s")
    assert passos_gastos(de_terceiro(no_teto, PEDIDO_REF), PEDIDO_REF) < 0


# --- Custo fixo por referencia seguida (R3ar; pendencia 3 do R3a) -----------------------------------------------


def cadeia(n: int) -> dict[str, Any]:
    """O pior caso achado pelo R3a: cada item da lista segue uma cadeia de referencias curtas, uma chamando a outra
    (``d0`` chama ``d1`` e assim por diante, ate ``d{n}``, vazia): ``n + 1`` referencias por item."""
    definicoes: dict[str, Any] = {f"d{k}": {"$ref": f"#/$defs/d{k + 1}"} for k in range(n)}
    definicoes[f"d{n}"] = {}
    return em_json(objeto({"l": {"type": "array", "items": {"$ref": "#/$defs/d0"}}}, **{"$defs": definicoes}))


def referencias_seguidas(monkeypatch: pytest.MonkeyPatch, copia: dict[str, Any], pedido: dict[str, Any]) -> int:
    """Quantas vezes a conferencia chegou a um ``$ref`` antes de passar ou de parar no orcamento."""
    seguidas = 0
    original = argumentos_mod._passos

    def contando(nome: str, valor: Any, instancia: Any) -> int:
        nonlocal seguidas
        seguidas += nome == "$ref"
        return original(nome, valor, instancia)

    with monkeypatch.context() as troca:
        troca.setattr(argumentos_mod, "_passos", contando)
        with contextlib.suppress(OrcamentoEsgotado):
            conferir_argumentos(copia, pedido, terceiro=True)
    return seguidas


def test_cadeia_de_referencias_curtas_para_depois_de_um_quarto_das_referencias(monkeypatch: pytest.MonkeyPatch) -> None:
    # Antes do custo fixo: 60 referencias curtas em cada item de uma lista de 1.000 paravam no orcamento depois de
    # seguir umas 49 mil referencias, de 0,28 a 0,49 s de CPU (de 1,6 a 2,3 s com tracemalloc). Com 3 passos a mais
    # por referencia, param depois de umas 12 mil, de 0,07 a 0,12 s de CPU (de 0,4 a 0,7 s com tracemalloc). Sao os
    # tempos das rodadas medidas nesta maquina (os do texto ao G8); outra maquina, ou a mesma carregada, pode dar
    # outros. A prova e a conta, deterministica: o tempo fica no relatorio.
    copia = de_terceiro(cadeia(60), PEDIDO_REF)
    with pytest.raises(OrcamentoEsgotado) as erro:
        conferir_argumentos(copia, PEDIDO_REF, terceiro=True)
    assert erro.value.passos == 50_003
    seguidas = referencias_seguidas(monkeypatch, copia, PEDIDO_REF)
    assert seguidas == 12_250
    with monkeypatch.context() as troca:
        troca.setattr(argumentos_mod, "PASSOS_POR_REFERENCIA", 0)
        sem_o_custo_fixo = referencias_seguidas(monkeypatch, copia, PEDIDO_REF)
    assert sem_o_custo_fixo > 3.9 * seguidas
    assert passos_gastos(copia, PEDIDO_REF) == -erro.value.passos  # sempre no mesmo passo


def test_cadeia_de_10_referencias_em_cada_item_ainda_cabe_no_orcamento() -> None:
    # 11 referencias por item, 1.000 itens: type, properties (1 + 1), type e items (1 + 1.000), e 4 passos por
    # referencia. Sem o custo fixo, 12.005 passos: menos de um quarto do orcamento, entao continua cabendo.
    copia = de_terceiro(cadeia(10), PEDIDO_REF)
    assert conferir_argumentos(copia, PEDIDO_REF, terceiro=True) == 1005 + 1000 * 11 * 4 == 45_005


LISTA_DE_OBJETOS = em_json(
    objeto(
        {"l": {"type": "array", "items": {"$ref": "#/$defs/item"}}},
        **{
            "$defs": {
                "item": {
                    "type": "object",
                    "properties": {c: {"type": "integer"} for c in "abcde"},
                    "required": list("abcde"),
                }
            }
        },
    )
)
REFERENCIAS_QUE_PASSAM: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "cadeia_de_10": (cadeia(10), PEDIDO_REF),
    "lista_de_objetos": (LISTA_DE_OBJETOS, {"l": [dict.fromkeys("abcde", 1) for _ in range(500)]}),
    "nome_no_teto": (ref_longa(256 - len("#/$defs/"), chamadas=2), PEDIDO_REF),
}


@pytest.mark.parametrize("caso", list(REFERENCIAS_QUE_PASSAM))
def test_o_custo_fixo_por_referencia_no_maximo_quadruplica_a_conta(caso: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cada referencia contava ao menos 1 passo e agora conta 3 a mais: a conta de um pedido no maximo quadruplica.
    # Por isso, um pedido que gastava ate um quarto do orcamento continua cabendo nele.
    esquema, pedido = REFERENCIAS_QUE_PASSAM[caso]
    copia = de_terceiro(esquema, pedido)
    agora = conferir_argumentos(copia, pedido, terceiro=True)
    with monkeypatch.context() as troca:
        troca.setattr(argumentos_mod, "PASSOS_POR_REFERENCIA", 0)
        antes = conferir_argumentos(copia, pedido, terceiro=True)
    assert antes < agora <= 4 * antes
    assert agora <= ORCAMENTO_PASSOS


def test_a_frase_do_orcamento() -> None:
    assert RECUSA_ORCAMENTO == "O esquema deste servidor ficou caro demais para conferir este pedido. Nada foi enviado."
    assert ORCAMENTO_PASSOS == 50_000


def test_o_orcamento_e_deterministico() -> None:
    # Mesmo pedido, mesma conta: passou ou parou, sempre no mesmo passo.
    pedido = {"l": [-1] * 300}
    copia = de_terceiro(ANYOF_CONST, pedido)
    primeira = passos_gastos(copia, pedido)
    argumentos_mod._validador.cache_clear()
    assert [passos_gastos(copia, pedido) for _ in range(3)] == [primeira] * 3
    assert primeira < 0  # parou

    valido = {"l": [1989] * 3}
    assert len({passos_gastos(copia, valido) for _ in range(3)}) == 1
    assert passos_gastos(copia, valido) > 0  # passou


# --- Trabalho que nenhuma palavra fazia sozinha ------------------------------------------------------------------

MUITOS = {f"k{n}": 0 for n in range(3900)}

CAMINHOS_VAZIOS = {
    # Um ramo com um esquema vazio dentro anda pela lista inteira sem chamar palavra nenhuma.
    "items_vazio": (objeto({"l": {"anyOf": [{"items": {}, "const": n} for n in range(995)]}}), {"l": [0] * 1000}),
    "contains_vazio": (
        objeto({"l": {"anyOf": [{"contains": {}, "const": n} for n in range(995)]}}),
        {"l": [0] * 1000},
    ),
    "additionalProperties_vazio": (
        {"type": "object", "anyOf": [{"additionalProperties": {}, "const": n} for n in range(995)]},
        MUITOS,
    ),
    "propertyNames_vazio": (
        {"type": "object", "anyOf": [{"propertyNames": {}, "const": n} for n in range(995)]},
        MUITOS,
    ),
    # oneOf confere todos os ramos; ramos vazios nao chamam palavra nenhuma.
    "oneOf_vazio": (objeto({"l": {"items": {"not": {"oneOf": [{}] * 1990}}}}), {"l": [0] * TETO_LISTA}),
    "oneOf_true": (objeto({"l": {"items": {"not": {"oneOf": [True] * 1990}}}}), {"l": [0] * TETO_LISTA}),
    # Ramo false: o erro nasce fora de toda palavra, com o pedido inteiro na mensagem.
    "anyOf_false": ({"type": "object", "anyOf": [False] * 1990}, MUITOS),
    # Cada erro repete o pedido inteiro na mensagem (uns 50 KB).
    "anyOf_type": ({"type": "object", "anyOf": [{"type": "array"} for _ in range(1990)]}, MUITOS),
    # Cada ramo guarda os mil erros dos itens.
    "anyOf_items_const": (objeto({"l": {"anyOf": [{"items": {"const": n}} for n in range(995)]}}), {"l": [-1] * 1000}),
}


@pytest.mark.parametrize("caso", list(CAMINHOS_VAZIOS))
def test_trabalho_sem_palavra_e_mensagens_grandes_entram_na_conta(caso: str) -> None:
    # Sem a conta por item percorrido, por erro e por letra, e sem trocar o ramo false: de 2 a 28 s, ou mais de
    # 100 MB.
    esquema, pedido = CAMINHOS_VAZIOS[caso]
    copia = de_terceiro(esquema, pedido)
    desfecho, segundos, pico = medir(copia, pedido)
    assert isinstance(desfecho, OrcamentoEsgotado), desfecho
    assert segundos < SEGUNDOS, segundos
    assert pico < MEGAS * 1024 * 1024, pico


# --- A conta, passo a passo --------------------------------------------------------------------------------------


def test_enum_conta_um_passo_por_valor() -> None:
    # type, properties (um campo do esquema) e enum: 1 + (1 + 1) + (1 + N). Antes de chamar o enum, a conta ja
    # sabe: com N grande demais, o enum nem roda, mesmo para um valor que estaria na lista.
    n = ORCAMENTO_PASSOS - 4
    esquema = objeto({"e": {"enum": list(range(n))}})
    assert conferir_argumentos(esquema, {"e": n - 1}, terceiro=True) == n + 4
    esquema = objeto({"e": {"enum": list(range(n + 1))}})
    with pytest.raises(OrcamentoEsgotado) as erro:
        conferir_argumentos(esquema, {"e": 0}, terceiro=True)
    assert erro.value.passos == ORCAMENTO_PASSOS + 1
    assert erro.value.problemas == ["o esquema deste servidor ficou caro demais para conferir este pedido"]


def test_listas_do_esquema_e_partes_do_pedido_contam_por_item() -> None:
    lista = {"l": list(range(10))}
    # type (1), properties (1 + 1), items (1 + 10 itens), type de cada item (10).
    assert conferir_argumentos(objeto({"l": {"items": {"type": "integer"}}}), lista, terceiro=True) == 24
    # anyOf com tres ramos: 1 + 3, e o primeiro ramo ja vale.
    assert conferir_argumentos(objeto({"a": {"anyOf": [{}, {}, {}]}}), {"a": 1}, terceiro=True) == 1 + 2 + 4
    # required e properties so andam pelo esquema quando a parte e um objeto.
    esquema = objeto({"a": {"required": ["x", "y"], "properties": {"x": {}, "y": {}}}})
    assert conferir_argumentos(esquema, {"a": "texto"}, terceiro=True) == 1 + 2 + 1 + 1
    assert conferir_argumentos(esquema, {"a": {"x": 1, "y": 2}}, terceiro=True) == 1 + 2 + 3 + 3
    # additionalProperties anda pelos campos do pedido.
    esquema = {"type": "object", "additionalProperties": {"type": "integer"}}
    assert conferir_argumentos(esquema, {"a": 1, "b": 2}, terceiro=True) == 1 + 3 + 2
    # dependentRequired anda tambem pela lista de nomes de cada campo.
    esquema = {"type": "object", "dependentRequired": {"a": ["b", "c"], "d": ["e"]}}
    assert conferir_argumentos(esquema, {"a": 1, "b": 1, "c": 1}, terceiro=True) == 1 + (1 + 2 + 3)


def test_cada_erro_conta_e_mensagem_longa_conta_mais() -> None:
    # Sem erro: type, properties (1 + 1) e type do campo, 4 passos.
    lista = list(range(2000))
    sem_erro = conferir_argumentos(objeto({"a": {"type": "array"}}), {"a": lista}, terceiro=True)
    assert sem_erro == 4
    # Com erro: a mensagem repete a lista (mais de 10 mil letras), e o erro conta na palavra que o achou e em cada
    # palavra por onde ele sobe (aqui, properties).
    longo = objeto({"a": {"type": "string"}})
    conta = argumentos_mod._Conta(10**9)
    marca = argumentos_mod._CONTA.set(conta)
    try:
        validador = argumentos_mod._validador(argumentos_mod.json.dumps(longo, sort_keys=True), True)
        erros = list(validador.iter_errors({"a": lista}))
    finally:
        argumentos_mod._CONTA.reset(marca)
    assert len(erros) == 1
    letras = len(erros[0].message)
    assert letras > 10_000
    por_erro = argumentos_mod.PASSOS_POR_ERRO + letras // argumentos_mod.LETRAS_POR_PASSO
    # O erro passa pela palavra type (no campo) e sobe por properties: conta nas duas.
    assert conta.passos == sem_erro + 2 * por_erro


def test_ramo_false_tem_o_mesmo_veredito() -> None:
    esquema = objeto({"a": {"anyOf": [False, {"type": "integer"}]}, "b": {"oneOf": [False, {"type": "integer"}]}})
    assert conferir_argumentos(esquema, {"a": 1, "b": 2}, terceiro=True) > 0
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(esquema, {"a": "x", "b": "y"}, terceiro=True)
    assert erro.value.problemas == ["a: regra 'anyOf' nao atendida", "b: regra 'oneOf' nao atendida"]
    assert not isinstance(erro.value, ArgumentosGrandes)


def test_pedidos_validos_cabem_no_orcamento() -> None:
    # O esquema dos testes de terceiro na entrada, com o pedido nos tetos.
    simples = objeto({"texto": {"type": "string"}, "destinatario": {"type": "string"}, "lista": {"type": "array"}})
    assert conferir_argumentos(simples, {"texto": "a" * 10_000, "lista": list(range(TETO_LISTA))}, terceiro=True) < 20
    # Uma lista grande de objetos pequenos, cada um conferido por inteiro.
    item = {
        "type": "object",
        "properties": {c: {"type": "integer"} for c in "abcde"},
        "required": list("abcde"),
        "additionalProperties": False,
    }
    pedido = {"l": [dict.fromkeys("abcde", 1) for _ in range(800)]}
    conferir_tamanho(pedido)
    assert conferir_argumentos(objeto({"l": {"type": "array", "items": item}}), pedido, terceiro=True) < 25_000


def test_sistema_proprio_nao_conta() -> None:
    # D3: o mesmo esquema caro, num sistema proprio, confere como nas Fases 1 e 2.
    assert conferir_argumentos(ENUM_GRANDE, {"l": [199_999]}) == 0
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(ENUM_GRANDE, {"l": [-1]})
    assert not isinstance(erro.value, OrcamentoEsgotado)


# --- Cinco primeiros erros (tambem num sistema proprio) ----------------------------------------------------------


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
def test_so_os_cinco_primeiros_erros_sao_montados(terceiro: bool) -> None:
    # Vinte mil itens errados: montar todos os erros passaria de 40 MB. So os cinco primeiros, em ordem.
    esquema = objeto({"l": {"type": "array", "items": {"type": "string"}}})
    pedido = {"l": list(range(20_000))} if not terceiro else {"l": list(range(TETO_LISTA))}
    conferir_argumentos(esquema, {})
    conferir_argumentos(esquema, {}, terceiro=terceiro)
    desfecho, _, pico = medir(esquema, pedido, terceiro=terceiro)
    assert isinstance(desfecho, ArgumentosInvalidos)
    assert desfecho.problemas == [f"l/{n}: regra 'type' nao atendida" for n in range(5)]
    assert pico < 2 * 1024 * 1024, pico


def test_cinco_primeiros_em_ordem_de_caminho_e_sempre_os_mesmos() -> None:
    # A conferencia acha os erros dos campos (properties) antes dos da raiz (required, que vem depois na ordem das
    # palavras), e sao seis na raiz: dos cinco primeiros achados, a frase sai em ordem de caminho, a raiz primeiro.
    esquema = {
        "type": "object",
        "properties": {"b": {"type": "string"}, "a": {"type": "string"}},
        "required": ["x1", "x2", "x3", "x4", "x5", "x6"],
    }
    frases = set()
    for _ in range(3):
        argumentos_mod._validador.cache_clear()
        with pytest.raises(ArgumentosInvalidos) as erro:
            conferir_argumentos(esquema, {"a": 1, "b": 2})
        frases.add(tuple(erro.value.problemas))
    assert len(frases) == 1
    (problemas,) = frases
    assert problemas == (
        "(raiz): regra 'required' nao atendida",
        "(raiz): regra 'required' nao atendida",
        "(raiz): regra 'required' nao atendida",
        "a: regra 'type' nao atendida",
        "b: regra 'type' nao atendida",
    )


# --- Aninhamento que estoura a pilha -----------------------------------------------------------------------------

ARVORE: dict[str, Any] = {
    "type": "object",
    "properties": {"lista": {"$ref": "#/$defs/no"}},
    "$defs": {"no": {"type": "array", "items": {"$ref": "#/$defs/no"}}},
}


def fundo(n: int) -> dict[str, Any]:
    valor: Any = []
    for _ in range(n):
        valor = [valor]
    return {"lista": valor}


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
@pytest.mark.parametrize("n", [600, 5000])
def test_pilha_estourada_vira_recusa_legivel(n: int, terceiro: bool) -> None:
    conferir_argumentos(ARVORE, fundo(10), terceiro=terceiro)
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(ARVORE, fundo(n), terceiro=terceiro)
    assert erro.value.problemas == [PROBLEMA_FUNDO]
    assert not isinstance(erro.value, ArgumentosGrandes)
    # A conta volta ao normal: a proxima conferencia nao herda a conta parada no meio.
    assert argumentos_mod._CONTA.get() is None


def propriedades_aninhadas(n: int) -> dict[str, Any]:
    """Um esquema de objeto com ``n`` niveis de ``properties``, um dentro do outro (a raiz e o primeiro)."""
    no: dict[str, Any] = {"type": "string"}
    for _ in range(n - 1):
        no = objeto({"a": no})
    return no


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
def test_pilha_estourada_na_montagem_do_validador_vira_recusa_legivel(terceiro: bool) -> None:
    # A conferencia do esquema da biblioteca (check_schema, na primeira chamada de cada esquema) desce com recursao:
    # com uns 100 niveis de properties, a pilha estoura antes de qualquer argumento. Sem a guarda, RecursionError.
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(propriedades_aninhadas(100), {}, terceiro=terceiro)
    assert erro.value.problemas == [PROBLEMA_FUNDO]
    assert not isinstance(erro.value, ArgumentosGrandes)
    assert argumentos_mod._CONTA.get() is None
    # Nada fica guardado: a mesma chamada de novo da a mesma recusa (lru_cache nao guarda excecao).
    with pytest.raises(ArgumentosInvalidos):
        conferir_argumentos(propriedades_aninhadas(100), {}, terceiro=terceiro)


@pytest.mark.parametrize("niveis", [100, 300])
def test_conferir_esquema_fundo_demais_levanta_esquema_invalido(niveis: int) -> None:
    # A revisao do ADMIN (proprio e terceiro) chama conferir_esquema antes de tudo: antes, RecursionError (erro 500).
    esquema = objeto({"p": propriedades_aninhadas(niveis)})
    with pytest.raises(EsquemaInvalido) as erro:
        conferir_esquema(esquema)
    assert str(erro.value) == argumentos_mod.ESQUEMA_FUNDO_DEMAIS
    assert str(erro.value) == "esquema invalido: partes demais umas dentro das outras para conferir"
    assert not isinstance(erro.value, EsquemaRecusado)
    conferir_esquema(objeto({"p": propriedades_aninhadas(60)}))  # o que cabe na pilha segue como antes


def test_conferir_esquema_troca_so_o_estouro_da_pilha_pela_frase_de_partes_demais() -> None:
    # A guarda da pilha so troca o RecursionError: um esquema invalido comum mostra ao ADMIN o motivo de verdade, e
    # nao "partes demais umas dentro das outras".
    with pytest.raises(EsquemaInvalido) as erro:
        conferir_esquema({"type": "string"})
    assert str(erro.value) == 'o esquema de entrada precisa ter "type": "object"'
    assert erro.value.__cause__ is None
    with pytest.raises(EsquemaInvalido) as erro:
        conferir_esquema({"type": "object", "properties": {"a": {"type": 5}}})
    assert str(erro.value).startswith("esquema invalido: ")
    assert str(erro.value) != argumentos_mod.ESQUEMA_FUNDO_DEMAIS
    assert isinstance(erro.value.__cause__, SchemaError)


def test_o_validador_que_conta_tambem_nao_confere_format() -> None:
    # Como nas Fases 1 e 2: format nao e conferido, nem num terceiro.
    esquema = objeto({"e": {"type": "string", "format": "email"}})
    assert conferir_argumentos(esquema, {"e": "nao e e-mail"}, terceiro=True) > 0
    texto = argumentos_mod.json.dumps(esquema, sort_keys=True)
    assert argumentos_mod._validador(texto, True).format_checker is None
    assert argumentos_mod._validador(texto).format_checker is None


# --- enum e const com listas e objetos (a comparacao entra na conta) ---------------------------------------------


def quase(k: int) -> list[list[int]]:
    """Cinco listas de 998 zeros, com o ultimo numero trocado por ``k + 1``: so a ultima parte difere do pedido."""
    valor = [[0] * 998 for _ in range(5)]
    valor[4] = [0] * 997 + [k + 1]
    return valor


PEDIDO_ESTRUTURADO = {"l": [[0] * 998 for _ in range(5)]}
ENUM_ESTRUTURADO = em_json(
    {
        "type": "object",
        "properties": {"l": {"allOf": [{"$ref": "#/$defs/e"}] * 995}},
        "$defs": {"e": {"enum": [quase(k) for k in range(23)] + [PEDIDO_ESTRUTURADO["l"]]}},
    }
)
"""O caso do verificador: passa na revisao (custo 1.992, 260 KB, 24 valores) e o pedido e valido. Antes, 26.869
passos e 5 s: cada uma das 995 referencias comparava os 24 valores, parte por parte, sem contar."""
CONST_ESTRUTURADO = em_json(
    {
        "type": "object",
        "properties": {"l": {"allOf": [{"$ref": "#/$defs/c"}] * 995}},
        "$defs": {"c": {"const": PEDIDO_ESTRUTURADO["l"]}},
    }
)


@pytest.mark.parametrize("esquema", [ENUM_ESTRUTURADO, CONST_ESTRUTURADO], ids=["enum", "const"])
def test_enum_e_const_com_valores_estruturados_contam_cada_parte_comparada(esquema: dict[str, Any]) -> None:
    pedido = em_json(PEDIDO_ESTRUTURADO)
    copia = de_terceiro(esquema, pedido)
    assert copia == esquema
    recusado_pelo_orcamento(copia, pedido)
    # Num sistema proprio, o pedido continua valido (D3); com uma referencia so, para o teste nao levar segundos.
    uma = {**esquema, "properties": {"l": {"allOf": esquema["properties"]["l"]["allOf"][:1]}}}
    assert conferir_argumentos(uma, pedido) == 0


def test_comparacao_conta_um_passo_por_par_de_partes_e_para_na_primeira_diferenca() -> None:
    # type, properties (1 + 1) e const (1): 4 passos; mais um por par de partes comparado, na ordem da biblioteca.
    esquema = objeto({"c": {"const": [1, [2, 3], {"a": 4}]}})
    assert conferir_argumentos(esquema, {"c": [1, [2, 3], {"a": 4}]}, terceiro=True) == 4 + 3 + 2 + 1
    with pytest.raises(ArgumentosInvalidos):
        conferir_argumentos(esquema, {"c": [9, [2, 3], {"a": 4}]}, terceiro=True)  # para no primeiro par
    conta = argumentos_mod._Conta(10**9)
    assert not argumentos_mod._igual(conta, [1, [2, 3], {"a": 4}], [9, [2, 3], {"a": 4}])
    assert conta.passos == 1
    # Tamanhos diferentes nem abrem a lista; valores simples nao contam (o enum ja paga um passo por valor).
    conta = argumentos_mod._Conta(10**9)
    assert not argumentos_mod._igual(conta, [0] * 998, [0] * 997)
    assert argumentos_mod._igual(conta, 7, 7.0)
    assert conta.passos == 0
    # enum com listas: 1 + 2 valores, e cada valor comparado conta os pares que abriu.
    esquema = objeto({"e": {"enum": [[1, 2], [1, 3]]}})
    assert conferir_argumentos(esquema, {"e": [1, 3]}, terceiro=True) == 3 + 3 + 2 + 2


def test_comparacao_sem_recursao() -> None:
    um: Any = []
    outro: Any = []
    for _ in range(20_000):
        um, outro = [um], [outro]
    assert argumentos_mod._igual(None, um, outro)


ESCALARES = st.none() | st.booleans() | st.integers(-2, 2) | st.sampled_from([0.0, 1.0, 1.5, math.nan, math.inf])
VALORES = st.recursive(
    ESCALARES | st.sampled_from(["", "a", "1"]),
    lambda filhos: st.lists(filhos, max_size=3) | st.dictionaries(st.sampled_from(["a", "b", "c"]), filhos, max_size=3),
    max_leaves=12,
)


@pytest.mark.parametrize(
    ("um", "outro"),
    [
        (True, 1),
        (False, 0),
        (1, 1.0),
        ([True], [1]),
        ({"a": 1}, {"a": True}),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
        ({"a": 1, "b": 2}, {"a": 1, "c": 2}),
        ({"a": None}, {"b": None}),
        ([1], [1, 2]),
        ([[1, 2]], [[1]]),
        ("1", 1),
        ([], {}),
        ([[]], [[]]),
        (math.nan, math.nan),
        ([math.nan], [math.nan]),
        (None, False),
    ],
)
def test_comparacao_tem_o_veredito_da_biblioteca(um: Any, outro: Any) -> None:
    for a, b in ((um, outro), (outro, um), (um, copy.deepcopy(um)), (em_json(um), em_json(um))):
        assert argumentos_mod._igual(None, a, b) == igual_da_biblioteca(a, b), (a, b)


@settings(max_examples=1500, deadline=None, database=None)
@given(um=VALORES, outro=VALORES)
def test_comparacao_tem_o_veredito_da_biblioteca_em_qualquer_par(um: Any, outro: Any) -> None:
    assert argumentos_mod._igual(None, um, outro) == igual_da_biblioteca(um, outro)
    copia = copy.deepcopy(um)
    assert argumentos_mod._igual(None, um, copia) == igual_da_biblioteca(um, copia)


@settings(max_examples=300, deadline=None, database=None)
@given(valores=st.lists(VALORES, min_size=1, max_size=4), pedido=VALORES)
def test_enum_e_const_contados_tem_o_veredito_do_proprio(valores: list[Any], pedido: Any) -> None:
    for esquema in (objeto({"x": {"enum": valores}}), objeto({"x": {"const": valores[0]}})):
        assert veredito(esquema, {"x": pedido}, terceiro=False) == veredito(esquema, {"x": pedido}, terceiro=True)


def test_mensagens_de_enum_e_const_sao_as_da_biblioteca() -> None:
    esquema = objeto({"e": {"enum": [[1], {"a": 2}]}, "c": {"const": [3]}})
    texto = json.dumps(esquema, sort_keys=True)
    pedido = {"e": [9], "c": [4]}
    contadas = argumentos_mod._validador(texto, True)
    marca = argumentos_mod._CONTA.set(argumentos_mod._Conta(10**9))
    try:
        delas = sorted((e.validator, e.message) for e in contadas.iter_errors(pedido))
    finally:
        argumentos_mod._CONTA.reset(marca)
    da_biblioteca = sorted((e.validator, e.message) for e in argumentos_mod._validador(texto).iter_errors(pedido))
    assert delas == da_biblioteca
    assert [v for v, _ in delas] == ["const", "enum"]


# --- Esquema false onde o erro e jogado fora (not, if, contains) -------------------------------------------------

NUMEROS_LONGOS = {f"n{k}": [10**99] * 980 for k in range(5)}
"""Dentro dos tetos (numeros de 100 digitos, 4.905 partes) e uns 500 KB: escrever o pedido numa mensagem custa caro."""

FALSE_JOGADO_FORA: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "not_false": ({"type": "object", "allOf": [{"not": False}] * 995}, NUMEROS_LONGOS),
    "if_false": ({"type": "object", "allOf": [{"if": False, "else": True}] * 660}, NUMEROS_LONGOS),
    # Com minContains 0 nenhum erro sai, mas cada item e escrito na mensagem do false; aqui, um item de 500 KB.
    "contains_false": (
        objeto({"l": {"allOf": [{"contains": False, "minContains": 0}] * 990}}),
        {"l": [NUMEROS_LONGOS]},
    ),
}


@pytest.mark.parametrize("caso", list(FALSE_JOGADO_FORA))
def test_esquema_false_em_not_if_e_contains_entra_na_conta(caso: str) -> None:
    # Sem a troca por {"not": {}}: not_false gastava 1.992 passos e 1,2 s, if_false 1.322 passos e 0,9 s, e
    # contains_false 2.974 passos e 1,1 s, porque cada false escrevia o pedido (ou o item) inteiro numa mensagem que
    # nenhuma palavra contava.
    esquema, pedido = FALSE_JOGADO_FORA[caso]
    esquema = em_json(esquema)
    recusado_pelo_orcamento(de_terceiro(esquema, pedido), pedido)


@pytest.mark.parametrize(
    ("esquema", "pedidos"),
    [
        (objeto({"a": {"not": False}}), [{"a": 1}, {"a": []}]),
        (objeto({"a": {"if": False, "then": {"type": "string"}, "else": {"type": "integer"}}}), [{"a": 1}, {"a": "x"}]),
        (objeto({"a": {"contains": False}}), [{"a": []}, {"a": [1]}, {"a": "x"}]),
        (objeto({"a": {"contains": False, "minContains": 0}}), [{"a": [1]}]),
        (objeto({"a": {"not": {"not": False}}}), [{"a": 1}]),
    ],
    ids=["not", "if", "contains", "contains_min_0", "not_not"],
)
def test_esquema_false_trocado_tem_o_mesmo_veredito(esquema: dict[str, Any], pedidos: list[dict[str, Any]]) -> None:
    for pedido in pedidos:
        assert veredito(esquema, pedido, terceiro=True) == veredito(esquema, pedido, terceiro=False), pedido


# --- Numero que a conta de multipleOf nao divide -----------------------------------------------------------------

MULTIPLO = objeto({"n": {"type": "number", "multipleOf": 0.5}})


@pytest.mark.parametrize("terceiro", [False, True], ids=["proprio", "terceiro"])
@pytest.mark.parametrize(
    "numero",
    [10**4000, -(10**400), math.inf, -math.inf, math.nan],
    ids=["inteiro_enorme", "inteiro_enorme_negativo", "infinito", "menos_infinito", "nan"],
)
def test_numero_que_a_conta_nao_divide_vira_recusa_legivel(numero: Any, terceiro: bool) -> None:
    # Antes: OverflowError (inteiro enorme dividido por fracao, infinito) ou ValueError (NaN), erro interno.
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(MULTIPLO, {"n": numero}, terceiro=terceiro)
    assert erro.value.problemas == [PROBLEMA_NUMERO]
    assert not isinstance(erro.value, ArgumentosGrandes)
    assert argumentos_mod._CONTA.get() is None
    # O que a conta divide segue como antes.
    conferir_argumentos(MULTIPLO, {"n": 10**300}, terceiro=terceiro)
    conferir_argumentos(MULTIPLO, {"n": 1.5}, terceiro=terceiro)
    with pytest.raises(ArgumentosInvalidos) as erro:
        conferir_argumentos(MULTIPLO, {"n": 1.25}, terceiro=terceiro)
    assert erro.value.problemas == ["n: regra 'multipleOf' nao atendida"]


def test_esquema_invalido_nao_vira_recusa_de_numero() -> None:
    with pytest.raises(EsquemaInvalido):
        conferir_argumentos({"type": "string"}, {})
    with pytest.raises(EsquemaInvalido):
        conferir_argumentos({"type": "object", "properties": {"n": {"multipleOf": -1}}}, {"n": math.nan})
