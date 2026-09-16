"""SQLAlchemy delivery claims and finalization for supported publications."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    SUPPORTED_PUBLICATION_DELIVERY_ROUTES,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    ClaimedPublication,
    FinalizePublication,
    PublicationAwaitingPromotionInvalidSourceError,
    PublicationDeliveryRepository,
    PublicationPromotionDestination,
    PublicationUnknownReconciliationInvalidSourceError,
    PublicationUnknownResolution,
    StoredPublicationDelivery,
    UnknownPublicationDelivery,
)
from uma_st2.domain.publication import PublicationStatus

from .datetime_codec import from_database_utc, to_database_utc
from .orm import BotGuildSettingORM, DiscordPublicationORM
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)


def _supported_route_clause():
    return or_(
        *(
            and_(
                DiscordPublicationORM.destination_kind == destination_kind,
                DiscordPublicationORM.event_type == event_type,
            )
            for destination_kind, event_type in SUPPORTED_PUBLICATION_DELIVERY_ROUTES
        )
    )


class SqlAlchemyPublicationDeliveryRepository(PublicationDeliveryRepository):
    """Claim supported outbox rows and persist one bounded delivery state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim_next(
        self,
        *,
        claimed_at: datetime,
        retry_before: datetime,
        max_attempts: int,
    ) -> ClaimedPublication | None:
        stored_claimed_at = to_database_utc(claimed_at, field_name="claimed_at")
        stored_retry_before = to_database_utc(retry_before, field_name="retry_before")
        publication = self._session.scalar(
            select(DiscordPublicationORM)
            .where(
                _supported_route_clause(),
                DiscordPublicationORM.target_channel_id.is_not(None),
                or_(
                    DiscordPublicationORM.status == PublicationStatus.READY.value,
                    and_(
                        DiscordPublicationORM.status == PublicationStatus.FAILED.value,
                        DiscordPublicationORM.attempt_count < max_attempts,
                        DiscordPublicationORM.updated_at <= stored_retry_before,
                    ),
                ),
            )
            .order_by(DiscordPublicationORM.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if publication is None:
            return None

        publication.status = PublicationStatus.PENDING.value
        publication.attempt_count += 1
        publication.attempt_started_at = stored_claimed_at
        publication.last_error_code = None
        publication.failure_stage = None
        publication.updated_at = stored_claimed_at
        return ClaimedPublication(
            publication_id=publication.id,
            guild_id=publication.guild_id,
            destination_kind=publication.destination_kind,
            event_type=publication.event_type,
            target_channel_id=publication.target_channel_id,
            payload_json=deepcopy(publication.payload_json),
            payload_fingerprint=publication.payload_fingerprint,
            attempt_count=publication.attempt_count,
        )

    def lock_publication(
        self,
        *,
        publication_id: int,
    ) -> StoredPublicationDelivery | None:
        publication = self._session.scalar(
            select(DiscordPublicationORM)
            .where(
                DiscordPublicationORM.id == publication_id,
                _supported_route_clause(),
            )
            .with_for_update()
        )
        return None if publication is None else self._state(publication)

    def list_unknown(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> tuple[UnknownPublicationDelivery, ...]:
        publications = self._session.scalars(
            select(DiscordPublicationORM)
            .where(
                _supported_route_clause(),
                DiscordPublicationORM.guild_id == guild_id,
                DiscordPublicationORM.destination_kind == destination_kind,
                DiscordPublicationORM.status == PublicationStatus.DELIVERY_UNKNOWN.value,
            )
            .order_by(DiscordPublicationORM.id)
            .limit(max_count)
        ).all()
        return tuple(self._unknown_state(publication) for publication in publications)

    def lock_stale_pending(
        self,
        *,
        stale_before: datetime,
        limit: int,
    ) -> tuple[StoredPublicationDelivery, ...]:
        stored_stale_before = to_database_utc(stale_before, field_name="stale_before")
        publications = self._session.scalars(
            select(DiscordPublicationORM)
            .where(
                _supported_route_clause(),
                DiscordPublicationORM.status == PublicationStatus.PENDING.value,
                DiscordPublicationORM.attempt_started_at.is_not(None),
                DiscordPublicationORM.attempt_started_at <= stored_stale_before,
            )
            .order_by(DiscordPublicationORM.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        return tuple(self._state(publication) for publication in publications)

    def update_delivery(
        self,
        *,
        command: FinalizePublication,
        changed_at: datetime,
    ) -> None:
        publication = self._session.get(DiscordPublicationORM, command.publication_id)
        if publication is None:
            raise RuntimeError("Locked publication disappeared before finalization.")
        stored_changed_at = to_database_utc(changed_at, field_name="changed_at")
        publication.status = command.status.value
        publication.discord_message_id = command.discord_message_id
        publication.last_error_code = command.error_code
        publication.failure_stage = None if command.failure_stage is None else command.failure_stage.value
        publication.attempt_started_at = None
        publication.published_at = stored_changed_at if command.status == PublicationStatus.SENT else None
        publication.updated_at = stored_changed_at

    def lock_promotion_destination(
        self,
        *,
        guild_id: str,
        destination_kind: str,
    ) -> PublicationPromotionDestination | None:
        setting = self._session.scalar(
            select(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id).with_for_update()
        )
        if setting is None:
            return None
        if destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND:
            enabled = setting.win5_announcements_enabled
            channel_id = setting.win5_announcement_channel_id
        elif destination_kind == MATCH_ANNOUNCEMENT_DESTINATION_KIND:
            enabled = setting.match_announcements_enabled
            channel_id = setting.match_announcement_channel_id
        else:
            raise PublicationAwaitingPromotionInvalidSourceError(
                "Publication destination kind is unsupported by guild settings."
            )
        try:
            return PublicationPromotionDestination(
                guild_id=setting.guild_id,
                destination_kind=destination_kind,
                announcements_enabled=enabled,
                target_channel_id=channel_id,
            )
        except ValueError as exc:
            raise PublicationAwaitingPromotionInvalidSourceError(
                "Stored guild publication destination is malformed."
            ) from exc

    def promote_awaiting(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        target_channel_id: str,
        max_count: int,
        promoted_at: datetime,
    ) -> int:
        publications = self._session.scalars(
            select(DiscordPublicationORM)
            .where(
                _supported_route_clause(),
                DiscordPublicationORM.guild_id == guild_id,
                DiscordPublicationORM.destination_kind == destination_kind,
                DiscordPublicationORM.status == PublicationStatus.AWAITING_CHANNEL.value,
            )
            .order_by(DiscordPublicationORM.id)
            .limit(max_count)
            .with_for_update()
        ).all()
        for publication in publications:
            if (
                publication.target_channel_id is not None
                or publication.attempt_count != 0
                or publication.discord_message_id is not None
                or publication.last_error_code is not None
                or publication.failure_stage is not None
                or publication.attempt_started_at is not None
                or publication.published_at is not None
            ):
                raise PublicationAwaitingPromotionInvalidSourceError(
                    "Stored awaiting-channel publication has delivery-attempt evidence."
                )
        stored_promoted_at = to_database_utc(promoted_at, field_name="promoted_at")
        for publication in publications:
            publication.target_channel_id = target_channel_id
            publication.status = PublicationStatus.READY.value
            publication.updated_at = stored_promoted_at
        return len(publications)

    def lock_unknown(
        self,
        *,
        publication_id: int,
        guild_id: str,
        destination_kind: str,
    ) -> UnknownPublicationDelivery | None:
        publication = self._session.scalar(
            select(DiscordPublicationORM)
            .where(
                _supported_route_clause(),
                DiscordPublicationORM.id == publication_id,
                DiscordPublicationORM.guild_id == guild_id,
                DiscordPublicationORM.destination_kind == destination_kind,
                DiscordPublicationORM.status == PublicationStatus.DELIVERY_UNKNOWN.value,
            )
            .with_for_update()
        )
        return None if publication is None else self._unknown_state(publication)

    def reconcile_unknown(
        self,
        *,
        publication_id: int,
        resolution: PublicationUnknownResolution,
        discord_message_id: str | None,
        reconciled_at: datetime,
    ) -> None:
        publication = self._session.get(DiscordPublicationORM, publication_id)
        if publication is None:
            raise RuntimeError("Locked unknown publication disappeared before reconciliation.")
        stored_reconciled_at = to_database_utc(reconciled_at, field_name="reconciled_at")
        if resolution == PublicationUnknownResolution.CONFIRMED_SENT:
            publication.status = PublicationStatus.SENT.value
            publication.discord_message_id = discord_message_id
            publication.published_at = stored_reconciled_at
        elif resolution == PublicationUnknownResolution.RETRY_ZERO_SEND:
            publication.status = PublicationStatus.READY.value
            publication.discord_message_id = None
            publication.published_at = None
        else:
            raise RuntimeError("Unknown publication resolution is unsupported.")
        publication.last_error_code = None
        publication.failure_stage = None
        publication.attempt_started_at = None
        publication.updated_at = stored_reconciled_at

    @staticmethod
    def _state(publication: DiscordPublicationORM) -> StoredPublicationDelivery:
        return StoredPublicationDelivery(
            publication_id=publication.id,
            status=PublicationStatus(publication.status),
            attempt_count=publication.attempt_count,
        )

    @staticmethod
    def _unknown_state(publication: DiscordPublicationORM) -> UnknownPublicationDelivery:
        try:
            return UnknownPublicationDelivery(
                publication_id=publication.id,
                guild_id=publication.guild_id,
                destination_kind=publication.destination_kind,
                event_type=publication.event_type,
                event_key=publication.event_key,
                source_kind=publication.source_kind,
                source_id=publication.source_id,
                target_channel_id=publication.target_channel_id,
                payload_fingerprint=publication.payload_fingerprint,
                attempt_count=publication.attempt_count,
                discord_message_id=publication.discord_message_id,
                error_code=publication.last_error_code,
                failure_stage=publication.failure_stage,
                created_at=from_database_utc(
                    publication.created_at,
                    field_name="discord_publications.created_at",
                ),
                updated_at=from_database_utc(
                    publication.updated_at,
                    field_name="discord_publications.updated_at",
                ),
            )
        except (TypeError, ValueError) as exc:
            raise PublicationUnknownReconciliationInvalidSourceError(
                "Stored unknown publication delivery evidence is malformed."
            ) from exc


class SqlAlchemyPublicationDeliveryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete UoW exposing only the publication delivery repository."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._publication_delivery: SqlAlchemyPublicationDeliveryRepository | None = None

    @property
    def publication_delivery(self) -> SqlAlchemyPublicationDeliveryRepository:
        return self._require_active_repository(self._publication_delivery)

    def _activate_repositories(self) -> None:
        self._publication_delivery = SqlAlchemyPublicationDeliveryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._publication_delivery = None


class SqlAlchemyPublicationDeliveryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyPublicationDeliveryUnitOfWork]
):
    """Create one fresh delivery UoW per claim/finalize operation."""

    unit_of_work_type = SqlAlchemyPublicationDeliveryUnitOfWork
