"""Post-commit delivery state machine for supported stored publications."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, QueryRunner, UnitOfWork
from uma_st2.domain.publication import PublicationStatus
from uma_st2.shared import normalize_utc_datetime

from .match import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETS_REFUNDED_EVENT_TYPE,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
)
from .match_close import MATCH_BETTING_CLOSED_EVENT_TYPE
from .match_odds import MATCH_ODDS_REFRESH_EVENT_TYPE
from .win5 import (
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_HALL_OF_FAME_EVENT_TYPE,
    WIN5_ROUND_RESULT_EVENT_TYPE,
)

SUPPORTED_PUBLICATION_DELIVERY_ROUTES = (
    (WIN5_ANNOUNCEMENT_DESTINATION_KIND, WIN5_ROUND_RESULT_EVENT_TYPE),
    (WIN5_ANNOUNCEMENT_DESTINATION_KIND, WIN5_HALL_OF_FAME_EVENT_TYPE),
    (MATCH_ANNOUNCEMENT_DESTINATION_KIND, MATCH_BETTING_OPENED_EVENT_TYPE),
    (MATCH_ANNOUNCEMENT_DESTINATION_KIND, MATCH_ODDS_REFRESH_EVENT_TYPE),
    (MATCH_ANNOUNCEMENT_DESTINATION_KIND, MATCH_BETTING_CLOSED_EVENT_TYPE),
    (MATCH_ANNOUNCEMENT_DESTINATION_KIND, MATCH_BETS_REFUNDED_EVENT_TYPE),
    (MATCH_ANNOUNCEMENT_DESTINATION_KIND, MATCH_RESULT_CONFIRMED_EVENT_TYPE),
    (MATCH_ANNOUNCEMENT_DESTINATION_KIND, MATCH_SETTLEMENT_VOIDED_EVENT_TYPE),
)
SUPPORTED_PUBLICATION_DESTINATION_KINDS: Final = frozenset(
    destination_kind for destination_kind, _ in SUPPORTED_PUBLICATION_DELIVERY_ROUTES
)
PUBLICATION_AWAITING_PROMOTION_MAX_COUNT: Final = 1000
PUBLICATION_UNKNOWN_LIST_MAX_COUNT: Final = 1000
PUBLICATION_STALE_DELIVERY_ERROR_CODE = "worker_lease_expired"


class PublicationDeliveryError(ValueError):
    """Base error for a rejected publication delivery transition."""


class PublicationDeliveryUnavailableError(PublicationDeliveryError):
    """The target publication is missing or not owned by this delivery capability."""


class PublicationDeliveryLeaseConflictError(PublicationDeliveryError):
    """A finalizer no longer owns the current pending attempt."""


class PublicationAwaitingPromotionUnavailableError(PublicationDeliveryError):
    """The current guild setting cannot authorize awaiting-channel promotion."""


class PublicationAwaitingPromotionInvalidSourceError(PublicationDeliveryError):
    """Stored awaiting-channel evidence is not safe to promote."""


class PublicationUnknownReconciliationUnavailableError(PublicationDeliveryError):
    """The exact supported unknown publication is not available."""


class PublicationUnknownReconciliationConflictError(PublicationDeliveryError):
    """The inspected unknown evidence no longer matches the request."""


class PublicationUnknownReconciliationInvalidSourceError(PublicationDeliveryError):
    """Stored unknown delivery evidence is malformed."""


class PublicationFailureStage(StrEnum):
    """Bounded durable stage vocabulary for a delivery failure."""

    RENDER = "render"
    CHANNEL = "channel"
    SEND = "send"
    WORKER = "worker"


class PublicationUnknownResolution(StrEnum):
    """Operator-confirmed disposition for one unknown delivery attempt."""

    CONFIRMED_SENT = "confirmed_sent"
    RETRY_ZERO_SEND = "retry_zero_send"


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _require_positive_duration(value: timedelta, *, field_name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{field_name} must be a positive duration.")


def _require_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        qualifier = "optional " if optional else ""
        raise ValueError(f"{field_name} must be a non-empty {qualifier}string no longer than {max_length} characters.")


def _require_snowflake(value: str | None, *, field_name: str, optional: bool = False) -> None:
    if value is None and optional:
        return
    _require_bounded_string(value, field_name=field_name, max_length=32)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:  # type: ignore[union-attr]
        raise ValueError(f"{field_name} must be a positive decimal Discord snowflake.")


@dataclass(frozen=True, slots=True)
class ClaimedPublication:
    """Closed-session stored snapshot owned by one pending delivery attempt."""

    publication_id: int
    guild_id: str
    destination_kind: str
    event_type: str
    target_channel_id: str
    payload_json: Mapping[str, object]
    payload_fingerprint: str
    attempt_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        _require_bounded_string(self.event_type, field_name="event_type", max_length=64)
        if (self.destination_kind, self.event_type) not in SUPPORTED_PUBLICATION_DELIVERY_ROUTES:
            raise ValueError("Publication delivery route is unsupported.")
        _require_snowflake(self.target_channel_id, field_name="target_channel_id")
        if not isinstance(self.payload_json, Mapping):
            raise ValueError("payload_json must be a mapping.")
        _require_bounded_string(
            self.payload_fingerprint,
            field_name="payload_fingerprint",
            max_length=64,
        )
        _require_positive_int(self.attempt_count, field_name="attempt_count")


@dataclass(frozen=True, slots=True)
class StoredPublicationDelivery:
    """Current locked state used to authorize one finalizer."""

    publication_id: int
    status: PublicationStatus
    attempt_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        object.__setattr__(self, "status", PublicationStatus(self.status))
        _require_non_negative_int(self.attempt_count, field_name="attempt_count")


@dataclass(frozen=True, slots=True)
class PublicationPromotionDestination:
    """Current canonical guild setting used by one explicit promotion batch."""

    guild_id: str
    destination_kind: str
    announcements_enabled: bool
    target_channel_id: str | None

    def __post_init__(self) -> None:
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise ValueError("Publication destination kind is unsupported.")
        if not isinstance(self.announcements_enabled, bool):
            raise ValueError("announcements_enabled must be a boolean.")
        _require_snowflake(
            self.target_channel_id,
            field_name="target_channel_id",
            optional=True,
        )


@dataclass(frozen=True, slots=True)
class PromotedAwaitingPublications:
    """Closed-session receipt for one bounded awaiting-channel promotion."""

    guild_id: str
    destination_kind: str
    target_channel_id: str
    promoted_count: int
    promoted_at: datetime

    def __post_init__(self) -> None:
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise ValueError("Publication destination kind is unsupported.")
        _require_snowflake(self.target_channel_id, field_name="target_channel_id")
        _require_non_negative_int(self.promoted_count, field_name="promoted_count")
        object.__setattr__(
            self,
            "promoted_at",
            normalize_utc_datetime(self.promoted_at, field_name="promoted_at"),
        )


@dataclass(frozen=True, slots=True)
class UnknownPublicationDelivery:
    """Detached technical evidence for one unknown provider delivery."""

    publication_id: int
    guild_id: str
    destination_kind: str
    event_type: str
    event_key: str
    source_kind: str
    source_id: int
    target_channel_id: str
    payload_fingerprint: str
    attempt_count: int
    discord_message_id: str | None
    error_code: str
    failure_stage: PublicationFailureStage
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        _require_bounded_string(self.event_type, field_name="event_type", max_length=64)
        if (self.destination_kind, self.event_type) not in SUPPORTED_PUBLICATION_DELIVERY_ROUTES:
            raise ValueError("Publication delivery route is unsupported.")
        _require_bounded_string(self.event_key, field_name="event_key", max_length=128)
        _require_bounded_string(self.source_kind, field_name="source_kind", max_length=32)
        _require_positive_int(self.source_id, field_name="source_id")
        _require_snowflake(self.target_channel_id, field_name="target_channel_id")
        _require_bounded_string(
            self.payload_fingerprint,
            field_name="payload_fingerprint",
            max_length=64,
        )
        _require_positive_int(self.attempt_count, field_name="attempt_count")
        _require_snowflake(
            self.discord_message_id,
            field_name="discord_message_id",
            optional=True,
        )
        _require_bounded_string(self.error_code, field_name="error_code", max_length=64)
        object.__setattr__(self, "failure_stage", PublicationFailureStage(self.failure_stage))
        object.__setattr__(
            self,
            "created_at",
            normalize_utc_datetime(self.created_at, field_name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            normalize_utc_datetime(self.updated_at, field_name="updated_at"),
        )


@dataclass(frozen=True, slots=True)
class ReconcileUnknownPublication:
    """Exact operator judgement for one inspected unknown attempt."""

    publication_id: int
    guild_id: str
    destination_kind: str
    expected_attempt_count: int
    resolution: PublicationUnknownResolution
    discord_message_id: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise ValueError("Publication destination kind is unsupported.")
        _require_positive_int(self.expected_attempt_count, field_name="expected_attempt_count")
        object.__setattr__(self, "resolution", PublicationUnknownResolution(self.resolution))
        _require_snowflake(
            self.discord_message_id,
            field_name="discord_message_id",
            optional=True,
        )
        if self.resolution == PublicationUnknownResolution.CONFIRMED_SENT:
            if self.discord_message_id is None:
                raise ValueError("Confirmed sent reconciliation requires a Discord message ID.")
        elif self.discord_message_id is not None:
            raise ValueError("Zero-send retry reconciliation must not provide a Discord message ID.")


@dataclass(frozen=True, slots=True)
class ReconciledUnknownPublication:
    """Closed-session receipt for one operator-confirmed unknown resolution."""

    publication_id: int
    guild_id: str
    destination_kind: str
    attempt_count: int
    resolution: PublicationUnknownResolution
    status: PublicationStatus
    discord_message_id: str | None
    reconciled_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        _require_snowflake(self.guild_id, field_name="guild_id")
        _require_bounded_string(self.destination_kind, field_name="destination_kind", max_length=32)
        if self.destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise ValueError("Publication destination kind is unsupported.")
        _require_positive_int(self.attempt_count, field_name="attempt_count")
        object.__setattr__(self, "resolution", PublicationUnknownResolution(self.resolution))
        object.__setattr__(self, "status", PublicationStatus(self.status))
        _require_snowflake(
            self.discord_message_id,
            field_name="discord_message_id",
            optional=True,
        )
        if self.resolution == PublicationUnknownResolution.CONFIRMED_SENT:
            if self.status != PublicationStatus.SENT or self.discord_message_id is None:
                raise ValueError("Confirmed sent receipt requires sent status and an anchor.")
        elif self.status != PublicationStatus.READY or self.discord_message_id is not None:
            raise ValueError("Zero-send retry receipt requires ready status without an anchor.")
        object.__setattr__(
            self,
            "reconciled_at",
            normalize_utc_datetime(self.reconciled_at, field_name="reconciled_at"),
        )


@dataclass(frozen=True, slots=True)
class FinalizePublication:
    """Final state for one claimed provider attempt."""

    publication_id: int
    attempt_count: int
    status: PublicationStatus
    discord_message_id: str | None = None
    error_code: str | None = None
    failure_stage: PublicationFailureStage | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        _require_positive_int(self.attempt_count, field_name="attempt_count")
        object.__setattr__(self, "status", PublicationStatus(self.status))
        if self.failure_stage is not None:
            object.__setattr__(self, "failure_stage", PublicationFailureStage(self.failure_stage))
        _require_snowflake(
            self.discord_message_id,
            field_name="discord_message_id",
            optional=True,
        )
        _require_bounded_string(
            self.error_code,
            field_name="error_code",
            max_length=64,
            optional=True,
        )

        if self.status == PublicationStatus.SENT:
            if self.discord_message_id is None or self.error_code is not None or self.failure_stage is not None:
                raise ValueError("A sent delivery requires only its anchor Discord message ID.")
            return
        if self.status == PublicationStatus.FAILED:
            if self.discord_message_id is not None or self.error_code is None or self.failure_stage is None:
                raise ValueError("A failed delivery must be a known zero-send failure with error metadata.")
            return
        if self.status == PublicationStatus.DELIVERY_UNKNOWN:
            if self.error_code is None or self.failure_stage is None:
                raise ValueError("An unknown delivery requires bounded error metadata.")
            return
        raise ValueError("A delivery may finalize only as sent, failed, or delivery_unknown.")


@dataclass(frozen=True, slots=True)
class FinalizedPublication:
    """Committed terminal delivery state returned to the worker."""

    publication_id: int
    attempt_count: int
    status: PublicationStatus
    discord_message_id: str | None
    finalized_at: datetime

    def __post_init__(self) -> None:
        _require_positive_int(self.publication_id, field_name="publication_id")
        _require_positive_int(self.attempt_count, field_name="attempt_count")
        object.__setattr__(self, "status", PublicationStatus(self.status))
        if self.status not in {
            PublicationStatus.SENT,
            PublicationStatus.FAILED,
            PublicationStatus.DELIVERY_UNKNOWN,
        }:
            raise ValueError("Finalized publication has a non-terminal delivery status.")
        _require_snowflake(
            self.discord_message_id,
            field_name="discord_message_id",
            optional=True,
        )
        object.__setattr__(
            self,
            "finalized_at",
            normalize_utc_datetime(self.finalized_at, field_name="finalized_at"),
        )


class PublicationDeliveryQueryRepository(Protocol):
    """Read-only technical evidence for manual delivery reconciliation."""

    def list_unknown(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> tuple[UnknownPublicationDelivery, ...]: ...


class PublicationDeliveryRepository(PublicationDeliveryQueryRepository, Protocol):
    """Persistence operations required by delivery and reconciliation commands."""

    def claim_next(
        self,
        *,
        claimed_at: datetime,
        retry_before: datetime,
        max_attempts: int,
    ) -> ClaimedPublication | None: ...

    def lock_publication(
        self,
        *,
        publication_id: int,
    ) -> StoredPublicationDelivery | None: ...

    def lock_stale_pending(
        self,
        *,
        stale_before: datetime,
        limit: int,
    ) -> tuple[StoredPublicationDelivery, ...]: ...

    def update_delivery(
        self,
        *,
        command: FinalizePublication,
        changed_at: datetime,
    ) -> None: ...

    def lock_promotion_destination(
        self,
        *,
        guild_id: str,
        destination_kind: str,
    ) -> PublicationPromotionDestination | None: ...

    def promote_awaiting(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        target_channel_id: str,
        max_count: int,
        promoted_at: datetime,
    ) -> int: ...

    def lock_unknown(
        self,
        *,
        publication_id: int,
        guild_id: str,
        destination_kind: str,
    ) -> UnknownPublicationDelivery | None: ...

    def reconcile_unknown(
        self,
        *,
        publication_id: int,
        resolution: PublicationUnknownResolution,
        discord_message_id: str | None,
        reconciled_at: datetime,
    ) -> None: ...


class PublicationDeliveryQueryUnitOfWork(UnitOfWork, Protocol):
    """Read-only UoW exposing publication reconciliation evidence."""

    @property
    def publication_delivery(self) -> PublicationDeliveryQueryRepository: ...


class PublicationDeliveryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the publication delivery repository."""

    @property
    def publication_delivery(self) -> PublicationDeliveryRepository: ...


@dataclass(frozen=True, slots=True)
class PublicationDeliveryQueries:
    """Application entry point for bounded unknown-delivery inspection."""

    query_runner: QueryRunner[PublicationDeliveryQueryUnitOfWork]

    def list_unknown(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> tuple[UnknownPublicationDelivery, ...]:
        _require_snowflake(guild_id, field_name="guild_id")
        _require_bounded_string(destination_kind, field_name="destination_kind", max_length=32)
        if destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise ValueError("Publication destination kind is unsupported.")
        _require_positive_int(max_count, field_name="max_count")
        if max_count > PUBLICATION_UNKNOWN_LIST_MAX_COUNT:
            raise ValueError(f"max_count must not exceed {PUBLICATION_UNKNOWN_LIST_MAX_COUNT}.")

        def query(
            unit_of_work: PublicationDeliveryQueryUnitOfWork,
        ) -> tuple[UnknownPublicationDelivery, ...]:
            rows = unit_of_work.publication_delivery.list_unknown(
                guild_id=guild_id,
                destination_kind=destination_kind,
                max_count=max_count,
            )
            if len(rows) > max_count:
                raise PublicationUnknownReconciliationInvalidSourceError(
                    "Unknown publication query exceeded its requested bound."
                )
            if any(row.guild_id != guild_id or row.destination_kind != destination_kind for row in rows):
                raise PublicationUnknownReconciliationInvalidSourceError(
                    "Unknown publication query returned a different context."
                )
            publication_ids = tuple(row.publication_id for row in rows)
            if publication_ids != tuple(sorted(set(publication_ids))):
                raise PublicationUnknownReconciliationInvalidSourceError(
                    "Unknown publication query ordering or identity is malformed."
                )
            return rows

        return self.query_runner.run(query)


@dataclass(frozen=True, slots=True)
class PublicationDeliveryCommands:
    """Application entry point for claim, recovery, and finalization."""

    command_runner: CommandRunner[PublicationDeliveryUnitOfWork]
    clock: Callable[[], datetime]

    def claim_next(
        self,
        *,
        retry_delay: timedelta,
        max_attempts: int,
    ) -> ClaimedPublication | None:
        _require_positive_duration(retry_delay, field_name="retry_delay")
        _require_positive_int(max_attempts, field_name="max_attempts")
        claimed_at = self._now()
        return self.command_runner.run(
            lambda unit_of_work: unit_of_work.publication_delivery.claim_next(
                claimed_at=claimed_at,
                retry_before=claimed_at - retry_delay,
                max_attempts=max_attempts,
            )
        )

    def recover_stale_pending(
        self,
        *,
        pending_timeout: timedelta,
        max_count: int,
    ) -> int:
        _require_positive_duration(pending_timeout, field_name="pending_timeout")
        _require_positive_int(max_count, field_name="max_count")
        recovered_at = self._now()

        def recover(unit_of_work: PublicationDeliveryUnitOfWork) -> int:
            repository = unit_of_work.publication_delivery
            stale = repository.lock_stale_pending(
                stale_before=recovered_at - pending_timeout,
                limit=max_count,
            )
            for publication in stale:
                repository.update_delivery(
                    command=FinalizePublication(
                        publication_id=publication.publication_id,
                        attempt_count=publication.attempt_count,
                        status=PublicationStatus.DELIVERY_UNKNOWN,
                        error_code=PUBLICATION_STALE_DELIVERY_ERROR_CODE,
                        failure_stage=PublicationFailureStage.WORKER,
                    ),
                    changed_at=recovered_at,
                )
            return len(stale)

        return self.command_runner.run(recover)

    def promote_awaiting(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> PromotedAwaitingPublications:
        _require_snowflake(guild_id, field_name="guild_id")
        _require_bounded_string(destination_kind, field_name="destination_kind", max_length=32)
        if destination_kind not in SUPPORTED_PUBLICATION_DESTINATION_KINDS:
            raise ValueError("Publication destination kind is unsupported.")
        _require_positive_int(max_count, field_name="max_count")
        if max_count > PUBLICATION_AWAITING_PROMOTION_MAX_COUNT:
            raise ValueError(f"max_count must not exceed {PUBLICATION_AWAITING_PROMOTION_MAX_COUNT}.")
        promoted_at = self._now()

        def promote(unit_of_work: PublicationDeliveryUnitOfWork) -> PromotedAwaitingPublications:
            repository = unit_of_work.publication_delivery
            destination = repository.lock_promotion_destination(
                guild_id=guild_id,
                destination_kind=destination_kind,
            )
            if destination is None or not destination.announcements_enabled or destination.target_channel_id is None:
                raise PublicationAwaitingPromotionUnavailableError(
                    "Current guild settings do not provide an enabled publication destination."
                )
            promoted_count = repository.promote_awaiting(
                guild_id=guild_id,
                destination_kind=destination_kind,
                target_channel_id=destination.target_channel_id,
                max_count=max_count,
                promoted_at=promoted_at,
            )
            if not 0 <= promoted_count <= max_count:
                raise PublicationAwaitingPromotionInvalidSourceError(
                    "Awaiting-channel promotion returned an invalid batch count."
                )
            return PromotedAwaitingPublications(
                guild_id=guild_id,
                destination_kind=destination_kind,
                target_channel_id=destination.target_channel_id,
                promoted_count=promoted_count,
                promoted_at=promoted_at,
            )

        return self.command_runner.run(promote)

    def reconcile_unknown(
        self,
        command: ReconcileUnknownPublication,
    ) -> ReconciledUnknownPublication:
        reconciled_at = self._now()

        def reconcile(unit_of_work: PublicationDeliveryUnitOfWork) -> ReconciledUnknownPublication:
            repository = unit_of_work.publication_delivery
            target = repository.lock_unknown(
                publication_id=command.publication_id,
                guild_id=command.guild_id,
                destination_kind=command.destination_kind,
            )
            if target is None:
                raise PublicationUnknownReconciliationUnavailableError(
                    "Exact supported unknown publication does not exist."
                )
            if (
                target.publication_id != command.publication_id
                or target.guild_id != command.guild_id
                or target.destination_kind != command.destination_kind
            ):
                raise PublicationUnknownReconciliationInvalidSourceError(
                    "Locked unknown publication identity does not match the request."
                )
            if target.attempt_count != command.expected_attempt_count:
                raise PublicationUnknownReconciliationConflictError(
                    "Unknown publication attempt changed after inspection."
                )
            if command.resolution == PublicationUnknownResolution.CONFIRMED_SENT:
                if target.discord_message_id is not None and target.discord_message_id != command.discord_message_id:
                    raise PublicationUnknownReconciliationConflictError(
                        "Confirmed message ID does not match the stored first-message anchor."
                    )
                status = PublicationStatus.SENT
                message_id = command.discord_message_id
            else:
                if target.discord_message_id is not None:
                    raise PublicationUnknownReconciliationConflictError(
                        "An anchor-bearing unknown publication cannot be retried as zero-send."
                    )
                status = PublicationStatus.READY
                message_id = None
            repository.reconcile_unknown(
                publication_id=target.publication_id,
                resolution=command.resolution,
                discord_message_id=message_id,
                reconciled_at=reconciled_at,
            )
            return ReconciledUnknownPublication(
                publication_id=target.publication_id,
                guild_id=target.guild_id,
                destination_kind=target.destination_kind,
                attempt_count=target.attempt_count,
                resolution=command.resolution,
                status=status,
                discord_message_id=message_id,
                reconciled_at=reconciled_at,
            )

        return self.command_runner.run(reconcile)

    def finalize(self, command: FinalizePublication) -> FinalizedPublication:
        finalized_at = self._now()

        def finalize(unit_of_work: PublicationDeliveryUnitOfWork) -> FinalizedPublication:
            repository = unit_of_work.publication_delivery
            current = repository.lock_publication(publication_id=command.publication_id)
            if current is None:
                raise PublicationDeliveryUnavailableError("Supported publication does not exist.")
            if current.status != PublicationStatus.PENDING or current.attempt_count != command.attempt_count:
                raise PublicationDeliveryLeaseConflictError(
                    "Publication delivery attempt is no longer owned by this finalizer."
                )
            repository.update_delivery(command=command, changed_at=finalized_at)
            return FinalizedPublication(
                publication_id=command.publication_id,
                attempt_count=command.attempt_count,
                status=command.status,
                discord_message_id=command.discord_message_id,
                finalized_at=finalized_at,
            )

        return self.command_runner.run(finalize)

    def _now(self) -> datetime:
        return normalize_utc_datetime(self.clock(), field_name="clock result")
