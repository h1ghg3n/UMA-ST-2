"""User-facing copy for Circle Match betting open."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import MatchBettingOpenTarget, MatchBettingOpenTargetChoice, OpenedMatchBetting
from uma_st2.application.publication import MatchOpeningCondition, MatchOpeningMarket
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchSeason,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)
from uma_st2.domain.publication import PublicationStatus

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CONFIRM_LABEL = "베팅 오픈 확정"
CANCEL_LABEL = "취소"
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
CONFIRMATION_STARTED = "이 Preview의 확정은 이미 처리 중이거나 완료되었습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 베팅 오픈 내용에 이미 사용되었습니다."
STALE = "Match 상태·환경·Entry 또는 공지 설정이 Preview 이후 변경되었습니다. 명령을 다시 열어 주세요."
UNAVAILABLE = "선택한 Match가 더 이상 베팅 오픈을 허용하지 않습니다."
CANCELLED = "룸매치 베팅 오픈을 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."

_SURFACE_LABELS = {MatchSurface.TURF: "잔디", MatchSurface.DIRT: "더트"}
_DIRECTION_LABELS = {
    MatchDirection.LEFT: "좌회전",
    MatchDirection.RIGHT: "우회전",
    MatchDirection.STRAIGHT: "직선",
}
_LAYOUT_LABELS = {
    StadiumCourseLayout.STANDARD: "일반",
    StadiumCourseLayout.INNER: "내측",
    StadiumCourseLayout.OUTER: "외측",
    StadiumCourseLayout.OUTER_TO_INNER: "외측→내측",
}
_SEASON_LABELS = {
    MatchSeason.SPRING: "봄",
    MatchSeason.SUMMER: "여름",
    MatchSeason.AUTUMN: "가을",
    MatchSeason.WINTER: "겨울",
}
_WEATHER_LABELS = {
    MatchWeather.RANDOM: "랜덤",
    MatchWeather.SUNNY: "맑음",
    MatchWeather.CLOUDY: "흐림",
    MatchWeather.RAIN: "비",
    MatchWeather.SNOW: "눈",
}
_TIME_OF_DAY_LABELS = {MatchTimeOfDay.DAY: "낮", MatchTimeOfDay.NIGHT: "밤"}
_TRACK_CONDITION_LABELS = {
    MatchTrackCondition.RANDOM: "랜덤",
    MatchTrackCondition.FIRM: "양호",
    MatchTrackCondition.GOOD: "다소 무거움",
    MatchTrackCondition.SOFT: "포화",
    MatchTrackCondition.HEAVY: "불량",
}
_BET_TYPE_LABELS = {
    BetType.WIN: "단승",
    BetType.QUINELLA: "복승",
    BetType.TRIO: "삼복승",
}
_PUBLICATION_STATUS_LABELS = {
    PublicationStatus.SUPPRESSED: "공지 비활성 · suppressed 저장",
    PublicationStatus.AWAITING_CHANNEL: "공지 채널 미설정 · awaiting_channel 저장",
    PublicationStatus.READY: "공지 대기열 ready",
}


def _format_condition(condition: MatchOpeningCondition | None) -> str:
    if condition is None:
        return "미확정"
    return " · ".join(
        (
            _SEASON_LABELS[condition.season],
            _WEATHER_LABELS[condition.weather],
            _TIME_OF_DAY_LABELS[condition.time_of_day],
            _TRACK_CONDITION_LABELS[condition.track_condition],
        )
    )


def _format_market(market: MatchOpeningMarket) -> str:
    label = _BET_TYPE_LABELS[market.bet_type]
    if not market.available:
        return f"{label}: 이용 불가 (Entry 수 부족)"
    return f"{label}: 모든 유효 조합 `{market.uniform_odds:.4f}` · {market.selection_count}개 조합"


def match_betting_open_autocomplete_choices(
    targets: tuple[MatchBettingOpenTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert scheduled targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        readiness = "준비 완료" if target.is_ready else "준비 미완료"
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · Entry {target.entry_count}명 · {readiness}",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_betting_open_target(
    *,
    target: MatchBettingOpenTarget,
    page: int,
    page_count: int,
    all_pages_reviewed: bool,
    page_size: int,
) -> str:
    """Render a bounded complete opening projection review page."""

    course = target.course
    destination = target.destination
    lines = [
        "## 룸매치 베팅 오픈 Preview",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        f"등급/개최: `{target.grade.value}` · {format_discord_datetime(target.scheduled_at)}",
        (
            "코스: "
            f"{safe_discord_text(course.stadium_name, limit=100)} · {_SURFACE_LABELS[course.surface]} "
            f"{course.distance}m · {_DIRECTION_LABELS[course.direction]} · {_LAYOUT_LABELS[course.layout]}"
        ),
        f"환경: {_format_condition(target.condition)}",
        f"Entry: {len(target.entries)}명 · 페이지 {page + 1}/{page_count}",
    ]
    start = page * page_size
    stop = start + page_size
    lines.extend(
        f"{entry.entry_number}. {safe_discord_text(entry.game_account_name, limit=90)} · "
        f"{safe_discord_text(entry.horse_name, limit=90)}"
        for entry in target.entries[start:stop]
    )
    if target.markets:
        lines.extend(("", "### Zero-pool opening odds"))
        lines.extend(_format_market(market) for market in target.markets)
    if destination is not None:
        lines.extend(("", f"공지: {_PUBLICATION_STATUS_LABELS[destination.initial_status]}"))
        if destination.initial_status == PublicationStatus.READY:
            lines.append(f"채널 snapshot: <#{destination.target_channel_id}>")
    if target.readiness_issues:
        lines.extend(("", "확정 불가:"))
        lines.extend(f"- {safe_discord_text(issue, limit=300)}" for issue in target.readiness_issues)
    elif not all_pages_reviewed:
        lines.extend(("", "모든 Entry 페이지를 확인해야 최종 확정할 수 있습니다."))
    else:
        lines.extend(
            (
                "",
                "Final Confirm에서 상태·환경·Entry·Bet zero-prestate·공지 설정을 다시 잠그고 검사합니다.",
            )
        )
    return bounded_discord_message(lines, limit=3500)


def format_match_betting_open_success(result: OpenedMatchBetting) -> str:
    """Render the committed private opening receipt."""

    lines = [
        "## 룸매치 베팅 오픈 완료",
        f"Match: {safe_discord_text(result.match_name, limit=200)}",
        f"상태: `{result.status.value}` · Entry {result.entry_count}명",
    ]
    lines.extend(_format_market(market) for market in result.markets)
    lines.append(f"Publication: `{result.publication.status.value}` · ID `{result.publication.publication_id}`")
    if result.publication.target_channel_id is not None:
        lines.append(f"채널 snapshot: <#{result.publication.target_channel_id}>")
    return bounded_discord_message(lines, limit=1900)


def preview_error(error: object) -> str:
    return f"베팅 오픈 Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"베팅 오픈 Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def opening_error(error: object) -> str:
    return f"베팅을 열지 못했습니다: {error}"


def opening_internal_error(reference_id: str) -> str:
    return f"베팅을 열지 못했습니다. 참조 ID: `{reference_id}`"
