"""SQLAlchemy persistence for request-only native Account registration."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    AccountRegistrationAuditType,
    AccountRegistrationIdempotencyConflictError,
    AccountRegistrationRequester,
    AccountRegistrationRequestSnapshot,
    ActiveAccountRegistrationRequest,
    StoredAccountRegistrationOperation,
    SubmitAccountRegistrationRequest,
)
from uma_st2.domain.identity import GameRegion, RegistrationRequestStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    DiscordAccountORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def _sqlite_next_id(session: Session, column: object) -> int:
    """Supply deterministic IDs only for focused SQLite infrastructure tests."""

    value = session.scalar(select(func.coalesce(func.max(column), 0) + 1))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("SQLite test identity allocation failed.")
    return value


class SqlAlchemyAccountRegistrationRepository:
    """Lock one Discord requester and persist one pending request plus audit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_operation(self, *, idempotency_key: str) -> StoredAccountRegistrationOperation | None:
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
        return StoredAccountRegistrationOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            discord_user_id=row.discord_user_id,
            after_data=row.after_data,
        )

    def lock_or_create_requester(
        self,
        *,
        discord_user_id: str,
        created_at: datetime,
    ) -> AccountRegistrationRequester:
        stored_at = to_database_utc(created_at, field_name="created_at")
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

        requester = self._session.scalar(
            select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id).with_for_update()
        )
        if requester is None:
            raise RuntimeError("Discord requester was not created.")
        return AccountRegistrationRequester(
            discord_account_id=requester.id,
            discord_user_id=requester.discord_user_id,
            persona_id=requester.persona_id,
        )

    def find_active_request(
        self,
        *,
        guild_id: str,
        requester_discord_user_id: str,
    ) -> ActiveAccountRegistrationRequest | None:
        request_id = self._session.scalar(
            select(GameAccountRegistrationRequestORM.id)
            .where(
                GameAccountRegistrationRequestORM.guild_id == guild_id,
                GameAccountRegistrationRequestORM.requester_discord_user_id == requester_discord_user_id,
                GameAccountRegistrationRequestORM.active_marker.is_(True),
            )
            .with_for_update()
        )
        return None if request_id is None else ActiveAccountRegistrationRequest(request_id=request_id)

    def registered_game_account_exists(self, *, game_region: GameRegion, uma_pid: str) -> bool:
        account_id = self._session.scalar(
            select(GameAccountORM.id)
            .where(
                GameAccountORM.game_region == game_region.value,
                GameAccountORM.uma_pid == uma_pid,
            )
            .with_for_update()
        )
        return account_id is not None

    def create_request(
        self,
        *,
        command: SubmitAccountRegistrationRequest,
        requester: AccountRegistrationRequester,
        created_at: datetime,
    ) -> AccountRegistrationRequestSnapshot:
        request_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "requester_discord_user_id": command.actor_discord_user_id,
            "discord_display_name_snapshot": command.discord_display_name_snapshot,
            "game_region": command.game_region.value,
            "uma_pid": command.uma_pid,
            "nickname": command.nickname,
            "affiliation": command.affiliation,
            "status": RegistrationRequestStatus.PENDING.value,
            "active_marker": True,
            "reason": None,
            "created_at": to_database_utc(created_at, field_name="created_at"),
            "resolved_at": None,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            request_values["id"] = _sqlite_next_id(
                self._session,
                GameAccountRegistrationRequestORM.id,
            )
        request = GameAccountRegistrationRequestORM(**request_values)
        self._session.add(request)
        self._session.flush()
        return AccountRegistrationRequestSnapshot(
            request_id=request.id,
            discord_account_id=requester.discord_account_id,
            guild_id=request.guild_id,
            requester_discord_user_id=request.requester_discord_user_id,
            discord_display_name_snapshot=request.discord_display_name_snapshot,
            game_region=GameRegion(request.game_region),
            uma_pid=request.uma_pid,
            nickname=request.nickname,
            affiliation=request.affiliation,
            status=RegistrationRequestStatus(request.status),
            created_at=from_database_utc(
                request.created_at,
                field_name="GameAccountRegistrationRequest.created_at",
            ),
        )

    def add_request_audit(
        self,
        *,
        command: SubmitAccountRegistrationRequest,
        snapshot: AccountRegistrationRequestSnapshot,
        created_at: datetime,
    ) -> None:
        operation_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.actor_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": None,
            "created_at": to_database_utc(created_at, field_name="created_at"),
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = _sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise AccountRegistrationIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=None,
                game_account_id=None,
                discord_user_id=command.actor_discord_user_id,
                type=AccountRegistrationAuditType.REQUESTED.value,
                before_data=None,
                after_data=snapshot.to_audit_payload(),
            )
        )
        self._session.flush()


class SqlAlchemyAccountRegistrationUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW exposing native Account registration submission."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._account_registration: SqlAlchemyAccountRegistrationRepository | None = None

    @property
    def account_registration(self) -> SqlAlchemyAccountRegistrationRepository:
        return self._require_active_repository(self._account_registration)

    def _activate_repositories(self) -> None:
        self._account_registration = SqlAlchemyAccountRegistrationRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._account_registration = None


class SqlAlchemyAccountRegistrationUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyAccountRegistrationUnitOfWork]
):
    """Create one registration UoW per final Modal submission."""

    unit_of_work_type = SqlAlchemyAccountRegistrationUnitOfWork
