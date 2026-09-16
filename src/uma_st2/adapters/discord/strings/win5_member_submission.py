"""User-facing copy and pure formatters for WIN5 member submissions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from discord import app_commands

from uma_st2.application.win5 import (
    Win5MemberQueryError,
    Win5MemberQueryIdentityError,
    Win5NormalSubmissionEditor,
    Win5NormalSubmissionInvalidSourceError,
    Win5NormalSubmissionRoundChoice,
    Win5NormalSubmissionRoundUnavailableError,
    Win5RaceCard,
    Win5SpecialSubmissionEditor,
    Win5SpecialSubmissionInvalidSourceError,
    Win5SpecialSubmissionRoundChoice,
    Win5SpecialSubmissionRoundUnavailableError,
)
from uma_st2.domain.win5 import Win5SubmissionTier

from ..common import bounded_discord_message, safe_discord_text

CLEAR_PICK_LABEL = "미입력"
CURRENT_SELECTION_DESCRIPTION = "현재 선택"
TIER_PLACEHOLDER = "TOP1 / TOP3 / TOP5 선택"
TIER_INVALID = "Tier 선택값이 올바르지 않습니다. 제출 화면을 다시 열어 주세요."
ENTRY_INVALID = "Entry 선택값이 올바르지 않습니다. 제출 화면을 다시 열어 주세요."
SAVE_LABEL = "변경 저장"
PREVIOUS_LABEL = "이전"
NEXT_LABEL = "다음"
SPECIAL_GATE_PLACEHOLDER = "게이트 번호 (빈 값은 미입력)"
SPECIAL_EDIT_LABEL = "현재 페이지 입력"

TIER_CHANGE_NOTICE = "Tier가 변경되었습니다. 표시 범위를 벗어난 기존 pick은 편집값에서 제외됐습니다."
PICK_UNAVAILABLE = "현재 화면에 없는 position 또는 Entry입니다. 제출 화면을 다시 열어 주세요."
ENTRY_PAGE_INVALID = "Entry 페이지가 더 이상 유효하지 않습니다. 제출 화면을 다시 열어 주세요."
BOUND_SAVE_ERROR = "이 화면을 연 사용자와 서버·채널에서만 저장할 수 있습니다."
EMPTY_SAVE_ERROR = "하나 이상의 pick이 있어야 저장할 수 있습니다. 전체 취소는 `제출 취소`를 사용해 주세요."
SPECIAL_PAGE_INVALID = "Race 페이지가 더 이상 유효하지 않습니다. 제출 화면을 다시 열어 주세요."
SPECIAL_MODAL_STALE = "이미 변경된 화면에서 연 입력 창입니다. 현재 제출 화면에서 다시 입력해 주세요."
SPECIAL_MODAL_TARGET_MISMATCH = "현재 페이지와 입력 대상이 일치하지 않습니다. 제출 화면을 다시 열어 주세요."
SPECIAL_GATE_INTEGER_ERROR = "게이트 번호는 양의 정수로 입력해 주세요. 빈 값은 해당 Race를 미입력으로 둡니다."
SPECIAL_GATE_RANGE_ERROR = "게이트 번호는 저장 가능한 양의 정수 범위여야 합니다."
SPECIAL_PAGE_APPLIED_NOTICE = "현재 페이지 입력을 반영했습니다. 아직 저장되지 않았습니다."

NORMAL_SAVE_STALE = (
    "제출을 저장하지 못했습니다: 화면을 연 뒤 Submission이 변경되었습니다. `/win5 submit`을 다시 열어 주세요."
)
SPECIAL_SAVE_STALE = (
    "제출을 저장하지 못했습니다: 화면을 연 뒤 Submission이 변경되었습니다. `/win5 special-submit`을 다시 열어 주세요."
)
SAVE_UNAVAILABLE = "제출을 저장하지 못했습니다: 라운드 또는 참가 자격이 더 이상 유효하지 않습니다."
NORMAL_ACTION_ALREADY_STARTED = (
    "이 화면의 제출 처리가 이미 시작되었습니다. 완료 후 `/win5 submit`을 다시 확인해 주세요."
)
SPECIAL_ACTION_ALREADY_STARTED = (
    "이 화면의 제출 처리가 이미 시작되었습니다. 완료 후 `/win5 special-submit`을 다시 확인해 주세요."
)
NORMAL_INVALID_DETAIL = "같은 Entry 중복 여부와 현재 Tier의 position 범위를 확인해 주세요."
NORMAL_EMPTY_DETAIL = "하나 이상의 pick을 선택하거나 기존 제출을 명시적으로 취소해 주세요."
SPECIAL_INVALID_DETAIL = "각 Race에는 양의 정수 gate winner를 하나만 입력해 주세요."
SPECIAL_EMPTY_DETAIL = "하나 이상의 pick을 입력하거나 기존 제출을 명시적으로 취소해 주세요."
SUBMISSION_UNAVAILABLE_DETAIL = "accepted Submission을 찾을 수 없습니다. 제출 화면을 다시 열어 주세요."
CURRENT_DRAFT_UNAVAILABLE_DETAIL = "현재 편집값을 저장할 수 없습니다."


def editor_transition_error(*, command_name: str) -> str:
    slash_command = "/" + command_name.replace(".", " ")
    return f"WIN5 화면을 갱신하지 못했습니다. `{slash_command}`을 다시 열어 주세요."


def entry_placeholder(position: int) -> str:
    return f"{position}착 Entry 선택"


def page_label(*, page: int, page_count: int) -> str:
    return f"{page + 1} / {page_count}"


def special_modal_title(*, page: int, page_count: int) -> str:
    return f"Special picks {page + 1}/{page_count}"


def special_race_input_label(race: Win5RaceCard) -> str:
    return safe_discord_text(race.name, limit=45) or f"Race {race.id}"


def normal_submission_round_choices(
    choices: tuple[Win5NormalSubmissionRoundChoice, ...],
) -> list[app_commands.Choice[int]]:
    label_keys = tuple((choice.round_name, choice.race_name) for choice in choices)
    duplicate_counts = Counter(label_keys)
    results: list[app_commands.Choice[int]] = []
    for choice, label_key in zip(choices, label_keys, strict=True):
        suffix = f" · ID {choice.round_id}" if duplicate_counts[label_key] > 1 else ""
        base_limit = max(1, 100 - len(suffix))
        base = safe_discord_text(
            f"{choice.round_name} · {choice.race_name}",
            limit=base_limit,
        )
        results.append(
            app_commands.Choice(
                name=f"{base}{suffix}",
                value=choice.round_id,
            )
        )
    return results


def special_submission_round_choices(
    choices: tuple[Win5SpecialSubmissionRoundChoice, ...],
) -> list[app_commands.Choice[int]]:
    label_keys = tuple((choice.round_name, choice.race_count) for choice in choices)
    duplicate_counts = Counter(label_keys)
    results: list[app_commands.Choice[int]] = []
    for choice, label_key in zip(choices, label_keys, strict=True):
        suffix = f" · {choice.race_count}경기"
        if duplicate_counts[label_key] > 1:
            suffix += f" · ID {choice.round_id}"
        base = safe_discord_text(choice.round_name, limit=max(1, 100 - len(suffix)))
        results.append(app_commands.Choice(name=f"{base}{suffix}", value=choice.round_id))
    return results


def entry_label(*, gate_number: int, name: str) -> str:
    prefix = f"{gate_number} · "
    return f"{prefix}{safe_discord_text(name, limit=max(1, 100 - len(prefix)))}"


def special_pick_label(*, race: Win5RaceCard, gate_number: int) -> str:
    reference = next(
        (entry for entry in race.entries if entry.gate_number == gate_number),
        None,
    )
    if reference is None:
        return f"Gate {gate_number}"
    return f"Gate {gate_number} — {safe_discord_text(reference.name, limit=120)}"


def committed_submission_receipt_notice(*, version: int, unchanged: bool) -> str:
    if unchanged:
        return f"이미 같은 내용으로 접수되어 있습니다 · version {version}"
    return f"접수 성공 · version {version}"


def format_normal_submission_editor_copy(
    editor: Win5NormalSubmissionEditor,
    *,
    tier: Win5SubmissionTier | None,
    picks_by_position: Mapping[int, int],
    required_positions: int,
    page: int,
    page_count: int,
    notice: str | None,
) -> str:
    entry_by_id = {entry.id: entry for entry in editor.entries}
    tier_label = tier.value if tier is not None else "선택 필요"
    lines = [
        "## WIN5 Normal Submission",
        f"시즌: {safe_discord_text(editor.season_name)}",
        f"라운드: {safe_discord_text(editor.round_name)}",
        f"경기: {safe_discord_text(editor.race_name)}",
        f"Tier: {tier_label}",
    ]
    if tier is None:
        lines.append("Tier를 먼저 선택해 주세요.")
    else:
        lines.append("현재 편집값:")
        for position in range(1, required_positions + 1):
            entry_id = picks_by_position.get(position)
            if entry_id is None:
                value = "—"
            else:
                entry = entry_by_id[entry_id]
                value = entry_label(gate_number=entry.gate_number, name=entry.name)
            lines.append(f"- {position}착: {value}")
        lines.append(f"입력: {len(picks_by_position)} / {required_positions}")
    if page_count > 1:
        lines.append(f"엔트리 페이지: {page + 1} / {page_count}")
    if editor.submission is None:
        lines.append("저장된 제출: 없음")
    else:
        lines.append(f"저장된 제출: version {editor.submission.version}")
    if notice is not None:
        lines.append(f"**{safe_discord_text(notice, limit=500)}**")
    lines.append("미입력 position은 저장하지 않으며 마감 시 해당 position만 MISS입니다.")
    if editor.submission is not None:
        lines.append("모든 pick을 없애려면 빈 상태를 저장하지 말고 `제출 취소`를 사용해 주세요.")
    return bounded_discord_message(lines, limit=3500)


def format_special_submission_editor_copy(
    editor: Win5SpecialSubmissionEditor,
    *,
    page_races: Sequence[Win5RaceCard],
    picks_by_race_id: Mapping[int, int],
    page: int,
    page_count: int,
    notice: str | None,
) -> str:
    lines = [
        "## WIN5 Special Submission",
        f"시즌: {safe_discord_text(editor.season_name)}",
        f"라운드: {safe_discord_text(editor.round_name)}",
        f"Race 페이지: {page + 1} / {page_count}",
        "현재 편집값:",
    ]
    for race in page_races:
        gate_number = picks_by_race_id.get(race.id)
        value = "—" if gate_number is None else special_pick_label(race=race, gate_number=gate_number)
        lines.append(f"- {safe_discord_text(race.name)}: {value}")
    lines.append(f"입력: {len(picks_by_race_id)} / {len(editor.races)}")
    if editor.submission is None:
        lines.append("저장된 제출: 없음")
    else:
        lines.append(f"저장된 제출: version {editor.submission.version}")
    if notice is not None:
        lines.append(f"**{safe_discord_text(notice, limit=500)}**")
    lines.extend(
        (
            "표시되는 말 이름은 게이트 reference이며, 게이트 번호 입력을 제한하지 않습니다.",
            "미입력 Race는 저장하지 않으며 마감 시 해당 Race만 MISS입니다.",
        )
    )
    if editor.submission is not None:
        lines.append("모든 pick을 없애려면 빈 상태를 저장하지 말고 `제출 취소`를 사용해 주세요.")
    return bounded_discord_message(lines, limit=3500)


def normal_editor_query_error_message(error: Win5MemberQueryError) -> str:
    if isinstance(error, Win5MemberQueryIdentityError):
        detail = "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다."
    elif isinstance(error, Win5NormalSubmissionRoundUnavailableError):
        detail = "선택한 일반 라운드가 더 이상 열려 있지 않습니다."
    elif isinstance(error, Win5NormalSubmissionInvalidSourceError):
        detail = "라운드·Entry·현재 제출 상태를 안전하게 표시할 수 없습니다. 운영진에게 알려 주세요."
    else:
        detail = "제출 화면을 열 수 없습니다."
    return f"WIN5 제출 화면을 열지 못했습니다: {detail}"


def special_editor_query_error_message(error: Win5MemberQueryError) -> str:
    if isinstance(error, Win5MemberQueryIdentityError):
        detail = "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다."
    elif isinstance(error, Win5SpecialSubmissionRoundUnavailableError):
        detail = "선택한 Special 라운드가 더 이상 열려 있지 않습니다."
    elif isinstance(error, Win5SpecialSubmissionInvalidSourceError):
        detail = "라운드·Race·현재 제출 상태를 안전하게 표시할 수 없습니다. 운영진에게 알려 주세요."
    else:
        detail = "Special 제출 화면을 열 수 없습니다."
    return f"WIN5 Special 제출 화면을 열지 못했습니다: {detail}"


def save_notice(detail: str) -> str:
    return f"저장 실패: {detail}"


def normal_save_internal_error(reference_id: str) -> str:
    return f"WIN5 제출을 저장하지 못했습니다. 참조 ID: `{reference_id}`"


def special_save_internal_error(reference_id: str) -> str:
    return f"WIN5 Special 제출을 저장하지 못했습니다. 참조 ID: `{reference_id}`"
