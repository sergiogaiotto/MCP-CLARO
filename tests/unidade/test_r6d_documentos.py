"""Documentos da revisao cruzada da Fase 3 (grupo R6d), conferidos contra o codigo.

O que se prova, sem banco:

- R6d-1: o teste de carga que o plano pede antes de abrir fase nao existe no repositorio, e o andamento, as decisoes
  e o plano dizem isso; os numeros da conformidade no andamento sao os da lista de falhas esperadas; a restauracao
  com o pgBackRest e terceiros nao foi conferida, e a copia logica foi (a tabela das provas e cada teste da etapa 6 no
  andamento ficam em ``test_docs_fase3_g8a.py``);
- R6d-2: cada prova de isolamento da sala e citada pelo que ela sobe (o Compose de cada uma, lido pela arvore do
  codigo);
- R6d-4: ``VISITS_INTERNAL_URL`` e conferido na partida de toda peca, menos a sala, e o plano, o vigia e as decisoes
  dizem isso;
- R6d-5: os documentos das correcoes dos outros grupos, pelos contratos K1 a K9 da triagem, conferidos contra o codigo
  que os grupos R6a, R6b e R6e entregaram. Os testes ``test_contrato_k*_codigo_*`` conferem o codigo do R6e (K4, K5,
  K7 e K8) pelo contrato, e cada item desses nas decisoes cita os testes ``test_r6e_*`` como prova. A sala cheia na
  leitura das declaracoes (dita pelo processo pai, nao conta rodada; o mesmo corpo escrito por um filho, conta) e
  conferida pelo caminho de verdade em ``test_docs_fase3_g8a.py``;
- R7b: a falha de dado do banco nas declaracoes conta contra o servidor (K5); os nomes neutros vao na ordem do banco
  (K3); a renovacao segura uma conexao do banco enquanto troca a chave, e a defesa da mistura nao cobre dois sistemas
  proprios (os textos do R6a).

Texto para leigo: com acento, sem emoji e sem promessa de garantia (os testes de estilo dos documentos ficam em
``test_docs_fase3_g8a.py`` e ``test_docs_terceiros_g8b.py``; o ``vigia.md`` e conferido aqui).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import re
import tomllib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
import yaml
from sqlalchemy.exc import DBAPIError

from mcp_claro import __main__ as principal
from mcp_claro.cadastro import esquema
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import app as despachante_app
from mcp_claro.despachante import autorizacao, renovacao, servico
from mcp_claro.extrato.frases import FRASES
from mcp_claro.ligacao import cliente, sala_contrato
from mcp_claro.rotinas import despachante as rotinas_despachante
from mcp_claro.saida import despachante as porta
from mcp_claro.telas import admin, autorizar, paginas
from mcp_claro.visitas import app as sala
from mcp_claro.visitas import partida

RAIZ = Path(__file__).resolve().parents[2]
DOCS = RAIZ / "docs"
INTEGRACAO = RAIZ / "tests" / "integracao"
MODELOS = RAIZ / "src" / "mcp_claro" / "telas" / "modelos"


def _junto(texto: str) -> str:
    """O texto numa linha so (as frases quebram linha no documento)."""
    return " ".join(texto.split())


def _ler(nome: str) -> str:
    return _junto((DOCS / nome).read_text(encoding="utf-8"))


def _secao(nome: str, titulo: str) -> str:
    """Do titulo ate o proximo titulo do mesmo nivel ou de cima, numa linha so."""
    linhas = (DOCS / nome).read_text(encoding="utf-8").splitlines()
    inicio = linhas.index(titulo)
    nivel = len(titulo) - len(titulo.lstrip("#"))
    fim = next(
        (
            i
            for i in range(inicio + 1, len(linhas))
            if linhas[i].startswith("#") and len(linhas[i]) - len(linhas[i].lstrip("#")) <= nivel
        ),
        len(linhas),
    )
    return _junto("\n".join(linhas[inicio:fim]))


def _linha(nome: str, comeco: str) -> str:
    return next(x for x in (DOCS / nome).read_text(encoding="utf-8").splitlines() if x.startswith(comeco))


FASE3_ANDAMENTO = "## Fase 3: servidores de terceiros"
MUDANCAS = "## Mudanças deliberadas da Fase 3 (21/11/2026)"
AINDA_EM_ABERTO = "## Ainda em aberto"


# --- R6d-1: o teste de carga que falta, a conformidade e a restauracao -------------------------------------------


def _importa_locust(arquivo: Path) -> bool:
    for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
        if isinstance(no, ast.Import) and any(a.name.split(".")[0] == "locust" for a in no.names):
            return True
        if isinstance(no, ast.ImportFrom) and (no.module or "").split(".")[0] == "locust":
            return True
    return False


def test_r6d1_o_teste_de_carga_que_falta_esta_dito_no_andamento_nas_decisoes_e_no_plano() -> None:
    # Codigo: nenhum .py do repositorio importa o locust, e ele nem e dependencia do projeto.
    pastas = ("tests", "src", "conformidade", "exemplos", "scripts")
    com_locust = [p for pasta in pastas for p in (RAIZ / pasta).rglob("*.py") if _importa_locust(p)]
    assert com_locust == []
    projeto = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    assert "locust" not in repr(projeto).lower()
    # O plano pede a carga antes de abrir fase e diz que ainda nao foi feita.
    carga = _linha("plano.md", "| Carga |")
    assert "| `locust` 2.47.0 em contêiner próprio | Antes de abrir fase." in carga
    assert (
        'Ainda não feito: o repositório não tem teste de carga ([decisões, "Ainda em aberto"]'
        "(decisoes.md#ainda-em-aberto)) |"
    ) in carga
    # O andamento diz que nao ha, sem "como em toda fase".
    fase3 = _secao("andamento.md", FASE3_ANDAMENTO)
    assert "como em toda fase" not in fase3
    assert "não há teste de carga no repositório: o locust da tabela de testes do plano ainda não entrou." in fase3
    assert "Ligar a chave da Fase 3 (D6) depende da decisão do dono sobre ele" in fase3
    # As decisoes: em aberto, e o D6 depende disso.
    aberto = _secao("decisoes.md", AINDA_EM_ABERTO)
    assert (
        "o teste de carga com o `locust`, que o plano pede antes de abrir fase, não existe no repositório: não houve"
        " teste de carga nas Fases 1 e 2 nem nesta. Ligar a chave da Fase 3 (D6) depende dessa decisão do dono;"
    ) in aberto
    assert "e da decisão sobre o teste de carga (item seguinte)" in aberto
    # O caso de pressao da sala nao se passa por teste de carga.
    assert "a sala sob pressão (não é teste de carga):" in fase3


def _numero(padrao: str, texto: str) -> int:
    achado = re.search(padrao, texto)
    assert achado is not None, padrao
    return int(achado.group(1))


def test_r6d1_a_conformidade_no_andamento_e_a_da_lista_de_falhas_esperadas() -> None:
    caminho = RAIZ / "conformidade" / "falhas-esperadas-gateway.yml"
    texto = caminho.read_text(encoding="utf-8")
    lista = yaml.safe_load(texto)
    cenarios = {entrada.split(":", 1)[0] for entrada in lista["server"]}
    exigidos = _numero(r"dos (\d+) cenarios de servidor exigidos", texto)
    passam = _numero(r"(\d+) passam inteiros pelo\s+#\s+gateway", texto)
    assert (exigidos, passam, len(cenarios)) == (37, 10, 27)
    assert exigidos - len(cenarios) == passam
    fase3 = _secao("andamento.md", FASE3_ANDAMENTO)
    assert (
        "suíte oficial de conformidade pelo gateway, direto e pelo portão, com a chave da Fase 3 desligada e ligada:"
        f" {passam} cenários exigidos passam, {len(cenarios)} falham só como previsto, nenhuma falha sem previsão; a"
        " Fase 3 não mudou a lista (`conformidade/falhas-esperadas-gateway.yml`)."
    ) in fase3
    assert "Ela roda fora do pytest, por `conformidade/gateway.py`, como no CI;" in fase3


def test_r6d1_a_restauracao_com_pgbackrest_e_terceiros_nao_foi_conferida_e_a_copia_logica_foi() -> None:
    pgbackrest = (INTEGRACAO / "test_continuidade_pgbackrest.py").read_text(encoding="utf-8")
    assert "sem o repositorio apt do PGDG" in pgbackrest  # o pulo que o andamento cita
    fase3 = _secao("andamento.md", FASE3_ANDAMENTO)
    assert (
        "a restauração com o pgBackRest e servidores de terceiros não foi conferida neste ambiente:"
        " `test_continuidade_pgbackrest.py` pula sem o repositório apt do PGDG. A cópia lógica com terceiros foi"
        " conferida (`test_fase3_ensaios.py::test_copia_logica_com_terceiros_volta_com_os_dados_as_guardas_e_os_grants"
        "_da_fase_3`);"
    ) in fase3


# --- R6d-2: cada prova de isolamento pelo que ela sobe ------------------------------------------------------------


def _servicos_que_sobem(arquivo: Path) -> set[str]:
    """Os servicos de cada ``compose("up", ...)`` do arquivo, pela arvore do codigo."""
    sobem: set[str] = set()
    for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
        if not (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute) and no.func.attr == "compose"):
            continue
        textos = [a.value for a in no.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if textos[:1] == ["up"]:
            sobem |= {t for t in textos[1:] if not t.startswith("-")}
    return sobem


def test_r6d2_cada_prova_de_isolamento_e_citada_pelo_que_ela_sobe() -> None:
    # Codigo: a prova de cada mudanca sobe so o despachante e a sala (com o banco e o cofre como dependencias); a
    # entrada, as rotinas e o login sobem so no ensaio da etapa 6.
    isolada = _servicos_que_sobem(INTEGRACAO / "test_sala_isolada.py")
    ensaio = _servicos_que_sobem(INTEGRACAO / "test_fase3_ensaios.py")
    assert isolada == {"despachante", "visitas"}
    assert {"entrada", "rotinas", "login"} <= ensaio
    assert not {"entrada", "rotinas", "login"} & isolada
    # Documento: nada atribui a test_sala_isolada a entrada, as rotinas e o login.
    decisoes = _ler("decisoes.md")
    assert "que ela não alcança o banco, o cofre, a entrada, as rotinas nem o login" not in decisoes
    assert (
        "`tests/integracao/test_sala_isolada.py` sobe a sala, o despachante, o banco e o cofre e prova, de dentro da"
        " sala, que ela não alcança o banco nem o cofre, pelo nome e pelo IP, nem os metadados das nuvens"
        " (169.254.169.254) e os IPs privados da rede `saida_visitas`"
        " (`tests/integracao/test_sala_isolada.py::test_de_dentro_da_sala_nada_alem_do_despachante_e_da_internet`)."
        " A entrada, as rotinas e o login não sobem nessa prova. Com o Compose inteiro no ar, a prova de que a sala não"
        " os alcança, pelo nome nem pelo IP, é `tests/integracao/test_fase3_ensaios.py::test_de_dentro_da_sala_entrada"
        "_rotinas_login_no_ar_nao_alcancaveis_por_nome_nem_por_ip`."
    ) in decisoes


# --- R6d-4: VISITS_INTERNAL_URL na partida de toda peca -----------------------------------------------------------

TODA_PECA = (
    "Toda peça confere este valor na partida, menos a própria sala. Em piloto e produção, ponha o mesmo"
    " `VISITS_INTERNAL_URL` em todos os papéis, também no vigia da segunda máquina e nos comandos `migrar`,"
    " `migrar-vigia` e `conferir`."
)


def test_r6d4_toda_peca_confere_a_sala_na_partida_e_os_documentos_dizem() -> None:
    # Codigo: a regra mora em Parametros.problemas, que nao sabe o papel...
    assert list(inspect.signature(Parametros.problemas).parameters) == ["self"]
    assert "self._problemas_da_fase3()" in inspect.getsource(Parametros.problemas)
    base = {"APP_ENV": "piloto", "PUBLIC_BASE_URL": "https://mcp-claro.exemplo"}
    sem = Parametros(**base).problemas()
    com = Parametros(**base, VISITS_INTERNAL_URL="https://visitas:9446").problemas()
    assert [e for e in sem if e not in com] == [e for e in sem if "VISITS_INTERNAL_URL" in e]
    assert any(e.startswith("Fora de desenvolvimento e teste, VISITS_INTERNAL_URL e obrigatorio") for e in sem)
    # ... como as regras das Fases 1 e 2 que tambem valem para todo papel.
    for nome in ("SCREENS_CLIENT_SECRET_FILE", "GRAFANA_WEBHOOK_PASSWORD_FILE", "WATCH_CONTINUITY_URL"):
        assert any(nome in e for e in sem), nome
    # ... e todo comando, menos o da sala, confere os parametros do gateway (vigia, migrar, migrar-vigia, conferir).
    assert "p = _conferir_sala() if args.comando == Papel.VISITAS else _conferir()" in inspect.getsource(principal.main)
    assert "Parametros()" in inspect.getsource(principal._conferir)
    # O Compose da segunda maquina do repositorio esta em teste e nao precisa do valor.
    vigia_compose = yaml.safe_load((RAIZ / "docker/vigia/compose.vigia.yml").read_text(encoding="utf-8"))
    assert vigia_compose["x-vigia"]["environment"]["APP_ENV"] == "teste"
    # Documentos.
    linha = _junto(_linha("plano.md", "| `VISITS_INTERNAL_URL` |"))
    assert f"Onde o despachante acha a sala. {TODA_PECA}" not in linha  # a frase vem depois da regra
    assert f"mesmo com a chave da Fase 3 desligada. {TODA_PECA} Sem a sala, nenhum pedido MCP" in linha
    vigia = _ler("vigia.md")
    assert TODA_PECA in vigia
    assert "O vigia não fala com a sala, mas sem o valor ele não sobe." in vigia
    assert "O `docker/vigia/compose.vigia.yml` do repositório está em `APP_ENV: teste` e não precisa dele." in vigia
    tabela = _junto(_linha("vigia.md", "| `VISITS_INTERNAL_URL` |"))
    assert tabela == (
        "| `VISITS_INTERNAL_URL` | URL | nenhum | despachante; a partida de todo papel, menos a sala, confere |"
        " Vazio ou fora de `https://` em piloto e produção; em qualquer ambiente, um valor que não seja só o esquema"
        " (`http://` ou `https://`), o nome e a porta da sala |"
    )
    # A regra de formato vale em todo ambiente, com ou sem a chave da Fase 3.
    formato = "VISITS_INTERNAL_URL e so o esquema, o nome e a porta da sala (ex.: https://visitas:9446)."
    for ambiente in ("desenvolvimento", "teste", "piloto", "producao"):
        extra = {} if ambiente in ("desenvolvimento", "teste") else base
        for errado in ("https://visitas:9446/caminho", "ftp://visitas:9446", "https://eu@visitas:9446"):
            erros = Parametros(**{**extra, "APP_ENV": ambiente, "VISITS_INTERNAL_URL": errado}).problemas()
            assert formato in erros, (ambiente, errado)
    assert formato not in Parametros(APP_ENV="teste", VISITS_INTERNAL_URL="http://visitas:9446").problemas()
    assert not [e for e in Parametros(APP_ENV="teste").problemas() if "VISITS_INTERNAL_URL" in e]
    mudancas = _secao("decisoes.md", MUDANCAS)
    assert (
        "**Ao atualizar um piloto para a Fase 3, `VISITS_INTERNAL_URL` entra no ambiente de todos os papéis.** A"
        " partida de toda peça, menos a sala, confere os parâmetros do gateway inteiro (`Parametros.problemas`, que"
        " não sabe o papel), como nas Fases 1 e 2 com `SCREENS_CLIENT_SECRET_FILE`, `GRAFANA_WEBHOOK_PASSWORD_FILE`"
        " e `WATCH_CONTINUITY_URL`."
    ) in mudancas
    assert "Fazer a partida saber o papel, para exigir o valor só de quem fala com a sala, fica para uma decisão" in (
        mudancas
    )


def test_r6d4_o_vigia_e_para_leigo_com_acento_sem_emoji_e_sem_promessa() -> None:
    texto = (DOCS / "vigia.md").read_text(encoding="utf-8")
    sem_codigo = re.sub(r"`[^`]*`|\"[^\"]*\"", " ", texto)
    assert not re.search(r"\b(nao|voce|versao|revisao|peca|maquina|tambem|so|ate|producao)\b", sem_codigo)
    assert not [c for c in texto if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF]
    assert not re.search(r"(?i)\b(garante|garantimos|garantido|garantida|nunca falha|sem risco)\b|100 ?%", texto)


# --- R6d-5, K1: o endereco de volta de cada servidor de terceiros --------------------------------------------------


def test_r6d5_k1_o_endereco_de_volta_de_cada_terceiro_esta_no_roteiro_nas_decisoes_e_nas_ameacas() -> None:
    # Codigo (R6a): o endereco de volta de um terceiro e so dele; os proprios continuam com o da Fase 2.
    sistema_id = uuid.uuid4()
    assert autorizacao.caminho_da_volta(sistema_id) == f"/telas/autorizar/{sistema_id}/volta"
    assert autorizacao.caminho_da_volta(None) == autorizacao.CAMINHO_VOLTA_PROPRIOS == "/telas/autorizar/volta"
    assert autorizacao.ESTADO_DE_OUTRO_SISTEMA == "estado_de_outro_sistema"
    rotas = {getattr(r, "path", "") for r in autorizar.TelasDeAutorizacao(cast(Any, None)).rotas()}
    assert {"/autorizar/volta", "/autorizar/{sistema_id:uuid}/volta"} <= rotas
    preparar = inspect.getsource(autorizacao._preparar_troca)
    assert preparar.index("_conferir_volta(") < preparar.index("abrir_segredo_cliente(")
    concluir = inspect.getsource(autorizacao.concluir_autorizacao)
    assert concluir.index("_preparar_troca(") < concluir.index("trocar(")
    tela = (MODELOS / "admin" / "acesso.html").read_text(encoding="utf-8")
    aviso = "No servidor de autorização deste fornecedor, cadastre este endereço de volta. Ele é só deste servidor:"
    assert aviso in tela
    mensagem = autorizar.MENSAGENS["estado_de_outro_sistema"]
    # Roteiro do ADMIN, passo 3.
    passo = _secao("terceiros.md", "## Passo 3. Guardar a autorização e as chaves")
    assert (
        "Num servidor de terceiros, o endereço de volta é só dele: ele termina em"
        " `/telas/autorizar/<número do servidor>/volta`. Os sistemas próprios continuam com `/telas/autorizar/volta`."
    ) in passo
    assert f"> {aviso} (o endereço)" in passo
    assert "Copie o endereço de volta que a tela mostra e peça ao fornecedor para cadastrá-lo" in passo
    # Decisoes.
    mudancas = _secao("decisoes.md", MUDANCAS)
    assert (
        "**Endereço de volta só de cada servidor de terceiros.** Num servidor de terceiros, a volta da página de"
        " autorização chega por um endereço só dele, `/telas/autorizar/<número do servidor>/volta`, e a entrada diz"
        " ao despachante por qual endereço ela chegou. Os sistemas próprios continuam com `/telas/autorizar/volta`, o"
        " endereço já cadastrado nos servidores de autorização desde a Fase 2."
    ) in mudancas
    assert (
        "O despachante recusa, com o motivo `estado_de_outro_sistema`, a volta que chega pelo endereço de outro"
        " sistema e a de um terceiro que chega pela volta dos próprios, antes de abrir o segredo do cliente, de"
        " decifrar o verificador e de trocar o código. Nada é enviado a ninguém nem guardado"
    ) in mudancas
    assert f'a pessoa lê "{mensagem}"' in mudancas
    assert "O `iss` na volta (RFC 9207) ainda não é conferido." in mudancas
    # Modelo de ameacas.
    ameacas = _ler("modelo-de-ameacas.md")
    assert (
        "| Um terceiro manda a pessoa à página de autorização de outro sistema, com o pedido dele (mistura de"
        " servidores de autorização), para trocar o código dela no endereço de troca dele |"
    ) in ameacas
    assert (
        "Cada servidor de terceiros tem um endereço de volta só dele (`/telas/autorizar/<id>/volta`). A volta que chega"
        " pelo endereço de outro sistema, ou a de um terceiro pela volta dos próprios, é recusada"
        " (`estado_de_outro_sistema`) antes de abrir o segredo do cliente, decifrar o verificador ou trocar o código:"
        " nada vai a ninguém."
    ) in ameacas


# --- R6d-5, K2: prazo total da troca de chave e espera curta da renovacao -----------------------------------------


def test_r6d5_k2_o_prazo_da_troca_e_a_espera_da_renovacao_estao_nas_decisoes_nas_ameacas_e_na_saida() -> None:
    # Codigo (R6a).
    assert autorizacao.PRAZO_TROCA == 15.0
    assert autorizacao.ESPERA_TRAVA_RENOVACAO == 3.0
    assert "anyio.fail_after(PRAZO_TROCA)" in inspect.getsource(autorizacao.trocar)
    assert 'raise TrocaIndisponivel("prazo")' in inspect.getsource(autorizacao.trocar)
    assert 'RenovacaoIndisponivel("trava_ocupada")' in inspect.getsource(renovacao.renovar)
    assert "autorizacao.travar_com_prazo(" in inspect.getsource(renovacao)
    detalhe = "nao deu para renovar a chave"
    assert f'return None, None, "{detalhe}"' in inspect.getsource(porta.PortaDeSaida._chave)
    prazo, espera = f"{autorizacao.PRAZO_TROCA:g} s", f"{autorizacao.ESPERA_TRAVA_RENOVACAO:g} s"
    # Decisoes.
    mudancas = _secao("decisoes.md", MUDANCAS)
    assert (
        f"**Prazo total da troca de chave (OAuth) e espera curta da renovação (vale para todos).** A troca de chave"
        f" tem um prazo total de {prazo}: abrir a conexão, mandar o pedido e ler a resposta inteira."
    ) in mudancas
    assert (
        f"A renovação espera no máximo {espera} pela vez, quando outra renovação da mesma pessoa no mesmo sistema já"
        " está em curso."
    ) in mudancas
    riscos = _secao("decisoes.md", "### Riscos aceitos da Fase 3")
    assert (
        "**Troca de chave lenta ocupa a porta de saída.** Cada ação de um terceiro com o endereço de troca de chave"
        f" lento ainda pode ocupar a porta única de saída por até {prazo} da troca, mais a espera da vez (até {espera})"
        " e a conferência do endereço de troca (até 3 s)."
    ) in riscos
    # Modelo de ameacas.
    ameacas = _ler("modelo-de-ameacas.md")
    assert (
        f"O teto de tempo da troca: a troca inteira tem um prazo total de {prazo} (abrir a conexão, mandar o pedido e"
        f" ler a resposta inteira), e a renovação espera no máximo {espera} pela vez de outra renovação da mesma pessoa"
        " no mesmo sistema."
    ) in ameacas
    assert (
        "O que fica: cada ação de um terceiro com o endereço de troca de chave lento ainda pode ocupar a porta única"
        f" de saída por até {prazo} da troca, mais a espera da vez e a conferência do endereço"
    ) in ameacas
    # Porta de saida: o detalhe "nao deu para renovar a chave" tambem vem do prazo.
    saida = _ler("saida.md")
    assert (
        'Quando a renovação falha, o detalhe diz "não deu para renovar a chave"; isso inclui o servidor de autorização'
        f" que não responde dentro do prazo total de {prazo} da troca e outra renovação da mesma pessoa no mesmo"
        f" sistema que segura a vez por mais de {espera}."
    ) in saida
    assert f"com prazo total de {prazo} para a troca inteira" in saida


class _Engine:
    """Engine sem banco: a transacao e a conexao so abrem e fecham (as leituras sao trocadas no teste)."""

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        yield object()

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[object]:
        yield object()


def _renovar_sem_a_vez(monkeypatch: pytest.MonkeyPatch, vence_em: datetime) -> renovacao.ChaveAberta | Exception:
    """``renovacao.renovar`` com a trava do par segura por outra renovacao alem da espera curta, e a chave de acesso
    guardada vencendo em ``vence_em``: a chave usada ou o erro. Nada pode ir ao servidor de autorizacao."""

    async def sem_a_vez(*_: object, **__: object) -> bool:
        return False

    async def ler(_c: object, _sistema: object, _pessoa: object, tipo: str) -> SimpleNamespace:
        assert tipo == "acesso"
        return SimpleNamespace(cifrada="cifrada", expira_em=vence_em)

    async def abrir(*_: object) -> str:
        return "chave-atual"

    async def trocar(*_: object, **__: object) -> NoReturn:
        raise AssertionError("sem a vez, nada vai ao servidor de autorizacao")

    monkeypatch.setattr(autorizacao, "travar_com_prazo", sem_a_vez)
    monkeypatch.setattr(renovacao, "_ler", ler)
    monkeypatch.setattr(renovacao, "_abrir", abrir)
    monkeypatch.setattr(renovacao, "_trocar_com_trava", trocar)

    async def rodar() -> renovacao.ChaveAberta | Exception:
        try:
            return await renovacao.renovar(
                cast(Any, _Engine()), cast(Any, None), sistema_id=uuid.uuid4(), pessoa_id=uuid.uuid4()
            )
        except Exception as erro:  # o teste olha o erro
            return erro

    return asyncio.run(rodar())


def test_r6d5_k2_sem_a_vez_a_renovacao_usa_a_chave_que_ainda_nao_venceu_como_as_decisoes_dizem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Codigo (R6a). No prazo esgotado da troca, a chave atual, se ainda vale; senao, a renovacao falha...
    trocar_com_trava = inspect.getsource(renovacao._trocar_com_trava)
    assert (
        "except autorizacao.TrocaIndisponivel:\n        await _registrar_falha(c, sistema_id, pessoa_id, sistema.nome,"
        ' "o sistema nao respondeu")\n        return atual or RenovacaoIndisponivel('
    ) in trocar_com_trava
    # ... e sem a vez, o mesmo: a chave atual que ainda nao venceu e usada, mesmo dentro da margem de renovacao...
    assert timedelta(minutes=1) == renovacao.MARGEM
    agora = datetime.now(UTC)
    usada = _renovar_sem_a_vez(monkeypatch, agora + renovacao.MARGEM / 2)
    assert usada == renovacao.ChaveAberta("chave-atual", "cifrada")
    # ... e, com ela vencida, a renovacao falha com `trava_ocupada`, que a porta mostra como "nao deu para renovar".
    vencida = _renovar_sem_a_vez(monkeypatch, agora - timedelta(seconds=1))
    assert isinstance(vencida, renovacao.RenovacaoIndisponivel)
    assert str(vencida) == "trava_ocupada"
    # Decisoes.
    mudancas = _secao("decisoes.md", MUDANCAS)
    assert "Sem a vez, ela desiste sem segurar conexão do banco, com o mesmo desfecho." not in mudancas
    assert (
        "Sem a vez, ela desiste sem segurar conexão do banco e sem nada enviado. Depois, lê de novo a chave atual e a"
        ' usa se ela ainda não venceu, mesmo no último minuto, quando já seria renovada; senão, a ação fica "não'
        ' saiu" ("não deu para renovar a chave") e a leitura é recusada.'
    ) in mudancas
    assert (
        "`tests/integracao/test_r6a_renovacao_prazo.py`, com"
        " `::test_sem_a_trava_a_renovacao_usa_a_chave_atual_se_ela_ainda_vale`"
    ) in mudancas
    # Porta de saida.
    assert "Nos dois casos, se a chave atual ainda não venceu, ela é usada e a ação sai." in _ler("saida.md")


class _EngineQueConta:
    """Engine sem banco que conta as transacoes abertas (cada uma segura uma conexao do grupo do despachante)."""

    def __init__(self) -> None:
        self.abertas = 0

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        self.abertas += 1
        try:
            yield object()
        finally:
            self.abertas -= 1


def test_r7b_r6a_a_renovacao_troca_com_uma_conexao_aberta_e_a_mistura_entre_proprios_nao_e_coberta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Codigo (R6a). A renovacao troca a chave de dentro da transacao da trava: uma conexao do banco fica presa durante
    # a troca. A trava e do par (pessoa, sistema): pessoas diferentes no mesmo sistema nao dividem a vez.
    engine = _EngineQueConta()
    abertas_na_troca: list[int] = []

    async def com_a_vez(*_: object, **__: object) -> bool:
        return True

    async def ler(_c: object, _sistema: object, _pessoa: object, _tipo: str) -> SimpleNamespace:
        return SimpleNamespace(cifrada="cifrada", expira_em=datetime.now(UTC) - timedelta(seconds=1))

    async def trocar(c: object, *_: object, **__: object) -> renovacao.ChaveAberta:
        abertas_na_troca.append(engine.abertas)
        return renovacao.ChaveAberta("chave-nova", "cifrada-nova")

    monkeypatch.setattr(autorizacao, "travar_com_prazo", com_a_vez)
    monkeypatch.setattr(renovacao, "_ler", ler)
    monkeypatch.setattr(renovacao, "_trocar_com_trava", trocar)
    nova = asyncio.run(
        renovacao.renovar(cast(Any, engine), cast(Any, None), sistema_id=uuid.uuid4(), pessoa_id=uuid.uuid4())
    )
    assert nova == renovacao.ChaveAberta("chave-nova", "cifrada-nova")
    assert abertas_na_troca == [1]
    assert engine.abertas == 0
    monkeypatch.undo()
    assert "resposta = await autorizacao.trocar(" in inspect.getsource(renovacao._trocar_com_trava)
    sistema = uuid.uuid4()
    assert autorizacao.chave_da_trava(uuid.uuid4(), sistema) != autorizacao.chave_da_trava(uuid.uuid4(), sistema)
    # Os sistemas proprios dividem a mesma volta: a defesa da mistura e so entre um terceiro e os outros sistemas.
    assert autorizacao.caminho_da_volta(None) == autorizacao.CAMINHO_VOLTA_PROPRIOS
    # Decisoes e ameacas.
    riscos = _secao("decisoes.md", "### Riscos aceitos da Fase 3")
    assert (
        "Cada renovação também segura uma conexão do banco do despachante enquanto troca a chave. Renovações de pessoas"
        " diferentes no mesmo terceiro lento não dividem a vez, e cada uma pode segurar a sua conexão por até cerca de"
        f" {autorizacao.PRAZO_TROCA:g} s."
    ) in riscos
    mudancas = _secao("decisoes.md", MUDANCAS)
    assert (
        "A defesa supõe que o servidor de autorização de cada sistema só aceita o endereço de volta que tem cadastrado"
        " (RFC 6749, 3.1.2). Ela não cobre a mistura entre dois sistemas próprios, que dividem a mesma volta."
    ) in mudancas
    assert (
        "A defesa conta com o servidor de autorização de cada sistema aceitar só o endereço de volta que tem cadastrado"
        " (RFC 6749, 3.1.2) e não cobre a mistura entre dois sistemas próprios, que dividem a mesma volta."
    ) in _ler("modelo-de-ameacas.md")


# --- R6d-5, K3: esquema de terceiro com nomes neutros --------------------------------------------------------------


def test_r6d5_k3_o_esquema_de_terceiro_vai_com_nomes_neutros_e_sem_schema() -> None:
    # Codigo (R6b): nomes neutros na ordem do banco (a chave mais curta antes), $ref reescrito, $schema fora.
    declarado = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"pedido": {"$ref": "#/$defs/NomeDoFornecedor"}},
        "$defs": {"NomeDoFornecedor": {"type": "string"}},
    }
    neutro = esquema.nomes_neutros(declarado)
    assert esquema.PREFIXO_NEUTRO == "d"
    assert neutro == {
        "type": "object",
        "properties": {"pedido": {"$ref": "#/$defs/d1"}},
        "$defs": {"d1": {"type": "string"}},
    }
    # Duas definicoes fora da ordem do banco: o fornecedor escreveu "zeta" antes de "a", e as referencias tambem
    # vem nessa ordem; a mais curta ("a") e d1, e cada $ref aponta para a sua.
    fora_da_ordem = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/zeta"}, "y": {"$ref": "#/$defs/a"}},
        "$defs": {"zeta": {"type": "string"}, "a": {"type": "integer"}},
    }
    assert esquema.nomes_neutros(fora_da_ordem) == {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/d2"}, "y": {"$ref": "#/$defs/d1"}},
        "$defs": {"d2": {"type": "string"}, "d1": {"type": "integer"}},
    }
    # Tres ordens diferentes: documento c, ab, b; so por letras ab, b, c; a do banco (a mais curta antes, depois
    # pelas letras) b, c, ab. So a ordem do banco da b=d1, c=d2, ab=d3.
    tres_ordens = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/c"}, "y": {"$ref": "#/$defs/ab"}, "z": {"$ref": "#/$defs/b"}},
        "$defs": {"c": {"type": "string"}, "ab": {"type": "integer"}, "b": {"type": "boolean"}},
    }
    tres = esquema.nomes_neutros(tres_ordens)
    assert tres["properties"] == {"x": {"$ref": "#/$defs/d2"}, "y": {"$ref": "#/$defs/d3"}, "z": {"$ref": "#/$defs/d1"}}
    assert tres["$defs"] == {"d2": {"type": "string"}, "d3": {"type": "integer"}, "d1": {"type": "boolean"}}
    decisoes = _ler("decisoes.md")
    assert (
        "No esquema que o aplicativo vê, os nomes das definições (`$defs` e `definitions`) viram nomes neutros (`d1`,"
        " `d2`, ...), com cada `$ref` reescrito, e o `$schema` da raiz sai: a entrada confere sempre como 2020-12. Os"
        " números seguem uma ordem fixa, a ordem em que o banco guarda as chaves (a mais curta antes), e não a ordem em"
        " que o fornecedor as escreveu: assim a revisão, a prévia do ADMIN e o aplicativo veem os mesmos nomes."
    ) in decisoes
    assert "na ordem do documento" not in decisoes
    assert "`tests/integracao/test_r6b_nomes_do_banco.py`" in decisoes
    assert (
        "Continuam indo ao modelo de IA, porque são precisos para chamar: nomes de campo, valores de `enum` e"
        " `const`, e `pattern`."
    ) in decisoes
    passo = _secao("terceiros.md", "## Passo 5. Revisar cada ferramenta")
    assert (
        "Nele, as partes internas do esquema aparecem com nomes neutros (`d1`, `d2`...), no lugar dos nomes que o"
        " fornecedor deu."
    ) in passo
    assert "o esquema gravado com até 256 KiB" not in passo


# --- R6d-5, K4 e K9: o quadro "Pedidos recusados deste sistema" -----------------------------------------------------

ROTULOS_NOVOS = {
    "declaracoes_sistema_pediu_dados": "Na leitura das ferramentas",
    "sondagem_sistema_pediu_dados": "Na sondagem",
    "acao_nao_confirmada": "Numa ação sem confirmação",
    "acao_nao_saiu": "Numa ação, que não saiu",
}


def test_r6d5_k4_k9_o_quadro_de_pedidos_recusados_conta_fora_da_chamada_e_as_acoes_que_nao_terminaram_bem() -> None:
    # Codigo (R6b): os rotulos da tela.
    for tipo, rotulo in ROTULOS_NOVOS.items():
        assert paginas.ONDE_O_PEDIDO[tipo] == rotulo, tipo
    # Roteiro do ADMIN.
    roteiro = _ler("terceiros.md")
    assert (
        "Conta também os pedidos que o servidor fez fora de uma chamada, na leitura das ferramentas e na sondagem, e"
        ' as ações que terminaram sem confirmação ou que não saíram depois de um pedido recusado. A coluna "Onde" diz'
        f' em que ponto foi: por exemplo, "{ROTULOS_NOVOS["declaracoes_sistema_pediu_dados"]}",'
        f' "{ROTULOS_NOVOS["sondagem_sistema_pediu_dados"]}", "{ROTULOS_NOVOS["acao_nao_confirmada"]}" ou'
        f' "{ROTULOS_NOVOS["acao_nao_saiu"]}". Na leitura das ferramentas e na sondagem, a linha vem sem o nome de'
        " ferramenta."
    ) in roteiro
    # Decisoes.
    decisoes = _ler("decisoes.md")
    assert (
        "**Pedido barrado fora de uma chamada também vai ao extrato (vale para todos).** Quando um sistema pede algo no"
        " meio da leitura das ferramentas (a lista das declarações, na revisão e no mudou-parou) ou no meio da"
        " sondagem de versão, o pedido continua barrado e agora fica registrado: o extrato ganha a linha"
        " `declaracoes_sistema_pediu_dados` ou `sondagem_sistema_pediu_dados`, sem pessoa, com as categorias do nosso"
        " vocabulário, e o contador `pedidos_do_sistema_barrados` soma, com `onde=declaracoes` ou `onde=sondagem`."
    ) in decisoes
    assert (
        'O quadro "Pedidos recusados deste sistema", na tela do ADMIN, conta essas linhas'
        f' ("{ROTULOS_NOVOS["declaracoes_sistema_pediu_dados"]}" e'
        f' "{ROTULOS_NOVOS["sondagem_sistema_pediu_dados"]}", sem ferramenta) e também as ações que terminaram'
        f' "não confirmada" ("{ROTULOS_NOVOS["acao_nao_confirmada"]}") ou "não saiu"'
        f' ("{ROTULOS_NOVOS["acao_nao_saiu"]}") depois de um pedido barrado.'
    ) in decisoes
    assert (
        "Prova: `src/mcp_claro/despachante/servico.py`, `tests/integracao/test_r6e_pedidos_fora_da_chamada.py` e"
        " `tests/integracao/test_r6b_pedidos_na_tela.py`."
    ) in decisoes
    assert "As linhas `acao_enviada` e `acao_sistema_pediu_dados` do extrato levam as categorias em" not in decisoes
    assert (
        "A linha do fim da ação leva as categorias em `dados.pedidos`: sempre em `acao_enviada` e"
        " `acao_sistema_pediu_dados`, e em `acao_nao_confirmada` e `acao_nao_saiu` quando houve pedido barrado."
    ) in decisoes
    saida = _ler("saida.md")
    assert (
        'A linha do fim no extrato guarda as categorias (`dados.pedidos`), também quando a ação fica "não confirmada"'
        ' ou "não saiu" depois do pedido, para a tela do ADMIN mostrar os pedidos recusados.'
    ) in saida


def test_contrato_k4_codigo_do_r6e_grava_os_pedidos_fora_da_chamada() -> None:
    """O codigo do R6e pelo contrato K4: as linhas e o contador dos pedidos fora da chamada."""
    assert {"declaracoes_sistema_pediu_dados", "sondagem_sistema_pediu_dados"} <= set(FRASES)
    for tipo in ("declaracoes_sistema_pediu_dados", "sondagem_sistema_pediu_dados"):
        assert FRASES[tipo].campos() == {"sistema", "pedidos"}, tipo
    servico = (RAIZ / "src/mcp_claro/despachante/servico.py").read_text(encoding="utf-8")
    assert '"onde": "declaracoes"' in servico or 'onde="declaracoes"' in servico
    assert '"onde": "sondagem"' in servico or 'onde="sondagem"' in servico


class _LigacaoQuePedeEFalha:
    """O terceiro pede algo no meio da listagem e depois manda um erro: a sala diz o pedido na falha."""

    async def listar(self, *_: object) -> NoReturn:
        raise sala_contrato.FalhaNaSala("duvida", "ErroDoServidor", pedidos=("fio_antigo:elicitation/create",))


def test_r7b_k4_o_pedido_barrado_numa_listagem_que_falha_fica_registrado_e_a_falha_conta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Codigo (R6e): a listagem que falha depois de um pedido grava a linha do pedido (sem pessoa) e soma o contador;
    # a falha continua o servidor que nao entregou (502, a rodada conta).
    registrados: list[tuple[str, tuple[str, ...]]] = []
    contados: list[tuple[tuple[str, ...], str]] = []

    async def registrar_sala(_c: object, _sistema: object, linha: str | None) -> str | None:
        return linha

    async def registrar_pedidos(_c: object, _sistema: object, tipo: str, pedidos: Any) -> None:
        registrados.append((tipo, tuple(pedidos)))

    def contar_pedidos(pedidos: Any, *, onde: str) -> None:
        contados.append((tuple(pedidos), onde))

    @contextlib.asynccontextmanager
    async def begin() -> AsyncIterator[None]:
        yield None

    despachante: Any = object.__new__(servico.Despachante)
    despachante.ligacao = lambda _sistema: _LigacaoQuePedeEFalha()
    despachante.engine = SimpleNamespace(begin=begin)
    monkeypatch.setattr(despachante, "_registrar_sala", registrar_sala)
    monkeypatch.setattr(despachante, "_registrar_pedidos", registrar_pedidos)
    monkeypatch.setattr(despachante, "_contar_pedidos", contar_pedidos)
    sistema = SimpleNamespace(
        id=uuid.uuid4(), nome="terceiro-x", endereco="https://x.exemplo/mcp", origem="terceiro", de_terceiro=True
    )

    async def listar() -> Exception:
        try:
            await despachante._listar(sistema, None, None)
        except Exception as erro:  # o teste olha o tipo do erro
            return erro
        raise AssertionError("a listagem que falhou nao levantou")

    erro = asyncio.run(listar())
    assert type(erro) is servico.DeclaracoesIndisponiveis
    assert registrados == [("declaracoes_sistema_pediu_dados", ("fio_antigo:elicitation/create",))]
    assert contados == [(("fio_antigo:elicitation/create",), "declaracoes")]
    # Na sondagem, o pedido vem na propria sondagem (tambem a que passou do prazo) e e gravado com qualquer desfecho.
    sondar = inspect.getsource(servico.Despachante.sondar_sistema)
    assert 'await self._registrar_pedidos(c, sistema, "sondagem_sistema_pediu_dados", sondagem.pedidos)' in sondar
    assert 'self._contar_pedidos(sondagem.pedidos, onde="sondagem")' in sondar
    # Decisoes.
    assert (
        "Vale também quando a leitura das ferramentas falha depois do pedido (o servidor pede e depois manda um erro) e"
        " quando a sondagem passa do prazo depois do pedido: o pedido fica no extrato e no contador, e a falha conta"
        " como antes."
    ) in _secao("decisoes.md", MUDANCAS)


# --- R6d-5, K5: a falha nossa nas declaracoes nao conta ------------------------------------------------------------


def test_r6d5_k5_a_falha_nossa_nas_declaracoes_nao_conta_contra_o_servidor() -> None:
    # Codigo das rotinas (ja no repositorio): o motivo conta como falha do MCP-CLARO, e um 503 nunca conta.
    assert "conferencia_nao_feita" in rotinas_despachante.MOTIVOS_DO_MCP_CLARO
    assert "resposta.status_code in (404, 503)" in inspect.getsource(rotinas_despachante.ClienteDespachante)
    quarentena = _secao("decisoes.md", "### Quarentena e mudou-parou")
    assert "um 500 ou o prazo de leitura" not in quarentena
    assert (
        "Uma rodada que o MCP-CLARO não conseguiu fazer não conta nem zera: o despachante fora do ar (sem conexão, 503"
        " ou 404 dele), a sala de visitas fora, a sala de visitas cheia dita pelo processo pai dela (503 com o motivo"
        " `sala_sem_vaga`), a chave das declarações que o cofre não abriu e uma falha do MCP-CLARO no banco ou no"
        " grupo de conexões do despachante durante a conferência (503 com o motivo `conferencia_nao_feita`)."
    ) in quarentena
    assert (
        "Uma falha do banco ou do grupo de conexões do despachante durante a leitura (por exemplo, todas as conexões"
        " ocupadas) é 503 `conferencia_nao_feita`."
    ) in quarentena
    # As provas do R6e: o pool tomado da 503, e duas rodadas com o banco do despachante fora nao levam a quarentena.
    assert "`tests/integracao/test_r6e_vistas.py::test_pool_tomado_nas_declaracoes_e_503_conferencia_nao_feita`" in (
        quarentena
    )
    assert (
        "`tests/integracao/test_r6e_vistas.py::test_duas_rodadas_com_o_banco_do_despachante_fora_nao_levam_a_quarentena`"
    ) in quarentena


def test_contrato_k5_codigo_do_r6e_responde_503_conferencia_nao_feita() -> None:
    """O codigo do R6e pelo contrato K5: a rota das declaracoes da 503 com o motivo na falha do banco."""
    fontes = {
        nome: (RAIZ / "src/mcp_claro/despachante" / nome).read_text(encoding="utf-8")
        for nome in ("app.py", "servico.py")
    }
    assert any('"conferencia_nao_feita"' in fonte for fonte in fontes.values())
    assert any("SQLAlchemyError" in fonte for fonte in fontes.values())
    assert "status_code=503" in fontes["app.py"]


class _Origem(Exception):
    """O erro do driver do banco, com o SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("x")
        self.sqlstate = sqlstate


class _LigacaoQueEntrega:
    """O servidor entregou a lista (uma ferramenta valida): o que falha, se falhar, e o banco ao gravar."""

    async def listar(self, *_: object) -> cliente.Listagem:
        return cliente.Listagem([{"name": "consultar", "inputSchema": {"type": "object"}}])


def _listar_com_o_banco_que_recusa(sqlstate: str) -> BaseException:
    """``Despachante._listar`` de um terceiro cuja lista o banco recusa ao gravar, com ``sqlstate``: o erro que sai."""

    @contextlib.asynccontextmanager
    async def begin() -> AsyncIterator[None]:
        raise DBAPIError("INSERT INTO ferramentas_vistas ...", {}, _Origem(sqlstate))
        yield None  # pragma: no cover

    despachante: Any = object.__new__(servico.Despachante)
    despachante.ligacao = lambda _sistema: _LigacaoQueEntrega()
    despachante.engine = SimpleNamespace(begin=begin)
    sistema = SimpleNamespace(
        id=uuid.uuid4(), nome="terceiro-x", endereco="https://x.exemplo/mcp", origem="terceiro", de_terceiro=True
    )

    async def listar() -> BaseException:
        try:
            await despachante._listar(sistema, None, None)
        except BaseException as erro:  # o teste olha o tipo do erro
            return erro
        raise AssertionError("o banco recusou e a listagem nao levantou")

    return asyncio.run(listar())


def test_r7b_k5_o_banco_que_recusa_o_que_o_servidor_declarou_conta_contra_o_servidor() -> None:
    # Codigo: o banco que recusa o dado declarado (classes 22 e 54) e o servidor que nao entregou (502, a rodada
    # conta); a falha nossa (um impasse, 40P01) sobe como veio, e a rota a responde com 503 conferencia_nao_feita.
    assert {"22", "54"} == servico._CLASSES_DE_DADO
    for sqlstate in ("22P05", "22001", "54000"):
        assert type(_listar_com_o_banco_que_recusa(sqlstate)) is servico.DeclaracoesIndisponiveis, sqlstate
    impasse = _listar_com_o_banco_que_recusa("40P01")
    assert isinstance(impasse, DBAPIError)
    assert isinstance(impasse, despachante_app.FALHA_DO_BANCO)
    assert despachante_app.CONFERENCIA_NAO_FEITA == "conferencia_nao_feita"
    # O que o banco nao guarda e recusado antes de gravar: nome de mais de 1.000 bytes, caractere nulo, texto que nao
    # se escreve em UTF-8. Nome de mais de 128 letras que cabe em 1.000 bytes fica guardado, e a revisao o recusa.
    assert servico.TETO_BYTES_NOME == 1000
    assert admin.TETO_NOME_FERRAMENTA == 128

    def ferramenta(nome: str) -> dict[str, Any]:
        return {"name": nome, "inputSchema": {"type": "object"}}

    assert servico._declaracao_invalida([ferramenta("a" * 1001)])
    assert servico._declaracao_invalida([ferramenta("a\x00b")])
    assert servico._declaracao_invalida([ferramenta("a\ud800")])
    assert not servico._declaracao_invalida([ferramenta("a" * 300)])
    # Documentos.
    quarentena = _secao("decisoes.md", "### Quarentena e mudou-parou")
    assert (
        "O 503 `conferencia_nao_feita` é só para a falha nossa: um impasse no banco, a conexão que cai ou o grupo de"
        " conexões tomado. Quando o banco recusa o que o servidor declarou, a falha é do servidor: um nome de"
        " ferramenta com mais de 1.000 bytes, um texto com caractere nulo ou que não se escreve em UTF-8, ou um erro do"
        " banco causado pelo dado ao gravar (as classes 22 e 54 do PostgreSQL) dão 502 `declaracoes_indisponiveis`, e"
        " a rodada conta. Um nome de mais de 128 letras que cabe em 1.000 bytes continua guardado: o ADMIN o vê, e a"
        " revisão o recusa."
    ) in quarentena
    for prova in ("`tests/unidade/test_r6e_falha_de_dado.py`", "`tests/integracao/test_r6e_dados_do_terceiro.py`"):
        assert prova in quarentena, prova


# --- R6d-5, K6: a entrada perde em sistemas o que nenhum codigo usa -------------------------------------------------


def test_r6d5_k6_a_entrada_perde_em_sistemas_o_que_nenhum_codigo_usa() -> None:
    migracao = (RAIZ / "migrations/versions/0006_fase3.py").read_text(encoding="utf-8")
    assert "REVOKE UPDATE (nome, endereco, tipo_chave) ON sistemas FROM mcp_claro_entrada;" in migracao
    assert "REVOKE INSERT (autorizacao) ON sistemas FROM mcp_claro_entrada;" in migracao
    banco = _secao("decisoes.md", "### Banco: migração 0006")
    assert (
        "**A entrada perde em `sistemas` o que nenhum código usa (vale para todos).** A 0006 tira da entrada, a peça"
        " voltada à internet, o direito de mudar o nome, o endereço e o tipo de chave de um sistema e o de gravar a"
        " autorização no cadastro de um sistema novo."
    ) in banco
    assert "Essas mudanças são da equipe, pelo dono das tabelas do banco, como o roteiro do ADMIN já diz." in banco
    assert "`tests/integracao/test_r6b_grants_entrada.py`" in banco


# --- R6d-5, K7: a sala cheia --------------------------------------------------------------------------------------

# Quantas frases, por extenso (feminino), como o roteiro escreve: "Estas quatro frases".
_FRASES_POR_EXTENSO: dict[int, str] = {2: "duas", 3: "três", 4: "quatro", 5: "cinco", 6: "seis", 7: "sete", 8: "oito"}


def test_r6d5_k7_a_sala_cheia_por_mais_de_2_s_da_nao_saiu_nos_documentos() -> None:
    contrato = _secao("decisoes.md", "### Sala de visitas: o contrato entre o despachante e a sala")
    assert (
        f"**Sala cheia: nada sai, sem dúvida.** A sala atende até {sala.MAX_CHAMADAS} chamadas ao mesmo tempo, somando"
        ' todos os terceiros. Uma chamada espera uma vaga por no máximo 2 s; sem vaga, a sala responde "nada'
        ' enviado", com a exceção `sala_sem_vaga`, e nada vai a um filho.'
    ) in contrato
    assert 'Na ação, isso é "não saiu" ("a sala de visitas estava cheia; nada saiu")' in contrato
    assert (
        "Prova: `src/mcp_claro/visitas/app.py`, `tests/unidade/test_r6e_sala_vagas.py` e, para as declarações,"
        " `tests/unidade/test_r6e_sala_cheia_marcada.py`, `tests/integracao/test_r6e_sala_cheia.py` e"
    ) in contrato
    # Na leitura das declaracoes, a sala cheia dita pelo processo pai nao conta contra o servidor (o caminho de verdade
    # e conferido em test_docs_fase3_g8a.py); o risco que fica esta nas decisoes, nas ameacas e no simulado.
    assert "O mesmo corpo de sala cheia sem o cabeçalho, que um filho tomado pelo terceiro poderia escrever" in contrato
    riscos = _secao("decisoes.md", "### Riscos aceitos da Fase 3")
    assert (
        f"**Sala cheia em toda conferência.** Se as {sala.MAX_CHAMADAS} vagas da sala ficarem ocupadas por mais de 2 s"
        " na hora de cada rodada do mudou-parou, nenhuma conferência de terceiro é feita, e um servidor que mudou só é"
        " visto quando a sala esvazia."
    ) in riscos
    assert "Sala cheia na conferência leva à quarentena" not in riscos
    assert (
        'Na sala, um filho tomado pelo terceiro pode, depois de chamar o terceiro, escrever qualquer resposta "nada'
        ' enviado" do contrato, também a de sala cheia (`sala_sem_vaga`)'
    ) in riscos
    ameacas = _secao("modelo-de-ameacas.md", "### Isolamento dos processos da sala")
    assert (
        f'Com as {sala.MAX_CHAMADAS} vagas ocupadas por mais de 2 s, a chamada seguinte não sai ("não saiu"), sem'
        " ficar em dúvida: nada vai a um filho."
    ) in ameacas
    assert (
        "Na conferência do mudou-parou, a sala cheia dita pelo processo pai (com um cabeçalho que o filho não consegue"
        " escrever) é falha do MCP-CLARO e não conta contra o servidor; o mesmo corpo escrito por um filho tomado, sem"
        " o cabeçalho, conta como o servidor que não entregou. Com a sala cheia em toda rodada, nenhuma conferência de"
        " terceiro é feita (contador `sala_sem_vaga`, `onde=declaracoes`). Isso vale também para o terceiro lento que"
        " ocupa as vagas: enquanto a sala estiver cheia, a mudança dele não é vista; a vitrine continua só com o que"
        " foi revisado."
    ) in ameacas
    assert (
        "- Sala cheia em toda conferência: enquanto durar, nenhuma conferência de terceiro é feita, nem a do terceiro"
        ' lento que ocupa as vagas (risco aceito, em [decisões, "Riscos aceitos da Fase 3"]'
        "(decisoes.md#riscos-aceitos-da-fase-3))."
    ) in _ler("modelo-de-ameacas.md")
    assert "Sala cheia na conferência" not in _ler("modelo-de-ameacas.md")
    assert 'Uma quarentena por "Não respondeu" também pode vir da sala de visitas cheia' not in _ler("terceiros.md")
    simulado = _secao("simulado-dia-ruim.md", "### 12. Sala de visitas fora do ar")
    assert (
        f"Com a sala no ar, mas cheia (as {sala.MAX_CHAMADAS} vagas ocupadas por mais de 2 segundos), a ação também"
        ' vira "não saiu", e o mudou-parou também não conta a rodada contra o servidor.'
    ) in simulado
    assert "mas o mudou-parou conta a rodada como sem resposta" not in simulado
    # Na tela do ADMIN, a leitura das ferramentas com uma falha do MCP-CLARO (a sala fora ou cheia, o cofre, o banco)
    # tem frase propria e 503; o roteiro traz cada uma entre os problemas do MCP-CLARO, e a frase comum fica so para o
    # servidor que nao entregou e as outras falhas.
    assert {"sala_fora", "sala_sem_vaga", "chave_indisponivel", "conferencia_nao_feita"} <= admin.DECLARACOES_NOSSAS
    assert "declaracoes_indisponiveis" not in admin.DECLARACOES_FALHA
    passo4 = _secao("terceiros.md", "## Passo 4. Ler as ferramentas")
    nossas, _, depois = passo4.partition("Espere um pouco e, se continuar, avise a equipe que opera o gateway.")
    assert depois, passo4
    # O roteiro diz quantas sao, em algarismo ou por extenso: se o numero mudar no codigo, o documento muda junto.
    quantas = len(admin.DECLARACOES_NOSSAS)
    assert quantas in _FRASES_POR_EXTENSO, quantas
    assert any(
        f"Estas {numero} frases são problemas do MCP-CLARO, e não do servidor:" in nossas
        for numero in (quantas, _FRASES_POR_EXTENSO[quantas])
    ), quantas
    for motivo in sorted(admin.DECLARACOES_NOSSAS):
        assert f'"{admin.DECLARACOES_FALHA[motivo][1]}"' in nossas, motivo
    comum = admin.DECLARACOES_FALHA[""][1]
    assert f'"{comum}" aparece quando o servidor não entregou a lista e nas outras falhas: confira a chave' in depois
    assert "como a sala de visitas cheia" not in passo4
    saida = _secao("saida.md", "## Ações em servidores de terceiros")
    assert (
        f'todas as {sala.MAX_CHAMADAS} vagas da sala de visitas ficaram ocupadas por mais de 2 segundos: "a sala de'
        ' visitas estava cheia; nada saiu"'
    ) in saida


def test_contrato_k7_codigo_do_r6e_sala_sem_vaga() -> None:
    """O codigo do R6e pelo contrato K7: o prazo da vaga, a excecao e o detalhe da porta."""
    assert getattr(sala, "PRAZO_VAGA", None) == 2.0
    sem_vaga = getattr(sala_contrato, "SALA_SEM_VAGA", None)
    assert sem_vaga == "sala_sem_vaga"
    assert porta._NAO_ENVIADO[sem_vaga] == "a sala de visitas estava cheia; nada saiu"


# --- R6d-5, K8: a sala nao sobe sem APP_ENV -------------------------------------------------------------------------

SEM_APP_ENV = (
    "A sala de visitas precisa de APP_ENV escrito no ambiente dela (desenvolvimento, teste, piloto ou producao): sem"
    " ele, ela subiria como desenvolvimento e aceitaria enderecos locais."
)


def test_r6d5_k8_a_sala_nao_sobe_sem_app_env_nos_documentos() -> None:
    simulado = _ler("simulado-dia-ruim.md")
    assert (
        "A sala recusa subir sem `APP_ENV` escrito no ambiente dela: sem ele, ela subiria como desenvolvimento e"
        " aceitaria endereços locais."
    ) in simulado
    sala_doc = _secao("decisoes.md", "### A chave da Fase 3 e a sala de visitas")
    assert (
        "**A sala não sobe sem `APP_ENV`.** A partida da sala de visitas recusa subir sem `APP_ENV` escrito no ambiente"
        " dela, com a frase"
    ) in sala_doc
    assert f'"{SEM_APP_ENV}"' in sala_doc
    assert "Prova: `src/mcp_claro/visitas/partida.py` e `tests/unidade/test_r6e_ambiente_sala.py`." in sala_doc
    ameacas = _secao("modelo-de-ameacas.md", "### Isolamento dos processos da sala")
    assert "e recusa subir sem `APP_ENV` escrito no ambiente dela (sem ele, subiria como desenvolvimento" in ameacas


def test_contrato_k8_codigo_do_r6e_sala_recusa_sem_app_env() -> None:
    """O codigo do R6e pelo contrato K8: a partida da sala recusa sem APP_ENV no ambiente."""
    segredos = {
        nome: "/run/secrets/x" for nome in ("INTERNAL_CERT_FILE", "INTERNAL_KEY_FILE", "INTERNAL_CLIENT_CA_FILE")
    }
    p = Parametros(**segredos)
    assert SEM_APP_ENV in partida.problemas_da_sala(p, dict(segredos))
    assert SEM_APP_ENV not in partida.problemas_da_sala(p, {**segredos, "APP_ENV": "teste"})
