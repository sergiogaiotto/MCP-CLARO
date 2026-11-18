"""Regras do despachante: reconfere cada leitura, abre a chave e chama o sistema uma vez.

A porta interna de leituras so atende ferramenta da classe "le" do cadastro
revisado e da ordem de servico. Acao pedida por aqui e recusada e vai ao
extrato, mesmo que a entrada tenha sido alterada de proposito.

Regras da Fase 1 (``_conferir_regras`` e ``_chave_da_pessoa``): bloqueios ativos e
porta de saida antes de abrir a chave; chave por pessoa renovada antes do envio,
fora de qualquer conexao aberta; 401 no envio nao e reenviado.
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
from mcp_claro.despachante import chaves, medida, renovacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios, estado
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP

log = logger("mcp_claro.despachante")


class DeclaracoesIndisponiveis(Exception):
    """O sistema nao respondeu a listagem (sem chave, fora do ar ou resposta invalida)."""


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


_PROPRIO = "__mcp_claro_proprio"


def _resultado_de_erro(texto: str, motivo: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": True,
        "_meta": {PREFIXO_META + "motivo": motivo},
        _PROPRIO: True,
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

    async def _conferir_regras(self, pedido: PedidoLeitura, sistema_id: uuid.UUID, nome: str | None) -> None:
        """Bloqueios ativos (pessoa, aplicativo, par, sistema ou tudo) e porta de saida, antes de abrir a chave."""
        async with self.engine.connect() as c:
            vigentes = await bloqueios.ativos(c, pessoa_id=pedido.pessoa_id, aplicativo_id=pedido.aplicativo_id)
            saida = await estado.ler_saida(c)
        bloqueio = vigentes.para(sistema_id)
        if bloqueio is not None:
            raise await self._recusar(pedido, bloqueio.motivo_recusa(), bloqueio.mensagem(), nome)
        if not saida.aberta:
            raise await self._recusar(pedido, "saida_fechada", estado.MENSAGEM_SAIDA_FECHADA, nome)

    async def _chave_da_pessoa(
        self, pedido: PedidoLeitura, sistema_id: uuid.UUID, sistema_nome: str, nome: str | None
    ) -> renovacao.ChaveAberta:
        """Chave por pessoa pronta para o envio: renovada antes, com trava, se venceu ou vai vencer."""
        try:
            return await renovacao.chave_pronta(
                self.engine, self.cifra, sistema_id=sistema_id, pessoa_id=pedido.pessoa_id
            )
        except chaves.ChaveAusente:
            raise await self._recusar(pedido, "sem_chave", "Voce ainda nao autorizou este sistema.", nome) from None
        except renovacao.AutorizacaoVencida:
            raise await self._recusar(
                pedido,
                "autorizacao_vencida",
                f"Sua autorizacao no sistema {sistema_nome} venceu. Autorize de novo nas telas do MCP-CLARO. "
                "Nada foi enviado.",
                nome,
            ) from None
        except renovacao.RenovacaoIndisponivel:
            raise await self._recusar(
                pedido,
                "renovacao_indisponivel",
                f"Nao deu para renovar a sua chave no sistema {sistema_nome} agora. Nada foi enviado. "
                "Tente de novo em instantes.",
                nome,
            ) from None

    async def ler(self, pedido: PedidoLeitura) -> dict[str, Any]:
        async with self.engine.connect() as c:
            ferramenta = await repositorio.ferramenta_da_ordem(
                c,
                pessoa_id=pedido.pessoa_id,
                aplicativo_id=pedido.aplicativo_id,
                ordem_id=pedido.ordem_id,
                ferramenta_id=pedido.ferramenta_id,
                versao=VERSAO_MCP,
            )
            pessoa = (
                await c.execute(text("SELECT situacao FROM pessoas WHERE id = :p"), {"p": pedido.pessoa_id})
            ).scalar_one_or_none()
            app = (
                await c.execute(text("SELECT situacao FROM aplicativos WHERE id = :a"), {"a": pedido.aplicativo_id})
            ).scalar_one_or_none()
        if ferramenta is None:
            # Vale so a versao revisada vigente ('ativa'), de modelo ativo, na ordem vigente da pessoa e do
            # aplicativo, de sistema ativo na 2026-07-28: a mesma regra da vitrine.
            raise await self._recusar(pedido, "fora_da_ordem", "Ferramenta fora da ordem de servico.", None)
        nome = ferramenta.nome_vitrine
        if pessoa != "ativa" or app != "ativo":
            raise await self._recusar(pedido, "bloqueado", "Pessoa ou aplicativo bloqueado ou pausado.", nome)
        if ferramenta.classe != "le":
            raise await self._recusar(
                pedido, "acao_pela_porta_de_leitura", "A porta de leitura so atende leituras.", nome
            )
        sistema = ferramenta.sistema
        if sistema.situacao != "ativo" or sistema.versao_mcp != VERSAO_MCP:
            raise await self._recusar(pedido, "sistema_indisponivel", "O sistema nao esta ativo na 2026-07-28.", nome)
        await self._conferir_regras(pedido, sistema.id, nome)
        aberta = (
            await self._chave_da_pessoa(pedido, sistema.id, sistema.nome, nome)
            if sistema.tipo_chave == "por_pessoa"
            else None
        )

        async with self.engine.connect() as c:
            discover_json = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema.id})
            ).scalar_one_or_none()
            declarado = await repositorio.esquema_declarado(c, sistema.id, ferramenta.nome_original)
            try:
                if sistema.tipo_chave == "por_pessoa":
                    chave = aberta.segredo if aberta else None
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
            esquema_declarado=declarado,
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
        elif resposta.tipo == "recusado_http" and 401 in resposta.status_http and aberta is not None:
            await renovacao.marcar_vencida(
                self.engine, sistema_id=sistema.id, pessoa_id=pedido.pessoa_id, cifrada=aberta.cifrada
            )
            resultado = _resultado_de_erro(
                f"O sistema {sistema.nome} recusou a sua chave. Nada foi reenviado. Tente de novo: o MCP-CLARO "
                "renova a chave antes; se nao der, autorize de novo nas telas.",
                "chave_recusada",
            )
        else:
            resultado = _resultado_de_erro(
                f"O sistema {sistema.nome} nao respondeu a leitura ({resposta.tipo}).",
                "falha_" + resposta.tipo,
            )
        # O _meta do sistema nao passa ao aplicativo: pode trazer dado pessoal, instrucao ou chave forjada
        # com o nosso prefixo. So vale o que o proprio MCP-CLARO escreveu (resultado de erro montado aqui).
        meta: dict[str, Any] = dict(resultado.get("_meta") or {}) if resultado.get(_PROPRIO) else {}
        resultado.pop(_PROPRIO, None)
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
                        # So pendente ou recusado viram ativo: quarentena e desativado continuam como estao.
                        "UPDATE sistemas SET versao_mcp = :v, sondagem = CAST(:s AS jsonb), sondado_em = now(),"
                        " situacao = CASE WHEN situacao IN ('pendente', 'recusado') THEN 'ativo' ELSE situacao END,"
                        " motivo = CASE WHEN situacao IN ('pendente', 'recusado') THEN NULL ELSE motivo END,"
                        " atualizado_em = now() WHERE id = :i"
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
                        "UPDATE sistemas SET situacao = CASE WHEN situacao = 'desativado' THEN situacao"
                        " ELSE 'recusado' END, versao_mcp = NULL, motivo = :m, sondado_em = now(),"
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
        if segredo is None:
            # Sem chave dada (pedido das rotinas): a chave propria de leitura das declaracoes, guardada pelo
            # ADMIN; num sistema de chave compartilhada, a propria chave compartilhada.
            async with self.engine.connect() as c:
                for tipo in ("declaracoes", "acesso") if sistema.tipo_chave == "compartilhada" else ("declaracoes",):
                    try:
                        segredo = await chaves.abrir(c, self.cifra, sistema_id=sistema_id, tipo=tipo)
                        break
                    except chaves.ChaveAusente:
                        continue
        chave_usada = segredo

        async def obter() -> str | None:
            return chave_usada

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        try:
            ferramentas = await cliente.listar_ferramentas(sistema.endereco, obter, sondagem)
        except Exception as erro:
            raise DeclaracoesIndisponiveis(f"o sistema {sistema.nome} nao entregou as declaracoes") from erro
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
