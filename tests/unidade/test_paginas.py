"""Textos das telas do ADMIN da Fase 3 (``telas/paginas.py``), sem banco: todo desfecho, motivo, pedido e recusa de
esquema tem frase com acento, nada repete texto do servidor e nenhuma frase promete garantia. Os modelos novos do
ADMIN tambem passam pela conferencia de acento e de promessa. Do retoque (G6ar): as recusas do leitor de
formularios, a frase do 403 ``terceiros_desligados`` na autorizacao e o motivo da quarentena pela versao num lugar so
(``cadastro.quarentena``)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import pytest
from markupsafe import escape

from mcp_claro.cadastro import esquema
from mcp_claro.cadastro import quarentena as cadastro_quarentena
from mcp_claro.despachante import servico
from mcp_claro.entrada.argumentos import ESQUEMA_FUNDO_DEMAIS, EsquemaInvalido
from mcp_claro.ligacao import cliente, sala_contrato
from mcp_claro.telas import admin, admin_portas, autorizar, paginas, seguranca
from mcp_claro.telas.paginas import PASTA_MODELOS

# Palavras que, sem acento, denunciam texto de tela escrito como codigo (as telas e os documentos levam acento).
SEM_ACENTO = re.compile(
    r"\b(nao|voce|revisao|versao|versoes|conferencia|pagina|numero|codigo|sera|ja|ate|so|tambem|descricao|"
    r"referencia|definicao|definicoes|nivel|niveis|ancora|dinamica|formulario|unica|publico|invalido|valido|"
    r"situacao|sondagem|estao|ate|alem|ninguem|possivel|disponivel|ultima|ultimas|ultimo|proxima|proximo|"
    r"aplicacao|opcao|automatico|tecnico|endereco|enderecos|letras sao|campos sao|e texto)\b",
    re.IGNORECASE,
)
PROMESSA = re.compile(r"garant|100\s?%|nunca falha|sempre funciona|totalmente segur|sem risco|à prova", re.IGNORECASE)

MODELOS_DO_ADMIN = ("sistemas", "sistema", "ferramenta", "modelos", "acesso")


def _textos_de_tela() -> list[str]:
    """Todo texto de tela novo da Fase 3 do lado do ADMIN."""
    textos: list[str] = []
    for chave in (
        "servidor_revisado",
        "servidor_revisao_recusada",
        "despachante_mudo_revisao",
        "despachante_demorou_revisao",
        "despachante_demorou_sondagem",
        "ferramenta_revisada_terceiro",
    ):
        textos.append(paginas.AVISOS[chave])
    textos += list(paginas.ORIGENS.values()) + list(paginas.ORIGENS_CURTAS.values())
    textos += list(paginas.DESFECHOS_SONDAGEM.values()) + list(paginas.INALCANCAVEL_POR_MOTIVO.values())
    textos += [paginas.VERSAO_PROPRIO, paginas.VERSOES_NENHUMA, paginas.VERSAO_FORA_DO_FORMATO]
    textos += [*list(paginas.PEDIDOS_LEGIVEIS.values()), paginas.FORA_DO_PADRAO, *paginas.ONDE_O_PEDIDO.values()]
    textos += [*list(paginas.MOTIVOS_REVISAO_SERVIDOR.values()), paginas.MOTIVO_REVISAO_DESCONHECIDO]
    textos += [*list(paginas.PROBLEMAS_REVISAO.values()), paginas.PROBLEMA_DESCONHECIDO]
    textos += [paginas.ESQUEMA_FUNDO_DEMAIS] + [frase for frase, _ in _todas_as_recusas_de_esquema()]
    textos += [
        admin.ENDERECO_GERAL,
        admin.ENDERECO_NOME_INVALIDO,
        admin.ENDERECO_TERCEIRO_HTTPS,
        admin.ENDERECO_NAO_PUBLICO,
        admin.ORIGEM_FALTA,
        admin.CHAVE_DE_TERCEIRO,
        admin.PUBLICAR_FORA_DA_LISTA,
        admin.PUBLICAR_DEMAIS,
        admin.SO_TERCEIRO,
        admin.MODELO_FORA_DA_REVISAO,
        admin_portas.TERCEIRO_JA_CONFIGURADO,
        str(admin.erro_descricao_longa("campo")),
        str(admin.erro_campos_desconhecidos(["campo", "outro"])),
        admin.CAIXAS_DEMAIS,
        autorizar.MENSAGENS["terceiros_desligados"],
        seguranca.FORMATO_INESPERADO,
        seguranca.GRANDE_DEMAIS,
        seguranca.ILEGIVEL,
        seguranca.CAMPOS_DEMAIS,
    ]
    textos += [m for t, m in admin.DECLARACOES_FALHA.values()] + [t for t, m in admin.DECLARACOES_FALHA.values()]
    return textos


def _todas_as_recusas_de_esquema() -> list[tuple[str, tuple[str, str, str | None]]]:
    casos: list[tuple[str, str, str | None]] = [
        ("palavra", "properties.x", p) for p in (*esquema.PALAVRAS_RECUSADAS, *esquema.SO_NA_RAIZ)
    ]
    casos += [
        ("referencia", "properties.x", None),
        ("referencia", "properties.x", "$ref"),
        ("referencia", "$defs.aaaa...", "$defs"),
        ("referencia", "definitions.aaaa...", "definitions"),
        ("ciclo", "$defs.a", None),
        ("grande", "properties.x", None),
        ("fundo", "properties.x", None),
        ("fundo", "(raiz)", None),
        ("pesado", "(raiz)", None),
        ("escolhas", "properties.x", None),
    ]
    return [(paginas.frase_do_esquema_recusado(*caso), caso) for caso in casos]


def _texto_visivel(html: str) -> str:
    """O texto do modelo sem as marcas do Jinja e do HTML (sobra o que a pessoa le)."""
    sem_jinja = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", " ", html, flags=re.DOTALL)
    return re.sub(r"<[^>]+>", " ", sem_jinja)


# --- Acento e promessa ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("texto", _textos_de_tela())
def test_texto_de_tela_novo_tem_acento_e_nao_promete(texto: str) -> None:
    sem_campos = re.sub(r"\{[a-z_]+\}", "", texto)  # {versoes} e o nome do campo, nao texto
    assert not SEM_ACENTO.search(sem_campos), texto
    assert not PROMESSA.search(texto), texto
    assert texto == texto.strip()
    assert texto


@pytest.mark.parametrize("modelo", MODELOS_DO_ADMIN)
def test_modelos_do_admin_com_acento_e_sem_promessa(modelo: str) -> None:
    visivel = _texto_visivel((PASTA_MODELOS / "admin" / f"{modelo}.html").read_text(encoding="utf-8"))
    achados = [m.group(0) for m in SEM_ACENTO.finditer(visivel)]
    assert not achados, (modelo, achados)
    assert not PROMESSA.search(visivel), modelo


def test_conferencia_de_acento_pega_texto_sem_acento() -> None:
    # A conferencia acima precisa achar o erro que ela procura.
    assert SEM_ACENTO.search("O servidor nao respondeu a conferencia.")
    assert PROMESSA.search("O MCP-CLARO garante que nada passa.")
    assert SEM_ACENTO.search(_texto_visivel("<p>{{ x }} Revisao do servidor</p>"))


# --- Cada desfecho, motivo, problema e pedido tem texto ----------------------------------------------------------


@pytest.mark.parametrize("desfecho", get_args(cliente.Desfecho))
def test_todo_desfecho_da_sondagem_tem_frase(desfecho: str) -> None:
    assert desfecho in paginas.DESFECHOS_SONDAGEM
    for origem in ("terceiro", "proprio"):
        frase = paginas.frase_da_sondagem(desfecho, None, origem=origem, versoes=["2025-06-18"])
        assert frase
        assert "{" not in frase


def test_frase_da_sondagem_por_versao_e_motivo() -> None:
    terceiro = paginas.frase_da_sondagem("versao", "qualquer", origem="terceiro", versoes=["2025-06-18", "outra"])
    assert terceiro is not None
    assert terceiro.startswith("Este servidor de terceiros foi recusado.")
    assert "a única que o MCP-CLARO aceita" in terceiro
    assert "Ele anunciou: 2025-06-18, uma versão escrita fora do formato AAAA-MM-DD." in terceiro
    assert "A recusa entrou na contagem de versões." in terceiro
    sem_versoes = paginas.frase_da_sondagem("versao", None, origem="terceiro", versoes=[])
    assert sem_versoes is not None
    assert paginas.VERSOES_NENHUMA in sem_versoes
    proprio = paginas.frase_da_sondagem("versao", None, origem="proprio", versoes=["2025-03-26"])
    assert proprio == "Ele anunciou: 2025-03-26. Peça à equipe do sistema para atualizar o kit MCP. Depois, peça a" + (
        " conferência de novo."
    )
    assert paginas.frase_da_sondagem("desvio", "motivo cru", origem="terceiro") == paginas.DESFECHOS_SONDAGEM["desvio"]
    assert paginas.frase_da_sondagem("nao_mcp", None, origem="terceiro") == (
        "Este endereço não respondeu como um servidor MCP. Confira o endereço."
    )
    assert paginas.frase_da_sondagem("sem_acesso", None, origem="terceiro") == (
        "O servidor pediu uma chave. Informe a chave de teste e confira de novo."
    )
    for motivo, frase in paginas.INALCANCAVEL_POR_MOTIVO.items():
        assert paginas.frase_da_sondagem("inalcancavel", motivo, origem="terceiro") == frase
    assert cliente.MOTIVO_ENDERECO_RECUSADO in paginas.INALCANCAVEL_POR_MOTIVO
    assert sala_contrato.MOTIVO_SALA_FORA in paginas.INALCANCAVEL_POR_MOTIVO
    assert paginas.frase_da_sondagem(None, "motivo cru", origem="terceiro") is None
    # O motivo tecnico cru nunca vira a frase.
    for desfecho in get_args(cliente.Desfecho):
        frase = paginas.frase_da_sondagem(desfecho, "HTTP 307 motivo cru", origem="terceiro")
        assert frase is not None
        assert "motivo cru" not in frase


# Nomes ``PEDIDO_*`` de ``ligacao.cliente`` que sao nomes de excecao, nao categorias de pedido do sistema.
_NAO_SAO_CATEGORIA = ("PEDIDO_A_MAIS", "PEDIDO_FUNDO_DEMAIS")


def _categorias_de_pedido() -> list[str]:
    """Todas as categorias do vocabulario do cliente, achadas no modulo: uma categoria nova sem texto falha aqui."""
    simples = {
        valor
        for nome, valor in vars(cliente).items()
        if nome.startswith("PEDIDO_") and nome not in _NAO_SAO_CATEGORIA and isinstance(valor, str)
    }
    fio = {cliente.PREFIXO_FIO_ANTIGO + m for m in (*cliente.METODOS_DO_SISTEMA, cliente.DESCONHECIDO)}
    return sorted(simples | fio)


@pytest.mark.parametrize("categoria", _categorias_de_pedido())
def test_toda_categoria_de_pedido_tem_texto_proprio(categoria: str) -> None:
    assert categoria in paginas.PEDIDOS_LEGIVEIS
    assert paginas.pedido_legivel(categoria) != paginas.FORA_DO_PADRAO


def test_categorias_de_pedido_cobrem_o_vocabulario_do_cliente() -> None:
    esperadas = {
        cliente.PEDIDO_FORMULARIO,
        cliente.PEDIDO_LINK,
        cliente.PEDIDO_AMOSTRAGEM,
        cliente.PEDIDO_RAIZES,
        cliente.PEDIDO_SO_ESTADO,
        cliente.PEDIDO_LINK_CODIGO_ANTIGO,
    } | {cliente.PREFIXO_FIO_ANTIGO + m for m in (*cliente.METODOS_DO_SISTEMA, cliente.DESCONHECIDO)}
    assert esperadas <= set(_categorias_de_pedido())
    assert set(paginas.PEDIDOS_LEGIVEIS) == esperadas


@pytest.mark.parametrize("vindo", ["https://phish.exemplo/login", "Digite sua senha", "fio_antigo:x", "", None, 3])
def test_pedido_desconhecido_nunca_repete_o_que_veio(vindo: Any) -> None:
    assert paginas.pedido_legivel(vindo) == paginas.FORA_DO_PADRAO
    assert paginas.pedidos_legiveis([vindo]) == paginas.FORA_DO_PADRAO


def test_pedidos_legiveis_juntam_sem_repetir() -> None:
    assert paginas.pedidos_legiveis(None) == paginas.FORA_DO_PADRAO
    assert paginas.pedidos_legiveis(["link", "link_codigo_antigo", "link"]) == (
        "que a pessoa abrisse um link e que a pessoa abrisse um link (pelo código de erro antigo)"
    )
    assert paginas.pedidos_legiveis(["formulario", "raizes", "amostragem"]) == (
        "dados por formulário, a lista de pastas do aplicativo e uma resposta da IA do aplicativo"
    )


def _motivos_da_revisao_no_despachante() -> set[str]:
    fonte = Path(servico.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'(?:\brecusar|RevisaoRecusada)\(\s*"([a-z_]+)"', fonte))


def test_todo_motivo_da_revisao_do_servidor_tem_frase() -> None:
    motivos = _motivos_da_revisao_no_despachante()
    contrato = {
        "nao_e_terceiro",
        "fornecedor",
        "publicadas_demais",
        "situacao",
        "versao",
        "chave_indisponivel",
        "sem_chave_declaracoes",
        "sem_resposta",
        "situacao_mudou",
        "ferramentas",
    }
    assert contrato <= motivos  # a busca no codigo acha os motivos do contrato
    for motivo in motivos | {"pedido_invalido"}:
        assert motivo in paginas.MOTIVOS_REVISAO_SERVIDOR, motivo
        assert paginas.frase_da_revisao_recusada(motivo) != paginas.MOTIVO_REVISAO_DESCONHECIDO
    assert paginas.frase_da_revisao_recusada("sem_chave_declaracoes").startswith(
        "Guarde a chave de leitura das declarações deste servidor antes de concluir a revisão."
    )
    assert "sala de visitas" in paginas.frase_da_revisao_recusada("sem_resposta")
    assert paginas.frase_da_revisao_recusada("outro") == paginas.MOTIVO_REVISAO_DESCONHECIDO


def test_todo_problema_de_ferramenta_da_recusa_tem_frase() -> None:
    assert set(servico._PROBLEMAS) == set(paginas.PROBLEMAS_REVISAO)


def test_avisos_novos_da_revisao_do_servidor() -> None:
    assert paginas.AVISOS["servidor_revisado"] == (
        "Pronto. A revisão do servidor foi registrada. As ferramentas marcadas aparecem nas vitrines, dentro das"
        " ordens de serviço."
    )
    assert paginas.AVISOS["servidor_revisao_recusada"] == (
        "A revisão do servidor não foi concluída. Veja abaixo o que mudou e revise de novo."
    )
    assert paginas.AVISOS["despachante_mudo_revisao"] == (
        "O despachante não respondeu. Nada mudou. Tente de novo daqui a pouco."
    )


# --- Recusa do esquema de terceiro -----------------------------------------------------------------------------


@pytest.mark.parametrize("motivo", get_args(esquema.MotivoEsquema))
def test_todo_motivo_de_esquema_recusado_tem_frase_propria(motivo: str) -> None:
    frases = [f for f, (m, _, _) in _todas_as_recusas_de_esquema() if m == motivo]
    assert frases
    for frase in frases:
        assert frase.endswith(".")
        assert "não é aceito num servidor de terceiros" not in frase  # nunca a frase de reserva


def test_frases_do_esquema_pelo_lugar_e_pela_palavra() -> None:
    raiz = paginas.frase_do_esquema_recusado("palavra", "(raiz)", "$id")
    assert raiz == (
        "O esquema deste servidor usa identificador de parte ($id) na raiz do esquema. Num servidor de terceiros, o"
        " MCP-CLARO não aceita isso. Peça ao fornecedor um esquema sem essa palavra."
    )
    assert "em properties.x" in paginas.frase_do_esquema_recusado("palavra", "properties.x", "$anchor")
    assert "âncora ($anchor)" in paginas.frase_do_esquema_recusado("palavra", "properties.x", "$anchor")
    assert paginas.frase_do_esquema_recusado("palavra", "properties.x", "$schema").startswith(
        "O esquema deste servidor troca a versão do JSON Schema ($schema) em properties.x."
    )
    assert "tem mais de 256 letras" in paginas.frase_do_esquema_recusado("referencia", "properties.x", "$ref")
    assert paginas.frase_do_esquema_recusado("referencia", "$defs.aaa...", "$defs").startswith(
        "O nome de uma definição ($defs) em $defs.aaa... tem mais de 256 letras."
    )
    assert "não aponta para uma definição" in paginas.frase_do_esquema_recusado("referencia", "x", None)
    assert paginas.frase_do_esquema_recusado("fundo", "(raiz)", None) == (
        "O esquema tem partes demais umas dentro das outras para o MCP-CLARO conferir. Peça ao fornecedor um esquema"
        " mais simples."
    )
    assert "mais de 64 níveis" in paginas.frase_do_esquema_recusado("fundo", "properties.x", None)
    assert "mais de 2.000 partes" in paginas.frase_do_esquema_recusado("grande", "properties.x", None)
    assert "256 KiB (262.144 bytes)" in paginas.frase_do_esquema_recusado("pesado", "(raiz)", None)
    assert "mais de 1.000 valores" in paginas.frase_do_esquema_recusado("escolhas", "properties.x", None)


def _recusa_de_verdade(declarado: dict[str, Any]) -> esquema.EsquemaRecusado:
    with pytest.raises(esquema.EsquemaRecusado) as erro:
        esquema.conferir_terceiro(esquema.limpar_textos(declarado))
    return erro.value


@pytest.mark.parametrize(
    ("declarado", "trecho"),
    [
        ({"type": "object", "patternProperties": {"^a": {}}}, "regra de formato nos nomes de campo"),
        ({"type": "object", "properties": {"x": {"$ref": "#/fora"}}}, "não aponta para uma definição"),
        (
            {"type": "object", "properties": {"x": {"$ref": "#/$defs/a"}}, "$defs": {"a": {"$ref": "#/$defs/a"}}},
            "ciclo",
        ),
        ({"type": "object", "properties": {"x": {"enum": list(range(1001))}}}, "mais de 1.000 valores"),
    ],
)
def test_frase_para_recusas_de_verdade_do_esquema(declarado: dict[str, Any], trecho: str) -> None:
    erro = _recusa_de_verdade(declarado)
    frase = admin.frase_do_esquema(erro)
    assert trecho in frase
    assert str(erro) not in frase  # a frase da tela, com acento, no lugar da mensagem sem acento


def test_frase_do_esquema_fundo_demais_e_da_reserva() -> None:
    assert admin.frase_do_esquema(EsquemaInvalido(ESQUEMA_FUNDO_DEMAIS)) == paginas.ESQUEMA_FUNDO_DEMAIS
    # A mensagem da conferencia do esquema pode trazer nomes do fornecedor: vai dentro de "estranho", escapada.
    assert admin.frase_do_esquema(EsquemaInvalido("x")) == (
        'O esquema de entrada desta ferramenta não é aceito: <span class="estranho">x</span>.'
    )
    assert admin.frase_do_esquema(EsquemaInvalido("<b>\u202e")) == (
        'O esquema de entrada desta ferramenta não é aceito: <span class="estranho">&lt;b&gt;[U+202E]</span>.'
    )


@pytest.mark.parametrize(("frase", "caso"), _todas_as_recusas_de_esquema())
def test_frase_do_esquema_na_tela_e_a_mesma_com_o_caminho_em_estranho(frase: str, caso: tuple[str, str, Any]) -> None:
    motivo, onde, palavra = caso
    na_tela = paginas.frase_do_esquema_recusado_na_tela(motivo, onde, palavra)
    lugar = "" if onde == "(raiz)" else f'<code class="estranho">{onde}</code>'
    if lugar:
        assert lugar in na_tela
        assert str(na_tela).replace(lugar, onde) == str(escape(frase))
    else:
        assert str(na_tela) == str(escape(frase))
    assert "<nome>" not in na_tela  # o texto nosso tambem e escapado ("#/$defs/<nome>")


def test_caminho_da_recusa_com_escondidos_e_tags_fica_escapado() -> None:
    na_tela = paginas.frase_do_esquema_recusado_na_tela("ciclo", "properties.<b>a\u202e</b>", None)
    assert '<code class="estranho">properties.&lt;b&gt;a[U+202E]&lt;/b&gt;</code>' in na_tela
    assert "<b>" not in na_tela
    assert "\u202e" not in na_tela
    erro = esquema.EsquemaRecusado("x", onde="properties.<i>c</i>", motivo="escolhas")
    assert '<code class="estranho">properties.&lt;i&gt;c&lt;/i&gt;</code>' in admin.frase_do_esquema(erro)


def test_motivo_da_quarentena_pela_versao_fica_so_no_cadastro() -> None:
    """O despachante grava e as telas leem o mesmo objeto, de ``cadastro.quarentena``: nenhuma copia do texto."""
    from mcp_claro.telas import admin as telas_admin

    assert vars(telas_admin)["MOTIVO_QUARENTENA_VERSAO"] is cadastro_quarentena.MOTIVO_QUARENTENA_VERSAO
    assert vars(servico)["MOTIVO_QUARENTENA_VERSAO"] is cadastro_quarentena.MOTIVO_QUARENTENA_VERSAO
    trecho = "deixou de falar a versao 2026-07-28 na sondagem"
    assert trecho in cadastro_quarentena.MOTIVO_QUARENTENA_VERSAO
    for modulo in (telas_admin, servico):
        assert trecho not in Path(str(modulo.__file__)).read_text(encoding="utf-8"), modulo.__name__


def test_textos_novos_do_retoque() -> None:
    assert paginas.AVISOS["ferramenta_revisada_terceiro"] == (
        "Pronto. A revisão foi gravada. Num servidor de terceiros, a ferramenta só aparece nas vitrines e nos modelos"
        " de ordem depois que você concluir a revisão do servidor."
    )
    assert paginas.AVISOS["ferramenta_revisada"] == (
        "Pronto. A revisão foi gravada. A ferramenta já pode entrar em modelos de ordem."
    )
    assert admin.MODELO_FORA_DA_REVISAO == (
        "Esta ferramenta é de um servidor de terceiros e não foi publicada na revisão do servidor."
    )
    assert autorizar.MENSAGENS["terceiros_desligados"] == (
        "Servidores de terceiros ainda estão desligados neste MCP-CLARO. Só pessoas de teste os autorizam. Nada foi"
        " guardado."
    )
    assert admin.CAIXAS_DEMAIS == "Marque até 500 ferramentas para a vitrine. Nada mudou."


def test_onde_o_pedido_cobre_toda_linha_de_pedido_barrado() -> None:
    from mcp_claro.telas import consultas_terceiros

    assert set(paginas.ONDE_O_PEDIDO) == {*consultas_terceiros.TIPOS_PEDIDO_BARRADO, "acao_enviada"}
