"""Regras do despachante: reconfere cada leitura, abre a chave e chama o sistema uma vez.

A porta interna de leituras so atende ferramenta da classe "le" do cadastro
revisado e da ordem de servico. Acao pedida por aqui e recusada e vai ao
extrato, mesmo que a entrada tenha sido alterada de proposito.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.impressao import impressao
from mcp_claro.despachante import chaves, medida
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP

log = logger("mcp_claro.despachante")


class LeituraRecusada(Exception):
    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


@dataclass(frozen=True, slots=True)
class PedidoLeitura:
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ordem_id: uuid.UUID
    ferramenta_id: uuid.UUID
    argumentos: dict[str, Any]
    trace_id: str | None = None

    @classmethod
    def de_json(cls, dados: dict[str, Any]) -> PedidoLeitura:
        return cls(
            pessoa_id=uuid.UUID(str(dados["pessoa_id"])),
            aplicativo_id=uuid.UUID(str(dados["aplicativo_id"])),
            ordem_id=uuid.UUID(str(dados["ordem_id"])),
            ferramenta_id=uuid.UUID(str(dados["ferramenta_id"])),
            argumentos=dict(dados.get("argumentos") or {}),
            trace_id=dados.get("trace_id"),
        )


def _resultado_de_erro(texto: str, motivo: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": True,
        "_meta": {PREFIXO_META + "motivo": motivo},
    }


class Despachante:
    def __init__(self, engine: AsyncEngine, cifra: Cifra, *, prazo_leitura: float = 30.0) -> None:
        self.engine = engine
        self.cifra = cifra
        self.prazo_leitura = prazo_leitura

    async def _recusar(
        self, pedido: PedidoLeitura, motivo: str, mensagem: str, ferramenta: str | None
    ) -> LeituraRecusada:
        async with self.engine.begin() as c:
            await registrar(
                c,
                papel="despachante",
                tipo="leitura_recusada",
                campos={"ferramenta": ferramenta or "?", "motivo": motivo},
                aplicativo_id=pedido.aplicativo_id,
                pessoa_id=pedido.pessoa_id,
                ferramenta=ferramenta,
                trace_id=pedido.trace_id,
            )
        CONTADORES.somar("leituras_recusadas_despachante", motivo=motivo)
        return LeituraRecusada(motivo, mensagem)

    async def ler(self, pedido: PedidoLeitura) -> dict[str, Any]:
        async with self.engine.connect() as c:
            ferramenta = await repositorio.ferramenta_por_id(c, pedido.ferramenta_id)
            ordem_ok = await repositorio.ordem_valida(c, pedido.ordem_id, pedido.pessoa_id, pedido.aplicativo_id)
            na_ordem = (
                await c.execute(
                    text(
                        "SELECT 1 FROM ordens_servico o"
                        " JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = o.modelo_id"
                        " JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id"
                        " AND f.nome_original = mf.nome_original "
                        "WHERE o.id = :o AND f.id = :f"
                    ),
                    {"o": pedido.ordem_id, "f": pedido.ferramenta_id},
                )
            ).first() is not None
            pessoa = (
                await c.execute(text("SELECT situacao FROM pessoas WHERE id = :p"), {"p": pedido.pessoa_id})
            ).scalar_one_or_none()
            app = (
                await c.execute(text("SELECT situacao FROM aplicativos WHERE id = :a"), {"a": pedido.aplicativo_id})
            ).scalar_one_or_none()
        nome = ferramenta.nome_vitrine if ferramenta else None
        if ferramenta is None or not ordem_ok or not na_ordem:
            raise await self._recusar(pedido, "fora_da_ordem", "Ferramenta fora da ordem de servico.", nome)
        if pessoa != "ativa" or app != "ativo":
            raise await self._recusar(pedido, "bloqueado", "Pessoa ou aplicativo bloqueado ou pausado.", nome)
        if ferramenta.classe != "le":
            raise await self._recusar(
                pedido, "acao_pela_porta_de_leitura", "A porta de leitura so atende leituras.", nome
            )
        sistema = ferramenta.sistema
        if sistema.situacao != "ativo" or sistema.versao_mcp != VERSAO_MCP:
            raise await self._recusar(pedido, "sistema_indisponivel", "O sistema nao esta ativo na 2026-07-28.", nome)

        async with self.engine.connect() as c:
            discover_json = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema.id})
            ).scalar_one_or_none()
            try:
                if sistema.tipo_chave == "por_pessoa":
                    chave = await chaves.abrir(c, self.cifra, sistema_id=sistema.id, pessoa_id=pedido.pessoa_id)
                elif sistema.tipo_chave == "compartilhada":
                    chave = await chaves.abrir(c, self.cifra, sistema_id=sistema.id)
                else:
                    chave = await chaves.abrir(c, self.cifra, sistema_id=sistema.id, aplicativo_id=pedido.aplicativo_id)
            except chaves.ChaveAusente:
                raise await self._recusar(pedido, "sem_chave", "Voce ainda nao autorizou este sistema.", nome) from None

        async def segredo() -> str | None:
            return chave

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        resposta = await cliente.chamar_ferramenta(
            sistema.endereco,
            segredo,
            sondagem,
            ferramenta.nome_original,
            pedido.argumentos,
            traceparent=None,
            prazo=self.prazo_leitura,
        )
        if resposta.tipo == "resultado" and resposta.resultado is not None:
            bruto = resposta.resultado.model_dump(by_alias=True, exclude_none=True, mode="json")
            if bruto.get("resultType") == "input_required":
                resultado = _resultado_de_erro(
                    "O sistema pediu dados no meio da leitura. O MCP-CLARO nao repassa esses pedidos.",
                    "sistema_pediu_dados",
                )
            else:
                resultado = medida.aplicar(bruto, ferramenta.campos_pessoais)
                resultado.pop("resultType", None)
        else:
            resultado = _resultado_de_erro(
                f"O sistema {sistema.nome} nao respondeu a leitura ({resposta.tipo}).",
                "falha_" + resposta.tipo,
            )
        meta = dict(resultado.get("_meta") or {})
        meta.pop("io.modelcontextprotocol/serverInfo", None)
        meta[PREFIXO_META + "sistema"] = sistema.nome
        meta[PREFIXO_META + "selo"] = sistema.selo
        if sistema.tipo_chave == "compartilhada":
            meta[PREFIXO_META + "aviso"] = "Sistema de chave compartilhada: ele nao distingue as pessoas."
        resultado["_meta"] = meta
        async with self.engine.begin() as c:
            await registrar(
                c,
                papel="despachante",
                tipo="leitura_feita",
                campos={"ferramenta": ferramenta.nome_vitrine},
                aplicativo_id=pedido.aplicativo_id,
                pessoa_id=pedido.pessoa_id,
                sistema_id=sistema.id,
                ferramenta=ferramenta.nome_vitrine,
                dados={"desfecho": resposta.tipo, "com_erro": bool(resultado.get("isError"))},
                trace_id=pedido.trace_id,
            )
        return resultado

    async def sondar_sistema(self, sistema_id: uuid.UUID, segredo: str | None) -> cliente.Sondagem:
        """Sondagem de versao no cadastro. Sistema que nao fala a 2026-07-28 e recusado e contado."""
        async with self.engine.connect() as c:
            sistema = await repositorio.sistema_por_id(c, sistema_id)
        if sistema is None:
            raise LookupError("sistema desconhecido")

        async def obter() -> str | None:
            return segredo

        sondagem = await cliente.sondar(sistema.endereco, obter)
        async with self.engine.begin() as c:
            if sondagem.aceito:
                await c.execute(
                    text(
                        "UPDATE sistemas SET versao_mcp = :v, sondagem = CAST(:s AS jsonb), sondado_em = now(),"
                        " situacao = 'ativo', motivo = NULL, atualizado_em = now() WHERE id = :i"
                    ),
                    {"v": VERSAO_MCP, "s": sondagem.discover_json, "i": sistema_id},
                )
            elif sondagem.inalcancavel:
                await c.execute(
                    text("UPDATE sistemas SET motivo = :m, sondado_em = now(), atualizado_em = now() WHERE id = :i"),
                    {"m": sondagem.motivo, "i": sistema_id},
                )
            else:
                await c.execute(
                    text(
                        "UPDATE sistemas SET situacao = 'recusado', versao_mcp = NULL, motivo = :m, sondado_em = now(),"
                        " atualizado_em = now() WHERE id = :i"
                    ),
                    {"m": sondagem.motivo, "i": sistema_id},
                )
                CONTADORES.recusa_versao("sistema", sistema.nome, "geracao-antiga", "cadastro")
                await registrar(
                    c,
                    papel="despachante",
                    tipo="recusa_versao_sistema",
                    campos={
                        "versao_pedida": ",".join(sondagem.versoes) or "geracao antiga",
                        "aceita": VERSAO_MCP,
                    },
                    sistema_id=sistema_id,
                    dados={"motivo": sondagem.motivo, "codigo": sondagem.codigo},
                )
        return sondagem

    async def ler_declaracoes(self, sistema_id: uuid.UUID, segredo: str | None) -> list[dict[str, Any]]:
        """Le o que o sistema declara hoje e guarda em ``ferramentas_vistas`` (base do mudou-parou)."""
        async with self.engine.connect() as c:
            sistema = await repositorio.sistema_por_id(c, sistema_id)
            discover_json = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema_id})
            ).scalar_one_or_none()
        if sistema is None or sistema.situacao not in ("ativo", "quarentena"):
            raise LookupError("sistema inativo")

        async def obter() -> str | None:
            return segredo

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        ferramentas = await cliente.listar_ferramentas(sistema.endereco, obter, sondagem)
        async with self.engine.begin() as c:
            for f in ferramentas:
                await c.execute(
                    text(
                        "INSERT INTO ferramentas_vistas (sistema_id, nome, definicao, impressao) "
                        "VALUES (:s, :n, CAST(:d AS jsonb), :i) ON CONFLICT (sistema_id, nome) DO UPDATE "
                        "SET definicao = EXCLUDED.definicao, impressao = EXCLUDED.impressao, vista_em = now()"
                    ),
                    {"s": sistema_id, "n": f["name"], "d": json.dumps(f), "i": impressao(f)},
                )
        return ferramentas
