"""Application-boundary tests for WIN5 publication delivery state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_CLOSED_EVENT_TYPE,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
    PUBLICATION_AWAITING_PROMOTION_MAX_COUNT,
    PUBLICATION_STALE_DELIVERY_ERROR_CODE,
    PUBLICATION_UNKNOWN_LIST_MAX_COUNT,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_ROUND_RESULT_EVENT_TYPE,
    ClaimedPublication,
    FinalizePublication,
    PublicationAwaitingPromotionInvalidSourceError,
    PublicationAwaitingPromotionUnavailableError,
    PublicationDeliveryCommands,
    PublicationDeliveryError,
    PublicationDeliveryLeaseConflictError,
    PublicationDeliveryQueries,
    PublicationFailureStage,
    PublicationPromotionDestination,
    PublicationUnknownReconciliationConflictError,
    PublicationUnknownReconciliationInvalidSourceError,
    PublicationUnknownReconciliationUnavailableError,
    PublicationUnknownResolution,
    ReconcileUnknownPublication,
    StoredPublicationDelivery,
    UnknownPublicationDelivery,
)
from uma_st2.domain.publication import PublicationStatus

NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)


def _claimed(*, attempt_count: int = 1) -> ClaimedPublication:
    return ClaimedPublication(
        publication_id=41,
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=WIN5_ROUND_RESULT_EVENT_TYPE,
        target_channel_id="123456789",
        payload_json={"schema_version": 1},
        payload_fingerprint="a" * 64,
        attempt_count=attempt_count,
    )


def _unknown(*, anchor_message_id: str | None = None, attempt_count: int = 2) -> UnknownPublicationDelivery:
    return UnknownPublicationDelivery(
        publication_id=51,
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=WIN5_ROUND_RESULT_EVENT_TYPE,
        event_key="win5-round:81:result",
        source_kind="win5_round",
        source_id=81,
        target_channel_id="123456789",
        payload_fingerprint="b" * 64,
        attempt_count=attempt_count,
        discord_message_id=anchor_message_id,
        error_code="discord_send_timeout",
        failure_stage=PublicationFailureStage.SEND,
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.claimed: ClaimedPublication | None = _claimed()
        self.current: StoredPublicationDelivery | None = StoredPublicationDelivery(
            publication_id=41,
            status=PublicationStatus.PENDING,
            attempt_count=1,
        )
        self.stale: tuple[StoredPublicationDelivery, ...] = ()
        self.claim_arguments: list[tuple[datetime, datetime, int]] = []
        self.locked_ids: list[int] = []
        self.stale_arguments: list[tuple[datetime, int]] = []
        self.updates: list[tuple[FinalizePublication, datetime]] = []
        self.promotion_destination: PublicationPromotionDestination | None = PublicationPromotionDestination(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            announcements_enabled=True,
            target_channel_id="123456789",
        )
        self.promotion_destination_arguments: list[tuple[str, str]] = []
        self.promotion_count = 2
        self.promotion_arguments: list[tuple[str, str, str, int, datetime]] = []
        self.unknown_rows: tuple[UnknownPublicationDelivery, ...] = (_unknown(),)
        self.unknown_target: UnknownPublicationDelivery | None = _unknown()
        self.unknown_list_arguments: list[tuple[str, str, int]] = []
        self.unknown_lock_arguments: list[tuple[int, str, str]] = []
        self.unknown_reconciliations: list[tuple[int, PublicationUnknownResolution, str | None, datetime]] = []

    def claim_next(
        self,
        *,
        claimed_at: datetime,
        retry_before: datetime,
        max_attempts: int,
    ) -> ClaimedPublication | None:
        self.claim_arguments.append((claimed_at, retry_before, max_attempts))
        return self.claimed

    def lock_publication(
        self,
        *,
        publication_id: int,
    ) -> StoredPublicationDelivery | None:
        self.locked_ids.append(publication_id)
        return self.current

    def lock_stale_pending(
        self,
        *,
        stale_before: datetime,
        limit: int,
    ) -> tuple[StoredPublicationDelivery, ...]:
        self.stale_arguments.append((stale_before, limit))
        return self.stale

    def update_delivery(
        self,
        *,
        command: FinalizePublication,
        changed_at: datetime,
    ) -> None:
        self.updates.append((command, changed_at))

    def lock_promotion_destination(
        self,
        *,
        guild_id: str,
        destination_kind: str,
    ) -> PublicationPromotionDestination | None:
        self.promotion_destination_arguments.append((guild_id, destination_kind))
        return self.promotion_destination

    def promote_awaiting(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        target_channel_id: str,
        max_count: int,
        promoted_at: datetime,
    ) -> int:
        self.promotion_arguments.append((guild_id, destination_kind, target_channel_id, max_count, promoted_at))
        return self.promotion_count

    def list_unknown(
        self,
        *,
        guild_id: str,
        destination_kind: str,
        max_count: int,
    ) -> tuple[UnknownPublicationDelivery, ...]:
        self.unknown_list_arguments.append((guild_id, destination_kind, max_count))
        return self.unknown_rows

    def lock_unknown(
        self,
        *,
        publication_id: int,
        guild_id: str,
        destination_kind: str,
    ) -> UnknownPublicationDelivery | None:
        self.unknown_lock_arguments.append((publication_id, guild_id, destination_kind))
        return self.unknown_target

    def reconcile_unknown(
        self,
        *,
        publication_id: int,
        resolution: PublicationUnknownResolution,
        discord_message_id: str | None,
        reconciled_at: datetime,
    ) -> None:
        self.unknown_reconciliations.append((publication_id, resolution, discord_message_id, reconciled_at))


@dataclass
class RecordingUnitOfWork:
    publication_delivery: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commit_count == 0 and self.rollback_count == 0:
            self.rollback_count += 1
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(repository: RecordingRepository) -> tuple[PublicationDeliveryCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return PublicationDeliveryCommands(CommandRunner(factory), clock=lambda: NOW), factory


def _queries(repository: RecordingRepository) -> tuple[PublicationDeliveryQueries, RecordingFactory]:
    factory = RecordingFactory(repository)
    return PublicationDeliveryQueries(QueryRunner(factory)), factory


def test_claim_uses_configured_retry_cutoff_and_commits_pending_attempt() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    claimed = commands.claim_next(
        retry_delay=timedelta(minutes=7),
        max_attempts=4,
    )

    assert claimed == _claimed()
    assert repository.claim_arguments == [(NOW, NOW - timedelta(minutes=7), 4)]
    assert factory.created[0].commit_count == 1
    assert factory.created[0].rollback_count == 0


@pytest.mark.parametrize(
    "event_type",
    (
        MATCH_BETTING_OPENED_EVENT_TYPE,
        MATCH_BETTING_CLOSED_EVENT_TYPE,
        MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
    ),
)
def test_claim_contract_accepts_match_lifecycle_delivery_routes(event_type: str) -> None:
    claimed = ClaimedPublication(
        publication_id=42,
        guild_id="987654321",
        destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        event_type=event_type,
        target_channel_id="123456789",
        payload_json={"publication_type": "match_betting_opening"},
        payload_fingerprint="b" * 64,
        attempt_count=1,
    )

    assert claimed.event_type == event_type


def test_finalize_sent_validates_current_attempt_and_commits_anchor() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)
    command = FinalizePublication(
        publication_id=41,
        attempt_count=1,
        status=PublicationStatus.SENT,
        discord_message_id="555555555",
    )

    result = commands.finalize(command)

    assert result.status == PublicationStatus.SENT
    assert result.discord_message_id == "555555555"
    assert result.finalized_at == NOW
    assert repository.locked_ids == [41]
    assert repository.updates == [(command, NOW)]
    assert factory.created[0].commit_count == 1


@pytest.mark.parametrize(
    "current",
    (
        StoredPublicationDelivery(
            publication_id=41,
            status=PublicationStatus.FAILED,
            attempt_count=1,
        ),
        StoredPublicationDelivery(
            publication_id=41,
            status=PublicationStatus.PENDING,
            attempt_count=2,
        ),
    ),
)
def test_stale_finalizer_rolls_back_without_update(current: StoredPublicationDelivery) -> None:
    repository = RecordingRepository()
    repository.current = current
    commands, factory = _commands(repository)

    with pytest.raises(PublicationDeliveryLeaseConflictError):
        commands.finalize(
            FinalizePublication(
                publication_id=41,
                attempt_count=1,
                status=PublicationStatus.FAILED,
                error_code="known_zero_send",
                failure_stage=PublicationFailureStage.SEND,
            )
        )

    assert repository.updates == []
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_stale_pending_recovery_marks_each_locked_attempt_unknown() -> None:
    repository = RecordingRepository()
    repository.stale = (
        StoredPublicationDelivery(41, PublicationStatus.PENDING, 1),
        StoredPublicationDelivery(42, PublicationStatus.PENDING, 3),
    )
    commands, factory = _commands(repository)

    count = commands.recover_stale_pending(
        pending_timeout=timedelta(minutes=15),
        max_count=8,
    )

    assert count == 2
    assert repository.stale_arguments == [(NOW - timedelta(minutes=15), 8)]
    assert [command.status for command, _ in repository.updates] == [
        PublicationStatus.DELIVERY_UNKNOWN,
        PublicationStatus.DELIVERY_UNKNOWN,
    ]
    assert [command.attempt_count for command, _ in repository.updates] == [1, 3]
    assert all(command.error_code == PUBLICATION_STALE_DELIVERY_ERROR_CODE for command, _ in repository.updates)
    assert all(command.failure_stage == PublicationFailureStage.WORKER for command, _ in repository.updates)
    assert all(changed_at == NOW for _, changed_at in repository.updates)
    assert factory.created[0].commit_count == 1


def test_promote_awaiting_uses_current_destination_and_commits_bounded_batch() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.promote_awaiting(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        max_count=8,
    )

    assert result.guild_id == "987654321"
    assert result.destination_kind == WIN5_ANNOUNCEMENT_DESTINATION_KIND
    assert result.target_channel_id == "123456789"
    assert result.promoted_count == 2
    assert result.promoted_at == NOW
    assert repository.promotion_destination_arguments == [("987654321", WIN5_ANNOUNCEMENT_DESTINATION_KIND)]
    assert repository.promotion_arguments == [
        (
            "987654321",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            "123456789",
            8,
            NOW,
        )
    ]
    assert factory.created[0].commit_count == 1


@pytest.mark.parametrize(
    "destination",
    (
        None,
        PublicationPromotionDestination(
            "987654321",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            False,
            "123456789",
        ),
        PublicationPromotionDestination(
            "987654321",
            WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            True,
            None,
        ),
    ),
)
def test_promote_awaiting_rejects_unavailable_current_destination_without_write(
    destination: PublicationPromotionDestination | None,
) -> None:
    repository = RecordingRepository()
    repository.promotion_destination = destination
    commands, factory = _commands(repository)

    with pytest.raises(PublicationAwaitingPromotionUnavailableError):
        commands.promote_awaiting(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=8,
        )

    assert repository.promotion_arguments == []
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_promote_awaiting_rejects_invalid_repository_count_and_rolls_back() -> None:
    repository = RecordingRepository()
    repository.promotion_count = 9
    commands, factory = _commands(repository)

    with pytest.raises(PublicationAwaitingPromotionInvalidSourceError, match="batch count"):
        commands.promote_awaiting(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=8,
        )

    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_list_unknown_returns_bounded_exact_context_and_rolls_back_query() -> None:
    repository = RecordingRepository()
    queries, factory = _queries(repository)

    rows = queries.list_unknown(
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        max_count=8,
    )

    assert rows == (_unknown(),)
    assert repository.unknown_list_arguments == [("987654321", WIN5_ANNOUNCEMENT_DESTINATION_KIND, 8)]
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_list_unknown_rejects_repository_context_drift() -> None:
    repository = RecordingRepository()
    repository.unknown_rows = (
        UnknownPublicationDelivery(
            publication_id=51,
            guild_id="111111111",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            event_type=WIN5_ROUND_RESULT_EVENT_TYPE,
            event_key="win5-round:81:result",
            source_kind="win5_round",
            source_id=81,
            target_channel_id="123456789",
            payload_fingerprint="b" * 64,
            attempt_count=2,
            discord_message_id=None,
            error_code="discord_send_timeout",
            failure_stage=PublicationFailureStage.SEND,
            created_at=NOW - timedelta(minutes=2),
            updated_at=NOW - timedelta(minutes=1),
        ),
    )
    queries, factory = _queries(repository)

    with pytest.raises(PublicationUnknownReconciliationInvalidSourceError, match="different context"):
        queries.list_unknown(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=8,
        )

    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_reconcile_unknown_marks_complete_delivery_sent_with_verified_anchor() -> None:
    repository = RecordingRepository()
    repository.unknown_target = _unknown(anchor_message_id="555555555")
    commands, factory = _commands(repository)
    command = ReconcileUnknownPublication(
        publication_id=51,
        guild_id="987654321",
        destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
        expected_attempt_count=2,
        resolution=PublicationUnknownResolution.CONFIRMED_SENT,
        discord_message_id="555555555",
    )

    result = commands.reconcile_unknown(command)

    assert result.status == PublicationStatus.SENT
    assert result.discord_message_id == "555555555"
    assert result.reconciled_at == NOW
    assert repository.unknown_lock_arguments == [(51, "987654321", WIN5_ANNOUNCEMENT_DESTINATION_KIND)]
    assert repository.unknown_reconciliations == [(51, PublicationUnknownResolution.CONFIRMED_SENT, "555555555", NOW)]
    assert factory.created[0].commit_count == 1


def test_reconcile_unknown_requeues_only_anchor_free_confirmed_zero_send() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.reconcile_unknown(
        ReconcileUnknownPublication(
            publication_id=51,
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            expected_attempt_count=2,
            resolution=PublicationUnknownResolution.RETRY_ZERO_SEND,
        )
    )

    assert result.status == PublicationStatus.READY
    assert result.discord_message_id is None
    assert repository.unknown_reconciliations == [(51, PublicationUnknownResolution.RETRY_ZERO_SEND, None, NOW)]
    assert factory.created[0].commit_count == 1


@pytest.mark.parametrize(
    ("target", "command", "error_type"),
    (
        (
            None,
            ReconcileUnknownPublication(
                51,
                "987654321",
                WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                2,
                PublicationUnknownResolution.RETRY_ZERO_SEND,
            ),
            PublicationUnknownReconciliationUnavailableError,
        ),
        (
            _unknown(attempt_count=3),
            ReconcileUnknownPublication(
                51,
                "987654321",
                WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                2,
                PublicationUnknownResolution.RETRY_ZERO_SEND,
            ),
            PublicationUnknownReconciliationConflictError,
        ),
        (
            replace(_unknown(), guild_id="111111111"),
            ReconcileUnknownPublication(
                51,
                "987654321",
                WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                2,
                PublicationUnknownResolution.RETRY_ZERO_SEND,
            ),
            PublicationUnknownReconciliationInvalidSourceError,
        ),
        (
            _unknown(anchor_message_id="555555555"),
            ReconcileUnknownPublication(
                51,
                "987654321",
                WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                2,
                PublicationUnknownResolution.RETRY_ZERO_SEND,
            ),
            PublicationUnknownReconciliationConflictError,
        ),
        (
            _unknown(anchor_message_id="555555555"),
            ReconcileUnknownPublication(
                51,
                "987654321",
                WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                2,
                PublicationUnknownResolution.CONFIRMED_SENT,
                "666666666",
            ),
            PublicationUnknownReconciliationConflictError,
        ),
    ),
)
def test_reconcile_unknown_rejects_unavailable_or_stale_evidence_without_write(
    target: UnknownPublicationDelivery | None,
    command: ReconcileUnknownPublication,
    error_type: type[PublicationDeliveryError],
) -> None:
    repository = RecordingRepository()
    repository.unknown_target = target
    commands, factory = _commands(repository)

    with pytest.raises(error_type):
        commands.reconcile_unknown(command)

    assert repository.unknown_reconciliations == []
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_finalize_contract_distinguishes_known_zero_send_from_unknown_partial() -> None:
    with pytest.raises(ValueError, match="zero-send"):
        FinalizePublication(
            publication_id=41,
            attempt_count=1,
            status=PublicationStatus.FAILED,
            discord_message_id="555555555",
            error_code="partial",
            failure_stage=PublicationFailureStage.SEND,
        )

    unknown = FinalizePublication(
        publication_id=41,
        attempt_count=1,
        status=PublicationStatus.DELIVERY_UNKNOWN,
        discord_message_id="555555555",
        error_code="partial",
        failure_stage=PublicationFailureStage.SEND,
    )

    assert unknown.discord_message_id == "555555555"


def test_retry_and_timeout_configuration_must_be_positive() -> None:
    commands, _ = _commands(RecordingRepository())

    with pytest.raises(ValueError, match="retry_delay"):
        commands.claim_next(retry_delay=timedelta(0), max_attempts=3)
    with pytest.raises(ValueError, match="max_attempts"):
        commands.claim_next(retry_delay=timedelta(seconds=1), max_attempts=0)
    with pytest.raises(ValueError, match="pending_timeout"):
        commands.recover_stale_pending(pending_timeout=timedelta(0), max_count=3)
    with pytest.raises(ValueError, match="destination kind"):
        commands.promote_awaiting(
            guild_id="987654321",
            destination_kind="log",
            max_count=3,
        )
    with pytest.raises(ValueError, match="max_count"):
        commands.promote_awaiting(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=0,
        )
    with pytest.raises(ValueError, match="must not exceed"):
        commands.promote_awaiting(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=PUBLICATION_AWAITING_PROMOTION_MAX_COUNT + 1,
        )
    queries, _ = _queries(RecordingRepository())
    with pytest.raises(ValueError, match="must not exceed"):
        queries.list_unknown(
            guild_id="987654321",
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=PUBLICATION_UNKNOWN_LIST_MAX_COUNT + 1,
        )
