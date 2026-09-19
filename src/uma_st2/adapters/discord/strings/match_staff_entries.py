"""User-facing copy for Circle Match Entry-roster replacement."""

from __future__ import annotations

from collections import Counter

from discord import app_commands

from uma_st2.application.match import (
    MatchEntryCandidateDraft,
    MatchEntryDraftEntry,
    MatchEntryReplacementDraft,
    MatchEntryRosterSnapshot,
    MatchEntrySnapshot,
    MatchEntryTargetChoice,
    ReplacedMatchEntries,
)
from uma_st2.domain.match import MATCH_ENTRY_MAXIMUM_COUNT

from ..common import bounded_discord_message, safe_discord_text
from ..datetime_codec import format_discord_datetime

INPUT_TYPE_ERROR = "Entry 입력은 문자열이어야 합니다."
INPUT_EMPTY_ERROR = "Entry를 한 줄 이상 입력해 주세요."
REASON_TOO_LONG = "사유는 255자 이하여야 합니다."
INPUT_BUTTON_LABEL = "Entry 입력"
MODAL_TITLE = "룸매치 Entry 전체 입력"
MODAL_LABEL = "게임계정 일부 혹은 전체"
MODAL_PLACEHOLDER = "계정 닉네임 일부\n다른 계정 닉네임 전체"
SEARCH_CORRECTION_TITLE = "Entry 검색어 수정"
SEARCH_CORRECTION_LABEL = "게임계정 검색어"
ACCOUNT_SEARCH_LABEL = "계정 검색어 수정"
NO_MATCHING_ACCOUNT = "일치하는 계정 없음"
REENTRY_LABEL = "전체 다시 입력"
REOPEN_ENTRY = "Entry 화면을 갱신하지 못했습니다. `/match staff race`를 다시 열어 주세요."
CANDIDATE_CONFIRM_LABEL = "선택 내용 검토"
CANDIDATE_ACCOUNT_PLACEHOLDER = "GameAccount를 선택하세요"
CANDIDATE_CHARACTER_PLACEHOLDER = "우마무스메를 선택하세요"
CANDIDATE_CHARACTER_PREVIOUS_LABEL = "이전 캐릭터"
CANDIDATE_CHARACTER_NEXT_LABEL = "다음 캐릭터"
CANDIDATE_SELECT_LABEL = "선택"
CONFIRM_LABEL = "Entry 교체 확정"
CANCEL_LABEL = "취소"
BACK_LABEL = "뒤로"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
BOUND_SUBMIT_ERROR = "이 입력 창을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
BOUND_CONFIRM_ERROR = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
REVIEW_REQUIRED = "모든 Entry 페이지를 확인한 뒤 확정해 주세요."
CONFIRMATION_STARTED = "이 Preview의 확정은 이미 처리 중이거나 완료되었습니다."
IDEMPOTENCY_CONFLICT = "동일 요청 키가 다른 Entry 교체 내용에 이미 사용되었습니다."
STALE = "Entry roster가 Preview 이후 변경되었습니다. 명령을 다시 열어 주세요."
UNAVAILABLE = "선택한 Match가 더 이상 Entry 교체를 허용하지 않습니다."
CANCELLED = "룸매치 Entry 교체를 취소했습니다. DB에는 기록되지 않았습니다."
BOUND_INTERACTION_ERROR = "이 화면을 연 사용자와 서버·채널에서만 조작할 수 있습니다."


def entry_line_invalid_error(line_number: int, error: object) -> str:
    return f"{line_number}번째 줄이 유효하지 않습니다: {error}"


def match_entry_autocomplete_choices(
    targets: tuple[MatchEntryTargetChoice, ...],
) -> list[app_commands.Choice[int]]:
    """Convert eligible targets into bounded mention-safe choices."""

    duplicate_names = {name for name, count in Counter(target.match_name for target in targets).items() if count > 1}
    choices: list[app_commands.Choice[int]] = []
    for target in targets:
        suffix = f" · ID {target.match_id}" if target.match_name in duplicate_names else ""
        label = safe_discord_text(
            f"{target.match_name} · {target.grade.value} · "
            f"{format_discord_datetime(target.scheduled_at)} · Entry {target.current_entry_count}명{suffix}",
            limit=100,
        )
        choices.append(app_commands.Choice(name=label, value=target.match_id))
    return choices


def format_match_entry_input(target: MatchEntryRosterSnapshot, *, notice: str | None = None) -> str:
    """Render the zero-write bulk-input launcher."""

    lines = [
        "## 룸매치 엔트리 입력/수정",
        f"Match: {safe_discord_text(target.match_name, limit=200)}",
        f"현재 Entry: {len(target.entries)}명",
        "`게임계정 일부 혹은 전체`에 GameAccount nickname 일부 또는 전체를 한 줄씩 입력합니다. "
        f"빈 줄은 무시하며 최대 {MATCH_ENTRY_MAXIMUM_COUNT}명입니다.",
        "각 줄은 Entry 번호가 됩니다. 다음 화면의 Entry 버튼에서 GameAccount와 우마무스메를 선택합니다.",
        "PID와 우마무스메 이름은 이 입력란에 넣지 않습니다.",
        "입력 제출만으로 DB가 변경되지 않으며, Preview 최종 확정 때만 전체 roster를 교체합니다.",
    ]
    if notice is not None:
        lines.append(f"안내: {safe_discord_text(notice, limit=500)}")
    return bounded_discord_message(lines, limit=3500)


def format_match_entry_candidate_roster(
    draft: MatchEntryCandidateDraft,
    *,
    selected_account_ids: tuple[int | None, ...],
    selected_character_identities: tuple[tuple[int, int | None] | None, ...],
    complete: bool,
    notice: str | None = None,
) -> str:
    """Render the complete Entry-button roster and selection status."""

    lines = [
        "## 룸매치 Entry 설정",
        f"Match: {safe_discord_text(draft.current.match_name, limit=200)}",
        "각 엔트리 버튼을 누르면 수정 혹은 선택 창이 뜹니다.",
    ]
    for row, account_id, character_identity in zip(
        draft.rows,
        selected_account_ids,
        selected_character_identities,
        strict=True,
    ):
        if not row.accounts:
            lines.append(
                f"{row.entry_number}번 엔트리 : {NO_MATCHING_ACCOUNT} "
                f"· 검색어 {safe_discord_text(row.search.account_chunk, limit=80)}"
            )
            continue
        account = next((item for item in row.accounts if item.id == account_id), None)
        character = next((item for item in draft.characters if item.identity == character_identity), None)
        account_name = "계정 미선택" if account is None else account.nickname
        character_name = "우마무스메 미선택" if character is None else character.display_name
        lines.append(
            f"{row.entry_number}. {safe_discord_text(account_name, limit=80)} · "
            f"{safe_discord_text(character_name, limit=80)}"
        )
    if notice is not None:
        lines.append(f"안내: {safe_discord_text(notice, limit=500)}")
    lines.append(
        "모든 Entry 선택이 끝났습니다. 선택 내용 검토로 진행하세요."
        if complete
        else "모든 Entry에서 두 항목을 선택해야 검토로 진행할 수 있습니다."
    )
    return bounded_discord_message(lines, limit=3500)


def format_match_entry_candidate_detail(
    draft: MatchEntryCandidateDraft,
    *,
    entry_index: int,
    selected_account_id: int | None,
    selected_character_identity: tuple[int, int | None] | None,
    character_page: int,
    character_page_count: int,
) -> str:
    """Render one Entry's selected account information and Umamusume page."""

    row = draft.rows[entry_index]
    account = next((item for item in row.accounts if item.id == selected_account_id), None)
    character = next((item for item in draft.characters if item.identity == selected_character_identity), None)
    account_text = "미선택"
    if account is not None:
        account_text = f"{account.nickname} · {account.game_region.value} · 소속 {account.affiliation or '없음'}"
    lines = [
        f"## Entry {row.entry_number} 설정",
        f"GameAccount 검색: {safe_discord_text(row.search.account_chunk, limit=100)}",
        f"GameAccount 후보: {len(row.accounts)}개",
        f"선택 GameAccount: {safe_discord_text(account_text, limit=220)}",
        "선택 우마무스메: " + ("미선택" if character is None else safe_discord_text(character.display_name, limit=100)),
        f"우마무스메 목록: {character_page + 1}/{character_page_count}",
    ]
    return bounded_discord_message(lines, limit=3500)


def _format_current(entry: MatchEntrySnapshot | None) -> str:
    if entry is None:
        return "없음"
    return safe_discord_text(
        f"{entry.game_account_name} · {entry.umamusume_name}",
        limit=180,
    )


def _format_desired(entry: MatchEntryDraftEntry | None) -> str:
    if entry is None:
        return "없음"
    return safe_discord_text(
        f"{entry.account.nickname} · {entry.character.display_name}",
        limit=180,
    )


def format_match_entry_draft(
    *,
    draft: MatchEntryReplacementDraft,
    page: int,
    page_count: int,
    all_pages_reviewed: bool,
    page_size: int,
    notice: str | None = None,
) -> str:
    """Render one bounded PID-free before/after roster page."""

    start = page * page_size
    stop = start + page_size
    current = draft.current.entries
    desired = draft.desired_entries
    lines = [
        "## 룸매치 Entry 교체 Preview",
        f"Match: {safe_discord_text(draft.current.match_name, limit=200)}",
        f"현재 {len(current)}명 → 변경 후 {len(desired)}명",
        f"페이지: {page + 1}/{page_count}",
    ]
    for index in range(start, min(stop, max(len(current), len(desired)))):
        old = current[index] if index < len(current) else None
        new = desired[index] if index < len(desired) else None
        lines.append(f"{index + 1}. {_format_current(old)} → {_format_desired(new)}")
    if notice is not None:
        lines.append(f"안내: {safe_discord_text(notice, limit=500)}")
    if all_pages_reviewed:
        lines.append("모든 페이지를 확인했습니다. 최종 확정 시 현재 roster와 대상 master를 다시 검사합니다.")
    else:
        lines.append("모든 Entry 페이지를 확인해야 최종 확정할 수 있습니다.")
    return bounded_discord_message(lines, limit=3500)


def format_match_entry_success(result: ReplacedMatchEntries) -> str:
    """Render the committed PID-free replacement receipt."""

    lines = [
        "## 룸매치 Entry 저장 완료",
        f"Match: {safe_discord_text(result.snapshot.match_name, limit=200)}",
        f"Entry: {len(result.snapshot.entries)}명",
    ]
    lines.extend(
        f"{entry.entry_number}. {safe_discord_text(entry.game_account_name, limit=100)} · "
        f"{safe_discord_text(entry.umamusume_name, limit=100)}"
        for entry in result.snapshot.entries
    )
    return bounded_discord_message(lines, limit=3500)


def input_open_error(error: object) -> str:
    return f"Entry 입력 화면을 열지 못했습니다: {error}"


def input_open_internal_error(reference_id: str) -> str:
    return f"Entry 입력 화면을 열지 못했습니다. 참조 ID: `{reference_id}`"


def reentry_notice(error: object) -> str:
    return f"전체 다시 입력을 적용하지 않았습니다: {error}"


def input_notice(error: object) -> str:
    return f"입력을 적용하지 않았습니다: {error}"


def input_internal_error(reference_id: str) -> str:
    return f"입력을 처리하지 못했습니다. 기존 Entry 입력 화면에서 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def preview_internal_error(reference_id: str) -> str:
    return f"Entry 검토 화면을 준비하지 못했습니다. 현재 선택 화면에서 다시 시도해 주세요. 참조 ID: `{reference_id}`"


def replacement_error(error: object) -> str:
    return f"Entry를 교체하지 못했습니다: {error}"


def replacement_internal_error(reference_id: str) -> str:
    return f"Entry를 교체하지 못했습니다. 참조 ID: `{reference_id}`"
