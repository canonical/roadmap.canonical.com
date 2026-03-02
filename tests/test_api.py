"""Tests for /api/v1/* endpoints."""

import json

from psycopg.types.json import Jsonb

from src.database import get_db_connection


def _get_uncategorized_id():
    """Return the id of the seeded 'Uncategorized' product."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product WHERE name = 'Uncategorized'")
        return cur.fetchone()[0]


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
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, description, status, release, tags, product_id, color_status, url)
                VALUES
                    ('TEST-1', 'Test Epic', 'A description', 'In Progress', '25.10',
                     ARRAY['roadmap'], %s, %s, 'http://jira/TEST-1')
                """,
                (uncat_id, Jsonb(color)),
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
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, st in [("A-1", "Done"), ("A-2", "In Progress")]:
                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, status, product_id, url)
                    VALUES (%s, %s, %s, %s, '')
                    """,
                    (key, f"Epic {key}", st, uncat_id),
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
# Product CRUD tests
# ---------------------------------------------------------------------------

def test_list_products(client):
    """GET /api/v1/products returns the seeded Uncategorized product."""
    resp = client.get("/api/v1/products")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert any(p["name"] == "Uncategorized" for p in data)


def test_create_product(client):
    """POST /api/v1/products creates a product with Jira sources."""
    resp = client.post("/api/v1/products", json={
        "name": "MAAS",
        "department": "Engineering",
        "jira_sources": [
            {
                "jira_project_key": "MAAS",
                "include_components": ["UI", "API"],
                "exclude_components": None,
                "include_labels": None,
                "exclude_labels": None,
                "include_teams": ["MAAS-team"],
                "exclude_teams": None,
            },
            {
                "jira_project_key": "SNAP",
                "include_labels": ["maas-related"],
            },
        ],
    })
    assert resp.status_code == 201
    product = resp.json()["data"]
    assert product["name"] == "MAAS"
    assert product["department"] == "Engineering"
    assert len(product["jira_sources"]) == 2
    assert product["jira_sources"][0]["jira_project_key"] == "MAAS"
    assert product["jira_sources"][0]["include_components"] == ["UI", "API"]
    assert product["jira_sources"][0]["include_teams"] == ["MAAS-team"]
    assert product["jira_sources"][1]["jira_project_key"] == "SNAP"
    assert product["jira_sources"][1]["include_labels"] == ["maas-related"]


def test_get_product(client):
    """GET /api/v1/products/{id} returns the product."""
    create = client.post("/api/v1/products", json={"name": "Juju", "department": "Infra"})
    pid = create.json()["data"]["id"]

    resp = client.get(f"/api/v1/products/{pid}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Juju"


def test_get_product_not_found(client):
    """GET /api/v1/products/999999 returns 404."""
    resp = client.get("/api/v1/products/999999")
    assert resp.status_code == 404


def test_update_product(client):
    """PUT /api/v1/products/{id} replaces product details and sources."""
    create = client.post("/api/v1/products", json={
        "name": "LXD",
        "department": "Containers",
        "jira_sources": [{"jira_project_key": "LXD"}],
    })
    pid = create.json()["data"]["id"]

    resp = client.put(f"/api/v1/products/{pid}", json={
        "name": "LXD",
        "department": "Containers",
        "jira_sources": [
            {"jira_project_key": "LXD", "exclude_components": ["CI"]},
            {"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]},
        ],
    })
    assert resp.status_code == 200
    product = resp.json()["data"]
    assert len(product["jira_sources"]) == 2
    assert product["jira_sources"][0]["exclude_components"] == ["CI"]
    assert product["jira_sources"][1]["include_components"] == ["Anbox/LXD Tribe"]


def test_delete_product(client):
    """DELETE /api/v1/products/{id} removes the product."""
    create = client.post("/api/v1/products", json={"name": "Deleteme"})
    pid = create.json()["data"]["id"]

    resp = client.delete(f"/api/v1/products/{pid}")
    assert resp.status_code == 204

    resp = client.get(f"/api/v1/products/{pid}")
    assert resp.status_code == 404


def test_delete_product_unlinks_roadmap_items(client):
    """Deleting a product sets product_id to NULL on linked roadmap items, not deleting them."""
    create = client.post("/api/v1/products", json={"name": "Temp"})
    pid = create.json()["data"]["id"]

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO roadmap_item (jira_key, title, status, product_id, url) "
            "VALUES ('DEL-1', 'Keep me', 'Open', %s, '')",
            (pid,),
        )
        conn.commit()

    client.delete(f"/api/v1/products/{pid}")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT product_id FROM roadmap_item WHERE jira_key = 'DEL-1'")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None


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
    """Items with cycle labels appear in the rendered HTML grouped by cycle then objective."""
    color = Jsonb({"health": {"color": "green"}, "carry_over": None})
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product_id, color_status, url)
                VALUES
                    ('HTML-1', 'Render test', 'In Progress', ARRAY['25.10'],
                     %s, %s, 'http://jira/HTML-1')
                """,
                (uncat_id, color),
            )
        conn.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "HTML-1" in resp.text
    assert "Render test" in resp.text
    assert "Cycle 25.10" in resp.text
    assert "No objective" in resp.text


def test_roadmap_page_with_parent(client):
    """Items with a parent show up grouped by objective (parent summary only)."""
    color = Jsonb({"health": {"color": "green"}, "carry_over": None})
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product_id, color_status, url,
                     parent_key, parent_summary)
                VALUES
                    ('OBJ-1', 'Child epic', 'In Progress', ARRAY['25.10'],
                     %s, %s, 'http://jira/OBJ-1', 'ROCK-100', 'Improve performance')
                """,
                (uncat_id, color),
            )
        conn.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "OBJ-1" in resp.text
    # Objective heading shows summary, not the Jira key
    assert "Improve performance" in resp.text
    # The link to the parent issue is present
    assert "/browse/ROCK-100" in resp.text


def test_roadmap_page_hides_items_without_cycle(client):
    """Items that have no XX.XX cycle label are not shown."""
    color = Jsonb({"health": {"color": "white"}, "carry_over": None})
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product_id, color_status, url)
                VALUES
                    ('NO-1', 'No cycle', 'Open', ARRAY['SomeTag'], %s, %s, '')
                """,
                (uncat_id, color),
            )
        conn.commit()

    resp = client.get("/")
    assert resp.status_code == 200
    assert "NO-1" not in resp.text


def test_roadmap_page_item_in_multiple_cycles(client):
    """An item with two cycle labels appears under both cycle headings."""
    color = Jsonb({"health": {"color": "green"}, "carry_over": {"color": "purple", "count": 1}})
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roadmap_item
                    (jira_key, title, status, tags, product_id, color_status, url)
                VALUES
                    ('MULTI-1', 'Multi-cycle', 'In Progress', ARRAY['25.10', '26.04'],
                     %s, %s, 'http://jira/MULTI-1')
                """,
                (uncat_id, color),
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
    color = Jsonb({"health": {"color": "green"}, "carry_over": None})
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, tags in [("CY-1", ["25.10"]), ("CY-2", ["26.04"])]:
                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, status, tags, product_id, color_status, url)
                    VALUES (%s, %s, 'Open', %s, %s, %s, '')
                    """,
                    (key, f"Epic {key}", tags, uncat_id, color),
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
    color = Jsonb({"health": {"color": "green"}, "carry_over": None})
    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO product (name, department) VALUES ('Juju', 'Engineering') "
                "ON CONFLICT (name) DO NOTHING"
            )
            cur.execute("SELECT id FROM product WHERE name = 'Juju'")
            juju_id = cur.fetchone()[0]
            for key, pid in [("F-1", juju_id), ("F-2", uncat_id)]:
                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, status, tags, product_id, color_status, url)
                    VALUES (%s, %s, 'Open', ARRAY['26.04'], %s, %s, '')
                    """,
                    (key, f"Epic {key}", pid, color),
                )
        conn.commit()

    resp = client.get("/", params={"product": "Juju"})
    assert resp.status_code == 200
    assert "F-1" in resp.text
    assert "F-2" not in resp.text
