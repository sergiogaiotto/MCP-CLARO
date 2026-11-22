"""Segunda rodada de correcoes da Fase 2 nas telas, sem banco: campos de destinatario marcados pelo ADMIN,
limite sem passkey com a passkey ligada, paginas de erro fora da transacao, aviso de passkey e caracteres
escondidos que faltavam."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request
from starlette.responses import Response

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.telas import acoes, consultas, limites, passkeys
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.nucleo import Telas
from mcp_claro.telas.paginas import criar_ambiente
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import Sessao
from tests.unidade.test_telas_acoes_paginas import _escolha

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


# --- T5: caracteres escondidos que ainda passavam sem marca ---


@pytest.mark.parametrize(
    "ponto",
    [0x2800, 0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x180B, 0x180F, 0x2060, 0x2064, 0xFEFF, 0xE0041],
)
def test_escondidos_que_faltavam_viram_marca(ponto: int) -> None:
    bruto = f"con{chr(ponto)}ta"
    assert acoes.marcar_escondidos(bruto) == f"con[U+{ponto:04X}]ta"
    assert acoes.tem_escondidos({"conta": bruto})
    assert acoes.valor_legivel(["x", bruto]) == f'["x", "con[U+{ponto:04X}]ta"]'


def test_letras_de_verdade_ficam_como_estao() -> None:
    for texto in ("한국어", "ខ្មែរ", "Ação ç ã é", "ﾊﾝｶｸ", "⠁⠃", "a b"):
        assert acoes.marcar_escondidos(texto) == texto
        assert not acoes.tem_escondidos(texto)


# --- T1: os campos de destinatario sao os que o ADMIN marcou ---


def test_campos_de_entrada_e_campo_fora_do_esquema() -> None:
    esquema = {"type": "object", "properties": {"para": {"type": "string"}, "copia": {}, "texto": {}}}
    assert repositorio.campos_de_entrada(esquema) == ("para", "copia", "texto")
    assert repositorio.campos_de_entrada({"type": "object"}) == ()

    class SemBanco:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("nao chega ao banco com campo fora do esquema")

    with pytest.raises(repositorio.CampoForaDoEsquema) as erro:
        asyncio.run(
            repositorio.revisar_ferramenta(
                cast(Any, SemBanco()),
                sistema_id=uuid.uuid4(),
                definicao_declarada={"name": "enviar_email", "inputSchema": esquema},
                nome_vitrine="email_x",
                descricao="Envia e-mail.",
                classe="muda",
                revisado_por="admin",
                campos_destinatario=["para", "bcc"],
            )
        )
    assert erro.value.campos == ["bcc"]


# Fase 3: o que a tela de revisao de ferramenta recebe a mais (sistema proprio, sem previa de terceiro).
CONTEXTO_FASE3_REVISAO: dict[str, Any] = {
    "terceiro": False,
    "previa": None,
    "recusa_previa": None,
    "antes": None,
    "antes_esquema": "{}",
    "prefixo_descricao": "desc:",
    "descricao_maxima": 300,
}


def test_tela_do_admin_explica_e_marca_os_campos_de_destinatario() -> None:
    html = _render(
        "admin/ferramenta.html",
        sistema=SimpleNamespace(id=uuid.uuid4(), nome="caixa"),
        nome="enviar_email",
        definicao={"description": "Envia."},
        esquema_entrada="{}",
        campos_saida=[],
        campos_entrada=("para", "copia", "texto"),
        outros_campos="",
        revisao=None,
        mudou=False,
        valores={
            "nome_vitrine": "e",
            "titulo": "",
            "descricao": "",
            "classe": "muda",
            "risco": "medio",
            "campos": [],
            "destinatario": ["para", "copia"],
        },
        **CONTEXTO_FASE3_REVISAO,
    )
    assert "Campos do pedido que dizem o destinatário" in html
    assert "O MCP-CLARO confere só os campos marcados aqui" in html
    assert "O resto do pedido fica livre." in html
    assert '<input type="checkbox" name="destinatario" value="para" checked>' in html
    assert '<input type="checkbox" name="destinatario" value="copia" checked>' in html
    assert '<input type="checkbox" name="destinatario" value="texto" >' in html


def _novo(escolha: limites.Escolha) -> str:
    return _render(
        "acoes/limite_novo.html",
        e=escolha,
        maximo_dia=5,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )


def test_sem_campo_marcado_o_limite_so_vale_para_qualquer_destinatario() -> None:
    sem = dataclasses.replace(_escolha(), campos=())
    html = _novo(sem)
    assert "O ADMIN ainda não marcou qual campo desta ação diz o destinatário." in html
    assert 'value="estes"' not in html
    assert 'value="qualquer" checked' in html
    lido, erros = limites.ler_pedido(
        Formulario(
            [
                ("escolha", sem.chave),
                ("destinatarios", "estes"),
                ("campo", "texto"),
                ("valores", "x"),
                ("por_dia", "1"),
                ("dias", "1"),
            ]
        ),
        {sem.chave: sem},
        maximo_dia=5,
        maximo_dias=30,
    )
    assert lido is None
    assert erros == [limites.SEM_CAMPO_MARCADO]


def test_com_campos_marcados_a_tela_diz_o_que_e_conferido() -> None:
    um = _novo(dataclasses.replace(_escolha(), campos=("destinatario",)))
    assert "O ADMIN marcou este campo do pedido como o que diz o destinatário desta ação:" in um
    assert "O resto do pedido, como o texto, fica livre." in um
    assert "precisam vir vazios" not in um
    dois = _novo(dataclasses.replace(_escolha(), campos=("para", "copia")))
    assert "estes campos do pedido como os que dizem o destinatário desta ação: <strong>para, copia</strong>" in dois
    assert "<strong>para, copia</strong>, menos o que você escolher acima) precisam vir vazios" in dois
    # O texto da mensagem nao e campo marcado: o limite grava so os marcados.
    e = dataclasses.replace(_escolha(), campos=("para", "copia"))
    lido, _ = limites.ler_pedido(
        Formulario(
            [
                ("escolha", e.chave),
                ("destinatarios", "estes"),
                ("campo", "para"),
                ("valores", "chefe@org"),
                ("por_dia", "1"),
                ("dias", "1"),
            ]
        ),
        {e.chave: e},
        maximo_dia=5,
        maximo_dias=30,
    )
    assert lido is not None
    assert lido.destinatarios == [{"campo": "para", "valores": ["chefe@org"]}, {"campo": "copia", "valores": []}]


def test_escolha_le_a_coluna_da_ferramenta_revisada() -> None:
    linha = SimpleNamespace(
        aplicativo_id=uuid.uuid4(),
        aplicativo="App",
        ferramenta_id=uuid.uuid4(),
        sistema_id=uuid.uuid4(),
        sistema="caixa",
        nome_original="enviar_email",
        nome_vitrine="email_x",
        descricao="Envia.",
        risco="medio",
        campos_destinatario=["para", "copia"],
        origem="proprio",
    )
    assert limites.Escolha.da_linha(cast(Any, linha)).campos == ("para", "copia")
    assert (
        limites.Escolha.da_linha(cast(Any, SimpleNamespace(**{**vars(linha), "campos_destinatario": []}))).campos == ()
    )


# --- T2: limite criado sem a passkey, com a passkey ligada ---


def _linha_limite(com_passkey: bool, situacao: str = "ativo") -> Any:
    agora = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        ferramenta="mensagem_x",
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
        sistema_origem="proprio",
        sem_revisao=False,
    )


def test_limite_sem_passkey_nao_vale_com_ela_ligada() -> None:
    sem = _linha_limite(False)
    assert limites.nao_vale_com_passkey(sem, True)
    assert not limites.nao_vale_com_passkey(sem, False)
    assert not limites.nao_vale_com_passkey(_linha_limite(True), True)
    assert not limites.nao_vale_com_passkey(_linha_limite(False, "encerrado"), True)
    assert limites._limite(sem, True).situacao == "Não vale com a passkey ligada"
    assert limites._limite(sem, False).situacao == "Valendo"

    html = _render(
        "acoes/limite.html",
        l=limites._limite(sem, True),
        minimo_dia=2,
        maximo_dia=5,
        acima_do_teto=False,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )
    assert "<strong>Não vale com a passkey ligada.</strong>" in html
    assert "encerre este e crie outro, com a passkey" in html
    assert "/ampliar" not in html  # nao oferece ampliar
    assert "/encerrar" in html
    valendo = _render(
        "acoes/limite.html",
        l=limites._limite(_linha_limite(True), True),
        minimo_dia=2,
        maximo_dia=5,
        acima_do_teto=False,
        maximo_dias=30,
        ligada=True,
        tem_passkey=True,
        refazer=None,
        pede="",
    )
    assert "Não vale com a passkey ligada" not in valendo
    assert "/ampliar" in valendo

    lista = _render(
        "acoes/limites.html",
        ativos=[limites._limite(sem, True)],
        terminados=[],
        escolhas=[],
        de_risco_alto=[],
        maximo_dia=5,
        maximo_dias=30,
        ligada=True,
    )
    assert "<strong>Não vale com a passkey ligada.</strong>" in lista
    assert ">Ver</a>" in lista


# --- T3: as paginas de erro saem depois da transacao (uma conexao do pool por pedido) ---


class EngineContado:
    """Conta as conexoes abertas ao mesmo tempo, como o pool veria."""

    def __init__(self) -> None:
        self.abertas = 0

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        self.abertas += 1
        try:
            yield SimpleNamespace(begin_nested=contextlib.nullcontext)
        finally:
            self.abertas -= 1

    connect = begin


def _arranjo(monkeypatch: pytest.MonkeyPatch) -> tuple[Telas, EngineContado, list[int]]:
    engine = EngineContado()
    p = Parametros(APP_ENV="teste", PUBLIC_BASE_URL="https://mcp-claro.teste", FEATURE_PASSKEY=False)
    t = Telas(p, ConfigTelas(), cast(Any, engine), cast(Any, None), cast(Any, None), criar_ambiente("UTC"))
    vistas: list[int] = []
    pessoa = SimpleNamespace(id=uuid.uuid4(), papel="pessoa", teste=False)
    velha = Sessao(
        b"h", cast(Any, pessoa), "csrf", datetime.now(UTC) + timedelta(hours=1), datetime(2026, 1, 1, tzinfo=UTC)
    )

    async def sessao(_: Any) -> Sessao:
        return velha

    async def formulario(*_: Any) -> Formulario:
        return Formulario([("por_dia", "3"), ("dias", "5")])

    def erro(_: Any, status: int, *__: Any) -> Response:
        vistas.append(engine.abertas)
        return Response(status_code=status)

    monkeypatch.setattr(t, "exigir_sessao", sessao)
    monkeypatch.setattr(t, "formulario", formulario)
    monkeypatch.setattr(t, "erro", erro)
    return t, engine, vistas


def _pedido(**parametros: Any) -> Request:
    return Request({"type": "http", "method": "POST", "path_params": parametros, "headers": [], "query_string": b""})


def test_confirmar_sem_login_refeito_monta_a_pagina_fora_da_transacao(monkeypatch: pytest.MonkeyPatch) -> None:
    t, engine, vistas = _arranjo(monkeypatch)
    telas = acoes.TelasDeAcoes(t)

    async def ficha(*_: Any) -> Any:
        return SimpleNamespace(ficha_exige_passkey=False, ficha_risco="alto")

    async def pagina(_: Any, __: Any, *, erros: list[str], status: int) -> Response:
        vistas.append(engine.abertas)
        assert erros == [acoes.LOGIN_REFEITO_FICHA]
        return Response(status_code=status)

    monkeypatch.setattr(telas, "_ficha_da_pessoa", ficha)
    monkeypatch.setattr(telas, "_pede_login", lambda _: True)
    monkeypatch.setattr(telas, "_pagina_ficha", pagina)
    r = asyncio.run(telas.confirmar(_pedido(ficha_id=uuid.uuid4())))
    assert r.status_code == 403
    assert vistas == [0]  # nenhuma conexao aberta enquanto a pagina (que abre a sua) e montada


@pytest.mark.parametrize("rota", ["confirmar", "recusar"])
def test_ficha_alheia_responde_fora_da_transacao(monkeypatch: pytest.MonkeyPatch, rota: str) -> None:
    t, _, vistas = _arranjo(monkeypatch)
    telas = acoes.TelasDeAcoes(t)

    async def nenhuma(*_: Any) -> None:
        return None

    monkeypatch.setattr(telas, "_ficha_da_pessoa", nenhuma)
    r = asyncio.run(getattr(telas, rota)(_pedido(ficha_id=uuid.uuid4())))
    assert r.status_code == 404
    assert vistas == [0]


def test_ampliar_limite_alheio_responde_fora_da_transacao(monkeypatch: pytest.MonkeyPatch) -> None:
    t, _, vistas = _arranjo(monkeypatch)

    async def nenhum(*_: Any) -> None:
        return None

    monkeypatch.setattr(consultas, "limite_da_pessoa", nenhum)
    r = asyncio.run(limites.TelasDeLimites(t).ampliar(_pedido(limite_id=uuid.uuid4())))
    assert r.status_code == 404
    assert vistas == [0]


# --- Segunda rodada, conferencia: criar trava a versao revisada antes de ler os campos de destinatario ---


def test_ferramenta_da_escolha() -> None:
    aplicativo, ferramenta = uuid.uuid4(), uuid.uuid4()
    assert limites.ferramenta_da_escolha(f"{aplicativo}:{ferramenta}") == ferramenta
    for ruim in ("", "x", f"{aplicativo}", f"{aplicativo}:nao-e-uuid"):
        assert limites.ferramenta_da_escolha(ruim) is None


@pytest.mark.parametrize("valida", [True, False])
def test_criar_trava_a_revisao_antes_de_ler_as_escolhas(monkeypatch: pytest.MonkeyPatch, valida: bool) -> None:
    t, _, _ = _arranjo(monkeypatch)
    ferramenta = uuid.uuid4()
    escolha = f"{uuid.uuid4()}:{ferramenta}" if valida else "qualquer-coisa"
    passos: list[Any] = []

    async def formulario(*_: Any) -> Formulario:
        return Formulario([("escolha", escolha), ("destinatarios", "qualquer"), ("por_dia", "1"), ("dias", "1")])

    async def travar(_: Any, ferramenta_id: uuid.UUID) -> None:
        passos.append(("trava", ferramenta_id))

    async def revisao(_: Any, ferramenta_id: uuid.UUID) -> None:
        passos.append(("revisao", ferramenta_id))
        return None

    async def escolhas(*_: Any) -> list[Any]:
        passos.append("escolhas")
        return []

    async def maximos(*_: Any) -> dict[str, int]:
        return {"ALLOWANCE_MAX_PER_DAY": 5, "ALLOWANCE_MAX_DAYS": 30}

    async def pagina(*_: Any, status: int, **__: Any) -> Response:
        return Response(status_code=status)

    monkeypatch.setattr(t, "formulario", formulario)
    monkeypatch.setattr(consultas, "revisao_por_id", revisao)
    monkeypatch.setattr(consultas, "travar_revisao", travar)
    monkeypatch.setattr(consultas, "ferramentas_de_acao", escolhas)
    monkeypatch.setattr(limites.politica, "varios", maximos)
    telas = limites.TelasDeLimites(t)
    monkeypatch.setattr(telas, "_pagina_novo", pagina)
    r = asyncio.run(telas.criar(_pedido()))
    assert r.status_code == 400  # a escolha nao esta na lista (vazia aqui)
    assert passos == ([("revisao", ferramenta), ("trava", ferramenta), "escolhas"] if valida else ["escolhas"])


# --- T4: aviso a cada passkey cadastrada ou desativada ---


def test_texto_do_aviso_da_passkey() -> None:
    quando = datetime(2026, 11, 19, 15, 4, tzinfo=UTC)
    assert passkeys.texto_do_aviso("cadastrada", quando) == (
        "Uma passkey foi cadastrada na sua conta em 12:04 de 19/11/2026 (hora de Brasília)."
        " Se não foi você, fale com o ADMIN."
    )
    assert passkeys.texto_do_aviso("desativada", quando).startswith("Uma passkey foi desativada na sua conta em ")


def test_aviso_da_passkey_vai_para_a_fila_e_falha_nao_sobe(monkeypatch: pytest.MonkeyPatch) -> None:
    pedidos: list[tuple[Any, ...]] = []

    async def avisar(c: Any, pessoa_id: uuid.UUID, evento: str, assunto: str, corpo: dict[str, Any]) -> bool:
        pedidos.append((pessoa_id, evento, assunto, corpo))
        return True

    monkeypatch.setattr(passkeys, "avisar_pessoa", avisar)
    pessoa_id = uuid.uuid4()
    engine = EngineContado()
    quando = datetime(2026, 11, 19, 15, 4, tzinfo=UTC)
    assert asyncio.run(passkeys.avisar_mudanca(cast(Any, engine), pessoa_id, "cadastrada", quando))
    assert pedidos == [
        (
            pessoa_id,
            "passkey_cadastrada",
            "MCP-CLARO: uma passkey foi cadastrada na sua conta",
            {"texto": passkeys.texto_do_aviso("cadastrada", quando)},
        )
    ]

    class Quebrado:
        @contextlib.asynccontextmanager
        async def begin(self) -> AsyncIterator[Any]:
            raise OSError("banco fora")
            yield  # pragma: no cover

    assert not asyncio.run(passkeys.avisar_mudanca(cast(Any, Quebrado()), pessoa_id, "desativada", quando))


# --- T6: limpeza ---


def test_rota_antiga_dos_programas_e_inicio_sem_uso_sairam() -> None:
    from mcp_claro.telas import admin, paginas, rotas

    t = cast(Any, SimpleNamespace(p=None))
    caminhos = [getattr(r, "path", "") for r in admin.TelasDoAdmin(t).rotas()]
    assert "/admin/programas" not in caminhos  # a de telas/admin_programas.py e a que vale
    assert not hasattr(admin.TelasDoAdmin, "cadastrar_programa")
    assert not hasattr(consultas, "todos_programas")
    assert not hasattr(consultas, "cadastrar_programa")
    assert not hasattr(rotas.TelasDaPessoa, "inicio")  # o inicio e TelasDeAcoes.inicio
    assert not (paginas.PASTA_MODELOS / "admin" / "programas.html").exists()


def test_inicio_mostra_meus_programas_so_para_quem_cuida_de_um() -> None:
    sessao = SimpleNamespace(pessoa=SimpleNamespace(nome="Ana", teste=True), admin=False)
    com = _render("inicio.html", sessao=sessao, pendencias=None, cuida_de_programa=True)
    assert '<a href="/telas/programas">Meus programas</a>' in com
    sem = _render("inicio.html", sessao=sessao, pendencias=None, cuida_de_programa=False)
    assert "/telas/programas" not in sem
