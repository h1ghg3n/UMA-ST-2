"""Normal WIN5 scoring application-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType

import pytest

from uma_st2.application.execution import CommandRunner
from uma_st2.application.publication import (
    Win5PublicationDestination,
    Win5PublicationPersona,
    Win5PublicationRace,
    Win5PublicationResultPlacement,
    Win5RoundPublicationSource,
)
from uma_st2.application.win5 import (
    WIN5_NORMAL_SCORING_OPERATION_TYPE,
    ScoreNormalWin5Round,
    StoredWin5ScoringOperation,
    Win5NormalScoringAlreadyCompletedError,
    Win5NormalScoringAuditError,
    Win5NormalScoringCommands,
    Win5NormalScoringIdempotencyConflictError,
    Win5NormalScoringInvalidSourceError,
    Win5NormalScoringMutation,
    Win5NormalScoringPickTarget,
    Win5NormalScoringRoundTarget,
    Win5NormalScoringSubmissionTarget,
    Win5NormalScoringUnavailableError,
    Win5NormalScoringWalletUnavailableError,
)
from uma_st2.domain.win5 import (
    Win5NormalResultPlacement,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SubmissionTier,
)

NOW = datetime(2026, 8, 25, 6, 30, tzinfo=UTC)


def _round(
    *,
    status: Win5RoundStatus = Win5RoundStatus.CLOSED,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5NormalScoringRoundTarget:
    return Win5NormalScoringRoundTarget(
        id=11,
        season_id=7,
        type=Win5RoundType.NORMAL,
        status=status,
        season_status=Win5SeasonStatus.ACTIVE,
        source_kind=source_kind,
    )


def _results() -> tuple[Win5NormalResultPlacement, ...]:
    return tuple(
        Win5NormalResultPlacement(id=200 + position, position=position, race_entry_id=100 + position)
        for position in range(1, 6)
    )


def _submission(
    *,
    submission_id: int = 31,
    persona_id: str = "persona-1",
    tier: Win5SubmissionTier = Win5SubmissionTier.TOP5,
    picks: tuple[Win5NormalScoringPickTarget, ...] | None = None,
) -> Win5NormalScoringSubmissionTarget:
    return Win5NormalScoringSubmissionTarget(
        id=submission_id,
        round_id=11,
        persona_id=persona_id,
        tier=tier,
        version=4,
        active_marker=True,
        picks=(
            Win5NormalScoringPickTarget(id=301, race_id=17, position=1, race_entry_id=101),
            Win5NormalScoringPickTarget(id=302, race_id=17, position=2, race_entry_id=103),
            Win5NormalScoringPickTarget(id=303, race_id=17, position=3, race_entry_id=999),
        )
        if picks is None
        else picks,
    )


def _command(*, idempotency_key: str = "score-round-11-v1") -> ScoreNormalWin5Round:
    return ScoreNormalWin5Round(
        round_id=11,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="interaction-score-55",
        reason="operator confirmed complete result",
    )


def _publication_source(
    *,
    results: tuple[Win5NormalResultPlacement, ...],
    submissions: tuple[Win5NormalScoringSubmissionTarget, ...],
) -> Win5RoundPublicationSource:
    return Win5RoundPublicationSource(
        destination=Win5PublicationDestination(
            guild_id="987654321",
            announcements_enabled=True,
            target_channel_id="1122334455",
        ),
        season_id=7,
        season_name="2026 Season",
        round_id=11,
        round_name="Normal Round 1",
        round_type=Win5RoundType.NORMAL,
        races=(
            Win5PublicationRace(
                race_id=17,
                race_name="Race 1",
                placements=tuple(
                    Win5PublicationResultPlacement(
                        result_id=result.id,
                        position=result.position,
                        gate_number=result.position,
                        race_entry_id=result.race_entry_id,
                        horse_name=f"Horse {result.position}",
                    )
                    for result in results
                ),
            ),
        ),
        personas=tuple(
            Win5PublicationPersona(
                persona_id=submission.persona_id,
                display_name=f"Display {submission.persona_id}",
            )
            for submission in submissions
        ),
    )


class RecordingRepository:
    def __init__(
        self,
        *,
        round_: Win5NormalScoringRoundTarget | None = None,
        stored: StoredWin5ScoringOperation | None = None,
        race_ids: tuple[int, ...] = (17,),
        results: tuple[Win5NormalResultPlacement, ...] | None = None,
        submissions: tuple[Win5NormalScoringSubmissionTarget, ...] | None = None,
        wallets: tuple[str, ...] = ("persona-1",),
    ) -> None:
        self.round = _round() if round_ is None else round_
        self.stored = stored
        self.race_ids = race_ids
        self.results = _results() if results is None else results
        self.submissions = (_submission(),) if submissions is None else submissions
        self.wallets = wallets
        self.calls: list[tuple[str, object]] = []
        self.mutations: list[Win5NormalScoringMutation] = []

    def lock_round(self, *, round_id: int) -> Win5NormalScoringRoundTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def find_operation(self, *, idempotency_key: str) -> StoredWin5ScoringOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def lock_round_race_ids(self, *, round_id: int) -> tuple[int, ...]:
        self.calls.append(("lock_round_race_ids", round_id))
        return self.race_ids

    def lock_result_board(self, *, race_id: int) -> tuple[Win5NormalResultPlacement, ...]:
        self.calls.append(("lock_result_board", race_id))
        return self.results

    def lock_accepted_submissions(
        self,
        *,
        round_id: int,
    ) -> tuple[Win5NormalScoringSubmissionTarget, ...]:
        self.calls.append(("lock_accepted_submissions", round_id))
        return self.submissions

    def lock_circle_point_wallets(self, *, persona_ids: tuple[str, ...]) -> tuple[str, ...]:
        self.calls.append(("lock_circle_point_wallets", persona_ids))
        return self.wallets

    def load_scored_round_publication_source(
        self,
        *,
        guild_id: str,
        round_id: int,
        persona_ids: tuple[str, ...],
    ) -> Win5RoundPublicationSource:
        self.calls.append(
            (
                "load_scored_round_publication_source",
                (guild_id, round_id, persona_ids),
            )
        )
        return _publication_source(results=self.results, submissions=self.submissions)

    def apply_scoring(self, *, mutation: Win5NormalScoringMutation) -> None:
        self.calls.append(("apply_scoring", mutation))
        self.mutations.append(mutation)


@dataclass
class RecordingUnitOfWork:
    win5_normal_scoring: RecordingRepository
    commit_count: int = 0
    rollback_count: int = 0

    def __enter__(self) -> RecordingUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self.commit_count == 0 and self.rollback_count == 0:
            self.rollback_count += 1
        return False

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class RecordingFactory:
    def __init__(self, repository: RecordingRepository) -> None:
        self.repository = repository
        self.created: list[RecordingUnitOfWork] = []

    def __call__(self) -> RecordingUnitOfWork:
        unit_of_work = RecordingUnitOfWork(self.repository)
        self.created.append(unit_of_work)
        return unit_of_work


def _commands(repository: RecordingRepository) -> tuple[Win5NormalScoringCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5NormalScoringCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_score_round_builds_one_atomic_event_projection_reward_mutation() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.score_round(_command())

    assert result.season_id == 7
    assert result.round_id == 11
    assert result.race_id == 17
    assert len(result.events) == 1
    event = result.events[0]
    assert event.submission_id == 31
    assert event.submission_version == 4
    assert (
        event.exact_count,
        event.wrong_position_count,
        event.off_board_count,
        event.missing_count,
    ) == (1, 1, 1, 2)
    assert event.season_score_delta == 4
    assert event.top1_score_delta == 0
    assert event.circle_point_reward == 10
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_round_race_ids",
        "lock_result_board",
        "lock_accepted_submissions",
        "lock_circle_point_wallets",
        "load_scored_round_publication_source",
        "apply_scoring",
    ]
    mutation = repository.mutations[0]
    assert mutation.created_at == NOW
    assert mutation.command.request_fingerprint == _command().request_fingerprint
    assert mutation.before_data["round_status"] == "closed"
    assert mutation.before_data["accepted_submission_count"] == 1
    assert mutation.after_data == result.to_payload()
    assert len(mutation.publication_intents) == 1
    publication = mutation.publication_intents[0]
    assert publication.event_type == "win5_round_scored"
    assert publication.status.value == "ready"
    assert publication.payload_json["no_hits"] is False
    assert factory.created[0].commit_count == 1


def test_complete_round_without_accepted_submissions_scores_without_wallet_or_events() -> None:
    repository = RecordingRepository(submissions=(), wallets=())
    commands, _ = _commands(repository)

    result = commands.score_round(_command())

    assert result.events == ()
    assert result.season_score_delta == 0
    assert result.circle_point_reward == 0
    assert "lock_circle_point_wallets" not in [name for name, _ in repository.calls]
    assert len(repository.mutations) == 1
    publication = repository.mutations[0].publication_intents[0]
    assert publication.payload_json["hits"] == []
    assert publication.payload_json["no_hits"] is True


def test_complete_top5_exact_result_creates_one_coalesced_hall_of_fame_intent() -> None:
    exact_picks = tuple(
        Win5NormalScoringPickTarget(
            id=300 + position,
            race_id=17,
            position=position,
            race_entry_id=100 + position,
        )
        for position in range(1, 6)
    )
    repository = RecordingRepository(submissions=(_submission(picks=exact_picks),))

    _commands(repository)[0].score_round(_command())

    publications = repository.mutations[0].publication_intents
    assert [publication.event_type for publication in publications] == [
        "win5_round_scored",
        "win5_hall_of_fame_updated",
    ]
    hall_payload = publications[1].payload_json
    assert hall_payload["records"] == [
        {
            "submission_id": 31,
            "persona_id": "persona-1",
            "display_name": "Display persona-1",
            "tier": "TOP5",
        }
    ]


def test_missing_positive_reward_wallet_fails_closed_without_mutation() -> None:
    repository = RecordingRepository(wallets=())
    commands, factory = _commands(repository)

    with pytest.raises(Win5NormalScoringWalletUnavailableError, match="persona-1"):
        commands.score_round(_command())

    assert repository.mutations == []
    assert factory.created[0].commit_count == 0
    assert factory.created[0].rollback_count == 1


def test_exact_retry_returns_stored_dto_without_loading_or_writing_sources() -> None:
    first_repository = RecordingRepository()
    first_commands, _ = _commands(first_repository)
    stored_result = first_commands.score_round(_command())
    command = _command()
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5ScoringOperation(
            request_fingerprint=command.request_fingerprint,
            type=WIN5_NORMAL_SCORING_OPERATION_TYPE,
            round_id=11,
            after_data=stored_result.to_payload(),
        ),
    )
    commands, factory = _commands(repository)

    retried = commands.score_round(command)

    assert retried == stored_result
    assert [name for name, _ in repository.calls] == ["lock_round", "find_operation"]
    assert repository.mutations == []
    assert factory.created[0].commit_count == 1


def test_reused_key_with_different_fingerprint_is_zero_write_conflict() -> None:
    command = _command()
    repository = RecordingRepository(
        stored=StoredWin5ScoringOperation(
            request_fingerprint="0" * 64,
            type=WIN5_NORMAL_SCORING_OPERATION_TYPE,
            round_id=11,
            after_data=None,
        )
    )
    commands, factory = _commands(repository)

    with pytest.raises(Win5NormalScoringIdempotencyConflictError):
        commands.score_round(command)

    assert repository.mutations == []
    assert factory.created[0].rollback_count == 1


def test_exact_retry_rejects_audit_whose_judgement_counts_drifted() -> None:
    first_repository = RecordingRepository()
    first_commands, _ = _commands(first_repository)
    stored_result = first_commands.score_round(_command())
    payload = stored_result.to_payload()
    raw_events = payload["events"]
    assert isinstance(raw_events, list)
    raw_event = raw_events[0]
    assert isinstance(raw_event, dict)
    raw_event["missing_count"] = 1
    command = _command()
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5ScoringOperation(
            request_fingerprint=command.request_fingerprint,
            type=WIN5_NORMAL_SCORING_OPERATION_TYPE,
            round_id=11,
            after_data=payload,
        ),
    )
    commands, _ = _commands(repository)

    with pytest.raises(Win5NormalScoringAuditError, match="malformed"):
        commands.score_round(command)

    assert repository.mutations == []


def test_already_scored_round_with_new_key_is_zero_write_rejection() -> None:
    repository = RecordingRepository(round_=_round(status=Win5RoundStatus.SCORED))
    commands, _ = _commands(repository)

    with pytest.raises(Win5NormalScoringAlreadyCompletedError):
        commands.score_round(_command(idempotency_key="another-score-key"))

    assert repository.mutations == []


def test_incomplete_result_and_cross_race_pick_fail_before_any_write() -> None:
    incomplete = RecordingRepository(results=_results()[:4])
    commands, _ = _commands(incomplete)
    with pytest.raises(Win5NormalScoringInvalidSourceError, match="position 1 through 5"):
        commands.score_round(_command())
    assert incomplete.mutations == []

    bad_submission = _submission(
        picks=(Win5NormalScoringPickTarget(id=301, race_id=99, position=1, race_entry_id=101),)
    )
    cross_race = RecordingRepository(submissions=(bad_submission,))
    commands, _ = _commands(cross_race)
    with pytest.raises(Win5NormalScoringInvalidSourceError, match="Round's Race"):
        commands.score_round(_command())
    assert cross_race.mutations == []


def test_imported_round_rejects_normal_scoring_before_reward_or_publication() -> None:
    repository = RecordingRepository(round_=_round(source_kind=Win5RoundSourceKind.IMPORTED_V1))
    commands, _ = _commands(repository)

    with pytest.raises(Win5NormalScoringUnavailableError, match="read-only"):
        commands.score_round(_command())

    assert [name for name, _ in repository.calls] == ["lock_round"]
    assert repository.mutations == []
