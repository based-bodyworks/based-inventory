-- Separate durable sales history and Slack reservations for the daily report.
-- Reuses the existing paid PostgreSQL database without enabling cloud publishers.
CREATE TABLE IF NOT EXISTS daily_velocity_archive (
    archive_key text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
