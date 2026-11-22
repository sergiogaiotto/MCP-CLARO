"""Renovacao das chaves por pessoa: antes de usar, uma por vez, gravando a nova antes do uso.

- Chave sem prazo conhecido, ou que vence depois de ``MARGEM``, e usada como esta (caminho rapido,
  sem trava).
- Chave vencida ou perto de vencer: o despachante pega a trava consultiva do par (pessoa, sistema)
  (``autorizacao.travar_com_prazo``), le de novo (outro pedido pode ter renovado enquanto ele esperava) e so
  entao troca a chave de renovacao no ``token_endpoint``. A chave nova e gravada, cifrada, na mesma
  transacao, antes de qualquer envio ao sistema. Duas renovacoes ao mesmo tempo geram uma troca so, se a
  segunda chegar a trava dentro da espera curta.
- A espera pela trava e curta (``autorizacao.ESPERA_TRAVA_RENOVACAO``): quem a segura pode estar esperando um
  servidor de autorizacao lento, ate ``autorizacao.PRAZO_TROCA``. Sem a trava no prazo, a transacao fecha e a
  conexao volta ao pool, sem nada enviado. Como quando o servidor nao responde: se a chave atual ainda vale (lida de
  novo, sem a trava), ela e usada; senao, ``RenovacaoIndisponivel("trava_ocupada")``.
- O servidor de autorizacao recusou a renovacao (``invalid_grant``): as chaves da pessoa naquele
  sistema sao apagadas e ela precisa autorizar de novo (``AutorizacaoVencida``).
- O servidor nao respondeu, inclusive no prazo total da troca (``autorizacao.PRAZO_TROCA``): se a chave atual
  ainda vale, ela e usada; senao, ``RenovacaoIndisponivel``.

A troca acontece de dentro da trava e da transacao: cada renovacao de um servidor lento segura uma conexao do
despachante (e, na porta de saida, a porta unica) por ate ``PRAZO_TROCA`` segundos, mais a espera da trava.

Nunca ha reenvio do ``tools/call``: renova-se antes de enviar. Um 401 no envio e recusa definitiva
daquele pedido; ``marcar_vencida`` so faz o proximo pedido renovar antes.

Servidor de terceiros (Fase 3): o ``token_endpoint`` passa antes pela conferencia do endereco combinado
(``autorizacao.trocar(..., terceiro=True)``). Recusado, nada sai, a falha vai ao extrato com
``sala_endereco_recusado``, e a chave atual so serve enquanto valer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.despachante import autorizacao, chaves
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES

log = logger("mcp_claro.despachante.renovacao")

MARGEM: Final = timedelta(seconds=60)
"""Chave que vence antes disso e renovada antes do uso."""


class AutorizacaoVencida(Exception):
    """A chave venceu e nao ha como renovar: a pessoa precisa autorizar de novo."""


class RenovacaoIndisponivel(Exception):
    """A chave atual ja venceu e nao deu para renovar: o servidor de autorizacao nao respondeu, ou outra renovacao do
    mesmo par segura a trava por mais que a espera curta (``"trava_ocupada"``). Tente de novo mais tarde."""


class _SemTrava(Exception):
    """Interna: a trava do par nao veio na espera curta. Sai do bloco da transacao, que desfaz e devolve a conexao."""


@dataclass(frozen=True, slots=True)
class ChaveAberta:
    segredo: str
    cifrada: str
    renovada: bool = False


def _vale(expira_em: datetime | None, margem: timedelta, agora: datetime) -> bool:
    return expira_em is None or expira_em > agora + margem


async def _ler(c: AsyncConnection, sistema_id: uuid.UUID, pessoa_id: uuid.UUID, tipo: str) -> Row[Any] | None:
    return (
        await c.execute(
            text(
                "SELECT cifrada, expira_em FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p "
                "AND aplicativo_id IS NULL AND tipo = :t"
            ),
            {"s": sistema_id, "p": pessoa_id, "t": tipo},
        )
    ).first()


async def _abrir(
    cifra: Cifra, linha: Row[Any], sistema_id: uuid.UUID, pessoa_id: uuid.UUID, tipo: str = "acesso"
) -> str:
    return (await cifra.decifrar(linha.cifrada, chaves.contexto(sistema_id, pessoa_id, None, tipo))).decode()


async def chave_pronta(
    engine: AsyncEngine,
    cifra: Cifra,
    *,
    sistema_id: uuid.UUID,
    pessoa_id: uuid.UUID,
    margem: timedelta = MARGEM,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> ChaveAberta:
    """Chave de acesso da pessoa no sistema, renovada antes se estiver vencida ou perto de vencer.

    Levanta ``chaves.ChaveAusente`` (nunca autorizou ou foi derrubada), ``AutorizacaoVencida`` ou
    ``RenovacaoIndisponivel``. ``ambiente`` e ``liberar_locais`` (so desenvolvimento e teste) valem para a
    conferencia do endereco de troca de um terceiro.
    """
    async with engine.connect() as c:
        linha = await _ler(c, sistema_id, pessoa_id, "acesso")
    if linha is None:
        raise chaves.ChaveAusente("sem chave guardada para este sistema e esta pessoa")
    if _vale(linha.expira_em, margem, datetime.now(UTC)):
        return ChaveAberta(await _abrir(cifra, linha, sistema_id, pessoa_id), linha.cifrada)
    return await renovar(
        engine,
        cifra,
        sistema_id=sistema_id,
        pessoa_id=pessoa_id,
        margem=margem,
        ambiente=ambiente,
        liberar_locais=liberar_locais,
    )


async def renovar(
    engine: AsyncEngine,
    cifra: Cifra,
    *,
    sistema_id: uuid.UUID,
    pessoa_id: uuid.UUID,
    margem: timedelta = MARGEM,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> ChaveAberta:
    """Renova com a trava do par (pessoa, sistema), esperada no maximo ``autorizacao.ESPERA_TRAVA_RENOVACAO``.

    Quem chegar a trava dentro dessa espera usa a chave que o primeiro gravou. Quem nao chegar desiste depois do
    ROLLBACK, sem segurar a conexao e sem nada enviado: le de novo a chave de acesso, sem a trava, e a usa se ela
    ainda vale (sem a ``margem``); senao, ``RenovacaoIndisponivel("trava_ocupada")``. Nos dois casos, o contador
    ``renovacoes_falhas{motivo="trava_ocupada"}``.
    """
    try:
        async with engine.begin() as c:
            desfecho = await _renovar_na_transacao(
                c, cifra, sistema_id, pessoa_id, margem, ambiente=ambiente, liberar_locais=liberar_locais
            )
    except _SemTrava:
        CONTADORES.somar("renovacoes_falhas", motivo="trava_ocupada")
        atual = await _atual_sem_trava(engine, cifra, sistema_id, pessoa_id)
        log.warning("renovacao_sem_trava", usou_a_chave_atual=atual is not None)
        if atual is not None:
            return atual
        raise RenovacaoIndisponivel("trava_ocupada") from None
    if isinstance(desfecho, Exception):
        CONTADORES.somar("renovacoes_falhas", motivo=type(desfecho).__name__)
        raise desfecho
    return desfecho


async def _atual_sem_trava(
    engine: AsyncEngine, cifra: Cifra, sistema_id: uuid.UUID, pessoa_id: uuid.UUID
) -> ChaveAberta | None:
    """Sem a trava: a chave de acesso de agora, se ainda vale (sem margem). Le sem trava e devolve a conexao antes de
    decifrar. Vencida, marcada vencida depois de um 401 ou apagada: ``None``."""
    async with engine.connect() as c:
        linha = await _ler(c, sistema_id, pessoa_id, "acesso")
    if linha is None or not _vale(linha.expira_em, timedelta(0), datetime.now(UTC)):
        return None
    return ChaveAberta(await _abrir(cifra, linha, sistema_id, pessoa_id), linha.cifrada)


async def _renovar_na_transacao(
    c: AsyncConnection,
    cifra: Cifra,
    sistema_id: uuid.UUID,
    pessoa_id: uuid.UUID,
    margem: timedelta,
    *,
    ambiente: str,
    liberar_locais: bool,
) -> ChaveAberta | Exception:
    """Pega a trava (espera curta; sem ela, ``_SemTrava``), le de novo e, se ainda precisa, troca a chave."""
    if not await autorizacao.travar_com_prazo(c, pessoa_id, sistema_id):
        raise _SemTrava
    acesso = await _ler(c, sistema_id, pessoa_id, "acesso")
    if acesso is None:
        raise chaves.ChaveAusente("a autorizacao foi derrubada")
    agora = datetime.now(UTC)
    if _vale(acesso.expira_em, margem, agora):
        return ChaveAberta(await _abrir(cifra, acesso, sistema_id, pessoa_id), acesso.cifrada)
    atual: ChaveAberta | None = None
    if _vale(acesso.expira_em, timedelta(0), agora):
        atual = ChaveAberta(await _abrir(cifra, acesso, sistema_id, pessoa_id), acesso.cifrada)
    return await _trocar_com_trava(
        c, cifra, sistema_id, pessoa_id, atual, ambiente=ambiente, liberar_locais=liberar_locais
    )


async def _trocar_com_trava(
    c: AsyncConnection,
    cifra: Cifra,
    sistema_id: uuid.UUID,
    pessoa_id: uuid.UUID,
    atual: ChaveAberta | None,
    *,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> ChaveAberta | Exception:
    """Ja dentro da trava. Devolve a chave a usar ou a falha a levantar depois do COMMIT (o extrato fica)."""
    renovacao = await _ler(c, sistema_id, pessoa_id, "renovacao")
    if renovacao is None:
        if atual is not None:
            return atual
        await _derrubar(c, sistema_id, pessoa_id, "a chave venceu e nao ha como renovar")
        return AutorizacaoVencida("a chave venceu e nao ha chave de renovacao")
    try:
        sistema = await autorizacao.carregar_sistema(c, sistema_id)
        segredo = await autorizacao.abrir_segredo_cliente(c, cifra, sistema_id) if sistema.config.usa_segredo else None
    except autorizacao.AutorizacaoRecusada as recusa:
        nome = await _nome(c, sistema_id)
        await _registrar_falha(c, sistema_id, pessoa_id, nome, "a autorizacao do sistema esta mal configurada")
        return atual or RenovacaoIndisponivel(recusa.motivo)
    try:
        resposta = await autorizacao.trocar(
            sistema.config,
            segredo,
            {
                "grant_type": "refresh_token",
                "refresh_token": await _abrir(cifra, renovacao, sistema_id, pessoa_id, "renovacao"),
                "resource": sistema.recurso,
            },
            terceiro=sistema.de_terceiro,
            ambiente=ambiente,
            liberar_locais=liberar_locais,
        )
    except autorizacao.TrocaRecusada as recusa:
        log.warning("renovacao_recusada", sistema=sistema.nome, erro_oauth=recusa.codigo)
        if recusa.codigo == autorizacao.ENDERECO_RECUSADO:
            # Nada saiu: o endereco de troca do terceiro nao e o combinado. A chave de renovacao nao vai a ele.
            await _registrar_falha(
                c, sistema_id, pessoa_id, sistema.nome, "o endereco de troca de chave nao e publico ou nao usa https"
            )
            await registrar(
                c,
                papel="despachante",
                tipo="sala_endereco_recusado",
                campos={"sistema": sistema.nome},
                pessoa_id=pessoa_id,
                sistema_id=sistema_id,
            )
            CONTADORES.somar("sala_endereco_recusado", onde="renovacao")
            return atual or RenovacaoIndisponivel("endereco de troca recusado")
        if recusa.codigo == "invalid_grant":
            await _derrubar(c, sistema_id, pessoa_id, "o sistema recusou a renovacao", nome=sistema.nome)
            return AutorizacaoVencida("o sistema recusou a renovacao")
        await _registrar_falha(c, sistema_id, pessoa_id, sistema.nome, "o sistema recusou o MCP-CLARO")
        return atual or RenovacaoIndisponivel("o sistema recusou o cliente do MCP-CLARO")
    except autorizacao.TrocaIndisponivel:
        await _registrar_falha(c, sistema_id, pessoa_id, sistema.nome, "o sistema nao respondeu")
        return atual or RenovacaoIndisponivel("o servidor de autorizacao nao respondeu")
    cifrada = await autorizacao.guardar_resposta(c, cifra, sistema_id, pessoa_id, resposta)
    await registrar(
        c,
        papel="despachante",
        tipo="chave_renovada",
        campos={"sistema": sistema.nome},
        pessoa_id=pessoa_id,
        sistema_id=sistema_id,
        dados={"vence_em": resposta.expira_em.isoformat() if resposta.expira_em else None},
    )
    CONTADORES.somar("chaves_renovadas")
    return ChaveAberta(resposta.acesso, cifrada, renovada=True)


async def _nome(c: AsyncConnection, sistema_id: uuid.UUID) -> str:
    nome = (await c.execute(text("SELECT nome FROM sistemas WHERE id = :s"), {"s": sistema_id})).scalar_one_or_none()
    return str(nome or "desconhecido")


async def _registrar_falha(
    c: AsyncConnection, sistema_id: uuid.UUID, pessoa_id: uuid.UUID, nome: str, motivo: str
) -> None:
    await registrar(
        c,
        papel="despachante",
        tipo="renovacao_falhou",
        campos={"sistema": nome, "motivo": motivo},
        pessoa_id=pessoa_id,
        sistema_id=sistema_id,
    )


async def _derrubar(
    c: AsyncConnection, sistema_id: uuid.UUID, pessoa_id: uuid.UUID, motivo: str, *, nome: str | None = None
) -> None:
    """Chave sem volta: apaga acesso e renovacao do par (ja dentro da trava) e registra."""
    await c.execute(
        text("DELETE FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p AND aplicativo_id IS NULL"),
        {"s": sistema_id, "p": pessoa_id},
    )
    await _registrar_falha(c, sistema_id, pessoa_id, nome or await _nome(c, sistema_id), motivo)


async def marcar_vencida(engine: AsyncEngine, *, sistema_id: uuid.UUID, pessoa_id: uuid.UUID, cifrada: str) -> None:
    """Depois de um 401 no envio: a chave usada passa a contar como vencida, e o proximo pedido renova antes.

    So mexe na linha se ela ainda guarda a mesma chave (outra renovacao pode ter gravado uma nova).
    """
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE chaves_cifradas SET expira_em = now() WHERE sistema_id = :s AND pessoa_id = :p "
                "AND aplicativo_id IS NULL AND tipo = 'acesso' AND cifrada = :c"
            ),
            {"s": sistema_id, "p": pessoa_id, "c": cifrada},
        )
