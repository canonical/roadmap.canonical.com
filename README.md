# roadmap-web

A company-wide roadmap visualization tool. Data flows from Jira → PostgreSQL → FastAPI → server-rendered Vanilla Framework UI.

## Quick start

```bash
# 1. Start PostgreSQL
docker compose up -d db

# 2. Configure environment
cp .env.example .env
# Edit .env with your Jira credentials (JIRA_URL, JIRA_USERNAME, JIRA_PAT)

# 3. Install & run
pip install -e ".[dev]"
uvicorn src.app:app --reload --port 8000
```

Open **http://localhost:8000**. Schema is applied automatically on startup.

To populate the page, register a cycle, create products with Jira mappings, and trigger a sync — see the [Getting started guide](docs/how-to/getting-started.md).

## Documentation

Full documentation lives in the [`docs/`](docs/index.md) folder, organised by the [Diátaxis](https://diataxis.fr/) framework:

### How-to guides (for administrators)

- [Getting started](docs/how-to/getting-started.md) — set up a local dev environment
- [Managing products](docs/how-to/managing-products.md) — CRUD products with Jira mappings
- [Managing cycles](docs/how-to/managing-cycles.md) — register, transition, freeze/unfreeze cycles
- [Triggering a Jira sync](docs/how-to/triggering-sync.md) — manual and automatic sync
- [Generating change reports](docs/how-to/change-reports.md) — compare snapshots
- [Configuring authentication](docs/how-to/configuring-authentication.md) — OIDC/SSO setup
- [Running tests](docs/how-to/running-tests.md) — test suite and linting

### Reference (for developers)

- [API reference](docs/reference/api.md) — all HTTP endpoints with request/response schemas
- [Database schema](docs/reference/database-schema.md) — tables, columns, indexes, constraints
- [Configuration](docs/reference/configuration.md) — all environment variables
- [Project structure](docs/reference/project-structure.md) — file layout and module responsibilities

### Explanation (background and rationale)

- [Architecture overview](docs/explanation/architecture.md) — data flow, tech choices
- [Jira sync pipeline](docs/explanation/jira-sync-pipeline.md) — two-phase design
- [Colour and health logic](docs/explanation/color-health-logic.md) — how epic colours are derived
- [Cycle lifecycle](docs/explanation/cycle-lifecycle.md) — frozen/current/future state machine
- [Snapshots and change tracking](docs/explanation/snapshots.md) — daily snapshots and diffs
- [Product-Jira mapping](docs/explanation/product-jira-mapping.md) — issue-to-product matching
- [Authentication flow](docs/explanation/authentication.md) — OIDC transparent SSO

## Running tests

```bash
docker compose up -d db-test
pytest -v
```

## Project structure

```
roadmap-web/
├── src/                  # Application source code
├── templates/            # Jinja2 templates (Vanilla Framework)
├── tests/                # Test suite
├── docs/                 # Documentation (Diátaxis)
├── charm/                # Juju charm for Kubernetes deployment
├── docker-compose.yaml   # PostgreSQL for dev + test
├── pyproject.toml        # Dependencies & tool config
└── .env.example          # Environment variable reference
```

See [Project structure reference](docs/reference/project-structure.md) for full details.
