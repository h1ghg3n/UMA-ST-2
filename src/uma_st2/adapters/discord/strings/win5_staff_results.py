"""User-facing copy and pure formatters for WIN5 authoritative Result flows."""

from __future__ import annotations

from collections import Counter

import discord

from uma_st2.application.win5 import (
    SavedNormalWin5Result,
    SavedSpecialWin5Result,
    Win5NormalResultAuditError,
    Win5NormalResultCommandError,
    Win5NormalResultIdempotencyConflictError,
    Win5NormalResultImmutableError,
    Win5NormalResultInvalidSourceError,
    Win5NormalResultPlacementInput,
    Win5NormalResultUnavailableError,
    Win5NormalResultVersionConflictError,
    Win5SpecialResultAuditError,
    Win5SpecialResultCommandError,
    Win5SpecialResultIdempotencyConflictError,
    Win5SpecialResultImmutableError,
    Win5SpecialResultInvalidSourceError,
    Win5SpecialResultUnavailableError,
    Win5SpecialResultVersionConflictError,
    Win5SpecialResultWinnerInput,
)
from uma_st2.application.win5.staff_result_queries import (
    Win5NormalResultTarget,
    Win5NormalResultTargetChoice,
    Win5NormalResultTargetMode,
    Win5SpecialResultRace,
    Win5SpecialResultTarget,
    Win5SpecialResultTargetChoice,
    Win5SpecialResultTargetMode,
    Win5StaffResultInvalidSourceError,
    Win5StaffResultQueryError,
    Win5StaffResultTargetUnavailableError,
)

from ..common import bounded_discord_message, safe_discord_text

NORMAL_GATE_INPUT_FORMAT = "1착부터 5착까지 게이트 번호 다섯 개를 쉼표 또는 하이픈으로 입력해 주세요."
NORMAL_GATE_DISTINCT = "결과 게이트 번호는 서로 다른 양의 정수 다섯 개여야 합니다."
SPECIAL_GATE_LINES = "Race 순서대로 우승 게이트 번호를 한 줄에 하나씩 입력해 주세요."
SPECIAL_GATE_POSITIVE = "우승 게이트 번호는 양의 정수여야 합니다."
GATE_NOT_IN_ENTRIES = "입력한 게이트 번호 중 현재 경기 엔트리에 없는 값이 있습니다."
NORMAL_TARGET_PLACEHOLDER = "결과 대상 라운드·경기 선택"
SPECIAL_TARGET_PLACEHOLDER = "특별 결과 대상 라운드 선택"
NORMAL_ORDER_LABEL = "1착부터 5착 게이트 번호"
NORMAL_ORDER_PLACEHOLDER = "예: 1-3-5-2-4"
OPERATOR_MEMO_LABEL = "운영 메모 (선택)"
CONFIRM_LABEL = "확정 저장"
CANCEL_LABEL = "취소"
SPECIAL_OPEN_MODAL_LABEL = "우승 게이트 입력"
SPECIAL_GATE_PLACEHOLDER = "예: 3\n7\n1"
NORMAL_TARGET_SELECTION_INVALID = "결과 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
NORMAL_TARGET_NOT_ON_PAGE = "선택한 결과 대상이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
SPECIAL_TARGET_SELECTION_INVALID = "특별 결과 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
SPECIAL_TARGET_NOT_ON_PAGE = "선택한 특별 결과 대상이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
BOUND_MODAL_SUBMIT = "이 입력 창을 연 사용자와 서버·채널에서만 제출할 수 있습니다."
BOUND_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 확정할 수 있습니다."
NORMAL_CANCELLED = "WIN5 결과 저장을 취소했습니다."
SPECIAL_CANCELLED = "WIN5 특별 결과 저장을 취소했습니다."
NORMAL_CONFIRM_ALREADY_STARTED = "이 일반 결과 확인은 이미 처리 중이거나 완료되었습니다."
NORMAL_RESULT_TRANSITION_ERROR = "WIN5 일반 결과 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
SPECIAL_CONFIRM_ALREADY_STARTED = "이 특별 결과 확인은 이미 처리 중이거나 완료되었습니다."
SPECIAL_RESULT_TRANSITION_ERROR = "WIN5 특별 결과 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."


def special_gate_count(expected_count: int) -> str:
    return f"Race {expected_count}개 순서대로 양의 정수 게이트 번호를 한 줄에 하나씩 입력해 주세요."


def normal_target_options(
    choices: tuple[Win5NormalResultTargetChoice, ...],
) -> list[discord.SelectOption]:
    label_keys = tuple((choice.round_name, choice.race_name) for choice in choices)
    duplicate_counts = Counter(label_keys)
    options: list[discord.SelectOption] = []
    for choice, label_key in zip(choices, label_keys, strict=True):
        suffix = f" · ID {choice.round_id}" if duplicate_counts[label_key] > 1 else ""
        base_limit = max(1, 100 - len(suffix))
        base = safe_discord_text(f"{choice.round_name} · {choice.race_name}", limit=base_limit)
        options.append(discord.SelectOption(label=f"{base}{suffix}", value=str(choice.round_id)))
    return options


def special_target_options(
    choices: tuple[Win5SpecialResultTargetChoice, ...],
) -> list[discord.SelectOption]:
    duplicate_counts = Counter(choice.round_name for choice in choices)
    options: list[discord.SelectOption] = []
    for choice in choices:
        suffix = f" · ID {choice.round_id}" if duplicate_counts[choice.round_name] > 1 else ""
        base_limit = max(1, 100 - len(suffix))
        description = (
            f"Race {choice.race_count}개"
            if choice.void_count == 0
            else f"Race {choice.race_count}개 · VOID {choice.void_count}개"
        )
        options.append(
            discord.SelectOption(
                label=f"{safe_discord_text(choice.round_name, limit=base_limit)}{suffix}",
                value=str(choice.round_id),
                description=description,
            )
        )
    return options


def special_void_label(race: Win5SpecialResultRace) -> str:
    if not race.is_void or race.void_reason is None:
        raise ValueError("Special void label requires a complete explicit void fact.")
    return f"VOID — {safe_discord_text(race.void_reason, limit=255)}"


def special_gate_label(race: Win5SpecialResultRace, gate_number: int) -> str:
    reference = next((entry for entry in race.entries if entry.gate_number == gate_number), None)
    if reference is None:
        return str(gate_number)
    return f"{gate_number} · {safe_discord_text(reference.name)}"


def format_normal_preview(
    *,
    target: Win5NormalResultTarget,
    placements: tuple[Win5NormalResultPlacementInput, ...],
    reason: str | None,
) -> str:
    entry_by_id = {entry.id: entry for entry in target.entries}
    desired_by_position = {placement.position: placement for placement in placements}
    mode_label = "정정" if target.mode == Win5NormalResultTargetMode.CORRECTION else "입력"
    lines = [
        f"WIN5 일반 결과 {mode_label} 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"경기: {safe_discord_text(target.race_name)}",
    ]
    if target.current_placements:
        lines.append("현재 결과:")
        for placement in target.current_placements:
            entry = entry_by_id[placement.race_entry_id]
            lines.append(f"- {placement.position}착: {entry.gate_number} · {safe_discord_text(entry.name)}")
    else:
        lines.append("현재 결과: 없음")
    lines.append("저장할 결과:")
    for position in range(1, 6):
        desired = desired_by_position[position]
        entry = entry_by_id[desired.race_entry_id]
        lines.append(f"- {position}착: {entry.gate_number} · {safe_discord_text(entry.name)}")
    if reason is not None:
        lines.append(f"운영 메모: {safe_discord_text(reason, limit=255)}")
    lines.append("확정하면 이 전체 결과가 권위 결과로 저장됩니다.")
    return bounded_discord_message(lines)


def format_normal_success(*, target: Win5NormalResultTarget, result: SavedNormalWin5Result) -> str:
    if result.operation_type is None:
        action = "변경 없음"
    elif result.operation_type.value == "normal_result_entered":
        action = "입력 완료"
    else:
        action = "정정 완료"
    entry_by_id = {entry.id: entry for entry in target.entries}
    order = "-".join(str(entry_by_id[item.race_entry_id].gate_number) for item in result.placements)
    return bounded_discord_message(
        (
            f"WIN5 일반 결과 {action}",
            f"라운드: {safe_discord_text(target.round_name)}",
            f"경기: {safe_discord_text(target.race_name)}",
            f"1~5착: {order}",
        )
    )


def format_special_editor(target: Win5SpecialResultTarget) -> str:
    current_by_race = {winner.race_id: winner for winner in target.current_winners}
    non_void_ids = {race.id for race in target.non_void_races}
    if current_by_race and set(current_by_race) != non_void_ids:
        raise ValueError("Current Special result does not cover every non-void Race.")
    mode_label = "정정" if target.mode == Win5SpecialResultTargetMode.CORRECTION else "입력"
    lines = [
        f"WIN5 특별 결과 {mode_label}",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"입력 대상: 정상 Race {len(target.non_void_races)}개 · VOID {target.void_count}개",
        "Race 상태:",
    ]
    input_index = 0
    for race in target.races:
        race_name = safe_discord_text(race.name, limit=200)
        if race.is_void:
            lines.append(f"- {race_name}: {special_void_label(race)}")
            continue
        input_index += 1
        winner = current_by_race.get(race.id)
        current = "미입력" if winner is None else special_gate_label(race, winner.gate_number)
        lines.append(f"- {race_name}: 입력 #{input_index} · 현재 {current}")
    lines.append("아래 버튼에서 입력 번호 순서대로 non-void Race의 우승 게이트만 입력합니다.")
    message = "\n".join(lines)
    if len(message) > 1900:
        raise ValueError("전체 Special Race 상태를 한 화면에 표시할 수 없어 입력을 진행하지 않습니다.")
    return message


def format_special_preview(
    *,
    target: Win5SpecialResultTarget,
    winners: tuple[Win5SpecialResultWinnerInput, ...],
    reason: str | None,
) -> str:
    current_by_race = {winner.race_id: winner for winner in target.current_winners}
    desired_by_race = {winner.race_id: winner for winner in winners}
    non_void_ids = {race.id for race in target.non_void_races}
    if set(desired_by_race) != non_void_ids or (current_by_race and set(current_by_race) != non_void_ids):
        raise ValueError("Special result preview must cover every non-void Race and no void Race.")
    mode_label = "정정" if target.mode == Win5SpecialResultTargetMode.CORRECTION else "입력"
    lines = [
        f"WIN5 특별 결과 {mode_label} 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
    ]
    if current_by_race:
        lines.append("현재 결과:")
        for race in target.races:
            if race.is_void:
                lines.append(f"- {safe_discord_text(race.name, limit=200)}: {special_void_label(race)}")
                continue
            winner = current_by_race[race.id]
            lines.append(f"- {safe_discord_text(race.name, limit=200)}: {special_gate_label(race, winner.gate_number)}")
    else:
        lines.append("현재 결과: 없음")
    lines.append("저장할 결과:")
    for race in target.races:
        if race.is_void:
            lines.append(f"- {safe_discord_text(race.name, limit=200)}: {special_void_label(race)}")
            continue
        winner = desired_by_race[race.id]
        lines.append(f"- {safe_discord_text(race.name, limit=200)}: {special_gate_label(race, winner.gate_number)}")
    if reason is not None:
        lines.append(f"운영 메모: {safe_discord_text(reason, limit=255)}")
    lines.append("확정하면 모든 non-void Race의 우승 게이트가 하나의 권위 결과 묶음으로 저장됩니다.")
    message = "\n".join(lines)
    if len(message) > 1900:
        raise ValueError("전체 Special 결과를 한 화면에 표시할 수 없어 저장을 진행하지 않습니다.")
    return message


def format_special_success(*, target: Win5SpecialResultTarget, result: SavedSpecialWin5Result) -> str:
    if result.operation_type is None:
        action = "변경 없음"
    elif result.operation_type.value == "special_result_entered":
        action = "입력 완료"
    else:
        action = "정정 완료"
    gates = "-".join(str(winner.gate_number) for winner in result.winners)
    return bounded_discord_message(
        (
            f"WIN5 특별 결과 {action}",
            f"라운드: {safe_discord_text(target.round_name)}",
            f"non-void Race 순서별 우승 게이트: {gates}",
            f"VOID Race: {target.void_count}개",
        )
    )


def normal_command_error_message(error: Win5NormalResultCommandError) -> str:
    if isinstance(error, Win5NormalResultVersionConflictError):
        detail = "미리보기 이후 결과가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5NormalResultImmutableError):
        detail = "이미 채점된 라운드의 결과는 변경할 수 없습니다."
    elif isinstance(error, Win5NormalResultUnavailableError):
        detail = "선택한 라운드가 더 이상 결과 작업 대상이 아닙니다."
    elif isinstance(error, Win5NormalResultInvalidSourceError):
        detail = "라운드·엔트리·현재 결과 상태가 완전하지 않아 저장하지 않았습니다."
    elif isinstance(error, Win5NormalResultIdempotencyConflictError):
        detail = "같은 요청이 다른 입력에 사용되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5NormalResultAuditError):
        detail = "기존 요청 기록을 안전하게 확인할 수 없습니다. 운영 로그를 확인해 주세요."
    else:
        detail = "결과를 저장할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 결과를 저장하지 못했습니다: {detail}"


def special_command_error_message(error: Win5SpecialResultCommandError) -> str:
    if isinstance(error, Win5SpecialResultVersionConflictError):
        detail = "미리보기 이후 결과가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialResultImmutableError):
        detail = "이미 채점된 라운드의 결과는 변경할 수 없습니다."
    elif isinstance(error, Win5SpecialResultUnavailableError):
        detail = "선택한 라운드가 더 이상 특별 결과 작업 대상이 아닙니다."
    elif isinstance(error, Win5SpecialResultInvalidSourceError):
        detail = "라운드·Race·현재 결과 상태가 완전하지 않아 저장하지 않았습니다."
    elif isinstance(error, Win5SpecialResultIdempotencyConflictError):
        detail = "같은 요청이 다른 입력에 사용되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialResultAuditError):
        detail = "기존 요청 기록을 안전하게 확인할 수 없습니다. 운영 로그를 확인해 주세요."
    else:
        detail = "특별 결과를 저장할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 특별 결과를 저장하지 못했습니다: {detail}"


def normal_target_query_error(error: Win5StaffResultQueryError) -> str:
    if isinstance(error, Win5StaffResultTargetUnavailableError):
        detail = "대상 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5StaffResultInvalidSourceError):
        detail = "엔트리 또는 현재 결과가 불완전하여 작업할 수 없습니다."
    else:
        detail = "대상을 안전하게 확인할 수 없습니다."
    return f"결과 미리보기를 만들지 못했습니다: {detail}"


def special_target_query_error(error: Win5StaffResultQueryError) -> str:
    if isinstance(error, Win5StaffResultTargetUnavailableError):
        detail = "대상 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5StaffResultInvalidSourceError):
        detail = "Race 또는 현재 우승 결과가 불완전하여 작업할 수 없습니다."
    else:
        detail = "대상을 안전하게 확인할 수 없습니다."
    return f"특별 결과 미리보기를 만들지 못했습니다: {detail}"


def normal_modal_title(mode: Win5NormalResultTargetMode) -> str:
    return "WIN5 일반 결과 정정" if mode == Win5NormalResultTargetMode.CORRECTION else "WIN5 일반 결과 입력"


def special_modal_title(mode: Win5SpecialResultTargetMode) -> str:
    return "WIN5 특별 결과 정정" if mode == Win5SpecialResultTargetMode.CORRECTION else "WIN5 특별 결과 입력"


def special_gate_label_text(race_count: int) -> str:
    return f"non-void Race 우승 게이트 ({race_count}개)"


def normal_targets_load_error(reference_id: str) -> str:
    return f"결과 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def special_targets_load_error(reference_id: str) -> str:
    return f"특별 결과 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def no_normal_targets(mode: Win5NormalResultTargetMode) -> str:
    action = "입력" if mode == Win5NormalResultTargetMode.ENTRY else "정정"
    return f"현재 일반 결과를 {action}할 수 있는 라운드가 없습니다."


def no_special_targets(mode: Win5SpecialResultTargetMode) -> str:
    action = "입력" if mode == Win5SpecialResultTargetMode.ENTRY else "정정"
    return f"현재 특별 결과를 {action}할 수 있는 라운드가 없습니다."


NORMAL_TARGET_PROMPT = "결과 대상 라운드·경기를 선택해 주세요."
SPECIAL_TARGET_PROMPT = "특별 결과 대상 라운드를 선택해 주세요."


def normal_preview_error(error: object) -> str:
    return f"결과 미리보기를 만들지 못했습니다: {error}"


def normal_preview_internal_error(reference_id: str) -> str:
    return f"결과 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def special_editor_error(error: object) -> str:
    return f"특별 결과 입력 화면을 만들지 못했습니다: {error}"


def special_editor_internal_error(reference_id: str) -> str:
    return f"특별 결과 입력 화면을 만들지 못했습니다. 참조 ID: `{reference_id}`"


def special_preview_error(error: object) -> str:
    return f"특별 결과 미리보기를 만들지 못했습니다: {error}"


def special_preview_internal_error(reference_id: str) -> str:
    return f"특별 결과 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def normal_save_internal_error(reference_id: str) -> str:
    return f"WIN5 결과를 저장하지 못했습니다. 참조 ID: `{reference_id}`"


def special_save_internal_error(reference_id: str) -> str:
    return f"WIN5 특별 결과를 저장하지 못했습니다. 참조 ID: `{reference_id}`"
