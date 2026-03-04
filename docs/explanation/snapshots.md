# Snapshots and change tracking

The app captures daily snapshots of all roadmap items to support change reports — e.g. "what turned red in the last two weeks?"

## Why daily snapshots over change events?

Two approaches were considered:

1. **Change events** — record individual changes as they happen (event sourcing).
2. **Daily snapshots** — capture the full state once per day.

Daily snapshots were chosen because:

- **Simpler to implement** — a single `INSERT ... SELECT` captures everything.
- **Simpler to query** — comparing two dates is a straightforward JOIN, not an event replay.
- **Storage is trivial** — with ~2,500 items and one snapshot per day, that's ~912K rows/year (~a few MB). PostgreSQL handles this effortlessly.
- **No missed events** — if the app restarts or a sync fails, the next successful sync captures the current state. No events are lost.

## How it works

After each Jira sync, `take_daily_snapshot()` is called:

1. **Check idempotency** — if a snapshot for today already exists, return 0 (no-op).
2. **Copy all roadmap items** into `roadmap_snapshot` with denormalized fields.

The snapshot includes:

| Copied from `roadmap_item` | Denormalized from `product` |
|----------------------------|----------------------------|
| jira_key, title, status, tags, release | product_name, department |
| parent_key, parent_summary | |
| Extracted: `color_status->'health'->>'color'` → `color` | |

## Why denormalize?

Product name and department are copied into the snapshot at capture time rather than JOINed at query time. This ensures reports remain accurate even if:

- A product is renamed
- A product is deleted
- A product is moved to a different department

Historical records reflect the state at the time of capture.

## Diff queries

The `/api/v1/snapshots/diff` endpoint compares two snapshot dates and returns four categories:

### Turned red

Items whose `color` changed **to** `red` between the two dates. This is a subset of `color_changes` and is called out separately because it's the most actionable category for management.

### Colour changes

All items whose `color` differs between the two dates. Includes turned red, but also items that went green→orange, white→green, etc.

### Disappeared

Items present on `from_date` but **missing** on `to_date`. These are items that were removed from the roadmap — either their Jira epic was deleted, their cycle label was removed, or they no longer match the JQL filter.

### Appeared

Items present on `to_date` but **not** on `from_date`. New items added to the roadmap since the last snapshot.

## Snapshot vs cycle freeze

These are separate mechanisms with different purposes:

| | Daily snapshot | Cycle freeze |
|--|---------------|-------------|
| **Purpose** | Change tracking / reports | Preserve historical state at cycle closure |
| **Granularity** | All items, date-based | Per-cycle, label-based |
| **Trigger** | Automatic after each sync | Manual admin action |
| **Mutability** | Append-only (never modified) | Deleted on unfreeze |
| **Used by** | `/api/v1/snapshots/diff` | Roadmap page (frozen cycle view) |
