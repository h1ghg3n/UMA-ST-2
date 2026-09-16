"""CLI request boundary for reviewed initial master-data seeding."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MASTER_DATA_SEED_RECEIPT_SCHEMA = "uma-st-2-master-data-seed-receipt/v1"


@dataclass(frozen=True, slots=True)
class MasterDataSeedCliRequest:
    manifest_path: Path


def parse_master_data_seed_cli_request(argv: Sequence[str] | None = None) -> MasterDataSeedCliRequest:
    parser = argparse.ArgumentParser(
        prog="uma-st-2-seed-master-data",
        description="Seed one reviewed complete master-data manifest into a fresh V2 database.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="UTF-8 JSON manifest using uma-st-2-master-data-seed/v1",
    )
    namespace = parser.parse_args(argv)
    return MasterDataSeedCliRequest(manifest_path=namespace.manifest)
