"""User-facing copy and pure formatters for WIN5 submission history."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Literal

import discord

from uma_st2.application.win5 import (
    Win5MemberQueryError,
    Win5MemberQueryIdentityError,
    Win5MemberResult,
    Win5MemberSubmission,
    Win5MemberSubmissionPick,
    Win5MemberSubmissionRound,
    Win5MemberSubmissionRoundPage,
    Win5MemberSubmissionRoundSummary,
    Win5NormalSubmissionJudgement,
    Win5SpecialSubmissionJudgement,
    Win5SubmissionsInvalidSourceError,
    Win5SubmissionsUnavailableError,
)
from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5RoundStatus,
    Win5RoundType,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

from ..common import bounded_discord_message, safe_discord_text

ROUND_SELECT_PLACEHOLDER = "조회할 라운드 선택"
ROUND_SELECT_ERROR = "선택한 라운드를 조회할 수 없습니다."
ROUND_STATUS_CHANGED = "라운드 상태가 바뀌었습니다. `/win5 submissions`를 다시 실행해 주세요."
HISTORY_REFRESH_ERROR = "제출 이력을 새로 읽지 못했습니다. `/win5 submissions`를 다시 실행해 주세요."
HISTORY_TRANSITION_ERROR = "제출 이력 화면을 갱신하지 못했습니다. `/win5 submissions`를 다시 실행해 주세요."
HISTORY_TRANSITION_STARTED = "제출 이력 화면 갱신이 이미 시작되었습니다. 완료 후 다시 확인해 주세요."
TARGET_MOVED_NOTICE = "선택한 라운드의 상태가 바뀌어 현재 목록을 새로 표시했습니다."
NO_DETAIL = "선택한 라운드의 제출 이력을 표시할 수 없습니다."
PAGE_UNAVAILABLE = "현재 페이지를 더 이상 조회할 수 없습니다."
PAGE_OUT_OF_RANGE = "요청한 페이지가 현재 이력 범위를 벗어났습니다."
OPEN_LABEL = "진행 중"
SCORED_LABEL = "채점 완료"
CANCELLED_LABEL = "취소됨"
REFRESH_LABEL = "새로고침"
INITIAL_STATUS_CHANGED = "제출 이력을 조회하지 못했습니다: 라운드 상태가 바뀌었습니다. 다시 실행해 주세요."

Win5SubmissionsMode = Literal["open", "scored", "cancelled"]


def submissions_query_error_message(error: Win5MemberQueryError) -> str:
    if isinstance(error, Win5MemberQueryIdentityError):
        detail = "WIN5 이용에는 활성 Persona와 PID가 등록된 게임 계정이 필요합니다."
    elif isinstance(error, Win5SubmissionsUnavailableError):
        detail = "현재 활성 WIN5 시즌이 없습니다."
    elif isinstance(error, Win5SubmissionsInvalidSourceError):
        detail = "제출·채점 이력을 안전하게 표시할 수 없습니다. 운영진에게 알려 주세요."
    else:
        detail = "현재 제출 이력을 조회할 수 없습니다."
    return f"제출 이력을 조회하지 못했습니다: {detail}"


def history_refresh_internal_error(reference_id: str) -> str:
    return f"제출 이력을 새로 읽지 못했습니다. 참조 ID: `{reference_id}`"


def history_page_button_label(*, axis: Literal["submission", "race"], direction: Literal[-1, 1]) -> str:
    noun = "제출" if axis == "submission" else "Race"
    movement = "이전" if direction == -1 else "다음"
    return f"{movement} {noun}"


def empty_history_message(mode: Win5SubmissionsMode) -> str:
    if mode == "open":
        return "현재 열린 라운드가 없습니다."
    if mode == "scored":
        return "현재 활성 시즌에 채점 완료된 내 제출이 없습니다."
    return "현재 활성 시즌에 공식 취소된 내 Special 제출이 없습니다."


def _submission_history_type_label(round_type: Win5RoundType) -> str:
    return "Normal" if round_type == Win5RoundType.NORMAL else "Special"


def _submission_history_status_label(status: Win5SubmissionStatus) -> str:
    return "제출 중" if status == Win5SubmissionStatus.ACCEPTED else "취소 이력"


def _submission_history_round_status_label(status: Win5RoundStatus) -> str:
    labels = {
        Win5RoundStatus.SETUP: "준비 중",
        Win5RoundStatus.OPEN: "진행 중",
        Win5RoundStatus.CLOSED: "마감",
        Win5RoundStatus.SCORED: "채점 완료",
        Win5RoundStatus.CANCELLED: "취소됨",
    }
    return labels[status]


def _submission_history_tier_label(tier: Win5SubmissionTier) -> str:
    return "Special" if tier == Win5SubmissionTier.SPECIAL_WINNER else tier.value


def _submission_history_pick_label(
    pick: Win5MemberSubmissionPick,
    *,
    name_limit: int = 45,
) -> str:
    gate_number = pick.gate_number
    if gate_number is None and pick.reference_entry is not None:
        gate_number = pick.reference_entry.gate_number
    if gate_number is None:
        return "알 수 없는 pick"
    label = f"Gate {gate_number}"
    if pick.reference_entry is not None:
        label += f" — {safe_discord_text(pick.reference_entry.name, limit=name_limit)}"
    return label


def _submission_history_result_label(
    result: Win5MemberResult,
    *,
    name_limit: int = 45,
) -> str:
    gate_number = result.gate_number
    if gate_number is None and result.reference_entry is not None:
        gate_number = result.reference_entry.gate_number
    label = f"Gate {gate_number}" if gate_number is not None else "알 수 없는 결과"
    if result.reference_entry is not None:
        label += f" — {safe_discord_text(result.reference_entry.name, limit=name_limit)}"
    return label


def _normal_history_pick_lines(
    submission: Win5MemberSubmission,
    *,
    tier_position_count: Mapping[Win5SubmissionTier, int],
) -> list[str]:
    picks_by_position = {pick.position: pick for pick in submission.picks}
    position_count = tier_position_count[submission.tier]
    return [
        f"  - {position}착: "
        + (_submission_history_pick_label(picks_by_position[position]) if position in picks_by_position else "—")
        for position in range(1, position_count + 1)
    ]


def _special_history_pick_lines(
    round_: Win5MemberSubmissionRound,
    submission: Win5MemberSubmission,
) -> list[str]:
    picks_by_race_id = {pick.race_id: pick for pick in submission.picks}
    return [
        f"  - {safe_discord_text(race.name, limit=45)}: "
        + (_submission_history_pick_label(picks_by_race_id[race.id]) if race.id in picks_by_race_id else "—")
        for race in round_.races
    ]


def _normal_history_judgement_lines(
    round_: Win5MemberSubmissionRound,
    submission: Win5MemberSubmission,
) -> list[str]:
    judgement = submission.judgement
    if not isinstance(judgement, Win5NormalSubmissionJudgement):
        return []
    results_by_id = {result.id: result for result in round_.results}
    item_labels: list[str] = []
    for item in judgement.score.items:
        if item.outcome == Win5JudgementOutcome.EXACT:
            outcome = "정확(+3)"
        elif item.outcome == Win5JudgementOutcome.WRONG_POSITION:
            matched = results_by_id[item.matched_result_id]  # type: ignore[index]
            outcome = f"{matched.position}착 적중(+1)"
        elif item.outcome == Win5JudgementOutcome.OFF_BOARD:
            outcome = "TOP5 밖(0)"
        else:
            outcome = "미입력(MISS)"
        item_labels.append(f"{item.position}착 {outcome}")
    score = judgement.score
    return [
        f"  판정: {' · '.join(item_labels)}",
        "  합계: "
        f"정확 {score.exact_count} / 순위 다름 {score.wrong_position_count} / "
        f"TOP5 밖 {score.off_board_count} / MISS {score.missing_count}",
        "  획득: "
        f"Season +{score.season_score_delta} / TOP1 +{score.top1_score_delta} / "
        f"Circle Point +{score.circle_point_reward}",
    ]


def _special_history_judgement_lines(
    round_: Win5MemberSubmissionRound,
    submission: Win5MemberSubmission,
) -> list[str]:
    judgement = submission.judgement
    if not isinstance(judgement, Win5SpecialSubmissionJudgement):
        return []
    picks_by_race_id = {pick.race_id: pick for pick in submission.picks}
    results_by_race_id = {result.race_id: result for result in round_.results}
    items_by_race_id = {item.race_id: item for item in judgement.items}
    lines: list[str] = []
    for race in round_.races:
        pick = picks_by_race_id.get(race.id)
        item = items_by_race_id[race.id]
        if item.outcome == Win5JudgementOutcome.VOID:
            outcome = "공식 취소(0)"
            pick_label = _submission_history_pick_label(pick, name_limit=25) if pick is not None else "—"
            reason = safe_discord_text(race.void_reason or "사유 없음", limit=80)
            detail = f"제출 {pick_label} / 결과 없음 / 사유 {reason}"
        elif item.outcome == Win5JudgementOutcome.EXACT:
            result = results_by_race_id[race.id]
            outcome = "정확(+1)"
            detail = f"제출=결과 {_submission_history_result_label(result, name_limit=25)}"
        elif item.outcome == Win5JudgementOutcome.OFF_BOARD:
            result = results_by_race_id[race.id]
            outcome = "불일치(0)"
            detail = (
                f"제출 {_submission_history_pick_label(pick, name_limit=25)} / "  # type: ignore[arg-type]
                f"결과 {_submission_history_result_label(result, name_limit=25)}"
            )
        else:
            result = results_by_race_id[race.id]
            outcome = "미입력(MISS)"
            detail = f"제출 — / 결과 {_submission_history_result_label(result, name_limit=25)}"
        lines.append(f"  - {safe_discord_text(race.name, limit=35)}: {detail} / {outcome}")
    lines.append(
        "  합계: "
        f"정확 {judgement.exact_count} / 불일치 {judgement.off_board_count} / "
        f"MISS {judgement.missing_count} / VOID {judgement.void_count} · "
        f"Season/TOP1 +{judgement.season_score_delta}/+{judgement.top1_score_delta} · "
        f"Circle Point +{judgement.circle_point_reward}"
    )
    return lines


def _special_cancelled_void_lines(round_: Win5MemberSubmissionRound) -> list[str]:
    return [
        "공식 취소 Race:",
        *(
            f"  - {safe_discord_text(race.name, limit=35)}: "
            f"{safe_discord_text(race.void_reason or '사유 없음', limit=60)}"
            for race in round_.races
        ),
    ]


def _special_cancelled_pick_lines(
    round_: Win5MemberSubmissionRound,
    submission: Win5MemberSubmission,
) -> list[str]:
    picks_by_race_id = {pick.race_id: pick for pick in submission.picks}
    return [
        f"  - {safe_discord_text(race.name, limit=30)}: "
        + (
            _submission_history_pick_label(picks_by_race_id[race.id], name_limit=25)
            if race.id in picks_by_race_id
            else "—"
        )
        for race in round_.races
    ]


def _history_page_label(*, offset: int, limit: int, total: int) -> str:
    page = offset // limit + 1
    pages = max(1, (total + limit - 1) // limit)
    return f"{page}/{pages}"


def format_submission_history_round_copy(
    page: Win5MemberSubmissionRoundPage,
    *,
    tier_position_count: Mapping[Win5SubmissionTier, int],
) -> str:
    """Render one current-Season owned history card from closed DTO facts."""

    round_ = page.round
    submission_page = _history_page_label(
        offset=page.submission_offset,
        limit=page.submission_limit,
        total=page.total_submission_count,
    )
    lines = [
        "## WIN5 제출 이력",
        f"시즌: {safe_discord_text(page.season_name)}",
        f"라운드: {safe_discord_text(round_.name)}",
        "유형/상태: "
        f"{_submission_history_type_label(round_.round_type)} / "
        f"{_submission_history_round_status_label(round_.status)}",
        f"제출 이력: {page.total_submission_count}건 · 페이지 {submission_page}",
    ]
    if round_.round_type == Win5RoundType.SPECIAL:
        lines.append(
            "Race: "
            f"{page.total_race_count}개 · 페이지 "
            f"{_history_page_label(offset=page.race_offset, limit=page.race_limit, total=page.total_race_count)}"
        )
        if round_.status == Win5RoundStatus.CANCELLED:
            lines.extend(_special_cancelled_void_lines(round_))
    if round_.round_type == Win5RoundType.NORMAL and round_.results:
        board = " / ".join(
            f"{result.position}착 {_submission_history_result_label(result)}" for result in round_.results
        )
        lines.append(f"확정 결과: {board}")
    if not round_.submissions:
        lines.append("내 제출: 없음")
        return bounded_discord_message(lines, limit=3500)

    for index, submission in enumerate(round_.submissions, start=page.submission_offset + 1):
        lines.append(
            f"제출 {index}: {_submission_history_tier_label(submission.tier)} / "
            f"{_submission_history_status_label(submission.status)} / version {submission.version}"
        )
        if round_.round_type == Win5RoundType.NORMAL:
            lines.extend(
                _normal_history_pick_lines(
                    submission,
                    tier_position_count=tier_position_count,
                )
            )
            lines.extend(_normal_history_judgement_lines(round_, submission))
        elif round_.status == Win5RoundStatus.CANCELLED:
            lines.extend(_special_cancelled_pick_lines(round_, submission))
        else:
            judgement_lines = _special_history_judgement_lines(round_, submission)
            lines.extend(judgement_lines or _special_history_pick_lines(round_, submission))
    return bounded_discord_message(lines, limit=3500)


def submission_history_round_options(
    rounds: tuple[Win5MemberSubmissionRoundSummary, ...],
    *,
    selected_round_id: int,
) -> list[discord.SelectOption]:
    duplicate_counts = Counter(round_.name for round_ in rounds)
    options: list[discord.SelectOption] = []
    for round_ in rounds:
        suffix = f" · ID {round_.id}" if duplicate_counts[round_.name] > 1 else ""
        title = safe_discord_text(round_.name, limit=max(1, 100 - len(suffix)))
        options.append(
            discord.SelectOption(
                label=f"{title}{suffix}",
                value=str(round_.id),
                default=round_.id == selected_round_id,
            )
        )
    return options
