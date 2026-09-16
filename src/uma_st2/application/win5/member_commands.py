"""Member-facing WIN5 mutation use cases and persistence ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5Race,
    Win5Round,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5Submission,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    validate_submission_for_round,
)
from uma_st2.shared import normalize_utc_datetime

from .member_identity import Win5MemberPersona
from .submission_audit import (
    Win5SubmissionAuditContext,
    Win5SubmissionAuditRecord,
    Win5SubmissionAuditSnapshot,
    Win5SubmissionAuditType,
    Win5SubmissionPickAuditSnapshot,
)


class Win5MemberCommandError(ValueError):
    """Base error for rejected member WIN5 mutations."""


class Win5MemberIdentityError(Win5MemberCommandError):
    """The Discord actor has no currently eligible WIN5 identity."""


class Win5MemberApprovalPendingError(Win5MemberIdentityError):
    """The actor Persona is read-only while approval is pending."""


class Win5RoundUnavailableError(Win5MemberCommandError):
    """The requested Round cannot currently accept member mutations."""


class Win5SubmissionUnavailableError(Win5MemberCommandError):
    """The requested Submission is not an accepted row owned by the actor."""


class Win5SubmissionVersionConflictError(Win5MemberCommandError):
    """The command was based on a stale Submission version."""


class Win5SubmissionInvalidError(Win5MemberCommandError):
    """The desired Submission does not satisfy the current Round contract."""


class Win5EmptyPickSetUnsupportedError(Win5MemberCommandError):
    """A save command has no persistence meaning when its desired pick set is empty."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        qualifier = "non-empty " if not optional else "non-empty optional "
        raise ValueError(f"{field_name} must be a {qualifier}string no longer than {max_length} characters.")


@dataclass(frozen=True, slots=True)
class Win5SubmissionPickInput:
    """One desired canonical pick supplied to the member save use case."""

    race_id: int
    position: int
    race_entry_id: int | None = None
    gate_number: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.position, field_name="position")
        if (self.race_entry_id is None) == (self.gate_number is None):
            raise ValueError("Exactly one of race_entry_id and gate_number is required.")
        if self.race_entry_id is not None:
            _require_positive_int(self.race_entry_id, field_name="race_entry_id")
        if self.gate_number is not None:
            _require_positive_int(self.gate_number, field_name="gate_number")

    def to_audit_snapshot(self) -> Win5SubmissionPickAuditSnapshot:
        """Project the desired pick into canonical audit/persistence fields."""

        return Win5SubmissionPickAuditSnapshot(
            race_id=self.race_id,
            position=self.position,
            race_entry_id=self.race_entry_id,
            gate_number=self.gate_number,
        )


@dataclass(frozen=True, slots=True)
class SaveWin5Submission:
    """Create or update one non-empty desired accepted Submission state."""

    round_id: int
    tier: Win5SubmissionTier
    picks: tuple[Win5SubmissionPickInput, ...]
    actor_discord_user_id: str
    submission_id: int | None = None
    expected_version: int | None = None
    guild_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        object.__setattr__(self, "picks", tuple(self.picks))
        if (self.submission_id is None) != (self.expected_version is None):
            raise ValueError("submission_id and expected_version must both be present or both be absent.")
        if self.submission_id is not None:
            _require_positive_int(self.submission_id, field_name="submission_id")
        if self.expected_version is not None:
            _require_positive_int(self.expected_version, field_name="expected_version")
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32, optional=True)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )

    @property
    def is_creation(self) -> bool:
        """Return whether this command starts a replacement/new accepted row."""

        return self.submission_id is None


@dataclass(frozen=True, slots=True)
class CancelWin5Submission:
    """Expected-version command for one member-owned Submission cancellation."""

    round_id: int
    submission_id: int
    expected_version: int
    actor_discord_user_id: str
    guild_id: str | None = None
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_positive_int(self.expected_version, field_name="expected_version")
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32, optional=True)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )
        _require_bounded_string(self.reason, field_name="reason", max_length=255, optional=True)


@dataclass(frozen=True, slots=True)
class Win5RoundMutationTarget:
    """Persisted Season/Round state read after locking the Round root."""

    id: int
    season_id: int
    name: str
    type: Win5RoundType
    source_kind: Win5RoundSourceKind
    season_status: Win5SeasonStatus
    status: Win5RoundStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.season_id, field_name="season_id")
        _require_bounded_string(self.name, field_name="name", max_length=100)
        object.__setattr__(self, "type", Win5RoundType(self.type))
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "status", Win5RoundStatus(self.status))
        object.__setattr__(self, "source_kind", Win5RoundSourceKind(self.source_kind))


@dataclass(frozen=True, slots=True)
class Win5SubmissionMutationTarget:
    """Locked persisted Submission and its canonical audit snapshot."""

    id: int
    round_id: int
    active_marker: bool | None
    snapshot: Win5SubmissionAuditSnapshot

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.round_id, field_name="round_id")
        if self.active_marker is not None and not isinstance(self.active_marker, bool):
            raise ValueError("active_marker must be a boolean or None.")


@dataclass(frozen=True, slots=True)
class CancelledWin5Submission:
    """Closed-session result returned after a successful cancellation."""

    season_id: int
    round_id: int
    submission_id: int
    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    version: int


@dataclass(frozen=True, slots=True)
class SavedWin5Submission:
    """Canonical accepted state returned after a save or exact no-op."""

    season_id: int
    round_id: int
    submission_id: int
    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    version: int
    picks: tuple[Win5SubmissionPickAuditSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        object.__setattr__(self, "status", Win5SubmissionStatus(self.status))
        object.__setattr__(
            self,
            "picks",
            tuple(sorted(self.picks, key=lambda pick: (pick.race_id, pick.position))),
        )


class Win5MemberCommandRepository(Protocol):
    """Persistence operations required by member WIN5 mutations."""

    def lock_round(self, *, round_id: int) -> Win5RoundMutationTarget | None: ...

    def lock_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None: ...

    def load_round_races(
        self,
        *,
        round_id: int,
        lock_race_ids: tuple[int, ...],
    ) -> tuple[Win5Race, ...]: ...

    def lock_accepted_submission(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5SubmissionMutationTarget | None: ...

    def lock_submission(
        self,
        *,
        submission_id: int,
        round_id: int,
        persona_id: str,
    ) -> Win5SubmissionMutationTarget | None: ...

    def cancel_submission(
        self,
        *,
        submission_id: int,
        expected_version: int,
        updated_at: datetime,
    ) -> bool: ...

    def create_accepted_submission(
        self,
        *,
        round_id: int,
        persona_id: str,
        tier: Win5SubmissionTier,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
        created_at: datetime,
    ) -> int: ...

    def update_accepted_submission(
        self,
        *,
        submission_id: int,
        expected_version: int,
        tier: Win5SubmissionTier,
        picks: tuple[Win5SubmissionPickAuditSnapshot, ...],
        updated_at: datetime,
    ) -> bool: ...

    def add_submission_audit(
        self,
        *,
        record: Win5SubmissionAuditRecord,
        actor_discord_user_id: str,
        guild_id: str | None,
        correlation_id: str | None,
        reason: str | None,
        created_at: datetime,
    ) -> None: ...


class Win5MemberCommandUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the bounded WIN5 mutation repository."""

    @property
    def win5_member_commands(self) -> Win5MemberCommandRepository: ...


@dataclass(frozen=True, slots=True)
class Win5MemberCommands:
    """Application entry point for member-facing WIN5 mutations."""

    command_runner: CommandRunner[Win5MemberCommandUnitOfWork]
    clock: Callable[[], datetime]

    def save_submission(self, command: SaveWin5Submission) -> SavedWin5Submission:
        """Save one non-empty desired pick state for an open Round."""

        if not command.picks:
            raise Win5EmptyPickSetUnsupportedError(
                "Empty desired pick sets are not a supported save action; "
                "use explicit cancellation for an accepted Submission."
            )
        return self.command_runner.run(
            lambda unit_of_work: self._save_submission(unit_of_work.win5_member_commands, command)
        )

    def cancel_submission(self, command: CancelWin5Submission) -> CancelledWin5Submission:
        """Cancel one accepted Submission while preserving its row and picks."""

        return self.command_runner.run(
            lambda unit_of_work: self._cancel_submission(unit_of_work.win5_member_commands, command)
        )

    def _save_submission(
        self,
        repository: Win5MemberCommandRepository,
        command: SaveWin5Submission,
    ) -> SavedWin5Submission:
        round_ = self._require_open_round(repository, round_id=command.round_id)
        member = self._require_active_member(
            repository,
            actor_discord_user_id=command.actor_discord_user_id,
        )

        target: Win5SubmissionMutationTarget | None
        if command.is_creation:
            target = repository.lock_accepted_submission(round_id=round_.id, persona_id=member.id)
            if target is not None:
                raise Win5SubmissionVersionConflictError(
                    "An accepted Submission already exists: "
                    f"submission_id={target.id}, current version={target.snapshot.version}."
                )
        else:
            submission_id = command.submission_id
            expected_version = command.expected_version
            if submission_id is None or expected_version is None:
                raise RuntimeError("An update command requires Submission identity and version.")
            target = repository.lock_submission(
                submission_id=submission_id,
                round_id=round_.id,
                persona_id=member.id,
            )
            if target is None or target.round_id != round_.id or target.snapshot.persona_id != member.id:
                raise Win5SubmissionUnavailableError("Submission is not available to this actor and Round.")
            if target.snapshot.version != expected_version:
                raise Win5SubmissionVersionConflictError(
                    f"Submission version conflict: expected {expected_version}, current {target.snapshot.version}."
                )
            if target.snapshot.status != Win5SubmissionStatus.ACCEPTED or target.active_marker is not True:
                raise Win5SubmissionUnavailableError("Submission is no longer accepted.")

        before = None if target is None else target.snapshot
        desired_picks = tuple(
            sorted(
                (pick.to_audit_snapshot() for pick in command.picks),
                key=lambda pick: (pick.race_id, pick.position),
            )
        )
        addressed_race_ids = {pick.race_id for pick in desired_picks}
        if before is not None:
            addressed_race_ids.update(pick.race_id for pick in before.picks)
        lock_race_ids = tuple(sorted(addressed_race_ids)) if round_.type == Win5RoundType.SPECIAL else ()
        races = repository.load_round_races(
            round_id=round_.id,
            lock_race_ids=lock_race_ids,
        )
        domain_round = Win5Round(
            id=round_.id,
            season_id=round_.season_id,
            name=round_.name,
            type=round_.type,
            status=round_.status,
            races=races,
        )
        pending_submission_id: int | str = target.id if target is not None else "pending"
        domain_submission = Win5Submission(
            id=pending_submission_id,
            round_id=round_.id,
            persona_id=member.id,
            tier=command.tier,
            status=Win5SubmissionStatus.ACCEPTED,
            picks=tuple(
                Win5SubmissionPick(
                    id=index,
                    submission_id=pending_submission_id,
                    race_id=pick.race_id,
                    race_entry_id=pick.race_entry_id,
                    gate_number=pick.gate_number,
                    position=pick.position,
                )
                for index, pick in enumerate(desired_picks, start=1)
            ),
        )
        try:
            validate_submission_for_round(domain_round, domain_submission)
        except Win5DomainError as exc:
            raise Win5SubmissionInvalidError(str(exc)) from exc

        before_picks = (
            () if before is None else tuple(sorted(before.picks, key=lambda pick: (pick.race_id, pick.position)))
        )
        if before is not None and before.tier == command.tier and before_picks == desired_picks:
            return self._saved_result(
                round_=round_,
                submission_id=target.id,
                snapshot=before,
            )

        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        if target is None:
            submission_id = repository.create_accepted_submission(
                round_id=round_.id,
                persona_id=member.id,
                tier=command.tier,
                picks=desired_picks,
                created_at=changed_at,
            )
            after = Win5SubmissionAuditSnapshot(
                persona_id=member.id,
                tier=command.tier,
                status=Win5SubmissionStatus.ACCEPTED,
                version=1,
                picks=desired_picks,
            )
        else:
            after = Win5SubmissionAuditSnapshot(
                persona_id=member.id,
                tier=command.tier,
                status=Win5SubmissionStatus.ACCEPTED,
                version=before.version + 1,
                picks=desired_picks,
            )
            if not repository.update_accepted_submission(
                submission_id=target.id,
                expected_version=before.version,
                tier=after.tier,
                picks=after.picks,
                updated_at=changed_at,
            ):
                raise Win5SubmissionVersionConflictError(
                    "Submission changed while the desired pick state was being applied."
                )
            submission_id = target.id

        audit_record = Win5SubmissionAuditRecord(
            context=Win5SubmissionAuditContext(
                season_id=round_.season_id,
                round_id=round_.id,
                submission_id=submission_id,
            ),
            type=Win5SubmissionAuditType.SAVED,
            before=before,
            after=after,
        )
        repository.add_submission_audit(
            record=audit_record,
            actor_discord_user_id=command.actor_discord_user_id,
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            reason=None,
            created_at=changed_at,
        )
        return self._saved_result(
            round_=round_,
            submission_id=submission_id,
            snapshot=after,
        )

    @staticmethod
    def _require_open_round(
        repository: Win5MemberCommandRepository,
        *,
        round_id: int,
    ) -> Win5RoundMutationTarget:
        round_ = repository.lock_round(round_id=round_id)
        if (
            round_ is None
            or round_.source_kind != Win5RoundSourceKind.NATIVE_V2
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.OPEN
        ):
            raise Win5RoundUnavailableError("WIN5 Round is not open in an active Season.")
        return round_

    @staticmethod
    def _require_active_member(
        repository: Win5MemberCommandRepository,
        *,
        actor_discord_user_id: str,
    ) -> Win5MemberPersona:
        member = repository.lock_member_persona(discord_user_id=actor_discord_user_id)
        if member is not None and member.status is PersonaStatus.PENDING_APPROVAL:
            raise Win5MemberApprovalPendingError("Discord actor Persona approval is pending.")
        if member is None or not member.is_eligible:
            raise Win5MemberIdentityError(
                "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
            )
        return member

    @staticmethod
    def _saved_result(
        *,
        round_: Win5RoundMutationTarget,
        submission_id: int,
        snapshot: Win5SubmissionAuditSnapshot,
    ) -> SavedWin5Submission:
        return SavedWin5Submission(
            season_id=round_.season_id,
            round_id=round_.id,
            submission_id=submission_id,
            persona_id=snapshot.persona_id,
            tier=snapshot.tier,
            status=snapshot.status,
            version=snapshot.version,
            picks=snapshot.picks,
        )

    def _cancel_submission(
        self,
        repository: Win5MemberCommandRepository,
        command: CancelWin5Submission,
    ) -> CancelledWin5Submission:
        round_ = repository.lock_round(round_id=command.round_id)
        if (
            round_ is None
            or round_.source_kind != Win5RoundSourceKind.NATIVE_V2
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.OPEN
        ):
            raise Win5RoundUnavailableError("WIN5 Round is not open in an active Season.")

        member = repository.lock_member_persona(discord_user_id=command.actor_discord_user_id)
        if member is not None and member.status is PersonaStatus.PENDING_APPROVAL:
            raise Win5MemberApprovalPendingError("Discord actor Persona approval is pending.")
        if member is None or not member.is_eligible:
            raise Win5MemberIdentityError(
                "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
            )

        target = repository.lock_submission(
            submission_id=command.submission_id,
            round_id=round_.id,
            persona_id=member.id,
        )
        if target is None or target.round_id != round_.id or target.snapshot.persona_id != member.id:
            raise Win5SubmissionUnavailableError("Submission is not available to this actor and Round.")

        before = target.snapshot
        if before.version != command.expected_version:
            raise Win5SubmissionVersionConflictError(
                f"Submission version conflict: expected {command.expected_version}, current {before.version}."
            )
        if before.status != Win5SubmissionStatus.ACCEPTED or target.active_marker is not True:
            raise Win5SubmissionUnavailableError("Submission is no longer accepted.")

        after = Win5SubmissionAuditSnapshot(
            persona_id=before.persona_id,
            tier=before.tier,
            status=Win5SubmissionStatus.CANCELLED,
            version=before.version + 1,
            picks=before.picks,
        )
        audit_record = Win5SubmissionAuditRecord(
            context=Win5SubmissionAuditContext(
                season_id=round_.season_id,
                round_id=round_.id,
                submission_id=target.id,
            ),
            type=Win5SubmissionAuditType.CANCELLED,
            before=before,
            after=after,
        )
        changed_at = normalize_utc_datetime(self.clock(), field_name="clock result")
        if not repository.cancel_submission(
            submission_id=target.id,
            expected_version=before.version,
            updated_at=changed_at,
        ):
            raise Win5SubmissionVersionConflictError("Submission changed while cancellation was being applied.")
        repository.add_submission_audit(
            record=audit_record,
            actor_discord_user_id=command.actor_discord_user_id,
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            reason=command.reason,
            created_at=changed_at,
        )

        return CancelledWin5Submission(
            season_id=round_.season_id,
            round_id=round_.id,
            submission_id=target.id,
            persona_id=after.persona_id,
            tier=after.tier,
            status=after.status,
            version=after.version,
        )
