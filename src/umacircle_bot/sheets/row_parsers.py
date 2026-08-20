from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Generic, TypeVar

T = TypeVar("T")

MAX_TEXT_LENGTH = 200
MAX_INT_VALUE = 2_147_483_647


@dataclass(frozen=True)
class RowParseIssue:
    row_number: int
    code: str
    message: str
    sheet_name: str | None = None


@dataclass(frozen=True)
class RowParseReport(Generic[T]):
    rows: tuple[T, ...]
    issues: tuple[RowParseIssue, ...]

    @property
    def has_errors(self) -> bool:
        return bool(self.issues)


@dataclass(frozen=True)
class RoomPointSourceRow:
    row_number: int
    discord_user_id: str
    nickname: str
    points: int
    ingame_name: str | None


@dataclass(frozen=True)
class PlayerStatSourceRow:
    row_number: int
    player_name: str
    rate: Decimal | None
    total_count: int
    first_count: int
    second_count: int
    third_count: int


@dataclass(frozen=True)
class MatchDataSourceRow:
    row_number: int
    raced_at: datetime | None
    room_match_name: str
    grade: str | None
    player_name: str
    uma_name: str
    rank: int | None
    running_style: str | None
    is_excluded: bool
    venue: str | None
    track_surface: str | None
    distance: int | None
    direction: str | None
    season: str | None
    weather: str | None
    track_condition: str | None
    condition_label: str | None
    max_participant_count: int | None
    converted_rank: int | None
    rating_before: Decimal | None
    rating_diff: Decimal | None
    base_delta: Decimal | None
    adjustment_delta: Decimal | None
    rating_after: Decimal | None
    rank_ratio: Decimal | None


@dataclass(frozen=True)
class Win5ScoreSourceRow:
    row_number: int
    round_number: int | None
    round_label: str
    nickname: str
    score: int


def parse_room_point_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "동친 포인트",
    start_row: int = 1,
) -> RowParseReport[RoomPointSourceRow]:
    parsed: list[RoomPointSourceRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if _is_blank_row(row) or row_number == start_row:
            continue
        try:
            parsed.append(
                RoomPointSourceRow(
                    row_number=row_number,
                    discord_user_id=_parse_discord_user_id(_cell(row, 0)),
                    nickname=_parse_required_text(_cell(row, 1), field_name="nickname"),
                    points=_parse_int(_cell(row, 2), field_name="points", minimum=0),
                    ingame_name=_parse_optional_text(_cell(row, 3), field_name="ingame_name"),
                )
            )
        except ValueError as exc:
            issues.append(_issue(row_number, str(exc), sheet_name=sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def parse_player_stat_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "플레이어",
    start_row: int = 1,
) -> RowParseReport[PlayerStatSourceRow]:
    parsed: list[PlayerStatSourceRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if _is_blank_row(row) or row_number == start_row:
            continue
        try:
            parsed.append(
                PlayerStatSourceRow(
                    row_number=row_number,
                    player_name=_parse_required_text(_cell(row, 0), field_name="player_name"),
                    rate=_parse_optional_decimal(_cell(row, 1), field_name="rate"),
                    total_count=_parse_int(_cell(row, 2), field_name="total_count", minimum=0),
                    first_count=_parse_int(_cell(row, 3), field_name="first_count", minimum=0),
                    second_count=_parse_int(_cell(row, 4), field_name="second_count", minimum=0),
                    third_count=_parse_int(_cell(row, 5), field_name="third_count", minimum=0),
                )
            )
        except ValueError as exc:
            issues.append(_issue(row_number, str(exc), sheet_name=sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def parse_match_data_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "Data",
    start_row: int = 1,
) -> RowParseReport[MatchDataSourceRow]:
    parsed: list[MatchDataSourceRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if _is_blank_row(row) or _is_blank_match_data_row(row) or row_number == start_row:
            continue
        try:
            parsed.append(
                MatchDataSourceRow(
                    row_number=row_number,
                    raced_at=_parse_optional_datetime(_cell(row, 0), field_name="raced_at"),
                    room_match_name=_parse_required_text(_cell(row, 1), field_name="room_match_name"),
                    grade=_parse_optional_text(_cell(row, 2), field_name="grade"),
                    player_name=_parse_required_text(_cell(row, 3), field_name="player_name"),
                    uma_name=_parse_required_text(_cell(row, 4), field_name="uma_name"),
                    rank=_parse_optional_int(_cell(row, 5), field_name="rank", minimum=1),
                    running_style=_parse_optional_text(_cell(row, 6), field_name="running_style"),
                    is_excluded=_parse_bool(_cell(row, 7), field_name="is_excluded"),
                    venue=_parse_optional_text(_cell(row, 8), field_name="venue"),
                    track_surface=_parse_optional_text(_cell(row, 9), field_name="track_surface"),
                    distance=_parse_optional_int(_cell(row, 10), field_name="distance", minimum=1),
                    direction=_parse_optional_text(_cell(row, 11), field_name="direction"),
                    season=_parse_optional_text(_cell(row, 12), field_name="season"),
                    weather=_parse_optional_text(_cell(row, 13), field_name="weather"),
                    track_condition=_parse_optional_text(_cell(row, 14), field_name="track_condition"),
                    condition_label=_parse_optional_text(_cell(row, 15), field_name="condition_label"),
                    max_participant_count=_parse_optional_int(
                        _cell(row, 16), field_name="max_participant_count", minimum=1
                    ),
                    converted_rank=_parse_optional_int(_cell(row, 17), field_name="converted_rank", minimum=1),
                    rating_before=_parse_optional_decimal(_cell(row, 18), field_name="rating_before"),
                    rating_diff=_parse_optional_decimal(_cell(row, 19), field_name="rating_diff"),
                    base_delta=_parse_optional_decimal(_cell(row, 20), field_name="base_delta"),
                    adjustment_delta=_parse_optional_decimal(_cell(row, 21), field_name="adjustment_delta"),
                    rating_after=_parse_optional_decimal(_cell(row, 22), field_name="rating_after"),
                    rank_ratio=_parse_optional_decimal(_cell(row, 23), field_name="rank_ratio"),
                )
            )
        except ValueError as exc:
            issues.append(_issue(row_number, str(exc), sheet_name=sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def parse_win5_score_rows(
    rows: Iterable[Sequence[Any]],
    *,
    sheet_name: str = "스코어",
    start_row: int = 1,
) -> RowParseReport[Win5ScoreSourceRow]:
    parsed: list[Win5ScoreSourceRow] = []
    issues: list[RowParseIssue] = []
    for row_number, row in _enumerate_rows(rows, start_row=start_row):
        if _is_blank_row(row) or row_number < start_row + 2:
            continue
        try:
            round_number, round_label = _parse_win5_round_cell(_cell(row, 1))
            parsed.append(
                Win5ScoreSourceRow(
                    row_number=row_number,
                    round_number=round_number,
                    round_label=round_label,
                    nickname=_parse_required_text(_cell(row, 2), field_name="nickname"),
                    score=_parse_int(_cell(row, 3), field_name="score", minimum=0),
                )
            )
        except ValueError as exc:
            issues.append(_issue(row_number, str(exc), sheet_name=sheet_name))
    return RowParseReport(rows=tuple(parsed), issues=tuple(issues))


def _enumerate_rows(rows: Iterable[Sequence[Any]], *, start_row: int) -> Iterable[tuple[int, Sequence[Any]]]:
    if not isinstance(start_row, int) or isinstance(start_row, bool) or start_row <= 0:
        raise ValueError("start_row must be a positive integer")
    for offset, row in enumerate(rows):
        yield start_row + offset, row


def _cell(row: Sequence[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def _is_blank_row(row: Sequence[Any]) -> bool:
    return all(_normalize_blank(value) is None for value in row)


def _is_blank_match_data_row(row: Sequence[Any]) -> bool:
    meaningful_indexes = (0, 1, 2, 3, 4, 5, 6, 8, 9)
    return all(_normalize_blank(_cell(row, index)) is None for index in meaningful_indexes)


def _parse_win5_round_cell(value: Any) -> tuple[int | None, str]:
    normalized = _normalize_blank(value)
    if normalized is None:
        raise ValueError("round_label is required")
    if isinstance(normalized, bool):
        raise ValueError("round_label must be text or integer")
    if isinstance(normalized, int):
        return _validate_round_number(normalized), str(normalized)
    if isinstance(normalized, float) and normalized.is_integer():
        round_number = _validate_round_number(int(normalized))
        return round_number, str(round_number)
    if isinstance(normalized, Decimal) and normalized == normalized.to_integral_value():
        round_number = _validate_round_number(int(normalized))
        return round_number, str(round_number)
    round_label = _parse_required_text(normalized, field_name="round_label", max_length=32)
    return None, round_label


def _validate_round_number(value: int) -> int:
    if not 1 <= value <= MAX_INT_VALUE:
        raise ValueError(f"round_number must be between 1 and {MAX_INT_VALUE}")
    return value


def _parse_discord_user_id(value: Any) -> str:
    normalized = _parse_required_text(value, field_name="discord_user_id", max_length=32)
    if not normalized.isascii() or not normalized.isdigit():
        raise ValueError("discord_user_id must contain only ASCII digits")
    return normalized


def _parse_required_text(value: Any, *, field_name: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    normalized = _parse_optional_text(value, field_name=field_name, max_length=max_length)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _parse_optional_text(value: Any, *, field_name: str, max_length: int = MAX_TEXT_LENGTH) -> str | None:
    normalized = _normalize_blank(value)
    if normalized is None:
        return None
    if not isinstance(normalized, str):
        normalized = str(normalized).strip()
    if not normalized:
        return None
    if len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{field_name} contains unsupported text")
    return normalized


def _parse_int(value: Any, *, field_name: str, minimum: int = 0, maximum: int = MAX_INT_VALUE) -> int:
    parsed = _parse_optional_int(value, field_name=field_name, minimum=minimum, maximum=maximum)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def _parse_optional_int(
    value: Any,
    *,
    field_name: str,
    minimum: int = 0,
    maximum: int = MAX_INT_VALUE,
) -> int | None:
    normalized = _normalize_blank(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        raise ValueError(f"{field_name} must be an integer")
    if isinstance(normalized, int):
        parsed = normalized
    elif isinstance(normalized, float) and normalized.is_integer():
        parsed = int(normalized)
    elif isinstance(normalized, Decimal) and normalized == normalized.to_integral_value():
        parsed = int(normalized)
    elif isinstance(normalized, str) and normalized.isascii() and normalized.isdigit():
        parsed = int(normalized)
    else:
        raise ValueError(f"{field_name} must be an integer")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


def _parse_optional_decimal(value: Any, *, field_name: str) -> Decimal | None:
    normalized = _normalize_blank(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        parsed = Decimal(str(normalized))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _parse_optional_datetime(value: Any, *, field_name: str) -> datetime | None:
    normalized = _normalize_blank(value)
    if normalized is None:
        return None
    if not isinstance(normalized, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return normalized.replace(microsecond=0)


def _parse_bool(value: Any, *, field_name: str) -> bool:
    normalized = _normalize_blank(value)
    if normalized is None:
        return False
    if isinstance(normalized, bool):
        return normalized
    if isinstance(normalized, int) and normalized in {0, 1}:
        return bool(normalized)
    if isinstance(normalized, str):
        lowered = normalized.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    raise ValueError(f"{field_name} must be boolean")


def _normalize_blank(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _issue(row_number: int, message: str, *, sheet_name: str) -> RowParseIssue:
    return RowParseIssue(
        row_number=row_number,
        code="invalid_row",
        message=message,
        sheet_name=sheet_name,
    )
