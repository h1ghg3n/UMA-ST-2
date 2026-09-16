"""Shared SQLAlchemy persistence for native account registration bootstrap."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from uma_st2.application.identity.registration_bootstrap import (
    ACCOUNT_REGISTRATION_INITIAL_GRANT,
    ACCOUNT_REGISTRATION_INITIAL_GRANT_POINT_ACTION,
    AccountRegistrationBootstrap,
    CreateAccountRegistrationBootstrap,
    RegistrationBootstrapAuditError,
    RegistrationBootstrapConcurrentConflictError,
    RegistrationBootstrapIdempotencyConflictError,
    RegistrationBootstrapPidUnavailableError,
)
from uma_st2.domain.identity import PersonaStatus

from .datetime_codec import to_database_utc
from .orm import (
    CirclePointORM,
    DiscordAccountORM,
    GameAccountORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)


def sqlite_next_id(session: Session, column: object) -> int:
    """Supply deterministic IDs only for focused SQLite infrastructure tests."""

    value = session.scalar(select(func.coalesce(func.max(column), 0) + 1))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("SQLite test identity allocation failed.")
    return value


def is_mariadb_deadlock(error: OperationalError) -> bool:
    arguments = getattr(error.orig, "args", ())
    return bool(arguments) and arguments[0] == 1213


class SqlAlchemyRegistrationBootstrapRepository:
    """Create the shared identity/wallet/grant graph without committing."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_registration_bootstrap(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
    ) -> AccountRegistrationBootstrap:
        stored_at = to_database_utc(command.registered_at, field_name="registered_at")
        target = self._session.scalar(
            select(DiscordAccountORM)
            .where(DiscordAccountORM.discord_user_id == command.target_discord_user_id)
            .with_for_update()
        )
        if target is None or target.persona_id is not None:
            raise RegistrationBootstrapAuditError("Locked Discord registration target is unavailable.")

        persona = PersonaORM(
            id=command.persona_id,
            display_name=command.persona_display_name,
            status=PersonaStatus.NORMAL.value,
            created_at=stored_at,
            updated_at=stored_at,
        )
        self._session.add(persona)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise RegistrationBootstrapAuditError("Generated Persona identity conflicts with current state.") from error

        target.persona_id = command.persona_id
        target.updated_at = stored_at
        game_values: dict[str, object] = {
            "persona_id": command.persona_id,
            "game_region": command.game_region.value,
            "uma_pid": command.uma_pid,
            "nickname": command.nickname,
            "affiliation": command.affiliation,
            "created_at": stored_at,
            "updated_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            game_values["id"] = sqlite_next_id(self._session, GameAccountORM.id)
        game_account = GameAccountORM(**game_values)
        self._session.add(game_account)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise RegistrationBootstrapPidUnavailableError("Registration PID conflicts with current state.") from error
        except OperationalError as error:
            if is_mariadb_deadlock(error):
                raise RegistrationBootstrapConcurrentConflictError("Concurrent registration bootstrap won.") from error
            raise

        wallet = CirclePointORM(
            persona_id=command.persona_id,
            balance=0,
            updated_at=stored_at,
        )
        self._session.add(wallet)
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            raise RegistrationBootstrapAuditError("Registration wallet creation failed.") from error

        operation_values: dict[str, object] = {
            "guild_id": command.guild_id,
            "correlation_id": command.correlation_id,
            "actor_discord_user_id": command.actor_discord_user_id,
            "idempotency_key": command.idempotency_key,
            "request_fingerprint": command.request_fingerprint,
            "reason": command.reason,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            operation_values["id"] = sqlite_next_id(self._session, OperationORM.id)
        operation = OperationORM(**operation_values)
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise RegistrationBootstrapIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error
        except OperationalError as error:
            if is_mariadb_deadlock(error):
                raise RegistrationBootstrapConcurrentConflictError("Concurrent registration bootstrap won.") from error
            raise

        wallet.balance = ACCOUNT_REGISTRATION_INITIAL_GRANT
        wallet.updated_at = stored_at
        point_values: dict[str, object] = {
            "persona_id": command.persona_id,
            "operation_id": operation.id,
            "action": ACCOUNT_REGISTRATION_INITIAL_GRANT_POINT_ACTION,
            "amount": ACCOUNT_REGISTRATION_INITIAL_GRANT,
            "created_at": stored_at,
        }
        if self._session.get_bind().dialect.name == "sqlite":
            point_values["id"] = sqlite_next_id(self._session, PointTransactionORM.id)
        self._session.add(PointTransactionORM(**point_values))
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            raise RegistrationBootstrapAuditError("Registration initial grant failed.") from error

        return AccountRegistrationBootstrap(
            operation_id=operation.id,
            persona_id=persona.id,
            persona_display_name=persona.display_name,
            persona_status=PersonaStatus(persona.status),
            target_discord_user_id=target.discord_user_id,
            game_account_id=game_account.id,
            game_region=command.game_region,
            uma_pid=game_account.uma_pid,
            nickname=game_account.nickname,
            affiliation=game_account.affiliation,
            wallet_balance=wallet.balance,
            initial_grant_amount=ACCOUNT_REGISTRATION_INITIAL_GRANT,
            registered_at=command.registered_at,
        )

    def complete_registration_bootstrap_audit(
        self,
        *,
        command: CreateAccountRegistrationBootstrap,
        bootstrap: AccountRegistrationBootstrap,
        before_data: Mapping[str, object] | None,
        after_data: Mapping[str, object],
    ) -> None:
        self._session.add(
            IdentityOperationORM(
                operation_id=bootstrap.operation_id,
                persona_id=bootstrap.persona_id,
                game_account_id=bootstrap.game_account_id,
                discord_user_id=bootstrap.target_discord_user_id,
                type=command.audit_type.value,
                before_data=before_data,
                after_data=after_data,
            )
        )
        try:
            self._session.flush()
        except (IntegrityError, OperationalError) as error:
            raise RegistrationBootstrapAuditError("Registration bootstrap audit failed.") from error
