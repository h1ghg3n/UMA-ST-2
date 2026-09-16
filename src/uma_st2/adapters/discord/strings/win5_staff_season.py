"""User-facing copy and pure formatters for WIN5 Season management."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import discord

from uma_st2.application.win5 import (
    ChangedWin5Season,
    Win5SeasonAction,
    Win5SeasonActivationConflictError,
    Win5SeasonLifecycleAuditError,
    Win5SeasonLifecycleError,
    Win5SeasonLifecycleIdempotencyConflictError,
    Win5SeasonLifecycleInvalidSourceError,
    Win5SeasonLifecycleUnavailableError,
    Win5SeasonSnapshot,
)
from uma_st2.application.win5.staff_season_queries import (
    Win5StaffSeasonInvalidSourceError,
    Win5StaffSeasonQueryError,
    Win5StaffSeasonUnavailableError,
)

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

ACTION_PLACEHOLDER = "시즌 작업 선택"
SEASON_TITLE_LABEL = "시즌 제목"
OPERATOR_TITLE_PLACEHOLDER = "운영 번호가 필요하면 제목에 직접 포함"
STARTS_AT_LABEL = "시작 참고 시각 (KST, 선택)"
ENDS_AT_LABEL = "종료 참고 시각 (KST, 선택)"
DATETIME_PLACEHOLDER = "YYYY-MM-DD HH:MM"
OPERATOR_MEMO_LABEL = "운영 메모 (선택)"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CONFIRM_LABEL = "시즌 작업 확정"
CANCEL_LABEL = "취소"
ACTION_SELECTION_INVALID = "Season 작업 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
TARGET_SELECTION_INVALID = "Season 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
TARGET_NOT_ON_PAGE = "선택한 Season이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
TARGET_PAGE_INVALID = "Season 대상 페이지가 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
EDIT_TARGET_REQUIRED = "수정할 Season 대상이 없습니다."
BOUND_MODAL_SUBMIT = "이 입력 창을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
BOUND_TARGET = "이 화면을 연 사용자와 서버·채널에서만 Season 대상을 선택할 수 있습니다."
BOUND_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 Season 작업을 확정할 수 있습니다."
CANCELLED = "WIN5 Season 작업을 취소했습니다."
CONFIRM_ALREADY_STARTED = "이 Season 작업은 이미 처리 중이거나 완료되었습니다."
SEASON_TRANSITION_ERROR = "WIN5 Season 화면을 갱신하지 못했습니다. `/win5 staff season`을 다시 열어 주세요."
START_FIELD_LABEL = "시작 참고 시각"
END_FIELD_LABEL = "종료 참고 시각"


def action_label(action: Win5SeasonAction) -> str:
    return {
        Win5SeasonAction.CREATE: "시즌 생성",
        Win5SeasonAction.ACTIVATE: "시즌 활성화",
        Win5SeasonAction.CLOSE: "시즌 종료",
        Win5SeasonAction.CANCEL: "draft 시즌 취소",
        Win5SeasonAction.EDIT: "시즌 정보 수정",
    }[action]


def action_options(
    *,
    create_value: str,
    activate_value: str,
    close_value: str,
    cancel_value: str,
    edit_value: str,
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            value=create_value,
            label="시즌 생성",
            description="operator 제목과 선택 참고 기간으로 draft를 만듭니다.",
        ),
        discord.SelectOption(
            value=activate_value,
            label="시즌 활성화",
            description="모든 child Round가 setup인 draft를 활성화합니다.",
        ),
        discord.SelectOption(
            value=close_value,
            label="시즌 종료",
            description="모든 child Round가 scored/cancelled인 active를 닫습니다.",
        ),
        discord.SelectOption(
            value=cancel_value,
            label="draft 시즌 취소",
            description="Round가 없는 잘못 만든 draft를 soft-cancel합니다.",
        ),
        discord.SelectOption(
            value=edit_value,
            label="시즌 정보 수정",
            description="상태를 유지하며 제목과 참고 기간을 수정합니다.",
        ),
    ]


def target_placeholder(action: Win5SeasonAction) -> str:
    return f"{action_label(action)} 대상 선택"


def datetime_label(value: datetime | None) -> str:
    return "미지정" if value is None else format_discord_datetime(value)


def target_options(choices: tuple[Win5SeasonSnapshot, ...]) -> list[discord.SelectOption]:
    duplicate_counts = Counter(choice.name for choice in choices)
    options: list[discord.SelectOption] = []
    for choice in choices:
        suffix = f" · ID {choice.id}" if duplicate_counts[choice.name] > 1 else ""
        options.append(
            discord.SelectOption(
                label=f"{safe_discord_text(choice.name, limit=max(1, 100 - len(suffix)))}{suffix}",
                value=str(choice.id),
                description=safe_discord_text(
                    f"{choice.status.value} · Round {choice.rounds.total}개",
                    limit=100,
                ),
            )
        )
    return options


def format_preview_copy(
    *,
    action: Win5SeasonAction,
    target: Win5SeasonSnapshot | None,
    desired_name: str | None,
    desired_starts_at: datetime | None,
    desired_ends_at: datetime | None,
    reason: str | None,
) -> str:
    lines = [f"WIN5 {action_label(action)} 확인"]
    if action in {Win5SeasonAction.CREATE, Win5SeasonAction.EDIT}:
        if target is not None:
            lines.extend(
                (
                    f"현재 이름: {safe_discord_text(target.name)}",
                    f"현재 상태: {target.status.value}",
                    f"현재 참고 기간: {datetime_label(target.starts_at)} ~ {datetime_label(target.ends_at)}",
                )
            )
        lines.extend(
            (
                f"적용 이름: {safe_discord_text(desired_name or '')}",
                f"적용 참고 기간: {datetime_label(desired_starts_at)} ~ {datetime_label(desired_ends_at)}",
            )
        )
        if action == Win5SeasonAction.CREATE:
            lines.append("확정하면 draft Season 한 건을 생성합니다.")
    else:
        if target is None:
            raise ValueError("Season transition Preview has no target.")
        target_status = {
            Win5SeasonAction.ACTIVATE: "active",
            Win5SeasonAction.CLOSE: "closed",
            Win5SeasonAction.CANCEL: "cancelled",
        }[action]
        lines.extend(
            (
                f"시즌: {safe_discord_text(target.name)}",
                f"상태: {target.status.value} → {target_status}",
                f"Round: 총 {target.rounds.total}개 · setup {target.rounds.setup} · open {target.rounds.open} · "
                f"closed {target.rounds.closed} · scored {target.rounds.scored} · cancelled {target.rounds.cancelled}",
            )
        )
        if action == Win5SeasonAction.CANCEL:
            lines.append("Season row는 삭제하지 않고 cancelled 상태로 보존합니다.")
    if reason:
        lines.append(f"운영 메모: {safe_discord_text(reason)}")
    return bounded_discord_message(lines)


def format_success(result: ChangedWin5Season) -> str:
    snapshot = result.snapshot
    heading = "WIN5 시즌 작업을 완료했습니다." if result.changed else "WIN5 시즌 정보에 변경 사항이 없습니다."
    return bounded_discord_message(
        (
            heading,
            f"시즌: {safe_discord_text(snapshot.name)}",
            f"상태: {snapshot.status.value}",
            f"참고 기간: {datetime_label(snapshot.starts_at)} ~ {datetime_label(snapshot.ends_at)}",
            f"Round: {snapshot.rounds.total}개",
        )
    )


def query_error_message(error: Win5StaffSeasonQueryError) -> str:
    if isinstance(error, Win5StaffSeasonUnavailableError):
        detail = "선택한 Season이 더 이상 이 작업의 대상이 아닙니다."
    elif isinstance(error, Win5StaffSeasonInvalidSourceError):
        detail = "Season 또는 child Round 상태를 안전하게 확인할 수 없습니다."
    else:
        detail = "Season 상태를 확인할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 Season 대상을 불러오지 못했습니다: {detail}"


def command_error_message(error: Win5SeasonLifecycleError) -> str:
    if isinstance(error, Win5SeasonActivationConflictError):
        detail = "다른 active Season이 이미 존재합니다. 현재 상태를 다시 확인해 주세요."
    elif isinstance(error, Win5SeasonLifecycleUnavailableError):
        detail = "미리보기 이후 Season 또는 child Round 상태가 변경되었습니다."
    elif isinstance(error, Win5SeasonLifecycleInvalidSourceError):
        detail = "저장된 Season 상태가 불완전하여 변경하지 않았습니다."
    elif isinstance(error, Win5SeasonLifecycleIdempotencyConflictError):
        detail = "같은 요청이 다른 Season 작업에 사용되었습니다."
    elif isinstance(error, Win5SeasonLifecycleAuditError):
        detail = "기존 Season 작업 기록을 안전하게 확인할 수 없습니다."
    else:
        detail = "Season 작업을 완료할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 Season 작업을 완료하지 못했습니다: {detail}"


def datetime_format_error(*, field_label: str) -> str:
    return f"{field_label}은 YYYY-MM-DD HH:MM 형식이어야 합니다."


def datetime_value_error(*, field_label: str) -> str:
    return f"{field_label}은 유효한 YYYY-MM-DD HH:MM KST여야 합니다."


def target_load_error(reference_id: str) -> str:
    return f"Season 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def no_targets(action: Win5SeasonAction) -> str:
    return f"현재 {action_label(action)} 작업이 가능한 WIN5 Season이 없습니다."


def target_page_prompt(*, action: Win5SeasonAction, page_number: int) -> str:
    return f"{action_label(action)} 대상을 선택해 주세요. (페이지 {page_number})"


def modal_error(error: object) -> str:
    return f"Season 입력 창을 열지 못했습니다: {error}"


def preview_error(error: object) -> str:
    return f"WIN5 Season Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"WIN5 Season Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def mutation_error(error: object) -> str:
    return f"WIN5 Season 작업을 완료하지 못했습니다: {error}"


def mutation_internal_error(reference_id: str) -> str:
    return f"WIN5 Season 작업을 완료하지 못했습니다. 참조 ID: `{reference_id}`"
