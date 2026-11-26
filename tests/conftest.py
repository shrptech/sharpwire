import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Settings are required at startup; tests that don't touch a database or NetBox only need placeholders.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://sharpwire:sharpwire@localhost:5433/sharpwire_test")
os.environ.setdefault("NETBOX_URL", "https://netbox.test.invalid")
os.environ.setdefault("NETBOX_TOKEN", "test-placeholder-token")
os.environ.setdefault("NETBOX_WEBHOOK_SECRET", "test-webhook-secret-placeholder")
os.environ.setdefault("ROUTEROS_USERNAME", "sharpwire")
os.environ.setdefault("ROUTEROS_PASSWORD", "test-placeholder-password")
os.environ.setdefault("ROUTEROS_CA_FILE", "/nonexistent/routeros-ca.pem")
os.environ.setdefault("API_TOKEN", "test-api-token-placeholder")


# --- The database: the real Postgres from docker compose (never a mocked DB layer, as in sharpstack) --------------

TABLES = "alerts, plans, jobs, targets"


def _refusal() -> str | None:
    """The tests truncate every table in the database they use, so only a *_test database is theirs (as sharpstack)."""
    name = make_url(os.environ["DATABASE_URL"]).database or ""
    if name.endswith("_test"):
        return None
    return (
        f"DATABASE_URL names the database {name!r}, and the tests empty every table in the database they use. "
        "Unset DATABASE_URL to use sharpwire_test, or point it at a database whose name ends in _test."
    )


def pytest_configure(config: pytest.Config) -> None:
    if (refusal := _refusal()) is not None:
        pytest.exit(refusal, returncode=2)


@pytest.fixture(scope="session")
def migrated() -> Iterator[None]:
    """The test database at the latest migration, once per run."""
    assert _refusal() is None
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture
async def session_factory(migrated: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory on the test database, emptied after each test. Code under test commits for real."""
    # A pool of its own per test: asyncpg connections belong to the test's event loop.
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES}"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES}"))
    await engine.dispose()
