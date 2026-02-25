"""FastAPI application — the single entry point for the roadmap backend."""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .database import get_db_connection
from .jira_sync import process_raw_jira_data, sync_jira_data

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup — ensure schema exists
# ---------------------------------------------------------------------------
SCHEMA_PATH = pathlib.Path(__file__).with_name("db_schema.sql")


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Apply DB schema on startup."""
    sql = SCHEMA_PATH.read_text()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info("Database schema applied")
    yield


app = FastAPI(title="Roadmap API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory sync status (good enough until we need persistence)
# ---------------------------------------------------------------------------
_sync_status: dict = {
    "last_sync_start": None,
    "last_sync_end": None,
    "state": "idle",  # idle | syncing | processing | done | failed
    "error": None,
    "issues_fetched": None,
    "issues_processed": None,
}


def _run_full_sync() -> None:
    """Execute the two-phase Jira sync; updates ``_sync_status`` in place."""
    _sync_status["state"] = "syncing"
    _sync_status["last_sync_start"] = datetime.now(UTC).isoformat()
    _sync_status["error"] = None
    try:
        fetched = sync_jira_data()
        _sync_status["issues_fetched"] = fetched
        _sync_status["state"] = "processing"
        processed = process_raw_jira_data()
        _sync_status["issues_processed"] = processed
        _sync_status["state"] = "done"
    except Exception as exc:
        logger.exception("Sync failed")
        _sync_status["state"] = "failed"
        _sync_status["error"] = str(exc)
    finally:
        _sync_status["last_sync_end"] = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    """Kick off a background Jira sync."""
    if _sync_status["state"] in ("syncing", "processing"):
        return {"message": "Sync already in progress", "status": _sync_status}
    background_tasks.add_task(_run_full_sync)
    return {"message": "Sync started"}


@app.get("/api/v1/status")
def get_status():
    """Return current sync status."""
    return _sync_status


@app.get("/api/v1/roadmap")
def get_roadmap(
    product: str | None = Query(None),
    status: str | None = Query(None),
    release: str | None = Query(None),
):
    """Return roadmap items with optional filtering."""
    clauses: list[str] = []
    params: list = []

    if product:
        clauses.append("product = %s")
        params.append(product)
    if status:
        clauses.append("status = %s")
        params.append(status)
    if release:
        clauses.append("release = %s")
        params.append(release)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        "SELECT id, jira_key, title, description, status, release, tags, "
        f"       product, color_status, url, created_at, updated_at FROM roadmap_item{where} "
        "ORDER BY updated_at DESC"
    )

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    return {"data": [dict(zip(columns, row, strict=False)) for row in rows], "meta": {"total": len(rows)}}
