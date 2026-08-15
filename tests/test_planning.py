"""Tests for the capacity planning module."""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# Ensure env is set up before importing our modules
os.environ.setdefault("JIRA_URL", "http://mock.jira.test")
os.environ.setdefault("JIRA_USERNAME", "mock")
os.environ.setdefault("JIRA_PAT", "mock")
os.environ.setdefault("JQL_FILTER", "issuetype = Epic")
os.environ["OIDC_CLIENT_ID"] = ""

# Point at the test DB
os.environ["POSTGRESQL_DB_CONNECT_STRING"] = "postgresql://roadmap:roadmap@localhost:5433/roadmap_test"
os.environ["DATABASE_URL"] = "postgresql://roadmap:roadmap@localhost:5433/roadmap_test"


@pytest.fixture
def clean_db(client: TestClient):
    """Reset planning tables between test groups."""
    from src.database import get_db_connection

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM epic_weekly_progress")
            cur.execute("DELETE FROM epic_cycle_selection")
            cur.execute("DELETE FROM epic_role_estimate")
            cur.execute("DELETE FROM product_planning_config")
            cur.execute("DELETE FROM member_weekly_availability")
            cur.execute("DELETE FROM team_member")
            cur.execute("DELETE FROM product_role")
            cur.execute("DELETE FROM roadmap_item")
            cur.execute("DELETE FROM product_jira_source")
            cur.execute("DELETE FROM cycle_config")
            cur.execute("DELETE FROM jira_issue_raw")
            # Re-seed product
            cur.execute("INSERT INTO product (name, department) VALUES ('Test Product', 'Engineering') ON CONFLICT DO NOTHING")
        conn.commit()
    yield


@pytest.fixture
def product_id() -> int:
    from src.database import get_db_connection

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM product WHERE name = 'Test Product'")
            row = cur.fetchone()
            if not row:
                cur.execute("INSERT INTO product (name, department) VALUES ('Test Product', 'Engineering') RETURNING id")
                row = cur.fetchone()
            return row[0]


@pytest.fixture
def cycle() -> str:
    return "26.04"


@pytest.fixture(autouse=True)
def seed_cycle(clean_db, cycle: str):
    from src.database import get_db_connection

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cycle_config (cycle, state, start_date, end_date) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (cycle, "current", date(2026, 4, 6), date(2026, 9, 27)),
            )
        conn.commit()


class TestTShirtMapping:
    def test_all_sizes(self):
        from src.jira_sync import map_tshirt_to_days

        assert map_tshirt_to_days("XXS") == 5
        assert map_tshirt_to_days("XS") == 10
        assert map_tshirt_to_days("S") == 15
        assert map_tshirt_to_days("M") == 25
        assert map_tshirt_to_days("L") == 40
        assert map_tshirt_to_days("XL") == 65
        assert map_tshirt_to_days("XXL") == 105
        assert map_tshirt_to_days("XXXL") == 170

    def test_case_insensitive(self):
        from src.jira_sync import map_tshirt_to_days

        assert map_tshirt_to_days("m") == 25
        assert map_tshirt_to_days(" M ") == 25

    def test_unknown(self):
        from src.jira_sync import map_tshirt_to_days

        assert map_tshirt_to_days("Z") is None
        assert map_tshirt_to_days(None) is None


class TestRoles:
    def test_create_role(self, client: TestClient, product_id: int):
        resp = client.post(
            f"/api/v1/products/{product_id}/roles",
            json={"name": "Backend", "sort_order": 0, "is_default": True},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["name"] == "Backend"
        assert data["is_default"] is True

    def test_max_four_roles(self, client: TestClient, product_id: int):
        for i, name in enumerate(["Backend", "Frontend", "Design", "QA"]):
            client.post(
                f"/api/v1/products/{product_id}/roles",
                json={"name": name, "sort_order": i, "is_default": i == 0},
            )
        resp = client.post(
            f"/api/v1/products/{product_id}/roles",
            json={"name": "Ops", "sort_order": 5},
        )
        assert resp.status_code == 400

    def test_default_unique(self, client: TestClient, product_id: int):
        client.post(f"/api/v1/products/{product_id}/roles", json={"name": "Backend", "is_default": True})
        client.post(f"/api/v1/products/{product_id}/roles", json={"name": "Frontend", "is_default": True})

        resp = client.get(f"/api/v1/products/{product_id}/roles")
        roles = resp.json()["data"]
        defaults = [r for r in roles if r["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["name"] == "Frontend"


class TestMembers:
    def test_create_and_list(self, client: TestClient, product_id: int):
        # Need a role first
        r = client.post(f"/api/v1/products/{product_id}/roles", json={"name": "Backend", "is_default": True})
        role_id = r.json()["data"]["id"]

        resp = client.post(
            f"/api/v1/products/{product_id}/members",
            json={"name": "Alice", "role_id": role_id, "individual_coefficient": 1.0},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["name"] == "Alice"

        resp = client.get(f"/api/v1/products/{product_id}/members")
        assert len(resp.json()["data"]) == 1


class TestAvailability:
    def test_grid(self, client: TestClient, product_id: int, cycle: str):
        # Setup role + member
        r = client.post(f"/api/v1/products/{product_id}/roles", json={"name": "Backend", "is_default": True})
        role_id = r.json()["data"]["id"]
        m = client.post(
            f"/api/v1/products/{product_id}/members",
            json={"name": "Alice", "role_id": role_id},
        )
        member_id = m.json()["data"]["id"]

        resp = client.get(f"/api/v1/products/{product_id}/availability?cycle={cycle}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["weeks"]) > 0
        assert len(data["members"]) == 1

        # Set availability
        resp = client.put(
            f"/api/v1/products/{product_id}/availability/{member_id}/2026-04-06?days_available=4",
        )
        assert resp.status_code == 200

        resp = client.get(f"/api/v1/products/{product_id}/availability?cycle={cycle}")
        assert resp.json()["grid"][str(member_id)]["2026-04-06"] == 4


class TestCapacityMath:
    """Core mathematical tests for capacity and curves."""

    @pytest.fixture(autouse=True)
    def setup(self, client: TestClient, product_id: int, cycle: str):
        # Create Backend role (default)
        r = client.post(f"/api/v1/products/{product_id}/roles", json={"name": "Backend", "is_default": True})
        self.role_id = r.json()["data"]["id"]

        # Add Alice (coefficient 1.0, available 5 days every week)
        m = client.post(
            f"/api/v1/products/{product_id}/members",
            json={"name": "Alice", "role_id": self.role_id, "individual_coefficient": 1.0},
        )
        self.member_id = m.json()["data"]["id"]

        # Set efficiency to 0.6
        client.put(f"/api/v1/products/{product_id}/planning-config", json={"cycle_id": cycle, "team_efficiency": 0.6})

        # Pre-fill availability: 5 days for every week
        resp = client.get(f"/api/v1/products/{product_id}/availability?cycle={cycle}")
        weeks = resp.json()["weeks"]
        for w in weeks:
            client.put(f"/api/v1/products/{product_id}/availability/{self.member_id}/{w}?days_available=5")

        yield

    def test_capacity_per_week(self, client: TestClient, product_id: int, cycle: str):
        resp = client.get(f"/api/v1/products/{product_id}/curves?cycle={cycle}")
        data = resp.json()
        # Alice: 5 days × 0.6 efficiency × 1.0 coeff = 3 ideal days/week
        assert data["summary"]["total_capacity"] == len(data["weeks"]) * 3.0

    def test_ideal_curve(self, client: TestClient, product_id: int, cycle: str):
        resp = client.get(f"/api/v1/products/{product_id}/curves?cycle={cycle}")
        data = resp.json()
        ideal = data["global"]["ideal"]
        # Starts at total capacity, ends at 0
        total_cap = data["summary"]["total_capacity"]
        assert ideal[0] == total_cap
        assert ideal[-1] == 0.0
        # Monotonically non-increasing
        for i in range(1, len(ideal)):
            assert ideal[i] <= ideal[i - 1]

    def test_initial_curve_with_epics(self, client: TestClient, product_id: int, cycle: str):
        # Create a roadmap item
        from src.database import get_db_connection

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO roadmap_item (jira_key, title, status, tags, product_id, t_shirt_size)
                    VALUES ('TEST-1', 'Feature A', 'Open', %s, %s, 'M')
                    RETURNING id
                    """,
                    ([cycle], product_id),
                )
                item_id = cur.fetchone()[0]
            conn.commit()

        # Select it
        client.put(f"/api/v1/epics/{item_id}/selection", json={"cycle": cycle, "is_in_roadmap": True})

        resp = client.get(f"/api/v1/products/{product_id}/curves?cycle={cycle}")
        data = resp.json()

        # M = 25 ideal days
        initial = data["global"]["initial"]
        # Week 0 = 25 - 3 (first week capacity)
        assert initial[0] == 25.0
        assert initial[-1] == 25.0 - data["summary"]["total_capacity"]
        assert data["summary"]["total_committed"] == 25

    def test_expected_curve_empty_progress(self, client: TestClient, product_id: int, cycle: str):
        # Same setup as above
        from src.database import get_db_connection

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO roadmap_item (jira_key, title, status, tags, product_id, t_shirt_size)
                    VALUES ('TEST-2', 'Feature B', 'Open', %s, %s, 'M')
                    RETURNING id
                    """,
                    ([cycle], product_id),
                )
                item_id = cur.fetchone()[0]
            conn.commit()

        client.put(f"/api/v1/epics/{item_id}/selection", json={"cycle": cycle, "is_in_roadmap": True})

        resp = client.get(f"/api/v1/products/{product_id}/curves?cycle={cycle}")
        data = resp.json()

        expected = data["global"]["expected"]
        # No manual progress → expected follows ideal trajectory from current committed
        assert expected[0] == 25.0
        for i in range(1, len(expected)):
            assert expected[i] == round(expected[i - 1] - 3.0, 2)

    def test_expected_curve_with_manual_progress(self, client: TestClient, product_id: int, cycle: str):
        from src.database import get_db_connection

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO roadmap_item (jira_key, title, status, tags, product_id, t_shirt_size)
                    VALUES ('TEST-3', 'Feature C', 'Open', %s, %s, 'M')
                    RETURNING id
                    """,
                    ([cycle], product_id),
                )
                item_id = cur.fetchone()[0]
            conn.commit()

        client.put(f"/api/v1/epics/{item_id}/selection", json={"cycle": cycle, "is_in_roadmap": True})

        # Enter manual remaining 10 days for week 2
        client.post(
            f"/api/v1/epics/{item_id}/progress",
            json={"cycle": cycle, "week_start_date": "2026-04-13", "remaining_days": 10},
        )

        resp = client.get(f"/api/v1/products/{product_id}/curves?cycle={cycle}")
        data = resp.json()
        weeks = data["weeks"]
        w2_idx = weeks.index("2026-04-13")

        expected = data["global"]["expected"]
        # Week 0 = committed (25)
        assert expected[0] == 25.0
        # Week 1 = ideal burn-down since no manual entry yet
        assert expected[1] == 22.0
        # Week 2 = manual sum = 10 (only one epic)
        assert expected[w2_idx] == 10.0

    def test_blue_item_initial_zero(self, client: TestClient, product_id: int, cycle: str):
        from src.database import get_db_connection

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO roadmap_item (jira_key, title, status, tags, product_id, t_shirt_size)
                    VALUES ('TEST-4', 'Feature D', 'Open', %s, %s, 'S')
                    RETURNING id
                    """,
                    ([cycle], product_id),
                )
                item_id = cur.fetchone()[0]
            conn.commit()

        # Set initial_size_days = 0 manually to simulate blue item
        client.put(
            f"/api/v1/epics/{item_id}/estimates",
            json={"estimates": [{"role_id": self.role_id, "size_days": 15, "initial_size_days": 0}]},
        )
        client.put(f"/api/v1/epics/{item_id}/selection", json={"cycle": cycle, "is_in_roadmap": True})

        resp = client.get(f"/api/v1/products/{product_id}/curves?cycle={cycle}")
        data = resp.json()

        # Initial curve should be 0 (blue item not included), Expected should be 15
        assert data["summary"]["total_committed"] == 0
        expected = data["global"]["expected"]
        assert expected[0] == 15.0


class TestSoftDeletePreservesPlanning:
    def test_soft_delete_keeps_data(self, client: TestClient, product_id: int, cycle: str):
        from src.database import get_db_connection

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO roadmap_item (jira_key, title, status, tags, product_id, t_shirt_size, is_deleted)
                    VALUES ('TEST-DEL', 'Gone', 'Open', %s, %s, 'S', TRUE)
                    RETURNING id
                    """,
                    ([cycle], product_id),
                )
                item_id = cur.fetchone()[0]
                # Insert planning data
                cur.execute("INSERT INTO product_role (product_id, name) VALUES (%s, 'Backend') RETURNING id", (product_id,))
                role_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO epic_role_estimate (roadmap_item_id, role_id, size_days, initial_size_days) VALUES (%s, %s, 15, 15)",
                    (item_id, role_id),
                )
            conn.commit()

        # Verify data still exists
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT size_days FROM epic_role_estimate WHERE roadmap_item_id = %s", (item_id,))
                assert cur.fetchone()[0] == 15


class TestAuditLog:
    def test_undo_role_create(self, client: TestClient, product_id: int):
        resp = client.post(f"/api/v1/products/{product_id}/roles", json={"name": "TempRole"})
        assert resp.status_code == 201

        # Undo
        resp = client.post(f"/api/v1/products/{product_id}/undo")
        assert resp.status_code == 200
        assert resp.json()["data"]["undone"] is True
        assert resp.json()["data"]["table"] == "product_role"

        # Verify role is gone
        roles = client.get(f"/api/v1/products/{product_id}/roles").json()["data"]
        assert not any(r["name"] == "TempRole" for r in roles)
