# Database schema

All tables are defined in `src/db_schema.sql` and applied idempotently on every app startup. The schema uses PostgreSQL-specific features (JSONB, arrays, `DO $$` blocks).

## Entity relationship

```
┌─────────────────┐       ┌─────────────────────┐
│     product      │──1:N──│  product_jira_source │
└────────┬────────┘       └─────────────────────┘
         │ 1:N
┌────────▼────────┐       ┌─────────────────────┐
│  roadmap_item    │       │   jira_issue_raw     │
└────────┬────────┘       └─────────────────────┘
         │
    ┌────┴────────────────────┐
    │                         │
┌───▼──────────────┐  ┌──────▼──────────┐
│ roadmap_snapshot  │  │cycle_freeze_item │
└──────────────────┘  └──────┬──────────┘
                             │ N:1
                      ┌──────▼──────────┐
                      │  cycle_freeze    │
                      └─────────────────┘

┌─────────────────┐  ┌─────────────────┐
│  cycle_config    │  │  sync_metadata   │
└─────────────────┘  └─────────────────┘
```

---

## Tables

### `jira_issue_raw`

Stores raw Jira JSON payloads — the single source of truth from Jira.

| Column | Type | Description |
|--------|------|-------------|
| `jira_key` | `VARCHAR(64)` PK | Jira issue key (e.g. `LXD-123`) |
| `raw_data` | `JSONB` NOT NULL | Full raw Jira issue JSON |
| `fetched_at` | `TIMESTAMPTZ` | When the issue was last fetched |
| `processed_at` | `TIMESTAMPTZ` | When the issue was last processed into `roadmap_item` (NULL = unprocessed) |

### `product`

Products (organisational units) that own roadmap items.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-generated ID |
| `name` | `VARCHAR(128)` UNIQUE | Product name (e.g. `LXD`) |
| `department` | `VARCHAR(128)` | Department name (default: `Unassigned`) |
| `created_at` | `TIMESTAMPTZ` | Creation timestamp |
| `updated_at` | `TIMESTAMPTZ` | Last modification timestamp |

An `Uncategorized` product is auto-seeded on schema creation and serves as the fallback for unmatched issues.

### `product_jira_source`

Jira project → product mapping rules with optional filters.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-generated ID |
| `product_id` | `INTEGER` FK → `product(id)` | Owning product (CASCADE delete) |
| `jira_project_key` | `VARCHAR(32)` | Jira project key (e.g. `LXD`) |
| `include_components` | `TEXT[]` | Only include epics with these components |
| `exclude_components` | `TEXT[]` | Exclude epics with these components |
| `include_labels` | `TEXT[]` | Only include epics with these labels |
| `exclude_labels` | `TEXT[]` | Exclude epics with these labels |
| `include_teams` | `TEXT[]` | Only include epics with these teams |
| `exclude_teams` | `TEXT[]` | Exclude epics with these teams |
| `created_at` | `TIMESTAMPTZ` | Creation timestamp |

**Unique constraint:** `(product_id, jira_project_key)`

### `roadmap_item`

Processed roadmap items ready for the API and UI.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-generated ID |
| `jira_key` | `VARCHAR(64)` UNIQUE | Jira issue key |
| `title` | `VARCHAR(512)` | Issue summary |
| `description` | `TEXT` | Issue description |
| `status` | `VARCHAR(64)` | Jira workflow status (e.g. `In Progress`) |
| `release` | `VARCHAR(64)` | Fix version name |
| `tags` | `TEXT[]` | Jira labels (includes cycle labels like `26.04`) |
| `product_id` | `INTEGER` FK → `product(id)` | Assigned product |
| `color_status` | `JSONB` | Computed health/carry-over JSON |
| `url` | `TEXT` | Jira browse URL |
| `parent_key` | `VARCHAR(64)` | Parent (objective) Jira key |
| `parent_summary` | `VARCHAR(512)` | Parent (objective) summary |
| `rank` | `VARCHAR(64)` | Jira rank string (lexicographic ordering) |
| `parent_rank` | `VARCHAR(64)` | Parent rank string (objective ordering) |
| `created_at` | `TIMESTAMPTZ` | Creation timestamp |
| `updated_at` | `TIMESTAMPTZ` | Last modification timestamp |

### `roadmap_snapshot`

Daily snapshots for change tracking. Denormalized — self-contained historical records.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-generated ID |
| `snapshot_date` | `DATE` | Snapshot date |
| `jira_key` | `VARCHAR(64)` | Jira issue key |
| `title` | `VARCHAR(512)` | Issue summary at snapshot time |
| `status` | `VARCHAR(64)` | Status at snapshot time |
| `color` | `VARCHAR(32)` | Extracted health colour (`green`, `red`, etc.) |
| `release` | `VARCHAR(64)` | Fix version at snapshot time |
| `tags` | `TEXT[]` | Labels at snapshot time |
| `product_id` | `INTEGER` | Product ID at snapshot time |
| `product_name` | `VARCHAR(128)` | Denormalized product name |
| `department` | `VARCHAR(128)` | Denormalized department name |
| `parent_key` | `VARCHAR(64)` | Parent key at snapshot time |
| `parent_summary` | `VARCHAR(512)` | Parent summary at snapshot time |

**Unique constraint:** `(snapshot_date, jira_key)`

**Indexes:** `idx_snapshot_date`, `idx_snapshot_jira_key`

### `cycle_freeze`

Frozen cycle headers — one row per frozen cycle.

| Column | Type | Description |
|--------|------|-------------|
| `cycle` | `VARCHAR(16)` PK | Cycle label (e.g. `25.10`) |
| `frozen_at` | `TIMESTAMPTZ` | When the cycle was frozen |
| `frozen_by` | `VARCHAR(256)` | Email of the person who triggered the freeze |
| `note` | `TEXT` | Optional free-text note |

### `cycle_freeze_item`

Frozen item snapshots — fully denormalized, self-contained.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` PK | Auto-generated ID |
| `cycle` | `VARCHAR(16)` FK → `cycle_freeze(cycle)` | Owning cycle (CASCADE delete) |
| `jira_key` | `VARCHAR(64)` | Jira issue key |
| `title` | `VARCHAR(512)` | Summary at freeze time |
| `status` | `VARCHAR(64)` | Status at freeze time |
| `color_status` | `JSONB` | Full colour/health JSON at freeze time |
| `url` | `TEXT` | Jira URL |
| `product_id` | `INTEGER` | Product ID at freeze time |
| `product_name` | `VARCHAR(128)` | Denormalized product name |
| `department` | `VARCHAR(128)` | Denormalized department name |
| `parent_key` | `VARCHAR(64)` | Objective Jira key |
| `parent_summary` | `VARCHAR(512)` | Objective summary |
| `rank` | `VARCHAR(64)` | Item rank |
| `parent_rank` | `VARCHAR(64)` | Objective rank |
| `tags` | `TEXT[]` | Labels at freeze time |

**Unique constraint:** `(cycle, jira_key)`

**Index:** `idx_cycle_freeze_item_cycle`

### `cycle_config`

Cycle lifecycle state registry.

| Column | Type | Description |
|--------|------|-------------|
| `cycle` | `VARCHAR(16)` PK | Cycle label (e.g. `26.04`) |
| `state` | `VARCHAR(16)` CHECK | One of: `frozen`, `current`, `future` |
| `updated_at` | `TIMESTAMPTZ` | Last state change timestamp |
| `updated_by` | `VARCHAR(256)` | Email of the person who last changed the state |

**Constraint:** At most one row may have `state = 'current'` (enforced at the application level).

### `sync_metadata`

Single-row table tracking the latest sync times.

| Column | Type | Description |
|--------|------|-------------|
| `id` | `INTEGER` PK | Always `1` (CHECK constraint) |
| `last_sync_start` | `TIMESTAMPTZ` | When the last sync started |
| `last_sync_end` | `TIMESTAMPTZ` | When the last sync finished |
| `last_sync_ok` | `BOOLEAN` | Whether the last sync succeeded |
| `next_sync_at` | `TIMESTAMPTZ` | Scheduled time for next sync |
| `interval_seconds` | `INTEGER` | Configured sync interval (default: 3600) |
| `error_message` | `TEXT` | Error message from last failed sync |
