"""A sala de visitas (desenho 3.8; ajustes D1, A1 e M4; prova U9).

- mTLS: so o certificado de cliente do despachante (``ca_clientes_visitas``) entra; sem certificado, com o de outra
  autoridade ou com o da propria sala, a porta recusa e nenhum filho recebe nada;
- A1: cada chamada roda num processo filho novo; duas chamadas a terceiros diferentes ao mesmo tempo rodam em
  processos diferentes, e o processo de X nao recebe a chave de Y;
- o pai devolve o que o filho escreveu, sem ler; erro inesperado do filho, ou prazo da sala, vira 500 (duvida);
- o filho so avisa que esta pronto depois de se isolar, e isolado nao escreve no stdout nem no stderr da sala, nem
  com um cano herdado no fd 0, nem fala com o rastreador de recursos do ``multiprocessing``;
- os filhos nascem numa thread da sala que vive tanto quanto ela (``PR_SET_PDEATHSIG`` vale pela thread); o filho
  isolado tem ``PR_SET_PDEATHSIG`` com ``SIGKILL``, e fechar a sala mata o que ainda vive;
- o ambiente que vale e o da sala; enderecos locais so em desenvolvimento;
- partida: sem ``DATABASE_*``, ``DB_*``, ``VAULT_*`` nem outro segredo, so com mTLS e com ``APP_ENV`` escrito no
  ambiente (contrato K8; a recusa sem ele fica em ``test_r6e_ambiente_sala.py``).
"""

from __future__ import annotations

import contextlib
import errno
import functools
import json
import os
import select
import signal
import socket
import ssl
import sys
import threading
import time
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import anyio
import anyio._backends._asyncio
import anyio.to_thread
import httpx2
import pytest
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from exemplos import terceiro_falante
from exemplos.mal_comportado import ler_corpo, repetir_corpo
from mcp_claro import __main__ as partida
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.ligacao import cliente, sala_contrato
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.papeis import Papel
from mcp_claro.visitas import app as visitas_app
from mcp_claro.visitas import filho, saude
from mcp_claro.visitas.partida import conferir_sala, problemas_da_sala
from mcp_claro.visitas.tls import contexto_do_despachante
from tests import apoio_visitas
from tests.apoio import porta_livre, servidor
from tests.apoio_visitas import Certificados

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def certificados(tmp_path_factory: pytest.TempPathFactory) -> Certificados:
    return apoio_visitas.gerar_certificados(tmp_path_factory.mktemp("certificados_visitas"))


@pytest.fixture
def espiao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pasta onde cada filho espiao grava o que recebeu (o filho herda o ambiente do pai)."""
    pasta = tmp_path / "espiao"
    pasta.mkdir()
    monkeypatch.setenv(apoio_visitas.ESPIAO, str(pasta))
    return pasta


def _parametros(c: Certificados, porta: int, ambiente: str = "desenvolvimento", **extra: Any) -> Parametros:
    return Parametros(
        APP_ENV=ambiente,
        VISITS_INTERNAL_PORT=porta,
        VISITS_INTERNAL_URL=f"https://localhost:{porta}",
        VISITS_CA_FILE=str(c.ca_visitas),
        VISITS_CLIENT_CERT_FILE=str(c.despachante_cert),
        VISITS_CLIENT_KEY_FILE=str(c.despachante_key),
        **extra,
    )


def _tls_da_sala(c: Certificados) -> dict[str, Any]:
    """As opcoes de TLS que a partida da sala da ao uvicorn (``__main__.opcoes_tls``), com os certificados de teste."""
    opcoes = partida.opcoes_tls(_com_tls(c))
    assert opcoes["ssl_cert_reqs"] is ssl.CERT_REQUIRED
    return opcoes


@contextlib.asynccontextmanager
async def _sala_no_ar(
    c: Certificados, ambiente: str = "desenvolvimento", **opcoes: Any
) -> AsyncIterator[tuple[ClienteSala, Parametros, visitas_app.Sala]]:
    porta = porta_livre()
    p = _parametros(c, porta, ambiente)
    sala = visitas_app.Sala(p, **opcoes)
    async with servidor(visitas_app.criar_app_visitas(p, sala=sala), porta, **_tls_da_sala(c)):
        yield ClienteSala(str(p.VISITS_INTERNAL_URL), tls=contexto_do_despachante(p)), p, sala


def _chave_de(texto: str) -> Any:
    async def chave() -> str:
        return texto

    return chave


def _pedidos_do_espiao(pasta: Path) -> dict[int, bytes]:
    return {int(a.stem): a.read_bytes() for a in pasta.glob("*.pedido")}


def _vivos_do_espiao(pasta: Path) -> list[int]:
    return [int(a.stem) for a in pasta.glob("*.vivo")]


def _pids_com(pasta: Path, sufixo: str) -> list[int]:
    return sorted(int(a.stem) for a in pasta.glob(f"*.{sufixo}"))


def _vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/status").exists() and "zombie" not in Path(f"/proc/{pid}/status").read_text()


# --- caminho feliz, com o filho de verdade ------------------------------------------------------------------------


async def test_chamar_listar_e_sondar_pela_sala_com_o_certificado_do_despachante(certificados: Certificados) -> None:
    falante = terceiro_falante.criar()
    async with servidor(falante) as base, _sala_no_ar(certificados) as (cs, _, _):
        url = f"{base}/mcp"
        chave = _chave_de("chave-ana")
        s = await cs.sondar(url, chave, prazo=5)
        nomes = [f["name"] for f in await cs.listar_ferramentas(url, chave, s.discover_json)]
        r = await cs.chamar_ferramenta(url, chave, s.discover_json, "ler_mural", {"assunto": "x"}, prazo=5)
    assert (s.desfecho, nomes, r.tipo, r.status_http) == ("aceito", ["ler_mural", "apagar_mural"], "resultado", (200,))
    assert {dict(p["cabecalhos"])["authorization"] for p in falante.state.recebidos} == {"Bearer chave-ana"}
    assert {dict(p["cabecalhos"])["user-agent"] for p in falante.state.recebidos} == {"mcp-claro"}


# --- mTLS: so o despachante entra -----------------------------------------------------------------------------------


def _pedir_cru(porta: int, contexto: ssl.SSLContext) -> bytes:
    """Abre TLS com a sala e manda um pedido de chamada; devolve o que voltar (vazio se a sala fechar).

    Sem certificado aceito, a recusa chega como erro de TLS, conexao derrubada ou fim sem resposta (b"")."""
    try:
        return _pedir_cru_ou_falhar(porta, contexto)
    except ssl.SSLError, ConnectionError:
        return b""


def _pedir_cru_ou_falhar(porta: int, contexto: ssl.SSLContext) -> bytes:
    corpo = sala_contrato.PedidoChamar(endereco="http://127.0.0.1:9/mcp", nome="x", argumentos={}).para_json()
    pedido = (
        f"POST {sala_contrato.ROTA_CHAMAR} HTTP/1.1\r\nhost: localhost\r\ncontent-type: application/json\r\n"
        f"content-length: {len(corpo)}\r\nconnection: close\r\n\r\n"
    ).encode() + corpo
    with (
        socket.create_connection(("127.0.0.1", porta), timeout=5) as bruto,
        contexto.wrap_socket(bruto, server_hostname="localhost") as tls,
    ):
        tls.sendall(pedido)
        recebido = b""
        while pedaco := tls.recv(65536):
            recebido += pedaco
        return recebido


def _contexto_cliente(c: Certificados, cert: Path | None = None, chave: Path | None = None) -> ssl.SSLContext:
    contexto = ssl.create_default_context(cafile=str(c.ca_visitas))
    if cert is not None and chave is not None:
        contexto.load_cert_chain(str(cert), str(chave))
    return contexto


@pytest.mark.parametrize("quem", ["sem_certificado", "outra_autoridade", "certificado_da_sala"])
async def test_porta_da_sala_recusa_quem_nao_e_o_despachante(
    certificados: Certificados, espiao: Path, quem: str
) -> None:
    c = certificados
    contexto = {
        "sem_certificado": lambda: _contexto_cliente(c),
        "outra_autoridade": lambda: _contexto_cliente(c, c.outro_cert, c.outro_key),
        "certificado_da_sala": lambda: _contexto_cliente(c, c.visitas_cert, c.visitas_key),
    }[quem]()
    async with _sala_no_ar(c, alvo=apoio_visitas.espiao) as (_, p, _):
        recusado = await anyio.to_thread.run_sync(_pedir_cru, p.VISITS_INTERNAL_PORT, contexto)
        assert recusado == b""
        assert _pedidos_do_espiao(espiao) == {}
        # O despachante, com o certificado dele, entra.
        certo = _contexto_cliente(c, c.despachante_cert, c.despachante_key)
        resposta = await anyio.to_thread.run_sync(_pedir_cru, p.VISITS_INTERNAL_PORT, certo)
    assert resposta.startswith(b"HTTP/1.1 200")
    assert len(_pedidos_do_espiao(espiao)) == 1  # so o pedido do despachante chegou a um filho


async def test_despachante_confia_so_na_autoridade_da_sala(certificados: Certificados) -> None:
    """Uma "sala" com certificado de outra autoridade: o despachante nao conecta, e nada sai (``nao_enviado``)."""
    c = certificados
    porta = porta_livre()
    p = _parametros(c, porta)
    falsa = Starlette()
    tls = _tls_da_sala(c) | {"ssl_certfile": str(c.outro_cert), "ssl_keyfile": str(c.outro_key)}
    async with servidor(falsa, porta, **tls):
        cs = ClienteSala(str(p.VISITS_INTERNAL_URL), tls=contexto_do_despachante(p))
        r = await cs.chamar_ferramenta("https://t.exemplo/mcp", _chave_de("k"), None, "x", {})
    assert (r.tipo, r.excecao) == ("nao_enviado", sala_contrato.SALA_FORA)


def test_contexto_do_despachante() -> None:
    assert contexto_do_despachante(Parametros()) is None
    assert contexto_do_despachante(Parametros(VISITS_INTERNAL_URL="http://127.0.0.1:9446")) is None
    with pytest.raises(ConfiguracaoInvalida, match="VISITS_CA_FILE, VISITS_CLIENT_CERT_FILE, VISITS_CLIENT_KEY_FILE"):
        contexto_do_despachante(Parametros(VISITS_INTERNAL_URL="https://visitas:9446"))


def test_contexto_do_despachante_nao_carrega_autoridades_do_sistema(certificados: Certificados) -> None:
    contexto = contexto_do_despachante(_parametros(certificados, 9446))
    assert contexto is not None
    assert contexto.check_hostname is True
    assert contexto.verify_mode is ssl.CERT_REQUIRED
    assert [a["subject"] for a in contexto.get_ca_certs()] == [((("commonName", "ca_visitas"),),)]


# --- A1: um processo por chamada --------------------------------------------------------------------------------


class _Encontro:
    """Segura cada ``tools/call`` ate os dois terceiros receberem o seu: prova que as chamadas correm juntas."""

    def __init__(self) -> None:
        self.chegaram: set[str] = set()
        self.juntos: dict[str, bool] = {}
        self.evento = anyio.Event()

    def envolver(self, nome: str, interno: ASGIApp) -> ASGIApp:
        async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http" or scope.get("path") != "/mcp" or scope.get("method") != "POST":
                await interno(scope, receive, send)
                return
            corpo = await ler_corpo(receive)
            if b'"tools/call"' in corpo:
                self.chegaram.add(nome)
                if len(self.chegaram) == 2:
                    self.evento.set()
                with anyio.move_on_after(15):
                    await self.evento.wait()
                self.juntos[nome] = self.evento.is_set()
            await interno(scope, repetir_corpo(corpo, receive), send)

        return asgi


async def test_duas_chamadas_a_terceiros_diferentes_ao_mesmo_tempo_rodam_em_processos_diferentes(
    certificados: Certificados, espiao: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, apoio_visitas.SO_DO_PAI, types.ModuleType(apoio_visitas.SO_DO_PAI))
    encontro = _Encontro()
    x, y = terceiro_falante.criar(), terceiro_falante.criar()
    chaves = {"x": "chave-ana", "y": "chave-bia"}  # as chaves que os dubles aceitam
    resultados: dict[str, cliente.Resposta] = {}
    async with (
        servidor(encontro.envolver("x", x)) as base_x,
        servidor(encontro.envolver("y", y)) as base_y,
        _sala_no_ar(certificados, alvo=apoio_visitas.espiao) as (cs, _, _),
    ):

        async def chamar(nome: str, base: str) -> None:
            resultados[nome] = await cs.chamar_ferramenta(
                f"{base}/mcp", _chave_de(chaves[nome]), None, "ler_mural", {"assunto": nome}, prazo=20
            )

        async with anyio.create_task_group() as tarefas:
            tarefas.start_soon(chamar, "x", base_x)
            tarefas.start_soon(chamar, "y", base_y)
    assert {n: r.tipo for n, r in resultados.items()} == {"x": "resultado", "y": "resultado"}
    # As duas chamadas estavam com os terceiros ao mesmo tempo.
    assert encontro.juntos == {"x": True, "y": True}

    pedidos = _pedidos_do_espiao(espiao)
    assert len(pedidos) == 2, "uma chamada, um processo"
    por_terceiro = {("x" if chaves["x"].encode() in dados else "y"): pid for pid, dados in pedidos.items()}
    assert set(por_terceiro) == {"x", "y"}
    assert por_terceiro["x"] != por_terceiro["y"]
    assert os.getpid() not in por_terceiro.values()  # nenhum dos dois e o processo pai da sala
    for nome, outro in (("x", "y"), ("y", "x")):
        dados = pedidos[por_terceiro[nome]]
        # O processo de X recebeu so a chamada de X: a chave, o endereco e os argumentos dele, nada de Y.
        assert dados.startswith(sala_contrato.ROTA_CHAMAR.encode() + b"\n")
        assert chaves[nome].encode() in dados
        assert chaves[outro].encode() not in dados
        assert {"x": base_x, "y": base_y}[outro].encode() not in dados
        processo = json.loads((espiao / f"{por_terceiro[nome]}.processo").read_text())
        # Interpretador novo (spawn): nada da memoria do pai, nem os modulos que so o pai carregou.
        assert processo == {
            "pai": os.getpid(),
            "ambiente": "desenvolvimento",
            "liberar_locais": True,
            "modulos_do_pai": [],
        }
        if filho.depuravel() is not None:  # Linux: o filho nao deixa outro processo ler a memoria dele
            assert json.loads((espiao / f"{por_terceiro[nome]}.fim").read_text()) == {"depuravel": False}
        assert not _vivo(por_terceiro[nome]), "o processo da chamada termina com ela"
    for nome, terceiro in (("x", x), ("y", y)):
        autorizacoes = {dict(p["cabecalhos"]).get("authorization") for p in terceiro.state.recebidos}
        assert autorizacoes == {f"Bearer {chaves[nome]}"}


async def test_filhos_prontos_nao_tem_dado_de_chamada_e_cada_um_atende_uma_so(
    certificados: Certificados, espiao: Path
) -> None:
    falante = terceiro_falante.criar()
    async with servidor(falante) as base, _sala_no_ar(certificados, alvo=apoio_visitas.espiao) as (cs, _, sala):
        for _ in range(3):
            r = await cs.chamar_ferramenta(f"{base}/mcp", _chave_de("chave-ana"), None, "ler_mural", {"assunto": "a"})
            assert r.tipo == "resultado"
        with anyio.fail_after(30):
            while len(sala._prontos) < visitas_app.PRONTOS:  # noqa: ASYNC110 - espera a reposicao da sala
                await anyio.sleep(0.05)
        prontos = [f.processo.pid for f in sala._prontos]
    pedidos = _pedidos_do_espiao(espiao)
    assert len(pedidos) == 3
    assert not set(prontos) & set(pedidos)  # os prontos ainda nao receberam nada
    assert all(not _vivo(pid) for pid in [*pedidos, *(p for p in prontos if p is not None)])


# --- a janela antes do isolamento de um filho recem-iniciado ------------------------------------------------------


async def test_filho_recem_iniciado_so_recebe_o_pedido_depois_de_avisar_que_se_isolou(espiao: Path) -> None:
    """Sob rajada (sem filho pronto), o pai espera o aviso do filho antes de entregar o pedido com a chave."""
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.lento_antes_de_isolar, prontos=0)
    async with sala.ligada():
        atendimento = await sala.atender(sala_contrato.ROTA_CHAMAR, b'{"chave":"chave-da-bia"}')
    assert (atendimento.status, atendimento.corpo, atendimento.desfecho) == (200, b'{"recebido":24}', "filho")
    [pid] = _pids_com(espiao, "antes")
    assert json.loads((espiao / f"{pid}.antes").read_text()) == {"pedido_antes_de_isolar": False}
    assert (espiao / f"{pid}.pedido").read_bytes() == b'/interno/visitas/chamar\n{"chave":"chave-da-bia"}'


async def test_o_filho_so_avisa_que_esta_pronto_depois_de_se_isolar(espiao: Path) -> None:
    """Dentro do ``filho.principal`` de verdade: o aviso ``PRONTO`` vem depois do ``_isolar()``, e no momento do aviso
    o processo ja nao pode ser depurado por outro do mesmo usuario."""
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.lento_antes_de_isolar, prontos=0)
    async with sala.ligada():
        atendimento = await sala.atender(sala_contrato.ROTA_CHAMAR, b'{"chave":"k"}')
    assert (atendimento.status, atendimento.desfecho) == (200, "filho")
    [pid] = _pids_com(espiao, "ordem")
    passos = [json.loads(linha) for linha in (espiao / f"{pid}.ordem").read_text().splitlines()]
    assert [passo["passo"] for passo in passos] == ["isolou", "aviso"]
    if filho.depuravel() is not None:  # Linux
        assert passos[1]["depuravel"] is False


async def test_filho_tomado_nao_alcanca_o_cano_nem_a_memoria_de_um_irmao_que_ainda_nao_se_isolou(
    espiao: Path,
) -> None:
    """A1, a janela de partida: um filho tomado por um terceiro X (ja isolado) vigia o ``/proc`` e tenta, como o
    mesmo usuario, abrir os canos e a memoria de um filho novo antes de ele se isolar, para ler a chave de Y."""
    sala = visitas_app.Sala(
        Parametros(APP_ENV="teste"), alvo=apoio_visitas.lento_antes_de_isolar, prontos=0, teto_chamada=30
    )
    segredo = b"chave-da-bia-" + os.urandom(8).hex().encode()
    resultados: dict[str, visitas_app.Atendimento] = {}
    async with sala.ligada():

        async def espiar() -> None:
            resultados["x"] = await sala.atender(sala_contrato.ROTA_CHAMAR, apoio_visitas.PEDIDO_DE_ESPIAR)

        async with anyio.create_task_group() as tarefas:
            tarefas.start_soon(espiar)
            with anyio.fail_after(30):
                while not (espiao / "espiao_pronto").exists():  # noqa: ASYNC110 - espera o filho tomado
                    await anyio.sleep(0.02)
            resultados["y"] = await sala.atender(sala_contrato.ROTA_CHAMAR, b'{"chave":"' + segredo + b'"}')
    assert resultados["x"].status == 200
    visto = json.loads(resultados["x"].corpo)
    # Nada da chamada de Y chegou ao filho de X.
    assert segredo not in bytes.fromhex(visto["lidos"])
    if filho.versao_landlock() >= 1:
        assert (resultados["y"].status, resultados["y"].desfecho) == (200, "filho")
        tomado = int((espiao / "espiao_pronto").read_text())
        [vitima] = [pid for pid in _pids_com(espiao, "isolou") if pid != tomado]
        # O filho tomado viu o novo na janela: antes de ele se isolar.
        assert str(vitima) in visto["tentados"]
        assert visto["tentados"][str(vitima)] < float((espiao / f"{vitima}.isolou").read_text())
        assert json.loads((espiao / f"{vitima}.antes").read_text()) == {"pedido_antes_de_isolar": False}
        assert (espiao / f"{vitima}.pedido").read_bytes().endswith(segredo + b'"}')
        # Cada filho isolado tem o seu dominio: nao abre nada de um irmao (nem do pai), nem o que e publico so
        # para o mesmo usuario (o cano, a memoria).
        assert visto["abertos"] == []
        assert visto["recusas"] == ["EACCES"]
    if filho.versao_landlock() >= 6:
        # Nem sinal: o filho tomado nao derruba um irmao nem o pai.
        assert set(visto["sinais"].values()) == {"EPERM"}


async def test_filho_que_morre_antes_de_se_isolar_e_trocado_e_nada_vai_a_ele(espiao: Path) -> None:
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.nao_se_isola_na_primeira, prontos=0)
    async with sala.ligada():
        trocado = await sala.atender(sala_contrato.ROTA_CHAMAR, b'{"chave":"k"}')
    assert (trocado.status, trocado.desfecho) == (200, "filho")
    assert len(_pids_com(espiao, "pedido")) == 1
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.nao_se_isola, prontos=0)
    async with sala.ligada():
        perdido = await sala.atender(sala_contrato.ROTA_CHAMAR, b'{"chave":"k"}')
    assert (perdido.status, perdido.corpo, perdido.desfecho) == (500, visitas_app._ERRO, "filho_nao_isolou")


def test_os_canos_do_pai_com_o_filho_nao_se_reabrem_pelo_proc() -> None:
    """Os canos entre o pai e o filho sao soquetes: ``/proc/<pid>/fd/N`` de um soquete nao abre (ENXIO), mesmo para
    o mesmo usuario e antes de o filho se isolar; o de um cano comum abriria e deixaria ler o pedido depois."""
    if not sys.platform.startswith("linux"):
        pytest.skip("o /proc e do Linux")
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.nao_se_isola, prontos=0)
    novo = sala._iniciar()
    try:
        for ponta in (novo.pedidos, novo.respostas):
            with pytest.raises(OSError, match=os.strerror(errno.ENXIO)) as recusa:
                os.open(f"/proc/self/fd/{ponta.fileno()}", os.O_RDONLY | os.O_NONBLOCK)
            assert recusa.value.errno == errno.ENXIO
    finally:
        novo.encerrar()


@contextlib.contextmanager
def _cano_na_entrada() -> Iterator[None]:
    """Poe um cano no fd 0 deste processo, e no fim devolve o de antes: o filho iniciado aqui herda algo que nao e
    ``/dev/null`` (sob o pytest, o fd 0 ja e ``/dev/null``, e um filho que nao trocasse o 0 passaria igual)."""
    leitura, escrita = os.pipe()
    antes = os.dup(0)
    try:
        os.dup2(leitura, 0)
        yield
    finally:
        os.dup2(antes, 0)
        for fd in (antes, leitura, escrita):
            os.close(fd)


async def test_filho_isolado_nao_escreve_na_saida_da_sala_e_a_chamada_responde(
    espiao: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Depois de ``_isolar()``, 0, 1 e 2 apontam para ``/dev/null`` (o 0 herdado e um cano), o cano do rastreador de
    recursos do ``multiprocessing`` (herdado do pai) fica fechado e o rastreador nao volta a ser iniciado: um filho
    tomado nao escreve no log da sala nem fala com o rastreador."""
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.escreve_depois_de_isolar, prontos=0)
    capfd.readouterr()
    with _cano_na_entrada():
        async with sala.ligada():
            atendimento = await sala.atender(sala_contrato.ROTA_CHAMAR, b"{}")
    capturado = capfd.readouterr()
    assert (atendimento.status, atendimento.desfecho) == (200, "filho")
    assert apoio_visitas.MARCA_NA_SAIDA.decode() not in capturado.out + capturado.err
    vistos = json.loads(atendimento.corpo)
    assert vistos["descritores_antes"]["0"] == "outro"  # o filho herdou o cano: a troca do 0 e do filho
    vistos.pop("pdeathsig")  # o teste da thread criadora confere
    assert vistos == {
        "descritores_antes": vistos["descritores_antes"],
        "descritores": {"0": "devnull", "1": "devnull", "2": "devnull"},
        "rastreador_herdado": True,
        "rastreador_fechado": True,
        "rastreador_agora": None,
        "rastreador_volta": "recusado",
    }


async def _atender_com_resposta(saida: bytes) -> visitas_app.Atendimento:
    alvo = functools.partial(apoio_visitas.responde_cru, saida)
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=alvo, prontos=0)
    async with sala.ligada():
        return await sala.atender(sala_contrato.ROTA_CHAMAR, b"{}")


@pytest.mark.parametrize("saida", [b"999{}", b"000{}", b"600{}", b"099{}", b"20{}"])
async def test_status_de_tres_digitos_fora_de_100_a_599_vira_500_resposta_invalida(saida: bytes) -> None:
    atendimento = await _atender_com_resposta(saida)
    assert (atendimento.status, atendimento.corpo, atendimento.desfecho) == (
        500,
        visitas_app._ERRO,
        "filho_resposta_invalida",
    )


@pytest.mark.parametrize("saida", [b"100{}", b"599{}", b"2000"])
async def test_status_nas_pontas_da_faixa_passa_sem_o_pai_ler_o_corpo(saida: bytes) -> None:
    atendimento = await _atender_com_resposta(saida)
    assert (atendimento.status, atendimento.corpo, atendimento.desfecho) == (int(saida[:3]), saida[3:], "filho")


# --- os filhos nascem numa thread que vive tanto quanto a sala -----------------------------------------------------


async def test_filhos_nascem_sempre_na_thread_criadora_da_sala(monkeypatch: pytest.MonkeyPatch, espiao: Path) -> None:
    """``PR_SET_PDEATHSIG`` vale pela thread que criou o filho, nao pelo processo. Todo filho (os prontos da
    reposicao, o da chamada sem pronto e o que troca um filho que nao se isolou) nasce na thread criadora da sala,
    aberta em ``Sala.ligada()`` e fechada no fim; nunca numa thread do anyio, que o anyio aposenta quando ociosa."""
    onde: list[threading.Thread] = []
    iniciar = visitas_app.Sala._iniciar

    def anotar(self: visitas_app.Sala) -> Any:
        onde.append(threading.current_thread())
        return iniciar(self)

    monkeypatch.setattr(visitas_app.Sala, "_iniciar", anotar)
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.nao_se_isola_na_primeira, prontos=1)
    resultados: list[visitas_app.Atendimento] = []

    async def chamar() -> None:
        resultados.append(await sala.atender(sala_contrato.ROTA_CHAMAR, b'{"chave":"k"}'))

    async with sala.ligada():
        async with anyio.create_task_group() as tarefas:
            for _ in range(3):
                tarefas.start_soon(chamar)
        await chamar()
    assert [(r.status, r.desfecho) for r in resultados] == [(200, "filho")] * 4
    assert len(onde) >= 5  # o primeiro pronto, o que o trocou, os das chamadas sem pronto e a reposicao
    assert not [t.name for t in onde if t.name.startswith("AnyIO worker thread")]
    [criadora] = set(onde)
    assert criadora.name.startswith(visitas_app.CRIADORA)
    assert criadora is not threading.main_thread()
    assert not criadora.is_alive()  # fechada no fim da sala


async def test_filho_isolado_morre_com_a_thread_que_o_criou(espiao: Path) -> None:
    """Depois de ``_isolar()``, o filho tem ``PR_SET_PDEATHSIG`` com ``SIGKILL``: morre quando morre a thread da sala
    que o criou (``PR_GET_PDEATHSIG`` le o sinal guardado no nucleo)."""
    if not sys.platform.startswith("linux"):
        pytest.skip("PR_SET_PDEATHSIG e do Linux")
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.escreve_depois_de_isolar, prontos=0)
    async with sala.ligada():
        atendimento = await sala.atender(sala_contrato.ROTA_CHAMAR, b"{}")
    assert (atendimento.status, atendimento.desfecho) == (200, "filho")
    assert json.loads(atendimento.corpo)["pdeathsig"] == signal.SIGKILL


async def test_fechar_a_sala_mata_o_filho_isolado_que_ainda_vive_fora_dos_prontos() -> None:
    """Um filho de verdade (``filho.principal``), ja isolado, a espera de um pedido que nao vem, e que nao esta em
    ``_prontos`` (a sala nao o encerra no fim): ao sair de ``ligada()``, a thread criadora fecha e o nucleo mata o
    filho com ``SIGKILL``. Sem o ``PR_SET_PDEATHSIG``, ele ficaria vivo, preso na leitura do pedido."""
    if not sys.platform.startswith("linux"):
        pytest.skip("PR_SET_PDEATHSIG e do Linux")
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), prontos=0)
    soltos: list[visitas_app._Filho] = []
    try:
        async with sala.ligada():
            await sala._novo(soltos)  # nasce na thread criadora, fora dos prontos
            [solto] = soltos
            assert await solto.esperar_isolamento()
            assert solto.processo.is_alive()
            assert solto not in sala._prontos
        await anyio.to_thread.run_sync(solto.processo.join, 15.0)
        assert solto.processo.exitcode == -signal.SIGKILL
    finally:
        for filho_solto in soltos:
            await anyio.to_thread.run_sync(filho_solto.encerrar)


async def test_filho_nao_morre_quando_o_anyio_aposenta_uma_thread_ociosa(
    monkeypatch: pytest.MonkeyPatch, espiao: Path
) -> None:
    """Com o intervalo ocioso do anyio curto (0,2 s), threads do anyio vem e vao durante as chamadas. Nenhum filho
    morre por isso: em cada rodada, duas chamadas longas (cada uma num filho que demora 1 s) estao em andamento no
    filho, ja isolado, quando chega outra, depois do intervalo ocioso, que faz o anyio aposentar as threads ociosas.

    Sem prontos, cada chamada inicia o seu filho; as duas longas, ao mesmo tempo, em duas threads diferentes. Com os
    filhos criados em threads do anyio, a chamada que chega aposentava a thread de uma das duas longas, e o filho
    dela morria no meio (500, ``filho_sem_resposta``): uma por rodada."""
    monkeypatch.setattr(anyio._backends._asyncio.WorkerThread, "MAX_IDLE_TIME", 0.2)
    sala = visitas_app.Sala(Parametros(APP_ENV="teste"), alvo=apoio_visitas.atende_devagar, prontos=0, teto_chamada=60)
    resultados: list[tuple[str, visitas_app.Atendimento]] = []

    async def chamar(nome: str, corpo: bytes) -> None:
        resultados.append((nome, await sala.atender(sala_contrato.ROTA_CHAMAR, corpo)))

    def longas_em_andamento() -> int:
        return sum((espiao / f"{pid}.dormindo").read_text() == "1.0" for pid in _pids_com(espiao, "dormindo"))

    async with sala.ligada():
        # Varias threads do anyio ao mesmo tempo; depois, todas ociosas por mais que o intervalo.
        async with anyio.create_task_group() as tarefas:
            for _ in range(8):
                tarefas.start_soon(anyio.to_thread.run_sync, time.sleep, 0.05)
        await anyio.sleep(0.5)
        for rodada in range(1, 11):
            async with anyio.create_task_group() as tarefas:
                for _ in range(2):
                    tarefas.start_soon(chamar, "longa", b'{"dormir":1.0}')
                with anyio.fail_after(60):  # as duas longas ja estao no filho isolado (sem relogio de parede)
                    while longas_em_andamento() < 2 * rodada:  # noqa: ASYNC110 - espera o filho
                        await anyio.sleep(0.02)
                await anyio.sleep(0.3)  # mais que o intervalo ocioso
                tarefas.start_soon(chamar, "depois", b'{"dormir":0.0}')
    assert len(resultados) == 30
    assert [(nome, r.status, r.desfecho) for nome, r in resultados if (r.status, r.desfecho) != (200, "filho")] == []


async def test_o_pai_devolve_os_bytes_do_filho_sem_ler(certificados: Certificados) -> None:
    async with _sala_no_ar(certificados, alvo=apoio_visitas.responde_bytes_crus) as (cs, p, _):
        contexto = contexto_do_despachante(p)
        async with httpx2.AsyncClient(verify=contexto) as http:
            resposta = await http.post(f"{p.VISITS_INTERNAL_URL}{sala_contrato.ROTA_CHAMAR}", content=b"{}")
        r = await cs.chamar_ferramenta("https://t.exemplo/mcp", _chave_de("k"), None, "x", {})
    assert (resposta.status_code, resposta.content) == (200, apoio_visitas.BYTES_CRUS)
    # Quem confere o contrato e o despachante: fora dele, duvida.
    assert (r.tipo, r.excecao) == ("duvida", sala_contrato.SALA_RESPOSTA_INVALIDA)


@pytest.mark.parametrize(
    "alvo",
    [apoio_visitas.sai_sem_resposta, apoio_visitas.levanta, apoio_visitas.responde_status_invalido],
    ids=["sai_sem_resposta", "levanta", "status_invalido"],
)
async def test_erro_inesperado_do_filho_vira_500_e_o_despachante_le_duvida(
    certificados: Certificados, alvo: Any
) -> None:
    async with _sala_no_ar(certificados, alvo=alvo) as (cs, p, _):
        async with httpx2.AsyncClient(verify=contexto_do_despachante(p)) as http:
            resposta = await http.post(f"{p.VISITS_INTERNAL_URL}{sala_contrato.ROTA_SONDAR}", content=b"{}")
        r = await cs.chamar_ferramenta("https://t.exemplo/mcp", _chave_de("k"), None, "x", {})
        s = await cs.sondar("https://t.exemplo/mcp", _chave_de("k"))
    assert resposta.status_code == 500
    assert b"t.exemplo" not in resposta.content
    assert (r.tipo, r.excecao) == ("duvida", sala_contrato.SALA_RESPOSTA_INVALIDA)
    assert (s.desfecho, s.excecao) == ("inalcancavel", sala_contrato.SALA_RESPOSTA_INVALIDA)


async def _prontos_isolados(sala: visitas_app.Sala) -> None:
    """Espera os filhos prontos avisarem que se isolaram (o aviso fica no cano ate a chamada o ler). Assim, o prazo
    de um teste conta so o atendimento, e nao a partida de um filho numa maquina carregada."""
    with anyio.fail_after(60):
        while len(sala._prontos) < visitas_app.PRONTOS or not all(  # noqa: ASYNC110 - espera a reposicao
            select.select([f.respostas.fileno()], [], [], 0)[0] for f in sala._prontos
        ):
            await anyio.sleep(0.05)


async def test_prazo_da_sala_esgotado_mata_o_filho_e_vira_500(certificados: Certificados, espiao: Path) -> None:
    teto = 3.0
    async with _sala_no_ar(certificados, alvo=apoio_visitas.dorme, teto_chamada=teto) as (_, p, sala):
        await _prontos_isolados(sala)
        inicio = anyio.current_time()
        async with httpx2.AsyncClient(verify=contexto_do_despachante(p), timeout=60) as http:
            resposta = await http.post(f"{p.VISITS_INTERNAL_URL}{sala_contrato.ROTA_CHAMAR}", content=b"{}")
        demora = anyio.current_time() - inicio
    assert resposta.status_code == 500
    # Foi o prazo da sala: nem antes dele, nem os 120 s em que o filho dormiria, nem o prazo do cliente (60 s).
    assert teto <= demora < teto + 15
    vivos = _vivos_do_espiao(espiao)
    assert len(vivos) == 1  # o filho recebeu o pedido antes do prazo
    assert not any(_vivo(pid) for pid in vivos)


async def test_despachante_que_desiste_derruba_o_filho_da_chamada(certificados: Certificados, espiao: Path) -> None:
    async with _sala_no_ar(certificados, alvo=apoio_visitas.dorme) as (_, p, sala):
        await _prontos_isolados(sala)
        async with httpx2.AsyncClient(verify=contexto_do_despachante(p), timeout=2) as http:
            with pytest.raises(httpx2.ReadTimeout):
                await http.post(f"{p.VISITS_INTERNAL_URL}{sala_contrato.ROTA_CHAMAR}", content=b"{}")
        with anyio.fail_after(15):
            while not (vivos := _vivos_do_espiao(espiao)) or any(_vivo(pid) for pid in vivos):  # noqa: ASYNC110
                await anyio.sleep(0.1)


async def test_pedido_grande_demais_nao_chega_a_filho(certificados: Certificados, espiao: Path) -> None:
    c = certificados
    porta = porta_livre()
    p = _parametros(c, porta, MAX_REQUEST_BYTES=1024)
    sala = visitas_app.Sala(p, alvo=apoio_visitas.espiao)
    async with (
        servidor(visitas_app.criar_app_visitas(p, sala=sala), porta, **_tls_da_sala(c)),
        httpx2.AsyncClient(verify=contexto_do_despachante(p)) as http,
    ):
        grande = b"x" * (visitas_app.teto_pedido(p) + 1)
        respostas = {
            rota: await http.post(f"{p.VISITS_INTERNAL_URL}{rota}", content=grande) for rota in visitas_app.ROTAS
        }
    assert _pedidos_do_espiao(espiao) == {}
    assert sala_contrato.ler_resposta_chamar(200, respostas[sala_contrato.ROTA_CHAMAR].content).excecao == (
        sala_contrato.PEDIDO_FORA_DO_CONTRATO
    )
    s = sala_contrato.ler_resposta_sondar(200, respostas[sala_contrato.ROTA_SONDAR].content)
    assert (s.desfecho, s.excecao) == ("inalcancavel", sala_contrato.PEDIDO_FORA_DO_CONTRATO)
    with pytest.raises(sala_contrato.FalhaNaSala) as falha:
        sala_contrato.ler_resposta_listar(200, respostas[sala_contrato.ROTA_LISTAR].content)
    assert (falha.value.tipo, falha.value.excecao) == ("nao_enviado", sala_contrato.PEDIDO_FORA_DO_CONTRATO)


# --- ambiente da sala ---------------------------------------------------------------------------------------------


def test_liberacao_de_locais_so_em_desenvolvimento() -> None:
    assert visitas_app.Sala(Parametros(APP_ENV="desenvolvimento")).liberar_locais is True
    for ambiente in ("teste", "piloto", "producao"):
        sala = visitas_app.Sala(Parametros(APP_ENV=ambiente))
        assert (sala.ambiente, sala.liberar_locais) == (ambiente, False)


async def test_o_ambiente_que_vale_e_o_da_sala(certificados: Certificados) -> None:
    """Na sala em teste, um terceiro em 127.0.0.1 e recusado antes de conectar; em desenvolvimento, e atendido. O
    pedido nao tem como escolher o ambiente: campo a mais e pedido fora do contrato."""
    falante = terceiro_falante.criar()
    async with servidor(falante) as base:
        url = f"{base}/mcp"
        async with _sala_no_ar(certificados, "teste") as (cs, p, _):
            recusada = await cs.chamar_ferramenta(url, _chave_de("chave-ana"), None, "ler_mural", {"assunto": "a"})
            pedido = json.loads(sala_contrato.PedidoChamar(endereco=url, nome="ler_mural", argumentos={}).para_json())
            async with httpx2.AsyncClient(verify=contexto_do_despachante(p)) as http:
                com_ambiente = await http.post(
                    f"{p.VISITS_INTERNAL_URL}{sala_contrato.ROTA_CHAMAR}",
                    json=pedido | {"ambiente": "desenvolvimento", "liberar_locais": True},
                )
        assert falante.state.recebidos == []
        async with _sala_no_ar(certificados, "desenvolvimento") as (cs, _, _):
            atendida = await cs.chamar_ferramenta(url, _chave_de("chave-ana"), None, "ler_mural", {"assunto": "a"})
    assert (recusada.tipo, recusada.excecao) == ("nao_enviado", cliente.ENDERECO_RECUSADO)
    r = sala_contrato.ler_resposta_chamar(com_ambiente.status_code, com_ambiente.content)
    assert (r.tipo, r.excecao) == ("nao_enviado", sala_contrato.PEDIDO_FORA_DO_CONTRATO)
    assert atendida.tipo == "resultado"


# --- partida -----------------------------------------------------------------------------------------------------


def _com_tls(c: Certificados) -> Parametros:
    return Parametros(
        INTERNAL_CA_FILE=str(c.ca_visitas),
        INTERNAL_CERT_FILE=str(c.visitas_cert),
        INTERNAL_KEY_FILE=str(c.visitas_key),
        INTERNAL_CLIENT_CA_FILE=str(c.ca_clientes_visitas),
    )


@pytest.mark.parametrize(
    "nome",
    [
        "DATABASE_URL",
        "DATABASE_PASSWORD_FILE",
        "DB_SSLMODE",
        "DB_SSLROOTCERT",
        "VAULT_ADDR",
        "VAULT_ROLE_ID_FILE",
        "VAULT_SECRET_ID_FILE",
    ],
)
def test_partida_da_sala_recusa_banco_e_cofre(certificados: Certificados, nome: str) -> None:
    problemas = problemas_da_sala(_com_tls(certificados), {nome: "x", "APP_ENV": "teste"})
    assert problemas == [f"A sala de visitas nao tem banco nem cofre: tire {nome} do ambiente dela."]


@pytest.mark.parametrize("nome", ["SMTP_PASSWORD_FILE", "WEBHOOK_SECRET_FILE", "VISITS_CLIENT_KEY_FILE"])
def test_partida_da_sala_recusa_segredo_que_nao_e_o_certificado_dela(certificados: Certificados, nome: str) -> None:
    problemas = problemas_da_sala(_com_tls(certificados), {nome: "/run/secrets/x", "APP_ENV": "teste"})
    assert problemas == [f"A sala de visitas so recebe o proprio certificado: tire {nome} do ambiente dela."]


def test_partida_da_sala_aceita_o_proprio_certificado_e_exige_mtls(certificados: Certificados) -> None:
    ambiente = {n: "/run/secrets/x" for n in ("INTERNAL_CA_FILE", "INTERNAL_CERT_FILE", "INTERNAL_KEY_FILE")}
    ambiente |= {"INTERNAL_CLIENT_CA_FILE": "/run/secrets/y", "APP_ENV": "teste", "VISITS_INTERNAL_PORT": "9446"}
    # A imagem deixa SSL_CERT_FILE vazio; vazio, ou a lista de autoridades do sistema, nao e segredo.
    ambiente |= {"SSL_CERT_FILE": "", "SMTP_PASSWORD_FILE": " "}
    assert problemas_da_sala(_com_tls(certificados), ambiente) == []
    ambiente |= {"SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"}
    assert problemas_da_sala(_com_tls(certificados), ambiente) == []
    for falta in ("INTERNAL_CERT_FILE", "INTERNAL_KEY_FILE", "INTERNAL_CLIENT_CA_FILE"):
        p = _com_tls(certificados).model_copy(update={falta: None})
        assert [e for e in problemas_da_sala(p, {"APP_ENV": "teste"}) if "so sobe com mTLS" not in e] == []
        assert any("so sobe com mTLS" in e for e in problemas_da_sala(p, {"APP_ENV": "teste"}))
    with pytest.raises(ConfiguracaoInvalida, match="so sobe com mTLS"):
        conferir_sala(Parametros(), {"APP_ENV": "teste"})


def test_a_sala_nao_confere_o_que_e_dos_outros_papeis(certificados: Certificados) -> None:
    """Em producao, a partida dos outros papeis pede telas, avisos e vigia; a sala nao tem nada disso."""
    p = _com_tls(certificados).model_copy(update={"APP_ENV": "producao"})
    assert p.problemas() != []
    assert problemas_da_sala(p, {"APP_ENV": "producao"}) == []


@pytest.fixture
def subir_falso(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Papel, Parametros]]:
    subidos: list[tuple[Papel, Parametros]] = []
    monkeypatch.setattr(partida, "_subir", lambda papel, p: subidos.append((papel, p)))
    monkeypatch.setattr(partida, "configurar", lambda *_: None)
    return subidos


def _ambiente_da_sala(monkeypatch: pytest.MonkeyPatch, c: Certificados) -> None:
    for nome in list(os.environ):
        if nome.startswith(("DATABASE_", "DB_", "VAULT_")) or nome.endswith("_FILE"):
            monkeypatch.delenv(nome)
    monkeypatch.setenv("INTERNAL_CA_FILE", str(c.ca_visitas))
    monkeypatch.setenv("INTERNAL_CERT_FILE", str(c.visitas_cert))
    monkeypatch.setenv("INTERNAL_KEY_FILE", str(c.visitas_key))
    monkeypatch.setenv("INTERNAL_CLIENT_CA_FILE", str(c.ca_clientes_visitas))
    monkeypatch.setenv("APP_ENV", "teste")  # a sala nao sobe sem ele (contrato K8)


def test_comando_visitas_sobe_o_papel_com_a_porta_da_sala(
    monkeypatch: pytest.MonkeyPatch, certificados: Certificados, subir_falso: list[tuple[Papel, Parametros]]
) -> None:
    _ambiente_da_sala(monkeypatch, certificados)
    partida.main(["visitas"])
    assert [papel for papel, _ in subir_falso] == [Papel.VISITAS]
    assert partida._PORTAS[Papel.VISITAS] == "VISITS_INTERNAL_PORT"


@pytest.mark.parametrize("nome", ["DATABASE_PASSWORD_FILE", "VAULT_SECRET_ID_FILE", "DB_SSLMODE"])
def test_comando_visitas_recusa_ambiente_com_banco_ou_cofre(
    monkeypatch: pytest.MonkeyPatch,
    certificados: Certificados,
    subir_falso: list[tuple[Papel, Parametros]],
    capsys: pytest.CaptureFixture[str],
    nome: str,
) -> None:
    _ambiente_da_sala(monkeypatch, certificados)
    monkeypatch.setenv(nome, "verify-full" if nome == "DB_SSLMODE" else "/run/secrets/x")
    with pytest.raises(SystemExit) as saida:
        partida.main(["visitas"])
    assert saida.value.code == 2
    assert subir_falso == []
    assert "nao tem banco nem cofre" in capsys.readouterr().err


# --- saude --------------------------------------------------------------------------------------------------------


async def test_conferencia_de_saude_sem_certificado_de_cliente(
    certificados: Certificados, monkeypatch: pytest.MonkeyPatch, espiao: Path
) -> None:
    c = certificados
    async with _sala_no_ar(c, alvo=apoio_visitas.espiao) as (_, p, _):
        monkeypatch.setenv("INTERNAL_CA_FILE", str(c.ca_visitas))
        monkeypatch.setenv("VISITS_INTERNAL_PORT", str(p.VISITS_INTERNAL_PORT))
        assert await anyio.to_thread.run_sync(saude.main) == 0
        # Outra autoridade no lugar da ca_visitas: a sala nao e reconhecida.
        monkeypatch.setenv("INTERNAL_CA_FILE", str(c.ca_outra))
        assert await anyio.to_thread.run_sync(saude.main) == 1
    monkeypatch.setenv("INTERNAL_CA_FILE", str(c.ca_visitas))
    assert await anyio.to_thread.run_sync(saude.main) == 1  # a sala parou
    assert _pedidos_do_espiao(espiao) == {}  # a conferencia nunca chega a um filho


@pytest.fixture(autouse=True)
def _sem_filhos_perdidos() -> Iterator[None]:
    yield
    import multiprocessing

    assert [f for f in multiprocessing.active_children() if f.name == "mcp-claro-visitas-filho"] == []
