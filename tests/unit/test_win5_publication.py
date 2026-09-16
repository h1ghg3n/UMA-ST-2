"""Provider-independent WIN5 publication projection tests."""

from __future__ import annotations

import pytest

from uma_st2.application.publication import (
    Win5PublicationDestination,
    Win5PublicationPersona,
    Win5PublicationRace,
    Win5PublicationResultPlacement,
    Win5RoundPublicationSource,
    Win5ScoredSubmissionPublicationSource,
    build_win5_scored_publication_intents,
)
from uma_st2.domain.publication import PublicationStatus
from uma_st2.domain.win5 import Win5JudgementOutcome, Win5RoundType, Win5SubmissionTier


@pytest.mark.parametrize(
    ("enabled", "channel_id", "expected_status", "expected_channel"),
    (
        (False, "123456789", PublicationStatus.SUPPRESSED, None),
        (True, None, PublicationStatus.AWAITING_CHANNEL, None),
        (True, "123456789", PublicationStatus.READY, "123456789"),
    ),
)
def test_destination_maps_settings_to_initial_delivery_state(
    enabled: bool,
    channel_id: str | None,
    expected_status: PublicationStatus,
    expected_channel: str | None,
) -> None:
    destination = Win5PublicationDestination(
        guild_id="987654321",
        announcements_enabled=enabled,
        target_channel_id=channel_id,
    )

    assert destination.initial_status == expected_status
    assert destination.snapshotted_channel_id == expected_channel


def _normal_source(*, personas: tuple[Win5PublicationPersona, ...]) -> Win5RoundPublicationSource:
    return Win5RoundPublicationSource(
        destination=Win5PublicationDestination(
            guild_id="987654321",
            announcements_enabled=True,
            target_channel_id="123456789",
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
                        result_id=200 + position,
                        position=position,
                        gate_number=position,
                        race_entry_id=100 + position,
                        horse_name=f"Horse {position}",
                    )
                    for position in range(1, 6)
                ),
            ),
        ),
        personas=personas,
    )


def _perfect_top5(*, submission_id: int, persona_id: str) -> Win5ScoredSubmissionPublicationSource:
    return Win5ScoredSubmissionPublicationSource(
        submission_id=submission_id,
        persona_id=persona_id,
        tier=Win5SubmissionTier.TOP5,
        season_score_delta=15,
        top1_score_delta=0,
        outcomes=(Win5JudgementOutcome.EXACT,) * 5,
    )


def test_normal_co_records_are_complete_coalesced_and_deterministic() -> None:
    personas = (
        Win5PublicationPersona(persona_id="persona-b", display_name="Bravo"),
        Win5PublicationPersona(persona_id="persona-a", display_name="Alpha"),
    )
    submissions = (
        _perfect_top5(submission_id=32, persona_id="persona-b"),
        _perfect_top5(submission_id=31, persona_id="persona-a"),
    )

    first = build_win5_scored_publication_intents(
        source=_normal_source(personas=personas),
        scored_submissions=submissions,
    )
    second = build_win5_scored_publication_intents(
        source=_normal_source(personas=tuple(reversed(personas))),
        scored_submissions=tuple(reversed(submissions)),
    )

    assert first == second
    assert [intent.event_type for intent in first] == [
        "win5_round_scored",
        "win5_hall_of_fame_updated",
    ]
    assert [record["persona_id"] for record in first[1].payload_json["records"]] == [
        "persona-a",
        "persona-b",
    ]
    assert len(first[0].payload_fingerprint) == 64
    assert len(first[1].payload_fingerprint) == 64


def test_missing_persona_snapshot_fails_before_any_intent_is_built() -> None:
    source = _normal_source(personas=(Win5PublicationPersona(persona_id="persona-a", display_name="Alpha"),))

    with pytest.raises(ValueError, match="exactly match"):
        build_win5_scored_publication_intents(
            source=source,
            scored_submissions=(
                _perfect_top5(submission_id=31, persona_id="persona-a"),
                _perfect_top5(submission_id=32, persona_id="persona-b"),
            ),
        )


def test_mixed_void_special_result_uses_payload_v2_without_synthetic_result() -> None:
    source = Win5RoundPublicationSource(
        destination=Win5PublicationDestination(
            guild_id="987654321",
            announcements_enabled=True,
            target_channel_id="123456789",
        ),
        season_id=7,
        season_name="2026 Season",
        round_id=21,
        round_name="Mixed Special",
        round_type=Win5RoundType.SPECIAL,
        races=(
            Win5PublicationRace(
                race_id=101,
                race_name="Race 1",
                placements=(
                    Win5PublicationResultPlacement(
                        result_id=201,
                        position=1,
                        gate_number=3,
                    ),
                ),
            ),
            Win5PublicationRace(
                race_id=102,
                race_name="Race 2",
                placements=(),
                void_reason="official no-contest",
            ),
        ),
    )

    (intent,) = build_win5_scored_publication_intents(source=source, scored_submissions=())

    assert intent.payload_json["schema_version"] == 2
    assert intent.event_key == "win5-round:21:scored-result:v1"
    assert intent.payload_json["results"][1] == {
        "race_id": 102,
        "race_name": "Race 2",
        "placements": [],
        "void_reason": "official no-contest",
    }
