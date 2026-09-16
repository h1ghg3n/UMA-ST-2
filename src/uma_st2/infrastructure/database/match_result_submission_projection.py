"""Shared closed/locked projection builder for Match result submissions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from uma_st2.application.match import (
    MatchResultCandidate,
    MatchResultCandidateEntry,
    MatchResultRevisionReference,
    MatchResultSubmissionEntry,
    MatchResultSubmissionTarget,
)
from uma_st2.domain.match import (
    MatchResultSubmissionStatus,
    MatchSourceKind,
    MatchStatus,
)

from .orm import (
    GameAccountORM,
    MatchEntryORM,
    MatchORM,
    MatchResultSubmissionORM,
    UmamusumeORM,
    UmamusumeVariantORM,
)


def load_match_result_submission_target(
    session: Session,
    *,
    match_id: int,
    lock: bool,
) -> MatchResultSubmissionTarget | None:
    """Build one validated target without leaking ORM rows."""

    match_statement = select(MatchORM).where(MatchORM.id == match_id)
    if lock:
        match_statement = match_statement.with_for_update()
    match = session.scalar(match_statement)
    if match is None:
        return None

    variant = aliased(UmamusumeVariantORM)
    entry_statement = (
        select(
            MatchEntryORM.id,
            MatchEntryORM.entry_number,
            MatchEntryORM.rank,
            MatchEntryORM.popularity_rank,
            MatchEntryORM.margin,
            GameAccountORM.nickname.label("game_account_name"),
            UmamusumeORM.name_ko.label("base_name_ko"),
            UmamusumeORM.name_jp.label("base_name_jp"),
            variant.name_ko.label("variant_name_ko"),
            variant.name_jp.label("variant_name_jp"),
        )
        .join(GameAccountORM, GameAccountORM.id == MatchEntryORM.game_account_id)
        .join(UmamusumeORM, UmamusumeORM.id == MatchEntryORM.umamusume_id)
        .outerjoin(variant, variant.id == MatchEntryORM.umamusume_variant_id)
        .where(MatchEntryORM.match_id == match_id)
        .order_by(MatchEntryORM.entry_number, MatchEntryORM.id)
    )
    if lock:
        entry_statement = entry_statement.with_for_update()
    entry_rows = tuple(session.execute(entry_statement))

    revision_statement = (
        select(MatchResultSubmissionORM)
        .where(MatchResultSubmissionORM.match_id == match_id)
        .order_by(MatchResultSubmissionORM.revision_number, MatchResultSubmissionORM.id)
    )
    if lock:
        revision_statement = revision_statement.with_for_update()
    revision_rows = tuple(session.scalars(revision_statement))

    references: dict[MatchResultSubmissionStatus, MatchResultRevisionReference] = {}
    max_revision = 0
    for row in revision_rows:
        status = MatchResultSubmissionStatus(row.status)
        max_revision = max(max_revision, row.revision_number)
        if (status == MatchResultSubmissionStatus.PENDING) != (row.pending_marker is True):
            raise ValueError("ResultSubmission pending status and marker are inconsistent.")
        if (status == MatchResultSubmissionStatus.CONFIRMED) != (row.confirmed_marker is True):
            raise ValueError("ResultSubmission confirmed status and marker are inconsistent.")
        if status not in {
            MatchResultSubmissionStatus.PENDING,
            MatchResultSubmissionStatus.CONFIRMED,
        }:
            continue
        if status in references:
            raise ValueError(f"Multiple current {status.value} ResultSubmissions exist.")
        references[status] = MatchResultRevisionReference(
            submission_id=row.id,
            revision_number=row.revision_number,
            status=status,
            source_kind=row.source_kind,
            candidate=MatchResultCandidate.from_payload(row.candidate_json),
        )

    entries = tuple(
        MatchResultSubmissionEntry(
            entry_id=row.id,
            entry_number=row.entry_number,
            game_account_name=row.game_account_name,
            horse_name=(
                (row.variant_name_ko or row.variant_name_jp)
                if row.variant_name_ko is not None or row.variant_name_jp is not None
                else (row.base_name_ko or row.base_name_jp)
            ),
        )
        for row in entry_rows
    )
    pending = references.get(MatchResultSubmissionStatus.PENDING)
    confirmed = references.get(MatchResultSubmissionStatus.CONFIRMED)
    target_identity = tuple((entry.entry_id, entry.entry_number) for entry in entries)
    for reference in (pending, confirmed):
        if reference is None:
            continue
        candidate_identity = tuple(
            sorted(
                ((entry.entry_id, entry.entry_number) for entry in reference.candidate.entries),
                key=lambda value: value[1],
            )
        )
        if candidate_identity != target_identity:
            raise ValueError("ResultSubmission candidate does not match current Match Entries.")
    source_kind = MatchSourceKind(match.source_kind)
    status = MatchStatus(match.status)
    if source_kind == MatchSourceKind.NATIVE_V2 and status in {
        MatchStatus.BETTING_CLOSED,
        MatchStatus.RESULT_CONFIRMED,
    }:
        _validate_materialized_authority(
            status=status,
            finish_time_ms=match.finish_time_ms,
            entry_rows=entry_rows,
            confirmed=confirmed,
        )
    return MatchResultSubmissionTarget(
        match_id=match.id,
        match_name=match.name,
        source_kind=source_kind,
        status=status,
        entries=entries,
        next_revision_number=max_revision + 1,
        pending=pending,
        confirmed=confirmed,
    )


def _validate_materialized_authority(
    *,
    status: MatchStatus,
    finish_time_ms: int | None,
    entry_rows: tuple[object, ...],
    confirmed: MatchResultRevisionReference | None,
) -> None:
    if status == MatchStatus.BETTING_CLOSED:
        if confirmed is not None:
            raise ValueError("A betting-closed Match cannot own confirmed authority.")
        if finish_time_ms is not None or any(
            row.rank is not None or row.popularity_rank is not None or row.margin is not None for row in entry_rows
        ):
            raise ValueError("A betting-closed Match cannot contain materialized result fields.")
        return
    if confirmed is None:
        raise ValueError("A result-confirmed Match requires confirmed authority.")
    if any(row.rank is None for row in entry_rows):
        raise ValueError("A result-confirmed Match must have a complete materialized rank board.")
    materialized = MatchResultCandidate(
        entries=tuple(
            MatchResultCandidateEntry(
                entry_id=row.id,
                entry_number=row.entry_number,
                rank=row.rank,
                popularity_rank=row.popularity_rank,
                margin=row.margin,
            )
            for row in entry_rows
        ),
        finish_time_ms=finish_time_ms,
    )
    if materialized != confirmed.candidate:
        raise ValueError("Confirmed candidate and materialized Match result differ.")
