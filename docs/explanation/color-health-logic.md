# Colour and health logic

Every roadmap item (epic) has a **health colour** and an optional **carry-over badge**. These are computed by `calculate_epic_color()` in `src/color_logic.py`.

## Why a separate module?

The colour logic is deliberately isolated from the rest of the codebase:

- It has **no dependencies** on the database, Jira, or FastAPI.
- It takes a plain dict (Jira issue fields) and returns a plain dict.
- It is **trivially testable** — `test_color_logic.py` covers all branches with pure unit tests.
- Both the sync pipeline and the display layer can use it without circular imports.

## Health colour derivation

The function examines data sources in the following order of precedence:

### 1. Jira status `Done` (highest priority)

If the Jira workflow status is **Done**, it overrides any `roadmap_state` value:

| roadmap_state | Status | Result |
|---------------|--------|--------|
| Added | Done | 🟦 Blue + "C" label |
| *(any other)* | Done | 🟢 Green + "C" label |

This ensures completed items are always clearly marked, even if a product manager previously set them as At Risk, Excluded, or Dropped.

### 2. `Dropped` roadmap_state

**Dropped** (⬛ Black) is preserved regardless of the Jira status (except Done). In particular, a Rejected + Dropped item stays black.

### 3. Jira status `Rejected`

If the Jira workflow status is **Rejected**, it overrides the remaining `roadmap_state` values (At Risk, Excluded, Added) and produces 🟥 Red.

### 4. Custom field `roadmap_state`

The Jira custom field `customfield_10968` (called "roadmap_state") provides explicit state overrides set by product managers:

| roadmap_state value | Colour |
|---------------------|--------|
| At Risk | 🟧 Orange |
| Excluded | 🟥 Red |
| Added | 🟦 Blue |

The field value may contain decorative emoji (🟧, 🟥, etc.) which are stripped before matching.

### 5. Jira workflow status (fallback)

If `roadmap_state` is not set (or has an unmapped value), the colour is derived from the Jira status:

| Status | Colour |
|--------|--------|
| In Progress, In Review, To Be Deployed, BLOCKED | 🟢 Green |
| Everything else | ⬜ White (unknown / not started) |

### Priority summary

```
Done + Added        → 🟦 Blue + "C"
Done + (anything)   → 🟢 Green + "C"
Dropped             → ⬛ Black         (even if Rejected)
Rejected            → 🟥 Red           (overrides At Risk, Added)
roadmap_state       → mapped colour
Active statuses     → 🟢 Green
(default)           → ⬜ White
```

### 6. Future cycle override (display layer only)

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
        "label": "C"          # optional — present for "Done" status
                              # (green "C" or blue "C" for Added+Done)
    },
    "carry_over": {
        "color": "purple",
        "count": 2            # number of carry-over cycles
    }  # or None if no carry-over
}
```

This is stored as JSONB in the `color_status` column of `roadmap_item` and used by the Jinja2 templates to render coloured cells and badges.
