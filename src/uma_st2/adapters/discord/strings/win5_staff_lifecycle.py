"""User-facing copy and pure formatters for WIN5 Round lifecycle."""

from __future__ import annotations

from collections import Counter

import discord

from uma_st2.application.win5 import (
    TransitionedWin5Round,
    Win5RoundLifecycleAction,
    Win5RoundLifecycleAuditError,
    Win5RoundLifecycleError,
    Win5RoundLifecycleIdempotencyConflictError,
    Win5RoundLifecycleInvalidSourceError,
    Win5RoundLifecycleUnavailableError,
    Win5RoundOpenLimitError,
)
from uma_st2.application.win5.staff_round_lifecycle_queries import (
    Win5RoundLifecycleTarget,
    Win5RoundLifecycleTargetChoice,
    Win5StaffRoundLifecycleInvalidSourceError,
    Win5StaffRoundLifecycleQueryError,
    Win5StaffRoundLifecycleUnavailableError,
    Win5StaffRoundOpenLimitError,
)
from uma_st2.domain.win5 import Win5RoundType

from ..common import bounded_discord_message, safe_discord_text

PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CONFIRM_LABEL = "상태 변경 확정"
CANCEL_LABEL = "취소"
TARGET_SELECTION_INVALID = "lifecycle 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
TARGET_NOT_ON_PAGE = "선택한 lifecycle 대상이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
TARGET_PAGE_INVALID = "lifecycle 대상 페이지가 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
BOUND_TARGET = "이 화면을 연 사용자와 서버·채널에서만 lifecycle 대상을 선택할 수 있습니다."
BOUND_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 상태 변경을 확정할 수 있습니다."
CANCELLED = "WIN5 라운드 상태 변경을 취소했습니다."
CONFIRM_ALREADY_STARTED = "이 Round 상태 변경은 이미 처리 중이거나 완료되었습니다."
ROUND_LIFECYCLE_TRANSITION_ERROR = (
    "WIN5 Round 상태 변경 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
)


def round_type_label(round_type: Win5RoundType) -> str:
    return "Normal" if round_type == Win5RoundType.NORMAL else "Special"


def action_label(action: Win5RoundLifecycleAction) -> str:
    return "열기" if action == Win5RoundLifecycleAction.OPEN else "마감"


def target_placeholder(action: Win5RoundLifecycleAction) -> str:
    return f"{action_label(action)} 대상 Round 선택"


def target_options(
    choices: tuple[Win5RoundLifecycleTargetChoice, ...],
) -> list[discord.SelectOption]:
    label_keys = tuple((choice.season_name, choice.round_name, choice.round_type) for choice in choices)
    duplicate_counts = Counter(label_keys)
    options: list[discord.SelectOption] = []
    for choice, label_key in zip(choices, label_keys, strict=True):
        suffix = f" · ID {choice.round_id}" if duplicate_counts[label_key] > 1 else ""
        type_label = round_type_label(choice.round_type)
        base_limit = max(1, 100 - len(suffix) - len(type_label) - 3)
        label = safe_discord_text(choice.round_name, limit=base_limit)
        options.append(
            discord.SelectOption(
                label=f"{label} · {type_label}{suffix}",
                value=str(choice.round_id),
                description=safe_discord_text(choice.season_name, limit=100),
            )
        )
    return options


def format_preview(target: Win5RoundLifecycleTarget) -> str:
    opening = target.action == Win5RoundLifecycleAction.OPEN
    lines = [
        f"WIN5 라운드 {'열기' if opening else '마감'} 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"유형: {round_type_label(target.round_type)}",
        f"현재 상태: {target.current_status.value} → {target.target_status.value}",
        f"Race: {target.race_count}개",
    ]
    if target.round_type == Win5RoundType.NORMAL:
        lines.append(f"Entry: {target.race_entry_count}개")
    lines.append(f"현재 open Round: {target.season_open_round_count}/25개")
    if opening:
        lines.append("확정하면 이 Round의 참가자 제출을 받기 시작합니다.")
    else:
        lines.extend(
            (
                f"현재 accepted Submission: {target.accepted_submission_count}건",
                "확정하면 저장된 제출을 동결하고 이후 수정·취소를 받지 않습니다.",
            )
        )
    return bounded_discord_message(lines)


def format_success(*, target: Win5RoundLifecycleTarget, result: TransitionedWin5Round) -> str:
    action = "열었습니다" if result.status.value == "open" else "마감했습니다"
    return bounded_discord_message(
        (
            f"WIN5 라운드를 {action}.",
            f"시즌: {safe_discord_text(result.season_name)}",
            f"라운드: {safe_discord_text(result.round_name)}",
            f"상태: {target.current_status.value} → {result.status.value}",
        )
    )


def query_error_message(error: Win5StaffRoundLifecycleQueryError) -> str:
    if isinstance(error, Win5StaffRoundOpenLimitError):
        detail = "현재 시즌에 open Round가 이미 25개입니다. 먼저 다른 Round를 마감해 주세요."
    elif isinstance(error, Win5StaffRoundLifecycleUnavailableError):
        detail = "선택한 Round가 더 이상 이 작업의 대상이 아닙니다."
    elif isinstance(error, Win5StaffRoundLifecycleInvalidSourceError):
        detail = "Season·Round·Race 상태가 안전한 lifecycle 작업을 만들 수 없습니다."
    else:
        detail = "라운드 상태를 확인할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 lifecycle 대상을 불러오지 못했습니다: {detail}"


def command_error_message(error: Win5RoundLifecycleError) -> str:
    if isinstance(error, Win5RoundOpenLimitError):
        detail = "동시 open Round 25개 한도에 도달했습니다."
    elif isinstance(error, Win5RoundLifecycleUnavailableError):
        detail = "미리보기 이후 Season 또는 Round 상태가 변경되었습니다."
    elif isinstance(error, Win5RoundLifecycleInvalidSourceError):
        detail = "Round의 Race·Entry·Result 상태가 완전하지 않아 변경하지 않았습니다."
    elif isinstance(error, Win5RoundLifecycleIdempotencyConflictError):
        detail = "같은 요청이 다른 lifecycle 작업에 사용되었습니다."
    elif isinstance(error, Win5RoundLifecycleAuditError):
        detail = "기존 lifecycle 요청 기록을 안전하게 확인할 수 없습니다."
    else:
        detail = "라운드 상태를 변경할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 라운드 상태를 변경하지 못했습니다: {detail}"


def targets_load_error(reference_id: str) -> str:
    return f"lifecycle 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def no_targets(action: Win5RoundLifecycleAction) -> str:
    action_label = "열" if action == Win5RoundLifecycleAction.OPEN else "마감할"
    return f"현재 {action_label} 수 있는 WIN5 Round가 없습니다."


def target_page_prompt(*, action: Win5RoundLifecycleAction, page_number: int) -> str:
    return f"{action_label(action)} 대상 WIN5 Round를 선택해 주세요. (페이지 {page_number})"


def preview_error(error: object) -> str:
    return f"lifecycle 미리보기를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"lifecycle 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def mutation_error(error: object) -> str:
    return f"WIN5 라운드 상태를 변경하지 못했습니다: {error}"


def mutation_internal_error(reference_id: str) -> str:
    return f"WIN5 라운드 상태를 변경하지 못했습니다. 참조 ID: `{reference_id}`"
