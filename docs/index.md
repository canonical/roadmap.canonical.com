# roadmap-web Documentation

Welcome to the documentation for **roadmap-web** — a company-wide roadmap visualization tool. Data flows from Jira → PostgreSQL → FastAPI → server-rendered Vanilla Framework UI.

This documentation follows the [Diátaxis](https://diataxis.fr/) framework and is organised into four sections:

## How-to guides

Step-by-step instructions for administrators operating the system.

- [Getting started](how-to/getting-started.md) — set up a local development environment
- [Managing products](how-to/managing-products.md) — create, update, and delete products with Jira mappings
- [Managing cycles](how-to/managing-cycles.md) — register cycles, transition states, freeze and unfreeze
- [Triggering a Jira sync](how-to/triggering-sync.md) — run syncs manually and monitor progress
- [Generating change reports](how-to/change-reports.md) — compare snapshots to find what changed
- [Configuring authentication](how-to/configuring-authentication.md) — set up OIDC/SSO for production
- [Running tests](how-to/running-tests.md) — execute the test suite locally

## Reference

Technical specifications for developers.

- [API reference](reference/api.md) — all HTTP endpoints, request/response schemas
- [Database schema](reference/database-schema.md) — tables, columns, indexes, constraints
- [Configuration](reference/configuration.md) — all environment variables and settings
- [Project structure](reference/project-structure.md) — file layout and module responsibilities

## Explanation

Background context and rationale for key design decisions.

- [Architecture overview](explanation/architecture.md) — data flow, component roles, technology choices
- [Jira sync pipeline](explanation/jira-sync-pipeline.md) — two-phase fetch + process design
- [Color and health logic](explanation/color-health-logic.md) — how epic health colors are derived
- [Cycle lifecycle](explanation/cycle-lifecycle.md) — frozen/current/future state machine
- [Snapshot and change tracking](explanation/snapshots.md) — daily snapshots and diff reports
- [Product-Jira mapping](explanation/product-jira-mapping.md) — how issues are matched to products
- [Authentication flow](explanation/authentication.md) — OIDC transparent SSO design
