-- Open Reachout initial schema (engineering spec section 5).
-- M0: applied via `psql -f`; Alembic migrations take over in M1.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tenants (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        text NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS config_versions (
  hash        text PRIMARY KEY,
  tenant_id   uuid REFERENCES tenants(id),
  kind        text NOT NULL,               -- brief|program|prompt_pack
  content     jsonb NOT NULL,
  generated_by text,                       -- synthesis provenance (FR-0.4)
  pinned      boolean NOT NULL DEFAULT false,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entities (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id),
  display_name text,
  last_campaign_contact_at timestamptz,
  active_sequence_touch_id uuid,
  touches_12mo int NOT NULL DEFAULT 0,
  status       text NOT NULL DEFAULT 'active',
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entity_keys (
  entity_id  uuid NOT NULL REFERENCES entities(id),
  key_type   text NOT NULL,
  key_value  text NOT NULL,
  UNIQUE (key_type, key_value)
);

CREATE TABLE IF NOT EXISTS prospects (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id),
  entity_id       uuid NOT NULL REFERENCES entities(id),
  cohort_id       text NOT NULL,
  persona_id      text NOT NULL,
  state           text NOT NULL,
  email_raw       text,
  email_canonical text,
  email_confidence text,
  source_adapter  text NOT NULL,
  source_ref      jsonb NOT NULL DEFAULT '{}'::jsonb,
  data_basis      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cohort_id, entity_id)
);

CREATE TABLE IF NOT EXISTS evidence_facts (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id uuid NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
  fact_type   text NOT NULL,
  content     jsonb NOT NULL,
  source_url  text NOT NULL,
  observed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS touches (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id  uuid NOT NULL REFERENCES prospects(id),
  campaign_id  text NOT NULL,
  variant_id   text,
  step_index   int NOT NULL DEFAULT 0 CHECK (step_index <= 3),
  kind         text NOT NULL,
  status       text NOT NULL,
  subject      text,
  body         text,
  scrubbed     boolean NOT NULL DEFAULT false,
  content_hash text NOT NULL,
  claimed_at   timestamptz,
  sent_at      timestamptz,
  provider_ref jsonb,
  idempotency_key text UNIQUE
);

CREATE TABLE IF NOT EXISTS decision_traces (
  touch_id           uuid PRIMARY KEY REFERENCES touches(id),
  evidence_fact_ids  uuid[] NOT NULL DEFAULT '{}',
  claims             jsonb NOT NULL DEFAULT '{}'::jsonb,
  variables_resolved jsonb NOT NULL DEFAULT '{}'::jsonb,
  variant_id         text,
  variant_prompt_hash text,
  claim_registry_version text,
  prompt_versions    jsonb NOT NULL DEFAULT '{}'::jsonb,
  model_id           text NOT NULL DEFAULT '',
  bandit_posterior   jsonb,
  gate_results       jsonb NOT NULL DEFAULT '{}'::jsonb,
  config_version     text REFERENCES config_versions(hash)
);

CREATE TABLE IF NOT EXISTS replies (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id uuid NOT NULL REFERENCES prospects(id),
  touch_id    uuid REFERENCES touches(id),
  body        text,
  scrubbed    boolean NOT NULL DEFAULT false,
  intent      text,
  confidence  real,
  agentic_exchanges int NOT NULL DEFAULT 0,
  received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS suppressions (
  email_canonical text NOT NULL,
  scope           text NOT NULL,            -- 'global' or tenant slug
  reason          text NOT NULL,
  expires_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (email_canonical, scope)
);

CREATE TABLE IF NOT EXISTS forget_tombstones (
  email_hash  text PRIMARY KEY,
  receipt_id  uuid NOT NULL DEFAULT gen_random_uuid(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  provider_propagated_at timestamptz
);

CREATE TABLE IF NOT EXISTS control_flags (
  scope   text PRIMARY KEY,                 -- 'global' | tenant slug | domain
  flag    text NOT NULL,
  set_by  text NOT NULL,
  set_at  timestamptz NOT NULL DEFAULT now(),
  resume_requires text NOT NULL DEFAULT 'human'
);

CREATE TABLE IF NOT EXISTS counters (
  scope_type text NOT NULL,
  scope_id   text NOT NULL,
  period     text NOT NULL,
  used       int NOT NULL DEFAULT 0,
  cap        int NOT NULL,
  PRIMARY KEY (scope_type, scope_id, period)
);

CREATE TABLE IF NOT EXISTS spend_ledger (
  id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid REFERENCES tenants(id),
  category  text NOT NULL,
  job_id    bigint,
  est_usd   numeric(10,4) NOT NULL,
  actual_usd numeric(10,4),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS provider_events (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider          text NOT NULL,
  provider_event_id text NOT NULL,
  kind              text NOT NULL,
  payload           jsonb NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_event_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  uuid,
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  event      text NOT NULL,
  payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
  actor      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  queue        text NOT NULL,
  payload      jsonb NOT NULL,
  idempotency_key text UNIQUE,
  status       text NOT NULL DEFAULT 'ready',
  attempts     int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  lease_until  timestamptz,
  run_after    timestamptz NOT NULL DEFAULT now(),
  last_error   text
);
CREATE INDEX IF NOT EXISTS jobs_poll ON jobs (queue, run_after) WHERE status = 'ready';

-- Belt-and-braces (I-3): refuse touch claims for suppressed addresses even if
-- application logic is bypassed.
CREATE OR REPLACE FUNCTION reject_suppressed_touch() RETURNS trigger AS $$
DECLARE addr text;
BEGIN
  SELECT email_canonical INTO addr FROM prospects WHERE id = NEW.prospect_id;
  IF NEW.status = 'claimed' AND EXISTS (
    SELECT 1 FROM suppressions s
    WHERE s.email_canonical = addr
      AND (s.expires_at IS NULL OR s.expires_at > now())
  ) THEN
    RAISE EXCEPTION 'suppressed address (invariant I-3)';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reject_suppressed ON touches;
CREATE TRIGGER trg_reject_suppressed
  BEFORE INSERT OR UPDATE OF status ON touches
  FOR EACH ROW EXECUTE FUNCTION reject_suppressed_touch();

-- Belt-and-braces (I-1): a touch may only become 'dispatched' from 'claimed'.
CREATE OR REPLACE FUNCTION enforce_claim_lineage() RETURNS trigger AS $$
BEGIN
  IF NEW.status = 'dispatched' AND OLD.status IS DISTINCT FROM 'claimed' THEN
    RAISE EXCEPTION 'dispatch without claim (invariant I-1)';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_claim_lineage ON touches;
CREATE TRIGGER trg_claim_lineage
  BEFORE UPDATE OF status ON touches
  FOR EACH ROW EXECUTE FUNCTION enforce_claim_lineage();

CREATE TABLE IF NOT EXISTS mailboxes (
  mailbox        text PRIMARY KEY,
  tenant         text NOT NULL,
  domain         text NOT NULL,
  warmup_complete boolean NOT NULL DEFAULT false,
  daily_cap      int NOT NULL DEFAULT 25
);
