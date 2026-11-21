"""O processo filho da sala de visitas: uma chamada, um processo novo (ajustes, A1).

O pai inicia o filho por ``spawn`` (um interpretador novo, sem nada da memoria do pai) com o que e igual para todas
as chamadas: o ambiente da sala, a liberacao de locais e o teto do pedido. O filho carrega o cliente fixado e espera
um pedido num cano (um soquete local): a rota, uma quebra de linha e o corpo, ate o fim do cano. Atende com
``sala_contrato.atender`` e escreve a resposta num segundo cano: tres digitos do status HTTP e o corpo, como bytes
crus (o pai nunca desempacota objeto vindo do filho). Depois morre. Assim, um servidor de terceiros que explore uma
falha do kit dentro do filho so ve a propria chamada.

Antes de receber o pedido, o filho se fecha: nao gera core nem aceita ser depurado por outro processo do mesmo
usuario (``PR_SET_DUMPABLE``), morre junto com o pai (``PR_SET_PDEATHSIG``), entra num dominio Landlock so dele
(``dominio_proprio``) e nao escreve log nem aviso (o texto de um terceiro nunca vai ao log da sala). So depois
escreve ``PRONTO`` no cano da resposta, e o pai so entrega o pedido a quem ja avisou.

Por que o aviso: do ``spawn`` ate o isolamento (cerca de 0,8 s de import num filho recem-iniciado), o filho ainda e
um processo comum do usuario da sala. Com o dominio, nenhum filho isolado (por exemplo, um tomado por um terceiro)
alcanca outro processo fora do proprio dominio: nem ``/proc/<pid>/fd``, nem ``/proc/<pid>/mem``, nem ``ptrace``,
nem, com o Landlock 6, sinais. Sem Landlock no nucleo, ficam o aviso, o ``PR_SET_DUMPABLE`` e os canos por soquete
(que o ``/proc`` nao reabre); se o nucleo tambem deixar ``ptrace`` entre processos irmaos (Yama desligado), um filho
tomado ainda pode, na janela, prender-se a um filho novo ou abrir a memoria dele e ler o pedido depois (residuo no
modelo de ameacas).

Erro inesperado: o filho sai sem escrever resposta, com ``SAIDA_ERRO``, e o pai responde 500 (o despachante le
duvida).
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import logging
import os
import signal
import struct
import sys
import threading
import warnings
from typing import Any, Final, Protocol

import anyio

from mcp_claro.ligacao import sala_contrato

PR_SET_PDEATHSIG: Final = 1
PR_GET_DUMPABLE: Final = 3
PR_SET_DUMPABLE: Final = 4
PR_SET_NO_NEW_PRIVS: Final = 38
TETO_ROTA: Final = 64
SAIDA_ERRO: Final = 70
PRONTO: Final = b"\x06"  # o filho ja se isolou: so agora o pai entrega o pedido
_PEDACO: Final = 1024 * 1024

# Landlock (mesmos numeros em x86_64 e aarch64).
_LANDLOCK_CREATE_RULESET: Final = 444
_LANDLOCK_RESTRICT_SELF: Final = 446
_LANDLOCK_CREATE_RULESET_VERSION: Final = 1
# Criar dispositivos (o filho nunca cria): da ao dominio algo a restringir sem tirar nada de que o filho precise.
_LANDLOCK_ACCESS_FS_MAKE_CHAR: Final = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_BLOCK: Final = 1 << 11
# Landlock 6: soquete abstrato e sinal so dentro do proprio dominio.
_LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET: Final = 1 << 0
_LANDLOCK_SCOPE_SIGNAL: Final = 1 << 1


class Ponta(Protocol):
    """Uma ponta de cano: so o descritor e o fechamento (o filho le e escreve bytes crus)."""

    def fileno(self) -> int: ...

    def close(self) -> None: ...


def _libc() -> Any | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    return libc


def _prctl(opcao: int, valor: int = 0) -> int | None:
    libc = _libc()
    if libc is None:
        return None
    try:
        resultado: int = libc.prctl(opcao, valor, 0, 0, 0)
    except AttributeError:
        return None
    return resultado


def _syscall(numero: int, *args: Any) -> int:
    libc = _libc()
    if libc is None:
        return -1
    try:
        chamada = libc.syscall
    except AttributeError:
        return -1
    chamada.restype = ctypes.c_long
    resultado: int = chamada(ctypes.c_long(numero), *(ctypes.c_long(a) if isinstance(a, int) else a for a in args))
    return resultado


def versao_landlock() -> int:
    """A versao do Landlock que o nucleo oferece; 0 sem Landlock (outro sistema, nucleo antigo ou desligado)."""
    if not sys.platform.startswith("linux") or os.uname().machine not in {"x86_64", "aarch64"}:
        return 0
    return max(_syscall(_LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION), 0)


def dominio_proprio() -> bool:
    """Poe o processo num dominio Landlock novo, so dele. Melhor esforco: False sem Landlock.

    O que importa e a fronteira: um processo num dominio so alcanca, por ``ptrace`` e pelo ``/proc`` (``fd``,
    ``mem``), processos do mesmo dominio ou de dominios dentro dele. Cada filho tem o seu; o pai fica fora. Com o
    Landlock 6, sinais e soquetes abstratos tambem ficam dentro do dominio.
    """
    versao = versao_landlock()
    if versao < 1:
        return False
    escopo = _LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | _LANDLOCK_SCOPE_SIGNAL if versao >= 6 else 0
    # struct landlock_ruleset_attr: handled_access_fs, handled_access_net, scoped (zeros a mais valem em qualquer
    # versao).
    regras = struct.pack("=QQQ", _LANDLOCK_ACCESS_FS_MAKE_CHAR | _LANDLOCK_ACCESS_FS_MAKE_BLOCK, 0, escopo)
    atributos = ctypes.create_string_buffer(regras, len(regras))
    fd = _syscall(_LANDLOCK_CREATE_RULESET, atributos, len(regras), 0)
    if fd < 0:
        return False
    try:
        if _prctl(PR_SET_NO_NEW_PRIVS, 1) != 0:
            return False
        return _syscall(_LANDLOCK_RESTRICT_SELF, fd, 0) == 0
    finally:
        os.close(fd)


def nao_depuravel() -> bool:
    """Sem core e sem depuracao por outro processo do mesmo usuario (nem ``/proc/<pid>/mem``). Melhor esforco."""
    return _prctl(PR_SET_DUMPABLE, 0) == 0


def depuravel() -> bool | None:
    """True se o processo ainda pode ser depurado por outro do mesmo usuario; None fora do Linux."""
    valor = _prctl(PR_GET_DUMPABLE)
    return None if valor is None or valor < 0 else bool(valor)


def _calar(*_: Any) -> None:
    return None


def _isolar() -> None:
    nao_depuravel()
    _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    dominio_proprio()
    # Nada do terceiro vai ao log: o kit registra excecoes com o texto que recebeu.
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    sys.excepthook = _calar
    sys.unraisablehook = _calar
    threading.excepthook = _calar


def ler_ate_o_fim(fd: int, teto: int) -> bytes | None:
    """Le um descritor ate o fim; None se passar de ``teto`` bytes."""
    partes: list[bytes] = []
    lidos = 0
    while pedaco := os.read(fd, _PEDACO):
        lidos += len(pedaco)
        if lidos > teto:
            return None
        partes.append(pedaco)
    return b"".join(partes)


def escrever_tudo(fd: int, dados: bytes) -> None:
    vista = memoryview(dados)
    while vista:
        vista = vista[os.write(fd, vista) :]


def avisar_pronto(respostas: Ponta) -> None:
    """Diz ao pai que o filho ja se isolou: so depois disto o pai entrega o pedido."""
    escrever_tudo(respostas.fileno(), PRONTO)


def principal(pedidos: Ponta, respostas: Ponta, ambiente: str, liberar_locais: bool, teto_pedido: int) -> None:
    """Alvo do processo filho. Se isola, avisa, le um pedido, atende uma vez, escreve a resposta e termina."""
    _isolar()
    # O cliente fixado ja foi carregado com este modulo: o filho pronto so espera o pedido.
    try:
        avisar_pronto(respostas)
        dados = ler_ate_o_fim(pedidos.fileno(), TETO_ROTA + 1 + teto_pedido)
    except OSError:
        dados = None
    finally:
        with contextlib.suppress(OSError):
            pedidos.close()
    rota, separador, corpo = (dados or b"").partition(b"\n")
    if not separador:
        os._exit(0)  # o pai desistiu antes de entregar o pedido inteiro: nada foi ao terceiro
    try:
        atender = functools.partial(sala_contrato.atender, ambiente=ambiente, liberar_locais=liberar_locais)
        status, saida = anyio.run(atender, rota.decode("ascii"), corpo)
        escrever_tudo(respostas.fileno(), b"%03d" % status + saida)
        respostas.close()
    except BaseException:  # noqa: BLE001 - erro inesperado: sem resposta, o pai devolve 500
        os._exit(SAIDA_ERRO)
