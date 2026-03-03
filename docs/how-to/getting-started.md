# Getting started

Set up a local development environment for roadmap-web.

## Prerequisites

- **Docker & Docker Compose** — for PostgreSQL databases
- **Python 3.11+** — the app targets 3.11 minimum
- **A Jira PAT** (Personal Access Token) with read access to your Jira project

## 1. Start the database

```bash
docker compose up -d db
```

This spins up PostgreSQL 16 on port **5432** (user: `roadmap`, password: `roadmap`, db: `roadmap`).

## 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your Jira credentials:

```bash
JIRA_URL=https://warthogs.atlassian.net
JIRA_USERNAME=your-email@canonical.com
JIRA_PAT=your-jira-personal-access-token
```

Leave `OIDC_CLIENT_ID` empty to disable authentication during local development.

> **Tip:** The `JQL_FILTER` default is `issuetype = Epic AND "Properties[Checkboxes]" = "Roadmap Item"`. Override it in `.env` if your Jira instance uses different fields.

## 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

This installs the app in editable mode along with `pytest` and `ruff` for development.

## 4. Run the application

```bash
uvicorn src.app:app --reload --port 8000
```

The database schema is applied automatically on startup. Open **http://localhost:8000** in your browser to see the roadmap page.

## 5. Seed initial data

Before the page shows anything useful you need to:

1. **Register at least one cycle** (see [Managing cycles](managing-cycles.md)):
   ```bash
   curl -X POST http://localhost:8000/api/v1/cycles/26.04 \
     -H 'Content-Type: application/json' \
     -d '{"state": "current"}'
   ```

2. **Create at least one product** with Jira source mappings (see [Managing products](managing-products.md)):
   ```bash
   curl -X POST http://localhost:8000/api/v1/products \
     -H 'Content-Type: application/json' \
     -d '{
       "name": "LXD",
       "department": "Containers",
       "jira_sources": [{"jira_project_key": "LXD"}]
     }'
   ```

3. **Trigger a Jira sync** (see [Triggering a Jira sync](triggering-sync.md)):
   ```bash
   curl -X POST http://localhost:8000/api/v1/sync
   ```

4. Refresh the browser — roadmap items should now be visible.

## 6. Verify with tests

```bash
docker compose up -d db-test
pytest -v
```

See [Running tests](running-tests.md) for details.
