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
para construir a imagem (uma linha de erro de rede no passo que falhou ou no bloco final do log), a prova e pulada
numa maquina comum, mas falha no CI (variavel ``CI`` ligada), com o fim do log: no CI, ela nunca some em silencio. Um
aviso de rede num passo que deu certo (o pip que tentou de novo e instalou, o apt que so avisou), ou num passo que o
BuildKit cancelou porque outro falhou, nao conta. Qualquer outra falha de construcao (um certificado recusado no pip, no
uv ou no apt, um uv.lock ou um Dockerfile quebrado, ou um log sem passo que falhou nem bloco final) falha a prova, no CI
ou fora dele. O log junta a saida e o erro do Compose, nessa ordem: o Compose v5 escreve o progresso do BuildKit na
saida e o fim no erro, e o ``docker build`` escreve tudo no erro; os testes rodam os dois jeitos. O fixture ``projeto``
e a funcao ``montar_projeto``, sem nada no meio: os testes de construir ou pular rodam esse mesmo caminho com um projeto
falso.
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
# Erros de rede na construcao da imagem (curl, apt, pip, uv e o BuildKit), casados linha a linha, so nas linhas que
# dizem por que a construcao falhou (``linhas_da_falha``): as do passo que falhou e as do bloco final. Um aviso de rede
# num passo que deu certo nao explica a falha de outro passo. Nao contam as linhas em que o BuildKit repete o
# Dockerfile: o cabecalho de cada passo (``#6 [construcao 2/7] RUN ... pip install ...``, impresso em todo log, ate do
# cache), o passo que falhou (`` > [construcao 5/7] RUN ...``), o trecho do Dockerfile (``  11 | >>> RUN ...``) e o
# comando que falhou (``process "/bin/sh -c ..."``), no fim. O ``Failed to fetch`` sozinho tambem nao conta: o uv e o
# apt o escrevem em toda falha de download, ate num certificado recusado (``cause: invalid peer certificate:
# UnknownIssuer``, ``certificate verify failed``); numa falta de rede de verdade, eles escrevem tambem a causa
# (``failed to lookup address information``, ``Temporary failure resolving``), e e ela que conta. O Go do BuildKit
# escreve ``network is unreachable`` em minusculas.
SEM_REDE = re.compile(
    r"Could not resolve|Temporary failure in name resolution|Temporary failure resolving|Name or service not known"
    r"|(?i:network is unreachable)|failed to lookup address information"
    r"|Failed to establish a new connection|dial tcp: lookup \S+.*: no such host"
)
ECO_DO_DOCKERFILE = re.compile(r'^#\d+ \[[^\]]*\] |^\s*> \[[^\]]*\] |^\s*\d+ \| |process "/bin/sh -c ')
# No log do BuildKit (``--progress plain``, como o Compose escreve sem terminal), cada linha de um passo comeca por
# ``#N `` e o passo que falhou termina em ``#N ERROR: ...``. Os passos que ainda rodavam quando outro falhou terminam em
# ``#N CANCELED``: nao sao os que falharam. Depois de todos os passos vem o bloco final, sem prefixo: ``------``, o fim
# da saida do passo que falhou, ``------``, o trecho do Dockerfile e ``failed to solve: ...`` (so esta linha, quando a
# falha nao e de um passo com saida, como buscar a imagem de base). O Compose v5 escreve na saida os passos e o bloco
# final ate o segundo ``------``, e no erro o resto; o ``docker build`` escreve tudo no erro.
LINHA_DE_PASSO = re.compile(r"#(\d+) ")
PASSO_QUE_FALHOU = re.compile(r"#(\d+) ERROR:")
INICIO_DO_BLOCO_FINAL = re.compile(r"^\s*-{6,}\s*$|failed to solve")
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


def linhas_da_falha(log: str) -> list[str]:
    """As linhas do log da construcao que dizem por que ela falhou: as de cada passo que falhou (o ``#N`` que tem a
    linha ``#N ERROR:``) e as sem prefixo do bloco final (da primeira ``------`` ou ``failed to solve`` ate o fim).
    Ficam de fora os passos que deram certo (ou vieram do cache, ou que o BuildKit cancelou porque outro falhou) e as
    linhas sem prefixo antes do bloco final. Num log sem passo que falhou nem bloco final, nenhuma: nada ali diz que
    faltou rede."""
    linhas = log.splitlines()
    falharam = {m.group(1) for linha in linhas if (m := PASSO_QUE_FALHOU.match(linha))}
    escolhidas: list[str] = []
    no_bloco_final = False
    for linha in linhas:
        passo = LINHA_DE_PASSO.match(linha)
        if passo is not None:
            if passo.group(1) in falharam:
                escolhidas.append(linha)
            continue
        no_bloco_final = no_bloco_final or INICIO_DO_BLOCO_FINAL.search(linha) is not None
        if no_bloco_final:
            escolhidas.append(linha)
    return escolhidas


def linhas_sem_rede(log: str) -> list[str]:
    """As linhas de erro de rede que explicam a falha da construcao: so entre as ``linhas_da_falha`` (um aviso de rede
    num passo que deu certo, como o pip que tentou de novo e instalou, nao conta), fora as que so repetem o
    Dockerfile."""
    return [linha for linha in linhas_da_falha(log) if SEM_REDE.search(linha) and not ECO_DO_DOCKERFILE.search(linha)]


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
    """Constroi a imagem do gateway para a prova. Sem rede (alguma linha de erro de rede no passo que falhou ou no
    bloco final do log, ``linhas_sem_rede``): pula fora do CI e falha no CI. Qualquer outra falha de construcao falha a
    prova, no CI ou fora dele."""
    construcao = p.compose("build", "visitas", verificar=False, prazo=1800)
    if construcao.returncode == 0:
        return
    # O Compose v5 escreve o progresso do BuildKit na saida e o fim (o trecho do Dockerfile, ``failed to solve``) no
    # erro; o ``docker build`` escreve tudo no erro. Uma saida sem fim de linha nao gruda na primeira linha do erro (que
    # perderia o prefixo ``#N``).
    log = "\n".join((construcao.stdout, construcao.stderr))
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
PASSO_DO_UV = "RUN --mount=type=secret,id=ca_corporativa,required=false ...; uv sync --frozen --no-dev"
PROCESSO_DO_UV = 'process "/bin/sh -c uv sync --frozen --no-dev"'


def _lock_quebrado(n: int) -> list[str]:
    """O passo ``#n`` do uv que falha com o uv.lock quebrado, e o bloco final do BuildKit."""
    return [
        f"#{n} [construcao 5/7] {PASSO_DO_UV}",
        f"#{n} 0.071 error: Failed to parse `uv.lock`",
        f"#{n} 0.071   Caused by: TOML parse error at line 1, column 6",
        f"#{n} ERROR: {PROCESSO_DO_UV} did not complete successfully: exit code: 2",
        "------",
        f" > [construcao 5/7] {PASSO_DO_UV}:",
        "0.071 error: Failed to parse `uv.lock`",
        "0.071   Caused by: TOML parse error at line 1, column 6",
        "------",
        f"failed to solve: {PROCESSO_DO_UV} did not complete successfully: exit code: 2",
    ]


LOG_LOCK_QUEBRADO = "\n".join(
    [
        f"#6 [construcao 2/7] {PASSO_DO_PIP}",
        "#6 3.9 Successfully installed uv-0.13.9",
        "#6 4.0 [notice] To update, run: pip install --upgrade pip",
        "#6 DONE 4.4s",
        "",
        *_lock_quebrado(9),
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
APT_SEM_REDE = [
    "#5 [estagio-1 2/7] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     apt-get update  &&"
    " apt-get upgrade -y --no-install-recommends ...",
    "#5 0.3 Err:1 http://deb.debian.org/debian trixie InRelease",
    "#5 0.3   Temporary failure resolving 'deb.debian.org'",
    "#5 0.4 W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Temporary failure resolving"
    " 'deb.debian.org'",
    "#5 0.4 W: Some index files failed to download. They have been ignored, or old ones used instead.",
]
# O apt do trixie sem rede so avisa (``W:``) e o passo segue: os avisos ficam num passo que deu certo.
LOG_APT_QUE_SO_AVISOU = "\n".join([*APT_SEM_REDE, "#5 DONE 0.6s", ""])
# O apt que falha sem rede ao baixar um pacote (``E: Failed to fetch``, e sai com 100): a causa e do passo que falhou.
LOG_SEM_REDE_APT = "\n".join(
    [
        *APT_SEM_REDE,
        "#5 0.5 E: Failed to fetch http://deb.debian.org/debian/pool/main/o/openssl/libssl3t64_3.5.1-1_amd64.deb"
        "  Temporary failure resolving 'deb.debian.org'",
        "#5 0.5 E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?",
        '#5 ERROR: process "/bin/sh -c ... apt-get update && apt-get upgrade -y" did not complete successfully:'
        " exit code: 100",
        "",
    ]
)
# O pip 26 sem rede (as linhas do pip sao de verdade, ``docker run --network none``), com o prefixo do BuildKit no
# passo que falhou e o bloco final.
AVISO_DO_PIP = (
    "WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken"
    " by 'NameResolutionError(\"HTTPSConnection(host='pypi.org', port=443): Failed to resolve 'pypi.org' ([Errno -3]"
    " Temporary failure in name resolution)\")': /simple/uv/"
)
LOG_SEM_REDE_PIP = "\n".join(
    [
        f"#6 [construcao 2/7] {PASSO_DO_PIP}",
        f"#6 1.2 {AVISO_DO_PIP}",
        "#6 9.1 ERROR: Could not find a version that satisfies the requirement uv==0.13.9 (from versions: none)",
        "#6 9.1 ERROR: No matching distribution found for uv==0.13.9",
        f"#6 ERROR: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "------",
        f" > [construcao 2/7] {PASSO_DO_PIP}:",
        f"1.2 {AVISO_DO_PIP}",
        "9.1 ERROR: No matching distribution found for uv==0.13.9",
        "------",
        f"failed to solve: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "",
    ]
)
# Sem rede: o apt so avisa e o passo dele segue (deu certo), e o pip falha.
LOG_SEM_REDE = LOG_APT_QUE_SO_AVISOU + "\n" + LOG_SEM_REDE_PIP
# Trechos de dois logs de verdade (``docker build --no-cache --progress plain``, Docker 29.10), com as linhas longas
# cortadas. Sem rede (``--network none``): o pip falha no ``#7``, o apt do outro estagio e cancelado, e o Docker novo
# escreve ``ERROR: failed to build: failed to solve: ...`` no fim.
APT_DE_VERDADE = "#6 [stage-1 2/8] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     apt-get update ..."
LOG_SEM_REDE_DE_VERDADE = "\n".join(
    [
        APT_DE_VERDADE,
        "#6 2.305 Ign:1 http://deb.debian.org/debian trixie InRelease",
        "#6 ...",
        "",
        f"#7 [construcao 2/7] {PASSO_DO_PIP}",
        f"#7 1.668 {AVISO_DO_PIP}",
        "#7 9.176 ERROR: No matching distribution found for uv==0.13.9",
        f"#7 ERROR: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "",
        APT_DE_VERDADE,
        "#6 CANCELED",
        "------",
        f" > [construcao 2/7] {PASSO_DO_PIP}:",
        f"1.668 {AVISO_DO_PIP}",
        "9.176 ERROR: No matching distribution found for uv==0.13.9",
        "------",
        f"ERROR: failed to build: failed to solve: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "",
    ]
)
# Com rede para o pip, mas o apt atras de um proxy http que nao existe, e o uv.lock quebrado (numa copia, um ``RUN sleep
# 60`` antes do uv deixou o apt terminar): o apt avisou e o passo ``#6`` deu certo; o uv falhou depois, no ``#12``.
UV_DE_VERDADE = (
    "RUN --mount=type=secret,id=ca_corporativa,required=false ...;     uv sync --frozen --no-dev --no-install-project"
)
LOG_APT_SEM_PROXY_E_LOCK_DE_VERDADE = "\n".join(
    [
        APT_DE_VERDADE,
        "#6 7.120 Err:3 http://deb.debian.org/debian-security trixie-security InRelease",
        "#6 7.120   Could not resolve 'proxy-que-nao-existe.invalid'",
        "#6 7.328 W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Could not resolve"
        " 'proxy-que-nao-existe.invalid'",
        "#6 7.328 W: Some index files failed to download. They have been ignored, or old ones used instead.",
        "#6 7.391 0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.",
        "#6 DONE 9.7s",
        "",
        f"#12 [construcao 6/8] {UV_DE_VERDADE}",
        "#12 0.098 error: Failed to parse `uv.lock`",
        "#12 0.098   cause: TOML parse error at line 1, column 6",
        '#12 ERROR: process "/bin/sh -c ... uv sync --frozen --no-dev --no-install-project" did not complete'
        " successfully: exit code: 2",
        "------",
        f" > [construcao 6/8] {UV_DE_VERDADE}:",
        "0.098 error: Failed to parse `uv.lock`",
        "------",
        "ERROR: failed to build: failed to solve: process"
        ' "/bin/sh -c ... uv sync --frozen --no-dev --no-install-project" did not complete successfully: exit code: 2',
        "",
    ]
)
# Um passo que o BuildKit cancelou porque outro falhou (``#N CANCELED``) nao e o que falhou. Trechos de dois logs de
# verdade (``docker compose build``, Compose v5.7.1, ``build.network: none``, dois estagios em paralelo; o do pip e do
# verificador do R3br): o apt (ou o pip) de um estagio avisa sem rede e segue rodando, o outro estagio falha com um TOML
# quebrado (como um uv.lock quebrado), e o BuildKit cancela o primeiro. No Dockerfile do produto, o apt (``stage-1``)
# roda em paralelo com o pip e o uv: o apt que so avisou tende a aparecer assim, cancelado, e nao como ``DONE``.
RUN_DO_TOML = "RUN sleep 15 && printf 'isto nao e toml [[[\\n' > /tmp/uv.lock && python -c ..."
PROCESSO_DO_TOML = (
    "process \"/bin/sh -c sleep 15 && printf 'isto nao e toml [[[\\\\n' > /tmp/uv.lock && python -c ...\""
)
ERRO_DO_TOML = "tomllib.TOMLDecodeError: Expected '=' after a key in a key/value pair (at line 1, column 6)"


def _toml_quebrado(n: int, estagio: str, tempo: str, cancelados: list[str], linha: int) -> list[str]:
    """O passo ``#n`` que falha com o TOML quebrado, os passos que o BuildKit cancelou e o bloco final."""
    passo = f"[{estagio} 2/2] {RUN_DO_TOML}"
    return [
        f"#{n} {passo}",
        f"#{n} {tempo} Traceback (most recent call last):",
        f"#{n} {tempo} {ERRO_DO_TOML}",
        f"#{n} ERROR: {PROCESSO_DO_TOML} did not complete successfully: exit code: 1",
        "",
        *cancelados,
        "------",
        f" > {passo}:",
        f"{tempo} {ERRO_DO_TOML}",
        "------",
        f"Dockerfile:{linha}",
        "--------------------",
        f"   {linha} | >>> {RUN_DO_TOML}",
        "--------------------",
        f"failed to solve: {PROCESSO_DO_TOML} did not complete successfully: exit code: 1",
    ]


APT_CANCELADO = "#6 [stage-1 2/3] RUN apt-get update && sleep 40 && echo fim > /tmp/fim"
LOG_APT_CANCELADO_E_TOML_DE_VERDADE = "\n".join(
    [
        APT_CANCELADO,
        "#6 0.093 Ign:1 http://deb.debian.org/debian trixie InRelease",
        "#6 7.097 Err:1 http://deb.debian.org/debian trixie InRelease",
        "#6 7.097   Temporary failure resolving 'deb.debian.org'",
        "#6 7.111 W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Temporary failure resolving"
        " 'deb.debian.org'",
        "#6 7.111 W: Some index files failed to download. They have been ignored, or old ones used instead.",
        "#6 ...",
        "",
        *_toml_quebrado(7, "construcao", "15.28", [APT_CANCELADO, "#6 CANCELED"], 2),
        "",
    ]
)
PIP_CANCELADO = (
    "#6 [avisa 2/2] RUN pip download --no-cache-dir --retries 1 --timeout 2 -d /tmp/x uv==0.13.9 || true; sleep 40; ..."
)
LOG_PIP_CANCELADO_E_TOML_DE_VERDADE = "\n".join(
    [
        PIP_CANCELADO,
        f"#6 1.591 {AVISO_DO_PIP.replace('total=4', 'total=0')}",
        "#6 1.595 ERROR: No matching distribution found for uv==0.13.9",
        "#6 ...",
        "",
        *_toml_quebrado(7, "quebra", "15.35", [PIP_CANCELADO, "#6 CANCELED"], 4),
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
    # o mesmo pelo docker build do Docker 29, que abre o bloco final por "ERROR: failed to build:", sem ``------``
    "ERROR: failed to build: failed to solve: python:3.14-slim: failed to resolve source metadata for"
    ' docker.io/library/python:3.14-slim: failed to do request: Head "https://registry-1.docker.io/v2/library/python/'
    'manifests/3.14-slim": dial tcp 104.16.0.1:443: connect: network is unreachable',
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
# Um aviso de rede num passo que deu certo, e depois uma falha que nao e de rede (a prova do verificador, R3b): o pip
# que tentou de novo e instalou, ou o apt que so avisou, e o uv.lock quebrado ou um certificado recusado no uv. O pip
# tambem escreve ``ERROR:`` sem falhar (o aviso do resolvedor, e sai com 0): so o ``#N ERROR:`` do BuildKit diz que o
# passo falhou.
ERRO_DO_PIP_QUE_PASSA = (
    "ERROR: pip's dependency resolver does not currently take into account all the packages that are installed."
    " This behaviour is the source of the following dependency conflicts."
)
AVISOS_QUE_PASSARAM = {
    "pip_e_lock": LOG_LOCK_QUEBRADO.replace(
        "#6 3.9 Successfully installed uv-0.13.9", f"#6 1.2 {AVISO_DO_PIP}\n#6 3.9 Successfully installed uv-0.13.9"
    ),
    "pip_com_error_e_lock": LOG_LOCK_QUEBRADO.replace(
        "#6 3.9 Successfully installed uv-0.13.9",
        f"#6 1.2 {AVISO_DO_PIP}\n#6 3.8 {ERRO_DO_PIP_QUE_PASSA}\n#6 3.9 Successfully installed uv-0.13.9",
    ),
    "apt_e_lock": LOG_APT_QUE_SO_AVISOU + "\n" + LOG_LOCK_QUEBRADO,
    "apt_e_certificado_do_uv": LOG_APT_QUE_SO_AVISOU + "\n" + LOG_CERTIFICADO_UV,
    "apt_sem_proxy_e_lock_de_verdade": LOG_APT_SEM_PROXY_E_LOCK_DE_VERDADE,
    # o passo que avisou foi cancelado (``#6 CANCELED``) quando o outro falhou
    "apt_cancelado_e_toml_de_verdade": LOG_APT_CANCELADO_E_TOML_DE_VERDADE,
    "pip_cancelado_e_toml_de_verdade": LOG_PIP_CANCELADO_E_TOML_DE_VERDADE,
}
# O BuildKit repetindo, no passo que falhou e no bloco final, um Dockerfile que tivesse as mesmas palavras: nao e erro
# de rede.
ECOS = [
    "#6 [construcao 2/7] RUN curl -fsS https://x.exemplo || echo 'Could not resolve'",
    "#6 ERROR: process \"/bin/sh -c curl -fsS https://x.exemplo || echo 'Could not resolve'\" did not complete"
    " successfully: exit code: 1",
    "------",
    " > [construcao 2/7] RUN echo Network is unreachable:",
    "------",
    "   8 | >>>     apt-get update || echo 'Failed to fetch'",
    'failed to solve: process "/bin/sh -c echo Temporary failure in name resolution" did not complete successfully',
]


def _no_passo_que_falhou(linha: str) -> str:
    """Um log em que ``linha`` explica a falha: com o prefixo ``#N``, o passo ``#N`` termina em ``#N ERROR:`` (a linha
    do comando, que so repete o Dockerfile); sem prefixo (``failed to solve: ...``), ela ja abre o bloco final."""
    passo = LINHA_DE_PASSO.match(linha)
    if passo is None:
        return "\n".join([CABECALHOS, linha, ""])
    fim = f'#{passo.group(1)} ERROR: process "/bin/sh -c ..." did not complete successfully: exit code: 1'
    return "\n".join([CABECALHOS, linha, fim, ""])


def _num_passo_que_deu_certo(linha: str) -> str:
    """Um log em que ``linha`` e de um passo que deu certo (``#N DONE``), e a construcao falha depois noutro passo, o
    ``#40``, com o uv.lock quebrado."""
    passo = LINHA_DE_PASSO.match(linha)
    assert passo is not None
    assert passo.group(1) != "40"
    return "\n".join([CABECALHOS, linha, f"#{passo.group(1)} DONE 1.0s", "", *_lock_quebrado(40), ""])


@pytest.mark.parametrize("linha", ERROS_DE_REDE)
def test_linha_de_erro_de_rede_no_passo_que_falhou_conta_como_sem_rede(linha: str) -> None:
    assert linhas_sem_rede(_no_passo_que_falhou(linha)) == [linha]


@pytest.mark.parametrize("linha", [linha for linha in ERROS_DE_REDE if LINHA_DE_PASSO.match(linha)])
def test_linha_de_erro_de_rede_num_passo_que_deu_certo_nao_conta(linha: str) -> None:
    """O pip que tentou de novo e instalou, o apt que so avisou: o aviso nao explica a falha de outro passo."""
    log = _num_passo_que_deu_certo(linha)
    assert linha in log.splitlines()
    assert linhas_sem_rede(log) == []
    assert linhas_sem_rede(_no_passo_que_falhou(linha)) == [linha]  # a mesma linha, no passo que falhou, conta


def test_linhas_da_falha_sao_as_do_passo_que_falhou_e_as_do_bloco_final() -> None:
    """Nem os passos que deram certo (o ``#6`` do pip), nem as linhas sem prefixo antes do bloco final (a linha em
    branco entre os passos)."""
    assert linhas_da_falha(LOG_LOCK_QUEBRADO) == _lock_quebrado(9)
    assert linhas_da_falha(LOG_SEM_REDE) == LOG_SEM_REDE_PIP.splitlines()  # o apt que so avisou fica de fora
    # O passo que o BuildKit cancelou (``#6``, o apt que avisou) fica de fora, ate a repeticao do cabecalho dele.
    cancelado = LOG_APT_CANCELADO_E_TOML_DE_VERDADE.splitlines()
    assert "#6 CANCELED" in cancelado
    assert linhas_da_falha(LOG_APT_CANCELADO_E_TOML_DE_VERDADE) == [
        *(linha for linha in cancelado if linha.startswith("#7 ")),
        *cancelado[cancelado.index("------") :],
    ]
    # So a linha do fim, quando a falha nao e de um passo com saida (buscar a imagem de base).
    assert linhas_da_falha(CABECALHOS + ERROS_DE_REDE[-1] + "\n") == [ERROS_DE_REDE[-1]]


@pytest.mark.parametrize("log", ["error: Could not resolve host: pypi.org\n", CABECALHOS + ERROS_DE_REDE[0] + "\n"])
def test_log_sem_passo_que_falhou_nem_bloco_final_nao_conta_como_sem_rede(log: str) -> None:
    """Nada ali diz que foi a rede que faltou: a prova falha (nao pula), para nunca sumir em silencio."""
    assert linhas_da_falha(log) == []
    assert linhas_sem_rede(log) == []


@pytest.mark.parametrize("linha", SO_FAILED_TO_FETCH)
def test_failed_to_fetch_sozinho_nao_conta_como_sem_rede(linha: str) -> None:
    """O uv e o apt escrevem "Failed to fetch" em toda falha de download, ate num certificado recusado."""
    log = _no_passo_que_falhou(linha)
    assert linha in linhas_da_falha(log)
    assert linhas_sem_rede(log) == []


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


@pytest.mark.parametrize("log", [CABECALHOS, *OUTRAS_FALHAS, *AVISOS_QUE_PASSARAM.values(), "\n".join(ECOS)])
def test_falha_que_nao_e_de_rede_nao_conta_como_sem_rede(log: str) -> None:
    """O ``pip install`` do cabecalho de passo, um certificado recusado (no pip, no uv e no apt), um uv.lock quebrado,
    um aviso de rede num passo que deu certo e o eco do Dockerfile."""
    assert "pip install" in LOG_CERTIFICADO + LOG_LOCK_QUEBRADO  # o que o SEM_REDE antigo casava em todo log
    assert "Failed to fetch" in LOG_CERTIFICADO_UV + LOG_CERTIFICADO_APT  # o que o SEM_REDE da etapa 4 casava
    assert linhas_sem_rede(log) == []


def test_eco_do_dockerfile_no_passo_que_falhou_nao_conta() -> None:
    """As linhas que repetem o Dockerfile estao entre as que explicam a falha; e o filtro do eco que as tira."""
    log = "\n".join(ECOS)
    com_palavras_de_rede = [linha for linha in ECOS if SEM_REDE.search(linha)]
    assert len(com_palavras_de_rede) == 4
    assert set(com_palavras_de_rede) <= set(linhas_da_falha(log))
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


# De verdade (verificador do R3br): o Dockerfile e o Compose do repositorio, sem rede (``build.network: none``), pelo
# Compose v5.7.1 (Docker 29.10), com as linhas longas cortadas. Na saida, os passos e o bloco final ate o segundo
# ``------``; no erro, o resto, com uma linha em branco depois de cada uma. O apt avisou sem rede e foi cancelado
# (``#7``); o pip falhou sem rede (``#8``).
APT_DO_PRODUTO = "#7 [stage-1 2/8] RUN --mount=type=secret,id=ca_corporativa,required=false ...;     apt-get update ..."
SAIDA_DO_COMPOSE_SEM_REDE = "\n".join(
    [
        "#5 [construcao 1/7] FROM docker.io/library/python:3.14-slim@sha256:9aaa474097d0...",
        "#5 CACHED",
        "",
        APT_DO_PRODUTO,
        "#7 7.484 Err:3 http://deb.debian.org/debian-security trixie-security InRelease",
        "#7 7.484   Temporary failure resolving 'deb.debian.org'",
        "#7 7.542 W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Temporary failure resolving"
        " 'deb.debian.org'",
        "#7 7.542 W: Some index files failed to download. They have been ignored, or old ones used instead.",
        "#7 ...",
        "",
        f"#8 [construcao 2/7] {PASSO_DO_PIP}",
        f"#8 1.763 {AVISO_DO_PIP}",
        "#8 9.274 ERROR: No matching distribution found for uv==0.13.9",
        f"#8 ERROR: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
        "",
        APT_DO_PRODUTO,
        "#7 CANCELED",
        "------",
        f" > [construcao 2/7] {PASSO_DO_PIP}:",
        f"1.763 {AVISO_DO_PIP}",
        "9.274 ERROR: No matching distribution found for uv==0.13.9",
        "------",
        "",
    ]
)
ERRO_DO_COMPOSE_SEM_REDE = " Image mcp-claro:sala-falsa Building \n" + "".join(
    f"{linha}\n\n"
    for linha in [
        "Dockerfile:6",
        "--------------------",
        "   6 | >>> RUN --mount=type=secret,id=ca_corporativa,required=false \\",
        "   7 | >>>     if [ -s /run/secrets/ca_corporativa ]; then export PIP_CERT=...; fi; \\",
        "   8 | >>>     pip install --no-cache-dir uv==0.13.9",
        "   9 |     WORKDIR /app",
        "--------------------",
        f"failed to solve: {PROCESSO_DO_PIP} did not complete successfully: exit code: 1",
    ]
)
# Os dois jeitos de verdade de escrever o log do BuildKit: o do Compose v5 (o de cima) e o do ``docker build`` (Docker
# 29.10), que poe tudo no erro.
COMO_ESCREVEM = ("compose", "docker_build")


def _saidas(log: str, como: str) -> dict[str, str]:
    """A saida e o erro de um ``docker compose build`` que falhou com o ``log`` do BuildKit, escritos ``como``
    (``COMO_ESCREVEM``). No jeito do Compose, um log sem bloco final vai inteiro para a saida."""
    if como == "docker_build":
        return {"stdout": "", "stderr": log}
    linhas = log.splitlines()
    tracos = [i for i, linha in enumerate(linhas) if linha == "------"]
    corte = tracos[1] + 1 if len(tracos) > 1 else len(linhas)
    return {
        "stdout": "".join(f"{linha}\n" for linha in linhas[:corte]),
        "stderr": " Image mcp-claro:sala-falsa Building \n" + "".join(f"{linha}\n\n" for linha in linhas[corte:]),
    }


def _falso(log: str, como: str, returncode: int = 1) -> _ProjetoFalso:
    return _ProjetoFalso(returncode, **_saidas(log, como))


def test_saidas_no_jeito_do_compose_sao_as_de_verdade() -> None:
    """O jeito do Compose de ``_saidas`` (usado nos testes abaixo) da a saida e o erro de verdade."""
    erro = [linha for linha in ERRO_DO_COMPOSE_SEM_REDE.splitlines()[1:] if linha]
    log = SAIDA_DO_COMPOSE_SEM_REDE + "".join(f"{linha}\n" for linha in erro)  # como o docker build escreveria
    assert _saidas(log, "compose") == {"stdout": SAIDA_DO_COMPOSE_SEM_REDE, "stderr": ERRO_DO_COMPOSE_SEM_REDE}
    assert _saidas(log, "docker_build") == {"stdout": "", "stderr": log}


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


@pytest.mark.parametrize("como", COMO_ESCREVEM)
def test_construir_imagem_sem_rede_falha_no_ci(monkeypatch: pytest.MonkeyPatch, como: str) -> None:
    _no_ci_ou_nao(monkeypatch, "true")
    p = _falso(LOG_SEM_REDE, como)
    desfecho, texto = _desfecho(p)
    assert desfecho == "falhou"
    assert "nao construiu no CI" in texto
    assert f"#6 ERROR: {PROCESSO_DO_PIP}" in texto  # o passo que falhou, da saida ou do erro
    assert texto.endswith(p.resultado.stderr[-200:])  # o fim do log (o do erro) vai junto
    assert p.chamadas == [("build", "visitas")]


@pytest.mark.parametrize(("ci", "esperado"), [(None, "pulou"), ("true", "falhou")])
def test_construir_imagem_sem_rede_com_a_saida_e_o_erro_de_verdade_do_compose(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, esperado: str
) -> None:
    """De verdade (Compose v5.7.1, o Dockerfile e o Compose do repositorio, sem rede): as linhas de rede que contam (as
    do passo que falhou e as do bloco final) estao so na saida; o erro sozinho nao diz que faltou rede. O apt que avisou
    sem rede e foi cancelado (``#7``) nao conta."""
    assert not [linha for linha in ERRO_DO_COMPOSE_SEM_REDE.splitlines() if LINHA_DE_PASSO.match(linha)]
    assert linhas_sem_rede(ERRO_DO_COMPOSE_SEM_REDE) == []
    log = "\n".join((SAIDA_DO_COMPOSE_SEM_REDE, ERRO_DO_COMPOSE_SEM_REDE))
    assert linhas_sem_rede(log) == [f"#8 1.763 {AVISO_DO_PIP}", f"1.763 {AVISO_DO_PIP}"]
    _no_ci_ou_nao(monkeypatch, ci)
    p = _ProjetoFalso(1, stdout=SAIDA_DO_COMPOSE_SEM_REDE, stderr=ERRO_DO_COMPOSE_SEM_REDE)
    desfecho, texto = _desfecho(p)
    assert desfecho == esperado, texto
    if esperado == "falhou":
        assert texto.endswith(ERRO_DO_COMPOSE_SEM_REDE[-200:])  # o fim do log vai junto
        assert f"#8 1.763 {AVISO_DO_PIP}" in texto


# Sem rede para buscar a imagem de base: o passo do BuildKit que falhou e o bloco final, que termina so na linha do
# ``failed to solve``.
LOG_SEM_REDE_BASE = "\n".join(
    [
        "#2 [internal] load metadata for docker.io/library/python:3.14-slim",
        '#2 ERROR: failed to do request: Head "https://registry-1.docker.io/v2/library/python/manifests/3.14-slim":'
        " dial tcp: lookup registry-1.docker.io on 127.0.0.11:53: no such host",
        "------",
        " > [internal] load metadata for docker.io/library/python:3.14-slim:",
        "------",
        ERROS_DE_REDE[-1],
        "",
    ]
)


@pytest.mark.parametrize("como", COMO_ESCREVEM)
@pytest.mark.parametrize("ci", [None, "false"])
@pytest.mark.parametrize(
    "log", [LOG_SEM_REDE, LOG_SEM_REDE_DE_VERDADE, LOG_SEM_REDE_PIP, LOG_SEM_REDE_UV, LOG_SEM_REDE_BASE]
)
def test_construir_imagem_sem_rede_pula_fora_do_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, log: str, como: str
) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    desfecho, texto = _desfecho(_falso(log, como))
    assert (desfecho, texto) == ("pulou", "a imagem do gateway nao constroi aqui (sem rede para as dependencias)")


@pytest.mark.parametrize("como", COMO_ESCREVEM)
@pytest.mark.parametrize("ci", [None, "true"])
@pytest.mark.parametrize("log", AVISOS_QUE_PASSARAM.values(), ids=AVISOS_QUE_PASSARAM.keys())
def test_construir_imagem_com_aviso_de_rede_que_passou_e_outra_falha_falha_a_prova_dentro_e_fora_do_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, log: str, como: str
) -> None:
    """A prova do verificador (R3b): o pip tentou de novo e instalou (ou o apt so avisou, ou o passo que avisou foi
    cancelado), e depois o uv.lock quebrado (ou um certificado recusado no uv) derrubou a construcao. Nao foi a rede: a
    prova falha, nao pula."""
    avisos = [linha for linha in log.splitlines() if SEM_REDE.search(linha) and not ECO_DO_DOCKERFILE.search(linha)]
    assert avisos  # o log tem erro de rede, mas so num passo que nao e o que falhou
    _no_ci_ou_nao(monkeypatch, ci)
    desfecho, texto = _desfecho(_falso(log, como))
    assert desfecho == "nao_construiu", texto
    assert texto.startswith("a imagem nao construiu")
    assert "sem rede" not in texto


@pytest.mark.parametrize("como", COMO_ESCREVEM)
@pytest.mark.parametrize("ci", [None, "true"])
def test_construir_imagem_sem_passo_que_falhou_nem_bloco_final_falha_a_prova(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, como: str
) -> None:
    """Um log que nao e do BuildKit nao diz que foi a rede que faltou: a prova falha (nao pula)."""
    _no_ci_ou_nao(monkeypatch, ci)
    desfecho, texto = _desfecho(_falso("error: Could not resolve host: pypi.org\n", como))
    assert desfecho == "nao_construiu", texto


def test_construir_imagem_nao_gruda_uma_saida_sem_fim_de_linha_na_primeira_linha_do_erro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defesa: com o progresso no erro (como o ``docker build`` escreve) e uma saida sem fim de linha (aqui o `` Image
    ... Building ``, que o Compose v5 escreve no erro, e com fim de linha), a saida nao gruda na primeira linha do erro,
    que perderia o prefixo ``#2``. Nos dois jeitos de verdade (``COMO_ESCREVEM``), grudar nao mudaria o desfecho."""
    _no_ci_ou_nao(monkeypatch, None)
    erro = LOG_SEM_REDE_BASE.splitlines()[1] + "\n"  # so a linha #2 ERROR, com a causa
    desfecho, _ = _desfecho(_ProjetoFalso(1, stdout=" Image mcp-claro:sala-falsa Building", stderr=erro))
    assert desfecho == "pulou"


@pytest.mark.parametrize("como", COMO_ESCREVEM)
@pytest.mark.parametrize("ci", [None, "true"])
@pytest.mark.parametrize("log", OUTRAS_FALHAS)
def test_construir_imagem_com_outra_falha_falha_a_prova_dentro_e_fora_do_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, log: str, como: str
) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    p = _falso(log, como)
    desfecho, texto = _desfecho(p)
    assert desfecho == "nao_construiu"
    assert texto.startswith("a imagem nao construiu")
    assert texto.endswith(p.resultado.stderr[-200:])  # o fim do log (o do erro) vai junto
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
    falso = _falso(LOG_SEM_REDE, "compose")
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
    falso = _falso(LOG_SEM_REDE_UV, "compose")
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
    falso = _falso(LOG_CERTIFICADO_UV, "compose")
    desfecho, texto = _desfecho_do_fixture(falso)
    assert desfecho == "nao_construiu", texto
    assert texto.startswith("a imagem nao construiu")
    assert falso.chamadas == [("build", "visitas"), ("down", "-v", "--remove-orphans")]


@pytest.mark.parametrize("caso", ["pip_e_lock", "apt_cancelado_e_toml_de_verdade"])
@pytest.mark.parametrize("ci", [None, "true"])
def test_fixture_com_aviso_de_rede_que_passou_e_lock_quebrado_falha_dentro_e_fora_do_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None, caso: str
) -> None:
    _no_ci_ou_nao(monkeypatch, ci)
    monkeypatch.delenv("MCP_CLARO_IMAGEM", raising=False)
    falso = _falso(AVISOS_QUE_PASSARAM[caso], "compose")
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
    falso = _falso(LOG_SEM_REDE_UV, "compose")  # o build daria errado, mas nao roda
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
