"""Cifra por origem: a chave Transit de cada sistema depende de quem e o sistema (desenho 3.4; critica B9).

- Sistema proprio: a chave dos sistemas proprios (``VAULT_TRANSIT_KEY``), como nas Fases 1 e 2.
- Servidor de terceiros (toda origem que nao e ``proprio``, o lado que aperta): a chave so dos terceiros
  (``VAULT_TRANSIT_KEY_THIRD_PARTY``). Girar, perder ou ter a chave de terceiros recusada nao toca nos proprios.

A escolha sai do contexto do Transit, que sempre comeca por ``mcp-claro|{sistema_id}|`` (``chaves.contexto``,
``autorizacao.contexto_segredo_cliente`` e ``autorizacao.contexto_pendente``): quem chama ``chaves.guardar`` e
``chaves.abrir`` nao muda nada. A origem de cada sistema e lida uma vez no banco e guardada em memoria: ela nunca
muda (guarda da migracao 0006, D8). Contexto sem sistema, ou de sistema que nao existe, e recusado: nada e cifrado
nem decifrado com a chave errada.

Quem chama a cifra quase sempre segura uma conexao do pool do despachante (a leitura, a renovacao com trava, a porta
de saida). Por isso a busca da origem nunca pede conexao a esse pool: na partida, ``carregar`` le a origem de todos
os sistemas de uma vez; um sistema novo depois disso e lido por um engine sem pool (``engine_sem_pool``: a mesma
conexao, aberta e fechada a cada busca), e uma busca so por sistema, mesmo com muitas leituras ao mesmo tempo.

``conferir_chave_de_terceiros`` roda na partida do despachante: a chave de terceiros existe, o despachante a usa, e
ela e ``derived`` (cifrar sem contexto e recusado). Se nao, a partida recusa (como ``scripts/iniciar-cofre.sh``
confere a outra chave).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from mcp_claro.cofre import ErroCofre, PedidoRecusado
from mcp_claro.config.settings import ConfiguracaoInvalida
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.logs import logger

log = logger("mcp_claro.despachante.cifra_origem")

PREFIXO_CONTEXTO: Final = b"mcp-claro|"
TEXTO_DA_CONFERENCIA: Final = b"mcp-claro: conferencia da chave de terceiros na partida"
CONTEXTO_DA_CONFERENCIA: Final = b"mcp-claro|partida|conferencia-da-chave-de-terceiros"
"""Contexto da conferencia da partida: nao comeca por um sistema, nunca serve para abrir uma chave guardada."""

OrigemDe = Callable[[uuid.UUID], Awaitable[str | None]]
TodasAsOrigens = Callable[[], Awaitable[dict[uuid.UUID, str]]]


class ContextoSemSistema(ValueError):
    """O contexto do Transit nao diz de que sistema e a chave, ou o sistema nao existe: nada e cifrado."""


def sistema_do_contexto(contexto: bytes) -> uuid.UUID:
    """O ``sistema_id`` de um contexto ``mcp-claro|{sistema_id}|...``. Levanta ``ContextoSemSistema``."""
    if not contexto.startswith(PREFIXO_CONTEXTO):
        raise ContextoSemSistema("contexto do Transit sem o prefixo do MCP-CLARO")
    pedaco = contexto[len(PREFIXO_CONTEXTO) :].split(b"|", 1)[0]
    try:
        return uuid.UUID(pedaco.decode("ascii"))
    except UnicodeDecodeError, ValueError:
        raise ContextoSemSistema("contexto do Transit sem o sistema") from None


def engine_sem_pool(engine: AsyncEngine) -> AsyncEngine:
    """Um engine sem pool, com a mesma conexao de ``engine`` (usuario, senha, TLS, papel e nome da aplicacao): cada
    uso abre e fecha a sua conexao, sem nunca esperar por uma conexao do pool de ``engine``.

    Usa o criador de conexoes do pool de ``engine`` (o SQLAlchemy o guarda em ``Pool._creator``), para nao copiar
    parametros de conexao. O dialeto do engine novo prepara cada conexao como o de ``engine``."""
    criador = engine.sync_engine.pool._creator
    return create_async_engine(engine.url, poolclass=NullPool, creator=criador)


def origem_no_banco(engine: AsyncEngine) -> OrigemDe:
    """Le ``sistemas.origem`` de um sistema numa conexao propria, curta (a do chamador pode estar no meio de uma
    transacao). ``engine`` deve ser um engine so para isso (``engine_sem_pool``), nunca o do despachante."""

    async def origem_de(sistema_id: uuid.UUID) -> str | None:
        async with engine.connect() as c:
            valor = (
                await c.execute(text("SELECT origem FROM sistemas WHERE id = :s"), {"s": sistema_id})
            ).scalar_one_or_none()
        return None if valor is None else str(valor)

    return origem_de


def todas_no_banco(engine: AsyncEngine) -> TodasAsOrigens:
    """Le a origem de todos os sistemas de uma vez (na partida)."""

    async def todas() -> dict[uuid.UUID, str]:
        async with engine.connect() as c:
            linhas = (await c.execute(text("SELECT id, origem FROM sistemas"))).all()
        return {uuid.UUID(str(linha.id)): str(linha.origem) for linha in linhas}

    return todas


class CifraPorOrigem:
    """``Cifra`` que escolhe a chave Transit pela origem do sistema do contexto."""

    def __init__(
        self,
        *,
        propria: Cifra,
        terceiros: Cifra,
        origem_de: OrigemDe,
        todas: TodasAsOrigens | None = None,
        fechar: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._propria = propria
        self._terceiros = terceiros
        self._origem_de = origem_de
        self._todas = todas
        self._fechar = fechar
        self._origens: dict[uuid.UUID, str] = {}
        self._buscas: dict[uuid.UUID, asyncio.Future[str | None]] = {}

    @classmethod
    def do_banco(cls, engine: AsyncEngine, *, propria: Cifra, terceiros: Cifra) -> CifraPorOrigem:
        """Le as origens pelo banco de ``engine``, mas por um engine sem pool (``engine_sem_pool``): a busca nunca
        disputa o pool de quem chama a cifra."""
        origens = engine_sem_pool(engine)
        return cls(
            propria=propria,
            terceiros=terceiros,
            origem_de=origem_no_banco(origens),
            todas=todas_no_banco(origens),
            fechar=origens.dispose,
        )

    async def carregar(self) -> int:
        """Na partida: a origem de todos os sistemas que ja existem, numa leitura so. Devolve quantos."""
        if self._todas is None:
            return 0
        lidas = await self._todas()
        self._origens.update(lidas)
        return len(lidas)

    async def fechar(self) -> None:
        if self._fechar is not None:
            await self._fechar()

    async def _buscar(self, sistema_id: uuid.UUID) -> str | None:
        lida = await self._origem_de(sistema_id)
        if lida is not None:
            self._origens[sistema_id] = lida
        return lida

    def _busca_terminou(self, sistema_id: uuid.UUID, busca: asyncio.Future[str | None]) -> None:
        if self._buscas.get(sistema_id) is busca:
            del self._buscas[sistema_id]
        if not busca.cancelled():
            busca.exception()  # quem esperava pode ter desistido: o erro nao fica sem dono

    async def origem(self, sistema_id: uuid.UUID) -> str:
        """Origem do sistema, lida uma vez (uma busca so por sistema, para todos os que pedem ao mesmo tempo).
        Sistema que nao existe levanta ``ContextoSemSistema`` (sem guardar)."""
        guardada = self._origens.get(sistema_id)
        if guardada is not None:
            return guardada
        busca = self._buscas.get(sistema_id)
        if busca is None:
            busca = asyncio.ensure_future(self._buscar(sistema_id))
            self._buscas[sistema_id] = busca
            busca.add_done_callback(lambda feita: self._busca_terminou(sistema_id, feita))
        # shield: quem desiste (prazo, cancelamento) nao cancela a busca dos outros.
        lida = await asyncio.shield(busca)
        if lida is None:
            raise ContextoSemSistema("sistema desconhecido para a cifra")
        return lida

    async def cifra_para(self, contexto: bytes) -> Cifra:
        origem = await self.origem(sistema_do_contexto(contexto))
        return self._propria if origem == "proprio" else self._terceiros

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        return await (await self.cifra_para(contexto)).cifrar(texto, contexto)

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        return await (await self.cifra_para(contexto)).decifrar(cifrado, contexto)


class CofreQueCifra(Protocol):
    async def cifrar(self, chave: str, texto: bytes, *, contexto: bytes | None = None) -> str: ...


async def conferir_chave_de_terceiros(cofre: CofreQueCifra, chave: str) -> None:
    """Na partida: a chave de terceiros existe e o despachante cifra com ela (com contexto), e ela e ``derived``
    (sem contexto, o cofre recusa com 400). Senao, ``ConfiguracaoInvalida``: a partida do despachante recusa.

    O papel do despachante nao le ``transit/keys/*`` (politica do cofre); por isso a prova e cifrar. Sem a chave,
    o cofre nega (o papel nao pode criar chave); chave sem ``derived`` cifra sem contexto e e recusada aqui.
    """
    try:
        await cofre.cifrar(chave, TEXTO_DA_CONFERENCIA, contexto=CONTEXTO_DA_CONFERENCIA)
    except ErroCofre as erro:
        raise ConfiguracaoInvalida(
            [
                f"A chave Transit dos servidores de terceiros ({chave}) nao existe no cofre ou o despachante nao a"
                f" usa ({type(erro).__name__}). Rode scripts/iniciar-cofre.sh e confira a politica do despachante."
            ]
        ) from erro
    try:
        await cofre.cifrar(chave, TEXTO_DA_CONFERENCIA)
    except ErroCofre as erro:
        if isinstance(erro, PedidoRecusado) and erro.status == 400:
            log.info("chave_de_terceiros_conferida", chave=chave)
            return
        raise ConfiguracaoInvalida(
            [
                f"A conferencia da chave Transit dos servidores de terceiros ({chave}) falhou"
                f" ({type(erro).__name__}); ela precisa ser derived."
            ]
        ) from erro
    raise ConfiguracaoInvalida(
        [
            f"A chave Transit dos servidores de terceiros ({chave}) nao e derived: cifra sem contexto. Ela precisa"
            " amarrar cada chave guardada ao sistema e ao dono (crie a chave com derived=true)."
        ]
    )
