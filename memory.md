# Memory — roadmap-web

## 2026-02-25 — Foundation: Backend + DB + Jira sync

### What was built
- FastAPI backend (`backend/src/`) with three endpoints: sync, status, roadmap
- PostgreSQL schema (`db_schema.sql`) with `jira_issue_raw`, `roadmap_item`, `product` tables
- Two-phase Jira sync pipeline: raw fetch → process into roadmap items
- Color/health logic extracted into standalone module (`color_logic.py`)
- Docker Compose with separate dev DB (port 5432) and test DB (port 5433)
- Test suite covering API endpoints, color logic, and sync pipeline

### Key decisions
1. **FastAPI over Flask** — the `backend-example` already used FastAPI; it's the better choice for a pure API service (auto OpenAPI docs, Pydantic, async-ready). Flask remains an option for the future SSR/template layer if needed.
2. **Plain psycopg2 over SQLAlchemy** — schema is small and stable; raw SQL is simpler for upserts and DDL management. Reconsidering if model count grows past ~5 tables.
3. **pydantic-settings for config** — replaces manual `os.environ` reads; validates on startup, supports `.env` files.
4. **Two-phase sync** — store raw Jira JSON first, then process. Allows re-processing without re-fetching and keeps raw data for debugging.
5. **Separate test DB** — port 5433 via docker-compose `db-test` service; tests never touch dev data.
6. **API versioned under `/api/v1/`** — allows future breaking changes without disrupting clients.
7. **ruff for linting** — replaces flake8 + black; single tool, faster.

### Files created
- `docker-compose.yaml`
- `README.md`
- `backend/pyproject.toml`
- `backend/Dockerfile`
- `backend/.env.example`
- `backend/src/{__init__, api, settings, database, db_schema.sql, jira_sync, color_logic}.py`
- `backend/tests/{__init__, conftest, test_api, test_color_logic, test_jira_sync}.py`

### Built from
- `backend-example/` — original prototype; iterated and improved upon
- `reference/` — mirrored patterns: env var config, DB init on startup, background sync, psycopg2

### Known issues / tech debt
- No auth on API endpoints yet (SSO integration is future work)
- Sync status is in-memory — lost on restart; acceptable for now
- `product` table seeding is manual (only "Uncategorized" auto-seeded)
- Custom field ID for `roadmap_state` (`customfield_10968`) is hardcoded — needs per-instance config
- No rate limiting or pagination on `/api/v1/roadmap` yet

---

## 2026-02-25 — Iteration: lint cleanup, lifespan migration, verified green

### What changed
- Replaced deprecated `@app.on_event("startup")` with FastAPI `lifespan` async context manager — zero deprecation warnings now.
- Ran `ruff check --fix --unsafe-fixes` to auto-fix 11 lint issues: import sorting, `datetime.UTC` alias, combined `with` statements, `zip(..., strict=)`.
- Confirmed: **20/20 tests pass, 0 warnings, 0 lint errors**.

### Dev environment
- Python 3.12.3 via `python3 -m venv venv` in `backend/`
- PostgreSQL 16-alpine via `docker compose up -d db db-test`
- `pip install -e ".[dev]"` for editable install + pytest + ruff

### Next steps
- Add `.env` with real Jira credentials and test a live sync
- Add pagination to `/api/v1/roadmap`
- Start the React frontend scaffold

---

## 2026-02-26 — Bug fix: carry-over logic + frontend (Jinja2 + Vanilla Framework)

### Bug fixed
- **carry_over counted ALL labels** instead of only cycle labels matching `^\d{2}\.\d{2}$` (e.g. `24.04`, `25.10`).
  Labels like `ComponentPlatform`, `Major`, `SSDLC` were inflating the count.
- Fixed in `color_logic.py`: added `CYCLE_RE = re.compile(r"^\d{2}\.\d{2}$")` regex filter.
- Added 2 new tests: `test_carry_over_ignores_non_cycle_labels`, `test_carry_over_with_mixed_labels`.

### Frontend built
- **Server-rendered Jinja2 templates** served directly from FastAPI (same pattern as `reference/` project uses Flask + Jinja2).
- Vanilla Framework CSS loaded from CDN (`assets.ubuntu.com`).
- Single page at `GET /` with:
  - Department, Product, and Cycle filter dropdowns (auto-submit on change).
  - Color legend bar.
  - One `<table>` per product group.
  - Columns: carry-over badge, health color cell, summary, Jira key (hyperlink), status, release.
- 3 new HTML page tests added to `test_api.py`.

### Schema change
- Added `department` column to `product` table (`VARCHAR(128) NOT NULL DEFAULT 'Unassigned'`).
- Idempotent `ALTER TABLE` migration added to `db_schema.sql` for existing databases.

### Dependencies
- Added `jinja2>=3.1,<4` to `pyproject.toml`.
- Fixed Starlette `TemplateResponse` deprecation: `TemplateResponse(request, name, context)` instead of `TemplateResponse(name, {"request": request, ...})`.

### Files created
- `backend/templates/base.html` — base layout with Vanilla Framework nav + CSS
- `backend/templates/roadmap.html` — main roadmap page template

### Files modified
- `backend/src/color_logic.py` — carry_over regex fix
- `backend/src/api.py` — added Jinja2Templates, `GET /` endpoint, helper queries
- `backend/src/db_schema.sql` — department column + migration
- `backend/pyproject.toml` — jinja2 dependency
- `backend/tests/test_api.py` — 3 new HTML page tests
- `backend/tests/test_color_logic.py` — 2 new carry_over tests

### Test status
- **25/25 tests pass, 0 warnings, 0 lint errors**

### Next steps
- Seed `product` table with real products and departments
- Re-sync Jira data to populate the page
- Add admin guide for product/department seeding
- Consider adding status/cycle column to tables

---

## 2026-02-25 — Iteration: cycle-based vertical grouping, products horizontal

### What changed
- **Layout restructured**: Cycle rows (vertical, newest-first) × Product columns (horizontal, side-by-side).
  Each cycle is an `<h2>` section; products sit in a flex grid below it.
- **Cycle source**: Cycles now come from `tags` (Jira labels matching `XX.XX`), not from `release`/`fixVersions`.
  An item with labels `['25.10', '26.04']` appears in both cycle buckets.
- **Removed columns**: Status and Release columns dropped from tables.
  Remaining columns: Carry-over, Health, Summary, Jira key.
- **Hidden items**: Items with no `XX.XX` cycle label are excluded from the page entirely.
- **Cycle filter**: When a cycle is selected, only that cycle's section appears.

### Files modified
- `backend/src/api.py` — rewrote `_query_filter_options` (cycles from `unnest(tags)`),
  `_query_roadmap_items` (returns `OrderedDict[cycle, OrderedDict[product, list[item]]]`),
  added `import re` and `CYCLE_RE`
- `backend/templates/roadmap.html` — full rewrite: nested loop cycle→product, removed Status/Release columns
- `backend/templates/base.html` — added `.cycle-section`, `.product-grid`, `.product-column` CSS
- `backend/tests/test_api.py` — replaced 3 old HTML tests with 6 new ones:
  `test_roadmap_page_empty`, `test_roadmap_page_with_data`, `test_roadmap_page_hides_items_without_cycle`,
  `test_roadmap_page_item_in_multiple_cycles`, `test_roadmap_page_filter_by_cycle`,
  `test_roadmap_page_filter_by_product`

### Test status
- **28/28 tests pass, 0 warnings, 0 lint errors**

### Next steps
- Seed products & departments via API, re-sync real Jira data
- Possibly add collapsible cycle sections for long pages
- Add pagination if item count grows large

---

## 2026-02-25 — Product-Jira mapping API (replaces direct DB seeding)

### What changed
- **Schema redesign**: `product` table now uses `id SERIAL PRIMARY KEY` instead of `name` as PK.
  Old columns `primary_project`, `secondary_projects`, `component_filter` removed.
- **New table `product_jira_source`**: normalized mapping of product → Jira project keys with
  filter columns: `include_components`, `exclude_components`, `include_labels`, `teams`.
  One product can have many Jira sources (e.g. `LXD` + `WD["Anbox/LXD Tribe"]`).
- **`roadmap_item.product`** column renamed to **`roadmap_item.product_id`** (FK to `product.id`).
- **CRUD API** at `/api/v1/products` — full Create/Read/Update/Delete with nested `jira_sources`.
  Replaces manual SQL seeding.
- **Sync pipeline rewritten**: `_match_issue_to_product()` evaluates source rules with
  include/exclude component and label filters. First matching rule wins; unmatched issues
  land in `Uncategorized`.
- **All existing queries updated** to JOIN on `product_id` instead of `product` name.

### New API endpoints
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/products` | List all products with Jira sources |
| `GET` | `/api/v1/products/{id}` | Get single product |
| `POST` | `/api/v1/products` | Create product + Jira sources |
| `PUT` | `/api/v1/products/{id}` | Replace product + sources |
| `DELETE` | `/api/v1/products/{id}` | Delete product (unlinks items) |

### Files modified
- `backend/src/db_schema.sql` — new schema: `product` (id PK), `product_jira_source`, `roadmap_item.product_id`
- `backend/src/api.py` — added Pydantic models, CRUD endpoints, updated all queries
- `backend/src/jira_sync.py` — `JiraSourceRule` dataclass, `_match_issue_to_product()`, updated Phase 2
- `backend/tests/test_api.py` — 7 new product CRUD tests, all existing tests adapted to new schema
- `backend/tests/test_jira_sync.py` — 8 new tests: 2 integration (source rules, fallback), 6 unit tests for matching logic
- `backend/tests/conftest.py` — updated DROP to include `product_jira_source`
- `README.md` — updated seeding docs, API reference table, mapping syntax examples
- `memory.md` — this entry

### Test status
- **44/44 tests pass, 0 warnings**

### Key decisions
1. **Normalized `product_jira_source` over arrays** — cleaner to query, easier to CRUD via API,
   supports per-source filters without parsing syntax strings.
2. **`id` PK on product** instead of `name` — allows product renames without cascading FK updates.
3. **PUT replaces all sources** (delete + re-insert) — simpler than PATCH for small cardinality.
   Individual source management can be added later if needed.
4. **First-match-wins** rule ordering — matches the old spreadsheet convention where the first
   matching project/filter takes precedence.
5. **DELETE unlinks items** (sets `product_id = NULL`) rather than cascade-deleting roadmap items.

### Next steps
- Seed real products via API
- After re-sync, re-process to populate product assignments
- Consider admin UI for product management (currently API-only)

---

## 2026-02-25 — Daily snapshots for biweekly change reports

### What was built
- **`roadmap_snapshot` table** — stores a full copy of every `roadmap_item` once per day,
  with denormalized product name/department and extracted health color.
- **`take_daily_snapshot()`** in `jira_sync.py` — called automatically after each sync.
  Idempotent: if today's snapshot already exists, it's a no-op (safe for hourly syncs).
- **`GET /api/v1/snapshots`** — lists all available snapshot dates with item counts.
- **`GET /api/v1/snapshots/diff?from_date=&to_date=`** — compares two snapshots and returns:
  - `turned_red` — items whose color changed to red
  - `color_changes` — all color changes
  - `disappeared` — items removed from the roadmap
  - `appeared` — new items added to the roadmap
- **12 new tests** in `test_snapshots.py` covering snapshot creation, idempotency,
  product info capture, and all four diff categories.

### Key decisions
1. **Daily snapshots over change events** — simpler to implement and query.
   With 2,500 items and 1 snapshot/day, that's ~912K rows/year (~a few MB). Trivial for PostgreSQL.
2. **Idempotent per day** — only one snapshot per calendar day, regardless of how many
   syncs run. Prevents bloat from hourly syncs.
3. **Denormalized product info** — product name and department are copied into the snapshot
   so reports remain accurate even if products are renamed or deleted later.
4. **Health color extracted to plain column** — `color_status->'health'->>'color'` is stored
   as `VARCHAR(32)` for easy SQL comparisons in diff queries.
5. **No FK to product** — snapshot rows are self-contained historical records.

### Files created
- `backend/tests/test_snapshots.py` — 12 tests for snapshot + diff logic

### Files modified
- `backend/src/db_schema.sql` — added `roadmap_snapshot` table + indexes
- `backend/src/jira_sync.py` — added `take_daily_snapshot()` function
- `backend/src/api.py` — wired snapshot into sync, added `/api/v1/snapshots` and `/api/v1/snapshots/diff`
- `backend/tests/conftest.py` — added `roadmap_snapshot` to teardown DROP
- `README.md` — documented snapshot architecture, API endpoints, query examples
- `memory.md` — this entry

### Test status
- **67/67 tests pass, 0 new lint errors**

---

## 2026-02-26 — OIDC authentication (transparent SSO for internal users)

### What was built
- **OIDC integration** using [Authlib](https://docs.authlib.org/en/latest/) with Starlette's
  `SessionMiddleware` for signed cookie-based sessions.
- **New module `src/auth.py`** — configures the Authlib OAuth registry, provides
  `is_authenticated()`, `login_redirect()`, and `handle_callback()` helpers.
- **Three new routes**: `/login` (redirects to IdP), `/callback` (exchanges auth code),
  and `GET /` now requires authentication when OIDC is configured.
- **Transparent SSO flow**: unauthenticated users hitting `/` are automatically redirected
  through the IdP — no login page, no login button. If the user already has a corporate
  SSO session the authentication is completely silent.
- **No logout**: this is an internal-only tool; users stay authenticated via their
  corporate SSO session. The session cookie expires after 24 hours, after which the
  next visit silently re-authenticates.
- **Graceful disable**: when `OIDC_CLIENT_ID` is empty (default), authentication is
  completely disabled — convenient for local development.

### OIDC provider details
- Issuer: `https://iam.green.canonical.com` (Hydra-based)
- Discovery: `/.well-known/openid-configuration`
- Grant type: `authorization_code` + `refresh_token`
- Scopes requested: `openid email profile`

### New settings (in `settings.py`)
| Setting | Env var(s) | Default |
|---------|-----------|---------|
| `oidc_client_id` | `OIDC_CLIENT_ID` / `APP_OIDC_CLIENT_ID` | `""` (disabled) |
| `oidc_client_secret` | `OIDC_CLIENT_SECRET` / `APP_OIDC_CLIENT_SECRET` | `""` |
| `oidc_issuer` | `OIDC_ISSUER` / `APP_OIDC_ISSUER` | `https://iam.green.canonical.com` |
| `oidc_redirect_uri` | `OIDC_REDIRECT_URI` / `APP_OIDC_REDIRECT_URI` | `http://localhost:8000/callback` |
| `session_secret` | `SESSION_SECRET` / `APP_SESSION_SECRET` | random on startup |

### Key decisions
1. **Authlib over python-jose / raw OIDC** — handles discovery, JWKS rotation, token exchange,
   and Starlette integration out of the box.
2. **Session in signed cookie** (via `SessionMiddleware`) — no server-side session store needed;
   `itsdangerous` (transitive dep of Starlette) signs the cookie.
3. **No explicit `itsdangerous` dependency** — it's pulled in transitively by Starlette;
   no need to pin it in `pyproject.toml`.
4. **No logout route** — internal-only app; users rely on corporate SSO session lifecycle.
5. **API endpoints (`/api/v1/*`) not gated** — they are machine-to-machine; auth can be
   added later if needed (e.g. via API keys or the same OIDC tokens).
6. **Auth disabled by default** — empty `OIDC_CLIENT_ID` means no redirects, no middleware
   interference. Zero friction for local dev.

### Files created
- `src/auth.py` — OIDC helpers (configure, authenticate, callback)

### Files modified
- `src/settings.py` — added OIDC + session settings
- `src/app.py` — added `SessionMiddleware`, OIDC startup config, `/login` + `/callback` routes,
  auth guard on `GET /`
- `templates/base.html` — cleaned up (no user display, no logout link)
- `pyproject.toml` — added `authlib>=1.3,<2`
- `requirements.txt` — added `authlib==1.4.1`
- `.env.example` — documented OIDC env vars
- `README.md` — added Authentication section, updated API reference + project structure

### Dependencies added
- `authlib>=1.3,<2` (explicit)
- `itsdangerous` (transitive via Starlette — not pinned)
