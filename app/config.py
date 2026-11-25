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
    # Shared with NetBox's webhook for the event rules, which signs every call with it (X-Hook-Signature).
    netbox_webhook_secret: str

    # The RouterOS service account, the same on every core and leaf (docs/07-security-and-observability.md), and the
    # CA the bootstraps issued the devices' certificates from.
    routeros_username: str
    routeros_password: str
    routeros_ca_file: str

    # The read-only JSON API's bearer token, until the operator UI's Kanidm login (step 5).
    api_token: str

    # Every target is planned this often (docs/06-drift-and-approvals.md), and a NetBox change waits this long for
    # the changes that follow it before the reconcile it triggers runs.
    reconcile_interval_seconds: int = 300
    netbox_debounce_seconds: int = 10
    # A target that hasn't been planned in this many intervals raises "reconcile overdue".
    reconcile_overdue_intervals: int = 3
    # How long a worker may hold a job before it's considered dead and the job is queued again.
    job_lease_seconds: int = 900


def secrets_dir() -> str | None:
    """Where secret files are mounted, one file per setting, named after it (v1 credential storage).

    SHARPWIRE_SECRETS_DIR wins (e.g. /run/secrets with Docker secrets); otherwise systemd's
    CREDENTIALS_DIRECTORY, set when the unit uses LoadCredential=. None means environment only.
    """
    return os.environ.get("SHARPWIRE_SECRETS_DIR") or os.environ.get("CREDENTIALS_DIRECTORY")


@lru_cache
def get_settings() -> Settings:
    return Settings(_secrets_dir=secrets_dir())
