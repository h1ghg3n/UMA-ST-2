"""UTC/KST translation boundaries shared by Discord, application, and persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from uma_st2.adapters.discord import (
    DiscordTimezone,
    format_discord_datetime,
    parse_operator_date_and_time,
)
from uma_st2.infrastructure.database.datetime_codec import (
    from_database_utc,
    to_database_utc,
)
from uma_st2.shared import normalize_utc_datetime, utc_now

KST = timezone(timedelta(hours=9), name="KST")


@pytest.mark.parametrize(
    ("timezone_name", "expected"),
    [
        (DiscordTimezone.KST, datetime(2030, 1, 1, 12, 30, tzinfo=UTC)),
        (DiscordTimezone.UTC, datetime(2030, 1, 1, 21, 30, tzinfo=UTC)),
    ],
)
def test_discord_operator_fields_parse_to_aware_utc(
    timezone_name: DiscordTimezone,
    expected: datetime,
) -> None:
    assert parse_operator_date_and_time("2030-01-01", "21:30", timezone_name) == expected


def test_discord_operator_fields_default_to_fixed_kst() -> None:
    assert parse_operator_date_and_time("2030-01-01", "09:00") == datetime(
        2030,
        1,
        1,
        0,
        0,
        tzinfo=UTC,
    )


@pytest.mark.parametrize(
    ("date_value", "time_value", "timezone_name"),
    [
        ("2030/01/01", "21:30", "KST"),
        ("2030-02-30", "21:30", "KST"),
        ("2030-01-01", "24:00", "KST"),
        ("2030-01-01", "21:30", "JST"),
    ],
)
def test_discord_operator_fields_reject_invalid_or_untracked_values(
    date_value: str,
    time_value: str,
    timezone_name: str,
) -> None:
    with pytest.raises(ValueError):
        parse_operator_date_and_time(date_value, time_value, timezone_name)


def test_discord_presentation_defaults_to_kst_and_supports_explicit_utc() -> None:
    instant = datetime(2030, 1, 1, 12, 30, tzinfo=UTC)

    assert format_discord_datetime(instant) == "2030-01-01 21:30 KST"
    assert format_discord_datetime(instant, DiscordTimezone.UTC) == "2030-01-01 12:30 UTC"


def test_naive_application_or_discord_datetime_is_rejected() -> None:
    naive = datetime(2030, 1, 1, 12, 30)

    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_utc_datetime(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        format_discord_datetime(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        to_database_utc(naive)


def test_database_codec_round_trips_utc_through_naive_datetime_storage() -> None:
    kst_instant = datetime(2030, 1, 1, 21, 30, tzinfo=KST)
    stored = to_database_utc(kst_instant)

    assert stored == datetime(2030, 1, 1, 12, 30)
    assert stored.tzinfo is None
    assert from_database_utc(stored) == datetime(2030, 1, 1, 12, 30, tzinfo=UTC)


def test_utc_clock_is_timezone_aware() -> None:
    current = utc_now()

    assert current.tzinfo is UTC
    assert current.utcoffset() == timedelta(0)
