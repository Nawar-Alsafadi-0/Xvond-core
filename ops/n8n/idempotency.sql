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
-- provider workflows resolve them inside the isolated workflow database.
CREATE TABLE IF NOT EXISTS xvond_managed_channel_routes (
    company_id BIGINT NOT NULL,
    connection_key TEXT NOT NULL,
    channel_id BIGINT NOT NULL,
    agent_id BIGINT NOT NULL,
    channel_type TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    provider_url TEXT NOT NULL,
    provider_secret TEXT NOT NULL,
    provider_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider_account_label TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (company_id, connection_key),
    UNIQUE (channel_id)
);

CREATE INDEX IF NOT EXISTS ix_xvond_managed_channel_routes_company_type
    ON xvond_managed_channel_routes (company_id, channel_type);

-- Atomic claim pattern:
-- INSERT ... ON CONFLICT DO NOTHING. If no row is inserted, read the existing row.
-- completed => return the stored prior result without calling the provider again.
-- processing/ambiguous => do not blindly repeat the side effect; reconcile first.
