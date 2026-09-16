"""User-facing copy and pure formatters for Special Race void operations."""

from __future__ import annotations

from collections import Counter

import discord

from uma_st2.application.win5 import (
    CancelledSpecialWin5Round,
    UpdatedSpecialWin5Void,
    Win5SpecialVoidAuditError,
    Win5SpecialVoidError,
    Win5SpecialVoidIdempotencyConflictError,
    Win5SpecialVoidImmutableError,
    Win5SpecialVoidInvalidSourceError,
    Win5SpecialVoidUnavailableError,
    Win5SpecialVoidVersionConflictError,
)
from uma_st2.application.win5.staff_special_void_queries import (
    Win5StaffSpecialVoidRace,
    Win5StaffSpecialVoidTarget,
    Win5StaffSpecialVoidTargetChoice,
)

from ..common import bounded_discord_message, safe_discord_text

TARGET_PLACEHOLDER = "취소·복원할 특별 라운드 선택"
RACE_PLACEHOLDER = "상태를 변경할 Race 선택"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CHANGE_CONFIRM_LABEL = "상태 변경 확정"
CANCEL_LABEL = "취소"
WHOLE_TARGET_PLACEHOLDER = "전체 취소할 특별 라운드 선택"
WHOLE_MODAL_TITLE = "WIN5 특별 라운드 전체 취소"
WHOLE_REASON_LABEL = "전체 취소 사유"
WHOLE_REASON_PLACEHOLDER = "모든 남은 Race를 취소하는 운영 근거를 기록해 주세요."
WHOLE_CONFIRM_LABEL = "전체 취소 확정"
TARGET_SELECTION_INVALID = "특별 Race 취소 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
ROUND_NOT_ON_PAGE = "선택한 특별 라운드가 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
RACE_SELECTION_INVALID = "특별 Race 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
RACE_NOT_ON_PAGE = "선택한 Race가 현재 페이지에 없습니다. 작업 화면을 다시 열어 주세요."
WHOLE_TARGET_SELECTION_INVALID = "특별 라운드 전체 취소 대상값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
TARGET_SOURCE_INVALID = "특별 Race 취소 대상을 안전하게 확인할 수 없습니다."
WHOLE_TARGET_SOURCE_INVALID = "특별 라운드 전체 취소 대상을 안전하게 확인할 수 없습니다."
NO_TARGETS = "현재 취소·복원할 수 있는 특별 라운드가 없습니다."
NO_WHOLE_TARGETS = "현재 전체 취소할 수 있는 특별 라운드가 없습니다."
TARGET_PROMPT = "취소·복원할 특별 라운드를 선택해 주세요."
WHOLE_TARGET_PROMPT = "모든 남은 Race를 취소할 특별 라운드를 선택해 주세요."
ROUND_STALE = "선택한 특별 라운드 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
RACE_PAGE_INVALID = "선택한 Race 페이지가 현재 대상 범위를 벗어났습니다."
RACE_NOT_IN_ROUND = "선택한 Race가 현재 Special Round에 없습니다."
VOID_DIRECTION_INVALID = "Special Race 취소 변경은 현재 상태의 반대 상태여야 합니다."
REASON_LENGTH = "운영 사유는 1~255자로 입력해 주세요."
WHOLE_REASON_LENGTH = "전체 취소 사유는 1~255자로 입력해 주세요."
RACE_STATE_STALE = "특별 Race 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
WHOLE_STATE_STALE = "특별 라운드 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
VOID_FINGERPRINT_STALE = "Race 취소 상태가 선택 이후 변경되었습니다. 작업 화면을 다시 열어 주세요."
SELECTED_RACE_STALE = "선택한 Race 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
BOUND_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 상태 변경을 확정할 수 있습니다."
BOUND_WHOLE_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 전체 취소를 확정할 수 있습니다."
CHANGE_ALREADY_STARTED = "이 Race 상태 변경은 이미 처리 중이거나 완료되었습니다."
WHOLE_ALREADY_STARTED = "이 특별 라운드 전체 취소는 이미 처리 중이거나 완료되었습니다."
CHANGE_CANCELLED = "WIN5 특별 Race 상태 변경을 취소했습니다."
WHOLE_CANCELLED = "WIN5 특별 라운드 전체 취소를 중단했습니다."
RACE_TRANSITION_ERROR = "WIN5 특별 Race 상태 변경 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
WHOLE_TRANSITION_ERROR = (
    "WIN5 특별 라운드 전체 취소 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
)


def target_options(
    choices: tuple[Win5StaffSpecialVoidTargetChoice, ...],
) -> list[discord.SelectOption]:
    duplicate_counts = Counter(choice.round_name for choice in choices)
    options: list[discord.SelectOption] = []
    for choice in choices:
        suffix = f" · ID {choice.round_id}" if duplicate_counts[choice.round_name] > 1 else ""
        base_limit = max(1, 100 - len(suffix))
        options.append(
            discord.SelectOption(
                label=f"{safe_discord_text(choice.round_name, limit=base_limit)}{suffix}",
                value=str(choice.round_id),
                description=f"Race {choice.race_count}개 · 취소 {choice.void_count}개",
            )
        )
    return options


def race_options(races: tuple[Win5StaffSpecialVoidRace, ...]) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=safe_discord_text(f"{'취소' if race.is_void else '정상'} · {race.name}", limit=100),
            value=str(race.id),
            description=("선택하면 정상 상태로 복원합니다." if race.is_void else "선택하면 명시적 void로 취소합니다."),
        )
        for race in races
    ]


def reason_modal_title(*, race_is_void: bool) -> str:
    return f"WIN5 특별 Race {'복원' if race_is_void else '취소'}"


def reason_label(*, race_is_void: bool) -> str:
    return f"{'복원' if race_is_void else '취소'} 사유"


RACE_REASON_PLACEHOLDER = "공식 취소·복원 근거를 기록해 주세요."


def format_preview(
    *,
    target: Win5StaffSpecialVoidTarget,
    race: Win5StaffSpecialVoidRace,
    target_voided: bool,
    reason: str,
) -> str:
    action = "취소" if target_voided else "복원"
    current = f"취소 · {safe_discord_text(race.void_reason or '', limit=255)}" if race.is_void else "정상"
    lines = [
        f"WIN5 특별 Race {action} 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"Race: {safe_discord_text(race.name, limit=200)}",
        f"현재 상태: {current}",
        f"변경 상태: {'취소(void)' if target_voided else '정상'}",
        f"운영 사유: {safe_discord_text(reason, limit=255)}",
    ]
    if target.result_count:
        lines.append(
            f"⚠️ 현재 미채점 특별 결과 {target.result_count}건은 전체 삭제되며, "
            "변경된 void set 기준으로 다시 입력해야 합니다."
        )
    non_void_count = sum(not item.is_void for item in target.races)
    if target_voided and non_void_count == 1:
        lines.extend(
            (
                "⚠️ 마지막 정상 Race를 취소하므로 Round 전체가 cancelled로 종료됩니다.",
                "채점·승점·서클 포인트·결과 publication은 생성하지 않으며 제출과 pick은 보존됩니다.",
            )
        )
    else:
        lines.append("확정하면 현재 void set 전체를 재검증한 뒤 Race 상태를 변경합니다.")
    return bounded_discord_message(lines)


def format_success(
    *,
    target: Win5StaffSpecialVoidTarget,
    race: Win5StaffSpecialVoidRace,
    result: UpdatedSpecialWin5Void,
) -> str:
    if result.operation_type is None:
        action = "변경 없음"
    elif result.target_voided:
        action = "취소 완료"
    else:
        action = "복원 완료"
    lines = [
        f"WIN5 특별 Race {action}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"Race: {safe_discord_text(race.name, limit=200)}",
        f"현재 취소 Race: {len(result.state.voids)}/{len(result.state.race_ids)}개",
    ]
    if result.state.round_status.value == "cancelled":
        lines.append("Round 상태: cancelled · 채점/포인트/publication 없음")
    elif target.result_count:
        lines.append("기존 미채점 특별 결과 묶음: 삭제 완료")
    return bounded_discord_message(lines)


def format_whole_preview(
    *,
    target: Win5StaffSpecialVoidTarget,
    reason: str,
    newly_voided_count: int,
) -> str:
    lines = [
        "WIN5 특별 라운드 전체 취소 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        (
            f"Race: 전체 {len(target.races)}개 · 기존 void {len(target.races) - newly_voided_count}개 · "
            f"이번 취소 {newly_voided_count}개"
        ),
        f"운영 사유: {safe_discord_text(reason, limit=255)}",
    ]
    if target.result_count:
        lines.append(f"⚠️ 현재 미채점 특별 결과 {target.result_count}건은 전체 삭제됩니다.")
    lines.extend(
        (
            "⚠️ 모든 남은 Race를 한 transaction에서 void 처리하고 Round를 cancelled로 종료합니다.",
            "채점·승점·서클 포인트·결과 publication은 생성하지 않으며 제출과 pick은 보존됩니다.",
            "이 terminal 취소는 현재 command lane에서 복원할 수 없습니다.",
        )
    )
    return bounded_discord_message(lines)


def format_whole_success(
    *,
    target: Win5StaffSpecialVoidTarget,
    result: CancelledSpecialWin5Round,
) -> str:
    return bounded_discord_message(
        (
            "WIN5 특별 라운드 전체 취소 완료",
            f"라운드: {safe_discord_text(target.round_name)}",
            f"새로 취소한 Race: {len(result.newly_voided_race_ids)}개",
            f"현재 취소 Race: {len(result.state.voids)}/{len(result.state.race_ids)}개",
            "Round 상태: cancelled · 채점/포인트/publication 없음",
        )
    )


def command_error_message(error: Win5SpecialVoidError) -> str:
    if isinstance(error, Win5SpecialVoidVersionConflictError):
        detail = "미리보기 이후 취소 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialVoidImmutableError):
        detail = "이미 채점되거나 cancelled로 종료된 라운드의 취소 사실은 변경할 수 없습니다."
    elif isinstance(error, Win5SpecialVoidUnavailableError):
        detail = "선택한 라운드가 더 이상 특별 Race 취소 작업 대상이 아닙니다."
    elif isinstance(error, Win5SpecialVoidInvalidSourceError):
        detail = "Race·현재 결과·취소 상태가 완전하지 않아 변경하지 않았습니다."
    elif isinstance(error, Win5SpecialVoidIdempotencyConflictError):
        detail = "같은 요청이 다른 변경에 사용되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialVoidAuditError):
        detail = "기존 요청 기록을 안전하게 확인할 수 없습니다. 운영 로그를 확인해 주세요."
    else:
        detail = "Race 취소 상태를 변경할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 특별 Race 상태를 변경하지 못했습니다: {detail}"


def whole_command_error_message(error: Win5SpecialVoidError) -> str:
    if isinstance(error, Win5SpecialVoidVersionConflictError):
        detail = "미리보기 이후 취소 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialVoidImmutableError):
        detail = "이미 채점되거나 cancelled로 종료된 라운드는 전체 취소할 수 없습니다."
    elif isinstance(error, Win5SpecialVoidUnavailableError):
        detail = "선택한 라운드가 더 이상 전체 취소 대상이 아닙니다."
    elif isinstance(error, Win5SpecialVoidInvalidSourceError):
        detail = "Race·현재 결과·취소 상태가 완전하지 않아 취소하지 않았습니다."
    elif isinstance(error, Win5SpecialVoidIdempotencyConflictError):
        detail = "같은 요청이 다른 변경에 사용되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialVoidAuditError):
        detail = "기존 요청 기록을 안전하게 확인할 수 없습니다. 운영 로그를 확인해 주세요."
    else:
        detail = "특별 라운드를 전체 취소할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 특별 라운드를 취소하지 못했습니다: {detail}"


def targets_load_error(reference_id: str) -> str:
    return f"특별 Race 취소 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def whole_targets_load_error(reference_id: str) -> str:
    return f"특별 라운드 전체 취소 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def race_page_error(error: object) -> str:
    return f"특별 Race 페이지를 표시할 수 없습니다: {error}"


def races_load_error(reference_id: str) -> str:
    return f"특별 Race를 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def race_page_content(
    *,
    target: Win5StaffSpecialVoidTarget,
    page: int,
    page_count: int,
    start: int,
    visible_count: int,
) -> str:
    return bounded_discord_message(
        (
            f"라운드: {safe_discord_text(target.round_name)}",
            (
                f"Race {len(target.races)}개 · 취소 {sum(race.is_void for race in target.races)}개 · "
                f"페이지 {page + 1}/{page_count} ({start + 1}~{start + visible_count})"
            ),
            "상태를 변경할 Race를 선택해 주세요. 정상 Race는 취소, 취소 Race는 복원으로 진행합니다.",
        )
    )


def preview_error(error: object) -> str:
    return f"특별 Race 미리보기를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"특별 Race 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def whole_preview_error(error: object) -> str:
    return f"전체 취소 미리보기를 만들지 못했습니다: {error}"


def whole_preview_internal_error(reference_id: str) -> str:
    return f"전체 취소 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def mutation_error(error: object) -> str:
    return f"WIN5 특별 Race 상태를 변경하지 못했습니다: {error}"


def mutation_internal_error(reference_id: str) -> str:
    return f"WIN5 특별 Race 상태를 변경하지 못했습니다. 참조 ID: `{reference_id}`"


def whole_mutation_error(error: object) -> str:
    return f"WIN5 특별 라운드를 취소하지 못했습니다: {error}"


def whole_mutation_internal_error(reference_id: str) -> str:
    return f"WIN5 특별 라운드를 취소하지 못했습니다. 참조 ID: `{reference_id}`"
