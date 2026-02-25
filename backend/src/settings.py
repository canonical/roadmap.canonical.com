"""Application settings loaded from environment variables."""

import pathlib

from pydantic_settings import BaseSettings

# Resolve the .env file relative to *this* package, not the CWD.
# This means `uvicorn --app-dir …` or running from any directory works.
_ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """All configuration comes from env vars (or .env file)."""

    database_url: str = "postgresql://roadmap:roadmap@localhost:5432/roadmap"

    jira_url: str = "https://warthogs.atlassian.net"
    jira_username: str = ""
    jira_pat: str = ""
    jql_filter: str = "issuetype = Epic"

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


settings = Settings()
