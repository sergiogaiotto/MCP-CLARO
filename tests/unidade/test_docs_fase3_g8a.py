"""Documentos da Fase 3 (G8a): decisoes.md, andamento.md e saida.md conferidos contra o codigo.

O que se prova, sem banco:

- as secoes da Fase 3 existem (decisoes D1 a D33, "Mudancas deliberadas da Fase 3", os riscos aceitos, a Fase 3 no
  andamento e as acoes de terceiros na porta de saida), e a recusa da partida (D6) aponta para uma secao que existe;
- cada numero e cada frase que os documentos repetem do codigo bate com o codigo de agora: se um teto, um prazo ou
  uma frase mudar no codigo, este teste falha ate o documento mudar junto;
- o que os documentos dizem que o codigo faz (a ordem das chamadas na acao e na leitura, os contadores da quarentena,
  o nome e a versao mandados a um terceiro, o escopo do Landlock) e conferido no codigo de agora; o que so foi
  conferido numa maquina (a versao do Landlock) diz onde e quando, e o que nao foi conferido diz isso;
- cada arquivo e cada teste citado como prova existe, e cada link entre documentos acha a secao;
- texto para leigo: com acento fora das citacoes de codigo, sem emoji e sem promessa de garantia.

Dos acertos finais (etapa 5f): o Ubuntu, o Yama, a versao do Landlock e o nucleo 6.18 so aparecem nas frases
conferidas do item do Landlock (lista branca, nao lista negra); a frase "o mudou-parou nao conta essa rodada" com a sala
fora e conferida pelo caminho inteiro no codigo (despachante, rotinas e mudou-parou). No retoque, a versao do Landlock
conta escrita de qualquer jeito ("Landlock" e um numero na mesma frase), frase a frase.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx2
import mcp_types as types
import pytest

from mcp_claro import __version__
from mcp_claro.cadastro import esquema
from mcp_claro.cadastro.modelos import FIM_DA_DESCRICAO_TERCEIRO, FerramentaVitrine, Sistema
from mcp_claro.cadastro.quarentena import MOTIVO_PARADA
from mcp_claro.config import politica
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import autorizacao, renovacao, servico
from mcp_claro.entrada import argumentos, servidor
from mcp_claro.extrato.frases import AVISOS_ADMIN
from mcp_claro.ligacao import cliente, ponto, sala_cliente, sala_contrato
from mcp_claro.logs import LOGGERS_DO_KIT
from mcp_claro.regras import programas, sinais
from mcp_claro.rotinas import despachante as rotinas_despachante
from mcp_claro.rotinas import mudou_parou
from mcp_claro.saida import despachante as porta
from mcp_claro.saida import pedidos
from mcp_claro.saida import quarentena as quarentena_da_porta
from mcp_claro.telas import autorizar, paginas
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from mcp_claro.visitas import app as sala
from mcp_claro.visitas import filho

RAIZ = Path(__file__).resolve().parents[2]
DOCS = RAIZ / "docs"
NOMES = ("decisoes.md", "andamento.md", "saida.md")


def ler(nome: str) -> str:
    return (DOCS / nome).read_text(encoding="utf-8")


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def junto(texto: str) -> str:
    """O texto numa linha so, com um espaco entre as palavras (as frases quebram linha no documento)."""
    return " ".join(texto.split())


def secao(texto: str, titulo: str) -> str:
    """Do titulo (``## ...`` ou ``### ...``) ate o proximo titulo do mesmo nivel ou de cima."""
    nivel = titulo.split(" ", 1)[0]
    linhas = texto.splitlines()
    inicio = linhas.index(titulo)
    fim = len(linhas)
    for i in range(inicio + 1, len(linhas)):
        marca = linhas[i].split(" ", 1)[0]
        if set(marca) == {"#"} and len(marca) <= len(nivel):
            fim = i
            break
    return "\n".join(linhas[inicio:fim])


def tem(nome: str, trecho: str, *, acento: bool = True) -> bool:
    """O documento traz o trecho (quebras de linha contam como espaco; sem acento, compara sem os acentos)."""
    doc = junto(ler(nome))
    alvo = junto(trecho)
    return alvo in doc if acento else sem_acento(alvo) in sem_acento(doc)


# Secoes ----------------------------------------------------------------------------------------------------------

SECOES = (
    ("decisoes.md", "## Decisões da Fase 3: servidores de terceiros (21/11/2026)"),
    ("decisoes.md", "## Mudanças deliberadas da Fase 3 (21/11/2026)"),
    ("decisoes.md", "### Riscos aceitos da Fase 3"),
    ("andamento.md", "## Fase 3: servidores de terceiros"),
    ("saida.md", "## Ações em servidores de terceiros"),
)


@pytest.mark.parametrize(("nome", "titulo"), SECOES)
def test_as_secoes_da_fase_3_existem(nome: str, titulo: str) -> None:
    assert titulo in ler(nome).splitlines()


def test_as_decisoes_d1_a_d33_estao_na_tabela_uma_vez_cada() -> None:
    tabela = secao(ler("decisoes.md"), "## Decisões da Fase 3: servidores de terceiros (21/11/2026)")
    for n in range(1, 34):
        assert len(re.findall(rf"^\| D{n}\. ", tabela, re.MULTILINE)) == 1, f"D{n}"
    assert not re.search(r"^\| D34\. ", tabela, re.MULTILINE)


def test_d1_tem_a_frase_corrigida_pela_critica_b8() -> None:
    d1 = next(linha for linha in ler("decisoes.md").splitlines() if linha.startswith("| D1. "))
    assert (
        "O despachante fala com os sistemas próprios, com a sala e com o endereço de troca de chave (OAuth) de cada"
        " terceiro; só o despachante abre as chaves" in d1
    )


def _problemas(*, ligada: bool) -> list[str]:
    return Parametros(FEATURE_PHASE1_READ=True, FEATURE_PHASE2_ACT=True, FEATURE_PHASE3_THIRD_PARTY=ligada).problemas()


def test_a_recusa_da_partida_aponta_para_a_secao_que_existe() -> None:
    # D6: a frase da partida manda ler "decisoes.md, Mudancas deliberadas da Fase 3".
    frase = next(p for p in _problemas(ligada=True) if p.startswith("FEATURE_PHASE3_THIRD_PARTY ainda nao liga"))
    assert "decisoes.md, Mudancas deliberadas da Fase 3" in frase
    titulos = [sem_acento(linha) for linha in ler("decisoes.md").splitlines() if linha.startswith("## ")]
    assert any(t.startswith("## Mudancas deliberadas da Fase 3") for t in titulos)


def test_o_andamento_diz_que_a_chave_da_fase_3_continua_desligada_como_o_codigo() -> None:
    assert any(p.startswith("FEATURE_PHASE3_THIRD_PARTY ainda nao liga") for p in _problemas(ligada=True))
    assert not _problemas(ligada=False)
    assert tem("andamento.md", "**A chave da Fase 3 continua desligada na partida (D6).**")
    assert tem("decisoes.md", "**A chave da Fase 3 continua recusada na partida (D6).**")


# Numeros do codigo -----------------------------------------------------------------------------------------------

JANELA = politica.POLITICA["THREE_SIGNALS_WINDOW_MINUTES"]
PORTA_DA_SALA = Parametros.model_fields["VISITS_INTERNAL_PORT"]
DERIVA = Parametros.model_fields["TOOL_DRIFT_CHECK_SECONDS"].default


def _faixa(campo: object) -> tuple[int, int]:
    metadados = getattr(campo, "metadata", [])
    ge = next(int(m.ge) for m in metadados if getattr(m, "ge", None) is not None)
    le = next(int(m.le) for m in metadados if getattr(m, "le", None) is not None)
    return ge, le


def _milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


NUMEROS: tuple[tuple[str, bool, str], ...] = (
    # Tetos de tamanho da entrada num terceiro.
    ("decisoes.md", argumentos.TETO_TEXTO == 10_000, f"texto de mais de {_milhar(argumentos.TETO_TEXTO)} letras"),
    ("saida.md", argumentos.TETO_TEXTO == 10_000, f"texto de mais de {_milhar(argumentos.TETO_TEXTO)} letras"),
    ("decisoes.md", argumentos.TETO_LISTA == 1_000, f"lista de mais de {_milhar(argumentos.TETO_LISTA)} itens"),
    ("saida.md", argumentos.TETO_LISTA == 1_000, f"lista de mais de {_milhar(argumentos.TETO_LISTA)} itens"),
    ("decisoes.md", argumentos.TETO_DIGITOS == 100, f"número inteiro de mais de {argumentos.TETO_DIGITOS} dígitos"),
    ("saida.md", argumentos.TETO_DIGITOS == 100, f"número inteiro de mais de {argumentos.TETO_DIGITOS} dígitos"),
    ("decisoes.md", True, f"mais de {_milhar(argumentos.TETO_LETRAS)} letras somando tudo"),
    ("saida.md", True, f"mais de {_milhar(argumentos.TETO_LETRAS)} letras somando tudo"),
    ("decisoes.md", True, f"mais de {_milhar(argumentos.TETO_PARTES)} partes"),
    ("saida.md", True, f"mais de {_milhar(argumentos.TETO_PARTES)} partes"),
    ("decisoes.md", True, f"mais de {esquema.PROFUNDIDADE_MAXIMA} níveis de listas e objetos, um dentro do outro"),
    ("saida.md", True, f"mais de {esquema.PROFUNDIDADE_MAXIMA} níveis de listas e objetos, um dentro do outro"),
    # Orcamento de passos.
    ("decisoes.md", True, f"um orçamento de {_milhar(argumentos.ORCAMENTO_PASSOS)} passos, contado e não medido"),
    ("saida.md", True, f"com um orçamento de {_milhar(argumentos.ORCAMENTO_PASSOS)} passos"),
    ("decisoes.md", True, f"conta {argumentos.PASSOS_POR_REFERENCIA} passos a mais"),
    ("decisoes.md", True, f"mais um a cada {argumentos.LETRAS_POR_PASSO} letras do texto da referência"),
    ("decisoes.md", True, f"Cada erro conta {argumentos.PASSOS_POR_ERRO} passos"),
    # Tetos do esquema de terceiro.
    ("decisoes.md", True, f"Até {_milhar(esquema.CUSTO_MAXIMO)} partes e {esquema.PROFUNDIDADE_MAXIMA} níveis"),
    ("decisoes.md", True, f"Cada `enum` com até {_milhar(esquema.ESCOLHAS_MAXIMAS)} valores"),
    (
        "decisoes.md",
        esquema.BYTES_MAXIMOS == 256 * 1024,
        f"256 KiB ({_milhar(esquema.BYTES_MAXIMOS)} bytes em JSON compacto, UTF-8)",
    ),
    ("decisoes.md", True, f"têm até {esquema.REFERENCIA_MAXIMA} letras"),
    ("decisoes.md", True, f"de até {esquema.DESCRICAO_DE_CAMPO_MAXIMA} letras"),
    ("decisoes.md", True, f"de {servico.TETO_FORNECEDOR[0]} a {servico.TETO_FORNECEDOR[1]} letras"),
    # Niveis.
    ("decisoes.md", True, f"Ação com mais de {pedidos.NIVEIS_DA_ACAO} níveis"),
    ("saida.md", True, f"os argumentos têm mais de {pedidos.NIVEIS_DA_ACAO} níveis"),
    ("decisoes.md", True, f"Argumentos com mais de {porta.TETO_NIVEIS_PEDIDO} níveis não saem por nenhum caminho"),
    ("saida.md", True, f"argumentos com mais de {porta.TETO_NIVEIS_PEDIDO} níveis, um dentro do outro"),
    ("decisoes.md", True, f"(sistemas próprios), a resposta com mais de {porta.TETO_NIVEIS_RESULTADO} níveis"),
    ("decisoes.md", True, f"um resultado com mais de {servico.TETO_NIVEIS_RESPOSTA} níveis"),
    ("saida.md", True, f"Uma resposta com mais de {porta.TETO_NIVEIS_RESULTADO} níveis"),
    # Ligacao e sala.
    ("decisoes.md", True, f"até {_milhar(sala_contrato.TETO_FERRAMENTAS)} ferramentas de um terceiro"),
    ("decisoes.md", cliente._PAGINAS == 50, f"lê até {cliente._PAGINAS} páginas da lista de ferramentas"),
    ("decisoes.md", True, f"até {cliente.MAX_VERSOES} versões"),
    ("decisoes.md", cliente.PRAZO_CONEXAO == 3.0, "um prazo curto e fixo, de 3 s"),
    ("decisoes.md", sala_cliente.FOLGA_PADRAO == 10.0, "com o prazo da chamada mais 10 s"),
    ("saida.md", sala_cliente.FOLGA_PADRAO == 10.0, "pelo prazo da chamada mais 10 segundos"),
    ("decisoes.md", True, f"a sala deixa {sala.PRONTOS} filhos prontos"),
    ("decisoes.md", True, f"atende até {sala.MAX_CHAMADAS} chamadas ao mesmo tempo"),
    # Parametros.
    (
        "decisoes.md",
        _faixa(PORTA_DA_SALA) == (1024, 65535),
        f"`VISITS_INTERNAL_PORT`: {PORTA_DA_SALA.default} no início, de 1024 a 65535",
    ),
    (
        "decisoes.md",
        True,
        f"`{Parametros.model_fields['VAULT_TRANSIT_KEY_THIRD_PARTY'].default}` no início",
    ),
    (
        "decisoes.md",
        True,
        f"{JANELA.inicial} minutos no início, de {JANELA.minimo} a {_milhar(JANELA.maximo)}",
    ),
    ("andamento.md", True, f"(`THREE_SIGNALS_WINDOW_MINUTES`, {JANELA.inicial} minutos no início)"),
    ("saida.md", True, f"de `THREE_SIGNALS_WINDOW_MINUTES` ({JANELA.inicial} minutos no início)"),
    # Sinais e quarentena.
    (
        "decisoes.md",
        sinais.MAIOR_JANELA_MINUTOS == JANELA.maximo == 7 * 24 * 60,
        f"só se apaga depois de {_milhar(sinais.MAIOR_JANELA_MINUTOS)} minutos (7 dias)",
    ),
    ("decisoes.md", True, f"em {mudou_parou.RODADAS_SEM_RESPOSTA} rodadas seguidas"),
    ("saida.md", True, f"do mudou-parou em {mudou_parou.RODADAS_SEM_RESPOSTA} rodadas seguidas"),
    ("decisoes.md", True, f"(`TOOL_DRIFT_CHECK_SECONDS`, {DERIVA} s)"),
    ("decisoes.md", True, f"(com o padrão, {DERIVA // 2} s)"),
)


@pytest.mark.parametrize(("nome", "confere", "trecho"), NUMEROS, ids=[f"{n}:{t[:50]}" for n, _, t in NUMEROS])
def test_os_numeros_dos_documentos_sao_os_do_codigo(nome: str, confere: bool, trecho: str) -> None:
    assert confere, "o codigo mudou: atualize o documento e este teste"
    assert tem(nome, trecho), trecho


def test_a_marca_de_estranhos_tem_o_tamanho_do_documento() -> None:
    from mcp_claro.regras.estranhos import nova_marca

    assert re.fullmatch(r"[0-9a-f]{12}", nova_marca())
    assert tem("decisoes.md", "uma marca sorteada (12 letras hexadecimais, uma por resposta)")


# Frases do codigo (o extrato e os textos ao aplicativo ficam sem acento; o documento, com) -----------------------

FRASES: tuple[tuple[str, str], ...] = (
    ("decisoes.md", argumentos.RECUSA_ORCAMENTO),
    ("decisoes.md", argumentos.PROBLEMA_FUNDO),
    ("decisoes.md", argumentos.PROBLEMA_NUMERO),
    ("decisoes.md", pedidos.RECUSA_FUNDO),
    ("decisoes.md", pedidos.RECUSA_NUMERO),
    ("decisoes.md", servidor.RECUSA_NUMERO_LEITURA),
    ("decisoes.md", FIM_DA_DESCRICAO_TERCEIRO),
    ("decisoes.md", porta.MOTIVO_TERCEIROS),
    ("saida.md", porta.MOTIVO_TERCEIROS),
    ("decisoes.md", MOTIVO_PARADA),
    ("saida.md", MOTIVO_PARADA),
    ("decisoes.md", autorizacao.MENSAGEM_TERCEIRO_JA_CONFIGURADO),
    ("decisoes.md", "Guarde a chave de leitura das declaracoes deste servidor antes de concluir a revisao"),
    ("saida.md", porta.RESULTADO_FUNDO_DEMAIS),
    ("decisoes.md", servico.MENSAGEM_CHAVE_INDISPONIVEL),
    ("saida.md", quarentena_da_porta.MOTIVO_LIMITE),
    ("saida.md", porta._NAO_ENVIADO[sala_contrato.SALA_FORA]),
    ("saida.md", porta._NAO_ENVIADO[cliente.ENDERECO_RECUSADO]),
    ("decisoes.md", ponto.FORA_DO_PADRAO),
    ("saida.md", ponto.FORA_DO_PADRAO),
    *(("decisoes.md", legivel) for legivel in sorted(set(ponto.PEDIDOS_LEGIVEIS.values()))),
    *(("saida.md", legivel) for legivel in sorted(set(ponto.PEDIDOS_LEGIVEIS.values()))),
    *(("decisoes.md", aviso.assunto) for aviso in AVISOS_ADMIN.values()),
    *(("decisoes.md", f"`{evento}`") for evento in AVISOS_ADMIN),
)


@pytest.mark.parametrize(("nome", "frase"), FRASES, ids=[f"{n}:{f[:50]}" for n, f in FRASES])
def test_as_frases_citadas_sao_as_do_codigo(nome: str, frase: str) -> None:
    assert tem(nome, frase, acento=False), frase


def test_a_mensagem_da_conclusao_sem_a_chave_e_a_do_despachante() -> None:
    fonte = (RAIZ / "src/mcp_claro/despachante/servico.py").read_text(encoding="utf-8")
    assert '"Guarde a chave de leitura das declaracoes deste servidor antes de concluir a revisao."' in fonte


def test_o_texto_de_terceiro_nas_telas_da_pessoa_e_o_da_tela() -> None:
    modelo = (RAIZ / "src/mcp_claro/telas/modelos/acoes/_estado.html").read_text(encoding="utf-8")
    frase = "O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos."
    assert frase in modelo
    assert tem("saida.md", frase)
    assert tem("decisoes.md", frase)
    dica = (
        "O MCP-CLARO não repassa a você pedidos de dados, links ou confirmações que um sistema faça no meio de uma"
        " chamada. Um texto de resposta ainda pode trazer um link: confirme ações só na tela do MCP-CLARO."
    )
    assert dica in modelo
    assert tem("saida.md", dica)


def test_os_desfechos_da_sondagem_sao_os_do_documento() -> None:
    por_status = {s: (cliente._desfecho_por_status(s) or ("",))[0] for s in range(300, 600)}
    assert {s for s, d in por_status.items() if d == "desvio"} == set(range(300, 400))
    assert {s for s, d in por_status.items() if d == "sem_acesso"} == {401, 403, 407}
    assert {s for s, d in por_status.items() if d == "inalcancavel"} == {408, 429, *range(500, 600)}
    assert {s for s, d in por_status.items() if d == "versao"} == {405}
    assert {s for s, d in por_status.items() if d == "nao_mcp"} == {406, 415}
    assert tem("decisoes.md", "desvio (3xx)")
    assert tem("decisoes.md", "sem acesso (401, 403 e 407)")
    assert tem("decisoes.md", "inalcançável (408, 429, 5xx, rede, prazo)")
    assert tem("decisoes.md", "o HTTP 405, que um servidor do transporte antigo do MCP costuma dar")
    assert tem("decisoes.md", "não é MCP (404 sem resposta JSON-RPC, página HTML, 406 e 415)")


def test_os_codigos_que_viram_nao_saiu_sao_os_do_documento() -> None:
    assert {-32601, -32602, -32600, -32700, -32021} == porta._ANTES_DE_EXECUTAR
    assert {s for s in range(300, 600) if cliente.recusado_por_status(s)} == {401, 403, *range(300, 400)}
    for nome in ("decisoes.md", "saida.md"):
        assert tem(nome, "-32601, -32602, -32600, -32700 e -32021")


def test_os_nomes_que_so_o_despachante_da_sao_os_do_documento() -> None:
    assert {"SalaFora", "SalaRespostaInvalida"} == sala_contrato.EXCECOES_DO_DESPACHANTE
    assert tem("decisoes.md", "`SalaFora` e `SalaRespostaInvalida`")
    assert {
        sala_contrato.MOTIVO_SALA_FORA,
        sala_contrato.MOTIVO_SALA_INVALIDA,
    } == sala_contrato.MOTIVOS_DO_DESPACHANTE
    for motivo in sala_contrato.MOTIVOS_DO_DESPACHANTE:
        assert tem("decisoes.md", motivo.removesuffix("; nada foi enviado"), acento=False)


def test_os_registradores_do_kit_filtrados_sao_os_do_documento() -> None:
    assert {"mcp.client.streamable_http", "client"} <= set(LOGGERS_DO_KIT)
    assert tem("decisoes.md", "São eles o `mcp.client.streamable_http` e o `client`")


# Provas citadas e links ------------------------------------------------------------------------------------------

_RAIZES = ("tests/", "src/", "docker/", "migrations/", "grafana/", "scripts/", "conformidade/", "exemplos/")


def _citacoes(texto: str) -> Iterator[tuple[Path, str | None]]:
    """(arquivo, teste) de cada citacao entre crases, na ordem; ``::teste`` sozinho vale para o ultimo arquivo."""
    atual: Path | None = None
    for trecho in re.findall(r"`([^`\n]+)`", texto):
        caminho, _, teste = trecho.partition("::")
        if trecho.startswith("::"):
            assert atual is not None, trecho
            yield atual, teste
            continue
        if caminho.startswith(_RAIZES) and " " not in caminho:
            atual = RAIZ / caminho
        elif re.fullmatch(r"test_\w+\.py", caminho):
            achados = [p for p in (RAIZ / "tests/integracao" / caminho, RAIZ / "tests/unidade" / caminho) if p.exists()]
            assert achados, caminho
            atual = achados[0]
        elif re.fullmatch(r"[a-z_]+/[a-z_]+\.py", caminho):
            atual = RAIZ / "src/mcp_claro" / caminho
        else:
            continue
        yield atual, teste or None


@pytest.mark.parametrize("nome", NOMES)
def test_cada_prova_citada_existe(nome: str) -> None:
    vistas = 0
    for arquivo, teste in _citacoes(ler(nome)):
        assert arquivo.exists(), arquivo
        if teste:
            fonte = arquivo.read_text(encoding="utf-8")
            assert re.search(rf"^(async )?def {re.escape(teste)}\(", fonte, re.MULTILINE), f"{arquivo}::{teste}"
        vistas += 1
    assert nome == "saida.md" or vistas > 10  # o roteiro da porta de saida nao cita testes


def _ancora(titulo: str) -> str:
    """A ancora que o GitHub da a um titulo: minusculas, sem pontuacao, espacos viram hifens."""
    return re.sub(r"[^\w\- ]", "", titulo.strip().lower()).replace(" ", "-")


@pytest.mark.parametrize("nome", NOMES)
def test_cada_link_entre_documentos_acha_a_secao(nome: str) -> None:
    for alvo, ancora in re.findall(r"\]\(([\w-]+\.md)?(?:#([^)]+))?\)", ler(nome)):
        arquivo = DOCS / (alvo or nome)
        assert arquivo.exists(), alvo
        if ancora:
            titulos = [linha.lstrip("#") for linha in arquivo.read_text(encoding="utf-8").splitlines()]
            linhas = arquivo.read_text(encoding="utf-8").splitlines()
            ancoras = {_ancora(t) for t, linha in zip(titulos, linhas, strict=True) if linha.startswith("#")}
            assert ancora in ancoras, f"{alvo or nome}#{ancora}"


# Texto para leigo ------------------------------------------------------------------------------------------------

_SEM_ACENTO = re.compile(
    r"\b(nao|acao|acoes|revisao|versao|tambem|voce|entao|numero|niveis|sao|publico|conferencia|servico|"
    r"saida|politica|sessao|informacao|ja|so|ate|alem|area|pagina|paginas)\b"
)


def _fora_das_citacoes(texto: str) -> str:
    """O texto sem as citacoes (codigo entre crases e frases entre aspas: as do codigo ficam sem acento) e sem o
    destino dos links (nomes de arquivo)."""
    return re.sub(r'`[^`]*`|"[^"]*"|“[^”]*”|\]\([^)]*\)', " ", junto(texto))


PARTES_DA_FASE_3 = (
    ("decisoes.md", "## Decisões da Fase 3: servidores de terceiros (21/11/2026)"),
    ("decisoes.md", "## Mudanças deliberadas da Fase 3 (21/11/2026)"),
    ("andamento.md", "## Fase 3: servidores de terceiros"),
    ("saida.md", "## Ações em servidores de terceiros"),
)


@pytest.mark.parametrize(("nome", "titulo"), PARTES_DA_FASE_3)
def test_as_partes_da_fase_3_tem_acento_fora_das_citacoes(nome: str, titulo: str) -> None:
    texto = secao(ler(nome), titulo)
    achados = sorted(set(_SEM_ACENTO.findall(_fora_das_citacoes(texto))))
    assert not achados, f"{nome}, {titulo}: {achados}"


def _e_emoji(n: int) -> bool:
    return 0x1F000 <= n <= 0x1FAFF or 0x2600 <= n <= 0x27BF or 0x2B00 <= n <= 0x2BFF or n == 0xFE0F


@pytest.mark.parametrize("nome", NOMES)
def test_sem_emoji(nome: str) -> None:
    emoji = [c for c in ler(nome) if _e_emoji(ord(c))]
    assert not emoji


_PROMESSA = re.compile(r"(?i)\b(garante|garantem|garantimos|garantido|garantida|nunca falha|sem nenhum risco)\b|100 ?%")


@pytest.mark.parametrize("nome", NOMES)
def test_sem_promessa_de_garantia(nome: str) -> None:
    assert not _PROMESSA.findall(ler(nome))


def test_os_numeros_medidos_dizem_como_foram_medidos_e_que_nao_sao_garantia() -> None:
    mudancas = junto(secao(ler("decisoes.md"), "## Mudanças deliberadas da Fase 3 (21/11/2026)"))
    assert '"tempo de CPU" é o tempo que o processador gastou na conta' in mudancas
    assert '"tempo de relógio" é o tempo que passou, com a espera junto' in mudancas
    assert "Nenhum deles é garantia." in mudancas
    assert "Nada disso é garantia." in mudancas
    assert "em tempo de CPU, com a memória pelo `tracemalloc`" in mudancas


# Textos de tela citados (com acento, iguais aos da tela) ----------------------------------------------------------


def test_a_frase_do_admin_para_o_esquema_fundo_e_a_da_tela() -> None:
    assert tem("decisoes.md", f'a tela do ADMIN diz "{paginas.ESQUEMA_FUNDO_DEMAIS}"')


# Leitura: o cofre que nao abre a chave e o pedido do sistema no meio (G3a, c e d) --------------------------------


def _ferramenta(origem: str) -> FerramentaVitrine:
    sistema = Sistema(
        uuid.uuid4(), f"sis-{origem}", "https://t.exemplo/mcp", origem, "por_pessoa", 1, "ativo", VERSAO_MCP
    )
    return FerramentaVitrine(
        id=uuid.uuid4(),
        sistema=sistema,
        nome_original="ler",
        versao=1,
        nome_vitrine="ler_x",
        titulo=None,
        descricao="Ler.",
        esquema_entrada={"type": "object"},
        esquema_saida=None,
        etiquetas={},
        classe="le",
        risco="baixo",
    )


@pytest.mark.parametrize("origem", ["proprio", "terceiro"])
def test_a_linha_da_leitura_com_pedido_do_sistema_e_a_do_documento(origem: str) -> None:
    pede_link = types.InputRequiredResult.model_validate(
        {
            "resultType": "input_required",
            "inputRequests": {
                "l": {"method": "elicitation/create", "params": {"mode": "url", "message": "x", "url": "https://p.ex"}}
            },
        }
    )
    com_pedido = servico.interpretar(
        cliente.Resposta("resultado", resultado=pede_link, status_http=(200,)),
        _ferramenta(origem),
        com_chave_da_pessoa=False,
    )
    feita = servico.interpretar(
        cliente.Resposta(
            "resultado",
            resultado=types.CallToolResult(content=[types.TextContent(type="text", text="ok")]),
            status_http=(200,),
        ),
        _ferramenta(origem),
        com_chave_da_pessoa=False,
    )
    assert (com_pedido.tipo, feita.tipo) == ("leitura_sistema_pediu_dados", "leitura_feita")
    pelo_programa = programas.PARA_PROGRAMA["leitura_sistema_pediu_dados"]
    assert tem(
        "decisoes.md",
        f"a linha do extrato é `{com_pedido.tipo}` (por um programa, `{pelo_programa}`), com as categorias do nosso"
        f" vocabulário, no lugar de `{feita.tipo}`",
    )
    assert tem("decisoes.md", "**Pedido do sistema no meio da leitura: linha própria no extrato (vale para todos).**")


def test_o_cofre_que_nao_abre_a_chave_na_leitura_esta_no_documento() -> None:
    # A frase e conferida em FRASES; aqui, o motivo e o titulo do item (G3a, c).
    fonte = inspect.getsource(servico.Despachante.ler)
    assert '"chave_indisponivel", MENSAGEM_CHAVE_INDISPONIVEL' in fonte
    assert tem("decisoes.md", "**Leitura com o cofre que não abre a chave: recusa legível (vale para todos).**")
    assert tem("decisoes.md", "a leitura é recusada com o motivo `chave_indisponivel`")


# Critica B8: so a troca de chave do OAuth sai do despachante direto para um terceiro -------------------------------


def test_so_a_troca_de_chave_do_oauth_sai_do_despachante_direto_para_um_terceiro() -> None:
    # Codigo: as chamadas MCP de um terceiro nunca vao pelo cliente local fora de desenvolvimento e teste; a troca e a
    # renovacao do OAuth fazem o POST no token_endpoint de dentro do despachante.
    terceiro = _ferramenta("terceiro").sistema
    for ambiente in ("piloto", "producao"):
        assert ponto.ligacao_para(terceiro, ambiente=ambiente, local_para_terceiro=ponto.PROPRIO) is ponto.SEM_SALA
    assert 'http.stream("POST", config.token_endpoint' in inspect.getsource(autorizacao.trocar)
    assert "terceiro=sistema.de_terceiro" in inspect.getsource(renovacao)
    # Documentos: nenhum diz que o despachante nunca fala com um terceiro; os dois "nunca" levam a ressalva.
    for nome in NOMES:
        assert not re.search(r"nunca fala direto com", junto(ler(nome))), nome
    assert tem(
        "saida.md",
        "Fora de desenvolvimento e teste, o despachante nunca chama as ferramentas de um servidor de terceiros direto:"
        " só pela sala de visitas. A troca e a renovação da autorização (OAuth) saem do despachante direto para o"
        " endereço de troca de chave do terceiro, conferido antes (https e IP público).",
    )
    assert tem(
        "decisoes.md",
        "o despachante nunca chama um terceiro direto: a sondagem, a lista de ferramentas, a leitura e a ação vão pela"
        " sala, e a chamada direta só existe nos testes, posta de propósito. A exceção é a autorização (OAuth): a troca"
        " do código e a renovação da chave saem do despachante direto para o endereço de troca de chave do terceiro,"
        " conferido antes (https e IP público), como diz o D1.",
    )
    assert tem("saida.md", "Só a renovação da chave da pessoa, quando é preciso, não passa pela sala")


# Sem a sala, nenhum pedido MCP vai ao terceiro; "nada vai" nao: a renovacao da chave sai antes, do despachante.
_NADA_VAI = re.compile(
    r"(?i)\bnada (?:vai|sai|chega) (?:a|ao|aos|à|às) (?:um |uma )?(?:servidor(?:es)? de )?terceiro"
    r"|\bsem (?:a )?sala\b[^.]{0,40}\bnada (?:vai|sai|chega)\b"
)


def test_sem_a_sala_nenhum_pedido_mcp_vai_ao_terceiro_mas_a_renovacao_da_chave_sai() -> None:
    # Codigo: na acao e na leitura, a chave da pessoa (e a renovacao dela, pelo token_endpoint) vem antes da ligacao,
    # que, sem a sala, e SEM_SALA.
    enviar = inspect.getsource(porta.PortaDeSaida._enviar)
    assert enviar.index("await self._chave(op, ferramenta)") < enviar.index("self.ligacao(sistema)")
    assert "renovacao.chave_pronta(" in inspect.getsource(porta.PortaDeSaida._chave)
    ler_fonte = inspect.getsource(servico.Despachante.ler)
    assert ler_fonte.index("await self._chave_da_pessoa(") < ler_fonte.index("self.ligacao(sistema)")
    assert "renovacao.chave_pronta(" in inspect.getsource(servico.Despachante._chave_da_pessoa)
    # Documentos.
    for nome in NOMES:
        achado = _NADA_VAI.search(junto(ler(nome)))
        assert achado is None, (nome, achado.group(0) if achado else "")
    d1 = next(linha for linha in ler("decisoes.md").splitlines() if linha.startswith("| D1. "))
    assert "sem ela, nenhum pedido MCP vai a terceiro" in d1
    assert tem("decisoes.md", "Sem sala, nenhum pedido MCP vai a um terceiro")
    assert tem(
        "decisoes.md",
        'Sem a sala, nenhum pedido MCP vai ao terceiro: a ação fica "não saiu", com a linha `sala_fora`. Só a renovação'
        " da chave da pessoa, quando é preciso, sai do despachante direto para o endereço de troca de chave (OAuth) do"
        " terceiro, conferido antes (https e IP público), antes de o pedido ir à sala.",
    )
    assert tem("saida.md", "Sem a sala, a ação não vai ao servidor.")
    assert tem("saida.md", '**"A sala de visitas está fora."** Nenhum pedido MCP vai a servidor de terceiros')


def test_a_troca_de_chave_de_terceiro_confere_o_endereco_antes_do_post() -> None:
    # Os documentos dizem "conferido antes (https e IP publico)".
    trocar = inspect.getsource(autorizacao.trocar)
    conferir = "if terceiro:\n        await _conferir_troca_de_terceiro(config.token_endpoint, ambiente,"
    assert conferir in trocar
    assert trocar.index(conferir) < trocar.index('http.stream("POST", config.token_endpoint')
    assert "cliente.conferir_endereco_terceiro(" in inspect.getsource(autorizacao._conferir_troca_de_terceiro)
    for nome in ("decisoes.md", "saida.md"):
        assert tem(nome, "endereço de troca de chave do terceiro, conferido antes (https e IP público)"), nome


def test_d11_diz_o_nome_e_a_versao_que_cada_caminho_manda() -> None:
    # Na conversa MCP com um terceiro: nome e versao genericos. Na troca e na renovacao do OAuth: a versao do produto.
    assert (cliente._IDENTIDADE_TERCEIRO.name, cliente._IDENTIDADE_TERCEIRO.version) == ("mcp-claro", "1")
    assert cliente._AGENTE_TERCEIRO == "mcp-claro"
    assert not inspect.signature(autorizacao.http_para_autorizacao).parameters  # o mesmo cliente para todo sistema
    assert '"User-Agent": f"mcp-claro/{__version__}"' in inspect.getsource(autorizacao.http_para_autorizacao)
    assert __version__ != "1"
    d11 = next(linha for linha in ler("decisoes.md").splitlines() if linha.startswith("| D11. "))
    assert (
        'Genéricos para terceiros na conversa MCP: o MCP-CLARO se apresenta como `mcp-claro`, versão "1", e o'
        " User-Agent (o nome que vai em cada pedido HTTP) é só `mcp-claro`." in d11
    )
    assert (
        "A troca e a renovação da chave (OAuth), que saem do despachante para o endereço de troca de chave do"
        " terceiro, ainda mandam a versão do produto no User-Agent (`mcp-claro/` e a versão)" in d11
    )


def test_a_frase_da_autorizacao_com_os_terceiros_desligados_e_a_da_tela() -> None:
    frase = autorizar.MENSAGENS["terceiros_desligados"]
    assert "estão" in frase  # a tela, com acento
    assert tem("decisoes.md", f'só pessoa de teste autoriza (`terceiros_desligados`); a tela diz à pessoa "{frase}"')


# D6 e D9 -----------------------------------------------------------------------------------------------------------


def test_d6_diz_que_as_provas_de_ponta_a_ponta_nao_tiram_a_recusa() -> None:
    d6 = next(linha for linha in ler("decisoes.md").splitlines() if linha.startswith("| D6. "))
    assert "As provas de ponta a ponta não tiram essa recusa." in d6
    assert any(p.startswith("FEATURE_PHASE3_THIRD_PARTY ainda nao liga") for p in _problemas(ligada=True))


def test_d9_diz_o_que_a_guarda_faz_quando_o_tipo_de_chave_muda() -> None:
    guarda = (RAIZ / "migrations/versions/0006_fase3.py").read_text(encoding="utf-8")
    regra = guarda[guarda.index("OR NEW.tipo_chave IS DISTINCT FROM OLD.tipo_chave THEN") :]
    regra = regra[: regra.index("  END IF;\n  IF ")]
    for campo in ("versao_mcp", "sondagem", "revisao_servidor_id"):
        assert f"NEW.{campo} := NULL;" in regra, campo
    assert "WHEN NEW.situacao = 'desativado' THEN 'desativado' ELSE 'quarentena' END;" in regra
    assert "NEW.situacao := 'pendente';" in regra
    assert "IF OLD.origem" not in regra  # vale para todo sistema, proprio ou terceiro
    d9 = next(linha for linha in ler("decisoes.md").splitlines() if linha.startswith("| D9. "))
    assert (
        "Mudar o tipo de chave de qualquer sistema apaga a versão, a sondagem e a revisão do servidor e volta o sistema"
        ' a "pendente", como nas Fases 1 e 2; um sistema em quarentena continua em quarentena (ou vai a "desativado",'
        " se for esse o pedido)" in d9
    )


# Contas do orcamento, feitas pelo codigo de agora ------------------------------------------------------------------


def _cadeia(referencias: int) -> dict[str, Any]:
    """Cada item da lista segue ``referencias`` referencias curtas, uma chamando a outra (a ultima, vazia)."""
    n = referencias - 1
    definicoes: dict[str, Any] = {f"d{k}": {"$ref": f"#/$defs/d{k + 1}"} for k in range(n)}
    definicoes[f"d{n}"] = {}
    lista = {"type": "array", "items": {"$ref": "#/$defs/d0"}}
    esquema_json: dict[str, Any] = json.loads(
        json.dumps({"type": "object", "properties": {"l": lista}, "$defs": definicoes})
    )
    return esquema_json


def _copia(esquema_json: dict[str, Any]) -> dict[str, Any]:
    """A copia que a entrada confere, com o validador pronto (como depois da primeira chamada)."""
    argumentos.conferir_esquema(esquema_json)
    esquema.conferir_terceiro(esquema_json)
    copia = esquema.para_conferir(esquema_json)
    with contextlib.suppress(argumentos.ArgumentosInvalidos):
        argumentos.conferir_argumentos(copia, {}, terceiro=True)
    return copia


def _passos(copia: dict[str, Any], pedido: dict[str, Any]) -> int | None:
    """Os passos gastos, ou None quando passa do orcamento."""
    try:
        return argumentos.conferir_argumentos(copia, pedido, terceiro=True)
    except argumentos.OrcamentoEsgotado:
        return None


def test_as_contas_do_orcamento_no_documento_sao_as_do_codigo(monkeypatch: pytest.MonkeyPatch) -> None:
    lista = {"l": [0] * argumentos.TETO_LISTA}
    cabem = _passos(_copia(_cadeia(12)), lista)
    assert cabem is not None
    assert _passos(_copia(_cadeia(13)), lista) is None
    monkeypatch.setattr(argumentos, "PASSOS_POR_REFERENCIA", 0)
    antes = _passos(_copia(_cadeia(13)), lista)
    assert antes is not None
    monkeypatch.undo()
    assert tem(
        "decisoes.md",
        f"em cada item de uma lista de {_milhar(argumentos.TETO_LISTA)} itens, 12 referências por item ainda cabem"
        f" ({_milhar(cabem)} passos), e 13 não cabem mais (antes dos {argumentos.PASSOS_POR_REFERENCIA} passos a mais"
        f" por referência, gastavam {_milhar(antes)})",
    )

    enum_no_teto = {
        "type": "object",
        "properties": {"l": {"type": "array", "items": {"enum": list(range(esquema.ESCOLHAS_MAXIMAS))}}},
    }
    copia = _copia(enum_no_teto)
    valor = esquema.ESCOLHAS_MAXIMAS - 1
    um, dois = _passos(copia, {"l": [valor]}), _passos(copia, {"l": [valor] * 2})
    assert um is not None
    assert dois is not None
    assert _passos(copia, {"l": [valor] * 49}) is not None
    assert _passos(copia, {"l": [valor] * 50}) is None
    assert tem(
        "decisoes.md",
        f"com `items: {{enum: [{_milhar(esquema.ESCOLHAS_MAXIMAS)} valores]}}`, cada item custa {_milhar(dois - um)}"
        " passos, e uma lista válida com mais de 49 itens é recusada",
    )


# Numeros medidos ---------------------------------------------------------------------------------------------------

_TEMPO = re.compile(r"\b\d+(?:,\d+)? (?:s|ms)\b")
_MEDIDO = re.compile(r"(?i)\bmedid[ao]s?\b")


def _itens(texto: str) -> list[str]:
    """Os itens de primeiro nivel (``- ...`` na coluna 0) de uma secao, cada um numa linha so."""
    return [junto(item) for item in re.split(r"\n(?=- )", texto) if item.startswith("- ")]


def test_cada_item_com_tempo_medido_diz_se_e_cpu_ou_relogio() -> None:
    mudancas = secao(ler("decisoes.md"), "## Mudanças deliberadas da Fase 3 (21/11/2026)")
    vistos = 0
    for item in _itens(mudancas):
        if _MEDIDO.search(item) and _TEMPO.search(item):
            vistos += 1
            assert "CPU" in item or "relógio" in item, item[:120]
    assert vistos >= 5
    assert tem("decisoes.md", "a conferência levou 12 s de tempo de relógio numa medida da etapa 3")
    assert tem("decisoes.md", "(cerca de 0,8 s de tempo de relógio, numa medida da etapa 3)")
    assert tem("decisoes.md", "cerca de 0,75 s de tempo de relógio")


def test_o_pior_caso_pelo_maior_tempo_bate_com_as_rodadas() -> None:
    # A rodada da etapa 5 deu 0,30 s no allOf de 660 if vazios, acima dos 0,27 s do anyOf nas rodadas da etapa 4.
    assert tem("decisoes.md", "numa rodada da etapa 5, com a máquina ocupada por outros trabalhos, de 0,13 a 0,30 s")
    assert tem(
        "decisoes.md",
        "Pelo maior tempo, o `allOf` de 660 `if` vazios chegou a 0,30 s na rodada da etapa 5; nas da etapa 4, o maior"
        " foi o `anyOf` de 1.990 `const`, com 0,27 s.",
    )
    assert not tem("decisoes.md", "Pelo maior tempo, o `anyOf` de 1.990 `const` chega a 0,27 s")


# Riscos aceitos ----------------------------------------------------------------------------------------------------


def _riscos() -> dict[str, str]:
    riscos = secao(ler("decisoes.md"), "### Riscos aceitos da Fase 3")
    return {m.group(1): item for item in _itens(riscos) if (m := re.match(r"- \*\*(.+?)\*\*", item))}


def test_os_riscos_que_o_escopo_nomeia_estao_registrados_como_o_codigo_faz(monkeypatch: pytest.MonkeyPatch) -> None:
    riscos = _riscos()
    # Landlock (decisao do dono): sem Landlock, o filho segue sem o dominio proprio, e a partida diz landlock=0.
    landlock = riscos["A sala sobe mesmo sem Landlock (decisão do dono)."]
    assert "a sala não recusa a partida: o log de partida diz `landlock=0`" in landlock
    monkeypatch.setattr(filho, "versao_landlock", lambda: 0)
    assert filho.dominio_proprio() is False
    assert "landlock=filho.versao_landlock()" in inspect.getsource(sala)
    # Com o Landlock 1 a 5 o sinal entre filhos passa: o escopo dos sinais so entra a partir do 6.
    assert "_LANDLOCK_SCOPE_SIGNAL if versao >= 6 else 0" in inspect.getsource(filho.dominio_proprio)
    assert "Com o Landlock 1 a 5, o sinal entre filhos ainda passa (negação de serviço)." in landlock
    # So o conferido, dizendo onde; o que nao foi conferido, dito assim (a versao vem do nucleo, nao da distribuicao).
    # O nucleo da maquina de testes e do proprio ambiente, nao do Ubuntu: o documento diz o nome inteiro e isso.
    assert (
        "Conferido em 21/11/2026, só na máquina de testes da Fase 3: o sistema é o Ubuntu 24.04.5 (x86_64), mas o"
        " núcleo é o 6.18.44-fc-v726 (`uname -r`), do próprio ambiente de testes, e não um núcleo do Ubuntu"
        " (`/proc/version`)." in landlock
    )
    assert (
        "Esse núcleo anuncia o Landlock 7 (`filho.versao_landlock()`, que pergunta ao núcleo), e o Yama não está ligado"
        " nele (não existe `/proc/sys/kernel/yama`)." in landlock
    )
    assert (
        "Não foram conferidos o runner ubuntu-24.04 do CI nem as máquinas de piloto e produção: o log de partida da"
        " sala (`landlock=N`) diz a versão em cada uma." in landlock
    )
    assert set(re.findall(r"Landlock \d+", landlock)) == {"Landlock 1", "Landlock 7"}
    # O modelo de ameacas diz o mesmo: o nucleo 6.18 da maquina de testes com o Landlock 7; o CI, nao conferido. O
    # nucleo vale com o nome curto ou com o nome inteiro (o mesmo do item acima), nunca cortado no meio.
    ameacas = junto((DOCS / "modelo-de-ameacas.md").read_text(encoding="utf-8"))
    paragrafo = ameacas[ameacas.index("**Sala sem Landlock (decisão do dono).**") :][:900]
    assert re.search(r"\(núcleo 6\.18(?:\.44-fc-v726)?\) tem Landlock 7", paragrafo)
    assert re.search(r"ubuntu-24\.04 do CI .{0,80}não foi conferido", paragrafo)
    # "Nao saiu" forjavel: os mesmos codigos e status que a porta conta como "nao saiu".
    forjavel = riscos['"Não saiu" forjável por um terceiro.']
    assert {int(c) for c in re.findall(r"-\d{5}", forjavel)} == porta._ANTES_DE_EXECUTAR
    assert "401, 403 e 3xx no último status" in forjavel
    assert {s for s in range(300, 600) if cliente.recusado_por_status(s)} == {401, 403, *range(300, 400)}
    # Resposta funda demais pelos dois caminhos (decisao do coordenador).
    funda = riscos["Resposta funda demais pelos dois caminhos."]
    assert 'Pela sala, a resposta funda demais volta como dúvida, e a ação fica "não confirmada".' in funda
    assert 'Pelo cliente local (sistemas próprios), a ação fica "enviada" sem resultado.' in funda
    for nome, trecho in (
        (
            "decisoes.md",
            "Pelo cliente local (sistemas próprios), a resposta com mais de 199 níveis, ou que o kit não escreve, deixa"
            ' a ação "enviada" sem resultado.',
        ),
        (
            "decisoes.md",
            'Pela sala de visitas, o caminho de um terceiro, uma resposta assim volta como dúvida, e a ação fica "não'
            ' confirmada"',
        ),
        (
            "saida.md",
            'Pela sala de visitas, o caminho de um servidor de terceiros, ela volta como dúvida, e a ação fica "não'
            ' confirmada". Pelo cliente local, o dos sistemas próprios, ela deixa a ação "enviada" sem resultado',
        ),
    ):
        assert tem(nome, trecho), trecho
    assert porta.TETO_NIVEIS_RESULTADO == 199


# A versao do Landlock vem do nucleo de cada maquina, nao da distribuicao. Lista branca (etapa 5f): nos tres
# documentos, o Ubuntu, o Yama, uma versao do Landlock e o nucleo 6.18 so aparecem nas frases conferidas do item "A
# sala sobe mesmo sem Landlock", cada uma inteira e uma vez. Qualquer outra frase com eles falha, certa ou errada:
# uma frase nova precisa entrar aqui, conferida. Conta como versao do Landlock toda frase que traga "Landlock" e um
# numero, em algarismo ou por extenso, em qualquer ordem ("Landlock 7", "Landlock v7", "versao 7 do Landlock",
# "landlock=0"; retoque da etapa 5f: antes so contava "Landlock", um espaco e um algarismo).
_CITA_A_MAQUINA = re.compile(r"(?i)ubuntu|yama|6\.18")
_LANDLOCK = re.compile(r"(?i)landlock")
_NUMERO = re.compile(
    r"(?i)\d|\b(?:dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez)\b"
    r"|\b(?:primeir|segund|terceir|quart|quint|sext|s[eé]tim|oitav|non|d[eé]cim)[oa]s?\b"
)
_FRASES_DA_MAQUINA = (
    "Num núcleo sem Landlock (antigo ou com ele desligado), a sala não recusa a partida: o log de partida diz"
    " `landlock=0`, e cada filho fica com o aviso de isolado, a proteção contra depuração (`PR_SET_DUMPABLE`) e os"
    " canos por soquete.",
    "Se o núcleo também deixar um processo depurar o irmão (o Yama desligado), ele pode, na janela de partida de um"
    " filho novo, até o isolamento (cerca de 0,8 s de tempo de relógio, numa medida da etapa 3), prender-se a ele ou"
    " abrir a memória dele e ler o pedido depois.",
    "Com o Landlock 1 a 5, o sinal entre filhos ainda passa (negação de serviço).",
    "Conferido em 21/11/2026, só na máquina de testes da Fase 3: o sistema é o Ubuntu 24.04.5 (x86_64), mas o"
    " núcleo é o 6.18.44-fc-v726 (`uname -r`), do próprio ambiente de testes, e não um núcleo do Ubuntu"
    " (`/proc/version`).",
    "Esse núcleo anuncia o Landlock 7 (`filho.versao_landlock()`, que pergunta ao núcleo), e o Yama não está ligado"
    " nele (não existe `/proc/sys/kernel/yama`).",
    "Não foram conferidos o runner ubuntu-24.04 do CI nem as máquinas de piloto e produção: o log de partida da"
    " sala (`landlock=N`) diz a versão em cada uma.",
)
_ITEM_DO_LANDLOCK = "A sala sobe mesmo sem Landlock (decisão do dono)."
_LINHA_SO = re.compile(r"#|\|")  # titulo e linha de tabela: um bloco cada
_INICIO_DE_ITEM = re.compile(r"\s*(?:[-*]|\d+\.) ")


def _frases(texto: str) -> list[str]:
    """O texto cortado em frases: primeiro em blocos (a linha em branco separa; titulo e linha de tabela sao um bloco
    cada; um item de lista abre um bloco novo; assim um titulo como "Fase 3" nao gruda na frase de baixo), depois em
    ponto, interrogacao ou exclamacao seguidos de espaco."""
    blocos: list[list[str]] = [[]]
    for linha in texto.splitlines():
        so = bool(_LINHA_SO.match(linha))
        if so or not linha.strip() or _INICIO_DE_ITEM.match(linha):
            blocos.append([])
        blocos[-1].append(linha)
        if so:
            blocos.append([])
    return [f for bloco in blocos for f in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ý`*\"“(|])", junto("\n".join(bloco))) if f]


def _cita_a_maquina(frase: str) -> str | None:
    """O trecho da ``frase`` que cita a maquina: o Ubuntu, o Yama, o nucleo 6.18 ou uma versao do Landlock."""
    if achado := _CITA_A_MAQUINA.search(frase):
        return achado.group()
    if _LANDLOCK.search(frase) and (numero := _NUMERO.search(frase)):
        return f"Landlock e {numero.group()!r}"
    return None


def _frases_sem_as_conferidas(nome: str) -> list[str]:
    """As frases do documento, sem as conferidas do item do Landlock (so o decisoes.md as tem, todas dentro do item,
    cada uma inteira e uma vez)."""
    frases = _frases(ler(nome))
    if nome != "decisoes.md":
        return frases
    texto = junto(ler(nome))
    item = _riscos()[_ITEM_DO_LANDLOCK]
    assert texto.count(item) == 1
    for conferida in _FRASES_DA_MAQUINA:
        assert item.count(conferida) == 1, conferida
        assert texto.count(conferida) == 1, conferida
        assert sum(frase.count(conferida) for frase in frases) == 1, conferida
        frases = [frase.replace(conferida, " ") for frase in frases]
    return frases


@pytest.mark.parametrize("nome", NOMES)
def test_ubuntu_yama_e_versao_do_landlock_so_nas_frases_conferidas(nome: str) -> None:
    citam = [(frase, achado) for frase in _frases_sem_as_conferidas(nome) if (achado := _cita_a_maquina(frase))]
    assert citam == [], nome


def test_a_lista_branca_do_landlock_pega_as_frases_que_ja_passaram() -> None:
    # As frases erradas que ja estiveram nos documentos, ou que passavam pela lista negra de antes (etapa 5r) e pela
    # lista branca da etapa 5f (as ultimas, com a versao escrita de outro jeito): todas citam a maquina, entao so
    # passariam escritas como uma das frases conferidas.
    for errada in (
        "o Ubuntu 24.04 e a máquina de testes têm o Landlock 7",
        "O Ubuntu 24.04 tem o Landlock 7.",
        "o Ubuntu 24.04.5 (x86_64) traz o núcleo 6.18",
        "o Landlock 7 do Ubuntu",
        "(Ubuntu 24.04.5, x86_64): o núcleo é o 6.18.44 (`uname -r`)",
        "o Yama está ligado nela",
        "O Ubuntu 24.04 oferece o Landlock 7.",
        "No Ubuntu 24.04, o Landlock é o 7.",
        "A sala roda no Ubuntu 24.04 com Landlock 7.",
        "O núcleo do Ubuntu 24.04 é o 6.18 e tem o Landlock 7.",
        "o Yama está ativo",
        "com o Yama ligado",
        "A versão 7 do Landlock vem da distribuição da máquina.",
        "A máquina de testes tem o Landlock v7.",
        "O Landlock ABI 7 vem da distribuição.",
        "A distribuição traz o Landlock na versão 7.",
        "A distribuição traz o Landlock na versão sete.",
        "Na máquina de testes, o log diz landlock=7.",
        "O Landlock (7) vem da distribuição.",
    ):
        assert _cita_a_maquina(errada), errada
    # O texto de fora do Landlock nao conta: sem numero, ou sem Landlock.
    for certa in (
        "A versão do Landlock vem do núcleo do Linux de cada máquina, não do nome da distribuição.",
        "O filho entra num domínio Landlock só dele (uma proteção do núcleo do Linux).",
        "A sala atende até 32 chamadas ao mesmo tempo.",
    ):
        assert _cita_a_maquina(certa) is None, certa
    # O numero de um titulo, de um item ou de uma linha de tabela nao gruda na frase de baixo.
    for texto in (
        "## Fase 3: servidores de terceiros\nA sala usa o Landlock do núcleo.",
        "- Prova: 2 testes\n- A sala usa o Landlock do núcleo.",
        "| Etapa 3 | feito |\n| A sala usa o Landlock do núcleo. | feito |",
        "Na etapa 3, nada mudou.\n\nA sala usa o Landlock do núcleo.",
    ):
        frases = _frases(texto)
        assert any(frase.endswith("A sala usa o Landlock do núcleo.") for frase in frases), frases
        assert not any(_cita_a_maquina(frase) for frase in frases), frases
        assert _cita_a_maquina(junto(texto)), texto  # numa frase so, o numero de cima contaria
    assert _frases("A sala usa o Landlock\ndo núcleo. Na versão 7.") == [
        "A sala usa o Landlock do núcleo.",
        "Na versão 7.",
    ]
    # Cada frase conferida cita a maquina (senao nao precisaria estar na lista) e fica dentro do item.
    item = _riscos()[_ITEM_DO_LANDLOCK]
    for frase in _FRASES_DA_MAQUINA:
        assert _cita_a_maquina(frase), frase
        assert frase in item, frase
    # Fora das frases conferidas, o item nao cita a maquina.
    resto = item
    for frase in _FRASES_DA_MAQUINA:
        resto = resto.replace(frase, " ")
    assert [frase for frase in _frases(resto) if _cita_a_maquina(frase)] == []


def test_a_sala_fora_da_a_linha_sala_fora_na_acao_e_na_leitura() -> None:
    # Codigo: sem a sala (SemSala devolve resposta_sala_fora), a leitura volta com a recusa, um erro nosso com o motivo
    # sala_fora, e grava a linha `sala_fora`; a acao usa a mesma regra (linha_da_sala). Proprio nunca tem essa linha.
    fora = sala_contrato.resposta_sala_fora()
    leitura = servico.interpretar(fora, _ferramenta("terceiro"), com_chave_da_pessoa=False)
    assert leitura.resultado["isError"] is True
    assert leitura.resultado["_meta"][PREFIXO_META + "motivo"] == ponto.LINHA_SALA_FORA == "sala_fora"
    assert leitura.sala == ponto.LINHA_SALA_FORA
    assert "tipo=leitura.sala," in inspect.getsource(servico.Despachante.ler)
    assert ponto.linha_da_sala(fora, _ferramenta("terceiro").sistema) == "sala_fora"
    assert ponto.linha_da_sala(fora, _ferramenta("proprio").sistema) is None
    assert "tipo = linha_da_sala(resposta, sistema)" in inspect.getsource(porta.PortaDeSaida._registrar_sala)
    assert tem(
        "saida.md",
        '**"A sala de visitas está fora."** Nenhum pedido MCP vai a servidor de terceiros: a ação vira "não saiu" e a'
        " leitura volta com a recusa, as duas com a linha `sala_fora` no extrato. Os sistemas próprios seguem como"
        " sempre. A conferência do mudou-parou não conta essa rodada contra os servidores de terceiros. Quando a sala"
        " volta, a pessoa pode pedir de novo.",
    )
    # A rodada do mudou-parou com a sala fora: o despachante responde 503 `sala_fora`, as rotinas a leem como falha do
    # MCP-CLARO (ConferenciaNaoFeita), e o mudou-parou nao soma rodada sem resposta (ver o teste abaixo).
    assert "sala_fora" in rotinas_despachante.MOTIVOS_DO_MCP_CLARO
    assert _trata_sem_somar(mudou_parou.conferir_terceiro, "ConferenciaNaoFeita")


def _chamadas(no: ast.AST) -> set[str]:
    """Os nomes das funcoes chamadas dentro de um trecho do codigo."""
    return {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(no)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name | ast.Attribute)
    }


def _trata_sem_somar(funcao: Any, excecao: str) -> bool:
    """O ``except <excecao>`` da funcao volta sem somar nem zerar a contagem de rodadas sem resposta."""
    arvore = ast.parse(inspect.getsource(funcao).lstrip())
    tratadores = [
        t
        for t in ast.walk(arvore)
        if isinstance(t, ast.ExceptHandler) and isinstance(t.type, ast.Name) and t.type.id == excecao
    ]
    assert len(tratadores) == 1, excecao
    corpo = ast.Module(body=tratadores[0].body, type_ignores=[])
    return isinstance(tratadores[0].body[-1], ast.Return) and not _chamadas(corpo) & {
        "_sem_resposta_de_terceiro",
        "_zerar_sem_resposta",
    }


class _Conexao:
    async def execute(self, *_: object) -> _Conexao:
        return self

    def first(self) -> object:
        return object()  # a revisao vigente existe; o que ela tem nao importa antes de ler as declaracoes


class _Banco:
    """So o ``connect`` que ``conferir_terceiro`` usa para ler a revisao vigente; o resto nao e chamado aqui."""

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[_Conexao]:
        yield _Conexao()


class _Fonte:
    def __init__(self, erro: Exception) -> None:
        self.erro = erro

    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        raise self.erro


def _rodada(monkeypatch: pytest.MonkeyPatch, erro: Exception) -> list[str]:
    """Uma rodada do mudou-parou de um terceiro cuja leitura das declaracoes falha com ``erro``: as contagens que ela
    mexeu (``somou`` e ``zerou``)."""
    mexidas: list[str] = []

    async def somou(*_: object, **__: object) -> None:
        mexidas.append("somou")

    async def zerou(*_: object, **__: object) -> None:
        mexidas.append("zerou")

    monkeypatch.setattr(mudou_parou, "_sem_resposta_de_terceiro", somou)
    monkeypatch.setattr(mudou_parou, "_zerar_sem_resposta", zerou)
    resultado = mudou_parou.ResultadoMudouParou()
    asyncio.run(
        mudou_parou.conferir_terceiro(
            cast(Any, _Banco()),
            _Fonte(erro),
            uuid.uuid4(),
            "terceiro-x",
            uuid.uuid4(),
            (),
            resultado,
            intervalo=900.0,
        )
    )
    return mexidas


def _declaracoes_com(status: int, corpo: dict[str, str]) -> Exception:
    """O erro que as rotinas levantam quando o despachante responde ``status`` com ``corpo``."""

    def responder(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=corpo)

    p = Parametros(DISPATCHER_INTERNAL_URL="http://despachante:9443")
    cliente_rotinas = rotinas_despachante.ClienteDespachante(
        p, http=httpx2.AsyncClient(transport=httpx2.MockTransport(responder))
    )

    async def ler() -> Exception:
        try:
            await cliente_rotinas.declaracoes(uuid.uuid4())
        except Exception as erro:  # o teste olha o tipo do erro
            return erro
        finally:
            await cliente_rotinas.fechar()
        raise AssertionError("o despachante respondeu com erro e as rotinas nao levantaram")

    return asyncio.run(ler())


def test_a_sala_fora_nao_conta_rodada_do_mudou_parou_como_os_documentos_dizem(monkeypatch: pytest.MonkeyPatch) -> None:
    # Codigo: na leitura das declaracoes, a sala fora vira 503 `sala_fora` no despachante...
    listar = inspect.getsource(servico.Despachante._listar)
    sala_fora_no_listar = "if linha == LINHA_SALA_FORA:\n                raise DeclaracoesNaoConferidas(\n"
    assert sala_fora_no_listar + '                    "sala_fora"' in listar
    assert issubclass(servico.DeclaracoesNaoConferidas, servico.DeclaracoesIndisponiveis)
    fonte_app = (RAIZ / "src/mcp_claro/despachante/app.py").read_text(encoding="utf-8")
    assert (
        'except DeclaracoesNaoConferidas as erro:\n            return JSONResponse({"motivo": erro.motivo, "mensagem":'
        " str(erro)}, status_code=503)\n        except DeclaracoesIndisponiveis as erro:"
    ) in fonte_app
    # ... as rotinas leem esse 503 (e o motivo, com qualquer status de erro) como falha do MCP-CLARO...
    sala_fora = _declaracoes_com(503, {"motivo": "sala_fora", "mensagem": "x"})
    assert isinstance(sala_fora, mudou_parou.ConferenciaNaoFeita)
    assert isinstance(_declaracoes_com(502, {"motivo": "sala_fora"}), mudou_parou.ConferenciaNaoFeita)
    nao_entregou = _declaracoes_com(502, {"motivo": "declaracoes_indisponiveis"})
    assert not isinstance(nao_entregou, mudou_parou.ConferenciaNaoFeita)
    # ... e o mudou-parou nao soma nem zera a contagem com ela; o servidor que nao entregou (502) soma uma rodada.
    assert _rodada(monkeypatch, sala_fora) == []
    assert _rodada(monkeypatch, nao_entregou) == ["somou"]
    # Documentos.
    assert tem("saida.md", "A conferência do mudou-parou não conta essa rodada contra os servidores de terceiros.")
    assert tem(
        "decisoes.md",
        "Na leitura das declarações (revisão e mudou-parou), com a sala de visitas fora, a resposta do despachante"
        " é 503 `sala_fora`.",
    )
    assert tem("decisoes.md", 'As rotinas não contam essas rodadas como "sem resposta".')
    assert tem(
        "decisoes.md",
        "Uma rodada que o MCP-CLARO não conseguiu fazer não conta nem zera: o despachante fora do ar (sem conexão, 503"
        " ou 404 dele), a sala de visitas fora e a chave das declarações que o cofre não abriu. Conta qualquer outra"
        " falha: o servidor que não entregou as declarações (502), um 500 ou o prazo de leitura.",
    )


def test_o_orcamento_alem_do_literal_diz_como_os_tempos_foram_medidos() -> None:
    # Os seis casos da etapa 3 (de 0,6 a 28 s) foram medidos com o tracemalloc ligado; os 18 s "sem ele" eram de
    # outro caso, sem orcamento.
    item = _riscos()["O orçamento conta além do pedido literal."]
    assert (
        "levava até 28 s de tempo de relógio dentro dos 50 mil passos (seis casos, de 0,6 a 28 s). Todas essas medidas"
        " foram feitas com o `tracemalloc` ligado" in item
    )
    assert argumentos.ORCAMENTO_PASSOS == 50_000
    assert not re.search(r"\b18 s\b|sem ele", item)


def test_as_acoes_canceladas_pela_quarentena_da_sondagem_entram_no_painel() -> None:
    # Codigo: a sondagem (D29) e o mudou-parou somam acoes_paradas com o motivo da porta de saida; o contador
    # quarentenas_pela_sondagem soma 1 por quarentena.
    sondar = inspect.getsource(servico.Despachante.sondar_sistema)
    assert 'CONTADORES.somar("quarentenas_pela_sondagem")\n' in sondar
    assert '"acoes_paradas", valor=len(quarentena.paradas), motivo=cadastro_quarentena.MOTIVO_PARADA' in sondar
    assert servico.cadastro_quarentena.MOTIVO_PARADA == MOTIVO_PARADA
    rodada = inspect.getsource(mudou_parou)
    assert 'CONTADORES.somar("acoes_paradas", valor=len(feita.paradas), motivo=cadastro_quarentena.MOTIVO_PARADA)' in (
        rodada
    )
    assert tem(
        "decisoes.md",
        "as da quarentena do mudou-parou, as da quarentena da sondagem (D29) e as que a porta de saída cancela por"
        " isso. Cada uma também fica no extrato da pessoa. O contador `quarentenas_pela_sondagem` conta as"
        " quarentenas, não as ações.",
    )
    assert not tem("decisoes.md", "não entram nessa série")


def test_o_que_o_documento_diz_da_leitura_e_o_que_o_despachante_faz() -> None:
    # Cofre que nao abre a chave: recusa legivel nos dois caminhos (chave por pessoa e compartilhada).
    recusa = '"chave_indisponivel", MENSAGEM_CHAVE_INDISPONIVEL'
    assert recusa in inspect.getsource(servico.Despachante._chave_da_pessoa)
    assert recusa in inspect.getsource(servico.Despachante.ler)
    assert tem("decisoes.md", "Vale para a chave por pessoa e para a compartilhada.")
    # Pedido do sistema na leitura: o contador com onde=leitura, e o resultado depois do fio antigo descartado (D12).
    assert '("pedidos_do_sistema_barrados", {"tipo": p, "onde": "leitura"})' in inspect.getsource(servico)
    assert tem("decisoes.md", "O contador `pedidos_do_sistema_barrados` soma, com `onde=leitura`.")
    assert tem("decisoes.md", "o resultado que vem depois de um pedido pelo fio antigo é descartado (D12)")


def test_o_que_a_porta_de_saida_diz_da_quarentena_e_do_alerta_e_o_que_o_codigo_faz() -> None:
    # A ordem das travas: a do servidor, os limites, a fila.
    fonte = inspect.getsource(quarentena_da_porta.por_em_quarentena)
    assert (
        fonte.index("travas.exclusiva(")
        < fonte.index("fila.suspender_limites(")
        < fonte.index("fila.parar_por_quarentena(")
    )
    assert tem("saida.md", "Nessa ordem: primeiro a trava do servidor, depois os limites, depois a fila")
    # B7: o texto de fora de ferramenta propria marcada conta sempre; o de terceiro, so com a chave ou pessoa de teste.
    assert "AND (g.tipo <> 'texto_de_fora' OR s.origem = 'proprio' OR CAST(:t AS boolean))" in sinais._SQL_ALERTA
    assert tem(
        "saida.md",
        "O texto de fora vindo de um servidor de terceiros só conta com a chave da Fase 3 ligada ou para pessoa de"
        " teste; o de ferramenta própria marcada conta sempre.",
    )


def test_o_andamento_diz_o_que_falta_na_fase_3() -> None:
    assert tem(
        "andamento.md",
        "Falta a etapa 6: as provas de saída de ponta a ponta, os ensaios antes de ligar e uma revisão cruzada.",
    )


def test_a_janela_de_dns_nao_promete_e_lembra_a_troca_de_chave() -> None:
    dns = _riscos()["Janela de DNS."]
    assert "impede" not in dns
    assert "O https com certificado público dificulta o desvio para um endereço interno" in dns
    assert (
        "A troca de chave do OAuth, que sai do despachante e não da sala, só conta com o https e a conferência" in dns
    )
