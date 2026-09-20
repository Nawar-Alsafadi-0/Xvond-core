CREATE TABLE IF NOT EXISTS xvond_workflow_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    company_id BIGINT NOT NULL,
    agent_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing','completed','failed','ambiguous')),
    provider_reference TEXT,
    result_json JSONB,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_xvond_workflow_idempotency_request_id
    ON xvond_workflow_idempotency (request_id);

CREATE INDEX IF NOT EXISTS ix_xvond_workflow_idempotency_company_action
    ON xvond_workflow_idempotency (company_id, action);

-- Workflow-plane managed channel registry. Provider credentials never enter the
-- Xvond Core database; Core sends them once to the private provisioning API and
-- the registry encrypts them before persistence in workflow-postgres.
CREATE TABLE IF NOT EXISTS xvond_managed_channel_routes (
    company_id BIGINT NOT NULL,
    connection_key TEXT NOT NULL,
    channel_id BIGINT NOT NULL,
    agent_id BIGINT NOT NULL,
    channel_type TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    provider_url TEXT NOT NULL,
    provider_secret_enc TEXT NOT NULL,
    provider_config_enc TEXT NOT NULL,
    provider_account_label TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, connection_key),
    UNIQUE (channel_id)
);

-- PR #227 introduced plaintext provider_secret/provider_config columns. A
-- production database may already contain that schema, so CREATE TABLE IF NOT
-- EXISTS alone is not a migration. Add encrypted columns first, fail old rows
-- closed, then remove the plaintext columns. Existing routes can be reprovisioned
-- through the authenticated registry service; no plaintext credential is kept.
ALTER TABLE xvond_managed_channel_routes
    ADD COLUMN IF NOT EXISTS provider_secret_enc TEXT;

ALTER TABLE xvond_managed_channel_routes
    ADD COLUMN IF NOT EXISTS provider_config_enc TEXT;

UPDATE xvond_managed_channel_routes
SET active = FALSE,
    updated_at = NOW()
WHERE active = TRUE
  AND (provider_secret_enc IS NULL OR provider_config_enc IS NULL);

ALTER TABLE xvond_managed_channel_routes
    DROP COLUMN IF EXISTS provider_secret;

ALTER TABLE xvond_managed_channel_routes
    DROP COLUMN IF EXISTS provider_config;

CREATE INDEX IF NOT EXISTS ix_xvond_managed_channel_routes_company_type
    ON xvond_managed_channel_routes (company_id, channel_type);

-- Atomic claim pattern:
-- INSERT ... ON CONFLICT DO NOTHING. If no row is inserted, read the existing row.
-- completed => return the stored prior result without calling the provider again.
-- processing/ambiguous => do not blindly repeat the side effect; reconcile first.
