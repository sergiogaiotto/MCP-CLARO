"""Copia logica (pg_dump) e restauracao de verdade, com os scripts de docker/backup, em PostgreSQL 18.7.

Os scripts rodam na imagem postgres:18.7 (a mesma base da imagem de backup), como o usuario postgres e
pela rede do hospedeiro, contra o banco do teste. A copia volta num PostgreSQL novo (outro servidor) e,
em ensaio, no mesmo servidor com outro nome. O script grava a marca "restaurado", e o despachante percebe
na partida seguinte: fecha a porta de saida e pausa os aplicativos (prova de saida da Fase 1: "backup
restaurado e lido").

O pgBackRest nao entra aqui: a imagem dele vem do repositorio apt do PGDG (ver test_continuidade_pgbackrest).
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

from mcp_claro.config.settings import Parametros
from mcp_claro.continuidade import conferir_continuidade
from mcp_claro.extrato import fechar, registrar
from tests.conftest import IMAGEM_POSTGRES, RAIZ
from tests.integracao.test_continuidade import engine_como, executar, migrar, parametros_do_banco

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

SCRIPTS = RAIZ / "docker" / "backup"


@dataclass
class Copia:
    origem: Parametros
    banco: str
    pasta: Path
    segredos: Path
    numero_fechamento: int
    linhas_extrato: int
    ultimo_id: int
    aplicativo_id: uuid.UUID


def rodar_script(script: str, ambiente: dict[str, str], volumes: dict[Path, tuple[str, str]], *args: str) -> str:
    """Roda um script de docker/backup na imagem postgres:18.7, como postgres. Devolve a saida."""
    import docker

    cliente = docker.from_env()
    montagens = {str(SCRIPTS): {"bind": "/scripts", "mode": "ro"}}
    montagens |= {str(origem): {"bind": destino, "mode": modo} for origem, (destino, modo) in volumes.items()}
    container = cliente.containers.run(
        IMAGEM_POSTGRES,
        ["bash", f"/scripts/{script}", *args],
        entrypoint=[],
        environment=ambiente,
        volumes=montagens,
        network_mode="host",
        user="postgres",
        detach=True,
    )
    try:
        fim = container.wait(timeout=180)
        saida = container.logs(stdout=True, stderr=True).decode()
    finally:
        container.remove(force=True)
    if fim["StatusCode"] != 0:
        raise ScriptFalhou(fim["StatusCode"], saida)
    return saida


def liberar_para_o_teste(pasta: Path) -> None:
    """A copia sai com pastas 700 e arquivos 600, do usuario postgres do conteiner (como deve ser). O teste
    roda com outro usuario na maquina (no CI, o do executor): abre a leitura so desta pasta temporaria,
    para conferir, copiar e apagar depois. Nada muda nos scripts nem na copia de producao."""
    import docker

    cliente = docker.from_env()
    container = cliente.containers.run(
        IMAGEM_POSTGRES,
        ["chmod", "-R", "a+rwX", "/alvo"],
        entrypoint=[],
        volumes={str(pasta): {"bind": "/alvo", "mode": "rw"}},
        user="root",
        detach=True,
    )
    try:
        fim = container.wait(timeout=60)
    finally:
        container.remove(force=True)
    if fim["StatusCode"] != 0:
        raise ScriptFalhou(fim["StatusCode"], "chmod da pasta temporaria do teste falhou")


class ScriptFalhou(Exception):
    def __init__(self, codigo: int, saida: str) -> None:
        self.codigo = codigo
        self.saida = saida
        super().__init__(f"script saiu com {codigo}:\n{saida[-3000:]}")


def endereco(p: Parametros) -> tuple[str, str]:
    partes = urlsplit(p.DATABASE_URL)
    return "127.0.0.1", str(partes.port)


async def preparar_origem(origem: Parametros) -> tuple[int, int, int, uuid.UUID]:
    """Origem com aplicativo ativo, extrato, um fechamento e a primeira partida do despachante."""
    dono = engine_como(origem)
    despachante = engine_como(origem, "mcp_claro_despachante")
    try:
        async with dono.begin() as c:
            app = (
                await c.execute(
                    text(
                        "INSERT INTO aplicativos (client_id, nome, familia) VALUES ('app-restauracao', "
                        "'Aplicativo da restauracao', 'teste') RETURNING id"
                    )
                )
            ).scalar_one()
            for i in range(3):
                await registrar(c, papel="teste", tipo="fechamento", campos={"numero": i, "linhas": i})
        async with dono.begin() as c:
            numero = (await fechar(c)).numero
        primeira = await conferir_continuidade(despachante, numero)
        assert primeira.desfecho == "primeira_partida"
        async with dono.connect() as c:
            linhas, ultimo = (await c.execute(text("SELECT count(*), max(id) FROM extrato"))).one()
        return numero, int(linhas), int(ultimo), uuid.UUID(str(app))
    finally:
        await dono.dispose()
        await despachante.dispose()


@pytest.fixture(scope="module")
def copia(banco_migrado: Parametros) -> Iterator[Copia]:
    if urlsplit(banco_migrado.DATABASE_URL).hostname not in ("localhost", "127.0.0.1"):
        pytest.skip("os scripts rodam pela rede do hospedeiro e precisam do banco em localhost")
    nome = f"origem_{uuid.uuid4().hex[:10]}"
    senha_backup = secrets.token_hex(16)
    asyncio.run(
        executar(
            banco_migrado,
            f"CREATE DATABASE {nome}",
            # O script de iniciacao do banco cria o usuario da copia logica com login; no teste, aqui.
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_claro_backup') THEN "
            "CREATE ROLE mcp_claro_backup; END IF; END $$",
            f"ALTER ROLE mcp_claro_backup WITH LOGIN PASSWORD '{senha_backup}'",
        )
    )
    origem = parametros_do_banco(banco_migrado, nome)
    migrar(origem)
    # Contador de transacoes da origem bem a frente do de um servidor novo, como numa instalacao em uso.
    asyncio.run(
        executar(origem, "DO $$ BEGIN FOR i IN 1..20000 LOOP PERFORM pg_current_xact_id(); COMMIT; END LOOP; END $$")
    )
    numero, linhas, ultimo, app = asyncio.run(preparar_origem(origem))

    raiz = Path(tempfile.mkdtemp(prefix="mcp-claro-logico-"))
    segredos, saida = raiz / "segredos", raiz / "saida"
    segredos.mkdir()
    saida.mkdir()
    (segredos / "backup_cifra").write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    (segredos / "db_backup").write_text(senha_backup + "\n", encoding="utf-8")
    (segredos / "db_migracao").write_text("senha-de-teste\n", encoding="utf-8")
    for caminho in (raiz, segredos):
        caminho.chmod(0o755)
    saida.chmod(0o777)
    for arquivo in segredos.iterdir():
        arquivo.chmod(0o644)
    host, porta = endereco(origem)
    texto = rodar_script(
        "logico.sh",
        {
            "LOGICO_HOST": host,
            "LOGICO_PORTA": porta,
            "LOGICO_SSLMODE": "disable",
            "LOGICO_BANCOS": nome,
            "LOGICO_DESTINO": "/saida",
            "LOGICO_SENHA_ARQUIVO": "/segredos/db_backup",
            "LOGICO_CIFRA_ARQUIVO": "/segredos/backup_cifra",
        },
        {segredos: ("/segredos", "ro"), saida: ("/saida", "rw")},
    )
    liberar_para_o_teste(saida)
    pastas = [p for p in saida.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert len(pastas) == 1, texto
    try:
        yield Copia(origem, nome, pastas[0], segredos, numero, linhas, ultimo, app)
    finally:
        shutil.rmtree(raiz, ignore_errors=True)
        asyncio.run(executar(banco_migrado, f"DROP DATABASE IF EXISTS {nome} WITH (FORCE)"))


@pytest.fixture(scope="module")
def destino() -> Iterator[Parametros]:
    """Um PostgreSQL novo, outro servidor: como uma maquina refeita do zero."""
    testcontainers = pytest.importorskip("testcontainers.postgres")
    with testcontainers.PostgresContainer(
        IMAGEM_POSTGRES, username="mcp_claro_migracao", password="senha-de-teste", dbname="mcp_claro"
    ) as pg:
        pasta = Path(tempfile.mkdtemp(prefix="mcp-claro-destino-"))
        senha = pasta / "db"
        senha.write_text("senha-de-teste", encoding="utf-8")
        url = f"postgresql://mcp_claro_migracao@127.0.0.1:{pg.get_exposed_port(5432)}/mcp_claro"
        try:
            yield Parametros(APP_ENV="teste", DATABASE_URL=url, DATABASE_PASSWORD_FILE=str(senha), DB_SSLMODE="disable")
        finally:
            shutil.rmtree(pasta, ignore_errors=True)


def restaurar(copia: Copia, destino: Parametros, pasta: Path | None = None, **extra: str) -> str:
    host, porta = endereco(destino)
    ambiente = {
        "RESTAURAR_HOST": host,
        "RESTAURAR_PORTA": porta,
        "RESTAURAR_SSLMODE": "disable",
        "RESTAURAR_SENHA_ARQUIVO": "/segredos/db_migracao",
        "LOGICO_CIFRA_ARQUIVO": "/segredos/backup_cifra",
        **extra,
    }
    pasta = pasta or copia.pasta
    return rodar_script(
        "restaurar-logico.sh",
        ambiente,
        {copia.segredos: ("/segredos", "ro"), pasta: ("/copia", "ro")},
        "/copia",
    )


def test_copia_logica_cifrada_e_conferida(copia: Copia) -> None:
    nomes = sorted(p.name for p in copia.pasta.iterdir())
    assert nomes == sorted(["SHA256SUMS", "copia.txt", "globais.sql.gpg", f"{copia.banco}.dump.gpg"])
    for cifrado in copia.pasta.glob("*.gpg"):
        conteudo = cifrado.read_bytes()
        assert not conteudo.startswith(b"PGDMP")
        assert b"extrato" not in conteudo
        assert b"mcp_claro_despachante" not in conteudo
    somas = (copia.pasta / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"{copia.banco}.dump.gpg" in somas
    descricao = (copia.pasta / "copia.txt").read_text(encoding="utf-8")
    for segredo in ("backup_cifra", "db_backup"):
        assert (copia.segredos / segredo).read_text(encoding="utf-8").strip() not in descricao


@pytest.fixture(scope="module")
def no_servidor_novo(copia: Copia, destino: Parametros) -> Parametros:
    """A copia restaurada no servidor novo, uma vez para o modulo."""
    saida = restaurar(copia, destino)
    assert "pronto" in saida
    return parametros_do_banco(destino, copia.banco)


async def test_restauracao_num_servidor_novo_e_lida_e_percebida(copia: Copia, no_servidor_novo: Parametros) -> None:
    restaurado = no_servidor_novo
    dono = engine_como(restaurado)
    despachante = engine_como(restaurado, "mcp_claro_despachante")
    try:
        async with dono.connect() as c:
            linhas = (await c.execute(text("SELECT count(*) FROM extrato"))).scalar_one()
            # A conta do fechamento refeita so sobre as linhas que vieram na copia. Num servidor novo o
            # contador de transacoes recomeca, e linhas novas (a marca) podem cair dentro de um trecho ja
            # fechado: por isso extrato_conferir() sozinho nao serve aqui (ver o relatorio da Fase 1).
            confere = (
                await c.execute(
                    text(
                        "SELECT f.hash = sha256(coalesce(f.hash_anterior, '\\x'::bytea) || "
                        "coalesce(string_agg(e.hash_linha, '\\x'::bytea ORDER BY e.xid, e.id), '\\x'::bytea)) "
                        "FROM fechamentos f LEFT JOIN extrato e ON e.xid >= f.desde_xid AND e.xid < f.ate_xid "
                        "AND e.id <= :ultimo WHERE f.numero = :n GROUP BY f.hash, f.hash_anterior"
                    ),
                    {"n": copia.numero_fechamento, "ultimo": copia.ultimo_id},
                )
            ).scalar_one()
            versao = (await c.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            ajustes = (
                await c.execute(
                    text(
                        "SELECT s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
                        "JOIN pg_database d ON d.oid = s.setdatabase "
                        "WHERE r.rolname = 'grafana_leitor' AND d.datname = current_database()"
                    )
                )
            ).scalar_one_or_none()
            pode = (
                await c.execute(
                    text("SELECT has_function_privilege('mcp_claro_despachante', 'continuidade_controle()', 'EXECUTE')")
                )
            ).scalar_one()
        # Lido: o extrato voltou inteiro (mais a linha da marca "restaurado" gravada pelo script) e o
        # fechamento guardado confere com a conta refeita sobre as linhas da copia.
        assert linhas == copia.linhas_extrato + 1
        assert confere is True
        assert versao == "0004"
        assert ajustes is not None
        assert "default_transaction_read_only=on" in ajustes
        assert pode is True

        # Percebido: outro servidor, outro identificador; a saida fecha e o aplicativo volta pausado.
        r = await conferir_continuidade(despachante, copia.numero_fechamento)
        assert r.desfecho == "nao_confere"
        # A marca foi gravada ja no servidor novo, com o identificador dele: e ela que avisa o despachante.
        assert r.motivos == ("marca_restaurado",)
        assert r.aplicativos_pausados == (copia.aplicativo_id,)
        async with dono.connect() as c:
            aberta = (await c.execute(text("SELECT aberta FROM estado_saida"))).scalar_one()
            tipos = [t for (t,) in (await c.execute(text("SELECT tipo FROM extrato ORDER BY id"))).all()]
        assert aberta is False
        assert tipos[-4:] == [
            "estado_saida_mudou",
            "restauracao_detectada",
            "intervalo_perdido",
            "aplicativo_pausado_continuidade",
        ]
    finally:
        await dono.dispose()
        await despachante.dispose()


async def test_ensaio_no_mesmo_servidor_e_percebido_pela_marca(copia: Copia) -> None:
    """De volta ao mesmo servidor, identificador e linha do tempo nao mudam: so a marca avisa o despachante."""
    ensaio = f"ensaio_{uuid.uuid4().hex[:8]}"
    restaurar(
        copia, copia.origem, RESTAURAR_COMO=ensaio, RESTAURAR_GLOBAIS="desligado", RESTAURAR_QUEM="Pessoa do ensaio"
    )
    p = parametros_do_banco(copia.origem, ensaio)
    dono = engine_como(p)
    despachante = engine_como(p, "mcp_claro_despachante")
    try:
        async with dono.connect() as c:
            linhas = (await c.execute(text("SELECT count(*) FROM extrato"))).scalar_one()
            # No mesmo servidor o contador de transacoes segue: a conta do fechamento ainda bate inteira.
            confere = (
                await c.execute(text("SELECT extrato_conferir(:n)"), {"n": copia.numero_fechamento})
            ).scalar_one()
            marca = (
                await c.execute(text("SELECT quem, motivo, origem FROM continuidade_banco ORDER BY id DESC LIMIT 1"))
            ).one()
        r = await conferir_continuidade(despachante, copia.numero_fechamento)
    finally:
        await dono.dispose()
        await despachante.dispose()
        await executar(copia.origem, f"DROP DATABASE IF EXISTS {ensaio} WITH (FORCE)")
    # O extrato voltou inteiro, mais a linha da marca.
    assert linhas == copia.linhas_extrato + 1
    assert confere is True
    assert (marca.quem, marca.origem) == ("Pessoa do ensaio", "roteiro")
    assert marca.motivo == f"copia logica {copia.pasta.name}"
    assert r.motivos == ("marca_restaurado",)
    assert r.saida_fechada


def test_copia_alterada_nao_e_restaurada(copia: Copia, destino: Parametros) -> None:
    alterada = Path(tempfile.mkdtemp(prefix="mcp-claro-alterada-"))
    try:
        alvo = alterada / "copia"
        shutil.copytree(copia.pasta, alvo)
        alterada.chmod(0o755)
        alvo.chmod(0o755)
        dump = alvo / f"{copia.banco}.dump.gpg"
        dados = bytearray(dump.read_bytes())
        dados[len(dados) // 2] ^= 0xFF
        dump.write_bytes(bytes(dados))
        for arquivo in alvo.iterdir():
            arquivo.chmod(0o644)
        with pytest.raises(ScriptFalhou) as erro:
            restaurar(copia, destino, alvo, RESTAURAR_COMO=f"nunca_{uuid.uuid4().hex[:8]}")
        assert erro.value.codigo == 3
        assert "SHA256SUMS falhou" in erro.value.saida
    finally:
        shutil.rmtree(alterada, ignore_errors=True)


def test_senha_de_cifra_errada_nao_abre_a_copia(copia: Copia, destino: Parametros) -> None:
    outra = Path(tempfile.mkdtemp(prefix="mcp-claro-outra-cifra-"))
    try:
        for arquivo in copia.segredos.iterdir():
            shutil.copy(arquivo, outra / arquivo.name)
        (outra / "backup_cifra").write_text("outra-senha-qualquer\n", encoding="utf-8")
        outra.chmod(0o755)
        for arquivo in outra.iterdir():
            arquivo.chmod(0o644)
        errada = Copia(**{**copia.__dict__, "segredos": outra})
        with pytest.raises(ScriptFalhou):
            restaurar(errada, destino, RESTAURAR_COMO=f"nunca_{uuid.uuid4().hex[:8]}", RESTAURAR_GLOBAIS="desligado")
    finally:
        shutil.rmtree(outra, ignore_errors=True)


async def test_fechamento_segue_conferindo_depois_da_copia_logica_em_servidor_novo(
    copia: Copia, no_servidor_novo: Parametros
) -> None:
    engine = engine_como(no_servidor_novo)
    try:
        async with engine.connect() as c:
            confere = (
                await c.execute(text("SELECT extrato_conferir(:n)"), {"n": copia.numero_fechamento})
            ).scalar_one()
    finally:
        await engine.dispose()
    assert confere is True
