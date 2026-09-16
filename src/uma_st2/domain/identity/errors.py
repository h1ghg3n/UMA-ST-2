"""Domain errors for identity invariants."""


class IdentityDomainError(ValueError):
    """Raised when an identity invariant is violated."""


class PersonaInvariantError(IdentityDomainError):
    """Raised when Persona domain rules are violated."""


class DiscordAccountInvariantError(IdentityDomainError):
    """Raised when DiscordAccount domain rules are violated."""


class GameAccountInvariantError(IdentityDomainError):
    """Raised when GameAccount domain rules are violated."""


class RegistrationRequestInvariantError(IdentityDomainError):
    """Raised when registration request domain rules are violated."""
