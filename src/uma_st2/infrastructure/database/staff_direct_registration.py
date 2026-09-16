"""SQLAlchemy query and mutation boundaries for staff direct registration."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    StaffDirectRegistrationAuditError,
    StaffDirectRegistrationConcurrentConflictError,
    StaffDirectRegistrationState,
    StoredStaffDirectRegistrationOperation,
)
from uma_st2.domain.identity import GameRegion

from .datetime_codec import to_database_utc
from .orm import (
    DiscordAccountORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
)
from .registration_bootstrap import (
    SqlAlchemyRegistrationBootstrapRepository,
    is_mariadb_deadlock,
    sqlite_next_id,
)
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _target_state(
    session: Session,
    *,
    guild_id: str,
    target_discord_user_id: str,
    game_region: GameRegion,
    uma_pid: str,
    current_persona_id: str | None,
    lock_dependencies: bool,
) -> StaffDirectRegistrationState:
    request_statement = (
        select(GameAccountRegistrationRequestORM.id)
        .where(
            GameAccountRegistrationRequestORM.guild_id == guild_id,
            GameAccountRegistrationRequestORM.requester_discord_user_id == target_discord_user_id,
            GameAccountRegistrationRequestORM.active_marker.is_(True),
        )
        .order_by(GameAccountRegistrationRequestORM.id.asc())
        .limit(1)
    )
    if lock_dependencies:
        request_statement = request_statement.with_for_update()
    active_request_id = session.scalar(request_statement)
    registered_game_account_id = session.scalar(
        select(GameAccountORM.id).where(
            GameAccountORM.game_region == game_region.value,
            GameAccountORM.uma_pid == uma_pid,
        )
    )
    return StaffDirectRegistrationState(
        guild_id=guild_id,
        target_discord_user_id=target_discord_user_id,
        game_region=game_region,
        uma_pid=uma_pid,
        current_persona_id=current_persona_id,
        active_registration_request_id=active_request_id,
        registered_game_account_id=registered_game_account_id,
    )


class SqlAlchemyStaffDirectRegistrationQueryRepository:
    """Materialize one detached preflight state without locks or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_target_state(
        self,
        *,
        guild_id: str,
        target_discord_user_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffDirectRegistrationState:
        current_persona_id = self._session.scalar(
            select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == target_discord_user_id)
        )
        return _target_state(
            self._session,
            guild_id=guild_id,
            target_discord_user_id=target_discord_user_id,
            game_region=game_region,
            uma_pid=uma_pid,
            current_persona_id=current_persona_id,
            lock_dependencies=False,
        )


class SqlAlchemyStaffDirectRegistrationRepository(SqlAlchemyRegistrationBootstrapRepository):
    """Serialize on the Discord target and invoke the shared bootstrap writer."""

    def lock_target_state(
        self,
        *,
        guild_id: str,
        target_discord_user_id: str,
        game_region: GameRegion,
        uma_pid: str,
        created_at: datetime,
    ) -> StaffDirectRegistrationState:
        target = self._lock_or_create_discord_account(
            discord_user_id=target_discord_user_id,
            created_at=created_at,
        )
        return _target_state(
            self._session,
            guild_id=guild_id,
            target_discord_user_id=target_discord_user_id,
            game_region=game_region,
            uma_pid=uma_pid,
            current_persona_id=target.persona_id,
            lock_dependencies=True,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDirectRegistrationOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                IdentityOperationORM.type,
                IdentityOperationORM.persona_id,
                IdentityOperationORM.game_account_id,
                IdentityOperationORM.discord_user_id,
                IdentityOperationORM.after_data,
            )
            .outerjoin(IdentityOperationORM, IdentityOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredStaffDirectRegistrationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            discord_user_id=row.discord_user_id,
            after_data=row.after_data,
        )

    def _lock_or_create_discord_account(
        self,
        *,
        discord_user_id: str,
        created_at: datetime,
    ) -> DiscordAccountORM:
        stored_at = to_database_utc(created_at, field_name="created_at")
        try:
            if self._session.get_bind().dialect.name in {"mysql", "mariadb"}:
                statement = mysql_insert(DiscordAccountORM).values(
                    discord_user_id=discord_user_id,
                    persona_id=None,
                    created_at=stored_at,
                    updated_at=stored_at,
                )
                self._session.execute(statement.on_duplicate_key_update(id=DiscordAccountORM.id))
            else:
                existing_id = self._session.scalar(
                    select(DiscordAccountORM.id).where(DiscordAccountORM.discord_user_id == discord_user_id)
                )
                if existing_id is None:
                    self._session.add(
                        DiscordAccountORM(
                            id=sqlite_next_id(self._session, DiscordAccountORM.id),
                            discord_user_id=discord_user_id,
                            persona_id=None,
                            created_at=stored_at,
                            updated_at=stored_at,
                        )
                    )
                    self._session.flush()
            target = self._session.scalar(
                select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id).with_for_update()
            )
        except OperationalError as error:
            if is_mariadb_deadlock(error):
                raise StaffDirectRegistrationConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this direct registration."
                ) from error
            raise
        if target is None:
            raise StaffDirectRegistrationAuditError("Discord registration target was not created.")
        return target


class SqlAlchemyStaffDirectRegistrationQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_direct_registration: SqlAlchemyStaffDirectRegistrationQueryRepository | None = None

    @property
    def staff_direct_registration(self) -> SqlAlchemyStaffDirectRegistrationQueryRepository:
        return self._require_active_repository(self._staff_direct_registration)

    def _activate_repositories(self) -> None:
        self._staff_direct_registration = SqlAlchemyStaffDirectRegistrationQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_direct_registration = None


class SqlAlchemyStaffDirectRegistrationQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffDirectRegistrationQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffDirectRegistrationQueryUnitOfWork


class SqlAlchemyStaffDirectRegistrationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_direct_registration: SqlAlchemyStaffDirectRegistrationRepository | None = None

    @property
    def staff_direct_registration(self) -> SqlAlchemyStaffDirectRegistrationRepository:
        return self._require_active_repository(self._staff_direct_registration)

    def _activate_repositories(self) -> None:
        self._staff_direct_registration = SqlAlchemyStaffDirectRegistrationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_direct_registration = None


class SqlAlchemyStaffDirectRegistrationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffDirectRegistrationUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffDirectRegistrationUnitOfWork
