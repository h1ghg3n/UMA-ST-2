"""User-facing copy for Circle Match condition setting."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import (
    MatchConditionTarget,
    MatchConditionValues,
    UpdatedMatchConditions,
)
from uma_st2.domain.match import MatchSeason, MatchTimeOfDay, MatchTrackCondition, MatchWeather

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

REASON_TOO_LONG = "사유는 255자 이하여야 합니다."
TARGET_UNAVAILABLE = "선택한 Match가 더 이상 조건 설정 대상이 아닙니다."
NO_CHANGE = "선택한 값이 현재 환경 조건과 같습니다."
CHANGE_REASON_REQUIRED = "기존 환경 조건을 바꾸려면 사유를 입력해 주세요."
CONFIRM_LABEL = "환경 조건 확정"
CANCEL_LABEL = "취소"
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
CONFIRMATION_STARTED = "이미 이 Preview의 요청을 처리하고 있습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 환경 조건 변경에 이미 사용되었습니다."
STALE = "환경 조건이 Preview 이후 변경되었습니다. 명령을 다시 열어 주세요."
CHANGE_UNAVAILABLE = "선택한 Match가 더 이상 환경 조건 변경을 허용하지 않습니다."
BOUND_CANCEL_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 취소할 수 있습니다."
CANCELLED = "환경 조건 설정을 취소했습니다. DB에는 기록되지 않았습니다."

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
_TIME_OF_DAY_LABELS = {
    MatchTimeOfDay.DAY: "낮",
    MatchTimeOfDay.NIGHT: "밤",
}
_TRACK_CONDITION_LABELS = {
    MatchTrackCondition.RANDOM: "랜덤",
    MatchTrackCondition.FIRM: "양호",
    MatchTrackCondition.GOOD: "다소 무거움",
    MatchTrackCondition.SOFT: "포화",
    MatchTrackCondition.HEAVY: "불량",
}


def _format_values(values: MatchConditionValues) -> str:
    return " · ".join(
        (
            _SEASON_LABELS[values.season],
            _WEATHER_LABELS[values.weather],
            _TIME_OF_DAY_LABELS[values.time_of_day],
            _TRACK_CONDITION_LABELS[values.track_condition],
        )
    )


def format_match_condition_target(
    *,
    target: MatchConditionTarget,
    values: MatchConditionValues,
    reason: str | None,
) -> str:
    """Render one complete mention-safe before/after condition preview."""

    current = "미확정"
    if target.condition is not None:
        current = _format_values(target.condition.values)
    reason_text = "없음" if reason is None else safe_discord_text(reason, limit=255)
    return bounded_discord_message(
        (
            "## 룸매치 환경 조건 Preview",
            f"Match: {safe_discord_text(target.name, limit=200)}",
            f"개최: {format_discord_datetime(target.scheduled_at)}",
            f"현재: {current}",
            f"변경 후: {_format_values(values)}",
            f"사유: {reason_text}",
            "아직 DB에 반영되지 않았습니다. 최종 확정 시 현재 상태를 다시 검사합니다.",
        ),
        limit=1900,
    )


def format_match_condition_success(result: UpdatedMatchConditions) -> str:
    """Render a committed Match condition receipt."""

    condition = result.snapshot.condition
    if condition is None:  # pragma: no cover - DTO invariant
        raise ValueError("Committed Match condition receipt has no condition row.")
    action = "최초 확정" if result.operation_type.value == "match_conditions_set" else "변경"
    return bounded_discord_message(
        (
            "## 룸매치 환경 조건 저장 완료",
            f"Match: {safe_discord_text(result.snapshot.name, limit=200)}",
            f"처리: {action}",
            f"조건: {_format_values(condition.values)}",
        ),
        limit=1900,
    )


def match_condition_autocomplete_choices(
    targets: tuple[MatchConditionTarget, ...],
) -> list[app_commands.Choice[int]]:
    """Convert bounded targets to mention-safe Discord autocomplete choices."""

    duplicate_names = {name for name, count in Counter(target.name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        state = "조건 미확정" if target.condition is None else "조건 확정"
        suffix = f" · ID {target.match_id}" if target.name in duplicate_names else ""
        label = safe_discord_text(f"{target.name} · {state}{suffix}", limit=100)
        choices.append(app_commands.Choice(name=label, value=target.match_id))
    return choices


def preview_error(error: object) -> str:
    return f"환경 조건 Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"환경 조건 Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def save_error(error: object) -> str:
    return f"환경 조건을 저장하지 못했습니다: {error}"


def save_internal_error(reference_id: str) -> str:
    return f"환경 조건을 저장하지 못했습니다. 참조 ID: `{reference_id}`"
