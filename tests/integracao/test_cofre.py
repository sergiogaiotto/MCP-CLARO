"""Cliente fino do cofre contra um OpenBao 2.7.2 de verdade, em Docker.

O OpenBao sobe em modo dev (memoria) e e preparado pela API como no Compose: Transit com a chave
``mcp-claro-chaves`` (derived), KV v2 com a chave do estado das confirmacoes, as politicas de
``docker/cofre/politicas/`` e um AppRole por papel. Um teste a parte roda ``scripts/iniciar-cofre.sh``
dentro do conteiner, duas vezes, e confere que ele e idempotente.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import socket
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mcp_claro.cofre import (
    AcessoNegado,
    CifradoInvalido,
    Cofre,
    CofreIndisponivel,
    CofreTrancado,
    LoginRecusado,
)
from mcp_claro.config.settings import Parametros

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

IMAGEM_OPENBAO = os.environ.get(
    "MCP_CLARO_TESTE_OPENBAO",
    "openbao/openbao:2.7.2@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f",
)
RAIZ = Path(__file__).resolve().parents[2]
POLITICAS = RAIZ / "docker" / "cofre" / "politicas"
PAPEIS = ("entrada", "despachante", "rotinas")
CHAVE = "mcp-claro-chaves"
ESTADO = "mcp-claro/estado-confirmacoes"


def contexto(sistema: str, pessoa: str) -> bytes:
    """Mesmo formato do despachante (``despachante.chaves.contexto``)."""
    return f"mcp-claro|{sistema}|p:{pessoa}".encode()


def api(url: str, token: str, metodo: str, caminho: str, corpo: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pedido de administrador (token raiz), so para preparar e mexer no cofre de teste."""
    with httpx2.Client(base_url=f"{url}/v1/", trust_env=False, timeout=10, follow_redirects=False) as c:
        r = c.request(metodo, caminho, json=corpo, headers={"X-Vault-Token": token})
    assert r.is_success, (metodo, caminho, r.status_code, r.text)
    return r.json() if r.content else {}


def esperar_saude(url: str, *, destravado: bool = True, verify: Any = True) -> None:
    limite = time.monotonic() + 60
    while time.monotonic() < limite:
        try:
            with httpx2.Client(trust_env=False, timeout=2, verify=verify) as c:
                r = c.get(f"{url}/v1/sys/health", params={"sealedcode": "200", "uninitcode": "200"})
            if r.status_code == 200 and (not destravado or r.json().get("sealed") is False):
                return
        except httpx2.TransportError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"OpenBao nao ficou pronto em {url}")


def preparar(url: str, raiz: str, pasta: Path) -> None:
    """O que scripts/iniciar-cofre.sh faz no Compose, pela API."""
    api(url, raiz, "POST", "sys/mounts/transit", {"type": "transit"})
    api(url, raiz, "POST", f"transit/keys/{CHAVE}", {"type": "aes256-gcm96", "derived": True})
    api(url, raiz, "POST", "sys/mounts/kv", {"type": "kv", "options": {"version": "2"}})
    for _ in range(50):  # o KV v2 recem-ligado leva um instante para aceitar escrita
        try:
            api(url, raiz, "POST", f"kv/metadata/{ESTADO}", {"max_versions": 5})
            break
        except AssertionError:
            time.sleep(0.2)
    chave = base64.b64encode(secrets.token_bytes(32)).decode()
    api(url, raiz, "POST", f"kv/data/{ESTADO}", {"data": {"chave": chave}})
    api(url, raiz, "POST", "sys/auth/approle", {"type": "approle"})
    for papel in PAPEIS:
        politica = (POLITICAS / f"{papel}.hcl").read_text(encoding="utf-8")
        api(url, raiz, "PUT", f"sys/policies/acl/mcp-claro-{papel}", {"policy": politica})
        api(
            url,
            raiz,
            "POST",
            f"auth/approle/role/{papel}",
            {
                "token_policies": [f"mcp-claro-{papel}"],
                "token_type": "service",
                "token_ttl": "15m",
                "token_max_ttl": "1h",
            },
        )
        role_id = api(url, raiz, "GET", f"auth/approle/role/{papel}/role-id")["data"]["role_id"]
        secret_id = api(url, raiz, "POST", f"auth/approle/role/{papel}/secret-id")["data"]["secret_id"]
        (pasta / f"{papel}_role_id").write_text(role_id, encoding="utf-8")
        (pasta / f"{papel}_secret_id").write_text(secret_id + "\n", encoding="utf-8")


@dataclass
class Bao:
    url: str
    raiz: str
    conteiner: Any
    pasta: Path

    def parametros(self, papel: str, **extra: Any) -> Parametros:
        return Parametros(
            APP_ENV="teste",
            VAULT_ADDR=self.url,
            VAULT_ROLE_ID_FILE=str(self.pasta / f"{papel}_role_id"),
            VAULT_SECRET_ID_FILE=str(self.pasta / f"{papel}_secret_id"),
            **extra,
        )

    def api(self, metodo: str, caminho: str, corpo: dict[str, Any] | None = None) -> dict[str, Any]:
        return api(self.url, self.raiz, metodo, caminho, corpo)

    def destravar(self) -> None:
        """No modo dev, a chave de destravar sai no log da partida."""
        saida, erros = self.conteiner.get_logs()
        achado = re.search(r"Unseal Key: (\S+)", (saida + erros).decode())
        assert achado, "chave de destravar nao apareceu no log do OpenBao dev"
        with httpx2.Client(trust_env=False, timeout=5) as c:
            c.post(f"{self.url}/v1/sys/unseal", json={"key": achado.group(1)})
        esperar_saude(self.url)


def _conteiner(**volumes: str) -> Any:
    testcontainers = pytest.importorskip("testcontainers.core.container")
    raiz = secrets.token_hex(16)
    c = testcontainers.DockerContainer(IMAGEM_OPENBAO).with_env("BAO_DEV_ROOT_TOKEN_ID", raiz).with_exposed_ports(8200)
    for destino, origem in volumes.items():
        c = c.with_volume_mapping(origem, destino, "ro")
    return c, raiz


@pytest.fixture(scope="module")
def openbao(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bao]:
    conteiner, raiz = _conteiner()
    with conteiner as c:
        url = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(8200)}"
        esperar_saude(url)
        pasta = tmp_path_factory.mktemp("cofre")
        preparar(url, raiz, pasta)
        yield Bao(url, raiz, c, pasta)


class Gravador(httpx2.AsyncBaseTransport):
    """Transporte de verdade que anota caminho e token de cada pedido (so para conferir nos testes)."""

    def __init__(self) -> None:
        self._real = httpx2.AsyncHTTPTransport()
        self.pedidos: list[tuple[str, str | None]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.pedidos.append((request.url.path.removeprefix("/v1/"), request.headers.get("x-vault-token")))
        return await self._real.handle_async_request(request)

    async def aclose(self) -> None:
        await self._real.aclose()

    def contar(self, caminho: str) -> int:
        return sum(1 for c, _ in self.pedidos if c == caminho)


class Relogio:
    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


async def test_cifrar_e_decifrar_ida_e_volta(openbao: Bao) -> None:
    texto = secrets.token_bytes(48)
    async with Cofre(openbao.parametros("despachante")) as cofre:
        cifrado = await cofre.cifrar(CHAVE, texto, contexto=contexto("sistema-a", "pessoa-1"))
        assert cifrado.startswith("vault:v1:")
        assert await cofre.decifrar(CHAVE, cifrado, contexto=contexto("sistema-a", "pessoa-1")) == texto
        outro = await cofre.cifrar(CHAVE, texto, contexto=contexto("sistema-a", "pessoa-2"))
        assert outro != cifrado
        cifra = cofre.cifra()  # formato do despachante, chave VAULT_TRANSIT_KEY
        ctx = contexto("sistema-b", "pessoa-9")
        assert await cifra.decifrar(await cifra.cifrar(b"chave-do-sistema", ctx), ctx) == b"chave-do-sistema"


async def test_contexto_errado_nao_decifra(openbao: Bao) -> None:
    async with Cofre(openbao.parametros("despachante")) as cofre:
        cifrado = await cofre.cifrar(CHAVE, b"chave-da-pessoa", contexto=contexto("sistema-a", "pessoa-1"))
        for errado in (contexto("sistema-a", "pessoa-2"), contexto("sistema-b", "pessoa-1"), b"", None):
            with pytest.raises(CifradoInvalido):
                await cofre.decifrar(CHAVE, cifrado, contexto=errado)
        prefixo, corpo = cifrado.rsplit(":", 1)
        adulterado = f"{prefixo}:{('B' if corpo[0] == 'A' else 'A')}{corpo[1:]}"
        with pytest.raises(CifradoInvalido):
            await cofre.decifrar(CHAVE, adulterado, contexto=contexto("sistema-a", "pessoa-1"))


async def test_entrada_e_rotinas_nao_cifram_nem_decifram(openbao: Bao) -> None:
    ctx = contexto("sistema-a", "pessoa-1")
    async with Cofre(openbao.parametros("despachante")) as despachante:
        cifrado = await despachante.cifrar(CHAVE, b"chave-da-pessoa", contexto=ctx)
    for papel in ("entrada", "rotinas"):
        async with Cofre(openbao.parametros(papel)) as cofre:
            with pytest.raises(AcessoNegado) as erro:
                await cofre.decifrar(CHAVE, cifrado, contexto=ctx)
            assert erro.value.status == 403
            with pytest.raises(AcessoNegado):
                await cofre.cifrar(CHAVE, b"x", contexto=ctx)


async def test_despachante_nao_le_nem_gira_a_chave_do_estado(openbao: Bao) -> None:
    async with Cofre(openbao.parametros("despachante")) as cofre:
        with pytest.raises(AcessoNegado):
            await cofre.ler_chave_estado()
        with pytest.raises(AcessoNegado):
            await cofre.girar_chave_estado()


async def test_chave_do_estado_tem_32_bytes_e_o_rodizio_cria_versao_nova(openbao: Bao) -> None:
    async with Cofre(openbao.parametros("entrada")) as entrada, Cofre(openbao.parametros("rotinas")) as rotinas:
        antes = await entrada.ler_chave_estado_versionada()
        assert len(antes.valor) >= 32
        nova = await rotinas.girar_chave_estado()
        assert nova == antes.versao + 1
        depois = await entrada.ler_chave_estado_versionada()
        assert depois.versao == nova
        assert len(depois.valor) >= 32
        assert depois.valor != antes.valor
        assert await entrada.ler_chave_estado() == depois.valor
        # A versao anterior continua legivel, para conferir estados criados antes do rodizio.
        assert await entrada.ler_chave_estado(versao=antes.versao) == antes.valor
        with pytest.raises(AcessoNegado):
            await entrada.girar_chave_estado()
        with pytest.raises(AcessoNegado):
            await rotinas.ler_chave_estado()


async def test_login_errado_ou_de_outro_papel_e_recusado(openbao: Bao) -> None:
    async with Cofre(openbao.parametros("entrada"), secret_id="nao-e-este") as cofre:
        with pytest.raises(LoginRecusado):
            await cofre.entrar()
    secret_do_despachante = (openbao.pasta / "despachante_secret_id").read_text(encoding="utf-8").strip()
    async with Cofre(openbao.parametros("entrada"), secret_id=secret_do_despachante) as cofre:
        with pytest.raises(LoginRecusado):
            await cofre.ler_chave_estado()


async def test_renova_o_token_antes_de_vencer(openbao: Bao) -> None:
    relogio, gravador = Relogio(), Gravador()
    ctx = contexto("sistema-a", "pessoa-1")
    async with Cofre(openbao.parametros("despachante"), transporte=gravador, relogio=relogio) as cofre:
        await cofre.cifrar(CHAVE, b"x", contexto=ctx)
        relogio.agora += 601  # token de 15 min: faltam 299 s, menos de um terco
        await cofre.cifrar(CHAVE, b"x", contexto=ctx)
    assert gravador.contar("auth/approle/login") == 1
    assert gravador.contar("auth/token/renew-self") == 1
    tokens = {t for c, t in gravador.pedidos if c.startswith("transit/")}
    assert len(tokens) == 1  # renovado, nao trocado
    assert gravador.pedidos[-1][0] == "auth/token/revoke-self"


async def test_token_revogado_por_fora_entra_de_novo(openbao: Bao) -> None:
    gravador = Gravador()
    ctx = contexto("sistema-a", "pessoa-1")
    async with Cofre(openbao.parametros("despachante"), transporte=gravador) as cofre:
        cifrado = await cofre.cifrar(CHAVE, b"x", contexto=ctx)
        token = next(t for c, t in gravador.pedidos if c.startswith("transit/"))
        assert token
        openbao.api("POST", "auth/token/revoke", {"token": token})
        assert await cofre.decifrar(CHAVE, cifrado, contexto=ctx) == b"x"
    assert gravador.contar("auth/approle/login") == 2
    assert gravador.contar("auth/token/lookup-self") == 1


async def test_cofre_trancado_fecha_a_porta(openbao: Bao) -> None:
    ctx = contexto("sistema-a", "pessoa-1")
    async with Cofre(openbao.parametros("despachante")) as ja_entrou, Cofre(openbao.parametros("despachante")) as novo:
        cifrado = await ja_entrou.cifrar(CHAVE, b"x", contexto=ctx)
        assert (await novo.saude()).destravado
        openbao.api("POST", "sys/seal")
        try:
            estado = await novo.saude()
            assert estado.inicializado
            assert estado.trancado
            assert not estado.destravado
            assert estado.versao == "2.7.2"
            with pytest.raises(CofreTrancado):
                await ja_entrou.cifrar(CHAVE, b"x", contexto=ctx)
            with pytest.raises(CofreTrancado):
                await novo.decifrar(CHAVE, cifrado, contexto=ctx)  # login com o cofre trancado
            async with Cofre(openbao.parametros("entrada")) as entrada:
                with pytest.raises(CofreTrancado):
                    await entrada.ler_chave_estado()
        finally:
            openbao.destravar()
        assert (await novo.saude()).destravado
        assert await ja_entrou.decifrar(CHAVE, cifrado, contexto=ctx) == b"x"


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_endereco_errado_e_cofre_indisponivel() -> None:
    p = Parametros(APP_ENV="teste", VAULT_ADDR=f"http://127.0.0.1:{_porta_livre()}", VAULT_TIMEOUT_SECONDS=2)
    async with Cofre(p, role_id="r", secret_id="s") as cofre:
        with pytest.raises(CofreIndisponivel):
            await cofre.saude()
        with pytest.raises(CofreIndisponivel):
            await cofre.cifrar(CHAVE, b"x", contexto=b"c")


def _autoridade(destino: Path) -> Path:
    chave = ec.generate_private_key(ec.SECP256R1())
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "outra autoridade")])
    agora = datetime.now(UTC)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(sujeito)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(minutes=1))
        .not_valid_after(agora + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(chave, hashes.SHA256())
    )
    arquivo = destino / "outra.pem"
    arquivo.write_bytes(certificado.public_bytes(serialization.Encoding.PEM))
    return arquivo


async def test_tls_confia_so_na_autoridade_interna(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conteiner, _ = _conteiner()
    conteiner = conteiner.with_command("server -dev -dev-tls -dev-tls-cert-dir=/openbao/file")
    with conteiner as c:
        ca = tmp_path / "ca.pem"
        for _ in range(100):
            lido = c.exec(["cat", "/openbao/file/vault-ca.pem"])
            if lido.exit_code == 0 and lido.output:
                ca.write_bytes(lido.output)
                break
            await anyio.sleep(0.2)
        url = f"https://{c.get_container_host_ip()}:{c.get_exposed_port(8200)}"
        esperar_saude(url, verify=ssl.create_default_context(cafile=str(ca)))
        async with Cofre(Parametros(APP_ENV="teste", VAULT_ADDR=url, INTERNAL_CA_FILE=str(ca))) as cofre:
            assert (await cofre.saude()).destravado
        # Com a autoridade interna errada nao fala, mesmo com SSL_CERT_FILE apontando para a certa.
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        errada = Parametros(APP_ENV="teste", VAULT_ADDR=url, INTERNAL_CA_FILE=str(_autoridade(tmp_path)))
        async with Cofre(errada) as cofre:
            with pytest.raises(CofreIndisponivel):
                await cofre.saude()


async def test_script_iniciar_cofre_e_idempotente() -> None:
    conteiner, raiz = _conteiner(**{"/iniciar/scripts": str(RAIZ / "scripts"), "/iniciar/politicas": str(POLITICAS)})
    arquivos = [f"cofre_{papel}_{tipo}" for papel in PAPEIS for tipo in ("role_id", "secret_id")]
    comando = [
        "env",
        "BAO_ADDR=http://127.0.0.1:8200",
        f"BAO_TOKEN={raiz}",
        "COFRE_SEGREDOS=/openbao/file/segredos",
        "COFRE_POLITICAS=/iniciar/politicas",
        "sh",
        "/iniciar/scripts/iniciar-cofre.sh",
    ]
    with conteiner as c:
        url = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(8200)}"
        esperar_saude(url)
        assert c.exec(["mkdir", "-p", "/openbao/file/segredos"]).exit_code == 0
        lidos = []
        for _ in range(2):
            rodada = c.exec(comando)
            assert rodada.exit_code == 0, rodada.output.decode()
            lidos.append({nome: c.exec(["cat", f"/openbao/file/segredos/{nome}"]).output.decode() for nome in arquivos})
        assert lidos[0] == lidos[1]  # a segunda rodada nao troca credencial que ainda entra
        assert all(valor and "\n" not in valor for valor in lidos[0].values())

        chave = api(url, raiz, "GET", f"transit/keys/{CHAVE}")["data"]
        assert chave["derived"] is True
        assert chave["exportable"] is False
        metadados = api(url, raiz, "GET", f"kv/metadata/{ESTADO}")["data"]
        assert metadados["current_version"] == 1  # a segunda rodada nao girou a chave
        assert metadados["max_versions"] == 5

        def cofre(papel: str) -> Cofre:
            return Cofre(
                Parametros(APP_ENV="teste", VAULT_ADDR=url),
                role_id=lidos[0][f"cofre_{papel}_role_id"],
                secret_id=lidos[0][f"cofre_{papel}_secret_id"],
            )

        ctx = contexto("sistema-a", "pessoa-1")
        async with cofre("despachante") as despachante, cofre("entrada") as entrada, cofre("rotinas") as rotinas:
            cifrado = await despachante.cifrar(CHAVE, b"segredo", contexto=ctx)
            assert await despachante.decifrar(CHAVE, cifrado, contexto=ctx) == b"segredo"
            assert len(await entrada.ler_chave_estado()) >= 32
            with pytest.raises(AcessoNegado):
                await entrada.decifrar(CHAVE, cifrado, contexto=ctx)
            assert await rotinas.girar_chave_estado() == 2
