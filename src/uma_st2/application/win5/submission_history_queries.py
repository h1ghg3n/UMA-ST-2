"""Bounded member-owned WIN5 Submission history queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import (
    WIN5_SPECIAL_REWARD_POLICY_VERSION,
    WIN5_SPECIAL_SCORING_POLICY_VERSION,
    WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
    Win5DomainError,
    Win5JudgementOutcome,
    Win5NormalResultPlacement,
    Win5Race,
    Win5RaceEntry,
    Win5Round,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5Submission,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    fingerprint_normal_result,
    validate_special_score_totals,
    validate_submission_for_round,
)

from .member_identity import Win5MemberPersona
from .member_queries import (
    Win5MemberQueryIdentityError,
    Win5MemberResult,
    Win5MemberSubmission,
    Win5MemberSubmissionPick,
    Win5MemberSubmissionRound,
    Win5MemberSubmissionRoundPage,
    Win5MemberSubmissionRoundPageSource,
    Win5MemberSubmissionsDashboard,
    Win5MemberSubmissionsDashboardSource,
    Win5NormalSubmissionJudgement,
    Win5RaceEntryOption,
    Win5SpecialSubmissionJudgement,
    Win5SubmissionsInvalidSourceError,
    Win5SubmissionsUnavailableError,
)


class Win5SubmissionHistoryQueryRepository(Protocol):
    """Read-only persistence port for owned WIN5 Submission history."""

    def find_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None: ...

    def get_submissions_dashboard_source(
        self,
        *,
        persona_id: str,
        open_limit: int,
        scored_limit: int,
        cancelled_limit: int,
    ) -> Win5MemberSubmissionsDashboardSource | None: ...

    def get_submission_history_round_source(
        self,
        *,
        persona_id: str,
        round_id: int,
        submission_offset: int,
        submission_limit: int,
        race_offset: int,
        race_limit: int,
    ) -> Win5MemberSubmissionRoundPageSource | None: ...


class Win5SubmissionHistoryQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Submission history repository."""

    @property
    def win5_submission_history_queries(self) -> Win5SubmissionHistoryQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5SubmissionHistoryQueries:
    """Application entry point for member-owned WIN5 Submission history."""

    query_runner: QueryRunner[Win5SubmissionHistoryQueryUnitOfWork]

    def get_submissions_dashboard(self, *, discord_user_id: str) -> Win5MemberSubmissionsDashboard:
        """Return the active Persona's bounded active-Season Submission history."""

        self._validate_discord_user_id(discord_user_id)

        def query(unit_of_work: Win5SubmissionHistoryQueryUnitOfWork) -> Win5MemberSubmissionsDashboard:
            repository = unit_of_work.win5_submission_history_queries
            member = self._require_active_member(repository, discord_user_id=discord_user_id)
            try:
                source = repository.get_submissions_dashboard_source(
                    persona_id=member.id,
                    open_limit=25,
                    scored_limit=25,
                    cancelled_limit=25,
                )
            except (TypeError, ValueError) as exc:
                raise Win5SubmissionsInvalidSourceError(
                    "Stored WIN5 Submission dashboard facts are malformed."
                ) from exc
            if source is None:
                raise Win5SubmissionsUnavailableError("No active WIN5 Season is available.")
            return self._build_submissions_dashboard(source=source)

        return self.query_runner.run(query)

    def get_submission_history_round(
        self,
        *,
        discord_user_id: str,
        round_id: int,
        submission_offset: int,
        submission_limit: int = 5,
        race_offset: int = 0,
        race_limit: int = 5,
    ) -> Win5MemberSubmissionRoundPage:
        """Return one independently bounded active-Season history detail page."""

        self._validate_discord_user_id(discord_user_id)
        if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id <= 0:
            raise ValueError("round_id must be a positive integer.")
        for value, field_name in (
            (submission_offset, "submission_offset"),
            (race_offset, "race_offset"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        for value, field_name in (
            (submission_limit, "submission_limit"),
            (race_limit, "race_limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
                raise ValueError(f"{field_name} must be an integer from 1 through 5.")

        def query(unit_of_work: Win5SubmissionHistoryQueryUnitOfWork) -> Win5MemberSubmissionRoundPage:
            repository = unit_of_work.win5_submission_history_queries
            member = self._require_active_member(repository, discord_user_id=discord_user_id)
            try:
                source = repository.get_submission_history_round_source(
                    persona_id=member.id,
                    round_id=round_id,
                    submission_offset=submission_offset,
                    submission_limit=submission_limit,
                    race_offset=race_offset,
                    race_limit=race_limit,
                )
            except (TypeError, ValueError) as exc:
                raise Win5SubmissionsInvalidSourceError(
                    "Stored WIN5 Submission history page facts are malformed."
                ) from exc
            if source is None:
                raise Win5SubmissionsUnavailableError("Submission history Round is unavailable in the active Season.")
            return self._build_submission_history_round_page(
                source=source,
                persona_id=member.id,
                round_id=round_id,
                submission_offset=submission_offset,
                submission_limit=submission_limit,
                race_offset=race_offset,
                race_limit=race_limit,
            )

        return self.query_runner.run(query)

    @staticmethod
    def _validate_discord_user_id(discord_user_id: str) -> None:
        if not isinstance(discord_user_id, str) or not discord_user_id or len(discord_user_id) > 32:
            raise ValueError("discord_user_id must be a non-empty string of at most 32 characters.")

    @staticmethod
    def _require_active_member(
        repository: Win5SubmissionHistoryQueryRepository,
        *,
        discord_user_id: str,
    ) -> Win5MemberPersona:
        member = repository.find_member_persona(discord_user_id=discord_user_id)
        if member is None or not member.can_read_member_history:
            raise Win5MemberQueryIdentityError(
                "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
            )
        return member

    @staticmethod
    def _build_submissions_dashboard(
        *,
        source: Win5MemberSubmissionsDashboardSource,
    ) -> Win5MemberSubmissionsDashboard:
        try:
            if source.season_status != Win5SeasonStatus.ACTIVE:
                raise Win5SubmissionsUnavailableError("WIN5 Submission dashboard requires an active Season.")
            if (
                source.open_round_overflow
                or len(source.open_rounds) > 25
                or len(source.scored_rounds) > 25
                or len(source.cancelled_rounds) > 25
            ):
                raise Win5SubmissionsInvalidSourceError("WIN5 Submission dashboard source exceeds its query bound.")

            round_ids = [
                round_.id
                for round_ in (
                    *source.open_rounds,
                    *source.scored_rounds,
                    *source.cancelled_rounds,
                )
            ]
            if len(round_ids) != len(set(round_ids)):
                raise Win5SubmissionsInvalidSourceError("WIN5 Submission dashboard contains duplicate Rounds.")

            for round_ in source.open_rounds:
                if (
                    round_.season_id != source.season_id
                    or round_.status != Win5RoundStatus.OPEN
                    or isinstance(round_.submission_count, bool)
                    or not isinstance(round_.submission_count, int)
                    or round_.submission_count < 0
                ):
                    raise Win5SubmissionsInvalidSourceError("Open dashboard group contains an invalid Round.")
            for round_ in source.scored_rounds:
                if (
                    round_.season_id != source.season_id
                    or round_.status != Win5RoundStatus.SCORED
                    or isinstance(round_.submission_count, bool)
                    or not isinstance(round_.submission_count, int)
                    or round_.submission_count <= 0
                ):
                    raise Win5SubmissionsInvalidSourceError("Scored dashboard group contains an invalid Round.")
            for round_ in source.cancelled_rounds:
                if (
                    round_.season_id != source.season_id
                    or round_.round_type != Win5RoundType.SPECIAL
                    or round_.status != Win5RoundStatus.CANCELLED
                    or isinstance(round_.submission_count, bool)
                    or not isinstance(round_.submission_count, int)
                    or round_.submission_count <= 0
                ):
                    raise Win5SubmissionsInvalidSourceError("Cancelled dashboard group contains an invalid Round.")
        except (Win5SubmissionsInvalidSourceError, Win5SubmissionsUnavailableError):
            raise
        except (TypeError, ValueError, Win5DomainError) as exc:
            raise Win5SubmissionsInvalidSourceError("Stored WIN5 Submission dashboard facts are inconsistent.") from exc

        return Win5MemberSubmissionsDashboard(
            season_id=source.season_id,
            season_name=source.season_name,
            open_rounds=source.open_rounds,
            scored_rounds=source.scored_rounds,
            cancelled_rounds=source.cancelled_rounds,
        )

    @staticmethod
    def _build_submission_history_round_page(
        *,
        source: Win5MemberSubmissionRoundPageSource,
        persona_id: str,
        round_id: int,
        submission_offset: int,
        submission_limit: int,
        race_offset: int,
        race_limit: int,
    ) -> Win5MemberSubmissionRoundPage:
        try:
            round_ = source.round
            if source.season_status != Win5SeasonStatus.ACTIVE:
                raise Win5SubmissionsUnavailableError("Submission history requires an active Season.")
            if (
                source.season_id != round_.season_id
                or round_.id != round_id
                or round_.status
                not in {
                    Win5RoundStatus.OPEN,
                    Win5RoundStatus.SCORED,
                    Win5RoundStatus.CANCELLED,
                }
                or source.submission_offset != submission_offset
                or source.submission_limit != submission_limit
                or source.race_offset != race_offset
                or source.race_limit != race_limit
            ):
                raise Win5SubmissionsInvalidSourceError("Submission history page identity is inconsistent.")
            counts = (
                source.total_submission_count,
                source.total_race_count,
                source.total_void_race_count,
            )
            if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
                raise Win5SubmissionsInvalidSourceError("Submission history page totals are invalid.")
            if source.total_race_count == 0:
                raise Win5SubmissionsInvalidSourceError("Submission history Round has no Race.")
            if source.total_void_race_count > source.total_race_count:
                raise Win5SubmissionsInvalidSourceError("Submission history void count exceeds its Race count.")
            if (
                round_.status in {Win5RoundStatus.SCORED, Win5RoundStatus.CANCELLED}
                and source.total_submission_count == 0
            ):
                raise Win5SubmissionsUnavailableError("Terminal Round has no Persona-owned Submission history.")
            if round_.round_type == Win5RoundType.NORMAL and source.total_void_race_count != 0:
                raise Win5SubmissionsInvalidSourceError("Normal Submission history cannot contain void Races.")
            if round_.status == Win5RoundStatus.OPEN and source.total_void_race_count != 0:
                raise Win5SubmissionsInvalidSourceError("Open Submission history cannot contain void Races.")
            if round_.status == Win5RoundStatus.SCORED and source.total_void_race_count == source.total_race_count:
                raise Win5SubmissionsInvalidSourceError("An all-void Round cannot be scored.")
            if round_.status == Win5RoundStatus.CANCELLED and (
                round_.round_type != Win5RoundType.SPECIAL or source.total_void_race_count != source.total_race_count
            ):
                raise Win5SubmissionsInvalidSourceError("Cancelled history requires an all-void Special Round.")
            if source.total_submission_count == 0:
                if submission_offset != 0:
                    raise Win5SubmissionsUnavailableError("Submission page offset is outside the retained history.")
            elif submission_offset >= source.total_submission_count:
                raise Win5SubmissionsUnavailableError("Submission page offset is outside the retained history.")
            if race_offset >= source.total_race_count:
                raise Win5SubmissionsUnavailableError("Race page offset is outside the Round.")

            expected_submission_count = min(
                submission_limit,
                source.total_submission_count - submission_offset,
            )
            expected_race_count = min(race_limit, source.total_race_count - race_offset)
            if (
                len(round_.submissions) != expected_submission_count
                or len(round_.races) != expected_race_count
                or len(round_.submissions) > 5
                or len(round_.races) > 5
            ):
                raise Win5SubmissionsInvalidSourceError("Submission history page exceeds or misses its bound.")
            if tuple(race.id for race in round_.races) != tuple(sorted(race.id for race in round_.races)):
                raise Win5SubmissionsInvalidSourceError("Submission history Races are not stably ordered.")
            if tuple((submission.created_at, submission.id) for submission in round_.submissions) != tuple(
                sorted((submission.created_at, submission.id) for submission in round_.submissions)
            ):
                raise Win5SubmissionsInvalidSourceError("Submission history rows are not stably ordered.")

            Win5SubmissionHistoryQueries._validate_submission_history_round(
                round_,
                persona_id=persona_id,
                total_race_count=source.total_race_count,
                total_void_race_count=source.total_void_race_count,
            )
        except (Win5SubmissionsInvalidSourceError, Win5SubmissionsUnavailableError):
            raise
        except (TypeError, ValueError, Win5DomainError) as exc:
            raise Win5SubmissionsInvalidSourceError("Stored WIN5 Submission history facts are inconsistent.") from exc

        return Win5MemberSubmissionRoundPage(
            season_id=source.season_id,
            season_name=source.season_name,
            round=round_,
            total_submission_count=source.total_submission_count,
            submission_offset=source.submission_offset,
            submission_limit=source.submission_limit,
            total_race_count=source.total_race_count,
            race_offset=source.race_offset,
            race_limit=source.race_limit,
            total_void_race_count=source.total_void_race_count,
        )

    @staticmethod
    def _validate_submission_history_round(
        round_: Win5MemberSubmissionRound,
        *,
        persona_id: str,
        total_race_count: int,
        total_void_race_count: int,
    ) -> None:
        if not round_.races or len({race.id for race in round_.races}) != len(round_.races):
            raise Win5SubmissionsInvalidSourceError("Submission history Round has an invalid Race graph.")
        if round_.round_type == Win5RoundType.NORMAL and len(round_.races) != 1:
            raise Win5SubmissionsInvalidSourceError("Normal Submission history requires exactly one Race.")

        entries_by_id: dict[int, tuple[int, Win5RaceEntryOption]] = {}
        page_void_race_count = 0
        for race in round_.races:
            if (race.void_reason is None) != (race.voided_at is None):
                raise Win5SubmissionsInvalidSourceError("Submission history has an incomplete Race void fact.")
            if race.void_reason is not None:
                if (
                    not isinstance(race.void_reason, str)
                    or not race.void_reason.strip()
                    or len(race.void_reason.strip()) > 255
                    or race.void_reason != race.void_reason.strip()
                    or race.voided_at is None
                    or not isinstance(race.voided_at, datetime)
                    or race.voided_at.utcoffset() is None
                ):
                    raise Win5SubmissionsInvalidSourceError("Submission history has an invalid Race void fact.")
                page_void_race_count += 1
            if (
                len({entry.id for entry in race.entries}) != len(race.entries)
                or len({entry.gate_number for entry in race.entries}) != len(race.entries)
                or any(
                    isinstance(entry.gate_number, bool)
                    or not isinstance(entry.gate_number, int)
                    or entry.gate_number <= 0
                    for entry in race.entries
                )
            ):
                raise Win5SubmissionsInvalidSourceError("Submission history has invalid RaceEntry references.")
            for entry in race.entries:
                if entry.id in entries_by_id:
                    raise Win5SubmissionsInvalidSourceError("RaceEntry identity crosses dashboard Races.")
                entries_by_id[entry.id] = (race.id, entry)
        if page_void_race_count > total_void_race_count or (
            total_void_race_count == total_race_count and page_void_race_count != len(round_.races)
        ):
            raise Win5SubmissionsInvalidSourceError("Submission history Race page contradicts its void count.")
        if round_.round_type == Win5RoundType.NORMAL and len(entries_by_id) < 5:
            raise Win5SubmissionsInvalidSourceError("Normal Submission history requires at least five RaceEntries.")

        results_by_id = {result.id: result for result in round_.results}
        if len(results_by_id) != len(round_.results):
            raise Win5SubmissionsInvalidSourceError("Submission history has duplicate Result IDs.")
        if round_.status != Win5RoundStatus.SCORED and round_.results:
            raise Win5SubmissionsInvalidSourceError("Unscored Submission history cannot contain authoritative Results.")
        race_ids = {race.id for race in round_.races}
        races_by_id = {race.id: race for race in round_.races}
        if round_.round_type == Win5RoundType.SPECIAL and round_.status == Win5RoundStatus.SCORED:
            non_void_page_race_ids = tuple(race.id for race in round_.races if race.void_reason is None)
            if tuple(result.race_id for result in round_.results) != non_void_page_race_ids:
                raise Win5SubmissionsInvalidSourceError(
                    "Scored Special history requires one ordered winner Result per non-void page Race."
                )
            if any(result.position != 1 for result in round_.results):
                raise Win5SubmissionsInvalidSourceError("Special history Result must be a Race winner.")
        for result in round_.results:
            if result.race_id not in race_ids:
                raise Win5SubmissionsInvalidSourceError("Result does not belong to the dashboard Round.")
            if races_by_id[result.race_id].void_reason is not None:
                raise Win5SubmissionsInvalidSourceError("A void Special Race cannot have a winner Result.")
            Win5SubmissionHistoryQueries._validate_projected_identity(
                round_type=round_.round_type,
                race_id=result.race_id,
                race_entry_id=result.race_entry_id,
                gate_number=result.gate_number,
                reference_entry=result.reference_entry,
                entries_by_id=entries_by_id,
            )

        if round_.round_type == Win5RoundType.NORMAL and round_.status == Win5RoundStatus.SCORED:
            placements = tuple(
                Win5NormalResultPlacement(
                    id=result.id,
                    position=result.position,
                    race_entry_id=result.race_entry_id,
                )
                for result in round_.results
                if result.race_entry_id is not None
            )
            result_fingerprint = fingerprint_normal_result(placements)
        else:
            result_fingerprint = None

        submission_ids: set[int] = set()
        referenced_special_entry_ids: set[int] = set()
        if round_.round_type == Win5RoundType.SPECIAL:
            referenced_special_entry_ids.update(
                result.reference_entry.id for result in round_.results if result.reference_entry is not None
            )
        special_result_fingerprints: set[str] = set()
        for submission in round_.submissions:
            if submission.id in submission_ids:
                raise Win5SubmissionsInvalidSourceError("Submission history contains duplicate Submission IDs.")
            submission_ids.add(submission.id)
            if (
                submission.round_id != round_.id
                or submission.persona_id != persona_id
                or isinstance(submission.version, bool)
                or not isinstance(submission.version, int)
                or submission.version <= 0
                or (submission.status == Win5SubmissionStatus.ACCEPTED) != (submission.active_marker is True)
            ):
                raise Win5SubmissionsInvalidSourceError("Submission history ownership or lifecycle is inconsistent.")

            domain_picks: list[Win5SubmissionPick] = []
            picks_by_id: dict[int, Win5MemberSubmissionPick] = {}
            for pick in submission.picks:
                if pick.id in picks_by_id or pick.race_id not in race_ids:
                    raise Win5SubmissionsInvalidSourceError("Submission pick identity is inconsistent.")
                picks_by_id[pick.id] = pick
                Win5SubmissionHistoryQueries._validate_projected_identity(
                    round_type=round_.round_type,
                    race_id=pick.race_id,
                    race_entry_id=pick.race_entry_id,
                    gate_number=pick.gate_number,
                    reference_entry=pick.reference_entry,
                    entries_by_id=entries_by_id,
                )
                if round_.round_type == Win5RoundType.SPECIAL and pick.reference_entry is not None:
                    referenced_special_entry_ids.add(pick.reference_entry.id)
                domain_picks.append(
                    Win5SubmissionPick(
                        id=pick.id,
                        submission_id=submission.id,
                        race_id=pick.race_id,
                        race_entry_id=pick.race_entry_id,
                        gate_number=pick.gate_number,
                        position=pick.position,
                    )
                )

            validate_submission_for_round(
                Win5Round(
                    id=round_.id,
                    season_id=round_.season_id,
                    name=round_.name,
                    type=round_.round_type,
                    status=round_.status,
                    races=tuple(
                        Win5Race(
                            id=race.id,
                            name=race.name,
                            entries=tuple(
                                Win5RaceEntry(
                                    id=entry.id,
                                    race_id=race.id,
                                    gate_number=entry.gate_number,
                                    name=entry.name,
                                )
                                for entry in race.entries
                            ),
                        )
                        for race in round_.races
                    ),
                ),
                Win5Submission(
                    id=submission.id,
                    round_id=submission.round_id,
                    persona_id=submission.persona_id,
                    tier=submission.tier,
                    # The shared validator describes accepted desired-state shape;
                    # lifecycle consistency for retained cancelled history is checked above.
                    status=Win5SubmissionStatus.ACCEPTED,
                    picks=tuple(domain_picks),
                ),
            )

            judgement = submission.judgement
            if round_.round_type == Win5RoundType.SPECIAL:
                if round_.status != Win5RoundStatus.SCORED:
                    if judgement is not None:
                        raise Win5SubmissionsInvalidSourceError("Unscored Special Submission has a score event.")
                    continue
                if submission.status == Win5SubmissionStatus.ACCEPTED and not isinstance(
                    judgement, Win5SpecialSubmissionJudgement
                ):
                    raise Win5SubmissionsInvalidSourceError(
                        "Accepted scored Special Submission has no Special score event."
                    )
                if submission.status == Win5SubmissionStatus.CANCELLED and judgement is not None:
                    raise Win5SubmissionsInvalidSourceError("Cancelled Special Submission cannot have a score event.")
                if isinstance(judgement, Win5SpecialSubmissionJudgement):
                    Win5SubmissionHistoryQueries._validate_special_judgement(
                        judgement=judgement,
                        submission=submission,
                        round_=round_,
                        picks_by_id=picks_by_id,
                        results_by_id=results_by_id,
                        total_race_count=total_race_count,
                        total_void_race_count=total_void_race_count,
                    )
                    special_result_fingerprints.add(judgement.result_fingerprint)
                continue
            if round_.status != Win5RoundStatus.SCORED:
                if judgement is not None:
                    raise Win5SubmissionsInvalidSourceError("Unscored Normal Submission has a score event.")
                continue
            if submission.status == Win5SubmissionStatus.ACCEPTED and judgement is None:
                raise Win5SubmissionsInvalidSourceError("Accepted scored Normal Submission has no score event.")
            if submission.status == Win5SubmissionStatus.CANCELLED and judgement is not None:
                raise Win5SubmissionsInvalidSourceError("Cancelled Normal Submission cannot have a score event.")
            if judgement is not None:
                if not isinstance(judgement, Win5NormalSubmissionJudgement):
                    raise Win5SubmissionsInvalidSourceError("Normal Submission has a Special score event.")
                Win5SubmissionHistoryQueries._validate_normal_judgement(
                    judgement=judgement,
                    submission=submission,
                    round_=round_,
                    picks_by_id=picks_by_id,
                    results_by_id=results_by_id,
                    result_fingerprint=result_fingerprint,
                )

        if round_.round_type == Win5RoundType.SPECIAL and set(entries_by_id) != referenced_special_entry_ids:
            raise Win5SubmissionsInvalidSourceError("Special history contains an unreferenced RaceEntry projection.")
        if len(special_result_fingerprints) > 1:
            raise Win5SubmissionsInvalidSourceError("Special history events disagree on the Result fingerprint.")

    @staticmethod
    def _validate_projected_identity(
        *,
        round_type: Win5RoundType,
        race_id: int,
        race_entry_id: int | None,
        gate_number: int | None,
        reference_entry: Win5RaceEntryOption | None,
        entries_by_id: dict[int, tuple[int, Win5RaceEntryOption]],
    ) -> None:
        if round_type == Win5RoundType.NORMAL:
            if race_entry_id is None or gate_number is not None:
                raise Win5SubmissionsInvalidSourceError("Normal pick/result discriminator is invalid.")
            stored_entry = entries_by_id.get(race_entry_id)
            if stored_entry is None or stored_entry[0] != race_id or reference_entry != stored_entry[1]:
                raise Win5SubmissionsInvalidSourceError("Normal pick/result RaceEntry projection is invalid.")
        elif (
            race_entry_id is not None
            or isinstance(gate_number, bool)
            or not isinstance(gate_number, int)
            or gate_number <= 0
        ):
            raise Win5SubmissionsInvalidSourceError("Special pick/result discriminator is invalid.")
        elif reference_entry is not None:
            stored_entry = entries_by_id.get(reference_entry.id)
            if (
                stored_entry is None
                or stored_entry[0] != race_id
                or stored_entry[1] != reference_entry
                or reference_entry.gate_number != gate_number
            ):
                raise Win5SubmissionsInvalidSourceError("Special reference Entry projection is invalid.")

    @staticmethod
    def _validate_special_judgement(
        *,
        judgement: Win5SpecialSubmissionJudgement,
        submission: Win5MemberSubmission,
        round_: Win5MemberSubmissionRound,
        picks_by_id: dict[int, Win5MemberSubmissionPick],
        results_by_id: dict[int, Win5MemberResult],
        total_race_count: int,
        total_void_race_count: int,
    ) -> None:
        expected_policy_version = (
            WIN5_SPECIAL_SCORING_POLICY_VERSION
            if total_void_race_count == 0
            else WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION
        )
        if (
            judgement.season_id != round_.season_id
            or judgement.round_id != round_.id
            or judgement.submission_id != submission.id
            or judgement.submission_version != submission.version
            or judgement.persona_id != submission.persona_id
            or judgement.tier != Win5SubmissionTier.SPECIAL_WINNER
            or submission.tier != Win5SubmissionTier.SPECIAL_WINNER
            or judgement.race_count != total_race_count
            or judgement.void_count != total_void_race_count
            or judgement.scoring_policy_version != expected_policy_version
            or judgement.reward_policy_version != WIN5_SPECIAL_REWARD_POLICY_VERSION
        ):
            raise Win5SubmissionsInvalidSourceError("Special score-event identity is inconsistent.")
        if (
            len(judgement.result_fingerprint) != 64
            or judgement.result_fingerprint.lower() != judgement.result_fingerprint
            or any(character not in "0123456789abcdef" for character in judgement.result_fingerprint)
        ):
            raise Win5SubmissionsInvalidSourceError("Special score-event Result fingerprint is malformed.")

        validate_special_score_totals(
            race_count=judgement.race_count,
            exact_count=judgement.exact_count,
            off_board_count=judgement.off_board_count,
            missing_count=judgement.missing_count,
            void_count=judgement.void_count,
            season_score_delta=judgement.season_score_delta,
            top1_score_delta=judgement.top1_score_delta,
            circle_point_reward=judgement.circle_point_reward,
        )
        page_race_ids = tuple(race.id for race in round_.races)
        if tuple(item.race_id for item in judgement.items) != page_race_ids:
            raise Win5SubmissionsInvalidSourceError(
                "Special judgement page must cover every projected Race in canonical order."
            )
        page_outcomes = tuple(item.outcome for item in judgement.items)
        if (
            page_outcomes.count(Win5JudgementOutcome.EXACT) > judgement.exact_count
            or page_outcomes.count(Win5JudgementOutcome.OFF_BOARD) > judgement.off_board_count
            or page_outcomes.count(Win5JudgementOutcome.MISSING) > judgement.missing_count
            or page_outcomes.count(Win5JudgementOutcome.VOID) > judgement.void_count
        ):
            raise Win5SubmissionsInvalidSourceError("Special judgement page exceeds its event aggregate.")

        picks_by_race_id = {pick.race_id: pick for pick in picks_by_id.values()}
        results_by_race_id = {result.race_id: result for result in results_by_id.values()}
        races_by_id = {race.id: race for race in round_.races}
        if len(picks_by_race_id) != len(picks_by_id) or len(results_by_race_id) != len(results_by_id):
            raise Win5SubmissionsInvalidSourceError("Special judgement provenance is not Race-unique.")
        for item in judgement.items:
            pick = picks_by_race_id.get(item.race_id)
            result = results_by_race_id.get(item.race_id)
            race = races_by_id[item.race_id]
            if item.outcome == Win5JudgementOutcome.VOID:
                expected_pick_id = pick.id if pick is not None else None
                if (
                    race.void_reason is None
                    or result is not None
                    or item.submission_pick_id != expected_pick_id
                    or item.matched_result_id is not None
                    or item.season_score_delta != 0
                ):
                    raise Win5SubmissionsInvalidSourceError("Void Special judgement provenance is invalid.")
                continue
            if race.void_reason is not None:
                raise Win5SubmissionsInvalidSourceError("A void Race has a non-void Special judgement.")
            if result is None:
                raise Win5SubmissionsInvalidSourceError("Special judgement has no authoritative winner Result.")
            if item.outcome == Win5JudgementOutcome.MISSING:
                if pick is not None:
                    raise Win5SubmissionsInvalidSourceError("Missing Special judgement contradicts a persisted pick.")
                continue
            if pick is None or item.submission_pick_id != pick.id:
                raise Win5SubmissionsInvalidSourceError("Special judgement references a foreign Submission pick.")
            if item.outcome == Win5JudgementOutcome.OFF_BOARD:
                if pick.gate_number == result.gate_number:
                    raise Win5SubmissionsInvalidSourceError("Off-board Special judgement matches its winner Result.")
                continue
            if item.matched_result_id != result.id or pick.gate_number != result.gate_number:
                raise Win5SubmissionsInvalidSourceError("Exact Special judgement provenance is invalid.")

    @staticmethod
    def _validate_normal_judgement(
        *,
        judgement: Win5NormalSubmissionJudgement,
        submission: Win5MemberSubmission,
        round_: Win5MemberSubmissionRound,
        picks_by_id: dict[int, Win5MemberSubmissionPick],
        results_by_id: dict[int, Win5MemberResult],
        result_fingerprint: str | None,
    ) -> None:
        race_id = round_.races[0].id
        if (
            judgement.season_id != round_.season_id
            or judgement.round_id != round_.id
            or judgement.race_id != race_id
            or judgement.submission_id != submission.id
            or judgement.submission_version != submission.version
            or judgement.persona_id != submission.persona_id
            or judgement.tier != submission.tier
            or judgement.score.tier != submission.tier
            or judgement.result_fingerprint != result_fingerprint
        ):
            raise Win5SubmissionsInvalidSourceError("Normal score-event identity is inconsistent.")

        for item in judgement.score.items:
            pick = picks_by_id.get(item.submission_pick_id) if item.submission_pick_id is not None else None
            result = results_by_id.get(item.matched_result_id) if item.matched_result_id is not None else None
            if item.outcome == Win5JudgementOutcome.MISSING:
                if pick is not None or result is not None:
                    raise Win5SubmissionsInvalidSourceError("Missing judgement has persisted provenance.")
                continue
            if pick is None:
                raise Win5SubmissionsInvalidSourceError("Judgement references a foreign Submission pick.")
            if pick.position != item.position:
                raise Win5SubmissionsInvalidSourceError("Judgement position does not match its Submission pick.")
            if item.outcome == Win5JudgementOutcome.OFF_BOARD:
                if result is not None or any(
                    board.race_entry_id == pick.race_entry_id for board in results_by_id.values()
                ):
                    raise Win5SubmissionsInvalidSourceError("Off-board judgement contradicts the Result board.")
                continue
            if result is None or result.race_entry_id != pick.race_entry_id:
                raise Win5SubmissionsInvalidSourceError("Judgement matched Result provenance is invalid.")
            if (item.outcome == Win5JudgementOutcome.EXACT) != (result.position == item.position):
                raise Win5SubmissionsInvalidSourceError("Judgement outcome contradicts the Result position.")
