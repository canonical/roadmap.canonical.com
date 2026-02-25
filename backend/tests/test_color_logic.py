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
