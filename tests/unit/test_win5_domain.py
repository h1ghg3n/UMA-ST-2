import ast
from pathlib import Path

import pytest

from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5JudgementOutcome,
    Win5Placement,
    Win5Race,
    Win5RaceEntry,
    Win5RaceInvariantError,
    Win5RaceResult,
    Win5ResultInvariantError,
    Win5Round,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5Submission,
    Win5SubmissionInvariantError,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    validate_accepted_submission_uniqueness_per_persona_round,
    validate_normal_round_result_structure,
    validate_normal_round_submission_structure,
    validate_pick_race_membership,
    validate_special_round_result_structure,
    validate_special_round_submission_structure,
    validate_win5_round_race_cardinality,
)


def _race_entry(entry_id: int, race_id: int, gate: int) -> Win5RaceEntry:
    return Win5RaceEntry(id=entry_id, race_id=race_id, gate_number=gate, name=f"horse-{entry_id}")


def _race(race_id: int, gates: tuple[int, ...] = ()) -> Win5Race:
    return Win5Race(
        id=race_id,
        name=f"race-{race_id}",
        entries=tuple(_race_entry((race_id * 10) + index, race_id, gate) for index, gate in enumerate(gates, start=1)),
    )


def _submission(submission_id: int, round_id: int, persona_id: str, status: Win5SubmissionStatus) -> Win5Submission:
    return Win5Submission(
        id=submission_id,
        round_id=round_id,
        persona_id=persona_id,
        tier=Win5SubmissionTier.TOP1,
        status=status,
    )


def _normal_pick(
    pick_id: int, submission_id: int, race_id: int, race_entry_id: int, position: int
) -> Win5SubmissionPick:
    return Win5SubmissionPick(
        id=pick_id,
        submission_id=submission_id,
        race_id=race_id,
        race_entry_id=race_entry_id,
        gate_number=None,
        position=position,
    )


def _special_pick(pick_id: int, submission_id: int, race_id: int, gate_number: int) -> Win5SubmissionPick:
    return Win5SubmissionPick(
        id=pick_id,
        submission_id=submission_id,
        race_id=race_id,
        race_entry_id=None,
        gate_number=gate_number,
        position=1,
    )


def _normal_placement(race_entry_id: int, position: int) -> Win5Placement:
    return Win5Placement(race_entry_id=race_entry_id, gate_number=None, position=position)


def _special_winner(gate_number: int) -> Win5Placement:
    return Win5Placement(race_entry_id=None, gate_number=gate_number, position=1)


def test_canonical_vocabularies_are_exact() -> None:
    assert {status.value for status in Win5SeasonStatus} == {"draft", "active", "closed", "cancelled"}
    assert {type_.value for type_ in Win5RoundType} == {"normal", "special"}
    assert {status.value for status in Win5RoundStatus} == {
        "setup",
        "open",
        "closed",
        "scored",
        "cancelled",
    }
    assert {tier.value for tier in Win5SubmissionTier} == {"TOP1", "TOP3", "TOP5", "SPECIAL_WINNER"}
    assert {status.value for status in Win5SubmissionStatus} == {"accepted", "cancelled"}
    assert {outcome.value for outcome in Win5JudgementOutcome} == {
        "exact",
        "wrong_position",
        "off_board",
        "missing",
        "void",
    }


def test_round_race_cardinality_depends_on_round_type() -> None:
    normal = Win5Round(1, 10, "제1회 Normal", Win5RoundType.NORMAL, Win5RoundStatus.OPEN, (_race(100, (1, 2)),))
    special = Win5Round(
        2,
        10,
        "제2회 Special",
        Win5RoundType.SPECIAL,
        Win5RoundStatus.OPEN,
        (_race(200), _race(201)),
    )

    validate_win5_round_race_cardinality(normal)
    validate_win5_round_race_cardinality(special)

    with pytest.raises(Win5RaceInvariantError):
        validate_win5_round_race_cardinality(
            Win5Round(3, 10, "빈 Normal", Win5RoundType.NORMAL, Win5RoundStatus.OPEN, ())
        )
    with pytest.raises(Win5RaceInvariantError):
        validate_win5_round_race_cardinality(
            Win5Round(4, 10, "복수 Normal", Win5RoundType.NORMAL, Win5RoundStatus.OPEN, (_race(1), _race(2)))
        )
    with pytest.raises(Win5RaceInvariantError):
        validate_win5_round_race_cardinality(
            Win5Round(5, 10, "빈 Special", Win5RoundType.SPECIAL, Win5RoundStatus.OPEN, ())
        )


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_round_requires_bounded_operator_authored_title(name: str) -> None:
    with pytest.raises(Win5DomainError, match="name must be a non-empty string"):
        Win5Round(1, 10, name, Win5RoundType.NORMAL, Win5RoundStatus.SETUP)


def test_special_round_allows_partial_gate_submission_without_reference_entries() -> None:
    round_ = Win5Round(
        id=7,
        season_id=10,
        name="제1회 Special",
        type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.OPEN,
        races=(_race(301), _race(302), _race(303)),
    )
    picks = (_special_pick(1, 10, 301, 7), _special_pick(2, 10, 303, 2))

    validate_special_round_submission_structure(round_, picks)


def test_special_reference_entries_do_not_restrict_gate_pick() -> None:
    round_ = Win5Round(
        id=8,
        season_id=10,
        name="제2회 Special",
        type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.OPEN,
        races=(_race(304, (1, 3, 8)),),
    )
    # Gate 5 is intentionally absent from the optional reference list.
    validate_special_round_submission_structure(round_, (_special_pick(1, 11, 304, 5),))


def test_special_round_rejects_duplicate_or_invalid_gate_payloads() -> None:
    round_ = Win5Round(
        id=9,
        season_id=10,
        name="제3회 Special",
        type=Win5RoundType.SPECIAL,
        status=Win5RoundStatus.OPEN,
        races=(_race(305), _race(306)),
    )

    with pytest.raises(Win5SubmissionInvariantError):
        validate_special_round_submission_structure(
            round_,
            (_special_pick(1, 12, 305, 1), _special_pick(2, 12, 305, 2)),
        )

    with pytest.raises(Win5SubmissionInvariantError):
        validate_special_round_submission_structure(round_, (_special_pick(1, 12, 305, 0),))

    wrong_identity = Win5SubmissionPick(
        id=3,
        submission_id=12,
        race_id=305,
        race_entry_id=3051,
        gate_number=1,
        position=1,
    )
    with pytest.raises(Win5SubmissionInvariantError):
        validate_special_round_submission_structure(round_, (wrong_identity,))

    with pytest.raises(Win5RaceInvariantError):
        validate_special_round_submission_structure(round_, (_special_pick(4, 12, 999, 1),))


def test_normal_submission_uses_race_entries_and_allows_partial_positions() -> None:
    race = _race(401, (1, 2, 3, 4, 5))
    round_ = Win5Round(10, 10, "제1회 Normal", Win5RoundType.NORMAL, Win5RoundStatus.OPEN, (race,))
    picks = (
        _normal_pick(1, 20, 401, race.entries[0].id, 1),
        _normal_pick(2, 20, 401, race.entries[2].id, 3),
    )

    validate_normal_round_submission_structure(round_, picks)


def test_normal_submission_rejects_gate_payload_and_foreign_entry() -> None:
    race = _race(402, (1, 2, 3))
    round_ = Win5Round(11, 10, "제2회 Normal", Win5RoundType.NORMAL, Win5RoundStatus.OPEN, (race,))

    gate_payload = Win5SubmissionPick(
        id=1,
        submission_id=21,
        race_id=402,
        race_entry_id=None,
        gate_number=2,
        position=1,
    )
    with pytest.raises(Win5SubmissionInvariantError):
        validate_normal_round_submission_structure(round_, (gate_payload,))

    bad_entry = _normal_pick(2, 21, 402, 9999, 1)
    with pytest.raises(Win5SubmissionInvariantError):
        validate_pick_race_membership((bad_entry,), round_.races)


def test_gate_gaps_and_ordering_are_preserved() -> None:
    race = Win5Race(
        id=500,
        name="race",
        entries=(
            _race_entry(1, 500, 8),
            _race_entry(2, 500, 2),
            _race_entry(3, 500, 20),
        ),
    )
    assert [entry.gate_number for entry in race.entries_ordered_by_gate] == [2, 8, 20]
    assert [entry.gate_number for entry in race.entries] == [8, 2, 20]


def test_accepted_submission_per_persona_round_is_unique_and_cancelled_allows_replacement() -> None:
    with pytest.raises(Win5DomainError):
        validate_accepted_submission_uniqueness_per_persona_round(
            [
                _submission(1, 1, "persona-a", Win5SubmissionStatus.ACCEPTED),
                _submission(2, 1, "persona-a", Win5SubmissionStatus.ACCEPTED),
            ]
        )

    validate_accepted_submission_uniqueness_per_persona_round(
        [
            _submission(3, 2, "persona-b", Win5SubmissionStatus.CANCELLED),
            _submission(4, 2, "persona-b", Win5SubmissionStatus.ACCEPTED),
        ]
    )


def test_normal_round_result_requires_same_race_entries_and_positions_1_through_5() -> None:
    race = _race(601, (1, 2, 3, 4, 5))
    round_ = Win5Round(12, 10, "제3회 Normal", Win5RoundType.NORMAL, Win5RoundStatus.CLOSED, (race,))
    valid = Win5RaceResult(
        race_id=601,
        placements=tuple(_normal_placement(entry.id, position) for position, entry in enumerate(race.entries, start=1)),
    )
    validate_normal_round_result_structure(round_, valid)

    incomplete = Win5RaceResult(race_id=601, placements=valid.placements[:4])
    with pytest.raises(Win5ResultInvariantError):
        validate_normal_round_result_structure(round_, incomplete)

    foreign = Win5RaceResult(
        race_id=601,
        placements=valid.placements[:4] + (_normal_placement(9999, 5),),
    )
    with pytest.raises(Win5ResultInvariantError):
        validate_normal_round_result_structure(round_, foreign)

    duplicate = Win5RaceResult(
        race_id=601,
        placements=valid.placements[:4] + (_normal_placement(race.entries[0].id, 5),),
    )
    with pytest.raises(Win5ResultInvariantError, match="each RaceEntry at most once"):
        validate_normal_round_result_structure(round_, duplicate)


def test_special_round_result_is_one_gate_winner_and_does_not_require_reference_entry() -> None:
    round_ = Win5Round(
        13,
        10,
        "제4회 Special",
        Win5RoundType.SPECIAL,
        Win5RoundStatus.CLOSED,
        (_race(701, (1, 3)), _race(702)),
    )
    result = Win5RaceResult(race_id=701, placements=(_special_winner(5),))
    validate_special_round_result_structure(round_, result)

    with pytest.raises(Win5ResultInvariantError):
        validate_special_round_result_structure(
            round_,
            Win5RaceResult(race_id=701, placements=(_special_winner(0),)),
        )

    with pytest.raises(Win5ResultInvariantError):
        validate_special_round_result_structure(
            round_,
            Win5RaceResult(race_id=999, placements=(_special_winner(1),)),
        )


def test_no_framework_dependency_leaks_into_win5_domain() -> None:
    package_root = Path("src/uma_st2/domain/win5")
    package_files = [
        package_root / "__init__.py",
        package_root / "enums.py",
        package_root / "errors.py",
        package_root / "models.py",
        package_root / "invariants.py",
        package_root / "scoring.py",
    ]
    banned_modules = {
        "alembic",
        "discord",
        "fastapi",
        "sqlalchemy",
        "uma_st2.application",
        "uma_st2.adapters",
        "uma_st2.infrastructure",
    }

    for file_path in package_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                imported_modules = [node.module]
            else:
                continue

            for module_name in imported_modules:
                normalized = module_name.lower()
                for banned in banned_modules:
                    if normalized == banned or normalized.startswith(f"{banned}."):
                        pytest.fail(f"Forbidden dependency detected: {module_name}")
