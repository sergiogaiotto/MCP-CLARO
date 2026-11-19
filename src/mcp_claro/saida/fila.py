"""Porta unica de saida no banco: operacoes, fichas, limites e a fila.

Cada funcao grava na transacao do chamador e deixa uma linha no extrato. As mudancas de estado sempre
dizem o estado de partida (``UPDATE ... WHERE estado = ...``): quem chega depois nao muda nada e recebe
a recusa com o estado atual. O banco tem a mesma tabela de transicoes (``transicoes_saida``) como
segunda barreira.

Ordem das travas, para nunca haver impasse: consultivas de alcance (ver ``travas``), depois a linha da
ficha, depois a linha da fila. Quem nao precisa de uma delas simplesmente pula. Quem trava varias linhas
da fila de uma vez (bloqueio, restauracao, retomada, vencimento) trava em ordem de ``id``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.modelos import FerramentaVitrine
from mcp_claro.extrato import registrar
from mcp_claro.regras.bloqueios import Bloqueio, quem_rotina
from mcp_claro.saida import travas
from mcp_claro.saida.estados import ANTES_DE_SAIR, FRASE, Estado
from mcp_claro.saida.protocolo import FUSO, chave_pedido_igual, codigo_conferencia, novo_protocolo

JA_SAIRAM: Final = (Estado.ENVIANDO, Estado.ENVIADA, Estado.NAO_CONFIRMADA, Estado.SISTEMA_PEDIU_DADOS)
QUEM_TETO: Final = quem_rotina("teto_confirmacoes")
"""Quem liga a pausa do teto de fichas por hora (``CONFIRM_CAP_PER_HOUR``)."""
SALTO_NA_RESTAURACAO: Final = 1_000_000
"""Quanto a sequencia do protocolo anda para frente quando o banco volta de uma copia."""
COMO_CONFIRMOU: Final = {"sessao": "sem passkey", "passkey": "com passkey", "login_refeito": "com o login refeito"}
DECISOES_NAO_CONFIRMADA: Final = {
    "aconteceu": "aconteceu no sistema",
    "nao_aconteceu": "nao aconteceu no sistema",
    "nao_sei": "nao sabe ainda",
}


class DecisaoRecusada(Exception):
    """Pedido da pessoa que nao muda nada: ficha ja decidida, acao que ja saiu, passkey que faltou."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


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

    @property
    def antes_de_sair(self) -> bool:
        return self.estado in ANTES_DE_SAIR

    def conta_como_repeticao(self) -> bool:
        """Um pedido igual devolve esta operacao se ela pode ter efeito no sistema ou ainda pode sair."""
        if self.estado in (Estado.AGUARDANDO_FICHA, Estado.LIBERADA, Estado.ENVIANDO, Estado.ENVIADA):
            return True
        return self.estado == Estado.NAO_CONFIRMADA and self.decisao_pessoa != "nao_aconteceu"


_SQL_OPERACAO = """
SELECT o.id, o.protocolo, o.codigo_conferencia, o.pessoa_id, o.aplicativo_id, o.ordem_id, o.ferramenta_id,
       o.sistema_id, o.argumentos, o.aplicativo_diz, o.resultado, o.desfecho, o.criada_em,
       f.id AS fila_id, f.estado, f.ficha_id, f.limite_id, f.hora_saida, f.enviada_em, f.mudou_em, f.mudou_por,
       r.nome_vitrine, s.nome AS sistema_nome, d.decisao AS decisao_pessoa,
       fi.expira_em AS ficha_expira_em, fi.exige_passkey AS ficha_exige_passkey, fi.risco AS ficha_risco,
       fi.decisao AS ficha_decisao
FROM operacoes o
JOIN fila_saida f ON f.operacao_id = o.id
JOIN ferramentas_revisadas r ON r.id = o.ferramenta_id
JOIN sistemas s ON s.id = o.sistema_id
LEFT JOIN fichas fi ON fi.id = f.ficha_id
LEFT JOIN decisoes_nao_confirmada d ON d.operacao_id = o.id
"""


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


# Limites por aproximacao


def cobre(destinatarios: Any, argumentos: dict[str, Any]) -> bool:
    """Lista vazia: qualquer destinatario. Cada item ``{"campo", "valores"}`` exige o argumento entre os valores.

    Item com ``valores`` vazio quer dizer "este campo tem de vir ausente, vazio ou lista vazia": e assim que um
    limite "so estes destinatarios" fecha os outros campos de destinatario da ferramenta (copia, copia oculta).
    """
    itens = _json(destinatarios) or []
    if not isinstance(itens, list):
        return False
    for item in itens:
        if not isinstance(item, dict):
            return False
        campo, valores = item.get("campo"), item.get("valores")
        if not isinstance(campo, str) or not isinstance(valores, list):
            return False
        valor = argumentos.get(campo)
        if not valores:
            if valor is None or (isinstance(valor, str | list) and len(valor) == 0):
                continue
            return False
        pedidos = valor if isinstance(valor, list) else [valor]
        if not pedidos or any(not isinstance(v, str) or v not in valores for v in pedidos):
            return False
    return True


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
    for linha in candidatos:
        if not cobre(linha.destinatarios, argumentos):
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
    """Por onde a acao passa: ficha (com ou sem passkey) ou limite ja gasto."""

    limite_id: uuid.UUID | None = None
    exige_passkey: bool = False
    motivo: str = "ficha"

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
) -> Operacao:
    protocolo = await novo_protocolo(c)
    codigo = codigo_conferencia(ferramenta.sistema.id, ferramenta.nome_original, argumentos)
    operacao_id = (
        await c.execute(
            text(
                "INSERT INTO operacoes (protocolo, codigo_conferencia, pessoa_id, aplicativo_id, ordem_id,"
                " ferramenta_id, sistema_id, argumentos, aplicativo_diz, chave_pedido_igual)"
                " VALUES (:pr, :co, :pe, :a, :o, :f, :s, CAST(:ar AS jsonb), :diz, :k) RETURNING id"
            ),
            {
                "pr": protocolo,
                "co": codigo,
                "pe": pessoa_id,
                "a": aplicativo_id,
                "o": ordem_id,
                "f": ferramenta.id,
                "s": ferramenta.sistema.id,
                "ar": json.dumps(argumentos, ensure_ascii=False),
                "diz": aplicativo_diz,
                "k": chave_pedido_igual(pessoa_id, aplicativo_id, ferramenta.id, argumentos),
            },
        )
    ).scalar_one()
    if rota.por_limite:
        await c.execute(
            text(
                "INSERT INTO fila_saida (operacao_id, estado, limite_id, hora_saida, mudou_por)"
                " VALUES (:o, 'liberada', :l, now() + make_interval(secs => :e), 'entrada')"
            ),
            {"o": operacao_id, "l": rota.limite_id, "e": espera_segundos},
        )
    else:
        ficha_id = (
            await c.execute(
                text(
                    "INSERT INTO fichas (operacao_id, codigo_conferencia, expira_em, risco, exige_passkey)"
                    " VALUES (:o, :co, now() + make_interval(mins => :m), :r, :pk) RETURNING id"
                ),
                {
                    "o": operacao_id,
                    "co": codigo,
                    "m": prazo_ficha_minutos,
                    "r": ferramenta.risco,
                    "pk": rota.exige_passkey,
                },
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO fila_saida (operacao_id, estado, ficha_id, mudou_por)"
                " VALUES (:o, 'aguardando_ficha', :fi, 'entrada')"
            ),
            {"o": operacao_id, "fi": ficha_id},
        )
    await registrar(
        c,
        papel="entrada",
        tipo="acao_pedida",
        campos={"ferramenta": ferramenta.nome_vitrine, "protocolo": protocolo},
        protocolo=protocolo,
        aplicativo_id=aplicativo_id,
        pessoa_id=pessoa_id,
        sistema_id=ferramenta.sistema.id,
        ferramenta=ferramenta.nome_vitrine,
        dados={"rota": rota.motivo, "risco": ferramenta.risco, "passkey": rota.exige_passkey},
        trace_id=trace_id,
    )
    op = _existe(await por_id(c, operacao_id))
    return op


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
) -> Operacao:
    """Confirma a ficha e libera a acao com a espera visivel. A ficha vale uma vez, so para esta operacao.

    Com a passkey ligada (``passkey_ligada``, de ``FEATURE_PASSKEY``), a ficha que pede passkey so confirma com
    ela. Com a passkey desligada (inclusive a ficha montada antes, quando pedia passkey), a acao de risco alto
    confirma com a passkey ou com o login refeito (``login_refeito``). O banco guarda como foi
    (``confirmada_com``) e recusa a ficha de risco alto confirmada so com a sessao.
    """
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
    await c.execute(
        text(
            "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), decidida_por = :p, com_passkey = :pk,"
            " segundos_ate_decidir = :s, confirmada_com = :cc WHERE id = :f AND decisao IS NULL"
        ),
        {"f": ficha_id, "p": pessoa_id, "pk": com_passkey, "s": f.segundos, "cc": confirmada_com},
    )
    await registrar(
        c,
        papel="entrada",
        tipo="ficha_confirmada",
        campos={"protocolo": f.protocolo, "como": COMO_CONFIRMOU[confirmada_com]},
        protocolo=f.protocolo,
        pessoa_id=pessoa_id,
        aplicativo_id=f.aplicativo_id,
        sistema_id=f.sistema_id,
        dados={"rapida_demais": float(f.segundos) < rapida_segundos, "segundos": round(float(f.segundos), 1)},
    )
    op = _existe(await por_id(c, f.operacao_id))
    return op


async def recusar_ficha(c: AsyncConnection, ficha_id: uuid.UUID, *, pessoa_id: uuid.UUID) -> Operacao:
    f = await _ficha_travada(c, ficha_id, pessoa_id)
    await c.execute(
        text(
            "UPDATE fila_saida SET estado = 'cancelada', mudou_por = 'pessoa'"
            " WHERE operacao_id = :o AND estado = 'aguardando_ficha'"
        ),
        {"o": f.operacao_id},
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
    c: AsyncConnection, operacao_id: uuid.UUID, *, pessoa_id: uuid.UUID, decisao: str, observacao: str | None
) -> Operacao:
    """Registra o que a pessoa apurou sobre um resultado nao confirmado. A acao nunca e reenviada."""
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
    atual = _existe(await por_id(c, operacao_id))
    return atual


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
    c: AsyncConnection, op: Operacao, estado: Estado, resultado: dict[str, Any] | None, desfecho: str
) -> bool:
    """Grava o fim do envio. Se outra copia ja deu a acao como nao confirmada, so guarda a resposta."""
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
    await c.execute(
        text("UPDATE operacoes SET resultado = CAST(:r AS jsonb), desfecho = :d WHERE id = :o"),
        {
            "o": op.id,
            "r": json.dumps(resultado, ensure_ascii=False) if resultado is not None else None,
            "d": desfecho if mudou is not None else f"{desfecho} (resposta depois da retomada)",
        },
    )
    tipo = _TIPO_FINAL[estado]
    campos: dict[str, object] = {"protocolo": op.protocolo}
    if tipo == "acao_enviada":
        campos["ferramenta"] = op.nome_vitrine
    elif tipo == "acao_nao_saiu":
        campos["motivo"] = desfecho
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
        dados={"desfecho": desfecho[:120], "gravado": mudou is not None},
    )
    return mudou is not None


async def retomar_enviando(c: AsyncConnection, *, quem: str = "despachante") -> list[str]:
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

    ``informado`` fica para o que um sistema de nivel 3 relatar (Fase 4): vazio na Fase 2.
    """

    bloqueio_id: uuid.UUID
    paradas: list[dict[str, Any]]
    ja_saidas: list[dict[str, Any]]
    informado: list[dict[str, Any]]
    em_conferencia: list[dict[str, Any]] = field(default_factory=list)


async def parar_por_bloqueio(c: AsyncConnection, b: Bloqueio, *, papel: str, janela_horas: int = 24) -> Comprovante:
    """Na transacao que liga o bloqueio (com a trava exclusiva do alcance ja tomada): para o que ainda nao
    saiu e monta o comprovante em tres partes. As linhas paradas sao travadas em ordem de ``id``."""
    filtro, valores = _filtro_alcance(b)
    paradas = (
        await c.execute(
            text(
                "WITH alvo AS (SELECT f.id FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"  # noqa: S608
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
                "SELECT o.protocolo, f.estado, f.enviada_em, f.mudou_em, d.decisao FROM fila_saida f"  # noqa: S608
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


async def comprovante_do_bloqueio(c: AsyncConnection, bloqueio_id: uuid.UUID) -> Comprovante | None:
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
    return Comprovante(
        linha.bloqueio_id,
        _json(linha.paradas),
        _json(linha.ja_saidas),
        _json(linha.informado),
        _json(linha.em_conferencia),
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
    await c.execute(
        text("SELECT setval('protocolo_seq', (SELECT last_value FROM protocolo_seq) + :n)"),
        {"n": SALTO_NA_RESTAURACAO},
    )
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
