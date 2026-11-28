"""A consulta ao proprio sistema e o aviso de bloqueio (desenho 5.2, 5.5 e 6; criticas A1, A2, A3.3, M2, M7, M8, B8
e B9), e a gravacao do que o sistema informa (``informacoes_sistema``), que a porta de saida tambem usa (recibo).

Quem fala com o sistema e o despachante: as rotinas pedem a consulta pela rota ``contrato.ROTA_CONSULTAR``, e o aviso
de bloqueio sai de duas tarefas do proprio despachante. Regras comuns:

- consulta e aviso so vao a sistema ``ativo`` na 2026-07-28, com a porta de saida aberta e, num terceiro, com a
  revisao do servidor do teste ainda vigente (critica A2). Fora disso, nada e perguntado: a consulta grava
  ``sistema_fora`` (sem gastar tentativa, e a escada para) e o aviso fica ``sistema_fora``, com a linha legivel no
  extrato. Nada sai com a porta fechada: a consulta nao pergunta nem grava nada (``PortaFechada``, 409
  ``porta_fechada``; a escada nao anda, decisao 2 do coordenador no retoque da etapa 3), e o aviso de bloqueio espera
  a porta abrir, se o bloqueio ainda valer (``bloqueio_encerrado`` se acabou antes). Depois de abrir a chave (que
  pode demorar: a renovacao chama o servidor de autorizacao), a porta e a situacao do sistema sao conferidas de novo,
  antes de chamar;
- a chave e a da operacao: a da pessoa (renovada antes, se for preciso) ou a do programa. Sem chave (cofre trancado,
  chave apagada ou vencida) nada e perguntado, nada conta contra o sistema e nenhuma tentativa e gasta (critica
  M7): a consulta grava ``sem_chave`` (o degrau sobe mesmo assim, e a linha ``consulta_nao_feita`` do extrato so
  volta com outro motivo), e o aviso espera a chave, como espera a porta abrir, com uma linha so para a dona
  (``aviso_bloqueio_ainda_nao_saiu``). Um 401 com a chave da pessoa marca a chave vencida e tambem e falha do
  MCP-CLARO;
- um POST so por pergunta (``ligacao.chamar_ferramenta`` faz exatamente um), com prazo
  ``BADGE_LOOKUP_TIMEOUT_SECONDS`` e teto de resposta. A resposta passa por ``selo.respostas`` (validacao estrita):
  so as categorias do contrato e horas do MCP-CLARO chegam ao banco, ao extrato e as telas, nunca texto do sistema;
- a transacao que grava ``informacoes_sistema`` nunca fica aberta durante uma chamada ao sistema (a trava consultiva
  do sistema, ``informacoes_sistema_nasce``): chamar primeiro e gravar depois, numa transacao curta, em READ
  COMMITTED, com ``informacoes_sistema`` por ultimo (depois dos outros UPDATE e INSERT) e de um sistema so;
- a contradicao do sistema (``RETURNING contradiz``; o banco ja retirou o selo) vira a linha
  ``sistema_se_contradisse`` no extrato da pessoa e o aviso ``selo_retirado`` ao ADMIN.

Consulta (``Perguntador.consultar``): o despachante nao confia em quem chama (critica M7) e confere tudo pelo banco,
com as funcoes do contrato (``proxima_consulta``, ``escada_vencida``, ``e_a_ultima_consulta``,
``resposta_que_encerra``): sem envio, 404; nada a perguntar (resposta que encerra, escada acabada no maximo ou pelo
tempo, ``sistema_fora`` na ultima linha de consulta, outro protocolo com o mesmo numero, ainda nao e a vez, ou outra
copia do despachante perguntando agora: critica M8), 409 ``nada_a_perguntar``; a porta fechada, 409
``porta_fechada``. Uma ``nao_recebida`` sozinha nao encerra (A3.3), e a resposta ao aviso de bloqueio nunca encerra
(decisao 1 do coordenador no retoque da etapa 3). No fim da escada sem resposta que encerra, o extrato da pessoa ganha
``sistema_nao_respondeu_consulta``; e, se as ultimas ``BADGE_LOOKUP_FAILS_TO_WITHDRAW`` operacoes do sistema que
terminaram a escada depois do teste do selo de agora (e da ultima retirada) terminaram todas assim (sem resposta,
resposta invalida ou ``desconhecido``, critica B8), o selo de agora e retirado (``nao_respondeu_consultas``), num
SAVEPOINT da mesma transacao, com aviso ao ADMIN. As falhas de antes de um reteste aprovado nao contam contra o selo
novo, e as operacoes ainda na escada (ou cuja escada acabou pelo tempo, sem o maximo de tentativas) saem da conta
antes do limite de operacoes lidas (SQL).

Aviso de bloqueio (``Perguntador.derivar_avisos`` e ``Perguntador.avisar_bloqueios``; critica A1): o bloqueio continua
o da Fase 2. Uma tarefa deriva os avisos dos comprovantes novos (que ja nao mudam) e de ``envios_selo``, so de um
bloqueio (uma pausa nao avisa), por sistema e pessoa, em partes de ate 100 numeros, cada numero uma vez, e marca o
bloqueio em ``avisos_bloqueio_derivados`` na mesma transacao. Outra tarefa pega um aviso pendente na hora (``FOR
UPDATE SKIP LOCKED``), abre a chave da pessoa, reserva a vez antes de chamar (``tentativas`` mais um e a proxima vez a
1, 5 e 15 minutos: com varias copias do despachante, uma chamada so por vez, critica M8), chama ``selo_bloqueio`` e
grava uma linha de ``informacoes_sistema`` (origem ``aviso``) por operacao, com ``sistema_informou_no_bloqueio`` no
extrato da dona. Sem resposta depois de ``BADGE_BLOCK_NOTICE_ATTEMPTS`` tentativas, o aviso fica ``sem_resposta``,
com ``aviso_bloqueio_sem_resposta``. Quando a falha e do MCP-CLARO (a chave nao abre, ou o sistema recusa a chave da
pessoa na ultima tentativa), o sistema nao leva a culpa: sem chave o aviso espera, sem gastar tentativa, e a dona
ganha ``aviso_bloqueio_ainda_nao_saiu`` na primeira vez (uma so por bloqueio e sistema); a chave recusada na ultima
vez deixa o aviso ``sistema_fora``, com ``aviso_bloqueio_sistema_fora`` e o motivo nosso. A resposta ao aviso fica
gravada, mas nunca encerra a escada da consulta (``contrato.ORIGENS_DA_RESPOSTA_FINAL``): um ``parada`` seguido da
escrita e pego pela consulta seguinte, como contradicao.

Retirada e contador: ``selo_retiradas`` soma uma vez por teste retirado (o aviso ao ADMIN de cada um vai uma vez so
por transacao, mesmo com varios numeros contraditos na mesma resposta ao aviso).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import Sistema
from mcp_claro.cofre import CofreIndisponivel, CofreTrancado
from mcp_claro.despachante import chaves, renovacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.despachante.cifra_origem import ContextoSemSistema
from mcp_claro.extrato import registrar
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.ligacao.ponto import Ligacao
from mcp_claro.ligacao.sala_contrato import SALA_FORA
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import estado as regras_estado
from mcp_claro.selo import contrato, respostas
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.selo.consulta")

Ligador = Callable[[Sistema], Ligacao]

TETO_DO_AVISO: Final = 4 * contrato.TETO_RESPOSTA_BYTES
"""Teto, em bytes, do corpo HTTP da resposta ao aviso: ela leva a resposta (``structuredContent``, ate
``contrato.TETO_RESPOSTA_BYTES``, conferido em ``selo.respostas``) e a copia em texto que a 2026-07-28 recomenda, para
ate 100 numeros. A consulta, bem menor, vai com o teto do contrato."""

ESPERAS_DO_AVISO_MINUTOS: Final = (1, 5, 15)
"""Depois de cada tentativa do aviso que falha, a proxima vai 1, 5 e 15 minutos depois (desenho 6; o ultimo degrau
vale para o resto, ate ``BADGE_BLOCK_NOTICE_ATTEMPTS``)."""

ESPERA_SEM_CHAVE_MINUTOS: Final = ESPERAS_DO_AVISO_MINUTOS[-1]
"""Sem a chave da pessoa, o aviso tenta abrir a chave de novo depois deste tanto, sem gastar tentativa (critica M7):
quem traz a chave de volta e gente (a pessoa autoriza de novo, o ADMIN destranca o cofre)."""

LIMITE_DA_RODADA: Final = 20
"""Avisos (e bloqueios a derivar) por rodada das tarefas do aviso."""

OPERACOES_NA_CONTA_DA_RETIRADA: Final = 50
"""Quantas operacoes do sistema, das que mais recentemente terminaram a escada, a conta da retirada le (critica B8).
Bem acima de ``BADGE_LOOKUP_FAILS_TO_WITHDRAW`` (ate 20). As que ainda estao na escada (e as que pararam sem o maximo
de tentativas: ``sistema_fora`` ou o fim pelo tempo) saem no SQL, antes do limite: uma rajada de operacoes ainda
sendo consultadas nao segura a retirada."""

TRAVA_DA_CONSULTA: Final = "mcp_claro.selo.consulta"
TRAVA_DA_DERIVACAO: Final = "mcp_claro.selo.derivar"
TRAVA_DO_AVISO_SEM_CHAVE: Final = "mcp_claro.selo.aviso_sem_chave"

MOTIVOS_DA_RETIRADA: Final[Mapping[str, str]] = {
    "se_contradisse": "o sistema deu respostas que nao combinam",
    "nao_respondeu_consultas": "o sistema nao respondeu as consultas",
}
"""O motivo da retirada automatica no extrato e no aviso ao ADMIN (sem acento, como as frases)."""

SEM_CHAVE: Final[Mapping[type[BaseException], str]] = {
    chaves.ChaveAusente: "a pessoa ainda nao autorizou o sistema",
    renovacao.AutorizacaoVencida: "a autorizacao da pessoa no sistema venceu",
    renovacao.RenovacaoIndisponivel: "nao deu para renovar a chave da pessoa",
    CofreTrancado: "o cofre de chaves esta trancado",
    CofreIndisponivel: "o cofre de chaves nao respondeu",
}
"""Por que a chave nao abriu, como vai a linha ``consulta_nao_feita`` (a falha e do MCP-CLARO, nao do sistema)."""
CHAVE_NAO_ABRIU: Final = "nao deu para abrir a chave no cofre"
CHAVE_DE_PROGRAMA: Final = "o aviso de bloqueio so usa a chave de cada pessoa"
"""O aviso e por pessoa (critica B7): num sistema sem chave por pessoa nao ha chave para ele."""
CHAVE_RECUSADA: Final = "o sistema recusou a chave da pessoa"
"""Um 401 com a chave da pessoa: a chave fica marcada vencida, e a falha e do MCP-CLARO (critica M7)."""


class SemEnvio(LookupError):
    """A operacao nao existe ou nao saiu com numero (404 ``sem_envio``)."""


class NadaAPerguntar(Exception):
    """Nada foi perguntado: nao e a vez (409 ``nada_a_perguntar``)."""


class PortaFechada(Exception):
    """Era a vez, mas a porta de saida esta fechada: nada foi perguntado nem gravado, e a escada nao anda (409
    ``porta_fechada``, decisao 2 do coordenador no retoque da etapa 3). As rotinas tentam na proxima rodada."""


@dataclass(frozen=True, slots=True)
class Desfecho:
    """Como terminou a consulta (``contrato.RespostaDaConsulta``): o desfecho e, com ``respondeu``, o estado."""

    desfecho: str
    estado: str | None = None

    def como_json(self) -> dict[str, Any]:
        return {"desfecho": self.desfecho, "estado": self.estado}


# --- Gravar o que o sistema informou (consulta, aviso e recibo) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Informacao:
    """Uma linha de ``informacoes_sistema``: so as categorias do contrato, contagens e horas do MCP-CLARO."""

    operacao_id: uuid.UUID
    sistema_id: uuid.UUID
    origem: str
    desfecho: str
    estado: str | None = None
    aviso_id: uuid.UUID | None = None
    repetida: bool = False
    objetos: int | None = None
    depois: tuple[respostas.Evento, ...] = ()


async def gravar_informacao(c: AsyncConnection, info: Informacao) -> bool:
    """Grava a linha e devolve ``contradiz`` (o banco ja retirou o selo do teste do envio e o de agora). Vai por
    ultimo na transacao: a trava consultiva do sistema fica com ela ate o fim."""
    contradiz = (
        await c.execute(
            text(
                "INSERT INTO informacoes_sistema (operacao_id, sistema_id, origem, aviso_id, desfecho, estado,"
                " repetida, objetos, depois) VALUES (:o, :s, :origem, :a, :d, :e, :r, :ob, CAST(:dp AS jsonb))"
                " RETURNING contradiz"
            ),
            {
                "o": info.operacao_id,
                "s": info.sistema_id,
                "origem": info.origem,
                "a": info.aviso_id,
                "d": info.desfecho,
                "e": info.estado,
                "r": info.repetida,
                "ob": info.objetos,
                "dp": json.dumps([e.guardado() for e in info.depois]),
            },
        )
    ).scalar_one()
    CONTADORES.somar("selo_informacoes", origem=info.origem, desfecho=info.desfecho)
    return bool(contradiz)


async def avisar_retirada(
    c: AsyncConnection,
    *,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    motivo: str,
    chave_unica: str,
    emails_admin: Sequence[str],
) -> None:
    """O aviso ``selo_retirado`` ao ADMIN (uma vez por ``chave_unica``), sem protocolo nem pessoa, e o contador
    ``selo_retiradas``. Quem chama passa uma vez por teste retirado na transacao (``contradicoes`` junta os numeros
    contraditos antes): o contador conta retiradas, nunca numeros nem avisos repetidos."""
    legivel = MOTIVOS_DA_RETIRADA[motivo]
    assunto, texto_aviso = aviso_admin("selo_retirado", sistema=sistema_nome, motivo=legivel)
    await avisar_admin(
        c,
        "selo_retirado",
        assunto,
        {"texto": texto_aviso, "sistema": sistema_nome, "sistema_id": str(sistema_id), "motivo": motivo},
        chave_unica,
        emails_admin,
    )
    CONTADORES.somar("selo_retiradas", motivo=motivo)


@dataclass(frozen=True, slots=True)
class Contradito:
    """Um numero sobre o qual o sistema se contradisse, com o que a linha da dona leva."""

    protocolo: str
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID | None
    teste_id: uuid.UUID


async def contradicoes(
    c: AsyncConnection,
    *,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    contraditos: Sequence[Contradito],
    emails_admin: Sequence[str],
) -> None:
    """O sistema se contradisse sobre estes numeros, na mesma transacao (o banco ja retirou o selo): uma linha
    ``sistema_se_contradisse`` por protocolo, e depois, uma vez so, o aviso ao ADMIN e o contador, um por teste
    retirado nesta transacao (D10 da conferencia da etapa 3: uma resposta ao aviso com varios numeros contraditos nao
    soma a mesma retirada varias vezes). O gatilho retira o teste do envio e, se for outro, o ultimo teste aprovado do
    sistema: depois de um reteste, contradizer-se sobre um envio do teste antigo derruba o selo de agora, e o ADMIN fica
    sabendo deste, mesmo que o antigo ja tivesse sido retirado e avisado."""
    for x in contraditos:
        await registrar(
            c,
            papel="despachante",
            tipo="sistema_se_contradisse",
            campos={"sistema": sistema_nome, "protocolo": x.protocolo},
            protocolo=x.protocolo,
            pessoa_id=x.pessoa_id,
            aplicativo_id=x.aplicativo_id,
            sistema_id=sistema_id,
        )
    retirados = await retirados_agora(c, sistema_id, "se_contradisse")
    for retirado in retirados:
        await avisar_retirada(
            c,
            sistema_id=sistema_id,
            sistema_nome=sistema_nome,
            motivo="se_contradisse",
            chave_unica=f"selo_retirado:se_contradisse:{sistema_id}:{retirado}",
            emails_admin=emails_admin,
        )
    log.warning(
        "sistema_se_contradisse",
        sistema=sistema_nome,
        numeros=len(contraditos),
        retirados=len(retirados),
        do_envio=any(x.teste_id in retirados for x in contraditos),
    )


async def contradicao(
    c: AsyncConnection,
    *,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    protocolo: str,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID | None,
    teste_id: uuid.UUID,
    emails_admin: Sequence[str],
) -> None:
    """O sistema se contradisse sobre o numero do ``protocolo`` (o banco ja retirou o selo): ``contradicoes`` com um
    numero so (o recibo e a consulta gravam uma linha por transacao)."""
    await contradicoes(
        c,
        sistema_id=sistema_id,
        sistema_nome=sistema_nome,
        contraditos=(Contradito(protocolo, pessoa_id, aplicativo_id, teste_id),),
        emails_admin=emails_admin,
    )


async def retirados_agora(c: AsyncConnection, sistema_id: uuid.UUID, motivo: str) -> list[uuid.UUID]:
    """Os testes do sistema retirados por ``motivo`` nesta transacao (``retirado_em`` e o ``now()`` dela, pelo
    gatilho), em ordem. Vazio quando o gatilho nao retirou nada novo (os dois testes ja estavam retirados assim)."""
    return [
        uuid.UUID(str(t))
        for t in (
            await c.execute(
                text(
                    "SELECT teste_id FROM selos_retirados WHERE sistema_id = :s AND motivo = :m"
                    " AND retirado_em = now() ORDER BY teste_id"
                ),
                {"s": sistema_id, "m": motivo},
            )
        ).scalars()
    ]


def motivo_sem_chave(erro: BaseException) -> str:
    for tipo, motivo in SEM_CHAVE.items():
        if isinstance(erro, tipo):
            return motivo
    return CHAVE_NAO_ABRIU


def fora_de_ativo(linha: Any) -> str | None:
    """Por que nada e perguntado ao sistema agora (critica A2), ou None. ``linha``: ``situacao``, ``versao_mcp``,
    ``origem``, ``revisao_servidor_id`` (de agora) e ``revisao_do_teste`` (a do teste do envio). A porta fechada nao
    entra aqui: com ela, a consulta nao grava nada (``PortaFechada``) e o aviso espera."""
    if linha.situacao == "quarentena":
        return "o sistema esta em quarentena"
    if linha.situacao != "ativo" or linha.versao_mcp != VERSAO_MCP:
        return "o sistema nao esta ativo"
    if linha.origem != "proprio" and (
        linha.revisao_servidor_id is None or str(linha.revisao_servidor_id) != str(linha.revisao_do_teste)
    ):
        return "a revisao do servidor de terceiros mudou depois do teste"
    return None


# --- A pergunta ao sistema -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Lido:
    """O que a resposta do sistema deu, ja validado."""

    desfecho: str
    estado: str | None = None
    depois: tuple[respostas.Evento, ...] = ()
    invalida: str | None = None
    motivo: str | None = None  # sem_chave e sala_fora: por que o MCP-CLARO nao conseguiu perguntar


def _sem_resposta(resposta: Resposta, sistema: Sistema) -> _Lido:
    """A falha de uma pergunta do contrato, separada: a sala fora e do MCP-CLARO; resposta grande demais e invalida;
    erro JSON-RPC (o sistema respondeu, fora do contrato) e invalido; o resto e sem resposta."""
    if sistema.de_terceiro and resposta.tipo == "nao_enviado" and resposta.excecao == SALA_FORA:
        return _Lido("sala_fora", motivo="a sala de visitas nao esta disponivel")
    if resposta.excecao == "RespostaGrandeDemais":
        return _Lido("resposta_invalida", invalida="grande_demais")
    if resposta.tipo == "erro_rpc":
        return _Lido("resposta_invalida", invalida="erro_rpc")
    return _Lido("sem_resposta")


@dataclass
class Perguntador:
    """A consulta e o aviso de bloqueio, com o que o app do despachante monta (``despachante.app``)."""

    engine: AsyncEngine
    cifra: Cifra
    ligacao: Ligador
    maximo: int = 8
    """``BADGE_LOOKUP_MAX_ATTEMPTS``."""
    prazo: float = 10.0
    """``BADGE_LOOKUP_TIMEOUT_SECONDS``: o prazo de cada pergunta (consulta e aviso)."""
    prazo_envio: float = 20.0
    """``RESPONSE_MAX_WAIT_SECONDS``: o prazo da chamada na porta de saida (o piso da primeira consulta, A3.3)."""
    falhas_para_retirar: int = 3
    """``BADGE_LOOKUP_FAILS_TO_WITHDRAW``."""
    tentativas_do_aviso: int = 3
    """``BADGE_BLOCK_NOTICE_ATTEMPTS``."""
    emails_admin: Sequence[str] = field(default_factory=tuple)
    ambiente: str = "producao"
    liberar_locais: bool = False

    # --- Chave ---------------------------------------------------------------------------------------------------

    async def _segredo(
        self, sistema: Sistema, *, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID | None
    ) -> tuple[str | None, Any, str | None]:
        """(segredo, chave aberta por pessoa, motivo de nao abrir). Como a porta de saida: a chave da pessoa,
        renovada antes, ou a do programa (``aplicativo_id``)."""
        try:
            if sistema.tipo_chave == "por_pessoa":
                aberta = await renovacao.chave_pronta(
                    self.engine,
                    self.cifra,
                    sistema_id=sistema.id,
                    pessoa_id=pessoa_id,
                    ambiente=self.ambiente,
                    liberar_locais=self.liberar_locais,
                )
                return aberta.segredo, aberta, None
            if aplicativo_id is None:
                return None, None, CHAVE_DE_PROGRAMA
            async with self.engine.connect() as c:
                segredo = await chaves.abrir(c, self.cifra, sistema_id=sistema.id, aplicativo_id=aplicativo_id)
        except ContextoSemSistema as erro:
            log.warning("selo_chave_indisponivel", erro=type(erro).__name__)
            return None, None, CHAVE_NAO_ABRIU
        except Exception as erro:  # noqa: BLE001 - toda falha da chave e do MCP-CLARO: nada e perguntado
            log.warning("selo_chave_indisponivel", erro=type(erro).__name__)
            return None, None, motivo_sem_chave(erro)
        return segredo, None, None

    async def _perguntar(
        self, sistema: Sistema, segredo: str | None, nome: str, argumentos: dict[str, Any], *, teto: int
    ) -> Resposta:
        async def obter() -> str | None:
            return segredo

        async with self.engine.connect() as c:
            sondagem = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema.id})
            ).scalar_one_or_none()
        discover = json.dumps(sondagem) if isinstance(sondagem, dict) else sondagem
        try:
            return await self.ligacao(sistema).chamar_ferramenta(
                sistema.endereco,
                obter,
                discover,
                nome,
                argumentos,
                traceparent=None,
                prazo=self.prazo,
                teto_resposta=teto,
            )
        except Exception as erro:  # noqa: BLE001 - uma pergunta nunca vira erro interno; nada e reenviado
            log.warning("selo_pergunta_falhou", sistema=sistema.nome, ferramenta=nome, erro=type(erro).__name__)
            return Resposta("duvida", excecao=type(erro).__name__)

    async def _marcar_vencida(self, sistema: Sistema, pessoa_id: uuid.UUID, aberta: Any) -> None:
        try:
            await renovacao.marcar_vencida(
                self.engine, sistema_id=sistema.id, pessoa_id=pessoa_id, cifrada=aberta.cifrada
            )
        except (SQLAlchemyError, OSError, TimeoutError) as erro:
            log.warning("selo_chave_vencida_nao_marcada", erro=type(erro).__name__)

    # --- Consulta ------------------------------------------------------------------------------------------------

    async def consultar(self, operacao_id: uuid.UUID) -> Desfecho:
        """A consulta da rota ``contrato.ROTA_CONSULTAR``. Levanta ``SemEnvio`` (404), ``NadaAPerguntar`` (409
        ``nada_a_perguntar``) e ``PortaFechada`` (409 ``porta_fechada``: nada perguntado nem gravado); uma falha do
        banco sobe como veio (503 na rota). Uma copia do despachante por vez pergunta sobre a operacao (trava
        consultiva de sessao, numa conexao sem transacao aberta durante a chamada, critica M8)."""
        async with self.engine.connect() as c:
            travou = (
                await c.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:t), hashtext(:o))"),
                    {"t": TRAVA_DA_CONSULTA, "o": str(operacao_id)},
                )
            ).scalar_one()
            await c.commit()
            if not travou:
                raise NadaAPerguntar("outra copia do despachante esta perguntando sobre esta operacao")
            try:
                return await self._consultar(c, operacao_id)
            finally:
                await _soltar(c, TRAVA_DA_CONSULTA, str(operacao_id))

    async def _consultar(self, c: AsyncConnection, operacao_id: uuid.UUID) -> Desfecho:
        vez = await self._a_vez(c, operacao_id)
        await c.commit()
        sistema = vez.sistema
        if vez.fora is not None:
            async with c.begin():
                await self._gravar_consulta(c, vez, _Lido("sistema_fora", motivo=vez.fora))
            return Desfecho("sistema_fora")
        segredo, aberta, sem_chave = await self._segredo(
            sistema, pessoa_id=vez.pessoa_id, aplicativo_id=vez.aplicativo_id
        )
        # A chave pode demorar a abrir (a renovacao chama o servidor de autorizacao): a porta e a situacao do sistema
        # sao conferidas de novo antes de chamar, como no aviso (critica A2, decisao 2 do coordenador).
        fora = await self._fora_agora(c, vez)
        if fora is not None:
            async with c.begin():
                await self._gravar_consulta(c, vez, _Lido("sistema_fora", motivo=fora))
            return Desfecho("sistema_fora")
        if sem_chave is not None:
            lido = _Lido("sem_chave", motivo=sem_chave)
        else:
            resposta = await self._perguntar(
                sistema,
                segredo,
                contrato.FERRAMENTA_CONSULTAR,
                {"operacao": vez.numero},
                teto=contrato.TETO_RESPOSTA_BYTES,
            )
            if resposta.tipo == "recusado_http" and resposta.status_http[-1:] == (401,) and aberta is not None:
                # A chave da pessoa nao vale mais: a falha e do MCP-CLARO (a proxima pergunta renova antes).
                await self._marcar_vencida(sistema, vez.pessoa_id, aberta)
                lido = _Lido("sem_chave", motivo=CHAVE_RECUSADA)
            else:
                lido = self._ler_consulta(resposta, vez)
        async with c.begin():
            await self._gravar_consulta(c, vez, lido)
        return Desfecho(lido.desfecho, lido.estado)

    @staticmethod
    def _ler_consulta(resposta: Resposta, vez: _Vez) -> _Lido:
        if resposta.tipo == "resultado" and resposta.resultado is not None:
            lida = respostas.ler_consulta(
                resposta.resultado, numero=vez.numero, nivel=vez.nivel, desde=vez.enviado_em, ate=vez.agora
            )
            if isinstance(lida, respostas.Invalida):
                return _Lido("resposta_invalida", invalida=lida.motivo)
            return _Lido("respondeu", lida.estado, lida.depois)
        return _sem_resposta(resposta, vez.sistema)

    async def _a_vez(self, c: AsyncConnection, operacao_id: uuid.UUID) -> _Vez:
        """Le do banco, numa transacao so (com o ``now()`` dele), se ja e a vez de perguntar."""
        linha = (
            await c.execute(
                text(
                    "SELECT e.chave, e.nivel, e.teste_id, e.gravado_em, e.sistema_id, o.protocolo, o.pessoa_id,"
                    " o.aplicativo_id, f.estado AS fila, s.situacao, s.versao_mcp, s.origem, s.revisao_servidor_id,"
                    " t.revisao_servidor_id AS revisao_do_teste, selo_substituto(e.operacao_id) AS substituto,"
                    " now() AS agora"
                    " FROM envios_selo e JOIN operacoes o ON o.id = e.operacao_id"
                    " JOIN fila_saida f ON f.operacao_id = o.id JOIN sistemas s ON s.id = e.sistema_id"
                    " JOIN testes_selo t ON t.id = e.teste_id WHERE e.operacao_id = :o"
                ),
                {"o": operacao_id},
            )
        ).first()
        if linha is None:
            raise SemEnvio("a operacao nao saiu com numero")
        linhas = await _linhas(c, operacao_id)
        if linha.substituto is not None:
            raise NadaAPerguntar("outro protocolo saiu com o mesmo numero depois")
        if not _vale_perguntar(str(linha.fila), linhas):
            raise NadaAPerguntar("a operacao nao esta em duvida")
        proxima = contrato.proxima_consulta(
            linhas, enviado_em=linha.gravado_em, maximo=self.maximo, prazo_envio_segundos=self.prazo_envio
        )
        if proxima is None or proxima > linha.agora or contrato.escada_vencida(linha.gravado_em, linha.agora):
            raise NadaAPerguntar("ainda nao e a vez, ou nao ha mais o que perguntar")
        sistema = await repositorio.sistema_por_id(c, uuid.UUID(str(linha.sistema_id)))
        if sistema is None:  # pragma: no cover - a chave estrangeira do envio garante
            raise SemEnvio("sistema do envio sumiu")
        if not (await regras_estado.ler_saida(c)).aberta:
            raise PortaFechada("a porta de saida esta fechada")
        return _Vez(
            operacao_id=operacao_id,
            numero=str(linha.chave),
            nivel=int(linha.nivel),
            teste_id=uuid.UUID(str(linha.teste_id)),
            enviado_em=linha.gravado_em,
            agora=linha.agora,
            protocolo=str(linha.protocolo),
            pessoa_id=uuid.UUID(str(linha.pessoa_id)),
            aplicativo_id=uuid.UUID(str(linha.aplicativo_id)),
            sistema=sistema,
            linhas=tuple(linhas),
            ultima=contrato.e_a_ultima_consulta(linhas, self.maximo),
            fora=fora_de_ativo(linha),
        )

    async def _fora_agora(self, c: AsyncConnection, vez: _Vez) -> str | None:
        """Depois de abrir a chave, de novo: com a porta fechada levanta ``PortaFechada`` (nada perguntado nem
        gravado); fora de ``ativo`` (ou com a revisao do terceiro trocada) da o motivo de ``sistema_fora``. Numa
        transacao curta, fechada antes da chamada."""
        async with c.begin():
            if not (await regras_estado.ler_saida(c)).aberta:
                raise PortaFechada("a porta de saida fechou enquanto a chave abria")
            linha = (
                await c.execute(
                    text(
                        "SELECT s.situacao, s.versao_mcp, s.origem, s.revisao_servidor_id,"
                        " t.revisao_servidor_id AS revisao_do_teste FROM sistemas s, testes_selo t"
                        " WHERE s.id = :s AND t.id = :t"
                    ),
                    {"s": vez.sistema.id, "t": vez.teste_id},
                )
            ).one()
        return fora_de_ativo(linha)

    async def _gravar_consulta(self, c: AsyncConnection, vez: _Vez, lido: _Lido) -> None:
        """A linha do extrato da pessoa e a de ``informacoes_sistema`` (por ultimo), na mesma transacao curta, e o
        que vem depois dela: a contradicao e, no fim da escada, a conta da retirada (num SAVEPOINT)."""
        sistema = vez.sistema
        comum: dict[str, Any] = {
            "protocolo": vez.protocolo,
            "pessoa_id": vez.pessoa_id,
            "aplicativo_id": vez.aplicativo_id,
            "sistema_id": sistema.id,
        }
        if lido.desfecho == "respondeu":
            await registrar(
                c,
                papel="despachante",
                tipo="sistema_informou",
                campos={"sistema": sistema.nome, "protocolo": vez.protocolo, "estado": lido.estado},
                dados={"origem": "consulta", "estado": lido.estado},
                **comum,
            )
        elif lido.desfecho == "sistema_fora":
            await registrar(
                c,
                papel="despachante",
                tipo="sistema_fora_consulta",
                campos={"sistema": sistema.nome, "protocolo": vez.protocolo, "motivo": lido.motivo},
                dados={"origem": "consulta", "desfecho": lido.desfecho},
                **comum,
            )
        elif lido.desfecho in ("sem_chave", "sala_fora") and await _motivo_novo(c, vez.protocolo, lido.motivo):
            # Decisao 3 do coordenador: a linha so volta quando o motivo muda (sem a chave da pessoa, a escada segue
            # perguntando pelos degraus, e o extrato nao ganha a mesma linha a cada vez).
            await registrar(
                c,
                papel="despachante",
                tipo="consulta_nao_feita",
                campos={"sistema": sistema.nome, "protocolo": vez.protocolo, "motivo": lido.motivo},
                dados={"origem": "consulta", "desfecho": lido.desfecho, "motivo": lido.motivo},
                **comum,
            )
        falha_do_sistema = lido.desfecho in contrato.DESFECHOS_DE_FALHA_DO_SISTEMA or (
            lido.desfecho == "respondeu" and lido.estado == "desconhecido"
        )
        if vez.ultima and falha_do_sistema:
            await registrar(
                c,
                papel="despachante",
                tipo="sistema_nao_respondeu_consulta",
                campos={"sistema": sistema.nome, "protocolo": vez.protocolo},
                dados={"origem": "consulta", "desfecho": lido.desfecho},
                **comum,
            )
        if lido.invalida is not None:
            CONTADORES.somar("selo_respostas_invalidas", origem="consulta", motivo=lido.invalida)
        contradiz = await gravar_informacao(
            c,
            Informacao(vez.operacao_id, sistema.id, "consulta", lido.desfecho, estado=lido.estado, depois=lido.depois),
        )
        if contradiz:
            await contradicao(
                c,
                sistema_id=sistema.id,
                sistema_nome=sistema.nome,
                protocolo=vez.protocolo,
                pessoa_id=vez.pessoa_id,
                aplicativo_id=vez.aplicativo_id,
                teste_id=vez.teste_id,
                emails_admin=self.emails_admin,
            )
        if vez.ultima and falha_do_sistema:
            if lido.desfecho == "respondeu":
                CONTADORES.somar("selo_informacoes", origem="consulta", desfecho="desconhecido_no_fim")
            try:
                async with c.begin_nested():
                    await self._talvez_retirar(c, sistema)
            except SQLAlchemyError as erro:
                # A linha da consulta fica: so a retirada (e o aviso dela) nao foi gravada.
                log.warning("selo_retirada_nao_gravada", sistema=sistema.nome, erro=type(erro).__name__)

    async def _talvez_retirar(self, c: AsyncConnection, sistema: Sistema) -> bool:
        """Critica B8 e D18: as ultimas ``falhas_para_retirar`` operacoes do sistema que terminaram a escada
        terminaram todas sem resposta (ou com resposta invalida, ou ``desconhecido``): o selo de agora e retirado
        (``nao_respondeu_consultas``). As que terminaram com resposta que encerra interrompem a sequencia. As que
        ainda estao na escada, as que pararam por ``sistema_fora`` antes do maximo e as que acabaram pelo tempo sem o
        maximo de tentativas que gastam (``contrato.escada_vencida``: o sistema nao foi perguntado o bastante) nao
        contam: saem no SQL (``_SQL_DA_RETIRADA``), antes do limite de ``OPERACOES_NA_CONTA_DA_RETIRADA`` (D4 da
        conferencia da etapa 3). ``terminou_sem_resposta`` decide o resto, e pula (``None``) a que chegou ao maximo e
        parou depois em ``sistema_fora`` (o maximo, ``BADGE_LOOKUP_MAX_ATTEMPTS``, pode ter baixado).

        So contam as operacoes cuja escada terminou (a ultima linha de consulta) depois do teste do selo de agora:
        as falhas de antes de um reteste aprovado ja derrubaram (ou nao) o selo antigo e nunca derrubam o novo. A
        operacao de agora mesmo conta, ainda que tenha saido no teste antigo (o banco a aceita como prova da retirada
        do selo de agora). Toda retirada (do ADMIN, por contradicao ou por esta conta) leva o selo de agora junto, e
        um selo vigente depois dela e de um teste mais novo: o corte pelo teste ja fica depois da ultima retirada."""
        vigente_agora = (
            await c.execute(text("SELECT testado_em FROM selo_vigente(:s) WHERE nivel >= 2"), {"s": sistema.id})
        ).first()
        if vigente_agora is None:
            return False  # nada a retirar
        linhas = (
            await c.execute(
                text(_SQL_DA_RETIRADA),
                {
                    "s": sistema.id,
                    "t": vigente_agora.testado_em,
                    "d": contrato.GUARDA_MINIMA_DIAS,
                    "k": OPERACOES_NA_CONTA_DA_RETIRADA,
                    "maximo": self.maximo,
                    "gastam": list(contrato.DESFECHOS_QUE_GASTAM_TENTATIVA),
                },
            )
        ).all()
        por_operacao: dict[str, list[contrato.LinhaInformada]] = {}
        for linha in linhas:
            por_operacao.setdefault(str(linha.operacao_id), []).append(
                contrato.LinhaInformada(str(linha.origem), str(linha.desfecho), linha.estado, linha.gravado_em)
            )
        seguidas = 0
        for lista in por_operacao.values():  # da que terminou mais perto de agora para tras
            fim = terminou_sem_resposta(lista, self.maximo)
            if fim is None:
                continue
            if not fim:
                break
            seguidas += 1
            if seguidas >= self.falhas_para_retirar:
                break
        if seguidas < self.falhas_para_retirar:
            return False
        retirado = (
            await c.execute(
                text(
                    "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, retirado_por)"
                    " SELECT CAST(:s AS uuid), v.teste_id, 'nao_respondeu_consultas', 'mcp-claro'"
                    " FROM selo_vigente(:s) v WHERE v.nivel >= 2"
                    " AND NOT EXISTS (SELECT 1 FROM selos_retirados r WHERE r.teste_id = v.teste_id)"
                    " RETURNING teste_id"
                ),
                {"s": sistema.id},
            )
        ).scalar_one_or_none()
        if retirado is None:
            return False
        await registrar(
            c,
            papel="despachante",
            tipo="selo_retirado",
            campos={"sistema": sistema.nome, "motivo": MOTIVOS_DA_RETIRADA["nao_respondeu_consultas"]},
            sistema_id=sistema.id,
            dados={"motivo": "nao_respondeu_consultas", "seguidas": seguidas},
        )
        await avisar_retirada(
            c,
            sistema_id=sistema.id,
            sistema_nome=sistema.nome,
            motivo="nao_respondeu_consultas",
            chave_unica=f"selo_retirado:nao_respondeu_consultas:{sistema.id}:{retirado}",
            emails_admin=self.emails_admin,
        )
        log.warning("selo_retirado_sem_resposta", sistema=sistema.nome, seguidas=seguidas)
        return True

    # --- Aviso de bloqueio: derivar ----------------------------------------------------------------------------

    async def derivar_avisos(self, limite: int = LIMITE_DA_RODADA) -> int:
        """Deriva os avisos dos comprovantes que ainda nao foram derivados (ate ``limite``). Devolve quantos
        bloqueios foram marcados em ``avisos_bloqueio_derivados``."""
        async with self.engine.connect() as c:
            novos = (
                await c.execute(
                    text(
                        "SELECT c.bloqueio_id FROM comprovantes c WHERE NOT EXISTS (SELECT 1 FROM"
                        " avisos_bloqueio_derivados d WHERE d.bloqueio_id = c.bloqueio_id)"
                        " ORDER BY c.criado_em, c.bloqueio_id LIMIT :n"
                    ),
                    {"n": limite},
                )
            ).scalars()
            bloqueios = [uuid.UUID(str(b)) for b in novos]
        feitos = 0
        for bloqueio_id in bloqueios:
            try:
                if await self._derivar(bloqueio_id):
                    feitos += 1
            except IntegrityError:
                continue  # outra copia do despachante derivou o mesmo bloqueio
            except SQLAlchemyError as erro:
                log.warning("selo_aviso_nao_derivado", erro=type(erro).__name__)
        return feitos

    async def _derivar(self, bloqueio_id: uuid.UUID) -> bool:
        async with self.engine.begin() as c:
            travou = (
                await c.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(:t), hashtext(:b))"),
                    {"t": TRAVA_DA_DERIVACAO, "b": str(bloqueio_id)},
                )
            ).scalar_one()
            if not travou:
                return False
            ja = (
                await c.execute(
                    text("SELECT 1 FROM avisos_bloqueio_derivados WHERE bloqueio_id = :b"), {"b": bloqueio_id}
                )
            ).first()
            if ja is not None:
                return False
            tipo = (await c.execute(text("SELECT tipo FROM bloqueios WHERE id = :b"), {"b": bloqueio_id})).scalar_one()
            grupos: dict[tuple[str, str], list[str]] = {}
            if tipo == "bloqueio":  # uma pausa nao pede ao sistema que pare nem queima numeros
                for linha in await c.execute(
                    text(_SQL_DO_AVISO), {"b": bloqueio_id, "abertos": list(contrato.ESTADOS_EM_ABERTO)}
                ):
                    grupos.setdefault((str(linha.sistema_id), str(linha.pessoa_id)), []).append(str(linha.operacao_id))
            avisos = 0
            for (sistema_id, pessoa_id), operacoes in sorted(grupos.items()):
                ordenadas = sorted(operacoes)
                parte = 0
                for inicio in range(0, len(ordenadas), contrato.MAX_OPERACOES_POR_AVISO):
                    try:
                        # Num SAVEPOINT: um aviso que o banco recuse nao prende o bloqueio (e os seguintes) na fila
                        # da derivacao para sempre; os outros grupos saem, e o contador diz o que ficou de fora.
                        async with c.begin_nested():
                            await c.execute(
                                text(
                                    "INSERT INTO avisos_bloqueio (bloqueio_id, sistema_id, pessoa_id, parte, operacoes)"
                                    " VALUES (:b, :s, :p, :n, CAST(:o AS uuid[]))"
                                ),
                                {
                                    "b": bloqueio_id,
                                    "s": sistema_id,
                                    "p": pessoa_id,
                                    "n": parte + 1,
                                    "o": ordenadas[inicio : inicio + contrato.MAX_OPERACOES_POR_AVISO],
                                },
                            )
                    except SQLAlchemyError as erro:
                        log.warning("selo_aviso_recusado", erro=type(erro).__name__)
                        CONTADORES.somar("selo_avisos_bloqueio", desfecho="nao_derivado")
                        continue
                    parte += 1
                    avisos += 1
            await c.execute(
                text("INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) VALUES (:b, :n)"),
                {"b": bloqueio_id, "n": avisos},
            )
        if avisos:
            CONTADORES.somar("selo_avisos_bloqueio", desfecho="derivado", valor=avisos)
        return True

    # --- Aviso de bloqueio: avisar -----------------------------------------------------------------------------

    async def avisar_bloqueios(self, limite: int = LIMITE_DA_RODADA) -> int:
        """Manda ate ``limite`` avisos pendentes na hora, um de cada vez. Com a porta fechada, nada sai (os avisos
        esperam, sem gastar tentativa). Devolve quantos avisos foram vistos (mandados, encerrados ou adiados por
        falta da chave)."""
        vistos = 0
        for _ in range(limite):
            vez = await self._proximo_aviso()
            if vez is None:
                break
            vistos += 1
            if isinstance(vez, _VezDoAviso):
                await self._mandar_aviso(vez)
        return vistos

    async def _proximo_aviso(self) -> _VezDoAviso | bool | None:
        """O proximo aviso na hora, com a chave aberta e a vez reservada (``_VezDoAviso``); ``True`` quando o aviso
        foi visto sem chamar o sistema (encerrado, ou adiado porque a chave nao abriu); ``None`` sem aviso na hora ou
        com a porta fechada.

        A chave abre antes de a vez ser reservada (critica M7): sem chave (cofre trancado, chave apagada ou vencida),
        a falha e do MCP-CLARO, e o aviso espera, como com a porta fechada, sem gastar tentativa. Enquanto esta copia
        abre a chave (fora de transacao: a renovacao pode chamar o servidor de autorizacao), a proxima vez do aviso
        fica um degrau para a frente, e nenhuma outra copia o pega (critica M8); a reserva confere que ninguem mudou
        o aviso nesse meio tempo."""
        pego = await self._pegar_aviso()
        if not isinstance(pego, _AvisoPego):
            return pego
        segredo, aberta, sem_chave = await self._segredo(pego.sistema, pessoa_id=pego.pessoa_id, aplicativo_id=None)
        return await self._reservar_aviso(pego, segredo, aberta, sem_chave)

    async def _pegar_aviso(self) -> _AvisoPego | bool | None:
        async with self.engine.begin() as c:
            if not (await regras_estado.ler_saida(c)).aberta:
                return None
            a = (await c.execute(text(_SQL_DO_AVISO_NA_HORA))).first()
            if a is None:
                return None
            sistema = await repositorio.sistema_por_id(c, uuid.UUID(str(a.sistema_id)))
            if sistema is None:  # pragma: no cover - a chave estrangeira do aviso garante
                return None
            if await self._encerrar_se_acabou(c, a, sistema):
                return True
            await c.execute(
                text("UPDATE avisos_bloqueio SET proxima_em = now() + make_interval(mins => :m) WHERE id = :a"),
                {"a": a.id, "m": _espera_do_aviso(int(a.tentativas) + 1)},
            )
        return _AvisoPego(uuid.UUID(str(a.id)), sistema, uuid.UUID(str(a.pessoa_id)), int(a.tentativas))

    async def _reservar_aviso(
        self, pego: _AvisoPego, segredo: str | None, aberta: Any, sem_chave: str | None
    ) -> _VezDoAviso | bool:
        """A vez reservada antes de chamar (``tentativas`` mais um e a proxima vez a 1, 5 e 15 minutos), conferindo de
        novo a porta e o aviso; sem chave, so a proxima vez anda (``ESPERA_SEM_CHAVE_MINUTOS``)."""
        async with self.engine.begin() as c:
            if not (await regras_estado.ler_saida(c)).aberta:
                return True  # a porta fechou no meio: o aviso espera, sem gastar tentativa
            a = (await c.execute(text(_SQL_DO_AVISO_PEGO), {"a": pego.aviso_id, "t": pego.tentativas})).first()
            if a is None:
                return True  # outra copia (depois do degrau) ou o fim do aviso chegou antes
            if await self._encerrar_se_acabou(c, a, pego.sistema):
                return True
            if sem_chave is not None:
                await c.execute(
                    text("UPDATE avisos_bloqueio SET proxima_em = now() + make_interval(mins => :m) WHERE id = :a"),
                    {"a": pego.aviso_id, "m": ESPERA_SEM_CHAVE_MINUTOS},
                )
                await _avisar_que_ainda_nao_saiu(c, a, pego.sistema, sem_chave)
                CONTADORES.somar("selo_avisos_bloqueio", desfecho="sem_chave")
                log.warning("selo_aviso_sem_chave", sistema=pego.sistema.nome)
                return True
            tentativa = pego.tentativas + 1
            await c.execute(
                text(
                    "UPDATE avisos_bloqueio SET tentativas = tentativas + 1,"
                    " proxima_em = now() + make_interval(mins => :m) WHERE id = :a"
                ),
                {"a": pego.aviso_id, "m": _espera_do_aviso(tentativa)},
            )
            numeros = (
                await c.execute(
                    text(
                        "SELECT e.operacao_id, e.chave, o.protocolo, o.aplicativo_id, e.teste_id FROM envios_selo e"
                        " JOIN operacoes o ON o.id = e.operacao_id WHERE e.operacao_id = ANY (CAST(:o AS uuid[]))"
                        " ORDER BY array_position(CAST(:o AS uuid[]), e.operacao_id)"
                    ),
                    {"o": list(a.operacoes)},
                )
            ).all()
        return _VezDoAviso(
            aviso_id=pego.aviso_id,
            sistema=pego.sistema,
            pessoa_id=pego.pessoa_id,
            tentativa=tentativa,
            operacoes=tuple(
                _NumeroDoAviso(
                    uuid.UUID(str(n.operacao_id)),
                    str(n.chave),
                    str(n.protocolo),
                    uuid.UUID(str(n.aplicativo_id)),
                    uuid.UUID(str(n.teste_id)),
                )
                for n in numeros
            ),
            segredo=segredo,
            aberta=aberta,
        )

    async def _encerrar_se_acabou(self, c: AsyncConnection, a: Any, sistema: Sistema) -> bool:
        """Encerra, sem chamar o sistema, o aviso que nao sai mais: bloqueio encerrado (``bloqueio_encerrado``),
        sistema fora de ``ativo`` ou sem chave por pessoa (``sistema_fora``, critica A2 e B7), ou todas as tentativas
        ja gastas (a ultima nao gravou o fim: ``sem_resposta``). Devolve ``True`` quando encerrou."""
        comum: dict[str, Any] = {"pessoa_id": uuid.UUID(str(a.pessoa_id)), "sistema_id": sistema.id}
        if a.encerrado_em is not None:
            await _encerrar_aviso(c, a.id, "bloqueio_encerrado")
            await registrar(
                c, papel="despachante", tipo="aviso_bloqueio_dispensado", campos={"sistema": sistema.nome}, **comum
            )
            CONTADORES.somar("selo_avisos_bloqueio", desfecho="bloqueio_encerrado")
            return True
        fora = fora_de_ativo(a)
        if fora is None and sistema.tipo_chave != "por_pessoa":
            # Defesa: o banco so grava teste de selo de sistema com chave por pessoa, e trocar o tipo da chave tira o
            # sistema de "ativo" (0002); este caminho fica para o caso de a regra mudar.
            fora = CHAVE_DE_PROGRAMA
        if fora is not None:
            await _encerrar_aviso(c, a.id, "sistema_fora")
            await registrar(
                c,
                papel="despachante",
                tipo="aviso_bloqueio_sistema_fora",
                campos={"sistema": sistema.nome, "motivo": fora},
                **comum,
            )
            CONTADORES.somar("selo_avisos_bloqueio", desfecho="sistema_fora")
            return True
        if int(a.tentativas) >= self.tentativas_do_aviso:
            # Ja chamou todas as vezes e a ultima nao gravou o fim (o banco falhou): nada mais sai.
            await _encerrar_aviso(c, a.id, "sem_resposta")
            await registrar(
                c, papel="despachante", tipo="aviso_bloqueio_sem_resposta", campos={"sistema": sistema.nome}, **comum
            )
            CONTADORES.somar("selo_avisos_bloqueio", desfecho="sem_resposta")
            return True
        return False

    async def _mandar_aviso(self, vez: _VezDoAviso) -> None:
        sistema = vez.sistema
        numeros = [n.numero for n in vez.operacoes]
        resposta = await self._perguntar(
            sistema,
            vez.segredo,
            contrato.FERRAMENTA_BLOQUEIO,
            {"bloqueio": str(vez.aviso_id), "operacoes": numeros},
            teto=TETO_DO_AVISO,
        )
        lida: respostas.RespostaAoAviso | None = None
        chave_recusada = False
        if resposta.tipo == "resultado" and resposta.resultado is not None:
            lido = respostas.ler_bloqueio(resposta.resultado, bloqueio=str(vez.aviso_id), numeros=numeros)
            if isinstance(lido, respostas.Invalida):
                CONTADORES.somar("selo_respostas_invalidas", origem="aviso", motivo=lido.motivo)
            else:
                lida = lido
        elif resposta.tipo == "recusado_http" and resposta.status_http[-1:] == (401,) and vez.aberta is not None:
            # A chave da pessoa nao vale mais: a falha e do MCP-CLARO (a proxima vez renova antes, ou espera a chave).
            await self._marcar_vencida(sistema, vez.pessoa_id, vez.aberta)
            chave_recusada = True
        ultima = vez.tentativa >= self.tentativas_do_aviso
        if lida is None and not ultima:
            CONTADORES.somar("selo_avisos_bloqueio", desfecho="tentativa_sem_resposta")
            return  # a proxima vez ja foi reservada
        async with self.engine.begin() as c:
            if lida is None:
                comum: dict[str, Any] = {"pessoa_id": vez.pessoa_id, "sistema_id": sistema.id}
                if chave_recusada:
                    # A ultima tentativa caiu na chave da pessoa: o MCP-CLARO nao conseguiu avisar (critica M7). O
                    # aviso fica "sistema_fora" (o comprovante nao culpa o sistema), com o motivo nosso no extrato.
                    await _encerrar_aviso(c, vez.aviso_id, "sistema_fora")
                    await registrar(
                        c,
                        papel="despachante",
                        tipo="aviso_bloqueio_sistema_fora",
                        campos={"sistema": sistema.nome, "motivo": CHAVE_RECUSADA},
                        **comum,
                    )
                    CONTADORES.somar("selo_avisos_bloqueio", desfecho="sistema_fora")
                    return
                await _encerrar_aviso(c, vez.aviso_id, "sem_resposta")
                await registrar(
                    c,
                    papel="despachante",
                    tipo="aviso_bloqueio_sem_resposta",
                    campos={"sistema": sistema.nome},
                    **comum,
                )
                CONTADORES.somar("selo_avisos_bloqueio", desfecho="sem_resposta")
                return
            await _encerrar_aviso(c, vez.aviso_id, "respondido")
            for n in vez.operacoes:
                estado_do_numero = lida.estado_de(n.numero)
                await registrar(
                    c,
                    papel="despachante",
                    tipo="sistema_informou_no_bloqueio",
                    campos={"sistema": sistema.nome, "protocolo": n.protocolo, "estado": estado_do_numero},
                    protocolo=n.protocolo,
                    pessoa_id=vez.pessoa_id,
                    aplicativo_id=n.aplicativo_id,
                    sistema_id=sistema.id,
                    dados={"origem": "aviso", "estado": estado_do_numero},
                )
            contraditas: list[Contradito] = []
            for n in vez.operacoes:  # por ultimo, todas do mesmo sistema (uma trava so)
                info = Informacao(
                    n.operacao_id,
                    sistema.id,
                    "aviso",
                    "respondeu",
                    estado=lida.estado_de(n.numero),
                    aviso_id=vez.aviso_id,
                )
                if await gravar_informacao(c, info):
                    contraditas.append(Contradito(n.protocolo, vez.pessoa_id, n.aplicativo_id, n.teste_id))
            if contraditas:
                # Uma vez so por transacao: o aviso ao ADMIN e o contador contam testes retirados, nao numeros (D10).
                await contradicoes(
                    c,
                    sistema_id=sistema.id,
                    sistema_nome=sistema.nome,
                    contraditos=contraditas,
                    emails_admin=self.emails_admin,
                )
        CONTADORES.somar("selo_avisos_bloqueio", desfecho="respondido")


# --- Pecas -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Vez:
    operacao_id: uuid.UUID
    numero: str
    nivel: int
    teste_id: uuid.UUID
    enviado_em: datetime
    agora: datetime
    protocolo: str
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    sistema: Sistema
    linhas: tuple[contrato.LinhaInformada, ...]
    ultima: bool
    fora: str | None


@dataclass(frozen=True, slots=True)
class _NumeroDoAviso:
    operacao_id: uuid.UUID
    numero: str
    protocolo: str
    aplicativo_id: uuid.UUID
    teste_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _VezDoAviso:
    aviso_id: uuid.UUID
    sistema: Sistema
    pessoa_id: uuid.UUID
    tentativa: int
    operacoes: tuple[_NumeroDoAviso, ...]
    segredo: str | None = field(default=None, repr=False)
    aberta: Any = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _AvisoPego:
    """O aviso que esta copia pegou (com a proxima vez um degrau para a frente), antes de abrir a chave."""

    aviso_id: uuid.UUID
    sistema: Sistema
    pessoa_id: uuid.UUID
    tentativas: int


_COLUNAS_DO_AVISO: Final = (
    "SELECT a.id, a.bloqueio_id, a.sistema_id, a.pessoa_id, a.operacoes, a.tentativas, a.criado_em,"
    " b.encerrado_em, s.situacao, s.versao_mcp, s.origem, s.revisao_servidor_id,"
    " (SELECT t.revisao_servidor_id FROM envios_selo e JOIN testes_selo t ON t.id = e.teste_id"
    "  WHERE e.operacao_id = ANY (a.operacoes) ORDER BY e.gravado_em DESC LIMIT 1) AS revisao_do_teste"
    " FROM avisos_bloqueio a JOIN bloqueios b ON b.id = a.bloqueio_id JOIN sistemas s ON s.id = a.sistema_id"
)
_SQL_DO_AVISO_NA_HORA: Final = (
    _COLUNAS_DO_AVISO + " WHERE a.estado = 'pendente' AND a.proxima_em <= now()"
    " ORDER BY a.proxima_em, a.id LIMIT 1 FOR UPDATE OF a SKIP LOCKED"
)
"""O proximo aviso pendente na hora (``FOR UPDATE SKIP LOCKED``: duas copias nunca pegam o mesmo)."""
_SQL_DO_AVISO_PEGO: Final = (
    _COLUNAS_DO_AVISO + " WHERE a.id = :a AND a.estado = 'pendente' AND a.tentativas = :t FOR UPDATE OF a SKIP LOCKED"
)
"""O aviso pego, se ninguem o mudou depois (nenhuma tentativa a mais, ainda pendente)."""


def _espera_do_aviso(tentativa: int) -> int:
    """Minutos ate a vez seguinte, depois da tentativa ``tentativa`` (contada de 1): 1, 5 e 15, e 15 dali em diante."""
    return ESPERAS_DO_AVISO_MINUTOS[min(tentativa, len(ESPERAS_DO_AVISO_MINUTOS)) - 1]


_SQL_DO_AVISO: Final = """
WITH b AS (SELECT * FROM bloqueios WHERE id = :b),
pelo_comprovante AS (
  SELECT p ->> 'protocolo' AS protocolo FROM comprovantes c, jsonb_array_elements(c.ja_saidas || c.em_conferencia) p
  WHERE c.bloqueio_id = :b
),
candidatas AS (
  SELECT e.operacao_id, e.sistema_id, o.pessoa_id, e.chave, e.gravado_em
  FROM envios_selo e JOIN operacoes o ON o.id = e.operacao_id CROSS JOIN b
  WHERE e.nivel = 3
    AND (o.protocolo IN (SELECT protocolo FROM pelo_comprovante)
         OR (e.gravado_em >= b.criado_em - make_interval(days => 30) AND e.gravado_em <= b.criado_em
             AND CASE b.alcance
                   WHEN 'pessoa' THEN o.pessoa_id = b.pessoa_id
                   WHEN 'aplicativo' THEN o.aplicativo_id = b.aplicativo_id
                   WHEN 'pessoa_aplicativo' THEN o.pessoa_id = b.pessoa_id AND o.aplicativo_id = b.aplicativo_id
                   WHEN 'sistema' THEN o.sistema_id = b.sistema_id
                   ELSE true
                 END
             AND (SELECT i.estado FROM informacoes_sistema i
                  WHERE i.operacao_id = e.operacao_id AND i.desfecho = 'respondeu'
                  ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1) = ANY (CAST(:abertos AS text[]))))
)
SELECT DISTINCT ON (chave) operacao_id, sistema_id, pessoa_id FROM candidatas
ORDER BY chave, gravado_em DESC, operacao_id
"""
"""As operacoes do aviso de um bloqueio (a mesma regra de ``avisos_bloqueio_nasce``): saidas com numero de nivel 3
que estao no comprovante (ja saidas ou em conferencia) ou que, sendo do alcance do bloqueio, sairam ate 30 dias antes
dele e tem o ultimo estado informado em aberto (critica B9). Cada numero uma vez (o original e o pedido igual tem o
mesmo, critica B12): fica o envio mais recente."""


_SQL_DA_RETIRADA: Final = """
WITH corte AS (SELECT greatest(CAST(:t AS timestamptz), now() - make_interval(days => :d)) AS desde),
recentes AS (
  SELECT i.operacao_id, max(i.gravado_em) AS ultima FROM informacoes_sistema i, corte
  WHERE i.sistema_id = :s AND i.origem = 'consulta' AND i.gravado_em > corte.desde
  GROUP BY i.operacao_id
),
ops AS (
  SELECT r.operacao_id, r.ultima FROM recentes r
  WHERE selo_estado_final(r.operacao_id) IS NOT NULL
     OR (SELECT count(*) FROM informacoes_sistema x
         WHERE x.operacao_id = r.operacao_id AND x.origem = 'consulta'
           AND x.desfecho = ANY (CAST(:gastam AS text[]))) >= :maximo
  ORDER BY r.ultima DESC LIMIT :k
)
SELECT ops.operacao_id, i.origem, i.desfecho, i.estado, i.gravado_em, ops.ultima
FROM ops CROSS JOIN LATERAL (
  SELECT x.origem, x.desfecho, x.estado, x.gravado_em, x.id FROM informacoes_sistema x
  WHERE x.operacao_id = ops.operacao_id
) i
ORDER BY ops.ultima DESC, ops.operacao_id, i.gravado_em, i.id
"""
"""As linhas das operacoes do sistema que terminaram a escada (consulta depois do corte: o teste do selo de agora, ou
30 dias), das que terminaram mais perto de agora para tras: so as com resposta que encerra (``selo_estado_final``, a
regra de ``contrato.resposta_que_encerra``) ou com o maximo de tentativas que gastam. As que ainda estao na escada, ou
pararam antes do maximo (``sistema_fora``, fim pelo tempo), saem antes do ``LIMIT`` (D4 da conferencia da etapa 3):
uma rajada de operacoes ainda sendo consultadas nao segura a retirada. ``recentes`` le o indice
``informacoes_sistema_consultas`` da 0007, e as linhas de cada operacao vem pelo indice dela (``LATERAL``), sem ler a
tabela inteira, que e so de acrescentar e de todos os sistemas."""


async def _motivo_novo(c: AsyncConnection, protocolo: str, motivo: str | None) -> bool:
    """A ultima linha ``consulta_nao_feita`` da operacao (pelo protocolo) nao tem este motivo (decisao 3 do
    coordenador: sem a chave da pessoa, o extrato nao ganha a mesma linha a cada degrau)."""
    ultimo = (
        await c.execute(
            text(
                "SELECT dados ->> 'motivo' FROM extrato WHERE protocolo = :p AND tipo = 'consulta_nao_feita'"
                " ORDER BY id DESC LIMIT 1"
            ),
            {"p": protocolo},
        )
    ).first()
    return ultimo is None or ultimo[0] != motivo


async def _linhas(c: AsyncConnection, operacao_id: uuid.UUID) -> list[contrato.LinhaInformada]:
    return [
        contrato.LinhaInformada(str(x.origem), str(x.desfecho), x.estado, x.gravado_em)
        for x in await c.execute(
            text(
                "SELECT origem, desfecho, estado, gravado_em FROM informacoes_sistema WHERE operacao_id = :o"
                " ORDER BY gravado_em, id"
            ),
            {"o": operacao_id},
        )
    ]


def _vale_perguntar(fila: str, linhas: Sequence[contrato.LinhaInformada]) -> bool:
    """Ha duvida a tirar: a acao ficou "nao confirmada", ou saiu "enviada" com o ultimo estado informado pelo recibo
    ou pela consulta em aberto (``em_andamento`` ou ``desconhecido``, critica B9). A resposta ao aviso nao conta
    (``contrato.ORIGENS_DA_RESPOSTA_FINAL``, decisao 1 do coordenador): um ``parada`` do aviso sobre uma escrita em
    andamento nao encerra, e a consulta seguinte pega o sistema que escreveu mesmo assim. O resto (ainda na fila,
    enviando, nao saiu, enviada com desfecho) nao se pergunta."""
    if fila == "nao_confirmada":
        return True
    if fila != "enviada":
        return False
    ditos = [x.estado for x in linhas if x.desfecho == "respondeu" and x.origem in contrato.ORIGENS_DA_RESPOSTA_FINAL]
    return bool(ditos) and ditos[-1] in contrato.ESTADOS_EM_ABERTO


def terminou_sem_resposta(linhas: Sequence[contrato.LinhaInformada], maximo: int) -> bool | None:
    """Como a escada de uma operacao terminou, para a conta da retirada (critica B8): ``True`` sem resposta que
    encerra, com a ultima consulta sem resposta, invalida ou ``desconhecido``; ``False`` com resposta que encerra,
    ou com a escada acabada numa resposta do sistema (``em_andamento``, ``nao_recebida`` sozinha); ``None`` quando
    nao conta: ainda na escada, parada por ``sistema_fora`` na ultima consulta (o sistema nao foi perguntado;
    ``contrato.parou_por_sistema_fora``, mesmo com uma resposta ao aviso depois) ou acabada pelo tempo sem o
    maximo de tentativas que gastam (``contrato.escada_vencida``: o sistema nao foi perguntado o bastante)."""
    if contrato.resposta_que_encerra(linhas) is not None:
        return False
    if contrato.parou_por_sistema_fora(linhas):
        return None
    if contrato.tentativas_da_consulta(linhas) < maximo:
        return None
    consultas = [x for x in linhas if x.origem == "consulta" and x.desfecho in contrato.DESFECHOS_QUE_GASTAM_TENTATIVA]
    ultima = consultas[-1]
    return ultima.desfecho in contrato.DESFECHOS_DE_FALHA_DO_SISTEMA or (
        ultima.desfecho == "respondeu" and ultima.estado == "desconhecido"
    )


async def _avisar_que_ainda_nao_saiu(c: AsyncConnection, a: Any, sistema: Sistema, motivo: str) -> None:
    """Na primeira vez que a chave da pessoa nao abre para o aviso do bloqueio, a dona ganha uma linha legivel
    (``aviso_bloqueio_ainda_nao_saiu``), uma so por bloqueio e sistema: as partes de ate 100 numeros do mesmo bloqueio
    sao um aviso so para ela, e as voltas seguintes sem a chave nao repetem a linha. O motivo e o nosso (``SEM_CHAVE``),
    nunca texto do sistema. A procura vai pelo indice da pessoa no extrato, so desde que o aviso nasceu; duas copias
    do despachante com duas partes do mesmo bloqueio fazem fila na trava da transacao, e a segunda ve a linha da
    primeira."""
    pessoa_id = uuid.UUID(str(a.pessoa_id))
    await c.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:t), hashtext(:k))"),
        {"t": TRAVA_DO_AVISO_SEM_CHAVE, "k": f"{a.bloqueio_id}:{sistema.id}:{pessoa_id}"},
    )
    ja = (
        await c.execute(
            text(
                "SELECT 1 FROM extrato WHERE pessoa_id = :p AND criado_em >= :desde"
                " AND tipo = 'aviso_bloqueio_ainda_nao_saiu' AND sistema_id = :s AND dados ->> 'bloqueio' = :b LIMIT 1"
            ),
            {"p": pessoa_id, "desde": a.criado_em, "s": sistema.id, "b": str(a.bloqueio_id)},
        )
    ).first()
    if ja is not None:
        return
    await registrar(
        c,
        papel="despachante",
        tipo="aviso_bloqueio_ainda_nao_saiu",
        campos={"sistema": sistema.nome, "motivo": motivo},
        pessoa_id=pessoa_id,
        sistema_id=sistema.id,
        dados={"bloqueio": str(a.bloqueio_id)},
    )


async def _encerrar_aviso(c: AsyncConnection, aviso_id: Any, estado: str) -> None:
    await c.execute(text("UPDATE avisos_bloqueio SET estado = :e WHERE id = :a"), {"a": aviso_id, "e": estado})


async def _soltar(c: AsyncConnection, trava: str, chave: str) -> None:
    """Solta a trava de sessao. Se a conexao falhar, ela e descartada (e a trava vai junto)."""
    try:
        if c.in_transaction():
            await c.rollback()
        await c.execute(text("SELECT pg_advisory_unlock(hashtext(:t), hashtext(:k))"), {"t": trava, "k": chave})
        await c.commit()
    except Exception:  # noqa: BLE001 - a conexao some e leva a trava
        await c.invalidate()
