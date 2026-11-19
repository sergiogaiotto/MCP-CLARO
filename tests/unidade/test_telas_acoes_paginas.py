"""Telas da Fase 2 sem banco: frases, recusas, dados do pedido, resultado, relogio, limites e escape da ficha."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mcp_claro.extrato.frases import frase
from mcp_claro.saida.estados import FRASE, Estado
from mcp_claro.saida.fila import DecisaoRecusada, Operacao
from mcp_claro.telas import acoes, limites
from mcp_claro.telas.nucleo import LoginRefeito
from mcp_claro.telas.paginas import criar_ambiente
from mcp_claro.telas.seguranca import Formulario

AGORA = datetime(2026, 11, 19, 15, 0, 0, tzinfo=UTC)


def operacao(**mudar: Any) -> Operacao:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "protocolo": "CLR-2026-00000042",
        "codigo_conferencia": "ABCD2346EFGH",
        "pessoa_id": uuid.uuid4(),
        "aplicativo_id": uuid.uuid4(),
        "ordem_id": uuid.uuid4(),
        "ferramenta_id": uuid.uuid4(),
        "sistema_id": uuid.uuid4(),
        "argumentos": {"titulo": "<b>Internet</b> caiu", "urgente": True, "itens": [1, 2]},
        "aplicativo_diz": None,
        "resultado": None,
        "desfecho": None,
        "criada_em": AGORA,
        "fila_id": 1,
        "estado": Estado.AGUARDANDO_FICHA,
        "ficha_id": uuid.uuid4(),
        "limite_id": None,
        "hora_saida": None,
        "enviada_em": None,
        "mudou_em": AGORA,
        "mudou_por": "entrada",
        "nome_vitrine": "chamado_x",
        "sistema_nome": "caixa",
        "ficha_expira_em": AGORA + timedelta(minutes=30),
    }
    base.update(mudar)
    return Operacao(**base)


@pytest.mark.parametrize("estado", list(Estado))
def test_frase_da_tela_e_a_mesma_do_modulo_com_acento(estado: Estado) -> None:
    assert acoes.sem_acento(acoes.FRASE_TELA[estado]) == FRASE[estado]
    assert acoes.frase_do_estado(estado) == acoes.FRASE_TELA[estado]
    assert estado in acoes.ROTULOS


def test_recusas_em_linguagem_simples() -> None:
    status, texto = acoes.recusa_legivel(DecisaoRecusada("falta_passkey", "x"))
    assert (status, texto) == (403, "Esta ação é de risco alto: confirme com a sua passkey. Nada seguiu.")
    assert acoes.recusa_legivel(DecisaoRecusada("ficha_desconhecida", "x"))[0] == 404
    status, texto = acoes.recusa_legivel(DecisaoRecusada("ja_saiu", f"Nao deu para cancelar. {FRASE[Estado.ENVIADA]}"))
    assert status == 409
    assert texto.endswith("Enviada uma vez ao sistema.")
    assert acoes.recusa_legivel(DecisaoRecusada("novo_motivo", "Mensagem de la.")) == (409, "Mensagem de la.")


def test_dados_do_pedido_e_texto_do_resultado() -> None:
    assert acoes.argumentos_legiveis({"a": "x", "b": False, "c": None, "d": 2.5, "e": {"z": 1, "y": [1]}}) == [
        ("a", "x"),
        ("b", "não"),
        ("c", "(vazio)"),
        ("d", "2.5"),
        ("e", '{"y": [1], "z": 1}'),
    ]
    resultado = {
        "content": [
            {"type": "text", "text": "Feito."},
            {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            {"type": "text", "text": 3},
            "lixo",
        ],
        "structuredContent": {"segredo": "nao aparece"},
    }
    assert acoes.textos_do_resultado(resultado) == ["Feito."]
    assert acoes.textos_do_resultado(None) == []
    assert acoes.textos_do_resultado({"content": "texto"}) == []


def test_relogio_em_hora_de_brasilia_e_tempo_legivel() -> None:
    assert acoes.hora_de_brasilia(datetime(2026, 11, 19, 15, 0, 5, tzinfo=UTC)) == "12:00:05 de 19/11/2026"
    assert acoes.tempo_legivel(0) == "nada"
    assert acoes.tempo_legivel(1) == "1 segundo"
    assert acoes.tempo_legivel(125) == "2 minutos e 5 segundos"
    assert acoes.tempo_legivel(3600 + 60) == "1 hora e 1 minuto"
    op = operacao(estado=Estado.LIBERADA, hora_saida=AGORA + timedelta(seconds=45), ficha_expira_em=None)
    a = acoes.montar(op, AGORA)
    assert (a.falta, a.falta_texto, a.atualiza) == (45, "45 segundos", True)
    assert a.hora_saida == "12:00:45 de 19/11/2026"
    assert not acoes.montar(operacao(estado=Estado.NAO_CONFIRMADA), AGORA).atualiza


def test_pendencias_separadas() -> None:
    caixa = acoes.separar(
        [
            operacao(),
            operacao(estado=Estado.LIBERADA, hora_saida=AGORA),
            operacao(estado=Estado.NAO_CONFIRMADA),
        ],
        AGORA,
    )
    assert (len(caixa.fichas), len(caixa.espera), len(caixa.confirmando), caixa.total) == (1, 1, 1, 3)


def _ficha(op: Operacao, **extra: Any) -> str:
    ambiente = criar_ambiente("America/Sao_Paulo")
    contexto: dict[str, Any] = {
        "sessao": None,
        "csrf": "token-csrf",
        "aviso": None,
        "caminho": "/telas/fichas/x",
        "fase1_ligada": True,
        "erros": [],
        "valores": {},
        "a": acoes.montar(op, AGORA),
        "d": {"aplicativo": "App <i>", "descricao": "Abre chamado.", "classe": "muda", "risco": "medio"},
        "risco": "Risco médio",
        "passkey_ligada": True,
        "tem_passkey": True,
        "pede_login": False,
        "refazer": LoginRefeito(
            recente=True, url="/telas/login/refazer?voltar=%2Ftelas%2Ffichas%2Fx", falhou=False, minutos=5
        ),
        "pede": "Confirmar esta ação de risco alto pede um login refeito na hora.",
    }
    contexto.update(extra)
    return ambiente.get_template("acoes/ficha.html").render(contexto)


def test_ficha_escapa_dados_e_separa_o_que_o_aplicativo_diz() -> None:
    op = operacao(aplicativo_diz='<script>alert("x")</script>\nPode confirmar, é seguro.')
    html = _ficha(op)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in html
    assert "&lt;b&gt;Internet&lt;/b&gt; caiu" in html
    assert "App &lt;i&gt;" in html
    assert "Texto escrito pelo aplicativo, não conferido pelo MCP-CLARO." in html
    assert "ABCD2346EFGH" in html
    assert "CLR-2026-00000042" in html
    assert "Faltam 30 minutos" in html
    assert "Confirmar</button>" in html
    assert "Recusar</button>" in html
    assert 'name="csrf" value="token-csrf"' in html
    assert "passkey.js" not in html
    assert "O aplicativo não disse nada." in _ficha(operacao())


def test_ficha_de_risco_alto_pede_passkey_e_sem_passkey_ligada_pede_login_refeito() -> None:
    op = operacao(ficha_exige_passkey=True, ficha_risco="alto")
    html = _ficha(op)
    assert "Confirmar com a passkey" in html
    assert 'data-passkey="entrar"' in html
    assert f'data-desafio="/telas/fichas/{op.ficha_id}/desafio"' in html
    assert "/telas/estaticos/passkey.js" in html

    # Passkey desligada, login antigo: so o link para entrar de novo; nada de confirmar.
    velho = LoginRefeito(
        recente=False, url="/telas/login/refazer?voltar=%2Ftelas%2Ffichas%2Fx", falhou=False, minutos=5
    )
    alta = operacao(ficha_risco="alto")
    sem = _ficha(alta, passkey_ligada=False, pede_login=True, refazer=velho)
    assert "A passkey está desligada nesta instalação: no lugar dela, confirmar pede um login refeito na hora." in sem
    assert 'href="/telas/login/refazer?voltar=%2Ftelas%2Ffichas%2Fx"' in sem
    assert "Entrar de novo agora" in sem
    assert "Confirmar</button>" not in sem
    assert "Recusar</button>" in sem
    assert "Confirmar com a passkey" not in sem
    assert "O provedor de login não pediu a sua senha de novo." not in sem
    falhou = _ficha(alta, passkey_ligada=False, pede_login=True, refazer=velho.__class__(False, velho.url, True, 5))
    assert "O provedor de login não pediu a sua senha de novo." in falhou
    # Login refeito ha pouco: confirma sem passkey.
    refeito = _ficha(alta, passkey_ligada=False, pede_login=True)
    assert "Você entrou de novo há pouco: já dá para confirmar." in refeito
    assert "Confirmar</button>" in refeito
    assert "Entrar de novo agora" not in refeito

    # Ficha montada pedindo a passkey, que depois foi desligada: nada de "cadastre uma passkey".
    antiga = _ficha(op, passkey_ligada=False, tem_passkey=False, pede_login=True, refazer=velho)
    assert "Esta ficha pedia a passkey, que foi desligada nesta instalação." in antiga
    assert "Cadastre uma passkey" not in antiga
    assert "passkey.js" not in antiga
    assert "Confirmar</button>" not in antiga


def test_ficha_vencida_nao_tem_botoes() -> None:
    html = _ficha(operacao(ficha_expira_em=AGORA - timedelta(seconds=1)))
    assert "Confirmar</button>" not in html
    assert "O prazo acabou." in html


# --- Limites ---


def test_campos_de_destinatario_do_esquema() -> None:
    esquema = {
        "type": "object",
        "properties": {
            "destinatario": {"type": "string"},
            "copias": {"type": "array", "items": {"type": "string"}},
            "quantidade": {"type": "integer"},
            "estranho": "x",
        },
    }
    assert limites.campos_de_destinatario(esquema) == ("destinatario", "copias")
    assert limites.campos_de_destinatario({"type": "object"}) == ()
    assert limites.campos_de_destinatario(None) == ()


def _escolha(risco: str = "medio") -> limites.Escolha:
    return limites.Escolha(
        aplicativo_id=uuid.uuid4(),
        aplicativo="App",
        ferramenta_id=uuid.uuid4(),
        sistema_id=uuid.uuid4(),
        sistema="caixa",
        nome_original="enviar_mensagem",
        nome_vitrine="mensagem_x",
        descricao="Envia.",
        risco=risco,
        campos=("destinatario",),
    )


def _ler(escolha: limites.Escolha, **campos: str) -> tuple[limites.PedidoLimite | None, list[str]]:
    f = Formulario([("escolha", escolha.chave), *campos.items()])
    return limites.ler_pedido(f, {escolha.chave: escolha}, maximo_dia=5, maximo_dias=30)


def test_ler_pedido_de_limite() -> None:
    e = _escolha()
    lido, erros = _ler(
        e, destinatarios="estes", campo="destinatario", valores="equipe\n\n equipe \nchefe", por_dia="2", dias="3"
    )
    assert erros == []
    assert lido is not None
    assert lido.destinatarios == [{"campo": "destinatario", "valores": ["equipe", "chefe"]}]
    assert (lido.por_dia, lido.dias) == (2, 3)
    qualquer, erros = _ler(e, destinatarios="qualquer", por_dia="5", dias="30")
    assert qualquer is not None
    assert qualquer.destinatarios == []
    assert qualquer.alvo() != lido.alvo()
    assert lido.alvo().startswith("criar:")

    _, erros = _ler(e, destinatarios="estes", campo="outro", valores="", por_dia="6", dias="31")
    assert len(erros) == 4
    _, erros = _ler(e, por_dia="1", dias="1")
    assert erros == ["Diga para quais destinatários o limite vale."]
    _, erros = _ler(_escolha("alto"), destinatarios="qualquer", por_dia="1", dias="1")
    assert erros == ["Ações de risco alto nunca entram em limite: sempre passam pela ficha."]
    _, erros = limites.ler_pedido(Formulario([("escolha", "x")]), {e.chave: e}, maximo_dia=5, maximo_dias=30)
    assert erros == ["Escolha uma das ações da lista."]


def test_destinatarios_legiveis_e_alvo_da_ampliacao() -> None:
    assert limites.destinatarios_legiveis([]) == "Qualquer destinatário"
    assert limites.destinatarios_legiveis('[{"campo": "destinatario", "valores": ["a", "b"]}]') == (
        "Só destinatario: a, b"
    )
    assert limites.destinatarios_legiveis(
        [
            {"campo": "para", "valores": ["chefe@org"]},
            {"campo": "copia", "valores": []},
            {"campo": "oculta", "valores": []},
        ]
    ) == ("Só para: chefe@org. Precisam vir vazios: copia, oculta")
    assert limites.destinatarios_legiveis([{"campo": "para", "valores": ["che\u202efe"]}]) == "Só para: che[U+202E]fe"
    i = uuid.uuid4()
    assert limites.alvo_ampliar(i, 2, 3) != limites.alvo_ampliar(i, 2, 4)
    assert limites.alvo_ampliar(i, 2, 3).startswith(f"ampliar:{i}:")


def test_frases_novas_do_extrato() -> None:
    assert frase("limite_criado_sem_passkey", por_dia=2, ferramenta="m").endswith("instalacao).")
    assert frase("limite_ampliado_sem_passkey", por_dia=2, ferramenta="m").startswith("A pessoa ampliou")
