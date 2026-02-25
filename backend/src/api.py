"""FastAPI application — the single entry point for the roadmap backend."""

from __future__ import annotations

import json
import logging
import pathlib
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .database import get_db_connection
from .jira_sync import _build_jql, process_raw_jira_data, sync_jira_data
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
    logger.info("  JQL_FILTER    = %s", settings.jql_filter)
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
    try:
        effective_jql = _build_jql()
    except RuntimeError:
        effective_jql = "(no projects configured)"
    logger.info("  Effective JQL: %s", effective_jql)
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

    try:
        effective_jql = _build_jql()
    except (RuntimeError, Exception):
        effective_jql = "(no projects configured)"

    return {
        **_sync_status,
        "config": {
            "jira_url": settings.jira_url,
            "jql_filter": settings.jql_filter,
            "effective_jql": effective_jql,
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
        clauses.append("p.name = %s")
        params.append(product)
    if status:
        clauses.append("r.status = %s")
        params.append(status)
    if release:
        clauses.append("r.release = %s")
        params.append(release)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        "SELECT r.id, r.jira_key, r.title, r.description, r.status, r.release, r.tags, "
        "       p.name AS product, r.color_status, r.url, "
        "       r.parent_key, r.parent_summary, r.created_at, r.updated_at "
        "FROM roadmap_item r "
        f"LEFT JOIN product p ON p.id = r.product_id{where} "
        "ORDER BY r.updated_at DESC"
    )

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    return {"data": [dict(zip(columns, row, strict=False)) for row in rows], "meta": {"total": len(rows)}}


# ---------------------------------------------------------------------------
# Product CRUD — /api/v1/products
# ---------------------------------------------------------------------------

class JiraSourceIn(BaseModel):
    """Input schema for a Jira source rule within a product."""

    jira_project_key: str
    include_components: list[str] | None = None
    exclude_components: list[str] | None = None
    include_labels: list[str] | None = None
    exclude_labels: list[str] | None = None
    include_teams: list[str] | None = None
    exclude_teams: list[str] | None = None


class ProductIn(BaseModel):
    """Input schema for creating/updating a product."""

    name: str
    department: str = "Unassigned"
    jira_sources: list[JiraSourceIn] = []


class JiraSourceOut(BaseModel):
    """Output schema for a Jira source rule."""

    id: int
    jira_project_key: str
    include_components: list[str] | None
    exclude_components: list[str] | None
    include_labels: list[str] | None
    exclude_labels: list[str] | None
    include_teams: list[str] | None
    exclude_teams: list[str] | None


class ProductOut(BaseModel):
    """Output schema for a product with its Jira source rules."""

    id: int
    name: str
    department: str
    jira_sources: list[JiraSourceOut]


def _fetch_product_with_sources(cur, product_id: int) -> dict | None:
    """Read a single product + its jira_sources from the DB. Returns None if not found."""
    cur.execute("SELECT id, name, department FROM product WHERE id = %s", (product_id,))
    row = cur.fetchone()
    if not row:
        return None
    product = {"id": row[0], "name": row[1], "department": row[2]}
    cur.execute(
        "SELECT id, jira_project_key, include_components, exclude_components, "
        "       include_labels, exclude_labels, include_teams, exclude_teams "
        "FROM product_jira_source WHERE product_id = %s ORDER BY id",
        (product_id,),
    )
    product["jira_sources"] = [
        {
            "id": r[0],
            "jira_project_key": r[1],
            "include_components": r[2],
            "exclude_components": r[3],
            "include_labels": r[4],
            "exclude_labels": r[5],
            "include_teams": r[6],
            "exclude_teams": r[7],
        }
        for r in cur.fetchall()
    ]
    return product


@app.get("/api/v1/products")
def list_products():
    """List all products with their Jira source mappings."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product ORDER BY department, name")
        product_ids = [r[0] for r in cur.fetchall()]
        products = [_fetch_product_with_sources(cur, pid) for pid in product_ids]
    return {"data": products, "meta": {"total": len(products)}}


@app.get("/api/v1/products/{product_id}")
def get_product(product_id: int):
    """Get a single product by ID."""
    with get_db_connection() as conn, conn.cursor() as cur:
        product = _fetch_product_with_sources(cur, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"data": product}


@app.post("/api/v1/products", status_code=201)
def create_product(body: ProductIn):
    """Create a product with optional Jira source mappings."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO product (name, department) VALUES (%s, %s) RETURNING id",
            (body.name, body.department),
        )
        product_id = cur.fetchone()[0]

        for src in body.jira_sources:
            cur.execute(
                "INSERT INTO product_jira_source "
                "  (product_id, jira_project_key, include_components, exclude_components, "
                "   include_labels, exclude_labels, include_teams, exclude_teams) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    product_id,
                    src.jira_project_key,
                    src.include_components,
                    src.exclude_components,
                    src.include_labels,
                    src.exclude_labels,
                    src.include_teams,
                    src.exclude_teams,
                ),
            )

        conn.commit()
        product = _fetch_product_with_sources(cur, product_id)

    return {"data": product}


@app.put("/api/v1/products/{product_id}")
def update_product(product_id: int, body: ProductIn):
    """Replace a product's details and Jira source mappings entirely."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product WHERE id = %s", (product_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Product not found")

        cur.execute(
            "UPDATE product SET name = %s, department = %s, updated_at = now() WHERE id = %s",
            (body.name, body.department, product_id),
        )

        # Replace all source rules (simple and safe for small cardinality)
        cur.execute("DELETE FROM product_jira_source WHERE product_id = %s", (product_id,))
        for src in body.jira_sources:
            cur.execute(
                "INSERT INTO product_jira_source "
                "  (product_id, jira_project_key, include_components, exclude_components, "
                "   include_labels, exclude_labels, include_teams, exclude_teams) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    product_id,
                    src.jira_project_key,
                    src.include_components,
                    src.exclude_components,
                    src.include_labels,
                    src.exclude_labels,
                    src.include_teams,
                    src.exclude_teams,
                ),
            )

        conn.commit()
        product = _fetch_product_with_sources(cur, product_id)

    return {"data": product}


@app.delete("/api/v1/products/{product_id}", status_code=204)
def delete_product(product_id: int):
    """Delete a product and its Jira source mappings.

    Roadmap items referencing this product will have their product_id set to NULL.
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM product WHERE id = %s", (product_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Product not found")

        # Unlink roadmap items so they don't get cascade-deleted
        cur.execute("UPDATE roadmap_item SET product_id = NULL WHERE product_id = %s", (product_id,))
        cur.execute("DELETE FROM product WHERE id = %s", (product_id,))
        conn.commit()

    return None


# ---------------------------------------------------------------------------
# Server-rendered HTML page
# ---------------------------------------------------------------------------

CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")


def _query_filter_options(department: str | None = None) -> dict:
    """Fetch distinct departments, products (filtered by department), and cycle labels for filter dropdowns.

    Also returns a ``dept_products`` mapping (department → [product names]) so the
    frontend can dynamically update the product dropdown when the department changes.
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT department FROM product ORDER BY department")
        departments = [r[0] for r in cur.fetchall()]

        # Products for the selected department (or all if none selected)
        if department:
            cur.execute("SELECT DISTINCT name FROM product WHERE department = %s ORDER BY name", (department,))
        else:
            cur.execute("SELECT DISTINCT name FROM product ORDER BY name")
        products = [r[0] for r in cur.fetchall()]

        # Full department → products mapping for client-side filtering
        cur.execute("SELECT department, name FROM product ORDER BY department, name")
        dept_products: dict[str, list[str]] = {}
        for r in cur.fetchall():
            dept_products.setdefault(r[0], []).append(r[1])

        # Cycles come from the tags array (labels) on roadmap_item.
        # unnest expands the array; we then filter for XX.XX pattern in Python.
        cur.execute("SELECT DISTINCT unnest(tags) AS tag FROM roadmap_item")
        all_tags = [r[0] for r in cur.fetchall()]
        cycles = sorted(
            [t for t in all_tags if CYCLE_RE.match(t)],
            reverse=True,
        )

    return {"departments": departments, "products": products, "cycles": cycles, "dept_products": dept_products}


def _query_roadmap_items(
    department: str | None = None,
    product: str | None = None,
    cycles: list[str] | None = None,
) -> OrderedDict[str, OrderedDict[str, list[dict]]]:
    """Return roadmap items grouped by cycle → objective (parent).

    Structure: ``{cycle: {objective_label: [items]}}``
    - Cycles sorted newest-first.
    - Objectives sorted alphabetically within each cycle.
    - Items with no parent are grouped under "No objective".
    - Items with no cycle labels are excluded.
    - An item with multiple cycle labels appears in each bucket.
    """
    clauses: list[str] = []
    params: list = []

    if department:
        clauses.append("p.department = %s")
        params.append(department)
    if product:
        clauses.append("p.name = %s")
        params.append(product)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        "SELECT r.id, r.jira_key, r.title, p.name AS product, "
        "       r.color_status, r.url, r.tags, "
        "       r.parent_key, r.parent_summary, r.rank, r.parent_rank "
        "FROM roadmap_item r "
        f"JOIN product p ON p.id = r.product_id{where} "
        "ORDER BY r.parent_rank NULLS LAST, r.rank NULLS LAST, r.title"
    )

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    # Build cycle → objective → items mapping
    raw: dict[str, dict[str, list[dict]]] = {}
    objective_urls: dict[str, str] = {}  # objective_label → Jira URL

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
        if cycles:
            matching = [c for c in item_cycles if c in cycles]
            if not matching:
                continue
            item_cycles = matching

        # Group by objective (parent summary) instead of product
        parent_key = item.get("parent_key")
        parent_summary = item.get("parent_summary")
        if parent_key and parent_summary:
            objective_label = parent_summary
            objective_urls[objective_label] = f"{settings.jira_url}/browse/{parent_key}"
        else:
            objective_label = "No objective"

        item["_objective"] = objective_label

        for c in item_cycles:
            raw.setdefault(c, {}).setdefault(objective_label, []).append(item)

    # Sort: cycles newest-first, objectives by parent_rank ("No objective" last)
    grouped: OrderedDict[str, OrderedDict[str, list[dict]]] = OrderedDict()
    for c in sorted(raw.keys(), reverse=True):
        objectives = raw[c]
        sorted_keys = sorted(
            objectives.keys(),
            key=lambda k: (
                k == "No objective",
                min((item.get("parent_rank") or "\xff") for item in objectives[k]),
            ),
        )
        grouped[c] = OrderedDict((k, objectives[k]) for k in sorted_keys)

    return grouped, objective_urls


@app.get("/", response_class=HTMLResponse)
def roadmap_page(
    request: Request,
    department: str | None = Query(None),
    product: str | None = Query(None),
    cycle: list[str] | None = Query(None),
):
    """Render the main roadmap page with server-side Jinja2 templates."""
    options = _query_filter_options(department=department)

    # Force a product selection — if none chosen, default to the first available
    available_products = options["products"]
    if not product or product not in available_products:
        product = available_products[0] if available_products else None

    # Normalise cycle list: drop empty strings, None → []
    selected_cycles = [c for c in (cycle or []) if c]

    grouped_items, objective_urls = _query_roadmap_items(
        department=department, product=product, cycles=selected_cycles,
    )

    return templates.TemplateResponse(
        request,
        "roadmap.html",
        {
            "departments": options["departments"],
            "products": available_products,
            "cycles": options["cycles"],
            "dept_products_json": json.dumps(options["dept_products"]),
            "selected_department": department or "",
            "selected_product": product or "",
            "selected_cycles": selected_cycles,
            "grouped_items": grouped_items,
            "objective_urls": objective_urls,
        },
    )
