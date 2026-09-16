"""User-facing copy for Circle Match result submission."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from discord import app_commands

from uma_st2.application.match import MatchResultSubmissionTargetChoice, SavedMatchResultSubmission
from uma_st2.domain.match import MatchStatus

from ..common import bounded_discord_message, safe_discord_text

FINISH_TIME_TYPE_ERROR = "1착 기록은 문자열이어야 합니다."
FINISH_TIME_FORMAT_ERROR = "1착 기록은 `M:SS.d` 또는 total `S.d` 형식으로 입력해 주세요."
FINISH_TIME_POSITIVE_ERROR = "1착 기록은 0보다 커야 합니다."
MARGIN_TOO_LONG = "착차는 16자 이하로 입력해 주세요."
WINNER_MARGIN_ERROR = "1착에는 착차를 입력할 수 없습니다."
REASON_TOO_LONG = "사유는 255자 이하로 입력해 주세요."
ENTRY_ALREADY_ASSIGNED = "선택한 Entry는 이미 배정됐거나 board가 완성되었습니다."
ENTRY_UNAVAILABLE = "선택한 Entry가 현재 Match에 없습니다."
CORRECTION_REQUIRES_COMPLETE = "complete board에서만 Entry별 상세를 수정할 수 있습니다."
RANK_UNAVAILABLE = "수정할 rank가 현재 board에 없습니다."
ENTRY_NUMBER_UNAVAILABLE = "입력한 Entry 번호가 현재 Match에 없습니다."
POPULARITY_RANGE_ERROR = "인기 순위는 1부터 Entry 수 사이여야 합니다."
BOARD_INCOMPLETE = "모든 Entry의 rank를 배정해야 제출할 수 있습니다."

CORRECTION_PLACEHOLDER = "Entry/인기/기록·착차 수정"
ENTRY_NUMBER_LABEL = "Entry 번호"
POPULARITY_LABEL = "인기 순위 (선택)"
WINNER_TIME_LABEL = "1착 기록 M:SS.d 또는 S.d (선택)"
MARGIN_LABEL = "착차 (선택)"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
RESET_LABEL = "전체 초기화"
SUBMIT_LABEL = "Submission 저장"
CANCEL_LABEL = "취소"
ENTRY_NUMBER_ASCII_ERROR = "Entry 번호는 양의 ASCII 정수여야 합니다."
POPULARITY_ASCII_ERROR = "인기 순위는 양의 ASCII 정수 또는 빈칸이어야 합니다."
CORRECTION_REASON_REQUIRED = "확정 결과를 정정하려면 slash option에 사유를 입력해 주세요."
BOUND_SUBMIT_ERROR = "이 결과 Draft를 연 사용자와 서버·채널에서만 저장할 수 있습니다."
SUBMISSION_STARTED = "이 Draft의 저장은 이미 처리 중이거나 완료되었습니다."
REVIEW_REQUIRED = "모든 complete-board page를 확인한 뒤 저장해 주세요."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 결과 Submission에 이미 사용되었습니다."
STALE = "Match Entry 또는 result revision이 변경됐습니다. Draft를 다시 열어 주세요."
REASON_REQUIRED = "확정 결과 정정에는 non-empty 사유가 필요합니다."
UNAVAILABLE = "선택한 Match는 더 이상 결과 Submission을 허용하지 않습니다."
CANCELLED = "룸매치 결과 Submission Draft를 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


@dataclass(frozen=True, slots=True)
class ResultSubmissionEditorEntry:
    entry_number: int
    game_account_name: str
    horse_name: str
    rank: int | None
    popularity_rank: int | None
    finish_time_ms: int | None
    margin: str | None


def match_result_submission_autocomplete_choices(
    targets: tuple[MatchResultSubmissionTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert eligible result targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    status_labels = {
        MatchStatus.BETTING_CLOSED.value: "최초/대기",
        MatchStatus.RESULT_CONFIRMED.value: "확정 정정",
    }
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        primary = safe_discord_text(
            f"{target.match_name} · {status_labels[target.status]} · "
            f"Entry {target.entry_count} · rev {target.next_revision_number}",
            limit=100 - len(suffix),
        )
        choices.append(app_commands.Choice(name=f"{primary}{suffix}", value=target.match_id))
    return choices


def format_match_result_submission_editor(
    *,
    match_name: str,
    status: str,
    next_revision_number: int,
    assigned_count: int,
    total_count: int,
    page_index: int,
    page_count: int,
    entries: tuple[ResultSubmissionEditorEntry, ...],
    is_complete: bool,
    unseen_page_count: int,
    reason: str | None,
) -> str:
    """Render one bounded private editor page from primitive adapter-local facts."""

    lines = [
        "## 룸매치 결과 Submission Draft",
        f"Match: {safe_discord_text(match_name, limit=200)}",
        f"상태/새 revision: `{status}` / `{next_revision_number}`",
        f"배정: {assigned_count}/{total_count} · Entry page {page_index + 1}/{page_count}",
        "",
    ]
    for entry in entries:
        primary = f"{entry.rank}착" if entry.rank is not None else f"다음 {assigned_count + 1}착 후보"
        lines.append(
            f"- {primary}: Entry {entry.entry_number} · "
            f"{safe_discord_text(entry.game_account_name)} · {safe_discord_text(entry.horse_name)}"
        )
        if entry.rank is not None:
            details: list[str] = []
            if entry.popularity_rank is not None:
                details.append(f"인기 {entry.popularity_rank}위")
            if entry.rank == 1 and entry.finish_time_ms is not None:
                details.append(f"기록 {format_match_finish_time(entry.finish_time_ms)}")
            if entry.margin is not None:
                details.append(f"착차 {safe_discord_text(entry.margin, limit=16)}")
            if details:
                lines.append("  " + " · ".join(details))
    if is_complete:
        lines.extend(
            (
                "",
                "Complete board입니다. Entry 수정 selector는 번호 변경 시 기존 rank와 서로 교환합니다.",
                (
                    "모든 page를 확인해 Submission 저장을 활성화해 주세요."
                    if unseen_page_count
                    else "모든 page를 확인했습니다. 저장하면 pending revision만 생성됩니다."
                ),
            )
        )
    else:
        lines.extend(("", "아직 배정되지 않은 Entry를 하나 선택하면 다음 rank에 배정됩니다."))
    if reason is not None:
        lines.append(f"사유: {safe_discord_text(reason, limit=255)}")
    return bounded_discord_message(lines, limit=3500)


def format_match_result_submission_success(result: SavedMatchResultSubmission) -> str:
    """Render the committed private pending revision receipt."""

    lines = [
        "## 룸매치 결과 Submission 저장 완료",
        f"Match: {safe_discord_text(result.match_name, limit=200)}",
        f"Submission: `#{result.submission_id}` · revision `{result.revision_number}` · `{result.status.value}`",
        f"Entry: {len(result.candidate.entries)}명 · source `{result.source_kind.value}`",
        "canonical rank/Match status는 아직 변경하지 않았습니다.",
    ]
    if result.superseded_submission_id is not None:
        lines.append(f"Superseded pending: `#{result.superseded_submission_id}`")
    if result.corrects_confirmed:
        lines.append("기존 confirmed authority는 후속 explicit confirmation 전까지 유지됩니다.")
    return bounded_discord_message(lines, limit=1900)


def assignment_choice_name(*, entry_number: int, game_account_name: str, horse_name: str) -> str:
    return safe_discord_text(f"{entry_number} · {game_account_name} · {horse_name}", limit=100)


def assignment_placeholder(rank: int) -> str:
    return f"{rank}착 Entry 선택"


def correction_choice_name(*, rank: int, entry_number: int, horse_name: str) -> str:
    return safe_discord_text(f"{rank}착 · {entry_number} · {horse_name}", limit=100)


def correction_modal_title(rank: int) -> str:
    return f"{rank}착 Entry 상세 수정"


def open_error(error: object) -> str:
    return f"결과 Submission Draft를 열지 못했습니다: {error}"


def open_internal_error(reference_id: str) -> str:
    return f"결과 Submission Draft를 열지 못했습니다. 참조 ID: `{reference_id}`"


def evidence_error(reference_id: str) -> str:
    return f"결과 Submission evidence를 확인하지 못했습니다. 참조 ID: `{reference_id}`"


def submission_error(error: object) -> str:
    return f"결과 Submission을 저장하지 못했습니다: {error}"


def submission_internal_error(reference_id: str) -> str:
    return f"결과 Submission을 저장하지 못했습니다. 참조 ID: `{reference_id}`"


def format_match_finish_time(value: int | None) -> str:
    """Render one exact 0.1-second millisecond value for operator correction."""

    if value is None:
        return ""
    total_tenths = value // 100
    minutes, remaining_tenths = divmod(total_tenths, 600)
    seconds, tenth = divmod(remaining_tenths, 10)
    return f"{minutes}:{seconds:02d}.{tenth}"
