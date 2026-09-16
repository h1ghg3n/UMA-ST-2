"""WIN5 member mutation application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.win5 import (
    CancelledWin5Submission,
    CancelWin5Submission,
    SavedWin5Submission,
    SaveWin5Submission,
    Win5EmptyPickSetUnsupportedError,
    Win5MemberApprovalPendingError,
    Win5MemberCommands,
    Win5MemberIdentityError,
    Win5MemberPersona,
    Win5RoundMutationTarget,
    Win5RoundUnavailableError,
    Win5SubmissionAuditRecord,
    Win5SubmissionAuditSnapshot,
    Win5SubmissionInvalidError,
    Win5SubmissionMutationTarget,
    Win5SubmissionPickAuditSnapshot,
    Win5SubmissionPickInput,
    Win5SubmissionUnavailableError,
    Win5SubmissionVersionConflictError,
)
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.win5 import (
    Win5Race,
    Win5RaceEntry,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

NOW = datetime(2026, 8, 25, 3, 0, tzinfo=UTC)


def _round(
    *,
    status: Win5RoundStatus = Win5RoundStatus.OPEN,
    season_status: Win5SeasonStatus = Win5SeasonStatus.ACTIVE,
    type_: Win5RoundType = Win5RoundType.NORMAL,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5RoundMutationTarget:
    return Win5RoundMutationTarget(
        id=11,
        season_id=7,
        name="제2회 아리마 기념",
        type=type_,
        season_status=season_status,
        status=status,
        source_kind=source_kind,
    )


def _normal_races() -> tuple[Win5Race, ...]:
    return (
        Win5Race(
            id=17,
            name="아리마 기념",
            entries=tuple(
                Win5RaceEntry(
                    id=100 + gate_number,
                    race_id=17,
                    gate_number=gate_number,
                    name=f"entry-{gate_number}",
                )
                for gate_number in range(1, 6)
            ),
        ),
    )


def _special_races() -> tuple[Win5Race, ...]:
    return (
        Win5Race(id=21, name="race-21"),
        Win5Race(id=22, name="race-22"),
        Win5Race(id=23, name="race-23"),
    )


def _member(
    *,
    status: PersonaStatus = PersonaStatus.NORMAL,
    has_eligible_game_account: bool = True,
) -> Win5MemberPersona:
    return Win5MemberPersona(
        id="persona-1",
        status=status,
        has_eligible_game_account=has_eligible_game_account,
    )


def _target(
    *,
    submission_id: int = 13,
    version: int = 4,
    status: Win5SubmissionStatus = Win5SubmissionStatus.ACCEPTED,
    persona_id: str = "persona-1",
    round_id: int = 11,
    active_marker: bool | None = True,
    tier: Win5SubmissionTier = Win5SubmissionTier.TOP3,
    picks: tuple[Win5SubmissionPickAuditSnapshot, ...] | None = None,
) -> Win5SubmissionMutationTarget:
    return Win5SubmissionMutationTarget(
        id=submission_id,
        round_id=round_id,
        active_marker=active_marker,
        snapshot=Win5SubmissionAuditSnapshot(
            persona_id=persona_id,
            tier=tier,
            status=status,
            version=version,
            picks=(
                Win5SubmissionPickAuditSnapshot(
                    race_id=17,
                    position=1,
                    race_entry_id=101,
                    gate_number=None,
                ),
            )
            if picks is None
            else picks,
        ),
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        round_: Win5RoundMutationTarget | None = None,
        member: Win5MemberPersona | None = None,
        target: Win5SubmissionMutationTarget | None = None,
        accepted_target: Win5SubmissionMutationTarget | None = None,
        races: tuple[Win5Race, ...] | None = None,
        cancel_changed: bool = True,
        update_changed: bool = True,
        created_submission_id: int = 19,
    ) -> None:
        self.round = _round() if round_ is None else round_
        self.member = _member() if member is None else member
        self.target = _target() if target is None else target
        self.accepted_target = accepted_target
        self.races = _normal_races() if races is None else races
        self.cancel_changed = cancel_changed
        self.update_changed = update_changed
        self.created_submission_id = created_submission_id
        self.calls: list[tuple[str, object]] = []
        self.audits: list[dict[str, object]] = []

    def lock_round(self, *, round_id: int) -> Win5RoundMutationTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def lock_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None:
        self.calls.append(("lock_member_persona", discord_user_id))
        return self.member

    def load_round_races(
        self,
        *,
        round_id: int,
        lock_race_ids: tuple[int, ...],
    ) -> tuple[Win5Race, ...]:
        self.calls.append(("load_round_races", (round_id, lock_race_ids)))
        return self.races

    def lock_accepted_submission(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5SubmissionMutationTarget | None:
        self.calls.append(("lock_accepted_submission", (round_id, persona_id)))
        return self.accepted_target

    def lock_submission(
        self,
        *,
        submission_id: int,
        round_id: int,
        persona_id: str,
    ) -> Win5SubmissionMutationTarget | None:
        self.calls.append(("lock_submission", (submission_id, round_id, persona_id)))
        return self.target

    def cancel_submission(
        self,
        *,
        submission_id: int,
        expected_version: int,
        updated_at: datetime,
    ) -> bool:
        self.calls.append(
            (
                "cancel_submission",
                (submission_id, expected_version, updated_at),
            )
        )
        return self.cancel_changed

    def create_accepted_submission(
        self,
        *,
        round_id: int,
        persona_id: str,
        tier: Win5SubmissionTier,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
        created_at: datetime,
    ) -> int:
        self.calls.append(
            (
                "create_accepted_submission",
                (round_id, persona_id, tier, picks, created_at),
            )
        )
        return self.created_submission_id

    def update_accepted_submission(
        self,
        *,
        submission_id: int,
        expected_version: int,
        tier: Win5SubmissionTier,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
        updated_at: datetime,
    ) -> bool:
        self.calls.append(
            (
                "update_accepted_submission",
                (submission_id, expected_version, tier, picks, updated_at),
            )
        )
        return self.update_changed

    def add_submission_audit(
        self,
        *,
        record: Win5SubmissionAuditRecord,
        actor_discord_user_id: str,
        guild_id: str | None,
        correlation_id: str | None,
        reason: str | None,
        created_at: datetime,
    ) -> None:
        self.calls.append(("add_submission_audit", record))
        self.audits.append(
            {
                "record": record,
                "actor_discord_user_id": actor_discord_user_id,
                "guild_id": guild_id,
                "correlation_id": correlation_id,
                "reason": reason,
                "created_at": created_at,
            }
        )


@dataclass
class RecordingUnitOfWork:
    win5_member_commands: RecordingRepository
    entered: bool = False
    exited: bool = False
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commit_count == 0 and self.rollback_count == 0:
            self.rollback_count += 1
        self.exited = True
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(repository: RecordingRepository) -> tuple[Win5MemberCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5MemberCommands(CommandRunner(factory), clock=lambda: NOW), factory


def _command(*, expected_version: int = 4, reason: str | None = None) -> CancelWin5Submission:
    return CancelWin5Submission(
        round_id=11,
        submission_id=13,
        expected_version=expected_version,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="interaction-55",
        reason=reason,
    )


def _normal_pick(*, position: int, race_entry_id: int) -> Win5SubmissionPickInput:
    return Win5SubmissionPickInput(
        race_id=17,
        position=position,
        race_entry_id=race_entry_id,
    )


def _save_command(
    *,
    tier: Win5SubmissionTier = Win5SubmissionTier.TOP3,
    picks: tuple[Win5SubmissionPickInput, ...] = (_normal_pick(position=1, race_entry_id=101),),
    submission_id: int | None = None,
    expected_version: int | None = None,
) -> SaveWin5Submission:
    return SaveWin5Submission(
        round_id=11,
        tier=tier,
        picks=picks,
        actor_discord_user_id="123456789",
        submission_id=submission_id,
        expected_version=expected_version,
        guild_id="987654321",
        correlation_id="interaction-save-55",
    )


def test_create_submission_saves_partial_desired_state_and_one_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)
    command = _save_command(
        picks=(
            _normal_pick(position=3, race_entry_id=103),
            _normal_pick(position=1, race_entry_id=101),
        )
    )

    result = commands.save_submission(command)

    assert result == SavedWin5Submission(
        season_id=7,
        round_id=11,
        submission_id=19,
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP3,
        status=Win5SubmissionStatus.ACCEPTED,
        version=1,
        picks=(
            Win5SubmissionPickAuditSnapshot(17, 1, 101, None),
            Win5SubmissionPickAuditSnapshot(17, 3, 103, None),
        ),
    )
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "lock_member_persona",
        "lock_accepted_submission",
        "load_round_races",
        "create_accepted_submission",
        "add_submission_audit",
    ]
    record = repository.audits[0]["record"]
    assert isinstance(record, Win5SubmissionAuditRecord)
    assert record.before_data is None
    assert record.after_data["version"] == 1
    assert record.after_data["picks"] == [
        {"race_id": 17, "position": 1, "race_entry_id": 101, "gate_number": None},
        {"race_id": 17, "position": 3, "race_entry_id": 103, "gate_number": None},
    ]
    assert factory.created[0].commit_count == 1


def test_update_submission_replaces_adds_and_deletes_picks_with_one_version_change() -> None:
    before_picks = (
        Win5SubmissionPickAuditSnapshot(17, 1, 101, None),
        Win5SubmissionPickAuditSnapshot(17, 2, 102, None),
    )
    repository = RecordingRepository(target=_target(picks=before_picks))
    commands, _ = _commands(repository)

    result = commands.save_submission(
        _save_command(
            submission_id=13,
            expected_version=4,
            picks=(
                _normal_pick(position=1, race_entry_id=104),
                _normal_pick(position=3, race_entry_id=103),
            ),
        )
    )

    assert result.version == 5
    assert result.picks == (
        Win5SubmissionPickAuditSnapshot(17, 1, 104, None),
        Win5SubmissionPickAuditSnapshot(17, 3, 103, None),
    )
    assert [name for name, _ in repository.calls][-2:] == [
        "update_accepted_submission",
        "add_submission_audit",
    ]
    record = repository.audits[0]["record"]
    assert isinstance(record, Win5SubmissionAuditRecord)
    before_data = record.before_data
    assert before_data is not None
    assert before_data["version"] == 4
    assert record.after_data["version"] == 5


def test_exact_no_op_returns_current_state_without_version_or_audit_write() -> None:
    target = _target(
        picks=(
            Win5SubmissionPickAuditSnapshot(17, 3, 103, None),
            Win5SubmissionPickAuditSnapshot(17, 1, 101, None),
        )
    )
    repository = RecordingRepository(target=target)
    commands, factory = _commands(repository)

    result = commands.save_submission(
        _save_command(
            submission_id=13,
            expected_version=4,
            picks=(
                _normal_pick(position=1, race_entry_id=101),
                _normal_pick(position=3, race_entry_id=103),
            ),
        )
    )

    assert result.version == 4
    assert [name for name, _ in repository.calls][-1] == "load_round_races"
    assert repository.audits == []
    assert factory.created[0].commit_count == 1


def test_stale_save_is_zero_write_and_does_not_load_mutation_graph() -> None:
    repository = RecordingRepository(target=_target(version=5))
    commands, factory = _commands(repository)

    with pytest.raises(Win5SubmissionVersionConflictError, match="expected 4, current 5"):
        commands.save_submission(_save_command(submission_id=13, expected_version=4))

    assert [name for name, _ in repository.calls][-1] == "lock_submission"
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


def test_invalid_tier_position_is_zero_write() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    with pytest.raises(Win5SubmissionInvalidError, match="TOP1 picks"):
        commands.save_submission(
            _save_command(
                tier=Win5SubmissionTier.TOP1,
                picks=(_normal_pick(position=2, race_entry_id=102),),
            )
        )

    assert [name for name, _ in repository.calls][-1] == "load_round_races"
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


def test_special_save_locks_only_addressed_races_in_deterministic_order() -> None:
    repository = RecordingRepository(
        round_=_round(type_=Win5RoundType.SPECIAL),
        races=_special_races(),
    )
    commands, _ = _commands(repository)

    result = commands.save_submission(
        _save_command(
            tier=Win5SubmissionTier.SPECIAL_WINNER,
            picks=(
                Win5SubmissionPickInput(race_id=23, position=1, gate_number=8),
                Win5SubmissionPickInput(race_id=21, position=1, gate_number=2),
            ),
        )
    )

    assert result.version == 1
    assert ("load_round_races", (11, (21, 23))) in repository.calls


def test_empty_desired_pick_set_is_rejected_without_opening_uow() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    with pytest.raises(Win5EmptyPickSetUnsupportedError, match="not a supported save action"):
        commands.save_submission(_save_command(picks=()))

    assert repository.calls == []
    assert factory.created == []


def test_persona_without_pid_bearing_game_account_cannot_save_submission() -> None:
    repository = RecordingRepository(member=_member(has_eligible_game_account=False))
    commands, _ = _commands(repository)

    with pytest.raises(Win5MemberIdentityError, match="non-NULL PID GameAccount"):
        commands.save_submission(_save_command())

    assert [name for name, _ in repository.calls] == ["lock_round", "lock_member_persona"]
    assert repository.audits == []


@pytest.mark.parametrize("operation", ["save", "cancel"])
def test_pending_approval_persona_cannot_mutate_submission(operation: str) -> None:
    repository = RecordingRepository(member=_member(status=PersonaStatus.PENDING_APPROVAL))
    commands, factory = _commands(repository)

    with pytest.raises(Win5MemberApprovalPendingError, match="approval is pending"):
        if operation == "save":
            commands.save_submission(_save_command())
        else:
            commands.cancel_submission(_command())

    assert [name for name, _ in repository.calls] == ["lock_round", "lock_member_persona"]
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


def test_create_from_stale_panel_conflicts_with_current_accepted_submission() -> None:
    repository = RecordingRepository(accepted_target=_target(version=2))
    commands, _ = _commands(repository)

    with pytest.raises(Win5SubmissionVersionConflictError, match="already exists"):
        commands.save_submission(_save_command())

    assert [name for name, _ in repository.calls][-1] == "lock_accepted_submission"
    assert repository.audits == []


def test_conditional_save_update_miss_is_zero_audit_conflict() -> None:
    repository = RecordingRepository(update_changed=False)
    commands, _ = _commands(repository)

    with pytest.raises(Win5SubmissionVersionConflictError, match="changed while"):
        commands.save_submission(
            _save_command(
                submission_id=13,
                expected_version=4,
                picks=(_normal_pick(position=1, race_entry_id=102),),
            )
        )

    assert [name for name, _ in repository.calls][-1] == "update_accepted_submission"
    assert repository.audits == []


def test_cancel_submission_commits_preserved_history_and_one_canonical_audit() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.cancel_submission(_command(reason="사용자 요청"))

    assert result == CancelledWin5Submission(
        season_id=7,
        round_id=11,
        submission_id=13,
        persona_id="persona-1",
        tier=Win5SubmissionTier.TOP3,
        status=Win5SubmissionStatus.CANCELLED,
        version=5,
    )
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "lock_member_persona",
        "lock_submission",
        "cancel_submission",
        "add_submission_audit",
    ]
    assert factory.created[0].commit_count == 1
    assert factory.created[0].rollback_count == 0

    audit = repository.audits[0]
    record = audit["record"]
    assert isinstance(record, Win5SubmissionAuditRecord)
    assert record.context.season_id == 7
    assert record.context.round_id == 11
    assert record.context.submission_id == 13
    assert record.before_data is not None
    assert record.before_data["version"] == 4
    assert record.after_data["version"] == 5
    assert record.before_data["picks"] == record.after_data["picks"]
    assert audit == {
        "record": record,
        "actor_discord_user_id": "123456789",
        "guild_id": "987654321",
        "correlation_id": "interaction-55",
        "reason": "사용자 요청",
        "created_at": NOW,
    }


def test_stale_cancel_is_zero_write_and_does_not_create_audit() -> None:
    repository = RecordingRepository(target=_target(version=5))
    commands, factory = _commands(repository)

    with pytest.raises(Win5SubmissionVersionConflictError, match="expected 4, current 5"):
        commands.cancel_submission(_command(expected_version=4))

    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "lock_member_persona",
        "lock_submission",
    ]
    assert repository.audits == []
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_cancelled_row_from_a_stale_view_reports_version_conflict_first() -> None:
    repository = RecordingRepository(target=_target(version=5, status=Win5SubmissionStatus.CANCELLED))
    commands, _ = _commands(repository)

    with pytest.raises(Win5SubmissionVersionConflictError, match="expected 4, current 5"):
        commands.cancel_submission(_command(expected_version=4))

    assert repository.audits == []


def test_accepted_row_without_active_marker_is_unavailable_and_zero_write() -> None:
    repository = RecordingRepository(target=_target(active_marker=None))
    commands, _ = _commands(repository)

    with pytest.raises(Win5SubmissionUnavailableError, match="no longer accepted"):
        commands.cancel_submission(_command())

    assert [name for name, _ in repository.calls][-1] == "lock_submission"
    assert repository.audits == []


@pytest.mark.parametrize(
    "member",
    [
        _member(status=PersonaStatus.EXPELLED),
        _member(status=PersonaStatus.WITHDRAWN),
        _member(has_eligible_game_account=False),
    ],
)
def test_ineligible_persona_cannot_cancel_submission(member: Win5MemberPersona) -> None:
    repository = RecordingRepository(member=member)
    commands, _ = _commands(repository)

    with pytest.raises(Win5MemberIdentityError, match="non-NULL PID GameAccount"):
        commands.cancel_submission(_command())

    assert [name for name, _ in repository.calls] == ["lock_round", "lock_member_persona"]


@pytest.mark.parametrize(
    ("round_status", "season_status"),
    [
        (Win5RoundStatus.CLOSED, Win5SeasonStatus.ACTIVE),
        (Win5RoundStatus.OPEN, Win5SeasonStatus.CLOSED),
    ],
)
def test_cancel_requires_open_round_in_active_season(
    round_status: Win5RoundStatus,
    season_status: Win5SeasonStatus,
) -> None:
    repository = RecordingRepository(round_=_round(status=round_status, season_status=season_status))
    commands, _ = _commands(repository)

    with pytest.raises(Win5RoundUnavailableError, match="not open"):
        commands.cancel_submission(_command())

    assert [name for name, _ in repository.calls] == ["lock_round"]


@pytest.mark.parametrize(
    "target",
    [
        _target(persona_id="persona-2"),
        _target(round_id=12),
    ],
)
def test_cancel_rejects_submission_outside_resolved_actor_and_round(
    target: Win5SubmissionMutationTarget,
) -> None:
    repository = RecordingRepository(target=target)
    commands, _ = _commands(repository)

    with pytest.raises(Win5SubmissionUnavailableError, match="not available"):
        commands.cancel_submission(_command())

    assert repository.audits == []


def test_conditional_update_miss_is_zero_audit_conflict() -> None:
    repository = RecordingRepository(cancel_changed=False)
    commands, _ = _commands(repository)

    with pytest.raises(Win5SubmissionVersionConflictError, match="changed while"):
        commands.cancel_submission(_command())

    assert [name for name, _ in repository.calls][-1] == "cancel_submission"
    assert repository.audits == []


def test_cancel_rejects_naive_clock_before_writing_submission_or_audit() -> None:
    repository = RecordingRepository()
    factory = RecordingFactory(repository)
    commands = Win5MemberCommands(
        CommandRunner(factory),
        clock=lambda: datetime(2026, 8, 25, 3, 0),
    )

    with pytest.raises(ValueError, match="clock result must be timezone-aware"):
        commands.cancel_submission(_command())

    assert [name for name, _ in repository.calls][-1] == "lock_submission"
    assert repository.audits == []
    assert factory.created[0].rollback_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"round_id": 0},
        {"submission_id": False},
        {"expected_version": -1},
        {"actor_discord_user_id": ""},
        {"guild_id": ""},
        {"correlation_id": "x" * 129},
        {"reason": "x" * 256},
    ],
)
def test_cancel_command_rejects_invalid_envelope(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "round_id": 11,
        "submission_id": 13,
        "expected_version": 4,
        "actor_discord_user_id": "123456789",
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        CancelWin5Submission(**values)  # type: ignore[arg-type]


def test_imported_round_rejects_member_save_and_cancel_before_identity_resolution() -> None:
    imported = _round(source_kind=Win5RoundSourceKind.IMPORTED_V1)

    save_repository = RecordingRepository(round_=imported)
    with pytest.raises(Win5RoundUnavailableError, match="not open"):
        _commands(save_repository)[0].save_submission(_save_command())
    assert [name for name, _ in save_repository.calls] == ["lock_round"]

    cancel_repository = RecordingRepository(round_=imported)
    with pytest.raises(Win5RoundUnavailableError, match="not open"):
        _commands(cancel_repository)[0].cancel_submission(_command())
    assert [name for name, _ in cancel_repository.calls] == ["lock_round"]
