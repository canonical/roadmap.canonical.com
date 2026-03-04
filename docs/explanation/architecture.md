# Architecture overview

roadmap-web is a company-wide roadmap visualisation tool. It pulls epic-level data from Jira, stores it in PostgreSQL, and serves a server-rendered HTML page via FastAPI.

## High-level data flow

```
┌──────────┐     ┌───────────────┐     ┌──────────────┐     ┌──────────────────┐
│   Jira   │────▶│  Sync Worker  │────▶│  PostgreSQL   │────▶│  FastAPI + Jinja │
│  (source)│     │  (background) │     │   (DB)        │     │  (web + API)     │
└──────────┘     └───────────────┘     └──────────────┘     └──────────────────┘
```

1. **Jira** is the source of truth for all roadmap items (epics).
2. The **sync worker** fetches issues via JQL and stores raw JSON, then processes it into structured roadmap items.
3. **PostgreSQL** holds raw data, processed items, products, cycle config, snapshots, and frozen cycle data.
4. **FastAPI** serves both a JSON API and a server-rendered HTML page using Jinja2 templates styled with Vanilla Framework CSS.

## Technology choices

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Web framework** | FastAPI | Async-ready, Pydantic validation built in, auto OpenAPI docs at `/docs` |
| **Templates** | Jinja2 + Vanilla Framework CSS (CDN) | Server-rendered — no separate frontend build step. Matches the Canonical reference project pattern. |
| **Database** | PostgreSQL 16 + psycopg3 | Supports JSONB for raw Jira storage, arrays for tags/labels, robust and well-understood |
| **ORM** | None (plain SQL) | Schema is small (~8 tables) and stable. Raw SQL is simpler for upserts and DDL management. |
| **Jira client** | `jira` Python library | Handles pagination, auth, and REST API details |
| **Auth** | Authlib (OIDC) + Starlette SessionMiddleware | Handles discovery, JWKS rotation, token exchange. Session in signed cookies — no server-side store needed. |
| **Config** | pydantic-settings | Validates on startup, supports `.env` files, dual-name env vars for charm injection |
| **Linting** | ruff | Replaces flake8 + black in a single tool |
| **Packaging** | Rockcraft (OCI) + Charmcraft (Juju) | Canonical standard for Kubernetes deployment |

## Why FastAPI over Flask

The project initially considered Flask (matching the Canonical reference project), but FastAPI was chosen because:

- The app is primarily an API service — FastAPI's automatic OpenAPI docs, Pydantic request/response models, and dependency injection are a better fit.
- Async support is built in, which matters for the connection pool and concurrent requests.
- Jinja2 templates work identically in FastAPI (via `fastapi.templating`).

## Why plain SQL over SQLAlchemy

The schema has ~8 tables with well-defined relationships. The query patterns are mostly upserts, JOINs, and aggregations. Plain SQL with psycopg3 is:

- Easier to reason about (the SQL in the code matches what runs in the database)
- Simpler for the idempotent schema DDL (`CREATE TABLE IF NOT EXISTS`, `DO $$` blocks)
- Avoids the ORM abstraction tax for a small, stable schema

If the schema grows past ~15 tables, SQLAlchemy may become worthwhile.

## Connection model

The app uses **two connection modes**:

- **Async pool** (`psycopg_pool.AsyncConnectionPool`) — used by FastAPI route handlers. Non-blocking, supports concurrent requests. Pool size: 2–10 connections.
- **Sync connections** (`psycopg.connect`) — used by background tasks (Jira sync, schema setup, scheduler). Simpler and appropriate for sequential batch processing.

## Deployment architecture

```
┌─────────────────────────────────────┐
│          Kubernetes (Juju)          │
│                                     │
│  ┌──────────────┐  ┌────────────┐  │
│  │  roadmap-web  │  │ scheduler  │  │
│  │  (FastAPI)    │  │ (cron)     │  │
│  │  port 8000    │  │ unit 0 only│  │
│  └──────┬───────┘  └─────┬──────┘  │
│         │                 │         │
│  ┌──────▼─────────────────▼──────┐  │
│  │        PostgreSQL             │  │
│  │   (Juju postgresql charm)     │  │
│  └───────────────────────────────┘  │
│                                     │
│  ┌───────────────────────────────┐  │
│  │   Hydra (OIDC IdP)           │  │
│  └───────────────────────────────┘  │
└─────────────────────────────────────┘
```

- The web app and scheduler run as separate Pebble services in the same rock.
- Only unit 0 runs the scheduler (guaranteed by paas-charm), avoiding duplicate syncs.
- PostgreSQL is managed by the Juju postgresql charm and connected via relation.
- OIDC authentication connects to Hydra (or any OpenID Connect provider).
