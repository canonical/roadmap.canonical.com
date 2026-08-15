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
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import planning
from .auth import configure_oauth, handle_callback, is_authenticated, login_redirect
from .database import close_pool, get_async_conn, get_db_connection, open_pool
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
from .scheduler import _update_sync_metadata
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
            # Split and execute each statement so errors in one don't silently
            # abort the rest (psycopg executes multiple statements in one
            # cursor.execute() call, but stops on first error).
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in statements:
                # Skip the seed INSERTs that use ON CONFLICT so they don't fail
                # on empty split, and skip comments
                if stmt.startswith("--"):
                    continue
                try:
                    cur.execute(stmt + ";")
                except Exception as exc:
                    # Idempotent DDL errors (e.g. column already exists) are harmless
                    logger.warning("Schema statement skipped (may already exist): %s", exc)
            conn.commit()
    logger.info("Database schema applied")

    # Configure OIDC (Authlib)
    if settings.oidc_client_id:
        configure_oauth()
        logger.info("  OIDC issuer   = %s", settings.oidc_issuer)
    else:
        logger.warning("  OIDC_CLIENT_ID is empty — authentication disabled")

    await open_pool()
    logger.info("Async connection pool opened")

    yield

    await close_pool()
    logger.info("Async connection pool closed")


# Paths that must be accessible without authentication
_PUBLIC_PATHS = {"/login", "/callback"}


class OIDCAuthMiddleware(BaseHTTPMiddleware):
    """Enforce OIDC authentication on all routes except /login and /callback.

    - Browser requests (HTML pages) → redirect to /login
    - API requests (/api/*) → return 401 JSON
    - Disabled entirely when OIDC_CLIENT_ID is empty
    """

    async def dispatch(self, request: Request, call_next):
        if settings.oidc_client_id and request.scope["path"] not in _PUBLIC_PATHS and not is_authenticated(request):
            if request.scope["path"].startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                )
            return RedirectResponse(url="/login")
        return await call_next(request)


_OPENAPI_TAGS = [
    {"name": "Sync", "description": "Jira sync trigger and status"},
    {"name": "Roadmap", "description": "Roadmap items (JSON)"},
    {"name": "Products", "description": "Product and Jira-source mapping CRUD"},
    {"name": "Snapshots", "description": "Daily snapshots and change-tracking diffs"},
    {"name": "Cycles", "description": "Cycle lifecycle management (frozen / current / future)"},
    {"name": "Auth", "description": "OIDC authentication flow"},
    {"name": "Planning", "description": "Capacity planning — roles, members, availability, curves"},
]

app = FastAPI(
    title="Roadmap API",
    version="0.1.0",
    description=(
        "Company-wide roadmap visualisation tool.  Data flows from Jira → PostgreSQL → this API → server-rendered UI."
    ),
    lifespan=lifespan,
    openapi_tags=_OPENAPI_TAGS,
)

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
    _update_sync_metadata(started=True, error="")
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
        _update_sync_metadata(
            finished=True,
            ok=True,
            interval=settings.sync_interval_seconds,
        )
    except Exception as exc:
        logger.exception("Sync failed: %s", exc)
        _sync_status["state"] = "failed"
        _sync_status["error"] = str(exc)
        _update_sync_metadata(
            finished=True,
            ok=False,
            error=str(exc),
            interval=settings.sync_interval_seconds,
        )
    finally:
        _sync_status["last_sync_end"] = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Authentication endpoints
# ---------------------------------------------------------------------------


@app.get("/login", tags=["Auth"])
async def login(request: Request):
    """Redirect to the OIDC provider for login."""
    if not settings.oidc_client_id:
        raise HTTPException(status_code=501, detail="OIDC not configured")
    return await login_redirect(request)


@app.get("/callback", tags=["Auth"])
async def callback(request: Request):
    """Handle the OIDC callback (authorization code exchange)."""
    return await handle_callback(request)


@app.get("/token", response_class=HTMLResponse, tags=["Auth"])
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
<h2>Cookie value</h2>
<div style="display:flex;gap:.5rem;align-items:center;">
<input id="cookie-val" type="text" class="p-form-validation__input" readonly
       value="roadmap_session={cookie_value}" style="font-family:monospace;flex:1;">
    <button class="p-button--positive" 
        onclick="navigator.clipboard.writeText(document.getElementById('cookie-val').value)">Copy
    </button>
</div>
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


@app.post("/api/v1/sync", tags=["Sync"])
def trigger_sync(background_tasks: BackgroundTasks):
    """Kick off a background Jira sync."""
    if _sync_status["state"] in ("syncing", "processing"):
        return {"message": "Sync already in progress", "status": _sync_status}
    background_tasks.add_task(_run_full_sync)
    return {"message": "Sync started"}


@app.get("/api/v1/status", tags=["Sync"])
async def get_status():
    """Return current sync status enriched with DB row counts and active config."""
    db_counts = {}
    db_error_message = None
    db_last_sync_ok = None
    try:
        async with get_async_conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM jira_issue_raw")
            db_counts["raw_issues"] = (await cur.fetchone())[0]
            await cur.execute("SELECT count(*) FROM jira_issue_raw WHERE processed_at IS NOT NULL")
            db_counts["raw_processed"] = (await cur.fetchone())[0]
            await cur.execute("SELECT count(*) FROM roadmap_item")
            db_counts["roadmap_items"] = (await cur.fetchone())[0]
            await cur.execute("SELECT count(*) FROM product")
            db_counts["products"] = (await cur.fetchone())[0]
            await cur.execute("SELECT count(DISTINCT snapshot_date) FROM roadmap_snapshot")
            db_counts["snapshot_dates"] = (await cur.fetchone())[0]
            await cur.execute("SELECT count(*) FROM roadmap_snapshot")
            db_counts["snapshot_rows"] = (await cur.fetchone())[0]
            # Also read persisted error from sync_metadata (written by scheduler)
            await cur.execute("SELECT error_message, last_sync_ok FROM sync_metadata WHERE id = 1")
            meta_row = await cur.fetchone()
            if meta_row:
                db_error_message = meta_row[0]
                db_last_sync_ok = meta_row[1]
    except Exception:
        db_counts["error"] = "could not query database"

    try:
        effective_jql = _build_jql()
    except (RuntimeError, Exception):
        effective_jql = "(no projects configured)"

    # Merge in-memory status with persisted error from sync_metadata.
    # The scheduler runs in a separate process, so the in-memory _sync_status
    # may still show "idle" / error=None even after a failed scheduler sync.
    error = _sync_status.get("error")
    state = _sync_status.get("state", "idle")
    if not error and db_error_message:
        error = db_error_message
    if state == "idle" and db_last_sync_ok is False:
        state = "failed"

    return {
        **_sync_status,
        "state": state,
        "error": error,
        "config": {
            "jira_url": settings.jira_url,
            "jql_filter": settings.jql_filter,
            "effective_jql": effective_jql,
            "database_url": settings.database_url.rsplit("@", 1)[-1],  # hide password
        },
        "db": db_counts,
    }


@app.get("/api/v1/sync/schedule", tags=["Sync"])
async def get_sync_schedule():
    """Return scheduler timing info (last sync, next sync, interval).

    This reads from the ``sync_metadata`` table which the scheduler process
    keeps up-to-date, so it survives process restarts.
    """
    try:
        async with get_async_conn() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT last_sync_start, last_sync_end, last_sync_ok, "
                "       next_sync_at, interval_seconds, error_message "
                "FROM sync_metadata WHERE id = 1"
            )
            row = await cur.fetchone()
    except Exception:
        return {"error": "could not query sync_metadata"}

    if row is None:
        return {"configured": False}

    last_start, last_end, last_ok, next_at, interval, error = row
    now = datetime.now(UTC)

    return {
        "configured": True,
        "last_sync_start": last_start.isoformat() if last_start else None,
        "last_sync_end": last_end.isoformat() if last_end else None,
        "last_sync_ok": last_ok,
        "next_sync_at": next_at.isoformat() if next_at else None,
        "interval_seconds": interval,
        "seconds_since_last_sync": (int((now - last_end).total_seconds()) if last_end else None),
        "seconds_until_next_sync": (int((next_at - now).total_seconds()) if next_at else None),
        "error_message": error,
    }


# ---------------------------------------------------------------------------
# Snapshot diff endpoints — biweekly change reports
# ---------------------------------------------------------------------------


@app.get("/api/v1/snapshots", tags=["Snapshots"])
async def list_snapshots():
    """List all available snapshot dates (newest first)."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, count(*) AS item_count "
            "FROM roadmap_snapshot GROUP BY snapshot_date ORDER BY snapshot_date DESC"
        )
        rows = await cur.fetchall()
    return {
        "data": [{"date": str(r[0]), "item_count": r[1]} for r in rows],
        "meta": {"total": len(rows)},
    }


@app.get("/api/v1/snapshots/diff", tags=["Snapshots"])
async def snapshot_diff(
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
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Verify both dates exist
        await cur.execute(
            "SELECT DISTINCT snapshot_date FROM roadmap_snapshot WHERE snapshot_date IN (%s, %s)",
            (from_date, to_date),
        )
        found_dates = {str(r[0]) for r in await cur.fetchall()}
        missing = {from_date, to_date} - found_dates
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"No snapshot found for date(s): {', '.join(sorted(missing))}",
            )

        # --- Color changes (including turned_red) ---
        await cur.execute(
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
        color_changes = [dict(zip(color_cols, r, strict=False)) for r in await cur.fetchall()]
        turned_red = [c for c in color_changes if c["new_color"] == "red"]

        # --- Disappeared items ---
        await cur.execute(
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
        disappeared = [dict(zip(dis_cols, r, strict=False)) for r in await cur.fetchall()]

        # --- Appeared items ---
        await cur.execute(
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
        appeared = [dict(zip(app_cols, r, strict=False)) for r in await cur.fetchall()]

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


@app.get("/api/v1/cycles", tags=["Cycles"])
async def list_cycles():
    """List all known cycles with their state and metadata."""
    configs = get_cycle_configs()
    frozen = get_frozen_cycles()

    # Also gather live cycles from roadmap_item tags
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT unnest(tags) AS tag FROM roadmap_item")
        all_tags = [r[0] for r in await cur.fetchall()]

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


@app.post("/api/v1/cycles/{cycle}", status_code=201, tags=["Cycles"])
async def register_cycle_endpoint(cycle: str, body: CycleRegisterIn | None = None, request: Request = None):
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


@app.put("/api/v1/cycles/{cycle}", tags=["Cycles"])
async def set_cycle_state_endpoint(cycle: str, body: CycleStateIn, request: Request = None):
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


class CycleDatesIn(BaseModel):
    """Input schema for updating cycle start and end dates."""

    start_date: str
    end_date: str


@app.put("/api/v1/cycles/{cycle}/dates", tags=["Cycles"])
async def set_cycle_dates_endpoint(cycle: str, body: CycleDatesIn, request: Request = None):
    """Set the start and end dates for a registered cycle."""
    updated_by = None
    if request and request.session.get("user"):
        updated_by = request.session["user"].get("email")

    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM cycle_config WHERE cycle = %s", (cycle,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Cycle {cycle} is not registered")

        await cur.execute(
            "UPDATE cycle_config SET start_date = %s, end_date = %s, updated_at = now(), updated_by = %s WHERE cycle = %s",
            (body.start_date, body.end_date, updated_by, cycle),
        )
        await conn.commit()

    return {"message": f"Cycle {cycle} dates updated", "start_date": body.start_date, "end_date": body.end_date}


@app.get("/api/v1/cycles/{cycle}/dates", tags=["Cycles"])
async def get_cycle_dates_endpoint(cycle: str):
    """Get the start and end dates for a registered cycle."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT cycle, state, start_date, end_date, updated_at, updated_by FROM cycle_config WHERE cycle = %s",
            (cycle,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Cycle {cycle} is not registered")

    return {
        "cycle": row[0],
        "state": row[1],
        "start_date": str(row[2]) if row[2] else None,
        "end_date": str(row[3]) if row[3] else None,
        "updated_at": row[4].isoformat() if row[4] else None,
        "updated_by": row[5],
    }


@app.delete("/api/v1/cycles/{cycle}", status_code=200, tags=["Cycles"])
async def remove_cycle_endpoint(cycle: str):
    """Remove a cycle from the registry (also deletes freeze data if frozen)."""
    try:
        remove_cycle(cycle)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {"message": f"Cycle {cycle} removed"}


@app.get("/api/v1/cycles/{cycle}/items", tags=["Cycles"])
async def get_frozen_cycle_items(cycle: str):
    """Return the frozen items for a specific cycle."""
    frozen = get_frozen_cycles()
    if cycle not in frozen:
        raise HTTPException(status_code=404, detail=f"Cycle {cycle} is not frozen")

    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT jira_key, title, status, color_status, url, "
            "       product_name, department, parent_key, parent_summary, "
            "       rank, parent_rank, tags "
            "FROM cycle_freeze_item WHERE cycle = %s "
            "ORDER BY NULLIF(parent_rank, '') NULLS LAST, "
            "         NULLIF(rank, '') NULLS LAST, title",
            (cycle,),
        )
        columns = [desc[0] for desc in cur.description]
        rows = await cur.fetchall()

    return {
        "data": [dict(zip(columns, row, strict=False)) for row in rows],
        "meta": {"total": len(rows), "cycle": cycle, **frozen[cycle]},
    }


@app.get("/api/v1/roadmap", tags=["Roadmap"])
async def get_roadmap(
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

    clauses.append("r.is_deleted = FALSE")
    where = f" WHERE {' AND '.join(clauses)}"
    query = (
        "SELECT r.id, r.jira_key, r.title, p.name AS product, p.department, "
        "       r.color_status, r.url, r.tags, "
        "       r.parent_key, r.parent_summary, r.created_at, r.updated_at "
        "FROM roadmap_item r "
        f"LEFT JOIN product p ON p.id = r.product_id{where} "
        "ORDER BY r.updated_at DESC"
    )

    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = await cur.fetchall()

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


async def _fetch_product_with_sources(cur, product_id: int) -> dict | None:
    """Read a single product + its jira_sources from the DB. Returns None if not found."""
    await cur.execute("SELECT id, name, department FROM product WHERE id = %s", (product_id,))
    row = await cur.fetchone()
    if not row:
        return None
    product = {"id": row[0], "name": row[1], "department": row[2]}
    await cur.execute(
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
        for r in await cur.fetchall()
    ]
    return product


@app.get("/api/v1/products", tags=["Products"])
async def list_products():
    """List all products with their Jira source mappings."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM product ORDER BY department, name")
        product_ids = [r[0] for r in await cur.fetchall()]
        products = [await _fetch_product_with_sources(cur, pid) for pid in product_ids]
    return {"data": products, "meta": {"total": len(products)}}


@app.get("/api/v1/products/{product_id}", tags=["Products"])
async def get_product(product_id: int):
    """Get a single product by ID."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        product = await _fetch_product_with_sources(cur, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"data": product}


@app.post("/api/v1/products", status_code=201, tags=["Products"])
async def create_product(body: ProductIn):
    """Create a product with optional Jira source mappings."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO product (name, department) VALUES (%s, %s) RETURNING id",
            (body.name, body.department),
        )
        product_id = (await cur.fetchone())[0]

        for src in body.jira_sources:
            await cur.execute(
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

        await conn.commit()
        product = await _fetch_product_with_sources(cur, product_id)

    return {"data": product}


@app.put("/api/v1/products/{product_id}", tags=["Products"])
async def update_product(product_id: int, body: ProductIn):
    """Replace a product's details and Jira source mappings entirely."""
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM product WHERE id = %s", (product_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Product not found")

        await cur.execute(
            "UPDATE product SET name = %s, department = %s, updated_at = now() WHERE id = %s",
            (body.name, body.department, product_id),
        )

        # Replace all source rules (simple and safe for small cardinality)
        await cur.execute("DELETE FROM product_jira_source WHERE product_id = %s", (product_id,))
        for src in body.jira_sources:
            await cur.execute(
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

        await conn.commit()
        product = await _fetch_product_with_sources(cur, product_id)

    return {"data": product}


@app.delete("/api/v1/products/{product_id}", status_code=204, tags=["Products"])
async def delete_product(product_id: int):
    """Delete a product and its Jira source mappings.

    Roadmap items referencing this product will have their product_id set to NULL.
    """
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT id FROM product WHERE id = %s", (product_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Product not found")

        # Unlink roadmap items so they don't get cascade-deleted
        await cur.execute("UPDATE roadmap_item SET product_id = NULL WHERE product_id = %s", (product_id,))
        await cur.execute("DELETE FROM product WHERE id = %s", (product_id,))
        await conn.commit()

    return None


# ---------------------------------------------------------------------------
# Capacity Planning Endpoints
# ---------------------------------------------------------------------------


class RoleIn(BaseModel):
    name: str
    sort_order: int = 0
    is_default: bool = False


class MemberIn(BaseModel):
    name: str
    role_id: int | None = None
    individual_coefficient: float = 1.0
    is_active: bool = True


class AvailabilityBulkIn(BaseModel):
    entries: list[dict]


class PlanningConfigIn(BaseModel):
    cycle_id: str
    team_efficiency: float = Field(0.60, ge=0.01, le=1.00)


class EpicEstimateIn(BaseModel):
    estimates: list[dict]


class EpicSelectionIn(BaseModel):
    cycle: str
    is_in_roadmap: bool
    is_dropped: bool = False


class EpicProgressIn(BaseModel):
    cycle: str
    week_start_date: str
    remaining_days: int | None = None


# --- Roles ---

@app.get("/api/v1/products/{product_id}/roles", tags=["Planning"])
async def list_roles_endpoint(request: Request, product_id: int):
    roles = await planning.list_roles(product_id)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "planning/_roles.html", {"product_id": product_id, "roles": roles})
    return {"data": roles}


@app.post("/api/v1/products/{product_id}/roles", status_code=201, tags=["Planning"])
async def create_role_endpoint(request: Request, product_id: int, body: RoleIn):
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    try:
        role = await planning.create_role(product_id, body.name, body.sort_order, body.is_default, changed_by=changed_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.headers.get("HX-Request") == "true":
        roles = await planning.list_roles(product_id)
        return templates.TemplateResponse(request, "planning/_roles.html", {"product_id": product_id, "roles": roles}, headers={"HX-Trigger": "roles-updated"})
    return {"data": role}


@app.put("/api/v1/products/{product_id}/roles/{role_id}", tags=["Planning"])
async def update_role_endpoint(request: Request, product_id: int, role_id: int, body: RoleIn):
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    try:
        role = await planning.update_role(role_id, body.name, body.sort_order, body.is_default, changed_by=changed_by)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if request.headers.get("HX-Request") == "true":
        roles = await planning.list_roles(product_id)
        return templates.TemplateResponse(request, "planning/_roles.html", {"product_id": product_id, "roles": roles}, headers={"HX-Trigger": "roles-updated"})
    return {"data": role}


@app.delete("/api/v1/products/{product_id}/roles/{role_id}", status_code=204, tags=["Planning"])
async def delete_role_endpoint(request: Request, product_id: int, role_id: int):
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    await planning.delete_role(role_id, changed_by=changed_by)
    if request.headers.get("HX-Request") == "true":
        roles = await planning.list_roles(product_id)
        return templates.TemplateResponse(request, "planning/_roles.html", {"product_id": product_id, "roles": roles}, headers={"HX-Trigger": "roles-updated"})
    return None


# --- Members ---

@app.get("/api/v1/products/{product_id}/members", tags=["Planning"])
async def list_members_endpoint(request: Request, product_id: int):
    members = await planning.list_members(product_id)
    roles = await planning.list_roles(product_id)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "planning/_members.html", {"product_id": product_id, "members": members, "roles": roles})
    return {"data": members}


@app.post("/api/v1/products/{product_id}/members", status_code=201, tags=["Planning"])
async def create_member_endpoint(request: Request, product_id: int, body: MemberIn):
    from decimal import Decimal
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    member = await planning.create_member(
        product_id, body.name, body.role_id, Decimal(str(body.individual_coefficient)), body.is_active
    )
    if request.headers.get("HX-Request") == "true":
        members = await planning.list_members(product_id)
        roles = await planning.list_roles(product_id)
        return templates.TemplateResponse(request, "planning/_members.html", {"product_id": product_id, "members": members, "roles": roles}, headers={"HX-Trigger": "members-updated"})
    return {"data": member}


@app.put("/api/v1/products/{product_id}/members/{member_id}", tags=["Planning"])
async def update_member_endpoint(request: Request, product_id: int, member_id: int, body: MemberIn):
    from decimal import Decimal
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    try:
        member = await planning.update_member(
            member_id,
            name=body.name,
            role_id=body.role_id,
            individual_coefficient=Decimal(str(body.individual_coefficient)),
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Log the update manually for members
    await planning.write_audit_log(product_id, "team_member", member_id, "UPDATE", None, member, changed_by)
    if request.headers.get("HX-Request") == "true":
        members = await planning.list_members(product_id)
        roles = await planning.list_roles(product_id)
        return templates.TemplateResponse(request, "planning/_members.html", {"product_id": product_id, "members": members, "roles": roles}, headers={"HX-Trigger": "members-updated,availability-updated"})
    return {"data": member}


@app.delete("/api/v1/products/{product_id}/members/{member_id}", status_code=204, tags=["Planning"])
async def delete_member_endpoint(request: Request, product_id: int, member_id: int):
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    try:
        old = await planning.get_member(member_id)
    except ValueError:
        old = None
    await planning.delete_member(member_id)
    if old:
        await planning.write_audit_log(product_id, "team_member", member_id, "DELETE", old, None, changed_by)
    if request.headers.get("HX-Request") == "true":
        members = await planning.list_members(product_id)
        roles = await planning.list_roles(product_id)
        return templates.TemplateResponse(request, "planning/_members.html", {"product_id": product_id, "members": members, "roles": roles}, headers={"HX-Trigger": "members-updated,availability-updated"})
    return None


# --- Availability ---

@app.get("/api/v1/products/{product_id}/availability", tags=["Planning"])
async def get_availability_endpoint(request: Request, product_id: int, cycle: str):
    data = await planning.get_availability(product_id, cycle)
    if request.headers.get("HX-Request") == "true":
        # Compute member totals
        member_totals = {}
        for m in data.get("members", []):
            total = sum(data.get("grid", {}).get(m["id"], {}).values())
            member_totals[m["id"]] = total
        return templates.TemplateResponse(request, "planning/_availability.html", {
            "product_id": product_id,
            "members": data.get("members", []),
            "weeks": data.get("weeks", []),
            "grid": data.get("grid", {}),
            "member_totals": member_totals,
        })
    return data


@app.post("/api/v1/products/{product_id}/availability/bulk", tags=["Planning"])
async def bulk_availability_endpoint(request: Request, product_id: int, body: AvailabilityBulkIn):
    from datetime import date
    entries = []
    for e in body.entries:
        entries.append({
            "member_id": e["member_id"],
            "week_start_date": date.fromisoformat(e["week_start_date"]),
            "days_available": e["days_available"],
        })
    result = await planning.bulk_set_availability(entries)
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Trigger": "availability-updated"})
    return result


@app.put("/api/v1/products/{product_id}/availability/{member_id}/{week_start_date}", tags=["Planning"])
async def set_availability_endpoint(request: Request, product_id: int, member_id: int, week_start_date: str, days_available: int = Query(..., ge=0, le=5)):
    from datetime import date
    result = await planning.set_availability(member_id, date.fromisoformat(week_start_date), days_available)
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Trigger": "availability-updated"})
    return result


# --- Planning Config ---

@app.get("/api/v1/products/{product_id}/planning-config", tags=["Planning"])
async def get_planning_config_endpoint(request: Request, product_id: int):
    cfg = await planning.get_planning_config(product_id)
    cycles = await _get_cycles_for_config()
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "planning/_config.html", {"product_id": product_id, "config": cfg, "cycles": cycles})
    return {"data": cfg}


@app.put("/api/v1/products/{product_id}/planning-config", tags=["Planning"])
async def set_planning_config_endpoint(request: Request, product_id: int, body: PlanningConfigIn):
    from decimal import Decimal
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    old_cfg = await planning.get_planning_config(product_id)
    cfg = await planning.set_planning_config(product_id, body.cycle_id, Decimal(str(body.team_efficiency)))
    await planning.write_audit_log(product_id, "product_planning_config", product_id, "UPDATE", old_cfg, cfg, changed_by)
    if request.headers.get("HX-Request") == "true":
        cycles = await _get_cycles_for_config()
        return templates.TemplateResponse(request, "planning/_config.html", {"product_id": product_id, "config": cfg, "cycles": cycles}, headers={"HX-Trigger": "config-updated"})
    return {"data": cfg}


async def _get_cycles_for_config():
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT cycle FROM cycle_config ORDER BY cycle DESC")
        return [{"cycle": r[0]} for r in await cur.fetchall()]


# --- Epic Estimates ---

@app.get("/api/v1/epics/{item_id}/estimates", tags=["Planning"])
async def get_epic_estimates_endpoint(item_id: int):
    return {"data": await planning.get_epic_estimates(item_id)}


@app.put("/api/v1/epics/{item_id}/estimates", tags=["Planning"])
async def set_epic_estimates_endpoint(request: Request, item_id: int, body: EpicEstimateIn):
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    old_est = await planning.get_epic_estimates(item_id)
    result = await planning.set_epic_estimates(item_id, body.estimates)
    # Need product_id for audit log
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT product_id FROM roadmap_item WHERE id = %s", (item_id,))
        row = await cur.fetchone()
        product_id = row[0] if row else None
    if product_id:
        await planning.write_audit_log(product_id, "epic_role_estimate", item_id, "UPDATE", {"estimates": old_est}, {"estimates": result}, changed_by)
    return {"data": result}


# --- Epic Selection ---

@app.get("/api/v1/epics/{item_id}/selection", tags=["Planning"])
async def get_epic_selection_endpoint(item_id: int, cycle: str):
    return {"data": await planning.get_epic_selection(item_id, cycle)}


@app.put("/api/v1/epics/{item_id}/selection", tags=["Planning"])
async def set_epic_selection_endpoint(request: Request, item_id: int, body: EpicSelectionIn):
    changed_by = request.session.get("user", {}).get("email") if request.session.get("user") else None
    old_sel = await planning.get_epic_selection(item_id, body.cycle)
    result = await planning.set_epic_selection(item_id, body.cycle, body.is_in_roadmap, body.is_dropped)
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT product_id FROM roadmap_item WHERE id = %s", (item_id,))
        row = await cur.fetchone()
        product_id = row[0] if row else None
    if product_id:
        await planning.write_audit_log(product_id, "epic_cycle_selection", item_id, "UPDATE", old_sel, result, changed_by)
    return {"data": result}


# --- Epic Progress ---

@app.get("/api/v1/epics/{item_id}/progress", tags=["Planning"])
async def get_epic_progress_endpoint(item_id: int, cycle: str):
    return await planning.get_epic_progress(item_id, cycle)


@app.post("/api/v1/epics/{item_id}/progress", tags=["Planning"])
async def set_epic_progress_endpoint(request: Request, item_id: int, body: EpicProgressIn):
    from datetime import date
    created_by = None
    if request.session.get("user"):
        created_by = request.session["user"].get("email")
    old_prog = await planning.get_epic_progress(item_id, body.cycle)
    result = await planning.set_epic_progress(
        item_id, date.fromisoformat(body.week_start_date), body.remaining_days, created_by
    )
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT product_id FROM roadmap_item WHERE id = %s", (item_id,))
        row = await cur.fetchone()
        product_id = row[0] if row else None
    if product_id:
        await planning.write_audit_log(product_id, "epic_weekly_progress", item_id, "UPDATE", old_prog, result, created_by)
    return {"data": result}


# --- Curves ---

@app.get("/api/v1/products/{product_id}/curves", tags=["Planning"])
async def get_curves_endpoint(product_id: int, cycle: str):
    return await planning.calculate_curves(product_id, cycle)


# --- Undo ---

@app.post("/api/v1/products/{product_id}/undo", tags=["Planning"])
async def undo_endpoint(product_id: int, request: Request):
    changed_by = None
    if request.session.get("user"):
        changed_by = request.session["user"].get("email")
    try:
        result = await planning.undo_last_change(product_id, changed_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}


# ---------------------------------------------------------------------------
# Planning Page (server-rendered HTML)
# ---------------------------------------------------------------------------

@app.get("/products/{product_id}/planning", response_class=HTMLResponse)
async def planning_page(request: Request, product_id: int, cycle: str | None = Query(None)):
    """Render the capacity planning page for a product."""
    dates_populated = False
    async with get_async_conn() as conn, conn.cursor() as cur:
        # Verify product exists
        await cur.execute("SELECT id, name, department FROM product WHERE id = %s", (product_id,))
        product_row = await cur.fetchone()
        if not product_row:
            raise HTTPException(status_code=404, detail="Product not found")
        product_name, department = product_row[1], product_row[2]

        # Check whether any cycles exist at all
        await cur.execute("SELECT COUNT(*) FROM cycle_config")
        total_cycles = (await cur.fetchone())[0]

        # Check whether any cycles already have dates
        await cur.execute("SELECT COUNT(*) FROM cycle_config WHERE start_date IS NOT NULL AND end_date IS NOT NULL")
        count_with_dates = (await cur.fetchone())[0]

        # Auto-populate dates using Canonical convention if none exist
        if total_cycles > 0 and count_with_dates == 0:
            updated = await planning.auto_populate_cycle_dates()
            dates_populated = updated > 0

        # Available cycles with dates
        await cur.execute(
            "SELECT cycle, state, start_date, end_date FROM cycle_config WHERE start_date IS NOT NULL AND end_date IS NOT NULL ORDER BY cycle DESC"
        )
        cycles = [
            {"cycle": r[0], "state": r[1], "start_date": str(r[2]) if r[2] else None, "end_date": str(r[3]) if r[3] else None}
            for r in await cur.fetchall()
        ]

        # Default to current cycle, or latest cycle
        selected_cycle = cycle
        if not selected_cycle and cycles:
            current = [c for c in cycles if c["state"] == "current"]
            selected_cycle = current[0]["cycle"] if current else cycles[0]["cycle"]

    return templates.TemplateResponse(
        request,
        "planning.html",
        {
            "product_id": product_id,
            "product_name": product_name,
            "department": department,
            "cycles": cycles,
            "selected_cycle": selected_cycle,
            "total_cycles": total_cycles,
            "dates_populated": dates_populated,
        },
    )


# ---------------------------------------------------------------------------
# Server-rendered HTML page (original roadmap)
# ---------------------------------------------------------------------------

CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")


async def _query_filter_options(department: str | None = None) -> dict:
    """Fetch distinct departments, products (filtered by department), and cycle labels for filter dropdowns.

    Also returns a ``dept_products`` mapping (department → [product names]) so the
    frontend can dynamically update the product dropdown when the department changes,
    and a ``cycle_states`` mapping (cycle → state) from ``cycle_config``.
    """
    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT department FROM product ORDER BY department")
        departments = [r[0] for r in await cur.fetchall()]

        # Products for the selected department (or all if none selected)
        if department:
            await cur.execute("SELECT DISTINCT name FROM product WHERE department = %s ORDER BY name", (department,))
        else:
            await cur.execute("SELECT DISTINCT name FROM product ORDER BY name")
        products = [r[0] for r in await cur.fetchall()]

        # Full department → products mapping for client-side filtering
        await cur.execute("SELECT department, name FROM product ORDER BY department, name")
        dept_products: dict[str, list[str]] = {}
        for r in await cur.fetchall():
            dept_products.setdefault(r[0], []).append(r[1])

        # Cycles come from the tags array (labels) on roadmap_item (non-deleted).
        # unnest expands the array; we then filter for XX.XX pattern in Python.
        # Also include cycles that exist in cycle_config or cycle_freeze.
        await cur.execute(
            "SELECT DISTINCT unnest(tags) AS tag FROM roadmap_item WHERE is_deleted = FALSE"
        )
        all_tags = [r[0] for r in await cur.fetchall()]
        live_cycles = {t for t in all_tags if CYCLE_RE.match(t)}

        await cur.execute("SELECT cycle FROM cycle_freeze")
        frozen_cycles = {r[0] for r in await cur.fetchall()}

        await cur.execute("SELECT cycle FROM cycle_config")
        config_cycles = {r[0] for r in await cur.fetchall()}

        cycles = sorted(live_cycles | frozen_cycles | config_cycles, reverse=True)

        # Cycle state map for UI badges
        await cur.execute("SELECT cycle, state FROM cycle_config")
        cycle_states = {r[0]: r[1] for r in await cur.fetchall()}

    return {
        "departments": departments,
        "products": products,
        "cycles": cycles,
        "dept_products": dept_products,
        "cycle_states": cycle_states,
    }


async def _query_frozen_items_for_cycle(
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
        "SELECT f.jira_key, f.title, f.product_name AS product, f.department, "
        "       f.color_status, f.url, f.tags, "
        "       f.parent_key, f.parent_summary, f.rank, f.parent_rank "
        f"FROM cycle_freeze_item f{where} "
        "ORDER BY NULLIF(f.parent_rank, '') NULLS LAST, "
        "         NULLIF(f.rank, '') NULLS LAST, f.title"
    )

    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = await cur.fetchall()

    items = []
    for row in rows:
        item = dict(zip(columns, row, strict=False))
        cs = item.get("color_status")
        if isinstance(cs, str):
            item["color_status"] = json.loads(cs)
        items.append(item)
    return items


async def _query_roadmap_items(
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
    - **Carry-over** counts cycle labels chronologically before the displayed cycle.

    Returns:
        A tuple of (grouped_items, objective_urls, cycle_states_in_view).
        ``cycle_states_in_view`` maps cycle label → state (``"frozen"``/``"current"``/``"future"``/``None``).
    """
    frozen_map = get_frozen_cycles()  # {cycle: {frozen_at, frozen_by, note}}
    config_map = get_cycle_configs()  # {cycle: {state, updated_at, updated_by}}

    # Determine which cycles are in which state
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

    clauses.append("r.is_deleted = FALSE")
    where = f" WHERE {' AND '.join(clauses)}"
    query = (
        "SELECT r.id, r.jira_key, r.title, p.name AS product, p.department, "
        "       r.color_status, r.url, r.tags, "
        "       r.parent_key, r.parent_summary, r.rank, r.parent_rank, "
        "       r.assignee_name, r.priority, r.t_shirt_size "
        "FROM roadmap_item r "
        f"JOIN product p ON p.id = r.product_id{where} "
        "ORDER BY NULLIF(r.parent_rank, '') NULLS LAST, "
        "         NULLIF(r.rank, '') NULLS LAST, r.title"
    )

    async with get_async_conn() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = await cur.fetchall()

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

            # Carry-over = number of cycle labels chronologically before this cycle
            display_item = dict(item)
            item_cycle_labels = [t for t in tags if CYCLE_RE.match(t)]
            prior_count = sum(1 for lbl in item_cycle_labels if lbl < c)
            carry_over = {"color": "purple", "count": prior_count} if prior_count > 0 else None

            # Future cycle override: force health to white/Inactive but keep carry-over
            if c in future_cycle_labels:
                display_item["color_status"] = {
                    "health": {"color": "white"},
                    "carry_over": carry_over,
                }
            else:
                item_cs = dict(display_item.get("color_status") or {})
                item_cs["carry_over"] = carry_over
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
        frozen_items = await _query_frozen_items_for_cycle(fc, department=department, product=product)
        cycle_states_in_view[fc] = config_map[fc]["state"] if fc in config_map else "frozen"

        for item in frozen_items:
            # Carry-over = number of cycle labels chronologically before this frozen cycle
            item_tags = item.get("tags") or []
            item_cycle_labels = [t for t in item_tags if CYCLE_RE.match(t)]
            prior_count = sum(1 for lbl in item_cycle_labels if lbl < fc)
            item_cs = item.get("color_status") or {}
            if isinstance(item_cs, str):
                item_cs = json.loads(item_cs)
            item_cs = dict(item_cs)
            if prior_count > 0:
                item_cs["carry_over"] = {"color": "purple", "count": prior_count}
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

    # Sort: cycles newest-first, objectives by parent_rank ("No objective" last),
    # then epics within each objective by their own rank.
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
        grouped[c] = OrderedDict()
        for k in sorted_keys:
            grouped[c][k] = sorted(
                objectives[k],
                key=lambda item: (item.get("rank") or "\xff", item.get("title") or ""),
            )

    return grouped, objective_urls, cycle_states_in_view


@app.get("/", response_class=HTMLResponse)
async def roadmap_page(
    request: Request,
    department: str | None = Query(None),
    product: str | None = Query(None),
    cycle: str | None = Query(None),
):
    """Render the main roadmap page with server-side Jinja2 templates."""
    options = await _query_filter_options()

    # Normalise department — drop invalid values; track whether it was bad
    # so we can correct it from the product below.
    dept_was_invalid = False
    if department and department not in options["departments"]:
        department = None
        dept_was_invalid = True

    # When department was invalid (stale bookmark) or mismatches the product,
    # derive the correct department from the product.
    if product and department:
        if product not in options["dept_products"].get(department, []):
            for dept, prods in options["dept_products"].items():
                if product in prods:
                    department = dept
                    dept_was_invalid = True
                    break
    elif product and dept_was_invalid:
        for dept, prods in options["dept_products"].items():
            if product in prods:
                department = dept
                break

    # Redirect to the corrected URL so the browser address bar stays clean
    if dept_was_invalid:
        params = {}
        if department:
            params["department"] = department
        if product:
            params["product"] = product
        if cycle:
            params["cycle"] = cycle
        from urllib.parse import urlencode

        qs = urlencode(params)
        return RedirectResponse(url=f"/?{qs}" if qs else "/", status_code=302)

    # Derive available products from the dept→products mapping
    available_products = options["dept_products"].get(department, []) if department else options["products"]

    # Normalise product — drop empty/invalid
    selected_product = product if product and product in available_products else None

    # Default to the current cycle, or the latest available cycle as fallback
    default_cycle = None
    current = [c for c, s in options["cycle_states"].items() if s == "current"]
    if current:
        default_cycle = current[0]
    elif options["cycles"]:
        default_cycle = options["cycles"][0]
    if not cycle or cycle not in options["cycles"]:
        cycle = default_cycle

    # Skip querying if no product selected
    selected_product_id = None
    if not selected_product:
        grouped_items: OrderedDict = OrderedDict()
        objective_urls: dict[str, str] = {}
        cycle_states: dict[str, str] = {}
    else:
        grouped_items, objective_urls, cycle_states = await _query_roadmap_items(
            department=department,
            product=selected_product,
            cycle=cycle,
        )
        # Look up product id for planning link
        async with get_async_conn() as conn, conn.cursor() as cur:
            await cur.execute("SELECT id FROM product WHERE name = %s", (selected_product,))
            prow = await cur.fetchone()
            selected_product_id = prow[0] if prow else None

    return templates.TemplateResponse(
        request,
        "roadmap.html",
        {
            "cycles": options["cycles"],
            "dept_products": options["dept_products"],
            "cycle_states": options["cycle_states"],
            "selected_department": department or "",
            "selected_product": selected_product or "",
            "selected_product_id": selected_product_id,
            "product_department": next(
                (d for d, ps in options["dept_products"].items() if selected_product in ps),
                "",
            )
            if selected_product
            else "",
            "selected_cycle": cycle or "",
            "default_cycle": default_cycle or "",
            "grouped_items": grouped_items,
            "objective_urls": objective_urls,
        },
    )
