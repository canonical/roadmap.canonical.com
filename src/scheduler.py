"""Standalone periodic Jira-sync scheduler.

Run as a separate Pebble service (name must end with ``-scheduler``).
paas-charm ensures that only unit 0 runs scheduler services, so this
process will never run on other replicas — no distributed-lock needed.

Usage inside the rock::

    python3 -m src.scheduler
"""

from __future__ import annotations

import logging
import pathlib
import time
from datetime import UTC, datetime, timedelta

from .database import get_db_connection
from .jira_sync import process_raw_jira_data, sync_jira_data, take_daily_snapshot
from .settings import settings

logger = logging.getLogger(__name__)

SCHEMA_PATH = pathlib.Path(__file__).with_name("db_schema.sql")


def _apply_schema() -> None:
    """Ensure the database schema is up-to-date (idempotent)."""
    sql = SCHEMA_PATH.read_text()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info("Database schema applied")


def _update_sync_metadata(
    *,
    started: bool = False,
    finished: bool = False,
    ok: bool | None = None,
    error: str | None = None,
    interval: int = 0,
) -> None:
    """Write scheduler progress to the ``sync_metadata`` table."""
    clauses: list[str] = []
    params: list[object] = []

    if started:
        clauses.append("last_sync_start = %s")
        params.append(datetime.now(UTC))
    if finished:
        now = datetime.now(UTC)
        clauses.append("last_sync_end = %s")
        params.append(now)
        clauses.append("last_sync_ok = %s")
        params.append(ok)
        if interval > 0:
            clauses.append("next_sync_at = %s")
            params.append(now + timedelta(seconds=interval))
    if error is not None:
        clauses.append("error_message = %s")
        params.append(error or None)  # store NULL for empty string
    if interval:
        clauses.append("interval_seconds = %s")
        params.append(interval)

    if not clauses:
        return

    sql = f"UPDATE sync_metadata SET {', '.join(clauses)} WHERE id = 1"  # noqa: S608
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def _run_sync(interval: int) -> None:
    """Execute the full three-phase Jira sync and record metadata."""
    _update_sync_metadata(started=True, error="")
    logger.info("Sync started at %s", datetime.now(UTC).isoformat())
    try:
        fetched = sync_jira_data()
        logger.info("Phase 1 complete — fetched %d issues", fetched)

        processed = process_raw_jira_data()
        logger.info("Phase 2 complete — processed %d issues", processed)

        snapshot_count = take_daily_snapshot()
        logger.info("Phase 3 complete — snapshot %d items", snapshot_count)

        logger.info("Sync finished — fetched=%d, processed=%d, snapshot=%d", fetched, processed, snapshot_count)
        _update_sync_metadata(finished=True, ok=True, interval=interval)
    except Exception as exc:
        logger.exception("Sync failed")
        _update_sync_metadata(finished=True, ok=False, error=str(exc), interval=interval)


def main() -> None:
    """Entry-point: apply schema, then loop forever with a configurable sleep."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    interval = settings.sync_interval_seconds
    if interval <= 0:
        logger.info("sync_interval_seconds=%d — periodic sync disabled, exiting.", interval)
        return

    logger.info("Scheduler starting — interval=%d s", interval)

    _apply_schema()
    # Record the initial interval and estimated next-sync time
    _update_sync_metadata(interval=interval)

    # Run the first sync immediately, then sleep between iterations.
    while True:
        _run_sync(interval)
        logger.info("Sleeping %d s until next sync …", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
