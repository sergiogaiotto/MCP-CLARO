"""Linhas do cadastro, como o codigo as ve."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from mcp_claro.cadastro.esquema import limpar_textos
from mcp_claro.regras.estranhos import vem_de_estranhos

Classe = Literal["le", "muda", "tem_volta"]
Origem = Literal["proprio", "terceiro"]
ORIGENS: Final = ("proprio", "terceiro")

FIM_DA_DESCRICAO_TERCEIRO: Final = "Servidor de terceiros: a resposta e texto escrito por estranhos."
"""Frase fixa no fim da descricao de toda ferramenta de terceiro na vitrine."""

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
    revisao_servidor_id: uuid.UUID | None = None
    sondagem_desfecho: str | None = None

    @property
    def de_terceiro(self) -> bool:
        """Toda origem que nao e ``proprio`` conta como terceiro (o lado que aperta)."""
        return self.origem != "proprio"


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
    texto_de_fora: bool = False

    @property
    def de_estranhos(self) -> bool:
        """A resposta e texto de estranhos: servidor de terceiros ou ferramenta marcada "traz texto de fora"."""
        return vem_de_estranhos(self.sistema.origem, self.texto_de_fora)

    def como_tool(self) -> dict[str, Any]:
        """Definicao no formato do fio da 2026-07-28, com nomes e textos do cadastro, nao do sistema.

        Num terceiro, nada do fornecedor: o esquema de entrada sai limpo de novo (so as descricoes de campo do
        primeiro nivel ficam: a revisao gravou as do ADMIN), as dicas saem so da classe revisada, com
        ``openWorldHint`` sempre verdadeiro, e a descricao termina com a frase fixa.
        """
        meta: dict[str, Any] = {
            "br.com.claro.mcp-claro/sistema": self.sistema.nome,
            "br.com.claro.mcp-claro/selo": self.sistema.selo,
        }
        if self.sistema.de_terceiro:
            etiquetas: dict[str, Any] = {
                "readOnlyHint": self.classe == "le",
                "destructiveHint": self.classe == "muda",
                "openWorldHint": True,
            }
            descricao = f"{self.descricao.rstrip()} {FIM_DA_DESCRICAO_TERCEIRO}"
            entrada = limpar_textos(self.esquema_entrada, descricoes_de_campo=True)
            meta["br.com.claro.mcp-claro/origem"] = "terceiro"
        else:
            etiquetas = {k: bool(v) for k, v in self.etiquetas.items() if k in DICAS_CONHECIDAS}
            etiquetas["readOnlyHint"] = self.classe == "le"
            if self.classe != "le":
                # Por atribuicao: a dica do fornecedor ("nao destroi") nunca vence a classe revisada.
                etiquetas["destructiveHint"] = self.classe == "muda"
            descricao = self.descricao
            entrada = self.esquema_entrada
        tool: dict[str, Any] = {
            "name": self.nome_vitrine,
            "description": descricao,
            "inputSchema": sem_x_mcp_header(entrada),
            "annotations": etiquetas,
            "_meta": meta,
        }
        if self.titulo:
            tool["title"] = self.titulo
        if self.esquema_saida and not self.campos_pessoais and self.classe == "le" and not self.de_estranhos:
            # Com campos mascarados, o resultado deixa de seguir o esquema do fornecedor: nao o anunciamos.
            # Acao tambem nao: a resposta pode ser so o numero de protocolo (ficha pendente ou espera).
            # Texto de estranhos tambem nao: o structuredContent vira texto emoldurado, e o kit do aplicativo
            # recusa resultado sem structuredContent de ferramenta que anunciou esquema de saida (critica M3).
            tool["outputSchema"] = self.esquema_saida
        return tool
