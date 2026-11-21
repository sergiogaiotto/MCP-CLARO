"""Grafana 13.3.1 com a fonte de dados, o painel e os alertas provisionados pelos arquivos do repositorio.

Sobe a imagem fixada por digest com ``grafana/grafana.ini`` e ``grafana/provisionamento`` montados como no
Compose (sistema de arquivos so de leitura, sem capacidades), contra o PostgreSQL do teste: banco proprio
do Grafana (usuario ``grafana``) e um banco de medicoes migrado so para este modulo, lido pelo
``grafana_leitor``. O Grafana roda na rede do hospedeiro para alcancar a porta do banco de teste; no
Compose ele fica so na rede interna. O webhook dos alertas chega a um receptor HTTPS falso que exige
certificado de cliente e usa as mesmas regras das rotinas (``mcp_claro.avisos.grafana``).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import os
import secrets
import shutil
import socket
import ssl
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx2
import pytest
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro.avisos.grafana import ler_alertas, senha_confere
from mcp_claro.config.settings import Parametros
from tests.conftest import RAIZ
from tests.integracao.test_continuidade import dsn, executar, migrar, parametros_do_banco

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

IMAGEM = os.environ.get(
    "MCP_CLARO_TESTE_GRAFANA",
    "grafana/grafana:13.3.1@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf",
)
PASTA_GRAFANA = RAIZ / "grafana"
FONTE = "mcp-claro-medicoes"
PAINEL = "mcp-claro-visao-geral"
REGRAS = {
    "mcp-claro-recusas-versao-subindo",
    "mcp-claro-sistema-recusado-versao",
    "mcp-claro-crachas-recusados-subindo",
    "mcp-claro-recusas-chamada-subindo",
    "mcp-claro-leituras-pararam",
}


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- Certificados de teste: autoridade interna, autoridade de clientes das rotinas e os certificados ----


def _pem_chave(chave: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return chave.private_bytes(  # type: ignore[no-any-return]
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _emitir(nome: str, emissor: tuple[Any, Any] | None, *, ca: bool = False, ip: str | None = None) -> tuple[Any, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    chave = ec.generate_private_key(ec.SECP256R1())
    sujeito = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nome)])
    cert_emissor, chave_emissor = emissor if emissor else (None, chave)
    agora = dt.datetime.now(dt.UTC)
    construtor = (
        x509.CertificateBuilder()
        .subject_name(sujeito)
        .issuer_name(cert_emissor.subject if cert_emissor else sujeito)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - dt.timedelta(minutes=5))
        .not_valid_after(agora + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        # O Python 3.14 confere em modo estrito: precisa dos identificadores de chave.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(chave.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(chave_emissor.public_key()), critical=False)
    )
    if ca:
        construtor = construtor.add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True
        )
    else:
        uso = ExtendedKeyUsageOID.SERVER_AUTH if ip else ExtendedKeyUsageOID.CLIENT_AUTH
        construtor = construtor.add_extension(x509.ExtendedKeyUsage([uso]), critical=False)
    if ip:
        nomes = [x509.IPAddress(ipaddress.ip_address(ip)), x509.DNSName("localhost")]
        construtor = construtor.add_extension(x509.SubjectAlternativeName(nomes), critical=False)
    return construtor.sign(chave_emissor, hashes.SHA256()), chave


@dataclass(frozen=True)
class Certificados:
    ca_interna: Path
    ca_clientes: Path
    rotinas_cert: Path
    rotinas_chave: Path


def gerar_certificados(segredos: Path, privado: Path) -> Certificados:
    from cryptography.hazmat.primitives import serialization

    pem = serialization.Encoding.PEM
    ca_interna = _emitir("MCP-CLARO autoridade interna (teste)", None, ca=True)
    ca_clientes = _emitir("MCP-CLARO clientes das rotinas (teste)", None, ca=True)
    rotinas = _emitir("rotinas", ca_interna, ip="127.0.0.1")
    servidor = _emitir("grafana", ca_interna, ip="127.0.0.1")
    cliente = _emitir("grafana_cliente", ca_clientes)
    arquivos = {
        segredos / "ca_interna.pem": ca_interna[0].public_bytes(pem),
        segredos / "grafana_cert.pem": servidor[0].public_bytes(pem),
        segredos / "grafana_key.pem": _pem_chave(servidor[1]),
        segredos / "grafana_cliente_cert.pem": cliente[0].public_bytes(pem),
        segredos / "grafana_cliente_key.pem": _pem_chave(cliente[1]),
        privado / "ca_clientes.pem": ca_clientes[0].public_bytes(pem),
        privado / "rotinas_cert.pem": rotinas[0].public_bytes(pem),
        privado / "rotinas_key.pem": _pem_chave(rotinas[1]),
    }
    for caminho, conteudo in arquivos.items():
        caminho.write_bytes(conteudo)
    return Certificados(
        segredos / "ca_interna.pem",
        privado / "ca_clientes.pem",
        privado / "rotinas_cert.pem",
        privado / "rotinas_key.pem",
    )


# --- Banco do teste --------------------------------------------------------------------------------------

DADOS = r"""
INSERT INTO recusas_versao (minuto, lado, origem, versao_pedida, motivo, contagem) VALUES
  (date_trunc('minute', now()) - interval '10 minutes', 'aplicativo', 'app-antigo', '2025-06-18',
   'versao_nao_aceita', 25),
  (date_trunc('minute', now()) - interval '10 minutes', 'sistema', 'sistema-velho', 'geracao-antiga',
   'cadastro', 2),
  (date_trunc('minute', now()) - interval '10 minutes', 'sistema', 'terceiro-velho', 'geracao-antiga',
   'cadastro_terceiro', 3);
INSERT INTO sistemas (nome, endereco, origem, tipo_chave, situacao) VALUES
  ('terceiro-em-quarentena', 'https://t1.exemplo.teste/mcp', 'terceiro', 'por_pessoa', 'quarentena'),
  ('terceiro-pendente', 'https://t2.exemplo.teste/mcp', 'terceiro', 'compartilhada', 'pendente'),
  ('proprio-ativo', 'https://p1.exemplo.teste/mcp', 'proprio', 'por_pessoa', 'ativo');
INSERT INTO medicoes.minuto (minuto, nome, rotulos, valor) VALUES
  (date_trunc('minute', now()) - interval '5 minutes', 'crachas_recusados', '{"motivo": "audiencia"}', 3),
  (date_trunc('minute', now()) - interval '5 minutes', 'leituras', '{"sistema": "agenda"}', 7),
  (date_trunc('minute', now()) - interval '5 minutes', 'chamadas_recusadas', '{"motivo": "fora_da_ordem"}', 4),
  (date_trunc('minute', now()) - interval '5 minutes', 'leituras_recusadas_despachante', '{"motivo": "bloqueado"}', 1),
  (date_trunc('minute', now()) - interval '5 minutes', 'pedidos_recusados', '{"motivo": "entrada_fechada"}', 2),
  (date_trunc('minute', now()) - interval '5 minutes', 'recusas_versao', '{"lado": "aplicativo"}', 25);
INSERT INTO medicoes.dia (dia, nome, rotulos, valor) VALUES
  (current_date - 1, 'recusas_versao', '{"lado": "aplicativo"}', 40);
"""


async def preparar_banco(p: Parametros, sufixo: str, senha_grafana: str, senha_leitor: str) -> None:
    await executar(
        p,
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana') THEN "
        "CREATE ROLE grafana LOGIN; END IF; END $$",
        f"ALTER ROLE grafana WITH LOGIN PASSWORD '{senha_grafana}'",
        f"ALTER ROLE grafana_leitor WITH LOGIN PASSWORD '{senha_leitor}'",
        f"CREATE DATABASE grafana_{sufixo} OWNER grafana",
        f"CREATE DATABASE medicoes_{sufixo}",
    )


async def popular(p: Parametros) -> None:
    conexao = await asyncpg.connect(dsn(p))
    try:
        await conexao.execute(DADOS)
    finally:
        await conexao.close()


# --- Grafana ---------------------------------------------------------------------------------------------


@dataclass
class Grafana:
    url: str
    senha_admin: str
    senha_webhook: str
    certificados: Certificados
    porta_webhook: int
    porta: int
    container: Any = field(repr=False)
    medicoes: Parametros | None = field(default=None, repr=False)
    """O banco de medicoes deste modulo (o dono), para os testes que mexem nos dados e devolvem no fim."""

    def logs(self) -> tuple[bytes, bytes]:
        saida, erros = self.container.get_logs()
        return bytes(saida), bytes(erros)

    def confianca(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.certificados.ca_interna))

    def cliente(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=self.url, auth=("admin", self.senha_admin), timeout=30, trust_env=False, verify=self.confianca()
        )


def _esperar_saude(url: str, ca: Path, container: Any, prazo: float = 180) -> None:
    fim = time.monotonic() + prazo
    ultimo: object = None
    confianca = ssl.create_default_context(cafile=str(ca))
    while time.monotonic() < fim:
        try:
            r = httpx2.get(f"{url}/api/health", timeout=3, trust_env=False, verify=confianca)
            if r.status_code == 200 and r.json().get("database") == "ok":
                return
            ultimo = r.text
        except httpx2.HTTPError as erro:
            ultimo = erro
        if container.get_wrapped_container().status == "exited":
            break
        time.sleep(1)
    saida, erros = container.get_logs()
    raise AssertionError(f"o Grafana nao ficou saudavel: {ultimo}\n{(saida + erros).decode()[-4000:]}")


@pytest.fixture(scope="module")
def grafana(banco_migrado: Parametros) -> Iterator[Grafana]:
    nucleo = pytest.importorskip("testcontainers.core.container")
    partes = urlsplit(banco_migrado.DATABASE_URL)
    if partes.hostname not in ("localhost", "127.0.0.1"):
        pytest.skip("o Grafana do teste usa a rede do hospedeiro e precisa do banco em localhost")
    endereco_banco = f"127.0.0.1:{partes.port}"
    sufixo = uuid.uuid4().hex[:10]
    senha_grafana, senha_leitor = secrets.token_hex(16), secrets.token_hex(16)
    senha_admin, senha_webhook = secrets.token_hex(16), secrets.token_hex(16)
    asyncio.run(preparar_banco(banco_migrado, sufixo, senha_grafana, senha_leitor))
    medicoes = parametros_do_banco(banco_migrado, f"medicoes_{sufixo}")
    migrar(medicoes)
    asyncio.run(popular(medicoes))

    # Segredos como no Compose (/run/secrets), legiveis pelo usuario do Grafana (472).
    pasta = Path(tempfile.mkdtemp(prefix="mcp-claro-grafana-"))
    segredos, privado = pasta / "segredos", pasta / "privado"
    segredos.mkdir()
    privado.mkdir()
    for nome, valor in {
        "db_grafana": senha_grafana,
        "db_grafana_leitor": senha_leitor,
        "grafana_admin": senha_admin,
        "grafana_webhook": senha_webhook,
    }.items():
        (segredos / nome).write_text(valor + "\n", encoding="utf-8")
    certificados = gerar_certificados(segredos, privado)
    pasta.chmod(0o755)
    segredos.chmod(0o755)
    for arquivo in segredos.iterdir():
        arquivo.chmod(0o644)

    porta, porta_webhook = porta_livre(), porta_livre()
    container = nucleo.DockerContainer(IMAGEM)
    for chave, valor in {
        "MCP_CLARO_BANCO_ENDERECO": endereco_banco,
        "MCP_CLARO_BANCO_NOME": f"medicoes_{sufixo}",
        "MCP_CLARO_BANCO_SSLMODE": "disable",
        "MCP_CLARO_ALERTAS_URL": f"https://127.0.0.1:{porta_webhook}/interno/alertas-grafana",
        # O que muda do Compose: banco do teste sem TLS, banco proprio deste modulo e porta livre.
        "GF_SERVER_HTTP_ADDR": "127.0.0.1",
        "GF_SERVER_HTTP_PORT": str(porta),
        "GF_DATABASE_NAME": f"grafana_{sufixo}",
        "GF_DATABASE_SSL_MODE": "disable",
    }.items():
        container.with_env(chave, valor)
    container.with_volume_mapping(str(PASTA_GRAFANA / "grafana.ini"), "/etc/grafana/grafana.ini", "ro")
    container.with_volume_mapping(str(PASTA_GRAFANA / "provisionamento"), "/etc/grafana/provisioning", "ro")
    container.with_volume_mapping(str(PASTA_GRAFANA / "paineis"), "/etc/grafana/paineis", "ro")
    container.with_volume_mapping(str(segredos), "/run/secrets", "ro")
    # Pastas que o Grafana escreve: no Compose, /tmp em tmpfs e /var/lib/grafana num volume.
    container.tmpfs.update({"/tmp": "", "/var/lib/grafana": "uid=472,gid=0,mode=0770"})  # noqa: S108
    container.with_kwargs(
        network_mode="host",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
    )
    container.start()
    url = f"https://127.0.0.1:{porta}"
    try:
        _esperar_saude(url, certificados.ca_interna, container)
        yield Grafana(url, senha_admin, senha_webhook, certificados, porta_webhook, porta, container, medicoes)
    finally:
        container.stop()
        shutil.rmtree(pasta, ignore_errors=True)
        asyncio.run(
            executar(
                banco_migrado,
                f"DROP DATABASE IF EXISTS grafana_{sufixo} WITH (FORCE)",
                f"DROP DATABASE IF EXISTS medicoes_{sufixo} WITH (FORCE)",
            )
        )


async def consultar(g: Grafana, consultas: list[dict[str, Any]], desde: str = "now-24h") -> dict[str, Any]:
    async with g.cliente() as c:
        r = await c.post("/api/ds/query", json={"queries": consultas, "from": desde, "to": "now"})
    corpo: dict[str, Any] = r.json()
    return corpo


def sql(texto: str, ref: str = "A", formato: str = "table") -> dict[str, Any]:
    return {"refId": ref, "datasource": {"uid": FONTE}, "rawSql": texto, "format": formato, "rawQuery": True}


def valores(resultado: dict[str, Any], ref: str = "A") -> list[list[Any]]:
    quadros = resultado["results"][ref].get("frames", [])
    return [quadro["data"]["values"] for quadro in quadros]


# --- Testes ----------------------------------------------------------------------------------------------


async def test_grafana_responde_saude_com_o_banco_proprio(grafana: Grafana) -> None:
    async with grafana.cliente() as c:
        saude = (await c.get("/api/health")).json()
        org = (await c.get("/api/org")).json()
    assert saude["database"] == "ok"
    assert saude["version"] == "13.3.1"
    assert org["name"] == "Main Org."


def test_saude_pelo_mesmo_comando_do_compose(grafana: Grafana) -> None:
    """O healthcheck do Compose (curl com a autoridade interna, em https) funciona dentro da imagem."""
    codigo, saida = grafana.container.exec(
        [
            "curl",
            "-fsS",
            "-o",
            "/dev/null",
            "--cacert",
            "/run/secrets/ca_interna.pem",
            f"https://localhost:{grafana.porta}/api/health",
        ]
    )
    assert codigo == 0, saida


async def test_fonte_de_dados_provisionada_com_grafana_leitor(grafana: Grafana) -> None:
    async with grafana.cliente() as c:
        fonte = (await c.get(f"/api/datasources/uid/{FONTE}")).json()
        saude = await c.get(f"/api/datasources/uid/{FONTE}/health")
    assert fonte["type"] == "grafana-postgresql-datasource"
    assert fonte["user"] == "grafana_leitor"
    assert fonte["readOnly"] is True
    assert fonte["isDefault"] is True
    assert saude.status_code == 200, saude.text
    assert saude.json()["status"] == "OK"


async def test_painel_provisionado_aparece_na_busca(grafana: Grafana) -> None:
    async with grafana.cliente() as c:
        busca = (await c.get("/api/search", params={"query": "MCP-CLARO", "type": "dash-db"})).json()
        painel = (await c.get(f"/api/dashboards/uid/{PAINEL}")).json()
        pastas = (await c.get("/api/folders")).json()
    achados = {item["uid"]: item for item in busca}
    assert achados[PAINEL]["title"] == "MCP-CLARO: visao geral"
    assert achados[PAINEL]["folderUid"] == "mcp-claro"
    assert painel["meta"]["provisioned"] is True
    assert painel["meta"]["canSave"] is False or painel["meta"]["provisioned"] is True
    titulos = [p["title"] for p in painel["dashboard"]["panels"]]
    # Recusas por versao em destaque: logo depois do texto de leitura, antes de tudo o resto (decisao 8).
    assert titulos[1] == "Recusas por versao (decisao 8)"
    assert titulos[2:4] == ["Aplicativos recusados por versao", "Sistemas recusados por versao"]
    # Sem pasta repetida: alertas e paineis em pastas com nomes diferentes.
    nomes = [p["title"] for p in pastas]
    assert len(nomes) == len(set(nomes))


async def test_consultas_do_painel_rodam_como_grafana_leitor(grafana: Grafana) -> None:
    async with grafana.cliente() as c:
        painel = (await c.get(f"/api/dashboards/uid/{PAINEL}")).json()["dashboard"]
    alvos = [(p["title"], p.get("timeFrom"), alvo) for p in painel["panels"] for alvo in p.get("targets", [])]
    assert len(alvos) >= 10
    totais: dict[str, Any] = {}
    for titulo, janela, alvo in alvos:
        desde = f"now-{janela}" if janela else "now-24h"
        consulta = {**alvo, "datasource": {"uid": FONTE}, "intervalMs": 60000, "maxDataPoints": 500}
        resultado = await consultar(grafana, [consulta], desde)
        resposta = resultado["results"]["A"]
        assert "error" not in resposta, f"{titulo}: {resposta.get('error')}"
        quadros = valores(resultado)
        assert quadros, f"{titulo}: sem dados"
        totais[titulo] = quadros
    assert totais["Aplicativos recusados por versao"] == [[[25]]]
    # O total da decisao 8 conta todos os sistemas, proprios e de terceiros.
    assert totais["Sistemas recusados por versao"] == [[[5]]]
    assert totais["Versoes pedidas diferentes"] == [[[2]]]
    serie_por_lado = totais["Recusas por versao, dos dois lados"]
    assert sum(v for quadro in serie_por_lado for campo in quadro[1:] for v in campo if v) == 30
    # Fase 3: o bloco dos servidores de terceiros, lido como grafana_leitor.
    assert totais["Servidores de terceiros recusados por versao"] == [[[1]]]
    assert totais["Servidores de terceiros em quarentena agora"] == [[[1]]]
    ((situacoes, revisados, quantos),) = totais["Servidores de terceiros por situacao"]
    assert sorted(zip(situacoes, revisados, quantos, strict=True)) == [("pendente", "nao", 1), ("quarentena", "nao", 1)]


async def test_grafana_leitor_nao_le_tabelas_nem_escreve(grafana: Grafana) -> None:
    for comando, esperado in (
        ("SELECT count(*) FROM medicoes.minuto", "permission denied"),
        ("SELECT count(*) FROM public.recusas_versao", "permission denied"),
        ("SELECT count(*) FROM public.extrato", "permission denied"),
        ("SELECT count(*) FROM public.pessoas", "permission denied"),
        ("SELECT * FROM public.continuidade_controle()", "permission denied"),
        ("CREATE TABLE medicoes.invasora (x int)", "read-only transaction"),
    ):
        resposta = (await consultar(grafana, [sql(comando)]))["results"]["A"]
        assert esperado in str(resposta.get("error", "")), f"{comando}: {resposta}"


async def test_alertas_provisionados_e_avaliados(grafana: Grafana) -> None:
    async with grafana.cliente() as c:
        regras = (await c.get("/api/v1/provisioning/alert-rules")).json()
        politica = (await c.get("/api/v1/provisioning/policies")).json()
    assert {r["uid"] for r in regras} == REGRAS
    assert politica["receiver"] == "mcp-claro-rotinas"
    disparam = {}
    for regra in regras:
        consultas = []
        for passo in regra["data"]:
            modelo = dict(passo["model"])
            modelo["datasource"] = (
                {"uid": passo["datasourceUid"], "type": "__expr__"}
                if (passo["datasourceUid"] == "__expr__")
                else {"uid": passo["datasourceUid"]}
            )
            consultas.append(modelo)
        resultado = await consultar(grafana, consultas, "now-25h")
        for ref, resposta in resultado["results"].items():
            assert "error" not in resposta, f"{regra['uid']} {ref}: {resposta.get('error')}"
        disparam[regra["uid"]] = valores(resultado, regra["condition"])[0][-1][0]
    # Com os dados do teste: 30 recusas por versao na ultima hora e nenhuma antes; 2 recusas de sistema proprio e 3
    # de servidor de terceiros.
    assert disparam == {
        "mcp-claro-recusas-versao-subindo": 1,
        "mcp-claro-sistema-recusado-versao": 1,
        "mcp-claro-crachas-recusados-subindo": 0,
        "mcp-claro-recusas-chamada-subindo": 0,
        "mcp-claro-leituras-pararam": 0,
    }


async def _avaliar(g: Grafana, uid: str) -> tuple[Any, Any]:
    """Valor da consulta (A) e da condicao de uma regra de alerta, avaliadas pelo Grafana como grafana_leitor."""
    async with g.cliente() as c:
        regras = (await c.get("/api/v1/provisioning/alert-rules")).json()
    (regra,) = (r for r in regras if r["uid"] == uid)
    consultas = []
    for passo in regra["data"]:
        modelo = dict(passo["model"])
        expressao = passo["datasourceUid"] == "__expr__"
        modelo["datasource"] = {"uid": passo["datasourceUid"], "type": "__expr__"} if expressao else {"uid": FONTE}
        consultas.append(modelo)
    resultado = await consultar(g, consultas, "now-25h")
    for ref, resposta in resultado["results"].items():
        assert "error" not in resposta, f"{uid} {ref}: {resposta.get('error')}"
    return valores(resultado, "A")[0][-1][0], valores(resultado, regra["condition"])[0][-1][0]


async def test_recusa_de_terceiro_nao_dispara_o_alerta_alto_e_a_de_proprio_dispara(grafana: Grafana) -> None:
    """D30: so a recusa de sistema proprio (motivo cadastro) dispara "Sistema recusado por versao"; a de servidor
    de terceiros (cadastro_terceiro) fica no painel. Mexe nos dados do banco deste modulo e devolve no fim."""
    uid = "mcp-claro-sistema-recusado-versao"
    assert await _avaliar(grafana, uid) == (2, 1)  # as 3 recusas de terceiro nao contam
    assert grafana.medicoes is not None
    conexao = await asyncpg.connect(dsn(grafana.medicoes))
    try:
        # So recusas de terceiro na ultima hora (a de proprio vai para 3 horas atras): o alerta alto nao dispara.
        await conexao.execute(
            "UPDATE recusas_versao SET minuto = minuto - interval '3 hours' WHERE motivo = 'cadastro'"
        )
        assert await _avaliar(grafana, uid) == (0, 0)
    finally:
        await conexao.execute(
            "UPDATE recusas_versao SET minuto = minuto + interval '3 hours' WHERE motivo = 'cadastro'"
        )
        await conexao.close()
    assert await _avaliar(grafana, uid) == (2, 1)


def test_arquivo_das_regras_sem_macros_trocadas_pelo_provisionamento() -> None:
    """O provisionamento troca ``$NOME`` por variavel de ambiente: as consultas dos alertas nao usam ``$``."""
    regras = yaml.safe_load((PASTA_GRAFANA / "provisionamento" / "alerting" / "regras.yaml").read_text("utf-8"))
    for grupo in regras["groups"]:
        for regra in grupo["rules"]:
            for passo in regra["data"]:
                assert "$" not in str(passo["model"].get("rawSql", "")), regra["uid"]


@contextlib.asynccontextmanager
async def receptor_das_rotinas(g: Grafana) -> AsyncIterator[list[dict[str, Any]]]:
    """Receptor HTTPS falso das rotinas: exige certificado de cliente e confere a senha como as rotinas."""
    recebidos: list[dict[str, Any]] = []

    async def alertas(pedido: Request) -> JSONResponse:
        alertas_lidos, _ = ler_alertas(await pedido.json())
        recebidos.append(
            {
                "senha_ok": senha_confere(pedido.headers.get("authorization"), g.senha_webhook),
                "alertas": alertas_lidos,
            }
        )
        return JSONResponse({"aceitos": len(alertas_lidos)})

    app = Starlette(routes=[Route("/interno/alertas-grafana", alertas, methods=["POST"])])
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=g.porta_webhook,
        log_level="warning",
        lifespan="off",
        ssl_certfile=str(g.certificados.rotinas_cert),
        ssl_keyfile=str(g.certificados.rotinas_chave),
        ssl_ca_certs=str(g.certificados.ca_clientes),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
    srv = uvicorn.Server(config)
    tarefa = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield recebidos
    finally:
        srv.should_exit = True
        await tarefa


async def test_webhook_chega_as_rotinas_com_certificado_e_senha(grafana: Grafana) -> None:
    caminho = "/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers"
    async with grafana.cliente() as c:
        receptores = (await c.get(caminho)).json()["items"]
        rotinas = next(r for r in receptores if r["spec"]["title"] == "mcp-claro-rotinas")
        integracao = rotinas["spec"]["integrations"][0]
        assert integracao["type"] == "webhook"
        assert integracao["settings"]["url"].endswith("/interno/alertas-grafana")
        assert integracao["secureFields"] == {
            "password": True,
            "tlsConfig.caCertificate": True,
            "tlsConfig.clientCertificate": True,
            "tlsConfig.clientKey": True,
        }
        async with receptor_das_rotinas(grafana) as recebidos:
            r = await c.post(f"{caminho}/{rotinas['metadata']['name']}/test", json={"integration": integracao})
            for _ in range(50):
                if recebidos:
                    break
                await asyncio.sleep(0.1)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success", r.text
    assert len(recebidos) == 1
    assert recebidos[0]["senha_ok"] is True
    assert recebidos[0]["alertas"][0].situacao == "firing"

    # Sem o certificado de cliente do Grafana, o receptor recusa a conexao; com ele, aceita.
    endereco = f"https://127.0.0.1:{grafana.porta_webhook}/interno/alertas-grafana"
    segredos = grafana.certificados.ca_interna.parent
    async with receptor_das_rotinas(grafana) as recebidos_depois:
        sem = ssl.create_default_context(cafile=str(grafana.certificados.ca_interna))
        async with httpx2.AsyncClient(verify=sem, trust_env=False, timeout=5) as sem_certificado:
            with pytest.raises(httpx2.HTTPError):
                await sem_certificado.post(endereco, json={"alerts": []})
        com = ssl.create_default_context(cafile=str(grafana.certificados.ca_interna))
        com.load_cert_chain(segredos / "grafana_cliente_cert.pem", segredos / "grafana_cliente_key.pem")
        async with httpx2.AsyncClient(verify=com, trust_env=False, timeout=5) as com_certificado:
            resposta = await com_certificado.post(endereco, json={"alerts": []})
    assert resposta.status_code == 200
    assert recebidos_depois == [{"senha_ok": False, "alertas": []}]


async def test_grafana_nao_chama_nada_fora(grafana: Grafana) -> None:
    async with grafana.cliente() as c:
        ajustes = (await c.get("/api/admin/settings")).json()
    assert ajustes["analytics"]["reporting_enabled"] == "false"
    assert ajustes["analytics"]["check_for_updates"] == "false"
    assert ajustes["analytics"]["check_for_plugin_updates"] == "false"
    assert ajustes["plugins"]["plugin_admin_enabled"] == "false"
    assert ajustes["plugins"]["preinstall_disabled"] == "true"
    assert ajustes["plugins"]["preinstall_auto_update"] == "false"
    assert ajustes["plugins"]["public_key_retrieval_disabled"] == "true"
    assert ajustes["news"]["news_feed_enabled"] == "false"
    assert ajustes["snapshots"]["external_enabled"] == "false"
    assert ajustes["auth.anonymous"]["enabled"] == "false"
    assert ajustes["users"]["allow_sign_up"] == "false"
    # Nenhum erro no log, fora as consultas recusadas de proposito pelo teste do grafana_leitor e o aviso do
    # Grafana sobre o envio de teste do ponto de contato, que nao nasce de um alerta.
    esperados = ("logger=plugin.grafana-postgresql-datasource", 'msg="Missing notification reason"')
    saida, erros = grafana.logs()
    linhas = [
        linha
        for linha in (saida + erros).decode().splitlines()
        if "level=error" in linha and not any(e in linha for e in esperados)
    ]
    assert not linhas, linhas[:5]
