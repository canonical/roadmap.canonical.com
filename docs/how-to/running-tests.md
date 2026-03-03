# Running tests

Tests use a separate PostgreSQL instance so they never touch your development data.

## Start the test database

```bash
docker compose up -d db-test
```

This starts PostgreSQL on port **5433** (user: `roadmap`, password: `roadmap`, db: `roadmap_test`).

## Run the full test suite

```bash
pytest -v
```

The test configuration in `conftest.py` automatically:

- Overrides `DATABASE_URL` to point at the test database (port 5433)
- Disables OIDC authentication
- Drops and recreates all tables between test runs for a clean state

## Run specific test files

```bash
pytest tests/test_api.py -v          # API endpoint + HTML page tests
pytest tests/test_color_logic.py -v  # Color derivation unit tests
pytest tests/test_jira_sync.py -v    # Sync pipeline integration tests
pytest tests/test_snapshots.py -v    # Snapshot + diff tests
pytest tests/test_cycle_freeze.py -v # Cycle lifecycle tests
```

## Run a specific test

```bash
pytest tests/test_api.py::test_roadmap_page_with_data -v
```

## Linting

The project uses **ruff** for linting and formatting:

```bash
ruff check .        # Check for lint errors
ruff check --fix .  # Auto-fix what can be fixed
ruff format .       # Format code
```

Ruff is configured in `pyproject.toml` with a line length of 120 and rules for imports, Python upgrades, bugbear, and simplification.

## Test structure

| File | Coverage |
|------|----------|
| `tests/conftest.py` | Shared fixtures: test DB setup/teardown, test client |
| `tests/test_api.py` | API endpoints, product CRUD, HTML page rendering |
| `tests/test_color_logic.py` | `calculate_epic_color` unit tests |
| `tests/test_jira_sync.py` | Sync pipeline: source rules, product matching, Phase 2 processing |
| `tests/test_snapshots.py` | Daily snapshot creation, idempotency, diff queries |
| `tests/test_cycle_freeze.py` | Cycle registration, state transitions, freeze/unfreeze, carry-over |
