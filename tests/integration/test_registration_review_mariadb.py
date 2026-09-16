"""MariaDB atomicity and serialization evidence for staff registration review."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine

from uma_st2.application.identity import (
    ApproveAccountRegistrationRequest,
    RejectAccountRegistrationRequest,
    StaffRegistrationPidUnavailableError,
    StaffRegistrationRequestNotPendingError,
    StaffRegistrationRequestState,
    StaffRegistrationReviewConcurrentConflictError,
    SubmitAccountRegistrationRequest,
)
from uma_st2.compose import (
    compose_account_registration_commands,
    compose_registration_review_commands,
    compose_staff_registration_review_queries,
)
from uma_st2.domain.identity import GameRegion
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.database.orm import (
    CirclePointORM,
    DiscordAccountORM,
    DiscordPublicationORM,
    GameAccountORM,
    GameAccountRegistrationRequestORM,
    IdentityOperationORM,
    OperationORM,
    PersonaORM,
    PointTransactionORM,
)

pytestmark = pytest.mark.integration


def _suffix() -> str:
    return str(uuid4().int % 10**17).zfill(17)


def _submission(
    *,
    suffix: str,
    guild_id: str,
    uma_pid: str | None = None,
) -> SubmitAccountRegistrationRequest:
    return SubmitAccountRegistrationRequest(
        guild_id=guild_id,
        actor_discord_user_id=f"9{suffix}",
        discord_display_name_snapshot=f"MariaDB 요청자 {suffix[-4:]}",
        game_region=GameRegion.KR,
        uma_pid=uma_pid or f"7{suffix}",
        nickname=f"계정 {suffix[-4:]}",
        affiliation="통합 테스트",
        idempotency_key=f"registration-request-{suffix}",
        correlation_id=f"registration-request-{suffix}",
    )


def _approve(
    request: StaffRegistrationRequestState,
    *,
    suffix: str,
) -> ApproveAccountRegistrationRequest:
    return ApproveAccountRegistrationRequest(
        request_id=request.request_id,
        guild_id=request.guild_id,
        reviewed_by_discord_user_id=f"8{suffix}",
        expected_request_fingerprint=request.state_fingerprint,
        idempotency_key=f"registration-approve-{suffix}",
        correlation_id=f"registration-approve-{suffix}",
    )


def _reject(
    request: StaffRegistrationRequestState,
    *,
    suffix: str,
) -> RejectAccountRegistrationRequest:
    return RejectAccountRegistrationRequest(
        request_id=request.request_id,
        guild_id=request.guild_id,
        reviewed_by_discord_user_id=f"8{suffix}",
        expected_request_fingerprint=request.state_fingerprint,
        reason="운영자 검토 반려",
        idempotency_key=f"registration-reject-{suffix}",
        correlation_id=f"registration-reject-{suffix}",
    )


def _seed_request(
    engine: Engine,
    *,
    submission: SubmitAccountRegistrationRequest,
) -> StaffRegistrationRequestState:
    runtime = DatabaseRuntime.from_engine(engine)
    created = compose_account_registration_commands(runtime).submit_registration_request(submission)
    return compose_staff_registration_review_queries(runtime).get_pending_request(
        guild_id=submission.guild_id,
        request_id=created.snapshot.request_id,
    )


def _cleanup(engine: Engine, *, guild_id: str) -> None:
    with engine.begin() as connection:
        persona_ids = tuple(
            connection.scalars(
                select(DiscordAccountORM.persona_id)
                .join(
                    GameAccountRegistrationRequestORM,
                    GameAccountRegistrationRequestORM.requester_discord_user_id == DiscordAccountORM.discord_user_id,
                )
                .where(GameAccountRegistrationRequestORM.guild_id == guild_id)
            )
        )
        actor_ids = tuple(
            connection.scalars(
                select(GameAccountRegistrationRequestORM.requester_discord_user_id).where(
                    GameAccountRegistrationRequestORM.guild_id == guild_id
                )
            )
        )
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(IdentityOperationORM).where(IdentityOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(PointTransactionORM).where(PointTransactionORM.operation_id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.guild_id == guild_id))
        if persona_ids:
            connection.execute(delete(GameAccountORM).where(GameAccountORM.persona_id.in_(persona_ids)))
            connection.execute(delete(CirclePointORM).where(CirclePointORM.persona_id.in_(persona_ids)))
        connection.execute(
            delete(GameAccountRegistrationRequestORM).where(GameAccountRegistrationRequestORM.guild_id == guild_id)
        )
        if actor_ids:
            connection.execute(delete(DiscordAccountORM).where(DiscordAccountORM.discord_user_id.in_(actor_ids)))
        if persona_ids:
            connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(persona_ids)))
        if operation_ids:
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))


def test_approval_commits_complete_bootstrap_and_exact_retry_without_publication(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    submission = _submission(suffix=suffix, guild_id=guild_id)
    try:
        request = _seed_request(migrated_engine, submission=submission)
        command = _approve(request, suffix=suffix)
        commands = compose_registration_review_commands(DatabaseRuntime.from_engine(migrated_engine))

        created = commands.approve(command)
        retried = commands.approve(command)

        assert created.exact_retry is False
        assert retried.exact_retry is True
        assert retried.persona_id == created.persona_id
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(DiscordAccountORM.persona_id).where(
                        DiscordAccountORM.discord_user_id == submission.actor_discord_user_id
                    )
                )
                == created.persona_id
            )
            assert (
                connection.scalar(select(CirclePointORM.balance).where(CirclePointORM.persona_id == created.persona_id))
                == 500
            )
            point = connection.execute(
                select(PointTransactionORM.action, PointTransactionORM.amount).where(
                    PointTransactionORM.persona_id == created.persona_id
                )
            ).one()
            assert point.action == "initial_grant"
            assert point.amount == 500
            assert (
                connection.scalar(
                    select(GameAccountRegistrationRequestORM.status).where(
                        GameAccountRegistrationRequestORM.id == request.request_id
                    )
                )
                == "approved"
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(IdentityOperationORM)
                    .where(IdentityOperationORM.type == "account_registration_approved")
                )
                >= 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(DiscordPublicationORM)
                    .where(DiscordPublicationORM.guild_id == guild_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id)


def test_rejection_is_terminal_zero_bootstrap_and_exact_retry(migrated_engine: Engine) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    submission = _submission(suffix=suffix, guild_id=guild_id)
    try:
        request = _seed_request(migrated_engine, submission=submission)
        command = _reject(request, suffix=suffix)
        commands = compose_registration_review_commands(DatabaseRuntime.from_engine(migrated_engine))

        created = commands.reject(command)
        retried = commands.reject(command)

        assert created.exact_retry is False
        assert retried.exact_retry is True
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(GameAccountRegistrationRequestORM.status).where(
                        GameAccountRegistrationRequestORM.id == request.request_id
                    )
                )
                == "cancelled"
            )
            assert (
                connection.scalar(
                    select(DiscordAccountORM.persona_id).where(
                        DiscordAccountORM.discord_user_id == submission.actor_discord_user_id
                    )
                )
                is None
            )
            assert (
                connection.scalar(
                    select(func.count()).select_from(GameAccountORM).where(GameAccountORM.uma_pid == submission.uma_pid)
                )
                == 0
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(DiscordPublicationORM)
                    .where(DiscordPublicationORM.guild_id == guild_id)
                )
                == 0
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id)


def test_concurrent_approve_and_reject_serialize_to_one_terminal_outcome(
    migrated_engine: Engine,
) -> None:
    suffix = _suffix()
    guild_id = f"6{suffix}"
    submission = _submission(suffix=suffix, guild_id=guild_id)
    barrier = Barrier(2)
    try:
        request = _seed_request(migrated_engine, submission=submission)

        def approve() -> str:
            commands = compose_registration_review_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                commands.approve(_approve(request, suffix=f"1{suffix[:-1]}"))
            except StaffRegistrationRequestNotPendingError:
                return "lost"
            return "approved"

        def reject() -> str:
            commands = compose_registration_review_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                commands.reject(_reject(request, suffix=f"2{suffix[:-1]}"))
            except StaffRegistrationRequestNotPendingError:
                return "lost"
            return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[str], ...] = (executor.submit(approve), executor.submit(reject))
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert "lost" in outcomes
        assert len({*outcomes} - {"lost"}) == 1
        with migrated_engine.connect() as connection:
            status = connection.scalar(
                select(GameAccountRegistrationRequestORM.status).where(
                    GameAccountRegistrationRequestORM.id == request.request_id
                )
            )
            assert status in {"approved", "cancelled"}
            terminal_operations = connection.scalar(
                select(func.count())
                .select_from(IdentityOperationORM)
                .join(OperationORM, OperationORM.id == IdentityOperationORM.operation_id)
                .where(
                    OperationORM.guild_id == guild_id,
                    IdentityOperationORM.type.in_(("account_registration_approved", "account_registration_rejected")),
                )
            )
            assert terminal_operations == 1
    finally:
        _cleanup(migrated_engine, guild_id=guild_id)


def test_concurrent_same_pid_approvals_fail_closed_to_one_owner(migrated_engine: Engine) -> None:
    first_suffix = _suffix()
    second_suffix = _suffix()
    guild_id = f"6{first_suffix}"
    shared_pid = f"5{_suffix()}"
    first = _submission(suffix=first_suffix, guild_id=guild_id, uma_pid=shared_pid)
    second = _submission(suffix=second_suffix, guild_id=guild_id, uma_pid=shared_pid)
    barrier = Barrier(2)
    try:
        first_request = _seed_request(migrated_engine, submission=first)
        second_request = _seed_request(migrated_engine, submission=second)

        def approve(request: StaffRegistrationRequestState, suffix: str) -> str:
            commands = compose_registration_review_commands(DatabaseRuntime.from_engine(migrated_engine))
            barrier.wait()
            try:
                commands.approve(_approve(request, suffix=suffix))
            except StaffRegistrationReviewConcurrentConflictError:
                return "concurrent-conflict"
            except StaffRegistrationPidUnavailableError:
                return "pid-unavailable"
            return "approved"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(approve, first_request, first_suffix),
                executor.submit(approve, second_request, second_suffix),
            )
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert outcomes.count("approved") == 1
        assert len({*outcomes} - {"approved"}) == 1
        assert ({*outcomes} - {"approved"}).pop() in {
            "concurrent-conflict",
            "pid-unavailable",
        }
        queries = compose_staff_registration_review_queries(DatabaseRuntime.from_engine(migrated_engine))
        pending = queries.list_pending_requests(guild_id=guild_id, page=0)
        assert len(pending.items) == 1
        retry_command = _approve(pending.items[0], suffix=_suffix())
        with pytest.raises(StaffRegistrationPidUnavailableError):
            compose_registration_review_commands(DatabaseRuntime.from_engine(migrated_engine)).approve(retry_command)
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count()).select_from(GameAccountORM).where(GameAccountORM.uma_pid == shared_pid)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(PointTransactionORM)
                    .where(PointTransactionORM.action == "initial_grant")
                    .join(PersonaORM, PersonaORM.id == PointTransactionORM.persona_id)
                    .join(DiscordAccountORM, DiscordAccountORM.persona_id == PersonaORM.id)
                    .where(
                        DiscordAccountORM.discord_user_id.in_(
                            (first.actor_discord_user_id, second.actor_discord_user_id)
                        )
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, guild_id=guild_id)
