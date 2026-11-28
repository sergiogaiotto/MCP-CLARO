"""Telas da pessoa da Fase 4 (selos de nivel 2 e 3), de ponta a ponta (G6b, etapa 4): PostgreSQL de verdade, provedor
OIDC falso e as telas com o usuario do banco da entrada (``mcp_claro_entrada``: se faltar GRANT, estes testes falham),
sem despachante (nada sai sozinho).

A parte dos outros grupos e montada pelo banco, como dono, com ``tests/apoio_selos.py``: o teste de selo gravado (como
a bateria, G3b), o envio com numero e as respostas do sistema (como a porta e a consulta, G3a), o aviso do bloqueio
(como a tarefa do despachante, G3a) e as linhas do extrato com o retrato do selo (como a porta, G3a). O caminho inteiro
(porta, duble, consulta e aviso de verdade) e prova do G9.

Provas do desenho 12.1 G6 do lado da pessoa, com as correcoes da critica: o cartao do selo de cada nivel na ficha (com
a frase do nivel 1, a data e a validade, e o aviso de terceiro junto) e o selo de agora quando muda (M3); a ficha do
pedido igual (B12, A3.3) e a escolha do numero novo (M4.2); a "nao confirmada" com "O sistema informou", a hora e a
marca, e o botao que grava a decisao com CSRF e o estado visto (decisao 6); a escada acabada; o aviso ao ADMIN pela
decisao do formulario; as etiquetas das listas; o comprovante com a quarta parte e a dica por linha; o extrato com a
coluna "Selo" da hora do passo, mesmo depois de o selo mudar (M3 e D25). Toda pagina tem acento e nao promete nada.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.applications import Starlette

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.extrato.registro import registrar
from mcp_claro.saida import fila
from mcp_claro.selo import vigente
from mcp_claro.telas import ConfigTelas, acoes, consultas, paginas, rotas_telas
from tests import apoio_selos as apoio
from tests import apoio_terceiros as t3
from tests.apoio import porta_livre, servidor
from tests.idp_oidc_falso import IdpOidcFalso
from tests.integracao.test_telas import PUBLICO, SEGREDO_TELAS, Ambiente, Navegador, engine_do_papel, token_csrf
from tests.unidade.test_paginas import PROMESSA, SEM_ACENTO

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

EMAIL_DO_ADMIN = "admin-g6b@exemplo.teste"
DESCRICAO_DO_APOIO = "Descricao escrita pelo ADMIN."
"""A descricao que ``apoio_terceiros.revisar_ferramenta`` grava por padrao (texto do cadastro, nao da tela)."""
FUSO = ZoneInfo("America/Sao_Paulo")
PROIBIDAS = re.compile(r"certificad|conforme|aprovado no|MWS|write-safety|write safety", re.IGNORECASE)


@dataclass
class Cena:
    amb: Ambiente
    nav: Navegador
    csrf: str
    pessoa_id: uuid.UUID

    @property
    def dono(self) -> AsyncEngine:
        return self.amb.engine

    async def sistema(self, nivel: int = 2) -> tuple[apoio.SistemaDoSelo, dict[str, Any], Any]:
        """Um sistema proprio com a acao ``enviar_lembrete``; com ``nivel`` 2 ou 3, um teste aprovado nesse nivel.
        Devolve o sistema, o retrato do selo de agora e a linha de ``selo_vigente``."""
        async with self.dono.begin() as c:
            s = await apoio.proprio_com_acoes(c)
            if nivel > 1:
                await apoio.gravar_teste(c, s.id, nivel=nivel)
            vig = await apoio.selo_vigente(c, s.id)
        assert vig.nivel == nivel
        return s, apoio.retrato(vig), vig

    async def operacao(
        self,
        ferramenta: t3.Revisada,
        retrato: dict[str, Any] | None,
        *,
        liberada: bool = False,
        repete: apoio.OperacaoDeTeste | None = None,
    ) -> apoio.OperacaoDeTeste:
        """Uma acao da pessoa logada; sem ``liberada``, a ficha espera."""
        async with self.dono.begin() as c:
            return await apoio.criar_operacao(
                c,
                ferramenta,
                pessoa_id=self.pessoa_id,
                aplicativo_id=None if repete is None else repete.aplicativo_id,
                argumentos=None if repete is None else repete.argumentos,
                repete=None if repete is None else repete.id,
                liberada=liberada,
                ficha_selo=retrato,
            )

    async def nao_confirmada(
        self, s: apoio.SistemaDoSelo, retrato: dict[str, Any], *respostas: str | None, fim: str = "nao_confirmada"
    ) -> apoio.OperacaoDeTeste:
        """Uma acao que saiu com numero e terminou em ``fim``, com as respostas a consulta (None: sem resposta)."""
        op = await self.operacao(s.acoes[0], retrato, liberada=True)
        async with self.dono.begin() as c:
            await apoio.enviar(c, op.id, fim)
            for estado in respostas:
                await apoio.informar(c, op.id, estado)
        return op

    async def um(self, sql: str, **valores: Any) -> Any:
        async with self.dono.connect() as c:
            return (await c.execute(text(sql), valores)).first()

    async def pagina(self, caminho: str) -> str:
        r = await self.nav.get(caminho)
        assert r.status_code == 200, r.text
        return r.text


@contextlib.asynccontextmanager
async def subir(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    porta_idp = porta_livre()
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
                # Nenhum despachante escuta: as telas da pessoa nao falam com ele, e nada sai sozinho.
                "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_livre()}",
                "NOTIFY_ADMIN_EMAILS": [EMAIL_DO_ADMIN],
            }
        )
    )
    engine = criar_engine(p, pool_size=3, max_overflow=3)
    engine_entrada = engine_do_papel(p, "mcp_claro_entrada")
    http = httpx2.AsyncClient(follow_redirects=False, timeout=15, trust_env=False)
    despachante = ClienteDespachante(p)
    cfg = ConfigTelas(segredo=SecretStr(SEGREDO_TELAS))
    app = Starlette(routes=rotas_telas(p, engine_entrada, despachante, config=cfg, http_login=http))
    try:
        async with servidor(idp.app(), porta_idp), servidor(app) as url_telas:
            amb = Ambiente(p, idp, url_telas, engine, http, "")
            nav = amb.navegador()
            sujeito = f"g6b-{uuid.uuid4().hex[:8]}"
            await amb.entrar(nav, sujeito, "Ana da Silva")
            pessoa_id = (await amb.pessoa(sujeito)).id
            # Pessoa de teste: com FEATURE_PHASE4_BADGES desligada, o selo efetivo dela e o vigente (D24).
            async with engine.begin() as c:
                await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": pessoa_id})
            csrf = token_csrf((await nav.get("/telas/bloqueio")).text)
            yield Cena(amb, nav, csrf, pessoa_id)
    finally:
        await despachante.fechar()
        await http.aclose()
        await engine_entrada.dispose()
        await engine.dispose()


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    async with subir(banco_migrado) as c:
        yield c


def _texto(html: str) -> str:
    """O texto visivel: as marcas de dentro da linha (``bdi``, ``a``, ``span``, ``strong``) saem sem espaco, as outras
    viram espaco, e os espacos se juntam."""
    sem_tags = re.sub(r"</?(?:bdi|a|span|strong|code|em)\b[^>]*>", "", html)
    sem_tags = re.sub(r"<[^>]+>", " ", sem_tags)
    for de, para in (("&#34;", '"'), ("&#39;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        sem_tags = sem_tags.replace(de, para)
    return re.sub(r"\s+", " ", sem_tags)


def _secao(html: str, marca: str, fim: str = "</section>") -> str:
    assert marca in html, marca
    return html.split(marca, 1)[1].split(fim, 1)[0]


def _garante_so_negado(t: str) -> bool:
    return all(m.start() >= 4 and t[m.start() - 4 : m.start()] == "não " for m in re.finditer("garant", t))


def confere_pagina(html: str, *, acento: bool = True) -> None:
    """A pagina da pessoa: acento, nada prometido ("garante" so na negacao) e nenhum nome proibido perto do selo."""
    visivel = _texto(re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.DOTALL))
    visivel = visivel.replace(DESCRICAO_DO_APOIO, " ")  # o texto do ADMIN no cadastro do apoio, nao da tela
    if acento:
        assert not [m.group(0) for m in SEM_ACENTO.finditer(visivel)], visivel
    assert _garante_so_negado(visivel), visivel
    assert not PROMESSA.search(visivel.replace("não garante", "")), visivel
    assert not PROIBIDAS.search(visivel), visivel


def _dia(momento: Any) -> str:
    return momento.astimezone(FUSO).strftime("%d/%m/%Y")


# --- Ficha: o cartao do selo -------------------------------------------------------------------------------------


@pytest.mark.parametrize("nivel", [1, 2, 3])
async def test_ficha_mostra_o_cartao_de_cada_nivel_com_a_data_e_a_validade(cena: Cena, nivel: int) -> None:
    s, retrato, vig = await cena.sistema(nivel)
    op = await cena.operacao(s.acoes[0], retrato)
    html = await cena.pagina(f"/telas/fichas/{op.ficha_id}")
    cartao = _texto(_secao(html, 'id="selo-do-sistema"'))
    if nivel == 1:
        assert (
            "Selo do sistema: nível 1 (conectado). Este sistema não informa o que faz depois de receber a ação."
            in cartao
        )
        assert "Testado" not in cartao
    else:
        nome = "nível 2 (confere)" if nivel == 2 else "nível 3 (presta contas)"
        assert f"Selo do sistema: {nome}. Testado pela equipe do MCP-CLARO em {_dia(vig.testado_em)}," in cartao
        assert f"no ambiente de teste do sistema; vale até {_dia(vig.vale_ate)}." in cartao
        assert "O selo diz como o sistema respondeu no teste; não garante o que ele fará depois." in cartao
    assert 'id="aviso-terceiro"' not in html
    assert 'id="selo-mudou"' not in html  # o selo de agora e o do retrato
    confere_pagina(html)


async def test_ficha_de_terceiro_mostra_o_aviso_junto_do_selo(cena: Cena) -> None:
    async with cena.dono.begin() as c:
        sistema_id = await t3.cadastrar_terceiro(c, endereco=f"https://t-{uuid.uuid4().hex[:8]}.exemplo.teste/mcp")
        acao = await t3.revisar_ferramenta(
            c,
            sistema_id,
            t3.definicao(
                "enviar_lembrete",
                esquema={"type": "object", "properties": {"para": {"type": "string"}, "texto": {"type": "string"}}},
            ),
            classe="muda",
        )
        retrato = apoio.retrato(await apoio.selo_vigente(c, sistema_id))
    op = await cena.operacao(acao, retrato)
    html = await cena.pagina(f"/telas/fichas/{op.ficha_id}")
    cartao = _secao(html, 'id="selo-do-sistema"')
    assert "Selo do sistema: nível 1 (conectado)." in cartao
    assert (
        '<p class="atencao" id="aviso-terceiro">Este servidor é de terceiros. O MCP-CLARO não controla o que ele faz do'
        " lado de lá.</p>" in cartao
    )


async def test_ficha_que_espera_diz_quando_o_selo_de_agora_e_outro(cena: Cena) -> None:
    """Critica M3: o retrato da ficha e o da hora; se o selo cai antes de a pessoa decidir, a ficha diz o de agora."""
    s, retrato, _ = await cena.sistema(2)
    op = await cena.operacao(s.acoes[0], retrato)
    async with cena.dono.begin() as c:
        await apoio.gravar_teste(c, s.id, nivel=2, aprovado=False)  # o reteste reprovado derruba o selo
        assert (await apoio.selo_vigente(c, s.id)).nivel == 1
    html = await cena.pagina(f"/telas/fichas/{op.ficha_id}")
    assert "Selo do sistema: nível 2 (confere)." in html  # o retrato da hora fica
    nota = _texto(_secao(html, 'id="selo-mudou">', "</p>"))
    assert nota.startswith("O selo deste sistema mudou depois que a ficha foi montada: agora é nível 1.")
    assert "a decisão fica com você" in nota
    # Pessoa real, com a chave da fase desligada: o selo efetivo de agora e o nivel 1 para ela.
    async with cena.dono.begin() as c:
        await c.execute(text("UPDATE pessoas SET teste = false WHERE id = :p"), {"p": cena.pessoa_id})
    s2, retrato2, _ = await cena.sistema(3)
    op2 = await cena.operacao(s2.acoes[0], retrato2)
    html = await cena.pagina(f"/telas/fichas/{op2.ficha_id}")
    assert "agora é nível 1." in _texto(_secao(html, 'id="selo-mudou">', "</p>"))


async def test_ficha_ja_decidida_nao_le_o_selo_de_agora(cena: Cena) -> None:
    """A nota do selo de agora (M3) e so da ficha que espera: a ficha ja decidida mostra o retrato da hora, sem nota,
    mesmo depois de o selo cair."""
    s, retrato, _ = await cena.sistema(2)
    espera = await cena.operacao(s.acoes[0], retrato)
    decidida = await cena.operacao(s.acoes[0], retrato)
    r = await cena.nav.post(f"/telas/fichas/{decidida.ficha_id}/recusar", csrf=cena.csrf)
    assert r.status_code == 303, r.text
    async with cena.dono.begin() as c:
        await apoio.gravar_teste(c, s.id, nivel=2, aprovado=False)  # o reteste reprovado derruba o selo
        assert (await apoio.selo_vigente(c, s.id)).nivel == 1
    html = await cena.pagina(f"/telas/fichas/{espera.ficha_id}")
    assert 'id="selo-mudou"' in html  # a que espera diz o selo de agora
    html = await cena.pagina(f"/telas/fichas/{decidida.ficha_id}")
    assert "Selo do sistema: nível 2 (confere)." in html
    assert 'id="selo-mudou"' not in html
    assert "agora é nível" not in html
    confere_pagina(html)


async def test_selo_que_nao_se_le_nunca_para_a_ficha(cena: Cena, monkeypatch: pytest.MonkeyPatch) -> None:
    """A1 (decisao 3 do coordenador): se o selo de agora nao puder ser lido, a ficha abre do mesmo jeito, sem a nota."""
    s, retrato, _ = await cena.sistema(2)
    op = await cena.operacao(s.acoes[0], retrato)

    async def quebrado(*_: Any, **__: Any) -> Any:
        raise OperationalError("SELECT selo_vigente", None, Exception("o banco caiu"))

    monkeypatch.setattr(vigente, "ler", quebrado)
    html = await cena.pagina(f"/telas/fichas/{op.ficha_id}")
    assert "Selo do sistema: nível 2 (confere)." in html
    assert 'id="selo-mudou"' not in html
    assert "Confirmar</button>" in html


# --- Ficha do pedido igual (B12, A3.3, M4.2) ----------------------------------------------------------------------


async def _original_em_desconhecido(cena: Cena, tentativas: int) -> tuple[apoio.SistemaDoSelo, Any, Any]:
    s, retrato, _ = await cena.sistema(2)
    o = await cena.nao_confirmada(s, retrato, *(["desconhecido"] * tentativas))
    return s, retrato, o


async def test_pedido_igual_com_desconhecido_no_fim_oferece_e_grava_o_numero_novo(cena: Cena) -> None:
    maximo = cena.amb.p.BADGE_LOOKUP_MAX_ATTEMPTS
    s, retrato, o = await _original_em_desconhecido(cena, maximo)
    r = await cena.operacao(s.acoes[0], retrato, repete=o)
    html = await cena.pagina(f"/telas/fichas/{r.ficha_id}")
    igual = _texto(_secao(html, 'id="pedido-igual"'))
    assert f"Este pedido repete o protocolo {o.protocolo}, que ficou sem confirmação." in igual
    assert "O sistema não sabe se o primeiro foi feito; com número novo, ele pode fazer duas vezes." in igual
    assert html.count('name="numero_novo" value="sim"') == 1
    confere_pagina(html)
    resposta = await cena.nav.post(f"/telas/fichas/{r.ficha_id}/confirmar", {"numero_novo": "sim"}, csrf=cena.csrf)
    assert resposta.status_code == 303, resposta.text
    assert resposta.headers["location"].endswith("aviso=confirmada_numero_novo")
    ficha = await cena.um("SELECT decisao, numero_novo FROM fichas WHERE id = :f", f=r.ficha_id)
    assert (ficha.decisao, ficha.numero_novo) == ("confirmada", True)
    linha = await cena.um("SELECT dados FROM extrato WHERE tipo = 'ficha_confirmada' AND protocolo = :p", p=r.protocolo)
    assert linha.dados["numero_novo"] is True
    acao = await cena.pagina(f"/telas/acoes/{r.id}?aviso=confirmada_numero_novo")
    assert paginas.AVISOS["confirmada_numero_novo"] in acao
    assert acoes.ESCOLHEU_NUMERO_NOVO in acao
    # Sem a caixa, a chamada de sempre: vale o mesmo numero.
    r2 = await cena.operacao(s.acoes[0], retrato, repete=o)
    resposta = await cena.nav.post(f"/telas/fichas/{r2.ficha_id}/confirmar", csrf=cena.csrf)
    assert resposta.status_code == 303
    assert resposta.headers["location"].endswith("aviso=confirmada")
    assert (await cena.um("SELECT numero_novo FROM fichas WHERE id = :f", f=r2.ficha_id)).numero_novo is False


async def test_numero_novo_que_nao_cabe_volta_a_ficha_que_espera(cena: Cena) -> None:
    s, retrato, o = await _original_em_desconhecido(cena, 1)  # a escada ainda anda
    r = await cena.operacao(s.acoes[0], retrato, repete=o)
    html = await cena.pagina(f"/telas/fichas/{r.ficha_id}")
    assert 'name="numero_novo"' not in html
    assert acoes.MESMO_NUMERO_PREVISTO in html
    resposta = await cena.nav.post(f"/telas/fichas/{r.ficha_id}/confirmar", {"numero_novo": "sim"}, csrf=cena.csrf)
    assert resposta.status_code == 409
    assert acoes.RECUSAS["numero_novo_nao_cabe"][1] in resposta.text
    assert "Sua decisão" in resposta.text  # a ficha que espera, de novo
    ficha = await cena.um(
        "SELECT fi.decisao, fi.numero_novo, f.estado FROM fichas fi JOIN fila_saida f ON f.ficha_id = fi.id"
        " WHERE fi.id = :f",
        f=r.ficha_id,
    )
    assert (ficha.decisao, ficha.numero_novo, ficha.estado) == (None, False, "aguardando_ficha")


async def test_pedido_igual_com_o_numero_queimado_avisa_que_e_um_pedido_novo(cena: Cena) -> None:
    s, retrato, _ = await cena.sistema(2)
    o = await cena.nao_confirmada(s, retrato, "recusada")
    r = await cena.operacao(s.acoes[0], retrato, repete=o)
    igual = _texto(_secao(await cena.pagina(f"/telas/fichas/{r.ficha_id}"), 'id="pedido-igual"'))
    assert (
        f"O sistema informou sobre o protocolo {o.protocolo}: o sistema recusou o pedido; nada ficou feito. Por isso"
        " este pedido vai com número novo: para o sistema, é um pedido novo." in igual
    )
    assert "Se o sistema estiver errado, este pedido faz de novo." in igual


# --- A "nao confirmada": o que o sistema informou e o botao -------------------------------------------------------


async def test_nao_confirmada_mostra_o_que_o_sistema_informou_e_o_botao_grava_com_csrf(cena: Cena) -> None:
    s, retrato, _ = await cena.sistema(2)
    op = await cena.nao_confirmada(s, retrato, "concluida")
    gravado = (await cena.um("SELECT gravado_em FROM informacoes_sistema WHERE operacao_id = :o", o=op.id)).gravado_em
    html = await cena.pagina(f"/telas/acoes/{op.id}")
    bloco = _secao(html, 'id="o-que-o-sistema-informou"', "</div>")
    hora = gravado.astimezone(FUSO).strftime("%H:%M")
    assert f"O sistema <bdi>{s.nome}</bdi> informou às {hora}" in bloco
    assert ": o pedido foi feito." in bloco
    assert "Isto é o que o sistema diz; o MCP-CLARO não confere do lado de lá." in bloco
    assert acoes.VALE_A_SUA in bloco
    formulario = _secao(html, 'id="registrar-o-que-o-sistema-informou"', "</form>")
    assert 'name="estado_visto" value="concluida"' in formulario
    confere_pagina(html)
    # Sem CSRF, nada.
    sem = await cena.nav.post(f"/telas/acoes/{op.id}/registrar_informado", {"estado_visto": "concluida"})
    assert sem.status_code == 403
    assert await cena.um("SELECT 1 FROM decisoes_nao_confirmada WHERE operacao_id = :o", o=op.id) is None
    # Com CSRF e o estado mostrado: a decisao da pessoa, de acordo com o que o sistema informou.
    r = await cena.nav.post(f"/telas/acoes/{op.id}/registrar_informado", {"estado_visto": "concluida"}, csrf=cena.csrf)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/telas/acoes/{op.id}?aviso=informado_registrado"
    decisao = await cena.um("SELECT decisao, observacao FROM decisoes_nao_confirmada WHERE operacao_id = :o", o=op.id)
    assert (decisao.decisao, decisao.observacao) == ("aconteceu", fila.OBSERVACAO_DO_SISTEMA)
    depois = await cena.pagina(r.headers["location"])
    assert paginas.AVISOS["informado_registrado"] in depois
    assert "Você registrou: <strong>Aconteceu no sistema</strong>." in depois
    assert "registrar-o-que-o-sistema-informou" not in depois
    assert acoes.VALE_A_SUA not in depois  # a decisao e uma so: nada de convite a registrar de novo


async def test_botao_recusa_quando_o_que_o_sistema_informou_mudou(cena: Cena) -> None:
    s, retrato, _ = await cena.sistema(2)
    op = await cena.nao_confirmada(s, retrato, "recusada")
    r = await cena.nav.post(f"/telas/acoes/{op.id}/registrar_informado", {"estado_visto": "concluida"}, csrf=cena.csrf)
    assert r.status_code == 409
    assert acoes.RECUSAS["informado_mudou"][1] in r.text
    assert await cena.um("SELECT 1 FROM decisoes_nao_confirmada WHERE operacao_id = :o", o=op.id) is None


async def test_nao_recebida_sozinha_continua_perguntando_sem_o_botao(cena: Cena) -> None:
    s, retrato, _ = await cena.sistema(2)
    op = await cena.nao_confirmada(s, retrato, "nao_recebida")
    html = await cena.pagina(f"/telas/acoes/{op.id}")
    bloco = _texto(_secao(html, 'id="o-que-o-sistema-informou"', "</div>"))
    assert "o sistema não recebeu o pedido e não vai aceitá-lo depois." in bloco
    assert acoes.UMA_SO_NAO_BASTA in bloco
    assert "registrar-o-que-o-sistema-informou" not in html
    r = await cena.nav.post(
        f"/telas/acoes/{op.id}/registrar_informado", {"estado_visto": "nao_recebida"}, csrf=cena.csrf
    )
    assert r.status_code == 409
    assert acoes.RECUSAS["sem_resposta_final"][1] in r.text


async def test_escada_acabada_diz_que_a_decisao_e_da_pessoa(cena: Cena) -> None:
    maximo = cena.amb.p.BADGE_LOOKUP_MAX_ATTEMPTS
    s, retrato, _ = await cena.sistema(2)
    em_aberto = await cena.nao_confirmada(s, retrato, *(["desconhecido"] * maximo))
    bloco = _texto(_secao(await cena.pagina(f"/telas/acoes/{em_aberto.id}"), 'id="o-que-o-sistema-informou"', "</div>"))
    assert "o sistema não sabe se o pedido foi feito." in bloco
    assert bloco.strip().endswith(acoes.NAO_CONFIRMOU)
    assert acoes.CONTINUAMOS not in bloco
    mudo = await cena.nao_confirmada(s, retrato, *([None] * maximo))
    bloco = _texto(_secao(await cena.pagina(f"/telas/acoes/{mudo.id}"), 'id="o-que-o-sistema-informou"', "</div>"))
    assert f"O sistema {s.nome} não respondeu à consulta. Decida você, como num sistema de nível 1." in bloco
    # Depois da decisao (que e uma so), nada convida a decidir de novo.
    for op in (em_aberto, mudo):
        r = await cena.nav.post(f"/telas/acoes/{op.id}/decidir", {"decisao": "nao_sei"}, csrf=cena.csrf)
        assert r.status_code == 303, r.text
    depois = await cena.pagina(f"/telas/acoes/{mudo.id}")
    bloco = _texto(_secao(depois, 'id="o-que-o-sistema-informou"', "</div>"))
    assert bloco.strip().endswith(f"O sistema {s.nome} não respondeu à consulta. {acoes.VALE_A_DECISAO}")
    assert "Decida" not in bloco
    assert 'name="decisao"' not in depois
    bloco = _texto(_secao(await cena.pagina(f"/telas/acoes/{em_aberto.id}"), 'id="o-que-o-sistema-informou"', "</div>"))
    assert bloco.strip().endswith(acoes.NAO_CONFIRMOU_DEPOIS)
    assert "a decisão é sua" not in bloco
    perguntando = await cena.nao_confirmada(s, retrato)
    bloco = _texto(
        _secao(await cena.pagina(f"/telas/acoes/{perguntando.id}"), 'id="o-que-o-sistema-informou"', "</div>")
    )
    assert "O que o sistema informou Estamos confirmando com o sistema." in bloco


async def test_decisao_pelo_formulario_contra_o_sistema_avisa_o_admin_por_email(cena: Cena) -> None:
    s, retrato, _ = await cena.sistema(2)
    op = await cena.nao_confirmada(s, retrato, "concluida")
    r = await cena.nav.post(f"/telas/acoes/{op.id}/decidir", {"decisao": "nao_aconteceu"}, csrf=cena.csrf)
    assert r.status_code == 303, r.text
    linha = await cena.um("SELECT 1 FROM extrato WHERE tipo = 'sistema_contradito' AND protocolo = :p", p=op.protocolo)
    assert linha is not None
    email = await cena.um(
        "SELECT destino FROM avisos WHERE canal = 'email' AND evento = 'sistema_contradito' AND corpo::text LIKE :p",
        p=f"%{op.protocolo}%",
    )
    assert email is not None
    assert email.destino == EMAIL_DO_ADMIN


# --- Listas ------------------------------------------------------------------------------------------------------


async def test_lista_e_pendencias_levam_a_etiqueta_do_que_o_sistema_informou(cena: Cena) -> None:
    s, retrato, _ = await cena.sistema(2)
    op = await cena.nao_confirmada(s, retrato, "concluida")
    etiqueta = '<span class="etiqueta">O sistema informou: o pedido foi feito</span>'
    lista = await cena.pagina(f"/telas/acoes?protocolo={op.protocolo}")
    assert "Falta a sua decisão" in lista
    assert etiqueta in lista
    pendencias = await cena.pagina("/telas/pendencias")
    item = next(li for li in re.findall(r"<li>.*?</li>", pendencias, re.DOTALL) if op.protocolo in li)
    assert "Falta a sua decisão." in item
    assert etiqueta in item
    confere_pagina(lista)
    confere_pagina(pendencias)


# --- Comprovante: a quarta parte e a dica por linha ---------------------------------------------------------------


async def _avisar(
    cena: Cena, bloqueio_id: uuid.UUID, sistema_id: uuid.UUID, respostas: dict[uuid.UUID, str | None]
) -> None:
    """O que a tarefa do despachante (G3a) faria: o aviso do bloqueio com os numeros e, para quem respondeu, a
    resposta de origem ``aviso`` e o aviso respondido. Sem resposta nenhuma, o aviso fica ``pendente``."""
    async with cena.dono.begin() as c:
        aviso = (
            await c.execute(
                text(
                    "INSERT INTO avisos_bloqueio (bloqueio_id, sistema_id, pessoa_id, operacoes)"
                    " VALUES (:b, :s, :p, CAST(:o AS uuid[])) RETURNING id"
                ),
                {"b": bloqueio_id, "s": sistema_id, "p": cena.pessoa_id, "o": list(respostas)},
            )
        ).scalar_one()
        if not any(respostas.values()):
            return
        await c.execute(
            text("UPDATE avisos_bloqueio SET tentativas = 1, proxima_em = now() + interval '1 minute' WHERE id = :a"),
            {"a": aviso},
        )
    for operacao_id, estado in respostas.items():
        async with cena.dono.begin() as c:
            await apoio.informar(c, operacao_id, estado, origem="aviso", aviso_id=aviso)
    async with cena.dono.begin() as c:
        await c.execute(text("UPDATE avisos_bloqueio SET estado = 'respondido' WHERE id = :a"), {"a": aviso})


async def test_comprovante_com_a_quarta_parte_marcada_e_a_dica_por_linha(cena: Cena) -> None:
    s3, retrato3, _ = await cena.sistema(3)
    outro, retrato_outro, _ = await cena.sistema(3)
    s1, retrato1, _ = await cena.sistema(1)
    enviada = await cena.nao_confirmada(s3, retrato3, fim="enviada")
    async with cena.dono.begin() as c:
        await apoio.informar(c, enviada.id, "concluida", origem="recibo")
    duvida = await cena.nao_confirmada(s3, retrato3)
    esperando = await cena.nao_confirmada(outro, retrato_outro)
    nivel1 = await cena.operacao(s1.acoes[0], retrato1, liberada=True)
    async with cena.dono.begin() as c:
        await c.execute(
            text("UPDATE fila_saida SET estado = 'enviando', mudou_por = 'despachante' WHERE operacao_id = :o"),
            {"o": nivel1.id},
        )
        await c.execute(
            text("UPDATE fila_saida SET estado = 'enviada', enviada_em = now() WHERE operacao_id = :o"),
            {"o": nivel1.id},
        )
    r = await cena.nav.post("/telas/bloqueio", {"aplicativo": "todos"}, csrf=cena.csrf)
    assert r.status_code == 303, r.text
    caminho = r.headers["location"].split("?", 1)[0]
    bloqueio_id = uuid.UUID(caminho.rsplit("/", 1)[1])
    await _avisar(cena, bloqueio_id, s3.id, {duvida.id: "parada"})
    await _avisar(cena, bloqueio_id, outro.id, {esperando.id: None})
    html = await cena.pagina(caminho)
    # A dica por linha, pelo nivel com que cada acao saiu.
    seguiu = _secao(html, 'id="ja-seguiu"')
    linha_3 = next(li for li in re.findall(r"<li>.*?</li>", seguiu) if enviada.protocolo in li)
    linha_1 = next(li for li in re.findall(r"<li>.*?</li>", seguiu) if nivel1.protocolo in li)
    assert "Neste sistema (nível 3), o que ele informar sobre esta ação aparece na parte 4." in linha_3
    assert "Neste sistema (nível 1), “seguiu” quer dizer entregue ao sistema, não concluído por ele." in linha_1
    # A quarta parte: o que o sistema informou, com a hora do gateway e a marca.
    quarta = _texto(_secao(html, 'id="o-que-o-sistema-informou"'))
    assert "4. O que o sistema informou (nível 3)" in quarta
    assert f"Protocolo {enviada.protocolo}, sistema {s3.nome}: o pedido foi feito, às" in quarta
    assert "(na resposta da própria ação). Informado pelo sistema" in quarta
    assert (
        f"Protocolo {duvida.protocolo}, sistema {s3.nome}: o pedido foi parado antes de terminar, pelo bloqueio"
        in quarta
    )
    assert "(em resposta ao aviso do bloqueio). Informado pelo sistema" in quarta
    assert f"Avisamos o sistema {s3.nome} do bloqueio, e ele respondeu." in quarta
    assert f"Sistema {outro.nome}: o aviso ainda não saiu." in quarta
    assert "não respondeu ao aviso" not in quarta  # o aviso pendente nunca culpa o sistema
    confere_pagina(html)
    # A linha do comprovante nao muda: o que o sistema informou vem da leitura de agora.
    linha = await cena.um("SELECT informado FROM comprovantes WHERE bloqueio_id = :b", b=bloqueio_id)
    assert linha.informado == []
    # A resposta ao aviso, na acao, e o que o sistema disse, nunca final e sem o botao.
    acao = await cena.pagina(f"/telas/acoes/{duvida.id}")
    bloco = _texto(_secao(acao, 'id="o-que-o-sistema-informou"', "</div>"))
    assert "o pedido foi parado antes de terminar, pelo bloqueio." in bloco
    assert acoes.DO_AVISO in bloco
    assert "registrar-o-que-o-sistema-informou" not in acao


async def test_comprovante_com_um_sistema_de_nivel_2_nao_o_poe_sob_o_nivel_3(cena: Cena) -> None:
    """A leitura do comprovante traz tambem a ultima resposta a consulta de uma acao de nivel 2 em conferencia: a
    quarta parte nao a poe sob o titulo "(nivel 3)", e cada linha diz o nivel com que a acao saiu (envios_selo)."""
    s2, retrato2, _ = await cena.sistema(2)
    s3, retrato3, _ = await cena.sistema(3)
    conferindo = await cena.nao_confirmada(s2, retrato2, "concluida")
    enviada = await cena.nao_confirmada(s3, retrato3, fim="enviada")
    async with cena.dono.begin() as c:
        await apoio.informar(c, enviada.id, "concluida", origem="recibo")
    nivel = await cena.um("SELECT nivel FROM envios_selo WHERE operacao_id = :o", o=conferindo.id)
    assert nivel.nivel == 2
    r = await cena.nav.post("/telas/bloqueio", {"aplicativo": "todos"}, csrf=cena.csrf)
    assert r.status_code == 303, r.text
    html = await cena.pagina(r.headers["location"].split("?", 1)[0])
    quarta = _secao(html, 'id="o-que-o-sistema-informou"')
    assert '<h3 id="titulo-informou">4. O que o sistema informou</h3>' in quarta
    assert "(nível 3)</h3>" not in quarta
    texto = _texto(quarta)
    assert "Esta parte é o que cada sistema de nível 2 ou 3 diz sobre o que já tinha recebido." in texto
    assert f"Protocolo {conferindo.protocolo}, sistema {s2.nome} (nível 2): o pedido foi feito, às" in texto
    assert "(em resposta à consulta do MCP-CLARO). Informado pelo sistema" in texto
    assert f"Protocolo {enviada.protocolo}, sistema {s3.nome} (nível 3): o pedido foi feito, às" in texto
    confere_pagina(html)


# --- Extrato: a coluna "Selo" da hora do passo ---------------------------------------------------------------------


async def _linha_do_extrato(
    c: AsyncConnection, cena: Cena, sistema_id: uuid.UUID, protocolo: str, tipo: str, dados: dict[str, Any]
) -> None:
    campos: dict[str, object] = {"protocolo": protocolo}
    if tipo == "acao_enviada":
        campos["ferramenta"] = "enviar_lembrete"
    else:
        campos |= {"sistema": "sistema", "estado": "concluida"}
    await registrar(
        c,
        papel="despachante",
        tipo=tipo,
        campos=campos,
        protocolo=protocolo,
        pessoa_id=cena.pessoa_id,
        sistema_id=sistema_id,
        dados=dados,
    )


def _linhas(html: str) -> dict[str, str]:
    """Cada linha da tabela do extrato pelo protocolo (o texto da linha)."""
    achadas: dict[str, str] = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL):
        protocolo = re.search(r"Protocolo (CLR-G6B-[0-9a-f]+)\.", tr)
        if protocolo:
            achadas[protocolo.group(1)] = tr
    return achadas


def _ultima_celula(tr: str) -> str:
    return _texto(re.findall(r"<td>(.*?)</td>", tr, re.DOTALL)[-1]).strip()


async def test_extrato_mostra_o_selo_da_hora_do_passo_mesmo_depois_de_mudar(cena: Cena) -> None:
    s, retrato, vig = await cena.sistema(2)
    _, retrato_alheio, _ = await cena.sistema(3)  # um teste aprovado, mas de outro sistema
    chaves = ("hora", "forjado", "alheio", "torto", "texto", "informou", "sem")
    p = {k: f"CLR-G6B-{uuid.uuid4().hex[:8]}" for k in chaves}
    async with cena.dono.begin() as c:
        reprovado = await apoio.gravar_teste(c, s.id, nivel=3, aprovado=False)  # o selo cai para o nivel 1
        assert (await apoio.selo_vigente(c, s.id)).nivel == 1
        await _linha_do_extrato(c, cena, s.id, p["hora"], "acao_enviada", {"selo": retrato})
        forjado = dict(retrato, nivel=3, teste_id=str(reprovado.id))
        await _linha_do_extrato(c, cena, s.id, p["forjado"], "acao_enviada", {"selo": forjado})
        await _linha_do_extrato(c, cena, s.id, p["alheio"], "acao_enviada", {"selo": retrato_alheio})
        await _linha_do_extrato(c, cena, s.id, p["torto"], "acao_enviada", {"selo": dict(retrato, teste_id="x')")})
        await _linha_do_extrato(c, cena, s.id, p["texto"], "acao_enviada", {"selo": dict(retrato, nivel="2")})
        await _linha_do_extrato(c, cena, s.id, p["informou"], "sistema_informou", {"origem": "consulta"})
        await _linha_do_extrato(c, cena, s.id, p["sem"], "acao_enviada", {})
    html = await cena.pagina("/telas/extrato")
    assert "<th>Selo</th>" in html
    linhas = _linhas(html)
    # O retrato da propria linha, nunca o selo de agora (que caiu para o nivel 1).
    assert _ultima_celula(linhas[p["hora"]]) == f"nível 2 (teste de {_dia(vig.testado_em)})"
    # Nivel 2 ou 3 so com o teste aprovado do retrato (M3): o forjado e o torto ficam no nivel 1.
    assert _ultima_celula(linhas[p["forjado"]]) == "nível 1"
    assert _ultima_celula(linhas[p["alheio"]]) == "nível 1"
    assert _ultima_celula(linhas[p["torto"]]) == "nível 1"
    assert _ultima_celula(linhas[p["texto"]]) == ""
    assert _ultima_celula(linhas[p["sem"]]) == ""
    assert '<span class="etiqueta">Informado pelo sistema</span>' in linhas[p["informou"]]
    assert "Informado pelo sistema" not in linhas[p["hora"]]
    confere_pagina(html, acento=False)  # as frases do extrato sao sem acento, como sempre


async def test_extrato_de_outra_pessoa_aberto_pelo_admin_usa_a_mesma_coluna(cena: Cena) -> None:
    """A tela do ADMIN que abre o extrato de outra pessoa (``admin.extrato_alheio``, com o papel da entrada) mostra a
    mesma coluna "Selo", do retrato da propria linha, e a etiqueta "Informado pelo sistema"."""
    s, retrato, vig = await cena.sistema(3)
    p = {k: f"CLR-G6B-{uuid.uuid4().hex[:8]}" for k in ("hora", "forjado", "informou")}
    async with cena.dono.begin() as c:
        await _linha_do_extrato(c, cena, s.id, p["hora"], "acao_enviada", {"selo": retrato})
        forjado = dict(retrato, teste_id=str(uuid.uuid4()))  # um teste que nao existe: nada acima do nivel 1
        await _linha_do_extrato(c, cena, s.id, p["forjado"], "acao_enviada", {"selo": forjado})
        await _linha_do_extrato(c, cena, s.id, p["informou"], "sistema_informou", {"origem": "consulta"})
    admin = cena.amb.navegador()
    sujeito = f"admin-g6b-{uuid.uuid4().hex[:8]}"
    await cena.amb.entrar(admin, sujeito, "Carla Admin")
    await cena.amb.tornar_admin(sujeito)
    csrf = token_csrf((await admin.get("/telas/admin/extrato")).text)
    motivo = "A pessoa pediu ajuda no chamado CH-77."
    r = await admin.post("/telas/admin/extrato", {"pessoa": str(cena.pessoa_id), "motivo": motivo}, csrf=csrf)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/telas/admin/extrato/{cena.pessoa_id}"
    r = await admin.get(r.headers["location"])
    assert r.status_code == 200, r.text
    html = r.text
    assert "Extrato de Ana da Silva" in html
    assert "<th>Selo</th>" in html
    linhas = _linhas(html)
    assert _ultima_celula(linhas[p["hora"]]) == f"nível 3 (teste de {_dia(vig.testado_em)})"
    assert _ultima_celula(linhas[p["forjado"]]) == "nível 1"
    assert '<span class="etiqueta">Informado pelo sistema</span>' in linhas[p["informou"]]
    confere_pagina(html, acento=False)  # as frases do extrato sao sem acento, como sempre
    # A consulta de baixo e a mesma da tela da pessoa.
    async with cena.dono.connect() as c:
        achada = next(x for x in (await consultas.linhas_extrato(c, cena.pessoa_id, 1))[0] if x.protocolo == p["hora"])
    assert (achada.selo_nivel, achada.selo_teste_nivel) == (3, 3)
    assert achada.selo_testado_em == vig.testado_em
