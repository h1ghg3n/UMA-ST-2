"""User-facing copy and pure formatters for WIN5 scoring."""

from __future__ import annotations

from collections import Counter

import discord

from uma_st2.application.win5 import (
    ScoredNormalWin5Round,
    ScoredSpecialWin5Round,
    Win5NormalScoringAlreadyCompletedError,
    Win5NormalScoringAuditError,
    Win5NormalScoringError,
    Win5NormalScoringIdempotencyConflictError,
    Win5NormalScoringInvalidSourceError,
    Win5NormalScoringUnavailableError,
    Win5NormalScoringWalletUnavailableError,
    Win5SpecialScoringAlreadyCompletedError,
    Win5SpecialScoringAuditError,
    Win5SpecialScoringError,
    Win5SpecialScoringIdempotencyConflictError,
    Win5SpecialScoringInvalidSourceError,
    Win5SpecialScoringUnavailableError,
)
from uma_st2.application.win5.staff_result_queries import (
    Win5NormalResultTarget,
    Win5NormalResultTargetChoice,
    Win5SpecialResultRace,
    Win5SpecialResultTarget,
    Win5SpecialResultTargetChoice,
    Win5StaffResultInvalidSourceError,
    Win5StaffResultQueryError,
    Win5StaffResultTargetUnavailableError,
)

from ..common import bounded_discord_message, safe_discord_text

NORMAL_TARGET_PLACEHOLDER = "채점 대상 라운드·경기 선택"
SPECIAL_TARGET_PLACEHOLDER = "채점 대상 특별 라운드 선택"
CONFIRM_LABEL = "채점 확정"
CANCEL_LABEL = "취소"
NORMAL_TARGET_SELECTION_INVALID = "채점 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
NORMAL_TARGET_NOT_ON_PAGE = "선택한 채점 대상이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
SPECIAL_TARGET_SELECTION_INVALID = "특별 채점 대상 선택값이 올바르지 않습니다. 작업 화면을 다시 열어 주세요."
SPECIAL_TARGET_NOT_ON_PAGE = "선택한 특별 채점 대상이 현재 화면에 없습니다. 작업 화면을 다시 열어 주세요."
BOUND_NORMAL_TARGET = "이 화면을 연 사용자와 서버·채널에서만 채점 대상을 선택할 수 있습니다."
BOUND_SPECIAL_TARGET = "이 화면을 연 사용자와 서버·채널에서만 특별 채점 대상을 선택할 수 있습니다."
BOUND_NORMAL_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 채점을 확정할 수 있습니다."
BOUND_SPECIAL_CONFIRM = "이 확인 화면을 연 사용자와 서버·채널에서만 특별 채점을 확정할 수 있습니다."
NORMAL_CANCELLED = "WIN5 일반 라운드 채점을 취소했습니다."
SPECIAL_CANCELLED = "WIN5 특별 라운드 채점을 취소했습니다."
NORMAL_CONFIRM_ALREADY_STARTED = "이 일반 채점 확인은 이미 처리 중이거나 완료되었습니다."
NORMAL_SCORING_TRANSITION_ERROR = "WIN5 일반 채점 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
SPECIAL_CONFIRM_ALREADY_STARTED = "이 특별 채점 확인은 이미 처리 중이거나 완료되었습니다."
SPECIAL_SCORING_TRANSITION_ERROR = "WIN5 특별 채점 화면을 갱신하지 못했습니다. `/win5 staff round`를 다시 열어 주세요."
NO_NORMAL_TARGETS = "현재 채점할 수 있는 일반 라운드가 없습니다."
NO_SPECIAL_TARGETS = "현재 채점할 수 있는 특별 라운드가 없습니다."
NORMAL_TARGET_PROMPT = "채점 대상 라운드·경기를 선택해 주세요."
SPECIAL_TARGET_PROMPT = "채점 대상 특별 라운드를 선택해 주세요."


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


def format_normal_preview(target: Win5NormalResultTarget) -> str:
    entry_by_id = {entry.id: entry for entry in target.entries}
    lines = [
        "WIN5 일반 라운드 채점 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        f"경기: {safe_discord_text(target.race_name)}",
        "권위 결과:",
    ]
    for placement in target.current_placements:
        entry = entry_by_id[placement.race_entry_id]
        lines.append(f"- {placement.position}착: {entry.gate_number} · {safe_discord_text(entry.name)}")
    lines.extend(
        (
            "확정하면 현재 접수된 제출 전체를 이 결과로 채점하고 승점과 서클 포인트 보상을 한 번에 반영합니다.",
            "채점 완료 후에는 결과를 정정하거나 다시 채점할 수 없습니다.",
        )
    )
    return bounded_discord_message(lines)


def format_normal_success(*, target: Win5NormalResultTarget, result: ScoredNormalWin5Round) -> str:
    return bounded_discord_message(
        (
            "WIN5 일반 라운드 채점 완료",
            f"라운드: {safe_discord_text(target.round_name)}",
            f"경기: {safe_discord_text(target.race_name)}",
            f"채점 제출: {len(result.events)}건",
            f"시즌 승점 합계: {result.season_score_delta}점",
            f"TOP1 승점 합계: {result.top1_score_delta}점",
            f"서클 포인트 지급 합계: {result.circle_point_reward}",
        )
    )


def format_special_preview(target: Win5SpecialResultTarget) -> str:
    winner_by_race = {winner.race_id: winner for winner in target.current_winners}
    lines = [
        "WIN5 특별 라운드 채점 확인",
        f"시즌: {safe_discord_text(target.season_name)}",
        f"라운드: {safe_discord_text(target.round_name)}",
        "권위 결과:",
    ]
    for race in target.races:
        race_name = safe_discord_text(race.name, limit=200)
        if race.is_void:
            lines.append(f"- {race_name}: {special_void_label(race)}")
            continue
        winner = winner_by_race[race.id]
        lines.append(f"- {race_name}: {special_gate_label(race, winner.gate_number)}")
    lines.extend(
        (
            "확정하면 현재 접수된 제출 전체를 한 batch로 채점하고 적중 Race마다 시즌/TOP1 승점을 각각 1점 반영합니다.",
            "서클 포인트는 변경하지 않으며, 채점 완료 후에는 결과를 정정하거나 다시 채점할 수 없습니다.",
        )
    )
    message = "\n".join(lines)
    if len(message) > 1900:
        raise ValueError("전체 Special 결과를 한 화면에 표시할 수 없어 채점을 진행하지 않습니다.")
    return message


def format_special_success(*, target: Win5SpecialResultTarget, result: ScoredSpecialWin5Round) -> str:
    race_summary = f"채점 Race: {len(result.race_ids)}개"
    if result.void_race_ids:
        race_summary = f"{race_summary} · VOID {len(result.void_race_ids)}개"
    return bounded_discord_message(
        (
            "WIN5 특별 라운드 채점 완료",
            f"라운드: {safe_discord_text(target.round_name)}",
            race_summary,
            f"채점 제출: {len(result.events)}건",
            f"시즌 승점 합계: {result.season_score_delta}점",
            f"TOP1 승점 합계: {result.top1_score_delta}점",
            "서클 포인트 변경: 없음",
        )
    )


def normal_command_error_message(error: Win5NormalScoringError) -> str:
    if isinstance(error, Win5NormalScoringAlreadyCompletedError):
        detail = "이미 채점 완료된 라운드입니다."
    elif isinstance(error, Win5NormalScoringUnavailableError):
        detail = "선택한 라운드가 더 이상 일반 채점 대상이 아닙니다."
    elif isinstance(error, Win5NormalScoringInvalidSourceError):
        detail = "결과 또는 제출 상태가 완전하지 않아 채점하지 않았습니다."
    elif isinstance(error, Win5NormalScoringWalletUnavailableError):
        detail = "보상 대상의 서클 포인트 지갑이 없어 전체 채점을 취소했습니다."
    elif isinstance(error, Win5NormalScoringIdempotencyConflictError):
        detail = "같은 요청이 다른 채점에 사용되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5NormalScoringAuditError):
        detail = "기존 채점 요청 기록을 안전하게 확인할 수 없습니다. 운영 로그를 확인해 주세요."
    else:
        detail = "라운드를 채점할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 채점을 완료하지 못했습니다: {detail}"


def special_command_error_message(error: Win5SpecialScoringError) -> str:
    if isinstance(error, Win5SpecialScoringAlreadyCompletedError):
        detail = "이미 채점 완료된 라운드입니다."
    elif isinstance(error, Win5SpecialScoringUnavailableError):
        detail = "선택한 라운드가 더 이상 특별 채점 대상이 아닙니다."
    elif isinstance(error, Win5SpecialScoringInvalidSourceError):
        detail = "Race·결과 또는 제출 상태가 완전하지 않아 채점하지 않았습니다."
    elif isinstance(error, Win5SpecialScoringIdempotencyConflictError):
        detail = "같은 요청이 다른 채점에 사용되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5SpecialScoringAuditError):
        detail = "기존 채점 요청 기록을 안전하게 확인할 수 없습니다. 운영 로그를 확인해 주세요."
    else:
        detail = "특별 라운드를 채점할 수 없습니다. 작업 화면을 다시 열어 주세요."
    return f"WIN5 특별 채점을 완료하지 못했습니다: {detail}"


def normal_target_query_error(error: Win5StaffResultQueryError) -> str:
    if isinstance(error, Win5StaffResultTargetUnavailableError):
        detail = "대상 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5StaffResultInvalidSourceError):
        detail = "엔트리 또는 권위 결과가 불완전하여 채점할 수 없습니다."
    else:
        detail = "대상을 안전하게 확인할 수 없습니다."
    return f"채점 미리보기를 만들지 못했습니다: {detail}"


def special_target_query_error(error: Win5StaffResultQueryError) -> str:
    if isinstance(error, Win5StaffResultTargetUnavailableError):
        detail = "대상 상태가 변경되었습니다. 작업 화면을 다시 열어 주세요."
    elif isinstance(error, Win5StaffResultInvalidSourceError):
        detail = "Race 또는 권위 결과가 불완전하여 채점할 수 없습니다."
    else:
        detail = "대상을 안전하게 확인할 수 없습니다."
    return f"특별 채점 미리보기를 만들지 못했습니다: {detail}"


def normal_targets_load_error(reference_id: str) -> str:
    return f"채점 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def special_targets_load_error(reference_id: str) -> str:
    return f"특별 채점 대상을 불러오지 못했습니다. 참조 ID: `{reference_id}`"


def normal_preview_internal_error(reference_id: str) -> str:
    return f"채점 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def normal_scoring_internal_error(reference_id: str) -> str:
    return f"WIN5 채점을 완료하지 못했습니다. 참조 ID: `{reference_id}`"


def special_preview_internal_error(reference_id: str) -> str:
    return f"특별 채점 미리보기를 만들지 못했습니다. 참조 ID: `{reference_id}`"


def special_scoring_internal_error(reference_id: str) -> str:
    return f"WIN5 특별 채점을 완료하지 못했습니다. 참조 ID: `{reference_id}`"
