# Imagem de teste separada: sistema que so fala a geracao antiga (kit mcp 1.30.0).
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then export PIP_CERT=/run/secrets/ca_corporativa; fi; \
    pip install --no-cache-dir mcp==1.30.0
COPY servidor.py /app/servidor.py
USER 10001
EXPOSE 9105
CMD ["python", "/app/servidor.py"]
