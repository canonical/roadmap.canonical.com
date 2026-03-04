# Managing cycles

Work is planned in 6-month cycles (e.g. `25.10`, `26.04`). Each cycle has an explicit lifecycle state that determines how it appears on the roadmap.

## Cycle states

| State | Meaning | Items shown as | Data source |
|-------|---------|----------------|-------------|
| **future** | Planned but not started | All Inactive (white) | Live Jira (colours overridden) |
| **current** | The active cycle | Live Jira health colours | Live Jira |
| **frozen** | Closed cycle | Immutable snapshot | `cycle_freeze_item` table |

**Constraint:** At most **one** cycle can be `current` at any time. Zero is allowed during transition windows.

## Register a new cycle

Upcoming cycles should be registered as `future`:

```bash
curl -X POST http://localhost:8000/api/v1/cycles/27.04 \
  -H 'Content-Type: application/json' \
  -d '{"state": "future"}'
```

## List all cycles

```bash
curl http://localhost:8000/api/v1/cycles
```

Returns all known cycles (from both `cycle_config` and live Jira data) with their state and metadata.

## Change a cycle's state

```bash
curl -X PUT http://localhost:8000/api/v1/cycles/27.04 \
  -H 'Content-Type: application/json' \
  -d '{"state": "current"}'
```

### Side effects of state transitions

| Transition | Side effect |
|------------|-------------|
| Any → `frozen` | A snapshot is automatically created from the current `roadmap_item` data |
| `frozen` → any | The snapshot is automatically deleted |
| Any → `current` | Validated that no other cycle is already `current` |
| Any → `future` | No special side effects |

## Typical lifecycle workflow

```bash
# 1. Register upcoming cycle (items show as Inactive)
curl -X POST localhost:8000/api/v1/cycles/27.04 \
  -H 'Content-Type: application/json' -d '{"state": "future"}'

# 2. Cycle starts — items show live Jira colours
curl -X PUT localhost:8000/api/v1/cycles/27.04 \
  -H 'Content-Type: application/json' -d '{"state": "current"}'

# 3. Previous cycle ends — snapshot taken, data becomes immutable
curl -X PUT localhost:8000/api/v1/cycles/26.10 \
  -H 'Content-Type: application/json' -d '{"state": "frozen"}'
```

## Remove a cycle

```bash
curl -X DELETE http://localhost:8000/api/v1/cycles/27.04
```

If the cycle is frozen, the freeze snapshot is also deleted.

## View frozen cycle items

```bash
curl http://localhost:8000/api/v1/cycles/25.10/items
```

Returns the denormalized snapshot data for a frozen cycle.

## Corrections to a frozen cycle

If you need to fix data in a frozen cycle:

1. **Unfreeze**: `PUT /api/v1/cycles/25.10 {"state": "current"}` — the snapshot is deleted and live data is shown.
2. Make corrections in Jira and re-sync.
3. **Re-freeze**: `PUT /api/v1/cycles/25.10 {"state": "frozen"}` — a new snapshot is taken.

> **Note:** While a cycle is temporarily unfrozen for corrections, remember the at-most-one-current constraint. You may need to change the currently active cycle to `future` first.
