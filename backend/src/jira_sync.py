"""Jira → PostgreSQL sync pipeline.

Two-phase approach:
  1. ``sync_jira_data``      — fetch issues via JQL and upsert raw JSON into ``jira_issue_raw``.
  2. ``process_raw_jira_data`` — read unprocessed rows, derive roadmap fields, upsert into ``roadmap_item``.
"""

from __future__ import annotations

import json
import logging

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
    jira = JIRA(server=settings.jira_url, basic_auth=(settings.jira_username, settings.jira_pat))
    issues = jira.search_issues(settings.jql_query, maxResults=False)
    logger.info("Fetched %d issues from Jira", len(issues))

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
# Phase 2 — process raw → roadmap_item
# ---------------------------------------------------------------------------

def _get_product_mapping(cursor) -> dict[str, str]:
    """Return {jira_project_key: product_name} from the product table."""
    cursor.execute("SELECT name, primary_project FROM product")
    return {row[1]: row[0] for row in cursor.fetchall()}


def process_raw_jira_data() -> int:
    """Transform unprocessed raw issues into roadmap_items.

    Returns the number of rows processed.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            product_map = _get_product_mapping(cur)

            cur.execute("SELECT jira_key, raw_data FROM jira_issue_raw WHERE processed_at IS NULL")
            raw_issues = cur.fetchall()

            for jira_key, raw_data in raw_issues:
                fields = raw_data["fields"]
                jira_project = jira_key.split("-")[0]
                product = product_map.get(jira_project, "Uncategorized")

                color_status = calculate_epic_color(fields)

                fix_versions = fields.get("fixVersions") or []
                release = fix_versions[0].get("name") if fix_versions else None

                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, description, status, release, tags, product, color_status, url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (jira_key) DO UPDATE SET
                        title        = EXCLUDED.title,
                        description  = EXCLUDED.description,
                        status       = EXCLUDED.status,
                        release      = EXCLUDED.release,
                        tags         = EXCLUDED.tags,
                        product      = EXCLUDED.product,
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
                        fields.get("labels"),
                        product,
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
