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
cp .env.example .env
# Edit .env with your Jira credentials:
#   JIRA_URL, JIRA_USERNAME, JIRA_PAT, JQL_FILTER
```

### 3. Install dependencies & run the API

```bash
pip install -e ".[dev]"
uvicorn src.app:app --reload --port 8000
```

The app is now at **http://localhost:8000**. Schema is applied automatically on startup.

### 4. Open the roadmap page

Navigate to **http://localhost:8000** in your browser. The page shows filter dropdowns (department, product, cycle) and one table per product group.

## Authentication (OIDC)

The app uses OpenID Connect for authentication. When configured, unauthenticated users are automatically redirected to the identity provider — no login page or button is needed. Once the IdP authenticates the user (silently if they already have a corporate SSO session), they are redirected back and can use the app immediately.

Authentication is **disabled** when `OIDC_CLIENT_ID` is empty (the default), which is convenient for local development.

### Configuration

Add these to your `.env` file:

```bash
OIDC_CLIENT_ID=your-client-id
OIDC_CLIENT_SECRET=your-client-secret
OIDC_ISSUER=https://iam.green.canonical.com
OIDC_REDIRECT_URI=http://localhost:8000/callback
SESSION_SECRET=any-random-string
```

The session cookie (`roadmap_session`) is valid for 24 hours. After expiry the user is silently re-authenticated via the IdP.

### How it works

1. User visits `/` → no session → redirected to `/login`
2. `/login` redirects to the OIDC authorization endpoint (`OIDC_ISSUER`)
3. IdP authenticates the user (SSO) and redirects to `/callback`
4. `/callback` exchanges the authorization code for tokens, stores user info in a signed session cookie
5. User is redirected back to `/` — now authenticated

There is no logout flow — this is an internal-only tool and users stay authenticated via their corporate SSO session.

### 5. Trigger a Jira sync

```bash
curl -X POST http://localhost:8000/api/v1/sync
```

Check progress:
```bash
curl http://localhost:8000/api/v1/status
```

Then refresh the page to see the synced items.

### 6. Seed products & Jira mappings

Products and their Jira project associations are managed via the API.

**Create a product with Jira source mappings:**
```bash
curl -X POST http://localhost:8000/api/v1/products \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "LXD",
    "department": "Containers",
    "jira_sources": [
      {"jira_project_key": "LXD"},
      {"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]}
    ]
  }'
```

**List all products:**
```bash
curl http://localhost:8000/api/v1/products
```

**Get a single product by ID:**
```bash
curl http://localhost:8000/api/v1/products/1
```

**Update a product (replaces all fields and Jira sources):**
```bash
curl -X PUT http://localhost:8000/api/v1/products/1 \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "LXD",
    "department": "Containers",
    "jira_sources": [
      {"jira_project_key": "LXD", "exclude_components": ["CI"]},
      {"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]},
      {"jira_project_key": "SNAP", "include_labels": ["lxd-related"], "exclude_teams": ["QA"]}
    ]
  }'
```

**Delete a product:**
```bash
curl -X DELETE http://localhost:8000/api/v1/products/1
```

**Mapping syntax (per Jira source rule):**

| Field | Type | Description |
|-------|------|-------------|
| `jira_project_key` | string | Jira project key (e.g. `LXD`, `MAAS`) |
| `include_components` | string[] | Only include epics that have at least one of these components |
| `exclude_components` | string[] | Exclude epics that have any of these components |
| `include_labels` | string[] | Only include epics that have at least one of these labels |
| `exclude_labels` | string[] | Exclude epics that have any of these labels |
| `include_teams` | string[] | Only include epics owned by at least one of these teams |
| `exclude_teams` | string[] | Exclude epics owned by any of these teams |

All filters are optional (NULL = no filtering). When multiple filters are set on a single source rule, they are AND-ed together.

A single product can have multiple Jira sources. During sync, each issue is matched against the rules (first match wins). Issues that don't match any rule land in the `Uncategorized` product.

**Examples matching the old spreadsheet syntax:**

| Old syntax | API equivalent |
|------------|---------------|
| `LXD` | `{"jira_project_key": "LXD"}` |
| `OPENG;TAP;SMS;OBC` | 4 separate sources, one per key |
| `LXD;WD["Anbox/LXD Tribe"]` | `[{"jira_project_key": "LXD"}, {"jira_project_key": "WD", "include_components": ["Anbox/LXD Tribe"]}]` |
| `PALS["Scriptlets","Starform"]` | `{"jira_project_key": "PALS", "include_components": ["Scriptlets", "Starform"]}` |
| `FR["!Toolchains"]` | `{"jira_project_key": "FR", "exclude_components": ["Toolchains"]}` |

After seeding products, re-sync (`POST /api/v1/sync`) to re-process issues with the new mappings.

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
| `GET` | `/` | Server-rendered roadmap page (requires auth; supports `?department=`, `?product=`, `?cycle=` filters) |
| `GET` | `/login` | Redirects to OIDC provider (automatic, not user-facing) |
| `GET` | `/callback` | OIDC callback (exchanges auth code for session) |
| `POST` | `/api/v1/sync` | Trigger a background Jira sync |
| `GET` | `/api/v1/status` | Current sync status |
| `GET` | `/api/v1/roadmap` | JSON list of roadmap items (supports `?product=`, `?status=`, `?release=` filters) |
| `GET` | `/api/v1/products` | List all products with their Jira source mappings |
| `GET` | `/api/v1/products/{id}` | Get a single product |
| `POST` | `/api/v1/products` | Create a product with Jira source mappings |
| `PUT` | `/api/v1/products/{id}` | Replace a product's details and Jira sources |
| `DELETE` | `/api/v1/products/{id}` | Delete a product (unlinks roadmap items) |
| `GET` | `/api/v1/snapshots` | List all available snapshot dates with item counts |
| `GET` | `/api/v1/snapshots/diff` | Compare two snapshots (`?from_date=&to_date=`, YYYY-MM-DD) |
| `GET` | `/api/v1/cycles` | List all cycles with state (frozen/current/future) |
| `POST` | `/api/v1/cycles/{cycle}` | Register a new cycle with initial state |
| `PUT` | `/api/v1/cycles/{cycle}` | Change a cycle's state (with freeze/unfreeze side effects) |
| `DELETE` | `/api/v1/cycles/{cycle}` | Remove a cycle from the registry |
| `GET` | `/api/v1/cycles/{cycle}/items` | Get frozen items for a specific cycle |

## Cycle lifecycle management

Work is planned in 6-month cycles (e.g. `25.10`, `26.04`). Each cycle has an explicit lifecycle state managed via the `cycle_config` table:

| State | Meaning | Carry-over? | Data source |
|-------|---------|-------------|-------------|
| **future** | Planned but not yet started. All items shown as **Inactive** (white). | No | Live Jira (colors overridden) |
| **current** | The active cycle. Items show live Jira health colors. | Yes (counts frozen cycles) | Live Jira |
| **frozen** | Closed cycle. Data is immutable — a snapshot taken at freeze time. | Counted by other cycles | `cycle_freeze_item` snapshot |

### State machine

```
  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  future  │ ──▶ │ current  │ ──▶ │  frozen  │
  └──────────┘     └──────────┘     └──────────┘
       │                │                │
       └────────────────┴────────────────┘
         (any transition is allowed)
```

**Constraints:**
- At most **one** cycle can be `current` at any time (zero is OK during transitions).
- Transitioning **to** `frozen` creates a `cycle_freeze` snapshot automatically.
- Transitioning **away from** `frozen` deletes the snapshot.

### Typical lifecycle

```
# Upcoming cycle — all items show as Inactive
curl -X POST localhost:8000/api/v1/cycles/27.04 -H 'Content-Type: application/json' -d '{"state": "future"}'

# Cycle starts — items show live Jira colors
curl -X PUT localhost:8000/api/v1/cycles/27.04 -H 'Content-Type: application/json' -d '{"state": "current"}'

# Cycle ends — snapshot taken, data becomes immutable
curl -X PUT localhost:8000/api/v1/cycles/27.04 -H 'Content-Type: application/json' -d '{"state": "frozen"}'
```

### Carry-over logic

An item appearing in multiple cycles (e.g. `25.10`, `26.04`) shows a purple carry-over badge. The carry-over count equals the number of **frozen** cycle labels on that item. This means:
- Items in the current cycle that also appeared in a frozen past cycle show carry-over.
- Items spanning two future cycles show no carry-over (neither cycle is frozen).
- Items in a frozen cycle show carry-over for *other* frozen cycles they belong to.

## Daily snapshots & change reports

After each Jira sync, the backend automatically takes a **daily snapshot** of all roadmap items. If a snapshot for today already exists (e.g. from an earlier hourly sync), the step is skipped — so you get exactly **one snapshot per day**, regardless of sync frequency.

### How it works

1. The `roadmap_snapshot` table stores a full copy of every `roadmap_item` row, tagged with `snapshot_date`.
2. Product name and department are **denormalized** into the snapshot so reports remain accurate even if products are renamed/deleted later.
3. The health color is extracted from `color_status->'health'->>'color'` into a plain `VARCHAR` column for easy querying.

### Querying changes

**List available snapshots:**
```bash
curl http://localhost:8000/api/v1/snapshots
```

**Compare two dates (biweekly report):**
```bash
curl "http://localhost:8000/api/v1/snapshots/diff?from_date=2026-02-01&to_date=2026-02-15"
```

The diff response contains four lists:

| Field | Description |
|-------|-------------|
| `turned_red` | Items whose color changed **to** red (subset of `color_changes`) |
| `color_changes` | All items whose color changed between the two dates |
| `disappeared` | Items present on `from_date` but **missing** on `to_date` |
| `appeared` | Items present on `to_date` but **not** on `from_date` |

### Storage estimate

With 2,500 roadmap items and one snapshot per day: ~912K rows/year (~a few MB). PostgreSQL handles this trivially.

## Project structure

```
roadmap-web/
├── docker-compose.yaml          # PostgreSQL for dev + test
├── pyproject.toml               # Dependencies & tool config
├── .env.example                 # Environment variable reference
├── src/
│   ├── app.py                   # FastAPI app, endpoints & HTML page
│   ├── auth.py                  # OIDC authentication (Authlib)
│   ├── settings.py              # Env var config via pydantic-settings
│   ├── database.py              # DB connection helper
│   ├── db_schema.sql            # Idempotent schema DDL
│   ├── jira_sync.py             # Two-phase Jira sync pipeline
│   └── color_logic.py           # Epic health/color derivation
├── templates/
│   ├── base.html                # Base layout (Vanilla Framework + nav)
│   └── roadmap.html             # Main roadmap page template
├── tests/
│   ├── conftest.py              # Fixtures (test DB setup/teardown)
│   ├── test_api.py              # Endpoint + HTML page tests
│   ├── test_color_logic.py      # Color derivation unit tests
│   ├── test_jira_sync.py        # Sync pipeline integration tests
│   └── test_snapshots.py        # Snapshot + diff tests
├── charm/                       # Juju charm for deployment
├── constitution.md              # AI coding guidelines
└── memory.md                    # Architectural state across sessions
```

## Architecture decisions

- **FastAPI** over Flask for the API layer — async-ready, Pydantic validation built in, automatic OpenAPI docs at `/docs`.
- **Server-rendered Jinja2 templates** with Vanilla Framework CSS from CDN — same pattern as the reference Canonical project. No separate frontend build step needed.
- **Two-phase sync**: raw Jira JSON is stored first (`jira_issue_raw`), then processed into `roadmap_item`. This means we can re-process historical data without re-fetching from Jira, and the raw payload is always available for debugging.
- **Plain SQL** via psycopg2 instead of an ORM — the schema is small and stable; raw SQL is easier to reason about and deploy via `db_schema.sql`.
- **Separate test DB** on port 5433 so tests are completely isolated from dev data.
- **Manual product/department mapping** — admin seeds the `product` table with SQL; items are associated during sync processing based on Jira project key.
