"""Terceira rodada de correcoes da Fase 2 nas telas, sem banco: caracteres escondidos marcados nos nomes de
campo e nos textos que vem do sistema, o que a tela diz da passkey de um limite, a escolha que o ADMIN acabou de
revisar e as frases do extrato que sairam."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlencode

import pytest
from starlette.requests import Request
from starlette.responses import Response

from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import FRASES, FraseDesconhecida, frase
from mcp_claro.regras import politica
from mcp_claro.saida.estados import Estado
from mcp_claro.telas import acoes, consultas, limites, passkeys
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.nucleo import Telas
from mcp_claro.telas.paginas import AVISOS, criar_ambiente
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import Sessao
from tests.unidade.test_telas_acoes_paginas import AGORA, _escolha, operacao

BASE: dict[str, Any] = {
    "sessao": None,
    "csrf": "t",
    "aviso": None,
    "caminho": "/telas/x",
    "fase1_ligada": True,
    "erros": [],
    "valores": {},
}
ZW = "​"  # largura zero: "para" e "para" + ZW parecem iguais na tela
RLO = "‮"  # inverte o que vem depois


def _render(modelo: str, **contexto: Any) -> str:
    return criar_ambiente("America/Sao_Paulo").get_template(modelo).render({**BASE, **contexto})


# --- T2: nome de campo e texto do sistema com a marca dos caracteres escondidos ---


def test_filtro_escondidos_chama_a_marca_da_ficha() -> None:
    filtro = criar_ambiente("UTC").filters["escondidos"]
    assert filtro(f"para{ZW}") == "para[U+200B]"
    assert filtro("Ação ç") == "Ação ç"
    assert filtro(3) == "3"


def _revisao(**mudar: Any) -> str:
    contexto: dict[str, Any] = {
        "sistema": SimpleNamespace(id=uuid.uuid4(), nome="caixa"),
        "nome": f"enviar{RLO}email",
        "definicao": {"description": f"Envia{RLO} o e-mail."},
        "esquema_entrada": '{\n  "para": {},\n  "para' + ZW + '": {}\n}',
        "campos_saida": [f"nome{ZW}"],
        "campos_entrada": ("para", f"para{ZW}", "texto"),
        "outros_campos": "",
        "revisao": None,
        "mudou": False,
        "valores": {
            "nome_vitrine": "e",
            "titulo": "",
            "descricao": "",
            "classe": "muda",
            "risco": "medio",
            "campos": [],
            "destinatario": [],
        },
    }
    contexto.update(mudar)
    return _render("admin/ferramenta.html", **contexto)


def test_nome_de_campo_com_escondido_e_marcado_na_revisao_e_no_limite() -> None:
    """A prova do verificador, ao contrario: 'para' e 'para' + U+200B viram duas caixas diferentes na tela."""
    html = _revisao()
    # As caixas: o texto visivel marca; o value= fica cru, para o formulario gravar o nome de verdade.
    assert 'name="destinatario" value="para" > <code>para</code>' in html
    assert f'name="destinatario" value="para{ZW}" > <code>para[U+200B]</code>' in html
    assert f'name="campos" value="nome{ZW}" > <code>nome[U+200B]</code>' in html
    # O nome e o texto que o sistema declara, e o esquema de entrada.
    assert "<h1>Revisar a ferramenta <code>enviar[U+202E]email</code></h1>" in html
    assert "<title>Revisar enviar[U+202E]email" in html
    assert f'<input type="hidden" name="nome" value="enviar{RLO}email">' in html
    assert '<p class="estranho">Envia[U+202E] o e-mail.</p>' in html
    assert "&#34;para[U+200B]&#34;: {}" in html  # aspas escapadas pelo HTML
    # Cru, so nos value= (o nome da ferramenta, a caixa do campo pessoal e a do destinatario).
    assert html.count(RLO) == 1
    assert html.count(ZW) == 2


def test_limite_novo_marca_os_campos_de_destinatario() -> None:
    escolha = dataclasses.replace(_escolha(), campos=(f"para{ZW}", "copia"))
    html = _render(
        "acoes/limite_novo.html",
        e=escolha,
        maximo_dia=5,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )
    assert f'<option value="para{ZW}" >para[U+200B]</option>' in html
    assert '<option value="copia" >copia</option>' in html
    # A lista dos campos marcados e o aviso dos que precisam vir vazios.
    assert html.count("<strong>para[U+200B], copia</strong>") == 2
    assert html.count(ZW) == 1  # so no value= da opcao


def _linha_limite(ferramenta: str, com_passkey: bool, situacao: str = "ativo") -> Any:
    agora = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        ferramenta=ferramenta,
        sistema="caixa",
        aplicativo="App",
        por_dia=2,
        usados_hoje=0,
        valido_ate=agora + timedelta(days=3),
        situacao=situacao,
        motivo=None,
        criado_em=agora,
        com_passkey=com_passkey,
        destinatarios=[],
    )


def _pagina_limite(linha: Any, ligada: bool = True) -> str:
    return _render(
        "acoes/limite.html",
        l=limites._limite(linha, ligada),
        minimo_dia=2,
        maximo_dia=5,
        acima_do_teto=False,
        maximo_dias=30,
        ligada=ligada,
        tem_passkey=True,
        refazer=None,
        pede="",
    )


def _lista_limites(ativos: list[Any], terminados: list[Any], ligada: bool = True) -> str:
    return _render(
        "acoes/limites.html",
        ativos=[limites._limite(x, ligada) for x in ativos],
        terminados=[limites._limite(x, ligada) for x in terminados],
        escolhas=[],
        de_risco_alto=[],
        maximo_dia=5,
        maximo_dias=30,
        ligada=ligada,
    )


def _render_sistema() -> str:
    vista = SimpleNamespace(
        nome=f"ler{RLO}fatura",
        definicao={"description": f"Mostra{ZW} a fatura."},
        revisada_id=None,
        impressao="a",
        impressao_revisada=None,
        nome_vitrine=None,
        classe=None,
    )
    sistema = SimpleNamespace(
        id=uuid.uuid4(),
        nome="caixa",
        endereco="https://caixa.exemplo/mcp",
        tipo_chave="por_pessoa",
        situacao="ativo",
        versao_mcp="2026-07-28",
        motivo=None,
        sondado_em=None,
    )
    return _render("admin/sistema.html", sistema=sistema, vistas=[vista])


def _render_modelos() -> str:
    f = SimpleNamespace(nome_vitrine=None, nome_original=f"ler{RLO}fatura", sistema="caixa", classe=None)
    m = SimpleNamespace(nome="Ver faturas", descricao="Deixa ver.", validade_dias=10, ferramentas=[f])
    return _render("admin/modelos.html", modelos=[m], ferramentas=[], maximo_dias=30, so_leitura=False)


def _render_ordens() -> str:
    f = SimpleNamespace(descricao=None, nome_original=f"ler{RLO}fatura", sistema="caixa")
    o = SimpleNamespace(
        id=uuid.uuid4(), aplicativo="App", modelo="Ver", descricao="d", ferramentas=[f], fim=AGORA, ultimo_uso=None
    )
    m = SimpleNamespace(id=uuid.uuid4(), nome="Ver", descricao="d", validade_dias=10, ferramentas=[f])
    a = SimpleNamespace(id=uuid.uuid4(), nome="App")
    return _render("ordens.html", ativas=[o], terminadas=[], aplicativos=[a], modelos=[m], maximo_dias=30)


def _render_acao() -> str:
    op = operacao(
        estado=Estado.ENVIADA,
        resultado={"content": [{"type": "text", "text": f"Pago{RLO} 100.\nOutra linha{ZW}."}]},
        ficha_id=None,
    )
    return _render("acoes/acao.html", a=acoes.montar(op, AGORA), intervalo=2)


def _render_extrato() -> str:
    linha = SimpleNamespace(
        criado_em=AGORA,
        frase=f"Leitura em ler{RLO}fatura recusada: fora_da_vitrine.",
        protocolo=None,
        aplicativo="App",
        sistema=None,
    )
    return _render("extrato.html", linhas=[linha], pagina=1, ha_mais=False, alheio=None, base="/telas/extrato")


@pytest.mark.parametrize(
    ("onde", "montar", "marcas"),
    [
        ("sistema", _render_sistema, ["<code>ler[U+202E]fatura</code>", "Mostra[U+200B] a fatura."]),
        ("modelos", _render_modelos, ["<code>ler[U+202E]fatura</code>"]),
        ("ordens", _render_ordens, ["ler[U+202E]fatura (ainda sem revisão)", "Pode ler: ler[U+202E]fatura."]),
        (
            "limite",
            lambda: _pagina_limite(_linha_limite(f"ler{RLO}fatura", True)),
            ["<h1>Limite de ler[U+202E]fatura</h1>", "<dd>ler[U+202E]fatura, no sistema caixa</dd>"],
        ),
        (
            "limites",
            lambda: _lista_limites(
                [_linha_limite(f"ler{RLO}fatura", True)], [_linha_limite(f"ver{RLO}conta", True, "encerrado")]
            ),
            ["<td>ler[U+202E]fatura <span", "<li>ver[U+202E]conta pelo aplicativo"],
        ),
        ("resposta do sistema", _render_acao, ["Pago[U+202E] 100.<br>Outra linha[U+200B]."]),
        ("extrato", _render_extrato, ["Leitura em ler[U+202E]fatura recusada: fora_da_vitrine."]),
    ],
)
def test_textos_do_sistema_e_nomes_originais_mostram_a_marca(onde: str, montar: Any, marcas: list[str]) -> None:
    html = montar()
    for marca in marcas:
        assert marca in html, onde
    assert RLO not in html, onde
    assert ZW not in html, onde


# --- T3: o que a tela diz da passkey de um limite ---


def test_limite_ampliado_sem_a_passkey_nao_diz_criado_sem_a_passkey() -> None:
    """com_passkey segue a ultima ampliacao: um limite criado com a passkey e ampliado com o login refeito fica
    com com_passkey falso. A tela diz "criado ou ampliado sem a passkey", que vale nos dois casos."""
    sem = _linha_limite("mensagem_x", False)
    lista = _lista_limites([sem], [])
    assert "Foi criado ou ampliado sem a passkey: as ações dele passam pela ficha." in lista
    assert "Foi criado sem a passkey" not in lista

    pagina = _pagina_limite(sem)
    assert '<dd id="passkey-do-limite">Criado ou ampliado sem a passkey</dd>' in pagina
    assert ", com passkey" not in pagina
    com = _pagina_limite(_linha_limite("mensagem_x", True))
    assert '<dd id="passkey-do-limite">Pedida ao criar e a cada ampliação</dd>' in com
    # Com a passkey desligada nesta instalacao, o limite vale, e a tela continua dizendo como foi feito.
    desligada = _pagina_limite(sem, ligada=False)
    assert '<dd id="passkey-do-limite">Criado ou ampliado sem a passkey</dd>' in desligada
    assert "Não vale com a passkey ligada" not in desligada


# --- T4: a escolha que o ADMIN acabou de revisar ---


def test_aplicativo_da_escolha_e_mesma_acao() -> None:
    aplicativo, ferramenta = uuid.uuid4(), uuid.uuid4()
    assert limites.aplicativo_da_escolha(f"{aplicativo}:{ferramenta}") == aplicativo
    for ruim in ("", "x", f"nao-e-uuid:{ferramenta}"):
        assert limites.aplicativo_da_escolha(ruim) is None
    e = _escolha()
    outra = _escolha()
    escolhas = [outra, e]
    assert limites.mesma_acao(escolhas, e.aplicativo_id, e.sistema_id, e.nome_original) is e
    assert limites.mesma_acao(escolhas, uuid.uuid4(), e.sistema_id, e.nome_original) is None
    assert limites.mesma_acao(escolhas, e.aplicativo_id, uuid.uuid4(), e.nome_original) is None
    assert limites.mesma_acao(escolhas, e.aplicativo_id, e.sistema_id, "outra") is None


class EngineFalso:
    """As consultas sao trocadas por monkeypatch: a conexao nao e usada."""

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        yield SimpleNamespace()

    connect = begin


def _telas(monkeypatch: pytest.MonkeyPatch, *, ligada: bool, form: list[tuple[str, str]]) -> Telas:
    p = Parametros(APP_ENV="teste", PUBLIC_BASE_URL="https://mcp-claro.teste", FEATURE_PASSKEY=ligada)
    t = Telas(p, ConfigTelas(), cast(Any, EngineFalso()), cast(Any, None), cast(Any, None), criar_ambiente("UTC"))
    pessoa = SimpleNamespace(id=uuid.uuid4(), papel="pessoa", nome="Ana")
    sessao = Sessao(b"h", cast(Any, pessoa), "csrf", datetime.now(UTC) + timedelta(hours=1), datetime.now(UTC))

    async def exigir(_: Any) -> Sessao:
        return sessao

    async def formulario(*_: Any) -> Formulario:
        return Formulario(form)

    monkeypatch.setattr(t, "exigir_sessao", exigir)
    monkeypatch.setattr(t, "formulario", formulario)
    return t


def _consultas_falsas(monkeypatch: pytest.MonkeyPatch, nova: limites.Escolha, antiga: Any) -> None:
    linha = SimpleNamespace(
        aplicativo_id=nova.aplicativo_id,
        aplicativo=nova.aplicativo,
        ferramenta_id=nova.ferramenta_id,
        sistema_id=nova.sistema_id,
        sistema=nova.sistema,
        nome_original=nova.nome_original,
        nome_vitrine=nova.nome_vitrine,
        descricao=nova.descricao,
        risco=nova.risco,
        campos_destinatario=list(nova.campos),
    )

    async def ferramentas(*_: Any) -> list[Any]:
        return [linha]

    async def maximos(*_: Any) -> dict[str, int]:
        return {"ALLOWANCE_MAX_PER_DAY": 5, "ALLOWANCE_MAX_DAYS": 30}

    async def tem(*_: Any) -> bool:
        return True

    async def revisao(_: Any, ferramenta_id: uuid.UUID) -> Any:
        return antiga if antiga is not None and ferramenta_id == antiga.id else None

    async def nada(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(consultas, "ferramentas_de_acao", ferramentas)
    monkeypatch.setattr(politica, "varios", maximos)
    monkeypatch.setattr(passkeys, "tem_ativa", tem)
    monkeypatch.setattr(consultas, "revisao_por_id", revisao)
    monkeypatch.setattr(consultas, "travar_revisao", nada)


def _get(query: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/telas/limites/novo",
            "path_params": {},
            "headers": [],
            "query_string": urlencode(query).encode(),
        }
    )


def _post() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/telas/limites",
            "path_params": {},
            "headers": [],
            "query_string": b"",
        }
    )


def _arranjo_revisada(monkeypatch: pytest.MonkeyPatch) -> tuple[limites.Escolha, str]:
    """A acao foi revisada: a escolha nova tem outra versao revisada; o formulario traz a antiga."""
    nova = _escolha()
    antiga = SimpleNamespace(id=uuid.uuid4(), sistema_id=nova.sistema_id, nome_original=nova.nome_original)
    _consultas_falsas(monkeypatch, nova, antiga)
    return nova, f"{nova.aplicativo_id}:{antiga.id}"


def _texto(r: Response) -> str:
    return bytes(r.body).decode()


def test_criar_com_a_escolha_de_antes_da_revisao_diz_que_o_admin_revisou(monkeypatch: pytest.MonkeyPatch) -> None:
    nova, velha = _arranjo_revisada(monkeypatch)
    form = [("escolha", velha), ("destinatarios", "qualquer"), ("por_dia", "1"), ("dias", "1")]
    t = _telas(monkeypatch, ligada=False, form=form)
    r = asyncio.run(limites.TelasDeLimites(t).criar(_post()))
    assert r.status_code == 409
    html = _texto(r)
    assert "O ADMIN acabou de revisar esta ação. Abra Criar limite de novo." in html
    assert "não está nas suas ordens de serviço" not in html
    link = "/telas/limites/novo?" + urlencode({"escolha": nova.chave})
    assert f'<a class="botao" href="{link}" hx-boost="false">Criar limite para esta ação</a>' in html

    # O link de antes (GET) diz o mesmo.
    r = asyncio.run(limites.TelasDeLimites(t).novo(_get({"escolha": velha})))
    assert r.status_code == 409
    assert "O ADMIN acabou de revisar esta ação." in _texto(r)


def test_desafio_com_a_escolha_de_antes_da_revisao_diz_que_o_admin_revisou(monkeypatch: pytest.MonkeyPatch) -> None:
    """Com a passkey ligada, o botao chama primeiro o desafio. O formulario foi aberto antes da revisao e o toque
    vem depois: o desafio diz a frase e manda o caminho da versao nova, que o passkey.js mostra como botao."""
    nova, velha = _arranjo_revisada(monkeypatch)
    form = [("escolha", velha), ("destinatarios", "qualquer"), ("por_dia", "1"), ("dias", "1")]
    t = _telas(monkeypatch, ligada=True, form=form)
    r = asyncio.run(limites.TelasDeLimites(t).desafio_criar(_post()))
    assert r.status_code == 409
    assert json.loads(_texto(r)) == {
        "erro": "O ADMIN acabou de revisar esta ação. Abra Criar limite de novo.",
        "link": {
            "caminho": "/telas/limites/novo?" + urlencode({"escolha": nova.chave}),
            "texto": "Criar limite para esta ação",
        },
    }

    # Outra recusa do desafio (aqui, a acao saiu das ordens) segue sem link.
    t = _telas(monkeypatch, ligada=True, form=[("escolha", f"{uuid.uuid4()}:{uuid.uuid4()}"), ("por_dia", "1")])
    r = asyncio.run(limites.TelasDeLimites(t).desafio_criar(_post()))
    assert r.status_code == 400
    assert "link" not in json.loads(_texto(r))


def test_revisada_para_risco_alto_diz_logo_que_nao_entra_em_limite(monkeypatch: pytest.MonkeyPatch) -> None:
    """O ADMIN revisou a acao e a marcou como de risco alto: o formulario de antes nao manda criar de novo (seria
    outra recusa). Diz logo que acao de risco alto nunca entra em limite, sem botao, nas duas portas."""
    nova = _escolha("alto")
    antiga = SimpleNamespace(id=uuid.uuid4(), sistema_id=nova.sistema_id, nome_original=nova.nome_original)
    _consultas_falsas(monkeypatch, nova, antiga)
    velha = f"{nova.aplicativo_id}:{antiga.id}"
    form = [("escolha", velha), ("destinatarios", "qualquer"), ("por_dia", "1"), ("dias", "1")]

    t = _telas(monkeypatch, ligada=False, form=form)
    for r in (
        asyncio.run(limites.TelasDeLimites(t).criar(_post())),
        asyncio.run(limites.TelasDeLimites(t).novo(_get({"escolha": velha}))),
    ):
        assert r.status_code == 400
        html = _texto(r)
        assert "marcou como de risco alto" in html
        assert "Ações de risco alto nunca entram em limite" in html
        assert "Abra Criar limite de novo" not in html
        assert 'class="botao"' not in html or "Criar limite para esta ação" not in html

    t = _telas(monkeypatch, ligada=True, form=form)
    r = asyncio.run(limites.TelasDeLimites(t).desafio_criar(_post()))
    assert r.status_code == 400
    assert json.loads(_texto(r)) == {"erro": limites.REVISADA_RISCO_ALTO}


def test_resposta_desafio_so_leva_link_quando_pedido() -> None:
    assert json.loads(bytes(passkeys.resposta_desafio(None, "Frase.", status=409).body)) == {"erro": "Frase."}
    com = passkeys.resposta_desafio(None, "Frase.", status=409, link=("/telas/x", "Ir"))
    assert com.status_code == 409
    assert json.loads(bytes(com.body)) == {"erro": "Frase.", "link": {"caminho": "/telas/x", "texto": "Ir"}}
    assert json.loads(bytes(passkeys.resposta_desafio({"a": 1}, link=("/x", "y")).body)) == {"publicKey": {"a": 1}}


def test_acao_que_saiu_das_ordens_segue_com_a_mensagem_de_antes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem escolha de agora para a mesma acao (a versao antiga e de outra acao, ou nem existe), a tela diz que
    a acao nao esta nas ordens que valem agora."""
    nova = _escolha()
    de_outra = SimpleNamespace(id=uuid.uuid4(), sistema_id=uuid.uuid4(), nome_original=nova.nome_original)
    _consultas_falsas(monkeypatch, nova, de_outra)
    t = _telas(monkeypatch, ligada=False, form=[])
    for escolha in (f"{nova.aplicativo_id}:{de_outra.id}", f"{nova.aplicativo_id}:{uuid.uuid4()}", "lixo"):
        r = asyncio.run(limites.TelasDeLimites(t).novo(_get({"escolha": escolha})))
        assert r.status_code == 404, escolha
        assert "Esta ação não está nas suas ordens de serviço que valem agora." in _texto(r)
        assert "O ADMIN acabou de revisar" not in _texto(r)
    # Outro aplicativo, mesma acao: tambem nao e a mesma escolha.
    r = asyncio.run(limites.TelasDeLimites(t).novo(_get({"escolha": f"{uuid.uuid4()}:{de_outra.id}"})))
    assert r.status_code == 404

    # A versao antiga e da MESMA acao (mesmo sistema, mesmo nome), mas a escolha e de outro aplicativo: so o
    # aplicativo decide aqui. Com o aplicativo certo, a mesma versao antiga leva a "o ADMIN acabou de revisar".
    mesma = SimpleNamespace(id=uuid.uuid4(), sistema_id=nova.sistema_id, nome_original=nova.nome_original)
    _consultas_falsas(monkeypatch, nova, mesma)
    r = asyncio.run(limites.TelasDeLimites(t).novo(_get({"escolha": f"{uuid.uuid4()}:{mesma.id}"})))
    assert r.status_code == 404
    assert "Esta ação não está nas suas ordens de serviço que valem agora." in _texto(r)
    assert "O ADMIN acabou de revisar" not in _texto(r)
    r = asyncio.run(limites.TelasDeLimites(t).novo(_get({"escolha": f"{nova.aplicativo_id}:{mesma.id}"})))
    assert r.status_code == 409
    assert "O ADMIN acabou de revisar" in _texto(r)


def test_pagina_de_erro_sem_link_fica_como_era() -> None:
    html = _render("erro.html", titulo="T", mensagem="M")
    assert 'class="botao"' not in html


# --- T1: avisos da reativacao; T5: frases que sairam do extrato ---


def test_avisos_e_frase_da_reativacao() -> None:
    assert AVISOS["aplicativo_reativado"] == (
        "Pronto. O aplicativo foi reativado. Os pedidos dele voltam a passar, se nenhum bloqueio ou pausa os"
        " segurar. As ações dele que já tinham parado não voltam."
    )
    # Vale para todo POST que nao reativa: assistente ativo, programa (pausado ou nao) e id que nao existe.
    assert AVISOS["aplicativo_nada_mudou"] == (
        "Nada mudou: este aplicativo não é um assistente pausado. Um programa pausado volta pela tela do programa."
    )
    assert frase("admin_aplicativo_reativado", aplicativo="Goose", quem="Carla") == (
        "Aplicativo Goose reativado por Carla; estava pausado pela conferencia do banco na partida."
    )


@pytest.mark.parametrize("tipo", ["limite_criado_sem_passkey", "limite_ampliado_sem_passkey"])
def test_frases_sem_uso_sairam(tipo: str) -> None:
    assert tipo not in FRASES
    with pytest.raises(FraseDesconhecida):
        frase(tipo, por_dia=2, ferramenta="m")
