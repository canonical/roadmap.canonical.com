"""Application settings loaded from environment variables."""

import pathlib
import secrets

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

# Resolve the .env file relative to *this* package, not the CWD.
# This means `uvicorn --app-dir …` or running from any directory works.
_ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """All configuration comes from env vars (or .env file).

    Each Jira/JQL field accepts both its plain name (for local .env usage)
    and the APP_-prefixed name that the fastapi-framework charm injects
    from user-defined config options.
    """

    database_url: str = Field(
        default="postgresql://roadmap:roadmap@localhost:5432/roadmap",
        alias="POSTGRESQL_DB_CONNECT_STRING",
    )

    jira_url: str = Field(
        default="https://warthogs.atlassian.net",
        validation_alias=AliasChoices("jira_url", "APP_JIRA_URL"),
    )
    jira_username: str = Field(
        default="",
        validation_alias=AliasChoices("jira_username", "APP_JIRA_USERNAME"),
    )
    jira_pat: str = Field(
        default="",
        validation_alias=AliasChoices("jira_pat", "APP_JIRA_PAT"),
    )
    jql_filter: str = Field(
        default='issuetype = Epic AND "Properties[Checkboxes]" = "Roadmap Item"',
        validation_alias=AliasChoices("jql_filter", "APP_JQL_FILTER"),
    )

    # ── OIDC / OAuth 2.0 ────────────────────────────────────────────
    # Set via juju config/secrets → charm injects as APP_OIDC_* env vars.
    # For local dev, set the plain names in .env.
    oidc_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("oidc_client_id", "APP_OIDC_CLIENT_ID"),
    )
    oidc_client_secret: str = Field(
        default="",
        validation_alias=AliasChoices("oidc_client_secret", "APP_OIDC_CLIENT_SECRET"),
    )
    oidc_issuer: str = Field(
        default="https://iam.green.canonical.com",
        validation_alias=AliasChoices("oidc_issuer", "APP_OIDC_ISSUER"),
    )
    oidc_redirect_uri: str = Field(
        default="http://localhost:8000/callback",
        validation_alias=AliasChoices("oidc_redirect_uri", "APP_OIDC_REDIRECT_URI"),
    )
    # Secret key used to sign the session cookie.
    # A random default is generated at startup; set explicitly for multi-replica deployments.
    session_secret: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        validation_alias=AliasChoices("session_secret", "APP_SESSION_SECRET"),
    )

    # ── Periodic sync ────────────────────────────────────────────
    # Interval (in seconds) between automatic Jira syncs.
    # Set to 0 to disable the periodic sync entirely.
    sync_interval_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("sync_interval_seconds", "APP_SYNC_INTERVAL_SECONDS"),
    )

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


settings = Settings()
