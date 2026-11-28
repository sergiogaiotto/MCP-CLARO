"""Textos das telas do ADMIN da Fase 4 (selos), sem banco: todo codigo do contrato tem texto, todo texto tem acento,
nenhum promete (so "não garante", na negacao), nenhum traz o nome nem os IDs do rascunho independente (critica B6) e
as recusas do banco que a tela traduz ainda existem na migracao 0007."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from markupsafe import Markup, escape

from mcp_claro.selo import contrato, vigente
from mcp_claro.selo import declaracao as declaracao_do_selo
from mcp_claro.telas import admin, paginas
from mcp_claro.telas.paginas import PASTA_MODELOS

RAIZ = Path(__file__).resolve().parents[2]
MIGRACAO = RAIZ / "migrations" / "versions" / "0007_fase4.py"

# A mesma lista de palavras de test_paginas: sem acento, elas denunciam texto de tela escrito como codigo.
SEM_ACENTO = re.compile(
    r"\b(nao|voce|revisao|versao|versoes|conferencia|pagina|numero|numeros|codigo|sera|ja|ate|so|tambem|descricao|"
    r"declaracao|definicao|nivel|niveis|formulario|unica|publico|situacao|estao|alem|ninguem|possivel|disponivel|"
    r"ultima|ultimo|proxima|proximo|automatico|endereco|enderecos|chamadas sao|producao|acao|acoes|maximo)\b",
    re.IGNORECASE,
)
PROMESSA = re.compile(r"100\s?%|nunca falha|sempre funciona|totalmente segur|sem risco|à prova", re.IGNORECASE)
PROIBIDAS = re.compile(r"certificad|conforme|aprovado no|MWS|write-safety|write safety", re.IGNORECASE)


def _garante_so_negado(texto: str) -> bool:
    return all(texto[max(0, m.start() - 4) : m.start()] == "não " for m in re.finditer(r"garant", texto))


def _textos() -> list[str]:
    textos = [
        admin.SELO_NAO_GARANTE,
        admin.SELO_RODAPE,
        admin.SELO_CONFIRMO,
        admin.SELO_NOTA_DO_ALVO,
        admin.SELO_ATENCAO_DO_ALVO,
        admin.SELO_PEDE_LOGIN,
        admin.SELO_LOGIN_REFEITO,
        admin.SELO_SEM_CONFIRMO,
        admin.SELO_SEM_DECLARACAO,
        admin.SELO_TESTE_JA_RODANDO,
        admin.SELO_NIVEL_FORA,
        admin.SELO_NADA_A_RETIRAR,
        admin.SELO_MOTIVO_DA_RETIRADA,
        admin.SELO_SO_POR_PESSOA,
        admin.SELO_NAO_LIDO,
        admin.SELO_RECUSA_DO_BANCO,
        admin.SELO_DECLARACAO_SALVA_SEM_VERSAO,
        admin.ESCOPO_SISTEMA,
    ]
    textos += list(admin.AVISOS_ADMIN.values()) + list(admin.TEXTOS_DO_CARTAO.values())
    textos += [frase for _, frase in admin.RECUSAS_DO_BANCO]
    for tabela in (
        admin.RESULTADOS_LEGIVEIS,
        admin.RAZOES_LEGIVEIS,
        admin.INTERRUPCOES_LEGIVEIS,
        admin.MODOS_LEGIVEIS,
        admin.ESTADOS_DO_PEDIDO_LEGIVEIS,
    ):
        textos += list(tabela.values())
    return textos


@pytest.mark.parametrize("texto", _textos())
def test_texto_do_selo_tem_acento_nao_promete_e_nao_cita_o_rascunho(texto: str) -> None:
    sem_campos = re.sub(r"\{[a-z_]+\}", "", texto)
    assert not SEM_ACENTO.search(sem_campos), texto
    assert not PROMESSA.search(texto), texto
    assert not PROIBIDAS.search(texto), texto
    assert _garante_so_negado(texto), texto
    assert texto == texto.strip()
    assert texto


def test_a_conferencia_de_texto_pega_o_que_procura() -> None:
    assert SEM_ACENTO.search("O teste de nivel 2 nao comecou.")
    assert PROIBIDAS.search("Aprovado no MWS-RETRY-2.")
    assert not _garante_so_negado("O MCP-CLARO garante o resultado.")
    assert _garante_so_negado(admin.SELO_NAO_GARANTE)


def test_todo_codigo_do_contrato_tem_texto_na_tela() -> None:
    assert set(admin.RESULTADOS_LEGIVEIS) == set(contrato.RESULTADOS)
    assert set(admin.RAZOES_LEGIVEIS) == set(contrato.RAZOES)
    assert set(admin.INTERRUPCOES_LEGIVEIS) == set(contrato.MOTIVOS_DE_INTERRUPCAO)
    assert set(admin.MODOS_LEGIVEIS) == set(contrato.MODOS)
    assert set(admin.ESTADOS_DO_PEDIDO_LEGIVEIS) == set(contrato.ESTADOS_DO_PEDIDO)
    assert admin.RESULTADOS_LEGIVEIS == {
        "cumpriu": "Cumpriu",
        "falhou": "Falhou",
        "nao_se_aplica": "Não se aplica",
        "nao_testado": "Não testado",
        "nao_observado": "Não observado",
    }  # as palavras do desenho 7.1


def test_as_frases_fixas_do_desenho() -> None:
    assert admin.SELO_RODAPE == (
        "Este relatório é do MCP-CLARO. Alguns casos adaptam requisitos de um rascunho independente de escrita"
        " segura; veja o contrato. Ele não é uma declaração de conformidade desse rascunho."
    )
    assert admin.SELO_CONFIRMO == "Confirmo que o alvo declarado é de teste: a bateria vai fazer escritas reais nele."
    assert admin.SELO_NOTA_DO_ALVO.startswith(
        "O MCP-CLARO só escreve no alvo que você declarou e que se declara de teste"
    )
    assert "Nunca chama" not in admin.SELO_NOTA_DO_ALVO  # critica A4.6: so com as condicoes ditas
    assert admin.TEXTOS_DO_CARTAO["terceiro"] == declaracao_do_selo.TERCEIRO_AINDA_NAO
    assert admin.SELO_TESTE_JA_RODANDO == "Já há um teste deste sistema rodando. Espere o resultado."
    assert admin.SELO_SEM_DECLARACAO == "Salve a declaração de teste e a chave de teste antes de pedir o teste."


def test_aviso_da_pagina_procura_no_admin_e_depois_nas_paginas() -> None:
    assert admin.aviso_da_pagina("selo_declaracao_salva", versao=3) == (
        "Pronto. A declaração de teste foi salva (versão 3). O teste usa sempre a mais nova."
    )
    assert admin.aviso_da_pagina("selo_declaracao_salva") == admin.SELO_DECLARACAO_SALVA_SEM_VERSAO
    assert admin.aviso_da_pagina("selo_teste_pedido") == admin.AVISOS_ADMIN["selo_teste_pedido"]
    assert admin.aviso_da_pagina("declaracoes_lidas") == paginas.AVISOS["declaracoes_lidas"]
    assert admin.aviso_da_pagina("nao_existe") is None
    for codigo, texto in admin.AVISOS_ADMIN.items():  # o do ADMIN vale primeiro, mesmo se paginas tiver o codigo
        if codigo != "selo_declaracao_salva":
            assert admin.aviso_da_pagina(codigo) == texto


def test_o_que_nao_testamos_leva_a_guarda_declarada() -> None:
    guarda = next(n for n in contrato.NAO_TESTADOS if n.item == "guarda_dos_numeros")
    outro = next(n for n in contrato.NAO_TESTADOS if n.item != "guarda_dos_numeros")
    assert (
        admin.com_a_guarda(guarda, 45)
        == "Por quanto tempo o sistema guarda os números (declarado pelo sistema: 45 dias)."
    )
    assert admin.com_a_guarda(guarda, None) == guarda.texto
    assert admin.com_a_guarda(guarda, True) == guarda.texto
    assert admin.com_a_guarda(outro, 45) == outro.texto
    assert admin.dias_legiveis(1) == "1 dia"
    assert admin.dias_legiveis(7) == "7 dias"


def test_casos_que_nao_aprovam_conta_falhas_e_obrigatorios_sem_aprovar() -> None:
    f = "11111111-1111-1111-1111-111111111111"
    itens = [
        {"caso": "C-2.1", "ferramenta": f, "resultado": "falhou"},
        {"caso": "C-2.2a", "ferramenta": f, "resultado": "nao_testado"},  # obrigatorio sem aprovar
        {"caso": "C-2.2b", "ferramenta": f, "resultado": "nao_se_aplica"},  # admite nao_se_aplica
        {"caso": "C-2.2d", "ferramenta": f, "resultado": "nao_testado"},  # informativo: nao conta
        {"caso": "C-2.3", "ferramenta": f, "resultado": "nao_observado"},  # o maximo do metodo
        {"caso": "C-2.4b", "ferramenta": f, "resultado": "nao_se_aplica"},  # admite so no nivel 2
        {"caso": "C-9.9", "ferramenta": f, "resultado": "falhou"},  # fora do contrato: ignorado
    ]
    assert admin.casos_que_nao_aprovam(itens, 2) == 2
    assert admin.casos_que_nao_aprovam(itens, 3) == 3
    assert admin.casos_que_nao_aprovam("nao e lista", 2) == 0


def test_toda_recusa_traduzida_ainda_existe_no_banco() -> None:
    """Se a 0007 mudar a mensagem de um gatilho, a tela volta a frase geral: este teste avisa antes."""
    fonte = MIGRACAO.read_text(encoding="utf-8")
    for trecho, _ in admin.RECUSAS_DO_BANCO:
        assert trecho in fonte, trecho
    assert admin.recusa_do_banco(Exception("x a bateria nao escreve na producao y")) == admin.RECUSAS_DO_BANCO[0][1]
    assert admin.recusa_do_banco(Exception("outra coisa")) == admin.SELO_RECUSA_DO_BANCO


@pytest.mark.parametrize("modelo", ["sistema", "sistemas", "selo_teste"])
def test_modelos_do_selo_com_acento_sem_promessa_e_sem_rascunho(modelo: str) -> None:
    bruto = (PASTA_MODELOS / "admin" / f"{modelo}.html").read_text(encoding="utf-8")
    sem_jinja = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", " ", bruto, flags=re.DOTALL)
    visivel = re.sub(r"<[^>]+>", " ", sem_jinja)
    assert not SEM_ACENTO.search(visivel), (modelo, SEM_ACENTO.search(visivel))
    assert not PROMESSA.search(visivel), modelo
    assert not re.search(r"garant", visivel, re.IGNORECASE), modelo  # a frase do selo vem pronta (SELO_NAO_GARANTE)
    assert not PROIBIDAS.search(bruto), modelo


def test_reprovado_conta_como_o_relatorio_e_nunca_diz_zero() -> None:
    assert admin.reprovado_legivel(1) == "O último teste não foi aprovado: 1 caso falhou ou ficou sem teste."
    assert admin.reprovado_legivel(3) == "O último teste não foi aprovado: 3 casos falharam ou ficaram sem teste."
    assert admin.reprovado_legivel(0) == "O último teste não foi aprovado."
    for n in (0, 1, 3):
        texto = admin.reprovado_legivel(n)
        assert not SEM_ACENTO.search(texto)
        assert not PROIBIDAS.search(texto)


_TESTE = uuid.UUID("22222222-2222-2222-2222-222222222222")
_DIA = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


def test_selo_agora_marca_os_nomes_do_sistema_e_conta_o_reprovado() -> None:
    nova = vigente.SeloVigente(1, _TESTE, _DIA, _DIA, "ambiente_de_teste", "ferramenta_nova")
    um = admin.selo_agora_na_tela(nova, vigente.Detalhes(ferramentas=("a\u202eb",)), fuso="America/Sao_Paulo")
    assert isinstance(um, Markup)
    assert str(um) == (
        'Selo agora: nível 1. Voltou ao nível 1: a ferramenta <code class="estranho">a[U+202E]b</code> é nova ou'
        " mudou depois do teste."
    )
    dois = admin.selo_agora_na_tela(nova, vigente.Detalhes(ferramentas=("<x>", "y&z")), fuso="America/Sao_Paulo")
    assert str(dois) == (
        'Selo agora: nível 1. Voltou ao nível 1: as ferramentas <code class="estranho">&lt;x&gt;</code>,'
        ' <code class="estranho">y&amp;z</code> são novas ou mudaram depois do teste.'
    )
    sem_nome = admin.selo_agora_na_tela(nova, vigente.Detalhes(), fuso="America/Sao_Paulo")
    assert str(sem_nome) == "Selo agora: nível 1. " + vigente.MOTIVOS_LEGIVEIS["ferramenta_nova"]

    reprovado = vigente.SeloVigente(1, _TESTE, _DIA, _DIA, "ambiente_de_teste", "reprovado")
    texto = admin.selo_agora_na_tela(reprovado, vigente.Detalhes(falhas=0), fuso="America/Sao_Paulo", reprovados=2)
    assert str(texto) == "Selo agora: nível 1. O último teste não foi aprovado: 2 casos falharam ou ficaram sem teste."

    vale = vigente.SeloVigente(2, _TESTE, _DIA, _DIA, "ambiente_de_teste", "vigente")
    assert str(admin.selo_agora_na_tela(vale, vigente.Detalhes(), fuso="UTC")) == str(
        escape(vigente.selo_agora_legivel(vale, vigente.Detalhes(), fuso="UTC"))
    )


def test_a_trava_da_retirada_e_a_das_respostas_do_sistema_na_0007() -> None:
    """A retirada pelo ADMIN toma a mesma trava que ``informacoes_sistema_nasce``: se a 0007 mudar o nome ou a chave,
    as duas deixam de fazer fila, e este teste avisa."""
    fonte = MIGRACAO.read_text(encoding="utf-8")
    assert f"pg_advisory_xact_lock(hashtext('{admin.TRAVA_DAS_INFORMACOES}'), hashtext(NEW.sistema_id::text))" in fonte
