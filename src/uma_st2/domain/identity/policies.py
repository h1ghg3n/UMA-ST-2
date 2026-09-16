"""Shared Persona status policy predicates."""

from __future__ import annotations

from typing import Final

from .enums import PersonaStatus

MEMBER_MUTATION_PERSONA_STATUSES: Final = frozenset(
    {
        PersonaStatus.NORMAL,
        PersonaStatus.WARNING,
    }
)


def allows_member_mutation(status: PersonaStatus | str) -> bool:
    """Return whether current Persona status permits a new member-owned mutation."""

    return PersonaStatus(status) in MEMBER_MUTATION_PERSONA_STATUSES
