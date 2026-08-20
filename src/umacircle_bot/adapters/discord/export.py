from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import discord
from discord import app_commands

from umacircle_bot.adapters.discord.common import run_blocking_application
from umacircle_bot.domain.errors import DomainError


class RuntimeSettings(Protocol):
    export_dir: Path


class CirclePointExportResult(Protocol):
    export_run_id: int
    output_path: Path
    row_count: int
    sha256_checksum: str


class Win5SeasonExportResult(Protocol):
    export_run_id: int
    output_path: Path
    season_id: int
    season_number: int
    season_name: str
    round_count: int
    submission_count: int
    participant_count: int
    hall_of_fame_count: int
    sha256_checksum: str


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> RuntimeSettings | None: ...


class AutocompleteWin5SeasonPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]: ...


class ExportCirclePointsPort(Protocol):
    def __call__(self, output_dir: Path) -> CirclePointExportResult: ...


class ExportWin5SeasonPort(Protocol):
    def __call__(self, output_dir: Path, season_id: int) -> Win5SeasonExportResult: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendInternalErrorPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str) -> None: ...


class SendFollowupPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
        *,
        response_kind: str = "success",
        file_path: Path | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExportAdapterPorts:
    prepare_command: PrepareCommandPort
    autocomplete_win5_season: AutocompleteWin5SeasonPort
    export_circle_points: ExportCirclePointsPort
    export_win5_season: ExportWin5SeasonPort
    send_user_error: SendUserErrorPort
    send_internal_error: SendInternalErrorPort
    send_followup: SendFollowupPort
    correlation_id: Callable[[object], str]
    logger: logging.Logger


class ExportAdapter:
    def __init__(self, ports: ExportAdapterPorts) -> None:
        self.ports = ports

    def create_command_group(self) -> ExportCommandGroup:
        return ExportCommandGroup(adapter=self)

    async def export_circle_points(self, interaction: discord.Interaction) -> None:
        command_name = "export.circle-points"
        settings = await self.ports.prepare_command(interaction, command_name)
        if settings is None:
            return
        try:
            result = await run_blocking_application(lambda: self.ports.export_circle_points(settings.export_dir))
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return

        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s export_run_id=%s",
            self.ports.correlation_id(interaction),
            command_name,
            result.export_run_id,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            f"서클 포인트 snapshot {result.row_count}건을 생성했습니다. SHA-256: `{result.sha256_checksum}`",
            file_path=result.output_path,
        )

    async def export_win5_season(
        self,
        interaction: discord.Interaction,
        *,
        season_id: int,
    ) -> None:
        command_name = "export.win5.season"
        settings = await self.ports.prepare_command(interaction, command_name)
        if settings is None:
            return
        try:
            result = await run_blocking_application(
                lambda: self.ports.export_win5_season(settings.export_dir, season_id)
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                command_name,
                "WIN5 시즌 XLSX를 생성하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, command_name)
            return

        self.ports.logger.info(
            "discord command succeeded correlation_id=%s command=%s export_run_id=%s season_id=%s",
            self.ports.correlation_id(interaction),
            command_name,
            result.export_run_id,
            result.season_id,
        )
        await self.ports.send_followup(
            interaction,
            command_name,
            (
                f"WIN5 {result.season_number}시즌 `{result.season_name}` snapshot을 생성했습니다. "
                f"라운드 {result.round_count}개, 제출 내역 {result.submission_count}건, "
                f"참가자 {result.participant_count}명, TOP5 완전 적중 {result.hall_of_fame_count}건입니다. "
                f"SHA-256: `{result.sha256_checksum}`"
            ),
            file_path=result.output_path,
        )

    async def autocomplete_win5_season(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self.ports.autocomplete_win5_season(interaction, current)


class Win5ExportCommandGroup(app_commands.Group):
    def __init__(self, *, adapter: ExportAdapter) -> None:
        super().__init__(name="win5", description="WIN5 결과를 XLSX로 내보냅니다.")
        self._adapter = adapter

    async def season_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_win5_season(interaction, current)

    @app_commands.command(name="season", description="선택한 WIN5 시즌 XLSX를 생성해 전송합니다.")
    @app_commands.describe(season="내보낼 WIN5 시즌")
    @app_commands.autocomplete(season=season_autocomplete)
    async def season(self, interaction: discord.Interaction, season: int) -> None:
        await self._adapter.export_win5_season(interaction, season_id=season)


class ExportCommandGroup(app_commands.Group):
    def __init__(self, *, adapter: ExportAdapter) -> None:
        super().__init__(name="export", description="운영 snapshot을 XLSX로 내보냅니다.")
        self._adapter = adapter
        self.add_command(Win5ExportCommandGroup(adapter=adapter))

    @app_commands.command(name="circle-points", description="현재 서클 포인트 XLSX를 생성해 전송합니다.")
    async def circle_points(self, interaction: discord.Interaction) -> None:
        await self._adapter.export_circle_points(interaction)


def create_export_command_group(ports: ExportAdapterPorts) -> ExportCommandGroup:
    return ExportAdapter(ports).create_command_group()
