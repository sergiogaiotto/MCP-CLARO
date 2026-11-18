"""Fase 2: operacoes, fichas, porta unica de saida, limites, passkeys, comprovantes e decisoes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL = r"""
-- Numero de protocolo legivel: CLR-AAAA-NNNNNNNN, sequencial por instalacao.
CREATE SEQUENCE protocolo_seq;

-- Uma operacao por acao pedida. O codigo de conferencia e o hash do que vai seguir (ferramenta e argumentos
-- canonicos): a ficha mostra o mesmo codigo, e o despachante confere antes de enviar.
CREATE TABLE operacoes (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  protocolo           text        NOT NULL UNIQUE,
  codigo_conferencia  text        NOT NULL,
  pessoa_id           uuid        NOT NULL REFERENCES pessoas (id),
  aplicativo_id       uuid        NOT NULL REFERENCES aplicativos (id),
  ordem_id            uuid        NOT NULL REFERENCES ordens_servico (id),
  ferramenta_id       uuid        NOT NULL REFERENCES ferramentas_revisadas (id),
  sistema_id          uuid        NOT NULL REFERENCES sistemas (id),
  argumentos          jsonb       NOT NULL,
  aplicativo_diz      text,
  chave_pedido_igual  text        NOT NULL,
  resultado           jsonb,
  desfecho            text,
  criada_em           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operacoes_pedido_igual ON operacoes (chave_pedido_igual, criada_em DESC);
CREATE INDEX operacoes_pessoa ON operacoes (pessoa_id, criada_em DESC);

-- Fichas: montadas pelo gateway, decididas pela pessoa na tela. Valem uma vez, so para aquela operacao.
CREATE TABLE fichas (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  operacao_id        uuid        NOT NULL UNIQUE REFERENCES operacoes (id),
  codigo_conferencia text        NOT NULL,
  montada_em         timestamptz NOT NULL DEFAULT now(),
  expira_em          timestamptz NOT NULL,
  risco              text        NOT NULL,
  exige_passkey      boolean     NOT NULL DEFAULT false,
  decisao            text        CHECK (decisao IN ('confirmada', 'recusada', 'vencida')),
  decidida_em        timestamptz,
  decidida_por       uuid        REFERENCES pessoas (id),
  com_passkey        boolean     NOT NULL DEFAULT false,
  segundos_ate_decidir numeric,
  avisada_em         timestamptz,
  CHECK ((decisao IS NULL) = (decidida_em IS NULL))
);
CREATE INDEX fichas_pendentes ON fichas (expira_em) WHERE decisao IS NULL;

-- Limites por aproximacao: ferramenta, destinatarios, quantidade e prazo, criados ou ampliados com passkey.
CREATE TABLE limites (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  pessoa_id      uuid        NOT NULL REFERENCES pessoas (id),
  aplicativo_id  uuid        NOT NULL REFERENCES aplicativos (id),
  sistema_id     uuid        NOT NULL REFERENCES sistemas (id),
  nome_original  text        NOT NULL,
  destinatarios  jsonb       NOT NULL DEFAULT '[]'::jsonb,
  por_dia        integer     NOT NULL CHECK (por_dia > 0),
  usados_hoje    integer     NOT NULL DEFAULT 0 CHECK (usados_hoje >= 0),
  dia            date        NOT NULL DEFAULT current_date,
  valido_ate     timestamptz NOT NULL,
  com_passkey    boolean     NOT NULL DEFAULT false,
  situacao       text        NOT NULL DEFAULT 'ativo' CHECK (situacao IN ('ativo', 'suspenso', 'encerrado', 'vencido')),
  motivo         text,
  criado_em      timestamptz NOT NULL DEFAULT now(),
  CHECK (usados_hoje <= por_dia)
);
CREATE INDEX limites_ativos ON limites (pessoa_id, aplicativo_id) WHERE situacao = 'ativo';

-- Porta unica de saida: uma linha por acao; cada mudanca exige o estado anterior.
CREATE TABLE fila_saida (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  operacao_id   uuid        NOT NULL UNIQUE REFERENCES operacoes (id),
  estado        text        NOT NULL DEFAULT 'aguardando_ficha' CHECK (estado IN (
                  'aguardando_ficha', 'liberada', 'enviando', 'enviada', 'nao_saiu', 'sistema_pediu_dados',
                  'nao_confirmada', 'vencida', 'cancelada', 'parada_por_bloqueio', 'parada_por_restauracao')),
  ficha_id      uuid        UNIQUE REFERENCES fichas (id),
  limite_id     uuid        REFERENCES limites (id),
  hora_saida    timestamptz,
  mudou_em      timestamptz NOT NULL DEFAULT now(),
  mudou_por     text        NOT NULL DEFAULT 'entrada',
  enviada_em    timestamptz,
  CHECK (estado <> 'liberada' OR hora_saida IS NOT NULL),
  CHECK (estado NOT IN ('liberada', 'enviando', 'enviada', 'nao_saiu', 'sistema_pediu_dados', 'nao_confirmada')
         OR ficha_id IS NOT NULL OR limite_id IS NOT NULL)
);
CREATE INDEX fila_liberada ON fila_saida (hora_saida) WHERE estado = 'liberada';

-- Transicoes permitidas: a mesma tabela do modulo saida, guardada no banco como segunda barreira.
CREATE TABLE transicoes_saida (
  de   text NOT NULL,
  para text NOT NULL,
  PRIMARY KEY (de, para)
);
INSERT INTO transicoes_saida (de, para) VALUES
  ('aguardando_ficha', 'liberada'), ('aguardando_ficha', 'vencida'), ('aguardando_ficha', 'cancelada'),
  ('aguardando_ficha', 'parada_por_bloqueio'),
  ('liberada', 'enviando'), ('liberada', 'cancelada'), ('liberada', 'parada_por_bloqueio'),
  ('enviando', 'enviada'), ('enviando', 'nao_saiu'), ('enviando', 'sistema_pediu_dados'),
  ('enviando', 'nao_confirmada'),
  ('aguardando_ficha', 'parada_por_restauracao'), ('liberada', 'parada_por_restauracao');

CREATE FUNCTION fila_saida_guarda() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.estado IS DISTINCT FROM OLD.estado THEN
    IF NOT EXISTS (SELECT 1 FROM transicoes_saida WHERE de = OLD.estado AND para = NEW.estado) THEN
      RAISE EXCEPTION 'Mudanca de estado recusada na porta de saida: % -> %', OLD.estado, NEW.estado
        USING ERRCODE = 'check_violation';
    END IF;
    -- Enviar e so do despachante: so ele marca "enviando", grava o fim do envio e para por restauracao.
    IF (NEW.estado IN ('enviando', 'parada_por_restauracao') OR OLD.estado = 'enviando')
       AND current_user <> 'mcp_claro_despachante'
       AND current_user <> (SELECT relowner::regrole::text FROM pg_class WHERE oid = TG_RELID) THEN
      RAISE EXCEPTION 'So o despachante muda uma acao para %, ou a partir de enviando', NEW.estado
        USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.mudou_em := now();
  END IF;
  IF NEW.operacao_id IS DISTINCT FROM OLD.operacao_id THEN
    RAISE EXCEPTION 'A operacao de uma linha da fila nao muda' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fila_saida_guarda BEFORE UPDATE ON fila_saida FOR EACH ROW EXECUTE FUNCTION fila_saida_guarda();

-- Toda linha nasce esperando a ficha, ou liberada por um limite ja gasto; nunca ja saida.
CREATE FUNCTION fila_saida_nasce() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT (NEW.estado = 'aguardando_ficha' AND NEW.ficha_id IS NOT NULL AND NEW.limite_id IS NULL)
     AND NOT (NEW.estado = 'liberada' AND NEW.limite_id IS NOT NULL AND NEW.ficha_id IS NULL) THEN
    RAISE EXCEPTION 'Uma acao entra na fila esperando a ficha ou liberada por um limite'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fila_saida_nasce BEFORE INSERT ON fila_saida FOR EACH ROW EXECUTE FUNCTION fila_saida_nasce();
CREATE TRIGGER fila_saida_sem_delete BEFORE DELETE ON fila_saida FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();

-- Acorda o despachante quando uma linha fica liberada (LISTEN/NOTIFY), alem da varredura de reserva.
CREATE FUNCTION fila_saida_avisa() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.estado = 'liberada' THEN
    PERFORM pg_notify('mcp_claro_saida', NEW.id::text);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fila_saida_avisa AFTER INSERT OR UPDATE OF estado ON fila_saida
  FOR EACH ROW EXECUTE FUNCTION fila_saida_avisa();

-- "Nao confirmada": muda so pela decisao registrada da pessoa; a linha da fila nunca e reenviada.
CREATE TABLE decisoes_nao_confirmada (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  operacao_id  uuid        NOT NULL UNIQUE REFERENCES operacoes (id),
  decisao      text        NOT NULL CHECK (decisao IN ('aconteceu', 'nao_aconteceu', 'nao_sei')),
  decidida_por uuid        NOT NULL REFERENCES pessoas (id),
  decidida_em  timestamptz NOT NULL DEFAULT now(),
  observacao   text
);

-- Passkeys (WebAuthn) das pessoas.
CREATE TABLE passkeys (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  pessoa_id        uuid        NOT NULL REFERENCES pessoas (id),
  credencial_id    bytea       NOT NULL UNIQUE,
  chave_publica    bytea       NOT NULL,
  contador         bigint      NOT NULL DEFAULT 0,
  apelido          text,
  criada_em        timestamptz NOT NULL DEFAULT now(),
  ultimo_uso       timestamptz,
  ativa            boolean     NOT NULL DEFAULT true
);
CREATE TABLE desafios_passkey (
  desafio      bytea       PRIMARY KEY,
  pessoa_id    uuid        NOT NULL REFERENCES pessoas (id),
  finalidade   text        NOT NULL,
  alvo         text,
  criado_em    timestamptz NOT NULL DEFAULT now(),
  expira_em    timestamptz NOT NULL,
  usado_em     timestamptz
);

-- Comprovante de bloqueio em tres partes: o que parou, o que ja tinha saido, o que o sistema informa.
CREATE TABLE comprovantes (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  bloqueio_id     uuid        NOT NULL UNIQUE REFERENCES bloqueios (id),
  paradas         jsonb       NOT NULL DEFAULT '[]'::jsonb,
  ja_saidas       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  informado       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  criado_em       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER comprovantes_sem_update_delete BEFORE UPDATE OR DELETE ON comprovantes
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
"""

GRANTS = r"""
GRANT USAGE ON SEQUENCE protocolo_seq TO mcp_claro_entrada;
GRANT SELECT, INSERT ON operacoes, fichas, fila_saida TO mcp_claro_entrada;
GRANT SELECT ON transicoes_saida TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
GRANT UPDATE (decisao, decidida_em, decidida_por, com_passkey, segundos_ate_decidir) ON fichas TO mcp_claro_entrada;
GRANT UPDATE (estado, hora_saida, ficha_id, limite_id, mudou_por) ON fila_saida TO mcp_claro_entrada;
GRANT SELECT, UPDATE (estado, mudou_por, enviada_em) ON fila_saida TO mcp_claro_despachante;
GRANT SELECT, UPDATE (resultado, desfecho) ON operacoes TO mcp_claro_despachante;
GRANT SELECT ON fichas, limites TO mcp_claro_despachante;
GRANT SELECT, INSERT, UPDATE (usados_hoje, dia, situacao, motivo, por_dia, valido_ate, destinatarios, com_passkey)
  ON limites TO mcp_claro_entrada;
GRANT SELECT ON operacoes, fichas, fila_saida, limites TO mcp_claro_rotinas;
GRANT SELECT, UPDATE (estado, mudou_por) ON fila_saida TO mcp_claro_despachante;
GRANT UPDATE (estado, mudou_por) ON fila_saida TO mcp_claro_rotinas;
GRANT UPDATE (decisao, decidida_em) ON fichas TO mcp_claro_rotinas;
GRANT UPDATE (situacao, motivo) ON limites TO mcp_claro_rotinas;
GRANT SELECT, INSERT ON decisoes_nao_confirmada TO mcp_claro_entrada;
GRANT SELECT ON decisoes_nao_confirmada TO mcp_claro_despachante, mcp_claro_rotinas;
GRANT SELECT, INSERT, UPDATE (contador, ultimo_uso, ativa, apelido) ON passkeys TO mcp_claro_entrada;
GRANT SELECT, INSERT, UPDATE (usado_em) ON desafios_passkey TO mcp_claro_entrada;
GRANT DELETE ON desafios_passkey TO mcp_claro_rotinas;
GRANT SELECT, INSERT ON comprovantes TO mcp_claro_entrada, mcp_claro_despachante;
GRANT SELECT, INSERT ON comprovantes TO mcp_claro_rotinas;
GRANT UPDATE (avisada_em) ON fichas TO mcp_claro_rotinas;
GRANT UPDATE (desfecho) ON operacoes TO mcp_claro_entrada, mcp_claro_rotinas;
"""


def upgrade() -> None:
    sql_em_bloco(SQL)
    sql_em_bloco(GRANTS)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
