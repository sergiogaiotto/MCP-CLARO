"""Porta unica de saida no banco: operacoes, fichas, limites e a fila.

Cada funcao grava na transacao do chamador e deixa uma linha no extrato. As mudancas de estado sempre
dizem o estado de partida (``UPDATE ... WHERE estado = ...``): quem chega depois nao muda nada e recebe
a recusa com o estado atual. O banco tem a mesma tabela de transicoes (``transicoes_saida``) como
segunda barreira.

Ordem das travas, para nunca haver impasse: consultivas de alcance (ver ``travas``), depois a linha do
limite, depois a linha da ficha, depois a linha da fila. Quem nao precisa de uma delas simplesmente pula. Quem
trava varias linhas de uma vez (bloqueio, restauracao, retomada, vencimento, quarentena) trava em ordem de ``id``.
A linha do limite vem antes da linha da fila porque a tela que encerra um limite (sem trava consultiva) trava o
limite e depois cancela o que ele liberou; a quarentena faz o mesmo (``suspender_limites`` antes de
``parar_por_quarentena``).

Fase 3: a operacao sabe a origem do sistema e se a ferramenta traz texto de fora (``Operacao.de_estranhos``);
a ficha guarda o retrato do alerta dos tres sinais (``fichas.sinais``), quando nasceu com ele; a quarentena de
um servidor de terceiros para o que esperava (``parar_por_quarentena``) e suspende os limites dele
(``suspender_limites``).

Fase 4 (selos de nivel 2 e 3):

- a operacao traz o retrato do selo da ficha (``Operacao.selo``, ``fichas.selo``), o envio com numero
  (``envios_selo``: o nivel e, quando o banco copiou o numero de outra, o protocolo dela) e a ultima informacao do
  sistema (``Operacao.informado``, ``contrato.Informado``), com o estado final do banco (``selo_estado_final``). So
  o estado final resolve o "estamos confirmando"; uma ``nao_recebida`` sozinha nao (critica A3.3). A resposta do
  sistema nunca muda a fila, a decisao da pessoa nem o pedido igual (``conta_como_repeticao``, D15);
- a ficha nasce com o retrato do selo efetivo da pessoa na hora (``registrar_pedido``), gravado num ponto de
  salvamento: se o banco recusar o retrato (o selo caiu entre a leitura e o INSERT), o selo e lido de novo e o
  retrato de agora e gravado, em vez de falhar o pedido (A1);
- o pedido igual a uma "nao confirmada" que saiu com numero leva ``repete_operacao_id`` (D16): o banco decide, no
  envio, se o numero e o mesmo (``envios_selo_nasce``). Um numero cujo desfecho ja se sabe (uma acao com ele
  ``enviada`` ou com a decisao ``aconteceu``) nao e repetido (``original_que_repete``). Quando a original terminou a
  consulta em ``desconhecido`` e nenhuma acao com o numero dela tem estado final
  (``Operacao.original_ficou_em_desconhecido``), a pessoa pode escolher na ficha o numero novo
  (``confirmar_ficha(..., numero_novo=True)``, ``fichas.numero_novo``, critica M4.2); sem a escolha, vale o mesmo
  numero;
- "Registrar o que o sistema informou" (``registrar_o_que_o_sistema_informou``) reusa ``decidir_nao_confirmada`` e
  so grava o estado final que a pessoa viu (``estado_visto``), de um sistema que nao se contradisse sobre o numero; a
  pessoa que registra o contrario do que o sistema informou gera ``sistema_contradito`` e aviso ao ADMIN, e o selo
  fica (D19);
- a resposta ao aviso de bloqueio (origem ``aviso``) aparece em ``Operacao.informado`` como o que o sistema disse,
  mas nunca e final: nao resolve o "estamos confirmando" nem queima o numero (decisao 1 do coordenador, retoque da
  etapa 3: ``selo_estado_final`` e ``envios_selo_nasce`` so contam recibo e consulta);
- o comprovante do bloqueio junta, na leitura, o que os sistemas informaram (``Comprovante.informado``). O bloqueio
  em si e o da Fase 2: os avisos de bloqueio sao derivados depois, pelo despachante (critica A1).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.avisos import avisar_admin
from mcp_claro.cadastro.modelos import Aplicativo, FerramentaVitrine
from mcp_claro.extrato import registrar
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import programas, sinais
from mcp_claro.regras.bloqueios import Bloqueio, quem_rotina
from mcp_claro.regras.estranhos import vem_de_estranhos
from mcp_claro.saida import travas
from mcp_claro.saida.estados import ANTES_DE_SAIR, FRASE, Estado
from mcp_claro.saida.protocolo import FUSO, chave_pedido_igual, codigo_conferencia, novo_protocolo
from mcp_claro.selo import contrato, vigente
from mcp_claro.selo.vigente import SeloVigente
from mcp_claro.versao import PREFIXO_META

log = logger("mcp_claro.saida.fila")

JA_SAIRAM: Final = (Estado.ENVIANDO, Estado.ENVIADA, Estado.NAO_CONFIRMADA, Estado.SISTEMA_PEDIU_DADOS)
PASSOU_POR_ENVIANDO: Final = frozenset(
    {Estado.ENVIANDO, Estado.ENVIADA, Estado.NAO_SAIU, Estado.SISTEMA_PEDIU_DADOS, Estado.NAO_CONFIRMADA}
)
"""Os estados de uma acao que ja passou por "enviando": o envio com numero (``envios_selo``), se houve, nasceu ali."""
QUEM_TETO: Final = quem_rotina("teto_confirmacoes")
"""Quem liga a pausa do teto de fichas por hora (``CONFIRM_CAP_PER_HOUR``)."""
SALTO_NA_RESTAURACAO: Final = 1_000_000
"""Quanto a sequencia do protocolo anda para frente quando o banco volta de uma copia."""
DEPOIS_DA_RETOMADA: Final = " (resposta depois da retomada)"
"""Vai no fim do desfecho quando a resposta chega depois de a retomada dar a acao como nao confirmada."""
QUEM_RETOMA: Final = "despachante:retomada"
"""``mudou_por`` da retomada: diferente do ``despachante`` que so o fim do envio grava a partir de "enviando"."""
COMO_CONFIRMOU: Final = {"sessao": "sem passkey", "passkey": "com passkey", "login_refeito": "com o login refeito"}
DECISOES_NAO_CONFIRMADA: Final = {
    "aconteceu": "aconteceu no sistema",
    "nao_aconteceu": "nao aconteceu no sistema",
    "nao_sei": "nao sabe ainda",
}
META_RETIRADAS: Final = PREFIXO_META + "retiradas"
"""Chave nossa no ``_meta`` de ``operacoes.resultado`` (o ``_meta`` do sistema nunca e guardado): quantas partes sem
texto a leitura com medida tirou do resultado. Quem grava e a porta de saida, com a conta de
``regras.estranhos.retiradas_pela_medida`` feita ANTES de ``medida.aplicar``, nunca pelo texto de um bloco (o
sistema pode escrever um bloco igual ao aviso). ``pedidos.resposta`` passa a conta a ``estranhos.marcar``, que poe o
aviso de partes retiradas fora da moldura (critica B4), e nunca devolve a chave ao aplicativo."""


class DecisaoRecusada(Exception):
    """Pedido da pessoa que nao muda nada: ficha ja decidida, acao que ja saiu, passkey que faltou."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


@dataclass(frozen=True, slots=True)
class InformadoDoSistema:
    """A ultima informacao do sistema sobre a operacao (``contrato.Informado``), de ``informacoes_sistema``: a ultima
    linha com ``respondeu`` e, sem ela, a ultima linha. So as categorias do contrato e a hora do gateway
    (``gravado_em``, nunca ``depois[].em``, critica B12); nenhum texto do sistema.

    - ``estado_final``: ``selo_estado_final(operacao)``, do banco. So ele resolve o "estamos confirmando" e libera o
      botao "Registrar o que o sistema informou" (``registrar_o_que_o_sistema_informou``). Com ``estado``
      ``nao_recebida`` e ``estado_final`` nulo, o sistema disse uma vez que nao recebeu: a tela mostra o que ele disse,
      com "continuamos perguntando", sem o botao (critica A3.3);
    - ``de_terceiro``: o que um servidor de terceiros informou (critica M9);
    - ``tentativas``: as consultas que gastaram a vez (origem ``consulta``, ``contrato.DESFECHOS_QUE_GASTAM_TENTATIVA``)
      e ``ultimo_desfecho``, o desfecho da ultima linha de qualquer origem. Com eles, ``Operacao.consulta_acabou`` diz
      se a escada acabou sem resposta que encerra ("o sistema nao confirmou; a decisao e sua");
    - ``se_contradisse``: alguma resposta sobre o NUMERO desta acao (as dela e as de toda acao que saiu com o mesmo
      numero, original ou pedido igual) tem ``informacoes_sistema.contradiz``: o sistema deu respostas que nao combinam
      (e o banco retirou o selo). O botao de um toque nao registra o que ele disse (decisao 6 do coordenador).

    A ultima resposta pode ser a do aviso de bloqueio (origem ``aviso``): aparece em ``estado``, como o que o sistema
    disse, mas nunca em ``estado_final`` (decisao 1 do coordenador, retoque da etapa 3)."""

    estado: str | None
    desfecho: str
    origem: str
    gravado_em: datetime
    de_terceiro: bool
    estado_final: str | None
    tentativas: int = 0
    ultimo_desfecho: str | None = None
    se_contradisse: bool = False


_INFORMADO: Final[type[contrato.Informado]] = InformadoDoSistema  # o mypy confere o contrato


@dataclass(frozen=True, slots=True)
class EscadaDaOriginal:
    """A escada da consulta da "nao confirmada" que este pedido igual repete (``operacoes.repete_operacao_id``), lida
    na mesma linha da operacao (critica M4.2): o envio dela, o estado final do NUMERO dela, o estado da ultima resposta
    de origem ``consulta`` (``respondeu``) dela, as tentativas que gastaram a vez e o desfecho da ultima linha de
    qualquer origem. So as categorias do contrato, nunca o numero.

    ``estado_final_do_numero`` e um estado final do banco (``selo_estado_final``) de alguma acao que saiu com o numero
    da original: ela mesma, a primeira acao com esse numero ou outro pedido igual que o copiou (a cadeia de
    ``envios_selo.repete_operacao_id``, a mesma da contradicao, ``InformadoDoSistema.se_contradisse``). Para o sistema,
    todas sao a mesma escrita: um ``concluida`` sobre o numero em outra acao ja diz que ela foi feita, e o numero novo
    faria a escrita duas vezes (defeito 2 do verificador do G4r, etapa 3r). Com mais de um, vale o da acao mais perto
    da primeira com o numero."""

    enviado_em: datetime
    estado_final_do_numero: str | None
    ultima_consulta: str | None
    tentativas: int
    ultimo_desfecho: str | None


def _escada_acabou(
    *, tentativas: int, ultimo_desfecho: str | None, enviado_em: datetime, agora: datetime | None, maximo: int
) -> bool:
    """A escada da consulta acabou sem resposta que encerra, por uma das regras que nao dependem do estado final: as
    tentativas chegaram a ``maximo`` (``BADGE_LOOKUP_MAX_ATTEMPTS``), a ultima linha e ``sistema_fora`` ou passou o
    tempo (``contrato.escada_vencida``, com ``agora``, o ``now()`` do banco lido na mesma linha; decisao 3 do
    coordenador, retoque da etapa 3)."""
    if agora is not None and contrato.escada_vencida(enviado_em, agora):
        return True
    return ultimo_desfecho == "sistema_fora" or tentativas >= maximo


@dataclass(frozen=True, slots=True)
class Operacao:
    id: uuid.UUID
    protocolo: str
    codigo_conferencia: str
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ordem_id: uuid.UUID
    ferramenta_id: uuid.UUID
    sistema_id: uuid.UUID
    argumentos: dict[str, Any]
    aplicativo_diz: str | None
    resultado: dict[str, Any] | None
    desfecho: str | None
    criada_em: datetime
    fila_id: int
    estado: Estado
    ficha_id: uuid.UUID | None
    limite_id: uuid.UUID | None
    hora_saida: datetime | None
    enviada_em: datetime | None
    mudou_em: datetime
    mudou_por: str
    nome_vitrine: str
    sistema_nome: str
    decisao_pessoa: str | None = None
    ficha_expira_em: datetime | None = None
    ficha_exige_passkey: bool = False
    ficha_risco: str | None = None
    ficha_decisao: str | None = None
    sistema_origem: str = "proprio"
    texto_de_fora: bool = False
    ficha_sinais: dict[str, Any] | None = None
    """Retrato do alerta dos tres sinais na ficha (``sinais.Alerta.retrato``); None sem alerta."""
    selo: dict[str, Any] | None = None
    """Retrato do selo efetivo da pessoa quando a ficha nasceu (``fichas.selo``, ``contrato.RETRATO_DO_SELO``); None
    sem ficha (limite) ou em ficha de antes da 0007 ("nao guardado")."""
    informado: InformadoDoSistema | None = None
    """A ultima informacao do sistema sobre a operacao (``contrato.Informado``); None sem nenhuma."""
    envio_nivel: int | None = None
    """O nivel do envio com numero (``envios_selo.nivel``, 2 ou 3); None quando a acao nao saiu com numero."""
    repete_protocolo: str | None = None
    """O protocolo da "nao confirmada" que este pedido igual repete (``operacoes.repete_operacao_id``, D16): o que a
    entrada pediu. Se o numero foi o mesmo, quem decide e o banco no envio (``mesmo_numero_de``)."""
    mesmo_numero_de: str | None = None
    """O protocolo cujo numero esta acao levou ao sistema (``envios_selo.repete_operacao_id``: so preenchido quando o
    banco copiou o numero); None com numero proprio."""
    substituto: str | None = None
    """Um protocolo que saiu depois ao sistema com o numero desta acao (``selo_substituto``, do banco, critica M4): o
    fim da cadeia de pedidos iguais que copiaram o numero (o passo mais longo). O que o sistema informar aparece nele,
    esta acao para de ser consultada e o botao de um toque fica desligado aqui.

    So contam os pedidos iguais cuja chamada pode ter chegado ao sistema: a fila em ``enviando``, ``enviada`` ou
    ``nao_confirmada`` (decisao 5 do coordenador, retoque da etapa 3). Um pedido igual que terminou ``nao_saiu`` ou
    ``sistema_pediu_dados`` nao substitui esta acao: ela segue na escada da consulta, e o pedido igual seguinte a
    repete de novo (``original_que_repete`` so escolhe uma ``nao_confirmada``). Assim, dois pedidos iguais no mesmo
    passo so existem quando o primeiro nao chegou ao sistema, e ele nao conta; se ainda houver dois que contam, vale o
    envio mais novo (``envios_selo.gravado_em``)."""
    envio_gravado_em: datetime | None = None
    """A hora do envio com numero (``envios_selo.gravado_em``, do banco); None sem numero."""
    lido_em: datetime | None = None
    """O ``now()`` do banco na leitura desta linha (o mesmo SELECT): a hora que ``contrato.escada_vencida`` compara
    com o envio. Nunca o relogio do processo."""
    ficha_numero_novo: bool = False
    """A pessoa escolheu na ficha o numero novo para este pedido igual (``fichas.numero_novo``, critica M4.2)."""
    original: EscadaDaOriginal | None = None
    """A escada da consulta da original que este pedido igual repete (``repete_operacao_id``), se ela saiu com numero;
    None nos outros casos. Veja ``original_ficou_em_desconhecido``."""

    @property
    def nivel_do_selo(self) -> int | None:
        """O nivel do selo desta operacao, o que vai no ``_meta`` ao aplicativo: o do envio com numero; 1 para a acao
        que passou por "enviando" sem numero (saiu como nivel 1); antes disso, o do retrato da ficha. None quando nada
        disso diz (limite que ainda nao saiu, ficha de antes da 0007): quem chama calcula o efetivo de agora."""
        if self.envio_nivel is not None:
            return self.envio_nivel
        if self.estado in PASSOU_POR_ENVIANDO:
            return 1
        nivel = (self.selo or {}).get("nivel")
        return nivel if type(nivel) is int and nivel in (1, 2, 3) else None

    @property
    def escada_vencida(self) -> bool:
        """A escada da consulta acabou pelo tempo (``contrato.escada_vencida``): a acao saiu com numero ha
        ``contrato.REAPROVEITAR_NUMERO_DIAS`` dias ou mais, pela hora do banco (``lido_em``). Decisao 3 do
        coordenador, retoque da etapa 3."""
        return (
            self.envio_gravado_em is not None
            and self.lido_em is not None
            and contrato.escada_vencida(self.envio_gravado_em, self.lido_em)
        )

    def consulta_acabou(self, maximo: int) -> bool:
        """A escada da consulta acabou sem resposta que encerra (``contrato.ESCADA_DA_CONSULTA_MINUTOS``): a acao
        saiu com numero, o sistema nao deu estado final e ou as tentativas chegaram a ``maximo``
        (``BADGE_LOOKUP_MAX_ATTEMPTS``), ou a ultima linha e ``sistema_fora``, ou outro protocolo saiu depois com o
        mesmo numero (``substituto``), ou passou o tempo da escada (``escada_vencida``, mesmo sem nenhuma linha: as
        linhas que nao gastam tentativa nao a fazem rodar sem fim, decisao 3 do coordenador). A tela e o texto ao
        aplicativo dizem "o sistema nao confirmou; a decisao e sua"."""
        if self.envio_nivel is None or (self.informado is not None and self.informado.estado_final is not None):
            return False
        if self.substituto is not None or self.escada_vencida:
            return True
        if self.informado is None:
            return False
        return self.informado.ultimo_desfecho == "sistema_fora" or self.informado.tentativas >= maximo

    def original_ficou_em_desconhecido(self, maximo: int) -> bool:
        """Este pedido igual repete uma "nao confirmada" (``repete_protocolo``) que saiu com numero e terminou a escada
        da consulta sem resposta final, com a ultima resposta a consulta ``desconhecido`` (critica M4.2, decisao 4 do
        coordenador). A escada acabou pelas mesmas regras de ``consulta_acabou``: as tentativas (``maximo``,
        ``BADGE_LOOKUP_MAX_ATTEMPTS``), ``sistema_fora`` na ultima linha ou o tempo (``contrato.escada_vencida``).
        "Sem resposta final" vale pelo NUMERO da original, nao so por ela: nenhuma acao que saiu com esse numero tem
        estado final (``EscadaDaOriginal.estado_final_do_numero``).

        So assim a ficha mostra o aviso e a escolha "mandar com numero novo" ("o sistema nao sabe se o primeiro foi
        feito; com numero novo, pode fazer duas vezes"), e ``confirmar_ficha(..., numero_novo=True)`` a aceita. O banco
        confere a parte que sabe (``fichas_guarda``: nenhum estado final sobre o numero e a ultima resposta a consulta
        ``desconhecido``)."""
        o = self.original
        if self.repete_protocolo is None or o is None or o.estado_final_do_numero is not None:
            return False
        if o.ultima_consulta != "desconhecido":
            return False
        return _escada_acabou(
            tentativas=o.tentativas,
            ultimo_desfecho=o.ultimo_desfecho,
            enviado_em=o.enviado_em,
            agora=self.lido_em,
            maximo=maximo,
        )

    @property
    def de_estranhos(self) -> bool:
        """O resultado e texto de estranhos: servidor de terceiros ou ferramenta marcada "traz texto de fora"."""
        return vem_de_estranhos(self.sistema_origem, self.texto_de_fora)

    @property
    def de_terceiro(self) -> bool:
        return self.sistema_origem != "proprio"

    @property
    def retiradas(self) -> int:
        """Partes sem texto que a leitura com medida tirou do resultado (``META_RETIRADAS``); 0 sem a conta."""
        meta = (self.resultado or {}).get("_meta")
        quantas = meta.get(META_RETIRADAS) if isinstance(meta, dict) else None
        return quantas if isinstance(quantas, int) and not isinstance(quantas, bool) and quantas > 0 else 0

    @property
    def antes_de_sair(self) -> bool:
        return self.estado in ANTES_DE_SAIR

    def conta_como_repeticao(self) -> bool:
        """Um pedido igual devolve esta operacao se ela pode ter efeito no sistema ou ainda pode sair.

        So a decisao da pessoa conta: a resposta do sistema (``informado``) nunca muda isto (D15), nem uma
        ``nao_recebida`` confirmada."""
        if self.estado in (Estado.AGUARDANDO_FICHA, Estado.LIBERADA, Estado.ENVIANDO, Estado.ENVIADA):
            return True
        return self.estado == Estado.NAO_CONFIRMADA and self.decisao_pessoa != "nao_aconteceu"


# Fase 4: o envio com numero (so as colunas que a entrada e as rotinas leem: nunca envios_selo.chave, critica M10), a
# ultima informacao do sistema, o estado final do banco e o protocolo que saiu depois com o mesmo numero. Sem envio,
# nada disso e lido (as subconsultas param no primeiro teste). As tentativas contam como
# contrato.tentativas_da_consulta. now() e a hora do banco na leitura (contrato.escada_vencida). A contradicao conta
# pelo numero, pela cadeia de envios_selo.repete_operacao_id (a mesma de original_que_repete): sobe ate o envio com o
# numero proprio e desce por todos os pedidos iguais que o copiaram. A escada da original (ori) so existe no pedido
# igual cuja original saiu com numero; o estado final dela conta pelo numero, pela mesma cadeia, a partir do envio da
# original (o numero que o pedido igual levaria).
_SQL_OPERACAO = """
SELECT o.id, o.protocolo, o.codigo_conferencia, o.pessoa_id, o.aplicativo_id, o.ordem_id, o.ferramenta_id,
       o.sistema_id, o.argumentos, o.aplicativo_diz, o.resultado, o.desfecho, o.criada_em,
       f.id AS fila_id, f.estado, f.ficha_id, f.limite_id, f.hora_saida, f.enviada_em, f.mudou_em, f.mudou_por,
       r.nome_vitrine, s.nome AS sistema_nome, d.decisao AS decisao_pessoa,
       fi.expira_em AS ficha_expira_em, fi.exige_passkey AS ficha_exige_passkey, fi.risco AS ficha_risco,
       fi.decisao AS ficha_decisao, s.origem AS sistema_origem, r.texto_de_fora, fi.sinais AS ficha_sinais,
       fi.selo AS ficha_selo, fi.numero_novo AS ficha_numero_novo, e.nivel AS envio_nivel,
       e.gravado_em AS envio_gravado_em, now() AS lido_em, ro.protocolo AS repete_protocolo,
       me.protocolo AS mesmo_numero_de, inf.origem AS inf_origem, inf.desfecho AS inf_desfecho,
       inf.estado AS inf_estado, inf.gravado_em AS inf_gravado_em, ult.desfecho AS inf_ultimo_desfecho,
       ult.tentativas AS inf_tentativas, ult.estado_final AS inf_estado_final, num.se_contradisse AS inf_se_contradisse,
       sub.protocolo AS substituto, ori.enviado_em AS ori_enviado_em,
       ori.estado_final_do_numero AS ori_estado_final_do_numero,
       ori.ultima_consulta AS ori_ultima_consulta, ori.tentativas AS ori_tentativas,
       ori.ultimo_desfecho AS ori_ultimo_desfecho
FROM operacoes o
JOIN fila_saida f ON f.operacao_id = o.id
JOIN ferramentas_revisadas r ON r.id = o.ferramenta_id
JOIN sistemas s ON s.id = o.sistema_id
LEFT JOIN fichas fi ON fi.id = f.ficha_id
LEFT JOIN decisoes_nao_confirmada d ON d.operacao_id = o.id
LEFT JOIN operacoes ro ON ro.id = o.repete_operacao_id
LEFT JOIN envios_selo e ON e.operacao_id = o.id
LEFT JOIN operacoes me ON me.id = e.repete_operacao_id
LEFT JOIN LATERAL (
  SELECT i.origem, i.desfecho, i.estado, i.gravado_em FROM informacoes_sistema i
  WHERE e.operacao_id IS NOT NULL AND i.operacao_id = o.id
  ORDER BY (i.desfecho = 'respondeu') DESC, i.gravado_em DESC, i.id DESC LIMIT 1
) inf ON true
LEFT JOIN LATERAL (
  SELECT (SELECT i.desfecho FROM informacoes_sistema i WHERE i.operacao_id = o.id
          ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1) AS desfecho,
         (SELECT count(*) FROM informacoes_sistema i WHERE i.operacao_id = o.id AND i.origem = 'consulta'
            AND i.desfecho = ANY (selo_vocabulario('desfechos_que_gastam_tentativa'))) AS tentativas,
         selo_estado_final(o.id) AS estado_final
  WHERE e.operacao_id IS NOT NULL
) ult ON true
LEFT JOIN LATERAL (
  WITH RECURSIVE acima (operacao_id, repete, passo) AS (
    SELECT e.operacao_id, e.repete_operacao_id, 0
    UNION ALL
    SELECT x.operacao_id, x.repete_operacao_id, a.passo + 1 FROM envios_selo x
    JOIN acima a ON x.operacao_id = a.repete WHERE a.passo < 100
  ), numero (operacao_id, passo) AS (
    SELECT a.operacao_id, 0 FROM acima a WHERE a.repete IS NULL
    UNION ALL
    SELECT x.operacao_id, n.passo + 1 FROM envios_selo x
    JOIN numero n ON x.repete_operacao_id = n.operacao_id WHERE n.passo < 100
  )
  SELECT EXISTS (SELECT 1 FROM numero n JOIN informacoes_sistema i ON i.operacao_id = n.operacao_id
                 WHERE i.contradiz) AS se_contradisse
  WHERE e.operacao_id IS NOT NULL
) num ON true
LEFT JOIN LATERAL (
  SELECT x.protocolo FROM operacoes x WHERE e.operacao_id IS NOT NULL AND x.id = selo_substituto(o.id)
) sub ON true
LEFT JOIN LATERAL (
  WITH RECURSIVE acima (operacao_id, repete, passo) AS (
    SELECT x.operacao_id, x.repete_operacao_id, 0 FROM envios_selo x WHERE x.operacao_id = o.repete_operacao_id
    UNION ALL
    SELECT x.operacao_id, x.repete_operacao_id, a.passo + 1 FROM envios_selo x
    JOIN acima a ON x.operacao_id = a.repete WHERE a.passo < 100
  ), numero (operacao_id, passo) AS (
    SELECT a.operacao_id, 0 FROM acima a WHERE a.repete IS NULL
    UNION ALL
    SELECT x.operacao_id, n.passo + 1 FROM envios_selo x
    JOIN numero n ON x.repete_operacao_id = n.operacao_id WHERE n.passo < 100
  )
  SELECT eo.gravado_em AS enviado_em,
         (SELECT fim.estado FROM numero n CROSS JOIN LATERAL (SELECT selo_estado_final(n.operacao_id) AS estado) fim
          WHERE fim.estado IS NOT NULL ORDER BY n.passo, n.operacao_id LIMIT 1) AS estado_final_do_numero,
         (SELECT i.estado FROM informacoes_sistema i WHERE i.operacao_id = eo.operacao_id AND i.origem = 'consulta'
            AND i.desfecho = 'respondeu' ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1) AS ultima_consulta,
         (SELECT count(*) FROM informacoes_sistema i WHERE i.operacao_id = eo.operacao_id AND i.origem = 'consulta'
            AND i.desfecho = ANY (selo_vocabulario('desfechos_que_gastam_tentativa'))) AS tentativas,
         (SELECT i.desfecho FROM informacoes_sistema i WHERE i.operacao_id = eo.operacao_id
          ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1) AS ultimo_desfecho
  FROM envios_selo eo WHERE eo.operacao_id = o.repete_operacao_id
) ori ON true
"""


_TEM_INFORMADO: Final[type[contrato.TemInformado]] = Operacao  # o mypy confere o contrato


def _existe(op: Operacao | None) -> Operacao:
    if op is None:
        raise LookupError("A operacao nao foi encontrada na mesma transacao.")
    return op


def _json(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


def _operacao(linha: Row[Any]) -> Operacao:
    return Operacao(
        id=linha.id,
        protocolo=linha.protocolo,
        codigo_conferencia=linha.codigo_conferencia,
        pessoa_id=linha.pessoa_id,
        aplicativo_id=linha.aplicativo_id,
        ordem_id=linha.ordem_id,
        ferramenta_id=linha.ferramenta_id,
        sistema_id=linha.sistema_id,
        argumentos=_json(linha.argumentos) or {},
        aplicativo_diz=linha.aplicativo_diz,
        resultado=_json(linha.resultado),
        desfecho=linha.desfecho,
        criada_em=linha.criada_em,
        fila_id=linha.fila_id,
        estado=Estado(linha.estado),
        ficha_id=linha.ficha_id,
        limite_id=linha.limite_id,
        hora_saida=linha.hora_saida,
        enviada_em=linha.enviada_em,
        mudou_em=linha.mudou_em,
        mudou_por=linha.mudou_por,
        nome_vitrine=linha.nome_vitrine,
        sistema_nome=linha.sistema_nome,
        decisao_pessoa=linha.decisao_pessoa,
        ficha_expira_em=linha.ficha_expira_em,
        ficha_exige_passkey=bool(linha.ficha_exige_passkey),
        ficha_risco=linha.ficha_risco,
        ficha_decisao=linha.ficha_decisao,
        sistema_origem=linha.sistema_origem,
        texto_de_fora=bool(linha.texto_de_fora),
        ficha_sinais=_json(linha.ficha_sinais),
        selo=_json(linha.ficha_selo),
        informado=_informado(linha),
        envio_nivel=None if linha.envio_nivel is None else int(linha.envio_nivel),
        repete_protocolo=linha.repete_protocolo,
        mesmo_numero_de=linha.mesmo_numero_de,
        substituto=linha.substituto,
        envio_gravado_em=linha.envio_gravado_em,
        lido_em=linha.lido_em,
        ficha_numero_novo=linha.ficha_numero_novo is True,
        original=_original(linha),
    )


def _original(linha: Row[Any]) -> EscadaDaOriginal | None:
    if linha.ori_enviado_em is None:
        return None
    return EscadaDaOriginal(
        enviado_em=linha.ori_enviado_em,
        estado_final_do_numero=linha.ori_estado_final_do_numero,
        ultima_consulta=linha.ori_ultima_consulta,
        tentativas=int(linha.ori_tentativas or 0),
        ultimo_desfecho=linha.ori_ultimo_desfecho,
    )


def _informado(linha: Row[Any]) -> InformadoDoSistema | None:
    if linha.inf_desfecho is None:
        return None
    return InformadoDoSistema(
        estado=linha.inf_estado,
        desfecho=linha.inf_desfecho,
        origem=linha.inf_origem,
        gravado_em=linha.inf_gravado_em,
        de_terceiro=linha.sistema_origem != "proprio",
        estado_final=linha.inf_estado_final,
        tentativas=int(linha.inf_tentativas or 0),
        ultimo_desfecho=linha.inf_ultimo_desfecho,
        se_contradisse=linha.inf_se_contradisse is True,
    )


async def por_id(c: AsyncConnection, operacao_id: uuid.UUID) -> Operacao | None:
    linha = (await c.execute(text(_SQL_OPERACAO + " WHERE o.id = :i"), {"i": operacao_id})).first()
    return _operacao(linha) if linha else None


async def por_fila(c: AsyncConnection, fila_id: int) -> Operacao | None:
    linha = (await c.execute(text(_SQL_OPERACAO + " WHERE f.id = :i"), {"i": fila_id})).first()
    return _operacao(linha) if linha else None


async def por_protocolo(
    c: AsyncConnection, protocolo: str, *, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID
) -> Operacao | None:
    """So a mesma pessoa pelo mesmo aplicativo enxerga a operacao; para os outros, ela nao existe."""
    linha = (
        await c.execute(
            text(_SQL_OPERACAO + " WHERE o.protocolo = :p AND o.pessoa_id = :pe AND o.aplicativo_id = :a"),
            {"p": protocolo, "pe": pessoa_id, "a": aplicativo_id},
        )
    ).first()
    return _operacao(linha) if linha else None


async def da_pessoa(
    c: AsyncConnection, pessoa_id: uuid.UUID, *, estados: Iterable[Estado] | None = None, limite: int = 100
) -> list[Operacao]:
    filtro = " AND f.estado = ANY(:e)" if estados is not None else ""
    linhas = (
        await c.execute(
            text(_SQL_OPERACAO + " WHERE o.pessoa_id = :p" + filtro + " ORDER BY o.criada_em DESC LIMIT :n"),
            {"p": pessoa_id, "e": [str(e) for e in estados or ()], "n": limite},
        )
    ).all()
    return [_operacao(linha) for linha in linhas]


async def pendencias(c: AsyncConnection, pessoa_id: uuid.UUID) -> list[Operacao]:
    """Caixa de pendencias: fichas a decidir, acoes na espera e resultados nao confirmados sem decisao."""
    linhas = (
        await c.execute(
            text(
                _SQL_OPERACAO + " WHERE o.pessoa_id = :p AND (f.estado IN ('aguardando_ficha', 'liberada')"
                " OR (f.estado = 'nao_confirmada' AND d.decisao IS NULL)) ORDER BY o.criada_em"
            ),
            {"p": pessoa_id},
        )
    ).all()
    return [_operacao(linha) for linha in linhas]


# Pedido igual


async def travar_pedido_igual(c: AsyncConnection, chave: str) -> None:
    """Dois pedidos iguais ao mesmo tempo: o segundo espera o primeiro gravar e o encontra."""
    await c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": travas.chave("pedido-igual", chave)})


async def pedido_igual(c: AsyncConnection, chave: str, janela_minutos: int) -> Operacao | None:
    """A operacao igual que ainda pode sair (esperando a ficha, liberada ou enviando) conta em qualquer idade.
    A que ja saiu ou ficou nao confirmada conta por ``janela_minutos``, desde a ultima mudanca da linha."""
    linhas = (
        await c.execute(
            text(
                _SQL_OPERACAO + " WHERE o.chave_pedido_igual = :k"
                " AND (f.estado IN ('aguardando_ficha', 'liberada', 'enviando')"
                " OR greatest(o.criada_em, f.mudou_em) > now() - make_interval(mins => :j))"
                " ORDER BY o.criada_em DESC"
            ),
            {"k": chave, "j": janela_minutos},
        )
    ).all()
    for linha in linhas:
        op = _operacao(linha)
        if op.conta_como_repeticao():
            return op
    return None


async def original_que_repete(
    c: AsyncConnection, *, chave: str, codigo: str, sistema_id: uuid.UUID, pessoa_id: uuid.UUID
) -> uuid.UUID | None:
    """O pedido igual nos niveis 2 e 3 (desenho 5.4, D16): a "nao confirmada" mais recente com a mesma
    ``chave_pedido_igual`` e o mesmo ``codigo_conferencia`` (os mesmos argumentos), do mesmo sistema e da mesma pessoa,
    de ate ``contrato.REAPROVEITAR_NUMERO_DIAS`` dias, sem a decisao ``aconteceu`` e que saiu com numero
    (``envios_selo``, critica A1). E a regra de ``operacoes_repete_confere``; so a operacao nova a guarda em
    ``repete_operacao_id``, e o banco decide no envio se o numero e o mesmo (``envios_selo_nasce``: numero novo se o
    sistema ja disse algo que o queima). Sem envio na original (nivel 1, ou antes da 0007), nenhuma repeticao.

    A mais que a guarda do banco (critica M4): o numero cujo desfecho ja se sabe nao e repetido. Se uma das acoes que
    sairam com esse numero (a original da cadeia e os pedidos iguais que o copiaram, por
    ``envios_selo.repete_operacao_id``; a entrada nunca le ``envios_selo.chave``) ficou ``enviada`` ou tem a decisao
    ``aconteceu`` da pessoa, o sistema ja recebeu a escrita desse numero e respondeu. Um pedido igual que chega
    depois, fora da janela do pedido igual, e um pedido novo: com o mesmo numero, um sistema honesto responderia "ja
    feito" e nao faria. Por isso ele sai com numero novo. Sem isso, a mesma original ganharia dois pedidos iguais com
    o numero dela que chegaram ao sistema.

    So uma ``nao_confirmada`` e escolhida: um pedido igual R que terminou ``nao_saiu`` ou ``sistema_pediu_dados`` e mais
    novo que a original O, mas nao e repetido (o banco recusaria, ``operacoes_repete_confere``, e a operacao nova
    sairia com numero novo com o desfecho de O ainda desconhecido). O pedido igual seguinte repete O e leva o numero
    dela, e R nao substitui O (``selo_substituto``, decisao 5 do coordenador).

    So e chamada quando ``pedido_igual`` nao devolveu nada: a resposta do sistema nao muda quem conta como repeticao."""
    valor = (
        await c.execute(
            text(
                "SELECT o.id FROM operacoes o JOIN fila_saida f ON f.operacao_id = o.id AND f.estado = 'nao_confirmada'"
                " JOIN envios_selo e ON e.operacao_id = o.id"
                " LEFT JOIN decisoes_nao_confirmada d ON d.operacao_id = o.id"
                " WHERE o.chave_pedido_igual = :k AND o.codigo_conferencia = :co AND o.sistema_id = :s"
                " AND o.pessoa_id = :p AND o.criada_em >= now() - make_interval(days => :dias)"
                " AND d.decisao IS DISTINCT FROM 'aconteceu'"
                # As acoes que sairam com o mesmo numero de ``e``: sobe pela cadeia de envios_selo.repete_operacao_id
                # ate a que tem o numero proprio e desce por todos os pedidos iguais que o copiaram (teto de 100
                # passos, o de selo_substituto). Alguma ``enviada`` ou com ``aconteceu``: o desfecho ja se sabe.
                " AND NOT EXISTS (WITH RECURSIVE acima (operacao_id, repete, passo) AS ("
                " SELECT e.operacao_id, e.repete_operacao_id, 0 UNION ALL"
                " SELECT x.operacao_id, x.repete_operacao_id, a.passo + 1 FROM envios_selo x"
                " JOIN acima a ON x.operacao_id = a.repete WHERE a.passo < 100"
                " ), numero (operacao_id, passo) AS ("
                " SELECT a.operacao_id, 0 FROM acima a WHERE a.repete IS NULL UNION ALL"
                " SELECT x.operacao_id, n.passo + 1 FROM envios_selo x"
                " JOIN numero n ON x.repete_operacao_id = n.operacao_id WHERE n.passo < 100"
                " ) SELECT 1 FROM numero n LEFT JOIN fila_saida fx ON fx.operacao_id = n.operacao_id"
                " LEFT JOIN decisoes_nao_confirmada dx ON dx.operacao_id = n.operacao_id"
                " WHERE fx.estado = 'enviada' OR dx.decisao = 'aconteceu')"
                " ORDER BY o.criada_em DESC, o.id DESC LIMIT 1"
            ),
            {
                "k": chave,
                "co": codigo,
                "s": sistema_id,
                "p": pessoa_id,
                "dias": contrato.REAPROVEITAR_NUMERO_DIAS,
            },
        )
    ).scalar_one_or_none()
    return None if valor is None else uuid.UUID(str(valor))


# Limites por aproximacao


def _valores_do_campo(valor: Any) -> list[str] | None:
    """O valor de um campo de destinatario como lista de textos; ``None`` quando nao da para conferir.

    Ausente, ``None``, ``""`` ou ``[]``: vazio. Texto: um destinatario. Lista so de textos: a lista. Qualquer
    outra coisa (numero, objeto, lista com objeto) nao e conferida, e o limite nao cobre."""
    if valor is None or valor == "" or valor == []:
        return []
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, list) and all(isinstance(v, str) for v in valor):
        return list(valor)
    return None


def cobre(destinatarios: Any, argumentos: dict[str, Any], *, propriedades: Collection[str] | None = None) -> bool:
    """Lista vazia: qualquer destinatario. Cada item ``{"campo", "valores"}`` exige o argumento entre os valores.

    Item com ``valores`` vazio quer dizer "este campo tem de vir ausente, vazio ou lista vazia": e assim que um
    limite "so estes destinatarios" fecha os outros campos de destinatario da ferramenta (copia, copia oculta).
    Item com valores exige pelo menos um destinatario, e todos entre os valores.

    ``propriedades`` (chaves de ``esquema_entrada.properties`` da ferramenta revisada): num limite "so estes",
    qualquer argumento fora delas faz o limite nao cobrir, e o pedido vai para a ficha. Os campos do esquema
    que nao sao de destinatario (como o texto da mensagem) ficam livres.
    """
    itens = _json(destinatarios) or []
    if not isinstance(itens, list):
        return False
    if itens and propriedades is not None and any(chave not in propriedades for chave in argumentos):
        return False
    for item in itens:
        if not isinstance(item, dict):
            return False
        campo, valores = item.get("campo"), item.get("valores")
        if not isinstance(campo, str) or not isinstance(valores, list):
            return False
        pedidos = _valores_do_campo(argumentos.get(campo))
        if pedidos is None:
            return False
        if not valores:
            if pedidos:
                return False
            continue
        if not pedidos or any(v not in valores for v in pedidos):
            return False
    return True


async def propriedades_da_ferramenta(c: AsyncConnection, sistema_id: uuid.UUID, nome_original: str) -> frozenset[str]:
    """Chaves de ``esquema_entrada.properties`` da revisao ativa da ferramenta (vazio sem revisao ativa)."""
    esquema = _json(
        (
            await c.execute(
                text(
                    "SELECT esquema_entrada FROM ferramentas_revisadas WHERE sistema_id = :s AND nome_original = :n"
                    " AND situacao = 'ativa'"
                ),
                {"s": sistema_id, "n": nome_original},
            )
        ).scalar_one_or_none()
    )
    propriedades = esquema.get("properties") if isinstance(esquema, dict) else None
    return frozenset(propriedades) if isinstance(propriedades, dict) else frozenset()


async def gastar_limite(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    sistema_id: uuid.UUID,
    nome_original: str,
    argumentos: dict[str, Any],
    exige_passkey: bool = True,
) -> uuid.UUID | None:
    """Gasta uma vez de um limite que cobre o pedido. O UPDATE so passa com saldo: nunca passa do teto.

    ``exige_passkey`` (``FEATURE_PASSKEY``): com a passkey ligada, so vale limite criado ou ampliado com ela,
    como o despachante confere. Sem limite que valha, o pedido vai para a ficha.

    Virada do dia (Brasilia): o dia gravado nunca volta. Um pedido que comecou antes da meia-noite e grava
    depois de outro do dia novo conta no dia novo.

    Limite "so estes destinatarios": ``cobre`` recebe as chaves do esquema de entrada da ferramenta revisada,
    lidas nesta mesma transacao. Argumento fora do esquema faz o pedido ir para a ficha.
    """
    candidatos = (
        await c.execute(
            text(
                "SELECT id, destinatarios FROM limites WHERE pessoa_id = :p AND aplicativo_id = :a"
                " AND sistema_id = :s AND nome_original = :n AND situacao = 'ativo' AND valido_ate > now()"
                " AND (com_passkey OR NOT :pk) ORDER BY valido_ate, id"
            ),
            {"p": pessoa_id, "a": aplicativo_id, "s": sistema_id, "n": nome_original, "pk": exige_passkey},
        )
    ).all()
    propriedades: frozenset[str] | None = None
    for linha in candidatos:
        if _json(linha.destinatarios) and propriedades is None:
            propriedades = await propriedades_da_ferramenta(c, sistema_id, nome_original)
        if not cobre(linha.destinatarios, argumentos, propriedades=propriedades):
            continue
        gasto = (
            await c.execute(
                text(
                    "UPDATE limites SET"
                    " usados_hoje = CASE WHEN dia >= (now() AT TIME ZONE :fuso)::date THEN usados_hoje + 1 ELSE 1 END,"
                    " dia = greatest(dia, (now() AT TIME ZONE :fuso)::date)"
                    " WHERE id = :i AND situacao = 'ativo' AND valido_ate > now() AND (com_passkey OR NOT :pk)"
                    " AND (CASE WHEN dia >= (now() AT TIME ZONE :fuso)::date THEN usados_hoje ELSE 0 END) < por_dia"
                    " RETURNING id"
                ),
                {"i": linha.id, "fuso": FUSO, "pk": exige_passkey},
            )
        ).scalar_one_or_none()
        if gasto is not None:
            return uuid.UUID(str(gasto))
    return None


# Pedido de acao


@dataclass(frozen=True, slots=True)
class Rota:
    """Por onde a acao passa: ficha (com ou sem passkey) ou limite ja gasto.

    ``sinais``: retrato do alerta dos tres sinais (``sinais.Alerta.retrato``), gravado na ficha. Vem na rota
    ``tres_sinais`` e tambem nas de risco alto e de experiencia, quando o alerta valia; nunca num limite."""

    limite_id: uuid.UUID | None = None
    exige_passkey: bool = False
    motivo: str = "ficha"
    sinais: dict[str, Any] | None = None

    @property
    def por_limite(self) -> bool:
        return self.limite_id is not None


async def registrar_pedido(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    ordem_id: uuid.UUID,
    ferramenta: FerramentaVitrine,
    argumentos: dict[str, Any],
    aplicativo_diz: str | None,
    rota: Rota,
    prazo_ficha_minutos: int,
    espera_segundos: int,
    trace_id: str | None = None,
    aplicativo: Aplicativo | None = None,
    selo_ligado: bool = False,
) -> Operacao:
    """``aplicativo`` (opcional, o mesmo de ``aplicativo_id``) poupa a consulta do extrato para um assistente.

    Com ``rota.sinais`` (alerta dos tres sinais), a ficha guarda o retrato, ``acao_pedida`` leva
    ``dados.tres_sinais`` e o extrato ganha a linha ``tres_sinais``. Um limite nunca leva o retrato.

    Fase 4:

    - ``selo_ligado`` e ``FEATURE_PHASE4_BADGES``. O selo efetivo da pessoa (``vigente.efetivo``, com
      ``pessoas.teste`` lido aqui) sai do selo lido de ``ferramenta.sistema.selo_vigente`` (a vitrine) ou, sem ele, de
      ``selo_vigente()`` agora. O retrato vai na ficha (``fichas.selo``) e em ``dados.selo`` de ``acao_pedida``. A
      ficha e gravada num ponto de salvamento: se o banco recusar o retrato (o selo caiu entre a leitura e o INSERT,
      ``fichas_guarda``), o selo e lido de novo e o retrato de agora e gravado; se ainda assim nao der, a ficha nasce
      sem retrato ("nao guardado"). O selo nunca faz o pedido da pessoa falhar (A1);
    - o pedido igual a uma "nao confirmada" que saiu com numero guarda ``repete_operacao_id`` (``original_que_repete``,
      D16). Se o banco recusar a repeticao, a operacao nasce sem ela (numero novo no envio), nunca erro."""
    if rota.por_limite and rota.sinais is not None:
        raise ValueError("Com o alerta dos tres sinais nenhum limite vale: a acao vai para a ficha.")
    protocolo = await novo_protocolo(c)
    codigo = codigo_conferencia(ferramenta.sistema.id, ferramenta.nome_original, argumentos)
    chave = chave_pedido_igual(pessoa_id, aplicativo_id, ferramenta.id, argumentos)
    pessoa_teste = await _pessoa_de_teste(c, pessoa_id)
    repete = await original_que_repete(
        c, chave=chave, codigo=codigo, sistema_id=ferramenta.sistema.id, pessoa_id=pessoa_id
    )
    valores = {
        "pr": protocolo,
        "co": codigo,
        "pe": pessoa_id,
        "a": aplicativo_id,
        "o": ordem_id,
        "f": ferramenta.id,
        "s": ferramenta.sistema.id,
        "ar": json.dumps(argumentos, ensure_ascii=False),
        "diz": aplicativo_diz,
        "k": chave,
    }
    operacao_id = None
    if repete is not None:
        try:
            async with c.begin_nested():
                operacao_id = (await c.execute(text(_SQL_NOVA_OPERACAO), valores | {"r": repete})).scalar_one()
        except DBAPIError as erro:
            # O banco confere a mesma regra (operacoes_repete_confere): recusada, a operacao nasce sem repeticao.
            log.warning("pedido_igual_sem_repeticao", erro=type(erro).__name__)
            CONTADORES.somar("selo_pedido_igual_sem_repeticao")
        else:
            CONTADORES.somar("selo_pedido_igual_repete")
    if operacao_id is None:
        operacao_id = (await c.execute(text(_SQL_NOVA_OPERACAO), valores | {"r": None})).scalar_one()
    selo = await _selo_efetivo(c, ferramenta, ligada=selo_ligado, pessoa_teste=pessoa_teste)
    retrato = None if selo is None else selo.retrato()
    if rota.por_limite:
        await c.execute(
            text(
                "INSERT INTO fila_saida (operacao_id, estado, limite_id, hora_saida, mudou_por)"
                " VALUES (:o, 'liberada', :l, now() + make_interval(secs => :e), 'entrada')"
            ),
            {"o": operacao_id, "l": rota.limite_id, "e": espera_segundos},
        )
    else:
        ficha_id, retrato = await _gravar_ficha(
            c,
            {
                "o": operacao_id,
                "co": codigo,
                "m": prazo_ficha_minutos,
                "r": ferramenta.risco,
                "pk": rota.exige_passkey,
                "si": json.dumps(rota.sinais, ensure_ascii=False) if rota.sinais is not None else None,
            },
            retrato,
            sistema_id=ferramenta.sistema.id,
            ligada=selo_ligado,
            pessoa_teste=pessoa_teste,
        )
        await c.execute(
            text(
                "INSERT INTO fila_saida (operacao_id, estado, ficha_id, mudou_por)"
                " VALUES (:o, 'aguardando_ficha', :fi, 'entrada')"
            ),
            {"o": operacao_id, "fi": ficha_id},
        )
    # Para um programa, a linha diz "programa X, responsavel Y" (``programas.linha``), como nas leituras.
    linha = await programas.linha(
        c,
        aplicativo if aplicativo is not None and aplicativo.id == aplicativo_id else aplicativo_id,
        "acao_pedida",
        {"ferramenta": ferramenta.nome_vitrine, "protocolo": protocolo},
    )
    dados: dict[str, Any] = {
        "rota": rota.motivo,
        "risco": ferramenta.risco,
        "passkey": rota.exige_passkey,
        "tres_sinais": rota.sinais is not None,
    }
    if retrato is not None:
        dados["selo"] = retrato
    await registrar(
        c,
        papel="entrada",
        tipo=linha.tipo,
        campos=linha.campos,
        protocolo=protocolo,
        aplicativo_id=aplicativo_id,
        pessoa_id=pessoa_id,
        sistema_id=ferramenta.sistema.id,
        ferramenta=ferramenta.nome_vitrine,
        dados=dados,
        trace_id=trace_id,
    )
    if rota.sinais is not None:
        de_fora, privado = sinais.nomes_do_retrato(rota.sinais)
        await registrar(
            c,
            papel="entrada",
            tipo="tres_sinais",
            campos={"protocolo": protocolo, "de_fora": de_fora, "privado": privado},
            protocolo=protocolo,
            aplicativo_id=aplicativo_id,
            pessoa_id=pessoa_id,
            sistema_id=ferramenta.sistema.id,
            ferramenta=ferramenta.nome_vitrine,
            dados={"rota": rota.motivo, "janela_minutos": rota.sinais.get("janela_minutos")},
            trace_id=trace_id,
        )
    op = _existe(await por_id(c, operacao_id))
    return op


_SQL_NOVA_OPERACAO: Final = (
    "INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
    " ferramenta_id, sistema_id, argumentos, aplicativo_diz, chave_pedido_igual, repete_operacao_id)"
    " VALUES (:pr, :co, :pe, :a, :o, :f, :s, CAST(:ar AS jsonb), :diz, :k, :r) RETURNING id"
)
_SQL_NOVA_FICHA: Final = (
    "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco, exige_passkey, sinais, selo)"
    " VALUES (:o, :co, now() + make_interval(mins => :m), :r, :pk, CAST(:si AS jsonb), CAST(:selo AS jsonb))"
    " RETURNING id"
)


async def _pessoa_de_teste(c: AsyncConnection, pessoa_id: uuid.UUID) -> bool:
    """``pessoas.teste``, lido na transacao do pedido (como a porta de saida le no envio)."""
    valor = (await c.execute(text("SELECT teste FROM pessoas WHERE id = :p"), {"p": pessoa_id})).scalar_one_or_none()
    return valor is True


async def _selo_lido_agora(c: AsyncConnection, sistema_id: uuid.UUID) -> SeloVigente | None:
    """O selo vigente de agora (``selo_vigente()``), num ponto de salvamento: uma falha vira None, nunca erro (A1)."""
    try:
        async with c.begin_nested():
            return await vigente.ler(c, sistema_id)
    except (DBAPIError, ValueError) as erro:
        log.warning("selo_nao_lido_no_pedido", erro=type(erro).__name__)
        CONTADORES.somar("selo_nao_lido_no_pedido")
        return None


async def _efetivo_de_agora(
    c: AsyncConnection, sistema_id: uuid.UUID, *, ligada: bool, pessoa_teste: bool
) -> SeloVigente | None:
    """O selo efetivo da pessoa sobre o selo vigente de agora; None quando o selo nao pode ser lido."""
    agora = await _selo_lido_agora(c, sistema_id)
    return None if agora is None else vigente.efetivo(agora, ligada=ligada, pessoa_teste=pessoa_teste)


async def _selo_efetivo(
    c: AsyncConnection, ferramenta: FerramentaVitrine, *, ligada: bool, pessoa_teste: bool
) -> SeloVigente | None:
    """O selo efetivo da pessoa para a operacao: sobre o selo lido da vitrine (``ferramenta.sistema.selo_vigente``) ou,
    sem ele (``SELO_NAO_LIDO``), sobre o de agora. None quando o selo nao pode ser lido."""
    lido = ferramenta.sistema.selo_vigente
    if vigente.foi_lido(lido):
        return vigente.efetivo(lido, ligada=ligada, pessoa_teste=pessoa_teste)
    return await _efetivo_de_agora(c, ferramenta.sistema.id, ligada=ligada, pessoa_teste=pessoa_teste)


async def _gravar_ficha(
    c: AsyncConnection,
    valores: dict[str, Any],
    retrato: dict[str, Any] | None,
    *,
    sistema_id: uuid.UUID,
    ligada: bool,
    pessoa_teste: bool,
) -> tuple[uuid.UUID, dict[str, Any] | None]:
    """INSERT da ficha com o retrato do selo, num ponto de salvamento (``contrato.RETRATO_DO_SELO``). O banco confere o
    retrato contra ``selo_vigente`` agora (``fichas_guarda``, critica M3): se recusar (uma retirada, um teste novo ou
    uma ferramenta revisada entre a leitura e o INSERT), o selo e lido de novo e o retrato de agora e gravado (nivel
    1, se o selo caiu). Sem selo que o banco aceite, a ficha nasce sem retrato. Devolve o id e o retrato gravado."""
    if retrato is not None:
        try:
            async with c.begin_nested():
                ficha = (await c.execute(text(_SQL_NOVA_FICHA), valores | {"selo": json.dumps(retrato)})).scalar_one()
            return uuid.UUID(str(ficha)), retrato
        except DBAPIError as erro:
            log.info("ficha_retrato_relido", erro=type(erro).__name__)
            CONTADORES.somar("selo_ficha_retrato_relido")
        agora = await _efetivo_de_agora(c, sistema_id, ligada=ligada, pessoa_teste=pessoa_teste)
        if agora is not None:
            retrato = agora.retrato()
            try:
                async with c.begin_nested():
                    ficha = (
                        await c.execute(text(_SQL_NOVA_FICHA), valores | {"selo": json.dumps(retrato)})
                    ).scalar_one()
                return uuid.UUID(str(ficha)), retrato
            except DBAPIError as erro:
                log.warning("ficha_sem_retrato", erro=type(erro).__name__)
        CONTADORES.somar("selo_ficha_sem_retrato")
    ficha = (await c.execute(text(_SQL_NOVA_FICHA), valores | {"selo": None})).scalar_one()
    return uuid.UUID(str(ficha)), None


async def travar_teto(c: AsyncConnection, aplicativo_id: uuid.UUID) -> None:
    """Trava exclusiva do teto de fichas do aplicativo, ate o COMMIT: contar e montar a ficha viram um passo so.

    Chave propria, que so o pedido toma (depois das compartilhadas do alcance e da do pedido igual): nao
    disputa com bloqueio nem com o despachante."""
    await c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": travas.chave("teto", aplicativo_id)})


async def fichas_na_ultima_hora(c: AsyncConnection, aplicativo_id: uuid.UUID) -> int:
    """Fichas montadas na ultima hora, contadas so depois do fim da ultima pausa do teto deste aplicativo:
    quando o ADMIN reativa, as fichas de antes da pausa nao pausam o aplicativo de novo."""
    return int(
        (
            await c.execute(
                text(
                    "SELECT count(*) FROM fichas fi JOIN operacoes o ON o.id = fi.operacao_id"
                    " WHERE o.aplicativo_id = :a AND fi.montada_em > greatest(now() - interval '1 hour',"
                    " coalesce((SELECT max(b.encerrado_em) FROM bloqueios b WHERE b.aplicativo_id = :a"
                    " AND b.alcance = 'aplicativo' AND b.quem = :q), '-infinity'))"
                ),
                {"a": aplicativo_id, "q": QUEM_TETO},
            )
        ).scalar_one()
    )


async def em_experiencia(c: AsyncConnection, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, dias: int) -> bool:
    """Periodo de experiencia: conta da primeira ordem de servico da pessoa naquele aplicativo."""
    if dias <= 0:
        return False
    return bool(
        (
            await c.execute(
                text(
                    "SELECT coalesce(min(criado_em) > now() - make_interval(days => :d), true)"
                    " FROM ordens_servico WHERE pessoa_id = :p AND aplicativo_id = :a"
                ),
                {"p": pessoa_id, "a": aplicativo_id, "d": dias},
            )
        ).scalar_one()
    )


# Decisoes da pessoa


async def _ficha_travada(c: AsyncConnection, ficha_id: uuid.UUID, pessoa_id: uuid.UUID) -> Row[Any]:
    alvo = (
        await c.execute(
            text(
                "SELECT o.pessoa_id, o.aplicativo_id, o.sistema_id FROM fichas fi"
                " JOIN operacoes o ON o.id = fi.operacao_id WHERE fi.id = :f"
            ),
            {"f": ficha_id},
        )
    ).first()
    if alvo is None or alvo.pessoa_id != pessoa_id:
        raise DecisaoRecusada("ficha_desconhecida", "Ficha nao encontrada.")
    # Liberar uma acao passa pela mesma trava do envio e do bloqueio (ordem: consultiva, ficha, fila).
    await travas.compartilhar_envio(c, alvo.pessoa_id, alvo.aplicativo_id, alvo.sistema_id)
    linha = (
        await c.execute(
            text(
                "SELECT fi.id, fi.operacao_id, fi.codigo_conferencia, fi.expira_em <= now() AS vencida,"
                " fi.exige_passkey, fi.risco, fi.decisao, o.pessoa_id, o.protocolo, o.aplicativo_id, o.sistema_id,"
                " o.codigo_conferencia AS codigo_operacao,"
                " extract(epoch FROM now() - fi.montada_em) AS segundos"
                " FROM fichas fi JOIN operacoes o ON o.id = fi.operacao_id WHERE fi.id = :f FOR UPDATE OF fi"
            ),
            {"f": ficha_id},
        )
    ).first()
    if linha is None or linha.pessoa_id != pessoa_id:
        raise DecisaoRecusada("ficha_desconhecida", "Ficha nao encontrada.")
    if linha.decisao is not None:
        raise DecisaoRecusada("ficha_ja_decidida", f"Esta ficha ja foi {linha.decisao}. Nada mudou.")
    if linha.vencida:
        raise DecisaoRecusada("ficha_vencida", "O prazo desta ficha acabou. Nada seguiu.")
    return linha


async def confirmar_ficha(
    c: AsyncConnection,
    ficha_id: uuid.UUID,
    *,
    pessoa_id: uuid.UUID,
    com_passkey: bool,
    espera_segundos: int,
    rapida_segundos: int,
    login_refeito: bool = False,
    passkey_ligada: bool = True,
    numero_novo: bool = False,
    maximo_consultas: int | None = None,
) -> Operacao:
    """Confirma a ficha e libera a acao com a espera visivel. A ficha vale uma vez, so para esta operacao.

    Com a passkey ligada (``passkey_ligada``, de ``FEATURE_PASSKEY``), a ficha que pede passkey so confirma com
    ela. Com a passkey desligada (inclusive a ficha montada antes, quando pedia passkey), a acao de risco alto
    confirma com a passkey ou com o login refeito (``login_refeito``). O banco guarda como foi
    (``confirmada_com``) e recusa a ficha de risco alto confirmada so com a sessao.

    Fase 4, critica M4.2 (decisao 4 do coordenador): ``numero_novo`` e a escolha explicita da pessoa, na ficha do pedido
    igual, de mandar com numero novo, gravada em ``fichas.numero_novo`` na mesma transacao (o mesmo UPDATE da
    decisao). Vale so quando a original terminou a escada da consulta em ``desconhecido``
    (``Operacao.original_ficou_em_desconhecido``, com ``maximo_consultas``, o ``BADGE_LOOKUP_MAX_ATTEMPTS``, que e
    obrigatorio com ``numero_novo``). Fora disso, ``DecisaoRecusada("numero_novo_nao_cabe")`` e nada muda (a pessoa ve
    a ficha de novo); o banco confere de novo o que sabe (``fichas_guarda``) e, se recusar (uma resposta chegou entre a
    leitura e o UPDATE), e a mesma recusa. Sem a escolha, o envio leva o mesmo numero da original (o lado seguro), e o
    banco decide no envio (``envios_selo_nasce``). Com ela, o extrato da confirmacao leva ``dados.numero_novo``.
    """
    if numero_novo and maximo_consultas is None:
        raise ValueError("numero_novo pede maximo_consultas (BADGE_LOOKUP_MAX_ATTEMPTS)")
    f = await _ficha_travada(c, ficha_id, pessoa_id)
    if f.exige_passkey and passkey_ligada:
        if not com_passkey:
            raise DecisaoRecusada("falta_passkey", "Esta acao e de risco alto: confirme com a sua passkey.")
    elif (f.exige_passkey or f.risco == "alto") and not (com_passkey or login_refeito):
        raise DecisaoRecusada(
            "falta_login_refeito",
            "Esta acao e de risco alto: entre de novo nas telas do MCP-CLARO para confirmar. Nada seguiu.",
        )
    confirmada_com = "passkey" if com_passkey else "login_refeito" if login_refeito else "sessao"
    if f.codigo_conferencia != f.codigo_operacao:
        raise DecisaoRecusada("codigo_nao_bate", "O codigo de conferencia nao bate com o pedido. Nada seguiu.")
    if numero_novo and maximo_consultas is not None:
        atual = _existe(await por_id(c, f.operacao_id))
        if not atual.original_ficou_em_desconhecido(maximo_consultas):
            raise DecisaoRecusada("numero_novo_nao_cabe", NUMERO_NOVO_NAO_CABE)
    liberada = (
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = 'liberada', hora_saida = now() + make_interval(secs => :e),"
                " mudou_por = 'pessoa' WHERE operacao_id = :o AND estado = 'aguardando_ficha' RETURNING id"
            ),
            {"o": f.operacao_id, "e": espera_segundos},
        )
    ).scalar_one_or_none()
    if liberada is None:
        raise DecisaoRecusada("nao_esta_aguardando", "Esta acao nao espera mais confirmacao. Veja o protocolo.")
    valores = {"f": ficha_id, "p": pessoa_id, "pk": com_passkey, "s": f.segundos, "cc": confirmada_com}
    if numero_novo:
        try:
            async with c.begin_nested():
                await c.execute(text(_SQL_CONFIRMA_FICHA_NUMERO_NOVO), valores)
        except DBAPIError as erro:
            # A guarda do banco (fichas_guarda) recusou: entre a leitura e o UPDATE, o numero da original ganhou
            # resposta final, ou a original outra resposta a consulta. Nada muda: quem chama desfaz a liberacao junto
            # com a recusa.
            log.info("numero_novo_recusado_pelo_banco", erro=type(erro).__name__)
            raise DecisaoRecusada("numero_novo_nao_cabe", NUMERO_NOVO_NAO_CABE) from erro
        CONTADORES.somar("selo_numero_novo_escolhido")
    else:
        await c.execute(text(_SQL_CONFIRMA_FICHA), valores)
    dados: dict[str, Any] = {
        "rapida_demais": float(f.segundos) < rapida_segundos,
        "segundos": round(float(f.segundos), 1),
    }
    if numero_novo:
        dados["numero_novo"] = True
    await registrar(
        c,
        papel="entrada",
        tipo="ficha_confirmada",
        campos={"protocolo": f.protocolo, "como": COMO_CONFIRMOU[confirmada_com]},
        protocolo=f.protocolo,
        pessoa_id=pessoa_id,
        aplicativo_id=f.aplicativo_id,
        sistema_id=f.sistema_id,
        dados=dados,
    )
    op = _existe(await por_id(c, f.operacao_id))
    return op


_SQL_CONFIRMA_FICHA: Final = (
    "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), decidida_por = :p, com_passkey = :pk,"
    " segundos_ate_decidir = :s, confirmada_com = :cc WHERE id = :f AND decisao IS NULL"
)
_SQL_CONFIRMA_FICHA_NUMERO_NOVO: Final = (
    "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), decidida_por = :p, com_passkey = :pk,"
    " segundos_ate_decidir = :s, confirmada_com = :cc, numero_novo = true WHERE id = :f AND decisao IS NULL"
)
NUMERO_NOVO_NAO_CABE: Final = (
    "O numero novo so vale quando o sistema nao confirmou o primeiro pedido e a consulta terminou sem saber. Nada"
    " seguiu; veja a ficha de novo."
)


async def recusar_ficha(c: AsyncConnection, ficha_id: uuid.UUID, *, pessoa_id: uuid.UUID) -> Operacao:
    """Recusa a ficha e cancela a acao. So vale enquanto a acao espera a ficha, como a confirmacao.

    A acao que ja parou (bloqueio, restauracao) com a ficha ainda sem decisao nao muda: a recusa e recusada,
    sem gravar a decisao na ficha nem linha no extrato."""
    f = await _ficha_travada(c, ficha_id, pessoa_id)
    cancelada = (
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = 'cancelada', mudou_por = 'pessoa'"
                " WHERE operacao_id = :o AND estado = 'aguardando_ficha' RETURNING id"
            ),
            {"o": f.operacao_id},
        )
    ).scalar_one_or_none()
    if cancelada is None:
        atual = _existe(await por_id(c, f.operacao_id))
        raise DecisaoRecusada(
            "nao_esta_aguardando",
            f"Esta acao nao espera mais a sua decisao: a ficha nao foi recusada e nada mudou. {FRASE[atual.estado]}",
        )
    await c.execute(
        text(
            "UPDATE fichas SET decisao = 'recusada', decidida_em = now(), decidida_por = :p,"
            " segundos_ate_decidir = :s WHERE id = :f AND decisao IS NULL"
        ),
        {"f": ficha_id, "p": pessoa_id, "s": f.segundos},
    )
    await registrar(
        c,
        papel="entrada",
        tipo="ficha_recusada",
        campos={"protocolo": f.protocolo},
        protocolo=f.protocolo,
        pessoa_id=pessoa_id,
        aplicativo_id=f.aplicativo_id,
        sistema_id=f.sistema_id,
    )
    op = _existe(await por_id(c, f.operacao_id))
    return op


async def cancelar(
    c: AsyncConnection, operacao_id: uuid.UUID, *, pessoa_id: uuid.UUID, papel: str = "entrada"
) -> Operacao:
    """Cancela antes de sair. Se o despachante ja marcou "enviando", nada muda e a recusa diz o estado."""
    op = await por_id(c, operacao_id)
    if op is None or op.pessoa_id != pessoa_id:
        raise DecisaoRecusada("operacao_desconhecida", "Acao nao encontrada.")
    if op.ficha_id is not None:
        # Mesma ordem de travas da confirmacao: a ficha antes da linha da fila.
        await c.execute(text("SELECT id FROM fichas WHERE id = :f FOR UPDATE"), {"f": op.ficha_id})
    mudou = (
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = 'cancelada', mudou_por = 'pessoa'"
                " WHERE operacao_id = :o AND estado IN ('aguardando_ficha', 'liberada') RETURNING id"
            ),
            {"o": operacao_id},
        )
    ).scalar_one_or_none()
    if mudou is None:
        atual = _existe(await por_id(c, operacao_id))
        raise DecisaoRecusada("ja_saiu", f"Nao deu para cancelar. {FRASE[atual.estado]}")
    if op.ficha_id is not None:
        await c.execute(
            text(
                "UPDATE fichas SET decisao = 'recusada', decidida_em = now(), decidida_por = :p"
                " WHERE id = :f AND decisao IS NULL"
            ),
            {"f": op.ficha_id, "p": pessoa_id},
        )
    await registrar(
        c,
        papel=papel,
        tipo="acao_cancelada",
        campos={"protocolo": op.protocolo},
        protocolo=op.protocolo,
        pessoa_id=pessoa_id,
        aplicativo_id=op.aplicativo_id,
        sistema_id=op.sistema_id,
        ferramenta=op.nome_vitrine,
    )
    atual = _existe(await por_id(c, operacao_id))
    return atual


async def seguir_agora(c: AsyncConnection, operacao_id: uuid.UUID, *, pessoa_id: uuid.UUID) -> Operacao:
    """A pessoa dispensa o resto da espera. So vale enquanto a acao esta liberada e ainda esperando."""
    mudou = (
        await c.execute(
            text(
                "UPDATE fila_saida f SET hora_saida = now(), mudou_por = 'pessoa' FROM operacoes o"
                " WHERE f.operacao_id = o.id AND o.id = :o AND o.pessoa_id = :p AND f.estado = 'liberada'"
                " RETURNING f.id, o.protocolo, o.aplicativo_id, o.sistema_id"
            ),
            {"o": operacao_id, "p": pessoa_id},
        )
    ).first()
    if mudou is None:
        raise DecisaoRecusada("nao_esta_esperando", "Esta acao nao esta mais na espera.")
    await c.execute(text("SELECT pg_notify('mcp_claro_saida', :i)"), {"i": str(mudou.id)})
    await registrar(
        c,
        papel="entrada",
        tipo="seguir_agora",
        campos={"protocolo": mudou.protocolo},
        protocolo=mudou.protocolo,
        pessoa_id=pessoa_id,
        aplicativo_id=mudou.aplicativo_id,
        sistema_id=mudou.sistema_id,
    )
    op = _existe(await por_id(c, operacao_id))
    return op


async def decidir_nao_confirmada(
    c: AsyncConnection,
    operacao_id: uuid.UUID,
    *,
    pessoa_id: uuid.UUID,
    decisao: str,
    observacao: str | None,
    emails_admin: Sequence[str] = (),
) -> Operacao:
    """Registra o que a pessoa apurou sobre um resultado nao confirmado. A acao nunca e reenviada.

    Fase 4 (desenho 5.5, D19): se a decisao contraria o que o sistema informou (``contradiz_o_sistema``), o extrato
    ganha ``sistema_contradito`` e o ADMIN e avisado (``emails_admin``: ``NOTIFY_ADMIN_EMAILS``; os webhooks que
    assinam o evento recebem sempre). O selo do sistema fica: a pessoa pode ter se enganado, e o ADMIN decide."""
    if decisao not in DECISOES_NAO_CONFIRMADA:
        raise DecisaoRecusada("decisao_invalida", "Escolha: aconteceu, nao aconteceu ou nao sei.")
    op = await por_id(c, operacao_id)
    if op is None or op.pessoa_id != pessoa_id:
        raise DecisaoRecusada("operacao_desconhecida", "Acao nao encontrada.")
    if op.estado != Estado.NAO_CONFIRMADA:
        raise DecisaoRecusada("nao_e_nao_confirmada", "So um resultado nao confirmado pede esta decisao.")
    gravou = (
        await c.execute(
            text(
                "INSERT INTO decisoes_nao_confirmada (operacao_id, decisao, decidida_por, observacao)"
                " VALUES (:o, :d, :p, :obs) ON CONFLICT (operacao_id) DO NOTHING RETURNING id"
            ),
            {"o": operacao_id, "d": decisao, "p": pessoa_id, "obs": (observacao or "").strip()[:500] or None},
        )
    ).scalar_one_or_none()
    if gravou is None:
        raise DecisaoRecusada("ja_decidida", "Esta decisao ja foi registrada.")
    await registrar(
        c,
        papel="entrada",
        tipo="nao_confirmada_decidida",
        campos={"protocolo": op.protocolo, "decisao": DECISOES_NAO_CONFIRMADA[decisao]},
        protocolo=op.protocolo,
        pessoa_id=pessoa_id,
        aplicativo_id=op.aplicativo_id,
        sistema_id=op.sistema_id,
    )
    contrariado = contradiz_o_sistema(decisao, op.informado)
    if contrariado is not None:
        await _sistema_contradito(c, op, decisao, contrariado, emails_admin)
    atual = _existe(await por_id(c, operacao_id))
    return atual


DECISAO_PELO_SISTEMA: Final[Mapping[str, str]] = {
    "concluida": "aconteceu",
    "recusada": "nao_aconteceu",
    "nao_recebida": "nao_aconteceu",
    "parada": "nao_aconteceu",
}
"""A decisao que o botao "Registrar o que o sistema informou" grava para cada estado final (desenho 5.3). ``parcial`` e
``desfeita`` ficam de fora: "feito so em parte" e "feito e depois desfeito" nao cabem em "aconteceu" nem em "nao
aconteceu", e a pessoa decide com o formulario de sempre."""
OBSERVACAO_DO_SISTEMA: Final = "conforme o sistema informou"
"""A observacao que o botao grava na decisao (desenho 5.3)."""


def contradiz_o_sistema(decisao: str, informado: contrato.Informado | None) -> str | None:
    """O estado informado pelo sistema que a ``decisao`` da pessoa contraria, ou None (desenho 5.5, D19). Conta como o
    vocabulario (``contrato.CONTA_COMO``): "aconteceu" contra um estado que "nao aconteceu" (``recusada``,
    ``nao_recebida``, ``parada``), e "nao aconteceu" contra um que "aconteceu" ou "em parte" (``concluida``,
    ``parcial``). Vale o estado final do banco e a ultima resposta do sistema (uma ``nao_recebida`` sozinha tambem: o
    ADMIN fica sabendo). "Nao sei" nunca contraria; ``desfeita``, ``em_andamento`` e ``desconhecido`` tambem nao."""
    if informado is None or decisao not in ("aconteceu", "nao_aconteceu"):
        return None
    ditos = [informado.estado_final]
    if informado.desfecho == "respondeu":
        ditos.append(informado.estado)
    contrarios = ("nao_aconteceu",) if decisao == "aconteceu" else ("aconteceu", "em_parte")
    for estado in ditos:
        if estado is not None and contrato.CONTA_COMO.get(estado) in contrarios:
            return estado
    return None


async def _sistema_contradito(
    c: AsyncConnection, op: Operacao, decisao: str, estado: str, emails_admin: Sequence[str]
) -> None:
    """A linha ``sistema_contradito`` no extrato da pessoa e o aviso ao ADMIN, na transacao da decisao. Nada mais muda:
    o selo fica (D19)."""
    await registrar(
        c,
        papel="entrada",
        tipo="sistema_contradito",
        campos={"protocolo": op.protocolo, "sistema": op.sistema_nome},
        protocolo=op.protocolo,
        pessoa_id=op.pessoa_id,
        aplicativo_id=op.aplicativo_id,
        sistema_id=op.sistema_id,
        dados={"estado": estado, "decisao": decisao, "de_terceiro": op.de_terceiro},
    )
    assunto, texto = aviso_admin("sistema_contradito", protocolo=op.protocolo, sistema=op.sistema_nome)
    await avisar_admin(
        c,
        "sistema_contradito",
        assunto,
        {"texto": texto, "sistema": op.sistema_nome, "sistema_id": str(op.sistema_id), "protocolo": op.protocolo},
        f"sistema_contradito:{op.id}",
        emails_admin,
    )
    CONTADORES.somar("selo_sistema_contradito")


async def registrar_o_que_o_sistema_informou(
    c: AsyncConnection,
    operacao_id: uuid.UUID,
    *,
    pessoa_id: uuid.UUID,
    estado_visto: str,
    emails_admin: Sequence[str] = (),
) -> Operacao:
    """O botao "Registrar o que o sistema informou" (desenho 5.3): grava a decisao da pessoa de acordo com o estado
    final que o sistema informou (``DECISAO_PELO_SISTEMA``), com a observacao ``OBSERVACAO_DO_SISTEMA``, por
    ``decidir_nao_confirmada``. Quem decide continua sendo a pessoa, com um toque; nada e reenviado.

    ``estado_visto`` e o estado final que a tela mostrou a pessoa (o formulario o manda, decisao 6 do coordenador): o
    toque registra o que ela viu, e nunca uma resposta que chegou depois.

    Recusa (``DecisaoRecusada``), sem gravar nada:

    - ``sem_resposta_final``: o sistema nao deu estado final (``selo_estado_final``); uma ``nao_recebida`` sozinha nao
      basta (critica A3.3);
    - ``resultado_em_outro_protocolo``: outro protocolo saiu depois com o mesmo numero, e o que o sistema informar
      aparece nele (``Operacao.substituto``, critica M4);
    - ``sistema_se_contradisse``: alguma resposta do sistema sobre o numero desta acao
      (``informacoes_sistema.contradiz``, ``InformadoDoSistema.se_contradisse``) nao combina com outra; o que ele diz
      nao vale um toque, e a pessoa decide pelo formulario;
    - ``informado_mudou``: o estado final de agora nao e o que a pessoa viu (``estado_visto``); ela ve a tela de novo;
    - ``terceiro_nao_recebida``: um servidor de terceiros disse ``nao_recebida``; o que ele informa e texto de
      estranhos, e a pessoa confere por fora antes de decidir (critica A3.3);
    - ``decida_pelo_formulario``: ``parcial`` ou ``desfeita``, que nao cabem num toque;
    - e as de ``decidir_nao_confirmada`` (operacao de outra pessoa, que nao esta nao confirmada, ja decidida)."""
    op = await por_id(c, operacao_id)
    if op is None or op.pessoa_id != pessoa_id:
        raise DecisaoRecusada("operacao_desconhecida", "Acao nao encontrada.")
    if op.estado != Estado.NAO_CONFIRMADA:
        raise DecisaoRecusada("nao_e_nao_confirmada", "So um resultado nao confirmado pede esta decisao.")
    if op.substituto is not None:
        raise DecisaoRecusada(
            "resultado_em_outro_protocolo",
            f"O protocolo {op.substituto} saiu depois com o mesmo numero; o que o sistema informar aparece nele."
            " Nada foi registrado.",
        )
    final = op.informado.estado_final if op.informado is not None else None
    if final is None:
        raise DecisaoRecusada(
            "sem_resposta_final",
            "O sistema ainda nao deu uma resposta final sobre este pedido. Nada foi registrado; decida pelo"
            " formulario.",
        )
    if op.informado is not None and op.informado.se_contradisse:
        raise DecisaoRecusada(
            "sistema_se_contradisse",
            "O sistema deu respostas que nao combinam sobre este pedido. Nada foi registrado; confira no sistema e"
            " decida pelo formulario.",
        )
    if final != estado_visto:
        raise DecisaoRecusada(
            "informado_mudou",
            "O que o sistema informou mudou depois que a tela foi aberta. Nada foi registrado; veja a tela de novo.",
        )
    if op.de_terceiro and final == "nao_recebida":
        raise DecisaoRecusada(
            "terceiro_nao_recebida",
            "Um servidor de terceiros disse que nao recebeu o pedido. Confira no proprio servidor antes de decidir."
            " Nada foi registrado.",
        )
    decisao = DECISAO_PELO_SISTEMA.get(final)
    if decisao is None:
        raise DecisaoRecusada(
            "decida_pelo_formulario",
            "O que o sistema informou nao cabe num toque (feito so em parte, ou feito e desfeito). Nada foi"
            " registrado; decida pelo formulario.",
        )
    return await decidir_nao_confirmada(
        c,
        operacao_id,
        pessoa_id=pessoa_id,
        decisao=decisao,
        observacao=OBSERVACAO_DO_SISTEMA,
        emails_admin=emails_admin,
    )


# Despachante


async def marcar_enviando(c: AsyncConnection, op: Operacao) -> bool:
    mudou = (
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = 'enviando', mudou_por = 'despachante'"
                " WHERE id = :i AND estado = 'liberada' RETURNING id"
            ),
            {"i": op.fila_id},
        )
    ).scalar_one_or_none()
    return mudou is not None


async def parar(
    c: AsyncConnection,
    op: Operacao,
    estado: Estado,
    motivo: str,
    *,
    quem: str = "despachante",
    papel: str = "despachante",
) -> bool:
    """Para uma acao antes de sair (bloqueio, ordem que mudou, codigo que nao bate)."""
    if estado not in (Estado.CANCELADA, Estado.PARADA_POR_BLOQUEIO, Estado.PARADA_POR_RESTAURACAO):
        raise ValueError(f"Parar so leva a cancelada ou parada, nao a {estado}.")
    mudou = (
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = :e, mudou_por = :q"
                " WHERE id = :i AND estado IN ('aguardando_ficha', 'liberada') RETURNING id"
            ),
            {"i": op.fila_id, "e": str(estado), "q": quem},
        )
    ).scalar_one_or_none()
    if mudou is None:
        return False
    await c.execute(
        text("UPDATE operacoes SET desfecho = :d WHERE id = :o AND desfecho IS NULL"), {"o": op.id, "d": motivo}
    )
    await registrar(
        c,
        papel=papel,
        tipo="acao_parada",
        campos={"protocolo": op.protocolo, "motivo": motivo},
        protocolo=op.protocolo,
        pessoa_id=op.pessoa_id,
        aplicativo_id=op.aplicativo_id,
        sistema_id=op.sistema_id,
        ferramenta=op.nome_vitrine,
    )
    return True


_TIPO_FINAL: Final = {
    Estado.ENVIADA: "acao_enviada",
    Estado.NAO_CONFIRMADA: "acao_nao_confirmada",
    Estado.NAO_SAIU: "acao_nao_saiu",
    Estado.SISTEMA_PEDIU_DADOS: "acao_sistema_pediu_dados",
}


async def finalizar(
    c: AsyncConnection,
    op: Operacao,
    estado: Estado,
    resultado: dict[str, Any] | None,
    desfecho: str,
    *,
    pedidos: Sequence[str] = (),
    selo: Mapping[str, Any] | None = None,
) -> bool:
    """Grava o fim do envio. Se outra copia ja deu a acao como nao confirmada, so guarda a resposta.

    ``pedidos``: as categorias do que o sistema pediu no meio da chamada (``Resposta.pedidos``, nosso vocabulario),
    em ``dados.pedidos`` da linha do fim, para a tela de pedidos recusados do ADMIN mostrar tambem as acoes: sempre
    em ``acao_sistema_pediu_dados`` e ``acao_enviada`` (como antes, lista vazia sem pedido) e, com pedido, tambem em
    ``acao_nao_confirmada`` e ``acao_nao_saiu`` (contrato K9).

    ``selo`` (Fase 4, desenho 7.2): o retrato do selo com que a acao saiu (``contrato.RETRATO_DO_SELO``, o da hora do
    passo), em ``dados.selo`` da linha do fim; quem envia (a porta de saida) o passa. Sem ele, a linha fica como antes.

    Nova tentativa depois de um COMMIT que deu certo, mas cuja confirmacao se perdeu: a linha ja tem o que este
    fim gravou (``_fim_ja_gravado``), antes ou depois de uma retomada. Nada e gravado de novo."""
    if estado not in _TIPO_FINAL:
        raise ValueError(f"O fim de um envio nao pode ser {estado}.")
    saiu = estado != Estado.NAO_SAIU
    mudou = (
        await c.execute(
            text(
                "UPDATE fila_saida SET estado = :e, mudou_por = 'despachante',"
                " enviada_em = CASE WHEN :saiu THEN now() ELSE NULL END"
                " WHERE id = :i AND estado = 'enviando' RETURNING id"
            ),
            {"i": op.fila_id, "e": str(estado), "saiu": saiu},
        )
    ).scalar_one_or_none()
    if mudou is None and await _fim_ja_gravado(c, op, estado, desfecho):
        return True
    await c.execute(
        text("UPDATE operacoes SET resultado = CAST(:r AS jsonb), desfecho = :d WHERE id = :o"),
        {
            "o": op.id,
            "r": json.dumps(resultado, ensure_ascii=False) if resultado is not None else None,
            "d": desfecho if mudou is not None else desfecho + DEPOIS_DA_RETOMADA,
        },
    )
    tipo = _TIPO_FINAL[estado]
    campos: dict[str, object] = {"protocolo": op.protocolo}
    dados: dict[str, Any] = {"desfecho": desfecho[:120], "gravado": mudou is not None}
    if tipo == "acao_enviada":
        campos["ferramenta"] = op.nome_vitrine
        dados["texto_de_fora"] = op.de_estranhos
    elif tipo == "acao_nao_saiu":
        campos["motivo"] = desfecho
    if pedidos or tipo in ("acao_enviada", "acao_sistema_pediu_dados"):
        dados["pedidos"] = list(pedidos)
    if selo is not None:
        dados["selo"] = dict(selo)
    await registrar(
        c,
        papel="despachante",
        tipo=tipo,
        campos=campos,
        protocolo=op.protocolo,
        pessoa_id=op.pessoa_id,
        aplicativo_id=op.aplicativo_id,
        sistema_id=op.sistema_id,
        ferramenta=op.nome_vitrine,
        dados=dados,
    )
    return mudou is not None


async def _fim_ja_gravado(c: AsyncConnection, op: Operacao, estado: Estado, desfecho: str) -> bool:
    """A linha ja tem o que este fim de envio grava. Dois casos:

    - gravado pelo fim do envio: o mesmo estado, ``mudou_por = 'despachante'`` e o mesmo desfecho;
    - depois de uma retomada (``despachante:retomada`` ou ``restauracao``): a linha e nao confirmada, e o
      desfecho e o deste fim com a marca ``DEPOIS_DA_RETOMADA``.

    Depois de "enviando", so o fim do envio e a retomada levam a linha a um estado final, e estado final nao
    muda mais. Um envio so tem um fim, e so ele escreve o desfecho com a marca: achar isso quer dizer que a
    tentativa anterior chegou ao banco."""
    linha = (
        await c.execute(
            text(
                "SELECT f.estado, f.mudou_por, o.desfecho FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"
                " WHERE f.id = :i"
            ),
            {"i": op.fila_id},
        )
    ).first()
    if linha is None:
        return False
    if linha.mudou_por == "despachante":
        return (linha.estado, linha.desfecho) == (str(estado), desfecho)
    return (linha.estado, linha.desfecho) == (str(Estado.NAO_CONFIRMADA), desfecho + DEPOIS_DA_RETOMADA)


async def retomar_enviando(c: AsyncConnection, *, quem: str = QUEM_RETOMA) -> list[str]:
    """Quem assume a porta (ou a partida depois de queda) acha acoes em "enviando": viram nao confirmadas."""
    linhas = (
        await c.execute(
            text(
                "WITH alvo AS (SELECT f.id FROM fila_saida f WHERE f.estado = 'enviando' ORDER BY f.id FOR UPDATE)"
                " UPDATE fila_saida f SET estado = 'nao_confirmada', mudou_por = :q FROM alvo, operacoes o"
                " WHERE f.id = alvo.id AND f.operacao_id = o.id AND f.estado = 'enviando'"
                " RETURNING o.id, o.protocolo, o.pessoa_id, o.aplicativo_id, o.sistema_id"
            ),
            {"q": quem},
        )
    ).all()
    for linha in linhas:
        await c.execute(
            text("UPDATE operacoes SET desfecho = 'interrompida no envio' WHERE id = :o AND desfecho IS NULL"),
            {"o": linha.id},
        )
        await registrar(
            c,
            papel="despachante",
            tipo="acao_nao_confirmada",
            campos={"protocolo": linha.protocolo},
            protocolo=linha.protocolo,
            pessoa_id=linha.pessoa_id,
            aplicativo_id=linha.aplicativo_id,
            sistema_id=linha.sistema_id,
            dados={"desfecho": "interrompida no envio"},
        )
    return [linha.protocolo for linha in linhas]


# Bloqueio e restauracao


def _filtro_alcance(b: Bloqueio) -> tuple[str, dict[str, Any]]:
    """Trecho fixo de SQL por alcance; os valores vao sempre como parametros."""
    if b.alcance == "pessoa":
        return "o.pessoa_id = :bp", {"bp": b.pessoa_id}
    if b.alcance == "aplicativo":
        return "o.aplicativo_id = :ba", {"ba": b.aplicativo_id}
    if b.alcance == "pessoa_aplicativo":
        return "o.pessoa_id = :bp AND o.aplicativo_id = :ba", {"bp": b.pessoa_id, "ba": b.aplicativo_id}
    if b.alcance == "sistema":
        return "o.sistema_id = :bs", {"bs": b.sistema_id}
    return "true", {}


@dataclass(frozen=True, slots=True)
class Comprovante:
    """Comprovante de bloqueio em tres partes, como na proposta.

    - ``paradas``: o que paramos a tempo (``{"protocolo", "estava"}``);
    - ``ja_saidas``: o que ja tinha seguido: enviada, sistema pediu dados, e nao confirmada que a pessoa
      registrou como acontecida (``{"protocolo", "estado", "enviada_em"}``);
    - ``em_conferencia``: o que ainda esta em conferencia: enviando agora, e nao confirmada sem decisao ou
      com "nao sei" (``{"protocolo", "estado", "desde"}``).

    Fase 4 (desenho 6.3, critica A1): ``informado`` e o que os sistemas informaram sobre as operacoes do comprovante
    (``{"protocolo", "sistema", "estado", "origem", "gravado_em", "de_terceiro"}``): as respostas ao aviso deste
    bloqueio e a ultima resposta de recibo ou de consulta de cada operacao, so com ``respondeu``. Sai da leitura
    (``comprovante_do_bloqueio``), nunca da linha de ``comprovantes``, que nao muda (``informado`` la continua ``[]``).
    So as categorias do contrato e a hora do gateway, nunca texto nem hora do sistema. ``avisos`` e o andamento de cada
    aviso do bloqueio (``{"sistema", "estado", "de_terceiro"}``, ``contrato.ESTADOS_DO_AVISO``), para a tela dizer
    "avisamos os sistemas" ou "o sistema nao respondeu ao aviso".
    """

    bloqueio_id: uuid.UUID
    paradas: list[dict[str, Any]]
    ja_saidas: list[dict[str, Any]]
    informado: list[dict[str, Any]]
    em_conferencia: list[dict[str, Any]] = field(default_factory=list)
    avisos: list[dict[str, Any]] = field(default_factory=list)


async def parar_por_bloqueio(c: AsyncConnection, b: Bloqueio, *, papel: str, janela_horas: int = 24) -> Comprovante:
    """Na transacao que liga o bloqueio (com a trava exclusiva do alcance ja tomada): para o que ainda nao
    saiu e monta o comprovante em tres partes. As linhas paradas sao travadas em ordem de ``id``.

    Fase 4 (critica A1): esta transacao e a da Fase 2 e nao cria aviso de bloqueio nem le selo. O despachante deriva
    os avisos depois, do comprovante gravado aqui (que nao muda) e de ``envios_selo``; o que os sistemas informarem
    aparece na leitura (``comprovante_do_bloqueio``)."""
    filtro, valores = _filtro_alcance(b)
    paradas = (
        await c.execute(
            text(
                "WITH alvo AS (SELECT f.id FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"  # noqa: S608  # nosec B608
                f" WHERE f.estado IN ('aguardando_ficha', 'liberada') AND {filtro} ORDER BY f.id FOR UPDATE OF f)"
                " UPDATE fila_saida f SET estado = 'parada_por_bloqueio', mudou_por = :q FROM alvo, operacoes o"
                " WHERE f.id = alvo.id AND f.operacao_id = o.id AND f.estado IN ('aguardando_ficha', 'liberada')"
                " RETURNING o.id, o.protocolo, old.estado AS antes, o.pessoa_id, o.aplicativo_id, o.sistema_id,"
                " o.criada_em"
            ),
            {"q": f"bloqueio:{b.id}", **valores},
        )
    ).all()
    paradas = sorted(paradas, key=lambda linha: (linha.criada_em, linha.protocolo))
    for linha in paradas:
        await c.execute(
            text("UPDATE operacoes SET desfecho = :d WHERE id = :o AND desfecho IS NULL"),
            {"o": linha.id, "d": b.motivo_recusa()},
        )
    # Ja seguiu: nas ultimas ``janela_horas``. Em conferencia: enviando agora ou nao confirmada sem decisao (ou
    # com "nao sei"), de qualquer idade. A nao confirmada que a pessoa disse que nao aconteceu fica de fora.
    saidas = (
        await c.execute(
            text(
                "SELECT o.protocolo, f.estado, f.enviada_em, f.mudou_em, d.decisao FROM fila_saida f"  # noqa: S608  # nosec B608
                " JOIN operacoes o ON o.id = f.operacao_id"
                f" LEFT JOIN decisoes_nao_confirmada d ON d.operacao_id = o.id WHERE {filtro} AND ("
                " f.estado = 'enviando'"
                " OR (f.estado = 'nao_confirmada' AND (d.decisao IS NULL OR d.decisao = 'nao_sei'))"
                " OR (f.mudou_em > now() - make_interval(hours => :h)"
                " AND (f.estado IN ('enviada', 'sistema_pediu_dados')"
                " OR (f.estado = 'nao_confirmada' AND d.decisao = 'aconteceu'))))"
                " ORDER BY o.criada_em, o.protocolo"
            ),
            {"h": janela_horas, **valores},
        )
    ).all()
    em_conferencia = [
        s for s in saidas if s.estado == "enviando" or (s.estado == "nao_confirmada" and s.decisao != "aconteceu")
    ]
    conferindo = {s.protocolo for s in em_conferencia}
    comprovante = Comprovante(
        bloqueio_id=b.id,
        paradas=[{"protocolo": p.protocolo, "estava": p.antes} for p in paradas],
        ja_saidas=[
            {
                "protocolo": s.protocolo,
                "estado": s.estado,
                "enviada_em": s.enviada_em.isoformat() if s.enviada_em else None,
            }
            for s in saidas
            if s.protocolo not in conferindo
        ],
        informado=[],
        em_conferencia=[
            {"protocolo": s.protocolo, "estado": s.estado, "desde": s.mudou_em.isoformat()} for s in em_conferencia
        ],
    )
    await c.execute(
        text(
            "INSERT INTO comprovantes (bloqueio_id, paradas, ja_saidas, em_conferencia, informado)"
            " VALUES (:b, CAST(:p AS jsonb), CAST(:j AS jsonb), CAST(:e AS jsonb), '[]'::jsonb)"
        ),
        {
            "b": b.id,
            "p": json.dumps(comprovante.paradas),
            "j": json.dumps(comprovante.ja_saidas),
            "e": json.dumps(comprovante.em_conferencia),
        },
    )
    await registrar(
        c,
        papel=papel,
        tipo="comprovante_bloqueio_partes",
        campos={
            "paradas": len(comprovante.paradas),
            "ja_saidas": len(comprovante.ja_saidas),
            "em_conferencia": len(comprovante.em_conferencia),
        },
        pessoa_id=b.pessoa_id,
        aplicativo_id=b.aplicativo_id,
        sistema_id=b.sistema_id,
        dados={"bloqueio_id": str(b.id)},
    )
    return comprovante


# Quarentena de um servidor de terceiros (Fase 3)


async def parar_por_quarentena(
    c: AsyncConnection, sistema_id: uuid.UUID, *, motivo: str, papel: str, quem: str
) -> list[str]:
    """Na transacao que poe o servidor em quarentena (com a trava exclusiva do sistema ja tomada e, antes, os
    limites suspensos por ``suspender_limites``): o que espera a ficha ou esta liberado vira ``cancelada``, com
    ``motivo`` no desfecho e ``acao_parada`` no extrato de cada pessoa. O que ja esta "enviando" segue: ja saiu. As
    linhas sao travadas em ordem de ``id``.

    A ficha nao muda: confirmar depois nao libera nada, porque a linha da fila ja nao espera a ficha.
    Devolve os protocolos parados."""
    linhas = (
        await c.execute(
            text(
                _SQL_OPERACAO + " WHERE o.sistema_id = :s AND f.estado IN ('aguardando_ficha', 'liberada')"
                " ORDER BY f.id FOR UPDATE OF f"
            ),
            {"s": sistema_id},
        )
    ).all()
    paradas: list[str] = []
    for linha in linhas:
        op = _operacao(linha)
        if await parar(c, op, Estado.CANCELADA, motivo, quem=quem, papel=papel):
            paradas.append(op.protocolo)
    return paradas


SQL_SUSPENDER_LIMITES = """
WITH alvo AS (
  SELECT id FROM limites WHERE sistema_id = :s AND situacao = 'ativo' ORDER BY id FOR UPDATE
), mudou AS (
  UPDATE limites l SET situacao = 'suspenso', motivo = :m
  FROM alvo WHERE l.id = alvo.id AND l.situacao = 'ativo'
  RETURNING l.id, l.pessoa_id, l.aplicativo_id, l.nome_original
)
SELECT mudou.id, mudou.pessoa_id, mudou.aplicativo_id,
       coalesce(
         (SELECT r.nome_vitrine FROM ferramentas_revisadas r
          WHERE r.sistema_id = :s AND r.nome_original = mudou.nome_original
          ORDER BY (r.situacao = 'ativa') DESC, r.versao DESC LIMIT 1),
         mudou.nome_original) AS ferramenta
FROM mudou
ORDER BY mudou.id
"""


async def suspender_limites(c: AsyncConnection, sistema_id: uuid.UUID, *, motivo: str, papel: str) -> int:
    """Suspende todos os limites ativos do sistema (critica M2), com ``motivo`` e ``limite_suspenso`` no extrato
    de cada pessoa. So de ``ativo`` para ``suspenso`` (a guarda do banco so deixa o despachante fazer isso). A
    pessoa refaz o limite depois da nova revisao. Devolve quantos foram suspensos.

    Chamada antes de ``parar_por_quarentena`` (a linha do limite antes da linha da fila: ver o topo do modulo)."""
    linhas = (await c.execute(text(SQL_SUSPENDER_LIMITES), {"s": sistema_id, "m": motivo})).all()
    for linha in linhas:
        await registrar(
            c,
            papel=papel,
            tipo="limite_suspenso",
            campos={"ferramenta": linha.ferramenta, "motivo": motivo},
            pessoa_id=uuid.UUID(str(linha.pessoa_id)),
            aplicativo_id=uuid.UUID(str(linha.aplicativo_id)),
            sistema_id=sistema_id,
            ferramenta=linha.ferramenta,
            dados={"limite_id": str(linha.id), "situacao": "suspenso"},
        )
    return len(linhas)


# O que os sistemas informaram sobre as operacoes do comprovante (as ja saidas e as em conferencia, pelo protocolo) e
# sobre as dos avisos deste bloqueio (critica B9: em aberto no alcance, fora do comprovante): a resposta ao aviso deste
# bloqueio e a ultima de recibo ou de consulta, so com 'respondeu'. Nunca envios_selo.chave (critica M10).
_SQL_INFORMADO_DO_BLOQUEIO: Final = """
WITH alvo AS (
  SELECT o.id, o.protocolo, o.criada_em, s.nome AS sistema, s.origem <> 'proprio' AS de_terceiro
  FROM comprovantes c
  CROSS JOIN LATERAL jsonb_array_elements(c.ja_saidas || c.em_conferencia) p
  JOIN operacoes o ON o.protocolo = p ->> 'protocolo'
  JOIN sistemas s ON s.id = o.sistema_id
  WHERE c.bloqueio_id = :b
  UNION
  SELECT o.id, o.protocolo, o.criada_em, s.nome, s.origem <> 'proprio'
  FROM avisos_bloqueio a
  CROSS JOIN LATERAL unnest(a.operacoes) x
  JOIN operacoes o ON o.id = x
  JOIN sistemas s ON s.id = o.sistema_id
  WHERE a.bloqueio_id = :b
)
SELECT alvo.protocolo, alvo.sistema, alvo.de_terceiro, inf.estado, inf.origem, inf.gravado_em
FROM alvo
CROSS JOIN LATERAL (
  (SELECT i.estado, i.origem, i.gravado_em, i.id FROM informacoes_sistema i
   JOIN avisos_bloqueio a ON a.id = i.aviso_id AND a.bloqueio_id = :b
   WHERE i.operacao_id = alvo.id AND i.origem = 'aviso' AND i.desfecho = 'respondeu')
  UNION ALL
  (SELECT i.estado, i.origem, i.gravado_em, i.id FROM informacoes_sistema i
   WHERE i.operacao_id = alvo.id AND i.origem IN ('recibo', 'consulta') AND i.desfecho = 'respondeu'
   ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1)
) inf
ORDER BY alvo.criada_em, alvo.protocolo, inf.gravado_em, inf.id
"""


async def comprovante_do_bloqueio(c: AsyncConnection, bloqueio_id: uuid.UUID) -> Comprovante | None:
    """O comprovante do bloqueio: as tres partes da linha de ``comprovantes`` (que nao muda) e, da leitura de agora, o
    que os sistemas informaram (``Comprovante.informado``) e o andamento dos avisos (``Comprovante.avisos``)."""
    linha = (
        await c.execute(
            text(
                "SELECT bloqueio_id, paradas, ja_saidas, em_conferencia, informado FROM comprovantes"
                " WHERE bloqueio_id = :b"
            ),
            {"b": bloqueio_id},
        )
    ).first()
    if linha is None:
        return None
    informado = [
        {
            "protocolo": i.protocolo,
            "sistema": i.sistema,
            "estado": i.estado,
            "origem": i.origem,
            "gravado_em": i.gravado_em.isoformat(),
            "de_terceiro": bool(i.de_terceiro),
        }
        for i in (await c.execute(text(_SQL_INFORMADO_DO_BLOQUEIO), {"b": bloqueio_id})).all()
    ]
    avisos = [
        {"sistema": a.sistema, "estado": a.estado, "de_terceiro": bool(a.de_terceiro)}
        for a in (
            await c.execute(
                text(
                    "SELECT s.nome AS sistema, a.estado, s.origem <> 'proprio' AS de_terceiro FROM avisos_bloqueio a"
                    " JOIN sistemas s ON s.id = a.sistema_id WHERE a.bloqueio_id = :b"
                    " ORDER BY s.nome, a.criado_em, a.parte, a.id"
                ),
                {"b": bloqueio_id},
            )
        ).all()
    ]
    return Comprovante(
        linha.bloqueio_id,
        _json(linha.paradas),
        _json(linha.ja_saidas),
        informado,
        _json(linha.em_conferencia),
        avisos,
    )


async def saltar_protocolo(c: AsyncConnection) -> None:
    """A sequencia do protocolo anda ``SALTO_NA_RESTAURACAO`` para frente.

    Se o banco voltou de uma copia, os numeros dados depois dela se perderam com ela, mas as pessoas e os
    aplicativos podem guarda-los: nenhum deles e dado de novo a outra acao."""
    await c.execute(
        text("SELECT setval('protocolo_seq', (SELECT last_value FROM protocolo_seq) + :n)"),
        {"n": SALTO_NA_RESTAURACAO},
    )


async def parar_por_restauracao(c: AsyncConnection, *, papel: str = "despachante") -> tuple[int, int]:
    """Banco restaurado: o que estava "enviando" fica nao confirmado; o que esperava para por restauracao.

    Nada sai sem nova confirmacao. Devolve (paradas, nao confirmadas).

    - Toma a trava exclusiva de ``tudo`` (a mesma de fechar a saida) antes de qualquer linha, e trava as
      linhas em ordem de ``id``: sem impasse com o vencimento de fichas nem com o bloqueio.
    - O "enviando" so vira nao confirmado se ninguem segura a trava ``DESPACHANTE_UNICO``. Se outro despachante
      vivo a segura, aquelas linhas sao dele, no meio do envio: ele mesmo grava o fim (e, se cair, quem assumir
      faz a retomada). Fica registrado quantas eram.
    - A sequencia do protocolo salta ``SALTO_NA_RESTAURACAO`` para frente: numero dado depois da copia, e
      perdido com ela, nao e dado de novo a outra acao.
    """
    await travas.exclusiva(c, [travas.TUDO])
    await saltar_protocolo(c)
    livre = bool(
        (await c.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": travas.DESPACHANTE_UNICO})).scalar_one()
    )
    com_dono = 0
    if livre:
        duvida = await retomar_enviando(c, quem="restauracao")
    else:
        duvida = []
        com_dono = int(
            (await c.execute(text("SELECT count(*) FROM fila_saida WHERE estado = 'enviando'"))).scalar_one()
        )
    paradas = (
        await c.execute(
            text(
                "WITH alvo AS (SELECT f.id FROM fila_saida f WHERE f.estado IN ('aguardando_ficha', 'liberada')"
                " ORDER BY f.id FOR UPDATE)"
                " UPDATE fila_saida f SET estado = 'parada_por_restauracao', mudou_por = 'restauracao'"
                " FROM alvo WHERE f.id = alvo.id AND f.estado IN ('aguardando_ficha', 'liberada')"
                " RETURNING f.operacao_id AS id"
            )
        )
    ).all()
    for linha in paradas:
        await c.execute(
            text("UPDATE operacoes SET desfecho = 'parada por restauracao' WHERE id = :o AND desfecho IS NULL"),
            {"o": linha.id},
        )
    await registrar(
        c,
        papel=papel,
        tipo="fila_parada_restauracao",
        campos={"paradas": len(paradas), "duvida": len(duvida)},
        dados={"enviando_com_dono": com_dono},
    )
    if com_dono:
        await registrar(c, papel=papel, tipo="fila_restauracao_envio_com_dono", campos={"enviando": com_dono})
    return len(paradas), len(duvida)


async def vencer_fichas(c: AsyncConnection, *, papel: str = "rotinas", lote: int = 500) -> list[str]:
    """Fichas sem resposta no prazo: vencem, e nada segue.

    Mesma ordem de travas do envio e do bloqueio: primeiro as consultivas compartilhadas do alcance de todas
    as fichas do lote (em ordem), depois as fichas (por ``id``, pulando as que outra transacao segura), depois
    as linhas da fila (por ``id``). Um bloqueio ou uma restauracao ao mesmo tempo espera, sem impasse.
    """
    candidatas = (
        await c.execute(
            text(
                "SELECT fi.id, o.pessoa_id, o.aplicativo_id, o.sistema_id FROM fichas fi"
                " JOIN operacoes o ON o.id = fi.operacao_id WHERE fi.decisao IS NULL AND fi.expira_em <= now()"
                " ORDER BY fi.expira_em, fi.id LIMIT :n"
            ),
            {"n": lote},
        )
    ).all()
    if not candidatas:
        return []
    chaves = sorted({k for x in candidatas for k in travas.chaves_do_envio(x.pessoa_id, x.aplicativo_id, x.sistema_id)})
    for k in chaves:
        await c.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": k})
    linhas = (
        await c.execute(
            text(
                "SELECT fi.id, fi.operacao_id FROM fichas fi WHERE fi.id = ANY(:i) AND fi.decisao IS NULL"
                " AND fi.expira_em <= now() ORDER BY fi.id FOR UPDATE OF fi SKIP LOCKED"
            ),
            {"i": [x.id for x in candidatas]},
        )
    ).all()
    if not linhas:
        return []
    mudaram = {
        str(o)
        for o in (
            await c.execute(
                text(
                    "WITH alvo AS (SELECT f.id FROM fila_saida f WHERE f.operacao_id = ANY(:o)"
                    " AND f.estado = 'aguardando_ficha' ORDER BY f.id FOR UPDATE)"
                    " UPDATE fila_saida f SET estado = 'vencida', mudou_por = 'rotina:vencimento' FROM alvo"
                    " WHERE f.id = alvo.id AND f.estado = 'aguardando_ficha' RETURNING f.operacao_id"
                ),
                {"o": [x.operacao_id for x in linhas]},
            )
        ).scalars()
    }
    await c.execute(
        text("UPDATE fichas SET decisao = 'vencida', decidida_em = now() WHERE id = ANY(:f) AND decisao IS NULL"),
        {"f": [x.id for x in linhas]},
    )
    vencidas: list[str] = []
    for linha in linhas:
        if str(linha.operacao_id) not in mudaram:
            continue
        op = _existe(await por_id(c, linha.operacao_id))
        vencidas.append(op.protocolo)
        await registrar(
            c,
            papel=papel,
            tipo="ficha_vencida",
            campos={"protocolo": op.protocolo},
            protocolo=op.protocolo,
            pessoa_id=op.pessoa_id,
            aplicativo_id=op.aplicativo_id,
            sistema_id=op.sistema_id,
        )
    return vencidas
