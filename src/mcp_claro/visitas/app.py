"""Processo pai da sala de visitas (desenho 3.8; ajustes D1 e A1).

``POST /interno/visitas/chamar``, ``/listar`` e ``/sondar``, com o contrato de ``ligacao/sala_contrato.py``, numa
porta com mTLS que so aceita o certificado de cliente do despachante (a partida e a porta ficam em ``__main__``).
Sem banco, sem cofre e sem extrato.

Cada chamada vai a um processo filho novo, iniciado por ``spawn`` (``visitas/filho.py``), que recebe so os dados
daquela chamada e morre no fim. Para a chamada nao esperar o filho carregar o cliente fixado, a sala deixa alguns
filhos ja iniciados e sem dado nenhum (``PRONTOS``), cada um para uma chamada so. O pai:

- le o pedido do despachante com teto (``teto_pedido``) e so o entrega, sem interpretar, a um filho que ja avisou
  que se isolou (``filho.PRONTO``): sob rajada, um filho recem-iniciado recebe a chave so depois do isolamento. Um
  filho que morre antes de avisar (ou antes de receber o pedido inteiro) e trocado uma vez por um novo: nada foi a
  ele;
- fala com cada filho por soquetes, nao por canos comuns: o ``/proc/<pid>/fd`` de um soquete nao abre, nem para o
  mesmo usuario (o de um cano abriria e deixaria ler o pedido);
- devolve o status e os bytes que o filho escreveu, sem ler nem conferir (o despachante confere o contrato);
- erro inesperado do filho (saiu sem resposta, resposta fora do formato, prazo da sala esgotado): 500, que o
  despachante le como duvida;
- espera uma vaga (``MAX_CHAMADAS``, de todos os terceiros juntos) no maximo ``PRAZO_VAGA``. Sem vaga, responde 200
  com o corpo de "nao enviado" da rota e a excecao ``sala_sem_vaga`` (``_SEM_VAGA``), e nada vai a um filho: um
  terceiro lento que ocupa as vagas nao deixa em duvida a chamada a outro, que nem saiu (contrato K7). Essa resposta,
  e so ela, leva o cabecalho ``sala_contrato.CABECALHO_DA_SALA``: o filho escreve so o status e o corpo, e nao
  consegue imita-la (o despachante nao conta a sala cheia contra o servidor nas declaracoes, critica A5);
- se o despachante desiste da conexao, o filho daquela chamada e morto na hora;
- cria todos os filhos numa thread so (``CRIADORA``), aberta em ``Sala.ligada()`` e fechada no fim: o
  ``PR_SET_PDEATHSIG`` do filho vale pela thread que o criou, nao pelo processo, e um filho criado numa thread do
  anyio morreria no meio da chamada quando o anyio aposenta a thread ociosa.

Logs so com a rota, o status HTTP, o desfecho do filho e a duracao.
"""

from __future__ import annotations

import collections
import contextlib
import multiprocessing
import os
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Final

import anyio
import anyio.abc
import anyio.to_thread
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.config.settings import Ambiente, Parametros
from mcp_claro.ligacao import sala_contrato
from mcp_claro.logs import logger
from mcp_claro.visitas import filho

ROTAS: Final = (sala_contrato.ROTA_CHAMAR, sala_contrato.ROTA_LISTAR, sala_contrato.ROTA_SONDAR)
PRONTOS: Final = 2  # filhos iniciados, sem dado nenhum, a espera de uma chamada cada
MAX_CHAMADAS: Final = 32  # chamadas ao mesmo tempo, cada uma no seu processo
PRAZO_VAGA: Final = 2.0  # segundos que uma chamada espera uma vaga; sem vaga, "nao enviado" (sala_sem_vaga)
TETO_CHAMADA: Final = 660.0  # segundos: o maior prazo do contrato (600) com folga; nenhum filho vive mais que isso
TETO_RESPOSTA: Final = 3 + sala_contrato.TETO_SALTO  # status e corpo que o filho escreve
TETO_ESPERA_FIM: Final = 5.0
CRIADORA: Final = "mcp-claro-visitas-criadora"  # nome da thread que cria os filhos
_PEDACO: Final = 1024 * 1024
_JSON: Final = "application/json"
_ERRO: Final = b'{"erro":"a sala de visitas nao atendeu"}'

# Pedido grande demais: o pai responde como o contrato responde a pedido fora dele (nada foi ao terceiro).
_FORA_DO_CONTRATO: Final = {
    sala_contrato.ROTA_CHAMAR: sala_contrato.RespostaChamar(
        tipo="nao_enviado", excecao=sala_contrato.PEDIDO_FORA_DO_CONTRATO
    ).para_json(),
    sala_contrato.ROTA_LISTAR: sala_contrato.RespostaListar(
        tipo="nao_enviado", excecao=sala_contrato.PEDIDO_FORA_DO_CONTRATO
    ).para_json(),
    sala_contrato.ROTA_SONDAR: sala_contrato.RespostaSondar(
        desfecho="inalcancavel", motivo=sala_contrato.MOTIVO_PEDIDO_FORA, excecao=sala_contrato.PEDIDO_FORA_DO_CONTRATO
    ).para_json(),
}

# Sala cheia (contrato K7): a resposta de "nao enviado" de cada rota, sem nada ir a um filho.
_SEM_VAGA: Final = {
    sala_contrato.ROTA_CHAMAR: sala_contrato.RespostaChamar(
        tipo="nao_enviado", excecao=sala_contrato.SALA_SEM_VAGA
    ).para_json(),
    sala_contrato.ROTA_LISTAR: sala_contrato.RespostaListar(
        tipo="nao_enviado", excecao=sala_contrato.SALA_SEM_VAGA
    ).para_json(),
    sala_contrato.ROTA_SONDAR: sala_contrato.RespostaSondar(
        desfecho="inalcancavel", motivo=sala_contrato.MOTIVO_SALA_SEM_VAGA, excecao=sala_contrato.SALA_SEM_VAGA
    ).para_json(),
}

Alvo = Callable[[Connection, Connection, str, bool, int], None]


def teto_pedido(p: Parametros) -> int:
    """Maior pedido que a sala aceita do despachante.

    Os argumentos vem do aplicativo (ate ``MAX_REQUEST_BYTES``) e o despachante os regrava em JSON, o que pode
    crescer (um ``1e15`` vira ``1000000000000000.0``); somam-se o ``server/discover`` e o esquema declarado.
    """
    return 5 * p.MAX_REQUEST_BYTES + 2 * sala_contrato.TETO_DISCOVER + 2 * 1024 * 1024


@dataclass
class _Filho:
    processo: BaseProcess
    pedidos: Connection  # o pai escreve; o filho le
    respostas: Connection  # o filho escreve; o pai le
    inicio: float = field(default_factory=time.monotonic)

    async def esperar_isolamento(self) -> bool:
        """Espera o aviso de que o filho se isolou (``filho.PRONTO``). False se ele morreu antes de avisar."""
        fd = self.respostas.fileno()
        os.set_blocking(fd, False)
        while True:
            try:
                aviso = os.read(fd, len(filho.PRONTO))
            except BlockingIOError:
                await anyio.wait_readable(fd)
                continue
            except ConnectionError:
                return False
            return aviso == filho.PRONTO

    async def entregar(self, rota: str, corpo: bytes) -> None:
        """Escreve a rota, uma quebra de linha e o corpo, e fecha: o filho le ate o fim do cano."""
        fd = self.pedidos.fileno()
        os.set_blocking(fd, False)
        vista = memoryview(rota.encode("ascii") + b"\n" + corpo)
        while vista:
            try:
                vista = vista[os.write(fd, vista) :]
            except BlockingIOError:
                await anyio.wait_writable(fd)
        self.pedidos.close()

    async def receber(self) -> bytes | None:
        """O que o filho escreveu, ate o fim do cano; None se passar de ``TETO_RESPOSTA``."""
        fd = self.respostas.fileno()
        os.set_blocking(fd, False)
        partes: list[bytes] = []
        lidos = 0
        while True:
            try:
                pedaco = os.read(fd, _PEDACO)
            except BlockingIOError:
                await anyio.wait_readable(fd)
                continue
            if not pedaco:
                return b"".join(partes)
            lidos += len(pedaco)
            if lidos > TETO_RESPOSTA:
                return None
            partes.append(pedaco)

    def encerrar(self) -> int | None:
        """Mata o filho se ainda vive, espera o fim e fecha os canos. Devolve o codigo de saida."""
        if self.processo.is_alive():
            self.processo.kill()
        self.processo.join(TETO_ESPERA_FIM)
        codigo = self.processo.exitcode
        for ponta in (self.pedidos, self.respostas):
            with contextlib.suppress(OSError):
                ponta.close()
        if codigo is not None:
            self.processo.close()
        return codigo


@dataclass(frozen=True, slots=True)
class Atendimento:
    status: int
    corpo: bytes
    desfecho: str  # filho, filho_nao_isolou, filho_sem_resposta, filho_resposta_invalida, prazo, sem_vaga
    marca: str | None = None  # so o pai: o valor de ``CABECALHO_DA_SALA`` (sala cheia)


class _NaoIsolou(Exception):
    """Nem o filho da chamada nem o que o trocou avisaram que se isolaram: o pedido nao foi a ninguem."""


def _status(mensagem: bytes) -> int | None:
    cabeca = mensagem[:3]
    if len(cabeca) != 3 or not cabeca.isdigit():
        return None
    status = int(cabeca)
    return status if 100 <= status <= 599 else None


class Sala:
    """Os filhos da sala: um processo novo por chamada, com alguns ja iniciados a espera."""

    def __init__(
        self,
        p: Parametros,
        *,
        alvo: Alvo = filho.principal,
        prontos: int = PRONTOS,
        max_chamadas: int = MAX_CHAMADAS,
        teto_chamada: float = TETO_CHAMADA,
        prazo_vaga: float = PRAZO_VAGA,
    ) -> None:
        # O ambiente que vale e o da sala, nunca um campo do pedido; enderecos locais so em desenvolvimento.
        self.ambiente = str(p.APP_ENV)
        self.liberar_locais = p.APP_ENV is Ambiente.DESENVOLVIMENTO
        self.teto_pedido = teto_pedido(p)
        self._contexto = multiprocessing.get_context("spawn")
        self._alvo = alvo
        self._quantos_prontos = prontos
        self._prontos: collections.deque[_Filho] = collections.deque()
        self._max_chamadas = max_chamadas
        self._vagas: anyio.Semaphore | None = None
        self._prazo_vaga = prazo_vaga
        self._teto_chamada = teto_chamada
        self._tarefas: anyio.abc.TaskGroup | None = None
        self._criadora: ThreadPoolExecutor | None = None
        self._repondo = False
        self._log = logger("mcp_claro.visitas")

    def _iniciar(self) -> _Filho:
        # Soquetes (duplex=True), cada um usado num sentido so: o /proc/<pid>/fd de um soquete nao se reabre.
        leitor_do_filho, pedidos = self._contexto.Pipe(duplex=True)
        respostas, escritor_do_filho = self._contexto.Pipe(duplex=True)
        processo = self._contexto.Process(
            target=self._alvo,
            args=(leitor_do_filho, escritor_do_filho, self.ambiente, self.liberar_locais, self.teto_pedido),
            name="mcp-claro-visitas-filho",
            daemon=True,
        )
        try:
            processo.start()
        finally:
            # As pontas do filho ficam so com ele: o fim do cano chega quando ele fecha ou morre.
            leitor_do_filho.close()
            escritor_do_filho.close()
        return _Filho(processo, pedidos, respostas)

    async def _novo(self, destino: list[_Filho] | collections.deque[_Filho]) -> None:
        """Inicia um filho na thread criadora e o poe em ``destino`` (quem o encerra no fim).

        So na criadora: ``PR_SET_PDEATHSIG`` mata o filho quando morre a thread que o criou. Uma thread do anyio
        so espera o fim do ``start`` (rapido: o ``spawn`` carrega o filho depois, sozinho). Sem janela de cancelamento
        entre o inicio e o ``destino``: nenhum filho fica sem dono.
        """
        criadora = self._criadora
        if criadora is None:
            raise RuntimeError("a sala de visitas so inicia filhos ligada (Sala.ligada)")
        with anyio.CancelScope(shield=True):
            destino.append(await anyio.to_thread.run_sync(criadora.submit(self._iniciar).result))

    async def _pegar(self, filhos: list[_Filho]) -> None:
        """Poe em ``filhos`` um filho pronto que ainda vive ou, sem nenhum, um novo."""
        while self._prontos:
            pronto = self._prontos.popleft()
            if pronto.processo.is_alive():
                filhos.append(pronto)
                return
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(pronto.encerrar)
        await self._novo(filhos)

    async def repor(self) -> None:
        """Completa os filhos prontos. Falha ao iniciar nao derruba a sala: a chamada inicia o seu."""
        if self._repondo:
            return
        self._repondo = True
        try:
            while len(self._prontos) < self._quantos_prontos:
                await self._novo(self._prontos)
        except Exception as erro:  # noqa: BLE001 - so o nome do erro vai ao log
            self._log.warning("filho_pronto_nao_iniciou", erro=type(erro).__name__)
        finally:
            self._repondo = False

    def pedir_reposicao(self) -> None:
        if self._tarefas is not None:
            self._tarefas.start_soon(self.repor)

    @asynccontextmanager
    async def ligada(self) -> AsyncIterator[None]:
        """A sala no ar: a thread criadora, as vagas e a reposicao dos prontos. No fim, mata os prontos e fecha a
        criadora (com ela, morre qualquer filho ja isolado que ainda viva, pelo ``PR_SET_PDEATHSIG``)."""
        self._vagas = anyio.Semaphore(self._max_chamadas)
        self._criadora = ThreadPoolExecutor(max_workers=1, thread_name_prefix=CRIADORA)
        try:
            async with anyio.create_task_group() as tarefas:
                self._tarefas = tarefas
                self.pedir_reposicao()
                try:
                    yield
                finally:
                    self._tarefas = None
                    tarefas.cancel_scope.cancel()
        finally:
            # Depois do grupo: a reposicao ja terminou e nao poe mais nenhum pronto.
            with anyio.CancelScope(shield=True):
                while self._prontos:
                    await anyio.to_thread.run_sync(self._prontos.popleft().encerrar)
                criadora, self._criadora = self._criadora, None
                if criadora is not None:
                    await anyio.to_thread.run_sync(criadora.shutdown)

    async def atender(self, rota: str, corpo: bytes) -> Atendimento:
        """Um filho por chamada. O corpo devolvido e o que o filho escreveu, sem leitura.

        A vaga e esperada no maximo ``prazo_vaga``. Sem vaga, 200 com o "nao enviado" da rota (``_SEM_VAGA``): nada
        foi a um filho nem ao terceiro, e o despachante le "nao enviado", e nao a duvida do prazo dele."""
        if self._vagas is None or self._criadora is None:
            raise RuntimeError("a sala de visitas so atende ligada (Sala.ligada)")
        # Cancelada a espera, o semaforo do anyio devolve a vaga que tivesse acabado de dar: nenhuma fica presa.
        with anyio.move_on_after(self._prazo_vaga) as espera:
            await self._vagas.acquire()
        if espera.cancelled_caught:
            return Atendimento(200, _SEM_VAGA[rota], "sem_vaga", marca=sala_contrato.SALA_SEM_VAGA)
        try:
            filhos: list[_Filho] = []
            try:
                await self._pegar(filhos)
                self.pedir_reposicao()
                with anyio.fail_after(self._teto_chamada):
                    mensagem = await self._entregar_e_receber(filhos, rota, corpo)
            except _NaoIsolou:
                return Atendimento(500, _ERRO, "filho_nao_isolou")
            except TimeoutError:
                return Atendimento(500, _ERRO, "prazo")
            except OSError:
                return Atendimento(500, _ERRO, "filho_sem_resposta")
            finally:
                with anyio.CancelScope(shield=True):
                    for usado in filhos:
                        await anyio.to_thread.run_sync(usado.encerrar)
        finally:
            self._vagas.release()
        if not mensagem:  # o filho saiu sem escrever (erro inesperado, ou morto)
            return Atendimento(500, _ERRO, "filho_sem_resposta" if mensagem is not None else "filho_resposta_invalida")
        status = _status(mensagem)
        if status is None:
            return Atendimento(500, _ERRO, "filho_resposta_invalida")
        return Atendimento(status, mensagem[3:], "filho")

    async def _entregar_e_receber(self, filhos: list[_Filho], rota: str, corpo: bytes) -> bytes | None:
        """Entrega o pedido so a um filho que avisou que se isolou e le a resposta dele.

        Se o filho morre antes de avisar, ou antes de receber o pedido inteiro, nada foi ao terceiro: ele e trocado
        uma vez por um novo (que entra em ``filhos``, para o pai encerrar no fim).
        """
        for tentativa in range(2):
            if tentativa:
                await self._novo(filhos)
            atual = filhos[-1]
            if not await atual.esperar_isolamento():
                continue
            try:
                await atual.entregar(rota, corpo)
            except BrokenPipeError, ConnectionResetError:
                continue
            return await atual.receber()
        raise _NaoIsolou


async def _ler_corpo(pedido: Request, teto: int) -> bytes | None:
    declarado = pedido.headers.get("content-length", "")
    if declarado.isdigit() and int(declarado) > teto:
        return None
    partes: list[bytes] = []
    lidos = 0
    async for pedaco in pedido.stream():
        lidos += len(pedaco)
        if lidos > teto:
            return None
        partes.append(pedaco)
    return b"".join(partes)


def criar_app_visitas(p: Parametros, *, sala: Sala | None = None) -> Starlette:
    """A aplicacao da sala. A partida (``__main__``) confere antes ``visitas.partida.conferir_sala``."""
    sala = sala or Sala(p)
    log = logger("mcp_claro.visitas")

    async def saude(_: Request) -> JSONResponse:
        return JSONResponse({"papel": "visitas", "versao": __version__, "vivo": True})

    async def visitar(pedido: Request) -> Response:
        rota = pedido.url.path
        inicio = time.monotonic()
        corpo = await _ler_corpo(pedido, sala.teto_pedido)
        if corpo is None:
            log.info("visita", rota=rota.rsplit("/", 1)[-1], status=200, desfecho="pedido_grande_demais")
            return Response(_FORA_DO_CONTRATO[rota], status_code=200, media_type=_JSON)
        atendimento: Atendimento | None = None
        async with anyio.create_task_group() as tarefas:

            async def vigiar_desistencia() -> None:
                # O despachante desistiu (prazo dele): o filho desta chamada morre na hora.
                while (await pedido.receive())["type"] != "http.disconnect":
                    continue
                tarefas.cancel_scope.cancel()

            tarefas.start_soon(vigiar_desistencia)
            atendimento = await sala.atender(rota, corpo)
            tarefas.cancel_scope.cancel()
        duracao_ms = round((time.monotonic() - inicio) * 1000)
        if atendimento is None:
            log.info("visita", rota=rota.rsplit("/", 1)[-1], desfecho="despachante_desistiu", duracao_ms=duracao_ms)
            return Response(_ERRO, status_code=500, media_type=_JSON)
        log.info(
            "visita",
            rota=rota.rsplit("/", 1)[-1],
            status=atendimento.status,
            desfecho=atendimento.desfecho,
            duracao_ms=duracao_ms,
        )
        # O cabecalho e do pai, nunca do filho (que so escreve status e corpo).
        cabecalhos = {sala_contrato.CABECALHO_DA_SALA: atendimento.marca} if atendimento.marca else None
        return Response(atendimento.corpo, status_code=atendimento.status, media_type=_JSON, headers=cabecalhos)

    @asynccontextmanager
    async def ciclo(_: Starlette) -> AsyncIterator[None]:
        # landlock=0: o nucleo nao tem Landlock e cada filho fica so com PR_SET_DUMPABLE (modelo de ameacas).
        log.info(
            "partida", papel="visitas", versao=__version__, ambiente=sala.ambiente, landlock=filho.versao_landlock()
        )
        async with sala.ligada():
            yield
        log.info("parada", papel="visitas")

    rotas: list[Any] = [Route("/saude", saude), *(Route(r, visitar, methods=["POST"]) for r in ROTAS)]
    app = Starlette(routes=rotas, lifespan=ciclo)
    app.state.parametros = p
    app.state.sala = sala
    return app
