class DomainError(Exception):
    """Base error for domain rule violations."""


class InvalidUmaPidError(DomainError, ValueError):
    """Raised when an Uma Musume player ID is not acceptable."""


class DuplicateUmaPidError(DomainError, ValueError):
    """Raised when an Uma Musume player ID is already registered."""


class AccountAlreadyRegisteredError(DomainError, ValueError):
    """Raised when a Discord user attempts to register an existing account again."""


class AccountRegistrationRequestError(DomainError, ValueError):
    """Raised when an account registration request is invalid or cannot change state."""

    code = "ACCOUNT_REGISTRATION_REQUEST_INVALID"


class AccountRegistrationRequestConflictError(AccountRegistrationRequestError):
    """Raised when a registration request conflicts with persisted state or idempotency."""

    code = "ACCOUNT_REGISTRATION_REQUEST_CONFLICT"


class IdentityStateError(DomainError, ValueError):
    """Raised when a game account identity transition is not allowed."""


class PlayerLinkRequestError(DomainError, ValueError):
    """Raised when a player-link request is invalid or cannot change state."""

    code = "PLAYER_LINK_REQUEST_INVALID"


class PlayerLinkRequestConflictError(PlayerLinkRequestError):
    """Raised when a player-link request conflicts with persisted state or idempotency."""

    code = "PLAYER_LINK_REQUEST_CONFLICT"


class LegacyImportError(DomainError, ValueError):
    """Raised when legacy source data cannot be imported safely."""


class LegacyImportConflictError(LegacyImportError):
    """Raised when an import would overwrite or duplicate existing data."""


class AccountNotFoundError(DomainError, LookupError):
    """Raised when a requested Discord or game account does not exist."""


class AccountOwnershipError(DomainError, PermissionError):
    """Raised when a game account does not belong to the Discord account."""


class PersonaLinkConsoleError(DomainError, ValueError):
    """Raised when a Persona link-console operation is invalid."""

    code = "PERSONA_LINK_CONSOLE_INVALID"


class PersonaLinkConsoleConflictError(PersonaLinkConsoleError):
    """Raised when current Persona linkage or an operation ID conflicts."""

    code = "PERSONA_LINK_CONSOLE_CONFLICT"


class BettingRuleError(DomainError, ValueError):
    """Raised when a bet violates betting rules."""


class InsufficientCirclePointsError(BettingRuleError):
    """Raised when a room point account cannot cover a bet stake."""


class BetJudgementError(BettingRuleError):
    """Raised when room-match results cannot be judged consistently."""


class BetSettlementError(BettingRuleError):
    """Raised when room-match points cannot be settled consistently."""


class MatchRaceError(DomainError, ValueError):
    """Raised when a room-match control-plane request is invalid."""

    code = "RACE_INVALID_REQUEST"


class InvalidRaceTransitionError(MatchRaceError):
    """Raised when a room-match lifecycle signal is not currently allowed."""

    code = "RACE_INVALID_TRANSITION"


class RaceMutationConflictError(MatchRaceError):
    """Raised when persisted race state prevents a requested mutation."""

    code = "RACE_MUTATION_CONFLICT"


class MatchResultError(MatchRaceError):
    """Raised when a native room-match result request is invalid."""

    code = "ROOM_RESULT_INVALID_REQUEST"


class MatchResultConflictError(MatchResultError):
    """Raised when persisted result state or an idempotency key conflicts."""

    code = "ROOM_RESULT_MUTATION_CONFLICT"


class MatchPlacementRewardError(MatchRaceError):
    """Raised when a Circle Match placement reward cannot be calculated."""

    code = "ROOM_PLACEMENT_REWARD_INVALID"


class MatchRatingError(MatchRaceError):
    """Raised when a Room Match Rating calculation cannot be reproduced safely."""

    code = "ROOM_RATING_INVALID_REQUEST"


class MatchRatingConflictError(MatchRatingError):
    """Raised when a Rating event already exists or source state has changed."""

    code = "ROOM_RATING_MUTATION_CONFLICT"


class MatchOddsSnapshotError(MatchRaceError):
    """Raised when Room Match settlement odds cannot be snapshotted safely."""

    code = "ROOM_ODDS_SNAPSHOT_INVALID_REQUEST"


class MatchOddsSnapshotConflictError(MatchOddsSnapshotError):
    """Raised when immutable Room Match odds state conflicts with a request."""

    code = "ROOM_ODDS_SNAPSHOT_CONFLICT"


class MatchSettlementConflictError(BetSettlementError):
    """Raised when final Room Match settlement conflicts with persisted state."""

    code = "ROOM_SETTLEMENT_CONFLICT"


class GuildDiscordSettingsError(DomainError, ValueError):
    """Raised when persisted per-guild Discord settings are invalid."""

    code = "GUILD_DISCORD_SETTINGS_INVALID"


class GuildDiscordSettingsConflictError(GuildDiscordSettingsError):
    """Raised when settings state or an idempotency key conflicts."""

    code = "GUILD_DISCORD_SETTINGS_CONFLICT"


class DiscordPublicationError(DomainError, ValueError):
    """Raised when a durable Discord delivery request is invalid."""

    code = "DISCORD_PUBLICATION_INVALID"


class DiscordPublicationConflictError(DiscordPublicationError):
    """Raised when a durable Discord delivery state conflicts."""

    code = "DISCORD_PUBLICATION_CONFLICT"


class Win5RuleError(DomainError, ValueError):
    """Raised when a WIN5 prediction or result violates domain rules."""


class Win5SubmissionError(Win5RuleError):
    """Raised when a WIN5 prediction cannot be submitted consistently."""


class Win5LifecycleError(Win5RuleError):
    """Raised when a WIN5 season or round transition is not allowed."""


class Win5MutationConflictError(Win5RuleError):
    """Raised when an idempotency key or persisted WIN5 state conflicts."""


class CurrentWin5ReplayError(DomainError, ValueError):
    """Raised when the one-time current WIN5 epoch cannot be replayed safely."""


class CurrentWin5ReplayConflictError(CurrentWin5ReplayError):
    """Raised when persisted state conflicts with the reviewed replay manifest."""


class ImportApplicationError(DomainError, ValueError):
    """Raised when a normalized source row cannot be applied safely."""


class ImportRunStateError(ImportApplicationError):
    """Raised when an import run is missing or is not accepting rows."""


class ImportFingerprintConflictError(ImportApplicationError):
    """Raised when a previously seen source row has changed content."""
