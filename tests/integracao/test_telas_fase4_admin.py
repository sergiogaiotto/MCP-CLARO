"""Telas do ADMIN da Fase 4 (selos de nivel 2 e 3), de ponta a ponta: PostgreSQL de verdade, provedor OIDC falso, as
telas com o usuario do banco da entrada (``mcp_claro_entrada``: se faltar GRANT, estes testes falham) e a rota de
chaves do despachante de verdade (``mcp_claro_despachante``, cifra falsa), sem as tarefas dele: a bateria (G3b) e
escrita ao mesmo tempo, e aqui o pedido de teste fica como a tela o gravou. Os testes gravados, os pedidos que
andaram e as retiradas automaticas sao montados pelo banco, como dono, com ``tests/apoio_selos.py``.

Provas do desenho 12.1 G6 do lado do ADMIN, com as correcoes da critica: o cartao "Selo" (selo de agora, de selo
lido), a declaracao com CSRF, login refeito e a caixa (a incompleta da 400 com a ferramenta que falta), o pedido do
teste (409 com outro esperando ou rodando; o numero estimado de escritas reais, B5), o relatorio por ferramenta e caso
com o que nao testamos e o JSON (sem IDs do rascunho, B6), a coluna "Selo" da lista, a retirada com o motivo, a chave
de teste que nunca troca a de producao (M11) e o 403 a quem nao e ADMIN.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.selo import contrato
from mcp_claro.selo import declaracao as declaracao_do_selo
from mcp_claro.telas import ConfigTelas, admin, rotas_telas
from tests import apoio_g3a as g
from tests import apoio_selos as apoio
from tests import apoio_terceiros as t3
from tests.apoio import porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_oidc_falso import IdpOidcFalso
from tests.integracao.test_telas import PUBLICO, SEGREDO_TELAS, Ambiente, Navegador, engine_do_papel, token_csrf

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

NOME_DO_ADMIN = "Carla Admin"
CHAVE_DE_TESTE = "chave-de-teste-do-selo"
PROIBIDAS = re.compile(r"certificad|conforme|aprovado no|MWS|write-safety|write safety", re.IGNORECASE)
"""O que nenhuma tela, relatorio ou JSON do selo diz (critica B6 e D2). "garante" so na negacao
(``_garante_so_negado``)."""


@dataclass
class Cena:
    amb: Ambiente
    nav: Navegador
    csrf: str
    admin_id: uuid.UUID
    cifra: CifraFalsa

    @property
    def dono(self) -> AsyncEngine:
        return self.amb.engine

    async def proprio(self, **extra: Any) -> apoio.SistemaDoSelo:
        async with self.dono.begin() as c:
            return await apoio.proprio_com_acoes(c, **extra)

    async def ficha(self, sistema_id: uuid.UUID) -> httpx2.Response:
        r = await self.nav.get(f"/telas/admin/sistemas/{sistema_id}")
        assert r.status_code == 200, r.text
        return r

    async def declarar(
        self, sistema_id: uuid.UUID, dados: Any, *, confirmo: bool = True, csrf: str | None = None
    ) -> httpx2.Response:
        texto = dados if isinstance(dados, str) else json.dumps(dados, ensure_ascii=False)
        pares = {"declaracao": texto} | ({"confirmo": "sim"} if confirmo else {})
        return await self.nav.post(
            f"/telas/admin/sistemas/{sistema_id}/selo/declaracao", pares, csrf=self.csrf if csrf is None else csrf
        )

    async def entregar_chave_de_teste(self, sistema_id: uuid.UUID, segredo: str = CHAVE_DE_TESTE) -> httpx2.Response:
        return await self.nav.post(
            f"/telas/admin/sistemas/{sistema_id}/chave",
            {"tipo": contrato.TIPO_CHAVE_TESTE, "segredo": segredo},
            csrf=self.csrf,
        )

    async def pedir(self, sistema_id: uuid.UUID, nivel: str = "2") -> httpx2.Response:
        return await self.nav.post(f"/telas/admin/sistemas/{sistema_id}/selo/testar", {"nivel": nivel}, csrf=self.csrf)

    async def retirar(self, sistema_id: uuid.UUID, motivo: str) -> httpx2.Response:
        return await self.nav.post(
            f"/telas/admin/sistemas/{sistema_id}/selo/retirar", {"motivo": motivo}, csrf=self.csrf
        )

    async def envelhecer_login(self, minutos: int = 30) -> None:
        """O login do ADMIN passa a ser de ``minutos`` atras, pelo relogio do banco (a sessao segue valendo)."""
        async with self.dono.begin() as c:
            await c.execute(
                text(
                    "UPDATE sessoes_telas SET auth_time = now() - make_interval(mins => :m)"
                    " WHERE pessoa_id = :p AND encerrada_em IS NULL"
                ),
                {"p": self.admin_id, "m": minutos},
            )

    async def um(self, sql: str, **valores: Any) -> Any:
        async with self.dono.connect() as c:
            return (await c.execute(text(sql), valores)).first()

    async def todos(self, sql: str, **valores: Any) -> list[Any]:
        async with self.dono.connect() as c:
            return list((await c.execute(text(sql), valores)).all())


@contextlib.asynccontextmanager
async def subir(banco_migrado: Parametros, **extra: Any) -> AsyncIterator[Cena]:
    """As telas e as rotas do despachante; ``extra`` troca parametros (``BADGE_TEST_MAX_CALLS``, por exemplo)."""
    porta_idp, porta_desp = porta_livre(), porta_livre()
    idp = IdpOidcFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro", segredo=SEGREDO_TELAS)
    p = Parametros(
        **(
            banco_migrado.model_dump()
            | {
                "APP_ENV": "teste",
                "PUBLIC_BASE_URL": PUBLICO,
                "MCP_RESOURCE_ID": None,
                "WATCH_PUBLIC_URL": None,
                "ALLOWED_HOSTS": ["127.0.0.1:*"],
                "IDP_KIND": "oidc",
                "IDP_ISSUER_URL": idp.emissor,
                "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
                "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}",
                "RESPONSE_MAX_WAIT_SECONDS": 5,
            }
            | extra
        )
    )
    engine = criar_engine(p, pool_size=3, max_overflow=3)
    engine_entrada = engine_do_papel(p, "mcp_claro_entrada")
    desp_db = g.engine_do_papel(p, "mcp_claro_despachante")
    cifra = CifraFalsa()
    http = httpx2.AsyncClient(follow_redirects=False, timeout=15, trust_env=False)
    despachante = ClienteDespachante(p)
    cfg = ConfigTelas(segredo=SecretStr(SEGREDO_TELAS))
    app = Starlette(routes=rotas_telas(p, engine_entrada, despachante, config=cfg, http_login=http))
    # So as rotas do despachante, sem a partida dele: nenhuma tarefa (porta, avisos, bateria) mexe no que o teste monta.
    so_rotas = Starlette(routes=list(criar_app_despachante(p, desp_db, cifra).routes))
    try:
        async with servidor(idp.app(), porta_idp), servidor(so_rotas, porta_desp), servidor(app) as url_telas:
            amb = Ambiente(p, idp, url_telas, engine, http, "")
            nav = amb.navegador()
            sujeito = f"admin-{uuid.uuid4().hex[:8]}"
            await amb.entrar(nav, sujeito, NOME_DO_ADMIN)
            await amb.tornar_admin(sujeito)
            admin_id = (await amb.pessoa(sujeito)).id
            csrf = token_csrf((await nav.get("/telas/admin")).text)
            yield Cena(amb, nav, csrf, admin_id, cifra)
    finally:
        await despachante.fechar()
        await http.aclose()
        await engine_entrada.dispose()
        await desp_db.dispose()
        await engine.dispose()


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    async with subir(banco_migrado) as c:
        yield c


def _texto(html: str) -> str:
    """O texto visivel da pagina, sem as marcas e com os espacos juntos (as entidades do Jinja voltam a ser letras)."""
    sem_tags = re.sub(r"<[^>]+>", " ", html)
    for de, para in (("&#34;", '"'), ("&#39;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        sem_tags = sem_tags.replace(de, para)
    return re.sub(r"\s+", " ", sem_tags)


def _erros(html: str) -> str:
    """O texto da caixa "Falta corrigir" da pagina (vazio se nao ha)."""
    if 'class="erros"' not in html:
        return ""
    return _texto(html.split('class="erros"', 1)[1].split(">", 1)[1].split("</div>", 1)[0]).strip()


def _cartao(html: str) -> str:
    """O texto do cartao "Selo" da ficha do sistema."""
    return _texto(html.split('id="titulo-selo"', 1)[1].split("</section>", 1)[0])


@dataclass(frozen=True)
class PlanoNoCartao:
    chamadas: int
    escritas: int
    teto_dito: int | None
    """O teto que o cartao diz que o plano passa ("Passa do teto de M chamadas: este teste não começa."), ou None."""


def _planos(cartao: str) -> dict[int, PlanoNoCartao]:
    """O plano de cada nivel no cartao "Selo" (critica B5)."""
    return {
        int(n): PlanoNoCartao(int(ch), int(es), int(teto) if teto else None)
        for n, ch, es, teto in re.findall(
            r"Teste de nível (\d): pela declaração, até (\d+) chamadas, (\d+) delas escritas reais no alvo de teste\."
            r"(?: Passa do teto de (\d+) chamadas: este teste não começa\.)?",
            cartao,
        )
    }


def _garante_so_negado(texto: str) -> bool:
    return all(texto[max(0, m.start() - 4) : m.start()] == "não " for m in re.finditer(r"garant", texto))


def _sem_promessa_nem_rascunho(texto: str) -> None:
    assert not PROIBIDAS.search(texto), PROIBIDAS.search(texto)
    assert _garante_so_negado(texto)


async def _proprio_compartilhado(c: Any) -> uuid.UUID:
    """Um sistema proprio ativo com uma chave so para todos (que so le)."""
    nome = f"compartilhado-{uuid.uuid4().hex[:8]}"
    return uuid.UUID(
        str(
            (
                await c.execute(
                    text(
                        "INSERT INTO sistemas (nome, endereco, tipo_chave, situacao, versao_mcp)"
                        " VALUES (:n, :e, 'compartilhada', 'ativo', '2026-07-28') RETURNING id"
                    ),
                    {"n": nome, "e": f"https://{nome}.exemplo.teste/mcp"},
                )
            ).scalar_one()
        )
    )


async def _declaracao_e_chave(cena: Cena, sistema: apoio.SistemaDoSelo) -> None:
    r = await cena.declarar(sistema.id, apoio.declaracao([a.nome_original for a in sistema.acoes]))
    assert r.status_code == 303, r.text
    r = await cena.entregar_chave_de_teste(sistema.id)
    assert r.status_code == 303, r.text


# --- 1. O cartao "Selo" ----------------------------------------------------------------------------------------


async def test_cartao_do_selo_de_um_sistema_nunca_testado(cena: Cena) -> None:
    sistema = await cena.proprio()
    r = await cena.ficha(sistema.id)
    cartao = _cartao(r.text)
    assert "Selo agora: nível 1. Nunca testado." in cartao
    assert admin.SELO_NAO_GARANTE in cartao
    assert admin.TEXTOS_DO_CARTAO["fase_desligada"] in cartao  # FEATURE_PHASE4_BADGES desligada: so pessoas de teste
    for n in contrato.NAO_TESTADOS:
        assert n.texto in cartao  # o que o teste nunca cobre, sempre no cartao, sem IDs do rascunho
    assert "Nenhuma declaração de teste salva ainda." in cartao
    assert admin.SELO_CONFIRMO in cartao
    assert admin.SELO_NOTA_DO_ALVO in cartao
    assert admin.SELO_ATENCAO_DO_ALVO in cartao
    assert "Nenhuma chave de teste entregue ainda." in cartao
    assert admin.SELO_SEM_DECLARACAO in cartao  # sem declaracao e chave, nada de "Pedir o teste"
    assert "Pedir o teste de nível 2" not in cartao
    assert "Retirar o selo" not in cartao  # nivel 1: nada a retirar
    assert f'action="/telas/admin/sistemas/{sistema.id}/selo/declaracao"' in r.text
    assert 'name="confirmo" value="sim" required' in r.text
    assert 'name="tipo" value="teste_selo"' in r.text
    _sem_promessa_nem_rascunho(_texto(r.text))


async def test_cartao_le_o_selo_do_banco_e_nunca_da_coluna_antiga(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        teste = await apoio.gravar_teste(c, sistema.id, nivel=3, admin_id=cena.admin_id)
    cartao = _cartao((await cena.ficha(sistema.id)).text)
    assert (await cena.um("SELECT selo FROM sistemas WHERE id = :s", s=sistema.id)).selo == 1  # a coluna antiga
    assert f"Selo agora: nível 3. Teste de {_dia(teste.testado_em)}, vale até {_dia(teste.vale_ate)}." in cartao
    assert "Retirar o selo" in cartao
    assert "Por que retirar o selo" in cartao


async def test_ferramenta_nova_vem_marcada_no_selo_agora(cena: Cena) -> None:
    """O nome da ferramenta nova (texto do sistema) vai no "Selo agora" dentro de ``<code class="estranho">`` com os
    escondidos marcados, como na tabela das ferramentas da mesma ficha, e nunca cru: um U+202E inverteria a frase."""
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id)
        for nome in ("apagar\u202elembrete", "remarcar_<b>lembrete</b>"):
            await t3.revisar_ferramenta(
                c,
                sistema.id,
                t3.definicao(nome, anotacoes={"readOnlyHint": False}),
                classe="muda",
                nome_vitrine=f"nova_{uuid.uuid4().hex[:12]}",
            )
    html = (await cena.ficha(sistema.id)).text
    secao = html.split('id="titulo-selo"', 1)[1].split("</section>", 1)[0]
    assert "\u202e" not in secao
    assert "<b>" not in secao
    assert (
        "<strong>Selo agora: nível 1. Voltou ao nível 1: as ferramentas"
        ' <code class="estranho">apagar[U+202E]lembrete</code>,'
        ' <code class="estranho">remarcar_&lt;b&gt;lembrete&lt;/b&gt;</code>'
        " são novas ou mudaram depois do teste.</strong>"
    ) in secao


async def test_cartao_marca_o_texto_da_equipe_na_declaracao(cena: Cena) -> None:
    """O alvo, o responsavel e as ferramentas da declaracao sao texto da equipe do sistema: no cartao, com a classe
    ``estranho`` e os escondidos marcados (a declaracao aceita um preenchedor invisivel como U+3164 no responsavel)."""
    sistema = await cena.proprio()
    r = await cena.declarar(sistema.id, apoio.declaracao(["enviar_lembrete"], responsavel="Equipe\u3164 do sistema"))
    assert r.status_code == 303, _erros(r.text) or r.text[:300]
    html = (await cena.ficha(sistema.id)).text
    secao = html.split('id="titulo-selo"', 1)[1].split("</section>", 1)[0]
    assert "\u3164" not in secao
    assert f', no alvo <code class="estranho">{apoio.ENDERECO_TESTE}</code>.' in secao
    assert 'Responsável, como a declaração diz: <span class="estranho">Equipe[U+3164] do sistema</span>.' in secao
    assert 'Ferramentas de ação declaradas: <code class="estranho">enviar_lembrete</code>.' in secao


def _dia(momento: Any) -> str:
    """A data como as telas mostram, no fuso padrao das telas (``SCREENS_TIMEZONE``)."""
    return str(momento.astimezone(ZoneInfo(ConfigTelas(segredo=SecretStr("x")).fuso)).strftime("%d/%m/%Y"))


async def test_cartao_de_terceiro_diz_que_o_selo_ainda_nao_existe(cena: Cena) -> None:
    async with cena.dono.begin() as c:
        terceiro = await t3.cadastrar_terceiro(c)
    r = await cena.ficha(terceiro)
    cartao = _cartao(r.text)
    assert declaracao_do_selo.TERCEIRO_AINDA_NAO in cartao
    assert "selo/declaracao" not in r.text
    assert "selo/testar" not in r.text
    r = await cena.declarar(terceiro, apoio.declaracao(["enviar_lembrete"]))
    assert r.status_code == 409
    assert declaracao_do_selo.TERCEIRO_AINDA_NAO in _erros(r.text)
    r = await cena.pedir(terceiro)
    assert r.status_code == 409
    assert _erros(r.text) == f"Falta corrigir: {declaracao_do_selo.TERCEIRO_AINDA_NAO}"
    assert await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=terceiro) == []
    assert await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s", s=terceiro) == []


async def test_cartao_de_chave_compartilhada_diz_por_que_nao(cena: Cena) -> None:
    async with cena.dono.begin() as c:
        sistema_id = await _proprio_compartilhado(c)
    cartao = _cartao((await cena.ficha(sistema_id)).text)
    assert admin.SELO_SO_POR_PESSOA in cartao
    assert "selo/testar" not in cartao


async def test_selo_que_nao_le_nao_para_a_ficha_nem_a_lista(cena: Cena, monkeypatch: pytest.MonkeyPatch) -> None:
    """Decisao 3 (A1): se a leitura do selo falhar, a ficha e a lista abrem do mesmo jeito e dizem que nao leram."""
    sistema = await cena.proprio()

    async def falha(*_: Any, **__: Any) -> Any:
        raise OperationalError("SELECT * FROM selo_vigente(:s)", {}, Exception("selo_vigente falhou"))

    monkeypatch.setattr(admin, "cartao_do_selo", falha)
    monkeypatch.setattr(admin, "selos_da_lista", falha)
    r = await cena.ficha(sistema.id)
    assert admin.SELO_NAO_LIDO in _cartao(r.text)
    assert "Selo agora" not in r.text
    r = await cena.nav.get("/telas/admin/sistemas")
    assert r.status_code == 200
    linha = r.text.split(f">{sistema.nome}</a>", 1)[1].split("</tr>", 1)[0]
    assert "não lido agora" in _texto(linha)


# --- 2. Declaracao de teste --------------------------------------------------------------------------------------


async def test_declaracao_pede_csrf_login_refeito_e_a_caixa(cena: Cena) -> None:
    sistema = await cena.proprio()
    dados = apoio.declaracao(["enviar_lembrete"])
    r = await cena.declarar(sistema.id, dados, csrf="")  # sem CSRF
    assert r.status_code == 403
    assert "Este pedido não veio desta tela" in _texto(r.text)

    await cena.envelhecer_login()
    r = await cena.declarar(sistema.id, dados)
    assert r.status_code == 403
    recusa = _texto(r.text)
    assert admin.SELO_LOGIN_REFEITO in _erros(r.text)
    assert admin.SELO_PEDE_LOGIN in recusa
    volta = urlencode({"voltar": f"/telas/admin/sistemas/{sistema.id}"})
    assert f'href="/telas/login/refazer?{volta}"' in r.text  # "Entrar de novo agora" volta para a ficha, por GET
    assert "teste do selo" in r.text  # o JSON colado volta na caixa de texto
    assert await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=sistema.id) == []

    async with cena.dono.begin() as c:  # o login refeito agora
        await c.execute(text("UPDATE sessoes_telas SET auth_time = now() WHERE pessoa_id = :p"), {"p": cena.admin_id})
    r = await cena.declarar(sistema.id, dados, confirmo=False)
    assert r.status_code == 400
    assert admin.SELO_SEM_CONFIRMO in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=sistema.id) == []

    r = await cena.declarar(sistema.id, dados)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/telas/admin/sistemas/{sistema.id}?aviso=selo_declaracao_salva"
    salva = await cena.um(
        "SELECT versao, salva_por, pessoa_id, modo, endereco_teste, declaracao FROM declaracoes_selo"
        " WHERE sistema_id = :s",
        s=sistema.id,
    )
    assert (salva.versao, salva.salva_por, salva.pessoa_id, salva.modo) == (
        1,
        NOME_DO_ADMIN,
        cena.admin_id,
        "ambiente_de_teste",
    )
    assert salva.endereco_teste == apoio.ENDERECO_TESTE
    linha = await cena.um(
        "SELECT frase, pessoa_id, dados FROM extrato WHERE sistema_id = :s AND tipo = 'admin_selo_declaracao_salva'",
        s=sistema.id,
    )
    assert linha.pessoa_id == cena.admin_id
    assert f"salva por {NOME_DO_ADMIN} (versao 1)" in linha.frase
    assert linha.dados["versao"] == 1

    pagina = await cena.nav.get(r.headers["location"])
    assert "Pronto. A declaração de teste foi salva (versão 1)." in _texto(pagina.text)
    cartao = _cartao(pagina.text)
    assert f"Versão 1, salva por {NOME_DO_ADMIN}" in cartao
    assert "O teste roda no ambiente de teste do sistema, no alvo" in cartao
    assert apoio.ENDERECO_TESTE in cartao

    r = await cena.declarar(sistema.id, dados)  # outra versao, so de acrescentar
    assert r.status_code == 303
    pagina = await cena.nav.get(r.headers["location"])
    assert "Pronto. A declaração de teste foi salva (versão 2)." in _texto(pagina.text)


async def test_declaracao_incompleta_da_400_com_a_ferramenta_que_falta(cena: Cena) -> None:
    sistema = await cena.proprio(acoes=("enviar_lembrete", "apagar_lembrete"))
    r = await cena.declarar(sistema.id, apoio.declaracao(["enviar_lembrete"]))
    assert r.status_code == 400
    assert "A declaração não cobre a ferramenta apagar_lembrete. Toda ferramenta de ação precisa estar nela." in (
        _erros(r.text)
    )
    for errado, frase in (
        ("{nao e json", "A declaração não é um JSON válido."),
        ("[]", "A declaração é um objeto JSON, entre { e }."),
    ):
        r = await cena.declarar(sistema.id, errado)
        assert r.status_code == 400
        assert frase in _erros(r.text)
    producao = apoio.declaracao(["enviar_lembrete", "apagar_lembrete"], endereco_teste=sistema.endereco)
    r = await cena.declarar(sistema.id, producao)
    assert r.status_code == 400
    assert "A bateria faz escritas reais no alvo: ele nunca pode ser a produção." in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=sistema.id) == []


async def test_declaracao_grande_cabe_no_formulario_e_a_de_mais_de_64_kib_tem_a_frase(cena: Cena) -> None:
    """O formulario da declaracao tem teto proprio: um JSON perto dos 64 KiB, com letras que viram %XX no formulario
    (mais de 64 KiB de corpo), chega a conferencia e e salvo; um de mais de 64 KiB volta com a frase da declaracao, e
    nao com a recusa geral do formulario."""
    sistema = await cena.proprio()
    dados = apoio.declaracao(["enviar_lembrete"])
    longo = "é" * 7000  # 14 KB em UTF-8 e 42 KB no formulario, por campo
    entrada = dados["ferramentas"]["enviar_lembrete"]
    entrada["argumentos"] = {"para": "caixa-de-teste@exemplo.teste", "texto": longo}
    entrada["argumentos_outros"] = {"para": "caixa-de-teste@exemplo.teste", "texto": longo + "x"}
    entrada["argumentos_recusados"] = {"para": "nao-e-um-endereco", "texto": longo}
    texto = json.dumps(dados, ensure_ascii=False)
    assert len(texto.encode()) < contrato.TETO_DECLARACAO_BYTES < len(urlencode({"declaracao": texto}))
    r = await cena.declarar(sistema.id, texto)
    assert r.status_code == 303, _erros(r.text) or r.text[:300]
    curto = "x" * 5000
    grande = dict(dados, ferramentas={})
    for n in range(5):  # mais de 64 KiB de JSON, sem letra que vire %XX: o corpo cabe no teto do formulario
        grande["ferramentas"][f"acao_{n}"] = {
            k: {"para": "caixa-de-teste@exemplo.teste", "texto": curto + k}
            for k in ("argumentos", "argumentos_outros", "argumentos_recusados")
        } | {"rastro": entrada["rastro"]}
    r = await cena.declarar(sistema.id, json.dumps(grande, ensure_ascii=False))
    assert r.status_code == 400
    assert "A declaração passa de 64 KiB. Encurte os argumentos." in _erros(r.text)
    assert len(await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=sistema.id)) == 1


async def test_recusa_do_banco_vira_frase_da_tela(cena: Cena, monkeypatch: pytest.MonkeyPatch) -> None:
    """A segunda barreira: sem a conferencia da tela, o banco recusa o alvo que e a producao, e a tela diz por que
    com acento (nada de erro 500 nem do texto do banco)."""
    sistema = await cena.proprio()
    monkeypatch.setattr(declaracao_do_selo, "conferir", lambda *a, **k: None)
    r = await cena.declarar(sistema.id, apoio.declaracao(["enviar_lembrete"], endereco_teste=sistema.endereco))
    assert r.status_code == 409
    pagina = _texto(r.text)
    assert admin.RECUSAS_DO_BANCO[0][1] in _erros(r.text)
    assert "a bateria nao escreve na producao" not in pagina
    assert await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=sistema.id) == []


# --- 3. Pedir o teste --------------------------------------------------------------------------------------------


async def test_pedir_o_teste_grava_o_pedido_e_outro_rodando_da_409(cena: Cena) -> None:
    sistema = await cena.proprio()
    r = await cena.pedir(sistema.id)  # sem declaracao
    assert r.status_code == 409
    assert admin.SELO_SEM_DECLARACAO in _erros(r.text)
    r = await cena.declarar(sistema.id, apoio.declaracao(["enviar_lembrete"]))
    assert r.status_code == 303
    r = await cena.pedir(sistema.id)  # sem a chave de teste
    assert r.status_code == 409
    assert admin.SELO_SEM_DECLARACAO in _erros(r.text)
    r = await cena.entregar_chave_de_teste(sistema.id)
    assert r.status_code == 303

    cartao = _cartao((await cena.ficha(sistema.id)).text)
    assert "Última chave de teste entregue ao cofre em" in cartao
    planos = _planos(cartao)
    assert set(planos) == {2, 3}  # critica B5: o numero estimado de escritas reais, antes de pedir
    assert 0 < planos[2].escritas < planos[2].chamadas
    assert planos[2].chamadas < planos[3].chamadas
    assert planos[2].escritas < planos[3].escritas
    teto = cena.amb.p.BADGE_TEST_MAX_CALLS
    for plano in planos.values():  # uma acao so: cabe no teto de sempre, e o cartao nao diz que passa
        assert plano.chamadas <= teto
        assert plano.teto_dito is None
    assert "Pedir o teste de nível 2" in cartao
    assert "Pedir o teste de nível 3" in cartao

    r = await cena.pedir(sistema.id, "4")
    assert r.status_code == 400
    assert admin.SELO_NIVEL_FORA in _erros(r.text)

    await cena.envelhecer_login()
    r = await cena.pedir(sistema.id)
    assert r.status_code == 403
    assert admin.SELO_LOGIN_REFEITO in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s", s=sistema.id) == []
    async with cena.dono.begin() as c:
        await c.execute(text("UPDATE sessoes_telas SET auth_time = now() WHERE pessoa_id = :p"), {"p": cena.admin_id})

    r = await cena.pedir(sistema.id, "2")
    assert r.status_code == 303, r.text
    assert r.headers["location"].endswith("?aviso=selo_teste_pedido")
    pedido = await cena.um(
        "SELECT p.id, p.nivel, p.estado, p.pedido_por, d.versao FROM pedidos_teste_selo p"
        " JOIN declaracoes_selo d ON d.id = p.declaracao_id WHERE p.sistema_id = :s",
        s=sistema.id,
    )
    assert (pedido.nivel, pedido.estado, pedido.pedido_por, pedido.versao) == (2, "pedido", cena.admin_id, 1)
    linha = await cena.um(
        "SELECT frase, pessoa_id FROM extrato WHERE sistema_id = :s AND tipo = 'admin_selo_teste_pedido'", s=sistema.id
    )
    assert linha.frase == f"Teste de selo nivel 2 do sistema {sistema.nome} pedido por {NOME_DO_ADMIN}."
    pagina = await cena.nav.get(r.headers["location"])
    assert "Pronto. O teste foi pedido; o resultado aparece aqui quando terminar." in _texto(pagina.text)
    assert re.search(r"Teste de nível 2 pedido às [0-9/: ]+; esperando a vez\.", _cartao(pagina.text))

    r = await cena.pedir(sistema.id, "3")
    assert r.status_code == 409
    assert admin.SELO_TESTE_JA_RODANDO in _erros(r.text)
    async with cena.dono.begin() as c:  # o despachante pegou o pedido
        await c.execute(text("UPDATE pedidos_teste_selo SET estado = 'rodando' WHERE id = :i"), {"i": pedido.id})
    assert re.search(r"Teste de nível 2 pedido às [0-9/: ]+; rodando\.", _cartao((await cena.ficha(sistema.id)).text))
    r = await cena.pedir(sistema.id, "3")
    assert r.status_code == 409
    assert admin.SELO_TESTE_JA_RODANDO in _erros(r.text)
    assert len(await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s", s=sistema.id)) == 1


async def test_so_a_chave_de_teste_libera_o_pedido_e_o_nivel_3_fica_gravado(cena: Cena) -> None:
    """A chave de leitura das declaracoes, entregue pela mesma rota, nao conta como chave de teste (so a linha
    ``admin_chave_entregue`` com a entrega ``teste_selo``); e "Pedir o teste de nível 3" grava o nivel 3."""
    sistema = await cena.proprio()
    r = await cena.declarar(sistema.id, apoio.declaracao(["enviar_lembrete"]))
    assert r.status_code == 303
    r = await cena.nav.post(
        f"/telas/admin/sistemas/{sistema.id}/chave",
        {"tipo": "declaracoes", "segredo": "chave-das-declaracoes"},
        csrf=cena.csrf,
    )
    assert r.status_code == 303, _erros(r.text) or r.text[:300]
    entregas = await cena.todos(
        "SELECT dados ->> 'entrega' AS entrega FROM extrato WHERE sistema_id = :s AND tipo = 'admin_chave_entregue'",
        s=sistema.id,
    )
    assert [x.entrega for x in entregas] == ["declaracoes"]
    assert "Nenhuma chave de teste entregue ainda." in _cartao((await cena.ficha(sistema.id)).text)
    r = await cena.pedir(sistema.id, "3")
    assert r.status_code == 409
    assert admin.SELO_SEM_DECLARACAO in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s", s=sistema.id) == []

    r = await cena.entregar_chave_de_teste(sistema.id)
    assert r.status_code == 303
    r = await cena.pedir(sistema.id, "3")
    assert r.status_code == 303, _erros(r.text) or r.text[:300]
    pedido = await cena.um("SELECT nivel, estado FROM pedidos_teste_selo WHERE sistema_id = :s", s=sistema.id)
    assert (pedido.nivel, pedido.estado) == (3, "pedido")
    linha = await cena.um(
        "SELECT frase, dados FROM extrato WHERE sistema_id = :s AND tipo = 'admin_selo_teste_pedido'", s=sistema.id
    )
    assert linha.frase == f"Teste de selo nivel 3 do sistema {sistema.nome} pedido por {NOME_DO_ADMIN}."
    assert linha.dados["nivel"] == 3
    assert re.search(
        r"Teste de nível 3 pedido às [0-9/: ]+; esperando a vez\.", _cartao((await cena.ficha(sistema.id)).text)
    )


async def test_plano_que_passa_do_teto_diz_que_o_teste_nao_comeca(banco_migrado: Parametros) -> None:
    """Critica B5: com o teto de chamadas baixo (``BADGE_TEST_MAX_CALLS``, o minimo de 50), o plano que passa dele diz
    "Passa do teto de 50 chamadas: este teste não começa.", e so ele."""
    async with subir(banco_migrado, BADGE_TEST_MAX_CALLS=50) as cena:
        sistema = await cena.proprio(acoes=("enviar_lembrete", "apagar_lembrete"))
        await _declaracao_e_chave(cena, sistema)
        planos = _planos(_cartao((await cena.ficha(sistema.id)).text))
    assert set(planos) == {2, 3}
    assert any(plano.chamadas > 50 for plano in planos.values())
    for plano in planos.values():
        assert plano.teto_dito == (50 if plano.chamadas > 50 else None)


async def test_pedir_o_teste_confere_a_declaracao_de_novo(cena: Cena) -> None:
    """Uma ferramenta de acao revisada depois da declaracao: o pedido volta com 400 e a ferramenta que falta."""
    sistema = await cena.proprio()
    await _declaracao_e_chave(cena, sistema)
    async with cena.dono.begin() as c:
        await t3.revisar_ferramenta(
            c,
            sistema.id,
            t3.definicao("remarcar_lembrete", anotacoes={"readOnlyHint": False}),
            classe="muda",
        )
    r = await cena.pedir(sistema.id)
    assert r.status_code == 400
    assert "A declaração não cobre a ferramenta remarcar_lembrete." in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s", s=sistema.id) == []


async def test_pedido_que_o_banco_recusa_volta_com_a_frase(cena: Cena) -> None:
    """O sistema saiu de ativo depois da declaracao: a tela confere a declaracao, mas quem recusa o pedido e o banco
    (``pedidos_teste_selo_nasce``), e a tela diz por que, com acento."""
    sistema = await cena.proprio()
    await _declaracao_e_chave(cena, sistema)
    async with cena.dono.begin() as c:
        await c.execute(text("UPDATE sistemas SET situacao = 'quarentena' WHERE id = :s"), {"s": sistema.id})
    r = await cena.pedir(sistema.id)
    assert r.status_code == 409
    assert "O teste do selo só vale para sistema ativo, que fala a versão 2026-07-28 do MCP." in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s", s=sistema.id) == []


# --- 4. Historico e relatorio ------------------------------------------------------------------------------------


def _casos_com_nao_se_aplica(ferramentas: list[uuid.UUID]) -> list[dict[str, Any]]:
    """Os casos de um teste de nivel 2 aprovado, com o C-2.2b ``nao_se_aplica`` (sem segundo efeito) e uma
    tentativa excluida por prazo no C-2.1."""
    itens = apoio.casos_que_aprovam(ferramentas, 2)
    for i in itens:
        if i["caso"] == "C-2.2b":
            i.update(resultado="nao_se_aplica", razao="sem_segundo_efeito", tentativas=0)
        if i["caso"] == "C-2.1":
            i.update(tentativas=2, excluidas=1)
    return itens


async def test_relatorio_por_ferramenta_com_o_que_nao_testamos_e_o_json(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        ferramentas = await apoio.acoes_do_sistema(c, sistema.id)
        teste = await apoio.gravar_teste(
            c, sistema.id, nivel=2, admin_id=cena.admin_id, casos=_casos_com_nao_se_aplica(ferramentas)
        )
    ficha = await cena.ficha(sistema.id)
    historico = _texto(ficha.text.split("Histórico", 1)[1])
    assert "Aprovado: nível 2" in historico
    caminho = f"/telas/admin/sistemas/{sistema.id}/selo/testes/{teste.id}"
    assert f'href="{caminho}">ver os casos' in ficha.text

    r = await cena.nav.get(caminho)
    assert r.status_code == 200
    pagina = _texto(r.text)
    assert "Aprovado: o sistema ficou com o nível 2" in pagina
    for coluna in ("Caso", "O que testamos", "Resultado", "Por quê"):
        assert f"<th>{coluna}</th>" in r.text
    assert admin.ESCOPO_SISTEMA in pagina
    assert "Ferramenta enviar_lembrete" in pagina
    c21 = contrato.CASOS_POR_NOME["C-2.1"]
    assert f"C-2.1 {c21.o_que_testamos} Cumpriu 1 tentativa passou do prazo e ficou de fora" in pagina
    c22b = contrato.CASOS_POR_NOME["C-2.2b"]
    assert f"C-2.2b {c22b.o_que_testamos} Não se aplica nesta ferramenta, repetir não muda nada" in pagina
    assert "C-2.2d" in pagina
    assert "Não testado poucos pares ao mesmo tempo para observar" in pagina
    assert "C-0.1" in pagina.split("Ferramenta enviar_lembrete", 1)[0]  # os de escopo sistema ficam a parte
    assert "C-3.1a" not in pagina  # teste de nivel 2: so os casos que ele gravou
    assert "O que não testamos" in pagina
    assert "Por quanto tempo o sistema guarda os números (declarado pelo sistema: 30 dias)." in pagina
    interposicao = next(n for n in contrato.NAO_TESTADOS if n.item == "interposicao_e_reinicio")
    assert interposicao.texto in pagina
    assert "A declaração usada" in pagina
    assert "Versão 1" in pagina
    assert apoio.ENDERECO_TESTE in pagina  # a declaracao usada, como a equipe escreveu
    assert admin.SELO_RODAPE in pagina
    assert admin.SELO_NAO_GARANTE in pagina
    assert f'href="{caminho}.json"' in r.text
    _sem_promessa_nem_rascunho(pagina)

    r = await cena.nav.get(f"{caminho}.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"] == f'attachment; filename="teste-do-selo-{teste.id}.json"'
    corpo = r.json()
    gravado = await cena.um("SELECT casos, nao_testado FROM testes_selo WHERE id = :t", t=teste.id)
    assert corpo["casos"] == gravado.casos  # so o que o banco guardou
    assert [{"item": n["item"], "razao": n["razao"]} for n in corpo["nao_testado"]] == gravado.nao_testado
    assert (corpo["contrato"], corpo["bateria"]) == (contrato.CONTRATO, contrato.VERSAO_BATERIA)
    assert corpo["teste"]["nivel_pedido"] == 2
    assert corpo["teste"]["resultado"] == "aprovado"
    assert corpo["ferramentas"] == [{"id": str(f), "nome": "enviar_lembrete", "versao": 1} for f in ferramentas]
    assert corpo["declaracao"]["versao"] == 1
    assert corpo["declaracao"]["dados"]["modo"] == "ambiente_de_teste"
    assert corpo["rodape"] == admin.SELO_RODAPE
    _sem_promessa_nem_rascunho(r.text)

    outro = await cena.proprio()
    for sufixo in ("", ".json"):
        r = await cena.nav.get(f"/telas/admin/sistemas/{outro.id}/selo/testes/{teste.id}{sufixo}")
        assert r.status_code == 404  # o teste e de outro sistema


async def test_teste_reprovado_no_cartao_no_historico_e_no_relatorio(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        teste = await apoio.gravar_teste(c, sistema.id, nivel=2, aprovado=False, admin_id=cena.admin_id)
    ficha = await cena.ficha(sistema.id)
    cartao = _cartao(ficha.text)
    assert "Selo agora: nível 1. O último teste não foi aprovado: 1 caso falhou ou ficou sem teste." in cartao
    assert "Não aprovado: nível 1" in cartao
    assert "Retirar o selo" not in cartao
    r = await cena.nav.get(f"/telas/admin/sistemas/{sistema.id}/selo/testes/{teste.id}")
    pagina = _texto(r.text)
    assert "Não aprovado: 1 caso falhou ou ficou sem teste; o sistema ficou no nível 1" in pagina
    assert "C-2.1 " + contrato.CASOS_POR_NOME["C-2.1"].o_que_testamos + " Falhou" in pagina
    _sem_promessa_nem_rascunho(pagina)


async def test_reprovado_sem_nenhum_falhou_conta_igual_no_cartao_e_no_relatorio(cena: Cena) -> None:
    """Decisao 7 da Etapa 3r: sem resposta do sistema, o caso obrigatorio fica ``nao_testado`` (``sem_resposta``) e o
    teste e reprovado sem nenhum ``falhou``. O cartao conta como o relatorio e o extrato (os que falharam e os
    obrigatorios que ficaram sem aprovar) e nunca diz "0 casos falharam"."""
    sistema = await cena.proprio(acoes=("enviar_lembrete", "apagar_lembrete"))
    async with cena.dono.begin() as c:
        ferramentas = await apoio.acoes_do_sistema(c, sistema.id)
        casos = apoio.casos_que_aprovam(ferramentas, 2)
        for i in casos:
            if i["caso"] == "C-2.1":  # um por ferramenta: dois obrigatorios sem resposta
                i.update(resultado="nao_testado", razao="sem_resposta", tentativas=1)
        teste = await apoio.gravar_teste(c, sistema.id, nivel=2, aprovado=False, admin_id=cena.admin_id, casos=casos)
    assert not [i for i in casos if i["resultado"] == "falhou"]
    cartao = _cartao((await cena.ficha(sistema.id)).text)
    assert "Selo agora: nível 1. O último teste não foi aprovado: 2 casos falharam ou ficaram sem teste." in cartao
    assert "0 casos" not in cartao
    pagina = _texto((await cena.nav.get(f"/telas/admin/sistemas/{sistema.id}/selo/testes/{teste.id}")).text)
    assert "Não aprovado: 2 casos falharam ou ficaram sem teste; o sistema ficou no nível 1" in pagina
    assert "C-2.1 " + contrato.CASOS_POR_NOME["C-2.1"].o_que_testamos + " Não testado o sistema não respondeu" in pagina


async def test_relatorio_marca_o_nome_da_ferramenta_e_a_declaracao_usada(cena: Cena) -> None:
    """O nome original da ferramenta (texto do sistema) e a declaracao usada (texto da equipe) vem no relatorio com a
    classe ``estranho`` e os escondidos marcados; nada chega cru."""
    async with cena.dono.begin() as c:
        sistema = await apoio.proprio_com_acoes(c)
        await c.execute(
            text("UPDATE ferramentas_revisadas SET nome_original = :n WHERE sistema_id = :s AND classe = 'muda'"),
            {"n": "enviar\u202elembrete", "s": sistema.id},
        )
        dados = apoio.declaracao(["enviar\u202elembrete"], responsavel="Equipe\u3164 do sistema")
        declaracao_id = await apoio.salvar_declaracao(c, sistema.id, cena.admin_id, dados)
        teste = await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id, declaracao_id=declaracao_id)
    r = await cena.nav.get(f"/telas/admin/sistemas/{sistema.id}/selo/testes/{teste.id}")
    assert r.status_code == 200
    assert "\u202e" not in r.text
    assert "\u3164" not in r.text
    assert '<h2>Ferramenta <code class="estranho">enviar[U+202E]lembrete</code> (versão 1 da revisão)</h2>' in r.text
    usada = r.text.split('id="titulo-declaracao-usada"', 1)[1].split("</section>", 1)[0]
    pre = usada.split('<pre class="estranho dado">', 1)[1].split("</pre>", 1)[0]
    assert "&#34;responsavel&#34;: &#34;Equipe[U+3164] do sistema&#34;" in pre
    assert "&#34;enviar[U+202E]lembrete&#34;" in pre


async def test_pedido_interrompido_aparece_com_o_motivo(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        declaracao_id = await apoio.salvar_declaracao(c, sistema.id, cena.admin_id)
        pedido = await apoio.pedir_teste(c, sistema.id, cena.admin_id, declaracao_id, rodando=False)
        await c.execute(
            text("UPDATE pedidos_teste_selo SET estado = 'interrompido', motivo = 'sem_chave_de_teste' WHERE id = :i"),
            {"i": pedido},
        )
    cartao = _cartao((await cena.ficha(sistema.id)).text)
    assert "foi interrompido: falta a chave de teste do selo. Nada mudou no selo." in cartao
    assert "Interrompido: falta a chave de teste do selo" in cartao


# --- 5. Retirar o selo -------------------------------------------------------------------------------------------


async def test_retirar_o_selo_grava_o_motivo_do_admin(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        teste = await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id)
    motivo = "A equipe do sistema avisou que trocou a forma de guardar os números."

    await cena.envelhecer_login()
    r = await cena.retirar(sistema.id, motivo)
    assert r.status_code == 403
    assert admin.SELO_LOGIN_REFEITO in _erros(r.text)
    async with cena.dono.begin() as c:
        await c.execute(text("UPDATE sessoes_telas SET auth_time = now() WHERE pessoa_id = :p"), {"p": cena.admin_id})
    for errado in ("", "  \n ", "x" * 501):
        r = await cena.retirar(sistema.id, errado)
        assert r.status_code == 400
        assert admin.SELO_MOTIVO_DA_RETIRADA in _erros(r.text)
    assert await cena.todos("SELECT 1 FROM selos_retirados WHERE sistema_id = :s", s=sistema.id) == []

    r = await cena.retirar(sistema.id, "x" * 500)  # a borda de 500 letras passa
    assert r.status_code == 303, r.text
    assert r.headers["location"].endswith("?aviso=selo_retirado")
    retirada = await cena.um(
        "SELECT teste_id, motivo, detalhe, retirado_por, pessoa_id FROM selos_retirados WHERE sistema_id = :s",
        s=sistema.id,
    )
    assert (retirada.teste_id, retirada.motivo, retirada.pessoa_id) == (teste.id, "admin", cena.admin_id)
    assert retirada.detalhe == "x" * 500
    assert retirada.retirado_por == NOME_DO_ADMIN  # o nome do cadastro, gravado pelo banco
    linha = await cena.um(
        "SELECT frase, pessoa_id, dados FROM extrato WHERE sistema_id = :s AND tipo = 'selo_retirado'", s=sistema.id
    )
    assert linha.frase == (
        f"Selo do sistema {sistema.nome} retirado: o ADMIN {NOME_DO_ADMIN} retirou o selo; o sistema volta ao nivel 1."
    )
    assert linha.pessoa_id == cena.admin_id
    assert linha.dados["motivo"] == "admin"
    pagina = await cena.nav.get(r.headers["location"])
    texto = _texto(pagina.text)
    assert "Pronto. O selo foi retirado; o sistema volta ao nível 1." in texto
    cartao = _cartao(pagina.text)
    assert "Selo agora: nível 1. Retirado em" in cartao
    assert ": retirado pelo ADMIN." in cartao
    assert "O que o ADMIN escreveu ao retirar: " + "x" * 500 in cartao
    assert "Por que retirar o selo" not in cartao

    r = await cena.retirar(sistema.id, motivo)  # ja nao ha selo de nivel 2 ou 3
    assert r.status_code == 409
    assert admin.SELO_NADA_A_RETIRAR in _erros(r.text)
    assert len(await cena.todos("SELECT 1 FROM selos_retirados WHERE sistema_id = :s", s=sistema.id)) == 1


async def test_motivo_da_retirada_com_caractere_escondido_vem_marcado(cena: Cena) -> None:
    """O texto do ADMIN na retirada fica gravado como foi escrito, e o cartao o mostra com os escondidos marcados."""
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id)
    motivo = "A equipe pediu\u202e para tirar."
    r = await cena.retirar(sistema.id, motivo)
    assert r.status_code == 303, _erros(r.text) or r.text[:300]
    assert (await cena.um("SELECT detalhe FROM selos_retirados WHERE sistema_id = :s", s=sistema.id)).detalhe == motivo
    secao = (await cena.ficha(sistema.id)).text.split('id="titulo-selo"', 1)[1].split("</section>", 1)[0]
    assert "\u202e" not in secao
    assert "<p>O que o ADMIN escreveu ao retirar: A equipe pediu[U+202E] para tirar.</p>" in secao


async def test_retirada_soma_o_contador_uma_vez(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id)
    antes = _contagem("selo_retiradas", motivo="admin")
    r = await cena.retirar(sistema.id, "Motivo do ADMIN.")
    assert r.status_code == 303
    r = await cena.retirar(sistema.id, "Motivo do ADMIN.")
    assert r.status_code == 409
    assert _contagem("selo_retiradas", motivo="admin") == antes + 1


async def _espera_a_trava(cena: Cena, tarefa: asyncio.Future[Any]) -> bool:
    """Se a tela ficou esperando uma trava consultiva (ate uns 10 segundos, por voltas): True; se a tela respondeu
    antes, False."""
    for _ in range(200):
        if tarefa.done():
            return False
        if (await cena.um("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")).n:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_duas_retiradas_ao_mesmo_tempo_gravam_uma_so(cena: Cena) -> None:
    """A retirada pela tela toma a trava das respostas do sistema (a da 0007, ``informacoes_sistema_nasce``) antes de
    ler o selo. Outra retirada no meio (outra aba do ADMIN, ou a automatica do despachante, que grava a resposta do
    sistema e a retirada na mesma transacao, com a trava) faz a tela esperar; depois, ela ve o selo ja retirado e volta
    com 409. Sem a trava, as duas liam o nivel 2 e gravavam duas retiradas do mesmo teste."""
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        teste = await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id)
    antes = _contagem("selo_retiradas", motivo="admin")
    outra = await cena.dono.connect()
    tarefa: asyncio.Future[Any] | None = None
    try:
        await outra.begin()
        await outra.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtext('mcp_claro.selo.informacoes'),"
                " hashtext(CAST(CAST(:s AS uuid) AS text)))"
            ),
            {"s": sistema.id},
        )
        await outra.execute(
            text(
                "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                " VALUES (:s, :t, 'admin', 'Pela outra aba.', 'x', :p)"
            ),
            {"s": sistema.id, "t": teste.id, "p": cena.admin_id},
        )
        tarefa = asyncio.ensure_future(cena.retirar(sistema.id, "Motivo desta aba."))
        esperou = await _espera_a_trava(cena, tarefa)
        await outra.commit()
        r = await tarefa
    finally:
        if outra.in_transaction():
            await outra.rollback()
        await outra.close()
        if tarefa is not None and not tarefa.done():
            await tarefa
    assert esperou, "a retirada pela tela nao esperou a trava das respostas do sistema"
    assert r.status_code == 409
    assert admin.SELO_NADA_A_RETIRAR in _erros(r.text)
    retiradas = await cena.todos("SELECT detalhe FROM selos_retirados WHERE teste_id = :t", t=teste.id)
    assert [x.detalhe for x in retiradas] == ["Pela outra aba."]
    assert (
        await cena.todos("SELECT 1 FROM extrato WHERE sistema_id = :s AND tipo = 'selo_retirado'", s=sistema.id) == []
    )
    assert _contagem("selo_retiradas", motivo="admin") == antes


def _contagem(nome: str, **rotulos: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    return sum(
        n
        for (_, k, r), n in medidas.items()
        if k == nome and all(json.loads(r).get(chave) == valor for chave, valor in rotulos.items())
    )


# --- 6. A lista de sistemas --------------------------------------------------------------------------------------


async def test_lista_de_sistemas_tem_a_coluna_selo_com_o_vencimento(cena: Cena) -> None:
    sem = await cena.proprio()
    com = await cena.proprio()
    perto = await cena.proprio()
    async with cena.dono.begin() as c:
        await apoio.gravar_teste(c, com.id, nivel=3, admin_id=cena.admin_id)
        # O teste de validade curta: a politica com 7 dias so nesta transacao (a linha sai antes do commit).
        politica = (
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_TTL_DAYS', 7, 't', 'teste')"
                    " RETURNING id"
                )
            )
        ).scalar_one()
        await apoio.gravar_teste(c, perto.id, nivel=2, admin_id=cena.admin_id)
        await c.execute(text("DELETE FROM politica WHERE id = :i"), {"i": politica})
    r = await cena.nav.get("/telas/admin/sistemas")
    assert r.status_code == 200
    assert "<th>Selo</th>" in r.text

    def coluna(nome: str) -> str:
        linha = r.text.split(f">{nome}</a>", 1)[1].split("</tr>", 1)[0]
        return _texto(linha.rsplit("<td>", 1)[1]).strip()

    assert coluna(sem.nome) == "nível 1"
    assert coluna(com.nome) == "nível 3"
    assert coluna(perto.nome) == "nível 2 vence em 7 dias"
    _sem_promessa_nem_rascunho(_texto(r.text))


async def test_lista_le_o_aviso_da_politica(cena: Cena) -> None:
    """O "vence em" da lista vem do aviso da politica vigente (``BADGE_WARN_DAYS``, o mesmo das rotinas), e nao de um
    numero fixo: com o padrao (14 dias), um selo de 60 dias nao avisa; com 89 dias na politica, avisa."""
    s = await cena.proprio()
    async with cena.dono.begin() as c:  # a validade de 60 dias so para este teste gravado (a linha sai antes do commit)
        ttl = (
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_TTL_DAYS', 60, 't', 'teste')"
                    " RETURNING id"
                )
            )
        ).scalar_one()
        await apoio.gravar_teste(c, s.id, nivel=2, admin_id=cena.admin_id)
        await c.execute(text("DELETE FROM politica WHERE id = :i"), {"i": ttl})

    async def coluna() -> str:
        r = await cena.nav.get("/telas/admin/sistemas")
        assert r.status_code == 200
        linha = r.text.split(f">{s.nome}</a>", 1)[1].split("</tr>", 1)[0]
        return _texto(linha.rsplit("<td>", 1)[1]).strip()

    assert await coluna() == "nível 2"
    async with cena.dono.begin() as c:
        aviso = (
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo) VALUES ('BADGE_WARN_DAYS', 89, 't', 'teste')"
                    " RETURNING id"
                )
            )
        ).scalar_one()
    try:
        assert await coluna() == "nível 2 vence em 60 dias"
    finally:
        async with cena.dono.begin() as c:
            await c.execute(text("DELETE FROM politica WHERE id = :i"), {"i": aviso})


# --- 7. A chave de teste -----------------------------------------------------------------------------------------


async def test_chave_de_teste_nunca_troca_a_de_producao(cena: Cena) -> None:
    """Critica M11: num sistema de chave compartilhada, a chave de teste fica ao lado da de producao (``acesso``),
    com o tipo proprio, e a de producao nao muda."""
    async with cena.dono.begin() as c:
        sistema_id = await _proprio_compartilhado(c)
        await chaves.guardar(c, cena.cifra, sistema_id=sistema_id, segredo="chave-de-producao", tipo="acesso")
    antes = await cena.um(
        "SELECT cifrada, atualizada_em FROM chaves_cifradas WHERE sistema_id = :s AND tipo = 'acesso'", s=sistema_id
    )
    r = await cena.entregar_chave_de_teste(sistema_id)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/telas/admin/sistemas/{sistema_id}?aviso=selo_chave_guardada"
    depois = await cena.todos(
        "SELECT tipo, cifrada, atualizada_em, pessoa_id, aplicativo_id FROM chaves_cifradas WHERE sistema_id = :s"
        " ORDER BY tipo",
        s=sistema_id,
    )
    assert [x.tipo for x in depois] == ["acesso", "teste_selo"]
    assert (depois[0].cifrada, depois[0].atualizada_em) == (antes.cifrada, antes.atualizada_em)
    assert (depois[1].pessoa_id, depois[1].aplicativo_id) == (None, None)
    async with cena.dono.connect() as c:
        aberta = await chaves.abrir(c, cena.cifra, sistema_id=sistema_id, tipo=contrato.TIPO_CHAVE_TESTE)
        producao = await chaves.abrir(c, cena.cifra, sistema_id=sistema_id, tipo="acesso")
    assert (aberta, producao) == (CHAVE_DE_TESTE, "chave-de-producao")
    frases = {
        x.tipo: x.frase
        for x in await cena.todos(
            "SELECT tipo, frase FROM extrato WHERE sistema_id = :s"
            " AND tipo IN ('admin_chave_entregue', 'chave_guardada')",
            s=sistema_id,
        )
    }
    assert frases["admin_chave_entregue"].startswith("Chave de teste do selo do sistema ")
    assert frases["chave_guardada"].startswith("Chave de teste do selo guardada cifrada no cofre por ")
    pagina = await cena.nav.get(r.headers["location"])
    assert admin.AVISOS_ADMIN["selo_chave_guardada"] in _texto(pagina.text)
    assert CHAVE_DE_TESTE not in pagina.text


async def test_chave_de_teste_recusada_volta_para_a_ficha(cena: Cena) -> None:
    sistema = await cena.proprio()
    r = await cena.entregar_chave_de_teste(sistema.id, "com espaço no meio")
    assert r.status_code == 400
    assert "Cole o valor inteiro, sem espaços nem quebras de linha" in _erros(r.text)
    assert 'id="titulo-selo"' in r.text  # a recusa volta para o cartao "Selo" da ficha
    assert await cena.todos("SELECT 1 FROM chaves_cifradas WHERE sistema_id = :s", s=sistema.id) == []


# --- 8. Quem nao e ADMIN -----------------------------------------------------------------------------------------


async def test_quem_nao_e_admin_recebe_403_em_todas_as_rotas_do_selo(cena: Cena) -> None:
    sistema = await cena.proprio()
    async with cena.dono.begin() as c:
        teste = await apoio.gravar_teste(c, sistema.id, nivel=2, admin_id=cena.admin_id)
    pessoa = cena.amb.navegador()
    await cena.amb.entrar(pessoa, f"bia-{uuid.uuid4().hex[:8]}", "Bia")
    csrf = token_csrf((await pessoa.get("/telas")).text)
    base = f"/telas/admin/sistemas/{sistema.id}"
    for caminho in (base, f"{base}/selo/testes/{teste.id}", f"{base}/selo/testes/{teste.id}.json"):
        r = await pessoa.get(caminho)
        assert r.status_code == 403, caminho
        assert "Esta área é só do ADMIN" in _texto(r.text)
    for caminho, dados in (
        (
            f"{base}/selo/declaracao",
            {"declaracao": json.dumps(apoio.declaracao(["enviar_lembrete"])), "confirmo": "sim"},
        ),
        (f"{base}/selo/testar", {"nivel": "2"}),
        (f"{base}/selo/retirar", {"motivo": "Quero tirar."}),
        (f"{base}/chave", {"tipo": contrato.TIPO_CHAVE_TESTE, "segredo": CHAVE_DE_TESTE}),
    ):
        r = await pessoa.post(caminho, dados, csrf=csrf)
        assert r.status_code == 403, caminho
    assert await cena.todos("SELECT 1 FROM declaracoes_selo WHERE sistema_id = :s", s=sistema.id) != []  # do apoio
    assert await cena.todos("SELECT 1 FROM selos_retirados WHERE sistema_id = :s", s=sistema.id) == []
    assert (
        await cena.todos("SELECT 1 FROM pedidos_teste_selo WHERE sistema_id = :s AND estado = 'pedido'", s=sistema.id)
        == []
    )
