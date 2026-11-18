# Provedor de login

O provedor de login emite os crachás (JWT) que os aplicativos apresentam ao MCP-CLARO. O padrão é o **Keycloak
26.8.2** (`IDP_KIND=keycloak`), com o realm `mcp-claro` guardado em
[`docker/login/realm-mcp-claro.json`](../docker/login/realm-mcp-claro.json) e importado na partida (`--import-realm`).

## Por que Keycloak (decisão 3)

- Funciona inteiro sem licença paga (Apache-2.0). Nada do que usamos fica atrás de plano comercial.
- Conta de serviço com JWT assinado (`private_key_jwt`): o programa assina um pedido curto com a chave dele, e o
  Keycloak guarda só a chave pública. Não há segredo compartilhado para vazar.
- O verificador do MCP-CLARO é genérico (OIDC com JWKS): troca-se o provedor sem mudar código.

## O realm

| Cliente | Quem é | Como entra |
| --- | --- | --- |
| `claude-code`, `goose` | Aplicativos públicos de IA, usados por pessoas | Authorization code com PKCE S256, sem segredo, retorno só em `http://127.0.0.1/*` ou `http://localhost/*` (qualquer porta) |
| `mcp-claro-ronda`, `mcp-claro-vigia` | Programas que rodam sozinhos (nascem na Fase 1) | Conta de serviço com JWT assinado (ES256). Crachá de 300 s, sem refresh token |
| `mcp-claro-teste-cc` | **Só de teste automatizado** | `client_credentials` com segredo |
| `outro-servico` | **Só de teste**: o "outro condomínio" | `client_credentials` com segredo; crachá com audiência de outro recurso |

Regras do realm:

- Todos os clientes são pré-cadastrados. O cadastro dinâmico está fechado: as políticas de registro recusam pedido
  anônimo e pedido com token inicial (limite de 0 clientes). O Keycloak ainda anuncia `registration_endpoint` na
  descoberta, mas ele responde 403. O cadastro por documento de metadados (`cimd`) fica desligado, como vem.
- Crachás de 300 s. Refresh token das pessoas é de uso único (rodízio).
- Sem cadastro de pessoas pela tela de login, sem papéis no crachá (`fullScopeAllowed` desligado) e com proteção
  contra força bruta. Eventos de login (por 30 dias) e de administração, com detalhes, ficam guardados.
- O pedido assinado do programa vale por no máximo 60 s depois de emitido, é de uso único (`jti`) e só aceita ES256.

### Mapeador de audiência

O crachá precisa citar como audiência (`aud`) o recurso do MCP-CLARO, `PUBLIC_BASE_URL/mcp`; senão o verificador o
recusa como "de outro destino". O Keycloak não faz isso sozinho: ele ignora o parâmetro `resource` (RFC 8707) que os
aplicativos MCP mandam, e o recurso experimental `resource-indicators` fica desligado.

Por isso existe o escopo de cliente `mcp-claro`, com um mapeador `oidc-audience-mapper`
(`included.custom.audience`), padrão em todos os clientes que falam com o MCP-CLARO. O valor vem de
`MCP_CLARO_RECURSO` (padrão `https://mcp-claro.localhost/mcp`) e deve ser igual ao `MCP_RESOURCE_ID` do gateway. O
escopo `outro-servico` faz o mesmo para `https://outro-servico.localhost/api`, só no cliente de teste do outro
condomínio, que não pode pedir o escopo `mcp-claro`.

O `azp` do crachá diz qual aplicativo é; o verificador usa esse valor como `client_id`.

### Chaves da ronda e do vigia

No realm guardado, os dois vêm com a lista de chaves vazia (`jwks.string` = `{"keys":[]}`): sem chave cadastrada,
não entram. Na Fase 1, o ADMIN cadastra a chave **pública** de cada um pela API de administração do Keycloak
(atributo `jwks.string` do cliente). No rodízio, a lista leva a chave antiga e a nova ao mesmo tempo, cada uma com o
seu `kid`.

Escolhemos a lista guardada no Keycloak, e não uma URL de JWKS servida pelo programa. Com URL, o Keycloak teria de
alcançar o programa (o vigia roda em outra máquina) e quem controlasse aquele endereço trocaria a chave sem ninguém
ver. Com a lista, cada troca é um ato do ADMIN, registrado nos eventos de administração.

No teste automatizado, o próprio teste gera o par de chaves na hora. A chave privada não sai do processo do teste; só
a pública entra numa cópia do realm.

### Clientes de teste

`mcp-claro-teste-cc` e `outro-servico` ficam **desligados** se `MCP_CLARO_CLIENTES_DE_TESTE` não for `true`, e os
segredos vêm de `MCP_CLARO_TESTE_CC_SEGREDO` e `MCP_CLARO_OUTRO_SERVICO_SEGREDO`. Nenhum segredo fica no
repositório. Só o teste automatizado os liga, com segredos aleatórios a cada rodada. O Compose não os liga. Em piloto
ou produção, use um realm sem eles.

## Emissor e endereços

O emissor (`iss`) é fixo, dado por `KC_HOSTNAME`, e não depende de por onde o pedido chegou:

- Emissor (`IDP_ISSUER_URL`): `https://mcp-claro.localhost/login/realms/mcp-claro`, atrás do Caddy.
- JWKS para a entrada (`IDP_JWKS_URL`), pela rede interna: `http://login:8080/login/realms/mcp-claro/protocol/openid-connect/certs`.
- Saúde: porta de gerência 9000, `/health/ready`, que não passa pelo portão.

O serviço `login` está em [`docker/login/compose.login.yml`](../docker/login/compose.login.yml): `start-dev`
(desenvolvimento e teste), banco PostgreSQL `login` com TLS `verify-full`, redes `borda` e `interna`. A importação só
cria o realm se ele não existe; depois de mudar o JSON, apague o banco `login` em desenvolvimento.

## A prova

[`tests/integracao/test_keycloak.py`](../tests/integracao/test_keycloak.py) sobe o Keycloak real em Docker e usa o
`VerificadorCracha` do produto:

- crachá certo é aceito, com `client_id` igual ao `azp`, e vale 300 s;
- crachá do `outro-servico` é recusado pela audiência, embora tenha o mesmo emissor e a mesma assinatura válida;
- crachá alterado (carga ou assinatura) é recusado;
- a ronda entra com JWT assinado; o vigia, sem chave cadastrada, não;
- aplicativo público sem PKCE, com PKCE `plain` ou com retorno fora do loopback é recusado;
- cadastro anônimo de cliente é recusado.

## Logto como alternativa

O Logto 1.46.0 (MPL-2.0) segue como alternativa pela mesma bateria, trocando só o que sobe e como se pede o crachá:

- O recurso do MCP-CLARO vira um "recurso de API" com indicador `https://mcp-claro.localhost/mcp`, e um segundo
  recurso faz o papel do outro condomínio. No Logto a audiência vem do parâmetro `resource` do pedido, não de um
  mapeador.
- `claude-code` e `goose` viram aplicativos nativos com PKCE; o cliente de teste vira um aplicativo máquina a
  máquina.
- Gateway com `IDP_KIND=logto`, `IDP_ISSUER_URL` no formato `https://<host>/oidc` e `IDP_JWKS_URL` explícito (no
  Logto a lista fica em `/oidc/jwks`). O crachá do Logto traz `client_id`, que o verificador também aceita.
- Falta confirmar se o Logto aceita `private_key_jwt` para máquina a máquina. Sem isso, a ronda e o vigia teriam de
  usar segredo, que `PROGRAM_AUTH_METHODS` só aceita por decisão do ADMIN. É o motivo de o Keycloak ser o padrão.
