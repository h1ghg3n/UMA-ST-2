"""Pure Discord presentation for supported stored Match publications."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Final

from uma_st2.application.publication import (
    MATCH_BETTING_CLOSE_PAYLOAD_SCHEMA_VERSION,
    MATCH_BETTING_CLOSE_PUBLICATION_TYPE,
    MATCH_ODDS_REFRESH_PAYLOAD_SCHEMA_VERSION,
    MATCH_ODDS_REFRESH_PUBLICATION_TYPE,
    MATCH_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
    MATCH_RESULT_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
    build_zero_pool_opening_markets,
)
from uma_st2.application.publication.match_odds import MatchOddsRefreshMode
from uma_st2.domain.betting import (
    APPLIED_ODDS_QUANTUM,
    PROVISIONAL_ODDS_QUANTUM,
    WEIGHTED_ODDS_RULE_VERSION,
)

from .strings.match_publication import (
    BETTING_CLOSE_ODDS_SECTION_TITLES,
    DIRECTION_LABELS,
    GRADE_LABELS,
    LAYOUT_LABELS,
    OPENING_ENTRY_SECTION_TITLE,
    OPENING_ODDS_SECTION_TITLE,
    RESULT_ODDS_SECTION_TITLE,
    RESULT_SECTION_TITLE,
    SEASON_LABELS,
    SURFACE_LABELS,
    TIME_LABELS,
    TRACK_LABELS,
    WEATHER_LABELS,
    betting_close_header_lines,
    betting_close_selection_line,
    odds_market_summary_title,
    odds_refresh_header_lines,
    odds_refresh_market_title,
    odds_refresh_match_header_lines,
    odds_refresh_selection_line,
    opening_entry_line,
    opening_header_lines,
    opening_market_line,
    pagination_footer,
    refund_page,
    result_line,
    settled_header_lines,
    settled_odds_line,
    settlement_voided_page,
    uniform_odds_line,
)

_PAGE_LIMIT: Final = 1900
_FOOTER_RESERVE: Final = 48
_COMBINATION_SUMMARY_LIMIT: Final = 10
_RATING_DISPLAY_QUANTUM: Final = Decimal("0.0001")
_BET_TYPE_ORDER: Final = ("win", "quinella", "trio")
_SUPPORTED_PUBLICATION_TYPES: Final = {
    "match_bet_refund_completed",
    "match_betting_opening",
    "match_settled_result",
    "match_settlement_voided",
    MATCH_ODDS_REFRESH_PUBLICATION_TYPE,
    MATCH_BETTING_CLOSE_PUBLICATION_TYPE,
}


class MatchDiscordPublicationRenderError(ValueError):
    """Stored Match payload cannot be rendered without ambiguity or omission."""


@dataclass(frozen=True, slots=True)
class RenderedMatchDiscordPublication:
    """Bounded presentation pages for one logical Match publication."""

    publication_type: str
    pages: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.publication_type not in _SUPPORTED_PUBLICATION_TYPES:
            raise ValueError("publication_type is unsupported.")
        if not self.pages or any(not page or len(page) > _PAGE_LIMIT for page in self.pages):
            raise ValueError("Rendered publication pages must be non-empty and within the Discord bound.")


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MatchDiscordPublicationRenderError(f"{field_name} must be an object.")
    return value


def _sequence(value: object, *, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise MatchDiscordPublicationRenderError(f"{field_name} must be a non-empty array.")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], *, field_name: str) -> None:
    if set(value) != expected:
        raise MatchDiscordPublicationRenderError(f"{field_name} has unsupported or missing fields.")


def _string(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise MatchDiscordPublicationRenderError(
            f"{field_name} must be a non-empty string no longer than {max_length} characters."
        )
    return value


def _optional_string(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _string(value, field_name=field_name, max_length=max_length)


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MatchDiscordPublicationRenderError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MatchDiscordPublicationRenderError(f"{field_name} must be a non-negative integer.")
    return value


def _enum_string(
    value: object,
    labels: Mapping[str, str],
    *,
    field_name: str,
) -> str:
    normalized = _string(value, field_name=field_name, max_length=32)
    if normalized not in labels:
        raise MatchDiscordPublicationRenderError(f"{field_name} is unsupported.")
    return normalized


def _decimal(value: object, *, field_name: str, places: int) -> Decimal:
    if not isinstance(value, str):
        raise MatchDiscordPublicationRenderError(f"{field_name} must be a decimal string.")
    try:
        converted = Decimal(value)
    except InvalidOperation as exc:
        raise MatchDiscordPublicationRenderError(f"{field_name} must be a decimal string.") from exc
    if not converted.is_finite() or format(converted, f".{places}f") != value:
        raise MatchDiscordPublicationRenderError(f"{field_name} must preserve exactly {places} decimal places.")
    return converted


def _fits_page(common_header: str, body_lines: Sequence[str]) -> bool:
    return len(common_header) + 2 + len("\n".join(body_lines)) + _FOOTER_RESERVE <= _PAGE_LIMIT


def _paginate(
    *,
    header_lines: Sequence[str],
    sections: Sequence[tuple[str, tuple[str, ...]]],
) -> tuple[str, ...]:
    common_header = "\n".join(header_lines)
    body_pages: list[tuple[str, ...]] = []
    current: list[str] = []
    for title, entries in sections:
        if not entries:
            raise MatchDiscordPublicationRenderError("A rendered section must not be empty.")
        first = (title, entries[0])
        if _fits_page(common_header, (*current, *first)):
            current.extend(first)
        else:
            if current:
                body_pages.append(tuple(current))
            current = list(first)
            if not _fits_page(common_header, current):
                raise MatchDiscordPublicationRenderError("One publication Entry exceeds the Discord bound.")
        for entry in entries[1:]:
            if _fits_page(common_header, (*current, entry)):
                current.append(entry)
                continue
            body_pages.append(tuple(current))
            current = [title, entry]
            if not _fits_page(common_header, current):
                raise MatchDiscordPublicationRenderError("One publication Entry exceeds the Discord bound.")
    if current:
        body_pages.append(tuple(current))
    if not body_pages:
        raise MatchDiscordPublicationRenderError("A publication must contain renderable sections.")
    page_count = len(body_pages)
    rendered_pages: list[str] = []
    for index, body in enumerate(body_pages, start=1):
        body_text = "\n".join(body)
        rendered_pages.append(f"{common_header}\n\n{body_text}{pagination_footer(index=index, page_count=page_count)}")
    pages = tuple(rendered_pages)
    if any(len(page) > _PAGE_LIMIT for page in pages):
        raise MatchDiscordPublicationRenderError("Rendered pagination exceeds the Discord bound.")
    return pages


def _paginate_odds_refresh(
    *,
    header_lines: Sequence[str],
    matches: Sequence[tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]],
) -> tuple[str, ...]:
    common_header = "\n".join(header_lines)
    body_pages: list[tuple[str, ...]] = []
    current: list[str] = []

    def start_or_append(lines: tuple[str, ...], *, continuation: tuple[str, ...]) -> None:
        nonlocal current
        if _fits_page(common_header, (*current, *lines)):
            current.extend(lines)
            return
        if current:
            body_pages.append(tuple(current))
        current = list(continuation)
        if not _fits_page(common_header, current):
            raise MatchDiscordPublicationRenderError("One periodic odds selection exceeds the Discord bound.")

    for match_index, (match_header, markets) in enumerate(matches):
        prefix = ("",) if current and match_index > 0 else ()
        start_or_append((*prefix, *match_header), continuation=match_header)
        for market_title, selections in markets:
            if not selections:
                raise MatchDiscordPublicationRenderError("A periodic odds market must not be empty.")
            first = (market_title, selections[0])
            start_or_append(first, continuation=(*match_header, *first))
            for selection in selections[1:]:
                start_or_append((selection,), continuation=(*match_header, market_title, selection))

    if current:
        body_pages.append(tuple(current))
    if not body_pages:
        raise MatchDiscordPublicationRenderError("A periodic odds publication must contain Matches.")
    page_count = len(body_pages)
    rendered_pages: list[str] = []
    for index, body in enumerate(body_pages, start=1):
        body_text = "\n".join(body)
        rendered_pages.append(f"{common_header}\n\n{body_text}{pagination_footer(index=index, page_count=page_count)}")
    pages = tuple(rendered_pages)
    if any(len(page) > _PAGE_LIMIT for page in pages):
        raise MatchDiscordPublicationRenderError("Rendered periodic odds pagination exceeds the Discord bound.")
    return pages


def _present_odds_market(
    *,
    title: str,
    bet_type: str,
    selections: Sequence[tuple[tuple[int, ...], Decimal]],
    decimal_places: int,
    selection_line: Callable[[Sequence[int], Decimal], str],
) -> tuple[str, tuple[str, ...]]:
    unique_odds = {odds for _, odds in selections}
    if len(unique_odds) == 1:
        return title, (
            uniform_odds_line(
                selection_count=len(selections),
                odds=next(iter(unique_odds)),
                decimal_places=decimal_places,
            ),
        )
    ordered = tuple(sorted(selections, key=lambda item: (item[1], item[0])))
    shown = ordered if bet_type == "win" else ordered[:_COMBINATION_SUMMARY_LIMIT]
    return (
        odds_market_summary_title(title=title, shown_count=len(shown), total_count=len(ordered)),
        tuple(selection_line(entry_numbers, odds) for entry_numbers, odds in shown),
    )


def _render_match_odds_refresh_publication(
    payload: Mapping[str, object],
) -> RenderedMatchDiscordPublication:
    _exact_keys(
        payload,
        {"schema_version", "publication_type", "coverage", "odds", "matches"},
        field_name="payload_json",
    )
    if payload.get("schema_version") != MATCH_ODDS_REFRESH_PAYLOAD_SCHEMA_VERSION:
        raise MatchDiscordPublicationRenderError("Unsupported periodic Match odds payload schema version.")

    coverage = _mapping(payload.get("coverage"), field_name="coverage")
    _exact_keys(coverage, {"mode", "cadence_seconds", "generated_at"}, field_name="coverage")
    mode = _string(coverage.get("mode"), field_name="coverage.mode", max_length=16)
    try:
        refresh_mode = MatchOddsRefreshMode(mode)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("coverage.mode is unsupported.") from exc
    cadence_seconds = _positive_int(coverage.get("cadence_seconds"), field_name="coverage.cadence_seconds")
    if cadence_seconds != int(refresh_mode.interval.total_seconds()):
        raise MatchDiscordPublicationRenderError("coverage cadence does not match mode.")
    generated_text = _string(coverage.get("generated_at"), field_name="coverage.generated_at", max_length=64)
    try:
        generated_at = datetime.fromisoformat(generated_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("coverage.generated_at must be ISO-8601.") from exc
    if generated_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("coverage.generated_at must be timezone-aware.")

    odds = _mapping(payload.get("odds"), field_name="odds")
    _exact_keys(odds, {"rule_version", "provisional_precision"}, field_name="odds")
    if _string(odds.get("rule_version"), field_name="odds.rule_version", max_length=64) != WEIGHTED_ODDS_RULE_VERSION:
        raise MatchDiscordPublicationRenderError("Stored periodic odds rule version is unsupported.")
    if (
        _decimal(
            odds.get("provisional_precision"),
            field_name="odds.provisional_precision",
            places=4,
        )
        != PROVISIONAL_ODDS_QUANTUM
    ):
        raise MatchDiscordPublicationRenderError("Stored periodic odds precision is unsupported.")

    raw_matches = _sequence(payload.get("matches"), field_name="matches")
    rendered_matches: list[tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]] = []
    match_order: list[tuple[datetime, int]] = []
    seen_match_ids: set[int] = set()
    cardinality = {"win": 1, "quinella": 2, "trio": 3}
    for match_index, raw_match in enumerate(raw_matches):
        match = _mapping(raw_match, field_name=f"matches[{match_index}]")
        _exact_keys(
            match,
            {"match_id", "name", "grade", "scheduled_at", "markets"},
            field_name=f"matches[{match_index}]",
        )
        match_id = _positive_int(match.get("match_id"), field_name=f"matches[{match_index}].match_id")
        if match_id in seen_match_ids:
            raise MatchDiscordPublicationRenderError("Periodic odds Match IDs must be unique.")
        seen_match_ids.add(match_id)
        match_name = _string(match.get("name"), field_name=f"matches[{match_index}].name", max_length=200)
        grade = _enum_string(match.get("grade"), GRADE_LABELS, field_name=f"matches[{match_index}].grade")
        scheduled_text = _string(
            match.get("scheduled_at"),
            field_name=f"matches[{match_index}].scheduled_at",
            max_length=64,
        )
        try:
            scheduled_at = datetime.fromisoformat(scheduled_text)
        except ValueError as exc:
            raise MatchDiscordPublicationRenderError("Periodic Match schedule must be ISO-8601.") from exc
        if scheduled_at.tzinfo is None:
            raise MatchDiscordPublicationRenderError("Periodic Match schedule must be timezone-aware.")
        match_order.append((scheduled_at, match_id))

        raw_markets = _sequence(match.get("markets"), field_name=f"matches[{match_index}].markets")
        market_presentations: list[tuple[str, tuple[str, ...]]] = []
        win_numbers: tuple[int, ...] | None = None
        seen_types: list[str] = []
        parsed_by_type: dict[str, tuple[tuple[int, ...], ...]] = {}
        for market_index, raw_market in enumerate(raw_markets):
            market = _mapping(raw_market, field_name=f"matches[{match_index}].markets[{market_index}]")
            _exact_keys(
                market,
                {"bet_type", "selections"},
                field_name=f"matches[{match_index}].markets[{market_index}]",
            )
            bet_type = _string(
                market.get("bet_type"),
                field_name=f"matches[{match_index}].markets[{market_index}].bet_type",
                max_length=16,
            )
            if bet_type not in cardinality or bet_type in seen_types:
                raise MatchDiscordPublicationRenderError("Periodic odds market type is unsupported or duplicated.")
            seen_types.append(bet_type)
            raw_selections = _sequence(
                market.get("selections"),
                field_name=f"matches[{match_index}].markets[{market_index}].selections",
            )
            parsed_selections: list[tuple[int, ...]] = []
            selection_odds: list[tuple[tuple[int, ...], Decimal]] = []
            for selection_index, raw_selection in enumerate(raw_selections):
                selection = _mapping(
                    raw_selection,
                    field_name=f"matches[{match_index}].markets[{market_index}].selections[{selection_index}]",
                )
                _exact_keys(
                    selection,
                    {"entry_numbers", "provisional_odds"},
                    field_name=(f"matches[{match_index}].markets[{market_index}].selections[{selection_index}]"),
                )
                numbers = tuple(
                    _positive_int(value, field_name="entry_number")
                    for value in _sequence(selection.get("entry_numbers"), field_name="entry_numbers")
                )
                if len(numbers) != cardinality[bet_type] or numbers != tuple(sorted(numbers)):
                    raise MatchDiscordPublicationRenderError("Periodic odds selection is not canonical.")
                provisional_odds = _decimal(
                    selection.get("provisional_odds"),
                    field_name="provisional_odds",
                    places=4,
                )
                if provisional_odds <= 0:
                    raise MatchDiscordPublicationRenderError("Periodic provisional odds must be positive.")
                parsed_selections.append(numbers)
                selection_odds.append((numbers, provisional_odds))
            parsed_by_type[bet_type] = tuple(parsed_selections)
            market_presentations.append(
                _present_odds_market(
                    title=odds_refresh_market_title(bet_type=bet_type),
                    bet_type=bet_type,
                    selections=selection_odds,
                    decimal_places=4,
                    selection_line=lambda entry_numbers, value: odds_refresh_selection_line(
                        entry_numbers=entry_numbers,
                        provisional_odds=value,
                    ),
                )
            )
            if bet_type == "win":
                win_numbers = tuple(numbers[0] for numbers in parsed_selections)

        if win_numbers is None or win_numbers != tuple(range(1, len(win_numbers) + 1)):
            raise MatchDiscordPublicationRenderError("Periodic Win selections must define contiguous Entries.")
        expected_types = _BET_TYPE_ORDER[: min(len(win_numbers), len(_BET_TYPE_ORDER))]
        if tuple(seen_types) != expected_types:
            raise MatchDiscordPublicationRenderError("Periodic odds markets are incomplete or out of order.")
        for bet_type in expected_types:
            expected_selections = tuple(combinations(win_numbers, cardinality[bet_type]))
            if parsed_by_type[bet_type] != expected_selections:
                raise MatchDiscordPublicationRenderError("Periodic odds combinations are incomplete or out of order.")

        rendered_matches.append(
            (
                odds_refresh_match_header_lines(
                    match_name=match_name,
                    grade=grade,
                    scheduled_at=scheduled_at,
                ),
                tuple(market_presentations),
            )
        )

    if match_order != sorted(match_order):
        raise MatchDiscordPublicationRenderError("Periodic odds Matches are out of stable order.")
    return RenderedMatchDiscordPublication(
        publication_type=MATCH_ODDS_REFRESH_PUBLICATION_TYPE,
        pages=_paginate_odds_refresh(
            header_lines=odds_refresh_header_lines(mode=refresh_mode.value, generated_at=generated_at),
            matches=tuple(rendered_matches),
        ),
    )


def _render_match_betting_close_publication(
    payload: Mapping[str, object],
) -> RenderedMatchDiscordPublication:
    _exact_keys(
        payload,
        {"schema_version", "publication_type", "match", "odds"},
        field_name="payload_json",
    )
    if payload.get("schema_version") != MATCH_BETTING_CLOSE_PAYLOAD_SCHEMA_VERSION:
        raise MatchDiscordPublicationRenderError("Unsupported Match betting-close payload schema version.")

    match = _mapping(payload.get("match"), field_name="match")
    _exact_keys(match, {"id", "name", "grade", "scheduled_at", "closed_at"}, field_name="match")
    _positive_int(match.get("id"), field_name="match.id")
    match_name = _string(match.get("name"), field_name="match.name", max_length=200)
    grade = _enum_string(match.get("grade"), GRADE_LABELS, field_name="match.grade")
    scheduled_text = _string(match.get("scheduled_at"), field_name="match.scheduled_at", max_length=64)
    closed_text = _string(match.get("closed_at"), field_name="match.closed_at", max_length=64)
    try:
        scheduled_at = datetime.fromisoformat(scheduled_text)
        closed_at = datetime.fromisoformat(closed_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("Match betting-close times must be ISO-8601.") from exc
    if scheduled_at.tzinfo is None or closed_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("Match betting-close times must be timezone-aware.")

    odds = _mapping(payload.get("odds"), field_name="odds")
    _exact_keys(
        odds,
        {"rule_version", "provisional_precision", "applied_precision", "field_multiplier", "markets"},
        field_name="odds",
    )
    if _string(odds.get("rule_version"), field_name="odds.rule_version", max_length=64) != WEIGHTED_ODDS_RULE_VERSION:
        raise MatchDiscordPublicationRenderError("Stored betting-close odds rule version is unsupported.")
    if (
        _decimal(
            odds.get("provisional_precision"),
            field_name="odds.provisional_precision",
            places=4,
        )
        != PROVISIONAL_ODDS_QUANTUM
    ):
        raise MatchDiscordPublicationRenderError("Stored betting-close provisional precision is unsupported.")
    if (
        _decimal(
            odds.get("applied_precision"),
            field_name="odds.applied_precision",
            places=1,
        )
        != APPLIED_ODDS_QUANTUM
    ):
        raise MatchDiscordPublicationRenderError("Stored betting-close applied precision is unsupported.")

    raw_markets = _sequence(odds.get("markets"), field_name="odds.markets")
    cardinality = {"win": 1, "quinella": 2, "trio": 3}
    seen_types: list[str] = []
    parsed_by_type: dict[str, tuple[tuple[int, ...], ...]] = {}
    presentations: list[tuple[str, tuple[str, ...]]] = []
    win_numbers: tuple[int, ...] | None = None
    for market_index, raw_market in enumerate(raw_markets):
        market = _mapping(raw_market, field_name=f"odds.markets[{market_index}]")
        _exact_keys(market, {"bet_type", "selections"}, field_name=f"odds.markets[{market_index}]")
        bet_type = _string(
            market.get("bet_type"),
            field_name=f"odds.markets[{market_index}].bet_type",
            max_length=16,
        )
        if bet_type not in cardinality or bet_type in seen_types:
            raise MatchDiscordPublicationRenderError("Betting-close market type is unsupported or duplicated.")
        seen_types.append(bet_type)
        raw_selections = _sequence(
            market.get("selections"),
            field_name=f"odds.markets[{market_index}].selections",
        )
        parsed_selections: list[tuple[int, ...]] = []
        selection_odds: list[tuple[tuple[int, ...], Decimal]] = []
        for selection_index, raw_selection in enumerate(raw_selections):
            selection = _mapping(
                raw_selection,
                field_name=f"odds.markets[{market_index}].selections[{selection_index}]",
            )
            _exact_keys(
                selection,
                {"entry_numbers", "confirmed_odds"},
                field_name=f"odds.markets[{market_index}].selections[{selection_index}]",
            )
            numbers = tuple(
                _positive_int(value, field_name="entry_number")
                for value in _sequence(selection.get("entry_numbers"), field_name="entry_numbers")
            )
            if len(numbers) != cardinality[bet_type] or numbers != tuple(sorted(numbers)):
                raise MatchDiscordPublicationRenderError("Betting-close selection is not canonical.")
            confirmed_odds = _decimal(
                selection.get("confirmed_odds"),
                field_name="confirmed_odds",
                places=1,
            )
            if confirmed_odds <= 0:
                raise MatchDiscordPublicationRenderError("Betting-close confirmed odds must be positive.")
            parsed_selections.append(numbers)
            selection_odds.append((numbers, confirmed_odds))
        parsed_by_type[bet_type] = tuple(parsed_selections)
        presentations.append(
            _present_odds_market(
                title=BETTING_CLOSE_ODDS_SECTION_TITLES[bet_type],
                bet_type=bet_type,
                selections=selection_odds,
                decimal_places=1,
                selection_line=lambda entry_numbers, value: betting_close_selection_line(
                    entry_numbers=entry_numbers,
                    confirmed_odds=value,
                ),
            )
        )
        if bet_type == "win":
            win_numbers = tuple(numbers[0] for numbers in parsed_selections)

    if win_numbers is None or win_numbers != tuple(range(1, len(win_numbers) + 1)):
        raise MatchDiscordPublicationRenderError("Betting-close Win selections must define contiguous Entries.")
    expected_types = _BET_TYPE_ORDER[: min(len(win_numbers), len(_BET_TYPE_ORDER))]
    if tuple(seen_types) != expected_types:
        raise MatchDiscordPublicationRenderError("Betting-close markets are incomplete or out of order.")
    for bet_type in expected_types:
        expected_selections = tuple(combinations(win_numbers, cardinality[bet_type]))
        if parsed_by_type[bet_type] != expected_selections:
            raise MatchDiscordPublicationRenderError("Betting-close combinations are incomplete or out of order.")
    expected_multiplier = Decimal("0.5") if len(win_numbers) < 9 else Decimal("1.0")
    if (
        _decimal(
            odds.get("field_multiplier"),
            field_name="odds.field_multiplier",
            places=1,
        )
        != expected_multiplier
    ):
        raise MatchDiscordPublicationRenderError("Stored betting-close field multiplier is inconsistent.")

    return RenderedMatchDiscordPublication(
        publication_type=MATCH_BETTING_CLOSE_PUBLICATION_TYPE,
        pages=_paginate(
            header_lines=betting_close_header_lines(
                match_name=match_name,
                grade=grade,
                scheduled_at=scheduled_at,
                closed_at=closed_at,
            ),
            sections=tuple(presentations),
        ),
    )


def _render_match_opening_publication(
    payload: Mapping[str, object],
) -> RenderedMatchDiscordPublication:
    _exact_keys(
        payload,
        {"schema_version", "publication_type", "match", "course", "condition", "entries", "odds"},
        field_name="payload_json",
    )

    match = _mapping(payload.get("match"), field_name="match")
    _exact_keys(match, {"id", "name", "description", "grade", "scheduled_at"}, field_name="match")
    _positive_int(match.get("id"), field_name="match.id")
    match_name = _string(match.get("name"), field_name="match.name", max_length=200)
    description = _optional_string(match.get("description"), field_name="match.description", max_length=4000)
    grade = _enum_string(match.get("grade"), GRADE_LABELS, field_name="match.grade")
    scheduled_text = _string(match.get("scheduled_at"), field_name="match.scheduled_at", max_length=64)
    try:
        scheduled_at = datetime.fromisoformat(scheduled_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be ISO-8601.") from exc
    if scheduled_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be timezone-aware.")

    course = _mapping(payload.get("course"), field_name="course")
    _exact_keys(
        course,
        {"course_id", "stadium_id", "stadium_name", "surface", "distance", "direction", "layout"},
        field_name="course",
    )
    _positive_int(course.get("course_id"), field_name="course.course_id")
    _positive_int(course.get("stadium_id"), field_name="course.stadium_id")
    stadium_name = _string(course.get("stadium_name"), field_name="course.stadium_name", max_length=100)
    surface = _enum_string(course.get("surface"), SURFACE_LABELS, field_name="course.surface")
    distance = _positive_int(course.get("distance"), field_name="course.distance")
    direction = _enum_string(course.get("direction"), DIRECTION_LABELS, field_name="course.direction")
    layout = _enum_string(course.get("layout"), LAYOUT_LABELS, field_name="course.layout")

    condition = _mapping(payload.get("condition"), field_name="condition")
    _exact_keys(condition, {"season", "weather", "time_of_day", "track_condition"}, field_name="condition")
    season = _enum_string(condition.get("season"), SEASON_LABELS, field_name="condition.season")
    weather = _enum_string(condition.get("weather"), WEATHER_LABELS, field_name="condition.weather")
    time_of_day = _enum_string(condition.get("time_of_day"), TIME_LABELS, field_name="condition.time_of_day")
    track = _enum_string(
        condition.get("track_condition"),
        TRACK_LABELS,
        field_name="condition.track_condition",
    )

    entry_rows = _sequence(payload.get("entries"), field_name="entries")
    entry_lines: list[str] = []
    seen_entry_ids: set[int] = set()
    for index, raw_entry in enumerate(entry_rows):
        entry = _mapping(raw_entry, field_name=f"entries[{index}]")
        _exact_keys(
            entry,
            {"entry_id", "entry_number", "game_account_name", "horse_name", "affiliation"},
            field_name=f"entries[{index}]",
        )
        entry_id = _positive_int(entry.get("entry_id"), field_name=f"entries[{index}].entry_id")
        entry_number = _positive_int(entry.get("entry_number"), field_name=f"entries[{index}].entry_number")
        if entry_id in seen_entry_ids or entry_number != index + 1:
            raise MatchDiscordPublicationRenderError(
                "Opening Entries must preserve unique IDs and contiguous Entry numbers."
            )
        seen_entry_ids.add(entry_id)
        game_account_name = _string(
            entry.get("game_account_name"),
            field_name=f"entries[{index}].game_account_name",
            max_length=100,
        )
        horse_name = _string(
            entry.get("horse_name"),
            field_name=f"entries[{index}].horse_name",
            max_length=100,
        )
        affiliation = _optional_string(
            entry.get("affiliation"),
            field_name=f"entries[{index}].affiliation",
            max_length=100,
        )
        entry_lines.append(
            opening_entry_line(
                entry_number=entry_number,
                game_account_name=game_account_name,
                horse_name=horse_name,
                affiliation=affiliation,
            )
        )

    odds = _mapping(payload.get("odds"), field_name="odds")
    _exact_keys(
        odds,
        {"rule_version", "provisional_precision", "zero_pool", "markets"},
        field_name="odds",
    )
    rule_version = _string(odds.get("rule_version"), field_name="odds.rule_version", max_length=64)
    if rule_version != WEIGHTED_ODDS_RULE_VERSION:
        raise MatchDiscordPublicationRenderError("Stored opening odds rule version is unsupported.")
    precision = _decimal(
        odds.get("provisional_precision"),
        field_name="odds.provisional_precision",
        places=4,
    )
    if precision != PROVISIONAL_ODDS_QUANTUM:
        raise MatchDiscordPublicationRenderError("Stored opening odds precision is unsupported.")
    if odds.get("zero_pool") is not True:
        raise MatchDiscordPublicationRenderError("Stored opening odds must be the canonical zero-pool projection.")

    market_rows = _sequence(odds.get("markets"), field_name="odds.markets")
    expected_markets = build_zero_pool_opening_markets(len(entry_rows))
    if len(market_rows) != len(expected_markets):
        raise MatchDiscordPublicationRenderError("Stored opening odds markets are incomplete.")
    odds_lines: list[str] = []
    for index, (raw_market, expected_market) in enumerate(zip(market_rows, expected_markets, strict=True)):
        market = _mapping(raw_market, field_name=f"odds.markets[{index}]")
        _exact_keys(
            market,
            {"bet_type", "available", "selection_count", "uniform_odds"},
            field_name=f"odds.markets[{index}]",
        )
        bet_type = _string(market.get("bet_type"), field_name=f"odds.markets[{index}].bet_type", max_length=16)
        available = market.get("available")
        if not isinstance(available, bool):
            raise MatchDiscordPublicationRenderError(f"odds.markets[{index}].available must be a boolean.")
        selection_count = _non_negative_int(
            market.get("selection_count"),
            field_name=f"odds.markets[{index}].selection_count",
        )
        raw_uniform_odds = market.get("uniform_odds")
        uniform_odds = (
            None
            if raw_uniform_odds is None
            else _decimal(
                raw_uniform_odds,
                field_name=f"odds.markets[{index}].uniform_odds",
                places=4,
            )
        )
        if (
            bet_type != expected_market.bet_type.value
            or available != expected_market.available
            or selection_count != expected_market.selection_count
            or uniform_odds != expected_market.uniform_odds
        ):
            raise MatchDiscordPublicationRenderError("Stored opening odds do not match the canonical projection.")
        odds_lines.append(
            opening_market_line(
                bet_type=bet_type,
                available=available,
                uniform_odds=uniform_odds,
                selection_count=selection_count,
            )
        )

    normalized_description = None
    if description is not None:
        normalized_description = " ".join(description.split())
        if not normalized_description:
            raise MatchDiscordPublicationRenderError("match.description must not be blank.")
    header_lines = opening_header_lines(
        match_name=match_name,
        grade=grade,
        scheduled_at=scheduled_at,
        stadium_name=stadium_name,
        surface=surface,
        distance=distance,
        direction=direction,
        layout=layout,
        season=season,
        weather=weather,
        time_of_day=time_of_day,
        track=track,
        description=normalized_description,
    )

    return RenderedMatchDiscordPublication(
        publication_type="match_betting_opening",
        pages=_paginate(
            header_lines=tuple(header_lines),
            sections=(
                (OPENING_ENTRY_SECTION_TITLE, tuple(entry_lines)),
                (OPENING_ODDS_SECTION_TITLE, tuple(odds_lines)),
            ),
        ),
    )


def _render_match_refund_publication(
    payload: Mapping[str, object],
) -> RenderedMatchDiscordPublication:
    _exact_keys(
        payload,
        {"schema_version", "publication_type", "match", "refund"},
        field_name="payload_json",
    )
    match = _mapping(payload.get("match"), field_name="match")
    _exact_keys(match, {"name", "grade", "scheduled_at"}, field_name="match")
    match_name = _string(match.get("name"), field_name="match.name", max_length=200)
    grade = _enum_string(match.get("grade"), GRADE_LABELS, field_name="match.grade")
    scheduled_text = _string(match.get("scheduled_at"), field_name="match.scheduled_at", max_length=64)
    try:
        scheduled_at = datetime.fromisoformat(scheduled_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be ISO-8601.") from exc
    if scheduled_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be timezone-aware.")

    refund = _mapping(payload.get("refund"), field_name="refund")
    _exact_keys(refund, {"completed_at", "full_original_stake", "reason"}, field_name="refund")
    completed_text = _string(refund.get("completed_at"), field_name="refund.completed_at", max_length=64)
    try:
        completed_at = datetime.fromisoformat(completed_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("refund.completed_at must be ISO-8601.") from exc
    if completed_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("refund.completed_at must be timezone-aware.")
    if refund.get("full_original_stake") is not True:
        raise MatchDiscordPublicationRenderError("refund.full_original_stake must be true.")
    reason = _optional_string(refund.get("reason"), field_name="refund.reason", max_length=255)

    return RenderedMatchDiscordPublication(
        publication_type="match_bet_refund_completed",
        pages=(
            refund_page(
                match_name=match_name,
                grade=grade,
                scheduled_at=scheduled_at,
                completed_at=completed_at,
                reason=reason,
            ),
        ),
    )


def _render_match_settlement_voided_publication(
    payload: Mapping[str, object],
) -> RenderedMatchDiscordPublication:
    _exact_keys(
        payload,
        {"schema_version", "publication_type", "match", "rollback"},
        field_name="payload_json",
    )
    match = _mapping(payload.get("match"), field_name="match")
    _exact_keys(match, {"name", "grade", "scheduled_at"}, field_name="match")
    match_name = _string(match.get("name"), field_name="match.name", max_length=200)
    grade = _enum_string(match.get("grade"), GRADE_LABELS, field_name="match.grade")
    scheduled_text = _string(match.get("scheduled_at"), field_name="match.scheduled_at", max_length=64)
    try:
        scheduled_at = datetime.fromisoformat(scheduled_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be ISO-8601.") from exc
    if scheduled_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be timezone-aware.")

    rollback = _mapping(payload.get("rollback"), field_name="rollback")
    _exact_keys(
        rollback,
        {"completed_at", "reason", "prior_settlement_voided", "compensation_completed"},
        field_name="rollback",
    )
    completed_text = _string(rollback.get("completed_at"), field_name="rollback.completed_at", max_length=64)
    try:
        completed_at = datetime.fromisoformat(completed_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("rollback.completed_at must be ISO-8601.") from exc
    if completed_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("rollback.completed_at must be timezone-aware.")
    reason = _string(rollback.get("reason"), field_name="rollback.reason", max_length=255)
    if rollback.get("prior_settlement_voided") is not True:
        raise MatchDiscordPublicationRenderError("rollback.prior_settlement_voided must be true.")
    if rollback.get("compensation_completed") is not True:
        raise MatchDiscordPublicationRenderError("rollback.compensation_completed must be true.")

    return RenderedMatchDiscordPublication(
        publication_type="match_settlement_voided",
        pages=(
            settlement_voided_page(
                match_name=match_name,
                grade=grade,
                scheduled_at=scheduled_at,
                completed_at=completed_at,
                reason=reason,
            ),
        ),
    )


def render_match_discord_publication(
    payload_json: Mapping[str, object],
) -> RenderedMatchDiscordPublication:
    """Render a supported stored snapshot without current Match/Rating/Bet reads."""

    payload = _mapping(payload_json, field_name="payload_json")
    publication_type = _string(payload.get("publication_type"), field_name="publication_type", max_length=64)
    if publication_type == MATCH_ODDS_REFRESH_PUBLICATION_TYPE:
        return _render_match_odds_refresh_publication(payload)
    if publication_type == MATCH_BETTING_CLOSE_PUBLICATION_TYPE:
        return _render_match_betting_close_publication(payload)
    schema_version = payload.get("schema_version")
    if publication_type == "match_settled_result":
        if schema_version not in (1, MATCH_RESULT_PUBLICATION_PAYLOAD_SCHEMA_VERSION):
            raise MatchDiscordPublicationRenderError("Unsupported Match result publication schema version.")
    elif schema_version != MATCH_PUBLICATION_PAYLOAD_SCHEMA_VERSION:
        raise MatchDiscordPublicationRenderError("Unsupported Match publication schema version.")
    if publication_type == "match_betting_opening":
        return _render_match_opening_publication(payload)
    if publication_type == "match_bet_refund_completed":
        return _render_match_refund_publication(payload)
    if publication_type == "match_settlement_voided":
        return _render_match_settlement_voided_publication(payload)
    if publication_type != "match_settled_result":
        raise MatchDiscordPublicationRenderError("Unsupported Match publication type.")
    _exact_keys(
        payload,
        {"schema_version", "publication_type", "match", "course", "condition", "results", "odds"},
        field_name="payload_json",
    )

    match = _mapping(payload.get("match"), field_name="match")
    _exact_keys(match, {"name", "grade", "scheduled_at"}, field_name="match")
    match_name = _string(match.get("name"), field_name="match.name", max_length=200)
    grade = _enum_string(match.get("grade"), GRADE_LABELS, field_name="match.grade")
    scheduled_text = _string(match.get("scheduled_at"), field_name="match.scheduled_at", max_length=64)
    try:
        scheduled_at = datetime.fromisoformat(scheduled_text)
    except ValueError as exc:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be ISO-8601.") from exc
    if scheduled_at.tzinfo is None:
        raise MatchDiscordPublicationRenderError("match.scheduled_at must be timezone-aware.")

    course = _mapping(payload.get("course"), field_name="course")
    _exact_keys(course, {"stadium_name", "surface", "distance", "direction", "layout"}, field_name="course")
    stadium_name = _string(course.get("stadium_name"), field_name="course.stadium_name", max_length=100)
    surface = _enum_string(course.get("surface"), SURFACE_LABELS, field_name="course.surface")
    distance = _positive_int(course.get("distance"), field_name="course.distance")
    direction = _enum_string(course.get("direction"), DIRECTION_LABELS, field_name="course.direction")
    layout = _enum_string(course.get("layout"), LAYOUT_LABELS, field_name="course.layout")

    condition = _mapping(payload.get("condition"), field_name="condition")
    _exact_keys(condition, {"season", "weather", "time_of_day", "track_condition"}, field_name="condition")
    season = _enum_string(condition.get("season"), SEASON_LABELS, field_name="condition.season")
    weather = _enum_string(condition.get("weather"), WEATHER_LABELS, field_name="condition.weather")
    time_of_day = _enum_string(condition.get("time_of_day"), TIME_LABELS, field_name="condition.time_of_day")
    track = _enum_string(
        condition.get("track_condition"),
        TRACK_LABELS,
        field_name="condition.track_condition",
    )

    result_rows = _sequence(payload.get("results"), field_name="results")
    result_lines: list[str] = []
    ranked_entry_numbers: list[int] = []
    rating_ranks: list[int] = []
    seen_entry_numbers: set[int] = set()
    for index, raw_result in enumerate(result_rows):
        result = _mapping(raw_result, field_name=f"results[{index}]")
        if schema_version == MATCH_RESULT_PUBLICATION_PAYLOAD_SCHEMA_VERSION:
            _exact_keys(
                result,
                {
                    "entry_number",
                    "official_rank",
                    "player_name",
                    "character_name",
                    "affiliation",
                    "rating_disposition",
                    "rating_rank",
                    "rating",
                },
                field_name=f"results[{index}]",
            )
        else:
            _exact_keys(
                result,
                {"entry_number", "official_rank", "player_name", "character_name", "affiliation", "rating"},
                field_name=f"results[{index}]",
            )
        entry_number = _positive_int(result.get("entry_number"), field_name=f"results[{index}].entry_number")
        rank = _positive_int(result.get("official_rank"), field_name=f"results[{index}].official_rank")
        if rank != index + 1 or entry_number in seen_entry_numbers:
            raise MatchDiscordPublicationRenderError("Results must preserve contiguous ranks and unique Entry numbers.")
        seen_entry_numbers.add(entry_number)
        ranked_entry_numbers.append(entry_number)
        player_name = _string(result.get("player_name"), field_name=f"results[{index}].player_name", max_length=100)
        character_name = _string(
            result.get("character_name"),
            field_name=f"results[{index}].character_name",
            max_length=100,
        )
        affiliation = _optional_string(
            result.get("affiliation"),
            field_name=f"results[{index}].affiliation",
            max_length=100,
        )
        rating = _mapping(result.get("rating"), field_name=f"results[{index}].rating")
        _exact_keys(rating, {"before", "delta", "after"}, field_name=f"results[{index}].rating")
        before = _decimal(rating.get("before"), field_name=f"results[{index}].rating.before", places=4)
        delta = _decimal(rating.get("delta"), field_name=f"results[{index}].rating.delta", places=4)
        after = _decimal(rating.get("after"), field_name=f"results[{index}].rating.after", places=4)
        if before < 0 or after < 0 or abs(before + delta - after) > _RATING_DISPLAY_QUANTUM:
            raise MatchDiscordPublicationRenderError("Stored Rating transition is inconsistent.")
        rating_disposition: str | None = None
        rating_rank: int | None = None
        if schema_version == MATCH_RESULT_PUBLICATION_PAYLOAD_SCHEMA_VERSION:
            rating_disposition = _enum_string(
                result.get("rating_disposition"),
                {
                    "rated": "rated",
                    "excluded": "excluded",
                    "not_applicable": "not_applicable",
                },
                field_name=f"results[{index}].rating_disposition",
            )
            raw_rating_rank = result.get("rating_rank")
            if rating_disposition == "rated":
                rating_rank = _positive_int(raw_rating_rank, field_name=f"results[{index}].rating_rank")
                rating_ranks.append(rating_rank)
            elif raw_rating_rank is not None or delta != 0:
                raise MatchDiscordPublicationRenderError("A non-rated result cannot have a Rating rank or delta.")
        result_lines.append(
            result_line(
                rank=rank,
                entry_number=entry_number,
                player_name=player_name,
                character_name=character_name,
                affiliation=affiliation,
                before=before,
                delta=delta,
                after=after,
                rating_disposition=rating_disposition,
                rating_rank=rating_rank,
            )
        )

    if rating_ranks and tuple(rating_ranks) != tuple(range(1, len(rating_ranks) + 1)):
        raise MatchDiscordPublicationRenderError("Rated results must preserve contiguous derived ranks.")

    odds = _mapping(payload.get("odds"), field_name="odds")
    _exact_keys(odds, {"markets"}, field_name="odds")
    market_rows = _sequence(odds.get("markets"), field_name="odds.markets")
    expected_types = _BET_TYPE_ORDER[: min(len(result_rows), len(_BET_TYPE_ORDER))]
    odds_lines: list[str] = []
    if len(market_rows) != len(expected_types):
        raise MatchDiscordPublicationRenderError("Stored odds markets are incomplete.")
    for index, raw_market in enumerate(market_rows):
        market = _mapping(raw_market, field_name=f"odds.markets[{index}]")
        _exact_keys(
            market,
            {"bet_type", "winning_selection", "confirmed_odds"},
            field_name=f"odds.markets[{index}]",
        )
        bet_type = _string(market.get("bet_type"), field_name=f"odds.markets[{index}].bet_type", max_length=16)
        if bet_type != expected_types[index]:
            raise MatchDiscordPublicationRenderError("Stored odds market ordering is inconsistent.")
        selection = tuple(
            _positive_int(value, field_name=f"odds.markets[{index}].winning_selection")
            for value in _sequence(
                market.get("winning_selection"),
                field_name=f"odds.markets[{index}].winning_selection",
            )
        )
        expected_selection = tuple(sorted(ranked_entry_numbers[: index + 1]))
        if selection != expected_selection:
            raise MatchDiscordPublicationRenderError("Stored odds selection does not match official ranks.")
        confirmed_odds = _decimal(
            market.get("confirmed_odds"),
            field_name=f"odds.markets[{index}].confirmed_odds",
            places=1,
        )
        if confirmed_odds <= 0:
            raise MatchDiscordPublicationRenderError("confirmed_odds must be positive.")
        odds_lines.append(
            settled_odds_line(
                bet_type=bet_type,
                selection=selection,
                confirmed_odds=confirmed_odds,
            )
        )

    header = settled_header_lines(
        match_name=match_name,
        grade=grade,
        scheduled_at=scheduled_at,
        stadium_name=stadium_name,
        surface=surface,
        distance=distance,
        direction=direction,
        layout=layout,
        season=season,
        weather=weather,
        time_of_day=time_of_day,
        track=track,
    )
    return RenderedMatchDiscordPublication(
        publication_type=publication_type,
        pages=_paginate(
            header_lines=header,
            sections=(
                (RESULT_SECTION_TITLE, tuple(result_lines)),
                (RESULT_ODDS_SECTION_TITLE, tuple(odds_lines)),
            ),
        ),
    )
