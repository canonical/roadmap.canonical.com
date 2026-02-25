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
- Add pagination if item count grows large
