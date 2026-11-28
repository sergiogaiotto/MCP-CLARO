"""Os casos da bateria do selo (``selo/casos.py``) e o plano (``selo/bateria.py``), sem rede.

- ``Juiz``: os veredictos e as razoes fechadas do contrato, pela decisao 7 do coordenador na etapa 3r (parte que o
  sistema nao cumpriu: ``falhou``; sem resposta: ``nao_testado`` com ``sem_resposta``; condicao nao forcada:
  ``nao_testado`` com ``condicao_nao_forcada``; tentativa com prazo esgotado: excluida e contada a parte); os itens
  tem so os campos do contrato e passam em ``contrato.problema_nos_casos``;
- o que a bateria le das respostas: o recibo so pelo ``selo.respostas`` (os identificadores so depois de validado), as
  partes com tres valores, o ponteiro do rastro;
- os casos contra um sistema na memoria (``SistemaNaMemoria``, pelo ``AlvoDoTeste`` de verdade), nas condicoes que a
  bateria nao forca sozinha (decisao 7): a escrita lenta que termina antes da resposta da consulta (C-2.6b), o corte
  que nao sai (C-2.6a), o aviso que volta depois dos segundos do efeito adiado (C-3.6b); o prazo esgotado que nao
  esconde a falha que o rastro prova (C-2.2d, C-2.2a, C-2.5); o C-0.4 sem nenhuma resposta de ferramenta de teste;
- o plano (critica B5): as chamadas e as escritas pela declaracao; sem as dicas de idempotencia, o maximo.
"""

from __future__ import annotations

import contextlib
import json
import ssl
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import mcp_types as types
import pytest

from exemplos import selo_nivel2
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.selo import contrato
from mcp_claro.selo.alvo import AlvoDoTeste, SemMarca
from mcp_claro.selo.bateria import MOTIVOS_LEGIVEIS, Plano, casos_que_reprovam, frase_do_teto, plano, rodar_bateria
from mcp_claro.selo.casos import (
    Casos,
    Juiz,
    contagem_do_rastro,
    espera_fixa,
    ponteiro,
    recibo_de,
    recusada,
    sucesso,
    tudo,
)
from mcp_claro.selo.declaracao import Declaracao, validar

pytestmark = pytest.mark.anyio

F1 = str(uuid.uuid4())


def juiz(nivel: int = 3) -> Juiz:
    return Juiz({"acao": F1}, nivel)


# --- O juiz -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("partes", "kw", "esperado"),
    [
        ({"a": True, "b": True}, {}, ("cumpriu", None, 1, 0)),
        ({"a": True, "b": False}, {}, ("falhou", None, 1, 0)),
        ({"a": None, "b": True}, {}, ("nao_testado", "sem_resposta", 1, 0)),
        ({"a": None, "b": False}, {}, ("falhou", None, 1, 0)),
        ({"a": True}, {"nao_forcada": True}, ("nao_testado", "condicao_nao_forcada", 1, 0)),
        ({"a": None}, {"nao_forcada": True}, ("nao_testado", "condicao_nao_forcada", 1, 0)),
        ({"a": False}, {"nao_forcada": True}, ("falhou", None, 1, 0)),
        ({"a": False}, {"excluida": True}, ("nao_testado", "tentativas_excluidas", 1, 1)),
        ({"a": True}, {"sem_falha": "nao_observado"}, ("nao_observado", None, 1, 0)),
        ({"a": True}, {"sem_falha": "nao_testado", "razao": "poucos_pares"}, ("nao_testado", "poucos_pares", 1, 0)),
        ({"a": False, "b": True}, {"excluida": True, "provadas": ("a",)}, ("falhou", None, 1, 0)),
        (
            {"a": True, "b": False},
            {"excluida": True, "provadas": ("a",)},
            ("nao_testado", "tentativas_excluidas", 1, 1),
        ),
        ({"a": None, "b": True}, {"excluida": True, "provadas": ("a",)}, ("nao_testado", "tentativas_excluidas", 1, 1)),
    ],
    ids=[
        "cumpriu",
        "uma_parte_falsa",
        "sem_resposta",
        "falsa_vence_sem_resposta",
        "nao_forcada",
        "nao_forcada_vence_sem_resposta",
        "falsa_vence_nao_forcada",
        "prazo_esgotado_e_excluido",
        "maximo_do_metodo",
        "poucos_pares",
        "prova_vence_prazo_esgotado",
        "falsa_fora_da_prova_e_excluida",
        "prova_sem_resposta_e_excluida",
    ],
)
def test_juiz_pela_decisao_7(partes: dict[str, bool | None], kw: dict[str, Any], esperado: tuple[Any, ...]) -> None:
    j = juiz()
    j.julgar("C-2.1", "acao", partes, **kw)
    [item] = j.itens
    assert set(item) == contrato.CAMPOS_DO_ITEM
    assert (item["resultado"], item["razao"], item["tentativas"], item["excluidas"]) == esperado
    assert item["ferramenta"] == F1
    assert j.partes[("C-2.1", "acao")] == partes


def test_todas_as_partes_sao_avaliadas_e_guardadas() -> None:
    """Sem curto-circuito: a primeira parte falsa nao esconde as outras (o relatorio dos testes mostra cada uma)."""
    j = juiz()
    j.julgar("C-2.5", "acao", {"nao_recebida": False, "recusada": True, "rastro": None})
    assert j.partes[("C-2.5", "acao")] == {"nao_recebida": False, "recusada": True, "rastro": None}


def test_caso_julgado_duas_vezes_e_erro_da_bateria() -> None:
    j = juiz()
    j.julgar("C-2.1", "acao", {"a": True})
    with pytest.raises(ValueError, match="duas vezes"):
        j.item("C-2.1", "acao", "falhou", tentativas=1)


@pytest.mark.parametrize("nivel", [2, 3])
def test_completar_poe_o_que_falta_como_nao_testado_e_os_itens_valem_no_banco(nivel: int) -> None:
    j = juiz(nivel)
    j.julgar("C-0.1", None, {"x": True})
    j.julgar("C-2.1", "acao", {"x": True})
    j.completar("fora_do_alvo")
    casos = {(i["caso"], i["ferramenta"]) for i in j.itens}
    esperado = {(c.caso, None if c.escopo == "sistema" else F1) for c in contrato.CASOS if c.nivel <= nivel}
    assert casos == esperado
    assert len(j.itens) == len(esperado)
    resto = [i for i in j.itens if i["caso"] not in ("C-0.1", "C-2.1")]
    assert {(i["resultado"], i["razao"], i["tentativas"]) for i in resto} == {("nao_testado", "fora_do_alvo", 0)}
    assert contrato.problema_nos_casos(j.itens, [F1]) is None
    assert not contrato.casos_aprovam(j.itens, [F1], nivel)
    assert (
        casos_que_reprovam(j.itens, nivel) == sum(1 for c in contrato.CASOS if c.obrigatorio and c.nivel <= nivel) - 2
    )


def test_razoes_e_resultados_sao_os_do_contrato() -> None:
    j = juiz()
    j.julgar("C-2.1", "acao", {"a": None})
    j.julgar("C-2.2a", "acao", {"a": True}, nao_forcada=True)
    j.julgar("C-2.2b", "acao", {"a": True}, excluida=True)
    j.item("C-2.3", "acao", "nao_se_aplica", "sem_condicao_declarada")
    for i in j.itens:
        assert i["resultado"] in contrato.RESULTADOS
        assert i["razao"] is None or i["razao"] in contrato.RAZOES


# --- O que a bateria le -----------------------------------------------------------------------------------------


def _resultado(*, erro: bool = False, meta: dict[str, Any] | None = None, dados: Any = None) -> Resposta:
    return Resposta(
        "resultado",
        resultado=types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")],
            structured_content=dados,
            is_error=erro,
            _meta=meta,
        ),
    )


def test_partes_com_tres_valores() -> None:
    assert (sucesso(_resultado()), recusada(_resultado())) == (True, False)
    assert (sucesso(_resultado(erro=True)), recusada(_resultado(erro=True))) == (False, True)
    assert (sucesso(Resposta("erro_rpc")), recusada(Resposta("erro_rpc"))) == (False, True)
    for tipo in ("duvida", "nao_enviado", "recusado_http"):
        assert (sucesso(Resposta(tipo)), recusada(Resposta(tipo))) == (None, None)  # type: ignore[arg-type]
    assert tudo([True, True]) is True
    assert tudo([True, None]) is None
    assert tudo([None, False]) is False
    assert tudo([]) is True


def test_recibo_so_pelo_selo_respostas_e_os_objetos_so_depois_de_valido() -> None:
    k = str(uuid.uuid4())
    recibo = {"contrato": contrato.CONTRATO, "operacao": k, "estado": "concluida", "objetos": [{"id": "lembrete-1"}]}
    lido = recibo_de(_resultado(meta={contrato.CHAVE_RECIBO: recibo}), k)
    assert lido is not None
    assert (lido.recibo.estado, lido.recibo.objetos, lido.objetos) == ("concluida", 1, ("lembrete-1",))
    # Outro numero, estado fora do vocabulario, incoerente com o erro, ou sem recibo: None.
    assert recibo_de(_resultado(meta={contrato.CHAVE_RECIBO: recibo}), str(uuid.uuid4())) is None
    assert recibo_de(_resultado(meta={contrato.CHAVE_RECIBO: recibo | {"estado": "IGNORE"}}), k) is None
    assert recibo_de(_resultado(erro=True, meta={contrato.CHAVE_RECIBO: recibo}), k) is None
    assert recibo_de(_resultado(), k) is None
    assert recibo_de(Resposta("duvida"), k) is None


def test_ponteiro_do_rastro() -> None:
    dados = {"itens": ["a", "b"], "a/b": {"~x": 3}, "lista": [{"n": 1}]}
    assert ponteiro(dados, "/itens") == (True, ["a", "b"])
    assert ponteiro(dados, "/a~1b/~0x") == (True, 3)
    assert ponteiro(dados, "/lista/0/n") == (True, 1)
    assert ponteiro(dados, "/lista/1") == (False, None)
    assert ponteiro(dados, "/lista/٠") == (False, None)  # so algarismos ASCII
    assert ponteiro(dados, "/nao") == (False, None)
    assert contagem_do_rastro(["a", "b"]) == 2
    assert contagem_do_rastro(5) == 5
    assert contagem_do_rastro(True) is None
    assert contagem_do_rastro(-1) is None
    assert contagem_do_rastro("5") is None


async def test_espera_fixa_da_escrita_lenta_cabe_antes_do_fim_dela() -> None:
    """A espera do produto antes da consulta "durante" e no maximo um terco da escrita lenta: a consulta vai antes de
    a escrita terminar."""
    chegada = espera_fixa(10.0)
    inicio = anyio.current_time()
    async with chegada("acao", 0.3):
        pass
    assert anyio.current_time() - inicio < 0.3


# --- Os casos contra um sistema na memoria ------------------------------------------------------------------------

PRODUCAO = "https://cobranca.exemplo.teste/mcp"
ALVO = "https://teste.cobranca.exemplo.teste/mcp"
RECUSADO = "nao-e-um-endereco"
"""O ``para`` dos ``argumentos_recusados`` da declaracao do duble de nivel 2."""


def _feito(dados: Any, *, erro: bool = False, meta: dict[str, Any] | None = None) -> Resposta:
    return Resposta(
        "resultado",
        resultado=types.CallToolResult(
            content=[types.TextContent(type="text", text="ok")], structured_content=dados, is_error=erro, _meta=meta
        ),
    )


def _pede_dados() -> Resposta:
    return Resposta(
        "resultado",
        resultado=types.InputRequiredResult.model_validate(
            {
                "resultType": "input_required",
                "inputRequests": {
                    "confirmar": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Confirma?",
                            "requestedSchema": {"type": "object", "properties": {"sim": {"type": "boolean"}}},
                        },
                    }
                },
                "requestState": "confirmar",
            }
        ),
    )


@dataclass
class SistemaNaMemoria:
    """Um sistema na memoria atras do ``AlvoDoTeste`` (e a ligacao dele). Honesto: registra o numero ao chegar,
    aplica uma vez por numero, recusa outros argumentos com um numero usado, queima o numero consultado ou avisado
    antes de chegar (e recusa a chamada depois), para o efeito adiado avisado a tempo. O que muda isso: ``duplica``
    (aplica toda chamada, ate a repetida), ``aplica_queimado`` (aplica o numero queimado), ``sem_resposta_se`` (a
    escrita aplica e volta sem resposta, como um prazo esgotado), ``demora`` (o que a escrita lenta leva),
    ``consulta_diz`` (o estado de toda consulta) e ``atraso_do_aviso``."""

    nivel: int = 2
    duplica: bool = False
    aplica_queimado: bool = False
    sem_resposta_se: Callable[[SistemaNaMemoria, str], bool] | None = None
    demora: float = 0.0
    consulta_diz: str | None = None
    atraso_do_aviso: float = 0.0
    rastro: int = 0
    escritas: int = 0
    argumentos: dict[str, str] = field(default_factory=dict)
    aplicados: set[str] = field(default_factory=set)
    em_curso: set[str] = field(default_factory=set)
    queimados: set[str] = field(default_factory=set)
    recusados: set[str] = field(default_factory=set)
    adiados: set[str] = field(default_factory=set)
    parados: set[str] = field(default_factory=set)

    def _estado(self, numero: str) -> str:
        for estados, estado in (
            (self.em_curso, "em_andamento"),
            (self.parados, "parada"),
            (self.adiados, "em_andamento"),
            (self.aplicados, "concluida"),
            (self.recusados, "recusada"),
        ):
            if numero in estados:
                return estado
        self.queimados.add(numero)
        return "nao_recebida"

    def _recibo(self, numero: str, estado: str) -> dict[str, Any] | None:
        if self.nivel < 3:
            return None
        return {contrato.CHAVE_RECIBO: {"contrato": contrato.CONTRATO, "operacao": numero, "estado": estado}}

    async def chamar_ferramenta(
        self,
        endereco: str,
        segredo: Any,
        discover_json: str | None,
        nome: str,
        argumentos: dict[str, Any],
        *,
        chave_operacao: str | None = None,
        **_: Any,
    ) -> Resposta:
        if nome == contrato.FERRAMENTA_ALVO:
            return _feito({"contrato": contrato.CONTRATO, "alvo": "producao" if endereco == PRODUCAO else "teste"})
        if nome == contrato.FERRAMENTA_CONSULTAR:
            numero = argumentos["operacao"]
            dados = {
                "contrato": contrato.CONTRATO,
                "operacao": numero,
                "estado": self.consulta_diz or self._estado(numero),
            }
            return _feito(dados | ({"depois": []} if self.nivel >= 3 else {}))
        if nome == contrato.FERRAMENTA_BLOQUEIO:
            await anyio.sleep(self.atraso_do_aviso)
            self.parados |= self.adiados & set(argumentos["operacoes"])
            itens = [{"operacao": n, "estado": self._estado(n)} for n in argumentos["operacoes"]]
            return _feito({"contrato": contrato.CONTRATO, "bloqueio": argumentos["bloqueio"], "operacoes": itens})
        if nome.startswith("listar"):
            return _feito({"itens": list(range(self.rastro)), "aplicacoes": self.rastro, "lidos": []})
        assert chave_operacao is not None
        self.escritas += 1
        resposta = await self._escrever(chave_operacao, argumentos)
        if self.sem_resposta_se is not None and self.sem_resposta_se(self, chave_operacao):
            return Resposta("duvida", excecao="TimeoutError")
        return resposta

    async def _escrever(self, numero: str, argumentos: dict[str, Any]) -> Resposta:
        texto = json.dumps(argumentos, sort_keys=True)
        queimado = numero in self.queimados and not self.aplica_queimado
        outro = numero in self.argumentos and self.argumentos[numero] != texto
        if queimado or outro or argumentos.get("para") == RECUSADO:
            if numero not in self.argumentos:
                self.recusados.add(numero)
            return _feito(None, erro=True, meta=self._recibo(numero, "recusada"))
        if argumentos.get("pedir_confirmacao"):
            return _pede_dados()
        repetida = numero in self.argumentos
        self.argumentos[numero] = texto
        if "daqui_a" in argumentos:
            self.adiados.add(numero)
            return _feito({"ok": True}, meta=self._recibo(numero, "em_andamento"))
        if "demora" in argumentos:
            self.em_curso.add(numero)
            await anyio.sleep(self.demora)
            self.em_curso.discard(numero)
        if not repetida or self.duplica:
            self.rastro += 1
        self.aplicados.add(numero)
        return _feito({"ok": True}, meta=self._recibo(numero, "concluida"))

    async def listar_ferramentas(self, *_: Any) -> list[dict[str, Any]]:
        return []


class SemRede:
    """O corte que nao abre a conexao (C-2.6a): a condicao nao e forcada."""

    async def __call__(self, nome: str, porta: int, tls: ssl.SSLContext | None, nome_tls: str) -> Any:
        raise OSError("sem rede")


class Cortes:
    """O corte que abre a conexao e manda (o sistema na memoria nao o recebe: e outra conexao)."""

    @dataclass
    class Fluxo:
        enviado: bytearray = field(default_factory=bytearray)

        async def send(self, item: bytes) -> None:
            self.enviado.extend(item)

        async def aclose(self) -> None:
            return None

    async def __call__(self, nome: str, porta: int, tls: ssl.SSLContext | None, nome_tls: str) -> Any:
        return Cortes.Fluxo()


def _chegada(espera: float = 0.0) -> Any:
    @contextlib.asynccontextmanager
    async def chegada(_ferramenta: str, _lenta: float | None) -> AsyncIterator[None]:
        yield
        await anyio.sleep(espera)

    return chegada


def _modelo_do_duble(modo: str = "ambiente_de_teste", *, segundos: int = 1) -> Declaracao:
    endereco = ALVO if modo == "ambiente_de_teste" else None
    return validar(
        selo_nivel2.declaracao(
            endereco_teste=endereco,
            modo=modo,  # type: ignore[arg-type]
            acoes=("enviar_lembrete",),
            segundos=segundos,
        )
    )


async def _casos(
    sistema: SistemaNaMemoria,
    *,
    modo: str = "ambiente_de_teste",
    conectar: Any = None,
    espera: float = 0.0,
    pares: int = 0,
    segundos: int = 1,
) -> tuple[Casos, Juiz]:
    """Os casos de ``enviar_lembrete`` contra o sistema na memoria, pelo ``AlvoDoTeste`` de verdade, com a marca ja
    conferida."""
    modelo = _modelo_do_duble(modo, segundos=segundos)
    alvo = AlvoDoTeste(
        modelo,
        producao=PRODUCAO,
        producoes=(),
        nivel=sistema.nivel,
        ligacao=sistema,  # type: ignore[arg-type]
        segredo_teste="chave-teste",
        segredo_producao="chave-declaracoes",
        teto=5000,
        conectar=conectar or Cortes(),
    )
    j = Juiz({"enviar_lembrete": F1}, sistema.nivel)
    casos = Casos(alvo, modelo, j, pares=pares, chegada=_chegada(espera), intervalo_do_corte=0.0, folga_do_efeito=0.0)
    casos.marcas = await alvo.conferir_marcas()
    assert alvo.pode_escrever
    return casos, j


def _item(j: Juiz, caso: str) -> tuple[str, str | None]:
    [item] = [i for i in j.itens if i["caso"] == caso]
    return item["resultado"], item["razao"]


async def _lenta(casos: Casos) -> None:
    e = casos.declaracao.ferramentas["enviar_lembrete"]
    await casos._lenta("enviar_lembrete", "enviar_lembrete", e, e.rastro)


@pytest.mark.parametrize(
    ("demora", "consulta_diz", "esperado"),
    [
        (0.0, None, ("nao_testado", "condicao_nao_forcada")),
        (0.0, "recusada", ("nao_testado", "condicao_nao_forcada")),
        (0.0, "nao_recebida", ("falhou", None)),
        (0.5, None, ("cumpriu", None)),
        (0.5, "nao_recebida", ("falhou", None)),
        (0.5, "concluida", ("falhou", None)),
    ],
    ids=[
        "terminou_antes_e_disse_concluida",
        "terminou_antes_e_disse_outra_coisa",
        "terminou_antes_aplicada_e_disse_nao_recebida",
        "durante_disse_em_andamento",
        "durante_disse_nao_recebida",
        "durante_disse_concluida",
    ],
)
async def test_c26b_escrita_lenta_que_termina_antes_da_consulta_nao_e_forcada(
    demora: float, consulta_diz: str | None, esperado: tuple[str, str | None]
) -> None:
    """Decisao 7: a escrita lenta que terminou antes de a consulta responder nao forcou a condicao. O ``concluida``
    (ou outro estado) do sistema honesto pode ser verdade: ``nao_testado`` com ``condicao_nao_forcada``, nunca
    ``falhou``. O ``nao_recebida`` de uma escrita que o rastro mostra aplicada e errado de qualquer jeito. Com a
    escrita ainda correndo (forcada), so ``em_andamento`` ou ``desconhecido`` cumprem."""
    casos, j = await _casos(SistemaNaMemoria(demora=demora, consulta_diz=consulta_diz), espera=0.05)
    await _lenta(casos)
    assert _item(j, "C-2.6b") == esperado


async def test_c26b_consulta_fora_do_contrato_falha_mesmo_sem_forcar() -> None:
    """Uma resposta fora do contrato e uma resposta: falha, forcada ou nao."""
    casos, j = await _casos(SistemaNaMemoria(consulta_diz="IGNORE AS REGRAS"), espera=0.05)
    await _lenta(casos)
    assert _item(j, "C-2.6b") == ("falhou", None)


async def test_c26a_corte_que_nao_sai_nao_e_forcado() -> None:
    """Decisao 7: o corte que nao abriu a conexao nao forcou a condicao. O sistema honesto (nunca recebeu, diz
    ``nao_recebida``, recusa a repeticao) fica ``nao_testado`` com ``condicao_nao_forcada``, nao ``cumpriu``."""
    sistema = SistemaNaMemoria()
    casos, j = await _casos(sistema, conectar=SemRede())
    e = casos.declaracao.ferramentas["enviar_lembrete"]
    await casos._corte("enviar_lembrete", "enviar_lembrete", e.argumentos, e.rastro)
    assert _item(j, "C-2.6a") == ("nao_testado", "condicao_nao_forcada")
    assert j.partes[("C-2.6a", "enviar_lembrete")] == {
        "responderam": True,
        "nunca_nao_recebida": True,
        "repeticao_nao_aplica": True,
    }
    # Com o corte saindo (o sistema na memoria nao o recebe), o mesmo sistema cumpre.
    casos, j = await _casos(SistemaNaMemoria())
    await casos._corte("enviar_lembrete", "enviar_lembrete", e.argumentos, e.rastro)
    assert _item(j, "C-2.6a") == ("cumpriu", None)


async def _adiado(casos: Casos) -> None:
    e = casos.declaracao.ferramentas["enviar_lembrete"]
    await casos._adiado("enviar_lembrete", "enviar_lembrete", e, e.rastro)


async def test_c36b_aviso_que_volta_depois_dos_segundos_nao_e_forcado() -> None:
    """Decisao 7: o aviso que so voltou depois dos segundos declarados do efeito adiado pode ter chegado depois do
    efeito: ``nao_testado`` com ``condicao_nao_forcada``, e as partes que dependiam dele nao sao julgadas."""
    casos, j = await _casos(SistemaNaMemoria(nivel=3, atraso_do_aviso=1.2))
    await _adiado(casos)
    assert _item(j, "C-3.6b") == ("nao_testado", "condicao_nao_forcada")
    assert j.partes[("C-3.6b", "enviar_lembrete")] == {
        "recibo_em_andamento": True,
        "aviso_parada": None,
        "rastro": None,
        "consulta_parada": None,
    }
    # O aviso a tempo forca a condicao: o mesmo sistema cumpre.
    casos, j = await _casos(SistemaNaMemoria(nivel=3))
    await _adiado(casos)
    assert _item(j, "C-3.6b") == ("cumpriu", None)


def _a_terceira(sistema: SistemaNaMemoria, _numero: str) -> bool:
    return sistema.escritas == 3


async def _pares(casos: Casos) -> None:
    e = casos.declaracao.ferramentas["enviar_lembrete"]
    await casos._pares("enviar_lembrete", "enviar_lembrete", e.argumentos, e.rastro)


@pytest.mark.parametrize(
    ("duplica", "esperado"),
    [(True, ("falhou", None)), (False, ("nao_testado", "tentativas_excluidas"))],
    ids=["dobrou", "nao_dobrou"],
)
async def test_c22d_dobra_que_o_rastro_prova_vence_o_prazo_esgotado(
    duplica: bool, esperado: tuple[str, str | None]
) -> None:
    """Critica (desenho 3.3: o C-2.2d so da ``falhou`` ou ``nao_observado``): com uma chamada dos pares sem resposta no
    prazo, o rastro que subiu mais que os pares prova a dobra, e o caso falha. Sem a subida, a duvida exclui."""
    casos, j = await _casos(
        SistemaNaMemoria(duplica=duplica, sem_resposta_se=_a_terceira), pares=contrato.PARES_MINIMOS
    )
    await _pares(casos)
    assert _item(j, "C-2.2d") == esperado
    [item] = [i for i in j.itens if i["caso"] == "C-2.2d"]
    assert (item["tentativas"], item["excluidas"]) == (1, 0 if duplica else 1)


def _repetida(sistema: SistemaNaMemoria, numero: str) -> bool:
    return numero in sistema.aplicados and sistema.escritas == 2


def _queimada(sistema: SistemaNaMemoria, numero: str) -> bool:
    return numero in sistema.queimados and numero in sistema.aplicados


@pytest.mark.parametrize(
    ("sistema", "caso"),
    [
        (lambda: SistemaNaMemoria(duplica=True, sem_resposta_se=_repetida), "C-2.2a"),
        (lambda: SistemaNaMemoria(aplica_queimado=True, sem_resposta_se=_queimada), "C-2.5"),
    ],
    ids=["repeticao_que_aplica", "queimado_que_aplica"],
)
async def test_rastro_que_prova_a_falha_vence_o_prazo_esgotado(
    sistema: Callable[[], SistemaNaMemoria], caso: str
) -> None:
    """A escrita sem resposta no prazo que o rastro mostra aplicada de novo (a repeticao do C-2.2a) ou aplicada com o
    numero queimado (C-2.5): a subida nao vem de uma escrita que ainda nao terminou, e o caso falha."""
    s = sistema()
    casos, j = await _casos(s)
    await casos.ferramenta("enviar_lembrete")
    assert _item(j, caso) == ("falhou", None)
    assert j.partes[(caso, "enviar_lembrete")]["rastro"] is False


async def test_sistema_na_memoria_honesto_cumpre_o_nivel_2() -> None:
    """O sistema na memoria honesto passa nos casos de ferramenta do nivel 2 (o arranjo dos testes acima e certo)."""
    casos, j = await _casos(SistemaNaMemoria(demora=0.5), espera=0.05, pares=2)
    await casos.ferramenta("enviar_lembrete")
    assert {i["caso"]: (i["resultado"], i["razao"]) for i in j.itens} == {
        "C-2.1": ("cumpriu", None),
        "C-2.2a": ("cumpriu", None),
        "C-2.2b": ("cumpriu", None),
        "C-2.2c": ("cumpriu", None),
        "C-2.2d": ("nao_testado", "poucos_pares"),
        "C-2.3": ("nao_observado", None),
        "C-2.4a": ("cumpriu", None),
        "C-2.4b": ("cumpriu", None),
        "C-2.5": ("cumpriu", None),
        "C-2.6a": ("cumpriu", None),
        "C-2.6b": ("cumpriu", None),
        "C-2.7": ("nao_se_aplica", "sem_condicao_declarada"),
    }


@pytest.mark.parametrize(
    ("parou", "esperado"),
    [(None, ("nao_testado", "sem_resposta")), ("teto", None)],
    ids=["sem_resposta", "parou_antes"],
)
async def test_c04_sem_nenhuma_resposta_de_ferramenta_de_teste_nao_falha(
    parou: str | None, esperado: tuple[str, str | None] | None
) -> None:
    """Decisao 7: no modo de ferramentas de teste, sem nenhuma resposta de ferramenta de teste (todas sem resposta), a
    marca nao foi vista nem negada: o C-0.4 fica ``nao_testado`` com ``sem_resposta``, nunca ``falhou``. Se a bateria
    parou antes (o teto), o caso fica para ``Juiz.completar``, com a razao da parada."""
    sistema = SistemaNaMemoria(sem_resposta_se=lambda *_: True)
    casos, j = await _casos(sistema, modo="ferramentas_de_teste")
    e = casos.declaracao.ferramentas["enviar_lembrete"]
    await casos._acao("enviar_lembrete_teste", e.argumentos, casos.alvo.numero_novo())
    assert casos.alvo.marcas == []
    casos.alvos(parou)
    if esperado is None:
        assert not j.feito("C-0.4", None)
        j.completar("teto")
        assert _item(j, "C-0.4") == ("nao_testado", "teto")
    else:
        assert _item(j, "C-0.4") == esperado


async def test_bateria_que_para_no_teto_antes_da_primeira_resposta_de_teste() -> None:
    """``rodar_bateria`` no modo de ferramentas de teste, com o teto antes da primeira escrita: nenhuma resposta de
    ferramenta de teste, e o C-0.4 fica com a razao da parada (``teto``), como o resto, nunca ``falhou`` nem
    ``sem_resposta``."""
    modelo = _modelo_do_duble("ferramentas_de_teste")
    alvo = AlvoDoTeste(
        modelo,
        producao=PRODUCAO,
        producoes=(),
        nivel=2,
        ligacao=SistemaNaMemoria(),  # type: ignore[arg-type]
        segredo_teste="chave-teste",
        segredo_producao="chave-declaracoes",
        teto=5,  # as duas listas, a consulta de controle, o selo_alvo da producao e o rastro antes da primeira escrita
        conectar=Cortes(),
    )
    rodada = await rodar_bateria(alvo, modelo, ferramentas={"enviar_lembrete": F1}, nivel=2, pares=0)
    assert (rodada.parou, rodada.escritas, alvo.marcas) == ("teto", 0, [])
    por_caso = {i["caso"]: (i["resultado"], i["razao"]) for i in rodada.itens}
    assert por_caso["C-0.4"] == ("nao_testado", "teto")
    assert por_caso["C-2.1"] == ("nao_testado", "teto")
    assert contrato.problema_nos_casos(rodada.itens, [F1]) is None


async def test_c04_marca_que_faltou_falha() -> None:
    """A resposta de ferramenta de teste sem a marca falha o C-0.4 (o modo ``ferramenta_de_teste_sem_marca``)."""
    casos, j = await _casos(SistemaNaMemoria(), modo="ferramentas_de_teste")
    e = casos.declaracao.ferramentas["enviar_lembrete"]
    with pytest.raises(SemMarca, match="marca_faltou"):
        await casos._acao("enviar_lembrete_teste", e.argumentos, casos.alvo.numero_novo())
    casos.alvos("fora_do_alvo")
    assert _item(j, "C-0.4") == ("falhou", None)


# --- O plano (B5) -----------------------------------------------------------------------------------------------


def _modelo(modo: str = "ambiente_de_teste") -> Any:
    endereco = "https://teste.duble.exemplo.teste/mcp" if modo == "ambiente_de_teste" else None
    return validar(selo_nivel2.declaracao(endereco_teste=endereco, modo=modo))  # type: ignore[arg-type]


def test_plano_conta_chamadas_e_escritas_pela_declaracao() -> None:
    modelo = _modelo()
    exato = plano(modelo, 3, pares=5, idempotentes={"marcar_lido"})
    maximo = plano(modelo, 3, pares=5)
    assert exato.chamadas < maximo.chamadas
    assert exato.escritas < maximo.escritas
    assert plano(modelo, 2, pares=5, idempotentes={"marcar_lido"}).chamadas < exato.chamadas
    # Cada par a mais sao duas escritas a mais por acao.
    mais = plano(modelo, 3, pares=6, idempotentes={"marcar_lido"})
    assert (mais.chamadas - exato.chamadas, mais.escritas - exato.escritas) == (8, 8)
    assert plano(modelo, 3, pares=0, idempotentes={"marcar_lido"}).escritas == exato.escritas - 40
    # O modo de ferramentas de teste nao pergunta ao alvo quem ele e (o alvo e a producao).
    assert plano(_modelo("ferramentas_de_teste"), 3, pares=5).chamadas == maximo.chamadas - 1
    with pytest.raises(ValueError, match="niveis 2 e 3"):
        plano(modelo, 1, pares=5)


def test_frase_do_teto_na_tela() -> None:
    assert frase_do_teto(Plano(320, 140), 300) == "Este teste precisa de 320 chamadas; o teto é 300."


def test_motivos_legiveis_cobrem_a_lista_do_contrato_e_nao_prometem() -> None:
    assert set(MOTIVOS_LEGIVEIS) == set(contrato.MOTIVOS_DE_INTERRUPCAO)
    texto = json.dumps(MOTIVOS_LEGIVEIS, ensure_ascii=False).lower()
    for proibido in ("garante", "certificado", "conforme", "aprovado no", "mws"):
        assert proibido not in texto
    assert texto.isascii()
