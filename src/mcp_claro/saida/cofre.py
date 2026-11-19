"""Porta de saida e saude do cofre.

Sem o cofre destravado, nenhuma chave abre: enquanto ele estiver trancado, a porta de saida fica fechada. O
despachante que segura a trava unica (``PortaDeSaida``) confere a saude do cofre a cada ``VAULT_CHECK_SECONDS``,
logo antes de marcar cada acao como "enviando" e logo depois de uma chamada ao cofre responder que ele esta
trancado. Esta parte decide e grava; quem chama e a porta.

- Trancado (ou ainda nao iniciado): a saida fecha na hora.
- Fora do ar (sem resposta): fecha so na segunda conferencia seguida, para uma falha de rede passageira nao
  abrir e fechar a porta.
- Destravado de novo: a saida reabre sozinha so se quem a fechou foi esta conferencia e nada mudou depois
  (``estado_saida.mudou_por`` e o motivo do cofre). Fechada porque o cofre estava trancado, reabre na primeira
  conferencia destravada. Fechada porque o cofre nao respondia, espera ``DESTRAVADO_PARA_REABRIR`` conferencias
  seguidas destravadas. Se ela tinha reaberto, depois de outra falta de resposta, menos de ``JANELA_INSTAVEL``
  antes de fechar de novo, espera
  tambem o cofre responder destravado por ``ESPERA_SE_INSTAVEL`` seguidos: rede que vai e volta nao abre e
  fecha a porta (nem manda aviso) a cada poucas conferencias.
- Fechada pelo ADMIN ou pela conferencia do banco na partida: continua fechada, mas o ADMIN fica sabendo do
  cofre. Sai um aviso quando o cofre aparece trancado e outro quando ele volta
  (``cofre_trancado_saida_fechada`` e ``cofre_destravado_saida_fechada`` no extrato, que guardam a hora do
  fechamento a que se referem). Quem fecha de novo a saida ja fechada nao recomeca a conta: a linha
  ``estado_saida_mudou`` guarda o que o ADMIN ja sabia do cofre (``CAMPO_HERDADO``: trancado, sem resposta ou
  nada). Assim, a saida que o cofre fechou e o ADMIN fechou de novo da o aviso de que o cofre voltou, sem repetir
  o de trancado; e, se o cofre so nao respondia, o aviso de trancado ainda sai quando ele aparecer trancado.
- Fechada pelo cofre "sem resposta", e ele volta a responder trancado: a saida continua fechada, o motivo passa a
  "trancado" (vale a regra de reabrir do cofre trancado), com uma linha e um aviso proprios, sem dizer que fechou.

Cada mudanca e gravada numa transacao so, com a trava exclusiva de ``tudo`` (a mesma de fechar a saida) e a
linha do estado relida: a linha ``estado_saida_mudou`` e a frase do cofre no extrato e um aviso ao ADMIN
(``cofre_trancado`` ao fechar ou avisar, ``cofre_destravado`` ao reabrir ou avisar), uma vez por mudanca. O
aviso leva so o texto e o motivo, nada do cofre. As acoes liberadas nao mudam de estado: esperam a saida
reabrir e sao conferidas de novo antes de sair.

Sem um cliente de cofre com ``saude()`` (testes, cifra local), nada disso roda.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.avisos import avisar_admin
from mcp_claro.cofre import CofreTrancado, EstadoCofre
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.regras import estado
from mcp_claro.regras.bloqueios import quem_rotina
from mcp_claro.saida import travas

log = logger("mcp_claro.saida.cofre")

QUEM: Final = quem_rotina("cofre")
"""Autor das mudancas da saida feitas pela conferencia do cofre (``estado_saida.mudou_por``)."""
MOTIVO_TRANCADO: Final = "o cofre esta trancado"
MOTIVO_FORA_DO_AR: Final = "o cofre nao responde"
MOTIVO_REABRIU: Final = "o cofre voltou destravado"
MOTIVOS: Final[frozenset[str]] = frozenset({MOTIVO_TRANCADO, MOTIVO_FORA_DO_AR})
"""Motivos que a conferencia grava ao fechar: so a saida fechada com um deles (e por ``QUEM``) reabre sozinha."""
SEM_RESPOSTA_PARA_FECHAR: Final = 2
"""Conferencias seguidas sem resposta do cofre antes de fechar a saida."""
DESTRAVADO_PARA_REABRIR: Final = 2
"""Conferencias seguidas com o cofre destravado antes de reabrir a saida fechada porque ele nao respondia."""
JANELA_INSTAVEL: Final = 3600.0
"""Segundos: a saida fechou de novo porque o cofre nao respondia, menos disso depois de reaberta pelo cofre."""
ESPERA_SE_INSTAVEL: Final = 1800.0
"""Segundos seguidos de cofre destravado antes de reabrir, quando a saida ja abriu e fechou na ultima hora."""

EVENTO_TRANCADO: Final = "cofre_trancado"
EVENTO_DESTRAVADO: Final = "cofre_destravado"
TIPO_TRANCADO_COM_SAIDA_FECHADA: Final = "cofre_trancado_saida_fechada"
TIPO_DESTRAVADO_COM_SAIDA_FECHADA: Final = "cofre_destravado_saida_fechada"
TIPO_PASSOU_A_TRANCADO: Final = "cofre_passou_a_trancado"
CAMPO_HERDADO: Final = "cofre_trancado_avisado"
"""Na linha ``estado_saida_mudou`` de quem fecha de novo a saida ja fechada: o que o ADMIN sabia do cofre antes
deste fechamento (``avisado_trancado``): ``true`` (trancado), ``null`` (so que nao respondia) ou ``false``."""

_AVISO_FECHOU: Final[dict[str, tuple[str, str]]] = {
    MOTIVO_TRANCADO: (
        "MCP-CLARO: o cofre esta trancado e a porta de saida fechou",
        "O cofre esta trancado. A porta de saida fechou: nenhuma acao sai para os sistemas, e as ja liberadas"
        " esperam. Quando o cofre for destravado, a porta reabre sozinha, se ninguem a mudar antes.",
    ),
    MOTIVO_FORA_DO_AR: (
        "MCP-CLARO: o cofre nao responde e a porta de saida fechou",
        "O cofre nao respondeu em duas conferencias seguidas. A porta de saida fechou: nenhuma acao sai para os"
        " sistemas, e as ja liberadas esperam. Quando o cofre responder destravado em duas conferencias seguidas,"
        " a porta reabre sozinha, se ninguem a mudar antes.",
    ),
}
_AVISO_FECHOU_INSTAVEL: Final = (
    "MCP-CLARO: o cofre nao responde e a porta de saida fechou de novo",
    "O cofre nao respondeu em duas conferencias seguidas. A porta de saida fechou: nenhuma acao sai para os"
    " sistemas, e as ja liberadas esperam. Menos de uma hora antes, ela tinha reaberto depois de outra falta de"
    " resposta do cofre. Por isso so reabre sozinha depois de o cofre responder destravado por"
    f" {int(ESPERA_SE_INSTAVEL // 60)} minutos seguidos, se ninguem a mudar antes. O ADMIN pode reabrir antes.",
)
_AVISO_PASSOU_A_TRANCADO: Final = (
    "MCP-CLARO: o cofre voltou a responder, mas esta trancado",
    "O cofre voltou a responder, mas esta trancado. A porta de saida continua fechada: nenhuma acao sai para os"
    " sistemas. Quando o cofre for destravado, ela reabre sozinha, se ninguem a mudar antes.",
)
_AVISO_REABRIU: Final = (
    "MCP-CLARO: o cofre voltou e a porta de saida reabriu",
    "O cofre voltou destravado. A porta de saida, que a conferencia do cofre tinha fechado, reabriu sozinha. As"
    " acoes que esperavam sao conferidas de novo antes de sair.",
)
_AVISO_TRANCADO_COM_SAIDA_FECHADA: Final = (
    "MCP-CLARO: o cofre esta trancado",
    "O cofre esta trancado. A porta de saida ja estava fechada por outro motivo e continua fechada. Quando o cofre"
    " for destravado, ela nao reabre sozinha. Reabrir e decisao do ADMIN.",
)
_AVISO_DESTRAVADO_COM_SAIDA_FECHADA: Final = (
    "MCP-CLARO: o cofre voltou, e a porta de saida continua fechada",
    "O cofre voltou destravado. A porta de saida continua fechada, porque foi fechada por outro motivo. Reabrir e"
    " decisao do ADMIN.",
)


class SaudeDoCofre(Protocol):
    """O que a conferencia usa do cliente do cofre (``cofre.Cofre``)."""

    async def saude(self) -> EstadoCofre: ...


class Leitura(enum.StrEnum):
    DESTRAVADO = "destravado"
    TRANCADO = "trancado"
    FORA_DO_AR = "fora_do_ar"


class Passo(enum.StrEnum):
    NADA = "nada"
    FECHAR = "fechar"
    REABRIR = "reabrir"
    AVISAR_TRANCADO = "avisar_trancado"
    """A saida ja estava fechada por outro motivo: so a linha do extrato e o aviso de cofre trancado."""
    AVISAR_DESTRAVADO = "avisar_destravado"
    """A saida continua fechada por outro motivo: so a linha do extrato e o aviso de que o cofre voltou."""


@dataclass(frozen=True, slots=True)
class Decisao:
    passo: Passo
    motivo: str | None = None


NADA: Final = Decisao(Passo.NADA)


async def ler(cofre: SaudeDoCofre) -> Leitura:
    """Saude do cofre, sem login. Qualquer falha ao perguntar conta como fora do ar (o log leva so o tipo)."""
    try:
        situacao = await cofre.saude()
    except CofreTrancado:
        return Leitura.TRANCADO
    except Exception as erro:  # noqa: BLE001 - qualquer outra falha e "sem resposta"; a contagem decide quando fechar
        log.warning("cofre_sem_resposta", erro=type(erro).__name__)
        return Leitura.FORA_DO_AR
    return Leitura.DESTRAVADO if situacao.destravado else Leitura.TRANCADO


def fechada_pelo_cofre(saida: estado.EstadoSaida) -> bool:
    return not saida.aberta and saida.mudou_por == QUEM and saida.motivo in MOTIVOS


def fechada_por_outro(saida: estado.EstadoSaida) -> bool:
    return not saida.aberta and not fechada_pelo_cofre(saida)


def instavel(reabriu_ha: float | None) -> bool:
    """A conferencia do cofre reabriu a saida ha menos de ``JANELA_INSTAVEL`` segundos (``None``: nunca reabriu)."""
    return reabriu_ha is not None and reabriu_ha < JANELA_INSTAVEL


def pronta_para_reabrir(destravado_seguidas: int, destravado_ha: float, *, saida_instavel: bool) -> bool:
    """Saida fechada porque o cofre nao respondia: ja pode reabrir?

    ``destravado_seguidas`` conta esta leitura; ``destravado_ha`` e ha quantos segundos a sequencia comecou.
    """
    if destravado_seguidas < DESTRAVADO_PARA_REABRIR:
        return False
    return not saida_instavel or destravado_ha >= ESPERA_SE_INSTAVEL


def decidir(
    leitura: Leitura,
    sem_resposta_seguidas: int,
    saida: estado.EstadoSaida,
    *,
    pronta: bool = True,
    avisado_trancado: bool | None = None,
) -> Decisao:
    """O que fazer com a saida diante da leitura do cofre. ``sem_resposta_seguidas`` ja conta esta leitura.

    ``pronta``: a saida fechada porque o cofre nao respondia ja pode reabrir (``pronta_para_reabrir``).
    ``avisado_trancado``: com a saida fechada por outro motivo, se o ultimo aviso ao ADMIN foi o de cofre trancado
    (``True``), o de cofre de volta ou nenhum (``False``), ou se a porta ainda nao sabe ou o ADMIN so sabe que o
    cofre nao respondia (``None``: avisa tanto o trancado quanto a volta).
    """
    if saida.aberta:
        if leitura is Leitura.TRANCADO:
            return Decisao(Passo.FECHAR, MOTIVO_TRANCADO)
        if leitura is Leitura.FORA_DO_AR and sem_resposta_seguidas >= SEM_RESPOSTA_PARA_FECHAR:
            return Decisao(Passo.FECHAR, MOTIVO_FORA_DO_AR)
        return NADA
    if fechada_pelo_cofre(saida):
        if leitura is Leitura.TRANCADO:
            # Ja fechada pelo cofre "sem resposta": o motivo passa a "trancado" (outro aviso). Ja trancado: nada.
            return Decisao(Passo.FECHAR, MOTIVO_TRANCADO) if saida.motivo != MOTIVO_TRANCADO else NADA
        if leitura is Leitura.DESTRAVADO and (saida.motivo == MOTIVO_TRANCADO or pronta):
            return Decisao(Passo.REABRIR)
        return NADA
    # Fechada por outro motivo (ADMIN, conferencia do banco): o cofre nao muda a saida, so avisa o ADMIN.
    if leitura is Leitura.TRANCADO and avisado_trancado is not True:
        return Decisao(Passo.AVISAR_TRANCADO, MOTIVO_TRANCADO)
    if leitura is Leitura.DESTRAVADO and avisado_trancado is not False:
        return Decisao(Passo.AVISAR_DESTRAVADO, MOTIVO_REABRIU)
    return NADA


async def _ler_para_mudar(c: AsyncConnection) -> estado.EstadoSaida:
    linha = (
        await c.execute(text("SELECT aberta, motivo, mudou_por, mudou_em FROM estado_saida WHERE id = 1 FOR UPDATE"))
    ).first()
    if linha is None:
        return estado.EstadoSaida(False, "estado da saida ausente")
    return estado.EstadoSaida(bool(linha.aberta), linha.motivo, linha.mudou_por, linha.mudou_em)


def _fechamento(saida: estado.EstadoSaida) -> str:
    return saida.mudou_em.isoformat() if saida.mudou_em is not None else "?"


async def avisado_trancado(c: AsyncConnection, saida: estado.EstadoSaida) -> bool | None:
    """O que o ADMIN ja sabe do cofre, com a saida fechada como esta: ``True``, o ultimo aviso foi o de cofre
    trancado; ``False``, foi o de volta, ou nenhum; ``None``, so sabe que o cofre nao respondia (``decidir`` avisa
    tanto o trancado quanto a volta).

    Saida fechada pelo cofre: o aviso de fechar disse "trancado" ou "nao responde". Fechada por outro: le o extrato
    desde o fechamento; sem aviso do cofre neste fechamento, vale o que a linha ``estado_saida_mudou`` dele herdou
    do fechamento anterior (``CAMPO_HERDADO``)."""
    if saida.aberta:
        return False
    if fechada_pelo_cofre(saida):
        return True if saida.motivo == MOTIVO_TRANCADO else None
    # So as linhas desde o fechamento (a particao e o indice do extrato vao pela hora): um fechamento antigo nao
    # conta, e a busca nao varre o extrato inteiro.
    desde = saida.mudou_em or datetime.min.replace(tzinfo=UTC)
    tipo = (
        await c.execute(
            text(
                "SELECT tipo FROM extrato WHERE criado_em >= :desde AND tipo IN (:t, :d)"
                " AND dados->>'fechada_em' = :f ORDER BY criado_em DESC, id DESC LIMIT 1"
            ),
            {
                "desde": desde,
                "t": TIPO_TRANCADO_COM_SAIDA_FECHADA,
                "d": TIPO_DESTRAVADO_COM_SAIDA_FECHADA,
                "f": _fechamento(saida),
            },
        )
    ).scalar_one_or_none()
    if tipo is not None:
        return bool(tipo == TIPO_TRANCADO_COM_SAIDA_FECHADA)
    # A linha do proprio fechamento: com a saida como esta, e a ultima "estado_saida_mudou" desde ele.
    # Indice parcial ``extrato_saida_cofre_idx`` (migracao 0005): a busca nao passa pelas outras linhas.
    herdado = (
        await c.execute(
            text(
                "SELECT jsonb_typeof(dados -> :campo) AS tipo, dados ->> :campo AS valor FROM extrato"
                " WHERE criado_em >= :desde AND tipo = 'estado_saida_mudou' ORDER BY criado_em DESC, id DESC LIMIT 1"
            ),
            {"campo": CAMPO_HERDADO, "desde": desde},
        )
    ).first()
    if herdado is None or herdado.tipo is None:
        return False
    if herdado.tipo == "null":
        return None
    return bool(herdado.valor == "true")


async def aplicar(
    c: AsyncConnection,
    leitura: Leitura,
    sem_resposta_seguidas: int,
    *,
    emails_admin: Sequence[str] = (),
    papel: str = "despachante",
    pronta: bool = True,
    instavel_ao_fechar: bool = False,
) -> tuple[Decisao, estado.EstadoSaida]:
    """Decide de novo com a linha do estado travada e grava a mudanca, na transacao de ``c``.

    Devolve o que fez e a saida como estava antes. Toma antes a trava exclusiva de ``tudo``: um pedido ou envio
    em andamento termina antes, e nenhum comeca no meio da mudanca. ``instavel_ao_fechar``: se fechar porque o
    cofre nao responde, a saida ja tinha aberto e fechado assim na ultima hora (o aviso diz quando reabre).
    """
    await travas.exclusiva(c, [travas.TUDO])
    saida = await _ler_para_mudar(c)
    avisado: bool | None = None
    if fechada_por_outro(saida) and leitura is not Leitura.FORA_DO_AR:
        avisado = await avisado_trancado(c, saida)
    decisao = decidir(leitura, sem_resposta_seguidas, saida, pronta=pronta, avisado_trancado=avisado)
    if decisao.passo is Passo.NADA:
        return decisao, saida
    if decisao.passo is Passo.FECHAR and fechada_pelo_cofre(saida):
        # Ja fechada pelo cofre "sem resposta", e ele voltou a responder trancado: a porta continua fechada, desde a
        # mesma hora. So o motivo muda (para reabrir, vale a regra do cofre trancado), com linha e aviso proprios.
        await c.execute(text("UPDATE estado_saida SET motivo = :m WHERE id = 1"), {"m": MOTIVO_TRANCADO})
        await registrar(c, papel=papel, tipo=TIPO_PASSOU_A_TRANCADO, dados={"fechada_em": _fechamento(saida)})
        assunto, texto_aviso = _AVISO_PASSOU_A_TRANCADO
        await avisar_admin(
            c,
            EVENTO_TRANCADO,
            assunto,
            {"texto": texto_aviso, "motivo": MOTIVO_TRANCADO},
            f"saida-cofre:{EVENTO_TRANCADO}:passou:{_fechamento(saida)}",
            emails_admin,
        )
        log.warning("saida_pelo_cofre", passo="passou_a_trancado", leitura=str(leitura))
        return Decisao(Passo.FECHAR, MOTIVO_TRANCADO), saida
    if decisao.passo is Passo.FECHAR:
        motivo = decisao.motivo or MOTIVO_TRANCADO
        await estado.mudar_saida(c, aberta=False, quem=QUEM, motivo=motivo, papel=papel)
        await registrar(c, papel=papel, tipo="cofre_fechou_saida", campos={"motivo": motivo}, dados={"motivo": motivo})
        evento = EVENTO_TRANCADO
        if motivo == MOTIVO_FORA_DO_AR and instavel_ao_fechar:
            assunto, texto_aviso = _AVISO_FECHOU_INSTAVEL
        else:
            assunto, texto_aviso = _AVISO_FECHOU[motivo]
    elif decisao.passo is Passo.REABRIR:
        motivo = MOTIVO_REABRIU
        await estado.mudar_saida(c, aberta=True, quem=QUEM, motivo=motivo, papel=papel)
        await registrar(c, papel=papel, tipo="cofre_reabriu_saida")
        evento = EVENTO_DESTRAVADO
        assunto, texto_aviso = _AVISO_REABRIU
    else:
        # A saida continua como esta: so o extrato e o aviso, um por fechamento e por volta do cofre.
        trancado = decisao.passo is Passo.AVISAR_TRANCADO
        motivo = MOTIVO_TRANCADO if trancado else MOTIVO_REABRIU
        linha = await registrar(
            c,
            papel=papel,
            tipo=TIPO_TRANCADO_COM_SAIDA_FECHADA if trancado else TIPO_DESTRAVADO_COM_SAIDA_FECHADA,
            dados={"fechada_em": _fechamento(saida)},
        )
        evento = EVENTO_TRANCADO if trancado else EVENTO_DESTRAVADO
        assunto, texto_aviso = _AVISO_TRANCADO_COM_SAIDA_FECHADA if trancado else _AVISO_DESTRAVADO_COM_SAIDA_FECHADA
        await avisar_admin(
            c,
            evento,
            assunto,
            {"texto": texto_aviso, "motivo": motivo},
            f"saida-cofre:{evento}:extrato:{linha}",
            emails_admin,
        )
        log.warning("cofre_com_saida_fechada", passo=str(decisao.passo), leitura=str(leitura))
        return Decisao(decisao.passo, motivo), saida
    # Uma mudanca, um aviso: a chave e a hora da mudanca gravada nesta transacao.
    mudou_em: datetime = (await c.execute(text("SELECT mudou_em FROM estado_saida WHERE id = 1"))).scalar_one()
    await avisar_admin(
        c,
        evento,
        assunto,
        {"texto": texto_aviso, "motivo": motivo},
        f"saida-cofre:{evento}:{mudou_em.isoformat()}",
        emails_admin,
    )
    log.warning("saida_pelo_cofre", passo=str(decisao.passo), leitura=str(leitura))
    return Decisao(decisao.passo, motivo), saida
