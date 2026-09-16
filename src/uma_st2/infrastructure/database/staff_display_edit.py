"""SQLAlchemy persistence for staff display-information corrections."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity.display_edit import (
    StaffDisplayEditAuditError,
    StaffDisplayEditAuditType,
    StaffDisplayEditConcurrentConflictError,
    StaffDisplayEditIdempotencyConflictError,
    StaffDisplayGameAccountPage,
    StaffGameAccountDisplayState,
    StaffPersonaDisplayState,
    StoredStaffDisplayEditOperation,
    UpdatedGameAccountDisplayInfo,
    UpdatedPersonaDisplayName,
    UpdateGameAccountDisplayInfo,
    UpdatePersonaDisplayName,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus

from .datetime_codec import to_database_utc
from .orm import GameAccountORM, IdentityOperationORM, OperationORM, PersonaORM
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _sqlite_next_id(session: Session, column: object) -> int:
    value = session.scalar(select(func.coalesce(func.max(column), 0) + 1))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("SQLite test identity allocation failed.")
    return value


def _is_deadlock(error: OperationalError) -> bool:
    arguments = getattr(error.orig, "args", ())
    return bool(arguments) and arguments[0] == 1213


def _persona_state(*, guild_id: str, persona: PersonaORM) -> StaffPersonaDisplayState:
    return StaffPersonaDisplayState(
        guild_id=guild_id,
        persona_id=persona.id,
        display_name=persona.display_name,
        status=PersonaStatus(persona.status),
    )


def _game_account_state(
    *,
    guild_id: str,
    persona: PersonaORM,
    account: GameAccountORM,
) -> StaffGameAccountDisplayState:
    return StaffGameAccountDisplayState(
        guild_id=guild_id,
        persona_id=persona.id,
        persona_display_name=persona.display_name,
        persona_status=PersonaStatus(persona.status),
        game_account_id=account.id,
        game_region=GameRegion(account.game_region),
        uma_pid=account.uma_pid,
        nickname=account.nickname,
        affiliation=account.affiliation,
    )


class SqlAlchemyStaffDisplayEditQueryRepository:
    """Return detached current display state without locks or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_persona_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaDisplayState | None:
        persona = self._session.get(PersonaORM, persona_id)
        return None if persona is None else _persona_state(guild_id=guild_id, persona=persona)

    def list_game_accounts(
        self,
        *,
        guild_id: str,
        persona_id: str,
        offset: int,
        limit: int,
    ) -> StaffDisplayGameAccountPage | None:
        persona = self._session.get(PersonaORM, persona_id)
        if persona is None:
            return None
        total_count = self._session.scalar(
            select(func.count()).select_from(GameAccountORM).where(GameAccountORM.persona_id == persona_id)
        )
        if isinstance(total_count, bool) or not isinstance(total_count, int):
            raise StaffDisplayEditAuditError("GameAccount count is malformed.")
        accounts = self._session.scalars(
            select(GameAccountORM)
            .where(GameAccountORM.persona_id == persona_id)
            .order_by(GameAccountORM.id.asc())
            .offset(offset)
            .limit(limit)
        ).all()
        return StaffDisplayGameAccountPage(
            persona=_persona_state(guild_id=guild_id, persona=persona),
            items=tuple(
                _game_account_state(guild_id=guild_id, persona=persona, account=account) for account in accounts
            ),
            page=offset // limit,
            total_count=total_count,
        )

    def get_game_account_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        game_account_id: int,
    ) -> StaffGameAccountDisplayState | None:
        persona = self._session.get(PersonaORM, persona_id)
        if persona is None:
            return None
        account = self._session.scalar(
            select(GameAccountORM).where(
                GameAccountORM.id == game_account_id,
                GameAccountORM.persona_id == persona_id,
            )
        )
        return None if account is None else _game_account_state(guild_id=guild_id, persona=persona, account=account)


class SqlAlchemyStaffDisplayEditRepository:
    """Serialize on Persona and persist one narrow audited correction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_persona_state(self, *, guild_id: str, persona_id: str) -> StaffPersonaDisplayState | None:
        try:
            persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
        except OperationalError as error:
            self._raise_concurrency(error)
            raise
        return None if persona is None else _persona_state(guild_id=guild_id, persona=persona)

    def lock_game_account_state(
        self,
        *,
        guild_id: str,
        persona_id: str,
        game_account_id: int,
    ) -> StaffGameAccountDisplayState | None:
        try:
            persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
            if persona is None:
                return None
            account = self._session.scalar(
                select(GameAccountORM)
                .where(
                    GameAccountORM.id == game_account_id,
                    GameAccountORM.persona_id == persona_id,
                )
                .with_for_update()
            )
        except OperationalError as error:
            self._raise_concurrency(error)
            raise
        return None if account is None else _game_account_state(guild_id=guild_id, persona=persona, account=account)

    def find_operation(self, *, idempotency_key: str) -> StoredStaffDisplayEditOperation | None:
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
        return StoredStaffDisplayEditOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            after_data=row.after_data,
        )

    def update_persona(
        self,
        *,
        command: UpdatePersonaDisplayName,
        state: StaffPersonaDisplayState,
        updated_at: datetime,
    ) -> UpdatedPersonaDisplayName:
        persona = self._session.get(PersonaORM, state.persona_id)
        if persona is None:
            raise StaffDisplayEditConcurrentConflictError("The locked Persona disappeared.")
        stored_at = to_database_utc(updated_at, field_name="updated_at")
        result = UpdatedPersonaDisplayName(
            persona_id=state.persona_id,
            previous_display_name=state.display_name,
            display_name=command.display_name,
            status=state.status,
            reason=command.reason,
            updated_at=updated_at,
        )
        persona.display_name = command.display_name
        persona.updated_at = stored_at
        operation = self._add_operation(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.updated_by_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=state.persona_id,
                game_account_id=None,
                discord_user_id=None,
                type=StaffDisplayEditAuditType.PERSONA_UPDATED.value,
                before_data=state.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        self._flush_audit()
        return result

    def update_game_account(
        self,
        *,
        command: UpdateGameAccountDisplayInfo,
        state: StaffGameAccountDisplayState,
        updated_at: datetime,
    ) -> UpdatedGameAccountDisplayInfo:
        account = self._session.get(GameAccountORM, state.game_account_id)
        if account is None or account.persona_id != state.persona_id:
            raise StaffDisplayEditConcurrentConflictError("The locked GameAccount ownership changed.")
        stored_at = to_database_utc(updated_at, field_name="updated_at")
        result = UpdatedGameAccountDisplayInfo(
            persona_id=state.persona_id,
            persona_display_name=state.persona_display_name,
            persona_status=state.persona_status,
            game_account_id=state.game_account_id,
            game_region=state.game_region,
            uma_pid=state.uma_pid,
            previous_nickname=state.nickname,
            nickname=command.nickname,
            previous_affiliation=state.affiliation,
            affiliation=command.affiliation,
            reason=command.reason,
            updated_at=updated_at,
        )
        account.nickname = command.nickname
        account.affiliation = command.affiliation
        account.updated_at = stored_at
        operation = self._add_operation(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.updated_by_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=state.persona_id,
                game_account_id=state.game_account_id,
                discord_user_id=None,
                type=StaffDisplayEditAuditType.GAME_ACCOUNT_UPDATED.value,
                before_data=state.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        self._flush_audit()
        return result

    def _add_operation(
        self,
        *,
        guild_id: str,
        correlation_id: str | None,
        actor_discord_user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        reason: str,
        created_at: datetime,
    ) -> OperationORM:
        values: dict[str, object] = {
            "guild_id": guild_id,
            "correlation_id": correlation_id,
            "actor_discord_user_id": actor_discord_user_id,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "reason": reason,
            "created_at": created_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            values["id"] = _sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffDisplayEditIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            self._raise_concurrency(error)
            raise
        return operation

    def _flush_audit(self) -> None:
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError):
                self._raise_concurrency(error)
            raise StaffDisplayEditAuditError("Display-edit audit could not be stored.") from error

    @staticmethod
    def _raise_concurrency(error: OperationalError) -> None:
        if _is_deadlock(error):
            raise StaffDisplayEditConcurrentConflictError(
                "Concurrent Identity mutation rolled back this display edit."
            ) from error


class SqlAlchemyStaffDisplayEditQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_display_edit: SqlAlchemyStaffDisplayEditQueryRepository | None = None

    @property
    def staff_display_edit(self) -> SqlAlchemyStaffDisplayEditQueryRepository:
        return self._require_active_repository(self._staff_display_edit)

    def _activate_repositories(self) -> None:
        self._staff_display_edit = SqlAlchemyStaffDisplayEditQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_display_edit = None


class SqlAlchemyStaffDisplayEditQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffDisplayEditQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffDisplayEditQueryUnitOfWork


class SqlAlchemyStaffDisplayEditUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_display_edit: SqlAlchemyStaffDisplayEditRepository | None = None

    @property
    def staff_display_edit(self) -> SqlAlchemyStaffDisplayEditRepository:
        return self._require_active_repository(self._staff_display_edit)

    def _activate_repositories(self) -> None:
        self._staff_display_edit = SqlAlchemyStaffDisplayEditRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_display_edit = None


class SqlAlchemyStaffDisplayEditUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffDisplayEditUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffDisplayEditUnitOfWork
