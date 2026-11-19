"""Rota da entrada para o vigia buscar os fechamentos: ``GET /vigia/fechamentos?desde=N``.

So o programa do vigia entra: cracha conferido pelo mesmo ``VerificadorCracha`` do endereco MCP
(assinatura, emissor, audiencia do MCP-CLARO, validade e algoritmos), e o aplicativo do cracha
(``azp`` ou ``client_id``) precisa ser exatamente ``WATCH_CLIENT_ID`` (``mcp-claro-vigia``).

- sem cracha ou cracha recusado: 401, com ``WWW-Authenticate`` (igual ao endereco MCP);
- cracha valido de outro aplicativo: 403, e a recusa vai ao extrato;
- ``desde`` (inteiro >= 0, padrao 0) e ``limite`` (1 a 1000, padrao 500) fora disso: 400.

A resposta traz os fechamentos de numero maior que ``desde``, em ordem: numero, ``desde_xid`` e
``ate_xid`` (texto, sao ``xid8``), linhas, ``hash_anterior`` e ``hash`` em hexadecimal, e a hora do
calculo. ``mais`` diz se ha outra pagina. Nada do extrato sai por aqui, so os numeros do fechamento.
"""

from __future__ import annotations

from typing import Any, Final

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import CrachaRecusado, VerificadorCracha, desafio_401
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.ronda.config import CLIENTE_VIGIA, ler

log = logger("mcp_claro.entrada.fechamentos")

CAMINHO: Final = "/vigia/fechamentos"
LIMITE_PADRAO: Final = 500
LIMITE_MAXIMO: Final = 1000
_SEM_CACHE: Final = {"Cache-Control": "no-store"}

_SQL = """
SELECT numero, desde_xid::text AS desde_xid, ate_xid::text AS ate_xid, linhas,
       encode(hash_anterior, 'hex') AS hash_anterior, encode(hash, 'hex') AS hash, calculado_em
FROM fechamentos
WHERE numero > :desde
ORDER BY numero
LIMIT :limite
"""


def _inteiro(valor: str | None, padrao: int, minimo: int, maximo: int) -> int | None:
    if valor is None or valor == "":
        return padrao
    if not valor.isdigit() or len(valor) > 19:
        return None
    numero = int(valor)
    return numero if minimo <= numero <= maximo else None


def rotas_fechamentos(p: Parametros, engine: AsyncEngine, verificador: VerificadorCracha) -> list[Route]:
    """Rotas para ligar na aplicacao da entrada, no mesmo endereco publico do MCP."""
    vigia = str(ler(p, "WATCH_CLIENT_ID", CLIENTE_VIGIA))

    async def fechamentos(pedido: Request) -> Response:
        autorizacoes = pedido.headers.getlist("authorization")
        if len(autorizacoes) != 1 or not autorizacoes[0].lower().startswith("bearer "):
            return desafio_401(p)
        try:
            cracha = await verificador.verificar(autorizacoes[0][7:].strip())
        except CrachaRecusado as erro:
            log.info("cracha_recusado", rota=CAMINHO, motivo=erro.motivo)
            CONTADORES.somar("crachas_recusados", motivo=erro.motivo)
            return desafio_401(p)
        except httpx2.HTTPError:
            log.warning("jwks_indisponivel", rota=CAMINHO)
            return JSONResponse(
                {"error": "temporarily_unavailable", "error_description": "Provedor de login indisponivel"},
                status_code=503,
                headers={"Retry-After": "5"} | _SEM_CACHE,
            )
        if cracha.client_id != vigia:
            async with engine.begin() as c:
                await registrar(
                    c,
                    papel="entrada",
                    tipo="pedido_recusado",
                    campos={"metodo": CAMINHO, "motivo": "so o vigia busca fechamentos"},
                    dados={"client_id": cracha.client_id[:128]},
                )
            CONTADORES.somar("pedidos_recusados", motivo="fechamentos_nao_e_o_vigia")
            return JSONResponse(
                {"error": "forbidden", "error_description": "So o programa do vigia busca os fechamentos."},
                status_code=403,
                headers=_SEM_CACHE,
            )
        desde = _inteiro(pedido.query_params.get("desde"), 0, 0, 2**63 - 1)
        limite = _inteiro(pedido.query_params.get("limite"), LIMITE_PADRAO, 1, LIMITE_MAXIMO)
        if desde is None or limite is None:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "desde e limite precisam ser inteiros validos."},
                status_code=400,
                headers=_SEM_CACHE,
            )
        async with engine.connect() as c:
            linhas = (await c.execute(text(_SQL), {"desde": desde, "limite": limite + 1})).all()
        itens: list[dict[str, Any]] = [
            {
                "numero": int(linha.numero),
                "desde_xid": str(linha.desde_xid),
                "ate_xid": str(linha.ate_xid),
                "linhas": int(linha.linhas),
                "hash_anterior": linha.hash_anterior,
                "hash": str(linha.hash),
                "calculado_em": linha.calculado_em.isoformat(),
            }
            for linha in linhas[:limite]
        ]
        CONTADORES.somar("fechamentos_entregues_ao_vigia", valor=len(itens))
        return JSONResponse({"fechamentos": itens, "mais": len(linhas) > limite}, headers=_SEM_CACHE)

    return [Route(CAMINHO, fechamentos, methods=["GET"])]
