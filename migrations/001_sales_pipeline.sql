CREATE TABLE IF NOT EXISTS sales_versions (
    version_id text PRIMARY KEY,
    day date NOT NULL,
    captured_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    report bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_versions_day ON sales_versions(day, captured_at DESC);
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id text PRIMARY KEY,
    kind text NOT NULL,
    day date NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'verified', 'failed')),
    version_id text REFERENCES sales_versions(version_id),
    error_code text,
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pipeline_receipts (
    version_id text NOT NULL REFERENCES sales_versions(version_id),
    destination text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'verified', 'ambiguous')),
    digest text NOT NULL,
    provider_id text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(version_id, destination)
);
CREATE TABLE IF NOT EXISTS pipeline_plans (
    version_id text NOT NULL REFERENCES sales_versions(version_id),
    destination text NOT NULL,
    plan jsonb NOT NULL,
    PRIMARY KEY(version_id, destination)
);
