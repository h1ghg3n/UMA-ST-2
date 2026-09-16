"""User-facing copy and pure formatters for WIN5 Round creation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

import discord

from uma_st2.application.win5 import (
    CreatedWin5Round,
    Win5RoundCreationAuditError,
    Win5RoundCreationError,
    Win5RoundCreationIdempotencyConflictError,
    Win5RoundCreationUnavailableError,
)
from uma_st2.application.win5.staff_round_creation_queries import (
    Win5RoundCreationSeasonChoice,
    Win5RoundCreationSeasonPage,
)
from uma_st2.domain.win5 import Win5RoundType

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

SPECIAL_RACE_NAME_LINES = "Race 이름을 한 줄에 하나씩 입력해 주세요."
SPECIAL_RACE_REQUIRED = "Special Round에는 Race 이름이 하나 이상 필요합니다."
SPECIAL_RACE_NAME_LENGTH = "각 Race 이름은 200자 이하여야 합니다."
ENTRY_LIST_REQUIRED = "게이트 번호와 말 이름 목록을 입력해 주세요."
ENTRY_COUNT_MISMATCH = "게이트 번호와 말 이름의 non-empty line 개수가 같아야 합니다."
ENTRY_LINES_REQUIRED = "게이트 번호와 말 이름을 각각 한 줄에 하나씩 입력해 주세요."
GATE_LINE_FORMAT = "게이트 번호는 한 줄에 하나의 양의 정수로 입력해 주세요."
GATE_POSITIVE = "게이트 번호는 양의 정수여야 합니다."
DUPLICATE_GATE = "게이트 번호는 중복될 수 없습니다."
HORSE_NAME_LINES = "말 이름은 각각 100자 이하의 non-empty line이어야 합니다."
DRAFT_ENTRY_MISSING = "선택한 Entry가 현재 draft에 없습니다."
DRAFT_PAGE_INVALID = "선택한 Entry 페이지가 현재 draft 범위를 벗어났습니다."
RACE_SCHEDULE_FORMAT = "Race 일정은 YYYY-MM-DD HH:MM 형식으로 입력해 주세요."
RACE_SCHEDULE_VALID = "Race 일정은 유효한 YYYY-MM-DD HH:MM KST 형식이어야 합니다."

SEASON_SELECT_PLACEHOLDER = "Round를 생성할 Season 선택"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
ROUND_TITLE_LABEL = "라운드 제목"
OPERATOR_TITLE_PLACEHOLDER = "운영 번호가 필요하면 제목에 직접 포함"
RACE_NAME_LABEL = "Race 이름"
RACE_SCHEDULE_LABEL = "Race 일정 (KST, 선택)"
DATETIME_PLACEHOLDER = "YYYY-MM-DD HH:MM"
GATE_NUMBERS_LABEL = "게이트 번호 (한 줄에 하나)"
GATE_NUMBERS_PLACEHOLDER = "1\n2\n3\n4\n5"
HORSE_NAMES_LABEL = "말 이름 (한 줄에 하나)"
HORSE_NAMES_PLACEHOLDER = "말A\n말B\n말C\n말D\n말E"
ENTRY_CORRECTION_TITLE = "WIN5 Entry 수정"
GATE_NUMBER_LABEL = "게이트 번호"
HORSE_NAME_LABEL = "말 이름"
ENTRY_CORRECTION_PLACEHOLDER = "수정할 Entry 선택"
REENTRY_LABEL = "전체 다시 입력"
CONFIRM_LABEL = "생성 확정"
CANCEL_LABEL = "취소"
SPECIAL_RACE_NAMES_LABEL = "Race 이름 (표시 순서대로 한 줄에 하나)"
OPERATOR_MEMO_LABEL = "운영 메모 (선택)"

SEASON_SELECTION_INVALID = "Season 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
SEASON_NOT_ON_PAGE = "선택한 Season이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
ENTRY_NOT_ON_PAGE = "선택한 Entry가 현재 페이지에 없습니다. Preview를 다시 열어 주세요."
ENTRY_NOT_IN_DRAFT = "선택한 Entry가 현재 draft에 없습니다. Preview를 다시 열어 주세요."
BOUND_CORRECTION_SUBMIT = "이 수정 창을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
HORSE_NAME_INVALID = "말 이름은 100자 이하의 non-empty 값이어야 합니다."
ENTRY_CORRECTION_APPLIED = "Entry 수정이 adapter-local draft에 반영되었습니다."
BOUND_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 생성을 확정할 수 있습니다."
REVIEW_ALL_PAGES = "모든 Entry 페이지를 확인한 뒤 생성을 확정해 주세요."
CONFIRM_ALREADY_STARTED = "이 Preview의 생성 확정은 이미 처리 중이거나 완료되었습니다."
NORMAL_CREATION_CANCELLED = "WIN5 일반 라운드 생성을 취소했습니다."
ROUND_CREATION_TRANSITION_ERROR = "WIN5 라운드 생성 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
CREATION_SOURCE_INVALID = "Round를 생성할 Season 상태를 안전하게 확인할 수 없습니다."
SEASON_PAGE_INVALID = "Season 선택 페이지가 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
NO_ELIGIBLE_SEASON = (
    "## ⚠️ WIN5 Round 생성 불가\n"
    "현재 Round를 소유할 draft 또는 active Season이 없습니다. "
    "Eligible Season이 준비된 뒤 다시 시도해 주세요."
)


def minimum_entry_count(count: int) -> str:
    return f"Normal Round에는 Entry가 최소 {count}개 필요합니다."


def special_result_gate_count(expected_count: int) -> str:
    return f"Race {expected_count}개 순서대로 양의 정수 게이트 번호를 한 줄에 하나씩 입력해 주세요."


def round_type_label(round_type: Win5RoundType) -> str:
    return "Normal" if round_type == Win5RoundType.NORMAL else "Special"


def season_warnings(season: Win5RoundCreationSeasonChoice) -> tuple[str, ...]:
    warnings: list[str] = []
    if season.status.value == "draft":
        warnings.append("선택한 Season은 아직 준비 중(draft)입니다.")
    if season.starts_at is None and season.ends_at is None:
        warnings.append("Season 참고 시작·종료 일정이 모두 미지정입니다.")
    elif season.starts_at is None:
        warnings.append("Season 참고 시작 일정이 미지정입니다.")
    elif season.ends_at is None:
        warnings.append("Season 참고 종료 일정이 미지정입니다.")
    return tuple(warnings)


def season_period(season: Win5RoundCreationSeasonChoice) -> str:
    starts_at = format_discord_datetime(season.starts_at) if season.starts_at is not None else "미지정"
    ends_at = format_discord_datetime(season.ends_at) if season.ends_at is not None else "미지정"
    return f"{starts_at} ~ {ends_at}"


def format_normal_round_creation_preview_copy(
    *,
    season: Win5RoundCreationSeasonChoice,
    round_name: str,
    race_name: str,
    scheduled_at: datetime | None,
    entries: Sequence[tuple[int, str]],
    page: int,
    page_count: int,
    start: int,
    end: int,
    notice: str | None,
) -> str:
    status_label = "진행 중" if season.status.value == "active" else "준비 중"
    schedule = format_discord_datetime(scheduled_at) if scheduled_at is not None else "미지정"
    lines = [
        "## WIN5 일반 라운드 생성 확인",
        f"Season: {safe_discord_text(season.name)} · {status_label}",
        f"Season 참고 기간: {season_period(season)}",
        f"Round: {safe_discord_text(round_name)}",
        f"Race: {safe_discord_text(race_name)}",
        f"일정: {schedule}",
        f"Entry: {len(entries)}개 · 게이트 번호순 · 페이지 {page + 1}/{page_count} ({start + 1}~{end})",
    ]
    lines.extend(f"⚠️ Season 경고: {warning}" for warning in season_warnings(season))
    if notice is not None:
        lines.append(f"⚠️ {safe_discord_text(notice, limit=500)}")
    lines.append("### 최종 pairing")
    lines.extend(f"- {gate_number} · {safe_discord_text(horse_name)}" for gate_number, horse_name in entries[start:end])
    lines.append("모든 페이지를 확인한 뒤 생성 확정을 눌러 주세요. Preview는 canonical state가 아닙니다.")
    rendered = "\n".join(lines)
    if len(rendered) > 3500:
        raise ValueError("현재 Entry 페이지를 Discord 표시 한도 안에서 안전하게 표시할 수 없습니다.")
    return rendered


def creation_season_options(
    choices: tuple[Win5RoundCreationSeasonChoice, ...],
) -> list[discord.SelectOption]:
    label_keys = tuple((choice.name, choice.status) for choice in choices)
    duplicate_counts = Counter(label_keys)
    options: list[discord.SelectOption] = []
    for choice, label_key in zip(choices, label_keys, strict=True):
        suffix = f" · ID {choice.id}" if duplicate_counts[label_key] > 1 else ""
        label = safe_discord_text(choice.name, limit=max(1, 100 - len(suffix)))
        status_label = "진행 중" if choice.status.value == "active" else "준비 중"
        period_label = (
            "참고 기간 설정됨" if choice.starts_at is not None and choice.ends_at is not None else "참고 기간 미완성"
        )
        warning_prefix = "⚠ " if season_warnings(choice) else ""
        options.append(
            discord.SelectOption(
                label=f"{label}{suffix}",
                value=str(choice.id),
                description=f"{warning_prefix}{status_label} · {period_label}",
            )
        )
    return options


def season_selector_message(*, round_type: Win5RoundType, page: Win5RoundCreationSeasonPage) -> str:
    page_number = page.offset // page.limit + 1
    lines = [f"{round_type_label(round_type)} Round를 생성할 Season을 선택해 주세요. (페이지 {page_number})"]
    if any(season_warnings(choice) for choice in page.choices):
        lines.append(
            "⚠️ 준비 중이거나 참고 기간이 미완성인 Season도 현재 생성 대상이지만, "
            "표시된 경고를 확인해 주세요. 참고 기간은 자동 lifecycle 기준이 아닙니다."
        )
    return "\n".join(lines)


def format_round_creation_success(result: CreatedWin5Round) -> str:
    snapshot = result.snapshot
    lines = [
        "WIN5 라운드 생성 완료",
        f"시즌: {safe_discord_text(snapshot.season_name)}",
        f"라운드: {safe_discord_text(snapshot.round_name)}",
        f"유형: {round_type_label(snapshot.round_type)}",
        f"상태: {snapshot.round_status.value}",
        f"Race: {len(snapshot.races)}개",
    ]
    for index, race in enumerate(snapshot.races, start=1):
        line = f"- {index}. {safe_discord_text(race.name, limit=200)}"
        if race.scheduled_at is not None:
            line = f"{line} · {format_discord_datetime(race.scheduled_at)}"
        lines.append(line)
    if snapshot.round_type == Win5RoundType.NORMAL:
        entries = snapshot.races[0].entries
        lines.append(f"Entry: {len(entries)}개")
        lines.append("게이트 번호: " + ", ".join(str(entry.gate_number) for entry in entries))
        lines.append("Round를 열기 전에 생성된 Entry를 운영 원본과 다시 대조해 주세요.")
    else:
        lines.append("Reference Entry는 선택 사항이며 별도 setup 단계에서 추가할 수 있습니다.")
    return bounded_discord_message(lines)


def creation_command_error_message(error: Win5RoundCreationError) -> str:
    if isinstance(error, Win5RoundCreationUnavailableError):
        detail = "선택한 Season이 더 이상 Round 생성 대상이 아닙니다."
    elif isinstance(error, Win5RoundCreationIdempotencyConflictError):
        detail = "같은 요청이 다른 Round 생성 입력에 사용되었습니다."
    elif isinstance(error, Win5RoundCreationAuditError):
        detail = "기존 생성 요청 기록을 안전하게 확인할 수 없습니다."
    else:
        detail = "Round를 생성할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 라운드를 생성하지 못했습니다: {detail}"


def normal_modal_title(*, has_warnings: bool) -> str:
    return f"{'⚠ ' if has_warnings else ''}WIN5 일반 라운드 생성"


def special_modal_title(*, has_warnings: bool) -> str:
    return f"{'⚠ ' if has_warnings else ''}WIN5 특별 라운드 생성"


def entry_choice_label(*, gate_number: int, horse_name: str) -> str:
    return safe_discord_text(f"{gate_number} · {horse_name}", limit=100)


def page_label(*, page: int, page_count: int) -> str:
    return f"{page + 1} / {page_count}"


def season_load_error(reference_id: str) -> str:
    return f"Season을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def preview_error(error: object) -> str:
    return f"WIN5 일반 라운드 Preview를 만들지 못했습니다: {error}"


def reentry_error(error: object) -> str:
    return f"전체 다시 입력을 적용하지 않았습니다: {error}"


def correction_error(detail: str) -> str:
    return f"Entry 수정을 적용하지 않았습니다: {detail}"


def creation_error(error: object) -> str:
    return f"WIN5 라운드를 생성하지 못했습니다: {error}"


def creation_internal_error(reference_id: str) -> str:
    return f"WIN5 라운드를 생성하지 못했습니다. 참조 ID: `{reference_id}`"
