"""User-facing copy for stored Circle Match publication pages."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from ..common import safe_discord_text
from ..datetime_codec import format_discord_datetime

GRADE_LABELS = {
    "G1": "G1",
    "G2": "G2",
    "G3": "G3",
    "LISTED": "Listed",
    "OP": "OP",
}
SURFACE_LABELS = {"turf": "잔디", "dirt": "더트"}
DIRECTION_LABELS = {"left": "좌회전", "right": "우회전", "straight": "직선"}
LAYOUT_LABELS = {
    "standard": "기본",
    "inner": "내측",
    "outer": "외측",
    "outer_to_inner": "외측→내측",
}
SEASON_LABELS = {"spring": "봄", "summer": "여름", "autumn": "가을", "winter": "겨울"}
WEATHER_LABELS = {"random": "랜덤", "sunny": "맑음", "cloudy": "흐림", "rain": "비", "snow": "눈"}
TIME_LABELS = {"day": "낮", "night": "밤"}
TRACK_LABELS = {"random": "랜덤", "firm": "양호", "good": "다소 무거움", "soft": "포화", "heavy": "불량"}
BET_TYPE_LABELS = {"win": "단승", "quinella": "복승", "trio": "삼복승"}

OPENING_ENTRY_SECTION_TITLE = "### Entry"
OPENING_ODDS_SECTION_TITLE = "### 개시 잠정 배당률"
RESULT_SECTION_TITLE = "### 공식 순위 · Rating"
RESULT_ODDS_SECTION_TITLE = "### 최종 배당률"
BETTING_CLOSE_ODDS_SECTION_TITLES = {
    "win": "### 단승 최종 배당률",
    "quinella": "### 복승 최종 배당률",
    "trio": "### 삼복승 최종 배당률",
}
ODDS_REFRESH_MODE_LABELS = {"normal": "NORMAL · 10분", "live": "LIVE · 1분"}


def pagination_footer(*, index: int, page_count: int) -> str:
    return f"\n\n페이지 {index}/{page_count}"


def opening_entry_line(
    *,
    entry_number: int,
    game_account_name: str,
    horse_name: str,
    affiliation: str | None,
) -> str:
    affiliation_text = f" · {safe_discord_text(affiliation, limit=200)}" if affiliation is not None else ""
    return (
        f"- Entry {entry_number} · {safe_discord_text(game_account_name, limit=200)} / "
        f"{safe_discord_text(horse_name, limit=200)}{affiliation_text}"
    )


def opening_market_line(
    *,
    bet_type: str,
    available: bool,
    uniform_odds: Decimal | None,
    selection_count: int,
) -> str:
    if not available:
        return f"- {BET_TYPE_LABELS[bet_type]} · 이용 불가 (Entry 수 부족)"
    return f"- {BET_TYPE_LABELS[bet_type]} · 전체 {selection_count}개 조합 · 잠정 `{uniform_odds:.4f}배`"


def odds_market_summary_title(*, title: str, shown_count: int, total_count: int) -> str:
    if shown_count == total_count:
        return f"{title} · 인기순 전체 {total_count}개"
    return f"{title} · 인기순 {shown_count}/{total_count}"


def uniform_odds_line(*, selection_count: int, odds: Decimal, decimal_places: int) -> str:
    return f"- 전체 {selection_count}개 조합 동일 · `{odds:.{decimal_places}f}배`"


def opening_header_lines(
    *,
    match_name: str,
    grade: str,
    scheduled_at: datetime,
    stadium_name: str,
    surface: str,
    distance: int,
    direction: str,
    layout: str,
    season: str,
    weather: str,
    time_of_day: str,
    track: str,
    description: str | None,
) -> tuple[str, ...]:
    lines = [
        "## 룸매치 베팅 오픈",
        f"Match: {safe_discord_text(match_name, limit=400)} · {GRADE_LABELS[grade]}",
        f"개최: {format_discord_datetime(scheduled_at)}",
        (
            f"코스: {safe_discord_text(stadium_name, limit=200)} · {SURFACE_LABELS[surface]} {distance}m · "
            f"{DIRECTION_LABELS[direction]} · {LAYOUT_LABELS[layout]}"
        ),
        (
            f"조건: {SEASON_LABELS[season]} · {WEATHER_LABELS[weather]} · "
            f"{TIME_LABELS[time_of_day]} · {TRACK_LABELS[track]}"
        ),
    ]
    if description is not None:
        safe_description = safe_discord_text(description, limit=241)
        if len(safe_description) > 240:
            safe_description = f"{safe_description[:240]}…"
        lines.append(f"설명: {safe_description}")
    lines.append("안내: 개시 시점의 잠정 배당률이며 이후 베팅에 따라 변동됩니다.")
    return tuple(lines)


def refund_page(
    *,
    match_name: str,
    grade: str,
    scheduled_at: datetime,
    completed_at: datetime,
    reason: str | None,
) -> str:
    lines = [
        "## 룸매치 베팅 환불 완료",
        f"Match: {safe_discord_text(match_name, limit=400)} · {GRADE_LABELS[grade]}",
        f"개최: {format_discord_datetime(scheduled_at)}",
        f"환불 완료: {format_discord_datetime(completed_at)}",
        "환불 내역: 취소 처리 시점의 모든 active Bet original stake를 전액 환불했습니다.",
    ]
    if reason is not None:
        lines.append(f"사유: {safe_discord_text(reason, limit=510)}")
    return "\n".join(lines)


def settlement_voided_page(
    *,
    match_name: str,
    grade: str,
    scheduled_at: datetime,
    completed_at: datetime,
    reason: str,
) -> str:
    return "\n".join(
        (
            "## 룸매치 정산 무효 처리",
            f"Match: {safe_discord_text(match_name, limit=400)} · {GRADE_LABELS[grade]}",
            f"개최: {format_discord_datetime(scheduled_at)}",
            f"처리 완료: {format_discord_datetime(completed_at)}",
            f"사유: {safe_discord_text(reason, limit=510)}",
            "안내: 기존 정산 결과는 무효 처리되었습니다.",
            "보상: 관련 서클 포인트와 Rating 상태의 보상 처리가 완료되었습니다.",
        )
    )


def result_line(
    *,
    rank: int,
    entry_number: int,
    player_name: str,
    character_name: str,
    affiliation: str | None,
    before: Decimal,
    delta: Decimal,
    after: Decimal,
    rating_disposition: str | None = None,
    rating_rank: int | None = None,
) -> str:
    affiliation_text = f" · {safe_discord_text(affiliation, limit=200)}" if affiliation is not None else ""
    if rating_disposition == "excluded":
        rating_text = "Rating `제외`"
    elif rating_disposition == "not_applicable":
        rating_text = "Rating `미적용`"
    else:
        rank_text = f" #{rating_rank}" if rating_rank is not None else ""
        delta_text = f"+{delta:.4f}" if delta >= 0 else f"{delta:.4f}"
        rating_text = f"Rating{rank_text} `{before:.4f} → {after:.4f}` (`{delta_text}`)"
    return (
        f"- {rank}착 · Entry {entry_number} · {safe_discord_text(player_name, limit=200)} / "
        f"{safe_discord_text(character_name, limit=200)}{affiliation_text} · "
        f"{rating_text}"
    )


def settled_odds_line(*, bet_type: str, selection: Sequence[int], confirmed_odds: Decimal) -> str:
    return f"- {BET_TYPE_LABELS[bet_type]} `{'-'.join(str(number) for number in selection)}` · `{confirmed_odds:.1f}배`"


def betting_close_header_lines(
    *,
    match_name: str,
    grade: str,
    scheduled_at: datetime,
    closed_at: datetime,
) -> tuple[str, ...]:
    return (
        "## 룸매치 베팅 마감",
        f"Match: {safe_discord_text(match_name, limit=400)} · {GRADE_LABELS[grade]}",
        f"개최: {format_discord_datetime(scheduled_at)}",
        f"마감: {format_discord_datetime(closed_at)}",
        "안내: 마감된 Bet pool에 적용되는 최종 배당률입니다.",
    )


def betting_close_selection_line(
    *,
    entry_numbers: Sequence[int],
    confirmed_odds: Decimal,
) -> str:
    return f"- `{'-'.join(str(number) for number in entry_numbers)}` · `{confirmed_odds:.1f}배`"


def settled_header_lines(
    *,
    match_name: str,
    grade: str,
    scheduled_at: datetime,
    stadium_name: str,
    surface: str,
    distance: int,
    direction: str,
    layout: str,
    season: str,
    weather: str,
    time_of_day: str,
    track: str,
) -> tuple[str, ...]:
    return (
        "## 룸매치 최종 결과",
        f"Match: {safe_discord_text(match_name, limit=400)} · {GRADE_LABELS[grade]}",
        f"개최: {format_discord_datetime(scheduled_at)}",
        (
            f"코스: {safe_discord_text(stadium_name, limit=200)} · {SURFACE_LABELS[surface]} {distance}m · "
            f"{DIRECTION_LABELS[direction]} · {LAYOUT_LABELS[layout]}"
        ),
        (
            f"조건: {SEASON_LABELS[season]} · {WEATHER_LABELS[weather]} · "
            f"{TIME_LABELS[time_of_day]} · {TRACK_LABELS[track]}"
        ),
    )


def odds_refresh_header_lines(*, mode: str, generated_at: datetime) -> tuple[str, ...]:
    return (
        "## 룸매치 잠정 배당률",
        f"공지 모드: {ODDS_REFRESH_MODE_LABELS[mode]}",
        f"기준 시각: {format_discord_datetime(generated_at)}",
        "안내: 현재 active Bet으로 계산한 잠정 배당률이며 베팅 마감 전까지 변동될 수 있습니다.",
    )


def odds_refresh_match_header_lines(
    *,
    match_name: str,
    grade: str,
    scheduled_at: datetime,
) -> tuple[str, ...]:
    return (
        f"### {safe_discord_text(match_name, limit=400)} · {GRADE_LABELS[grade]}",
        f"개최: {format_discord_datetime(scheduled_at)}",
    )


def odds_refresh_market_title(*, bet_type: str) -> str:
    return f"#### {BET_TYPE_LABELS[bet_type]}"


def odds_refresh_selection_line(
    *,
    entry_numbers: Sequence[int],
    provisional_odds: Decimal,
) -> str:
    return f"- `{'-'.join(str(number) for number in entry_numbers)}` · `{provisional_odds:.4f}배`"
