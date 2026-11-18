"""Fila de avisos sem banco: espera crescente, corpo sem conteudo, mensagem de e-mail e destinos."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from mcp_claro.avisos.destinos import destino_pessoa, parece_email
from mcp_claro.avisos.email import EnvioEmail, montar_mensagem
from mcp_claro.avisos.fila import Aviso, ConfigEnvio, CorpoRecusado, conferir_corpo, espera
from mcp_claro.config.settings import Parametros


def test_espera_cresce_e_para_no_teto() -> None:
    esperas = [espera(n, base=30, teto=3600) for n in range(1, 10)]
    assert esperas[:5] == [30, 60, 120, 240, 480]
    assert all(a <= b for a, b in itertools.pairwise(esperas))
    assert max(esperas) == 3600
    assert espera(0) == 0
    assert espera(1000, base=30, teto=3600) == 3600  # sem estouro de numero


def test_espera_padrao_da_configuracao() -> None:
    cfg = ConfigEnvio()
    assert cfg.retry_max == 5
    assert espera(1, cfg.espera_base, cfg.espera_teto) == 30
    # Com 5 novas tentativas, a ultima espera ainda e menor que o teto: tudo se resolve em ~15 min.
    assert sum(espera(n, cfg.espera_base, cfg.espera_teto) for n in range(1, cfg.retry_max + 1)) == 930


@pytest.mark.parametrize(
    "corpo",
    [
        {"argumentos": {"mes": "2026-10"}},
        {"texto": "x", "detalhe": {"resultado": "segredo"}},
        {"lista": [{"cpf": "123"}]},
        {"Authorization": "Bearer x"},
        {"email": "ana@exemplo.teste"},
        {"objeto": object()},
    ],
)
def test_corpo_recusa_conteudo_e_dado_pessoal(corpo: dict[str, object]) -> None:
    with pytest.raises(CorpoRecusado):
        conferir_corpo(corpo)


def test_corpo_aceita_frase_numeros_e_identificadores() -> None:
    corpo = {"texto": "Ordem venceu.", "ordem_id": "abc", "contagens": {"leituras": 3}, "linhas": ["a", "b"]}
    assert conferir_corpo(corpo) == corpo


def test_corpo_grande_demais() -> None:
    with pytest.raises(CorpoRecusado, match="tamanho"):
        conferir_corpo({"texto": "x" * 20000})


def test_mensagem_de_email_simples_e_sem_quebra_no_assunto() -> None:
    aviso = Aviso(
        id=3,
        canal="email",
        destino="admin@exemplo.teste",
        evento="resumo_semanal",
        assunto="Resumo\r\nBcc: intruso@exemplo.teste",
        corpo={"texto": "Resumo da semana.", "linhas": ["Leituras feitas: 3"]},
        tentativas=0,
        criado_em=datetime(2026, 11, 18, tzinfo=UTC),
    )
    msg = montar_mensagem(aviso, "mcp-claro@exemplo.teste", "admin@exemplo.teste")
    assert msg["Subject"] == "Resumo Bcc: intruso@exemplo.teste"
    assert msg["Bcc"] is None
    assert msg["Message-ID"] == f"<{aviso.identificador}@exemplo.teste>"
    assert msg["Auto-Submitted"] == "auto-generated"
    texto = msg.get_content()
    assert "Resumo da semana." in texto
    assert "- Leituras feitas: 3" in texto
    assert "aviso automatico" in texto


def test_destinos_e_forma_de_email() -> None:
    import uuid

    pessoa = uuid.uuid4()
    assert destino_pessoa(pessoa) == f"pessoa:{pessoa}"
    assert parece_email("admin@exemplo.teste")
    for ruim in ("sem-arroba", "a@b", "a b@c.d", "a@@c.d", "a@c.d\nBcc: x", "@c.d"):
        assert not parece_email(ruim), ruim


def test_canal_de_email_so_com_host_e_remetente() -> None:
    assert EnvioEmail.de_parametros(Parametros()) is None
    envio = EnvioEmail.de_parametros(Parametros(SMTP_HOST="correio", SMTP_FROM="mcp@exemplo.teste"))
    assert envio is not None
    assert envio.starttls is True
    assert envio.tls_direto is False
    direto = EnvioEmail.de_parametros(Parametros(SMTP_HOST="correio", SMTP_FROM="mcp@exemplo.teste", SMTP_PORT=465))
    assert direto is not None
    assert direto.tls_direto is True
    assert direto.starttls is False
