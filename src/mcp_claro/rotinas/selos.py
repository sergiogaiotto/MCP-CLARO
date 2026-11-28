"""Rotinas dos selos de nivel 2 e 3 (Fase 4, desenho 5.2 e 7.4): pedir a consulta das acoes em duvida e avisar o
ADMIN sobre os selos.

As rotinas nunca falam com os sistemas nem abrem chaves. Quem pergunta ao sistema e o despachante, pela rota
``contrato.ROTA_CONSULTAR`` (``ClienteDespachante.conferir``); ele confere tudo de novo pelo banco (critica M7) e grava
o que o sistema disse.

``conferir_pelo_sistema`` (a cada ``BADGE_LOOKUP_INTERVAL_SECONDS``):

- escolhe ate ``LIMITE_POR_RODADA`` operacoes cuja vez de perguntar ja chegou, pela mesma regra da rota: a fila
  ``nao_confirmada``, ou a ``enviada`` cujo ultimo estado dito pelo recibo ou pela consulta esta em aberto
  (``em_andamento`` ou ``desconhecido``, critica B9; ``vale_perguntar``), sem outro protocolo que saiu depois com o
  mesmo numero (``selo_substituto``). A vez sai de ``contrato.proxima_consulta`` (degrau por todas as linhas de
  consulta, piso no prazo do envio mais a margem da critica A3.3, resposta que encerra, ``sistema_fora`` na ultima
  consulta, maximo de tentativas, fim pelo tempo) e de ``contrato.escada_vencida``, comparadas com o ``now()`` do
  banco lido na mesma transacao que leu as linhas, nunca com o relogio da maquina. Uma ``nao_recebida`` sozinha nao
  encerra, e a resposta ao aviso de bloqueio nunca encerra (``contrato.resposta_que_encerra``);
- pede ao despachante uma por uma, sem transacao aberta: o despachante decide pelo banco se ainda e a vez e se e a
  ultima tentativa (por isso o pedido nao leva "ultima"). ``nada_a_perguntar`` e ``sem_envio`` so pulam a operacao;
  a porta de saida fechada (409 ``porta_fechada``) e o despachante fora do ar param a rodada, sem contar nada e sem
  dar a escada por acabada: a proxima rodada tenta de novo. ``conferencia_nao_feita`` (503) pula a operacao e segue
  com as outras;
- duas copias das rotinas: a trava consultiva da rodada (``TRAVA_DA_RODADA``, alem da trava do agendador) faz uma
  rodada so por vez, e o despachante ainda trava cada operacao (critica M8).

``avisar_selos_a_vencer`` (a cada ``ConfigRotinas.intervalo_aviso_vencer``), avisos ao ADMIN por e-mail e webhook
(desenho 7.4), cada um uma vez so (``chave_unica``):

- o selo de nivel 2 ou 3 que vence em ate ``config.politica.dias_de_aviso_do_selo(BADGE_TTL_DAYS,
  BADGE_WARN_DAYS)`` dias: ``selo_a_vencer``, um por teste, com a linha ``selo_a_vencer`` no extrato;
- o teste que nao foi aprovado: ``selo_nao_aprovado``, um por teste, com a ``chave_unica`` da bateria
  (``chave_do_nao_aprovado``), que ja avisa na transacao que grava o teste: nada sai duas vezes;
- a retirada (do ADMIN, por contradicao ou por falta de resposta): ``selo_retirado``, um por motivo e teste, com a
  mesma ``chave_unica`` do despachante (``selo.consulta.avisar_retirada``), que ja avisa as retiradas que ele grava:
  nada sai duas vezes;
- o selo que caiu porque o sistema mudou (saiu de ativo, endereco, tipo de chave, revisao do servidor, ferramenta de
  acao nova, ferramentas do contrato fora da lista): ``selo_retirado``, com o motivo da mudanca, um por motivo e
  teste. O selo calculado (``selo_vigente``) pode voltar sozinho quando a mudanca se desfaz (o motivo do aviso diz
  isso, ``VOLTA_SOZINHO``); se cair de novo pelo mesmo motivo, o aviso nao se repete.

Os avisos levam o nome do cadastro, niveis, datas e contagens: nunca protocolo, pessoa, nome de ferramenta (num
terceiro e texto do fornecedor) nem nada escrito pelo sistema.

Contadores: o despachante ja soma o que acontece nas perguntas (``selo_informacoes`` e os outros). As rotinas somam
so o que e delas: ``selo_consultas_pedidas{resultado}``, como terminou cada pedido ao despachante.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.config.politica import dias_de_aviso_do_selo
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.rotinas.agendador import Tarefa, trava_consultiva
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.despachante import DespachanteFora, NadaAPerguntar, PortaFechada, SemEnvio
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita
from mcp_claro.rotinas.politica import valor_politica
from mcp_claro.selo import contrato

log = logger("mcp_claro.rotinas.selos")

LIMITE_POR_RODADA: Final = 20
"""Operacoes pedidas ao despachante por rodada (desenho 5.2)."""

CANDIDATAS_LIDAS: Final = 500
"""Teto das operacoes ainda na escada lidas por rodada, das que estao paradas ha mais tempo para as mais novas (a
hora base da escada). Bem acima de ``LIMITE_POR_RODADA``: so uma rajada fora do comum chega nele (fica no log). O SQL
so le escadas que ainda nao acabaram (``SQL_CANDIDATAS``): a que acabou nunca ganha linha nova e, se entrasse, ficaria
na frente do lote ate o fim pelo tempo. As lidas giram, porque cada pergunta poe a operacao no fim da fila."""

TRAVA_DA_RODADA: Final = "selos.conferir_pelo_sistema"
"""Trava consultiva da rodada (espaco ``agendador.ESPACO_DAS_TRAVAS``). E outro nome que o da tarefa: o agendador ja
segura a trava com o nome dela noutra conexao, e a trava de sessao so e reentrante na mesma conexao."""

JANELA_DOS_AVISOS: Final = timedelta(days=7)
"""Testes nao aprovados e retiradas mais velhos que isto nao geram aviso novo (as rotinas rodam de hora em hora; a
janela cobre dias de rotinas paradas sem mandar ao ADMIN um aviso de meses atras)."""

MOTIVOS_DA_RETIRADA: Final[Mapping[str, str]] = {
    "admin": "o ADMIN retirou o selo",
    "se_contradisse": "o sistema deu respostas que nao combinam",
    "nao_respondeu_consultas": "o sistema nao respondeu as consultas",
}
"""O motivo de ``selos_retirados`` no aviso ``selo_retirado`` (sem acento, como as frases). Os dois automaticos tem o
texto do despachante (``selo.consulta.MOTIVOS_DA_RETIRADA``; um teste confere)."""

VOLTA_SOZINHO: Final = "; se a mudanca se desfizer, o selo volta sozinho, sem teste novo"
"""O fim do motivo da queda que se desfaz: ``selo_vigente`` e calculado na hora, e o selo aprovado e na validade volta
quando o sistema volta ao que foi testado. O texto do aviso ``selo_retirado`` (``extrato/frases.py``) diz "foi
retirado" e "peca um teste novo"; para esses motivos, isso nao e tudo (pendencia ao coordenador: um aviso proprio)."""

MOTIVOS_DA_QUEDA: Final[Mapping[str, str]] = {
    "fora_de_ativo": "o sistema saiu de ativo" + VOLTA_SOZINHO,
    "endereco_mudou": "o endereco do sistema mudou depois do teste" + VOLTA_SOZINHO,
    "tipo_chave_mudou": "o tipo de chave do sistema mudou depois do teste" + VOLTA_SOZINHO,
    "revisao_nova": "a revisao do servidor e nova",
    "ferramenta_nova": "uma ferramenta de acao e nova ou mudou depois do teste" + VOLTA_SOZINHO,
    "contrato_sumiu": "o MCP-CLARO nao viu as ferramentas do contrato na lista do sistema nas ultimas 48 horas"
    + VOLTA_SOZINHO,
}
"""Os motivos de ``selo_vigente`` que derrubam um selo aprovado e ainda na validade porque o sistema mudou (desenho
7.4), no aviso ``selo_retirado``. ``reprovado``, ``retirado`` e ``vencido`` tem aviso proprio (ou o de antes de
vencer). A revisao nova nao volta sozinha: o cadastro so troca a revisao vigente por uma mais nova."""


def chave_do_nao_aprovado(teste_id: object) -> str:
    """A ``chave_unica`` do aviso ``selo_nao_aprovado``: a mesma da bateria (``selo.bateria``, que avisa na transacao
    que grava o teste). O ADMIN recebe um aviso so por teste, de quem mandar primeiro (defeito S2 do verificador da
    etapa 4: com chaves diferentes, saiam dois)."""
    return f"selo_nao_aprovado|{teste_id}"


FUSO_PADRAO: Final = "America/Sao_Paulo"
"""Fuso das datas dos avisos quando quem chama nao da outro (o padrao de ``SCREENS_TIMEZONE``)."""


# --- Conferir pelo sistema -------------------------------------------------------------------------------------


def vale_perguntar(fila: str, linhas: Sequence[contrato.LinhaInformada]) -> bool:
    """A mesma regra da rota (``selo.consulta._vale_perguntar``; um teste confere as duas): a fila ficou
    ``nao_confirmada``, ou saiu ``enviada`` com o ultimo estado dito pelo recibo ou pela consulta em aberto
    (``em_andamento`` ou ``desconhecido``, critica B9). A resposta ao aviso de bloqueio nao conta
    (``contrato.ORIGENS_DA_RESPOSTA_FINAL``)."""
    if fila == "nao_confirmada":
        return True
    if fila != "enviada":
        return False
    ditos = [x.estado for x in linhas if x.desfecho == "respondeu" and x.origem in contrato.ORIGENS_DA_RESPOSTA_FINAL]
    return bool(ditos) and ditos[-1] in contrato.ESTADOS_EM_ABERTO


def vez_de_perguntar(
    fila: str,
    linhas: Sequence[contrato.LinhaInformada],
    *,
    enviado_em: datetime,
    agora: datetime,
    maximo: int,
    prazo_envio_segundos: float,
) -> datetime | None:
    """A hora da vez da operacao, se ela ja chegou em ``agora`` (o ``now()`` do banco), ou ``None``: nao ha duvida a
    tirar (``vale_perguntar``), a escada acabou ou a vez ainda nao chegou. ``linhas`` em ordem de ``(gravado_em,
    id)``. As funcoes sao as do contrato, as mesmas da rota."""
    if not vale_perguntar(fila, linhas):
        return None
    vez = contrato.proxima_consulta(
        linhas, enviado_em=enviado_em, maximo=maximo, prazo_envio_segundos=prazo_envio_segundos
    )
    if vez is None or vez > agora or contrato.escada_vencida(enviado_em, agora):
        return None
    return vez


SQL_CANDIDATAS: Final = """
SELECT e.operacao_id, e.gravado_em AS enviado_em, f.estado AS fila, selo_substituto(e.operacao_id) AS substituto
FROM fila_saida f JOIN envios_selo e ON e.operacao_id = f.operacao_id
WHERE f.estado IN ('nao_confirmada', 'enviada')
  AND f.mudou_em > now() - make_interval(days => :dias)
  AND e.gravado_em > now() - make_interval(days => :dias)
  AND (f.estado = 'nao_confirmada' OR (
    SELECT i.estado FROM informacoes_sistema i
    WHERE i.operacao_id = e.operacao_id AND i.origem = ANY (CAST(:finais AS text[])) AND i.desfecho = 'respondeu'
    ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1) = ANY (CAST(:abertos AS text[])))
  AND (SELECT count(*) FROM informacoes_sistema i
       WHERE i.operacao_id = e.operacao_id AND i.origem = 'consulta' AND i.desfecho = ANY (CAST(:gastam AS text[])))
      < :maximo
  AND selo_estado_final(e.operacao_id) IS NULL
  AND coalesce((SELECT i.desfecho FROM informacoes_sistema i
                WHERE i.operacao_id = e.operacao_id AND i.origem = 'consulta'
                ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1), '') <> 'sistema_fora'
  AND selo_substituto(e.operacao_id) IS NULL
ORDER BY coalesce((SELECT max(i.gravado_em) FROM informacoes_sistema i WHERE i.operacao_id = e.operacao_id),
                  e.gravado_em), e.operacao_id
LIMIT :lote
"""
"""As operacoes cuja escada ainda nao acabou, sem conta de tempo: so o que a escada exige e nao muda com o relogio
(envio de ate ``contrato.REAPROVEITAR_NUMERO_DIAS`` dias, a fila em duvida, o maximo de tentativas que gastam, nenhuma
resposta que encerra (``selo_estado_final``, a mesma regra de ``contrato.resposta_que_encerra``), a ultima consulta
fora de ``sistema_fora`` (``contrato.parou_por_sistema_fora``) e nenhum outro protocolo com o mesmo numero depois
(``selo_substituto``)). Sem esses filtros antes do ``LIMIT``, as escadas acabadas tomariam o lote, das mais paradas
para as mais novas, e a acao na vez nao seria lida. A vez e calculada em Python, pelas funcoes do contrato
(``vez_de_perguntar``), que tambem conferem de novo cada uma dessas paradas. Cada operacao le as proprias linhas pelo
indice ``informacoes_sistema_operacao``; a ``nao_confirmada`` vem pelo indice ``fila_em_aberto`` e a ``enviada`` por
``fila_mudou``."""

SQL_LINHAS: Final = """
SELECT operacao_id, origem, desfecho, estado, gravado_em FROM informacoes_sistema
WHERE operacao_id = ANY (CAST(:ids AS uuid[])) ORDER BY operacao_id, gravado_em, id
"""


@dataclass(frozen=True, slots=True)
class Candidata:
    operacao_id: uuid.UUID
    vez: datetime


async def operacoes_na_vez(
    c: AsyncConnection, *, maximo: int, prazo_envio_segundos: float, limite: int = LIMITE_POR_RODADA
) -> list[Candidata]:
    """As operacoes cuja vez de perguntar ja chegou, ate ``limite``, da vez mais antiga para a mais nova. Le tudo,
    com o ``now()`` do banco, na transacao de ``c``; quem chama a fecha antes de pedir ao despachante."""
    agora = (await c.execute(text("SELECT now()"))).scalar_one()
    linhas_sql = (
        await c.execute(
            text(SQL_CANDIDATAS),
            {
                "dias": contrato.REAPROVEITAR_NUMERO_DIAS,
                "finais": list(contrato.ORIGENS_DA_RESPOSTA_FINAL),
                "abertos": list(contrato.ESTADOS_EM_ABERTO),
                "gastam": list(contrato.DESFECHOS_QUE_GASTAM_TENTATIVA),
                "maximo": maximo,
                "lote": CANDIDATAS_LIDAS,
            },
        )
    ).all()
    if len(linhas_sql) >= CANDIDATAS_LIDAS:
        log.warning("selo_consultas_muitas_na_escada", lidas=len(linhas_sql))
    abertas = [x for x in linhas_sql if x.substituto is None]  # conferencia: o SQL ja tirou as substituidas
    por_operacao: dict[uuid.UUID, list[contrato.LinhaInformada]] = {}
    if abertas:
        for x in await c.execute(text(SQL_LINHAS), {"ids": [uuid.UUID(str(a.operacao_id)) for a in abertas]}):
            por_operacao.setdefault(uuid.UUID(str(x.operacao_id)), []).append(
                contrato.LinhaInformada(str(x.origem), str(x.desfecho), x.estado, x.gravado_em)
            )
    prontas: list[Candidata] = []
    for a in abertas:
        operacao_id = uuid.UUID(str(a.operacao_id))
        vez = vez_de_perguntar(
            str(a.fila),
            por_operacao.get(operacao_id, []),
            enviado_em=a.enviado_em,
            agora=agora,
            maximo=maximo,
            prazo_envio_segundos=prazo_envio_segundos,
        )
        if vez is not None:
            prontas.append(Candidata(operacao_id, vez))
    prontas.sort(key=lambda x: (x.vez, str(x.operacao_id)))
    return prontas[:limite]


@dataclass(slots=True)
class RodadaDaConsulta:
    """Como foi uma rodada de ``conferir_pelo_sistema`` (para o log e os testes; nada disto vai ao extrato)."""

    ocupada: bool = False
    """Outra copia das rotinas estava na rodada: esta nao pediu nada."""
    na_vez: int = 0
    perguntadas: dict[uuid.UUID, contrato.RespostaDaConsulta] = field(default_factory=dict)
    puladas: list[uuid.UUID] = field(default_factory=list)
    """O despachante disse que nada havia a perguntar (ja nao era a vez, outra copia perguntando) ou sem envio."""
    nao_feitas: list[uuid.UUID] = field(default_factory=list)
    """A falha foi do MCP-CLARO (503 ``conferencia_nao_feita`` ou outra falha): fica para a proxima rodada."""
    parou: str | None = None
    """``porta_fechada`` ou ``despachante_fora``: a rodada parou ai, e as que faltavam ficam para a proxima."""


async def conferir_pelo_sistema(
    engine: AsyncEngine,
    despachante: contrato.ConfereNoDespachante,
    *,
    maximo: int,
    prazo_envio_segundos: float,
    limite: int = LIMITE_POR_RODADA,
) -> RodadaDaConsulta:
    """Uma rodada: pede ao despachante a consulta de cada operacao cuja vez chegou (``operacoes_na_vez``).
    ``maximo`` e ``BADGE_LOOKUP_MAX_ATTEMPTS`` e ``prazo_envio_segundos`` e ``RESPONSE_MAX_WAIT_SECONDS``, os mesmos
    do despachante. Nenhuma transacao fica aberta enquanto o despachante pergunta."""
    rodada = RodadaDaConsulta()
    async with trava_consultiva(engine, TRAVA_DA_RODADA) as minha:
        if not minha:
            rodada.ocupada = True
            return rodada
        async with engine.begin() as c:
            prontas = await operacoes_na_vez(c, maximo=maximo, prazo_envio_segundos=prazo_envio_segundos, limite=limite)
        rodada.na_vez = len(prontas)
        for candidata in prontas:
            resultado = await _pedir(despachante, candidata.operacao_id, rodada)
            CONTADORES.somar("selo_consultas_pedidas", resultado=resultado)
            if rodada.parou is not None:
                break
    if rodada.na_vez or rodada.parou:
        log.info(
            "selo_consultas_pedidas",
            na_vez=rodada.na_vez,
            perguntadas=len(rodada.perguntadas),
            puladas=len(rodada.puladas),
            nao_feitas=len(rodada.nao_feitas),
            parou=rodada.parou,
        )
    return rodada


async def _pedir(despachante: contrato.ConfereNoDespachante, operacao_id: uuid.UUID, rodada: RodadaDaConsulta) -> str:
    """Um pedido ao despachante. Devolve o rotulo do contador ``selo_consultas_pedidas``."""
    try:
        rodada.perguntadas[operacao_id] = await despachante.conferir(operacao_id)
    except PortaFechada:
        rodada.parou = "porta_fechada"
        return "porta_fechada"
    except DespachanteFora as erro:
        rodada.parou = "despachante_fora"
        log.warning("selo_consulta_sem_despachante", erro=type(erro.__cause__ or erro).__name__)
        return "despachante_fora"
    except SemEnvio:
        rodada.puladas.append(operacao_id)
        return "sem_envio"
    except NadaAPerguntar:
        rodada.puladas.append(operacao_id)
        return "nada_a_perguntar"
    except ConferenciaNaoFeita:
        rodada.nao_feitas.append(operacao_id)
        return "nao_feita"
    except Exception as erro:  # noqa: BLE001 - uma operacao nunca para a rodada das outras
        rodada.nao_feitas.append(operacao_id)
        log.warning("selo_consulta_pedido_falhou", erro=type(erro).__name__)
        return "erro"
    return "feita"


# --- Avisos ao ADMIN sobre os selos ----------------------------------------------------------------------------


def casos_que_nao_aprovam(itens: Sequence[Mapping[str, Any]], nivel: int) -> int:
    """Quantos itens de ``testes_selo.casos`` impediram a aprovacao num teste de nivel ``nivel``: os ``falhou`` e os
    de caso obrigatorio do nivel que nao aprovam (``contrato.resultado_aprova``: ``nao_testado``, ``nao_observado``
    fora do maximo, ``nao_se_aplica`` onde nao cabe). E o ``{falhas}`` do aviso ``selo_nao_aprovado`` ("casos
    falharam ou ficaram sem teste"): a conta unica de ``contrato.casos_que_nao_aprovam``."""
    return contrato.casos_que_nao_aprovam(list(itens), nivel)


def _data(momento: datetime, fuso: str) -> str:
    return momento.astimezone(ZoneInfo(fuso)).strftime("%d/%m/%Y")


def dias_ate(vale_ate: datetime, agora: datetime) -> int:
    """Dias inteiros que faltam, arredondados para cima: com 13 dias e meio, "vence em 14 dias"."""
    return max(0, math.ceil((vale_ate - agora) / timedelta(days=1)))


@dataclass(slots=True)
class AvisosDosSelos:
    """Quantos avisos novos entraram na fila nesta rodada, por evento (os repetidos nao contam)."""

    a_vencer: int = 0
    nao_aprovados: int = 0
    retirados: int = 0
    cairam: int = 0


SQL_A_VENCER: Final = """
SELECT v.sistema_id, s.nome, v.nivel, v.teste_id, v.vale_ate, now() AS agora
FROM selos_vigentes v JOIN sistemas s ON s.id = v.sistema_id
WHERE v.nivel >= 2 AND v.vale_ate > now() AND v.vale_ate <= now() + make_interval(days => :dias)
ORDER BY v.vale_ate, v.sistema_id
"""

SQL_NAO_APROVADOS: Final = """
SELECT t.id AS teste_id, t.sistema_id, s.nome, t.nivel_pedido, t.casos
FROM testes_selo t JOIN sistemas s ON s.id = t.sistema_id
WHERE t.resultado <> 'aprovado' AND t.testado_em > now() - make_interval(secs => :janela)
ORDER BY t.testado_em, t.id
"""

SQL_RETIRADOS: Final = """
SELECT DISTINCT r.teste_id, r.sistema_id, s.nome, r.motivo
FROM selos_retirados r JOIN sistemas s ON s.id = r.sistema_id
WHERE r.retirado_em > now() - make_interval(secs => :janela)
ORDER BY r.sistema_id, r.teste_id, r.motivo
"""

SQL_CAIRAM: Final = """
SELECT v.sistema_id, s.nome, v.teste_id, v.motivo
FROM selos_vigentes v JOIN sistemas s ON s.id = v.sistema_id
WHERE v.teste_id IS NOT NULL AND v.motivo = ANY (CAST(:motivos AS text[]))
ORDER BY v.sistema_id
"""
"""Selo aprovado, na validade e sem retirada, que esta no nivel 1 porque o sistema mudou: ``selo_vigente`` escolhe
``reprovado``, ``vencido`` e ``retirado`` antes destes motivos."""


async def _avisar(
    c: AsyncConnection,
    evento: str,
    chave_unica: str,
    emails_admin: Sequence[str],
    *,
    sistema_id: Any,
    corpo: Mapping[str, Any],
    **campos: object,
) -> int:
    assunto, texto_aviso = aviso_admin(evento, **campos)
    return await avisar_admin(
        c,
        evento,
        assunto,
        {"texto": texto_aviso, "sistema_id": str(sistema_id), **corpo},
        chave_unica,
        emails_admin,
    )


async def avisar_selos_a_vencer(
    engine: AsyncEngine, emails_admin: Sequence[str], *, fuso: str = FUSO_PADRAO
) -> AvisosDosSelos:
    """Uma rodada dos avisos ao ADMIN sobre os selos (veja o modulo): a vencer, nao aprovado, retirado e caido por
    mudanca do sistema. Cada aviso uma vez so (``chave_unica``); numa transacao so, como as ordens."""
    feitos = AvisosDosSelos()
    janela = JANELA_DOS_AVISOS.total_seconds()
    async with engine.begin() as c:
        validade = await valor_politica(c, "BADGE_TTL_DAYS")
        dias = dias_de_aviso_do_selo(validade, await valor_politica(c, "BADGE_WARN_DAYS"))
        for v in (await c.execute(text(SQL_A_VENCER), {"dias": dias})).all():
            vale_ate = _data(v.vale_ate, fuso)
            entrou = await _avisar(
                c,
                "selo_a_vencer",
                f"selo_a_vencer:{v.teste_id}",
                emails_admin,
                sistema_id=v.sistema_id,
                corpo={"sistema": v.nome, "nivel": int(v.nivel), "vale_ate": v.vale_ate.isoformat()},
                nivel=int(v.nivel),
                sistema=v.nome,
                dias=dias_ate(v.vale_ate, v.agora),
                vale_ate=vale_ate,
            )
            if entrou:
                feitos.a_vencer += 1
                await registrar(
                    c,
                    papel="rotinas",
                    tipo="selo_a_vencer",
                    campos={"nivel": int(v.nivel), "sistema": v.nome, "vale_ate": vale_ate},
                    sistema_id=uuid.UUID(str(v.sistema_id)),
                    dados={"teste_id": str(v.teste_id), "dias_de_aviso": dias, "destinos": entrou},
                )
        for t in (await c.execute(text(SQL_NAO_APROVADOS), {"janela": janela})).all():
            nivel = int(t.nivel_pedido)
            falhas = casos_que_nao_aprovam(t.casos if isinstance(t.casos, list) else [], nivel)
            if await _avisar(
                c,
                "selo_nao_aprovado",
                chave_do_nao_aprovado(t.teste_id),
                emails_admin,
                sistema_id=t.sistema_id,
                corpo={"sistema": t.nome, "nivel": nivel, "falhas": falhas},
                nivel=nivel,
                sistema=t.nome,
                falhas=falhas,
            ):
                feitos.nao_aprovados += 1
        for r in (await c.execute(text(SQL_RETIRADOS), {"janela": janela})).all():
            motivo = str(r.motivo)
            # A mesma chave de selo.consulta.avisar_retirada: a retirada que o despachante ja avisou nao sai de novo.
            if await _avisar(
                c,
                "selo_retirado",
                f"selo_retirado:{motivo}:{r.sistema_id}:{r.teste_id}",
                emails_admin,
                sistema_id=r.sistema_id,
                corpo={"sistema": r.nome, "motivo": motivo},
                sistema=r.nome,
                motivo=MOTIVOS_DA_RETIRADA.get(motivo, "motivo nao guardado"),
            ):
                feitos.retirados += 1
        for q in (await c.execute(text(SQL_CAIRAM), {"motivos": list(MOTIVOS_DA_QUEDA)})).all():
            motivo = str(q.motivo)
            if await _avisar(
                c,
                "selo_retirado",
                f"selo_caiu:{motivo}:{q.sistema_id}:{q.teste_id}",
                emails_admin,
                sistema_id=q.sistema_id,
                corpo={"sistema": q.nome, "motivo": motivo},
                sistema=q.nome,
                motivo=MOTIVOS_DA_QUEDA[motivo],
            ):
                feitos.cairam += 1
    if feitos.a_vencer or feitos.nao_aprovados or feitos.retirados or feitos.cairam:
        log.info(
            "avisos_dos_selos",
            a_vencer=feitos.a_vencer,
            nao_aprovados=feitos.nao_aprovados,
            retirados=feitos.retirados,
            cairam=feitos.cairam,
        )
    return feitos


# --- Tarefas ---------------------------------------------------------------------------------------------------


def tarefas_dos_selos(
    engine: AsyncEngine, p: Parametros, config: ConfigRotinas, despachante: contrato.ConfereNoDespachante
) -> list[Tarefa]:
    """As tarefas dos selos no formato do agendador. Rodam com ou sem ``FEATURE_PHASE4_BADGES``: so ha envio com
    numero (e consulta) quando o selo efetivo da operacao era 2 ou 3, e o ADMIN declara e testa com a chave
    desligada."""
    prazo = config.prazo_tarefa
    maximo = int(p.BADGE_LOOKUP_MAX_ATTEMPTS)
    prazo_envio = float(p.RESPONSE_MAX_WAIT_SECONDS)
    fuso = str(p.SCREENS_TIMEZONE)
    return [
        Tarefa(
            "conferir_pelo_sistema",
            float(p.BADGE_LOOKUP_INTERVAL_SECONDS),
            lambda: conferir_pelo_sistema(engine, despachante, maximo=maximo, prazo_envio_segundos=prazo_envio),
            prazo,
        ),
        Tarefa(
            "avisar_selos",
            config.intervalo_aviso_vencer,
            lambda: avisar_selos_a_vencer(engine, config.emails_admin, fuso=fuso),
            prazo,
        ),
    ]
