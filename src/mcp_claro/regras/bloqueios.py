"""Bloqueio de um toque e pausa.

Uma linha de ``bloqueios`` vale enquanto ``encerrado_em`` estiver vazio. Nenhuma linha e apagada:
encerrar e registrar quem e quando. A entrada e o despachante consultam os bloqueios ativos antes de
cada pedido e recusam com frase legivel.

Alcances e campos exigidos:

- ``pessoa`` (``pessoa_id``): tudo o que a pessoa pedir, por qualquer aplicativo;
- ``aplicativo`` (``aplicativo_id``): tudo o que chegar por aquele aplicativo;
- ``pessoa_aplicativo`` (``pessoa_id`` e ``aplicativo_id``): a pessoa naquele aplicativo;
- ``sistema`` (``sistema_id``): nada vai aquele sistema;
- ``tudo`` (nenhum campo): o gateway inteiro.

Quem pode (``pode_ligar`` e ``pode_encerrar``):

- o ADMIN liga e encerra qualquer bloqueio e qualquer pausa;
- as rotinas (ronda, teto de confirmacoes, restauracao) ligam qualquer um, mas nao encerram;
- a pessoa liga bloqueio ou pausa so no proprio alcance (``pessoa`` ou ``pessoa_aplicativo`` dela);
- pausa e reversivel pela propria pessoa, quando o alcance e so dela, seja quem for que a ligou;
- bloqueio so o ADMIN encerra, mesmo o que a pessoa ligou sobre si.

``quem`` e um texto com papel e identificador: ``admin:<pessoa_id>``, ``pessoa:<pessoa_id>`` ou
``rotina:<nome>`` (use ``quem_admin``, ``quem_pessoa`` e ``quem_rotina``). O texto completo fica na
tabela; no extrato vai so a descricao ("O ADMIN", "A propria pessoa", "A rotina ronda"). O motivo
escrito fica so na tabela, nunca no extrato, que e so de acrescentar.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, get_args

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.extrato import registrar

Tipo = Literal["bloqueio", "pausa"]
Alcance = Literal["pessoa", "aplicativo", "pessoa_aplicativo", "sistema", "tudo"]
PapelAutor = Literal["admin", "pessoa", "rotina"]

TIPOS: Final[tuple[str, ...]] = get_args(Tipo)
ALCANCES: Final[tuple[str, ...]] = get_args(Alcance)
CAMPOS_POR_ALCANCE: Final[dict[str, frozenset[str]]] = {
    "pessoa": frozenset({"pessoa_id"}),
    "aplicativo": frozenset({"aplicativo_id"}),
    "pessoa_aplicativo": frozenset({"pessoa_id", "aplicativo_id"}),
    "sistema": frozenset({"sistema_id"}),
    "tudo": frozenset(),
}
_ALCANCE_LEGIVEL: Final[dict[str, str]] = {
    "pessoa": "uma pessoa",
    "aplicativo": "um aplicativo",
    "pessoa_aplicativo": "uma pessoa num aplicativo",
    "sistema": "um sistema",
    "tudo": "todo o MCP-CLARO",
}
_NOME_ROTINA = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")

_MENSAGENS: Final[dict[tuple[str, str], str]] = {
    ("bloqueio", "tudo"): "O MCP-CLARO esta bloqueado pelo ADMIN. Nada foi enviado.",
    ("bloqueio", "pessoa"): "Seu acesso ao MCP-CLARO esta bloqueado. So o ADMIN pode desbloquear. Nada foi enviado.",
    ("bloqueio", "aplicativo"): (
        "Este aplicativo esta bloqueado no MCP-CLARO. So o ADMIN pode desbloquear. Nada foi enviado."
    ),
    ("bloqueio", "pessoa_aplicativo"): (
        "Seu acesso por este aplicativo esta bloqueado. So o ADMIN pode desbloquear. Nada foi enviado."
    ),
    ("bloqueio", "sistema"): "O sistema desta ferramenta esta bloqueado no MCP-CLARO. Nada foi enviado.",
    ("pausa", "tudo"): "O MCP-CLARO esta pausado pelo ADMIN. Nada foi enviado.",
    ("pausa", "pessoa"): (
        "Seu acesso ao MCP-CLARO esta pausado. Voce pode retomar nas telas do MCP-CLARO. Nada foi enviado."
    ),
    ("pausa", "aplicativo"): "Este aplicativo esta pausado no MCP-CLARO. Nada foi enviado.",
    ("pausa", "pessoa_aplicativo"): (
        "Seu acesso por este aplicativo esta pausado. Voce pode retomar nas telas do MCP-CLARO. Nada foi enviado."
    ),
    ("pausa", "sistema"): "O sistema desta ferramenta esta pausado no MCP-CLARO. Nada foi enviado.",
}


class BloqueioInvalido(ValueError):
    """Pedido de bloqueio mal formado: tipo, alcance ou campos que nao combinam."""


class SemPermissao(PermissionError):
    """Quem pediu nao pode ligar ou encerrar este bloqueio."""


@dataclass(frozen=True, slots=True)
class Autor:
    papel: PapelAutor
    pessoa_id: uuid.UUID | None = None
    nome: str | None = None


def quem_admin(pessoa_id: uuid.UUID) -> str:
    return f"admin:{pessoa_id}"


def quem_pessoa(pessoa_id: uuid.UUID) -> str:
    return f"pessoa:{pessoa_id}"


def quem_rotina(nome: str) -> str:
    if not _NOME_ROTINA.match(nome):
        raise BloqueioInvalido(f"Nome de rotina invalido: {nome!r}.")
    return f"rotina:{nome}"


def autor_de(quem: str) -> Autor:
    """Le o texto ``quem``. Recusa qualquer forma fora de admin:, pessoa: ou rotina:."""
    papel, _, resto = quem.partition(":")
    if papel in ("admin", "pessoa"):
        try:
            return Autor(papel, uuid.UUID(resto))  # type: ignore[arg-type]
        except ValueError:
            raise BloqueioInvalido(f"quem deve ser {papel}:<id da pessoa>.") from None
    if papel == "rotina" and _NOME_ROTINA.match(resto):
        return Autor("rotina", nome=resto)
    raise BloqueioInvalido("quem deve ser admin:<id>, pessoa:<id> ou rotina:<nome>.")


def descrever_quem(quem: str) -> str:
    autor = autor_de(quem)
    if autor.papel == "admin":
        return "O ADMIN"
    if autor.papel == "pessoa":
        return "A propria pessoa"
    return f"A rotina {autor.nome}"


def conferir_pedido(
    tipo: str,
    alcance: str,
    *,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    sistema_id: uuid.UUID | None = None,
) -> None:
    """Tipo e alcance conhecidos e exatamente os campos que o alcance pede."""
    if tipo not in TIPOS:
        raise BloqueioInvalido(f"Tipo desconhecido: {tipo}. Use bloqueio ou pausa.")
    if alcance not in ALCANCES:
        raise BloqueioInvalido(f"Alcance desconhecido: {alcance}.")
    dados = {"pessoa_id": pessoa_id, "aplicativo_id": aplicativo_id, "sistema_id": sistema_id}
    presentes = {k for k, v in dados.items() if v is not None}
    exigidos = CAMPOS_POR_ALCANCE[alcance]
    if presentes != exigidos:
        pede = ", ".join(sorted(exigidos)) or "nenhum campo"
        raise BloqueioInvalido(f"O alcance {alcance} pede {pede}.")


def pode_ligar(autor: Autor, tipo: str, alcance: str, pessoa_id: uuid.UUID | None) -> bool:
    """O ADMIN e as rotinas ligam qualquer um; a pessoa, so no proprio alcance."""
    if autor.papel in ("admin", "rotina"):
        return True
    return alcance in ("pessoa", "pessoa_aplicativo") and pessoa_id is not None and pessoa_id == autor.pessoa_id


def pode_encerrar(bloqueio: Bloqueio, autor: Autor) -> bool:
    """Bloqueio so o ADMIN encerra. Pausa: o ADMIN, ou a propria pessoa quando o alcance e so dela.

    As rotinas nunca encerram: voltar depois de uma parada automatica e decisao de uma pessoa.
    """
    if autor.papel == "admin":
        return True
    if bloqueio.tipo != "pausa" or autor.papel != "pessoa":
        return False
    return bloqueio.alcance in ("pessoa", "pessoa_aplicativo") and bloqueio.pessoa_id == autor.pessoa_id


@dataclass(frozen=True, slots=True)
class Bloqueio:
    id: uuid.UUID
    tipo: Tipo
    alcance: Alcance
    pessoa_id: uuid.UUID | None
    aplicativo_id: uuid.UUID | None
    sistema_id: uuid.UUID | None
    quem: str
    motivo: str
    criado_em: datetime
    encerrado_em: datetime | None = None
    encerrado_por: str | None = None

    @property
    def ativo(self) -> bool:
        return self.encerrado_em is None

    def motivo_recusa(self) -> str:
        """Codigo curto da recusa, para o extrato e o aplicativo (ex.: ``pausa_pessoa_aplicativo``)."""
        return f"{self.tipo}_{self.alcance}"

    def mensagem(self) -> str:
        """Frase legivel para o aplicativo mostrar a pessoa."""
        return _MENSAGENS[(self.tipo, self.alcance)]


def _bloqueio(linha: Row[Any]) -> Bloqueio:
    return Bloqueio(
        id=linha.id,
        tipo=linha.tipo,
        alcance=linha.alcance,
        pessoa_id=linha.pessoa_id,
        aplicativo_id=linha.aplicativo_id,
        sistema_id=linha.sistema_id,
        quem=linha.quem,
        motivo=linha.motivo,
        criado_em=linha.criado_em,
        encerrado_em=linha.encerrado_em,
        encerrado_por=linha.encerrado_por,
    )


@dataclass(frozen=True, slots=True)
class Vigentes:
    """Bloqueios ativos que tocam uma pessoa num aplicativo.

    ``geral`` e o mais forte que vale para qualquer pedido (bloqueio antes de pausa); ``por_sistema``
    guarda os de alcance ``sistema``, para a vitrine esconder e a chamada recusar.
    """

    geral: Bloqueio | None = None
    por_sistema: dict[uuid.UUID, Bloqueio] = field(default_factory=dict)

    def para(self, sistema_id: uuid.UUID | None) -> Bloqueio | None:
        if self.geral is not None:
            return self.geral
        return self.por_sistema.get(sistema_id) if sistema_id is not None else None


_SQL_ATIVOS = """
SELECT * FROM bloqueios
WHERE encerrado_em IS NULL AND (
  alcance = 'tudo'
  OR alcance = 'sistema'
  OR (alcance = 'pessoa' AND pessoa_id = :p)
  OR (alcance = 'aplicativo' AND aplicativo_id = :a)
  OR (alcance = 'pessoa_aplicativo' AND pessoa_id = :p AND aplicativo_id = :a)
)
ORDER BY (tipo = 'bloqueio') DESC, criado_em, id
"""


async def ativos(c: AsyncConnection, *, pessoa_id: uuid.UUID | None, aplicativo_id: uuid.UUID | None) -> Vigentes:
    """Uma consulta so: tudo o que esta ativo para esta pessoa neste aplicativo, e os sistemas parados."""
    linhas = (await c.execute(text(_SQL_ATIVOS), {"p": pessoa_id, "a": aplicativo_id})).all()
    geral: Bloqueio | None = None
    por_sistema: dict[uuid.UUID, Bloqueio] = {}
    for linha in linhas:
        b = _bloqueio(linha)
        if b.alcance == "sistema" and b.sistema_id is not None:
            por_sistema.setdefault(b.sistema_id, b)
        elif geral is None:
            geral = b
    return Vigentes(geral, por_sistema)


async def ativo_para(
    c: AsyncConnection,
    *,
    pessoa_id: uuid.UUID | None,
    aplicativo_id: uuid.UUID | None,
    sistema_id: uuid.UUID | None = None,
) -> Bloqueio | None:
    """O bloqueio ativo mais forte que recusa este pedido, ou ``None``."""
    return (await ativos(c, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id)).para(sistema_id)


async def por_id(c: AsyncConnection, bloqueio_id: uuid.UUID, *, travar: bool = False) -> Bloqueio | None:
    sql = "SELECT * FROM bloqueios WHERE id = :i FOR UPDATE" if travar else "SELECT * FROM bloqueios WHERE id = :i"
    linha = (await c.execute(text(sql), {"i": bloqueio_id})).first()
    return _bloqueio(linha) if linha else None


async def _conferir_autor(c: AsyncConnection, autor: Autor) -> None:
    """O ADMIN precisa ser ADMIN ativo no cadastro; a pessoa precisa existir. As rotinas sao do proprio gateway."""
    if autor.papel == "rotina":
        return
    linha = (await c.execute(text("SELECT papel, situacao FROM pessoas WHERE id = :p"), {"p": autor.pessoa_id})).first()
    if linha is None:
        raise SemPermissao("Pessoa desconhecida.")
    if autor.papel == "admin" and (linha.papel != "admin" or linha.situacao != "ativa"):
        raise SemPermissao("So um ADMIN ativo age como ADMIN.")


async def bloquear(
    conexao: AsyncConnection,
    tipo: str,
    alcance: str,
    quem: str,
    motivo: str,
    *,
    pessoa_id: uuid.UUID | None = None,
    aplicativo_id: uuid.UUID | None = None,
    sistema_id: uuid.UUID | None = None,
    papel: str = "entrada",
) -> Bloqueio:
    """Liga um bloqueio ou uma pausa, na transacao do chamador, e registra no extrato.

    Na mesma transacao, para as acoes do alcance que ainda nao sairam e grava o comprovante (Fase 2).

    Levanta ``BloqueioInvalido`` (campos) ou ``SemPermissao`` (ver ``pode_ligar``). ``papel`` e o papel
    do gateway que grava (entrada pelas telas, rotinas pela ronda).
    """
    conferir_pedido(tipo, alcance, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id, sistema_id=sistema_id)
    autor = autor_de(quem)
    if not motivo.strip():
        raise BloqueioInvalido("Diga o motivo do bloqueio.")
    if not pode_ligar(autor, tipo, alcance, pessoa_id):
        raise SemPermissao("A pessoa so pode bloquear ou pausar o proprio acesso.")
    await _conferir_autor(conexao, autor)
    from mcp_claro.saida import fila, travas

    # Trava exclusiva do alcance antes de gravar: o despachante, que toma a mesma trava compartilhada,
    # ou ja marcou "enviando" (e a acao aparece no comprovante como ja saida) ou enxerga este bloqueio.
    await travas.exclusiva(
        conexao,
        travas.chaves_do_alcance(alcance, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id, sistema_id=sistema_id),
    )
    linha = (
        await conexao.execute(
            text(
                "INSERT INTO bloqueios (tipo, alcance, pessoa_id, aplicativo_id, sistema_id, quem, motivo) "
                "VALUES (:t, :al, :p, :a, :s, :q, :m) RETURNING *"
            ),
            {
                "t": tipo,
                "al": alcance,
                "p": pessoa_id,
                "a": aplicativo_id,
                "s": sistema_id,
                "q": quem,
                "m": motivo.strip()[:500],
            },
        )
    ).one()
    b = _bloqueio(linha)
    await registrar(
        conexao,
        papel=papel,
        tipo="bloqueio_ligado" if tipo == "bloqueio" else "pausa_ligada",
        campos={"quem": descrever_quem(quem), "alcance": _ALCANCE_LEGIVEL[alcance]},
        pessoa_id=pessoa_id,
        aplicativo_id=aplicativo_id,
        sistema_id=sistema_id,
        dados={"bloqueio_id": str(b.id), "alcance": alcance, "por": autor.papel},
    )
    # Fase 2: o que esperava na porta de saida para aqui, e o comprovante em tres partes fica guardado.
    await fila.parar_por_bloqueio(conexao, b, papel=papel)
    return b


async def encerrar(conexao: AsyncConnection, bloqueio_id: uuid.UUID, quem: str, *, papel: str = "entrada") -> Bloqueio:
    """Encerra um bloqueio ou pausa (ver ``pode_encerrar``) e registra no extrato.

    Encerrar o que ja esta encerrado nao muda nada e devolve a linha como esta (toque repetido).
    Levanta ``LookupError`` se o bloqueio nao existe e ``SemPermissao`` se quem pediu nao pode.
    """
    autor = autor_de(quem)
    b = await por_id(conexao, bloqueio_id, travar=True)
    if b is None:
        raise LookupError("Bloqueio desconhecido.")
    if not b.ativo:
        return b
    if not pode_encerrar(b, autor):
        if b.tipo == "bloqueio":
            raise SemPermissao("So o ADMIN encerra um bloqueio.")
        raise SemPermissao("Esta pausa so pode ser retomada pelo ADMIN ou pela propria pessoa.")
    await _conferir_autor(conexao, autor)
    linha = (
        await conexao.execute(
            text(
                "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = :q "
                "WHERE id = :i AND encerrado_em IS NULL RETURNING *"
            ),
            {"i": bloqueio_id, "q": quem},
        )
    ).one()
    encerrado = _bloqueio(linha)
    await registrar(
        conexao,
        papel=papel,
        tipo="bloqueio_encerrado" if b.tipo == "bloqueio" else "pausa_encerrada",
        campos={"quem": descrever_quem(quem), "alcance": _ALCANCE_LEGIVEL[b.alcance]},
        pessoa_id=b.pessoa_id,
        aplicativo_id=b.aplicativo_id,
        sistema_id=b.sistema_id,
        dados={"bloqueio_id": str(b.id), "alcance": b.alcance, "por": autor.papel},
    )
    return encerrado
