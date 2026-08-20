from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import discord

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.adapters.discord.match_staff_entries import (
    MatchStaffEntryAdapter,
    MatchStaffEntryAdapterPorts,
)
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.autocomplete_queries import (
    AutocompleteChoice,
    RoomRaceChoicePurpose,
)
from umacircle_bot.services.dtos import RaceOperationResultDTO
from umacircle_bot.services.match_entry_resolution import (
    execute_match_race_resolved_entries_replacement,
    parse_match_entry_draft_text,
    query_match_entry_game_account_candidates,
)
from umacircle_bot.services.match_lifecycle_application import (
    MatchRaceBettingBatchCommand,
    MatchRaceConditionCommand,
    MatchRaceCreateCommand,
    MatchRaceEditCommand,
    MatchRaceEntriesCommand,
)
from umacircle_bot.services.match_races import RegisteredFinalEntrySnapshotInput


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class PrepareBoundComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendCommandErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> None: ...


class SendInitialResponsePort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
    ) -> bool: ...


class SendRaceResultPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        result: RaceOperationResultDTO,
    ) -> None: ...


class SendRaceBatchResultPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        results: tuple[RaceOperationResultDTO, ...],
    ) -> None: ...


class QueryMatchRacePort(Protocol):
    def __call__(self, *, race_id: int) -> RaceOperationResultDTO: ...


@dataclass(frozen=True, slots=True)
class MatchStaffLifecycleAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_bound_component: PrepareBoundComponentPort
    build_context: Callable[[discord.Interaction], InteractionContext]
    query_race_choices: Callable[[RoomRaceChoicePurpose], tuple[AutocompleteChoice, ...]]
    execute_match_race_create: Callable[[MatchRaceCreateCommand], RaceOperationResultDTO]
    execute_match_race_edit_preserving_event: Callable[[MatchRaceEditCommand], RaceOperationResultDTO]
    execute_match_race_condition_set: Callable[[MatchRaceConditionCommand], RaceOperationResultDTO]
    execute_match_race_entries_replacement: Callable[[MatchRaceEntriesCommand], RaceOperationResultDTO]
    execute_match_betting_batch_transition: Callable[[MatchRaceBettingBatchCommand], tuple[RaceOperationResultDTO, ...]]
    query_match_race: QueryMatchRacePort
    parse_operator_kst_datetime: Callable[[str], datetime]
    parse_room_condition_profile: Callable[[str], tuple[str, str, str, int, str]]
    parse_room_condition_environment: Callable[[str], tuple[str, str, str]]
    parse_final_entry_snapshot: Callable[[str], tuple[RegisteredFinalEntrySnapshotInput, ...]]
    send_user_error: SendUserErrorPort
    send_pre_modal_user_error: SendUserErrorPort
    send_internal_error: SendCommandErrorPort
    handle_modal_open_error: SendCommandErrorPort
    send_initial_response: SendInitialResponsePort
    send_race_result: SendRaceResultPort
    send_race_batch_result: SendRaceBatchResultPort
    correlation_id: Callable[[object], str]
    interaction_user_id: Callable[[object], str]
    logger: logging.Logger


class MatchStaffLifecycleAdapter:
    """Persistence-free Discord orchestration for Room Match race lifecycle."""

    def __init__(self, ports: MatchStaffLifecycleAdapterPorts) -> None:
        self.ports = ports
        self._entry_adapter = MatchStaffEntryAdapter(
            MatchStaffEntryAdapterPorts(
                prepare_command=ports.prepare_command,
                prepare_component=ports.prepare_bound_component,
                build_context=ports.build_context,
                query_race_choices=ports.query_race_choices,
                parse_draft=parse_match_entry_draft_text,
                query_candidates=query_match_entry_game_account_candidates,
                execute_replacement=execute_match_race_resolved_entries_replacement,
                send_user_error=ports.send_user_error,
                send_pre_modal_user_error=ports.send_pre_modal_user_error,
                send_internal_error=ports.send_internal_error,
                handle_modal_open_error=ports.handle_modal_open_error,
                send_initial_response=ports.send_initial_response,
                send_race_result=ports.send_race_result,
                correlation_id=ports.correlation_id,
                interaction_user_id=ports.interaction_user_id,
                logger=ports.logger,
            )
        )

    async def race_create(self, interaction: discord.Interaction) -> None:
        command_name = "match.staff.race-create"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(RoomRaceCreateModal(adapter=self, context=context))
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def race_edit(self, interaction: discord.Interaction) -> None:
        await self._show_target_modal(
            interaction,
            command_name="match.staff.race-edit",
            purpose=RoomRaceChoicePurpose.EDIT,
            modal_factory=lambda choices, context: RoomRaceEditModal(
                adapter=self,
                choices=choices,
                context=context,
            ),
        )

    async def race_condition_set(self, interaction: discord.Interaction) -> None:
        await self._show_target_modal(
            interaction,
            command_name="match.staff.race-condition-set",
            purpose=RoomRaceChoicePurpose.EDIT,
            modal_factory=lambda choices, context: RoomRaceConditionModal(
                adapter=self,
                choices=choices,
                context=context,
            ),
        )

    async def entries_set(self, interaction: discord.Interaction) -> None:
        await self._show_target_modal(
            interaction,
            command_name="match.staff.entries-set",
            purpose=RoomRaceChoicePurpose.ENTRIES,
            modal_factory=lambda choices, context: RoomEntriesModal(
                adapter=self,
                choices=choices,
                context=context,
                resolution_adapter=self._entry_adapter,
            ),
        )

    async def betting_open(self, interaction: discord.Interaction) -> None:
        await self._show_lifecycle_batch_modal(interaction, opening=True)

    async def betting_close(self, interaction: discord.Interaction) -> None:
        await self._show_lifecycle_batch_modal(interaction, opening=False)

    async def race_show(self, interaction: discord.Interaction, race_id: int) -> None:
        command_name = "match.staff.race-show"
        if await self.ports.prepare_command(interaction, command_name) is None:
            return
        scalar_race_id = race_id
        try:
            result = await run_blocking_application(lambda: self.ports.query_match_race(race_id=scalar_race_id))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "레이스를 조회하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_race_result(interaction, command_name, result)

    async def submit_create(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        name: str,
        starts_at: str,
        description: str,
        reason: str,
    ) -> None:
        command_name = "match.staff.race-create"
        if not await self._prepare_submit(interaction, context, command_name):
            return
        actor_id, interaction_id = self._scalar_identity(interaction)
        try:
            command = MatchRaceCreateCommand(
                name=str(name),
                starts_at=self.ports.parse_operator_kst_datetime(str(starts_at)),
                description=_optional_text(description),
                reason=_optional_text(reason),
                actor_discord_user_id=actor_id,
                interaction_id=interaction_id,
            )
            result = await run_blocking_application(lambda: self.ports.execute_match_race_create(command))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(interaction, command_name, "레이스를 생성하지 못했습니다", exc)
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_race_result(interaction, command_name, result)

    async def submit_edit(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        name: str,
        starts_at: str,
        description: str,
        reason: str,
    ) -> None:
        command_name = "match.staff.race-edit"
        if not await self._prepare_submit(interaction, context, command_name):
            return
        actor_id, interaction_id = self._scalar_identity(interaction)
        try:
            command = MatchRaceEditCommand(
                race_id=_selected_id(selected_values, allowed_ids, "대상 경기"),
                name=str(name),
                starts_at=self.ports.parse_operator_kst_datetime(str(starts_at)),
                description=_optional_text(description),
                reason=_optional_text(reason),
                actor_discord_user_id=actor_id,
                interaction_id=interaction_id,
            )
            result = await run_blocking_application(
                lambda: self.ports.execute_match_race_edit_preserving_event(command)
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(interaction, command_name, "레이스를 수정하지 못했습니다", exc)
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_race_result(interaction, command_name, result)

    async def submit_condition(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        race_profile: str,
        environment: str,
        condition_label: str,
        reason: str,
    ) -> None:
        command_name = "match.staff.race-condition-set"
        if not await self._prepare_submit(interaction, context, command_name):
            return
        actor_id, interaction_id = self._scalar_identity(interaction)
        try:
            grade, venue, surface, distance, direction = self.ports.parse_room_condition_profile(str(race_profile))
            season, weather, track_condition = self.ports.parse_room_condition_environment(str(environment))
            command = MatchRaceConditionCommand(
                race_id=_selected_id(selected_values, allowed_ids, "대상 경기"),
                grade=grade,
                venue=venue,
                track_surface=surface,
                distance=distance,
                direction=direction,
                season=season,
                weather=weather,
                track_condition=track_condition,
                condition_label=str(condition_label),
                reason=_optional_text(reason),
                actor_discord_user_id=actor_id,
                interaction_id=interaction_id,
            )
            result = await run_blocking_application(lambda: self.ports.execute_match_race_condition_set(command))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(interaction, command_name, "조건을 설정하지 못했습니다", exc)
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_race_result(interaction, command_name, result)

    async def submit_entries(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        entries: str,
        reason: str,
    ) -> None:
        """Compatibility path retained for legacy direct-PID callers."""
        command_name = "match.staff.entries-set"
        if not await self._prepare_submit(interaction, context, command_name):
            return
        actor_id, interaction_id = self._scalar_identity(interaction)
        try:
            command = MatchRaceEntriesCommand(
                race_id=_selected_id(selected_values, allowed_ids, "대상 경기"),
                entries=self.ports.parse_final_entry_snapshot(str(entries)),
                reason=_optional_text(reason),
                actor_discord_user_id=actor_id,
                interaction_id=interaction_id,
            )
            result = await run_blocking_application(lambda: self.ports.execute_match_race_entries_replacement(command))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "확정 엔트리를 설정하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_race_result(interaction, command_name, result)

    async def submit_betting_batch(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
        command_name: str,
        opening: bool,
        selected_values: tuple[str, ...],
        allowed_ids: frozenset[int],
        reason: str,
    ) -> None:
        if not await self._prepare_submit(interaction, context, command_name):
            return
        actor_id, interaction_id = self._scalar_identity(interaction)
        try:
            command = MatchRaceBettingBatchCommand(
                race_ids=_selected_batch_ids(selected_values, allowed_ids),
                opening=opening,
                reason=_optional_text(reason),
                actor_discord_user_id=actor_id,
                interaction_id=interaction_id,
            )
            results = await run_blocking_application(lambda: self.ports.execute_match_betting_batch_transition(command))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "베팅 상태를 변경하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return
        await self.ports.send_race_batch_result(interaction, command_name, results)

    async def _show_target_modal(
        self,
        interaction: discord.Interaction,
        *,
        command_name: str,
        purpose: RoomRaceChoicePurpose,
        modal_factory: Callable[[tuple[AutocompleteChoice, ...], InteractionContext], discord.ui.Modal],
    ) -> None:
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        try:
            choices = await run_blocking_application(lambda: self.ports.query_race_choices(purpose))
            if not choices:
                await self.ports.send_initial_response(
                    interaction,
                    command_name,
                    "현재 이 작업을 적용할 수 있는 룸매치 경기가 없습니다.",
                )
                return
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(modal_factory(choices, context))
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction, command_name, "경기 목록을 불러오지 못했습니다", exc
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def _show_lifecycle_batch_modal(
        self,
        interaction: discord.Interaction,
        *,
        opening: bool,
    ) -> None:
        command_name = "match.staff.betting-open" if opening else "match.staff.betting-close"
        if await self.ports.prepare_command(interaction, command_name, defer=False) is None:
            return
        purpose = RoomRaceChoicePurpose.OPEN if opening else RoomRaceChoicePurpose.CLOSE
        try:
            choices = await run_blocking_application(lambda: self.ports.query_race_choices(purpose))
            if not choices:
                action_label = "열 수 있는" if opening else "마감할"
                await self.ports.send_initial_response(
                    interaction,
                    command_name,
                    f"현재 {action_label} 경기 또는 라운드가 없습니다.",
                )
                return
            context = self.ports.build_context(interaction)
            await interaction.response.send_modal(
                LifecycleBatchModal(
                    adapter=self,
                    command_name=command_name,
                    choices=choices,
                    opening=opening,
                    context=context,
                )
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_pre_modal_user_error(
                interaction, command_name, "경기 목록을 불러오지 못했습니다", exc
            )
        except Exception:
            await self.ports.handle_modal_open_error(interaction, command_name)

    async def _prepare_submit(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        command_name: str,
    ) -> bool:
        return (
            await self.ports.prepare_bound_component(
                interaction,
                context,
                command_name=command_name,
            )
            is not None
        )

    def _scalar_identity(self, interaction: discord.Interaction) -> tuple[str, str]:
        return str(interaction.user.id), self.ports.correlation_id(interaction)

    async def modal_error(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> None:
        log_sanitized_exception(
            self.ports.logger,
            "discord lifecycle modal failed correlation_id=%s command=%s actor_id=%s",
            self.ports.correlation_id(interaction),
            command_name,
            self.ports.interaction_user_id(interaction),
        )
        if not interaction.response.is_done():
            await self.ports.send_initial_response(
                interaction,
                command_name,
                f"내부 오류가 발생했습니다. 요청 ID: `{self.ports.correlation_id(interaction)}`",
            )


class _BoundLifecycleModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        adapter: MatchStaffLifecycleAdapter,
        context: InteractionContext,
        command_name: str,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._adapter = adapter
        self._context = context
        self._command_name = command_name

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await self._adapter.modal_error(interaction, self._command_name)


class RoomRaceCreateModal(_BoundLifecycleModal, title="룸매치 경기 생성"):
    def __init__(
        self,
        *,
        adapter: MatchStaffLifecycleAdapter,
        context: InteractionContext,
    ) -> None:
        super().__init__(
            adapter=adapter,
            context=context,
            command_name="match.staff.race-create",
            timeout=600,
        )
        self.name = discord.ui.TextInput(
            custom_id="room-race-create-name",
            placeholder="경기 표시명",
            required=True,
            min_length=1,
            max_length=200,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="room-race-create-starts-at",
            placeholder="2026-07-27 18:00 (KST)",
            required=True,
            min_length=16,
            max_length=16,
        )
        self.description = discord.ui.TextInput(
            custom_id="room-race-create-description",
            style=discord.TextStyle.paragraph,
            placeholder="선택 입력",
            required=False,
            max_length=1000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-race-create-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 특이사항 또는 운영 메모",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="경기명", component=self.name))
        self.add_item(
            discord.ui.Label(
                text="경기 시작 시각",
                description="기본 시간대는 KST이며 DB에는 UTC로 저장됩니다.",
                component=self.starts_at,
            )
        )
        self.add_item(discord.ui.Label(text="경기 설명", component=self.description))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_create(
            interaction,
            context=self._context,
            name=str(self.name.value),
            starts_at=str(self.starts_at.value),
            description=str(self.description.value),
            reason=str(self.reason.value),
        )


class RoomRaceEditModal(_BoundLifecycleModal, title="룸매치 경기 수정"):
    def __init__(
        self,
        *,
        adapter: MatchStaffLifecycleAdapter,
        choices: tuple[AutocompleteChoice, ...],
        context: InteractionContext,
    ) -> None:
        super().__init__(
            adapter=adapter,
            context=context,
            command_name="match.staff.race-edit",
            timeout=600,
        )
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = _race_select(
            custom_id="room-race-edit-target",
            placeholder="수정할 경기 선택",
            choices=choices,
        )
        self.name = discord.ui.TextInput(
            custom_id="room-race-edit-name",
            placeholder="변경 후 경기 표시명",
            required=True,
            min_length=1,
            max_length=200,
        )
        self.starts_at = discord.ui.TextInput(
            custom_id="room-race-edit-starts-at",
            placeholder="2026-07-27 18:00 (KST)",
            required=True,
            min_length=16,
            max_length=16,
        )
        self.description = discord.ui.TextInput(
            custom_id="room-race-edit-description",
            style=discord.TextStyle.paragraph,
            placeholder="빈 값이면 설명을 제거합니다.",
            required=False,
            max_length=1000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-race-edit-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 수정 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text="경기명", component=self.name))
        self.add_item(
            discord.ui.Label(
                text="경기 시작 시각",
                description="기본 시간대는 KST이며 DB에는 UTC로 저장됩니다.",
                component=self.starts_at,
            )
        )
        self.add_item(discord.ui.Label(text="경기 설명", component=self.description))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_edit(
            interaction,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            name=str(self.name.value),
            starts_at=str(self.starts_at.value),
            description=str(self.description.value),
            reason=str(self.reason.value),
        )


class RoomRaceConditionModal(_BoundLifecycleModal, title="룸매치 경기 조건 설정"):
    def __init__(
        self,
        *,
        adapter: MatchStaffLifecycleAdapter,
        choices: tuple[AutocompleteChoice, ...],
        context: InteractionContext,
    ) -> None:
        super().__init__(
            adapter=adapter,
            context=context,
            command_name="match.staff.race-condition-set",
            timeout=600,
        )
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.target = _race_select(
            custom_id="room-condition-target",
            placeholder="조건을 설정할 경기 선택",
            choices=choices,
        )
        self.race_profile = discord.ui.TextInput(
            custom_id="room-condition-race-profile",
            placeholder="G1 | 도쿄 | turf | 2400 | left",
            required=True,
            max_length=300,
        )
        self.environment = discord.ui.TextInput(
            custom_id="room-condition-environment",
            placeholder="summer | clear | good",
            required=True,
            max_length=200,
        )
        self.condition_label = discord.ui.TextInput(
            custom_id="room-condition-label",
            placeholder="표시용 조건명",
            required=True,
            min_length=1,
            max_length=200,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-condition-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 조건 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text="등급 | 경기장 | 트랙 | 거리 | 방향", component=self.race_profile))
        self.add_item(discord.ui.Label(text="계절 | 날씨 | 주로 상태", component=self.environment))
        self.add_item(discord.ui.Label(text="조건 표시명", component=self.condition_label))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_condition(
            interaction,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            race_profile=str(self.race_profile.value),
            environment=str(self.environment.value),
            condition_label=str(self.condition_label.value),
            reason=str(self.reason.value),
        )


class RoomEntriesModal(_BoundLifecycleModal, title="룸매치 엔트리 입력"):
    def __init__(
        self,
        *,
        adapter: MatchStaffLifecycleAdapter,
        choices: tuple[AutocompleteChoice, ...],
        context: InteractionContext,
        resolution_adapter: MatchStaffEntryAdapter | None = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            context=context,
            command_name="match.staff.entries-set",
            timeout=600,
        )
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self._resolution_adapter = resolution_adapter
        self.target = _race_select(
            custom_id="room-entries-target",
            placeholder="엔트리를 교체할 경기 선택",
            choices=choices,
        )
        if resolution_adapter is None:
            placeholder = "PID | 말 이름\n966621167959 | 스페셜 위크\n123456789012 | 사일런스 스즈카"
            label = "PID | 말 이름"
            description = None
        else:
            placeholder = "zener | 스페셜 위크\nalice | 라이스 샤워"
            label = "GameAccount 이름 일부 | 우마무스메명"
            description = "검색어는 후보 정렬에만 사용되며 실제 계정은 다음 화면에서 직접 선택합니다."
        self.entries = discord.ui.TextInput(
            custom_id="room-entries-values",
            placeholder=placeholder,
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-entries-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 엔트리 관련 특이사항",
            required=False,
            max_length=255,
        )
        self.add_item(discord.ui.Label(text="대상 경기", component=self.target))
        self.add_item(discord.ui.Label(text=label, description=description, component=self.entries))
        self.add_item(discord.ui.Label(text="운영 메모 (선택)", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self._resolution_adapter is not None:
            await self._resolution_adapter.begin_resolution(
                interaction,
                context=self._context,
                selected_values=tuple(self.target.values),
                allowed_ids=self._allowed_ids,
                entries_text=str(self.entries.value),
                reason=str(self.reason.value),
            )
            return
        await self._adapter.submit_entries(
            interaction,
            context=self._context,
            selected_values=tuple(self.target.values),
            allowed_ids=self._allowed_ids,
            entries=str(self.entries.value),
            reason=str(self.reason.value),
        )


class LifecycleBatchModal(_BoundLifecycleModal):
    def __init__(
        self,
        *,
        adapter: MatchStaffLifecycleAdapter,
        command_name: str,
        choices: tuple[AutocompleteChoice, ...],
        opening: bool,
        context: InteractionContext,
    ) -> None:
        action_label = "열기" if opening else "마감"
        super().__init__(
            adapter=adapter,
            context=context,
            command_name=command_name,
            title=f"룸매치 경기 일괄 {action_label}",
            timeout=600,
        )
        self._opening = opening
        self._allowed_ids = frozenset(choice.value for choice in choices)
        self.targets = discord.ui.Select(
            custom_id="room-lifecycle-targets",
            placeholder=f"{action_label}할 룸매치 경기 선택",
            min_values=1,
            max_values=len(choices),
            options=_select_options(choices),
            required=True,
        )
        self.reason = discord.ui.TextInput(
            custom_id="room-lifecycle-reason",
            style=discord.TextStyle.paragraph,
            placeholder="선택: 특이사항을 모든 항목에 공통 기록",
            required=False,
            max_length=255,
        )
        self.add_item(
            discord.ui.Label(
                text="룸매치 경기 선택",
                description="여러 항목을 동시에 선택할 수 있습니다.",
                component=self.targets,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="운영 메모 (선택)",
                description="입력하면 모든 항목의 감사 기록에 공통 적용됩니다.",
                component=self.reason,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.submit_betting_batch(
            interaction,
            context=self._context,
            command_name=self._command_name,
            opening=self._opening,
            selected_values=tuple(self.targets.values),
            allowed_ids=self._allowed_ids,
            reason=str(self.reason.value),
        )


def create_match_staff_lifecycle_adapter(
    ports: MatchStaffLifecycleAdapterPorts,
) -> MatchStaffLifecycleAdapter:
    return MatchStaffLifecycleAdapter(ports)


def _race_select(
    *,
    custom_id: str,
    placeholder: str,
    choices: tuple[AutocompleteChoice, ...],
) -> discord.ui.Select:
    return discord.ui.Select(
        custom_id=custom_id,
        placeholder=placeholder,
        min_values=1,
        max_values=1,
        options=_select_options(choices),
        required=True,
    )


def _select_options(
    choices: tuple[AutocompleteChoice, ...],
) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=choice.label, value=str(choice.value)) for choice in choices]


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _selected_id(
    values: tuple[str, ...],
    allowed_ids: frozenset[int],
    field_name: str,
) -> int:
    if len(values) != 1:
        raise ValueError(f"{field_name}을(를) 하나 선택해야 합니다.")
    try:
        selected_id = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 선택값이 올바르지 않습니다.") from exc
    if selected_id not in allowed_ids:
        raise ValueError(f"{field_name} 선택값이 현재 허용되지 않습니다.")
    return selected_id


def _selected_batch_ids(
    values: tuple[str, ...],
    allowed_ids: frozenset[int],
) -> tuple[int, ...]:
    try:
        selected_ids = tuple(sorted({int(value) for value in values}))
    except (TypeError, ValueError) as exc:
        raise ValueError("선택한 경기 목록이 유효하지 않습니다.") from exc
    if not selected_ids or not set(selected_ids).issubset(allowed_ids):
        raise ValueError("선택한 경기 목록이 유효하지 않습니다.")
    return selected_ids
