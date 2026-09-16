"""Tests for the executable V2 runtime configuration boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from uma_st2.config import (
    DatabaseSettings,
    IdentityTransitionRehearsalSettings,
    MatchTransitionRehearsalSettings,
    PointTransitionRehearsalSettings,
    RatingTransitionRehearsalSettings,
    RuntimeSettings,
    Win5TransitionRehearsalSettings,
)

DATABASE_URL = "mysql+pymysql://uma_st_2:runtime-password@mariadb:3306/uma_st_2?charset=utf8mb4"
TEST_DATABASE_URL = "mysql+pymysql://uma_st_2:runtime-password@mariadb:3306/uma_st_2_test?charset=utf8mb4"


def _settings(token_file: Path, **overrides: object) -> RuntimeSettings:
    values: dict[str, object] = {
        "database_url": DATABASE_URL,
        "discord_token_file": token_file,
        "discord_guild_id": 987654321,
        "win5_delivery_poll_interval_seconds": 10,
        "win5_delivery_batch_size": 12,
        "win5_delivery_max_attempts": 3,
        "win5_delivery_retry_delay_seconds": 300,
        "win5_delivery_pending_timeout_seconds": 900,
        "log_level": "warning",
    }
    values.update(overrides)
    return RuntimeSettings(_env_file=None, **values)


def test_runtime_settings_resolve_only_file_token_and_derive_delivery_values(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "discord_token"
    token_file.write_text("token.value.signature\n", encoding="utf-8")

    settings = _settings(token_file)

    assert settings.resolve_discord_token() == "token.value.signature"
    assert settings.delivery_poll_interval.total_seconds() == 10
    assert settings.delivery_retry_delay.total_seconds() == 300
    assert settings.delivery_pending_timeout.total_seconds() == 900
    assert settings.log_level == "WARNING"


def test_runtime_settings_repr_does_not_expose_database_credentials(tmp_path: Path) -> None:
    token_file = tmp_path / "discord_token"
    token_file.write_text("token.value.signature", encoding="utf-8")

    rendered = repr(_settings(token_file))

    assert "runtime-password" not in rendered
    assert "mysql+pymysql" not in rendered


def test_database_settings_require_only_safe_database_url() -> None:
    settings = DatabaseSettings(_env_file=None, database_url=DATABASE_URL)

    assert settings.database_url_value == DATABASE_URL
    assert "runtime-password" not in repr(settings)


def test_database_settings_assemble_url_from_password_file(tmp_path: Path) -> None:
    password_file = tmp_path / "mariadb_app_password"
    password_file.write_text("runtime-password\n", encoding="utf-8")
    settings = DatabaseSettings(
        _env_file=None,
        database_host="mariadb",
        database_port=3306,
        database_name="uma_st_2",
        database_user="uma_st_2",
        database_password_file=password_file,
    )

    assert settings.database_url_value == DATABASE_URL
    assert "runtime-password" not in repr(settings)


def test_database_settings_reject_mixed_url_and_password_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        DatabaseSettings(
            _env_file=None,
            database_url=DATABASE_URL,
            database_host="mariadb",
            database_name="uma_st_2",
            database_user="uma_st_2",
            database_password_file=tmp_path / "mariadb_app_password",
        )


@pytest.mark.parametrize("contents", ["", "runtime password"])
def test_database_settings_reject_invalid_password_file(
    tmp_path: Path,
    contents: str,
) -> None:
    password_file = tmp_path / "mariadb_app_password"
    password_file.write_text(contents, encoding="utf-8")
    settings = DatabaseSettings(
        _env_file=None,
        database_host="mariadb",
        database_name="uma_st_2",
        database_user="uma_st_2",
        database_password_file=password_file,
    )

    with pytest.raises(RuntimeError, match="must not be empty|invalid whitespace"):
        _ = settings.database_url_value


def test_database_settings_reject_missing_password_file(tmp_path: Path) -> None:
    settings = DatabaseSettings(
        _env_file=None,
        database_host="mariadb",
        database_name="uma_st_2",
        database_user="uma_st_2",
        database_password_file=tmp_path / "missing",
    )

    with pytest.raises(RuntimeError, match="could not be read"):
        _ = settings.database_url_value


@pytest.mark.parametrize(
    "settings_type",
    [
        IdentityTransitionRehearsalSettings,
        Win5TransitionRehearsalSettings,
        MatchTransitionRehearsalSettings,
        RatingTransitionRehearsalSettings,
        PointTransitionRehearsalSettings,
    ],
)
def test_transition_rehearsal_settings_require_isolated_test_database(settings_type: type) -> None:
    settings = settings_type(
        _env_file=None,
        test_database_url=TEST_DATABASE_URL,
    )

    assert settings.database_url_value == TEST_DATABASE_URL
    assert "runtime-password" not in repr(settings)

    with pytest.raises(ValidationError, match="must end in '_test'"):
        settings_type(
            _env_file=None,
            test_database_url=DATABASE_URL,
        )


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+pysqlite:///:memory:",
        "mysql+pymysql://uma_st_2:runtime-password@mariadb:3306/uma_st_2",
        "mysql+pymysql://uma_st_2:change-me@mariadb:3306/uma_st_2?charset=utf8mb4",
        "mysql+pymysql://uma_st_2:runtime-password@/uma_st_2?charset=utf8mb4",
    ],
)
def test_runtime_settings_reject_unsafe_database_url(
    tmp_path: Path,
    database_url: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path / "token", database_url=database_url)


def test_runtime_settings_require_positive_delivery_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _settings(
            tmp_path / "token",
            win5_delivery_pending_timeout_seconds=0,
        )


def test_runtime_settings_reject_missing_or_malformed_token_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="could not be read"):
        settings.resolve_discord_token()

    malformed = tmp_path / "malformed"
    malformed.write_text("token value", encoding="utf-8")
    with pytest.raises(RuntimeError, match="whitespace"):
        _settings(malformed).resolve_discord_token()
