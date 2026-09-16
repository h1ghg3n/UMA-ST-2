"""Application-owned WIN5 Season export projection and orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionStatus,
    Win5SubmissionTier,
)
from uma_st2.shared import normalize_utc_datetime

from .artifact import ExportArtifact

WIN5_EXPORT_PROJECTION_VERSION = "win5-season-projection/v1"
WIN5_EXPORT_WORKBOOK_SCHEMA_VERSION = "win5-season-xlsx/v1"


class Win5SeasonExportError(ValueError):
    """Base error for an expected WIN5 Season export rejection."""


class Win5SeasonExportUnavailableError(Win5SeasonExportError):
    """The requested active/closed Season is unavailable."""


class Win5SeasonExportInvalidSourceError(Win5SeasonExportError):
    """Stored WIN5 facts cannot form one complete export projection."""


@dataclass(frozen=True, slots=True)
class Win5ExportSeasonChoice:
    """One active/closed Season exposed by export autocomplete."""

    id: int
    name: str
    status: Win5SeasonStatus


@dataclass(frozen=True, slots=True)
class Win5ExportEntry:
    """One Normal Race Entry display fact."""

    id: int
    gate_number: int
    name: str


@dataclass(frozen=True, slots=True)
class Win5ExportResult:
    """One stored authoritative WIN5 result position."""

    id: int
    race_id: int
    position: int
    race_entry_id: int | None
    gate_number: int | None


@dataclass(frozen=True, slots=True)
class Win5ExportRace:
    """One ordered Race and its stored entry/result/void facts."""

    id: int
    name: str
    scheduled_at: datetime | None
    void_reason: str | None
    voided_at: datetime | None
    entries: tuple[Win5ExportEntry, ...] = field(default_factory=tuple)
    results: tuple[Win5ExportResult, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5ExportRound:
    """One ordered Round in the selected Season."""

    id: int
    round_type: Win5RoundType
    status: Win5RoundStatus
    name: str
    opens_at: datetime | None
    closes_at: datetime | None
    races: tuple[Win5ExportRace, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5ExportPick:
    """One retained ordered Submission selection."""

    id: int
    race_id: int
    position: int
    race_entry_id: int | None
    gate_number: int | None


@dataclass(frozen=True, slots=True)
class Win5ExportJudgementItem:
    """One persisted score-event judgement item."""

    id: int
    race_id: int
    position: int
    submission_pick_id: int | None
    matched_result_id: int | None
    outcome: Win5JudgementOutcome
    season_score_delta: int


@dataclass(frozen=True, slots=True)
class Win5ExportScoreEvent:
    """Persisted aggregate scoring authority for one Submission."""

    id: int
    season_id: int
    round_id: int
    persona_id: str
    submission_version: int
    tier: Win5SubmissionTier
    exact_count: int
    wrong_position_count: int
    off_board_count: int
    missing_count: int
    season_score_delta: int
    top1_score_delta: int
    circle_point_reward: int
    created_at: datetime
    items: tuple[Win5ExportJudgementItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5ExportSubmission:
    """One retained current Submission row and optional stored judgement."""

    id: int
    round_id: int
    persona_id: str
    display_name: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    version: int
    created_at: datetime
    updated_at: datetime
    picks: tuple[Win5ExportPick, ...] = field(default_factory=tuple)
    score_event: Win5ExportScoreEvent | None = None


@dataclass(frozen=True, slots=True)
class Win5ExportScore:
    """Current persisted score projection for one Persona."""

    persona_id: str
    display_name: str
    season_score: int
    top1_score: int


@dataclass(frozen=True, slots=True)
class Win5SeasonExportSource:
    """Complete closed-session persistence source for one Season."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    starts_at: datetime | None
    ends_at: datetime | None
    source_cutoff: datetime
    rounds: tuple[Win5ExportRound, ...] = field(default_factory=tuple)
    submissions: tuple[Win5ExportSubmission, ...] = field(default_factory=tuple)
    scores: tuple[Win5ExportScore, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5ExportStanding:
    """One competition-ranked persisted score value."""

    rank: int
    persona_id: str
    display_name: str
    score: int


@dataclass(frozen=True, slots=True)
class Win5ExportProjection:
    """Renderer-ready complete WIN5 Season snapshot."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    starts_at: datetime | None
    ends_at: datetime | None
    source_cutoff: datetime
    rounds: tuple[Win5ExportRound, ...]
    submissions: tuple[Win5ExportSubmission, ...]
    season_standings: tuple[Win5ExportStanding, ...]
    top1_standings: tuple[Win5ExportStanding, ...]
    hall_of_fame_submission_ids: tuple[int, ...]
    participant_count: int
    projection_version: str = WIN5_EXPORT_PROJECTION_VERSION

    @property
    def race_count(self) -> int:
        return sum(len(round_.races) for round_ in self.rounds)

    @property
    def judgement_row_count(self) -> int:
        count = 0
        for submission in self.submissions:
            count += len(submission.picks)
            if submission.score_event is not None:
                pick_ids = {pick.id for pick in submission.picks}
                count += sum(
                    item.submission_pick_id is None or item.submission_pick_id not in pick_ids
                    for item in submission.score_event.items
                )
        return count

    @property
    def workbook_data_row_count(self) -> int:
        return (
            max(len(self.season_standings), len(self.top1_standings))
            + self.race_count
            + self.judgement_row_count
            + len(self.hall_of_fame_submission_ids)
        )


class Win5SeasonExportRepository(Protocol):
    """Read-only persistence port for one complete WIN5 Season snapshot."""

    def search_seasons(self, *, query: str, limit: int) -> tuple[Win5ExportSeasonChoice, ...]: ...

    def get_season_source(
        self,
        *,
        season_id: int,
        source_cutoff: datetime,
    ) -> Win5SeasonExportSource | None: ...


class Win5SeasonExportUnitOfWork(UnitOfWork, Protocol):
    """Fresh read-only UoW for a complete Season projection."""

    @property
    def win5_season_exports(self) -> Win5SeasonExportRepository: ...


class Win5SeasonExportRenderer(Protocol):
    """Serialize one closed projection without persistence resources."""

    def render(
        self,
        projection: Win5ExportProjection,
        *,
        generated_at: datetime,
    ) -> ExportArtifact: ...


@dataclass(frozen=True, slots=True)
class Win5SeasonExports:
    """Query, validate and render complete WIN5 Season artifacts."""

    query_runner: QueryRunner[Win5SeasonExportUnitOfWork]
    renderer: Win5SeasonExportRenderer
    clock: Callable[[], datetime]

    def search_seasons(self, *, query: str = "", limit: int = 25) -> tuple[Win5ExportSeasonChoice, ...]:
        if not isinstance(query, str):
            raise ValueError("query must be a string.")
        normalized_query = query.strip()
        if len(normalized_query) > 100:
            raise ValueError("query must contain at most 100 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")
        try:
            choices = self.query_runner.run(
                lambda uow: uow.win5_season_exports.search_seasons(
                    query=normalized_query,
                    limit=limit,
                )
            )
            if len(choices) > limit:
                raise Win5SeasonExportInvalidSourceError("WIN5 Season choice limit was exceeded.")
            for choice in choices:
                _validate_choice(choice)
            _require_unique((choice.id for choice in choices), field_name="Season choice IDs")
            return choices
        except Win5SeasonExportError:
            raise
        except (TypeError, ValueError) as exc:
            raise Win5SeasonExportInvalidSourceError("Stored WIN5 Season choices are malformed.") from exc

    def export_season(self, *, season_id: int) -> ExportArtifact:
        _require_positive_int(season_id, field_name="season_id")
        source_cutoff = normalize_utc_datetime(self.clock(), field_name="source_cutoff")
        try:
            source = self.query_runner.run(
                lambda uow: uow.win5_season_exports.get_season_source(
                    season_id=season_id,
                    source_cutoff=source_cutoff,
                )
            )
            if source is None:
                raise Win5SeasonExportUnavailableError("WIN5 Season is not available for export.")
            if source.source_cutoff != source_cutoff:
                raise Win5SeasonExportInvalidSourceError("WIN5 Season source cutoff does not match the request.")
            projection = _build_projection(source)
        except Win5SeasonExportError:
            raise
        except (TypeError, ValueError) as exc:
            raise Win5SeasonExportInvalidSourceError("Stored WIN5 Season export facts are malformed.") from exc

        generated_at = normalize_utc_datetime(self.clock(), field_name="generated_at")
        artifact = self.renderer.render(projection, generated_at=generated_at)
        if (
            artifact.schema_version != WIN5_EXPORT_WORKBOOK_SCHEMA_VERSION
            or artifact.projection_version != WIN5_EXPORT_PROJECTION_VERSION
            or artifact.scope_type != "win5_season"
            or artifact.scope_id != str(season_id)
            or artifact.scope_name != projection.season_name
            or artifact.source_cutoff != projection.source_cutoff
            or artifact.generated_at != generated_at
            or artifact.row_count != projection.workbook_data_row_count
        ):
            raise Win5SeasonExportInvalidSourceError("WIN5 renderer returned mismatched artifact provenance.")
        return artifact


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _validate_choice(choice: Win5ExportSeasonChoice) -> None:
    if not isinstance(choice, Win5ExportSeasonChoice):
        raise ValueError("Season choice has an invalid type.")
    _require_positive_int(choice.id, field_name="choice.id")
    if not choice.name.strip():
        raise ValueError("Season choice name must be non-empty.")
    if Win5SeasonStatus(choice.status) not in {Win5SeasonStatus.ACTIVE, Win5SeasonStatus.CLOSED}:
        raise ValueError("Season choice must be active or closed.")


def _build_projection(source: Win5SeasonExportSource) -> Win5ExportProjection:
    _validate_source(source)
    season_standings = _rank_scores(source.scores, ranking="season")
    top1_standings = _rank_scores(source.scores, ranking="top1")
    hall_of_fame_ids = tuple(
        submission.id
        for submission in source.submissions
        if _qualifies_for_hall_of_fame(submission, rounds_by_id={round_.id: round_ for round_ in source.rounds})
    )
    return Win5ExportProjection(
        season_id=source.season_id,
        season_name=source.season_name,
        season_status=source.season_status,
        starts_at=source.starts_at,
        ends_at=source.ends_at,
        source_cutoff=source.source_cutoff,
        rounds=source.rounds,
        submissions=source.submissions,
        season_standings=season_standings,
        top1_standings=top1_standings,
        hall_of_fame_submission_ids=hall_of_fame_ids,
        participant_count=len({submission.persona_id for submission in source.submissions}),
    )


def _validate_source(source: Win5SeasonExportSource) -> None:
    if not isinstance(source, Win5SeasonExportSource):
        raise ValueError("Season source has an invalid type.")
    _require_positive_int(source.season_id, field_name="season_id")
    if not source.season_name.strip():
        raise ValueError("season_name must be non-empty.")
    if Win5SeasonStatus(source.season_status) not in {Win5SeasonStatus.ACTIVE, Win5SeasonStatus.CLOSED}:
        raise ValueError("Season export requires active or closed status.")
    normalize_utc_datetime(source.source_cutoff, field_name="source_cutoff")
    for field_name, timestamp in (("starts_at", source.starts_at), ("ends_at", source.ends_at)):
        if timestamp is not None:
            normalize_utc_datetime(timestamp, field_name=field_name)

    _require_unique((round_.id for round_ in source.rounds), field_name="Round IDs")
    rounds_by_id = {round_.id: round_ for round_ in source.rounds}
    race_by_id: dict[int, tuple[Win5ExportRound, Win5ExportRace]] = {}
    entry_by_id: dict[int, tuple[int, Win5ExportEntry]] = {}
    result_by_id: dict[int, Win5ExportResult] = {}
    for round_ in source.rounds:
        _require_positive_int(round_.id, field_name="round.id")
        Win5RoundType(round_.round_type)
        Win5RoundStatus(round_.status)
        if not round_.name.strip() or not round_.races:
            raise ValueError("Every exported Round must have a name and at least one Race.")
        if round_.round_type == Win5RoundType.NORMAL and len(round_.races) != 1:
            raise ValueError("Normal export Round must contain exactly one Race.")
        for timestamp_name, timestamp in (("opens_at", round_.opens_at), ("closes_at", round_.closes_at)):
            if timestamp is not None:
                normalize_utc_datetime(timestamp, field_name=timestamp_name)
        for race in round_.races:
            _require_positive_int(race.id, field_name="race.id")
            if race.id in race_by_id or not race.name.strip():
                raise ValueError("Race IDs must be unique and names non-empty.")
            if (race.void_reason is None) != (race.voided_at is None):
                raise ValueError("Race void provenance must be complete.")
            if race.void_reason is not None and not race.void_reason.strip():
                raise ValueError("Race void reason must be non-empty.")
            if race.scheduled_at is not None:
                normalize_utc_datetime(race.scheduled_at, field_name="scheduled_at")
            if race.voided_at is not None:
                normalize_utc_datetime(race.voided_at, field_name="voided_at")
            race_by_id[race.id] = (round_, race)
            _validate_race_values(round_=round_, race=race, entry_by_id=entry_by_id, result_by_id=result_by_id)

    _require_unique((submission.id for submission in source.submissions), field_name="Submission IDs")
    for submission in source.submissions:
        _validate_submission(
            submission,
            rounds_by_id=rounds_by_id,
            race_by_id=race_by_id,
            entry_by_id=entry_by_id,
            result_by_id=result_by_id,
            season_id=source.season_id,
        )
    _require_unique(
        (pick.id for submission in source.submissions for pick in submission.picks),
        field_name="global SubmissionPick IDs",
    )
    _require_unique(
        (submission.score_event.id for submission in source.submissions if submission.score_event is not None),
        field_name="ScoreEvent IDs",
    )
    _require_unique(
        (
            item.id
            for submission in source.submissions
            if submission.score_event is not None
            for item in submission.score_event.items
        ),
        field_name="global ScoreEventItem IDs",
    )
    _require_unique((score.persona_id for score in source.scores), field_name="score Persona IDs")
    for score in source.scores:
        if not score.persona_id or not score.display_name.strip():
            raise ValueError("Score Persona identity must be complete.")


def _validate_race_values(
    *,
    round_: Win5ExportRound,
    race: Win5ExportRace,
    entry_by_id: dict[int, tuple[int, Win5ExportEntry]],
    result_by_id: dict[int, Win5ExportResult],
) -> None:
    _require_unique((entry.id for entry in race.entries), field_name="RaceEntry IDs")
    _require_unique((entry.gate_number for entry in race.entries), field_name="RaceEntry gate numbers")
    for entry in race.entries:
        _require_positive_int(entry.id, field_name="entry.id")
        _require_positive_int(entry.gate_number, field_name="entry.gate_number")
        if entry.id in entry_by_id or not entry.name.strip():
            raise ValueError("RaceEntry IDs must be globally unique and names non-empty.")
        entry_by_id[entry.id] = (race.id, entry)
    _require_unique((result.id for result in race.results), field_name="Result IDs")
    _require_unique((result.position for result in race.results), field_name="Result positions")
    for result in race.results:
        _require_positive_int(result.id, field_name="result.id")
        _require_positive_int(result.position, field_name="result.position")
        if result.id in result_by_id or result.race_id != race.id:
            raise ValueError("Result identity is inconsistent with its Race.")
        if round_.round_type == Win5RoundType.NORMAL:
            if result.race_entry_id is None or result.gate_number is not None:
                raise ValueError("Normal Result must reference one RaceEntry.")
            referenced_entry = entry_by_id.get(result.race_entry_id)
            if referenced_entry is None or referenced_entry[0] != race.id:
                raise ValueError("Normal Result references an unavailable RaceEntry.")
        else:
            if result.race_entry_id is not None or result.gate_number is None:
                raise ValueError("Special Result must store one gate number.")
            _require_positive_int(result.gate_number, field_name="result.gate_number")
        result_by_id[result.id] = result


def _validate_submission(
    submission: Win5ExportSubmission,
    *,
    rounds_by_id: dict[int, Win5ExportRound],
    race_by_id: dict[int, tuple[Win5ExportRound, Win5ExportRace]],
    entry_by_id: dict[int, tuple[int, Win5ExportEntry]],
    result_by_id: dict[int, Win5ExportResult],
    season_id: int,
) -> None:
    _require_positive_int(submission.id, field_name="submission.id")
    round_ = rounds_by_id.get(submission.round_id)
    if round_ is None or not submission.persona_id or not submission.display_name.strip():
        raise ValueError("Submission identity is incomplete.")
    tier = Win5SubmissionTier(submission.tier)
    Win5SubmissionStatus(submission.status)
    _require_positive_int(submission.version, field_name="submission.version")
    normalize_utc_datetime(submission.created_at, field_name="submission.created_at")
    normalize_utc_datetime(submission.updated_at, field_name="submission.updated_at")
    if round_.round_type == Win5RoundType.NORMAL and tier not in {
        Win5SubmissionTier.TOP1,
        Win5SubmissionTier.TOP3,
        Win5SubmissionTier.TOP5,
    }:
        raise ValueError("Normal Submission tier is invalid.")
    if round_.round_type == Win5RoundType.SPECIAL and tier != Win5SubmissionTier.SPECIAL_WINNER:
        raise ValueError("Special Submission tier is invalid.")

    _require_unique((pick.id for pick in submission.picks), field_name="SubmissionPick IDs")
    if not submission.picks:
        raise ValueError("Every retained Submission must contain at least one pick.")
    pick_by_id: dict[int, Win5ExportPick] = {}
    for pick in submission.picks:
        _require_positive_int(pick.id, field_name="pick.id")
        _require_positive_int(pick.position, field_name="pick.position")
        race_pair = race_by_id.get(pick.race_id)
        if race_pair is None or race_pair[0].id != round_.id:
            raise ValueError("SubmissionPick references a Race outside its Round.")
        if round_.round_type == Win5RoundType.NORMAL:
            if pick.race_entry_id is None or pick.gate_number is not None:
                raise ValueError("Normal pick must reference one RaceEntry.")
            referenced_entry = entry_by_id.get(pick.race_entry_id)
            if referenced_entry is None or referenced_entry[0] != pick.race_id:
                raise ValueError("Normal pick references an unavailable RaceEntry.")
        else:
            if pick.race_entry_id is not None or pick.gate_number is None:
                raise ValueError("Special pick must store one gate number.")
            _require_positive_int(pick.gate_number, field_name="pick.gate_number")
        pick_by_id[pick.id] = pick

    event = submission.score_event
    if event is None:
        return
    if submission.status != Win5SubmissionStatus.ACCEPTED:
        raise ValueError("Cancelled Submission must not have a ScoreEvent.")
    _require_positive_int(event.id, field_name="score_event.id")
    if (
        event.season_id != season_id
        or event.round_id != submission.round_id
        or event.persona_id != submission.persona_id
        or event.submission_version != submission.version
        or event.tier != tier
    ):
        raise ValueError("ScoreEvent does not match its Submission revision.")
    normalize_utc_datetime(event.created_at, field_name="score_event.created_at")
    for count in (
        event.exact_count,
        event.wrong_position_count,
        event.off_board_count,
        event.missing_count,
        event.circle_point_reward,
    ):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("ScoreEvent aggregate counts/reward must be non-negative integers.")
    _require_unique((item.id for item in event.items), field_name="ScoreEventItem IDs")
    for item in event.items:
        _require_positive_int(item.id, field_name="score_event_item.id")
        _require_positive_int(item.position, field_name="score_event_item.position")
        race_pair = race_by_id.get(item.race_id)
        if race_pair is None or race_pair[0].id != round_.id:
            raise ValueError("ScoreEventItem references a Race outside its Round.")
        outcome = Win5JudgementOutcome(item.outcome)
        if item.submission_pick_id is None:
            if outcome != Win5JudgementOutcome.MISSING:
                raise ValueError("Only missing judgement may omit SubmissionPick provenance.")
        else:
            pick = pick_by_id.get(item.submission_pick_id)
            if pick is None or pick.race_id != item.race_id or pick.position != item.position:
                raise ValueError("ScoreEventItem references an inconsistent SubmissionPick.")
        if item.matched_result_id is not None:
            result = result_by_id.get(item.matched_result_id)
            if result is None or result.race_id != item.race_id:
                raise ValueError("ScoreEventItem references an inconsistent Result.")


def _require_unique(values: Iterable[object], *, field_name: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{field_name} must be unique.")


def _rank_scores(
    scores: tuple[Win5ExportScore, ...],
    *,
    ranking: str,
) -> tuple[Win5ExportStanding, ...]:
    value = (lambda score: score.season_score) if ranking == "season" else (lambda score: score.top1_score)
    ordered = sorted(scores, key=lambda score: (-value(score), score.persona_id))
    standings: list[Win5ExportStanding] = []
    previous_score: int | None = None
    previous_rank = 0
    for index, score in enumerate(ordered, start=1):
        current_score = value(score)
        rank = previous_rank if current_score == previous_score else index
        standings.append(
            Win5ExportStanding(
                rank=rank,
                persona_id=score.persona_id,
                display_name=score.display_name,
                score=current_score,
            )
        )
        previous_score = current_score
        previous_rank = rank
    return tuple(standings)


def _qualifies_for_hall_of_fame(
    submission: Win5ExportSubmission,
    *,
    rounds_by_id: dict[int, Win5ExportRound],
) -> bool:
    round_ = rounds_by_id[submission.round_id]
    event = submission.score_event
    if round_.round_type != Win5RoundType.NORMAL or submission.tier != Win5SubmissionTier.TOP5 or event is None:
        return False
    if len(event.items) != 5:
        return False
    return {item.position for item in event.items} == {1, 2, 3, 4, 5} and all(
        item.outcome == Win5JudgementOutcome.EXACT for item in event.items
    )
