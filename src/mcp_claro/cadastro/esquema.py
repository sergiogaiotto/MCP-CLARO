"""Esquema limpo de um servidor de terceiros: so o que valida fica; o texto do fornecedor sai.

O aplicativo (e o modelo de IA por tras dele) le o esquema de entrada de cada ferramenta da vitrine. Num servidor
de terceiros, nada do texto escrito pelo fornecedor deve chegar la: descricao, titulo, exemplos, comentario,
valor padrao e qualquer palavra que o JSON Schema nao usa para validar. ``limpar_textos`` fica so com as
palavras que mudam o veredito da validacao (JSON Schema 2020-12) e com as de estrutura (``$ref``, ``$defs``...);
``format`` fica so com os nomes do proprio JSON Schema. O que continua indo ao modelo, porque e preciso para
chamar: nomes de campo, valores de ``enum``/``const``, ``pattern`` e identificadores (``$id``, ``$anchor``).

``com_descricoes`` poe as descricoes de campo escritas pelo ADMIN, so no primeiro nivel.

A entrada confere cada argumento com o esquema revisado, sem prazo (``entrada.argumentos``). Num terceiro, a
revisao recusa o que pode travar essa conferencia com um pedido feito de proposito:

- ``conferir_padroes`` (critica B3): regra de formato longa, invalida ou cara. A conta le a regra pela arvore do
  proprio ``re`` (entende ``(?x)``, comentarios, classes e escapes) e recusa repeticao de algo ambiguo
  (``(a+)+``, ``(a|aa)+``, ``(a?){30}``), combinacoes demais (``a?a?a?...``) e repeticoes longas em sequencia
  que disputam as mesmas letras (``a+a+a+``). Ver ``custo_da_regra``.
- ``conferir_custo``: o esquema com as referencias seguidas (``$ref`` dentro de ``$ref``) grande demais, em
  ciclo, com ``$dynamicRef`` ou com referencia que nao acha uma parte do proprio esquema.
- ``conferir_referencias``: referencia que so achava algo no esquema do fornecedor e deixou de achar no limpo.

Nada disso e garantia: a conta e conservadora (recusa algumas regras seguras) e um texto muito longo ainda pesa
numa regra aceita (ver ``custo_da_regra``).
"""

from __future__ import annotations

import copy
import importlib
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from mcp_claro.entrada.argumentos import EsquemaInvalido
from mcp_claro.regras.escondidos import marcar_escondidos

if TYPE_CHECKING:
    from referencing._core import Resolved, Resolver

DESCRICAO_DE_CAMPO_MAXIMA: Final = 300
"""Letras de uma descricao de campo escrita pelo ADMIN."""

PADRAO_MAXIMO: Final = 256
"""Letras de uma regra de formato (``pattern`` ou chave de ``patternProperties``) aceita num terceiro."""

ESCOLHAS_MAXIMAS: Final = 1024
"""Caminhos que uma regra de formato de terceiro pode tentar (``(a?){10}`` tem 1024)."""

GRAU_MAXIMO: Final = 2
"""Repeticoes longas que disputam as mesmas letras, em sequencia, numa regra de terceiro (contando a busca)."""

CUSTO_MAXIMO: Final = 2000
"""Partes de um esquema de entrada de terceiro, contadas com as referencias seguidas."""

PROFUNDIDADE_MAXIMA: Final = 64
"""Niveis de um esquema de entrada de terceiro, contados com as referencias seguidas."""

# Palavras que valem para a validacao (JSON Schema 2020-12, as do Draft202012Validator e as que ele le junto:
# then/else com if, minContains/maxContains com contains) e as de estrutura, que as referencias usam.
_ESTRUTURA: Final = frozenset({"$schema", "$id", "$anchor", "$dynamicAnchor", "$ref", "$dynamicRef"})
_VALIDACAO: Final = frozenset(
    {
        "type",
        "enum",
        "const",
        "multipleOf",
        "maximum",
        "exclusiveMaximum",
        "minimum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "pattern",
        "maxItems",
        "minItems",
        "uniqueItems",
        "maxContains",
        "minContains",
        "maxProperties",
        "minProperties",
        "required",
        "dependentRequired",
    }
)
# Palavras que guardam esquemas: o limpador desce por elas.
_UM_ESQUEMA: Final = frozenset(
    {
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "items",
    }
)
_LISTA_DE_ESQUEMAS: Final = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# Mapas de nome para esquema: os NOMES ficam como estao (um campo pode se chamar "description").
_MAPA_DE_ESQUEMAS: Final = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
# Guardam esquemas que so valem quando uma referencia os chama.
_DEFINICOES: Final = frozenset({"$defs", "definitions"})
FORMATOS: Final = frozenset(
    {
        "date-time",
        "date",
        "time",
        "duration",
        "email",
        "idn-email",
        "hostname",
        "idn-hostname",
        "ipv4",
        "ipv6",
        "uri",
        "uri-reference",
        "iri",
        "iri-reference",
        "uuid",
        "uri-template",
        "json-pointer",
        "relative-json-pointer",
        "regex",
    }
)
"""Os formatos do proprio JSON Schema. Outro valor em ``format`` e texto do fornecedor e sai."""


class CampoDesconhecido(ValueError):
    """Descricao para um campo que o esquema de entrada nao tem no primeiro nivel."""

    def __init__(self, campos: list[str]) -> None:
        self.campos = campos
        super().__init__("campos fora do esquema de entrada: " + ", ".join(marcar_escondidos(c) for c in campos))


class EsquemaRecusado(EsquemaInvalido):
    """Esquema valido que a revisao de terceiro nao aceita. A mensagem e para o ADMIN; ``onde`` (com os
    escondidos ja marcados) e ``motivo`` servem para a tela montar a propria frase."""

    def __init__(self, mensagem: str, *, onde: str, motivo: str) -> None:
        self.onde = onde
        self.motivo = motivo
        super().__init__(mensagem)


MotivoPadrao = Literal["longa", "invalida", "aninhada", "escolhas", "seguidas"]
MotivoEsquema = Literal["referencia", "fora", "ciclo", "dinamica", "grande", "fundo"]


class PadraoRecusado(EsquemaRecusado):
    """Regra de formato que a revisao de terceiro nao aceita (``motivo``: um de ``MotivoPadrao``)."""

    def __init__(self, mensagem: str, *, onde: str, motivo: MotivoPadrao) -> None:
        super().__init__(mensagem, onde=onde, motivo=motivo)
        self.motivo: MotivoPadrao = motivo


def _limpar(no: Any, *, descricoes_de_campo: bool) -> Any:
    if not isinstance(no, dict):
        return copy.deepcopy(no)  # esquema booleano (true/false) ou valor que o validador recusa depois
    limpo: dict[str, Any] = {}
    for chave, valor in no.items():
        if chave in _MAPA_DE_ESQUEMAS and isinstance(valor, dict):
            limpo[chave] = {nome: _limpar(sub, descricoes_de_campo=False) for nome, sub in valor.items()}
            if descricoes_de_campo and chave == "properties":
                for nome, sub in valor.items():
                    if isinstance(sub, dict) and isinstance(sub.get("description"), str):
                        limpo[chave][nome]["description"] = sub["description"]
        elif chave in _LISTA_DE_ESQUEMAS and isinstance(valor, list):
            limpo[chave] = [_limpar(sub, descricoes_de_campo=False) for sub in valor]
        elif chave in _UM_ESQUEMA:
            limpo[chave] = _limpar(valor, descricoes_de_campo=False)
        elif chave == "format":
            if valor in FORMATOS:
                limpo[chave] = valor
        elif chave in _VALIDACAO or chave in _ESTRUTURA or chave in _MAPA_DE_ESQUEMAS or chave in _LISTA_DE_ESQUEMAS:
            limpo[chave] = copy.deepcopy(valor)  # dado (enum, const, required...), nunca descido
    return limpo


def limpar_textos(esquema: Mapping[str, Any], *, descricoes_de_campo: bool = False) -> dict[str, Any]:
    """Copia do esquema so com o que valida: tira ``description``, ``title``, ``examples``, ``$comment``,
    ``default``, ``deprecated``, ``readOnly``, ``writeOnly``, ``x-*``, ``content*`` e qualquer palavra que o JSON
    Schema 2020-12 nao usa, em todos os niveis. Nunca mexe nos nomes de ``properties`` nem nos valores de
    ``enum``, ``const`` e ``required``. O veredito da validacao e o mesmo com o esquema original e com o limpo.

    ``descricoes_de_campo``: deixa a ``description`` dos campos do primeiro nivel (num terceiro, gravado pela
    revisao, sao as do ADMIN). A vitrine usa para limpar de novo o que ja foi limpo, sem perder essas descricoes.
    """
    return dict(_limpar(dict(esquema), descricoes_de_campo=descricoes_de_campo))


def com_descricoes(esquema: Mapping[str, Any], descricoes: Mapping[str, str]) -> dict[str, Any]:
    """Copia do esquema com as descricoes do ADMIN nos campos do primeiro nivel. Descricao vazia nao entra.

    Campo que o esquema nao tem levanta ``CampoDesconhecido``; descricao longa demais, ``ValueError``.
    """
    saida = copy.deepcopy(dict(esquema))
    textos = {campo: str(texto).strip() for campo, texto in descricoes.items() if str(texto).strip()}
    if not textos:
        return saida
    propriedades = saida.get("properties")
    conhecidos = propriedades if isinstance(propriedades, dict) else {}
    fora = [campo for campo in textos if campo not in conhecidos]
    if fora:
        raise CampoDesconhecido(fora)
    longas = [campo for campo, texto in textos.items() if len(texto) > DESCRICAO_DE_CAMPO_MAXIMA]
    if longas:
        raise ValueError(
            f"descricao de campo com mais de {DESCRICAO_DE_CAMPO_MAXIMA} letras: "
            + ", ".join(marcar_escondidos(c) for c in longas)
        )
    for campo, texto in textos.items():
        sub = conhecidos[campo]
        if sub is True:
            sub = {}
        elif sub is False:
            sub = {"not": {}}
        conhecidos[campo] = {**sub, "description": texto} if isinstance(sub, dict) else sub
    return saida


# --- Partes do esquema e referencias ------------------------------------------------------------------------------


def _filhos(no: dict[str, Any], *, com_definicoes: bool = True) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Os esquemas logo abaixo de ``no`` (as mesmas palavras por onde o limpador desce), com o caminho."""
    for chave, valor in no.items():
        if chave in _MAPA_DE_ESQUEMAS and isinstance(valor, dict):
            if com_definicoes or chave not in _DEFINICOES:
                for nome, sub in valor.items():
                    yield (chave, str(nome)), sub
        elif chave in _LISTA_DE_ESQUEMAS and isinstance(valor, list):
            for i, sub in enumerate(valor):
                yield (chave, str(i)), sub
        elif chave in _UM_ESQUEMA:
            yield (chave,), valor


def _esquemas(no: Any, caminho: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
    """Cada objeto de esquema, com o caminho ate ele."""
    if not isinstance(no, dict):
        return
    yield caminho, no
    for sub_caminho, sub in _filhos(no):
        yield from _esquemas(sub, (*caminho, *sub_caminho))


def _lugar(caminho: tuple[str, ...]) -> str:
    return marcar_escondidos("/".join(caminho) or "(raiz)")[:200]


def _resolvedor_da_raiz(esquema: Any) -> Resolver[Any]:
    """Como a entrada resolve: as referencias do proprio esquema, nada buscado de fora."""
    return Registry().resolver_with_root(DRAFT202012.create_resource(esquema))


def _no_subrecurso(resolvedor: Resolver[Any], no: Any) -> Resolver[Any]:
    """O resolvedor dentro de ``no``, filho do lugar de ``resolvedor``: muda de base quando ``no`` tem ``$id``
    (como a entrada faz ao descer de um esquema para o de dentro). Nao vale para a raiz nem para a parte achada
    por uma referencia: essas ja vem com o resolvedor certo."""
    if isinstance(no, dict) and isinstance(no.get("$id"), str):
        return resolvedor.in_subresource(DRAFT202012.create_resource(no))
    return resolvedor


def _com_resolvedor(
    no: Any, resolvedor: Resolver[Any], caminho: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], dict[str, Any], Resolver[Any]]]:
    """Cada objeto de esquema com o caminho e o resolvedor que vale ali (a base muda a cada ``$id``).
    ``resolvedor`` e o do proprio ``no``."""
    if not isinstance(no, dict):
        return
    yield caminho, no, resolvedor
    for sub_caminho, sub in _filhos(no):
        yield from _com_resolvedor(sub, _no_subrecurso(resolvedor, sub), (*caminho, *sub_caminho))


def _resolver(resolvedor: Resolver[Any], alvo: str) -> Resolved[Any] | None:
    """A parte que a referencia acha, ou None. O ponteiro e decodificado como na entrada (``%20``, ``~0``, ``~1``)."""
    try:
        return resolvedor.lookup(alvo)
    except Unresolvable, LookupError, ValueError, TypeError:
        return None  # PointerToNowhere, ancora que nao existe, indice que nao e numero...


def conferir_referencias(original: Mapping[str, Any], limpo: Mapping[str, Any]) -> None:
    """Uma referencia (``$ref``, ``$dynamicRef``) que achava algo no original precisa achar no limpo.

    Ela so deixa de achar quando aponta para dentro de uma parte que so tinha texto do fornecedor (uma palavra
    desconhecida, por exemplo): o esquema e recusado na revisao (``EsquemaRecusado``, motivo ``referencia``), em
    vez de falhar na hora de cada chamada. A referencia e resolvida como a entrada resolve: ponteiro decodificado
    e ``$id`` (inclusive referencia absoluta pelo ``$id``), sem buscar nada de fora.
    """
    no_original = {
        caminho: resolvedor for caminho, _, resolvedor in _com_resolvedor(original, _resolvedor_da_raiz(original))
    }
    for caminho, no, resolvedor in _com_resolvedor(limpo, _resolvedor_da_raiz(limpo)):
        for palavra in ("$ref", "$dynamicRef"):
            alvo = no.get(palavra)
            if not isinstance(alvo, str) or _resolver(resolvedor, alvo) is not None:
                continue
            antes = no_original.get(caminho)
            if antes is not None and _resolver(antes, alvo) is not None:
                onde = _lugar(caminho)
                raise EsquemaRecusado(
                    f"a referencia em {onde} aponta para uma parte do esquema que so tinha texto do fornecedor",
                    onde=onde,
                    motivo="referencia",
                )


class _Conta:
    """O custo do esquema com as referencias seguidas, cada parte contada uma vez so (memoria por parte)."""

    def __init__(self) -> None:
        self.memoria: dict[int, int] = {}
        self.pilha: set[int] = set()

    def recusar(self, caminho: tuple[str, ...], motivo: MotivoEsquema) -> EsquemaRecusado:
        onde = _lugar(caminho)
        frases = {
            "fora": f"a referencia em {onde} nao acha uma parte deste esquema; a revisao de terceiro so aceita "
            "referencia para dentro do proprio esquema",
            "ciclo": f"a referencia em {onde} volta a uma parte que a contem (esquema em ciclo); a revisao de "
            "terceiro nao aceita esquema em ciclo",
            "dinamica": f"o esquema usa $dynamicRef em {onde}; a revisao de terceiro so aceita $ref",
            "grande": f"o esquema fica com mais de {CUSTO_MAXIMO} partes quando se seguem as referencias ($ref) "
            f"(passou em {onde}); isso pode travar o MCP-CLARO a cada chamada",
            "fundo": f"o esquema tem mais de {PROFUNDIDADE_MAXIMA} niveis quando se seguem as referencias "
            f"(passou em {onde})",
        }
        return EsquemaRecusado(
            frases[motivo] + ". Peca ao fornecedor um esquema mais simples", onde=onde, motivo=motivo
        )

    def custo(self, no: Any, resolvedor: Resolver[Any], caminho: tuple[str, ...]) -> int:
        """``resolvedor``: o do proprio ``no`` (o filho recebe ``_no_subrecurso``; a parte achada por ``$ref``,
        o resolvedor que a busca devolveu, como na entrada)."""
        if not isinstance(no, dict):
            return 1  # esquema booleano
        chave = id(no)
        if chave in self.memoria:
            return self.memoria[chave]
        if chave in self.pilha:
            raise self.recusar(caminho, "ciclo")
        if len(self.pilha) >= PROFUNDIDADE_MAXIMA:
            raise self.recusar(caminho, "fundo")
        if "$dynamicRef" in no:
            raise self.recusar(caminho, "dinamica")
        self.pilha.add(chave)
        total = 1
        partes: list[tuple[tuple[str, ...], Any, Resolver[Any]]] = [
            ((*caminho, *sub_caminho), sub, _no_subrecurso(resolvedor, sub))
            for sub_caminho, sub in _filhos(no, com_definicoes=False)
        ]
        alvo = no.get("$ref")
        if isinstance(alvo, str):
            achado = _resolver(resolvedor, alvo)
            conteudo = achado.contents if achado is not None else None
            # O registro so tem este esquema: o que nao acha nada aqui dentro vem de fora (ou nao existe).
            if achado is None or not isinstance(conteudo, (dict, bool)):
                raise self.recusar((*caminho, "$ref"), "fora")
            partes.append(((*caminho, "$ref"), conteudo, achado.resolver))
        for sub_caminho, sub, sub_resolvedor in partes:
            total += self.custo(sub, sub_resolvedor, sub_caminho)
            if total > CUSTO_MAXIMO:
                raise self.recusar(sub_caminho, "grande")
        self.pilha.discard(chave)
        self.memoria[chave] = total
        return total


def custo_do_esquema(esquema: Mapping[str, Any]) -> int:
    """Quantas partes a conferencia de um argumento pode visitar: cada objeto de esquema conta uma vez, e cada
    ``$ref`` conta de novo a parte que ele acha (``$defs`` so contam quando citados). Levanta ``EsquemaRecusado``
    (``conferir_custo`` explica os motivos)."""
    conta = _Conta()
    return conta.custo(esquema, _resolvedor_da_raiz(esquema), ())


def conferir_custo(esquema: Mapping[str, Any]) -> None:
    """Revisao de terceiro do tamanho do esquema com as referencias seguidas. Um ``$ref`` que chama duas vezes
    outro, que chama duas vezes outro... dobra a cada nivel: com 20 niveis, cada chamada custaria um milhao de
    partes. Recusa (``EsquemaRecusado``, frase para o ADMIN) com o motivo:

    - ``grande``: mais de ``CUSTO_MAXIMO`` partes; ``fundo``: mais de ``PROFUNDIDADE_MAXIMA`` niveis;
    - ``ciclo``: referencia que volta a uma parte que a contem (esquema recursivo);
    - ``dinamica``: ``$dynamicRef`` (o alvo muda conforme o caminho; nao da para contar antes);
    - ``fora``: referencia que nao acha uma parte deste esquema (de fora, ou que nao acha nada).
    """
    custo_do_esquema(esquema)


# --- Regras de formato (critica B3) --------------------------------------------------------------------------------

# A arvore do proprio re (a mesma leitura que a entrada faz ao conferir): entende (?x), comentarios, classes,
# escapes e flags. E modulo interno do Python: o teste confere que ele continua com o formato que a conta usa, e
# um no desconhecido recusa a regra (o lado que aperta).
_sre: Any = importlib.import_module("re._parser")
_op: Any = importlib.import_module("re._constants")

_FAIXA_CURTA: Final = 8
"""Repeticao com teto e ate 8 escolhas de tamanho (``\\d{1,3}``) conta como escolhas; mais que isso, como longa."""
_TETO: Final = 10**12

# Pontos de prova para saber se duas classes grandes (\d, \w, [^@], faixas grandes) tem letra em comum: ASCII,
# Latin-1 e alguns de outras escritas (digitos, espacos, largura cheia, letras com dobra de caixa especial).
_PROVAS: Final = frozenset(
    {
        *range(0x300),
        *range(0x660, 0x66A),
        *range(0x6F0, 0x6FA),
        *range(0x966, 0x970),
        *range(0x2000, 0x200C),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        *range(0xFF10, 0xFF1A),
        *range(0xFF21, 0xFF3B),
        *range(0xFF41, 0xFF5B),
        0x0130,
        0x0131,
        0x017F,
        0x212A,
        0x212B,
        0x0E50,
        0x4E00,
        0xAC00,
        0xE000,
        0xFFFD,
        0x1D7CE,
        0x10FFFF,
    }
)


@dataclass(frozen=True, slots=True)
class _Letras:
    """Um conjunto de letras para a conta. ``contadas``: ``provas`` e o conjunto inteiro; senao, ``provas`` sao
    pontos de prova (pontas e meio das faixas) e o teste diz quem esta dentro."""

    testes: tuple[Callable[[int], bool], ...]
    provas: frozenset[int]
    contadas: bool
    tudo: bool = False

    def tem(self, ponto: int) -> bool:
        return self.tudo or any(teste(ponto) for teste in self.testes)


_VAZIO: Final = _Letras((), frozenset(), True)
_TUDO: Final = _Letras((), frozenset(), False, tudo=True)


def _uniao(a: _Letras, b: _Letras) -> _Letras:
    if a.tudo or b.tudo:
        return _TUDO
    if a is _VAZIO:
        return b
    if b is _VAZIO:
        return a
    return _Letras(a.testes + b.testes, a.provas | b.provas, a.contadas and b.contadas)


def _cruzam(a: _Letras, b: _Letras) -> bool:
    """As duas tem alguma letra em comum? Na duvida entre duas classes grandes, decide pelos pontos de prova."""
    if a.contadas:
        return any(b.tem(p) for p in a.provas)
    if b.contadas:
        return any(a.tem(p) for p in b.provas)
    return any(a.tem(p) and b.tem(p) for p in _PROVAS | a.provas | b.provas)


def _variantes(ponto: int) -> set[int]:
    letra = chr(ponto)
    saida = {ponto}
    for outra in (letra.lower(), letra.upper(), letra.casefold(), letra.swapcase(), letra.title()):
        saida.update(ord(c) for c in outra)
    return saida


_CATEGORIAS: Final[dict[str, re.Pattern[str]]] = {
    "CATEGORY_DIGIT": re.compile(r"\d"),
    "CATEGORY_NOT_DIGIT": re.compile(r"\D"),
    "CATEGORY_SPACE": re.compile(r"\s"),
    "CATEGORY_NOT_SPACE": re.compile(r"\S"),
    "CATEGORY_WORD": re.compile(r"\w"),
    "CATEGORY_NOT_WORD": re.compile(r"\W"),
}


def _na_categoria(expressao: re.Pattern[str]) -> Callable[[int], bool]:
    def teste(ponto: int) -> bool:
        return expressao.fullmatch(chr(ponto)) is not None

    return teste


def _igual(letra: int) -> Callable[[int], bool]:
    def teste(ponto: int) -> bool:
        return ponto == letra

    return teste


def _entre(baixo: int, alto: int) -> Callable[[int], bool]:
    def teste(ponto: int) -> bool:
        return baixo <= ponto <= alto

    return teste


def _algum(testes: tuple[Callable[[int], bool], ...], *, negada: bool, sem_caixa: bool) -> Callable[[int], bool]:
    def dentro(ponto: int) -> bool:
        return any(t(ponto) for t in testes) != negada

    def teste(ponto: int) -> bool:
        return any(dentro(q) for q in _variantes(ponto)) if sem_caixa else dentro(ponto)

    return teste


def _classe(itens: Any, sem_caixa: bool) -> _Letras:
    """As letras de um ``[...]`` (ou de uma letra so). Item desconhecido vira ``tudo`` (o lado que aperta)."""
    negada = False
    testes: list[Callable[[int], bool]] = []
    provas: set[int] = set()
    contadas = True
    for op, valor in itens:
        categoria = _CATEGORIAS.get(getattr(valor, "name", "")) if op is _op.CATEGORY else None
        if op is _op.NEGATE:
            negada = True
        elif op is _op.LITERAL:
            provas.add(valor)
            testes.append(_igual(valor))
        elif op is _op.RANGE:
            baixo, alto = valor
            testes.append(_entre(baixo, alto))
            if alto - baixo < 256:
                provas.update(range(baixo, alto + 1))
            else:
                provas.update({baixo, baixo + 1, (baixo + alto) // 2, alto - 1, alto})
                contadas = False
        elif categoria is not None:
            testes.append(_na_categoria(categoria))
            contadas = False
        else:
            return _TUDO
    if negada:
        provas |= {p + d for p in provas for d in (-1, 1) if 0 <= p + d <= 0x10FFFF}
        contadas = False
    if sem_caixa:
        provas = {q for p in provas for q in _variantes(p)}
        contadas = False
    return _Letras((_algum(tuple(testes), negada=negada, sem_caixa=sem_caixa),), frozenset(provas), contadas)


@dataclass(frozen=True, slots=True)
class CustoDaRegra:
    """A conta de uma regra de formato.

    - ``escolhas``: caminhos que a regra pode tentar sem repeticao longa (``a?a?a?`` tem 8), ate um teto;
    - ``grau``: repeticoes longas que disputam as mesmas letras, uma depois da outra, contando a busca quando a
      regra nao comeca com ``^``. O tempo cresce como o tamanho do texto elevado a ``grau``: ``^a+a+$`` e
      ``\\d+x`` tem grau 2 e levam cerca de meio segundo com 20 mil letras;
    - ``aninhada``: repeticao longa de algo que pode casar de mais de um jeito (``(a+)+``): tempo exponencial.
    """

    escolhas: int
    grau: int
    aninhada: bool


@dataclass(frozen=True, slots=True)
class _Medida:
    escolhas: int  # caminhos sem repeticao longa
    grau: int  # o maior grau de trabalho dentro (quantas repeticoes longas multiplicam o trabalho)
    sai: int  # repeticoes longas que disputam letras com o que vem depois (multiplicam o que vem depois)


_UM: Final = _Medida(1, 0, 0)


class _Aninhada(Exception):
    pass


class _Desconhecido(Exception):
    pass


def _vezes(a: int, b: int) -> int:
    return min(a * b, _TETO)


def _potencias(base: int, minimo: int, maximo: int) -> int:
    """Soma de ``base`` elevado a k, para k de ``minimo`` a ``maximo``, ate o teto (base >= 2)."""
    if minimo > 64:
        return _TETO
    termo, total = base**minimo, 0
    for _ in range(minimo, maximo + 1):
        total += termo
        if total >= _TETO:
            return _TETO
        termo *= base
    return total


def _caixa(sem_caixa: bool, liga: int, desliga: int) -> bool:
    return (sem_caixa or bool(liga & re.IGNORECASE)) and not desliga & re.IGNORECASE


def _repeticoes() -> tuple[Any, ...]:
    return (_op.MAX_REPEAT, _op.MIN_REPEAT, _op.POSSESSIVE_REPEAT)


def _inicio(itens: Any, sem_caixa: bool) -> tuple[_Letras, bool]:
    """As letras com que uma sequencia pode comecar e se ela pode casar vazia."""
    letras = _VAZIO
    for op, valor in itens:
        primeiras, anulavel = _inicio_item(op, valor, sem_caixa)
        letras = _uniao(letras, primeiras)
        if not anulavel:
            return letras, False
    return letras, True


def _inicio_item(op: Any, valor: Any, sem_caixa: bool) -> tuple[_Letras, bool]:
    if op is _op.LITERAL:
        return _classe([(op, valor)], sem_caixa), False
    if op is _op.IN:
        return _classe(valor, sem_caixa), False
    if op is _op.ANY or op is _op.NOT_LITERAL:
        return _TUDO, False
    if op is _op.AT:
        return _VAZIO, True
    if op is _op.FAILURE:
        return _VAZIO, False
    if op is _op.SUBPATTERN:
        return _inicio(valor[3], _caixa(sem_caixa, valor[1], valor[2]))
    if op is _op.ATOMIC_GROUP:
        return _inicio(valor, sem_caixa)
    if op is _op.ASSERT or op is _op.ASSERT_NOT:
        direcao, dentro = valor
        return (_inicio(dentro, sem_caixa)[0] if direcao == 1 else _VAZIO), True
    if op is _op.BRANCH:
        letras, anulavel = _VAZIO, False
        for ramo in valor[1]:
            primeiras, vazio = _inicio(ramo, sem_caixa)
            letras, anulavel = _uniao(letras, primeiras), anulavel or vazio
        return letras, anulavel
    if op is _op.GROUPREF_EXISTS:
        _, sim, nao = valor
        letras_sim, vazio_sim = _inicio(sim, sem_caixa)
        letras_nao, vazio_nao = _inicio(nao, sem_caixa) if nao is not None else (_VAZIO, True)
        return _uniao(letras_sim, letras_nao), vazio_sim or vazio_nao
    if op in _repeticoes():
        minimo, _, corpo = valor
        letras, anulavel = _inicio(corpo, sem_caixa)
        return letras, anulavel or minimo == 0
    if op is _op.GROUPREF:
        return _TUDO, True
    raise _Desconhecido


def _medir(itens: Any, depois: _Letras, sem_caixa: bool) -> _Medida:
    """A conta de uma sequencia. ``depois``: as letras do que pode vir logo depois dela (vazio: a regra acaba)."""
    itens = list(itens)
    seguintes: list[_Letras] = [_VAZIO] * len(itens)
    seguinte = depois
    for i in range(len(itens) - 1, -1, -1):
        seguintes[i] = seguinte
        op, valor = itens[i]
        primeiras, anulavel = _inicio_item(op, valor, sem_caixa)
        seguinte = _uniao(primeiras, seguinte) if anulavel else primeiras
    escolhas, grau, entram = 1, 0, 0
    for (op, valor), apos in zip(itens, seguintes, strict=True):
        m = _medir_item(op, valor, apos, sem_caixa)
        escolhas = _vezes(escolhas, m.escolhas)
        grau = max(grau, entram + m.grau)
        entram = min(entram + m.sai, _TETO)
    return _Medida(escolhas, grau, entram)


def _medir_item(op: Any, valor: Any, depois: _Letras, sem_caixa: bool) -> _Medida:
    if op in (_op.LITERAL, _op.NOT_LITERAL, _op.ANY, _op.IN, _op.AT, _op.FAILURE):
        return _UM
    if op is _op.SUBPATTERN:
        return _medir(valor[3], depois, _caixa(sem_caixa, valor[1], valor[2]))
    if op is _op.ATOMIC_GROUP:
        return _medir(valor, depois, sem_caixa)
    if op is _op.ASSERT or op is _op.ASSERT_NOT:
        dentro = _medir(valor[1], _VAZIO, sem_caixa)
        return _Medida(dentro.escolhas, dentro.grau, 0)  # olhar adiante ou atras: depois de decidido, nao volta
    if op is _op.BRANCH or op is _op.GROUPREF_EXISTS:
        ramos = list(valor[1]) if op is _op.BRANCH else [valor[1], valor[2] if valor[2] is not None else []]
        medidas = [_medir(ramo, depois, sem_caixa) for ramo in ramos]
        inicios = [_inicio(ramo, sem_caixa) for ramo in ramos]
        separados = (
            op is _op.BRANCH
            and len(ramos) <= 16
            and not any(anulavel for _, anulavel in inicios)
            and not any(_cruzam(a[0], b[0]) for i, a in enumerate(inicios) for b in inicios[i + 1 :])
        )
        # Ramos que comecam por letras diferentes: so um segue adiante. Senao, cada ramo e um caminho a mais.
        escolhas = max(m.escolhas for m in medidas) if separados else min(sum(m.escolhas for m in medidas), _TETO)
        return _Medida(escolhas, max(m.grau for m in medidas), max(m.sai for m in medidas))
    if op is _op.GROUPREF:
        return _Medida(1, 1, 0)  # compara um pedaco ja lido: pode ser longo
    if op in _repeticoes():
        return _medir_repeticao(valor, depois, sem_caixa)
    raise _Desconhecido


def _medir_repeticao(valor: Any, depois: _Letras, sem_caixa: bool) -> _Medida:
    minimo, maximo, corpo = valor
    if corpo.getwidth()[1] == 0:
        return _UM  # so o vazio se repete
    sem_teto = maximo == _op.MAXREPEAT
    primeiras, anulavel = _inicio(corpo, sem_caixa)
    # Depois de uma volta pode vir outra volta ou o que segue a repeticao.
    depois_do_corpo = _uniao(primeiras, depois) if sem_teto or maximo > 1 else depois
    corpo_medido = _medir(corpo, depois_do_corpo, sem_caixa)
    # Cada volta casa de um jeito so: sem escolhas dentro, sem disputa com a volta seguinte, nunca vazia.
    determinada = corpo_medido.escolhas == 1 and corpo_medido.sai == 0 and not anulavel
    if sem_teto or maximo - minimo + 1 > _FAIXA_CURTA:
        if not determinada:
            raise _Aninhada  # (a+)+, (a|aa)+, (a?){0,30}, (\w+\s?)*
        disputa = 1 if _cruzam(primeiras, depois) else 0
        return _Medida(1, max(1, corpo_medido.grau), disputa)
    if determinada:
        return _Medida(maximo - minimo + 1, corpo_medido.grau, 0)
    if corpo_medido.sai > 0 and maximo > 1:
        raise _Aninhada  # (.*a){3}, (a{2,}){2}
    base = max(corpo_medido.escolhas, 2)
    return _Medida(_potencias(base, minimo, maximo), corpo_medido.grau, corpo_medido.sai)


def custo_da_regra(padrao: str) -> CustoDaRegra:
    """A conta de uma regra de formato valida, lida pela arvore do proprio ``re`` (como a entrada vai rodar:
    ``re.search``, sem flags). Conservadora: na duvida, conta como cara. ``CustoDaRegra`` explica os numeros.

    O que fica de fora: o tempo de uma regra aceita ainda cresce com o tamanho do texto (grau 2 e quadratico) e
    um pedido pode ter ate 4 MB; o remedio de verdade e um teto de tamanho dos textos na entrada.
    """
    arvore = _sre.parse(padrao)
    flags = int(arvore.state.flags)
    itens = list(arvore)
    try:
        medida = _medir(itens, _VAZIO, bool(flags & re.IGNORECASE))
    except _Aninhada:
        return CustoDaRegra(_TETO, _TETO, True)
    primeiro = itens[0] if itens else None
    ancorada = (
        primeiro is not None
        and primeiro[0] is _op.AT
        and (primeiro[1] is _op.AT_BEGINNING_STRING or (primeiro[1] is _op.AT_BEGINNING and not flags & re.MULTILINE))
    )
    # Sem ^, a busca tenta a regra a partir de cada letra do texto: conta como mais uma repeticao longa.
    return CustoDaRegra(medida.escolhas, medida.grau + (0 if ancorada else 1), False)


def quantificador_aninhado(padrao: str) -> bool:
    """Ha repeticao longa de algo que pode casar de mais de um jeito? ``(a+)+``, ``(a|aa)*``, ``(.*x){20}``.

    ``(\\d{1,3}\\.){3}`` (teto pequeno) e ``^[a-z]+(-[a-z]+)*$`` (cada volta comeca com uma letra que a anterior
    nao aceita) passam.
    """
    return custo_da_regra(padrao).aninhada


def padroes(esquema: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Cada regra de formato do esquema: (onde, regra). ``pattern`` de qualquer nivel e chaves de
    ``patternProperties``."""
    achados: list[tuple[str, str]] = []
    for caminho, no in _esquemas(dict(esquema)):
        onde = "/".join(caminho) or "(raiz)"
        if isinstance(no.get("pattern"), str):
            achados.append((onde, no["pattern"]))
        if isinstance(no.get("patternProperties"), dict):
            achados.extend((f"{onde}/patternProperties", str(chave)) for chave in no["patternProperties"])
    return achados


_FRASES_DO_PADRAO: Final[dict[str, str]] = {
    "aninhada": "repete algo que ja se repete ou que pode casar de mais de um jeito, como (a+)+ ou (a|aa)+; isso "
    "pode travar o MCP-CLARO com um pedido feito de proposito. Peca ao fornecedor uma regra sem repeticao dentro "
    "de repeticao",
    "escolhas": f"tem combinacoes demais (mais de {ESCOLHAS_MAXIMAS}), como (a?){{20}} ou a?a?a?a?...; isso pode "
    "travar o MCP-CLARO com um pedido feito de proposito. Peca ao fornecedor uma regra mais simples",
    "seguidas": "tem repeticoes longas que disputam as mesmas letras, uma depois da outra, como a+a+a+ ou .*x.*y; "
    "isso pode travar o MCP-CLARO com um texto longo. Peca ao fornecedor uma regra mais simples, que comece com ^",
}


def conferir_padroes(esquema: Mapping[str, Any]) -> None:
    """Revisao de terceiro das regras de formato. Levanta ``PadraoRecusado`` com uma frase para o ADMIN."""
    for onde, regra in padroes(esquema):
        lugar = marcar_escondidos(onde)[:200]
        if len(regra) > PADRAO_MAXIMO:
            raise PadraoRecusado(
                f"a regra de formato (pattern) em {lugar} tem {len(regra)} letras, mais que {PADRAO_MAXIMO}; "
                "peca ao fornecedor uma regra mais curta",
                onde=lugar,
                motivo="longa",
            )
        try:
            re.compile(regra)
            custo = custo_da_regra(regra)
        except re.error, RecursionError, OverflowError, ValueError:
            raise PadraoRecusado(
                f"a regra de formato (pattern) em {lugar} nao e uma expressao valida", onde=lugar, motivo="invalida"
            ) from None
        except _Desconhecido:
            raise PadraoRecusado(
                f"a regra de formato (pattern) em {lugar} usa um recurso que a revisao nao sabe medir; peca ao "
                "fornecedor uma regra mais simples",
                onde=lugar,
                motivo="invalida",
            ) from None
        motivo: MotivoPadrao | None = None
        if custo.aninhada:
            motivo = "aninhada"
        elif custo.escolhas > ESCOLHAS_MAXIMAS:
            motivo = "escolhas"
        elif custo.grau > GRAU_MAXIMO:
            motivo = "seguidas"
        if motivo is not None:
            raise PadraoRecusado(
                f"a regra de formato (pattern) em {lugar} {_FRASES_DO_PADRAO[motivo]}", onde=lugar, motivo=motivo
            )
