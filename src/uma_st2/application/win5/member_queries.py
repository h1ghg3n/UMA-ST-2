"""Bounded member-facing WIN5 queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.identity import PersonaStatus
from uma_st2.domain.win5 import (
    Win5DomainError,
    Win5NormalSubmissionScore,
    Win5Race,
    Win5RaceEntry,
    Win5Round,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialJudgementItem,
    Win5Submission,
    Win5SubmissionPick,
    Win5SubmissionStatus,
    Win5SubmissionTier,
    validate_submission_for_round,
)

from .member_identity import Win5MemberPersona
from .round_lifecycle import MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON


class Win5MemberQueryError(ValueError):
    """Base error for an expected member-facing WIN5 query rejection."""


class Win5MemberQueryIdentityError(Win5MemberQueryError):
    """The Discord actor has no currently authorized Persona."""


class Win5MemberQueryApprovalPendingError(Win5MemberQueryIdentityError):
    """The actor Persona is read-only while approval is pending."""


class Win5MemberQueryInvalidSourceError(Win5MemberQueryError):
    """Stored member-query facts violate a shared WIN5 invariant."""


class Win5ActiveSeasonUnavailableError(Win5MemberQueryError):
    """No active WIN5 Season is available for the member summary."""


class Win5CancellableSubmissionUnavailableError(Win5MemberQueryError):
    """The requested accepted Submission is no longer cancellable by the actor."""


class Win5StandingsSeasonUnavailableError(Win5MemberQueryError):
    """The requested Season cannot be used for public standings."""


class Win5NormalSubmissionRoundUnavailableError(Win5MemberQueryError):
    """The requested Round cannot currently provide a Normal editor."""


class Win5NormalSubmissionInvalidSourceError(Win5MemberQueryError):
    """Stored facts cannot form a safe Normal Submission editor."""


class Win5SpecialSubmissionRoundUnavailableError(Win5MemberQueryError):
    """The requested Round cannot currently provide a Special editor."""


class Win5SpecialSubmissionInvalidSourceError(Win5MemberQueryError):
    """Stored facts cannot form a safe Special Submission editor."""


class Win5SubmissionsUnavailableError(Win5MemberQueryError):
    """The active-Season member Submission dashboard is unavailable."""


class Win5SubmissionsInvalidSourceError(Win5MemberQueryError):
    """Stored facts cannot form a safe member Submission dashboard."""


Win5StandingRanking = Literal["season", "top1"]


@dataclass(frozen=True, slots=True)
class Win5RaceEntryOption:
    """Canonical or reference entry rendered for one WIN5 Race."""

    id: int
    gate_number: int
    name: str


@dataclass(frozen=True, slots=True)
class Win5RaceCard:
    """One Race in a member-facing Round card."""

    id: int
    name: str
    scheduled_at: datetime | None
    entries: tuple[Win5RaceEntryOption, ...] = field(default_factory=tuple)
    void_reason: str | None = None
    voided_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Win5OpenRoundCard:
    """Immutable projection for an open Round in an active Season."""

    id: int
    season_id: int
    season_name: str
    round_type: Win5RoundType
    name: str
    opens_at: datetime | None
    closes_at: datetime | None
    races: tuple[Win5RaceCard, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionRoundChoice:
    """One open Normal Round available to the member editor selector."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_id: int
    race_name: str


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionSource:
    """Accepted Submission facts revalidated by the member query application."""

    id: int
    round_id: int
    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    active_marker: bool | None
    version: int
    picks: tuple[Win5SubmissionPick, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionEditorSource:
    """Persistence projection for one member-owned Normal editor."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    races: tuple[Win5RaceCard, ...] = field(default_factory=tuple)
    submission: Win5NormalSubmissionSource | None = None


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionPick:
    """One current canonical Entry selection shown in the Normal editor."""

    position: int
    race_entry_id: int


@dataclass(frozen=True, slots=True)
class Win5AcceptedNormalSubmission:
    """Current accepted Submission identity and stale-write token."""

    id: int
    tier: Win5SubmissionTier
    version: int
    picks: tuple[Win5NormalSubmissionPick, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionEditor:
    """Complete closed-session state for one Normal member editor."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_id: int
    race_name: str
    entries: tuple[Win5RaceEntryOption, ...] = field(default_factory=tuple)
    submission: Win5AcceptedNormalSubmission | None = None


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionRoundChoice:
    """One open Special Round available to the member editor selector."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    race_count: int


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionSource:
    """Accepted Special Submission facts revalidated by the query application."""

    id: int
    round_id: int
    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    active_marker: bool | None
    version: int
    picks: tuple[Win5SubmissionPick, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionEditorSource:
    """Persistence projection for one member-owned Special editor."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round_id: int
    round_name: str
    round_type: Win5RoundType
    round_status: Win5RoundStatus
    races: tuple[Win5RaceCard, ...] = field(default_factory=tuple)
    submission: Win5SpecialSubmissionSource | None = None


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionPick:
    """One current gate-number winner selection shown in the Special editor."""

    race_id: int
    gate_number: int


@dataclass(frozen=True, slots=True)
class Win5AcceptedSpecialSubmission:
    """Current accepted Special Submission identity and stale-write token."""

    id: int
    version: int
    picks: tuple[Win5SpecialSubmissionPick, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionEditor:
    """Complete closed-session state for one Special member editor."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    races: tuple[Win5RaceCard, ...] = field(default_factory=tuple)
    submission: Win5AcceptedSpecialSubmission | None = None


@dataclass(frozen=True, slots=True)
class Win5CancellableSubmission:
    """One member-owned accepted Submission available for explicit cancellation."""

    season_id: int
    season_name: str
    round_id: int
    round_name: str
    round_type: Win5RoundType
    submission_id: int
    tier: Win5SubmissionTier
    version: int
    pick_count: int


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionPick:
    """One actual persisted pick with optional display reference."""

    id: int
    race_id: int
    position: int
    race_entry_id: int | None
    gate_number: int | None
    reference_entry: Win5RaceEntryOption | None = None


@dataclass(frozen=True, slots=True)
class Win5MemberResult:
    """One authoritative persisted Result with optional display reference."""

    id: int
    race_id: int
    position: int
    race_entry_id: int | None
    gate_number: int | None
    reference_entry: Win5RaceEntryOption | None = None


@dataclass(frozen=True, slots=True)
class Win5NormalSubmissionJudgement:
    """Persisted Normal score-event authority for one Submission."""

    event_id: int
    season_id: int
    round_id: int
    race_id: int
    submission_id: int
    submission_version: int
    persona_id: str
    tier: Win5SubmissionTier
    result_fingerprint: str
    scoring_policy_version: str
    reward_policy_version: str
    score: Win5NormalSubmissionScore


@dataclass(frozen=True, slots=True)
class Win5SpecialSubmissionJudgement:
    """Persisted Special event aggregate plus one bounded Race item page."""

    event_id: int
    season_id: int
    round_id: int
    submission_id: int
    submission_version: int
    persona_id: str
    tier: Win5SubmissionTier
    result_fingerprint: str
    scoring_policy_version: str
    reward_policy_version: str
    race_count: int
    exact_count: int
    off_board_count: int
    missing_count: int
    season_score_delta: int
    top1_score_delta: int
    circle_point_reward: int
    void_count: int = 0
    items: tuple[Win5SpecialJudgementItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5MemberSubmission:
    """One accepted or cancelled Persona-owned Submission history row."""

    id: int
    round_id: int
    persona_id: str
    tier: Win5SubmissionTier
    status: Win5SubmissionStatus
    active_marker: bool | None
    version: int
    created_at: datetime
    updated_at: datetime
    picks: tuple[Win5MemberSubmissionPick, ...] = field(default_factory=tuple)
    judgement: Win5NormalSubmissionJudgement | Win5SpecialSubmissionJudgement | None = None


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionRound:
    """One bounded page of an active-Season Round's member-owned history."""

    id: int
    season_id: int
    round_type: Win5RoundType
    status: Win5RoundStatus
    name: str
    opens_at: datetime | None
    closes_at: datetime | None
    races: tuple[Win5RaceCard, ...] = field(default_factory=tuple)
    results: tuple[Win5MemberResult, ...] = field(default_factory=tuple)
    submissions: tuple[Win5MemberSubmission, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionRoundSummary:
    """Child-free root summary for one active-Season Round."""

    id: int
    season_id: int
    round_type: Win5RoundType
    status: Win5RoundStatus
    name: str
    submission_count: int


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionsDashboardSource:
    """Child-free persistence projection awaiting application validation."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    open_round_overflow: bool
    open_rounds: tuple[Win5MemberSubmissionRoundSummary, ...] = field(default_factory=tuple)
    scored_rounds: tuple[Win5MemberSubmissionRoundSummary, ...] = field(default_factory=tuple)
    cancelled_rounds: tuple[Win5MemberSubmissionRoundSummary, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionsDashboard:
    """Child-free active-Season Submission dashboard for one active Persona."""

    season_id: int
    season_name: str
    open_rounds: tuple[Win5MemberSubmissionRoundSummary, ...] = field(default_factory=tuple)
    scored_rounds: tuple[Win5MemberSubmissionRoundSummary, ...] = field(default_factory=tuple)
    cancelled_rounds: tuple[Win5MemberSubmissionRoundSummary, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionRoundPage:
    """One closed, independently bounded Submission/Race detail page."""

    season_id: int
    season_name: str
    round: Win5MemberSubmissionRound
    total_submission_count: int
    submission_offset: int
    submission_limit: int
    total_race_count: int
    race_offset: int
    race_limit: int
    total_void_race_count: int = 0


@dataclass(frozen=True, slots=True)
class Win5MemberSubmissionRoundPageSource:
    """Persistence detail page plus active-Season validation state."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    round: Win5MemberSubmissionRound
    total_submission_count: int
    submission_offset: int
    submission_limit: int
    total_race_count: int
    race_offset: int
    race_limit: int
    total_void_race_count: int = 0


@dataclass(frozen=True, slots=True)
class Win5ActiveSeasonInfo:
    """Bounded active-Season and personal-score projection."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    starts_at: datetime | None
    ends_at: datetime | None
    total_round_count: int
    open_round_count: int
    season_score: int
    top1_score: int


@dataclass(frozen=True, slots=True)
class Win5SeasonChoice:
    """One active or closed Season available to a public selector."""

    id: int
    name: str
    status: Win5SeasonStatus


@dataclass(frozen=True, slots=True)
class Win5StandingEntry:
    """One Persona-owned score with its competition rank."""

    rank: int
    persona_id: str
    display_name: str
    score: int


@dataclass(frozen=True, slots=True)
class Win5StandingScore:
    """One Persona-owned score row before application ranking."""

    persona_id: str
    display_name: str
    score: int


@dataclass(frozen=True, slots=True)
class Win5StandingsSource:
    """Persistence projection used to build authoritative standings ranks."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    ranking: Win5StandingRanking
    scores: tuple[Win5StandingScore, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Win5Standings:
    """Bounded standings projection for one active or closed Season."""

    season_id: int
    season_name: str
    season_status: Win5SeasonStatus
    ranking: Win5StandingRanking
    entries: tuple[Win5StandingEntry, ...] = field(default_factory=tuple)


class Win5MemberQueryRepository(Protocol):
    """Read-only persistence port for member WIN5 projections."""

    def find_member_persona(self, *, discord_user_id: str) -> Win5MemberPersona | None: ...

    def has_active_open_round_overflow(self) -> bool: ...

    def get_active_season_info(self, *, persona_id: str) -> Win5ActiveSeasonInfo | None: ...

    def list_open_rounds(self, *, limit: int) -> tuple[Win5OpenRoundCard, ...]: ...

    def search_normal_submission_rounds(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[Win5NormalSubmissionRoundChoice, ...]: ...

    def get_normal_submission_editor_source(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5NormalSubmissionEditorSource | None: ...

    def search_special_submission_rounds(
        self,
        *,
        query: str,
        limit: int,
    ) -> tuple[Win5SpecialSubmissionRoundChoice, ...]: ...

    def get_special_submission_editor_source(
        self,
        *,
        round_id: int,
        persona_id: str,
    ) -> Win5SpecialSubmissionEditorSource | None: ...

    def search_cancellable_submissions(
        self,
        *,
        persona_id: str,
        query: str,
        limit: int,
    ) -> tuple[Win5CancellableSubmission, ...]: ...

    def get_cancellable_submission(
        self,
        *,
        submission_id: int,
        persona_id: str,
    ) -> Win5CancellableSubmission | None: ...

    def search_standings_seasons(self, *, query: str, limit: int) -> tuple[Win5SeasonChoice, ...]: ...

    def get_standings(
        self,
        *,
        season_id: int,
        ranking: Win5StandingRanking,
        limit: int,
    ) -> Win5StandingsSource | None: ...


class Win5MemberQueryUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the bounded WIN5 query repository."""

    @property
    def win5_member_queries(self) -> Win5MemberQueryRepository: ...


@dataclass(frozen=True, slots=True)
class Win5MemberQueries:
    """Application entry point for member-facing WIN5 read operations."""

    query_runner: QueryRunner[Win5MemberQueryUnitOfWork]

    def get_active_season_info(self, *, discord_user_id: str) -> Win5ActiveSeasonInfo:
        """Return the active Season and personal Persona-owned score projection."""

        if not isinstance(discord_user_id, str) or not discord_user_id or len(discord_user_id) > 32:
            raise ValueError("discord_user_id must be a non-empty string of at most 32 characters.")

        def query(unit_of_work: Win5MemberQueryUnitOfWork) -> Win5ActiveSeasonInfo:
            repository = unit_of_work.win5_member_queries
            member = repository.find_member_persona(discord_user_id=discord_user_id)
            if member is None or not member.can_read_member_history:
                raise Win5MemberQueryIdentityError(
                    "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
                )
            info = repository.get_active_season_info(persona_id=member.id)
            if info is None:
                raise Win5ActiveSeasonUnavailableError("No active WIN5 Season is available.")
            self._require_active_open_round_bound(
                repository,
                observed_count=info.open_round_count,
            )
            return info

        return self.query_runner.run(query)

    def list_open_rounds(self, *, limit: int = 25) -> tuple[Win5OpenRoundCard, ...]:
        """Return a bounded open-Round catalog without committing a transaction."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100.")

        def query(unit_of_work: Win5MemberQueryUnitOfWork) -> tuple[Win5OpenRoundCard, ...]:
            repository = unit_of_work.win5_member_queries
            self._require_active_open_round_bound(repository)
            rounds = repository.list_open_rounds(limit=limit)
            if len(rounds) > MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON:
                raise Win5MemberQueryInvalidSourceError("An active Season contains more than 25 open Rounds.")
            return rounds

        return self.query_runner.run(query)

    def search_normal_submission_rounds(
        self,
        *,
        discord_user_id: str,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5NormalSubmissionRoundChoice, ...]:
        """Return authorized open Normal Round choices without committing."""

        self._validate_discord_user_id(discord_user_id)
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be a string of at most 200 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")

        normalized_query = query.strip()

        def search(
            unit_of_work: Win5MemberQueryUnitOfWork,
        ) -> tuple[Win5NormalSubmissionRoundChoice, ...]:
            repository = unit_of_work.win5_member_queries
            self._require_active_member(repository, discord_user_id=discord_user_id)
            self._require_active_open_round_bound(repository)
            return repository.search_normal_submission_rounds(
                query=normalized_query,
                limit=limit,
            )

        return self.query_runner.run(search)

    def get_normal_submission_editor(
        self,
        *,
        discord_user_id: str,
        round_id: int,
    ) -> Win5NormalSubmissionEditor:
        """Return a member-owned open Normal editor with accepted picks prefilled."""

        self._validate_discord_user_id(discord_user_id)
        if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id <= 0:
            raise ValueError("round_id must be a positive integer.")

        def query(unit_of_work: Win5MemberQueryUnitOfWork) -> Win5NormalSubmissionEditor:
            repository = unit_of_work.win5_member_queries
            member = self._require_active_member(repository, discord_user_id=discord_user_id)
            try:
                source = repository.get_normal_submission_editor_source(
                    round_id=round_id,
                    persona_id=member.id,
                )
            except (TypeError, ValueError) as exc:
                raise Win5NormalSubmissionInvalidSourceError(
                    "Stored Normal Submission editor facts are malformed."
                ) from exc
            if source is None:
                raise Win5NormalSubmissionRoundUnavailableError("Normal Submission editor target does not exist.")
            return self._build_normal_submission_editor(source=source, persona_id=member.id)

        return self.query_runner.run(query)

    def search_special_submission_rounds(
        self,
        *,
        discord_user_id: str,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5SpecialSubmissionRoundChoice, ...]:
        """Return authorized open Special Round choices without committing."""

        self._validate_discord_user_id(discord_user_id)
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be a string of at most 200 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")

        normalized_query = query.strip()

        def search(
            unit_of_work: Win5MemberQueryUnitOfWork,
        ) -> tuple[Win5SpecialSubmissionRoundChoice, ...]:
            repository = unit_of_work.win5_member_queries
            self._require_active_member(repository, discord_user_id=discord_user_id)
            self._require_active_open_round_bound(repository)
            return repository.search_special_submission_rounds(
                query=normalized_query,
                limit=limit,
            )

        return self.query_runner.run(search)

    def get_special_submission_editor(
        self,
        *,
        discord_user_id: str,
        round_id: int,
    ) -> Win5SpecialSubmissionEditor:
        """Return a member-owned open Special editor with accepted picks prefilled."""

        self._validate_discord_user_id(discord_user_id)
        if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id <= 0:
            raise ValueError("round_id must be a positive integer.")

        def query(unit_of_work: Win5MemberQueryUnitOfWork) -> Win5SpecialSubmissionEditor:
            repository = unit_of_work.win5_member_queries
            member = self._require_active_member(repository, discord_user_id=discord_user_id)
            try:
                source = repository.get_special_submission_editor_source(
                    round_id=round_id,
                    persona_id=member.id,
                )
            except (TypeError, ValueError) as exc:
                raise Win5SpecialSubmissionInvalidSourceError(
                    "Stored Special Submission editor facts are malformed."
                ) from exc
            if source is None:
                raise Win5SpecialSubmissionRoundUnavailableError("Special Submission editor target does not exist.")
            return self._build_special_submission_editor(source=source, persona_id=member.id)

        return self.query_runner.run(query)

    def search_cancellable_submissions(
        self,
        *,
        discord_user_id: str,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5CancellableSubmission, ...]:
        """Return member-owned accepted Submissions in currently open Rounds."""

        self._validate_discord_user_id(discord_user_id)
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be a string of at most 200 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")

        normalized_query = query.strip()

        def search(
            unit_of_work: Win5MemberQueryUnitOfWork,
        ) -> tuple[Win5CancellableSubmission, ...]:
            repository = unit_of_work.win5_member_queries
            member = self._require_active_member(
                repository,
                discord_user_id=discord_user_id,
            )
            self._require_active_open_round_bound(repository)
            return repository.search_cancellable_submissions(
                persona_id=member.id,
                query=normalized_query,
                limit=limit,
            )

        return self.query_runner.run(search)

    def get_cancellable_submission(
        self,
        *,
        discord_user_id: str,
        submission_id: int,
    ) -> Win5CancellableSubmission:
        """Return the exact owned accepted Submission used by a cancel confirmation."""

        self._validate_discord_user_id(discord_user_id)
        if isinstance(submission_id, bool) or not isinstance(submission_id, int) or submission_id <= 0:
            raise ValueError("submission_id must be a positive integer.")

        def query(unit_of_work: Win5MemberQueryUnitOfWork) -> Win5CancellableSubmission:
            repository = unit_of_work.win5_member_queries
            member = self._require_active_member(
                repository,
                discord_user_id=discord_user_id,
            )
            target = repository.get_cancellable_submission(
                submission_id=submission_id,
                persona_id=member.id,
            )
            if target is None:
                raise Win5CancellableSubmissionUnavailableError(
                    "Submission is not accepted in an open Round owned by the actor."
                )
            return target

        return self.query_runner.run(query)

    def search_standings_seasons(
        self,
        *,
        query: str = "",
        limit: int = 25,
    ) -> tuple[Win5SeasonChoice, ...]:
        """Return a bounded active/closed Season selector projection."""

        if not isinstance(query, str) or len(query) > 100:
            raise ValueError("query must be a string of at most 100 characters.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25.")

        normalized_query = query.strip()
        return self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_member_queries.search_standings_seasons(
                query=normalized_query,
                limit=limit,
            )
        )

    def get_standings(
        self,
        *,
        season_id: int,
        ranking: Win5StandingRanking = "season",
        limit: int = 100,
    ) -> Win5Standings:
        """Return Persona-owned public standings without committing a transaction."""

        if isinstance(season_id, bool) or not isinstance(season_id, int) or season_id <= 0:
            raise ValueError("season_id must be a positive integer.")
        if ranking not in ("season", "top1"):
            raise ValueError("ranking must be 'season' or 'top1'.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 through 100.")

        source = self.query_runner.run(
            lambda unit_of_work: unit_of_work.win5_member_queries.get_standings(
                season_id=season_id,
                ranking=ranking,
                limit=limit,
            )
        )
        if source is None:
            raise Win5StandingsSeasonUnavailableError("Requested WIN5 Season is not active or closed.")

        entries: list[Win5StandingEntry] = []
        previous_score: int | None = None
        current_rank = 0
        ordered_scores = sorted(source.scores, key=lambda score: (-score.score, score.persona_id))
        for position, score in enumerate(ordered_scores, start=1):
            if score.score != previous_score:
                current_rank = position
                previous_score = score.score
            entries.append(
                Win5StandingEntry(
                    rank=current_rank,
                    persona_id=score.persona_id,
                    display_name=score.display_name,
                    score=score.score,
                )
            )
        return Win5Standings(
            season_id=source.season_id,
            season_name=source.season_name,
            season_status=source.season_status,
            ranking=source.ranking,
            entries=tuple(entries),
        )

    @staticmethod
    def _validate_discord_user_id(discord_user_id: str) -> None:
        if not isinstance(discord_user_id, str) or not discord_user_id or len(discord_user_id) > 32:
            raise ValueError("discord_user_id must be a non-empty string of at most 32 characters.")

    @staticmethod
    def _require_active_member(
        repository: Win5MemberQueryRepository,
        *,
        discord_user_id: str,
    ) -> Win5MemberPersona:
        member = repository.find_member_persona(discord_user_id=discord_user_id)
        if member is not None and member.status is PersonaStatus.PENDING_APPROVAL:
            raise Win5MemberQueryApprovalPendingError("Discord actor Persona approval is pending.")
        if member is None or not member.is_eligible:
            raise Win5MemberQueryIdentityError(
                "Discord actor requires an active Persona with at least one non-NULL PID GameAccount."
            )
        return member

    @staticmethod
    def _require_active_open_round_bound(
        repository: Win5MemberQueryRepository,
        *,
        observed_count: int | None = None,
    ) -> None:
        if observed_count is not None and (
            isinstance(observed_count, bool) or not isinstance(observed_count, int) or observed_count < 0
        ):
            raise Win5MemberQueryInvalidSourceError("Active-Season open Round count is malformed.")
        if (
            observed_count is not None and observed_count > MAX_OPEN_WIN5_ROUNDS_PER_ACTIVE_SEASON
        ) or repository.has_active_open_round_overflow():
            raise Win5MemberQueryInvalidSourceError("An active Season contains more than 25 open Rounds.")

    @staticmethod
    def _build_normal_submission_editor(
        *,
        source: Win5NormalSubmissionEditorSource,
        persona_id: str,
    ) -> Win5NormalSubmissionEditor:
        if (
            source.season_status != Win5SeasonStatus.ACTIVE
            or source.round_type != Win5RoundType.NORMAL
            or source.round_status != Win5RoundStatus.OPEN
        ):
            raise Win5NormalSubmissionRoundUnavailableError(
                "Normal Submission editor requires an open Round in an active Season."
            )
        if len(source.races) != 1:
            raise Win5NormalSubmissionInvalidSourceError("A Normal Submission editor requires exactly one Race.")

        race = source.races[0]
        entries = tuple(sorted(race.entries, key=lambda entry: (entry.gate_number, entry.id)))
        if (
            len(entries) < 5
            or len({entry.id for entry in entries}) != len(entries)
            or len({entry.gate_number for entry in entries}) != len(entries)
        ):
            raise Win5NormalSubmissionInvalidSourceError(
                "A Normal Submission editor requires at least five unique canonical RaceEntries."
            )

        current: Win5AcceptedNormalSubmission | None = None
        stored = source.submission
        if stored is not None:
            if (
                stored.round_id != source.round_id
                or stored.persona_id != persona_id
                or stored.status != Win5SubmissionStatus.ACCEPTED
                or stored.active_marker is not True
                or isinstance(stored.version, bool)
                or not isinstance(stored.version, int)
                or stored.version <= 0
                or any(pick.submission_id != stored.id or pick.race_id != race.id for pick in stored.picks)
            ):
                raise Win5NormalSubmissionInvalidSourceError(
                    "Stored accepted Submission identity or version is inconsistent."
                )
            try:
                validate_submission_for_round(
                    Win5Round(
                        id=source.round_id,
                        season_id=source.season_id,
                        name=source.round_name,
                        type=source.round_type,
                        status=source.round_status,
                        races=(
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
                                    for entry in entries
                                ),
                            ),
                        ),
                    ),
                    Win5Submission(
                        id=stored.id,
                        round_id=stored.round_id,
                        persona_id=stored.persona_id,
                        tier=stored.tier,
                        status=stored.status,
                        picks=stored.picks,
                    ),
                )
            except (TypeError, ValueError, Win5DomainError) as exc:
                raise Win5NormalSubmissionInvalidSourceError(
                    "Stored accepted Submission cannot form a canonical Normal desired state."
                ) from exc
            current = Win5AcceptedNormalSubmission(
                id=stored.id,
                tier=stored.tier,
                version=stored.version,
                picks=tuple(
                    Win5NormalSubmissionPick(
                        position=pick.position,
                        race_entry_id=pick.race_entry_id,
                    )
                    for pick in sorted(stored.picks, key=lambda pick: pick.position)
                    if pick.race_entry_id is not None
                ),
            )

        return Win5NormalSubmissionEditor(
            season_id=source.season_id,
            season_name=source.season_name,
            round_id=source.round_id,
            round_name=source.round_name,
            race_id=race.id,
            race_name=race.name,
            entries=entries,
            submission=current,
        )

    @staticmethod
    def _build_special_submission_editor(
        *,
        source: Win5SpecialSubmissionEditorSource,
        persona_id: str,
    ) -> Win5SpecialSubmissionEditor:
        if (
            source.season_status != Win5SeasonStatus.ACTIVE
            or source.round_type != Win5RoundType.SPECIAL
            or source.round_status != Win5RoundStatus.OPEN
        ):
            raise Win5SpecialSubmissionRoundUnavailableError(
                "Special Submission editor requires an open Round in an active Season."
            )
        if not source.races or len({race.id for race in source.races}) != len(source.races):
            raise Win5SpecialSubmissionInvalidSourceError(
                "A Special Submission editor requires one or more unique Races."
            )

        races = tuple(source.races)
        for race in races:
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
                raise Win5SpecialSubmissionInvalidSourceError(
                    "Optional Special RaceEntry references must have unique positive gates."
                )

        current: Win5AcceptedSpecialSubmission | None = None
        stored = source.submission
        if stored is not None:
            race_ids = {race.id for race in races}
            if (
                stored.round_id != source.round_id
                or stored.persona_id != persona_id
                or stored.status != Win5SubmissionStatus.ACCEPTED
                or stored.active_marker is not True
                or isinstance(stored.version, bool)
                or not isinstance(stored.version, int)
                or stored.version <= 0
                or any(pick.submission_id != stored.id or pick.race_id not in race_ids for pick in stored.picks)
            ):
                raise Win5SpecialSubmissionInvalidSourceError(
                    "Stored accepted Submission identity or version is inconsistent."
                )
            try:
                validate_submission_for_round(
                    Win5Round(
                        id=source.round_id,
                        season_id=source.season_id,
                        name=source.round_name,
                        type=source.round_type,
                        status=source.round_status,
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
                            for race in races
                        ),
                    ),
                    Win5Submission(
                        id=stored.id,
                        round_id=stored.round_id,
                        persona_id=stored.persona_id,
                        tier=stored.tier,
                        status=stored.status,
                        picks=stored.picks,
                    ),
                )
            except (TypeError, ValueError, Win5DomainError) as exc:
                raise Win5SpecialSubmissionInvalidSourceError(
                    "Stored accepted Submission cannot form a canonical Special desired state."
                ) from exc
            current = Win5AcceptedSpecialSubmission(
                id=stored.id,
                version=stored.version,
                picks=tuple(
                    Win5SpecialSubmissionPick(
                        race_id=pick.race_id,
                        gate_number=pick.gate_number,
                    )
                    for pick in sorted(stored.picks, key=lambda pick: pick.race_id)
                    if pick.gate_number is not None
                ),
            )

        return Win5SpecialSubmissionEditor(
            season_id=source.season_id,
            season_name=source.season_name,
            round_id=source.round_id,
            round_name=source.round_name,
            races=races,
            submission=current,
        )
