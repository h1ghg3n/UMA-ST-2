"""User-facing copy and pure formatters for WIN5 submission cancellation."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.win5 import (
    Win5CancellableSubmission,
    Win5CancellableSubmissionUnavailableError,
    Win5MemberCommandError,
    Win5MemberIdentityError,
    Win5MemberQueryError,
    Win5MemberQueryIdentityError,
    Win5RoundUnavailableError,
    Win5SubmissionUnavailableError,
    Win5SubmissionVersionConflictError,
)
from uma_st2.domain.win5 import Win5RoundType, Win5SubmissionTier

from ..common import bounded_discord_message, safe_discord_text

CONFIRM_LABEL = "제출 취소 확정"
BACK_LABEL = "돌아가기"
CANCEL_LABEL = "제출 취소"
NO_ACCEPTED_SUBMISSION = "취소할 accepted Submission이 없습니다."
REASON_TOO_LONG = "WIN5 제출 취소 화면을 열지 못했습니다: 사유는 255자 이하여야 합니다."
DISMISSED = "WIN5 제출을 취소하지 않았습니다."
BOUND_CANCEL_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 취소할 수 있습니다."
CANCEL_ALREADY_STARTED = "이 화면의 제출 취소 처리가 이미 시작되었습니다. 완료 후 `/win5 cancel`을 다시 확인해 주세요."
NORMAL_EDITOR_REPLACEMENT = "라운드가 열린 동안 `/win5 submit`으로 새 제출을 만들 수 있습니다."
SPECIAL_EDITOR_REPLACEMENT = "라운드가 열린 동안 `/win5 special-submit`으로 새 제출을 만들 수 있습니다."
NORMAL_SUCCESS_REPLACEMENT = "라운드가 열려 있으면 `/win5 submit`으로 새 제출을 만들 수 있습니다."
SPECIAL_SUCCESS_REPLACEMENT = "라운드가 열려 있으면 `/win5 special-submit`으로 새 제출을 만들 수 있습니다."


def cancellable_submission_choices(
    targets: tuple[Win5CancellableSubmission, ...],
) -> list[app_commands.Choice[int]]:
    tier_labels = {
        Win5SubmissionTier.TOP1: "TOP1",
        Win5SubmissionTier.TOP3: "TOP3",
        Win5SubmissionTier.TOP5: "TOP5",
        Win5SubmissionTier.SPECIAL_WINNER: "Special",
    }
    label_keys = tuple((target.round_name, tier_labels[target.tier]) for target in targets)
    duplicate_counts = Counter(label_keys)
    choices: list[app_commands.Choice[int]] = []
    for target, label_key in zip(targets, label_keys, strict=True):
        suffix = f" · {tier_labels[target.tier]}"
        if duplicate_counts[label_key] > 1:
            suffix += f" · ID {target.submission_id}"
        title = safe_discord_text(target.round_name, limit=max(1, 100 - len(suffix)))
        choices.append(
            app_commands.Choice(
                name=f"{title}{suffix}",
                value=target.submission_id,
            )
        )
    return choices


def _submission_type_label(target: Win5CancellableSubmission) -> str:
    return "Normal" if target.round_type == Win5RoundType.NORMAL else "Special"


def format_submission_cancel_confirmation(
    target: Win5CancellableSubmission,
    *,
    reason: str | None,
) -> str:
    """Render a mention-safe explicit cancellation preview."""

    lines = [
        "## WIN5 제출 취소 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"유형: {_submission_type_label(target)} / {target.tier.value}",
        f"현재 pick: {target.pick_count}개 / version {target.version}",
    ]
    if reason is not None:
        lines.append(f"사유: {safe_discord_text(reason, limit=255)}")
    lines.extend(
        (
            "현재 accepted Submission을 취소 이력으로 보존합니다.",
            "라운드가 열려 있는 동안 해당 제출 command로 replacement를 만들 수 있습니다.",
        )
    )
    return bounded_discord_message(lines, limit=3500)


def format_editor_cancel_confirmation(*, round_name: str, special: bool) -> str:
    heading = "## WIN5 Special 제출 취소 확인" if special else "## WIN5 제출 취소 확인"
    replacement_message = SPECIAL_EDITOR_REPLACEMENT if special else NORMAL_EDITOR_REPLACEMENT
    return bounded_discord_message(
        (
            heading,
            f"라운드: {safe_discord_text(round_name)}",
            "현재 accepted Submission을 취소 이력으로 보존합니다.",
            replacement_message,
        ),
        limit=3500,
    )


def format_cancel_success(
    *,
    round_name: str,
    version: int,
    replacement_command: str,
    special_heading: bool = False,
    fixed_normal_replacement: bool = False,
) -> str:
    heading = "## WIN5 Special 제출 취소 완료" if special_heading else "## WIN5 제출 취소 완료"
    if special_heading:
        replacement_message = SPECIAL_SUCCESS_REPLACEMENT
    elif fixed_normal_replacement:
        replacement_message = NORMAL_SUCCESS_REPLACEMENT
    else:
        replacement_message = f"라운드가 열려 있으면 `{replacement_command}`으로 새 제출을 만들 수 있습니다."
    return bounded_discord_message(
        (
            heading,
            f"라운드: {safe_discord_text(round_name)}",
            f"취소 이력 version: {version}",
            replacement_message,
        ),
        limit=3500,
    )


def cancellable_query_error_message(error: Win5MemberQueryError) -> str:
    if isinstance(error, Win5MemberQueryIdentityError):
        detail = "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다."
    elif isinstance(error, Win5CancellableSubmissionUnavailableError):
        detail = "선택한 accepted Submission이 더 이상 열린 상태가 아닙니다."
    else:
        detail = "취소할 제출을 조회할 수 없습니다."
    return f"WIN5 제출 취소 화면을 열지 못했습니다: {detail}"


def independent_cancel_error_message(error: Win5MemberCommandError) -> str:
    if isinstance(error, Win5SubmissionVersionConflictError):
        detail = "Submission이 변경되었습니다. `/win5 cancel`을 다시 실행해 주세요."
    elif isinstance(error, Win5RoundUnavailableError):
        detail = "라운드가 더 이상 열려 있지 않습니다."
    elif isinstance(error, Win5MemberIdentityError):
        detail = "현재 WIN5 참가 자격을 확인할 수 없습니다."
    elif isinstance(error, Win5SubmissionUnavailableError):
        detail = "accepted Submission이 더 이상 존재하지 않습니다."
    else:
        detail = "현재 제출을 취소할 수 없습니다."
    return f"WIN5 제출을 취소하지 못했습니다: {detail}"


def normal_cancel_error_message(error: Win5MemberCommandError) -> str:
    if isinstance(error, Win5SubmissionVersionConflictError):
        detail = "Submission이 변경되었습니다. `/win5 submit`을 다시 열어 주세요."
    elif isinstance(error, Win5RoundUnavailableError):
        detail = "라운드가 더 이상 열려 있지 않습니다."
    elif isinstance(error, Win5MemberIdentityError):
        detail = "현재 WIN5 참가 자격을 확인할 수 없습니다."
    elif isinstance(error, Win5SubmissionUnavailableError):
        detail = "accepted Submission이 더 이상 존재하지 않습니다."
    else:
        detail = "현재 제출을 취소할 수 없습니다."
    return f"WIN5 제출을 취소하지 못했습니다: {detail}"


def special_cancel_error_message(error: Win5MemberCommandError) -> str:
    if isinstance(error, Win5SubmissionVersionConflictError):
        detail = "Submission이 변경되었습니다. `/win5 special-submit`을 다시 열어 주세요."
    elif isinstance(error, Win5RoundUnavailableError):
        detail = "라운드가 더 이상 열려 있지 않습니다."
    elif isinstance(error, Win5MemberIdentityError):
        detail = "현재 WIN5 참가 자격을 확인할 수 없습니다."
    elif isinstance(error, Win5SubmissionUnavailableError):
        detail = "accepted Submission이 더 이상 존재하지 않습니다."
    else:
        detail = "현재 Special 제출을 취소할 수 없습니다."
    return f"WIN5 Special 제출을 취소하지 못했습니다: {detail}"


def cancel_internal_error(reference_id: str) -> str:
    return f"WIN5 제출을 취소하지 못했습니다. 참조 ID: `{reference_id}`"


def special_cancel_internal_error(reference_id: str) -> str:
    return f"WIN5 Special 제출을 취소하지 못했습니다. 참조 ID: `{reference_id}`"
