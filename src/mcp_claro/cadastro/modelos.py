"""Linhas do cadastro, como o codigo as ve."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Classe = Literal["le", "muda", "tem_volta"]

DICAS_CONHECIDAS = frozenset({"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
"""So estas dicas do fornecedor passam a vitrine; titulo e textos vem do cadastro revisado."""


def sem_x_mcp_header(esquema: Any) -> Any:
    """Tira as anotacoes x-mcp-header: o salto do aplicativo termina na entrada; a saida as recria."""
    if isinstance(esquema, dict):
        return {k: sem_x_mcp_header(v) for k, v in esquema.items() if k != "x-mcp-header"}
    if isinstance(esquema, list):
        return [sem_x_mcp_header(v) for v in esquema]
    return esquema


TipoChave = Literal["por_pessoa", "compartilhada", "programa"]


@dataclass(frozen=True, slots=True)
class Aplicativo:
    id: uuid.UUID
    client_id: str
    nome: str
    familia: str
    tipo: str
    situacao: str


@dataclass(frozen=True, slots=True)
class Pessoa:
    id: uuid.UUID
    emissor: str
    sujeito: str
    nome: str | None
    papel: str
    situacao: str
    teste: bool


@dataclass(frozen=True, slots=True)
class Sistema:
    id: uuid.UUID
    nome: str
    endereco: str
    origem: str
    tipo_chave: TipoChave
    selo: int
    situacao: str
    versao_mcp: str | None


@dataclass(frozen=True, slots=True)
class FerramentaVitrine:
    """Uma ferramenta revisada, como entra na vitrine de uma pessoa."""

    id: uuid.UUID
    sistema: Sistema
    nome_original: str
    versao: int
    nome_vitrine: str
    titulo: str | None
    descricao: str
    esquema_entrada: dict[str, Any]
    esquema_saida: dict[str, Any] | None
    etiquetas: dict[str, Any]
    classe: Classe
    risco: str
    campos_pessoais: tuple[str, ...] = field(default_factory=tuple)
    ordem_id: uuid.UUID | None = None

    def como_tool(self) -> dict[str, Any]:
        """Definicao no formato do fio da 2026-07-28, com nomes e textos do cadastro, nao do sistema."""
        etiquetas = {k: bool(v) for k, v in self.etiquetas.items() if k in DICAS_CONHECIDAS}
        etiquetas["readOnlyHint"] = self.classe == "le"
        if self.classe != "le":
            etiquetas.setdefault("destructiveHint", self.classe == "muda")
        tool: dict[str, Any] = {
            "name": self.nome_vitrine,
            "description": self.descricao,
            "inputSchema": sem_x_mcp_header(self.esquema_entrada),
            "annotations": etiquetas,
            "_meta": {
                "br.com.claro.mcp-claro/sistema": self.sistema.nome,
                "br.com.claro.mcp-claro/selo": self.sistema.selo,
            },
        }
        if self.titulo:
            tool["title"] = self.titulo
        if self.esquema_saida and not self.campos_pessoais:
            # Com campos mascarados, o resultado deixa de seguir o esquema do fornecedor: nao o anunciamos.
            tool["outputSchema"] = self.esquema_saida
        return tool
