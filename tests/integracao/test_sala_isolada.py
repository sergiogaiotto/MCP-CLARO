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
eles: prova, na imagem de verdade (seccomp padrao do Docker, sem capacidades), que o isolamento dos filhos vale. E o
pai parte por ``python -m mcp_claro visitas``: um filho do spawn nao roda de novo o programa principal do pai antes
de se isolar (nem importa ``mcp_claro.__main__``).

O banco usa a imagem oficial do PostgreSQL com os scripts do repositorio montados, sem o pgBackRest (que so muda o
backup), para a prova nao depender do repositorio apt do PGDG. A imagem do gateway e construida aqui, com uma
etiqueta so desta rodada, salvo se ``MCP_CLARO_IMAGEM`` apontar uma imagem pronta (feita do mesmo codigo). Sem rede
para construir a imagem (uma linha de erro de rede no log), a prova e pulada numa maquina comum, mas falha no CI
(variavel ``CI`` ligada), com o fim do log: no CI, ela nunca some em silencio. Qualquer outra falha de construcao (um
certificado recusado no pip, no uv ou no apt, um uv.lock ou um Dockerfile quebrado) falha a prova, no CI ou fora
dele. O fixture ``projeto`` e a funcao ``montar_projeto``, sem nada no meio: os testes de construir ou pular rodam esse
mesmo caminho com um projeto falso.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NoReturn

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
# Erros de rede na construcao da imagem (curl, apt, pip, uv e o BuildKit), casados linha a linha. Nao contam as
# linhas em que o BuildKit repete o Dockerfile: o cabecalho de cada passo (``#6 [construcao 2/7] RUN ... pip install
# ...``, impresso em todo log, ate do cache), o passo que falhou (`` > [construcao 5/7] RUN ...``), o trecho do
# Dockerfile (``  11 | >>> RUN ...``) e o comando que falhou (``process "/bin/sh -c ..."``), no fim. O ``Failed to
# fetch`` sozinho tambem nao conta: o uv e o apt o escrevem em toda falha de download, ate num certificado recusado
# (``cause: invalid peer certificate: UnknownIssuer``, ``certificate verify failed``); numa falta de rede de verdade,
# eles escrevem tambem a causa (``failed to lookup address information``, ``Temporary failure resolving``), e e ela
# que conta. O Go do BuildKit escreve ``network is unreachable`` em minusculas.
SEM_REDE = re.compile(
    r"Could not resolve|Temporary failure in name resolution|Temporary failure resolving|Name or service not known"
    r"|(?i:network is unreachable)|failed to lookup address information"
    r"|Failed to establish a new connection|dial tcp: lookup \S+.*: no such host"
)
ECO_DO_DOCKERFILE = re.compile(r'^#\d+ \[[^\]]*\] |^\s*> \[[^\]]*\] |^\s*\d+ \| |process "/bin/sh -c ')
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

# Roda na rede interna (imagem do gateway): o certificado da sala no cofre e na porta 9443, e os certos. No TLS 1.3, o
# servidor confere o certificado do cliente depois que o cliente ja terminou o aperto de mao; ao recusar, ele manda um
# alerta e fecha, mas as vezes fecha sem que o alerta chegue (cerca de 1 em 10 rodadas, medido). Com
# ``suppress_ragged_eofs=False``, esse fim sem ``close_notify`` levanta erro de TLS (recusou) em vez de parecer um
# servidor que aceitou e fechou.
SONDA_INTERNA = r"""
import json, socket, ssl
def tentar(host, porta, cert, chave, caminho):
    ctx = ssl.create_default_context(cafile="/run/secrets/ca_interna.pem")
    ctx.load_cert_chain(cert, chave)
    try:
        with socket.create_connection((host, porta), timeout=5) as s, ctx.wrap_socket(
            s, server_hostname=host, suppress_ragged_eofs=False
        ) as t:
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
[pai] = [n for n in vivos if ler(f"/proc/{n}/cmdline") == b"/app/.venv/bin/python\x00-m\x00mcp_claro\x00visitas\x00"]
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

# Roda dentro da sala: como o pai de verdade partiu (/proc/1/cmdline) e o que um filho do spawn carrega com o
# programa principal montado como o Python o monta para essa partida, e para a partida antiga pelo script mcp-claro.
# O pai de verdade nao deixa ler a memoria dele nem a dos filhos: a sonda inicia um filho por spawn do mesmo jeito,
# com o Python e o multiprocessing da imagem.
SONDA_DA_PARTIDA = r"""
import importlib.util, json, multiprocessing, os, sys
def argv(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [p.decode() for p in f.read().split(b"\0")[:-1]]
    except OSError:
        return []
def ppid(pid):
    try:
        linhas = open(f"/proc/{pid}/status").read().splitlines()
    except OSError:
        return -1
    return next((int(l.split()[1]) for l in linhas if l.startswith("PPid:")), -1)
CODIGO = (
    "import json, sys\n"
    "json.dump({'main_do_gateway': 'mcp_claro.__main__' in sys.modules, 'settings': 'mcp_claro.config.settings' in"
    " sys.modules, 'modulos': len(sys.modules)}, open(destino, 'w'))\n"
)
def filho_de(partida, destino):
    principal = sys.modules["__main__"]
    if partida[1:2] == ["-m"]:
        spec = importlib.util.find_spec(partida[2])
        if spec.submodule_search_locations is not None:
            spec = importlib.util.find_spec(partida[2] + ".__main__")
        principal.__spec__ = spec
    else:
        principal.__spec__ = None
        principal.__file__ = partida[1]
    processo = multiprocessing.get_context("spawn").Process(target=exec, args=(CODIGO, {"destino": destino}))
    processo.start()
    processo.join(120)
    with open(destino) as f:
        return json.load(f)
pai = argv(1)
filhos = [n for n in os.listdir("/proc") if n.isdigit() and ppid(n) == 1 and "spawn_main" in " ".join(argv(n))]
print(json.dumps({
    "pai": pai,
    "filhos_do_pai": len(filhos),
    "como_o_pai": filho_de(pai, "/tmp/sonda_como_o_pai.json"),
    "como_o_script": filho_de(["/app/.venv/bin/python", "/app/.venv/bin/mcp-claro", "visitas"],
                              "/tmp/sonda_como_o_script.json"),
}))
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


def _no_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in {"", "0", "false", "no"}


def linhas_sem_rede(log: str) -> list[str]:
    """As linhas do log da construcao que sao erro de rede (fora as que so repetem o Dockerfile)."""
    return [linha for linha in log.splitlines() if SEM_REDE.search(linha) and not ECO_DO_DOCKERFILE.search(linha)]


def _sem_rede_para_construir(log: str) -> NoReturn:
    """A imagem nao construiu por falta de rede: pula a prova fora do CI; no CI, falha com o fim do log."""
    if _no_ci():
        pytest.fail(f"a imagem do gateway nao construiu no CI (sem rede para as dependencias):\n{log[-4000:]}")
    pytest.skip("a imagem do gateway nao constroi aqui (sem rede para as dependencias)")


class Projeto:
    def __init__(self, raiz: Path, nome: str, imagem: str) -> None:
        self.raiz = raiz
        self.nome = nome
        self.imagem = imagem

    def preparar(self) -> None:
        """Copia para a raiz o que a imagem e o Compose usam e gera os segredos de desenvolvimento."""
        for nome in COPIAR:
            origem = RAIZ / nome
            if origem.is_dir():
                shutil.copytree(origem, self.raiz / nome, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(origem, self.raiz / nome)
        subprocess.run(["bash", "scripts/gerar-segredos-dev.sh"], cwd=self.raiz, check=True, capture_output=True)  # noqa: S607
        segredos = self.raiz / ".segredos"
        texto = (self.raiz / "docker" / "compose.yml").read_text(encoding="utf-8")
        for nome in set(re.findall(r"\.\./\.segredos/([A-Za-z0-9_.-]+)", texto)):
            (segredos / nome).touch(exist_ok=True)
        for arquivo in segredos.iterdir():
            arquivo.chmod(0o644)
        (self.raiz / "docker" / "compose.sala.yml").write_text(BANCO_SEM_BACKUP, encoding="utf-8")

    def apagar_imagem(self) -> None:
        subprocess.run(["docker", "image", "rm", "-f", self.imagem], capture_output=True, check=False)  # noqa: S603, S607

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


def construir_imagem(p: Projeto) -> None:
    """Constroi a imagem do gateway para a prova. Sem rede (alguma linha de erro de rede no log): pula fora do CI e
    falha no CI. Qualquer outra falha de construcao falha a prova, no CI ou fora dele."""
    construcao = p.compose("build", "visitas", verificar=False, prazo=1800)
    if construcao.returncode == 0:
        return
    log = construcao.stdout + construcao.stderr
    if linhas_sem_rede(log):
        _sem_rede_para_construir(log)
    raise AssertionError(
        "a imagem nao construiu (para usar uma imagem pronta, feita do mesmo codigo: MCP_CLARO_IMAGEM=<nome:etiqueta>;"
        f" atras de um proxy que troca o certificado, construa com --secret id=ca_corporativa):\n{log[-4000:]}"
    )


def _ha_docker() -> bool:
    return shutil.which("docker") is not None


def montar_projeto(
    *, fabrica: Callable[[Path, str, str], Projeto] = Projeto, ha_docker: Callable[[], bool] = _ha_docker
) -> Iterator[Projeto]:
    """Monta o projeto da prova, constroi a imagem (salvo se ``MCP_CLARO_IMAGEM`` apontar uma pronta) e sobe a sala e
    o despachante; no fim, derruba tudo. E o proprio fixture ``projeto`` (logo abaixo), sem nada no meio: os testes de
    construir ou pular rodam este caminho com um projeto falso (``fabrica``), e um pulo posto aqui no lugar de
    ``construir_imagem`` faz esses testes falharem."""
    if not ha_docker():
        pytest.skip("sem Docker")
    pronta = os.environ.get("MCP_CLARO_IMAGEM")
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-sala-"))
    p = fabrica(raiz, f"mcpclarosala{uuid.uuid4().hex[:8]}", pronta or f"mcp-claro:sala-{uuid.uuid4().hex[:8]}")
    try:
        p.preparar()
        if not pronta:
            construir_imagem(p)
        p.compose("up", "-d", "--wait", "despachante", "visitas", prazo=900)
        yield p
    finally:
        p.compose("down", "-v", "--remove-orphans", verificar=False, prazo=300)
        if not pronta:
            p.apagar_imagem()
        shutil.rmtree(raiz, ignore_errors=True)


projeto = pytest.fixture(scope="module", name="projeto")(montar_projeto)


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


def test_sala_parte_por_python_m_e_o_filho_nao_carrega_o_main_do_gateway(projeto: Projeto) -> None:
    """O pai da sala parte por ``python -m mcp_claro visitas`` (o entrypoint do Compose): um filho do spawn nao roda
    de novo o programa principal do pai, nem importa ``mcp_claro.__main__`` e o que ele importa (os parametros, o
    log); pela partida antiga, o script ``mcp-claro``, importaria."""
    vistos = projeto.json("exec", "-T", "visitas", "python", "-c", SONDA_DA_PARTIDA)
    assert vistos["pai"] == ["/app/.venv/bin/python", "-m", "mcp_claro", "visitas"], vistos
    assert vistos["filhos_do_pai"] >= 1, vistos  # os prontos, filhos do pai de verdade
    como_o_pai, como_o_script = vistos["como_o_pai"], vistos["como_o_script"]
    assert (como_o_pai["main_do_gateway"], como_o_pai["settings"]) == (False, False), vistos
    assert (como_o_script["main_do_gateway"], como_o_script["settings"]) == (True, True), vistos
    assert como_o_pai["modulos"] < como_o_script["modulos"], vistos


@pytest.mark.parametrize("ci", ["true", "1", "yes"])
def test_sem_rede_para_construir_a_imagem_falha_no_ci(monkeypatch: pytest.MonkeyPatch, ci: str) -> None:
    monkeypatch.setenv("CI", ci)
    log = "passo 1\n" * 1000 + "error: Could not resolve host: pypi.org\n"
    # Pegar tambem o pulo: se a prova so pulasse, este teste tambem pularia em silencio.
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as falha:
        _sem_rede_para_construir(log)
    assert falha.type is pytest.fail.Exception
    assert "nao construiu no CI" in str(falha.value)
    assert str(falha.value).endswith("Could not resolve host: pypi.org\n")  # o fim do log vai junto
    assert len(str(falha.value)) < 4200


@pytest.mark.parametrize("ci", [None, "", "false", "0"])
def test_sem_rede_para_construir_a_imagem_pula_fora_do_ci(monkeypatch: pytest.MonkeyPatch, ci: str | None) -> None:
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    with pytest.raises(pytest.skip.Exception, match="sem rede para as dependencias"):
        _sem_rede_para_construir("Could not resolve host")


# --- construir ou pular ----------------------------------------------------------------------------------------------

# Trechos de logs de verdade do BuildKit (docker compose build), com as linhas longas cortadas. Em todo log, o
# cabecalho de cada passo repete o comando do Dockerfile (com o ``pip install``), mesmo quando o passo vem do cache.
PASSO_DO_PIP = "RUN --mount=type=secret,id=ca_corporativa,required=false ...;     pip install --no-cache-dir uv==0.13.9"
PROCESSO_DO_PIP = (
    'process "/bin/sh -c if [ -s /run/secrets/ca_corporativa ]; ...;     pip install --no-cache-dir uv==0.13.9"'
)
CERTIFICADO = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain"
CABECALHOS = "\n".join(
    [f"#6 [construcao 2/7] {PASSO_DO_PIP}", "#6 CACHED", "", "#7 [construcao 3/7] WORKDIR /app", "#7 CACHED", ""]
)
# Atras de um proxy que troca o certificado: ha rede, mas o pip nao confia no certificado (nao e falta de rede).
LOG_CERTIFICADO = "\n".join(
    [
        f"#9 [construcao 2/7] {PASSO_DO_PIP}",
        "#9 1.793 WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after"
        f" connection broken by 'SSLError(SSLCertVerificationError(1, '{CERTIFICADO} (_ssl.c:1085)'))': /simple/uv/",
        "#9 9.379 Could not fetch URL https://pypi.org/simple/uv/: There was a problem confirming the ssl certificate:"
        f" {CERTIFICADO} (_ssl.c:1085) - skipping",
        "#9 9.384 ERROR: Could not find a version that satisfies the requirement uv==0.13.9 (from versions: none)",
        "#9 9.384 ERROR: No matching distribution found for uv==0.13.9",
        f"#9 ERROR: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "------",
        f" > [construcao 2/7] {PASSO_DO_PIP}:",
        "9.384 ERROR: No matching distribution found for uv==0.13.9",
        "------",
        "Dockerfile:6",
        "--------------------",
        "   6 | >>> RUN --mount=type=secret,id=ca_corporativa,required=false \\",
        "   7 | >>>     if [ -s /run/secrets/ca_corporativa ]; then export PIP_CERT=...; fi; \\",
        "   8 | >>>     pip install --no-cache-dir uv==0.13.9",
        "   9 |     WORKDIR /app",
        "--------------------",
        f"failed to solve: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "",
    ]
)
# O uv.lock quebrado: o passo do uv falha sem tocar a rede. O passo do pip rodou (fora do cache), e o aviso do pip
# tambem diz "pip install".
LOG_LOCK_QUEBRADO = "\n".join(
    [
        f"#6 [construcao 2/7] {PASSO_DO_PIP}",
        "#6 3.9 Successfully installed uv-0.13.9",
        "#6 4.0 [notice] To update, run: pip install --upgrade pip",
        "#6 DONE 4.4s",
        "",
        "#9 [construcao 5/7] RUN --mount=type=secret,id=ca_corporativa,required=false ...; uv sync --frozen --no-dev",
        "#9 0.071 error: Failed to parse `uv.lock`",
        "#9 0.071   Caused by: TOML parse error at line 1, column 6",
        '#9 ERROR: process "/bin/sh -c uv sync --frozen --no-dev" did not complete successfully: exit code: 2',
        'failed to solve: process "/bin/sh -c uv sync --frozen --no-dev" did not complete successfully: exit code: 2',
        "",
    ]
)
# Os mesmos programas sem rede (``docker run --network none``, python:3.14-slim, uv 0.13.9 e apt do trixie), com o
# prefixo do BuildKit: a linha que conta e a da causa.
URL_DO_WHEEL = "https://files.pythonhosted.org/packages/62/a1/3d68/jinja2-3.1.6-py3-none-any.whl"
LOG_SEM_REDE_UV = "\n".join(
    [
        "#10 [construcao 4/7] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     uv sync --frozen"
        " --no-dev --no-install-project",
        "#10 0.2 Using CPython 3.14.10 interpreter at: /usr/local/bin/python3",
        "#10 4.3 error: Failed to download `jinja2==3.1.6`",
        "#10 4.3   cause: Request failed after 3 retries in 4.1s",
        f"#10 4.3   cause: Failed to fetch: {URL_DO_WHEEL}",
        f"#10 4.3   cause: error sending request for url ({URL_DO_WHEEL})",
        "#10 4.3   cause: client error (Connect)",
        "#10 4.3   cause: dns error",
        "#10 4.3   cause: error resolving DNS",
        "#10 4.3   cause: failed to lookup address information: Temporary failure in name resolution",
        '#10 ERROR: process "/bin/sh -c ... uv sync --frozen --no-dev --no-install-project" did not complete'
        " successfully: exit code: 2",
        "",
    ]
)
LOG_SEM_REDE_APT = "\n".join(
    [
        "#5 [estagio-1 2/7] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     apt-get update  &&"
        " apt-get upgrade -y --no-install-recommends ...",
        "#5 0.3 Err:1 http://deb.debian.org/debian trixie InRelease",
        "#5 0.3   Temporary failure resolving 'deb.debian.org'",
        "#5 0.4 W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Temporary failure resolving"
        " 'deb.debian.org'",
        "#5 0.4 W: Some index files failed to download. They have been ignored, or old ones used instead.",
        "",
    ]
)
ERROS_DE_REDE = [
    "#9 2.1 curl: (6) Could not resolve host: pypi.org",
    "#9 9.3 WARNING: Retrying ... 'NewConnectionError('<...>: Failed to establish a new connection: [Errno -3]"
    " Temporary failure in name resolution')': /simple/uv/",
    # pip 26 sem rede (de verdade): NameResolutionError
    "#9 9.3 WARNING: Retrying (Retry(total=0, ...)) after connection broken by 'NameResolutionError(\"HTTPSConnection("
    "host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno -3] Temporary failure in name resolution)\")'",
    "#12 3.0 E: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Temporary failure resolving"
    " 'deb.debian.org'",
    "#12 0.3   Temporary failure resolving 'deb.debian.org'",
    "#10 4.3   cause: failed to lookup address information: Temporary failure in name resolution",
    "#9 1.0   Caused by: dns error: failed to lookup address information: Name or service not known",
    "#10 4.3   cause: tcp connect error: Network is unreachable (os error 101)",
    "#9 0.5 connect: Network is unreachable",
    # o Go do BuildKit, em minusculas
    'failed to solve: failed to do request: Head "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim":'
    " dial tcp 104.16.0.1:443: connect: network is unreachable",
    "failed to solve: failed to resolve source metadata for docker.io/library/python:3.14-slim: failed to do request:"
    ' Head "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim": dial tcp: lookup registry-1.docker.io'
    " on 127.0.0.11:53: no such host",
]
# O "Failed to fetch" sozinho: o uv e o apt o escrevem em toda falha de download; quem diz se e rede e a causa.
SO_FAILED_TO_FETCH = [
    "#9 1.0 error: Failed to fetch: `https://pypi.org/simple/uv/`",
    f"#11 1.2   cause: Failed to fetch: {URL_DO_WHEEL}",
    "#12 2.0 W: Failed to fetch https://deb.debian.org/debian/dists/trixie/InRelease  SSL connection failed:"
    " error:0A000086:SSL routines::certificate verify failed / Success [IP: 151.101.2.132 443]",
    "#12 2.0 E: Failed to fetch https://deb.debian.org/debian/dists/trixie/InRelease  Certificate verification"
    " failed: The certificate is NOT trusted.",
]
# Certificado recusado no passo do uv (de verdade: uv 0.13.9 com SSL_CERT_FILE de outra autoridade, como a construcao
# faz com um --secret id=ca_corporativa errado; a camada do pip veio do cache). Ha rede: nao pode pular.
LOG_CERTIFICADO_UV = "\n".join(
    [
        CABECALHOS,
        "#11 [construcao 4/7] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     uv sync --frozen"
        " --no-dev --no-install-project",
        "#11 0.1 warning: The `UV_NATIVE_TLS` environment variable is deprecated and will be removed in a future"
        " release. Use `UV_SYSTEM_CERTS` instead.",
        "#11 1.2 error: Failed to download `idna==3.20`",
        "#11 1.2   cause: Failed to fetch: https://files.pythonhosted.org/packages/58/a2/bb08/idna-3.20-py3-none-any.whl",
        "#11 1.2   cause: error sending request for url (https://files.pythonhosted.org/packages/58/a2/bb08/idna-3.20"
        "-py3-none-any.whl)",
        "#11 1.2   cause: client error (Connect)",
        "#11 1.2   cause: invalid peer certificate: UnknownIssuer",
        "#11 1.2 hint: `idna` (v3.20) was included because `mcp-claro` (v0.1.0) depends on `httpx2` (v2.16.2) which"
        " depends on `idna`",
        '#11 ERROR: process "/bin/sh -c ... uv sync --frozen --no-dev --no-install-project" did not complete'
        " successfully: exit code: 2",
        "",
    ]
)
# Certificado recusado no apt: as linhas do apt do trixie sao de verdade (contra um servidor https com certificado de
# outra autoridade, so o endereco trocado); a ultima e como o apt mais antigo (GnuTLS) escreve.
LOG_CERTIFICADO_APT = "\n".join(
    [
        "#5 [estagio-1 2/7] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     apt-get update  &&"
        " apt-get upgrade -y --no-install-recommends ...",
        "#5 0.3 Err:1 https://deb.debian.org/debian trixie InRelease",
        "#5 0.3   SSL connection failed: error:0A000086:SSL routines::certificate verify failed / Success"
        " [IP: 151.101.2.132 443]",
        "#5 0.4 W: Failed to fetch https://deb.debian.org/debian/dists/trixie/InRelease  SSL connection failed:"
        " error:0A000086:SSL routines::certificate verify failed / Success [IP: 151.101.2.132 443]",
        "#5 0.4 W: Some index files failed to download. They have been ignored, or old ones used instead.",
        "#5 0.4 E: Failed to fetch https://deb.debian.org/debian/dists/trixie/InRelease  Certificate verification"
        " failed: The certificate is NOT trusted.",
        '#5 ERROR: process "/bin/sh -c ... apt-get update && apt-get upgrade -y" did not complete successfully:'
        " exit code: 100",
        "",
    ]
)
OUTRAS_FALHAS = [LOG_CERTIFICADO, LOG_CERTIFICADO_UV, LOG_CERTIFICADO_APT, LOG_LOCK_QUEBRADO]
# O BuildKit repetindo um Dockerfile que tivesse as mesmas palavras: nao e erro de rede.
ECOS = [
    "#6 [construcao 2/7] RUN curl -fsS https://x.exemplo || echo 'Could not resolve'",
    " > [construcao 2/7] RUN echo Network is unreachable:",
    "   8 | >>>     apt-get update || echo 'Failed to fetch'",
    'failed to solve: process "/bin/sh -c echo Temporary failure in name resolution" did not complete successfully',
]


@pytest.mark.parametrize("linha", ERROS_DE_REDE)
def test_linha_de_erro_de_rede_conta_como_sem_rede(linha: str) -> None:
    assert linhas_sem_rede(CABECALHOS + linha + "\n") == [linha]


@pytest.mark.parametrize("linha", SO_FAILED_TO_FETCH)
def test_failed_to_fetch_sozinho_nao_conta_como_sem_rede(linha: str) -> None:
    """O uv e o apt escrevem "Failed to fetch" em toda falha de download, ate num certificado recusado."""
    assert linhas_sem_rede(CABECALHOS + linha + "\n") == []


@pytest.mark.parametrize(
    ("log", "causa"),
    [
        (
            LOG_SEM_REDE_UV,
            "#10 4.3   cause: failed to lookup address information: Temporary failure in name resolution",
        ),
        (LOG_SEM_REDE_APT, "#5 0.3   Temporary failure resolving 'deb.debian.org'"),
    ],
)
def test_log_sem_rede_do_uv_e_do_apt_conta_pela_causa(log: str, causa: str) -> None:
    """Sem rede de verdade, o uv e o apt escrevem a causa, e e ela que conta (nao o "Failed to fetch")."""
    assert "Failed to fetch" in log
    linhas = linhas_sem_rede(log)
    assert causa in linhas
    assert not any("Failed to fetch" in linha and "Temporary failure" not in linha for linha in linhas), linhas


@pytest.mark.parametrize("log", [CABECALHOS, *OUTRAS_FALHAS, "\n".join(ECOS)])
def test_falha_que_nao_e_de_rede_nao_conta_como_sem_rede(log: str) -> None:
    """O ``pip install`` do cabecalho de passo, um certificado recusado (no pip, no uv e no apt), um uv.lock quebrado
    e o eco do Dockerfile."""
    assert "pip install" in LOG_CERTIFICADO + LOG_LOCK_QUEBRADO  # o que o SEM_REDE antigo casava em todo log
    assert "Failed to fetch" in LOG_CERTIFICADO_UV + LOG_CERTIFICADO_APT  # o que o SEM_REDE da etapa 4 casava
    assert linhas_sem_rede(log) == []


class _ProjetoFalso(Projeto):
    """Um projeto cujo ``docker compose build`` devolve sempre o mesmo resultado, sem rodar nada; os outros comandos
    do Compose (``up``, ``down``) dao certo. Guarda o que foi pedido."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__(Path(tempfile.gettempdir()) / "nao-existe", "falso", "mcp-claro:falsa")
        self.resultado = subprocess.CompletedProcess(["docker", "compose"], returncode, stdout, stderr)
        self.chamadas: list[tuple[str, ...]] = []
        self.preparado = False
        self.imagem_apagada = False

    def preparar(self) -> None:
        self.preparado = True

    def apagar_imagem(self) -> None:
        self.imagem_apagada = True

    def compose(self, *args: str, verificar: bool = True, prazo: float = 600) -> subprocess.CompletedProcess[str]:
        self.chamadas.append(args)
        if args[0] != "build":
            return subprocess.CompletedProcess(["docker", "compose", *args], 0, "", "")
        assert verificar is False  # quem confere o resultado e construir_imagem
        return self.resultado


def _no_ci_ou_nao(monkeypatch: pytest.MonkeyPatch, ci: str | None) -> None:
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)


def _desfecho(p: Projeto) -> tuple[str, str]:
    """O que ``construir_imagem`` fez: segue, pulou, falhou (``pytest.fail``) ou nao construiu (AssertionError).

    Pega tambem o pulo e a falha do pytest: se ``construir_imagem`` so pulasse, um teste que esperasse outra coisa
    pularia junto, em silencio."""
    try:
        construir_imagem(p)
    except pytest.skip.Exception as pulo:
        return "pulou", str(pulo)
    except pytest.fail.Exception as falha:
        return "falhou", str(falha)
    except AssertionError as erro:
        return "nao_construiu", str(erro)
    return "segue", ""


def test_construir_imagem_sem_rede_falha_no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci_ou_nao(monkeypatch, "true")
    p = _ProjetoFalso(1, stdout=CABECALHOS, stderr="#9 2.1 curl: (6) Could not resolve host: pypi.org\n")
    desfecho, texto = _desfecho(p)
    assert desfecho == "falhou"
    assert "nao construiu no CI" in texto
    assert texto.endswith("Could not resolve host: pypi.org\n")  # o fim do log vai junto
    assert p.chamadas == [("build", "visitas")]


@pytest.mark.parametrize("ci", [None, "false"])
@pytest.mark.parametrize("log", ["Could not resolve host", LOG_SEM_REDE_UV])
def test_construir_imagem_sem_rede_pula_fora_do_ci(monkeypatch: pytest.MonkeyPatch, ci: str | None, log: str) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    desfecho, texto = _desfecho(_ProjetoFalso(1, stderr=log))
    assert (desfecho, texto) == ("pulou", "a imagem do gateway nao constroi aqui (sem rede para as dependencias)")


@pytest.mark.parametrize("ci", [None, "true"])
@pytest.mark.parametrize("log", OUTRAS_FALHAS)
def test_construir_imagem_com_outra_falha_falha_a_prova_dentro_e_fora_do_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, log: str
) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    desfecho, texto = _desfecho(_ProjetoFalso(1, stderr=log))
    assert desfecho == "nao_construiu"
    assert texto.startswith("a imagem nao construiu")
    assert texto.endswith(log[-200:])  # o fim do log vai junto
    assert "MCP_CLARO_IMAGEM" in texto


def test_construir_imagem_que_construiu_segue(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci_ou_nao(monkeypatch, "true")
    p = _ProjetoFalso(0, stdout=CABECALHOS + ERROS_DE_REDE[0])  # aviso de rede num passo que se recuperou
    assert _desfecho(p) == ("segue", "")
    assert p.chamadas == [("build", "visitas")]


# --- o fixture: montar, construir ou pular, subir, derrubar ---------------------------------------------------------


def _desfecho_do_fixture(falso: _ProjetoFalso, *, ha_docker: bool = True) -> tuple[str, str]:
    """Roda o fixture ``projeto`` (``montar_projeto``) com o projeto falso, como o pytest roda: ate entregar o projeto
    e, se entregou, ate o fim (derrubar). Pega tambem o pulo e a falha do pytest, como ``_desfecho``."""
    criados: list[tuple[Path, str, str]] = []

    def fabrica(raiz: Path, nome: str, imagem: str) -> Projeto:
        criados.append((raiz, nome, imagem))
        assert raiz.is_dir()
        falso.raiz, falso.nome, falso.imagem = raiz, nome, imagem
        return falso

    passos = montar_projeto(fabrica=fabrica, ha_docker=lambda: ha_docker)
    try:
        entregue = next(passos)
    except pytest.skip.Exception as pulo:
        desfecho = ("pulou", str(pulo))
    except pytest.fail.Exception as falha:
        desfecho = ("falhou", str(falha))
    except AssertionError as erro:
        desfecho = ("nao_construiu", str(erro))
    else:
        assert entregue is falso
        with pytest.raises(StopIteration):
            next(passos)  # o fim do modulo: o pytest pede o resto do fixture
        desfecho = ("segue", "")
    assert len(criados) <= 1
    if criados:
        assert not criados[0][0].exists()  # a raiz temporaria foi apagada no fim, desse ou daquele jeito
    return desfecho


def test_fixture_sem_rede_falha_no_ci_e_derruba(monkeypatch: pytest.MonkeyPatch) -> None:
    """O fixture constroi a imagem por ``construir_imagem``: no CI, sem rede, a prova falha (nao pula)."""
    _no_ci_ou_nao(monkeypatch, "true")
    monkeypatch.delenv("MCP_CLARO_IMAGEM", raising=False)
    falso = _ProjetoFalso(1, stderr=LOG_SEM_REDE_UV)
    desfecho, texto = _desfecho_do_fixture(falso)
    assert desfecho == "falhou", texto
    assert "nao construiu no CI" in texto
    assert falso.preparado
    assert falso.chamadas == [("build", "visitas"), ("down", "-v", "--remove-orphans")]
    assert falso.imagem.startswith("mcp-claro:sala-")
    assert falso.imagem_apagada


@pytest.mark.parametrize("ci", [None, "false"])
def test_fixture_sem_rede_pula_fora_do_ci(monkeypatch: pytest.MonkeyPatch, ci: str | None) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    monkeypatch.delenv("MCP_CLARO_IMAGEM", raising=False)
    falso = _ProjetoFalso(1, stderr=LOG_SEM_REDE_UV)
    assert _desfecho_do_fixture(falso) == (
        "pulou",
        "a imagem do gateway nao constroi aqui (sem rede para as dependencias)",
    )
    assert falso.chamadas == [("build", "visitas"), ("down", "-v", "--remove-orphans")]


@pytest.mark.parametrize("ci", [None, "true"])
def test_fixture_com_certificado_recusado_falha_dentro_e_fora_do_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None
) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    monkeypatch.delenv("MCP_CLARO_IMAGEM", raising=False)
    falso = _ProjetoFalso(1, stderr=LOG_CERTIFICADO_UV)
    desfecho, texto = _desfecho_do_fixture(falso)
    assert desfecho == "nao_construiu", texto
    assert texto.startswith("a imagem nao construiu")
    assert falso.chamadas == [("build", "visitas"), ("down", "-v", "--remove-orphans")]


def test_fixture_que_construiu_sobe_entrega_e_derruba(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci_ou_nao(monkeypatch, "true")
    monkeypatch.delenv("MCP_CLARO_IMAGEM", raising=False)
    falso = _ProjetoFalso(0)
    assert _desfecho_do_fixture(falso) == ("segue", "")
    assert falso.preparado
    assert falso.chamadas == [
        ("build", "visitas"),
        ("up", "-d", "--wait", "despachante", "visitas"),
        ("down", "-v", "--remove-orphans"),
    ]
    assert falso.imagem_apagada  # a imagem desta rodada sai no fim


def test_fixture_com_imagem_pronta_nao_constroi_nem_apaga_a_imagem(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci_ou_nao(monkeypatch, "true")
    monkeypatch.setenv("MCP_CLARO_IMAGEM", "mcp-claro:pronta")
    falso = _ProjetoFalso(1, stderr=LOG_SEM_REDE_UV)  # o build daria errado, mas nao roda
    assert _desfecho_do_fixture(falso) == ("segue", "")
    assert falso.imagem == "mcp-claro:pronta"
    assert falso.chamadas == [("up", "-d", "--wait", "despachante", "visitas"), ("down", "-v", "--remove-orphans")]
    assert not falso.imagem_apagada


def test_fixture_sem_docker_pula_sem_montar_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ci_ou_nao(monkeypatch, "true")
    falso = _ProjetoFalso(0)
    assert _desfecho_do_fixture(falso, ha_docker=False) == ("pulou", "sem Docker")
    assert (falso.chamadas, falso.preparado) == ([], False)


def test_o_fixture_projeto_e_montar_projeto() -> None:
    """Sem nada no meio: o que os testes acima provam vale para o fixture de verdade."""
    assert inspect.unwrap(projeto) is montar_projeto  # o pytest guarda a funcao em __wrapped__ (functools)
