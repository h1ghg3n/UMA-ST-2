"""User-facing copy for the periodic Match odds mode View."""

from __future__ import annotations

from uma_st2.application.match import ChangedMatchOddsRefreshMode, MatchOddsRefreshStatus
from uma_st2.application.publication import MatchOddsRefreshMode

from ..datetime_codec import format_discord_datetime

MODE_LABELS = {
    MatchOddsRefreshMode.NORMAL: "NORMAL · 10분",
    MatchOddsRefreshMode.LIVE: "LIVE · 1분",
}

TITLE = "## 룸매치 배당 공지 모드"
GUIDANCE = "운영자가 mode를 직접 전환합니다. 현재 권한과 open Match는 Final interaction에서 다시 확인합니다."
NORMAL_LABEL = "NORMAL로 전환"
LIVE_LABEL = "LIVE로 전환"
BACK_LABEL = "작업 목록"
UNAVAILABLE = "현재 배당 공지 mode를 변경할 수 있는 베팅 오픈 경기가 없습니다."
NO_CHANGE = "이미 선택한 배당 공지 mode입니다. DB에는 기록되지 않았습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 배당 공지 mode 변경에 이미 사용되었습니다."
INVALID_SOURCE = "현재 경기·베팅 정보로 안전한 배당 공지를 만들 수 없습니다."
INTERNAL_ERROR = "배당 공지 mode를 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."


def format_status(status: MatchOddsRefreshStatus, *, success: str | None = None) -> str:
    lines = [TITLE]
    if success is not None:
        lines.append(success)
    lines.extend(
        (
            f"현재 mode: **{MODE_LABELS[status.mode]}**",
            f"베팅 오픈 경기: **{status.open_match_count}개**",
            (
                f"다음 공지 eligible: {format_discord_datetime(status.next_refresh_at)}"
                if status.next_refresh_at is not None
                else "다음 공지 eligible: open Match cycle 대기 중"
            ),
            GUIDANCE,
        )
    )
    return "\n".join(lines)


def format_success(result: ChangedMatchOddsRefreshMode) -> str:
    immediate = (
        f" · immediate publication #{result.publication.publication_id} 생성" if result.publication is not None else ""
    )
    return f"배당 공지 mode를 **{MODE_LABELS[result.mode]}**으로 변경했습니다{immediate}."
