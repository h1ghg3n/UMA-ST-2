"""User-facing copy for Circle Match betting close."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import (
    ClosedMatchBetting,
    MatchBettingClosePreviewTarget,
    MatchBettingCloseTargetChoice,
)

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

CONFIRM_LABEL = "베팅 마감 확정"
CANCEL_LABEL = "취소"
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
CONFIRMATION_STARTED = "이 Preview의 확정은 이미 처리 중이거나 완료되었습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 베팅 마감 내용에 이미 사용되었습니다."
UNAVAILABLE = "선택한 Match가 더 이상 베팅 마감을 허용하지 않습니다."
CANCELLED = "룸매치 베팅 마감을 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


def match_betting_close_autocomplete_choices(
    targets: tuple[MatchBettingCloseTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert betting-open targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · Entry {target.entry_count}명 · Bet {target.active_bet_count}건",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_betting_close_target(target: MatchBettingClosePreviewTarget) -> str:
    """Render one bounded private close confirmation from its target."""

    lines = [
        "## 룸매치 베팅 마감 Preview",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        f"등급/개최: `{target.grade.value}` · {format_discord_datetime(target.scheduled_at)}",
        f"Entry: {target.entry_count}명",
        f"현재 active Bet: {target.active_bet_count}건 · {target.active_stake_total} Circle Point",
        "",
    ]
    if target.active_bet_count == 0:
        lines.append("현재 active Bet이 없습니다. 계약상 Bet 0건 상태에서도 마감할 수 있습니다.")
    lines.extend(
        (
            "Preview 이후에도 Final Confirm이 Match lock을 얻기 전까지 Bet이 추가될 수 있습니다.",
            "Final lock 시점의 pool이 확정되며 마감 뒤에는 새 Bet과 reopen을 허용하지 않습니다.",
            "Final Confirm은 complete 최종 배당률 공지 intent를 같은 transaction에 저장합니다.",
            "실제 public 전송은 commit 이후 별도 worker가 수행하며 settlement·refund는 실행하지 않습니다.",
        )
    )
    return bounded_discord_message(lines, limit=1900)


def format_match_betting_close_success(result: ClosedMatchBetting) -> str:
    """Render the committed private close receipt."""

    return bounded_discord_message(
        (
            "## 룸매치 베팅 마감 완료",
            f"Match: {safe_discord_text(result.match_name, limit=200)}",
            f"상태: `{result.status.value}` · Entry {result.entry_count}명",
            f"확정 active Bet: {result.active_bet_count}건 · {result.active_stake_total} Circle Point",
            f"Publication: `{result.publication.status.value}` · ID `{result.publication.publication_id}`",
            "Bet/Point는 변경하지 않았습니다. 최종 배당률 공지는 별도 메시지로 전송됩니다.",
        ),
        limit=1900,
    )


def preview_error(error: object) -> str:
    return f"베팅 마감 Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"베팅 마감 Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def close_error(error: object) -> str:
    return f"베팅을 마감하지 못했습니다: {error}"


def close_internal_error(reference_id: str) -> str:
    return f"베팅을 마감하지 못했습니다. 참조 ID: `{reference_id}`"
