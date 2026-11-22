"""Documentos da Fase 3 do grupo G8b: plano, modelo de ameacas, segredos, roteiro do ADMIN, simulado e o comentario
das falhas esperadas da conformidade.

Os documentos citam numeros e textos de tela que o codigo define. Estes testes conferem os dois lados: o numero ou
o texto ainda e o do codigo (ou do modelo da tela), e o documento ainda o traz. Quem mudar a tela ou o codigo ve
aqui qual documento ficou velho. Tambem conferem as frases que a critica mandou corrigir (B8 e a linha do
``OBS_TRACE_PROPAGATE_DOWNSTREAM``), os links entre os documentos e o estilo (acento, sem emoji, sem promessa de
garantia).

Do retoque da etapa 5 (G8br): onde a frase da tela depende de uma condicao (singular e plural, o motivo da
quarentena), o teste monta a frase pelo proprio modelo (``_renderizar``) e confere a frase inteira no documento. Os
textos que o G6ar e o G6br mudaram (aviso da revisao de terceiro, teto de 500 caixas, ferramenta fora da revisao,
nota de "Minhas ordens", "Como passou" do alerta, pedido barrado na tela da acao) e a troca de chave no despachante
(B8: plano, modelo de ameacas, segredos e a mensagem da partida) tambem.

Do segundo retoque: o quadro da recusa da conclusao traz so os motivos que chegam a ele (os outros a tela barra antes,
com frase propria), e as frases em volta das citadas sao conferidas inteiras (o resumo errado nao passa so porque a
citacao esta certa).

Dos acertos finais (etapa 5f): as frases da B8 inteiras (README, plano, roteiro e modelo de ameacas), com o que elas
dizem conferido no codigo e no Compose; o fornecedor fora do tamanho com ``FORNECEDOR_LETRAS``; as duas notas de
"Minhas ordens" inteiras, para cada pessoa; o ``liberar_locais`` desligado fora dos testes; e as duas saidas da
quarentena que a guarda da 0006 aceita. No retoque: o ``liberar_locais`` levado como veio, da partida do despachante
ate a troca (pela arvore do codigo e rodando o app montado), e quem sai para a internet pelo ``internal`` das redes do
Compose, nao pelo nome delas.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml
from sqlalchemy.ext.asyncio import create_async_engine

from exemplos import mal_comportado, terceiro_falante
from mcp_claro import __version__
from mcp_claro.cadastro import esquema
from mcp_claro.cadastro.modelos import Sistema
from mcp_claro.cadastro.quarentena import MOTIVO_PARADA
from mcp_claro.config import politica
from mcp_claro.config.settings import Parametros, PropagacaoRastro
from mcp_claro.despachante import app as despachante_app
from mcp_claro.despachante import autorizacao, renovacao, servico
from mcp_claro.extrato.frases import AVISOS_ADMIN, FRASES
from mcp_claro.ligacao import cliente, ponto, sala_contrato
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.papeis import Papel
from mcp_claro.regras import estranhos, sinais
from mcp_claro.rotinas import despachante as rotinas_despachante
from mcp_claro.rotinas import mudou_parou
from mcp_claro.saida import despachante as porta
from mcp_claro.saida import pedidos
from mcp_claro.telas import admin, admin_portas, autorizar, nucleo, paginas, rotas, seguranca
from mcp_claro.telas.acoes import como_passou, detalhe_do_desfecho
from mcp_claro.versao import VERSAO_MCP
from mcp_claro.visitas import app as sala
from mcp_claro.visitas.partida import PREFIXOS_PROIBIDOS
from tests.cifra_falsa import CifraFalsa

RAIZ = Path(__file__).resolve().parents[2]
DOCS = RAIZ / "docs"
MODELOS = RAIZ / "src" / "mcp_claro" / "telas" / "modelos"

PLANO = DOCS / "plano.md"
AMEACAS = DOCS / "modelo-de-ameacas.md"
SEGREDOS = DOCS / "segredos.md"
TERCEIROS = DOCS / "terceiros.md"
SIMULADO = DOCS / "simulado-dia-ruim.md"
README = RAIZ / "README.md"
FALHAS = RAIZ / "conformidade" / "falhas-esperadas-gateway.yml"
COMPOSE = RAIZ / "docker" / "compose.yml"
FONTES = RAIZ / "src" / "mcp_claro"
MIGRACAO_0006 = RAIZ / "migrations" / "versions" / "0006_fase3.py"

DOCS_DO_GRUPO = (PLANO, AMEACAS, SEGREDOS, TERCEIROS, SIMULADO, README)


def _plano(texto: str) -> str:
    """Texto corrido: as quebras de linha e os espacos repetidos viram um espaco so."""
    return " ".join(texto.split())


def _ler(caminho: Path) -> str:
    return _plano(caminho.read_text(encoding="utf-8"))


def _tela(nome: str) -> str:
    return _ler(MODELOS / nome)


def _campo(nome: str) -> Any:
    return Parametros.model_fields[nome]


def _faixa(nome: str) -> tuple[int, int, int]:
    campo = _campo(nome)
    minimo = next(m.ge for m in campo.metadata if hasattr(m, "ge"))
    maximo = next(m.le for m in campo.metadata if hasattr(m, "le"))
    return int(campo.default), int(minimo), int(maximo)


def _milhar(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _secao(caminho: Path, titulo: str) -> str:
    """O texto corrido de uma secao (de um titulo ``## `` ate o proximo do mesmo nivel ou mais alto)."""
    linhas = caminho.read_text(encoding="utf-8").splitlines()
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
    return _plano("\n".join(linhas[inicio:fim]))


def _terceiro(nome: str) -> Sistema:
    return Sistema(uuid.uuid4(), nome, "https://t.exemplo/mcp", "terceiro", "por_pessoa", 1, "ativo", VERSAO_MCP)


# --- Item 4: o roteiro do ADMIN cita os textos que as telas mostram ---

# Os motivos da recusa do despachante que a tela da conclusao mostra no quadro "A revisao do servidor nao foi
# concluida". Os outros tres a tela confere antes de chamar o despachante, com frase propria (ou recusa o pedido):
# nunca chegam ao quadro (``test_o_quadro_da_conclusao_so_traz_os_motivos_que_a_tela_mostra``).
MOTIVOS_NO_QUADRO_DA_CONCLUSAO = (
    "sem_chave_declaracoes",
    "ferramentas",
    "situacao",
    "versao",
    "sem_resposta",
    "situacao_mudou",
    "chave_indisponivel",
)
MOTIVOS_QUE_A_TELA_BARRA_ANTES = ("nao_e_terceiro", "fornecedor", "publicadas_demais")

# Textos que o codigo das telas define (constantes): o roteiro precisa trazer cada um, igual.
TEXTOS_DO_CODIGO_NO_ROTEIRO = [
    paginas.ORIGENS["terceiro"],
    admin.ORIGEM_FALTA,
    admin.ENDERECO_TERCEIRO_HTTPS,
    admin.ENDERECO_NAO_PUBLICO,
    admin.ENDERECO_NOME_INVALIDO,
    admin.CHAVE_DE_TERCEIRO,
    admin.MODELO_FORA_DA_REVISAO,
    admin_portas.TERCEIRO_JA_CONFIGURADO,
    # As falhas do MCP-CLARO ao ler as ferramentas (503, cada uma com a sua frase) e a frase comum.
    *(admin.DECLARACOES_FALHA[motivo][1] for motivo in sorted(admin.DECLARACOES_NOSSAS)),
    admin.DECLARACOES_FALHA[""][1],
    paginas.DESFECHOS_SONDAGEM["desvio"],
    paginas.DESFECHOS_SONDAGEM["nao_mcp"],
    paginas.DESFECHOS_SONDAGEM["sem_acesso"],
    paginas.DESFECHOS_SONDAGEM["inalcancavel"],
    *paginas.INALCANCAVEL_POR_MOTIVO.values(),
    *(paginas.MOTIVOS_REVISAO_SERVIDOR[m] for m in MOTIVOS_NO_QUADRO_DA_CONCLUSAO),
    *paginas.PROBLEMAS_REVISAO.values(),
    paginas.AVISOS["servidor_revisado"],
    paginas.AVISOS["servidor_revisao_recusada"],
    paginas.AVISOS["despachante_mudo_revisao"],
    paginas.AVISOS["despachante_demorou_revisao"],
    paginas.AVISOS["ferramenta_revisada_terceiro"],
    paginas.ESQUEMA_FUNDO_DEMAIS,
    admin.CAIXAS_DEMAIS,
    autorizar.MENSAGENS["terceiros_desligados"],
]

# Textos escritos nos modelos das telas: o modelo ainda os tem, e o documento tambem.
TEXTOS_DOS_MODELOS = [
    (
        "admin/sistemas.html",
        TERCEIROS,
        "Servidores de terceiros ficam desligados até a Fase 3 passar nas provas. Você pode cadastrar, conferir e"
        " revisar; só pessoas de teste os veem.",
    ),
    (
        "admin/sistemas.html",
        TERCEIROS,
        "Esta escolha não muda depois. Um servidor de terceiros é atendido na sala de visitas: só no endereço"
        " combinado, sem seguir desvios, com a chave guardada à parte e sem dados de rastreio. Ele só aparece para as"
        " pessoas depois que você revisar o servidor inteiro.",
    ),
    ("admin/sistemas.html", TERCEIROS, "Cadastrar e conferir a versão"),
    ("admin/sistemas.html", TERCEIROS, "Uma chave por pessoa (cada pessoa autoriza)"),
    ("admin/sistemas.html", TERCEIROS, "Uma chave para todos (só leitura)"),
    ("admin/sistemas.html", TERCEIROS, "De quem é este servidor?"),
    ("admin/sistema.html", TERCEIROS, "Ler as ferramentas agora"),
    ("admin/sistema.html", TERCEIROS, "Conferir a versão de novo"),
    ("admin/sistema.html", TERCEIROS, "Concluir a revisão do servidor"),
    ("admin/sistema.html", TERCEIROS, "Quem fornece este servidor"),
    ("admin/sistema.html", TERCEIROS, "Mostrar na vitrine"),
    ("admin/sistema.html", TERCEIROS, "Mudou desde a revisão"),
    ("admin/sistema.html", TERCEIROS, "Pedidos recusados deste sistema"),
    (
        "admin/sistema.html",
        TERCEIROS,
        "Ao concluir, o MCP-CLARO lê o servidor de novo. Se algo mudou desde a sua revisão, a conclusão é recusada e"
        " mostra o quê.",
    ),
    ("admin/sistema.html", TERCEIROS, "Nunca revisado. Nenhuma ferramenta dele aparece para as pessoas."),
    (
        "admin/sistema.html",
        TERCEIROS,
        "O servidor não respondeu à última conferência. Se não responder à próxima, entra em quarentena.",
    ),
    (
        "admin/sistema.html",
        TERCEIROS,
        "A revisão anterior deixou de valer quando o servidor saiu do ar; conclua a revisão de novo. Até lá, nenhuma"
        " ferramenta dele aparece para as pessoas.",
    ),
    (
        "admin/sistema.html",
        TERCEIROS,
        "A revisão anterior deixou de valer quando o servidor saiu do ar. Quando ele voltar e passar na conferência da"
        " versão, conclua a revisão de novo. Até lá, nenhuma ferramenta dele aparece para as pessoas.",
    ),
    (
        "admin/sistema.html",
        TERCEIROS,
        "A revisão anterior deixou de valer com a quarentena. Nenhuma ferramenta dele aparece para as pessoas.",
    ),
    ("admin/sistemas.html", TERCEIROS, "Servidor de terceiros: só endereço https da internet."),
    ("admin/ferramenta.html", TERCEIROS, "Escreva um nome seu: o nome declarado é texto do fornecedor."),
    (
        "ordens.html",
        TERCEIROS,
        "Uma ferramenta marcada “de terceiros” é de um servidor de fora da organização. O MCP-CLARO não controla o que"
        " ele faz do lado de lá, e o que ele responde chega ao aplicativo marcado como texto de estranhos.",
    ),
    (
        "ordens.html",
        TERCEIROS,
        "Enquanto os servidores de terceiros estiverem desligados nesta instalação, as ferramentas marcadas “de"
        " terceiros” não funcionam para você: o aplicativo não as vê. Só pessoas de teste as usam.",
    ),
    ("admin/sistema.html", TERCEIROS, '{% if d.get("mudanca") == "nova" %}ferramenta nova'),
    ("admin/sistema.html", TERCEIROS, '{% elif d.get("mudanca") == "sumiu" %}o servidor não declara mais'),
    ("admin/sistema.html", TERCEIROS, "{% else %}mudou a definição{% endif %}"),
    ("acoes/ficha.html", SIMULADO, '<h2 id="alerta-tres-sinais">Alerta dos três sinais</h2>'),
    ("acoes/ficha.html", SIMULADO, "<p><strong>Seus limites com este aplicativo não valem agora.</strong></p>"),
    ("admin/sistema.html", TERCEIROS, "Este servidor está em quarentena."),
    (
        "admin/sistema.html",
        TERCEIROS,
        "Ele mudou a definição de uma ferramenta depois da sua revisão. Mesmo que tenha mudado só o texto, nada dele"
        " chega às pessoas até você revisar de novo.",
    ),
    ("admin/sistema.html", TERCEIROS, "{% if not versao_certa %}<li>Toque em “Conferir a versão de novo”.</li>"),
    ("admin/sistema.html", TERCEIROS, "Toque em “Ler as ferramentas agora”."),
    ("admin/pessoas.html", TERCEIROS, "(teste)"),
    ("admin/sistema.html", TERCEIROS, "Revise as marcadas “Mudou desde a revisão”."),
    ("admin/sistema.html", TERCEIROS, "Conclua a revisão do servidor."),
    ("admin/ferramenta.html", TERCEIROS, "Sempre, em servidor de terceiros: a resposta conta como texto de fora."),
    (
        "admin/ferramenta.html",
        TERCEIROS,
        "Nada do texto escrito pelo fornecedor vai para o aplicativo, nem as dicas como “só leitura”.",
    ),
    ("admin/ferramenta.html", TERCEIROS, "O que o aplicativo vai ver"),
    ("admin/ferramenta.html", TERCEIROS, "Gravar a revisão"),
    ("admin/ferramenta.html", TERCEIROS, "Descrição de cada campo (opcional)"),
    (
        "admin/acesso.html",
        TERCEIROS,
        "Servidor de terceiros: a troca do código e a renovação da chave só vão a endereços https da internet. Depois"
        " de guardada, esta configuração não muda; para mudar, cadastre o servidor de novo.",
    ),
    ("admin/acesso.html", TERCEIROS, "Chave só para ler as declarações"),
    ("admin/acesso.html", TERCEIROS, "Guardar a configuração"),
    ("admin/acesso.html", TERCEIROS, "Chave compartilhada"),
    (
        "admin/modelos.html",
        TERCEIROS,
        "Enquanto a Fase 3 estiver desligada, só pessoas de teste usam ferramentas de terceiros.",
    ),
    ("admin/modelos.html", TERCEIROS, "(de terceiros)"),
    ("admin/bloqueios.html", TERCEIROS, "Um sistema: nada vai a ele"),
    ("admin/inicio.html", TERCEIROS, "Modelos de ordem"),
    ("admin/inicio.html", TERCEIROS, "Bloqueios e pausas"),
    (
        "acoes/_estado.html",
        TERCEIROS,
        "O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos.",
    ),
    (
        "acoes/ficha.html",
        TERCEIROS,
        "Este servidor é de terceiros. O MCP-CLARO não controla o que ele faz do lado de lá.",
    ),
    (
        "acoes/_estado.html",
        SIMULADO,
        "O MCP-CLARO não repassa a você pedidos de dados, links ou confirmações que um sistema faça no meio de uma"
        " chamada. Um texto de resposta ainda pode trazer um link: confirme ações só na tela do MCP-CLARO.",
    ),
    (
        "acoes/_estado.html",
        SIMULADO,
        "O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos.",
    ),
    (
        "acoes/limites.html",
        SIMULADO,
        "Um limite suspenso não volta sozinho. Depois que o ADMIN revisar o servidor de novo, crie outro limite, se"
        " ainda precisar.",
    ),
    (
        "admin/sistema.html",
        SIMULADO,
        "O servidor não respondeu à última conferência. Se não responder à próxima, entra em quarentena.",
    ),
    ("admin/sistema.html", SIMULADO, "Este servidor está em quarentena."),
    ("admin/sistema.html", SIMULADO, "Pedidos recusados deste sistema"),
]


@pytest.mark.parametrize("texto", TEXTOS_DO_CODIGO_NO_ROTEIRO)
def test_o_roteiro_traz_cada_texto_que_o_codigo_das_telas_mostra(texto: str) -> None:
    assert _plano(texto) in _ler(TERCEIROS)


def test_o_roteiro_traz_a_recusa_por_versao_como_a_tela_monta() -> None:
    antes, depois = paginas.DESFECHOS_SONDAGEM["versao"].split("{versoes}")
    roteiro = _ler(TERCEIROS)
    assert _plano(antes) in roteiro
    assert _plano(depois) in roteiro
    assert f"**Aceito.** Este servidor fala a versão {VERSAO_MCP} do MCP." in roteiro
    assert "<strong>Aceito.</strong> Este {% if terceiro %}servidor{% else %}sistema{% endif %} fala a versão" in (
        _tela("admin/sistema.html")
    )


@pytest.mark.parametrize(("modelo", "documento", "texto"), TEXTOS_DOS_MODELOS)
def test_texto_da_tela_esta_no_modelo_e_no_documento(modelo: str, documento: Path, texto: str) -> None:
    assert _plano(texto) in _tela(modelo), f"o modelo {modelo} mudou: atualize {documento.name}"
    # No documento, o texto sem as marcas do modelo (condicoes do Jinja e etiquetas).
    legivel = re.sub(r"\{%.*?%\}|<[^>]+>", "", texto)
    assert _plano(legivel) in _ler(documento), f"{documento.name} nao traz o texto da tela {modelo}"


# Botoes, campos e titulos que o roteiro manda tocar ou preencher: o modelo os tem nesse papel (um botao continua
# botao), e o roteiro usa o mesmo nome.
ROTULOS = [
    ("admin/sistemas.html", "<legend>{}</legend>", "De quem é este servidor?"),
    ("admin/sistemas.html", ">{}</button>", "Cadastrar e conferir a versão"),
    ("admin/sistema.html", ">{}</button>", "Ler as ferramentas agora"),
    ("admin/sistema.html", ">{}</button>", "Conferir a versão de novo"),
    ("admin/sistema.html", ">{}</button>", "Concluir a revisão do servidor"),
    ("admin/sistema.html", ">{}</label>", "Quem fornece este servidor"),
    ("admin/sistema.html", "<legend>{}</legend>", "Mostrar na vitrine"),
    ("admin/sistema.html", ">{}</a>", "Revisar"),
    ("admin/sistema.html", ">{}</h2>", "Revisão do servidor"),
    ("admin/sistema.html", ">{}</h2>", "Pedidos recusados deste sistema"),
    ("admin/ferramenta.html", ">{}</button>", "Gravar a revisão"),
    ("admin/ferramenta.html", "<legend>{}</legend>", "Descrição de cada campo (opcional)"),
    ("admin/ferramenta.html", "<h2>{}</h2>", "O que o aplicativo vai ver"),
    ("admin/acesso.html", ">{}</button>", "Guardar a configuração"),
    ("admin/acesso.html", "<h2>{}</h2>", "Chave só para ler as declarações"),
    ("admin/acesso.html", "<h2>{}</h2>", "Chave compartilhada"),
    ("admin/acesso.html", "<h2>{}</h2>", "Autorização de cada pessoa"),
    ("admin/acesso.html", "<h2>{}</h2>", "Segredo do MCP-CLARO no sistema"),
    ("admin/inicio.html", ">{}</a></h2>", "Sistemas"),
    ("admin/inicio.html", ">{}</a></h2>", "Pessoas"),
    ("admin/inicio.html", ">{}</a></h2>", "Avisos por webhook"),
    ("admin/sistemas.html", "<h2>{}</h2>", "Cadastrar um sistema"),
    ("admin/sistemas.html", ">{}</label>", "Nome do sistema"),
    ("admin/sistemas.html", ">{}</label>", "Endereço MCP do sistema"),
    ("admin/sistemas.html", ">{}</label>", "Tipo de chave"),
    ("admin/sistema.html", ">{}</a></p>", "Autorização e chaves deste sistema"),
    ("admin/sistema.html", "<h2>{}</h2>", "Ferramentas que o sistema declara"),
    ("admin/sistema.html", '<span class="etiqueta">{}</span>', "Sem revisão"),
    ("admin/sistema.html", '<span class="etiqueta bom">{}</span>', "Revisada"),
    ("admin/sistema.html", '<span class="etiqueta ruim">{}</span>', "Mudou desde a revisão"),
    ("admin/ferramenta.html", ">{}</label>", "Nome na vitrine"),
    ("admin/ferramenta.html", ">{}</label>", "Descrição própria"),
    ("admin/ferramenta.html", "<legend>{}</legend>", "Classe"),
    ("admin/ferramenta.html", "> {}: não muda nada no sistema.</label>", "Só lê"),
    ("admin/ferramenta.html", "> {}.</label>", "Muda algo e não tem volta"),
    ("admin/ferramenta.html", "> {}.</label>", "Muda algo, mas dá para desfazer"),
    ("admin/ferramenta.html", "<legend>{}</legend>", "Risco"),
    ("admin/ferramenta.html", "<legend>{}</legend>", "Texto de fora"),
    ("admin/ferramenta.html", "<legend>{}</legend>", "Campos pessoais da resposta"),
    ("admin/ferramenta.html", "<legend>{}</legend>", "Campos do pedido que dizem o destinatário"),
    ("admin/webhooks.html", ">{}</label>", "Eventos (separados por vírgula)"),
    ("ordens.html", "<h1>{}</h1>", "Minhas ordens de serviço"),
]


@pytest.mark.parametrize(("modelo", "forma", "rotulo"), ROTULOS)
def test_botoes_e_campos_do_roteiro_existem_na_tela(modelo: str, forma: str, rotulo: str) -> None:
    assert forma.format(rotulo) in _tela(modelo), f"o modelo {modelo} mudou o rotulo: atualize terceiros.md"
    roteiro = _ler(TERCEIROS)
    assert f'"{rotulo}"' in roteiro or f"**{rotulo}**" in roteiro


def _renderizar(modelo: str, inicio: str, fim: str, *, etiquetas: bool = False, **contexto: Any) -> str:
    """O texto que a tela mostra num trecho do modelo (de ``inicio`` ate ``fim``, sem ele), com as condicoes do
    Jinja resolvidas pelo ``contexto`` e sem as etiquetas HTML. Assim, o documento confere a frase montada, com o
    singular ou o plural que a tela escolhe, e nao so pedacos dela. Com ``etiquetas``, o HTML montado, com elas
    (para conferir que a frase aparece, e nao so que esta no texto)."""
    bruto = (MODELOS / modelo).read_text(encoding="utf-8")
    trecho = bruto[bruto.index(inicio) : bruto.index(fim, bruto.index(inicio))]
    html = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=True).from_string(trecho).render(contexto)
    return _plano(html if etiquetas else re.sub(r"<[^>]+>", " ", html))


def _cartao_da_revisao(**contexto: Any) -> str:
    return _renderizar(
        "admin/sistema.html",
        "{% set revistas = terceiro.fora_da_vitrine %}",
        "{% if detalhe.conferencias_sem_resposta %}",
        **contexto,
    )


def test_o_roteiro_separa_as_ferramentas_revistas_das_que_mudaram_no_servidor() -> None:
    """O cartao "Revisao do servidor" diz duas coisas diferentes (G6a, pedido 4a): a ferramenta revista pelo ADMIN
    sai da vitrine; a que so mudou no servidor continua nela, com o texto da revisao, ate o mudou-parou."""
    acompanhar = _secao(TERCEIROS, "## Acompanhar o servidor")
    revista = _cartao_da_revisao(terceiro={"fora_da_vitrine": 1, "mudaram_no_servidor": 0})
    mudou = _cartao_da_revisao(terceiro={"fora_da_vitrine": 0, "mudaram_no_servidor": 1})
    assert revista.startswith("1 ferramenta foi revista depois desta revisão. Ela fica fora da vitrine")
    assert mudou.startswith("1 ferramenta mudou no servidor desde a sua revisão.")
    assert "Até lá, ela continua na vitrine, com o texto da sua revisão." in mudou
    assert revista in acompanhar
    assert mudou in acompanhar
    assert (
        "Se você revisou de novo uma ferramenta publicada, ele diz quantas ficaram fora da vitrine até você concluir"
        " de novo."
    ) in acompanhar
    assert (
        "Se, na última leitura das ferramentas, uma ferramenta publicada estava diferente da sua revisão, ele avisa"
        " que ela ainda não saiu da vitrine."
    ) in acompanhar
    # O texto velho dizia que as que mudaram no servidor tambem ficavam fora da vitrine.
    assert "Se uma ferramenta mudou ou foi revista depois, ele diz quantas ficaram fora da vitrine" not in acompanhar
    # O codigo: so a revista (outra versao ativa) sai da vitrine; a que mudou continua publicada e ativa.
    ficha = inspect.getsource(admin.ficha_do_terceiro)
    assert 'revistas = sum(1 for p in publicadas if p.situacao != "ativa")' in ficha
    assert 'p.situacao == "ativa" and p.nome_original in agora and agora[p.nome_original].mudou' in ficha


def _quadro_da_quarentena(*, versao_certa: bool = True, **motivo: bool) -> str:
    """O quadro da quarentena como a pagina do sistema mostra, para um motivo (ou nenhum) e a versao conferida."""
    motivos = {"quarentena_sem_resposta": False, "quarentena_mudou": False, "quarentena_versao": False}
    return _renderizar(
        "admin/sistema.html",
        '<p id="titulo-quarentena">',
        '<ol class="passos">',
        terceiro=motivos | motivo,
        versao_certa=versao_certa,
    )


def test_o_roteiro_traz_o_quadro_da_quarentena_de_cada_motivo() -> None:
    """O quadro da quarentena, como a pagina do sistema monta para cada motivo (G6a, pedidos 4c e 4d): o roteiro traz
    a frase de cada um, a frase comum e a que soma quando a versao ja foi conferida de novo."""
    quarentena = _secao(TERCEIROS, "## Quarentena: o que é e como sair")
    fixo = "Este servidor está em quarentena."
    comum = (
        "O que estava esperando para sair foi cancelado, e os limites das pessoas com este servidor foram suspensos;"
        " cada pessoa vê isso no próprio extrato."
    )
    nada = "Nada dele chega às pessoas até você revisar de novo."

    def partes(quadro: str) -> tuple[str, bool]:
        """(a frase do motivo, se o quadro repete "nada dele chega"), sem o comeco e o fim, que sao de todos."""
        assert quadro.startswith(fixo)
        assert quadro.endswith(comum)
        meio = quadro.removeprefix(fixo).removesuffix(comum).strip()
        return meio.removesuffix(nada).strip(), meio.endswith(nada)

    mudou, repete_mudou = partes(_quadro_da_quarentena(quarentena_mudou=True))
    sem_resposta, repete_sem_resposta = partes(_quadro_da_quarentena(quarentena_sem_resposta=True))
    versao, repete_versao = partes(_quadro_da_quarentena(quarentena_versao=True, versao_certa=False))
    versao_conferida, _ = partes(_quadro_da_quarentena(quarentena_versao=True))
    outro, repete_outro = partes(_quadro_da_quarentena())
    # Na quarentena por mudanca, a frase "nada dele chega" vem no motivo e nao se repete (G6a, pedido 4d).
    assert "nada dele chega às pessoas até você revisar de novo." in mudou
    assert not repete_mudou
    assert repete_sem_resposta
    assert repete_versao
    assert repete_outro
    assert f'Nos três últimos casos, o quadro segue com "{nada}"' in quarentena
    assert f"> {comum}" in quarentena
    for motivo in (mudou, sem_resposta, versao, outro):
        assert motivo
        assert motivo in quarentena, motivo
    soma = versao_conferida.removeprefix(versao).strip()
    assert soma.startswith("A versão já foi conferida de novo e está certa")
    assert f'"{soma}"' in quarentena


def test_o_roteiro_diz_o_que_a_tela_faz_com_mais_de_500_caixas() -> None:
    """A conclusao tem teto de formulario proprio (G6ar, item 3): as 500 caixas do despachante cabem; com uma a
    mais, a tela recusa o pedido inteiro com a frase propria, na pagina "Pedido recusado"."""
    passo = _secao(TERCEIROS, "## Passo 6. Concluir a revisão do servidor")
    teto = admin.TETO_FORMULARIO_CONCLUSAO
    assert servico.TETO_PUBLICADAS == admin.TETO_PUBLICAR
    assert teto.campos == admin.TETO_PUBLICAR + 2  # o fornecedor e o CSRF
    assert teto.campos_demais == admin.CAIXAS_DEMAIS
    assert teto.tamanho > seguranca.MAXIMO_FORMULARIO
    assert f"Até {admin.TETO_PUBLICAR} ferramentas por conclusão." in passo
    assert f'Se você marcar mais de {admin.TETO_PUBLICAR}, a tela mostra a página "Pedido recusado"' in passo
    assert _plano(admin.CAIXAS_DEMAIS) in passo
    assert "198" not in _ler(TERCEIROS)
    rotas = _plano(inspect.getsource(admin.TelasDoAdmin.rotas))
    assert (
        'rota_com_teto( "/admin/sistemas/{sistema_id:uuid}/revisao_servidor", self.revisar_servidor,'
        " TETO_FORMULARIO_CONCLUSAO )"
    ) in rotas
    assert '"Pedido recusado", erro.mensagem' in inspect.getsource(nucleo.Telas.formulario)


def _tabelas(caminho: Path, titulo: str) -> list[list[str]]:
    """A primeira coluna de cada tabela de uma secao, sem a linha do titulo e a dos tracos, na ordem do documento."""
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    inicio = linhas.index(titulo)
    tabelas: list[list[str]] = []
    dentro = False
    for linha in linhas[inicio + 1 :]:
        if linha.startswith("## "):
            break
        if not linha.startswith("| "):
            dentro = False
            continue
        celula = linha.split(" | ")[0].removeprefix("| ")
        if not dentro:
            tabelas.append([])
            dentro = True
        elif celula != "---":
            tabelas[-1].append(_plano(celula))
    return tabelas


def test_o_quadro_da_conclusao_so_traz_os_motivos_que_a_tela_mostra() -> None:
    """Passo 6: o quadro "A revisao do servidor nao foi concluida" traz cada motivo que o despachante pode dar e que
    chega a ele, e so esses. O fornecedor fora do tamanho e as caixas demais, a tela barra antes (frase propria); o
    despachante mudo ou lento volta como aviso no alto, sem o quadro."""
    titulo = "## Passo 6. Concluir a revisão do servidor"
    passo = _secao(TERCEIROS, titulo)
    quadro, avisos = _tabelas(TERCEIROS, titulo)
    assert quadro == [_plano(paginas.MOTIVOS_REVISAO_SERVIDOR[m]) for m in MOTIVOS_NO_QUADRO_DA_CONCLUSAO]
    assert avisos == [
        _plano(paginas.AVISOS["despachante_mudo_revisao"]),
        _plano(paginas.AVISOS["despachante_demorou_revisao"]),
    ]
    avisos_no_alto = "Se o despachante não responder, esse quadro não aparece: a página do sistema volta com um aviso"
    assert f"{avisos_no_alto} no alto." in passo
    # Os motivos que o despachante pode dar: os do quadro e os que a tela barra antes, nenhum outro.
    fonte = inspect.getsource(servico.Despachante.revisar_servidor)
    motivos = set(re.findall(r'(?:recusar|RevisaoRecusada)\(\s*"(\w+)"', fonte))
    assert motivos == set(MOTIVOS_NO_QUADRO_DA_CONCLUSAO) | set(MOTIVOS_QUE_A_TELA_BARRA_ANTES)
    # A tela barra antes: servidor proprio, fornecedor fora do tamanho do despachante e caixas demais.
    revisar = inspect.getsource(admin.TelasDoAdmin.revisar_servidor)
    chamada = revisar.index("self.t.despachante.post(")
    assert revisar.index('if linha.origem == "proprio":') < chamada
    assert revisar.index('_texto(f, "fornecedor", FORNECEDOR_ROTULO, erros, minimo=minimo, maximo=maximo)') < chamada
    assert revisar.index("conferir_publicar(") < revisar.index("if erros:") < chamada
    assert admin.FORNECEDOR_LETRAS == servico.TETO_FORNECEDOR
    assert admin.TETO_PUBLICAR == servico.TETO_PUBLICADAS
    assert admin.TETO_FORMULARIO_CONCLUSAO.campos == admin.TETO_PUBLICAR + 2
    # O despachante tira os espacos das pontas; a tela ja manda sem eles: a conta das letras e a mesma.
    for bruto in ("  ab  ", "a\x01b", "\x01\x01ab", "a \t b"):
        limpo = admin._limpo(bruto)
        assert limpo == limpo.strip()
    # O que a tela diz com o fornecedor fora do tamanho, como a tela monta, e onde.
    minimo, maximo = admin.FORNECEDOR_LETRAS
    for curto_ou_longo in ("x" * (minimo - 1), "x" * (maximo + 1)):
        erros: list[str] = []
        f = seguranca.Formulario([("fornecedor", curto_ou_longo)])
        admin._texto(f, "fornecedor", admin.FORNECEDOR_ROTULO, erros, minimo=minimo, maximo=maximo)
        assert erros == [f"Preencha quem fornece este servidor com {minimo} a {maximo} letras."]
    assert f'diz, em "Falta corrigir:": > {erros[0]}' in passo
    # Fora do tamanho, nada vai ao despachante: com erros, a pagina do sistema volta antes do POST (etapa 5f).
    assert (
        f"Se o nome do fornecedor ficar com menos de {minimo} letras ou mais de {maximo}, nada vai ao despachante (o"
        ' navegador costuma avisar antes de enviar). A página do sistema volta e diz, em "Falta corrigir:":'
    ) in passo
    volta = "if erros:\n            return await self._pagina_sistema(pedido, linha, erros=erros, valores=valores)\n"
    assert volta in revisar
    assert revisar.index(volta) < chamada
    assert "<strong>Falta corrigir:</strong>" in _tela("base.html")
    campo = 'name="fornecedor" required minlength="{}" maxlength="{}"'.format(*admin.FORNECEDOR_LETRAS)
    assert campo in _tela("admin/sistema.html")
    # As frases do despachante que nunca chegam ao quadro nao estao no roteiro como linha da tabela.
    roteiro = TERCEIROS.read_text(encoding="utf-8")
    for motivo in MOTIVOS_QUE_A_TELA_BARRA_ANTES:
        assert f"| {paginas.MOTIVOS_REVISAO_SERVIDOR[motivo]} |" not in roteiro, motivo


def test_o_roteiro_traz_o_aviso_da_revisao_de_ferramenta_de_terceiro() -> None:
    passo = _secao(TERCEIROS, "## Passo 5. Revisar cada ferramenta")
    assert _plano(paginas.AVISOS["ferramenta_revisada_terceiro"]) in passo
    # O aviso de sistema proprio ("ja pode entrar em modelos de ordem") nao vale para terceiro.
    assert _plano(paginas.AVISOS["ferramenta_revisada"]) not in _ler(TERCEIROS)
    revisar = inspect.getsource(admin.TelasDoAdmin.revisar)
    assert 'aviso = "ferramenta_revisada_terceiro" if terceiro else "ferramenta_revisada"' in revisar
    # O nome sugerido de um terceiro nao leva o nome declarado (texto do fornecedor).
    assert admin.nome_vitrine_sugerido("Mural", "ler_mural", terceiro=True) == "Mural_"
    assert admin.nome_vitrine_sugerido("Mural", "ler_mural", terceiro=False) == "Mural_ler_mural"
    assert "a página sugere só o começo (o nome do sistema e `_`)" in passo


def test_o_roteiro_traz_a_recusa_do_nome_de_ferramenta_longo_demais() -> None:
    passo = _secao(TERCEIROS, "## Passo 5. Revisar cada ferramenta")
    assert admin.TETO_NOME_FERRAMENTA == 128
    assert _plano(admin.NOME_LONGO_DEMAIS) in passo
    assert "com nome de mais de 128 letras também não pode ser revisada" in passo


@pytest.mark.parametrize("ambiente", ["desenvolvimento", "teste", "piloto", "producao"])
def test_a_troca_de_chave_de_terceiro_confere_o_endereco_em_todo_ambiente(ambiente: str) -> None:
    """A frase do modelo de ameacas ("Fora dos testes automaticos ... a troca so vai a endereco https://") pelo
    caminho da troca: sem ``liberar_locais``, um token_endpoint local e recusado antes de abrir conexao em todo
    ambiente (em desenvolvimento e teste, pelo IP; fora deles, ja pelo http)."""
    config = autorizacao.ConfigAutorizacao.de_json(
        {
            "authorization_endpoint": "http://127.0.0.1:9/a",
            "token_endpoint": "http://127.0.0.1:9/token",
            "client_id": "x",
        }
    )
    with pytest.raises(autorizacao.TrocaRecusada) as erro:
        asyncio.run(autorizacao.trocar(config, "s", {}, terceiro=True, ambiente=ambiente))
    assert erro.value.codigo == autorizacao.ENDERECO_RECUSADO


def test_o_roteiro_diz_como_escolher_os_eventos_do_webhook() -> None:
    acompanhar = _secao(TERCEIROS, "## Acompanhar o servidor")
    assert 'para escolher em "Avisos por webhook"' not in acompanhar
    assert 'no campo "Eventos (separados por vírgula)"' in acompanhar
    exemplo = "sistema_em_quarentena,sistema_em_quarentena_sem_resposta"
    assert f"`{exemplo}`" in acompanhar
    assert all(admin.NOME_EVENTO.match(e) for e in exemplo.split(","))
    assert "Deixe vazio para receber todos os avisos." in _tela("admin/webhooks.html")
    assert "Deixe o campo vazio para receber todos os avisos." in acompanhar


def _notas_das_ordens(
    *, teste: bool, terceiros_ligados: bool, origem: str = "terceiro", etiquetas: bool = False
) -> str:
    """As notas de "Minhas ordens de servico" com uma ferramenta de ``origem`` na lista, como o modelo monta."""
    return _renderizar(
        "ordens.html",
        "{% set ha = namespace(terceiros=false) %}",
        "<h2>Valendo agora</h2>",
        etiquetas=etiquetas,
        ativas=[{"ferramentas": [{"origem": origem}]}],
        modelos=[],
        sessao={"pessoa": {"teste": teste}},
        terceiros_ligados=terceiros_ligados,
    )


def test_o_roteiro_diz_quem_le_a_nota_das_ordens_como_o_modelo_mostra() -> None:
    """Passo 8: a nota "so pessoas de teste as usam" aparece so para a pessoa real, com a chave da Fase 3 desligada
    (G6br, item 2). O roteiro diz isso inteiro, e nao que toda pessoa a le."""
    passo = _secao(TERCEIROS, "## Passo 8. Testar com uma pessoa de teste")
    nota = "Só pessoas de teste as usam."
    assert nota in _notas_das_ordens(teste=False, terceiros_ligados=False)
    assert nota not in _notas_das_ordens(teste=True, terceiros_ligados=False)
    assert nota not in _notas_das_ordens(teste=False, terceiros_ligados=True)
    assert "de fora da organização" in _notas_das_ordens(teste=True, terceiros_ligados=False)
    assert (
        'Com a Fase 3 desligada, uma pessoa real (que não é de teste) não usa o servidor. Em "Minhas ordens de'
        ' serviço", ela lê também: > Enquanto os servidores de terceiros estiverem desligados nesta instalação, as'
        " ferramentas marcadas “de terceiros” não funcionam para você: o aplicativo não as vê. Só pessoas de teste"
        " as usam."
    ) in passo
    # A primeira nota: toda pessoa que ve uma ferramenta de terceiros a le, de teste ou nao, com a chave da Fase 3
    # ligada ou nao (etapa 5f); sem ferramenta de terceiros na pagina, ela nao aparece.
    primeira = (
        "Uma ferramenta marcada “de terceiros” é de um servidor de fora da organização. O MCP-CLARO não controla o que"
        " ele faz do lado de lá, e o que ele responde chega ao aplicativo marcado como texto de estranhos."
    )
    for teste in (True, False):
        for ligados in (True, False):
            assert _notas_das_ordens(teste=teste, terceiros_ligados=ligados).startswith(primeira), (teste, ligados)
            html = _notas_das_ordens(teste=teste, terceiros_ligados=ligados, etiquetas=True)
            assert html.startswith(f'<p class="dica" id="dica-terceiros">{primeira}</p>'), (teste, ligados)
            assert _notas_das_ordens(teste=teste, terceiros_ligados=ligados, origem="proprio") == ""
    assert f'Em "Minhas ordens de serviço", toda pessoa que vê uma ferramenta de terceiros lê: > {primeira}' in passo
    assert '"terceiros_ligados": bool(self.t.p.FEATURE_PHASE3_THIRD_PARTY)' in inspect.getsource(
        rotas.TelasDaPessoa._pagina_ordens
    )


def test_o_simulado_traz_a_tela_da_acao_com_o_pedido_barrado() -> None:
    passo = _secao(SIMULADO, "### 10. Servidor de terceiros que pede senha")
    for categoria, palavras in ((cliente.PEDIDO_FORMULARIO, "dados por formulário"), (cliente.PEDIDO_LINK, None)):
        detalhe, explicacao = detalhe_do_desfecho(
            f"o sistema pediu {ponto.pedidos_legiveis((categoria,))} no meio da acao"
        )
        assert explicacao
        assert _plano(explicacao) in passo
        if palavras:
            assert f'"Detalhe: {detalhe}."' in passo
            assert palavras in detalhe
        else:
            assert f'"{detalhe.removeprefix("o sistema pediu ").removesuffix(" no meio da ação")}"' in passo
    # Pedido pelo fio antigo seguido de resposta: a acao sai e o desfecho leva o rabo do pedido barrado.
    assert "pede_pelo_fio_antigo" in mal_comportado.MODOS
    antigo = ponto.pedidos_legiveis((cliente.PREFIXO_FIO_ANTIGO + "elicitation/create",))
    detalhe, explicacao = detalhe_do_desfecho(f"o sistema respondeu; antes pediu {antigo} e o MCP-CLARO recusou")
    assert f'"Detalhe: {detalhe}."' in passo
    assert _plano(explicacao) in passo
    assert "`pede_pelo_fio_antigo`" in passo


def _operacao_da_ficha(*, risco: str, passkey: bool) -> Any:
    """So o que ``como_passou`` le de uma operacao."""
    return type(
        "Op",
        (),
        {
            "limite_id": None,
            "ficha_sinais": {"janela_minutos": 240, "vistos": []},
            "ficha_exige_passkey": passkey,
            "ficha_risco": risco,
        },
    )()


def test_o_simulado_traz_o_alerta_dos_tres_sinais_como_as_telas_mostram() -> None:
    passo = _secao(SIMULADO, "### 14. Texto de fora seguido de uma ação (alerta dos três sinais)")
    janela = politica.POLITICA["THREE_SIGNALS_WINDOW_MINUTES"]
    assert f"(`THREE_SIGNALS_WINDOW_MINUTES`, {janela.inicial} minutos de início)" in passo
    assert "instrucao_na_resposta" in mal_comportado.MODOS
    assert "`instrucao_na_resposta`" in passo
    # O periodo de experiencia vem antes do alerta na rota: sem passar dele, o caminho contado e o da experiencia.
    experiencia = politica.POLITICA["TRIAL_PERIOD_DAYS"]
    assert f"(`TRIAL_PERIOD_DAYS`, {experiencia.inicial} dias de" in passo
    rota_fonte = inspect.getsource(pedidos.Pedidos._rota)
    assert rota_fonte.index("fila.em_experiencia(") < rota_fonte.index('motivo="tres_sinais"')
    assert "o caminho contado é o da experiência" in passo
    # "Como passou", como a tela da acao escreve para cada ficha com o retrato do alerta. So a ficha de risco alto
    # pede passkey (com FEATURE_PASSKEY); a do alerta, nunca.
    alerta = como_passou(_operacao_da_ficha(risco="baixo", passkey=False))
    alto = como_passou(_operacao_da_ficha(risco="alto", passkey=False))
    alto_com_passkey = como_passou(_operacao_da_ficha(risco="alto", passkey=True))
    assert len({alerta, alto, alto_com_passkey}) == 3
    assert "passkey" in alto_com_passkey
    assert "passkey" not in alto
    assert f'em "Como passou": "{alerta}".' in passo
    assert f'a tela diz "{alto}", ou "{alto_com_passkey}" quando a ficha pede passkey.' in passo
    assert 'Rota(exige_passkey=self.p.FEATURE_PASSKEY, motivo="risco_alto", sinais=retrato)' in rota_fonte
    # "Meus limites": a frase do modelo, com a hora e o aplicativo no lugar das variaveis.
    linha = next(
        x for x in (MODELOS / "acoes" / "limites.html").read_text(encoding="utf-8").splitlines() if "três sinais" in x
    )
    frase = re.sub(r"<[^>]+>", "", linha.replace("{{ x.ate }}", "HH:MM"))
    frase = _plano(re.sub(r"\{\{ x\.aplicativo \| escondidos \}\}", "(o aplicativo)", frase))
    assert frase == "Enquanto durar o alerta dos três sinais (até HH:MM), seus limites com (o aplicativo) não valem."
    assert f'"{frase}"' in passo
    # O caminho no Grafana e as linhas do extrato.
    rota = inspect.getsource(pedidos.Pedidos._rota)
    assert 'Rota(motivo="tres_sinais", sinais=retrato)' in rota
    assert 'CONTADORES.somar("acoes_pedidas", rota=rota.motivo)' in inspect.getsource(pedidos)
    assert "caminho `tres_sinais`" in passo
    assert "tres_sinais" in FRASES
    assert "extrato com `acao_pedida` e `tres_sinais`" in passo
    # O aplicativo nao fica sabendo do alerta (D24): a resposta nao tem a rota.
    assert "sinais" not in inspect.getsource(pedidos.resposta)


def test_a_mensagem_da_partida_e_os_documentos_dizem_o_mesmo_sobre_a_sala_fora() -> None:
    """Item 3 do retoque: o coordenador corrigiu a mensagem da partida e a docstring do cliente; os documentos dizem
    como o codigo faz (B8): sem a sala, nenhum pedido MCP vai ao terceiro; a troca e a renovacao da chave saem do
    despachante."""
    mensagem = next(e for e in Parametros(APP_ENV="piloto")._problemas_da_fase3() if "VISITS_INTERNAL_URL" in e)
    assert (
        "nenhum pedido MCP vai a servidor de terceiros; so a troca e a renovacao da chave (OAuth) saem do despachante"
        in (mensagem)
    )
    assert "nunca fala direto" not in mensagem
    linha = next(x for x in PLANO.read_text(encoding="utf-8").splitlines() if x.startswith("| `VISITS_INTERNAL_URL` |"))
    assert (
        "Sem a sala, nenhum pedido MCP vai a servidor de terceiros: o despachante não fala MCP direto com eles (só os"
        " testes automáticos põem essa ligação, de propósito)."
    ) in linha
    assert (
        "A troca e a renovação da chave vão do despachante ao endereço de troca de chave de cada terceiro, com ou sem"
        " a sala |"
    ) in linha
    doc = _plano(cliente.__doc__ or "")
    assert "nao responde sozinho" not in doc
    assert "o kit montaria sozinho uma resposta de erro a um pedido pelo fio antigo" in doc
    assert "o kit ainda montaria sozinho uma resposta de erro a um pedido" in _ler(PLANO)


def test_o_plano_poe_a_troca_de_chave_no_despachante_e_nao_na_ligacao() -> None:
    linha = next(x for x in PLANO.read_text(encoding="utf-8").splitlines() if x.startswith("| `ligacao` |"))
    assert "Troca o código de autorização e renova chaves" not in linha
    assert "roda dentro da sala de visitas |" not in linha
    assert "Num servidor de terceiros, a conversa MCP roda dentro da sala de visitas." in linha
    assert "ficam no despachante (`despachante/autorizacao.py` e `despachante/renovacao.py`)" in linha
    # O codigo: nada da troca de chave em ligacao/ nem em visitas/; ela fica no despachante.
    for pasta in ("ligacao", "visitas"):
        for fonte in (FONTES / pasta).rglob("*.py"):
            texto = fonte.read_text(encoding="utf-8")
            assert "token_endpoint" not in texto, fonte
            assert "refresh_token" not in texto, fonte
    assert "config.token_endpoint" in inspect.getsource(autorizacao.trocar)
    assert "autorizacao.trocar(" in (FONTES / "despachante" / "renovacao.py").read_text(encoding="utf-8")


def test_o_plano_so_poe_o_quadro_de_pedidos_recusados_num_terceiro() -> None:
    plano = _ler(PLANO)
    assert "que vai ao extrato e à tela do ADMIN" not in plano
    assert (
        'que vai ao extrato e, num servidor de terceiros, também ao quadro "Pedidos recusados deste sistema" na tela'
        " do ADMIN"
    ) in plano
    # A linha da migracao 0006: o indice e de todos os sistemas; o quadro, so de um terceiro.
    assert "de cada sistema, que a tela do ADMIN mostra." not in plano
    assert (
        "O extrato ganha um índice para as linhas de pedido recusado de cada sistema; num servidor de terceiros, a"
        ' tela do ADMIN as mostra no quadro "Pedidos recusados deste sistema".'
    ) in plano
    indice = MIGRACAO_0006.read_text(encoding="utf-8")
    assert "CREATE INDEX extrato_pedido_barrado_idx ON extrato (sistema_id, criado_em DESC)" in indice
    # O codigo: so a ficha de um terceiro le os pedidos recusados, e a secao fica dentro do bloco do terceiro.
    assert 'await ficha_do_terceiro(c, linha) if linha.origem != "proprio" else None' in inspect.getsource(
        admin.TelasDoAdmin._pagina_sistema
    )
    tela = (MODELOS / "admin" / "sistema.html").read_text(encoding="utf-8")
    bloco = tela.split('{% if terceiro %}\n<section class="cartao" aria-labelledby="titulo-revisao-servidor">')[1]
    assert "Pedidos recusados deste sistema" in bloco
    assert bloco.rstrip().endswith("{% endif %}\n{% endblock %}")
    assert bloco.count("{% if ") == bloco.count("{% endif %}") - 1


# Os modulos que so levam ``liberar_locais`` da partida do despachante ate a troca de chave (etapa 5f, retoque): cada um
# recebe o valor e o passa adiante como veio, sem decidir nada com ele. Quem decide e ``conferir_endereco_terceiro``,
# conferido rodando.
_LEVAM_LIBERAR_LOCAIS = (
    FONTES / "despachante" / "app.py",
    FONTES / "despachante" / "servico.py",
    FONTES / "despachante" / "autorizacao.py",
    FONTES / "despachante" / "renovacao.py",
    FONTES / "saida" / "despachante.py",
)
_LIBERAR_GUARDADO = frozenset({"liberar_locais", "_liberar_locais"})


def _e_o_liberar_recebido(no: ast.AST) -> bool:
    """``liberar_locais`` (o parametro) ou ``self.liberar_locais`` / ``self._liberar_locais`` (o guardado dele)."""
    if isinstance(no, ast.Name):
        return no.id == "liberar_locais"
    return (
        isinstance(no, ast.Attribute)
        and no.attr in _LIBERAR_GUARDADO
        and isinstance(no.value, ast.Name)
        and no.value.id == "self"
    )


def _parametros_liberar(funcao: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int | None, ast.expr | None] | None:
    """Se a ``funcao`` recebe ``liberar_locais``: a posicao dele entre os posicionais (sem ``self``; ``None`` se so
    por nome) e o padrao (``None`` se nao tem)."""
    a = funcao.args
    posicionais = [*a.posonlyargs, *a.args]
    padroes = [None] * (len(posicionais) - len(a.defaults)) + list(a.defaults)
    nomes = [x.arg for x in posicionais]
    if "liberar_locais" in nomes:
        i = nomes.index("liberar_locais")
        return i - (nomes[:1] == ["self"]), padroes[i]
    nomes = [x.arg for x in a.kwonlyargs]
    if "liberar_locais" in nomes:
        return None, a.kw_defaults[nomes.index("liberar_locais")]
    return None


def _liberar_locais_mexido(fonte: str) -> list[str]:
    """Onde a ``fonte`` faz com ``liberar_locais`` outra coisa que receber (padrao ``False``), guardar como veio e
    passar adiante como veio: um valor proprio, uma conta (``liberar_locais or ...``), um parametro que nao segue."""
    arvore = ast.parse(fonte)
    pais = {filho: pai for pai in ast.walk(arvore) for filho in ast.iter_child_nodes(pai)}
    funcoes = [no for no in ast.walk(arvore) if isinstance(no, ast.FunctionDef | ast.AsyncFunctionDef)]
    posicao = {f.name: dados[0] for f in funcoes if (dados := _parametros_liberar(f)) and dados[0] is not None}
    erros: list[str] = []
    for funcao in funcoes:
        dados = _parametros_liberar(funcao)
        if dados is None:
            continue
        padrao = dados[1]
        if padrao is not None and not (isinstance(padrao, ast.Constant) and padrao.value is False):
            erros.append(f"{funcao.name}:{funcao.lineno}: padrao {ast.unparse(padrao)}")
        if not any(
            isinstance(x, ast.Name) and x.id == "liberar_locais" and isinstance(x.ctx, ast.Load)
            for x in ast.walk(funcao)
        ):
            erros.append(f"{funcao.name}:{funcao.lineno}: recebe liberar_locais e nao passa adiante")
    for no in ast.walk(arvore):
        linha = getattr(no, "lineno", 0)
        if isinstance(no, ast.Call):
            chamada = no.func.id if isinstance(no.func, ast.Name) else getattr(no.func, "attr", None)
            i = posicao.get(str(chamada))
            if i is not None and len(no.args) > i and not _e_o_liberar_recebido(no.args[i]):
                erros.append(f"{linha}: {ast.unparse(no)}")
        elif isinstance(no, ast.keyword) and no.arg == "liberar_locais":
            if not _e_o_liberar_recebido(no.value):
                erros.append(f"{linha}: liberar_locais={ast.unparse(no.value)}")
        elif isinstance(no, ast.Attribute) and no.attr in _LIBERAR_GUARDADO and not _e_o_liberar_recebido(no):
            erros.append(f"{linha}: {ast.unparse(pais[no])}")  # o de outro objeto, lido ou trocado de fora
        elif isinstance(no, ast.Constant) and no.value in _LIBERAR_GUARDADO:
            erros.append(f"{linha}: {ast.unparse(pais[no])}")  # pelo nome em texto: setattr, getattr, dicionario
        elif _e_o_liberar_recebido(no):
            assert isinstance(no, ast.Name | ast.Attribute)
            pai = pais[no]
            if isinstance(no.ctx, ast.Load):
                passa = (isinstance(pai, ast.keyword) and pai.arg == "liberar_locais") or (
                    isinstance(pai, ast.Call) and no in pai.args
                )
                guarda = (
                    isinstance(no, ast.Name)
                    and isinstance(pai, ast.Assign)
                    and pai.value is no
                    and all(isinstance(alvo, ast.Attribute) and _e_o_liberar_recebido(alvo) for alvo in pai.targets)
                )
                if not (passa or guarda):
                    erros.append(f"{linha}: {ast.unparse(pai)}")
            elif not (
                isinstance(no, ast.Attribute)
                and isinstance(pai, ast.Assign)
                and pai.targets == [no]
                and isinstance(pai.value, ast.Name)
                and pai.value.id == "liberar_locais"
            ):
                erros.append(f"{linha}: {ast.unparse(pai)}")
    return erros


def test_quem_so_leva_liberar_locais_pega_o_que_ja_foi_mexido() -> None:
    """A conferencia de ``_liberar_locais_mexido`` pega as formas de ligar o ``liberar_locais`` por outro caminho que
    um ``= True`` literal (o verificador da etapa 5f achou a primeira viva no corpo de ``criar_app_despachante``)."""
    certo = (
        "def criar(p, *, liberar_locais: bool = False):\n"
        "    ambiente = p\n"
        "    d = Despachante(ambiente=ambiente, liberar_locais=liberar_locais)\n"
        "    return rotas(p, liberar_locais)\n"
        "def rotas(p, liberar_locais: bool = False):\n"
        "    return conferir(p, liberar_locais=liberar_locais)\n"
        "class Despachante:\n"
        "    def __init__(self, *, ambiente: str = 'x', liberar_locais: bool = False) -> None:\n"
        "        self.liberar_locais = liberar_locais\n"
        "    def ler(self):\n"
        "        return chave(liberar_locais=self.liberar_locais)\n"
    )
    assert _liberar_locais_mexido(certo) == []
    for de, para in (
        ("ambiente, liberar_locais=liberar_locais)", 'ambiente, liberar_locais=liberar_locais or ambiente == "x")'),
        ("ambiente, liberar_locais=liberar_locais)", "ambiente, liberar_locais=True)"),
        ("ambiente, liberar_locais=liberar_locais)", "ambiente, liberar_locais=bool(p))"),
        ("    ambiente = p\n", "    ambiente = p\n    liberar_locais = ambiente == 'desenvolvimento'\n"),
        ("    ambiente = p\n", "    ambiente = p\n    liberar_locais |= True\n"),
        ("    ambiente = p\n", "    ambiente = p\n    if (liberar_locais := True):\n        pass\n"),
        ("    return rotas(p, liberar_locais)", "    return rotas(p, liberar_locais or True)"),
        ("    return rotas(p, liberar_locais)", "    return rotas(p, True)"),
        ("    return rotas(p, liberar_locais)", "    d.liberar_locais = True\n    return rotas(p, liberar_locais)"),
        (
            "    return rotas(p, liberar_locais)",
            "    setattr(d, 'liberar_locais', True)\n    return rotas(p, liberar_locais)",
        ),
        ("conferir(p, liberar_locais=liberar_locais)", "conferir(p, liberar_locais=True)"),
        ("conferir(p, liberar_locais=liberar_locais)", "conferir(p)"),
        ("def rotas(p, liberar_locais: bool = False)", "def rotas(p, liberar_locais: bool = True)"),
        ("*, liberar_locais: bool = False):\n    ambiente", "*, liberar_locais: bool = True):\n    ambiente"),
        ("self.liberar_locais = liberar_locais", "self.liberar_locais = liberar_locais or self.x"),
        ("self.liberar_locais = liberar_locais", "self.liberar_locais = True"),
        ("chave(liberar_locais=self.liberar_locais)", "chave(liberar_locais=not self.liberar_locais)"),
        ("        return chave(", "        self.liberar_locais = True\n        return chave("),
        ("    return rotas(p, liberar_locais)", "    return rotas(p, **{'liberar_locais': True})"),
    ):
        assert certo.count(de) == 1, de
        assert _liberar_locais_mexido(certo.replace(de, para)), para


def test_o_modelo_de_ameacas_diz_o_que_segura_a_janela_de_dns_na_troca_de_chave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ameacas = _ler(AMEACAS)
    assert (
        "Na troca de chave (OAuth) de um terceiro, que sai do despachante pela rede `saida`, não há muro: só o https"
        " com certificado público segura essa janela."
    ) in ameacas
    assert "a troca só vai a endereço `https://`" in ameacas
    # O codigo: o despachante nao esta atras do muro da sala; a troca confere o certificado; a configuracao so
    # aceita http num endereco local, que a conferencia de terceiro recusa fora dos testes (liberar_locais).
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "network_mode" not in compose["services"]["despachante"]
    assert "verify=ssl.create_default_context()" in inspect.getsource(autorizacao.http_para_autorizacao)
    assert 'partes.scheme == "http" and partes.hostname and _loopback(partes.hostname)' in inspect.getsource(
        autorizacao.conferir_endereco
    )
    assert "liberar_locais: bool = False" in inspect.getsource(autorizacao.trocar)
    # "Fora dos testes automaticos": so os testes ligam ``liberar_locais``. O despachante de verdade (o de
    # ``python -m mcp_claro despachante``) sobe sem ele, e todo caminho ate a troca de chave o tem desligado de
    # inicio (etapa 5f). Sem ele, o endereco local de teste e recusado ate em desenvolvimento, e fora de
    # desenvolvimento e teste, nem com ele o http passa.
    assert (
        "Fora dos testes automáticos, que usam um servidor de teste local, a troca só vai a endereço `https://`."
        in ameacas
    )
    assert inspect.signature(despachante_app.criar_app_despachante).parameters["liberar_locais"].default is False
    assert "liberar_locais: bool = False" in inspect.getsource(despachante_app.criar_app_despachante)
    principal = (FONTES / "__main__.py").read_text(encoding="utf-8")
    assert "criar_app_despachante(p, criar_engine(p))" in principal
    assert "liberar_locais" not in principal
    for caminho in (autorizacao.trocar, renovacao.chave_pronta, servico.Despachante, porta.PortaDeSaida):
        assert inspect.signature(caminho).parameters["liberar_locais"].default is False, caminho
    for fonte in FONTES.rglob("*.py"):
        assert not re.search(r"liberar_locais(?:: bool)? ?= ?True", fonte.read_text(encoding="utf-8")), fonte
    # Nem por outro caminho: do ``criar_app_despachante`` ate a troca, cada modulo so leva o valor que recebeu, sem
    # conta nem valor proprio (``liberar_locais or ambiente == "desenvolvimento"`` deixaria o despachante rodado a mao
    # em desenvolvimento mandar a troca por http a 127.0.0.1).
    for modulo in _LEVAM_LIBERAR_LOCAIS:
        assert _liberar_locais_mexido(modulo.read_text(encoding="utf-8")) == [], modulo
    # E rodando: o despachante montado como a partida o monta, em desenvolvimento e em teste, da ``False`` ao
    # despachante, a porta de saida e as rotas da autorizacao.
    engine = create_async_engine("postgresql+asyncpg://ninguem@127.0.0.1:1/nada")
    rotas_dadas: list[object] = []
    montar_rotas = despachante_app.rotas_autorizacao

    def rotas_conferidas(*args: Any, **kwargs: Any) -> Any:
        rotas_dadas.append(kwargs.get("liberar_locais"))
        return montar_rotas(*args, **kwargs)

    monkeypatch.setattr(despachante_app, "rotas_autorizacao", rotas_conferidas)
    for ambiente in ("desenvolvimento", "teste"):
        montado = despachante_app.criar_app_despachante(Parametros(APP_ENV=ambiente), engine, CifraFalsa())
        assert montado.state.despachante.ambiente == ambiente
        assert montado.state.despachante.liberar_locais is False, ambiente
        assert montado.state.porta.liberar_locais is False, ambiente
    assert rotas_dadas == [False, False]
    for ambiente, liberar in (("desenvolvimento", False), ("teste", False), ("piloto", True), ("producao", True)):
        with pytest.raises(cliente.EnderecoRecusado):
            asyncio.run(
                cliente.conferir_endereco_terceiro("http://127.0.0.1:9/token", ambiente, liberar_locais=liberar)
            )


def test_os_segredos_dizem_que_a_troca_de_chave_fica_no_despachante() -> None:
    texto = _ler(SEGREDOS)
    assert (
        "Na troca do código e na renovação (OAuth), o despachante manda o código, a chave de renovação e o segredo do"
        " cliente direto ao endereço de troca de chave do terceiro, sem passar pela sala, e só depois de conferir que"
        " o endereço é `https://` e da internet |"
    ) in texto
    trocar = inspect.getsource(autorizacao.trocar)
    conferir = trocar.index(
        "    if terceiro:\n        await _conferir_troca_de_terceiro(config.token_endpoint, ambiente, liberar_locais)"
    )
    assert conferir < trocar.index('http.stream("POST", config.token_endpoint')
    assert "A sala de visitas fala MCP com os servidores de terceiros" in texto
    assert "**Não recebe o que é da troca de chave:**" in texto
    # O contrato da sala nao tem campo para isso: so a chave daquela chamada.
    campos = set(sala_contrato.PedidoChamar.model_fields)
    assert not campos & {"segredo_cliente", "refresh_token", "renovacao", "token_endpoint"}


def test_o_readme_cita_a_sala_e_o_roteiro() -> None:
    readme = _ler(README)
    assert "Uma imagem, cinco papéis" in readme
    assert "| `visitas` | Sala de visitas (Fase 3)" in readme
    assert "[Ligar um servidor de terceiros](docs/terceiros.md)" in readme
    assert "`FEATURE_PHASE3_THIRD_PARTY`" in readme
    assert "`VISITS_INTERNAL_URL`" in readme
    assert {str(p) for p in Papel} == {"entrada", "despachante", "rotinas", "visitas", "vigia"}


def _leitura_com_a_sala_fora(nome: str) -> str:
    """O texto que o aplicativo recebe numa leitura de terceiro com a sala fora, como o despachante monta."""
    resultado = servico._falha(Resposta("nao_enviado", excecao=sala_contrato.SALA_FORA), _terceiro(nome))
    texto = resultado["content"][0]["text"]
    assert isinstance(texto, str)
    return texto


def test_o_simulado_traz_as_explicacoes_da_tela_da_acao() -> None:
    simulado = _ler(SIMULADO)
    _, quarentena = detalhe_do_desfecho(MOTIVO_PARADA)
    # O desfecho que a porta de saida grava quando a sala esta fora, e a explicacao que a tela da acao da a ele.
    _, sala_fora = detalhe_do_desfecho(porta._NAO_ENVIADO[sala_contrato.SALA_FORA])
    assert quarentena
    assert _plano(quarentena) in simulado
    assert sala_fora
    assert _plano(sala_fora) in simulado
    # O que o aplicativo recebe na leitura (sem acento, como vai ao aplicativo) e o assunto dos avisos ao ADMIN.
    assert _plano(servico.MENSAGEM_CHAVE_INDISPONIVEL) in simulado
    leitura = _leitura_com_a_sala_fora("SERVIDOR-X")
    antes, nome, depois = leitura.partition(" SERVIDOR-X")
    assert nome
    assert depois == "."
    assert f'"{antes}" e o nome do servidor' in simulado
    assert AVISOS_ADMIN["sistema_em_quarentena"].assunto in simulado
    assert AVISOS_ADMIN["sistema_em_quarentena_sem_resposta"].assunto in simulado
    for frase in (
        admin.DECLARACOES_FALHA["sala_fora"][1],
        paginas.INALCANCAVEL_POR_MOTIVO[sala_contrato.MOTIVO_SALA_FORA],
    ):
        assert _plano(frase) in simulado
    assert _plano(paginas.MOTIVOS_REVISAO_SERVIDOR["chave_indisponivel"]) in simulado


def test_o_roteiro_e_o_simulado_citam_os_eventos_e_os_modos_que_existem() -> None:
    roteiro = _ler(TERCEIROS)
    for evento in AVISOS_ADMIN:
        assert f"`{evento}`" in roteiro
    simulado = _ler(SIMULADO)
    for modo in ("muda_so_texto", "pede_dados", "pede_link", "mudo", "erro500"):
        assert modo in mal_comportado.MODOS
        assert f"`{modo}`" in simulado
    assert "senha_no_texto" in terceiro_falante.MODOS
    assert "`senha_no_texto`" in simulado
    assert "exemplos.mal_comportado --porta 9104" in simulado
    assert "exemplos.terceiro_falante --porta 9105" in simulado


def _updates_de_sistemas() -> list[str]:
    """Cada ``UPDATE sistemas`` do codigo, com o resto do comando (ate 600 letras depois)."""
    trechos = []
    for fonte in FONTES.rglob("*.py"):
        texto = fonte.read_text(encoding="utf-8")
        trechos += [texto[m.start() : m.start() + 600] for m in re.finditer(r"UPDATE sistemas SET", texto)]
    return trechos


def test_o_roteiro_diz_quando_a_revisao_deixa_de_valer_sem_a_quarentena() -> None:
    acompanhar = _secao(TERCEIROS, "## Acompanhar o servidor")
    # O exemplo antigo nao acontece: um terceiro ativo e revisado que falha a versao vai a quarentena (D29), e em
    # quarentena o "aceito" nao muda a situacao.
    assert "conferência da versão recusada e de outra aceita" not in _ler(TERCEIROS)
    assert 'Se o servidor saiu de "ativo" sem passar pela quarentena e voltou, a revisão anterior deixa de valer.' in (
        acompanhar
    )
    assert "Só acontece quando a equipe que opera o gateway muda o cadastro dele direto no banco" in acompanhar
    assert "- deixou de falar a versão 2026-07-28 numa nova conferência da versão;" in _secao(
        TERCEIROS, "## Quarentena: o que é e como sair"
    )
    sondar = inspect.getsource(servico.Despachante.sondar_sistema)
    assert "motivo=MOTIVO_QUARENTENA_VERSAO" in sondar
    assert 'agora.situacao == "ativo" and agora.revisao_servidor_id is not None' in sondar
    assert "situacao = CASE WHEN situacao IN ('pendente', 'recusado') THEN 'ativo' ELSE situacao END" in sondar
    # Nenhum codigo (tela, despachante, rotinas) muda o tipo de chave nem desativa ou poe pendente um sistema: so a
    # equipe que opera o gateway, direto no banco. Se vier uma tela para isso, o roteiro precisa dizer.
    trechos = _updates_de_sistemas()
    assert trechos
    for trecho in trechos:
        comando = trecho.split('"),')[0]
        assert "tipo_chave" not in comando
        assert "situacao = 'desativado'" not in comando
        assert "situacao = 'pendente'" not in comando


def test_o_roteiro_nao_promete_tela_da_acao_numa_leitura() -> None:
    passo = _secao(TERCEIROS, "## Passo 8. Testar com uma pessoa de teste")
    assert "No MCP-CLARO, a leitura não tem tela própria: no extrato, ela vê só a linha da leitura" in passo
    assert "e usa uma leitura. 3. Na tela da ação" not in passo
    resposta = "O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo"
    assert passo.index("Depois que a ação sai, na tela da ação") < passo.index(resposta)
    # So o pedido de uma acao cria operacao (e a tela da acao); a leitura vai ao extrato so com o nome da ferramenta.
    com_operacao = [f for f in FONTES.rglob("*.py") if "INSERT INTO operacoes" in f.read_text(encoding="utf-8")]
    assert [f.relative_to(FONTES).as_posix() for f in com_operacao] == ["saida/fila.py"]
    assert FRASES["leitura_feita"].campos() == {"ferramenta"}
    assert "{% if a.de_estranhos and a.respondeu %}" in (MODELOS / "acoes" / "_estado.html").read_text(encoding="utf-8")


def test_mais_ferramentas_que_o_teto_da_sala_contam_como_sem_resposta() -> None:
    teto = sala_contrato.TETO_FERRAMENTAS
    reserva = sala_contrato.RespostaListar(tipo="duvida", excecao=sala_contrato.RESPOSTA_FORA_DO_CONTRATO)

    def lista(n: int) -> bytes:
        return sala_contrato._no_contrato(
            lambda: sala_contrato.RespostaListar(tipo="resultado", ferramentas=[{}] * n), reserva
        )

    assert lista(teto) != reserva.para_json()
    assert lista(teto + 1) == reserva.para_json()
    with pytest.raises(sala_contrato.FalhaNaSala) as falha:
        sala_contrato.ler_resposta_listar(200, lista(teto + 1))
    assert falha.value.tipo == "duvida"
    # Nao e a sala fora: o despachante diz que o servidor nao entregou as declaracoes, e as rotinas contam a rodada
    # como sem resposta.
    assert ponto.linha_da_sala(servico.resposta_da_listagem(falha.value), _terceiro("x")) is None
    ameacas = _ler(AMEACAS)
    assert "até 5.000 ferramentas: um servidor maior parece ter ferramentas sumidas" not in ameacas
    assert (
        f"Pela sala, uma lista com mais de {_milhar(teto)} ferramentas nem chega ao despachante: conta como sem"
        f" resposta (quarentena depois de {mudou_parou.RODADAS_SEM_RESPOSTA} rodadas seguidas"
    ) in ameacas
    assert "e a revisão do servidor não conclui." in ameacas
    assert paginas.MOTIVOS_REVISAO_SERVIDOR["sem_resposta"].startswith(
        "O servidor não entregou a lista de ferramentas agora"
    )


def test_o_plano_diz_o_que_o_aceito_faz_e_o_que_a_guarda_prende() -> None:
    plano = _ler(PLANO)
    ameacas = _ler(AMEACAS)
    assert "Os outros não mudam a situação do sistema" not in plano
    assert 'O aceito põe em "ativo" um sistema pendente ou recusado; um sistema em quarentena ou desativado' in plano
    assert "Os desfechos sem veredito (desvio, não é MCP, sem acesso, inalcançável) não mudam a situação" in plano
    # A guarda (M1): o endereco de um terceiro nao muda nunca; a autorizacao so de vazia para um valor.
    guarda = MIGRACAO_0006.read_text(encoding="utf-8").split("Bloco GUARDA_SISTEMAS")[1]
    assert "IF OLD.origem = 'terceiro'\n     AND (NEW.endereco IS DISTINCT FROM OLD.endereco\n" in guarda
    assert "AND OLD.autorizacao IS NOT NULL AND jsonb_typeof(OLD.autorizacao) <> 'null'" in guarda
    for documento in (plano, ameacas):
        assert "vazios a preenchidos" not in documento
        assert "nunca seguem" not in documento
        assert (
            "o endereço não muda depois do cadastro, e a configuração de autorização só passa de vazia a preenchida"
            in documento
        )
        assert "as chaves já guardadas não seguem para um endereço cadastrado depois" in documento
        assert "O nome do servidor continua dependendo do DNS e do certificado do fornecedor" in documento
    assert "quem tiver um certificado válido para esse nome recebe as chaves" in ameacas
    # O retrato do alerta vai a toda ficha que nasce com o alerta ligado, nao so a que nasce pelo alerta.
    rota = inspect.getsource(pedidos.Pedidos._rota)
    for motivo in ("risco_alto", "periodo_de_experiencia", "tres_sinais"):
        assert re.search(rf'motivo="{motivo}", sinais=retrato\)', rota), motivo
    assert "quando ela nasce pelo alerta dos três sinais, o retrato" not in plano
    assert (
        "quando ela nasce com o alerta dos três sinais ligado (pelo alerta, por risco alto ou no período de"
        " experiência), o retrato do alerta"
    ) in plano


def test_o_modelo_de_ameacas_nao_afirma_o_landlock_do_ci_e_diz_o_agente_da_troca_de_chave() -> None:
    ameacas = _ler(AMEACAS)
    assert "O ubuntu-24.04 (o do CI)" not in ameacas
    assert "O runner ubuntu-24.04 do CI depende do núcleo da vez, que muda com o tempo, e não foi conferido" in ameacas
    assert "o log de partida da sala (`landlock=N`) diz a versão em cada máquina" in ameacas
    fluxos = "".join(f.read_text(encoding="utf-8") for f in (RAIZ / ".github" / "workflows").glob("*.yml"))
    assert "runs-on: ubuntu-24.04" in fluxos
    # A conversa MCP com o terceiro vai com nome e versao genericos (D11); a troca de chave (OAuth), nao.
    assert cliente._IDENTIDADE_TERCEIRO.version == "1"
    assert cliente._AGENTE_TERCEIRO == "mcp-claro"
    http = autorizacao.http_para_autorizacao()
    try:
        agente = http.headers["User-Agent"]
    finally:
        asyncio.run(http.aclose())
    assert agente == f"mcp-claro/{__version__}"
    assert (
        "nessa troca, o despachante se apresenta com o nome e a versão do MCP-CLARO (cabeçalho `User-Agent`,"
        " `mcp-claro/` e a versão)"
    ) in ameacas
    assert "A troca de chave (OAuth) ainda leva a versão do produto" in ameacas


def test_os_documentos_nao_dizem_que_o_despachante_nunca_fala_com_o_terceiro() -> None:
    # O despachante fala com o endereco de troca de chave (OAuth) de cada terceiro (critica B8): o que passa so pela
    # sala e a conversa MCP.
    for documento in (PLANO, SIMULADO, README, TERCEIROS):
        texto = _ler(documento)
        assert "o despachante nunca fala direto com eles" not in texto
        assert "Nunca fala direto com um servidor de terceiros" not in texto
        assert "a única que fala com servidores de terceiros" not in texto
    assert "É o único que fala MCP com os servidores de terceiros." in _ler(PLANO)
    assert "a única que fala MCP com servidores de terceiros" in _ler(README)
    simulado = _secao(SIMULADO, "### 12. Sala de visitas fora do ar")
    assert "a renovação ainda vai do despachante ao endereço de troca de chave do terceiro" in simulado
    # A rodada do mudou-parou com a sala fora nao conta contra o servidor: o motivo `sala_fora` e falha do MCP-CLARO
    # para as rotinas (o caminho inteiro esta em test_docs_fase3_g8a.py).
    assert "O mudou-parou não conta essa rodada contra o servidor. Os sistemas próprios seguem normais." in simulado
    assert "sala_fora" in rotinas_despachante.MOTIVOS_DO_MCP_CLARO
    renovar = inspect.getsource(porta.PortaDeSaida._chave)
    assert "renovacao.chave_pronta(" in renovar
    preparacao = _ler(SIMULADO)
    assert "Em desenvolvimento e teste, ela aceita também `http://`." in preparacao
    assert {"desenvolvimento", "teste"} == cliente.AMBIENTES_LOCAIS


# --- Numeros dos documentos contra o codigo ---


def test_numeros_do_roteiro_batem_com_o_codigo() -> None:
    roteiro = _ler(TERCEIROS)
    minimo, maximo = servico.TETO_FORNECEDOR
    assert f"de {minimo} a {maximo} letras" in roteiro
    assert servico.TETO_PUBLICADAS == admin.TETO_PUBLICAR
    assert f"Até {servico.TETO_PUBLICADAS} ferramentas" in roteiro
    assert f"até {esquema.DESCRICAO_DE_CAMPO_MAXIMA} letras" in roteiro
    assert f"até {_milhar(esquema.CUSTO_MAXIMO)} partes" in roteiro
    assert f"até {esquema.PROFUNDIDADE_MAXIMA} níveis" in roteiro
    assert f"até {_milhar(esquema.ESCOLHAS_MAXIMAS)} valores" in roteiro
    assert f"até {esquema.BYTES_MAXIMOS // 1024} KiB" in roteiro
    assert f"em {mudou_parou.RODADAS_SEM_RESPOSTA} rodadas seguidas" in roteiro
    padrao, _, _ = _faixa("TOOL_DRIFT_CHECK_SECONDS")
    assert f"`TOOL_DRIFT_CHECK_SECONDS` ({padrao} segundos, {padrao // 60} minutos" in roteiro


def test_o_plano_e_o_simulado_batem_com_o_codigo_da_sondagem_da_sala_e_do_banco() -> None:
    plano = _ler(PLANO)
    # 405 conta como versao; 406 e 415, como "nao e MCP".
    assert cliente._desfecho_por_status(405) == ("versao", cliente.MOTIVO_TRANSPORTE_ANTIGO)
    assert all(cliente._desfecho_por_status(s)[0] == "nao_mcp" for s in (406, 415))  # type: ignore[index]
    assert "Conta também o HTTP 405" in plano
    assert "(404 sem resposta MCP, página HTML, HTTP 406 ou 415)" in plano
    # So em desenvolvimento a sala libera endereco local (o simulado depende disso).
    assert "self.liberar_locais = p.APP_ENV is Ambiente.DESENVOLVIMENTO" in inspect.getsource(sala)
    assert "Só com `APP_ENV=desenvolvimento` ela aceita um endereço local" in _ler(SIMULADO)
    # O muro tira o endereco de metadados das nuvens (169.254.0.0/16).
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert "169.254.0.0/16" in " ".join(compose["services"]["visitas-muro"]["entrypoint"])
    assert "o endereço de metadados das nuvens" in plano
    # O Compose usa https para a sala, como o plano diz.
    porta_sala, _, _ = _faixa("VISITS_INTERNAL_PORT")
    assert compose["services"]["despachante"]["environment"]["VISITS_INTERNAL_URL"] == f"https://visitas:{porta_sala}"
    assert f"| `VISITS_INTERNAL_URL` | `https://visitas:{porta_sala}` no Compose;" in plano
    # O banco recusa chave de programa num terceiro.
    migracao = MIGRACAO_0006.read_text(encoding="utf-8")
    assert "CHECK (origem = 'proprio' OR tipo_chave IN ('por_pessoa', 'compartilhada'))" in migracao
    assert "Um servidor de terceiros usa só chave por pessoa ou chave compartilhada (que só lê)." in plano


def test_numeros_do_plano_batem_com_o_codigo() -> None:
    plano = _ler(PLANO)
    porta, minimo, maximo = _faixa("VISITS_INTERNAL_PORT")
    assert f"| `VISITS_INTERNAL_PORT` | {porta} (de {minimo} a {maximo}) |" in plano
    padrao, minimo, maximo = _faixa("TOOL_DRIFT_CHECK_SECONDS")
    assert f"| `TOOL_DRIFT_CHECK_SECONDS` | {padrao} (de {minimo} a {maximo}) |" in plano
    janela = politica.POLITICA["THREE_SIGNALS_WINDOW_MINUTES"]
    assert f"| `THREE_SIGNALS_WINDOW_MINUTES` | {janela.inicial} (de {janela.minimo} a {janela.maximo}) |" in plano
    assert janela.maximo == sinais.MAIOR_JANELA_MINUTOS == 7 * 24 * 60
    assert "só se apaga depois de 7 dias" in plano
    chave = _campo("VAULT_TRANSIT_KEY_THIRD_PARTY").default
    propria = _campo("VAULT_TRANSIT_KEY").default
    assert f"| `VAULT_TRANSIT_KEY_THIRD_PARTY` | `{chave}` |" in plano
    assert f"(`VAULT_TRANSIT_KEY`, `{propria}`)" in plano
    assert f"https://visitas:{porta}" in plano
    assert f'até {cliente.MAX_VERSOES}; o resto vira "outra"' in plano
    assert "(até 500)" in plano
    assert servico.TETO_PUBLICADAS == 500
    assert f"Com {mudou_parou.RODADAS_SEM_RESPOSTA} rodadas seguidas sem resposta" in plano


def test_numeros_do_modelo_de_ameacas_batem_com_o_codigo() -> None:
    ameacas = _ler(AMEACAS)
    assert f"a sala deixa {sala.PRONTOS} filhos já iniciados" in ameacas
    assert f"atende até {sala.MAX_CHAMADAS} chamadas ao mesmo tempo" in ameacas
    assert len(estranhos.nova_marca()) == 12
    assert "marca sorteada (12 letras, uma por resposta)" in ameacas
    assert f"{cliente._PAGINAS} páginas" in ameacas
    assert f"mais de {_milhar(sala_contrato.TETO_FERRAMENTAS)} ferramentas nem chega ao despachante" in ameacas
    padrao, _, _ = _faixa("TOOL_DRIFT_CHECK_SECONDS")
    assert f"`TOOL_DRIFT_CHECK_SECONDS`, {padrao} segundos" in ameacas
    janela = politica.POLITICA["THREE_SIGNALS_WINDOW_MINUTES"]
    assert f"({janela.inicial} minutos de início)" in ameacas
    assert _campo("VAULT_TRANSIT_KEY_THIRD_PARTY").default in ameacas


# --- Item 1: plano (B8, :392, :597, redes, quem fala com quem, pecas, dados) ---


def test_o_plano_traz_a_frase_do_d1_corrigida_pela_critica_b8() -> None:
    plano = _ler(PLANO)
    assert (
        "O despachante fala com os sistemas próprios, com a sala e com o endereço de troca de chave (OAuth) de cada"
        " terceiro; só o despachante abre as chaves."
    ) in plano
    assert "só o despachante fala com os sistemas e abre as chaves" not in plano
    assert "É o único que fala com os sistemas" not in plano


# As frases da B8 inteiras (etapa 5f): a troca e a renovacao da chave (OAuth) de um terceiro saem do despachante, pela
# rede `saida`, direto ao endereco de troca do terceiro, conferido como o da sala; a conversa MCP passa pela sala.
FRASES_DA_B8 = (
    (
        README,
        "| `despachante` | Único que abre as chaves. Fala com os sistemas próprios, com a sala de visitas e com a troca"
        " de chave (OAuth) dos servidores de terceiros |",
    ),
    (
        PLANO,
        "Três papéis do gateway saem para a internet: o despachante, que fala com os sistemas próprios e com o endereço"
        " de troca de chave (OAuth) de cada terceiro; a sala de visitas, que fala com os servidores de terceiros; e as"
        " rotinas, que mandam avisos.",
    ),
    (
        PLANO,
        "- **Gateway, papel `despachante`.** É o único que abre as chaves das pessoas. Fala com os sistemas próprios,"
        " com a sala de visitas e com o endereço de troca de chave (OAuth) de cada terceiro. Atende leituras na hora,"
        " depois de reconferi-las, e envia ações só a partir da fila. A conversa MCP com um servidor de terceiros passa"
        " sempre pela sala: sem ela, nenhum pedido MCP vai ao servidor. Só a troca e a renovação da chave saem do"
        " despachante direto ao endereço de troca de chave do terceiro.",
    ),
    (
        TERCEIROS,
        "- conversa com ele só pela **sala de visitas**: só no endereço combinado, sem seguir desvios, com a chave"
        " guardada à parte e sem dados de rastreio. Só a troca e a renovação da chave de cada pessoa (OAuth) saem do"
        " despachante, direto ao endereço de troca que o fornecedor deu, com a mesma conferência de endereço;",
    ),
    (
        AMEACAS,
        "Por isso o despachante fala com os sistemas próprios, com a sala e com o endereço de troca de chave (OAuth) de"
        " cada terceiro, pela rede `saida`. Antes, esse endereço passa pela mesma conferência da sala (https e só"
        " endereço da internet); um endereço recusado não recebe nada e vira `sala_endereco_recusado` no extrato.",
    ),
)


def _redes(compose: dict[str, Any], servico_compose: str) -> set[str]:
    """As redes de um servico do Compose; a sala usa as do muro (``network_mode: service:visitas-muro``)."""
    dados = compose["services"][servico_compose]
    modo = str(dados.get("network_mode", ""))
    if modo.startswith("service:"):
        return _redes(compose, modo.removeprefix("service:"))
    return set(dados.get("networks") or ())


@pytest.mark.parametrize(
    ("documento", "frase"), FRASES_DA_B8, ids=[f"{d.name}-{i}" for i, (d, _) in enumerate(FRASES_DA_B8)]
)
def test_as_frases_da_b8_estao_inteiras(documento: Path, frase: str) -> None:
    assert frase in _ler(documento), documento.name


def test_as_frases_da_b8_dizem_o_que_o_codigo_faz() -> None:
    """O que as frases de ``FRASES_DA_B8`` dizem, conferido no codigo e no Compose."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    # Tres papeis do gateway saem para a internet: o despachante e as rotinas pela `saida`; a sala, pela rede dela.
    # Sai para a internet quem esta numa rede que o Compose nao fecha (``internal: true`` fecha; etapa 5f, retoque).
    redes = compose["networks"]
    abertas = {nome for nome, rede in redes.items() if not (rede or {}).get("internal")}
    assert {"saida", "saida_visitas"} <= abertas
    assert not (redes["saida"] or {}).get("internal")
    assert not (redes["saida_visitas"] or {}).get("internal")
    assert redes["interna"].get("internal") is True
    assert redes["visitas"].get("internal") is True
    papeis = {str(p) for p in Papel}
    gateway = {n for n, s in compose["services"].items() if (s.get("command") or [None])[0] in papeis}
    para_fora = {n for n in gateway if _redes(compose, n) & abertas}
    assert para_fora == {"despachante", "visitas", "rotinas"}
    assert {n: _redes(compose, n) & abertas for n in para_fora} == {
        "despachante": {"saida"},
        "rotinas": {"saida"},
        "visitas": {"saida_visitas"},
    }
    assert _redes(compose, "despachante") == {"interna", "saida", "visitas"}
    assert _redes(compose, "visitas") == {"visitas", "saida_visitas"}
    # Sem a sala, nenhum pedido MCP vai a um terceiro fora de desenvolvimento e teste.
    for ambiente in ("piloto", "producao"):
        assert (
            ponto.ligacao_para(_terceiro("x"), ambiente=ambiente, local_para_terceiro=ponto.PROPRIO) is ponto.SEM_SALA
        )
    # A troca e a renovacao da chave saem do despachante: um POST no endereco de troca do terceiro, depois da mesma
    # conferencia de endereco que a sala usa; recusado, nada sai e o extrato ganha `sala_endereco_recusado`.
    trocar = inspect.getsource(autorizacao.trocar)
    conferir = (
        "if terceiro:\n        await _conferir_troca_de_terceiro(config.token_endpoint, ambiente, liberar_locais)"
    )
    assert trocar.index(conferir) < trocar.index('http.stream("POST", config.token_endpoint')
    assert "cliente.conferir_endereco_terceiro(" in inspect.getsource(autorizacao._conferir_troca_de_terceiro)
    assert "await conferir_endereco_terceiro(endereco, ambiente, liberar_locais=liberar_locais)" in inspect.getsource(
        cliente._endereco_do_terceiro
    )
    renovar = (FONTES / "despachante" / "renovacao.py").read_text(encoding="utf-8")
    assert "autorizacao.trocar(" in renovar
    assert "terceiro=sistema.de_terceiro" in renovar
    for fonte in (inspect.getsource(autorizacao), renovar):
        assert 'tipo="sala_endereco_recusado"' in fonte
    # Ninguem na ligacao nem na sala troca ou renova chave (ver test_o_plano_poe_a_troca_de_chave_no_despachante).
    for pasta in ("ligacao", "visitas"):
        for fonte_py in (FONTES / pasta).rglob("*.py"):
            assert "token_endpoint" not in fonte_py.read_text(encoding="utf-8"), fonte_py


def test_o_modelo_de_ameacas_diz_as_saidas_da_quarentena_que_o_banco_aceita() -> None:
    """Item 3 da etapa 5f: a guarda da 0006 tira um servidor da quarentena por dois caminhos so, a conclusao de uma
    revisao nova (de volta a "ativo") e a desativacao; o modelo de ameacas e o plano dizem os dois."""
    guarda = MIGRACAO_0006.read_text(encoding="utf-8").split("Bloco GUARDA_SISTEMAS")[1]
    inicio = guarda.index("IF OLD.situacao = 'quarentena' AND NEW.situacao IS DISTINCT FROM 'quarentena'")
    bloco = guarda[inicio : guarda.index("'Servidor em quarentena so sai pela nova revisao do ADMIN'", inicio)]
    assert re.findall(r"AND NOT \(NEW\.situacao = '(\w+)'", bloco) == ["ativo", "desativado"]
    volta = bloco[bloco.index("NEW.situacao = 'ativo'") : bloco.index("NEW.situacao = 'desativado'")]
    for condicao in (
        "current_user = 'mcp_claro_despachante'",
        "NEW.versao_mcp IS NOT DISTINCT FROM '2026-07-28'",
        "NEW.revisao_servidor_id IS NOT NULL",
        "NEW.revisao_servidor_id IS DISTINCT FROM OLD.revisao_servidor_id",
    ):
        assert condicao in volta, condicao
    assert "NEW.situacao = 'desativado' AND current_user IN ('mcp_claro_entrada', dono)" in bloco
    # Mudar o endereco ou o tipo de chave de um servidor em quarentena nao o tira de la (salvo para desativar).
    assert "NEW.situacao := CASE WHEN NEW.situacao = 'desativado' THEN 'desativado' ELSE 'quarentena' END;" in guarda
    ameacas = _ler(AMEACAS)
    assert "o banco recusa outra saída" not in ameacas
    assert (
        "Duas rodadas seguidas sem resposta também. Só uma nova conclusão da revisão do servidor o põe de volta em"
        " serviço; além dela, o banco só aceita desativá-lo. |"
    ) in ameacas
    assert (
        'Um servidor em quarentena só sai dela para "ativo" pela conclusão de uma nova revisão do servidor, feita'
        ' pelo despachante, ou para "desativado".'
    ) in _ler(PLANO)


def test_a_linha_do_rastro_nao_promete_so_por_padrao_e_cita_os_valores_que_existem() -> None:
    linha = next(x for x in PLANO.read_text(encoding="utf-8").splitlines() if "`OBS_TRACE_PROPAGATE_DOWNSTREAM`" in x)
    assert "por padrão" not in linha
    assert "Nenhum valor libera" in linha
    assert "(D10)" in linha
    for valor in PropagacaoRastro:
        assert f"`{valor}`" in linha
    assert str(_campo("OBS_TRACE_PROPAGATE_DOWNSTREAM").default) == PropagacaoRastro.SISTEMAS_PROPRIOS


def test_o_plano_nao_diz_mais_que_o_cliente_nao_responde_sozinho() -> None:
    plano = _ler(PLANO)
    assert "e assim não responde sozinho a pedidos do sistema" not in plano
    assert (
        "A barreira de pedidos do cliente fixado não deixa sair nenhum POST que não seja o pedido da própria chamada"
        in (plano)
    )
    # O que o codigo diz da barreira (ligacao/cliente.py).
    assert "barreira de pedidos (vale para todos os sistemas): nenhum POST que nao seja pedido" in _plano(
        cliente.__doc__ or ""
    )


def test_o_plano_traz_as_redes_as_pecas_e_quem_fala_com_quem_da_sala() -> None:
    plano = _ler(PLANO)
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    redes = compose["networks"]
    assert redes["visitas"]["internal"] is True
    assert "saida_visitas" in redes
    assert "- `visitas` (Fase 3): só despachante e sala de visitas. Sem saída para a internet." in plano
    assert "- `saida_visitas` (Fase 3): só a sala de visitas" in plano
    muro = compose["services"]["visitas-muro"]
    assert set(muro["networks"]) == {"visitas", "saida_visitas"}
    assert muro["cap_add"] == ["NET_ADMIN"]
    imagem = str(muro["image"]).split("@")[0]
    assert f"| Muro da sala | `{imagem}`, fixada por digest" in plano
    servico_sala = compose["services"]["visitas"]
    assert servico_sala["network_mode"] == "service:visitas-muro"
    assert servico_sala["entrypoint"][-2:] == ["-m", "mcp_claro"]
    assert servico_sala["command"] == ["visitas"]
    assert "iniciada por `python -m mcp_claro visitas`" in plano
    assert set(compose["services"]["despachante"]["networks"]) == {"interna", "saida", "visitas"}
    porta, _, _ = _faixa("VISITS_INTERNAL_PORT")
    assert f"| Despachante | Sala de visitas | {porta}, rede `visitas` |" in plano
    assert "| Sala de visitas | Servidores de terceiros | 443, HTTPS, rede `saida_visitas` |" in plano
    assert "| Despachante | Endereço de troca de chave (OAuth) de cada terceiro | 443, HTTPS, rede `saida` |" in plano
    assert "| Rotinas | Despachante | 9443, interna |" in plano


def test_a_tabela_de_dados_do_plano_traz_a_migracao_0006() -> None:
    plano = _ler(PLANO)
    migracao = (RAIZ / "migrations" / "versions" / "0006_fase3.py").read_text(encoding="utf-8")
    for tabela in ("revisoes_servidor", "sinais"):
        assert f"CREATE TABLE {tabela}" in migracao
        assert f"| `{tabela}` |" in plano
    for coluna in ("conferencias_sem_resposta", "conferencia_sem_resposta_em", "revisao_servidor_id"):
        assert f"ADD COLUMN {coluna}" in migracao
    assert "`sistemas.conferencias_sem_resposta`" in plano
    assert "`sistemas.conferencia_sem_resposta_em`" in plano
    assert "CREATE VIEW medicoes.v_terceiros" in migracao
    assert "`medicoes.v_terceiros`" in plano
    assert "v_recusas_versao_terceiros" not in migracao
    assert "v_recusas_versao_terceiros" not in plano
    assert "REVOKE TEMPORARY ON DATABASE" in migracao
    assert "REVOKE TEMPORARY ... FROM PUBLIC" in plano


def test_o_plano_traz_a_prova_nova_d31_e_o_roteiro() -> None:
    plano = _ler(PLANO)
    assert "nada dela passa fora da marca (prova acrescentada pela decisão D31" in plano
    assert "[terceiros](terceiros.md)" in plano


# --- Itens 2 e 3: modelo de ameacas e segredos ---


def test_o_modelo_de_ameacas_traz_a_anotacao_da_b8_e_o_landlock() -> None:
    ameacas = _ler(AMEACAS)
    assert "a troca do código de autorização e a renovação da chave (OAuth) de um terceiro ficam no despachante" in (
        ameacas
    )
    assert "**Sala sem Landlock (decisão do dono).**" in ameacas
    assert "`landlock=0`" in ameacas
    filho = (RAIZ / "src" / "mcp_claro" / "visitas" / "app.py").read_text(encoding="utf-8")
    assert "landlock=filho.versao_landlock()" in filho
    assert '"Não saiu" forjável por um terceiro (risco aceito, em' in ameacas
    assert "-32601, -32602, -32600, -32700 e -32021" in ameacas


def test_os_segredos_da_sala_existem_no_compose_e_a_sala_nao_recebe_banco_nem_cofre() -> None:
    texto = _ler(SEGREDOS)
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    segredos = set(compose["secrets"])
    for nome in (
        "ca_visitas.pem",
        "visitas_cert.pem",
        "visitas_key.pem",
        "ca_clientes_visitas.pem",
        "despachante_visitas_cliente_cert.pem",
        "despachante_visitas_cliente_key.pem",
    ):
        assert nome in segredos
        assert f"`{nome}`" in texto
    sala_recebe = set(compose["services"]["visitas"]["secrets"])
    assert sala_recebe == {"ca_visitas.pem", "visitas_cert.pem", "visitas_key.pem", "ca_clientes_visitas.pem"}
    assert not any(s.startswith(("db_", "cofre_")) for s in sala_recebe)
    for chave_de_autoridade in ("ca_visitas_key.pem", "ca_clientes_visitas_key.pem"):
        assert chave_de_autoridade not in segredos
        assert f"`{chave_de_autoridade}`" in texto
    assert PREFIXOS_PROIBIDOS == ("DATABASE_", "DB_", "VAULT_")
    assert "recusa qualquer variável `DATABASE_*`, `DB_*` ou `VAULT_*`" in texto
    assert f"`{_campo('VAULT_TRANSIT_KEY_THIRD_PARTY').default}`" in texto
    cofre = (RAIZ / "scripts" / "iniciar-cofre.sh").read_text(encoding="utf-8")
    assert f"CHAVE_TERCEIROS={_campo('VAULT_TRANSIT_KEY_THIRD_PARTY').default}" in cofre
    assert "derived=true" in cofre


# --- Item 6: comentario das falhas esperadas ---


def test_o_comentario_das_falhas_esperadas_fala_da_barreira_e_do_d13() -> None:
    texto = FALHAS.read_text(encoding="utf-8")
    bloco = texto.split("# --- Sem pedidos do sistema ao aplicativo")[1].split("  - input-required-result")[0]
    comentario = _plano(bloco.replace("#", " "))
    assert "nao responde sozinho" not in comentario
    assert "a barreira de pedidos do cliente fixado (ligacao/cliente.py)" in comentario
    assert "nunca o -32021 (D13)" in comentario
    assert "o fixture desta suite e um sistema proprio" in comentario
    # So o comentario mudou: as entradas continuam as mesmas.
    dados = yaml.safe_load(texto)
    assert dados["client"] == []
    assert len([e for e in dados["server"] if e.startswith("input-required-result")]) == 11
    assert len(dados["server"]) == 30


# --- Links e estilo ---

_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _ancora(titulo: str) -> str:
    """O nome da ancora de um titulo, como o GitHub monta."""
    titulo = titulo.strip().lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", titulo).replace(" ", "-")


def _ancoras(caminho: Path) -> set[str]:
    return {_ancora(x.lstrip("#")) for x in caminho.read_text(encoding="utf-8").splitlines() if x.startswith("#")}


@pytest.mark.parametrize("documento", DOCS_DO_GRUPO, ids=lambda d: d.name)
def test_links_entre_documentos_apontam_para_arquivo_e_titulo_que_existem(documento: Path) -> None:
    for alvo in _LINK.findall(documento.read_text(encoding="utf-8")):
        if alvo.startswith(("http://", "https://", "mailto:")):
            continue
        caminho, _, ancora = alvo.partition("#")
        destino = (documento.parent / caminho).resolve() if caminho else documento
        assert destino.is_file(), f"{documento.name}: link para {alvo} sem arquivo"
        if ancora and destino.suffix == ".md":
            assert ancora in _ancoras(destino), f"{documento.name}: {alvo} sem esse titulo"


_PROMESSAS = re.compile(
    r"\b(garante|garantimos|garantido|garantida|nunca falha|sem risco|totalmente seguro)\b|100 ?%", re.I
)


def _emoji(texto: str) -> list[str]:
    return [c for c in texto if unicodedata.category(c) == "So" or 0x1F000 <= ord(c) <= 0x1FAFF]


@pytest.mark.parametrize("documento", [AMEACAS, SEGREDOS, TERCEIROS, SIMULADO], ids=lambda d: d.name)
def test_documentos_do_grupo_sem_emoji_e_sem_promessa_de_garantia(documento: Path) -> None:
    texto = documento.read_text(encoding="utf-8")
    assert not _emoji(texto)
    assert not _PROMESSAS.findall(texto)


def test_o_roteiro_e_para_leigo_com_acento() -> None:
    texto = TERCEIROS.read_text(encoding="utf-8")
    sem_codigo = re.sub(r"`[^`]*`", "", texto)
    for palavra in ("não", "você", "versão", "revisão", "conferência"):
        assert palavra in sem_codigo
    # Fora de trecho de codigo, nenhuma palavra comum sem acento.
    assert not re.search(r"\b(nao|voce|versao|revisao|sera|esta escolha nao)\b", sem_codigo)
