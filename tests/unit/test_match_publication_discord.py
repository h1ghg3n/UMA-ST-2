"""Discord rendering tests for supported stored Match publication snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from uma_st2.adapters.discord import (
    DiscordPublicationRenderError,
    MatchDiscordPublicationRenderError,
    render_discord_publication,
    render_match_discord_publication,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETS_REFUNDED_EVENT_TYPE,
    MATCH_BETTING_CLOSED_EVENT_TYPE,
    MATCH_BETTING_OPENED_EVENT_TYPE,
    MATCH_ODDS_REFRESH_EVENT_TYPE,
    MATCH_RESULT_CONFIRMED_EVENT_TYPE,
    MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
    MatchBettingClosePublicationSource,
    MatchBettingCloseSelection,
    MatchOddsRefreshMatch,
    MatchOddsRefreshMode,
    MatchOddsRefreshPublicationSource,
    MatchOddsRefreshSelection,
    MatchOpeningCondition,
    MatchOpeningCourse,
    MatchOpeningEntry,
    MatchOpeningPublicationSource,
    MatchPublicationDestination,
    MatchRefundPublicationSource,
    MatchResultCourse,
    MatchResultEntry,
    MatchResultOdds,
    MatchResultPublicationSource,
    MatchSettlementVoidedPublicationSource,
    build_zero_pool_opening_markets,
)
from uma_st2.domain.betting import BetPoolStake, BetType, calculate_provisional_odds, quantize_applied_odds
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)


def _opening_payload(*, entry_count: int = 3) -> dict[str, object]:
    entries = tuple(
        MatchOpeningEntry(
            entry_id=800 + index,
            entry_number=index,
            game_account_name=f"trainer-{index} @everyone",
            horse_name=f"horse-{index} @here",
            affiliation="circle-a",
        )
        for index in range(1, entry_count + 1)
    )
    return MatchOpeningPublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        match_id=99887766,
        match_name="제12회 @here 정기전",
        description="공식 @everyone 룸매치",
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        course=MatchOpeningCourse(
            course_id=88776655,
            stadium_id=77665544,
            stadium_name="도쿄 @everyone",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=MatchOpeningCondition(
            season=MatchSeason.AUTUMN,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.NIGHT,
            track_condition=MatchTrackCondition.FIRM,
        ),
        entries=entries,
        markets=build_zero_pool_opening_markets(entry_count),
    ).to_payload()


def _payload(*, entry_count: int = 3) -> dict[str, object]:
    entries = tuple(
        MatchResultEntry(
            entry_number=index,
            official_rank=index,
            player_name=f"trainer-{index} @everyone",
            character_name=f"horse-{index}",
            affiliation="circle-a",
            rating_before=Decimal(100),
            rating_delta=Decimal(index if index == 1 else 0),
            rating_after=Decimal(100 + (index if index == 1 else 0)),
        )
        for index in range(1, entry_count + 1)
    )
    odds = tuple(
        MatchResultOdds(
            bet_type=bet_type,
            selection_entry_numbers=tuple(range(1, selection_count + 1)),
            confirmed_odds=Decimal(f"{selection_count}.1"),
        )
        for selection_count, bet_type in enumerate(tuple(BetType)[: min(entry_count, len(BetType))], start=1)
    )
    return MatchResultPublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        match_id=71,
        match_name="제12회 @here 정기전",
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        course=MatchResultCourse(
            stadium_name="도쿄 @everyone",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=MatchOpeningCondition(
            season=MatchSeason.AUTUMN,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.NIGHT,
            track_condition=MatchTrackCondition.FIRM,
        ),
        entries=entries,
        odds=odds,
    ).to_payload()


def _refund_payload(*, reason: str | None = "태풍 @everyone") -> dict[str, object]:
    return MatchRefundPublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        match_id=99887766,
        match_name="제12회 @here 정기전",
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        refunded_at=datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
        reason=reason,
    ).to_payload()


def _settlement_voided_payload() -> dict[str, object]:
    return MatchSettlementVoidedPublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        match_id=99887766,
        match_name="제12회 @here 정기전",
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        rolled_back_at=datetime(2026, 9, 2, 14, 0, tzinfo=UTC),
        reason="공식 결과 오류 @everyone",
    ).to_payload()


def _odds_refresh_payload(
    *,
    match_count: int = 2,
    entry_count: int = 4,
    mode: MatchOddsRefreshMode = MatchOddsRefreshMode.NORMAL,
    active_bets: tuple[BetPoolStake, ...] = (),
) -> dict[str, object]:
    matches = []
    for match_index in range(match_count):
        odds = calculate_provisional_odds(tuple(range(1, entry_count + 1)), active_bets)
        matches.append(
            MatchOddsRefreshMatch(
                match_id=900 + match_index,
                match_name=f"제{match_index + 1}경기 @everyone",
                grade=MatchGrade.G1,
                scheduled_at=datetime(2026, 9, 2 + match_index, 12, 0, tzinfo=UTC),
                selections=tuple(
                    MatchOddsRefreshSelection(
                        bet_type=item.bet_type,
                        entry_numbers=item.selection_ids,
                        provisional_odds=item.odds,
                    )
                    for item in odds
                ),
            )
        )
    return MatchOddsRefreshPublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        mode=mode,
        sequence=7,
        generated_at=datetime(2026, 9, 1, 5, 0, tzinfo=UTC),
        matches=tuple(matches),
    ).to_payload()


def _betting_close_payload(
    *,
    entry_count: int = 4,
    active_bets: tuple[BetPoolStake, ...] = (),
) -> dict[str, object]:
    odds = calculate_provisional_odds(tuple(range(1, entry_count + 1)), active_bets)
    return MatchBettingClosePublicationSource(
        destination=MatchPublicationDestination("987654321", True, "777777777"),
        match_id=99887766,
        match_name="제12회 @everyone 정기전",
        grade=MatchGrade.G1,
        scheduled_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        closed_at=datetime(2026, 9, 2, 11, 55, tzinfo=UTC),
        selections=tuple(
            MatchBettingCloseSelection(
                bet_type=item.bet_type,
                entry_numbers=item.selection_ids,
                confirmed_odds=quantize_applied_odds(item.odds, field_size=entry_count),
            )
            for item in odds
        ),
    ).to_payload()


def test_renderer_shows_complete_opening_entries_and_uniform_odds_without_protected_details() -> None:
    rendered = render_match_discord_publication(_opening_payload())

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "match_betting_opening"
    assert "룸매치 베팅 오픈" in combined
    assert "Entry 1 · trainer-1" in combined
    assert "/ horse-1" in combined
    assert "· circle-a" in combined
    assert "단승 · 전체 3개 조합 · 잠정 `3.0000배`" in combined
    assert "삼복승 · 전체 1개 조합 · 잠정 `1.0000배`" in combined
    assert "이후 베팅에 따라 변동" in combined
    assert "@everyone" not in combined
    assert "@here" not in combined
    assert "99887766" not in combined
    assert "88776655" not in combined
    assert "77665544" not in combined
    assert "participant_count" not in combined
    assert "pool_amount" not in combined
    assert "persona_id" not in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


def test_opening_renderer_marks_unavailable_market_without_enumerating_selections() -> None:
    rendered = render_match_discord_publication(_opening_payload(entry_count=2))

    combined = "\n".join(rendered.pages)
    assert "단승 · 전체 2개 조합 · 잠정 `2.0000배`" in combined
    assert "복승 · 전체 1개 조합 · 잠정 `1.0000배`" in combined
    assert "삼복승 · 이용 불가 (Entry 수 부족)" in combined


def test_opening_renderer_labels_random_configured_conditions() -> None:
    payload = _opening_payload()
    payload["condition"]["weather"] = "random"
    payload["condition"]["track_condition"] = "random"

    rendered = render_match_discord_publication(payload)

    assert "가을 · 랜덤 · 밤 · 랜덤" in "\n".join(rendered.pages)


def test_renderer_shows_complete_result_rating_and_final_odds_without_mentions() -> None:
    rendered = render_match_discord_publication(_payload())

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "match_settled_result"
    assert "룸매치 최종 결과" in combined
    assert "1착 · Entry 1" in combined
    assert "Rating #1 `100.0000 → 101.0000` (`+1.0000`)" in combined
    assert "단승 `1` · `1.1배`" in combined
    assert "복승 `1-2` · `2.1배`" in combined
    assert "@everyone" not in combined
    assert "@here" not in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


def test_result_renderer_accepts_one_quantum_from_independent_rating_rounding() -> None:
    payload = _payload()
    payload["results"][0]["rating"] = {
        "before": "52.7512",
        "delta": "16.5855",
        "after": "69.3366",
    }

    rendered = render_match_discord_publication(payload)

    assert "Rating #1 `52.7512 → 69.3366` (`+16.5855`)" in "\n".join(rendered.pages)


def test_refund_renderer_shows_only_full_refund_fact_and_optional_reason() -> None:
    rendered = render_match_discord_publication(_refund_payload())

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "match_bet_refund_completed"
    assert "룸매치 베팅 환불 완료" in combined
    assert "모든 active Bet original stake를 전액 환불" in combined
    assert "사유: 태풍" in combined
    assert "@everyone" not in combined
    assert "@here" not in combined
    assert "99887766" not in combined
    for protected in ("participant", "Bet 수", "환불 총액", "Circle Point", "persona_id", "bet_id"):
        assert protected not in combined

    without_reason = "\n".join(render_match_discord_publication(_refund_payload(reason=None)).pages)
    assert "사유:" not in without_reason


def test_settlement_voided_renderer_shows_append_only_correction_without_private_compensation_details() -> None:
    rendered = render_match_discord_publication(_settlement_voided_payload())

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "match_settlement_voided"
    assert "룸매치 정산 무효 처리" in combined
    assert "기존 정산 결과는 무효 처리" in combined
    assert "서클 포인트와 Rating 상태의 보상 처리가 완료" in combined
    assert "사유: 공식 결과 오류" in combined
    assert "@everyone" not in combined
    assert "@here" not in combined
    assert "99887766" not in combined
    for protected in ("Bet", "stake", "balance", "transaction_id", "persona_id", "rating_before"):
        assert protected not in combined


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload["rollback"].update({"compensation_completed": False}),
            "compensation_completed must be true",
        ),
        (
            lambda payload: payload["rollback"].update({"reason": ""}),
            "rollback.reason must be a non-empty string",
        ),
        (
            lambda payload: payload["rollback"].update({"private_balance": 500}),
            "unsupported or missing fields",
        ),
    ),
)
def test_settlement_voided_renderer_rejects_incomplete_or_extended_evidence(mutate: object, message: str) -> None:
    payload = deepcopy(_settlement_voided_payload())
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(MatchDiscordPublicationRenderError, match=message):
        render_match_discord_publication(payload)


def test_periodic_odds_renderer_collapses_uniform_markets_with_blank_line_and_no_protected_pool_data() -> None:
    rendered = render_match_discord_publication(_odds_refresh_payload())

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "match_odds_refresh"
    assert "룸매치 잠정 배당률" in combined
    assert "공지 모드: NORMAL · 10분" in combined
    assert "### 제1경기" in combined
    assert "\n\n### 제2경기" in combined
    assert "#### 단승" in combined
    assert "#### 복승" in combined
    assert "#### 삼복승" in combined
    assert "전체 4개 조합 동일 · `4.0000배`" in combined
    assert "전체 6개 조합 동일 · `6.0000배`" in combined
    assert "@everyone" not in combined
    assert "900" not in combined
    for protected in ("participant_count", "pool_amount", "persona_id", "bet_id", "stake"):
        assert protected not in combined


def test_betting_close_renderer_collapses_uniform_final_odds_in_a_separate_publication() -> None:
    rendered = render_match_discord_publication(_betting_close_payload())

    combined = "\n".join(rendered.pages)
    assert rendered.publication_type == "match_betting_closed"
    assert "룸매치 베팅 마감" in combined
    assert "단승 최종 배당률" in combined
    assert "복승 최종 배당률" in combined
    assert "삼복승 최종 배당률" in combined
    assert "전체 4개 조합 동일 · `2.0배`" in combined
    assert "전체 6개 조합 동일 · `3.0배`" in combined
    assert "전체 4개 조합 동일 · `2.0배`" in combined
    assert "@everyone" not in combined
    assert "99887766" not in combined
    for protected in ("participant_count", "pool_amount", "persona_id", "bet_id", "stake"):
        assert protected not in combined


def test_betting_close_renderer_reduces_eighteen_entry_zero_pool_to_one_page() -> None:
    rendered = render_match_discord_publication(_betting_close_payload(entry_count=18))

    assert len(rendered.pages) == 1
    combined = "\n".join(rendered.pages)
    assert "전체 18개 조합 동일" in combined
    assert "전체 153개 조합 동일" in combined
    assert "전체 816개 조합 동일" in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


def test_betting_close_renderer_shows_all_win_and_top_ten_combination_odds() -> None:
    active_bets = (
        BetPoolStake(BetType.WIN, (1,), 100),
        BetPoolStake(BetType.QUINELLA, (1, 2), 100),
        BetPoolStake(BetType.TRIO, (1, 2, 3), 100),
    )
    rendered = render_match_discord_publication(_betting_close_payload(entry_count=18, active_bets=active_bets))

    combined = "\n".join(rendered.pages)
    selection_lines = [line for line in combined.splitlines() if line.startswith("- `")]
    assert len(selection_lines) == 18 + 10 + 10
    assert "단승 최종 배당률 · 인기순 전체 18개" in combined
    assert "복승 최종 배당률 · 인기순 10/153" in combined
    assert "삼복승 최종 배당률 · 인기순 10/816" in combined
    assert "- `1` · `" in combined
    assert "- `1-2` · `" in combined
    assert "- `1-2-3` · `" in combined
    assert "배`" in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload["odds"].update({"field_multiplier": "1.0"}),
            "field multiplier",
        ),
        (
            lambda payload: payload["odds"]["markets"][1]["selections"].pop(),
            "incomplete or out of order",
        ),
        (
            lambda payload: payload["odds"]["markets"][0]["selections"][0].update({"confirmed_odds": "2.0000"}),
            "exactly 1 decimal",
        ),
    ),
)
def test_betting_close_renderer_rejects_inconsistent_stored_projection(mutate: object, message: str) -> None:
    payload = deepcopy(_betting_close_payload())
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(MatchDiscordPublicationRenderError, match=message):
        render_match_discord_publication(payload)


def test_periodic_odds_renderer_summarizes_non_uniform_combination_markets() -> None:
    entry_count = 18
    active_bets = (
        BetPoolStake(BetType.WIN, (1,), 100),
        BetPoolStake(BetType.QUINELLA, (1, 2), 100),
        BetPoolStake(BetType.TRIO, (1, 2, 3), 100),
    )
    rendered = render_match_discord_publication(
        _odds_refresh_payload(
            match_count=2,
            entry_count=entry_count,
            mode=MatchOddsRefreshMode.LIVE,
            active_bets=active_bets,
        )
    )

    combined = "\n".join(rendered.pages)
    selection_lines = [line for line in combined.splitlines() if line.startswith("- `")]
    expected_per_match = entry_count + 10 + 10
    assert len(selection_lines) == expected_per_match * 2
    assert combined.count("#### 단승 · 인기순 전체 18개") == 2
    assert combined.count("#### 복승 · 인기순 10/153") == 2
    assert combined.count("#### 삼복승 · 인기순 10/816") >= 2
    assert sum(line.startswith("- `1-2-3` · `") for line in selection_lines) == 2
    assert all("배`" in line for line in selection_lines)
    assert "공지 모드: LIVE · 1분" in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload["coverage"].update({"cadence_seconds": 60}),
            "cadence",
        ),
        (
            lambda payload: payload["matches"].reverse(),
            "stable order",
        ),
        (
            lambda payload: payload["matches"][0]["markets"][1]["selections"].pop(),
            "incomplete or out of order",
        ),
    ),
)
def test_periodic_odds_renderer_rejects_inconsistent_stored_projection(mutate: object, message: str) -> None:
    payload = deepcopy(_odds_refresh_payload())
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(MatchDiscordPublicationRenderError, match=message):
        render_match_discord_publication(payload)


def test_generic_delivery_dispatches_each_match_route_and_rejects_cross_route_payloads() -> None:
    result = render_discord_publication(
        MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        MATCH_RESULT_CONFIRMED_EVENT_TYPE,
        _payload(),
    )
    opening = render_discord_publication(
        MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        MATCH_BETTING_OPENED_EVENT_TYPE,
        _opening_payload(),
    )
    refund = render_discord_publication(
        MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        MATCH_BETS_REFUNDED_EVENT_TYPE,
        _refund_payload(),
    )
    odds = render_discord_publication(
        MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        MATCH_ODDS_REFRESH_EVENT_TYPE,
        _odds_refresh_payload(),
    )
    close = render_discord_publication(
        MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        MATCH_BETTING_CLOSED_EVENT_TYPE,
        _betting_close_payload(),
    )
    voided = render_discord_publication(
        MATCH_ANNOUNCEMENT_DESTINATION_KIND,
        MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
        _settlement_voided_payload(),
    )

    assert "룸매치 최종 결과" in result.pages[0]
    assert "룸매치 베팅 오픈" in opening.pages[0]
    assert "룸매치 베팅 환불 완료" in refund.pages[0]
    assert "룸매치 잠정 배당률" in odds.pages[0]
    assert "룸매치 베팅 마감" in close.pages[0]
    assert "룸매치 정산 무효 처리" in voided.pages[0]

    with pytest.raises(DiscordPublicationRenderError, match="disagree"):
        render_discord_publication(
            MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            MATCH_BETTING_OPENED_EVENT_TYPE,
            _payload(),
        )
    with pytest.raises(DiscordPublicationRenderError, match="disagree"):
        render_discord_publication(
            MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            MATCH_RESULT_CONFIRMED_EVENT_TYPE,
            _opening_payload(),
        )
    with pytest.raises(DiscordPublicationRenderError, match="disagree"):
        render_discord_publication(
            MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            MATCH_SETTLEMENT_VOIDED_EVENT_TYPE,
            _payload(),
        )
    with pytest.raises(DiscordPublicationRenderError, match="disagree"):
        render_discord_publication(
            MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            MATCH_BETTING_CLOSED_EVENT_TYPE,
            _opening_payload(),
        )
    with pytest.raises(DiscordPublicationRenderError, match="disagree"):
        render_discord_publication(
            MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            MATCH_BETS_REFUNDED_EVENT_TYPE,
            _payload(),
        )
    with pytest.raises(DiscordPublicationRenderError, match="disagree"):
        render_discord_publication(
            MATCH_ANNOUNCEMENT_DESTINATION_KIND,
            MATCH_ODDS_REFRESH_EVENT_TYPE,
            _opening_payload(),
        )

    with pytest.raises(DiscordPublicationRenderError):
        render_discord_publication("match_announcement", "unsupported", _payload())


def test_renderer_paginates_every_entry_without_omission() -> None:
    rendered = render_match_discord_publication(_payload(entry_count=25))

    assert len(rendered.pages) > 1
    combined = "\n".join(rendered.pages)
    for index in range(1, 26):
        assert f"{index}착 · Entry {index}" in combined
    assert "개 항목 생략" not in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


def test_opening_renderer_paginates_every_entry_without_omission() -> None:
    rendered = render_match_discord_publication(_opening_payload(entry_count=25))

    assert len(rendered.pages) > 1
    combined = "\n".join(rendered.pages)
    for index in range(1, 26):
        assert f"Entry {index} · trainer-{index}" in combined
        assert f"/ horse-{index}" in combined
    assert "개 항목 생략" not in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


def test_opening_renderer_bounds_max_length_markdown_snapshots_without_omission() -> None:
    payload = _opening_payload()
    payload["match"]["name"] = "*" * 200
    payload["match"]["description"] = "_" * 4000
    payload["course"]["stadium_name"] = "~" * 100
    payload["entries"][0]["game_account_name"] = "|" * 100
    payload["entries"][0]["horse_name"] = ">" * 100
    payload["entries"][0]["affiliation"] = "*" * 100

    rendered = render_match_discord_publication(payload)

    combined = "\n".join(rendered.pages)
    assert "Entry 1" in combined
    assert "Entry 2" in combined
    assert "Entry 3" in combined
    assert "…" in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


def test_renderer_preserves_max_length_markdown_snapshots_after_escaping() -> None:
    payload = _payload()
    payload["match"]["name"] = "*" * 200
    payload["course"]["stadium_name"] = "_" * 100
    payload["results"][0]["player_name"] = "~" * 100
    payload["results"][0]["character_name"] = "|" * 100
    payload["results"][0]["affiliation"] = ">" * 100

    rendered = render_match_discord_publication(payload)

    combined = "\n".join(rendered.pages)
    assert "\\*" * 200 in combined
    assert "\\_" * 100 in combined
    assert "\\~" * 100 in combined
    assert "\\|" * 100 in combined
    assert ">" * 100 in combined
    assert all(len(page) <= 1900 for page in rendered.pages)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload.update({"uma_pid": "protected"}),
            "unsupported or missing fields",
        ),
        (
            lambda payload: payload["results"][0]["rating"].update({"after": "999.0000"}),
            "Rating transition",
        ),
        (
            lambda payload: payload["odds"]["markets"][1].update({"winning_selection": [1, 3]}),
            "official ranks",
        ),
    ),
)
def test_renderer_fails_closed_on_malformed_or_extended_payload(mutate: object, message: str) -> None:
    payload = deepcopy(_payload())
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(MatchDiscordPublicationRenderError, match=message):
        render_match_discord_publication(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda payload: payload.update({"participant_count": 3}),
            "unsupported or missing fields",
        ),
        (
            lambda payload: payload["entries"][1].update({"entry_id": 801}),
            "unique IDs",
        ),
        (
            lambda payload: payload["entries"][1].update({"entry_number": 3}),
            "contiguous Entry numbers",
        ),
        (
            lambda payload: payload["odds"].update({"rule_version": "future-rule"}),
            "rule version",
        ),
        (
            lambda payload: payload["odds"]["markets"][0].update({"uniform_odds": "3.0001"}),
            "canonical projection",
        ),
    ),
)
def test_opening_renderer_fails_closed_on_malformed_or_extended_payload(mutate: object, message: str) -> None:
    payload = deepcopy(_opening_payload())
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(MatchDiscordPublicationRenderError, match=message):
        render_match_discord_publication(payload)
