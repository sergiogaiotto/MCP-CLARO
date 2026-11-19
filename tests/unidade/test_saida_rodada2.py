"""Segunda rodada de correcoes da porta de saida (nucleo), sem banco: o limite "so estes destinatarios" com o
esquema da ferramenta, o caractere nulo no resultado e nos argumentos do aplicativo."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_claro.saida.despachante import TROCA_DO_NULO, sem_nulo
from mcp_claro.saida.fila import cobre
from mcp_claro.saida.pedidos import tem_nulo

# Limite "so estes": o campo escolhido com os valores, e cada outro campo de destinatario marcado com [].
SO_EQUIPE = [{"campo": "para", "valores": ["equipe@org"]}, {"campo": "copia", "valores": []}]
# Esquema de entrada da ferramenta revisada: para (texto), copia (texto ou lista de textos, ou nulo), texto.
PROPRIEDADES = frozenset({"para", "copia", "texto"})


@pytest.mark.parametrize(
    "argumentos",
    [
        {"para": "equipe@org", "texto": "oi"},
        {"para": ["equipe@org"], "texto": "oi"},  # anyOf texto ou lista: a lista so de textos vale
        {"para": "equipe@org", "copia": None, "texto": "oi"},  # campo anulavel
        {"para": "equipe@org", "copia": "", "texto": "oi"},
        {"para": "equipe@org", "copia": [], "texto": "oi"},
        {"para": "equipe@org"},  # o texto e livre: pode faltar
    ],
)
def test_so_estes_cobre_o_destinatario_escolhido_com_o_resto_vazio(argumentos: dict[str, Any]) -> None:
    assert cobre(SO_EQUIPE, argumentos, propriedades=PROPRIEDADES)


@pytest.mark.parametrize(
    ("argumentos", "porque"),
    [
        ({"para": "equipe@org", "texto": "oi", "oculta": "fora@x"}, "chave fora do esquema"),
        ({"para": "equipe@org", "texto": "oi", "Para": "fora@x"}, "chave fora do esquema (maiuscula)"),
        ({"para": ["equipe@org", "fora@x"], "texto": "oi"}, "um dos destinatarios fora da lista"),
        ({"para": [{"email": "equipe@org"}], "texto": "oi"}, "lista de objetos"),
        ({"para": ["equipe@org", {"email": "fora@x"}], "texto": "oi"}, "lista com objeto"),
        ({"para": {"email": "equipe@org"}, "texto": "oi"}, "objeto"),
        ({"para": 7, "texto": "oi"}, "numero"),
        ({"para": True, "texto": "oi"}, "verdadeiro ou falso"),
        ({"texto": "oi"}, "sem o destinatario escolhido"),
        ({"para": None, "texto": "oi"}, "destinatario escolhido nulo"),
        ({"para": "", "texto": "oi"}, "destinatario escolhido vazio"),
        ({"para": [], "texto": "oi"}, "lista vazia no destinatario escolhido"),
        ({"para": "equipe@org", "copia": "fora@x", "texto": "oi"}, "copia preenchida"),
        ({"para": "equipe@org", "copia": [""], "texto": "oi"}, "copia com texto vazio"),
        ({"para": "equipe@org", "copia": 0, "texto": "oi"}, "copia com numero"),
        ({"para": "equipe@org", "copia": {}, "texto": "oi"}, "copia com objeto vazio"),
        ({"para": "equipe@org", "copia": [{"email": "fora@x"}], "texto": "oi"}, "copia com lista de objetos"),
    ],
)
def test_so_estes_nao_cobre(argumentos: dict[str, Any], porque: str) -> None:
    assert not cobre(SO_EQUIPE, argumentos, propriedades=PROPRIEDADES), porque


def test_chave_fora_do_esquema_so_conta_no_limite_so_estes() -> None:
    fora = {"para": "equipe@org", "texto": "oi", "oculta": "fora@x"}
    # Sem as propriedades (quem chama nao leu o esquema), a regra antiga continua: so os campos da lista.
    assert cobre(SO_EQUIPE, fora)
    # Limite para qualquer destinatario: nada a conferir, nem as chaves.
    assert cobre([], fora, propriedades=PROPRIEDADES)
    # Esquema sem propriedades: no limite "so estes", qualquer argumento faz ir para a ficha.
    assert not cobre(SO_EQUIPE, {"para": "equipe@org"}, propriedades=frozenset())


def test_caractere_nulo_vira_o_de_troca_em_chaves_e_valores() -> None:
    resultado = {
        "content": [{"type": "text", "text": "ok\x00fim"}],
        "structuredContent": {"campo\x00a": ["x\x00", {"y": "\x00\x00"}], "numero": 3, "nada": None},
        "isError": False,
    }
    limpo, trocas = sem_nulo(resultado)
    assert trocas == 5
    assert limpo == {
        "content": [{"type": "text", "text": f"ok{TROCA_DO_NULO}fim"}],
        "structuredContent": {
            f"campo{TROCA_DO_NULO}a": [f"x{TROCA_DO_NULO}", {"y": TROCA_DO_NULO * 2}],
            "numero": 3,
            "nada": None,
        },
        "isError": False,
    }
    assert "\x00" not in repr(limpo)
    assert sem_nulo({"text": "sem nulo"}) == ({"text": "sem nulo"}, 0)


@pytest.mark.parametrize(
    "argumentos",
    [
        {"titulo": "a\x00b"},
        {"titulo\x00": "ok"},
        {"lista": ["ok", ["fundo", {"x": "\x00"}]]},
        {"tupla": ("ok", "\x00")},
    ],
)
def test_argumento_com_caractere_nulo_em_qualquer_lugar(argumentos: dict[str, Any]) -> None:
    assert tem_nulo(argumentos)


def test_argumento_sem_caractere_nulo() -> None:
    assert not tem_nulo({"titulo": "ok", "numero": 0, "nada": None, "lista": [1, 2.5, True, {"x": "\\u0000"}]})
