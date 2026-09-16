"""User-facing copy for Circle Match result review and rejection."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import (
    MatchResultReviewTargetChoice,
    MatchResultSubmissionTarget,
    RejectedMatchResultSubmission,
)
from uma_st2.domain.match import MatchStatus

from ..common import bounded_discord_message, safe_discord_text
from .match_staff_result_submission import format_match_finish_time

PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CORRECTION_GUIDE_LABEL = "정정 방법"
REJECT_LABEL = "Submission Reject"
CLOSE_LABEL = "닫기"
REJECT_MODAL_TITLE = "ResultSubmission Reject"
REJECT_REASON_LABEL = "Reject 사유"
REJECT_REASON_PLACEHOLDER = "공식 결과와 다른 부분을 입력해 주세요."
CORRECTION_GUIDE = (
    "정정하려면 `/match staff result`의 `결과 입력/정정`에서 같은 Match를 다시 선택해 주세요. "
    "현재 pending candidate가 prefill되며 Final Submit 전에는 DB를 변경하지 않습니다."
)
BOUND_REJECT_ERROR = "이 review를 연 사용자와 서버·채널에서만 reject할 수 있습니다."
REJECTION_STARTED = "이 Submission의 reject는 이미 처리 중이거나 완료되었습니다."
REASON_REQUIRED = "Reject 사유를 입력해 주세요."
PENDING_UNAVAILABLE = "Current pending ResultSubmission이 없습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 ResultSubmission reject에 이미 사용되었습니다."
STALE = "Current pending revision이 변경됐습니다. review를 다시 열어 주세요."
UNAVAILABLE = "선택한 Submission은 더 이상 reject할 수 없습니다."
CLOSED = "ResultSubmission review를 닫았습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


def match_result_review_autocomplete_choices(
    targets: tuple[MatchResultReviewTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert current-pending targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    status_labels = {
        MatchStatus.BETTING_CLOSED: "최초 결과",
        MatchStatus.RESULT_CONFIRMED: "확정 정정",
    }
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · {status_labels[target.match_status]} · "
            f"rev {target.revision_number} · Entry {target.entry_count}",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_result_review(
    target: MatchResultSubmissionTarget,
    *,
    page_index: int,
    page_size: int,
) -> str:
    """Render one private bounded current-pending candidate page."""

    pending = target.pending
    if pending is None:
        raise ValueError("Current pending ResultSubmission is unavailable.")
    pages = max(1, (len(target.entries) + page_size - 1) // page_size)
    if not 0 <= page_index < pages:
        raise ValueError("page_index is outside the current result review.")
    entries_by_id = {entry.entry_id: entry for entry in target.entries}
    start = page_index * page_size
    visible = pending.candidate.entries[start : start + page_size]
    lines = [
        "## 룸매치 ResultSubmission Review",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        (
            f"Submission: `#{pending.submission_id}` · revision `{pending.revision_number}` · "
            f"source `{pending.source_kind.value}`"
        ),
        f"Match 상태: `{target.status.value}` · page {page_index + 1}/{pages}",
        "",
    ]
    for candidate_entry in visible:
        entry = entries_by_id.get(candidate_entry.entry_id)
        if entry is None or entry.entry_number != candidate_entry.entry_number:
            raise ValueError("Pending candidate does not match current Match Entries.")
        details: list[str] = []
        if candidate_entry.popularity_rank is not None:
            details.append(f"인기 {candidate_entry.popularity_rank}위")
        if candidate_entry.rank == 1 and pending.candidate.finish_time_ms is not None:
            details.append(f"기록 {format_match_finish_time(pending.candidate.finish_time_ms)}")
        if candidate_entry.margin is not None:
            details.append(f"착차 {safe_discord_text(candidate_entry.margin, limit=16)}")
        detail_suffix = f" · {' · '.join(details)}" if details else ""
        lines.append(
            f"- {candidate_entry.rank}착 · Entry {candidate_entry.entry_number} · "
            f"{safe_discord_text(entry.game_account_name)} · {safe_discord_text(entry.horse_name)}"
            f"{detail_suffix}"
        )
    lines.extend(
        (
            "",
            (
                f"Existing confirmed `#{target.confirmed.submission_id}`은 reject/정정 중에도 유지됩니다."
                if target.confirmed is not None
                else "아직 confirmed authority는 없습니다."
            ),
            "정정은 `/match staff result`의 `결과 입력/정정`을 열어 새 pending revision으로 저장합니다.",
            "이 review read 자체는 DB 상태나 audit을 만들지 않습니다.",
        )
    )
    return bounded_discord_message(lines, limit=3500)


def format_match_result_rejection_success(result: RejectedMatchResultSubmission) -> str:
    """Render a committed private rejection receipt."""

    lines = [
        "## 룸매치 ResultSubmission Reject 완료",
        f"Match: {safe_discord_text(result.match_name, limit=200)}",
        f"Submission: `#{result.submission_id}` · revision `{result.revision_number}` · `{result.status.value}`",
        f"source: `{result.source_kind.value}` · rejected at `{result.rejected_at.isoformat()}`",
        f"사유: {safe_discord_text(result.reason, limit=255)}",
        "Match status와 canonical result는 변경하지 않았습니다.",
    ]
    if result.preserved_confirmed_submission_id is not None:
        lines.append(f"Preserved confirmed: `#{result.preserved_confirmed_submission_id}`")
    return bounded_discord_message(lines, limit=1900)


def open_error(error: object) -> str:
    return f"ResultSubmission review를 열지 못했습니다: {error}"


def open_internal_error(reference_id: str) -> str:
    return f"ResultSubmission review를 열지 못했습니다. 참조 ID: `{reference_id}`"


def evidence_error(reference_id: str) -> str:
    return f"ResultSubmission reject evidence를 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def rejection_error(error: object) -> str:
    return f"ResultSubmission을 reject하지 못했습니다: {error}"


def rejection_internal_error(reference_id: str) -> str:
    return f"ResultSubmission을 reject하지 못했습니다. 참조 ID: `{reference_id}`"
