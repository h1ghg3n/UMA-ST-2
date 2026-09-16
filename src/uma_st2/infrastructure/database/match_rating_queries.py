"""SQLAlchemy current Room Match Rating standings projection."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from uma_st2.application.rating import MatchRatingStanding

from .orm import GameAccountORM, PersonaORM, RatingORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchRatingQueryRepository:
    """Materialize global competition ranks before optional result filtering."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_standings(
        self,
        *,
        rank_start: int | None,
        rank_end: int | None,
        persona_query: str | None,
        limit: int,
    ) -> tuple[MatchRatingStanding, ...]:
        ranked = select(
            RatingORM.game_account_id.label("game_account_id"),
            RatingORM.rating.label("current_rating"),
            func.rank().over(order_by=RatingORM.rating.desc()).label("competition_rank"),
        ).subquery("ranked_ratings")
        statement = (
            select(
                ranked.c.game_account_id,
                GameAccountORM.persona_id.label("current_owner_persona_id"),
                PersonaORM.display_name.label("current_owner_display_name"),
                ranked.c.current_rating,
                ranked.c.competition_rank,
            )
            .join(GameAccountORM, GameAccountORM.id == ranked.c.game_account_id)
            .outerjoin(PersonaORM, PersonaORM.id == GameAccountORM.persona_id)
        )
        if rank_start is not None:
            statement = statement.where(ranked.c.competition_rank >= rank_start)
        if rank_end is not None:
            statement = statement.where(ranked.c.competition_rank <= rank_end)
        if persona_query is not None:
            statement = statement.where(PersonaORM.display_name.contains(persona_query, autoescape=True))

        rows = self._session.execute(
            statement.order_by(ranked.c.competition_rank, ranked.c.game_account_id).limit(limit)
        )
        return tuple(
            MatchRatingStanding(
                game_account_id=row.game_account_id,
                current_owner_persona_id=row.current_owner_persona_id,
                current_owner_display_name=row.current_owner_display_name,
                current_rating=row.current_rating,
                competition_rank=row.competition_rank,
            )
            for row in rows
        )


class SqlAlchemyMatchRatingQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for current Rating standings."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchRatingQueryRepository | None = None

    @property
    def match_rating_queries(self) -> SqlAlchemyMatchRatingQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchRatingQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchRatingQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchRatingQueryUnitOfWork]
):
    """Create one Rating standings query UoW per read."""

    unit_of_work_type = SqlAlchemyMatchRatingQueryUnitOfWork
