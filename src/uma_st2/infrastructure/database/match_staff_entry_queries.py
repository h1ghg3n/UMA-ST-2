"""SQLAlchemy staff query projections for Match Entry replacement."""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from uma_st2.application.match.entries import (
    MatchEntryAccountTarget,
    MatchEntryCharacterTarget,
    MatchEntryRosterSnapshot,
)
from uma_st2.application.match.staff_entry_queries import MatchEntryTargetChoice
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .match_entries import SqlAlchemyMatchEntryRepository
from .orm import GameAccountORM, MatchEntryORM, MatchORM, UmamusumeORM, UmamusumeVariantORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)

_EDITABLE_STATUS_VALUES = (MatchStatus.SCHEDULED.value, MatchStatus.ENTRY_CONFIRMED.value)


class SqlAlchemyMatchStaffEntryQueryRepository:
    """Build bounded PID-free Entry target and resolved draft DTOs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchEntryTargetChoice, ...]:
        entry_count = (
            select(func.count(MatchEntryORM.id))
            .where(MatchEntryORM.match_id == MatchORM.id)
            .correlate(MatchORM)
            .scalar_subquery()
        )
        statement = select(MatchORM, entry_count.label("entry_count")).where(
            MatchORM.source_kind == MatchSourceKind.NATIVE_V2.value,
            MatchORM.status.in_(_EDITABLE_STATUS_VALUES),
        )
        if search:
            statement = statement.where(MatchORM.name.contains(search))
        rows = self._session.execute(statement.order_by(MatchORM.scheduled_at, MatchORM.id).limit(limit)).all()
        return tuple(
            MatchEntryTargetChoice(
                match_id=match.id,
                match_name=match.name,
                status=MatchStatus(match.status),
                current_entry_count=stored_entry_count,
            )
            for match, stored_entry_count in rows
        )

    def get_target(self, *, match_id: int) -> MatchEntryRosterSnapshot | None:
        match = self._session.get(MatchORM, match_id)
        if match is None:
            return None
        entries = SqlAlchemyMatchEntryRepository(self._session)._current_entries(  # noqa: SLF001
            match_id=match_id,
            lock=False,
        )
        return MatchEntryRosterSnapshot(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
            entries=entries,
        )

    def search_game_accounts(
        self,
        *,
        chunk: str,
        limit: int,
    ) -> tuple[MatchEntryAccountTarget, ...]:
        normalized = chunk.casefold()
        nickname = func.lower(GameAccountORM.nickname)
        rank = case(
            (nickname == normalized, 0),
            (GameAccountORM.nickname.istartswith(chunk, autoescape=True), 1),
            else_=2,
        )
        rows = self._session.scalars(
            select(GameAccountORM)
            .where(GameAccountORM.nickname.icontains(chunk, autoescape=True))
            .order_by(rank, func.char_length(GameAccountORM.nickname), GameAccountORM.id)
            .limit(limit)
        )
        return tuple(self._account_target(account) for account in rows)

    def list_characters(self) -> tuple[MatchEntryCharacterTarget, ...]:
        candidates: list[MatchEntryCharacterTarget] = []
        base_rows = self._session.scalars(select(UmamusumeORM).order_by(UmamusumeORM.id))
        for base in base_rows:
            candidates.append(
                MatchEntryCharacterTarget(
                    umamusume_id=base.id,
                    umamusume_variant_id=None,
                    display_name=base.name_ko or base.name_jp,
                )
            )
        variant_rows = self._session.execute(
            select(UmamusumeVariantORM, UmamusumeORM)
            .join(UmamusumeORM, UmamusumeORM.id == UmamusumeVariantORM.umamusume_id)
            .order_by(UmamusumeVariantORM.id)
        )
        for variant, base in variant_rows:
            candidates.append(
                MatchEntryCharacterTarget(
                    umamusume_id=base.id,
                    umamusume_variant_id=variant.id,
                    display_name=variant.name_ko or variant.name_jp,
                )
            )
        candidates.sort(
            key=lambda item: (
                item.display_name.casefold(),
                item.umamusume_id,
                item.umamusume_variant_id or 0,
            )
        )
        return tuple(candidates)

    def resolve_game_accounts(self, *, ids: tuple[int, ...]) -> tuple[MatchEntryAccountTarget, ...]:
        resolved: list[MatchEntryAccountTarget] = []
        for account_id in ids:
            account = self._session.get(GameAccountORM, account_id)
            if account is None:
                return ()
            resolved.append(self._account_target(account))
        return tuple(resolved)

    def resolve_characters(
        self,
        *,
        identities: tuple[tuple[int, int | None], ...],
    ) -> tuple[MatchEntryCharacterTarget, ...]:
        resolved: list[MatchEntryCharacterTarget] = []
        for umamusume_id, variant_id in identities:
            base = self._session.get(UmamusumeORM, umamusume_id)
            if base is None:
                return ()
            if variant_id is None:
                resolved.append(
                    MatchEntryCharacterTarget(
                        umamusume_id=base.id,
                        umamusume_variant_id=None,
                        display_name=base.name_ko or base.name_jp,
                    )
                )
                continue
            variant = self._session.scalar(
                select(UmamusumeVariantORM).where(
                    UmamusumeVariantORM.id == variant_id,
                    UmamusumeVariantORM.umamusume_id == base.id,
                )
            )
            if variant is None:
                return ()
            resolved.append(
                MatchEntryCharacterTarget(
                    umamusume_id=base.id,
                    umamusume_variant_id=variant.id,
                    display_name=variant.name_ko or variant.name_jp,
                )
            )
        return tuple(resolved)

    @staticmethod
    def _account_target(account: GameAccountORM) -> MatchEntryAccountTarget:
        return MatchEntryAccountTarget(
            id=account.id,
            persona_id=account.persona_id,
            game_region=GameRegion(account.game_region),
            nickname=account.nickname,
            affiliation=account.affiliation,
        )


class SqlAlchemyMatchStaffEntryQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete read-only UoW for Match Entry editor projections."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._queries: SqlAlchemyMatchStaffEntryQueryRepository | None = None

    @property
    def match_staff_entry_queries(self) -> SqlAlchemyMatchStaffEntryQueryRepository:
        return self._require_active_repository(self._queries)

    def _activate_repositories(self) -> None:
        self._queries = SqlAlchemyMatchStaffEntryQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._queries = None


class SqlAlchemyMatchStaffEntryQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchStaffEntryQueryUnitOfWork]
):
    """Create one read-only Match Entry query UoW per operation."""

    unit_of_work_type = SqlAlchemyMatchStaffEntryQueryUnitOfWork
