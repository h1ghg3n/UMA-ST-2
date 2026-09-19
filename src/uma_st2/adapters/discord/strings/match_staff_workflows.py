"""User-facing copy for consolidated Circle Match staff workflow panels."""

from __future__ import annotations

from datetime import datetime

from uma_st2.application.match import (
    CreatedMatch,
    MatchConditionValues,
    MatchSetupTarget,
    UpdatedMatchSetup,
)
from uma_st2.domain.match import MatchSeason, MatchTimeOfDay, MatchTrackCondition, MatchWeather

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import DiscordTimezone, format_discord_datetime
from .match_staff_creation import course_label

RACE_COMMAND_DESCRIPTION = "룸매치 생성·수정과 베팅 lifecycle을 관리합니다."
RESULT_COMMAND_DESCRIPTION = "룸매치 결과 입력·검토·확정을 관리합니다."
SETTLEMENT_COMMAND_DESCRIPTION = "룸매치 정산·롤백과 결과 공개 복구를 관리합니다."

RACE_PANEL_TITLE = "## 룸매치 경기 관리"
RESULT_PANEL_TITLE = "## 룸매치 결과 관리"
SETTLEMENT_PANEL_TITLE = "## 룸매치 정산 관리"
PANEL_GUIDANCE = "실행할 작업을 선택해 주세요. 선택 이후에도 현재 권한과 대상 상태를 다시 확인합니다."

RACE_ACTIONS = (
    ("create", "새 경기 생성", "환경 조건까지 포함한 새 룸매치 설정을 준비합니다."),
    ("edit", "경기 정보 수정", "베팅 시작 전 이름·일정·코스·환경을 수정합니다."),
    ("entries", "엔트리 입력/수정", "경기를 선택해 출전자와 우마무스메를 입력하거나 수정합니다."),
    ("open", "베팅 시작", "현재 경기 정보를 검토한 뒤 베팅을 시작합니다."),
    ("close", "베팅 마감", "현재 active pool을 기준으로 베팅을 마감합니다."),
    ("cancel", "경기 전체 취소", "필요한 active Bet 환불과 함께 경기를 종료합니다."),
    ("odds", "배당 공지 모드", "주기 배당 공지의 NORMAL/LIVE cadence를 전환합니다."),
)
RESULT_ACTIONS = (
    ("submit", "결과 입력/정정", "새 pending 결과 또는 정정 revision을 준비합니다."),
    ("review", "pending 결과 검토", "현재 pending 결과를 검토하고 필요하면 거부합니다."),
    ("confirm", "pending 결과 확정", "현재 pending 결과를 canonical 결과로 확정합니다."),
)
SETTLEMENT_ACTIONS = (
    ("settle", "정산", "확정 결과를 Bet·Point·Rating과 함께 정산합니다."),
    ("rollback", "정산 롤백", "retained evidence로 terminal 보상 rollback을 실행합니다."),
    ("recover", "결과 공개 복구", "누락된 settled-result publication intent만 복구합니다."),
)

ACTION_PLACEHOLDER = "작업 선택"
TARGET_PLACEHOLDER = "대상 경기 선택"
NO_TARGETS = "현재 선택할 수 있는 대상 경기가 없습니다."
TARGET_LOAD_ERROR = "대상 경기를 불러오지 못했습니다."
INVALID_ACTION = "지원하지 않는 작업입니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."
PANEL_CANCELLED = "작업을 취소했습니다. DB에는 기록되지 않았습니다."
PANEL_CLOSE_LABEL = "닫기"
PANEL_BACK_LABEL = "작업 목록"
RESULT_TRANSITION_ERROR = "결과 작업 화면을 갱신하지 못했습니다. `/match staff result`를 다시 열어 주세요."
RACE_TRANSITION_ERROR = "경기 작업 화면을 갱신하지 못했습니다. `/match staff race`를 다시 열어 주세요."
SETTLEMENT_TRANSITION_ERROR = "정산 작업 화면을 갱신하지 못했습니다. `/match staff settlement`를 다시 열어 주세요."

REASON_MODAL_TITLES = {
    "cancel": "경기 전체 취소",
    "submit": "결과 입력/정정",
    "settle": "룸매치 정산",
    "rollback": "룸매치 정산 롤백",
}
REASON_MODAL_LABELS = {
    "cancel": "취소·환불 사유 (선택)",
    "submit": "결과 입력·정정 사유 (선택)",
    "settle": "정산 사유 (선택)",
    "rollback": "롤백 사유 (필수)",
}

SETUP_TITLE_CREATE = "새 룸매치 설정"
SETUP_TITLE_EDIT = "룸매치 설정 수정"
SETUP_BASIC_LABEL = "기본 정보 편집"
SETUP_COURSE_LABEL = "코스 편집"
SETUP_CONDITION_LABEL = "환경 조건 편집"
SETUP_SAVE_LABEL = "최종 저장"
SETUP_CANCEL_LABEL = "취소"
SETUP_BACK_LABEL = "뒤로"
SETUP_APPLY_LABEL = "적용"
SETUP_RESET_COURSE_LABEL = "코스 다시 선택"
SETUP_TEXT_LABEL = "텍스트·일정 입력"
SETUP_CONFIRM_LABEL = "최종 확정"

BASIC_MODAL_CREATE_TITLE = "새 룸매치 기본 정보"
BASIC_MODAL_EDIT_TITLE = "룸매치 기본 정보 수정"
NAME_LABEL = "제목"
DATE_LABEL = "개최일 (YYYY-MM-DD)"
TIME_LABEL = "개최시각 (HH:MM)"
DESCRIPTION_LABEL = "설명 (선택)"
REASON_LABEL = "변경 사유 (수정 시 필수)"

GRADE_PLACEHOLDER = "Grade"
TIMEZONE_PLACEHOLDER = "Timezone"
SEASON_PLACEHOLDER = "계절"
WEATHER_PLACEHOLDER = "날씨"
TIME_OF_DAY_PLACEHOLDER = "시간대"
TRACK_PLACEHOLDER = "마장 상태"
RANDOM_CONDITION_LABEL = "랜덤"

INCOMPLETE_SETUP = "기본 정보, 코스와 환경 조건을 모두 확정해 주세요."
EDIT_REASON_REQUIRED = "기존 경기 설정을 바꾸려면 사유를 입력해 주세요."
NO_CHANGE = "변경할 경기 설정이 없습니다."
SETUP_STALE = "경기 설정이 Preview 이후 변경되었습니다. 새로 열어 주세요."
SETUP_UNAVAILABLE = "선택한 경기는 더 이상 설정을 수정할 수 없습니다."
SETUP_IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 경기 설정 작업에 이미 사용되었습니다."
SETUP_CONFIRMATION_STARTED = "이미 이 Preview의 요청을 처리하고 있습니다."
SETUP_CANCELLED = "룸매치 설정 작업을 취소했습니다. DB에는 기록되지 않았습니다."

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
_TIME_LABELS = {
    MatchTimeOfDay.DAY: "낮",
    MatchTimeOfDay.NIGHT: "밤",
}
_TRACK_LABELS = {
    MatchTrackCondition.RANDOM: "랜덤",
    MatchTrackCondition.FIRM: "양호",
    MatchTrackCondition.GOOD: "다소 무거움",
    MatchTrackCondition.SOFT: "포화",
    MatchTrackCondition.HEAVY: "불량",
}


def format_condition(values: MatchConditionValues | None) -> str:
    if values is None:
        return "미확정"
    return " · ".join(
        (
            _SEASON_LABELS[values.season],
            _WEATHER_LABELS[values.weather],
            _TIME_LABELS[values.time_of_day],
            _TRACK_LABELS[values.track_condition],
        )
    )


def format_setup_summary(
    *,
    mode: str,
    name: str | None,
    grade: object,
    scheduled_at: datetime | None,
    timezone: DiscordTimezone,
    description: str | None,
    course: object | None,
    condition: MatchConditionValues | None,
    entry_count: int,
    reason: str | None,
) -> str:
    schedule = "미입력" if scheduled_at is None else format_discord_datetime(scheduled_at, timezone)
    course_text = "미확정" if course is None else course_label(course)
    description_text = "없음" if description is None else safe_discord_text(description, limit=1000)
    reason_text = "없음" if reason is None else safe_discord_text(reason, limit=255)
    return bounded_discord_message(
        (
            f"## {SETUP_TITLE_CREATE if mode == 'create' else SETUP_TITLE_EDIT}",
            f"제목: {safe_discord_text(name or '미입력', limit=200)}",
            f"Grade: {getattr(grade, 'value', grade)}",
            f"개최: {schedule}",
            f"설명: {description_text}",
            f"코스: {course_text}",
            f"환경: {format_condition(condition)}",
            f"Entry: 현재 {entry_count}명",
            f"사유: {reason_text}",
            "아직 DB에 반영되지 않았습니다.",
        ),
        limit=3500,
    )


def format_setup_confirmation(*, before: MatchSetupTarget | None, desired: str) -> str:
    if before is None:
        return bounded_discord_message(
            ("## 새 룸매치 생성 Preview", desired, "최종 확정 시 한 번만 생성합니다."), limit=3500
        )
    before_condition = None if before.condition is None else before.condition.values
    before_text = bounded_discord_message(
        (
            "### 변경 전",
            f"제목: {safe_discord_text(before.name, limit=200)}",
            f"Grade: {before.grade.value}",
            f"개최: {format_discord_datetime(before.scheduled_at)}",
            f"코스: {course_label(before.course)}",
            f"환경: {format_condition(before_condition)}",
        ),
        limit=1600,
    )
    return bounded_discord_message(
        (
            "## 룸매치 설정 변경 Preview",
            before_text,
            "### 변경 후",
            desired,
            "최종 확정 시 현재 상태를 다시 검사합니다.",
        ),
        limit=3500,
    )


def format_setup_success(result: CreatedMatch | UpdatedMatchSetup) -> str:
    snapshot = result.snapshot
    condition = snapshot.condition
    return bounded_discord_message(
        (
            "## 룸매치 생성 완료" if isinstance(result, CreatedMatch) else "## 룸매치 설정 수정 완료",
            f"경기: {safe_discord_text(snapshot.name, limit=200)}",
            f"Grade: {snapshot.grade.value}",
            f"개최: {format_discord_datetime(snapshot.scheduled_at)}",
            f"코스: {course_label(snapshot.course)}",
            f"환경: {format_condition(condition.values)}",
        ),
        limit=1900,
    )
