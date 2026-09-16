"""Special WIN5 scoring application-boundary tests."""

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
    WIN5_SPECIAL_SCORING_OPERATION_TYPE,
    ScoreSpecialWin5Round,
    StoredWin5SpecialScoringOperation,
    Win5SpecialScoringAlreadyCompletedError,
    Win5SpecialScoringAuditError,
    Win5SpecialScoringCommands,
    Win5SpecialScoringIdempotencyConflictError,
    Win5SpecialScoringInvalidSourceError,
    Win5SpecialScoringMutation,
    Win5SpecialScoringPickTarget,
    Win5SpecialScoringRoundTarget,
    Win5SpecialScoringSubmissionTarget,
    Win5SpecialScoringUnavailableError,
)
from uma_st2.domain.win5 import (
    WIN5_SPECIAL_SCORING_POLICY_VERSION,
    WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION,
    Win5RoundSourceKind,
    Win5RoundStatus,
    Win5RoundType,
    Win5SeasonStatus,
    Win5SpecialResultWinner,
    Win5SubmissionTier,
)

NOW = datetime(2026, 8, 26, 8, 30, tzinfo=UTC)


def _round(
    *,
    status: Win5RoundStatus = Win5RoundStatus.CLOSED,
    source_kind: Win5RoundSourceKind = Win5RoundSourceKind.NATIVE_V2,
) -> Win5SpecialScoringRoundTarget:
    return Win5SpecialScoringRoundTarget(
        id=11,
        season_id=7,
        type=Win5RoundType.SPECIAL,
        status=status,
        season_status=Win5SeasonStatus.ACTIVE,
        source_kind=source_kind,
    )


def _results() -> tuple[Win5SpecialResultWinner, ...]:
    return (
        Win5SpecialResultWinner(id=201, race_id=101, gate_number=3),
        Win5SpecialResultWinner(id=202, race_id=102, gate_number=7),
        Win5SpecialResultWinner(id=203, race_id=103, gate_number=1),
    )


def _submission(
    *,
    submission_id: int = 31,
    persona_id: str = "persona-1",
    tier: Win5SubmissionTier = Win5SubmissionTier.SPECIAL_WINNER,
    picks: tuple[Win5SpecialScoringPickTarget, ...] | None = None,
) -> Win5SpecialScoringSubmissionTarget:
    return Win5SpecialScoringSubmissionTarget(
        id=submission_id,
        round_id=11,
        persona_id=persona_id,
        tier=tier,
        version=4,
        active_marker=True,
        picks=(
            Win5SpecialScoringPickTarget(id=301, race_id=101, gate_number=3),
            Win5SpecialScoringPickTarget(id=302, race_id=102, gate_number=4),
        )
        if picks is None
        else picks,
    )


def _command(*, idempotency_key: str = "score-special-round-11-v1") -> ScoreSpecialWin5Round:
    return ScoreSpecialWin5Round(
        round_id=11,
        idempotency_key=idempotency_key,
        actor_discord_user_id="123456789",
        guild_id="987654321",
        correlation_id="interaction-special-score-55",
        reason="operator confirmed complete result",
    )


def _publication_source(
    *,
    race_ids: tuple[int, ...],
    void_race_ids: tuple[int, ...],
    results: tuple[Win5SpecialResultWinner, ...],
    submissions: tuple[Win5SpecialScoringSubmissionTarget, ...],
) -> Win5RoundPublicationSource:
    results_by_race_id = {result.race_id: result for result in results}
    void_id_set = set(void_race_ids)
    return Win5RoundPublicationSource(
        destination=Win5PublicationDestination(
            guild_id="987654321",
            announcements_enabled=True,
            target_channel_id="1122334455",
        ),
        season_id=7,
        season_name="2026 Season",
        round_id=11,
        round_name="Special Round 1",
        round_type=Win5RoundType.SPECIAL,
        races=tuple(
            Win5PublicationRace(
                race_id=race_id,
                race_name=f"Race {index}",
                placements=(
                    ()
                    if race_id in void_id_set
                    else (
                        Win5PublicationResultPlacement(
                            result_id=results_by_race_id[race_id].id,
                            position=1,
                            gate_number=results_by_race_id[race_id].gate_number,
                        ),
                    )
                ),
                void_reason="official no-contest" if race_id in void_id_set else None,
            )
            for index, race_id in enumerate(race_ids, start=1)
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
        round_: Win5SpecialScoringRoundTarget | None = None,
        stored: StoredWin5SpecialScoringOperation | None = None,
        race_ids: tuple[int, ...] = (101, 102, 103),
        void_race_ids: tuple[int, ...] = (),
        results: tuple[Win5SpecialResultWinner, ...] | None = None,
        submissions: tuple[Win5SpecialScoringSubmissionTarget, ...] | None = None,
    ) -> None:
        self.round = _round() if round_ is None else round_
        self.stored = stored
        self.race_ids = race_ids
        self.void_race_ids = void_race_ids
        self.results = _results() if results is None else results
        self.submissions = (_submission(),) if submissions is None else submissions
        self.calls: list[tuple[str, object]] = []
        self.mutations: list[Win5SpecialScoringMutation] = []

    def lock_round(self, *, round_id: int) -> Win5SpecialScoringRoundTarget | None:
        self.calls.append(("lock_round", round_id))
        return self.round

    def find_operation(self, *, idempotency_key: str) -> StoredWin5SpecialScoringOperation | None:
        self.calls.append(("find_operation", idempotency_key))
        return self.stored

    def lock_round_race_ids(self, *, round_id: int) -> tuple[int, ...]:
        self.calls.append(("lock_round_race_ids", round_id))
        return self.race_ids

    def lock_void_race_ids(self, *, round_id: int) -> tuple[int, ...]:
        self.calls.append(("lock_void_race_ids", round_id))
        return self.void_race_ids

    def lock_result_winners(self, *, race_ids: tuple[int, ...]) -> tuple[Win5SpecialResultWinner, ...]:
        self.calls.append(("lock_result_winners", race_ids))
        return self.results

    def lock_accepted_submissions(
        self,
        *,
        round_id: int,
    ) -> tuple[Win5SpecialScoringSubmissionTarget, ...]:
        self.calls.append(("lock_accepted_submissions", round_id))
        return self.submissions

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
        return _publication_source(
            race_ids=self.race_ids,
            void_race_ids=self.void_race_ids,
            results=self.results,
            submissions=self.submissions,
        )

    def apply_scoring(self, *, mutation: Win5SpecialScoringMutation) -> None:
        self.calls.append(("apply_scoring", mutation))
        self.mutations.append(mutation)


@dataclass
class RecordingUnitOfWork:
    win5_special_scoring: RecordingRepository
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


def _commands(repository: RecordingRepository) -> tuple[Win5SpecialScoringCommands, RecordingFactory]:
    factory = RecordingFactory(repository)
    return Win5SpecialScoringCommands(CommandRunner(factory), clock=lambda: NOW), factory


def test_score_round_builds_atomic_race_judgements_and_both_score_deltas() -> None:
    repository = RecordingRepository()
    commands, factory = _commands(repository)

    result = commands.score_round(_command())

    assert result.season_id == 7
    assert result.round_id == 11
    assert result.race_ids == (101, 102, 103)
    assert result.void_race_ids == ()
    assert result.scoring_policy_version == WIN5_SPECIAL_SCORING_POLICY_VERSION
    assert len(result.events) == 1
    event = result.events[0]
    assert event.submission_id == 31
    assert event.submission_version == 4
    assert (event.exact_count, event.off_board_count, event.missing_count) == (1, 1, 1)
    assert event.season_score_delta == 1
    assert event.top1_score_delta == 1
    assert event.circle_point_reward == 0
    assert [name for name, _ in repository.calls] == [
        "lock_round",
        "find_operation",
        "lock_round_race_ids",
        "lock_void_race_ids",
        "lock_result_winners",
        "lock_accepted_submissions",
        "load_scored_round_publication_source",
        "apply_scoring",
    ]
    mutation = repository.mutations[0]
    assert mutation.created_at == NOW
    assert mutation.before_data["race_ids"] == [101, 102, 103]
    assert mutation.before_data["accepted_submission_count"] == 1
    assert mutation.after_data == result.to_payload()
    assert len(mutation.publication_intents) == 1
    publication = mutation.publication_intents[0]
    assert publication.event_type == "win5_round_scored"
    assert publication.payload_json["no_hits"] is False
    assert publication.payload_json["results"][0]["race_name"] == "Race 1"
    assert factory.created[0].commit_count == 1


def test_mixed_void_round_writes_v2_judgements_audit_and_publication() -> None:
    repository = RecordingRepository(
        void_race_ids=(102,),
        results=(_results()[0], _results()[2]),
    )
    commands, factory = _commands(repository)

    result = commands.score_round(_command())

    assert result.void_race_ids == (102,)
    assert result.scoring_policy_version == WIN5_SPECIAL_VOID_SCORING_POLICY_VERSION
    event = result.events[0]
    assert (event.exact_count, event.off_board_count, event.missing_count, event.void_count) == (1, 0, 1, 1)
    mutation = repository.mutations[0]
    assert tuple(item.outcome.value for item in mutation.events[0].score.items) == (
        "exact",
        "void",
        "missing",
    )
    assert mutation.events[0].score.items[1].submission_pick_id == 302
    assert mutation.before_data["schema_version"] == 2
    assert mutation.before_data["void_race_ids"] == [102]
    assert mutation.after_data["events"][0]["void_count"] == 1
    publication = mutation.publication_intents[0]
    assert publication.payload_json["schema_version"] == 2
    assert publication.payload_json["results"][1] == {
        "race_id": 102,
        "race_name": "Race 2",
        "placements": [],
        "void_reason": "official no-contest",
    }
    assert factory.created[0].commit_count == 1

    retry_repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5SpecialScoringOperation(
            request_fingerprint=_command().request_fingerprint,
            type=WIN5_SPECIAL_SCORING_OPERATION_TYPE,
            round_id=11,
            after_data=result.to_payload(),
        ),
    )
    assert _commands(retry_repository)[0].score_round(_command()) == result
    assert [name for name, _ in retry_repository.calls] == ["lock_round", "find_operation"]


def test_complete_round_without_accepted_submissions_still_becomes_scored() -> None:
    repository = RecordingRepository(submissions=())
    commands, _ = _commands(repository)

    result = commands.score_round(_command())

    assert result.events == ()
    assert result.season_score_delta == 0
    assert result.top1_score_delta == 0
    assert len(repository.mutations) == 1
    publication = repository.mutations[0].publication_intents[0]
    assert publication.payload_json["hits"] == []
    assert publication.payload_json["no_hits"] is True
    assert all(
        intent.event_type != "win5_hall_of_fame_updated" for intent in repository.mutations[0].publication_intents
    )


def test_exact_retry_returns_stored_result_without_reloading_sources() -> None:
    first_repository = RecordingRepository()
    first_commands, _ = _commands(first_repository)
    stored_result = first_commands.score_round(_command())
    command = _command()
    repository = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5SpecialScoringOperation(
            request_fingerprint=command.request_fingerprint,
            type=WIN5_SPECIAL_SCORING_OPERATION_TYPE,
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


def test_reused_key_and_drifted_exact_retry_payload_fail_closed() -> None:
    command = _command()
    conflict = RecordingRepository(
        stored=StoredWin5SpecialScoringOperation(
            request_fingerprint="0" * 64,
            type=WIN5_SPECIAL_SCORING_OPERATION_TYPE,
            round_id=11,
            after_data=None,
        )
    )
    commands, factory = _commands(conflict)
    with pytest.raises(Win5SpecialScoringIdempotencyConflictError):
        commands.score_round(command)
    assert factory.created[0].rollback_count == 1

    first_repository = RecordingRepository()
    stored_result = _commands(first_repository)[0].score_round(command)
    payload = stored_result.to_payload()
    raw_events = payload["events"]
    assert isinstance(raw_events, list)
    raw_event = raw_events[0]
    assert isinstance(raw_event, dict)
    raw_event["missing_count"] = 0
    drifted = RecordingRepository(
        round_=_round(status=Win5RoundStatus.SCORED),
        stored=StoredWin5SpecialScoringOperation(
            request_fingerprint=command.request_fingerprint,
            type=WIN5_SPECIAL_SCORING_OPERATION_TYPE,
            round_id=11,
            after_data=payload,
        ),
    )
    with pytest.raises(Win5SpecialScoringAuditError, match="malformed"):
        _commands(drifted)[0].score_round(command)


def test_already_scored_round_with_new_key_is_zero_write_rejection() -> None:
    repository = RecordingRepository(round_=_round(status=Win5RoundStatus.SCORED))
    commands, _ = _commands(repository)

    with pytest.raises(Win5SpecialScoringAlreadyCompletedError):
        commands.score_round(_command(idempotency_key="another-special-score-key"))

    assert repository.mutations == []


def test_incomplete_results_foreign_pick_and_wrong_tier_fail_before_write() -> None:
    all_void = RecordingRepository(void_race_ids=(101, 102, 103), results=())
    with pytest.raises(Win5SpecialScoringInvalidSourceError, match="all-void"):
        _commands(all_void)[0].score_round(_command())
    assert all_void.mutations == []

    foreign_void = RecordingRepository(void_race_ids=(999,))
    with pytest.raises(Win5SpecialScoringInvalidSourceError, match="Round subset"):
        _commands(foreign_void)[0].score_round(_command())
    assert foreign_void.mutations == []

    incomplete = RecordingRepository(results=_results()[:2])
    with pytest.raises(Win5SpecialScoringInvalidSourceError, match="complete Result"):
        _commands(incomplete)[0].score_round(_command())
    assert incomplete.mutations == []

    foreign = RecordingRepository(
        submissions=(_submission(picks=(Win5SpecialScoringPickTarget(id=301, race_id=999, gate_number=3),)),)
    )
    with pytest.raises(Win5SpecialScoringInvalidSourceError, match="target-Race"):
        _commands(foreign)[0].score_round(_command())
    assert foreign.mutations == []

    wrong_tier = RecordingRepository(submissions=(_submission(tier=Win5SubmissionTier.TOP1),))
    with pytest.raises(Win5SpecialScoringInvalidSourceError, match="Special Submission"):
        _commands(wrong_tier)[0].score_round(_command())
    assert wrong_tier.mutations == []


def test_imported_round_rejects_special_scoring_before_publication() -> None:
    repository = RecordingRepository(round_=_round(source_kind=Win5RoundSourceKind.IMPORTED_V1))
    commands, _ = _commands(repository)

    with pytest.raises(Win5SpecialScoringUnavailableError, match="read-only"):
        commands.score_round(_command())

    assert [name for name, _ in repository.calls] == ["lock_round"]
    assert repository.mutations == []
