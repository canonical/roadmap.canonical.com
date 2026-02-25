"""Tests for the Jira sync pipeline (Phase 2 only — no live Jira calls)."""

import json

from src.database import get_db_connection
from src.jira_sync import process_raw_jira_data


def _insert_raw_issue(jira_key: str, fields: dict) -> None:
    """Helper: insert a fake raw issue for processing."""
    raw = {"fields": fields}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jira_issue_raw (jira_key, raw_data) VALUES (%s, %s)",
                (jira_key, json.dumps(raw)),
            )
        conn.commit()


def test_process_creates_roadmap_item():
    """A raw issue gets turned into a roadmap_item row."""
    _insert_raw_issue(
        "MOCK-1",
        {
            "summary": "Ship feature X",
            "description": "Details here",
            "status": {"name": "In Progress"},
            "labels": ["25.10"],
            "fixVersions": [{"name": "25.10"}],
        },
    )

    count = process_raw_jira_data()
    assert count == 1

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT jira_key, title, status, release FROM roadmap_item WHERE jira_key = 'MOCK-1'")
        row = cur.fetchone()

    assert row is not None
    assert row[0] == "MOCK-1"
    assert row[1] == "Ship feature X"
    assert row[2] == "In Progress"
    assert row[3] == "25.10"


def test_process_marks_as_processed():
    """After processing, the raw row has a non-NULL processed_at."""
    _insert_raw_issue("MOCK-2", {"summary": "Test", "status": {"name": "Open"}, "labels": []})
    process_raw_jira_data()

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT processed_at FROM jira_issue_raw WHERE jira_key = 'MOCK-2'")
        row = cur.fetchone()

    assert row[0] is not None


def test_process_skips_already_processed():
    """Running process twice doesn't reprocess already-processed rows."""
    _insert_raw_issue("MOCK-3", {"summary": "Once", "status": {"name": "Done"}, "labels": []})
    first = process_raw_jira_data()
    second = process_raw_jira_data()
    assert first == 1
    assert second == 0


def test_process_upserts_on_re_fetch():
    """If a raw issue is re-fetched (processed_at reset to NULL), it gets re-processed."""
    _insert_raw_issue("MOCK-4", {"summary": "v1", "status": {"name": "Open"}, "labels": []})
    process_raw_jira_data()

    # Simulate a re-fetch by resetting processed_at and updating data
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            new_raw = json.dumps({"fields": {"summary": "v2", "status": {"name": "Done"}, "labels": []}})
            cur.execute(
                "UPDATE jira_issue_raw SET raw_data = %s, processed_at = NULL WHERE jira_key = 'MOCK-4'",
                (new_raw,),
            )
        conn.commit()

    count = process_raw_jira_data()
    assert count == 1

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT title, status FROM roadmap_item WHERE jira_key = 'MOCK-4'")
        row = cur.fetchone()

    assert row[0] == "v2"
    assert row[1] == "Done"
