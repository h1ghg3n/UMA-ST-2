"""Discord adapter-local editor for configured native Match creation and setup edits."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

import discord

from uma_st2.application.match import (
    CreateMatch,
    MatchConditionValues,
    MatchCourseChoice,
    MatchCreationAuditError,
    MatchCreationCommands,
    MatchCreationError,
    MatchCreationIdempotencyConflictError,
    MatchCreationUnavailableError,
    MatchSetupAuditError,
    MatchSetupCommands,
    MatchSetupEditorTarget,
    MatchSetupError,
    MatchSetupIdempotencyConflictError,
    MatchSetupNoChangeError,
    MatchSetupReasonRequiredError,
    MatchSetupStaleError,
    MatchSetupTarget,
    MatchSetupUnavailableError,
    MatchStaffCreationQueries,
    MatchStaffSetupQueries,
    MatchStaffSetupQueryError,
    UpdateMatchSetup,
)
from uma_st2.domain.match import (
    MatchGrade,
    MatchSeason,
    MatchTimeOfDay,
    MatchTrackCondition,
    MatchWeather,
)

from .common import (
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    bounded_discord_message,
    correlation_id,
    run_blocking_application,
)
from .datetime_codec import DiscordTimezone, format_discord_datetime, parse_operator_date_and_time
from .match_staff import (
    MatchCourseSelection,
    MatchStaffInteractionContext,
    course_selection_options,
    format_course_selection,
    next_course_selection_step,
    normalize_course_selection,
    selected_course,
    update_course_selection,
)
from .strings import match_staff_workflows as copy

logger = logging.getLogger(__name__)

_COMMAND_NAME = "match.staff.race"
_COMPONENT_TIMEOUT_SECONDS = 600.0
_MAX_SELECT_OPTIONS = 25


class MatchSetupMode(StrEnum):
    """Adapter-only editor mode."""

    CREATE = "create"
    EDIT = "edit"


def _required_snowflake(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} is unavailable.")
    return value


def _optional_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"입력은 {max_length}자 이하여야 합니다.")
    return normalized


def _terminal_layout(content: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(content)))
    return view


@dataclass(frozen=True, slots=True)
class MatchSetupDraft:
    """Complete or partial adapter-local state; never canonical authority."""

    mode: MatchSetupMode
    course_choices: tuple[MatchCourseChoice, ...]
    target: MatchSetupEditorTarget | None = None
    name: str | None = None
    description: str | None = None
    grade: MatchGrade = MatchGrade.G1
    timezone: DiscordTimezone = DiscordTimezone.KST
    scheduled_at: datetime | None = None
    course: MatchCourseChoice | None = None
    condition: MatchConditionValues | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", MatchSetupMode(self.mode))
        if not self.course_choices or any(not isinstance(choice, MatchCourseChoice) for choice in self.course_choices):
            raise ValueError("course_choices must contain current MatchCourseChoice values.")
        if self.mode == MatchSetupMode.CREATE and self.target is not None:
            raise ValueError("A create draft cannot own an existing target.")
        if self.mode == MatchSetupMode.EDIT and self.target is None:
            raise ValueError("An edit draft requires an existing target.")
        if self.name is not None:
            normalized_name = self.name.strip()
            if not normalized_name or len(normalized_name) > 200:
                raise ValueError("제목은 1자 이상 200자 이하여야 합니다.")
            object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "description", _optional_text(self.description, max_length=4000))
        object.__setattr__(self, "reason", _optional_text(self.reason, max_length=255))
        object.__setattr__(self, "grade", MatchGrade(self.grade))
        object.__setattr__(self, "timezone", DiscordTimezone(self.timezone))
        if self.scheduled_at is not None and (
            not isinstance(self.scheduled_at, datetime) or self.scheduled_at.tzinfo is None
        ):
            raise ValueError("scheduled_at must be an aware datetime or null.")
        if self.course is not None and self.course not in self.course_choices:
            raise ValueError("course must be one current course choice.")
        if self.condition is not None and not isinstance(self.condition, MatchConditionValues):
            raise ValueError("condition must be MatchConditionValues or null.")

    @property
    def entry_count(self) -> int:
        return 0 if self.target is None else self.target.entry_count

    @property
    def complete(self) -> bool:
        return (
            self.name is not None
            and self.scheduled_at is not None
            and self.course is not None
            and self.condition is not None
            and (self.mode == MatchSetupMode.CREATE or self.reason is not None)
        )

    @property
    def current_setup(self) -> MatchSetupTarget | None:
        return None if self.target is None else self.target.setup

    def summary(self) -> str:
        return copy.format_setup_summary(
            mode=self.mode.value,
            name=self.name,
            grade=self.grade,
            scheduled_at=self.scheduled_at,
            timezone=self.timezone,
            description=self.description,
            course=self.course,
            condition=self.condition,
            entry_count=self.entry_count,
            reason=self.reason,
        )


@dataclass(frozen=True, slots=True)
class MatchConditionSelection:
    """Partial condition subview state."""

    season: MatchSeason | None = None
    weather: MatchWeather | None = None
    time_of_day: MatchTimeOfDay | None = None
    track_condition: MatchTrackCondition | None = None

    @classmethod
    def from_values(cls, values: MatchConditionValues | None) -> MatchConditionSelection:
        if values is None:
            return cls()
        return cls(
            season=values.season,
            weather=values.weather,
            time_of_day=values.time_of_day,
            track_condition=values.track_condition,
        )

    def to_values(self) -> MatchConditionValues | None:
        if None in (self.season, self.weather, self.time_of_day, self.track_condition):
            return None
        return MatchConditionValues(
            season=self.season,
            weather=self.weather,
            time_of_day=self.time_of_day,
            track_condition=self.track_condition,
        )


class MatchSetupMainButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._action = action
        super().__init__(
            label=label,
            style=style,
            disabled=disabled,
            custom_id=f"match-setup-main-{action}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.handle_main_action(
            interaction,
            context=self._context,
            draft=self._draft,
            action=self._action,
            source_view=self.view,
        )


class MatchSetupEditorView(discord.ui.LayoutView):
    """Main detached setup editor shared by create and edit."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self.draft = draft
        container = discord.ui.Container(discord.ui.TextDisplay(draft.summary()))
        editor_actions = discord.ui.ActionRow()
        for action, label in (
            ("basic", copy.SETUP_BASIC_LABEL),
            ("course", copy.SETUP_COURSE_LABEL),
            ("condition", copy.SETUP_CONDITION_LABEL),
        ):
            editor_actions.add_item(
                MatchSetupMainButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    action=action,
                    label=label,
                )
            )
        final_actions = discord.ui.ActionRow()
        final_actions.add_item(
            MatchSetupMainButton(
                adapter=adapter,
                context=context,
                draft=draft,
                action="save",
                label=copy.SETUP_SAVE_LABEL,
                style=discord.ButtonStyle.success,
                disabled=not draft.complete,
            )
        )
        final_actions.add_item(
            MatchSetupMainButton(
                adapter=adapter,
                context=context,
                draft=draft,
                action="cancel",
                label=copy.SETUP_CANCEL_LABEL,
                style=discord.ButtonStyle.danger,
            )
        )
        container.add_item(editor_actions)
        container.add_item(final_actions)
        self.add_item(container)


class MatchSetupBasicSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        field: str,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._field = field
        if field == "grade":
            options = [
                discord.SelectOption(label=value.value, value=value.value, default=value == draft.grade)
                for value in MatchGrade
            ]
            placeholder = copy.GRADE_PLACEHOLDER
        else:
            options = [
                discord.SelectOption(label=value.value, value=value.value, default=value == draft.timezone)
                for value in DiscordTimezone
            ]
            placeholder = copy.TIMEZONE_PLACEHOLDER
        super().__init__(
            custom_id=f"match-setup-basic-{field}",
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.update_basic_choice(
            interaction,
            context=self._context,
            draft=self._draft,
            field=self._field,
            value=self.values[0],
            source_view=self.view,
        )


class MatchSetupBasicButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        action: str,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._action = action
        super().__init__(
            label=copy.SETUP_TEXT_LABEL if action == "text" else copy.SETUP_BACK_LABEL,
            style=discord.ButtonStyle.primary if action == "text" else discord.ButtonStyle.secondary,
            custom_id=f"match-setup-basic-{action}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._action == "text":
            await self._adapter.open_basic_modal(
                interaction,
                context=self._context,
                draft=self._draft,
                source_view=self.view,
            )
        else:
            await self._adapter.return_to_editor(
                interaction,
                context=self._context,
                draft=self._draft,
                source_view=self.view,
            )


class MatchSetupBasicView(discord.ui.LayoutView):
    """Selection-valued basic fields plus a TextInput-only metadata Modal."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(
            discord.ui.TextDisplay("## 기본 정보\nGrade와 timezone을 고른 뒤 텍스트·일정을 입력하세요.")
        )
        grade_row = discord.ui.ActionRow()
        grade_row.add_item(MatchSetupBasicSelect(adapter=adapter, context=context, draft=draft, field="grade"))
        timezone_row = discord.ui.ActionRow()
        timezone_row.add_item(MatchSetupBasicSelect(adapter=adapter, context=context, draft=draft, field="timezone"))
        action_row = discord.ui.ActionRow()
        action_row.add_item(MatchSetupBasicButton(adapter=adapter, context=context, draft=draft, action="text"))
        action_row.add_item(MatchSetupBasicButton(adapter=adapter, context=context, draft=draft, action="back"))
        container.add_item(grade_row)
        container.add_item(timezone_row)
        container.add_item(action_row)
        self.add_item(container)


class MatchSetupBasicModal(discord.ui.Modal):
    """Direct-string setup fields; submit changes only adapter-local draft."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        super().__init__(
            title=(
                copy.BASIC_MODAL_CREATE_TITLE if draft.mode == MatchSetupMode.CREATE else copy.BASIC_MODAL_EDIT_TITLE
            ),
            timeout=_COMPONENT_TIMEOUT_SECONDS,
        )
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._source_view = source_view
        formatted = None
        if getattr(draft.scheduled_at, "tzinfo", None) is not None:
            formatted = format_discord_datetime(draft.scheduled_at, draft.timezone).split()
        self.name = discord.ui.TextInput(
            label=copy.NAME_LABEL,
            default=draft.name,
            min_length=1,
            max_length=200,
        )
        self.date = discord.ui.TextInput(
            label=copy.DATE_LABEL,
            placeholder="YYYY-MM-DD",
            default=None if formatted is None else formatted[0],
            min_length=10,
            max_length=10,
        )
        self.time = discord.ui.TextInput(
            label=copy.TIME_LABEL,
            placeholder="HH:MM",
            default=None if formatted is None else formatted[1],
            min_length=5,
            max_length=5,
        )
        self.description = discord.ui.TextInput(
            label=copy.DESCRIPTION_LABEL,
            style=discord.TextStyle.paragraph,
            required=False,
            default=draft.description,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            label=copy.REASON_LABEL,
            style=discord.TextStyle.paragraph,
            required=draft.mode == MatchSetupMode.EDIT,
            default=draft.reason,
            max_length=255,
        )
        for item in (self.name, self.date, self.time, self.description, self.reason):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.apply_basic_text(
            interaction,
            context=self._context,
            draft=self._draft,
            name=str(self.name.value),
            date_value=str(self.date.value),
            time_value=str(self.time.value),
            description=str(self.description.value),
            reason=str(self.reason.value),
            source_view=self._source_view,
        )


class MatchSetupCourseSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchCourseSelection,
        step: str,
        values: tuple[object, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._selection = selection
        self._step = step
        super().__init__(
            custom_id=f"match-setup-course-{step}",
            min_values=1,
            max_values=1,
            options=course_selection_options(step, values),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.update_course_choice(
            interaction,
            context=self._context,
            draft=self._draft,
            selection=self._selection,
            step=self._step,
            value=self.values[0],
            source_view=self.view,
        )


class MatchSetupCourseButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchCourseSelection,
        action: str,
        page: int = 0,
        disabled: bool = False,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._selection = selection
        self._action = action
        self._page = page
        labels = {
            "apply": copy.SETUP_APPLY_LABEL,
            "reset": copy.SETUP_RESET_COURSE_LABEL,
            "back": copy.SETUP_BACK_LABEL,
            "previous": "이전",
            "next": "다음",
        }
        super().__init__(
            label=labels[action],
            style=discord.ButtonStyle.success if action == "apply" else discord.ButtonStyle.secondary,
            custom_id=f"match-setup-course-{action}-{page}",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.handle_course_action(
            interaction,
            context=self._context,
            draft=self._draft,
            selection=self._selection,
            action=self._action,
            page=self._page,
            source_view=self.view,
        )


class MatchSetupCourseView(discord.ui.LayoutView):
    """Existing current-course filter embedded as one setup subview."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchCourseSelection,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        selection = normalize_course_selection(selection)
        container = discord.ui.Container(discord.ui.TextDisplay(format_course_selection(selection)))
        step, values = next_course_selection_step(selection)
        if step == "complete":
            row = discord.ui.ActionRow()
            row.add_item(
                MatchSetupCourseButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    selection=selection,
                    action="apply",
                )
            )
            row.add_item(
                MatchSetupCourseButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    selection=selection,
                    action="reset",
                )
            )
            row.add_item(
                MatchSetupCourseButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    selection=selection,
                    action="back",
                )
            )
            container.add_item(row)
        else:
            page_count = max(1, (len(values) + _MAX_SELECT_OPTIONS - 1) // _MAX_SELECT_OPTIONS)
            page = min(max(page, 0), page_count - 1)
            start = page * _MAX_SELECT_OPTIONS
            select_row = discord.ui.ActionRow()
            select_row.add_item(
                MatchSetupCourseSelect(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    selection=selection,
                    step=step,
                    values=values[start : start + _MAX_SELECT_OPTIONS],
                )
            )
            container.add_item(select_row)
            actions = discord.ui.ActionRow()
            if page_count > 1:
                actions.add_item(
                    MatchSetupCourseButton(
                        adapter=adapter,
                        context=context,
                        draft=draft,
                        selection=selection,
                        action="previous",
                        page=page - 1,
                        disabled=page == 0,
                    )
                )
                actions.add_item(
                    MatchSetupCourseButton(
                        adapter=adapter,
                        context=context,
                        draft=draft,
                        selection=selection,
                        action="next",
                        page=page + 1,
                        disabled=page + 1 >= page_count,
                    )
                )
            if selection.stadium_id is not None:
                actions.add_item(
                    MatchSetupCourseButton(
                        adapter=adapter,
                        context=context,
                        draft=draft,
                        selection=selection,
                        action="reset",
                    )
                )
            actions.add_item(
                MatchSetupCourseButton(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    selection=selection,
                    action="back",
                )
            )
            container.add_item(actions)
        self.add_item(container)


class MatchSetupConditionSelect(discord.ui.Select[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchConditionSelection,
        field: str,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._selection = selection
        self._field = field
        catalog: dict[str, tuple[type[StrEnum], str]] = {
            "season": (MatchSeason, copy.SEASON_PLACEHOLDER),
            "weather": (MatchWeather, copy.WEATHER_PLACEHOLDER),
            "time_of_day": (MatchTimeOfDay, copy.TIME_OF_DAY_PLACEHOLDER),
            "track_condition": (MatchTrackCondition, copy.TRACK_PLACEHOLDER),
        }
        enum_type, placeholder = catalog[field]
        current = getattr(selection, field)
        options = [
            discord.SelectOption(
                label=copy.RANDOM_CONDITION_LABEL if value.value == "random" else value.value,
                value=value.value,
                default=value == current,
            )
            for value in enum_type
        ]
        super().__init__(
            custom_id=f"match-setup-condition-{field}",
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.update_condition_choice(
            interaction,
            context=self._context,
            draft=self._draft,
            selection=self._selection,
            field=self._field,
            value=self.values[0],
            source_view=self.view,
        )


class MatchSetupConditionButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchConditionSelection,
        action: str,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._selection = selection
        self._action = action
        super().__init__(
            label=copy.SETUP_APPLY_LABEL if action == "apply" else copy.SETUP_BACK_LABEL,
            style=discord.ButtonStyle.success if action == "apply" else discord.ButtonStyle.secondary,
            custom_id=f"match-setup-condition-{action}",
            disabled=action == "apply" and selection.to_values() is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._action == "apply":
            await self._adapter.apply_condition(
                interaction,
                context=self._context,
                draft=self._draft,
                selection=self._selection,
                source_view=self.view,
            )
        else:
            await self._adapter.return_to_editor(
                interaction,
                context=self._context,
                draft=self._draft,
                source_view=self.view,
            )


class MatchSetupConditionView(discord.ui.LayoutView):
    """Four explicit enum selections with local apply/back actions."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchConditionSelection,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay("## 환경 조건\n네 값을 모두 선택한 뒤 적용하세요."))
        for field in ("season", "weather", "time_of_day", "track_condition"):
            row = discord.ui.ActionRow()
            row.add_item(
                MatchSetupConditionSelect(
                    adapter=adapter,
                    context=context,
                    draft=draft,
                    selection=selection,
                    field=field,
                )
            )
            container.add_item(row)
        actions = discord.ui.ActionRow()
        actions.add_item(
            MatchSetupConditionButton(
                adapter=adapter,
                context=context,
                draft=draft,
                selection=selection,
                action="apply",
            )
        )
        actions.add_item(
            MatchSetupConditionButton(
                adapter=adapter,
                context=context,
                draft=draft,
                selection=selection,
                action="back",
            )
        )
        container.add_item(actions)
        self.add_item(container)


class MatchSetupConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        confirm: bool,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._draft = draft
        self._confirm = confirm
        super().__init__(
            label=copy.SETUP_CONFIRM_LABEL if confirm else copy.SETUP_CANCEL_LABEL,
            style=discord.ButtonStyle.success if confirm else discord.ButtonStyle.secondary,
            custom_id=f"match-setup-final-{'confirm' if confirm else 'cancel'}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._confirm:
            await self._adapter.confirm_setup(
                interaction,
                context=self._context,
                draft=self._draft,
                source_view=self.view,
            )
        else:
            await self._adapter.return_to_editor(
                interaction,
                context=self._context,
                draft=self._draft,
                source_view=self.view,
            )


class MatchSetupConfirmView(discord.ui.LayoutView):
    """Complete before/after preview; only Confirm executes a mutation."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._confirmation_started = False
        content = copy.format_setup_confirmation(before=draft.current_setup, desired=draft.summary())
        container = discord.ui.Container(discord.ui.TextDisplay(content))
        actions = discord.ui.ActionRow()
        actions.add_item(MatchSetupConfirmButton(adapter=adapter, context=context, draft=draft, confirm=True))
        actions.add_item(MatchSetupConfirmButton(adapter=adapter, context=context, draft=draft, confirm=False))
        container.add_item(actions)
        self.add_item(container)

    def claim_confirmation(self) -> bool:
        if self._confirmation_started:
            return False
        self._confirmation_started = True
        return True


class MatchSetupTargetSelect(discord.ui.Select[discord.ui.LayoutView]):
    """Select one current native scheduled setup target."""

    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        targets: tuple[MatchSetupEditorTarget, ...],
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._targets = {target.setup.match_id: target for target in targets}
        options = [
            discord.SelectOption(
                label=bounded_discord_message(
                    (f"{target.setup.name} · {format_discord_datetime(target.setup.scheduled_at)}",),
                    limit=100,
                ),
                value=str(target.setup.match_id),
                description=f"{target.setup.grade.value} · Entry {target.entry_count}명",
            )
            for target in targets
        ]
        super().__init__(
            custom_id="match-setup-edit-target",
            placeholder=copy.TARGET_PLACEHOLDER,
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            match_id = int(self.values[0])
        except (IndexError, TypeError, ValueError):
            await self._adapter.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        await self._adapter.start_edit(
            interaction,
            context=self._context,
            match_id=match_id,
            source_view=self.view,
        )


class MatchSetupTargetView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: MatchSetupDiscordAdapter,
        context: MatchStaffInteractionContext,
        targets: tuple[MatchSetupEditorTarget, ...],
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay("## 수정할 룸매치 선택"))
        row = discord.ui.ActionRow()
        row.add_item(MatchSetupTargetSelect(adapter=adapter, context=context, targets=targets))
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class MatchSetupDiscordAdapter:
    """Translate the shared setup editor into narrow query/command calls."""

    creation_queries: MatchStaffCreationQueries
    setup_queries: MatchStaffSetupQueries
    creation_commands: MatchCreationCommands
    setup_commands: MatchSetupCommands
    authorize_interaction: AuthorizeDiscordInteraction
    blocking_runner: BlockingApplicationRunner = run_blocking_application

    async def start_creation(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.View | None,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if not await self._defer_message_update(interaction, response_kind="setup-create-open"):
            return
        try:
            choices = await self.blocking_runner(lambda: self.creation_queries.list_course_choices())
            draft = MatchSetupDraft(mode=MatchSetupMode.CREATE, course_choices=choices)
            view = MatchSetupEditorView(adapter=self, context=context, draft=draft)
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-create-open",
        )

    async def show_edit_targets(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        source_view: discord.ui.View | None,
    ) -> None:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return
        if not await self._defer_message_update(interaction, response_kind="setup-target-list"):
            return
        try:
            targets = await self.blocking_runner(lambda: self.setup_queries.search_targets(limit=25))
            if not targets:
                await self.send_component_error(interaction, copy.NO_TARGETS)
                return
            view = MatchSetupTargetView(adapter=self, context=context, targets=targets)
        except MatchStaffSetupQueryError:
            await self.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(interaction, view=view, response_kind="setup-target-list")

    async def start_edit(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        match_id: int,
        source_view: discord.ui.View | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-edit-open",
        ):
            return
        try:
            target, choices = await self.blocking_runner(
                lambda: (
                    self.setup_queries.get_target(match_id=match_id),
                    self.creation_queries.list_course_choices(),
                )
            )
            if target is None:
                raise MatchSetupUnavailableError(copy.SETUP_UNAVAILABLE)
            course = next((choice for choice in choices if choice.id == target.setup.course.id), None)
            if course is None:
                raise MatchSetupUnavailableError(copy.SETUP_UNAVAILABLE)
            draft = MatchSetupDraft(
                mode=MatchSetupMode.EDIT,
                course_choices=choices,
                target=target,
                name=target.setup.name,
                description=target.setup.description,
                grade=target.setup.grade,
                timezone=DiscordTimezone.KST,
                scheduled_at=target.setup.scheduled_at,
                course=course,
                condition=None if target.setup.condition is None else target.setup.condition.values,
            )
            view = MatchSetupEditorView(adapter=self, context=context, draft=draft)
        except (MatchStaffSetupQueryError, MatchSetupError, TypeError, ValueError):
            await self.send_component_error(interaction, copy.SETUP_UNAVAILABLE)
            return
        except Exception:
            self._log_application_failure(interaction)
            await self.send_component_error(interaction, copy.TARGET_LOAD_ERROR)
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-edit-open",
        )

    async def handle_main_action(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        action: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind=f"setup-main-{action}",
        ):
            return
        if action == "basic":
            view: discord.ui.LayoutView = MatchSetupBasicView(adapter=self, context=context, draft=draft)
        elif action == "course":
            selection = MatchCourseSelection(
                choices=draft.course_choices,
                grade=draft.grade,
                timezone=draft.timezone,
                stadium_id=None if draft.course is None else draft.course.stadium_id,
                surface=None if draft.course is None else draft.course.surface,
                distance=None if draft.course is None else draft.course.distance,
                direction=None if draft.course is None else draft.course.direction,
                layout=None if draft.course is None else draft.course.layout,
            )
            view = MatchSetupCourseView(
                adapter=self,
                context=context,
                draft=draft,
                selection=selection,
            )
        elif action == "condition":
            view = MatchSetupConditionView(
                adapter=self,
                context=context,
                draft=draft,
                selection=MatchConditionSelection.from_values(draft.condition),
            )
        elif action == "save":
            if not draft.complete:
                await self.send_component_error(
                    interaction,
                    copy.EDIT_REASON_REQUIRED if draft.mode == MatchSetupMode.EDIT else copy.INCOMPLETE_SETUP,
                )
                return
            view = MatchSetupConfirmView(adapter=self, context=context, draft=draft)
        elif action == "cancel":
            view = _terminal_layout(copy.SETUP_CANCELLED)
            if source_view is not None:
                source_view.stop()
            await self._edit_layout(
                interaction,
                view=view,
                response_kind="setup-main-cancel",
            )
            return
        else:
            await self.send_component_error(interaction, copy.INVALID_ACTION)
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind=f"setup-main-{action}",
        )

    async def update_basic_choice(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        field: str,
        value: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-basic-choice",
        ):
            return
        updated = replace(
            draft,
            **({"grade": MatchGrade(value)} if field == "grade" else {"timezone": DiscordTimezone(value)}),
        )
        view = MatchSetupBasicView(adapter=self, context=context, draft=updated)
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-basic-choice",
        )

    async def open_basic_modal(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._authorize_bound(interaction, context=context):
            return
        try:
            await interaction.response.send_modal(
                MatchSetupBasicModal(
                    adapter=self,
                    context=context,
                    draft=draft,
                    source_view=source_view,
                )
            )
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=setup-basic-modal error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                type(error).__name__,
            )

    async def apply_basic_text(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        name: str,
        date_value: str,
        time_value: str,
        description: str,
        reason: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-basic-submit",
        ):
            return
        try:
            updated = replace(
                draft,
                name=name,
                scheduled_at=parse_operator_date_and_time(date_value, time_value, draft.timezone),
                description=description,
                reason=reason,
            )
        except (TypeError, ValueError) as error:
            await self.send_component_error(interaction, str(error))
            return
        view = MatchSetupEditorView(adapter=self, context=context, draft=updated)
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-basic-submit",
        )

    async def update_course_choice(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchCourseSelection,
        step: str,
        value: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-course-choice",
        ):
            return
        try:
            updated = update_course_selection(selection, step=step, value=value)
            view = MatchSetupCourseView(
                adapter=self,
                context=context,
                draft=draft,
                selection=updated,
            )
        except (TypeError, ValueError) as error:
            await self.send_component_error(interaction, str(error))
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-course-choice",
        )

    async def handle_course_action(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchCourseSelection,
        action: str,
        page: int,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind=f"setup-course-{action}",
        ):
            return
        if action == "apply":
            course = selected_course(selection)
            if course is None:
                await self.send_component_error(interaction, copy.INCOMPLETE_SETUP)
                return
            view: discord.ui.LayoutView = MatchSetupEditorView(
                adapter=self,
                context=context,
                draft=replace(draft, course=course),
            )
        elif action == "reset":
            view = MatchSetupCourseView(
                adapter=self,
                context=context,
                draft=draft,
                selection=MatchCourseSelection(
                    choices=draft.course_choices,
                    grade=draft.grade,
                    timezone=draft.timezone,
                ),
            )
        elif action == "back":
            view = MatchSetupEditorView(adapter=self, context=context, draft=draft)
        elif action in {"previous", "next"}:
            view = MatchSetupCourseView(
                adapter=self,
                context=context,
                draft=draft,
                selection=selection,
                page=page,
            )
        else:
            await self.send_component_error(interaction, copy.INVALID_ACTION)
            return
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind=f"setup-course-{action}",
        )

    async def update_condition_choice(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchConditionSelection,
        field: str,
        value: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-condition-choice",
        ):
            return
        enum_types = {
            "season": MatchSeason,
            "weather": MatchWeather,
            "time_of_day": MatchTimeOfDay,
            "track_condition": MatchTrackCondition,
        }
        try:
            updated = replace(selection, **{field: enum_types[field](value)})
        except (KeyError, TypeError, ValueError):
            await self.send_component_error(interaction, copy.INVALID_ACTION)
            return
        view = MatchSetupConditionView(
            adapter=self,
            context=context,
            draft=draft,
            selection=updated,
        )
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-condition-choice",
        )

    async def apply_condition(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        selection: MatchConditionSelection,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-condition-apply",
        ):
            return
        values = selection.to_values()
        if values is None:
            await self.send_component_error(interaction, copy.INCOMPLETE_SETUP)
            return
        view = MatchSetupEditorView(
            adapter=self,
            context=context,
            draft=replace(draft, condition=values),
        )
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-condition-apply",
        )

    async def return_to_editor(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-editor-return",
        ):
            return
        view = MatchSetupEditorView(adapter=self, context=context, draft=draft)
        if source_view is not None:
            source_view.stop()
        await self._edit_layout(
            interaction,
            view=view,
            response_kind="setup-editor-return",
        )

    async def confirm_setup(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        draft: MatchSetupDraft,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        if not await self._prepare_bound_update(
            interaction,
            context=context,
            response_kind="setup-confirm",
        ):
            return
        if not isinstance(source_view, MatchSetupConfirmView) or not source_view.claim_confirmation():
            await self.send_component_error(interaction, copy.SETUP_CONFIRMATION_STARTED)
            return
        try:
            if not draft.complete or draft.name is None or draft.course is None or draft.condition is None:
                raise ValueError(copy.INCOMPLETE_SETUP)
            interaction_id = _required_snowflake(getattr(interaction, "id", None), field_name="interaction ID")
            actor_id = _required_snowflake(
                getattr(getattr(interaction, "user", None), "id", None),
                field_name="user ID",
            )
            if draft.mode == MatchSetupMode.CREATE:
                result = await self.blocking_runner(
                    lambda: self.creation_commands.create_match(
                        CreateMatch(
                            name=draft.name,
                            description=draft.description,
                            grade=draft.grade,
                            stadium_course_id=draft.course.id,
                            scheduled_at=draft.scheduled_at,
                            condition=draft.condition,
                            idempotency_key=f"match-create:{interaction_id}",
                            actor_discord_user_id=str(actor_id),
                            guild_id=str(context.guild_id),
                            correlation_id=str(interaction_id),
                            reason=draft.reason,
                        )
                    )
                )
            else:
                target = draft.current_setup
                if target is None:
                    raise MatchSetupUnavailableError(copy.SETUP_UNAVAILABLE)
                result = await self.blocking_runner(
                    lambda: self.setup_commands.update_setup(
                        UpdateMatchSetup(
                            match_id=target.match_id,
                            name=draft.name,
                            description=draft.description,
                            grade=draft.grade,
                            stadium_course_id=draft.course.id,
                            scheduled_at=draft.scheduled_at,
                            condition=draft.condition,
                            expected_state_fingerprint=target.state_fingerprint,
                            expected_setup_version=target.setup_version,
                            idempotency_key=f"match-setup:{interaction_id}",
                            actor_discord_user_id=str(actor_id),
                            guild_id=str(context.guild_id),
                            correlation_id=str(interaction_id),
                            reason=draft.reason,
                        )
                    )
                )
        except (MatchCreationIdempotencyConflictError, MatchSetupIdempotencyConflictError):
            message = copy.SETUP_IDEMPOTENCY_CONFLICT
        except MatchSetupStaleError:
            message = copy.SETUP_STALE
        except (MatchCreationUnavailableError, MatchSetupUnavailableError):
            message = copy.SETUP_UNAVAILABLE
        except MatchSetupNoChangeError:
            message = copy.NO_CHANGE
        except MatchSetupReasonRequiredError:
            message = copy.EDIT_REASON_REQUIRED
        except (
            MatchCreationAuditError,
            MatchSetupAuditError,
            MatchCreationError,
            MatchSetupError,
            TypeError,
            ValueError,
        ) as error:
            message = str(error)
        except Exception:
            self._log_application_failure(interaction)
            message = f"룸매치 설정을 저장하지 못했습니다. 참조 ID: `{correlation_id(interaction)}`"
        else:
            message = copy.format_setup_success(result)
        terminal = _terminal_layout(message)
        source_view.stop()
        await self._edit_layout(
            interaction,
            view=terminal,
            response_kind="setup-confirm",
        )

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    async def _prepare_bound_update(
        self,
        interaction: discord.Interaction,
        *,
        context: MatchStaffInteractionContext,
        response_kind: str,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR)
            return False
        if not await self._defer_message_update(interaction, response_kind=response_kind):
            return False
        return await self.authorize_interaction(interaction, _COMMAND_NAME)

    @staticmethod
    async def _defer_message_update(
        interaction: discord.Interaction,
        *,
        response_kind: str,
    ) -> bool:
        try:
            await interaction.response.defer(thinking=False)
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=%s-defer error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            return False
        return True

    @staticmethod
    async def _edit_layout(
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        response_kind: str,
    ) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception as error:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=%s error_type=%s",
                correlation_id(interaction),
                _COMMAND_NAME,
                response_kind,
                type(error).__name__,
            )
            await MatchSetupDiscordAdapter.send_component_error(interaction, copy.RACE_TRANSITION_ERROR)

    @staticmethod
    async def send_component_error(interaction: discord.Interaction, content: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            logger.error(
                "Discord response failed correlation_id=%s command=%s response_kind=component-error",
                correlation_id(interaction),
                _COMMAND_NAME,
            )

    @staticmethod
    def _log_application_failure(interaction: object) -> None:
        logger.error(
            "Discord application call failed correlation_id=%s command=%s",
            correlation_id(interaction),
            _COMMAND_NAME,
        )
