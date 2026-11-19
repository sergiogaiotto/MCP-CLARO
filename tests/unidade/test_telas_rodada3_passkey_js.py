"""O passkey.js de verdade, rodado no Node com um DOM de mentira: quando o desafio da passkey e recusado com um
passo seguinte ({"erro", "link"}), o aviso mostra a frase e um botao para esse caminho. Sem link, ou com um link
que nao e caminho local, fica so a frase.

O corpo do caso principal e o que ``TelasDeLimites.desafio_criar`` responde quando o ADMIN acabou de revisar a
acao (o formulario foi aberto antes da revisao e o toque vem depois)."""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
from typing import Any
from urllib.parse import urlencode

import pytest

import mcp_claro.telas
from mcp_claro.telas import limites
from tests.unidade.test_telas_rodada3 import _arranjo_revisada, _post, _telas, _texto

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="sem Node para rodar o passkey.js")

# O arquivo que as telas servem (o do pacote importado).
PASSKEY_JS = pathlib.Path(mcp_claro.telas.__file__).parent / "estaticos" / "passkey.js"

# Um formulario com data-passkey, a caixa [data-passkey-aviso] e o fetch do desafio; o resto do DOM nao existe.
ARRANJO = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const cfg = JSON.parse(fs.readFileSync(0, "utf8"));

function elemento(tag) {
  return {
    tagName: tag.toUpperCase(), className: "", atributos: {}, filhos: [], hidden: true, proprio: "",
    setAttribute(nome, valor) { this.atributos[nome] = String(valor); },
    appendChild(filho) { this.filhos.push(filho); return filho; },
    get textContent() { return this.proprio + this.filhos.map(function (f) { return f.textContent; }).join(""); },
    set textContent(valor) { this.proprio = String(valor); this.filhos = []; },
  };
}

class HTMLFormElement {}
const form = new HTMLFormElement();
form.dataset = { passkey: "entrar", desafio: "/telas/limites/desafio" };
const campo = { value: "" };
const caixa = elemento("p");
caixa.appendChild({ textContent: "aviso de antes" });
let enviado = false, alertas = [], pedidos = [];
form.querySelector = function (sel) {
  if (sel === "input[name=credencial]") { return campo; }
  if (sel === "[data-passkey-aviso]") { return caixa; }
  return null;
};
form.querySelectorAll = function () { return []; };
form.submit = function () { enviado = true; };

const ouvintes = [];
globalThis.window = globalThis;
globalThis.HTMLFormElement = HTMLFormElement;
globalThis.document = {
  addEventListener: function (tipo, f) { ouvintes.push([tipo, f]); },
  createElement: elemento,
  createTextNode: function (t) { return { textContent: String(t) }; },
};
globalThis.alert = function (t) { alertas.push(t); };
globalThis.PublicKeyCredential = function () {};
const recusa = new Error("cancelado");
recusa.name = "NotAllowedError";
Object.defineProperty(globalThis, "navigator", {
  value: { credentials: { get: function () { return Promise.reject(recusa); } } },
  configurable: true,
});
globalThis.FormData = function () { return [["csrf", "t"]]; };
globalThis.fetch = function (caminho) {
  pedidos.push(caminho);
  return Promise.resolve({ ok: cfg.status < 400, json: function () { return Promise.resolve(cfg.corpo); } });
};

vm.runInThisContext(fs.readFileSync(cfg.arquivo, "utf8"), { filename: "passkey.js" });
const evento = { target: form, preventDefault: function () {}, stopImmediatePropagation: function () {} };
ouvintes.forEach(function (o) { if (o[0] === "submit") { o[1](evento); } });
setTimeout(function () {
  process.stdout.write(JSON.stringify({
    pedidos: pedidos,
    escondido: caixa.hidden,
    frase: caixa.proprio,
    filhos: caixa.filhos.map(function (f) {
      return {
        tag: f.tagName || null, classe: f.className || null, atributos: f.atributos || null, texto: f.textContent,
      };
    }),
    enviado: enviado,
    alertas: alertas,
  }));
}, 50);
"""


def _rodar(status: int, corpo: dict[str, Any]) -> dict[str, Any]:
    assert NODE is not None
    entrada = json.dumps({"arquivo": str(PASSKEY_JS), "status": status, "corpo": corpo})
    feito = subprocess.run(  # noqa: S603 - comando fixo de teste
        [NODE, "-e", ARRANJO], input=entrada, capture_output=True, text=True, timeout=60, check=False
    )
    assert feito.returncode == 0, feito.stderr
    resultado: dict[str, Any] = json.loads(feito.stdout)
    assert resultado["pedidos"] == ["/telas/limites/desafio"]
    assert resultado["enviado"] is False
    assert resultado["alertas"] == []
    assert resultado["escondido"] is False
    return resultado


def test_desafio_recusado_porque_o_admin_revisou_mostra_o_botao_para_a_versao_nova(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nova, velha = _arranjo_revisada(monkeypatch)
    form = [("escolha", velha), ("destinatarios", "qualquer"), ("por_dia", "1"), ("dias", "1")]
    t = _telas(monkeypatch, ligada=True, form=form)
    r = asyncio.run(limites.TelasDeLimites(t).desafio_criar(_post()))
    assert r.status_code == 409

    visto = _rodar(r.status_code, json.loads(_texto(r)))
    assert visto["frase"] == "O ADMIN acabou de revisar esta ação. Abra Criar limite de novo."
    # O aviso de antes some; fica a frase, um espaco e o botao.
    assert visto["filhos"] == [
        {"tag": None, "classe": None, "atributos": None, "texto": " "},
        {
            "tag": "A",
            "classe": "botao",
            "atributos": {
                "href": "/telas/limites/novo?" + urlencode({"escolha": nova.chave}),
                "hx-boost": "false",
            },
            "texto": "Criar limite para esta ação",
        },
    ]


@pytest.mark.parametrize(
    "corpo",
    [
        {"erro": "Escolha a quantidade por dia."},
        {"erro": "Escolha a quantidade por dia.", "link": None},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "//mal.exemplo/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "/\\mal.exemplo/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "/\t/mal.exemplo/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "/\n/mal.exemplo/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "/\r\\mal.exemplo/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "/telas/x\\y", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": " /telas/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "https://mal.exemplo/x", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "javascript:alert(1)", "texto": "Ir"}},
        {"erro": "Escolha a quantidade por dia.", "link": {"caminho": "/telas/x"}},
        {"erro": "Escolha a quantidade por dia.", "link": "/telas/x"},
    ],
)
def test_sem_link_local_o_aviso_e_so_a_frase(corpo: dict[str, Any]) -> None:
    visto = _rodar(400, corpo)
    assert visto["frase"] == "Escolha a quantidade por dia."
    assert visto["filhos"] == []


def test_recusa_do_navegador_nao_leva_link() -> None:
    """O desafio veio; quem recusou foi o navegador (a pessoa cancelou): so a frase de sempre."""
    visto = _rodar(200, {"publicKey": {"challenge": "AAAA"}, "link": {"caminho": "/telas/x", "texto": "Ir"}})
    assert visto["frase"].startswith("A passkey não foi usada: o pedido foi cancelado ou passou do tempo.")
    assert visto["filhos"] == []
