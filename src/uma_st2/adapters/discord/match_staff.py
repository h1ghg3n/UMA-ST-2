"""Shared Discord Match context, course filtering, and root command group."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

import discord
from discord import app_commands

from uma_st2.application.match import (
    MatchCourseChoice,
)
from uma_st2.domain.match import (
    MatchDirection,
    MatchGrade,
    MatchSurface,
    StadiumCourseLayout,
)

from .datetime_codec import DiscordTimezone
from .strings.match_registration import (
    AMOUNT_OPTION_NAME,
    BET_AMOUNT_DESCRIPTION,
    BET_CHANGE_AMOUNT_DESCRIPTION,
    BET_CHANGE_DESCRIPTION,
    BET_CHANGE_NUMBERS_DESCRIPTION,
    BET_CHANGE_TARGET_DESCRIPTION,
    BET_CHANGE_TYPE_DESCRIPTION,
    BET_DESCRIPTION,
    BET_ID_OPTION_NAME,
    BET_MATCH_DESCRIPTION,
    BET_NUMBERS_DESCRIPTION,
    BET_TYPE_CHOICE_NAMES,
    BET_TYPE_DESCRIPTION,
    BET_TYPE_OPTION_NAME,
    BETS_DESCRIPTION,
    MATCH_ID_OPTION_NAME,
    MATCH_ROOT_DESCRIPTION,
    NUMBERS_OPTION_NAME,
    PERSONA_OPTION_NAME,
    RACES_DESCRIPTION,
    RACES_MATCH_DESCRIPTION,
    RANK_OPTION_NAME,
    RATINGS_DESCRIPTION,
    RATINGS_PERSONA_DESCRIPTION,
    RATINGS_RANK_DESCRIPTION,
)
from .strings.match_staff_creation import (
    COURSE_AMBIGUOUS,
    COURSE_COMBINATION_UNAVAILABLE,
    COURSE_OPTIONS_EMPTY,
    COURSE_OPTIONS_OVERFLOW,
    UNKNOWN_SELECTION_STEP,
    direction_label,
    format_course_selection_copy,
    layout_label,
    surface_label,
)

_MAX_SELECT_OPTIONS = 25


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive Discord snowflake.")
    return value


@dataclass(frozen=True, slots=True)
class MatchStaffInteractionContext:
    """Opener and guild/channel binding carried across one private flow."""

    user_id: int
    guild_id: int
    channel_id: int

    @classmethod
    def from_interaction(cls, interaction: object) -> MatchStaffInteractionContext:
        return cls(
            user_id=_required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            ),
            guild_id=_required_snowflake(getattr(interaction, "guild_id", None), field_name="guild ID"),
            channel_id=_required_snowflake(getattr(interaction, "channel_id", None), field_name="channel ID"),
        )

    def matches(self, interaction: object) -> bool:
        return (
            getattr(getattr(interaction, "user", None), "id", None) == self.user_id
            and getattr(interaction, "guild_id", None) == self.guild_id
            and getattr(interaction, "channel_id", None) == self.channel_id
        )


@dataclass(frozen=True, slots=True)
class MatchCourseSelection:
    """Adapter-local filtered master-course selection state."""

    choices: tuple[MatchCourseChoice, ...]
    grade: MatchGrade
    timezone: DiscordTimezone
    stadium_id: int | None = None
    surface: MatchSurface | None = None
    distance: int | None = None
    direction: MatchDirection | None = None
    layout: StadiumCourseLayout | None = None

    def __post_init__(self) -> None:
        if not self.choices or any(not isinstance(choice, MatchCourseChoice) for choice in self.choices):
            raise ValueError("choices must contain at least one MatchCourseChoice.")
        if len({choice.id for choice in self.choices}) != len(self.choices):
            raise ValueError("choices must contain unique course IDs.")
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(self, "timezone", DiscordTimezone(self.timezone))
        if self.surface is not None:
            object.__setattr__(self, "surface", MatchSurface(self.surface))
        if self.direction is not None:
            object.__setattr__(self, "direction", MatchDirection(self.direction))
        if self.layout is not None:
            object.__setattr__(self, "layout", StadiumCourseLayout(self.layout))


def _filtered_choices(selection: MatchCourseSelection) -> tuple[MatchCourseChoice, ...]:
    choices = selection.choices
    if selection.stadium_id is not None:
        choices = tuple(choice for choice in choices if choice.stadium_id == selection.stadium_id)
    if selection.surface is not None:
        choices = tuple(choice for choice in choices if choice.surface == selection.surface)
    if selection.distance is not None:
        choices = tuple(choice for choice in choices if choice.distance == selection.distance)
    if selection.direction is not None:
        choices = tuple(choice for choice in choices if choice.direction == selection.direction)
    if selection.layout is not None:
        choices = tuple(choice for choice in choices if choice.layout == selection.layout)
    return choices


def normalize_course_selection(selection: MatchCourseSelection) -> MatchCourseSelection:
    """Auto-select single direction/layout values without inventing combinations."""

    if selection.stadium_id is None or selection.surface is None or selection.distance is None:
        return selection
    base = replace(selection, direction=None, layout=None)
    directions = tuple(dict.fromkeys(choice.direction for choice in _filtered_choices(base)))
    direction = selection.direction if selection.direction in directions else None
    if direction is None and len(directions) == 1:
        direction = directions[0]
    if direction is None:
        return replace(base, direction=None, layout=None)
    directed = replace(base, direction=direction)
    layouts = tuple(dict.fromkeys(choice.layout for choice in _filtered_choices(directed)))
    layout = selection.layout if selection.layout in layouts else None
    if layout is None and len(layouts) == 1:
        layout = layouts[0]
    return replace(directed, layout=layout)


def selected_course(selection: MatchCourseSelection) -> MatchCourseChoice | None:
    """Return the exact selected course only when all filtering axes resolve."""

    normalized = normalize_course_selection(selection)
    if normalized.direction is None or normalized.layout is None:
        return None
    choices = _filtered_choices(normalized)
    return choices[0] if len(choices) == 1 else None


def next_course_selection_step(selection: MatchCourseSelection) -> tuple[str, tuple[object, ...]]:
    normalized = normalize_course_selection(selection)
    if normalized.stadium_id is None:
        stadiums = tuple(dict.fromkeys((choice.stadium_id, choice.stadium_name) for choice in normalized.choices))
        return "stadium", stadiums
    if normalized.surface is None:
        surfaces = tuple(dict.fromkeys(choice.surface for choice in _filtered_choices(normalized)))
        return "surface", surfaces
    if normalized.distance is None:
        distances = tuple(dict.fromkeys(choice.distance for choice in _filtered_choices(normalized)))
        return "distance", distances
    if normalized.direction is None:
        directions = tuple(dict.fromkeys(choice.direction for choice in _filtered_choices(normalized)))
        return "direction", directions
    if normalized.layout is None:
        layouts = tuple(dict.fromkeys(choice.layout for choice in _filtered_choices(normalized)))
        return "layout", layouts
    if selected_course(normalized) is None:
        raise ValueError(COURSE_AMBIGUOUS)
    return "complete", ()


def update_course_selection(
    selection: MatchCourseSelection,
    *,
    step: str,
    value: str,
) -> MatchCourseSelection:
    """Apply one selection and discard every dependent downstream choice."""

    if step == "stadium":
        updated = replace(
            selection,
            stadium_id=int(value),
            surface=None,
            distance=None,
            direction=None,
            layout=None,
        )
    elif step == "surface":
        updated = replace(
            selection,
            surface=MatchSurface(value),
            distance=None,
            direction=None,
            layout=None,
        )
    elif step == "distance":
        updated = replace(selection, distance=int(value), direction=None, layout=None)
    elif step == "direction":
        updated = replace(selection, direction=MatchDirection(value), layout=None)
    elif step == "layout":
        updated = replace(selection, layout=StadiumCourseLayout(value))
    else:
        raise ValueError(UNKNOWN_SELECTION_STEP)
    if not _filtered_choices(updated):
        raise ValueError(COURSE_COMBINATION_UNAVAILABLE)
    return normalize_course_selection(updated)


def format_course_selection(selection: MatchCourseSelection) -> str:
    """Render current adapter-local course selection progress."""

    normalized = normalize_course_selection(selection)
    stadium = next(
        (choice.stadium_name for choice in normalized.choices if choice.stadium_id == normalized.stadium_id),
        None,
    )
    return format_course_selection_copy(
        grade=normalized.grade.value,
        timezone=normalized.timezone.value,
        stadium_name=stadium,
        surface=normalized.surface,
        distance=normalized.distance,
        direction=normalized.direction,
        layout=normalized.layout,
        course=selected_course(normalized),
    )


def course_selection_options(step: str, values: tuple[object, ...]) -> list[discord.SelectOption]:
    if not values:
        raise ValueError(COURSE_OPTIONS_EMPTY)
    if len(values) > _MAX_SELECT_OPTIONS:
        raise ValueError(COURSE_OPTIONS_OVERFLOW)
    if step == "stadium":
        return [
            discord.SelectOption(label=str(name)[:100], value=str(stadium_id))
            for stadium_id, name in values  # type: ignore[misc]
        ]
    if step == "surface":
        return [discord.SelectOption(label=surface_label(value), value=value.value) for value in values]  # type: ignore[arg-type,union-attr]
    if step == "distance":
        return [discord.SelectOption(label=f"{value}m", value=str(value)) for value in values]
    if step == "direction":
        return [discord.SelectOption(label=direction_label(value), value=value.value) for value in values]  # type: ignore[arg-type,union-attr]
    if step == "layout":
        return [discord.SelectOption(label=layout_label(value), value=value.value) for value in values]  # type: ignore[arg-type,union-attr]
    raise ValueError(UNKNOWN_SELECTION_STEP)


class MatchStaffBettingOpenHandler(Protocol):
    """Betting-open adapter surface consumed by the shared Match staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def preview_opening(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffBettingCloseHandler(Protocol):
    """Betting-close adapter surface consumed by the shared Match staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def preview_close(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffResultSubmissionHandler(Protocol):
    """Result-submission adapter surface consumed by the shared Match staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def start_submission(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str | None,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffResultReviewHandler(Protocol):
    """Result-review adapter surface consumed by the shared Match staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def start_review(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffResultConfirmationHandler(Protocol):
    """Result-confirmation adapter surface consumed by the shared Match staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def start_confirmation(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffCancellationHandler(Protocol):
    """Whole-Match cancellation surface consumed by the shared staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def preview_cancellation(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str | None,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffSettlementHandler(Protocol):
    """Atomic settlement surface consumed by the shared staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def preview_settlement(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str | None,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffSettlementRollbackHandler(Protocol):
    """Terminal settlement rollback surface consumed by the shared staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def preview_rollback(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        reason: str,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchStaffResultPublicationHandler(Protocol):
    """Explicit post-settlement publication surface consumed by the staff group."""

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def publish_result(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.LayoutView,
    ) -> None: ...


class MatchMemberBettingHandler(Protocol):
    """Member Bet adapter surface consumed by the Match root group."""

    async def list_races(self, interaction: discord.Interaction, *, match_id: int | None = None) -> None: ...

    async def autocomplete_races(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def list_personal_bets(self, interaction: discord.Interaction) -> None: ...

    async def autocomplete_targets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def autocomplete_amount(
        self,
        interaction: discord.Interaction,
        current: int | str,
    ) -> list[app_commands.Choice[int]]: ...

    async def place_bet(
        self,
        interaction: discord.Interaction,
        *,
        match_id: int,
        bet_type: str,
        numbers: str,
        amount: int,
    ) -> None: ...

    async def autocomplete_active_bets(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...

    async def replace_bet(
        self,
        interaction: discord.Interaction,
        *,
        bet_id: int,
        bet_type: str,
        numbers: str,
        amount: int,
    ) -> None: ...


class MatchRatingHandler(Protocol):
    """Current Rating standings adapter surface consumed by the Match root."""

    async def list_ratings(
        self,
        interaction: discord.Interaction,
        *,
        rank: int | None = None,
        persona: str | None = None,
    ) -> None: ...


class MatchCommandGroup(app_commands.Group):
    """V2 `/match` root containing member reads, Bet mutations, and staff actions."""

    def __init__(
        self,
        *,
        staff_group: app_commands.Group,
        betting_adapter: MatchMemberBettingHandler,
        rating_adapter: MatchRatingHandler,
    ) -> None:
        super().__init__(name="match", description=MATCH_ROOT_DESCRIPTION)
        self._betting_adapter = betting_adapter
        self._rating_adapter = rating_adapter
        self.add_command(staff_group)

    async def _race_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._betting_adapter.autocomplete_races(interaction, current)

    @app_commands.command(name="races", description=RACES_DESCRIPTION)
    @app_commands.rename(match_id=MATCH_ID_OPTION_NAME)
    @app_commands.describe(match_id=RACES_MATCH_DESCRIPTION)
    @app_commands.autocomplete(match_id=_race_autocomplete)
    async def races(self, interaction: discord.Interaction, match_id: int | None = None) -> None:
        await self._betting_adapter.list_races(interaction, match_id=match_id)

    @app_commands.command(name="ratings", description=RATINGS_DESCRIPTION)
    @app_commands.rename(rank=RANK_OPTION_NAME, persona=PERSONA_OPTION_NAME)
    @app_commands.describe(
        rank=RATINGS_RANK_DESCRIPTION,
        persona=RATINGS_PERSONA_DESCRIPTION,
    )
    async def ratings(
        self,
        interaction: discord.Interaction,
        rank: int | None = None,
        persona: str | None = None,
    ) -> None:
        await self._rating_adapter.list_ratings(
            interaction,
            rank=rank,
            persona=persona,
        )

    @app_commands.command(name="bets", description=BETS_DESCRIPTION)
    async def bets(self, interaction: discord.Interaction) -> None:
        await self._betting_adapter.list_personal_bets(interaction)

    async def _bet_match_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._betting_adapter.autocomplete_targets(interaction, current)

    async def _bet_amount_autocomplete(
        self,
        interaction: discord.Interaction,
        current: int | str,
    ) -> list[app_commands.Choice[int]]:
        return await self._betting_adapter.autocomplete_amount(interaction, current)

    @app_commands.command(name="bet", description=BET_DESCRIPTION)
    @app_commands.choices(
        bet_type=[
            app_commands.Choice(name=BET_TYPE_CHOICE_NAMES[0], value="win"),
            app_commands.Choice(name=BET_TYPE_CHOICE_NAMES[1], value="quinella"),
            app_commands.Choice(name=BET_TYPE_CHOICE_NAMES[2], value="trio"),
        ]
    )
    @app_commands.describe(
        match_id=BET_MATCH_DESCRIPTION,
        bet_type=BET_TYPE_DESCRIPTION,
        numbers=BET_NUMBERS_DESCRIPTION,
        amount=BET_AMOUNT_DESCRIPTION,
    )
    @app_commands.rename(
        match_id=MATCH_ID_OPTION_NAME,
        bet_type=BET_TYPE_OPTION_NAME,
        numbers=NUMBERS_OPTION_NAME,
        amount=AMOUNT_OPTION_NAME,
    )
    @app_commands.autocomplete(
        match_id=_bet_match_autocomplete,
        amount=_bet_amount_autocomplete,
    )
    async def bet(
        self,
        interaction: discord.Interaction,
        match_id: int,
        bet_type: app_commands.Choice[str],
        numbers: str,
        amount: int,
    ) -> None:
        await self._betting_adapter.place_bet(
            interaction,
            match_id=match_id,
            bet_type=bet_type.value,
            numbers=numbers,
            amount=amount,
        )

    async def _bet_change_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._betting_adapter.autocomplete_active_bets(interaction, current)

    @app_commands.command(name="bet-change", description=BET_CHANGE_DESCRIPTION)
    @app_commands.choices(
        bet_type=[
            app_commands.Choice(name=BET_TYPE_CHOICE_NAMES[0], value="win"),
            app_commands.Choice(name=BET_TYPE_CHOICE_NAMES[1], value="quinella"),
            app_commands.Choice(name=BET_TYPE_CHOICE_NAMES[2], value="trio"),
        ]
    )
    @app_commands.describe(
        bet_id=BET_CHANGE_TARGET_DESCRIPTION,
        bet_type=BET_CHANGE_TYPE_DESCRIPTION,
        numbers=BET_CHANGE_NUMBERS_DESCRIPTION,
        amount=BET_CHANGE_AMOUNT_DESCRIPTION,
    )
    @app_commands.rename(
        bet_id=BET_ID_OPTION_NAME,
        bet_type=BET_TYPE_OPTION_NAME,
        numbers=NUMBERS_OPTION_NAME,
        amount=AMOUNT_OPTION_NAME,
    )
    @app_commands.autocomplete(bet_id=_bet_change_autocomplete)
    async def bet_change(
        self,
        interaction: discord.Interaction,
        bet_id: int,
        bet_type: app_commands.Choice[str],
        numbers: str,
        amount: int,
    ) -> None:
        await self._betting_adapter.replace_bet(
            interaction,
            bet_id=bet_id,
            bet_type=bet_type.value,
            numbers=numbers,
            amount=amount,
        )
