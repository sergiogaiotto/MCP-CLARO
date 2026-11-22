"""Apoio aos testes da sala de visitas (G7): certificados de teste e alvos de processo filho.

Os alvos ficam aqui, num modulo importavel, porque o filho e iniciado por ``spawn``: ele importa o alvo pelo nome.

- ``espiao``: roda o ``filho.principal`` de verdade e grava o que o processo recebeu do pai (o pedido inteiro, lido
  pelo proprio ``principal`` depois do isolamento) num arquivo com o pid, na pasta de ``ESPIAO``; no fim, grava se o
  processo ainda podia ser depurado.
- ``sai_sem_resposta``, ``responde_status_invalido``, ``levanta``, ``responde_bytes_crus``, ``dorme``: filhos que
  avisam que estao prontos e falham de proposito depois, para provar o que o pai faz.
- ``lento_antes_de_isolar``: um filho que demora a se isolar, como um recem-iniciado sob rajada. Grava se o pedido
  ja tinha chegado antes do isolamento e depois roda o ``filho.principal`` de verdade, com um ``atender`` de teste
  (sem rede). Num pedido que pede para espiar, ele faz o papel de um filho tomado por um terceiro: ja isolado,
  procura irmaos recem-iniciados e tenta abrir os canos e a memoria deles, e a memoria do pai.
- ``nao_se_isola`` e ``nao_se_isola_na_primeira``: filhos que morrem antes de avisar que se isolaram.
- ``atende_devagar``: o ``filho.principal`` de verdade com o ``atender`` de teste; ``{"dormir": s}`` grava
  ``<pid>.dormindo`` (o filho ja se isolou e recebeu o pedido) e demora ``s`` segundos para responder.
- ``responde_cru``: com ``functools.partial``, escreve no cano da resposta exatamente os bytes dados.
- ``escreve_depois_de_isolar``: depois de ``filho._isolar()``, escreve nos descritores 1 e 2 e devolve para onde
  apontam 0, 1 e 2 (antes e depois do isolamento), o que sobrou do cano do rastreador de recursos do
  ``multiprocessing``, se o rastreador volta a ser iniciado (``resource_tracker.ensure_running()``) e o sinal que o
  processo recebe quando morre a thread que o criou (``PR_GET_PDEATHSIG``).
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime
import errno
import ipaddress
import json
import os
import select
import sys
import time
from dataclasses import dataclass
from multiprocessing import resource_tracker
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mcp_claro.ligacao import sala_contrato
from mcp_claro.visitas import filho

ESPIAO = "MCP_CLARO_TESTE_ESPIAO_VISITAS"
# Modulo posto em sys.modules so no processo de teste (o pai): um filho por fork o herdaria; por spawn, nunca.
# Os modulos do pytest nao servem para isso: o spawn roda de novo o script de partida do pai (``bin/pytest``).
SO_DO_PAI = "mcp_claro_teste_modulo_so_do_pai"
JANELA = 1.0  # segundos que ``lento_antes_de_isolar`` espera antes de se isolar
PEDIDO_DE_ESPIAR = b'{"espiar":true}'


def _pasta() -> Path:
    return Path(os.environ[ESPIAO])


def espiao(pedidos: Any, respostas: Any, ambiente: str, liberar_locais: bool, teto_pedido: int) -> None:
    pid = os.getpid()
    ler = filho.ler_ate_o_fim

    def ler_e_gravar(fd: int, teto: int) -> bytes | None:
        dados = ler(fd, teto)
        if fd == pedidos.fileno():
            (_pasta() / f"{pid}.pedido").write_bytes(dados or b"")
            so_do_pai = sorted(m for m in (SO_DO_PAI, "tests.unidade.test_visitas_app") if m in sys.modules)
            (_pasta() / f"{pid}.processo").write_text(
                json.dumps(
                    {
                        "pai": os.getppid(),
                        "ambiente": ambiente,
                        "liberar_locais": liberar_locais,
                        "modulos_do_pai": so_do_pai,
                    }
                ),
                encoding="utf-8",
            )
        return dados

    filho.ler_ate_o_fim = ler_e_gravar  # type: ignore[assignment]  # so neste processo de teste
    filho.principal(pedidos, _PontaQueAnota(respostas, pid), ambiente, liberar_locais, teto_pedido)


class _PontaQueAnota:
    """A ponta da resposta: ao fechar (o filho ja se isolou e respondeu), anota se o processo e depuravel."""

    def __init__(self, ponta: Any, pid: int) -> None:
        self._ponta, self._pid = ponta, pid

    def fileno(self) -> int:
        return int(self._ponta.fileno())

    def close(self) -> None:
        (_pasta() / f"{self._pid}.fim").write_text(json.dumps({"depuravel": filho.depuravel()}), encoding="utf-8")
        self._ponta.close()


def _registrar_pid() -> None:
    if ESPIAO in os.environ:
        (_pasta() / f"{os.getpid()}.vivo").write_text("", encoding="utf-8")


def sai_sem_resposta(pedidos: Any, respostas: Any, *_: Any) -> None:
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    os._exit(filho.SAIDA_ERRO)


def levanta(pedidos: Any, respostas: Any, *_: Any) -> None:
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    raise RuntimeError("falha de proposito")


def responde_status_invalido(pedidos: Any, respostas: Any, *_: Any) -> None:
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    filho.escrever_tudo(respostas.fileno(), b"abc{}")


def responde_cru(saida: bytes, pedidos: Any, respostas: Any, *_: Any) -> None:
    """Escreve ``saida`` como a resposta inteira (use com ``functools.partial``)."""
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    filho.escrever_tudo(respostas.fileno(), saida)


MARCA_NA_SAIDA = b"marca-do-filho-isolado-"


def _para_onde(fd: int) -> str:
    try:
        estado = os.fstat(fd)
    except OSError as erro:
        return errno.errorcode.get(erro.errno or 0, str(erro.errno))
    return "devnull" if os.path.samestat(estado, os.stat(os.devnull)) else "outro"


PR_GET_PDEATHSIG = 2


def sinal_da_morte_do_criador() -> int | None:
    """O sinal que o processo recebe quando morre a thread que o criou (``PR_GET_PDEATHSIG``); None fora do Linux."""
    if not sys.platform.startswith("linux"):
        return None
    sinal = ctypes.c_int(-1)
    if ctypes.CDLL(None, use_errno=True).prctl(PR_GET_PDEATHSIG, ctypes.byref(sinal), 0, 0, 0) != 0:
        return None
    return sinal.value


def _rastreador_volta() -> str:
    """Tenta iniciar de novo o rastreador de recursos (como o ``multiprocessing`` faz ao registrar um recurso)."""
    try:
        resource_tracker.ensure_running()
    except OSError:
        return "recusado"
    return "iniciou"


def escreve_depois_de_isolar(pedidos: Any, respostas: Any, *_: Any) -> None:
    """Um filho isolado que escreve no stdout e no stderr herdados do pai (como o texto de um terceiro faria)."""
    rastreador = resource_tracker._resource_tracker._fd
    antes = {str(fd): _para_onde(fd) for fd in (0, 1, 2)}
    filho._isolar()
    for fd in (1, 2):
        with contextlib.suppress(OSError):
            os.write(fd, MARCA_NA_SAIDA + str(fd).encode() + b"\n")
    print(MARCA_NA_SAIDA.decode() + "print", flush=True)
    print(MARCA_NA_SAIDA.decode() + "stderr", file=sys.stderr, flush=True)
    vistos = {
        "descritores_antes": antes,
        "descritores": {str(fd): _para_onde(fd) for fd in (0, 1, 2)},
        "rastreador_herdado": rastreador is not None,
        "rastreador_fechado": rastreador is None or _para_onde(rastreador) == "EBADF",
        "rastreador_agora": resource_tracker._resource_tracker._fd,
        "rastreador_volta": _rastreador_volta(),
        "pdeathsig": sinal_da_morte_do_criador(),
    }
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    filho.escrever_tudo(respostas.fileno(), b"200" + json.dumps(vistos).encode())


def responde_bytes_crus(pedidos: Any, respostas: Any, *_: Any) -> None:
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    filho.escrever_tudo(respostas.fileno(), b"200" + BYTES_CRUS)


BYTES_CRUS = b"\xff\x00 isto nao e JSON nem do contrato \r\n[[nada]]"


def dorme(pedidos: Any, respostas: Any, *_: Any) -> None:
    """Recebe o pedido, marca o pid (so quem recebeu uma chamada) e nao responde."""
    filho.avisar_pronto(respostas)
    filho.ler_ate_o_fim(pedidos.fileno(), 1 << 30)
    _registrar_pid()
    time.sleep(120)


# --- a janela antes do isolamento ------------------------------------------------------------------------------------


def nao_se_isola(*_: Any) -> None:
    """Morre antes de avisar que se isolou (como um filho que falha ao carregar)."""
    os._exit(filho.SAIDA_ERRO)


def nao_se_isola_na_primeira(pedidos: Any, respostas: Any, *args: Any) -> None:
    """O primeiro filho da pasta morre antes de se isolar; os outros atendem como ``lento_antes_de_isolar``."""
    marca = _pasta() / "primeiro_morreu"
    try:
        marca.touch(exist_ok=False)
    except FileExistsError:
        lento_antes_de_isolar(pedidos, respostas, *args)
        return
    os._exit(filho.SAIDA_ERRO)


def lento_antes_de_isolar(pedidos: Any, respostas: Any, ambiente: str, liberar_locais: bool, teto_pedido: int) -> None:
    """Demora ``JANELA`` antes de rodar o ``filho.principal`` de verdade, e anota, em ``<pid>.ordem``, cada passo
    dele: o isolamento e o aviso ao pai (com ``filho.depuravel()`` no momento do aviso)."""
    pid = os.getpid()
    time.sleep(JANELA)
    chegou = bool(select.select([pedidos.fileno()], [], [], 0)[0])
    (_pasta() / f"{pid}.antes").write_text(json.dumps({"pedido_antes_de_isolar": chegou}), encoding="utf-8")
    isolar, avisar = filho._isolar, filho.avisar_pronto

    def anotar(passo: dict[str, Any]) -> None:
        with (_pasta() / f"{pid}.ordem").open("a", encoding="utf-8") as ordem:
            ordem.write(json.dumps(passo) + "\n")

    def isolar_e_anotar() -> None:
        isolar()
        (_pasta() / f"{pid}.isolou").write_text(repr(time.monotonic()), encoding="utf-8")
        anotar({"passo": "isolou"})

    def avisar_e_anotar(ponta: filho.Ponta) -> None:
        anotar({"passo": "aviso", "depuravel": filho.depuravel()})
        avisar(ponta)

    filho._isolar = isolar_e_anotar  # type: ignore[assignment]  # so neste processo de teste
    filho.avisar_pronto = avisar_e_anotar  # type: ignore[assignment]
    sala_contrato.atender = _atender_de_teste  # type: ignore[assignment]
    filho.principal(pedidos, respostas, ambiente, liberar_locais, teto_pedido)


def atende_devagar(pedidos: Any, respostas: Any, ambiente: str, liberar_locais: bool, teto_pedido: int) -> None:
    """O ``filho.principal`` de verdade, com o ``atender`` de teste (sem rede)."""
    sala_contrato.atender = _atender_de_teste  # type: ignore[assignment]  # so neste processo de teste
    filho.principal(pedidos, respostas, ambiente, liberar_locais, teto_pedido)


async def _atender_de_teste(rota: str, corpo: bytes, *, ambiente: str, liberar_locais: bool) -> tuple[int, bytes]:
    import anyio
    import anyio.to_thread

    if corpo == PEDIDO_DE_ESPIAR:
        return 200, json.dumps(await anyio.to_thread.run_sync(_espiar)).encode()
    if corpo.startswith(b'{"dormir":'):
        segundos = float(json.loads(corpo)["dormir"])
        # Ja isolado e com o pedido: o pai sabe por aqui que a chamada esta em andamento no filho.
        (_pasta() / f"{os.getpid()}.dormindo").write_text(repr(segundos), encoding="utf-8")
        await anyio.sleep(segundos)
        return 200, b'{"dormiu":%d}' % os.getpid()
    (_pasta() / f"{os.getpid()}.pedido").write_bytes(rota.encode() + b"\n" + corpo)
    return 200, b'{"recebido":%d}' % len(corpo)


def _irmaos(pai: int, eu: int) -> list[int]:
    """Os filhos do mesmo pai que estao iniciando pelo ``spawn`` (a linha de comando e publica)."""
    achados: list[int] = []
    for nome in os.listdir("/proc"):
        if not nome.isdigit() or int(nome) == eu:
            continue
        try:
            estado = Path(f"/proc/{nome}/status").read_text(encoding="utf-8")
            linha = Path(f"/proc/{nome}/cmdline").read_bytes()
        except OSError:
            continue
        ppid = next((int(x.split()[1]) for x in estado.splitlines() if x.startswith("PPid:")), -1)
        if ppid == pai and b"spawn_main" in linha:
            achados.append(int(nome))
    return achados


def _tentar_abrir(caminho: str, abertos: list[str], recusas: set[str]) -> int | None:
    try:
        fd = os.open(caminho, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None  # descritor que nao existe
    except OSError as erro:
        recusas.add(errno.errorcode.get(erro.errno or 0, str(erro.errno)))
        return None
    abertos.append(caminho)
    return fd


def _sinal(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except OSError as erro:
        return errno.errorcode.get(erro.errno or 0, str(erro.errno))
    return "aceito"


def _espiar() -> dict[str, Any]:
    """Um filho tomado: tenta alcancar irmaos recem-iniciados (e o pai) pelo ``/proc``, como o mesmo usuario."""
    eu, pai = os.getpid(), os.getppid()
    tentados: dict[int, float] = {}
    abertos: list[str] = []
    recusas: set[str] = set()
    sinais: dict[str, str] = {}
    lidos = b""
    para_ler: list[int] = []
    for caminho in (f"/proc/{pai}/mem", f"/proc/{pai}/fd/0"):
        fd = _tentar_abrir(caminho, abertos, recusas)
        if fd is not None:
            os.close(fd)
    sinais["pai"] = _sinal(pai)
    (_pasta() / "espiao_pronto").write_text(str(eu), encoding="utf-8")
    fim = time.monotonic() + 20
    while time.monotonic() < fim:
        for irmao in _irmaos(pai, eu):
            if irmao in tentados:
                continue
            tentados[irmao] = time.monotonic()
            for caminho in (f"/proc/{irmao}/mem", *(f"/proc/{irmao}/fd/{n}" for n in range(32))):
                fd = _tentar_abrir(caminho, abertos, recusas)
                if fd is None:
                    continue
                if "/fd/" in caminho:
                    para_ler.append(fd)
                else:
                    os.close(fd)
            sinais[str(irmao)] = _sinal(irmao)
        for fd in para_ler:
            try:
                lidos += os.read(fd, 1 << 20)
            except OSError:
                continue
        if tentados and time.monotonic() - max(tentados.values()) > 2 * JANELA + 1:
            break
        time.sleep(0.01)
    for fd in para_ler:
        os.close(fd)
    return {
        "tentados": {str(pid): quando for pid, quando in tentados.items()},
        "abertos": abertos,
        "recusas": sorted(recusas),
        "sinais": sinais,
        "lidos": lidos.hex(),
    }


# --- certificados de teste -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Certificados:
    ca_visitas: Path
    visitas_cert: Path
    visitas_key: Path
    ca_clientes_visitas: Path
    despachante_cert: Path
    despachante_key: Path
    ca_outra: Path
    outro_cert: Path
    outro_key: Path


def _nome(texto: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, texto)])


def _gravar(pasta: Path, nome: str, cert: x509.Certificate, chave: ec.EllipticCurvePrivateKey | None) -> Path:
    caminho = pasta / f"{nome}.pem"
    caminho.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if chave is not None:
        (pasta / f"{nome}_key.pem").write_bytes(
            chave.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    return caminho


def _autoridade(nome: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    chave = ec.generate_private_key(ec.SECP256R1())
    agora = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_nome(nome))
        .issuer_name(_nome(nome))
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - datetime.timedelta(minutes=5))
        .not_valid_after(agora + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(chave.public_key()), critical=False)
        .sign(chave, hashes.SHA256())
    )
    return cert, chave


def _folha(
    nome: str, ca: tuple[x509.Certificate, ec.EllipticCurvePrivateKey], uso: x509.ObjectIdentifier
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    chave = ec.generate_private_key(ec.SECP256R1())
    agora = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_nome(nome))
        .issuer_name(ca[0].subject)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - datetime.timedelta(minutes=5))
        .not_valid_after(agora + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(nome), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([uso]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca[1].public_key()),
            critical=False,
        )
        .sign(ca[1], hashes.SHA256())
    )
    return cert, chave


def gerar_certificados(pasta: Path) -> Certificados:
    """Autoridades e certificados como o ``gerar-segredos-dev.sh`` faz para a sala (M4), mais uma autoridade alheia."""
    pasta.mkdir(parents=True, exist_ok=True)
    ca_visitas = _autoridade("ca_visitas")
    ca_clientes = _autoridade("ca_clientes_visitas")
    ca_outra = _autoridade("ca_interna")
    visitas = _folha("visitas", ca_visitas, ExtendedKeyUsageOID.SERVER_AUTH)
    despachante = _folha("despachante", ca_clientes, ExtendedKeyUsageOID.CLIENT_AUTH)
    outro = _folha("despachante", ca_outra, ExtendedKeyUsageOID.CLIENT_AUTH)
    return Certificados(
        ca_visitas=_gravar(pasta, "ca_visitas", ca_visitas[0], None),
        visitas_cert=_gravar(pasta, "visitas", *visitas),
        visitas_key=pasta / "visitas_key.pem",
        ca_clientes_visitas=_gravar(pasta, "ca_clientes_visitas", ca_clientes[0], None),
        despachante_cert=_gravar(pasta, "despachante_visitas_cliente", *despachante),
        despachante_key=pasta / "despachante_visitas_cliente_key.pem",
        ca_outra=_gravar(pasta, "ca_outra", ca_outra[0], None),
        outro_cert=_gravar(pasta, "outro_cliente", *outro),
        outro_key=pasta / "outro_cliente_key.pem",
    )
