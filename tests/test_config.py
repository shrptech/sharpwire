from pathlib import Path

import pytest

from app.config import Settings, secrets_dir


def test_secret_files_fill_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    (tmp_path / "netbox_token").write_text("from-a-file")
    settings = Settings(_secrets_dir=tmp_path, _env_file=None)
    assert settings.netbox_token == "from-a-file"


def test_sharpwire_secrets_dir_wins_over_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/sharpwire.service")
    monkeypatch.setenv("SHARPWIRE_SECRETS_DIR", "/run/secrets")
    assert secrets_dir() == "/run/secrets"


def test_no_secrets_dir_means_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("SHARPWIRE_SECRETS_DIR", raising=False)
    assert secrets_dir() is None
