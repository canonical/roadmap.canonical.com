"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration comes from env vars (or .env file)."""

    database_url: str = "postgresql://roadmap:roadmap@localhost:5432/roadmap"

    jira_url: str = "https://warthogs.atlassian.net"
    jira_username: str = ""
    jira_pat: str = ""
    jql_query: str = 'project = "PLACEHOLDER" AND issuetype = Epic'

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
