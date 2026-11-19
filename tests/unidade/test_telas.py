"""Partes das telas que nao precisam de banco: protecoes, PKCE, frases, escape e o htmx servido por nos."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from pydantic import SecretStr

from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import FRASES, frase
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.paginas import PASTA_ESTATICOS, PASTA_MODELOS, criar_ambiente
from mcp_claro.telas.seguranca import CSP, Formulario, caminho_seguro, iguais
from mcp_claro.telas.sessoes import desafio_pkce, hash_sessao, novo_segredo

FRASES_DAS_TELAS = {
    "telas_entrada": {},
    "telas_saida": {},
    "ordem_concedida": {"modelo": "M", "aplicativo": "A", "fim": "01/01/2027"},
    "ordem_revogada": {"modelo": "M", "aplicativo": "A"},
    "extrato_alheio_aberto": {"quem": "Carla", "motivo": "chamado CH-1"},
    "admin_aplicativo_habilitado": {"aplicativo": "A", "quem": "Carla"},
    "admin_sistema_cadastrado": {"sistema": "S", "quem": "Carla"},
    "admin_sondagem_pedida": {"sistema": "S", "quem": "Carla", "desfecho": "aceito"},
    "admin_declaracoes_lidas": {"sistema": "S", "quem": "Carla", "quantas": 3},
    "admin_ferramenta_revisada": {"ferramenta": "f", "sistema": "S", "quem": "Carla", "classe": "le", "risco": "baixo"},
    "admin_modelo_criado": {"modelo": "M", "quem": "Carla", "quantas": 2},
    "admin_programa_cadastrado": {"aplicativo": "P", "quem": "Carla", "responsavel": "Rui", "substituto": "Sara"},
    "admin_webhook_cadastrado": {"destino": "D", "quem": "Carla"},
    "admin_webhook_mudou": {"destino": "D", "situacao": "desligado", "quem": "Carla"},
    "admin_bloqueio_ligado": {"quem": "Carla", "alcance": "um aplicativo"},
    "admin_bloqueio_encerrado": {"quem": "Carla", "alcance": "um sistema"},
    "admin_pausa_ligada": {"quem": "Carla", "alcance": "uma pessoa"},
    "admin_pausa_encerrada": {"quem": "Carla", "alcance": "todo o MCP-CLARO"},
    "admin_entrada_mudou": {"nivel": "fechada", "quem": "Carla"},
    "admin_saida_mudou": {"situacao": "aberta", "quem": "Carla"},
    "pessoa_desativada": {"quem": "Carla"},
    "admin_autorizacao_configurada": {"sistema": "S", "quem": "Carla"},
    "admin_segredo_cliente_entregue": {"sistema": "S", "quem": "Carla"},
    "admin_chave_entregue": {"dono": "compartilhada", "sistema": "S", "quem": "Carla"},
}


@pytest.mark.parametrize(("tipo", "campos"), FRASES_DAS_TELAS.items())
def test_frases_das_telas(tipo: str, campos: dict[str, object]) -> None:
    texto = frase(tipo, **campos)
    assert texto.endswith(".")
    assert "token" not in texto.lower()
    assert "senha" not in texto.lower()
    assert tipo in FRASES


def test_frase_do_extrato_alheio_mostra_quem_e_o_motivo() -> None:
    assert frase("extrato_alheio_aberto", quem="Carla", motivo="chamado CH-1") == (
        "O ADMIN Carla abriu este extrato. Motivo: chamado CH-1."
    )


def test_pkce_s256_sem_preenchimento() -> None:
    verificador = "a" * 43
    esperado = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode("ascii")).digest()).decode().rstrip("=")
    desafio = desafio_pkce(verificador)
    assert desafio == esperado
    assert len(desafio) == 43
    assert "=" not in desafio
    assert re.fullmatch(r"[A-Za-z0-9_-]+", desafio)


def test_segredos_e_hash_da_sessao() -> None:
    a, b = novo_segredo(), novo_segredo()
    assert a != b
    assert len(a) >= 43
    verificador = novo_segredo(64)
    assert 43 <= len(verificador) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_-]+", verificador)
    assert hash_sessao(a) == hashlib.sha256(a.encode()).digest()


def test_comparacao_em_tempo_constante_nao_aceita_vazio() -> None:
    assert iguais("abc", "abc")
    assert not iguais("abc", "abd")
    assert not iguais("", "")
    assert not iguais(None, "abc")


@pytest.mark.parametrize(
    ("pedido", "esperado"),
    [
        ("/telas/ordens", "/telas/ordens"),
        ("/telas/extrato?pagina=2", "/telas/extrato?pagina=2"),
        ("/telas", "/telas"),
        ("https://ataque.exemplo/telas", "/telas"),
        ("//ataque.exemplo/telas", "/telas"),
        ("/telasfalsas", "/telas"),
        ("/telas/../mcp", "/telas"),
        ("/telas/%2e%2e/mcp", "/telas"),
        ("/telas\\@ataque", "/telas"),
        ("/telas/login/volta?code=x", "/telas"),
        ("/mcp", "/telas"),
        (None, "/telas"),
    ],
)
def test_voltar_so_para_as_proprias_telas(pedido: str | None, esperado: str) -> None:
    assert caminho_seguro(pedido) == esperado


def test_formulario_varios_valores() -> None:
    f = Formulario([("a", " 1 "), ("b", "x"), ("b", "y"), ("b", " "), ("csrf", "t")])
    assert f.texto("a") == "1"
    assert f.lista("b") == ["x", "y"]
    assert f.texto("nao", "padrao") == "padrao"
    assert "csrf" not in f.como_dict()


def test_csp_restrita() -> None:
    diretivas = dict(d.strip().split(" ", 1) for d in CSP.split(";"))
    assert diretivas["default-src"] == "'none'"
    assert diretivas["script-src"] == "'self'"
    assert diretivas["style-src"] == "'self'"
    assert diretivas["frame-ancestors"] == "'none'"
    assert "unsafe" not in CSP


def test_jinja_escapa_tudo() -> None:
    ambiente = criar_ambiente("America/Sao_Paulo")
    html = ambiente.get_template("erro.html").render(
        sessao=None,
        csrf="",
        aviso=None,
        caminho="/telas",
        fase1_ligada=False,
        erros=["<b>x</b>"],
        valores={},
        titulo="<script>alert(1)</script>",
        mensagem='"><img src=x onerror=alert(1)>',
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_modelos_sem_script_nem_estilo_embutido() -> None:
    for modelo in PASTA_MODELOS.rglob("*.html"):
        texto = modelo.read_text(encoding="utf-8")
        assert " style=" not in texto, modelo
        assert "<style" not in texto, modelo
        assert not re.search(r"\son[a-z]+\s*=", texto), modelo  # nada de onclick e parecidos
        assert "|safe" not in texto.replace(" ", ""), modelo
        assert "autoescape false" not in texto, modelo
        for src in re.findall(r"<script[^>]*src=\"([^\"]+)\"", texto):
            assert src.startswith("/telas/estaticos/"), modelo
        assert "http://" not in texto, modelo
        assert "cdn" not in texto.lower(), modelo


def test_htmx_servido_por_nos_com_origem_e_licenca() -> None:
    bruto = (PASTA_ESTATICOS / "htmx.min.js").read_bytes()
    fim_comentario = bruto.index(b"*/\n") + 3
    cabecalho = bruto[:fim_comentario].decode()
    assert "htmx.org@2.0.11" in cabecalho
    assert "0BSD" in cabecalho
    registrado = re.search(r"SHA-256 do dist/htmx.min.js original: ([0-9a-f]{64})", cabecalho)
    assert registrado
    assert hashlib.sha256(bruto[fim_comentario:]).hexdigest() == registrado.group(1)


def test_config_recusa_combinacoes_invalidas() -> None:
    teste = Parametros(APP_ENV="teste")
    assert ConfigTelas().problemas(teste) == []
    assert any("SCREENS_SESSION_IDLE_MINUTES" in e for e in ConfigTelas(parada_minutos=600).problemas(teste))
    assert any("SCREENS_TIMEZONE" in e for e in ConfigTelas(fuso="Lugar/Nenhum").problemas(teste))
    piloto = Parametros(APP_ENV="piloto", PUBLIC_BASE_URL="https://mcp.claro.exemplo")
    assert any("SCREENS_CLIENT_SECRET_FILE" in e for e in ConfigTelas().problemas(piloto))
    assert ConfigTelas(segredo=SecretStr("s")).problemas(piloto) == []


def test_config_le_parametros_quando_existirem(tmp_path: object) -> None:
    p = Parametros(APP_ENV="teste", IDP_KIND="keycloak", IDP_ISSUER_URL="https://login.exemplo/realms/r")
    cfg = ConfigTelas.de_parametros(p)
    assert cfg.client_id == "mcp-claro-telas"
    assert cfg.segredo is None
    assert cfg.enderecos_por_tipo(p) == (
        "https://login.exemplo/realms/r/protocol/openid-connect/auth",
        "https://login.exemplo/realms/r/protocol/openid-connect/token",
    )
    assert cfg.redirect_uri(p) == "https://mcp-claro.localhost/telas/login/volta"
