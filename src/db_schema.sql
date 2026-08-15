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

-- Daily snapshots of roadmap_item for change-tracking reports
CREATE TABLE IF NOT EXISTS roadmap_snapshot (
    id              SERIAL       PRIMARY KEY,
    snapshot_date   DATE         NOT NULL,
    jira_key        VARCHAR(64)  NOT NULL,
    title           VARCHAR(512) NOT NULL,
    status          VARCHAR(64)  NOT NULL,
    color           VARCHAR(32),
    release         VARCHAR(64),
    tags            TEXT[],
    product_id      INTEGER,
    product_name    VARCHAR(128),
    department      VARCHAR(128),
    parent_key      VARCHAR(64),
    parent_summary  VARCHAR(512),
    UNIQUE (snapshot_date, jira_key)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_date ON roadmap_snapshot(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snapshot_jira_key ON roadmap_snapshot(jira_key);

-- Frozen cycles — a closed cycle's data is immutable until explicitly unfrozen.
CREATE TABLE IF NOT EXISTS cycle_freeze (
    cycle       VARCHAR(16)  PRIMARY KEY,   -- e.g. '25.10'
    frozen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    frozen_by   VARCHAR(256),               -- email of the user who triggered the freeze
    note        TEXT                         -- optional free-text note (e.g. "Q1 2026 exec review")
);

-- Frozen item state — one row per epic per frozen cycle.
-- Denormalized so it is completely self-contained (like roadmap_snapshot).
CREATE TABLE IF NOT EXISTS cycle_freeze_item (
    id              SERIAL       PRIMARY KEY,
    cycle           VARCHAR(16)  NOT NULL REFERENCES cycle_freeze(cycle) ON DELETE CASCADE,
    jira_key        VARCHAR(64)  NOT NULL,
    title           VARCHAR(512) NOT NULL,
    status          VARCHAR(64)  NOT NULL,
    color_status    JSONB,
    url             TEXT,
    product_id      INTEGER,
    product_name    VARCHAR(128),
    department      VARCHAR(128),
    parent_key      VARCHAR(64),
    parent_summary  VARCHAR(512),
    rank            VARCHAR(64),
    parent_rank     VARCHAR(64),
    tags            TEXT[],
    UNIQUE (cycle, jira_key)
);

CREATE INDEX IF NOT EXISTS idx_cycle_freeze_item_cycle ON cycle_freeze_item(cycle);

-- Cycle configuration — explicit lifecycle state for each planning cycle.
-- States: 'frozen' (immutable snapshot), 'current' (live Jira sync), 'future' (all items Inactive).
-- At most ONE cycle may be in 'current' state at any time (zero is allowed during transitions).
CREATE TABLE IF NOT EXISTS cycle_config (
    cycle       VARCHAR(16)  PRIMARY KEY,              -- e.g. '25.10'
    state       VARCHAR(16)  NOT NULL
                CHECK (state IN ('frozen', 'current', 'future')),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by  VARCHAR(256)                           -- email of last person who changed state
);

-- Scheduler metadata — single-row table tracking the latest sync times.
-- The scheduler process UPSERTs here so the API can report "time since last sync".
CREATE TABLE IF NOT EXISTS sync_metadata (
    id              INTEGER      PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- exactly one row
    last_sync_start TIMESTAMPTZ,
    last_sync_end   TIMESTAMPTZ,
    last_sync_ok    BOOLEAN,
    next_sync_at    TIMESTAMPTZ,
    interval_seconds INTEGER     NOT NULL DEFAULT 3600,
    error_message   TEXT
);

INSERT INTO sync_metadata (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Add start_date and end_date to cycle_config
ALTER TABLE cycle_config
    ADD COLUMN IF NOT EXISTS start_date DATE,
    ADD COLUMN IF NOT EXISTS end_date   DATE;

-- Add Jira-derived fields and soft-delete to roadmap_item
ALTER TABLE roadmap_item
    ADD COLUMN IF NOT EXISTS assignee_name VARCHAR(256),
    ADD COLUMN IF NOT EXISTS priority      VARCHAR(32),
    ADD COLUMN IF NOT EXISTS t_shirt_size  VARCHAR(8),
    ADD COLUMN IF NOT EXISTS is_deleted    BOOLEAN NOT NULL DEFAULT FALSE;

-- Create index for non-deleted items
CREATE INDEX IF NOT EXISTS idx_roadmap_item_not_deleted ON roadmap_item(is_deleted) WHERE is_deleted = FALSE;

-- ---------------------------------------------------------------------------
-- Capacity Planning Schema
-- ---------------------------------------------------------------------------

-- Roles per product (1-4 max)
CREATE TABLE IF NOT EXISTS product_role (
    id          SERIAL       PRIMARY KEY,
    product_id  INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    name        VARCHAR(64)  NOT NULL,
    sort_order  INTEGER      NOT NULL DEFAULT 0,
    is_default  BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (product_id, name)
);

-- Team members (one role per member)
CREATE TABLE IF NOT EXISTS team_member (
    id                      SERIAL       PRIMARY KEY,
    product_id              INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    name                    VARCHAR(128) NOT NULL,
    role_id                 INTEGER      REFERENCES product_role(id) ON DELETE SET NULL,
    individual_coefficient  DECIMAL(3,2) NOT NULL DEFAULT 1.00,
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Weekly availability grid (0-5 days in office)
CREATE TABLE IF NOT EXISTS member_weekly_availability (
    id              SERIAL       PRIMARY KEY,
    member_id       INTEGER      NOT NULL REFERENCES team_member(id) ON DELETE CASCADE,
    week_start_date DATE         NOT NULL,
    days_available  INTEGER      NOT NULL CHECK (days_available BETWEEN 0 AND 5),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (member_id, week_start_date)
);

CREATE INDEX IF NOT EXISTS idx_mwa_member_week ON member_weekly_availability(member_id, week_start_date);

-- Product-level planning config
CREATE TABLE IF NOT EXISTS product_planning_config (
    product_id        INTEGER      PRIMARY KEY REFERENCES product(id) ON DELETE CASCADE,
    cycle_id          VARCHAR(16)  NOT NULL REFERENCES cycle_config(cycle) ON DELETE CASCADE,
    team_efficiency   DECIMAL(3,2) NOT NULL DEFAULT 0.60,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Per-epic per-role size estimates
CREATE TABLE IF NOT EXISTS epic_role_estimate (
    id                  SERIAL       PRIMARY KEY,
    roadmap_item_id     INTEGER      NOT NULL REFERENCES roadmap_item(id) ON DELETE CASCADE,
    role_id             INTEGER      NOT NULL REFERENCES product_role(id) ON DELETE CASCADE,
    size_days           INTEGER      NOT NULL DEFAULT 0,
    initial_size_days   INTEGER      NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (roadmap_item_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_ere_roadmap_item ON epic_role_estimate(roadmap_item_id);
CREATE INDEX IF NOT EXISTS idx_ere_role ON epic_role_estimate(role_id);

-- Epic selection / drop state per cycle
CREATE TABLE IF NOT EXISTS epic_cycle_selection (
    id              SERIAL       PRIMARY KEY,
    roadmap_item_id INTEGER      NOT NULL REFERENCES roadmap_item(id) ON DELETE CASCADE,
    cycle_id        VARCHAR(16)  NOT NULL REFERENCES cycle_config(cycle) ON DELETE CASCADE,
    is_in_roadmap   BOOLEAN      NOT NULL DEFAULT FALSE,
    is_dropped      BOOLEAN      NOT NULL DEFAULT FALSE,
    dropped_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (roadmap_item_id, cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_ecs_cycle_roadmap ON epic_cycle_selection(cycle_id, is_in_roadmap);
CREATE INDEX IF NOT EXISTS idx_ecs_roadmap_item ON epic_cycle_selection(roadmap_item_id);

-- Manual weekly progress (mid-cycle remaining work)
CREATE TABLE IF NOT EXISTS epic_weekly_progress (
    id              SERIAL       PRIMARY KEY,
    roadmap_item_id INTEGER      NOT NULL REFERENCES roadmap_item(id) ON DELETE CASCADE,
    week_start_date DATE         NOT NULL,
    remaining_days  INTEGER,
    created_by      VARCHAR(256),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (roadmap_item_id, week_start_date)
);

CREATE INDEX IF NOT EXISTS idx_ewp_roadmap_item ON epic_weekly_progress(roadmap_item_id);
CREATE INDEX IF NOT EXISTS idx_ewp_week ON epic_weekly_progress(week_start_date);

-- Audit log for per-field undo
CREATE TABLE IF NOT EXISTS planning_audit_log (
    id          SERIAL       PRIMARY KEY,
    product_id  INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    table_name  VARCHAR(64)  NOT NULL,
    record_id   INTEGER      NOT NULL,
    action      VARCHAR(16)  NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    old_values  JSONB,
    new_values  JSONB,
    changed_by  VARCHAR(256),
    changed_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_product ON planning_audit_log(product_id, changed_at DESC);

-- Add start_date and end_date to cycle_config
ALTER TABLE cycle_config
    ADD COLUMN IF NOT EXISTS start_date DATE,
    ADD COLUMN IF NOT EXISTS end_date   DATE;

-- Add Jira-derived fields and soft-delete to roadmap_item
ALTER TABLE roadmap_item
    ADD COLUMN IF NOT EXISTS assignee_name VARCHAR(256),
    ADD COLUMN IF NOT EXISTS priority      VARCHAR(32),
    ADD COLUMN IF NOT EXISTS t_shirt_size  VARCHAR(8),
    ADD COLUMN IF NOT EXISTS is_deleted    BOOLEAN NOT NULL DEFAULT FALSE;

-- Create index for non-deleted items
CREATE INDEX IF NOT EXISTS idx_roadmap_item_not_deleted ON roadmap_item(is_deleted) WHERE is_deleted = FALSE;

-- ---------------------------------------------------------------------------
-- Capacity Planning Schema
-- ---------------------------------------------------------------------------

-- Roles per product (1-4 max)
CREATE TABLE IF NOT EXISTS product_role (
    id          SERIAL       PRIMARY KEY,
    product_id  INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    name        VARCHAR(64)  NOT NULL,
    sort_order  INTEGER      NOT NULL DEFAULT 0,
    is_default  BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (product_id, name)
);

-- Team members (one role per member)
CREATE TABLE IF NOT EXISTS team_member (
    id                      SERIAL       PRIMARY KEY,
    product_id              INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    name                    VARCHAR(128) NOT NULL,
    role_id                 INTEGER      REFERENCES product_role(id) ON DELETE SET NULL,
    individual_coefficient  DECIMAL(3,2) NOT NULL DEFAULT 1.00,
    is_active               BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Weekly availability grid (0-5 days in office)
CREATE TABLE IF NOT EXISTS member_weekly_availability (
    id              SERIAL       PRIMARY KEY,
    member_id       INTEGER      NOT NULL REFERENCES team_member(id) ON DELETE CASCADE,
    week_start_date DATE         NOT NULL,
    days_available  INTEGER      NOT NULL CHECK (days_available BETWEEN 0 AND 5),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (member_id, week_start_date)
);

CREATE INDEX IF NOT EXISTS idx_mwa_member_week ON member_weekly_availability(member_id, week_start_date);

-- Product-level planning config
CREATE TABLE IF NOT EXISTS product_planning_config (
    product_id        INTEGER      PRIMARY KEY REFERENCES product(id) ON DELETE CASCADE,
    cycle_id          VARCHAR(16)  NOT NULL REFERENCES cycle_config(cycle) ON DELETE CASCADE,
    team_efficiency   DECIMAL(3,2) NOT NULL DEFAULT 0.60,
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Per-epic per-role size estimates
CREATE TABLE IF NOT EXISTS epic_role_estimate (
    id                  SERIAL       PRIMARY KEY,
    roadmap_item_id     INTEGER      NOT NULL REFERENCES roadmap_item(id) ON DELETE CASCADE,
    role_id             INTEGER      NOT NULL REFERENCES product_role(id) ON DELETE CASCADE,
    size_days           INTEGER      NOT NULL DEFAULT 0,
    initial_size_days   INTEGER      NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (roadmap_item_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_ere_roadmap_item ON epic_role_estimate(roadmap_item_id);
CREATE INDEX IF NOT EXISTS idx_ere_role ON epic_role_estimate(role_id);

-- Epic selection / drop state per cycle
CREATE TABLE IF NOT EXISTS epic_cycle_selection (
    id              SERIAL       PRIMARY KEY,
    roadmap_item_id INTEGER      NOT NULL REFERENCES roadmap_item(id) ON DELETE CASCADE,
    cycle_id        VARCHAR(16)  NOT NULL REFERENCES cycle_config(cycle) ON DELETE CASCADE,
    is_in_roadmap   BOOLEAN      NOT NULL DEFAULT FALSE,
    is_dropped      BOOLEAN      NOT NULL DEFAULT FALSE,
    dropped_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (roadmap_item_id, cycle_id)
);

CREATE INDEX IF NOT EXISTS idx_ecs_cycle_roadmap ON epic_cycle_selection(cycle_id, is_in_roadmap);
CREATE INDEX IF NOT EXISTS idx_ecs_roadmap_item ON epic_cycle_selection(roadmap_item_id);

-- Manual weekly progress (mid-cycle remaining work)
CREATE TABLE IF NOT EXISTS epic_weekly_progress (
    id              SERIAL       PRIMARY KEY,
    roadmap_item_id INTEGER      NOT NULL REFERENCES roadmap_item(id) ON DELETE CASCADE,
    week_start_date DATE         NOT NULL,
    remaining_days  INTEGER,
    created_by      VARCHAR(256),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (roadmap_item_id, week_start_date)
);

CREATE INDEX IF NOT EXISTS idx_ewp_roadmap_item ON epic_weekly_progress(roadmap_item_id);
CREATE INDEX IF NOT EXISTS idx_ewp_week ON epic_weekly_progress(week_start_date);

-- Audit log for per-field undo
CREATE TABLE IF NOT EXISTS planning_audit_log (
    id          SERIAL       PRIMARY KEY,
    product_id  INTEGER      NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    table_name  VARCHAR(64)  NOT NULL,
    record_id   INTEGER      NOT NULL,
    action      VARCHAR(16)  NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    old_values  JSONB,
    new_values  JSONB,
    changed_by  VARCHAR(256),
    changed_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_product ON planning_audit_log(product_id, changed_at DESC);

-- Seed a catch-all product so FK never fails
INSERT INTO product (name, department)
VALUES ('Uncategorized', 'Unassigned')
ON CONFLICT (name) DO NOTHING;
