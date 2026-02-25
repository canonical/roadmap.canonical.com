"""Jira → PostgreSQL sync pipeline.

Two-phase approach:
  1. ``sync_jira_data``      — fetch issues via JQL and upsert raw JSON into ``jira_issue_raw``.
  2. ``process_raw_jira_data`` — read unprocessed rows, derive roadmap fields, upsert into ``roadmap_item``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from jira import JIRA

from .color_logic import calculate_epic_color
from .database import get_db_connection
from .settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1 — pull from Jira
# ---------------------------------------------------------------------------

def sync_jira_data() -> int:
    """Fetch issues matching the configured JQL and store raw JSON.

    Returns the number of issues upserted.
    """
    logger.info("Connecting to Jira at %s as %s", settings.jira_url, settings.jira_username)
    jira = JIRA(server=settings.jira_url, basic_auth=(settings.jira_username, settings.jira_pat))
    logger.info("Running JQL: %s", settings.jql_query)
    issues = jira.search_issues(settings.jql_query, maxResults=False)
    logger.info("Fetched %d issues from Jira", len(issues))

    if not issues:
        logger.warning("JQL returned 0 issues — check your query and credentials")
        return 0

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for issue in issues:
                cur.execute(
                    """
                    INSERT INTO jira_issue_raw (jira_key, raw_data)
                    VALUES (%s, %s)
                    ON CONFLICT (jira_key) DO UPDATE SET
                        raw_data   = EXCLUDED.raw_data,
                        fetched_at = now(),
                        processed_at = NULL;
                    """,
                    (issue.key, json.dumps(issue.raw)),
                )
        conn.commit()

    return len(issues)


# ---------------------------------------------------------------------------
# Product mapping helpers
# ---------------------------------------------------------------------------

@dataclass
class JiraSourceRule:
    """A single Jira project → product mapping rule with optional filters."""

    product_id: int
    jira_project_key: str
    include_components: list[str] = field(default_factory=list)
    exclude_components: list[str] = field(default_factory=list)
    include_labels: list[str] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_teams: list[str] = field(default_factory=list)
    exclude_teams: list[str] = field(default_factory=list)


def _load_source_rules(cursor) -> list[JiraSourceRule]:
    """Load all product_jira_source rows into structured rules."""
    cursor.execute(
        "SELECT product_id, jira_project_key, include_components, "
        "       exclude_components, include_labels, exclude_labels, "
        "       include_teams, exclude_teams "
        "FROM product_jira_source"
    )
    rules = []
    for row in cursor.fetchall():
        rules.append(JiraSourceRule(
            product_id=row[0],
            jira_project_key=row[1],
            include_components=row[2] or [],
            exclude_components=row[3] or [],
            include_labels=row[4] or [],
            exclude_labels=row[5] or [],
            include_teams=row[6] or [],
            exclude_teams=row[7] or [],
        ))
    return rules


def _get_uncategorized_product_id(cursor) -> int:
    """Return the id of the 'Uncategorized' product (always exists via schema seed)."""
    cursor.execute("SELECT id FROM product WHERE name = 'Uncategorized'")
    row = cursor.fetchone()
    return row[0]


def _match_issue_to_product(
    jira_project_key: str,
    issue_components: list[str],
    issue_labels: list[str],
    issue_teams: list[str],
    rules: list[JiraSourceRule],
    fallback_product_id: int,
) -> int:
    """Determine which product_id an issue belongs to based on source rules.

    Matching logic (first match wins):
      1. The rule's ``jira_project_key`` must match the issue's project.
      2. If ``include_components`` is set, the issue must have at least one matching component.
      3. If ``exclude_components`` is set, the issue must NOT have any matching component.
      4. If ``include_labels`` is set, the issue must have at least one matching label.
      5. If ``exclude_labels`` is set, the issue must NOT have any matching label.
      6. If ``include_teams`` is set, the issue must have at least one matching team.
      7. If ``exclude_teams`` is set, the issue must NOT have any matching team.
    """
    for rule in rules:
        if rule.jira_project_key != jira_project_key:
            continue

        # Component filters
        if rule.include_components and not set(rule.include_components) & set(issue_components):
            continue
        if rule.exclude_components and set(rule.exclude_components) & set(issue_components):
            continue

        # Label filters
        if rule.include_labels and not set(rule.include_labels) & set(issue_labels):
            continue
        if rule.exclude_labels and set(rule.exclude_labels) & set(issue_labels):
            continue

        # Team filters
        if rule.include_teams and not set(rule.include_teams) & set(issue_teams):
            continue
        if rule.exclude_teams and set(rule.exclude_teams) & set(issue_teams):
            continue

        return rule.product_id

    return fallback_product_id


# ---------------------------------------------------------------------------
# Phase 2 — process raw → roadmap_item
# ---------------------------------------------------------------------------

def process_raw_jira_data() -> int:
    """Transform unprocessed raw issues into roadmap_items.

    Returns the number of rows processed.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            rules = _load_source_rules(cur)
            fallback_id = _get_uncategorized_product_id(cur)

            cur.execute("SELECT jira_key, raw_data FROM jira_issue_raw WHERE processed_at IS NULL")
            raw_issues = cur.fetchall()
            logger.info("Found %d unprocessed raw issues", len(raw_issues))

            for jira_key, raw_data in raw_issues:
                fields = raw_data["fields"]
                jira_project = jira_key.split("-")[0]

                issue_components = [
                    c["name"] for c in (fields.get("components") or []) if isinstance(c, dict)
                ]
                issue_labels = fields.get("labels") or []

                # Jira "team" can live in customfield_10001 (Team) or similar — extract name
                team_field = fields.get("customfield_10001")
                if isinstance(team_field, dict):
                    issue_teams = [team_field.get("name") or team_field.get("value", "")]
                elif isinstance(team_field, list):
                    issue_teams = [
                        t.get("name") or t.get("value", "") for t in team_field if isinstance(t, dict)
                    ]
                else:
                    issue_teams = []

                product_id = _match_issue_to_product(
                    jira_project, issue_components, issue_labels, issue_teams, rules, fallback_id,
                )

                color_status = calculate_epic_color(fields)

                fix_versions = fields.get("fixVersions") or []
                release = fix_versions[0].get("name") if fix_versions else None

                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, description, status, release, tags, product_id, color_status, url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (jira_key) DO UPDATE SET
                        title        = EXCLUDED.title,
                        description  = EXCLUDED.description,
                        status       = EXCLUDED.status,
                        release      = EXCLUDED.release,
                        tags         = EXCLUDED.tags,
                        product_id   = EXCLUDED.product_id,
                        color_status = EXCLUDED.color_status,
                        url          = EXCLUDED.url,
                        updated_at   = now();
                    """,
                    (
                        jira_key,
                        fields.get("summary", ""),
                        fields.get("description"),
                        (fields.get("status") or {}).get("name", "Unknown"),
                        release,
                        issue_labels,
                        product_id,
                        json.dumps(color_status),
                        f"{settings.jira_url}/browse/{jira_key}",
                    ),
                )

            # mark as processed
            processed_keys = [row[0] for row in raw_issues]
            if processed_keys:
                cur.execute(
                    "UPDATE jira_issue_raw SET processed_at = now() WHERE jira_key = ANY(%s)",
                    (processed_keys,),
                )

        conn.commit()

    logger.info("Processed %d raw issues into roadmap_item", len(raw_issues))
    return len(raw_issues)
