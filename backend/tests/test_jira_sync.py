"""Tests for the Jira sync pipeline (Phase 2 only — no live Jira calls)."""

import json

from src.database import get_db_connection
from src.jira_sync import JiraSourceRule, _match_issue_to_product, process_raw_jira_data


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


def _get_uncategorized_id() -> int:
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product WHERE name = 'Uncategorized'")
        return cur.fetchone()[0]


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


def test_process_assigns_product_via_source_rule():
    """Issues are assigned to the correct product based on product_jira_source rules."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('Juju', 'Infra') RETURNING id")
        juju_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'JUJU')",
            (juju_id,),
        )
        conn.commit()

    _insert_raw_issue("JUJU-100", {"summary": "Juju epic", "status": {"name": "Open"}, "labels": []})
    process_raw_jira_data()

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT product_id FROM roadmap_item WHERE jira_key = 'JUJU-100'")
        row = cur.fetchone()

    assert row[0] == juju_id


def test_process_falls_back_to_uncategorized():
    """Issues with no matching source rule land in 'Uncategorized'."""
    _insert_raw_issue("UNKN-1", {"summary": "Unknown project", "status": {"name": "Open"}, "labels": []})
    process_raw_jira_data()

    uncat_id = _get_uncategorized_id()
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT product_id FROM roadmap_item WHERE jira_key = 'UNKN-1'")
        row = cur.fetchone()

    assert row[0] == uncat_id


# ---------------------------------------------------------------------------
# Unit tests for _match_issue_to_product
# ---------------------------------------------------------------------------

def test_match_simple_project():
    """A rule with only project key matches any issue in that project."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="LXD")]
    assert _match_issue_to_product("LXD", [], [], [], rules, fallback_product_id=99) == 1


def test_match_no_rules_falls_back():
    """No matching rules returns the fallback."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="LXD")]
    assert _match_issue_to_product("MAAS", [], [], [], rules, fallback_product_id=99) == 99


def test_match_include_components():
    """include_components filters to issues that have at least one matching component."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="WD", include_components=["Anbox/LXD Tribe"])]
    assert _match_issue_to_product("WD", ["Anbox/LXD Tribe"], [], [], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("WD", ["Other Tribe"], [], [], rules, fallback_product_id=99) == 99


def test_match_exclude_components():
    """exclude_components rejects issues with a matching component."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="FR", exclude_components=["Toolchains"])]
    assert _match_issue_to_product("FR", ["Toolchains"], [], [], rules, fallback_product_id=99) == 99
    assert _match_issue_to_product("FR", ["Other"], [], [], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("FR", [], [], [], rules, fallback_product_id=99) == 1


def test_match_include_labels():
    """include_labels filters to issues that have at least one matching label."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="PALS", include_labels=["Scriptlets", "Starform"])]
    assert _match_issue_to_product("PALS", [], ["Scriptlets"], [], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("PALS", [], ["Starform"], [], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("PALS", [], ["Other"], [], rules, fallback_product_id=99) == 99


def test_match_exclude_labels():
    """exclude_labels rejects issues with a matching label."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="PROJ", exclude_labels=["internal", "wontfix"])]
    assert _match_issue_to_product("PROJ", [], ["internal"], [], rules, fallback_product_id=99) == 99
    assert _match_issue_to_product("PROJ", [], ["wontfix"], [], rules, fallback_product_id=99) == 99
    assert _match_issue_to_product("PROJ", [], ["roadmap"], [], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("PROJ", [], [], [], rules, fallback_product_id=99) == 1


def test_match_include_teams():
    """include_teams filters to issues owned by at least one matching team."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="PROJ", include_teams=["Kernel", "Foundations"])]
    assert _match_issue_to_product("PROJ", [], [], ["Kernel"], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("PROJ", [], [], ["Foundations"], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("PROJ", [], [], ["Desktop"], rules, fallback_product_id=99) == 99
    assert _match_issue_to_product("PROJ", [], [], [], rules, fallback_product_id=99) == 99


def test_match_exclude_teams():
    """exclude_teams rejects issues owned by a matching team."""
    rules = [JiraSourceRule(product_id=1, jira_project_key="PROJ", exclude_teams=["QA"])]
    assert _match_issue_to_product("PROJ", [], [], ["QA"], rules, fallback_product_id=99) == 99
    assert _match_issue_to_product("PROJ", [], [], ["Dev"], rules, fallback_product_id=99) == 1
    assert _match_issue_to_product("PROJ", [], [], [], rules, fallback_product_id=99) == 1


def test_match_combined_filters():
    """Multiple filters on a single rule are AND-ed together."""
    rules = [JiraSourceRule(
        product_id=1,
        jira_project_key="PROJ",
        include_components=["Web"],
        exclude_components=["Legacy"],
        include_labels=["roadmap"],
        exclude_labels=["internal"],
        include_teams=["Frontend"],
        exclude_teams=["QA"],
    )]
    # All conditions met
    assert _match_issue_to_product(
        "PROJ", ["Web"], ["roadmap"], ["Frontend"], rules, fallback_product_id=99,
    ) == 1
    # Missing required label
    assert _match_issue_to_product(
        "PROJ", ["Web"], [], ["Frontend"], rules, fallback_product_id=99,
    ) == 99
    # Has excluded component
    assert _match_issue_to_product(
        "PROJ", ["Web", "Legacy"], ["roadmap"], ["Frontend"], rules, fallback_product_id=99,
    ) == 99
    # Missing required component
    assert _match_issue_to_product(
        "PROJ", ["API"], ["roadmap"], ["Frontend"], rules, fallback_product_id=99,
    ) == 99
    # Has excluded label
    assert _match_issue_to_product(
        "PROJ", ["Web"], ["roadmap", "internal"], ["Frontend"], rules, fallback_product_id=99,
    ) == 99
    # Has excluded team
    assert _match_issue_to_product(
        "PROJ", ["Web"], ["roadmap"], ["Frontend", "QA"], rules, fallback_product_id=99,
    ) == 99
    # Missing required team
    assert _match_issue_to_product(
        "PROJ", ["Web"], ["roadmap"], ["Backend"], rules, fallback_product_id=99,
    ) == 99


def test_match_first_rule_wins():
    """When multiple rules match, the first one wins."""
    rules = [
        JiraSourceRule(product_id=1, jira_project_key="PROJ", include_labels=["team-a"]),
        JiraSourceRule(product_id=2, jira_project_key="PROJ"),
    ]
    # First rule matches
    assert _match_issue_to_product("PROJ", [], ["team-a"], [], rules, fallback_product_id=99) == 1
    # First rule doesn't match, second does
    assert _match_issue_to_product("PROJ", [], ["team-b"], [], rules, fallback_product_id=99) == 2
