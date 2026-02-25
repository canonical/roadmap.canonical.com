"""Tests for /api/v1/* endpoints."""

import json

from src.database import get_db_connection


def test_roadmap_empty(client):
    """Empty DB returns an empty list."""
    resp = client.get("/api/v1/roadmap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["meta"]["total"] == 0


def test_roadmap_with_data(client):
    """Inserted row shows up in the roadmap response."""
    color = {"health": {"color": "green"}, "carry_over": None}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, description, status, release, tags, product, color_status, url)
                VALUES
                    ('TEST-1', 'Test Epic', 'A description', 'In Progress', '25.10',
                     ARRAY['roadmap'], 'Uncategorized', %s, 'http://jira/TEST-1')
                """,
                (json.dumps(color),),
            )
        conn.commit()

    resp = client.get("/api/v1/roadmap")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["jira_key"] == "TEST-1"
    assert data[0]["color_status"]["health"]["color"] == "green"


def test_roadmap_filter_by_status(client):
    """Filtering by status returns only matching items."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, st in [("A-1", "Done"), ("A-2", "In Progress")]:
                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, status, product, url)
                    VALUES (%s, %s, %s, 'Uncategorized', '')
                    """,
                    (key, f"Epic {key}", st),
                )
        conn.commit()

    resp = client.get("/api/v1/roadmap", params={"status": "Done"})
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["jira_key"] == "A-1"


def test_sync_endpoint(client):
    """POST /api/v1/sync returns a success message (actual sync is background)."""
    resp = client.post("/api/v1/sync")
    assert resp.status_code == 200
    assert "Sync started" in resp.json()["message"]


def test_status_endpoint(client):
    """GET /api/v1/status returns the status dict."""
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    assert "state" in resp.json()


# ---------------------------------------------------------------------------
# HTML page tests
# ---------------------------------------------------------------------------

def test_roadmap_page_empty(client):
    """GET / returns an HTML page even with no data."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Roadmap" in resp.text


def test_roadmap_page_with_data(client):
    """Items with cycle labels appear in the rendered HTML grouped by cycle then product."""
    color = json.dumps({"health": {"color": "green"}, "carry_over": None})
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product, color_status, url)
                VALUES
                    ('HTML-1', 'Render test', 'In Progress', ARRAY['25.10'],
                     'Uncategorized', %s, 'http://jira/HTML-1')
                """,
                (color,),
            )
        conn.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "HTML-1" in resp.text
    assert "Render test" in resp.text
    assert "Cycle 25.10" in resp.text
    assert "Uncategorized" in resp.text


def test_roadmap_page_hides_items_without_cycle(client):
    """Items that have no XX.XX cycle label are not shown."""
    color = json.dumps({"health": {"color": "white"}, "carry_over": None})
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product, color_status, url)
                VALUES
                    ('NO-1', 'No cycle', 'Open', ARRAY['SomeTag'], 'Uncategorized', %s, '')
                """,
                (color,),
            )
        conn.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "NO-1" not in resp.text


def test_roadmap_page_item_in_multiple_cycles(client):
    """An item with two cycle labels appears under both cycle headings."""
    color = json.dumps({"health": {"color": "green"}, "carry_over": {"color": "purple", "count": 1}})
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product, color_status, url)
                VALUES
                    ('MULTI-1', 'Multi-cycle', 'In Progress', ARRAY['25.10', '26.04'],
                     'Uncategorized', %s, 'http://jira/MULTI-1')
                """,
                (color,),
            )
        conn.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Cycle 26.04" in resp.text
    assert "Cycle 25.10" in resp.text
    # The item should appear twice (once per cycle)
    assert resp.text.count("MULTI-1") >= 2


def test_roadmap_page_filter_by_cycle(client):
    """Cycle filter shows only the selected cycle's items."""
    color = json.dumps({"health": {"color": "green"}, "carry_over": None})
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, tags in [("CY-1", ["25.10"]), ("CY-2", ["26.04"])]:
                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, status, tags, product, color_status, url)
                    VALUES (%s, %s, 'Open', %s, 'Uncategorized', %s, '')
                    """,
                    (key, f"Epic {key}", tags, color),
                )
        conn.commit()

    resp = client.get("/", params={"cycle": "26.04"})
    assert resp.status_code == 200
    assert "CY-2" in resp.text
    assert "CY-1" not in resp.text
    assert "Cycle 26.04" in resp.text
    assert "Cycle 25.10" not in resp.text


def test_roadmap_page_filter_by_product(client):
    """Filtering by product shows only matching items."""
    color = json.dumps({"health": {"color": "green"}, "carry_over": None})
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO product (name, primary_project) VALUES ('Juju', 'JUJU') ON CONFLICT DO NOTHING"
            )
            for key, prod in [("F-1", "Juju"), ("F-2", "Uncategorized")]:
                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, status, tags, product, color_status, url)
                    VALUES (%s, %s, 'Open', ARRAY['26.04'], %s, %s, '')
                    """,
                    (key, f"Epic {key}", prod, color),
                )
        conn.commit()

    resp = client.get("/", params={"product": "Juju"})
    assert resp.status_code == 200
    assert "F-1" in resp.text
    assert "F-2" not in resp.text
