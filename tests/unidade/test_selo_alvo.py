"""O alvo fechado da bateria do selo (``selo/alvo.py``; desenho 3.2, regras 1 e 2; criticas A3.1 e A4).

Com ``hypothesis`` sobre os nomes, os lados e os argumentos, nos dois modos: nada fora da lista fechada abre conexao
(nem a ligacao nem o corte sao chamados). Com ``hypothesis`` sobre declaracoes montadas sem a conferencia do cadastro:
uma leitura com o nome de uma acao (da declaracao ou do sistema) nem cria o alvo, nos dois modos (critica A4.2). Na
producao, so ``tools/list``, ``selo_alvo`` e a consulta de controle com um numero novo; nenhuma escrita antes da marca
positiva, nem com a producao (ou o alvo) sem resposta a ``selo_alvo``, nem com um ``selo_alvo`` fora do contrato; no
modo de ferramentas de teste, a acao real nunca sai e a bateria para na primeira resposta sem a marca (inclusive
depois de uma com ela); o alvo que e a producao escrita de outro jeito e recusado antes de qualquer rede; o teto de
chamadas vale antes de abrir conexao; o corte manda o pedido inteiro, com o numero so no ``_meta``, para o mesmo
caminho e o mesmo Host que o cliente fixado (``%XX`` fora do ASCII), e fecha.
"""

from __future__ import annotations

import copy
import json
import ssl
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2
import mcp_types as types
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from exemplos import selo_nivel2
from mcp_claro.ligacao.cliente import Listagem, Resposta
from mcp_claro.selo import contrato
from mcp_claro.selo.alvo import AlvoDoTeste, ForaDoAlvo, SemMarca, TetoDeChamadas, acoes_e_leituras
from mcp_claro.selo.declaracao import Cadastro, Declaracao, validar

pytestmark = pytest.mark.anyio

PRODUCAO = "https://cobranca.exemplo.teste/mcp"
ALVO = "https://teste.cobranca.exemplo.teste/mcp"


def _resultado(dados: Mapping[str, Any] | None, *, meta: Mapping[str, Any] | None = None) -> Resposta:
    return Resposta(
        "resultado",
        resultado=types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(dados))],
            structured_content=None if dados is None else dict(dados),
            is_error=False,
            _meta=None if meta is None else dict(meta),
        ),
    )


@dataclass
class LigacaoQueAnota:
    """Uma ligacao falsa: anota cada chamada (endereco, nome, argumentos, numero) e responde o que o alvo espera."""

    chamadas: list[tuple[str, str, dict[str, Any], str | None]] = field(default_factory=list)
    listagens: list[str] = field(default_factory=list)
    marca: bool = True
    marcas: list[bool] | None = None
    """A marca de cada resposta de ferramenta de teste, na ordem (depois, ``marca``)."""
    alvo_diz: Mapping[str, str] = field(default_factory=lambda: {PRODUCAO: "producao", ALVO: "teste"})
    quem_sou: Mapping[str, Resposta] = field(default_factory=dict)
    """A resposta de ``selo_alvo`` por endereco, no lugar da do contrato (sem resposta, fora do contrato)."""

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Any,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        traceparent: str | None = None,
        prazo: float = 30.0,
        esquema_declarado: dict[str, Any] | None = None,
        teto_resposta: int = 0,
        chave_operacao: str | None = None,
    ) -> Resposta:
        self.chamadas.append((endereco, nome, dict(argumentos), chave_operacao))
        if nome == contrato.FERRAMENTA_ALVO:
            if endereco in self.quem_sou:
                return self.quem_sou[endereco]
            return _resultado({"contrato": contrato.CONTRATO, "alvo": self.alvo_diz.get(endereco, "producao")})
        marca = self.marcas.pop(0) if self.marcas else self.marca
        meta = {contrato.CHAVE_ALVO: "teste"} if marca else {}
        return _resultado({"itens": []}, meta=meta)

    async def listar(self, endereco: str, segredo: Any, discover_json: str | None) -> Listagem:
        self.listagens.append(endereco)
        return Listagem([])

    async def listar_ferramentas(self, endereco: str, segredo: Any, discover_json: str | None) -> list[dict[str, Any]]:
        return (await self.listar(endereco, segredo, discover_json)).ferramentas

    async def sondar(self, endereco: str, segredo: Any, *, prazo: float = 10.0) -> Any:
        raise AssertionError("a bateria nao sonda")


@dataclass
class Fluxo:
    enviado: bytearray = field(default_factory=bytearray)
    fechado: bool = False

    async def send(self, item: bytes) -> None:
        self.enviado.extend(item)

    async def aclose(self) -> None:
        self.fechado = True


@dataclass
class Conexoes:
    abertas: list[tuple[str, int, bool, str]] = field(default_factory=list)
    fluxos: list[Fluxo] = field(default_factory=list)

    async def __call__(self, nome: str, porta: int, tls: ssl.SSLContext | None, nome_tls: str) -> Fluxo:
        self.abertas.append((nome, porta, tls is not None, nome_tls))
        fluxo = Fluxo()
        self.fluxos.append(fluxo)
        return fluxo


def declaracao(modo: str = "ambiente_de_teste", *, endereco: str | None = ALVO) -> Declaracao:
    return validar(
        selo_nivel2.declaracao(
            endereco_teste=endereco if modo == "ambiente_de_teste" else None,
            modo=modo,  # type: ignore[arg-type]
        )
    )


def alvo_do_teste(
    modelo: Declaracao | None = None,
    *,
    ligacao: LigacaoQueAnota | None = None,
    conectar: Conexoes | None = None,
    nivel: int = 3,
    teto: int = 1000,
    producoes: tuple[str, ...] = (),
    cadastro: Cadastro | None = None,
) -> tuple[AlvoDoTeste, LigacaoQueAnota, Conexoes]:
    ligacao = ligacao or LigacaoQueAnota()
    conectar = conectar or Conexoes()
    alvo = AlvoDoTeste(
        modelo or declaracao(),
        producao=PRODUCAO,
        producoes=producoes,
        nivel=nivel,
        ligacao=ligacao,
        segredo_teste="chave-teste",
        segredo_producao="chave-declaracoes",
        teto=teto,
        conectar=conectar,
        cadastro=cadastro,
    )
    return alvo, ligacao, conectar


def cadastro_do_duble(*, acoes: tuple[str, ...] = (), revisadas: tuple[str, ...] = ()) -> Cadastro:
    """O cadastro do duble (as acoes e as leituras dele), com acoes e revisadas a mais."""
    return Cadastro(
        de_terceiro=False,
        tipo_chave="por_pessoa",
        acoes=frozenset(selo_nivel2.ACOES) | frozenset(acoes),
        leituras=frozenset(selo_nivel2.LEITURAS),
        revisadas=frozenset(selo_nivel2.ACOES) | frozenset(selo_nivel2.LEITURAS) | frozenset(revisadas),
        producoes=(PRODUCAO,),
    )


def _nada_saiu(ligacao: LigacaoQueAnota, conectar: Conexoes, antes: int = 0) -> None:
    assert len(ligacao.chamadas) == antes
    assert not ligacao.listagens
    assert not conectar.abertas


# --- A lista fechada, com hypothesis ----------------------------------------------------------------------------

_MODELO = declaracao()
_ACOES, _LEITURAS = acoes_e_leituras(_MODELO)
_MODELOS = {"ambiente_de_teste": _MODELO, "ferramentas_de_teste": declaracao("ferramentas_de_teste")}
_LISTAS = {modo: acoes_e_leituras(modelo) for modo, modelo in _MODELOS.items()}
_CONHECIDOS = (
    frozenset(selo_nivel2.ACOES)
    | {a + selo_nivel2.SUFIXO_TESTE for a in selo_nivel2.ACOES}
    | frozenset(_LEITURAS)
    | contrato.NOMES_RESERVADOS
)
argumentos_quaisquer = st.dictionaries(
    st.text(max_size=12), st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20)), max_size=4
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    modo=st.sampled_from(sorted(_MODELOS)),
    nome=st.one_of(st.text(max_size=40), st.sampled_from(sorted(_CONHECIDOS))),
    lado=st.sampled_from(["alvo", "producao"]),
    argumentos=st.one_of(
        argumentos_quaisquer,
        st.sampled_from([e.argumentos for e in _MODELO.ferramentas.values()] + [{}]),
    ),
    com_numero=st.booleans(),
)
async def test_nada_fora_da_lista_abre_conexao(
    modo: str, nome: str, lado: str, argumentos: dict[str, Any], com_numero: bool
) -> None:
    """Qualquer nome e quaisquer argumentos, nos dois lados e nos dois modos, com a marca ja conferida: o que nao esta
    na lista fechada (nome, lado, argumentos declarados e numero da bateria) levanta ``ForaDoAlvo`` antes de abrir
    conexao. No modo de ferramentas de teste, o nome de uma acao real nunca esta na lista."""
    modelo = _MODELOS[modo]
    acoes, leituras = _LISTAS[modo]
    alvo, ligacao, conectar = alvo_do_teste(modelo)
    await alvo.conferir_marcas()
    antes = len(ligacao.chamadas)
    numero = alvo.numero_novo() if com_numero else None
    declarado = json.dumps(argumentos, sort_keys=True) in _declarados(modelo).get(nome, set())
    acao_permitida = nome in acoes and numero is not None and declarado
    leitura_permitida = nome in leituras and numero is None and declarado
    if modo == "ferramentas_de_teste":
        assert not (acao_permitida or leitura_permitida) or nome not in selo_nivel2.ACOES
    # selo_alvo sem argumentos vale nos dois lados; a consulta e o aviso so com numeros da bateria (nunca os gerados).
    quem_sou = nome == contrato.FERRAMENTA_ALVO and argumentos == {} and numero is None
    chamada_permitida = quem_sou or (lado == "alvo" and (acao_permitida or leitura_permitida))
    if not chamada_permitida:
        with pytest.raises(ForaDoAlvo):
            await alvo.chamar(lado, nome, argumentos, numero=numero)  # type: ignore[arg-type]
    if not acao_permitida:
        with pytest.raises(ForaDoAlvo):
            await alvo.mandar_e_cortar(nome, argumentos, numero or str(uuid.uuid4()))
    if chamada_permitida or acao_permitida:
        return  # o caminho permitido e provado abaixo, com os argumentos declarados
    _nada_saiu(ligacao, conectar, antes)


@settings(max_examples=200, deadline=None)
@given(numero=st.one_of(st.uuids().map(str), st.text(max_size=40)))
async def test_numero_que_a_bateria_nao_criou_nao_sai(numero: str) -> None:
    """Os numeros que vao ao sistema (no ``_meta`` da escrita, na consulta, no aviso) sao todos criados pela bateria."""
    alvo, ligacao, conectar = alvo_do_teste()
    await alvo.conferir_marcas()
    antes = len(ligacao.chamadas)
    acao = _MODELO.ferramentas["enviar_lembrete"]
    with pytest.raises(ForaDoAlvo):
        await alvo.chamar("alvo", "enviar_lembrete", acao.argumentos, numero=numero)
    with pytest.raises(ForaDoAlvo):
        await alvo.chamar("alvo", contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
    with pytest.raises(ForaDoAlvo):
        await alvo.chamar("producao", contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
    with pytest.raises(ForaDoAlvo):
        await alvo.chamar("alvo", contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": alvo.numero_novo(), "operacoes": [numero]})
    _nada_saiu(ligacao, conectar, antes)


def _declarados(modelo: Declaracao) -> dict[str, set[str]]:
    """Os argumentos declarados de cada acao (pelo nome que a bateria chama) e leitura (para o teste saber o que e
    permitido)."""
    saida: dict[str, set[str]] = {}
    for nome, e in modelo.ferramentas.items():
        conjuntos = [e.argumentos, e.argumentos_outros, e.argumentos_recusados]
        for opcional in (e.efeito_adiado, e.escrita_lenta, e.varios_itens, e.pede_dados):
            if opcional is not None:
                conjuntos.append(opcional.argumentos)
        chamado = (e.ferramenta_de_teste or nome) if modelo.modo == "ferramentas_de_teste" else nome
        saida.setdefault(chamado, set()).update(json.dumps(a, sort_keys=True) for a in conjuntos)
        leituras = [e.rastro]
        if e.idempotente is not None and e.idempotente.rastro is not None:
            leituras.append(e.idempotente.rastro)
        if e.conflito is not None:
            saida.setdefault(e.conflito.leitura.ferramenta, set()).add(
                json.dumps(e.conflito.leitura.argumentos, sort_keys=True)
            )
        for r in leituras:
            saida.setdefault(r.ferramenta, set()).add(json.dumps(r.argumentos, sort_keys=True))
    return saida


_LEITURAS_DA_ENTRADA = ("rastro", "conflito", "idempotente")


def _trocar_leitura(dados: dict[str, Any], acao: str, qual: str, nome: str) -> bool:
    """Poe ``nome`` no lugar da leitura ``qual`` (rastro, leitura do conflito, rastro do conteudo) da entrada de
    ``acao``; False se a entrada nao tem essa leitura."""
    entrada = dados["ferramentas"][acao]
    if qual == "rastro":
        entrada["rastro"]["ferramenta"] = nome
    elif qual == "conflito" and "conflito" in entrada:
        entrada["conflito"]["leitura"]["ferramenta"] = nome
    elif qual == "idempotente" and (entrada.get("idempotente") or {}).get("rastro"):
        entrada["idempotente"]["rastro"]["ferramenta"] = nome
    else:
        return False
    return True


@settings(max_examples=150, deadline=None)
@given(
    modo=st.sampled_from(sorted(_MODELOS)),
    acao=st.sampled_from(selo_nivel2.ACOES),
    qual=st.sampled_from(_LEITURAS_DA_ENTRADA),
    real=st.one_of(st.sampled_from(selo_nivel2.ACOES), st.just("apagar_tudo")),
    com_cadastro=st.booleans(),
)
def test_leitura_que_e_acao_nem_cria_o_alvo(modo: str, acao: str, qual: str, real: str, com_cadastro: bool) -> None:
    """Critica A4.2: o modelo montado sem a conferencia do cadastro (como a bateria o veria se a tela e a conferencia
    falhassem), com uma leitura que e uma acao real: uma da declaracao ou, com o cadastro, uma do sistema que a
    declaracao nao traz (``apagar_tudo``). Nos dois modos (no de ferramentas de teste, a leitura iria a producao), o
    ``AlvoDoTeste`` nem e criado, e nada sai."""
    dados = copy.deepcopy(_MODELOS[modo].guardada())
    if not _trocar_leitura(dados, acao, qual, real):
        return
    if real == "apagar_tudo" and not com_cadastro:
        return  # sem o cadastro, o alvo so conhece as acoes da declaracao
    modelo = Declaracao.model_validate(dados)
    ligacao, conectar = LigacaoQueAnota(), Conexoes()
    cadastro = cadastro_do_duble(acoes=("apagar_tudo",)) if com_cadastro else None
    with pytest.raises(ForaDoAlvo) as erro:
        alvo_do_teste(modelo, ligacao=ligacao, conectar=conectar, cadastro=cadastro)
    assert erro.value.motivo == "acao_que_le"
    _nada_saiu(ligacao, conectar)


@pytest.mark.parametrize(
    ("mexer", "motivo"),
    [
        (lambda d: d["ferramentas"]["enviar_lembrete"].update(ferramenta_de_teste="listar_lidos_antigo"), None),
        (lambda d: d["ferramentas"]["enviar_lembrete"].update(ferramenta_de_teste="revisada_velha"), "teste_real"),
        (lambda d: d["ferramentas"]["enviar_lembrete"]["rastro"].update(ferramenta="contar_sem_revisar"), "le"),
    ],
    ids=["nome_livre", "ferramenta_de_teste_com_nome_de_revisada", "leitura_que_nao_e_revisada_le"],
)
def test_no_modo_de_ferramentas_de_teste_o_cadastro_fecha_a_lista(mexer: Any, motivo: str | None) -> None:
    """Critica A4.3, com o cadastro: no modo de ferramentas de teste (o alvo e a producao), a ferramenta de teste nao
    tem o nome de nenhuma revisada do sistema, de nenhuma classe, e cada leitura e uma revisada ativa de classe ``le``.
    O modelo e montado sem a conferencia do cadastro."""
    dados = copy.deepcopy(_MODELOS["ferramentas_de_teste"].guardada())
    mexer(dados)
    modelo = Declaracao.model_validate(dados)
    cadastro = cadastro_do_duble(revisadas=("revisada_velha",))
    ligacao, conectar = LigacaoQueAnota(), Conexoes()
    if motivo is None:
        alvo_do_teste(modelo, ligacao=ligacao, conectar=conectar, cadastro=cadastro)
        return
    with pytest.raises(ForaDoAlvo) as erro:
        alvo_do_teste(modelo, ligacao=ligacao, conectar=conectar, cadastro=cadastro)
    assert erro.value.motivo == {"teste_real": "ferramenta_de_teste_real", "le": "leitura_que_nao_le"}[motivo]
    _nada_saiu(ligacao, conectar)
    # Sem o cadastro, so os nomes da declaracao: o alvo e criado (a conferencia da declaracao barra antes).
    alvo_do_teste(modelo)


# --- O caminho permitido ----------------------------------------------------------------------------------------


async def test_o_que_a_lista_deixa_sai_pelo_alvo_com_a_chave_de_teste() -> None:
    alvo, ligacao, _ = alvo_do_teste()
    assert await alvo.conferir_marcas() == (True, True)
    k = alvo.numero_novo()
    acao = _MODELO.ferramentas["enviar_lembrete"]
    await alvo.chamar("alvo", "enviar_lembrete", acao.argumentos, numero=k)
    await alvo.chamar("alvo", "listar_lembretes", acao.rastro.argumentos)
    await alvo.chamar("alvo", contrato.FERRAMENTA_CONSULTAR, {"operacao": k})
    await alvo.chamar("alvo", contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": alvo.numero_novo(), "operacoes": [k]})
    conflito = _MODELO.ferramentas["trocar_perfil"].conflito
    assert conflito is not None
    await alvo.chamar(
        "alvo", "trocar_perfil", dict(conflito.argumentos) | {conflito.argumento_versao: 7}, numero=alvo.numero_novo()
    )
    enderecos = {e for e, *_ in ligacao.chamadas[2:]}
    assert enderecos == {ALVO}
    assert [n for _, _, _, n in ligacao.chamadas if n is not None] == [k, ligacao.chamadas[-1][3]]
    assert (alvo.chamadas, alvo.escritas) == (7, 2)


async def test_na_producao_so_a_lista_o_alvo_e_a_consulta_de_controle() -> None:
    """Critica A4.4: na producao, ``tools/list``, ``selo_alvo`` e uma consulta com um numero novo, que nunca foi numa
    escrita. Nada de ``selo_bloqueio``, de acao nem de leitura na producao."""
    alvo, ligacao, conectar = alvo_do_teste()
    await alvo.listar("producao")
    await alvo.conferir_marcas()
    novo = alvo.numero_novo()
    await alvo.chamar("producao", contrato.FERRAMENTA_CONSULTAR, {"operacao": novo})
    antes = len(ligacao.chamadas)
    usado = alvo.numero_novo()
    await alvo.chamar("alvo", "enviar_lembrete", _MODELO.ferramentas["enviar_lembrete"].argumentos, numero=usado)
    antes += 1
    for nome, argumentos, numero in [
        (contrato.FERRAMENTA_CONSULTAR, {"operacao": usado}, None),
        (contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": alvo.numero_novo(), "operacoes": [novo]}, None),
        ("enviar_lembrete", _MODELO.ferramentas["enviar_lembrete"].argumentos, alvo.numero_novo()),
        ("listar_lembretes", {}, None),
        (contrato.FERRAMENTA_ALVO, {"x": 1}, None),
    ]:
        with pytest.raises(ForaDoAlvo):
            await alvo.chamar("producao", nome, argumentos, numero=numero)
    assert len(ligacao.chamadas) == antes
    assert ligacao.listagens == [PRODUCAO]
    producao = [(n, a) for e, n, a, _ in ligacao.chamadas if e == PRODUCAO]
    assert producao == [
        (contrato.FERRAMENTA_ALVO, {}),
        (contrato.FERRAMENTA_CONSULTAR, {"operacao": novo}),
    ]
    assert not conectar.abertas


async def test_nivel_2_nao_avisa_bloqueio() -> None:
    alvo, ligacao, conectar = alvo_do_teste(nivel=2)
    await alvo.conferir_marcas()
    antes = len(ligacao.chamadas)
    with pytest.raises(ForaDoAlvo):
        await alvo.chamar("alvo", contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": alvo.numero_novo(), "operacoes": []})
    with pytest.raises(ForaDoAlvo):
        await alvo.chamar(
            "alvo", contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": alvo.numero_novo(), "operacoes": [alvo.numero_novo()]}
        )
    _nada_saiu(ligacao, conectar, antes)


# --- A marca positiva -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("diz", "esperado"),
    [
        ({PRODUCAO: "producao", ALVO: "producao"}, (True, False)),
        ({PRODUCAO: "teste", ALVO: "teste"}, (False, True)),
        ({PRODUCAO: "teste", ALVO: "producao"}, (False, False)),
    ],
    ids=["alvo_diz_producao", "producao_diz_teste", "trocados"],
)
async def test_sem_a_marca_positiva_nenhuma_escrita_sai(diz: dict[str, str], esperado: tuple[bool, bool]) -> None:
    """Critica A4.1: antes de ``conferir_marcas``, e com uma marca errada, nenhuma escrita sai (nem o corte)."""
    alvo, ligacao, conectar = alvo_do_teste(ligacao=LigacaoQueAnota(alvo_diz=diz))
    acao = _MODELO.ferramentas["enviar_lembrete"]
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete", acao.argumentos, numero=alvo.numero_novo())
    _nada_saiu(ligacao, conectar)
    assert await alvo.conferir_marcas() == esperado
    assert not alvo.pode_escrever
    antes = len(ligacao.chamadas)
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete", acao.argumentos, numero=alvo.numero_novo())
    with pytest.raises(SemMarca):
        await alvo.mandar_e_cortar("enviar_lembrete", acao.argumentos, alvo.numero_novo())
    assert len(ligacao.chamadas) == antes
    assert not conectar.abertas


SEM_RESPOSTA = Resposta("duvida", excecao="TimeoutError")


@pytest.mark.parametrize(
    ("lado_mudo", "resposta", "esperado"),
    [
        (PRODUCAO, SEM_RESPOSTA, (None, True)),
        (ALVO, SEM_RESPOSTA, (True, None)),
        (PRODUCAO, Resposta("erro_rpc"), (None, True)),
        (PRODUCAO, _resultado({"alvo": "producao"}), (None, True)),
        (ALVO, _resultado({"alvo": "teste"}), (True, None)),
        (ALVO, _resultado({"contrato": "selo/2", "alvo": "teste"}), (True, None)),
        (ALVO, _resultado({"contrato": contrato.CONTRATO, "alvo": "talvez"}), (True, None)),
        (ALVO, _resultado(None), (True, None)),
    ],
    ids=[
        "producao_sem_resposta",
        "alvo_sem_resposta",
        "producao_com_erro",
        "producao_sem_contrato",
        "alvo_sem_contrato",
        "alvo_com_outro_contrato",
        "alvo_fora_do_vocabulario",
        "alvo_sem_estruturado",
    ],
)
async def test_marca_que_nao_veio_no_contrato_nao_deixa_escrever(
    lado_mudo: str, resposta: Resposta, esperado: tuple[bool | None, bool | None]
) -> None:
    """Critica A4.1: so a marca dita no contrato (``{"contrato": "selo/1", "alvo": ...}``) vale. A producao (ou o
    alvo) que nao responde a ``selo_alvo``, responde erro, ou responde sem o contrato ou fora do vocabulario nao se
    declarou: a bateria nao escreve (nem o corte)."""
    alvo, ligacao, conectar = alvo_do_teste(ligacao=LigacaoQueAnota(quem_sou={lado_mudo: resposta}))
    assert await alvo.conferir_marcas() == esperado
    assert not alvo.pode_escrever
    assert (alvo.producao_disse, alvo.alvo_disse) == (
        None if lado_mudo == PRODUCAO else "producao",
        None if lado_mudo == ALVO else "teste",
    )
    antes = len(ligacao.chamadas)
    acao = _MODELO.ferramentas["enviar_lembrete"]
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete", acao.argumentos, numero=alvo.numero_novo())
    with pytest.raises(SemMarca):
        await alvo.mandar_e_cortar("enviar_lembrete", acao.argumentos, alvo.numero_novo())
    assert len(ligacao.chamadas) == antes
    assert not conectar.abertas


async def test_modo_de_ferramentas_de_teste_nunca_chama_a_acao_real() -> None:
    modelo = declaracao("ferramentas_de_teste")
    alvo, ligacao, conectar = alvo_do_teste(modelo)
    assert await alvo.conferir_marcas() == (True, True)
    assert [e for e, *_ in ligacao.chamadas] == [PRODUCAO]  # so a producao diz quem e: o alvo e ela
    antes = len(ligacao.chamadas)
    for acao in selo_nivel2.ACOES:
        e = modelo.ferramentas[acao]
        with pytest.raises(ForaDoAlvo) as erro:
            await alvo.chamar("alvo", acao, e.argumentos, numero=alvo.numero_novo())
        assert erro.value.motivo == "acao_real"
        with pytest.raises(ForaDoAlvo):
            await alvo.chamar("alvo", acao, e.rastro.argumentos)
        with pytest.raises(ForaDoAlvo):
            await alvo.mandar_e_cortar(acao, e.argumentos, alvo.numero_novo())
    assert len(ligacao.chamadas) == antes
    e = modelo.ferramentas["enviar_lembrete"]
    await alvo.chamar("alvo", "enviar_lembrete" + selo_nivel2.SUFIXO_TESTE, e.argumentos, numero=alvo.numero_novo())
    assert ligacao.chamadas[-1][:2] == (PRODUCAO, "enviar_lembrete_teste")
    assert alvo.marcas == [True]
    assert not conectar.abertas


async def test_primeira_resposta_de_teste_sem_a_marca_para_a_bateria() -> None:
    """No modo de ferramentas de teste, a bateria para na primeira ferramenta de teste que nao devolver a marca: a
    escrita seguinte nao sai."""
    modelo = declaracao("ferramentas_de_teste")
    alvo, ligacao, conectar = alvo_do_teste(modelo, ligacao=LigacaoQueAnota(marca=False))
    await alvo.conferir_marcas()
    e = modelo.ferramentas["enviar_lembrete"]
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete_teste", e.argumentos, numero=alvo.numero_novo())
    assert alvo.parou_sem_marca
    assert not alvo.pode_escrever
    antes = len(ligacao.chamadas)
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete_teste", e.argumentos, numero=alvo.numero_novo())
    with pytest.raises(SemMarca):
        await alvo.mandar_e_cortar("enviar_lembrete_teste", e.argumentos, alvo.numero_novo())
    assert len(ligacao.chamadas) == antes
    assert alvo.marcas == [False]
    assert not conectar.abertas


async def test_marca_que_some_depois_da_primeira_resposta_para_a_bateria() -> None:
    """Cada resposta de ferramenta de teste e conferida, nao so a primeira: a que vem sem a marca depois de uma com
    ela para a bateria, e a escrita seguinte nao sai."""
    modelo = declaracao("ferramentas_de_teste")
    alvo, ligacao, conectar = alvo_do_teste(modelo, ligacao=LigacaoQueAnota(marcas=[True, True, False]))
    await alvo.conferir_marcas()
    e = modelo.ferramentas["enviar_lembrete"]
    for _ in range(2):
        await alvo.chamar("alvo", "enviar_lembrete_teste", e.argumentos, numero=alvo.numero_novo())
    assert alvo.pode_escrever
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete_teste", e.argumentos, numero=alvo.numero_novo())
    assert (alvo.marcas, alvo.parou_sem_marca, alvo.pode_escrever) == ([True, True, False], True, False)
    antes = len(ligacao.chamadas)
    with pytest.raises(SemMarca):
        await alvo.chamar("alvo", "enviar_lembrete_teste", e.argumentos, numero=alvo.numero_novo())
    assert len(ligacao.chamadas) == antes
    assert not conectar.abertas


# --- O endereco -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alvo",
    [
        "https://COBRANCA.exemplo.teste/mcp",
        "https://cobranca.exemplo.teste:443/mcp",
        "https://cobranca.exemplo.teste/mcp/",
        "https://cobranca.exemplo.teste/MCP",
        "http://cobranca.exemplo.teste:443/mcp",
        "https://cobranca.exemplo.teste/x/../mcp",
    ],
)
async def test_alvo_que_e_a_producao_escrita_de_outro_jeito_e_recusado_antes_da_rede(alvo: str) -> None:
    """Critica A4.1: o alvo e comparado com a producao (do sistema e de todo o cadastro) na forma normalizada e na de
    conexao, antes de abrir qualquer conexao. O modelo da declaracao e montado sem a conferencia do cadastro (como a
    bateria a veria se o banco e a tela falhassem)."""
    dados = selo_nivel2.declaracao(endereco_teste=alvo)
    modelo = Declaracao.model_validate(dados)
    ligacao, conectar = LigacaoQueAnota(), Conexoes()
    with pytest.raises(ForaDoAlvo) as erro:
        alvo_do_teste(modelo, ligacao=ligacao, conectar=conectar)
    assert erro.value.motivo == "alvo_na_producao"
    _nada_saiu(ligacao, conectar)


async def test_alvo_que_e_a_producao_de_outro_sistema_do_cadastro_e_recusado() -> None:
    ligacao, conectar = LigacaoQueAnota(), Conexoes()
    with pytest.raises(ForaDoAlvo):
        alvo_do_teste(ligacao=ligacao, conectar=conectar, producoes=("https://outro.exemplo.teste/x", ALVO.upper()))
    _nada_saiu(ligacao, conectar)


# --- O teto -----------------------------------------------------------------------------------------------------


async def test_teto_de_chamadas_vale_antes_de_abrir_conexao() -> None:
    alvo, ligacao, conectar = alvo_do_teste(teto=3)
    await alvo.conferir_marcas()  # 2 chamadas
    await alvo.listar("alvo")  # 3
    acao = _MODELO.ferramentas["enviar_lembrete"]
    with pytest.raises(TetoDeChamadas):
        await alvo.chamar("alvo", "enviar_lembrete", acao.argumentos, numero=alvo.numero_novo())
    with pytest.raises(TetoDeChamadas):
        await alvo.listar("producao")
    with pytest.raises(TetoDeChamadas):
        await alvo.mandar_e_cortar("enviar_lembrete", acao.argumentos, alvo.numero_novo())
    assert alvo.chamadas == 3
    assert len(ligacao.chamadas) == 2
    assert ligacao.listagens == [ALVO]
    assert not conectar.abertas


# --- O corte (C-2.6a) -------------------------------------------------------------------------------------------


async def test_o_corte_manda_o_pedido_inteiro_e_fecha_sem_ler() -> None:
    """Critica A3.1: o corte abre a conexao com o alvo (TLS no https), manda o ``tools/call`` inteiro, com o tamanho
    do corpo, a chave de teste e o numero so no ``_meta``, e fecha logo depois, sem ler nada."""
    alvo, ligacao, conectar = alvo_do_teste()
    await alvo.conferir_marcas()
    k = alvo.numero_novo()
    acao = _MODELO.ferramentas["enviar_lembrete"]
    assert await alvo.mandar_e_cortar("enviar_lembrete", acao.argumentos, k)
    assert conectar.abertas == [("teste.cobranca.exemplo.teste", 443, True, "teste.cobranca.exemplo.teste")]
    [fluxo] = conectar.fluxos
    assert fluxo.fechado
    cabeca, _, corpo = bytes(fluxo.enviado).partition(b"\r\n\r\n")
    linhas = cabeca.decode().split("\r\n")
    assert linhas[0] == "POST /mcp HTTP/1.1"
    cabecalhos = dict(linha.split(": ", 1) for linha in linhas[1:])
    assert cabecalhos["content-length"] == str(len(corpo))
    assert cabecalhos["authorization"] == "Bearer chave-teste"
    pedido = json.loads(corpo)
    assert pedido["method"] == "tools/call"
    assert pedido["params"]["arguments"] == acao.argumentos
    assert pedido["params"]["_meta"][contrato.CHAVE_OPERACAO] == k
    assert k not in json.dumps(pedido["params"]["arguments"])
    assert (alvo.chamadas, alvo.escritas) == (3, 1)
    assert len(ligacao.chamadas) == 2  # so as duas marcas


@pytest.mark.parametrize(
    "endereco",
    [
        "https://teste.cobranca.exemplo.teste/mcp/ação",
        "https://teste.cobranca.exemplo.teste/mcp/€",
        "http://127.0.0.1:8080/mcp/%7Ex/ç",
        "https://[::1]:8443/mcp",
        "https://teste.cobranca.exemplo.teste:443/mcp",
    ],
    ids=["acento", "fora_do_latin1", "porta_e_ja_escrito", "ipv6", "porta_padrao_escrita"],
)
async def test_o_corte_vai_ao_mesmo_caminho_e_host_que_o_cliente(endereco: str) -> None:
    """O corte do C-2.6a manda o pedido ao mesmo alvo que o cliente fixado (``httpx2``): o caminho com o que esta fora
    do ASCII escrito em ``%XX``, o mesmo cabecalho Host e o mesmo nome e porta na conexao. Um caminho fora do latin-1
    nao derruba o corte (a condicao seria dada como nao forcada para um sistema honesto)."""
    modelo = Declaracao.model_validate(selo_nivel2.declaracao(endereco_teste=endereco))
    ligacao = LigacaoQueAnota(alvo_diz={PRODUCAO: "producao", endereco: "teste"})
    alvo, _, conectar = alvo_do_teste(modelo, ligacao=ligacao)
    assert await alvo.conferir_marcas() == (True, True)
    acao = _MODELO.ferramentas["enviar_lembrete"]
    assert await alvo.mandar_e_cortar("enviar_lembrete", acao.argumentos, alvo.numero_novo())
    do_cliente = httpx2.Request("POST", endereco)
    [(nome, porta, com_tls, nome_tls)] = conectar.abertas
    assert (nome, porta, com_tls, nome_tls) == (
        do_cliente.url.raw_host.decode(),
        do_cliente.url.port or (443 if do_cliente.url.scheme == "https" else 80),
        do_cliente.url.scheme == "https",
        do_cliente.url.raw_host.decode(),
    )
    cabeca = bytes(conectar.fluxos[0].enviado).partition(b"\r\n\r\n")[0].decode("ascii").split("\r\n")
    assert cabeca[0] == f"POST {do_cliente.url.raw_path.decode()} HTTP/1.1"
    assert dict(linha.split(": ", 1) for linha in cabeca[1:])["host"] == do_cliente.headers["host"]


async def test_o_corte_que_nao_abre_a_conexao_so_diz_que_nao_saiu() -> None:
    class SemRede(Conexoes):
        async def __call__(self, nome: str, porta: int, tls: ssl.SSLContext | None, nome_tls: str) -> Fluxo:
            raise OSError("sem rede")

    alvo, _, _ = alvo_do_teste(conectar=SemRede())
    await alvo.conferir_marcas()
    acao = _MODELO.ferramentas["enviar_lembrete"]
    assert await alvo.mandar_e_cortar("enviar_lembrete", acao.argumentos, alvo.numero_novo()) is False
