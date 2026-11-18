"""Cliente fino do cofre sem Docker: um OpenBao de mentira no transporte do httpx2.

O ``respx`` nao intercepta o ``httpx2``; o cliente aceita um ``transporte`` para os testes.
Os casos com OpenBao de verdade ficam em ``tests/integracao/test_cofre.py``.
"""

from __future__ import annotations

import base64
import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from structlog.testing import capture_logs

from mcp_claro.cofre import (
    AcessoNegado,
    ChaveEstadoInvalida,
    CifradoInvalido,
    Cofre,
    CofreIndisponivel,
    CofreTrancado,
    LoginRecusado,
    contexto_tls,
)
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros, carregar

pytestmark = pytest.mark.anyio

Resposta = Callable[[httpx2.Request], httpx2.Response]
P = Parametros(APP_ENV="teste", VAULT_ADDR="http://cofre.teste:8200")
CIFRADO = "vault:v1:abc"
PRIMEIRO_TOKEN = "tok-1"


def resp(status: int, corpo: dict[str, Any] | None = None) -> Resposta:
    return lambda _: httpx2.Response(status, json=corpo) if corpo is not None else httpx2.Response(status)


def login(token: str = PRIMEIRO_TOKEN, validade: int = 60, *, renovavel: bool = True) -> Resposta:
    auth = {"client_token": token, "lease_duration": validade, "renewable": renovavel}
    return resp(200, {"auth": {**auth, "metadata": {"role_name": "despachante"}}})


def renovado(validade: int) -> Resposta:
    return resp(200, {"auth": {"client_token": "nao-muda", "lease_duration": validade, "renewable": True}})


def cifrado(_: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"data": {"ciphertext": CIFRADO, "key_version": 1}})


def falha(erro: Exception) -> Resposta:
    def levantar(_: httpx2.Request) -> httpx2.Response:
        raise erro

    return levantar


SELADO = resp(503, {"errors": ["Vault is sealed"]})
NEGADO = resp(403, {"errors": ["1 error occurred:\n\t* permission denied\n\n"]})


@dataclass
class Pedido:
    metodo: str
    caminho: str
    token: str | None
    corpo: dict[str, Any]


class CofreFalso:
    """Responde por "METODO caminho"; a ultima resposta da fila se repete. Anota os pedidos."""

    def __init__(self) -> None:
        self.rotas: dict[str, list[Resposta]] = {}
        self.pedidos: list[Pedido] = []

    def em(self, rota: str, *respostas: Resposta) -> CofreFalso:
        self.rotas[rota] = list(respostas)
        return self

    def __call__(self, pedido: httpx2.Request) -> httpx2.Response:
        caminho = pedido.url.path.removeprefix("/v1/")
        corpo = json.loads(pedido.content) if pedido.content else {}
        self.pedidos.append(Pedido(pedido.method, caminho, pedido.headers.get("x-vault-token"), corpo))
        fila = self.rotas.get(f"{pedido.method} {caminho}")
        if not fila:
            return httpx2.Response(404, json={"errors": []})
        responder = fila.pop(0) if len(fila) > 1 else fila[0]
        return responder(pedido)

    def contar(self, rota: str) -> int:
        metodo, caminho = rota.split(" ", 1)
        return sum(1 for p in self.pedidos if p.metodo == metodo and p.caminho == caminho)


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


def novo(falso: CofreFalso, relogio: Relogio | None = None, parametros: Parametros = P) -> Cofre:
    return Cofre(
        parametros,
        role_id="role-do-papel",
        secret_id="secret-do-papel",
        transporte=httpx2.MockTransport(falso),
        relogio=relogio or Relogio(),
    )


async def test_cifrar_entra_e_manda_texto_e_contexto_em_base64() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login()).em("POST transit/encrypt/mcp-claro-chaves", cifrado)
    async with novo(falso) as cofre:
        assert await cofre.cifrar("mcp-claro-chaves", b"segredo", contexto=b"s1|p1") == CIFRADO
    entrar, cifrar = falso.pedidos[0], falso.pedidos[1]
    assert entrar.corpo == {"role_id": "role-do-papel", "secret_id": "secret-do-papel"}
    assert entrar.token is None
    assert cifrar.token == "tok-1"
    assert base64.b64decode(cifrar.corpo["plaintext"]) == b"segredo"
    assert base64.b64decode(cifrar.corpo["context"]) == b"s1|p1"


async def test_sair_do_bloco_revoga_o_token() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login()).em("POST transit/encrypt/mcp-claro-chaves", cifrado)
    falso.em("POST auth/token/revoke-self", resp(204))
    async with novo(falso) as cofre:
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
    assert falso.pedidos[-1].caminho == "auth/token/revoke-self"
    assert falso.pedidos[-1].token == "tok-1"


async def test_renova_quando_passa_dois_tercos_da_validade() -> None:
    relogio = Relogio()
    falso = CofreFalso().em("POST auth/approle/login", login(validade=60))
    falso.em("POST transit/encrypt/mcp-claro-chaves", cifrado).em("POST auth/token/renew-self", renovado(60))
    async with novo(falso, relogio) as cofre:
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
        relogio.agora += 39  # faltam 21 s: ainda acima de um terco
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
        assert falso.contar("POST auth/token/renew-self") == 0
        relogio.agora += 2  # faltam 19 s: renova antes de vencer
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
        assert falso.contar("POST auth/token/renew-self") == 1
        relogio.agora += 30  # a renovacao deu mais 60 s a partir dali
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
    assert falso.contar("POST auth/token/renew-self") == 1
    assert falso.contar("POST auth/approle/login") == 1


async def test_entra_de_novo_na_validade_maxima_e_revoga_o_antigo() -> None:
    relogio = Relogio()
    falso = CofreFalso().em("POST auth/approle/login", login("tok-1", 60), login("tok-2", 60))
    falso.em("POST transit/encrypt/mcp-claro-chaves", cifrado)
    falso.em("POST auth/token/renew-self", renovado(5))  # o OpenBao corta na validade maxima do papel
    falso.em("POST auth/token/revoke-self", resp(204))
    async with novo(falso, relogio) as cofre:
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
        relogio.agora += 50
        await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
    assert falso.contar("POST auth/approle/login") == 2
    revogados = [p.token for p in falso.pedidos if p.caminho == "auth/token/revoke-self"]
    assert revogados == ["tok-1", "tok-2"]  # o antigo ao trocar; o novo ao sair
    assert [p.token for p in falso.pedidos if p.caminho.startswith("transit/")] == ["tok-1", "tok-2"]


async def test_token_vencido_por_fora_entra_de_novo_e_tenta_uma_vez() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login("tok-1"), login("tok-2"))
    falso.em("POST transit/encrypt/mcp-claro-chaves", NEGADO, cifrado)
    falso.em("GET auth/token/lookup-self", resp(403, {"errors": ["permission denied"]}))
    async with novo(falso) as cofre:
        assert await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c") == CIFRADO
    assert falso.contar("POST auth/approle/login") == 2
    assert [p.token for p in falso.pedidos if p.caminho.startswith("transit/")] == ["tok-1", "tok-2"]


async def test_403_com_token_vivo_e_acesso_negado() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login())
    falso.em("POST transit/decrypt/mcp-claro-chaves", NEGADO)
    falso.em("GET auth/token/lookup-self", resp(200, {"data": {"ttl": 50}}))
    async with novo(falso) as cofre:
        with pytest.raises(AcessoNegado, match="permission denied") as erro:
            await cofre.decifrar("mcp-claro-chaves", CIFRADO, contexto=b"c")
    assert erro.value.status == 403
    assert falso.contar("POST auth/approle/login") == 1


async def test_login_recusado() -> None:
    falso = CofreFalso().em("POST auth/approle/login", resp(400, {"errors": ["invalid role or secret ID"]}))
    async with novo(falso) as cofre:
        with pytest.raises(LoginRecusado):
            await cofre.entrar()


async def test_decifrar_com_contexto_errado_e_cifrado_invalido() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login())
    falso.em("POST transit/decrypt/mcp-claro-chaves", resp(400, {"errors": ["cipher: message authentication failed"]}))
    async with novo(falso) as cofre:
        with pytest.raises(CifradoInvalido, match="authentication failed"):
            await cofre.decifrar("mcp-claro-chaves", CIFRADO, contexto=b"outro")


async def test_cofre_trancado_levanta_cofre_trancado() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login()).em("POST transit/encrypt/mcp-claro-chaves", SELADO)
    async with novo(falso) as cofre:
        with pytest.raises(CofreTrancado):
            await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")
    # Login com o cofre trancado tambem.
    falso = CofreFalso().em("POST auth/approle/login", SELADO)
    async with novo(falso) as cofre:
        with pytest.raises(CofreTrancado):
            await cofre.entrar()


async def test_503_sem_texto_confere_a_saude() -> None:
    saude_trancado = resp(200, {"initialized": True, "sealed": True, "version": "2.7.2"})
    saude_ok = resp(200, {"initialized": True, "sealed": False, "version": "2.7.2"})
    for saude, esperado in ((saude_trancado, CofreTrancado), (saude_ok, CofreIndisponivel)):
        falso = CofreFalso().em("POST auth/approle/login", login())
        falso.em("POST transit/encrypt/mcp-claro-chaves", resp(503, {"errors": []})).em("GET sys/health", saude)
        async with novo(falso) as cofre:
            with pytest.raises(esperado):
                await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")


@pytest.mark.parametrize(
    "erro",
    [
        httpx2.ConnectError("recusado"),
        httpx2.ReadTimeout("tempo"),
        ssl.SSLError(1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required"),
    ],
)
async def test_falha_de_rede_ou_tls_e_cofre_indisponivel(erro: Exception) -> None:
    falso = CofreFalso().em("GET sys/health", falha(erro)).em("POST auth/approle/login", falha(erro))
    async with novo(falso) as cofre:
        with pytest.raises(CofreIndisponivel):
            await cofre.saude()
        with pytest.raises(CofreIndisponivel):
            await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")


async def test_5xx_e_429_sao_cofre_indisponivel() -> None:
    for status in (500, 429):
        falso = CofreFalso().em("POST auth/approle/login", login())
        falso.em("POST transit/encrypt/mcp-claro-chaves", resp(status, {"errors": ["falhou"]}))
        async with novo(falso) as cofre:
            with pytest.raises(CofreIndisponivel, match=str(status)):
                await cofre.cifrar("mcp-claro-chaves", b"x", contexto=b"c")


async def test_saude_le_inicializado_trancado_e_versao() -> None:
    falso = CofreFalso().em("GET sys/health", resp(200, {"initialized": False, "sealed": True, "version": "2.7.2"}))
    async with novo(falso) as cofre:
        estado = await cofre.saude()
    assert (estado.inicializado, estado.trancado, estado.versao, estado.destravado) == (False, True, "2.7.2", False)
    assert falso.pedidos[0].token is None  # sem login


def _segredo_kv(valor: bytes, versao: int = 3) -> Resposta:
    return resp(200, {"data": {"data": {"chave": base64.b64encode(valor).decode()}, "metadata": {"version": versao}}})


async def test_chave_estado_le_valor_e_versao() -> None:
    chave = bytes(range(32))
    falso = CofreFalso().em("POST auth/approle/login", login())
    falso.em("GET kv/data/mcp-claro/estado-confirmacoes", _segredo_kv(chave, 7))
    async with novo(falso) as cofre:
        lida = await cofre.ler_chave_estado_versionada()
        assert await cofre.ler_chave_estado() == chave
    assert (lida.valor, lida.versao) == (chave, 7)
    assert "valor" not in repr(lida)


@pytest.mark.parametrize(
    "resposta",
    [_segredo_kv(b"curta" * 3), resp(404, {"errors": []}), resp(200, {"data": {"data": {"outra": "x"}}})],
)
async def test_chave_estado_curta_ou_ausente_e_recusada(resposta: Resposta) -> None:
    falso = CofreFalso().em("POST auth/approle/login", login())
    falso.em("GET kv/data/mcp-claro/estado-confirmacoes", resposta)
    async with novo(falso) as cofre:
        with pytest.raises(ChaveEstadoInvalida):
            await cofre.ler_chave_estado()


async def test_girar_chave_estado_grava_32_bytes_aleatorios() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login())
    falso.em("POST kv/data/mcp-claro/estado-confirmacoes", resp(200, {"data": {"version": 2}}))
    async with novo(falso) as cofre:
        assert await cofre.girar_chave_estado() == 2
        assert await cofre.girar_chave_estado() == 2
    gravadas = [base64.b64decode(p.corpo["data"]["chave"]) for p in falso.pedidos if p.caminho.startswith("kv/")]
    assert [len(g) for g in gravadas] == [32, 32]
    assert gravadas[0] != gravadas[1]


async def test_nome_de_chave_com_caminho_e_recusado_sem_pedido() -> None:
    falso = CofreFalso().em("POST auth/approle/login", login())
    async with novo(falso) as cofre:
        for nome in ("../sys/seal", "a/b", "", "a..b"):
            with pytest.raises(ValueError, match="chave invalido"):
                await cofre.cifrar(nome, b"x", contexto=b"c")
    assert not any(p.caminho.startswith("transit/") for p in falso.pedidos)


async def test_credenciais_vem_dos_arquivos(tmp_path: Path) -> None:
    (tmp_path / "role").write_text("role-do-arquivo\n", encoding="utf-8")
    (tmp_path / "secret").write_text("secret-do-arquivo\n", encoding="utf-8")
    p = Parametros(
        APP_ENV="teste",
        VAULT_ADDR="http://cofre.teste:8200",
        VAULT_ROLE_ID_FILE=str(tmp_path / "role"),
        VAULT_SECRET_ID_FILE=str(tmp_path / "secret"),
    )
    falso = CofreFalso().em("POST auth/approle/login", login())
    async with Cofre(p, transporte=httpx2.MockTransport(falso)) as cofre:
        await cofre.entrar()
    assert falso.pedidos[0].corpo == {"role_id": "role-do-arquivo", "secret_id": "secret-do-arquivo"}
    async with Cofre(P, transporte=httpx2.MockTransport(falso)) as cofre:
        with pytest.raises(ConfiguracaoInvalida, match="VAULT_ROLE_ID_FILE"):
            await cofre.entrar()


async def test_logs_nao_levam_token_nem_credenciais() -> None:
    relogio = Relogio()
    falso = CofreFalso().em("POST auth/approle/login", login("tok-secreto", 60), login("tok-secreto-2", 60))
    falso.em("POST transit/encrypt/mcp-claro-chaves", cifrado).em("POST auth/token/renew-self", renovado(3))
    with capture_logs() as linhas:
        async with novo(falso, relogio) as cofre:
            await cofre.cifrar("mcp-claro-chaves", b"conteudo-da-pessoa", contexto=b"c")
            relogio.agora += 50
            await cofre.cifrar("mcp-claro-chaves", b"conteudo-da-pessoa", contexto=b"c")
    assert {linha["event"] for linha in linhas} >= {"cofre_entrou"}
    texto = json.dumps(linhas, default=str)
    for proibido in ("tok-secreto", "secret-do-papel", "role-do-papel", "conteudo-da-pessoa", CIFRADO):
        assert proibido not in texto


@pytest.mark.parametrize(
    "endereco", ["ftp://cofre:8200", "https://cofre:8200/v1", "https://u:s@cofre:8200", "cofre:8200"]
)
def test_endereco_invalido_e_recusado(endereco: str) -> None:
    with pytest.raises(ConfiguracaoInvalida, match="VAULT_ADDR"):
        Cofre(Parametros(VAULT_ADDR=endereco, INTERNAL_CA_FILE="/nao/importa"))


def test_montagem_com_caminho_estranho_e_recusada() -> None:
    with pytest.raises(ConfiguracaoInvalida, match="VAULT_KV_MOUNT"):
        Cofre(Parametros(VAULT_ADDR="http://cofre:8200", VAULT_KV_MOUNT="kv/../sys"))


# TLS proprio da conexao interna


def _autoridade(destino: Path, nome: str) -> Path:
    chave = ec.generate_private_key(ec.SECP256R1())
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    agora = datetime.now(UTC)
    uso = x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )
    certificado = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(sujeito)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(minutes=1))
        .not_valid_after(agora + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(uso, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(chave.public_key()), critical=False)
        .sign(chave, hashes.SHA256())
    )
    arquivo = destino / f"{nome}.pem"
    arquivo.write_bytes(certificado.public_bytes(serialization.Encoding.PEM))
    return arquivo


def test_https_exige_autoridade_interna(tmp_path: Path) -> None:
    with pytest.raises(ConfiguracaoInvalida, match="INTERNAL_CA_FILE"):
        Cofre(Parametros(VAULT_ADDR="https://cofre:8200"))
    with pytest.raises(ConfiguracaoInvalida, match="carregar INTERNAL_CA_FILE"):
        Cofre(Parametros(VAULT_ADDR="https://cofre:8200", INTERNAL_CA_FILE=str(tmp_path / "nao-existe.pem")))


def test_contexto_tls_confia_so_na_autoridade_interna(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    interna = _autoridade(tmp_path, "autoridade-interna")
    monkeypatch.setenv("SSL_CERT_FILE", str(_autoridade(tmp_path, "autoridade-global")))
    ctx = contexto_tls(Parametros(VAULT_ADDR="https://cofre:8200", INTERNAL_CA_FILE=str(interna)))
    nomes = [dict(par[0] for par in ca["subject"])["commonName"] for ca in ctx.get_ca_certs()]
    assert nomes == ["autoridade-interna"]
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


# Parametros novos do cofre


def test_parametros_do_cofre_com_valores_iniciais() -> None:
    p = carregar()
    assert (p.VAULT_TRANSIT_MOUNT, p.VAULT_TRANSIT_KEY) == ("transit", "mcp-claro-chaves")
    assert (p.VAULT_KV_MOUNT, p.VAULT_STATE_KEY_PATH) == ("kv", "mcp-claro/estado-confirmacoes")
    assert p.VAULT_TIMEOUT_SECONDS == 5


def test_partida_recusa_metodo_de_login_que_nao_e_approle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_AUTH_METHOD", "userpass")
    with pytest.raises(ConfiguracaoInvalida, match="approle"):
        carregar()


def test_producao_exige_cofre_com_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "producao")
    monkeypatch.setenv("VAULT_ADDR", "http://cofre:8200")
    problemas = Parametros().problemas()
    assert any("VAULT_ADDR" in p for p in problemas)
    monkeypatch.setenv("VAULT_ADDR", "https://cofre:8200")
    assert not any("VAULT_ADDR" in p for p in Parametros().problemas())
