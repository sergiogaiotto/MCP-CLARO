"""Telas da pessoa, acertos finais da etapa 5 (F5a, itens 2 e 3), sem banco.

- Item 2: a acao dada como nao confirmada pela retomada, cujo fim (que chegou depois, com a marca
  ``fila.DEPOIS_DA_RETOMADA``) foi um "nao saiu" em que o pedido nem saiu do MCP-CLARO (sala fora, pedido fundo
  demais, chave que nao abriu...). A tela nao diz "O pedido saiu uma vez..." junto de "Nada saiu.": diz que a acao
  foi dada como nao confirmada antes de o envio terminar e que, pelo que o MCP-CLARO viu depois, o pedido nao chegou
  ao sistema. A lista desses fins (``acoes._NAO_CHEGOU``) e lida do codigo da porta de saida.
- Item 3: o nome do sistema aparece com o filtro ``escondidos`` e dentro de ``<bdi>`` em toda tela da pessoa que o
  mostra. O nome vem do cadastro do ADMIN, que so tira os controles ASCII (``admin.CONTROLE``): um U+202E passa.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_claro.ligacao import cliente
from mcp_claro.ligacao.ponto import pedidos_legiveis
from mcp_claro.ligacao.sala_contrato import SALA_FORA
from mcp_claro.saida import despachante, fila
from mcp_claro.saida.estados import Estado
from mcp_claro.telas import acoes, admin, limites
from mcp_claro.telas.paginas import criar_ambiente
from tests.unidade.test_despachante_classifica import FERRAMENTA_T, OP, porta
from tests.unidade.test_telas_acoes_paginas import AGORA, operacao
from tests.unidade.test_telas_acoes_paginas import _ficha as render_ficha

RLO = "\u202e"
NOME = f"fornecedor{RLO}x"
MARCADO = "fornecedor[U+202E]x"
NO_BDI = f"<bdi>{MARCADO}</bdi>"
NAO_CHEGOU = (
    "A ação foi dada como não confirmada antes de o envio terminar. Pelo que o MCP-CLARO viu depois, o pedido não"
    " chegou ao sistema {nome}."
)
SAIU_UMA_VEZ = "O pedido saiu uma vez"
BASE: dict[str, Any] = {
    "sessao": None,
    "csrf": "t",
    "aviso": None,
    "caminho": "/telas/x",
    "fase1_ligada": True,
    "erros": [],
    "valores": {},
}


def _render(modelo: str, **contexto: Any) -> str:
    return criar_ambiente("America/Sao_Paulo").get_template(modelo).render({**BASE, **contexto})


def _texto(html: str) -> str:
    """O texto da pagina, sem as etiquetas (``<bdi>`` e ``<strong>`` nao separam palavras)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


def _acao(**mudar: Any) -> str:
    base: dict[str, Any] = {"sistema_origem": "terceiro", "sistema_nome": NOME, "resultado": None}
    return _render("acoes/acao.html", a=acoes.montar(operacao(**(base | mudar)), AGORA), intervalo=2)


# --- Item 2: o fim "nao saiu" depois da retomada -------------------------------------------------------------------


def _fins_sem_envio() -> list[str]:
    """Os fins de "nao saiu" da porta de saida em que o pedido nem saiu do MCP-CLARO, lidos do codigo dela: os de
    ``_NAO_ENVIADO``, o padrao dele em ``_estado_final`` e as chaves que nao abriram (``PortaDeSaida._chave``)."""
    fins = list(despachante._NAO_ENVIADO.values())
    estado_final = ast.parse(textwrap.dedent(inspect.getsource(despachante.PortaDeSaida._estado_final)))
    for no in ast.walk(estado_final):
        if (
            isinstance(no, ast.Call)
            and isinstance(no.func, ast.Attribute)
            and no.func.attr == "get"
            and isinstance(no.func.value, ast.Name)
            and no.func.value.id == "_NAO_ENVIADO"
        ):
            padrao = no.args[1]
            assert isinstance(padrao, ast.Constant)
            fins.append(padrao.value)
    chave = ast.parse(textwrap.dedent(inspect.getsource(despachante.PortaDeSaida._chave)))
    for no in ast.walk(chave):
        if isinstance(no, ast.Return) and isinstance(no.value, ast.Tuple) and len(no.value.elts) == 3:
            primeiro, _, motivo = no.value.elts
            if isinstance(primeiro, ast.Constant) and primeiro.value is None:
                assert isinstance(motivo, ast.Constant)
                assert isinstance(motivo.value, str)
                fins.append(motivo.value)
    return fins


FINS_SEM_ENVIO = _fins_sem_envio()
RABO = f"; o sistema pediu {pedidos_legiveis([cliente.PEDIDO_RAIZES])} e o MCP-CLARO recusou"


def test_os_fins_em_que_o_pedido_nao_chegou_sao_os_da_porta_de_saida() -> None:
    """Se a porta de saida ganhar outro fim sem envio (ou mudar o texto de um), este teste avisa."""
    assert "o pedido nao chegou ao sistema" in FINS_SEM_ENVIO
    assert despachante._NAO_ENVIADO[SALA_FORA] in FINS_SEM_ENVIO
    assert "nao deu para renovar a chave" in FINS_SEM_ENVIO
    assert len(FINS_SEM_ENVIO) == len(set(FINS_SEM_ENVIO)) + 1  # "nao deu para abrir a chave no cofre" vem 2 vezes
    assert set(FINS_SEM_ENVIO) == acoes._NAO_CHEGOU


@pytest.mark.parametrize("rabo", ["", RABO])
@pytest.mark.parametrize("fim", sorted(set(FINS_SEM_ENVIO)))
def test_nao_confirmada_cujo_fim_depois_da_retomada_foi_nao_saiu_nao_diz_que_o_pedido_saiu(fim: str, rabo: str) -> None:
    desfecho = fim + rabo + fila.DEPOIS_DA_RETOMADA
    assert acoes.nao_chegou_depois_da_retomada(desfecho)
    texto = _texto(_acao(estado=Estado.NAO_CONFIRMADA, desfecho=desfecho))
    assert SAIU_UMA_VEZ not in texto
    assert "Ele pode ter chegado ao sistema" not in texto
    assert NAO_CHEGOU.format(nome=MARCADO) in texto
    assert "Nada será reenviado." in texto  # a pessoa ainda confere e registra o que apurou
    assert "O que aconteceu no sistema?" in texto
    assert f"Detalhe: {acoes.com_acento(desfecho)}." in texto


@pytest.mark.parametrize(
    "fim", [despachante._NAO_ENVIADO[SALA_FORA], despachante._NAO_ENVIADO[cliente.PEDIDO_FUNDO_DEMAIS]]
)
def test_o_pedido_saiu_uma_vez_nunca_aparece_junto_de_nada_saiu(fim: str) -> None:
    """O caso do defeito (G6br): a explicacao do fim diz "Nada saiu."; a frase do estado nao pode dizer que saiu."""
    texto = _texto(_acao(estado=Estado.NAO_CONFIRMADA, desfecho=fim + fila.DEPOIS_DA_RETOMADA))
    assert "Nada saiu." in texto
    assert SAIU_UMA_VEZ not in texto
    assert NAO_CHEGOU.format(nome=MARCADO) in texto


@pytest.mark.anyio
async def test_o_fim_sala_fora_escrito_pela_porta_de_verdade_depois_da_retomada() -> None:
    """O desfecho da porta de saida de verdade (``_classificar``) para a sala fora, com a marca da retomada."""
    resposta = cliente.Resposta("nao_enviado", excecao=SALA_FORA)
    estado, corpo, desfecho, _ = await porta()._classificar(OP, FERRAMENTA_T, resposta, None)
    assert (estado, corpo) == (Estado.NAO_SAIU, None)
    texto = _texto(_acao(estado=Estado.NAO_CONFIRMADA, desfecho=desfecho + fila.DEPOIS_DA_RETOMADA))
    assert SAIU_UMA_VEZ not in texto
    assert NAO_CHEGOU.format(nome=MARCADO) in texto
    assert "Nada saiu." in texto


@pytest.mark.parametrize(
    "desfecho",
    [
        # Sem a marca: a retomada nao deu a acao como nao confirmada antes do fim.
        "a resposta do sistema nao chegou inteira",
        "falha inesperada durante o envio",
        "o sistema devolveu erro depois de receber (codigo -32603)",
        despachante._NAO_ENVIADO[SALA_FORA],
        None,
        # Com a marca, mas o pedido saiu: o sistema respondeu ou recusou.
        "o sistema respondeu" + fila.DEPOIS_DA_RETOMADA,
        despachante.RESULTADO_FUNDO_DEMAIS + fila.DEPOIS_DA_RETOMADA,
        "o sistema recusou na porta (HTTP 401)" + fila.DEPOIS_DA_RETOMADA,
        "o sistema recusou o pedido (codigo -32602)" + fila.DEPOIS_DA_RETOMADA,
        "o sistema respondeu; antes pediu que a pessoa abrisse um link e o MCP-CLARO recusou" + fila.DEPOIS_DA_RETOMADA,
    ],
)
def test_nao_confirmada_de_sempre_continua_dizendo_que_o_pedido_saiu(desfecho: str | None) -> None:
    assert not acoes.nao_chegou_depois_da_retomada(desfecho)
    texto = _texto(_acao(estado=Estado.NAO_CONFIRMADA, desfecho=desfecho))
    assert f"{SAIU_UMA_VEZ} para o sistema {MARCADO}, mas a resposta não chegou inteira." in texto
    assert "foi dada como não confirmada antes de o envio terminar" not in texto


PENDENCIA_NAO_CHEGOU = (
    "Esta ação foi dada como não confirmada antes de o envio terminar. Pelo que o MCP-CLARO viu depois, o pedido não"
    " chegou ao sistema."
)


def _pendencias(*ops: Any) -> str:
    return _render("acoes/pendencias.html", caixa=acoes.separar(list(ops), AGORA), passkey_ligada=True)


@pytest.mark.parametrize("fim", sorted(set(FINS_SEM_ENVIO)))
def test_pendencias_dizem_da_acao_cujo_fim_depois_da_retomada_foi_nao_saiu_que_o_pedido_nao_chegou(fim: str) -> None:
    """Retoque do F5a: a caixa de pendencias diz o mesmo que a tela da acao. No grupo "Estamos confirmando", a frase
    do grupo ("O pedido pode ter chegado ao sistema...") fica, e a acao cujo fim depois da retomada foi um "nao saiu"
    sem envio diz que, pelo que o MCP-CLARO viu depois, o pedido nao chegou ao sistema. A outra, nao."""
    chegou = operacao(estado=Estado.NAO_CONFIRMADA, ficha_id=None, protocolo="MCP-AAAA-1111")
    nao_chegou = operacao(
        estado=Estado.NAO_CONFIRMADA, ficha_id=None, protocolo="MCP-BBBB-2222", desfecho=fim + fila.DEPOIS_DA_RETOMADA
    )
    texto = _texto(_pendencias(chegou, nao_chegou))
    assert texto.count(PENDENCIA_NAO_CHEGOU) == 1
    item_chegou = texto.split("MCP-AAAA-1111", 1)[1].split("Falta a sua decisão.", 1)[0]
    item_nao_chegou = texto.split("MCP-BBBB-2222", 1)[1].split("Falta a sua decisão.", 1)[0]
    assert PENDENCIA_NAO_CHEGOU in item_nao_chegou
    assert PENDENCIA_NAO_CHEGOU not in item_chegou
    assert "O pedido pode ter chegado ao sistema, mas a resposta não chegou inteira." in texto


def test_pendencias_sem_a_marca_da_retomada_ou_com_envio_nao_dizem_que_o_pedido_nao_chegou() -> None:
    for desfecho in (
        None,
        despachante._NAO_ENVIADO[SALA_FORA],  # sem a marca da retomada
        "o sistema recusou o pedido (codigo -32602)" + fila.DEPOIS_DA_RETOMADA,  # o pedido saiu
    ):
        op = operacao(estado=Estado.NAO_CONFIRMADA, ficha_id=None, desfecho=desfecho)
        assert PENDENCIA_NAO_CHEGOU not in _texto(_pendencias(op)), desfecho


def test_a_frase_nova_so_vale_na_acao_nao_confirmada() -> None:
    """Outro estado com o mesmo desfecho (nao deveria haver, mas a tela nao depende disso) nao mostra a frase."""
    desfecho = despachante._NAO_ENVIADO[SALA_FORA] + fila.DEPOIS_DA_RETOMADA
    for estado in (Estado.NAO_SAIU, Estado.ENVIADA, Estado.CANCELADA):
        texto = _texto(_acao(estado=estado, desfecho=desfecho, ficha_id=None))
        assert "foi dada como não confirmada" not in texto, estado


# --- Item 3: o nome do sistema com o filtro e dentro de <bdi>, em toda tela da pessoa ----------------------------


def _sem_rlo(html: str) -> str:
    assert RLO not in html
    return html


def _bdis(html: str) -> int:
    return _sem_rlo(html).count(NO_BDI)


def test_acao_e_estado_mostram_o_nome_do_sistema_com_a_marca_e_isolado() -> None:
    # Nao confirmada de sempre: "O quê" e a frase do estado.
    html = _acao(estado=Estado.NAO_CONFIRMADA, desfecho="a resposta do sistema nao chegou inteira")
    assert f"no sistema {NO_BDI}" in html
    assert f"{SAIU_UMA_VEZ} para o sistema {NO_BDI}, mas" in html
    assert _bdis(html) == 2
    # Nao confirmada cujo fim nao saiu: a frase nova tambem.
    html = _acao(estado=Estado.NAO_CONFIRMADA, desfecho=despachante._NAO_ENVIADO[SALA_FORA] + fila.DEPOIS_DA_RETOMADA)
    assert f"o pedido não chegou ao sistema {NO_BDI}." in html
    assert _bdis(html) == 2
    # Resposta de terceiro: "O quê" e a dica do texto de estranhos (que ja tinha o filtro).
    resultado = {"content": [{"type": "text", "text": "oi"}], "isError": False}
    html = _acao(estado=Estado.ENVIADA, desfecho="o sistema respondeu", resultado=resultado, ficha_id=None)
    assert f"o servidor de terceiros {NO_BDI}." in html
    assert _bdis(html) == 2


def test_ficha_mostra_o_nome_do_sistema_isolado() -> None:
    html = render_ficha(operacao(sistema_origem="terceiro", sistema_nome=NOME))
    assert f"no sistema <strong>{NO_BDI}</strong>, em seu nome." in html
    assert _bdis(html) == 1


def test_lista_de_acoes_mostra_o_nome_do_sistema_isolado() -> None:
    op = operacao(sistema_nome=NOME, sistema_origem="terceiro")
    html = _render("acoes/lista.html", acoes=[acoes.montar(op, AGORA)], busca="")
    assert f'<span class="dica">no sistema {NO_BDI}</span>' in html
    assert _bdis(html) == 1


def test_pendencias_mostram_o_nome_do_sistema_isolado_nos_tres_grupos() -> None:
    ficha = operacao(sistema_nome=NOME)
    espera = operacao(sistema_nome=NOME, estado=Estado.LIBERADA, ficha_id=None, hora_saida=AGORA + timedelta(minutes=5))
    confirmando = operacao(sistema_nome=NOME, estado=Estado.NAO_CONFIRMADA, ficha_id=None)
    html = _render(
        "acoes/pendencias.html", caixa=acoes.separar([ficha, espera, confirmando], AGORA), passkey_ligada=True
    )
    assert f'<span class="dica">no sistema {NO_BDI}</span>' in html
    assert html.count(f"no sistema {NO_BDI}") == 3
    assert _bdis(html) == 3


def test_ordens_mostram_o_nome_do_sistema_isolado() -> None:
    f = SimpleNamespace(descricao="Ver horário.", nome_original="ver_horario", sistema=NOME, origem="terceiro")
    o = SimpleNamespace(
        id=uuid.uuid4(), aplicativo="App", modelo="Ver", descricao="d", ferramentas=[f], fim=AGORA, ultimo_uso=None
    )
    html = _render("ordens.html", ativas=[o], terminadas=[], aplicativos=[], modelos=[], maximo_dias=30)
    assert f'<span class="dica">(sistema {NO_BDI})</span> <span class="etiqueta">de terceiros</span>' in html
    assert _bdis(html) == 1


def test_extrato_mostra_o_nome_do_sistema_isolado() -> None:
    linha = SimpleNamespace(
        criado_em=AGORA,
        frase="Acao enviada.",
        protocolo=None,
        aplicativo="App",
        sistema=NOME,
        sistema_origem="terceiro",
    )
    sem_sistema = SimpleNamespace(
        criado_em=AGORA, frase="Entrou.", protocolo=None, aplicativo=None, sistema=None, sistema_origem=None
    )
    html = _render(
        "extrato.html", linhas=[linha, sem_sistema], pagina=1, ha_mais=False, alheio=None, base="/telas/extrato"
    )
    assert f'<td>{NO_BDI} <span class="etiqueta">de terceiros</span></td>' in html
    assert "<td></td>" in html  # a linha sem sistema fica vazia, sem <bdi> vazio
    assert _bdis(html) == 1


def _linha_limite(**mudar: Any) -> Any:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "ferramenta": "ler_fatura",
        "sistema": NOME,
        "aplicativo": "App",
        "por_dia": 2,
        "usados_hoje": 0,
        "valido_ate": AGORA + timedelta(days=3),
        "situacao": "ativo",
        "motivo": None,
        "criado_em": AGORA,
        "com_passkey": True,
        "destinatarios": [],
        "sistema_origem": "terceiro",
        "sem_revisao": False,
    }
    return SimpleNamespace(**(base | mudar))


def _escolha() -> Any:
    return SimpleNamespace(
        chave="a:b",
        nome_vitrine="ler_fatura",
        aplicativo="App",
        sistema=NOME,
        de_terceiro=True,
        descricao="Lê a fatura.",
        risco="baixo",
        campos=(),
    )


def test_limites_mostram_o_nome_do_sistema_isolado() -> None:
    html = _render(
        "acoes/limites.html",
        ativos=[limites._limite(_linha_limite(), True)],
        terminados=[limites._limite(_linha_limite(situacao="encerrado"), True)],
        escolhas=[_escolha()],
        de_risco_alto=[],
        maximo_dia=5,
        maximo_dias=30,
        ligada=True,
    )
    assert f"no sistema {NO_BDI}</span>" in html
    assert _bdis(html) == 2  # o ativo e a acao que pode entrar num limite (o terminado nao mostra o sistema)


def test_limite_mostra_o_nome_do_sistema_isolado() -> None:
    html = _render(
        "acoes/limite.html",
        l=limites._limite(_linha_limite(), True),
        minimo_dia=2,
        maximo_dia=5,
        acima_do_teto=False,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )
    assert f"<dd>ler_fatura, no sistema {NO_BDI}" in html
    assert _bdis(html) == 1


def test_criar_limite_mostra_o_nome_do_sistema_isolado() -> None:
    html = _render(
        "acoes/limite_novo.html",
        e=_escolha(),
        maximo_dia=5,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )
    assert f"no sistema <strong>{NO_BDI}</strong>." in html
    assert _bdis(html) == 1


def test_autorizar_mostra_o_nome_do_sistema_isolado() -> None:
    s = SimpleNamespace(id=uuid.uuid4(), nome=NOME, autorizado_em=None, configurado=True, situacao="ativo")
    html = _render("autorizar.html", sistemas=[s], fichas={s.id: "f"})
    assert f"<h2>{NO_BDI}</h2>" in html
    assert f"você vai para a página do sistema {NO_BDI}." in html
    assert f"Autorizar no sistema {NO_BDI}</a>" in html
    assert _bdis(html) == 3


def test_o_que_parou_com_o_nome_do_sistema_aparece_isolado() -> None:
    """Pausas e bloqueios: "O sistema X, para todas as pessoas" (``bloqueio.o_que_parou``) vem montado, com o nome
    dentro; a frase inteira vai com o filtro e dentro de ``<bdi>``."""
    o_que = f"O sistema {NOME}, para todas as pessoas"
    marcado = f"<bdi>O sistema {MARCADO}, para todas as pessoas</bdi>"
    p = SimpleNamespace(
        id=uuid.uuid4(), o_que=o_que, tipo="bloqueio", desde=AGORA, quem="ADMIN", retomavel=False, encerrado_em=None
    )
    html = _sem_rlo(_render("_paradas.html", suas=[p], gerais=[p]))
    assert f"<td>{marcado}</td>" in html
    assert f"<li>{marcado}: bloqueado desde" in html
    html = _sem_rlo(_render("comprovante.html", p=p, partes=None))
    assert f"<dt>O que parou</dt><dd>{marcado}</dd>" in html


def test_o_nome_do_cadastro_deixa_passar_o_rlo() -> None:
    """Por que o filtro e o ``<bdi>``: o cadastro do ADMIN so tira os controles ASCII do nome do sistema."""
    assert admin.CONTROLE.search(NOME) is None
