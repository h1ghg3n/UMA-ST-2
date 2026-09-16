"""User-facing copy for atomic Circle Match settlement."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchSettlementPlan,
    MatchSettlementRatingSelectionTarget,
    MatchSettlementTargetChoice,
    SettledMatch,
)
from uma_st2.domain.betting import BetType
from uma_st2.domain.match import MatchRatingDisposition

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

REASON_TYPE_ERROR = "정산 사유는 문자열이어야 합니다."
REASON_TOO_LONG = "정산 사유는 255자 이하로 입력해 주세요."
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CONFIRM_LABEL = "정산 확정"
RATING_PREVIEW_LABEL = "정산 Preview"
RATING_SELECTION_LABEL = "Rating 제외 대상"
RATING_SELECTION_BACK_LABEL = "Rating 선택 수정"
CANCEL_LABEL = "취소"
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
CONFIRMATION_STARTED = "이 Preview의 확정은 이미 처리 중이거나 완료되었습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 룸매치 정산 내용에 이미 사용되었습니다."
STALE = "Result, Bet pool, Rating 또는 rule version이 바뀌었습니다. 정산 Preview를 다시 열어 주세요."
RULE_UNAVAILABLE = "현재 등급·참가 인원에 적용할 완전한 Rating rule version이 없습니다."
WALLET_UNAVAILABLE = "지급 대상 Persona의 Circle Point wallet이 없어 정산을 중단했습니다."
UNAVAILABLE = "선택한 Match가 더 이상 정산을 허용하지 않습니다."
CANCELLED = "룸매치 정산을 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."

_BET_TYPE_LABELS = {
    BetType.WIN: "단승",
    BetType.QUINELLA: "복승",
    BetType.TRIO: "삼복승",
}

_RATING_DISPOSITION_LABELS = {
    MatchRatingDisposition.RATED: "Rating 반영",
    MatchRatingDisposition.EXCLUDED: "Rating 제외",
    MatchRatingDisposition.NOT_APPLICABLE: "Rating 미적용",
}


def _decimal_display(value: Decimal) -> str:
    text = format(value, ".4f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def match_settlement_autocomplete_choices(
    targets: tuple[MatchSettlementTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert settlement-eligible targets into bounded mention-safe choices."""

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


def match_settlement_rating_selection_options(
    target: MatchSettlementRatingSelectionTarget,
    *,
    excluded_rating_entry_ids: tuple[int, ...],
) -> list[discord.SelectOption]:
    """Build one bounded complete select from the official board."""

    excluded = set(excluded_rating_entry_ids)
    return [
        discord.SelectOption(
            label=safe_discord_text(
                f"{entry.rank}착 · {entry.game_account_name} / {entry.horse_name}",
                limit=100,
            ),
            value=str(entry.match_entry_id),
            description=safe_discord_text(f"Entry {entry.entry_number} · 선택 시 Rating 제외", limit=100),
            default=entry.match_entry_id in excluded,
        )
        for entry in target.entries
    ]


def format_match_settlement_rating_selection(
    *,
    target: MatchSettlementRatingSelectionTarget,
    excluded_rating_entry_ids: tuple[int, ...],
    reason: str | None,
    error: str | None = None,
) -> str:
    """Render the zero-write Rating participation draft."""

    excluded = set(excluded_rating_entry_ids)
    lines = [
        "## 룸매치 Rating 반영 선택",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        f"등급: `{target.grade.value}`",
        f"권위 결과: submission `#{target.result.submission_id}` · revision `{target.result.revision_number}`",
        f"정산 사유: {safe_discord_text(reason, limit=255) if reason is not None else '미입력'}",
        "",
        "Rating에서 제외할 Entry를 선택하세요. 공식 착순, Bet 판정과 착순 보상은 바뀌지 않습니다.",
        "같은 GameAccount의 복수 Entry 중 Rating에 남길 대상을 봇이 자동 선택하지 않습니다.",
        "",
    ]
    if error is not None:
        lines.extend((f"⚠️ {safe_discord_text(error, limit=500)}", ""))
    for entry in target.entries:
        state = "제외" if entry.match_entry_id in excluded else "반영"
        lines.append(
            f"- {entry.rank}착 · Entry {entry.entry_number} · "
            f"{safe_discord_text(entry.game_account_name, limit=80)} / "
            f"{safe_discord_text(entry.horse_name, limit=80)} · **{state}**"
        )
    lines.extend(
        (
            "",
            f"현재 선택: Rating 제외 {len(excluded)}명 · 반영 {len(target.entries) - len(excluded)}명",
            "전부 제외는 허용됩니다. Rating 반영이 정확히 1명인 선택은 허용되지 않습니다.",
        )
    )
    return bounded_discord_message(lines, limit=3900)


def format_match_settlement_plan(
    *,
    plan: MatchSettlementPlan,
    reason: str | None,
    page_index: int,
    page_count: int,
    page_size: int,
) -> str:
    """Render one bounded private settlement Preview page."""

    target = plan.target
    reason_text = safe_discord_text(reason, limit=255) if reason is not None else "미입력"
    lines = [
        "## 룸매치 정산 Preview",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        f"상태/등급: `{target.status.value}` · `{target.grade.value}`",
        f"개최: {format_discord_datetime(target.scheduled_at)}",
        (f"권위 결과: submission `#{target.result.submission_id}` · revision `{target.result.revision_number}`"),
        f"Entry: {len(target.entries)}명 · active Bet: {len(target.active_bets)}건 / {target.active_stake_total} Point",
        f"정산 사유: {reason_text}",
        "",
        "### 확정 배당률",
    ]
    for odds in plan.applied_odds:
        selection = "-".join(str(number) for number in odds.selection_entry_numbers)
        lines.append(
            f"- {_BET_TYPE_LABELS[odds.bet_type]} `{selection}`: "
            f"{format(odds.provisional_odds, '.4f')} → `{format(odds.confirmed_odds, '.1f')}`"
        )
    lines.extend(
        (
            "",
            f"Bet payout: {len(plan.payouts)}명 · {plan.payout_total} Circle Point",
            f"착순 보상: {len(plan.rewards)}명 · {plan.reward_total} Circle Point",
            (
                f"Rating rule: version `{target.rating_rule_version.version_number}`"
                if target.rating_rule_version is not None
                else "Rating rule: OP no-transaction"
            ),
            "",
            f"### Rating Preview · page {page_index + 1}/{page_count}",
        )
    )
    start = page_index * page_size
    for rating in plan.ratings[start : start + page_size]:
        disposition = _RATING_DISPOSITION_LABELS[rating.rating_disposition]
        rating_rank = f" · Rating {rating.rating_rank}위" if rating.rating_rank is not None else ""
        lines.append(
            f"- {rating.rank}착 · Entry {rating.entry_number} · "
            f"{safe_discord_text(rating.game_account_name, limit=80)} / "
            f"{safe_discord_text(rating.horse_name, limit=80)}: "
            f"{_decimal_display(rating.rating_before)} → {_decimal_display(rating.rating_after)} "
            f"(`{_decimal_display(rating.amount)}` · {disposition}{rating_rank})"
        )
    lines.extend(
        (
            "",
            (
                "Final Confirm은 current Result, Bet pool, Rating, rule version과 wallet을 "
                "Match-first lock 아래 다시 검증합니다."
            ),
            (
                "Final Confirm은 정산과 durable 결과 공지 intent를 같은 transaction에 저장합니다. "
                "Discord 전송은 commit 뒤 worker가 수행합니다."
            ),
        )
    )
    return bounded_discord_message(lines, limit=3900)


def format_match_settlement_success(result: SettledMatch) -> str:
    """Render a private receipt only from committed settlement evidence."""

    lines = [
        "## 룸매치 정산 완료",
        f"Match: {safe_discord_text(result.match_name, limit=200)}",
        f"상태: `{result.previous_status.value}` → `{result.status.value}` · 등급 `{result.grade.value}`",
        f"active Bet: {len(result.active_bet_ids)}건 / {result.active_stake_total} Point",
        f"Bet payout: {len(result.payouts)}명 · {result.payout_total} Circle Point",
        f"착순 보상: {len(result.rewards)}명 · {result.reward_total} Circle Point",
        f"Point transaction: {len(result.point_transaction_ids)}건",
        f"Rating transaction: {len(result.rating_transaction_ids)}건",
        "확정 배당률:",
    ]
    for odds in result.applied_odds:
        selection = "-".join(str(number) for number in odds.selection_entry_numbers)
        lines.append(f"- {_BET_TYPE_LABELS[odds.bet_type]} `{selection}` · `{format(odds.confirmed_odds, '.1f')}`")
    lines.append("결과 공지 intent도 함께 저장됐으며 Discord 전송은 commit 뒤 처리됩니다.")
    return bounded_discord_message(lines, limit=2500)


def preview_error(error: object) -> str:
    return f"정산 Preview를 만들지 못했습니다: {error}"


def preview_internal_error(reference_id: str) -> str:
    return f"정산 Preview를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def settlement_error(error: object) -> str:
    return f"룸매치를 정산하지 못했습니다: {error}"


def settlement_internal_error(reference_id: str) -> str:
    return f"룸매치를 정산하지 못했습니다. 참조 ID: `{reference_id}`"
