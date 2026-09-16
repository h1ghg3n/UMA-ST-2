"""Read-only private Account/Persona status projections."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.application.exporting.match import match_export_season_for_datetime
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus
from uma_st2.domain.match import MatchSourceKind, MatchStatus
from uma_st2.domain.win5 import Win5RoundStatus, Win5RoundType, Win5SeasonStatus, Win5SubmissionStatus
from uma_st2.shared import normalize_utc_datetime

ACCOUNT_STATUS_PAGE_SIZE: Final = 5


class AccountStatusQueryError(ValueError):
    """Base error for rejected Account status reads."""


class AccountStatusInvalidSourceError(AccountStatusQueryError):
    """Stored Account status facts cannot form a safe projection."""


class AccountEligibilityState(StrEnum):
    """Presentation-safe reason for current member mutation eligibility."""

    ELIGIBLE = "eligible"
    REGISTRATION_PENDING = "registration_pending"
    APPROVAL_PENDING = "approval_pending"
    GAME_ACCOUNT_REQUIRED = "game_account_required"
    WALLET_REQUIRED = "wallet_required"
    RESTRICTED = "restricted"
    UNREGISTERED = "unregistered"


def _text(value: object, *, field_name: str, max_length: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text.")
    normalized = value.strip()
    if optional and not normalized:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1 to {max_length} characters.")
    return normalized


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _optional_decimal(value: object, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal or null.")
    return value


@dataclass(frozen=True, slots=True)
class AccountRegistrationRequestSummary:
    """Latest onboarding request visible only to its Discord requester."""

    request_id: int
    game_region: GameRegion
    pid_hint: str
    nickname: str
    status: RegistrationRequestStatus
    reason: str | None
    created_at: datetime
    resolved_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _positive_int(self.request_id, field_name="request_id"))
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "pid_hint", _text(self.pid_hint, field_name="pid_hint", max_length=16))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(self, "status", RegistrationRequestStatus(self.status))
        object.__setattr__(self, "reason", _text(self.reason, field_name="reason", max_length=255, optional=True))
        object.__setattr__(self, "created_at", normalize_utc_datetime(self.created_at, field_name="created_at"))
        if self.resolved_at is not None:
            object.__setattr__(
                self,
                "resolved_at",
                normalize_utc_datetime(self.resolved_at, field_name="resolved_at"),
            )


@dataclass(frozen=True, slots=True)
class AccountMatchSummary:
    """Official Persona activity for one derived KST half-year."""

    season_key: str
    season_name: str
    participated_match_count: int
    entry_count: int
    win_count: int
    top3_count: int
    average_rank: Decimal | None
    excluded_terminal_match_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "season_key", _text(self.season_key, field_name="season_key", max_length=16))
        object.__setattr__(self, "season_name", _text(self.season_name, field_name="season_name", max_length=32))
        for field_name in (
            "participated_match_count",
            "entry_count",
            "win_count",
            "top3_count",
            "excluded_terminal_match_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _non_negative_int(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "average_rank",
            _optional_decimal(self.average_rank, field_name="average_rank"),
        )
        if self.win_count > self.top3_count or self.top3_count > self.entry_count:
            raise ValueError("Match summary win/top3 counts cannot exceed the official Entry count.")


@dataclass(frozen=True, slots=True)
class AccountWin5Summary:
    """Persona-owned score summary for active or latest closed WIN5 Season."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    season_score: int
    competition_rank: int | None
    submitted_round_count: int
    scored_round_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "season_id", _positive_int(self.season_id, field_name="season_id"))
        object.__setattr__(self, "season_name", _text(self.season_name, field_name="season_name", max_length=100))
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        if isinstance(self.season_score, bool) or not isinstance(self.season_score, int):
            raise ValueError("season_score must be an integer.")
        if self.competition_rank is not None:
            object.__setattr__(
                self,
                "competition_rank",
                _positive_int(self.competition_rank, field_name="competition_rank"),
            )
        object.__setattr__(
            self,
            "submitted_round_count",
            _non_negative_int(self.submitted_round_count, field_name="submitted_round_count"),
        )
        object.__setattr__(
            self,
            "scored_round_count",
            _non_negative_int(self.scored_round_count, field_name="scored_round_count"),
        )


@dataclass(frozen=True, slots=True)
class AccountStatusOverview:
    """Private Account status landing projection."""

    persona_id: str | None
    display_name: str | None
    persona_status: PersonaStatus | None
    wallet_balance: int | None
    game_account_count: int
    eligible_game_account_count: int
    registration_request: AccountRegistrationRequestSummary | None
    match: AccountMatchSummary
    win5: AccountWin5Summary | None

    def __post_init__(self) -> None:
        persona_id = _text(self.persona_id, field_name="persona_id", max_length=36, optional=True)
        display_name = _text(self.display_name, field_name="display_name", max_length=100, optional=True)
        if (persona_id is None) != (display_name is None) or (persona_id is None) != (self.persona_status is None):
            raise ValueError("Persona identity fields must be present together.")
        object.__setattr__(self, "persona_id", persona_id)
        object.__setattr__(self, "display_name", display_name)
        if self.persona_status is not None:
            object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        if self.wallet_balance is not None and (
            isinstance(self.wallet_balance, bool) or not isinstance(self.wallet_balance, int)
        ):
            raise ValueError("wallet_balance must be an integer or null.")
        object.__setattr__(
            self,
            "game_account_count",
            _non_negative_int(self.game_account_count, field_name="game_account_count"),
        )
        object.__setattr__(
            self,
            "eligible_game_account_count",
            _non_negative_int(self.eligible_game_account_count, field_name="eligible_game_account_count"),
        )
        if self.eligible_game_account_count > self.game_account_count:
            raise ValueError("Eligible GameAccount count cannot exceed the total count.")
        if not isinstance(self.match, AccountMatchSummary):
            raise ValueError("match must be an AccountMatchSummary.")
        if self.win5 is not None and not isinstance(self.win5, AccountWin5Summary):
            raise ValueError("win5 must be an AccountWin5Summary or null.")

    @property
    def eligibility(self) -> AccountEligibilityState:
        """Derive current new-member-mutation eligibility without persistence work."""

        if self.persona_id is None:
            if (
                self.registration_request is not None
                and self.registration_request.status is RegistrationRequestStatus.PENDING
            ):
                return AccountEligibilityState.REGISTRATION_PENDING
            return AccountEligibilityState.UNREGISTERED
        if self.persona_status is PersonaStatus.PENDING_APPROVAL:
            return AccountEligibilityState.APPROVAL_PENDING
        if self.persona_status in {PersonaStatus.EXPELLED, PersonaStatus.WITHDRAWN}:
            return AccountEligibilityState.RESTRICTED
        if self.wallet_balance is None:
            return AccountEligibilityState.WALLET_REQUIRED
        if self.eligible_game_account_count == 0:
            return AccountEligibilityState.GAME_ACCOUNT_REQUIRED
        return AccountEligibilityState.ELIGIBLE


@dataclass(frozen=True, slots=True)
class AccountMatchHistoryItem:
    """One immutable owner-at-event Match Entry shown in private recent history."""

    match_id: int
    match_name: str
    scheduled_at: datetime
    status: MatchStatus
    source_kind: MatchSourceKind
    game_account_id: int
    game_account_nickname: str
    character_name: str
    entry_number: int
    rank: int | None
    field_size: int
    rating_delta: Decimal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "match_id", _positive_int(self.match_id, field_name="match_id"))
        object.__setattr__(self, "match_name", _text(self.match_name, field_name="match_name", max_length=200))
        object.__setattr__(
            self,
            "scheduled_at",
            normalize_utc_datetime(self.scheduled_at, field_name="scheduled_at"),
        )
        object.__setattr__(self, "status", MatchStatus(self.status))
        object.__setattr__(self, "source_kind", MatchSourceKind(self.source_kind))
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(
            self,
            "game_account_nickname",
            _text(self.game_account_nickname, field_name="game_account_nickname", max_length=100),
        )
        object.__setattr__(
            self,
            "character_name",
            _text(self.character_name, field_name="character_name", max_length=100),
        )
        object.__setattr__(self, "entry_number", _positive_int(self.entry_number, field_name="entry_number"))
        if self.rank is not None:
            object.__setattr__(self, "rank", _positive_int(self.rank, field_name="rank"))
        object.__setattr__(self, "field_size", _positive_int(self.field_size, field_name="field_size"))
        object.__setattr__(
            self,
            "rating_delta",
            _optional_decimal(self.rating_delta, field_name="rating_delta"),
        )


@dataclass(frozen=True, slots=True)
class AccountMatchHistoryPage:
    """One bounded recent Match page."""

    season_key: str
    season_name: str
    page: int
    total_count: int
    items: tuple[AccountMatchHistoryItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "season_key", _text(self.season_key, field_name="season_key", max_length=16))
        object.__setattr__(self, "season_name", _text(self.season_name, field_name="season_name", max_length=32))
        object.__setattr__(self, "page", _non_negative_int(self.page, field_name="page"))
        object.__setattr__(self, "total_count", _non_negative_int(self.total_count, field_name="total_count"))
        object.__setattr__(self, "items", tuple(self.items))
        if len(self.items) > ACCOUNT_STATUS_PAGE_SIZE or any(
            not isinstance(item, AccountMatchHistoryItem) for item in self.items
        ):
            raise ValueError("Match history page contains invalid or excessive items.")
        if self.page * ACCOUNT_STATUS_PAGE_SIZE >= self.total_count and self.page != 0 and self.items:
            raise ValueError("Match history page offset exceeds its total count.")

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * ACCOUNT_STATUS_PAGE_SIZE < self.total_count


@dataclass(frozen=True, slots=True)
class AccountWin5HistoryItem:
    """One latest Persona-owned Submission state for a WIN5 Round."""

    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    submission_status: Win5SubmissionStatus
    score_delta: int | None
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "round_id", _positive_int(self.round_id, field_name="round_id"))
        object.__setattr__(self, "round_name", _text(self.round_name, field_name="round_name", max_length=100))
        object.__setattr__(self, "round_type", Win5RoundType(self.round_type))
        object.__setattr__(self, "round_status", Win5RoundStatus(self.round_status))
        object.__setattr__(self, "submission_status", Win5SubmissionStatus(self.submission_status))
        if self.score_delta is not None and (
            isinstance(self.score_delta, bool) or not isinstance(self.score_delta, int)
        ):
            raise ValueError("score_delta must be an integer or null.")
        object.__setattr__(
            self,
            "updated_at",
            normalize_utc_datetime(self.updated_at, field_name="updated_at"),
        )


@dataclass(frozen=True, slots=True)
class AccountWin5HistoryPage:
    """One bounded recent WIN5 page for the selected Season."""

    season: AccountWin5Summary | None
    page: int
    total_count: int
    items: tuple[AccountWin5HistoryItem, ...]

    def __post_init__(self) -> None:
        if self.season is not None and not isinstance(self.season, AccountWin5Summary):
            raise ValueError("season must be an AccountWin5Summary or null.")
        object.__setattr__(self, "page", _non_negative_int(self.page, field_name="page"))
        object.__setattr__(self, "total_count", _non_negative_int(self.total_count, field_name="total_count"))
        object.__setattr__(self, "items", tuple(self.items))
        if len(self.items) > ACCOUNT_STATUS_PAGE_SIZE or any(
            not isinstance(item, AccountWin5HistoryItem) for item in self.items
        ):
            raise ValueError("WIN5 history page contains invalid or excessive items.")

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * ACCOUNT_STATUS_PAGE_SIZE < self.total_count


@dataclass(frozen=True, slots=True)
class AccountGameAccountSummary:
    """One private peer GameAccount row with masked identity and own Rating."""

    game_account_id: int
    game_region: GameRegion
    pid_hint: str | None
    nickname: str
    affiliation: str | None
    current_rating: Decimal | None
    competition_rank: int | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        object.__setattr__(self, "game_region", GameRegion(self.game_region))
        object.__setattr__(self, "pid_hint", _text(self.pid_hint, field_name="pid_hint", max_length=16, optional=True))
        object.__setattr__(self, "nickname", _text(self.nickname, field_name="nickname", max_length=100))
        object.__setattr__(
            self,
            "affiliation",
            _text(self.affiliation, field_name="affiliation", max_length=100, optional=True),
        )
        object.__setattr__(
            self,
            "current_rating",
            _optional_decimal(self.current_rating, field_name="current_rating"),
        )
        if self.competition_rank is not None:
            object.__setattr__(
                self,
                "competition_rank",
                _positive_int(self.competition_rank, field_name="competition_rank"),
            )
        if (self.current_rating is None) != (self.competition_rank is None):
            raise ValueError("Rating and global rank must be present together.")


@dataclass(frozen=True, slots=True)
class AccountIdentityDetails:
    """Private identity tab projection."""

    persona_id: str | None
    display_name: str | None
    persona_status: PersonaStatus | None
    page: int
    total_count: int
    accounts: tuple[AccountGameAccountSummary, ...]
    registration_request: AccountRegistrationRequestSummary | None

    def __post_init__(self) -> None:
        persona_id = _text(self.persona_id, field_name="persona_id", max_length=36, optional=True)
        display_name = _text(self.display_name, field_name="display_name", max_length=100, optional=True)
        if (persona_id is None) != (display_name is None) or (persona_id is None) != (self.persona_status is None):
            raise ValueError("Persona identity fields must be present together.")
        object.__setattr__(self, "persona_id", persona_id)
        object.__setattr__(self, "display_name", display_name)
        if self.persona_status is not None:
            object.__setattr__(self, "persona_status", PersonaStatus(self.persona_status))
        object.__setattr__(self, "page", _non_negative_int(self.page, field_name="page"))
        object.__setattr__(self, "total_count", _non_negative_int(self.total_count, field_name="total_count"))
        object.__setattr__(self, "accounts", tuple(self.accounts))
        if len(self.accounts) > ACCOUNT_STATUS_PAGE_SIZE or any(
            not isinstance(account, AccountGameAccountSummary) for account in self.accounts
        ):
            raise ValueError("Identity page contains invalid or excessive GameAccount rows.")

    @property
    def has_previous(self) -> bool:
        return self.page > 0

    @property
    def has_next(self) -> bool:
        return (self.page + 1) * ACCOUNT_STATUS_PAGE_SIZE < self.total_count


class AccountStatusQueryRepository(Protocol):
    """Persistence projections for one Discord actor's private Account status."""

    def get_overview(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        match_season_key: str,
        match_season_name: str,
        match_starts_at: datetime,
        match_ends_at: datetime,
    ) -> AccountStatusOverview: ...

    def get_match_history(
        self,
        *,
        discord_user_id: str,
        match_season_key: str,
        match_season_name: str,
        match_starts_at: datetime,
        match_ends_at: datetime,
        page: int,
        offset: int,
        limit: int,
    ) -> AccountMatchHistoryPage: ...

    def get_win5_history(
        self,
        *,
        discord_user_id: str,
        page: int,
        offset: int,
        limit: int,
    ) -> AccountWin5HistoryPage: ...

    def get_identity_details(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        page: int,
        offset: int,
        limit: int,
    ) -> AccountIdentityDetails: ...


class AccountStatusQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing the Account status projection."""

    @property
    def account_status_queries(self) -> AccountStatusQueryRepository: ...


@dataclass(frozen=True, slots=True)
class AccountStatusQueries:
    """Application entry point for private Account status tabs."""

    query_runner: QueryRunner[AccountStatusQueryUnitOfWork]
    clock: Callable[[], datetime]

    def get_overview(self, *, discord_user_id: str, guild_id: str) -> AccountStatusOverview:
        actor, guild = self._validated_actor(discord_user_id, guild_id)
        season = match_export_season_for_datetime(self.clock())
        return self._run(
            lambda repository: repository.get_overview(
                discord_user_id=actor,
                guild_id=guild,
                match_season_key=season.key,
                match_season_name=season.name,
                match_starts_at=season.starts_at,
                match_ends_at=season.ends_at,
            ),
            AccountStatusOverview,
        )

    def get_match_history(self, *, discord_user_id: str, page: int = 0) -> AccountMatchHistoryPage:
        actor = self._validated_id(discord_user_id, field_name="discord_user_id")
        page = self._validated_page(page)
        season = match_export_season_for_datetime(self.clock())
        return self._run(
            lambda repository: repository.get_match_history(
                discord_user_id=actor,
                match_season_key=season.key,
                match_season_name=season.name,
                match_starts_at=season.starts_at,
                match_ends_at=season.ends_at,
                page=page,
                offset=page * ACCOUNT_STATUS_PAGE_SIZE,
                limit=ACCOUNT_STATUS_PAGE_SIZE,
            ),
            AccountMatchHistoryPage,
        )

    def get_win5_history(self, *, discord_user_id: str, page: int = 0) -> AccountWin5HistoryPage:
        actor = self._validated_id(discord_user_id, field_name="discord_user_id")
        page = self._validated_page(page)
        return self._run(
            lambda repository: repository.get_win5_history(
                discord_user_id=actor,
                page=page,
                offset=page * ACCOUNT_STATUS_PAGE_SIZE,
                limit=ACCOUNT_STATUS_PAGE_SIZE,
            ),
            AccountWin5HistoryPage,
        )

    def get_identity_details(
        self,
        *,
        discord_user_id: str,
        guild_id: str,
        page: int = 0,
    ) -> AccountIdentityDetails:
        actor, guild = self._validated_actor(discord_user_id, guild_id)
        page = self._validated_page(page)
        return self._run(
            lambda repository: repository.get_identity_details(
                discord_user_id=actor,
                guild_id=guild,
                page=page,
                offset=page * ACCOUNT_STATUS_PAGE_SIZE,
                limit=ACCOUNT_STATUS_PAGE_SIZE,
            ),
            AccountIdentityDetails,
        )

    def _run(self, operation: Callable[[AccountStatusQueryRepository], object], result_type: type[object]):
        def query(unit_of_work: AccountStatusQueryUnitOfWork):
            try:
                result = operation(unit_of_work.account_status_queries)
                if not isinstance(result, result_type):
                    raise TypeError("Account status repository returned an invalid projection type.")
                return result
            except AccountStatusQueryError:
                raise
            except (TypeError, ValueError) as error:
                raise AccountStatusInvalidSourceError("Stored Account status facts are malformed.") from error

        return self.query_runner.run(query)

    @classmethod
    def _validated_actor(cls, discord_user_id: str, guild_id: str) -> tuple[str, str]:
        return (
            cls._validated_id(discord_user_id, field_name="discord_user_id"),
            cls._validated_id(guild_id, field_name="guild_id"),
        )

    @staticmethod
    def _validated_id(value: str, *, field_name: str) -> str:
        normalized = _text(value, field_name=field_name, max_length=32)
        assert normalized is not None
        return normalized

    @staticmethod
    def _validated_page(page: int) -> int:
        return _non_negative_int(page, field_name="page")
