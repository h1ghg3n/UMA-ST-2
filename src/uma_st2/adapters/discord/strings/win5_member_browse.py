"""User-facing copy and pure formatters for WIN5 member browsing."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from discord import app_commands

from uma_st2.application.win5 import (
    Win5ActiveSeasonInfo,
    Win5ActiveSeasonUnavailableError,
    Win5MemberQueryError,
    Win5MemberQueryIdentityError,
    Win5OpenRoundCard,
    Win5SeasonChoice,
    Win5Standings,
)
from uma_st2.domain.win5 import Win5SeasonStatus

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

STANDINGS_UNAVAILABLE = "순위를 조회하지 못했습니다: 선택한 시즌은 활성 또는 종료 상태여야 합니다."


def _optional_discord_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return format_discord_datetime(value)


def format_active_season_info(info: Win5ActiveSeasonInfo) -> str:
    """Render private active-Season and member-score information."""

    status = {
        Win5SeasonStatus.DRAFT: "작성 중",
        Win5SeasonStatus.ACTIVE: "활성",
        Win5SeasonStatus.CLOSED: "종료",
        Win5SeasonStatus.CANCELLED: "취소",
    }.get(info.season_status, "알 수 없음")
    lines = [
        "현재 WIN5 시즌 정보",
        f"시즌: {safe_discord_text(info.season_name)}",
        f"상태: {status}",
        f"시작: {_optional_discord_datetime(info.starts_at)}",
        f"종료: {_optional_discord_datetime(info.ends_at)}",
        f"라운드: 전체 {info.total_round_count}개 / 오픈 {info.open_round_count}개",
        f"내 시즌 승점: {info.season_score}점",
        f"내 TOP1 승점: {info.top1_score}점",
        "열린 라운드 확인: /win5 rounds",
    ]
    return bounded_discord_message(lines)


def member_info_error_message(error: Win5MemberQueryError) -> str:
    if isinstance(error, Win5MemberQueryIdentityError):
        detail = "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다."
    elif isinstance(error, Win5ActiveSeasonUnavailableError):
        detail = "현재 활성 WIN5 시즌이 없습니다."
    else:
        detail = "현재 정보를 조회할 수 없습니다."
    return f"시즌 정보를 조회하지 못했습니다: {detail}"


def format_open_rounds(rounds: tuple[Win5OpenRoundCard, ...]) -> str:
    """Render the tracked public open-Round list without untrusted mentions."""

    if not rounds:
        return "현재 열린 WIN5 라운드가 없습니다."
    lines = ["현재 열린 WIN5 라운드"]
    lines.extend(f"- {safe_discord_text(round_.name)}" for round_ in rounds)
    return bounded_discord_message(lines)


def format_standings(standings: Win5Standings) -> str:
    """Render public Persona-owned standings with competition ranks."""

    heading = "WIN5 시즌 종합 순위" if standings.ranking == "season" else "WIN5 TOP1 순위"
    lines = [heading, f"시즌: {safe_discord_text(standings.season_name)}"]
    if not standings.entries:
        lines.append("등록된 점수가 없습니다.")
    else:
        lines.extend(
            f"{entry.rank}위 {safe_discord_text(entry.display_name)} / {entry.score}점" for entry in standings.entries
        )
    return bounded_discord_message(lines)


def standings_season_choices(
    seasons: tuple[Win5SeasonChoice, ...],
) -> list[app_commands.Choice[int]]:
    status_labels = {
        Win5SeasonStatus.ACTIVE: "활성",
        Win5SeasonStatus.CLOSED: "종료",
    }
    label_keys = [(season.name, status_labels[season.status]) for season in seasons]
    duplicate_counts = Counter(label_keys)
    choices: list[app_commands.Choice[int]] = []
    for season, (name, status_label) in zip(seasons, label_keys, strict=True):
        suffix = f" · {status_label}"
        if duplicate_counts[(name, status_label)] > 1:
            suffix += f" · ID {season.id}"
        title_limit = max(1, 100 - len(suffix))
        title = safe_discord_text(name, limit=title_limit) or "(제목 없음)"[:title_limit]
        choices.append(app_commands.Choice(name=f"{title}{suffix}", value=season.id))
    return choices
