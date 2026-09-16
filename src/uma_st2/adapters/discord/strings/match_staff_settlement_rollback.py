"""User-facing copy for terminal Circle Match settlement rollback."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from discord import app_commands

from uma_st2.application.match import (
    MatchSettlementRollbackPlan,
    MatchSettlementRollbackTargetChoice,
    RolledBackMatchSettlement,
)

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

REASON_REQUIRED = "정산 롤백 사유를 입력해 주세요."
REASON_FORMAT_ERROR = "정산 롤백 사유는 1~255자로 입력해 주세요."
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CONFIRM_LABEL = "정산 롤백 확정"
CANCEL_LABEL = "취소"
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
CONFIRMATION_STARTED = "이 Preview의 확정은 이미 처리 중이거나 완료되었습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 정산 롤백 내용에 이미 사용되었습니다."
STALE = "settlement evidence, wallet 또는 Rating이 바뀌었습니다. Preview를 다시 열어 주세요."
EVIDENCE_EXPIRED = "operation evidence expired: 정산 증거가 없거나 이후 Rating transaction이 존재합니다."
BALANCE_ERROR = "보상 후 Circle Point 잔액이 지원 범위를 벗어나 정산 롤백을 중단했습니다."
UNAVAILABLE = "선택한 Match가 더 이상 정산 롤백을 허용하지 않습니다."
CANCELLED = "룸매치 정산 롤백을 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


def _decimal_display(value: Decimal) -> str:
    text = format(value, ".4f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def match_settlement_rollback_autocomplete_choices(
    targets: tuple[MatchSettlementRollbackTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert rollback-eligible targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · settled Bet {target.settled_bet_count}건",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_settlement_rollback_plan(
    *,
    plan: MatchSettlementRollbackPlan,
    reason: str,
    page_index: int,
    page_count: int,
    page_size: int,
) -> str:
    """Render one bounded private danger-confirmation page."""

    settlement = plan.target.settlement
    lines = [
        "## 룸매치 정산 롤백 Preview",
        f"Match: {safe_discord_text(plan.target.match_name, limit=200)}",
        f"현재 상태: `{plan.target.status.value}` · 정산: {format_discord_datetime(settlement.settled_at)}",
        f"필수 사유: {safe_discord_text(reason, limit=255)}",
        "",
        "### 원자적 보상",
        f"- settled Bet `{len(plan.target.bets)}`건 취소",
        f"- original stake `{plan.stake_refund_total}` Circle Point 환불",
        f"- Bet payout `{plan.payout_reversal_total}` Circle Point 역분개",
        f"- 착순 보상 `{plan.reward_reversal_total}` Circle Point 역분개",
        f"- Persona wallet `{len(plan.wallets)}`개 갱신",
        f"- Rating compensation `{len(plan.ratings)}`건 append",
        "",
        f"### Rating compensation · page {page_index + 1}/{page_count}",
    ]
    rating_display = {
        item.rating_transaction_id: item for item in settlement.ratings if item.rating_transaction_id is not None
    }
    start = page_index * page_size
    for rating in plan.ratings[start : start + page_size]:
        display = rating_display[rating.original_transaction_id]
        lines.append(
            f"- {display.rank}착 · Entry {display.entry_number} · "
            f"{safe_discord_text(display.game_account_name, limit=80)} / "
            f"{safe_discord_text(display.horse_name, limit=80)}: "
            f"{_decimal_display(rating.compensation_before)} → "
            f"{_decimal_display(rating.compensation_after)} "
            f"(`{_decimal_display(rating.compensation_amount)}`)"
        )
    if not plan.ratings:
        lines.append("- OP: Rating transaction 없음")
    lines.extend(
        (
            "",
            "Final Confirm은 retained settlement bundle, wallet과 latest Rating을 다시 잠그고 검증합니다.",
            "성공하면 Match는 terminal `voided`가 되며 reopen·re-settlement할 수 없습니다.",
            "기존 Result·ResultSubmission·settlement audit은 삭제하거나 수정하지 않습니다.",
            "이미 공개한 Discord 결과는 자동 삭제되지 않습니다.",
        )
    )
    return bounded_discord_message(lines, limit=3900)


def format_match_settlement_rollback_success(result: RolledBackMatchSettlement) -> str:
    """Render a private receipt only from committed compensation evidence."""

    return bounded_discord_message(
        (
            "## 룸매치 정산 롤백 완료",
            f"Match: {safe_discord_text(result.match_name, limit=200)}",
            f"상태: `{result.previous_status.value}` → `{result.status.value}`",
            f"사유: {safe_discord_text(result.reason, limit=255)}",
            f"취소 Bet: {len(result.cancelled_bet_ids)}건",
            f"Point compensation: {len(result.point_compensations)}건",
            f"Rating compensation: {len(result.rating_compensations)}건",
            "원본 Result·ResultSubmission·settlement evidence는 보존했습니다.",
            "이미 공개한 Discord 결과는 자동 삭제되지 않습니다.",
        ),
        limit=2200,
    )


def preview_error(error: object) -> str:
    return f"정산 롤백 Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"정산 롤백 Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def rollback_error(error: object) -> str:
    return f"룸매치 정산을 롤백하지 못했습니다: {error}"


def rollback_internal_error(reference_id: str) -> str:
    return f"룸매치 정산을 롤백하지 못했습니다. 참조 ID: `{reference_id}`"
