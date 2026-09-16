"""WIN5 Submission audit v1 application contract tests."""

from __future__ import annotations

import pytest

from uma_st2.application.win5 import (
    WIN5_SUBMISSION_AUDIT_SCHEMA_VERSION,
    Win5SubmissionAuditContext,
    Win5SubmissionAuditRecord,
    Win5SubmissionAuditSnapshot,
    Win5SubmissionAuditType,
    Win5SubmissionPickAuditSnapshot,
)
from uma_st2.domain.win5 import Win5SubmissionStatus, Win5SubmissionTier


def _normal_pick(*, race_id: int, position: int, race_entry_id: int) -> Win5SubmissionPickAuditSnapshot:
    return Win5SubmissionPickAuditSnapshot(
        race_id=race_id,
        position=position,
        race_entry_id=race_entry_id,
        gate_number=None,
    )


def _special_pick(*, race_id: int, gate_number: int) -> Win5SubmissionPickAuditSnapshot:
    return Win5SubmissionPickAuditSnapshot(
        race_id=race_id,
        position=1,
        race_entry_id=None,
        gate_number=gate_number,
    )


def _snapshot(
    *,
    version: int,
    status: Win5SubmissionStatus = Win5SubmissionStatus.ACCEPTED,
    picks: tuple[Win5SubmissionPickAuditSnapshot, ...] = (),
) -> Win5SubmissionAuditSnapshot:
    return Win5SubmissionAuditSnapshot(
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP3,
        status=status,
        version=version,
        picks=picks,
    )


def _context() -> Win5SubmissionAuditContext:
    return Win5SubmissionAuditContext(season_id=7, round_id=11, submission_id=13)


def test_audit_v1_payload_is_canonical_and_deterministically_sorted() -> None:
    snapshot = _snapshot(
        version=3,
        picks=(
            _normal_pick(race_id=20, position=1, race_entry_id=208),
            _normal_pick(race_id=10, position=3, race_entry_id=103),
            _normal_pick(race_id=10, position=1, race_entry_id=101),
        ),
    )

    assert WIN5_SUBMISSION_AUDIT_SCHEMA_VERSION == 1
    assert snapshot.to_payload() == {
        "schema_version": 1,
        "persona_id": "persona-1",
        "tier": "TOP3",
        "status": "accepted",
        "version": 3,
        "picks": [
            {"race_id": 10, "position": 1, "race_entry_id": 101, "gate_number": None},
            {"race_id": 10, "position": 3, "race_entry_id": 103, "gate_number": None},
            {"race_id": 20, "position": 1, "race_entry_id": 208, "gate_number": None},
        ],
    }


def test_special_pick_snapshot_uses_gate_identity() -> None:
    assert _special_pick(race_id=20, gate_number=8).to_payload() == {
        "race_id": 20,
        "position": 1,
        "race_entry_id": None,
        "gate_number": 8,
    }


def test_initial_save_uses_context_envelope_and_null_before_snapshot() -> None:
    record = Win5SubmissionAuditRecord(
        context=_context(),
        type=Win5SubmissionAuditType.SAVED,
        before=None,
        after=_snapshot(version=1, picks=(_normal_pick(race_id=10, position=1, race_entry_id=101),)),
    )

    assert record.context == Win5SubmissionAuditContext(season_id=7, round_id=11, submission_id=13)
    assert record.before_data is None
    assert set(record.after_data) == {"schema_version", "persona_id", "tier", "status", "version", "picks"}
    assert "season_id" not in record.after_data
    assert "round_id" not in record.after_data
    assert "submission_id" not in record.after_data


def test_state_changing_save_increments_version_once() -> None:
    before = _snapshot(version=4, picks=(_normal_pick(race_id=10, position=1, race_entry_id=101),))
    after = _snapshot(version=5, picks=(_normal_pick(race_id=10, position=1, race_entry_id=102),))

    record = Win5SubmissionAuditRecord(
        context=_context(),
        type=Win5SubmissionAuditType.SAVED,
        before=before,
        after=after,
    )

    before_data = record.before_data
    assert before_data is not None
    assert before_data["version"] == 4
    assert record.after_data["version"] == 5


@pytest.mark.parametrize("after_version", [4, 6])
def test_update_rejects_reused_or_skipped_version(after_version: int) -> None:
    before = _snapshot(version=4, picks=(_normal_pick(race_id=10, position=1, race_entry_id=101),))
    after = _snapshot(version=after_version, picks=(_normal_pick(race_id=10, position=1, race_entry_id=102),))

    with pytest.raises(ValueError, match="increment version exactly once"):
        Win5SubmissionAuditRecord(
            context=_context(),
            type=Win5SubmissionAuditType.SAVED,
            before=before,
            after=after,
        )


def test_exact_noop_does_not_form_an_audit_record_even_if_pick_order_changes() -> None:
    first = _normal_pick(race_id=10, position=1, race_entry_id=101)
    second = _normal_pick(race_id=10, position=3, race_entry_id=103)

    with pytest.raises(ValueError, match="exact no-op"):
        Win5SubmissionAuditRecord(
            context=_context(),
            type=Win5SubmissionAuditType.SAVED,
            before=_snapshot(version=1, picks=(first, second)),
            after=_snapshot(version=2, picks=(second, first)),
        )


def test_cancellation_records_accepted_to_cancelled_transition() -> None:
    record = Win5SubmissionAuditRecord(
        context=_context(),
        type=Win5SubmissionAuditType.CANCELLED,
        before=_snapshot(version=2),
        after=_snapshot(version=3, status=Win5SubmissionStatus.CANCELLED),
    )

    assert record.type.value == "submission_cancelled"
    before_data = record.before_data
    assert before_data is not None
    assert before_data["status"] == "accepted"
    assert record.after_data["status"] == "cancelled"


def test_cancellation_requires_a_prior_accepted_snapshot() -> None:
    with pytest.raises(ValueError, match="before snapshot"):
        Win5SubmissionAuditRecord(
            context=_context(),
            type=Win5SubmissionAuditType.CANCELLED,
            before=None,
            after=_snapshot(version=1, status=Win5SubmissionStatus.CANCELLED),
        )


def test_cancellation_cannot_modify_the_preserved_submission_content() -> None:
    with pytest.raises(ValueError, match="preserve"):
        Win5SubmissionAuditRecord(
            context=_context(),
            type=Win5SubmissionAuditType.CANCELLED,
            before=_snapshot(version=2, picks=(_normal_pick(race_id=10, position=1, race_entry_id=101),)),
            after=_snapshot(
                version=3,
                status=Win5SubmissionStatus.CANCELLED,
                picks=(_normal_pick(race_id=10, position=1, race_entry_id=102),),
            ),
        )


def test_save_cannot_reactivate_a_cancelled_submission() -> None:
    with pytest.raises(ValueError, match="reactivate"):
        Win5SubmissionAuditRecord(
            context=_context(),
            type=Win5SubmissionAuditType.SAVED,
            before=_snapshot(version=2, status=Win5SubmissionStatus.CANCELLED),
            after=_snapshot(version=3, picks=(_normal_pick(race_id=10, position=1, race_entry_id=101),)),
        )


def test_update_cannot_change_submission_persona_ownership() -> None:
    after = Win5SubmissionAuditSnapshot(
        persona_id="persona-2",
        tier=Win5SubmissionTier.TOP3,
        status=Win5SubmissionStatus.ACCEPTED,
        version=2,
    )

    with pytest.raises(ValueError, match="persona ownership"):
        Win5SubmissionAuditRecord(
            context=_context(),
            type=Win5SubmissionAuditType.SAVED,
            before=_snapshot(version=1),
            after=after,
        )


@pytest.mark.parametrize(
    ("field_name", "values"),
    [
        ("season_id", (0, 11, 13)),
        ("round_id", (7, False, 13)),
        ("submission_id", (7, 11, -1)),
    ],
)
def test_audit_context_requires_positive_persisted_ids(field_name: str, values: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError, match=field_name):
        Win5SubmissionAuditContext(*values)


@pytest.mark.parametrize(
    ("race_entry_id", "gate_number"),
    [(None, None), (10, 3)],
)
def test_pick_snapshot_requires_exactly_one_payload_identity(
    race_entry_id: int | None,
    gate_number: int | None,
) -> None:
    with pytest.raises(ValueError, match="Exactly one"):
        Win5SubmissionPickAuditSnapshot(
            race_id=1,
            position=1,
            race_entry_id=race_entry_id,
            gate_number=gate_number,
        )


def test_snapshot_version_must_be_positive_integer() -> None:
    with pytest.raises(ValueError, match="version"):
        _snapshot(version=0)
