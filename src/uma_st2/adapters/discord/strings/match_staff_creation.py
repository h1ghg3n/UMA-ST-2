"""User-facing copy for native Circle Match creation."""

from __future__ import annotations

from datetime import datetime

from uma_st2.application.match import CreatedMatch, MatchCourseChoice
from uma_st2.domain.match import MatchDirection, MatchSurface, StadiumCourseLayout

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import DiscordTimezone, format_discord_datetime

TITLE_FORMAT_ERROR = "Match 제목은 1~200자로 입력해 주세요."
DESCRIPTION_TOO_LONG = "설명은 1000자 이하로 입력해 주세요."
REASON_TOO_LONG = "사유는 255자 이하로 입력해 주세요."
SCHEDULE_TIMEZONE_ERROR = "개최 시각은 timezone-aware datetime이어야 합니다."
COURSE_AMBIGUOUS = "선택한 조건이 정확히 하나의 코스로 해석되지 않습니다."
UNKNOWN_SELECTION_STEP = "알 수 없는 코스 선택 단계입니다."
COURSE_COMBINATION_UNAVAILABLE = "현재 master data에 없는 코스 조합입니다."
COURSE_OPTIONS_EMPTY = "선택할 수 있는 코스 값이 없습니다."
COURSE_OPTIONS_OVERFLOW = "현재 단계의 선택지가 Discord 한도 25개를 초과합니다."

STADIUM_PLACEHOLDER = "경기장을 선택하세요"
SURFACE_PLACEHOLDER = "잔디/더트를 선택하세요"
DISTANCE_PLACEHOLDER = "거리를 선택하세요"
DIRECTION_PLACEHOLDER = "방향을 선택하세요"
LAYOUT_PLACEHOLDER = "코스 구분을 선택하세요"
DETAILS_LABEL = "상세 입력"
COURSE_RESET_LABEL = "코스 다시 선택"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
MODAL_TITLE = "룸매치 생성"
NAME_LABEL = "Match 제목"
NAME_PLACEHOLDER = "운영 번호가 필요하면 제목에 직접 포함"
DESCRIPTION_LABEL = "설명 (선택)"
REASON_LABEL = "생성 사유 (선택)"
EDIT_LABEL = "내용 수정"
CONFIRM_LABEL = "생성 확정"
CANCEL_LABEL = "취소"

COURSE_REQUIRED = "정확한 코스를 먼저 선택해 주세요."
BOUND_SUBMIT_ERROR = "이 입력 창을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
COURSE_INCOMPLETE = "선택한 코스가 완전하지 않습니다."
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 생성할 수 있습니다."
CONFIRMATION_STARTED = "이미 이 Preview의 생성 요청을 처리하고 있습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 룸매치 생성 내용에 이미 사용되었습니다."
COURSE_STALE = "선택한 코스가 더 이상 현재 master data에 존재하지 않습니다."
CANCELLED = "룸매치 생성을 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."

_SURFACE_LABELS = {
    MatchSurface.TURF: "잔디",
    MatchSurface.DIRT: "더트",
}
_DIRECTION_LABELS = {
    MatchDirection.LEFT: "좌회전",
    MatchDirection.RIGHT: "우회전",
    MatchDirection.STRAIGHT: "직선",
}
_LAYOUT_LABELS = {
    StadiumCourseLayout.STANDARD: "기본",
    StadiumCourseLayout.INNER: "내측",
    StadiumCourseLayout.OUTER: "외측",
    StadiumCourseLayout.OUTER_TO_INNER: "외측→내측",
}


def surface_label(value: MatchSurface) -> str:
    return _SURFACE_LABELS[value]


def direction_label(value: MatchDirection) -> str:
    return _DIRECTION_LABELS[value]


def layout_label(value: StadiumCourseLayout) -> str:
    return _LAYOUT_LABELS[value]


def course_label(course: MatchCourseChoice) -> str:
    return (
        f"{safe_discord_text(course.stadium_name)} · {surface_label(course.surface)} "
        f"{course.distance}m · {direction_label(course.direction)} · {layout_label(course.layout)}"
    )


def format_course_selection_copy(
    *,
    grade: str,
    timezone: str,
    stadium_name: str | None,
    surface: MatchSurface | None,
    distance: int | None,
    direction: MatchDirection | None,
    layout: StadiumCourseLayout | None,
    course: MatchCourseChoice | None,
) -> str:
    """Render current adapter-local course selection progress."""

    stadium = stadium_name if stadium_name is not None else "미선택"
    lines = [
        "## 룸매치 생성 · 코스 선택",
        f"등급: `{grade}` · 입력 timezone: `{timezone}`",
        f"경기장: {safe_discord_text(stadium)}",
        f"주로: {_SURFACE_LABELS.get(surface, '미선택')}",
        f"거리: {f'{distance}m' if distance is not None else '미선택'}",
        f"방향: {_DIRECTION_LABELS.get(direction, '미선택')}",
        f"코스: {_LAYOUT_LABELS.get(layout, '미선택')}",
    ]
    if course is not None:
        lines.extend(("", f"선택 완료: {course_label(course)}", "상세 입력을 열어 제목과 개최 시각을 작성해 주세요."))
    else:
        lines.extend(("", "현재 master data에서 가능한 다음 값만 표시합니다."))
    return bounded_discord_message(lines, limit=1800)


def format_match_creation_preview_copy(
    *,
    course: MatchCourseChoice,
    grade: str,
    timezone: DiscordTimezone,
    name: str,
    description: str | None,
    scheduled_at: datetime,
    reason: str | None,
) -> str:
    """Render the complete zero-write draft carried to Final Confirm."""

    description_text = safe_discord_text(description, limit=1000) if description is not None else "미입력"
    reason_text = safe_discord_text(reason, limit=255) if reason is not None else "미입력"
    return bounded_discord_message(
        (
            "## 룸매치 생성 확인",
            f"제목: {safe_discord_text(name)}",
            f"등급: `{grade}`",
            f"코스: {course_label(course)}",
            f"개최: {format_discord_datetime(scheduled_at, timezone)}",
            f"설명: {description_text}",
            f"생성 사유: {reason_text}",
            "",
            "아직 DB에 기록되지 않은 Preview입니다. 생성 확정 시 현재 master course를 다시 확인합니다.",
        ),
        limit=3500,
    )


def format_match_creation_success(result: CreatedMatch, *, timezone: DiscordTimezone) -> str:
    """Render a private receipt from the committed exact-retry DTO."""

    snapshot = result.snapshot
    course = snapshot.course
    return bounded_discord_message(
        (
            "## ✅ 룸매치 생성 완료",
            f"제목: {safe_discord_text(snapshot.name)}",
            f"등급: `{snapshot.grade.value}` · 상태: `{snapshot.status.value}`",
            (
                f"코스: {safe_discord_text(course.stadium_name)} · {surface_label(course.surface)} "
                f"{course.distance}m · {direction_label(course.direction)} · {layout_label(course.layout)}"
            ),
            f"개최: {format_discord_datetime(snapshot.scheduled_at, timezone)}",
            f"Match ID: `{snapshot.match_id}` · source: `{snapshot.source_kind.value}`",
        ),
        limit=3500,
    )


def date_label(timezone: str) -> str:
    return f"개최일 ({timezone})"


def time_label(timezone: str) -> str:
    return f"개최 시각 ({timezone})"


def course_list_error(error: object) -> str:
    return f"코스 목록을 불러오지 못했습니다: {error}"


def creation_open_error(error: object) -> str:
    return f"룸매치 생성 화면을 열지 못했습니다: {error}"


def creation_open_internal_error(reference_id: str) -> str:
    return f"룸매치 생성 화면을 열지 못했습니다. 참조 ID: `{reference_id}`"


def course_apply_error(error: object) -> str:
    return f"코스 선택을 적용하지 못했습니다: {error}"


def course_refresh_error(error: object) -> str:
    return f"코스 선택 화면을 갱신하지 못했습니다: {error}"


def preview_error(error: object) -> str:
    return f"룸매치 Preview를 만들지 못했습니다: {error}"


def creation_error(error: object) -> str:
    return f"룸매치를 생성하지 못했습니다: {error}"


def creation_internal_error(reference_id: str) -> str:
    return f"룸매치를 생성하지 못했습니다. 참조 ID: `{reference_id}`"
