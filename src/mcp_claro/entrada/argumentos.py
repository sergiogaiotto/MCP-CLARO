"""Conferencia dos argumentos contra o esquema revisado (JSON Schema Draft 2020-12).

O servidor de baixo nivel do kit nao confere argumentos. A entrada confere cada
pedido contra o esquema revisado pelo ADMIN, antes das regras e da ficha. Nao
busca referencias externas (``$ref`` de fora): um registro vazio recusa.

A frase da recusa leva no maximo cinco problemas: os cinco primeiros que a conferencia acha
(``itertools.islice``), postos em ordem de caminho. Os outros erros nem sao montados, tambem num sistema proprio.
Tambem num sistema proprio, nunca erro interno: aninhamento que estoura a pilha do Python na conferencia
(``RecursionError``) vira a recusa legivel ``PROBLEMA_FUNDO``, e um numero que a conta de ``multipleOf`` nao
consegue dividir (inteiro enorme com fracao, infinito ou NaN) vira ``PROBLEMA_NUMERO``.

Ferramenta de servidor de terceiros (Fase 3). A conferencia roda dentro do laco da entrada. ``pattern`` e
``uniqueItems`` do fornecedor nunca rodam aqui (``cadastro.esquema.para_conferir``, na copia que
``repositorio.ferramenta_na_vitrine`` devolve), e a revisao de terceiro recusa ``unevaluated*``, ``$dynamicRef`` e
``$schema`` fora da raiz; o que sobra custa ate o tamanho do esquema (com as referencias seguidas, mais os valores
de ``enum`` e ``const``) vezes o tamanho do pedido. Por isso, duas barreiras, cada uma com frase legivel:

1. Tetos de tamanho (``conferir_tamanho``), antes do esquema:

   - texto com mais de ``TETO_TEXTO`` letras, em qualquer nivel, inclusive nome de campo;
   - lista com mais de ``TETO_LISTA`` itens;
   - numero inteiro com mais de ``TETO_DIGITOS`` digitos;
   - mais de ``TETO_LETRAS`` letras somando todos os textos e nomes de campo, ou mais de ``TETO_PARTES`` partes
     (cada valor, lista ou objeto conta uma): sem isso, muitos textos ou listas no limite, juntos, ainda travariam
     a entrada;
   - mais de ``cadastro.esquema.PROFUNDIDADE_MAXIMA`` niveis de listas e objetos, um dentro do outro (o
     ``TETO_NIVEIS`` de ``conferir_tamanho``): o mesmo teto de niveis do esquema de terceiro, lido de la.

   A propria medida tem custo preso aos tetos (memoria e tempo), qualquer que seja o formato dos argumentos: as
   partes sao contadas antes de entrar na pilha, o caminho de cada valor e uma ligacao ao pai, montado so na frase,
   e os numeros sao comparados, nunca escritos em texto.

2. Orcamento de passos (``ORCAMENTO_PASSOS``) na conferencia do esquema, contado e nao medido no relogio. Cada
   palavra do esquema que confere uma parte do pedido conta um passo, e mais um por item que ela percorre: da
   lista ou do mapa do esquema (``enum`` conta um por valor; ``anyOf``, ``oneOf``, ``allOf``, ``type``,
   ``required``, ``properties`` e as outras de ``_PELO_ESQUEMA``) ou das partes do pedido (``items``,
   ``contains``, ``additionalProperties``, ``propertyNames`` e as outras de ``_PELO_PEDIDO``). ``enum`` e ``const``
   comparam listas e objetos parte por parte, e cada par de partes comparado conta mais um passo (``_igual``, com
   o mesmo veredito da biblioteca). Cada erro achado conta, em cada palavra por onde passa, ``PASSOS_POR_ERRO`` e
   mais um a cada ``LETRAS_POR_PASSO`` letras da mensagem (a mensagem repete o valor conferido, e ``anyOf``/``oneOf``
   guardam os erros de todos os ramos). Um esquema ``false`` num ramo de ``anyOf``/``oneOf`` ou em ``not``, ``if`` e
   ``contains`` e conferido como ``{"not": {}}``, o mesmo veredito, para o erro dele tambem contar: sem isso, a
   biblioteca escreveria o pedido inteiro numa mensagem que ninguem conta. Passou do orcamento, a conferencia para e
   o pedido e recusado (``OrcamentoEsgotado``, com a frase ``RECUSA_ORCAMENTO``). O mesmo pedido, com o mesmo
   esquema, gasta sempre os mesmos passos.

Os tetos prendem o tamanho do pedido; os cinco primeiros erros e o orcamento prendem o trabalho e a memoria que a
conferencia gasta em Python. Ficam fora da conta, presos so pelos tetos do pedido e da revisao de terceiro
(``cadastro.esquema``, ate ``BYTES_MAXIMOS`` bytes de esquema): o que a biblioteca faz de uma vez sobre um valor so
(comparar dois textos ou dois numeros, escrever o valor na mensagem de um erro antes de o erro ser contado) e a
primeira conferencia de cada esquema (``check_schema``), que fica guardada. Um esquema caro pode recusar ate pedido
valido; quem simplifica e o fornecedor. Nada disso e garantia.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from functools import lru_cache
from itertools import islice
from typing import Any, Final

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

_REGISTRO_VAZIO: Registry[Any] = Registry()


class EsquemaInvalido(ValueError):
    pass


class ArgumentosInvalidos(ValueError):
    def __init__(self, problemas: list[str]) -> None:
        self.problemas = problemas
        super().__init__("; ".join(problemas))


class ArgumentosGrandes(ArgumentosInvalidos):
    """Argumentos grandes demais para servidor de terceiros: acima dos tetos de tamanho (``conferir_tamanho``) ou,
    na subclasse ``OrcamentoEsgotado``, caros demais para conferir com o esquema."""


class OrcamentoEsgotado(ArgumentosGrandes):
    """A conferencia do esquema de um terceiro passou de ``ORCAMENTO_PASSOS`` passos e parou no meio. A frase ao
    aplicativo e ``RECUSA_ORCAMENTO``; ``passos`` e a conta quando parou."""

    def __init__(self, passos: int) -> None:
        self.passos = passos
        super().__init__(["o esquema deste servidor ficou caro demais para conferir este pedido"])


TETO_TEXTO: Final = 10_000
"""Letras de um texto (valor ou nome de campo), num argumento para servidor de terceiros."""
TETO_LISTA: Final = 1_000
"""Itens de uma lista, num argumento para servidor de terceiros."""
TETO_LETRAS: Final = 20_000
"""Letras somando todos os textos e nomes de campo dos argumentos para servidor de terceiros."""
TETO_PARTES: Final = 5_000
"""Partes (cada valor, lista ou objeto) dos argumentos para servidor de terceiros."""
TETO_DIGITOS: Final = 100
"""Digitos de um numero inteiro, num argumento para servidor de terceiros. Sem ele, os numeros seriam a unica parte
sem tamanho preso: o Python le inteiros de ate 4.300 digitos, e escrever um deles de volta em texto (no JSON do
pedido, na mensagem de um erro da conferencia) custa muito mais que um texto do mesmo tamanho."""
_MENOR_COM_DIGITOS_DEMAIS: Final = 10**TETO_DIGITOS
ORCAMENTO_PASSOS: Final = 50_000
"""Passos da conferencia do esquema de um servidor de terceiros, por pedido (``conferir_argumentos``)."""
PASSOS_POR_ERRO: Final = 4
"""Passos de cada erro achado na conferencia de um terceiro, alem das letras da mensagem: cada erro guardado (com os
caminhos e o contexto) ocupa de 1,5 a 3 KB, e ``anyOf``/``oneOf`` guardam os erros de todos os ramos."""
LETRAS_POR_PASSO: Final = 100
"""Cada erro achado na conferencia de um terceiro conta mais um passo a cada tantas letras da mensagem."""
RECUSA_ORCAMENTO: Final = "O esquema deste servidor ficou caro demais para conferir este pedido. Nada foi enviado."
PROBLEMA_FUNDO: Final = "(raiz): aninhamento fundo demais para conferir com o esquema"
"""Problema da frase quando a conferencia estoura a pilha do Python (``RecursionError``)."""
PROBLEMA_NUMERO: Final = "(raiz): numero que nao da para conferir com o esquema"
"""Problema da frase quando a conta de um numero falha na conferencia: ``multipleOf`` fracionario com um inteiro
enorme, com infinito ou com NaN (o leitor de JSON do kit aceita ``NaN`` e ``Infinity``)."""
_PROBLEMAS: Final = 5
"""Problemas na frase de uma recusa."""
_TETO_CAMINHO: Final = 40
"""Letras de cada pedaco do caminho (nome de campo) na frase da recusa."""
_PEDACOS_NO_CAMINHO: Final = 10
"""Pedacos do caminho na frase da recusa: o primeiro, ``...`` e os ultimos."""


type _No = tuple[_No, str] | None
"""Caminho ate um valor como ligacao ao pai: (caminho do pai, pedaco). Cada item da pilha guarda so uma ligacao,
nunca uma copia do caminho inteiro; o texto so e montado quando ha problema."""


def _parte_do_caminho(chave: object) -> str:
    texto = str(chave)
    return texto if len(texto) <= _TETO_CAMINHO else texto[:_TETO_CAMINHO] + "..."


def _caminho(no: _No) -> str:
    pedacos: list[str] = []
    while no is not None:
        no, pedaco = no
        pedacos.append(pedaco)
    pedacos.reverse()
    if len(pedacos) > _PEDACOS_NO_CAMINHO:
        pedacos = [pedacos[0], "...", *pedacos[2 - _PEDACOS_NO_CAMINHO :]]
    return "/".join(pedacos) or "(raiz)"


def conferir_tamanho(argumentos: Mapping[str, Any] | None) -> None:
    """Levanta ``ArgumentosGrandes`` com ate cinco problemas legiveis, sem repetir os valores.

    Anda pelos argumentos sem recursao (uma pilha), entao nem o aninhamento fundo derruba a conferencia. O custo
    fica preso aos tetos, qualquer que seja o formato: as partes de uma lista ou de um objeto sao contadas antes de
    entrar na pilha (a pilha nunca passa de ``TETO_PARTES`` itens), e cada item guarda so a ligacao ao caminho do
    pai. Os argumentos sao o primeiro nivel; uma lista ou um objeto abaixo de ``TETO_NIVEIS`` niveis e recusado sem
    ser aberto. Um inteiro com mais de ``TETO_DIGITOS`` digitos e recusado sem ser escrito em texto."""
    # O mesmo teto de niveis do esquema de terceiro, importado e nao copiado. Importado aqui, e nao no topo:
    # cadastro.esquema importa EsquemaInvalido deste modulo, e a importacao no topo fecharia um ciclo.
    from mcp_claro.cadastro.esquema import PROFUNDIDADE_MAXIMA as TETO_NIVEIS

    problemas: list[str] = []
    letras = 0
    partes = 1  # a raiz; cada lista ou objeto soma as partes dele antes de empilhar
    pilha: list[tuple[Any, _No, int]] = [(dict(argumentos or {}), None, 1)]
    muitas_partes = f"(raiz): mais de {TETO_PARTES} partes somando todos os valores"
    while pilha and len(problemas) < _PROBLEMAS:
        valor, no, nivel = pilha.pop()
        if isinstance(valor, str):
            letras += len(valor)
            if len(valor) > TETO_TEXTO:
                problemas.append(f"{_caminho(no)}: texto com mais de {TETO_TEXTO} letras")
        elif isinstance(valor, int) and not isinstance(valor, bool):
            # Comparado, nunca escrito em texto: escrever um inteiro enorme ja seria o custo que o teto evita.
            if abs(valor) >= _MENOR_COM_DIGITOS_DEMAIS:
                problemas.append(f"{_caminho(no)}: numero com mais de {TETO_DIGITOS} digitos")
        elif isinstance(valor, Mapping | list | tuple):
            if nivel > TETO_NIVEIS:
                # Nem e aberto: o que tem dentro fica mais fundo ainda.
                problemas.append(
                    f"{_caminho(no)}: mais de {TETO_NIVEIS} niveis de listas e objetos, um dentro do outro"
                )
                continue
            if not isinstance(valor, Mapping) and len(valor) > TETO_LISTA:
                # Os itens nem sao olhados: a lista ja e recusada.
                problemas.append(f"{_caminho(no)}: lista com mais de {TETO_LISTA} itens")
                continue
            partes += len(valor)
            if partes > TETO_PARTES:
                problemas.append(muitas_partes)
                break
            if isinstance(valor, Mapping):
                filhos: list[tuple[Any, _No, int]] = []
                for chave, item in valor.items():
                    nome = str(chave)
                    letras += len(nome)
                    if len(nome) > TETO_TEXTO and len(problemas) < _PROBLEMAS:
                        problemas.append(f"{_caminho(no)}: nome de campo com mais de {TETO_TEXTO} letras")
                    filhos.append((item, (no, _parte_do_caminho(nome)), nivel + 1))
                pilha.extend(reversed(filhos))
            else:
                pilha.extend((valor[n], (no, str(n)), nivel + 1) for n in reversed(range(len(valor))))
        if letras > TETO_LETRAS:
            problemas.append(f"(raiz): mais de {TETO_LETRAS} letras somando todos os textos")
            break
    if problemas:
        raise ArgumentosGrandes(problemas[:_PROBLEMAS])


# --- Orcamento de passos (so terceiro) --------------------------------------------------------------------------


class _Acabou(Exception):
    """Passou do orcamento: sai do meio da conferencia, por cima de todas as palavras. Nao sai deste modulo: vira
    ``OrcamentoEsgotado``."""


class _Conta:
    """Passos gastos numa conferencia. Uma por pedido, posta em ``_CONTA`` enquanto a conferencia roda."""

    __slots__ = ("orcamento", "passos")

    def __init__(self, orcamento: int) -> None:
        self.orcamento = orcamento
        self.passos = 0

    def gastar(self, passos: int) -> None:
        self.passos += passos
        if self.passos > self.orcamento:
            raise _Acabou

    def erros(self, erros: Iterable[Any]) -> Iterator[Any]:
        """Cada erro que passa por uma palavra conta ``PASSOS_POR_ERRO`` e mais um a cada ``LETRAS_POR_PASSO``
        letras: a mensagem repete o valor conferido (e, no ``enum``, todos os valores)."""
        for erro in erros:
            self.gastar(PASSOS_POR_ERRO + len(erro.message) // LETRAS_POR_PASSO)
            yield erro


_CONTA: ContextVar[_Conta | None] = ContextVar("mcp_claro_conta_da_conferencia", default=None)

type _Palavra = Callable[[Any, Any, Any, Any], Iterable[Any] | None]

_COM_RAMOS: Final = frozenset({"anyOf", "oneOf"})
"""Palavras com uma lista de ramos, conferidos com ``descend``; os erros de todos os ramos ficam guardados."""
_SO_PERGUNTAM: Final = frozenset({"not", "if", "contains"})
"""Palavras que so perguntam se um esquema vale (``is_valid``) e jogam fora o erro."""
_NADA_VALE: Final[dict[str, Any]] = {"not": {}}
"""O mesmo veredito do esquema ``false``, mas conferido por uma palavra (``not``), entao entra na conta."""

type _Container = type[list[Any]] | type[dict[str, Any]]
"""Lista ou objeto: o tipo da parte do pedido em que uma palavra trabalha."""

_PELO_ESQUEMA: Final[dict[str, _Container | None]] = {
    "allOf": None,
    "anyOf": None,
    "oneOf": None,
    "enum": None,
    "type": None,
    "prefixItems": list,
    "required": dict,
    "properties": dict,
    "patternProperties": dict,
    "dependentSchemas": dict,
    "dependentRequired": dict,
}
"""Palavras que percorrem a lista ou o mapa delas no esquema: um passo a mais por item (``enum``: por valor), quando a
parte do pedido e do tipo em que a palavra trabalha (``None``: qualquer uma). Num ramo vazio (``{}`` ou ``true``)
nenhuma palavra roda, e so esta conta paga o trabalho de entrar nele."""

_PELO_PEDIDO: Final[dict[str, _Container]] = {
    "items": list,
    "prefixItems": list,
    "contains": list,
    "unevaluatedItems": list,
    "additionalProperties": dict,
    "patternProperties": dict,
    "propertyNames": dict,
    "unevaluatedProperties": dict,
}
"""Palavras que percorrem as partes do pedido (os itens da lista ou os campos do objeto): um passo a mais por parte.
Com um esquema vazio dentro (``items: {}``), so esta conta paga o trabalho de andar pela lista. ``patternProperties``
e ``unevaluated*`` sao recusadas na revisao de terceiro e ficam aqui so por defesa."""


def _passos(nome: str, valor: Any, instancia: Any) -> int:
    """Passos de uma chamada da palavra ``nome``: um, e mais um por item que ela percorre (``_PELO_ESQUEMA`` e
    ``_PELO_PEDIDO``). ``dependentRequired`` percorre tambem a lista de nomes de cada campo."""
    passos = 1
    if nome in _PELO_ESQUEMA and isinstance(valor, list | dict):
        tipo = _PELO_ESQUEMA[nome]
        if tipo is None or isinstance(instancia, tipo):
            passos += len(valor)
            if nome == "dependentRequired" and isinstance(valor, dict):
                passos += sum(len(nomes) for nomes in valor.values() if isinstance(nomes, list))
    tipo_do_pedido = _PELO_PEDIDO.get(nome)
    if tipo_do_pedido is not None and isinstance(instancia, tipo_do_pedido):
        passos += len(instancia)
    return passos


def _contada(nome: str, palavra: _Palavra) -> _Palavra:
    """A palavra ``nome`` do validador, contando os passos de cada chamada (``_passos``) e os erros dela."""

    def contada(validador: Any, valor: Any, instancia: Any, esquema: Any) -> Iterable[Any] | None:
        conta = _CONTA.get()
        if conta is None:
            return palavra(validador, valor, instancia, esquema)
        # Antes de chamar: uma palavra cara demais (um enum enorme) nem roda.
        conta.gastar(_passos(nome, valor, instancia))
        # Um esquema false gera o erro fora de toda palavra (na biblioteca), com o valor inteiro na mensagem: em
        # anyOf/oneOf, que guardam os erros de todos os ramos, e em not/if/contains, que jogam o erro fora sem ele
        # passar por palavra nenhuma. Trocado pelo mesmo veredito, o erro passa por uma palavra e entra na conta.
        if nome in _COM_RAMOS and isinstance(valor, list):
            valor = [_NADA_VALE if ramo is False else ramo for ramo in valor]
        elif nome in _SO_PERGUNTAM and valor is False:
            valor = _NADA_VALE
        erros = palavra(validador, valor, instancia, esquema)
        return None if erros is None else conta.erros(erros)

    return contada


# --- enum e const com a comparacao contada ----------------------------------------------------------------------

_VERDADEIRO: Final = object()
_FALSO: Final = object()
_FIM: Final = object()
_FALTA: Final = object()


def _sem_bool(valor: Any) -> Any:
    """Como ``jsonschema._utils.unbool``: ``true`` e ``false`` nunca sao iguais a 1 e 0."""
    if valor is True:
        return _VERDADEIRO
    if valor is False:
        return _FALSO
    return valor


def _pares_do_objeto(um: Mapping[Any, Any], outro: Mapping[Any, Any]) -> Iterator[Any]:
    for chave, valor in um.items():
        if chave not in outro:
            yield _FALTA
            return
        yield valor, outro[chave]


def _igual(conta: _Conta | None, um: Any, outro: Any) -> bool:
    """O veredito de ``jsonschema._utils.equal``, a comparacao de ``enum`` e ``const``: ``true`` diferente de 1, listas
    e objetos comparados parte por parte. Sem recursao (uma pilha de pares por abrir), na mesma ordem da biblioteca, e
    contando um passo por par de partes de dentro de uma lista ou de um objeto: a comparacao de dois valores com
    milhares de partes custa milhares de passos. O primeiro par, ja pago pela palavra, nao conta."""
    pilha: list[Iterator[Any]] = []
    par: Any = (um, outro)
    while True:
        a, b = par
        if a is b:
            pass
        elif isinstance(a, str) or isinstance(b, str):
            if a != b:
                return False
        elif isinstance(a, Sequence) and isinstance(b, Sequence):
            if len(a) != len(b):
                return False
            pilha.append(zip(a, b, strict=True))
        elif isinstance(a, Mapping) and isinstance(b, Mapping):
            if len(a) != len(b):
                return False
            pilha.append(_pares_do_objeto(a, b))
        elif _sem_bool(a) != _sem_bool(b):
            return False
        while pilha:
            par = next(pilha[-1], _FIM)
            if par is not _FIM:
                break
            pilha.pop()
        else:
            return True
        if par is _FALTA:
            return False
        if conta is not None:
            conta.gastar(1)


def _enum_contado(validador: Any, valores: Any, instancia: Any, esquema: Any) -> Iterator[ValidationError]:
    """A palavra ``enum`` da biblioteca (a mesma mensagem), com ``_igual``."""
    conta = _CONTA.get()
    if all(not _igual(conta, valor, instancia) for valor in valores):
        yield ValidationError(f"{instancia!r} is not one of {valores!r}")


def _const_contado(validador: Any, valor: Any, instancia: Any, esquema: Any) -> Iterator[ValidationError]:
    """A palavra ``const`` da biblioteca (a mesma mensagem), com ``_igual``."""
    if not _igual(_CONTA.get(), instancia, valor):
        yield ValidationError(f"{valor!r} was expected")


_COMPARAM: Final[dict[str, _Palavra]] = {"enum": _enum_contado, "const": _const_contado}
"""Palavras que comparam valores inteiros: no validador que conta, com a comparacao contada (``_igual``)."""

_ValidadorContado: Any = validators.extend(
    Draft202012Validator,
    validators={
        nome: _contada(nome, _COMPARAM.get(nome, palavra)) for nome, palavra in Draft202012Validator.VALIDATORS.items()
    },
)
"""O validador que a entrada ja usa, com cada palavra embrulhada pela conta (``_contada``) e ``enum`` e ``const``
comparando com a conta (``_igual``)."""


def conferir_esquema(esquema: Mapping[str, Any]) -> None:
    """O esquema revisado precisa ser valido e de objeto (exigencia da 2026-07-28)."""
    if esquema.get("type") != "object":
        raise EsquemaInvalido('o esquema de entrada precisa ter "type": "object"')
    try:
        Draft202012Validator.check_schema(esquema)
    except SchemaError as erro:
        raise EsquemaInvalido(f"esquema invalido: {erro.message}") from erro


@lru_cache(maxsize=1024)
def _validador(esquema_json: str, terceiro: bool = False) -> Any:
    """O validador do esquema, guardado. ``terceiro``: o que conta os passos (``_ValidadorContado``). Nenhum dos dois
    confere ``format``, como nas Fases 1 e 2."""
    esquema = json.loads(esquema_json)
    conferir_esquema(esquema)
    classe = _ValidadorContado if terceiro else Draft202012Validator
    return classe(esquema, registry=_REGISTRO_VAZIO)


def conferir_argumentos(
    esquema: Mapping[str, Any], argumentos: Mapping[str, Any] | None, *, terceiro: bool = False
) -> int:
    """Levanta ``ArgumentosInvalidos`` com ate cinco problemas legiveis, sem repetir os valores.

    ``terceiro``: conta os passos e, passou de ``ORCAMENTO_PASSOS``, levanta ``OrcamentoEsgotado``. Devolve os
    passos gastos (0 num sistema proprio, que nao conta)."""
    conta = _Conta(ORCAMENTO_PASSOS) if terceiro else None
    marca = _CONTA.set(conta)
    try:
        try:
            validador = _validador(json.dumps(esquema, sort_keys=True), terceiro)
        except RecursionError as erro:
            raise ArgumentosInvalidos([PROBLEMA_FUNDO]) from erro
        try:
            erros = list(islice(validador.iter_errors(dict(argumentos or {})), _PROBLEMAS))
        except Unresolvable as erro:
            raise ArgumentosInvalidos(["o esquema cita uma referencia externa, que nao e buscada"]) from erro
        except _Acabou as erro:
            raise OrcamentoEsgotado(conta.passos if conta is not None else 0) from erro
        except RecursionError as erro:
            raise ArgumentosInvalidos([PROBLEMA_FUNDO]) from erro
        except (ArithmeticError, ValueError) as erro:
            # So a conta de multipleOf da biblioteca levanta isso: inteiro enorme dividido por fracao (OverflowError),
            # infinito (OverflowError) ou NaN (ValueError). O esquema invalido (EsquemaInvalido) vem de _validador,
            # acima, e nao cai aqui.
            raise ArgumentosInvalidos([PROBLEMA_NUMERO]) from erro
    finally:
        _CONTA.reset(marca)
    if erros:
        problemas = []
        for falha in sorted(erros, key=lambda e: list(e.absolute_path)):
            caminho = "/".join(str(p) for p in falha.absolute_path) or "(raiz)"
            problemas.append(f"{caminho}: regra '{falha.validator}' nao atendida")
        raise ArgumentosInvalidos(problemas)
    return conta.passos if conta is not None else 0
