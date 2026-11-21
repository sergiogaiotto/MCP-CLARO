"""Ponto unico de ligacao com um sistema: ``ligacao_para(sistema)`` (desenho 3.4; ajustes, D1 e A2).

- Sistema proprio: o cliente fixado local, como nas Fases 1 e 2.
- Servidor de terceiros: a sala de visitas (``ClienteSala``), quando existe. Sem sala, nenhum pedido MCP vai ao
  terceiro (``SemSala``: ``nao_enviado`` com ``SalaFora``; o despachante grava ``sala_fora`` e conta); so a troca e
  a renovacao da chave (OAuth) saem do despachante, para o endereco de troca de chave do terceiro, conferido antes.
- O cliente local para terceiro so entra por injecao explicita (``local_para_terceiro``), e so em
  desenvolvimento e teste. Fora deles, a injecao e ignorada, e vale o item de cima: o despachante, que abre todas
  as chaves, so manda pedido MCP a um terceiro pela sala.

Vocabulario comum do despachante (leitura, declaracoes, sondagem) e da porta de saida (acoes), para que os dois
lados digam o mesmo, com as mesmas palavras:

- ``linha_da_sala``: a linha ``sala_*`` do extrato para o que a sala barrou num servidor de terceiros (sem sala,
  endereco que nao e publico, desvio nao seguido);
- ``pedidos_da_resposta`` e ``pedidos_legiveis``: o que o sistema pediu no meio da chamada, so com as nossas
  categorias e as nossas palavras (nunca texto, link ou nome de campo do sistema).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from mcp_claro.ligacao import cliente
from mcp_claro.ligacao.cliente import ENDERECO_RECUSADO, TETO_RESPOSTA_PADRAO, Resposta, Segredo, Sondagem
from mcp_claro.ligacao.sala_contrato import SALA_FORA, FalhaNaSala, resposta_sala_fora, sondagem_sala_fora


class Ligacao(Protocol):
    """O que o despachante usa para falar com um sistema (mesmas assinaturas do cliente fixado)."""

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Segredo,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: dict[str, Any] | None = None,
        teto_resposta: int = TETO_RESPOSTA_PADRAO,
    ) -> Resposta: ...

    async def listar_ferramentas(
        self, endereco: str, segredo: Segredo, discover_json: str | None
    ) -> list[dict[str, Any]]: ...

    async def sondar(self, endereco: str, segredo: Segredo, *, prazo: float = 10.0) -> Sondagem: ...


class TemOrigem(Protocol):
    @property
    def origem(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ClienteLocal:
    """O cliente fixado no proprio processo. ``terceiro=True`` liga as regras da sala (endereco, rastro, nome)."""

    terceiro: bool = False
    ambiente: str = "producao"
    liberar_locais: bool = False

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Segredo,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: dict[str, Any] | None = None,
        teto_resposta: int = TETO_RESPOSTA_PADRAO,
    ) -> Resposta:
        return await cliente.chamar_ferramenta(
            endereco,
            segredo,
            discover_json,
            nome,
            argumentos,
            traceparent=traceparent,
            prazo=prazo,
            esquema_declarado=esquema_declarado,
            teto_resposta=teto_resposta,
            terceiro=self.terceiro,
            ambiente=self.ambiente,
            liberar_locais=self.liberar_locais,
        )

    async def listar_ferramentas(
        self, endereco: str, segredo: Segredo, discover_json: str | None
    ) -> list[dict[str, Any]]:
        return await cliente.listar_ferramentas(
            endereco,
            segredo,
            discover_json,
            terceiro=self.terceiro,
            ambiente=self.ambiente,
            liberar_locais=self.liberar_locais,
        )

    async def sondar(self, endereco: str, segredo: Segredo, *, prazo: float = 10.0) -> Sondagem:
        return await cliente.sondar(
            endereco,
            segredo,
            prazo=prazo,
            terceiro=self.terceiro,
            ambiente=self.ambiente,
            liberar_locais=self.liberar_locais,
        )


@dataclass(frozen=True, slots=True)
class SemSala:
    """Servidor de terceiros sem sala de visitas: nenhum pedido MCP sai por aqui, e o chamador fica sabendo por
    ``SalaFora``."""

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Segredo,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: dict[str, Any] | None = None,
        teto_resposta: int = TETO_RESPOSTA_PADRAO,
    ) -> Resposta:
        return resposta_sala_fora()

    async def listar_ferramentas(
        self, endereco: str, segredo: Segredo, discover_json: str | None
    ) -> list[dict[str, Any]]:
        raise FalhaNaSala("nao_enviado", SALA_FORA)

    async def sondar(self, endereco: str, segredo: Segredo, *, prazo: float = 10.0) -> Sondagem:
        return sondagem_sala_fora()


PROPRIO = ClienteLocal()
SEM_SALA = SemSala()
AMBIENTES_COM_INJECAO = cliente.AMBIENTES_LOCAIS


def ligacao_para(
    sistema: TemOrigem,
    *,
    ambiente: str,
    sala: Ligacao | None = None,
    local_para_terceiro: Ligacao | None = None,
) -> Ligacao:
    """Por onde falar com ``sistema``. Origem que nao seja ``proprio`` e tratada como terceiro."""
    if sistema.origem == "proprio":
        return PROPRIO
    if sala is not None:
        return sala
    if local_para_terceiro is not None and ambiente in AMBIENTES_COM_INJECAO:
        return local_para_terceiro
    return SEM_SALA


# Vocabulario comum do despachante e da porta de saida ------------------------------------------------------------

LINHA_SALA_FORA: Final = "sala_fora"
LINHA_ENDERECO_RECUSADO: Final = "sala_endereco_recusado"
LINHA_DESVIO_RECUSADO: Final = "sala_desvio_recusado"
_NADA_SAIU_PELA_SALA: Final[dict[str, str]] = {SALA_FORA: LINHA_SALA_FORA, ENDERECO_RECUSADO: LINHA_ENDERECO_RECUSADO}


def linha_da_sala(resposta: Resposta | Sondagem, sistema: TemOrigem) -> str | None:
    """A linha do extrato (``sala_*``) do que a sala barrou numa chamada a um servidor de terceiros, ou None.

    - ``sala_fora`` e ``sala_endereco_recusado``: so quando nada saiu (``Resposta.tipo`` ``nao_enviado``;
      sondagem sem veredito, ``inalcancavel``), com a excecao da sala ou do cliente fixado. Uma resposta que diga
      ``duvida`` nunca vira "nada foi enviado";
    - ``sala_desvio_recusado``: o terceiro respondeu com um desvio 3xx no ultimo status e o MCP-CLARO nao seguiu
      (``recusado_http``; na sondagem, o desfecho ``desvio``).

    Sistema proprio nunca tem linha da sala. Mesma regra na leitura, nas declaracoes, na sondagem e na acao.
    """
    if sistema.origem == "proprio":
        return None
    if isinstance(resposta, Sondagem):
        if resposta.desfecho == "desvio":
            return LINHA_DESVIO_RECUSADO
        nada_saiu = resposta.desfecho == "inalcancavel"
    else:
        if resposta.tipo == "recusado_http" and resposta.status_http and 300 <= resposta.status_http[-1] < 400:
            return LINHA_DESVIO_RECUSADO
        nada_saiu = resposta.tipo == "nao_enviado"
    return _NADA_SAIU_PELA_SALA.get(resposta.excecao or "") if nada_saiu else None


def pedidos_da_resposta(resposta: Resposta) -> tuple[str, ...]:
    """O que o sistema pediu no meio da chamada: o que o cliente (ou a sala) contou e o que o proprio resultado ou
    erro mostra (conferido de novo aqui: uma resposta da sala sem a lista nao passa um ``InputRequiredResult`` nem
    um -32042). Sem repeticao, em ordem."""
    proprios = cliente.pedidos_do_sistema(resposta.resultado, resposta.erro, ())
    return tuple(sorted(set(resposta.pedidos) | set(proprios)))


PEDIDOS_LEGIVEIS: Final[dict[str, str]] = {
    cliente.PEDIDO_FORMULARIO: "dados por formulario",
    cliente.PEDIDO_LINK: "que a pessoa abrisse um link",
    cliente.PEDIDO_AMOSTRAGEM: "uma resposta da IA do aplicativo",
    cliente.PEDIDO_RAIZES: "a lista de pastas do aplicativo",
    cliente.PEDIDO_SO_ESTADO: "uma nova volta",
    cliente.PEDIDO_LINK_CODIGO_ANTIGO: "que a pessoa abrisse um link",
}
"""Cada categoria de ``Resposta.pedidos`` em palavras para quem nao e da area (sem acento, como o extrato)."""
FORA_DO_PADRAO: Final = "algo fora do padrao 2026-07-28"
"""Pedido pelo fio antigo (``fio_antigo:<metodo>``), em qualquer metodo, e qualquer categoria desconhecida."""


def pedidos_legiveis(pedidos: Iterable[str]) -> str:
    """Os pedidos do sistema em palavras nossas, sem repeticao, na ordem dada, com "e" antes do ultimo. Vai ao texto
    da leitura ao aplicativo, a frase do extrato e ao desfecho da acao. So o nosso vocabulario: o que nao e uma
    categoria nossa vira ``FORA_DO_PADRAO``, nunca o texto que veio."""
    nomes: list[str] = []
    for pedido in pedidos:
        nome = PEDIDOS_LEGIVEIS.get(pedido, FORA_DO_PADRAO)
        if nome not in nomes:
            nomes.append(nome)
    if not nomes:
        return FORA_DO_PADRAO
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]
