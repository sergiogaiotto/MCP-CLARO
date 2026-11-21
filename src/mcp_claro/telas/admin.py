"""Telas do ADMIN: aplicativos, sistemas e sondagem, revisao de ferramentas, modelos de ordem, destinos de
webhook e abertura do extrato de outra pessoa. Os programas que rodam sozinhos estao em ``telas/admin_programas.py``.

Um aplicativo assistente que a conferencia do banco na partida pausou volta a ativo aqui, por decisao do ADMIN
(``reativar_aplicativo``); o de um programa volta pela tela do programa.

Na revisao da ferramenta, o ADMIN marca os campos do pedido que dizem para quem a acao vai (so campos do esquema
de entrada). O limite "so estes destinatarios" da pessoa confere estes campos; o resto do pedido fica livre.

Toda mudanca vai ao extrato, na mesma transacao, com quem fez. A sondagem de versao e a leitura
das declaracoes sao pedidas ao despachante pela porta interna: so ele fala com os sistemas.

Servidores de terceiros (Fase 3):

- o cadastro pede de quem e o servidor (fixo para sempre: para mudar, cadastra-se de novo); um terceiro so com
  endereco https e publico (fora de desenvolvimento e teste) e chave por pessoa ou compartilhada. O despachante
  confere o endereco de novo, com o nome resolvido, antes de cada conversa;
- a ficha de um terceiro mostra a sala de visitas, a revisao do servidor (vigente ou nao), a quarentena, o desfecho
  da sondagem em palavras e os pedidos que ele fez e o MCP-CLARO recusou. Texto do servidor (nomes e descricoes que
  ele declara) so com o filtro ``escondidos`` e a classe ``estranho``;
- ``revisar_servidor`` pede ao despachante a conclusao da revisao do servidor (``/interno/revisao_servidor``): ele
  le o servidor de novo e grava. A recusa volta com o motivo e a tela escreve a frase (``paginas``);
- a revisao de cada ferramenta de terceiro mostra "O que o aplicativo vai ver" (o esquema limpo, com as descricoes
  de campo do ADMIN), trava a marca de texto de fora e recusa o esquema que a revisao de terceiro nao aceita, com a
  frase por motivo. Recusa tambem a ferramenta de nome com mais de ``TETO_NOME_FERRAMENTA`` letras, que a sala de
  visitas nao chama (``NOME_LONGO_DEMAIS``);
- um modelo de ordem so leva ferramenta de terceiro publicada na revisao do servidor vigente;
- a conclusao da revisao do servidor tem teto de formulario proprio (``TETO_FORMULARIO_CONCLUSAO``): cabem as
  ``TETO_PUBLICAR`` caixas do despachante, e as outras rotas ficam com o teto de sempre. Caixas demais voltam com
  ``CAIXAS_DEMAIS``, e o formulario grande demais com ``seguranca.GRANDE_DEMAIS``, em portugues.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import socket
import uuid
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import httpx2
from markupsafe import Markup, escape
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.cadastro import esquema, repositorio
from mcp_claro.cadastro.quarentena import MOTIVO_QUARENTENA, MOTIVO_QUARENTENA_VERSAO
from mcp_claro.config.settings import Ambiente
from mcp_claro.entrada.argumentos import ESQUEMA_FUNDO_DEMAIS, EsquemaInvalido
from mcp_claro.logs import logger
from mcp_claro.regras.escondidos import marcar_escondidos
from mcp_claro.saida.quarentena import MOTIVO_SEM_RESPOSTA
from mcp_claro.telas import consultas, consultas_terceiros, limites, paginas
from mcp_claro.telas.consultas_terceiros import PedidosRecusados
from mcp_claro.telas.nucleo import Interrompe, Telas, numero_pagina, tela, uuid_ou_none, ver_depois
from mcp_claro.telas.seguranca import Formulario, TetoFormulario, rota_com_teto
from mcp_claro.telas.sessoes import Sessao
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.telas.admin")

ID_SIMPLES: Final = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
NOME_VITRINE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
NOME_CAMPO: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
NOME_EVENTO: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
FAMILIA: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
CONTROLE: Final = re.compile(r"[\x00-\x1f\x7f]")
CLASSES: Final = ("le", "muda", "tem_volta")
RISCOS: Final = ("baixo", "medio", "alto")
TIPOS_CHAVE: Final = ("por_pessoa", "compartilhada", "programa")
# Os motivos aparecem para a pessoa (Meus limites, a pagina do limite e a da acao): texto de tela, com acento.
MOTIVO_CAMPOS_MUDARAM: Final = "o ADMIN marcou outro campo de destinatário nesta ação; crie outro limite se precisar"
MOTIVO_CANCELADA_CAMPOS: Final = (
    "cancelada porque o limite que a liberou terminou: o ADMIN mudou os campos de destinatário"
)
# Cadastro de sistema (recusas com 400).
ENDERECO_GERAL: Final = "O endereço precisa ser completo e começar com https:// (sem usuário e senha)."
ENDERECO_NOME_INVALIDO: Final = "Endereço com nome de servidor inválido."
ENDERECO_TERCEIRO_HTTPS: Final = "Servidor de terceiros precisa de endereço https."
ENDERECO_NAO_PUBLICO: Final = (
    "Este endereço não é público. O MCP-CLARO só chama servidores de terceiros em endereços da internet."
)
ORIGEM_FALTA: Final = "Escolha de quem é o servidor."
CHAVE_DE_TERCEIRO: Final = (
    "Servidor de terceiros usa uma chave por pessoa ou uma chave para todos (só leitura). Chave de programa fica"
    " para depois."
)
# Revisao do servidor (formulario da ficha).
FORNECEDOR_ROTULO: Final = "quem fornece este servidor"
FORNECEDOR_LETRAS: Final = (3, 200)
"""Do mesmo tamanho que o despachante aceita (``despachante.servico.TETO_FORNECEDOR``)."""
PUBLICAR_FORA_DA_LISTA: Final = "Marque para a vitrine só ferramentas da lista desta página."
PUBLICAR_DEMAIS: Final = "Marque até 500 ferramentas para a vitrine."
TETO_PUBLICAR: Final = 500
SO_TERCEIRO: Final = "Só servidor de terceiros tem revisão do servidor."
TETO_NOME_FERRAMENTA: Final = 128
"""O nome de ferramenta mais longo que o MCP preve (SEP-986, no kit: ``mcp.shared.tool_name_validation``) e que a
sala de visitas aceita chamar (``ligacao.sala_contrato.PedidoChamar.nome``)."""
NOME_LONGO_DEMAIS: Final = (
    f"O servidor declara esta ferramenta com um nome de mais de {TETO_NOME_FERRAMENTA} letras. O MCP-CLARO não chama"
    " ferramenta de terceiros com nome tão longo, então ela não pode ser revisada nem ir para a vitrine."
)
"""Recusa da revisao de uma ferramenta de terceiro com nome mais longo que ``TETO_NOME_FERRAMENTA``. Sem revisao, ela
nunca vira caixa na conclusao: o teto do formulario dela conta nomes de ate ``TETO_NOME_FERRAMENTA`` letras."""
_BYTES_POR_LETRA: Final = 12
"""No formulario, cada byte de uma letra fora de A-Z, 0-9 e ``_.-`` vira ``%XX``: uma letra de 4 bytes em UTF-8 vira
12."""
_FOLGA_CSRF: Final = 1024
"""O campo do token CSRF (``csrf=`` e 43 letras) e o que sobra das separacoes, com folga."""
_BYTES_POR_CAIXA: Final = len("&publicar=") + TETO_NOME_FERRAMENTA * _BYTES_POR_LETRA
"""Uma caixa marcada no pior caso: o nome com ``TETO_NOME_FERRAMENTA`` letras, todas de 4 bytes."""
CAIXAS_DEMAIS: Final = PUBLICAR_DEMAIS + " Nada mudou."
TETO_FORMULARIO_CONCLUSAO: Final = TetoFormulario(
    tamanho=(TETO_PUBLICAR + 1) * _BYTES_POR_CAIXA
    + len("&fornecedor=")
    + FORNECEDOR_LETRAS[1] * _BYTES_POR_LETRA
    + _FOLGA_CSRF,
    campos=TETO_PUBLICAR + 2,
    campos_demais=CAIXAS_DEMAIS,
)
"""O formulario da conclusao da revisao do servidor: ``TETO_PUBLICAR`` caixas marcadas com nomes de ate
``TETO_NOME_FERRAMENTA`` letras quaisquer, o fornecedor e o CSRF (502 campos). So esta rota: as outras ficam com
``seguranca.TETO_PADRAO``.

O tamanho (cerca de 760 KiB) conta uma caixa de pior caso a mais que ``TETO_PUBLICAR``. Assim, com uma caixa a mais,
mesmo no pior caso, o formulario cabe no tamanho e chega a conta de campos, que recusa com ``CAIXAS_DEMAIS``. Com
mais caixas, o corpo passa do tamanho, e o leitor conta os campos do que ja chegou: passou deles, ``CAIXAS_DEMAIS``;
senao (nomes mais longos que o teto, que a revisao de terceiro recusa: ``NOME_LONGO_DEMAIS``),
``seguranca.GRANDE_DEMAIS``. O ``max_body_size`` da rota tem folga sobre o tamanho (``seguranca.FOLGA_DA_ROTA``):
ate ali, quem responde e o leitor, em portugues, e nao o 413 do Starlette, em ingles."""
# Revisao de ferramenta.
TEXTO_DE_FORA_SIM: Final = "sim"
PREFIXO_DESCRICAO_CAMPO: Final = "desc:"
MODELO_FORA_DA_REVISAO: Final = (
    "Esta ferramenta é de um servidor de terceiros e não foi publicada na revisão do servidor."
)


_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")


def _ip_publico(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """IP da internet, pela mesma regra do cliente (``ligacao.cliente``): IPv6 que carrega um IPv4 dentro
    (mapeado, 6to4, Teredo, NAT64) vale pelo de dentro. A conferencia completa, com o nome resolvido, e a do
    despachante (``ligacao.cliente.conferir_endereco_terceiro``), antes de cada conversa."""
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        dentro = [d for d in (ip.ipv4_mapped, ip.sixtofour) if d is not None]
        if ip.teredo is not None:
            dentro.extend(ip.teredo)
        if ip in _NAT64:
            dentro.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        return all(d.is_global for d in dentro)
    return True


_IPV4_CURTO: Final = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+)){0,3}$")
"""IPv4 escrito de outro jeito (``127.1``, ``2130706433``, ``0x7f000001``, ``0177.0.0.1``): o resolvedor do sistema
aceita e chega ao mesmo IP."""


def _ip_escrito(nome: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """O IP que o nome escreve, tambem na forma curta que ``socket.inet_aton`` aceita; None para um nome comum."""
    try:
        return ipaddress.ip_address(nome)
    except ValueError:
        pass
    if not _IPV4_CURTO.match(nome):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(nome))
    except OSError:
        return None


def _nome_publico(host: str) -> bool:
    """Falso para nome local (``localhost``) e para IP escrito no endereco que nao e da internet, tambem na forma
    curta (``127.1``). ``host`` e o nome que o cliente conecta (``raw_host`` do ``httpx2``), ja em ASCII. Um nome
    comum passa aqui: o despachante resolve e confere cada IP antes de abrir conexao."""
    nome = host.lower().rstrip(".")
    if nome == "localhost" or nome.endswith(".localhost"):
        return False
    ip = _ip_escrito(nome)
    return ip is None or _ip_publico(ip)


def problema_do_endereco(endereco: str, *, origem: str, ambiente: str) -> str | None:
    """A frase da recusa do endereco no cadastro, ou None. Em desenvolvimento e teste, vale http e endereco local
    (os sistemas de teste rodam em 127.0.0.1). Origem que nao e ``proprio`` conta como terceiro."""
    local = ambiente in (Ambiente.DESENVOLVIMENTO, Ambiente.TESTE)
    terceiro = origem != "proprio"
    if len(endereco) > 500 or CONTROLE.search(endereco):
        return ENDERECO_GERAL
    try:
        partes = urlsplit(endereco)
        partes.port  # noqa: B018 - porta fora da faixa levanta ValueError
    except ValueError:
        return ENDERECO_GERAL
    if not partes.hostname or partes.username is not None or partes.password is not None or partes.fragment:
        return ENDERECO_GERAL
    if partes.scheme == "http" and terceiro and not local:
        return ENDERECO_TERCEIRO_HTTPS
    if partes.scheme not in ({"https", "http"} if local else {"https"}):
        return ENDERECO_GERAL
    try:
        # O mesmo leitor e o mesmo nome do cliente (``ligacao.cliente._nome_de_conexao``): o IDNA recusa nomes como
        # "☃.net" e troca os pontos de outros alfabetos (U+3002, U+FF0E, U+FF61: "127<U+3002>1") por pontos comuns.
        # Conferir o nome do ``urlsplit``, que guarda esses pontos, deixava passar um IP local que o cliente conecta.
        nome = httpx2.URL(endereco).raw_host.decode("ascii")
    except httpx2.InvalidURL, UnicodeError, ValueError:
        return ENDERECO_NOME_INVALIDO
    if terceiro and not local and not _nome_publico(nome):
        return ENDERECO_NAO_PUBLICO
    return None


def frase_do_esquema(erro: EsquemaInvalido) -> Markup:
    """A frase da tela para um esquema de entrada que a revisao nao aceita. O que vem do esquema (o caminho da
    recusa, a mensagem da conferencia) vai dentro de um elemento ``estranho``, com os escondidos marcados."""
    if isinstance(erro, esquema.EsquemaRecusado):
        return paginas.frase_do_esquema_recusado_na_tela(erro.motivo, erro.onde, erro.palavra)
    if str(erro) == ESQUEMA_FUNDO_DEMAIS:
        return escape(paginas.ESQUEMA_FUNDO_DEMAIS)
    return Markup('O esquema de entrada desta ferramenta não é aceito: <span class="estranho">{}</span>.').format(
        marcar_escondidos(str(erro))
    )


def erro_descricao_longa(campo: str) -> Markup:
    """A descricao de um campo passou do teto. O nome do campo vem do fornecedor."""
    return Markup("A descrição do campo {} passa de {} letras.").format(
        paginas.campo_do_fornecedor(campo), esquema.DESCRICAO_DE_CAMPO_MAXIMA
    )


def erro_campos_desconhecidos(campos: Any) -> Markup:
    """Descricao para campo que a ferramenta nao pede (``esquema.CampoDesconhecido``). Os nomes vem do formulario e
    so aparecem dentro de ``estranho``."""
    nomes = Markup(", ").join(paginas.campo_do_fornecedor(str(c)) for c in campos)
    return Markup("Escreva descrição só para campos que a ferramenta pede. Estes não estão nela: {}.").format(nomes)


DESFECHOS_NO_EXTRATO: Final[dict[str, str]] = {
    "aceito": "aceito, fala a 2026-07-28",
    "versao": "recusado, nao fala a 2026-07-28",
    "desvio": "desvio recusado",
    "nao_mcp": "endereco nao responde como MCP",
    "sem_acesso": "pediu chave",
    "inalcancavel": "sistema inalcancavel",
}
"""O desfecho da sondagem na frase ``admin_sondagem_pedida`` (sem acento, como o extrato)."""


def desfecho_no_extrato(resposta: dict[str, Any]) -> str:
    """Pelo ``desfecho`` que o despachante devolve; sem ele (despachante de antes), por ``aceito`` e
    ``inalcancavel``, como antes."""
    desfecho = resposta.get("desfecho")
    if isinstance(desfecho, str) and desfecho in DESFECHOS_NO_EXTRATO:
        return DESFECHOS_NO_EXTRATO[desfecho]
    if resposta.get("aceito"):
        return DESFECHOS_NO_EXTRATO["aceito"]
    if resposta.get("inalcancavel"):
        return DESFECHOS_NO_EXTRATO["inalcancavel"]
    return DESFECHOS_NO_EXTRATO["versao"]


# Falha ao ler as declaracoes (``/interno/declaracoes``): 503 com motivo quando a falha e do MCP-CLARO.
DECLARACOES_NOSSAS: Final = frozenset({"sala_fora", "chave_indisponivel"})
DECLARACOES_FALHA: Final[dict[str, tuple[str, str]]] = {
    "sala_fora": (
        "A sala de visitas não está disponível",
        "A sala de visitas não está disponível agora. Nada foi pedido ao servidor. Tente de novo daqui a pouco.",
    ),
    "chave_indisponivel": (
        "O cofre não abriu a chave",
        "Não deu para abrir no cofre a chave de leitura das declarações. Tente de novo daqui a pouco.",
    ),
    "": (
        "Não conseguimos ler",
        "O despachante não conseguiu ler as ferramentas deste sistema. Confira a chave e tente de novo.",
    ),
}


def _problemas(ferramentas: Any) -> list[tuple[str, str]]:
    """A lista ``ferramentas`` da recusa: (nome declarado, frase do problema). Nome e texto do servidor: a tela o
    mostra com o filtro ``escondidos`` e a classe ``estranho``."""
    saida: list[tuple[str, str]] = []
    for item in ferramentas if isinstance(ferramentas, list) else []:
        if not isinstance(item, dict):
            continue
        nome, problema = item.get("ferramenta"), item.get("problema")
        if isinstance(nome, str):
            saida.append((nome, paginas.PROBLEMAS_REVISAO.get(str(problema), paginas.PROBLEMA_DESCONHECIDO)))
    return saida


def _json_legivel(valor: Any) -> str:
    try:
        return json.dumps(valor, indent=2, ensure_ascii=False)
    except ValueError, RecursionError:
        return "(fundo demais para mostrar)"


def _descricoes_gravadas(esquema_entrada: Any) -> dict[str, str]:
    """As descricoes de campo do primeiro nivel do esquema gravado de um terceiro: sao as do ADMIN (o texto do
    fornecedor sai na limpeza)."""
    propriedades = esquema_entrada.get("properties") if isinstance(esquema_entrada, dict) else None
    if not isinstance(propriedades, dict):
        return {}
    return {
        str(campo): str(sub["description"])
        for campo, sub in propriedades.items()
        if isinstance(sub, dict) and isinstance(sub.get("description"), str)
    }


def _previa_de_terceiro(entrada: dict[str, Any], descricoes: dict[str, str]) -> tuple[str | None, str | None]:
    """ "O que o aplicativo vai ver": o esquema limpo, com as descricoes do ADMIN. Antes, ``conferir_documento``,
    que nao usa recursao e para no teto: um esquema enorme ou fundo demais mostra a frase, nunca trava a tela.
    Devolve (previa, frase da recusa)."""
    try:
        esquema.conferir_documento(entrada)
        limpo = esquema.limpar_textos(entrada)
    except EsquemaInvalido as erro:
        return None, frase_do_esquema(erro)
    # Campo que nao existe ou descricao longa: a previa fica sem as descricoes, e a revisao recusa com a frase.
    with contextlib.suppress(ValueError):
        limpo = esquema.com_descricoes(limpo, descricoes)
    return _json_legivel(limpo), None


@dataclass(frozen=True, slots=True)
class FerramentaDoTerceiro:
    """Uma ferramenta declarada, na ficha de um terceiro: a revisao dela e se pode ir a vitrine agora."""

    nome: str
    definicao: dict[str, Any]
    revisada: bool
    em_dia: bool
    """Revisada, com a mesma impressao da ultima leitura e com o texto declarado guardado (revisao da Fase 3)."""
    mudou: bool
    publicada: bool
    """Publicada na revisao do servidor vigente (esta revisada, nao outra versao dela)."""
    nome_vitrine: str | None
    classe: str | None
    antes: dict[str, Any] | None
    """O que o servidor declarava na hora da revisao, quando mudou desde ela (para o antes e depois)."""


@dataclass(frozen=True, slots=True)
class FichaDoTerceiro:
    """O que a ficha de um servidor de terceiros mostra alem do cadastro."""

    detalhe: Any
    ferramentas: list[FerramentaDoTerceiro]
    fora_da_vitrine: int
    """Publicadas na revisao vigente que o ADMIN reviu depois dela (outra versao ativa): a vitrine so mostra a versao
    publicada, entao ficam fora ate a nova conclusao."""
    mudaram_no_servidor: int
    """Publicadas na revisao vigente que o servidor declara diferente na ultima leitura. A vitrine nao compara com a
    leitura: elas continuam la, com o texto da revisao, ate o mudou-parou por o servidor em quarentena."""
    deixou_de_valer: bool
    """Ja houve revisao do servidor e nenhuma vale, fora da quarentena: a anterior caiu quando ele saiu de ativo
    (desativado, pendente, recusado, ou ativo de novo antes da nova conclusao)."""
    pedidos: PedidosRecusados
    quarentena: Any
    quarentena_mudou: bool
    quarentena_sem_resposta: bool
    quarentena_versao: bool
    """Quarentena pela sondagem que achou outra versao (D29). O motivo fica o mesmo depois que a versao e conferida de
    novo: a ficha diz o motivo pela quarentena, nao pelo desfecho da ultima sondagem."""
    fornecedor: str


def _antes(definicao_declarada: Any) -> dict[str, Any] | None:
    return dict(definicao_declarada) if isinstance(definicao_declarada, dict) else None


async def ficha_do_terceiro(c: AsyncConnection, linha: Any) -> FichaDoTerceiro:
    """Junta as consultas da ficha de um terceiro e calcula o que vai a vitrine na proxima conclusao."""
    detalhe = await consultas_terceiros.detalhe(c, linha.id)
    vistas = await consultas_terceiros.ferramentas(c, linha.id)
    publicadas = await consultas_terceiros.publicadas(c, linha.id)
    pedidos = await consultas_terceiros.pedidos_recusados(c, linha.id)
    quarentena = await consultas_terceiros.ultima_quarentena(c, linha.id) if linha.situacao == "quarentena" else None
    ids_publicados = {p.id for p in publicadas}
    lista: list[FerramentaDoTerceiro] = []
    for v in vistas:
        revisada = v.revisada_id is not None
        mudou = revisada and v.impressao != v.impressao_revisada
        lista.append(
            FerramentaDoTerceiro(
                nome=str(v.nome),
                definicao=dict(v.definicao),
                revisada=revisada,
                em_dia=revisada and not mudou and v.definicao_declarada is not None,
                mudou=mudou,
                publicada=revisada and v.revisada_id in ids_publicados,
                nome_vitrine=v.nome_vitrine,
                classe=v.classe,
                antes=_antes(v.definicao_declarada) if mudou else None,
            )
        )
    agora = {f.nome: f for f in lista}
    revistas = sum(1 for p in publicadas if p.situacao != "ativa")
    mudaram = sum(
        1 for p in publicadas if p.situacao == "ativa" and p.nome_original in agora and agora[p.nome_original].mudou
    )
    caiu = (
        detalhe is not None
        and detalhe.revisao_servidor_id is None
        and bool(detalhe.revisoes)
        and linha.situacao != "quarentena"
    )
    return FichaDoTerceiro(
        detalhe=detalhe,
        ferramentas=lista,
        fora_da_vitrine=revistas,
        mudaram_no_servidor=mudaram,
        deixou_de_valer=caiu,
        pedidos=pedidos,
        quarentena=quarentena,
        quarentena_mudou=linha.motivo == MOTIVO_QUARENTENA,
        quarentena_sem_resposta=linha.motivo == MOTIVO_SEM_RESPOSTA,
        quarentena_versao=linha.motivo == MOTIVO_QUARENTENA_VERSAO,
        fornecedor=str((detalhe.rs_fornecedor or detalhe.ultimo_fornecedor or "") if detalhe else ""),
    )


def conferir_publicar(marcadas: list[str], declaradas: set[str]) -> tuple[list[str], list[str]]:
    """As ferramentas marcadas para a vitrine na conclusao da revisao do servidor (sem repeticao e so as que o
    servidor declarou, que o banco guardou: nome com caractere nulo nunca chega ao despachante) e as recusas.

    O teto e o do despachante (``TETO_PUBLICAR``). O formulario desta rota (``TETO_FORMULARIO_CONCLUSAO``) ja para
    numa caixa a mais; esta conta vale para o que sobra depois de tirar as repetidas."""
    erros: list[str] = []
    publicar = list(dict.fromkeys(marcadas))
    if any(nome not in declaradas for nome in publicar):
        erros.append(PUBLICAR_FORA_DA_LISTA)
        publicar = [nome for nome in publicar if nome in declaradas]
    if len(publicar) > TETO_PUBLICAR:
        erros.append(PUBLICAR_DEMAIS)
    return publicar, erros


def valores_da_conclusao(terceiro: FichaDoTerceiro | None) -> dict[str, Any]:
    """O formulario da conclusao como a ficha abre: o fornecedor da ultima revisao e marcadas as publicadas que ainda
    podem ir a vitrine (em dia, com o texto declarado guardado)."""
    if terceiro is None:
        return {"fornecedor": "", "publicar": []}
    return {
        "fornecedor": terceiro.fornecedor,
        "publicar": [f.nome for f in terceiro.ferramentas if f.publicada and f.em_dia],
    }


def nome_vitrine_sugerido(sistema: str, nome: str, *, terceiro: bool) -> str:
    """O "Nome na vitrine" que a pagina da ferramenta sugere antes da primeira revisao. Num terceiro, so o nome do
    sistema: o nome declarado e texto do fornecedor, e o ADMIN escreve o resto."""
    base = f"{sistema}_" if terceiro else f"{sistema}_{nome}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)[:128]


def _limpo(valor: str) -> str:
    return " ".join(CONTROLE.sub(" ", valor).split())


def _texto(f: Formulario, nome: str, rotulo: str, erros: list[str], *, minimo: int = 1, maximo: int = 200) -> str:
    valor = _limpo(f.texto(nome))
    if not minimo <= len(valor) <= maximo:
        if minimo <= 1:
            erros.append(f"Preencha {rotulo} (até {maximo} letras).")
        else:
            erros.append(f"Preencha {rotulo} com {minimo} a {maximo} letras.")
    return valor


class TelasDoAdmin:
    def __init__(self, t: Telas) -> None:
        self.t = t

    def rotas(self) -> list[BaseRoute]:
        def r(caminho: str, funcao: Any, *metodos: str) -> Route:
            return Route(caminho, funcao, methods=list(metodos or ("GET",)))

        return [
            r("/admin", self.inicio),
            r("/admin/aplicativos", self.aplicativos),
            r("/admin/aplicativos", self.habilitar_aplicativo, "POST"),
            r("/admin/aplicativos/{aplicativo_id:uuid}/reativar", self.reativar_aplicativo, "POST"),
            r("/admin/sistemas", self.sistemas),
            r("/admin/sistemas", self.cadastrar_sistema, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}", self.sistema),
            r("/admin/sistemas/{sistema_id:uuid}/sondar", self.sondar, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/declaracoes", self.ler_declaracoes, "POST"),
            rota_com_teto(
                "/admin/sistemas/{sistema_id:uuid}/revisao_servidor", self.revisar_servidor, TETO_FORMULARIO_CONCLUSAO
            ),
            r("/admin/sistemas/{sistema_id:uuid}/ferramenta", self.ferramenta),
            r("/admin/sistemas/{sistema_id:uuid}/ferramenta", self.revisar, "POST"),
            r("/admin/modelos", self.modelos),
            r("/admin/modelos", self.criar_modelo, "POST"),
            r("/admin/webhooks", self.webhooks),
            r("/admin/webhooks", self.cadastrar_webhook, "POST"),
            r("/admin/webhooks/{destino_id:uuid}/situacao", self.mudar_webhook, "POST"),
            r("/admin/extrato", self.extrato_alheio_pedido),
            r("/admin/extrato", self.abrir_extrato_alheio, "POST"),
            r("/admin/extrato/{pessoa_id:uuid}", self.extrato_alheio),
        ]

    @tela
    async def inicio(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return self.t.pagina(pedido, "admin/inicio.html")

    # --- Aplicativos ---

    async def _pagina_aplicativos(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            aplicativos = await consultas.todos_aplicativos(c)
            pausados = await consultas.aplicativos_pausados(c)
            intervalo = await consultas.intervalo_que_pode_faltar(c) if pausados else None
            _, saida = await consultas.estado_das_portas(c)
        return self.t.pagina(
            pedido,
            "admin/aplicativos.html",
            {
                "aplicativos": aplicativos,
                "pausados": pausados,
                "intervalo": intervalo,
                "saida_fechada": saida is None or not saida.aberta,
                "erros": erros or [],
                "valores": valores or {},
            },
            status=400 if erros else 200,
        )

    @tela
    async def aplicativos(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_aplicativos(pedido)

    @tela
    async def habilitar_aplicativo(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        client_id = f.texto("client_id")
        if not ID_SIMPLES.match(client_id):
            erros.append("O client_id usa só letras, números e . _ : - (até 200).")
        nome = _texto(f, "nome", "o nome", erros, maximo=120)
        familia = f.texto("familia").lower()
        if not FAMILIA.match(familia):
            erros.append("A família usa letras minúsculas, números e - (por exemplo: claude-code, goose).")
        tipo = f.texto("tipo", "assistente")
        if tipo == "programa":
            erros.append(
                "Programa que roda sozinho tem cadastro próprio, com responsável e substituto. Use a tela Programas."
            )
        elif tipo != "assistente":
            erros.append("Escolha o tipo do aplicativo.")
        if not erros:
            async with self.t.engine.begin() as c:
                if await consultas.aplicativo_existe(c, client_id):
                    erros.append("Já existe um aplicativo com este client_id.")
                else:
                    aplicativo_id = await repositorio.habilitar_aplicativo(
                        c, client_id=client_id, nome=nome, familia=familia, tipo="assistente"
                    )
                    await self.t.extrato(
                        c,
                        "admin_aplicativo_habilitado",
                        {"aplicativo": nome, "quem": sessao.quem},
                        pessoa_id=sessao.pessoa.id,
                        aplicativo_id=aplicativo_id,
                        dados={"client_id": client_id, "familia": familia},
                    )
                    return ver_depois("/telas/admin/aplicativos", "aplicativo_habilitado")
        return await self._pagina_aplicativos(pedido, erros, dict(f.como_dict()))

    @tela
    async def reativar_aplicativo(self, pedido: Request) -> Response:
        """Reativa um aplicativo assistente que a conferencia do banco na partida pausou. So o ADMIN, aqui.

        A pausa do aplicativo (``aplicativos.situacao``) nao sai sozinha, nem quando o ADMIN reabre a porta de
        saida. O UPDATE so vale se o aplicativo ainda estiver pausado e for assistente: dois cliques ao mesmo tempo
        registram uma vez so, e um programa volta pela tela do programa (``telas/admin_programas.py``). As acoes
        que ja pararam continuam paradas: so os pedidos novos voltam a passar.
        """
        sessao = await self.t.exigir_admin(pedido)
        await self.t.formulario(pedido, sessao)
        aplicativo_id: uuid.UUID = pedido.path_params["aplicativo_id"]
        async with self.t.engine.begin() as c:
            voltou = await consultas.reativar_assistente(c, aplicativo_id)
            if voltou is not None:
                await self.t.extrato(
                    c,
                    "admin_aplicativo_reativado",
                    {"aplicativo": voltou.nome, "quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    dados={"client_id": voltou.client_id},
                )
        if voltou is None:
            return ver_depois("/telas/admin/aplicativos", "aplicativo_nada_mudou")
        log.info("telas_admin_aplicativo_reativado")
        return ver_depois("/telas/admin/aplicativos", "aplicativo_reativado")

    # --- Sistemas e sondagem ---

    async def _pagina_sistemas(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            sistemas = await consultas.todos_sistemas(c)
        return self.t.pagina(
            pedido,
            "admin/sistemas.html",
            {
                "sistemas": sistemas,
                "origens": paginas.ORIGENS,
                "terceiros_ligados": bool(self.t.p.FEATURE_PHASE3_THIRD_PARTY),
                "erros": erros or [],
                "valores": valores or {},
            },
            status=400 if erros else 200,
        )

    @tela
    async def sistemas(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_sistemas(pedido)

    def _endereco_aceito(self, endereco: str, origem: str = "proprio") -> str | None:
        """None se o endereco serve; senao, a frase da recusa (``problema_do_endereco``)."""
        return problema_do_endereco(endereco, origem=origem, ambiente=str(self.t.p.APP_ENV))

    @tela
    async def cadastrar_sistema(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nome = _texto(f, "nome", "o nome do sistema", erros, maximo=80)
        origem = f.texto("origem")
        if origem not in paginas.ORIGENS:
            erros.append(ORIGEM_FALTA)
        endereco = f.texto("endereco")
        if problema := self._endereco_aceito(endereco, origem if origem in paginas.ORIGENS else "proprio"):
            erros.append(problema)
        tipo_chave = f.texto("tipo_chave")
        if tipo_chave not in TIPOS_CHAVE:
            erros.append("Escolha o tipo de chave do sistema.")
        elif origem == "terceiro" and tipo_chave not in paginas.TIPOS_CHAVE_TERCEIRO:
            erros.append(CHAVE_DE_TERCEIRO)
        if erros:
            return await self._pagina_sistemas(pedido, erros, dict(f.como_dict(sem=("csrf", "chave_sondagem"))))
        terceiro = origem == "terceiro"
        try:
            async with self.t.engine.begin() as c:
                sistema_id = await repositorio.cadastrar_sistema(
                    c, nome=nome, endereco=endereco, tipo_chave=tipo_chave, origem=origem
                )
                await self.t.extrato(
                    c,
                    "admin_terceiro_cadastrado" if terceiro else "admin_sistema_cadastrado",
                    {"sistema": nome, "quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    sistema_id=sistema_id,
                    dados={"tipo_chave": tipo_chave, "origem": origem},
                )
        except IntegrityError:
            erros.append("Já existe um sistema com este nome.")
            return await self._pagina_sistemas(pedido, erros, dict(f.como_dict(sem=("csrf", "chave_sondagem"))))
        if terceiro:
            log.info("telas_admin_terceiro_cadastrado")
        return await self._sondar(sessao, sistema_id, nome, f.texto("chave_sondagem"), novo=True)

    async def _sondar(
        self, sessao: Sessao, sistema_id: uuid.UUID, nome: str, chave: str, *, novo: bool = False
    ) -> Response:
        """Pede a sondagem ao despachante e volta para a ficha do sistema, que explica o resultado."""
        corpo: dict[str, Any] = {"sistema_id": str(sistema_id)}
        if chave:
            corpo["chave_sondagem"] = chave
        aviso: str | None = None
        try:
            resposta = await self.t.despachante.post("/interno/sondar", corpo)
        except (httpx2.HTTPError, ValueError) as erro:
            log.warning("telas_sondagem_sem_resposta", erro=type(erro).__name__)
            desfecho = "o despachante nao respondeu"
            aviso = "despachante_mudo" if novo else "despachante_demorou_sondagem"
        else:
            desfecho = desfecho_no_extrato(resposta)
        async with self.t.engine.begin() as c:
            await self.t.extrato(
                c,
                "admin_sondagem_pedida",
                {"sistema": nome, "quem": sessao.quem, "desfecho": desfecho},
                pessoa_id=sessao.pessoa.id,
                sistema_id=sistema_id,
            )
        return ver_depois(f"/telas/admin/sistemas/{sistema_id}", aviso)

    async def _sistema_ou_404(self, pedido: Request) -> Any:
        async with self.t.engine.connect() as c:
            linha = await consultas.sistema(c, pedido.path_params["sistema_id"])
        if linha is None:
            raise Interrompe(self.t.erro(pedido, 404, "Sistema não encontrado", "Este sistema não está cadastrado."))
        return linha

    async def _pagina_sistema(
        self,
        pedido: Request,
        linha: Any,
        *,
        erros: list[str] | None = None,
        valores: dict[str, Any] | None = None,
        recusa: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> Response:
        """A ficha do sistema. ``recusa``: a conclusao da revisao do servidor que o despachante recusou (frase e
        ferramentas com problema), mostrada no cartao da revisao."""
        async with self.t.engine.connect() as c:
            vistas = await consultas.ferramentas_vistas(c, linha.id)
            detalhe = await consultas_terceiros.detalhe(c, linha.id)
            terceiro = await ficha_do_terceiro(c, linha) if linha.origem != "proprio" else None
        frase = paginas.frase_da_sondagem(
            detalhe.sondagem_desfecho if detalhe else None,
            linha.motivo,
            origem=str(linha.origem),
            versoes=list(detalhe.versoes_anunciadas or []) if detalhe else [],
        )
        if valores is None:
            valores = valores_da_conclusao(terceiro)
        return self.t.pagina(
            pedido,
            "admin/sistema.html",
            {
                "sistema": linha,
                "vistas": vistas,
                "detalhe": detalhe,
                "terceiro": terceiro,
                "frase_sondagem": frase,
                "sem_veredito": bool(detalhe and detalhe.sondagem_desfecho not in (None, "aceito", "versao")),
                "versao_certa": linha.versao_mcp == VERSAO_MCP,
                "terceiros_ligados": bool(self.t.p.FEATURE_PHASE3_THIRD_PARTY),
                "recusa": recusa,
                "erros": erros or [],
                "valores": valores,
            },
            status=status or (400 if erros else 200),
        )

    @tela
    async def sistema(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        linha = await self._sistema_ou_404(pedido)
        return await self._pagina_sistema(pedido, linha)

    @tela
    async def sondar(self, pedido: Request) -> Response:
        """Conferencia da versao de novo. Recusada (409) so num sistema desativado: em quarentena e aceita e nao muda
        a situacao (depois de mudar o endereco, por exemplo, a versao precisa ser conferida de novo)."""
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        if linha.situacao == "desativado":
            return self.t.erro(
                pedido,
                409,
                "Sistema desativado",
                "Este sistema está desativado. A conferência da versão não vale para ele.",
            )
        return await self._sondar(sessao, linha.id, linha.nome, f.texto("chave_sondagem"))

    @tela
    async def ler_declaracoes(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        if linha.situacao not in ("ativo", "quarentena"):
            return self.t.erro(
                pedido,
                409,
                "Falta a sondagem",
                "Só lemos as ferramentas de um sistema aceito na sondagem de versão.",
            )
        corpo: dict[str, Any] = {"sistema_id": str(linha.id)}
        if chave := f.texto("chave_sondagem"):
            corpo["chave_sondagem"] = chave
        resposta = await self.t.porta_interna("/interno/declaracoes", corpo)
        if not resposta.ok:
            titulo, mensagem = DECLARACOES_FALHA.get(resposta.motivo, DECLARACOES_FALHA[""])
            return self.t.erro(pedido, 503 if resposta.motivo in DECLARACOES_NOSSAS else 502, titulo, mensagem)
        quantas = len(resposta.dados.get("ferramentas") or [])
        async with self.t.engine.begin() as c:
            await self.t.extrato(
                c,
                "admin_declaracoes_lidas",
                {"sistema": linha.nome, "quem": sessao.quem, "quantas": quantas},
                pessoa_id=sessao.pessoa.id,
                sistema_id=linha.id,
            )
        return ver_depois(f"/telas/admin/sistemas/{linha.id}", "declaracoes_lidas")

    # --- Revisao do servidor de terceiros ---

    @tela
    async def revisar_servidor(self, pedido: Request) -> Response:
        """Conclusao da revisao do servidor pelo ADMIN: o despachante le o servidor de novo e grava a revisao (com a
        linha no extrato) ou recusa com o motivo. A tela so confere o formulario e escreve a frase."""
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        if linha.origem == "proprio":
            return self.t.erro(pedido, 409, "Revisão do servidor", SO_TERCEIRO)
        erros: list[str] = []
        # O texto que o banco nao guarda nao chega ao despachante: ``_texto`` troca os caracteres de controle
        # (o nulo tambem) por espaco, e cada nome marcado precisa ser um que o servidor declarou (o banco guardou).
        minimo, maximo = FORNECEDOR_LETRAS
        fornecedor = _texto(f, "fornecedor", FORNECEDOR_ROTULO, erros, minimo=minimo, maximo=maximo)
        async with self.t.engine.connect() as c:
            declaradas = {str(v.nome) for v in await consultas.ferramentas_vistas(c, linha.id)}
        publicar, recusas = conferir_publicar(f.lista("publicar"), declaradas)
        erros.extend(recusas)
        valores: dict[str, Any] = {"fornecedor": fornecedor, "publicar": publicar}
        if erros:
            return await self._pagina_sistema(pedido, linha, erros=erros, valores=valores)
        corpo = {
            "sistema_id": str(linha.id),
            "publicar": publicar,
            "fornecedor": fornecedor,
            "quem": sessao.quem,
            "pessoa_id": str(sessao.pessoa.id),
        }
        destino = f"/telas/admin/sistemas/{linha.id}"
        try:
            resposta = await self.t.despachante.post("/interno/revisao_servidor", corpo)
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as erro:
            # Nada chegou ao despachante: nada mudou.
            log.warning("telas_revisao_servidor_sem_resposta", erro=type(erro).__name__)
            return ver_depois(destino, "despachante_mudo_revisao")
        except httpx2.HTTPStatusError as erro:
            codigo = erro.response.status_code
            log.info("telas_revisao_servidor_recusada_pelo_despachante", status=codigo)
            if codigo == 400:
                invalido = {"frase": paginas.frase_da_revisao_recusada("pedido_invalido"), "ferramentas": []}
                return await self._pagina_sistema(pedido, linha, valores=valores, recusa=invalido, status=400)
            if codigo == 404:
                return self.t.erro(pedido, 404, "Sistema não encontrado", "Este sistema não está cadastrado.")
            return ver_depois(destino, "despachante_demorou_revisao")
        except (httpx2.HTTPError, ValueError) as erro:
            # Prazo ou queda depois de enviar: a revisao pode ter sido gravada. A ficha mostra o que vale.
            log.warning("telas_revisao_servidor_sem_resposta", erro=type(erro).__name__)
            return ver_depois(destino, "despachante_demorou_revisao")
        if resposta.get("revisado") is True:
            log.info("telas_admin_servidor_revisado")
            return ver_depois(destino, "servidor_revisado")
        recusa: dict[str, Any] = {
            "frase": paginas.frase_da_revisao_recusada(resposta.get("motivo")),
            "motivo": str(resposta.get("motivo") or ""),
            "ferramentas": _problemas(resposta.get("ferramentas")),
        }
        return await self._pagina_sistema(pedido, linha, valores=valores, recusa=recusa, status=409)

    # --- Revisao de cada ferramenta ---

    async def _pagina_ferramenta(
        self,
        pedido: Request,
        sistema: Any,
        nome: str,
        *,
        erros: list[str] | None = None,
        valores: dict[str, Any] | None = None,
    ) -> Response:
        async with self.t.engine.connect() as c:
            vista = await consultas.ferramenta_vista(c, sistema.id, nome)
            revisao = await consultas_terceiros.revisao_ativa(c, sistema.id, nome)
        if vista is None:
            raise Interrompe(
                self.t.erro(
                    pedido,
                    404,
                    "Ferramenta não encontrada",
                    "O sistema não declarou esta ferramenta na última leitura.",
                )
            )
        terceiro = sistema.origem != "proprio"
        definicao = dict(vista.definicao)
        entrada = definicao.get("inputSchema") or {}
        entrada = entrada if isinstance(entrada, dict) else {}
        saida = definicao.get("outputSchema") or {}
        campos_saida = sorted((saida.get("properties") or {}).keys()) if isinstance(saida, dict) else []
        campos_entrada = repositorio.campos_de_entrada(entrada)
        if valores is None:
            if revisao is not None:
                valores = {
                    "nome_vitrine": revisao.nome_vitrine,
                    "titulo": revisao.titulo or "",
                    "descricao": revisao.descricao,
                    "classe": revisao.classe,
                    "risco": revisao.risco,
                    "campos": list(revisao.campos_pessoais or []),
                    "destinatario": list(revisao.campos_destinatario or []),
                    "texto_de_fora": bool(revisao.texto_de_fora),
                    "descricoes": _descricoes_gravadas(revisao.esquema_entrada) if terceiro else {},
                }
            else:
                so_le = bool((definicao.get("annotations") or {}).get("readOnlyHint"))
                valores = {
                    "nome_vitrine": nome_vitrine_sugerido(str(sistema.nome), nome, terceiro=terceiro),
                    "titulo": "",
                    "descricao": "",
                    "classe": "le" if so_le else "muda",
                    "risco": "medio",
                    "campos": [],
                    "destinatario": [],
                    "texto_de_fora": False,
                    "descricoes": {},
                }
        if terceiro:
            valores["texto_de_fora"] = True
        previa, recusa_previa = (
            (_previa_de_terceiro(entrada, valores.get("descricoes") or {})) if terceiro else (None, None)
        )
        antes = None
        mudou = revisao is not None and revisao.impressao != vista.impressao
        if mudou and revisao is not None and isinstance(revisao.definicao_declarada, dict):
            antes = dict(revisao.definicao_declarada)
        outros = [c for c in valores.get("campos", []) if c not in campos_saida]
        return self.t.pagina(
            pedido,
            "admin/ferramenta.html",
            {
                "sistema": sistema,
                "terceiro": terceiro,
                "nome": nome,
                "definicao": definicao,
                "esquema_entrada": _json_legivel(entrada),
                "previa": previa,
                "recusa_previa": recusa_previa,
                "antes": antes,
                "antes_esquema": _json_legivel((antes or {}).get("inputSchema") or {}),
                "campos_saida": campos_saida,
                "campos_entrada": campos_entrada,
                "prefixo_descricao": PREFIXO_DESCRICAO_CAMPO,
                "descricao_maxima": esquema.DESCRICAO_DE_CAMPO_MAXIMA,
                "outros_campos": ", ".join(outros),
                "revisao": revisao,
                "mudou": mudou,
                "valores": valores,
                "erros": erros or [],
            },
            status=400 if erros else 200,
        )

    @tela
    async def ferramenta(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        sistema = await self._sistema_ou_404(pedido)
        return await self._pagina_ferramenta(pedido, sistema, pedido.query_params.get("nome", ""))

    @tela
    async def revisar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        sistema = await self._sistema_ou_404(pedido)
        terceiro = sistema.origem != "proprio"
        nome = f.texto("nome")
        erros: list[str] = []
        nome_vitrine = f.texto("nome_vitrine")
        if not NOME_VITRINE.match(nome_vitrine):
            erros.append("O nome na vitrine usa só letras sem acento, números e _ . - (até 128).")
        titulo = _texto(f, "titulo", "o título", erros, minimo=0, maximo=120)
        descricao = _texto(f, "descricao", "a descrição própria", erros, minimo=10, maximo=1000)
        classe = f.texto("classe")
        if classe not in CLASSES:
            erros.append("Escolha a classe: só lê, muda algo ou muda algo com volta.")
        risco = f.texto("risco")
        if risco not in RISCOS:
            erros.append("Escolha o risco: baixo, médio ou alto.")
        campos = f.lista("campos") + [c.strip() for c in f.texto("outros_campos").split(",") if c.strip()]
        campos = list(dict.fromkeys(campos))
        if len(campos) > 30 or not all(NOME_CAMPO.match(c) for c in campos):
            erros.append("Os campos pessoais usam só letras sem acento, números e _ . - (até 30 campos).")
        async with self.t.engine.connect() as c:
            vista = await consultas.ferramenta_vista(c, sistema.id, nome)
        if vista is None:
            return await self._pagina_ferramenta(pedido, sistema, nome)
        if terceiro and len(nome) > TETO_NOME_FERRAMENTA:
            erros.insert(0, NOME_LONGO_DEMAIS)
        destinatario = list(dict.fromkeys(f.lista("destinatario")))
        de_entrada = repositorio.campos_de_entrada(dict(vista.definicao).get("inputSchema") or {})
        if len(destinatario) > 30 or any(d not in de_entrada for d in destinatario):
            erros.append("Marque como destinatário só campos que a ferramenta pede (a lista desta página).")
            destinatario = [d for d in destinatario if d in de_entrada][:30]
        # Num terceiro, a resposta conta sempre como texto de fora (o banco tambem forca); num proprio, o ADMIN marca.
        texto_de_fora = terceiro or f.texto("texto_de_fora") == TEXTO_DE_FORA_SIM
        descricoes: dict[str, str] = {}
        if terceiro:
            for campo in de_entrada:
                texto_campo = _limpo(f.texto(PREFIXO_DESCRICAO_CAMPO + campo))
                if len(texto_campo) > esquema.DESCRICAO_DE_CAMPO_MAXIMA:
                    erros.append(erro_descricao_longa(campo))
                elif texto_campo:
                    descricoes[campo] = texto_campo
        valores = {
            "nome_vitrine": nome_vitrine,
            "titulo": titulo,
            "descricao": descricao,
            "classe": classe,
            "risco": risco,
            "campos": campos,
            "destinatario": destinatario,
            "texto_de_fora": texto_de_fora,
            "descricoes": descricoes,
        }
        if erros:
            return await self._pagina_ferramenta(pedido, sistema, nome, erros=erros, valores=valores)
        try:
            async with self.t.engine.begin() as c:
                ferramenta_id = await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=sistema.id,
                    definicao_declarada=dict(vista.definicao),
                    nome_vitrine=nome_vitrine,
                    descricao=descricao,
                    classe=classe,
                    revisado_por=sessao.quem,
                    risco=risco,
                    titulo=titulo or None,
                    campos_pessoais=campos,
                    campos_destinatario=destinatario,
                    descricoes_campos=descricoes or None,
                    texto_de_fora=texto_de_fora,
                )
                await self.t.extrato(
                    c,
                    "admin_ferramenta_revisada",
                    {
                        "ferramenta": nome_vitrine,
                        "sistema": sistema.nome,
                        "quem": sessao.quem,
                        "classe": classe,
                        "risco": risco,
                    },
                    pessoa_id=sessao.pessoa.id,
                    sistema_id=sistema.id,
                    ferramenta=nome_vitrine,
                    dados={
                        "ferramenta_id": str(ferramenta_id),
                        "nome_original": nome,
                        "campos_pessoais": campos,
                        "campos_destinatario": destinatario,
                        "origem": str(sistema.origem),
                        "texto_de_fora": texto_de_fora,
                        "campos_com_descricao": sorted(descricoes),
                    },
                )
                await self._encerrar_limites_sem_os_campos(c, sistema.id, nome, nome_vitrine, destinatario)
        except EsquemaInvalido as erro:  # inclui esquema.EsquemaRecusado (a recusa de terceiro, com o motivo)
            erros.append(frase_do_esquema(erro))
        except esquema.CampoDesconhecido as erro:
            erros.append(erro_campos_desconhecidos(erro.campos))
        except repositorio.CampoForaDoEsquema:
            erros.append("Marque como destinatário só campos que a ferramenta pede (a lista desta página).")
        except IntegrityError:
            erros.append("Já existe outra ferramenta valendo com este nome na vitrine. Escolha outro nome.")
        except ValueError:  # descricao de campo longa demais (esquema.com_descricoes)
            erros.append(f"Cada descrição de campo vai até {esquema.DESCRICAO_DE_CAMPO_MAXIMA} letras.")
        if erros:
            return await self._pagina_ferramenta(pedido, sistema, nome, erros=erros, valores=valores)
        # Num terceiro, a ferramenta so vai a vitrine e aos modelos depois da conclusao da revisao do servidor.
        aviso = "ferramenta_revisada_terceiro" if terceiro else "ferramenta_revisada"
        return ver_depois(f"/telas/admin/sistemas/{sistema.id}", aviso)

    async def _encerrar_limites_sem_os_campos(
        self, c: AsyncConnection, sistema_id: uuid.UUID, nome: str, nome_vitrine: str, campos: list[str]
    ) -> int:
        """Na transacao da revisao: os limites "so estes" que nao conferem um campo marcado agora terminam, e o
        que eles liberaram e ainda espera a hora de sair e cancelado. Cada pessoa ve no proprio extrato."""
        encerrados = await consultas.encerrar_limites_sem_os_campos(
            c, sistema_id=sistema_id, nome_original=nome, campos=campos, motivo=MOTIVO_CAMPOS_MUDARAM
        )
        for limite in encerrados:
            canceladas = await limites.cancelar_liberadas(c, limite.id, MOTIVO_CANCELADA_CAMPOS, quem="admin")
            await self.t.extrato(
                c,
                "limite_encerrado",
                {"ferramenta": nome_vitrine, "motivo": MOTIVO_CAMPOS_MUDARAM},
                pessoa_id=limite.pessoa_id,
                aplicativo_id=limite.aplicativo_id,
                sistema_id=limite.sistema_id,
                ferramenta=nome_vitrine,
                dados={"limite_id": str(limite.id), "canceladas": canceladas, "por": "admin"},
            )
        if encerrados:
            log.info("telas_limites_encerrados_pela_revisao", limites=len(encerrados))
        return len(encerrados)

    # --- Modelos de ordem de servico ---

    async def _pagina_modelos(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, Any] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            modelos = await consultas.todos_modelos(c)
            ferramentas = await consultas_terceiros.ferramentas_para_modelos(c)
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
        return self.t.pagina(
            pedido,
            "admin/modelos.html",
            {
                "modelos": modelos,
                "ferramentas": ferramentas,
                "maximo_dias": maximo,
                "so_leitura": not self.t.p.FEATURE_PHASE2_ACT,
                "terceiros_ligados": bool(self.t.p.FEATURE_PHASE3_THIRD_PARTY),
                "ha_terceiros": any(f.origem != "proprio" for f in ferramentas),
                "fora_da_revisao": MODELO_FORA_DA_REVISAO,
                "erros": erros or [],
                "valores": valores or {"ferramentas": []},
            },
            status=400 if erros else 200,
        )

    @tela
    async def modelos(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_modelos(pedido)

    @tela
    async def criar_modelo(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nome = _texto(f, "nome", "o nome do modelo", erros, maximo=80)
        descricao = _texto(f, "descricao", "a explicação para a pessoa", erros, minimo=10, maximo=500)
        escolhidas = [i for i in (uuid_ou_none(v) for v in f.lista("ferramentas")) if i is not None]
        valores: dict[str, Any] = dict(f.como_dict()) | {"ferramentas": [str(i) for i in escolhidas]}
        async with self.t.engine.begin() as c:
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
            try:
                dias = int(f.texto("validade_dias"))
            except ValueError:
                dias = 0
            if not 1 <= dias <= maximo:
                erros.append(f"O prazo máximo do modelo vai de 1 a {maximo} dias (regra da política).")
            ativas = {linha.id: linha for linha in await consultas_terceiros.ferramentas_para_modelos(c)}
            ferramentas = [ativas[i] for i in escolhidas if i in ativas]
            if not ferramentas or len(ferramentas) != len(escolhidas):
                erros.append("Escolha ao menos uma ferramenta revisada da lista.")
            if any(not x.publicada for x in ferramentas):
                # Com a chave da Fase 3 desligada tambem: o ADMIN prepara o modelo para as pessoas de teste, mas so
                # com o que a revisao do servidor vigente publicou (a vitrine esconderia o resto de todo jeito).
                erros.append(MODELO_FORA_DA_REVISAO + " Conclua a revisão do servidor com ela marcada, ou tire-a.")
            if not self.t.p.FEATURE_PHASE2_ACT and any(x.classe != "le" for x in ferramentas):
                erros.append("Nesta fase, a ordem de serviço só pode ter ferramentas que só leem.")
            if not erros and await consultas.modelo_existe(c, nome):
                erros.append("Já existe um modelo com este nome.")
            if not erros:
                modelo_id = await repositorio.criar_modelo_ordem(
                    c,
                    nome=nome,
                    descricao=descricao,
                    ferramentas=[(x.sistema_id, x.nome_original) for x in ferramentas],
                    validade_dias=dias,
                    criado_por=sessao.quem,
                )
                await self.t.extrato(
                    c,
                    "admin_modelo_criado",
                    {"modelo": nome, "quem": sessao.quem, "quantas": len(ferramentas)},
                    pessoa_id=sessao.pessoa.id,
                    dados={"modelo_id": str(modelo_id), "validade_dias": dias},
                )
                return ver_depois("/telas/admin/modelos", "modelo_criado")
        return await self._pagina_modelos(pedido, erros, valores)

    # --- Destinos de webhook ---

    async def _pagina_webhooks(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            destinos = await consultas.destinos_webhook(c)
        return self.t.pagina(
            pedido,
            "admin/webhooks.html",
            {"destinos": destinos, "erros": erros or [], "valores": valores or {}},
            status=400 if erros else 200,
        )

    @tela
    async def webhooks(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_webhooks(pedido)

    @tela
    async def cadastrar_webhook(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nome = _texto(f, "nome", "o nome do destino", erros, maximo=80)
        url = f.texto("url")
        partes = urlsplit(url)
        if (
            partes.scheme != "https"
            or not partes.hostname
            or partes.username is not None
            or partes.password is not None
            or len(url) > 500
            or CONTROLE.search(url)
        ):
            erros.append("O endereço do webhook precisa começar com https:// (sem usuário e senha).")
        eventos = [e.strip().lower() for e in f.texto("eventos").split(",") if e.strip()]
        if len(eventos) > 20 or not all(NOME_EVENTO.match(e) for e in eventos):
            erros.append("Os eventos usam letras minúsculas, números e _ (separados por vírgula).")
        if not erros:
            try:
                async with self.t.engine.begin() as c:
                    destino_id = await consultas.cadastrar_destino(
                        c, nome=nome, url=url, eventos=eventos, criado_por=sessao.quem
                    )
                    await self.t.extrato(
                        c,
                        "admin_webhook_cadastrado",
                        {"destino": nome, "quem": sessao.quem},
                        pessoa_id=sessao.pessoa.id,
                        dados={"destino_id": str(destino_id), "eventos": eventos},
                    )
            except IntegrityError:
                erros.append("Já existe um destino com este nome.")
            else:
                return ver_depois("/telas/admin/webhooks", "destino_cadastrado")
        return await self._pagina_webhooks(pedido, erros, dict(f.como_dict()))

    @tela
    async def mudar_webhook(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        ativo = f.texto("ativo") == "sim"
        async with self.t.engine.begin() as c:
            nome = await consultas.mudar_destino(c, pedido.path_params["destino_id"], ativo)
            if nome is not None:
                await self.t.extrato(
                    c,
                    "admin_webhook_mudou",
                    {"destino": nome, "situacao": "ligado" if ativo else "desligado", "quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    dados={"destino_id": str(pedido.path_params["destino_id"])},
                )
        return ver_depois("/telas/admin/webhooks", "destino_mudou" if nome else None)

    # --- Extrato de outra pessoa: so com motivo, e o acesso vira linha no extrato dela ---

    async def _pagina_pedido_extrato(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            pessoas = await consultas.pessoas(c)
        valores = valores or {}
        if "pessoa" not in valores and pedido.query_params.get("pessoa"):
            valores["pessoa"] = pedido.query_params["pessoa"]
        return self.t.pagina(
            pedido,
            "admin/extrato_pedido.html",
            {"pessoas": pessoas, "erros": erros or [], "valores": valores},
            status=400 if erros else 200,
        )

    @tela
    async def extrato_alheio_pedido(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_pedido_extrato(pedido)

    @tela
    async def abrir_extrato_alheio(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        alvo_id = uuid_ou_none(f.texto("pessoa"))
        motivo = _texto(f, "motivo", "o motivo", erros, minimo=10, maximo=300).rstrip(" .")
        async with self.t.engine.begin() as c:
            alvo = await consultas.pessoa_por_id(c, alvo_id) if alvo_id else None
            if alvo is None:
                erros.append("Escolha a pessoa.")
            if not erros and alvo is not None:
                await self.t.extrato(
                    c,
                    "extrato_alheio_aberto",
                    {"quem": sessao.quem, "motivo": motivo},
                    pessoa_id=alvo.id,
                    dados={"admin_id": str(sessao.pessoa.id)},
                )
                log.info("telas_extrato_alheio_aberto")
                return RedirectResponse(f"/telas/admin/extrato/{alvo.id}", status_code=303)
        return await self._pagina_pedido_extrato(pedido, erros, dict(f.como_dict()))

    @tela
    async def extrato_alheio(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        alvo_id: uuid.UUID = pedido.path_params["pessoa_id"]
        pagina = numero_pagina(pedido.query_params.get("pagina"))
        async with self.t.engine.connect() as c:
            alvo = await consultas.pessoa_por_id(c, alvo_id)
            liberado = alvo is not None and await consultas.acesso_alheio_recente(
                c, alvo_id, sessao.pessoa.id, self.t.cfg.janela_extrato_alheio_minutos
            )
            if alvo is None or not liberado:
                destino = "/telas/admin/extrato?" + urlencode({"pessoa": str(alvo_id), "aviso": "motivo_exigido"})
                return RedirectResponse(destino, status_code=303)
            linhas, ha_mais = await consultas.linhas_extrato(c, alvo_id, pagina)
        return self.t.pagina(
            pedido,
            "extrato.html",
            {
                "linhas": linhas,
                "pagina": pagina,
                "ha_mais": ha_mais,
                "alheio": alvo,
                "base": f"/telas/admin/extrato/{alvo_id}",
            },
        )
