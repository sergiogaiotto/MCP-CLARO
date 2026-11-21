"""Cifra por origem (G3a, U7; critica B9): a chave Transit sai da origem do sistema do contexto, lida uma vez; a
partida confere que a chave de terceiros existe e e ``derived``."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from mcp_claro.cofre import AcessoNegado, CofreIndisponivel, PedidoRecusado
from mcp_claro.config.settings import ConfiguracaoInvalida
from mcp_claro.despachante import autorizacao, chaves
from mcp_claro.despachante.cifra_origem import (
    CifraPorOrigem,
    ContextoSemSistema,
    conferir_chave_de_terceiros,
    sistema_do_contexto,
)

pytestmark = pytest.mark.anyio


class Anotadora:
    """Cifra falsa que anota quem cifrou e decifrou."""

    def __init__(self, nome: str) -> None:
        self.nome = nome
        self.usos: list[tuple[str, bytes]] = []

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        self.usos.append(("cifrar", contexto))
        return f"{self.nome}:{texto.decode()}"

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        self.usos.append(("decifrar", contexto))
        return cifrado.removeprefix(f"{self.nome}:").encode()


class Banco:
    def __init__(self, origens: dict[uuid.UUID, str]) -> None:
        self.origens = origens
        self.leituras: list[uuid.UUID] = []

    async def __call__(self, sistema_id: uuid.UUID) -> str | None:
        self.leituras.append(sistema_id)
        return self.origens.get(sistema_id)


def _cifra(origens: dict[uuid.UUID, str]) -> tuple[CifraPorOrigem, Anotadora, Anotadora, Banco]:
    propria, terceiros, banco = Anotadora("propria"), Anotadora("terceiros"), Banco(origens)
    return CifraPorOrigem(propria=propria, terceiros=terceiros, origem_de=banco), propria, terceiros, banco


def test_todo_contexto_do_despachante_diz_o_sistema() -> None:
    s, p = uuid.uuid4(), uuid.uuid4()
    for contexto in (
        chaves.contexto(s, p, None),
        chaves.contexto(s, None, uuid.uuid4(), "declaracoes"),
        chaves.contexto(s, None, None),
        autorizacao.contexto_segredo_cliente(s),
        autorizacao.contexto_pendente("estado", p, s),
    ):
        assert sistema_do_contexto(contexto) == s


@pytest.mark.parametrize(
    "contexto", [b"", b"outro|" + str(uuid.uuid4()).encode(), b"mcp-claro|nao-e-uuid|x", b"mcp-claro|\xff|x"]
)
def test_contexto_sem_sistema_e_recusado(contexto: bytes) -> None:
    with pytest.raises(ContextoSemSistema):
        sistema_do_contexto(contexto)


async def test_terceiro_usa_a_chave_de_terceiros_e_proprio_a_dos_proprios() -> None:
    proprio, terceiro, outra = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cifra, propria, de_terceiros, _ = _cifra({proprio: "proprio", terceiro: "terceiro", outra: "outra_origem"})
    assert await cifra.cifrar(b"a", chaves.contexto(proprio, None, None)) == "propria:a"
    assert await cifra.cifrar(b"b", chaves.contexto(terceiro, None, None)) == "terceiros:b"
    # Origem que nao e "proprio" conta como terceiro (o lado que aperta).
    assert await cifra.cifrar(b"c", chaves.contexto(outra, None, None)) == "terceiros:c"
    assert await cifra.decifrar("terceiros:b", chaves.contexto(terceiro, None, None)) == b"b"
    assert [u for u, _ in propria.usos] == ["cifrar"]
    assert [u for u, _ in de_terceiros.usos] == ["cifrar", "cifrar", "decifrar"]


async def test_origem_lida_uma_vez_e_sistema_desconhecido_nao_fica_guardado() -> None:
    conhecido, desconhecido = uuid.uuid4(), uuid.uuid4()
    cifra, propria, terceiros, banco = _cifra({conhecido: "proprio"})
    for _ in range(3):
        await cifra.cifrar(b"x", chaves.contexto(conhecido, None, None))
    assert banco.leituras == [conhecido]
    for _ in range(2):
        with pytest.raises(ContextoSemSistema):
            await cifra.cifrar(b"x", chaves.contexto(desconhecido, None, None))
    assert banco.leituras == [conhecido, desconhecido, desconhecido]
    assert len(propria.usos) == 3
    assert terceiros.usos == []


class BancoDevagar:
    """Busca de origem que so responde quando liberada (para pedidos ao mesmo tempo)."""

    def __init__(self, origem: str | None = "terceiro", erro: Exception | None = None) -> None:
        self.origem, self.erro = origem, erro
        self.leituras: list[uuid.UUID] = []
        self.liberar = asyncio.Event()

    async def __call__(self, sistema_id: uuid.UUID) -> str | None:
        self.leituras.append(sistema_id)
        await self.liberar.wait()
        if self.erro is not None:
            raise self.erro
        return self.origem


async def _esperando(banco: BancoDevagar, quantos: int) -> None:
    for _ in range(100):
        if len(banco.leituras) >= quantos:
            return
        await asyncio.sleep(0)
    raise AssertionError("a busca nao comecou")


async def test_muitos_pedidos_ao_mesmo_tempo_fazem_uma_busca_so() -> None:
    sistema = uuid.uuid4()
    banco = BancoDevagar()
    cifra = CifraPorOrigem(propria=Anotadora("propria"), terceiros=Anotadora("terceiros"), origem_de=banco)
    pedidos = [asyncio.ensure_future(cifra.cifrar(b"x", chaves.contexto(sistema, None, None))) for _ in range(10)]
    await _esperando(banco, 1)
    for _ in range(5):
        await asyncio.sleep(0)
    banco.liberar.set()
    assert await asyncio.gather(*pedidos) == ["terceiros:x"] * 10
    assert banco.leituras == [sistema]
    await cifra.cifrar(b"y", chaves.contexto(sistema, None, None))
    assert banco.leituras == [sistema]  # guardada


async def test_quem_desiste_nao_cancela_a_busca_dos_outros() -> None:
    sistema = uuid.uuid4()
    banco = BancoDevagar(origem="proprio")
    cifra = CifraPorOrigem(propria=Anotadora("propria"), terceiros=Anotadora("terceiros"), origem_de=banco)
    primeiro = asyncio.ensure_future(cifra.cifrar(b"a", chaves.contexto(sistema, None, None)))
    segundo = asyncio.ensure_future(cifra.cifrar(b"b", chaves.contexto(sistema, None, None)))
    await _esperando(banco, 1)
    primeiro.cancel()
    banco.liberar.set()
    assert await segundo == "propria:b"
    with pytest.raises(asyncio.CancelledError):
        await primeiro
    assert banco.leituras == [sistema]


async def test_busca_que_falha_chega_a_todos_e_a_proxima_tenta_de_novo() -> None:
    sistema = uuid.uuid4()
    banco = BancoDevagar(erro=OSError("banco fora"))
    cifra = CifraPorOrigem(propria=Anotadora("propria"), terceiros=Anotadora("terceiros"), origem_de=banco)
    pedidos = [asyncio.ensure_future(cifra.cifrar(b"x", chaves.contexto(sistema, None, None))) for _ in range(3)]
    await _esperando(banco, 1)
    banco.liberar.set()
    resultados = await asyncio.gather(*pedidos, return_exceptions=True)
    assert all(isinstance(r, OSError) for r in resultados)
    banco.erro = None
    assert await cifra.cifrar(b"x", chaves.contexto(sistema, None, None)) == "terceiros:x"
    assert banco.leituras == [sistema, sistema]


async def test_carregar_le_todas_as_origens_de_uma_vez_e_depois_nao_busca() -> None:
    proprio, terceiro, novo = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    banco = Banco({novo: "terceiro"})
    vezes: list[int] = []

    async def todas() -> dict[uuid.UUID, str]:
        vezes.append(1)
        return {proprio: "proprio", terceiro: "terceiro"}

    propria, de_terceiros = Anotadora("propria"), Anotadora("terceiros")
    cifra = CifraPorOrigem(propria=propria, terceiros=de_terceiros, origem_de=banco, todas=todas)
    assert await cifra.carregar() == 2
    assert await cifra.cifrar(b"p", chaves.contexto(proprio, None, None)) == "propria:p"
    assert await cifra.cifrar(b"t", chaves.contexto(terceiro, None, None)) == "terceiros:t"
    assert banco.leituras == []
    # Um sistema criado depois da partida: buscado (uma vez).
    assert await cifra.cifrar(b"n", chaves.contexto(novo, None, None)) == "terceiros:n"
    assert banco.leituras == [novo]
    assert vezes == [1]


async def test_sem_carga_carregar_nao_faz_nada() -> None:
    cifra, _, _, banco = _cifra({})
    assert await cifra.carregar() == 0
    await cifra.fechar()
    assert banco.leituras == []


async def test_contexto_sem_sistema_nao_chega_a_cifra_nenhuma() -> None:
    cifra, propria, terceiros, banco = _cifra({})
    with pytest.raises(ContextoSemSistema):
        await cifra.decifrar("x", b"mcp-claro|partida|conferencia")
    assert (propria.usos, terceiros.usos, banco.leituras) == ([], [], [])


class CofreFalso:
    """Cofre que responde a cifra com contexto e sem contexto como mandado."""

    def __init__(self, *, com_contexto: Exception | None = None, sem_contexto: Exception | None = None) -> None:
        self.com_contexto, self.sem_contexto = com_contexto, sem_contexto
        self.pedidos: list[tuple[str, bytes | None]] = []

    async def cifrar(self, chave: str, texto: bytes, *, contexto: bytes | None = None) -> str:
        self.pedidos.append((chave, contexto))
        erro = self.com_contexto if contexto is not None else self.sem_contexto
        if erro is not None:
            raise erro
        return "vault:v1:x"


async def test_partida_aceita_chave_de_terceiros_derived() -> None:
    cofre = CofreFalso(sem_contexto=PedidoRecusado("missing 'context' for key derivation", status=400))
    await conferir_chave_de_terceiros(cofre, "mcp-claro-chaves-terceiros")
    contextos = [c for _, c in cofre.pedidos]
    assert len(contextos) == 2
    assert contextos[0] is not None
    assert contextos[1] is None
    assert {k for k, _ in cofre.pedidos} == {"mcp-claro-chaves-terceiros"}


@pytest.mark.parametrize(
    "cofre",
    [
        CofreFalso(),  # cifra sem contexto: a chave nao e derived
        CofreFalso(com_contexto=AcessoNegado("permission denied", status=403)),  # a chave nao existe
        CofreFalso(sem_contexto=AcessoNegado("permission denied", status=403)),
        CofreFalso(com_contexto=CofreIndisponivel("fora")),
        CofreFalso(sem_contexto=CofreIndisponivel("fora")),
        # 400 sem contexto por outro motivo (chave que nao existe, sem criar): so a cifra com contexto mostra.
        CofreFalso(
            com_contexto=PedidoRecusado("encryption key not found", status=400),
            sem_contexto=PedidoRecusado("encryption key not found", status=400),
        ),
    ],
    ids=["nao_derived", "sem_chave", "negado_sem_contexto", "fora_com_contexto", "fora_sem_contexto", "400_sem_chave"],
)
async def test_partida_recusa_chave_de_terceiros_que_falta_ou_nao_e_derived(cofre: Any) -> None:
    with pytest.raises(ConfiguracaoInvalida):
        await conferir_chave_de_terceiros(cofre, "mcp-claro-chaves-terceiros")
