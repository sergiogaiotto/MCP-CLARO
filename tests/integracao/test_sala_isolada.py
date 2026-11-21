"""Prova dinamica de isolamento da sala de visitas, a cada mudanca (critica M5; desenho 6.3, I7; critica M4).

Sobe, com os arquivos do repositorio, a sala de visitas (``visitas`` e ``visitas-muro``), o despachante, o banco e o
cofre, e prova de dentro da sala, conectando de verdade:

- sem alcance a banco:5432, cofre:8200, entrada:8443, rotinas:9444 e login:8080, nem pelo nome nem pelo IP;
- sem alcance a 169.254.169.254 (metadados das nuvens), a IPs privados da rede ``saida_visitas`` (o host e os
  vizinhos) nem ao lado do host da rede ``visitas``: a rota recusa na hora, nao e so falta de resposta;
- o despachante:9443 e alcancavel pela rede ``visitas``, mas sem certificado (ou com o da sala) o TLS recusa;
- a sala nao tem capacidade nenhuma, nem variavel de banco ou de cofre, e so o proprio certificado em segredo;

e, de dentro da rede ``interna``, que o certificado da sala e recusado pelo cofre e pela porta 9443 (M4), enquanto
os certificados certos entram. Por fim, o despachante fala com a sala pelo mTLS e a sala atende num filho.

Tambem de dentro da sala, um processo do mesmo usuario num dominio Landlock proprio (como um filho tomado por um
terceiro) nao abre o ``/proc`` (canos, memoria) do pai nem dos filhos prontos e, com o Landlock 6, nao manda sinal a
eles: prova, na imagem de verdade (seccomp padrao do Docker, sem capacidades), que o isolamento dos filhos vale.

O banco usa a imagem oficial do PostgreSQL com os scripts do repositorio montados, sem o pgBackRest (que so muda o
backup), para a prova nao depender do repositorio apt do PGDG. A imagem do gateway e construida aqui, com uma
etiqueta so desta rodada, salvo se ``MCP_CLARO_IMAGEM`` apontar uma imagem pronta (feita do mesmo codigo).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import RAIZ

pytestmark = [pytest.mark.integracao]

COPIAR = (
    "docker",
    "grafana",
    "scripts",
    "src",
    "migrations",
    "migrations_vigia",
    "alembic.ini",
    "alembic_vigia.ini",
    "pyproject.toml",
    "uv.lock",
    "README.md",
)
SEM_REDE = ("Could not resolve", "Failed to fetch", "Temporary failure", "Network is unreachable", "pip install")
IMAGEM_BANCO = "postgres:18.7@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc"

# So para esta prova: o banco sem o pgBackRest (a imagem do produto o instala do apt do PGDG).
BANCO_SEM_BACKUP = f"""
services:
  banco:
    image: {IMAGEM_BANCO}
    build: !reset null
    entrypoint: ["/usr/local/bin/com-tls.sh"]
    environment:
      PGBACKREST_LIGADO: desligado
    volumes:
      - ./postgres/com-tls.sh:/usr/local/bin/com-tls.sh:ro
      - ./postgres/preparar-tls.sh:/usr/local/bin/preparar-tls.sh:ro
      - ./postgres/iniciar/10-papeis.sh:/docker-entrypoint-initdb.d/10-papeis.sh:ro
      - ./postgres/pg_hba.conf:/etc/postgresql/pg_hba.conf:ro
"""

# Roda dentro da sala: tenta cada destino e diz o que aconteceu, sem levantar.
SONDA_DA_SALA = r"""
import errno, json, os, socket, ssl, sys
alvos = json.loads(sys.argv[1])
def tentar(host, porta):
    try:
        with socket.create_connection((host, porta), timeout=3):
            return "conectou"
    except socket.gaierror:
        return "sem_nome"
    except TimeoutError:
        return "prazo"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
def tls(host, porta, cert=None):
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    if cert:
        ctx.load_cert_chain("/run/secrets/visitas_cert.pem", "/run/secrets/visitas_key.pem")
    try:
        with socket.create_connection((host, porta), timeout=5) as s, ctx.wrap_socket(s) as t:
            t.sendall(b"GET /saude HTTP/1.1\r\nhost: despachante\r\nconnection: close\r\n\r\n")
            dados = t.recv(64)
            return "respondeu" if dados.startswith(b"HTTP/") else "fechou"
    except (ssl.SSLError, OSError):
        return "recusou"
saida = {f"{h}:{p}": tentar(h, p) for h, p in alvos}
saida["tls_sem_certificado"] = tls("despachante", 9443)
saida["tls_com_certificado_da_sala"] = tls("despachante", 9443, cert=True)
estado = open("/proc/self/status").read()
saida["capacidades"] = [l.split()[1] for l in estado.splitlines() if l.startswith(("CapEff", "CapPrm", "CapBnd"))]
saida["ambiente"] = sorted(
    n for n, v in os.environ.items() if n.startswith(("DATABASE_", "DB_", "VAULT_")) or (n.endswith("_FILE") and v)
)
saida["segredos"] = sorted(os.listdir("/run/secrets"))
print(json.dumps(saida))
"""

# Roda na rede interna (imagem do gateway): o certificado da sala no cofre e na porta 9443, e os certos.
SONDA_INTERNA = r"""
import json, socket, ssl
def tentar(host, porta, cert, chave, caminho):
    ctx = ssl.create_default_context(cafile="/run/secrets/ca_interna.pem")
    ctx.load_cert_chain(cert, chave)
    try:
        with socket.create_connection((host, porta), timeout=5) as s, ctx.wrap_socket(s, server_hostname=host) as t:
            t.sendall(f"GET {caminho} HTTP/1.1\r\nhost: {host}\r\nconnection: close\r\n\r\n".encode())
            return "respondeu" if t.recv(64).startswith(b"HTTP/") else "fechou"
    except (ssl.SSLError, OSError):
        return "recusou"
sala = ("/sala/visitas_cert.pem", "/sala/visitas_key.pem")
print(json.dumps({
    "cofre_com_a_sala": tentar("cofre", 8200, *sala, "/v1/sys/health"),
    "cofre_com_o_despachante": tentar("cofre", 8200, "/sala/despachante_cert.pem", "/sala/despachante_key.pem",
                                      "/v1/sys/health"),
    "despachante_com_a_sala": tentar("despachante", 9443, *sala, "/saude"),
    "despachante_com_a_entrada": tentar("despachante", 9443, "/sala/entrada_cliente_cert.pem",
                                        "/sala/entrada_cliente_key.pem", "/saude"),
}))
"""

# Roda dentro da sala como um filho tomado: no proprio dominio Landlock, tenta alcancar o pai e os filhos prontos.
SONDA_DOS_FILHOS = r"""
import errno, json, os
from mcp_claro.visitas import filho
def ler(caminho):
    try:
        with open(caminho, "rb") as f:
            return f.read()
    except OSError:
        return b""
def ppid(pid):
    linhas = ler(f"/proc/{pid}/status").decode().splitlines()
    return next((int(l.split()[1]) for l in linhas if l.startswith("PPid:")), -1)
eu = os.getpid()
vivos = [int(n) for n in os.listdir("/proc") if n.isdigit() and int(n) != eu]
[pai] = [n for n in vivos if b"mcp-claro\x00visitas" in ler(f"/proc/{n}/cmdline")]
filhos = [n for n in vivos if ppid(n) == pai and b"spawn_main" in ler(f"/proc/{n}/cmdline")]
versao = filho.versao_landlock()
dominio = filho.dominio_proprio()
abertos, recusas, sinais = [], set(), set()
for alvo in [pai, *filhos]:
    for caminho in (f"/proc/{alvo}/mem", *(f"/proc/{alvo}/fd/{n}" for n in range(32))):
        try:
            fd = os.open(caminho, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        except OSError as e:
            recusas.add(errno.errorcode.get(e.errno, str(e.errno)))
            continue
        os.close(fd)
        abertos.append(caminho)
    try:
        os.kill(alvo, 0)
        sinais.add("aceito")
    except OSError as e:
        sinais.add(errno.errorcode.get(e.errno, str(e.errno)))
print(json.dumps({"landlock": versao, "dominio": dominio, "filhos": len(filhos), "abertos": abertos,
                  "recusas": sorted(recusas), "sinais": sorted(sinais)}))
"""

# Roda no despachante: o salto de verdade ate a sala (mTLS) e um filho que confere o endereco do terceiro.
SALTO = r"""
import anyio, json
from mcp_claro.config.settings import Parametros
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.visitas.tls import contexto_do_despachante
async def principal():
    p = Parametros()
    sala = ClienteSala(p.VISITS_INTERNAL_URL, tls=contexto_do_despachante(p))
    async def chave():
        return "chave-de-teste"
    s = await sala.sondar("https://203.0.113.10/mcp", chave, prazo=5)
    print(json.dumps({"desfecho": s.desfecho, "excecao": s.excecao}))
anyio.run(principal)
"""


class Projeto:
    def __init__(self, raiz: Path, nome: str, imagem: str) -> None:
        self.raiz = raiz
        self.nome = nome
        self.imagem = imagem

    def compose(self, *args: str, verificar: bool = True, prazo: float = 600) -> subprocess.CompletedProcess[str]:
        comando = ["docker", "compose", "-p", self.nome, "-f", "docker/compose.yml", "-f", "docker/compose.sala.yml"]
        r = subprocess.run(  # noqa: S603
            [*comando, *args],
            cwd=self.raiz,
            capture_output=True,
            text=True,
            timeout=prazo,
            check=False,
            env=os.environ | {"MCP_CLARO_IMAGEM": self.imagem},
        )
        if verificar and r.returncode != 0:
            raise AssertionError(f"{' '.join(args)} saiu com {r.returncode}:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
        return r

    def json(self, *args: str) -> Any:
        return json.loads(self.compose(*args).stdout.strip().splitlines()[-1])

    def ip(self, servico: str, rede: str) -> str:
        conteiner = self.compose("ps", "-q", servico).stdout.strip()
        r = subprocess.run(  # noqa: S603
            ["docker", "inspect", conteiner, "--format", "{{json .NetworkSettings.Networks}}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        redes: dict[str, Any] = json.loads(r.stdout)
        return str(redes[f"{self.nome}_{rede}"]["IPAddress"])

    def rede(self, rede: str) -> dict[str, Any]:
        r = subprocess.run(  # noqa: S603
            ["docker", "network", "inspect", f"{self.nome}_{rede}", "--format", "{{json .IPAM.Config}}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(json.loads(r.stdout)[0])


@pytest.fixture(scope="module")
def projeto() -> Iterator[Projeto]:
    if shutil.which("docker") is None:
        pytest.skip("sem Docker")
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-sala-"))
    pronta = os.environ.get("MCP_CLARO_IMAGEM")
    p = Projeto(raiz, f"mcpclarosala{uuid.uuid4().hex[:8]}", pronta or f"mcp-claro:sala-{uuid.uuid4().hex[:8]}")
    try:
        for nome in COPIAR:
            origem = RAIZ / nome
            if origem.is_dir():
                shutil.copytree(origem, raiz / nome, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(origem, raiz / nome)
        subprocess.run(["bash", "scripts/gerar-segredos-dev.sh"], cwd=raiz, check=True, capture_output=True)  # noqa: S607
        segredos = raiz / ".segredos"
        texto = (raiz / "docker" / "compose.yml").read_text(encoding="utf-8")
        for nome in set(re.findall(r"\.\./\.segredos/([A-Za-z0-9_.-]+)", texto)):
            (segredos / nome).touch(exist_ok=True)
        for arquivo in segredos.iterdir():
            arquivo.chmod(0o644)
        (raiz / "docker" / "compose.sala.yml").write_text(BANCO_SEM_BACKUP, encoding="utf-8")
        if not pronta:
            construcao = p.compose("build", "visitas", verificar=False, prazo=1800)
            if construcao.returncode != 0:
                texto = construcao.stdout + construcao.stderr
                if any(sinal in texto for sinal in SEM_REDE):
                    pytest.skip("a imagem do gateway nao constroi aqui (sem rede para as dependencias)")
                raise AssertionError(f"a imagem nao construiu:\n{texto[-4000:]}")
        p.compose("up", "-d", "--wait", "despachante", "visitas", prazo=900)
        yield p
    finally:
        p.compose("down", "-v", "--remove-orphans", verificar=False, prazo=300)
        if not pronta:
            subprocess.run(["docker", "image", "rm", "-f", p.imagem], capture_output=True, check=False)  # noqa: S603, S607
        shutil.rmtree(raiz, ignore_errors=True)


ROTA_RECUSADA = {"EHOSTUNREACH", "ENETUNREACH", "EACCES", "EPERM"}


def _mais_um(rede: str, deslocamento: int) -> str:
    base, _ = rede.split("/")
    partes = [int(x) for x in base.split(".")]
    numero = (partes[0] << 24 | partes[1] << 16 | partes[2] << 8 | partes[3]) + deslocamento
    return ".".join(str((numero >> s) & 255) for s in (24, 16, 8, 0))


def test_de_dentro_da_sala_nada_alem_do_despachante_e_da_internet(projeto: Projeto) -> None:
    p = projeto
    saida_visitas = p.rede("saida_visitas")
    visitas = p.rede("visitas")
    por_ip = {
        "banco": (p.ip("banco", "interna"), 5432),
        "cofre": (p.ip("cofre", "interna"), 8200),
        "metadados": ("169.254.169.254", 80),
        "host_da_saida": (saida_visitas["Gateway"], 2375),
        "vizinho_da_saida": (_mais_um(saida_visitas["Subnet"], 200), 443),
        "host_da_visitas": (visitas.get("Gateway") or _mais_um(visitas["Subnet"], 1), 22),
    }
    por_nome = [("banco", 5432), ("cofre", 8200), ("entrada", 8443), ("rotinas", 9444), ("login", 8080)]
    alvos = [*por_nome, *por_ip.values(), ("despachante", 9443)]
    vistos = p.json("exec", "-T", "visitas", "python", "-c", SONDA_DA_SALA, json.dumps(alvos))
    for host, porta in por_nome:
        assert vistos[f"{host}:{porta}"] == "sem_nome", (host, vistos)
    for nome, (host, porta) in por_ip.items():
        # A rota recusa na hora: nao e so o destino calado.
        assert vistos[f"{host}:{porta}"] in ROTA_RECUSADA, (nome, host, vistos)
    # O despachante e alcancavel (a sala responde a ele), mas o TLS dele recusa quem nao tem o certificado certo.
    assert vistos["despachante:9443"] == "conectou"
    assert vistos["tls_sem_certificado"] in ("recusou", "fechou")
    assert vistos["tls_com_certificado_da_sala"] in ("recusou", "fechou")
    assert {int(c, 16) for c in vistos["capacidades"]} == {0}
    assert vistos["ambiente"] == [
        "INTERNAL_CA_FILE",
        "INTERNAL_CERT_FILE",
        "INTERNAL_CLIENT_CA_FILE",
        "INTERNAL_KEY_FILE",
    ]
    assert vistos["segredos"] == ["ca_clientes_visitas.pem", "ca_visitas.pem", "visitas_cert.pem", "visitas_key.pem"]


def test_rotas_da_sala_so_tem_o_despachante_e_enderecos_publicos(projeto: Projeto) -> None:
    rotas = projeto.compose("exec", "-T", "visitas-muro", "ip", "route").stdout
    assert re.search(r"^default via \S+ dev \S+ onlink", rotas, re.MULTILINE), rotas
    assert re.search(r"^unreachable 169\.254\.0\.0/16", rotas, re.MULTILINE), rotas
    publico = projeto.compose("exec", "-T", "visitas-muro", "ip", "route", "get", "1.1.1.1").stdout
    assert " via " in publico, publico  # endereco publico: sai pela rede saida_visitas


def test_certificado_da_sala_e_recusado_pelo_cofre_e_pela_porta_9443(projeto: Projeto) -> None:
    segredos = projeto.raiz / ".segredos"
    usados = ("visitas", "despachante", "entrada_cliente")
    montagens = [
        a
        for nome in usados
        for parte in ("cert", "key")
        for a in ("-v", f"{segredos / f'{nome}_{parte}.pem'}:/sala/{nome}_{parte}.pem:ro")
    ]
    vistos = projeto.json(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        *montagens,
        "--entrypoint",
        "python",
        "migrar",
        "-c",
        SONDA_INTERNA,
    )
    assert vistos == {
        "cofre_com_a_sala": "recusou",
        "cofre_com_o_despachante": "respondeu",
        "despachante_com_a_sala": "recusou",
        "despachante_com_a_entrada": "respondeu",
    }


def test_despachante_fala_com_a_sala_pelo_mtls_e_a_sala_confere_o_endereco(projeto: Projeto) -> None:
    vistos = projeto.json("exec", "-T", "despachante", "python", "-c", SALTO)
    # A sala atendeu num filho: o endereco reservado (TEST-NET-3) nao e publico, e nada saiu.
    assert vistos == {"desfecho": "inalcancavel", "excecao": "EnderecoRecusado"}
    logs = projeto.compose("logs", "--no-color", "visitas").stdout
    assert '"rota": "sondar"' in logs
    assert '"desfecho": "filho"' in logs
    assert "203.0.113.10" not in logs
    assert "chave-de-teste" not in logs


def test_um_filho_tomado_nao_alcanca_o_pai_nem_os_outros_filhos(projeto: Projeto) -> None:
    vistos = projeto.json("exec", "-T", "visitas", "python", "-c", SONDA_DOS_FILHOS)
    assert vistos["filhos"] >= 1, vistos  # os filhos prontos, ja isolados, a espera de uma chamada
    # O pai e os filhos nao sao depuraveis e, com Landlock, ficam fora do dominio de quem tenta: nada abre.
    assert (vistos["abertos"], vistos["recusas"]) == ([], ["EACCES"]), vistos
    logs = projeto.compose("logs", "--no-color", "visitas").stdout
    assert re.search(rf'"landlock": {vistos["landlock"]}\b', logs), logs[-2000:]
    if vistos["landlock"] >= 1:
        assert vistos["dominio"] is True, vistos  # o seccomp padrao do Docker deixa o filho entrar no dominio
    if vistos["landlock"] >= 6:
        assert vistos["sinais"] == ["EPERM"], vistos  # nem derrubar o pai ou um irmao
