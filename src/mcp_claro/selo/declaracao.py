"""Declaracao de teste do selo (desenho 2.5; requisito SELO-0.2; criticas A4, A3 e M6).

A equipe do sistema escreve um JSON; o ADMIN o cola na tela e o salva com login refeito, marcando a caixa "Confirmo
que o alvo declarado e de teste". A declaracao diz ONDE a bateria pode escrever (o alvo de teste) e COM QUE: para
cada ferramenta de acao, os argumentos de teste e uma leitura que mostra o rastro. A bateria nunca inventa provas
alem das declaradas.

Tres conferencias, nesta ordem:

1. ``ler_texto`` (o JSON colado) ou ``validar`` (um objeto ja lido): ate ``contrato.TETO_DECLARACAO_BYTES``, JSON
   sem chave repetida nem NaN, sem o caractere nulo (que o jsonb do banco nao guarda), ate ``FUNDO_MAXIMO`` niveis, e a
   forma do modelo ``Declaracao`` (nada a mais, em nivel nenhum). O alvo de teste tem o nome escrito de um jeito so:
   ASCII, e um IP so por inteiro (``10.0.0.5``, nunca ``10.5`` nem ``0x0a000005``; um IPv6 sem um IPv4 dentro, nunca
   ``[::ffff:10.0.0.5]``), para que o texto guardado seja o nome que o cliente conecta;
2. ``conferir``, contra o cadastro de agora (``cadastro_do_sistema``): terceiro recusado ate o G7; https fora de
   desenvolvimento e teste; o alvo de teste nunca e o endereco de producao de NENHUM sistema do cadastro, de qualquer
   situacao, nem na forma normalizada (``contrato.mesmo_endereco``) nem na forma de conexao (``forma_de_conexao``: o
   nome que o cliente conecta, a porta efetiva e o caminho; critica A4; o banco confere de novo no INSERT, no
   pedido, na passagem para ``rodando`` e no teste); toda ferramenta de acao do sistema tem entrada, e toda entrada e
   uma ferramenta de acao; toda leitura (rastro, versao do conflito, rastro do idempotente) e uma revisada ativa de
   classe ``le`` (A4.2); a ferramenta de teste nao tem o nome de nenhuma revisada do sistema, de nenhuma classe nem
   situacao (A4.3). A tela usa ``conferir`` ao salvar e de novo ao pedir o teste;
3. o banco (``declaracoes_selo_nasce``): versao seguinte, ADMIN ativo, colunas iguais ao JSON e o alvo fora da
   producao.

Campos por ferramenta. Obrigatorios: ``argumentos`` (C-2.1 e quase todos), ``argumentos_outros`` (C-2.2c, diferentes
de ``argumentos``), ``argumentos_recusados`` (C-2.4b e C-3.1b) e ``rastro``. No modo ``ferramentas_de_teste``,
``ferramenta_de_teste`` (e nunca no modo ``ambiente_de_teste``). Opcionais, cada um com o caso que libera; sem ele, o
caso da ``nao_se_aplica`` pela declaracao (razao ``sem_condicao_declarada``):

- ``efeito_adiado`` (C-3.6b): argumentos cujo efeito so acontece ``segundos`` depois;
- ``escrita_lenta`` (C-2.6b, critica A3): argumentos de uma escrita que leva ``segundos`` para terminar;
- ``conflito`` (C-3.4, procedimento "stale write"): a leitura do objeto (com os ponteiros da versao e dos dois
  campos), o nome do argumento que leva a versao lida e os argumentos dos dois escritores, um campo cada;
- ``varios_itens`` (C-3.3): argumentos de uma escrita de ``itens`` itens com um item ruim;
- ``pede_dados`` (C-2.3): argumentos com que a ferramenta responde ``input_required``;
- ``idempotente`` (C-2.7 e critica M6): a declaracao diz que a repeticao com numero novo nao muda nada (C-2.2b fica
  ``nao_se_aplica``, ``sem_segundo_efeito``), com um rastro proprio do conteudo, se o da ferramenta contar
  aplicacoes.

Um ponteiro (``conta``, ``versao``...) e um ponteiro JSON (RFC 6901) no ``structuredContent`` da leitura, que comeca
com ``/``. Num ``rastro``, ele aponta para um numero ou uma lista (o tamanho conta).

Servidor de terceiros: recusado ate o G7 (desenho 8), com a frase ``TERCEIRO_AINDA_NAO``.

Os textos das recusas (``DeclaracaoRecusada.mensagem``) vao a tela do ADMIN: por isso tem acento.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

import httpx2
from mcp.shared.tool_name_validation import TOOL_NAME_REGEX
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.config.settings import Ambiente
from mcp_claro.selo import contrato

TERCEIRO_AINDA_NAO: Final = "O selo de servidores de terceiros ainda não está disponível."
"""A recusa da declaracao e do pedido de teste de um servidor de terceiros ate o G7 (desenho 8)."""

SEGUNDOS_MAXIMOS: Final = 600
"""Teto de ``segundos`` em ``efeito_adiado`` e ``escrita_lenta``: a bateria espera isso numa rodada so."""

TETO_ENDERECO: Final = 2048
TETO_PONTEIRO: Final = 1024
TETO_RESPONSAVEL: Final = 200
GUARDA_MAXIMA_DIAS: Final = 3650
_GUARDA_MINIMA_DIAS: Final = contrato.GUARDA_MINIMA_DIAS  # o campo "contrato" do modelo esconde o modulo na classe
_MAX_FERRAMENTAS: Final = contrato.MAX_FERRAMENTAS_DECLARACAO
MAX_ITENS: Final = 1000
FUNDO_MAXIMO: Final = 64
"""Niveis de objeto e lista, um dentro do outro, na declaracao inteira (a raiz e o nivel 1). Os campos do contrato
usam ate 6; o resto fica para os argumentos. Sem teto, uma declaracao de 64 KiB passava pela tela com centenas de
niveis e a gravacao falhava fora de ``DeclaracaoRecusada`` (o pydantic nao serializa mais de 255)."""

AMBIENTES_SEM_HTTPS: Final = frozenset({Ambiente.DESENVOLVIMENTO, Ambiente.TESTE})
"""Fora destes, o alvo de teste usa https (desenho 2.5)."""

_PONTEIRO: Final = re.compile(r"(?:/(?:[^~/]|~[01])*)+", re.DOTALL)
_NOME_ASCII: Final = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?")
"""O nome do alvo de teste: so letras ASCII (ja em minusculas, como ``urlsplit`` o devolve), numeros, ``_``, ``-`` e
pontos comuns. Um nome com acento vai na forma ``xn--``, que e a que o cliente procura no DNS."""
_IPV4_ESCRITO: Final = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+)){0,3}\.?")
"""Um nome que o resolvedor do sistema le como IPv4, tambem na forma curta (``127.1``), decimal (``2130706433``),
hexadecimal (``0x7f.0.0.1``) ou octal (``0177.0.0.1``): ``socket.inet_aton`` aceita e chega ao mesmo IP. A mesma
regra de ``telas.admin._IPV4_CURTO`` (Fase 3)."""
_PORTA_PADRAO: Final = {"http": 80, "https": 443}
_LOCAL: Final = "localhost"
_CATEGORIAS_DE_CONTROLE: Final = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Caracteres de controle, de formato (inclusive os de direcao do texto), metades de par e quebras de linha."""


def _tem_controle(texto: str) -> bool:
    return any(unicodedata.category(ch) in _CATEGORIAS_DE_CONTROLE for ch in texto)


class DeclaracaoRecusada(ValueError):
    """A declaracao nao vale. ``motivo``: codigo curto (veja ``MOTIVOS``); ``mensagem``: o texto para o ADMIN, com
    acento; ``ferramenta``: a ferramenta de acao em causa, quando ha uma (a tela a mostra)."""

    def __init__(self, motivo: str, mensagem: str, *, ferramenta: str | None = None) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        self.ferramenta = ferramenta
        super().__init__(mensagem)


MOTIVOS: Final = (
    "json_invalido",
    "grande_demais",
    "forma",
    "selo_terceiro_ainda_nao",
    "sem_chave_por_pessoa",
    "sem_acoes",
    "alvo_sem_https",
    "alvo_na_producao",
    "selo_declaracao_incompleta",
    "ferramenta_que_nao_e_acao",
    "leitura_que_nao_le",
    "ferramenta_de_teste_real",
)
"""Os codigos de ``DeclaracaoRecusada.motivo``. ``selo_terceiro_ainda_nao`` e ``selo_declaracao_incompleta`` sao os
nomes dos avisos da tela (desenho 7.1); ``forma`` cobre o que o modelo recusa (``mensagem`` diz o que e onde)."""


# --- Tipos dos campos ------------------------------------------------------------------------------------------


def _erro(tipo: str, mensagem: str) -> PydanticCustomError:
    return PydanticCustomError(tipo, mensagem)


def _nome_de_ferramenta(valor: str) -> str:
    if not TOOL_NAME_REGEX.fullmatch(valor):
        raise _erro("nome_de_ferramenta", "O nome de ferramenta tem de 1 a 128 letras sem acento, números, . _ ou -.")
    if valor in contrato.NOMES_RESERVADOS:
        raise _erro("nome_reservado", f"{valor} é uma ferramenta do contrato do selo e não entra na declaração.")
    return valor


def _ponteiro(valor: str) -> str:
    if len(valor) > TETO_PONTEIRO or not _PONTEIRO.fullmatch(valor):
        raise _erro(
            "ponteiro",
            "O ponteiro precisa ser um ponteiro JSON (RFC 6901) que comece com /, como /itens ou /total.",
        )
    return valor


def _compacto(valor: Any) -> bytes:
    return json.dumps(valor, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _argumentos(valor: dict[str, Any]) -> dict[str, Any]:
    try:
        tamanho = len(_compacto(valor))
    except ValueError, TypeError, RecursionError:
        raise _erro("argumentos", "Os argumentos precisam ser um objeto JSON.") from None
    if tamanho > contrato.TETO_ARGUMENTOS_BYTES:
        raise _erro("argumentos_grandes", f"Os argumentos passam de {contrato.TETO_ARGUMENTOS_BYTES // 1024} KiB.")
    return valor


def _responsavel(valor: str) -> str:
    if _tem_controle(valor) or len(valor) > TETO_RESPONSAVEL or len(valor.strip()) < 3:
        raise _erro(
            "responsavel",
            f"O responsável tem de 3 a {TETO_RESPONSAVEL} letras, numa linha, sem caracteres de controle.",
        )
    return valor


_ALVO_NOME: Final = (
    "O nome do alvo de teste vai escrito de um jeito só: letras sem acento, números, ponto e hífen (um nome com acento"
    " vai na forma xn--, como o DNS o guarda), ou o IP por inteiro, como 10.0.0.5 ou [2001:db8::1]."
)


def _ip_comum(nome: str) -> bool:
    """O nome e um IP escrito na forma comum (IPv4 com os quatro numeros; IPv6 na forma curta, sem zona)."""
    try:
        ip = ipaddress.ip_address(nome)
    except ValueError:
        return False
    return str(ip) == nome and not (isinstance(ip, ipaddress.IPv6Address) and ip.scope_id is not None)


def _ipv6_com_ipv4_dentro(nome: str) -> bool:
    """O IPv6 leva um IPv4 dentro e fica de fora do alvo, aqui e no banco (``selo_alvo_bem_escrito``):

    - o mapeado ``::ffff:a.b.c.d``: o cliente, num socket IPv6 comum, conecta no proprio IPv4, entao
      ``[::ffff:10.0.0.5]`` e a producao ``10.0.0.5`` escrita de outro jeito (e ``[::ffff:127.0.0.1]``, a maquina
      local). A tela pega a producao assim escrita pela forma de conexao, mas o banco compara o texto: sem esta regra,
      o INSERT direto passava;
    - o da forma antiga ``::a.b.c.d`` (os 96 primeiros bits zerados e o IPv4 nos 32 do fim, fora ``::`` e ``::N``),
      que o Python escreve em hexadecimal (``::a00:5``) e o banco com pontos (``::10.0.0.5``): sem forma comum aos
      dois, fica de fora nos dois."""
    try:
        ip = ipaddress.IPv6Address(nome)
    except ValueError:
        return False
    return ip.ipv4_mapped is not None or (ip.packed[:12] == bytes(12) and ip.packed[12:14] != bytes(2))


def _nome_do_alvo(nome: str, ipv6: bool) -> None:
    """O nome do alvo escrito de um jeito so (critica A4): o texto guardado e o nome que o cliente conecta.

    ``nome`` e o de ``urlsplit`` (em minusculas). Fora de ASCII, o leitor do cliente (``httpx2``, com IDNA) troca os
    pontos de outros alfabetos (U+3002, U+FF0E, U+FF61) por pontos comuns e conecta em outro nome que o do texto; um
    IPv4 escrito de outro jeito (``127.1``, ``2130706433``) conecta no IP por inteiro. Os dois passariam por diferentes
    da producao na comparacao de texto do banco. Com so ``_NOME_ASCII`` (ou um IPv6 na forma comum), o nome que o
    ``httpx2`` conecta e o proprio texto (``forma_de_conexao`` le pelo ``httpx2``, e o teste confere os dois).

    E a mesma regra de ``selo_alvo_bem_escrito`` no banco (a segunda barreira, no INSERT da declaracao, no pedido, em
    ``rodando`` e no teste; o teste confere as duas numa lista de formas), com o nome so em ASCII conferido antes por
    ``_endereco``: o IPv6 que leva um IPv4 dentro (``::ffff:a.b.c.d`` e ``::a.b.c.d``) fica de fora
    (``_ipv6_com_ipv4_dentro``)."""
    if ipv6:
        if not _ip_comum(nome) or _ipv6_com_ipv4_dentro(nome):
            raise _erro("alvo_nome", _ALVO_NOME)
    elif not _NOME_ASCII.fullmatch(nome) or (_IPV4_ESCRITO.fullmatch(nome) and not _ip_comum(nome)):
        raise _erro("alvo_nome", _ALVO_NOME)


def _endereco(valor: str) -> str:
    """A forma do endereco (``^https?://`` como o banco, nome, porta valida, sem usuario, consulta nem fragmento) e
    o nome escrito de um jeito so (``_nome_do_alvo``). https e a comparacao com a producao ficam com ``conferir``."""
    mensagem = (
        "O alvo de teste é um endereço http:// ou https:// com o nome, a porta e o caminho, sem usuário, sem consulta"
        " (?) e sem fragmento (#)."
    )
    if len(valor) > TETO_ENDERECO or _tem_controle(valor) or any(ch.isspace() for ch in valor):
        raise _erro("alvo_invalido", mensagem)
    if not (valor.startswith("http://") or valor.startswith("https://")):
        raise _erro("alvo_invalido", mensagem)
    try:
        partes = urlsplit(valor)
        partes.port  # noqa: B018 - porta invalida levanta ValueError
    except ValueError:
        raise _erro("alvo_invalido", mensagem) from None
    if not partes.hostname or "@" in partes.netloc or partes.query or partes.fragment or "?" in valor or "#" in valor:
        raise _erro("alvo_invalido", mensagem)
    if not partes.netloc.isascii():
        # Antes das minusculas de urlsplit: o sinal de kelvin (U+212A) vira "k", e o nome passaria por ASCII.
        raise _erro("alvo_nome", _ALVO_NOME)
    _nome_do_alvo(partes.hostname, "[" in partes.netloc)
    return valor


NomeFerramenta = Annotated[str, AfterValidator(_nome_de_ferramenta)]
Ponteiro = Annotated[str, AfterValidator(_ponteiro)]
Argumentos = Annotated[dict[str, Any], AfterValidator(_argumentos)]
Responsavel = Annotated[str, AfterValidator(_responsavel)]
Endereco = Annotated[str, AfterValidator(_endereco)]

_FORMA = ConfigDict(extra="forbid", frozen=True, strict=True)


# --- Modelo ----------------------------------------------------------------------------------------------------


class Rastro(BaseModel):
    """A leitura que mostra o rastro: uma revisada ativa de classe ``le`` do sistema (``conferir``), os argumentos e
    o ponteiro para o numero ou a lista que conta (objetos ou mudancas)."""

    model_config = _FORMA

    ferramenta: NomeFerramenta
    argumentos: Argumentos = Field(default_factory=dict)
    conta: Ponteiro


class ComArgumentos(BaseModel):
    """Os argumentos que forcam a condicao de um caso (``pede_dados``)."""

    model_config = _FORMA

    argumentos: Argumentos


class ComEspera(BaseModel):
    """Argumentos e quantos segundos a bateria espera (``efeito_adiado``, ``escrita_lenta``)."""

    model_config = _FORMA

    argumentos: Argumentos
    segundos: int = Field(ge=1, le=SEGUNDOS_MAXIMOS)


class VariosItens(BaseModel):
    """Uma escrita de ``itens`` itens, com um item ruim nos ``argumentos`` (C-3.3)."""

    model_config = _FORMA

    argumentos: Argumentos
    itens: int = Field(ge=2, le=MAX_ITENS)


class LeituraDoObjeto(BaseModel):
    """A leitura do objeto do conflito: uma revisada ``le``, os argumentos e os ponteiros da versao e dos dois
    campos que os escritores mudam (para mostrar que os campos sao independentes, regra 2 da secao 7.3 do rascunho)."""

    model_config = _FORMA

    ferramenta: NomeFerramenta
    argumentos: Argumentos = Field(default_factory=dict)
    versao: Ponteiro
    campo: Ponteiro
    campo_concorrente: Ponteiro


class Conflito(BaseModel):
    """C-3.4 ("stale write"): ``argumentos`` muda um campo e ``argumentos_concorrente`` muda o outro; a bateria poe a
    versao lida em ``argumento_versao`` (que nao vem nos argumentos)."""

    model_config = _FORMA

    leitura: LeituraDoObjeto
    argumento_versao: Annotated[str, Field(min_length=1, max_length=128)]
    argumentos: Argumentos
    argumentos_concorrente: Argumentos

    @model_validator(mode="after")
    def _versao_fora_dos_argumentos(self) -> Conflito:
        if self.argumento_versao in self.argumentos or self.argumento_versao in self.argumentos_concorrente:
            raise _erro("conflito", "O argumento da versão é posto pela bateria e não vem nos argumentos do conflito.")
        if self.argumentos == self.argumentos_concorrente:
            raise _erro("conflito", "Os dois escritores do conflito mudam campos diferentes: os argumentos diferem.")
        return self


class Idempotente(BaseModel):
    """A repeticao com numero novo nao muda nada (critica M6). ``rastro``: a leitura do conteudo, quando o rastro da
    ferramenta conta aplicacoes; sem ele, vale o rastro da ferramenta."""

    model_config = _FORMA

    rastro: Rastro | None = None


class FerramentaDeclarada(BaseModel):
    """O que a bateria usa de uma ferramenta de acao (veja o cabecalho do modulo)."""

    model_config = _FORMA

    argumentos: Argumentos
    argumentos_outros: Argumentos
    argumentos_recusados: Argumentos
    rastro: Rastro
    ferramenta_de_teste: NomeFerramenta | None = None
    efeito_adiado: ComEspera | None = None
    escrita_lenta: ComEspera | None = None
    conflito: Conflito | None = None
    varios_itens: VariosItens | None = None
    pede_dados: ComArgumentos | None = None
    idempotente: Idempotente | None = None

    @model_validator(mode="after")
    def _argumentos_diferentes(self) -> FerramentaDeclarada:
        if self.argumentos_outros == self.argumentos:
            raise _erro("argumentos_iguais", "Os argumentos_outros precisam ser diferentes dos argumentos.")
        if self.argumentos_recusados == self.argumentos:
            raise _erro("argumentos_iguais", "Os argumentos_recusados precisam ser diferentes dos argumentos.")
        return self

    def leituras(self) -> list[str]:
        """Toda ferramenta de leitura que a bateria chama por esta entrada (rastro, versao do conflito, conteudo)."""
        nomes = [self.rastro.ferramenta]
        if self.conflito is not None:
            nomes.append(self.conflito.leitura.ferramenta)
        if self.idempotente is not None and self.idempotente.rastro is not None:
            nomes.append(self.idempotente.rastro.ferramenta)
        return nomes


class Declaracao(BaseModel):
    """A declaracao de teste (desenho 2.5). Os quatro primeiros campos sao tambem colunas de ``declaracoes_selo``, e o
    banco confere que sao iguais aos do JSON."""

    model_config = _FORMA

    contrato: Literal["selo/1"]
    modo: Literal["ambiente_de_teste", "ferramentas_de_teste"]
    endereco_teste: Endereco | None = None
    responsavel: Responsavel
    guarda_dias: int = Field(ge=_GUARDA_MINIMA_DIAS, le=GUARDA_MAXIMA_DIAS)
    ferramentas: dict[NomeFerramenta, FerramentaDeclarada] = Field(min_length=1, max_length=_MAX_FERRAMENTAS)

    @model_validator(mode="after")
    def _modo(self) -> Declaracao:
        if self.modo == "ambiente_de_teste":
            if self.endereco_teste is None:
                raise _erro(
                    "alvo_faltando", "No modo ambiente de teste, a declaração traz o endereço do alvo de teste."
                )
            com_teste = sorted(n for n, f in self.ferramentas.items() if f.ferramenta_de_teste is not None)
            if com_teste:
                raise _erro(
                    "ferramenta_de_teste_no_ambiente",
                    f"No modo ambiente de teste, a ferramenta {com_teste[0]} não traz ferramenta de teste: a bateria"
                    " chama a própria ferramenta no alvo de teste.",
                )
            return self
        if self.endereco_teste is not None:
            raise _erro(
                "alvo_sobrando",
                "No modo ferramentas de teste, a declaração não traz endereço de alvo: a bateria usa o endereço de"
                " produção, só com as ferramentas de teste.",
            )
        vistas: dict[str, str] = {}
        for nome in sorted(self.ferramentas):
            de_teste = self.ferramentas[nome].ferramenta_de_teste
            if de_teste is None:
                raise _erro(
                    "sem_ferramenta_de_teste",
                    f"No modo ferramentas de teste, a ferramenta {nome} traz a ferramenta de teste dela.",
                )
            if de_teste in self.ferramentas:
                raise _erro(
                    "ferramenta_de_teste_real",
                    f"A ferramenta de teste de {nome} tem o nome de uma ferramenta de ação: {de_teste}.",
                )
            if de_teste in vistas:
                raise _erro(
                    "ferramenta_de_teste_repetida",
                    f"As ferramentas {vistas[de_teste]} e {nome} têm a mesma ferramenta de teste: {de_teste}.",
                )
            vistas[de_teste] = nome
        return self

    def guardada(self) -> dict[str, Any]:
        """O JSON que vai a ``declaracoes_selo.declaracao``: o que foi declarado, so com os campos dados."""
        return self.model_dump(mode="json", exclude_unset=True)


# --- Leitura do texto e do objeto ------------------------------------------------------------------------------


def _sem_repetidas(pares: list[tuple[str, Any]]) -> dict[str, Any]:
    vistas: dict[str, Any] = {}
    for chave, valor in pares:
        if chave in vistas:
            raise DeclaracaoRecusada("json_invalido", f"A declaração repete a chave {chave[:80]!r} num mesmo objeto.")
        vistas[chave] = valor
    return vistas


def _sem_nan(valor: str) -> Any:
    raise DeclaracaoRecusada("json_invalido", f"A declaração tem um número que o JSON não aceita ({valor}).")


def _grande_demais() -> DeclaracaoRecusada:
    return DeclaracaoRecusada(
        "grande_demais", f"A declaração passa de {contrato.TETO_DECLARACAO_BYTES // 1024} KiB. Encurte os argumentos."
    )


def ler_texto(texto: str) -> Declaracao:
    """A declaracao colada pelo ADMIN: ate ``contrato.TETO_DECLARACAO_BYTES`` em UTF-8, um objeto JSON sem chave
    repetida, sem NaN nem infinito, e na forma do modelo. Levanta ``DeclaracaoRecusada``."""
    if len(texto.encode("utf-8", "surrogatepass")) > contrato.TETO_DECLARACAO_BYTES:
        raise _grande_demais()
    try:
        dados = json.loads(texto, object_pairs_hook=_sem_repetidas, parse_constant=_sem_nan)
    except DeclaracaoRecusada:
        raise
    except ValueError, RecursionError:
        raise DeclaracaoRecusada("json_invalido", "A declaração não é um JSON válido.") from None
    if not isinstance(dados, dict):
        raise DeclaracaoRecusada("json_invalido", "A declaração é um objeto JSON, entre { e }.")
    return validar(dados)


_TEXTOS_DA_FORMA: Final[Mapping[str, str]] = {
    "missing": "falta o campo",
    "extra_forbidden": "o contrato não tem o campo",
    "literal_error": "o valor não é um dos do contrato, no campo",
    "too_short": "falta conteúdo no campo",
    "too_long": "há itens demais no campo",
    "greater_than_equal": "o número é pequeno demais no campo",
    "less_than_equal": "o número é grande demais no campo",
}
"""Os erros do pydantic que viram uma frase com o lugar. Os erros proprios do modelo (``PydanticCustomError``) ja
trazem a frase; o resto vira "valor de tipo errado"."""

_ERROS_PROPRIOS: Final = frozenset(
    {
        "nome_de_ferramenta",
        "nome_reservado",
        "ponteiro",
        "argumentos",
        "argumentos_grandes",
        "argumentos_iguais",
        "responsavel",
        "alvo_invalido",
        "alvo_nome",
        "alvo_faltando",
        "alvo_sobrando",
        "conflito",
        "ferramenta_de_teste_no_ambiente",
        "sem_ferramenta_de_teste",
        "ferramenta_de_teste_real",
        "ferramenta_de_teste_repetida",
    }
)


def _onde(local: tuple[int | str, ...]) -> str:
    return ".".join(str(p) for p in local)[:300]


def _da_forma(erro: ValidationError) -> DeclaracaoRecusada:
    primeiro = erro.errors(include_url=False, include_input=False)[0]
    tipo, onde = str(primeiro["type"]), _onde(tuple(primeiro["loc"]))
    if tipo in _ERROS_PROPRIOS:
        return DeclaracaoRecusada("forma", str(primeiro["msg"]) + (f" Campo: {onde}." if onde else ""))
    if tipo in _TEXTOS_DA_FORMA:
        lugar = f" {onde}" if onde else ""
        return DeclaracaoRecusada("forma", f"A declaração não segue o contrato: {_TEXTOS_DA_FORMA[tipo]}{lugar}.")
    lugar = f", no campo {onde}" if onde else ""
    return DeclaracaoRecusada("forma", f"A declaração não segue o contrato: valor de tipo errado{lugar}.")


def _problema_no_json(valor: Any) -> str | None:
    """Sem recursao: ``"nulo"`` se uma chave ou um texto, em qualquer nivel, tem o caractere nulo (U+0000), que o
    jsonb do PostgreSQL nao guarda; ``"fundo"`` se ha mais de ``FUNDO_MAXIMO`` niveis de objeto e lista. Senao,
    ``None``."""
    pilha: list[tuple[Any, int]] = [(valor, 1)]
    while pilha:
        no, fundo = pilha.pop()
        if isinstance(no, str):
            if "\x00" in no:
                return "nulo"
        elif isinstance(no, Mapping):
            if fundo > FUNDO_MAXIMO:
                return "fundo"
            for chave, filho in no.items():
                if isinstance(chave, str) and "\x00" in chave:
                    return "nulo"
                pilha.append((filho, fundo + 1))
        elif isinstance(no, list | tuple):
            if fundo > FUNDO_MAXIMO:
                return "fundo"
            pilha.extend((filho, fundo + 1) for filho in no)
    return None


NULO_NA_DECLARACAO: Final = "A declaração tem um caractere nulo (\\u0000), que o banco não guarda."
FUNDO_DEMAIS: Final = f"A declaração tem mais de {FUNDO_MAXIMO} níveis de objetos e listas, um dentro do outro."


def validar(dados: Mapping[str, Any]) -> Declaracao:
    """A declaracao de um objeto ja lido (por exemplo, o JSON guardado): ate ``contrato.TETO_DECLARACAO_BYTES`` no
    JSON compacto, sem o caractere nulo, ate ``FUNDO_MAXIMO`` niveis e na forma do modelo. Levanta
    ``DeclaracaoRecusada``: o que passa aqui o banco guarda e ``Declaracao.guardada`` serializa."""
    try:
        tamanho = len(_compacto(dados))
    except ValueError, TypeError, RecursionError:
        raise DeclaracaoRecusada("json_invalido", "A declaração não é um objeto JSON válido.") from None
    if tamanho > contrato.TETO_DECLARACAO_BYTES:
        raise _grande_demais()
    problema = _problema_no_json(dados)
    if problema == "nulo":
        raise DeclaracaoRecusada("json_invalido", NULO_NA_DECLARACAO)
    if problema == "fundo":
        raise DeclaracaoRecusada("json_invalido", FUNDO_DEMAIS)
    try:
        return Declaracao.model_validate(dict(dados))
    except ValidationError as erro:
        raise _da_forma(erro) from None
    except RecursionError:
        raise DeclaracaoRecusada("json_invalido", "A declaração tem níveis demais.") from None


# --- Conferencia contra o cadastro ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cadastro:
    """O que a declaracao confere no cadastro de agora (``cadastro_do_sistema``).

    ``tipo_chave``: o de ``sistemas.tipo_chave`` (o selo de nivel 2 ou 3 e so para chave por pessoa, critica B7).
    ``acoes``: o nome original das ferramentas de acao que o selo cobre (``selo_acoes_do_sistema``). ``leituras``: o
    das revisadas ativas de classe ``le``. ``revisadas``: o de toda revisada do sistema, de qualquer classe e
    situacao. ``producoes``: o endereco de producao de todo sistema do cadastro, de qualquer situacao."""

    de_terceiro: bool
    tipo_chave: str
    acoes: frozenset[str]
    leituras: frozenset[str]
    revisadas: frozenset[str]
    producoes: tuple[str, ...]


def recusar_terceiro(de_terceiro: bool) -> None:
    """Ate o G7, a declaracao e o pedido de teste de um servidor de terceiros sao recusados (desenho 8)."""
    if de_terceiro:
        raise DeclaracaoRecusada("selo_terceiro_ainda_nao", TERCEIRO_AINDA_NAO)


def _ip_escrito(nome: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """O IP que o nome escreve, tambem na forma curta que ``socket.inet_aton`` aceita; ``None`` para um nome comum."""
    try:
        return ipaddress.ip_address(nome)
    except ValueError:
        pass
    if not _IPV4_ESCRITO.fullmatch(nome):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(nome))
    except OSError:
        return None


def forma_de_conexao(endereco: str) -> tuple[str, int, str] | None:
    """Onde o cliente conecta: o nome, a porta efetiva e o caminho (critica A4). ``None`` se o cliente nao le o
    endereco.

    O nome e o que o leitor do cliente devolve (``httpx2``: IDNA, pontos de outros alfabetos trocados por pontos
    comuns), em minusculas e sem ponto no fim. Um IP escrito de qualquer jeito (``127.1``, ``2130706433``, IPv6
    longo) vai na forma comum, e um IPv4 dentro de um IPv6 (``::ffff:10.0.0.5``) vale pelo IPv4. ``localhost``, os
    nomes ``.localhost``, todo IP de loopback e o IP nao especificado (``0.0.0.0``, ``::``) chegam a mesma maquina:
    viram um nome so. A porta e a efetiva (80 no http e 443 no https sem porta) e o caminho e o de
    ``contrato.endereco_normalizado``. O esquema nao entra: o mesmo nome na mesma porta e o mesmo servico. Como
    ``endereco_normalizado``, e de proposito mais largo que a igualdade de URL.

    O banco tem a mesma regra (``selo_endereco_conexao``, em ``selo_alvo_na_producao``; o teste confere que os dois
    juntam os mesmos enderecos), menos o nome fora de ASCII: o ``httpx2`` o escreve na forma ``xn--`` (IDNA), que o
    banco nao calcula. Esse caso fica so com a tela (``alvo_na_producao``)."""
    try:
        url = httpx2.URL(endereco)
        nome = url.raw_host.decode("ascii").lower().rstrip(".")
    except httpx2.InvalidURL, UnicodeError, ValueError:
        return None
    porta = url.port if url.port is not None else _PORTA_PADRAO.get(url.scheme.lower())
    if porta is None or not nome:
        return None
    ip = _ip_escrito(nome)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if nome == _LOCAL or nome.endswith("." + _LOCAL) or (ip is not None and (ip.is_loopback or ip.is_unspecified)):
        nome = _LOCAL
    elif ip is not None:
        nome = str(ip)
    resto = contrato.endereco_normalizado(endereco).partition("://")[2]
    barra = resto.find("/")
    return nome, porta, resto[barra:] if barra >= 0 else ""


def alvo_na_producao(endereco: str, producoes: Iterable[str]) -> bool:
    """O alvo de teste pode ser o endereco de producao de algum sistema (critica A4): igual na forma normalizada
    (``contrato.mesmo_endereco``, a regra do banco) ou na forma de conexao (``forma_de_conexao``), que pega o nome e o
    IP escritos de outro jeito, dos dois lados."""
    alvo = forma_de_conexao(endereco)
    return any(
        contrato.mesmo_endereco(endereco, p) or (alvo is not None and alvo == forma_de_conexao(p)) for p in producoes
    )


def conferir(declaracao: Declaracao, cadastro: Cadastro, *, ambiente: Ambiente) -> None:
    """Confere a declaracao contra o cadastro de agora (veja o cabecalho do modulo). Vale ao salvar e de novo ao
    pedir o teste. Levanta ``DeclaracaoRecusada`` com a primeira recusa."""
    recusar_terceiro(cadastro.de_terceiro)
    if cadastro.tipo_chave != "por_pessoa":
        raise DeclaracaoRecusada(
            "sem_chave_por_pessoa",
            "O selo de nível 2 ou 3 é só para sistema com chave por pessoa: a consulta e o aviso usam a chave de quem"
            " pediu a ação.",
        )
    if not cadastro.acoes:
        raise DeclaracaoRecusada(
            "sem_acoes",
            "Este sistema não tem ferramenta de ação revisada: o selo de nível 2 ou 3 é só para sistema com ferramenta"
            " de ação.",
        )
    alvo = declaracao.endereco_teste
    if alvo is not None:
        if ambiente not in AMBIENTES_SEM_HTTPS and not alvo.startswith("https://"):
            raise DeclaracaoRecusada("alvo_sem_https", "Fora de desenvolvimento e teste, o alvo de teste usa https.")
        if alvo_na_producao(alvo, cadastro.producoes):
            raise DeclaracaoRecusada(
                "alvo_na_producao",
                "O alvo de teste é o endereço de produção de um sistema do cadastro (comparado como o MCP-CLARO"
                " conecta: sem diferença de maiúsculas, porta padrão, barra no fim, consulta nem jeito de escrever o"
                " nome ou o IP). A bateria faz escritas reais no alvo: ele nunca pode ser a produção.",
            )
    faltam = sorted(cadastro.acoes - set(declaracao.ferramentas))
    if faltam:
        raise DeclaracaoRecusada(
            "selo_declaracao_incompleta",
            f"A declaração não cobre a ferramenta {faltam[0]}. Toda ferramenta de ação precisa estar nela.",
            ferramenta=faltam[0],
        )
    for nome in sorted(declaracao.ferramentas):
        if nome not in cadastro.acoes:
            raise DeclaracaoRecusada(
                "ferramenta_que_nao_e_acao",
                f"A declaração traz {nome}, que não é uma ferramenta de ação ativa deste sistema.",
                ferramenta=nome,
            )
        entrada = declaracao.ferramentas[nome]
        for leitura in entrada.leituras():
            if leitura not in cadastro.leituras:
                raise DeclaracaoRecusada(
                    "leitura_que_nao_le",
                    f"A leitura {leitura}, da ferramenta {nome}, precisa ser uma ferramenta revisada deste sistema que"
                    " só lê.",
                    ferramenta=nome,
                )
        de_teste = entrada.ferramenta_de_teste
        if de_teste is not None and de_teste in cadastro.revisadas:
            raise DeclaracaoRecusada(
                "ferramenta_de_teste_real",
                f"A ferramenta de teste {de_teste}, da ferramenta {nome}, tem o nome de uma ferramenta revisada deste"
                " sistema. A ferramenta de teste tem um nome que nenhuma ferramenta de produção usa.",
                ferramenta=nome,
            )


async def cadastro_do_sistema(c: AsyncConnection, sistema_id: uuid.UUID) -> Cadastro:
    """Le o cadastro de agora para ``conferir``. Levanta ``LookupError`` se o sistema nao existe."""
    sistema = (
        await c.execute(text("SELECT origem, tipo_chave FROM sistemas WHERE id = :s"), {"s": sistema_id})
    ).first()
    if sistema is None:
        raise LookupError("sistema nao encontrado")
    acoes = (
        await c.execute(
            text("SELECT nome_original FROM ferramentas_revisadas WHERE id = ANY (selo_acoes_do_sistema(:s))"),
            {"s": sistema_id},
        )
    ).scalars()
    leituras = (
        await c.execute(
            text(
                "SELECT nome_original FROM ferramentas_revisadas"
                " WHERE sistema_id = :s AND situacao = 'ativa' AND classe = 'le'"
            ),
            {"s": sistema_id},
        )
    ).scalars()
    revisadas = (
        await c.execute(
            text("SELECT DISTINCT nome_original FROM ferramentas_revisadas WHERE sistema_id = :s"), {"s": sistema_id}
        )
    ).scalars()
    producoes = (await c.execute(text("SELECT endereco FROM sistemas ORDER BY id"))).scalars()
    return Cadastro(
        de_terceiro=str(sistema.origem) != "proprio",
        tipo_chave=str(sistema.tipo_chave),
        acoes=frozenset(str(n) for n in acoes),
        leituras=frozenset(str(n) for n in leituras),
        revisadas=frozenset(str(n) for n in revisadas),
        producoes=tuple(str(e) for e in producoes),
    )


# --- Gravacao e leitura de declaracoes_selo -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclaracaoSalva:
    """Uma linha de ``declaracoes_selo``. ``dados`` e o JSON guardado; ``modelo()`` o valida de novo."""

    id: uuid.UUID
    sistema_id: uuid.UUID
    versao: int
    dados: dict[str, Any]
    salva_por: str
    pessoa_id: uuid.UUID
    salva_em: datetime

    def modelo(self) -> Declaracao:
        return validar(self.dados)


async def salvar(
    c: AsyncConnection, *, sistema_id: uuid.UUID, pessoa_id: uuid.UUID, salva_por: str, declaracao: Declaracao
) -> DeclaracaoSalva:
    """Grava a proxima versao de ``declaracoes_selo`` (so de acrescentar). Quem chama ja conferiu (``conferir``), faz
    a linha do extrato na mesma transacao e o commit. Duas gravacoes do mesmo sistema ao mesmo tempo entram uma
    depois da outra (trava consultiva da transacao), cada uma com a sua versao."""
    await c.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('mcp_claro.selo.declaracao'), hashtext(CAST(:s AS text)))"),
        {"s": str(sistema_id)},
    )
    dados = declaracao.guardada()
    linha = (
        await c.execute(
            text(
                "INSERT INTO declaracoes_selo (sistema_id, versao, contrato, modo, endereco_teste, declaracao,"
                " responsavel, salva_por, pessoa_id)"
                " SELECT CAST(:s AS uuid), coalesce(max(versao), 0) + 1, CAST(:c AS text), CAST(:m AS text),"
                " CAST(:e AS text), CAST(:d AS jsonb), CAST(:r AS text), CAST(:q AS text), CAST(:p AS uuid)"
                " FROM declaracoes_selo WHERE sistema_id = :s"
                " RETURNING id, sistema_id, versao, declaracao, salva_por, pessoa_id, salva_em"
            ),
            {
                "s": sistema_id,
                "c": declaracao.contrato,
                "m": declaracao.modo,
                "e": declaracao.endereco_teste,
                "d": json.dumps(dados, ensure_ascii=False, allow_nan=False),
                "r": declaracao.responsavel,
                "q": salva_por,
                "p": pessoa_id,
            },
        )
    ).one()
    return _salva(linha)


def _salva(linha: Any) -> DeclaracaoSalva:
    return DeclaracaoSalva(
        uuid.UUID(str(linha.id)),
        uuid.UUID(str(linha.sistema_id)),
        int(linha.versao),
        dict(linha.declaracao),
        str(linha.salva_por),
        uuid.UUID(str(linha.pessoa_id)),
        linha.salva_em,
    )


async def ultima(c: AsyncConnection, sistema_id: uuid.UUID) -> DeclaracaoSalva | None:
    """A declaracao mais recente do sistema (a que o pedido de teste usa), ou ``None``."""
    linha = (
        await c.execute(
            text(
                "SELECT id, sistema_id, versao, declaracao, salva_por, pessoa_id, salva_em FROM declaracoes_selo"
                " WHERE sistema_id = :s ORDER BY versao DESC LIMIT 1"
            ),
            {"s": sistema_id},
        )
    ).first()
    return None if linha is None else _salva(linha)
