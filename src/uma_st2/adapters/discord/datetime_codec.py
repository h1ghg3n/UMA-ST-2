"""Discord operator-input and presentation datetime translation."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum

from uma_st2.shared import normalize_utc_datetime

_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_PATTERN = re.compile(r"[0-9]{2}:[0-9]{2}")
_KST = timezone(timedelta(hours=9), name="KST")


class DiscordTimezone(StrEnum):
    """Tracked timezone choices exposed by Discord interactions."""

    KST = "KST"
    UTC = "UTC"


def _normalize_timezone(value: DiscordTimezone | str) -> DiscordTimezone:
    try:
        return DiscordTimezone(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timezone must be KST or UTC.") from exc


def parse_operator_date_and_time(
    date_value: str,
    time_value: str,
    timezone_name: DiscordTimezone | str = DiscordTimezone.KST,
) -> datetime:
    """Parse strict operator fields and return a timezone-aware UTC instant."""

    if not isinstance(date_value, str) or _DATE_PATTERN.fullmatch(date_value) is None:
        raise ValueError("date must use YYYY-MM-DD format.")
    if not isinstance(time_value, str) or _TIME_PATTERN.fullmatch(time_value) is None:
        raise ValueError("time must use HH:MM format.")
    selected_timezone = _normalize_timezone(timezone_name)
    try:
        local_datetime = datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("date and time must form a valid calendar instant.") from exc

    tzinfo = _KST if selected_timezone == DiscordTimezone.KST else UTC
    return local_datetime.replace(tzinfo=tzinfo).astimezone(UTC)


def format_discord_datetime(
    value: datetime,
    timezone_name: DiscordTimezone | str = DiscordTimezone.KST,
) -> str:
    """Render an aware instant using the tracked Discord display format."""

    instant = normalize_utc_datetime(value, field_name="Discord datetime")
    selected_timezone = _normalize_timezone(timezone_name)
    if selected_timezone == DiscordTimezone.UTC:
        return instant.strftime("%Y-%m-%d %H:%M UTC")
    return instant.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")
