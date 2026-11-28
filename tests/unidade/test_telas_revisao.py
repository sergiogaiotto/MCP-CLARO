"""Correcoes da revisao da Fase 2 nas telas, sem banco: caracteres escondidos, login refeito na hora, limites
"so estes destinatarios" com os outros campos vazios, teto vigente ao ampliar, aviso de encerrar e o
comprovante em tres partes."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp_claro.config.settings import Parametros
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.telas import acoes, bloqueio, limites, nucleo
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.login import ProvedorLogin
from mcp_claro.telas.nucleo import LoginRefeito, Telas, url_refazer, voltar_refeito
from mcp_claro.telas.paginas import criar_ambiente
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import LoginPendente, Sessao
from tests.unidade.test_telas_acoes_paginas import AGORA, _escolha, _ficha, operacao

# --- Caracteres escondidos na ficha (achados 7 e 18) ---


@pytest.mark.parametrize(
    ("bruto", "marcado"),
    [
        ("12345\u202e6789", "12345[U+202E]6789"),
        ("\u2066conta\u2069", "[U+2066]conta[U+2069]"),
        ("equipe​", "equipe[U+200B]"),
        ("a\u200db\u200ec\u200f", "a[U+200D]b[U+200E]c[U+200F]"),
        ("tab\taqui\r", "tab[U+0009]aqui[U+000D]"),
        ("linha outra ", "linha[U+2028]outra[U+2029]"),
        ("hi­fen", "hi[U+00AD]fen"),
        ("vazioㅤ", "vazio[U+3164]"),
        ("duas\nlinhas", "duas\nlinhas"),
        ("Acentuação normal: ç, ã, é", "Acentuação normal: ç, ã, é"),
    ],
)
def test_escondidos_viram_marca_visivel(bruto: str, marcado: str) -> None:
    assert acoes.marcar_escondidos(bruto) == marcado
    assert acoes.tem_escondidos(bruto) is (bruto != marcado)


def test_escondidos_nas_chaves_nos_valores_e_dentro_de_listas() -> None:
    argumentos = {"con\u202eta": "1\u202e2", "lista": ["x\u2067y", {"k​": "v"}], "n": 3}
    assert acoes.argumentos_legiveis(argumentos) == [
        ("con[U+202E]ta", "1[U+202E]2"),
        ("lista", '["x[U+2067]y", {"k[U+200B]": "v"}]'),
        ("n", "3"),
    ]
    assert acoes.tem_escondidos(argumentos)
    assert acoes.tem_escondidos({"a": [{"b": "\u202d"}]})
    assert not acoes.tem_escondidos({"a": "limpo", "b": [1, "dois"], "c": None})
    assert acoes.linhas_do_aplicativo("Pode\u202e ir\nfeito") == ["Pode[U+202E] ir", "feito"]
    assert acoes.linhas_do_aplicativo(None) == []


def test_ficha_mostra_as_marcas_e_avisa_em_destaque() -> None:
    op = operacao(argumentos={"conta": "12345\u202e6789"}, aplicativo_diz="Confira\u2066 a conta")
    a = acoes.montar(op, AGORA)
    assert a.escondidos
    assert a.diz_escondidos
    html = _ficha(op)
    for ch in ("\u202e", "\u2066"):
        assert ch not in html  # nada de escondido chega ao HTML
    assert '<dt><bdi>conta</bdi></dt><dd class="dado"><bdi>12345[U+202E]6789</bdi></dd>' in html
    assert "Atenção: este pedido tem caracteres escondidos." in html
    assert "Se você não esperava por isso, recuse." in html
    assert "Atenção: este texto tinha caracteres escondidos." in html
    assert "<bdi>Confira[U+2066] a conta</bdi>" in html
    limpo = _ficha(operacao())
    assert "caracteres escondidos" not in limpo


def test_resumo_da_acao_tambem_mostra_as_marcas() -> None:
    ambiente = criar_ambiente("America/Sao_Paulo")
    op = operacao(argumentos={"para": "equipe​"}, estado=Estado.CANCELADA)
    html = ambiente.get_template("acoes/acao.html").render(
        sessao=None,
        csrf="t",
        aviso=None,
        caminho="/telas/acoes/x",
        fase1_ligada=True,
        erros=[],
        valores={},
        a=acoes.montar(op, AGORA),
        intervalo=2,
    )
    assert "​" not in html
    assert "equipe[U+200B]" in html
    assert "Atenção: este pedido tem caracteres escondidos." in html


# --- Frase de "nao saiu" (achado 36) ---


def test_frase_de_nao_saiu_diz_que_nao_executou() -> None:
    assert acoes.frase_do_estado(Estado.NAO_SAIU) == (
        "Não saiu: o sistema não executou o pedido (não recebeu ou recusou na porta). Nada aconteceu."
    )


# --- Login refeito na hora (achados 5, 14, 33 e 6) ---


@pytest.mark.parametrize(
    ("voltar", "esperado"),
    [
        ("/telas/fichas/abc", "/telas/fichas/abc?refeito=1"),
        ("/telas/limites/novo?escolha=a:b", "/telas/limites/novo?escolha=a%3Ab&refeito=1"),
        ("/telas/passkeys?refeito=1&aviso=x", "/telas/passkeys?aviso=x&refeito=1"),
        ("https://ataque.exemplo/telas/fichas/abc", "/telas?refeito=1"),
        ("//ataque.exemplo/telas", "/telas?refeito=1"),
        ("/telas/login/volta?code=x", "/telas?refeito=1"),
        ("/mcp", "/telas?refeito=1"),
    ],
)
def test_volta_do_login_refeito_so_para_as_telas(voltar: str, esperado: str) -> None:
    assert voltar_refeito(voltar) == esperado


def test_link_do_login_refeito() -> None:
    assert url_refazer("/telas/fichas/abc") == "/telas/login/refazer?voltar=%2Ftelas%2Ffichas%2Fabc"
    assert url_refazer("https://ataque.exemplo/") == "/telas/login/refazer?voltar=%2Ftelas"


def _sessao(auth_time: datetime | None) -> Sessao:
    return Sessao(b"h", cast(Any, None), "csrf", datetime.now(UTC) + timedelta(hours=1), auth_time)


def _telas(segundos: int = 300) -> Telas:
    nada = cast(Any, None)
    return Telas(nada, ConfigTelas(login_recente_segundos=segundos), nada, nada, nada, nada)


def test_login_recente_pelo_auth_time_do_provedor() -> None:
    agora = datetime.now(UTC)
    telas = _telas()
    assert Telas.login_recente(telas, _sessao(agora - timedelta(seconds=30)))
    assert Telas.login_recente(telas, _sessao(agora - timedelta(seconds=290)))
    assert not Telas.login_recente(telas, _sessao(agora - timedelta(seconds=320)))
    assert not Telas.login_recente(telas, _sessao(None))  # sem auth_time, nunca e recente
    assert not Telas.login_recente(telas, _sessao(agora + timedelta(hours=1)))  # do futuro nao vale
    assert Telas.login_recente(_telas(900), _sessao(agora - timedelta(seconds=600)))


def test_pagina_sabe_quando_o_provedor_nao_refez_o_login() -> None:
    velho = _sessao(datetime.now(UTC) - timedelta(hours=2))
    voltou = cast(Any, SimpleNamespace(query_params={"refeito": "1"}))
    primeira = cast(Any, SimpleNamespace(query_params={}))
    r = Telas.login_refeito(_telas(), voltou, velho, "/telas/fichas/x")
    assert r == LoginRefeito(False, "/telas/login/refazer?voltar=%2Ftelas%2Ffichas%2Fx", True, 5)
    assert not Telas.login_refeito(_telas(), primeira, velho, "/telas/fichas/x").falhou
    assert Telas.login_refeito(_telas(), voltou, _sessao(datetime.now(UTC)), "/telas").recente


def test_config_do_login_recente() -> None:
    teste = Parametros(APP_ENV="teste")
    assert ConfigTelas().login_recente_segundos == 300
    assert any("SCREENS_RECENT_LOGIN_SECONDS" in e for e in ConfigTelas(login_recente_segundos=30).problemas(teste))
    assert any("SCREENS_RECENT_LOGIN_SECONDS" in e for e in ConfigTelas(login_recente_segundos=3600).problemas(teste))
    assert ConfigTelas(login_recente_segundos=120).problemas(teste) == []
    lido = ConfigTelas.de_parametros(cast(Parametros, SimpleNamespace(SCREENS_RECENT_LOGIN_SECONDS=120)))
    assert lido.login_recente_segundos == 120


def test_url_do_login_refeito_pede_a_senha_de_novo() -> None:
    p = Parametros(APP_ENV="teste", IDP_KIND="keycloak", IDP_ISSUER_URL="https://login.exemplo/realms/r")
    provedor = ProvedorLogin(p, ConfigTelas(), cast(Any, None), cast(Any, None))
    login = LoginPendente("estado", "nonce", "v" * 64, "/telas")
    comum = parse_qs(urlsplit(asyncio.run(provedor.url_login(login))).query)
    refeito = parse_qs(urlsplit(asyncio.run(provedor.url_login(login, refazer=True))).query)
    assert "prompt" not in comum
    assert "max_age" not in comum
    assert refeito["prompt"] == ["login"]
    assert refeito["max_age"] == ["0"]
    assert refeito["state"] == comum["state"] == ["estado"]


def test_modulo_nucleo_exporta_o_caminho_do_login_refeito() -> None:
    assert nucleo.CAMINHO_REFAZER == "/telas/login/refazer"


# --- Limite "so estes destinatarios" (achado 0) ---


def _ler(escolha: limites.Escolha, **campos: str) -> tuple[limites.PedidoLimite | None, list[str]]:
    f = Formulario([("escolha", escolha.chave), *campos.items()])
    return limites.ler_pedido(f, {escolha.chave: escolha}, maximo_dia=5, maximo_dias=30)


def test_so_estes_destinatarios_fecha_os_outros_campos() -> None:
    escolha = dataclasses.replace(_escolha(), nome_original="enviar_email", campos=("para", "copia", "oculta"))
    lido, erros = _ler(escolha, destinatarios="estes", campo="para", valores="chefe@org", por_dia="1", dias="1")
    assert erros == []
    assert lido is not None
    assert lido.destinatarios == [
        {"campo": "para", "valores": ["chefe@org"]},
        {"campo": "copia", "valores": []},
        {"campo": "oculta", "valores": []},
    ]
    # O cenario da revisao: copia para fora nao passa sem ficha; vazio, ausente ou lista vazia passa.
    assert not fila.cobre(lido.destinatarios, {"para": "chefe@org", "copia": ["externo@fora.com"]})
    assert not fila.cobre(lido.destinatarios, {"para": "chefe@org", "oculta": "externo@fora.com"})
    assert fila.cobre(lido.destinatarios, {"para": "chefe@org"})
    assert fila.cobre(lido.destinatarios, {"para": "chefe@org", "copia": [], "oculta": ""})
    assert not fila.cobre(lido.destinatarios, {"para": "outro@org"})
    # Escolher outro campo muda quem fica vazio.
    outro, _ = _ler(escolha, destinatarios="estes", campo="copia", valores="a@org", por_dia="1", dias="1")
    assert outro is not None
    assert [i["campo"] for i in outro.destinatarios] == ["copia", "para", "oculta"]
    assert outro.alvo() != lido.alvo()


def test_tela_de_criar_diz_que_os_outros_campos_vem_vazios() -> None:
    ambiente = criar_ambiente("America/Sao_Paulo")
    escolha = dataclasses.replace(_escolha(), campos=("para", "copia"))
    html = ambiente.get_template("acoes/limite_novo.html").render(
        sessao=None,
        csrf="t",
        aviso=None,
        caminho="/telas/limites/novo",
        fase1_ligada=True,
        erros=[],
        valores={},
        e=escolha,
        maximo_dia=5,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )
    assert "<strong>para, copia</strong>, menos o que você escolher acima) precisam vir vazios" in html
    assert "Pedido para qualquer outro destinatário passa pela ficha." not in html


# --- Ampliar com o teto vigente (achado 31) e aviso de encerrar (achado 32) ---


def _linha(por_dia: int, dias_restantes: int = 1) -> Any:
    return SimpleNamespace(
        por_dia=por_dia, situacao="ativo", valido_ate=datetime.now(UTC) + timedelta(days=dias_restantes)
    )


def _ampliar(linha: Any, maximo_dia: int, maximo_dias: int, **campos: str) -> tuple[int, int, list[str]]:
    return limites.TelasDeLimites._ler_ampliacao(
        cast(Any, None), Formulario(list(campos.items())), linha, maximo_dia, maximo_dias
    )


def test_ampliar_respeita_o_teto_de_agora() -> None:
    # O teto baixou de 5 para 2: o limite de 5 nao renova com 5.
    _, _, erros = _ampliar(_linha(5), 2, 30, por_dia="5", dias="30")
    assert erros == ["O teto mudou: agora vai até 2 por dia. Para renovar, este limite passa a 2 por dia."]
    assert _ampliar(_linha(5), 2, 30, por_dia="2", dias="30") == (2, 30, [])
    _, _, erros = _ampliar(_linha(5), 2, 30, por_dia="1", dias="30")
    assert erros  # abaixo do teto, nao: so ate o teto
    # Dentro do teto, segue como antes: so aumenta.
    assert _ampliar(_linha(2), 5, 30, por_dia="3", dias="1") == (3, 1, [])
    _, _, erros = _ampliar(_linha(3), 5, 30, por_dia="2", dias="30")
    assert erros == ["A quantidade por dia vai de 3 a 5. Para diminuir, encerre e crie outro."]
    _, _, erros = _ampliar(_linha(2), 5, 10, por_dia="2", dias="30")
    assert erros == ["O prazo vai de 1 a 10 dias, contados de hoje."]


def test_aviso_de_encerrar_diz_quantas_foram_canceladas() -> None:
    assert limites.aviso_de_encerrado(0).endswith("Nenhuma ação liberada por ele esperava para sair.")
    assert "1 ação liberada por ele ainda esperava para sair e foi cancelada" in limites.aviso_de_encerrado(1)
    assert "3 ações liberadas por ele ainda esperavam para sair e foram canceladas" in limites.aviso_de_encerrado(3)


# --- Comprovante em tres partes (achado 24); na Fase 4, quatro_partes com a dica por linha ---


def _comprovante(**mudar: Any) -> fila.Comprovante:
    base: dict[str, Any] = {
        "bloqueio_id": uuid.uuid4(),
        "paradas": [{"protocolo": "CLR-2026-00000001", "estava": "aguardando_ficha"}],
        "ja_saidas": [{"protocolo": "CLR-2026-00000002", "estado": "enviada", "enviada_em": AGORA.isoformat()}],
        "informado": [],
        "em_conferencia": [{"protocolo": "CLR-2026-00000003", "estado": "nao_confirmada", "desde": AGORA.isoformat()}],
    }
    base.update(mudar)
    return fila.Comprovante(**base)


def test_tres_partes_como_na_proposta() -> None:
    partes = bloqueio.quatro_partes(_comprovante())
    assert partes is not None
    assert partes.paradas == [("CLR-2026-00000001", "esperava a sua ficha")]
    assert partes.ja_saidas == [("CLR-2026-00000002", "entregue ao sistema", "19/11/2026 12:00")]
    assert partes.em_conferencia == [
        ("CLR-2026-00000003", "pode ter saído, mas a resposta do sistema não chegou inteira", "19/11/2026 12:00")
    ]
    ambiente = criar_ambiente("America/Sao_Paulo")
    p = SimpleNamespace(
        tipo="bloqueio",
        o_que="Todos os seus aplicativos",
        desde=AGORA,
        quem="Você",
        id=uuid.uuid4(),
        encerrado_em=None,
        encerrado_por=None,
    )
    html = ambiente.get_template("comprovante.html").render(
        sessao=None,
        csrf="t",
        aviso=None,
        caminho="/telas/bloqueio/x",
        fase1_ligada=True,
        erros=[],
        valores={},
        p=p,
        partes=partes,
    )
    um, dois, tres = (
        html.index("1. O que paramos a tempo"),
        html.index("2. O que já tinha seguido"),
        html.index("3. O que ainda está em conferência"),
    )
    assert um < html.index("CLR-2026-00000001") < dois < html.index("CLR-2026-00000002") < tres
    assert tres < html.index("CLR-2026-00000003")
    # Fase 4: a dica fixa virou uma dica por linha, pelo nivel do selo com que a acao saiu (sem envio, nivel 1).
    assert "Neste sistema (nível 1), “seguiu” quer dizer entregue ao sistema, não concluído por ele." in html
    assert "O que o sistema informou" not in html


def test_comprovante_sem_a_parte_em_conferencia_separa_pelo_estado() -> None:
    antigo = SimpleNamespace(
        paradas=[],
        ja_saidas=[
            {"protocolo": "A", "estado": "enviada", "enviada_em": None},
            {"protocolo": "B", "estado": "nao_confirmada", "enviada_em": None},
            {"protocolo": "C", "estado": "enviando", "enviada_em": None},
        ],
    )
    partes = bloqueio.quatro_partes(cast(fila.Comprovante, antigo))
    assert partes is not None
    assert [p[0] for p in partes.ja_saidas] == ["A"]
    assert [p[0] for p in partes.em_conferencia] == ["B", "C"]


@pytest.mark.parametrize(
    ("ligada", "exige_passkey", "risco", "pede"),
    [
        (False, False, "alto", True),  # passkey desligada: risco alto pede login refeito
        (False, True, "alto", True),  # ficha montada pedindo passkey, que depois foi desligada
        (True, True, "alto", False),  # passkey ligada: vale a passkey
        (True, False, "alto", True),  # ficha montada sem passkey, que depois foi ligada
        (False, False, "medio", False),
        (True, False, "medio", False),
    ],
)
def test_qual_ficha_pede_login_refeito(ligada: bool, exige_passkey: bool, risco: str, pede: bool) -> None:
    telas = cast(Any, SimpleNamespace(t=SimpleNamespace(p=SimpleNamespace(FEATURE_PASSKEY=ligada))))
    op = operacao(ficha_exige_passkey=exige_passkey, ficha_risco=risco)
    assert acoes.TelasDeAcoes._pede_login(telas, op) is pede


def test_ficha_montada_sem_passkey_que_depois_foi_ligada() -> None:
    velho = LoginRefeito(recente=False, url="/telas/login/refazer?voltar=%2Ftelas", falhou=False, minutos=5)
    html = _ficha(operacao(ficha_risco="alto"), passkey_ligada=True, pede_login=True, refazer=velho)
    assert "a ficha foi montada quando a passkey estava desligada" in html
    assert "Confirmar com a passkey" not in html
    assert "Confirmar</button>" not in html
    assert "Entrar de novo agora" in html
