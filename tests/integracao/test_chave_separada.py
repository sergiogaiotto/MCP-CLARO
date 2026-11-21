"""Chave separada dos servidores de terceiros (G3a, I1; critica B9), com o OpenBao 2.7.2 de verdade.

A chave Transit dos terceiros (``VAULT_TRANSIT_KEY_THIRD_PARTY``) e outra: o cifrado de um terceiro nao abre com a
chave dos proprios nem com o mesmo contexto; girar a de terceiros nao muda os proprios; sem a de terceiros, os
proprios seguem e a acao de terceiro "nao sai". O despachante confere na partida que ela existe e e ``derived``.

"Chave de terceiros indisponivel" e feito como o teste faz, sem ``deletion_allowed`` (o script do cofre nao liga):
a politica do despachante perde o acesso a chave de terceiros (o cofre nega, como se ela nao existisse).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from exemplos import mal_comportado
from mcp_claro.cadastro import repositorio
from mcp_claro.cofre import AcessoNegado, CifradoInvalido, Cofre
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.db.engine import criar_engine, url_asyncpg
from mcp_claro.despachante import app as despachante_app
from mcp_claro.despachante import chaves
from mcp_claro.despachante.cifra_origem import CifraPorOrigem, conferir_chave_de_terceiros, engine_sem_pool
from mcp_claro.despachante.servico import Despachante, LeituraRecusada
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado
from tests import apoio_g3a as g
from tests import apoio_terceiros as t3
from tests.apoio import servidor
from tests.integracao.test_cofre import CHAVE, POLITICAS, Bao, openbao

__all__ = ["openbao"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

TERCEIROS = "mcp-claro-chaves-terceiros"
CONSULTAR = {
    "name": "consultar_pedido",
    "description": "Consulta a situacao de um pedido.",
    "inputSchema": {"type": "object", "properties": {"pedido": {"type": "string", "maxLength": 40}}},
}


@pytest.fixture(scope="module")
def bao(openbao: Bao) -> Bao:
    """O cofre de teste ja vem com a chave de terceiros: ``test_cofre.preparar`` a cria como
    scripts/iniciar-cofre.sh (derived, nao exportavel)."""
    return openbao


def test_o_cofre_de_teste_ja_vem_com_a_chave_dos_terceiros_como_o_script(bao: Bao) -> None:
    for nome in (CHAVE, TERCEIROS):
        chave = bao.api("GET", f"transit/keys/{nome}")["data"]
        assert (chave["type"], chave["derived"], chave["exportable"]) == ("aes256-gcm96", True, False)


@dataclass
class Cena:
    bao: Bao
    p: Parametros
    dono: AsyncEngine
    desp_db: AsyncEngine


@pytest.fixture
async def cena(bao: Bao, banco_migrado: Parametros) -> AsyncIterator[Cena]:
    vault = bao.parametros("despachante")
    p = banco_migrado.model_copy(
        update={
            "VAULT_ADDR": vault.VAULT_ADDR,
            "VAULT_ROLE_ID_FILE": vault.VAULT_ROLE_ID_FILE,
            "VAULT_SECRET_ID_FILE": vault.VAULT_SECRET_ID_FILE,
        }
    )
    dono = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    try:
        await g.abrir_a_saida(dono)
        yield Cena(bao, p, dono, desp_db)
    finally:
        await dono.dispose()
        await desp_db.dispose()


def _por_origem(cofre: Cofre, engine: AsyncEngine) -> CifraPorOrigem:
    return CifraPorOrigem.do_banco(engine, propria=cofre.cifra(), terceiros=cofre.cifra(TERCEIROS))


async def _sistemas(dono: AsyncEngine, endereco: str = "https://t.exemplo.teste/mcp") -> tuple[uuid.UUID, uuid.UUID]:
    async with dono.begin() as c:
        terceiro = await t3.cadastrar_terceiro(c, nome=f"g3a-cofre-t-{uuid.uuid4().hex[:6]}", endereco=endereco)
        proprio = await repositorio.cadastrar_sistema(
            c, nome=f"g3a-cofre-p-{uuid.uuid4().hex[:6]}", endereco=endereco, tipo_chave="compartilhada"
        )
    return terceiro, proprio


async def _cifrada(dono: AsyncEngine, sistema_id: uuid.UUID) -> str:
    async with dono.connect() as c:
        return str(
            (
                await c.execute(
                    text("SELECT cifrada FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id IS NULL"),
                    {"s": sistema_id},
                )
            ).scalar_one()
        )


def test_politica_do_despachante_usa_as_duas_chaves() -> None:
    politica = (POLITICAS / "despachante.hcl").read_text(encoding="utf-8")
    for chave in (CHAVE, TERCEIROS):
        for acao in ("encrypt", "decrypt"):
            assert f'path "transit/{acao}/{chave}"' in politica


async def test_cifrado_de_terceiro_nao_abre_com_a_chave_dos_proprios_nem_com_o_mesmo_contexto(cena: Cena) -> None:
    terceiro, proprio = await _sistemas(cena.dono)
    async with Cofre(cena.bao.parametros("despachante")) as cofre:
        cifra = _por_origem(cofre, cena.dono)
        async with cena.dono.begin() as c:
            await chaves.guardar(c, cifra, sistema_id=terceiro, segredo="segredo-do-terceiro")
            await chaves.guardar(c, cifra, sistema_id=proprio, segredo="segredo-do-proprio")
        do_terceiro, do_proprio = await _cifrada(cena.dono, terceiro), await _cifrada(cena.dono, proprio)
        contexto_terceiro = chaves.contexto(terceiro, None, None)
        contexto_proprio = chaves.contexto(proprio, None, None)
        # O mesmo contexto, a outra chave: recusado nos dois sentidos.
        with pytest.raises(CifradoInvalido):
            await cofre.decifrar(CHAVE, do_terceiro, contexto=contexto_terceiro)
        with pytest.raises(CifradoInvalido):
            await cofre.decifrar(TERCEIROS, do_proprio, contexto=contexto_proprio)
        assert await cofre.decifrar(TERCEIROS, do_terceiro, contexto=contexto_terceiro) == b"segredo-do-terceiro"
        async with cena.dono.connect() as c:
            assert await chaves.abrir(c, cifra, sistema_id=terceiro) == "segredo-do-terceiro"
            assert await chaves.abrir(c, cifra, sistema_id=proprio) == "segredo-do-proprio"


async def test_rodizio_da_chave_de_terceiros_nao_muda_os_proprios(cena: Cena) -> None:
    terceiro, proprio = await _sistemas(cena.dono)
    async with Cofre(cena.bao.parametros("despachante")) as cofre:
        cifra = _por_origem(cofre, cena.dono)
        async with cena.dono.begin() as c:
            await chaves.guardar(c, cifra, sistema_id=terceiro, segredo="antes-t")
            await chaves.guardar(c, cifra, sistema_id=proprio, segredo="antes-p")
        versao_t = cena.bao.api("GET", f"transit/keys/{TERCEIROS}")["data"]["latest_version"]
        versao_p = cena.bao.api("GET", f"transit/keys/{CHAVE}")["data"]["latest_version"]
        cena.bao.api("POST", f"transit/keys/{TERCEIROS}/rotate")
        assert cena.bao.api("GET", f"transit/keys/{TERCEIROS}")["data"]["latest_version"] == versao_t + 1
        assert cena.bao.api("GET", f"transit/keys/{CHAVE}")["data"]["latest_version"] == versao_p
        async with cena.dono.connect() as c:
            assert await chaves.abrir(c, cifra, sistema_id=terceiro) == "antes-t"
            assert await chaves.abrir(c, cifra, sistema_id=proprio) == "antes-p"
        async with cena.dono.begin() as c:
            await chaves.guardar(c, cifra, sistema_id=terceiro, segredo="depois-t")
            await chaves.guardar(c, cifra, sistema_id=proprio, segredo="depois-p")
        assert (await _cifrada(cena.dono, terceiro)).startswith(f"vault:v{versao_t + 1}:")
        assert (await _cifrada(cena.dono, proprio)).startswith(f"vault:v{versao_p}:")


async def test_sem_a_chave_de_terceiros_os_proprios_seguem_e_a_acao_de_terceiro_nao_sai(cena: Cena) -> None:
    original = (POLITICAS / "despachante.hcl").read_text(encoding="utf-8")
    sem = "\n\n".join(bloco for bloco in original.split("\n\n") if TERCEIROS not in bloco)
    assert TERCEIROS not in sem
    assert CHAVE in sem
    app = mal_comportado.criar()
    try:
        async with servidor(app) as base, Cofre(cena.bao.parametros("despachante")) as cofre:
            cifra = _por_origem(cofre, cena.dono)
            leitura_propria = await g.preparar(
                cena.dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="proprio"
            )
            leitura_terceiro = await g.preparar(
                cena.dono, cifra, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="terceiro"
            )
            acao_terceiro = await g.preparar(  # chave por pessoa: so ela serve a uma acao (D2)
                cena.dono,
                cifra,
                endereco=f"{base}/mcp",
                definicao=CONSULTAR,
                origem="terceiro",
                tipo_chave="por_pessoa",
                chave="chave-ana",
            )
            cena.bao.api("PUT", "sys/policies/acl/mcp-claro-despachante", {"policy": sem})
            # O cofre nega a chave de terceiros ao despachante; a dos proprios segue.
            with pytest.raises(AcessoNegado):
                await cofre.cifrar(TERCEIROS, b"x", contexto=chaves.contexto(leitura_terceiro.sistema_id, None, None))
            desp = g.despachante(cena.desp_db, cifra)
            propria = await desp.ler(leitura_propria.pedido({"pedido": "1"}))
            assert propria.get("isError") is not True
            assert app.state.contagem.chamadas.get("consultar_pedido") == 1
            # A acao de terceiro: a porta de saida nao abre a chave e a acao nao sai (nada vai ao sistema).
            chamadas: list[Any] = []

            async def chamar(*a: Any, **k: Any) -> Any:
                chamadas.append(a)
                raise AssertionError("nada sai sem a chave")

            porta = PortaDeSaida(cena.desp_db, cifra, chamar=chamar)
            async with cena.dono.connect() as c:
                sistema = await repositorio.sistema_por_id(c, acao_terceiro.sistema_id)
            assert sistema is not None
            ferramenta = SimpleNamespace(sistema=sistema, nome_original="consultar_pedido", campos_pessoais=())
            op = SimpleNamespace(
                pessoa_id=acao_terceiro.pessoa_id, aplicativo_id=acao_terceiro.aplicativo_id, argumentos={}
            )
            estado, resultado, desfecho, _ = await porta._enviar(op, ferramenta, None, None)  # type: ignore[arg-type]
            assert (estado, resultado) == (Estado.NAO_SAIU, None)
            assert desfecho == "nao deu para abrir a chave no cofre"
            assert chamadas == []
            # A leitura do terceiro pelo despachante: recusa legivel, nunca erro interno, e nada sai.
            with pytest.raises(LeituraRecusada) as recusa:
                await g.despachante(cena.desp_db, cifra).ler(leitura_terceiro.pedido({"pedido": "1"}))
            assert recusa.value.motivo == "chave_indisponivel"
            assert app.state.contagem.chamadas.get("consultar_pedido") == 1
    finally:
        cena.bao.api("PUT", "sys/policies/acl/mcp-claro-despachante", {"policy": original})


def _papel_de_teste(bao: Bao, pasta: Path) -> Parametros:
    """Um AppRole so deste teste, que cifra com as chaves ``g3a-*`` (para provar a conferencia da partida)."""
    politica = 'path "transit/encrypt/g3a-*" {\n  capabilities = ["update"]\n}\n'
    bao.api("PUT", "sys/policies/acl/mcp-claro-g3a-teste", {"policy": politica})
    bao.api("POST", "auth/approle/role/g3a_teste", {"token_policies": ["mcp-claro-g3a-teste"], "token_ttl": "15m"})
    role_id = bao.api("GET", "auth/approle/role/g3a_teste/role-id")["data"]["role_id"]
    secret_id = bao.api("POST", "auth/approle/role/g3a_teste/secret-id")["data"]["secret_id"]
    (pasta / "role_id").write_text(role_id, encoding="utf-8")
    (pasta / "secret_id").write_text(secret_id, encoding="utf-8")
    return Parametros(
        APP_ENV="teste",
        VAULT_ADDR=bao.url,
        VAULT_ROLE_ID_FILE=str(pasta / "role_id"),
        VAULT_SECRET_ID_FILE=str(pasta / "secret_id"),
    )


async def test_partida_confere_que_a_chave_de_terceiros_existe_e_e_derived(bao: Bao, tmp_path: Path) -> None:
    async with Cofre(bao.parametros("despachante")) as cofre:
        await conferir_chave_de_terceiros(cofre, TERCEIROS)  # derived: sem contexto, o cofre recusa com 400
    bao.api("POST", "transit/keys/g3a-nao-derivada", {"type": "aes256-gcm96", "derived": False})
    async with Cofre(_papel_de_teste(bao, tmp_path)) as teste:
        with pytest.raises(ConfiguracaoInvalida, match="nao e derived"):
            await conferir_chave_de_terceiros(teste, "g3a-nao-derivada")
        with pytest.raises(ConfiguracaoInvalida, match="nao existe"):
            await conferir_chave_de_terceiros(teste, "g3a-sumida")


async def test_app_entra_no_cofre_com_a_cifra_por_origem_e_recusa_a_partida_sem_a_chave(
    cena: Cena, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = despachante_app.criar_app_despachante(cena.p, cena.desp_db, None)
    async with app.router.lifespan_context(app):
        cifra = app.state.despachante.cifra
        assert isinstance(cifra.alvo, CifraPorOrigem)
        assert app.state.porta.cifra is cifra
        await asyncio.wait_for(app.state.porta.assumiu.wait(), 10)
    sem_chave = despachante_app.criar_app_despachante(
        cena.p.model_copy(update={"VAULT_TRANSIT_KEY_THIRD_PARTY": "g3a-chave-que-nao-existe"}), cena.desp_db, None
    )
    with pytest.raises(ConfiguracaoInvalida):
        async with sem_chave.router.lifespan_context(sem_chave):
            pass


class _Eco:
    """Cifra falsa que anota o contexto de cada uso (o cifrado e o proprio texto, em hexadecimal)."""

    def __init__(self, nome: str) -> None:
        self.nome = nome
        self.usos = 0

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        self.usos += 1
        return "vault:v1:" + texto.hex()

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        self.usos += 1
        return bytes.fromhex(cifrado.removeprefix("vault:v1:"))


async def test_cifra_por_origem_nunca_pede_conexao_ao_pool_de_quem_chama(banco_migrado: Parametros) -> None:
    """Com o pool do despachante cheio (uma conexao so, segurada por quem chama a cifra) e a origem ainda nao lida, a
    busca da origem nao espera por ele (antes: ``TimeoutError`` do pool, erro 500 numa leitura de sistema proprio)."""
    dono = criar_engine(banco_migrado, pool_size=2, max_overflow=0)
    pequeno = create_async_engine(
        url_asyncpg(banco_migrado),
        connect_args={"ssl": False, "server_settings": {"role": "mcp_claro_despachante"}},
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
    )
    propria, de_terceiros = _Eco("propria"), _Eco("terceiros")
    try:
        async with servidor(mal_comportado.criar()) as base:
            arr = await g.preparar(dono, propria, endereco=f"{base}/mcp", definicao=CONSULTAR, origem="proprio")
            terceiro, _ = await _sistemas(dono)
            cifra = CifraPorOrigem.do_banco(pequeno, propria=propria, terceiros=de_terceiros)
            # A busca usa a mesma conexao (o mesmo papel), sem o pool do engine.
            async with engine_sem_pool(pequeno).connect() as c:
                assert (await c.execute(text("SELECT current_user"))).scalar_one() == "mcp_claro_despachante"
            # Quem chama segura a unica conexao, no meio de uma transacao (como a renovacao com trava).
            async with pequeno.begin() as c:
                await asyncio.wait_for(chaves.guardar(c, cifra, sistema_id=terceiro, segredo="t"), 20)
                assert await asyncio.wait_for(chaves.abrir(c, cifra, sistema_id=terceiro), 20) == "t"
            assert de_terceiros.usos == 2
            # A leitura de um sistema proprio, com a origem ainda nao lida (ler segura a conexao ao abrir a chave).
            usos = propria.usos
            desp = Despachante(pequeno, cifra, ambiente="teste", prazo_leitura=5)
            resultado = await asyncio.wait_for(desp.ler(arr.pedido({"pedido": "1"})), 20)
            assert resultado.get("isError") is not True
            assert propria.usos == usos + 1
            await cifra.fechar()
    finally:
        await pequeno.dispose()
        await dono.dispose()


async def test_app_carrega_as_origens_na_partida(cena: Cena) -> None:
    terceiro, proprio = await _sistemas(cena.dono)
    app = despachante_app.criar_app_despachante(cena.p, cena.desp_db, None)
    async with app.router.lifespan_context(app):
        cifra = app.state.despachante.cifra.alvo
        assert isinstance(cifra, CifraPorOrigem)
        lidas: list[uuid.UUID] = []
        busca = cifra._origem_de

        async def anotada(sistema_id: uuid.UUID) -> str | None:
            lidas.append(sistema_id)
            return await busca(sistema_id)

        cifra._origem_de = anotada
        assert await cifra.origem(terceiro) == "terceiro"
        assert await cifra.origem(proprio) == "proprio"
        assert lidas == []  # ja estavam na memoria desde a partida
        novo, _ = await _sistemas(cena.dono)
        assert await cifra.origem(novo) == "terceiro"
        assert lidas == [novo]
