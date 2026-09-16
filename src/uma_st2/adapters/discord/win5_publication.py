"""Pure Discord presentation for stored WIN5 publication payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from uma_st2.application.publication import (
    WIN5_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
    WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
)

from .strings.win5_publication import (
    HALL_SECTION_TITLE,
    HIT_SECTION_TITLE,
    NO_HITS_LINE,
    ROUND_TYPE_LABELS,
    TIER_LABELS,
    hall_line,
    header_lines,
    hit_line,
    pagination_footer,
    placement_line,
    result_section_title,
    void_result_line,
)

_PAGE_LIMIT: Final = 1900
_FOOTER_RESERVE: Final = 48


class Win5DiscordPublicationRenderError(ValueError):
    """Stored payload cannot be rendered without ambiguity or omission."""


@dataclass(frozen=True, slots=True)
class RenderedWin5DiscordPublication:
    """Bounded presentation pages for one logical WIN5 publication."""

    publication_type: str
    pages: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.publication_type not in {
            "win5_round_result",
            "win5_hall_of_fame_update",
        }:
            raise ValueError("publication_type is unsupported.")
        if not self.pages:
            raise ValueError("A rendered publication must contain at least one page.")
        if any(not page or len(page) > _PAGE_LIMIT for page in self.pages):
            raise ValueError("Rendered publication pages must be non-empty and within the Discord bound.")


def _mapping(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Win5DiscordPublicationRenderError(f"{field_name} must be an object.")
    return value


def _sequence(value: object, *, field_name: str, allow_empty: bool = False) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise Win5DiscordPublicationRenderError(f"{field_name} must be an array.")
    if not value and not allow_empty:
        raise Win5DiscordPublicationRenderError(f"{field_name} must not be empty.")
    return value


def _string(value: object, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise Win5DiscordPublicationRenderError(
            f"{field_name} must be a non-empty string no longer than {max_length} characters."
        )
    return value


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Win5DiscordPublicationRenderError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Win5DiscordPublicationRenderError(f"{field_name} must be a non-negative integer.")
    return value


def _optional_string(value: object, *, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _string(value, field_name=field_name, max_length=max_length)


def _fits_page(common_header: str, body_lines: Sequence[str]) -> bool:
    body = "\n".join(body_lines)
    return len(common_header) + 2 + len(body) + _FOOTER_RESERVE <= _PAGE_LIMIT


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
            raise Win5DiscordPublicationRenderError("A rendered publication section must not be empty.")
        first = (title, entries[0])
        if _fits_page(common_header, (*current, *first)):
            current.extend(first)
        else:
            if current:
                body_pages.append(tuple(current))
            current = list(first)
            if not _fits_page(common_header, current):
                raise Win5DiscordPublicationRenderError("One publication entry exceeds the Discord page bound.")

        for entry in entries[1:]:
            if _fits_page(common_header, (*current, entry)):
                current.append(entry)
                continue
            body_pages.append(tuple(current))
            current = [title, entry]
            if not _fits_page(common_header, current):
                raise Win5DiscordPublicationRenderError("One publication entry exceeds the Discord page bound.")

    if current:
        body_pages.append(tuple(current))
    if not body_pages:
        raise Win5DiscordPublicationRenderError("A publication must contain at least one renderable section.")

    page_count = len(body_pages)
    rendered_pages: list[str] = []
    for index, body in enumerate(body_pages, start=1):
        body_text = "\n".join(body)
        rendered_pages.append(f"{common_header}\n\n{body_text}{pagination_footer(index=index, page_count=page_count)}")
    rendered = tuple(rendered_pages)
    if any(len(page) > _PAGE_LIMIT for page in rendered):
        raise Win5DiscordPublicationRenderError("Rendered pagination exceeds the Discord page bound.")
    return rendered


def _identity(payload: Mapping[str, object]) -> tuple[str, str, str]:
    season = _mapping(payload.get("season"), field_name="season")
    round_ = _mapping(payload.get("round"), field_name="round")
    _positive_int(season.get("id"), field_name="season.id")
    _positive_int(round_.get("id"), field_name="round.id")
    season_name = _string(season.get("name"), field_name="season.name", max_length=100)
    round_name = _string(round_.get("name"), field_name="round.name", max_length=100)
    round_type = _string(round_.get("type"), field_name="round.type", max_length=16)
    if round_type not in ROUND_TYPE_LABELS:
        raise Win5DiscordPublicationRenderError("round.type is unsupported.")
    return season_name, round_name, round_type


def _result_sections(
    payload: Mapping[str, object],
    *,
    schema_version: int,
    round_type: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    races = _sequence(payload.get("results"), field_name="results")
    sections: list[tuple[str, tuple[str, ...]]] = []
    seen_race_ids: set[int] = set()
    void_count = 0
    for race_index, raw_race in enumerate(races):
        race = _mapping(raw_race, field_name=f"results[{race_index}]")
        race_id = _positive_int(race.get("race_id"), field_name=f"results[{race_index}].race_id")
        if race_id in seen_race_ids:
            raise Win5DiscordPublicationRenderError("results Race IDs must be unique.")
        seen_race_ids.add(race_id)
        race_name = _string(
            race.get("race_name"),
            field_name=f"results[{race_index}].race_name",
            max_length=200,
        )
        void_reason = _optional_string(
            race.get("void_reason"),
            field_name=f"results[{race_index}].void_reason",
            max_length=255,
        )
        placements = _sequence(
            race.get("placements"),
            field_name=f"results[{race_index}].placements",
            allow_empty=schema_version == WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
        )
        placement_lines: list[str] = []
        if void_reason is not None:
            if schema_version != WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION or placements:
                raise Win5DiscordPublicationRenderError(
                    "A void publication Race requires payload schema version 2 and empty placements."
                )
            void_count += 1
            placement_lines.append(void_result_line(void_reason))
        elif not placements:
            raise Win5DiscordPublicationRenderError("A non-void publication Race requires Result placements.")
        seen_positions: set[int] = set()
        for placement_index, raw_placement in enumerate(placements):
            placement = _mapping(
                raw_placement,
                field_name=f"results[{race_index}].placements[{placement_index}]",
            )
            position = _positive_int(
                placement.get("position"),
                field_name=f"results[{race_index}].placements[{placement_index}].position",
            )
            if position in seen_positions:
                raise Win5DiscordPublicationRenderError("Result positions must be unique within a Race.")
            seen_positions.add(position)
            _positive_int(
                placement.get("result_id"),
                field_name=f"results[{race_index}].placements[{placement_index}].result_id",
            )
            gate_number = _positive_int(
                placement.get("gate_number"),
                field_name=f"results[{race_index}].placements[{placement_index}].gate_number",
            )
            horse_name = _optional_string(
                placement.get("horse_name"),
                field_name=f"results[{race_index}].placements[{placement_index}].horse_name",
                max_length=100,
            )
            placement_lines.append(
                placement_line(
                    position=position,
                    gate_number=gate_number,
                    horse_name=horse_name,
                )
            )
        sections.append(
            (
                result_section_title(race_name),
                tuple(placement_lines),
            )
        )
    if void_count:
        if round_type != "special":
            raise Win5DiscordPublicationRenderError("Only a Special Round can contain void publication Races.")
        if void_count == len(races):
            raise Win5DiscordPublicationRenderError("An all-void Special Round cannot be a scored publication.")
    elif schema_version == WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION:
        raise Win5DiscordPublicationRenderError("Payload schema version 2 requires a mixed-void Special Round.")
    return tuple(sections)


def _hit_section(payload: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    hits = _sequence(payload.get("hits"), field_name="hits", allow_empty=True)
    no_hits = payload.get("no_hits")
    if not isinstance(no_hits, bool) or no_hits != (len(hits) == 0):
        raise Win5DiscordPublicationRenderError("no_hits must exactly match the hit list.")
    if no_hits:
        return HIT_SECTION_TITLE, (NO_HITS_LINE,)

    lines: list[str] = []
    seen_submission_ids: set[int] = set()
    for index, raw_hit in enumerate(hits):
        hit = _mapping(raw_hit, field_name=f"hits[{index}]")
        submission_id = _positive_int(hit.get("submission_id"), field_name=f"hits[{index}].submission_id")
        if submission_id in seen_submission_ids:
            raise Win5DiscordPublicationRenderError("Hit Submission IDs must be unique.")
        seen_submission_ids.add(submission_id)
        _string(hit.get("persona_id"), field_name=f"hits[{index}].persona_id", max_length=36)
        display_name = _string(
            hit.get("display_name"),
            field_name=f"hits[{index}].display_name",
            max_length=100,
        )
        tier = _string(hit.get("tier"), field_name=f"hits[{index}].tier", max_length=32)
        if tier not in TIER_LABELS:
            raise Win5DiscordPublicationRenderError("Hit tier is unsupported.")
        season_delta = _non_negative_int(
            hit.get("season_score_delta"),
            field_name=f"hits[{index}].season_score_delta",
        )
        if season_delta == 0:
            raise Win5DiscordPublicationRenderError("Published hits must have a positive Season score delta.")
        top1_delta = _non_negative_int(
            hit.get("top1_score_delta"),
            field_name=f"hits[{index}].top1_score_delta",
        )
        lines.append(
            hit_line(
                display_name=display_name,
                tier=tier,
                season_delta=season_delta,
                top1_delta=top1_delta,
            )
        )
    return HIT_SECTION_TITLE, tuple(lines)


def _hall_section(payload: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    records = _sequence(payload.get("records"), field_name="records")
    lines: list[str] = []
    seen_submission_ids: set[int] = set()
    for index, raw_record in enumerate(records):
        record = _mapping(raw_record, field_name=f"records[{index}]")
        submission_id = _positive_int(
            record.get("submission_id"),
            field_name=f"records[{index}].submission_id",
        )
        if submission_id in seen_submission_ids:
            raise Win5DiscordPublicationRenderError("Hall-of-Fame Submission IDs must be unique.")
        seen_submission_ids.add(submission_id)
        _string(record.get("persona_id"), field_name=f"records[{index}].persona_id", max_length=36)
        display_name = _string(
            record.get("display_name"),
            field_name=f"records[{index}].display_name",
            max_length=100,
        )
        tier = _string(record.get("tier"), field_name=f"records[{index}].tier", max_length=32)
        if tier != "TOP5":
            raise Win5DiscordPublicationRenderError("Hall-of-Fame records must use TOP5.")
        lines.append(hall_line(display_name))
    return HALL_SECTION_TITLE, tuple(lines)


def render_win5_discord_publication(
    payload_json: Mapping[str, object],
) -> RenderedWin5DiscordPublication:
    """Render one stored payload without recalculating current WIN5 state."""

    payload = _mapping(payload_json, field_name="payload_json")
    schema_version = _positive_int(payload.get("schema_version"), field_name="schema_version")
    if schema_version not in {
        WIN5_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
        WIN5_VOID_PUBLICATION_PAYLOAD_SCHEMA_VERSION,
    }:
        raise Win5DiscordPublicationRenderError("Unsupported WIN5 publication schema version.")
    publication_type = _string(
        payload.get("publication_type"),
        field_name="publication_type",
        max_length=64,
    )
    if publication_type not in {
        "win5_round_result",
        "win5_hall_of_fame_update",
    }:
        raise Win5DiscordPublicationRenderError("Unsupported WIN5 publication type.")

    season_name, round_name, round_type = _identity(payload)
    publication_header = header_lines(
        publication_type=publication_type,
        season_name=season_name,
        round_name=round_name,
        round_type=round_type,
    )
    if publication_type == "win5_hall_of_fame_update" and schema_version != WIN5_PUBLICATION_PAYLOAD_SCHEMA_VERSION:
        raise Win5DiscordPublicationRenderError("Hall-of-Fame publication requires payload schema version 1.")
    sections = list(
        _result_sections(
            payload,
            schema_version=schema_version,
            round_type=round_type,
        )
    )
    if publication_type == "win5_round_result":
        sections.append(_hit_section(payload))
    else:
        if round_type != "normal":
            raise Win5DiscordPublicationRenderError("Hall-of-Fame updates require a Normal Round.")
        sections.append(_hall_section(payload))

    return RenderedWin5DiscordPublication(
        publication_type=publication_type,
        pages=_paginate(header_lines=publication_header, sections=sections),
    )
