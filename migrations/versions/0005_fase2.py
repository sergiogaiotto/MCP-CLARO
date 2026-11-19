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
CREATE INDEX operacoes_aplicativo ON operacoes (aplicativo_id, criada_em DESC);

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
  -- Como a pessoa confirmou: so na sessao, com a passkey, ou refazendo o login (passkey desligada).
  confirmada_com     text        CHECK (confirmada_com IN ('sessao', 'passkey', 'login_refeito')),
  CHECK ((decisao IS NULL) = (decidida_em IS NULL)),
  CHECK ((decisao IS NOT DISTINCT FROM 'confirmada') = (confirmada_com IS NOT NULL)),
  CHECK (confirmada_com IS DISTINCT FROM 'passkey' OR com_passkey)
);
CREATE INDEX fichas_pendentes ON fichas (expira_em) WHERE decisao IS NULL;
-- Teto de fichas por hora de cada aplicativo (CONFIRM_CAP_PER_HOUR): so a ultima hora e lida.
CREATE INDEX fichas_montada ON fichas (montada_em);

-- A decisao da ficha vale uma vez: nasce vazia, muda de vazia para um valor e nao muda mais. As rotinas so
-- vencem fichas. Ficha de risco alto (ou que pede passkey) so confirma com passkey ou com o login refeito.
CREATE FUNCTION fichas_guarda() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.decisao IS NOT NULL OR NEW.decidida_em IS NOT NULL OR NEW.decidida_por IS NOT NULL OR NEW.com_passkey
       OR NEW.confirmada_com IS NOT NULL OR NEW.segundos_ate_decidir IS NOT NULL THEN
      RAISE EXCEPTION 'Uma ficha nasce sem decisao' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.operacao_id, NEW.codigo_conferencia, NEW.risco, NEW.exige_passkey)
     IS DISTINCT FROM ROW(OLD.operacao_id, OLD.codigo_conferencia, OLD.risco, OLD.exige_passkey) THEN
    RAISE EXCEPTION 'A operacao, o codigo, o risco e a passkey de uma ficha nao mudam'
      USING ERRCODE = 'check_violation';
  END IF;
  IF ROW(NEW.decisao, NEW.decidida_em, NEW.decidida_por, NEW.com_passkey, NEW.segundos_ate_decidir,
         NEW.confirmada_com)
     IS DISTINCT FROM ROW(OLD.decisao, OLD.decidida_em, OLD.decidida_por, OLD.com_passkey, OLD.segundos_ate_decidir,
         OLD.confirmada_com) THEN
    IF OLD.decisao IS NOT NULL THEN
      RAISE EXCEPTION 'A ficha ja foi %; a decisao nao muda', OLD.decisao USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.decisao IS NULL THEN
      RAISE EXCEPTION 'Os campos da decisao so mudam junto com a decisao' USING ERRCODE = 'check_violation';
    END IF;
    IF current_user = 'mcp_claro_rotinas' AND NEW.decisao <> 'vencida' THEN
      RAISE EXCEPTION 'As rotinas so vencem fichas' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.decisao = 'confirmada' AND (NEW.exige_passkey OR NEW.risco = 'alto')
       AND coalesce(NEW.confirmada_com, '') NOT IN ('passkey', 'login_refeito') THEN
      RAISE EXCEPTION 'Ficha de risco alto so confirma com a passkey ou com o login refeito'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER fichas_guarda BEFORE INSERT OR UPDATE ON fichas FOR EACH ROW EXECUTE FUNCTION fichas_guarda();

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
-- Varreduras da fila (retomada, restauracao, bloqueio, retrato do Grafana) e a janela do comprovante.
CREATE INDEX fila_em_aberto ON fila_saida (estado)
  WHERE estado IN ('aguardando_ficha', 'liberada', 'enviando', 'nao_confirmada');
CREATE INDEX fila_mudou ON fila_saida (mudou_em);

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
  -- De um estado final nada mais muda: nem o estado, nem a hora, nem quem mudou.
  IF OLD.estado IN ('enviada', 'nao_saiu', 'sistema_pediu_dados', 'nao_confirmada', 'vencida', 'cancelada',
                    'parada_por_bloqueio', 'parada_por_restauracao') THEN
    RAISE EXCEPTION 'A acao ja terminou (%); a linha da fila nao muda mais', OLD.estado
      USING ERRCODE = 'check_violation';
  END IF;
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
  IF NEW.ficha_id IS DISTINCT FROM OLD.ficha_id OR NEW.limite_id IS DISTINCT FROM OLD.limite_id THEN
    RAISE EXCEPTION 'A ficha e o limite de uma linha da fila nao mudam' USING ERRCODE = 'check_violation';
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
CREATE TRIGGER fila_saida_sem_truncate BEFORE TRUNCATE ON fila_saida
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

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

-- Comprovante de bloqueio em tres partes: o que paramos a tempo, o que ja tinha seguido e o que ainda esta
-- em conferencia. "informado" fica para o que um sistema de nivel 3 relatar (Fase 4); vazio na Fase 2.
CREATE TABLE comprovantes (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  bloqueio_id     uuid        NOT NULL UNIQUE REFERENCES bloqueios (id),
  paradas         jsonb       NOT NULL DEFAULT '[]'::jsonb,
  ja_saidas       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  em_conferencia  jsonb       NOT NULL DEFAULT '[]'::jsonb,
  informado       jsonb       NOT NULL DEFAULT '[]'::jsonb,
  criado_em       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER comprovantes_sem_update_delete BEFORE UPDATE OR DELETE ON comprovantes
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();

-- Programas que rodam sozinhos (decisao 9): pedidos de cada programa por minuto (PROGRAM_MAX_REQUESTS_PER_MINUTE).
CREATE TABLE programas_pedidos_minuto (
  aplicativo_id uuid        NOT NULL REFERENCES aplicativos (id),
  minuto        timestamptz NOT NULL,
  contagem      integer     NOT NULL DEFAULT 0 CHECK (contagem >= 0),
  PRIMARY KEY (aplicativo_id, minuto)
);
CREATE TRIGGER comprovantes_sem_truncate BEFORE TRUNCATE ON comprovantes
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();
"""

# Medicoes da Fase 2 para o Grafana: so contagens, nunca protocolo, argumento nem pessoa.
VISOES = r"""
CREATE VIEW medicoes.v_acoes AS
  SELECT minuto, coalesce(rotulos ->> 'estado', 'sem_estado') AS estado,
         coalesce(rotulos ->> 'sistema', 'sem_sistema') AS sistema, valor AS contagem
  FROM medicoes.minuto WHERE nome = 'acoes';
COMMENT ON VIEW medicoes.v_acoes IS 'Fim do envio de cada acao, por minuto, estado e sistema.';

CREATE VIEW medicoes.v_acoes_pedidas AS
  SELECT minuto, coalesce(rotulos ->> 'rota', 'sem_rota') AS rota, valor AS contagem
  FROM medicoes.minuto WHERE nome = 'acoes_pedidas';
COMMENT ON VIEW medicoes.v_acoes_pedidas IS 'Acoes pedidas por minuto e caminho (ficha, limite, risco alto, experiencia).';

CREATE VIEW medicoes.v_acoes_paradas AS
  SELECT minuto, coalesce(rotulos ->> 'motivo', 'sem_motivo') AS motivo, valor AS contagem
  FROM medicoes.minuto WHERE nome = 'acoes_paradas';
COMMENT ON VIEW medicoes.v_acoes_paradas IS 'Acoes paradas pelo despachante antes de sair, por minuto e motivo.';

-- Retrato da fila agora: o que espera a pessoa, o que espera a hora e o que esta em duvida sem decisao.
CREATE VIEW medicoes.v_fila_agora AS
  SELECT f.estado, count(*) AS acoes, min(f.mudou_em) AS desde
  FROM public.fila_saida f
  WHERE f.estado IN ('aguardando_ficha', 'liberada', 'enviando')
     OR (f.estado = 'nao_confirmada'
         AND NOT EXISTS (SELECT 1 FROM public.decisoes_nao_confirmada d WHERE d.operacao_id = f.operacao_id))
  GROUP BY f.estado;
COMMENT ON VIEW medicoes.v_fila_agora IS
  'Fila de saida agora: aguardando ficha, liberada, enviando e nao confirmada ainda sem decisao da pessoa.';

GRANT SELECT ON medicoes.v_acoes, medicoes.v_acoes_pedidas, medicoes.v_acoes_paradas, medicoes.v_fila_agora
  TO grafana_leitor;
"""

GRANTS = r"""
GRANT SELECT, INSERT, UPDATE (contagem) ON programas_pedidos_minuto TO mcp_claro_entrada;
GRANT SELECT, DELETE ON programas_pedidos_minuto TO mcp_claro_rotinas;
GRANT USAGE ON SEQUENCE protocolo_seq TO mcp_claro_entrada;
-- Na restauracao, o despachante adianta a sequencia: numero dado antes da copia nao se repete.
GRANT SELECT, UPDATE ON SEQUENCE protocolo_seq TO mcp_claro_despachante;
GRANT SELECT, INSERT ON operacoes, fichas, fila_saida TO mcp_claro_entrada;
GRANT SELECT ON transicoes_saida TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
GRANT UPDATE (decisao, decidida_em, decidida_por, com_passkey, segundos_ate_decidir, confirmada_com)
  ON fichas TO mcp_claro_entrada;
GRANT UPDATE (estado, hora_saida, mudou_por) ON fila_saida TO mcp_claro_entrada;
GRANT SELECT, UPDATE (estado, mudou_por, enviada_em) ON fila_saida TO mcp_claro_despachante;
GRANT SELECT, UPDATE (resultado, desfecho) ON operacoes TO mcp_claro_despachante;
GRANT SELECT ON fichas, limites TO mcp_claro_despachante;
GRANT SELECT, INSERT, UPDATE (usados_hoje, dia, situacao, motivo, por_dia, valido_ate, com_passkey)
  ON limites TO mcp_claro_entrada;
GRANT SELECT ON operacoes, fichas, fila_saida, limites TO mcp_claro_rotinas;
GRANT UPDATE (estado, mudou_por) ON fila_saida TO mcp_claro_rotinas;
GRANT UPDATE (decisao, decidida_em) ON fichas TO mcp_claro_rotinas;
GRANT UPDATE (situacao, motivo) ON limites TO mcp_claro_rotinas;
GRANT SELECT, INSERT ON decisoes_nao_confirmada TO mcp_claro_entrada;
GRANT SELECT ON decisoes_nao_confirmada TO mcp_claro_despachante, mcp_claro_rotinas;
GRANT SELECT, INSERT, UPDATE (contador, ultimo_uso, ativa, apelido) ON passkeys TO mcp_claro_entrada;
GRANT SELECT, INSERT, UPDATE (usado_em) ON desafios_passkey TO mcp_claro_entrada;
-- Limpeza dos desafios vencidos ou usados (rotinas): o DELETE com WHERE precisa ler essas colunas.
GRANT SELECT (desafio, expira_em, usado_em), DELETE ON desafios_passkey TO mcp_claro_rotinas;
GRANT SELECT, INSERT ON comprovantes TO mcp_claro_entrada;
GRANT SELECT, INSERT ON comprovantes TO mcp_claro_rotinas;
GRANT UPDATE (avisada_em) ON fichas TO mcp_claro_rotinas;
GRANT UPDATE (desfecho) ON operacoes TO mcp_claro_entrada, mcp_claro_rotinas;
"""

CAMPOS_DESTINATARIO = r"""
-- Segunda rodada: campos de destinatario. O ADMIN marca, na revisao da ferramenta, quais campos do pedido dizem
-- para quem a acao vai (para, copia, copia oculta...). O limite "so estes destinatarios" confere estes campos;
-- o resto do pedido fica livre. Vazio: o ADMIN nao marcou nenhum, e o limite so vale para qualquer destinatario.
-- GRANTs: os de 0002 sao da tabela inteira e valem para a coluna nova. Entrada, despachante e rotinas a leem
-- (GRANT SELECT ON ferramentas_revisadas); a entrada a grava ao revisar (GRANT INSERT ON ferramentas_revisadas).
-- Ninguem a muda depois (o UPDATE da entrada e das rotinas so cobre situacao e motivo): mudar e revisar de novo.
ALTER TABLE ferramentas_revisadas ADD COLUMN campos_destinatario text[] NOT NULL DEFAULT '{}'
  CHECK (cardinality(campos_destinatario) <= 30 AND array_position(campos_destinatario, NULL) IS NULL);
-- Fim da segunda rodada: campos de destinatario.
"""

CONTINUIDADE_RODADA2 = r"""
-- Segunda rodada: continuidade. A marca "restaurado" (roteiro de restauracao) tambem adianta o numero de
-- protocolo, antes de a entrada subir: um numero dado depois da copia, e perdido com ela, nao e dado de novo a
-- outra acao. O salto e o mesmo da conferencia na partida do despachante (fila.SALTO_NA_RESTAURACAO, 1000000;
-- o teste confere). Dois saltos (marca e conferencia) so deixam um buraco maior: a sequencia so anda para frente.
-- Mesmo corpo da 0004 no resto; o downgrade desta migracao nao existe (o MCP-CLARO nao desfaz migracoes).
CREATE OR REPLACE FUNCTION continuidade_marcar_restaurado(p_quem text, p_motivo text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  atual record;
  novo  bigint;
BEGIN
  IF coalesce(btrim(p_quem), '') = '' OR coalesce(btrim(p_motivo), '') = '' THEN
    RAISE EXCEPTION 'Diga quem marcou e o motivo da marca restaurado' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT * INTO atual FROM continuidade_controle();
  INSERT INTO continuidade_banco (system_identifier, timeline_id, ultimo_fechamento, restaurado, motivo,
                                  origem, desfecho, quem)
  VALUES (atual.system_identifier, atual.timeline_id, (SELECT max(numero) FROM fechamentos), true,
          left(btrim(p_motivo), 500), 'roteiro', 'marca_restaurado', left(btrim(p_quem), 200))
  RETURNING id INTO novo;
  PERFORM setval('protocolo_seq', (SELECT last_value FROM protocolo_seq) + 1000000);
  -- Mesma frase de mcp_claro.extrato.frases ("restauracao_marcada"); o teste confere.
  PERFORM extrato_registrar('roteiro', 'restauracao_marcada',
    'O roteiro de restauracao marcou o banco como restaurado; a porta de saida fecha na proxima partida.',
    NULL, NULL, NULL, NULL, NULL, jsonb_build_object('continuidade_id', novo), NULL);
  RETURN novo;
END $$;
REVOKE ALL ON FUNCTION continuidade_marcar_restaurado(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION continuidade_marcar_restaurado(text, text) TO mcp_claro_despachante;
-- Fim da segunda rodada: continuidade.
"""

COFRE_RODADA3 = r"""
-- Terceira rodada: cofre e porta de saida. Com a saida fechada, a conferencia do cofre (e quem fecha de novo a
-- saida ja fechada) procura no extrato, desde o fechamento, os avisos do cofre e a linha do proprio fechamento,
-- com a trava exclusiva de "tudo". Este indice parcial deixa a busca passar so por essas linhas, e nao pelo
-- extrato inteiro do periodo. O indice do pai vale para as particoes de agora e para as criadas depois.
CREATE INDEX extrato_saida_cofre_idx ON extrato (criado_em, id)
  WHERE tipo IN ('cofre_trancado_saida_fechada', 'cofre_destravado_saida_fechada', 'estado_saida_mudou');
-- Fim da terceira rodada: cofre e porta de saida.
"""


def upgrade() -> None:
    sql_em_bloco(SQL)
    sql_em_bloco(GRANTS)
    sql_em_bloco(VISOES)
    sql_em_bloco(CAMPOS_DESTINATARIO)  # Segunda rodada: campos de destinatario
    sql_em_bloco(CONTINUIDADE_RODADA2)  # Segunda rodada: continuidade
    sql_em_bloco(COFRE_RODADA3)  # Terceira rodada: cofre e porta de saida


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
