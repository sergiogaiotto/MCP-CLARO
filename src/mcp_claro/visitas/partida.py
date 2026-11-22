"""O que a partida da sala de visitas recusa (desenho 3.8; ajustes D1, A1 e M4).

A sala nao tem banco nem cofre: o ambiente dela nao pode ter variavel ``DATABASE_*``, ``DB_*`` nem ``VAULT_*``, e o
unico segredo que ela recebe e o proprio certificado (``INTERNAL_*_FILE``; outro ``*_FILE`` com valor e recusado).
Como cada processo filho herda o ambiente do pai, isso tambem garante que um terceiro que tome um filho nao encontre
segredo nenhum ali.

A sala so sobe com mTLS: certificado de servidor da ``ca_visitas`` e, como autoridade de clientes, a
``ca_clientes_visitas``, que so emite o certificado do despachante.

A sala so sobe com ``APP_ENV`` escrito no ambiente dela (contrato K8): sem ele, os ``Parametros`` caem no padrao
``desenvolvimento``, e a sala aceitaria enderecos locais (``Sala.liberar_locais``) sem que o despachante soubesse.

As regras gerais de ``Parametros.problemas`` (telas, avisos, vigia, banco) sao dos outros papeis e nao valem aqui.
"""

from __future__ import annotations

from collections.abc import Mapping

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros

PREFIXOS_PROIBIDOS = ("DATABASE_", "DB_", "VAULT_")
SEM_APP_ENV = (
    "A sala de visitas precisa de APP_ENV escrito no ambiente dela (desenvolvimento, teste, piloto ou producao): sem"
    " ele, ela subiria como desenvolvimento e aceitaria enderecos locais."
)
SEGREDOS_DA_SALA = frozenset({"INTERNAL_CA_FILE", "INTERNAL_CERT_FILE", "INTERNAL_KEY_FILE", "INTERNAL_CLIENT_CA_FILE"})
# Nao e segredo: a lista de autoridades do sistema (a imagem a deixa vazia).
NAO_SAO_SEGREDO = frozenset({"SSL_CERT_FILE"})


def problemas_da_sala(p: Parametros, ambiente: Mapping[str, str]) -> list[str]:
    """Lista o que a partida da sala recusa. ``ambiente`` e o do processo (nomes e, nos ``*_FILE``, se ha valor)."""
    erros: list[str] = []
    if "APP_ENV" not in ambiente:
        erros.append(SEM_APP_ENV)
    proibidas = sorted(nome for nome in ambiente if nome.startswith(PREFIXOS_PROIBIDOS))
    if proibidas:
        erros.append(f"A sala de visitas nao tem banco nem cofre: tire {', '.join(proibidas)} do ambiente dela.")
    arquivos = sorted(
        nome
        for nome, valor in ambiente.items()
        if nome.endswith("_FILE")
        and valor.strip()
        and nome not in SEGREDOS_DA_SALA | NAO_SAO_SEGREDO
        and not nome.startswith(PREFIXOS_PROIBIDOS)
    )
    if arquivos:
        erros.append(f"A sala de visitas so recebe o proprio certificado: tire {', '.join(arquivos)} do ambiente dela.")
    if not (p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE and p.INTERNAL_CLIENT_CA_FILE):
        erros.append(
            "A sala de visitas so sobe com mTLS: INTERNAL_CERT_FILE e INTERNAL_KEY_FILE (certificado da ca_visitas) e"
            " INTERNAL_CLIENT_CA_FILE (a ca_clientes_visitas, que so emite o certificado do despachante)."
        )
    return erros


def conferir_sala(p: Parametros, ambiente: Mapping[str, str]) -> Parametros:
    """Levanta ``ConfiguracaoInvalida`` se a sala nao pode subir assim."""
    erros = problemas_da_sala(p, ambiente)
    if erros:
        raise ConfiguracaoInvalida(erros)
    return p
