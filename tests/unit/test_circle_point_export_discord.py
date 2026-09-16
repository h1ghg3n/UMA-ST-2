"""Discord `/export circle-points` adapter tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

from uma_st2.adapters.discord import CirclePointExportDiscordAdapter
from uma_st2.application.exporting import (
    CirclePointExportInvalidSourceError,
    ExportArtifact,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


async def _direct(operation):  # type: ignore[no-untyped-def]
    return operation()


class FakeExports:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0
        content = b"circle-point-xlsx"
        self.artifact = ExportArtifact(
            filename="circle-points.xlsx",
            media_type="application/xlsx",
            content=content,
            sha256_hex=sha256(content).hexdigest(),
            generated_at=NOW,
            source_cutoff=NOW,
            schema_version="circle-point-xlsx/v1",
            projection_version="circle-point-projection/v1",
            scope_type="circle_points",
            scope_id="current",
            scope_name="Current Circle Point snapshot",
            row_count=4,
        )

    def export_current(self) -> ExportArtifact:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
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


def test_circle_point_export_delivers_one_ephemeral_application_artifact() -> None:
    exports = FakeExports()
    prepare = RecordingPrepare()
    adapter = CirclePointExportDiscordAdapter(
        exports=exports,  # type: ignore[arg-type]
        prepare_command=prepare,
        blocking_runner=_direct,
    )
    interaction = FakeInteraction()

    asyncio.run(adapter.export_current(interaction))  # type: ignore[arg-type]

    assert prepare.calls == [("export.circle-points", True)]
    assert exports.calls == 1
    assert interaction.edits[0]["attachments"] == [("circle-points.xlsx", b"circle-point-xlsx")]
    assert exports.artifact.sha256_hex in interaction.edits[0]["content"]


def test_circle_point_export_expected_rejection_has_no_attachment() -> None:
    exports = FakeExports(failure=CirclePointExportInvalidSourceError("malformed"))
    adapter = CirclePointExportDiscordAdapter(
        exports=exports,  # type: ignore[arg-type]
        prepare_command=RecordingPrepare(),
        blocking_runner=_direct,
    )
    interaction = FakeInteraction()

    asyncio.run(adapter.export_current(interaction))  # type: ignore[arg-type]

    assert "생성하지 못했습니다" in interaction.edits[0]["content"]
    assert interaction.edits[0]["attachments"] == []
