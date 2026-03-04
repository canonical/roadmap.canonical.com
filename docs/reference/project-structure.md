# Project structure

```
roadmap-web/
├── docker-compose.yaml           # PostgreSQL for dev (port 5432) + test (port 5433)
├── pyproject.toml                # Dependencies, tool config (ruff, pytest)
├── requirements.txt              # Pinned dependencies for production
├── .env.example                  # Environment variable reference
├── rockcraft.yaml                # OCI rock definition for production
│
├── src/                          # Application source code
│   ├── __init__.py
│   ├── app.py                    # FastAPI app, all HTTP endpoints, HTML page
│   ├── auth.py                   # OIDC authentication helpers (Authlib)
│   ├── settings.py               # pydantic-settings config (loads .env / env vars)
│   ├── database.py               # DB connection layer (async pool + sync helper)
│   ├── db_schema.sql             # Idempotent PostgreSQL DDL
│   ├── jira_sync.py              # Jira sync pipeline, product matching, freeze logic
│   ├── color_logic.py            # Epic health/colour derivation (standalone, testable)
│   └── scheduler.py              # Standalone periodic sync process
│
├── templates/                    # Jinja2 templates (server-rendered)
│   ├── base.html                 # Base layout: Vanilla Framework CSS + nav
│   └── roadmap.html              # Main roadmap page (filters, cycle sections, tables)
│
├── tests/                        # Test suite
│   ├── conftest.py               # Fixtures: test DB, client, cleanup
│   ├── test_api.py               # API endpoint + HTML page tests
│   ├── test_color_logic.py       # Color derivation unit tests
│   ├── test_jira_sync.py         # Sync pipeline integration tests
│   ├── test_snapshots.py         # Snapshot + diff tests
│   └── test_cycle_freeze.py      # Cycle lifecycle tests
│
├── charm/                        # Juju charm for Kubernetes deployment
│   ├── charmcraft.yaml
│   ├── src/charm.py
│   └── ...
│
├── docs/                         # This documentation
│   ├── index.md
│   ├── how-to/
│   ├── reference/
│   └── explanation/
│
├── constitution.md               # AI coding guidelines
├── memory.md                     # Architectural decision log
└── README.md                     # Project overview and quick start
```

## Module responsibilities

### `src/app.py`

The central module. Defines the FastAPI application, all HTTP endpoints (API and HTML), middleware stack, and the sync orchestration function. Contains:

- Lifespan handler (schema application, OIDC config, connection pool)
- OIDC auth middleware
- Sync trigger + status endpoints
- Product CRUD endpoints
- Cycle lifecycle endpoints
- Snapshot/diff endpoints
- Roadmap JSON endpoint
- Server-rendered HTML page (`GET /`) with query helpers

### `src/auth.py`

OIDC authentication using Authlib. Provides:

- `configure_oauth()` — register the OIDC provider at startup
- `is_authenticated()` — check if a request has a valid session
- `login_redirect()` — redirect to the IdP
- `handle_callback()` — exchange auth code for tokens

### `src/settings.py`

All configuration loaded via `pydantic-settings`. Supports `.env` files and dual-name env vars (plain + `APP_` prefix for charm injection).

### `src/database.py`

Database connection layer with two modes:

- **Async pool** (`get_async_conn`) — used by FastAPI route handlers for non-blocking DB access
- **Sync connection** (`get_db_connection`) — used by background tasks (Jira sync, schema setup, scheduler)

### `src/db_schema.sql`

Idempotent DDL — safe to run on every startup. Uses `CREATE TABLE IF NOT EXISTS` and `DO $$` blocks for migrations.

### `src/jira_sync.py`

The largest module. Contains:

- **Phase 1** — `sync_jira_data()`: fetch from Jira, store raw JSON
- **Phase 2** — `process_raw_jira_data()`: transform raw → roadmap items
- **Phase 3** — `take_daily_snapshot()`: daily snapshot for change tracking
- **Phase 4** — `freeze_cycle()` / `unfreeze_cycle()`: cycle freeze operations
- **Phase 5** — `register_cycle()` / `set_cycle_state()` / `remove_cycle()`: cycle config CRUD
- Product matching helpers: `_load_source_rules()`, `_match_issue_to_product()`

### `src/color_logic.py`

Standalone module for computing epic health colours. Exported function:

- `calculate_epic_color(issue_fields, frozen_cycles=None)` → `dict` with `health` and `carry_over`

Deliberately isolated from the rest of the codebase to be easily testable and reusable.

### `src/scheduler.py`

Standalone process that runs periodic Jira syncs in a loop. Designed to run as a separate Pebble service in production. Updates `sync_metadata` table for observability.
