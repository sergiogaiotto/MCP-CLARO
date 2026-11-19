"""Despachante unico da porta de saida.

Uma copia do papel por vez segura a trava de sessao ``DESPACHANTE_UNICO`` numa conexao propria, fora do
pool (a conexao e descartada no fim, e a trava vai junto). Ao assumir, toda acao que ficou "enviando"
vira "nao confirmada": pode ter chegado ao sistema, e nada e reenviado.

Para cada acao liberada e na hora:

1. travas consultivas compartilhadas do alcance (tudo, sistema, aplicativo, pessoa) e a linha da fila;
2. reconferencia: saida aberta, pessoa e aplicativo ativos, sem bloqueio, ordem e ferramenta vigentes,
   ficha confirmada (ou limite gasto) e codigo de conferencia igual ao que vai seguir;
3. marca "enviando" e grava, antes de qualquer byte sair;
4. abre a chave (renovada antes, se for por pessoa) e faz exatamente uma chamada ao sistema;
5. grava o fim: enviada, nao saiu, sistema pediu dados ou nao confirmada. Nunca reenvia.

O despachante acorda pelo ``LISTEN mcp_claro_saida`` e, de reserva, por varredura.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import mcp_types as types
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import FerramentaVitrine
from mcp_claro.despachante import chaves, medida, renovacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios, estado
from mcp_claro.saida import fila, travas
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import Operacao
from mcp_claro.saida.protocolo import codigo_conferencia
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.saida.despachante")

CANAL = "mcp_claro_saida"

# Erros JSON-RPC que o sistema devolve antes de executar qualquer coisa: a acao nao aconteceu.
_ANTES_DE_EXECUTAR = {
    types.METHOD_NOT_FOUND,
    types.INVALID_PARAMS,
    types.INVALID_REQUEST,
    types.PARSE_ERROR,
    types.MISSING_REQUIRED_CLIENT_CAPABILITY,
}

Chamador = Callable[..., Awaitable[cliente.Resposta]]


class PortaDeSaida:
    def __init__(
        self,
        engine: AsyncEngine,
        cifra: Cifra,
        *,
        prazo_envio: float = 30.0,
        varredura: float = 5.0,
        acoes_de_programas: bool = False,
        limite_exige_passkey: bool = True,
        chamar: Chamador | None = None,
    ) -> None:
        self.engine = engine
        self.cifra = cifra
        self.prazo_envio = prazo_envio
        self.varredura = varredura
        self.acoes_de_programas = acoes_de_programas
        self.limite_exige_passkey = limite_exige_passkey
        self.chamar: Chamador = chamar or cliente.chamar_ferramenta
        self.assumiu = asyncio.Event()
        self._acordar = asyncio.Event()

    # Ciclo

    async def rodar(self) -> None:
        """Roda ate ser cancelada. Sem a trava, espera a vez; com erro, recomeca depois da varredura."""
        while True:
            try:
                await self._turno()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("porta_de_saida_falhou")
            self.assumiu.clear()
            await asyncio.sleep(self.varredura)

    async def _turno(self) -> None:
        async with self.engine.connect() as dono:
            bruta = await dono.get_raw_connection()
            pg: Any = bruta.driver_connection
            try:
                if not await pg.fetchval("SELECT pg_try_advisory_lock($1)", travas.DESPACHANTE_UNICO):
                    return
                await pg.add_listener(CANAL, self._aviso)
                await self.assumir(dono)
                while True:
                    await pg.fetchval("SELECT 1")  # a trava segue nossa enquanto a conexao vive
                    if not await self.enviar_uma(dono):
                        await self._esperar()
            finally:
                # A conexao nao volta ao pool: com ela vai a trava de sessao e o LISTEN.
                await dono.invalidate()

    def _aviso(self, *_: object) -> None:
        self._acordar.set()

    async def _esperar(self) -> None:
        async with self.engine.connect() as c:
            falta = (
                await c.execute(
                    text("SELECT extract(epoch FROM min(hora_saida) - now()) FROM fila_saida WHERE estado = 'liberada'")
                )
            ).scalar_one_or_none()
        espera = self.varredura if falta is None else max(0.0, min(self.varredura, float(falta)))
        if espera <= 0:
            espera = 0.05
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._acordar.wait(), timeout=espera)
        self._acordar.clear()

    @contextlib.asynccontextmanager
    async def _transacao(self, dono: AsyncConnection | None) -> AsyncIterator[AsyncConnection]:
        """Transacao na conexao que segura a trava (se houver): se ela cair, a trava some e a gravacao falha."""
        if dono is None:
            async with self.engine.begin() as c:
                yield c
        else:
            async with dono.begin():
                yield dono

    async def assumir(self, dono: AsyncConnection | None = None) -> list[str]:
        async with self._transacao(dono) as c:
            retomadas = await fila.retomar_enviando(c, quem="despachante:retomada")
            await registrar(c, papel="despachante", tipo="despachante_assumiu", campos={"retomadas": len(retomadas)})
        self.assumiu.set()
        log.info("porta_de_saida_assumida", retomadas=len(retomadas))
        return retomadas

    # Uma acao

    async def enviar_uma(self, dono: AsyncConnection | None = None) -> bool:
        """Processa a proxima acao na hora. Devolve ``False`` quando nao ha o que fazer agora.

        ``dono`` e a conexao que segura a trava de despachante unico: a reconferencia e a marca "enviando"
        sao gravadas nela. O fim do envio vai por outra conexao, para a resposta ficar guardada mesmo se a
        trava cair no meio (ai ela so entra no extrato e na tela de decisao).
        """
        async with self.engine.connect() as c:
            if not (await estado.ler_saida(c)).aberta:
                return False
            candidata = (
                await c.execute(
                    text(
                        "SELECT f.id, o.pessoa_id, o.aplicativo_id, o.sistema_id FROM fila_saida f"
                        " JOIN operacoes o ON o.id = f.operacao_id"
                        " WHERE f.estado = 'liberada' AND f.hora_saida <= now() ORDER BY f.hora_saida, f.id LIMIT 1"
                    )
                )
            ).first()
        if candidata is None:
            return False
        async with self._transacao(dono) as c:
            await travas.compartilhar_envio(c, candidata.pessoa_id, candidata.aplicativo_id, candidata.sistema_id)
            if not (await estado.ler_saida(c)).aberta:
                return False
            travada = (
                await c.execute(
                    text(
                        "SELECT id FROM fila_saida WHERE id = :i AND estado = 'liberada' AND hora_saida <= now()"
                        " FOR UPDATE SKIP LOCKED"
                    ),
                    {"i": candidata.id},
                )
            ).scalar_one_or_none()
            if travada is None:
                return True
            op = await fila.por_fila(c, candidata.id)
            if op is None:
                return True
            conferida = await self._conferir(c, op)
            if isinstance(conferida, tuple):
                estado_final, motivo = conferida
                await fila.parar(c, op, estado_final, motivo)
                CONTADORES.somar("acoes_paradas", motivo=motivo)
                return True
            ferramenta = conferida
            await fila.marcar_enviando(c, op)
        final, resultado, desfecho = await self._enviar(op, ferramenta)
        async with self.engine.begin() as c:
            await fila.finalizar(c, op, final, resultado, desfecho)
        CONTADORES.somar("acoes", estado=str(final), sistema=ferramenta.sistema.nome)
        return True

    async def _conferir(self, c: Any, op: Operacao) -> FerramentaVitrine | tuple[Estado, str]:
        situacoes = (
            await c.execute(
                text(
                    "SELECT (SELECT situacao FROM pessoas WHERE id = :p) AS pessoa,"
                    " (SELECT situacao FROM aplicativos WHERE id = :a) AS app,"
                    " (SELECT tipo FROM aplicativos WHERE id = :a) AS tipo_app"
                ),
                {"p": op.pessoa_id, "a": op.aplicativo_id},
            )
        ).one()
        if situacoes.pessoa != "ativa" or situacoes.app != "ativo":
            return Estado.PARADA_POR_BLOQUEIO, "pessoa ou aplicativo parado"
        vigentes = await bloqueios.ativos(c, pessoa_id=op.pessoa_id, aplicativo_id=op.aplicativo_id)
        bloqueio = vigentes.para(op.sistema_id)
        if bloqueio is not None:
            return Estado.PARADA_POR_BLOQUEIO, bloqueio.motivo_recusa()
        if situacoes.tipo_app == "programa" and not self.acoes_de_programas:
            return Estado.CANCELADA, "programas so leem nesta instalacao"
        ferramenta = await repositorio.ferramenta_da_ordem(
            c,
            pessoa_id=op.pessoa_id,
            aplicativo_id=op.aplicativo_id,
            ordem_id=op.ordem_id,
            ferramenta_id=op.ferramenta_id,
            versao=VERSAO_MCP,
        )
        if ferramenta is None or ferramenta.classe == "le":
            return Estado.CANCELADA, "a ordem de servico ou a ferramenta mudou"
        sistema = ferramenta.sistema
        if (
            sistema.id != op.sistema_id
            or sistema.situacao != "ativo"
            or sistema.versao_mcp != VERSAO_MCP
            or sistema.tipo_chave == "compartilhada"
        ):
            return Estado.CANCELADA, "o sistema nao esta ativo para acoes"
        codigo = codigo_conferencia(sistema.id, ferramenta.nome_original, op.argumentos)
        if codigo != op.codigo_conferencia:
            return Estado.CANCELADA, "o codigo de conferencia nao bate"
        if op.ficha_id is not None:
            ficha = (
                await c.execute(
                    text("SELECT decisao, codigo_conferencia FROM fichas WHERE id = :f"), {"f": op.ficha_id}
                )
            ).one()
            if ficha.decisao != "confirmada" or ficha.codigo_conferencia != codigo:
                return Estado.CANCELADA, "a ficha nao foi confirmada para este pedido"
        elif op.limite_id is not None:
            limite = (
                await c.execute(
                    text(
                        "SELECT pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, com_passkey"
                        " FROM limites WHERE id = :l"
                    ),
                    {"l": op.limite_id},
                )
            ).first()
            if (
                limite is None
                or (limite.pessoa_id, limite.aplicativo_id, limite.sistema_id)
                != (op.pessoa_id, op.aplicativo_id, op.sistema_id)
                or limite.nome_original != ferramenta.nome_original
                or not fila.cobre(limite.destinatarios, op.argumentos)
                or (self.limite_exige_passkey and not limite.com_passkey)
            ):
                return Estado.CANCELADA, "o limite nao cobre este pedido"
        else:
            return Estado.CANCELADA, "sem ficha nem limite"
        return ferramenta

    async def _chave(self, op: Operacao, ferramenta: FerramentaVitrine) -> tuple[str | None, Any, str | None]:
        """(segredo, chave aberta por pessoa, motivo de nao sair)."""
        sistema = ferramenta.sistema
        try:
            if sistema.tipo_chave == "por_pessoa":
                aberta = await renovacao.chave_pronta(
                    self.engine, self.cifra, sistema_id=sistema.id, pessoa_id=op.pessoa_id
                )
                return aberta.segredo, aberta, None
            async with self.engine.connect() as c:
                segredo = await chaves.abrir(c, self.cifra, sistema_id=sistema.id, aplicativo_id=op.aplicativo_id)
        except chaves.ChaveAusente:
            return None, None, "a pessoa ainda nao autorizou o sistema"
        except renovacao.AutorizacaoVencida:
            return None, None, "a autorizacao no sistema venceu"
        except renovacao.RenovacaoIndisponivel:
            return None, None, "nao deu para renovar a chave"
        except Exception:
            log.exception("chave_indisponivel")
            return None, None, "o cofre de chaves nao respondeu"
        return segredo, None, None

    async def _enviar(self, op: Operacao, ferramenta: FerramentaVitrine) -> tuple[Estado, dict[str, Any] | None, str]:
        sistema = ferramenta.sistema
        segredo, aberta, sem_chave = await self._chave(op, ferramenta)
        if sem_chave is not None:
            return Estado.NAO_SAIU, None, sem_chave
        async with self.engine.connect() as c:
            discover_json = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema.id})
            ).scalar_one_or_none()
            declarado = await repositorio.esquema_declarado(c, sistema.id, ferramenta.nome_original)

        async def obter() -> str | None:
            return segredo

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        try:
            resposta = await self.chamar(
                sistema.endereco,
                obter,
                sondagem,
                ferramenta.nome_original,
                op.argumentos,
                traceparent=None,
                prazo=self.prazo_envio,
                esquema_declarado=declarado,
            )
        except Exception:
            # Nao sabemos em que ponto parou: o pedido pode ter saido. Nunca reenvia.
            log.exception("envio_falhou")
            return Estado.NAO_CONFIRMADA, None, "falha inesperada durante o envio"
        return await self._classificar(op, ferramenta, resposta, aberta)

    async def _classificar(
        self, op: Operacao, ferramenta: FerramentaVitrine, resposta: cliente.Resposta, aberta: Any
    ) -> tuple[Estado, dict[str, Any] | None, str]:
        sistema = ferramenta.sistema
        if resposta.tipo == "resultado" and resposta.resultado is not None:
            bruto = resposta.resultado.model_dump(by_alias=True, exclude_none=True, mode="json")
            if bruto.get("resultType") == "input_required":
                return Estado.SISTEMA_PEDIU_DADOS, None, "o sistema pediu dados no meio da acao"
            resultado = medida.aplicar(bruto, ferramenta.campos_pessoais)
            resultado.pop("resultType", None)
            resultado.pop("_meta", None)  # o _meta do sistema nunca passa adiante
            desfecho = "o sistema respondeu com erro" if resultado.get("isError") else "o sistema respondeu"
            return Estado.ENVIADA, resultado, desfecho
        if resposta.tipo == "nao_enviado":
            return Estado.NAO_SAIU, None, "o pedido nao chegou ao sistema"
        if resposta.tipo == "recusado_http":
            if 401 in resposta.status_http and aberta is not None:
                await renovacao.marcar_vencida(
                    self.engine, sistema_id=sistema.id, pessoa_id=op.pessoa_id, cifrada=aberta.cifrada
                )
            codigos = ",".join(str(s) for s in resposta.status_http) or "?"
            return Estado.NAO_SAIU, None, f"o sistema recusou na porta (HTTP {codigos})"
        if resposta.tipo == "erro_rpc" and resposta.erro is not None and resposta.erro.code in _ANTES_DE_EXECUTAR:
            return Estado.NAO_SAIU, None, f"o sistema recusou o pedido (codigo {resposta.erro.code})"
        if resposta.tipo == "erro_rpc":
            codigo = resposta.erro.code if resposta.erro is not None else "?"
            return Estado.NAO_CONFIRMADA, None, f"o sistema devolveu erro depois de receber (codigo {codigo})"
        return Estado.NAO_CONFIRMADA, None, "a resposta do sistema nao chegou inteira"
