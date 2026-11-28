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

Selos de nivel 2 e 3 (Fase 4, desenho 7.1):

- a ficha do sistema tem o cartao "Selo" (``cartao_do_selo``): o selo de agora com o motivo legivel, lido so de
  ``selo_vigente`` (``vigente.ler``, nunca ``sistemas.selo``), a declaracao de teste, a chave de teste, o pedido do
  teste (com o numero estimado de chamadas e de escritas reais no alvo, pela conta da bateria: critica B5), o historico
  e a retirada. Se a leitura do selo falhar, a ficha abre sem o cartao: o selo nunca para a tela;
- salvar a declaracao, pedir o teste e retirar o selo pedem CSRF e login refeito na hora (``Telas.login_recente``;
  sem ele, 403 e o botao "Entrar de novo agora"). A tela confere antes (``selo.declaracao``) e o banco confere de novo
  (os gatilhos da 0007); a recusa do banco vira frase da tela (``RECUSAS_DO_BANCO``). Com outro teste esperando ou
  rodando, o pedido volta com 409. Num servidor de terceiros, a declaracao e o pedido ficam recusados ate o G7 (409);
- o relatorio de cada teste (``relatorio_do_teste``) mostra, por ferramenta, os casos, o resultado e por que, o que
  nao testamos (sem IDs do rascunho independente: critica B6), a declaracao usada, o JSON para baixar e o rodape do
  desenho 3.4. Nada do que o sistema respondeu aparece: so o vocabulario do contrato, contagens e horas;
- a lista de sistemas tem a coluna "Selo" (``selos_da_lista``), com "vence em {d} dias" no prazo do aviso das rotinas,
  contado pelo relogio do banco;
- os avisos novos ficam em ``AVISOS_ADMIN`` (``paginas.AVISOS`` de reserva). Toda tela do selo diz que ele nao e
  promessa (``SELO_NAO_GARANTE``).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import socket
import uuid
from dataclasses import dataclass, replace
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import httpx2
from markupsafe import Markup, escape
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.cadastro import esquema, repositorio
from mcp_claro.cadastro.quarentena import MOTIVO_QUARENTENA, MOTIVO_QUARENTENA_VERSAO
from mcp_claro.config.politica import dias_de_aviso_do_selo, valor_inicial
from mcp_claro.config.settings import Ambiente
from mcp_claro.entrada.argumentos import ESQUEMA_FUNDO_DEMAIS, EsquemaInvalido
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras.escondidos import marcar_escondidos
from mcp_claro.saida.quarentena import MOTIVO_SEM_RESPOSTA
from mcp_claro.selo import contrato, vigente
from mcp_claro.selo import declaracao as declaracao_do_selo
from mcp_claro.telas import consultas, consultas_terceiros, limites, paginas, seguranca
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
FERRAMENTA_MUDOU: Final = "A ferramenta mudou depois que esta página abriu. Confira a definição nova e revise de novo."
"""A revisao pela tela recusa (409) quando a impressao que a pagina mostrou (``impressao_vista``) nao e mais a da
ultima leitura: uma leitura das declaracoes no meio gravou outra definicao. Nada e gravado (R6b-3)."""
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


# Falha ao ler as declaracoes (``/interno/declaracoes``): 503 com motivo quando a falha e do MCP-CLARO
# (``despachante.app``: ``DeclaracoesNaoConferidas`` e ``CONFERENCIA_NAO_FEITA``), cada motivo com a sua frase; o
# 502 ``declaracoes_indisponiveis`` (o servidor nao entregou) e o resto ficam com a frase comum (chave ``""``).
DECLARACOES_FALHA: Final[dict[str, tuple[str, str]]] = {
    "sala_fora": (
        "A sala de visitas não está disponível",
        "A sala de visitas não está disponível agora. Nada foi pedido ao servidor. Tente de novo daqui a pouco.",
    ),
    # A sala cheia dita pelo processo pai dela (contrato K7): nada foi a um filho nem ao servidor.
    "sala_sem_vaga": (
        "A sala de visitas estava cheia",
        "Todas as vagas da sala de visitas estavam ocupadas. Nada foi pedido ao servidor. Tente de novo daqui a pouco.",
    ),
    "chave_indisponivel": (
        "O cofre não abriu a chave",
        "Não deu para abrir no cofre a chave de leitura das declarações. Tente de novo daqui a pouco.",
    ),
    # O banco ou o grupo de conexoes do despachante falhou (contrato K5). Pode ter sido antes ou depois de pedir a
    # lista ao servidor: a frase nao diz que nada foi pedido.
    "conferencia_nao_feita": (
        "O MCP-CLARO não conseguiu usar o banco de dados",
        "O MCP-CLARO não conseguiu usar o próprio banco de dados agora. A falha é do MCP-CLARO, não do servidor."
        " Tente de novo daqui a pouco.",
    ),
    "": (
        "Não conseguimos ler",
        "O despachante não conseguiu ler as ferramentas deste sistema. Confira a chave e tente de novo.",
    ),
}
DECLARACOES_NOSSAS: Final = frozenset(DECLARACOES_FALHA) - {""}
"""Os motivos de falha do MCP-CLARO, que a tela responde com 503 (e nunca com "Confira a chave")."""


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
    """ "O que o aplicativo vai ver": o esquema limpo, com as descricoes do ADMIN, sem o ``$schema`` da raiz e com
    nomes neutros nas definicoes (``esquema.nomes_neutros``: os mesmos nomes que a revisao grava e a vitrine mostra,
    qualquer que seja a ordem das chaves, porque a conta segue a ordem do banco). Antes, ``conferir_documento``, que
    nao usa recursao e para no teto: um esquema enorme ou fundo demais mostra a frase, nunca trava a tela. Devolve
    (previa, frase da recusa)."""
    try:
        esquema.conferir_documento(entrada)
        limpo = esquema.limpar_textos(entrada)
    except EsquemaInvalido as erro:
        return None, frase_do_esquema(erro)
    # Campo que nao existe ou descricao longa: a previa fica sem as descricoes, e a revisao recusa com a frase.
    with contextlib.suppress(ValueError):
        limpo = esquema.com_descricoes(limpo, descricoes)
    try:
        limpo = esquema.nomes_neutros(limpo)
    except EsquemaInvalido as erro:
        return None, frase_do_esquema(erro)
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


# --- Fase 4: o selo de nivel 2 e 3 (cartao "Selo", pedido de teste, relatorio, retirada) ---

AVISOS_ADMIN: Final[dict[str, str]] = {
    "selo_declaracao_salva": (
        "Pronto. A declaração de teste foi salva (versão {versao}). O teste usa sempre a mais nova."
    ),
    "selo_teste_pedido": "Pronto. O teste foi pedido; o resultado aparece aqui quando terminar.",
    "selo_retirado": "Pronto. O selo foi retirado; o sistema volta ao nível 1.",
    "selo_chave_guardada": (
        "Pronto. A chave de teste do selo foi entregue ao cofre. Ela não aparece mais nas telas e fica separada da"
        " chave de produção do sistema."
    ),
}
"""Os avisos das telas do ADMIN da Fase 4 (com acento). A URL leva so o codigo; a pagina procura aqui e depois em
``paginas.AVISOS``. ``selo_declaracao_salva`` ganha a versao da declaracao mais nova na hora de mostrar."""
SELO_DECLARACAO_SALVA_SEM_VERSAO: Final = "Pronto. A declaração de teste foi salva. O teste usa sempre a mais nova."

SELO_NAO_GARANTE: Final = "O selo diz como o sistema respondeu no teste; não garante o que ele fará depois."
"""A frase que toda tela do selo leva (desenho 13.2, "Leitura como certificacao")."""
SELO_RODAPE: Final = (
    "Este relatório é do MCP-CLARO. Alguns casos adaptam requisitos de um rascunho independente de escrita segura;"
    " veja o contrato. Ele não é uma declaração de conformidade desse rascunho."
)
"""O rodape do relatorio do teste (desenho 3.4), na pagina e no JSON."""
SELO_CONFIRMO: Final = "Confirmo que o alvo declarado é de teste: a bateria vai fazer escritas reais nele."
SELO_NOTA_DO_ALVO: Final = (
    "O MCP-CLARO só escreve no alvo que você declarou e que se declara de teste, e só com os argumentos declarados."
)
"""A nota da declaracao (critica A4.6): a escrita so no alvo, com as condicoes ditas."""
SELO_ATENCAO_DO_ALVO: Final = (
    "A bateria faz escritas reais no alvo de teste. O MCP-CLARO recusa um alvo que seja a produção de algum sistema do"
    " cadastro e para se o alvo não se declarar de teste, mas não sabe, sozinho, se o alvo é mesmo de teste. Confira"
    " o endereço com a equipe do sistema antes de salvar."
)
"""O destaque do risco "A bateria faz escritas reais" (desenho 13.2): depende de gente declarar certo."""
SELO_PEDE_LOGIN: Final = (
    "Salvar a declaração de teste, pedir o teste e retirar o selo pedem um login refeito na hora, como cuidado a mais."
)
SELO_LOGIN_REFEITO: Final = "Para isto, entre de novo agora, no botão “Entrar de novo agora”. Nada foi feito."
SELO_SEM_CONFIRMO: Final = "Marque a caixa que confirma que o alvo declarado é de teste. Nada foi salvo."
SELO_SEM_DECLARACAO: Final = "Salve a declaração de teste e a chave de teste antes de pedir o teste."
SELO_TESTE_JA_RODANDO: Final = "Já há um teste deste sistema rodando. Espere o resultado."
SELO_NIVEL_FORA: Final = "Escolha o teste de nível 2 ou o de nível 3."
SELO_NADA_A_RETIRAR: Final = "Este sistema não tem selo de nível 2 ou 3 agora. Nada foi retirado."
SELO_MOTIVO_DA_RETIRADA: Final = "Escreva por que retirar o selo (de 1 a 500 letras). Nada foi retirado."
SELO_SO_POR_PESSOA: Final = (
    "O selo de nível 2 ou 3 é só para sistema com chave por pessoa: a consulta e o aviso usam a chave de quem pediu"
    " a ação."
)
SELO_NAO_LIDO: Final = "Não deu para ler o selo deste sistema agora. Tente de novo daqui a pouco."
SELO_RECUSA_DO_BANCO: Final = "O banco de dados recusou o pedido. Nada foi gravado."
MOTIVO_MAXIMO_DA_RETIRADA: Final = 500
"""O texto do ADMIN na retirada (``selos_retirados.detalhe``: de 1 a 500 letras)."""
TRAVA_DAS_INFORMACOES: Final = "mcp_claro.selo.informacoes"
"""A trava consultiva das respostas do sistema, por sistema (0007, ``informacoes_sistema_nasce``:
``pg_advisory_xact_lock(hashtext('mcp_claro.selo.informacoes'), hashtext(NEW.sistema_id::text))``). A retirada pelo
ADMIN a toma antes de ler o selo: duas retiradas ao mesmo tempo, ou a do ADMIN e a automatica do despachante (pela
contradicao ou pelas consultas sem resposta, que gravam ``informacoes_sistema`` na mesma transacao), entram uma depois
da outra, e a segunda ve o selo ja retirado. Sem ela, as duas liam o nivel 2 e gravavam duas retiradas do mesmo
teste (duas linhas ``selo_retirado`` e o contador ``selo_retiradas`` duas vezes)."""
TETO_FORMULARIO_DECLARACAO: Final = TetoFormulario(
    tamanho=3 * contrato.TETO_DECLARACAO_BYTES + _FOLGA_CSRF, campos=8, campos_demais=seguranca.CAMPOS_DEMAIS
)
"""O formulario da declaracao de teste: o JSON de ate ``contrato.TETO_DECLARACAO_BYTES`` (cada byte vira ate tres no
formulario, ``%XX``), a caixa e o CSRF. A conta de bytes do JSON e de ``declaracao.ler_texto``, com a frase da tela."""

RECUSAS_DO_BANCO: Final[tuple[tuple[str, str], ...]] = (
    (
        "a bateria nao escreve na producao",
        "O alvo de teste é o endereço de produção de um sistema do cadastro. A bateria faz escritas reais no alvo: ele"
        " nunca pode ser a produção. Nada foi gravado.",
    ),
    ("escrito de um jeito so", "O nome do alvo de teste não está escrito de um jeito só. Nada foi gravado."),
    ("So um ADMIN ativo", "Só um ADMIN ativo salva a declaração, pede o teste ou retira o selo. Nada foi gravado."),
    (
        "declaracao de teste mais recente",
        "Outra declaração de teste foi salva enquanto esta página estava aberta. Confira a versão mais nova e peça de"
        " novo. Nada foi gravado.",
    ),
    (
        "sistema ativo na versao 2026-07-28",
        "O teste do selo só vale para sistema ativo, que fala a versão 2026-07-28 do MCP. Nada foi gravado.",
    ),
    ("chave por pessoa", SELO_SO_POR_PESSOA + " Nada foi gravado."),
    ("pedidos_teste_selo_um_por_vez", SELO_TESTE_JA_RODANDO),
    (
        "So se retira o selo vigente",
        "O selo deste sistema mudou enquanto esta página estava aberta e já não é de nível 2 ou 3. Nada foi retirado.",
    ),
    ("Diga por que retirar o selo", SELO_MOTIVO_DA_RETIRADA),
)
"""As recusas dos gatilhos da 0007 (sem acento, no banco), pelo trecho da mensagem, com o texto da tela. O banco
confere de novo o que a tela ja conferiu; a frase so aparece quando algo mudou no meio."""

RESULTADOS_LEGIVEIS: Final[dict[str, str]] = {
    "cumpriu": "Cumpriu",
    "falhou": "Falhou",
    "nao_se_aplica": "Não se aplica",
    "nao_testado": "Não testado",
    "nao_observado": "Não observado",
}
"""Os veredictos do MCP-CLARO (``contrato.RESULTADOS``) na coluna "Resultado" do relatorio."""

RAZOES_LEGIVEIS: Final[dict[str, str]] = {
    "sem_condicao_declarada": "a declaração não traz o que este caso precisa",
    "condicao_nao_forcada": "a bateria não conseguiu provocar a situação deste caso",
    "sem_resposta": "o sistema não respondeu",
    "tentativas_excluidas": "todas as tentativas passaram do prazo",
    "interposicao": "depende da equipe do sistema",
    "fora_do_alvo": "fica fora do alvo de teste",
    "teto": "o teste chegou ao teto de chamadas",
    "sem_segundo_efeito": "nesta ferramenta, repetir não muda nada",
    "poucos_pares": "poucos pares ao mesmo tempo para observar",
    "declarado": "declarado pelo sistema, sem teste",
}
"""As razoes de ``contrato.RAZOES`` na coluna "Por quê" do relatorio."""

INTERRUPCOES_LEGIVEIS: Final[dict[str, str]] = {
    "despachante_caiu": "o despachante parou no meio do teste",
    "sistema_mudou": "o sistema ou a declaração mudou, ou o pedido já não valia quando chegou a vez dele",
    "prazo": "o teste passou do tempo máximo",
    "porta_fechada": "a porta de saída estava fechada",
    "cofre_trancado": "o cofre estava trancado",
    "sem_chave_de_teste": "falta a chave de teste do selo",
    "teto_de_chamadas": "a declaração pede mais chamadas que o teto",
    "erro_interno": "houve um erro do MCP-CLARO",
}
"""Por que um pedido de teste ficou ``interrompido`` (``contrato.MOTIVOS_DE_INTERRUPCAO``)."""

MODOS_LEGIVEIS: Final[dict[str, str]] = {
    "ambiente_de_teste": "no ambiente de teste do sistema",
    "ferramentas_de_teste": "pelas ferramentas de teste que o sistema declarou",
}
"""Onde o teste roda (``contrato.MODOS``), como a ficha da pessoa diz (desenho 7.1)."""

ESTADOS_DO_PEDIDO_LEGIVEIS: Final[dict[str, str]] = {
    "pedido": "esperando a vez",
    "rodando": "rodando",
    "terminado": "terminado",
    "interrompido": "interrompido",
}

ESCOPO_SISTEMA: Final = "O sistema inteiro"


def recusa_do_banco(erro: BaseException) -> str:
    """O texto da tela para uma recusa do banco (``RECUSAS_DO_BANCO``), ou a frase geral."""
    mensagem = str(getattr(erro, "orig", erro))
    for trecho, texto_da_tela in RECUSAS_DO_BANCO:
        if trecho in mensagem:
            return texto_da_tela
    return SELO_RECUSA_DO_BANCO


def aviso_da_pagina(codigo: str, *, versao: int | None = None) -> str | None:
    """O aviso de um codigo da URL: os da Fase 4 (``AVISOS_ADMIN``) e, de reserva, ``paginas.AVISOS``."""
    if codigo == "selo_declaracao_salva":
        if versao is None:
            return SELO_DECLARACAO_SALVA_SEM_VERSAO
        return AVISOS_ADMIN[codigo].format(versao=versao)
    return AVISOS_ADMIN.get(codigo) or paginas.AVISOS.get(codigo)


def casos_que_nao_aprovam(itens: Any, nivel: int) -> int:
    """Quantos itens falharam ou, num caso obrigatorio do nivel, ficaram sem aprovar (o mesmo ``{falhas}`` do
    extrato): a conta unica de ``contrato.casos_que_nao_aprovam``."""
    return contrato.casos_que_nao_aprovam(itens, nivel)


def dias_legiveis(dias: int) -> str:
    return "1 dia" if dias == 1 else f"{dias} dias"


def reprovado_legivel(casos: int) -> str:
    """O motivo ``reprovado`` do cartao "Selo", com a conta e as palavras do relatorio e do extrato
    (``casos_que_nao_aprovam``: os que falharam e os obrigatorios que ficaram sem aprovar, como ``nao_testado`` com
    ``sem_resposta``). Sem caso nenhum na conta, so "O último teste não foi aprovado." (nunca "0 casos")."""
    if casos <= 0:
        return vigente.MOTIVOS_LEGIVEIS["reprovado"]
    quantos = "1 caso falhou ou ficou sem teste" if casos == 1 else f"{casos} casos falharam ou ficaram sem teste"
    return f"O último teste não foi aprovado: {quantos}."


_MARCA_DO_NOME: Final = "\x00{}\x00"
"""O lugar de cada nome de ferramenta na frase de ``vigente`` (o banco nunca guarda NUL num texto)."""


def selo_agora_na_tela(
    selo: contrato.SeloLido, detalhes: vigente.Detalhes, *, fuso: str, reprovados: int | None = None
) -> Markup:
    """A linha "Selo agora: nível {n}." do cartao, em HTML pronto, de selo lido (``vigente.selo_agora_legivel``), com
    duas trocas:

    - no motivo ``reprovado``, a conta de ``casos_que_nao_aprovam`` (``reprovados``), a mesma do relatorio e do extrato
      (``vigente.detalhes`` conta so os ``falhou`` e diria "0 casos falharam" num teste reprovado por um obrigatorio
      sem resposta);
    - no ``ferramenta_nova``, cada nome de ferramenta (texto do sistema) vai num ``<code class="estranho">`` com os
      escondidos marcados (``[U+202E]``), como na tabela das ferramentas da mesma ficha, e nunca cru na frase."""
    if selo.motivo == "reprovado" and reprovados is not None:
        return escape(f"Selo agora: nível {selo.nivel}. {reprovado_legivel(reprovados)}")
    nomes = detalhes.ferramentas if selo.motivo == "ferramenta_nova" else ()
    marcas = tuple(_MARCA_DO_NOME.format(n) for n in range(len(nomes)))
    html = escape(vigente.selo_agora_legivel(selo, replace(detalhes, ferramentas=marcas), fuso=fuso))
    for marca, nome in zip(marcas, nomes, strict=True):
        html = html.replace(marca, Markup('<code class="estranho">{}</code>').format(marcar_escondidos(nome)))
    return html


def com_a_guarda(item: contrato.NaoTestado, guarda: Any) -> str:
    """O texto do que nao testamos; na guarda dos numeros, com os dias que a declaracao diz (desenho 7.1)."""
    if item.item != "guarda_dos_numeros" or not isinstance(guarda, int) or isinstance(guarda, bool):
        return item.texto
    if item.texto.endswith(")."):
        return f"{item.texto[:-2]}: {dias_legiveis(guarda)})."
    return f"{item.texto} Declarado: {dias_legiveis(guarda)}."


@dataclass(frozen=True, slots=True)
class PlanoNaTela:
    """O plano do teste de um nivel pela declaracao (critica B5): ate quantas chamadas e escritas reais no alvo."""

    nivel: int
    chamadas: int
    escritas: int
    passa_do_teto: bool


@dataclass(frozen=True, slots=True)
class CartaoDoSelo:
    """O que o cartao "Selo" da ficha do sistema mostra (desenho 7.1), tudo lido do banco agora.

    ``agora``: "Selo agora: nivel {n}." com o motivo legivel, em HTML pronto (``selo_agora_na_tela``, de selo lido:
    a conta do reprovado e os nomes de ferramenta marcados). ``detalhe``: o texto do ADMIN na retirada.
    ``pode_testar``: sistema proprio com chave por pessoa; num terceiro (ate o G7) ou noutro tipo de chave, o cartao so
    diz por que nao. Se a leitura falhar, a ficha recebe ``None`` no lugar do cartao e abre do mesmo jeito (o selo
    nunca para a tela)."""

    agora: Markup
    nivel: int
    teste_id: uuid.UUID | None
    detalhe: str | None
    terceiro: bool
    por_pessoa: bool
    fase_ligada: bool
    declaracao: declaracao_do_selo.DeclaracaoSalva | None
    modo: str | None
    alvo: str | None
    responsavel: str | None
    ferramentas: tuple[str, ...]
    guarda_dias: int | None
    planos: tuple[PlanoNaTela, ...]
    teto: int
    chave_entregue_em: Any
    aberto: Any
    """O pedido ``pedido`` ou ``rodando`` do sistema, se houver (um por vez)."""
    interrompido: Any
    """O pedido mais recente, se ele ficou ``interrompido``."""
    historico: list[Any]

    @property
    def pode_testar(self) -> bool:
        return not self.terceiro and self.por_pessoa

    @property
    def nao_testado(self) -> list[str]:
        """O que a bateria nunca testa (``contrato.NAO_TESTADOS``, sem IDs do rascunho), com a guarda declarada."""
        return [com_a_guarda(n, self.guarda_dias) for n in sorted(contrato.NAO_TESTADOS, key=lambda n: n.item)]


TEXTOS_DO_CARTAO: Final[dict[str, str]] = {
    "nao_garante": SELO_NAO_GARANTE,
    "confirmo": SELO_CONFIRMO,
    "nota": SELO_NOTA_DO_ALVO,
    "atencao": SELO_ATENCAO_DO_ALVO,
    "terceiro": declaracao_do_selo.TERCEIRO_AINDA_NAO,
    "so_por_pessoa": SELO_SO_POR_PESSOA,
    "sem_declaracao": SELO_SEM_DECLARACAO,
    "nao_lido": SELO_NAO_LIDO,
    "fase_desligada": (
        "Os selos de nível 2 e 3 ainda não estão ligados para as pessoas reais: só pessoas de teste usam o selo deste"
        " sistema. Você pode declarar, guardar a chave de teste e pedir o teste."
    ),
}
"""Os textos fixos do cartao "Selo" (com acento), que a pagina recebe prontos."""


async def _chave_de_teste_entregue_em(c: AsyncConnection, sistema_id: uuid.UUID) -> Any:
    """Quando o ADMIN entregou a chave de teste pela tela, pela ultima vez (a linha ``admin_chave_entregue``). A
    entrada nao le ``chaves_cifradas``: quem confere de verdade e a bateria (``sem_chave_de_teste``)."""
    return (
        await c.execute(
            text(
                "SELECT max(criado_em) FROM extrato WHERE sistema_id = :s AND tipo = 'admin_chave_entregue'"
                " AND dados ->> 'entrega' = :t"
            ),
            {"s": sistema_id, "t": contrato.TIPO_CHAVE_TESTE},
        )
    ).scalar_one_or_none()


async def _casos_do_teste_que_nao_aprovam(c: AsyncConnection, teste_id: uuid.UUID) -> int:
    """A conta do relatorio (``casos_que_nao_aprovam``) para o teste ``teste_id``, pelo nivel pedido."""
    t = (await c.execute(text("SELECT casos, nivel_pedido FROM testes_selo WHERE id = :t"), {"t": teste_id})).first()
    return 0 if t is None else casos_que_nao_aprovam(t.casos, int(t.nivel_pedido))


_SQL_PEDIDOS: Final = (
    "SELECT p.id, p.nivel, p.estado, p.motivo, p.pedido_em, p.mudou_em,"
    " coalesce(nullif(btrim(pe.nome), ''), pe.sujeito) AS quem, t.id AS teste_id, t.resultado, t.nivel_obtido,"
    " t.testado_em, t.vale_ate"
    " FROM pedidos_teste_selo p JOIN pessoas pe ON pe.id = p.pedido_por"
    " LEFT JOIN testes_selo t ON t.pedido_id = p.id"
    " WHERE p.sistema_id = :s ORDER BY p.pedido_em DESC, p.id DESC LIMIT :n"
)
HISTORICO_MAXIMO: Final = 20


def planos_da_declaracao(modelo: declaracao_do_selo.Declaracao, *, pares: int, teto: int) -> tuple[PlanoNaTela, ...]:
    """O plano de cada nivel pela declaracao, pela conta da bateria (``selo.bateria.plano``, sem a lista do alvo: o
    maximo, com o C-2.7 e o C-2.2b em toda acao)."""
    from mcp_claro.selo import bateria  # so a conta do plano; o resto da bateria e do despachante

    saida: list[PlanoNaTela] = []
    for nivel in contrato.NIVEIS:
        p = bateria.plano(modelo, nivel, pares=pares)
        saida.append(PlanoNaTela(nivel, p.chamadas, p.escritas, p.chamadas > teto))
    return tuple(saida)


async def cartao_do_selo(
    c: AsyncConnection, linha: Any, *, fuso: str, fase_ligada: bool, pares: int, teto: int
) -> CartaoDoSelo:
    """Le o cartao "Selo" do sistema ``linha`` (``consultas.sistema``). O selo sai so de ``selo_vigente`` (vigente.ler),
    nunca de ``sistemas.selo``."""
    selo = await vigente.ler(c, linha.id)
    reprovados: int | None = None
    if selo.motivo == "reprovado" and selo.teste_id is not None:
        detalhes = vigente.Detalhes()
        reprovados = await _casos_do_teste_que_nao_aprovam(c, selo.teste_id)
    else:
        detalhes = await vigente.detalhes(c, linha.id, selo)
    agora = selo_agora_na_tela(selo, detalhes, fuso=fuso, reprovados=reprovados)
    salva = await declaracao_do_selo.ultima(c, linha.id)
    modelo: declaracao_do_selo.Declaracao | None = None
    if salva is not None:
        with contextlib.suppress(declaracao_do_selo.DeclaracaoRecusada):
            modelo = salva.modelo()
    pedidos = list((await c.execute(text(_SQL_PEDIDOS), {"s": linha.id, "n": HISTORICO_MAXIMO})).all())
    aberto = next((p for p in pedidos if p.estado in ("pedido", "rodando")), None)
    interrompido = pedidos[0] if pedidos and pedidos[0].estado == "interrompido" else None
    return CartaoDoSelo(
        agora=agora,
        nivel=selo.nivel,
        teste_id=selo.teste_id,
        detalhe=detalhes.detalhe if detalhes.retirada == "admin" else None,
        terceiro=str(linha.origem) != "proprio",
        por_pessoa=str(linha.tipo_chave) == "por_pessoa",
        fase_ligada=fase_ligada,
        declaracao=salva,
        modo=None if modelo is None else MODOS_LEGIVEIS.get(modelo.modo, modelo.modo),
        alvo=None if modelo is None else modelo.endereco_teste,
        responsavel=None if modelo is None else modelo.responsavel,
        ferramentas=() if modelo is None else tuple(sorted(modelo.ferramentas)),
        guarda_dias=None if modelo is None else modelo.guarda_dias,
        planos=() if modelo is None else planos_da_declaracao(modelo, pares=pares, teto=teto),
        teto=teto,
        chave_entregue_em=await _chave_de_teste_entregue_em(c, linha.id),
        aberto=aberto,
        interrompido=interrompido,
        historico=pedidos,
    )


@dataclass(frozen=True, slots=True)
class LinhaDoCaso:
    """Uma linha do relatorio: o caso, o que testamos, o resultado e por que (tudo do vocabulario do contrato)."""

    caso: str
    requisito: str
    o_que_testamos: str
    resultado: str
    codigo: str
    por_que: str
    excluidas: int


@dataclass(frozen=True, slots=True)
class GrupoDoRelatorio:
    """Os casos de uma ferramenta (``nome``, a revisada que o teste cobriu) ou do sistema inteiro (``nome`` nulo)."""

    nome: str | None
    versao: int | None
    linhas: list[LinhaDoCaso]


@dataclass(frozen=True, slots=True)
class Relatorio:
    """O relatorio de um teste do selo (``testes_selo``), com a declaracao usada e o que nao testamos."""

    teste: Any
    grupos: list[GrupoDoRelatorio]
    nao_testado: list[str]
    falhas: int
    declaracao: dict[str, Any]
    ferramentas: list[dict[str, Any]]

    def como_json(self, sistema: Any) -> dict[str, Any]:
        """O JSON que o ADMIN baixa: so o que o banco guardou (casos, contagens, horas e razoes do vocabulario, sem
        texto do sistema), a declaracao usada e o rodape."""
        t = self.teste
        return {
            "relatorio": "Teste do selo do MCP-CLARO",
            "contrato": t.contrato,
            "bateria": t.bateria,
            "sistema": {"id": str(sistema.id), "nome": sistema.nome},
            "teste": {
                "id": str(t.id),
                "nivel_pedido": int(t.nivel_pedido),
                "resultado": t.resultado,
                "nivel_obtido": int(t.nivel_obtido),
                "iniciado_em": t.iniciado_em.isoformat(),
                "testado_em": t.testado_em.isoformat(),
                "vale_ate": t.vale_ate.isoformat(),
                "chamadas": int(t.chamadas),
                "pedido_por": t.quem,
            },
            "retrato": {
                "endereco": t.sistema_endereco,
                "tipo_chave": t.sistema_tipo_chave,
                "revisao_servidor_id": None if t.revisao_servidor_id is None else str(t.revisao_servidor_id),
            },
            "ferramentas": self.ferramentas,
            "casos": list(t.casos),
            "nao_testado": [
                {"item": n.item, "razao": n.razao, "texto": n.texto}
                for n in sorted(contrato.NAO_TESTADOS, key=lambda n: n.item)
            ],
            "declaracao": self.declaracao,
            "rodape": SELO_RODAPE,
        }


_SQL_RELATORIO: Final = (
    "SELECT t.id, t.contrato, t.bateria, t.nivel_pedido, t.resultado, t.nivel_obtido, t.sistema_endereco,"
    " t.sistema_tipo_chave, t.revisao_servidor_id, t.ferramentas, t.casos, t.nao_testado, t.chamadas, t.iniciado_em,"
    " t.testado_em, t.vale_ate, p.pedido_em, coalesce(nullif(btrim(pe.nome), ''), pe.sujeito) AS quem,"
    " d.versao AS declaracao_versao, d.declaracao AS declaracao_dados, d.salva_por AS declaracao_salva_por,"
    " d.salva_em AS declaracao_salva_em"
    " FROM testes_selo t JOIN pedidos_teste_selo p ON p.id = t.pedido_id JOIN pessoas pe ON pe.id = p.pedido_por"
    " JOIN declaracoes_selo d ON d.id = t.declaracao_id WHERE t.id = :t AND t.sistema_id = :s"
)


def _linha_do_caso(item: dict[str, Any]) -> LinhaDoCaso:
    codigo = str(item.get("caso"))
    caso = contrato.CASOS_POR_NOME.get(codigo)
    resultado = str(item.get("resultado"))
    razao = item.get("razao")
    excluidas = item.get("excluidas")
    return LinhaDoCaso(
        caso=codigo,
        requisito="" if caso is None else caso.requisito,
        o_que_testamos="" if caso is None else caso.o_que_testamos,
        resultado=RESULTADOS_LEGIVEIS.get(resultado, resultado),
        codigo=resultado,
        por_que=RAZOES_LEGIVEIS.get(str(razao), "") if razao is not None else "",
        excluidas=excluidas if isinstance(excluidas, int) and not isinstance(excluidas, bool) else 0,
    )


async def relatorio_do_teste(c: AsyncConnection, sistema_id: uuid.UUID, teste_id: uuid.UUID) -> Relatorio | None:
    """O relatorio do teste ``teste_id`` do sistema ``sistema_id``; ``None`` se o teste nao e deste sistema."""
    t = (await c.execute(text(_SQL_RELATORIO), {"t": teste_id, "s": sistema_id})).first()
    if t is None:
        return None
    ids = [uuid.UUID(str(f)) for f in t.ferramentas]
    revisadas = {
        uuid.UUID(str(r.id)): r
        for r in (
            await c.execute(
                text("SELECT id, nome_original, versao FROM ferramentas_revisadas WHERE id = ANY (:i)"), {"i": ids}
            )
        ).all()
    }
    ordem = {caso.caso: n for n, caso in enumerate(contrato.CASOS)}
    itens = [i for i in t.casos if isinstance(i, dict)]
    itens.sort(key=lambda i: ordem.get(str(i.get("caso")), len(ordem)))
    grupos = [GrupoDoRelatorio(None, None, [_linha_do_caso(i) for i in itens if i.get("ferramenta") is None])]
    ferramentas: list[dict[str, Any]] = []
    for f in ids:
        r = revisadas.get(f)
        nome = None if r is None else str(r.nome_original)
        versao = None if r is None else int(r.versao)
        ferramentas.append({"id": str(f), "nome": nome, "versao": versao})
        linhas = [_linha_do_caso(i) for i in itens if str(i.get("ferramenta")) == str(f)]
        grupos.append(GrupoDoRelatorio(nome or str(f), versao, linhas))
    guarda = t.declaracao_dados.get("guarda_dias") if isinstance(t.declaracao_dados, dict) else None
    nao_testado: list[str] = []
    for n in sorted(contrato.NAO_TESTADOS, key=lambda n: n.item):
        nao_testado.append(com_a_guarda(n, guarda))
    return Relatorio(
        teste=t,
        grupos=[g for g in grupos if g.linhas or g.nome is not None],
        nao_testado=nao_testado,
        falhas=casos_que_nao_aprovam(list(t.casos), int(t.nivel_pedido)),
        declaracao={
            "versao": int(t.declaracao_versao),
            "salva_por": t.declaracao_salva_por,
            "salva_em": t.declaracao_salva_em.isoformat(),
            "dados": t.declaracao_dados,
        },
        ferramentas=ferramentas,
    )


@dataclass(frozen=True, slots=True)
class SeloNaLista:
    """A coluna "Selo" da lista de sistemas (pelo id do sistema em texto): o nivel e, se vence em ate o aviso das
    rotinas, quantos dias faltam."""

    nivel: int
    vence_em: int | None


async def selos_da_lista(c: AsyncConnection) -> dict[str, SeloNaLista]:
    """O selo de agora de cada sistema (``selos_vigentes``) e, num selo de nivel 2 ou 3 perto de vencer, os dias que
    faltam, contados pelo relogio do banco. O aviso e o mesmo das rotinas (``BADGE_WARN_DAYS``, menor que a
    validade: ``config.politica.dias_de_aviso_do_selo``)."""
    validade = await _politica(c, "BADGE_TTL_DAYS")
    aviso = dias_de_aviso_do_selo(validade, await _politica(c, "BADGE_WARN_DAYS"))
    linhas = (
        await c.execute(
            text(
                "SELECT sistema_id, nivel, CASE WHEN nivel >= 2 THEN"
                " CAST(ceil(extract(epoch FROM vale_ate - now()) / 86400) AS integer) END AS dias FROM selos_vigentes"
            )
        )
    ).all()
    return {
        str(x.sistema_id): SeloNaLista(
            int(x.nivel), int(x.dias) if x.dias is not None and 0 < int(x.dias) <= aviso else None
        )
        for x in linhas
    }


async def _politica(c: AsyncConnection, chave: str) -> int:
    try:
        return await consultas.valor_politica(c, chave)
    except ValueError:
        return valor_inicial(chave)


def _motivo_da_retirada(f: Formulario) -> str:
    return _limpo(f.texto("motivo"))


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
            rota_com_teto(
                "/admin/sistemas/{sistema_id:uuid}/selo/declaracao", self.salvar_declaracao, TETO_FORMULARIO_DECLARACAO
            ),
            r("/admin/sistemas/{sistema_id:uuid}/selo/testar", self.pedir_teste, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/selo/retirar", self.retirar_selo, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/selo/testes/{teste_id:uuid}.json", self.relatorio_json),
            r("/admin/sistemas/{sistema_id:uuid}/selo/testes/{teste_id:uuid}", self.relatorio),
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
        selos: dict[str, SeloNaLista] | None = None
        try:
            async with self.t.engine.connect() as c:
                selos = await selos_da_lista(c)
        except SQLAlchemyError as erro:  # o selo nunca para a tela: a coluna diz que nao leu
            log.warning("telas_admin_selos_nao_lidos", erro=type(erro).__name__)
        return self.t.pagina(
            pedido,
            "admin/sistemas.html",
            {
                "sistemas": sistemas,
                "selos": selos,
                "dias_legiveis": dias_legiveis,
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
        selo = await self._cartao_do_selo(linha)
        sessao = await self.t.sessao(pedido)
        versao = selo.declaracao.versao if selo is not None and selo.declaracao is not None else None
        return self.t.pagina(
            pedido,
            "admin/sistema.html",
            {
                "aviso": aviso_da_pagina(pedido.query_params.get("aviso", ""), versao=versao),
                "selo": selo,
                "selo_textos": TEXTOS_DO_CARTAO,
                # O login refeito volta sempre para a ficha (GET), mesmo quando a pagina e a resposta de um POST.
                "refazer": None
                if sessao is None
                else self.t.login_refeito(pedido, sessao, f"/telas/admin/sistemas/{linha.id}"),
                "pede": SELO_PEDE_LOGIN,
                "dias_legiveis": dias_legiveis,
                "interrupcoes": INTERRUPCOES_LEGIVEIS,
                "estados_do_pedido": ESTADOS_DO_PEDIDO_LEGIVEIS,
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

    async def pagina_do_sistema(
        self, pedido: Request, linha: Any, *, erros: list[str], status: int | None = None
    ) -> Response:
        """A ficha do sistema com ``erros`` (as telas de chaves, ``admin_portas``, voltam por aqui na chave de
        teste do selo)."""
        return await self._pagina_sistema(pedido, linha, erros=erros, status=status)

    # --- Selo de nivel 2 e 3 (Fase 4) ---

    async def _cartao_do_selo(self, linha: Any) -> CartaoDoSelo | None:
        """O cartao "Selo", numa conexao propria: se a leitura falhar, a ficha abre sem ele (o selo nunca para a
        tela) e diz que nao leu."""
        try:
            async with self.t.engine.connect() as c:
                return await cartao_do_selo(
                    c,
                    linha,
                    fuso=self.t.cfg.fuso,
                    fase_ligada=self.t.p.FEATURE_PHASE4_BADGES is True,
                    pares=self.t.p.BADGE_TEST_PAIRS,
                    teto=self.t.p.BADGE_TEST_MAX_CALLS,
                )
        except (SQLAlchemyError, ValueError) as erro:
            log.warning("telas_admin_selo_nao_lido", erro=type(erro).__name__)
            return None

    async def _recusa_do_selo(
        self, pedido: Request, linha: Any, erro: str, status: int, valores: dict[str, Any] | None = None
    ) -> Response:
        log.info("telas_admin_selo_recusado", status=status)
        return await self._pagina_sistema(pedido, linha, erros=[erro], valores=valores, status=status)

    @tela
    async def salvar_declaracao(self, pedido: Request) -> Response:
        """Salva a declaracao de teste (desenho 2.5): com CSRF, login refeito e a caixa marcada. Nesta ordem:
        ``declaracao.ler_texto``, ``cadastro_do_sistema``, ``conferir`` e ``salvar``, com a linha do extrato na mesma
        transacao. A recusa volta com a frase (``DeclaracaoRecusada.mensagem``; a declaracao incompleta diz a
        ferramenta que falta) e 400; o terceiro, 409 (ate o G7)."""
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        texto = f.texto("declaracao")
        valores: dict[str, Any] = {"declaracao": texto}
        if not self.t.login_recente(sessao):
            return await self._recusa_do_selo(pedido, linha, SELO_LOGIN_REFEITO, 403, valores)
        if f.texto("confirmo") != "sim":
            return await self._recusa_do_selo(pedido, linha, SELO_SEM_CONFIRMO, 400, valores)
        try:
            modelo = declaracao_do_selo.ler_texto(texto)
            async with self.t.engine.begin() as c:
                cadastro = await declaracao_do_selo.cadastro_do_sistema(c, linha.id)
                declaracao_do_selo.conferir(modelo, cadastro, ambiente=self.t.p.APP_ENV)
                salva = await declaracao_do_selo.salvar(
                    c, sistema_id=linha.id, pessoa_id=sessao.pessoa.id, salva_por=sessao.quem[:200], declaracao=modelo
                )
                await self.t.extrato(
                    c,
                    "admin_selo_declaracao_salva",
                    {"sistema": linha.nome, "quem": sessao.quem, "versao": salva.versao},
                    pessoa_id=sessao.pessoa.id,
                    sistema_id=linha.id,
                    dados={"declaracao_id": str(salva.id), "versao": salva.versao, "modo": modelo.modo},
                )
        except declaracao_do_selo.DeclaracaoRecusada as erro:
            status = 409 if erro.motivo == "selo_terceiro_ainda_nao" else 400
            return await self._recusa_do_selo(pedido, linha, erro.mensagem, status, valores)
        except DBAPIError as erro:
            return await self._recusa_do_selo(pedido, linha, recusa_do_banco(erro), 409, valores)
        log.info("telas_admin_selo_declaracao_salva", versao=salva.versao)
        return ver_depois(f"/telas/admin/sistemas/{linha.id}", "selo_declaracao_salva")

    @tela
    async def pedir_teste(self, pedido: Request) -> Response:
        """Pede o teste de nivel 2 ou 3 (desenho 3.1): com CSRF e login refeito, a declaracao mais recente conferida
        de novo contra o cadastro de agora e a chave de teste entregue. Um pedido por vez por sistema: com outro
        esperando ou rodando, 409. Quem roda e o despachante; a tela so grava o pedido e a linha do extrato."""
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        if not self.t.login_recente(sessao):
            return await self._recusa_do_selo(pedido, linha, SELO_LOGIN_REFEITO, 403)
        escolhido = f.texto("nivel")
        if escolhido not in ("2", "3"):
            return await self._recusa_do_selo(pedido, linha, SELO_NIVEL_FORA, 400)
        nivel = int(escolhido)
        recusa: tuple[str, int] | None = None
        try:
            declaracao_do_selo.recusar_terceiro(str(linha.origem) != "proprio")
            async with self.t.engine.begin() as c:
                salva = await declaracao_do_selo.ultima(c, linha.id)
                if salva is None or await _chave_de_teste_entregue_em(c, linha.id) is None:
                    recusa = (SELO_SEM_DECLARACAO, 409)
                else:
                    cadastro = await declaracao_do_selo.cadastro_do_sistema(c, linha.id)
                    declaracao_do_selo.conferir(salva.modelo(), cadastro, ambiente=self.t.p.APP_ENV)
                    # Um pedido por vez: o indice unico do banco (pedidos_teste_selo_um_por_vez) recusa o segundo,
                    # tambem com dois cliques ao mesmo tempo, e a recusa vira SELO_TESTE_JA_RODANDO.
                    pedido_id = (
                        await c.execute(
                            text(
                                "INSERT INTO pedidos_teste_selo (sistema_id, nivel, declaracao_id, pedido_por)"
                                " VALUES (:s, :n, :d, :p) RETURNING id"
                            ),
                            {"s": linha.id, "n": nivel, "d": salva.id, "p": sessao.pessoa.id},
                        )
                    ).scalar_one()
                    await self.t.extrato(
                        c,
                        "admin_selo_teste_pedido",
                        {"nivel": nivel, "sistema": linha.nome, "quem": sessao.quem},
                        pessoa_id=sessao.pessoa.id,
                        sistema_id=linha.id,
                        dados={"pedido_id": str(pedido_id), "nivel": nivel, "declaracao_versao": salva.versao},
                    )
        except declaracao_do_selo.DeclaracaoRecusada as erro:
            recusa = (erro.mensagem, 409 if erro.motivo == "selo_terceiro_ainda_nao" else 400)
        except DBAPIError as erro:
            recusa = (recusa_do_banco(erro), 409)
        if recusa is not None:
            return await self._recusa_do_selo(pedido, linha, *recusa)
        log.info("telas_admin_selo_teste_pedido", nivel=nivel)
        return ver_depois(f"/telas/admin/sistemas/{linha.id}", "selo_teste_pedido")

    @tela
    async def retirar_selo(self, pedido: Request) -> Response:
        """Retira o selo vigente (D29): com CSRF, login refeito e o motivo do ADMIN (de 1 a 500 letras). O banco grava
        quem retirou pelo cadastro (``selos_retirados_nasce``) e so aceita o selo vigente; o aviso ao ADMIN e das
        rotinas (``rotinas.selos``), uma vez por teste retirado. A transacao toma antes a trava das respostas do
        sistema (``TRAVA_DAS_INFORMACOES``): duas retiradas ao mesmo tempo gravam uma so, e a outra volta com 409."""
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        motivo = _motivo_da_retirada(f)
        valores: dict[str, Any] = {"motivo": motivo}
        if not self.t.login_recente(sessao):
            return await self._recusa_do_selo(pedido, linha, SELO_LOGIN_REFEITO, 403, valores)
        if not 1 <= len(motivo) <= MOTIVO_MAXIMO_DA_RETIRADA:
            return await self._recusa_do_selo(pedido, linha, SELO_MOTIVO_DA_RETIRADA, 400, valores)
        retirado: uuid.UUID | None = None
        try:
            async with self.t.engine.begin() as c:
                # A trava das respostas do sistema antes de ler o selo (TRAVA_DAS_INFORMACOES): uma retirada por vez.
                await c.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:t), hashtext(CAST(:s AS text)))"),
                    {"t": TRAVA_DAS_INFORMACOES, "s": str(linha.id)},
                )
                selo = await vigente.ler(c, linha.id)
                if selo.nivel >= 2 and selo.teste_id is not None:
                    await c.execute(
                        text(
                            "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por,"
                            " pessoa_id) VALUES (:s, :t, 'admin', :d, :q, :p)"
                        ),
                        {"s": linha.id, "t": selo.teste_id, "d": motivo, "q": sessao.quem[:200], "p": sessao.pessoa.id},
                    )
                    await self.t.extrato(
                        c,
                        "selo_retirado",
                        {"sistema": linha.nome, "motivo": f"o ADMIN {sessao.quem} retirou o selo"},
                        pessoa_id=sessao.pessoa.id,
                        sistema_id=linha.id,
                        dados={"motivo": "admin", "teste_id": str(selo.teste_id), "nivel": selo.nivel},
                    )
                    retirado = selo.teste_id
        except DBAPIError as erro:
            return await self._recusa_do_selo(pedido, linha, recusa_do_banco(erro), 409, valores)
        if retirado is None:
            return await self._recusa_do_selo(pedido, linha, SELO_NADA_A_RETIRAR, 409, valores)
        CONTADORES.somar("selo_retiradas", motivo="admin")
        log.info("telas_admin_selo_retirado")
        return ver_depois(f"/telas/admin/sistemas/{linha.id}", "selo_retirado")

    async def _relatorio_ou_404(self, pedido: Request, linha: Any) -> Relatorio:
        async with self.t.engine.connect() as c:
            achado = await relatorio_do_teste(c, linha.id, pedido.path_params["teste_id"])
        if achado is None:
            raise Interrompe(
                self.t.erro(pedido, 404, "Teste não encontrado", "Este teste do selo não é deste sistema.")
            )
        return achado

    @tela
    async def relatorio(self, pedido: Request) -> Response:
        """O relatorio de um teste do selo (desenho 7.1): por ferramenta, os casos, o resultado e por que; o que nao
        testamos; a declaracao usada; o JSON para baixar e o rodape da secao 3.4."""
        await self.t.exigir_admin(pedido)
        linha = await self._sistema_ou_404(pedido)
        achado = await self._relatorio_ou_404(pedido, linha)
        return self.t.pagina(
            pedido,
            "admin/selo_teste.html",
            {
                "sistema": linha,
                "r": achado,
                "t": achado.teste,
                "declaracao_legivel": _json_legivel(achado.declaracao["dados"]),
                "modos": MODOS_LEGIVEIS,
                "textos": {"nao_garante": SELO_NAO_GARANTE, "rodape": SELO_RODAPE, "sistema": ESCOPO_SISTEMA},
            },
        )

    @tela
    async def relatorio_json(self, pedido: Request) -> Response:
        """O mesmo relatorio em JSON, para baixar (sem texto do sistema: so o que ``testes_selo`` guarda e a
        declaracao usada)."""
        await self.t.exigir_admin(pedido)
        linha = await self._sistema_ou_404(pedido)
        achado = await self._relatorio_ou_404(pedido, linha)
        corpo = json.dumps(achado.como_json(linha), ensure_ascii=False, indent=2, sort_keys=False)
        return Response(
            corpo.encode("utf-8"),
            media_type="application/json",
            headers={"content-disposition": f'attachment; filename="teste-do-selo-{achado.teste.id}.json"'},
        )

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
        status: int | None = None,
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
                # O formulario devolve a impressao do que esta pagina mostrou: a revisao so grava essa definicao.
                "impressao_vista": str(vista.impressao),
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
            status=status or (400 if erros else 200),
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
        vista_na_pagina = f.texto("impressao_vista")
        if vista_na_pagina and vista_na_pagina != str(vista.impressao):
            # Uma leitura das declaracoes gravou outra definicao depois que a pagina abriu: o ADMIN nao a viu. A
            # pagina volta com a definicao nova (e a impressao dela); sem o campo (formulario antigo), segue.
            log.info("telas_revisao_ferramenta_mudou")
            return await self._pagina_ferramenta(
                pedido, sistema, nome, erros=[FERRAMENTA_MUDOU, *erros], valores=valores, status=409
            )
        if erros:
            return await self._pagina_ferramenta(pedido, sistema, nome, erros=erros, valores=valores)
        try:
            async with self.t.engine.begin() as c:
                # A definicao gravada e a comparada acima (a da pagina, quando ela mandou a impressao).
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
