"""pgBackRest de verdade: copia completa, recuperacao para um ponto no tempo e conferencia na partida.

Sobe so o banco e o backup com os arquivos do repositorio (docker/compose.yml mais o fragmento
docker/backup/compose.backup.yml), numa copia temporaria do projeto com segredos de teste, e segue o roteiro
de docs/restauracao.md: espera a primeira copia completa, grava marcas no extrato antes e depois de um
momento, restaura para aquele momento com docker/postgres/restaurar.sh e confere que (1) a marca de depois
sumiu, (2) o banco abriu numa nova linha do tempo e (3) a conferencia de continuidade fecha a porta de saida
e pausa os aplicativos.

Lento: roda na rodada diaria do CI. Pula quando as imagens nao constroem por falta do repositorio apt do
PGDG (alguns ambientes de desenvolvimento nao alcancam apt.postgresql.org).
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.conftest import RAIZ

pytestmark = [pytest.mark.integracao, pytest.mark.lento]

COPIAR = ("docker", "grafana", "scripts", "src", "migrations", "alembic.ini", "pyproject.toml", "uv.lock", "README.md")
SEM_REDE = ("Could not resolve", "Failed to fetch", "Temporary failure resolving", "Unable to locate package")


class Projeto:
    def __init__(self, raiz: Path, nome: str) -> None:
        self.raiz = raiz
        self.nome = nome

    def compose(self, *args: str, verificar: bool = True, prazo: float = 600) -> subprocess.CompletedProcess[str]:
        comando = [
            "docker",
            "compose",
            "-p",
            self.nome,
            "-f",
            "docker/compose.yml",
            "-f",
            "docker/backup/compose.backup.yml",
            *args,
        ]
        r = subprocess.run(comando, cwd=self.raiz, capture_output=True, text=True, timeout=prazo, check=False)  # noqa: S603
        if verificar and r.returncode != 0:
            raise AssertionError(f"{' '.join(args)} saiu com {r.returncode}:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
        return r

    def psql(self, sql: str) -> str:
        senha = (self.raiz / ".segredos" / "db_migracao").read_text(encoding="utf-8").strip()
        r = self.compose(
            "exec", "-T", "-e", f"PGPASSWORD={senha}", "banco",
            "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-U", "mcp_claro_migracao", "-d", "mcp_claro", "-c", sql,
        )  # fmt: skip
        return r.stdout.strip()

    def continuidade(self, *args: str) -> str:
        """A linha de comando da continuidade, na imagem do gateway (servico migrar, dono do esquema)."""
        r = self.compose(
            "run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "migrar", "-m", "mcp_claro.continuidade", *args
        )  # fmt: skip
        return r.stdout


def esperar(condicao: Callable[[], bool], prazo: float, descricao: str) -> None:
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        if condicao():
            return
        time.sleep(5)
    raise AssertionError(f"esperei {prazo:.0f} s e nao aconteceu: {descricao}")


@pytest.fixture(scope="module")
def projeto() -> Iterator[Projeto]:
    if shutil.which("docker") is None:
        pytest.skip("sem Docker")
    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-pgbackrest-"))
    for nome in COPIAR:
        origem = RAIZ / nome
        if origem.is_dir():
            shutil.copytree(origem, raiz / nome, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(origem, raiz / nome)
    subprocess.run(["bash", "scripts/gerar-segredos-dev.sh"], cwd=raiz, check=True, capture_output=True)  # noqa: S607
    segredos = raiz / ".segredos"
    for nome in ("backup_cifra", "db_backup"):
        if not (segredos / nome).exists():
            (segredos / nome).write_text(secrets.token_hex(24), encoding="utf-8")
    # Segredos de pecas que este teste nao sobe (cofre, telas): arquivos vazios, so para o Compose carregar.
    textos = "".join(p.read_text(encoding="utf-8") for p in (raiz / "docker").rglob("compose*.yml"))
    for nome in set(re.findall(r"\.\./\.segredos/([A-Za-z0-9_.-]+)", textos)):
        (segredos / nome).touch(exist_ok=True)
    for arquivo in segredos.iterdir():
        arquivo.chmod(0o644)

    p = Projeto(raiz, f"mcpclaropgbr{uuid.uuid4().hex[:8]}")
    construcao = p.compose("build", "banco", "backup", "migrar", verificar=False, prazo=1800)
    if construcao.returncode != 0:
        texto = construcao.stdout + construcao.stderr
        shutil.rmtree(raiz, ignore_errors=True)
        if any(sinal in texto for sinal in SEM_REDE):
            pytest.skip("as imagens com pgBackRest nao constroem aqui: sem o repositorio apt do PGDG")
        raise AssertionError(f"as imagens nao construiram:\n{texto[-4000:]}")
    try:
        p.compose("up", "-d", "--wait", "banco", prazo=300)
        p.compose("run", "--rm", "migrar", prazo=300)
        p.compose("up", "-d", "backup", prazo=300)
        yield p
    finally:
        p.compose("down", "-v", "--remove-orphans", verificar=False, prazo=300)
        shutil.rmtree(raiz, ignore_errors=True)


def test_copia_completa_recuperacao_no_tempo_e_conferencia(projeto: Projeto) -> None:
    p = projeto
    # 1. A primeira copia completa sai sozinha, assim que o banco responde.
    esperar(
        lambda: "full backup" in p.compose("exec", "-T", "backup", "backup.sh", "info", verificar=False).stdout,
        900,
        "a primeira copia completa",
    )
    assert "desfecho: primeira_partida" in p.continuidade("conferir", "--vigia", "0")

    # 2. Marcas no extrato antes e depois do momento escolhido para a recuperacao.
    p.psql("SELECT extrato_registrar('teste', 'partida', 'antes do ponto')")
    p.psql("INSERT INTO aplicativos (client_id, nome, familia) VALUES ('app-pitr', 'Aplicativo PITR', 'teste')")
    time.sleep(2)
    ponto = p.psql("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00'")
    time.sleep(2)
    p.psql("SELECT extrato_registrar('teste', 'partida', 'depois do ponto')")
    # O check troca o WAL e espera ele chegar ao repositorio.
    p.compose("exec", "-T", "backup", "backup.sh", "conferir", prazo=300)

    # 3. Restauracao para o ponto, como no roteiro: banco parado, restaurar.sh, banco de novo.
    p.compose("stop", "banco", prazo=300)
    p.compose(
        "run", "--rm", "--no-deps", "--entrypoint", "/usr/local/bin/restaurar.sh", "banco", "--ate", ponto, prazo=900
    )
    p.compose("up", "-d", "--wait", "banco", prazo=600)
    esperar(lambda: p.psql("SELECT pg_is_in_recovery()") == "f", 600, "o fim da recuperacao")

    # 4. Lido: o que veio antes do ponto ficou, o que veio depois sumiu; nova linha do tempo.
    frases = p.psql("SELECT string_agg(frase, '|') FROM extrato WHERE tipo = 'partida'")
    assert "antes do ponto" in frases
    assert "depois do ponto" not in frases
    assert int(p.psql("SELECT timeline_id FROM continuidade_controle()")) >= 2

    # 5. Percebido: a conferencia da partida fecha a saida e pausa os aplicativos.
    saida = p.continuidade("conferir", "--vigia", "0")
    assert "desfecho: nao_confere" in saida
    assert "linha_do_tempo_mudou" in saida
    assert p.psql("SELECT aberta FROM estado_saida") == "f"
    assert p.psql("SELECT situacao FROM aplicativos WHERE client_id = 'app-pitr'") == "pausado"

    # 6. O banco restaurado segue mandando WAL ao mesmo repositorio, na linha do tempo nova.
    p.compose("exec", "-T", "backup", "backup.sh", "conferir", prazo=300)
