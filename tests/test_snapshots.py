"""Tests for the daily snapshot and diff reporting feature."""

from datetime import date

from psycopg.types.json import Jsonb

from src.database import get_db_connection
from src.jira_sync import take_daily_snapshot


def _get_uncategorized_id() -> int:
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product WHERE name = 'Uncategorized'")
        return cur.fetchone()[0]


def _insert_roadmap_item(
    jira_key: str,
    title: str,
    status: str,
    color: str,
    product_id: int | None = None,
    tags: list[str] | None = None,
    parent_key: str | None = None,
    parent_summary: str | None = None,
    release: str | None = None,
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
                 parent_key, parent_summary, release)
            VALUES (%s, %s, %s, %s, %s, %s, '', %s, %s, %s)
            ON CONFLICT (jira_key) DO UPDATE SET
                title = EXCLUDED.title,
                status = EXCLUDED.status,
                color_status = EXCLUDED.color_status,
                product_id = EXCLUDED.product_id,
                tags = EXCLUDED.tags,
                parent_key = EXCLUDED.parent_key,
                parent_summary = EXCLUDED.parent_summary,
                release = EXCLUDED.release,
                updated_at = now()
            """,
            (jira_key, title, status, color_status, product_id, tags or [], parent_key, parent_summary, release),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# take_daily_snapshot tests
# ---------------------------------------------------------------------------


def test_snapshot_creates_rows():
    """A snapshot captures all current roadmap_item rows."""
    _insert_roadmap_item("SNAP-1", "First item", "In Progress", "green")
    _insert_roadmap_item("SNAP-2", "Second item", "Open", "yellow")

    count = take_daily_snapshot(snapshot_date=date(2026, 2, 10))
    assert count == 2

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT jira_key, title, color, status FROM roadmap_snapshot "
            "WHERE snapshot_date = '2026-02-10' ORDER BY jira_key"
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    assert rows[0] == ("SNAP-1", "First item", "green", "In Progress")
    assert rows[1] == ("SNAP-2", "Second item", "yellow", "Open")


def test_snapshot_is_idempotent():
    """Taking a snapshot twice for the same date returns 0 the second time."""
    _insert_roadmap_item("SNAP-3", "Item", "Open", "green")

    first = take_daily_snapshot(snapshot_date=date(2026, 2, 11))
    second = take_daily_snapshot(snapshot_date=date(2026, 2, 11))

    assert first == 1
    assert second == 0

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM roadmap_snapshot WHERE snapshot_date = '2026-02-11'")
        assert cur.fetchone()[0] == 1


def test_snapshot_captures_product_info():
    """Snapshot denormalizes product name and department."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('Juju', 'Infra') RETURNING id")
        juju_id = cur.fetchone()[0]
        conn.commit()

    _insert_roadmap_item("SNAP-4", "Juju epic", "Open", "green", product_id=juju_id)

    take_daily_snapshot(snapshot_date=date(2026, 2, 12))

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT product_name, department FROM roadmap_snapshot "
            "WHERE jira_key = 'SNAP-4' AND snapshot_date = '2026-02-12'"
        )
        row = cur.fetchone()

    assert row == ("Juju", "Infra")


def test_snapshot_empty_table():
    """Snapshot on an empty roadmap_item table inserts 0 rows."""
    count = take_daily_snapshot(snapshot_date=date(2026, 2, 13))
    assert count == 0


def test_snapshot_different_dates():
    """Snapshots on different dates are independent."""
    _insert_roadmap_item("SNAP-5", "Item v1", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 14))

    # Change the item and snapshot the next day
    _insert_roadmap_item("SNAP-5", "Item v2", "In Progress", "red")
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT title, color FROM roadmap_snapshot WHERE jira_key = 'SNAP-5' ORDER BY snapshot_date")
        rows = cur.fetchall()

    assert rows[0] == ("Item v1", "green")
    assert rows[1] == ("Item v2", "red")


# ---------------------------------------------------------------------------
# Diff endpoint tests
# ---------------------------------------------------------------------------


def test_diff_turned_red(client):
    """Items that changed to red are reported in turned_red."""
    _insert_roadmap_item("DIFF-1", "Went red", "Open", "green")
    _insert_roadmap_item("DIFF-2", "Stayed green", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 1))

    # Change DIFF-1 to red, snapshot again
    _insert_roadmap_item("DIFF-1", "Went red", "Open", "red")
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    resp = client.get(
        "/api/v1/snapshots/diff",
        params={
            "from_date": "2026-02-01",
            "to_date": "2026-02-15",
        },
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["summary"]["turned_red"] == 1
    assert body["turned_red"][0]["jira_key"] == "DIFF-1"
    assert body["turned_red"][0]["old_color"] == "green"
    assert body["turned_red"][0]["new_color"] == "red"

    # DIFF-2 should not appear in color_changes
    changed_keys = [c["jira_key"] for c in body["color_changes"]]
    assert "DIFF-2" not in changed_keys


def test_diff_disappeared(client):
    """Items present in old snapshot but missing in new are reported as disappeared."""
    _insert_roadmap_item("GONE-1", "Will vanish", "Open", "green")
    _insert_roadmap_item("STAY-1", "Will stay", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 1))

    # Remove GONE-1 from roadmap_item, then snapshot
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM roadmap_item WHERE jira_key = 'GONE-1'")
        conn.commit()
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    resp = client.get(
        "/api/v1/snapshots/diff",
        params={
            "from_date": "2026-02-01",
            "to_date": "2026-02-15",
        },
    )
    body = resp.json()

    assert body["summary"]["disappeared"] == 1
    assert body["disappeared"][0]["jira_key"] == "GONE-1"


def test_diff_appeared(client):
    """Items present in new snapshot but missing in old are reported as appeared."""
    _insert_roadmap_item("OLD-1", "Was here", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 1))

    _insert_roadmap_item("NEW-1", "Just added", "Open", "yellow")
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    resp = client.get(
        "/api/v1/snapshots/diff",
        params={
            "from_date": "2026-02-01",
            "to_date": "2026-02-15",
        },
    )
    body = resp.json()

    assert body["summary"]["appeared"] == 1
    assert body["appeared"][0]["jira_key"] == "NEW-1"


def test_diff_color_changes(client):
    """All color changes are captured, not just turned_red."""
    _insert_roadmap_item("CC-1", "Green to yellow", "Open", "green")
    _insert_roadmap_item("CC-2", "Red to green", "Open", "red")
    take_daily_snapshot(snapshot_date=date(2026, 2, 1))

    _insert_roadmap_item("CC-1", "Green to yellow", "Open", "yellow")
    _insert_roadmap_item("CC-2", "Red to green", "Done", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    resp = client.get(
        "/api/v1/snapshots/diff",
        params={
            "from_date": "2026-02-01",
            "to_date": "2026-02-15",
        },
    )
    body = resp.json()

    assert body["summary"]["color_changes"] == 2
    assert body["summary"]["turned_red"] == 0


def test_diff_missing_snapshot_returns_404(client):
    """Requesting a diff with a non-existent date returns 404."""
    resp = client.get(
        "/api/v1/snapshots/diff",
        params={
            "from_date": "2020-01-01",
            "to_date": "2020-01-15",
        },
    )
    assert resp.status_code == 404


def test_list_snapshots(client):
    """GET /api/v1/snapshots lists available snapshot dates."""
    _insert_roadmap_item("LS-1", "Item", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 1))

    _insert_roadmap_item("LS-2", "Item 2", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    resp = client.get("/api/v1/snapshots")
    assert resp.status_code == 200
    body = resp.json()

    assert body["meta"]["total"] == 2
    dates = [s["date"] for s in body["data"]]
    assert "2026-02-15" in dates
    assert "2026-02-01" in dates
    # Newest first
    assert dates[0] == "2026-02-15"


def test_diff_no_changes(client):
    """When nothing changed between snapshots, all lists are empty."""
    _insert_roadmap_item("NC-1", "Stable", "Open", "green")
    take_daily_snapshot(snapshot_date=date(2026, 2, 1))
    # Same data, different date
    take_daily_snapshot(snapshot_date=date(2026, 2, 15))

    resp = client.get(
        "/api/v1/snapshots/diff",
        params={
            "from_date": "2026-02-01",
            "to_date": "2026-02-15",
        },
    )
    body = resp.json()

    assert body["summary"]["turned_red"] == 0
    assert body["summary"]["color_changes"] == 0
    assert body["summary"]["disappeared"] == 0
    assert body["summary"]["appeared"] == 0
