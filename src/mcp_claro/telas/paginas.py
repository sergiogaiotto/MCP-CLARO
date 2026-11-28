"""Modelos Jinja2 das telas, sempre com escape automatico, e os textos de aviso.

Fase 3 (servidores de terceiros), textos das telas do ADMIN, com acento: de quem e o servidor (``ORIGENS``), o
desfecho da sondagem de versao (``frase_da_sondagem``), os pedidos que um sistema fez e o MCP-CLARO recusou
(``PEDIDOS_LEGIVEIS``, com todas as categorias de ``ligacao.cliente``, inclusive as do fio antigo), os motivos da
recusa da revisao do servidor (``MOTIVOS_REVISAO_SERVIDOR`` e ``PROBLEMAS_REVISAO``) e a recusa de um esquema de
terceiro (``frase_do_esquema_recusado``). Nada daqui leva texto do servidor: so o nosso vocabulario e os nomes do
cadastro. Onde uma frase precisa citar um nome de campo ou o caminho de uma recusa, que vem do fornecedor, a versao
para a tela (``frase_do_esquema_recusado_na_tela`` e ``campo_do_fornecedor``) poe o nome dentro de
``<code class="estranho">``, escapado e com os escondidos marcados.

Fase 4 (selos de nivel 2 e 3), textos das telas da pessoa, com acento: o vocabulario fechado do contrato do selo
(``ESTADOS_LEGIVEIS``, desenho 2.6), os avisos novos da pessoa e a coluna "Selo" do extrato
(``selo_da_linha_do_extrato`` e o filtro ``selo_da_linha``: so de ``dados.selo`` da propria linha, com nivel 2 ou 3
so pelo teste aprovado, critica M3) com a etiqueta "Informado pelo sistema" (``etiqueta_da_linha_do_extrato``). Nada
daqui leva texto do sistema.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from mcp_claro.cadastro import esquema
from mcp_claro.ligacao import cliente, sala_contrato
from mcp_claro.regras.escondidos import marcar_escondidos
from mcp_claro.versao import VERSAO_MCP

PASTA_MODELOS: Final = Path(__file__).resolve().parent / "modelos"
PASTA_ESTATICOS: Final = Path(__file__).resolve().parent / "estaticos"

# Avisos depois de um passo que deu certo. A URL leva so o codigo, nunca texto livre.
AVISOS: Final[dict[str, str]] = {
    "saiu": "Você saiu. Até logo.",
    "ordem_dada": "Pronto. A ordem de serviço foi dada ao aplicativo.",
    "ordem_revogada": "Pronto. A ordem foi revogada. O aplicativo não pode mais usá-la.",
    "aplicativo_habilitado": "Pronto. O aplicativo foi habilitado.",
    "aplicativo_reativado": (
        "Pronto. O aplicativo foi reativado. Os pedidos dele voltam a passar, se nenhum bloqueio ou pausa os"
        " segurar. As ações dele só saem com a porta de saída aberta. As que já tinham parado não voltam."
    ),
    "aplicativo_nada_mudou": (
        "Nada mudou: este aplicativo não é um assistente pausado. Um programa pausado volta pela tela do programa."
    ),
    "declaracoes_lidas": "Pronto. Lemos as ferramentas que o sistema declara hoje.",
    "ferramenta_revisada": "Pronto. A revisão foi gravada. A ferramenta já pode entrar em modelos de ordem.",
    "ferramenta_revisada_terceiro": (
        "Pronto. A revisão foi gravada. Num servidor de terceiros, a ferramenta só aparece nas vitrines e nos"
        " modelos de ordem depois que você concluir a revisão do servidor."
    ),
    "modelo_criado": "Pronto. O modelo de ordem de serviço foi criado.",
    "programa_cadastrado": "Pronto. O programa foi cadastrado, com responsável e substituto.",
    "destino_cadastrado": "Pronto. O destino de webhook foi cadastrado.",
    "destino_mudou": "Pronto. O destino de webhook foi atualizado.",
    "despachante_mudo": (
        "O despachante não respondeu. O sistema ficou pendente. Peça a conferência de novo daqui a pouco."
    ),
    "motivo_exigido": "Para abrir o extrato de outra pessoa, diga antes o motivo.",
    # Bloqueio de um toque e pausa (pessoa).
    "bloqueado": "Pronto. Está bloqueado a partir de agora. Só o ADMIN desfaz.",
    "pausado": "Pronto. Está pausado a partir de agora. Você retoma quando quiser.",
    "ja_valia": "Isto já estava valendo. Nada novo foi ligado. Este é o comprovante.",
    "pausa_retomada": "Pronto. A pausa terminou. O aplicativo volta a poder ler por você, dentro das suas ordens.",
    "ja_terminou": "Isto já tinha terminado. Nada mudou.",
    # Autorizar sistema (pessoa).
    "autorizacao_guardada": (
        "Pronto. Sua autorização foi guardada, cifrada no cofre. O aplicativo já pode ler nesse sistema por você,"
        " só dentro das suas ordens de serviço."
    ),
    "autorizacao_retirada": "Pronto. Sua autorização neste sistema foi retirada. Nada mais é lido lá por você.",
    "nada_a_retirar": "Não havia autorização sua guardada neste sistema. Nada mudou.",
    # Portas, pessoas, autorizacao e chaves (ADMIN).
    "bloqueio_ligado": "Pronto. O bloqueio foi ligado e vale a partir de agora.",
    "pausa_ligada": "Pronto. A pausa foi ligada e vale a partir de agora.",
    "ja_valia_admin": "Já havia um igual valendo. Nada novo foi ligado.",
    "parada_encerrada": "Pronto. Terminou. O que estava parado volta a funcionar, se nada mais o parar.",
    "entrada_mudou": "Pronto. A entrada mudou e vale a partir de agora.",
    "saida_mudou": "Pronto. A porta de saída mudou e vale a partir de agora.",
    "pessoa_desativada": "Pronto. A pessoa foi desativada e as autorizações dela nos sistemas foram derrubadas.",
    "autorizacao_configurada": "Pronto. A autorização por pessoa deste sistema foi configurada.",
    "segredo_guardado": "Pronto. O segredo foi entregue ao cofre. Ele não aparece mais nas telas.",
    "chave_guardada": "Pronto. A chave foi entregue ao cofre. Ela não aparece mais nas telas.",
    # Fase 2: fichas, acoes, passkeys e limites (pessoa).
    "confirmada": "Pronto. A ficha foi confirmada. A ação espera a hora de sair; ainda dá para cancelar.",
    "recusada": "Pronto. A ficha foi recusada. Nada foi para o sistema.",
    "cancelada": "Pronto. A ação foi cancelada antes de sair. Nada chegou ao sistema.",
    "seguir_agora": "Pronto. A ação sai agora, sem esperar o resto do tempo.",
    "decisao_registrada": "Pronto. Sua decisão foi registrada. Nada será reenviado.",
    "passkey_cadastrada": "Pronto. A passkey foi cadastrada. Use-a para confirmar ações de risco alto e limites.",
    "passkey_desativada": "Pronto. A passkey foi desativada. Ela não serve mais para confirmar nada.",
    "limite_criado": "Pronto. O limite foi criado e vale a partir de agora.",
    "limite_ampliado": "Pronto. O limite foi ampliado.",
    "limite_encerrado": "Pronto. O limite foi encerrado. A partir de agora, essa ação volta a pedir ficha.",
    # Fase 3: servidores de terceiros (ADMIN).
    "servidor_revisado": (
        "Pronto. A revisão do servidor foi registrada. As ferramentas marcadas aparecem nas vitrines, dentro das"
        " ordens de serviço."
    ),
    "servidor_revisao_recusada": "A revisão do servidor não foi concluída. Veja abaixo o que mudou e revise de novo.",
    "despachante_mudo_revisao": "O despachante não respondeu. Nada mudou. Tente de novo daqui a pouco.",
    "despachante_demorou_revisao": (
        "O despachante não respondeu a tempo. A revisão do servidor pode ter sido registrada ou não: confira o"
        " cartão “Revisão do servidor” abaixo antes de tentar de novo."
    ),
    "despachante_demorou_sondagem": (
        "O despachante não respondeu a tempo. Confira a situação abaixo e peça a conferência de novo daqui a pouco."
    ),
    # Fase 4: selos de nivel 2 e 3 (pessoa). Os avisos do ADMIN ficam em ``admin.AVISOS_ADMIN``.
    "informado_registrado": (
        "Pronto. Sua decisão foi registrada de acordo com o que o sistema informou. Nada será reenviado."
    ),
    "confirmada_numero_novo": (
        "Pronto. A ficha foi confirmada com número novo: para o sistema, é um pedido novo. A ação espera a hora de"
        " sair; ainda dá para cancelar."
    ),
}

SITUACOES: Final[dict[str, str]] = {
    "ativa": "Valendo",
    "ativo": "Ativo",
    "vencida": "Venceu",
    "revogada": "Revogada",
    "inativa": "Parada por falta de uso",
    "pausada": "Pausada",
    "pausado": "Pausado",
    "bloqueado": "Bloqueado",
    "bloqueada": "Bloqueada",
    "desativada": "Desativada",
    "pendente": "Falta a sondagem",
    "recusado": "Recusado",
    "quarentena": "Em quarentena",
    "desativado": "Desativado",
    "encerrado": "Encerrado",
}

CLASSES: Final[dict[str, str]] = {
    "le": "Só lê",
    "muda": "Muda algo",
    "tem_volta": "Muda algo, mas tem volta",
}

TIPOS_CHAVE: Final[dict[str, str]] = {
    "por_pessoa": "Uma chave por pessoa",
    "compartilhada": "Uma chave para todos",
    "programa": "Chave do programa",
}

# --- Fase 4: selos de nivel 2 e 3 (telas da pessoa) ---

ESTADOS_LEGIVEIS: Final[dict[str, str]] = {
    "concluida": "o pedido foi feito",
    "recusada": "o sistema recusou o pedido; nada ficou feito",
    "em_andamento": "o pedido ainda está em andamento",
    "nao_recebida": "o sistema não recebeu o pedido e não vai aceitá-lo depois",
    "parcial": "o pedido foi feito só em parte",
    "desfeita": "o pedido foi feito e depois desfeito",
    "parada": "o pedido foi parado antes de terminar, pelo bloqueio",
    "desconhecido": "o sistema não sabe se o pedido foi feito",
}
"""O vocabulario fechado do contrato do selo (``contrato.ESTADOS``, desenho 2.6) como a tela mostra, com acento. So
estas categorias chegam a pessoa: nunca texto, identificador ou hora escritos pelo sistema (D21)."""
ESTADO_FORA_DO_VOCABULARIO: Final = "uma resposta fora do vocabulário do contrato"
"""Reserva para um estado que nao e do vocabulario (o banco os recusa; a tela nunca repete o que veio)."""


def estado_legivel(estado: Any) -> str:
    """O estado do contrato do selo em palavras (``ESTADOS_LEGIVEIS``); fora do vocabulario, a reserva (nunca o
    valor que veio)."""
    return ESTADOS_LEGIVEIS.get(str(estado), ESTADO_FORA_DO_VOCABULARIO) if estado is not None else ""


TIPOS_INFORMADO_PELO_SISTEMA: Final = frozenset({"sistema_informou", "sistema_informou_no_bloqueio"})
"""As linhas do extrato com o que o sistema informou (desenho 7.2): levam a etiqueta "Informado pelo sistema"."""
INFORMADO_PELO_SISTEMA: Final = "Informado pelo sistema"
INFORMADO_PELO_TERCEIRO: Final = "Informado pelo servidor de terceiros"
"""Num terceiro, a etiqueta diz que foi o servidor de terceiros quem informou (critica M9, desenho 8)."""

SELO_NIVEL_1: Final = "nível 1"


def selo_da_linha_do_extrato(
    nivel: Any, nivel_do_teste: Any, testado_em: Any, *, data: Callable[[datetime], str] | None = None
) -> str:
    """A coluna "Selo" do extrato, so de ``dados.selo`` da propria linha (o retrato da hora do passo, D25), nunca do
    selo de agora do sistema. ``nivel`` e o nivel do retrato (numero 1, 2 ou 3; outro valor, sem selo: ""). Nivel 2
    ou 3 so aparece com o teste do retrato aprovado em ``testes_selo`` (critica M3): ``nivel_do_teste`` e o
    ``nivel_obtido`` desse teste (aprovado e do mesmo sistema da linha; None sem ele) e ``testado_em`` a data dele,
    do banco. Sem o teste que o confirme, "nível 1": nada e prometido."""
    if type(nivel) is not int or nivel not in (1, 2, 3):
        return ""
    if nivel == 1:
        return SELO_NIVEL_1
    if type(nivel_do_teste) is not int or nivel_do_teste < nivel or not isinstance(testado_em, datetime):
        return SELO_NIVEL_1
    dia = data(testado_em) if data is not None else testado_em.strftime("%d/%m/%Y")
    return f"nível {nivel} (teste de {dia})"


def etiqueta_da_linha_do_extrato(tipo: Any, origem: Any) -> str:
    """ "Informado pelo sistema" (ou "pelo servidor de terceiros") nas linhas com o que o sistema informou; "" nas
    outras."""
    if str(tipo) not in TIPOS_INFORMADO_PELO_SISTEMA:
        return ""
    return INFORMADO_PELO_TERCEIRO if origem and str(origem) != "proprio" else INFORMADO_PELO_SISTEMA


# --- Fase 3: servidores de terceiros (telas do ADMIN) ---

ORIGENS: Final[dict[str, str]] = {
    "proprio": "Da organização (sistema próprio)",
    "terceiro": "De terceiros (de um fornecedor de fora)",
}
"""As escolhas de "De quem é este servidor?" no cadastro."""

ORIGENS_CURTAS: Final[dict[str, str]] = {"proprio": "Da organização", "terceiro": "De terceiros"}
"""A coluna "De quem" da lista de sistemas."""

TIPOS_CHAVE_TERCEIRO: Final = ("por_pessoa", "compartilhada")
"""Tipos de chave que um servidor de terceiros aceita (D2; o banco tambem recusa os outros)."""

DESFECHOS_SONDAGEM: Final[dict[str, str]] = {
    "aceito": f"Aceito. Este servidor fala a versão {VERSAO_MCP} do MCP.",
    "versao": (
        f"Este servidor de terceiros foi recusado. Ele não fala a versão {VERSAO_MCP} do MCP, a única que o"
        " MCP-CLARO aceita. Ele anunciou: {versoes}. Peça ao fornecedor para atualizar o servidor e peça a"
        " conferência de novo. A recusa entrou na contagem de versões."
    ),
    "desvio": (
        "O servidor pediu para irmos a outro endereço. O MCP-CLARO só chama o endereço combinado e não segue"
        " desvios. Confira o endereço certo com o fornecedor."
    ),
    "nao_mcp": "Este endereço não respondeu como um servidor MCP. Confira o endereço.",
    "sem_acesso": "O servidor pediu uma chave. Informe a chave de teste e confira de novo.",
    "inalcancavel": (
        "Ainda não conseguimos falar com o servidor. Confira o endereço e peça a conferência de novo daqui a pouco."
    ),
}
"""O que a ficha do sistema diz sobre a ultima sondagem, por ``sistemas.sondagem_desfecho`` (``Sondagem.desfecho``).
No lugar do motivo tecnico cru, que deixa de aparecer. ``versao`` leva ``{versoes}``."""

VERSAO_PROPRIO: Final = (
    "Ele anunciou: {versoes}. Peça à equipe do sistema para atualizar o kit MCP. Depois, peça a conferência de novo."
)
"""O desfecho ``versao`` num sistema proprio (a ficha mostra antes "Este sistema foi recusado." e que ele so fala a
geracao antiga do MCP)."""

INALCANCAVEL_POR_MOTIVO: Final[dict[str, str]] = {
    cliente.MOTIVO_ENDERECO_RECUSADO: (
        "Nada foi enviado: o endereço deste servidor de terceiros não serve. Ou ele não é público (o MCP-CLARO só"
        " chama servidores de terceiros em endereços da internet), ou não usa https, ou o nome do servidor é"
        " inválido. Confira o endereço com o fornecedor."
    ),
    sala_contrato.MOTIVO_SALA_FORA: (
        "A sala de visitas, por onde o MCP-CLARO fala com servidores de terceiros, não está disponível agora. Nada"
        " foi enviado ao servidor. Peça a conferência de novo daqui a pouco."
    ),
}
"""``inalcancavel`` quando a falha e nossa ou do endereco, nao do servidor: frase propria."""

VERSOES_NENHUMA: Final = "nenhuma versão no formato do MCP de agora (é da geração antiga)"
VERSAO_FORA_DO_FORMATO: Final = "uma versão escrita fora do formato AAAA-MM-DD"


def versoes_legiveis(versoes: Iterable[str] | None) -> str:
    """As versoes que o servidor anunciou, ja conferidas pelo despachante (AAAA-MM-DD ou ``outra``)."""
    nomes: list[str] = []
    for versao in versoes or ():
        nome = VERSAO_FORA_DO_FORMATO if versao == cliente.OUTRA else str(versao)
        if nome not in nomes:
            nomes.append(nome)
    return ", ".join(nomes) if nomes else VERSOES_NENHUMA


def frase_da_sondagem(
    desfecho: str | None, motivo: str | None, *, origem: str, versoes: Sequence[str] | None = None
) -> str | None:
    """A frase da ficha do sistema para o desfecho da ultima sondagem. None: sem sondagem com desfecho (linhas de
    antes da Fase 3), e a ficha mostra o texto de antes."""
    if desfecho is None:
        return None
    if desfecho == "versao":
        modelo = DESFECHOS_SONDAGEM["versao"] if origem != "proprio" else VERSAO_PROPRIO
        return modelo.format(versoes=versoes_legiveis(versoes))
    if desfecho == "inalcancavel" and motivo in INALCANCAVEL_POR_MOTIVO:
        return INALCANCAVEL_POR_MOTIVO[str(motivo)]
    return DESFECHOS_SONDAGEM.get(desfecho, DESFECHOS_SONDAGEM["inalcancavel"])


# Os pedidos que um sistema fez no meio de uma chamada e o MCP-CLARO recusou (``Resposta.pedidos``), em palavras
# para quem nao e da area. O fio antigo e o jeito de antes da 2026-07-28: o servidor pedia pela resposta aberta.
_FIO_ANTIGO: Final = "pelo jeito antigo do MCP, fora do padrão 2026-07-28"
PEDIDOS_LEGIVEIS: Final[dict[str, str]] = {
    cliente.PEDIDO_FORMULARIO: "dados por formulário",
    cliente.PEDIDO_LINK: "que a pessoa abrisse um link",
    cliente.PEDIDO_AMOSTRAGEM: "uma resposta da IA do aplicativo",
    cliente.PEDIDO_RAIZES: "a lista de pastas do aplicativo",
    cliente.PEDIDO_SO_ESTADO: "uma nova volta",
    cliente.PEDIDO_LINK_CODIGO_ANTIGO: "que a pessoa abrisse um link (pelo código de erro antigo)",
    cliente.PREFIXO_FIO_ANTIGO + "elicitation/create": f"dados ou um link, {_FIO_ANTIGO}",
    cliente.PREFIXO_FIO_ANTIGO + "sampling/createMessage": f"uma resposta da IA do aplicativo, {_FIO_ANTIGO}",
    cliente.PREFIXO_FIO_ANTIGO + "roots/list": f"a lista de pastas do aplicativo, {_FIO_ANTIGO}",
    cliente.PREFIXO_FIO_ANTIGO + "ping": f"um sinal de que o aplicativo está ligado, {_FIO_ANTIGO}",
    cliente.PREFIXO_FIO_ANTIGO + cliente.DESCONHECIDO: f"algo fora do padrão, {_FIO_ANTIGO}",
}
"""Cada categoria do vocabulario de ``ligacao.cliente``. Fora daqui: ``FORA_DO_PADRAO``, nunca o texto que veio."""
FORA_DO_PADRAO: Final = "algo fora do padrão 2026-07-28"


ONDE_O_PEDIDO: Final[dict[str, str]] = {
    "leitura_sistema_pediu_dados": "Numa leitura",
    "programa_leitura_sistema_pediu_dados": "Numa leitura de um programa",
    "acao_sistema_pediu_dados": "Numa ação, que parou",
    "acao_enviada": "Numa ação, que saiu",
    "acao_nao_confirmada": "Numa ação sem confirmação",
    "acao_nao_saiu": "Numa ação, que não saiu",
    "declaracoes_sistema_pediu_dados": "Na leitura das ferramentas",
    "sondagem_sistema_pediu_dados": "Na sondagem",
}
"""Onde o sistema fez o pedido, pela linha do extrato ("Pedidos recusados deste sistema"): numa chamada ou, sem
ferramenta, na leitura das ferramentas e na sondagem (contratos K4 e K9)."""


def pedido_legivel(categoria: Any) -> str:
    return PEDIDOS_LEGIVEIS.get(str(categoria), FORA_DO_PADRAO)


def pedidos_legiveis(categorias: Iterable[Any] | None) -> str:
    """As categorias em palavras, sem repeticao, com "e" antes da ultima. Lista vazia: ``FORA_DO_PADRAO``."""
    nomes: list[str] = []
    for categoria in categorias or ():
        nome = pedido_legivel(categoria)
        if nome not in nomes:
            nomes.append(nome)
    if not nomes:
        return FORA_DO_PADRAO
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]


MOTIVOS_REVISAO_SERVIDOR: Final[dict[str, str]] = {
    "nao_e_terceiro": "Só servidor de terceiros tem revisão do servidor.",
    "fornecedor": "Escreva quem fornece este servidor, de 3 a 200 letras.",
    "publicadas_demais": "Marque até 500 ferramentas para a vitrine.",
    "situacao": "O servidor precisa estar ativo ou em quarentena para concluir a revisão.",
    "versao": (
        f"O servidor precisa passar na conferência da versão {VERSAO_MCP} antes de concluir a revisão. Toque em"
        " “Conferir a versão de novo”."
    ),
    "chave_indisponivel": (
        "Não deu para abrir no cofre a chave de leitura das declarações. Nada mudou. Tente de novo daqui a pouco."
    ),
    "sem_chave_declaracoes": (
        "Guarde a chave de leitura das declarações deste servidor antes de concluir a revisão. Ela fica em"
        " “Autorização e chaves deste sistema”. É a chave que o MCP-CLARO usa para conferir, de tempos em tempos,"
        " se o servidor mudou."
    ),
    "sem_resposta": (
        "O servidor não entregou a lista de ferramentas agora, ou a sala de visitas, por onde o MCP-CLARO fala com"
        " ele, não estava disponível. Nada mudou. Tente de novo daqui a pouco."
    ),
    "situacao_mudou": (
        "O servidor mudou de situação enquanto a revisão era concluída. Nada mudou. Leia as ferramentas de novo e"
        " conclua outra vez."
    ),
    "ferramentas": "Algumas ferramentas marcadas não estão em dia. Nada foi publicado. Veja a lista abaixo.",
    "pedido_invalido": (
        "O pedido de conclusão trouxe algo que o MCP-CLARO não guarda. Nada mudou. Confira o nome do fornecedor e"
        " as ferramentas marcadas."
    ),
}
"""A frase da tela para cada ``motivo`` da recusa de ``/interno/revisao_servidor`` (a ``mensagem`` vem sem acento)."""
MOTIVO_REVISAO_DESCONHECIDO: Final = "A revisão do servidor não foi concluída. Nada mudou."

PROBLEMAS_REVISAO: Final[dict[str, str]] = {
    "mudou": "mudou desde a sua revisão: revise de novo",
    "sem_revisao": "ainda não tem revisão sua",
    "sumiu": "o servidor não declara mais esta ferramenta",
    "rever": "foi revisada antes da Fase 3: revise de novo para guardar o texto que o servidor declara",
}
"""Cada ``problema`` da lista ``ferramentas`` da recusa."""
PROBLEMA_DESCONHECIDO: Final = "não está em dia"


def frase_da_revisao_recusada(motivo: Any) -> str:
    return MOTIVOS_REVISAO_SERVIDOR.get(str(motivo), MOTIVO_REVISAO_DESCONHECIDO)


# Recusa do esquema de entrada de um terceiro (``esquema.EsquemaRecusado``), por ``motivo``, ``onde`` e ``palavra``.
_PALAVRAS_RECUSADAS: Final[dict[str, str]] = {
    "patternProperties": "regra de formato nos nomes de campo (patternProperties)",
    "$id": "identificador de parte ($id)",
    "$anchor": "âncora ($anchor)",
    "$dynamicAnchor": "âncora dinâmica ($dynamicAnchor)",
    "$dynamicRef": "referência dinâmica ($dynamicRef)",
    "unevaluatedProperties": "regra dos campos não conferidos (unevaluatedProperties)",
    "unevaluatedItems": "regra dos itens não conferidos (unevaluatedItems)",
}
ESQUEMA_FUNDO_DEMAIS: Final = (
    "O esquema de entrada desta ferramenta tem partes demais umas dentro das outras para o MCP-CLARO conferir."
    " Peça ao fornecedor um esquema mais simples."
)
"""``EsquemaInvalido`` com ``entrada.argumentos.ESQUEMA_FUNDO_DEMAIS`` (proprio e terceiro)."""


def _numero(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def frase_do_esquema_recusado(motivo: str, onde: str, palavra: str | None) -> str:
    """A frase com acento para o ADMIN. ``onde`` ja vem com os escondidos marcados (``(raiz)`` na raiz)."""
    lugar = "na raiz do esquema" if onde == "(raiz)" else f"em {onde}"
    if motivo == "palavra" and palavra == "$schema":
        return (
            f"O esquema deste servidor troca a versão do JSON Schema ($schema) {lugar}. Num servidor de terceiros, o"
            " MCP-CLARO só aceita $schema na raiz do esquema. Peça ao fornecedor um esquema sem essa troca."
        )
    if motivo == "palavra":
        nome = _PALAVRAS_RECUSADAS.get(palavra or "", marcar_escondidos(str(palavra or "uma palavra recusada")))
        return (
            f"O esquema deste servidor usa {nome} {lugar}. Num servidor de terceiros, o MCP-CLARO não aceita isso."
            " Peça ao fornecedor um esquema sem essa palavra."
        )
    if motivo == "referencia" and palavra == "$ref":
        return (
            f"A referência ($ref) {lugar} tem mais de {esquema.REFERENCIA_MAXIMA} letras. Num servidor de terceiros,"
            f" o MCP-CLARO só aceita referência de até {esquema.REFERENCIA_MAXIMA} letras, porque lê o texto inteiro"
            " dela a cada chamada. Peça ao fornecedor um esquema com nomes mais curtos."
        )
    if motivo == "referencia" and palavra in ("$defs", "definitions"):
        return (
            f"O nome de uma definição ({palavra}) {lugar} tem mais de {esquema.REFERENCIA_MAXIMA} letras. Num"
            f" servidor de terceiros, o MCP-CLARO só aceita nome de definição de até {esquema.REFERENCIA_MAXIMA}"
            " letras. Peça ao fornecedor um esquema com nomes mais curtos."
        )
    if motivo == "referencia":
        return (
            f"A referência ($ref) {lugar} não aponta para uma definição deste esquema. Num servidor de terceiros, o"
            " MCP-CLARO só aceita referência da forma #/$defs/<nome> ou #/definitions/<nome>, para uma definição"
            " que existe. Peça ao fornecedor um esquema mais simples."
        )
    if motivo == "ciclo":
        return (
            f"A referência {lugar} volta a uma parte que a contém (esquema em ciclo). Num servidor de terceiros, o"
            " MCP-CLARO não aceita esquema em ciclo, nem em definições que nenhuma parte usa. Peça ao fornecedor um"
            " esquema mais simples."
        )
    if motivo == "grande":
        return (
            f"O esquema tem mais de {_numero(esquema.CUSTO_MAXIMO)} partes, contando todas as definições e seguindo"
            f" as referências (passou {lugar}). Isso pode travar o MCP-CLARO a cada chamada. Peça ao fornecedor um"
            " esquema mais simples."
        )
    if motivo == "fundo" and onde == "(raiz)":
        return (
            "O esquema tem partes demais umas dentro das outras para o MCP-CLARO conferir. Peça ao fornecedor um"
            " esquema mais simples."
        )
    if motivo == "fundo":
        return (
            f"O esquema tem mais de {esquema.PROFUNDIDADE_MAXIMA} níveis, contando as definições e seguindo as"
            f" referências, também a partir de cada definição (passou {lugar}). Peça ao fornecedor um esquema mais"
            " simples."
        )
    if motivo == "pesado":
        return (
            f"O esquema tem mais de {esquema.BYTES_MAXIMOS // 1024} KiB ({_numero(esquema.BYTES_MAXIMOS)} bytes),"
            " contando o esquema como fica gravado: sem os textos do fornecedor e com as descrições do ADMIN. Isso"
            " pode travar o MCP-CLARO a cada chamada. Peça ao fornecedor um esquema menor."
        )
    if motivo == "escolhas":
        return (
            f"A lista de escolhas (enum) {lugar} tem mais de {_numero(esquema.ESCOLHAS_MAXIMAS)} valores. Isso pode"
            " travar o MCP-CLARO a cada chamada. Peça ao fornecedor um esquema com menos escolhas."
        )
    return (
        "O esquema deste servidor não é aceito num servidor de terceiros. Peça ao fornecedor um esquema mais simples."
    )


_LUGAR: Final = "\x00"
"""Marca o lugar do caminho na frase, para ``frase_do_esquema_recusado_na_tela`` (o nulo nunca vem num texto nosso)."""


def frase_do_esquema_recusado_na_tela(motivo: str, onde: str, palavra: str | None) -> Markup:
    """A mesma frase, pronta para a tela: o caminho (``onde``) leva nomes de campo escritos pelo fornecedor, e vai
    dentro de ``<code class="estranho">``, com os escondidos marcados. O resto do texto e escapado."""
    if onde == "(raiz)":
        return escape(frase_do_esquema_recusado(motivo, onde, palavra))
    antes, achou, depois = frase_do_esquema_recusado(motivo, _LUGAR, palavra).partition(_LUGAR)
    if not achou:
        return escape(antes)
    caminho = Markup('<code class="estranho">{}</code>').format(marcar_escondidos(onde))
    return escape(antes) + caminho + escape(depois)


def campo_do_fornecedor(campo: str) -> Markup:
    """Um nome de campo escrito pelo fornecedor numa frase da tela: escapado, com os escondidos marcados e dentro de
    ``<code class="estranho">``."""
    return Markup('<code class="estranho">{}</code>').format(marcar_escondidos(campo))


def criar_ambiente(fuso: str) -> Environment:
    try:
        zona: ZoneInfo | None = ZoneInfo(fuso)
    except ZoneInfoNotFoundError, ValueError:
        zona = None

    def data_hora(valor: Any) -> str:
        if not isinstance(valor, datetime):
            return ""
        local = valor.astimezone(zona) if zona else valor
        return local.strftime("%d/%m/%Y %H:%M")

    def so_data(valor: Any) -> str:
        if isinstance(valor, datetime):
            valor = valor.astimezone(zona) if zona else valor
        if isinstance(valor, date):
            return valor.strftime("%d/%m/%Y")
        return ""

    ambiente = Environment(
        loader=FileSystemLoader(PASTA_MODELOS),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ambiente.filters["data_hora"] = data_hora
    ambiente.filters["data"] = so_data
    ambiente.filters["situacao"] = lambda v: SITUACOES.get(str(v), str(v))
    ambiente.filters["classe"] = lambda v: CLASSES.get(str(v), str(v))
    ambiente.filters["tipo_chave"] = lambda v: TIPOS_CHAVE.get(str(v), str(v))
    ambiente.filters["origem"] = lambda v: ORIGENS_CURTAS.get(str(v), ORIGENS_CURTAS["terceiro"])
    ambiente.filters["pedido"] = pedido_legivel
    ambiente.filters["pedidos"] = pedidos_legiveis
    ambiente.filters["onde_pedido"] = lambda v: ONDE_O_PEDIDO.get(str(v), "Numa chamada")
    # Fase 4: o selo da linha do extrato (de ``dados.selo`` da propria linha, ``consultas.linhas_extrato``) e a
    # etiqueta das linhas com o que o sistema informou. Uma linha sem as colunas novas fica sem selo e sem etiqueta.
    ambiente.filters["selo_da_linha"] = lambda linha: selo_da_linha_do_extrato(
        getattr(linha, "selo_nivel", None),
        getattr(linha, "selo_teste_nivel", None),
        getattr(linha, "selo_testado_em", None),
        data=so_data,
    )
    ambiente.filters["informado_pela_linha"] = lambda linha: etiqueta_da_linha_do_extrato(
        getattr(linha, "tipo", None), getattr(linha, "sistema_origem", None)
    )
    ambiente.filters["estado_legivel"] = estado_legivel
    # Nome de campo e texto vindo do sistema (ou do aplicativo), no texto visivel: os caracteres escondidos viram
    # uma marca como [U+200B], como na ficha. O ``value=`` dos formularios fica cru.
    ambiente.filters["escondidos"] = lambda v: marcar_escondidos(str(v))
    return ambiente
