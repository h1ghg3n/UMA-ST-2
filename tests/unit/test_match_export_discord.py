"""Discord `/export match season` adapter tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

from uma_st2.adapters.discord import (
    ExportCommandGroup,
    ExportMatchCommandGroup,
    ExportWin5CommandGroup,
    MatchSeasonExportDiscordAdapter,
)
from uma_st2.application.exporting import ExportArtifact, MatchExportSeasonChoice

NOW = datetime(2026, 8, 29, tzinfo=UTC)


async def _direct(operation):  # type: ignore[no-untyped-def]
    return operation()


class FakeExports:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.export_calls: list[str] = []
        content = b"match-xlsx"
        self.artifact = ExportArtifact(
            filename="match_2026-split-2.xlsx",
            media_type="application/xlsx",
            content=content,
            sha256_hex=sha256(content).hexdigest(),
            generated_at=NOW,
            source_cutoff=NOW,
            schema_version="match-season-xlsx/v2",
            projection_version="match-season-projection/v2",
            scope_type="match_season",
            scope_id="2026-split-2",
            scope_name="2026 Split 2 @everyone",
            row_count=7,
        )

    def search_seasons(self, *, query: str, limit: int) -> tuple[MatchExportSeasonChoice, ...]:
        self.search_calls.append((query, limit))
        return (
            MatchExportSeasonChoice("2026-split-2", "2026 Split 2"),
            MatchExportSeasonChoice("2026-split-1", "2026 Split 1"),
        )

    def export_season(self, *, season_key: str) -> ExportArtifact:
        self.export_calls.append(season_key)
        return self.artifact


class FakeResponse:
    def is_done(self) -> bool:
        return True


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
        captured: list[tuple[str, bytes]] = []
        for attachment in kwargs.get("attachments", []):
            attachment.fp.seek(0)
            captured.append((attachment.filename, attachment.fp.read()))
        kwargs["attachments"] = captured
        self.edits.append(kwargs)


class RecordingPrepare:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def __call__(self, interaction, command_name: str, *, ephemeral: bool) -> bool:  # type: ignore[no-untyped-def]
        self.calls.append((command_name, ephemeral))
        return True


class RecordingAutocompleteAuthorization:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, interaction, command_name: str) -> bool:  # type: ignore[no-untyped-def]
        self.calls.append(command_name)
        return True


def _adapter() -> tuple[
    MatchSeasonExportDiscordAdapter,
    FakeExports,
    RecordingPrepare,
    RecordingAutocompleteAuthorization,
]:
    exports = FakeExports()
    prepare = RecordingPrepare()
    authorization = RecordingAutocompleteAuthorization()
    return (
        MatchSeasonExportDiscordAdapter(
            exports=exports,  # type: ignore[arg-type]
            prepare_command=prepare,
            authorize_autocomplete=authorization,
            blocking_runner=_direct,
        ),
        exports,
        prepare,
        authorization,
    )


def test_match_export_autocomplete_and_final_command_reauthorize() -> None:
    adapter, exports, prepare, authorization = _adapter()
    interaction = FakeInteraction()

    choices = asyncio.run(adapter.autocomplete_seasons(interaction, "2026"))  # type: ignore[arg-type]
    asyncio.run(adapter.export_season(interaction, season_key="2026-split-2"))  # type: ignore[arg-type]

    assert authorization.calls == ["export.match.season"]
    assert exports.search_calls == [("2026", 25)]
    assert [(choice.name, choice.value) for choice in choices] == [
        ("2026 Split 2", "2026-split-2"),
        ("2026 Split 1", "2026-split-1"),
    ]
    assert prepare.calls == [("export.match.season", True)]
    assert exports.export_calls == ["2026-split-2"]
    assert interaction.edits[0]["attachments"] == [("match_2026-split-2.xlsx", b"match-xlsx")]
    assert "2026 Split 2 @\u200beveryone" in interaction.edits[0]["content"]


def test_export_root_registers_match_and_win5_season_leaves() -> None:
    adapter, _, _, _ = _adapter()
    root = ExportCommandGroup(
        circle_point_adapter=adapter,  # type: ignore[arg-type]
        win5_group=ExportWin5CommandGroup(adapter=adapter),  # type: ignore[arg-type]
        match_group=ExportMatchCommandGroup(adapter=adapter),
    )

    commands = {command.name: command for command in root.commands}
    assert set(commands) == {"circle-points", "win5", "match"}
    assert [leaf.name for leaf in commands["win5"].commands] == ["season"]
    assert [leaf.name for leaf in commands["match"].commands] == ["season"]
