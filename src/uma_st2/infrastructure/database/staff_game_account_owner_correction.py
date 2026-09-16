"""SQLAlchemy persistence for audited GameAccount owner correction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    CorrectedGameAccountOwner,
    CorrectGameAccountOwner,
    StaffGameAccountOwnerCorrectionAuditError,
    StaffGameAccountOwnerCorrectionAuditType,
    StaffGameAccountOwnerCorrectionConcurrentConflictError,
    StaffGameAccountOwnerCorrectionIdempotencyConflictError,
    StaffGameAccountOwnerCorrectionStaleError,
    StaffGameAccountOwnerCorrectionState,
    StoredStaffGameAccountOwnerCorrectionOperation,
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


def _raise_concurrent(error: OperationalError) -> None:
    if _is_deadlock(error):
        raise StaffGameAccountOwnerCorrectionConcurrentConflictError(
            "Concurrent Identity mutation rolled back this owner correction."
        ) from error


def _state(
    session: Session,
    *,
    guild_id: str,
    target_persona: PersonaORM,
    account: GameAccountORM,
    source_persona: PersonaORM,
    lock_dependencies: bool,
) -> StaffGameAccountOwnerCorrectionState:
    persona_ids = sorted({source_persona.id, target_persona.id})
    wallet_statement = (
        select(CirclePointORM.persona_id)
        .where(CirclePointORM.persona_id.in_(persona_ids))
        .order_by(CirclePointORM.persona_id.asc())
    )
    accounts_statement = (
        select(GameAccountORM.persona_id, GameAccountORM.id, GameAccountORM.uma_pid)
        .where(GameAccountORM.persona_id.in_(persona_ids))
        .order_by(GameAccountORM.persona_id.asc(), GameAccountORM.id.asc())
    )
    if lock_dependencies:
        wallet_statement = wallet_statement.with_for_update()
        accounts_statement = accounts_statement.with_for_update()
    wallets = set(session.scalars(wallet_statement).all())
    account_rows = session.execute(accounts_statement).all()

    def counts(persona_id: str) -> tuple[int, int]:
        owned = [row for row in account_rows if row.persona_id == persona_id]
        return len(owned), sum(row.uma_pid is not None for row in owned)

    source_count, source_qualifying_count = counts(source_persona.id)
    target_count, target_qualifying_count = counts(target_persona.id)
    return StaffGameAccountOwnerCorrectionState(
        guild_id=guild_id,
        game_account_id=account.id,
        game_region=GameRegion(account.game_region),
        uma_pid=account.uma_pid,
        nickname=account.nickname,
        affiliation=account.affiliation,
        source_persona_id=source_persona.id,
        source_persona_display_name=source_persona.display_name,
        source_persona_status=PersonaStatus(source_persona.status),
        source_has_wallet=source_persona.id in wallets,
        source_game_account_count=source_count,
        source_qualifying_game_account_count=source_qualifying_count,
        target_persona_id=target_persona.id,
        target_persona_display_name=target_persona.display_name,
        target_persona_status=PersonaStatus(target_persona.status),
        target_has_wallet=target_persona.id in wallets,
        target_game_account_count=target_count,
        target_qualifying_game_account_count=target_qualifying_count,
    )


class SqlAlchemyStaffGameAccountOwnerCorrectionQueryRepository:
    """Materialize a detached owner-correction Preview without locks or writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_state(
        self,
        *,
        guild_id: str,
        target_persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffGameAccountOwnerCorrectionState | None:
        target_persona = self._session.get(PersonaORM, target_persona_id)
        account = self._session.scalar(
            select(GameAccountORM).where(
                GameAccountORM.game_region == game_region.value,
                GameAccountORM.uma_pid == uma_pid,
            )
        )
        if target_persona is None or account is None:
            return None
        source_persona = self._session.get(PersonaORM, account.persona_id)
        if source_persona is None:
            return None
        return _state(
            self._session,
            guild_id=guild_id,
            target_persona=target_persona,
            account=account,
            source_persona=source_persona,
            lock_dependencies=False,
        )


class SqlAlchemyStaffGameAccountOwnerCorrectionRepository:
    """Lock both Persona roots and move one existing GameAccount owner."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_state(
        self,
        *,
        guild_id: str,
        target_persona_id: str,
        expected_source_persona_id: str,
        game_region: GameRegion,
        uma_pid: str,
    ) -> StaffGameAccountOwnerCorrectionState | None:
        try:
            locked_personas: dict[str, PersonaORM] = {}
            for persona_id in sorted({expected_source_persona_id, target_persona_id}):
                persona = self._session.scalar(select(PersonaORM).where(PersonaORM.id == persona_id).with_for_update())
                if persona is None:
                    return None
                locked_personas[persona_id] = persona
            account = self._session.scalar(
                select(GameAccountORM)
                .where(
                    GameAccountORM.game_region == game_region.value,
                    GameAccountORM.uma_pid == uma_pid,
                )
                .with_for_update()
            )
            if account is None:
                return None
            if account.persona_id not in locked_personas:
                raise StaffGameAccountOwnerCorrectionStaleError(
                    "GameAccount owner changed outside the expected correction pair."
                )
            return _state(
                self._session,
                guild_id=guild_id,
                target_persona=locked_personas[target_persona_id],
                account=account,
                source_persona=locked_personas[account.persona_id],
                lock_dependencies=True,
            )
        except OperationalError as error:
            _raise_concurrent(error)
            raise

    def find_operation(
        self,
        *,
        idempotency_key: str,
    ) -> StoredStaffGameAccountOwnerCorrectionOperation | None:
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
        return StoredStaffGameAccountOwnerCorrectionOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            after_data=row.after_data,
        )

    def reassign_owner(
        self,
        *,
        command: CorrectGameAccountOwner,
        state: StaffGameAccountOwnerCorrectionState,
        corrected_at: datetime,
    ) -> CorrectedGameAccountOwner:
        stored_at = to_database_utc(corrected_at, field_name="corrected_at")
        account = self._session.get(GameAccountORM, state.game_account_id)
        if (
            account is None
            or account.persona_id != state.source_persona_id
            or account.game_region != state.game_region.value
            or account.uma_pid != state.uma_pid
        ):
            raise StaffGameAccountOwnerCorrectionAuditError("Locked GameAccount is unavailable.")
        account.persona_id = state.target_persona_id
        account.updated_at = stored_at
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffGameAccountOwnerCorrectionConcurrentConflictError(
                "Another Identity mutation changed this GameAccount first."
            ) from error
        except OperationalError as error:
            _raise_concurrent(error)
            raise

        operation_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.corrected_by_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": command.evidence_reason,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = _sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffGameAccountOwnerCorrectionIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            _raise_concurrent(error)
            raise

        result = CorrectedGameAccountOwner(
            game_account_id=state.game_account_id,
            game_region=state.game_region,
            uma_pid=state.uma_pid,
            nickname=state.nickname,
            affiliation=state.affiliation,
            source_persona_id=state.source_persona_id,
            source_persona_display_name=state.source_persona_display_name,
            source_persona_status=state.source_persona_status,
            source_has_wallet=state.source_has_wallet,
            source_game_account_count=state.source_resulting_game_account_count,
            source_qualifying_game_account_count=state.source_resulting_qualifying_game_account_count,
            target_persona_id=state.target_persona_id,
            target_persona_display_name=state.target_persona_display_name,
            target_persona_status=state.target_persona_status,
            target_has_wallet=state.target_has_wallet,
            target_game_account_count=state.target_resulting_game_account_count,
            target_qualifying_game_account_count=state.target_resulting_qualifying_game_account_count,
            evidence_reason=command.evidence_reason,
            corrected_at=corrected_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=state.target_persona_id,
                game_account_id=state.game_account_id,
                discord_user_id=None,
                type=StaffGameAccountOwnerCorrectionAuditType.OWNER_REASSIGNED.value,
                before_data=state.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            if isinstance(error, OperationalError):
                _raise_concurrent(error)
            raise StaffGameAccountOwnerCorrectionAuditError(
                "GameAccount owner-correction audit could not be stored."
            ) from error
        return result


class SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyStaffGameAccountOwnerCorrectionQueryRepository | None = None

    @property
    def staff_game_account_owner_correction(self) -> SqlAlchemyStaffGameAccountOwnerCorrectionQueryRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyStaffGameAccountOwnerCorrectionQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffGameAccountOwnerCorrectionQueryUnitOfWork


class SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._repository: SqlAlchemyStaffGameAccountOwnerCorrectionRepository | None = None

    @property
    def staff_game_account_owner_correction(self) -> SqlAlchemyStaffGameAccountOwnerCorrectionRepository:
        return self._require_active_repository(self._repository)

    def _activate_repositories(self) -> None:
        self._repository = SqlAlchemyStaffGameAccountOwnerCorrectionRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._repository = None


class SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffGameAccountOwnerCorrectionUnitOfWork
