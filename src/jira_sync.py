"""Jira → PostgreSQL sync pipeline.

Two-phase approach:
  1. ``sync_jira_data``      — fetch issues via JQL and upsert raw JSON into ``jira_issue_raw``.
  2. ``process_raw_jira_data`` — read unprocessed rows, derive roadmap fields, upsert into ``roadmap_item``.
  3. ``take_daily_snapshot``  — once per day, snapshot all ``roadmap_item`` rows for change-tracking.
  4. ``freeze_cycle`` / ``unfreeze_cycle`` — lock a cycle's data for historical preservation.
  5. ``cycle_config`` CRUD    — manage cycle lifecycle states (frozen / current / future).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from jira import JIRA
from psycopg.types.json import Jsonb

from .color_logic import calculate_epic_color
from .database import get_db_connection
from .settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1 — pull from Jira
# ---------------------------------------------------------------------------


def _build_jql() -> str:
    """Build the JQL query dynamically from DB-managed projects and cycles.

    1. Reads distinct ``jira_project_key`` values from ``product_jira_source``.
    2. Reads non-frozen cycle labels from ``cycle_config`` (states ``current``
       and ``future``) to build the ``labels in (...)`` clause.
    3. Appends the static ``jql_filter`` setting (e.g. ``issuetype = Epic``).

    Returns a JQL string like::

        project in (JUJU, KU, DPE) AND labels in (26.04, 26.10) AND issuetype = Epic
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT jira_project_key FROM product_jira_source ORDER BY jira_project_key")
        project_keys = [row[0] for row in cur.fetchall()]

        cur.execute(
            "SELECT cycle FROM cycle_config WHERE state IN ('current', 'future') ORDER BY cycle"
        )
        cycle_labels = [row[0] for row in cur.fetchall()]

    if not project_keys:
        raise RuntimeError(
            "No Jira project keys found in product_jira_source table. "
            "Add at least one product_jira_source row before syncing."
        )

    if not cycle_labels:
        raise RuntimeError(
            "No active cycles found in cycle_config (state = current or future). "
            "Register at least one cycle before syncing."
        )

    jql = "project in ({})".format(", ".join(project_keys))
    jql += " AND labels in ({})".format(", ".join(cycle_labels))
    if settings.jql_filter:
        jql += f" AND {settings.jql_filter}"

    return jql


def sync_jira_data() -> int:
    """Fetch issues matching the configured JQL and store raw JSON.

    Also fetches parent issues (objectives) in a second batch to capture their rank.
    Returns the number of issues upserted.
    """
    logger.info("Connecting to Jira at %s as %s", settings.jira_url, settings.jira_username)
    jira = JIRA(server=settings.jira_url, basic_auth=(settings.jira_username, settings.jira_pat))
    jql = _build_jql()
    logger.info("Running JQL: %s", jql)
    issues = jira.search_issues(jql, maxResults=False)
    logger.info("Fetched %d issues from Jira", len(issues))

    if not issues:
        logger.warning("JQL returned 0 issues — check your query and credentials")
        return 0

    # Collect unique parent keys so we can fetch their rank
    parent_keys: set[str] = set()
    for issue in issues:
        parent = (issue.raw.get("fields") or {}).get("parent")
        if isinstance(parent, dict) and parent.get("key"):
            parent_keys.add(parent["key"])

    # Fetch parent issues in a single batch to get their rank
    parent_ranks: dict[str, str] = {}
    fetched_parent_keys = parent_keys.copy()
    # Remove parents that are already in the fetched issues
    for issue in issues:
        if issue.key in parent_keys:
            rank = (issue.raw.get("fields") or {}).get("customfield_10019", "")
            parent_ranks[issue.key] = rank or ""
            fetched_parent_keys.discard(issue.key)

    if fetched_parent_keys:
        # Batch fetch parents (JQL: key in (...))
        keys_csv = ", ".join(fetched_parent_keys)
        parent_jql = f"key in ({keys_csv})"
        logger.info("Fetching %d parent issues for rank: %s", len(fetched_parent_keys), parent_jql)
        try:
            parent_issues = jira.search_issues(parent_jql, maxResults=False, fields="customfield_10019,summary")
            for pi in parent_issues:
                rank = (pi.raw.get("fields") or {}).get("customfield_10019", "")
                parent_ranks[pi.key] = rank or ""
            logger.info("Fetched ranks for %d parent issues", len(parent_issues))
        except Exception:
            logger.exception("Failed to fetch parent ranks — objectives will sort by name")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for issue in issues:
                # Inject parent_rank into the raw payload for Phase 2
                raw = issue.raw
                parent = (raw.get("fields") or {}).get("parent")
                if isinstance(parent, dict) and parent.get("key"):
                    raw.setdefault("_roadmap_meta", {})["parent_rank"] = parent_ranks.get(parent["key"], "")

                cur.execute(
                    """
                    INSERT INTO jira_issue_raw (jira_key, raw_data)
                    VALUES (%s, %s)
                    ON CONFLICT (jira_key) DO UPDATE SET
                        raw_data   = EXCLUDED.raw_data,
                        fetched_at = now(),
                        processed_at = NULL;
                    """,
                    (issue.key, Jsonb(raw)),
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

                # Extract parent (objective) key and summary
                parent = fields.get("parent")
                parent_key = None
                parent_summary = None
                if isinstance(parent, dict):
                    parent_key = parent.get("key")
                    parent_fields = parent.get("fields") or {}
                    parent_summary = parent_fields.get("summary")

                # Jira rank (customfield_10019) — lexicographic string for ordering
                rank = fields.get("customfield_10019") or ""

                # Parent rank — injected by Phase 1 into _roadmap_meta
                parent_rank = (raw_data.get("_roadmap_meta") or {}).get("parent_rank", "")

                cur.execute(
                    """
                    INSERT INTO roadmap_item
                        (jira_key, title, description, status, release, tags, product_id,
                         color_status, url, parent_key, parent_summary, rank, parent_rank)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (jira_key) DO UPDATE SET
                        title           = EXCLUDED.title,
                        description     = EXCLUDED.description,
                        status          = EXCLUDED.status,
                        release         = EXCLUDED.release,
                        tags            = EXCLUDED.tags,
                        product_id      = EXCLUDED.product_id,
                        color_status    = EXCLUDED.color_status,
                        url             = EXCLUDED.url,
                        parent_key      = EXCLUDED.parent_key,
                        parent_summary  = EXCLUDED.parent_summary,
                        rank            = EXCLUDED.rank,
                        parent_rank     = EXCLUDED.parent_rank,
                        updated_at      = now();
                    """,
                    (
                        jira_key,
                        fields.get("summary", ""),
                        fields.get("description"),
                        (fields.get("status") or {}).get("name", "Unknown"),
                        release,
                        issue_labels,
                        product_id,
                        Jsonb(color_status),
                        f"{settings.jira_url}/browse/{jira_key}",
                        parent_key,
                        parent_summary,
                        rank,
                        parent_rank,
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


# ---------------------------------------------------------------------------
# Phase 3 — daily snapshot for change-tracking
# ---------------------------------------------------------------------------


def take_daily_snapshot(snapshot_date: date | None = None) -> int:
    """Snapshot all current roadmap_item rows into ``roadmap_snapshot``.

    Called after each sync.  If today's snapshot already exists, this is a
    no-op so that hourly syncs don't create duplicate data.

    Args:
        snapshot_date: Override the date (useful for tests).  Defaults to today.

    Returns:
        The number of rows inserted, or 0 if a snapshot already existed.
    """
    if snapshot_date is None:
        snapshot_date = date.today()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Check if today's snapshot already exists
            cur.execute(
                "SELECT 1 FROM roadmap_snapshot WHERE snapshot_date = %s LIMIT 1",
                (snapshot_date,),
            )
            if cur.fetchone():
                logger.info("Snapshot for %s already exists — skipping", snapshot_date)
                return 0

            # Insert a snapshot of every roadmap_item + its product info
            cur.execute(
                """
                INSERT INTO roadmap_snapshot
                    (snapshot_date, jira_key, title, status, color, release,
                     tags, product_id, product_name, department,
                     parent_key, parent_summary)
                SELECT
                    %s,
                    r.jira_key,
                    r.title,
                    r.status,
                    r.color_status->'health'->>'color',
                    r.release,
                    r.tags,
                    r.product_id,
                    p.name,
                    p.department,
                    r.parent_key,
                    r.parent_summary
                FROM roadmap_item r
                LEFT JOIN product p ON p.id = r.product_id
                """,
                (snapshot_date,),
            )
            count = cur.rowcount
        conn.commit()

    logger.info("Snapshot for %s created — %d items captured", snapshot_date, count)
    return count


# ---------------------------------------------------------------------------
# Phase 4 — cycle freeze / unfreeze
# ---------------------------------------------------------------------------

CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")


def freeze_cycle(cycle: str, frozen_by: str | None = None, note: str | None = None) -> int:
    """Freeze a cycle by snapshotting every ``roadmap_item`` tagged with that cycle label.

    Captures the *current* state of each item — title, status, color, product,
    objective — into ``cycle_freeze_item``.  Once frozen, the roadmap page
    serves this immutable copy instead of live Jira data.

    Args:
        cycle: The cycle label to freeze (e.g. ``"25.10"``).
        frozen_by: Optional identifier of the person who triggered the freeze.
        note: Optional free-text note.

    Returns:
        The number of items captured.

    Raises:
        ValueError: If the cycle label doesn't match ``XX.XX``.
        RuntimeError: If the cycle is already frozen.
    """
    if not CYCLE_RE.match(cycle):
        raise ValueError(f"Invalid cycle label: {cycle!r} (expected XX.XX format)")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Check if already frozen
            cur.execute("SELECT 1 FROM cycle_freeze WHERE cycle = %s", (cycle,))
            if cur.fetchone():
                raise RuntimeError(f"Cycle {cycle} is already frozen")

            # Create the freeze header
            cur.execute(
                "INSERT INTO cycle_freeze (cycle, frozen_by, note) VALUES (%s, %s, %s)",
                (cycle, frozen_by, note),
            )

            # Snapshot every item that carries this cycle label
            cur.execute(
                """
                INSERT INTO cycle_freeze_item
                    (cycle, jira_key, title, status, color_status, url,
                     product_id, product_name, department,
                     parent_key, parent_summary, rank, parent_rank, tags)
                SELECT
                    %s,
                    r.jira_key,
                    r.title,
                    r.status,
                    r.color_status,
                    r.url,
                    r.product_id,
                    p.name,
                    p.department,
                    r.parent_key,
                    r.parent_summary,
                    r.rank,
                    r.parent_rank,
                    r.tags
                FROM roadmap_item r
                LEFT JOIN product p ON p.id = r.product_id
                WHERE %s = ANY(r.tags)
                """,
                (cycle, cycle),
            )
            count = cur.rowcount
        conn.commit()

    logger.info("Cycle %s frozen — %d items captured", cycle, count)
    return count


def unfreeze_cycle(cycle: str) -> None:
    """Remove a cycle freeze, restoring live Jira data for that cycle.

    Deletes the ``cycle_freeze`` row (and cascades to ``cycle_freeze_item``).

    Raises:
        ValueError: If the cycle is not currently frozen.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cycle_freeze WHERE cycle = %s", (cycle,))
            if cur.rowcount == 0:
                raise ValueError(f"Cycle {cycle} is not frozen")
        conn.commit()

    logger.info("Cycle %s unfrozen — live data restored", cycle)


def get_frozen_cycles() -> dict[str, dict]:
    """Return a mapping of frozen cycle labels to their metadata.

    Returns:
        ``{"25.10": {"frozen_at": "...", "frozen_by": "...", "note": "..."}, ...}``
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cycle, frozen_at, frozen_by, note FROM cycle_freeze ORDER BY cycle DESC"
        )
        return {
            row[0]: {
                "frozen_at": row[1].isoformat() if row[1] else None,
                "frozen_by": row[2],
                "note": row[3],
            }
            for row in cur.fetchall()
        }


# ---------------------------------------------------------------------------
# Phase 5 — cycle_config: explicit lifecycle state management
# ---------------------------------------------------------------------------


def get_cycle_configs() -> dict[str, dict]:
    """Return all registered cycles with their state and metadata.

    Returns:
        ``{"25.10": {"state": "frozen", "updated_at": "...", "updated_by": "..."}, ...}``
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cycle, state, updated_at, updated_by "
            "FROM cycle_config ORDER BY cycle DESC"
        )
        return {
            row[0]: {
                "state": row[1],
                "updated_at": row[2].isoformat() if row[2] else None,
                "updated_by": row[3],
            }
            for row in cur.fetchall()
        }


def register_cycle(cycle: str, state: str, updated_by: str | None = None) -> dict:
    """Register a new cycle with an initial state.

    Args:
        cycle: The cycle label (e.g. ``"26.10"``). Must match XX.XX format.
        state: Initial state — ``"frozen"``, ``"current"``, or ``"future"``.
        updated_by: Optional email of the person registering the cycle.

    Returns:
        The newly created cycle config dict.

    Raises:
        ValueError: If the cycle label is invalid or state is unrecognised.
        RuntimeError: If the cycle is already registered, or if setting
            ``"current"`` would violate the at-most-one-current constraint.
    """
    if not CYCLE_RE.match(cycle):
        raise ValueError(f"Invalid cycle label: {cycle!r} (expected XX.XX format)")
    if state not in ("frozen", "current", "future"):
        raise ValueError(f"Invalid state: {state!r} (expected frozen/current/future)")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Already registered?
            cur.execute("SELECT 1 FROM cycle_config WHERE cycle = %s", (cycle,))
            if cur.fetchone():
                raise RuntimeError(f"Cycle {cycle} is already registered")

            # At most one current
            if state == "current":
                cur.execute("SELECT cycle FROM cycle_config WHERE state = 'current'")
                existing = cur.fetchone()
                if existing:
                    raise RuntimeError(
                        f"Cannot register {cycle} as current — "
                        f"cycle {existing[0]} is already current"
                    )

            cur.execute(
                "INSERT INTO cycle_config (cycle, state, updated_by) VALUES (%s, %s, %s)",
                (cycle, state, updated_by),
            )

            # Side effect: if registering as frozen, create the freeze snapshot
            if state == "frozen":
                _ensure_freeze_snapshot(cur, cycle, updated_by)

        conn.commit()

    logger.info("Cycle %s registered with state=%s", cycle, state)
    return {"cycle": cycle, "state": state, "updated_by": updated_by}


def set_cycle_state(cycle: str, new_state: str, updated_by: str | None = None) -> dict:
    """Change a registered cycle's state, with freeze/unfreeze side effects.

    State transitions and side effects:
        - **→ frozen**: creates a ``cycle_freeze`` snapshot if one doesn't already exist.
        - **frozen →** (any other state): deletes the ``cycle_freeze`` snapshot.
        - **→ current**: enforced that at most one cycle is current.
        - **→ future**: no special side effects.

    Args:
        cycle: The cycle label (must already be registered).
        new_state: Target state (``"frozen"`` / ``"current"`` / ``"future"``).
        updated_by: Optional email of the person changing state.

    Returns:
        Updated cycle config dict.

    Raises:
        ValueError: If the cycle is not registered or new_state is invalid.
        RuntimeError: If setting ``"current"`` would violate the at-most-one constraint.
    """
    if new_state not in ("frozen", "current", "future"):
        raise ValueError(f"Invalid state: {new_state!r} (expected frozen/current/future)")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM cycle_config WHERE cycle = %s", (cycle,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Cycle {cycle} is not registered")
            old_state = row[0]

            if old_state == new_state:
                # No-op
                return {"cycle": cycle, "state": new_state, "updated_by": updated_by}

            # At most one current
            if new_state == "current":
                cur.execute(
                    "SELECT cycle FROM cycle_config WHERE state = 'current' AND cycle != %s",
                    (cycle,),
                )
                existing = cur.fetchone()
                if existing:
                    raise RuntimeError(
                        f"Cannot set {cycle} to current — "
                        f"cycle {existing[0]} is already current"
                    )

            # Side effects: leaving frozen → delete snapshot
            if old_state == "frozen":
                _delete_freeze_snapshot(cur, cycle)

            # Side effects: entering frozen → create snapshot
            if new_state == "frozen":
                _ensure_freeze_snapshot(cur, cycle, updated_by)

            cur.execute(
                "UPDATE cycle_config SET state = %s, updated_at = now(), updated_by = %s "
                "WHERE cycle = %s",
                (new_state, updated_by, cycle),
            )
        conn.commit()

    logger.info("Cycle %s state changed: %s → %s", cycle, old_state, new_state)
    return {"cycle": cycle, "state": new_state, "updated_by": updated_by}


def remove_cycle(cycle: str) -> None:
    """Remove a cycle from the config registry.

    If the cycle is frozen, the freeze snapshot is also deleted.

    Raises:
        ValueError: If the cycle is not registered.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM cycle_config WHERE cycle = %s", (cycle,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Cycle {cycle} is not registered")

            # Clean up freeze data if frozen
            if row[0] == "frozen":
                _delete_freeze_snapshot(cur, cycle)

            cur.execute("DELETE FROM cycle_config WHERE cycle = %s", (cycle,))
        conn.commit()

    logger.info("Cycle %s removed from config", cycle)


# ---------------------------------------------------------------------------
# Internal helpers for freeze side effects
# ---------------------------------------------------------------------------


def _ensure_freeze_snapshot(cur, cycle: str, frozen_by: str | None = None) -> int:
    """Create a ``cycle_freeze`` + ``cycle_freeze_item`` snapshot if it doesn't exist.

    Returns the number of items captured.
    """
    cur.execute("SELECT 1 FROM cycle_freeze WHERE cycle = %s", (cycle,))
    if cur.fetchone():
        return 0  # already exists

    cur.execute(
        "INSERT INTO cycle_freeze (cycle, frozen_by) VALUES (%s, %s)",
        (cycle, frozen_by),
    )
    cur.execute(
        """
        INSERT INTO cycle_freeze_item
            (cycle, jira_key, title, status, color_status, url,
             product_id, product_name, department,
             parent_key, parent_summary, rank, parent_rank, tags)
        SELECT
            %s,
            r.jira_key,
            r.title,
            r.status,
            r.color_status,
            r.url,
            r.product_id,
            p.name,
            p.department,
            r.parent_key,
            r.parent_summary,
            r.rank,
            r.parent_rank,
            r.tags
        FROM roadmap_item r
        LEFT JOIN product p ON p.id = r.product_id
        WHERE %s = ANY(r.tags)
        """,
        (cycle, cycle),
    )
    count = cur.rowcount
    logger.info("Freeze snapshot for cycle %s — %d items captured", cycle, count)
    return count


def _delete_freeze_snapshot(cur, cycle: str) -> None:
    """Delete the ``cycle_freeze`` + ``cycle_freeze_item`` rows for a cycle."""
    cur.execute("DELETE FROM cycle_freeze WHERE cycle = %s", (cycle,))
    # Items are cascade-deleted via FK
