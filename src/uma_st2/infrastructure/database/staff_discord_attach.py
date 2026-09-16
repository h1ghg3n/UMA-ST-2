"""SQLAlchemy persistence for staff direct Discord-to-Persona attachment."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    AttachDiscordAccountToPersona,
    AttachedDiscordAccount,
    StaffDiscordAttachAuditError,
    StaffDiscordAttachAuditType,
    StaffDiscordAttachConcurrentConflictError,
    StaffDiscordAttachIdempotencyConflictError,
    StaffDiscordAttachState,
    StoredStaffDiscordAttachOperation,
)
from uma_st2.domain.identity import PersonaStatus

from .datetime_codec import to_database_utc
from .orm import (
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
)
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _sqlite_next_id(session: Session, column: object) -> int:
    value = session.scalar(select(func.coalesce(func.max(column), 0) + 1))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("SQLite test identity allocation failed.")
    return value


def _is_deadlock(error: OperationalError) -> bool:
    arguments = getattr(error.orig, "args", ())
    return bool(arguments) and arguments[0] == 1213


def _target_state(
    session: Session,
    *,
    guild_id: str,
    persona: PersonaORM,
    discord_user_id: str,
    current_persona_id: str | None,
    lock_dependencies: bool,
) -> StaffDiscordAttachState:
    wallet_statement = select(CirclePointORM.persona_id).where(CirclePointORM.persona_id == persona.id)
    accounts_statement = (
        select(GameAccountORM.id, GameAccountORM.uma_pid)
        .where(GameAccountORM.persona_id == persona.id)
        .order_by(GameAccountORM.id.asc())
    )
    request_statement = (
        select(GameAccountRegistrationRequestORM.id)
        .where(
            GameAccountRegistrationRequestORM.guild_id == guild_id,
            GameAccountRegistrationRequestORM.requester_discord_user_id == discord_user_id,
            GameAccountRegistrationRequestORM.active_marker.is_(True),
        )
        .order_by(GameAccountRegistrationRequestORM.id.asc())
        .limit(1)
    )
    if lock_dependencies:
        wallet_statement = wallet_statement.with_for_update()
        accounts_statement = accounts_statement.with_for_update()
        request_statement = request_statement.with_for_update()
    has_wallet = session.scalar(wallet_statement) is not None
    account_rows = session.execute(accounts_statement).all()
    active_request_id = session.scalar(request_statement)
    return StaffDiscordAttachState(
        guild_id=guild_id,
        persona_id=persona.id,
        persona_display_name=persona.display_name,
        persona_status=PersonaStatus(persona.status),
        target_discord_user_id=discord_user_id,
        current_persona_id=current_persona_id,
        active_registration_request_id=active_request_id,
        has_wallet=has_wallet,
        qualifying_game_account_count=sum(row.uma_pid is not None for row in account_rows),
    )


class SqlAlchemyStaffDiscordAttachQueryRepository:
    """Materialize one detached direct-attach Preview without locks or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_target_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        discord_user_id: str,
    ) -> StaffDiscordAttachState | None:
        persona = self._session.get(PersonaORM, persona_id)
        if persona is None:
            return None
        current_persona_id = self._session.scalar(
            select(DiscordAccountORM.persona_id).where(DiscordAccountORM.discord_user_id == discord_user_id)
        )
        return _target_state(
            self._session,
            guild_id=guild_id,
            persona=persona,
            discord_user_id=discord_user_id,
            current_persona_id=current_persona_id,
            lock_dependencies=False,
        )


class SqlAlchemyStaffDiscordAttachRepository:
    """Serialize on the Discord target and persist one audited access link."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        discord_user_id: str,
        created_at: datetime,
    ) -> StaffDiscordAttachState | None:
        target = self._lock_or_create_discord_account(
            discord_user_id=discord_user_id,
            created_at=created_at,
        )
        persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
        if persona is None:
            return None
        return _target_state(
            self._session,
            guild_id=guild_id,
            persona=persona,
            discord_user_id=discord_user_id,
            current_persona_id=target.persona_id,
            lock_dependencies=True,
        )

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDiscordAttachOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                IdentityOperationORM.type,
                IdentityOperationORM.persona_id,
                IdentityOperationORM.discord_user_id,
                IdentityOperationORM.after_data,
            )
            .outerjoin(IdentityOperationORM, IdentityOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredStaffDiscordAttachOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            discord_user_id=row.discord_user_id,
            after_data=row.after_data,
        )

    def attach(
        self,
        *,
        command: AttachDiscordAccountToPersona,
        state: StaffDiscordAttachState,
        attached_at: datetime,
    ) -> AttachedDiscordAccount:
        stored_at = to_database_utc(attached_at, field_name="attached_at")
        target = self._session.scalar(
            select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == state.target_discord_user_id)
        )
        if target is None or target.persona_id is not None:
            raise StaffDiscordAttachAuditError("Locked Discord attach target is unavailable.")
        target.persona_id = state.persona_id
        target.updated_at = stored_at

        operation_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.attached_by_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": command.operational_note,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = _sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffDiscordAttachIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            if _is_deadlock(error):
                raise StaffDiscordAttachConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this direct attach."
                ) from error
            raise

        result = AttachedDiscordAccount(
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            persona_status=state.persona_status,
            target_discord_user_id=state.target_discord_user_id,
            has_wallet=state.has_wallet,
            qualifying_game_account_count=state.qualifying_game_account_count,
            operational_note=command.operational_note,
            attached_at=attached_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=state.persona_id,
                game_account_id=None,
                discord_user_id=state.target_discord_user_id,
                type=StaffDiscordAttachAuditType.ATTACHED.value,
                before_data=state.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        self._session.flush()
        return result

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
                            id=_sqlite_next_id(self._session, DiscordAccountORM.id),
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
            if _is_deadlock(error):
                raise StaffDiscordAttachConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this direct attach."
                ) from error
            raise
        if target is None:
            raise StaffDiscordAttachAuditError("Discord attach target was not created.")
        return target


class SqlAlchemyStaffDiscordAttachQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_discord_attach: SqlAlchemyStaffDiscordAttachQueryRepository | None = None

    @property
    def staff_discord_attach(self) -> SqlAlchemyStaffDiscordAttachQueryRepository:
        return self._require_active_repository(self._staff_discord_attach)

    def _activate_repositories(self) -> None:
        self._staff_discord_attach = SqlAlchemyStaffDiscordAttachQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_discord_attach = None


class SqlAlchemyStaffDiscordAttachQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffDiscordAttachQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffDiscordAttachQueryUnitOfWork


class SqlAlchemyStaffDiscordAttachUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_discord_attach: SqlAlchemyStaffDiscordAttachRepository | None = None

    @property
    def staff_discord_attach(self) -> SqlAlchemyStaffDiscordAttachRepository:
        return self._require_active_repository(self._staff_discord_attach)

    def _activate_repositories(self) -> None:
        self._staff_discord_attach = SqlAlchemyStaffDiscordAttachRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_discord_attach = None


class SqlAlchemyStaffDiscordAttachUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffDiscordAttachUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffDiscordAttachUnitOfWork
