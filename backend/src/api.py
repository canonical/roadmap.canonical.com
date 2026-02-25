"""FastAPI application — the single entry point for the roadmap backend."""

from __future__ import annotations

import json
import logging
import pathlib
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .database import get_db_connection
from .jira_sync import process_raw_jira_data, sync_jira_data
from .settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ---------------------------------------------------------------------------
# Startup — ensure schema exists
# ---------------------------------------------------------------------------
SCHEMA_PATH = pathlib.Path(__file__).with_name("db_schema.sql")


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Configure logging, apply DB schema, and log effective settings on startup."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Log effective settings so mis-configuration is immediately visible
    logger.info("=== Roadmap API starting ===")
    logger.info("  JIRA_URL      = %s", settings.jira_url)
    logger.info("  JIRA_USERNAME = %s", settings.jira_username or "(empty)")
    logger.info("  JQL_QUERY     = %s", settings.jql_query)
    logger.info("  DATABASE_URL  = %s", settings.database_url)

    if not settings.jira_pat:
        logger.warning("  JIRA_PAT is empty — sync will fail!")

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
    logger.info("Sync started — Phase 1: fetching from Jira")
    logger.info("  JQL: %s", settings.jql_query)
    try:
        fetched = sync_jira_data()
        _sync_status["issues_fetched"] = fetched
        logger.info("Phase 1 complete — fetched %d issues", fetched)

        _sync_status["state"] = "processing"
        logger.info("Phase 2: processing raw → roadmap_item")
        processed = process_raw_jira_data()
        _sync_status["issues_processed"] = processed
        _sync_status["state"] = "done"
        logger.info("Sync complete — fetched=%d, processed=%d", fetched, processed)
    except Exception as exc:
        logger.exception("Sync failed: %s", exc)
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
    """Return current sync status enriched with DB row counts and active config."""
    db_counts = {}
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM jira_issue_raw")
            db_counts["raw_issues"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM jira_issue_raw WHERE processed_at IS NOT NULL")
            db_counts["raw_processed"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM roadmap_item")
            db_counts["roadmap_items"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM product")
            db_counts["products"] = cur.fetchone()[0]
    except Exception:
        db_counts["error"] = "could not query database"

    return {
        **_sync_status,
        "config": {
            "jira_url": settings.jira_url,
            "jql_query": settings.jql_query,
            "database_url": settings.database_url.rsplit("@", 1)[-1],  # hide password
        },
        "db": db_counts,
    }


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


# ---------------------------------------------------------------------------
# Server-rendered HTML page
# ---------------------------------------------------------------------------

CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")


def _query_filter_options() -> dict:
    """Fetch distinct departments, products, and cycle labels for filter dropdowns."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT department FROM product ORDER BY department")
        departments = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT DISTINCT name FROM product ORDER BY name")
        products = [r[0] for r in cur.fetchall()]

        # Cycles come from the tags array (labels) on roadmap_item.
        # unnest expands the array; we then filter for XX.XX pattern in Python.
        cur.execute("SELECT DISTINCT unnest(tags) AS tag FROM roadmap_item")
        all_tags = [r[0] for r in cur.fetchall()]
        cycles = sorted(
            [t for t in all_tags if CYCLE_RE.match(t)],
            reverse=True,
        )

    return {"departments": departments, "products": products, "cycles": cycles}


def _query_roadmap_items(
    department: str | None = None,
    product: str | None = None,
    cycle: str | None = None,
) -> OrderedDict[str, OrderedDict[str, list[dict]]]:
    """Return roadmap items grouped by cycle → product.

    Structure: ``{cycle: {product: [items]}}``
    - Cycles sorted newest-first.
    - Products sorted alphabetically within each cycle.
    - Items with no cycle labels are excluded.
    - An item with multiple cycle labels appears in each bucket.
    """
    clauses: list[str] = []
    params: list = []

    if department:
        clauses.append("p.department = %s")
        params.append(department)
    if product:
        clauses.append("r.product = %s")
        params.append(product)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        "SELECT r.id, r.jira_key, r.title, r.product, "
        "       r.color_status, r.url, r.tags "
        "FROM roadmap_item r "
        f"JOIN product p ON p.name = r.product{where} "
        "ORDER BY r.product, r.title"
    )

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    # Build cycle → product → items mapping
    raw: dict[str, dict[str, list[dict]]] = {}

    for row in rows:
        item = dict(zip(columns, row, strict=False))
        cs = item.get("color_status")
        if isinstance(cs, str):
            item["color_status"] = json.loads(cs)

        tags = item.get("tags") or []
        item_cycles = [t for t in tags if CYCLE_RE.match(t)]

        # Skip items with no cycle labels
        if not item_cycles:
            continue

        # Apply cycle filter
        if cycle:
            if cycle not in item_cycles:
                continue
            item_cycles = [cycle]

        product_name = item["product"]
        for c in item_cycles:
            raw.setdefault(c, {}).setdefault(product_name, []).append(item)

    # Sort: cycles newest-first, products alphabetically within each cycle
    grouped: OrderedDict[str, OrderedDict[str, list[dict]]] = OrderedDict()
    for c in sorted(raw.keys(), reverse=True):
        grouped[c] = OrderedDict(sorted(raw[c].items()))

    return grouped


@app.get("/", response_class=HTMLResponse)
def roadmap_page(
    request: Request,
    department: str | None = Query(None),
    product: str | None = Query(None),
    cycle: str | None = Query(None),
):
    """Render the main roadmap page with server-side Jinja2 templates."""
    options = _query_filter_options()
    grouped_items = _query_roadmap_items(department=department, product=product, cycle=cycle)

    return templates.TemplateResponse(
        request,
        "roadmap.html",
        {
            "departments": options["departments"],
            "products": options["products"],
            "cycles": options["cycles"],
            "selected_department": department or "",
            "selected_product": product or "",
            "selected_cycle": cycle or "",
            "grouped_items": grouped_items,
        },
    )
