"""A sala de visitas no Compose, nos certificados e no cofre (desenho 3.8 e 6.1, prova I6; criticas M4 e B9).

- Compose (``docker compose config``, sem subir nada): ``visitas`` usa a rede do ``visitas-muro``, que so esta nas
  redes ``visitas`` (interna, so com o despachante) e ``saida_visitas`` (so a sala); fora da ``interna`` e da
  ``saida``; sem segredo de banco nem de cofre; sem capacidade nenhuma. A prova que conecta de verdade fica em
  ``test_sala_isolada.py``.
- Certificados (``scripts/gerar-segredos-dev.sh``): a sala tem autoridade propria (``ca_visitas``), so
  ``serverAuth``; a ``ca_clientes_visitas`` so emite o certificado de cliente do despachante; o certificado da sala
  e recusado por um TLS montado como o do cofre e como o da porta 9443 do despachante.
- Cofre (``scripts/iniciar-cofre.sh`` num OpenBao de verdade): a chave Transit de terceiros nasce ``derived``; so o
  despachante cifra e decifra com ela; o cifrado de terceiro nao abre com a chave dos proprios no mesmo contexto.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from mcp_claro.cofre import AcessoNegado, CifradoInvalido, Cofre
from mcp_claro.config.settings import Parametros
from tests.conftest import RAIZ
from tests.integracao.test_cofre import PAPEIS, POLITICAS, _conteiner, api, contexto, esperar_saude

pytestmark = [pytest.mark.integracao]

CHAVE_PROPRIOS = "mcp-claro-chaves"
CHAVE_TERCEIROS = "mcp-claro-chaves-terceiros"
SEGREDOS_DA_SALA = {"ca_visitas.pem", "visitas_cert.pem", "visitas_key.pem", "ca_clientes_visitas.pem"}


# --- Compose --------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> Iterator[dict[str, Any]]:
    if shutil.which("docker") is None:
        pytest.skip("sem Docker: o teste precisa do 'docker compose config'")
    if subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode != 0:  # noqa: S607
        pytest.skip("sem o plugin 'docker compose'")
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-compose-visitas-"))
    try:
        for nome in ("docker", "grafana", "scripts"):
            shutil.copytree(RAIZ / nome, raiz / nome, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        segredos = raiz / ".segredos"
        segredos.mkdir()
        texto = (raiz / "docker" / "compose.yml").read_text(encoding="utf-8")
        for nome in set(re.findall(r"\.\./\.segredos/([A-Za-z0-9_.-]+)", texto)):
            (segredos / nome).touch()
        comando = ["docker", "compose", "-p", f"mcpclarovis{uuid.uuid4().hex[:8]}", "-f", "docker/compose.yml"]
        r = subprocess.run(  # noqa: S603
            [*comando, "config", "--format", "json"], cwd=raiz, capture_output=True, text=True, timeout=120, check=False
        )
        assert r.returncode == 0, r.stderr[-3000:]
        yield json.loads(r.stdout)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)


def _redes(servico: dict[str, Any]) -> set[str]:
    return set(servico.get("networks") or {})


def _segredos(servico: dict[str, Any]) -> set[str]:
    return {s["source"] for s in servico.get("secrets", [])}


def test_sala_usa_a_rede_do_muro_e_o_muro_so_esta_nas_redes_da_sala(compose: dict[str, Any]) -> None:
    servicos = compose["services"]
    sala, muro = servicos["visitas"], servicos["visitas-muro"]
    assert sala["network_mode"] == "service:visitas-muro"
    assert "networks" not in sala
    assert _redes(muro) == {"visitas", "saida_visitas"}
    assert muro["networks"]["visitas"]["aliases"] == ["visitas"]  # o despachante chama https://visitas:9446
    assert "ports" not in sala
    assert "ports" not in muro
    assert sala["depends_on"]["visitas-muro"]["condition"] == "service_healthy"


def test_rede_visitas_e_interna_e_so_tem_o_despachante_e_a_sala(compose: dict[str, Any]) -> None:
    redes = compose["networks"]
    assert redes["visitas"].get("internal") is True
    assert not redes["saida_visitas"].get("internal")
    na_visitas = {nome for nome, s in compose["services"].items() if "visitas" in _redes(s)}
    na_saida_da_sala = {nome for nome, s in compose["services"].items() if "saida_visitas" in _redes(s)}
    assert na_visitas == {"despachante", "visitas-muro"}
    assert na_saida_da_sala == {"visitas-muro"}
    for nome in ("visitas", "visitas-muro"):
        assert not _redes(compose["services"][nome]) & {"interna", "saida", "borda"}


def test_sala_sem_banco_sem_cofre_e_sem_capacidade(compose: dict[str, Any]) -> None:
    sala = compose["services"]["visitas"]
    assert sala["command"] == ["visitas"]
    assert sala["image"] == compose["services"]["despachante"]["image"]
    assert _segredos(sala) == SEGREDOS_DA_SALA
    ambiente = sala["environment"]
    assert not [n for n in ambiente if n.startswith(("DATABASE_", "DB_", "VAULT_"))]
    assert {n for n in ambiente if n.endswith("_FILE")} == {
        "INTERNAL_CA_FILE",
        "INTERNAL_CERT_FILE",
        "INTERNAL_KEY_FILE",
        "INTERNAL_CLIENT_CA_FILE",
    }
    assert ambiente["INTERNAL_CLIENT_CA_FILE"] == "/run/secrets/ca_clientes_visitas.pem"
    assert ambiente["INTERNAL_CERT_FILE"] == "/run/secrets/visitas_cert.pem"
    assert sala["cap_drop"] == ["ALL"]
    assert "cap_add" not in sala
    assert sala["read_only"] is True
    assert sala["security_opt"] == ["no-new-privileges:true"]
    assert sala["healthcheck"]["test"] == ["CMD", "python", "-m", "mcp_claro.visitas.saude"]
    assert set(sala.get("depends_on", {})) == {"visitas-muro"}  # nao depende de banco nem de cofre


def test_muro_so_tem_net_admin_e_bloqueia_o_que_nao_e_publico(compose: dict[str, Any]) -> None:
    muro = compose["services"]["visitas-muro"]
    assert re.fullmatch(r"busybox:[0-9.]+@sha256:[0-9a-f]{64}", muro["image"])
    assert (muro["cap_drop"], muro["cap_add"]) == (["ALL"], ["NET_ADMIN"])
    assert muro["read_only"] is True
    assert muro["security_opt"] == ["no-new-privileges:true"]
    assert not {"secrets", "volumes", "environment", "ports"} & set(muro)
    roteiro = muro["entrypoint"][2]
    for destino in ("169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"):
        assert destino in roteiro
    assert "ip route replace unreachable" in roteiro
    assert "onlink" in roteiro


def test_despachante_alcanca_a_sala_com_o_certificado_dela(compose: dict[str, Any]) -> None:
    servicos = compose["services"]
    despachante = servicos["despachante"]
    assert {"interna", "saida", "visitas"} == _redes(despachante)
    ambiente = despachante["environment"]
    assert ambiente["VISITS_INTERNAL_URL"] == "https://visitas:9446"
    assert servicos["visitas"]["environment"]["VISITS_INTERNAL_PORT"] == "9446"
    assert ambiente["VISITS_CA_FILE"] == "/run/secrets/ca_visitas.pem"
    assert ambiente["VISITS_CLIENT_CERT_FILE"] == "/run/secrets/despachante_visitas_cliente_cert.pem"
    assert ambiente["VISITS_CLIENT_KEY_FILE"] == "/run/secrets/despachante_visitas_cliente_key.pem"
    assert {"ca_visitas.pem", "despachante_visitas_cliente_cert.pem", "despachante_visitas_cliente_key.pem"} <= (
        _segredos(despachante)
    )
    # A chave da sala so fica na sala; a chave de cliente para a sala so fica no despachante.
    com_chave_da_sala = {n for n, s in servicos.items() if "visitas_key.pem" in _segredos(s)}
    com_cliente_da_sala = {n for n, s in servicos.items() if "despachante_visitas_cliente_key.pem" in _segredos(s)}
    assert (com_chave_da_sala, com_cliente_da_sala) == ({"visitas"}, {"despachante"})
    # Fora de desenvolvimento e teste a sala e obrigatoria: todo papel do gateway ja leva o endereco.
    for nome in ("entrada", "despachante", "rotinas", "migrar"):
        assert servicos[nome]["environment"]["VISITS_INTERNAL_URL"] == "https://visitas:9446", nome


# --- certificados (M4) -----------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def segredos() -> Iterator[Path]:
    if shutil.which("openssl") is None:
        pytest.skip("sem openssl")
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-segredos-visitas-"))
    try:
        shutil.copytree(RAIZ / "scripts", raiz / "scripts")
        subprocess.run(["bash", "scripts/gerar-segredos-dev.sh"], cwd=raiz, check=True, capture_output=True)  # noqa: S607
        yield raiz / ".segredos"
    finally:
        shutil.rmtree(raiz, ignore_errors=True)


def _cert(pasta: Path, nome: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate((pasta / nome).read_bytes())


def _usos(cert: x509.Certificate) -> list[x509.ObjectIdentifier]:
    return list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)


def test_sala_tem_autoridade_propria_e_certificado_so_de_servidor(segredos: Path) -> None:
    sala, ca_visitas = _cert(segredos, "visitas_cert.pem"), _cert(segredos, "ca_visitas.pem")
    assert sala.issuer == ca_visitas.subject
    assert _usos(sala) == [ExtendedKeyUsageOID.SERVER_AUTH]
    nomes = sala.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert "visitas" in nomes
    for outra in (
        "ca_interna.pem",
        "ca_clientes_entrada.pem",
        "ca_clientes_despachante.pem",
        "ca_clientes_rotinas.pem",
    ):
        assert _cert(segredos, outra).subject != ca_visitas.subject


def test_autoridades_da_sala_so_emitem_o_que_e_delas(segredos: Path) -> None:
    ca_visitas = _cert(segredos, "ca_visitas.pem").subject
    ca_clientes = _cert(segredos, "ca_clientes_visitas.pem").subject
    emitidos: dict[str, list[str]] = {"ca_visitas": [], "ca_clientes_visitas": []}
    for arquivo in sorted(segredos.glob("*_cert.pem")):
        emissor = _cert(segredos, arquivo.name).issuer
        if emissor == ca_visitas:
            emitidos["ca_visitas"].append(arquivo.name)
        if emissor == ca_clientes:
            emitidos["ca_clientes_visitas"].append(arquivo.name)
    assert emitidos == {
        "ca_visitas": ["visitas_cert.pem"],
        "ca_clientes_visitas": ["despachante_visitas_cliente_cert.pem"],
    }
    assert _usos(_cert(segredos, "despachante_visitas_cliente_cert.pem")) == [ExtendedKeyUsageOID.CLIENT_AUTH]


def test_gerar_segredos_de_novo_nao_troca_os_certificados_da_sala(segredos: Path) -> None:
    nomes = ("ca_visitas.pem", "visitas_cert.pem", "ca_clientes_visitas.pem", "despachante_visitas_cliente_cert.pem")
    antes = {n: (segredos / n).read_bytes() for n in nomes}
    subprocess.run(["bash", "scripts/gerar-segredos-dev.sh"], cwd=segredos.parent, check=True, capture_output=True)  # noqa: S607
    assert {n: (segredos / n).read_bytes() for n in nomes} == antes


class _PortaComMtls:
    """Um TLS montado como uma porta interna: certificado de servidor e autoridade de clientes, cliente obrigatorio."""

    def __init__(self, pasta: Path, cert: str, chave: str, autoridade_de_clientes: str) -> None:
        self.contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.contexto.load_cert_chain(str(pasta / cert), str(pasta / chave))
        self.contexto.load_verify_locations(str(pasta / autoridade_de_clientes))
        self.contexto.verify_mode = ssl.CERT_REQUIRED
        self.escuta = socket.create_server(("127.0.0.1", 0))
        self.porta = int(self.escuta.getsockname()[1])
        self._fio = threading.Thread(target=self._atender, daemon=True)
        self._fio.start()

    def _atender(self) -> None:
        while True:
            try:
                conexao, _ = self.escuta.accept()
            except OSError:
                return
            try:
                with self.contexto.wrap_socket(conexao, server_side=True) as tls:
                    tls.recv(1024)
                    tls.sendall(b"aceito")
            except ssl.SSLError, OSError:
                continue

    def tentar(self, pasta: Path, autoridade_do_servidor: str, cert: str, chave: str) -> bool:
        cliente = ssl.create_default_context(cafile=str(pasta / autoridade_do_servidor))
        cliente.check_hostname = False
        cliente.load_cert_chain(str(pasta / cert), str(pasta / chave))
        try:
            with (
                socket.create_connection(("127.0.0.1", self.porta), timeout=5) as bruto,
                cliente.wrap_socket(bruto) as tls,
            ):
                tls.sendall(b"oi")
                return tls.recv(1024) == b"aceito"
        except ssl.SSLError, OSError:
            return False

    def fechar(self) -> None:
        self.escuta.close()


@pytest.mark.parametrize(
    ("porta", "servidor", "autoridade", "aceito"),
    [
        # O cofre aceita como cliente qualquer certificado da ca_interna (docker/cofre/config.hcl).
        ("cofre", ("cofre_cert.pem", "cofre_key.pem", "ca_interna.pem"), "ca_interna.pem", "despachante"),
        # A porta 9443 do despachante aceita so a ca_clientes_despachante.
        (
            "despachante",
            ("despachante_cert.pem", "despachante_key.pem", "ca_clientes_despachante.pem"),
            "ca_interna.pem",
            "entrada_cliente",
        ),
    ],
)
def test_certificado_da_sala_e_recusado_pelo_cofre_e_pela_porta_do_despachante(
    segredos: Path, porta: str, servidor: tuple[str, str, str], autoridade: str, aceito: str
) -> None:
    alvo = _PortaComMtls(segredos, *servidor)
    try:
        assert alvo.tentar(segredos, autoridade, f"{aceito}_cert.pem", f"{aceito}_key.pem"), porta  # a porta funciona
        assert not alvo.tentar(segredos, autoridade, "visitas_cert.pem", "visitas_key.pem"), porta
    finally:
        alvo.fechar()


def test_porta_da_sala_aceita_so_o_certificado_do_despachante_para_ela(segredos: Path) -> None:
    sala = _PortaComMtls(segredos, "visitas_cert.pem", "visitas_key.pem", "ca_clientes_visitas.pem")
    try:
        assert sala.tentar(
            segredos, "ca_visitas.pem", "despachante_visitas_cliente_cert.pem", "despachante_visitas_cliente_key.pem"
        )
        for outro in ("despachante", "entrada_cliente", "rotinas_cliente", "despachante_saude"):
            assert not sala.tentar(segredos, "ca_visitas.pem", f"{outro}_cert.pem", f"{outro}_key.pem"), outro
    finally:
        sala.fechar()


# --- cofre ---------------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_iniciar_cofre_cria_a_chave_de_terceiros_e_so_o_despachante_usa(tmp_path: Path) -> None:
    conteiner, raiz = _conteiner(**{"/iniciar/scripts": str(RAIZ / "scripts"), "/iniciar/politicas": str(POLITICAS)})
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
        for _ in range(2):  # idempotente
            rodada = c.exec(comando)
            assert rodada.exit_code == 0, rodada.output.decode()
        chave = api(url, raiz, "GET", f"transit/keys/{CHAVE_TERCEIROS}")["data"]
        assert (chave["derived"], chave["exportable"], chave["deletion_allowed"]) == (True, False, False)
        assert chave["latest_version"] == 1  # a segunda rodada nao girou
        lidos = {
            f"{papel}_{tipo}": c.exec(["cat", f"/openbao/file/segredos/cofre_{papel}_{tipo}"]).output.decode()
            for papel in PAPEIS
            for tipo in ("role_id", "secret_id")
        }

        def cofre(papel: str) -> Cofre:
            return Cofre(
                Parametros(APP_ENV="teste", VAULT_ADDR=url),
                role_id=lidos[f"{papel}_role_id"],
                secret_id=lidos[f"{papel}_secret_id"],
            )

        ctx = contexto("terceiro-a", "pessoa-1")
        async with cofre("despachante") as despachante, cofre("entrada") as entrada, cofre("rotinas") as rotinas:
            cifrado = await despachante.cifrar(CHAVE_TERCEIROS, b"chave-do-terceiro", contexto=ctx)
            assert await despachante.decifrar(CHAVE_TERCEIROS, cifrado, contexto=ctx) == b"chave-do-terceiro"
            # B9: o cifrado de terceiro nao abre com a chave dos proprios, nem no mesmo contexto.
            with pytest.raises(CifradoInvalido):
                await despachante.decifrar(CHAVE_PROPRIOS, cifrado, contexto=ctx)
            proprio = await despachante.cifrar(CHAVE_PROPRIOS, b"chave-propria", contexto=ctx)
            with pytest.raises(CifradoInvalido):
                await despachante.decifrar(CHAVE_TERCEIROS, proprio, contexto=ctx)
            for outro in (entrada, rotinas):
                with pytest.raises(AcessoNegado):
                    await outro.cifrar(CHAVE_TERCEIROS, b"x", contexto=ctx)
                with pytest.raises(AcessoNegado):
                    await outro.decifrar(CHAVE_TERCEIROS, cifrado, contexto=ctx)
