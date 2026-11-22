"""Alerta dos tres sinais: o aplicativo leu texto de fora, viu dado privado e agora quer enviar algo.

A entrada grava um sinal sempre que o aplicativo recebe texto de um sistema (``registrar``), na transacao que
confirma ANTES de devolver o texto: uma acao causada por esse texto so chega depois, e ``alerta`` ja a enxerga.

- "leu texto de fora" (``TEXTO_DE_FORA``): a ferramenta e de um servidor de terceiros, ou e propria e o ADMIN a
  marcou "traz texto de fora" (``ferramentas_revisadas.texto_de_fora``);
- "viu dado privado" (``DADO_PRIVADO``): a ferramenta tem campos pessoais (``campos_pessoais`` nao vazio, D22).

Os dois somam pelo par pessoa e aplicativo (D20), dentro da janela ``THREE_SIGNALS_WINDOW_MINUTES``. Com os dois na
janela, toda acao (``muda`` ou ``tem_volta``, D21) vai para a ficha, sem consultar nem gastar limite
(``saida.pedidos``). O terceiro sinal e a propria acao.

Quando o alerta vale (D23 com a correcao B7): o sinal "leu texto de fora" de ferramenta propria marcada conta
sempre; o de servidor de terceiros so conta com ``terceiros`` (a chave da Fase 3 ligada, ou pessoa de teste).
"Viu dado privado" conta sempre: so aperta. Os sinais sao gravados sempre, com a chave ligada ou nao.

So nomes do cadastro e hora: nenhum argumento, resultado ou texto do sistema entra num sinal nem no retrato.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.regras import politica
from mcp_claro.versao import PREFIXO_META

TEXTO_DE_FORA: Final = "texto_de_fora"
DADO_PRIVADO: Final = "dado_privado"
TIPOS: Final = (TEXTO_DE_FORA, DADO_PRIVADO)

MAIOR_JANELA_MINUTOS: Final = 10080
"""O maximo de ``THREE_SIGNALS_WINDOW_MINUTES`` (7 dias). O gatilho ``sinais_guarda`` (migracao 0006) so deixa
apagar sinal mais velho que isso, e as rotinas apagam os velhos: um teste confere os dois numeros."""

CHAVE_JANELA: Final = "THREE_SIGNALS_WINDOW_MINUTES"
MOTIVO: Final = PREFIXO_META + "motivo"


def veio_do_sistema(resultado: Mapping[str, Any]) -> bool:
    """O resultado traz texto do sistema (mesmo com ``isError``), e nao um texto nosso.

    Texto nosso (leitura recusada, sistema fora do ar, pedido de dados barrado, capacidade que falta) sempre leva
    o motivo do MCP-CLARO no ``_meta``; o ``_meta`` do sistema nunca passa do despachante."""
    meta = resultado.get("_meta")
    return not (isinstance(meta, Mapping) and MOTIVO in meta)


_SQL_REGISTRAR = """
INSERT INTO sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id)
SELECT CAST(:p AS uuid), CAST(:a AS uuid), t.tipo, r.sistema_id, r.id
FROM ferramentas_revisadas r
JOIN sistemas s ON s.id = r.sistema_id
CROSS JOIN LATERAL (VALUES
  ('texto_de_fora', s.origem <> 'proprio' OR r.texto_de_fora),
  ('dado_privado', coalesce(cardinality(r.campos_pessoais), 0) > 0)
) AS t (tipo, vale)
WHERE r.id = :f AND t.vale
RETURNING tipo
"""


async def registrar(
    c: AsyncConnection, *, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, ferramenta_id: uuid.UUID
) -> tuple[str, ...]:
    """Grava os sinais que a ferramenta da (nenhum, um ou os dois), na transacao de quem chama.

    Quem chama confirma a transacao antes de devolver o texto ao aplicativo. Devolve os tipos gravados."""
    linhas = await c.execute(text(_SQL_REGISTRAR), {"p": pessoa_id, "a": aplicativo_id, "f": ferramenta_id})
    return tuple(sorted(str(t) for t in linhas.scalars()))


@dataclass(frozen=True, slots=True)
class Visto:
    """O sinal mais recente de um tipo, na janela: nomes do cadastro e hora."""

    sinal: str
    ferramenta: str
    sistema: str
    visto_em: datetime


@dataclass(frozen=True, slots=True)
class Alerta:
    """Os dois sinais na janela, para o par pessoa e aplicativo (``vistos`` em ordem de ``TIPOS``)."""

    janela_minutos: int
    vistos: tuple[Visto, ...]

    def visto(self, sinal: str) -> Visto:
        return next(v for v in self.vistos if v.sinal == sinal)

    @property
    def de_fora(self) -> Visto:
        return self.visto(TEXTO_DE_FORA)

    @property
    def privado(self) -> Visto:
        return self.visto(DADO_PRIVADO)

    @property
    def ate(self) -> datetime:
        """Ate quando o alerta dura, se nenhum sinal novo chegar: quando o mais velho dos dois sai da janela."""
        return min(v.visto_em for v in self.vistos) + timedelta(minutes=self.janela_minutos)

    def retrato(self) -> dict[str, Any]:
        """O que ``fichas.sinais`` guarda: a janela e, de cada sinal, ferramenta, sistema e hora."""
        return {
            "janela_minutos": self.janela_minutos,
            "vistos": [
                {"sinal": v.sinal, "ferramenta": v.ferramenta, "sistema": v.sistema, "visto_em": v.visto_em.isoformat()}
                for v in self.vistos
            ],
        }


_SQL_ALERTA = """
SELECT DISTINCT ON (g.tipo) g.tipo, g.visto_em, r.nome_vitrine AS ferramenta, s.nome AS sistema
FROM sinais g
JOIN ferramentas_revisadas r ON r.id = g.ferramenta_id
JOIN sistemas s ON s.id = g.sistema_id
WHERE g.pessoa_id = :p AND g.aplicativo_id = :a
  AND g.visto_em > now() - make_interval(mins => :j)
  AND (g.tipo <> 'texto_de_fora' OR s.origem = 'proprio' OR CAST(:t AS boolean))
ORDER BY g.tipo, g.visto_em DESC, g.id DESC
"""


def janela_valida(minutos: int) -> int:
    """A janela trazida para dentro de 1 a ``MAIOR_JANELA_MINUTOS`` (a politica ja traz para 5 a 10080)."""
    return max(1, min(int(minutos), MAIOR_JANELA_MINUTOS))


async def alerta(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID,
    aplicativo_id: uuid.UUID,
    janela_minutos: int,
    terceiros: bool,
) -> Alerta | None:
    """O alerta dos tres sinais para o par pessoa e aplicativo, ou None sem os dois sinais na janela.

    ``terceiros``: o sinal "leu texto de fora" vindo de servidor de terceiros conta (chave da Fase 3 ligada ou
    pessoa de teste). O de ferramenta propria marcada conta sempre (B7)."""
    janela = janela_valida(janela_minutos)
    linhas = (
        await c.execute(text(_SQL_ALERTA), {"p": pessoa_id, "a": aplicativo_id, "j": janela, "t": terceiros})
    ).all()
    por_tipo = {str(linha.tipo): linha for linha in linhas}
    if any(tipo not in por_tipo for tipo in TIPOS):
        return None
    vistos = tuple(
        Visto(tipo, str(por_tipo[tipo].ferramenta), str(por_tipo[tipo].sistema), por_tipo[tipo].visto_em)
        for tipo in TIPOS
    )
    return Alerta(janela, vistos)


async def alerta_agora(
    c: AsyncConnection, *, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, terceiros: bool
) -> Alerta | None:
    """``alerta`` com a janela da politica vigente (para as telas)."""
    janela = await politica.vigente(c, CHAVE_JANELA)
    return await alerta(c, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id, janela_minutos=janela, terceiros=terceiros)


def nomes_do_retrato(retrato: Mapping[str, Any]) -> tuple[str, str]:
    """(ferramenta do texto de fora, ferramenta do dado privado) de um retrato, para a linha ``tres_sinais``."""
    vistos = {v.get("sinal"): v for v in retrato.get("vistos") or [] if isinstance(v, Mapping)}
    return (
        str(vistos.get(TEXTO_DE_FORA, {}).get("ferramenta", "?")),
        str(vistos.get(DADO_PRIVADO, {}).get("ferramenta", "?")),
    )
