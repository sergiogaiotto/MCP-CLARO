"""R6b-6 (contrato K6): a entrada perde em ``sistemas`` os GRANTs que nenhum codigo usa.

O unico UPDATE de ``sistemas`` pela entrada e ``despachante/autorizacao.configurar`` (``autorizacao``,
``atualizado_em``), mais a desativacao do D27 e a volta a ``pendente`` (``situacao``, ``motivo``); o unico INSERT e
``cadastro/repositorio.cadastrar_sistema`` (``nome``, ``endereco``, ``tipo_chave``, ``origem``). A prova do revisor
(banco), invertida: a entrada nao muda mais o nome, o endereco nem o tipo de chave (de um proprio ativo, o endereco
mandaria as chaves por pessoa a outro lugar depois de uma sondagem), nem grava a autorizacao no INSERT.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integracao.test_migracao_0006 import ENTRADA, SEM_PERMISSAO, Cena, cena, dono, fazer, recusado

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

__all__ = ["cena", "dono"]  # fixtures

SEM_USO = [("UPDATE", "nome"), ("UPDATE", "endereco"), ("UPDATE", "tipo_chave"), ("INSERT", "autorizacao")]
EM_USO = [
    ("UPDATE", "situacao"),
    ("UPDATE", "motivo"),
    ("UPDATE", "autorizacao"),
    ("UPDATE", "atualizado_em"),
    ("INSERT", "nome"),
    ("INSERT", "endereco"),
    ("INSERT", "tipo_chave"),
    ("INSERT", "origem"),
]


@pytest.mark.parametrize(("privilegio", "coluna"), SEM_USO + EM_USO)
async def test_entrada_so_com_os_privilegios_que_o_codigo_usa(dono: AsyncEngine, privilegio: str, coluna: str) -> None:
    tem = await fazer(dono, "SELECT has_column_privilege(:p, 'sistemas', :c, :pr)", p=ENTRADA, c=coluna, pr=privilegio)
    assert tem is ((privilegio, coluna) in EM_USO), f"a entrada {'nao ' if tem else ''}tem {privilegio} ({coluna})"


@pytest.mark.parametrize(
    "mudanca",
    [
        "UPDATE sistemas SET endereco = 'https://outro-lugar.teste/mcp' WHERE id = :s",
        "UPDATE sistemas SET nome = 'outro nome' WHERE id = :s",
        "UPDATE sistemas SET tipo_chave = 'compartilhada' WHERE id = :s",
    ],
)
async def test_entrada_nao_muda_o_destino_de_um_proprio_ativo(dono: AsyncEngine, cena: Cena, mudanca: str) -> None:
    await recusado(dono, mudanca, SEM_PERMISSAO, papel=ENTRADA, s=cena.proprio_id)
    assert await fazer(dono, "SELECT situacao FROM sistemas WHERE id = :s", s=cena.proprio_id) == "ativo"


async def test_entrada_cadastra_e_configura_como_antes(dono: AsyncEngine, cena: Cena) -> None:
    # O INSERT de cadastrar_sistema e o UPDATE de configurar continuam; a autorizacao no INSERT, nao.
    nome = f"proprio-{uuid.uuid4().hex[:8]}"
    novo = await fazer(
        dono,
        "INSERT INTO sistemas (nome, endereco, tipo_chave, origem) VALUES (:n, 'https://p.teste/mcp', 'por_pessoa',"
        " 'proprio') RETURNING id",
        papel=ENTRADA,
        n=nome,
    )
    config = json.dumps({"authorization_endpoint": "https://login.teste/auth"})
    await fazer(
        dono,
        "UPDATE sistemas SET autorizacao = CAST(:a AS jsonb), atualizado_em = now() WHERE id = :s",
        papel=ENTRADA,
        a=config,
        s=novo,
    )
    await fazer(
        dono, "UPDATE sistemas SET situacao = 'desativado', motivo = 'teste' WHERE id = :s", papel=ENTRADA, s=novo
    )
    await recusado(
        dono,
        "INSERT INTO sistemas (nome, endereco, tipo_chave, origem, autorizacao) VALUES (:n, 'https://p.teste/mcp',"
        " 'por_pessoa', 'proprio', CAST(:a AS jsonb))",
        SEM_PERMISSAO,
        papel=ENTRADA,
        n=f"{nome}-2",
        a=config,
    )
