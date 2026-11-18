#!/usr/bin/env bash
# Roda a suite oficial de conformidade do MCP (versao FIXADA) contra o sistema de
# exemplo exemplos/conformidade.py, na versao 2026-07-28 do protocolo.
#
# Uso, a partir de qualquer pasta:  bash conformidade/rodar.sh
# Precisa de: uv (com o ambiente do projeto) e Node 20+ com npx.
# Variaveis opcionais:
#   CONFORMIDADE_PORTA   porta local do exemplo (padrao 9107)
#   CONFORMIDADE_ESPERA  segundos para o exemplo abrir a porta (padrao 60)
#
# Saida: conformidade/resultados/ (log do exemplo, saida da suite e o checks.json
# de cada cenario). O codigo de saida e o da suite: 0 quando todo cenario exigido
# passa ou falha so como previsto em conformidade/falhas-esperadas.yml.
set -euo pipefail

# Versao fixada de proposito: nunca "latest" (a tag latest aponta para 0.1.16,
# anterior a 2026-07-28). Trocar a versao e mudanca revisada.
readonly SUITE="@modelcontextprotocol/conformance@0.2.0-alpha.12"
readonly REQUISITOS="2026-07-28"

raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$raiz"

porta="${CONFORMIDADE_PORTA:-9107}"
espera="${CONFORMIDADE_ESPERA:-60}"
resultados="conformidade/resultados"
falhas="conformidade/falhas-esperadas.yml"
url="http://127.0.0.1:${porta}/mcp"

rm -rf "$resultados"
mkdir -p "$resultados/cenarios"

porta_aberta() {
  (exec 3<>"/dev/tcp/127.0.0.1/${porta}") 2>/dev/null
}

if porta_aberta; then
  echo "A porta ${porta} ja esta em uso; escolha outra com CONFORMIDADE_PORTA." >&2
  exit 2
fi

echo "Subindo o exemplo de conformidade em ${url}"
uv run --frozen python -m exemplos.conformidade --porta "$porta" >"$resultados/servidor.log" 2>&1 &
pid_servidor=$!

encerrar() {
  if kill -0 "$pid_servidor" 2>/dev/null; then
    kill "$pid_servidor" 2>/dev/null || true
    wait "$pid_servidor" 2>/dev/null || true
  fi
}
trap encerrar EXIT

for ((i = 0; i < espera * 4; i++)); do
  if porta_aberta; then
    break
  fi
  if ! kill -0 "$pid_servidor" 2>/dev/null; then
    echo "O exemplo parou antes de abrir a porta. Log:" >&2
    cat "$resultados/servidor.log" >&2
    exit 1
  fi
  sleep 0.25
done
if ! porta_aberta; then
  echo "O exemplo nao abriu a porta ${porta} em ${espera}s. Log:" >&2
  cat "$resultados/servidor.log" >&2
  exit 1
fi

echo "Rodando ${SUITE} com --requirements ${REQUISITOS}"
codigo=0
npx --yes "$SUITE" server \
  --url "$url" \
  --requirements "$REQUISITOS" \
  --expected-failures "$falhas" \
  --output-dir "$resultados/cenarios" 2>&1 | tee "$resultados/saida.txt" || codigo=$?

# O "Total" da suite soma tambem os cenarios nao pontuados (extensao tasks e
# pendentes). Este resumo conta so os exigidos, como a suite decide o codigo:
# aviso (WARNING) conta como falha. So informa; nao muda o codigo de saida.
npx --yes "$SUITE" list --requirements "$REQUISITOS" 2>/dev/null \
  | awk '/^Server scenarios/ {dentro = 1; next} /^$/ {dentro = 0} dentro && /^  - / {print $2}' \
    >"$resultados/exigidos.txt" || true
uv run --frozen python -I - "$resultados/exigidos.txt" "$resultados/cenarios" <<'PY' || true
import json, pathlib, re, sys

exigidos = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").split()
pastas = [p for p in pathlib.Path(sys.argv[2]).iterdir() if p.is_dir()]
ruins = []
for nome in exigidos:
    padrao = re.compile(rf"^server-{re.escape(nome)}-\d{{4}}-\d{{2}}-\d{{2}}T")
    achadas = [p for p in pastas if padrao.match(p.name)]
    checks = json.loads((achadas[0] / "checks.json").read_text(encoding="utf-8")) if achadas else []
    falhas = [c["id"] for c in checks if c.get("status") in ("FAILURE", "WARNING")]
    if not checks or falhas:
        ruins.append(f"{nome}: {', '.join(falhas) or 'sem resultado'}")
print(f"Cenarios de servidor exigidos: {len(exigidos) - len(ruins)} de {len(exigidos)} passaram.")
for linha in ruins:
    print(f"  falhou: {linha}")
PY

if [[ "$codigo" -eq 0 ]]; then
  echo "Conformidade ${REQUISITOS}: suite passou (resultados em ${resultados}/)."
else
  echo "Conformidade ${REQUISITOS}: suite falhou com codigo ${codigo} (veja ${resultados}/saida.txt)." >&2
fi
exit "$codigo"
