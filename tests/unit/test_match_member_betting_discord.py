"""Discord native member Match Bet placement adapter tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

from uma_st2.adapters.discord import (
    MatchMemberBettingDiscordAdapter,
    format_match_bet_replacement_success,
    format_match_bet_success,
    format_match_race_detail_pages,
    format_match_race_list,
    format_personal_match_bets,
    match_active_bet_autocomplete_choices,
    match_bet_amount_autocomplete_choices,
    match_bet_autocomplete_choices,
    match_race_autocomplete_choices,
    parse_match_bet_numbers,
)
from uma_st2.application.betting import (
    ActiveMatchBetChoice,
    BetPlacementApprovalPendingError,
    BetPlacementDuplicateError,
    BetPlacementEntry,
    BetPlacementStakeLimitError,
    BetReplacementApprovalPendingError,
    BetReplacementBet,
    BetReplacementNoChangeError,
    BetReplacementStakeLimitError,
    MatchBetInputPointState,
    MatchBetTargetChoice,
    MatchRaceDetail,
    MatchRaceDetailCondition,
    MatchRaceDetailCourse,
    MatchRaceDetailEntry,
    MatchRaceDetailUnavailableError,
    MatchRaceListItem,
    MemberMatchBetHistoryItem,
    PlacedMatchBet,
    PlaceMatchBet,
    ReplacedMatchBet,
    ReplaceMatchBet,
    fingerprint_bet_selection,
)
from uma_st2.domain.betting import BetStatus, BetType
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSeason,
    MatchStatus,
    MatchSurface,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
    StadiumCourseLayout,
)

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
_DEFAULT_POINT_STATE = object()


def _receipt() -> PlacedMatchBet:
    selections = (
        BetPlacementEntry(id=101, entry_number=1),
        BetPlacementEntry(id=102, entry_number=2),
    )
    return PlacedMatchBet(
        bet_id=901,
        match_id=71,
        match_name="제12회 @everyone 정기전",
        persona_id="persona-1",
        bet_type=BetType.QUINELLA,
        selections=selections,
        selection_fingerprint=fingerprint_bet_selection(
            bet_type=BetType.QUINELLA,
            selection_ids=(101, 102),
        ),
        amount=20,
        status=BetStatus.ACTIVE,
        balance_after=480,
        placed_at=NOW,
    )


def _replacement_receipt() -> ReplacedMatchBet:
    placement = _receipt()
    placed = PlacedMatchBet(
        bet_id=placement.bet_id,
        match_id=placement.match_id,
        match_name=placement.match_name,
        persona_id=placement.persona_id,
        bet_type=placement.bet_type,
        selections=placement.selections,
        selection_fingerprint=placement.selection_fingerprint,
        amount=placement.amount,
        status=placement.status,
        balance_after=460,
        placed_at=placement.placed_at,
    )
    old_selections = (
        BetPlacementEntry(id=101, entry_number=1),
        BetPlacementEntry(id=103, entry_number=3),
    )
    old = BetReplacementBet(
        id=801,
        match_id=placed.match_id,
        persona_id=placed.persona_id,
        bet_type=BetType.QUINELLA,
        selections=old_selections,
        selection_fingerprint=fingerprint_bet_selection(
            bet_type=BetType.QUINELLA,
            selection_ids=tuple(entry.id for entry in old_selections),
        ),
        amount=30,
        status=BetStatus.CANCELLED,
    )
    return ReplacedMatchBet(
        old_bet=old,
        new_bet=placed,
        balance_before=450,
        balance_after_refund=480,
        balance_after=460,
        refund_point_transaction_id=1001,
        stake_point_transaction_id=1002,
        replaced_at=NOW,
    )


def _history_item(
    bet_id: int = 801,
    *,
    match_name: str = "제12회 정기전",
    bet_status: BetStatus = BetStatus.ACTIVE,
    match_status: MatchStatus = MatchStatus.BETTING_CLOSED,
) -> MemberMatchBetHistoryItem:
    return MemberMatchBetHistoryItem(
        bet_id=bet_id,
        match_name=match_name,
        scheduled_at=NOW,
        match_status=match_status,
        bet_type=BetType.QUINELLA,
        entry_numbers=(1, 3),
        amount=30,
        bet_status=bet_status,
        created_at=NOW,
    )


def _race_detail(
    *,
    status: MatchStatus = MatchStatus.BETTING_OPEN,
    entry_count: int = 11,
) -> MatchRaceDetail:
    return MatchRaceDetail(
        match_id=71,
        match_name="제12회 @everyone 정기전",
        description="공개 설명 @everyone",
        grade=MatchGrade.G1,
        scheduled_at=NOW,
        status=status,
        course=MatchRaceDetailCourse(
            stadium_name="도쿄 @everyone",
            surface=MatchSurface.TURF,
            distance=2400,
            direction=MatchDirection.LEFT,
            layout=StadiumCourseLayout.STANDARD,
        ),
        condition=MatchRaceDetailCondition(
            season=MatchSeason.SPRING,
            weather=MatchWeather.SUNNY,
            time_of_day=MatchTimeOfDay.DAY,
            track_condition=MatchTrackCondition.FIRM,
        ),
        entries=tuple(
            MatchRaceDetailEntry(
                entry_number=index,
                game_account_name=f"주자 {index} @everyone",
                umamusume_name=f"우마무스메 {index}",
                affiliation="테스트 서클",
            )
            for index in range(1, entry_count + 1)
        ),
    )


class RecordingQueries:
    def __init__(
        self,
        *,
        list_error: Exception | None = None,
        personal_error: Exception | None = None,
        detail_error: Exception | None = None,
        point_state: MatchBetInputPointState | None | object = _DEFAULT_POINT_STATE,
    ) -> None:
        self.list_error = list_error
        self.personal_error = personal_error
        self.detail_error = detail_error
        self.point_state = (
            MatchBetInputPointState(balance=500, maximum_stake=50)
            if point_state is _DEFAULT_POINT_STATE
            else point_state
        )
        self.list_calls: list[int] = []
        self.race_search_calls: list[tuple[str, int]] = []
        self.race_detail_calls: list[int] = []
        self.personal_calls: list[tuple[str, int]] = []
        self.calls: list[tuple[str, int]] = []
        self.active_calls: list[tuple[str, str, int]] = []
        self.point_state_calls: list[str] = []

    def list_races(self, *, limit: int) -> tuple[MatchRaceListItem, ...]:
        self.list_calls.append(limit)
        if self.list_error is not None:
            raise self.list_error
        return (
            MatchRaceListItem(
                match_id=71,
                match_name="제12회 @everyone 정기전",
                grade=MatchGrade.G1,
                scheduled_at=NOW,
                status=MatchStatus.BETTING_OPEN,
                entry_count=3,
            ),
        )

    def search_races(self, *, search: str, limit: int) -> tuple[MatchRaceListItem, ...]:
        self.race_search_calls.append((search, limit))
        return (
            MatchRaceListItem(
                match_id=71,
                match_name="제12회 정기전",
                grade=MatchGrade.G1,
                scheduled_at=NOW,
                status=MatchStatus.BETTING_OPEN,
                entry_count=11,
            ),
        )

    def get_race_detail(self, *, match_id: int) -> MatchRaceDetail:
        self.race_detail_calls.append(match_id)
        if self.detail_error is not None:
            raise self.detail_error
        return _race_detail()

    def search_targets(self, *, search: str, limit: int) -> tuple[MatchBetTargetChoice, ...]:
        self.calls.append((search, limit))
        return (
            MatchBetTargetChoice(
                match_id=71,
                match_name="제12회 정기전",
                grade=MatchGrade.G1,
                scheduled_at=NOW,
                entry_count=3,
            ),
        )

    def get_input_point_state(self, *, actor_discord_user_id: str) -> MatchBetInputPointState | None:
        self.point_state_calls.append(actor_discord_user_id)
        return self.point_state  # type: ignore[return-value]

    def search_active_bets(
        self,
        *,
        actor_discord_user_id: str,
        search: str,
        limit: int,
    ) -> tuple[ActiveMatchBetChoice, ...]:
        self.active_calls.append((actor_discord_user_id, search, limit))
        return (
            ActiveMatchBetChoice(
                bet_id=801,
                match_id=71,
                match_name="제12회 정기전",
                scheduled_at=NOW,
                bet_type=BetType.QUINELLA,
                entry_numbers=(1, 3),
                selection_fingerprint="a" * 64,
                amount=30,
            ),
        )

    def list_personal_bets(
        self,
        *,
        actor_discord_user_id: str,
        limit: int,
    ) -> tuple[MemberMatchBetHistoryItem, ...]:
        self.personal_calls.append((actor_discord_user_id, limit))
        if self.personal_error is not None:
            raise self.personal_error
        return (_history_item(match_name="제12회 @everyone 정기전"),)


class RecordingCommands:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        replacement_error: Exception | None = None,
    ) -> None:
        self.error = error
        self.replacement_error = replacement_error
        self.calls: list[PlaceMatchBet] = []
        self.replacement_calls: list[ReplaceMatchBet] = []

    def place_bet(self, command: PlaceMatchBet) -> PlacedMatchBet:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return _receipt()

    def replace_bet(self, command: ReplaceMatchBet) -> ReplacedMatchBet:
        self.replacement_calls.append(command)
        if self.replacement_error is not None:
            raise self.replacement_error
        return _replacement_receipt()


class RecordingPreparation:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, bool]] = []

    async def __call__(self, interaction: object, command_name: str, *, ephemeral: bool) -> bool:
        self.calls.append((interaction, command_name, ephemeral))
        return True


class RecordingAuthorization:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def __call__(self, interaction: object, command_name: str) -> bool:
        self.calls.append((interaction, command_name))
        return True


class RecordingInteraction:
    def __init__(self, *, interaction_id: int = 555) -> None:
        self.id = interaction_id
        self.user = SimpleNamespace(id=123)
        self.guild_id = 987
        self.channel_id = 654
        self.edits: list[dict[str, object]] = []
        self.deleted_count = 0
        self.followup = RecordingFollowup()

    async def edit_original_response(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def delete_original_response(self) -> None:
        self.deleted_count += 1


class RecordingFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


async def _inline[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    return operation()


def _adapter(
    *,
    error: Exception | None = None,
    replacement_error: Exception | None = None,
    list_error: Exception | None = None,
    personal_error: Exception | None = None,
    detail_error: Exception | None = None,
) -> tuple[
    MatchMemberBettingDiscordAdapter,
    RecordingQueries,
    RecordingCommands,
    RecordingPreparation,
    RecordingAuthorization,
]:
    queries = RecordingQueries(
        list_error=list_error,
        personal_error=personal_error,
        detail_error=detail_error,
    )
    commands = RecordingCommands(error=error, replacement_error=replacement_error)
    preparation = RecordingPreparation()
    authorization = RecordingAuthorization()
    return (
        MatchMemberBettingDiscordAdapter(
            queries=queries,  # type: ignore[arg-type]
            commands=commands,  # type: ignore[arg-type]
            replacement_commands=commands,  # type: ignore[arg-type]
            prepare_command=preparation,
            authorize_autocomplete=authorization,
            blocking_runner=_inline,  # type: ignore[arg-type]
        ),
        queries,
        commands,
        preparation,
        authorization,
    )


def test_parser_accepts_v1_comma_and_hyphen_style() -> None:
    assert parse_match_bet_numbers("3, 1") == (3, 1)
    assert parse_match_bet_numbers("2-1") == (2, 1)


def test_parser_rejects_empty_non_ascii_and_non_positive_parts() -> None:
    for value in ("", "1,,2", "０,1", "0,1", "1-a"):
        try:
            parse_match_bet_numbers(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{value!r} should have been rejected")


def test_autocomplete_is_authorized_bounded_and_keeps_duplicate_ids() -> None:
    targets = tuple(
        MatchBetTargetChoice(
            match_id=index,
            match_name="@everyone " + "긴이름" * 40,
            grade=MatchGrade.G1,
            scheduled_at=NOW,
            entry_count=3,
        )
        for index in (71, 72)
    )

    choices = match_bet_autocomplete_choices(targets)

    assert len(choices) == 2
    assert all(len(choice.name) <= 100 for choice in choices)
    assert all("@everyone" not in choice.name for choice in choices)
    assert {choice.value for choice in choices} == {71, 72}
    assert all("ID" in choice.name for choice in choices)


def test_amount_autocomplete_shows_fresh_balance_cap_and_projected_balance() -> None:
    state = MatchBetInputPointState(balance=880, maximum_stake=80)

    initial = match_bet_amount_autocomplete_choices(current_amount="", point_state=state)
    entered = match_bet_amount_autocomplete_choices(current_amount="30", point_state=state)

    assert [(choice.name, choice.value) for choice in initial] == [("현재 잔액 880 pt · 1회 최대 80 pt", 80)]
    assert [(choice.name, choice.value) for choice in entered] == [("30 pt · 현재 잔액 880 pt · 베팅 후 850 pt", 30)]
    assert match_bet_amount_autocomplete_choices(current_amount=0, point_state=None) == []
    assert (
        match_bet_amount_autocomplete_choices(
            current_amount=0,
            point_state=MatchBetInputPointState(balance=5, maximum_stake=10),
        )
        == []
    )


def test_amount_autocomplete_is_authorized_and_actor_scoped() -> None:
    adapter, queries, _, _, authorization = _adapter()
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.autocomplete_amount(interaction, 20))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "match.bet")]
    assert queries.point_state_calls == ["123"]
    assert [(choice.name, choice.value) for choice in choices] == [("20 pt · 현재 잔액 500 pt · 베팅 후 480 pt", 20)]


def test_match_race_list_is_public_bounded_complete_and_mention_safe() -> None:
    matches = tuple(
        MatchRaceListItem(
            match_id=index,
            match_name=f"Match {index} @everyone " + "긴이름" * 40,
            grade=MatchGrade.LISTED,
            scheduled_at=NOW,
            status=MatchStatus.SCHEDULED if index == 1 else MatchStatus.BETTING_OPEN,
            entry_count=index - 1,
        )
        for index in range(1, 11)
    )

    content = format_match_race_list(matches)

    assert len(content) <= 1900
    assert "일정순 최대 10개" in content
    assert "LISTED" in content
    assert "예정 · Entry 0명" in content
    assert "베팅 중 · Entry 9명" in content
    assert all(f"Match {index}" in content for index in range(1, 11))
    assert "@everyone" not in content
    assert "항목 생략" not in content


def test_races_queries_once_and_returns_public_list() -> None:
    adapter, queries, _, preparation, _ = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_races(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.races", False)]
    assert queries.list_calls == [10]
    content = interaction.edits[0]["content"]
    assert isinstance(content, str)
    assert "제12회" in content
    assert "G1" in content
    assert "Entry 3명" in content
    assert "@everyone" not in content


def test_race_detail_autocomplete_is_authorized_and_bounded() -> None:
    adapter, queries, _, _, authorization = _adapter()
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.autocomplete_races(interaction, "정기"))  # type: ignore[arg-type]

    assert authorization.calls == [(interaction, "match.races")]
    assert queries.race_search_calls == [("정기", 25)]
    assert [(choice.value, len(choice.name) <= 100) for choice in choices] == [(71, True)]


def test_race_detail_autocomplete_disambiguates_duplicate_names_and_suppresses_mentions() -> None:
    matches = tuple(
        MatchRaceListItem(
            match_id=index,
            match_name="@everyone 정기전 " + "긴이름" * 30,
            grade=MatchGrade.G1,
            scheduled_at=NOW,
            status=MatchStatus.SCHEDULED,
            entry_count=0,
        )
        for index in (71, 72)
    )

    choices = match_race_autocomplete_choices(matches)

    assert {choice.value for choice in choices} == {71, 72}
    assert all("ID" in choice.name for choice in choices)
    assert all("@everyone" not in choice.name and len(choice.name) <= 100 for choice in choices)


def test_race_detail_renderer_pages_full_roster_and_suppresses_mentions() -> None:
    pages = format_match_race_detail_pages(_race_detail())

    assert len(pages) == 3
    assert all(len(page) <= 1900 for page in pages)
    combined = "\n".join(pages)
    assert "잔디 2400m · 좌회전 · 일반" in combined
    assert "환경: 봄 · 맑음 · 낮 · 양호" in combined
    assert "번호 · 주자 · 우마무스메" in combined
    assert all(f"- {index} · 주자 {index}" in combined for index in range(1, 12))
    assert "@everyone" not in combined
    assert "항목 생략" not in combined


def test_races_selected_detail_revalidates_once_and_delivers_every_public_page() -> None:
    adapter, queries, _, preparation, _ = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_races(interaction, match_id=71))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.races", False)]
    assert queries.list_calls == []
    assert queries.race_detail_calls == [71]
    assert len(interaction.edits) == 1
    assert len(interaction.followup.messages) == 2
    assert all(message["ephemeral"] is False for message in interaction.followup.messages)
    combined = "\n".join(
        [str(interaction.edits[0]["content"])] + [str(message["content"]) for message in interaction.followup.messages]
    )
    assert all(f"- {index} · 주자 {index}" in combined for index in range(1, 12))


def test_race_detail_supports_scheduled_zero_entry_match() -> None:
    pages = format_match_race_detail_pages(_race_detail(status=MatchStatus.SCHEDULED, entry_count=0))

    assert len(pages) == 1
    assert "Entry (0명)" in pages[0]
    assert "등록된 Entry가 없습니다" in pages[0]


def test_stale_race_detail_removes_public_placeholder_and_returns_expected_private_error() -> None:
    adapter, queries, _, preparation, _ = _adapter(detail_error=MatchRaceDetailUnavailableError("stale"))
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_races(interaction, match_id=71))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.races", False)]
    assert queries.race_detail_calls == [71]
    assert interaction.edits == []
    assert interaction.deleted_count == 1
    assert interaction.followup.messages[0]["ephemeral"] is True
    assert "더 이상 예정 또는 베팅 중" in interaction.followup.messages[0]["content"]


def test_races_internal_failure_removes_public_placeholder_and_sends_private_error() -> None:
    adapter, queries, _, preparation, _ = _adapter(list_error=RuntimeError("database unavailable"))
    interaction = RecordingInteraction(interaction_id=777)

    asyncio.run(adapter.list_races(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.races", False)]
    assert queries.list_calls == [10]
    assert interaction.edits == []
    assert interaction.deleted_count == 1
    assert interaction.followup.messages[0]["ephemeral"] is True
    assert "777" in interaction.followup.messages[0]["content"]


def test_match_race_list_formats_empty_result_as_normal_public_response() -> None:
    content = format_match_race_list(())

    assert "예정되었거나 베팅 중인 룸매치가 없습니다" in content


def test_personal_bet_list_is_private_bounded_complete_and_mention_safe() -> None:
    bets = tuple(
        _history_item(
            bet_id=index,
            match_name=f"Match {index} @everyone " + "긴이름" * 40,
            bet_status=(BetStatus.ACTIVE if index == 10 else BetStatus.CANCELLED),
            match_status=(MatchStatus.BETTING_CLOSED if index == 10 else MatchStatus.BETTING_OPEN),
        )
        for index in range(1, 11)
    )

    content = format_personal_match_bets(bets)

    assert len(content) <= 1900
    assert "최신 10건" in content
    assert "복승 1-3" in content
    assert "30 pt" in content
    assert "Circle Point" not in content
    assert "활성 / 베팅 마감" in content
    assert "취소됨 / 베팅 중" in content
    assert all(f"Bet #{index}" in content for index in range(1, 11))
    assert "@everyone" not in content
    assert "항목 생략" not in content


def test_bets_queries_actor_once_and_returns_private_list() -> None:
    adapter, queries, _, preparation, _ = _adapter()
    interaction = RecordingInteraction()

    asyncio.run(adapter.list_personal_bets(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.bets", True)]
    assert queries.personal_calls == [("123", 10)]
    content = interaction.edits[0]["content"]
    assert isinstance(content, str)
    assert "Bet #801" in content
    assert "활성 / 베팅 마감" in content
    assert "@everyone" not in content


def test_bets_internal_failure_stays_private_and_uses_generic_error() -> None:
    adapter, queries, _, preparation, _ = _adapter(personal_error=RuntimeError("database unavailable"))
    interaction = RecordingInteraction(interaction_id=778)

    asyncio.run(adapter.list_personal_bets(interaction))  # type: ignore[arg-type]

    assert preparation.calls == [(interaction, "match.bets", True)]
    assert queries.personal_calls == [("123", 10)]
    assert len(interaction.edits) == 1
    assert "778" in interaction.edits[0]["content"]


def test_personal_bet_list_formats_empty_result_without_identity_detail() -> None:
    content = format_personal_match_bets(())

    assert "베팅 내역이 없습니다" in content
    assert "Persona" not in content


def test_direct_slash_call_is_final_and_returns_private_committed_receipt() -> None:
    adapter, _, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=991)

    asyncio.run(
        adapter.place_bet(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            bet_type="quinella",
            numbers="2,1",
            amount=20,
        )
    )

    assert preparation.calls == [(interaction, "match.bet", True)]
    assert len(commands.calls) == 1
    command = commands.calls[0]
    assert command.entry_numbers == (1, 2)
    assert command.idempotency_key == "match-bet:991"
    assert command.actor_discord_user_id == "123"
    assert command.guild_id == "987"
    content = interaction.edits[0]["content"]
    assert isinstance(content, str)
    assert "베팅 접수 완료" in content
    assert "Entry `1-2`" in content
    assert "베팅액: 20 pt" in content
    assert "잔액: 480 pt" in content
    assert "Circle Point" not in content
    assert "@everyone" not in content


def test_active_bet_autocomplete_is_actor_scoped_bounded_and_mention_safe() -> None:
    adapter, queries, _, _, authorization = _adapter()
    interaction = RecordingInteraction()

    choices = asyncio.run(adapter.autocomplete_active_bets(interaction, "정기"))  # type: ignore[arg-type]

    assert queries.active_calls == [("123", "정기", 25)]
    assert authorization.calls == [(interaction, "match.bet-change")]
    assert [(choice.value, len(choice.name) <= 100) for choice in choices] == [(801, True)]

    unsafe = ActiveMatchBetChoice(
        bet_id=802,
        match_id=72,
        match_name="@everyone " + "긴이름" * 40,
        scheduled_at=NOW,
        bet_type=BetType.WIN,
        entry_numbers=(1,),
        selection_fingerprint="b" * 64,
        amount=10,
    )
    assert "@everyone" not in match_active_bet_autocomplete_choices((unsafe,))[0].name


def test_replacement_direct_slash_uses_final_interaction_and_private_receipt() -> None:
    adapter, _, commands, preparation, _ = _adapter()
    interaction = RecordingInteraction(interaction_id=992)

    asyncio.run(
        adapter.replace_bet(  # type: ignore[arg-type]
            interaction,
            bet_id=801,
            bet_type="quinella",
            numbers="2,1",
            amount=20,
        )
    )

    assert preparation.calls == [(interaction, "match.bet-change", True)]
    assert len(commands.replacement_calls) == 1
    command = commands.replacement_calls[0]
    assert command.bet_id == 801
    assert command.entry_numbers == (1, 2)
    assert command.idempotency_key == "match-bet-change:992"
    assert command.actor_discord_user_id == "123"
    content = interaction.edits[0]["content"]
    assert isinstance(content, str)
    assert "베팅 정정 완료" in content
    assert "기존 Bet" in content and "환불" in content
    assert "새 Bet" in content and "차감" in content
    assert "잔액: 460 pt" in content
    assert "Circle Point" not in content
    assert "@everyone" not in content


def test_known_rejection_is_private_and_does_not_retry_write() -> None:
    adapter, _, commands, _, _ = _adapter(error=BetPlacementDuplicateError("duplicate"))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.place_bet(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            bet_type="quinella",
            numbers="1,2",
            amount=20,
        )
    )

    assert len(commands.calls) == 1
    assert "active Bet이 이미" in interaction.edits[0]["content"]


def test_pending_approval_placement_uses_distinct_private_guidance() -> None:
    adapter, _, commands, _, _ = _adapter(error=BetPlacementApprovalPendingError("internal pending identity"))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.place_bet(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            bet_type="quinella",
            numbers="1,2",
            amount=20,
        )
    )

    assert len(commands.calls) == 1
    content = interaction.edits[0]["content"]
    assert "계정 승인을 기다리는 중" in content
    assert "internal pending identity" not in content


def test_placement_stake_limit_reports_current_private_cap() -> None:
    adapter, _, commands, _, _ = _adapter(error=BetPlacementStakeLimitError(maximum_stake=50))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.place_bet(  # type: ignore[arg-type]
            interaction,
            match_id=71,
            bet_type="quinella",
            numbers="1,2",
            amount=60,
        )
    )

    assert len(commands.calls) == 1
    assert "1회 베팅 상한은 `50 pt`" in interaction.edits[0]["content"]


def test_replacement_no_change_is_private_and_does_not_retry_write() -> None:
    adapter, _, commands, _, _ = _adapter(replacement_error=BetReplacementNoChangeError("same"))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.replace_bet(  # type: ignore[arg-type]
            interaction,
            bet_id=801,
            bet_type="quinella",
            numbers="1,3",
            amount=30,
        )
    )

    assert len(commands.replacement_calls) == 1
    assert "정정 내용이 같습니다" in interaction.edits[0]["content"]


def test_pending_approval_replacement_uses_distinct_private_guidance() -> None:
    adapter, _, commands, _, _ = _adapter(
        replacement_error=BetReplacementApprovalPendingError("internal pending identity")
    )
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.replace_bet(  # type: ignore[arg-type]
            interaction,
            bet_id=801,
            bet_type="quinella",
            numbers="1,3",
            amount=30,
        )
    )

    assert len(commands.replacement_calls) == 1
    content = interaction.edits[0]["content"]
    assert "계정 승인을 기다리는 중" in content
    assert "internal pending identity" not in content


def test_replacement_stake_limit_reports_post_refund_private_cap() -> None:
    adapter, _, commands, _, _ = _adapter(replacement_error=BetReplacementStakeLimitError(maximum_stake=50))
    interaction = RecordingInteraction()

    asyncio.run(
        adapter.replace_bet(  # type: ignore[arg-type]
            interaction,
            bet_id=801,
            bet_type="quinella",
            numbers="1,3",
            amount=60,
        )
    )

    assert len(commands.replacement_calls) == 1
    assert "환불 후 잔액 기준 1회 베팅 상한은 `50 pt`" in interaction.edits[0]["content"]


def test_success_formatter_suppresses_mentions() -> None:
    assert "@everyone" not in format_match_bet_success(_receipt())
    assert "@everyone" not in format_match_bet_replacement_success(_replacement_receipt())
