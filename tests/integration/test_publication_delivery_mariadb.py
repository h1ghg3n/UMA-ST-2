"""MariaDB locking evidence for WIN5 publication delivery claims."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    WIN5_ROUND_RESULT_EVENT_TYPE,
    FinalizePublication,
    PublicationAwaitingPromotionInvalidSourceError,
    PublicationAwaitingPromotionUnavailableError,
    PublicationDeliveryLeaseConflictError,
    PublicationFailureStage,
    PublicationUnknownReconciliationConflictError,
    PublicationUnknownReconciliationUnavailableError,
    PublicationUnknownResolution,
    ReconcileUnknownPublication,
)
from uma_st2.compose import (
    compose_publication_delivery,
    compose_publication_delivery_queries,
)
from uma_st2.domain.publication import PublicationStatus
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import BotGuildSettingORM, DiscordPublicationORM

pytestmark = pytest.mark.integration


def _publication_values(
    *,
    suffix: str,
    ordinal: int,
    status: PublicationStatus,
    attempt_count: int = 0,
    updated_at: datetime,
    attempt_started_at: datetime | None = None,
    guild_id: str = "987654321",
    destination_kind: str = WIN5_ANNOUNCEMENT_DESTINATION_KIND,
    event_type: str = WIN5_ROUND_RESULT_EVENT_TYPE,
    target_channel_id: str | None = "123456789",
    discord_message_id: str | None = None,
    last_error_code: str | None = None,
    failure_stage: PublicationFailureStage | None = None,
    published_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "guild_id": guild_id,
        "destination_kind": destination_kind,
        "event_type": event_type,
        "event_key": f"delivery-{suffix}-{ordinal}",
        "source_kind": "win5_round",
        "source_id": ordinal,
        "target_channel_id": target_channel_id,
        "payload_json": {"schema_version": 1, "ordinal": ordinal},
        "payload_fingerprint": f"{ordinal:064x}",
        "status": status.value,
        "attempt_count": attempt_count,
        "discord_message_id": discord_message_id,
        "last_error_code": last_error_code,
        "failure_stage": None if failure_stage is None else failure_stage.value,
        "attempt_started_at": attempt_started_at,
        "published_at": published_at,
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def _insert_publications(engine: Engine, values: list[dict[str, object]]) -> tuple[int, ...]:
    with engine.begin() as connection:
        return tuple(
            connection.execute(DiscordPublicationORM.__table__.insert().values(**item)).inserted_primary_key[0]
            for item in values
        )


def _cleanup(engine: Engine, publication_ids: tuple[int, ...]) -> None:
    with engine.begin() as connection:
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.id.in_(publication_ids)))


def _unique_guild_id() -> str:
    return str(10**17 + uuid4().int % 10**17)


def _insert_guild_setting(
    engine: Engine,
    *,
    guild_id: str,
    win5_channel_id: str | None,
    win5_enabled: bool = True,
    match_channel_id: str | None = None,
    match_enabled: bool = True,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    with engine.begin() as connection:
        connection.execute(
            BotGuildSettingORM.__table__.insert().values(
                guild_id=guild_id,
                win5_announcement_channel_id=win5_channel_id,
                match_announcement_channel_id=match_channel_id,
                log_channel_id=None,
                operator_role_id="333333333",
                bot_manager_role_id=None,
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=win5_enabled,
                match_announcements_enabled=match_enabled,
                created_at=now,
                updated_at=now,
            )
        )


def _cleanup_with_setting(
    engine: Engine,
    *,
    publication_ids: tuple[int, ...],
    guild_id: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.id.in_(publication_ids)))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def test_concurrent_workers_claim_distinct_ready_rows(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=ordinal,
                status=PublicationStatus.READY,
                updated_at=now,
            )
            for ordinal in (1, 2)
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))
    start = Barrier(2)

    def claim_after_barrier() -> object:
        start.wait()
        return commands.claim_next(retry_delay=timedelta(minutes=5), max_attempts=3)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim_after_barrier) for _ in range(2)]
            claimed = [_resolve(future) for future in futures]

        assert {item.publication_id for item in claimed if item is not None} == set(publication_ids)
        assert all(item is not None and item.attempt_count == 1 for item in claimed)
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(
                    DiscordPublicationORM.id,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.attempt_count,
                    DiscordPublicationORM.attempt_started_at,
                )
                .where(DiscordPublicationORM.id.in_(publication_ids))
                .order_by(DiscordPublicationORM.id)
            ).all()
        assert [row.status for row in rows] == [PublicationStatus.PENDING.value] * 2
        assert [row.attempt_count for row in rows] == [1, 1]
        assert all(row.attempt_started_at is not None for row in rows)
    finally:
        _cleanup(migrated_engine, publication_ids)


def test_retry_bounds_finalize_lease_and_stale_recovery(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    old = now - timedelta(hours=1)
    recent = now - timedelta(seconds=10)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=1,
                status=PublicationStatus.READY,
                updated_at=old,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=2,
                status=PublicationStatus.FAILED,
                attempt_count=1,
                updated_at=old,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=3,
                status=PublicationStatus.FAILED,
                attempt_count=1,
                updated_at=recent,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=4,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=1,
                updated_at=old,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=5,
                status=PublicationStatus.FAILED,
                attempt_count=3,
                updated_at=old,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=6,
                status=PublicationStatus.PENDING,
                attempt_count=2,
                updated_at=old,
                attempt_started_at=old,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=7,
                status=PublicationStatus.AWAITING_CHANNEL,
                updated_at=old,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=8,
                status=PublicationStatus.SUPPRESSED,
                updated_at=old,
            ),
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        first = commands.claim_next(retry_delay=timedelta(minutes=5), max_attempts=3)
        assert first is not None
        assert first.publication_id == publication_ids[0]
        commands.finalize(
            FinalizePublication(
                publication_id=first.publication_id,
                attempt_count=first.attempt_count,
                status=PublicationStatus.FAILED,
                error_code="known_zero_send",
                failure_stage=PublicationFailureStage.CHANNEL,
            )
        )

        second = commands.claim_next(retry_delay=timedelta(minutes=5), max_attempts=3)
        assert second is not None
        assert second.publication_id == publication_ids[1]
        assert second.attempt_count == 2
        commands.finalize(
            FinalizePublication(
                publication_id=second.publication_id,
                attempt_count=second.attempt_count,
                status=PublicationStatus.SENT,
                discord_message_id="555555555",
            )
        )

        assert commands.claim_next(retry_delay=timedelta(minutes=5), max_attempts=3) is None
        assert commands.recover_stale_pending(pending_timeout=timedelta(minutes=5), max_count=10) == 1

        with pytest.raises(PublicationDeliveryLeaseConflictError):
            commands.finalize(
                FinalizePublication(
                    publication_id=second.publication_id,
                    attempt_count=second.attempt_count,
                    status=PublicationStatus.SENT,
                    discord_message_id="666666666",
                )
            )

        with migrated_engine.connect() as connection:
            rows = {
                row.id: row
                for row in connection.execute(
                    select(
                        DiscordPublicationORM.id,
                        DiscordPublicationORM.status,
                        DiscordPublicationORM.attempt_count,
                        DiscordPublicationORM.discord_message_id,
                        DiscordPublicationORM.last_error_code,
                        DiscordPublicationORM.failure_stage,
                        DiscordPublicationORM.published_at,
                    ).where(DiscordPublicationORM.id.in_(publication_ids))
                ).all()
            }

        assert rows[publication_ids[0]].status == PublicationStatus.FAILED.value
        assert rows[publication_ids[1]].status == PublicationStatus.SENT.value
        assert rows[publication_ids[1]].discord_message_id == "555555555"
        assert rows[publication_ids[1]].published_at is not None
        assert rows[publication_ids[2]].status == PublicationStatus.FAILED.value
        assert rows[publication_ids[3]].status == PublicationStatus.DELIVERY_UNKNOWN.value
        assert rows[publication_ids[4]].attempt_count == 3
        assert rows[publication_ids[5]].status == PublicationStatus.DELIVERY_UNKNOWN.value
        assert rows[publication_ids[5]].last_error_code == "worker_lease_expired"
        assert rows[publication_ids[5]].failure_stage == PublicationFailureStage.WORKER.value
        assert rows[publication_ids[6]].status == PublicationStatus.AWAITING_CHANNEL.value
        assert rows[publication_ids[7]].status == PublicationStatus.SUPPRESSED.value
    finally:
        _cleanup(migrated_engine, publication_ids)


def test_promote_awaiting_uses_exact_current_destination_in_bounded_batches_and_preserves_evidence(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    channel_id = "444444444"
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    _insert_guild_setting(
        migrated_engine,
        guild_id=guild_id,
        win5_channel_id=channel_id,
        match_channel_id="555555555",
    )
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=ordinal,
                status=PublicationStatus.AWAITING_CHANNEL,
                updated_at=now,
                guild_id=guild_id,
                target_channel_id=None,
            )
            for ordinal in (11, 12)
        ]
        + [
            _publication_values(
                suffix=suffix,
                ordinal=13,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=1,
                updated_at=now,
                guild_id=guild_id,
            )
        ]
        + [
            _publication_values(
                suffix=suffix,
                ordinal=14,
                status=PublicationStatus.AWAITING_CHANNEL,
                updated_at=now,
                guild_id=guild_id,
                destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
                target_channel_id=None,
            )
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        first = commands.promote_awaiting(
            guild_id=guild_id,
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=1,
        )
        second = commands.promote_awaiting(
            guild_id=guild_id,
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=10,
        )
        empty = commands.promote_awaiting(
            guild_id=guild_id,
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=10,
        )
        match = commands.promote_awaiting(
            guild_id=guild_id,
            destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=10,
        )

        assert (first.promoted_count, second.promoted_count, empty.promoted_count) == (1, 1, 0)
        assert first.target_channel_id == second.target_channel_id == empty.target_channel_id == channel_id
        assert match.promoted_count == 1
        assert match.target_channel_id == "555555555"
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(
                    DiscordPublicationORM.id,
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.payload_json,
                    DiscordPublicationORM.payload_fingerprint,
                    DiscordPublicationORM.attempt_count,
                    DiscordPublicationORM.discord_message_id,
                    DiscordPublicationORM.last_error_code,
                    DiscordPublicationORM.failure_stage,
                    DiscordPublicationORM.attempt_started_at,
                    DiscordPublicationORM.published_at,
                )
                .where(DiscordPublicationORM.id.in_(publication_ids))
                .order_by(DiscordPublicationORM.id)
            ).all()

        for row, ordinal in zip(rows[:2], (11, 12), strict=True):
            assert row.status == PublicationStatus.READY.value
            assert row.target_channel_id == channel_id
            assert row.payload_json == {"schema_version": 1, "ordinal": ordinal}
            assert row.payload_fingerprint == f"{ordinal:064x}"
            assert row.attempt_count == 0
            assert row.discord_message_id is None
            assert row.last_error_code is None
            assert row.failure_stage is None
            assert row.attempt_started_at is None
            assert row.published_at is None
        assert rows[2].status == PublicationStatus.DELIVERY_UNKNOWN.value
        assert rows[2].target_channel_id == "123456789"
        assert rows[2].attempt_count == 1
        assert rows[3].status == PublicationStatus.READY.value
        assert rows[3].target_channel_id == "555555555"
    finally:
        _cleanup_with_setting(
            migrated_engine,
            publication_ids=publication_ids,
            guild_id=guild_id,
        )


def test_promote_awaiting_rejects_disabled_destination_without_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    _insert_guild_setting(
        migrated_engine,
        guild_id=guild_id,
        win5_channel_id="444444444",
        win5_enabled=False,
    )
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=21,
                status=PublicationStatus.AWAITING_CHANNEL,
                updated_at=now,
                guild_id=guild_id,
                target_channel_id=None,
            )
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        with pytest.raises(PublicationAwaitingPromotionUnavailableError):
            commands.promote_awaiting(
                guild_id=guild_id,
                destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                max_count=10,
            )
        with migrated_engine.connect() as connection:
            row = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                ).where(DiscordPublicationORM.id == publication_ids[0])
            ).one()
        assert row == (PublicationStatus.AWAITING_CHANNEL.value, None)
    finally:
        _cleanup_with_setting(
            migrated_engine,
            publication_ids=publication_ids,
            guild_id=guild_id,
        )


def test_malformed_awaiting_batch_rolls_back_every_promotion(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    _insert_guild_setting(
        migrated_engine,
        guild_id=guild_id,
        win5_channel_id="444444444",
    )
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=31,
                status=PublicationStatus.AWAITING_CHANNEL,
                updated_at=now,
                guild_id=guild_id,
                target_channel_id=None,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=32,
                status=PublicationStatus.AWAITING_CHANNEL,
                attempt_count=1,
                updated_at=now,
                guild_id=guild_id,
                target_channel_id=None,
            ),
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        with pytest.raises(PublicationAwaitingPromotionInvalidSourceError, match="attempt evidence"):
            commands.promote_awaiting(
                guild_id=guild_id,
                destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                max_count=10,
            )
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                )
                .where(DiscordPublicationORM.id.in_(publication_ids))
                .order_by(DiscordPublicationORM.id)
            ).all()
        assert rows == [
            (PublicationStatus.AWAITING_CHANNEL.value, None),
            (PublicationStatus.AWAITING_CHANNEL.value, None),
        ]
    finally:
        _cleanup_with_setting(
            migrated_engine,
            publication_ids=publication_ids,
            guild_id=guild_id,
        )


def test_concurrent_promotion_batches_serialize_on_guild_setting(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    _insert_guild_setting(
        migrated_engine,
        guild_id=guild_id,
        win5_channel_id="444444444",
    )
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=ordinal,
                status=PublicationStatus.AWAITING_CHANNEL,
                updated_at=now,
                guild_id=guild_id,
                target_channel_id=None,
            )
            for ordinal in (41, 42)
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))
    start = Barrier(2)

    def promote_after_barrier() -> object:
        start.wait()
        return commands.promote_awaiting(
            guild_id=guild_id,
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=1,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(promote_after_barrier) for _ in range(2)]
            receipts = [_resolve(future) for future in futures]
        assert [receipt.promoted_count for receipt in receipts] == [1, 1]
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                )
                .where(DiscordPublicationORM.id.in_(publication_ids))
                .order_by(DiscordPublicationORM.id)
            ).all()
        assert rows == [
            (PublicationStatus.READY.value, "444444444"),
            (PublicationStatus.READY.value, "444444444"),
        ]
    finally:
        _cleanup_with_setting(
            migrated_engine,
            publication_ids=publication_ids,
            guild_id=guild_id,
        )


def test_list_unknown_is_bounded_and_isolated_by_exact_guild_destination(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    other_guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=ordinal,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=2,
                updated_at=now,
                guild_id=guild_id,
                discord_message_id=None,
                last_error_code="discord_send_timeout",
                failure_stage=PublicationFailureStage.SEND,
            )
            for ordinal in (51, 52)
        ]
        + [
            _publication_values(
                suffix=suffix,
                ordinal=53,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=1,
                updated_at=now,
                guild_id=other_guild_id,
                last_error_code="worker_lease_expired",
                failure_stage=PublicationFailureStage.WORKER,
            ),
            _publication_values(
                suffix=suffix,
                ordinal=54,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=1,
                updated_at=now,
                guild_id=guild_id,
                destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
                last_error_code="discord_send_timeout",
                failure_stage=PublicationFailureStage.SEND,
            ),
        ],
    )
    queries = compose_publication_delivery_queries(DatabaseRuntime.from_engine(migrated_engine))

    try:
        first = queries.list_unknown(
            guild_id=guild_id,
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=1,
        )
        all_rows = queries.list_unknown(
            guild_id=guild_id,
            destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
            max_count=10,
        )

        assert [row.publication_id for row in first] == [publication_ids[0]]
        assert [row.publication_id for row in all_rows] == list(publication_ids[:2])
        assert [row.source_id for row in all_rows] == [51, 52]
        assert all(row.error_code == "discord_send_timeout" for row in all_rows)
        assert all(row.failure_stage == PublicationFailureStage.SEND for row in all_rows)
    finally:
        _cleanup(migrated_engine, publication_ids)


def test_mark_unknown_sent_preserves_snapshot_and_attempt_evidence(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=61,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=3,
                updated_at=now,
                guild_id=guild_id,
                discord_message_id="555555555",
                last_error_code="discord_send_timeout",
                failure_stage=PublicationFailureStage.SEND,
            )
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        result = commands.reconcile_unknown(
            ReconcileUnknownPublication(
                publication_id=publication_ids[0],
                guild_id=guild_id,
                destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                expected_attempt_count=3,
                resolution=PublicationUnknownResolution.CONFIRMED_SENT,
                discord_message_id="555555555",
            )
        )

        assert result.status == PublicationStatus.SENT
        with migrated_engine.connect() as connection:
            row = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.target_channel_id,
                    DiscordPublicationORM.payload_json,
                    DiscordPublicationORM.payload_fingerprint,
                    DiscordPublicationORM.attempt_count,
                    DiscordPublicationORM.discord_message_id,
                    DiscordPublicationORM.last_error_code,
                    DiscordPublicationORM.failure_stage,
                    DiscordPublicationORM.attempt_started_at,
                    DiscordPublicationORM.published_at,
                ).where(DiscordPublicationORM.id == publication_ids[0])
            ).one()
        assert row.status == PublicationStatus.SENT.value
        assert row.target_channel_id == "123456789"
        assert row.payload_json == {"schema_version": 1, "ordinal": 61}
        assert row.payload_fingerprint == f"{61:064x}"
        assert row.attempt_count == 3
        assert row.discord_message_id == "555555555"
        assert row.last_error_code is None
        assert row.failure_stage is None
        assert row.attempt_started_at is None
        assert row.published_at is not None
    finally:
        _cleanup(migrated_engine, publication_ids)


def test_retry_unknown_zero_send_grants_one_claim_after_automatic_attempt_limit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=71,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=3,
                updated_at=now,
                guild_id=guild_id,
                last_error_code="worker_lease_expired",
                failure_stage=PublicationFailureStage.WORKER,
            )
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        reconciled = commands.reconcile_unknown(
            ReconcileUnknownPublication(
                publication_id=publication_ids[0],
                guild_id=guild_id,
                destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                expected_attempt_count=3,
                resolution=PublicationUnknownResolution.RETRY_ZERO_SEND,
            )
        )
        claimed = commands.claim_next(
            retry_delay=timedelta(minutes=5),
            max_attempts=3,
        )

        assert reconciled.status == PublicationStatus.READY
        assert reconciled.attempt_count == 3
        assert claimed is not None
        assert claimed.publication_id == publication_ids[0]
        assert claimed.attempt_count == 4
        with migrated_engine.connect() as connection:
            row = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.attempt_count,
                    DiscordPublicationORM.discord_message_id,
                    DiscordPublicationORM.last_error_code,
                    DiscordPublicationORM.failure_stage,
                    DiscordPublicationORM.attempt_started_at,
                    DiscordPublicationORM.published_at,
                ).where(DiscordPublicationORM.id == publication_ids[0])
            ).one()
        assert row.status == PublicationStatus.PENDING.value
        assert row.attempt_count == 4
        assert row.discord_message_id is None
        assert row.last_error_code is None
        assert row.failure_stage is None
        assert row.attempt_started_at is not None
        assert row.published_at is None
    finally:
        _cleanup(migrated_engine, publication_ids)


def test_anchor_bearing_unknown_retry_is_zero_write(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=81,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=2,
                updated_at=now,
                guild_id=guild_id,
                discord_message_id="555555555",
                last_error_code="discord_send_timeout",
                failure_stage=PublicationFailureStage.SEND,
            )
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))

    try:
        with pytest.raises(PublicationUnknownReconciliationConflictError, match="anchor-bearing"):
            commands.reconcile_unknown(
                ReconcileUnknownPublication(
                    publication_id=publication_ids[0],
                    guild_id=guild_id,
                    destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                    expected_attempt_count=2,
                    resolution=PublicationUnknownResolution.RETRY_ZERO_SEND,
                )
            )
        with migrated_engine.connect() as connection:
            row = connection.execute(
                select(
                    DiscordPublicationORM.status,
                    DiscordPublicationORM.attempt_count,
                    DiscordPublicationORM.discord_message_id,
                    DiscordPublicationORM.last_error_code,
                ).where(DiscordPublicationORM.id == publication_ids[0])
            ).one()
        assert row == (
            PublicationStatus.DELIVERY_UNKNOWN.value,
            2,
            "555555555",
            "discord_send_timeout",
        )
    finally:
        _cleanup(migrated_engine, publication_ids)


def test_concurrent_unknown_resolutions_allow_exactly_one_winner(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    guild_id = _unique_guild_id()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    publication_ids = _insert_publications(
        migrated_engine,
        [
            _publication_values(
                suffix=suffix,
                ordinal=91,
                status=PublicationStatus.DELIVERY_UNKNOWN,
                attempt_count=2,
                updated_at=now,
                guild_id=guild_id,
                last_error_code="discord_send_timeout",
                failure_stage=PublicationFailureStage.SEND,
            )
        ],
    )
    commands = compose_publication_delivery(DatabaseRuntime.from_engine(migrated_engine))
    start = Barrier(2)

    def reconcile_after_barrier(resolution: PublicationUnknownResolution) -> object:
        start.wait()
        return commands.reconcile_unknown(
            ReconcileUnknownPublication(
                publication_id=publication_ids[0],
                guild_id=guild_id,
                destination_kind=WIN5_ANNOUNCEMENT_DESTINATION_KIND,
                expected_attempt_count=2,
                resolution=resolution,
                discord_message_id=("555555555" if resolution == PublicationUnknownResolution.CONFIRMED_SENT else None),
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    reconcile_after_barrier,
                    PublicationUnknownResolution.CONFIRMED_SENT,
                ),
                executor.submit(
                    reconcile_after_barrier,
                    PublicationUnknownResolution.RETRY_ZERO_SEND,
                ),
            ]
            outcomes: list[object] = []
            failures: list[BaseException] = []
            for future in futures:
                try:
                    outcomes.append(_resolve(future))
                except BaseException as exc:
                    failures.append(exc)

        assert len(outcomes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], PublicationUnknownReconciliationUnavailableError)
        with migrated_engine.connect() as connection:
            status = connection.scalar(
                select(DiscordPublicationORM.status).where(DiscordPublicationORM.id == publication_ids[0])
            )
        assert status in {PublicationStatus.SENT.value, PublicationStatus.READY.value}
    finally:
        _cleanup(migrated_engine, publication_ids)
