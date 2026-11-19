"""Estado das duas portas do gateway: a entrada (aplicativos) e a porta de saida (sistemas).

- Entrada ``aberta``: atende tudo o que as outras regras deixarem.
- Entrada ``so_leitura``: atende so ferramentas da classe "le". Na Fase 1 so ha leitura, entao
  nada muda para o aplicativo; a regra ja fica pronta para as acoes da Fase 2.
- Entrada ``fechada``: recusa todo pedido, ate a lista de ferramentas.
- Saida fechada: o despachante nao chama sistema nenhum e a entrada nao da protocolo novo (cofre
  trancado ou fora do ar, conferencia do banco na partida, decisao humana). Reabrir e decisao humana
  registrada, com uma excecao: a saida que a conferencia do cofre fechou (``saida.cofre``) reabre sozinha
  quando o cofre volta destravado, se ninguem a mudou depois.

Sem linha na tabela, vale o mais seguro: entrada e saida fechadas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.extrato import registrar
from mcp_claro.regras.bloqueios import autor_de, descrever_quem

NivelEntrada = Literal["aberta", "so_leitura", "fechada"]
NIVEIS: Final[tuple[str, ...]] = get_args(NivelEntrada)

MENSAGEM_ENTRADA: Final[dict[str, str]] = {
    "fechada": "A entrada do MCP-CLARO esta fechada agora. Nada foi enviado. Tente mais tarde ou fale com o ADMIN.",
    "so_leitura": "O MCP-CLARO esta aceitando so leituras agora. Nada foi enviado.",
}
MENSAGEM_SAIDA_FECHADA: Final = (
    "A porta de saida do MCP-CLARO esta fechada agora. Nada foi enviado ao sistema. Tente mais tarde."
)
_NIVEL_LEGIVEL: Final[dict[str, str]] = {"aberta": "aberta", "so_leitura": "so de leitura", "fechada": "fechada"}


class EstadoInvalido(ValueError):
    pass


class SaidaFechada(Exception):
    """A porta de saida esta fechada: nada vai a sistema nenhum."""

    def __init__(self, motivo: str | None) -> None:
        self.motivo = motivo
        super().__init__(MENSAGEM_SAIDA_FECHADA)


def permite(nivel: str, classe: str) -> bool:
    """A entrada atende um pedido desta classe neste nivel?"""
    if nivel == "aberta":
        return True
    if nivel == "so_leitura":
        return classe == "le"
    return False


@dataclass(frozen=True, slots=True)
class EstadoEntrada:
    nivel: NivelEntrada
    motivo: str | None = None

    @property
    def fechada(self) -> bool:
        return self.nivel == "fechada"

    def permite(self, classe: str) -> bool:
        return permite(self.nivel, classe)

    def mensagem(self) -> str:
        return MENSAGEM_ENTRADA.get(self.nivel, MENSAGEM_ENTRADA["fechada"])

    def motivo_recusa(self) -> str:
        return "entrada_" + self.nivel


@dataclass(frozen=True, slots=True)
class EstadoSaida:
    aberta: bool
    motivo: str | None = None
    mudou_por: str | None = None
    """Quem mudou por ultimo (``bloqueios.quem_*``): a saida fechada pelo cofre so reabre sozinha se foi ele."""
    mudou_em: datetime | None = None
    """Hora da ultima mudanca: separa um fechamento do outro (avisos do cofre com a saida ja fechada)."""


async def ler_entrada(c: AsyncConnection) -> EstadoEntrada:
    linha = (await c.execute(text("SELECT nivel, motivo FROM estado_entrada WHERE id = 1"))).first()
    if linha is None or linha.nivel not in NIVEIS:
        return EstadoEntrada("fechada", "estado da entrada ausente")
    return EstadoEntrada(linha.nivel, linha.motivo)


async def ler_saida(c: AsyncConnection) -> EstadoSaida:
    linha = (await c.execute(text("SELECT aberta, motivo, mudou_por, mudou_em FROM estado_saida WHERE id = 1"))).first()
    if linha is None:
        return EstadoSaida(False, "estado da saida ausente")
    return EstadoSaida(bool(linha.aberta), linha.motivo, linha.mudou_por, linha.mudou_em)


async def conferir_saida(c: AsyncConnection) -> None:
    """Para o despachante, antes de qualquer envio: levanta ``SaidaFechada`` se a porta estiver fechada."""
    saida = await ler_saida(c)
    if not saida.aberta:
        raise SaidaFechada(saida.motivo)


async def mudar_entrada(c: AsyncConnection, nivel: str, quem: str, motivo: str, *, papel: str = "entrada") -> None:
    """Muda o nivel da entrada (ronda, ADMIN) e registra no extrato. ``quem`` segue ``bloqueios.quem_*``."""
    if nivel not in NIVEIS:
        raise EstadoInvalido(f"Nivel da entrada desconhecido: {nivel}. Use aberta, so_leitura ou fechada.")
    autor_de(quem)
    await c.execute(
        text("UPDATE estado_entrada SET nivel = :n, motivo = :m, mudou_em = now(), mudou_por = :q WHERE id = 1"),
        {"n": nivel, "m": motivo[:500], "q": quem},
    )
    await registrar(
        c,
        papel=papel,
        tipo="estado_entrada_mudou",
        campos={"quem": descrever_quem(quem), "nivel": _NIVEL_LEGIVEL[nivel]},
        dados={"nivel": nivel},
    )


async def mudar_saida(c: AsyncConnection, *, aberta: bool, quem: str, motivo: str, papel: str = "despachante") -> None:
    """Abre ou fecha a porta de saida e registra no extrato.

    Fechar toma a trava exclusiva de ``tudo``: um envio em andamento termina de ser marcado antes, e
    nenhum outro comeca depois. Fechar de novo a saida ja fechada guarda na linha do extrato o que o ADMIN ja
    sabia do cofre: o aviso seguinte do cofre continua de onde parou (``saida.cofre.avisado_trancado``).
    """
    autor_de(quem)
    from mcp_claro.saida import cofre as saida_cofre
    from mcp_claro.saida import travas

    await travas.exclusiva(c, [travas.TUDO])
    dados: dict[str, bool | None] = {"aberta": aberta}
    if not aberta:
        anterior = await ler_saida(c)
        if not anterior.aberta:
            dados[saida_cofre.CAMPO_HERDADO] = await saida_cofre.avisado_trancado(c, anterior)
    await c.execute(
        text("UPDATE estado_saida SET aberta = :a, motivo = :m, mudou_em = now(), mudou_por = :q WHERE id = 1"),
        {"a": aberta, "m": motivo[:500], "q": quem},
    )
    await registrar(
        c,
        papel=papel,
        tipo="estado_saida_mudou",
        campos={"quem": descrever_quem(quem), "acao": "abriu" if aberta else "fechou"},
        dados=dados,
    )
