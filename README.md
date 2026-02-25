# roadmap-web

A company-wide roadmap visualization tool. Data flows from Jira → PostgreSQL → FastAPI → server-rendered Vanilla Framework UI.

## Quick start

### Prerequisites

- Docker & Docker Compose
- Python 3.11+ (for local dev without Docker)
- A Jira PAT (Personal Access Token) with read access to your project

### 1. Start the database

```bash
docker compose up -d db
```

This spins up PostgreSQL 16 on port **5432** (user: `roadmap`, password: `roadmap`, db: `roadmap`).

### 2. Configure environment

```bash
cp backend/.env.example backend/.env
# Edit backend/.env with your Jira credentials:
#   JIRA_URL, JIRA_USERNAME, JIRA_PAT, JQL_QUERY
```

### 3. Install dependencies & run the API

```bash
cd backend
pip install -e ".[dev]"
uvicorn src.api:app --reload --port 8000
```

The app is now at **http://localhost:8000**. Schema is applied automatically on startup.

### 4. Open the roadmap page

Navigate to **http://localhost:8000** in your browser. The page shows filter dropdowns (department, product, cycle) and one table per product group.

### 5. Trigger a Jira sync

```bash
curl -X POST http://localhost:8000/api/v1/sync
```

Check progress:
```bash
curl http://localhost:8000/api/v1/status
```

Then refresh the page to see the synced items.

### 6. Seed products & departments

Products are mapped manually. Insert rows into the `product` table:

```sql
INSERT INTO product (name, department, primary_project)
VALUES
    ('Juju',  'Engineering', 'JUJU'),
    ('MAAS',  'Engineering', 'MAAS'),
    ('Snap',  'Tooling',     'SNAP')
ON CONFLICT (name) DO UPDATE SET
    department = EXCLUDED.department,
    primary_project = EXCLUDED.primary_project;
```

After seeding, re-sync or re-process to associate items with the correct products.

## Running tests

Tests use a separate PostgreSQL instance on port **5433** so they never touch your dev data.

```bash
# Start the test DB
docker compose up -d db-test

# Run tests
cd backend
pytest -v
```

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Server-rendered roadmap page (supports `?department=`, `?product=`, `?cycle=` filters) |
| `POST` | `/api/v1/sync` | Trigger a background Jira sync |
| `GET` | `/api/v1/status` | Current sync status |
| `GET` | `/api/v1/roadmap` | JSON list of roadmap items (supports `?product=`, `?status=`, `?release=` filters) |

## Project structure

```
roadmap-web/
├── docker-compose.yaml          # PostgreSQL for dev + test
├── backend/
│   ├── pyproject.toml            # Dependencies & tool config
│   ├── Dockerfile
│   ├── .env.example
│   ├── src/
│   │   ├── api.py                # FastAPI app, endpoints & HTML page
│   │   ├── settings.py           # Env var config via pydantic-settings
│   │   ├── database.py           # DB connection helper
│   │   ├── db_schema.sql         # Idempotent schema DDL
│   │   ├── jira_sync.py          # Two-phase Jira sync pipeline
│   │   └── color_logic.py        # Epic health/color derivation
│   ├── templates/
│   │   ├── base.html             # Base layout (Vanilla Framework + nav)
│   │   └── roadmap.html          # Main roadmap page template
│   └── tests/
│       ├── conftest.py           # Fixtures (test DB setup/teardown)
│       ├── test_api.py           # Endpoint + HTML page tests
│       ├── test_color_logic.py   # Color derivation unit tests
│       └── test_jira_sync.py     # Sync pipeline integration tests
├── constitution.md               # AI coding guidelines
├── memory.md                     # Architectural state across sessions
└── reference/                    # Canonical reference project (gitignored)
```

## Architecture decisions

- **FastAPI** over Flask for the API layer — async-ready, Pydantic validation built in, automatic OpenAPI docs at `/docs`.
- **Server-rendered Jinja2 templates** with Vanilla Framework CSS from CDN — same pattern as the reference Canonical project. No separate frontend build step needed.
- **Two-phase sync**: raw Jira JSON is stored first (`jira_issue_raw`), then processed into `roadmap_item`. This means we can re-process historical data without re-fetching from Jira, and the raw payload is always available for debugging.
- **Plain SQL** via psycopg2 instead of an ORM — the schema is small and stable; raw SQL is easier to reason about and deploy via `db_schema.sql`.
- **Separate test DB** on port 5433 so tests are completely isolated from dev data.
- **Manual product/department mapping** — admin seeds the `product` table with SQL; items are associated during sync processing based on Jira project key.
