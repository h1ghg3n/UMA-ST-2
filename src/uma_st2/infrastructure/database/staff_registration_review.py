"""SQLAlchemy staff registration-review queries and atomic mutations."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from uma_st2.application.identity import (
    AccountRegistrationAuditType,
    ApproveAccountRegistrationRequest,
    RegistrationRequestLocator,
    RegistrationReviewRequester,
    RejectAccountRegistrationRequest,
    RejectedAccountRegistration,
    StaffPersonaChoice,
    StaffPersonaContext,
    StaffPersonaPanel,
    StaffRegistrationRequestNotFoundError,
    StaffRegistrationRequestPage,
    StaffRegistrationRequestState,
    StaffRegistrationReviewAuditError,
    StaffRegistrationReviewIdempotencyConflictError,
    StoredRegistrationReviewOperation,
)
from uma_st2.domain.identity import GameRegion, PersonaStatus, RegistrationRequestStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import (
    DiscordAccountORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
)
from .registration_bootstrap import SqlAlchemyRegistrationBootstrapRepository
from .uow import SessionFactory, SqlAlchemyFeatureUnitOfWork, SqlAlchemyFeatureUnitOfWorkFactory


def _sqlite_next_id(session: Session, column: object) -> int:
    value = session.scalar(select(func.coalesce(func.max(column), 0) + 1))
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError("SQLite test identity allocation failed.")
    return value


def _request_state(
    row: GameAccountRegistrationRequestORM,
    *,
    discord_account_id: int,
) -> StaffRegistrationRequestState:
    return StaffRegistrationRequestState(
        request_id=row.id,
        discord_account_id=discord_account_id,
        guild_id=row.guild_id,
        requester_discord_user_id=row.requester_discord_user_id,
        discord_display_name_snapshot=row.discord_display_name_snapshot,
        game_region=GameRegion(row.game_region),
        uma_pid=row.uma_pid,
        nickname=row.nickname,
        affiliation=row.affiliation,
        status=RegistrationRequestStatus(row.status),
        active_marker=row.active_marker,
        reason=row.reason,
        created_at=from_database_utc(row.created_at, field_name="registration request created_at"),
        resolved_at=(
            None
            if row.resolved_at is None
            else from_database_utc(row.resolved_at, field_name="registration request resolved_at")
        ),
    )


class SqlAlchemyStaffRegistrationReviewQueryRepository:
    """Materialize detached staff panel and pending-request projections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_panel(self, *, guild_id: str, persona_id: str | None) -> StaffPersonaPanel:
        pending_count = self._session.scalar(
            select(func.count())
            .select_from(GameAccountRegistrationRequestORM)
            .where(
                GameAccountRegistrationRequestORM.guild_id == guild_id,
                GameAccountRegistrationRequestORM.status == RegistrationRequestStatus.PENDING.value,
                GameAccountRegistrationRequestORM.active_marker.is_(True),
            )
        )
        selected: StaffPersonaContext | None = None
        if persona_id is not None:
            row = self._session.execute(
                select(
                    PersonaORM.id,
                    PersonaORM.display_name,
                    PersonaORM.status,
                    func.count(GameAccountORM.id),
                )
                .outerjoin(GameAccountORM, GameAccountORM.persona_id == PersonaORM.id)
                .where(PersonaORM.id == persona_id)
                .group_by(PersonaORM.id, PersonaORM.display_name, PersonaORM.status)
            ).one_or_none()
            if row is None:
                raise StaffRegistrationRequestNotFoundError("Selected Persona does not exist.")
            selected = StaffPersonaContext(
                persona_id=row.id,
                display_name=row.display_name,
                status=PersonaStatus(row.status),
                game_account_count=int(row[3]),
            )
        return StaffPersonaPanel(
            pending_request_count=int(pending_count or 0),
            selected_persona=selected,
        )

    def search_personas(self, *, query: str, limit: int) -> tuple[StaffPersonaChoice, ...]:
        statement = select(PersonaORM.id, PersonaORM.display_name)
        if query:
            statement = statement.where(
                or_(
                    PersonaORM.id == query,
                    PersonaORM.display_name.contains(query, autoescape=True),
                )
            )
        rows = self._session.execute(
            statement.order_by(PersonaORM.display_name.asc(), PersonaORM.id.asc()).limit(limit)
        ).all()
        return tuple(StaffPersonaChoice(persona_id=row.id, display_name=row.display_name) for row in rows)

    def list_pending_requests(
        self,
        *,
        guild_id: str,
        page: int,
        offset: int,
        limit: int,
    ) -> StaffRegistrationRequestPage:
        predicates = (
            GameAccountRegistrationRequestORM.guild_id == guild_id,
            GameAccountRegistrationRequestORM.status == RegistrationRequestStatus.PENDING.value,
            GameAccountRegistrationRequestORM.active_marker.is_(True),
        )
        total_count = self._session.scalar(
            select(func.count()).select_from(GameAccountRegistrationRequestORM).where(*predicates)
        )
        rows = self._session.execute(
            select(GameAccountRegistrationRequestORM, DiscordAccountORM.id)
            .join(
                DiscordAccountORM,
                DiscordAccountORM.discord_user_id == GameAccountRegistrationRequestORM.requester_discord_user_id,
            )
            .where(*predicates)
            .order_by(
                GameAccountRegistrationRequestORM.created_at.asc(),
                GameAccountRegistrationRequestORM.id.asc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return StaffRegistrationRequestPage(
            page=page,
            total_count=int(total_count or 0),
            items=tuple(_request_state(row[0], discord_account_id=row[1]) for row in rows),
        )

    def get_pending_request(
        self,
        *,
        guild_id: str,
        request_id: int,
    ) -> StaffRegistrationRequestState | None:
        row = self._session.execute(
            select(GameAccountRegistrationRequestORM, DiscordAccountORM.id)
            .join(
                DiscordAccountORM,
                DiscordAccountORM.discord_user_id == GameAccountRegistrationRequestORM.requester_discord_user_id,
            )
            .where(
                GameAccountRegistrationRequestORM.id == request_id,
                GameAccountRegistrationRequestORM.guild_id == guild_id,
                GameAccountRegistrationRequestORM.status == RegistrationRequestStatus.PENDING.value,
                GameAccountRegistrationRequestORM.active_marker.is_(True),
            )
        ).one_or_none()
        return None if row is None else _request_state(row[0], discord_account_id=row[1])


class SqlAlchemyRegistrationReviewRepository(SqlAlchemyRegistrationBootstrapRepository):
    """Lock requester-first and persist one approval or rejection transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_request_locator(self, *, request_id: int) -> RegistrationRequestLocator | None:
        row = self._session.execute(
            select(
                GameAccountRegistrationRequestORM.id,
                GameAccountRegistrationRequestORM.guild_id,
                GameAccountRegistrationRequestORM.requester_discord_user_id,
            ).where(GameAccountRegistrationRequestORM.id == request_id)
        ).one_or_none()
        if row is None:
            return None
        return RegistrationRequestLocator(
            request_id=row.id,
            guild_id=row.guild_id,
            requester_discord_user_id=row.requester_discord_user_id,
        )

    def lock_requester(self, *, discord_user_id: str) -> RegistrationReviewRequester | None:
        row = self._session.scalar(
            select(DiscordAccountORM).where(DiscordAccountORM.discord_user_id == discord_user_id).with_for_update()
        )
        if row is None:
            return None
        return RegistrationReviewRequester(
            discord_account_id=row.id,
            discord_user_id=row.discord_user_id,
            persona_id=row.persona_id,
        )

    def lock_request(self, *, request_id: int) -> StaffRegistrationRequestState | None:
        row = self._session.scalar(
            select(GameAccountRegistrationRequestORM)
            .where(GameAccountRegistrationRequestORM.id == request_id)
            .with_for_update()
        )
        if row is None:
            return None
        discord_account_id = self._session.scalar(
            select(DiscordAccountORM.id).where(DiscordAccountORM.discord_user_id == row.requester_discord_user_id)
        )
        if discord_account_id is None:
            raise StaffRegistrationReviewAuditError("Registration request has no DiscordAccount row.")
        return _request_state(row, discord_account_id=discord_account_id)

    def find_operation(self, *, idempotency_key: str) -> StoredRegistrationReviewOperation | None:
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
        return StoredRegistrationReviewOperation(
            request_fingerprint=row.request_fingerprint,
            type=row.type,
            persona_id=row.persona_id,
            game_account_id=row.game_account_id,
            discord_user_id=row.discord_user_id,
            after_data=row.after_data,
        )

    def registered_game_account_exists(self, *, game_region: GameRegion, uma_pid: str) -> bool:
        # This is a current-state preflight, not the concurrency authority. A
        # missing-key FOR UPDATE read gives concurrent approvals shared gap
        # locks and makes both later inserts deadlock. The canonical unique
        # constraint owns PID correctness. approve_request translates a
        # duplicate loser to PID-unavailable and a deadlock victim to an
        # explicit concurrent-review conflict; both roll back completely.
        return (
            self._session.scalar(
                select(GameAccountORM.id).where(
                    GameAccountORM.game_region == game_region.value,
                    GameAccountORM.uma_pid == uma_pid,
                )
            )
            is not None
        )

    def resolve_approved_request(
        self,
        *,
        command: ApproveAccountRegistrationRequest,
        request: StaffRegistrationRequestState,
        requester: RegistrationReviewRequester,
        resolved_at: datetime,
    ) -> None:
        stored_at = to_database_utc(resolved_at, field_name="resolved_at")
        request_row = self._session.get(GameAccountRegistrationRequestORM, request.request_id)
        requester_row = self._session.get(DiscordAccountORM, requester.discord_account_id)
        if request_row is None or requester_row is None:
            raise StaffRegistrationReviewAuditError("Locked approval authority disappeared.")
        request_row.status = RegistrationRequestStatus.APPROVED.value
        request_row.active_marker = None
        request_row.reason = command.review_note
        request_row.resolved_at = stored_at
        self._session.flush()

    def reject_request(
        self,
        *,
        command: RejectAccountRegistrationRequest,
        request: StaffRegistrationRequestState,
        requester: RegistrationReviewRequester,
        resolved_at: datetime,
    ) -> RejectedAccountRegistration:
        stored_at = to_database_utc(resolved_at, field_name="resolved_at")
        request_row = self._session.get(GameAccountRegistrationRequestORM, request.request_id)
        if request_row is None:
            raise StaffRegistrationReviewAuditError("Locked rejection authority disappeared.")
        operation = self._new_operation(
            guild_id=command.guild_id,
            correlation_id=command.correlation_id,
            actor_discord_user_id=command.reviewed_by_discord_user_id,
            idempotency_key=command.idempotency_key,
            request_fingerprint=command.request_fingerprint,
            reason=command.reason,
            created_at=stored_at,
        )
        self._session.add(operation)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise StaffRegistrationReviewIdempotencyConflictError(
                "Idempotency key is already bound to another logical operation."
            ) from error

        request_row.status = RegistrationRequestStatus.CANCELLED.value
        request_row.active_marker = None
        request_row.reason = command.reason
        request_row.resolved_at = stored_at
        result = RejectedAccountRegistration(
            request_id=request.request_id,
            requester_discord_user_id=request.requester_discord_user_id,
            reason=command.reason,
            resolved_at=resolved_at,
        )
        self._session.add(
            IdentityOperationORM(
                operation_id=operation.id,
                persona_id=None,
                game_account_id=None,
                discord_user_id=request.requester_discord_user_id,
                type=AccountRegistrationAuditType.REJECTED.value,
                before_data=request.to_audit_payload(),
                after_data=result.to_audit_payload(),
            )
        )
        self._session.flush()
        return result

    def _new_operation(
        self,
        *,
        guild_id: str,
        correlation_id: str | None,
        actor_discord_user_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        reason: str | None,
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
        return OperationORM(**values)


class SqlAlchemyStaffRegistrationReviewQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._staff_registration_review: SqlAlchemyStaffRegistrationReviewQueryRepository | None = None

    @property
    def staff_registration_review(self) -> SqlAlchemyStaffRegistrationReviewQueryRepository:
        return self._require_active_repository(self._staff_registration_review)

    def _activate_repositories(self) -> None:
        self._staff_registration_review = SqlAlchemyStaffRegistrationReviewQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._staff_registration_review = None


class SqlAlchemyStaffRegistrationReviewQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyStaffRegistrationReviewQueryUnitOfWork]
):
    unit_of_work_type = SqlAlchemyStaffRegistrationReviewQueryUnitOfWork


class SqlAlchemyRegistrationReviewUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._registration_review: SqlAlchemyRegistrationReviewRepository | None = None

    @property
    def registration_review(self) -> SqlAlchemyRegistrationReviewRepository:
        return self._require_active_repository(self._registration_review)

    def _activate_repositories(self) -> None:
        self._registration_review = SqlAlchemyRegistrationReviewRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._registration_review = None


class SqlAlchemyRegistrationReviewUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyRegistrationReviewUnitOfWork]
):
    unit_of_work_type = SqlAlchemyRegistrationReviewUnitOfWork
