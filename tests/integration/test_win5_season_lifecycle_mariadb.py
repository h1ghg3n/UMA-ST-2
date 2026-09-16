"""MariaDB transaction evidence for WIN5 Season lifecycle commands."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.win5 import (
    CreateWin5Season,
    TransitionWin5Season,
    UpdateWin5SeasonMetadata,
    Win5SeasonAction,
    Win5SeasonActivationConflictError,
    Win5SeasonAuditType,
    Win5SeasonLifecycleUnavailableError,
)
from uma_st2.compose import compose_win5_season_lifecycle
from uma_st2.domain.win5 import Win5SeasonStatus
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import OperationORM, Win5OperationORM, Win5SeasonORM

pytestmark = pytest.mark.integration


def _cleanup(engine: Engine, season_ids: tuple[int, ...]) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(select(Win5OperationORM.operation_id).where(Win5OperationORM.season_id.in_(season_ids)))
        )
        if operation_ids:
            connection.execute(delete(Win5OperationORM).where(Win5OperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(Win5SeasonORM).where(Win5SeasonORM.id.in_(season_ids)))


def _resolve(future: Future[object]) -> object:
    return future.result(timeout=15)


def _transition(*, season_id: int, action: Win5SeasonAction, key: str) -> TransitionWin5Season:
    return TransitionWin5Season(
        season_id=season_id,
        action=action,
        idempotency_key=key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id=f"season-{key}",
    )


def test_create_exact_retry_metadata_noop_and_empty_close(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    commands = compose_win5_season_lifecycle(DatabaseRuntime.from_engine(migrated_engine))
    create_command = CreateWin5Season(
        name=f"Season lifecycle {suffix}",
        starts_at=datetime(2026, 8, 30, 6, 30, tzinfo=UTC),
        ends_at=None,
        idempotency_key=f"season-create-{suffix}",
        actor_discord_user_id="123456789",
        guild_id="987654321",
    )
    season_ids: tuple[int, ...] = ()
    try:
        created = commands.create_season(create_command)
        season_ids = (created.snapshot.id,)
        assert commands.create_season(create_command) == created

        noop = commands.update_metadata(
            UpdateWin5SeasonMetadata(
                season_id=created.snapshot.id,
                name=create_command.name,
                starts_at=create_command.starts_at,
                ends_at=create_command.ends_at,
                idempotency_key=f"season-noop-{suffix}",
                actor_discord_user_id="123456789",
            )
        )
        assert noop.changed is False

        activated = commands.transition_season(
            _transition(
                season_id=created.snapshot.id,
                action=Win5SeasonAction.ACTIVATE,
                key=f"season-activate-{suffix}",
            )
        )
        closed = commands.transition_season(
            _transition(
                season_id=created.snapshot.id,
                action=Win5SeasonAction.CLOSE,
                key=f"season-close-{suffix}",
            )
        )
        assert activated.snapshot.status == Win5SeasonStatus.ACTIVE
        assert closed.snapshot.status == Win5SeasonStatus.CLOSED

        with pytest.raises(Win5SeasonLifecycleUnavailableError):
            commands.transition_season(
                _transition(
                    season_id=created.snapshot.id,
                    action=Win5SeasonAction.ACTIVATE,
                    key=f"season-reactivate-{suffix}",
                )
            )

        with migrated_engine.connect() as connection:
            operation_types = tuple(
                connection.scalars(
                    select(Win5OperationORM.type)
                    .where(Win5OperationORM.season_id == created.snapshot.id)
                    .order_by(Win5OperationORM.operation_id)
                )
            )
            operation_count = connection.scalar(
                select(func.count())
                .select_from(OperationORM)
                .join(Win5OperationORM, Win5OperationORM.operation_id == OperationORM.id)
                .where(Win5OperationORM.season_id == created.snapshot.id)
            )
        assert operation_types == (
            Win5SeasonAuditType.CREATED.value,
            Win5SeasonAuditType.ACTIVATED.value,
            Win5SeasonAuditType.CLOSED.value,
        )
        assert operation_count == 3
    finally:
        if season_ids:
            _cleanup(migrated_engine, season_ids)


def test_concurrent_activation_has_one_constraint_winner(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    with migrated_engine.begin() as connection:
        season_ids = tuple(
            connection.execute(
                Win5SeasonORM.__table__.insert().values(
                    name=f"Concurrent draft {index} {suffix}",
                    status=Win5SeasonStatus.DRAFT.value,
                    active_marker=None,
                    created_at=now,
                    updated_at=now,
                )
            ).inserted_primary_key[0]
            for index in range(2)
        )
    commands = compose_win5_season_lifecycle(DatabaseRuntime.from_engine(migrated_engine))
    commands_to_run = tuple(
        _transition(
            season_id=season_id,
            action=Win5SeasonAction.ACTIVATE,
            key=f"season-concurrent-activate-{suffix}-{index}",
        )
        for index, season_id in enumerate(season_ids)
    )
    start = Barrier(2)

    def activate_after_barrier(command_: TransitionWin5Season) -> object:
        start.wait()
        return commands.transition_season(command_)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(activate_after_barrier, command_) for command_ in commands_to_run]
            results: list[object] = []
            errors: list[Exception] = []
            for future in futures:
                try:
                    results.append(_resolve(future))
                except Exception as error:
                    errors.append(error)

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], Win5SeasonActivationConflictError)
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                select(Win5SeasonORM.status, Win5SeasonORM.active_marker)
                .where(Win5SeasonORM.id.in_(season_ids))
                .order_by(Win5SeasonORM.id)
            ).all()
            audit_count = connection.scalar(
                select(func.count())
                .select_from(Win5OperationORM)
                .where(
                    Win5OperationORM.season_id.in_(season_ids),
                    Win5OperationORM.type == Win5SeasonAuditType.ACTIVATED.value,
                )
            )
        assert sorted((row.status, row.active_marker) for row in rows) == [
            (Win5SeasonStatus.ACTIVE.value, True),
            (Win5SeasonStatus.DRAFT.value, None),
        ]
        assert audit_count == 1
    finally:
        _cleanup(migrated_engine, season_ids)
