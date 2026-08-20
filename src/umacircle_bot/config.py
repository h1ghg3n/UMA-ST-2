from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str = Field(default="", alias="DISCORD_TOKEN")
    discord_token_file: Path | None = Field(default=None, alias="DISCORD_TOKEN_FILE")
    discord_guild_id: int = Field(default=0, ge=0, le=2**64 - 1, alias="DISCORD_GUILD_ID")
    discord_admin_channel_ids: str = Field(default="", alias="DISCORD_ADMIN_CHANNEL_IDS")
    owner_role_id: int = Field(default=0, ge=0, le=2**64 - 1, alias="OWNER_ROLE_ID")

    database_url: str = Field(
        default="mysql+pymysql://uma_st2:change-me@mariadb:3306/uma_st2?charset=utf8mb4",
        alias="DATABASE_URL",
    )

    app_timezone: str = Field(default="Asia/Seoul", alias="APP_TIMEZONE")
    app_utc_offset_minutes: int = Field(default=540, alias="APP_UTC_OFFSET_MINUTES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    legacy_player_link_enabled: bool = Field(default=False, alias="ENABLE_LEGACY_PLAYER_LINK")

    default_match_points: int = Field(default=500, alias="DEFAULT_MATCH_POINTS")
    min_match_bet_amount: int = Field(default=10, ge=1, le=21_474_836_470, alias="MIN_MATCH_BET_AMOUNT")
    max_match_bet_amount: int = Field(
        default=21_474_836_470,
        ge=1,
        le=21_474_836_470,
        alias="MAX_MATCH_BET_AMOUNT",
    )
    bet_close_minutes: int = Field(default=30, ge=0, le=10_080, alias="BET_CLOSE_MINUTES")

    export_dir: Path = Field(default=Path("exports"), alias="EXPORT_DIR")
    backup_dir: Path = Field(default=Path("backups"), alias="BACKUP_DIR")

    @model_validator(mode="after")
    def validate_match_bet_amount_range(self) -> Self:
        if self.min_match_bet_amount > self.max_match_bet_amount:
            raise ValueError("minimum match bet amount must not exceed maximum")
        return self

    @property
    def admin_channel_ids(self) -> tuple[int, ...]:
        return _parse_discord_snowflake_csv(
            self.discord_admin_channel_ids,
            field_name="DISCORD_ADMIN_CHANNEL_IDS",
        )

    @model_validator(mode="after")
    def validate_discord_channel_ids(self) -> Self:
        _parse_discord_snowflake_csv(
            self.discord_admin_channel_ids,
            field_name="DISCORD_ADMIN_CHANNEL_IDS",
        )
        return self

    @model_validator(mode="after")
    def validate_privileged_role_ids(self) -> Self:
        if self.discord_guild_id and self.owner_role_id == self.discord_guild_id:
            raise ValueError("OWNER_ROLE_ID must not use the guild @everyone role ID")
        return self

    def validate_runtime(self) -> None:
        """Validate values required by the deployed Discord bot process."""
        self.resolve_discord_token()
        if not self.discord_guild_id:
            raise RuntimeError("DISCORD_GUILD_ID is required")
        if not self.owner_role_id:
            raise RuntimeError("OWNER_ROLE_ID is required")

        parsed_database_url = urlsplit(self.database_url)
        if parsed_database_url.scheme != "mysql+pymysql":
            raise RuntimeError("DATABASE_URL must use mysql+pymysql for the deployed bot")
        if not parsed_database_url.hostname or not parsed_database_url.path.strip("/"):
            raise RuntimeError("DATABASE_URL must include a database host and name")
        if not parsed_database_url.username or parsed_database_url.password in {None, "", "change-me"}:
            raise RuntimeError("DATABASE_URL must include non-placeholder database credentials")
        try:
            ZoneInfo(self.app_timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError("APP_TIMEZONE is not a recognized IANA timezone") from exc

    def resolve_discord_token(self) -> str:
        inline_token = self.discord_token.strip()
        if inline_token and self.discord_token_file is not None:
            raise RuntimeError("configure only one of DISCORD_TOKEN or DISCORD_TOKEN_FILE")
        if inline_token:
            return inline_token
        if self.discord_token_file is None:
            raise RuntimeError("DISCORD_TOKEN or DISCORD_TOKEN_FILE is required")

        try:
            file_token = self.discord_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("DISCORD_TOKEN_FILE could not be read") from exc
        if not file_token:
            raise RuntimeError("DISCORD_TOKEN_FILE must not be empty")
        return file_token


def _parse_discord_snowflake_csv(value: str, *, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if not value.strip():
        return ()
    parts = [part.strip() for part in value.split(",")]
    if any(not part or not part.isascii() or not part.isdigit() for part in parts):
        raise ValueError(f"{field_name} must contain comma-separated Discord IDs")
    identifiers = tuple(int(part) for part in parts)
    if any(not 1 <= identifier <= 2**64 - 1 for identifier in identifiers):
        raise ValueError(f"{field_name} contains an out-of-range Discord ID")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{field_name} must not contain duplicate Discord IDs")
    return identifiers


@lru_cache
def get_settings() -> Settings:
    return Settings()
