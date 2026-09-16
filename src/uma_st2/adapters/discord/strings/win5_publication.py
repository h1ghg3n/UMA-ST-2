"""User-facing copy for stored WIN5 result and Hall-of-Fame pages."""

from __future__ import annotations

from ..common import safe_discord_text

ROUND_TYPE_LABELS = {
    "normal": "일반",
    "special": "특별",
}
TIER_LABELS = {
    "TOP1": "TOP1",
    "TOP3": "TOP3",
    "TOP5": "TOP5",
    "SPECIAL_WINNER": "특별 우승",
}

HIT_SECTION_TITLE = "### 적중자"
HALL_SECTION_TITLE = "### 새 공동 기록"
NO_HITS_LINE = "- 적중자 없음"


def pagination_footer(*, index: int, page_count: int) -> str:
    return f"\n\n페이지 {index}/{page_count}"


def header_lines(
    *,
    publication_type: str,
    season_name: str,
    round_name: str,
    round_type: str,
) -> tuple[str, ...]:
    heading = "## WIN5 결과" if publication_type == "win5_round_result" else "## WIN5 Hall of Fame 갱신"
    return (
        heading,
        f"Season: {safe_discord_text(season_name, limit=100)}",
        f"Round: {safe_discord_text(round_name, limit=100)} · {ROUND_TYPE_LABELS[round_type]}",
    )


def result_section_title(race_name: str) -> str:
    return f"### 결과 · {safe_discord_text(race_name, limit=200)}"


def void_result_line(reason: str) -> str:
    return f"- VOID — {safe_discord_text(reason, limit=255)}"


def placement_line(
    *,
    position: int,
    gate_number: int,
    horse_name: str | None,
) -> str:
    horse = f" — {safe_discord_text(horse_name, limit=100)}" if horse_name is not None else ""
    return f"- {position}착: Gate {gate_number}{horse}"


def hit_line(
    *,
    display_name: str,
    tier: str,
    season_delta: int,
    top1_delta: int,
) -> str:
    return (
        f"- {safe_discord_text(display_name, limit=100)} · {TIER_LABELS[tier]} · "
        f"Season +{season_delta} · TOP1 +{top1_delta}"
    )


def hall_line(display_name: str) -> str:
    return f"- {safe_discord_text(display_name, limit=100)} · TOP5 완전 적중"
