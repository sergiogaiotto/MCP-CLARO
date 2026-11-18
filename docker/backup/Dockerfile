# Conteiner de backup do MCP-CLARO: pgBackRest 2.60.1 (o mesmo da imagem do banco, do repositorio apt
# do PGDG) e as ferramentas do PostgreSQL 18 para a copia logica (pg_dump) e o ensaio de restauracao.
# Base fixada por digest, a mesma do banco. Roda como o usuario postgres, sem nada de root.
FROM postgres:18.7@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc
ARG PGBACKREST_VERSAO=2.60.1
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then \
      printf 'Acquire::https::CaInfo "/run/secrets/ca_corporativa";\n' > /etc/apt/apt.conf.d/99ca; fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends "pgbackrest=${PGBACKREST_VERSAO}*" curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99ca /etc/pgbackrest.conf \
 && pgbackrest version | grep -qx "pgBackRest ${PGBACKREST_VERSAO}" \
 && install -d -o postgres -g postgres -m 700 /var/lib/pgbackrest /var/lib/mcp-claro-backup
COPY pgbackrest.conf /etc/pgbackrest/pgbackrest.conf
COPY backup.sh logico.sh restaurar-logico.sh /usr/local/bin/
RUN chmod 0644 /etc/pgbackrest/pgbackrest.conf \
 && chmod 0755 /usr/local/bin/backup.sh /usr/local/bin/logico.sh /usr/local/bin/restaurar-logico.sh
USER postgres
WORKDIR /var/lib/mcp-claro-backup
EXPOSE 8432
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["servir"]
HEALTHCHECK --interval=5m --timeout=60s --start-period=2h CMD ["/usr/local/bin/backup.sh", "saude"]
