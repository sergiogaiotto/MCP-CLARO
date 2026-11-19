"""O Compose unico tem o que o roteiro de restauracao chama (docs/restauracao.md).

O servico ``restaurar-logico`` (Caminho B e ensaio da copia logica) se perdeu quando o fragmento
``docker/backup/compose.backup.yml`` entrou no ``docker/compose.yml``: sobrou so o comentario, e o roteiro
falhava com "no such service". Aqui o ``docker compose config`` roda numa copia de ``docker/`` com segredos
falsos (como em ``test_continuidade_pgbackrest``), sem subir nada.
"""

from __future__ import annotations

import json
import re
import shlex
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

ROTEIRO = RAIZ / "docs" / "restauracao.md"


class Compose:
    def __init__(self, raiz: Path) -> None:
        self.raiz = raiz
        self.nome = f"mcpclarocfg{uuid.uuid4().hex[:8]}"

    def config(self, perfis: tuple[str, ...], *depois: str) -> str:
        comando = ["docker", "compose", "-p", self.nome, "-f", "docker/compose.yml"]
        comando += [a for perfil in perfis for a in ("--profile", perfil)]
        comando += ["config", *depois]
        r = subprocess.run(comando, cwd=self.raiz, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603
        if r.returncode != 0:
            raise AssertionError(f"{' '.join(comando)} saiu com {r.returncode}:\n{r.stderr[-3000:]}")
        return r.stdout

    def servicos(self, *perfis: str) -> set[str]:
        return set(self.config(perfis, "--services").split())

    def resolvido(self, *perfis: str) -> dict[str, Any]:
        dados: dict[str, Any] = json.loads(self.config(perfis, "--format", "json"))
        return dados


@pytest.fixture(scope="module")
def compose() -> Iterator[Compose]:
    if shutil.which("docker") is None:
        pytest.skip("sem Docker: o teste precisa do 'docker compose config'")
    if subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode != 0:  # noqa: S607
        pytest.skip("sem o plugin 'docker compose'")
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-compose-"))
    try:
        for nome in ("docker", "grafana", "scripts"):
            shutil.copytree(RAIZ / nome, raiz / nome, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Segredos falsos: arquivos vazios, so para o Compose carregar (nada sobe aqui).
        segredos = raiz / ".segredos"
        segredos.mkdir()
        textos = "".join(p.read_text(encoding="utf-8") for p in (raiz / "docker").rglob("compose*.yml"))
        for nome in set(re.findall(r"\.\./\.segredos/([A-Za-z0-9_.-]+)", textos)):
            (segredos / nome).touch()
        yield Compose(raiz)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)


def test_restaurar_logico_existe_so_no_perfil_de_restauracao(compose: Compose) -> None:
    assert "restaurar-logico" in compose.servicos("restauracao")
    # Nunca sobe sozinho: sem o perfil, o servico nao aparece.
    assert "restaurar-logico" not in compose.servicos()

    dados = compose.resolvido("restauracao")
    servico = dados["services"]["restaurar-logico"]
    assert servico["profiles"] == ["restauracao"]
    # A mesma imagem do backup (que traz restaurar-logico.sh), sem build proprio.
    assert servico["image"] == dados["services"]["backup"]["image"]
    assert "build" not in servico
    assert servico["entrypoint"] == ["/usr/local/bin/restaurar-logico.sh"]
    assert servico["read_only"] is True
    assert servico["cap_drop"] == ["ALL"]
    assert servico["restart"] == "no"
    # Segredos que o script le: o dono do servidor, a senha da cifra e a autoridade interna (TLS do banco).
    assert {s["source"] for s in servico["secrets"]} == {"backup_cifra", "db_migracao", "ca_interna.pem"}
    assert {s["source"] for s in servico["secrets"]} <= set(dados["secrets"])
    # A copia logica vem do volume do backup, so para leitura; so a rede interna (o banco).
    assert [(v["source"], v["target"], v.get("read_only")) for v in servico["volumes"]] == [
        ("backup_local", "/var/lib/mcp-claro-backup", True)
    ]
    assert set(servico["networks"]) == {"interna"}
    assert "backup_local" in dados["volumes"]


def test_comentario_do_servico_aparece_uma_vez() -> None:
    texto = (RAIZ / "docker" / "compose.yml").read_text(encoding="utf-8")
    assert texto.count("So no roteiro de restauracao (docs/restauracao.md), nunca sobe sozinho") == 1


def test_nada_cita_os_fragmentos_que_entraram_no_compose_unico() -> None:
    """``compose.backup.yml`` e ``compose.grafana.yml`` nao existem mais: tudo esta em ``docker/compose.yml``."""
    citam = [
        str(p.relative_to(RAIZ))
        for p in (RAIZ / "docker").rglob("*")
        if p.is_file() and re.search(r"compose\.(backup|grafana)\.yml", p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert citam == []


def _servicos_do_roteiro() -> set[str]:
    """Servicos que o roteiro chama com ``$MC`` (``docker compose -f docker/compose.yml``)."""
    texto = re.sub(r"\\\n\s*", " ", ROTEIRO.read_text(encoding="utf-8"))
    nomes: set[str] = set()
    for linha in texto.splitlines():
        if "$MC " not in linha:
            continue
        trecho = linha[linha.index("$MC ") :].split("`", 1)[0]
        partes = shlex.split(trecho)[1:]
        while partes[:1] == ["--profile"]:
            partes = partes[2:]
        comando, resto = partes[0], partes[1:]
        com_valor = {"-e", "--entrypoint", "--user", "-u", "-w", "--workdir"}
        sem_valor = {"--rm", "--no-deps", "-T", "-d", "--wait", "--detach"}
        alvos: list[str] = []
        i = 0
        while i < len(resto):
            if resto[i] in com_valor:
                i += 2
            elif resto[i] in sem_valor:
                i += 1
            else:
                alvos = resto[i:]
                break
        if comando in ("run", "exec"):
            nomes.add(alvos[0])
        elif comando in ("stop", "up", "start", "restart"):
            nomes.update(alvos)
    return nomes


def test_todo_servico_do_roteiro_de_restauracao_existe(compose: Compose) -> None:
    pedidos = _servicos_do_roteiro()
    assert {"restaurar-logico", "banco", "backup", "migrar", "despachante"} <= pedidos
    faltam = pedidos - compose.servicos("restauracao")
    assert faltam == set(), f"o roteiro chama servicos que o Compose nao tem: {sorted(faltam)}"


def test_quem_avisa_o_admin_tem_o_email_do_admin(compose: Compose) -> None:
    """O despachante (cofre trancado fecha a saida) e as rotinas avisam o ADMIN por e-mail: os dois recebem
    ``NOTIFY_ADMIN_EMAILS``. Sem ele, o aviso so iria ao log e aos webhooks."""
    servicos = compose.resolvido()["services"]
    for nome in ("despachante", "rotinas"):
        assert servicos[nome]["environment"].get("NOTIFY_ADMIN_EMAILS") == "admin@mcp-claro.localhost", nome
