from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import discord
from discord import app_commands


class InteractionActionPort(Protocol):
    async def __call__(self, interaction: discord.Interaction) -> None: ...


class RaceShowPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, race_id: int) -> None: ...


class SettlementRollbackPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        race_id: int,
        reason: str,
    ) -> None: ...


class ResultShowPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        race_id: int,
        revision_number: int | None,
    ) -> None: ...


class AutocompletePort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...


@dataclass(frozen=True, slots=True)
class MatchStaffAdapterPorts:
    race_create: InteractionActionPort
    race_edit: InteractionActionPort
    race_condition_set: InteractionActionPort
    entries_set: InteractionActionPort
    betting_open: InteractionActionPort
    betting_close: InteractionActionPort
    race_show: RaceShowPort
    result_submit: InteractionActionPort
    result_review: InteractionActionPort
    result_correct: InteractionActionPort
    result_reject: InteractionActionPort
    result_confirm: InteractionActionPort
    settlement: InteractionActionPort
    settlement_rollback: SettlementRollbackPort
    publish: RaceShowPort
    result_show: ResultShowPort
    autocomplete_race_show: AutocompletePort
    autocomplete_publish: AutocompletePort
    autocomplete_result_show: AutocompletePort


class MatchStaffAdapter:
    """Persistence-free owner of the production ``/match staff`` leaves."""

    def __init__(self, ports: MatchStaffAdapterPorts) -> None:
        self.ports = ports

    def create_command_group(self) -> MatchStaffCommandGroup:
        return MatchStaffCommandGroup(adapter=self)

    async def race_create(self, interaction: discord.Interaction) -> None:
        await self.ports.race_create(interaction)

    async def race_edit(self, interaction: discord.Interaction) -> None:
        await self.ports.race_edit(interaction)

    async def race_condition_set(self, interaction: discord.Interaction) -> None:
        await self.ports.race_condition_set(interaction)

    async def entries_set(self, interaction: discord.Interaction) -> None:
        await self.ports.entries_set(interaction)

    async def betting_open(self, interaction: discord.Interaction) -> None:
        await self.ports.betting_open(interaction)

    async def betting_close(self, interaction: discord.Interaction) -> None:
        await self.ports.betting_close(interaction)

    async def race_show(self, interaction: discord.Interaction, race_id: int) -> None:
        await self.ports.race_show(interaction, race_id)

    async def result_submit(self, interaction: discord.Interaction) -> None:
        await self.ports.result_submit(interaction)

    async def result_review(self, interaction: discord.Interaction) -> None:
        await self.ports.result_review(interaction)

    async def result_correct(self, interaction: discord.Interaction) -> None:
        await self.ports.result_correct(interaction)

    async def result_reject(self, interaction: discord.Interaction) -> None:
        await self.ports.result_reject(interaction)

    async def result_confirm(self, interaction: discord.Interaction) -> None:
        await self.ports.result_confirm(interaction)

    async def settlement(self, interaction: discord.Interaction) -> None:
        await self.ports.settlement(interaction)

    async def settlement_rollback(
        self,
        interaction: discord.Interaction,
        race_id: int,
        reason: str,
    ) -> None:
        await self.ports.settlement_rollback(interaction, race_id, reason)

    async def publish(self, interaction: discord.Interaction, race_id: int) -> None:
        await self.ports.publish(interaction, race_id)

    async def result_show(
        self,
        interaction: discord.Interaction,
        race_id: int,
        revision_number: int | None,
    ) -> None:
        await self.ports.result_show(interaction, race_id, revision_number)


class MatchStaffCommandGroup(app_commands.Group):
    def __init__(self, *, adapter: MatchStaffAdapter) -> None:
        super().__init__(name="staff", description="룸매치 스태프 전용 기능입니다.")
        self._adapter = adapter

    async def race_show_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.ports.autocomplete_race_show(interaction, current)

    async def publish_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.ports.autocomplete_publish(interaction, current)

    async def result_show_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.ports.autocomplete_result_show(interaction, current)

    @app_commands.command(name="race-create", description="룸매치 레이스를 생성합니다.")
    async def race_create(self, interaction: discord.Interaction) -> None:
        await self._adapter.race_create(interaction)

    @app_commands.command(name="race-edit", description="setup 룸매치 레이스 정보를 수정합니다.")
    async def race_edit(self, interaction: discord.Interaction) -> None:
        await self._adapter.race_edit(interaction)

    @app_commands.command(name="race-condition-set", description="setup 레이스의 조건을 설정합니다.")
    async def race_condition_set(self, interaction: discord.Interaction) -> None:
        await self._adapter.race_condition_set(interaction)

    @app_commands.command(name="entries-set", description="룸매치 확정 엔트리를 전체 교체합니다.")
    async def entries_set(self, interaction: discord.Interaction) -> None:
        await self._adapter.entries_set(interaction)

    @app_commands.command(name="betting-open", description="setup 레이스의 베팅을 시작합니다.")
    async def betting_open(self, interaction: discord.Interaction) -> None:
        await self._adapter.betting_open(interaction)

    @app_commands.command(name="betting-close", description="열린 룸매치 베팅을 마감합니다.")
    async def betting_close(self, interaction: discord.Interaction) -> None:
        await self._adapter.betting_close(interaction)

    @app_commands.command(name="race-show", description="룸매치 레이스 snapshot을 조회합니다.")
    @app_commands.autocomplete(race_id=race_show_autocomplete)
    async def race_show(self, interaction: discord.Interaction, race_id: int) -> None:
        await self._adapter.race_show(interaction, race_id)

    @app_commands.command(name="result-submit", description="룸매치 도착 순서를 제출합니다.")
    async def result_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.result_submit(interaction)

    @app_commands.command(name="result-review", description="제출된 룸매치 결과를 검토 완료 처리합니다.")
    async def result_review(self, interaction: discord.Interaction) -> None:
        await self._adapter.result_review(interaction)

    @app_commands.command(name="result-correct", description="현재 룸매치 결과 revision을 정정합니다.")
    async def result_correct(self, interaction: discord.Interaction) -> None:
        await self._adapter.result_correct(interaction)

    @app_commands.command(name="result-reject", description="현재 룸매치 결과 revision을 반려합니다.")
    async def result_reject(self, interaction: discord.Interaction) -> None:
        await self._adapter.result_reject(interaction)

    @app_commands.command(name="result-confirm", description="검토된 룸매치 결과를 미리보고 확정합니다.")
    async def result_confirm(self, interaction: discord.Interaction) -> None:
        await self._adapter.result_confirm(interaction)

    @app_commands.command(name="settlement", description="결과 확정 룸매치의 배율을 확인하고 정산합니다.")
    async def settlement(self, interaction: discord.Interaction) -> None:
        await self._adapter.settlement(interaction)

    @app_commands.command(name="settlement-rollback", description="정산 완료 룸매치를 사유와 함께 롤백합니다.")
    @app_commands.describe(race_id="롤백할 정산 완료 레이스 ID", reason="필수 롤백 사유")
    async def settlement_rollback(
        self,
        interaction: discord.Interaction,
        race_id: int,
        reason: str,
    ) -> None:
        await self._adapter.settlement_rollback(interaction, race_id, reason)

    @app_commands.command(name="publish", description="정산 완료 룸매치 결과를 공개합니다.")
    @app_commands.autocomplete(race_id=publish_autocomplete)
    async def publish(self, interaction: discord.Interaction, race_id: int) -> None:
        await self._adapter.publish(interaction, race_id)

    @app_commands.command(name="result-show", description="룸매치 결과 revision을 조회합니다.")
    @app_commands.autocomplete(race_id=result_show_autocomplete)
    async def result_show(
        self,
        interaction: discord.Interaction,
        race_id: int,
        revision_number: int | None = None,
    ) -> None:
        await self._adapter.result_show(interaction, race_id, revision_number)


def create_match_staff_command_group(ports: MatchStaffAdapterPorts) -> MatchStaffCommandGroup:
    return MatchStaffAdapter(ports).create_command_group()
