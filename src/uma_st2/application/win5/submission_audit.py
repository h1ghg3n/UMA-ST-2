"""Versioned application payload for WIN5 Submission audit rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from uma_st2.domain.win5 import Win5SubmissionStatus, Win5SubmissionTier

WIN5_SUBMISSION_AUDIT_SCHEMA_VERSION: Final = 1


class Win5SubmissionAuditType(StrEnum):
    """Canonical operation types for participant Submission mutations."""

    SAVED = "submission_saved"
    CANCELLED = "submission_cancelled"


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


@dataclass(frozen=True, slots=True)
class Win5SubmissionAuditContext:
    """Searchable non-FK identifiers stored on ``win5_operations``."""

    season_id: int
    round_id: int
    submission_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.submission_id, field_name="submission_id")


@dataclass(frozen=True, slots=True)
class Win5SubmissionPickAuditSnapshot:
    """Canonical pick fields retained in a Submission snapshot."""

    race_id: int
    position: int
    race_entry_id: int | None
    gate_number: int | None

    def __post_init__(self) -> None:
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.position, field_name="position")
        if (self.race_entry_id is None) == (self.gate_number is None):
            raise ValueError("Exactly one of race_entry_id and gate_number is required.")
        if self.race_entry_id is not None:
            _require_positive_int(self.race_entry_id, field_name="race_entry_id")
        if self.gate_number is not None:
            _require_positive_int(self.gate_number, field_name="gate_number")

    def to_payload(self) -> dict[str, int | None]:
        """Return the JSON-compatible pick representation."""

        return {
            "race_id": self.race_id,
            "position": self.position,
            "race_entry_id": self.race_entry_id,
            "gate_number": self.gate_number,
        }


@dataclass(frozen=True, slots=True)
class Win5SubmissionAuditSnapshot:
    """Canonical Submission state serialized into before/after JSON."""

    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    version: int
    picks: tuple[Win5SubmissionPickAuditSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.persona_id, str) or not self.persona_id:
            raise ValueError("persona_id must be a non-empty string.")
        _require_positive_int(self.version, field_name="version")
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        object.__setattr__(self, "status", Win5SubmissionStatus(self.status))
        object.__setattr__(self, "picks", tuple(self.picks))

    def to_payload(self) -> dict[str, object]:
        """Return the deterministic audit snapshot v1 payload."""

        ordered_picks = sorted(self.picks, key=lambda pick: (pick.race_id, pick.position))
        return {
            "schema_version": WIN5_SUBMISSION_AUDIT_SCHEMA_VERSION,
            "persona_id": self.persona_id,
            "tier": self.tier.value,
            "status": self.status.value,
            "version": self.version,
            "picks": [pick.to_payload() for pick in ordered_picks],
        }

    def _canonical_state(self) -> tuple[object, ...]:
        return (
            self.persona_id,
            self.tier,
            self.status,
            tuple(sorted(self.picks, key=lambda pick: (pick.race_id, pick.position))),
        )

    def _canonical_content(self) -> tuple[object, ...]:
        return (
            self.persona_id,
            self.tier,
            tuple(sorted(self.picks, key=lambda pick: (pick.race_id, pick.position))),
        )


@dataclass(frozen=True, slots=True)
class Win5SubmissionAuditRecord:
    """One state-changing Submission audit prepared for persistence."""

    context: Win5SubmissionAuditContext
    type: Win5SubmissionAuditType
    before: Win5SubmissionAuditSnapshot | None
    after: Win5SubmissionAuditSnapshot

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", Win5SubmissionAuditType(self.type))

        if self.before is None:
            if self.type != Win5SubmissionAuditType.SAVED or self.after.version != 1:
                raise ValueError("Only an initial version-1 save may omit the before snapshot.")
        else:
            if self.after.version != self.before.version + 1:
                raise ValueError("A state-changing command must increment version exactly once.")
            if self.after.persona_id != self.before.persona_id:
                raise ValueError("A Submission audit must preserve persona ownership.")
            if self.before._canonical_state() == self.after._canonical_state():
                raise ValueError("An exact no-op must not create a canonical audit record.")

        if self.type == Win5SubmissionAuditType.SAVED:
            if self.after.status != Win5SubmissionStatus.ACCEPTED:
                raise ValueError("submission_saved must produce an accepted Submission.")
            if self.before is not None and self.before.status != Win5SubmissionStatus.ACCEPTED:
                raise ValueError("submission_saved must not reactivate a cancelled Submission.")
        elif (
            self.before is None
            or self.before.status != Win5SubmissionStatus.ACCEPTED
            or self.after.status != Win5SubmissionStatus.CANCELLED
        ):
            raise ValueError("submission_cancelled must transition accepted to cancelled.")
        elif self.before._canonical_content() != self.after._canonical_content():
            raise ValueError("submission_cancelled must preserve the accepted Submission content.")

    @property
    def before_data(self) -> dict[str, object] | None:
        """JSON data for ``win5_operations.before_data``."""

        return None if self.before is None else self.before.to_payload()

    @property
    def after_data(self) -> dict[str, object]:
        """JSON data for ``win5_operations.after_data``."""

        return self.after.to_payload()
