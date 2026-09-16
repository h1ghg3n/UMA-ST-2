"""User-facing copy and pure formatters for guarded WIN5 setup deletion."""

from __future__ import annotations

from collections import Counter

import discord

from uma_st2.application.win5 import (
    DeletedWin5SetupRound,
    Win5SetupRoundDeletionAuditError,
    Win5SetupRoundDeletionError,
    Win5SetupRoundDeletionIdempotencyConflictError,
    Win5SetupRoundDeletionInvalidSourceError,
    Win5SetupRoundDeletionSnapshot,
    Win5SetupRoundDeletionUnavailableError,
    Win5SetupRoundDeletionVersionConflictError,
)
from uma_st2.application.win5.staff_round_deletion_queries import (
    Win5SetupRoundDeletionTargetChoice,
    Win5StaffRoundDeletionInvalidSourceError,
    Win5StaffRoundDeletionQueryError,
    Win5StaffRoundDeletionUnavailableError,
)
from uma_st2.domain.win5 import Win5RoundType

from ..common import bounded_discord_message, safe_discord_text

TARGET_PLACEHOLDER = "영구 삭제할 setup Round 선택"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
MODAL_TITLE = "WIN5 setup 라운드 삭제"
REASON_LABEL = "삭제 사유"
REASON_PLACEHOLDER = "오입력 또는 재생성 사유를 기록해 주세요."
CONFIRM_LABEL = "영구 삭제 확정"
CANCEL_LABEL = "취소"
TARGET_SELECTION_INVALID = "삭제 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
TARGET_NOT_ON_PAGE = "선택한 삭제 대상이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
TARGET_PAGE_INVALID = "삭제 대상 페이지가 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
NO_TARGETS = "현재 안전하게 삭제할 수 있는 setup WIN5 Round가 없습니다."
BOUND_INPUT = "이 입력 창을 연 사용자와 서버·채널에서만 삭제를 계속할 수 있습니다."
REASON_REQUIRED = "삭제 사유를 입력해 주세요."
BOUND_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 삭제를 확정할 수 있습니다."
CONFIRM_ALREADY_STARTED = "이 삭제 확인은 이미 처리 중이거나 완료되었습니다."
CANCELLED = "WIN5 setup 라운드 삭제를 취소했습니다."
SETUP_DELETION_TRANSITION_ERROR = (
    "WIN5 setup 라운드 삭제 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
)


def round_type_label(round_type: Win5RoundType) -> str:
    return "Normal" if round_type == Win5RoundType.NORMAL else "Special"


def target_options(
    choices: tuple[Win5SetupRoundDeletionTargetChoice, ...],
) -> list[discord.SelectOption]:
    label_keys = tuple((choice.season_name, choice.round_name, choice.round_type) for choice in choices)
    duplicate_counts = Counter(label_keys)
    options: list[discord.SelectOption] = []
    for choice, label_key in zip(choices, label_keys, strict=True):
        suffix = f" · ID {choice.round_id}" if duplicate_counts[label_key] > 1 else ""
        type_label = round_type_label(choice.round_type)
        base_limit = max(1, 100 - len(suffix) - len(type_label) - 3)
        options.append(
            discord.SelectOption(
                label=f"{safe_discord_text(choice.round_name, limit=base_limit)} · {type_label}{suffix}",
                value=str(choice.round_id),
                description=safe_discord_text(
                    f"{choice.season_name} · Race {choice.race_count} · Entry {choice.entry_count}",
                    limit=100,
                ),
            )
        )
    return options


def format_preview(*, snapshot: Win5SetupRoundDeletionSnapshot, reason: str) -> str:
    lines = [
        "WIN5 setup 라운드 영구 삭제 확인",
        f"시즌: {safe_discord_text(snapshot.season_name)} ({snapshot.season_status.value})",
        f"라운드: {safe_discord_text(snapshot.round_name)} · ID {snapshot.round_id}",
        f"유형/상태: {round_type_label(snapshot.round_type)} / {snapshot.round_status.value}",
        f"삭제 범위: Round 1개 · Race {snapshot.race_count}개 · Entry {snapshot.entry_count}개",
        f"운영 사유: {safe_discord_text(reason, limit=255)}",
        f"Graph fingerprint: `{snapshot.graph_fingerprint[:12]}`",
        "확정하면 Round와 모든 Race/Entry가 삭제됩니다.",
        "Season과 operation audit는 보존되며, 새 Round는 자동 생성되지 않습니다.",
        "Race 요약:",
    ]
    for index, race in enumerate(snapshot.races[:10], start=1):
        lines.append(f"- {index}. {safe_discord_text(race.name, limit=160)} · Entry {len(race.entries)}개")
    if snapshot.race_count > 10:
        lines.append(f"- 그 외 Race {snapshot.race_count - 10}개")
    return bounded_discord_message(lines)


def format_success(result: DeletedWin5SetupRound) -> str:
    snapshot = result.snapshot
    return bounded_discord_message(
        (
            "WIN5 setup 라운드를 삭제했습니다.",
            f"시즌: {safe_discord_text(snapshot.season_name)}",
            f"라운드: {safe_discord_text(snapshot.round_name)} · ID {snapshot.round_id}",
            f"삭제됨: Race {snapshot.race_count}개 · Entry {snapshot.entry_count}개",
            f"Audit fingerprint: `{result.graph_fingerprint[:12]}`",
            "필요하면 라운드 생성 작업에서 다시 만들어 주세요.",
        )
    )


def query_error_message(error: Win5StaffRoundDeletionQueryError) -> str:
    if isinstance(error, Win5StaffRoundDeletionUnavailableError):
        detail = "선택한 Round가 더 이상 안전한 삭제 대상이 아닙니다."
    elif isinstance(error, Win5StaffRoundDeletionInvalidSourceError):
        detail = "Season·Round·downstream 상태로 안전한 삭제 미리보기를 만들 수 없습니다."
    else:
        detail = "삭제 대상을 확인할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 setup 라운드 삭제 대상을 불러오지 못했습니다: {detail}"


def command_error_message(error: Win5SetupRoundDeletionError) -> str:
    if isinstance(error, Win5SetupRoundDeletionVersionConflictError):
        detail = "미리보기 이후 Round graph가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SetupRoundDeletionUnavailableError):
        detail = "Round 상태가 바뀌었거나 downstream fact가 생겨 삭제하지 않았습니다."
    elif isinstance(error, Win5SetupRoundDeletionInvalidSourceError):
        detail = "저장된 graph를 안전하게 삭제할 수 없습니다."
    elif isinstance(error, Win5SetupRoundDeletionIdempotencyConflictError):
        detail = "같은 요청이 다른 삭제 작업에 사용되었습니다."
    elif isinstance(error, Win5SetupRoundDeletionAuditError):
        detail = "기존 삭제 요청 기록을 안전하게 확인할 수 없습니다."
    else:
        detail = "setup 라운드를 삭제할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 setup 라운드를 삭제하지 못했습니다: {detail}"


def targets_load_error(reference_id: str) -> str:
    return f"삭제 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def target_page_prompt(page_number: int) -> str:
    return f"잘못 만든 setup Round를 선택해 주세요. Round와 모든 Race/Entry가 삭제됩니다. (페이지 {page_number})"


def preview_error(error: object) -> str:
    return f"삭제 미리보기를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"삭제 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def deletion_error(error: object) -> str:
    return f"WIN5 setup 라운드를 삭제하지 못했습니다: {error}"


def deletion_internal_error(reference_id: str) -> str:
    return f"WIN5 setup 라운드를 삭제하지 못했습니다. 참조 ID: `{reference_id}`"
