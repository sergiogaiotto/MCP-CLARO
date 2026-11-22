"""R6e-4 (contrato K8): a sala de visitas nao sobe sem ``APP_ENV`` escrito no ambiente dela.

Sem ``APP_ENV``, os ``Parametros`` caem no padrao ``desenvolvimento``, e a ``Sala`` liga ``liberar_locais``: o filho
chamaria o terceiro sem conferir https nem IP publico. O despachante nao tem como saber o ambiente da sala. Agora a
partida da sala (``visitas.partida.problemas_da_sala``) recusa subir sem ``APP_ENV`` no ambiente do processo, com a
frase abaixo, e o Compose escreve o ``APP_ENV`` da sala e dos outros papeis num lugar so (a ancora ``x-app-env``).

Prova da revisao: ``R/isolamento/repo/tests/unidade/test_rev_isolamento_ambiente_sala.py`` (aqui, invertida).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcp_claro import __main__ as principal
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.papeis import Papel
from mcp_claro.visitas.partida import conferir_sala, problemas_da_sala

RAIZ = Path(__file__).resolve().parents[2]

SEM_APP_ENV = (
    "A sala de visitas precisa de APP_ENV escrito no ambiente dela (desenvolvimento, teste, piloto ou producao): sem"
    " ele, ela subiria como desenvolvimento e aceitaria enderecos locais."
)


@pytest.fixture
def ambiente_da_sala(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """O ambiente de partida da sala no Compose, sem ``APP_ENV`` (como na prova): so a porta e o certificado."""
    import os

    for nome in list(os.environ):
        if nome == "APP_ENV" or nome.startswith(("DATABASE_", "DB_", "VAULT_")) or nome.endswith("_FILE"):
            monkeypatch.delenv(nome)
    arquivos = {n: tmp_path / f"{n}.pem" for n in ("cert", "key", "ca")}
    for f in arquivos.values():
        f.write_text("x", encoding="utf-8")
    ambiente = {
        "VISITS_INTERNAL_PORT": "9446",
        "INTERNAL_CA_FILE": str(arquivos["ca"]),
        "INTERNAL_CERT_FILE": str(arquivos["cert"]),
        "INTERNAL_KEY_FILE": str(arquivos["key"]),
        "INTERNAL_CLIENT_CA_FILE": str(arquivos["ca"]),
    }
    for nome, valor in ambiente.items():
        monkeypatch.setenv(nome, valor)
    return ambiente


def test_sala_sem_app_env_nao_sobe(ambiente_da_sala: dict[str, str]) -> None:
    """A prova da revisao, invertida: sem APP_ENV, a partida recusa (antes subia como desenvolvimento)."""
    p = Parametros()
    assert problemas_da_sala(p, ambiente_da_sala) == [SEM_APP_ENV]
    with pytest.raises(ConfiguracaoInvalida) as recusa:
        conferir_sala(p, ambiente_da_sala)
    assert SEM_APP_ENV in str(recusa.value)


@pytest.mark.parametrize("ambiente", ["desenvolvimento", "teste", "piloto", "producao"])
def test_sala_com_app_env_escrito_sobe(ambiente_da_sala: dict[str, str], ambiente: str) -> None:
    p = Parametros(APP_ENV=ambiente)
    assert problemas_da_sala(p, ambiente_da_sala | {"APP_ENV": ambiente}) == []


def test_comando_visitas_sem_app_env_sai_com_a_frase_e_nao_sobe(
    ambiente_da_sala: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A segunda prova da revisao (o filho com o ambiente padrao nao confere o endereco) nao chega a acontecer: a sala
    sem APP_ENV nao sobe, e nenhum filho nasce com ``liberar_locais`` por padrao."""
    subidos: list[tuple[Papel, Parametros]] = []
    monkeypatch.setattr(principal, "_subir", lambda papel, p: subidos.append((papel, p)))
    monkeypatch.setattr(principal, "configurar", lambda *_: None)
    with pytest.raises(SystemExit) as saida:
        principal.main(["visitas"])
    assert saida.value.code == 2
    assert subidos == []
    assert SEM_APP_ENV in capsys.readouterr().err
    monkeypatch.setenv("APP_ENV", "teste")
    principal.main(["visitas"])
    assert [(papel, str(p.APP_ENV)) for papel, p in subidos] == [(Papel.VISITAS, "teste")]


def test_compose_escreve_o_app_env_da_sala_e_dos_papeis_num_lugar_so() -> None:
    """A ancora ``x-app-env`` so tem o APP_ENV: a sala nao recebe o resto do ambiente do gateway (``DATABASE_*``)."""
    compose = yaml.safe_load((RAIZ / "docker" / "compose.yml").read_text(encoding="utf-8"))
    assert compose["x-app-env"] == {"APP_ENV": "teste"}
    assert compose["x-gateway"]["environment"]["APP_ENV"] == "teste"
    sala = compose["services"]["visitas"]["environment"]
    assert sala["APP_ENV"] == "teste"
    assert not [n for n in sala if n.startswith(("DATABASE_", "DB_", "VAULT_"))]
    texto = (RAIZ / "docker" / "compose.yml").read_text(encoding="utf-8")
    assert texto.count("APP_ENV:") == 1  # escrito uma vez so, na ancora
