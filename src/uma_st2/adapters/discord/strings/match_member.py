"""User-facing copy for Circle Match member commands."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.betting import (
    ActiveMatchBetChoice,
    MatchBetInputPointState,
    MatchBetTargetChoice,
    MatchRaceDetail,
    MatchRaceListItem,
    MemberMatchBetHistoryItem,
    PlacedMatchBet,
    ReplacedMatchBet,
)
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchSeason,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

ENTRY_NUMBERS_TYPE_ERROR = "Entry 번호는 문자열이어야 합니다."
ENTRY_NUMBERS_FORMAT_ERROR = "Entry 번호는 양의 정수를 쉼표 또는 하이픈으로 구분해 입력해 주세요."

BET_DUPLICATE = "같은 Match·베팅 유형·Entry 조합의 active Bet이 이미 있습니다."
BET_INSUFFICIENT_BALANCE = "잔액이 부족합니다."
BET_IDENTITY_UNAVAILABLE = "베팅하려면 Persona에 PID가 등록된 GameAccount가 하나 이상 필요합니다."
BET_WALLET_UNAVAILABLE = "사용 가능한 포인트 지갑이 없습니다. 운영진에게 확인해 주세요."
BET_SELECTION_UNAVAILABLE = "선택한 Entry 번호가 현재 Match에 없거나 조합이 올바르지 않습니다."
BET_MATCH_UNAVAILABLE = "선택한 Match는 현재 베팅을 받지 않습니다."
BET_IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 베팅 내용에 이미 사용되었습니다."

BET_REPLACEMENT_NO_CHANGE = "선택한 active Bet과 정정 내용이 같습니다. 변경할 내용을 입력해 주세요."
BET_REPLACEMENT_DUPLICATE = "정정하려는 Match·베팅 유형·Entry 조합의 다른 active Bet이 이미 있습니다."
BET_REPLACEMENT_INSUFFICIENT_BALANCE = "기존 베팅액을 환불해도 새 베팅액을 충당할 잔액이 부족합니다."
BET_REPLACEMENT_IDENTITY_UNAVAILABLE = "베팅을 정정하려면 Persona에 PID가 등록된 GameAccount가 하나 이상 필요합니다."
BET_REPLACEMENT_WALLET_UNAVAILABLE = "사용 가능한 포인트 지갑이 없습니다. 운영진에게 확인해 주세요."
BET_REPLACEMENT_SELECTION_UNAVAILABLE = "선택한 Entry 번호가 현재 Match에 없거나 조합이 올바르지 않습니다."
BET_REPLACEMENT_UNAVAILABLE = "선택한 Bet은 본인의 active Bet이 아니거나 현재 정정할 수 없습니다."
BET_REPLACEMENT_IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 베팅 정정 내용에 이미 사용되었습니다."
RACE_DETAIL_UNAVAILABLE = "선택한 룸매치는 더 이상 예정 또는 베팅 중 상태가 아닙니다. 목록을 다시 조회해 주세요."


def bet_stake_limit(maximum_stake: int) -> str:
    """Render the private current-wallet placement cap."""

    return f"현재 잔액 기준 1회 베팅 상한은 `{maximum_stake} pt`입니다."


def bet_replacement_stake_limit(maximum_stake: int) -> str:
    """Render the private post-refund replacement cap."""

    return f"기존 베팅액 환불 후 잔액 기준 1회 베팅 상한은 `{maximum_stake} pt`입니다."


_BET_TYPE_LABELS = {
    BetType.WIN: "단승",
    BetType.QUINELLA: "복승",
    BetType.TRIO: "삼복승",
}
_BET_STATUS_LABELS = {
    BetStatus.ACTIVE: "활성",
    BetStatus.SETTLED: "정산 완료",
    BetStatus.CANCELLED: "취소됨",
}
_MATCH_STATUS_LABELS = {
    MatchStatus.SCHEDULED: "예정",
    MatchStatus.ENTRY_CONFIRMED: "출전 확정",
    MatchStatus.BETTING_OPEN: "베팅 중",
    MatchStatus.BETTING_CLOSED: "베팅 마감",
    MatchStatus.RESULT_CONFIRMED: "결과 확정",
    MatchStatus.SETTLED: "정산 완료",
    MatchStatus.CANCELLED: "취소",
    MatchStatus.VOIDED: "무효",
}
_SURFACE_LABELS = {MatchSurface.TURF: "잔디", MatchSurface.DIRT: "더트"}
_DIRECTION_LABELS = {
    MatchDirection.LEFT: "좌회전",
    MatchDirection.RIGHT: "우회전",
    MatchDirection.STRAIGHT: "직선",
}
_LAYOUT_LABELS = {
    StadiumCourseLayout.STANDARD: "일반",
    StadiumCourseLayout.INNER: "내측",
    StadiumCourseLayout.OUTER: "외측",
    StadiumCourseLayout.OUTER_TO_INNER: "외측→내측",
}
_SEASON_LABELS = {
    MatchSeason.SPRING: "봄",
    MatchSeason.SUMMER: "여름",
    MatchSeason.AUTUMN: "가을",
    MatchSeason.WINTER: "겨울",
}
_WEATHER_LABELS = {
    MatchWeather.RANDOM: "랜덤",
    MatchWeather.SUNNY: "맑음",
    MatchWeather.CLOUDY: "흐림",
    MatchWeather.RAIN: "비",
    MatchWeather.SNOW: "눈",
}
_TIME_OF_DAY_LABELS = {MatchTimeOfDay.DAY: "낮", MatchTimeOfDay.NIGHT: "밤"}
_TRACK_CONDITION_LABELS = {
    MatchTrackCondition.RANDOM: "랜덤",
    MatchTrackCondition.FIRM: "양호",
    MatchTrackCondition.GOOD: "다소 무거움",
    MatchTrackCondition.SOFT: "포화",
    MatchTrackCondition.HEAVY: "불량",
}


def match_bet_autocomplete_choices(
    targets: tuple[MatchBetTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert current betting-open targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · {format_discord_datetime(target.scheduled_at)} · Entry {target.entry_count}명",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def match_bet_amount_autocomplete_choices(
    *,
    current_amount: int | str,
    point_state: MatchBetInputPointState | None,
) -> list[app_commands.Choice[int]]:
    """Show one valid amount suggestion with the actor's fresh private balance."""

    if isinstance(current_amount, str):
        stripped = current_amount.strip()
        if stripped and (not stripped.isascii() or not stripped.isdigit()):
            return []
        amount = 0 if not stripped else int(stripped)
    elif isinstance(current_amount, bool) or not isinstance(current_amount, int):
        return []
    else:
        amount = current_amount
    if point_state is None or point_state.balance < 10:
        return []

    if amount < 10 or amount % 10 or amount > point_state.maximum_stake:
        amount = point_state.maximum_stake
        label = f"현재 잔액 {point_state.balance:,} pt · 1회 최대 {amount:,} pt"
    else:
        label = f"{amount:,} pt · 현재 잔액 {point_state.balance:,} pt · 베팅 후 {point_state.balance - amount:,} pt"
    return [app_commands.Choice(name=label, value=amount)]


def format_match_race_list(matches: tuple[MatchRaceListItem, ...]) -> str:
    """Render scheduled and betting-open native Matches for the public list."""

    heading = "## 룸매치 일정 (일정순 최대 10개)"
    if not matches:
        return f"{heading}\n현재 예정되었거나 베팅 중인 룸매치가 없습니다."
    if len(matches) > 10:
        raise ValueError("Match race list cannot contain more than 10 items.")
    lines = [heading]
    lines.extend(
        (
            f"- {safe_discord_text(match.match_name, limit=100)} · {match.grade.value} · "
            f"{format_discord_datetime(match.scheduled_at)} · {_MATCH_STATUS_LABELS[match.status]} · "
            f"Entry {match.entry_count}명"
        )
        for match in matches
    )
    lines.append("베팅 중인 경기 접수: `/match bet`")
    return bounded_discord_message(lines, limit=1900)


def match_race_autocomplete_choices(
    matches: tuple[MatchRaceListItem, ...],
) -> list[app_commands.Choice[int]]:
    """Convert current scheduled/open Matches into bounded detail choices."""

    if len(matches) > 25:
        raise ValueError("Match race autocomplete cannot contain more than 25 items.")
    duplicate_names = {name for name, count in Counter(match.match_name for match in matches).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for match in matches:
        suffix = f" · ID {match.match_id}" if match.match_name in duplicate_names else ""
        primary = safe_discord_text(
            (
                f"{match.match_name} · {format_discord_datetime(match.scheduled_at)} · "
                f"{_MATCH_STATUS_LABELS[match.status]} · Entry {match.entry_count}명"
            ),
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=match.match_id))
    return choices


def format_match_race_detail_pages(
    detail: MatchRaceDetail,
    *,
    page_size: int = 5,
) -> tuple[str, ...]:
    """Render every roster row across bounded public Discord messages."""

    if not isinstance(detail, MatchRaceDetail):
        raise ValueError("detail must be MatchRaceDetail.")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 10:
        raise ValueError("page_size must be between 1 and 10.")

    entry_pages = tuple(
        detail.entries[start : start + page_size] for start in range(0, len(detail.entries), page_size)
    ) or ((),)
    page_count = len(entry_pages)
    rendered: list[str] = []
    for page_index, entries in enumerate(entry_pages):
        course = detail.course
        condition = detail.condition
        heading = "## 룸매치 상세"
        if page_count > 1:
            heading += f" ({page_index + 1}/{page_count})"
        lines = [
            heading,
            f"Match: {safe_discord_text(detail.match_name, limit=200)}",
            (
                f"등급/개최/상태: `{detail.grade.value}` · {format_discord_datetime(detail.scheduled_at)} · "
                f"{_MATCH_STATUS_LABELS[detail.status]}"
            ),
            (
                f"코스: {safe_discord_text(course.stadium_name, limit=100)} · "
                f"{_SURFACE_LABELS[course.surface]} {course.distance}m · "
                f"{_DIRECTION_LABELS[course.direction]} · {_LAYOUT_LABELS[course.layout]}"
            ),
            (
                f"환경: {_SEASON_LABELS[condition.season]} · {_WEATHER_LABELS[condition.weather]} · "
                f"{_TIME_OF_DAY_LABELS[condition.time_of_day]} · "
                f"{_TRACK_CONDITION_LABELS[condition.track_condition]}"
            ),
        ]
        if page_index == 0 and detail.description is not None:
            lines.append(f"설명: {safe_discord_text(detail.description, limit=250)}")
        lines.extend(("", f"### Entry ({len(detail.entries)}명) · 번호 · 주자 · 우마무스메"))
        if not entries:
            lines.append("등록된 Entry가 없습니다.")
        else:
            for entry in entries:
                affiliation = (
                    f" · {safe_discord_text(entry.affiliation, limit=30)}" if entry.affiliation is not None else ""
                )
                lines.append(
                    f"- {entry.entry_number} · {safe_discord_text(entry.game_account_name, limit=45)} · "
                    f"{safe_discord_text(entry.umamusume_name, limit=45)}{affiliation}"
                )
        complete = "\n".join(lines)
        bounded = bounded_discord_message(lines, limit=1900)
        if bounded != complete:
            raise ValueError("A complete Match race detail page exceeds the Discord message bound.")
        rendered.append(bounded)
    return tuple(rendered)


def format_personal_match_bets(bets: tuple[MemberMatchBetHistoryItem, ...]) -> str:
    """Render every item in the bounded private Persona-owned Bet list."""

    heading = "## 최근 내 룸매치 베팅 (최신 10건)"
    if not bets:
        return f"{heading}\n최근 룸매치 베팅 내역이 없습니다."
    if len(bets) > 10:
        raise ValueError("Personal Match Bet list cannot contain more than 10 items.")
    lines = [heading]
    for bet in bets:
        numbers = "-".join(str(number) for number in bet.entry_numbers)
        lines.append(
            f"- Bet #{bet.bet_id} · {safe_discord_text(bet.match_name, limit=30)} · "
            f"{format_discord_datetime(bet.scheduled_at)} · {_BET_TYPE_LABELS[bet.bet_type]} {numbers} · "
            f"{bet.amount} pt · {_BET_STATUS_LABELS[bet.bet_status]} / "
            f"{_MATCH_STATUS_LABELS[bet.match_status]}"
        )
    return bounded_discord_message(lines, limit=1900)


def match_active_bet_autocomplete_choices(
    bets: tuple[ActiveMatchBetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert actor-owned active Bets into bounded mention-safe choices."""

    choices: list[app_commands.Choice[int]] = []
    for bet in bets:
        numbers = "-".join(str(number) for number in bet.entry_numbers)
        label = safe_discord_text(
            (f"{bet.match_name} · Bet #{bet.bet_id} · {_BET_TYPE_LABELS[bet.bet_type]} {numbers} · {bet.amount} pt"),
            limit=100,
        )
        choices.append(app_commands.Choice(name=label, value=bet.bet_id))
    return choices


def format_match_bet_success(result: PlacedMatchBet) -> str:
    """Render one private receipt from committed canonical placement facts."""

    numbers = "-".join(str(selection.entry_number) for selection in result.selections)
    return bounded_discord_message(
        (
            "## 룸매치 베팅 접수 완료",
            f"Match: {safe_discord_text(result.match_name, limit=200)}",
            f"Bet: `#{result.bet_id}` · {_BET_TYPE_LABELS[result.bet_type]} · Entry `{numbers}`",
            f"베팅액: {result.amount} pt",
            f"잔액: {result.balance_after} pt",
        ),
        limit=1900,
    )


def format_match_bet_replacement_success(result: ReplacedMatchBet) -> str:
    """Render one private receipt from committed replacement facts."""

    old_numbers = "-".join(str(selection.entry_number) for selection in result.old_bet.selections)
    new_numbers = "-".join(str(selection.entry_number) for selection in result.new_bet.selections)
    return bounded_discord_message(
        (
            "## 룸매치 베팅 정정 완료",
            f"Match: {safe_discord_text(result.new_bet.match_name, limit=200)}",
            (
                f"기존 Bet: `#{result.old_bet.id}` · {_BET_TYPE_LABELS[result.old_bet.bet_type]} · "
                f"Entry `{old_numbers}` · {result.old_bet.amount} pt 환불"
            ),
            (
                f"새 Bet: `#{result.new_bet.bet_id}` · {_BET_TYPE_LABELS[result.new_bet.bet_type]} · "
                f"Entry `{new_numbers}` · {result.new_bet.amount} pt 차감"
            ),
            f"잔액: {result.balance_after} pt",
        ),
        limit=1900,
    )


def bet_audit_error(reference_id: str) -> str:
    return f"베팅 기록을 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def bet_input_error(error: object) -> str:
    return f"베팅을 접수하지 못했습니다: {error}"


def bet_internal_error(reference_id: str) -> str:
    return f"베팅을 접수하지 못했습니다. 참조 ID: `{reference_id}`"


def bet_replacement_audit_error(reference_id: str) -> str:
    return f"베팅 정정 기록을 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def bet_replacement_input_error(error: object) -> str:
    return f"베팅을 정정하지 못했습니다: {error}"


def bet_replacement_internal_error(reference_id: str) -> str:
    return f"베팅을 정정하지 못했습니다. 참조 ID: `{reference_id}`"
