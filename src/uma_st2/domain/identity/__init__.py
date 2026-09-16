"""Identity domain primitives and invariants."""

from .enums import GameRegion, PersonaStatus, RegistrationRequestStatus
from .errors import (
    DiscordAccountInvariantError,
    GameAccountInvariantError,
    IdentityDomainError,
    PersonaInvariantError,
    RegistrationRequestInvariantError,
)
from .models import (
    DiscordAccount,
    GameAccount,
    GameAccountRegistrationRequest,
    Persona,
    normalize_region,
)
from .policies import MEMBER_MUTATION_PERSONA_STATUSES, allows_member_mutation
from .registration import UMA_PID_MAX_LENGTH, normalize_registration_pid

__all__ = [
    "DiscordAccount",
    "DiscordAccountInvariantError",
    "GameAccount",
    "GameAccountInvariantError",
    "GameAccountRegistrationRequest",
    "GameRegion",
    "IdentityDomainError",
    "MEMBER_MUTATION_PERSONA_STATUSES",
    "Persona",
    "PersonaInvariantError",
    "PersonaStatus",
    "RegistrationRequestInvariantError",
    "RegistrationRequestStatus",
    "UMA_PID_MAX_LENGTH",
    "allows_member_mutation",
    "normalize_registration_pid",
    "normalize_region",
]
