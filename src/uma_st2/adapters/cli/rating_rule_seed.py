"""CLI request parsing for one reviewed Rating rule workbook seed."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RatingRuleSeedCliRequest:
    """Explicit local source provenance for one immutable rule seed."""

    workbook_path: Path
    source_identifier: str
    sheet_name: str | None


def _bounded_text(*, field_name: str, maximum: int) -> Callable[[str], str]:
    def parse(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise argparse.ArgumentTypeError(f"{field_name} must contain 1 to {maximum} characters")
        return normalized

    return parse


def parse_rating_rule_seed_cli_request(
    argv: Sequence[str] | None = None,
) -> RatingRuleSeedCliRequest:
    """Parse the explicit one-shot Rating rule seed surface."""

    parser = argparse.ArgumentParser(
        prog="uma-st-2-seed-rating-rules",
        description="Seed one reviewed Rating rule workbook into the V2 canonical database.",
    )
    parser.add_argument(
        "--workbook",
        required=True,
        type=Path,
        help="reviewed local XLSX workbook path",
    )
    parser.add_argument(
        "--source-identifier",
        required=True,
        type=_bounded_text(field_name="source identifier", maximum=200),
        help="stable reviewed source identifier stored with the immutable version",
    )
    parser.add_argument(
        "--sheet-name",
        type=_bounded_text(field_name="sheet name", maximum=100),
        help="reviewed rule worksheet; omit to use the canonical Rate 기준표 sheet",
    )
    namespace = parser.parse_args(argv)
    return RatingRuleSeedCliRequest(
        workbook_path=namespace.workbook,
        source_identifier=namespace.source_identifier,
        sheet_name=namespace.sheet_name,
    )
