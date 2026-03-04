# Triggering a Jira sync

The Jira sync pipeline fetches issues from Jira, processes them into roadmap items, and takes a daily snapshot. It can run on-demand via the API or automatically via the built-in scheduler.

## Manual sync

```bash
curl -X POST http://localhost:8000/api/v1/sync
```

The sync runs in the background. Monitor progress with:

```bash
curl http://localhost:8000/api/v1/status
```

The status response includes:

- `state` — one of `idle`, `syncing`, `processing`, `done`, `failed`
- `issues_fetched` — number of issues pulled from Jira (Phase 1)
- `issues_processed` — number of issues transformed (Phase 2)
- `snapshot_items` — number of items captured in the daily snapshot (Phase 3)
- `error` — error message if the sync failed
- `db` — database row counts for troubleshooting
- `config.effective_jql` — the JQL query that was actually sent to Jira

## Automatic sync (scheduler)

The app includes a standalone scheduler process (`src/scheduler.py`) that runs periodic syncs.

**Configuration:**

| Env var | Default | Description |
|---------|---------|-------------|
| `SYNC_INTERVAL_SECONDS` / `APP_SYNC_INTERVAL_SECONDS` | `3600` | Seconds between syncs. Set to `0` to disable. |

The scheduler runs as a separate process:

```bash
python3 -m src.scheduler
```

In production (Juju/rock deployment), the scheduler runs as a separate Pebble service. Only unit 0 runs it to avoid duplicate syncs.

## Sync schedule status

```bash
curl http://localhost:8000/api/v1/sync/schedule
```

Returns timing information from the `sync_metadata` table:

- `last_sync_start` / `last_sync_end` — when the last sync started and finished
- `last_sync_ok` — whether the last sync succeeded
- `next_sync_at` — when the next automatic sync is scheduled
- `interval_seconds` — the configured interval
- `seconds_since_last_sync` / `seconds_until_next_sync` — computed convenience fields

## Prerequisites for a successful sync

Before syncing, ensure:

1. **Jira credentials** are set: `JIRA_URL`, `JIRA_USERNAME`, `JIRA_PAT`
2. **At least one product** exists with Jira source mappings (see [Managing products](managing-products.md))
3. **At least one cycle** is registered with state `current` or `future` (see [Managing cycles](managing-cycles.md))

The sync builds its JQL dynamically from the configured products and active cycles. If either is missing, the sync will fail with a clear error message.

## What happens during a sync

1. **Phase 1 — Fetch**: Issues matching the JQL are fetched from Jira and stored as raw JSON in `jira_issue_raw`.
2. **Phase 2 — Process**: Unprocessed raw issues are transformed into `roadmap_item` rows with derived colour/health status and product assignment.
3. **Phase 3 — Snapshot**: A daily snapshot of all roadmap items is captured in `roadmap_snapshot` (idempotent — only one per calendar day).

See [Jira sync pipeline](../explanation/jira-sync-pipeline.md) for the full architectural explanation.
