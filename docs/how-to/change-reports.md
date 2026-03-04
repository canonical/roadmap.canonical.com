# Generating change reports

The app takes a daily snapshot of all roadmap items after each Jira sync. You can compare any two snapshots to generate a change report.

## List available snapshots

```bash
curl http://localhost:8000/api/v1/snapshots
```

Returns all snapshot dates (newest first) with item counts:

```json
{
  "data": [
    {"date": "2026-03-01", "item_count": 2450},
    {"date": "2026-02-28", "item_count": 2430}
  ],
  "meta": {"total": 2}
}
```

## Compare two snapshots

```bash
curl "http://localhost:8000/api/v1/snapshots/diff?from_date=2026-02-15&to_date=2026-03-01"
```

The diff response contains four categories:

| Field | Description |
|-------|-------------|
| `turned_red` | Items whose colour changed **to** red (subset of `color_changes`) |
| `color_changes` | All items whose health colour changed between the two dates |
| `disappeared` | Items present on `from_date` but **missing** on `to_date` |
| `appeared` | Items present on `to_date` but **not** on `from_date` |

Each category returns a list of items with their Jira key, title, old/new colour, product, and department.

## Example: biweekly report

To generate a biweekly change report:

```bash
# Find the dates
curl http://localhost:8000/api/v1/snapshots | jq '.data[:2]'

# Compare
curl "http://localhost:8000/api/v1/snapshots/diff?from_date=2026-02-15&to_date=2026-03-01" | jq '.summary'
```

The `summary` field gives a quick overview:

```json
{
  "turned_red": 3,
  "color_changes": 12,
  "disappeared": 5,
  "appeared": 8
}
```

## Snapshot behaviour

- **One snapshot per day**: If multiple syncs run on the same day, only the first creates a snapshot. Subsequent syncs are no-ops.
- **Denormalized data**: Product name and department are copied into the snapshot at capture time, so reports remain accurate even if products are renamed or deleted later.
- **Storage**: With ~2,500 items and one snapshot per day, you get ~912K rows/year (a few MB). PostgreSQL handles this trivially.
