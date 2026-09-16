"""Normal WIN5 scoring command boundary and persistence port."""

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
    WIN5_NORMAL_REWARD_POLICY_VERSION,
    WIN5_NORMAL_SCORING_POLICY_VERSION,
    Win5DomainError,
    Win5NormalResultPlacement,
    Win5NormalScoringPick,
    Win5NormalSubmissionScore,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionTier,
    fingerprint_normal_result,
    score_normal_submission,
    validate_normal_score_totals,
)
from uma_st2.shared import normalize_utc_datetime

WIN5_NORMAL_SCORING_AUDIT_SCHEMA_VERSION: Final = 1
WIN5_NORMAL_SCORING_OPERATION_TYPE: Final = "round_scored"


class Win5NormalScoringError(ValueError):
    """Base error for rejected Normal scoring commands."""


class Win5NormalScoringUnavailableError(Win5NormalScoringError):
    """The requested Round cannot be scored under the Normal contract."""


class Win5NormalScoringAlreadyCompletedError(Win5NormalScoringError):
    """The Round was scored by a different logical operation."""


class Win5NormalScoringIdempotencyConflictError(Win5NormalScoringError):
    """An idempotency key was reused for a different logical command."""


class Win5NormalScoringInvalidSourceError(Win5NormalScoringError):
    """Persisted result or Submission facts cannot be scored safely."""


class Win5NormalScoringWalletUnavailableError(Win5NormalScoringError):
    """At least one positive reward recipient has no Circle Point wallet."""


class Win5NormalScoringAuditError(Win5NormalScoringError):
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


@dataclass(frozen=True, slots=True)
class ScoreNormalWin5Round:
    """Staff command that scores one closed Normal Round exactly once."""

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

        canonical = f"win5-normal-round-score-v1:{self.round_id}"
        return sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Win5NormalScoringRoundTarget:
    """Locked Round and parent Season state."""

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
class Win5NormalScoringPickTarget:
    """Persisted pick with source Race context."""

    id: int
    race_id: int
    position: int
    race_entry_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.id, field_name="id")
        _require_positive_int(self.race_id, field_name="race_id")
        _require_positive_int(self.position, field_name="position")
        _require_positive_int(self.race_entry_id, field_name="race_entry_id")

    def to_domain(self) -> Win5NormalScoringPick:
        return Win5NormalScoringPick(
            id=self.id,
            position=self.position,
            race_entry_id=self.race_entry_id,
        )


@dataclass(frozen=True, slots=True)
class Win5NormalScoringSubmissionTarget:
    """One locked accepted Submission and its final version/picks."""

    id: int
    round_id: int
    persona_id: str
    tier: Win5SubmissionTier
    version: int
    active_marker: bool | None
    picks: tuple[Win5NormalScoringPickTarget, ...] = field(default_factory=tuple)

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
class Win5NormalScoredEventSummary:
    """Closed-session result facts for one accepted Submission."""

    submission_id: int
    submission_version: int
    persona_id: str
    tier: Win5SubmissionTier
    exact_count: int
    wrong_position_count: int
    off_board_count: int
    missing_count: int
    season_score_delta: int
    top1_score_delta: int
    circle_point_reward: int

    def __post_init__(self) -> None:
        _require_positive_int(self.submission_id, field_name="submission_id")
        _require_positive_int(self.submission_version, field_name="submission_version")
        _require_bounded_string(self.persona_id, field_name="persona_id", max_length=36)
        object.__setattr__(self, "tier", Win5SubmissionTier(self.tier))
        for field_name in (
            "exact_count",
            "wrong_position_count",
            "off_board_count",
            "missing_count",
            "season_score_delta",
            "top1_score_delta",
            "circle_point_reward",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name=field_name)
        validate_normal_score_totals(
            tier=self.tier,
            exact_count=self.exact_count,
            wrong_position_count=self.wrong_position_count,
            off_board_count=self.off_board_count,
            missing_count=self.missing_count,
            season_score_delta=self.season_score_delta,
            top1_score_delta=self.top1_score_delta,
            circle_point_reward=self.circle_point_reward,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "submission_id": self.submission_id,
            "submission_version": self.submission_version,
            "persona_id": self.persona_id,
            "tier": self.tier.value,
            "exact_count": self.exact_count,
            "wrong_position_count": self.wrong_position_count,
            "off_board_count": self.off_board_count,
            "missing_count": self.missing_count,
            "season_score_delta": self.season_score_delta,
            "top1_score_delta": self.top1_score_delta,
            "circle_point_reward": self.circle_point_reward,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Win5NormalScoredEventSummary:
        return cls(
            submission_id=_payload_int(payload, "submission_id"),
            submission_version=_payload_int(payload, "submission_version"),
            persona_id=_payload_string(payload, "persona_id"),
            tier=Win5SubmissionTier(_payload_string(payload, "tier")),
            exact_count=_payload_int(payload, "exact_count"),
            wrong_position_count=_payload_int(payload, "wrong_position_count"),
            off_board_count=_payload_int(payload, "off_board_count"),
            missing_count=_payload_int(payload, "missing_count"),
            season_score_delta=_payload_int(payload, "season_score_delta"),
            top1_score_delta=_payload_int(payload, "top1_score_delta"),
            circle_point_reward=_payload_int(payload, "circle_point_reward"),
        )


@dataclass(frozen=True, slots=True)
class ScoredNormalWin5Round:
    """Stored result returned after a score mutation or exact retry."""

    season_id: int
    round_id: int
    race_id: int
    result_fingerprint: str
    events: tuple[Win5NormalScoredEventSummary, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_positive_int(self.season_id, field_name="season_id")
        _require_positive_int(self.round_id, field_name="round_id")
        _require_positive_int(self.race_id, field_name="race_id")
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
        return {
            "schema_version": WIN5_NORMAL_SCORING_AUDIT_SCHEMA_VERSION,
            "season_id": self.season_id,
            "round_id": self.round_id,
            "race_id": self.race_id,
            "round_status": Win5RoundStatus.SCORED.value,
            "result_fingerprint": self.result_fingerprint,
            "scoring_policy_version": WIN5_NORMAL_SCORING_POLICY_VERSION,
            "reward_policy_version": WIN5_NORMAL_REWARD_POLICY_VERSION,
            "event_count": len(self.events),
            "season_score_delta": self.season_score_delta,
            "top1_score_delta": self.top1_score_delta,
            "circle_point_reward": self.circle_point_reward,
            "events": [event.to_payload() for event in self.events],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ScoredNormalWin5Round:
        if _payload_int(payload, "schema_version") != WIN5_NORMAL_SCORING_AUDIT_SCHEMA_VERSION:
            raise ValueError("Unsupported Normal scoring audit schema version.")
        if _payload_string(payload, "round_status") != Win5RoundStatus.SCORED.value:
            raise ValueError("Stored Normal scoring audit is not scored.")
        if _payload_string(payload, "scoring_policy_version") != WIN5_NORMAL_SCORING_POLICY_VERSION:
            raise ValueError("Stored Normal scoring policy version is unsupported.")
        if _payload_string(payload, "reward_policy_version") != WIN5_NORMAL_REWARD_POLICY_VERSION:
            raise ValueError("Stored Normal reward policy version is unsupported.")
        raw_events = payload["events"]
        if not isinstance(raw_events, list) or not all(isinstance(event, Mapping) for event in raw_events):
            raise ValueError("Stored Normal scoring events must be a JSON list of objects.")
        result = cls(
            season_id=_payload_int(payload, "season_id"),
            round_id=_payload_int(payload, "round_id"),
            race_id=_payload_int(payload, "race_id"),
            result_fingerprint=_payload_string(payload, "result_fingerprint"),
            events=tuple(Win5NormalScoredEventSummary.from_payload(event) for event in raw_events),
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
            raise ValueError("Stored Normal scoring aggregate fields do not match its events.")
        return result


@dataclass(frozen=True, slots=True)
class StoredWin5ScoringOperation:
    """Minimal persisted operation state used to resolve idempotent retries."""

    request_fingerprint: str | None
    type: str | None
    round_id: int | None
    after_data: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class Win5NormalScoringEventMutation:
    """Full append-only event payload passed to database infrastructure."""

    submission: Win5NormalScoringSubmissionTarget
    score: Win5NormalSubmissionScore

    @property
    def summary(self) -> Win5NormalScoredEventSummary:
        return Win5NormalScoredEventSummary(
            submission_id=self.submission.id,
            submission_version=self.submission.version,
            persona_id=self.submission.persona_id,
            tier=self.submission.tier,
            exact_count=self.score.exact_count,
            wrong_position_count=self.score.wrong_position_count,
            off_board_count=self.score.off_board_count,
            missing_count=self.score.missing_count,
            season_score_delta=self.score.season_score_delta,
            top1_score_delta=self.score.top1_score_delta,
            circle_point_reward=self.score.circle_point_reward,
        )


@dataclass(frozen=True, slots=True)
class Win5NormalScoringMutation:
    """One atomic Round scoring mutation prepared by Application."""

    command: ScoreNormalWin5Round
    round: Win5NormalScoringRoundTarget
    race_id: int
    result_fingerprint: str
    events: tuple[Win5NormalScoringEventMutation, ...]
    result: ScoredNormalWin5Round
    publication_intents: tuple[PublicationIntent, ...]
    created_at: datetime

    @property
    def before_data(self) -> dict[str, object]:
        return {
            "schema_version": WIN5_NORMAL_SCORING_AUDIT_SCHEMA_VERSION,
            "season_id": self.round.season_id,
            "round_id": self.round.id,
            "race_id": self.race_id,
            "round_status": Win5RoundStatus.CLOSED.value,
            "result_fingerprint": self.result_fingerprint,
            "scoring_policy_version": WIN5_NORMAL_SCORING_POLICY_VERSION,
            "reward_policy_version": WIN5_NORMAL_REWARD_POLICY_VERSION,
            "accepted_submission_count": len(self.events),
        }

    @property
    def after_data(self) -> dict[str, object]:
        return self.result.to_payload()


class Win5NormalScoringRepository(Protocol):
    """Persistence operations required by the Normal scoring command."""

    def lock_round(self, *, round_id: int) -> Win5NormalScoringRoundTarget | None: ...

    def find_operation(self, *, idempotency_key: str) -> StoredWin5ScoringOperation | None: ...

    def lock_round_race_ids(self, *, round_id: int) -> tuple[int, ...]: ...

    def lock_result_board(self, *, race_id: int) -> tuple[Win5NormalResultPlacement, ...]: ...

    def lock_accepted_submissions(
        self,
        *,
        round_id: int,
    ) -> tuple[Win5NormalScoringSubmissionTarget, ...]: ...

    def lock_circle_point_wallets(self, *, persona_ids: tuple[str, ...]) -> tuple[str, ...]: ...

    def load_scored_round_publication_source(
        self,
        *,
        guild_id: str,
        round_id: int,
        persona_ids: tuple[str, ...],
    ) -> Win5RoundPublicationSource: ...

    def apply_scoring(self, *, mutation: Win5NormalScoringMutation) -> None: ...


class Win5NormalScoringUnitOfWork(UnitOfWork, Protocol):
    """Feature UoW exposing only the Normal scoring repository."""

    @property
    def win5_normal_scoring(self) -> Win5NormalScoringRepository: ...


@dataclass(frozen=True, slots=True)
class Win5NormalScoringCommands:
    """Application entry point for atomic Normal Round scoring."""

    command_runner: CommandRunner[Win5NormalScoringUnitOfWork]
    clock: Callable[[], datetime]

    def score_round(self, command: ScoreNormalWin5Round) -> ScoredNormalWin5Round:
        return self.command_runner.run(
            lambda unit_of_work: self._score_round(unit_of_work.win5_normal_scoring, command)
        )

    def _score_round(
        self,
        repository: Win5NormalScoringRepository,
        command: ScoreNormalWin5Round,
    ) -> ScoredNormalWin5Round:
        round_ = repository.lock_round(round_id=command.round_id)
        if round_ is None:
            raise Win5NormalScoringUnavailableError("WIN5 Round does not exist.")
        if round_.source_kind != Win5RoundSourceKind.NATIVE_V2:
            raise Win5NormalScoringUnavailableError("Imported WIN5 Rounds are read-only.")

        stored = repository.find_operation(idempotency_key=command.idempotency_key)
        if stored is not None:
            return self._resolve_exact_retry(stored=stored, command=command)

        if round_.status == Win5RoundStatus.SCORED:
            raise Win5NormalScoringAlreadyCompletedError(
                "WIN5 Round was already scored by a different logical operation."
            )
        if (
            round_.type != Win5RoundType.NORMAL
            or round_.season_status != Win5SeasonStatus.ACTIVE
            or round_.status != Win5RoundStatus.CLOSED
        ):
            raise Win5NormalScoringUnavailableError(
                "Normal scoring requires a closed Normal Round in an active Season."
            )

        race_ids = repository.lock_round_race_ids(round_id=round_.id)
        if len(race_ids) != 1:
            raise Win5NormalScoringInvalidSourceError("Normal scoring requires exactly one Race.")
        race_id = race_ids[0]
        try:
            results = repository.lock_result_board(race_id=race_id)
            result_fingerprint = fingerprint_normal_result(results)
        except ValueError as exc:
            raise Win5NormalScoringInvalidSourceError(str(exc)) from exc

        try:
            submissions = repository.lock_accepted_submissions(round_id=round_.id)
        except ValueError as exc:
            raise Win5NormalScoringInvalidSourceError(str(exc)) from exc
        if len({submission.id for submission in submissions}) != len(submissions) or len(
            {submission.persona_id for submission in submissions}
        ) != len(submissions):
            raise Win5NormalScoringInvalidSourceError(
                "Accepted scoring Submissions must be unique by Submission and Persona."
            )

        event_mutations: list[Win5NormalScoringEventMutation] = []
        for submission in submissions:
            if (
                submission.round_id != round_.id
                or submission.active_marker is not True
                or any(pick.race_id != race_id for pick in submission.picks)
            ):
                raise Win5NormalScoringInvalidSourceError(
                    "Accepted Submission must be active and all picks must belong to the Normal Round's Race."
                )
            try:
                score = score_normal_submission(
                    tier=submission.tier,
                    picks=tuple(pick.to_domain() for pick in submission.picks),
                    results=results,
                )
            except Win5DomainError as exc:
                raise Win5NormalScoringInvalidSourceError(str(exc)) from exc
            event_mutations.append(Win5NormalScoringEventMutation(submission=submission, score=score))

        events = tuple(sorted(event_mutations, key=lambda event: (event.submission.persona_id, event.submission.id)))
        rewarded_persona_ids = tuple(
            event.submission.persona_id for event in events if event.score.circle_point_reward > 0
        )
        if rewarded_persona_ids:
            locked_wallets = set(repository.lock_circle_point_wallets(persona_ids=tuple(sorted(rewarded_persona_ids))))
            missing_wallets = tuple(
                persona_id for persona_id in sorted(rewarded_persona_ids) if persona_id not in locked_wallets
            )
            if missing_wallets:
                raise Win5NormalScoringWalletUnavailableError(
                    "Positive WIN5 reward recipient has no Circle Point wallet: " + ", ".join(missing_wallets)
                )

        result = ScoredNormalWin5Round(
            season_id=round_.season_id,
            round_id=round_.id,
            race_id=race_id,
            result_fingerprint=result_fingerprint,
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
                or publication_source.round_type != Win5RoundType.NORMAL
                or tuple(race.race_id for race in publication_source.races) != (race_id,)
            ):
                raise ValueError("Normal publication source does not match the locked scoring Round.")
            publication_placements = publication_source.races[0].placements
            if tuple(
                (placement.result_id, placement.position, placement.race_entry_id)
                for placement in publication_placements
            ) != tuple((placement.id, placement.position, placement.race_entry_id) for placement in results):
                raise ValueError("Normal publication Result does not match the locked scoring Result.")
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
            raise Win5NormalScoringInvalidSourceError(str(exc)) from exc
        mutation = Win5NormalScoringMutation(
            command=command,
            round=round_,
            race_id=race_id,
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
        stored: StoredWin5ScoringOperation,
        command: ScoreNormalWin5Round,
    ) -> ScoredNormalWin5Round:
        if (
            stored.request_fingerprint != command.request_fingerprint
            or stored.type != WIN5_NORMAL_SCORING_OPERATION_TYPE
            or stored.round_id != command.round_id
        ):
            raise Win5NormalScoringIdempotencyConflictError(
                "Idempotency key is already bound to a different logical operation."
            )
        if stored.after_data is None:
            raise Win5NormalScoringAuditError("Exact-retry operation has no stored result payload.")
        try:
            result = ScoredNormalWin5Round.from_payload(stored.after_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise Win5NormalScoringAuditError("Exact-retry operation has malformed stored result payload.") from exc
        if result.round_id != command.round_id:
            raise Win5NormalScoringAuditError("Exact-retry result does not belong to the requested Round.")
        return result
