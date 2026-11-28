"""Aplicacao do papel ``rotinas`` de verdade: ciclo de vida com as tarefas, fila de avisos e alertas do Grafana.

Sobe ``criar_app_rotinas`` com uvicorn, PostgreSQL real (usuario ``mcp_claro_rotinas``) e
Mailpit. Um alerta do Grafana entra pela rota interna, vira aviso e chega por e-mail pelo
laco de envio; as tarefas periodicas rodam todas no ciclo de vida.
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.avisos import ConfigEnvio
from mcp_claro.avisos.email import EnvioEmail
from mcp_claro.config.settings import Parametros
from mcp_claro.rotinas.agendador import Desfecho
from mcp_claro.rotinas.app import criar_app_rotinas, montar_tarefas
from mcp_claro.rotinas.config import ConfigRotinas
from mcp_claro.rotinas.despachante import Conferida, DespachanteFora
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita
from tests.apoio import servidor
from tests.integracao import test_rotinas_apoio as apoio
from tests.integracao.test_rotinas_apoio import Mailpit
from tests.unidade.test_avisos_grafana import carga_grafana

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

# Fixtures compartilhadas (definidas em test_rotinas_apoio).
engine_dono = apoio.engine_dono
engine_rotinas = apoio.engine_rotinas
mailpit = apoio.mailpit
parametros_rotinas = apoio.parametros_rotinas

SENHA = "senha-do-webhook-interno-de-teste"
ADMIN = "admin-app@exemplo.teste"


class SemSistemas:
    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        # Sem despachante: a conferencia nao acontece, e nenhum servidor de terceiros de outros testes do banco da
        # sessao soma rodada sem resposta (nem vai a quarentena) por causa deste teste.
        raise ConferenciaNaoFeita("sem despachante neste teste")

    async def conferir(self, operacao_id: uuid.UUID) -> Conferida:
        # Fase 4: a consulta das acoes em duvida tambem nao acontece (a rodada para, como com o despachante fora).
        raise DespachanteFora("sem despachante neste teste")


def config_rapida() -> ConfigRotinas:
    return ConfigRotinas(
        intervalo_curto=0.5,
        intervalo_aviso_vencer=0.5,
        intervalo_particoes=0.5,
        intervalo_limpeza=0.5,
        intervalo_resumo_semanal=0.5,
        intervalo_mudou_parou=0.5,
        primeira_espera=0.05,
        emails_admin=(ADMIN,),
        envio=ConfigEnvio(intervalo=0.2, espera_base=1, lote=500),
        senha_grafana=SecretStr(SENHA),
    )


@pytest.fixture
async def rotinas(
    parametros_rotinas: Parametros, engine_rotinas: AsyncEngine, mailpit: Mailpit
) -> AsyncIterator[tuple[str, Any]]:
    envio = EnvioEmail(host=mailpit.host, porta=mailpit.smtp, remetente="mcp-claro@exemplo.teste", starttls=False)
    app = criar_app_rotinas(
        parametros_rotinas,
        engine_rotinas,
        config=config_rapida(),
        envios={"email": envio},
        fonte=SemSistemas(),
        consulta=SemSistemas(),
    )
    async with servidor(app) as url:
        yield url, app


async def test_saude_e_tarefas_rodando(rotinas: tuple[str, Any]) -> None:
    url, _ = rotinas
    esperadas = {
        "vencer_ordens",
        "ronda",  # a ronda das portas roda nas rotinas (Fase 1)
        "avisar_ordens_a_vencer",
        "fechamento",
        "particoes",
        "resumo_medicoes",
        "limpeza",
        "resumo_semanal",
        "mudou_parou",
        "programas",  # rotinas dos programas que rodam sozinhos (Fase 2, decisao 9)
        # Fase 2: fichas e limites
        "vencer_fichas",
        "avisar_fichas_pendentes",
        "vencer_limites",
        "limpar_desafios_passkey",  # desafios de passkey vencidos ou usados (correcao da revisao)
        "limpar_sinais",  # Fase 3: sinais dos tres sinais mais velhos que a maior janela
        # Fase 4: a consulta das acoes em duvida ao proprio sistema (pelo despachante) e os avisos dos selos
        "conferir_pelo_sistema",
        "avisar_selos",
    }
    fim = time.monotonic() + 15
    tarefas: dict[str, str] = {}
    async with httpx2.AsyncClient() as http:
        while time.monotonic() < fim:
            r = await http.get(f"{url}/saude")
            assert r.status_code == 200
            corpo = r.json()
            assert corpo["papel"] == "rotinas"
            assert corpo["vivo"] is True
            tarefas = corpo["tarefas"]
            if set(tarefas) == esperadas and all(v == str(Desfecho.FEITA) for v in tarefas.values()):
                break
            await asyncio.sleep(0.2)
    assert set(tarefas) == esperadas
    # Todas rodam com as permissoes do papel rotinas (com os ajustes propostos para a 0003).
    assert all(v == str(Desfecho.FEITA) for v in tarefas.values()), tarefas


async def test_alerta_do_grafana_vira_aviso_e_chega_por_email(
    rotinas: tuple[str, Any], engine_rotinas: AsyncEngine, mailpit: Mailpit
) -> None:
    url, _ = rotinas
    impressao = uuid.uuid4().hex[:16]
    nome = f"Recusas por versao {impressao[:6]}"
    carga = carga_grafana(fingerprint=impressao, labels={"alertname": nome, "severity": "alta"})
    async with httpx2.AsyncClient() as http:
        sem = await http.post(f"{url}/interno/alertas-grafana", json=carga)
        assert sem.status_code == 401
        errada = await http.post(f"{url}/interno/alertas-grafana", json=carga, headers={"authorization": "Bearer x"})
        assert errada.status_code == 401
        basic = base64.b64encode(f"grafana:{SENHA}".encode()).decode()
        r = await http.post(f"{url}/interno/alertas-grafana", json=carga, headers={"authorization": f"Basic {basic}"})
        assert r.status_code == 202
        assert r.json() == {"recebidos": 1, "cortados": 0, "avisos": 1}
        # O Grafana repete o mesmo alerta a cada repeat_interval: nao vira outro aviso.
        de_novo = await http.post(
            f"{url}/interno/alertas-grafana", json=carga, headers={"authorization": f"Bearer {SENHA}"}
        )
        assert de_novo.json()["avisos"] == 0
        grande = await http.post(
            f"{url}/interno/alertas-grafana",
            content=b"{" + b" " * (300 * 1024) + b"}",
            headers={"authorization": f"Bearer {SENHA}", "content-type": "application/json"},
        )
        assert grande.status_code == 413
        torto = await http.post(
            f"{url}/interno/alertas-grafana", content=b"nao e json", headers={"authorization": f"Bearer {SENHA}"}
        )
        assert torto.status_code == 400
        assert (await http.get(f"{url}/interno/alertas-grafana")).status_code == 405
    (msg,) = await mailpit.esperar(f"MCP-CLARO: alerta {nome} disparou")
    assert msg["To"][0]["Address"] == ADMIN
    assert "Recusas por versao antiga subiram." in mailpit.texto(msg["ID"])
    async with engine_rotinas.connect() as c:
        frase = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE tipo = 'alerta_grafana' AND dados ->> 'impressao' = :i"),
                {"i": impressao},
            )
        ).scalar_one()
    assert frase == f"Alerta do Grafana {nome} recebido: disparou."


async def test_sem_senha_configurada_a_rota_recusa(parametros_rotinas: Parametros, engine_rotinas: AsyncEngine) -> None:
    app = criar_app_rotinas(
        parametros_rotinas, engine_rotinas, config=ConfigRotinas(), envios={}, fonte=SemSistemas(), tarefas=[]
    )
    async with servidor(app) as url, httpx2.AsyncClient() as http:
        r = await http.post(
            f"{url}/interno/alertas-grafana", json=carga_grafana(), headers={"authorization": "Bearer x"}
        )
    assert r.status_code == 503


def test_configuracao_sai_dos_parametros(tmp_path: Any) -> None:
    senha = tmp_path / "grafana"
    senha.write_text(SENHA + "\n", encoding="utf-8")
    p = Parametros(CLOSING_INTERVAL_HOURS=4, NOTIFY_RETRY_MAX=2, METRICS_MINUTE_RETENTION_DAYS=10)
    cfg = ConfigRotinas.de_parametros(p)
    assert cfg.fechamento_horas == 4
    assert cfg.envio.retry_max == 2
    assert cfg.retencao_minuto_dias == 10
    assert cfg.senha_grafana is None  # parametro novo ainda nao existe em Parametros
    tarefas = montar_tarefas(None, cfg, SemSistemas())  # type: ignore[arg-type]
    assert len({t.nome for t in tarefas}) == 8
    fechamento = next(t for t in tarefas if t.nome == "fechamento")
    assert fechamento.intervalo == cfg.intervalo_curto
