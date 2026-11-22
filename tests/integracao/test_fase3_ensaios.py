"""Ensaios antes de ligar a chave da Fase 3 (G9b; desenho 6.2, ultimo paragrafo; critica M5).

Todos pelo gateway inteiro (``tests/apoio_g9b.py``): o aplicativo fala com a entrada de verdade, com o cracha; o
despachante de verdade fala com o terceiro so pela sala de visitas; cada papel usa o proprio usuario do banco.
A chave da Fase 3 liga pelo construtor (a recusa D6 da partida fica).

1. Duble mal-intencionado: um terceiro devolve uma instrucao escondida (aberta, em letras invisiveis, com moldura e
   aviso forjados, no recurso, nos dados estruturados e no ``_meta``). A IA enganada le o terceiro, le o cadastro com
   CPF e tenta mandar uma mensagem dentro do limite: a acao vai para a ficha pelo alerta dos tres sinais, nada sai,
   nada do limite e gasto. O texto do terceiro chega ao aplicativo so dentro da marca, com a sala de verdade.
2. Resposta perdida, com ``tests/cortador.py`` entre o despachante e a sala e entre a sala e o terceiro: o terceiro
   executa uma vez so, o desfecho e "nao confirmada", nada se repete e o pedido igual devolve o mesmo protocolo.
3. Suite oficial de conformidade: roda fora do pytest (``python -m conformidade.gateway``, direto e pelo portao);
   ``test_conformidade_gateway.py`` segue com o fixture proprio. Nada aqui.
4. Prova dinamica de isolamento (critica M5): o que ``test_sala_isolada.py`` nao cobre (entrada, rotinas e login no
   ar, por nome e por IP em cada rede), com a mesma fixture.
5. Carga e restauracao: a sala sob pressao (cada resposta volta a quem pediu, nada se perde nem se repete) e a copia
   logica de um banco com terceiros (dados, guardas e GRANTs da 0006 voltam; a acao do terceiro para).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import mcp_types as types
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import ensaio_mal_intencionado as mal
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.continuidade import conferir_continuidade
from mcp_claro.despachante import chaves
from mcp_claro.extrato import fechar
from mcp_claro.regras import estranhos
from mcp_claro.regras.escondidos import escondido, marcar_escondidos
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import Operacao
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests import apoio_g9b as g9b
from tests import apoio_terceiros as t3
from tests import apoio_visitas
from tests.apoio_g9b import Gateway, estado, protocolo
from tests.apoio_visitas import Certificados
from tests.cifra_falsa import CifraFalsa
from tests.cortador import Cortador, Modo
from tests.integracao import test_continuidade as tcont
from tests.integracao import test_continuidade_restauracao as restauracao
from tests.integracao import test_sala_isolada as sala_isolada

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

SO_EQUIPE = [{"campo": "destinatario", "valores": ["equipe"]}]
# O pedido de cada salto, como o cortador o ve: o caminho da sala no salto do despachante, o metodo MCP no salto da
# sala ao terceiro.
CHAMADA_NA_SALA = b"/interno/visitas/chamar"
CHAMADA_NO_TERCEIRO = b"tools/call"
# As chaves do ``_meta`` que o MCP-CLARO poe numa leitura de terceiro: nenhuma outra passa.
META_NOSSO = {
    PREFIXO_META + "sistema",
    PREFIXO_META + "selo",
    estranhos.META_ORIGEM,
    estranhos.META_TEXTO_DE_ESTRANHOS,
    estranhos.META_MARCA,
}
SERVER_INFO = "io.modelcontextprotocol/serverInfo"
MOLDURA = re.compile(r"[\[\uff3b]\s*[\[\uff3b]\s*[/\uff0f]?\s*estranhos", re.IGNORECASE)


@pytest.fixture(scope="module")
def certificados(tmp_path_factory: pytest.TempPathFactory) -> Certificados:
    return apoio_visitas.gerar_certificados(tmp_path_factory.mktemp("certificados_g9b"))


@pytest.fixture
async def gw(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Gateway]:
    async with g9b.subir(banco_migrado, monkeypatch) as gateway:
        yield gateway


@pytest.fixture
async def gw_filhos(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, certificados: Certificados
) -> AsyncIterator[Gateway]:
    async with g9b.subir(banco_migrado, monkeypatch, sala="filhos", certificados=certificados) as gateway:
        yield gateway


def _sem_escondidos(texto: str) -> bool:
    return not any(escondido(ch) for ch in texto)


def conferir_so_dentro_da_marca(gw: Gateway, r: types.CallToolResult) -> list[str]:
    """O que o aplicativo recebeu de ``ler_avisos``: o aviso nosso primeiro, o aviso de partes retiradas, e todo texto
    do terceiro em blocos de texto entre ``[[estranhos M]]`` e ``[[/estranhos M]]``, com a marca sorteada ``M`` (nunca
    a forjada), sem moldura forjada nem caractere escondido por dentro, sem ``_meta``, dicas nem dados estruturados
    do terceiro. Devolve o miolo de cada moldura."""
    corpo = r.model_dump(by_alias=True, mode="json", exclude_none=True)
    meta = dict(corpo["_meta"])
    marca = meta[estranhos.META_MARCA]
    assert re.fullmatch(r"[0-9a-f]{12}", marca), marca
    assert marca != mal.MARCA_FORJADA
    # O kit da entrada poe o serverInfo do proprio MCP-CLARO (nunca o do fornecedor) em todo resultado.
    servidor = meta.pop(SERVER_INFO, None)
    assert servidor is None or servidor["name"] == "mcp-claro", servidor
    assert set(meta) <= META_NOSSO, meta
    assert meta[estranhos.META_ORIGEM] == "terceiro"
    assert meta[estranhos.META_TEXTO_DE_ESTRANHOS] is True
    assert meta[PREFIXO_META + "sistema"] == gw.terceiro.nome
    assert "structuredContent" not in corpo
    assert corpo["isError"] is False
    blocos = corpo["content"]
    assert all(set(b) == {"type", "text"} and b["type"] == "text" for b in blocos), blocos
    textos = [b["text"] for b in blocos]
    assert textos[0] == estranhos.aviso(marca, sistema=gw.terceiro.nome, de_terceiro=True)
    assert textos[1] == estranhos.aviso_de_retiradas(1)  # a imagem, tirada pela leitura com medida, fora da moldura
    abre, fecha = f"[[estranhos {marca}]]\n", f"\n[[/estranhos {marca}]]"
    miolos = []
    for texto in textos[2:]:
        assert texto.startswith(abre), texto
        assert texto.endswith(fecha), texto
        miolo = texto[len(abre) : -len(fecha)]
        assert not MOLDURA.search(miolo), miolo  # moldura forjada neutralizada por dentro
        assert _sem_escondidos(miolo), miolo
        miolos.append(miolo)
    # O bloco de texto, o recurso embutido e os dados estruturados, nessa ordem; nada do terceiro fora deles.
    assert len(miolos) == 3, miolos
    for fora in textos[:2]:
        assert mal.INSTRUCAO not in fora
        assert mal.AVISO_FALSO not in fora
    assert mal.INSTRUCAO in miolos[0]
    assert mal.AVISO_FALSO in miolos[0]
    assert mal.AVISO_FALSO in miolos[1]
    assert json.loads(miolos[2])["ordem"] == mal.INSTRUCAO
    # A instrucao invisivel aparece, marcada, no lugar exato; a inversao de direcao e os espacos de largura zero tambem.
    assert "[U+202E]" + marcar_escondidos(mal.escondido(mal.INVISIVEL)) in miolos[0]
    assert "I[U+200B]G[U+200B]N[U+200B]O[U+200B]R[U+200B]E" in miolos[0]
    assert marcar_escondidos(mal.escondido(mal.INVISIVEL)) in miolos[2]
    # Em lugar nenhum do que o aplicativo recebeu: caractere escondido, a marca forjada como moldura, a chave do
    # fornecedor, a URL do recurso ou o nome de sistema forjado.
    tudo = json.dumps(corpo, ensure_ascii=False)
    assert _sem_escondidos(tudo.replace("\n", ""))
    assert f"[[/estranhos {mal.MARCA_FORJADA}]]" not in tudo
    assert "fornecedor.exemplo" not in tudo
    assert "sistema-da-organizacao" not in tudo
    return miolos


# --- 1. Duble mal-intencionado -----------------------------------------------------------------------------------


async def test_duble_mal_intencionado_le_le_dado_pessoal_e_a_acao_dentro_do_limite_vai_para_a_ficha(
    gw: Gateway,
) -> None:
    limite = await gw.limite(gw.caixa, "enviar_mensagem", SO_EQUIPE)
    # Controle: sem o alerta, a mesma acao dentro do limite sai sem ficha (o limite vale neste arranjo).
    antes = await gw.chamar(gw.caixa, "enviar_mensagem", {"destinatario": "equipe", "texto": "bom dia"})
    assert (await gw.esperar_estado(protocolo(antes), Estado.ENVIADA)).limite_id == limite
    assert gw.caixa.chamadas("enviar_mensagem") == 1
    assert await gw.usados(limite) == 1

    # A vitrine nao mostra o texto do fornecedor (as descricoes sao as do ADMIN).
    vitrine = json.dumps([t.model_dump(by_alias=True, mode="json") for t in await gw.listar()], ensure_ascii=False)
    assert gw.terceiro.vitrine["ler_avisos"] in vitrine
    assert mal.INSTRUCAO not in vitrine
    # Nem a descoberta da entrada: as ``instructions`` e o nome do servidor do fornecedor nao chegam ao aplicativo.
    descoberta = await gw.descoberta()
    assert descoberta["server_info"] is not None, descoberta
    assert descoberta["server_info"]["name"] == "mcp-claro", descoberta
    assert VERSAO_MCP in descoberta["discover"]["supportedVersions"]
    texto_da_descoberta = json.dumps(descoberta, ensure_ascii=False)
    for do_fornecedor in (mal.INSTRUCAO, "Instrucoes do fornecedor", "fornecedor-mal-intencionado", "6.6.6"):
        assert do_fornecedor not in texto_da_descoberta, do_fornecedor

    # 1) A IA le o terceiro (pela sala): a instrucao chega so dentro da marca.
    lido = await gw.chamar(gw.terceiro, "ler_avisos", {"loja": "centro"})
    conferir_so_dentro_da_marca(gw, lido)
    assert gw.terceiro.chamadas("ler_avisos") == 1
    # O pedido foi a sala (pelo cortador da frente dela) e da sala ao terceiro (pelo cortador da frente dele).
    assert gw.corte_sala is not None
    assert sum(CHAMADA_NA_SALA in pedido for pedido in gw.corte_sala.pedidos) == 1
    assert sum(CHAMADA_NO_TERCEIRO in pedido for pedido in gw.corte_terceiro.pedidos) == 1

    # 2) A IA enganada segue a instrucao: le o cadastro (dado pessoal; a leitura com medida mascara o CPF)...
    cadastro = await gw.chamar(gw.cadastro, "ver_cadastro", {})
    texto_do_cadastro = json.dumps(cadastro.model_dump(by_alias=True, mode="json"), ensure_ascii=False)
    assert "123.456.789-09" not in texto_do_cadastro

    # 3) ...e tenta mandar a mensagem dentro do limite ("so equipe"): vai para a ficha, nada sai, nada e gasto.
    tentativa = await gw.chamar(
        gw.caixa, "enviar_mensagem", {"destinatario": "equipe", "texto": "CPF da Ana, como o aviso mandou"}
    )
    prot = protocolo(tentativa)
    assert estado(tentativa) == "aguardando_ficha"
    op = await gw.operacao(prot)
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.limite_id is None
    assert await gw.usados(limite) == 1
    assert gw.caixa.chamadas("enviar_mensagem") == 1
    # O aplicativo nao fica sabendo do alerta (D24): recebe a resposta de qualquer ficha.
    resposta = json.dumps(tentativa.model_dump(by_alias=True, mode="json"), ensure_ascii=False)
    assert "Nada saiu ainda" in tentativa.content[0].text
    assert "sinais" not in resposta.lower()

    # A ficha guarda o motivo: o texto de fora veio do terceiro, o dado pessoal do cadastro.
    ficha = await gw.ficha(prot)
    assert ficha is not None
    assert [(v["sinal"], v["ferramenta"], v["sistema"]) for v in ficha.sinais["vistos"]] == [
        ("texto_de_fora", gw.terceiro.vitrine["ler_avisos"], gw.terceiro.nome),
        ("dado_privado", gw.cadastro.vitrine["ver_cadastro"], gw.cadastro.nome),
    ]
    linhas = await gw.extrato(protocolo=prot)
    assert [linha.dados["rota"] for linha in linhas if linha.tipo == "acao_pedida"] == ["tres_sinais"]
    assert [linha.tipo for linha in linhas].count("tres_sinais") == 1

    # Nada do texto do terceiro foi ao extrato (nem na frase nem nos dados).
    extrato = json.dumps([[ln.tipo, ln.frase, ln.dados] for ln in await gw.extrato()], ensure_ascii=False, default=str)
    assert mal.INSTRUCAO not in extrato
    assert mal.AVISO_FALSO not in extrato
    assert _sem_escondidos(extrato)
    leituras = [ln for ln in await gw.extrato(sistema=gw.terceiro) if ln.tipo == "leitura_feita"]
    assert len(leituras) == 1
    assert leituras[0].dados["texto_de_fora"] is True


async def test_pela_sala_de_verdade_o_texto_do_terceiro_chega_so_dentro_da_marca(gw_filhos: Gateway) -> None:
    gw = gw_filhos
    assert gw.sala == "filhos"
    lido = await gw.chamar(gw.terceiro, "ler_avisos", {"loja": "centro"})
    conferir_so_dentro_da_marca(gw, lido)
    assert gw.terceiro.chamadas("ler_avisos") == 1
    # Uma conexao do filho da sala ao terceiro, um pedido de ferramenta.
    assert sum(CHAMADA_NO_TERCEIRO in pedido for pedido in gw.corte_terceiro.pedidos) == 1


# --- 2. Resposta perdida, pela sala -----------------------------------------------------------------------------

DESFECHO_PERDIDA = "a resposta do sistema nao chegou inteira"


async def pedir_confirmar_e_perder(gw: Gateway, corte: Cortador, recado: str, modo: Modo) -> Operacao:
    """Pede ``enviar_recado`` pelo aplicativo, confirma a ficha e corta a resposta no ``corte``.

    O cortador corta toda conexao nova enquanto esta no modo de corte. O modo so fica ligado da confirmacao ate o
    estado final da acao: nessa janela, so a chamada desta acao passa por ele (o arranjo nao le nem lista nada), e
    um reenvio tambem seria cortado e contado em ``cortes``."""
    r = await gw.chamar(gw.terceiro, "enviar_recado", {"recado": recado})
    assert estado(r) == "aguardando_ficha"
    op = await gw.operacao(protocolo(r))
    corte.modo = modo
    try:
        await gw.confirmar(op)
        final = await gw.esperar_estado(op.protocolo, Estado.NAO_CONFIRMADA, Estado.ENVIADA, Estado.NAO_SAIU)
    finally:
        corte.modo = "inteiro"
    assert final.estado == Estado.NAO_CONFIRMADA, final.desfecho
    assert final.desfecho == DESFECHO_PERDIDA
    assert final.resultado is None
    assert corte.cortes == 1
    return final


async def enviar_e_esperar(gw: Gateway, recado: str) -> None:
    """Outra acao ao mesmo terceiro, que sai inteira: a porta de saida passou de novo pela fila depois do corte."""
    r = await gw.chamar(gw.terceiro, "enviar_recado", {"recado": recado})
    op = await gw.operacao(protocolo(r))
    await gw.confirmar(op)
    assert (await gw.esperar_estado(op.protocolo, Estado.ENVIADA, Estado.NAO_CONFIRMADA, Estado.NAO_SAIU)).estado == (
        Estado.ENVIADA
    )
    assert gw.terceiro.chamadas(f"recado:{recado}") == 1


async def nada_se_repete(gw: Gateway, op: Operacao, recado: str) -> None:
    """Depois da resposta perdida: o terceiro executou uma vez so, nada sai de novo (nem com a porta trabalhando
    depois, nem com o pedido igual do aplicativo), e o pedido igual devolve o mesmo protocolo."""
    assert gw.terceiro.chamadas(f"recado:{recado}") == 1
    await enviar_e_esperar(gw, f"marcador 1 {gw.sufixo}")
    assert gw.terceiro.chamadas(f"recado:{recado}") == 1
    assert (await gw.operacao(op.protocolo)).estado == Estado.NAO_CONFIRMADA

    de_novo = await gw.chamar(gw.terceiro, "enviar_recado", {"recado": recado})
    meta = dict(de_novo.meta or {})
    assert meta[g9b.PROTOCOLO] == op.protocolo
    assert meta[g9b.ESTADO] == "nao_confirmada"
    assert meta[PREFIXO_META + "repetido"] is True
    assert "Estamos confirmando" in de_novo.content[0].text
    assert "Nada sera reenviado" in de_novo.content[0].text
    consulta = await gw.consultar(op.protocolo)
    assert dict(consulta.meta or {})[g9b.ESTADO] == "nao_confirmada"
    await enviar_e_esperar(gw, f"marcador 2 {gw.sufixo}")
    assert gw.terceiro.chamadas(f"recado:{recado}") == 1
    assert (await gw.operacao(op.protocolo)).estado == Estado.NAO_CONFIRMADA

    tipos = [linha.tipo for linha in await gw.extrato(protocolo=op.protocolo)]
    assert tipos.count("acao_nao_confirmada") == 1, tipos
    assert "acao_enviada" not in tipos


async def _perdida_na_sala(gw: Gateway, modo: Modo) -> None:
    corte = gw.corte_sala
    assert corte is not None
    # Com ``filhos-http``, quem esta atras do cortador e a sala de verdade (``SalaContada``); com ``processo``, nao.
    sala_de_verdade = gw.sala_de_verdade
    assert (sala_de_verdade is not None) == (gw.sala == "filhos-http"), gw.sala
    recado = f"perdida na sala {modo} {gw.sufixo}"
    op = await pedir_confirmar_e_perder(gw, corte, recado, modo)
    # A sala chamou o terceiro (uma conexao, um pedido de ferramenta) e respondeu; a resposta da sala se perdeu.
    assert sum(CHAMADA_NO_TERCEIRO in pedido for pedido in gw.corte_terceiro.pedidos) == 1
    assert sum(CHAMADA_NA_SALA in pedido for pedido in corte.pedidos) == 1
    if sala_de_verdade is not None:
        # O pedido que o cortador viu chegou ao pai de verdade, e um processo filho o atendeu (chamou o terceiro e
        # escreveu a resposta): o corte foi depois do filho, na volta ao despachante.
        assert sala_de_verdade.desfechos(recado) == ["filho"], sala_de_verdade.atendidas
    await nada_se_repete(gw, op, recado)
    assert corte.cortes == 1
    if sala_de_verdade is not None:
        # Nada se repetiu pelo pai de verdade: a mesma acao nao foi a nenhum outro filho; as outras acoes, sim.
        assert sala_de_verdade.desfechos(recado) == ["filho"], sala_de_verdade.atendidas
        for marcador in (f"marcador 1 {gw.sufixo}", f"marcador 2 {gw.sufixo}"):
            assert sala_de_verdade.desfechos(marcador) == ["filho"], sala_de_verdade.atendidas


@pytest.mark.parametrize("modo", ["sem_resposta", "meio_do_corpo", "engolir"])
async def test_resposta_perdida_entre_o_despachante_e_a_sala_nao_repete(gw: Gateway, modo: Modo) -> None:
    await _perdida_na_sala(gw, modo)


@pytest.mark.parametrize("modo", ["sem_resposta", "meio_do_corpo", "engolir"])
async def test_resposta_perdida_entre_o_despachante_e_a_sala_de_verdade_nao_repete(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, modo: Modo
) -> None:
    """O mesmo corte na frente do pai de verdade (um filho por chamada), sem TLS para o cortador ver o pedido: o
    filho chamou o terceiro e a resposta da sala se perdeu no caminho ao despachante."""
    async with g9b.subir(banco_migrado, monkeypatch, sala="filhos-http") as gw:
        assert gw.sala == "filhos-http"
        await _perdida_na_sala(gw, modo)


@pytest.mark.parametrize(
    ("sala", "modo"), [("processo", "sem_resposta"), ("processo", "meio_do_corpo"), ("filhos", "sem_resposta")]
)
async def test_resposta_perdida_entre_a_sala_e_o_terceiro_nao_repete(
    banco_migrado: Parametros,
    monkeypatch: pytest.MonkeyPatch,
    certificados: Certificados,
    sala: g9b.Sala,
    modo: Modo,
) -> None:
    async with g9b.subir(banco_migrado, monkeypatch, sala=sala, certificados=certificados) as gw:
        recado = f"perdida no terceiro {modo} {gw.sufixo}"
        op = await pedir_confirmar_e_perder(gw, gw.corte_terceiro, recado, modo)
        # O pedido chegou inteiro ao terceiro (que executou) e a resposta dele nao voltou inteira a sala.
        assert sum(CHAMADA_NO_TERCEIRO in pedido for pedido in gw.corte_terceiro.pedidos) == 1
        await nada_se_repete(gw, op, recado)
        assert gw.corte_terceiro.cortes == 1


# --- 4. Prova dinamica de isolamento (critica M5) ---------------------------------------------------------------
#
# ``test_sala_isolada.py`` sobe so o banco, o cofre, o despachante e a sala: entrada, rotinas e login nem estao no ar,
# entao o "sem nome" deles de dentro da sala nao prova muito, e nenhum IP deles (nem as redes ``saida`` e ``borda``)
# e tentado. Aqui, com a mesma fixture (``montar_projeto``) e a imagem do gateway, os tres sobem tambem, e de dentro
# da sala cada um e tentado pelo nome e por cada IP que tem em cada rede, como o despachante fora da rede ``visitas``
# e o correio. A rota recusa na hora (nao e so falta de resposta). O resto da lista da M5 (banco, cofre,
# despachante:9443 sem certificado, 169.254.169.254 e os IPs privados da ``saida_visitas``) fica com
# ``test_sala_isolada.py::test_de_dentro_da_sala_nada_alem_do_despachante_e_da_internet``, que roda a cada mudanca.

projeto = pytest.fixture(scope="module", name="projeto")(sala_isolada.montar_projeto)

# Servico -> (porta, redes em que ele esta no Compose, fora as da sala).
NO_AR: dict[str, tuple[int, tuple[str, ...]]] = {
    "entrada": (8443, ("interna",)),
    "rotinas": (9444, ("interna", "saida")),
    "login": (8080, ("interna", "borda")),
    "despachante": (9443, ("interna", "saida")),
    "correio": (8025, ("saida",)),
}


@pytest.fixture(scope="module")
def projeto_no_ar(projeto: sala_isolada.Projeto) -> sala_isolada.Projeto:
    projeto.compose("up", "-d", "--wait", "entrada", "rotinas", "login", prazo=900)
    return projeto


def test_de_dentro_da_sala_entrada_rotinas_login_no_ar_nao_alcancaveis_por_nome_nem_por_ip(
    projeto_no_ar: sala_isolada.Projeto,
) -> None:
    p = projeto_no_ar
    rodando = set(p.compose("ps", "--services", "--status", "running").stdout.split())
    assert {"entrada", "rotinas", "login", "despachante", "correio", "visitas"} <= rodando, rodando
    por_ip: dict[str, tuple[str, int]] = {}
    for servico, (porta, redes) in NO_AR.items():
        for rede in redes:
            por_ip[f"{servico}@{rede}"] = (p.ip(servico, rede), porta)
    # O lado do host de cada rede (o roteador do Docker) tambem nao.
    for rede in ("interna", "saida", "borda"):
        cfg = p.rede(rede)
        por_ip[f"host@{rede}"] = (cfg.get("Gateway") or sala_isolada._mais_um(cfg["Subnet"], 1), 22)
    por_nome = [("entrada", 8443), ("rotinas", 9444), ("login", 8080), ("correio", 8025)]
    alvos = [*por_nome, *por_ip.values(), ("despachante", 9443)]
    vistos = p.json("exec", "-T", "visitas", "python", "-c", sala_isolada.SONDA_DA_SALA, json.dumps(alvos))
    for host, porta in por_nome:
        assert vistos[f"{host}:{porta}"] == "sem_nome", (host, vistos)
    for nome, (host, porta) in por_ip.items():
        assert vistos[f"{host}:{porta}"] in sala_isolada.ROTA_RECUSADA, (nome, host, vistos)
    # O despachante continua alcancavel so pela rede ``visitas`` (e o TLS dele recusa a sala sem o certificado certo).
    assert vistos["despachante:9443"] == "conectou"
    assert vistos["tls_sem_certificado"] in ("recusou", "fechou")
    assert vistos["tls_com_certificado_da_sala"] in ("recusou", "fechou")


# --- 5. Carga: pressao pela sala de verdade ---------------------------------------------------------------------
#
# Nao ha teste de carga no repositorio (o ``locust`` do plano ainda nao entrou); os de restauracao rodam com a chave
# ligada pelo construtor (ver o relatorio do G9b). O caso de terceiro que falta e o da sala sob pressao: varias
# chamadas ao mesmo tempo, de duas pessoas, cada uma num processo filho, e acoes confirmadas juntas. Cada resposta
# volta a quem pediu (o terceiro escreve o nome da pessoa dona da chave), com uma marca so dela, e nada se perde nem
# se repete. Nada aqui mede tempo.

LEITURAS_AO_MESMO_TEMPO = 8
ACOES_AO_MESMO_TEMPO = 4


async def test_sob_pressao_pela_sala_de_verdade_cada_resposta_volta_a_quem_pediu_e_nada_se_perde(
    gw_filhos: Gateway,
) -> None:
    gw = gw_filhos
    quem = [("ana", "bia")[i % 2] for i in range(LEITURAS_AO_MESMO_TEMPO)]
    lidos = await asyncio.gather(
        *(gw.chamar(gw.terceiro, "ler_avisos", {"loja": f"loja {i}"}, pessoa=p) for i, p in enumerate(quem))
    )
    marcas = set()
    for pessoa, lido in zip(quem, lidos, strict=True):
        miolos = conferir_so_dentro_da_marca(gw, lido)
        outra = "bia" if pessoa == "ana" else "ana"
        assert miolos[0].startswith(f"Avisos do dia para {pessoa}:"), miolos[0]
        assert f"para {outra}:" not in json.dumps(lido.model_dump(by_alias=True, mode="json"))
        marcas.add(dict(lido.meta or {})[estranhos.META_MARCA])
    assert len(marcas) == LEITURAS_AO_MESMO_TEMPO  # uma marca sorteada por resposta
    assert gw.terceiro.chamadas("ler_avisos") == LEITURAS_AO_MESMO_TEMPO
    leituras = [ln for ln in await gw.extrato(sistema=gw.terceiro) if ln.tipo == "leitura_feita"]
    assert sorted(str(ln.pessoa_id) for ln in leituras) == sorted(str(gw.pessoas[p]) for p in quem)

    recados = [f"pressao {i} {gw.sufixo}" for i in range(ACOES_AO_MESMO_TEMPO)]
    donos = [("ana", "bia")[i % 2] for i in range(ACOES_AO_MESMO_TEMPO)]
    pedidos = await asyncio.gather(
        *(gw.chamar(gw.terceiro, "enviar_recado", {"recado": r}, pessoa=p) for r, p in zip(recados, donos, strict=True))
    )
    ops = [await gw.operacao(protocolo(r)) for r in pedidos]
    assert len({op.protocolo for op in ops}) == ACOES_AO_MESMO_TEMPO
    await asyncio.gather(*(gw.confirmar(op, p) for op, p in zip(ops, donos, strict=True)))
    finais = await asyncio.gather(
        *(gw.esperar_estado(op.protocolo, Estado.ENVIADA, Estado.NAO_CONFIRMADA, Estado.NAO_SAIU) for op in ops)
    )
    assert [f.estado for f in finais] == [Estado.ENVIADA] * ACOES_AO_MESMO_TEMPO, [f.desfecho for f in finais]
    assert [gw.terceiro.chamadas(f"recado:{r}") for r in recados] == [1] * ACOES_AO_MESMO_TEMPO
    assert gw.terceiro.chamadas("enviar_recado") == ACOES_AO_MESMO_TEMPO


# --- 5. Restauracao com servidores de terceiros -----------------------------------------------------------------
#
# Os testes de restauracao de hoje (``test_continuidade_restauracao.py``) fazem a copia logica de um banco sem nada
# da Fase 3. Aqui, a copia e de um banco com dois terceiros (um revisado e publicado, outro em quarentena), sinais,
# a chave de leitura das declaracoes e uma acao a um terceiro liberada; ela volta pelo roteiro (``logico.sh`` e
# ``restaurar-logico.sh``, num ensaio no mesmo servidor). Prova: os dados da Fase 3 voltam iguais, as guardas e os
# GRANTs da migracao 0006 continuam valendo no banco restaurado, e o despachante percebe a restauracao e para a acao
# do terceiro, que nao sai sem nova confirmacao.


PAPEIS = ("mcp_claro_entrada", "mcp_claro_despachante", "mcp_claro_rotinas", "grafana_leitor")
V_TERCEIROS = "SELECT situacao, revisado, sistemas FROM medicoes.v_terceiros ORDER BY 1, 2"


@dataclass
class CopiaFase3:
    copia: restauracao.Copia
    terceiro: uuid.UUID
    em_quarentena: uuid.UUID
    sinal_recente: int
    protocolo: str
    retrato: dict[str, list[tuple[str, ...]]]


async def retrato_fase3(p: Parametros) -> dict[str, list[tuple[str, ...]]]:
    """Os dados da Fase 3 do banco, como texto, para comparar a origem com a copia restaurada."""
    consultas = {
        "revisoes_servidor": "SELECT id, sistema_id, versao, impressao, declaradas, publicadas, fornecedor,"
        " revisado_por FROM revisoes_servidor ORDER BY id",
        "sistemas": "SELECT id, origem, situacao, motivo, revisao_servidor_id, sondagem_desfecho FROM sistemas"
        " WHERE origem = 'terceiro' ORDER BY id",
        "revisadas": "SELECT id, sistema_id, situacao, texto_de_fora, etiquetas, definicao_declarada"
        " FROM ferramentas_revisadas ORDER BY id",
        "sinais": "SELECT id, pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id, visto_em FROM sinais"
        " ORDER BY id",
        "chaves": "SELECT sistema_id, tipo, cifrada FROM chaves_cifradas ORDER BY sistema_id, tipo",
        "v_terceiros": V_TERCEIROS,
    }
    dono = tcont.engine_como(p)
    try:
        async with dono.connect() as c:
            return {
                nome: [tuple(str(v) for v in linha) for linha in (await c.execute(text(sql))).all()]
                for nome, sql in consultas.items()
            }
    finally:
        await dono.dispose()


async def _preparar_origem_fase3(origem: Parametros) -> tuple[uuid.UUID, uuid.UUID, int, str, int, int, int, uuid.UUID]:
    dono = tcont.engine_como(origem)
    despachante = tcont.engine_como(origem, "mcp_claro_despachante")
    cifra = CifraFalsa()
    try:
        async with dono.begin() as c:
            app = await repositorio.habilitar_aplicativo(c, client_id="app-g9b", nome="Aplicativo g9b", familia="g9b")
            pessoa = (await repositorio.pessoa_no_login(c, "https://idp.g9b.teste", "pessoa-g9b", "Pessoa")).id
            terceiro = await t3.cadastrar_terceiro(c, nome="terceiro-g9b-publicado")
            ler = await t3.revisar_ferramenta(c, terceiro, t3.definicao("ler_avisos"), nome_vitrine="ler_avisos_g9b")
            enviar = await t3.revisar_ferramenta(
                c, terceiro, t3.definicao("enviar_recado"), nome_vitrine="enviar_recado_g9b", classe="muda"
            )
            await t3.gravar_revisao_do_servidor(c, terceiro)
            parado = await t3.cadastrar_terceiro(c, nome="terceiro-g9b-quarentena")
            await t3.revisar_ferramenta(c, parado, t3.definicao("ler_mural"), nome_vitrine="ler_mural_g9b")
            await t3.gravar_revisao_do_servidor(c, parado)
            await t3.por_em_quarentena(c, parado, "mudou, parou: ensaio de restauracao")
            sinal = await t3.gravar_sinal(
                c, pessoa_id=pessoa, aplicativo_id=app, tipo="texto_de_fora", ferramenta_id=ler.id
            )
            await t3.gravar_sinal(c, pessoa_id=pessoa, aplicativo_id=app, tipo="dado_privado", ferramenta_id=ler.id)
            modelo = await repositorio.criar_modelo_ordem(
                c,
                nome="g9b-restauracao",
                descricao="Ordem do ensaio de restauracao",
                ferramentas=[(terceiro, "ler_avisos"), (terceiro, "enviar_recado")],
                validade_dias=30,
                criado_por="admin-g9b",
            )
            ordem = await repositorio.conceder_ordem(
                c, pessoa_id=pessoa, aplicativo_id=app, modelo_id=modelo, dias=7, max_dias=30
            )
        async with dono.begin() as c:
            await chaves.guardar(c, cifra, sistema_id=terceiro, segredo="terceiro-declaracoes", tipo="declaracoes")
            ferramenta = await repositorio.ferramenta_na_vitrine(
                c, pessoa, app, enviar.nome_vitrine, VERSAO_MCP, terceiros=True
            )
            assert ferramenta is not None
            op = await fila.registrar_pedido(
                c,
                pessoa_id=pessoa,
                aplicativo_id=app,
                ordem_id=ordem,
                ferramenta=ferramenta,
                argumentos={"recado": "antes da copia"},
                aplicativo_diz=None,
                rota=fila.Rota(),
                prazo_ficha_minutos=30,
                espera_segundos=0,
            )
            assert op.ficha_id is not None
            liberada = await fila.confirmar_ficha(
                c, op.ficha_id, pessoa_id=pessoa, com_passkey=True, espera_segundos=0, rapida_segundos=0
            )
            assert liberada.estado == Estado.LIBERADA
        async with dono.begin() as c:
            numero = (await fechar(c)).numero
        primeira = await conferir_continuidade(despachante, numero)
        assert primeira.desfecho == "primeira_partida"
        async with dono.connect() as c:
            linhas, ultimo = (await c.execute(text("SELECT count(*), max(id) FROM extrato"))).one()
        return terceiro, parado, sinal, op.protocolo, numero, int(linhas), int(ultimo), app
    finally:
        await dono.dispose()
        await despachante.dispose()


@pytest.fixture(scope="module")
def copia_fase3(banco_migrado: Parametros) -> Iterator[CopiaFase3]:
    """Banco proprio com dados da Fase 3, copiado pelo ``logico.sh`` (como ``test_continuidade_restauracao.copia``)."""
    if urlsplit(banco_migrado.DATABASE_URL).hostname not in ("localhost", "127.0.0.1"):
        pytest.skip("os scripts rodam pela rede do hospedeiro e precisam do banco em localhost")
    nome = f"origem_g9b_{uuid.uuid4().hex[:10]}"
    senha_backup = secrets.token_hex(16)
    asyncio.run(
        tcont.executar(
            banco_migrado,
            f"CREATE DATABASE {nome}",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_claro_backup') THEN "
            "CREATE ROLE mcp_claro_backup; END IF; END $$",
            f"ALTER ROLE mcp_claro_backup WITH LOGIN PASSWORD '{senha_backup}'",
        )
    )
    origem = tcont.parametros_do_banco(banco_migrado, nome)
    tcont.migrar(origem)
    terceiro, parado, sinal, prot, numero, linhas, ultimo, app = asyncio.run(_preparar_origem_fase3(origem))
    retrato = asyncio.run(retrato_fase3(origem))
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-g9b-logico-"))
    segredos, saida = raiz / "segredos", raiz / "saida"
    segredos.mkdir()
    saida.mkdir()
    (segredos / "backup_cifra").write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    (segredos / "db_backup").write_text(senha_backup + "\n", encoding="utf-8")
    (segredos / "db_migracao").write_text("senha-de-teste\n", encoding="utf-8")
    for caminho in (raiz, segredos):
        caminho.chmod(0o755)
    saida.chmod(0o777)
    for arquivo in segredos.iterdir():
        arquivo.chmod(0o644)
    host, porta = restauracao.endereco(origem)
    texto_saida = restauracao.rodar_script(
        "logico.sh",
        {
            "LOGICO_HOST": host,
            "LOGICO_PORTA": porta,
            "LOGICO_SSLMODE": "disable",
            "LOGICO_BANCOS": nome,
            "LOGICO_DESTINO": "/saida",
            "LOGICO_SENHA_ARQUIVO": "/segredos/db_backup",
            "LOGICO_CIFRA_ARQUIVO": "/segredos/backup_cifra",
        },
        {segredos: ("/segredos", "ro"), saida: ("/saida", "rw")},
    )
    restauracao.liberar_para_o_teste(saida)
    pastas = [p for p in saida.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert len(pastas) == 1, texto_saida
    copia = restauracao.Copia(origem, nome, pastas[0], segredos, numero, linhas, ultimo, app)
    try:
        yield CopiaFase3(copia, terceiro, parado, sinal, prot, retrato)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        asyncio.run(tcont.executar(banco_migrado, f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)"))


async def recusado(engine: AsyncEngine, sql: str, **params: Any) -> str:
    """Roda ``sql`` numa transacao propria e devolve a mensagem do erro do banco (falha se o banco aceitar)."""
    with pytest.raises(DBAPIError) as erro:
        async with engine.begin() as c:
            await c.execute(text(sql), params)
    return str(erro.value.orig)


async def test_copia_logica_com_terceiros_volta_com_os_dados_as_guardas_e_os_grants_da_fase_3(
    copia_fase3: CopiaFase3,
) -> None:
    f3 = copia_fase3
    ensaio = f"ensaio_g9b_{uuid.uuid4().hex[:8]}"
    saida = restauracao.restaurar(
        f3.copia,
        f3.copia.origem,
        RESTAURAR_COMO=ensaio,
        RESTAURAR_GLOBAIS="desligado",
        RESTAURAR_QUEM="Pessoa do ensaio do G9b",
    )
    assert "pronto" in saida
    p = tcont.parametros_do_banco(f3.copia.origem, ensaio)
    engines = {papel: tcont.engine_como(p, papel) for papel in (None, *PAPEIS)}
    dono = engines[None]
    try:
        # Os dados da Fase 3 voltaram iguais (a revisao vigente, a quarentena, os sinais, a chave das declaracoes).
        assert await retrato_fase3(p) == f3.retrato
        assert len(f3.retrato["revisoes_servidor"]) == 2
        assert len(f3.retrato["sinais"]) == 2

        # As guardas da 0006 continuam no banco restaurado.
        assert "so de acrescentar" in await recusado(dono, "UPDATE revisoes_servidor SET fornecedor = 'outro'")
        assert "so de acrescentar" in await recusado(dono, "DELETE FROM revisoes_servidor")
        entrada = engines["mcp_claro_entrada"]
        sai = "UPDATE sistemas SET situacao = 'pendente' WHERE id = :s"
        assert "Servidor em quarentena so sai pela nova revisao do ADMIN" in await recusado(
            entrada, sai, s=f3.em_quarentena
        )
        assert "permission denied" in await recusado(
            entrada, "UPDATE sistemas SET revisao_servidor_id = NULL WHERE id = :s", s=f3.terceiro
        )
        assert "O sinal ainda cabe na maior janela" in await recusado(
            engines["mcp_claro_rotinas"], "DELETE FROM sinais WHERE id = :i", i=f3.sinal_recente
        )
        assert "permission denied" in await recusado(engines["mcp_claro_despachante"], "SELECT count(*) FROM sinais")
        # E os GRANTs que dao o caminho certo: a entrada grava sinal, o despachante le a revisao, o painel le.
        async with entrada.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO sinais (pessoa_id, aplicativo_id, tipo, sistema_id, ferramenta_id)"
                    " SELECT pessoa_id, aplicativo_id, 'texto_de_fora', sistema_id, ferramenta_id FROM sinais"
                    " WHERE id = :i"
                ),
                {"i": f3.sinal_recente},
            )
        async with engines["mcp_claro_despachante"].connect() as c:
            assert (await c.execute(text("SELECT count(*) FROM revisoes_servidor"))).scalar_one() == 2
        async with engines["grafana_leitor"].connect() as c:
            assert [tuple(str(v) for v in linha) for linha in await c.execute(text(V_TERCEIROS))] == f3.retrato[
                "v_terceiros"
            ]

        # Percebida: o despachante fecha a saida e a acao ao terceiro que esperava para por restauracao.
        r = await conferir_continuidade(engines["mcp_claro_despachante"], f3.copia.numero_fechamento)
        assert r.motivos == ("marca_restaurado",)
        assert r.saida_fechada
        async with dono.connect() as c:
            parada = (
                await c.execute(
                    text(
                        "SELECT f.estado, o.desfecho FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"
                        " WHERE o.protocolo = :p"
                    ),
                    {"p": f3.protocolo},
                )
            ).one()
        assert (parada.estado, parada.desfecho) == ("parada_por_restauracao", "parada por restauracao")
    finally:
        for engine in engines.values():
            await engine.dispose()
        await tcont.executar(f3.copia.origem, f"DROP DATABASE IF EXISTS {ensaio} WITH (FORCE)")
