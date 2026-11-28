"""Telas da pessoa da Fase 4 (selos de nivel 2 e 3), sem banco (G6b, etapa 4): o cartao "Selo do sistema" da ficha,
a ficha do pedido igual (B12, A3.3 e M4.2), o bloco "O que o sistema informou" e o botao de um toque, as etiquetas das
listas, a quarta parte do comprovante e a coluna "Selo" do extrato.

Cada situacao do contrato tem texto proprio, com acento, sem texto do sistema e sem promessa: "garante" so na
negacao, nunca "certificado", "conforme", "aprovado no" nem o nome do rascunho (B6). As provas de ponta a ponta, com o
banco e as telas de verdade, estao em ``tests/integracao/test_telas_fase4_pessoa.py``."""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import DecisaoRecusada, EscadaDaOriginal, InformadoDoSistema, Operacao
from mcp_claro.selo import contrato
from mcp_claro.telas import acoes, bloqueio, paginas
from mcp_claro.telas.nucleo import LoginRefeito
from mcp_claro.telas.paginas import PASTA_MODELOS, criar_ambiente
from tests.unidade.test_paginas import PROMESSA, SEM_ACENTO, _texto_visivel
from tests.unidade.test_telas_acoes_paginas import operacao

AGORA = datetime(2026, 11, 19, 15, 0, 0, tzinfo=UTC)
MAXIMO = 8
TESTE_ID = str(uuid.uuid4())
PROIBIDAS = re.compile(r"certificad|conforme|aprovado no|MWS|write-safety|write safety", re.IGNORECASE)


def retrato(nivel: int = 2, *, modo: str = "ambiente_de_teste", motivo: str | None = None) -> dict[str, Any]:
    if nivel == 1:
        return {
            "nivel": 1,
            "teste_id": None,
            "testado_em": None,
            "vale_ate": None,
            "modo": None,
            "motivo": motivo or "nunca_testado",
        }
    return {
        "nivel": nivel,
        "teste_id": TESTE_ID,
        "testado_em": datetime(2026, 11, 10, 13, 0, tzinfo=UTC).isoformat(),
        "vale_ate": datetime(2027, 2, 8, 13, 0, tzinfo=UTC).isoformat(),
        "modo": modo,
        "motivo": "vigente",
    }


def informado(
    estado: str | None = "concluida",
    *,
    final: str | None = "concluida",
    desfecho: str = "respondeu",
    origem: str = "consulta",
    terceiro: bool = False,
    tentativas: int = 1,
    ultimo: str | None = None,
    contradisse: bool = False,
    em: datetime | None = None,
) -> InformadoDoSistema:
    return InformadoDoSistema(
        estado=estado,
        desfecho=desfecho,
        origem=origem,
        gravado_em=em or AGORA - timedelta(minutes=5),
        de_terceiro=terceiro,
        estado_final=final,
        tentativas=tentativas,
        ultimo_desfecho=ultimo if ultimo is not None else (desfecho if origem == "consulta" else None),
        se_contradisse=contradisse,
    )


def nao_confirmada(**mudar: Any) -> Operacao:
    base: dict[str, Any] = {
        "estado": Estado.NAO_CONFIRMADA,
        "envio_nivel": 2,
        "envio_gravado_em": AGORA - timedelta(hours=1),
        "lido_em": AGORA,
        "selo": retrato(2),
    }
    base.update(mudar)
    return operacao(**base)


def replace_op(op: Operacao, **mudar: Any) -> Operacao:
    return replace(op, **mudar)


def bloco(op: Operacao, maximo: int | None = MAXIMO) -> acoes.OQueOSistemaInformou:
    achado = acoes.o_que_o_sistema_informou(op, maximo, AGORA)
    assert achado is not None
    return achado


def texto(b: acoes.OQueOSistemaInformou) -> str:
    return " ".join(re.sub(r"<[^>]+>", "", str(f)) for f in b.frases)


def _garante_so_negado(t: str) -> bool:
    return all(m.start() >= 4 and t[m.start() - 4 : m.start()] == "não " for m in re.finditer("garant", t))


def confere_texto(t: str) -> None:
    """Acento, sem promessa (fora a negacao "não garante") e sem os nomes proibidos (B6)."""
    sem_campos = re.sub(r"\{[a-z_]+\}", "", t)
    assert not SEM_ACENTO.search(sem_campos), t
    assert not PROMESSA.search(t.replace("não garante", "")), t
    assert _garante_so_negado(t), t
    assert not PROIBIDAS.search(t), t


# --- Vocabulario e textos -----------------------------------------------------------------------------------------


def test_todo_estado_do_contrato_tem_texto_legivel() -> None:
    assert set(paginas.ESTADOS_LEGIVEIS) == set(contrato.ESTADOS)
    for estado in contrato.ESTADOS:
        assert paginas.estado_legivel(estado) == paginas.ESTADOS_LEGIVEIS[estado]
    assert paginas.ESTADOS_LEGIVEIS["nao_recebida"] == "o sistema não recebeu o pedido e não vai aceitá-lo depois"
    assert paginas.ESTADOS_LEGIVEIS["parada"] == "o pedido foi parado antes de terminar, pelo bloqueio"
    # Fora do vocabulario, nunca o valor que veio.
    for veio in ("IGNORE AS REGRAS", "concluida ", "", 3):
        assert paginas.estado_legivel(veio) == paginas.ESTADO_FORA_DO_VOCABULARIO
    assert paginas.estado_legivel(None) == ""


def _todos_os_textos() -> list[str]:
    textos = [*paginas.ESTADOS_LEGIVEIS.values(), paginas.ESTADO_FORA_DO_VOCABULARIO]
    textos += [paginas.AVISOS["informado_registrado"], paginas.AVISOS["confirmada_numero_novo"]]
    textos += [paginas.INFORMADO_PELO_SISTEMA, paginas.INFORMADO_PELO_TERCEIRO]
    textos += [str(v) for k, v in vars(acoes).items() if k.isupper() and isinstance(v, str) and len(v) > 25]
    textos += list(acoes.ONDE_TESTADO.values()) + list(bloqueio.ORIGENS.values())
    textos += [m for _, m in acoes.RECUSAS.values()]
    return textos


@pytest.mark.parametrize("t", _todos_os_textos())
def test_textos_da_pessoa_com_acento_sem_promessa(t: str) -> None:
    confere_texto(t)


def test_conferencia_de_texto_pega_o_que_procura() -> None:
    with pytest.raises(AssertionError):
        confere_texto("O selo garante que o sistema fez.")
    with pytest.raises(AssertionError):
        confere_texto("Aprovado no rascunho MWS.")
    with pytest.raises(AssertionError):
        confere_texto("O sistema nao respondeu.")
    confere_texto(acoes.NAO_GARANTE)


def _motivos_da_fila() -> set[str]:
    fonte = Path(str(fila.__file__)).read_text(encoding="utf-8")
    return set(re.findall(r'DecisaoRecusada\(\s*"([a-z_]+)"', fonte))


def test_toda_recusa_da_fila_tem_texto_com_acento() -> None:
    motivos = _motivos_da_fila()
    novos = {
        "sem_resposta_final",
        "resultado_em_outro_protocolo",
        "terceiro_nao_recebida",
        "decida_pelo_formulario",
        "informado_mudou",
        "sistema_se_contradisse",
        "numero_novo_nao_cabe",
    }
    assert novos <= motivos  # a busca no codigo acha os motivos novos
    falta = {m for m in motivos if m not in acoes.RECUSAS and m != "ja_saiu"}
    assert not falta
    for motivo in novos:
        status, frase = acoes.recusa_legivel(DecisaoRecusada(motivo, "mensagem sem acento"))
        assert status == 409
        assert frase == acoes.RECUSAS[motivo][1]
        assert "mensagem sem acento" not in frase


# --- Cartao "Selo do sistema" da ficha ----------------------------------------------------------------------------


def test_cartao_do_selo_de_cada_nivel_pelo_retrato() -> None:
    um = acoes.selo_da_ficha(retrato(1))
    assert um == acoes.SeloDaFicha(
        1, "Selo do sistema: nível 1 (conectado). Este sistema não informa o que faz depois de receber a ação."
    )
    assert acoes.selo_da_ficha(retrato(1, motivo="fase_desligada")).texto == um.texto
    dois = acoes.selo_da_ficha(retrato(2))
    assert dois.nivel == 2
    assert dois.texto == (
        "Selo do sistema: nível 2 (confere). Testado pela equipe do MCP-CLARO em 10/11/2026, no ambiente de teste do"
        " sistema; vale até 08/02/2027. Se a resposta do sistema se perder, o MCP-CLARO pergunta a ele o que houve,"
        " pelo número que manda junto. O selo diz como o sistema respondeu no teste; não garante o que ele fará"
        " depois."
    )
    tres = acoes.selo_da_ficha(retrato(3, modo="ferramentas_de_teste"))
    assert tres.texto.startswith("Selo do sistema: nível 3 (presta contas). Testado pela equipe do MCP-CLARO em")
    assert "pelas ferramentas de teste que o sistema declarou; vale até 08/02/2027." in tres.texto
    assert "O sistema informa o que fez depois de receber a ação" in tres.texto
    assert tres.texto.endswith(acoes.NAO_GARANTE)
    for t in (um.texto, dois.texto, tres.texto):
        confere_texto(t)


@pytest.mark.parametrize(
    "ruim",
    [
        None,
        {"nivel": 2},
        {**retrato(2), "nivel": "2"},
        {**retrato(2), "motivo": "reprovado"},
        {**retrato(2), "testado_em": "ontem"},
        {**retrato(2), "a_mais": 1},
    ],
)
def test_retrato_fora_da_forma_nao_promete_nada(ruim: Any) -> None:
    assert acoes.selo_da_ficha(ruim) == acoes.SeloDaFicha(None, acoes.SELO_NAO_GUARDADO)


def test_nota_quando_o_selo_de_agora_e_outro() -> None:
    dois = acoes.selo_da_ficha(retrato(2))
    assert acoes.selo_mudou(dois, 2) == ""
    assert acoes.selo_mudou(dois, None) == ""
    assert acoes.selo_mudou(acoes.SeloDaFicha(None, acoes.SELO_NAO_GUARDADO), 1) == ""
    caiu = acoes.selo_mudou(dois, 1)
    assert caiu.startswith("O selo deste sistema mudou depois que a ficha foi montada: agora é nível 1.")
    assert "a decisão fica com você" in caiu
    assert acoes.selo_mudou(acoes.selo_da_ficha(retrato(1)), 3) == (
        "O selo deste sistema mudou depois que a ficha foi montada: agora é nível 3."
    )


# --- Ficha do pedido igual ----------------------------------------------------------------------------------------


def escada(**mudar: Any) -> EscadaDaOriginal:
    base: dict[str, Any] = {
        "enviado_em": AGORA - timedelta(hours=9),
        "estado_final_do_numero": None,
        "ultima_consulta": "desconhecido",
        "tentativas": MAXIMO,
        "ultimo_desfecho": "respondeu",
    }
    base.update(mudar)
    return EscadaDaOriginal(**base)


def igual(**mudar: Any) -> Operacao:
    base: dict[str, Any] = {"repete_protocolo": "CLR-2026-00000007", "original": escada(), "lido_em": AGORA}
    base.update(mudar)
    return operacao(**base)


def test_ficha_do_pedido_igual_diz_o_que_repete_e_com_que_numero() -> None:
    assert acoes.pedido_igual(operacao(), MAXIMO, pendente=True) is None
    # Sem resposta final e a escada ainda andando: o mesmo numero, quando pode.
    mesmo = acoes.pedido_igual(igual(original=escada(tentativas=2)), MAXIMO, pendente=True)
    assert mesmo is not None
    assert mesmo.textos == (
        "Este pedido repete o protocolo CLR-2026-00000007, que ficou sem confirmação.",
        acoes.MESMO_NUMERO_PREVISTO,
    )
    assert not mesmo.oferece_numero_novo
    # O sistema queimou o numero (A3.3): pedido novo para o sistema, e o aviso de que ele pode estar errado.
    queimado = acoes.pedido_igual(igual(original=escada(estado_final_do_numero="nao_recebida")), MAXIMO, pendente=True)
    assert queimado is not None
    assert queimado.textos[1] == (
        "O sistema informou sobre o protocolo CLR-2026-00000007: o sistema não recebeu o pedido e não vai aceitá-lo"
        " depois. Por isso este pedido vai com número novo: para o sistema, é um pedido novo."
    )
    assert (
        queimado.textos[2]
        == "O sistema disse que o primeiro não chegou; se ele estiver errado, este pedido faz de novo."
    )
    assert not queimado.oferece_numero_novo
    recusada = acoes.pedido_igual(igual(original=escada(estado_final_do_numero="recusada")), MAXIMO, pendente=True)
    assert recusada is not None
    assert recusada.textos[2] == "Se o sistema estiver errado, este pedido faz de novo."
    feito = acoes.pedido_igual(igual(original=escada(estado_final_do_numero="concluida")), MAXIMO, pendente=True)
    assert feito is not None
    assert "o pedido foi feito. Este pedido vai com o mesmo número do primeiro" in feito.textos[1]


def test_ficha_do_pedido_igual_com_desconhecido_no_fim_oferece_o_numero_novo() -> None:
    op = igual()
    assert op.original_ficou_em_desconhecido(MAXIMO)
    m = acoes.pedido_igual(op, MAXIMO, pendente=True)
    assert m is not None
    assert m.oferece_numero_novo
    assert "O sistema não sabe se o primeiro foi feito; com número novo, ele pode fazer duas vezes." in m.textos
    assert acoes.NAO_SABE_SEM_CAIXA in m.textos
    # So a ficha que espera oferece a caixa; o aviso continua.
    decidida = acoes.pedido_igual(op, MAXIMO, pendente=False)
    assert decidida is not None
    assert not decidida.oferece_numero_novo
    assert acoes.NAO_SABE.format(quem="O sistema") in decidida.textos
    # A escada ainda nao acabou: nada de numero novo.
    andando = acoes.pedido_igual(igual(original=escada(tentativas=3)), MAXIMO, pendente=True)
    assert andando is not None
    assert not andando.oferece_numero_novo
    # Escolhido: a ficha diz que foi a escolha da pessoa.
    escolhido = acoes.pedido_igual(igual(ficha_numero_novo=True), MAXIMO, pendente=False)
    assert escolhido is not None
    assert escolhido.textos[1] == acoes.ESCOLHEU_NUMERO_NOVO


@pytest.mark.parametrize(
    ("final", "depois"),
    [
        ("recusada", "Se o servidor de terceiros estiver errado, este pedido faz de novo."),
        ("parada", "Se o servidor de terceiros estiver errado, este pedido faz de novo."),
        (
            "nao_recebida",
            "O servidor de terceiros disse que o primeiro não chegou; se ele estiver errado, este pedido faz de novo.",
        ),
        ("concluida", None),
    ],
)
def test_ficha_do_pedido_igual_de_um_terceiro_diz_o_servidor_de_terceiros(final: str, depois: str | None) -> None:
    """Critica M9: o que um terceiro informa aparece dito pelo servidor de terceiros, como nas outras telas."""
    op = igual(sistema_origem="terceiro", original=escada(estado_final_do_numero=final))
    m = acoes.pedido_igual(op, MAXIMO, pendente=True)
    assert m is not None
    assert m.textos[1].startswith(
        f"O servidor de terceiros informou sobre o protocolo CLR-2026-00000007: {paginas.estado_legivel(final)}."
    )
    if depois is not None:
        assert m.textos[2] == depois
    assert not [t for t in m.textos if re.search(r"(^|Se )[Oo] sistema (informou|disse|estiver|não sabe)", t)]
    for t in m.textos:
        confere_texto(t)
    # O proprio continua "o sistema".
    proprio = acoes.pedido_igual(igual(original=escada(estado_final_do_numero=final)), MAXIMO, pendente=True)
    assert proprio is not None
    assert proprio.textos[1].startswith("O sistema informou sobre o protocolo CLR-2026-00000007:")


def test_ficha_do_pedido_igual_de_um_terceiro_que_nao_sabe() -> None:
    m = acoes.pedido_igual(igual(sistema_origem="terceiro"), MAXIMO, pendente=True)
    assert m is not None
    assert m.oferece_numero_novo
    assert (
        "O servidor de terceiros não sabe se o primeiro foi feito; com número novo, ele pode fazer duas vezes."
        in m.textos
    )
    assert acoes.NAO_SABE.format(quem="O sistema") not in m.textos


def test_acao_diz_o_numero_que_levou() -> None:
    assert acoes.numero_do_envio(operacao(estado=Estado.ENVIADA)) == ()
    mesmo = operacao(estado=Estado.ENVIADA, envio_nivel=2, repete_protocolo="CLR-1", mesmo_numero_de="CLR-1")
    assert acoes.numero_do_envio(mesmo) == (
        "Este pedido saiu com o mesmo número do protocolo CLR-1. Se o primeiro já tinha chegado, o sistema não faz de"
        " novo.",
    )
    novo = operacao(estado=Estado.NAO_CONFIRMADA, envio_nivel=2, repete_protocolo="CLR-1")
    assert acoes.numero_do_envio(novo) == (acoes.SAIU_COM_NUMERO_NOVO.format(original="CLR-1"),)
    escolhido = replace(novo, ficha_numero_novo=True)
    assert acoes.numero_do_envio(escolhido) == (
        acoes.SAIU_COM_NUMERO_NOVO.format(original="CLR-1"),
        acoes.FOI_A_SUA_ESCOLHA,
    )
    # Saiu sem numero (nivel 1): nada a dizer do numero.
    assert acoes.numero_do_envio(operacao(estado=Estado.NAO_CONFIRMADA, repete_protocolo="CLR-1")) == ()
    antes = operacao(estado=Estado.LIBERADA, repete_protocolo="CLR-1", ficha_numero_novo=True)
    assert acoes.numero_do_envio(antes) == (acoes.ESCOLHEU_NUMERO_NOVO,)


# --- O que o sistema informou e o botao de um toque ---------------------------------------------------------------


def test_nivel_1_e_antes_de_sair_nao_tem_o_bloco() -> None:
    assert acoes.o_que_o_sistema_informou(operacao(estado=Estado.NAO_CONFIRMADA), MAXIMO, AGORA) is None
    assert acoes.o_que_o_sistema_informou(operacao(estado=Estado.LIBERADA, envio_nivel=2), MAXIMO, AGORA) is None
    assert acoes.o_que_o_sistema_informou(operacao(estado=Estado.ENVIADA, envio_nivel=3), MAXIMO, AGORA) is None


def test_perguntando_e_sem_resposta_ainda() -> None:
    b = bloco(nao_confirmada())
    assert b.situacao == "perguntando"
    assert texto(b) == (
        "Estamos confirmando com o sistema. O MCP-CLARO pergunta ao sistema caixa o que houve com este pedido, pelo"
        " número que mandou junto. Nada será reenviado."
    )
    assert not b.botao
    b = bloco(nao_confirmada(informado=informado(None, final=None, desfecho="sem_resposta")))
    assert b.situacao == "sem_resposta_ainda"
    assert "e ainda não teve resposta. Continuamos perguntando." in texto(b)
    for desfecho in ("sem_chave", "sala_fora"):
        b = bloco(nao_confirmada(informado=informado(None, final=None, desfecho=desfecho)))
        assert b.situacao == "nao_perguntou"
        assert "a falha é do MCP-CLARO, não do sistema" in texto(b)


def test_resposta_final_mostra_a_hora_a_marca_e_o_botao() -> None:
    b = bloco(nao_confirmada(informado=informado("concluida")))
    assert b.situacao == "respondeu"
    assert texto(b) == (
        "O sistema caixa informou às 11:55 (horário de Brasília): o pedido foi feito. Isto é o que o sistema diz; o"
        " MCP-CLARO não confere do lado de lá. Se você viu outra coisa no sistema, registre o que viu: vale a sua"
        " decisão."
    )
    assert b.botao
    assert b.estado_visto == "concluida"
    assert b.registra == "Aconteceu no sistema"
    for final in ("recusada", "nao_recebida", "parada"):
        b = bloco(nao_confirmada(informado=informado(final, final=final)))
        assert b.botao
        assert b.estado_visto == final
        assert b.registra == "Não aconteceu no sistema"


@pytest.mark.parametrize("final", ["parcial", "desfeita"])
def test_parcial_e_desfeita_nao_cabem_num_toque(final: str) -> None:
    b = bloco(nao_confirmada(informado=informado(final, final=final)))
    assert b.situacao == "respondeu"
    assert not b.botao
    assert b.estado_visto is None
    assert b.dica == acoes.NAO_CABE_NUM_TOQUE


def test_terceiro_nao_recebida_sem_botao_e_com_a_marca_do_terceiro() -> None:
    op = nao_confirmada(
        sistema_origem="terceiro", informado=informado("nao_recebida", final="nao_recebida", terceiro=True)
    )
    b = bloco(op)
    assert not b.botao
    assert b.dica == acoes.TERCEIRO_CONFIRA
    assert texto(b).startswith("O servidor de terceiros caixa informou às")
    assert "Isto é o que o servidor de terceiros diz" in texto(b)
    # Um terceiro que disse "concluida" ainda tem o toque.
    feito = nao_confirmada(sistema_origem="terceiro", informado=informado("concluida", terceiro=True))
    assert bloco(feito).botao


def test_nao_recebida_sozinha_continua_perguntando_sem_botao() -> None:
    b = bloco(nao_confirmada(informado=informado("nao_recebida", final=None)))
    assert b.situacao == "sem_final"
    assert not b.botao
    assert "o sistema não recebeu o pedido e não vai aceitá-lo depois." in texto(b)
    assert texto(b).endswith(acoes.UMA_SO_NAO_BASTA)


@pytest.mark.parametrize("estado", ["em_andamento", "desconhecido"])
def test_em_aberto_continua_perguntando(estado: str) -> None:
    b = bloco(nao_confirmada(informado=informado(estado, final=None)))
    assert b.situacao == "sem_final"
    assert not b.botao
    assert texto(b).endswith(acoes.CONTINUAMOS)


def test_resposta_ao_aviso_e_o_que_o_sistema_disse_nunca_final() -> None:
    b = bloco(nao_confirmada(informado=informado("parada", final=None, origem="aviso", ultimo="respondeu")))
    assert b.situacao == "sem_final"
    assert not b.botao
    assert "o pedido foi parado antes de terminar, pelo bloqueio." in texto(b)
    assert acoes.DO_AVISO in texto(b)
    # Final de antes e o aviso depois: o final, sem a hora dele, e a ultima resposta com a hora do aviso.
    op = nao_confirmada(informado=informado("em_andamento", final="concluida", origem="aviso", ultimo="respondeu"))
    b = bloco(op)
    assert texto(b).startswith("O sistema caixa informou: o pedido foi feito. A última resposta dele, às 11:55")
    assert acoes.DO_AVISO in texto(b)
    assert b.botao
    assert b.estado_visto == "concluida"


def test_escada_acabada_troca_continuamos_por_a_decisao_e_sua() -> None:
    # Pelo tempo (escada_vencida pelo now() do banco), mesmo sem nenhuma resposta.
    vencida = nao_confirmada(envio_gravado_em=AGORA - timedelta(days=contrato.REAPROVEITAR_NUMERO_DIAS))
    b = bloco(vencida)
    assert b.situacao == "acabou"
    assert texto(b) == acoes.NAO_CONFIRMOU
    b = bloco(replace_op(vencida, informado=informado("desconhecido", final=None)))
    assert b.situacao == "sem_final"
    assert texto(b).endswith(acoes.NAO_CONFIRMOU)
    assert acoes.CONTINUAMOS not in texto(b)
    # Pelas tentativas, sem nenhuma resposta: o sistema nao respondeu.
    b = bloco(nao_confirmada(informado=informado(None, final=None, desfecho="sem_resposta", tentativas=MAXIMO)))
    assert b.situacao == "nao_respondeu"
    assert texto(b) == "O sistema caixa não respondeu à consulta. Decida você, como num sistema de nível 1."
    # Sem o maximo (as listas), as tentativas nao acabam a escada.
    b = bloco(nao_confirmada(informado=informado(None, final=None, desfecho="sem_resposta", tentativas=MAXIMO)), None)
    assert b.situacao == "sem_resposta_ainda"
    # sistema_fora na ultima consulta: parou, e a decisao e da pessoa.
    b = bloco(nao_confirmada(informado=informado(None, final=None, desfecho="sistema_fora")))
    assert b.situacao == "sistema_fora"
    assert "não está ativo agora" in texto(b)
    assert texto(b).endswith(acoes.NAO_CONFIRMOU)


def test_outro_protocolo_contradicao_e_decisao_sem_botao() -> None:
    b = bloco(nao_confirmada(substituto="CLR-2026-00000099", informado=informado("concluida")))
    assert b.situacao == "outro_protocolo"
    assert not b.botao
    assert texto(b) == (
        "O resultado está no protocolo CLR-2026-00000099, que saiu depois com o mesmo número. O que o sistema"
        " informar aparece nele."
    )
    b = bloco(nao_confirmada(informado=informado("concluida", contradisse=True)))
    assert not b.botao
    assert acoes.CONTRADISSE in texto(b)
    assert acoes.VALE_A_SUA not in texto(b)
    # Depois da decisao (que e uma so), nada de convite a registrar de novo.
    b = bloco(nao_confirmada(decisao_pessoa="nao_sei", informado=informado("concluida")))
    assert not b.botao
    assert acoes.VALE_A_SUA not in texto(b)


CONVITES = re.compile(r"decida|registre|a decisão é sua", re.IGNORECASE)
"""As frases que convidam a decidir ou a registrar: depois da decisao (que e uma so), nenhuma."""


@pytest.mark.parametrize(
    "op",
    [
        nao_confirmada(informado=informado("concluida", contradisse=True)),
        nao_confirmada(informado=informado(None, final=None, desfecho="sem_resposta", tentativas=MAXIMO)),
        nao_confirmada(envio_gravado_em=AGORA - timedelta(days=contrato.REAPROVEITAR_NUMERO_DIAS)),
        nao_confirmada(
            envio_gravado_em=AGORA - timedelta(days=contrato.REAPROVEITAR_NUMERO_DIAS),
            informado=informado("desconhecido", final=None),
        ),
        nao_confirmada(informado=informado(None, final=None, desfecho="sistema_fora")),
        nao_confirmada(informado=informado("parcial", final="parcial")),
        nao_confirmada(
            sistema_origem="terceiro", informado=informado("nao_recebida", final="nao_recebida", terceiro=True)
        ),
    ],
    ids=["contradisse", "nao_respondeu", "acabou", "sem_final_acabou", "sistema_fora", "parcial", "terceiro"],
)
def test_depois_da_decisao_nada_convida_a_decidir_de_novo(op: Operacao) -> None:
    antes = bloco(op)
    assert CONVITES.search(texto(antes) + antes.dica)  # antes da decisao, o convite esta la
    for decisao in ("aconteceu", "nao_aconteceu", "nao_sei"):
        depois = bloco(replace(op, decisao_pessoa=decisao))
        assert not depois.botao
        assert not depois.dica
        assert not CONVITES.search(texto(depois)), texto(depois)
        assert acoes.VALE_A_DECISAO in texto(depois)
        confere_texto(texto(depois))
    # Na pagina: o bloco de depois nao convida, e o formulario ja nao existe.
    html = _acao(replace(op, decisao_pessoa="aconteceu"))
    informou = html.split('id="o-que-o-sistema-informou"', 1)[1].split("</div>", 1)[0]
    assert not CONVITES.search(_texto_visivel(informou))
    assert 'name="decisao"' not in html


def test_contradicao_depois_da_decisao_diz_so_que_nao_combinam() -> None:
    op = nao_confirmada(informado=informado("concluida", contradisse=True), decisao_pessoa="aconteceu")
    assert texto(bloco(op)).endswith(
        "O sistema deu respostas que não combinam sobre este pedido. Vale a decisão que você registrou."
    )
    assert acoes.CONTRADISSE not in texto(bloco(op))


@pytest.mark.parametrize("desfecho", contrato.DESFECHOS)
@pytest.mark.parametrize("origem", contrato.ORIGENS)
def test_todo_desfecho_e_origem_tem_texto(desfecho: str, origem: str) -> None:
    estado = "desconhecido" if desfecho == "respondeu" else None
    for maximo in (MAXIMO, None):
        b = bloco(nao_confirmada(informado=informado(estado, final=None, desfecho=desfecho, origem=origem)), maximo)
        assert b.frases
        confere_texto(texto(b))
        assert "{" not in texto(b)


def test_nome_do_sistema_vai_escapado_e_marcado() -> None:
    op = nao_confirmada(sistema_nome="<b>caixa</b>\u202e", informado=informado("concluida"))
    frase = str(bloco(op).frases[0])
    assert "<bdi>&lt;b&gt;caixa&lt;/b&gt;[U+202E]</bdi>" in frase
    assert "\u202e" not in frase
    assert "<b>" not in frase


def test_acao_enviada_mostra_o_recibo_sem_botao() -> None:
    op = operacao(
        estado=Estado.ENVIADA, envio_nivel=3, informado=informado("em_andamento", final=None, origem="recibo")
    )
    b = bloco(op)
    assert b.situacao == "enviada"
    assert not b.botao
    assert "o pedido ainda está em andamento." in texto(b)
    # Na pagina da acao enviada: o bloco aparece, marcado como dito pelo sistema, sem botao nem formulario.
    html = _acao(op)
    informou = html.split('id="o-que-o-sistema-informou"', 1)[1].split("</div>", 1)[0]
    assert 'data-situacao="enviada"' in html
    assert "<h3>O que o sistema informou</h3>" in informou
    assert "O sistema <bdi>caixa</bdi> informou às 11:55 (horário de Brasília): o pedido ainda está em andamento." in (
        informou
    )
    assert acoes.NAO_CONFERE in informou
    assert "registrar-o-que-o-sistema-informou" not in html
    assert 'name="decisao"' not in html
    # Sem resposta do sistema, nada do bloco.
    assert "o-que-o-sistema-informou" not in _acao(operacao(estado=Estado.ENVIADA, envio_nivel=3))


def test_etiqueta_das_listas() -> None:
    assert acoes.etiqueta_informado(nao_confirmada()) == ""
    assert acoes.etiqueta_informado(operacao(estado=Estado.NAO_CONFIRMADA)) == ""
    assert acoes.etiqueta_informado(nao_confirmada(informado=informado("concluida"))) == (
        "O sistema informou: o pedido foi feito"
    )
    sozinha = nao_confirmada(informado=informado("nao_recebida", final=None))
    assert acoes.etiqueta_informado(sozinha) == (
        "O sistema informou: o sistema não recebeu o pedido e não vai aceitá-lo depois"
    )
    terceiro = nao_confirmada(sistema_origem="terceiro", informado=informado("concluida", terceiro=True))
    assert acoes.etiqueta_informado(terceiro).startswith("O servidor de terceiros informou:")
    assert acoes.etiqueta_informado(nao_confirmada(substituto="CLR-9")) == "O resultado está no protocolo CLR-9"
    sem_resposta = nao_confirmada(informado=informado(None, final=None, desfecho="sem_resposta"))
    assert acoes.etiqueta_informado(sem_resposta) == ""


# --- Modelos ------------------------------------------------------------------------------------------------------


def _contexto(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sessao": None,
        "csrf": "token-csrf",
        "aviso": None,
        "caminho": "/telas/x",
        "fase1_ligada": True,
        "erros": [],
        "valores": {},
    }
    base.update(extra)
    return base


def _ficha(op: Operacao, **extra: Any) -> str:
    contexto = _contexto(
        a=extra.pop("a", None) or acoes.montar(op, AGORA, maximo_consultas=MAXIMO),
        d={"aplicativo": "App", "descricao": "Manda lembrete.", "classe": "muda", "risco": "medio"},
        risco="Risco médio",
        passkey_ligada=False,
        tem_passkey=False,
        pede_login=False,
        refazer=LoginRefeito(recente=True, url="/telas/login/refazer", falhou=False, minutos=5),
        pede="",
    )
    contexto.update(extra)
    return criar_ambiente("America/Sao_Paulo").get_template("acoes/ficha.html").render(contexto)


def _acao(op: Operacao) -> str:
    contexto = _contexto(a=acoes.montar(op, AGORA, maximo_consultas=MAXIMO), intervalo=2, saida_aberta=True)
    return criar_ambiente("America/Sao_Paulo").get_template("acoes/acao.html").render(contexto)


def test_ficha_com_o_cartao_do_selo_e_o_aviso_de_terceiro_junto() -> None:
    html = _ficha(operacao(selo=retrato(2)))
    cartao = html.split('id="selo-do-sistema"', 1)[1].split("</section>", 1)[0]
    assert acoes.selo_da_ficha(retrato(2)).texto in cartao
    assert 'id="aviso-terceiro"' not in html
    terceiro = _ficha(operacao(selo=retrato(3), sistema_origem="terceiro"))
    cartao = terceiro.split('id="selo-do-sistema"', 1)[1].split("</section>", 1)[0]
    assert "Selo do sistema: nível 3 (presta contas)." in cartao
    assert "Este servidor é de terceiros. O MCP-CLARO não controla o que ele faz do lado de lá." in cartao
    sem = _ficha(operacao())
    assert acoes.SELO_NAO_GUARDADO in sem
    # A nota do selo de agora (critica M3) vem do contexto montado pela tela.
    a = acoes.montar(operacao(selo=retrato(2)), AGORA)
    mudou = _ficha(operacao(), a=replace(a, selo_agora=acoes.selo_mudou(a.selo, 1)))
    assert 'id="selo-mudou"' in mudou
    assert "agora é nível 1." in mudou


def test_ficha_do_pedido_igual_com_a_caixa_em_todo_formulario_que_confirma() -> None:
    op = igual()
    html = _ficha(op)
    assert 'id="pedido-igual"' in html
    assert acoes.NAO_SABE.format(quem="O sistema") in html
    assert html.count('name="numero_novo" value="sim"') == 1  # o formulario que confirma, sem passkey
    assert acoes.CAIXA_NUMERO_NOVO in html
    # Com passkey ligada: a caixa vai no formulario da passkey.
    passkey = _ficha(replace_op(op, ficha_exige_passkey=True, ficha_risco="alto"), passkey_ligada=True)
    assert passkey.count('name="numero_novo" value="sim"') == 1
    formulario = passkey.split('data-passkey="entrar"', 1)[1].split("</form>", 1)[0]
    assert 'name="numero_novo"' in formulario
    # Login refeito: tambem.
    refeito = _ficha(replace_op(op, ficha_risco="alto"), pede_login=True)
    assert refeito.count('name="numero_novo" value="sim"') == 1
    # Sem desconhecido no fim, nenhuma caixa.
    assert 'name="numero_novo"' not in _ficha(igual(original=escada(tentativas=1)))
    assert 'name="numero_novo"' not in _ficha(operacao())
    # A ficha vencida nao oferece nada.
    assert 'name="numero_novo"' not in _ficha(replace_op(op, ficha_expira_em=AGORA - timedelta(seconds=1)))


def test_acao_nao_confirmada_com_o_botao_csrf_e_o_estado_visto() -> None:
    html = _acao(nao_confirmada(informado=informado("recusada", final="recusada")))
    assert 'id="o-que-o-sistema-informou"' in html
    formulario = html.split('id="registrar-o-que-o-sistema-informou"', 1)[1].split("</form>", 1)[0]
    assert 'name="csrf" value="token-csrf"' in formulario
    assert 'name="estado_visto" value="recusada"' in formulario
    assert "Registrar o que o sistema informou</button>" in formulario
    assert "Fica registrado como a sua decisão: <strong>Não aconteceu no sistema</strong>." in html
    # O formulario de sempre continua.
    assert 'name="decisao" value="aconteceu"' in html
    sem_botao = _acao(nao_confirmada(informado=informado("parcial", final="parcial")))
    assert "registrar-o-que-o-sistema-informou" not in sem_botao
    assert acoes.NAO_CABE_NUM_TOQUE in sem_botao
    # Nivel 1: nada do bloco.
    assert "o-que-o-sistema-informou" not in _acao(operacao(estado=Estado.NAO_CONFIRMADA))


def test_acao_diz_o_mesmo_numero() -> None:
    op = operacao(estado=Estado.ENVIADA, envio_nivel=2, repete_protocolo="CLR-1", mesmo_numero_de="CLR-1")
    assert "Este pedido saiu com o mesmo número do protocolo CLR-1." in _acao(op)


def test_numero_do_envio_com_duas_frases_tem_um_id_so() -> None:
    op = nao_confirmada(repete_protocolo="CLR-1", ficha_numero_novo=True)
    assert len(acoes.numero_do_envio(op)) == 2
    html = _acao(op)
    assert html.count('id="numero-do-envio"') == 1
    numero = html.split('id="numero-do-envio"', 1)[1].split("</div>", 1)[0]
    assert acoes.SAIU_COM_NUMERO_NOVO.format(original="CLR-1") in numero
    assert acoes.FOI_A_SUA_ESCOLHA in numero
    assert 'id="numero-do-envio"' not in _acao(operacao(estado=Estado.ENVIADA))


def _lista(ops: list[Operacao]) -> str:
    ambiente = criar_ambiente("America/Sao_Paulo")
    return ambiente.get_template("acoes/lista.html").render(
        _contexto(acoes=[acoes.montar(op, AGORA) for op in ops], busca="")
    )


def _pendencias(ops: list[Operacao]) -> str:
    ambiente = criar_ambiente("America/Sao_Paulo")
    return ambiente.get_template("acoes/pendencias.html").render(
        _contexto(caixa=acoes.separar(ops, AGORA), passkey_ligada=False)
    )


def test_listas_com_a_etiqueta_do_que_o_sistema_informou() -> None:
    op = nao_confirmada(informado=informado("concluida"))
    for html in (_lista([op]), _pendencias([op])):
        assert "Falta a sua decisão" in html
        assert '<span class="etiqueta">O sistema informou: o pedido foi feito</span>' in html
    assert "O sistema informou" not in _lista([nao_confirmada()])


# --- Comprovante ----------------------------------------------------------------------------------------------


def _comprovante(**mudar: Any) -> fila.Comprovante:
    base: dict[str, Any] = {
        "bloqueio_id": uuid.uuid4(),
        "paradas": [],
        "ja_saidas": [
            {"protocolo": "CLR-1", "estado": "enviada", "enviada_em": AGORA.isoformat()},
            {"protocolo": "CLR-2", "estado": "enviada", "enviada_em": AGORA.isoformat()},
            {"protocolo": "CLR-3", "estado": "enviada", "enviada_em": AGORA.isoformat()},
        ],
        "informado": [],
        "em_conferencia": [],
        "avisos": [],
    }
    base.update(mudar)
    return fila.Comprovante(**base)


def _html_comprovante(partes: bloqueio.QuatroPartes | None) -> str:
    p = SimpleNamespace(
        tipo="bloqueio", o_que="Todos", desde=AGORA, quem="Você", id=uuid.uuid4(), encerrado_em=None, encerrado_por=None
    )
    ambiente = criar_ambiente("America/Sao_Paulo")
    return ambiente.get_template("comprovante.html").render(_contexto(p=p, partes=partes))


def test_comprovante_com_a_dica_por_linha_pelo_nivel_do_envio() -> None:
    partes = bloqueio.quatro_partes(_comprovante(), {"CLR-2": 2, "CLR-3": 3})
    assert partes is not None
    assert [partes.nivel(p) for p, _, _ in partes.ja_saidas] == [1, 2, 3]
    html = _html_comprovante(partes)
    linhas = re.findall(r"<li>.*?</li>", html.split('id="ja-seguiu"', 1)[1].split("</section>", 1)[0])
    assert "Neste sistema (nível 1), “seguiu” quer dizer entregue ao sistema, não concluído por ele." in linhas[0]
    assert "Neste sistema (nível 2), “seguiu” quer dizer entregue ao sistema, não concluído por ele." in linhas[1]
    assert "Neste sistema (nível 3), o que ele informar sobre esta ação aparece na parte 4." in linhas[2]
    assert "Nos sistemas de nível 1 e 2" not in html
    assert "4. O que o sistema informou" not in html  # nada informado nem avisado


def test_comprovante_com_a_quarta_parte_marcada_e_o_andamento_dos_avisos() -> None:
    informou = [
        {
            "protocolo": "CLR-3",
            "sistema": "lembretes <i>",
            "estado": "parada",
            "origem": "aviso",
            "gravado_em": AGORA.isoformat(),
            "de_terceiro": False,
        },
        {
            "protocolo": "CLR-2",
            "sistema": "fornecedor",
            "estado": "concluida",
            "origem": "consulta",
            "gravado_em": AGORA.isoformat(),
            "de_terceiro": True,
        },
    ]
    avisos = [{"sistema": s, "estado": e, "de_terceiro": False} for s, e in ESTADOS_DOS_AVISOS]
    avisos.append({"sistema": "pendente", "estado": "pendente", "de_terceiro": False})  # outra parte: uma linha so
    partes = bloqueio.quatro_partes(_comprovante(informado=informou, avisos=avisos), {"CLR-3": 3, "CLR-2": 3})
    assert partes is not None
    assert partes.tem_quarta_parte
    assert partes.so_nivel_3
    assert len(partes.avisos) == len(ESTADOS_DOS_AVISOS)
    html = _html_comprovante(partes)
    quarta = html.split('id="o-que-o-sistema-informou"', 1)[1].split("</section>", 1)[0]
    assert "4. O que o sistema informou (nível 3)" in quarta
    assert "(nível 3):" not in quarta  # so nivel 3: o nivel fica no titulo, nao em cada linha
    assert (
        "Esta parte é o que cada sistema de nível 3 diz sobre o que já tinha recebido. O MCP-CLARO não confere do"
        " lado de lá." in quarta
    )
    assert re.search(
        r"Protocolo <a [^>]+>CLR-3</a>, sistema <bdi>lembretes &lt;i&gt;</bdi>: o pedido foi parado antes de"
        r" terminar, pelo bloqueio, às 12:00 de 19/11/2026 \(em resposta ao aviso do bloqueio\)\."
        r" <span class=\"etiqueta\">Informado pelo sistema</span>",
        quarta,
    )
    assert "servidor de terceiros <bdi>fornecedor</bdi>: o pedido foi feito" in quarta
    assert "Informado pelo servidor de terceiros" in quarta
    assert "Sistema <bdi>pendente</bdi>: o aviso ainda não saiu." in quarta
    assert "O sistema <bdi>sem_resposta</bdi> não respondeu ao aviso do bloqueio." in quarta
    assert "O MCP-CLARO não avisou o sistema <bdi>sistema_fora</bdi> do bloqueio." in quarta
    assert "Isto não quer dizer que o sistema falhou." in quarta
    assert "O bloqueio acabou antes de o aviso ao sistema <bdi>bloqueio_encerrado</bdi> sair." in quarta
    assert "e ele respondeu." in quarta
    assert "está em andamento" not in quarta  # todo andamento do contrato tem frase propria
    confere_texto(_texto_visivel(quarta))


ESTADOS_DOS_AVISOS = [(e, e) for e in contrato.ESTADOS_DO_AVISO]


def test_quarta_parte_marca_os_escondidos_do_nome_do_sistema() -> None:
    """O nome do sistema e do cadastro, mas pode trazer caracteres escondidos: na quarta parte e nos avisos, eles
    aparecem marcados (``[U+202E]``), nunca soltos."""
    nome = "caixa\u202eatad"
    informou = [
        {
            "protocolo": "CLR-3",
            "sistema": nome,
            "estado": "concluida",
            "origem": "recibo",
            "gravado_em": AGORA.isoformat(),
            "de_terceiro": False,
        }
    ]
    avisos = [{"sistema": nome, "estado": e, "de_terceiro": False} for e in contrato.ESTADOS_DO_AVISO]
    partes = bloqueio.quatro_partes(_comprovante(informado=informou, avisos=avisos), {"CLR-3": 3})
    quarta = _html_comprovante(partes).split('id="o-que-o-sistema-informou"', 1)[1].split("</section>", 1)[0]
    assert "\u202e" not in quarta
    linha = next(li for li in re.findall(r"<li>.*?</li>", quarta) if "CLR-3" in li)
    assert "sistema <bdi>caixa[U+202E]atad</bdi>: o pedido foi feito" in linha
    assert quarta.count("<bdi>caixa[U+202E]atad</bdi>") == 1 + len(contrato.ESTADOS_DO_AVISO)


def test_quarta_parte_com_um_sistema_de_nivel_2_nao_fica_sob_o_nivel_3() -> None:
    """O comprovante traz tambem a ultima resposta a consulta de uma acao de nivel 2 em conferencia: a quarta parte
    nao diz entao que e so de nivel 3, e cada linha diz o nivel com que a acao saiu."""
    informou = [
        {
            "protocolo": "CLR-2",
            "sistema": "conferente",
            "estado": "concluida",
            "origem": "consulta",
            "gravado_em": AGORA.isoformat(),
            "de_terceiro": False,
        },
        {
            "protocolo": "CLR-3",
            "sistema": "lembretes",
            "estado": "parada",
            "origem": "aviso",
            "gravado_em": AGORA.isoformat(),
            "de_terceiro": False,
        },
    ]
    partes = bloqueio.quatro_partes(_comprovante(informado=informou), {"CLR-2": 2, "CLR-3": 3})
    assert partes is not None
    assert not partes.so_nivel_3
    quarta = _html_comprovante(partes).split('id="o-que-o-sistema-informou"', 1)[1].split("</section>", 1)[0]
    assert "(nível 3)</h3>" not in quarta
    assert "4. O que o sistema informou</h3>" in quarta
    assert "nível 3 diz" not in quarta
    assert (
        "Esta parte é o que cada sistema de nível 2 ou 3 diz sobre o que já tinha recebido. O MCP-CLARO não confere"
        " do lado de lá." in quarta
    )
    assert "sistema <bdi>conferente</bdi> (nível 2): o pedido foi feito" in quarta
    assert "sistema <bdi>lembretes</bdi> (nível 3): o pedido foi parado antes de terminar" in quarta
    confere_texto(_texto_visivel(quarta))
    # So de nivel 2: tambem sem o "(nivel 3)".
    so_dois = bloqueio.quatro_partes(_comprovante(informado=informou[:1]), {"CLR-2": 2})
    assert so_dois is not None
    assert not so_dois.so_nivel_3
    assert "(nível 3)" not in _html_comprovante(so_dois)


def test_comprovante_avisado_sem_resposta_ainda() -> None:
    avisos = [{"sistema": "lembretes", "estado": "pendente", "de_terceiro": False}]
    partes = bloqueio.quatro_partes(_comprovante(avisos=avisos))
    html = _html_comprovante(partes)
    assert "O que os sistemas de nível 3 responderem ao aviso do bloqueio aparece aqui." in html
    assert "não respondeu" not in html.split('id="o-que-o-sistema-informou"', 1)[1]


# --- Extrato --------------------------------------------------------------------------------------------------


def _linha(**mudar: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "criado_em": AGORA,
        "tipo": "acao_enviada",
        "frase": "Acao x enviada uma vez ao sistema; protocolo CLR-1.",
        "protocolo": "CLR-1",
        "aplicativo": "App",
        "sistema": "caixa",
        "sistema_origem": "proprio",
        "selo_nivel": None,
        "selo_teste_nivel": None,
        "selo_testado_em": None,
    }
    base.update(mudar)
    return SimpleNamespace(**base)


def _extrato(linhas: list[Any]) -> str:
    ambiente = criar_ambiente("America/Sao_Paulo")
    return ambiente.get_template("extrato.html").render(
        _contexto(alheio=None, linhas=linhas, pagina=1, ha_mais=False, base="/telas/extrato")
    )


def test_coluna_selo_da_propria_linha_so_com_o_teste_aprovado() -> None:
    testado = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)  # 09/11 em Brasilia
    assert paginas.selo_da_linha_do_extrato(None, None, None) == ""
    assert paginas.selo_da_linha_do_extrato(1, None, None) == "nível 1"
    assert paginas.selo_da_linha_do_extrato(2, 2, testado) == "nível 2 (teste de 10/11/2026)"
    # Sem o teste aprovado que o confirme (critica M3), nada acima do nivel 1.
    assert paginas.selo_da_linha_do_extrato(2, None, None) == "nível 1"
    assert paginas.selo_da_linha_do_extrato(3, 2, testado) == "nível 1"
    assert paginas.selo_da_linha_do_extrato(True, 2, testado) == ""
    assert paginas.selo_da_linha_do_extrato("2", 2, testado) == ""
    html = _extrato(
        [
            _linha(selo_nivel=3, selo_teste_nivel=3, selo_testado_em=testado),
            _linha(selo_nivel=1),
            _linha(tipo="ficha_confirmada"),
        ]
    )
    assert "<th>Selo</th>" in html
    celulas = re.findall(r"<td>([^<]*)</td>\s*</tr>", html)
    assert celulas == ["nível 3 (teste de 09/11/2026)", "nível 1", ""]


def test_etiqueta_informado_pelo_sistema_nas_linhas_novas() -> None:
    html = _extrato(
        [
            _linha(tipo="sistema_informou", frase="O sistema caixa informou sobre o protocolo CLR-1: concluida."),
            _linha(tipo="sistema_informou_no_bloqueio", sistema_origem="terceiro"),
            _linha(tipo="sistema_nao_respondeu_consulta"),
        ]
    )
    assert html.count('<span class="etiqueta">Informado pelo sistema</span>') == 1
    assert html.count('<span class="etiqueta">Informado pelo servidor de terceiros</span>') == 1
    assert paginas.etiqueta_da_linha_do_extrato("sistema_nao_respondeu_consulta", "proprio") == ""


@pytest.mark.parametrize(
    "modelo", ["acoes/ficha", "acoes/_estado", "acoes/lista", "acoes/pendencias", "comprovante", "extrato"]
)
def test_modelos_da_pessoa_com_acento_e_sem_promessa(modelo: str) -> None:
    visivel = _texto_visivel((PASTA_MODELOS / f"{modelo}.html").read_text(encoding="utf-8"))
    achados = [m.group(0) for m in SEM_ACENTO.finditer(visivel)]
    assert not achados, (modelo, achados)
    assert not PROMESSA.search(visivel), modelo
    assert not PROIBIDAS.search(visivel), modelo


def test_paginas_montadas_sem_promessa() -> None:
    """As paginas inteiras, montadas com cada nivel e cada situacao: "garante" so na negacao."""
    paginas_montadas = [_ficha(operacao(selo=retrato(n))) for n in (1, 2, 3)] + [
        _acao(nao_confirmada(informado=informado(e, final=e))) for e in contrato.ESTADOS_FINAIS
    ]
    for html in paginas_montadas:
        visivel = re.sub(r"\s+", " ", _texto_visivel(html))
        assert _garante_so_negado(visivel)
        assert not PROIBIDAS.search(visivel)
        assert not PROMESSA.search(visivel.replace("não garante", ""))
