/* Cerimonia da passkey (WebAuthn) nas telas do MCP-CLARO. Sem biblioteca de fora.
 *
 * Um formulario com data-passkey="entrar" (confirmar com a passkey) ou "cadastrar" (nova passkey):
 * no envio, pede um desafio novo ao servidor (POST em data-desafio, com o token CSRF do formulario),
 * chama o navegador e manda a resposta no campo escondido "credencial". Quem confere e o servidor,
 * no mesmo envio que confirma. Nada de segredo fica aqui.
 * Se o servidor recusa o desafio com um passo seguinte ({"erro", "link": {"caminho", "texto"}}), o aviso
 * mostra a frase e um botao para esse caminho (so caminho local, comecando com uma barra).
 */
(function () {
  "use strict";
  if (window.mcpClaroPasskey) { return; }
  window.mcpClaroPasskey = true;

  function paraTexto(buffer) {
    var bytes = new Uint8Array(buffer), bruto = "";
    for (var i = 0; i < bytes.length; i++) { bruto += String.fromCharCode(bytes[i]); }
    return btoa(bruto).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function paraBytes(texto) {
    var base = texto.replace(/-/g, "+").replace(/_/g, "/");
    while (base.length % 4) { base += "="; }
    var bruto = atob(base), bytes = new Uint8Array(bruto.length);
    for (var i = 0; i < bruto.length; i++) { bytes[i] = bruto.charCodeAt(i); }
    return bytes.buffer;
  }

  function credenciais(lista) {
    return (lista || []).map(function (c) {
      var d = { type: c.type, id: paraBytes(c.id) };
      if (c.transports) { d.transports = c.transports; }
      return d;
    });
  }

  function opcoesCriar(o) {
    if (window.PublicKeyCredential && PublicKeyCredential.parseCreationOptionsFromJSON) {
      return PublicKeyCredential.parseCreationOptionsFromJSON(o);
    }
    var c = Object.assign({}, o);
    c.challenge = paraBytes(o.challenge);
    c.user = Object.assign({}, o.user, { id: paraBytes(o.user.id) });
    c.excludeCredentials = credenciais(o.excludeCredentials);
    return c;
  }

  function opcoesPegar(o) {
    if (window.PublicKeyCredential && PublicKeyCredential.parseRequestOptionsFromJSON) {
      return PublicKeyCredential.parseRequestOptionsFromJSON(o);
    }
    var c = Object.assign({}, o);
    c.challenge = paraBytes(o.challenge);
    c.allowCredentials = credenciais(o.allowCredentials);
    return c;
  }

  function comoJSON(cred) {
    if (typeof cred.toJSON === "function") { return cred.toJSON(); }
    var r = cred.response, resposta = { clientDataJSON: paraTexto(r.clientDataJSON) };
    if (r.attestationObject) {
      resposta.attestationObject = paraTexto(r.attestationObject);
      if (typeof r.getTransports === "function") { resposta.transports = r.getTransports(); }
    } else {
      resposta.authenticatorData = paraTexto(r.authenticatorData);
      resposta.signature = paraTexto(r.signature);
      if (r.userHandle) { resposta.userHandle = paraTexto(r.userHandle); }
    }
    return {
      id: cred.id,
      rawId: paraTexto(cred.rawId),
      type: cred.type,
      response: resposta,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}
    };
  }

  function linkLocal(link) {
    if (!link || typeof link.caminho !== "string" || typeof link.texto !== "string") { return null; }
    var c = link.caminho;
    // So caminho deste site: comeca com uma barra so e nao tem barra invertida nem caractere de controle em
    // lugar nenhum (o navegador apaga TAB e quebra de linha antes de ler o endereco: "/<TAB>/outro.site" viraria
    // "//outro.site").
    if (!/^\/(?![\/\\])[^\u0000-\u001f\u007f\\]*$/.test(c)) { return null; }
    return { caminho: c, texto: link.texto };
  }

  function avisar(form, texto, link) {
    var caixa = form.querySelector("[data-passkey-aviso]");
    if (!caixa) { window.alert(texto); return; }
    caixa.textContent = texto;
    if (link) {
      var a = document.createElement("a");
      a.className = "botao";
      a.setAttribute("href", link.caminho);
      a.setAttribute("hx-boost", "false");
      a.textContent = link.texto;
      caixa.appendChild(document.createTextNode(" "));
      caixa.appendChild(a);
    }
    caixa.hidden = false;
  }

  function frase(erro) {
    if (erro && erro.name === "NotAllowedError") {
      return "A passkey não foi usada: o pedido foi cancelado ou passou do tempo. Nada mudou. Tente de novo.";
    }
    if (erro && erro.name === "InvalidStateError") {
      return "Esta passkey já está cadastrada neste aparelho. Nada mudou.";
    }
    if (erro && erro.passkey) { return erro.message; }
    return "Não deu para usar a passkey agora. Nada mudou. Recarregue a página e tente de novo.";
  }

  document.addEventListener("submit", function (evento) {
    var form = evento.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.passkey || !form.dataset.desafio) { return; }
    var campo = form.querySelector("input[name=credencial]");
    if (!campo || campo.value) { return; }
    evento.preventDefault();
    evento.stopImmediatePropagation();
    if (!window.PublicKeyCredential || !navigator.credentials) {
      avisar(form, "Este navegador não sabe usar passkey. Use um navegador atualizado.");
      return;
    }
    var botoes = form.querySelectorAll("button");
    botoes.forEach(function (b) { b.disabled = true; });
    var cadastrar = form.dataset.passkey === "cadastrar";
    fetch(form.dataset.desafio, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
      body: new URLSearchParams(new FormData(form))
    }).then(function (resposta) {
      return resposta.json().then(function (dados) {
        if (!resposta.ok || !dados.publicKey) {
          var erro = new Error(dados.erro || "Não deu para pedir a passkey agora.");
          erro.passkey = true;
          erro.link = linkLocal(dados.link);
          throw erro;
        }
        return dados.publicKey;
      });
    }).then(function (opcoes) {
      return cadastrar
        ? navigator.credentials.create({ publicKey: opcoesCriar(opcoes) })
        : navigator.credentials.get({ publicKey: opcoesPegar(opcoes) });
    }).then(function (cred) {
      if (!cred) { throw new Error("sem resposta"); }
      campo.value = JSON.stringify(comoJSON(cred));
      form.submit();
    }).catch(function (erro) {
      botoes.forEach(function (b) { b.disabled = false; });
      avisar(form, frase(erro), erro && erro.passkey ? erro.link : null);
    });
  }, true);
})();
