"""CLI request parsing and caller-owned WIN5 export artifact delivery."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from uma_st2.application.exporting import ExportArtifact, Win5SeasonExports


class Win5ExportCliError(RuntimeError):
    """Base error for expected local artifact delivery failures."""


class Win5ExportCliArtifactExistsError(Win5ExportCliError):
    """The generated safe filename already exists in the output directory."""


class Win5ExportCliArtifactWriteError(Win5ExportCliError):
    """The complete returned artifact could not be written locally."""


@dataclass(frozen=True, slots=True)
class Win5SeasonExportCliRequest:
    """Validated command-line request for one canonical Season snapshot."""

    season_id: int


@dataclass(frozen=True, slots=True)
class SavedWin5SeasonExport:
    """Caller-visible receipt for one fully written Application artifact."""

    path: Path
    artifact: ExportArtifact


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_win5_export_cli_request(
    argv: Sequence[str] | None = None,
) -> Win5SeasonExportCliRequest:
    """Parse the confirmed one-shot WIN5 Season export surface."""

    parser = argparse.ArgumentParser(
        prog="uma-st-2-export-win5",
        description="Export one active or closed WIN5 Season as XLSX.",
    )
    parser.add_argument(
        "--season-id",
        required=True,
        type=_positive_integer,
        help="canonical active or closed WIN5 Season ID",
    )
    namespace = parser.parse_args(argv)
    return Win5SeasonExportCliRequest(season_id=namespace.season_id)


def _remove_incomplete_target(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError:
        # Preserve the original write failure. The process boundary reports the
        # target path so an operator can inspect an exceptional cleanup failure.
        pass


def _write_artifact_exclusively(
    artifact: ExportArtifact,
    *,
    output_directory: Path,
) -> Path:
    target = output_directory / artifact.filename
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            written = stream.write(artifact.content)
            if written != len(artifact.content):
                raise OSError("artifact write was incomplete")
    except FileExistsError as exc:
        raise Win5ExportCliArtifactExistsError(f"Refusing to overwrite existing artifact: {target}") from exc
    except OSError as exc:
        if created:
            _remove_incomplete_target(target)
        raise Win5ExportCliArtifactWriteError(f"Could not write WIN5 export artifact: {target}") from exc
    except BaseException:
        if created:
            _remove_incomplete_target(target)
        raise
    return target


@dataclass(frozen=True, slots=True)
class Win5SeasonExportCliAdapter:
    """Invoke the shared Application export and deliver its bytes locally."""

    exports: Win5SeasonExports

    def execute(
        self,
        request: Win5SeasonExportCliRequest,
        *,
        output_directory: Path,
    ) -> SavedWin5SeasonExport:
        artifact = self.exports.export_season(season_id=request.season_id)
        path = _write_artifact_exclusively(
            artifact,
            output_directory=output_directory,
        )
        return SavedWin5SeasonExport(path=path, artifact=artifact)
