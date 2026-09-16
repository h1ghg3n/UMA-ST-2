"""User-facing copy for private Account/Persona status."""

from __future__ import annotations

from decimal import Decimal

from uma_st2.application.identity import (
    AccountEligibilityState,
    AccountIdentityDetails,
    AccountMatchHistoryPage,
    AccountStatusOverview,
    AccountWin5HistoryPage,
)
from uma_st2.domain.identity import PersonaStatus, RegistrationRequestStatus
from uma_st2.domain.match import MatchStatus
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SubmissionStatus

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

ACCOUNT_ROOT_DESCRIPTION = "계정 등록을 요청하고 내 개인 상태를 확인합니다."
ACCOUNT_REGISTER_DESCRIPTION = "PID GameAccount 등록 검토를 요청합니다."
ACCOUNT_STATUS_DESCRIPTION = "내 계정·Circle Point·룸매치·WIN5 상태를 확인합니다."

REGISTER_MODAL_TITLE = "계정 등록 요청"
REGISTER_PID_LABEL = "PID"
REGISTER_PID_PLACEHOLDER = "숫자만 입력"
REGISTER_NICKNAME_LABEL = "게임 내 닉네임"
REGISTER_AFFILIATION_LABEL = "소속 (선택)"
REGISTER_ALREADY_LINKED = "이미 Persona에 연결된 계정입니다. `/account status`에서 현재 상태를 확인해 주세요."
REGISTER_PID_UNAVAILABLE = "해당 리전/PID는 이미 등록되어 있습니다. 운영진에게 확인을 요청해 주세요."
REGISTER_IDEMPOTENCY_CONFLICT = "이 상호작용은 다른 요청에 이미 사용되었습니다. 명령을 다시 실행해 주세요."
REGISTER_BOUND_INTERACTION_ERROR = "등록 Modal을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
REGISTER_INPUT_INVALID = (
    "입력값을 확인해 주세요. PID는 0으로 시작하지 않는 숫자 1~32자리이며, "
    "닉네임은 공백이 아닌 100자 이하, 소속은 선택 사항 100자 이하입니다."
)

OVERVIEW_LABEL = "개요"
MATCH_LABEL = "룸매치"
WIN5_LABEL = "WIN5"
IDENTITY_LABEL = "계정"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
REFRESH_LABEL = "새로고침"
CLOSE_LABEL = "닫기"
CLOSED = "계정 상태 화면을 닫았습니다."
STATUS_TRANSITION_ERROR = "계정 상태 화면을 갱신하지 못했습니다. `/account status`를 다시 열어 주세요."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 계속할 수 있습니다."
MEMBER_APPROVAL_PENDING = (
    "계정 승인을 기다리는 중입니다. 상태와 기존 기록은 `/account status`에서 확인할 수 있지만, "
    "승인 전에는 새 베팅이나 WIN5 제출을 만들거나 정정할 수 없습니다."
)

_PERSONA_STATUS_LABELS = {
    PersonaStatus.NORMAL: "정상",
    PersonaStatus.WARNING: "경고",
    PersonaStatus.PENDING_APPROVAL: "승인 대기",
    PersonaStatus.EXPELLED: "제명",
    PersonaStatus.WITHDRAWN: "탈퇴",
}
_ELIGIBILITY_LABELS = {
    AccountEligibilityState.ELIGIBLE: "사용 가능",
    AccountEligibilityState.REGISTRATION_PENDING: "등록 요청 검토 중",
    AccountEligibilityState.APPROVAL_PENDING: "계정 승인 대기",
    AccountEligibilityState.GAME_ACCOUNT_REQUIRED: "PID 등록 GameAccount 필요",
    AccountEligibilityState.WALLET_REQUIRED: "Circle Point 지갑 확인 필요",
    AccountEligibilityState.RESTRICTED: "사용 제한",
    AccountEligibilityState.UNREGISTERED: "등록 필요",
}
_MATCH_STATUS_LABELS = {
    MatchStatus.RESULT_CONFIRMED: "결과 확정",
    MatchStatus.SETTLED: "정산 완료",
    MatchStatus.CANCELLED: "취소",
    MatchStatus.VOIDED: "무효",
}
_ROUND_STATUS_LABELS = {
    Win5RoundStatus.SETUP: "준비",
    Win5RoundStatus.OPEN: "접수 중",
    Win5RoundStatus.CLOSED: "마감",
    Win5RoundStatus.SCORED: "채점 완료",
    Win5RoundStatus.CANCELLED: "취소",
}
_SUBMISSION_STATUS_LABELS = {
    Win5SubmissionStatus.ACCEPTED: "접수",
    Win5SubmissionStatus.CANCELLED: "제출 취소",
}
_REQUEST_STATUS_LABELS = {
    RegistrationRequestStatus.PENDING: "검토 중",
    RegistrationRequestStatus.APPROVED: "승인",
    RegistrationRequestStatus.CANCELLED: "취소/반려",
}


def _decimal_one(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _page_label(page: int, total_count: int) -> str:
    total_pages = max(1, (total_count + 4) // 5)
    return f"{page + 1}/{total_pages}"


def _registration_lines(overview: AccountStatusOverview | AccountIdentityDetails) -> list[str]:
    request = overview.registration_request
    if request is None:
        return []
    line = (
        f"- 등록 요청: {_REQUEST_STATUS_LABELS[request.status]} · {request.game_region.value} · "
        f"{request.pid_hint} · {safe_discord_text(request.nickname, limit=80)}"
    )
    lines = [line]
    if request.reason:
        lines.append(f"- 처리 사유: {safe_discord_text(request.reason, limit=180)}")
    return lines


def format_account_overview(overview: AccountStatusOverview) -> str:
    """Render the private Account dashboard landing tab."""

    lines = ["## 내 계정 상태"]
    if overview.persona_id is None:
        lines.extend(
            [
                "- Persona: 연결되지 않음",
                f"- 이용 상태: {_ELIGIBILITY_LABELS[overview.eligibility]}",
            ]
        )
        lines.extend(_registration_lines(overview))
        if overview.registration_request is None:
            lines.append("`/account register`로 PID GameAccount 등록 검토를 요청할 수 있습니다.")
        return bounded_discord_message(lines, limit=3800)

    assert overview.persona_status is not None
    lines.extend(
        [
            f"- Persona: {safe_discord_text(overview.display_name or '-', limit=100)}",
            f"- 상태: {_PERSONA_STATUS_LABELS[overview.persona_status]}",
            f"- 이용 상태: {_ELIGIBILITY_LABELS[overview.eligibility]}",
            f"- Circle Point: {overview.wallet_balance:,}"
            if overview.wallet_balance is not None
            else "- Circle Point: 확인 필요",
            (f"- GameAccount: {overview.game_account_count}개 (PID 등록 {overview.eligible_game_account_count}개)"),
            "",
            f"### 룸매치 · {overview.match.season_name}",
            (
                f"공식 경기 {overview.match.participated_match_count}회 · Entry {overview.match.entry_count}회 · "
                f"1착 {overview.match.win_count}회 · 3위 이내 {overview.match.top3_count}회"
            ),
            (
                f"평균 착순 {_decimal_one(overview.match.average_rank)} · "
                f"취소/무효 제외 {overview.match.excluded_terminal_match_count}경기"
            ),
            "",
            "### WIN5",
        ]
    )
    if overview.win5 is None:
        lines.append("표시할 active/closed Season이 없습니다.")
    else:
        rank = "순위 없음" if overview.win5.competition_rank is None else f"{overview.win5.competition_rank}위"
        lines.extend(
            [
                f"{safe_discord_text(overview.win5.season_name, limit=100)} · {overview.win5.season_status.value}",
                (
                    f"{overview.win5.season_score}점 · {rank} · "
                    f"제출 {overview.win5.submitted_round_count}회 · "
                    f"채점 {overview.win5.scored_round_count}회"
                ),
            ]
        )
    if overview.persona_status is PersonaStatus.WARNING:
        lines.extend(("", "⚠️ 경고 상태입니다. 현재 이용 가능 여부와 운영진 안내를 확인해 주세요."))
    elif overview.persona_status is PersonaStatus.PENDING_APPROVAL:
        lines.extend(("", f"⚠️ {MEMBER_APPROVAL_PENDING}"))
    lines.extend(_registration_lines(overview))
    return bounded_discord_message(lines, limit=3800)


def format_account_match_history(page: AccountMatchHistoryPage) -> str:
    """Render one five-row private Match history page."""

    lines = [f"## 룸매치 기록 · {page.season_name} · {_page_label(page.page, page.total_count)}"]
    if not page.items:
        lines.append("표시할 현재 반기 결과가 없습니다.")
        return bounded_discord_message(lines, limit=3800)
    for item in page.items:
        rank = "공식 착순 없음" if item.rank is None else f"{item.rank}/{item.field_size}착"
        rating = "Rating 기록 없음" if item.rating_delta is None else f"Rating {item.rating_delta:+.1f}"
        lines.extend(
            [
                "",
                f"### {safe_discord_text(item.match_name, limit=120)}",
                f"{format_discord_datetime(item.scheduled_at)} · {_MATCH_STATUS_LABELS[item.status]}",
                (
                    f"{item.entry_number}번 · {safe_discord_text(item.game_account_nickname, limit=80)} · "
                    f"{safe_discord_text(item.character_name, limit=80)}"
                ),
                f"{rank} · {rating}",
            ]
        )
    return bounded_discord_message(lines, limit=3800)


def format_account_win5_history(page: AccountWin5HistoryPage) -> str:
    """Render one five-row private WIN5 history page."""

    heading = "## WIN5 기록"
    if page.season is not None:
        heading += f" · {safe_discord_text(page.season.season_name, limit=100)}"
    heading += f" · {_page_label(page.page, page.total_count)}"
    lines = [heading]
    if page.season is None:
        lines.append("표시할 active/closed Season이 없습니다.")
        return bounded_discord_message(lines, limit=3800)
    rank = "순위 없음" if page.season.competition_rank is None else f"{page.season.competition_rank}위"
    lines.append(f"현재 {page.season.season_score}점 · {rank}")
    if not page.items:
        lines.append("이 Season의 제출 내역이 없습니다.")
        return bounded_discord_message(lines, limit=3800)
    for item in page.items:
        round_type = "Normal" if item.round_type is Win5RoundType.NORMAL else "Special"
        score = "채점 대기" if item.score_delta is None else f"{item.score_delta:+d}점"
        lines.extend(
            [
                "",
                f"### {safe_discord_text(item.round_name, limit=120)}",
                (
                    f"{round_type} · {_ROUND_STATUS_LABELS[item.round_status]} · "
                    f"{_SUBMISSION_STATUS_LABELS[item.submission_status]} · {score}"
                ),
                f"최근 변경 {format_discord_datetime(item.updated_at)}",
            ]
        )
    return bounded_discord_message(lines, limit=3800)


def format_account_identity(details: AccountIdentityDetails) -> str:
    """Render one bounded private peer-account identity page."""

    lines = [f"## 내 GameAccount · {_page_label(details.page, details.total_count)}"]
    if details.persona_id is None:
        lines.append("연결된 Persona가 없습니다.")
        lines.extend(_registration_lines(details))
        return bounded_discord_message(lines, limit=3800)
    assert details.persona_status is not None
    lines.extend(
        [
            f"Persona: {safe_discord_text(details.display_name or '-', limit=100)}",
            f"상태: {_PERSONA_STATUS_LABELS[details.persona_status]}",
        ]
    )
    if not details.accounts:
        lines.append("연결된 GameAccount가 없습니다.")
    for account in details.accounts:
        pid = account.pid_hint or "PID 미등록"
        affiliation = safe_discord_text(account.affiliation, limit=80) if account.affiliation else "소속 없음"
        rating = (
            "Rating 없음"
            if account.current_rating is None
            else f"Rating {account.current_rating:.1f} · 전체 {account.competition_rank}위"
        )
        lines.extend(
            [
                "",
                f"### {safe_discord_text(account.nickname, limit=100)}",
                f"{account.game_region.value} · {pid} · {affiliation}",
                rating,
            ]
        )
    lines.extend(_registration_lines(details))
    return bounded_discord_message(lines, limit=3800)


def account_status_internal_error(reference_id: str) -> str:
    return f"계정 상태를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def registration_pending(request_id: int) -> str:
    return (
        f"이미 검토 중인 계정 등록 요청이 있습니다. 요청 ID: `{request_id}`\n"
        "`/account status`에서 현재 상태를 확인해 주세요."
    )


def registration_internal_error(reference_id: str) -> str:
    return f"계정 등록 요청을 저장하지 못했습니다. 잠시 후 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def format_registration_receipt(
    *,
    request_id: int,
    region: str,
    pid: str,
    nickname: str,
    affiliation: str | None,
    exact_retry: bool,
) -> str:
    pid_hint = f"••••{pid[-4:]}" if len(pid) > 4 else "••••"
    lines = [
        "## 계정 등록 요청 접수",
        f"- 요청 ID: `{request_id}`",
        f"- 리전/PID: {region} · {pid_hint}",
        f"- 닉네임: {safe_discord_text(nickname, limit=100)}",
        f"- 소속: {safe_discord_text(affiliation, limit=100) if affiliation else '없음'}",
        "",
        "운영진 승인 전에는 새 베팅이나 WIN5 제출을 만들거나 정정할 수 없습니다.",
        "처리 상태는 `/account status`에서 확인할 수 있습니다.",
    ]
    if exact_retry:
        lines.append("동일한 요청이 이미 접수되어 기존 결과를 표시했습니다.")
    return bounded_discord_message(lines, limit=3800)
