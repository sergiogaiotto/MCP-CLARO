"""Contrato do selo (``mcp_claro.selo.contrato``): nomes, esquemas, vocabulario, passagens, casos, requisitos e as
assinaturas que os outros grupos usam. A igualdade com o banco esta em ``tests/integracao/test_migracao_0007.py``."""

from __future__ import annotations

import ast
import inspect
import itertools
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from mcp_claro.config.politica import POLITICA, dias_de_aviso_do_selo
from mcp_claro.selo import contrato
from mcp_claro.versao import PREFIXO_META

# --- Nomes e chaves --------------------------------------------------------------------------------------------


def test_nome_versao_e_chaves_de_meta() -> None:
    assert contrato.CONTRATO == "selo/1"
    assert contrato.VERSAO_BATERIA == "1.0.0"
    assert contrato.CHAVE_OPERACAO == PREFIXO_META + "operacao" == "br.com.claro.mcp-claro/operacao"
    assert contrato.CHAVE_RECIBO == "br.com.claro.mcp-claro/recibo"
    assert contrato.CHAVE_ALVO == "br.com.claro.mcp-claro/alvo"
    # O prefixo nao e dos reservados pelo MCP: nenhum rotulo e "mcp" nem "modelcontextprotocol".
    rotulos = PREFIXO_META.rstrip("/").split(".")
    assert not {"mcp", "modelcontextprotocol"} & set(rotulos)


@pytest.mark.parametrize(
    ("valor", "valido"),
    [
        (str(uuid.uuid4()), True),
        (str(uuid.uuid4()).upper(), False),
        ("{" + str(uuid.uuid4()) + "}", False),
        (str(uuid.uuid4()) + "\n", False),
        ("CLR-2026-00000001", False),
        (uuid.uuid4(), False),
        (None, False),
    ],
)
def test_numero_da_operacao_e_uuid_em_minusculas(valor: Any, valido: bool) -> None:
    assert contrato.numero_valido(valor) is valido


def test_ferramentas_do_contrato_por_nivel_e_nomes_reservados() -> None:
    assert contrato.ferramentas_do_nivel(2) == ("selo_alvo", "selo_consultar")
    assert contrato.ferramentas_do_nivel(3) == ("selo_alvo", "selo_consultar", "selo_bloqueio")
    with pytest.raises(ValueError, match="niveis 2 e 3"):
        contrato.ferramentas_do_nivel(1)
    assert {"selo_alvo", "selo_consultar", "selo_bloqueio"} == contrato.NOMES_RESERVADOS
    assert set(contrato.ESQUEMAS) == contrato.NOMES_RESERVADOS


# --- Vocabulario -----------------------------------------------------------------------------------------------


def test_vocabulario_fechado_e_sem_acento() -> None:
    listas = [
        contrato.ESTADOS,
        contrato.EVENTOS_DEPOIS,
        contrato.DESFECHOS,
        contrato.RESULTADOS,
        contrato.RAZOES,
        contrato.MOTIVOS_DO_SELO,
        contrato.MOTIVOS_DE_RETIRADA,
        contrato.MOTIVOS_DE_INTERRUPCAO,
        contrato.ESTADOS_DO_AVISO,
        contrato.ESTADOS_DO_PEDIDO,
        contrato.MODOS,
        contrato.ALVOS,
        contrato.ORIGENS,
    ]
    for lista in listas:
        assert len(set(lista)) == len(lista)
        assert all(v.isascii() and v == v.lower() and " " not in v for v in lista)
    assert set(contrato.CONTA_COMO) == set(contrato.ESTADOS) == set(contrato.QUER_DIZER)
    assert set(contrato.ESTADOS_EM_ABERTO) | set(contrato.ESTADOS_FINAIS) == set(contrato.ESTADOS)
    assert not set(contrato.ESTADOS_EM_ABERTO) & set(contrato.ESTADOS_FINAIS)


def test_vocabulario_tem_as_correcoes_da_critica() -> None:
    # A2: sistema fora de ativo (ou porta fechada) e um desfecho, sem estado.
    assert "sistema_fora" in contrato.DESFECHOS
    # M7: o que e do MCP-CLARO (chave, sala, sistema fora) nao gasta tentativa nem conta para a retirada.
    assert set(contrato.DESFECHOS_QUE_GASTAM_TENTATIVA) == {"respondeu", "sem_resposta", "resposta_invalida"}
    assert set(contrato.DESFECHOS_DE_FALHA_DO_SISTEMA) == {"sem_resposta", "resposta_invalida"}
    # M4: desfeita tambem da numero novo ao pedido igual.
    assert set(contrato.ESTADOS_QUE_QUEIMAM) == {"nao_recebida", "recusada", "parada", "desfeita"}
    # M2: o recibo de uma chamada com numero queimado diz nao_recebida ou parada; desfeita nunca vem no recibo.
    assert {"nao_recebida", "parada"} <= set(contrato.ESTADOS_DO_RECIBO)
    assert "desfeita" not in contrato.ESTADOS_DO_RECIBO
    # A3: nao_recebida tem a definicao precisa.
    assert "nunca vai pôr em efeito" in contrato.QUER_DIZER["nao_recebida"]
    assert "em_andamento" in contrato.QUER_DIZER["nao_recebida"]
    # M6 e M5: as razoes novas.
    assert {"sem_segundo_efeito", "poucos_pares", "declarado"} <= set(contrato.RAZOES)
    # M11: a lista fechada de tipos de chave do ADMIN.
    assert contrato.TIPOS_DE_CHAVE_DO_ADMIN == ("acesso", "declaracoes", "teste_selo")
    assert contrato.TIPO_CHAVE_TESTE == "teste_selo"


@pytest.mark.parametrize(
    ("antes", "novo", "permitida"),
    [
        (None, "concluida", True),
        ("em_andamento", "concluida", True),
        ("em_andamento", "nao_recebida", False),
        ("desconhecido", "nao_recebida", True),
        ("concluida", "desconhecido", True),
        ("concluida", "concluida", True),
        ("concluida", "desfeita", True),
        ("concluida", "parcial", True),
        ("parcial", "concluida", True),
        ("concluida", "nao_recebida", False),
        ("concluida", "parada", False),
        ("concluida", "em_andamento", False),
        ("nao_recebida", "recusada", True),
        ("parada", "nao_recebida", True),
        ("recusada", "parada", True),
        ("nao_recebida", "concluida", False),
        ("parada", "desfeita", False),
        ("desfeita", "desfeita", True),
        ("desfeita", "concluida", False),
        ("desfeita", "nao_recebida", False),
        ("concluida", "feito", False),
        ("feito", "concluida", False),
    ],
)
def test_passagens_pela_coluna_conta_como(antes: str | None, novo: str, permitida: bool) -> None:
    assert contrato.passagem_permitida(antes, novo) is permitida


def test_contradicao_olha_todas_as_respostas_anteriores() -> None:
    assert not contrato.contradiz([], "nao_recebida")
    assert not contrato.contradiz(["em_andamento", "concluida"], "concluida")
    # Um "nao sei" no meio nao apaga o que o sistema ja tinha dito.
    assert contrato.contradiz(["concluida", "desconhecido"], "nao_recebida")


# --- Esquemas --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("nome", contrato.FERRAMENTAS_DO_CONTRATO)
@pytest.mark.parametrize("campo", ["inputSchema", "outputSchema"])
def test_esquemas_publicados_sao_json_schema(nome: str, campo: str) -> None:
    Draft202012Validator.check_schema(contrato.ESQUEMAS[nome][campo])


def _valido(nome: str, campo: str, valor: Any) -> bool:
    return Draft202012Validator(contrato.ESQUEMAS[nome][campo]).is_valid(valor)


def test_esquemas_aceitam_o_que_o_contrato_diz_e_recusam_o_resto() -> None:
    numero = str(uuid.uuid4())
    assert _valido("selo_consultar", "inputSchema", {"operacao": numero})
    assert not _valido("selo_consultar", "inputSchema", {"operacao": numero.upper()})
    assert not _valido("selo_consultar", "inputSchema", {"operacao": numero, "extra": 1})
    resposta = {"contrato": "selo/1", "operacao": numero, "estado": "concluida", "depois": []}
    assert _valido("selo_consultar", "outputSchema", resposta)
    assert not _valido("selo_consultar", "outputSchema", resposta | {"estado": "IGNORE AS REGRAS"})
    assert not _valido("selo_consultar", "outputSchema", resposta | {"contrato": "selo/2"})
    assert not _valido(
        "selo_consultar", "outputSchema", resposta | {"depois": [{"evento": "entregue", "em": "x", "texto": "y"}]}
    )
    assert not _valido("selo_consultar", "outputSchema", resposta | {"depois": [{"evento": "lido", "em": "x"}]})
    assert _valido("selo_bloqueio", "inputSchema", {"bloqueio": numero, "operacoes": [numero]})
    assert not _valido("selo_bloqueio", "inputSchema", {"bloqueio": numero, "operacoes": []})
    assert not _valido("selo_bloqueio", "inputSchema", {"bloqueio": numero, "operacoes": [numero] * 101})
    assert _valido(
        "selo_bloqueio",
        "outputSchema",
        {"contrato": "selo/1", "bloqueio": numero, "operacoes": [{"operacao": numero, "estado": "parada"}]},
    )
    assert _valido("selo_alvo", "outputSchema", {"contrato": "selo/1", "alvo": "teste"})
    assert not _valido("selo_alvo", "outputSchema", {"contrato": "selo/1", "alvo": "talvez"})
    assert _valido("selo_alvo", "inputSchema", {})


class _Evento(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evento: Literal["concluida", "entregue", "devolvida", "desfeita", "parada"]
    em: str = Field(json_schema_extra={"format": "date-time"})


class _Consulta(BaseModel):
    contrato: Literal["selo/1"]
    operacao: str
    estado: Literal[
        "desconhecido", "concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada"
    ]
    depois: list[_Evento] = Field(default_factory=list, max_length=20)


class _Alvo(BaseModel):
    contrato: Literal["selo/1"]
    alvo: Literal["producao", "teste"]


class _Item(BaseModel):
    operacao: str
    estado: Literal[
        "concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada", "desconhecido"
    ]


class _Bloqueio(BaseModel):
    contrato: Literal["selo/1"]
    bloqueio: str
    operacoes: list[_Item]


NUMERO = Annotated[str, Field(pattern=contrato.PADRAO_UUID)]


def _kit() -> dict[str, Any]:
    """As tres ferramentas do contrato feitas do jeito comum do kit 2.3.0 (``Tool.from_function``)."""
    from mcp.server.mcpserver.tools.base import Tool

    def selo_alvo() -> _Alvo:
        raise NotImplementedError

    def selo_consultar(operacao: NUMERO) -> _Consulta:
        raise NotImplementedError

    def selo_bloqueio(bloqueio: NUMERO, operacoes: Annotated[list[NUMERO], Field(min_length=1)]) -> _Bloqueio:
        raise NotImplementedError

    saida = {}
    for funcao in (selo_alvo, selo_consultar, selo_bloqueio):
        t = Tool.from_function(funcao)
        saida[t.name] = {"name": t.name, "inputSchema": t.parameters, "outputSchema": t.output_schema}
    return saida


@pytest.mark.parametrize("nome", contrato.FERRAMENTAS_DO_CONTRATO)
def test_o_que_o_kit_gera_confere_com_o_contrato(nome: str) -> None:
    """Critica B4: ``title``, ``$defs``, ``$ref``, sem ``additionalProperties``, ``type`` ao lado de ``const`` e
    ``enum`` em outra ordem nao reprovam um sistema feito com o kit."""
    definicao = _kit()[nome]
    assert "title" in definicao["inputSchema"]
    assert contrato.definicao_confere(definicao)


def test_o_que_muda_o_sentido_nao_confere() -> None:
    base = contrato.ESQUEMAS["selo_consultar"]
    entrada = base["inputSchema"]
    sem_padrao = {**entrada, "properties": {"operacao": {"type": "string"}}}
    outro_tipo = {**entrada, "properties": {"operacao": {"type": "integer"}}}
    a_mais = {**entrada, "properties": {**entrada["properties"], "extra": {"type": "string"}}}
    escolha = {**entrada, "anyOf": [{"required": ["operacao"]}]}
    for errado in (sem_padrao, outro_tipo, a_mais, escolha, {**entrada, "required": []}, None, "texto"):
        assert not contrato.esquema_confere("selo_consultar", "inputSchema", errado), errado
    saida = base["outputSchema"]
    outro_estado = {
        **saida,
        "properties": {**saida["properties"], "estado": {"enum": [*contrato.ESTADOS, "feito"]}},
    }
    assert not contrato.esquema_confere("selo_consultar", "outputSchema", outro_estado)
    assert not contrato.definicao_confere({"name": "outra", "inputSchema": entrada})
    # O que so anota nao conta.
    anotado = {**entrada, "title": "x", "description": "y", "$schema": "https://json-schema.org/draft/2020-12/schema"}
    assert contrato.esquema_confere("selo_consultar", "inputSchema", anotado)


def test_forma_essencial_resolve_referencias_e_marca_o_que_nao_resolve() -> None:
    assert contrato.forma_essencial({"$ref": "#/$defs/A", "$defs": {"A": {"type": "string", "title": "A"}}}) == {
        "type": "string"
    }
    assert contrato.forma_essencial({"$ref": "https://fora"}) == "ref_de_fora"
    assert contrato.forma_essencial({"$ref": "#/$defs/B", "$defs": {}}) == "ref_sem_alvo"
    assert contrato.forma_essencial({"$ref": "#/$defs/A", "$defs": {"A": {"$ref": "#/$defs/A"}}}) == "ref_demais"
    # 34 niveis de items cabem no orcamento: o nivel 33 passa do fundo e vira o texto que nunca confere.
    fundo: dict[str, Any] = {"type": "string"}
    for _ in range(34):
        fundo = {"items": fundo}
    forma = contrato.forma_essencial(fundo)
    for _ in range(contrato.FUNDO_MAXIMO_DO_ESQUEMA):
        forma = forma["items"]
    assert forma == {"items": "fundo_demais"}
    # 40 niveis de properties passam do orcamento antes do fundo: o esquema inteiro vira grande_demais.
    largo: dict[str, Any] = {"type": "string"}
    for _ in range(40):
        largo = {"properties": {"x": largo}}
    assert contrato.forma_essencial(largo) == contrato.GRANDE_DEMAIS


# --- Orcamento de nos da forma essencial (retoque G02r) ------------------------------------------------------

CICLO_QUE_EXPLODE: Final = {
    "$defs": {"a": {"type": "object", "properties": {"x": {"$ref": "#/$defs/a"}, "y": {"$ref": "#/$defs/a"}}}},
    "$ref": "#/$defs/a",
}
"""129 bytes cuja forma essencial, sem orcamento, tem 2^33 partes (achado do verificador do G2)."""


def cadeia_que_explode(n: int = 32) -> dict[str, Any]:
    """A mesma explosao sem ciclo: a0 -> {x: a1, y: a1} -> ... -> a32."""
    defs: dict[str, Any] = {
        f"a{i}": {
            "type": "object",
            "properties": {"x": {"$ref": f"#/$defs/a{i + 1}"}, "y": {"$ref": f"#/$defs/a{i + 1}"}},
        }
        for i in range(n)
    }
    defs[f"a{n}"] = {"type": "string"}
    return {"$defs": defs, "$ref": "#/$defs/a0"}


def no_teto(base: dict[str, Any], *, a_mais: int = 0) -> dict[str, Any]:
    """``base`` com uma descricao na raiz que leva o custo a exatamente ``TETO_NOS_DO_ESQUEMA + a_mais`` nos (a
    descricao e anotacao: a forma nao muda, so o custo)."""
    custo = contrato.custo_da_forma_essencial(base)
    assert custo is not None
    # A chave "description" (1) e o texto (1 + bytes // 64) entram no peso da raiz, visitada uma vez.
    falta = contrato.TETO_NOS_DO_ESQUEMA + a_mais - custo - 2
    assert falta >= 0
    return {**base, "description": "d" * (falta * contrato.BYTES_POR_NO)}


@pytest.mark.parametrize("esquema", [CICLO_QUE_EXPLODE, cadeia_que_explode()], ids=["ciclo", "cadeia"])
def test_esquema_que_explode_vira_grande_demais_depressa(esquema: dict[str, Any]) -> None:
    inicio = time.monotonic()
    assert contrato.forma_essencial(esquema) == contrato.GRANDE_DEMAIS
    assert contrato.custo_da_forma_essencial(esquema) is None
    assert time.monotonic() - inicio < 1
    assert not contrato.esquema_confere("selo_consultar", "inputSchema", esquema)


def test_orcamento_vale_ate_o_teto_e_para_um_no_depois() -> None:
    base = contrato.ESQUEMAS["selo_consultar"]["outputSchema"]
    no = no_teto(base)
    assert contrato.custo_da_forma_essencial(no) == contrato.TETO_NOS_DO_ESQUEMA
    assert contrato.forma_essencial(no) == contrato.forma_essencial(base)
    assert contrato.esquema_confere("selo_consultar", "outputSchema", no)
    passou = no_teto(base, a_mais=1)
    assert contrato.custo_da_forma_essencial(passou) is None
    assert contrato.forma_essencial(passou) == contrato.GRANDE_DEMAIS
    assert not contrato.esquema_confere("selo_consultar", "outputSchema", passou)


def test_cada_ref_seguido_gasta_de_novo_a_definicao() -> None:
    """Duas referencias a mesma definicao gastam o peso dela duas vezes, e a definicao conta tambem dentro da raiz."""
    definicao = {"type": "string", "description": "x" * 640}
    uma = {"type": "object", "properties": {"a": {"$ref": "#/$defs/d"}}, "$defs": {"d": definicao}}
    duas = {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/d"}, "b": {"$ref": "#/$defs/d"}},
        "$defs": {"d": definicao},
    }
    custo_uma, custo_duas = contrato.custo_da_forma_essencial(uma), contrato.custo_da_forma_essencial(duas)
    assert custo_uma is not None
    assert custo_duas is not None
    peso_ref = contrato.peso_do_json({"$ref": "#/$defs/d"})
    # b: o nome (1) e o {"$ref"} na raiz, o {"$ref"} recebido e a definicao seguida.
    assert custo_duas - custo_uma == 1 + 2 * peso_ref + contrato.peso_do_json(definicao)


@pytest.mark.parametrize(
    ("valor", "peso"),
    [
        (None, 1),
        (True, 1),
        (0, 1),
        ("", 1),
        ("a" * 63, 1),
        ("a" * 64, 2),
        ("ç" * 32, 2),  # 64 bytes em UTF-8
        ("ç" * 31, 1),
        (1e16, 1),  # o banco escreve 10000000000000000
        (1e70, 2),  # o banco escreve 71 letras
        (5e-324, 6),  # o banco escreve 0. e 324 casas
        (-0.0, 1),
        (int("9" * 64), 2),
        ([], 1),
        ({}, 1),
        ({"a": 1}, 3),
        ({"k" * 64: [1, "x"]}, 6),
        ([[[]]], 3),
    ],
)
def test_peso_do_json(valor: Any, peso: int) -> None:
    assert contrato.peso_do_json(valor) == peso


@pytest.mark.parametrize(
    ("numero", "texto"),
    [
        (1e16, "10000000000000000"),
        (1.5e-07, "0.00000015"),
        (100.0, "100.0"),
        (-0.0, "0.0"),
        (0.1, "0.1"),
        (-12, "-12"),
    ],
)
def test_numero_como_o_banco_escreve(numero: float, texto: str) -> None:
    assert contrato.texto_do_numero_no_banco(numero) == texto


def test_inteiro_alem_do_teto_de_str_tambem_tem_texto() -> None:
    """str(int) recusa mais de 4300 digitos; o peso continua sem erro."""
    assert contrato.texto_do_numero_no_banco(10**5000) == "1" + "0" * 5000
    assert contrato.peso_do_json(10**5000) == 1 + 5001 // contrato.BYTES_POR_NO


def test_custo_dos_esquemas_do_kit_cabe_com_folga() -> None:
    """O mais caro dos esquemas que o kit gera para as ferramentas do contrato custa ``CUSTO_DO_KIT_MEDIDO`` nos, e o
    teto da mais de dez vezes isso. Os publicados custam menos."""
    custos = [
        contrato.custo_da_forma_essencial(definicao[campo])
        for definicao in _kit().values()
        for campo in ("inputSchema", "outputSchema")
    ]
    publicados = [contrato.custo_da_forma_essencial(e) for f in contrato.ESQUEMAS.values() for e in f.values()]
    assert None not in custos
    assert None not in publicados
    assert max(c for c in custos if c is not None) == contrato.CUSTO_DO_KIT_MEDIDO
    assert max(c for c in publicados if c is not None) <= contrato.CUSTO_DO_KIT_MEDIDO
    assert contrato.TETO_NOS_DO_ESQUEMA > 10 * contrato.CUSTO_DO_KIT_MEDIDO


def test_custo_nao_depende_da_ordem_das_chaves() -> None:
    """O banco anda nas chaves em outra ordem (jsonb: por tamanho, depois por byte); o custo e a forma sao os mesmos."""
    esquema = _kit()["selo_consultar"]["outputSchema"]

    def invertido(valor: Any) -> Any:
        if isinstance(valor, dict):
            return {k: invertido(valor[k]) for k in reversed(list(valor))}
        if isinstance(valor, list):
            return [invertido(v) for v in valor]
        return valor

    assert contrato.custo_da_forma_essencial(invertido(esquema)) == contrato.custo_da_forma_essencial(esquema)
    assert contrato.forma_essencial(invertido(esquema)) == contrato.forma_essencial(esquema)


# --- Endereco --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outro",
    [
        "https://cobranca.exemplo/mcp/",
        "HTTPS://COBRANCA.EXEMPLO/mcp",
        "https://cobranca.exemplo:443/mcp",
        "https://cobranca.exemplo/mcp?ambiente=teste",
        "https://cobranca.exemplo/mcp#teste",
        "https://cobranca.exemplo./mcp",
        "https://equipe@cobranca.exemplo/mcp",
        "https://cobranca.exemplo//mcp/./",
        "https://cobranca.exemplo/teste/../mcp",
        "https://cobranca.exemplo/%6dcp",
        " https://cobranca.exemplo/MCP ",
    ],
)
def test_endereco_que_pode_ser_o_mesmo_servico_e_igual(outro: str) -> None:
    """Critica A4: barra, maiusculas, porta padrao, consulta, fragmento, usuario e ponto no fim nao enganam."""
    assert contrato.mesmo_endereco("https://cobranca.exemplo/mcp", outro)


@pytest.mark.parametrize(
    "outro",
    [
        "https://teste.cobranca.exemplo/mcp",
        "https://cobranca.exemplo:8443/mcp",
        "http://cobranca.exemplo/mcp",
        "https://cobranca.exemplo/mcp-teste",
        "https://cobranca.exemplo/m%2Fcp",
    ],
)
def test_endereco_de_outro_servico_e_diferente(outro: str) -> None:
    assert not contrato.mesmo_endereco("https://cobranca.exemplo/mcp", outro)


def test_endereco_normalizado_mostra_a_forma() -> None:
    assert contrato.endereco_normalizado("HTTPS://User@Cobranca.Exemplo.:0443//A/./b/../MCP/?x#y") == (
        "https://cobranca.exemplo/a/mcp"
    )
    assert contrato.endereco_normalizado("http://[::1]:80/") == "http://[::1]"
    assert contrato.endereco_normalizado("nao e URL") == "nao e url"


# --- Requisitos ------------------------------------------------------------------------------------------------


def test_requisitos_tem_ids_proprios_e_citam_so_os_11_ids_conferidos() -> None:
    ids = [r.id for r in contrato.REQUISITOS]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("SELO-") and "MWS" not in i for i in ids)
    citados = {i for r in contrato.REQUISITOS for i in (*r.adaptado_de, *r.parte_de)}
    citados |= {i for _, ids_rec in contrato.RECOMENDACOES for i in ids_rec}
    assert citados == contrato.IDS_DO_RASCUNHO_USADOS
    assert len(contrato.IDS_DO_RASCUNHO_USADOS) == 11
    assert not any(i.startswith("MWS-CFG") for i in citados)
    # Os adaptados dizem o que mudou (critica M5): exigencia maior, mais fraco, inspirado sem adaptar.
    por_id = {r.id: r for r in contrato.REQUISITOS}
    assert "exigência maior" in por_id["SELO-2.2"].origem
    assert "mais fraco" in por_id["SELO-3.3"].origem
    assert "sem adaptá-la" in por_id["SELO-3.2"].origem
    assert por_id["SELO-2.6"].testado is False
    # B7: SELO-2.1 vale sempre, e o selo e so para sistema com acao e chave por pessoa.
    assert por_id["SELO-2.1"].vale_quando == "sempre"
    assert "chave por pessoa" in por_id["SELO-2.1"].texto
    # A4: o alvo se declara de teste.
    assert "selo_alvo" in por_id["SELO-0.3"].texto


def test_nenhum_texto_de_resultado_cita_o_rascunho() -> None:
    """Critica B6: os IDs e o nome do rascunho ficam so nas fontes do contrato, nunca perto de um resultado."""
    textos = [c.o_que_testamos for c in contrato.CASOS] + [n.texto for n in contrato.NAO_TESTADOS]
    textos += list(contrato.QUER_DIZER.values())
    for texto in textos:
        assert "MWS" not in texto, texto
        assert "rascunho" not in texto.lower(), texto
    for r in contrato.REQUISITOS:
        assert "MWS" not in r.texto, r.id


# --- Casos -----------------------------------------------------------------------------------------------------


def test_casos_cobrem_os_requisitos_e_as_correcoes_da_critica() -> None:
    requisitos = {r.id: r for r in contrato.REQUISITOS}
    casos = contrato.CASOS_POR_NOME
    assert len(casos) == len(contrato.CASOS)
    for c in contrato.CASOS:
        assert c.requisito in requisitos
        assert c.nivel in requisitos[c.requisito].niveis or c.requisito.startswith("SELO-0")
        assert c.nao_se_aplica_ate in (0, 2, 3)
        assert c.maximo in ("cumpriu", "nao_observado")
        assert c.o_que_testamos.endswith(".")
    testados = {c.requisito for c in contrato.CASOS}
    # SELO-0.2 e a propria declaracao: sem ela o teste nao comeca (selo/declaracao.py e declaracoes_selo_nasce).
    assert testados | {"SELO-0.2"} == {r.id for r in contrato.REQUISITOS if r.testado}
    # A3: a resposta cortada pelo proprio gateway.
    assert casos["C-2.6a"].obrigatorio
    assert casos["C-2.6a"].nao_se_aplica_ate == 0
    # A4: o alvo se declara de teste; a producao so recebe a consulta de controle.
    assert casos["C-0.4"].requisito == "SELO-0.3"
    assert "nao_recebida" in casos["C-0.3"].o_que_testamos
    # M5: o que a bateria so ve de fora da no maximo nao_observado; os pares so informam.
    assert casos["C-2.3"].maximo == casos["C-2.7"].maximo == "nao_observado"
    assert not casos["C-2.2d"].obrigatorio
    # M6: C-2.2b pode nao se aplicar (sem segundo efeito); C-2.4b so no nivel 2.
    assert casos["C-2.2b"].nao_se_aplica_ate == 3
    assert casos["C-2.4b"].nao_se_aplica_ate == 2
    assert contrato.PARES_MINIMOS == 30


def _aprovam(nivel: int, ferramentas: list[str]) -> list[dict[str, Any]]:
    itens = []
    for c in contrato.CASOS:
        if c.nivel > nivel or not c.obrigatorio:
            continue
        for f in [None] if c.escopo == "sistema" else ferramentas:
            itens.append(
                {"caso": c.caso, "ferramenta": f, "resultado": c.maximo, "razao": None, "tentativas": 1, "excluidas": 0}
            )
    return itens


def test_criterio_do_mcp_claro() -> None:
    f = [str(uuid.uuid4()), str(uuid.uuid4())]
    itens = _aprovam(2, f)
    assert contrato.problema_nos_casos(itens, f) is None
    assert contrato.casos_aprovam(itens, f, 2)
    # Sem os de nivel 3, nao aprova no 3.
    assert not contrato.casos_aprovam(itens, f, 3)
    # Um caso so informativo que falhou reprova.
    falhou = [
        *itens,
        {"caso": "C-2.2d", "ferramenta": f[0], "resultado": "falhou", "razao": None, "tentativas": 1, "excluidas": 0},
    ]
    assert contrato.problema_nos_casos(falhou, f) is None
    assert not contrato.casos_aprovam(falhou, f, 2)
    # Faltar um caso numa das ferramentas reprova.
    assert not contrato.casos_aprovam(
        [i for i in itens if not (i["caso"] == "C-2.5" and i["ferramenta"] == f[1])], f, 2
    )
    assert contrato.contagem_minima_de_chamadas(itens) == len(itens)


def test_resultado_aprova_pelo_maximo_e_pelo_nao_se_aplica_onde_cabe() -> None:
    casos = contrato.CASOS_POR_NOME
    assert contrato.resultado_aprova(casos["C-2.1"], "cumpriu", 2)
    assert not contrato.resultado_aprova(casos["C-2.1"], "nao_observado", 2)
    assert contrato.resultado_aprova(casos["C-2.3"], "nao_observado", 2)
    assert contrato.resultado_aprova(casos["C-2.4b"], "nao_se_aplica", 2)
    assert not contrato.resultado_aprova(casos["C-2.4b"], "nao_se_aplica", 3)
    assert not contrato.resultado_aprova(casos["C-2.1"], "nao_se_aplica", 2)


def test_o_que_nao_e_testado_e_fixo() -> None:
    assert contrato.nao_testado_gravado() == [
        {"item": "guarda_dos_numeros", "razao": "declarado"},
        {"item": "interposicao_e_reinicio", "razao": "interposicao"},
    ]
    assert all(n.razao in contrato.RAZOES for n in contrato.NAO_TESTADOS)


# --- Assinaturas dos outros grupos -----------------------------------------------------------------------------


def test_assinaturas_para_os_grupos_seguintes() -> None:
    chamar = inspect.signature(contrato.LigacaoComNumero.chamar_ferramenta)
    parametro = chamar.parameters["chave_operacao"]
    assert parametro.kind is inspect.Parameter.KEYWORD_ONLY
    assert parametro.default is None
    efetivo = inspect.signature(contrato.CalculaEfetivo.__call__)
    assert [p.name for p in efetivo.parameters.values()] == ["self", "vigente", "ligada", "pessoa_teste"]
    assert efetivo.parameters["ligada"].kind is inspect.Parameter.KEYWORD_ONLY
    conferir = inspect.signature(contrato.ConfereNoDespachante.conferir)
    assert [p.name for p in conferir.parameters.values()] == ["self", "operacao_id"]  # sem "ultima" (critica M7)
    assert contrato.ROTA_CONSULTAR == "/interno/selo/consultar"
    assert {"informado", "selo"} <= set(dir(contrato.TemInformado))
    # Critica A3.3: a tela so resolve o "estamos confirmando" (e oferece o botao de um toque) pelo selo_estado_final.
    assert {"estado", "desfecho", "origem", "gravado_em", "de_terceiro", "estado_final"} <= set(dir(contrato.Informado))
    assert "selo_estado_final" in " ".join((contrato.Informado.__doc__ or "").split())
    assert set(contrato.RETRATO_DO_SELO) == {"nivel", "teste_id", "testado_em", "vale_ate", "modo", "motivo"}
    assert contrato.MOTIVO_FASE_DESLIGADA not in contrato.MOTIVOS_DO_SELO
    assert contrato.MOTIVOS_DO_RETRATO[:-1] == contrato.MOTIVOS_DO_SELO
    assert contrato.MOTIVOS_DO_RETRATO[-1] == contrato.MOTIVO_FASE_DESLIGADA == "fase_desligada"


def _docstrings_das_constantes() -> dict[str, str | None]:
    arvore = ast.parse(inspect.getsource(contrato))
    return {
        alvo.id: ast.get_docstring(ast.Module(body=[seguinte], type_ignores=[]), clean=True)
        for anterior, seguinte in zip(arvore.body, arvore.body[1:], strict=False)
        if isinstance(anterior, ast.AnnAssign)
        and isinstance((alvo := anterior.target), ast.Name)
        and isinstance(seguinte, ast.Expr)
    }


def test_contrato_da_rota_e_do_retrato_para_o_g3_e_o_g4() -> None:
    """Critica M7: a rota confere pelo banco se ja e a vez (409 antes da hora) e grava a retirada num SAVEPOINT.
    Critica M3: o retrato leva as datas com microssegundos, e a ficha cai para o retrato de agora num SAVEPOINT."""
    textos = _docstrings_das_constantes()
    doc = " ".join((textos["ROTA_CONSULTAR"] or "").split())
    assert "ainda nao e a vez pela escada" in doc
    assert "espera_da_consulta" in doc
    assert "num SAVEPOINT" in doc
    retrato = " ".join((textos["RETRATO_DO_SELO"] or "").split())
    assert "microssegundos" in retrato
    assert "SAVEPOINT" in retrato


def test_escada_da_consulta() -> None:
    """Desenho 5.2: 1, 2, 5, 15, 30, 60, 120 e 240 minutos, pouco mais de 8 horas; depois do fim, o ultimo degrau ate
    o maximo (BADGE_LOOKUP_MAX_ATTEMPTS vai de 1 a 20)."""
    assert contrato.ESCADA_DA_CONSULTA_MINUTOS == (1, 2, 5, 15, 30, 60, 120, 240)
    assert sum(contrato.ESCADA_DA_CONSULTA_MINUTOS) == 473
    assert [contrato.espera_da_consulta(n, 8) for n in range(9)] == [1, 2, 5, 15, 30, 60, 120, 240, None]
    assert contrato.espera_da_consulta(10, 20) == 240
    assert contrato.espera_da_consulta(0, 1) == 1
    assert contrato.espera_da_consulta(1, 1) is None
    with pytest.raises(ValueError, match="negativo"):
        contrato.espera_da_consulta(-1, 8)


ENVIO = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PRAZO = 20.0  # RESPONSE_MAX_WAIT_SECONDS padrao


def _linha(minutos: float, origem: str = "consulta", desfecho: str = "sem_resposta", estado: str | None = None) -> Any:
    if estado is not None and desfecho == "sem_resposta":
        desfecho = "respondeu"
    return contrato.LinhaInformada(origem, desfecho, estado, ENVIO + timedelta(minutes=minutos))


def _proxima(linhas: list[Any], maximo: int = 8) -> datetime | None:
    return contrato.proxima_consulta(linhas, enviado_em=ENVIO, maximo=maximo, prazo_envio_segundos=PRAZO)


def test_escada_conta_so_as_consultas_que_gastam_a_vez() -> None:
    """Defeito da escada na conferencia final: as tentativas sao so as linhas de origem 'consulta' com desfecho em
    DESFECHOS_QUE_GASTAM_TENTATIVA. O recibo e a resposta ao aviso nunca contam, nem sem_chave, sala_fora e
    sistema_fora."""
    linhas = [
        _linha(0, "recibo", estado="em_andamento"),
        _linha(1, "aviso", estado="em_andamento"),
        _linha(2, "aviso", "sem_resposta"),
        _linha(3, desfecho="sem_chave"),
        _linha(4, desfecho="sala_fora"),
        _linha(5),
        _linha(6, desfecho="resposta_invalida"),
        _linha(7, estado="em_andamento"),
        _linha(8, estado="desconhecido"),
    ]
    assert contrato.tentativas_da_consulta(linhas) == 4
    assert contrato.tentativas_da_consulta(linhas[:5]) == 0
    # Decisao 3 do coordenador (retoque da etapa 3): o degrau conta todas as linhas de consulta (as 4 que gastam e as
    # 2 que nao gastam): a proxima vai 120 minutos (o setimo degrau) depois da ultima linha. Antes, 30 (o quinto).
    assert contrato.degrau_da_consulta(linhas) == 6
    assert _proxima(linhas) == ENVIO + timedelta(minutes=8 + 120)


def test_escada_conta_da_ultima_linha_de_qualquer_origem_ou_do_envio() -> None:
    """A hora base e a ultima linha da operacao, de qualquer origem e desfecho; sem nenhuma, o envio. Uma linha que
    nao gasta tentativa (sala_fora) empurra a vez sem gastar o degrau."""
    assert contrato.hora_base_da_consulta([], ENVIO) == ENVIO
    recibo = [_linha(10, "recibo", estado="em_andamento")]
    assert contrato.hora_base_da_consulta(recibo, ENVIO) == ENVIO + timedelta(minutes=10)
    assert _proxima(recibo) == ENVIO + timedelta(minutes=11)
    uma = [_linha(2)]
    assert _proxima(uma) == ENVIO + timedelta(minutes=4)
    # A sala_fora nao gasta tentativa, mas sobe o degrau (decisao 3): 5 minutos (o terceiro) depois dela. Antes, 2.
    assert _proxima([*uma, _linha(3, desfecho="sala_fora")]) == ENVIO + timedelta(minutes=3 + 5)


def test_primeira_consulta_so_depois_do_prazo_do_envio() -> None:
    """Critica A3.3: nenhuma consulta antes do prazo do envio mais a margem, contados do envio; o degrau vale quando
    e mais tarde."""
    assert contrato.MARGEM_DA_CONSULTA_SEGUNDOS == 60
    assert _proxima([]) == ENVIO + timedelta(seconds=PRAZO + 60)
    longo = contrato.proxima_consulta([], enviado_em=ENVIO, maximo=8, prazo_envio_segundos=120)
    assert longo == ENVIO + timedelta(seconds=180)
    cedo = [_linha(0.1, "recibo", estado="em_andamento")]
    assert contrato.proxima_consulta(cedo, enviado_em=ENVIO, maximo=8, prazo_envio_segundos=120) == ENVIO + timedelta(
        seconds=180
    )


def test_nao_recebida_so_encerra_com_duas_respostas_com_intervalo() -> None:
    """Critica A3.3: uma nao_recebida sozinha nao encerra a escada; duas, com ao menos o primeiro degrau entre elas,
    encerram. Qualquer outro estado final encerra na hora; em_andamento e desconhecido nao."""
    uma = [_linha(2, estado="nao_recebida")]
    assert contrato.resposta_que_encerra(uma) is None
    assert _proxima(uma) == ENVIO + timedelta(minutes=4)
    juntas = [*uma, _linha(2.5, "recibo", estado="nao_recebida")]
    assert contrato.resposta_que_encerra(juntas) is None
    duas = [*uma, _linha(4, estado="nao_recebida")]
    assert contrato.resposta_que_encerra(duas) == "nao_recebida"
    assert _proxima(duas) is None
    for estado in ("concluida", "recusada", "parcial", "desfeita", "parada"):
        assert contrato.resposta_que_encerra([_linha(2, estado=estado)]) == estado
        assert _proxima([_linha(2, estado=estado)]) is None
        assert contrato.resposta_que_encerra([*uma, _linha(5, estado=estado)]) == estado
    for estado in contrato.ESTADOS_EM_ABERTO:
        assert contrato.resposta_que_encerra([_linha(2, estado=estado)]) is None
    # Um estado final, mesmo antes de uma resposta em aberto, encerra (como selo_estado_final).
    assert contrato.resposta_que_encerra([_linha(2, estado="concluida"), _linha(4, estado="desconhecido")]) == (
        "concluida"
    )


def test_nao_recebida_confirmada_pede_duas_com_ao_menos_um_minuto() -> None:
    """Critica A3.3: a borda e o primeiro degrau, inclusive (1 minuto exato confirma; 59 segundos nao). A ordem das
    horas nao importa, e uma do meio nao muda nada."""
    assert contrato.ESCADA_DA_CONSULTA_MINUTOS[0] == 1
    hora = ENVIO
    assert not contrato.nao_recebida_confirmada([])
    assert not contrato.nao_recebida_confirmada([hora])
    assert not contrato.nao_recebida_confirmada([hora, hora])
    assert not contrato.nao_recebida_confirmada([hora, hora + timedelta(seconds=59)])
    assert contrato.nao_recebida_confirmada([hora, hora + timedelta(minutes=1)])
    assert contrato.nao_recebida_confirmada([hora + timedelta(minutes=1), hora])
    assert contrato.nao_recebida_confirmada([hora, hora + timedelta(seconds=30), hora + timedelta(minutes=1)])
    assert contrato.nao_recebida_confirmada(iter([hora, hora + timedelta(minutes=5)]))


def test_resposta_que_encerra_e_o_ultimo_estado_final() -> None:
    """Conferencia do G0r (duas mutacoes que sobreviveram): com dois estados finais diferentes vale o ultimo, e duas
    nao_recebida a 1 minuto exato ja encerram. Uma nao_recebida nao confirmada nao conta; confirmada, conta como
    qualquer outro estado final, pela ordem (a mesma regra de selo_estado_final no banco)."""
    assert contrato.resposta_que_encerra([_linha(2, estado="parcial"), _linha(4, estado="concluida")]) == "concluida"
    assert contrato.resposta_que_encerra([_linha(2, estado="concluida"), _linha(4, estado="desfeita")]) == "desfeita"
    assert contrato.resposta_que_encerra([_linha(2, estado="recusada"), _linha(3, estado="parada")]) == "parada"
    exato = [_linha(2, estado="nao_recebida"), _linha(3, origem="recibo", estado="nao_recebida")]
    assert contrato.resposta_que_encerra(exato) == "nao_recebida"
    quase = [_linha(2, estado="nao_recebida"), _linha(2 + 59 / 60, estado="nao_recebida")]
    assert contrato.resposta_que_encerra(quase) is None
    # Nao confirmada, a nao_recebida nao encobre o estado final de antes (o banco ja viu a contradicao).
    assert contrato.resposta_que_encerra([_linha(2, estado="concluida"), _linha(4, estado="nao_recebida")]) == (
        "concluida"
    )
    # Confirmada, e a ultima.
    confirmada = [_linha(2, estado="recusada"), _linha(3, estado="nao_recebida"), _linha(5, estado="nao_recebida")]
    assert contrato.resposta_que_encerra(confirmada) == "nao_recebida"
    # So 'respondeu' conta: sem_resposta e resposta_invalida nunca encerram nem confirmam.
    assert contrato.resposta_que_encerra([_linha(2, estado="nao_recebida"), _linha(4)]) is None


def test_escada_para_com_sistema_fora_e_no_maximo() -> None:
    """Critica A2: a ultima linha sistema_fora para a escada (a decisao fica com a pessoa). No maximo de tentativas,
    nao ha mais consulta, e a anterior e a ultima."""
    assert _proxima([_linha(2, desfecho="sistema_fora")]) is None
    assert _proxima([_linha(2, desfecho="sistema_fora"), _linha(3, desfecho="sala_fora")]) is not None
    sete = [_linha(n) for n in range(7)]
    assert not contrato.e_a_ultima_consulta(sete[:6], 8)
    assert contrato.e_a_ultima_consulta(sete, 8)
    assert _proxima(sete) == ENVIO + timedelta(minutes=6 + 240)
    assert _proxima([*sete, _linha(7)]) is None
    assert _proxima([], maximo=1) is not None
    assert contrato.e_a_ultima_consulta([], 1)


def test_a_resposta_ao_aviso_depois_de_sistema_fora_nao_reabre_a_escada() -> None:
    """Conferencia final da etapa 3r (defeito 3 do verificador do G3ar; decisao do coordenador): o ``sistema_fora``
    conta pela ultima linha de origem ``consulta``. O sistema volta a ``ativo``, um bloqueio o avisa, e a resposta ao
    aviso, de qualquer estado ou sem resposta, entra depois: a escada continua parada (critica A2)."""
    parou = [_linha(1), _linha(2, desfecho="sistema_fora")]
    assert contrato.parou_por_sistema_fora(parou)
    for estado in contrato.ESTADOS:
        depois = [*parou, _linha(3, "aviso", estado=estado)]
        assert contrato.parou_por_sistema_fora(depois)
        assert _proxima(depois) is None
    assert _proxima([*parou, _linha(3, "aviso", "sem_resposta")]) is None
    # So a consulta conta: sem consulta nenhuma, ou com a ultima consulta de outro desfecho, a escada nao parou.
    assert not contrato.parou_por_sistema_fora([])
    assert not contrato.parou_por_sistema_fora([_linha(2, "aviso", "sistema_fora")])
    assert not contrato.parou_por_sistema_fora([*parou, _linha(3, desfecho="sala_fora")])
    assert not contrato.parou_por_sistema_fora([_linha(2, desfecho="sistema_fora"), _linha(3)])


def test_o_que_diz_que_a_escrita_foi_feita_impede_o_numero_novo() -> None:
    """Critica M4.2, decisao do coordenador na conferencia final da etapa 3r: concluida e parcial (contam como
    ``aconteceu`` e ``em_parte``) impedem o numero novo, de qualquer origem; o resto nao. Nenhum deles queima o numero,
    e a lista nao se mistura com a dos que queimam."""
    assert contrato.ESTADOS_QUE_IMPEDEM_NUMERO_NOVO == ("concluida", "parcial")
    assert {contrato.CONTA_COMO[e] for e in contrato.ESTADOS_QUE_IMPEDEM_NUMERO_NOVO} == {"aconteceu", "em_parte"}
    assert not set(contrato.ESTADOS_QUE_IMPEDEM_NUMERO_NOVO) & set(contrato.ESTADOS_QUE_QUEIMAM)
    assert set(contrato.ESTADOS_QUE_IMPEDEM_NUMERO_NOVO) <= set(contrato.ESTADOS_FINAIS)


def test_a_resposta_ao_aviso_nunca_encerra_a_escada() -> None:
    """Decisao 1 do coordenador (retoque da etapa 3, D2): so recibo e consulta encerram. O ``parada`` (ou qualquer
    estado final) do aviso, sozinho ou depois do recibo em andamento, deixa a escada seguir; duas ``nao_recebida`` do
    aviso nao confirmam; a consulta seguinte encerra como sempre."""
    assert contrato.ORIGENS_DA_RESPOSTA_FINAL == ("recibo", "consulta")
    for estado in contrato.ESTADOS_FINAIS:
        so_o_aviso = [_linha(2, "aviso", estado=estado)]
        assert contrato.resposta_que_encerra(so_o_aviso) is None
        assert _proxima(so_o_aviso) is not None
    adiada = [_linha(0, "recibo", estado="em_andamento"), _linha(3, "aviso", estado="parada")]
    assert contrato.resposta_que_encerra(adiada) is None
    assert _proxima(adiada) == ENVIO + timedelta(minutes=3 + 1)
    duas_do_aviso = [_linha(2, "aviso", estado="nao_recebida"), _linha(5, "aviso", estado="nao_recebida")]
    assert contrato.resposta_que_encerra(duas_do_aviso) is None
    # Uma do aviso e uma da consulta tambem nao confirmam: so contam as do recibo e da consulta.
    assert (
        contrato.resposta_que_encerra([_linha(2, "aviso", estado="nao_recebida"), _linha(5, estado="nao_recebida")])
        is None
    )
    depois = [*adiada, _linha(5, estado="concluida")]
    assert contrato.resposta_que_encerra(depois) == "concluida"
    assert _proxima(depois) is None
    # O ultimo estado final do recibo ou da consulta vale, mesmo com um aviso depois.
    assert contrato.resposta_que_encerra(
        [_linha(2, "recibo", estado="concluida"), _linha(4, "aviso", estado="parada")]
    ) == ("concluida")


def test_degrau_conta_todas_as_linhas_de_consulta_e_o_maximo_so_as_que_gastam() -> None:
    """Decisao 3 do coordenador: o degrau sobe com toda linha de consulta (``sem_chave`` e ``sala_fora`` tambem); o
    maximo conta so as que gastam. Sem linhas que nao gastam, nada muda."""
    sem_chave = [_linha(n, desfecho="sem_chave") for n in range(9)]
    assert (contrato.tentativas_da_consulta(sem_chave), contrato.degrau_da_consulta(sem_chave)) == (0, 9)
    assert _proxima(sem_chave) == ENVIO + timedelta(minutes=8 + 240)
    assert not contrato.e_a_ultima_consulta(sem_chave, 8)
    # So as que gastam: o degrau e o de antes, linha a linha.
    for n in range(8):
        gastas = [_linha(m) for m in range(n)]
        base = n - 1 if n else 0
        assert contrato.degrau_da_consulta(gastas) == contrato.tentativas_da_consulta(gastas) == n
        esperado = ENVIO + timedelta(minutes=base + contrato.ESCADA_DA_CONSULTA_MINUTOS[n])
        assert _proxima(gastas) == max(esperado, ENVIO + timedelta(seconds=PRAZO + 60))
    # Muitas que nao gastam nao acabam a escada: com 7 que gastam, ainda vai a oitava.
    misturadas = [*[_linha(m, desfecho="sala_fora") for m in range(20)], *[_linha(20 + m) for m in range(7)]]
    assert _proxima(misturadas) == ENVIO + timedelta(minutes=26 + 240)
    assert _proxima([*misturadas, _linha(30)]) is None


def test_escada_acaba_pelo_tempo_e_sem_a_chave_tem_poucas_linhas_por_dia() -> None:
    """Decisao 3 do coordenador (a sonda SONDA_SEM_CHAVE_CONSULTA do verificador: uma linha por minuto, sem fim): com a
    chave da pessoa apagada, cada consulta grava ``sem_chave`` e a escada segue pelos degraus, 12 no primeiro dia e 6
    por dia depois, ate o fim pelo tempo (``REAPROVEITAR_NUMERO_DIAS`` dias depois do envio): 48 perguntas em 7 dias,
    em vez de 1440 por dia."""
    linhas: list[Any] = []
    for _ in range(100):  # com teto: sem o fim pelo tempo, a escada nunca devolveria None
        if (vez := _proxima(linhas)) is None:
            break
        linhas.append(contrato.LinhaInformada("consulta", "sem_chave", None, vez))
    assert len(linhas) == 48
    por_dia: dict[int, int] = {}
    for x in linhas:
        por_dia[(x.gravado_em - ENVIO).days] = por_dia.get((x.gravado_em - ENVIO).days, 0) + 1
    assert por_dia == {0: 12, 1: 6, 2: 6, 3: 6, 4: 6, 5: 6, 6: 6}
    assert linhas[-1].gravado_em < ENVIO + timedelta(days=contrato.REAPROVEITAR_NUMERO_DIAS)
    assert all(b.gravado_em - a.gravado_em == timedelta(minutes=240) for a, b in itertools.pairwise(linhas[6:]))
    # Uma escada so com respostas em aberto tambem acaba pelo tempo, sem passar do maximo.
    em_aberto = [_linha(m, estado="em_andamento") for m in range(3)]
    assert (
        contrato.proxima_consulta(em_aberto, enviado_em=ENVIO - timedelta(days=7), maximo=8, prazo_envio_segundos=PRAZO)
        is None
    )


def test_escada_vencida_no_limite_exato() -> None:
    """``escada_vencida(enviado_em, agora)``: ``agora >= enviado_em + REAPROVEITAR_NUMERO_DIAS`` dias, a janela do
    pedido igual. A assinatura e a que o G4 usa (decisao 3 do coordenador)."""
    assert contrato.REAPROVEITAR_NUMERO_DIAS == 7
    assert [p.name for p in inspect.signature(contrato.escada_vencida).parameters.values()] == ["enviado_em", "agora"]
    limite = ENVIO + timedelta(days=7)
    assert contrato.escada_vencida(ENVIO, limite)
    assert contrato.escada_vencida(ENVIO, limite + timedelta(days=30))
    assert not contrato.escada_vencida(ENVIO, limite - timedelta(microseconds=1))
    assert not contrato.escada_vencida(ENVIO, ENVIO)
    # A vez que cairia no limite exato ja nao vai; um microssegundo antes, vai.
    perto = ENVIO + timedelta(days=7) - timedelta(minutes=240)
    assert _proxima([contrato.LinhaInformada("consulta", "sem_chave", None, perto)] * 8) is None
    quase = perto - timedelta(microseconds=1)
    assert _proxima([contrato.LinhaInformada("consulta", "sem_chave", None, quase)] * 8) == quase + timedelta(
        minutes=240
    )


def test_contrato_registra_as_decisoes_do_coordenador() -> None:
    """Etapa 1: o aviso so para bloqueio, a A3.3 na rota e nas rotinas, e a conta da retirada com o despachante."""
    doc = " ".join((contrato.__doc__ or "").split())
    assert "O aviso de bloqueio vale so para bloqueio, nao para pausa" in doc
    assert "A3.3" in doc
    # A3.3 inteira: tambem no banco, na tela e na queima do numero (conferencia do G0r).
    for trecho in ("selo_estado_final", "medicoes.v_fila_agora", "envios_selo_nasce"):
        assert trecho in doc
    assert "BADGE_LOOKUP_FAILS_TO_WITHDRAW" in doc
    textos = _docstrings_das_constantes()
    aviso = " ".join((textos["ESTADOS_DO_AVISO"] or "").split())
    assert "o banco recusa o aviso de uma pausa" in aviso
    assert "tentativas`` >= 1" in aviso
    escada = " ".join((textos["ESCADA_DA_CONSULTA_MINUTOS"] or "").split())
    for trecho in ("origem ``consulta``", "de qualquer origem", "envios_selo.gravado_em", "RESPONSE_MAX_WAIT_SECONDS"):
        assert trecho in escada
    assert "Isto vale so para quando perguntar" not in escada
    assert "A mesma regra vale no banco" in escada
    rota = " ".join((textos["ROTA_CONSULTAR"] or "").split())
    assert "proxima_consulta" in rota
    assert "conta do despachante" in rota
    # Fim da escada sem resposta que encerra e uma transacao por sistema (coordenador, fim da etapa 1).
    assert "a decisao e da pessoa" in escada
    assert "o sistema nao confirmou; a decisao e sua" in escada
    assert "grava ``informacoes_sistema`` de UM sistema so, e por ultimo" in escada
    informado = " ".join((contrato.Informado.__doc__ or "").split())
    assert "nunca o botao de um toque para ``nao_recebida``, mesmo confirmada" in informado
    assert "a ficha diz que e um pedido novo para o sistema" in informado
    # O relogio da escada e o do banco (conferencia do G0r).
    proxima = " ".join((contrato.proxima_consulta.__doc__ or "").split())
    assert "``now()`` do banco" in proxima
    assert "monotonico" in proxima
    # Retoque da etapa 3: decisoes 1 a 3 do coordenador na escada e na rota.
    assert "So as respostas de origem ``recibo`` e ``consulta`` contam" in escada
    assert "degrau da espera e contado por TODAS as linhas de origem ``consulta``" in escada
    assert "``escada_vencida``" in escada
    assert '409 ``{"motivo": "porta_fechada"}``' in rota
    assert "nada foi perguntado e nada foi gravado" in rota
    assert "com a porta fechada, ou sem a chave da pessoa, o aviso espera" in aviso.lower()
    assert "a chave da pessoa foi recusada na ultima tentativa" in aviso
    assert "``aviso_bloqueio_ainda_nao_saiu``" in aviso


# --- BADGE_WARN_DAYS na politica ------------------------------------------------------------------------------


def test_aviso_do_selo_na_politica() -> None:
    aviso = POLITICA["BADGE_WARN_DAYS"]
    assert (aviso.inicial, aviso.minimo, aviso.maximo) == (14, 1, 90)
    assert aviso.inicial < POLITICA["BADGE_TTL_DAYS"].inicial
    # Menor que a validade: um aviso maior que ela sairia no dia do teste.
    assert dias_de_aviso_do_selo(90, 14) == 14
    assert dias_de_aviso_do_selo(10, 14) == 9
    assert dias_de_aviso_do_selo(1, 14) == 0
