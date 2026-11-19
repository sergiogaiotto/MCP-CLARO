"""Despachante unico da porta de saida.

Uma copia do papel por vez segura a trava de sessao ``DESPACHANTE_UNICO`` numa conexao propria, fora do
pool (a conexao e descartada no fim, e a trava vai junto). Ao assumir, toda acao que ficou "enviando"
vira "nao confirmada": pode ter chegado ao sistema, e nada e reenviado.

Para cada acao liberada e na hora:

1. travas consultivas compartilhadas do alcance (tudo, sistema, aplicativo, pessoa) e a linha da fila;
2. reconferencia: saida aberta, pessoa e aplicativo ativos, sem bloqueio, cadastro do programa valendo
   (quando o aplicativo e um programa), ordem e ferramenta vigentes, ficha confirmada para esta operacao
   (a de risco alto, com passkey ou login refeito) ou limite gasto e nao encerrado, e codigo de conferencia
   igual ao que vai seguir;
3. marca "enviando" e grava, antes de qualquer byte sair (o que o envio le do banco e lido antes);
4. abre a chave (renovada antes, se for por pessoa) e faz exatamente uma chamada ao sistema;
5. grava o fim: enviada, nao saiu, sistema pediu dados ou nao confirmada. Nunca reenvia. O caractere nulo
   (U+0000), que o banco nao guarda, vira U+FFFD no resultado. Se o banco falhar ao gravar o fim, tenta de
   novo algumas vezes; se nao der, encerra o turno: a conexao da trava cai, quem assumir a porta (esta mesma,
   na volta seguinte, ou outra copia) da a linha "enviando" como nao confirmada, e nada e reenviado.

O despachante acorda pelo ``LISTEN mcp_claro_saida`` e, de reserva, por varredura. Com a porta de saida
fechada, so olha de novo a cada varredura.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Final

import mcp_types as types
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
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
from mcp_claro.saida.protocolo import FUSO, codigo_conferencia
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

TENTATIVAS_DO_FIM = 4
"""Vezes que o despachante tenta gravar o fim de um envio antes de deixar a retomada decidir."""
ESPERA_DO_FIM = 0.25
"""Espera entre as tentativas (segundos, crescendo a cada uma)."""
APLICATIVO_PARADO: Final[dict[str, str]] = {
    "pausado": "o aplicativo esta pausado",
    "bloqueado": "o aplicativo esta bloqueado",
}
"""Desfecho da acao parada porque o aplicativo nao esta ativo (``aplicativos.situacao``)."""
NULO = "\x00"
TROCA_DO_NULO = "\ufffd"
"""O PostgreSQL nao guarda U+0000 em texto nem em jsonb: no resultado, ele vira U+FFFD (caractere de troca)."""


class FimNaoGravado(RuntimeError):
    """O fim do envio nao gravou depois de todas as tentativas. Encerra o turno da porta para a retomada decidir.

    A mensagem so diz a linha e o estado: o erro do banco pode trazer os valores do comando (o resultado)."""


def sem_nulo(valor: Any) -> tuple[Any, int]:
    """Troca U+0000 por U+FFFD em todo texto (chaves e valores, em qualquer profundidade). Devolve (valor, trocas)."""
    if isinstance(valor, str):
        trocas = valor.count(NULO)
        return (valor.replace(NULO, TROCA_DO_NULO), trocas) if trocas else (valor, 0)
    if isinstance(valor, dict):
        limpo: dict[Any, Any] = {}
        total = 0
        for chave, item in valor.items():
            nova_chave, n_chave = sem_nulo(chave)
            novo_item, n_item = sem_nulo(item)
            limpo[nova_chave] = novo_item
            total += n_chave + n_item
        return limpo, total
    if isinstance(valor, list):
        itens = [sem_nulo(item) for item in valor]
        return [item for item, _ in itens], sum(n for _, n in itens)
    return valor, 0


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
        # A porta de saida estava fechada na ultima volta: a espera e a varredura inteira.
        self._saida_fechada = False
        # Linhas puladas nesta volta porque outra transacao as segura (cancelar, confirmar, bloquear).
        self._puladas: set[int] = set()

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
        if self._saida_fechada:
            # Nada sai com a porta fechada: as liberadas na hora esperam a reabertura, sem consultar o banco
            # a cada instante.
            espera = self.varredura
        else:
            async with self.engine.connect() as c:
                falta = (
                    await c.execute(
                        text(
                            "SELECT extract(epoch FROM min(hora_saida) - now()) FROM fila_saida"
                            " WHERE estado = 'liberada'"
                        )
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
            retomadas = await fila.retomar_enviando(c, quem=fila.QUEM_RETOMA)
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
            self._saida_fechada = not (await estado.ler_saida(c)).aberta
            if self._saida_fechada:
                return False
            candidata = (
                await c.execute(
                    text(
                        "SELECT f.id, o.pessoa_id, o.aplicativo_id, o.sistema_id FROM fila_saida f"
                        " JOIN operacoes o ON o.id = f.operacao_id"
                        " WHERE f.estado = 'liberada' AND f.hora_saida <= now() AND NOT (f.id = ANY(:puladas))"
                        " ORDER BY f.hora_saida, f.id LIMIT 1"
                    ),
                    {"puladas": sorted(self._puladas)},
                )
            ).first()
        if candidata is None:
            # Volta completa: as puladas entram de novo na proxima, depois da espera.
            self._puladas.clear()
            return False
        async with self._transacao(dono) as c:
            await travas.compartilhar_envio(c, candidata.pessoa_id, candidata.aplicativo_id, candidata.sistema_id)
            if not (await estado.ler_saida(c)).aberta:
                self._saida_fechada = True
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
                self._puladas.add(candidata.id)
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
            # O que o envio precisa do banco e lido aqui, antes de "enviando": uma falha nesta leitura deixa a
            # acao liberada, e nao em duvida.
            sondagem = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": ferramenta.sistema.id})
            ).scalar_one_or_none()
            declarado = await repositorio.esquema_declarado(c, ferramenta.sistema.id, ferramenta.nome_original)
            await fila.marcar_enviando(c, op)
        final, resultado, desfecho = await self._enviar(op, ferramenta, sondagem, declarado)
        await self._gravar_fim(op, final, resultado, desfecho)
        CONTADORES.somar("acoes", estado=str(final), sistema=ferramenta.sistema.nome)
        return True

    async def _gravar_fim(self, op: Operacao, final: Estado, resultado: dict[str, Any] | None, desfecho: str) -> None:
        """Grava o fim do envio, tentando de novo algumas vezes se o banco falhar.

        Antes, troca U+0000 por U+FFFD no resultado (o banco recusaria a gravacao todas as vezes). Sem conseguir
        gravar, levanta ``FimNaoGravado``: o turno da porta acaba, a conexao da trava cai, e quem assumir a porta
        da a linha "enviando" como nao confirmada (``retomar_enviando``). Nada e reenviado. Uma tentativa nova
        depois de um COMMIT que deu certo (e cuja confirmacao se perdeu) reconhece o proprio fim
        (``fila.finalizar``). O log nao leva o resultado nem o erro do banco (que pode trazer os valores do
        comando), so o tipo do erro."""
        if resultado is not None:
            resultado, trocas = sem_nulo(resultado)
            if trocas:
                log.warning("resultado_com_caractere_nulo", fila_id=op.fila_id, trocas=trocas)
        for tentativa in range(1, TENTATIVAS_DO_FIM + 1):
            try:
                async with self.engine.begin() as c:
                    await fila.finalizar(c, op, final, resultado, desfecho)
            except (SQLAlchemyError, OSError, TimeoutError) as erro:
                log.warning("fim_do_envio_falhou", fila_id=op.fila_id, tentativa=tentativa, erro=type(erro).__name__)
                if tentativa < TENTATIVAS_DO_FIM:
                    await asyncio.sleep(ESPERA_DO_FIM * tentativa)
            else:
                return
        log.error("fim_do_envio_nao_gravado", fila_id=op.fila_id, estado=str(final))
        raise FimNaoGravado(f"O fim do envio da linha {op.fila_id} ({final}) nao gravou; a retomada decide.") from None

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
        if situacoes.pessoa != "ativa":
            return Estado.PARADA_POR_BLOQUEIO, "a pessoa nao esta ativa"
        if situacoes.app != "ativo":
            # Hoje so a conferencia do banco na partida (``continuidade``) pausa um aplicativo. O desfecho aparece
            # na tela da acao como detalhe.
            return Estado.PARADA_POR_BLOQUEIO, APLICATIVO_PARADO.get(situacoes.app, "o aplicativo nao esta ativo")
        vigentes = await bloqueios.ativos(c, pessoa_id=op.pessoa_id, aplicativo_id=op.aplicativo_id)
        bloqueio = vigentes.para(op.sistema_id)
        if bloqueio is not None:
            return Estado.PARADA_POR_BLOQUEIO, bloqueio.motivo_recusa()
        if situacoes.tipo_app == "programa":
            if not self.acoes_de_programas:
                return Estado.CANCELADA, "programas so leem nesta instalacao"
            cadastro = (
                await c.execute(
                    text(
                        "SELECT situacao, fim >= (now() AT TIME ZONE :fuso)::date AS no_prazo FROM programas"
                        " WHERE aplicativo_id = :a"
                    ),
                    {"a": op.aplicativo_id, "fuso": FUSO},
                )
            ).first()
            if cadastro is None or cadastro.situacao != "ativo" or not cadastro.no_prazo:
                return Estado.CANCELADA, "o cadastro do programa nao esta valendo (pausado, encerrado ou vencido)"
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
                    text(
                        "SELECT operacao_id, decisao, codigo_conferencia, exige_passkey, risco, confirmada_com"
                        " FROM fichas WHERE id = :f"
                    ),
                    {"f": op.ficha_id},
                )
            ).one()
            if (
                str(ficha.operacao_id) != str(op.id)
                or ficha.decisao != "confirmada"
                or ficha.codigo_conferencia != codigo
            ):
                return Estado.CANCELADA, "a ficha nao foi confirmada para este pedido"
            if (ficha.exige_passkey or ficha.risco == "alto") and ficha.confirmada_com not in (
                "passkey",
                "login_refeito",
            ):
                return Estado.CANCELADA, "a ficha de risco alto nao foi confirmada com passkey nem com login refeito"
        elif op.limite_id is not None:
            limite = (
                await c.execute(
                    text(
                        "SELECT pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, com_passkey,"
                        " situacao FROM limites WHERE id = :l"
                    ),
                    {"l": op.limite_id},
                )
            ).first()
            if limite is not None and limite.situacao in ("encerrado", "suspenso"):
                # A pessoa encerrou (ou alguem suspendeu) o limite: o que ele liberou e ainda nao saiu nao sai.
                # Vencido pelo prazo depois de liberar ainda sai: o limite valia quando foi gasto.
                return Estado.CANCELADA, "o limite foi encerrado ou suspenso antes de a acao sair"
            if (
                limite is None
                or (limite.pessoa_id, limite.aplicativo_id, limite.sistema_id)
                != (op.pessoa_id, op.aplicativo_id, op.sistema_id)
                or limite.nome_original != ferramenta.nome_original
                or (self.limite_exige_passkey and not limite.com_passkey)
            ):
                return Estado.CANCELADA, "o limite nao cobre este pedido"
            # A mesma conferencia do gasto: "so estes destinatarios" com as chaves do esquema da ferramenta
            # revisada do limite, lidas nesta transacao (argumento fora do esquema nao sai pelo limite).
            propriedades = await fila.propriedades_da_ferramenta(c, limite.sistema_id, limite.nome_original)
            if not fila.cobre(limite.destinatarios, op.argumentos, propriedades=propriedades):
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

    async def _enviar(
        self, op: Operacao, ferramenta: FerramentaVitrine, discover_json: Any, declarado: Any
    ) -> tuple[Estado, dict[str, Any] | None, str]:
        """Uma chamada so ao sistema. ``discover_json`` e ``declarado`` vem lidos antes de marcar "enviando"."""
        sistema = ferramenta.sistema
        segredo, aberta, sem_chave = await self._chave(op, ferramenta)
        if sem_chave is not None:
            return Estado.NAO_SAIU, None, sem_chave

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
                try:
                    await renovacao.marcar_vencida(
                        self.engine, sistema_id=sistema.id, pessoa_id=op.pessoa_id, cifrada=aberta.cifrada
                    )
                except (SQLAlchemyError, OSError, TimeoutError) as erro:
                    # A acao nao saiu de qualquer jeito; so a proxima renovacao fica para depois.
                    log.warning("chave_vencida_nao_marcada", erro=type(erro).__name__)
            codigos = ",".join(str(s) for s in resposta.status_http) or "?"
            return Estado.NAO_SAIU, None, f"o sistema recusou na porta (HTTP {codigos})"
        if resposta.tipo == "erro_rpc" and resposta.erro is not None and resposta.erro.code in _ANTES_DE_EXECUTAR:
            return Estado.NAO_SAIU, None, f"o sistema recusou o pedido (codigo {resposta.erro.code})"
        if resposta.tipo == "erro_rpc":
            codigo = resposta.erro.code if resposta.erro is not None else "?"
            return Estado.NAO_CONFIRMADA, None, f"o sistema devolveu erro depois de receber (codigo {codigo})"
        return Estado.NAO_CONFIRMADA, None, "a resposta do sistema nao chegou inteira"
