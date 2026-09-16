"""Domain errors for betting calculation kernel."""


class BettingDomainError(ValueError):
    """Raised when betting domain input violates deterministic business rules."""


class BettingSelectionError(BettingDomainError):
    """Raised when bet selections are invalid."""


class BettingStakeError(BettingDomainError):
    """Raised when stake validation fails."""


class BettingOddsError(BettingDomainError):
    """Raised when an odds pool or applied odds value is invalid."""
