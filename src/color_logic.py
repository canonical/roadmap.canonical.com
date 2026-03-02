"""Color/health logic for roadmap epics.

Separated into its own module so both the sync pipeline and the service
layer can use it without circular imports, and so it's trivially testable.
"""

from __future__ import annotations

import re
from typing import Any

CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")


def calculate_epic_color(
    issue_fields: dict[str, Any],
    frozen_cycles: set[str] | None = None,
) -> dict[str, Any]:
    """Derive color_status from raw Jira issue fields.

    Returns a dict like:
        {
            "health": {"color": "green", "label": "C"},  # label is optional
            "carry_over": {"color": "purple", "count": 1} | None,
        }

    Args:
        issue_fields: Raw Jira issue ``fields`` dict.
        frozen_cycles: Optional set of frozen cycle labels.  When provided,
            carry-over counts only the frozen cycle labels on the item
            (i.e. only past/closed cycles count towards carry-over).
            When ``None`` (the default — used during sync), carry-over
            counts all XX.XX cycle labels (backwards-compatible).

    Rules:
        - Multiple *cycle* labels (XX.XX pattern) → carry-over (purple badge).
        - Custom field ``roadmap_state`` overrides health color.
        - ``Done`` → green + completed label "C".
        - ``Rejected`` → red.
        - Active statuses (In Progress, In Review, …) → green.
        - Anything else → white (unknown / not started).
    """
    labels: list[str] = issue_fields.get("labels") or []
    status_name: str = (issue_fields.get("status") or {}).get("name", "")

    # roadmap_state is a custom field — adjust the ID to match your Jira instance
    roadmap_state_field = issue_fields.get("customfield_10968")
    raw_state: str | None = roadmap_state_field.get("value") if isinstance(roadmap_state_field, dict) else None
    # Strip leading/trailing whitespace and emoji characters (Jira values may
    # contain decorative emoji like 🟧, 🟥, 🟦, ⬛).
    state = re.sub(r"[^\w\s]", "", raw_state).strip() if raw_state else None

    # --- carry-over ----------------------------------------------------------
    cycle_labels = [lbl for lbl in labels if CYCLE_RE.match(lbl)]
    carry_over = None
    if frozen_cycles is not None:
        # Only count frozen (past) cycle labels as carry-over
        frozen_count = sum(1 for lbl in cycle_labels if lbl in frozen_cycles)
        if frozen_count > 0:
            carry_over = {"color": "purple", "count": frozen_count}
    else:
        # Legacy behaviour: count all cycle labels (used during sync pipeline)
        if len(cycle_labels) > 1:
            carry_over = {"color": "purple", "count": len(cycle_labels) - 1}

    # --- health color --------------------------------------------------------
    state_color_map = {
        "At Risk": "orange",
        "Excluded": "red",
        "Added": "blue",
        "Dropped": "black",
    }

    if state and state in state_color_map:
        health = {"color": state_color_map[state]}
    elif status_name == "Done":
        health = {"color": "green", "label": "C"}
    elif status_name == "Rejected":
        health = {"color": "red"}
    elif status_name in ("In Progress", "In Review", "To Be Deployed", "BLOCKED"):
        health = {"color": "green"}
    else:
        health = {"color": "white"}

    return {"health": health, "carry_over": carry_over}
