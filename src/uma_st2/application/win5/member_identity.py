"""Shared member identity projection for WIN5 application use cases."""

from __future__ import annotations

from dataclasses import dataclass

from uma_st2.domain.identity import PersonaStatus, allows_member_mutation


@dataclass(frozen=True, slots=True)
class Win5MemberPersona:
    """Current Persona and registered-account eligibility resolved from Discord."""

    id: str
    status: PersonaStatus
    has_eligible_game_account: bool

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("id must be a non-empty string.")
        object.__setattr__(self, "status", PersonaStatus(self.status))
        if not isinstance(self.has_eligible_game_account, bool):
            raise ValueError("has_eligible_game_account must be a boolean.")

    @property
    def is_active(self) -> bool:
        """Interpret the tracked new-member-mutation Persona statuses."""

        return allows_member_mutation(self.status)

    @property
    def is_eligible(self) -> bool:
        """Require an active Persona with at least one non-NULL PID account."""

        return self.is_active and self.has_eligible_game_account

    @property
    def can_read_member_history(self) -> bool:
        """Allow pending approval to inspect accepted history without mutating it."""

        if self.status is PersonaStatus.PENDING_APPROVAL:
            return True
        return self.is_active and self.has_eligible_game_account
