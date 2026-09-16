"""Special WIN5 scoring command boundary and persistence port."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol

from uma_st2.application.execution import CommandRunner, UnitOfWork
from uma_st2.application.publication import (
    PublicationIntent,
    Win5RoundPublicationSource,
    Win5ScoredSubmissionPublicationSource,
    build_win5_scored_publication_intents,
)
from uma_st2.domain.win5 import (
    WIN5_SPECIAL_REWARD_POLICY_VERSION,
    WIN5_SPECIAL_SCORING_POLICY_VERSION,
    WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
    Win5DomainError,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    Win5SpecialScoringPick,
    Win5SpecialSubmissionScore,
    Win5SubmissionTier,
    fingerprint_special_result,
    score_special_submission,
    validate_special_score_totals,
)
from uma_st2.shared import normalize_utc_datetime

WIN5_SPECIAL_SCORING_AUDIT_SCHEMA_VERSION: Final = 1
WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION: Final = 2
WIN5_SPECIAL_SCORING_OPERATION_TYPE: Final = "round_scored"


class Win5SpecialScoringError(ValueError):
    """Base error for rejected Special scoring commands."""


class Win5SpecialScoringUnavailableError(Win5SpecialScoringError):
    """The requested Round cannot be scored under the Special contract."""


class Win5SpecialScoringAlreadyCompletedError(Win5SpecialScoringError):
    """The Round was scored by a different logical operation."""


class Win5SpecialScoringIdempotencyConflictError(Win5SpecialScoringError):
    """An idempotency key was reused for a different logical command."""


class Win5SpecialScoringInvalidSourceError(Win5SpecialScoringError):
    """Persisted Result or Submission facts cannot be scored safely."""


class Win5SpecialScoringAuditError(Win5SpecialScoringError):
    """Stored exact-retry audit data is absent or malformed."""


def _require_positive_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")


def _require_bounded_string(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    optional: bool = False,
) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or len(value) > max_length:
        qualifier = "non-empty " if not optional else "non-empty optional "
        raise ValueError(f"{field_name} must be a {qualifier}string no longer than {max_length} characters.")


def _payload_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer.")
    return value


def _payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string.")
    return value


def _payload_race_ids(payload: Mapping[str, object]) -> tuple[int, ...]:
    raw = payload["race_ids"]
    if not isinstance(raw, list):
        raise ValueError("race_ids must be a JSON list.")
    race_ids = tuple(raw)
    for race_id in race_ids:
        _require_positive_int(race_id, field_name="race_id")
    if not race_ids or tuple(sorted(set(race_ids))) != race_ids:
        raise ValueError("race_ids must be a non-empty sorted unique list.")
    return race_ids


def _payload_optional_race_ids(payload: Mapping[str, object], key: str) -> tuple[int, ...]:
    if key not in payload:
        return ()
    raw = payload[key]
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a JSON list.")
    race_ids = tuple(raw)
    for race_id in race_ids:
        _require_positive_int(race_id, field_name="race_id")
    if tuple(sorted(set(race_ids))) != race_ids:
        raise ValueError(f"{key} must be a sorted unique list.")
    return race_ids


@dataclass(frozen=True, slots=True)
class ScoreSpecialWin5Round:
    """Staff command that scores one complete Special Round once."""

    round_id: int
    idempotency_key: str
    actor_discord_user_id: str
    guild_id: str
    correlation_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.idempotency_key, field_name="idempotency_key", max_length=128)
        _require_bounded_string(
            self.actor_discord_user_id,
            field_name="actor_discord_user_id",
            max_length=32,
        )
        _require_bounded_string(self.guild_id, field_name="guild_id", max_length=32)
        _require_bounded_string(
            self.correlation_id,
            field_name="correlation_id",
            max_length=128,
            optional=True,
        )
        _require_bounded_string(self.reason, field_name="reason", max_length=255, optional=True)

    @property
    def request_fingerprint(self) -> str:
        """Identify the bounded logical request independently of mutable results."""

        canonical = f"win5-special-round-score-v1:{self.round_id}"
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringRoundTarget:
    """Locked Special Round and parent Season state."""

    id: int
    season_id: int
    type: Win5RoundType
    source_kind: Win5RoundSourceKind
    status: Win5RoundStatus
    season_status: Win5SeasonStatus

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.season_id, field_name="season_id")
        object.__setattr__(self, "type", Win5RoundType(self.type))
        object.__setattr__(self, "status", Win5RoundStatus(self.status))
        object.__setattr__(self, "season_status", Win5SeasonStatus(self.season_status))
        object.__setattr__(self, "source_kind", Win5RoundSourceKind(self.source_kind))


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringPickTarget:
    """Persisted Special pick with its Race context."""

    id: int
    race_id: int
    gate_number: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.gate_number, field_name="gate_number")

    def to_domain(self) -> Win5SpecialScoringPick:
        return Win5SpecialScoringPick(
            id=self.id,
            race_id=self.race_id,
            gate_number=self.gate_number,
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringSubmissionTarget:
    """One locked accepted Special Submission and its final picks."""

    id: int
    round_id: int
    persona_id: str
    tier: Win5SubmissionTier
    version: int
    active_marker: bool | None
    picks: tuple[Win5SpecialScoringPickTarget, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_bounded_string(self.persona_id, field_name="persona_id", max_length=36)
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        _require_positive_int(self.version, field_name="version")
        if self.active_marker is not None and not isinstance(self.active_marker, bool):
            raise ValueError("active_marker must be a boolean or None.")
        object.__setattr__(self, "picks", tuple(self.picks))


@dataclass(frozen=True, slots=True)
class Win5SpecialScoredEventSummary:
    """Closed-session score facts for one accepted Special Submission."""

    submission_id: int
    submission_version: int
    persona_id: str
    race_count: int
    exact_count: int
    off_board_count: int
    missing_count: int
    season_score_delta: int
    top1_score_delta: int
    void_count: int = 0
    circle_point_reward: int = 0

    def __post_init__(self) -> None:
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_positive_int(self.submission_version, field_name="submission_version")
        _require_bounded_string(self.persona_id, field_name="persona_id", max_length=36)
        for field_name in (
            "race_count",
            "exact_count",
            "off_board_count",
            "missing_count",
            "void_count",
            "season_score_delta",
            "top1_score_delta",
            "circle_point_reward",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)
        validate_special_score_totals(
            race_count=self.race_count,
            exact_count=self.exact_count,
            off_board_count=self.off_board_count,
            missing_count=self.missing_count,
            void_count=self.void_count,
            season_score_delta=self.season_score_delta,
            top1_score_delta=self.top1_score_delta,
            circle_point_reward=self.circle_point_reward,
        )

    def to_payload(self, *, include_void_count: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "submission_id": self.submission_id,
            "submission_version": self.submission_version,
            "persona_id": self.persona_id,
            "tier": Win5SubmissionTier.SPECIAL_WINNER.value,
            "race_count": self.race_count,
            "exact_count": self.exact_count,
            "wrong_position_count": 0,
            "off_board_count": self.off_board_count,
            "missing_count": self.missing_count,
            "season_score_delta": self.season_score_delta,
            "top1_score_delta": self.top1_score_delta,
            "circle_point_reward": self.circle_point_reward,
        }
        if include_void_count:
            payload["void_count"] = self.void_count
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        require_void_count: bool = False,
    ) -> Win5SpecialScoredEventSummary:
        if _payload_string(payload, "tier") != Win5SubmissionTier.SPECIAL_WINNER.value:
            raise ValueError("Stored Special scoring event has an invalid tier.")
        if _payload_int(payload, "wrong_position_count") != 0:
            raise ValueError("Stored Special scoring event cannot contain wrong_position.")
        if require_void_count and "void_count" not in payload:
            raise ValueError("Stored mixed-void Special scoring event has no void_count.")
        return cls(
            submission_id=_payload_int(payload, "submission_id"),
            submission_version=_payload_int(payload, "submission_version"),
            persona_id=_payload_string(payload, "persona_id"),
            race_count=_payload_int(payload, "race_count"),
            exact_count=_payload_int(payload, "exact_count"),
            off_board_count=_payload_int(payload, "off_board_count"),
            missing_count=_payload_int(payload, "missing_count"),
            season_score_delta=_payload_int(payload, "season_score_delta"),
            top1_score_delta=_payload_int(payload, "top1_score_delta"),
            void_count=_payload_int(payload, "void_count") if "void_count" in payload else 0,
            circle_point_reward=_payload_int(payload, "circle_point_reward"),
        )


@dataclass(frozen=True, slots=True)
class ScoredSpecialWin5Round:
    """Stored result returned after a Special score mutation or exact retry."""

    season_id: int
    round_id: int
    race_ids: tuple[int, ...]
    result_fingerprint: str
    scoring_policy_version: str = WIN5_SPECIAL_SCORING_POLICY_VERSION
    void_race_ids: tuple[int, ...] = field(default_factory=tuple)
    events: tuple[Win5SpecialScoredEventSummary, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        canonical_race_ids = tuple(sorted(set(self.race_ids)))
        if not canonical_race_ids or canonical_race_ids != tuple(self.race_ids):
            raise ValueError("race_ids must be non-empty, sorted, and unique.")
        for race_id in canonical_race_ids:
            _require_positive_int(race_id, field_name="race_id")
        canonical_void_race_ids = tuple(sorted(set(self.void_race_ids)))
        if canonical_void_race_ids != tuple(self.void_race_ids) or any(
            race_id not in set(canonical_race_ids) for race_id in canonical_void_race_ids
        ):
            raise ValueError("void_race_ids must be a sorted unique subset of race_ids.")
        if self.scoring_policy_version == WIN5_SPECIAL_SCORING_POLICY_VERSION:
            if canonical_void_race_ids:
                raise ValueError("Special scoring v1 cannot contain void Races.")
        elif self.scoring_policy_version == WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION:
            if not canonical_void_race_ids or len(canonical_void_race_ids) == len(canonical_race_ids):
                raise ValueError("Special scoring v2 requires a proper non-empty void Race subset.")
        else:
            raise ValueError("scoring_policy_version is unsupported.")
        object.__setattr__(self, "void_race_ids", canonical_void_race_ids)
        if (
            not isinstance(self.result_fingerprint, str)
            or len(self.result_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.result_fingerprint)
        ):
            raise ValueError("result_fingerprint must be a lowercase SHA-256 hex digest.")
        object.__setattr__(
            self,
            "events",
            tuple(sorted(self.events, key=lambda event: (event.persona_id, event.submission_id))),
        )
        if any(
            event.race_count != len(canonical_race_ids) or event.void_count != len(canonical_void_race_ids)
            for event in self.events
        ):
            raise ValueError("Special scoring event race count does not match its Round.")

    @property
    def audit_schema_version(self) -> int:
        if self.scoring_policy_version == WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION:
            return WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION
        return WIN5_SPECIAL_SCORING_AUDIT_SCHEMA_VERSION

    @property
    def season_score_delta(self) -> int:
        return sum(event.season_score_delta for event in self.events)

    @property
    def top1_score_delta(self) -> int:
        return sum(event.top1_score_delta for event in self.events)

    @property
    def circle_point_reward(self) -> int:
        return sum(event.circle_point_reward for event in self.events)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.audit_schema_version,
            "season_id": self.season_id,
            "round_id": self.round_id,
            "race_ids": list(self.race_ids),
            "round_status": Win5RoundStatus.SCORED.value,
            "result_fingerprint": self.result_fingerprint,
            "scoring_policy_version": self.scoring_policy_version,
            "reward_policy_version": WIN5_SPECIAL_REWARD_POLICY_VERSION,
            "event_count": len(self.events),
            "season_score_delta": self.season_score_delta,
            "top1_score_delta": self.top1_score_delta,
            "circle_point_reward": self.circle_point_reward,
            "events": [
                event.to_payload(
                    include_void_count=self.audit_schema_version == WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION
                )
                for event in self.events
            ],
        }
        if self.audit_schema_version == WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION:
            payload["void_race_ids"] = list(self.void_race_ids)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ScoredSpecialWin5Round:
        schema_version = _payload_int(payload, "schema_version")
        if schema_version not in {
            WIN5_SPECIAL_SCORING_AUDIT_SCHEMA_VERSION,
            WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION,
        }:
            raise ValueError("Unsupported Special scoring audit schema version.")
        if _payload_string(payload, "round_status") != Win5RoundStatus.SCORED.value:
            raise ValueError("Stored Special scoring audit is not scored.")
        scoring_policy_version = _payload_string(payload, "scoring_policy_version")
        expected_policy_version = (
            WIN5_SPECIAL_SCORING_POLICY_VERSION
            if schema_version == WIN5_SPECIAL_SCORING_AUDIT_SCHEMA_VERSION
            else WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION
        )
        if scoring_policy_version != expected_policy_version:
            raise ValueError("Stored Special scoring policy version does not match its audit schema.")
        if _payload_string(payload, "reward_policy_version") != WIN5_SPECIAL_REWARD_POLICY_VERSION:
            raise ValueError("Stored Special reward policy version is unsupported.")
        raw_events = payload["events"]
        if not isinstance(raw_events, list) or not all(isinstance(event, Mapping) for event in raw_events):
            raise ValueError("Stored Special scoring events must be a JSON list of objects.")
        result = cls(
            season_id=_payload_int(payload, "season_id"),
            round_id=_payload_int(payload, "round_id"),
            race_ids=_payload_race_ids(payload),
            result_fingerprint=_payload_string(payload, "result_fingerprint"),
            scoring_policy_version=scoring_policy_version,
            void_race_ids=_payload_optional_race_ids(payload, "void_race_ids"),
            events=tuple(
                Win5SpecialScoredEventSummary.from_payload(
                    event,
                    require_void_count=schema_version == WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION,
                )
                for event in raw_events
            ),
        )
        expected_aggregates = (
            len(result.events),
            result.season_score_delta,
            result.top1_score_delta,
            result.circle_point_reward,
        )
        stored_aggregates = tuple(
            _payload_int(payload, key)
            for key in (
                "event_count",
                "season_score_delta",
                "top1_score_delta",
                "circle_point_reward",
            )
        )
        if stored_aggregates != expected_aggregates:
            raise ValueError("Stored Special scoring aggregate fields do not match its events.")
        return result


@dataclass(frozen=True, slots=True)
class StoredWin5SpecialScoringOperation:
    """Minimal persisted operation state used for idempotent retries."""

    request_fingerprint: str | None
    type: str | None
    round_id: int | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringEventMutation:
    """Full append-only Special event passed to database infrastructure."""

    submission: Win5SpecialScoringSubmissionTarget
    score: Win5SpecialSubmissionScore

    @property
    def summary(self) -> Win5SpecialScoredEventSummary:
        return Win5SpecialScoredEventSummary(
            submission_id=self.submission.id,
            submission_version=self.submission.version,
            persona_id=self.submission.persona_id,
            race_count=len(self.score.race_ids),
            exact_count=self.score.exact_count,
            off_board_count=self.score.off_board_count,
            missing_count=self.score.missing_count,
            season_score_delta=self.score.season_score_delta,
            top1_score_delta=self.score.top1_score_delta,
            void_count=self.score.void_count,
            circle_point_reward=self.score.circle_point_reward,
        )


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringMutation:
    """One atomic Special Round scoring mutation prepared by Application."""

    command: ScoreSpecialWin5Round
    round: Win5SpecialScoringRoundTarget
    race_ids: tuple[int, ...]
    result_fingerprint: str
    events: tuple[Win5SpecialScoringEventMutation, ...]
    result: ScoredSpecialWin5Round
    publication_intents: tuple[PublicationIntent, ...]
    created_at: datetime

    @property
    def before_data(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.result.audit_schema_version,
            "season_id": self.round.season_id,
            "round_id": self.round.id,
            "race_ids": list(self.race_ids),
            "round_status": Win5RoundStatus.CLOSED.value,
            "result_fingerprint": self.result_fingerprint,
            "scoring_policy_version": self.result.scoring_policy_version,
            "reward_policy_version": WIN5_SPECIAL_REWARD_POLICY_VERSION,
            "accepted_submission_count": len(self.events),
        }
        if self.result.audit_schema_version == WIN5_SPECIAL_VOID_SCORING_AUDIT_SCHEMA_VERSION:
            payload["void_race_ids"] = list(self.result.void_race_ids)
        return payload

    @property
    def after_data(self) -> dict[str, object]:
        return self.result.to_payload()


class Win5SpecialScoringRepository(Protocol):
    """Persistence operations required by the Special scoring command."""

    def lock_round(self, *, round_id: int) -> Win5SpecialScoringRoundTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialScoringOperation | None: ...

    def lock_round_race_ids(self, *, round_id: int) -> tuple[int, ...]: ...

    def lock_void_race_ids(self, *, round_id: int) -> tuple[int, ...]: ...

    def lock_result_winners(self, *, race_ids: tuple[int, ...]) -> tuple[Win5SpecialResultWinner, ...]: ...

    def lock_accepted_submissions(
        self,
        *,
        round_id: int,
    ) -> tuple[Win5SpecialScoringSubmissionTarget, ...]: ...

    def load_scored_round_publication_source(
        self,
        *,
        guild_id: str,
        round_id: int,
        persona_ids: tuple[str, ...],
    ) -> Win5RoundPublicationSource: ...

    def apply_scoring(self, *, mutation: Win5SpecialScoringMutation) -> None: ...


class Win5SpecialScoringUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Special scoring repository."""

    @property
    def win5_special_scoring(self) -> Win5SpecialScoringRepository: ...


@dataclass(frozen=True, slots=True)
class Win5SpecialScoringCommands:
    """Application entry point for atomic Special Round scoring."""

    command_runner: CommandRunner[Win5SpecialScoringUnitOfWork]
    clock: Callable[[], datetime]

    def score_round(self, command: ScoreSpecialWin5Round) -> ScoredSpecialWin5Round:
        return self.command_runner.run(
            lambda unit_of_work: self._score_round(unit_of_work.win5_special_scoring, command)
        )

    def _score_round(
        self,
        repository: Win5SpecialScoringRepository,
        command: ScoreSpecialWin5Round,
    ) -> ScoredSpecialWin5Round:
        round_ = repository.lock_round(round_id=command.round_id)
        if round_ is None:
            raise Win5SpecialScoringUnavailableError("WIN5 Round does not exist.")
        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5SpecialScoringUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if round_.status == Win5RoundStatus.SCORED:
            raise Win5SpecialScoringAlreadyCompletedError(
                "WIN5 Round was already scored by a different logical operation."
            )
        if (
            round_.type != Win5RoundType.SPECIAL
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.CLOSED
        ):
            raise Win5SpecialScoringUnavailableError(
                "Special scoring requires a closed Special Round in an active Season."
            )

        try:
            race_ids = tuple(repository.lock_round_race_ids(round_id=round_.id))
            if not race_ids or tuple(sorted(set(race_ids))) != race_ids:
                raise ValueError("Special scoring Race IDs must be non-empty, sorted, and unique.")
            void_race_ids = tuple(repository.lock_void_race_ids(round_id=round_.id))
            if tuple(sorted(set(void_race_ids))) != void_race_ids or any(
                race_id not in set(race_ids) for race_id in void_race_ids
            ):
                raise ValueError("Special scoring void Race IDs must be a sorted unique Round subset.")
            if len(void_race_ids) == len(race_ids):
                raise ValueError("An all-void Special Round must be cancelled instead of scored.")
            non_void_race_ids = tuple(race_id for race_id in race_ids if race_id not in set(void_race_ids))
            winners = repository.lock_result_winners(race_ids=non_void_race_ids)
            result_fingerprint = fingerprint_special_result(winners)
            if tuple(winner.race_id for winner in winners) != non_void_race_ids:
                raise ValueError("Special scoring requires one complete Result for every non-void Race.")
            submissions = repository.lock_accepted_submissions(round_id=round_.id)
        except (Win5DomainError, TypeError, ValueError) as exc:
            raise Win5SpecialScoringInvalidSourceError(str(exc)) from exc

        if len({submission.id for submission in submissions}) != len(submissions) or len(
            {submission.persona_id for submission in submissions}
        ) != len(submissions):
            raise Win5SpecialScoringInvalidSourceError(
                "Accepted scoring Submissions must be unique by Submission and Persona."
            )

        event_mutations: list[Win5SpecialScoringEventMutation] = []
        for submission in submissions:
            if (
                submission.round_id != round_.id
                or submission.active_marker is not True
                or submission.tier != Win5SubmissionTier.SPECIAL_WINNER
                or any(pick.race_id not in set(race_ids) for pick in submission.picks)
            ):
                raise Win5SpecialScoringInvalidSourceError(
                    "Accepted Special Submission must be active and contain only target-Race gate picks."
                )
            try:
                score = score_special_submission(
                    race_ids=race_ids,
                    void_race_ids=void_race_ids,
                    picks=tuple(pick.to_domain() for pick in submission.picks),
                    results=winners,
                )
            except Win5DomainError as exc:
                raise Win5SpecialScoringInvalidSourceError(str(exc)) from exc
            event_mutations.append(Win5SpecialScoringEventMutation(submission=submission, score=score))

        events = tuple(sorted(event_mutations, key=lambda event: (event.submission.persona_id, event.submission.id)))
        result = ScoredSpecialWin5Round(
            season_id=round_.season_id,
            round_id=round_.id,
            race_ids=race_ids,
            result_fingerprint=result_fingerprint,
            scoring_policy_version=(
                WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION if void_race_ids else WIN5_SPECIAL_SCORING_POLICY_VERSION
            ),
            void_race_ids=void_race_ids,
            events=tuple(event.summary for event in events),
        )
        try:
            publication_source = repository.load_scored_round_publication_source(
                guild_id=command.guild_id,
                round_id=round_.id,
                persona_ids=tuple(event.submission.persona_id for event in events),
            )
            if (
                publication_source.season_id != round_.season_id
                or publication_source.round_id != round_.id
                or publication_source.round_type != Win5RoundType.SPECIAL
                or tuple(race.race_id for race in publication_source.races) != race_ids
            ):
                raise ValueError("Special publication source does not match the locked scoring Round.")
            publication_void_race_ids = tuple(race.race_id for race in publication_source.races if race.is_void)
            if publication_void_race_ids != void_race_ids:
                raise ValueError("Special publication void set does not match the locked scoring Round.")
            publication_winners = tuple(
                (race.placements[0].result_id, race.race_id, race.placements[0].gate_number)
                for race in publication_source.races
                if not race.is_void and len(race.placements) == 1 and race.placements[0].position == 1
            )
            if publication_winners != tuple((winner.id, winner.race_id, winner.gate_number) for winner in winners):
                raise ValueError("Special publication Result does not match the locked scoring Result.")
            publication_intents = build_win5_scored_publication_intents(
                source=publication_source,
                scored_submissions=tuple(
                    Win5ScoredSubmissionPublicationSource(
                        submission_id=event.submission.id,
                        persona_id=event.submission.persona_id,
                        tier=event.submission.tier,
                        season_score_delta=event.score.season_score_delta,
                        top1_score_delta=event.score.top1_score_delta,
                        outcomes=tuple(item.outcome for item in event.score.items),
                    )
                    for event in events
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SpecialScoringInvalidSourceError(str(exc)) from exc
        mutation = Win5SpecialScoringMutation(
            command=command,
            round=round_,
            race_ids=race_ids,
            result_fingerprint=result_fingerprint,
            events=events,
            result=result,
            publication_intents=publication_intents,
            created_at=normalize_utc_datetime(self.clock(), field_name="clock result"),
        )
        repository.apply_scoring(mutation=mutation)
        return result

    @staticmethod
    def _resolve_exact_retry(
        *,
        stored: StoredWin5SpecialScoringOperation,
        command: ScoreSpecialWin5Round,
    ) -> ScoredSpecialWin5Round:
        if (
            stored.request_fingerprint != command.request_fingerprint
            or stored.type != WIN5_SPECIAL_SCORING_OPERATION_TYPE
            or stored.round_id != command.round_id
        ):
            raise Win5SpecialScoringIdempotencyConflictError(
                "Idempotency key is already bound to a different logical operation."
            )
        if stored.after_data is None:
            raise Win5SpecialScoringAuditError("Exact-retry operation has no stored result payload.")
        try:
            result = ScoredSpecialWin5Round.from_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5SpecialScoringAuditError("Exact-retry operation has malformed stored result payload.") from exc
        if result.round_id != command.round_id:
            raise Win5SpecialScoringAuditError("Exact-retry result does not belong to the requested Round.")
        return result
