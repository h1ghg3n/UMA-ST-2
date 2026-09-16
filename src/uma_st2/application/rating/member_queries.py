"""Read-only current Room Match Rating standings."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final, Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.rating import RatingCalculationError, normalize_rating_storage

DEFAULT_RATING_RANK_END: Final = 50
RATING_RANK_WINDOW: Final = 10
MAX_RATING_STANDING_ROWS: Final = 250


class MatchRatingQueryError(ValueError):
    """Base error for rejected current Rating standing queries."""


class MatchRatingFilterConflictError(MatchRatingQueryError):
    """Rank and Persona filters were supplied together."""


class MatchRatingResultTooLargeError(MatchRatingQueryError):
    """A complete result cannot be delivered within the bounded read contract."""


class MatchRatingInvalidSourceError(MatchRatingQueryError):
    """Stored Rating standing facts are malformed."""


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MatchRatingQueryError(f"{field_name} must be a positive integer.")
    return value


def _optional_text(
    value: object,
    *,
    field_name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MatchRatingQueryError(f"{field_name} must be text or null.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise MatchRatingQueryError(f"{field_name} must contain 1 to {max_length} characters.")
    return normalized


@dataclass(frozen=True, slots=True)
class MatchRatingStanding:
    """One GameAccount-scoped current Rating row with preserved global rank."""

    game_account_id: int
    current_owner_persona_id: str | None
    current_owner_display_name: str | None
    current_rating: Decimal
    competition_rank: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "game_account_id",
            _positive_int(self.game_account_id, field_name="game_account_id"),
        )
        owner_id = _optional_text(
            self.current_owner_persona_id,
            field_name="current_owner_persona_id",
            max_length=36,
        )
        owner_name = _optional_text(
            self.current_owner_display_name,
            field_name="current_owner_display_name",
            max_length=100,
        )
        if (owner_id is None) != (owner_name is None):
            raise MatchRatingQueryError("Current owner Persona ID and display name must be present together.")
        object.__setattr__(self, "current_owner_persona_id", owner_id)
        object.__setattr__(self, "current_owner_display_name", owner_name)
        try:
            with localcontext() as context:
                context.prec = 60
                rating = normalize_rating_storage(self.current_rating)
        except (RatingCalculationError, TypeError) as error:
            raise MatchRatingQueryError("current_rating must be a finite NUMERIC(30,18) Decimal.") from error
        if rating < 0:
            raise MatchRatingQueryError("current_rating cannot be negative.")
        if rating != self.current_rating:
            raise MatchRatingQueryError("current_rating must already use NUMERIC(30,18) precision.")
        object.__setattr__(self, "current_rating", rating)
        object.__setattr__(
            self,
            "competition_rank",
            _positive_int(self.competition_rank, field_name="competition_rank"),
        )


class MatchRatingQueryRepository(Protocol):
    """Persistence projection for globally ranked current Rating rows."""

    def list_standings(
        self,
        *,
        rank_start: int | None,
        rank_end: int | None,
        persona_query: str | None,
        limit: int,
    ) -> tuple[MatchRatingStanding, ...]: ...


class MatchRatingQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing the Rating standings projection."""

    @property
    def match_rating_queries(self) -> MatchRatingQueryRepository: ...


@dataclass(frozen=True, slots=True)
class MatchRatingQueries:
    """Application entry point for bounded current Rating standings."""

    query_runner: QueryRunner[MatchRatingQueryUnitOfWork]

    def list_ratings(
        self,
        *,
        rank: int | None = None,
        persona: str | None = None,
    ) -> tuple[MatchRatingStanding, ...]:
        """Return global Rating ranks, optionally narrowed by one filter."""

        if rank is not None and persona is not None:
            raise MatchRatingFilterConflictError("rank and persona filters are mutually exclusive.")

        if rank is not None:
            rank_start = _positive_int(rank, field_name="rank")
            rank_end = rank_start + RATING_RANK_WINDOW - 1
            persona_query = None
        elif persona is not None:
            rank_start = None
            rank_end = None
            persona_query = _optional_text(persona, field_name="persona", max_length=100)
        else:
            rank_start = 1
            rank_end = DEFAULT_RATING_RANK_END
            persona_query = None

        def query(unit_of_work: MatchRatingQueryUnitOfWork) -> tuple[MatchRatingStanding, ...]:
            try:
                rows = unit_of_work.match_rating_queries.list_standings(
                    rank_start=rank_start,
                    rank_end=rank_end,
                    persona_query=persona_query,
                    limit=MAX_RATING_STANDING_ROWS + 1,
                )
                if not isinstance(rows, tuple) or any(not isinstance(row, MatchRatingStanding) for row in rows):
                    raise ValueError("Rating repository returned an invalid projection.")
            except (TypeError, ValueError) as error:
                raise MatchRatingInvalidSourceError("Stored Match Rating standings are malformed.") from error
            if len(rows) > MAX_RATING_STANDING_ROWS:
                raise MatchRatingResultTooLargeError("Rating standings exceed the complete delivery capacity.")
            return rows

        return self.query_runner.run(query)
