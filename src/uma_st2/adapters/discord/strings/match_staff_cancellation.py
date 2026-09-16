"""User-facing copy for whole-Circle-Match cancellation."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import (
    CancelledMatch,
    MatchCancellationPreviewTarget,
    MatchCancellationTargetChoice,
)

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

REASON_TYPE_ERROR = "환불 사유는 문자열이어야 합니다."
REASON_TOO_LONG = "환불 사유는 255자 이하여야 합니다."
CONFIRM_LABEL = "전체 취소 확정"
CANCEL_LABEL = "취소"
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
CONFIRMATION_STARTED = "이 Preview의 확정은 이미 처리 중이거나 완료되었습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 룸매치 전체 취소 내용에 이미 사용되었습니다."
WALLET_UNAVAILABLE = "환불 대상 Persona의 Circle Point wallet이 없어 전체 취소를 중단했습니다."
UNAVAILABLE = "선택한 Match가 더 이상 전체 취소를 허용하지 않습니다."
CANCELLED = "룸매치 전체 취소를 중단했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


def match_cancellation_autocomplete_choices(
    targets: tuple[MatchCancellationTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert cancellable targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · {target.status.value} · Bet {target.active_bet_count}건",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_cancellation_target(
    *,
    target: MatchCancellationPreviewTarget,
    reason: str | None,
) -> str:
    """Render one bounded private terminal cancellation confirmation."""

    lines = [
        "## 룸매치 전체 취소 Preview",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        f"현재 상태/등급: `{target.status.value}` · `{target.grade.value}`",
        f"개최: {format_discord_datetime(target.scheduled_at)}",
        f"Entry: {target.entry_count}명",
        f"active Bet: {target.active_bet_count}건 · {target.active_stake_total} Circle Point",
        f"환불 대상 Persona: {target.affected_persona_count}명",
        (f"환불 사유: {safe_discord_text(reason, limit=255)}" if reason is not None else "환불 사유: 미입력 (선택)"),
        "",
    ]
    if target.active_bet_count == 0:
        lines.append("현재 active Bet이 없어 Point 환불과 bot 환불 완료 고지는 생성되지 않습니다.")
    else:
        lines.append("Final Confirm 시 current active Bet 전부를 취소하고 original stake를 전액 환불합니다.")
        if reason is not None:
            lines.append("입력한 환불 사유는 public 환불 완료 고지의 별도 사유란에 표시됩니다.")
        else:
            lines.append("Public 환불 완료 고지에는 사유란을 표시하지 않습니다.")
    lines.extend(
        (
            "Match는 terminal `cancelled`가 되며 reopen·settlement를 허용하지 않습니다.",
            "기존 Entry, ResultSubmission과 확정 Result는 삭제하지 않습니다.",
            "Preview는 안내용이며 Final command가 Match-first lock 아래 Bet과 wallet을 다시 검증합니다.",
        )
    )
    return bounded_discord_message(lines, limit=2100)


def format_match_cancellation_success(result: CancelledMatch) -> str:
    """Render the committed private cancellation receipt."""

    lines = [
        "## 룸매치 전체 취소 완료",
        f"Match: {safe_discord_text(result.match_name, limit=200)}",
        f"상태: `{result.previous_status.value}` → `{result.status.value}`",
        (
            f"환불 사유: {safe_discord_text(result.reason, limit=255)}"
            if result.reason is not None
            else "환불 사유: 미입력"
        ),
        f"취소 Bet: {result.cancelled_bet_count}건",
        f"환불: {len(result.refunds)}명 · {result.refund_total} Circle Point",
        "기존 Entry와 Result evidence는 보존했습니다.",
    ]
    if result.publication is not None:
        lines.append(f"Public 환불 완료 고지: `{result.publication.status.value}`")
    else:
        lines.append("Public 환불 완료 고지: 없음 (환불 대상 0건)")
    return bounded_discord_message(lines, limit=1900)


def preview_error(error: object) -> str:
    return f"전체 취소 Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"전체 취소 Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def cancellation_error(error: object) -> str:
    return f"룸매치를 전체 취소하지 못했습니다: {error}"


def cancellation_internal_error(reference_id: str) -> str:
    return f"룸매치를 전체 취소하지 못했습니다. 참조 ID: `{reference_id}`"
