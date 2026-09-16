"""SQLAlchemy implementation of native Match Entry roster replacement."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session, aliased

from uma_st2.application.match.entries import (
    MatchEntryAccountTarget,
    MatchEntryCharacterTarget,
    MatchEntryReference,
    MatchEntryRosterSnapshot,
    MatchEntrySnapshot,
    ReplaceMatchEntries,
    StoredMatchEntryOperation,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.domain.match import MatchSourceKind, MatchStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    BetORM,
    GameAccountORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    MatchResultSubmissionORM,
    OperationORM,
    RatingTransactionORM,
    UmamusumeORM,
    UmamusumeVariantORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


class SqlAlchemyMatchEntryRepository:
    """Lock, replace, and audit one complete Match Entry roster."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_match(self, *, match_id: int) -> MatchEntryRosterSnapshot | None:
        match = self._session.scalar(select(MatchORM).where(MatchORM.id == match_id).with_for_update())
        if match is None:
            return None
        return MatchEntryRosterSnapshot(
            match_id=match.id,
            match_name=match.name,
            source_kind=MatchSourceKind(match.source_kind),
            status=MatchStatus(match.status),
        )

    def find_operation(self, *, idempotency_key: str) -> StoredMatchEntryOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                MatchOperationORM.type,
                MatchOperationORM.match_id,
                MatchOperationORM.after_data,
            )
            .outerjoin(MatchOperationORM, MatchOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredMatchEntryOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            match_id=row.match_id,
            after_data=row.after_data,
        )

    def lock_current_entries(self, *, match_id: int) -> tuple[MatchEntrySnapshot, ...]:
        return self._current_entries(match_id=match_id, lock=True)

    def has_roster_dependents(self, *, match_id: int, entry_ids: tuple[int, ...]) -> bool:
        if self._session.scalar(select(BetORM.id).where(BetORM.match_id == match_id).limit(1)) is not None:
            return True
        if (
            self._session.scalar(
                select(MatchResultSubmissionORM.id).where(MatchResultSubmissionORM.match_id == match_id).limit(1)
            )
            is not None
        ):
            return True
        result_fact = self._session.scalar(
            select(MatchEntryORM.id)
            .where(
                MatchEntryORM.match_id == match_id,
                or_(
                    MatchEntryORM.rank.is_not(None),
                    MatchEntryORM.popularity_rank.is_not(None),
                    MatchEntryORM.margin.is_not(None),
                ),
            )
            .limit(1)
        )
        if result_fact is not None:
            return True
        if entry_ids:
            return (
                self._session.scalar(
                    select(RatingTransactionORM.id).where(RatingTransactionORM.match_entry_id.in_(entry_ids)).limit(1)
                )
                is not None
            )
        return False

    def lock_game_accounts(self, *, account_ids: tuple[int, ...]) -> tuple[MatchEntryAccountTarget, ...]:
        rows = self._session.scalars(
            select(GameAccountORM)
            .where(GameAccountORM.id.in_(account_ids))
            .order_by(GameAccountORM.id)
            .with_for_update()
        )
        return tuple(
            MatchEntryAccountTarget(
                id=row.id,
                persona_id=row.persona_id,
                game_region=GameRegion(row.game_region),
                nickname=row.nickname,
                affiliation=row.affiliation,
            )
            for row in rows
        )

    def lock_characters(
        self,
        *,
        identities: tuple[tuple[int, int | None], ...],
    ) -> tuple[MatchEntryCharacterTarget, ...]:
        targets: list[MatchEntryCharacterTarget] = []
        for umamusume_id, variant_id in identities:
            if variant_id is None:
                base = self._session.scalar(
                    select(UmamusumeORM).where(UmamusumeORM.id == umamusume_id).with_for_update()
                )
                if base is None:
                    continue
                targets.append(
                    MatchEntryCharacterTarget(
                        umamusume_id=base.id,
                        umamusume_variant_id=None,
                        display_name=base.name_ko or base.name_jp,
                    )
                )
                continue
            row = self._session.execute(
                select(UmamusumeORM, UmamusumeVariantORM)
                .join(UmamusumeVariantORM, UmamusumeVariantORM.umamusume_id == UmamusumeORM.id)
                .where(
                    UmamusumeORM.id == umamusume_id,
                    UmamusumeVariantORM.id == variant_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                continue
            base, variant = row
            targets.append(
                MatchEntryCharacterTarget(
                    umamusume_id=base.id,
                    umamusume_variant_id=variant.id,
                    display_name=variant.name_ko or variant.name_jp,
                )
            )
        return tuple(targets)

    def replace_entries(
        self,
        *,
        match_id: int,
        entries: tuple[MatchEntryReference, ...],
        accounts: Mapping[int, MatchEntryAccountTarget],
        characters: Mapping[tuple[int, int | None], MatchEntryCharacterTarget],
        changed_at: datetime,
    ) -> tuple[MatchEntrySnapshot, ...]:
        stored_changed_at = to_database_utc(changed_at, field_name="changed_at")
        self._session.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == match_id))
        self._session.flush()
        rows: list[tuple[MatchEntryORM, MatchEntryAccountTarget, MatchEntryCharacterTarget]] = []
        for entry in entries:
            account = accounts[entry.game_account_id]
            character = characters[(entry.umamusume_id, entry.umamusume_variant_id)]
            row = MatchEntryORM(
                match_id=match_id,
                game_account_id=account.id,
                owner_at_event_persona_id=account.persona_id,
                affiliation_at_event=account.affiliation,
                umamusume_id=character.umamusume_id,
                umamusume_variant_id=character.umamusume_variant_id,
                entry_number=entry.entry_number,
                running_style=None,
                training_grade=None,
                rank=None,
                popularity_rank=None,
                margin=None,
                created_at=stored_changed_at,
                updated_at=stored_changed_at,
            )
            self._session.add(row)
            rows.append((row, account, character))
        self._session.flush()
        changed = self._session.execute(
            update(MatchORM).where(MatchORM.id == match_id).values(updated_at=stored_changed_at)
        )
        if changed.rowcount != 1:
            raise RuntimeError("Match Entry replacement lost its locked Match row.")
        return tuple(
            MatchEntrySnapshot(
                entry_id=row.id,
                entry_number=row.entry_number,
                game_account_id=row.game_account_id,
                owner_at_event_persona_id=row.owner_at_event_persona_id,
                affiliation_at_event=row.affiliation_at_event,
                game_region=account.game_region,
                game_account_name=account.nickname,
                umamusume_id=row.umamusume_id,
                umamusume_variant_id=row.umamusume_variant_id,
                umamusume_name=character.display_name,
                created_at=changed_at,
            )
            for row, account, character in rows
        )

    def add_audit(
        self,
        *,
        command: ReplaceMatchEntries,
        before: MatchEntryRosterSnapshot,
        after: MatchEntryRosterSnapshot,
        created_at: datetime,
    ) -> None:
        operation = OperationORM(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.actor_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=to_database_utc(created_at, field_name="created_at"),
        )
        self._session.add(operation)
        self._session.flush()
        self._session.add(
            MatchOperationORM(
                operation_id=operation.id,
                match_id=after.match_id,
                type="match_entries_replaced",
                before_data=before.to_audit_payload(),
                after_data=after.to_audit_payload(),
            )
        )
        self._session.flush()

    def _current_entries(self, *, match_id: int, lock: bool) -> tuple[MatchEntrySnapshot, ...]:
        variant = aliased(UmamusumeVariantORM)
        statement = (
            select(MatchEntryORM, GameAccountORM, UmamusumeORM, variant)
            .join(GameAccountORM, GameAccountORM.id == MatchEntryORM.game_account_id)
            .join(UmamusumeORM, UmamusumeORM.id == MatchEntryORM.umamusume_id)
            .outerjoin(variant, variant.id == MatchEntryORM.umamusume_variant_id)
            .where(MatchEntryORM.match_id == match_id)
            .order_by(MatchEntryORM.entry_number, MatchEntryORM.id)
        )
        if lock:
            statement = statement.with_for_update()
        rows = self._session.execute(statement)
        return tuple(
            MatchEntrySnapshot(
                entry_id=entry.id,
                entry_number=entry.entry_number,
                game_account_id=entry.game_account_id,
                owner_at_event_persona_id=entry.owner_at_event_persona_id,
                affiliation_at_event=entry.affiliation_at_event,
                game_region=GameRegion(account.game_region),
                game_account_name=account.nickname,
                umamusume_id=entry.umamusume_id,
                umamusume_variant_id=entry.umamusume_variant_id,
                umamusume_name=(variant_row.name_ko or variant_row.name_jp)
                if variant_row is not None
                else (base.name_ko or base.name_jp),
                created_at=from_database_utc(entry.created_at, field_name="match_entries.created_at"),
            )
            for entry, account, base, variant_row in rows
        )


class SqlAlchemyMatchEntryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing Match Entry mutation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._match_entries: SqlAlchemyMatchEntryRepository | None = None

    @property
    def match_entries(self) -> SqlAlchemyMatchEntryRepository:
        return self._require_active_repository(self._match_entries)

    def _activate_repositories(self) -> None:
        self._match_entries = SqlAlchemyMatchEntryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._match_entries = None


class SqlAlchemyMatchEntryUnitOfWorkFactory(SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyMatchEntryUnitOfWork]):
    """Create one Match Entry UoW per Application command."""

    unit_of_work_type = SqlAlchemyMatchEntryUnitOfWork
