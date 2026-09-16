"""SQLAlchemy implementation of member-owned WIN5 Submission history."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from uma_st2.application.win5.member_identity import Win5MemberPersona
from uma_st2.application.win5.member_queries import (
    Win5MemberResult,
    Win5MemberSubmission,
    Win5MemberSubmissionPick,
    Win5MemberSubmissionRound,
    Win5MemberSubmissionRoundPageSource,
    Win5MemberSubmissionRoundSummary,
    Win5MemberSubmissionsDashboardSource,
    Win5NormalSubmissionJudgement,
    Win5RaceCard,
    Win5RaceEntryOption,
    Win5SpecialSubmissionJudgement,
)
from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5NormalJudgementItem,
    Win5NormalSubmissionScore,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialJudgementItem,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)

from .datetime_codec import from_database_utc
from .orm import (
    Win5RaceEntryORM,
    Win5RaceORM,
    Win5ResultORM,
    Win5RoundORM,
    Win5ScoreEventItemORM,
    Win5ScoreEventORM,
    Win5SeasonORM,
    Win5SubmissionORM,
    Win5SubmissionPickORM,
)
from .uow import (
    SessionFactory,
    SqlAlchemyFeatureUnitOfWork,
    SqlAlchemyFeatureUnitOfWorkFactory,
)
from .win5_member_identity import find_win5_member_persona


class SqlAlchemyWin5SubmissionHistoryQueryRepository:
    """Build bounded immutable Submission history projections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _optional_utc(value: datetime | None, *, field_name: str) -> datetime | None:
        if value is None:
            return None
        return from_database_utc(value, field_name=field_name)

    def find_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None:
        return find_win5_member_persona(self._session, discord_user_id=discord_user_id)

    def get_submissions_dashboard_source(
        self,
        *,
        persona_id: str,
        open_limit: int,
        scored_limit: int,
        cancelled_limit: int,
    ) -> Win5MemberSubmissionsDashboardSource | None:
        seasons = tuple(
            self._session.scalars(
                select(Win5SeasonORM)
                .where(Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value)
                .order_by(Win5SeasonORM.id)
                .limit(2)
            )
        )
        if not seasons:
            return None
        if len(seasons) != 1:
            raise ValueError("Multiple active WIN5 Seasons cannot form a member dashboard.")
        season = seasons[0]

        submission_count = (
            select(func.count(Win5SubmissionORM.id))
            .where(
                Win5SubmissionORM.round_id == Win5RoundORM.id,
                Win5SubmissionORM.persona_id == persona_id,
            )
            .correlate(Win5RoundORM)
            .scalar_subquery()
        )
        open_rows = tuple(
            self._session.execute(
                select(
                    Win5RoundORM,
                    submission_count.label("submission_count"),
                )
                .where(
                    Win5RoundORM.season_id == season.id,
                    Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                    Win5RoundORM.status == Win5RoundStatus.OPEN.value,
                )
                .order_by(Win5RoundORM.created_at, Win5RoundORM.id)
                .limit(open_limit + 1)
            ).all()
        )
        open_overflow = len(open_rows) > open_limit
        open_rows = open_rows[:open_limit]
        scored_rows = tuple(
            self._session.execute(
                select(
                    Win5RoundORM,
                    submission_count.label("submission_count"),
                )
                .where(
                    Win5RoundORM.season_id == season.id,
                    Win5RoundORM.status == Win5RoundStatus.SCORED.value,
                    submission_count > 0,
                )
                .order_by(Win5RoundORM.created_at.desc(), Win5RoundORM.id.desc())
                .limit(scored_limit)
            ).all()
        )
        cancelled_rows = tuple(
            self._session.execute(
                select(
                    Win5RoundORM,
                    submission_count.label("submission_count"),
                )
                .where(
                    Win5RoundORM.season_id == season.id,
                    Win5RoundORM.type == Win5RoundType.SPECIAL.value,
                    Win5RoundORM.status == Win5RoundStatus.CANCELLED.value,
                    submission_count > 0,
                )
                .order_by(Win5RoundORM.created_at.desc(), Win5RoundORM.id.desc())
                .limit(cancelled_limit)
            ).all()
        )

        def summary(row) -> Win5MemberSubmissionRoundSummary:
            round_ = row[0]
            return Win5MemberSubmissionRoundSummary(
                id=round_.id,
                season_id=round_.season_id,
                round_type=Win5RoundType(round_.type),
                status=Win5RoundStatus(round_.status),
                name=round_.name,
                submission_count=row.submission_count,
            )

        return Win5MemberSubmissionsDashboardSource(
            season_id=season.id,
            season_name=season.name,
            season_status=Win5SeasonStatus(season.status),
            open_round_overflow=open_overflow,
            open_rounds=tuple(summary(row) for row in open_rows),
            scored_rounds=tuple(summary(row) for row in scored_rows),
            cancelled_rounds=tuple(summary(row) for row in cancelled_rows),
        )

    def get_submission_history_round_source(
        self,
        *,
        persona_id: str,
        round_id: int,
        submission_offset: int,
        submission_limit: int,
        race_offset: int,
        race_limit: int,
    ) -> Win5MemberSubmissionRoundPageSource | None:
        seasons = tuple(
            self._session.scalars(
                select(Win5SeasonORM)
                .where(Win5SeasonORM.status == Win5SeasonStatus.ACTIVE.value)
                .order_by(Win5SeasonORM.id)
                .limit(2)
            )
        )
        if not seasons:
            return None
        if len(seasons) != 1:
            raise ValueError("Multiple active WIN5 Seasons cannot form a history page.")
        season = seasons[0]

        rounds = tuple(
            self._session.scalars(
                select(Win5RoundORM)
                .where(
                    Win5RoundORM.id == round_id,
                    Win5RoundORM.season_id == season.id,
                    Win5RoundORM.status.in_(
                        (
                            Win5RoundStatus.OPEN.value,
                            Win5RoundStatus.SCORED.value,
                            Win5RoundStatus.CANCELLED.value,
                        )
                    ),
                    or_(
                        Win5RoundORM.status != Win5RoundStatus.OPEN.value,
                        Win5RoundORM.source_kind == Win5RoundSourceKind.NATIVE_V2.value,
                    ),
                )
                .limit(2)
            )
        )
        if len(rounds) != 1:
            return None
        round_ = rounds[0]
        round_type = Win5RoundType(round_.type)
        round_status = Win5RoundStatus(round_.status)

        total_submission_count = (
            self._session.scalar(
                select(func.count(Win5SubmissionORM.id)).where(
                    Win5SubmissionORM.round_id == round_.id,
                    Win5SubmissionORM.persona_id == persona_id,
                )
            )
            or 0
        )
        total_race_count = (
            self._session.scalar(select(func.count(Win5RaceORM.id)).where(Win5RaceORM.round_id == round_.id)) or 0
        )
        total_void_race_count = (
            self._session.scalar(
                select(func.count(Win5RaceORM.id)).where(
                    Win5RaceORM.round_id == round_.id,
                    Win5RaceORM.void_reason.is_not(None),
                )
            )
            or 0
        )
        if round_status in {Win5RoundStatus.SCORED, Win5RoundStatus.CANCELLED} and total_submission_count == 0:
            return None

        def source_for(projected_round: Win5MemberSubmissionRound) -> Win5MemberSubmissionRoundPageSource:
            return Win5MemberSubmissionRoundPageSource(
                season_id=season.id,
                season_name=season.name,
                season_status=Win5SeasonStatus(season.status),
                round=projected_round,
                total_submission_count=total_submission_count,
                submission_offset=submission_offset,
                submission_limit=submission_limit,
                total_race_count=total_race_count,
                race_offset=race_offset,
                race_limit=race_limit,
                total_void_race_count=total_void_race_count,
            )

        def empty_round() -> Win5MemberSubmissionRound:
            return Win5MemberSubmissionRound(
                id=round_.id,
                season_id=round_.season_id,
                round_type=round_type,
                status=round_status,
                name=round_.name,
                opens_at=self._optional_utc(
                    round_.opens_at,
                    field_name="win5_rounds.opens_at",
                ),
                closes_at=self._optional_utc(
                    round_.closes_at,
                    field_name="win5_rounds.closes_at",
                ),
            )

        invalid_submission_offset = (
            submission_offset != 0 if total_submission_count == 0 else submission_offset >= total_submission_count
        )
        if total_race_count == 0 or invalid_submission_offset or race_offset >= total_race_count:
            return source_for(empty_round())

        submissions = tuple(
            self._session.scalars(
                select(Win5SubmissionORM)
                .where(
                    Win5SubmissionORM.round_id == round_.id,
                    Win5SubmissionORM.persona_id == persona_id,
                )
                .order_by(Win5SubmissionORM.created_at, Win5SubmissionORM.id)
                .offset(submission_offset)
                .limit(submission_limit)
            )
        )
        races = tuple(
            self._session.scalars(
                select(Win5RaceORM)
                .where(Win5RaceORM.round_id == round_.id)
                .order_by(Win5RaceORM.id)
                .offset(race_offset)
                .limit(race_limit)
            )
        )
        race_ids = tuple(race.id for race in races)
        submission_ids = tuple(submission.id for submission in submissions)

        if round_status != Win5RoundStatus.SCORED:
            unexpected_result = self._session.scalar(
                select(Win5ResultORM.id)
                .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
                .where(Win5RaceORM.round_id == round_.id)
                .limit(1)
            )
            if unexpected_result is not None:
                raise ValueError("Unscored Submission history cannot contain authoritative Results.")
        if round_status == Win5RoundStatus.CANCELLED:
            unexpected_score_event = self._session.scalar(
                select(Win5ScoreEventORM.id).where(Win5ScoreEventORM.round_id == round_.id).limit(1)
            )
            if unexpected_score_event is not None:
                raise ValueError("Cancelled Submission history cannot contain score events.")

        picks: list[Win5SubmissionPickORM] = []
        results: tuple[Win5ResultORM, ...] = ()
        entries: tuple[Win5RaceEntryORM, ...] = ()
        judgements_by_submission_id: dict[
            int,
            Win5NormalSubmissionJudgement | Win5SpecialSubmissionJudgement,
        ] = {}

        if round_type == Win5RoundType.NORMAL:
            if total_race_count != 1 or len(races) != 1:
                raise ValueError("Normal Submission history requires exactly one paged Race.")
            race = races[0]
            entry_count = (
                self._session.scalar(select(func.count(Win5RaceEntryORM.id)).where(Win5RaceEntryORM.race_id == race.id))
                or 0
            )
            if entry_count < 5:
                raise ValueError("Normal Submission history requires at least five RaceEntries.")

            for submission in submissions:
                submission_picks = tuple(
                    self._session.scalars(
                        select(Win5SubmissionPickORM)
                        .where(Win5SubmissionPickORM.submission_id == submission.id)
                        .order_by(
                            Win5SubmissionPickORM.race_id,
                            Win5SubmissionPickORM.position,
                            Win5SubmissionPickORM.id,
                        )
                        .limit(6)
                    )
                )
                if len(submission_picks) > 5:
                    raise ValueError("Normal Submission history exceeds five persisted picks.")
                picks.extend(submission_picks)

            if round_status == Win5RoundStatus.SCORED:
                loaded_results = tuple(
                    self._session.scalars(
                        select(Win5ResultORM)
                        .where(Win5ResultORM.race_id == race.id)
                        .order_by(Win5ResultORM.position, Win5ResultORM.id)
                        .limit(6)
                    )
                )
                if len(loaded_results) != 5:
                    raise ValueError("Scored Normal history requires exactly five authoritative Results.")
                results = loaded_results

            referenced_entry_ids = {
                entry_id
                for entry_id in (
                    *(pick.race_entry_id for pick in picks),
                    *(result.race_entry_id for result in results),
                )
                if entry_id is not None
            }
            first_entries = tuple(
                self._session.scalars(
                    select(Win5RaceEntryORM)
                    .where(Win5RaceEntryORM.race_id == race.id)
                    .order_by(Win5RaceEntryORM.gate_number, Win5RaceEntryORM.id)
                    .limit(5)
                )
            )
            first_entry_ids = {entry.id for entry in first_entries}
            additional_ids = referenced_entry_ids - first_entry_ids
            additional_entries: tuple[Win5RaceEntryORM, ...] = ()
            if additional_ids:
                loaded_additional = tuple(
                    self._session.scalars(
                        select(Win5RaceEntryORM)
                        .where(
                            Win5RaceEntryORM.race_id == race.id,
                            Win5RaceEntryORM.id.in_(additional_ids),
                        )
                        .order_by(Win5RaceEntryORM.gate_number, Win5RaceEntryORM.id)
                        .limit(len(additional_ids) + 1)
                    )
                )
                if len(loaded_additional) != len(additional_ids):
                    raise ValueError("Normal history references a missing RaceEntry.")
                additional_entries = loaded_additional
            entries = tuple(
                sorted(
                    (*first_entries, *additional_entries),
                    key=lambda entry: (entry.gate_number, entry.id),
                )
            )

            for submission in submissions:
                events = tuple(
                    self._session.scalars(
                        select(Win5ScoreEventORM)
                        .where(Win5ScoreEventORM.submission_id == submission.id)
                        .order_by(Win5ScoreEventORM.id)
                        .limit(2)
                    )
                )
                if len(events) > 1:
                    raise ValueError("A Submission has multiple score events.")
                if not events:
                    continue
                event = events[0]
                if event.race_id != race.id:
                    raise ValueError("A Normal score event has invalid Race provenance.")
                items = tuple(
                    self._session.scalars(
                        select(Win5ScoreEventItemORM)
                        .where(Win5ScoreEventItemORM.score_event_id == event.id)
                        .order_by(
                            Win5ScoreEventItemORM.position,
                            Win5ScoreEventItemORM.id,
                        )
                        .limit(6)
                    )
                )
                if len(items) > 5:
                    raise ValueError("A Normal score event has too many judgement items.")
                if any(item.race_id != race.id for item in items):
                    raise ValueError("A Normal judgement item has invalid Race provenance.")
                score = Win5NormalSubmissionScore(
                    tier=Win5SubmissionTier(event.tier),
                    items=tuple(
                        Win5NormalJudgementItem(
                            position=item.position,
                            submission_pick_id=item.submission_pick_id,
                            matched_result_id=item.matched_result_id,
                            outcome=Win5JudgementOutcome(item.outcome),
                            season_score_delta=item.season_score_delta,
                        )
                        for item in items
                    ),
                    exact_count=event.exact_count,
                    wrong_position_count=event.wrong_position_count,
                    off_board_count=event.off_board_count,
                    missing_count=event.missing_count,
                    season_score_delta=event.season_score_delta,
                    top1_score_delta=event.top1_score_delta,
                    circle_point_reward=event.circle_point_reward,
                )
                judgements_by_submission_id[submission.id] = Win5NormalSubmissionJudgement(
                    event_id=event.id,
                    season_id=event.season_id,
                    round_id=event.round_id,
                    race_id=event.race_id,
                    submission_id=event.submission_id,
                    submission_version=event.submission_version,
                    persona_id=event.persona_id,
                    tier=Win5SubmissionTier(event.tier),
                    result_fingerprint=event.result_fingerprint,
                    scoring_policy_version=event.scoring_policy_version,
                    reward_policy_version=event.reward_policy_version,
                    score=score,
                )
        else:
            maximum_page_picks = len(submissions) * len(races)
            if submission_ids and race_ids:
                loaded_picks = tuple(
                    self._session.scalars(
                        select(Win5SubmissionPickORM)
                        .where(
                            Win5SubmissionPickORM.submission_id.in_(submission_ids),
                            Win5SubmissionPickORM.race_id.in_(race_ids),
                        )
                        .order_by(
                            Win5SubmissionPickORM.submission_id,
                            Win5SubmissionPickORM.race_id,
                            Win5SubmissionPickORM.position,
                            Win5SubmissionPickORM.id,
                        )
                        .limit(maximum_page_picks + 1)
                    )
                )
                if len(loaded_picks) > maximum_page_picks:
                    raise ValueError("Special Submission history exceeds one pick per page Race.")
                picks.extend(loaded_picks)

            if round_status == Win5RoundStatus.SCORED:
                expected_result_count = total_race_count - total_void_race_count
                result_stats = self._session.execute(
                    select(
                        func.count(Win5ResultORM.id),
                        func.count(func.distinct(Win5ResultORM.race_id)),
                        func.min(Win5ResultORM.position),
                        func.max(Win5ResultORM.position),
                    )
                    .join(Win5RaceORM, Win5RaceORM.id == Win5ResultORM.race_id)
                    .where(Win5RaceORM.round_id == round_.id)
                ).one()
                if tuple(result_stats) != (
                    expected_result_count,
                    expected_result_count,
                    1,
                    1,
                ):
                    raise ValueError("Scored Special history requires one winner Result per non-void Race.")
                non_void_race_ids = tuple(race.id for race in races if race.void_reason is None)
                loaded_results = tuple(
                    self._session.scalars(
                        select(Win5ResultORM)
                        .where(Win5ResultORM.race_id.in_(non_void_race_ids))
                        .order_by(Win5ResultORM.race_id, Win5ResultORM.id)
                        .limit(len(non_void_race_ids) + 1)
                    )
                )
                if len(loaded_results) != len(non_void_race_ids):
                    raise ValueError("Scored Special history requires one Result per non-void page Race.")
                results = loaded_results

            for submission in submissions:
                events = tuple(
                    self._session.scalars(
                        select(Win5ScoreEventORM)
                        .where(Win5ScoreEventORM.submission_id == submission.id)
                        .order_by(Win5ScoreEventORM.id)
                        .limit(2)
                    )
                )
                if len(events) > 1:
                    raise ValueError("A Submission has multiple score events.")
                if not events:
                    continue
                event = events[0]
                if event.race_id is not None or event.wrong_position_count != 0:
                    raise ValueError("A Special score event has invalid header provenance.")
                item_stats = self._session.execute(
                    select(
                        func.count(Win5ScoreEventItemORM.id),
                        func.count(func.distinct(Win5ScoreEventItemORM.race_id)),
                        func.min(Win5ScoreEventItemORM.position),
                        func.max(Win5ScoreEventItemORM.position),
                        func.sum(case((Win5ScoreEventItemORM.outcome == Win5JudgementOutcome.EXACT.value, 1), else_=0)),
                        func.sum(
                            case((Win5ScoreEventItemORM.outcome == Win5JudgementOutcome.OFF_BOARD.value, 1), else_=0)
                        ),
                        func.sum(
                            case((Win5ScoreEventItemORM.outcome == Win5JudgementOutcome.MISSING.value, 1), else_=0)
                        ),
                        func.sum(case((Win5ScoreEventItemORM.outcome == Win5JudgementOutcome.VOID.value, 1), else_=0)),
                    ).where(Win5ScoreEventItemORM.score_event_id == event.id)
                ).one()
                event_void_count = total_race_count - event.exact_count - event.off_board_count - event.missing_count
                if tuple(item_stats) != (
                    total_race_count,
                    total_race_count,
                    1,
                    1,
                    event.exact_count,
                    event.off_board_count,
                    event.missing_count,
                    event_void_count,
                ):
                    raise ValueError("A Special score event must cover every canonical Race.")
                items = tuple(
                    self._session.scalars(
                        select(Win5ScoreEventItemORM)
                        .where(
                            Win5ScoreEventItemORM.score_event_id == event.id,
                            Win5ScoreEventItemORM.race_id.in_(race_ids),
                        )
                        .order_by(Win5ScoreEventItemORM.race_id, Win5ScoreEventItemORM.id)
                        .limit(len(races) + 1)
                    )
                )
                if len(items) != len(races) or any(item.position != 1 for item in items):
                    raise ValueError("A Special score event has an invalid Race item page.")
                judgements_by_submission_id[submission.id] = Win5SpecialSubmissionJudgement(
                    event_id=event.id,
                    season_id=event.season_id,
                    round_id=event.round_id,
                    submission_id=event.submission_id,
                    submission_version=event.submission_version,
                    persona_id=event.persona_id,
                    tier=Win5SubmissionTier(event.tier),
                    result_fingerprint=event.result_fingerprint,
                    scoring_policy_version=event.scoring_policy_version,
                    reward_policy_version=event.reward_policy_version,
                    race_count=total_race_count,
                    exact_count=event.exact_count,
                    off_board_count=event.off_board_count,
                    missing_count=event.missing_count,
                    season_score_delta=event.season_score_delta,
                    top1_score_delta=event.top1_score_delta,
                    circle_point_reward=event.circle_point_reward,
                    void_count=event_void_count,
                    items=tuple(
                        Win5SpecialJudgementItem(
                            race_id=item.race_id,
                            submission_pick_id=item.submission_pick_id,
                            matched_result_id=item.matched_result_id,
                            outcome=Win5JudgementOutcome(item.outcome),
                            season_score_delta=item.season_score_delta,
                        )
                        for item in items
                    ),
                )

            reference_rows: list[Win5RaceEntryORM] = []
            for race in races:
                gates = {
                    gate_number
                    for gate_number in (
                        *(pick.gate_number for pick in picks if pick.race_id == race.id),
                        *(result.gate_number for result in results if result.race_id == race.id),
                    )
                    if gate_number is not None
                }
                if not gates:
                    continue
                loaded_references = tuple(
                    self._session.scalars(
                        select(Win5RaceEntryORM)
                        .where(
                            Win5RaceEntryORM.race_id == race.id,
                            Win5RaceEntryORM.gate_number.in_(gates),
                        )
                        .order_by(Win5RaceEntryORM.gate_number, Win5RaceEntryORM.id)
                        .limit(len(gates) + 1)
                    )
                )
                if len(loaded_references) > len(gates):
                    raise ValueError("Special Race has duplicate gate references.")
                reference_rows.extend(loaded_references)
            entries = tuple(reference_rows)

        entry_options_by_id: dict[int, Win5RaceEntryOption] = {}
        entry_options_by_gate: dict[tuple[int, int], Win5RaceEntryOption] = {}
        entries_by_race_id: dict[int, list[Win5RaceEntryOption]] = defaultdict(list)
        for entry in entries:
            option = Win5RaceEntryOption(
                id=entry.id,
                gate_number=entry.gate_number,
                name=entry.name,
            )
            entry_options_by_id[entry.id] = option
            entry_options_by_gate[(entry.race_id, entry.gate_number)] = option
            entries_by_race_id[entry.race_id].append(option)

        picks_by_submission_id: dict[int, list[Win5MemberSubmissionPick]] = defaultdict(list)
        for pick in picks:
            reference = (
                entry_options_by_id.get(pick.race_entry_id)
                if pick.race_entry_id is not None
                else entry_options_by_gate.get((pick.race_id, pick.gate_number))
            )
            picks_by_submission_id[pick.submission_id].append(
                Win5MemberSubmissionPick(
                    id=pick.id,
                    race_id=pick.race_id,
                    position=pick.position,
                    race_entry_id=pick.race_entry_id,
                    gate_number=pick.gate_number,
                    reference_entry=reference,
                )
            )

        projected_results = tuple(
            Win5MemberResult(
                id=result.id,
                race_id=result.race_id,
                position=result.position,
                race_entry_id=result.race_entry_id,
                gate_number=result.gate_number,
                reference_entry=(
                    entry_options_by_id.get(result.race_entry_id)
                    if result.race_entry_id is not None
                    else entry_options_by_gate.get((result.race_id, result.gate_number))
                ),
            )
            for result in results
        )
        projected_submissions = tuple(
            Win5MemberSubmission(
                id=submission.id,
                round_id=submission.round_id,
                persona_id=submission.persona_id,
                tier=Win5SubmissionTier(submission.tier),
                status=Win5SubmissionStatus(submission.status),
                active_marker=submission.active_marker,
                version=submission.version,
                created_at=from_database_utc(
                    submission.created_at,
                    field_name="win5_submissions.created_at",
                ),
                updated_at=from_database_utc(
                    submission.updated_at,
                    field_name="win5_submissions.updated_at",
                ),
                picks=tuple(picks_by_submission_id[submission.id]),
                judgement=judgements_by_submission_id.get(submission.id),
            )
            for submission in submissions
        )
        projected_races = tuple(
            Win5RaceCard(
                id=race.id,
                name=race.name,
                scheduled_at=self._optional_utc(
                    race.scheduled_at,
                    field_name="win5_races.scheduled_at",
                ),
                entries=tuple(entries_by_race_id[race.id]),
                void_reason=race.void_reason,
                voided_at=self._optional_utc(
                    race.voided_at,
                    field_name="win5_races.voided_at",
                ),
            )
            for race in races
        )

        return source_for(
            Win5MemberSubmissionRound(
                id=round_.id,
                season_id=round_.season_id,
                round_type=round_type,
                status=round_status,
                name=round_.name,
                opens_at=self._optional_utc(
                    round_.opens_at,
                    field_name="win5_rounds.opens_at",
                ),
                closes_at=self._optional_utc(
                    round_.closes_at,
                    field_name="win5_rounds.closes_at",
                ),
                races=projected_races,
                results=projected_results,
                submissions=projected_submissions,
            )
        )


class SqlAlchemyWin5SubmissionHistoryQueryUnitOfWork(SqlAlchemyFeatureUnitOfWork):
    """Concrete feature UoW exposing only Submission history persistence."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory)
        self._win5_submission_history_queries: SqlAlchemyWin5SubmissionHistoryQueryRepository | None = None

    @property
    def win5_submission_history_queries(self) -> SqlAlchemyWin5SubmissionHistoryQueryRepository:
        return self._require_active_repository(self._win5_submission_history_queries)

    def _activate_repositories(self) -> None:
        self._win5_submission_history_queries = SqlAlchemyWin5SubmissionHistoryQueryRepository(self.session)

    def _deactivate_repositories(self) -> None:
        self._win5_submission_history_queries = None


class SqlAlchemyWin5SubmissionHistoryQueryUnitOfWorkFactory(
    SqlAlchemyFeatureUnitOfWorkFactory[SqlAlchemyWin5SubmissionHistoryQueryUnitOfWork]
):
    """Create one Submission history query UoW per application operation."""

    unit_of_work_type = SqlAlchemyWin5SubmissionHistoryQueryUnitOfWork
