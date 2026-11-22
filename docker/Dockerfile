# Imagem unica do MCP-CLARO, com cinco papeis: entrada, despachante, rotinas, vigia e visitas.
# Base fixada por digest. Dependencias pelo uv.lock. Sem FastMCP nem kit 1.x aqui.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS construcao
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# CA corporativa opcional (proxy com inspecao de TLS): docker build --secret id=ca_corporativa,src=ca.pem
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then export PIP_CERT=/run/secrets/ca_corporativa; fi; \
    pip install --no-cache-dir uv==0.13.9
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then export SSL_CERT_FILE=/run/secrets/ca_corporativa UV_NATIVE_TLS=1; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then export SSL_CERT_FILE=/run/secrets/ca_corporativa UV_NATIVE_TLS=1; fi; \
    uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
LABEL org.opencontainers.image.title="mcp-claro" \
      org.opencontainers.image.description="MCP-CLARO, o gateway MCP independente (MCP 2026-07-28)"
# Correcoes de seguranca do Debian e sem pip na imagem final (o produto nao instala nada em tempo de execucao).
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then \
      printf 'Acquire::https::CaInfo "/run/secrets/ca_corporativa";\n' > /etc/apt/apt.conf.d/99ca; fi; \
    apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99ca \
 && python -m pip uninstall -y pip \
 && groupadd --system --gid 10001 mcp && useradd --system --uid 10001 --gid mcp --no-create-home mcp
WORKDIR /app
COPY --from=construcao /app/.venv /app/.venv
COPY --from=construcao /app/src /app/src
COPY alembic.ini alembic_vigia.ini ./
COPY migrations ./migrations
COPY migrations_vigia ./migrations_vigia
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SSL_CERT_FILE=
USER mcp:mcp
ENTRYPOINT ["mcp-claro"]
CMD ["entrada"]
