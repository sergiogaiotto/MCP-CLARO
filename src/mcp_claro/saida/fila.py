"""Porta unica de saida no banco: operacoes, fichas, limites e a fila.

Cada funcao grava na transacao do chamador e deixa uma linha no extrato. As mudancas de estado sempre
dizem o estado de partida (``UPDATE ... WHERE estado = ...``): quem chega depois nao muda nada e recebe
a recusa com o estado atual. O banco tem a mesma tabela de transicoes (``transicoes_saida``) como
segunda barreira.

Ordem das travas, para nunca haver impasse: consultivas de alcance (ver ``travas``), depois a linha da
ficha, depois a linha da fila. Quem nao precisa de uma delas simplesmente pula.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.cadastro.modelos import FerramentaVitrine
from mcp_claro.extrato import registrar
from mcp_claro.regras.bloqueios import Bloqueio
from mcp_claro.saida import travas
from mcp_claro.saida.estados import ANTES_DE_SAIR, FRASE, Estado
from mcp_claro.saida.protocolo import FUSO, chave_pedido_igual, codigo_conferencia, novo_protocolo

JA_SAIRAM: Final = (Estado.ENVIANDO, Estado.ENVIADA, Estado.NAO_CONFIRMADA, Estado.SISTEMA_PEDIU_DADOS)
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
    linhas = (
        await c.execute(
            text(
                _SQL_OPERACAO + " WHERE o.chave_pedido_igual = :k AND o.criada_em > now() - make_interval(mins => :j)"
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
    """Lista vazia: qualquer destinatario. Cada item ``{"campo", "valores"}`` exige o argumento entre os valores."""
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
) -> uuid.UUID | None:
    """Gasta uma vez de um limite que cobre o pedido. O UPDATE so passa com saldo: nunca passa do teto."""
    candidatos = (
        await c.execute(
            text(
                "SELECT id, destinatarios FROM limites WHERE pessoa_id = :p AND aplicativo_id = :a"
                " AND sistema_id = :s AND nome_original = :n AND situacao = 'ativo' AND valido_ate > now()"
                " ORDER BY valido_ate, id"
            ),
            {"p": pessoa_id, "a": aplicativo_id, "s": sistema_id, "n": nome_original},
        )
    ).all()
    for linha in candidatos:
        if not cobre(linha.destinatarios, argumentos):
            continue
        gasto = (
            await c.execute(
                text(
                    "UPDATE limites SET"
                    " usados_hoje = CASE WHEN dia = (now() AT TIME ZONE :fuso)::date THEN usados_hoje + 1 ELSE 1 END,"
                    " dia = (now() AT TIME ZONE :fuso)::date"
                    " WHERE id = :i AND situacao = 'ativo' AND valido_ate > now()"
                    " AND (CASE WHEN dia = (now() AT TIME ZONE :fuso)::date THEN usados_hoje ELSE 0 END) < por_dia"
                    " RETURNING id"
                ),
                {"i": linha.id, "fuso": FUSO},
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


async def fichas_na_ultima_hora(c: AsyncConnection, aplicativo_id: uuid.UUID) -> int:
    return int(
        (
            await c.execute(
                text(
                    "SELECT count(*) FROM fichas fi JOIN operacoes o ON o.id = fi.operacao_id"
                    " WHERE o.aplicativo_id = :a AND fi.montada_em > now() - interval '1 hour'"
                ),
                {"a": aplicativo_id},
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
    linha = (
        await c.execute(
            text(
                "SELECT fi.id, fi.operacao_id, fi.codigo_conferencia, fi.expira_em <= now() AS vencida,"
                " fi.exige_passkey, fi.decisao, o.pessoa_id, o.protocolo, o.aplicativo_id, o.sistema_id,"
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
) -> Operacao:
    """Confirma a ficha e libera a acao com a espera visivel. A ficha vale uma vez, so para esta operacao."""
    f = await _ficha_travada(c, ficha_id, pessoa_id)
    if f.exige_passkey and not com_passkey:
        raise DecisaoRecusada("falta_passkey", "Esta acao e de risco alto: confirme com a sua passkey.")
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
            " segundos_ate_decidir = :s WHERE id = :f"
        ),
        {"f": ficha_id, "p": pessoa_id, "pk": com_passkey, "s": f.segundos},
    )
    await registrar(
        c,
        papel="entrada",
        tipo="ficha_confirmada",
        campos={"protocolo": f.protocolo, "como": "com passkey" if com_passkey else "sem passkey"},
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
            " segundos_ate_decidir = :s WHERE id = :f"
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
                "UPDATE fila_saida f SET estado = 'nao_confirmada', mudou_por = :q FROM operacoes o"
                " WHERE f.operacao_id = o.id AND f.estado = 'enviando'"
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
    """Comprovante de bloqueio em tres partes."""

    bloqueio_id: uuid.UUID
    paradas: list[dict[str, Any]]
    ja_saidas: list[dict[str, Any]]
    informado: list[dict[str, Any]]


async def parar_por_bloqueio(c: AsyncConnection, b: Bloqueio, *, papel: str, janela_horas: int = 24) -> Comprovante:
    """Na transacao que liga o bloqueio (com a trava exclusiva do alcance ja tomada): para o que ainda nao
    saiu e monta o comprovante. O que ja saiu aparece como saido, com o que o sistema respondeu."""
    filtro, valores = _filtro_alcance(b)
    paradas = (
        await c.execute(
            text(
                "UPDATE fila_saida f SET estado = 'parada_por_bloqueio', mudou_por = :q FROM operacoes o"  # noqa: S608
                f" WHERE f.operacao_id = o.id AND f.estado IN ('aguardando_ficha', 'liberada') AND {filtro}"
                " RETURNING o.id, o.protocolo, old.estado AS antes, o.pessoa_id, o.aplicativo_id, o.sistema_id"
            ),
            {"q": f"bloqueio:{b.id}", **valores},
        )
    ).all()
    for linha in paradas:
        await c.execute(
            text("UPDATE operacoes SET desfecho = :d WHERE id = :o AND desfecho IS NULL"),
            {"o": linha.id, "d": b.motivo_recusa()},
        )
    saidas = (
        await c.execute(
            text(
                "SELECT o.protocolo, f.estado, f.enviada_em, o.desfecho FROM fila_saida f"  # noqa: S608
                f" JOIN operacoes o ON o.id = f.operacao_id WHERE {filtro}"
                " AND f.estado IN ('enviando', 'enviada', 'nao_confirmada', 'sistema_pediu_dados')"
                " AND o.criada_em > now() - make_interval(hours => :h) ORDER BY o.criada_em"
            ),
            {"h": janela_horas, **valores},
        )
    ).all()
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
        ],
        informado=[
            {"protocolo": s.protocolo, "o_sistema_informou": s.desfecho or "ainda sem resposta"} for s in saidas
        ],
    )
    await c.execute(
        text(
            "INSERT INTO comprovantes (bloqueio_id, paradas, ja_saidas, informado)"
            " VALUES (:b, CAST(:p AS jsonb), CAST(:j AS jsonb), CAST(:i AS jsonb))"
        ),
        {
            "b": b.id,
            "p": json.dumps(comprovante.paradas),
            "j": json.dumps(comprovante.ja_saidas),
            "i": json.dumps(comprovante.informado, ensure_ascii=False),
        },
    )
    await registrar(
        c,
        papel=papel,
        tipo="comprovante_bloqueio",
        campos={"paradas": len(comprovante.paradas), "ja_saidas": len(comprovante.ja_saidas)},
        pessoa_id=b.pessoa_id,
        aplicativo_id=b.aplicativo_id,
        sistema_id=b.sistema_id,
        dados={"bloqueio_id": str(b.id)},
    )
    return comprovante


async def comprovante_do_bloqueio(c: AsyncConnection, bloqueio_id: uuid.UUID) -> Comprovante | None:
    linha = (
        await c.execute(
            text("SELECT bloqueio_id, paradas, ja_saidas, informado FROM comprovantes WHERE bloqueio_id = :b"),
            {"b": bloqueio_id},
        )
    ).first()
    if linha is None:
        return None
    return Comprovante(linha.bloqueio_id, _json(linha.paradas), _json(linha.ja_saidas), _json(linha.informado))


async def parar_por_restauracao(c: AsyncConnection, *, papel: str = "despachante") -> tuple[int, int]:
    """Banco restaurado: o que estava "enviando" fica nao confirmado; o que esperava para por restauracao.

    Nada sai sem nova confirmacao. Devolve (paradas, nao confirmadas)."""
    duvida = await retomar_enviando(c, quem="restauracao")
    paradas = (
        await c.execute(
            text(
                "UPDATE fila_saida f SET estado = 'parada_por_restauracao', mudou_por = 'restauracao'"
                " FROM operacoes o WHERE f.operacao_id = o.id AND f.estado IN ('aguardando_ficha', 'liberada')"
                " RETURNING o.id"
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
    )
    return len(paradas), len(duvida)


async def vencer_fichas(c: AsyncConnection, *, papel: str = "rotinas") -> list[str]:
    """Fichas sem resposta no prazo: vencem, e nada segue."""
    linhas = (
        await c.execute(
            text(
                "SELECT fi.id, fi.operacao_id FROM fichas fi WHERE fi.decisao IS NULL AND fi.expira_em <= now()"
                " ORDER BY fi.expira_em FOR UPDATE OF fi SKIP LOCKED"
            )
        )
    ).all()
    vencidas: list[str] = []
    for linha in linhas:
        mudou = (
            await c.execute(
                text(
                    "UPDATE fila_saida SET estado = 'vencida', mudou_por = 'rotina:vencimento'"
                    " WHERE operacao_id = :o AND estado = 'aguardando_ficha' RETURNING id"
                ),
                {"o": linha.operacao_id},
            )
        ).scalar_one_or_none()
        await c.execute(
            text("UPDATE fichas SET decisao = 'vencida', decidida_em = now() WHERE id = :f"), {"f": linha.id}
        )
        if mudou is None:
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
