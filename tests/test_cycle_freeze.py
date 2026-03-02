"""Tests for the cycle freeze / unfreeze feature."""

import json

import pytest

from src.database import get_db_connection
from src.jira_sync import freeze_cycle, get_frozen_cycles, unfreeze_cycle


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
    color_status = json.dumps({"health": {"color": color}, "carry_over": None})
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


# ---------------------------------------------------------------------------
# freeze_cycle unit tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# unfreeze_cycle unit tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# get_frozen_cycles unit tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


def test_api_freeze_cycle(client):
    """POST /api/v1/cycles/{cycle}/freeze creates a freeze."""
    pid = _insert_product("TestProd")
    _insert_roadmap_item("AF-1", "Item", "Open", "green", pid, tags=["25.10"])

    resp = client.post("/api/v1/cycles/25.10/freeze", json={"note": "End of cycle"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["items_captured"] == 1
    assert "frozen" in body["message"].lower()


def test_api_freeze_already_frozen_returns_409(client):
    """POST freeze on an already-frozen cycle returns 409."""
    _insert_roadmap_item("AF2-1", "Item", "Open", "green", tags=["25.10"])
    client.post("/api/v1/cycles/25.10/freeze")

    resp = client.post("/api/v1/cycles/25.10/freeze")
    assert resp.status_code == 409


def test_api_freeze_invalid_cycle_returns_400(client):
    """POST freeze with an invalid cycle label returns 400."""
    resp = client.post("/api/v1/cycles/bad/freeze")
    assert resp.status_code == 400


def test_api_unfreeze_cycle(client):
    """DELETE /api/v1/cycles/{cycle}/freeze removes the freeze."""
    _insert_roadmap_item("UFA-1", "Item", "Open", "green", tags=["25.10"])
    client.post("/api/v1/cycles/25.10/freeze")

    resp = client.delete("/api/v1/cycles/25.10/freeze")
    assert resp.status_code == 200
    assert "unfrozen" in resp.json()["message"].lower()


def test_api_unfreeze_not_frozen_returns_404(client):
    """DELETE unfreeze on a non-frozen cycle returns 404."""
    resp = client.delete("/api/v1/cycles/25.10/freeze")
    assert resp.status_code == 404


def test_api_list_cycles(client):
    """GET /api/v1/cycles lists cycles with freeze status."""
    pid = _insert_product("P")
    _insert_roadmap_item("LC-1", "A", "Open", "green", pid, tags=["25.10"])
    _insert_roadmap_item("LC-2", "B", "Open", "green", pid, tags=["26.04"])
    freeze_cycle("25.10")

    resp = client.get("/api/v1/cycles")
    assert resp.status_code == 200
    body = resp.json()

    cycles_by_name = {c["cycle"]: c for c in body["data"]}
    assert cycles_by_name["25.10"]["frozen"] is True
    assert cycles_by_name["26.04"]["frozen"] is False


def test_api_get_frozen_cycle_items(client):
    """GET /api/v1/cycles/{cycle}/items returns frozen items."""
    pid = _insert_product("MicroK8s")
    _insert_roadmap_item("FCI-1", "Frozen item", "Done", "green", pid, tags=["25.10"])
    freeze_cycle("25.10")

    resp = client.get("/api/v1/cycles/25.10/items")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 1
    assert body["data"][0]["jira_key"] == "FCI-1"


def test_api_get_frozen_items_not_frozen_returns_404(client):
    """GET items for a non-frozen cycle returns 404."""
    resp = client.get("/api/v1/cycles/25.10/items")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Roadmap page integration — frozen cycle serves frozen data
# ---------------------------------------------------------------------------


def test_roadmap_page_frozen_cycle_shows_frozen_data(client):
    """When a cycle is frozen, the roadmap page shows the frozen snapshot, not live data."""
    pid = _insert_product("MyProd")
    _insert_roadmap_item(
        "RP-1", "Original title", "In Progress", "green", pid,
        tags=["25.10"], parent_key="OBJ-1", parent_summary="Objective A",
    )

    # Freeze the cycle
    freeze_cycle("25.10")

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
    freeze_cycle("25.10")

    resp = client.get("/", params={"product": "P", "cycle": "25.10"})
    assert resp.status_code == 200
    assert "Frozen" in resp.text
    assert "🔒" in resp.text


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
