"""Tests for the Jira sync pipeline (Phase 2 only — no live Jira calls)."""

import pytest
from psycopg.types.json import Jsonb

from src.database import get_db_connection
from src.jira_sync import JiraSourceRule, _build_jql, _match_issue_to_product, process_raw_jira_data


def _insert_raw_issue(jira_key: str, fields: dict) -> None:
    """Helper: insert a fake raw issue for processing."""
    raw = {"fields": fields}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jira_issue_raw (jira_key, raw_data) VALUES (%s, %s)",
                (jira_key, Jsonb(raw)),
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


def test_process_extracts_parent():
    """Parent key and summary are extracted from the raw Jira data."""
    _insert_raw_issue(
        "MOCK-P1",
        {
            "summary": "Child epic",
            "status": {"name": "Open"},
            "labels": ["25.10"],
            "parent": {
                "key": "ROCK-100",
                "fields": {"summary": "Improve performance"},
            },
        },
    )

    process_raw_jira_data()

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT parent_key, parent_summary FROM roadmap_item WHERE jira_key = 'MOCK-P1'")
        row = cur.fetchone()

    assert row is not None
    assert row[0] == "ROCK-100"
    assert row[1] == "Improve performance"


def test_process_no_parent():
    """Items without a parent have NULL parent fields."""
    _insert_raw_issue(
        "MOCK-P2",
        {
            "summary": "Orphan epic",
            "status": {"name": "Open"},
            "labels": [],
        },
    )

    process_raw_jira_data()

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT parent_key, parent_summary FROM roadmap_item WHERE jira_key = 'MOCK-P2'")
        row = cur.fetchone()

    assert row is not None
    assert row[0] is None
    assert row[1] is None


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
            new_raw = Jsonb({"fields": {"summary": "v2", "status": {"name": "Done"}, "labels": []}})
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
    rules = [
        JiraSourceRule(
            product_id=1,
            jira_project_key="PROJ",
            include_components=["Web"],
            exclude_components=["Legacy"],
            include_labels=["roadmap"],
            exclude_labels=["internal"],
            include_teams=["Frontend"],
            exclude_teams=["QA"],
        )
    ]
    # All conditions met
    assert (
        _match_issue_to_product(
            "PROJ",
            ["Web"],
            ["roadmap"],
            ["Frontend"],
            rules,
            fallback_product_id=99,
        )
        == 1
    )
    # Missing required label
    assert (
        _match_issue_to_product(
            "PROJ",
            ["Web"],
            [],
            ["Frontend"],
            rules,
            fallback_product_id=99,
        )
        == 99
    )
    # Has excluded component
    assert (
        _match_issue_to_product(
            "PROJ",
            ["Web", "Legacy"],
            ["roadmap"],
            ["Frontend"],
            rules,
            fallback_product_id=99,
        )
        == 99
    )
    # Missing required component
    assert (
        _match_issue_to_product(
            "PROJ",
            ["API"],
            ["roadmap"],
            ["Frontend"],
            rules,
            fallback_product_id=99,
        )
        == 99
    )
    # Has excluded label
    assert (
        _match_issue_to_product(
            "PROJ",
            ["Web"],
            ["roadmap", "internal"],
            ["Frontend"],
            rules,
            fallback_product_id=99,
        )
        == 99
    )
    # Has excluded team
    assert (
        _match_issue_to_product(
            "PROJ",
            ["Web"],
            ["roadmap"],
            ["Frontend", "QA"],
            rules,
            fallback_product_id=99,
        )
        == 99
    )
    # Missing required team
    assert (
        _match_issue_to_product(
            "PROJ",
            ["Web"],
            ["roadmap"],
            ["Backend"],
            rules,
            fallback_product_id=99,
        )
        == 99
    )


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


# ---------------------------------------------------------------------------
# Unit tests for _build_jql
# ---------------------------------------------------------------------------


def test_build_jql_with_projects():
    """JQL is built from product_jira_source project keys + cycle_config labels + jql_filter."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('P1', 'D1') RETURNING id")
        p1_id = cur.fetchone()[0]
        cur.execute("INSERT INTO product (name, department) VALUES ('P2', 'D2') RETURNING id")
        p2_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'DPE'), (%s, 'JUJU')",
            (p1_id, p2_id),
        )
        # Register active cycles
        cur.execute("INSERT INTO cycle_config (cycle, state) VALUES ('26.04', 'current'), ('26.10', 'future')")
        conn.commit()

    jql = _build_jql()
    # Projects are sorted alphabetically and quoted (to handle JQL reserved words)
    assert jql.startswith('project in ("DPE", "JUJU")')
    assert "labels in (26.04, 26.10)" in jql
    assert "issuetype = Epic" in jql


def test_build_jql_deduplicates_projects():
    """Duplicate project keys across rules appear only once in the JQL."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('PA', 'DA') RETURNING id")
        pa_id = cur.fetchone()[0]
        cur.execute("INSERT INTO product (name, department) VALUES ('PB', 'DB') RETURNING id")
        pb_id = cur.fetchone()[0]
        # Both products map to the same Jira project
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'KU'), (%s, 'KU')",
            (pa_id, pb_id),
        )
        cur.execute("INSERT INTO cycle_config (cycle, state) VALUES ('26.04', 'current')")
        conn.commit()

    jql = _build_jql()
    assert jql.startswith('project in ("KU")')


def test_build_jql_no_projects_raises():
    """_build_jql raises RuntimeError when no project keys are configured."""
    # Need at least one cycle so we don't hit the cycle check first
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO cycle_config (cycle, state) VALUES ('26.04', 'current')")
        conn.commit()
    with pytest.raises(RuntimeError, match="No Jira project keys found"):
        _build_jql()


def test_build_jql_no_cycles_raises():
    """_build_jql raises RuntimeError when no active cycles are configured."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('Pz', 'Dz') RETURNING id")
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'ZZZ')",
            (pid,),
        )
        conn.commit()
    with pytest.raises(RuntimeError, match="No active cycles found"):
        _build_jql()


def test_build_jql_excludes_frozen_cycles():
    """Frozen cycles are NOT included in the labels clause."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('Pf', 'Df') RETURNING id")
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'FRZ')",
            (pid,),
        )
        cur.execute(
            "INSERT INTO cycle_config (cycle, state) VALUES "
            "('25.10', 'frozen'), ('26.04', 'current'), ('26.10', 'future')"
        )
        conn.commit()

    jql = _build_jql()
    assert "labels in (26.04, 26.10)" in jql
    assert "25.10" not in jql


def test_build_jql_respects_jql_filter(monkeypatch):
    """The jql_filter setting is appended after the project + labels clauses."""
    import src.jira_sync as jira_sync_mod
    import src.settings as settings_mod
    from src.settings import Settings

    monkeypatch.setenv("JQL_FILTER", "issuetype = Epic")
    new_settings = Settings()
    settings_mod.settings = new_settings
    monkeypatch.setattr(jira_sync_mod, "settings", new_settings)

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('Px', 'Dx') RETURNING id")
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'ABC')",
            (pid,),
        )
        cur.execute("INSERT INTO cycle_config (cycle, state) VALUES ('26.04', 'current'), ('26.10', 'future')")
        conn.commit()

    jql = _build_jql()
    assert jql == 'project in ("ABC") AND labels in (26.04, 26.10) AND issuetype = Epic'


def test_build_jql_empty_filter(monkeypatch):
    """When jql_filter is empty, only the project + labels clauses are returned."""
    import src.jira_sync as jira_sync_mod
    import src.settings as settings_mod
    from src.settings import Settings

    monkeypatch.setenv("JQL_FILTER", "")
    new_settings = Settings()
    settings_mod.settings = new_settings
    monkeypatch.setattr(jira_sync_mod, "settings", new_settings)

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO product (name, department) VALUES ('Py', 'Dy') RETURNING id")
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO product_jira_source (product_id, jira_project_key) VALUES (%s, 'XYZ')",
            (pid,),
        )
        cur.execute("INSERT INTO cycle_config (cycle, state) VALUES ('26.04', 'current')")
        conn.commit()

    jql = _build_jql()
    assert jql == 'project in ("XYZ") AND labels in (26.04)'
