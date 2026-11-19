"""Ponto unico de ligacao com um sistema: ``ligacao_para(sistema)`` (desenho 3.4; ajustes, D1 e A2).

- Sistema proprio: o cliente fixado local, como nas Fases 1 e 2.
- Servidor de terceiros: a sala de visitas (``ClienteSala``), quando existe. Sem sala, nada vai ao terceiro
  (``SemSala``: ``nao_enviado`` com ``SalaFora``; o despachante grava ``sala_fora`` no extrato e conta).
- O cliente local para terceiro so entra por injecao explicita (``local_para_terceiro``), e so em
  desenvolvimento e teste. Fora deles, a injecao e ignorada: o despachante, que abre todas as chaves, nunca fala
  direto com um servidor de terceiros.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from mcp_claro.ligacao import cliente
from mcp_claro.ligacao.cliente import TETO_RESPOSTA_PADRAO, Resposta, Segredo, Sondagem
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
    """Servidor de terceiros sem sala de visitas: nada sai, e o chamador fica sabendo por ``SalaFora``."""

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
