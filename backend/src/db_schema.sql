-- Schema for roadmap-web backend.
-- Executed once on app startup (all statements are idempotent).

-- Raw Jira payloads — single source of truth from Jira
CREATE TABLE IF NOT EXISTS jira_issue_raw (
    jira_key    VARCHAR(64) PRIMARY KEY,
    raw_data    JSONB       NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- Product → Jira project mapping (manually seeded)
CREATE TABLE IF NOT EXISTS product (
    name            VARCHAR(128) PRIMARY KEY,
    department      VARCHAR(128) NOT NULL DEFAULT 'Unassigned',
    primary_project VARCHAR(32)  NOT NULL,
    secondary_projects TEXT[],
    component_filter   TEXT[]
);

-- Migration: add department column if missing (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'product' AND column_name = 'department'
    ) THEN
        ALTER TABLE product ADD COLUMN department VARCHAR(128) NOT NULL DEFAULT 'Unassigned';
    END IF;
END $$;

-- Processed roadmap items ready for the API
CREATE TABLE IF NOT EXISTS roadmap_item (
    id           SERIAL       PRIMARY KEY,
    jira_key     VARCHAR(64)  NOT NULL UNIQUE,
    title        VARCHAR(512) NOT NULL,
    description  TEXT,
    status       VARCHAR(64)  NOT NULL,
    release      VARCHAR(64),
    tags         TEXT[],
    product      VARCHAR(128) REFERENCES product(name),
    color_status JSONB,
    url          TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Seed a catch-all product so FK never fails
INSERT INTO product (name, primary_project)
VALUES ('Uncategorized', 'NONE')
ON CONFLICT (name) DO NOTHING;
