"""Contrato do selo, versao ``selo/1``: o que um sistema faz para ter selo de nivel 2 ou 3 no MCP-CLARO.

E uma lista de regras para programas, publica, pequena e igual para sistemas proprios e de terceiros. Tudo vai
pela versao 2026-07-28 do MCP, sem extensao nem capacidade anunciada: tres ferramentas com nome fixo
(``selo_alvo``, ``selo_consultar`` e ``selo_bloqueio``) e chaves de ``_meta`` com o prefixo do gateway
(``versao.PREFIXO_META``): o numero da operacao no pedido, o recibo na resposta e a marca de alvo de teste.

Este modulo e a fonte do contrato no codigo. A migracao 0007 tem as mesmas listas no banco (``selo_casos``,
``selo_esquemas``, ``selo_vocabulario``, ``selo_passagem_permitida``, ``selo_esquema_essencial`` com o orcamento de
nos de ``selo_esquema_teto`` e ``selo_esquema_peso``, ``selo_endereco_normalizado``, ``selo_nao_testados``), e
``tests/integracao/test_migracao_0007.py`` e ``tests/integracao/test_selo_custo_do_banco.py`` conferem que o SQL e
o codigo dizem o mesmo. O alvo de teste tem mais tres funcoes no banco com a regra de ``selo/declaracao.py``, que
``tests/integracao/test_selo_declaracao_banco.py`` confere: ``selo_alvo_bem_escrito`` (``_nome_do_alvo``),
``selo_ipv4_escrito`` (``_ip_escrito``) e ``selo_endereco_conexao`` (``forma_de_conexao``, menos o nome fora de
ASCII e a porta escrita como ``int()`` a le, que o alvo nunca tem). ``docs/contrato.md`` (G8) publica as mesmas
tabelas.

Sobre o rascunho independente de escrita segura (commit ``7b952151dddd84260d057cdb506081820003fe56``,
``spec/mws.md``): os IDs dele aparecem so em ``Requisito.adaptado_de`` e ``Requisito.parte_de`` e nas
recomendacoes, que vao para a secao de fontes do contrato. Nunca aparecem nos casos, nos veredictos, no que nao e
testado, no relatorio, no extrato nem perto de qualquer resultado (critica B6). O selo nao e resultado, nota nem
declaracao de conformidade desse rascunho; o criterio de aprovacao e do MCP-CLARO.

Os textos dos requisitos, dos casos e do que nao e testado vao aos documentos e as telas: por isso tem acento.
O resto (nomes, codigos, vocabulario) nao tem.

Decisoes do coordenador na etapa 1 (os grupos seguintes seguem estas):

- O aviso de bloqueio vale so para bloqueio, nao para pausa (``ESTADOS_DO_AVISO``). O despachante (G3) filtra pelo
  tipo; o banco recusa o aviso de uma pausa (``avisos_bloqueio_nasce``).
- Critica A3.3: a primeira consulta so vai depois do prazo do envio, e ``nao_recebida`` so vale como final com
  duas respostas iguais, com intervalo (``nao_recebida_confirmada``). Vale na rota da consulta (G3) e nas rotinas
  (G5), pelas mesmas funcoes (``ESCADA_DA_CONSULTA_MINUTOS``, ``proxima_consulta``, ``resposta_que_encerra``), e
  no banco, pela mesma regra: na tela (``selo_estado_final``, ``medicoes.v_fila_agora``) e na queima do numero do
  pedido igual (``envios_selo_nasce``). Uma ``nao_recebida`` sozinha nao resolve o "estamos confirmando", nao
  libera o botao "Registrar o que o sistema informou" e nao da numero novo ao pedido igual.
- Quantas operacoes seguidas sem resposta retiram o selo (``BADGE_LOOKUP_FAILS_TO_WITHDRAW``) e conta do
  despachante, pelo banco; o banco so exige a prova da retirada na mesma transacao (``ROTA_CONSULTAR``).

Decisoes do coordenador no retoque da etapa 2:

- Decisao 1, ``compara_token`` (critica A3.4), opcao (b): a bateria nao testa se o sistema reconhece a mesma
  identidade com outra chave. O cadastro guarda uma chave de teste por sistema (``TIPO_CHAVE_TESTE``), e a bateria
  nao tem uma segunda chave da mesma identidade para mostrar que o sistema responde pela pessoa, e nao pelo texto
  da chave. Por isso nenhum caso de ``CASOS`` cobre o ponto, e o modo ``compara_token`` do duble mentiroso
  (``exemplos/selo_mentiroso.py``) fica sem caso que o reprove, com a razao escrita la. O que o contrato pede fica
  na recomendacao (``RECOMENDACOES``: responder pela mesma identidade, nao pelo mesmo texto de chave), que nao conta
  para o selo. O G9 prova pelo gateway, com a chave renovada, que a pessoa nunca le "nao chegou" como final por uma
  resposta so e que a ficha do pedido igual com numero novo leva o aviso da A3.3; o G8 escreve em
  ``docs/selos.md`` que este ponto e declarado pela equipe do sistema e nao testado pelo MCP-CLARO, e o poe em
  "Mudancas deliberadas da Fase 4".
- Decisao 3: o selo nunca para a porta de saida nem a leitura (A1). Os caminhos que nao mostram o selo nao chamam
  ``selo_vigente()``: ``repositorio.sistema_por_id`` e ``repositorio.ferramenta_da_ordem`` sem ``com_selo``, que
  devolvem o sistema com ``vigente.SELO_NAO_LIDO`` (o retrato e o efetivo dele levantam ``SeloNaoLido``). E a forma
  essencial dos esquemas tem orcamento de nos (``TETO_NOS_DO_ESQUEMA``), com os mesmos numeros no banco: um esquema
  que explode vira ``GRANDE_DEMAIS`` depressa, nos dois lados.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from mcp_claro.versao import PREFIXO_META

if TYPE_CHECKING:
    from mcp_claro.ligacao.cliente import Resposta, Segredo

# --- Nome, versao e chaves de _meta -----------------------------------------------------------------------------

CONTRATO: Final = "selo/1"
"""Nome e versao do contrato. Vai no recibo, nas respostas das ferramentas do contrato e em ``testes_selo``."""

VERSAO_BATERIA: Final = "1.0.0"
"""Versao da bateria do selo que grava ``testes_selo.bateria`` (o banco exige o formato N.N.N)."""

CHAVE_OPERACAO: Final = PREFIXO_META + "operacao"
"""Chave de ``params._meta`` do ``tools/call`` com o numero da operacao (uuid em minusculas, criado pelo banco em
``envios_selo.chave``). Nunca vai como argumento nem como cabecalho, e nunca e o protocolo ``CLR-...`` (D4, D5)."""

CHAVE_RECIBO: Final = PREFIXO_META + "recibo"
"""Chave do ``_meta`` do resultado de uma ferramenta de acao com o recibo do nivel 3 (secao 2.2 do desenho)."""

CHAVE_ALVO: Final = PREFIXO_META + "alvo"
"""Chave do ``_meta`` do resultado de uma ferramenta de teste (modo ``ferramentas_de_teste``): ``"teste"``. Sem
ela, a bateria para na primeira ferramenta de teste que nao a devolver (critica A4)."""

PADRAO_UUID: Final = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
"""Forma do numero da operacao, do aviso de bloqueio e de cada numero dentro dele: uuid em minusculas."""

_UUID: Final = re.compile(PADRAO_UUID)


def numero_valido(valor: object) -> bool:
    """O valor e um numero de operacao na forma do contrato (uuid em minusculas, sem chaves nem espacos)."""
    return isinstance(valor, str) and _UUID.fullmatch(valor) is not None


# --- Ferramentas do contrato ------------------------------------------------------------------------------------

FERRAMENTA_ALVO: Final = "selo_alvo"
FERRAMENTA_CONSULTAR: Final = "selo_consultar"
FERRAMENTA_BLOQUEIO: Final = "selo_bloqueio"
FERRAMENTAS_DO_CONTRATO: Final = (FERRAMENTA_ALVO, FERRAMENTA_CONSULTAR, FERRAMENTA_BLOQUEIO)
NOMES_RESERVADOS: Final = frozenset(FERRAMENTAS_DO_CONTRATO)
"""Nomes que o banco recusa revisar (``ferramentas_revisadas_nasce``): nunca vao a vitrine nem sao publicados."""

NIVEIS: Final = (2, 3)


def ferramentas_do_nivel(nivel: int) -> tuple[str, ...]:
    """As ferramentas do contrato que o sistema lista no nivel ``nivel`` (SELO-0.1)."""
    if nivel == 2:
        return (FERRAMENTA_ALVO, FERRAMENTA_CONSULTAR)
    if nivel == 3:
        return FERRAMENTAS_DO_CONTRATO
    raise ValueError(f"O contrato do selo so tem os niveis 2 e 3, nao {nivel}")


# --- Vocabulario (fechado) --------------------------------------------------------------------------------------

Estado = Literal[
    "concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada", "desconhecido"
]
ESTADOS: Final[tuple[Estado, ...]] = (
    "concluida",
    "recusada",
    "em_andamento",
    "nao_recebida",
    "parcial",
    "desfeita",
    "parada",
    "desconhecido",
)
"""O ``estado`` de uma operacao, no recibo, na consulta e na resposta ao aviso (desenho 2.6)."""

ContaComo = Literal["aconteceu", "nao_aconteceu", "em_parte", "nao_ficou", "em_aberto"]
CONTA_COMO: Final[Mapping[str, ContaComo]] = {
    "concluida": "aconteceu",
    "recusada": "nao_aconteceu",
    "em_andamento": "em_aberto",
    "nao_recebida": "nao_aconteceu",
    "parcial": "em_parte",
    "desfeita": "nao_ficou",
    "parada": "nao_aconteceu",
    "desconhecido": "em_aberto",
}
"""A coluna "Conta como" da tabela do vocabulario. A contradicao do sistema sai dela (critica M2), nao do estado
exato: entre ``nao_recebida``, ``recusada`` e ``parada`` nao ha contradicao. E ela e conferida pelo NUMERO
(``envios_selo.chave``), nao pela operacao: a original e o pedido igual que saiu com o numero dela sao, para o
sistema, a mesma escrita, e toda resposta sobre uma conta contra as respostas sobre a outra
(``informacoes_sistema_nasce``)."""

QUER_DIZER: Final[Mapping[str, str]] = {
    "concluida": "O que foi pedido está em efeito.",
    "recusada": "O sistema recusou; nada ficou aplicado.",
    "em_andamento": "O sistema recebeu o pedido, registrou o número e ainda não terminou.",
    "nao_recebida": (
        "O sistema nunca recebeu uma chamada com este número e nunca vai pôr em efeito uma chamada com ele: o"
        " número fica queimado. Uma chamada que chegou e ainda não terminou não é nao_recebida, é em_andamento."
    ),
    "parcial": "Parte aplicada, parte não.",
    "desfeita": "Foi feito e depois desfeito.",
    "parada": "Parado pelo aviso de bloqueio antes de terminar; não vai ser posto em efeito.",
    "desconhecido": "O sistema não sabe.",
}
"""O que cada estado quer dizer, como o contrato publica (com acento: vai ao documento). ``nao_recebida`` com a
precisao da critica A3."""

ESTADOS_EM_ABERTO: Final[tuple[Estado, ...]] = ("em_andamento", "desconhecido")
ESTADOS_FINAIS: Final[tuple[Estado, ...]] = tuple(e for e in ESTADOS if e not in ESTADOS_EM_ABERTO)
"""Uma resposta com um destes estados resolve o "estamos confirmando" na tela (sem gravar a decisao da pessoa);
``nao_recebida`` so confirmada por duas respostas com intervalo (critica A3.3, ``nao_recebida_confirmada`` e
``selo_estado_final``)."""

ESTADOS_DO_RECIBO: Final[tuple[Estado, ...]] = tuple(e for e in ESTADOS if e != "desfeita")
"""O que o recibo da propria chamada pode dizer: ``concluida``, ``recusada``, ``em_andamento``, ``parcial`` ou
``desconhecido``; e, numa chamada com numero queimado, ``nao_recebida`` (ou ``parada``, se foi o aviso de bloqueio
que o queimou), sem aplicar nada (critica M2)."""

ESTADOS_QUE_QUEIMAM: Final[tuple[Estado, ...]] = ("nao_recebida", "recusada", "parada", "desfeita")
"""Com uma resposta destas sobre o numero da original (dela ou de um pedido igual que saiu com ele), o pedido igual
sai com numero novo (``envios_selo_nasce``): o numero antigo esta queimado, ja tem desfecho, ou o que foi feito foi
desfeito e a pessoa quer de novo (M4). ``nao_recebida`` so queima confirmada (``nao_recebida_confirmada``, contada
pelas respostas sobre o numero, critica A3.3): com uma so, o pedido igual sai com o mesmo numero, e o sistema
honesto devolve o desfecho da primeira chamada, ou ``nao_recebida`` de novo."""

ESTADOS_QUE_IMPEDEM_NUMERO_NOVO: Final[tuple[Estado, ...]] = ("concluida", "parcial")
"""Com uma resposta destas sobre o numero da original, de QUALQUER origem (o aviso de bloqueio tambem), a ficha do
pedido igual nao oferece o numero novo, e o banco o recusa (critica M4.2; decisao do coordenador na conferencia final
da etapa 3r): o sistema disse que a escrita foi feita, inteira ou em parte, e o numero novo a faria duas vezes. A
resposta ao aviso continua sem ser final (``ORIGENS_DA_RESPOSTA_FINAL``): ela nao encerra a escada nem queima o
numero; so impede o numero novo, que e o lado arriscado. No banco,
``selo_vocabulario('estados_que_impedem_numero_novo')``; sao os estados que contam como ``aconteceu`` e ``em_parte``
(``CONTA_COMO``). Tambem impede o numero novo a ultima resposta da original, de qualquer origem, ``em_andamento``
(um ``em_andamento`` do aviso mais novo que a ultima consulta ``desconhecido``): o sistema recebeu a escrita e ainda
nao terminou."""

EventoDepois = Literal["concluida", "entregue", "devolvida", "desfeita", "parada"]
EVENTOS_DEPOIS: Final[tuple[EventoDepois, ...]] = ("concluida", "entregue", "devolvida", "desfeita", "parada")
"""Eventos de ``depois`` na consulta do nivel 3. Qualquer outro valor torna a resposta invalida."""

ALVOS: Final = ("teste", "producao")
"""O que ``selo_alvo`` responde: ``teste`` no alvo de teste, ``producao`` no endereco de producao (SELO-0.3)."""

MODOS: Final = ("ambiente_de_teste", "ferramentas_de_teste")
"""Os dois modos da declaracao de teste (desenho 2.5)."""

Origem = Literal["recibo", "consulta", "aviso"]
ORIGENS: Final[tuple[Origem, ...]] = ("recibo", "consulta", "aviso")
"""De onde veio uma linha de ``informacoes_sistema``: sempre de uma pergunta do gateway (D20)."""

Desfecho = Literal["respondeu", "sem_resposta", "resposta_invalida", "sem_chave", "sala_fora", "sistema_fora"]
DESFECHOS: Final[tuple[Desfecho, ...]] = (
    "respondeu",
    "sem_resposta",
    "resposta_invalida",
    "sem_chave",
    "sala_fora",
    "sistema_fora",
)
"""Como terminou uma pergunta ao sistema. ``sistema_fora`` (critica A2): o sistema nao esta ``ativo`` (ou na
2026-07-28) ou, num terceiro, a revisao do servidor nao e a vigente: nada foi perguntado. Com a porta de saida
fechada, a consulta nao pergunta nem grava nada (409 ``porta_fechada`` em ``ROTA_CONSULTAR``, decisao 2 do
coordenador no retoque da etapa 3), e o aviso espera."""

DESFECHOS_QUE_GASTAM_TENTATIVA: Final[tuple[Desfecho, ...]] = ("respondeu", "sem_resposta", "resposta_invalida")
"""So estes contam na escada da consulta. ``sem_chave``, ``sala_fora`` e ``sistema_fora`` sao do MCP-CLARO ou da
situacao do sistema: nao gastam tentativa (criticas A2 e M7)."""

DESFECHOS_DE_FALHA_DO_SISTEMA: Final[tuple[Desfecho, ...]] = ("sem_resposta", "resposta_invalida")
"""So estes (e ``respondeu`` com ``desconhecido`` no fim da escada, critica B8) contam para a retirada por
``nao_respondeu_consultas`` (critica M7). O banco confere a prova (``selos_retirados_nasce``); quantas operacoes
seguidas tiram o selo (``BADGE_LOOKUP_FAILS_TO_WITHDRAW``) e conta do despachante (``ROTA_CONSULTAR``)."""

MotivoDoSelo = Literal[
    "nunca_testado",
    "reprovado",
    "vencido",
    "retirado",
    "fora_de_ativo",
    "endereco_mudou",
    "tipo_chave_mudou",
    "revisao_nova",
    "ferramenta_nova",
    "contrato_sumiu",
    "vigente",
]
MOTIVOS_DO_SELO: Final[tuple[MotivoDoSelo, ...]] = (
    "nunca_testado",
    "reprovado",
    "vencido",
    "retirado",
    "fora_de_ativo",
    "endereco_mudou",
    "tipo_chave_mudou",
    "revisao_nova",
    "ferramenta_nova",
    "contrato_sumiu",
    "vigente",
)
"""O ``motivo`` de ``selo_vigente``, na ordem em que o banco o escolhe: vale o primeiro que se aplica. So
``vigente`` tem nivel 2 ou 3; todos os outros dao nivel 1."""

MOTIVOS_DE_RETIRADA: Final = ("admin", "se_contradisse", "nao_respondeu_consultas")
ESTADOS_DO_PEDIDO: Final = ("pedido", "rodando", "terminado", "interrompido")
MOTIVOS_DE_INTERRUPCAO: Final = (
    "despachante_caiu",
    "sistema_mudou",
    "prazo",
    "porta_fechada",
    "cofre_trancado",
    "sem_chave_de_teste",
    "teto_de_chamadas",
    "erro_interno",
)
"""Por que um pedido de teste ficou ``interrompido``. ``sistema_mudou``: o banco recusou o fim do teste porque o
sistema mudou no meio (critica M8), ou recusou a passagem para ``rodando`` porque o pedido ja nao vale agora: o
sistema saiu de ``ativo`` (ou da 2026-07-28), deixou de ter chave por pessoa, ou o alvo de teste da declaracao virou
a producao de algum sistema do cadastro (critica A4; ``pedidos_teste_selo_guarda`` confere de novo antes das
escritas da bateria). ``porta_fechada``: a passagem para ``rodando`` foi recusada com a porta de saida fechada.
``prazo``: o pedido passou de ``BADGE_TEST_MAX_SECONDS`` sem terminar, por exemplo com a porta fechada (B10).
``teto_de_chamadas``: o plano da declaracao passa do teto (B5)."""

ESTADOS_DO_AVISO: Final = ("pendente", "respondido", "sem_resposta", "sistema_fora", "bloqueio_encerrado")
"""``avisos_bloqueio.estado``. Com a porta fechada, ou sem a chave da pessoa, o aviso espera (continua ``pendente``,
sem gastar tentativa): nada sai com a porta fechada, e sem a chave a falha e do MCP-CLARO (critica M7). Na primeira
vez que a chave nao abre, a dona ganha uma linha no extrato, uma so por bloqueio e sistema
(``aviso_bloqueio_ainda_nao_saiu``, com o motivo nosso). ``sistema_fora``: o MCP-CLARO nao avisou o sistema, e o
comprovante nao o culpa: o sistema saiu de ``ativo`` (A2), a chave da pessoa foi recusada na ultima tentativa (401,
M7), ou, por defesa, o sistema nao tem chave por pessoa. ``sem_resposta``: o sistema foi chamado em todas as
tentativas e nao respondeu. ``bloqueio_encerrado``: o bloqueio acabou antes de o aviso sair, e ele nao sai mais
(``aviso_bloqueio_dispensado``).

So um bloqueio (``bloqueios.tipo = 'bloqueio'``) avisa os sistemas; uma pausa nao pede que parem nem queima numeros
(decisao do coordenador, etapa 1). O despachante (G3) filtra pelo tipo e marca a pausa em
``avisos_bloqueio_derivados`` com nenhum aviso; o banco recusa o aviso de uma pausa. A resposta ao aviso
(``informacoes_sistema`` de origem ``aviso``) so existe depois de o aviso chamar o sistema: com a vez reservada
(``tentativas`` >= 1) e fora de ``sistema_fora`` e ``bloqueio_encerrado``, que nunca gravam resposta (a chave
recusada na ultima tentativa chamou o sistema, mas ele nao respondeu ao aviso). Nesses dois, o extrato diz o que houve
e nenhuma linha de ``informacoes_sistema`` e gravada. A resposta ao aviso fica gravada e mostrada, mas nunca encerra
a escada da consulta nem queima o numero (``ORIGENS_DA_RESPOSTA_FINAL``)."""

TIPO_CHAVE_TESTE: Final = "teste_selo"
"""``chaves_cifradas.tipo`` da chave da bateria, guardada pelo ADMIN. A bateria usa so ela, nunca a de uma pessoa
nem a de producao (A4, M11)."""

TIPOS_DE_CHAVE_DO_ADMIN: Final = ("acesso", "declaracoes", TIPO_CHAVE_TESTE)
"""Lista fechada de ``tipo`` que a rota ``/interno/chaves`` aceita do ADMIN (critica M11): outro valor e 400, nunca
"acesso" no lugar dele."""

RETRATO_DO_SELO: Final = ("nivel", "teste_id", "testado_em", "vale_ate", "modo", "motivo")
"""Campos do retrato do selo em ``fichas.selo`` e em ``dados.selo`` do extrato: o selo efetivo para a pessoa na
hora, com exatamente estes campos. O banco confere na ficha (``fichas_guarda``, critica M3):

- ``nivel`` e o numero 1, 2 ou 3; ``motivo`` e um de ``MOTIVOS_DO_RETRATO``;
- nivel 2 ou 3: ``teste_id`` (texto), ``testado_em``, ``vale_ate`` e ``modo`` iguais aos de ``selo_vigente`` do
  sistema da operacao agora, e ``motivo`` ``vigente``;
- nivel 1: ``motivo`` nunca e ``vigente``. Sem teste (``teste_id`` nulo), ``testado_em``, ``vale_ate`` e ``modo``
  nulos, e ``motivo`` ``nunca_testado`` ou ``fase_desligada``; com teste, um teste do proprio sistema, com as datas e
  o modo dele, ``motivo`` nunca ``nunca_testado``, e ``reprovado`` so (e sempre, fora ``fase_desligada``) com um
  teste reprovado, como ``selo_vigente`` diria.

Datas em ISO 8601 com fuso e com os microssegundos do banco (``datetime.isoformat()`` do valor lido): o banco as
compara por igualdade, e uma data cortada nos segundos e recusada. Uma retirada pode cair entre a leitura do selo e o
INSERT da ficha: quem grava a ficha (G4) faz o INSERT num SAVEPOINT e, se o banco recusar o retrato, le o selo de
novo e grava o retrato de agora (nivel 1, se o selo caiu), em vez de falhar o pedido da pessoa."""

# --- Constantes do contrato (nao sao parametros) ----------------------------------------------------------------

GUARDA_MINIMA_DIAS: Final = 30
REAPROVEITAR_NUMERO_DIAS: Final = 7
MAX_OPERACOES_POR_AVISO: Final = 100
MAX_EVENTOS_DEPOIS: Final = 20
MARGEM_EVENTO_MINUTOS: Final = 5
"""A hora de um evento de ``depois`` fica entre o envio menos 5 minutos e agora mais 5 minutos (o banco confere)."""
TETO_RESPOSTA_BYTES: Final = 16 * 1024
TETO_DECLARACAO_BYTES: Final = 64 * 1024
MAX_FERRAMENTAS_DECLARACAO: Final = 200
TETO_ARGUMENTOS_BYTES: Final = 16 * 1024
VISTA_VALE_HORAS: Final = 48
"""As ferramentas do contrato contam como listadas so se a ultima leitura (``ferramentas_vistas.vista_em``) tem
menos de 48 horas e elas nao sumiram (``sumiu_em``): duas vezes o maior ``TOOL_DRIFT_CHECK_SECONDS`` (critica M1)."""
AVISO_ENVIOS_EM_ABERTO_DIAS: Final = GUARDA_MINIMA_DIAS
"""O aviso de bloqueio inclui todo envio de nivel 3 do alcance com ultimo estado em aberto, ate 30 dias (B9)."""
PARES_MINIMOS: Final = 30
"""Com menos pares simultaneos que isto, o caso C-2.2d fica ``nao_testado`` (``poucos_pares``) e so informa (M5)."""

# --- Esquemas das ferramentas do contrato -----------------------------------------------------------------------

_CONTRATO_FIXO: Final[dict[str, Any]] = {"type": "string", "const": CONTRATO}
_NUMERO: Final[dict[str, Any]] = {"type": "string", "pattern": PADRAO_UUID}

ESQUEMAS: Final[Mapping[str, Mapping[str, dict[str, Any]]]] = {
    FERRAMENTA_ALVO: {
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
        "outputSchema": {
            "type": "object",
            "required": ["contrato", "alvo"],
            "properties": {"contrato": _CONTRATO_FIXO, "alvo": {"type": "string", "enum": list(ALVOS)}},
        },
    },
    FERRAMENTA_CONSULTAR: {
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["operacao"],
            "properties": {"operacao": _NUMERO},
        },
        "outputSchema": {
            "type": "object",
            "required": ["contrato", "operacao", "estado"],
            "properties": {
                "contrato": _CONTRATO_FIXO,
                "operacao": {"type": "string"},
                "estado": {"type": "string", "enum": list(ESTADOS)},
                "depois": {
                    "type": "array",
                    "maxItems": MAX_EVENTOS_DEPOIS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["evento", "em"],
                        "properties": {
                            "evento": {"type": "string", "enum": list(EVENTOS_DEPOIS)},
                            "em": {"type": "string", "format": "date-time"},
                        },
                    },
                },
            },
        },
    },
    FERRAMENTA_BLOQUEIO: {
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["bloqueio", "operacoes"],
            "properties": {
                "bloqueio": _NUMERO,
                "operacoes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_OPERACOES_POR_AVISO,
                    "items": _NUMERO,
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "required": ["contrato", "bloqueio", "operacoes"],
            "properties": {
                "contrato": _CONTRATO_FIXO,
                "bloqueio": {"type": "string"},
                "operacoes": {
                    "type": "array",
                    "maxItems": MAX_OPERACOES_POR_AVISO,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["operacao", "estado"],
                        "properties": {
                            "operacao": {"type": "string"},
                            "estado": {"type": "string", "enum": list(ESTADOS)},
                        },
                    },
                },
            },
        },
    },
}
"""``inputSchema`` e ``outputSchema`` publicados de cada ferramenta do contrato, para copiar. A conferencia (C-0.1
na bateria, ``selo_contrato_listado`` no banco) compara a forma essencial (``forma_essencial``), nao o texto."""

ANOTACOES_DO_ESQUEMA: Final = frozenset(
    {
        "title",
        "description",
        "$schema",
        "$id",
        "$comment",
        "$defs",
        "definitions",
        "examples",
        "default",
        "format",
        "additionalProperties",
        "maxItems",
        "minItems",
        "maxLength",
        "minLength",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)
"""O que nao conta na conferencia dos esquemas (critica B4): anotacoes que o kit poe sozinho (``title``), as
definicoes (``$defs``, depois de resolver as referencias locais) e os limites que o MCP-CLARO confere sozinho na
resposta (``selo/respostas.py``). Conta o resto: ``type``, ``required``, ``properties``, ``items``, ``enum``,
``const``, ``pattern`` e qualquer palavra que nao esteja aqui (``anyOf``, ``minimum``...), que faz a conferencia
falhar. ``type`` ao lado de ``const`` ou ``enum`` tambem nao conta: os valores ja estao fixos."""

FUNDO_MAXIMO_DO_ESQUEMA: Final = 32
_REF_LOCAL: Final = re.compile(r"#/(\$defs|definitions)/([^/]+)")

TETO_NOS_DO_ESQUEMA: Final = 2000
"""Orcamento de nos de uma chamada de ``forma_essencial`` (e de ``selo_esquema_essencial`` no banco, com o mesmo
numero em ``selo_esquema_teto('nos')``, que o teste confere). Passou dele, a forma e ``GRANDE_DEMAIS``, que nunca
confere com o contrato.

A conta: cada parte que a forma visita gasta o seu peso inteiro, com tudo o que esta dentro dela (``peso_do_json``), e
cada ``$ref`` seguido gasta, de novo, o peso inteiro da definicao que ele acha. Assim um esquema pequeno cujas
referencias se repetem (``a -> {x: a, y: a}``, em ciclo ou numa cadeia sem ciclo de 32 definicoes) para depois de
uns 2000 nos, em vez de crescer em exponencial. E o banco, que copia cada parte que visita (a parte recebida, a
definicao, a juncao com o ``$ref``, os filhos, a forma de saida), so copia o que o orcamento ja pagou. O peso nao
depende da ordem das chaves: o banco e o codigo, que andam em ordens diferentes, passam do teto nos mesmos esquemas.

Folga: o mais caro dos esquemas que o kit gera para as ferramentas do contrato (com ``title`` e ``$defs``) gasta
``CUSTO_DO_KIT_MEDIDO`` nos; o teto da mais de dez vezes isso (o teste mede)."""

BYTES_POR_NO: Final = 64
"""Um texto (valor ou nome de campo) pesa 1 no e mais 1 a cada 64 bytes em UTF-8; um numero, 1 e mais 1 a cada 64
letras do jeito que o banco o escreve. Um texto longo, que o banco copia a cada visita, paga o que custa."""

CUSTO_DO_KIT_MEDIDO: Final = 185
"""O custo, em nos, do esquema mais caro que o kit gera para as ferramentas do contrato (a saida de
``selo_consultar``, com ``$defs``). O teste ``test_custo_dos_esquemas_do_kit_cabe_com_folga`` confere o numero."""

GRANDE_DEMAIS: Final = "grande_demais"
"""A forma essencial de um esquema que passou de ``TETO_NOS_DO_ESQUEMA``: um texto, que nunca confere."""


class _GrandeDemais(Exception):
    """O orcamento de nos acabou (interno a ``forma_essencial``)."""


def _peso_do_texto(texto: str) -> int:
    return 1 + len(texto.encode("utf-8", "surrogatepass")) // BYTES_POR_NO


def texto_do_numero_no_banco(numero: int | float) -> str:
    """O numero JSON como o banco o escreve (``numeric_out`` do jsonb): o inteiro como e; o real, que o JSON do
    Python escreve na forma curta (``1e+16``, ``1.5e-07``), por extenso (``10000000000000000``, ``0.00000015``), e
    sem o zero negativo, que o banco nao guarda."""
    if isinstance(numero, int):
        try:
            return str(numero)
        except ValueError:  # mais digitos do que str(int) escreve
            return format(Decimal(numero), "f")
    texto = format(Decimal(repr(numero)), "f")
    if texto.startswith("-") and Decimal(texto) == 0:
        return texto[1:]
    return texto


def _peso_de_um(valor: Any) -> int:
    if isinstance(valor, str):
        return _peso_do_texto(valor)
    if isinstance(valor, int | float) and not isinstance(valor, bool):
        return 1 + len(texto_do_numero_no_banco(valor)) // BYTES_POR_NO
    return 1


def _pesos(valor: Any) -> Iterator[int]:
    """O peso de cada valor e de cada nome de campo de ``valor``, um por vez, sem recursao."""
    pilha = [valor]
    while pilha:
        atual = pilha.pop()
        yield _peso_de_um(atual)
        if isinstance(atual, dict):
            for chave, filho in atual.items():
                yield _peso_do_texto(chave) if isinstance(chave, str) else 1
                pilha.append(filho)
        elif isinstance(atual, list | tuple):
            pilha.extend(atual)


def peso_do_json(valor: Any) -> int:
    """O peso de um valor JSON na conta de ``TETO_NOS_DO_ESQUEMA``: 1 por valor (objeto, lista, texto, numero,
    booleano ou nulo, em qualquer nivel) e 1 por nome de campo, e cada texto (valor ou nome) e cada numero mais 1 a
    cada ``BYTES_POR_NO``. Mesma regra de ``selo_esquema_peso`` no banco (o teste confere)."""
    return sum(_pesos(valor))


class _Orcamento:
    """Os nos ja gastos numa chamada de ``forma_essencial``."""

    __slots__ = ("gasto",)

    def __init__(self) -> None:
        self.gasto = 0

    def gastar_peso(self, valor: Any) -> None:
        """Gasta o peso de ``valor`` e para assim que o orcamento acaba (``_GrandeDemais``)."""
        for peso in _pesos(valor):
            self.gasto += peso
            if self.gasto > TETO_NOS_DO_ESQUEMA:
                raise _GrandeDemais


def custo_da_forma_essencial(esquema: Any) -> int | None:
    """Os nos que ``forma_essencial`` gasta com ``esquema``, ou ``None`` se passa de ``TETO_NOS_DO_ESQUEMA``. Mesma
    conta de ``selo_esquema_no`` no banco (o teste compara o que sobra do orcamento la)."""
    orcamento = _Orcamento()
    try:
        _essencial(esquema, esquema, 0, orcamento)
    except _GrandeDemais:
        return None
    return orcamento.gasto


def forma_essencial(esquema: Any) -> Any:
    """A forma do esquema que a conferencia compara: sem ``ANOTACOES_DO_ESQUEMA``, com as referencias locais
    (``#/$defs/X`` e ``#/definitions/X``) resolvidas e ``required`` e ``enum`` de textos em ordem.

    E a mesma regra de ``selo_esquema_essencial`` no banco (o teste confere). Uma referencia que nao e local, sem
    alvo, em ciclo ou fundo demais vira um texto que nunca confere com o contrato; um esquema que passa do orcamento
    de nos (``TETO_NOS_DO_ESQUEMA``) vira ``GRANDE_DEMAIS``, inteiro.
    """
    try:
        return _essencial(esquema, esquema, 0, _Orcamento())
    except _GrandeDemais:
        return GRANDE_DEMAIS


def _essencial(no: Any, raiz: Any, fundo: int, orcamento: _Orcamento) -> Any:
    orcamento.gastar_peso(no)
    if fundo > FUNDO_MAXIMO_DO_ESQUEMA:
        return "fundo_demais"
    if not isinstance(no, dict):
        return no
    passos = 0
    while "$ref" in no:
        passos += 1
        if passos > FUNDO_MAXIMO_DO_ESQUEMA:
            return "ref_demais"
        ref = no["$ref"]
        achou = _REF_LOCAL.fullmatch(ref) if isinstance(ref, str) else None
        if achou is None:
            return "ref_de_fora"
        grupo = raiz.get(achou.group(1)) if isinstance(raiz, dict) else None
        alvo = grupo.get(achou.group(2)) if isinstance(grupo, dict) else None
        if not isinstance(alvo, dict):
            return "ref_sem_alvo"
        orcamento.gastar_peso(alvo)
        no = {k: v for k, v in no.items() if k != "$ref"} | alvo
    fixo = "const" in no or "enum" in no
    saida: dict[str, Any] = {}
    for chave, valor in no.items():
        if chave in ANOTACOES_DO_ESQUEMA or (chave == "type" and fixo):
            continue
        if chave == "properties" and isinstance(valor, dict):
            saida[chave] = {k: _essencial(v, raiz, fundo + 1, orcamento) for k, v in valor.items()}
        elif chave == "items" and isinstance(valor, dict):
            saida[chave] = _essencial(valor, raiz, fundo + 1, orcamento)
        elif chave in ("required", "enum") and isinstance(valor, list) and all(isinstance(x, str) for x in valor):
            saida[chave] = sorted(valor)
        else:
            saida[chave] = valor
    return saida


def esquema_confere(nome: str, campo: Literal["inputSchema", "outputSchema"], esquema: Any) -> bool:
    """O esquema declarado pelo sistema confere com o do contrato para a ferramenta ``nome`` (SELO-0.1)."""
    return bool(forma_essencial(esquema) == forma_essencial(ESQUEMAS[nome][campo]))


def definicao_confere(definicao: Mapping[str, Any]) -> bool:
    """A definicao de uma ferramenta do contrato (como o ``tools/list`` a traz) confere nos dois esquemas."""
    nome = definicao.get("name")
    if nome not in ESQUEMAS:
        return False
    return esquema_confere(str(nome), "inputSchema", definicao.get("inputSchema")) and esquema_confere(
        str(nome), "outputSchema", definicao.get("outputSchema")
    )


# --- Endereco do alvo -------------------------------------------------------------------------------------------

_URL: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)(?:\?[^#]*)?(?:#.*)?", re.DOTALL)
_PORTA: Final = re.compile(r"(.*):([0-9]*)", re.DOTALL)
_PERCENT: Final = re.compile(r"%([0-9A-Fa-f]{2})")
_NAO_RESERVADOS: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PORTA_PADRAO: Final = {"http": "80", "https": "443"}
_ESPACOS: Final = " \t\n\r\f\v"
_MINUSCULAS: Final = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _so_ascii_minusculo(texto: str) -> str:
    return texto.translate(_MINUSCULAS)


def _sem_percent(caminho: str) -> str:
    def trocar(achou: re.Match[str]) -> str:
        letra = chr(int(achou.group(1), 16))
        return letra if letra in _NAO_RESERVADOS else achou.group(0)

    return _PERCENT.sub(trocar, caminho)


def endereco_normalizado(endereco: str) -> str:
    """O endereco na forma que a comparacao entre o alvo de teste e a producao usa (critica A4).

    Esquema e nome em minusculas (so ASCII), sem usuario, sem porta padrao (80 no http, 443 no https), sem zeros a
    esquerda na porta, sem ponto no fim do nome, caminho em minusculas com os ``%XX`` de letras sem reserva
    decodificados, sem ``.`` e ``..``, sem barras repetidas nem barra no fim, e sem consulta (``?``) nem fragmento
    (``#``). E de proposito mais largo que a igualdade de URL: dois enderecos que podem ser o mesmo servico ficam
    iguais, e a declaracao e recusada. Outro nome de DNS para o mesmo servico nao se pega assim: por isso o alvo
    tambem se declara de teste (``selo_alvo``). E a mesma regra de ``selo_endereco_normalizado`` no banco.
    """
    texto = endereco.strip(_ESPACOS)
    url = _URL.fullmatch(texto)
    if url is None:
        return _so_ascii_minusculo(texto)
    esquema = _so_ascii_minusculo(url.group(1))
    autoridade = _so_ascii_minusculo(url.group(2).rsplit("@", 1)[-1])
    hospedeiro, porta = autoridade, ""
    com_porta = _PORTA.fullmatch(autoridade)
    if com_porta is not None:
        hospedeiro, porta = com_porta.group(1), com_porta.group(2)
    if porta:
        porta = porta.lstrip("0") or "0"
    if porta == _PORTA_PADRAO.get(esquema):
        porta = ""
    hospedeiro = hospedeiro.rstrip(".")
    partes: list[str] = []
    for parte in _so_ascii_minusculo(_sem_percent(url.group(3))).split("/"):
        if parte in ("", "."):
            continue
        if parte == "..":
            if partes:
                partes.pop()
            continue
        partes.append(parte)
    caminho = "/" + "/".join(partes) if partes else ""
    return f"{esquema}://{hospedeiro}{':' + porta if porta else ''}{caminho}"


def mesmo_endereco(a: str, b: str) -> bool:
    """Os dois enderecos podem ser o mesmo servico (a declaracao com o alvo igual a producao e recusada)."""
    return endereco_normalizado(a) == endereco_normalizado(b)


# --- Passagens permitidas (contradicao) -------------------------------------------------------------------------


def passagem_permitida(antes: str | None, novo: str) -> bool:
    """Uma resposta ``novo`` combina com uma resposta anterior ``antes`` sobre o mesmo numero
    (``envios_selo.chave``): a propria operacao e o pedido igual que saiu com o numero dela (critica M2).

    Pela coluna "Conta como": de nenhuma ou de ``desconhecido``, tudo; para ``desconhecido``, tudo; de
    ``em_andamento``, tudo menos ``nao_recebida`` (o numero tinha chegado); de um estado final para
    ``em_andamento``, nada; entre "aconteceu" e "em parte" (``concluida`` e ``parcial``) e delas para
    ``desfeita``, sim; entre os "nao aconteceu" (``nao_recebida``, ``recusada`` e ``parada``), sim; de
    ``desfeita``, so ``desfeita``. O resto e contradicao: o banco retira o selo (``se_contradisse``).
    Mesma regra de ``selo_passagem_permitida`` no banco.
    """
    if novo not in CONTA_COMO or (antes is not None and antes not in CONTA_COMO):
        return False
    if antes is None or antes == "desconhecido" or novo == "desconhecido":
        return True
    if antes == "em_andamento":
        return novo != "nao_recebida"
    if novo == "em_andamento":
        return False
    de, para = CONTA_COMO[antes], CONTA_COMO[novo]
    if de in ("aconteceu", "em_parte"):
        return para in ("aconteceu", "em_parte", "nao_ficou")
    if de == "nao_aconteceu":
        return para == "nao_aconteceu"
    return novo == "desfeita"


def contradiz(anteriores: Iterable[str], novo: str) -> bool:
    """A resposta ``novo`` nao combina com alguma das respostas anteriores (``respondeu``) sobre o mesmo numero: as
    da propria operacao e as de toda operacao que saiu com o mesmo ``envios_selo.chave`` (original e pedido igual)."""
    return any(not passagem_permitida(antes, novo) for antes in anteriores)


# --- Requisitos -------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Requisito:
    """Um requisito ``SELO-*``. IDs nunca mudam de numero; um retirado fica na lista, marcado.

    ``adaptado_de``: IDs do rascunho de onde vem o texto e a regra, com as mudancas ditas em ``origem``.
    ``parte_de``: IDs do rascunho que inspiram o requisito sem que ele seja adaptado deles. Os dois so aparecem na
    secao de fontes do contrato (critica B6)."""

    id: str
    niveis: tuple[int, ...]
    vale_quando: str
    texto: str
    origem: str
    adaptado_de: tuple[str, ...] = ()
    parte_de: tuple[str, ...] = ()
    testado: bool = True
    retirado: bool = False


REQUISITOS: Final[tuple[Requisito, ...]] = (
    Requisito(
        "SELO-0.1",
        (2, 3),
        "sempre",
        "O sistema lista em tools/list, no endereço de produção e no alvo de teste, as ferramentas do contrato do"
        " nível pedido (selo_alvo e selo_consultar; no nível 3, também selo_bloqueio), com os nomes exatos e os"
        " esquemas publicados. Os esquemas são comparados na forma essencial: título, descrição, exemplos,"
        " definições e os limites que o MCP-CLARO confere sozinho não contam; tipo, campos obrigatórios,"
        " propriedades, itens, valores fixos e padrão contam.",
        "Próprio.",
    ),
    Requisito(
        "SELO-0.2",
        (2, 3),
        "sempre",
        "O sistema entrega ao ADMIN uma declaração de teste. Ela traz o alvo de teste, em que a bateria pode"
        " escrever, e, para cada ferramenta de ação, os argumentos de teste e uma leitura que mostra o rastro"
        " (quantos objetos ou mudanças). A leitura do rastro é de uma ferramenta que só lê.",
        "Próprio. A ideia de declaração vem do rascunho (seção 4).",
    ),
    Requisito(
        "SELO-0.3",
        (2, 3),
        "sempre",
        'O alvo de teste se declara de teste. selo_alvo responde {"contrato": "selo/1", "alvo": "teste"} no alvo'
        ' de teste e {"contrato": "selo/1", "alvo": "producao"} no endereço de produção. No modo de ferramentas'
        ' de teste, cada ferramenta de teste devolve no _meta do resultado "br.com.claro.mcp-claro/alvo":'
        ' "teste". Sem essa marca, a bateria não escreve.',
        "Próprio.",
    ),
    Requisito(
        "SELO-2.1",
        (2,),
        "sempre",
        'Toda ferramenta de ação aceita o número da operação em params._meta["br.com.claro.mcp-claro/operacao"],'
        " e não só as que criam. O selo de nível 2 ou 3 é só para sistema com ferramenta de ação e chave por"
        " pessoa.",
        "Próprio. Parte de uma recomendação do rascunho, que pede o número só em criação, e de um candidato da"
        " seção 12 (número em toda escrita cuja repetição tem efeito a mais).",
        parte_de=("MWS-RETRY-1",),
    ),
    Requisito(
        "SELO-2.2",
        (2,),
        "sempre",
        "Uma escrita repetida com o mesmo número e os mesmos argumentos é aplicada no máximo uma vez e responde"
        " com o desfecho da primeira. Número novo é escrita nova. O mesmo número com argumentos diferentes é"
        " recusado (erro de execução ou erro JSON-RPC), sem aplicar nada.",
        "Adaptado, com exigência maior: o rascunho pede o desfecho da primeira só quando ela aplicou a escrita e o"
        " servidor consegue saber disso; o contrato pede sempre.",
        adaptado_de=("MWS-RETRY-2",),
    ),
    Requisito(
        "SELO-2.3",
        (2,),
        "a ferramenta pode responder input_required",
        "A resposta input_required não pôs em efeito, naquela chamada, nada do que foi pedido.",
        "Adaptado. A bateria só vê de fora: o melhor resultado deste caso é não observado.",
        adaptado_de=("MWS-RETRY-3",),
    ),
    Requisito(
        "SELO-2.4",
        (2,),
        "sempre",
        "selo_consultar não muda nada e diz o estado verdadeiro no vocabulário do contrato. O sistema registra o"
        " número quando a chamada chega, antes de escrever: uma chamada que chegou e não terminou é em_andamento."
        " nao_recebida vale só para número que o sistema nunca recebeu e nunca vai pôr em efeito. Quando não sabe,"
        " responde desconhecido. Nunca responde um estado final que não é verdadeiro, nem desconhecido quando"
        " sabe.",
        "Próprio. Usa a ideia de desfecho assentado do rascunho (seção 4) e a regra de não dar por não sabido um"
        " desfecho assentado.",
        parte_de=("MWS-RES-2",),
    ),
    Requisito(
        "SELO-2.5",
        (2,),
        "sempre",
        "Depois de responder nao_recebida a um número, o sistema recusa, sem aplicar, toda chamada que chegue"
        " depois com ele. O recibo dessa chamada diz nao_recebida (ou parada, se foi o aviso de bloqueio que"
        " queimou o número).",
        "Próprio.",
    ),
    Requisito(
        "SELO-2.6",
        (2,),
        "sempre",
        "O sistema guarda o registro de cada número por pelo menos 30 dias e diz o prazo na descrição de"
        " selo_consultar. Declarado, não testado.",
        "Próprio. O rascunho não testa a guarda.",
        testado=False,
    ),
    Requisito(
        "SELO-2.7",
        (2,),
        "a ferramenta de ação declara idempotentHint: true",
        "Chamada de novo com os mesmos argumentos, não tem efeito a mais.",
        "Adaptado. A bateria só vê de fora: o melhor resultado deste caso é não observado.",
        adaptado_de=("MWS-ANN-3",),
    ),
    Requisito(
        "SELO-3.1",
        (3,),
        "sempre",
        "A ferramenta de ação só responde sucesso quando o que foi pedido está em efeito. Falha total ou parcial"
        " nunca vem como sucesso. O erro diz o que ficou aplicado, ou que não se sabe, e não manda repetir sem ler"
        " antes ou sem o mesmo número. Recusa do sistema por trás, ou de regra da ferramenta, volta com"
        " isError: true.",
        "Adaptado.",
        adaptado_de=("MWS-RES-1", "MWS-RES-2"),
    ),
    Requisito(
        "SELO-3.2",
        (3,),
        "sempre",
        "Toda resposta de ação traz o recibo com o número, o estado e, numa criação, o identificador de cada"
        " objeto criado, mesmo quando um passo depois falha.",
        "Adaptado. O recibo é inspirado numa recomendação do rascunho, sem adaptá-la: o contrato não pede os"
        " campos mudados nem a versão nova.",
        adaptado_de=("MWS-RES-3",),
        parte_de=("MWS-RES-4",),
    ),
    Requisito(
        "SELO-3.3",
        (3,),
        "a ferramenta escreve vários itens ou faz vários passos",
        "Uma escrita que não se completa não responde sucesso e diz no recibo quantos itens foram aplicados e"
        " quantos não. Tudo ou nada é recomendado e não conta para o selo.",
        "Adaptado, mais fraco num ponto: o rascunho pede os nomes dos itens aplicados e dos não aplicados; o"
        " contrato pede as contagens, e o MCP-CLARO guarda só a contagem.",
        adaptado_de=("MWS-ATOM-1",),
    ),
    Requisito(
        "SELO-3.4",
        (3,),
        "a ferramenta pode recusar por conflito",
        "Escrita recusada por conflito não é aplicada e volta com isError: true, dizendo que não foi aplicada.",
        "Adaptado.",
        adaptado_de=("MWS-CONC-4",),
    ),
    Requisito(
        "SELO-3.5",
        (3,),
        "sempre",
        "selo_consultar também diz, em depois, o que aconteceu depois de receber, como entregue, devolvida,"
        " desfeita ou parada, com a hora e só no vocabulário do contrato. O sistema não manda texto livre.",
        "Próprio. No rascunho, os requisitos de resultado tratam só da própria chamada.",
    ),
    Requisito(
        "SELO-3.6",
        (3,),
        "sempre",
        "selo_bloqueio: para cada número ainda não concluído, o sistema não o põe em efeito depois de responder e"
        " responde parada. Para os outros, responde o estado verdadeiro; para número que não recebeu, nao_recebida,"
        " que também fica queimado. Repetir o aviso com o mesmo bloqueio não tem efeito a mais e dá as mesmas"
        " respostas. Depois do aviso, chamada que chegue com um desses números é recusada sem aplicar, e o recibo"
        " dela diz parada (ou nao_recebida).",
        "Próprio. A autorização fica fora do escopo do rascunho (seção 2).",
    ),
)

RECOMENDACOES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("Tudo ou nada numa escrita de vários objetos.", ("MWS-ATOM-2",)),
    (
        "Responder só sobre números recebidos pela mesma identidade (a mesma pessoa ou o mesmo programa), e não"
        " pelo mesmo texto de chave: uma chave renovada continua sendo a mesma identidade.",
        (),
    ),
    ("Dizer na descrição de cada ferramenta de ação que uma repetição deve levar o mesmo número.", ("MWS-RETRY-1",)),
)
"""Recomendacoes do contrato que nao contam para o selo."""

IDS_DO_RASCUNHO_USADOS: Final = frozenset(
    {
        "MWS-RETRY-1",
        "MWS-RETRY-2",
        "MWS-RETRY-3",
        "MWS-ANN-3",
        "MWS-RES-1",
        "MWS-RES-2",
        "MWS-RES-3",
        "MWS-RES-4",
        "MWS-ATOM-1",
        "MWS-ATOM-2",
        "MWS-CONC-4",
    }
)
"""Os 11 IDs conferidos na fonte que o contrato cita (D1). ``MWS-CFG-1`` a ``MWS-CFG-4`` foram conferidos e nao
sao usados."""

# --- Casos da bateria -------------------------------------------------------------------------------------------

Resultado = Literal["cumpriu", "falhou", "nao_se_aplica", "nao_testado", "nao_observado"]
RESULTADOS: Final[tuple[Resultado, ...]] = ("cumpriu", "falhou", "nao_se_aplica", "nao_testado", "nao_observado")
"""Veredictos proprios do MCP-CLARO, que nao sao os do rascunho."""

RAZOES: Final = (
    "sem_condicao_declarada",
    "condicao_nao_forcada",
    "sem_resposta",
    "tentativas_excluidas",
    "interposicao",
    "fora_do_alvo",
    "teto",
    "sem_segundo_efeito",
    "poucos_pares",
    "declarado",
)
"""Por que um caso deu ``nao_se_aplica`` ou ``nao_testado`` (lista fechada). ``sem_segundo_efeito``: C-2.2b numa
ferramenta cuja repeticao nao muda nada (critica M6). ``poucos_pares``: C-2.2d com menos de ``PARES_MINIMOS``
pares sobrepostos (M5). ``declarado``: so declarado pelo sistema, sem teste."""


@dataclass(frozen=True, slots=True)
class Caso:
    """Um caso da bateria.

    ``nao_se_aplica_ate``: o maior nivel de teste em que ``nao_se_aplica`` aprova este caso (0: nunca; 2: so num
    teste de nivel 2, como o C-2.4b; 3: em qualquer nivel). ``maximo``: o melhor resultado que o metodo permite
    (``nao_observado`` nos casos que a bateria so ve de fora, critica M5); ``cumpriu`` num caso de maximo
    ``nao_observado`` e recusado pelo banco. ``o_que_testamos``: o texto da tela do relatorio (com acento)."""

    caso: str
    requisito: str
    nivel: int
    escopo: Literal["sistema", "ferramenta"]
    obrigatorio: bool
    nao_se_aplica_ate: int
    maximo: Literal["cumpriu", "nao_observado"]
    o_que_testamos: str


CASOS: Final[tuple[Caso, ...]] = (
    Caso(
        "C-0.1",
        "SELO-0.1",
        2,
        "sistema",
        True,
        0,
        "cumpriu",
        "Lista as ferramentas na produção e no alvo de teste e confere os nomes e os esquemas das ferramentas do"
        " contrato.",
    ),
    Caso(
        "C-0.2",
        "SELO-0.1",
        2,
        "sistema",
        True,
        0,
        "cumpriu",
        "No ambiente de teste, as ferramentas de ação são as mesmas da produção. Com ferramentas de teste, cada"
        " uma tem o esquema de entrada da ferramenta real e um nome que não é de nenhuma ferramenta revisada.",
    ),
    Caso(
        "C-0.3",
        "SELO-2.4",
        2,
        "sistema",
        True,
        0,
        "cumpriu",
        "Na produção, só a lista de ferramentas e uma consulta de controle com um número novo, que dá nao_recebida.",
    ),
    Caso(
        "C-0.4",
        "SELO-0.3",
        2,
        "sistema",
        True,
        0,
        "cumpriu",
        "Antes da primeira escrita, o alvo se declara de teste e a produção se declara de produção.",
    ),
    Caso(
        "C-2.1",
        "SELO-2.1",
        2,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "Chama com um número: a chamada é aceita e o rastro sobe um.",
    ),
    Caso(
        "C-2.2a",
        "SELO-2.2",
        2,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "Repete a mesma chamada com o mesmo número: o rastro não muda, a resposta não é erro se a primeira não foi,"
        " e a consulta diz concluida.",
    ),
    Caso(
        "C-2.2b",
        "SELO-2.2",
        2,
        "ferramenta",
        True,
        3,
        "cumpriu",
        "Os mesmos argumentos com um número novo: a escrita é aplicada de novo ou recusada pelo próprio mérito.",
    ),
    Caso(
        "C-2.2c",
        "SELO-2.2",
        2,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "Outros argumentos com um número já usado: a chamada é recusada e o rastro não muda.",
    ),
    Caso(
        "C-2.2d",
        "SELO-2.2",
        2,
        "ferramenta",
        False,
        0,
        "nao_observado",
        "Pares de chamadas ao mesmo tempo, cada par com o mesmo número: nenhum é aplicado duas vezes.",
    ),
    Caso(
        "C-2.3",
        "SELO-2.3",
        2,
        "ferramenta",
        True,
        3,
        "nao_observado",
        "Quando a ferramenta pede dados no meio, nada do pedido foi posto em efeito.",
    ),
    Caso(
        "C-2.4a",
        "SELO-2.4",
        2,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "A consulta de um número aplicado dá concluida, a de um número nunca mandado dá nao_recebida, e a consulta"
        " não muda o rastro.",
    ),
    Caso(
        "C-2.4b",
        "SELO-2.4",
        2,
        "ferramenta",
        True,
        2,
        "cumpriu",
        "Uma chamada que o sistema recusa, e depois a consulta do número dela: dá recusada (ou nao_recebida).",
    ),
    Caso(
        "C-2.5",
        "SELO-2.5",
        2,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "Consulta um número que nunca foi mandado (nao_recebida) e depois chama com ele: a chamada é recusada e o"
        " rastro não muda.",
    ),
    Caso(
        "C-2.6a",
        "SELO-2.4",
        2,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "Manda a escrita e corta a conexão logo depois, sem esperar a resposta; consulta o número na hora e depois"
        " de um intervalo, lê o rastro e repete o número: a consulta nunca diz nao_recebida de uma escrita que foi"
        " aplicada, e a repetição não aplica de novo.",
    ),
    Caso(
        "C-2.6b",
        "SELO-2.4",
        2,
        "ferramenta",
        True,
        3,
        "cumpriu",
        "Com uma escrita lenta declarada: a consulta durante a escrita dá em_andamento ou desconhecido.",
    ),
    Caso(
        "C-2.7",
        "SELO-2.7",
        2,
        "ferramenta",
        True,
        3,
        "nao_observado",
        "Numa ferramenta que se declara idempotente: duas chamadas iguais com números diferentes, e a segunda não"
        " muda o rastro.",
    ),
    Caso(
        "C-3.1a",
        "SELO-3.1",
        3,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "O sucesso só vem com o que foi pedido em efeito, lido de volta.",
    ),
    Caso(
        "C-3.1b",
        "SELO-3.1",
        3,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "Uma chamada que o sistema recusa: volta como erro, com o recibo recusada, e o rastro não muda.",
    ),
    Caso(
        "C-3.2",
        "SELO-3.2",
        3,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "O recibo está presente e combina com o erro ou o sucesso; numa criação, cada objeto do recibo existe.",
    ),
    Caso(
        "C-3.3",
        "SELO-3.3",
        3,
        "ferramenta",
        True,
        3,
        "cumpriu",
        "Uma escrita de vários itens com um item ruim: não vem sucesso, e as contagens do recibo batem com o rastro.",
    ),
    Caso(
        "C-3.4",
        "SELO-3.4",
        3,
        "ferramenta",
        True,
        3,
        "cumpriu",
        "Depois de mostrar que os dois campos usados são independentes, uma escrita velha é recusada como erro e"
        " não é aplicada.",
    ),
    Caso(
        "C-3.5",
        "SELO-3.5",
        3,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "A consulta de um número aplicado traz o que aconteceu depois, só no vocabulário do contrato e de acordo"
        " com o recibo.",
    ),
    Caso(
        "C-3.6a",
        "SELO-3.6",
        3,
        "ferramenta",
        True,
        0,
        "cumpriu",
        "O aviso de bloqueio com um número concluído e um nunca mandado: dá concluida e nao_recebida; o aviso"
        " repetido dá as mesmas respostas; a chamada com o número nunca mandado é recusada; o rastro não muda.",
    ),
    Caso(
        "C-3.6b",
        "SELO-3.6",
        3,
        "ferramenta",
        True,
        3,
        "cumpriu",
        "Com um efeito adiado declarado: o aviso de bloqueio chega antes do efeito, dá parada, e o efeito não"
        " acontece.",
    ),
)
"""Os casos de ``selo/1``. O banco tem a mesma tabela (``selo_casos``)."""

CASOS_POR_NOME: Final[Mapping[str, Caso]] = {c.caso: c for c in CASOS}


@dataclass(frozen=True, slots=True)
class NaoTestado:
    """O que a bateria nunca testa e que o relatorio e o cartao do selo sempre mostram (sem IDs do rascunho)."""

    item: str
    razao: str
    texto: str


NAO_TESTADOS: Final[tuple[NaoTestado, ...]] = (
    NaoTestado(
        "guarda_dos_numeros",
        "declarado",
        "Por quanto tempo o sistema guarda os números (declarado pelo sistema).",
    ),
    NaoTestado(
        "interposicao_e_reinicio",
        "interposicao",
        "Resposta perdida entre o sistema e o que está por trás dele, e queda no meio da escrita (depende da equipe"
        " do sistema).",
    ),
)
"""``testes_selo.nao_testado`` e sempre exatamente esta lista, em ``{"item", "razao"}`` (o banco confere)."""


def nao_testado_gravado() -> list[dict[str, str]]:
    """O valor de ``testes_selo.nao_testado``, em ordem de ``item``."""
    return [{"item": n.item, "razao": n.razao} for n in sorted(NAO_TESTADOS, key=lambda n: n.item)]


CAMPOS_DO_ITEM: Final = frozenset({"caso", "ferramenta", "resultado", "razao", "tentativas", "excluidas"})
"""Cada item de ``testes_selo.casos``: o caso, a ferramenta revisada (uuid; nulo nos casos de escopo sistema), o
resultado, a razao (do vocabulario, ou nulo), as tentativas e as tentativas excluidas por prazo. Nada mais: o
registro nao guarda argumento nem texto do sistema."""


def resultado_aprova(caso: Caso, resultado: str, nivel_do_teste: int) -> bool:
    """O resultado conta como aprovado para um caso obrigatorio num teste de nivel ``nivel_do_teste``."""
    return resultado == caso.maximo or (resultado == "nao_se_aplica" and nivel_do_teste <= caso.nao_se_aplica_ate)


def _inteiro(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0


def problema_nos_casos(itens: Any, ferramentas: Sequence[uuid.UUID | str]) -> str | None:
    """O primeiro problema na forma de ``testes_selo.casos``, ou None. Mesma regra de ``selo_casos_problema``.

    Codigos: ``nao_e_lista``, ``item_nao_e_objeto``, ``campos_do_item``, ``caso_desconhecido``,
    ``ferramenta_do_caso``, ``resultado_fora``, ``razao_fora``, ``razao_faltando``, ``acima_do_maximo``,
    ``nao_se_aplica_sem_lugar``, ``tentativas`` e ``caso_repetido``.
    """
    if not isinstance(itens, list):
        return "nao_e_lista"
    conhecidas = {str(f) for f in ferramentas}
    vistos: set[tuple[str, str | None]] = set()
    for item in itens:
        if not isinstance(item, dict):
            return "item_nao_e_objeto"
        if set(item) != CAMPOS_DO_ITEM:
            return "campos_do_item"
        caso = CASOS_POR_NOME.get(item["caso"]) if isinstance(item["caso"], str) else None
        if caso is None:
            return "caso_desconhecido"
        ferramenta = item["ferramenta"]
        if caso.escopo == "sistema":
            if ferramenta is not None:
                return "ferramenta_do_caso"
        elif not isinstance(ferramenta, str) or ferramenta not in conhecidas:
            return "ferramenta_do_caso"
        resultado, razao = item["resultado"], item["razao"]
        if resultado not in RESULTADOS:
            return "resultado_fora"
        if razao is not None and razao not in RAZOES:
            return "razao_fora"
        if razao is None and resultado in ("nao_se_aplica", "nao_testado"):
            return "razao_faltando"
        if resultado == "cumpriu" and caso.maximo == "nao_observado":
            return "acima_do_maximo"
        if resultado == "nao_se_aplica" and caso.nao_se_aplica_ate < caso.nivel:
            return "nao_se_aplica_sem_lugar"
        tentativas, excluidas = item["tentativas"], item["excluidas"]
        if not (_inteiro(tentativas) and _inteiro(excluidas) and excluidas <= tentativas):
            return "tentativas"
        if resultado in ("cumpriu", "falhou", "nao_observado") and tentativas < 1:
            return "tentativas"
        chave = (caso.caso, ferramenta)
        if chave in vistos:
            return "caso_repetido"
        vistos.add(chave)
    return None


def casos_aprovam(itens: Sequence[Mapping[str, Any]], ferramentas: Sequence[uuid.UUID | str], nivel: int) -> bool:
    """O criterio do MCP-CLARO (desenho 2.4, com as correcoes M5 e M6). Mesma regra de ``selo_casos_aprovam``.

    Nenhum item, de nenhum caso, tem ``falhou``; e cada caso obrigatorio de nivel ate ``nivel`` tem, em cada
    ferramenta (escopo ferramenta) ou no sistema (escopo sistema), um item que aprova (``resultado_aprova``). Um
    obrigatorio ``nao_testado`` ou ``nao_observado`` (fora do maximo) reprova. Supoe itens validos
    (``problema_nos_casos``)."""
    if any(i.get("resultado") == "falhou" for i in itens):
        return False
    por_chave = {(str(i.get("caso")), i.get("ferramenta")): str(i.get("resultado")) for i in itens}
    for caso in CASOS:
        if not caso.obrigatorio or caso.nivel > nivel:
            continue
        alvos: list[str | None] = [None] if caso.escopo == "sistema" else [str(f) for f in ferramentas]
        for alvo in alvos:
            resultado = por_chave.get((caso.caso, alvo))
            if resultado is None or not resultado_aprova(caso, resultado, nivel):
                return False
    return True


def contagem_minima_de_chamadas(itens: Sequence[Mapping[str, Any]]) -> int:
    """O banco exige ``testes_selo.chamadas`` ao menos a soma das tentativas dos itens (critica B3)."""
    return sum(int(i.get("tentativas") or 0) for i in itens)


# --- Assinaturas dos outros grupos (contrato; o G0 nao implementa) ----------------------------------------------


class LigacaoComNumero(Protocol):
    """G1, ``ligacao/cliente.py`` e ``ligacao/ponto.py`` (``Ligacao``, ``ClienteLocal``, ``SemSala``; ``ClienteSala``
    em ``ligacao/sala_cliente.py`` recusa ``chave_operacao`` ate o contrato da sala v2, G7).

    ``chave_operacao``: o numero da operacao (``envios_selo.chave``, ``numero_valido``). Vai so em
    ``params._meta[CHAVE_OPERACAO]``, com o valor recebido; nunca nos argumentos nem em cabecalho. Fora do formato
    uuid, ``ValueError`` antes da rede. Sem ele (``None``), o corpo e igual ao de hoje, byte a byte. Num terceiro,
    ``_sem_rastro`` deixa passar so essa chave, so quando o despachante a mandou e so com valor uuid."""

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Segredo,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: dict[str, Any] | None = None,
        teto_resposta: int = ...,
        chave_operacao: str | None = None,
    ) -> Resposta: ...


class SeloLido(Protocol):
    """G2, ``selo/vigente.py``: o selo lido de ``selo_vigente`` (ou ``selos_vigentes``), com os campos do retrato
    (``RETRATO_DO_SELO``). ``motivo`` e um de ``MOTIVOS_DO_SELO``, ou ``fase_desligada`` no efetivo de uma pessoa
    real com ``FEATURE_PHASE4_BADGES`` desligada."""

    @property
    def nivel(self) -> int: ...
    @property
    def teste_id(self) -> uuid.UUID | None: ...
    @property
    def testado_em(self) -> datetime | None: ...
    @property
    def vale_ate(self) -> datetime | None: ...
    @property
    def modo(self) -> str | None: ...
    @property
    def motivo(self) -> str: ...


MOTIVO_FASE_DESLIGADA: Final = "fase_desligada"

MOTIVOS_DO_RETRATO: Final = (*MOTIVOS_DO_SELO, MOTIVO_FASE_DESLIGADA)
"""O ``motivo`` que o retrato do selo (``RETRATO_DO_SELO``) aceita: os de ``selo_vigente`` e o do efetivo com a
chave da fase desligada. O banco aceita a mesma lista na ficha."""


class CalculaEfetivo(Protocol):
    """G2, ``selo/vigente.py``: ``efetivo(vigente, *, ligada, pessoa_teste)``.

    O selo efetivo de uma operacao: o vigente se ``FEATURE_PHASE4_BADGES`` estiver ligada ou se a pessoa for de
    teste (``pessoas.teste``); nivel 1 com motivo ``MOTIVO_FASE_DESLIGADA`` nos outros casos. Nunca sobe o nivel."""

    def __call__(self, vigente: SeloLido, *, ligada: bool, pessoa_teste: bool) -> SeloLido: ...


ESCADA_DA_CONSULTA_MINUTOS: Final = (1, 2, 5, 15, 30, 60, 120, 240)
"""A escada fixa da consulta (desenho 5.2), por operacao. A rota (G3) e as rotinas (G5) contam igual, pelas funcoes
abaixo (``proxima_consulta``), sobre as linhas de ``informacoes_sistema`` da operacao em ordem de ``(gravado_em,
id)``:

- as tentativas ja feitas sao so as linhas de origem ``consulta`` com desfecho em ``DESFECHOS_QUE_GASTAM_TENTATIVA``
  (``tentativas_da_consulta``). O recibo e a resposta ao aviso nunca contam como tentativa, nem ``sem_chave``,
  ``sala_fora`` e ``sistema_fora``. So elas contam para o maximo (``BADGE_LOOKUP_MAX_ATTEMPTS``);
- a hora base e a ultima linha da operacao, de qualquer origem e de qualquer desfecho, ou, sem nenhuma,
  ``envios_selo.gravado_em`` (``hora_base_da_consulta``). Uma linha que nao gasta tentativa (``sem_chave``,
  ``sala_fora``) empurra a vez seguinte, sem gastar a escada;
- o degrau da espera e contado por TODAS as linhas de origem ``consulta`` da operacao, as que gastam e as que nao
  gastam tentativa (``degrau_da_consulta``; decisao 3 do coordenador no retoque da etapa 3): a vez ``n`` (contada de
  0) vai ``ESCADA_DA_CONSULTA_MINUTOS[n]`` minutos depois da hora base, e, passado o fim da escada, vale o ultimo
  degrau. Sem linhas que nao gastam, o degrau e o numero de tentativas, como antes. Com a chave da pessoa apagada
  (``sem_chave`` a cada pergunta), as linhas ficam poucas: depois do oitavo degrau, uma a cada 240 minutos;
- critica A3.3 (decisao do coordenador, etapa 1): a primeira consulta, e toda consulta, so vai depois do prazo do
  envio (``RESPONSE_MAX_WAIT_SECONDS``, o prazo da chamada na porta de saida) mais
  ``MARGEM_DA_CONSULTA_SEGUNDOS``, contados de ``envios_selo.gravado_em``: antes disso a chamada ainda pode estar a
  caminho, e um sistema honesto diria ``nao_recebida`` de uma escrita que vai acontecer;
- a escada acaba com uma resposta final (``resposta_que_encerra``): o ultimo estado de ``ESTADOS_FINAIS``, com
  ``nao_recebida`` so confirmada por duas respostas assim com ao menos o primeiro degrau
  (``ESCADA_DA_CONSULTA_MINUTOS[0]`` minutos) entre a primeira e a ultima (A3.3, ``nao_recebida_confirmada``). A
  segunda pergunta pega o sistema que diz ``nao_recebida`` de uma escrita em andamento: se ele responder outra
  coisa, o banco ve a contradicao. So as respostas de origem ``recibo`` e ``consulta`` contam
  (``ORIGENS_DA_RESPOSTA_FINAL``): a resposta ao aviso de bloqueio nunca encerra a escada (decisao 1 do coordenador
  no retoque da etapa 3);
- a escada tambem acaba quando a ultima linha de origem ``consulta`` e ``sistema_fora`` (critica A2: a escada para, e
  a decisao fica com a pessoa; ``parou_por_sistema_fora``). Uma resposta ao aviso de bloqueio gravada depois nao a
  reabre (decisao do coordenador na conferencia final da etapa 3r). Acaba ainda quando outro protocolo saiu com o
  mesmo numero depois (``selo_substituto``, lido pela rota no banco), no maximo de tentativas que gastam e pelo
  tempo: nenhuma consulta vai depois de ``envios_selo.gravado_em`` mais ``REAPROVEITAR_NUMERO_DIAS`` dias, a mesma
  janela do pedido igual (``escada_vencida``; decisao 3).

A mesma regra vale no banco, para a tela e o pedido igual: ``selo_estado_final`` (o que resolve o "estamos
confirmando" e o que o botao "Registrar o que o sistema informou" registra) e ``medicoes.v_fila_agora`` contam como
``resposta_que_encerra``, sobre as respostas de origem ``recibo`` e ``consulta`` da operacao; a queima do numero no
pedido igual (``envios_selo_nasce``) conta as respostas sobre o numero (a original e os pedidos iguais que sairam com
ele), tambem sem as do aviso. Uma ``nao_recebida`` sozinha aparece como dita pelo sistema, com "continuamos
perguntando", sem o botao de um toque, e o pedido igual sai com o mesmo numero. A resposta ao aviso continua gravada
e mostrada (``Comprovante.avisos``); so nao e final. Um sistema que respondeu ``parada`` ao aviso e fez a escrita e
pego pela consulta seguinte: o gatilho de contradicao da 0007 compara as linhas de toda origem.

Fim da escada sem resposta que encerra (decisao do coordenador, retoque do G0): quando ``e_a_ultima_consulta`` ja
passou e nenhuma resposta encerra (inclusive uma ``nao_recebida`` sozinha, sem a segunda), a operacao fica como na
Fase 2: ``selo_estado_final`` continua ``None``, a decisao e da pessoa, e a tela (G6) troca "continuamos perguntando"
por "o sistema nao confirmou; a decisao e sua". Nada e resolvido sozinho, e o pedido igual sai com o mesmo numero. A
escada acabada pelo tempo (``escada_vencida``) e tratada igual na tela (G6) e em ``Operacao.consulta_acabou`` (G4); se
ela acabou sem chegar ao maximo de tentativas que gastam, a operacao nao entra na conta da retirada por
``nao_respondeu_consultas``: o sistema nao foi perguntado o bastante.

Gravacao das respostas (G3 e G5): cada transacao grava ``informacoes_sistema`` de UM sistema so, e por ultimo (depois
dos outros UPDATE e INSERT da transacao). A trava consultiva da 0007 e por sistema: duas transacoes que gravassem
respostas de dois sistemas em ordens opostas poderiam se esperar em ciclo."""

MARGEM_DA_CONSULTA_SEGUNDOS: Final = 60
"""Folga, alem do prazo do envio, antes de qualquer consulta sobre a operacao (critica A3.3)."""

ORIGENS_DA_RESPOSTA_FINAL: Final[tuple[Origem, ...]] = ("recibo", "consulta")
"""As origens cuja resposta encerra a escada, resolve o "estamos confirmando" e queima o numero (decisao 1 do
coordenador no retoque da etapa 3, desenho 5.2). A resposta ao aviso (origem ``aviso``) fica gravada e mostrada, mas
nunca e final: um sistema que diz ``parada`` e escreve mesmo assim e pego pela consulta seguinte."""


def _minutos_do_degrau(degrau: int) -> int:
    return ESCADA_DA_CONSULTA_MINUTOS[min(degrau, len(ESCADA_DA_CONSULTA_MINUTOS) - 1)]


def espera_da_consulta(feitas: int, maximo: int) -> int | None:
    """Minutos de espera antes da proxima consulta, depois de ``feitas`` tentativas que gastam a vez, ou ``None``
    quando a escada acabou (``feitas >= maximo``, o ``BADGE_LOOKUP_MAX_ATTEMPTS``). Sem linhas que nao gastam
    tentativa, e a espera de ``proxima_consulta``; com elas, o degrau sai de ``degrau_da_consulta``."""
    if feitas < 0:
        raise ValueError("feitas nao e negativo")
    if feitas >= maximo:
        return None
    return _minutos_do_degrau(feitas)


@dataclass(frozen=True, slots=True)
class LinhaInformada:
    """Uma linha de ``informacoes_sistema`` da operacao, so com o que a escada da consulta le."""

    origem: str
    desfecho: str
    estado: str | None
    gravado_em: datetime


def tentativas_da_consulta(linhas: Iterable[LinhaInformada]) -> int:
    """As tentativas ja gastas na escada: as linhas de origem ``consulta`` com desfecho em
    ``DESFECHOS_QUE_GASTAM_TENTATIVA``."""
    return sum(1 for x in linhas if x.origem == "consulta" and x.desfecho in DESFECHOS_QUE_GASTAM_TENTATIVA)


def degrau_da_consulta(linhas: Iterable[LinhaInformada]) -> int:
    """O degrau da proxima espera: todas as linhas de origem ``consulta``, as que gastam e as que nao gastam tentativa
    (``sem_chave``, ``sala_fora``). Uma linha que nao gasta nao gasta o maximo, mas sobe a espera: sem a chave da
    pessoa, a escada nao pergunta a cada minuto sem fim (decisao 3 do coordenador no retoque da etapa 3)."""
    return sum(1 for x in linhas if x.origem == "consulta")


def hora_base_da_consulta(linhas: Sequence[LinhaInformada], enviado_em: datetime) -> datetime:
    """A hora de onde conta o degrau: a ultima linha da operacao, de qualquer origem e desfecho, ou o envio."""
    return max((x.gravado_em for x in linhas), default=enviado_em)


def escada_vencida(enviado_em: datetime, agora: datetime) -> bool:
    """A escada da consulta acabou pelo tempo: ``agora`` (o ``now()`` do banco) ja passou de ``enviado_em``
    (``envios_selo.gravado_em``) mais ``REAPROVEITAR_NUMERO_DIAS`` dias, a mesma janela do pedido igual. Depois
    disso nenhuma consulta vai, e a tela e ``Operacao.consulta_acabou`` (G4) dizem "o sistema nao confirmou; a
    decisao e sua" (decisao 3 do coordenador no retoque da etapa 3)."""
    return agora >= enviado_em + timedelta(days=REAPROVEITAR_NUMERO_DIAS)


def nao_recebida_confirmada(horas: Iterable[datetime]) -> bool:
    """Critica A3.3: duas ou mais respostas ``nao_recebida`` (``horas``, o ``gravado_em`` de cada uma), com ao menos
    ``ESCADA_DA_CONSULTA_MINUTOS[0]`` minutos entre a primeira e a ultima. Mesma regra de
    ``selo_nao_recebida_confirmada`` no banco."""
    vistas = list(horas)
    return len(vistas) >= 2 and max(vistas) - min(vistas) >= timedelta(minutes=ESCADA_DA_CONSULTA_MINUTOS[0])


def resposta_que_encerra(linhas: Sequence[LinhaInformada]) -> str | None:
    """O estado final que encerra a escada (critica A3.3), ou ``None``: o ultimo (pela ordem de ``linhas``) estado de
    ``ESTADOS_FINAIS`` entre as respostas de origem ``recibo`` e ``consulta`` (``ORIGENS_DA_RESPOSTA_FINAL``), com
    ``nao_recebida`` so quando ``nao_recebida_confirmada`` por elas. A resposta ao aviso nunca encerra nem confirma.
    Mesma regra de ``selo_estado_final`` no banco, sobre as linhas da operacao."""
    finais = [
        x
        for x in linhas
        if x.origem in ORIGENS_DA_RESPOSTA_FINAL and x.desfecho == "respondeu" and x.estado in ESTADOS_FINAIS
    ]
    confirmada = nao_recebida_confirmada(x.gravado_em for x in finais if x.estado == "nao_recebida")
    valem = [x for x in finais if x.estado != "nao_recebida" or confirmada]
    return valem[-1].estado if valem else None


def parou_por_sistema_fora(linhas: Sequence[LinhaInformada]) -> bool:
    """A ultima linha de origem ``consulta`` da operacao e ``sistema_fora``: a escada parou de vez (critica A2), e a
    decisao fica com a pessoa. So a consulta grava ``sistema_fora``; a resposta ao aviso de bloqueio que chega depois
    (o sistema voltou a ``ativo`` e foi avisado) nao reabre a escada (decisao do coordenador na conferencia final da
    etapa 3r). ``linhas`` em ordem de ``(gravado_em, id)``."""
    consultas = [x for x in linhas if x.origem == "consulta"]
    return bool(consultas) and consultas[-1].desfecho == "sistema_fora"


def proxima_consulta(
    linhas: Sequence[LinhaInformada], *, enviado_em: datetime, maximo: int, prazo_envio_segundos: float
) -> datetime | None:
    """Quando a proxima consulta sobre a operacao pode ir, ou ``None`` quando nao ha mais o que perguntar (resposta
    final, ``sistema_fora`` na ultima linha de consulta (``parou_por_sistema_fora``), maximo de tentativas que
    gastam, ou a vez passaria do fim pelo tempo, ``escada_vencida``). ``linhas`` em ordem de ``(gravado_em, id)``;
    ``maximo`` e ``BADGE_LOOKUP_MAX_ATTEMPTS``;
    ``prazo_envio_segundos`` e ``RESPONSE_MAX_WAIT_SECONDS``. Veja ``ESCADA_DA_CONSULTA_MINUTOS``. A rota (G3) recusa
    antes da hora (409) e as rotinas (G5) so pedem depois dela; quem pergunta confere tambem ``escada_vencida`` com o
    ``now()`` de agora (uma vez perdida por dias nao vai depois do fim).

    A hora devolvida e do relogio do banco (``gravado_em`` e ``envios_selo.gravado_em`` sao ``now()`` dele): compare
    sempre com o ``now()`` do banco, lido na mesma transacao (``SELECT now()``), nunca com o relogio do processo nem
    com o relogio monotonico."""
    if resposta_que_encerra(linhas) is not None:
        return None
    if parou_por_sistema_fora(linhas):
        return None
    if espera_da_consulta(tentativas_da_consulta(linhas), maximo) is None:
        return None
    pelo_degrau = hora_base_da_consulta(linhas, enviado_em) + timedelta(
        minutes=_minutos_do_degrau(degrau_da_consulta(linhas))
    )
    pelo_envio = enviado_em + timedelta(seconds=prazo_envio_segundos + MARGEM_DA_CONSULTA_SEGUNDOS)
    vez = max(pelo_degrau, pelo_envio)
    return None if escada_vencida(enviado_em, vez) else vez


def e_a_ultima_consulta(linhas: Sequence[LinhaInformada], maximo: int) -> bool:
    """A proxima consulta e a ultima da escada: se ela tambem falhar, a decisao fica com a pessoa
    (``sistema_nao_respondeu_consulta``) e a operacao entra na conta da retirada por ``nao_respondeu_consultas``."""
    return espera_da_consulta(tentativas_da_consulta(linhas) + 1, maximo) is None


ROTA_CONSULTAR: Final = "/interno/selo/consultar"
"""G3, ``despachante/app.py``: a consulta ao sistema, pedida pelas rotinas.

Pedido: ``POST`` com ``{"operacao_id": "<uuid>"}``, pela regra de ``_so_texto_valido``. Nada mais no corpo. A porta
interna nao distingue quem chama (critica M7), por isso o despachante nao confia no chamador e confere tudo pelo
banco antes de perguntar, com as mesmas funcoes das rotinas: le as linhas de ``informacoes_sistema`` da operacao e
``envios_selo.gravado_em`` e calcula ``proxima_consulta`` (tentativas so de origem ``consulta``, hora base na ultima
linha de qualquer origem, degrau por todas as linhas de consulta, piso no prazo do envio, fim pelo tempo,
``espera_da_consulta``), ``escada_vencida`` e ``e_a_ultima_consulta``. Respostas:

- 200 ``{"desfecho": <DESFECHOS>, "estado": <ESTADOS> | null}``: a pergunta foi gravada em ``informacoes_sistema``
  (com ``sistema_fora``, ``sem_chave`` e ``sala_fora`` tambem; esses nao gastam tentativa). ``sistema_fora``
  (quarentena, fora de ``ativo``, fora da 2026-07-28 ou revisao do terceiro diferente da do teste) encerra a escada
  (A2). A linha ``consulta_nao_feita`` do extrato so e gravada quando a ultima linha desse tipo da operacao nao tem o
  mesmo motivo; ``informacoes_sistema`` continua com uma linha por pergunta;
- 404 ``{"motivo": "sem_envio"}``: a operacao nao existe ou nao saiu com numero;
- 409 ``{"motivo": "nada_a_perguntar"}``: nada foi perguntado, porque ja tem resposta que encerra
  (``resposta_que_encerra``: uma ``nao_recebida`` sozinha ainda nao encerra, e a resposta ao aviso nunca encerra), a
  escada acabou (no maximo de tentativas ou pelo tempo, ``escada_vencida``), a ultima linha de consulta e
  ``sistema_fora`` (``parou_por_sistema_fora``), outro
  protocolo saiu com o mesmo numero depois (``selo_substituto``) ou ainda nao e a vez pela escada (chamar a rota em
  sequencia nao gasta as tentativas contra um sistema lento);
- 409 ``{"motivo": "porta_fechada"}`` (decisao 2 do coordenador no retoque da etapa 3): era a vez, mas a porta de
  saida esta fechada (agora, ou fechou enquanto a chave abria): nada foi perguntado e nada foi gravado, a escada nao
  anda e nao gasta tentativa. As rotinas (G5) tentam de novo na proxima rodada;
- 503 ``{"motivo": "conferencia_nao_feita"}``: falha do MCP-CLARO no banco; nada foi gravado.

A retirada por ``nao_respondeu_consultas`` vai na mesma transacao da consulta que falhou (o banco exige a prova
nela), mas num SAVEPOINT: se o banco a recusar, a linha da consulta fica gravada. Quantas operacoes seguidas sem
resposta ate o fim da escada tiram o selo (``BADGE_LOOKUP_FAILS_TO_WITHDRAW``) e conta do despachante, pelo banco
(decisao do coordenador, etapa 1): o banco so exige a prova na mesma transacao."""


class RespostaDaConsulta(Protocol):
    @property
    def desfecho(self) -> str: ...
    @property
    def estado(self) -> str | None: ...


class ConfereNoDespachante(Protocol):
    """G5, ``rotinas/despachante.py``: ``ClienteDespachante.conferir(operacao_id)``, que vai a ``ROTA_CONSULTAR``.

    Sem o ``ultima`` do desenho (critica M7). Levanta ``ConferenciaNaoFeita`` quando a falha e do MCP-CLARO (o
    despachante nao atendeu, 503); 404 e 409 voltam como ``LookupError``. O 409 ``porta_fechada`` (decisao 2 do
    coordenador no retoque da etapa 3) nao encerra nada: a operacao continua na vez e as rotinas tentam de novo na
    proxima rodada, como com 503."""

    async def conferir(self, operacao_id: uuid.UUID) -> RespostaDaConsulta: ...


class Informado(Protocol):
    """G4, ``saida/fila.py``: ``Operacao.informado``, a ultima informacao do sistema sobre a operacao (ou None).

    Sai de ``informacoes_sistema`` (a ultima com ``respondeu``; sem ela, a ultima linha), so com as categorias do
    contrato e a hora do gateway (``gravado_em``, nunca ``depois[].em``, critica B12). ``de_terceiro`` marca o que
    um servidor de terceiros informou (critica M9). ``Operacao.selo`` e o retrato da ficha (``fichas.selo``).

    ``estado_final`` e ``selo_estado_final(operacao)``: so ele resolve o "estamos confirmando" e so com ele a tela
    (G6) oferece o botao "Registrar o que o sistema informou". Com ``estado`` ``nao_recebida`` e ``estado_final``
    ``None``, o sistema disse uma vez que nao recebeu: a tela mostra o que ele disse, com "continuamos perguntando",
    sem o botao de um toque (critica A3.3). Depois da ultima consulta, sem resposta que encerra, a tela diz "o sistema
    nao confirmou; a decisao e sua" (veja ``ESCADA_DA_CONSULTA_MINUTOS``).

    Num servidor de terceiros (``de_terceiro``), nunca o botao de um toque para ``nao_recebida``, mesmo confirmada: o
    que o terceiro informa e texto de estranhos, e a pessoa confere por fora antes de decidir. Na ficha do pedido igual
    que sai com numero novo (o sistema disse que nao recebeu a original), a ficha diz que e um pedido novo para o
    sistema (critica A3.3 e B12)."""

    @property
    def estado(self) -> str | None: ...
    @property
    def desfecho(self) -> str: ...
    @property
    def origem(self) -> str: ...
    @property
    def gravado_em(self) -> datetime: ...
    @property
    def de_terceiro(self) -> bool: ...
    @property
    def estado_final(self) -> str | None: ...


class TemInformado(Protocol):
    @property
    def informado(self) -> Informado | None: ...
    @property
    def selo(self) -> Mapping[str, Any] | None: ...
