"""User-facing copy for authoritative Circle Match result confirmation."""

from __future__ import annotations

from uma_st2.application.match import ConfirmedMatchResultSubmission, MatchResultSubmissionTarget

from ..common import bounded_discord_message, safe_discord_text
from .match_staff_result_submission import format_match_finish_time

PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
CONFIRM_LABEL = "결과 확정"
CANCEL_LABEL = "취소"
NO_PAGE = "이동할 결과 page가 없습니다."
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
REVIEW_REQUIRED = "모든 결과 page를 확인한 뒤 확정해 주세요."
CONFIRMATION_STARTED = "이 결과의 확정은 이미 처리 중이거나 완료되었습니다."
PENDING_UNAVAILABLE = "Current pending ResultSubmission이 없습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 결과 확정에 이미 사용되었습니다."
STALE = "Current pending revision이 변경됐습니다. 확인 화면을 다시 열어 주세요."
UNAVAILABLE = "선택한 Submission은 더 이상 확정할 수 없습니다."
CANCELLED = "룸매치 결과 확정을 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


def format_match_result_confirmation(
    target: MatchResultSubmissionTarget,
    *,
    page_index: int,
    viewed_pages: frozenset[int],
    page_size: int,
) -> str:
    """Render one bounded final-confirmation candidate page."""

    pending = target.pending
    if pending is None:
        raise ValueError("Current pending ResultSubmission is unavailable.")
    pages = max(1, (len(target.entries) + page_size - 1) // page_size)
    if not 0 <= page_index < pages:
        raise ValueError("page_index is outside the current result confirmation.")
    if not viewed_pages or any(not 0 <= value < pages for value in viewed_pages):
        raise ValueError("viewed_pages contains an invalid result page.")
    entries_by_id = {entry.entry_id: entry for entry in target.entries}
    start = page_index * page_size
    visible = pending.candidate.entries[start : start + page_size]
    lines = [
        "## 룸매치 결과 최종 확인",
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
    seen = len(viewed_pages)
    lines.extend(
        (
            "",
            f"확인한 page: `{seen}/{pages}`",
            (
                f"기존 confirmed `#{target.confirmed.submission_id}`은 확정 시 superseded됩니다."
                if target.confirmed is not None
                else "현재 confirmed authority는 없습니다."
            ),
            "확정하면 이 complete board가 권위 결과로 저장되고 Match가 `result_confirmed`가 됩니다.",
        )
    )
    if seen < pages:
        lines.append("모든 page를 확인해야 결과 확정이 활성화됩니다.")
    return bounded_discord_message(lines, limit=3500)


def format_match_result_confirmation_success(result: ConfirmedMatchResultSubmission) -> str:
    """Render a committed private confirmation receipt."""

    lines = [
        "## 룸매치 결과 확정 완료",
        f"Match: {safe_discord_text(result.match_name, limit=200)}",
        f"Submission: `#{result.submission_id}` · revision `{result.revision_number}` · `{result.status.value}`",
        f"Match 상태: `{result.match_status.value}` · source `{result.source_kind.value}`",
        f"확정 Entry: `{len(result.candidate.entries)}` · confirmed at `{result.confirmed_at.isoformat()}`",
    ]
    if result.candidate.finish_time_ms is not None:
        lines.append(f"1착 기록: `{format_match_finish_time(result.candidate.finish_time_ms)}`")
    if result.previous_confirmed_submission_id is not None:
        lines.append(f"Superseded confirmed: `#{result.previous_confirmed_submission_id}`")
    return bounded_discord_message(lines, limit=1900)


def open_error(error: object) -> str:
    return f"결과 확인 화면을 열지 못했습니다: {error}"


def open_internal_error(reference_id: str) -> str:
    return f"결과 확인 화면을 열지 못했습니다. 참조 ID: `{reference_id}`"


def evidence_error(reference_id: str) -> str:
    return f"결과 확정 evidence를 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def confirmation_error(error: object) -> str:
    return f"결과를 확정하지 못했습니다: {error}"


def confirmation_internal_error(reference_id: str) -> str:
    return f"결과를 확정하지 못했습니다. 참조 ID: `{reference_id}`"
