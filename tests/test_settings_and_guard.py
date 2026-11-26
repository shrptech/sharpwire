import pytest
from pydantic import ValidationError

from app.config import Settings
from tests import conftest


@pytest.mark.parametrize("name", ["netbox_webhook_secret", "api_token"])
def test_short_secrets_are_refused(name: str) -> None:
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None, **{name: "short"})  # type: ignore[arg-type]


def test_the_tests_refuse_a_database_not_named_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://sharpwire:x@localhost:5433/sharpwire")
    refusal = conftest._refusal()
    assert refusal is not None and "'sharpwire'" in refusal
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://sharpwire:x@localhost:5433/sharpwire_test")
    assert conftest._refusal() is None
