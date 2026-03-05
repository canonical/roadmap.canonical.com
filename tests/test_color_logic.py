"""Tests for the color/health derivation logic."""

from src.color_logic import calculate_epic_color


def test_done_status():
    fields = {"status": {"name": "Done"}, "labels": []}
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "green", "label": "C"}
    assert result["carry_over"] is None


def test_at_risk_state():
    fields = {
        "status": {"name": "In Progress"},
        "customfield_10968": {"value": "At Risk"},
        "labels": ["25.10"],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "orange"


def test_rejected_status():
    fields = {"status": {"name": "Rejected"}, "labels": []}
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "red"


def test_in_progress_status():
    fields = {"status": {"name": "In Progress"}, "labels": []}
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "green"


def test_unknown_status_defaults_white():
    fields = {"status": {"name": "Backlog"}, "labels": []}
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "white"


def test_carry_over_with_multiple_labels():
    fields = {"status": {"name": "In Progress"}, "labels": ["24.04", "25.10"]}
    result = calculate_epic_color(fields)
    assert result["carry_over"] == {"color": "purple", "count": 1}


def test_no_carry_over_with_single_label():
    fields = {"status": {"name": "In Progress"}, "labels": ["25.10"]}
    result = calculate_epic_color(fields)
    assert result["carry_over"] is None


def test_carry_over_ignores_non_cycle_labels():
    """Non-cycle labels (e.g. 'ComponentPlatform', 'Major') must not inflate carry_over."""
    fields = {"status": {"name": "In Progress"}, "labels": ["26.04", "ComponentPlatform", "Major", "SSDLC"]}
    result = calculate_epic_color(fields)
    assert result["carry_over"] is None


def test_carry_over_with_mixed_labels():
    """Only XX.XX labels count toward carry_over, non-cycle labels are ignored."""
    fields = {"status": {"name": "In Progress"}, "labels": ["24.04", "25.10", "ComponentPlatform", "Major"]}
    result = calculate_epic_color(fields)
    assert result["carry_over"] == {"color": "purple", "count": 1}


def test_dropped_state():
    fields = {
        "status": {"name": "Open"},
        "customfield_10968": {"value": "Dropped"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "black"


def test_added_state():
    fields = {
        "status": {"name": "Open"},
        "customfield_10968": {"value": "Added"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "blue"


def test_no_labels_field():
    """Missing labels key should not blow up."""
    fields = {"status": {"name": "Open"}}
    result = calculate_epic_color(fields)
    assert result["carry_over"] is None


def test_done_with_carry_over():
    """Done + multiple labels → green completed + carry-over badge."""
    fields = {"status": {"name": "Done"}, "labels": ["24.04", "25.10", "25.04"]}
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "green", "label": "C"}
    assert result["carry_over"] == {"color": "purple", "count": 2}


def test_emoji_at_risk_state():
    """Emoji-prefixed '🟧 At Risk' should be treated as 'At Risk'."""
    fields = {
        "status": {"name": "In Progress"},
        "customfield_10968": {"value": "🟧 At Risk"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "orange"


def test_emoji_excluded_state():
    """Emoji-prefixed '🟥 Excluded' should be treated as 'Excluded'."""
    fields = {
        "status": {"name": "Open"},
        "customfield_10968": {"value": "🟥 Excluded"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "red"


def test_emoji_added_state():
    """Emoji-prefixed '🟦 Added' should be treated as 'Added'."""
    fields = {
        "status": {"name": "Open"},
        "customfield_10968": {"value": "🟦 Added"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "blue"


def test_emoji_dropped_state():
    """Emoji-prefixed '⬛ Dropped' should be treated as 'Dropped'."""
    fields = {
        "status": {"name": "Open"},
        "customfield_10968": {"value": "⬛ Dropped"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "black"


# ---------------------------------------------------------------------------
# carry-over with current_cycle (chronological / prior-cycle counting)
# ---------------------------------------------------------------------------


def test_carry_over_current_cycle_three_labels():
    """Labels 25.04, 25.10, 26.04 viewed in 26.04 → carry-over count 2."""
    fields = {"status": {"name": "In Progress"}, "labels": ["25.04", "25.10", "26.04"]}
    result = calculate_epic_color(fields, current_cycle="26.04")
    assert result["carry_over"] == {"color": "purple", "count": 2}


def test_carry_over_current_cycle_two_labels():
    """Labels 25.10, 26.04 viewed in 26.04 → carry-over count 1."""
    fields = {"status": {"name": "In Progress"}, "labels": ["25.10", "26.04"]}
    result = calculate_epic_color(fields, current_cycle="26.04")
    assert result["carry_over"] == {"color": "purple", "count": 1}


def test_carry_over_current_cycle_single_label():
    """Single label 26.04 viewed in 26.04 → no carry-over."""
    fields = {"status": {"name": "In Progress"}, "labels": ["26.04"]}
    result = calculate_epic_color(fields, current_cycle="26.04")
    assert result["carry_over"] is None


def test_carry_over_current_cycle_four_labels():
    """Labels 25.04, 25.10, 26.04, 26.10 viewed across different cycles."""
    fields = {"status": {"name": "In Progress"}, "labels": ["25.04", "25.10", "26.04", "26.10"]}

    # Viewed in 25.10 → 1 prior cycle (25.04)
    result = calculate_epic_color(fields, current_cycle="25.10")
    assert result["carry_over"] == {"color": "purple", "count": 1}

    # Viewed in 26.04 → 2 prior cycles (25.04, 25.10)
    result = calculate_epic_color(fields, current_cycle="26.04")
    assert result["carry_over"] == {"color": "purple", "count": 2}

    # Viewed in 26.10 → 3 prior cycles (25.04, 25.10, 26.04)
    result = calculate_epic_color(fields, current_cycle="26.10")
    assert result["carry_over"] == {"color": "purple", "count": 3}


def test_carry_over_current_cycle_first_appearance():
    """Viewing the earliest cycle label → no carry-over."""
    fields = {"status": {"name": "In Progress"}, "labels": ["25.04", "25.10", "26.04"]}
    result = calculate_epic_color(fields, current_cycle="25.04")
    assert result["carry_over"] is None


def test_carry_over_current_cycle_ignores_non_cycle_labels():
    """Non-cycle labels don't affect carry-over count with current_cycle."""
    fields = {"status": {"name": "In Progress"}, "labels": ["25.04", "26.04", "ComponentPlatform", "Major"]}
    result = calculate_epic_color(fields, current_cycle="26.04")
    assert result["carry_over"] == {"color": "purple", "count": 1}


# ---------------------------------------------------------------------------
# Done overrides roadmap_state (highest priority)
# ---------------------------------------------------------------------------


def test_done_overrides_at_risk():
    """Done + At Risk → green completed (Done has highest priority)."""
    fields = {
        "status": {"name": "Done"},
        "customfield_10968": {"value": "At Risk"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "green", "label": "C"}


def test_done_overrides_excluded():
    """Done + Excluded → green completed (Done has highest priority)."""
    fields = {
        "status": {"name": "Done"},
        "customfield_10968": {"value": "🟥 Excluded"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "green", "label": "C"}


def test_done_overrides_dropped():
    """Done + Dropped → green completed (Done has highest priority)."""
    fields = {
        "status": {"name": "Done"},
        "customfield_10968": {"value": "Dropped"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "green", "label": "C"}


def test_done_with_added_state():
    """Done + Added → blue completed (special case: blue + 'C' label)."""
    fields = {
        "status": {"name": "Done"},
        "customfield_10968": {"value": "Added"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "blue", "label": "C"}


def test_done_with_added_emoji_state():
    """Done + 🟦 Added → blue completed."""
    fields = {
        "status": {"name": "Done"},
        "customfield_10968": {"value": "🟦 Added"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"] == {"color": "blue", "label": "C"}


# ---------------------------------------------------------------------------
# Rejected overrides orange and blue, but not black (Dropped)
# ---------------------------------------------------------------------------


def test_rejected_overrides_at_risk():
    """Rejected + At Risk → red (Rejected wins over orange)."""
    fields = {
        "status": {"name": "Rejected"},
        "customfield_10968": {"value": "At Risk"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "red"


def test_rejected_overrides_added():
    """Rejected + Added → red (Rejected wins over blue)."""
    fields = {
        "status": {"name": "Rejected"},
        "customfield_10968": {"value": "🟦 Added"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "red"


def test_rejected_does_not_override_dropped():
    """Rejected + Dropped → black (Dropped is preserved)."""
    fields = {
        "status": {"name": "Rejected"},
        "customfield_10968": {"value": "Dropped"},
        "labels": [],
    }
    result = calculate_epic_color(fields)
    assert result["health"]["color"] == "black"
