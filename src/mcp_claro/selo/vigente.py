"""Selo vigente e selo efetivo (desenho 4.4 e 9; ``contrato.SeloLido`` e ``contrato.CalculaEfetivo``).

O selo de agora de um sistema sai SO da funcao do banco ``selo_vigente()`` (ou da visao ``selos_vigentes``, que a
chama): nenhum papel grava "nivel 2", o vencimento nao depende de rotina e a unica fonte de nivel 2 ou 3 e um teste
aprovado (D8). Este modulo le esse resultado (``ler``, ``ler_todos``), calcula o selo efetivo de uma operacao
(``efetivo``), monta o retrato que a ficha e o extrato guardam (``SeloVigente.retrato``) e diz o motivo em texto
legivel para as telas do ADMIN (``motivo_legivel``, ``selo_agora_legivel``, ``motivo_no_banco``).

``FEATURE_PHASE4_BADGES`` (D24) controla os efeitos para pessoas reais. Desligada: pessoa real fica no nivel 1, com o
motivo ``fase_desligada``, e pessoa de teste (``pessoas.teste``) fica com o vigente. Ligada (so pelo construtor de
``Parametros``, nos testes: a partida a recusa): o vigente para todos. O efetivo nunca sobe acima do vigente.

Os textos legiveis vao as telas: por isso tem acento.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.selo import contrato

FUSO_PADRAO: Final = "America/Sao_Paulo"
"""Fuso das datas dos textos legiveis quando quem chama nao da outro (o padrao de ``SCREENS_TIMEZONE``)."""

_SEM_TESTE: Final = frozenset({"nunca_testado", contrato.MOTIVO_FASE_DESLIGADA})
"""Os motivos que podem vir sem teste. ``nunca_testado`` vem sempre sem; ``fase_desligada``, com ou sem."""


@dataclass(frozen=True, slots=True)
class SeloVigente:
    """O selo lido de ``selo_vigente`` (ou o efetivo de uma pessoa), com os campos do retrato
    (``contrato.RETRATO_DO_SELO``). Cumpre ``contrato.SeloLido``.

    O construtor recusa o que o banco nunca devolveria e que a ficha recusaria (``fichas_guarda``): nivel fora de 1 a
    3; nivel 2 ou 3 sem ``vigente`` ou sem teste, datas e modo; ``vigente`` no nivel 1; motivo fora do vocabulario;
    datas sem fuso; ``nunca_testado`` com teste; outro motivo de nivel 1 (``reprovado``, ``vencido``...) sem teste.
    """

    nivel: int
    teste_id: uuid.UUID | None
    testado_em: datetime | None
    vale_ate: datetime | None
    modo: str | None
    motivo: str

    def __post_init__(self) -> None:
        if isinstance(self.nivel, bool) or self.nivel not in (1, 2, 3):
            raise ValueError(f"nivel do selo fora de 1 a 3: {self.nivel!r}")
        if self.motivo not in contrato.MOTIVOS_DO_RETRATO:
            raise ValueError(f"motivo do selo fora do vocabulario: {self.motivo!r}")
        if (self.nivel > 1) != (self.motivo == "vigente"):
            raise ValueError("so o selo vigente tem nivel 2 ou 3, e todo selo vigente tem nivel 2 ou 3")
        if self.teste_id is not None and not isinstance(self.teste_id, uuid.UUID):
            raise ValueError("teste_id do selo e um uuid")
        if self.modo is not None and self.modo not in contrato.MODOS:
            raise ValueError(f"modo do selo fora do contrato: {self.modo!r}")
        for data in (self.testado_em, self.vale_ate):
            if data is not None and (not isinstance(data, datetime) or data.utcoffset() is None):
                raise ValueError("as datas do selo sao datetime com fuso")
        com_teste = (self.teste_id, self.testado_em, self.vale_ate, self.modo)
        if self.teste_id is None:
            if any(x is not None for x in com_teste):
                raise ValueError("selo sem teste nao tem datas nem modo")
            if self.motivo not in _SEM_TESTE:
                raise ValueError(f"o motivo {self.motivo} vem sempre de um teste")
        else:
            if any(x is None for x in com_teste):
                raise ValueError("selo com teste tem as datas e o modo do teste")
            if self.motivo == "nunca_testado":
                raise ValueError("nunca_testado nao tem teste")

    @classmethod
    def de_linha(cls, linha: Row[Any] | Any) -> SeloVigente:
        """De uma linha de ``selo_vigente()`` ou de ``selos_vigentes`` (nivel, teste_id, testado_em, vale_ate, modo,
        motivo)."""
        teste_id = linha.teste_id
        return cls(
            int(linha.nivel),
            None if teste_id is None else uuid.UUID(str(teste_id)),
            linha.testado_em,
            linha.vale_ate,
            linha.modo,
            str(linha.motivo),
        )

    @classmethod
    def de_retrato(cls, dados: Mapping[str, Any]) -> SeloVigente:
        """De um retrato guardado (``fichas.selo``, ``dados.selo`` do extrato), com exatamente os campos de
        ``contrato.RETRATO_DO_SELO``. Levanta ``ValueError`` em retrato fora da forma."""
        if not isinstance(dados, Mapping) or set(dados) != set(contrato.RETRATO_DO_SELO):
            raise ValueError("o retrato do selo tem exatamente os campos do contrato")
        nivel, teste_id = dados["nivel"], dados["teste_id"]
        forma_certa = type(nivel) is int and (teste_id is None or type(teste_id) is str)
        if not forma_certa:
            raise ValueError("o nivel do retrato e um numero, e o teste_id e texto ou nulo")
        return cls(
            nivel,
            None if teste_id is None else uuid.UUID(teste_id),
            _data_do_retrato(dados["testado_em"]),
            _data_do_retrato(dados["vale_ate"]),
            dados["modo"],
            dados["motivo"],
        )

    def retrato(self) -> dict[str, Any]:
        """O retrato do selo para ``fichas.selo`` e ``dados.selo`` do extrato: exatamente os campos de
        ``contrato.RETRATO_DO_SELO``, com as datas em ISO 8601 com fuso e com os microssegundos lidos do banco (o
        banco as compara por igualdade na ficha) e o ``teste_id`` em texto."""
        return {
            "nivel": self.nivel,
            "teste_id": None if self.teste_id is None else str(self.teste_id),
            "testado_em": None if self.testado_em is None else self.testado_em.isoformat(),
            "vale_ate": None if self.vale_ate is None else self.vale_ate.isoformat(),
            "modo": self.modo,
            "motivo": self.motivo,
        }


def _data_do_retrato(valor: Any) -> datetime | None:
    if valor is None:
        return None
    texto = valor if type(valor) is str else None
    if texto is None:
        raise ValueError("as datas do retrato sao texto ISO 8601")
    return datetime.fromisoformat(texto)


NIVEL_1_SEM_TESTE: Final = SeloVigente(1, None, None, None, None, "nunca_testado")
"""O padrao de ``Sistema.selo_vigente`` para quem monta o ``Sistema`` a mao (os testes) e para
``repositorio.sistema_por_id`` sem ``com_selo``: nivel 1, que nao promete nada. A vitrine e a porta leem o selo do
banco (``cadastro.repositorio``)."""

FASE_DESLIGADA: Final = SeloVigente(1, None, None, None, None, contrato.MOTIVO_FASE_DESLIGADA)
"""O efetivo de uma pessoa real com ``FEATURE_PHASE4_BADGES`` desligada: nivel 1, sem teste nem datas, para que nada
(ficha, extrato, ``_meta``) sugira um teste que nao vale para ela."""


def efetivo(vigente: contrato.SeloLido, *, ligada: bool, pessoa_teste: bool) -> SeloVigente:
    """O selo efetivo de uma operacao (``contrato.CalculaEfetivo``): o vigente quando ``FEATURE_PHASE4_BADGES``
    esta ligada (``ligada``) ou a pessoa e de teste (``pessoa_teste``); ``FASE_DESLIGADA`` nos outros casos. So
    ``True`` liga: qualquer outro valor conta como desligado (o lado que esconde). Nunca sobe o nivel."""
    if ligada is True or pessoa_teste is True:
        if isinstance(vigente, SeloVigente):
            return vigente
        return SeloVigente(
            vigente.nivel, vigente.teste_id, vigente.testado_em, vigente.vale_ate, vigente.modo, vigente.motivo
        )
    return FASE_DESLIGADA


_CALCULA: Final[contrato.CalculaEfetivo] = efetivo  # o mypy confere a assinatura do contrato


# --- Leitura do banco ------------------------------------------------------------------------------------------


async def ler(c: AsyncConnection, sistema_id: uuid.UUID) -> SeloVigente:
    """O selo de agora do sistema, pela funcao do banco (``selo_vigente(sistema)``, sem ``p_agora``)."""
    linha = (
        await c.execute(
            text("SELECT nivel, teste_id, testado_em, vale_ate, modo, motivo FROM selo_vigente(:s)"), {"s": sistema_id}
        )
    ).one()
    return SeloVigente.de_linha(linha)


async def ler_todos(c: AsyncConnection) -> dict[uuid.UUID, SeloVigente]:
    """O selo de agora de cada sistema do cadastro, pela visao ``selos_vigentes``."""
    linhas = (
        await c.execute(
            text("SELECT sistema_id, nivel, teste_id, testado_em, vale_ate, modo, motivo FROM selos_vigentes")
        )
    ).all()
    return {uuid.UUID(str(linha.sistema_id)): SeloVigente.de_linha(linha) for linha in linhas}


# --- Textos legiveis (telas do ADMIN) --------------------------------------------------------------------------

MOTIVOS_LEGIVEIS: Final[Mapping[str, str]] = {
    "vigente": "Teste de {testado_em}, vale até {vale_ate}.",
    "nunca_testado": "Nunca testado.",
    "reprovado": "O último teste não foi aprovado.",
    "vencido": "O teste venceu em {vale_ate}.",
    "retirado": "O selo foi retirado.",
    "fora_de_ativo": "Voltou ao nível 1: o sistema saiu de ativo.",
    "endereco_mudou": "Voltou ao nível 1: o endereço mudou depois do teste.",
    "tipo_chave_mudou": "Voltou ao nível 1: o tipo de chave mudou depois do teste.",
    "revisao_nova": "Voltou ao nível 1: a revisão do servidor é nova.",
    "ferramenta_nova": "Voltou ao nível 1: uma ferramenta de ação é nova ou mudou depois do teste.",
    "contrato_sumiu": "Voltou ao nível 1: o sistema não lista mais as ferramentas do contrato.",
    contrato.MOTIVO_FASE_DESLIGADA: (
        "Nível 1 para as pessoas reais: os selos de nível 2 e 3 ainda não estão ligados; só pessoas de teste usam o"
        " selo deste sistema."
    ),
}
"""O texto de cada motivo do selo (``contrato.MOTIVOS_DO_RETRATO``), com acento. Com os ``Detalhes`` lidos do banco,
``reprovado``, ``ferramenta_nova`` e ``retirado`` ganham o que falta (quantos casos, qual ferramenta, quando e
por que)."""

RETIRADAS_LEGIVEIS: Final[Mapping[str, str]] = {
    "admin": "retirado pelo ADMIN",
    "se_contradisse": "o sistema deu respostas que não combinam",
    "nao_respondeu_consultas": "o sistema não respondeu às consultas",
}
"""O texto de cada motivo de retirada (``contrato.MOTIVOS_DE_RETIRADA``). O texto livre do ADMIN fica em
``Detalhes.detalhe``, para a tela mostrar a parte."""


@dataclass(frozen=True, slots=True)
class Detalhes:
    """O que o motivo legivel usa alem do selo, lido do banco (``detalhes``).

    ``falhas``: quantos casos deram ``falhou`` no teste reprovado. ``ferramentas``: o nome original das ferramentas de
    acao ativas que o teste nao cobre (novas ou revisadas de novo). ``retirado_em``, ``retirada`` e ``detalhe``: a
    primeira retirada do teste (quando, o motivo do vocabulario e o texto do ADMIN, so na retirada pelo ADMIN)."""

    falhas: int | None = None
    ferramentas: tuple[str, ...] = ()
    retirado_em: datetime | None = None
    retirada: str | None = None
    detalhe: str | None = None


def _data(momento: datetime | None, fuso: str) -> str:
    if momento is None:
        return "data não guardada"
    return momento.astimezone(ZoneInfo(fuso)).strftime("%d/%m/%Y")


def motivo_legivel(selo: contrato.SeloLido, detalhes: Detalhes | None = None, *, fuso: str = FUSO_PADRAO) -> str:
    """O motivo do selo em texto para o ADMIN, com as datas no ``fuso`` (dia, mes e ano). Levanta ``ValueError`` para
    um motivo fora do vocabulario."""
    modelo = MOTIVOS_LEGIVEIS.get(selo.motivo)
    if modelo is None:
        raise ValueError(f"motivo do selo fora do vocabulario: {selo.motivo!r}")
    d = detalhes or Detalhes()
    if selo.motivo == "reprovado" and d.falhas is not None:
        quantos = "1 caso falhou" if d.falhas == 1 else f"{d.falhas} casos falharam"
        return f"O último teste não foi aprovado: {quantos}."
    if selo.motivo == "ferramenta_nova" and d.ferramentas:
        if len(d.ferramentas) == 1:
            return f"Voltou ao nível 1: a ferramenta {d.ferramentas[0]} é nova ou mudou depois do teste."
        nomes = ", ".join(d.ferramentas)
        return f"Voltou ao nível 1: as ferramentas {nomes} são novas ou mudaram depois do teste."
    if selo.motivo == "retirado" and d.retirada is not None:
        por = RETIRADAS_LEGIVEIS.get(d.retirada, "motivo não guardado")
        return f"Retirado em {_data(d.retirado_em, fuso)}: {por}."
    return modelo.format(testado_em=_data(selo.testado_em, fuso), vale_ate=_data(selo.vale_ate, fuso))


def selo_agora_legivel(selo: contrato.SeloLido, detalhes: Detalhes | None = None, *, fuso: str = FUSO_PADRAO) -> str:
    """A linha do cartao "Selo" do ADMIN: "Selo agora: nível {n}." com o motivo (desenho 7.1)."""
    return f"Selo agora: nível {selo.nivel}. {motivo_legivel(selo, detalhes, fuso=fuso)}"


async def detalhes(c: AsyncConnection, sistema_id: uuid.UUID, selo: contrato.SeloLido) -> Detalhes:
    """Le do banco o que o motivo legivel do ``selo`` pede (veja ``Detalhes``). Sem teste ou sem motivo que peca
    detalhe, nada e lido."""
    if selo.teste_id is None:
        return Detalhes()
    if selo.motivo == "reprovado":
        falhas = (
            await c.execute(
                text(
                    "SELECT count(*) FROM testes_selo t CROSS JOIN LATERAL jsonb_array_elements(t.casos) i"
                    " WHERE t.id = :t AND i ->> 'resultado' = 'falhou'"
                ),
                {"t": selo.teste_id},
            )
        ).scalar_one()
        return Detalhes(falhas=int(falhas))
    if selo.motivo == "ferramenta_nova":
        nomes = (
            await c.execute(
                text(
                    "SELECT DISTINCT f.nome_original FROM ferramentas_revisadas f"
                    " WHERE f.id = ANY (selo_acoes_do_sistema(:s))"
                    " AND f.id <> ALL (coalesce((SELECT t.ferramentas FROM testes_selo t WHERE t.id = :t),"
                    " '{}'::uuid[])) ORDER BY f.nome_original"
                ),
                {"s": sistema_id, "t": selo.teste_id},
            )
        ).scalars()
        return Detalhes(ferramentas=tuple(str(n) for n in nomes))
    if selo.motivo == "retirado":
        linha = (
            await c.execute(
                text(
                    "SELECT motivo, detalhe, retirado_em FROM selos_retirados WHERE teste_id = :t"
                    " ORDER BY retirado_em, id LIMIT 1"
                ),
                {"t": selo.teste_id},
            )
        ).first()
        if linha is None:
            return Detalhes()
        return Detalhes(retirado_em=linha.retirado_em, retirada=str(linha.motivo), detalhe=linha.detalhe)
    return Detalhes()


async def motivo_no_banco(c: AsyncConnection, sistema_id: uuid.UUID, *, fuso: str = FUSO_PADRAO) -> str:
    """O cartao "Selo" do ADMIN para o sistema, lido agora do banco (``selo_agora_legivel`` com os detalhes)."""
    selo = await ler(c, sistema_id)
    return selo_agora_legivel(selo, await detalhes(c, sistema_id, selo), fuso=fuso)
