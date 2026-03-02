"""Tests for the cycle lifecycle management (cycle_config + freeze/unfreeze)."""

import json

import pytest
from psycopg.types.json import Jsonb

from src.database import get_db_connection
from src.jira_sync import (
    freeze_cycle,
    get_cycle_configs,
    get_frozen_cycles,
    register_cycle,
    remove_cycle,
    set_cycle_state,
    unfreeze_cycle,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_uncategorized_id() -> int:
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product WHERE name = 'Uncategorized'")
        return cur.fetchone()[0]


def _insert_product(name: str, department: str = "TestDept") -> int:
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO product (name, department) VALUES (%s, %s) RETURNING id",
            (name, department),
        )
        pid = cur.fetchone()[0]
        conn.commit()
    return pid


def _insert_roadmap_item(
    jira_key: str,
    title: str,
    status: str,
    color: str,
    product_id: int | None = None,
    tags: list[str] | None = None,
    parent_key: str | None = None,
    parent_summary: str | None = None,
) -> None:
    """Insert a roadmap_item row for testing."""
    color_status = Jsonb({"health": {"color": color}, "carry_over": None})
    if product_id is None:
        product_id = _get_uncategorized_id()
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO roadmap_item
                (jira_key, title, status, color_status, product_id, tags, url,
                 parent_key, parent_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (jira_key) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                color_status = EXCLUDED.color_status,
                product_id = EXCLUDED.product_id,
                tags = EXCLUDED.tags,
                parent_key = EXCLUDED.parent_key,
                parent_summary = EXCLUDED.parent_summary,
                updated_at = now()
            """,
            (
                jira_key, title, status, color_status, product_id,
                tags or [], f"https://jira.test/browse/{jira_key}",
                parent_key, parent_summary,
            ),
        )
        conn.commit()


# ===========================================================================
# Legacy freeze_cycle / unfreeze_cycle unit tests (still used internally)
# ===========================================================================


def test_freeze_captures_items_with_cycle_tag():
    """Freezing a cycle captures all items tagged with that cycle label."""
    pid = _insert_product("MyProduct")
    _insert_roadmap_item("FC-1", "Item one", "In Progress", "green", pid, tags=["25.10"])
    _insert_roadmap_item("FC-2", "Item two", "Open", "orange", pid, tags=["25.10", "26.04"])
    _insert_roadmap_item("FC-3", "Other cycle", "Open", "green", pid, tags=["26.04"])

    count = freeze_cycle("25.10", frozen_by="test@example.com", note="Q4 review")
    assert count == 2  # FC-1 and FC-2, not FC-3

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT jira_key FROM cycle_freeze_item WHERE cycle = '25.10' ORDER BY jira_key"
        )
        keys = [r[0] for r in cur.fetchall()]
    assert keys == ["FC-1", "FC-2"]


def test_freeze_captures_product_info():
    """Frozen items have denormalized product name and department."""
    pid = _insert_product("Juju", department="Infra")
    _insert_roadmap_item("FP-1", "Juju epic", "Open", "green", pid, tags=["25.10"])

    freeze_cycle("25.10")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT product_name, department FROM cycle_freeze_item "
            "WHERE jira_key = 'FP-1' AND cycle = '25.10'"
        )
        row = cur.fetchone()
    assert row == ("Juju", "Infra")


def test_freeze_captures_color_status():
    """Frozen items preserve the color_status JSON from freeze time."""
    pid = _insert_product("LXD")
    _insert_roadmap_item("FCS-1", "Colored item", "In Progress", "orange", pid, tags=["25.10"])

    freeze_cycle("25.10")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT color_status FROM cycle_freeze_item "
            "WHERE jira_key = 'FCS-1' AND cycle = '25.10'"
        )
        cs = cur.fetchone()[0]
    assert cs["health"]["color"] == "orange"


def test_freeze_captures_objective():
    """Frozen items include parent_key and parent_summary."""
    pid = _insert_product("Snap")
    _insert_roadmap_item(
        "FO-1", "Child epic", "Open", "green", pid, tags=["25.10"],
        parent_key="OBJ-100", parent_summary="Big Objective",
    )

    freeze_cycle("25.10")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT parent_key, parent_summary FROM cycle_freeze_item "
            "WHERE jira_key = 'FO-1' AND cycle = '25.10'"
        )
        row = cur.fetchone()
    assert row == ("OBJ-100", "Big Objective")


def test_freeze_already_frozen_raises():
    """Freezing a cycle that is already frozen raises RuntimeError."""
    _insert_roadmap_item("AF-1", "Item", "Open", "green", tags=["25.10"])
    freeze_cycle("25.10")

    with pytest.raises(RuntimeError, match="already frozen"):
        freeze_cycle("25.10")


def test_freeze_invalid_cycle_label_raises():
    """Freeze rejects non-XX.XX cycle labels."""
    with pytest.raises(ValueError, match="Invalid cycle label"):
        freeze_cycle("not-a-cycle")


def test_freeze_empty_cycle():
    """Freezing a cycle with no matching items creates 0 rows but still creates the freeze record."""
    count = freeze_cycle("99.99")
    assert count == 0

    frozen = get_frozen_cycles()
    assert "99.99" in frozen


def test_unfreeze_removes_frozen_data():
    """Unfreezing deletes the freeze header and all associated items."""
    _insert_roadmap_item("UF-1", "Item", "Open", "green", tags=["25.10"])
    freeze_cycle("25.10")

    unfreeze_cycle("25.10")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM cycle_freeze WHERE cycle = '25.10'")
        assert cur.fetchone() is None
        cur.execute("SELECT count(*) FROM cycle_freeze_item WHERE cycle = '25.10'")
        assert cur.fetchone()[0] == 0


def test_unfreeze_non_frozen_raises():
    """Unfreezing a cycle that isn't frozen raises ValueError."""
    with pytest.raises(ValueError, match="not frozen"):
        unfreeze_cycle("25.10")


def test_get_frozen_cycles():
    """get_frozen_cycles returns metadata for all frozen cycles."""
    _insert_roadmap_item("GF-1", "A", "Open", "green", tags=["25.04"])
    _insert_roadmap_item("GF-2", "B", "Open", "green", tags=["25.10"])
    freeze_cycle("25.04", frozen_by="alice@test.com", note="H1 review")
    freeze_cycle("25.10")

    frozen = get_frozen_cycles()
    assert "25.04" in frozen
    assert "25.10" in frozen
    assert frozen["25.04"]["frozen_by"] == "alice@test.com"
    assert frozen["25.04"]["note"] == "H1 review"


# ===========================================================================
# cycle_config unit tests — register / set_state / remove
# ===========================================================================


def test_register_cycle_creates_config():
    """register_cycle creates a cycle_config row."""
    result = register_cycle("26.04", state="future", updated_by="bob@test.com")
    assert result["cycle"] == "26.04"
    assert result["state"] == "future"

    configs = get_cycle_configs()
    assert "26.04" in configs
    assert configs["26.04"]["state"] == "future"


def test_register_cycle_as_current():
    """Register a cycle directly as current."""
    register_cycle("26.04", state="current")
    configs = get_cycle_configs()
    assert configs["26.04"]["state"] == "current"


def test_register_cycle_as_frozen_creates_snapshot():
    """Registering a cycle as frozen also creates the freeze snapshot."""
    pid = _insert_product("TestP")
    _insert_roadmap_item("RC-1", "Item", "Open", "green", pid, tags=["25.10"])

    register_cycle("25.10", state="frozen")

    # cycle_config should exist
    configs = get_cycle_configs()
    assert configs["25.10"]["state"] == "frozen"

    # cycle_freeze should also exist (side effect)
    frozen = get_frozen_cycles()
    assert "25.10" in frozen

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cycle_freeze_item WHERE cycle = '25.10'")
        assert cur.fetchone()[0] == 1


def test_register_duplicate_raises():
    """Registering a cycle that already exists raises RuntimeError."""
    register_cycle("26.04", state="future")
    with pytest.raises(RuntimeError, match="already registered"):
        register_cycle("26.04", state="future")


def test_register_invalid_label_raises():
    """Invalid cycle labels are rejected."""
    with pytest.raises(ValueError, match="Invalid cycle label"):
        register_cycle("bad", state="future")


def test_register_invalid_state_raises():
    """Invalid state values are rejected."""
    with pytest.raises(ValueError, match="Invalid state"):
        register_cycle("26.04", state="expired")


def test_register_second_current_raises():
    """Cannot register a second cycle as current when one already exists."""
    register_cycle("26.04", state="current")
    with pytest.raises(RuntimeError, match="already current"):
        register_cycle("26.10", state="current")


def test_set_cycle_state_future_to_current():
    """Transition a cycle from future to current."""
    register_cycle("26.04", state="future")
    result = set_cycle_state("26.04", "current")
    assert result["state"] == "current"

    configs = get_cycle_configs()
    assert configs["26.04"]["state"] == "current"


def test_set_cycle_state_current_to_frozen():
    """Transition a cycle from current to frozen — creates freeze snapshot."""
    pid = _insert_product("P")
    _insert_roadmap_item("CS-1", "Item", "Open", "green", pid, tags=["26.04"])

    register_cycle("26.04", state="current")
    set_cycle_state("26.04", "frozen")

    configs = get_cycle_configs()
    assert configs["26.04"]["state"] == "frozen"

    frozen = get_frozen_cycles()
    assert "26.04" in frozen

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM cycle_freeze_item WHERE cycle = '26.04'")
        assert cur.fetchone()[0] == 1


def test_set_cycle_state_frozen_to_current_deletes_snapshot():
    """Transition from frozen to current — deletes the freeze snapshot."""
    pid = _insert_product("P")
    _insert_roadmap_item("FTC-1", "Item", "Open", "green", pid, tags=["26.04"])

    register_cycle("26.04", state="frozen")

    # Verify snapshot exists
    frozen = get_frozen_cycles()
    assert "26.04" in frozen

    set_cycle_state("26.04", "current")

    # Snapshot should be gone
    frozen = get_frozen_cycles()
    assert "26.04" not in frozen


def test_set_cycle_state_noop():
    """Setting same state is a no-op."""
    register_cycle("26.04", state="future")
    result = set_cycle_state("26.04", "future")
    assert result["state"] == "future"


def test_set_cycle_state_at_most_one_current():
    """Cannot set a second cycle to current."""
    register_cycle("26.04", state="current")
    register_cycle("26.10", state="future")

    with pytest.raises(RuntimeError, match="already current"):
        set_cycle_state("26.10", "current")


def test_set_cycle_state_zero_current_allowed():
    """Moving current cycle to frozen leaves zero current — that's OK."""
    pid = _insert_product("P")
    _insert_roadmap_item("ZC-1", "Item", "Open", "green", pid, tags=["26.04"])

    register_cycle("26.04", state="current")
    # Move to frozen — no current cycles at all
    set_cycle_state("26.04", "frozen")

    configs = get_cycle_configs()
    current_cycles = [c for c, cfg in configs.items() if cfg["state"] == "current"]
    assert len(current_cycles) == 0


def test_set_cycle_state_not_registered_raises():
    """Cannot change state of an unregistered cycle."""
    with pytest.raises(ValueError, match="not registered"):
        set_cycle_state("99.99", "frozen")


def test_set_cycle_state_invalid_state_raises():
    """Invalid target state is rejected."""
    register_cycle("26.04", state="future")
    with pytest.raises(ValueError, match="Invalid state"):
        set_cycle_state("26.04", "invalid")


def test_remove_cycle():
    """remove_cycle deletes the config entry."""
    register_cycle("26.04", state="future")
    remove_cycle("26.04")

    configs = get_cycle_configs()
    assert "26.04" not in configs


def test_remove_frozen_cycle_also_removes_snapshot():
    """Removing a frozen cycle also deletes the freeze snapshot."""
    pid = _insert_product("P")
    _insert_roadmap_item("RMF-1", "Item", "Open", "green", pid, tags=["26.04"])

    register_cycle("26.04", state="frozen")
    remove_cycle("26.04")

    configs = get_cycle_configs()
    assert "26.04" not in configs

    frozen = get_frozen_cycles()
    assert "26.04" not in frozen


def test_remove_unregistered_raises():
    """Cannot remove an unregistered cycle."""
    with pytest.raises(ValueError, match="not registered"):
        remove_cycle("99.99")


# ===========================================================================
# API endpoint tests — new cycle_config endpoints
# ===========================================================================


def test_api_register_cycle(client):
    """POST /api/v1/cycles/{cycle} registers a new cycle."""
    resp = client.post("/api/v1/cycles/26.10", json={"state": "future"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["cycle"] == "26.10"
    assert body["state"] == "future"


def test_api_register_cycle_default_state(client):
    """POST /api/v1/cycles/{cycle} without body defaults to future."""
    resp = client.post("/api/v1/cycles/26.10")
    assert resp.status_code == 201
    assert resp.json()["state"] == "future"


def test_api_register_duplicate_returns_409(client):
    """POST register on an already-registered cycle returns 409."""
    client.post("/api/v1/cycles/26.10", json={"state": "future"})
    resp = client.post("/api/v1/cycles/26.10", json={"state": "future"})
    assert resp.status_code == 409


def test_api_register_invalid_label_returns_400(client):
    """POST register with an invalid cycle label returns 400."""
    resp = client.post("/api/v1/cycles/bad", json={"state": "future"})
    assert resp.status_code == 400


def test_api_set_cycle_state(client):
    """PUT /api/v1/cycles/{cycle} changes state."""
    client.post("/api/v1/cycles/26.10", json={"state": "future"})
    resp = client.put("/api/v1/cycles/26.10", json={"state": "current"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "current"


def test_api_set_state_not_registered_returns_400(client):
    """PUT on an unregistered cycle returns 400."""
    resp = client.put("/api/v1/cycles/99.99", json={"state": "current"})
    assert resp.status_code == 400


def test_api_set_state_conflict_returns_409(client):
    """PUT setting second current returns 409."""
    client.post("/api/v1/cycles/26.04", json={"state": "current"})
    client.post("/api/v1/cycles/26.10", json={"state": "future"})
    resp = client.put("/api/v1/cycles/26.10", json={"state": "current"})
    assert resp.status_code == 409


def test_api_remove_cycle(client):
    """DELETE /api/v1/cycles/{cycle} removes the cycle."""
    client.post("/api/v1/cycles/26.10", json={"state": "future"})
    resp = client.delete("/api/v1/cycles/26.10")
    assert resp.status_code == 200
    assert "removed" in resp.json()["message"].lower()


def test_api_remove_not_registered_returns_404(client):
    """DELETE on an unregistered cycle returns 404."""
    resp = client.delete("/api/v1/cycles/99.99")
    assert resp.status_code == 404


def test_api_list_cycles_with_states(client):
    """GET /api/v1/cycles lists cycles with their state."""
    pid = _insert_product("P")
    _insert_roadmap_item("LC-1", "A", "Open", "green", pid, tags=["25.10"])
    _insert_roadmap_item("LC-2", "B", "Open", "green", pid, tags=["26.04"])

    register_cycle("25.10", state="frozen")
    register_cycle("26.04", state="current")

    resp = client.get("/api/v1/cycles")
    assert resp.status_code == 200
    body = resp.json()

    cycles_by_name = {c["cycle"]: c for c in body["data"]}
    assert cycles_by_name["25.10"]["state"] == "frozen"
    assert cycles_by_name["26.04"]["state"] == "current"


def test_api_list_cycles_includes_unregistered(client):
    """GET /api/v1/cycles includes live Jira cycles that aren't registered."""
    pid = _insert_product("P")
    _insert_roadmap_item("LU-1", "A", "Open", "green", pid, tags=["27.04"])

    resp = client.get("/api/v1/cycles")
    body = resp.json()
    cycles_by_name = {c["cycle"]: c for c in body["data"]}
    assert "27.04" in cycles_by_name
    assert cycles_by_name["27.04"]["state"] is None  # not registered


def test_api_get_frozen_cycle_items(client):
    """GET /api/v1/cycles/{cycle}/items returns frozen items."""
    pid = _insert_product("MicroK8s")
    _insert_roadmap_item("FCI-1", "Frozen item", "Done", "green", pid, tags=["25.10"])
    register_cycle("25.10", state="frozen")

    resp = client.get("/api/v1/cycles/25.10/items")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["jira_key"] == "FCI-1"


def test_api_get_frozen_items_not_frozen_returns_404(client):
    """GET items for a non-frozen cycle returns 404."""
    resp = client.get("/api/v1/cycles/25.10/items")
    assert resp.status_code == 404


# ===========================================================================
# Roadmap page integration — state-based display
# ===========================================================================


def test_roadmap_page_frozen_cycle_shows_frozen_data(client):
    """When a cycle is frozen, the roadmap page shows the frozen snapshot, not live data."""
    pid = _insert_product("MyProd")
    _insert_roadmap_item(
        "RP-1", "Original title", "In Progress", "green", pid,
        tags=["25.10"], parent_key="OBJ-1", parent_summary="Objective A",
    )

    # Freeze the cycle via cycle_config
    register_cycle("25.10", state="frozen")

    # Now change the live data (simulating a Jira sync after freeze)
    _insert_roadmap_item(
        "RP-1", "Updated title after freeze", "Done", "red", pid,
        tags=["25.10"], parent_key="OBJ-1", parent_summary="Objective A",
    )

    # The page should still show the frozen (original) data
    resp = client.get("/", params={"product": "MyProd", "cycle": "25.10"})
    assert resp.status_code == 200
    assert "Original title" in resp.text
    assert "Updated title after freeze" not in resp.text


def test_roadmap_page_frozen_badge(client):
    """Frozen cycles show a 🔒 Frozen badge on the page."""
    pid = _insert_product("P")
    _insert_roadmap_item("FB-1", "Item", "Open", "green", pid, tags=["25.10"])
    register_cycle("25.10", state="frozen")

    resp = client.get("/", params={"product": "P", "cycle": "25.10"})
    assert resp.status_code == 200
    assert "Frozen" in resp.text
    assert "🔒" in resp.text


def test_roadmap_page_future_cycle_shows_inactive(client):
    """Future cycles show all items as white/Inactive."""
    pid = _insert_product("FutureProd")
    _insert_roadmap_item(
        "FUT-1", "Future item", "In Progress", "green", pid,
        tags=["27.04"], parent_key="OBJ-F", parent_summary="Future Obj",
    )
    register_cycle("27.04", state="future")

    resp = client.get("/", params={"product": "FutureProd", "cycle": "27.04"})
    assert resp.status_code == 200
    assert "Future item" in resp.text
    assert "🔮" in resp.text
    assert "Future" in resp.text
    # Item should be rendered as white/Inactive (check for the color-cell--white class)
    assert "color-cell--white" in resp.text


def test_roadmap_page_future_badge(client):
    """Future cycles display a 🔮 Future badge in the heading."""
    pid = _insert_product("P")
    _insert_roadmap_item("FBadge-1", "Item", "Open", "green", pid, tags=["27.04"])
    register_cycle("27.04", state="future")

    resp = client.get("/", params={"product": "P", "cycle": "27.04"})
    assert resp.status_code == 200
    assert "🔮" in resp.text


def test_roadmap_page_current_badge(client):
    """Current cycle shows ▶ Current badge."""
    pid = _insert_product("P")
    _insert_roadmap_item("CB-1", "Item", "In Progress", "green", pid, tags=["26.04"])
    register_cycle("26.04", state="current")

    resp = client.get("/", params={"product": "P", "cycle": "26.04"})
    assert resp.status_code == 200
    assert "Current" in resp.text
    assert "▶" in resp.text


def test_roadmap_page_unfrozen_cycle_shows_live_data(client):
    """Non-frozen cycles continue to show live data from roadmap_item."""
    pid = _insert_product("LiveProd")
    _insert_roadmap_item(
        "UL-1", "Live title", "Open", "green", pid,
        tags=["26.04"], parent_key="OBJ-2", parent_summary="Objective B",
    )

    resp = client.get("/", params={"product": "LiveProd", "cycle": "26.04"})
    assert resp.status_code == 200
    assert "Live title" in resp.text
    # Should NOT have frozen badge
    assert "🔒" not in resp.text


def test_roadmap_page_carry_over_only_counts_frozen(client):
    """Carry-over count should only include frozen cycle labels."""
    pid = _insert_product("CarryProd")
    # Item appears in 3 cycles: 25.04 (frozen), 25.10 (frozen), 26.04 (current)
    _insert_roadmap_item(
        "CO-1", "Carry item", "In Progress", "green", pid,
        tags=["25.04", "25.10", "26.04"],
        parent_key="OBJ-CO", parent_summary="Carry Obj",
    )

    register_cycle("25.04", state="frozen")
    register_cycle("25.10", state="frozen")
    register_cycle("26.04", state="current")

    import asyncio

    from src.app import _query_roadmap_items

    loop = asyncio.new_event_loop()
    try:
        grouped, _, _ = loop.run_until_complete(
            _query_roadmap_items(product="CarryProd", cycle="26.04")
        )
    finally:
        loop.close()

    # The item in cycle 26.04 should have carry_over counting the 2 frozen cycles
    assert "26.04" in grouped
    items_26_04 = []
    for obj_items in grouped["26.04"].values():
        items_26_04.extend(obj_items)

    assert len(items_26_04) == 1
    co = items_26_04[0]["color_status"]["carry_over"]
    assert co is not None
    assert co["count"] == 2
    assert co["color"] == "purple"


def test_roadmap_page_carry_over_zero_when_no_frozen(client):
    """Carry-over is None when there are no frozen cycle labels on the item."""
    pid = _insert_product("NoCarryProd")
    _insert_roadmap_item(
        "NC-1", "No carry", "Open", "green", pid,
        tags=["26.04", "26.10"],
        parent_key="OBJ-NC", parent_summary="No Carry Obj",
    )

    register_cycle("26.04", state="current")
    register_cycle("26.10", state="future")

    import asyncio

    from src.app import _query_roadmap_items

    loop = asyncio.new_event_loop()
    try:
        grouped, _, _ = loop.run_until_complete(
            _query_roadmap_items(product="NoCarryProd", cycle="26.04")
        )
    finally:
        loop.close()

    assert "26.04" in grouped
    items = []
    for obj_items in grouped["26.04"].values():
        items.extend(obj_items)

    assert len(items) == 1
    assert items[0]["color_status"]["carry_over"] is None


# ===========================================================================
# Full lifecycle scenario test
# ===========================================================================


def test_full_lifecycle_scenario(client):
    """Test the complete cycle lifecycle: future → current → frozen."""
    pid = _insert_product("LifecycleProd")
    _insert_roadmap_item("LF-1", "Lifecycle item", "In Progress", "green", pid, tags=["26.04"])

    # 1. Register as future
    resp = client.post("/api/v1/cycles/26.04", json={"state": "future"})
    assert resp.status_code == 201

    # 2. Transition to current
    resp = client.put("/api/v1/cycles/26.04", json={"state": "current"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "current"

    # 3. Transition to frozen (creates snapshot)
    resp = client.put("/api/v1/cycles/26.04", json={"state": "frozen"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "frozen"

    # Verify snapshot exists
    resp = client.get("/api/v1/cycles/26.04/items")
    assert resp.status_code == 200
    assert resp.json()["meta"]["total"] == 1

    # 4. Can transition back to current (deletes snapshot)
    resp = client.put("/api/v1/cycles/26.04", json={"state": "current"})
    assert resp.status_code == 200

    resp = client.get("/api/v1/cycles/26.04/items")
    assert resp.status_code == 404  # no longer frozen

    # 5. Re-freeze and remove
    resp = client.put("/api/v1/cycles/26.04", json={"state": "frozen"})
    assert resp.status_code == 200

    resp = client.delete("/api/v1/cycles/26.04")
    assert resp.status_code == 200

    resp = client.get("/api/v1/cycles")
    cycles_by_name = {c["cycle"]: c for c in resp.json()["data"]}
    # 26.04 should still appear (live item exists) but as unregistered
    if "26.04" in cycles_by_name:
        assert cycles_by_name["26.04"]["state"] is None
