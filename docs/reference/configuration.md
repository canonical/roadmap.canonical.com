# Configuration reference

All configuration is loaded from environment variables (or a `.env` file) using `pydantic-settings`. Each setting accepts both a plain name (for local `.env` usage) and an `APP_`-prefixed name (for Juju charm injection).

Settings are defined in `src/settings.py`.

## Jira settings

| Variable | Charm alias | Default | Description |
|----------|------------|---------|-------------|
| `JIRA_URL` | `APP_JIRA_URL` | `https://warthogs.atlassian.net` | Jira instance URL |
| `JIRA_USERNAME` | `APP_JIRA_USERNAME` | `""` | Jira username (email) for Basic Auth |
| `JIRA_PAT` | `APP_JIRA_PAT` | `""` | Jira Personal Access Token |
| `JQL_FILTER` | `APP_JQL_FILTER` | `issuetype = Epic AND "Properties[Checkboxes]" = "Roadmap Item"` | Static JQL filter appended to the dynamically built query |

> **Note:** The sync pipeline builds its JQL dynamically from registered products and active cycles: `project in (...) AND labels in (...) AND {JQL_FILTER}`. The `JQL_FILTER` setting provides the invariant part.

## Database settings

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRESQL_DB_CONNECT_STRING` | `postgresql://roadmap:roadmap@localhost:5432/roadmap` | PostgreSQL connection URL |

## OIDC / Authentication settings

| Variable | Charm alias | Default | Description |
|----------|------------|---------|-------------|
| `OIDC_CLIENT_ID` | `APP_OIDC_CLIENT_ID` | `""` (disabled) | OIDC client ID. When empty, authentication is disabled entirely. |
| `OIDC_CLIENT_SECRET` | `APP_OIDC_CLIENT_SECRET` | `""` | OIDC client secret |
| `OIDC_ISSUER` | `APP_OIDC_ISSUER` | `https://iam.green.canonical.com` | OIDC issuer URL (must serve OpenID Discovery) |
| `OIDC_REDIRECT_URI` | `APP_OIDC_REDIRECT_URI` | `http://localhost:8000/callback` | Callback URL registered with the IdP |
| `SESSION_SECRET` | `APP_SESSION_SECRET` | Random on startup | Secret for signing session cookies |

## Scheduler settings

| Variable | Charm alias | Default | Description |
|----------|------------|---------|-------------|
| `SYNC_INTERVAL_SECONDS` | `APP_SYNC_INTERVAL_SECONDS` | `3600` | Seconds between automatic Jira syncs. Set to `0` to disable the scheduler. |

## Dual-name convention

Every setting accepts two names:

1. **Plain name** (e.g. `JIRA_URL`) — used in `.env` files for local development
2. **`APP_` prefix** (e.g. `APP_JIRA_URL`) — injected by the Juju charm from user-defined config options

The `AliasChoices` mechanism in pydantic-settings resolves whichever is set (plain name takes precedence).

## `.env` file location

The `.env` file is resolved relative to the `src/` package directory (i.e. the project root). This means `uvicorn --app-dir` or running from any directory works correctly.
