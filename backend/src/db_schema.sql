-- Schema for roadmap-web backend.
-- Executed once on app startup (all statements are idempotent).

-- Raw Jira payloads — single source of truth from Jira
CREATE TABLE IF NOT EXISTS jira_issue_raw (
    jira_key    VARCHAR(64) PRIMARY KEY,
    raw_data    JSONB       NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- Products — each product belongs to a department
CREATE TABLE IF NOT EXISTS product (
    id          SERIAL       PRIMARY KEY,
    name        VARCHAR(128) NOT NULL UNIQUE,
    department  VARCHAR(128) NOT NULL DEFAULT 'Unassigned',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Per-product Jira source rules — one product can pull from many Jira projects,
-- each with optional component/label/team filters.
CREATE TABLE IF NOT EXISTS product_jira_source (
    id                  SERIAL       PRIMARY KEY,
    product_id          INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    jira_project_key    VARCHAR(32)  NOT NULL,
    include_components  TEXT[],       -- only show epics in these components (NULL = all)
    exclude_components  TEXT[],       -- hide epics in these components (NULL = none)
    include_labels      TEXT[],       -- only show epics with these labels (NULL = all)
    exclude_labels      TEXT[],       -- hide epics with these labels (NULL = none)
    include_teams       TEXT[],       -- only show epics owned by these teams (NULL = all)
    exclude_teams       TEXT[],       -- hide epics owned by these teams (NULL = none)
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (product_id, jira_project_key)
);

-- Processed roadmap items ready for the API
CREATE TABLE IF NOT EXISTS roadmap_item (
    id              SERIAL       PRIMARY KEY,
    jira_key        VARCHAR(64)  NOT NULL UNIQUE,
    title           VARCHAR(512) NOT NULL,
    description     TEXT,
    status          VARCHAR(64)  NOT NULL,
    release         VARCHAR(64),
    tags            TEXT[],
    product_id      INTEGER      REFERENCES product(id),
    color_status    JSONB,
    url             TEXT,
    parent_key      VARCHAR(64),   -- parent (objective) Jira key, e.g. "ROCK-100"
    parent_summary  VARCHAR(512),  -- parent (objective) summary / title
    rank            VARCHAR(64),   -- Jira rank string for ordering (lexicographic)
    parent_rank     VARCHAR(64),   -- parent (objective) rank for objective ordering
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Add parent columns if table already existed without them
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'roadmap_item' AND column_name = 'parent_key'
    ) THEN
        ALTER TABLE roadmap_item ADD COLUMN parent_key VARCHAR(64);
        ALTER TABLE roadmap_item ADD COLUMN parent_summary VARCHAR(512);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'roadmap_item' AND column_name = 'rank'
    ) THEN
        ALTER TABLE roadmap_item ADD COLUMN rank VARCHAR(64);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'roadmap_item' AND column_name = 'parent_rank'
    ) THEN
        ALTER TABLE roadmap_item ADD COLUMN parent_rank VARCHAR(64);
    END IF;
END $$;

-- Seed a catch-all product so FK never fails
INSERT INTO product (name, department)
VALUES ('Uncategorized', 'Unassigned')
ON CONFLICT (name) DO NOTHING;
