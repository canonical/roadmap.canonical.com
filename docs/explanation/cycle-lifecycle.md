# Cycle lifecycle

Work is planned in 6-month cycles (e.g. `25.10`, `26.04`). Each cycle has an explicit lifecycle managed via the `cycle_config` table.

## Why explicit state management?

Early versions derived cycle state from heuristics (e.g. "is there a freeze record?"). This was fragile and ambiguous. Explicit state management gives administrators full manual control:

- **No guessing** — each cycle's state is unambiguously recorded.
- **Intentional transitions** — side effects (freeze/unfreeze) happen as a result of deliberate admin action.
- **Audit trail** — `updated_at` and `updated_by` record who changed what and when.

## State machine

```
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  future   │ ──▶ │ current  │ ──▶ │  frozen  │
  └──────────┘     └──────────┘     └──────────┘
       │                │                │
       └────────────────┴────────────────┘
         (any transition is allowed)
```

Any-to-any transitions are allowed. This is intentional — admins might need to:

- Unfreeze a past cycle for corrections (`frozen → current`)
- Skip directly to frozen for pre-seeded cycles (`future → frozen`)
- Revert a premature activation (`current → future`)

## State semantics

### Future

Items in a future cycle are displayed as **white/Inactive** regardless of their actual Jira health colour. This prevents premature attention to planned work that hasn't started yet.

The override happens at the display layer — the underlying `roadmap_item` still stores the real health colour from Jira.

### Current

The active cycle. Items show their live Jira health colours. At most **one** cycle can be current at any time (enforced at the application level). Zero current cycles is allowed during transition windows.

### Frozen

A frozen cycle's data is captured in an immutable snapshot (`cycle_freeze` + `cycle_freeze_item`). The roadmap page serves the snapshot instead of live Jira data. Jira syncs continue to update live items, but frozen cycles are unaffected.

## Side effects of transitions

| Transition | What happens |
|------------|-------------|
| → `frozen` | `_ensure_freeze_snapshot()` creates a `cycle_freeze` header and copies all matching `roadmap_item` rows into `cycle_freeze_item` with denormalized product/department/objective data |
| `frozen` → | `_delete_freeze_snapshot()` deletes the `cycle_freeze` row (items are CASCADE-deleted) |
| → `current` | Validates at-most-one-current constraint |
| → `future` | No special side effects |

## Freeze snapshot design

The freeze snapshot is **fully denormalized** — product name, department, colour status, objective key/summary are all copied into `cycle_freeze_item`. This means:

- Frozen data is completely independent of live data
- Products can be renamed or deleted without affecting historical records
- No JOINs needed to serve frozen cycle pages

## Carry-over interaction

Carry-over counts how many **frozen** cycle labels an item has. This interacts with the cycle state:

| Scenario | Carry-over |
|----------|-----------|
| Item in `25.10` (frozen) + `26.04` (current) | 1 (the frozen cycle counts) |
| Item in `26.04` (current) + `26.10` (future) | 0 (neither is frozen) |
| Item in `25.04` (frozen) + `25.10` (frozen) viewing `25.10` | 1 (the *other* frozen cycle) |

## Typical steady state

In normal operation:

- Multiple past cycles are `frozen` (each with an immutable snapshot)
- One cycle is `current` (showing live Jira data)
- Zero or more upcoming cycles are `future` (items visible but all Inactive)

## UI indicators

| State | Dropdown badge | Section heading |
|-------|---------------|-----------------|
| Frozen | 🔒 | 🔒 + "This cycle is frozen" banner |
| Current | ▶ | ▶ |
| Future | 🔮 | 🔮 + "This cycle is planned" banner |
