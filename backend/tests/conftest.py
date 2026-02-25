"""Shared fixtures for the test suite.

Tests run against the ``db-test`` service from docker-compose (port 5433).
The ``setup_test_database`` fixture auto-creates a clean schema for every test.
"""

import os
import pathlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _setup_test_database():
    """Point the app at the test DB instance and apply a fresh schema."""
    # Default Jira env vars so imports don't blow up
    os.environ.setdefault("JIRA_URL", "http://mock.jira.test")
    os.environ.setdefault("JIRA_USERNAME", "mock")
    os.environ.setdefault("JIRA_PAT", "mock")
    os.environ.setdefault("JQL_FILTER", "issuetype = Epic")

    # Test DB runs on port 5433 via docker-compose db-test service
    db_host = os.environ.get("DB_TEST_HOST", "localhost")
    db_port = os.environ.get("DB_TEST_PORT", "5433")
    os.environ["DATABASE_URL"] = f"postgresql://roadmap:roadmap@{db_host}:{db_port}/roadmap_test"

    # Force settings to re-read from env
    import src.settings as settings_mod
    from src.settings import Settings
    settings_mod.settings = Settings()

    from src.database import get_db_connection

    schema_sql = (pathlib.Path(__file__).resolve().parent.parent / "src" / "db_schema.sql").read_text()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS roadmap_item, product_jira_source, jira_issue_raw, product CASCADE"
            )
            cur.execute(schema_sql)
        conn.commit()

    yield

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS roadmap_item, product_jira_source, jira_issue_raw, product CASCADE"
            )
        conn.commit()


@pytest.fixture()
def client() -> TestClient:
    """FastAPI test client that skips the startup schema migration."""
    from src.api import app

    # Don't re-run startup events — conftest already applied the schema
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
