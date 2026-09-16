"""Discord rendering tests for stored WIN5 publication payload v1/v2."""

from __future__ import annotations

from copy import deepcopy

import pytest

from uma_st2.adapters.discord import (
    Win5DiscordPublicationRenderError,
    render_win5_discord_publication,
)
from uma_st2.application.publication import (
    Win5PublicationDestination,
    Win5PublicationPersona,
    Win5PublicationRace,
    Win5PublicationResultPlacement,
    Win5RoundPublicationSource,
    Win5ScoredSubmissionPublicationSource,
    build_win5_scored_publication_intents,
)
from uma_st2.domain.win5 import (
    Win5JudgementOutcome,
    Win5RoundType,
    Win5SubmissionTier,
)


def _race(
    race_id: int,
    *,
    name: str | None = None,
    placement_count: int = 1,
) -> dict[str, object]:
    return {
        "race_id": race_id,
        "race_name": name or f"Race {race_id}",
        "placements": [
            {
                "result_id": race_id * 100 + position,
                "position": position,
                "gate_number": race_id + position,
                "horse_name": f"Horse {race_id}-{position} @everyone",
            }
            for position in range(1, placement_count + 1)
        ],
    }


def _void_race(race_id: int, *, reason: str = "official no-contest") -> dict[str, object]:
    return {
        "race_id": race_id,
        "race_name": f"Race {race_id}",
        "placements": [],
        "void_reason": reason,
    }


def _result_payload(
    *,
    round_type: str = "normal",
    results: list[dict[str, object]] | None = None,
    hits: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    canonical_hits = hits or []
    return {
        "schema_version": 1,
        "publication_type": "win5_round_result",
        "season": {"id": 7, "name": "2026 @everyone Season"},
        "round": {"id": 11, "name": "Round **Final**", "type": round_type},
        "results": results or [_race(17, placement_count=5)],
        "hits": canonical_hits,
        "no_hits": not canonical_hits,
    }


def _hit(submission_id: int, *, display_name: str | None = None) -> dict[str, object]:
    return {
        "submission_id": submission_id,
        "persona_id": f"persona-{submission_id}",
        "display_name": display_name or f"Player {submission_id}",
        "tier": "TOP5",
        "season_score_delta": 15,
        "top1_score_delta": 0,
    }


def _hall_payload(*, record_count: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "publication_type": "win5_hall_of_fame_update",
        "season": {"id": 7, "name": "2026 Season"},
        "round": {"id": 11, "name": "Normal Final", "type": "normal"},
        "results": [_race(17, placement_count=5)],
        "records": [
            {
                "submission_id": index,
                "persona_id": f"persona-{index}",
                "display_name": f"Co-record {index:03d}",
                "tier": "TOP5",
            }
            for index in range(1, record_count + 1)
        ],
    }


def test_round_result_renders_authoritative_result_and_explicit_no_hit_without_mentions() -> None:
    rendered = render_win5_discord_publication(_result_payload())

    assert rendered.publication_type == "win5_round_result"
    assert len(rendered.pages) == 1
    page = rendered.pages[0]
    assert "## WIN5 결과" in page
    assert "2026 @\u200beveryone Season" in page
    assert "Round \\*\\*Final\\*\\* · 일반" in page
    assert "### 결과 · Race 17" in page
    assert "- 1착: Gate 18 — Horse 17-1 @\u200beveryone" in page
    assert "- 5착: Gate 22 — Horse 17-5 @\u200beveryone" in page
    assert "- 적중자 없음" in page
    assert "페이지 1/1" in page
    assert "…" not in page
    assert len(page) <= 1900


def test_renderer_accepts_the_current_application_payload_contract() -> None:
    intents = build_win5_scored_publication_intents(
        source=Win5RoundPublicationSource(
            destination=Win5PublicationDestination(
                guild_id="987654321",
                announcements_enabled=True,
                target_channel_id="123456789",
            ),
            season_id=7,
            season_name="2026 Season",
            round_id=11,
            round_name="Round 1",
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
            personas=(
                Win5PublicationPersona(
                    persona_id="persona-31",
                    display_name="Perfect Player",
                ),
            ),
        ),
        scored_submissions=(
            Win5ScoredSubmissionPublicationSource(
                submission_id=31,
                persona_id="persona-31",
                tier=Win5SubmissionTier.TOP5,
                season_score_delta=15,
                top1_score_delta=0,
                outcomes=(Win5JudgementOutcome.EXACT,) * 5,
            ),
        ),
    )

    result = render_win5_discord_publication(intents[0].payload_json)
    hall = render_win5_discord_publication(intents[1].payload_json)

    assert result.publication_type == "win5_round_result"
    assert "- 1착: Gate 1 — Horse 1" in result.pages[0]
    assert "Perfect Player · TOP5 · Season +15 · TOP1 +0" in result.pages[0]
    assert hall.publication_type == "win5_hall_of_fame_update"
    assert "Perfect Player · TOP5 완전 적중" in hall.pages[0]


def test_positive_hits_render_stored_tier_and_deltas_without_recalculation() -> None:
    rendered = render_win5_discord_publication(
        _result_payload(
            hits=[
                _hit(31, display_name="Alpha @here"),
                {
                    **_hit(32, display_name="Bravo"),
                    "tier": "SPECIAL_WINNER",
                    "season_score_delta": 2,
                    "top1_score_delta": 2,
                },
            ]
        )
    )

    page = rendered.pages[0]
    assert "Alpha @\u200bhere · TOP5 · Season +15 · TOP1 +0" in page
    assert "Bravo · 특별 우승 · Season +2 · TOP1 +2" in page


def test_special_result_paginates_every_registered_race_without_omission() -> None:
    results = [
        _race(
            race_id,
            name=f"Special Race {race_id:03d} " + ("긴이름" * 20),
        )
        for race_id in range(1, 41)
    ]

    rendered = render_win5_discord_publication(_result_payload(round_type="special", results=results))

    combined = "\n".join(rendered.pages)
    assert len(rendered.pages) > 1
    assert all(len(page) <= 1900 for page in rendered.pages)
    assert "개 항목 생략" not in combined
    for race_id in range(1, 41):
        assert combined.count(f"Special Race {race_id:03d}") == 1


def test_mixed_void_v2_renders_reason_without_synthetic_result_or_mention() -> None:
    payload = _result_payload(
        round_type="special",
        results=[_race(101), _void_race(102, reason="공식 취소 @everyone"), _race(103)],
    )
    payload["schema_version"] = 2

    rendered = render_win5_discord_publication(payload)

    page = rendered.pages[0]
    assert "### 결과 · Race 102" in page
    assert "- VOID — 공식 취소 @\u200beveryone" in page
    assert "Race 102\n- 1착" not in page


@pytest.mark.parametrize(
    "results",
    (
        [_void_race(101), _void_race(102)],
        [{**_void_race(101), "placements": _race(101)["placements"]}, _race(102)],
    ),
)
def test_malformed_void_v2_result_fails_closed(results: list[dict[str, object]]) -> None:
    payload = _result_payload(round_type="special", results=results)
    payload["schema_version"] = 2

    with pytest.raises(Win5DiscordPublicationRenderError):
        render_win5_discord_publication(payload)


def test_hall_of_fame_paginates_and_preserves_every_co_record() -> None:
    rendered = render_win5_discord_publication(_hall_payload(record_count=80))

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "win5_hall_of_fame_update"
    assert len(rendered.pages) > 1
    assert all(len(page) <= 1900 for page in rendered.pages)
    assert "개 항목 생략" not in combined
    for index in range(1, 81):
        assert combined.count(f"Co-record {index:03d} · TOP5 완전 적중") == 1


def test_rendering_is_deterministic_for_the_same_stored_payload() -> None:
    payload = _result_payload(hits=[_hit(31), _hit(32)])

    assert render_win5_discord_publication(payload) == render_win5_discord_publication(deepcopy(payload))


@pytest.mark.parametrize(
    ("mutator", "match"),
    (
        (lambda payload: payload.update(schema_version=2), "schema version"),
        (lambda payload: payload.update(no_hits=False), "no_hits"),
        (
            lambda payload: payload["results"].append(deepcopy(payload["results"][0])),
            "Race IDs must be unique",
        ),
    ),
)
def test_malformed_stored_result_payload_fails_closed(mutator: object, match: str) -> None:
    payload = _result_payload()
    assert callable(mutator)
    mutator(payload)

    with pytest.raises(Win5DiscordPublicationRenderError, match=match):
        render_win5_discord_publication(payload)


def test_hall_of_fame_rejects_special_round_or_non_top5_record() -> None:
    special = _hall_payload()
    special["round"]["type"] = "special"
    with pytest.raises(Win5DiscordPublicationRenderError, match="Normal Round"):
        render_win5_discord_publication(special)

    wrong_tier = _hall_payload()
    wrong_tier["records"][0]["tier"] = "TOP3"
    with pytest.raises(Win5DiscordPublicationRenderError, match="TOP5"):
        render_win5_discord_publication(wrong_tier)
