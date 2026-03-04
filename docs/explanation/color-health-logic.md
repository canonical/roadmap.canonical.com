# Colour and health logic

Every roadmap item (epic) has a **health colour** and an optional **carry-over badge**. These are computed by `calculate_epic_color()` in `src/color_logic.py`.

## Why a separate module?

The colour logic is deliberately isolated from the rest of the codebase:

- It has **no dependencies** on the database, Jira, or FastAPI.
- It takes a plain dict (Jira issue fields) and returns a plain dict.
- It is **trivially testable** — `test_color_logic.py` covers all branches with pure unit tests.
- Both the sync pipeline and the display layer can use it without circular imports.

## Health colour derivation

The function examines two data sources in order of precedence:

### 1. Custom field `roadmap_state` (highest priority)

The Jira custom field `customfield_10968` (called "roadmap_state") provides explicit state overrides set by product managers:

| roadmap_state value | Colour |
|---------------------|--------|
| At Risk | 🟧 Orange |
| Excluded | 🟥 Red |
| Added | 🟦 Blue |
| Dropped | ⬛ Black |

The field value may contain decorative emoji (🟧, 🟥, etc.) which are stripped before matching.

### 2. Jira workflow status (fallback)

If `roadmap_state` is not set (or has an unmapped value), the colour is derived from the Jira status:

| Status | Colour |
|--------|--------|
| Done | 🟢 Green + "C" label (completed) |
| Rejected | 🟥 Red |
| In Progress, In Review, To Be Deployed, BLOCKED | 🟢 Green |
| Everything else | ⬜ White (unknown / not started) |

### 3. Future cycle override (display layer only)

Items in cycles with state `future` have their colour overridden to **white/Inactive** on the roadmap page. This happens in the display layer (`app.py`), not in `calculate_epic_color()`, because the sync pipeline should store the actual health colour.

## Carry-over logic

An item appearing in multiple 6-month cycles shows a purple carry-over badge indicating it has persisted across planning periods.

### Cycle label detection

Labels matching the regex `^\d{2}\.\d{2}$` (e.g. `25.10`, `26.04`) are treated as cycle labels. Other labels (like `ComponentPlatform`, `SSDLC`) are ignored.

### Two counting modes

The `calculate_epic_color()` function has an optional `frozen_cycles` parameter that changes how carry-over is counted:

| Mode | When used | Counting rule |
|------|-----------|---------------|
| `frozen_cycles=None` | Sync pipeline (Phase 2) | Count = total cycle labels − 1 (all cycles count) |
| `frozen_cycles={set}` | Display layer | Count = number of frozen cycle labels on the item |

**Why the distinction?**

During sync, we don't know which cycles are frozen — we just store the raw carry-over count. On the display layer, we have full knowledge of cycle states, so carry-over reflects actual past-cycle persistence:

- An item in `25.10` (frozen) and `26.04` (current) → carry-over count = 1
- An item in `26.04` (current) and `26.10` (future) → carry-over count = 0
- An item in `25.04` (frozen) and `25.10` (frozen) → carry-over count = 1 (the *other* frozen cycle)

## Output format

```python
{
    "health": {
        "color": "green",     # green, red, orange, blue, black, white
        "label": "C"          # optional — only present for "Done" status
    },
    "carry_over": {
        "color": "purple",
        "count": 2            # number of carry-over cycles
    }  # or None if no carry-over
}
```

This is stored as JSONB in the `color_status` column of `roadmap_item` and used by the Jinja2 templates to render coloured cells and badges.
