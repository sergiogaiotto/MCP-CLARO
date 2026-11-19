"""Numero de protocolo, codigo de conferencia e chave do pedido igual.

- Protocolo: ``CLR-AAAA-NNNNNNNN`` (oito digitos ou mais), da sequencia ``protocolo_seq`` (unica por
  instalacao), com o ano de Brasilia. E o que a pessoa e o aplicativo usam para consultar uma acao. Na
  restauracao do banco, a sequencia salta para frente, e um numero dado antes da copia nao volta.
- Codigo de conferencia: resumo do que vai seguir ao sistema (sistema, nome original da ferramenta e
  argumentos canonicos), em 12 letras e numeros faceis de ler. A ficha mostra o mesmo codigo, e o
  despachante recalcula antes de enviar: se nao bater, nada sai.
- Chave do pedido igual: pessoa, aplicativo, ferramenta e argumentos canonicos. Um pedido igual devolve a
  operacao original em vez de criar outra: em qualquer idade, se ela ainda pode sair; por
  ``DUPLICATE_WINDOW_MINUTES``, se ela ja saiu ou ficou nao confirmada (``fila.pedido_igual``).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

FUSO: Final = "America/Sao_Paulo"
# Oito digitos ou mais: depois de uma restauracao, a sequencia salta para frente (ver
# ``fila.parar_por_restauracao``), e o numero pode passar de oito digitos.
PADRAO_PROTOCOLO: Final = re.compile(r"^CLR-[0-9]{4}-[0-9]{8,}$")

# Sem 0/O, 1/I/L e 5/S: o codigo e lido por pessoas, em voz alta ou de uma tela para outra.
_ALFABETO: Final = "ABCDEFGHJKMNPQRTUVWXYZ2346789"


def canonico(valor: Any) -> str:
    """JSON canonico: chaves em ordem, sem espacos, numeros e textos como vieram."""
    return json.dumps(valor, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _legivel(resumo: bytes, tamanho: int = 12) -> str:
    numero = int.from_bytes(resumo, "big")
    letras = []
    for _ in range(tamanho):
        numero, resto = divmod(numero, len(_ALFABETO))
        letras.append(_ALFABETO[resto])
    texto = "".join(letras)
    return "-".join(texto[i : i + 4] for i in range(0, tamanho, 4))


def codigo_conferencia(sistema_id: uuid.UUID, nome_original: str, argumentos: dict[str, Any]) -> str:
    """Codigo do que vai seguir. Mudou qualquer argumento, mudou o codigo."""
    conteudo = canonico({"sistema": str(sistema_id), "ferramenta": nome_original, "argumentos": argumentos})
    return _legivel(hashlib.sha256(("mcp-claro|conferencia|" + conteudo).encode()).digest())


def chave_pedido_igual(
    pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID, ferramenta_id: uuid.UUID, argumentos: dict[str, Any]
) -> str:
    conteudo = canonico(
        {
            "pessoa": str(pessoa_id),
            "aplicativo": str(aplicativo_id),
            "ferramenta": str(ferramenta_id),
            "argumentos": argumentos,
        }
    )
    return hashlib.sha256(("mcp-claro|pedido-igual|" + conteudo).encode()).hexdigest()


async def novo_protocolo(c: AsyncConnection) -> str:
    linha = (
        await c.execute(
            text("SELECT nextval('protocolo_seq') AS n, extract(year FROM now() AT TIME ZONE :f)::int AS ano"),
            {"f": FUSO},
        )
    ).one()
    return f"CLR-{linha.ano:04d}-{linha.n:08d}"


def protocolo_valido(valor: object) -> bool:
    return isinstance(valor, str) and bool(PADRAO_PROTOCOLO.match(valor))
