"""SQLAlchemy persistence for staff peer GameAccount addition."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    AddedGameAccount,
    AddGameAccountToPersona,
    StaffGameAccountAddAuditError,
    StaffGameAccountAddAuditType,
    StaffGameAccountAddConcurrentConflictError,
    StaffGameAccountAddIdempotencyConflictError,
    StaffGameAccountAddState,
    StoredStaffGameAccountAddOperation,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

from .datetime_codec import to_database_utc
from .orm import CirclePointORM, GameAccountORM, IdentityOperationORM, OperationORM, PersonaORM
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
    game_region: GameRegion,
    uma_pid: str,
    lock_dependencies: bool,
) -> StaffGameAccountAddState:
    wallet_statement = select(CirclePointORM.persona_id).where(CirclePointORM.persona_id == persona.id)
    accounts_statement = (
        select(GameAccountORM.id, GameAccountORM.uma_pid)
        .where(GameAccountORM.persona_id == persona.id)
        .order_by(GameAccountORM.id.asc())
    )
    requested_statement = select(GameAccountORM.id, GameAccountORM.persona_id).where(
        GameAccountORM.game_region == game_region.value,
        GameAccountORM.uma_pid == uma_pid,
    )
    if lock_dependencies:
        wallet_statement = wallet_statement.with_for_update()
        accounts_statement = accounts_statement.with_for_update()
        requested_statement = requested_statement.with_for_update()
    has_wallet = session.scalar(wallet_statement) is not None
    accounts = session.execute(accounts_statement).all()
    requested = session.execute(requested_statement).one_or_none()
    return StaffGameAccountAddState(
        guild_id=guild_id,
        persona_id=persona.id,
        persona_display_name=persona.display_name,
        persona_status=PersonaStatus(persona.status),
        has_wallet=has_wallet,
        game_account_count=len(accounts),
        qualifying_game_account_count=sum(row.uma_pid is not None for row in accounts),
        game_region=game_region,
        uma_pid=uma_pid,
        registered_game_account_id=None if requested is None else requested.id,
        registered_persona_id=None if requested is None else requested.persona_id,
    )


class SqlAlchemyStaffGameAccountAddQueryRepository:
    """Materialize one detached peer-add Preview without locks or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_target_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffGameAccountAddState | None:
        persona = self._session.get(PersonaORM, persona_id)
        if persona is None:
            return None
        return _target_state(
            self._session,
            guild_id=guild_id,
            persona=persona,
            game_region=game_region,
            uma_pid=uma_pid,
            lock_dependencies=False,
        )


class SqlAlchemyStaffGameAccountAddRepository:
    """Serialize on the selected Persona and persist one audited peer account."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_target_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffGameAccountAddState | None:
        try:
            persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
            if persona is None:
                return None
            return _target_state(
                self._session,
                guild_id=guild_id,
                persona=persona,
                game_region=game_region,
                uma_pid=uma_pid,
                lock_dependencies=True,
            )
        except OperationalError as error:
            if _is_deadlock(error):
                raise StaffGameAccountAddConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this GameAccount addition."
                ) from error
            raise

    def find_operation(self, *, idempotency_key: str) -> StoredStaffGameAccountAddOperation | None:
        row = self._session.execute(
            select(
                OperationORM.request_fingerprint,
                IdentityOperationORM.type,
                IdentityOperationORM.persona_id,
                IdentityOperationORM.game_account_id,
                IdentityOperationORM.after_data,
            )
            .outerjoin(IdentityOperationORM, IdentityOperationORM.operation_id == OperationORM.id)
            .where(OperationORM.idempotency_key == idempotency_key)
            .with_for_update()
        ).one_or_none()
        if row is None:
            return None
        return StoredStaffGameAccountAddOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            after_data=row.after_data,
        )

    def add_game_account(
        self,
        *,
        command: AddGameAccountToPersona,
        state: StaffGameAccountAddState,
        added_at: datetime,
    ) -> AddedGameAccount:
        stored_at = to_database_utc(added_at, field_name="added_at")
        account_values: dict[str, object] = {
            "persona_id": state.persona_id,
            "game_region": command.game_region.value,
            "uma_pid": command.uma_pid,
            "nickname": command.nickname,
            "affiliation": command.affiliation,
            "created_at": stored_at,
            "updated_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            account_values["id"] = _sqlite_next_id(self._session, GameAccountORM.id)
        account = GameAccountORM(**account_values)
        self._session.add(account)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffGameAccountAddConcurrentConflictError(
                "Another Identity mutation registered this PID first."
            ) from error
        except OperationalError as error:
            if _is_deadlock(error):
                raise StaffGameAccountAddConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this GameAccount addition."
                ) from error
            raise

        operation_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.added_by_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": command.reason,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = _sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffGameAccountAddIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            if _is_deadlock(error):
                raise StaffGameAccountAddConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this GameAccount addition."
                ) from error
            raise

        result = AddedGameAccount(
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            persona_status=state.persona_status,
            game_account_id=account.id,
            game_region=command.game_region,
            uma_pid=command.uma_pid,
            nickname=command.nickname,
            affiliation=command.affiliation,
            has_wallet=state.has_wallet,
            game_account_count=state.game_account_count + 1,
            qualifying_game_account_count=state.qualifying_game_account_count + 1,
            reason=command.reason,
            added_at=added_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=state.persona_id,
                game_account_id=account.id,
                discord_user_id=None,
                type=StaffGameAccountAddAuditType.ADDED.value,
                before_data=state.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError) and _is_deadlock(error):
                raise StaffGameAccountAddConcurrentConflictError(
                    "Concurrent Identity mutation rolled back this GameAccount addition."
                ) from error
            raise StaffGameAccountAddAuditError("GameAccount-add audit could not be stored.") from error
        return result


class SqlAlchemyStaffGameAccountAddQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_game_account_add: SqlAlchemyStaffGameAccountAddQueryRepository | None = None

    @property
    def staff_game_account_add(self) -> SqlAlchemyStaffGameAccountAddQueryRepository:
        return self._require_active_repository(self._staff_game_account_add)

    def _activate_repositories(self) -> None:
        self._staff_game_account_add = SqlAlchemyStaffGameAccountAddQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_game_account_add = None


class SqlAlchemyStaffGameAccountAddQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffGameAccountAddQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffGameAccountAddQueryUnitOfWork


class SqlAlchemyStaffGameAccountAddUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_game_account_add: SqlAlchemyStaffGameAccountAddRepository | None = None

    @property
    def staff_game_account_add(self) -> SqlAlchemyStaffGameAccountAddRepository:
        return self._require_active_repository(self._staff_game_account_add)

    def _activate_repositories(self) -> None:
        self._staff_game_account_add = SqlAlchemyStaffGameAccountAddRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_game_account_add = None


class SqlAlchemyStaffGameAccountAddUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffGameAccountAddUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffGameAccountAddUnitOfWork
