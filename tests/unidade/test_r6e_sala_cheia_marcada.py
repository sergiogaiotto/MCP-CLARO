"""R6e-3, retoque: a sala cheia, dita pelo processo pai, e os ramos do contrato K7 que ainda nao tinham teste.

- O processo pai da sala poe o cabecalho ``CABECALHO_DA_SALA`` so na resposta de sala cheia; o filho escreve so o
  status e o corpo e nao consegue imita-la. Na leitura das declaracoes, a sala cheia com o cabecalho e
  ``SalaSemVaga``, e o despachante responde ``DeclaracoesNaoConferidas("sala_sem_vaga")`` (503): a rodada nao conta
  contra um terceiro que nada fez (defeito 3 do verificador). O mesmo corpo escrito por um filho, sem o cabecalho,
  continua contando (critica A5).
- A leitura de um terceiro com a sala cheia devolve a frase propria e conta ``sala_sem_vaga{onde="leitura"}``; num
  sistema proprio (que nunca passa pela sala), nada disso (defeito 4 do verificador).
"""

from __future__ import annotations

import contextlib
import functools
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from mcp_claro.cadastro.modelos import Sistema
from mcp_claro.despachante import servico
from mcp_claro.despachante.servico import DeclaracoesIndisponiveis, DeclaracoesNaoConferidas, interpretar
from mcp_claro.ligacao import sala_contrato
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.ligacao.sala_contrato import CABECALHO_DA_SALA, SALA_SEM_VAGA, FalhaNaSala, SalaSemVaga
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from mcp_claro.visitas import app as visitas_app
from tests import apoio_visitas
from tests.apoio_visitas import Certificados
from tests.unidade.test_r6e_sala_vagas import _ocupar
from tests.unidade.test_sala_salto import _ferramenta
from tests.unidade.test_visitas_app import _chave_de, _pedidos_do_espiao, _sala_no_ar

pytestmark = pytest.mark.anyio

Y = "https://outro-terceiro.exemplo.teste/mcp"
MOTIVO = PREFIXO_META + "motivo"


@pytest.fixture(scope="module")
def certificados(tmp_path_factory: pytest.TempPathFactory) -> Certificados:
    return apoio_visitas.gerar_certificados(tmp_path_factory.mktemp("certificados_r6e_cheia"))


@pytest.fixture
def espiao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pasta = tmp_path / "espiao"
    pasta.mkdir()
    monkeypatch.setenv(apoio_visitas.ESPIAO, str(pasta))
    return pasta


def _contador(nome: str, **rotulos: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    alvo = json.dumps(rotulos, sort_keys=True, ensure_ascii=False)
    return sum(n for (_, nome_, r), n in medidas.items() if nome_ == nome and r == alvo)


def _sistema(origem: str = "terceiro", endereco: str = Y) -> Sistema:
    return Sistema(uuid.uuid4(), f"sis-{origem}", endereco, origem, "compartilhada", 1, "ativo", VERSAO_MCP)  # type: ignore[arg-type]


def _despachante_com(ligacao: Any) -> Any:
    """``Despachante._listar`` sem banco: a sala cheia nao grava linha (nada saiu, nada foi pedido)."""

    @contextlib.asynccontextmanager
    async def begin() -> AsyncIterator[None]:
        raise AssertionError("a sala cheia nao grava nada no banco")
        yield None  # pragma: no cover

    despachante: Any = object.__new__(servico.Despachante)
    despachante.ligacao = lambda _sistema: ligacao
    despachante.engine = SimpleNamespace(begin=begin)
    return despachante


async def _postar_listagem(cs: Any, p: Any) -> httpx2.Response:
    pedido = sala_contrato.PedidoListar(endereco=Y).para_json()
    async with httpx2.AsyncClient(verify=cs._tls, timeout=30, trust_env=False) as http:
        return await http.post(str(p.VISITS_INTERNAL_URL).rstrip("/") + sala_contrato.ROTA_LISTAR, content=pedido)


async def test_a_sala_cheia_marca_a_propria_resposta_e_as_declaracoes_nao_contam_contra_o_terceiro(
    certificados: Certificados, espiao: Path
) -> None:
    async with _sala_no_ar(
        certificados, alvo=apoio_visitas.atende_devagar, max_chamadas=1, prontos=1, prazo_vaga=0.3
    ) as (cs, p, _sala):
        primeira = await _ocupar(cs, p, espiao, 5.0)
        cru = await _postar_listagem(cs, p)
        with pytest.raises(SalaSemVaga) as cheia:
            await cs.listar(Y, _chave_de("chave-de-y"), None)
        antes = _contador("sala_sem_vaga", onde="declaracoes")
        with pytest.raises(DeclaracoesNaoConferidas) as falha:
            await _despachante_com(cs)._listar(_sistema(), "chave-de-y", None)
        depois = _contador("sala_sem_vaga", onde="declaracoes")
        assert await primeira == 200
    assert (cru.status_code, cru.headers.get(CABECALHO_DA_SALA)) == (200, SALA_SEM_VAGA)
    assert cru.content == visitas_app._SEM_VAGA[sala_contrato.ROTA_LISTAR]
    assert (cheia.value.tipo, cheia.value.excecao) == ("nao_enviado", SALA_SEM_VAGA)  # continua um FalhaNaSala
    assert falha.value.motivo == SALA_SEM_VAGA
    assert str(falha.value) == f"a sala de visitas estava cheia; nada foi pedido ao sistema {_sistema().nome}"
    assert depois == antes + 1
    assert _pedidos_do_espiao(espiao) == {}  # nada de Y chegou a um filho


async def test_o_filho_que_escreve_o_corpo_de_sala_cheia_nao_tem_a_marca_e_a_rodada_conta(
    certificados: Certificados,
) -> None:
    """Critica A5: um filho tomado pelo terceiro pode escrever o corpo da sala cheia, mas nao o cabecalho do pai."""
    saida = b"200" + visitas_app._SEM_VAGA[sala_contrato.ROTA_LISTAR]
    alvo = functools.partial(apoio_visitas.responde_cru, saida)
    async with _sala_no_ar(certificados, alvo=alvo, prontos=0) as (cs, p, _sala):
        cru = await _postar_listagem(cs, p)
        with pytest.raises(FalhaNaSala) as forjada:
            await cs.listar(Y, _chave_de("chave-de-y"), None)
        antes = _contador("sala_sem_vaga", onde="declaracoes")
        with pytest.raises(DeclaracoesIndisponiveis) as falha:
            await _despachante_com(cs)._listar(_sistema(), "chave-de-y", None)
        depois = _contador("sala_sem_vaga", onde="declaracoes")
    assert (cru.status_code, cru.content) == (200, saida[3:])
    assert CABECALHO_DA_SALA not in cru.headers
    assert type(forjada.value) is FalhaNaSala
    assert (forjada.value.tipo, forjada.value.excecao) == ("nao_enviado", SALA_SEM_VAGA)
    assert type(falha.value) is DeclaracoesIndisponiveis  # 502: conta como o servidor que nao entregou
    assert depois == antes  # a sala nao estava cheia


def test_so_a_marca_do_pai_com_o_corpo_de_sala_cheia_e_sala_sem_vaga() -> None:
    corpo = visitas_app._SEM_VAGA[sala_contrato.ROTA_LISTAR]
    with pytest.raises(SalaSemVaga):
        sala_contrato.ler_resposta_listagem(200, corpo, marca=SALA_SEM_VAGA)
    for marca in (None, "", "outra", SALA_SEM_VAGA.upper()):
        with pytest.raises(FalhaNaSala) as falha:
            sala_contrato.ler_resposta_listagem(200, corpo, marca=marca)
        assert type(falha.value) is FalhaNaSala
    # A marca sem o corpo de sala cheia nao vira sala cheia (o corpo e o do pai, sempre junto com a marca).
    fora = sala_contrato.RespostaListar(tipo="nao_enviado", excecao=sala_contrato.PEDIDO_FORA_DO_CONTRATO).para_json()
    with pytest.raises(FalhaNaSala) as falha:
        sala_contrato.ler_resposta_listagem(200, fora, marca=SALA_SEM_VAGA)
    assert type(falha.value) is FalhaNaSala
    lista = sala_contrato.RespostaListar(tipo="resultado", ferramentas=[]).para_json()
    assert sala_contrato.ler_resposta_listagem(200, lista, marca=SALA_SEM_VAGA).ferramentas == []
    # Status que nao e 200: duvida, com ou sem marca.
    with pytest.raises(FalhaNaSala) as falha:
        sala_contrato.ler_resposta_listagem(500, corpo, marca=SALA_SEM_VAGA)
    assert type(falha.value) is FalhaNaSala
    assert falha.value.tipo == "duvida"


def test_a_leitura_de_um_terceiro_com_a_sala_cheia_tem_frase_propria_e_conta() -> None:
    sistema = _sistema()
    leitura = interpretar(
        Resposta("nao_enviado", excecao=SALA_SEM_VAGA), _ferramenta(sistema), com_chave_da_pessoa=True
    )
    assert leitura.resultado["content"][0]["text"] == (
        f"A sala de visitas estava cheia; nada foi enviado ao servidor de terceiros {sistema.nome}. Tente de novo em"
        " instantes."
    )
    assert leitura.resultado["isError"] is True
    assert leitura.resultado["_meta"][MOTIVO] == SALA_SEM_VAGA
    assert leitura.sala is None  # nao e linha da sala: so o contador
    assert (SALA_SEM_VAGA, {"onde": "leitura"}) in leitura.contadores
    assert leitura.dados == {"desfecho": "nao_enviado", "com_erro": True, "texto_de_fora": False}


def test_num_sistema_proprio_nao_ha_frase_nem_contador_da_sala_cheia() -> None:
    sistema = _sistema("proprio", "https://proprio.exemplo.teste/mcp")
    leitura = interpretar(
        Resposta("nao_enviado", excecao=SALA_SEM_VAGA), _ferramenta(sistema), com_chave_da_pessoa=True
    )
    assert leitura.resultado["content"][0]["text"] == (
        f"O sistema {sistema.nome} nao respondeu a leitura (nao_enviado: nada foi enviado)."
    )
    assert leitura.resultado["_meta"][MOTIVO] == "falha_nao_enviado"
    assert all(nome != SALA_SEM_VAGA for nome, _ in leitura.contadores)
