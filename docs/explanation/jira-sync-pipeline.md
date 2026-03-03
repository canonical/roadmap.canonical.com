# Jira sync pipeline

The sync pipeline is the core data flow of the application. It follows a **two-phase** approach with two additional post-processing steps.

## Why two phases?

Separating raw fetch from processing gives several advantages:

1. **Re-processing without re-fetching** — if the colour logic or product matching rules change, we can re-process existing raw data without hitting Jira again.
2. **Debugging** — the raw Jira JSON is always available for inspection.
3. **Resilience** — if Phase 2 fails, the raw data is safe and can be processed on the next run.

## Pipeline phases

```
Phase 1: Fetch         Phase 2: Process       Phase 3: Snapshot
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Jira REST   │ ───▶ │ jira_issue_  │ ───▶ │  roadmap_    │ ───▶ roadmap_
│  API (JQL)   │      │ raw (JSONB)  │      │  item        │      snapshot
└──────────────┘      └──────────────┘      └──────────────┘
```

### Phase 1 — Fetch (`sync_jira_data`)

1. **Build JQL dynamically** from the database:
   - Read distinct `jira_project_key` values from `product_jira_source`
   - Read non-frozen cycle labels from `cycle_config` (states `current` and `future`)
   - Append the static `JQL_FILTER` setting
   - Result: `project in (LXD, MAAS) AND labels in (26.04, 26.10) AND issuetype = Epic`

2. **Fetch issues** using the `jira` Python library with `maxResults=False` (fetch all).

3. **Fetch parent issues** (objectives) in a second batch to capture their rank for ordering.

4. **Upsert raw JSON** into `jira_issue_raw`, resetting `processed_at = NULL` to mark them for reprocessing.

### Phase 2 — Process (`process_raw_jira_data`)

For each unprocessed raw issue:

1. **Match to a product** using `product_jira_source` rules (see [Product-Jira mapping](product-jira-mapping.md)).
2. **Compute health colour** using `calculate_epic_color()` (see [Colour and health logic](color-health-logic.md)).
3. **Extract fields**: summary, status, fix version, labels, parent key/summary, rank.
4. **Upsert into `roadmap_item`** with all derived fields.
5. **Mark as processed** by setting `processed_at = now()` in `jira_issue_raw`.

### Phase 3 — Snapshot (`take_daily_snapshot`)

After processing, a daily snapshot is taken:

1. Check if today's snapshot already exists → if yes, skip (idempotent).
2. Insert a copy of every `roadmap_item` into `roadmap_snapshot` with denormalized product name, department, and extracted health colour.

This ensures exactly one snapshot per calendar day, regardless of how many syncs run.

## Dynamic JQL construction

The JQL is built from database state, not hardcoded. This means:

- Adding a new product with Jira sources automatically includes its project in the next sync.
- Registering a new cycle automatically includes its label in the JQL.
- Frozen cycles are excluded — their data is immutable.

```
project in ({product_jira_source.jira_project_key})
AND labels in ({cycle_config WHERE state IN ('current', 'future')})
AND {settings.jql_filter}
```

## Parent rank injection

To support objective-level ordering on the roadmap page, Phase 1 fetches parent issues and injects their rank into the raw payload:

```python
raw.setdefault("_roadmap_meta", {})["parent_rank"] = parent_ranks.get(parent_key, "")
```

Phase 2 then extracts this injected field alongside the issue's own rank.

## Error handling

- If Phase 1 fails (Jira unavailable), no raw data is modified.
- If Phase 2 fails mid-way, only the successfully processed issues are marked; unprocessed ones will be retried on the next sync.
- The scheduler records success/failure in `sync_metadata` for observability.

## Scheduler

The scheduler (`src/scheduler.py`) is a standalone process that:

1. Applies the DB schema on startup
2. Runs the full three-phase sync immediately
3. Sleeps for `SYNC_INTERVAL_SECONDS`
4. Repeats

It is designed to run as a separate Pebble service. In Juju deployments, paas-charm ensures only unit 0 runs the scheduler, so no distributed locking is needed.
