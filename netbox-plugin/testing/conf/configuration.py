"""NetBox configuration for the plugin's tests and local development only.

Loaded through NETBOX_ROOT=testing (see the README). It uses the Postgres and Dragonfly from the repo's
docker-compose.yml. The values here are test placeholders, never real secrets.
"""

import os

ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "NAME": os.environ.get("NETBOX_DB_NAME", "netbox"),
        "USER": os.environ.get("NETBOX_DB_USER", "sharpwire"),
        "PASSWORD": os.environ.get("NETBOX_DB_PASSWORD", "sharpwire"),
        "HOST": os.environ.get("NETBOX_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("NETBOX_DB_PORT", "5433"),
        "CONN_MAX_AGE": 300,
    }
}

_REDIS_HOST = os.environ.get("NETBOX_REDIS_HOST", "127.0.0.1")
_REDIS_PORT = int(os.environ.get("NETBOX_REDIS_PORT", "6380"))
REDIS = {
    "tasks": {"HOST": _REDIS_HOST, "PORT": _REDIS_PORT, "USERNAME": "", "PASSWORD": "", "DATABASE": 0, "SSL": False},
    "caching": {"HOST": _REDIS_HOST, "PORT": _REDIS_PORT, "USERNAME": "", "PASSWORD": "", "DATABASE": 1, "SSL": False},
}

PLUGINS = ["sharpwire_netbox"]

RQ = {"COMMIT_MODE": "auto"}

SECRET_KEY = "test-only-not-a-secret-test-only-not-a-secret-test-only-not-a-secret"

API_TOKEN_PEPPERS = {
    1: "TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE-TEST-VALUE-DO-NOT-USE",
}

DEFAULT_PERMISSIONS = {}

# makemigrations is refused without it.
DEVELOPER = True

LOGGING = {"version": 1, "disable_existing_loggers": True}
