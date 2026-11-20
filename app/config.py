import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings, from environment variables (and `.env` in dev) or from secret files.

    The app refuses to start unless every setting without a default is present.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The controller's own Postgres: plans, runs, alerts, audit. Never shared with sharpstack.
    database_url: str
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # NetBox, the intent source (pinned to 4.7, like sharpstack).
    netbox_url: str
    netbox_token: str


def secrets_dir() -> str | None:
    """Where secret files are mounted, one file per setting, named after it (v1 credential storage).

    SHARPWIRE_SECRETS_DIR wins (e.g. /run/secrets with Docker secrets); otherwise systemd's
    CREDENTIALS_DIRECTORY, set when the unit uses LoadCredential=. None means environment only.
    """
    return os.environ.get("SHARPWIRE_SECRETS_DIR") or os.environ.get("CREDENTIALS_DIRECTORY")


@lru_cache
def get_settings() -> Settings:
    return Settings(_secrets_dir=secrets_dir())
