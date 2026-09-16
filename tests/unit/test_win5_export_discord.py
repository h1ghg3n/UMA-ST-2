"""Discord `/export win5 season` adapter tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import discord

from uma_st2.adapters.discord import (
    ExportCommandGroup,
    ExportWin5CommandGroup,
    Win5SeasonExportDiscordAdapter,
)
from uma_st2.application.exporting import (
    ExportArtifact,
    Win5ExportSeasonChoice,
    Win5SeasonExportUnavailableError,
)
from uma_st2.domain.win5 import Win5SeasonStatus

NOW = datetime(2026, 8, 29, tzinfo=UTC)


async def _direct(operation):  # type: ignore[no-untyped-def]
    return operation()


class FakeExports:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.search_calls: list[tuple[str, int]] = []
        self.export_calls: list[int] = []
        content = b"xlsx-content"
        self.artifact = ExportArtifact(
            filename="win5_1_season.xlsx",
            media_type="application/xlsx",
            content=content,
            sha256_hex=sha256(content).hexdigest(),
            generated_at=NOW,
            source_cutoff=NOW,
            schema_version="win5-season-xlsx/v1",
            projection_version="win5-season-projection/v1",
            scope_type="win5_season",
            scope_id="1",
            scope_name="Season @everyone",
            row_count=12,
        )

    def search_seasons(self, *, query: str, limit: int) -> tuple[Win5ExportSeasonChoice, ...]:
        self.search_calls.append((query, limit))
        return (
            Win5ExportSeasonChoice(1, "Season", Win5SeasonStatus.ACTIVE),
            Win5ExportSeasonChoice(2, "Season", Win5SeasonStatus.ACTIVE),
        )

    def export_season(self, *, season_id: int) -> ExportArtifact:
        self.export_calls.append(season_id)
        if self.failure is not None:
            raise self.failure
        return self.artifact


class FakeResponse:
    def __init__(self) -> None:
        self.done = True

    def is_done(self) -> bool:
        return self.done


class FakeFollowup:
    async def send(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None


class FakeInteraction:
    def __init__(self) -> None:
        self.id = 999
        self.guild_id = 111
        self.channel_id = 222
        self.user = SimpleNamespace(id=333)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits: list[dict[str, object]] = []

    async def edit_original_response(self, **kwargs):  # type: ignore[no-untyped-def]
        attachments = kwargs.get("attachments", [])
        captured: list[tuple[str, bytes]] = []
        for attachment in attachments:
            attachment.fp.seek(0)
            captured.append((attachment.filename, attachment.fp.read()))
        kwargs["attachments"] = captured
        self.edits.append(kwargs)


class RecordingPrepare:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, interaction, command_name: str, *, ephemeral: bool) -> bool:  # type: ignore[no-untyped-def]
        self.calls.append((command_name, ephemeral))
        return self.allowed


class RecordingAutocompleteAuthorization:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[str] = []

    async def __call__(self, interaction, command_name: str) -> bool:  # type: ignore[no-untyped-def]
        self.calls.append(command_name)
        return self.allowed


def _adapter(
    exports: FakeExports,
    *,
    prepare: RecordingPrepare | None = None,
    autocomplete: RecordingAutocompleteAuthorization | None = None,
) -> tuple[Win5SeasonExportDiscordAdapter, RecordingPrepare, RecordingAutocompleteAuthorization]:
    prepare = prepare or RecordingPrepare()
    autocomplete = autocomplete or RecordingAutocompleteAuthorization()
    return (
        Win5SeasonExportDiscordAdapter(
            exports=exports,  # type: ignore[arg-type]
            prepare_command=prepare,
            authorize_autocomplete=autocomplete,
            blocking_runner=_direct,
        ),
        prepare,
        autocomplete,
    )


def test_export_delivers_one_ephemeral_attachment_from_application_artifact() -> None:
    exports = FakeExports()
    adapter, prepare, _ = _adapter(exports)
    interaction = FakeInteraction()

    asyncio.run(adapter.export_season(interaction, season_id=1))  # type: ignore[arg-type]

    assert prepare.calls == [("export.win5.season", True)]
    assert exports.export_calls == [1]
    assert len(interaction.edits) == 1
    edit = interaction.edits[0]
    assert edit["attachments"] == [("win5_1_season.xlsx", b"xlsx-content")]
    assert "Season @\u200beveryone" in edit["content"]
    assert exports.artifact.sha256_hex in edit["content"]
    assert isinstance(edit["allowed_mentions"], discord.AllowedMentions)


def test_export_expected_rejection_returns_private_message_without_attachment() -> None:
    exports = FakeExports(failure=Win5SeasonExportUnavailableError("gone"))
    adapter, _, _ = _adapter(exports)
    interaction = FakeInteraction()

    asyncio.run(adapter.export_season(interaction, season_id=1))  # type: ignore[arg-type]

    assert len(interaction.edits) == 1
    assert "생성하지 못했습니다" in interaction.edits[0]["content"]
    assert interaction.edits[0]["attachments"] == []


def test_export_autocomplete_reauthorizes_and_disambiguates_duplicates() -> None:
    exports = FakeExports()
    adapter, _, authorization = _adapter(exports)

    choices = asyncio.run(adapter.autocomplete_seasons(FakeInteraction(), "sea"))  # type: ignore[arg-type]

    assert authorization.calls == ["export.win5.season"]
    assert exports.search_calls == [("sea", 25)]
    assert [(choice.name, choice.value) for choice in choices] == [
        ("Season · active · ID 1", 1),
        ("Season · active · ID 2", 2),
    ]


def test_export_root_registers_only_nested_win5_season_leaf() -> None:
    adapter, _, _ = _adapter(FakeExports())
    group = ExportCommandGroup(
        circle_point_adapter=adapter,  # type: ignore[arg-type]
        win5_group=ExportWin5CommandGroup(adapter=adapter),
    )

    assert group.name == "export"
    commands = {command.name: command for command in group.commands}
    assert set(commands) == {"circle-points", "win5"}
    assert [command.name for command in commands["win5"].commands] == ["season"]
