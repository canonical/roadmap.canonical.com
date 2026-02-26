"""Application settings loaded from environment variables."""

import pathlib

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
        default='issuetype = Epic AND labels in (26.04, 26.10) AND "Properties[Checkboxes]" = "Roadmap Item"',
        validation_alias=AliasChoices("jql_filter", "APP_JQL_FILTER"),
    )

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


settings = Settings()
