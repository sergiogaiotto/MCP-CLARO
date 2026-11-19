# PostgreSQL do MCP-CLARO: imagem oficial com pgBackRest 2.60.1 do repositorio apt do PGDG.
# O pgBackRest fica nos dois lados (aqui e no conteiner de backup, docker/backup), na mesma versao.
FROM postgres:18.7@sha256:2ea8630df176ad090ec92136dee850397c5af8781e4bcf2e4162593691c61bbc
ARG PGBACKREST_VERSAO=2.60.1
RUN --mount=type=secret,id=ca_corporativa,required=false \
    if [ -s /run/secrets/ca_corporativa ]; then \
      printf 'Acquire::https::CaInfo "/run/secrets/ca_corporativa";\n' > /etc/apt/apt.conf.d/99ca; fi; \
    apt-get update \
 && apt-get install -y --no-install-recommends "pgbackrest=${PGBACKREST_VERSAO}*" \
 && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99ca /etc/pgbackrest.conf \
 && pgbackrest version | grep -qx "pgBackRest ${PGBACKREST_VERSAO}"
COPY iniciar/ /docker-entrypoint-initdb.d/
COPY pgbackrest.conf /etc/pgbackrest/pgbackrest.conf
COPY com-tls.sh preparar-tls.sh pgbackrest-banco.sh restaurar.sh /usr/local/bin/
COPY pg_hba.conf /etc/postgresql/pg_hba.conf
RUN chmod 0644 /etc/pgbackrest/pgbackrest.conf \
 && chmod 0755 /usr/local/bin/com-tls.sh /usr/local/bin/preparar-tls.sh /usr/local/bin/pgbackrest-banco.sh \
      /usr/local/bin/restaurar.sh
ENTRYPOINT ["/usr/local/bin/com-tls.sh"]
CMD ["postgres", "-c", "hba_file=/etc/postgresql/pg_hba.conf"]
