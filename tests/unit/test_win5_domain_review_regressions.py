import pytest

from uma_st2.domain.win5 import (
    Win5Placement,
    Win5Race,
    Win5RaceEntry,
    Win5RaceResult,
    Win5ResultInvariantError,
    Win5Round,
    Win5RoundStatus,
    Win5RoundType,
    Win5SubmissionInvariantError,
    Win5SubmissionPick,
    validate_normal_round_result_structure,
    validate_special_round_submission_structure,
)


def _race_entry(entry_id: int, race_id: int, gate_number: int) -> Win5RaceEntry:
    return Win5RaceEntry(id=entry_id, race_id=race_id, gate_number=gate_number, name=f"horse-{entry_id}")


def _race(race_id: int) -> Win5Race:
    return Win5Race(
        id=race_id,
        name=f"race-{race_id}",
        entries=tuple(_race_entry((race_id * 10) + offset, race_id, offset) for offset in range(1, 6)),
    )


def test_special_round_structure_rejects_race_entry_identity_even_when_reference_exists() -> None:
    race_a = _race(101)
    race_b = _race(102)
    round_ = Win5Round(
        id=1,
        season_id=1,
        name="제1회 Special",
        type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.OPEN,
        races=(race_a, race_b),
    )
    pick = Win5SubmissionPick(
        id=1,
        submission_id=1,
        race_id=race_a.id,
        race_entry_id=race_a.entries[0].id,
        gate_number=race_a.entries[0].gate_number,
        position=1,
    )

    with pytest.raises(Win5SubmissionInvariantError):
        validate_special_round_submission_structure(round_, (pick,))


def test_special_round_allows_gate_missing_from_optional_reference_entries() -> None:
    race = Win5Race(id=103, name="race-103", entries=(_race_entry(1031, 103, 1),))
    round_ = Win5Round(
        id=2,
        season_id=1,
        name="제2회 Special",
        type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.OPEN,
        races=(race,),
    )
    pick = Win5SubmissionPick(
        id=2,
        submission_id=1,
        race_id=race.id,
        race_entry_id=None,
        gate_number=8,
        position=1,
    )

    validate_special_round_submission_structure(round_, (pick,))


def test_normal_result_rejects_foreign_race_entry_even_when_race_id_matches() -> None:
    race = _race(201)
    foreign_race = _race(202)
    round_ = Win5Round(
        id=3,
        season_id=1,
        name="제3회 Normal",
        type=Win5RoundType.NORMAL,
        status=Win5RoundStatus.CLOSED,
        races=(race,),
    )
    result = Win5RaceResult(
        race_id=race.id,
        placements=(
            Win5Placement(race_entry_id=race.entries[0].id, gate_number=None, position=1),
            Win5Placement(race_entry_id=race.entries[1].id, gate_number=None, position=2),
            Win5Placement(race_entry_id=race.entries[2].id, gate_number=None, position=3),
            Win5Placement(race_entry_id=race.entries[3].id, gate_number=None, position=4),
            Win5Placement(race_entry_id=foreign_race.entries[0].id, gate_number=None, position=5),
        ),
    )

    with pytest.raises(Win5ResultInvariantError):
        validate_normal_round_result_structure(round_, result)
