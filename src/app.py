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
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from .auth import configure_oauth, handle_callback, is_authenticated, login_redirect
from .database import get_db_connection
from .jira_sync import (
    _build_jql,
    get_cycle_configs,
    get_frozen_cycles,
    process_raw_jira_data,
    register_cycle,
    remove_cycle,
    set_cycle_state,
    sync_jira_data,
    take_daily_snapshot,
)
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

    # Configure OIDC (Authlib)
    if settings.oidc_client_id:
        configure_oauth()
        logger.info("  OIDC issuer   = %s", settings.oidc_issuer)
    else:
        logger.warning("  OIDC_CLIENT_ID is empty — authentication disabled")

    yield


# Paths that must be accessible without authentication
_PUBLIC_PATHS = {"/login", "/callback"}


class OIDCAuthMiddleware(BaseHTTPMiddleware):
    """Enforce OIDC authentication on all routes except /login and /callback.

    - Browser requests (HTML pages) → redirect to /login
    - API requests (/api/*) → return 401 JSON
    - Disabled entirely when OIDC_CLIENT_ID is empty
    """

    async def dispatch(self, request: Request, call_next):
        if settings.oidc_client_id and request.url.path not in _PUBLIC_PATHS:
            if not is_authenticated(request):
                if request.url.path.startswith("/api/"):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required"},
                    )
                return RedirectResponse(url="/login")
        return await call_next(request)


app = FastAPI(title="Roadmap API", version="0.1.0", lifespan=lifespan)

# Middleware order: add_middleware uses a stack, so the LAST added is the
# OUTERMOST (runs first).  We need: CORS → Session → OIDCAuth (innermost).
app.add_middleware(OIDCAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="roadmap_session",
    max_age=86400,  # 24 hours
    same_site="lax",
    https_only=False,  # set True when served behind HTTPS
)
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
    "snapshot_items": None,
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

        logger.info("Phase 3: daily snapshot")
        snapshot_count = take_daily_snapshot()
        _sync_status["snapshot_items"] = snapshot_count

        _sync_status["state"] = "done"
        logger.info("Sync complete — fetched=%d, processed=%d, snapshot=%d", fetched, processed, snapshot_count)
    except Exception as exc:
        logger.exception("Sync failed: %s", exc)
        _sync_status["state"] = "failed"
        _sync_status["error"] = str(exc)
    finally:
        _sync_status["last_sync_end"] = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Authentication endpoints
# ---------------------------------------------------------------------------

@app.get("/login")
async def login(request: Request):
    """Redirect to the OIDC provider for login."""
    if not settings.oidc_client_id:
        raise HTTPException(status_code=501, detail="OIDC not configured")
    return await login_redirect(request)


@app.get("/callback")
async def callback(request: Request):
    """Handle the OIDC callback (authorization code exchange)."""
    return await handle_callback(request)


@app.get("/token", response_class=HTMLResponse)
async def token_page(request: Request):
    """Show the session cookie as a ready-to-copy curl command."""
    cookie_value = request.cookies.get("roadmap_session", "")
    base_url = str(request.base_url).rstrip("/")
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head>
<title>API Token</title>
<link rel="stylesheet" href="https://assets.ubuntu.com/v1/vanilla-framework-version-4.21.0.min.css"/>
</head><body>
<div class="p-strip"><div class="row"><div class="col-12">
<h1>API session cookie</h1>
<p>You are authenticated. Use the cookie below to call API endpoints with <code>curl</code>:</p>
<pre class="p-code-snippet"><code>curl -b 'roadmap_session={cookie_value}' {base_url}/api/v1/status</code></pre>
<p>This cookie expires in 24 hours (or when the server restarts).</p>
<h2>Examples</h2>
<pre class="p-code-snippet"><code># Trigger a Jira sync
curl -X POST -b 'roadmap_session={cookie_value}' {base_url}/api/v1/sync

# Get roadmap items
curl -b 'roadmap_session={cookie_value}' {base_url}/api/v1/items

# Get sync status
curl -b 'roadmap_session={cookie_value}' {base_url}/api/v1/status</code></pre>
</div></div></div>
</body></html>""")


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
            cur.execute("SELECT count(DISTINCT snapshot_date) FROM roadmap_snapshot")
            db_counts["snapshot_dates"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM roadmap_snapshot")
            db_counts["snapshot_rows"] = cur.fetchone()[0]
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


# ---------------------------------------------------------------------------
# Snapshot diff endpoints — biweekly change reports
# ---------------------------------------------------------------------------


@app.get("/api/v1/snapshots")
def list_snapshots():
    """List all available snapshot dates (newest first)."""
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_date, count(*) AS item_count "
            "FROM roadmap_snapshot GROUP BY snapshot_date ORDER BY snapshot_date DESC"
        )
        rows = cur.fetchall()
    return {
        "data": [{"date": str(r[0]), "item_count": r[1]} for r in rows],
        "meta": {"total": len(rows)},
    }


@app.get("/api/v1/snapshots/diff")
def snapshot_diff(
    from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
):
    """Compare two snapshots and return items that changed, appeared, or disappeared.

    Returns four lists:
    - ``turned_red``   — items whose color changed *to* red
    - ``color_changes`` — all items whose color changed (includes turned_red)
    - ``disappeared``  — items present on *from_date* but missing on *to_date*
    - ``appeared``     — items present on *to_date* but missing on *from_date*
    """
    with get_db_connection() as conn, conn.cursor() as cur:
        # Verify both dates exist
        cur.execute(
            "SELECT DISTINCT snapshot_date FROM roadmap_snapshot "
            "WHERE snapshot_date IN (%s, %s)",
            (from_date, to_date),
        )
        found_dates = {str(r[0]) for r in cur.fetchall()}
        missing = {from_date, to_date} - found_dates
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot found for date(s): {', '.join(sorted(missing))}",
            )

        # --- Color changes (including turned_red) ---
        cur.execute(
            """
            SELECT f.jira_key, f.title, f.color AS old_color, t.color AS new_color,
                   t.product_name, t.department, t.status
            FROM roadmap_snapshot f
            JOIN roadmap_snapshot t ON f.jira_key = t.jira_key
            WHERE f.snapshot_date = %s AND t.snapshot_date = %s
              AND f.color IS DISTINCT FROM t.color
            ORDER BY t.product_name, f.jira_key
            """,
            (from_date, to_date),
        )
        color_cols = [d[0] for d in cur.description]
        color_changes = [dict(zip(color_cols, r, strict=False)) for r in cur.fetchall()]
        turned_red = [c for c in color_changes if c["new_color"] == "red"]

        # --- Disappeared items ---
        cur.execute(
            """
            SELECT f.jira_key, f.title, f.color, f.status,
                   f.product_name, f.department
            FROM roadmap_snapshot f
            LEFT JOIN roadmap_snapshot t
              ON f.jira_key = t.jira_key AND t.snapshot_date = %s
            WHERE f.snapshot_date = %s AND t.jira_key IS NULL
            ORDER BY f.product_name, f.jira_key
            """,
            (to_date, from_date),
        )
        dis_cols = [d[0] for d in cur.description]
        disappeared = [dict(zip(dis_cols, r, strict=False)) for r in cur.fetchall()]

        # --- Appeared items ---
        cur.execute(
            """
            SELECT t.jira_key, t.title, t.color, t.status,
                   t.product_name, t.department
            FROM roadmap_snapshot t
            LEFT JOIN roadmap_snapshot f
              ON t.jira_key = f.jira_key AND f.snapshot_date = %s
            WHERE t.snapshot_date = %s AND f.jira_key IS NULL
            ORDER BY t.product_name, t.jira_key
            """,
            (from_date, to_date),
        )
        app_cols = [d[0] for d in cur.description]
        appeared = [dict(zip(app_cols, r, strict=False)) for r in cur.fetchall()]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "turned_red": turned_red,
        "color_changes": color_changes,
        "disappeared": disappeared,
        "appeared": appeared,
        "summary": {
            "turned_red": len(turned_red),
            "color_changes": len(color_changes),
            "disappeared": len(disappeared),
            "appeared": len(appeared),
        },
    }


# ---------------------------------------------------------------------------
# Cycle lifecycle endpoints — manage cycle state (frozen / current / future)
# ---------------------------------------------------------------------------


class CycleRegisterIn(BaseModel):
    """Input schema for registering a new cycle."""

    state: str = "future"  # default to future for newly registered cycles


class CycleStateIn(BaseModel):
    """Input schema for changing a cycle's state."""

    state: str


@app.get("/api/v1/cycles")
def list_cycles():
    """List all known cycles with their state and metadata."""
    configs = get_cycle_configs()
    frozen = get_frozen_cycles()

    # Also gather live cycles from roadmap_item tags
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT unnest(tags) AS tag FROM roadmap_item")
        all_tags = [r[0] for r in cur.fetchall()]

    live_cycles = sorted(
        [t for t in all_tags if CYCLE_RE.match(t)],
        reverse=True,
    )

    # Merge all known cycle labels: live items + registered configs
    all_cycle_labels = sorted(
        set(live_cycles) | set(configs.keys()),
        reverse=True,
    )

    data = []
    for c in all_cycle_labels:
        entry: dict = {"cycle": c}
        if c in configs:
            entry["state"] = configs[c]["state"]
            entry["updated_at"] = configs[c]["updated_at"]
            entry["updated_by"] = configs[c]["updated_by"]
        else:
            entry["state"] = None  # not registered — appears in Jira but not managed
        # Include frozen metadata if available
        if c in frozen:
            entry["frozen_at"] = frozen[c]["frozen_at"]
            entry["frozen_by"] = frozen[c]["frozen_by"]
            entry["note"] = frozen[c]["note"]
        data.append(entry)

    return {"data": data, "meta": {"total": len(data)}}


@app.post("/api/v1/cycles/{cycle}", status_code=201)
def register_cycle_endpoint(cycle: str, body: CycleRegisterIn | None = None, request: Request = None):
    """Register a new cycle with an initial state."""
    updated_by = None
    if request and request.session.get("user"):
        updated_by = request.session["user"].get("email")

    state = body.state if body else "future"

    try:
        result = register_cycle(cycle, state=state, updated_by=updated_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"message": f"Cycle {cycle} registered as {state}", **result}


@app.put("/api/v1/cycles/{cycle}")
def set_cycle_state_endpoint(cycle: str, body: CycleStateIn, request: Request = None):
    """Change a registered cycle's state.

    Side effects:
    - Setting state to ``frozen`` creates a freeze snapshot.
    - Moving away from ``frozen`` deletes the snapshot.
    - At most one cycle can be ``current`` at any time.
    """
    updated_by = None
    if request and request.session.get("user"):
        updated_by = request.session["user"].get("email")

    try:
        result = set_cycle_state(cycle, new_state=body.state, updated_by=updated_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"message": f"Cycle {cycle} state set to {body.state}", **result}


@app.delete("/api/v1/cycles/{cycle}", status_code=200)
def remove_cycle_endpoint(cycle: str):
    """Remove a cycle from the registry (also deletes freeze data if frozen)."""
    try:
        remove_cycle(cycle)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"message": f"Cycle {cycle} removed"}


@app.get("/api/v1/cycles/{cycle}/items")
def get_frozen_cycle_items(cycle: str):
    """Return the frozen items for a specific cycle."""
    frozen = get_frozen_cycles()
    if cycle not in frozen:
        raise HTTPException(status_code=404, detail=f"Cycle {cycle} is not frozen")

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT jira_key, title, status, color_status, url, "
            "       product_name, department, parent_key, parent_summary, "
            "       rank, parent_rank, tags "
            "FROM cycle_freeze_item WHERE cycle = %s "
            "ORDER BY parent_rank NULLS LAST, rank NULLS LAST, title",
            (cycle,),
        )
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    return {
        "data": [dict(zip(columns, row, strict=False)) for row in rows],
        "meta": {"total": len(rows), "cycle": cycle, **frozen[cycle]},
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
    frontend can dynamically update the product dropdown when the department changes,
    and a ``cycle_states`` mapping (cycle → state) from ``cycle_config``.
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
        # Also include cycles that exist in cycle_config or cycle_freeze.
        cur.execute("SELECT DISTINCT unnest(tags) AS tag FROM roadmap_item")
        all_tags = [r[0] for r in cur.fetchall()]
        live_cycles = {t for t in all_tags if CYCLE_RE.match(t)}

        cur.execute("SELECT cycle FROM cycle_freeze")
        frozen_cycles = {r[0] for r in cur.fetchall()}

        cur.execute("SELECT cycle FROM cycle_config")
        config_cycles = {r[0] for r in cur.fetchall()}

        cycles = sorted(live_cycles | frozen_cycles | config_cycles, reverse=True)

        # Cycle state map for UI badges
        cur.execute("SELECT cycle, state FROM cycle_config")
        cycle_states = {r[0]: r[1] for r in cur.fetchall()}

    return {
        "departments": departments,
        "products": products,
        "cycles": cycles,
        "dept_products": dept_products,
        "cycle_states": cycle_states,
    }


def _query_frozen_items_for_cycle(
    frozen_cycle: str,
    department: str | None = None,
    product: str | None = None,
) -> list[dict]:
    """Query ``cycle_freeze_item`` for a single frozen cycle, applying product/dept filters."""
    clauses: list[str] = ["f.cycle = %s"]
    params: list = [frozen_cycle]

    if department:
        clauses.append("f.department = %s")
        params.append(department)
    if product:
        clauses.append("f.product_name = %s")
        params.append(product)

    where = " WHERE " + " AND ".join(clauses)
    query = (
        "SELECT f.jira_key, f.title, f.product_name AS product, "
        "       f.color_status, f.url, f.tags, "
        "       f.parent_key, f.parent_summary, f.rank, f.parent_rank "
        f"FROM cycle_freeze_item f{where} "
        "ORDER BY f.parent_rank NULLS LAST, f.rank NULLS LAST, f.title"
    )

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    items = []
    for row in rows:
        item = dict(zip(columns, row, strict=False))
        cs = item.get("color_status")
        if isinstance(cs, str):
            item["color_status"] = json.loads(cs)
        items.append(item)
    return items


def _query_roadmap_items(
    department: str | None = None,
    product: str | None = None,
    cycle: str | None = None,
) -> tuple[OrderedDict[str, OrderedDict[str, list[dict]]], dict[str, str], dict[str, str]]:
    """Return roadmap items grouped by cycle → objective (parent).

    Structure: ``{cycle: {objective_label: [items]}}``
    - Cycles sorted newest-first.
    - Objectives sorted alphabetically within each cycle.
    - Items with no parent are grouped under "No objective".
    - Items with no cycle labels are excluded.
    - An item with multiple cycle labels appears in each bucket.
    - **Frozen cycles** are served from ``cycle_freeze_item`` instead of live data.
    - **Future cycles** have all item colors overridden to white/Inactive.
    - **Carry-over** counts only frozen cycle labels on an item.

    Returns:
        A tuple of (grouped_items, objective_urls, cycle_states_in_view).
        ``cycle_states_in_view`` maps cycle label → state (``"frozen"``/``"current"``/``"future"``/``None``).
    """
    frozen_map = get_frozen_cycles()  # {cycle: {frozen_at, frozen_by, note}}
    config_map = get_cycle_configs()  # {cycle: {state, updated_at, updated_by}}

    # Determine which cycles are in which state
    frozen_cycle_labels = {c for c, cfg in config_map.items() if cfg["state"] == "frozen"}
    future_cycle_labels = {c for c, cfg in config_map.items() if cfg["state"] == "future"}

    # ── Live items from roadmap_item ──
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
    cycle_states_in_view: dict[str, str] = {}

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
            # Skip frozen cycles here — they'll be populated from freeze data below
            if c in frozen_map:
                continue

            # Future cycle override: force all items to white/Inactive
            if c in future_cycle_labels:
                display_item = dict(item)
                display_item["color_status"] = {
                    "health": {"color": "white"},
                    "carry_over": None,
                }
                raw.setdefault(c, {}).setdefault(objective_label, []).append(display_item)
            else:
                # Recalculate carry-over to only count frozen cycle labels
                display_item = dict(item)
                item_cs = display_item.get("color_status") or {}
                item_cycle_labels = [t for t in tags if CYCLE_RE.match(t)]
                frozen_count = sum(1 for lbl in item_cycle_labels if lbl in frozen_cycle_labels)
                if frozen_count > 0:
                    item_cs = dict(item_cs)
                    item_cs["carry_over"] = {"color": "purple", "count": frozen_count}
                else:
                    item_cs = dict(item_cs)
                    item_cs["carry_over"] = None
                display_item["color_status"] = item_cs
                raw.setdefault(c, {}).setdefault(objective_label, []).append(display_item)

            state = config_map[c]["state"] if c in config_map else None
            cycle_states_in_view[c] = state

    # ── Frozen cycle data ──
    # Determine which frozen cycles to include
    if cycle and cycle in frozen_map:
        frozen_to_load = [cycle]
    elif cycle:
        frozen_to_load = []  # specific non-frozen cycle requested
    else:
        frozen_to_load = list(frozen_map.keys())  # all frozen cycles

    for fc in frozen_to_load:
        frozen_items = _query_frozen_items_for_cycle(fc, department=department, product=product)
        cycle_states_in_view[fc] = config_map[fc]["state"] if fc in config_map else "frozen"

        for item in frozen_items:
            # Recalculate carry-over for frozen items too
            item_tags = item.get("tags") or []
            item_cycle_labels = [t for t in item_tags if CYCLE_RE.match(t)]
            frozen_count = sum(1 for lbl in item_cycle_labels if lbl in frozen_cycle_labels and lbl != fc)
            item_cs = item.get("color_status") or {}
            if isinstance(item_cs, str):
                item_cs = json.loads(item_cs)
            item_cs = dict(item_cs)
            if frozen_count > 0:
                item_cs["carry_over"] = {"color": "purple", "count": frozen_count}
            else:
                item_cs["carry_over"] = None
            item["color_status"] = item_cs

            parent_key = item.get("parent_key")
            parent_summary = item.get("parent_summary")
            if parent_key and parent_summary:
                objective_label = parent_summary
                objective_urls[objective_label] = f"{settings.jira_url}/browse/{parent_key}"
            else:
                objective_label = "No objective"

            item["_objective"] = objective_label
            raw.setdefault(fc, {}).setdefault(objective_label, []).append(item)

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

    return grouped, objective_urls, cycle_states_in_view


@app.get("/", response_class=HTMLResponse)
def roadmap_page(
    request: Request,
    department: str | None = Query(None),
    product: str | None = Query(None),
    cycle: str | None = Query(None),
):
    """Render the main roadmap page with server-side Jinja2 templates."""
    options = _query_filter_options(department=department)

    # Force a product selection — if none chosen, default to the first available
    available_products = options["products"]
    if not product or product not in available_products:
        product = available_products[0] if available_products else None

    grouped_items, objective_urls, cycle_states = _query_roadmap_items(
        department=department, product=product, cycle=cycle,
    )

    return templates.TemplateResponse(
        request,
        "roadmap.html",
        {
            "departments": options["departments"],
            "products": available_products,
            "cycles": options["cycles"],
            "dept_products_json": json.dumps(options["dept_products"]),
            "cycle_states": options["cycle_states"],
            "selected_department": department or "",
            "selected_product": product or "",
            "selected_cycle": cycle or "",
            "grouped_items": grouped_items,
            "objective_urls": objective_urls,
            "cycle_states_in_view": cycle_states,
        },
    )
