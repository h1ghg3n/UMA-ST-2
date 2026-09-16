"""Top-level deployment configuration for the UMA-ST-2 V2 runtime."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

_MAX_DISCORD_SNOWFLAKE = 2**64 - 1
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class DatabaseSettings(BaseSettings):
    """Database-only settings shared by long-running and one-shot processes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr | None = Field(
        default=None,
        alias="DATABASE_URL",
        repr=False,
    )
    database_host: str | None = Field(default=None, alias="DATABASE_HOST")
    database_port: int = Field(default=3306, alias="DATABASE_PORT", gt=0, le=65_535)
    database_name: str | None = Field(default=None, alias="DATABASE_NAME")
    database_user: str | None = Field(default=None, alias="DATABASE_USER")
    database_password_file: Path | None = Field(
        default=None,
        alias="DATABASE_PASSWORD_FILE",
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        return _validate_database_url(value, setting_name="DATABASE_URL")

    @model_validator(mode="after")
    def validate_database_source(self) -> DatabaseSettings:
        component_values = (
            self.database_host,
            self.database_name,
            self.database_user,
            self.database_password_file,
        )
        if self.database_url is not None:
            if any(value is not None for value in component_values):
                raise ValueError(
                    "DATABASE_URL cannot be combined with DATABASE_HOST, "
                    "DATABASE_NAME, DATABASE_USER, or DATABASE_PASSWORD_FILE."
                )
            return self
        if any(value is None for value in component_values):
            raise ValueError(
                "Set DATABASE_URL or all of DATABASE_HOST, DATABASE_NAME, DATABASE_USER, and DATABASE_PASSWORD_FILE."
            )
        return self

    @property
    def database_url_value(self) -> str:
        """Return the injected secret only at a Composition Root boundary."""

        if self.database_url is not None:
            return self.database_url.get_secret_value()

        assert self.database_host is not None
        assert self.database_name is not None
        assert self.database_user is not None
        assert self.database_password_file is not None
        try:
            password = self.database_password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("DATABASE_PASSWORD_FILE could not be read.") from exc
        if not password:
            raise RuntimeError("DATABASE_PASSWORD_FILE must not be empty.")
        if any(character.isspace() for character in password):
            raise RuntimeError("DATABASE_PASSWORD_FILE contains invalid whitespace.")

        database_url = URL.create(
            "mysql+pymysql",
            username=self.database_user,
            password=password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
            query={"charset": "utf8mb4"},
        ).render_as_string(hide_password=False)
        validated = _validate_database_url(
            SecretStr(database_url),
            setting_name="DATABASE_PASSWORD_FILE assembled URL",
        )
        return validated.get_secret_value()


class V1SourceDatabaseSettings(BaseSettings):
    """Explicit read-only source settings for one-shot V1 transition tooling."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    v1_source_database_url: SecretStr = Field(alias="V1_SOURCE_DATABASE_URL", repr=False)

    @field_validator("v1_source_database_url")
    @classmethod
    def validate_source_database_url(cls, value: SecretStr) -> SecretStr:
        return _validate_database_url(value, setting_name="V1_SOURCE_DATABASE_URL")

    @property
    def database_url_value(self) -> str:
        """Return the injected source secret only at the transition entrypoint."""

        return self.v1_source_database_url.get_secret_value()


class _TransitionRehearsalSettings(BaseSettings):
    """Shared isolated-target boundary for phase-specific rehearsal commands."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    test_database_url: SecretStr = Field(alias="TEST_DATABASE_URL", repr=False)

    @field_validator("test_database_url")
    @classmethod
    def validate_test_database_url(cls, value: SecretStr) -> SecretStr:
        validated = _validate_database_url(value, setting_name="TEST_DATABASE_URL")
        database_name = urlsplit(validated.get_secret_value()).path.strip("/")
        if not database_name.endswith("_test"):
            raise ValueError("TEST_DATABASE_URL database name must end in '_test'.")
        return validated

    @property
    def database_url_value(self) -> str:
        """Return the isolated target secret only at the rehearsal entrypoint."""

        return self.test_database_url.get_secret_value()


class IdentityTransitionRehearsalSettings(_TransitionRehearsalSettings):
    """Isolated target settings for the Phase 1 disposable rehearsal command."""


class Win5TransitionRehearsalSettings(_TransitionRehearsalSettings):
    """Isolated target settings for the Phase 2 disposable rehearsal command."""


class MatchTransitionRehearsalSettings(_TransitionRehearsalSettings):
    """Isolated target settings for the Phase 3 disposable rehearsal command."""


class RatingTransitionRehearsalSettings(_TransitionRehearsalSettings):
    """Isolated target settings for the Phase 4 disposable rehearsal command."""


class PointTransitionRehearsalSettings(_TransitionRehearsalSettings):
    """Isolated target settings for the Phase 5 disposable rehearsal command."""


class RuntimeSettings(DatabaseSettings):
    """Required Discord process settings loaded once at its executable boundary."""

    discord_token_file: Path = Field(alias="DISCORD_TOKEN_FILE")
    discord_guild_id: int = Field(
        alias="DISCORD_GUILD_ID",
        gt=0,
        le=_MAX_DISCORD_SNOWFLAKE,
    )
    win5_delivery_poll_interval_seconds: int = Field(
        alias="WIN5_DELIVERY_POLL_INTERVAL_SECONDS",
        gt=0,
        le=86_400,
    )
    win5_delivery_batch_size: int = Field(
        alias="WIN5_DELIVERY_BATCH_SIZE",
        gt=0,
        le=1_000,
    )
    win5_delivery_max_attempts: int = Field(
        alias="WIN5_DELIVERY_MAX_ATTEMPTS",
        gt=0,
        le=100,
    )
    win5_delivery_retry_delay_seconds: int = Field(
        alias="WIN5_DELIVERY_RETRY_DELAY_SECONDS",
        gt=0,
        le=2_592_000,
    )
    win5_delivery_pending_timeout_seconds: int = Field(
        alias="WIN5_DELIVERY_PENDING_TIMEOUT_SECONDS",
        gt=0,
        le=2_592_000,
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        canonical = value.strip().upper()
        if canonical not in _LOG_LEVELS:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO, or DEBUG.")
        return canonical

    @property
    def delivery_poll_interval(self) -> timedelta:
        return timedelta(seconds=self.win5_delivery_poll_interval_seconds)

    @property
    def delivery_retry_delay(self) -> timedelta:
        return timedelta(seconds=self.win5_delivery_retry_delay_seconds)

    @property
    def delivery_pending_timeout(self) -> timedelta:
        return timedelta(seconds=self.win5_delivery_pending_timeout_seconds)

    def resolve_discord_token(self) -> str:
        """Read the Discord token without supporting an inline environment secret."""

        try:
            token = self.discord_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("DISCORD_TOKEN_FILE could not be read.") from exc
        if not token:
            raise RuntimeError("DISCORD_TOKEN_FILE must not be empty.")
        if any(character.isspace() for character in token):
            raise RuntimeError("DISCORD_TOKEN_FILE contains invalid whitespace.")
        return token


def _validate_database_url(value: SecretStr, *, setting_name: str) -> SecretStr:
    raw = value.get_secret_value()
    parsed = urlsplit(raw)
    query = parse_qs(parsed.query)
    charset = tuple(item.lower() for item in query.get("charset", ()))
    if parsed.scheme != "mysql+pymysql":
        raise ValueError(f"{setting_name} must use mysql+pymysql.")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError(f"{setting_name} must include a database host and name.")
    if not parsed.username or parsed.password in {None, "", "change-me", "change-root"}:
        raise ValueError(f"{setting_name} must include non-placeholder credentials.")
    if charset != ("utf8mb4",):
        raise ValueError(f"{setting_name} must set charset=utf8mb4 exactly once.")
    return value
