"""Invariant validators for WIN5 core domain primitives.

The functions in this module are intentionally pure and only express frozen V2
contract rules. Persistence and Application orchestration stay outside Domain.
Implemented scoring semantics remain Domain-owned; deferred policies such as
Special Race void representation stay outside these validators until frozen.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from .enums import (
    Win5RoundType,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)
from .errors import (
    Win5RaceInvariantError,
    Win5ResultInvariantError,
    Win5SubmissionInvariantError,
)
from .models import (
    Win5Race,
    Win5RaceResult,
    Win5Round,
    Win5Submission,
    Win5SubmissionPick,
)

_NORMAL_ROUND_RESULT_POSITIONS = (1, 2, 3, 4, 5)
MIN_NORMAL_WIN5_RACE_ENTRIES: Final = 5
_NORMAL_TIER_MAX_POSITION = {
    Win5SubmissionTier.TOP1: 1,
    Win5SubmissionTier.TOP3: 3,
    Win5SubmissionTier.TOP5: 5,
}


def validate_win5_round_open_readiness(
    *,
    round_type: Win5RoundType,
    race_count: int,
    race_entry_count: int,
    result_count: int,
) -> None:
    """Validate the frozen graph requirements for opening one WIN5 Round."""

    try:
        canonical_type = Win5RoundType(round_type)
    except ValueError as exc:
        raise Win5RaceInvariantError("Round type must be Normal or Special.") from exc
    for field_name, value in (
        ("race_count", race_count),
        ("race_entry_count", race_entry_count),
        ("result_count", result_count),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Win5RaceInvariantError(f"{field_name} must be a non-negative integer.")

    if canonical_type == Win5RoundType.NORMAL:
        if race_count != 1:
            raise Win5RaceInvariantError("A Normal Round must include exactly one Race before opening.")
        if race_entry_count < MIN_NORMAL_WIN5_RACE_ENTRIES:
            raise Win5RaceInvariantError("A Normal Round requires at least five RaceEntries before opening.")
    elif race_count < 1:
        raise Win5RaceInvariantError("A Special Round must include one or more Races before opening.")

    if result_count:
        raise Win5ResultInvariantError("A result-bearing WIN5 Round cannot be opened.")


def validate_win5_round_race_cardinality(round_: Win5Round) -> None:
    """Validate round cardinality by round type.

    - Normal Round: exactly one Race.
    - Special Round: one or more Races.
    """

    if round_.type == Win5RoundType.NORMAL and len(round_.races) != 1:
        raise Win5RaceInvariantError(f"A normal round must include exactly one Race. actual_count={len(round_.races)}")

    if round_.type == Win5RoundType.SPECIAL and len(round_.races) < 1:
        raise Win5RaceInvariantError(
            f"A special round must include one or more Races. actual_count={len(round_.races)}"
        )


def validate_accepted_submission_uniqueness_per_persona_round(
    submissions: Sequence[Win5Submission],
) -> None:
    """Validate accepted Submission uniqueness for Persona and Round."""

    accepted_counts: dict[tuple[object, object], int] = {}
    for submission in submissions:
        if submission.status != Win5SubmissionStatus.ACCEPTED:
            continue

        key = (submission.round_id, submission.persona_id)
        current = accepted_counts.get(key, 0) + 1
        if current > 1:
            raise Win5SubmissionInvariantError("Only one accepted Submission is allowed per Persona and Round.")
        accepted_counts[key] = current


def validate_pick_race_membership(
    picks: Sequence[Win5SubmissionPick],
    races: Sequence[Win5Race],
) -> None:
    """Validate pick race identity and Normal RaceEntry membership.

    Special gate-based picks deliberately do not require a matching reference
    RaceEntry. Optional Special entry references are presentation-only.
    """

    races_by_id = {race.id: race for race in races}
    entry_id_to_race_id = {entry.id: entry.race_id for race in races for entry in race.entries}

    for pick in picks:
        if pick.race_id not in races_by_id:
            raise Win5RaceInvariantError(f"Pick race_id={pick.race_id!r} does not exist in the provided races.")

        if pick.race_entry_id is None:
            continue

        entry_race_id = entry_id_to_race_id.get(pick.race_entry_id)
        if entry_race_id is None:
            raise Win5SubmissionInvariantError("Pick must reference a race entry included in the provided races.")

        if pick.race_id != entry_race_id:
            raise Win5SubmissionInvariantError("Pick must reference a race_entry_id that belongs to the same Race.")


def validate_normal_round_submission_structure(
    round_: Win5Round,
    picks: Sequence[Win5SubmissionPick],
) -> None:
    """Validate optional Normal picks as canonical RaceEntry-based payloads."""

    if round_.type != Win5RoundType.NORMAL:
        return

    validate_pick_race_membership(picks, round_.races)
    if len(round_.races) != 1:
        raise Win5RaceInvariantError("A normal round must include exactly one Race.")

    race_id = round_.races[0].id
    seen_positions: set[int] = set()
    for pick in picks:
        if pick.race_id != race_id:
            raise Win5SubmissionInvariantError("Normal picks must target the Round's single Race.")
        if isinstance(pick.position, bool) or not isinstance(pick.position, int) or pick.position <= 0:
            raise Win5SubmissionInvariantError("Normal pick position must be a positive integer.")
        if pick.race_entry_id is None or pick.gate_number is not None:
            raise Win5SubmissionInvariantError("Normal picks require race_entry_id and must not store gate_number.")
        if pick.position in seen_positions:
            raise Win5SubmissionInvariantError("A Normal submission may contain at most one pick per position.")
        seen_positions.add(pick.position)


def validate_special_round_submission_structure(
    round_: Win5Round,
    picks: Sequence[Win5SubmissionPick],
) -> None:
    """Validate optional gate-number winner picks for a Special Round.

    Missing Race picks are allowed and score as MISS later. Each provided Race
    may have at most one position-1 gate pick. Optional reference RaceEntries do
    not constrain valid gate numbers.
    """

    if round_.type != Win5RoundType.SPECIAL:
        return

    race_ids = {race.id for race in round_.races}
    seen_race_ids: set[object] = set()

    for pick in picks:
        if pick.race_id not in race_ids:
            raise Win5RaceInvariantError(f"Pick race_id={pick.race_id!r} does not exist in the Special Round.")
        if pick.race_entry_id is not None or pick.gate_number is None:
            raise Win5SubmissionInvariantError("Special picks require gate_number and must not store race_entry_id.")
        if pick.gate_number <= 0:
            raise Win5SubmissionInvariantError("Special gate_number must be a positive integer.")
        if pick.position != 1:
            raise Win5SubmissionInvariantError("Special winner picks use position = 1.")
        if pick.race_id in seen_race_ids:
            raise Win5SubmissionInvariantError("A Special submission may contain at most one winner pick per Race.")
        seen_race_ids.add(pick.race_id)


def validate_submission_for_round(
    round_: Win5Round,
    submission: Win5Submission,
) -> None:
    """Validate one accepted Submission against its persisted Round graph."""

    if submission.round_id != round_.id:
        raise Win5SubmissionInvariantError("Submission must belong to the provided Round.")
    if submission.status != Win5SubmissionStatus.ACCEPTED:
        raise Win5SubmissionInvariantError("Member save validation requires an accepted Submission.")

    validate_win5_round_race_cardinality(round_)
    if round_.type == Win5RoundType.NORMAL:
        max_position = _NORMAL_TIER_MAX_POSITION.get(submission.tier)
        if max_position is None:
            raise Win5SubmissionInvariantError("A Normal submission requires TOP1, TOP3, or TOP5 tier.")
        if any(pick.position > max_position for pick in submission.picks):
            raise Win5SubmissionInvariantError(
                f"{submission.tier.value} picks must use positions 1 through {max_position}."
            )
        validate_normal_round_submission_structure(round_, submission.picks)
        race_entry_ids = [pick.race_entry_id for pick in submission.picks]
        if len(race_entry_ids) != len(set(race_entry_ids)):
            raise Win5SubmissionInvariantError("A Normal submission cannot select the same RaceEntry more than once.")
        return

    if submission.tier != Win5SubmissionTier.SPECIAL_WINNER:
        raise Win5SubmissionInvariantError("A Special submission requires SPECIAL_WINNER tier.")
    validate_special_round_submission_structure(round_, submission.picks)


def validate_normal_round_result_structure(
    round_: Win5Round,
    result: Win5RaceResult,
) -> None:
    """Validate Normal result identity and positions 1 through 5."""

    if round_.type != Win5RoundType.NORMAL:
        return

    if len(round_.races) != 1 or round_.races[0].id != result.race_id:
        raise Win5ResultInvariantError("Normal-round result must belong to the Round's single Race.")

    race_entry_ids = {entry.id for entry in round_.races[0].entries}
    if any(
        placement.race_entry_id is None
        or placement.gate_number is not None
        or placement.race_entry_id not in race_entry_ids
        for placement in result.placements
    ):
        raise Win5ResultInvariantError("Normal-round placements must use RaceEntries from the Round's Race.")

    if len(result.placements) != len(_NORMAL_ROUND_RESULT_POSITIONS):
        raise Win5ResultInvariantError(
            "Normal-round results must contain exactly one placement for each of 1 through 5."
        )

    actual_positions = {placement.position for placement in result.placements}
    if actual_positions != set(_NORMAL_ROUND_RESULT_POSITIONS):
        raise Win5ResultInvariantError(
            "Normal-round results must contain exactly one placement for each of 1 through 5."
        )

    result_entry_ids = [placement.race_entry_id for placement in result.placements]
    if len(result_entry_ids) != len(set(result_entry_ids)):
        raise Win5ResultInvariantError("Normal-round results must use each RaceEntry at most once.")


def validate_special_round_result_structure(
    round_: Win5Round,
    result: Win5RaceResult,
) -> None:
    """Validate one gate-number winner result for a Special Race."""

    if round_.type != Win5RoundType.SPECIAL:
        return

    race_ids = {race.id for race in round_.races}
    if result.race_id not in race_ids:
        raise Win5ResultInvariantError("Special result must belong to a Race in the Round.")
    if len(result.placements) != 1:
        raise Win5ResultInvariantError("Special result must contain exactly one winner placement.")

    winner = result.placements[0]
    if winner.position != 1 or winner.race_entry_id is not None or winner.gate_number is None:
        raise Win5ResultInvariantError("Special result must use one position-1 gate_number and no race_entry_id.")
    if winner.gate_number <= 0:
        raise Win5ResultInvariantError("Special winner gate_number must be a positive integer.")
