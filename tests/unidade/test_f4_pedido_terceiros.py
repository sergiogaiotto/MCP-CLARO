"""``Pedido.terceiros`` obrigatorio (F4, etapa 4f; decisao do coordenador).

Com ``False`` por padrao, um chamador que esquecesse o campo faria o sinal "leu texto de fora" vindo de terceiro nao
contar no alerta dos tres sinais: o lado inseguro. Sem valor padrao e so por nome, quem constroi o pedido decide.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from mcp_claro.cadastro.modelos import Aplicativo, FerramentaVitrine, Sistema
from mcp_claro.saida.pedidos import Pedido
from mcp_claro.versao import VERSAO_MCP


def _partes() -> tuple[uuid.UUID, Aplicativo, FerramentaVitrine, dict[str, Any], None]:
    sistema = Sistema(uuid.uuid4(), "sis", "https://t.exemplo/mcp", "terceiro", "por_pessoa", 1, "ativo", VERSAO_MCP)
    ferramenta = FerramentaVitrine(
        id=uuid.uuid4(),
        sistema=sistema,
        nome_original="enviar",
        versao=1,
        nome_vitrine="enviar_x",
        titulo=None,
        descricao="Enviar.",
        esquema_entrada={"type": "object"},
        esquema_saida=None,
        etiquetas={},
        classe="muda",
        risco="medio",
    )
    aplicativo = Aplicativo(uuid.uuid4(), "app", "Aplicativo", "teste", "assistente", "ativo")
    return uuid.uuid4(), aplicativo, ferramenta, {"texto": "oi"}, None


def test_pedido_sem_terceiros_levanta_type_error() -> None:
    with pytest.raises(TypeError, match="terceiros"):
        Pedido(*_partes())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="terceiros"):
        Pedido(*_partes(), trace_id="t")  # type: ignore[call-arg]


def test_pedido_terceiros_so_por_nome() -> None:
    with pytest.raises(TypeError):
        Pedido(*_partes(), None, True)  # type: ignore[call-arg, misc]
    for valor in (True, False):
        assert Pedido(*_partes(), terceiros=valor).terceiros is valor


def test_campo_terceiros_sem_valor_padrao() -> None:
    (campo,) = [f for f in dataclasses.fields(Pedido) if f.name == "terceiros"]
    assert campo.kw_only is True
    assert campo.default is dataclasses.MISSING
    assert campo.default_factory is dataclasses.MISSING
