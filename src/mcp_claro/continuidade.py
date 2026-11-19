"""Conferencia de continuidade do banco na partida do despachante.

Na partida, o despachante chama ``conferir_continuidade`` antes de atender qualquer pedido. Ela le o
identificador (``system_identifier``) e a linha do tempo (``timeline_id``) do PostgreSQL pela funcao
``continuidade_controle()`` (migracao 0004) e compara com a ultima linha de ``continuidade_banco``.

Nao confere quando:

- o identificador mudou (o banco veio de uma copia logica ou de outra instalacao);
- a linha do tempo mudou (recuperacao para um ponto no tempo, pelo pgBackRest);
- a ultima linha e a marca "restaurado", gravada pelo roteiro de restauracao;
- o vigia guarda um fechamento mais novo que o ultimo do banco (o banco voltou para tras);
- o vigia nao respondeu (``ultimo_fechamento_no_vigia`` e ``None``).

Quando nao confere, numa transacao so: a porta de saida fecha, os aplicativos ativos voltam pausados
(menos as identidades da ronda e do vigia, que nao alcancam sistema real), o extrato registra o
motivo, o intervalo que pode ter se perdido e cada aplicativo pausado, e uma nova linha entra em
``continuidade_banco``. Reabrir a saida e decisao humana registrada (``reabrir_saida``). Na primeira
partida, so grava a linha.

``ultimo_fechamento_no_vigia``: ``None`` quando o vigia nao respondeu; ``0`` quando respondeu e ainda
nao guarda fechamento nenhum.

Linha de comando, para o roteiro de restauracao (``docs/restauracao.md``), com o usuario do banco do
despachante::

    python -m mcp_claro.continuidade situacao
    python -m mcp_claro.continuidade conferir --vigia <ultimo fechamento no vigia>
    python -m mcp_claro.continuidade marcar-restaurado --quem "Nome da pessoa" --motivo "Ensaio de 2026-11-18"
    python -m mcp_claro.continuidade reabrir --quem admin:<id da pessoa ADMIN> --motivo "Conferido com o vigia"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.regras.bloqueios import SemPermissao, autor_de, quem_rotina
from mcp_claro.regras.estado import ler_saida, mudar_saida
from mcp_claro.ronda.config import CLIENTE_RONDA, CLIENTE_VIGIA

log = logger("mcp_claro.continuidade")

Desfecho = Literal["primeira_partida", "confere", "nao_confere"]

MOTIVOS: Final[dict[str, str]] = {
    "marca_restaurado": "o roteiro marcou o banco como restaurado",
    "identificador_mudou": "o identificador do PostgreSQL mudou",
    "linha_do_tempo_mudou": "a linha do tempo do PostgreSQL mudou",
    "vigia_mais_novo": "o vigia guarda um fechamento mais novo que o do banco",
    "vigia_sem_resposta": "o vigia nao respondeu",
}
"""Codigo de cada motivo e a frase legivel que vai ao extrato."""

_SINAIS_DE_RESTAURACAO: Final = frozenset(
    {"marca_restaurado", "identificador_mudou", "linha_do_tempo_mudou", "vigia_mais_novo"}
)
QUEM: Final = quem_rotina("continuidade")
POUPADOS: Final[tuple[str, ...]] = (CLIENTE_RONDA, CLIENTE_VIGIA)
"""Aplicativos que nao pausam: a ronda e o vigia precisam continuar conferindo o gateway."""
_TRAVA: Final = "mcp_claro.continuidade"


class ReaberturaRecusada(SemPermissao):
    """Quem pediu nao pode reabrir a porta de saida, ou faltou o motivo."""


@dataclass(frozen=True, slots=True)
class Controle:
    """Identificador e linha do tempo do PostgreSQL."""

    system_identifier: str
    timeline_id: int


@dataclass(frozen=True, slots=True)
class Resultado:
    """O que a conferencia viu e fez."""

    desfecho: Desfecho
    atual: Controle
    anterior: Controle | None
    ultimo_fechamento: int | None
    ultimo_fechamento_vigia: int | None
    motivos: tuple[str, ...]
    aplicativos_pausados: tuple[uuid.UUID, ...]
    linha_id: int

    @property
    def confere(self) -> bool:
        return self.desfecho != "nao_confere"

    @property
    def saida_fechada(self) -> bool:
        """A conferencia fechou a porta de saida (so quando nao confere)."""
        return self.desfecho == "nao_confere"


async def ler_controle(c: AsyncConnection) -> Controle:
    linha = (await c.execute(text("SELECT system_identifier, timeline_id FROM continuidade_controle()"))).one()
    return Controle(str(linha.system_identifier), int(linha.timeline_id))


async def _ultima_linha(c: AsyncConnection) -> Row[Any] | None:
    return (
        await c.execute(
            text(
                "SELECT id, system_identifier, timeline_id, restaurado, origem, desfecho, motivo, visto_em "
                "FROM continuidade_banco ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()


def comparar(
    anterior: Controle,
    restaurado: bool,
    atual: Controle,
    fechamento_banco: int | None,
    fechamento_vigia: int | None,
    *,
    exigir_vigia: bool = True,
) -> tuple[str, ...]:
    """Motivos pelos quais a continuidade nao confere. Vazio quando tudo bate."""
    motivos: list[str] = []
    if restaurado:
        motivos.append("marca_restaurado")
    if anterior.system_identifier != atual.system_identifier:
        motivos.append("identificador_mudou")
    if anterior.timeline_id != atual.timeline_id:
        motivos.append("linha_do_tempo_mudou")
    if fechamento_vigia is None:
        if exigir_vigia:
            motivos.append("vigia_sem_resposta")
    elif fechamento_vigia > (fechamento_banco or 0):
        motivos.append("vigia_mais_novo")
    return tuple(motivos)


def _legivel(motivos: Iterable[str]) -> str:
    return "; ".join(MOTIVOS[m] for m in motivos)


def _quando(momento: datetime) -> str:
    return momento.astimezone(UTC).strftime("%d/%m/%Y %H:%M UTC")


async def _gravar(
    c: AsyncConnection,
    atual: Controle,
    fechamento_banco: int | None,
    fechamento_vigia: int | None,
    desfecho: Desfecho,
    motivos: tuple[str, ...],
) -> int:
    return int(
        (
            await c.execute(
                text(
                    "INSERT INTO continuidade_banco (system_identifier, timeline_id, ultimo_fechamento, restaurado,"
                    " motivo, origem, desfecho, ultimo_fechamento_vigia) "
                    "VALUES (:s, :t, :f, false, :m, 'despachante', :d, :v) RETURNING id"
                ),
                {
                    "s": atual.system_identifier,
                    "t": atual.timeline_id,
                    "f": fechamento_banco,
                    "m": ",".join(motivos) or None,
                    "d": desfecho,
                    "v": fechamento_vigia,
                },
            )
        ).scalar_one()
    )


async def _pausar_aplicativos(c: AsyncConnection, papel: str, poupados: tuple[str, ...]) -> tuple[uuid.UUID, ...]:
    linhas = (
        await c.execute(
            text(
                "UPDATE aplicativos SET situacao = 'pausado', atualizado_em = now() "
                "WHERE situacao = 'ativo' AND NOT (client_id = ANY(:poupados)) RETURNING id, nome"
            ),
            {"poupados": list(poupados)},
        )
    ).all()
    for linha in linhas:
        await registrar(
            c,
            papel=papel,
            tipo="aplicativo_pausado_continuidade",
            campos={"aplicativo": linha.nome[:120]},
            aplicativo_id=linha.id,
        )
    return tuple(linha.id for linha in linhas)


async def conferir_continuidade(
    engine: AsyncEngine,
    ultimo_fechamento_no_vigia: int | None,
    *,
    exigir_vigia: bool = True,
    papel: str = "despachante",
    poupados: tuple[str, ...] = POUPADOS,
) -> Resultado:
    """Confere a continuidade do banco na partida. Fecha a saida e pausa os aplicativos se nao conferir.

    ``exigir_vigia=False`` so para desenvolvimento sem vigia: o vigia sem resposta deixa de fechar a
    saida (ver o parametro ``CONTINUITY_REQUIRE_WATCH`` no relatorio da Fase 1).
    """
    async with engine.begin() as c:
        # Duas replicas do despachante subindo juntas conferem uma de cada vez.
        await c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:t))"), {"t": _TRAVA})
        atual = await ler_controle(c)
        fechamento_banco = (await c.execute(text("SELECT max(numero) FROM fechamentos"))).scalar_one_or_none()
        ultima = await _ultima_linha(c)

        if ultima is None:
            linha_id = await _gravar(c, atual, fechamento_banco, ultimo_fechamento_no_vigia, "primeira_partida", ())
            await registrar(c, papel=papel, tipo="continuidade_primeira", dados={"continuidade_id": linha_id})
            log.info("continuidade_primeira_partida", timeline=atual.timeline_id)
            return Resultado(
                "primeira_partida", atual, None, fechamento_banco, ultimo_fechamento_no_vigia, (), (), linha_id
            )

        anterior = Controle(str(ultima.system_identifier), int(ultima.timeline_id))
        motivos = comparar(
            anterior,
            bool(ultima.restaurado),
            atual,
            fechamento_banco,
            ultimo_fechamento_no_vigia,
            exigir_vigia=exigir_vigia,
        )
        if not motivos:
            linha_id = await _gravar(c, atual, fechamento_banco, ultimo_fechamento_no_vigia, "confere", ())
            log.info("continuidade_confere", timeline=atual.timeline_id)
            return Resultado("confere", atual, anterior, fechamento_banco, ultimo_fechamento_no_vigia, (), (), linha_id)

        # Nao confere: o ultimo registro que sobrou no banco marca o comeco do que pode ter se perdido. Ficam
        # de fora as linhas do proprio roteiro e, havendo a marca, o que veio depois dela.
        ate = (await c.execute(text("SELECT clock_timestamp()"))).scalar_one()
        limite = ultima.visto_em if ultima.restaurado else ate
        desde = (
            await c.execute(
                text("SELECT max(criado_em) FROM extrato WHERE papel <> 'roteiro' AND criado_em < :limite"),
                {"limite": limite},
            )
        ).scalar_one_or_none() or ultima.visto_em
        dados: dict[str, Any] = {
            "motivos": list(motivos),
            "timeline_anterior": anterior.timeline_id,
            "timeline_atual": atual.timeline_id,
            "identificador_mudou": anterior.system_identifier != atual.system_identifier,
            "fechamento_banco": fechamento_banco,
            "fechamento_vigia": ultimo_fechamento_no_vigia,
        }
        await mudar_saida(c, aberta=False, quem=QUEM, motivo=_legivel(motivos), papel=papel)
        if _SINAIS_DE_RESTAURACAO.intersection(motivos):
            await registrar(c, papel=papel, tipo="restauracao_detectada", dados=dados)
        else:
            await registrar(
                c, papel=papel, tipo="continuidade_nao_confere", campos={"motivo": _legivel(motivos)}, dados=dados
            )
        perdidos = None
        if ultimo_fechamento_no_vigia is not None and ultimo_fechamento_no_vigia > (fechamento_banco or 0):
            perdidos = ultimo_fechamento_no_vigia - (fechamento_banco or 0)
        await registrar(
            c,
            papel=papel,
            tipo="intervalo_perdido",
            campos={"desde": _quando(desde), "ate": _quando(ate)},
            dados={"desde": desde.isoformat(), "ate": ate.isoformat(), "fechamentos_perdidos": perdidos},
        )
        pausados = await _pausar_aplicativos(c, papel, poupados)
        linha_id = await _gravar(c, atual, fechamento_banco, ultimo_fechamento_no_vigia, "nao_confere", motivos)
    log.warning("continuidade_nao_confere", motivos=list(motivos), aplicativos_pausados=len(pausados))
    return Resultado(
        "nao_confere", atual, anterior, fechamento_banco, ultimo_fechamento_no_vigia, motivos, pausados, linha_id
    )


async def marcar_restaurado(engine: AsyncEngine, quem: str, motivo: str) -> int:
    """Grava a marca "restaurado" (roteiro de restauracao). Devolve o numero da linha."""
    async with engine.begin() as c:
        return int(
            (
                await c.execute(text("SELECT continuidade_marcar_restaurado(:q, :m)"), {"q": quem, "m": motivo})
            ).scalar_one()
        )


async def reabrir_saida(engine: AsyncEngine, quem: str, motivo: str, *, papel: str = "entrada") -> bool:
    """Reabre a porta de saida por decisao humana registrada. So o ADMIN ativo reabre.

    ``quem`` segue ``bloqueios.quem_admin`` (``admin:<id da pessoa>``). Devolve ``False`` se a saida ja
    estava aberta. Os aplicativos continuam pausados: cada um volta por decisao propria.
    """
    autor = autor_de(quem)
    if autor.papel != "admin" or autor.pessoa_id is None:
        raise ReaberturaRecusada("So o ADMIN reabre a porta de saida.")
    if not motivo.strip():
        raise ReaberturaRecusada("Diga o motivo para reabrir a porta de saida.")
    async with engine.begin() as c:
        pessoa = (
            await c.execute(text("SELECT papel, situacao FROM pessoas WHERE id = :p"), {"p": autor.pessoa_id})
        ).first()
        if pessoa is None or pessoa.papel != "admin" or pessoa.situacao != "ativa":
            raise ReaberturaRecusada("So uma pessoa ADMIN ativa reabre a porta de saida.")
        if (await ler_saida(c)).aberta:
            return False
        await mudar_saida(c, aberta=True, quem=quem, motivo=motivo.strip(), papel=papel)
    log.info("saida_reaberta")
    return True


async def situacao(engine: AsyncEngine) -> dict[str, Any]:
    """Ultima conferencia, valores atuais e estado da saida (para o roteiro e as telas)."""
    async with engine.connect() as c:
        atual = await ler_controle(c)
        ultima = await _ultima_linha(c)
        saida = await ler_saida(c)
    return {
        "system_identifier": atual.system_identifier,
        "timeline_id": atual.timeline_id,
        "ultima_linha": None if ultima is None else {k: str(v) for k, v in ultima._mapping.items()},
        "saida_aberta": saida.aberta,
        "motivo_saida": saida.motivo,
    }


async def _rodar(args: argparse.Namespace) -> int:
    from mcp_claro.config.settings import Parametros
    from mcp_claro.db.engine import criar_engine

    engine = criar_engine(Parametros(), pool_size=1, max_overflow=0)
    try:
        if args.comando == "situacao":
            for chave, valor in (await situacao(engine)).items():
                print(f"{chave}: {valor}")
        elif args.comando == "conferir":
            r = await conferir_continuidade(engine, args.vigia, exigir_vigia=not args.sem_exigir_vigia)
            print(f"desfecho: {r.desfecho}")
            print(f"motivos: {','.join(r.motivos) or '-'}")
            print(f"timeline_id: {r.atual.timeline_id}")
            print(f"aplicativos_pausados: {len(r.aplicativos_pausados)}")
        elif args.comando == "marcar-restaurado":
            linha = await marcar_restaurado(engine, args.quem, args.motivo)
            print(f"Marca restaurado gravada (linha {linha}). A porta de saida fecha na proxima partida.")
        else:
            if await reabrir_saida(engine, args.quem, args.motivo, papel="despachante"):
                print("Porta de saida reaberta. Os aplicativos continuam pausados ate cada um voltar.")
            else:
                print("A porta de saida ja estava aberta; nada mudou.")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_claro.continuidade", description="Continuidade do banco do MCP-CLARO."
    )
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("situacao", help="mostra a ultima conferencia e o estado da porta de saida")
    conferir = sub.add_parser("conferir", help="faz agora a mesma conferencia da partida do despachante")
    conferir.add_argument(
        "--vigia", type=int, default=None, help="ultimo fechamento guardado no vigia (sem ele: vigia sem resposta)"
    )
    conferir.add_argument("--sem-exigir-vigia", action="store_true", help="so desenvolvimento, sem vigia")
    marcar = sub.add_parser("marcar-restaurado", help="grava a marca restaurado (roteiro de restauracao)")
    marcar.add_argument("--quem", required=True, help="nome de quem restaurou (fica so na tabela)")
    marcar.add_argument("--motivo", required=True)
    reabrir = sub.add_parser("reabrir", help="reabre a porta de saida (decisao do ADMIN)")
    reabrir.add_argument("--quem", required=True, help="admin:<id da pessoa ADMIN>")
    reabrir.add_argument("--motivo", required=True)
    try:
        return asyncio.run(_rodar(parser.parse_args(argv)))
    except (SemPermissao, ValueError) as erro:
        print(str(erro), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
