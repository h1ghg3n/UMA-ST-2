"""MariaDB Match result candidate revision, audit, and exact-retry evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from uma_st2.application.execution import CommandRunner, QueryRunner
from uma_st2.application.match import (
    ConfirmMatchResultSubmission,
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultConfirmationCommands,
    MatchResultConfirmationStaleError,
    MatchResultRejectionCommands,
    MatchResultReviewQueries,
    MatchResultReviewStaleError,
    MatchResultReviewUnavailableError,
    MatchResultSubmissionCommands,
    MatchResultSubmissionReasonRequiredError,
    MatchResultSubmissionStaleError,
    MatchResultSubmissionTarget,
    MatchStaffResultSubmissionQueries,
    RejectMatchResultSubmission,
    SaveMatchResultSubmission,
)
from uma_st2.domain.match import MatchResultSourceKind
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyMatchResultConfirmationUnitOfWorkFactory,
    SqlAlchemyMatchResultRejectionUnitOfWorkFactory,
    SqlAlchemyMatchResultReviewQueryUnitOfWorkFactory,
    SqlAlchemyMatchResultSubmissionUnitOfWorkFactory,
    SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWorkFactory,
)
from uma_st2.infrastructure.database.orm import (
    GameAccountORM,
    MatchEntryORM,
    MatchOperationORM,
    MatchORM,
    MatchResultSubmissionORM,
    OperationORM,
    PersonaORM,
    StadiumCourseORM,
    StadiumORM,
    UmamusumeORM,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededResultMatch:
    match_id: int
    guild_id: str
    stadium_id: int
    course_id: int
    persona_ids: tuple[str, ...]
    account_ids: tuple[int, ...]
    umamusume_ids: tuple[int, ...]
    entry_ids: tuple[int, ...]


def _seed_result_match(engine: Engine, *, suffix: str) -> SeededResultMatch:
    stored_now = (NOW - timedelta(days=1)).replace(tzinfo=None)
    external_base = int(suffix[:12], 16)
    guild_id = str(int(suffix[:15], 16) + 1)
    persona_ids = tuple(str(uuid4()) for _ in range(3))
    with engine.begin() as connection:
        connection.execute(
            PersonaORM.__table__.insert(),
            [
                {
                    "id": persona_id,
                    "display_name": f"Persona {index} {suffix}",
                    "status": "normal",
                    "created_at": stored_now,
                    "updated_at": stored_now,
                }
                for index, persona_id in enumerate(persona_ids, start=1)
            ],
        )
        account_ids = tuple(
            connection.execute(
                GameAccountORM.__table__.insert().values(
                    persona_id=persona_id,
                    game_region="KR",
                    uma_pid=f"{index}{suffix[:15]}",
                    nickname=f"Account {index} {suffix}",
                    affiliation="A조",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, persona_id in enumerate(persona_ids, start=1)
        )
        umamusume_ids = tuple(
            connection.execute(
                UmamusumeORM.__table__.insert().values(
                    external_id=external_base + index,
                    name_jp=f"Horse JP {index} {suffix}",
                    name_ko=f"말 {index} {suffix}",
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index in range(1, 4)
        )
        stadium_id = connection.execute(
            StadiumORM.__table__.insert().values(
                external_id=external_base + 100,
                name_jp=f"Tokyo {suffix}",
                name_ko=f"도쿄 {suffix}",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        course_id = connection.execute(
            StadiumCourseORM.__table__.insert().values(
                stadium_id=stadium_id,
                external_id=external_base + 101,
                surface="turf",
                distance=2400,
                direction="left",
                layout="standard",
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        match_id = connection.execute(
            MatchORM.__table__.insert().values(
                name=f"Result Match {suffix}",
                description="공식 룸매치",
                source_kind="native_v2",
                grade="G1",
                stadium_course_id=course_id,
                scheduled_at=SCHEDULED_AT.replace(tzinfo=None),
                status="betting_closed",
                terminal_reason=None,
                finish_time_ms=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
        ).inserted_primary_key[0]
        entry_ids = tuple(
            connection.execute(
                MatchEntryORM.__table__.insert().values(
                    match_id=match_id,
                    game_account_id=account_id,
                    owner_at_event_persona_id=persona_id,
                    affiliation_at_event="A조",
                    umamusume_id=umamusume_id,
                    umamusume_variant_id=None,
                    entry_number=index,
                    running_style=None,
                    training_grade=None,
                    rank=None,
                    popularity_rank=None,
                    margin=None,
                    created_at=stored_now,
                    updated_at=stored_now,
                )
            ).inserted_primary_key[0]
            for index, (account_id, persona_id, umamusume_id) in enumerate(
                zip(account_ids, persona_ids, umamusume_ids, strict=True),
                start=1,
            )
        )
    return SeededResultMatch(
        match_id=match_id,
        guild_id=guild_id,
        stadium_id=stadium_id,
        course_id=course_id,
        persona_ids=persona_ids,
        account_ids=account_ids,
        umamusume_ids=umamusume_ids,
        entry_ids=entry_ids,
    )


def _services(
    engine: Engine, *, now: datetime = NOW
) -> tuple[MatchResultSubmissionCommands, MatchStaffResultSubmissionQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchResultSubmissionCommands(
            CommandRunner(SqlAlchemyMatchResultSubmissionUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: now,
        ),
        MatchStaffResultSubmissionQueries(
            QueryRunner(SqlAlchemyMatchStaffResultSubmissionQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _candidate(seeded: SeededResultMatch, *, order: tuple[int, int, int] = (0, 1, 2)) -> MatchResultCandidate:
    return MatchResultCandidate(
        entries=tuple(
            MatchResultCandidateEntry(
                entry_id=seeded.entry_ids[entry_index],
                entry_number=entry_index + 1,
                rank=rank,
                popularity_rank=rank,
                margin=None if rank == 1 else f"{rank - 1}/2마신",
            )
            for rank, entry_index in enumerate(order, start=1)
        ),
        finish_time_ms=92_300,
    )


def _review_services(
    engine: Engine, *, now: datetime = NOW
) -> tuple[MatchResultRejectionCommands, MatchResultReviewQueries]:
    runtime = DatabaseRuntime.from_engine(engine)
    return (
        MatchResultRejectionCommands(
            CommandRunner(SqlAlchemyMatchResultRejectionUnitOfWorkFactory(runtime.session_factory)),
            clock=lambda: now,
        ),
        MatchResultReviewQueries(
            QueryRunner(SqlAlchemyMatchResultReviewQueryUnitOfWorkFactory(runtime.session_factory))
        ),
    )


def _confirmation_commands(
    engine: Engine,
    *,
    now: datetime = NOW,
) -> MatchResultConfirmationCommands:
    runtime = DatabaseRuntime.from_engine(engine)
    return MatchResultConfirmationCommands(
        CommandRunner(SqlAlchemyMatchResultConfirmationUnitOfWorkFactory(runtime.session_factory)),
        clock=lambda: now,
    )


def _confirmation_request(
    target: MatchResultSubmissionTarget,
    *,
    guild_id: str,
    key: str,
) -> ConfirmMatchResultSubmission:
    assert target.pending is not None
    return ConfirmMatchResultSubmission(
        match_id=target.match_id,
        submission_id=target.pending.submission_id,
        expected_state_fingerprint=target.state_fingerprint,
        expected_candidate_fingerprint=target.pending.candidate.fingerprint,
        actor_discord_user_id="123456789",
        guild_id=guild_id,
        idempotency_key=key,
        correlation_id=key,
    )


def _rejection_request(
    target: MatchResultSubmissionTarget,
    *,
    guild_id: str,
    key: str,
    reason: str = "공식 결과와 다름",
) -> RejectMatchResultSubmission:
    assert target.pending is not None
    return RejectMatchResultSubmission(
        match_id=target.match_id,
        submission_id=target.pending.submission_id,
        expected_state_fingerprint=target.state_fingerprint,
        expected_candidate_fingerprint=target.pending.candidate.fingerprint,
        reason=reason,
        actor_discord_user_id="123456789",
        guild_id=guild_id,
        idempotency_key=key,
        correlation_id=key,
    )


def _request(
    seeded: SeededResultMatch,
    *,
    fingerprint: str,
    candidate: MatchResultCandidate,
    key: str,
    reason: str | None = None,
) -> SaveMatchResultSubmission:
    return SaveMatchResultSubmission(
        match_id=seeded.match_id,
        expected_state_fingerprint=fingerprint,
        candidate=candidate,
        source_kind=MatchResultSourceKind.MANUAL,
        actor_discord_user_id="123456789",
        guild_id=seeded.guild_id,
        idempotency_key=key,
        correlation_id=key,
        reason=reason,
    )


def _cleanup(engine: Engine, seeded: SeededResultMatch) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(
            connection.scalars(
                select(MatchOperationORM.operation_id).where(MatchOperationORM.match_id == seeded.match_id)
            )
        )
        connection.execute(delete(MatchResultSubmissionORM).where(MatchResultSubmissionORM.match_id == seeded.match_id))
        if operation_ids:
            connection.execute(delete(MatchOperationORM).where(MatchOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(MatchEntryORM).where(MatchEntryORM.match_id == seeded.match_id))
        connection.execute(delete(MatchORM).where(MatchORM.id == seeded.match_id))
        connection.execute(delete(StadiumCourseORM).where(StadiumCourseORM.id == seeded.course_id))
        connection.execute(delete(StadiumORM).where(StadiumORM.id == seeded.stadium_id))
        connection.execute(delete(UmamusumeORM).where(UmamusumeORM.id.in_(seeded.umamusume_ids)))
        connection.execute(delete(GameAccountORM).where(GameAccountORM.id.in_(seeded.account_ids)))
        connection.execute(delete(PersonaORM).where(PersonaORM.id.in_(seeded.persona_ids)))


def test_pending_revision_replaces_only_pending_and_preserves_canonical_result(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        choices = queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.next_revision_number) for choice in choices] == [(seeded.match_id, 1)]
        initial_target = queries.get_target(match_id=seeded.match_id)
        initial_request = _request(
            seeded,
            fingerprint=initial_target.state_fingerprint,
            candidate=_candidate(seeded),
            key=f"result-initial-{suffix}",
        )
        initial = commands.submit(initial_request)
        assert commands.submit(initial_request) == initial

        revision_target = queries.get_target(match_id=seeded.match_id)
        assert revision_target.pending is not None
        assert revision_target.next_revision_number == 2
        revision_request = _request(
            seeded,
            fingerprint=revision_target.state_fingerprint,
            candidate=_candidate(seeded, order=(1, 0, 2)),
            key=f"result-revision-{suffix}",
            reason="입력 정정",
        )
        revision_commands, _ = _services(migrated_engine, now=NOW + timedelta(minutes=1))
        revised = revision_commands.submit(revision_request)
        assert revised.revision_number == 2
        assert revised.superseded_submission_id == initial.submission_id
        assert revision_commands.submit(initial_request) == initial

        with pytest.raises(MatchResultSubmissionStaleError):
            revision_commands.submit(
                _request(
                    seeded,
                    fingerprint=initial_target.state_fingerprint,
                    candidate=_candidate(seeded),
                    key=f"result-stale-{suffix}",
                )
            )

        with migrated_engine.connect() as connection:
            match_row = connection.execute(
                select(MatchORM.status, MatchORM.finish_time_ms).where(MatchORM.id == seeded.match_id)
            ).one()
            entries = tuple(
                connection.execute(
                    select(MatchEntryORM.rank, MatchEntryORM.popularity_rank, MatchEntryORM.margin)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.entry_number)
                )
            )
            submissions = tuple(
                connection.execute(
                    select(
                        MatchResultSubmissionORM.id,
                        MatchResultSubmissionORM.revision_number,
                        MatchResultSubmissionORM.status,
                        MatchResultSubmissionORM.pending_marker,
                        MatchResultSubmissionORM.confirmed_marker,
                    )
                    .where(MatchResultSubmissionORM.match_id == seeded.match_id)
                    .order_by(MatchResultSubmissionORM.revision_number)
                )
            )
            audits = tuple(
                connection.execute(
                    select(
                        MatchOperationORM.type,
                        MatchOperationORM.before_data,
                        MatchOperationORM.after_data,
                        OperationORM.reason,
                    )
                    .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                    .where(MatchOperationORM.match_id == seeded.match_id)
                    .order_by(MatchOperationORM.operation_id)
                )
            )
            stale_operation_count = connection.scalar(
                select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == f"result-stale-{suffix}")
            )

        assert match_row == ("betting_closed", None)
        assert entries == ((None, None, None),) * 3
        assert [
            (row.id, row.revision_number, row.status, row.pending_marker, row.confirmed_marker) for row in submissions
        ] == [
            (initial.submission_id, 1, "superseded", None, None),
            (revised.submission_id, 2, "pending", True, None),
        ]
        assert [row.type for row in audits] == ["match_result_submitted", "match_result_revised"]
        assert audits[0].after_data["candidate_fingerprint"] == initial.candidate_fingerprint
        assert audits[1].before_data["pending"]["submission_id"] == initial.submission_id
        assert audits[1].reason == "입력 정정"
        assert stale_operation_count == 0
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_exact_retry_converges_to_one_pending_revision(migrated_engine: Engine) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    _, queries = _services(migrated_engine)
    target = queries.get_target(match_id=seeded.match_id)
    request = _request(
        seeded,
        fingerprint=target.state_fingerprint,
        candidate=_candidate(seeded),
        key=f"result-concurrent-{suffix}",
    )
    start = Barrier(2)

    def run() -> int:
        commands, _ = _services(migrated_engine)
        start.wait()
        return commands.submit(request).submission_id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            submission_ids = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert submission_ids[0] == submission_ids[1]
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count(MatchResultSubmissionORM.id)).where(
                        MatchResultSubmissionORM.match_id == seeded.match_id
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_result_submitted",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_confirmed_correction_requires_reason_and_keeps_confirmed_authority(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    commands, queries = _services(migrated_engine)
    try:
        target = queries.get_target(match_id=seeded.match_id)
        initial = commands.submit(
            _request(
                seeded,
                fingerprint=target.state_fingerprint,
                candidate=_candidate(seeded),
                key=f"result-confirm-source-{suffix}",
            )
        )
        candidate = initial.candidate
        stored_now = NOW.replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            connection.execute(
                update(MatchResultSubmissionORM)
                .where(MatchResultSubmissionORM.id == initial.submission_id)
                .values(
                    status="confirmed",
                    pending_marker=None,
                    confirmed_marker=True,
                    confirmed_at=stored_now,
                    updated_at=stored_now,
                )
            )
            connection.execute(
                update(MatchORM)
                .where(MatchORM.id == seeded.match_id)
                .values(status="result_confirmed", finish_time_ms=candidate.finish_time_ms)
            )
            for entry in candidate.entries:
                connection.execute(
                    update(MatchEntryORM)
                    .where(MatchEntryORM.id == entry.entry_id)
                    .values(
                        rank=entry.rank,
                        popularity_rank=entry.popularity_rank,
                        margin=entry.margin,
                    )
                )

        confirmed_target = queries.get_target(match_id=seeded.match_id)
        assert confirmed_target.confirmed is not None
        correction = _candidate(seeded, order=(1, 0, 2))
        with pytest.raises(MatchResultSubmissionReasonRequiredError):
            commands.submit(
                _request(
                    seeded,
                    fingerprint=confirmed_target.state_fingerprint,
                    candidate=correction,
                    key=f"result-confirmed-no-reason-{suffix}",
                )
            )

        corrected = commands.submit(
            _request(
                seeded,
                fingerprint=confirmed_target.state_fingerprint,
                candidate=correction,
                key=f"result-confirmed-correction-{suffix}",
                reason="확정 결과 입력 정정",
            )
        )
        assert corrected.corrects_confirmed is True
        assert corrected.revision_number == 2

        with migrated_engine.connect() as connection:
            current = tuple(
                connection.execute(
                    select(
                        MatchResultSubmissionORM.id,
                        MatchResultSubmissionORM.status,
                        MatchResultSubmissionORM.pending_marker,
                        MatchResultSubmissionORM.confirmed_marker,
                    )
                    .where(MatchResultSubmissionORM.match_id == seeded.match_id)
                    .order_by(MatchResultSubmissionORM.revision_number)
                )
            )
            materialized = tuple(
                connection.execute(
                    select(MatchEntryORM.id, MatchEntryORM.rank)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.rank)
                )
            )
            failed_operation_count = connection.scalar(
                select(func.count(OperationORM.id)).where(
                    OperationORM.idempotency_key == f"result-confirmed-no-reason-{suffix}"
                )
            )

        assert current == (
            (initial.submission_id, "confirmed", None, True),
            (corrected.submission_id, "pending", True, None),
        )
        assert [row.id for row in materialized] == [entry.entry_id for entry in candidate.entries]
        assert failed_operation_count == 0
    finally:
        _cleanup(migrated_engine, seeded)


def test_pending_result_review_rejects_only_current_revision_with_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    submission_commands, submission_queries = _services(migrated_engine)
    rejection_commands, review_queries = _review_services(migrated_engine, now=NOW + timedelta(minutes=2))
    try:
        initial_target = submission_queries.get_target(match_id=seeded.match_id)
        initial = submission_commands.submit(
            _request(
                seeded,
                fingerprint=initial_target.state_fingerprint,
                candidate=_candidate(seeded),
                key=f"result-review-source-{suffix}",
            )
        )
        stale_review = review_queries.get_target(match_id=seeded.match_id)
        revision_target = submission_queries.get_target(match_id=seeded.match_id)
        revised = submission_commands.submit(
            _request(
                seeded,
                fingerprint=revision_target.state_fingerprint,
                candidate=_candidate(seeded, order=(1, 0, 2)),
                key=f"result-review-revision-{suffix}",
                reason="pending 입력 정정",
            )
        )

        stale_key = f"result-reject-stale-{suffix}"
        with pytest.raises(MatchResultReviewStaleError):
            rejection_commands.reject(_rejection_request(stale_review, guild_id=seeded.guild_id, key=stale_key))

        choices = review_queries.search_targets(search=suffix, limit=25)
        assert [(choice.match_id, choice.submission_id) for choice in choices] == [
            (seeded.match_id, revised.submission_id)
        ]
        current = review_queries.get_target(match_id=seeded.match_id)
        request = _rejection_request(
            current,
            guild_id=seeded.guild_id,
            key=f"result-reject-{suffix}",
        )
        rejected = rejection_commands.reject(request)
        assert rejection_commands.reject(request) == rejected

        with migrated_engine.connect() as connection:
            match_row = connection.execute(
                select(MatchORM.status, MatchORM.finish_time_ms).where(MatchORM.id == seeded.match_id)
            ).one()
            entries = tuple(
                connection.execute(
                    select(MatchEntryORM.rank, MatchEntryORM.popularity_rank, MatchEntryORM.margin)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.entry_number)
                )
            )
            submissions = tuple(
                connection.execute(
                    select(
                        MatchResultSubmissionORM.id,
                        MatchResultSubmissionORM.status,
                        MatchResultSubmissionORM.pending_marker,
                        MatchResultSubmissionORM.confirmed_marker,
                        MatchResultSubmissionORM.rejection_reason,
                    )
                    .where(MatchResultSubmissionORM.match_id == seeded.match_id)
                    .order_by(MatchResultSubmissionORM.revision_number)
                )
            )
            reject_audits = tuple(
                connection.execute(
                    select(MatchOperationORM.type, MatchOperationORM.after_data, OperationORM.reason)
                    .join(OperationORM, OperationORM.id == MatchOperationORM.operation_id)
                    .where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_result_rejected",
                    )
                )
            )
            stale_operation_count = connection.scalar(
                select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == stale_key)
            )

        assert rejected.submission_id == revised.submission_id
        assert rejected.preserved_confirmed_submission_id is None
        assert match_row == ("betting_closed", None)
        assert entries == ((None, None, None),) * 3
        assert submissions == (
            (initial.submission_id, "superseded", None, None, None),
            (revised.submission_id, "rejected", None, None, "공식 결과와 다름"),
        )
        assert len(reject_audits) == 1
        assert reject_audits[0].after_data["submission_id"] == revised.submission_id
        assert reject_audits[0].reason == "공식 결과와 다름"
        assert stale_operation_count == 0
    finally:
        _cleanup(migrated_engine, seeded)


def test_pending_rejection_preserves_confirmed_authority_and_materialized_board(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    submission_commands, submission_queries = _services(migrated_engine)
    rejection_commands, review_queries = _review_services(migrated_engine, now=NOW + timedelta(minutes=2))
    try:
        target = submission_queries.get_target(match_id=seeded.match_id)
        confirmed = submission_commands.submit(
            _request(
                seeded,
                fingerprint=target.state_fingerprint,
                candidate=_candidate(seeded),
                key=f"result-reject-confirmed-source-{suffix}",
            )
        )
        stored_now = NOW.replace(tzinfo=None)
        with migrated_engine.begin() as connection:
            connection.execute(
                update(MatchResultSubmissionORM)
                .where(MatchResultSubmissionORM.id == confirmed.submission_id)
                .values(
                    status="confirmed",
                    pending_marker=None,
                    confirmed_marker=True,
                    confirmed_at=stored_now,
                    updated_at=stored_now,
                )
            )
            connection.execute(
                update(MatchORM)
                .where(MatchORM.id == seeded.match_id)
                .values(status="result_confirmed", finish_time_ms=confirmed.candidate.finish_time_ms)
            )
            for entry in confirmed.candidate.entries:
                connection.execute(
                    update(MatchEntryORM)
                    .where(MatchEntryORM.id == entry.entry_id)
                    .values(
                        rank=entry.rank,
                        popularity_rank=entry.popularity_rank,
                        margin=entry.margin,
                    )
                )

        correction_target = submission_queries.get_target(match_id=seeded.match_id)
        pending = submission_commands.submit(
            _request(
                seeded,
                fingerprint=correction_target.state_fingerprint,
                candidate=_candidate(seeded, order=(1, 0, 2)),
                key=f"result-reject-correction-{suffix}",
                reason="확정 결과 정정 후보",
            )
        )
        review = review_queries.get_target(match_id=seeded.match_id)
        rejected = rejection_commands.reject(
            _rejection_request(
                review,
                guild_id=seeded.guild_id,
                key=f"result-reject-confirmed-{suffix}",
                reason="정정 후보 폐기",
            )
        )

        with migrated_engine.connect() as connection:
            match_row = connection.execute(
                select(MatchORM.status, MatchORM.finish_time_ms).where(MatchORM.id == seeded.match_id)
            ).one()
            materialized = tuple(
                connection.execute(
                    select(MatchEntryORM.id, MatchEntryORM.rank)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.rank)
                )
            )
            submissions = tuple(
                connection.execute(
                    select(
                        MatchResultSubmissionORM.id,
                        MatchResultSubmissionORM.status,
                        MatchResultSubmissionORM.pending_marker,
                        MatchResultSubmissionORM.confirmed_marker,
                    )
                    .where(MatchResultSubmissionORM.match_id == seeded.match_id)
                    .order_by(MatchResultSubmissionORM.revision_number)
                )
            )

        assert rejected.submission_id == pending.submission_id
        assert rejected.preserved_confirmed_submission_id == confirmed.submission_id
        assert match_row == ("result_confirmed", confirmed.candidate.finish_time_ms)
        assert [row.id for row in materialized] == [entry.entry_id for entry in confirmed.candidate.entries]
        assert submissions == (
            (confirmed.submission_id, "confirmed", None, True),
            (pending.submission_id, "rejected", None, None),
        )
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_pending_rejection_exact_retry_converges_to_one_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    submission_commands, submission_queries = _services(migrated_engine)
    try:
        target = submission_queries.get_target(match_id=seeded.match_id)
        submission_commands.submit(
            _request(
                seeded,
                fingerprint=target.state_fingerprint,
                candidate=_candidate(seeded),
                key=f"result-reject-concurrent-source-{suffix}",
            )
        )
        _, review_queries = _review_services(migrated_engine)
        review = review_queries.get_target(match_id=seeded.match_id)
        request = _rejection_request(
            review,
            guild_id=seeded.guild_id,
            key=f"result-reject-concurrent-{suffix}",
        )
        start = Barrier(2)

        def run() -> int:
            commands, _ = _review_services(migrated_engine, now=NOW + timedelta(minutes=2))
            start.wait()
            return commands.reject(request).submission_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            submission_ids = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert submission_ids[0] == submission_ids[1]
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_result_rejected",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)


def test_result_confirmation_materializes_complete_board_and_exact_retry(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    submission_commands, submission_queries = _services(migrated_engine)
    confirmation_commands = _confirmation_commands(migrated_engine, now=NOW + timedelta(minutes=2))
    _, review_queries = _review_services(migrated_engine)
    try:
        target = submission_queries.get_target(match_id=seeded.match_id)
        pending = submission_commands.submit(
            _request(
                seeded,
                fingerprint=target.state_fingerprint,
                candidate=_candidate(seeded, order=(1, 0, 2)),
                key=f"result-confirm-submit-{suffix}",
            )
        )
        preview = review_queries.get_target(match_id=seeded.match_id)
        request = _confirmation_request(
            preview,
            guild_id=seeded.guild_id,
            key=f"result-confirm-{suffix}",
        )

        confirmed = confirmation_commands.confirm(request)
        assert confirmation_commands.confirm(request) == confirmed

        with migrated_engine.connect() as connection:
            match_row = connection.execute(
                select(MatchORM.status, MatchORM.finish_time_ms).where(MatchORM.id == seeded.match_id)
            ).one()
            entries = tuple(
                connection.execute(
                    select(
                        MatchEntryORM.id,
                        MatchEntryORM.entry_number,
                        MatchEntryORM.rank,
                        MatchEntryORM.popularity_rank,
                        MatchEntryORM.margin,
                    )
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.rank)
                )
            )
            submission = connection.execute(
                select(
                    MatchResultSubmissionORM.status,
                    MatchResultSubmissionORM.pending_marker,
                    MatchResultSubmissionORM.confirmed_marker,
                    MatchResultSubmissionORM.confirmed_operation_id,
                    MatchResultSubmissionORM.confirmed_at,
                ).where(MatchResultSubmissionORM.id == pending.submission_id)
            ).one()
            audits = tuple(
                connection.execute(
                    select(MatchOperationORM.type, MatchOperationORM.after_data).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_result_confirmed",
                    )
                )
            )

        assert confirmed.submission_id == pending.submission_id
        assert confirmed.previous_confirmed_submission_id is None
        assert match_row == ("result_confirmed", pending.candidate.finish_time_ms)
        assert [row.id for row in entries] == [entry.entry_id for entry in pending.candidate.entries]
        assert [(row.entry_number, row.rank, row.popularity_rank, row.margin) for row in entries] == [
            (entry.entry_number, entry.rank, entry.popularity_rank, entry.margin) for entry in pending.candidate.entries
        ]
        assert submission.status == "confirmed"
        assert submission.pending_marker is None
        assert submission.confirmed_marker is True
        assert submission.confirmed_operation_id is not None
        assert submission.confirmed_at == (NOW + timedelta(minutes=2)).replace(tzinfo=None)
        assert len(audits) == 1
        assert audits[0].after_data["candidate_fingerprint"] == pending.candidate.fingerprint
        with pytest.raises(MatchResultReviewUnavailableError):
            review_queries.get_target(match_id=seeded.match_id)
    finally:
        _cleanup(migrated_engine, seeded)


def test_result_reconfirmation_supersedes_previous_and_stale_preview_is_zero_write(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    submission_commands, submission_queries = _services(migrated_engine)
    _, review_queries = _review_services(migrated_engine)
    try:
        initial_target = submission_queries.get_target(match_id=seeded.match_id)
        initial_pending = submission_commands.submit(
            _request(
                seeded,
                fingerprint=initial_target.state_fingerprint,
                candidate=_candidate(seeded),
                key=f"result-reconfirm-initial-submit-{suffix}",
            )
        )
        initial_preview = review_queries.get_target(match_id=seeded.match_id)
        initial_confirmed = _confirmation_commands(migrated_engine).confirm(
            _confirmation_request(
                initial_preview,
                guild_id=seeded.guild_id,
                key=f"result-reconfirm-initial-{suffix}",
            )
        )

        correction_target = submission_queries.get_target(match_id=seeded.match_id)
        first_correction = submission_commands.submit(
            _request(
                seeded,
                fingerprint=correction_target.state_fingerprint,
                candidate=_candidate(seeded, order=(1, 0, 2)),
                key=f"result-reconfirm-first-correction-{suffix}",
                reason="확정 결과 정정",
            )
        )
        stale_preview = review_queries.get_target(match_id=seeded.match_id)
        replacement_target = submission_queries.get_target(match_id=seeded.match_id)
        replacement = submission_commands.submit(
            _request(
                seeded,
                fingerprint=replacement_target.state_fingerprint,
                candidate=_candidate(seeded, order=(2, 1, 0)),
                key=f"result-reconfirm-replacement-{suffix}",
                reason="정정 후보 재입력",
            )
        )
        stale_key = f"result-reconfirm-stale-{suffix}"
        with pytest.raises(MatchResultConfirmationStaleError):
            _confirmation_commands(migrated_engine).confirm(
                _confirmation_request(
                    stale_preview,
                    guild_id=seeded.guild_id,
                    key=stale_key,
                )
            )

        current_preview = review_queries.get_target(match_id=seeded.match_id)
        corrected = _confirmation_commands(migrated_engine, now=NOW + timedelta(minutes=3)).confirm(
            _confirmation_request(
                current_preview,
                guild_id=seeded.guild_id,
                key=f"result-reconfirm-final-{suffix}",
            )
        )

        with migrated_engine.connect() as connection:
            submissions = tuple(
                connection.execute(
                    select(
                        MatchResultSubmissionORM.id,
                        MatchResultSubmissionORM.status,
                        MatchResultSubmissionORM.pending_marker,
                        MatchResultSubmissionORM.confirmed_marker,
                    )
                    .where(MatchResultSubmissionORM.match_id == seeded.match_id)
                    .order_by(MatchResultSubmissionORM.revision_number)
                )
            )
            materialized = tuple(
                connection.scalars(
                    select(MatchEntryORM.id)
                    .where(MatchEntryORM.match_id == seeded.match_id)
                    .order_by(MatchEntryORM.rank)
                )
            )
            stale_operations = connection.scalar(
                select(func.count(OperationORM.id)).where(OperationORM.idempotency_key == stale_key)
            )

        assert initial_confirmed.submission_id == initial_pending.submission_id
        assert first_correction.submission_id != replacement.submission_id
        assert corrected.submission_id == replacement.submission_id
        assert corrected.previous_confirmed_submission_id == initial_confirmed.submission_id
        assert submissions == (
            (initial_confirmed.submission_id, "superseded", None, None),
            (first_correction.submission_id, "superseded", None, None),
            (replacement.submission_id, "confirmed", None, True),
        )
        assert materialized == tuple(entry.entry_id for entry in replacement.candidate.entries)
        assert stale_operations == 0
    finally:
        _cleanup(migrated_engine, seeded)


def test_concurrent_result_confirmation_exact_retry_converges_to_one_audit(
    migrated_engine: Engine,
) -> None:
    suffix = uuid4().hex
    seeded = _seed_result_match(migrated_engine, suffix=suffix)
    submission_commands, submission_queries = _services(migrated_engine)
    try:
        target = submission_queries.get_target(match_id=seeded.match_id)
        submission_commands.submit(
            _request(
                seeded,
                fingerprint=target.state_fingerprint,
                candidate=_candidate(seeded),
                key=f"result-confirm-concurrent-submit-{suffix}",
            )
        )
        _, review_queries = _review_services(migrated_engine)
        preview = review_queries.get_target(match_id=seeded.match_id)
        request = _confirmation_request(
            preview,
            guild_id=seeded.guild_id,
            key=f"result-confirm-concurrent-{suffix}",
        )
        start = Barrier(2)

        def run() -> int:
            commands = _confirmation_commands(migrated_engine, now=NOW + timedelta(minutes=2))
            start.wait()
            return commands.confirm(request).submission_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            submission_ids = tuple(future.result() for future in (executor.submit(run), executor.submit(run)))

        assert submission_ids[0] == submission_ids[1]
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count(MatchOperationORM.operation_id)).where(
                        MatchOperationORM.match_id == seeded.match_id,
                        MatchOperationORM.type == "match_result_confirmed",
                    )
                )
                == 1
            )
    finally:
        _cleanup(migrated_engine, seeded)
