"""Esquema limpo de um servidor de terceiros: so o que valida fica; o texto do fornecedor sai.

O aplicativo (e o modelo de IA por tras dele) le o esquema de entrada de cada ferramenta da vitrine. Num servidor
de terceiros, nada do texto escrito pelo fornecedor deve chegar la: descricao, titulo, exemplos, comentario,
valor padrao e qualquer palavra que o JSON Schema nao usa para validar. ``limpar_textos`` fica so com as
palavras que mudam o veredito da validacao (JSON Schema 2020-12) e com as de estrutura (``$ref``, ``$defs``...);
``format`` fica so com os nomes do proprio JSON Schema. O que continua indo ao modelo, porque e preciso para
chamar: nomes de campo, valores de ``enum``/``const`` e ``pattern``.

``com_descricoes`` poe as descricoes de campo escritas pelo ADMIN, so no primeiro nivel.

A entrada confere cada argumento com o esquema revisado, dentro do proprio laco (``entrada.argumentos``). Num
terceiro (critica B3, desenho simples do coordenador):

- ``para_conferir``: a copia que a entrada usa nunca tem ``pattern`` nem ``uniqueItems``. Quem confere isso e o
  proprio servidor do terceiro; a vitrine continua mostrando ``pattern`` ao aplicativo. Nenhuma regra de formato
  do fornecedor roda no MCP-CLARO, e nenhuma lista e comparada item a item. Onde tirar a regra faria a entrada
  recusar um pedido valido (dentro de ``not``, ``oneOf``, ``if`` e ``contains`` com ``maxContains``), a copia
  afrouxa em vez de apertar, sem nunca ficar maior nem mais funda que o esquema revisado.
- ``conferir_terceiro`` (revisao): recusa ``patternProperties`` (regra de formato nos nomes de campo), ``$id``,
  ``$anchor``, ``$dynamicAnchor``, ``$dynamicRef``, ``unevaluatedProperties`` e ``unevaluatedItems`` em qualquer
  lugar, ``$schema`` fora da raiz, toda referencia que nao seja ``#/$defs/<nome>`` ou ``#/definitions/<nome>`` para
  uma definicao que existe, esquema em ciclo e esquema com mais de ``CUSTO_MAXIMO`` partes ou
  ``PROFUNDIDADE_MAXIMA`` niveis, contando todas as definicoes e seguindo as referencias.

O que sobra: sem essas palavras, cada parte do esquema (com as referencias seguidas) confere cada parte do pedido no
maximo uma vez, entao a conferencia custa ate o tamanho do esquema, mais os valores de ``enum`` e ``const``, vezes o
tamanho do pedido. Com o esquema e o pedido nos tetos, isso ainda pode levar segundos. Nada disso e garantia.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from typing import Any, Final, Literal
from urllib.parse import unquote

from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from mcp_claro.entrada.argumentos import EsquemaInvalido
from mcp_claro.regras.escondidos import marcar_escondidos

DESCRICAO_DE_CAMPO_MAXIMA: Final = 300
"""Letras de uma descricao de campo escrita pelo ADMIN."""

CUSTO_MAXIMO: Final = 2000
"""Partes de um esquema de entrada de terceiro: todas as do documento (definicoes nao citadas tambem) e, a parte,
as que a conferencia visita com as referencias seguidas (cada referencia conta a definicao de novo)."""

PROFUNDIDADE_MAXIMA: Final = 64
"""Niveis de um esquema de entrada de terceiro, no documento e com as referencias seguidas. No teto, a conferencia
de um esquema novo na entrada (check_schema e o pedido) gasta uns 800 dos 1000 quadros da pilha de Python."""

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

SO_O_TERCEIRO_CONFERE: Final = frozenset({"pattern", "uniqueItems"})
"""Palavras que a entrada nunca confere num terceiro (``para_conferir``): o servidor do terceiro confere."""

PALAVRAS_RECUSADAS: Final = (
    "patternProperties",
    "$id",
    "$anchor",
    "$dynamicAnchor",
    "$dynamicRef",
    "unevaluatedProperties",
    "unevaluatedItems",
)
"""Palavras que a revisao de terceiro recusa em qualquer lugar do esquema (``conferir_terceiro``).
``unevaluated*``: a biblioteca reconfere cada parte abaixo dela para saber o que ja foi visto, e aninhada o custo
dobra a cada nivel (15 niveis, 12 s com um pedido minusculo)."""

SO_NA_RAIZ: Final = ("$schema",)
"""Palavras que a revisao de terceiro so aceita na raiz. ``$schema`` numa parte troca a versao do JSON Schema dela
na entrada (a biblioteca le o ``$schema`` de cada parte); na raiz, a entrada sempre confere como 2020-12."""


class CampoDesconhecido(ValueError):
    """Descricao para um campo que o esquema de entrada nao tem no primeiro nivel."""

    def __init__(self, campos: list[str]) -> None:
        self.campos = campos
        super().__init__("campos fora do esquema de entrada: " + ", ".join(marcar_escondidos(c) for c in campos))


MotivoEsquema = Literal["palavra", "referencia", "ciclo", "grande", "fundo"]


class EsquemaRecusado(EsquemaInvalido):
    """Esquema valido que a revisao de terceiro nao aceita. A mensagem e para o ADMIN; ``onde`` (com os
    escondidos ja marcados), ``motivo`` (um de ``MotivoEsquema``) e ``palavra`` (a palavra recusada, com o motivo
    ``palavra``) servem para a tela montar a propria frase."""

    def __init__(self, mensagem: str, *, onde: str, motivo: MotivoEsquema, palavra: str | None = None) -> None:
        self.onde = onde
        self.motivo: MotivoEsquema = motivo
        self.palavra = palavra
        super().__init__(mensagem)


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
    """Cada objeto de esquema, com o caminho ate ele (definicoes tambem, citadas ou nao)."""
    if not isinstance(no, dict):
        return
    yield caminho, no
    for sub_caminho, sub in _filhos(no):
        yield from _esquemas(sub, (*caminho, *sub_caminho))


def _lugar(caminho: tuple[str, ...]) -> str:
    return marcar_escondidos("/".join(caminho) or "(raiz)")[:200]


def _definicao(raiz: Mapping[str, Any], referencia: Any) -> Any | None:
    """A definicao que ``referencia`` acha, se ela tem a forma ``#/$defs/<nome>`` ou ``#/definitions/<nome>``;
    senao None. O ponteiro e lido como a entrada le (``%20`` e depois ``~1`` e ``~0``): ``#/$defs/a%2Fb`` sao tres
    pedacos, nao um nome com barra."""
    if not isinstance(referencia, str) or not referencia.startswith("#/"):
        return None
    pedacos = [p.replace("~1", "/").replace("~0", "~") for p in unquote(referencia[2:]).split("/")]
    if len(pedacos) != 2 or pedacos[0] not in _DEFINICOES:
        return None
    definicoes = raiz.get(pedacos[0])
    if not isinstance(definicoes, dict) or pedacos[1] not in definicoes:
        return None
    alvo = definicoes[pedacos[1]]
    return alvo if isinstance(alvo, dict | bool) else None


def _acha_como_a_entrada(raiz: Mapping[str, Any], referencia: str, alvo: Any) -> bool:
    """A entrada (``referencing``, sem nada de fora) acha a mesma parte? Prende a leitura do ponteiro a da
    biblioteca: se as duas se separarem numa versao nova, a revisao recusa em vez de aceitar errado."""
    try:
        achado = Registry().resolver_with_root(DRAFT202012.create_resource(dict(raiz))).lookup(referencia)
    except Unresolvable, LookupError, ValueError, TypeError:
        return False
    return achado.contents is alvo


_FRASES_DAS_PALAVRAS: Final[dict[str, str]] = {
    "patternProperties": "regra de formato nos nomes de campo (patternProperties)",
    "$id": "identificador de parte ($id)",
    "$anchor": "ancora ($anchor)",
    "$dynamicAnchor": "ancora dinamica ($dynamicAnchor)",
    "$dynamicRef": "referencia dinamica ($dynamicRef)",
    "unevaluatedProperties": "regra dos campos nao conferidos (unevaluatedProperties)",
    "unevaluatedItems": "regra dos itens nao conferidos (unevaluatedItems)",
    "$schema": "troca de versao do JSON Schema ($schema) fora da raiz",
}


def _recusa(caminho: tuple[str, ...], motivo: MotivoEsquema, palavra: str | None = None) -> EsquemaRecusado:
    onde = _lugar(caminho)
    frases: dict[str, str] = {
        "palavra": f"o esquema usa {_FRASES_DAS_PALAVRAS.get(palavra or '', palavra)} em {onde}; a revisao de "
        "terceiro nao aceita essa palavra",
        "referencia": f"a referencia ($ref) em {onde} nao aponta para uma definicao deste esquema; a revisao de "
        "terceiro so aceita referencia da forma #/$defs/<nome> ou #/definitions/<nome>, para uma definicao que existe",
        "ciclo": f"a referencia em {onde} volta a uma parte que a contem (esquema em ciclo); a revisao de "
        "terceiro nao aceita esquema em ciclo",
        "grande": f"o esquema tem mais de {CUSTO_MAXIMO} partes, contando todas as definicoes e seguindo as "
        f"referencias ($ref) (passou em {onde}); isso pode travar o MCP-CLARO a cada chamada",
        "fundo": f"o esquema tem mais de {PROFUNDIDADE_MAXIMA} niveis, contando as definicoes e seguindo as "
        f"referencias (passou em {onde})",
    }
    return EsquemaRecusado(
        frases[motivo] + ". Peca ao fornecedor um esquema mais simples", onde=onde, motivo=motivo, palavra=palavra
    )


def conferir_documento(esquema: Mapping[str, Any]) -> None:
    """Todas as partes do documento, citadas ou nao, sem seguir as referencias: ate ``CUSTO_MAXIMO`` partes
    (motivo ``grande``) e ``PROFUNDIDADE_MAXIMA`` niveis (motivo ``fundo``). A copia da entrada
    (``para_conferir``) e a conferencia do esquema (check_schema) andam por todas, inclusive pelas definicoes que
    nenhuma referencia chama. Sem recursao: em ``conferir_terceiro``, roda antes das outras, que descem pelo
    esquema com recursao."""
    partes = 1
    pilha: list[tuple[Any, tuple[str, ...], int]] = [(dict(esquema), (), 0)]
    while pilha:
        no, caminho, nivel = pilha.pop()
        if not isinstance(no, dict):
            continue
        if nivel >= PROFUNDIDADE_MAXIMA:
            raise _recusa(caminho, "fundo")
        filhos = list(_filhos(no))
        if partes + len(filhos) > CUSTO_MAXIMO:
            raise _recusa((*caminho, *filhos[CUSTO_MAXIMO - partes][0]), "grande")  # a parte que passou do teto
        partes += len(filhos)
        pilha.extend((sub, (*caminho, *sub_caminho), nivel + 1) for sub_caminho, sub in filhos)


def conferir_palavras(esquema: Mapping[str, Any]) -> None:
    """Revisao de terceiro das palavras e referencias, em todo lugar do esquema (definicoes nao citadas tambem).

    Recusa (``EsquemaRecusado``, frase para o ADMIN) com o motivo ``palavra`` (``PALAVRAS_RECUSADAS`` em qualquer
    lugar, ``SO_NA_RAIZ`` fora da raiz; o atributo ``palavra`` diz qual) ou ``referencia`` (``$ref`` que nao e
    ``#/$defs/<nome>`` nem ``#/definitions/<nome>``, ou cuja definicao nao existe ou nao e um esquema). Com isso,
    nenhuma referencia cai dentro de um ``enum``, de um ``const`` ou de texto do fornecedor, nenhuma ancora repetida
    muda o alvo, e toda parte vale como JSON Schema 2020-12 na entrada.
    """
    raiz = dict(esquema)
    for caminho, no in _esquemas(raiz):
        for palavra in (*PALAVRAS_RECUSADAS, *(SO_NA_RAIZ if caminho else ())):
            if palavra in no:
                raise _recusa(caminho, "palavra", palavra)
        if "$ref" in no:
            referencia = no["$ref"]
            alvo = _definicao(raiz, referencia)
            if alvo is None or not _acha_como_a_entrada(raiz, referencia, alvo):
                raise _recusa(caminho, "referencia")


class _Conta:
    """O custo do esquema com as referencias seguidas. A memoria guarda, de cada parte ja contada, as partes e a
    altura (niveis dela para baixo): achada de novo mais funda, a parte nao e percorrida outra vez, mas os niveis
    dela contam a partir de onde ela foi achada."""

    def __init__(self, raiz: dict[str, Any]) -> None:
        self.raiz = raiz
        self.memoria: dict[int, tuple[int, int]] = {}
        self.pilha: set[int] = set()

    def custo(self, no: Any, caminho: tuple[str, ...]) -> tuple[int, int]:
        """(partes, altura) de ``no``; a parte esta no nivel ``len(self.pilha)``."""
        if not isinstance(no, dict):
            return 1, 0  # esquema booleano: conta como parte, nao como nivel
        chave = id(no)
        if chave in self.memoria:
            total, altura = self.memoria[chave]
            if len(self.pilha) + altura > PROFUNDIDADE_MAXIMA:
                raise _recusa(caminho, "fundo")
            return total, altura
        if chave in self.pilha:
            raise _recusa(caminho, "ciclo")
        if len(self.pilha) >= PROFUNDIDADE_MAXIMA:
            raise _recusa(caminho, "fundo")
        self.pilha.add(chave)
        total, altura = 1, 1
        partes = [((*caminho, *sub_caminho), sub) for sub_caminho, sub in _filhos(no, com_definicoes=False)]
        if "$ref" in no:
            alvo = _definicao(self.raiz, no["$ref"])
            if alvo is None:
                raise _recusa(caminho, "referencia")
            partes.append(((*caminho, "$ref"), alvo))
        for sub_caminho, sub in partes:
            custo_sub, altura_sub = self.custo(sub, sub_caminho)
            total += custo_sub
            altura = max(altura, altura_sub + 1)
            if total > CUSTO_MAXIMO:
                raise _recusa(sub_caminho, "grande")
        self.pilha.discard(chave)
        self.memoria[chave] = (total, altura)
        return total, altura


def custo_do_esquema(esquema: Mapping[str, Any]) -> int:
    """Quantas partes a conferencia de um argumento pode visitar: cada objeto de esquema conta uma vez, e cada
    ``$ref`` conta de novo a definicao que ele acha (aqui, ``$defs`` so contam quando citados). Levanta
    ``EsquemaRecusado`` com o motivo ``grande`` (mais de ``CUSTO_MAXIMO`` partes), ``fundo`` (mais de
    ``PROFUNDIDADE_MAXIMA`` niveis, com as referencias seguidas), ``ciclo`` (referencia que volta a uma parte que a
    contem) ou ``referencia``. Um ``$ref`` que chama duas vezes outro, que chama duas vezes outro... dobra a cada
    nivel: com 20 niveis, cada chamada custaria um milhao de partes."""
    raiz = dict(esquema)
    return _Conta(raiz).custo(raiz, ())[0]


def conferir_terceiro(esquema: Mapping[str, Any]) -> None:
    """Revisao de terceiro do esquema de entrada ja limpo: ``conferir_documento``, ``conferir_palavras`` e
    ``custo_do_esquema``. Levanta ``EsquemaRecusado`` (um ``EsquemaInvalido``) com a frase para o ADMIN. A copia da
    entrada (``para_conferir``) nunca e maior nem mais funda que o esquema, entao os tetos valem para ela."""
    conferir_documento(esquema)
    conferir_palavras(esquema)
    custo_do_esquema(esquema)


# --- A copia que a entrada usa para conferir os argumentos de um terceiro ------------------------------------------


class _Afrouxar:
    """Tira ``SO_O_TERCEIRO_CONFERE`` de todo lugar de esquema, sem nunca recusar o que o esquema aceita.

    Tirar uma regra afrouxa o lugar onde ela estava. Isso so aperta o todo em quatro casos, que viram outra
    coisa quando tem regra tirada dentro (``tem_regra``, seguindo as referencias):

    - ``not``: sai (a entrada nao confere esse ``not``);
    - ``oneOf``: vira ``anyOf`` no mesmo lugar (um ramo afrouxado pode passar junto com outro); se o lugar ja tem
      ``anyOf``, o ``oneOf`` sai;
    - ``if``/``then``/``else``: saem os tres;
    - ``maxContains``: sai (o ``contains`` afrouxado pode achar mais itens).

    Cada parte fica no lugar dela ou sai: a copia nunca e maior nem mais funda que o esquema, e os tetos da revisao
    valem para ela. ``unevaluated*`` e recusado na revisao, entao tirar uma parte nunca muda o que outra ve. Os
    dados (``enum``, ``const``...) sao os mesmos objetos do original: ninguem os muda.
    """

    def __init__(self, raiz: dict[str, Any]) -> None:
        self.raiz = raiz
        self.memoria: dict[int, bool] = {}
        self.andamento: set[int] = set()

    def tem_regra(self, no: Any) -> bool:
        if not isinstance(no, dict):
            return False
        chave = id(no)
        if chave in self.memoria:
            return self.memoria[chave]
        if chave in self.andamento:
            return True  # ciclo (a revisao recusa): afrouxar a mais nunca recusa um pedido valido
        self.andamento.add(chave)
        achou = any(palavra in no for palavra in SO_O_TERCEIRO_CONFERE) or any(
            self.tem_regra(sub) for _, sub in _filhos(no, com_definicoes=False)
        )
        if not achou and "$ref" in no:
            alvo = _definicao(self.raiz, no["$ref"])
            achou = alvo is None or self.tem_regra(alvo)  # referencia de outra forma: na duvida, afrouxa
        self.andamento.discard(chave)
        self.memoria[chave] = achou
        return achou

    def afrouxar(self, no: Any) -> Any:
        if not isinstance(no, dict):
            return no  # esquema booleano ou valor que o validador recusa
        saida: dict[str, Any] = {}
        condicao_com_regra = "if" in no and self.tem_regra(no["if"])
        for chave, valor in no.items():
            if (
                chave in SO_O_TERCEIRO_CONFERE
                or (chave == "not" and self.tem_regra(valor))
                or (chave in ("if", "then", "else") and condicao_com_regra)
                or (chave == "maxContains" and self.tem_regra(no.get("contains")))
            ):
                continue
            if chave == "oneOf" and isinstance(valor, list) and any(self.tem_regra(s) for s in valor):
                if "anyOf" not in no:
                    saida["anyOf"] = [self.afrouxar(sub) for sub in valor]
            elif chave in _MAPA_DE_ESQUEMAS and isinstance(valor, dict):
                saida[chave] = {nome: self.afrouxar(sub) for nome, sub in valor.items()}
            elif chave in _LISTA_DE_ESQUEMAS and isinstance(valor, list):
                saida[chave] = [self.afrouxar(sub) for sub in valor]
            elif chave in _UM_ESQUEMA:
                saida[chave] = self.afrouxar(valor)
            else:
                saida[chave] = valor
        return saida


def para_conferir(esquema: Mapping[str, Any]) -> dict[str, Any]:
    """A copia do esquema revisado de um terceiro que a entrada usa para conferir os argumentos: sem
    ``pattern`` e sem ``uniqueItems`` em nenhum lugar de esquema (nomes de campo e dados ficam como estao).

    Todo pedido que o esquema aceita, a copia aceita (``_Afrouxar`` explica os casos); o que so a regra tirada
    recusaria, o servidor do terceiro recusa. Sem essas palavras, a copia e igual ao esquema. Espera um esquema
    que passou em ``conferir_terceiro``.
    """
    raiz = dict(esquema)
    return dict(_Afrouxar(raiz).afrouxar(raiz))
