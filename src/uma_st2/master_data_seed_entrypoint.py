"""Executable one-shot boundary for reviewed initial master-data seeding."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence

from uma_st2.adapters.cli import (
    MASTER_DATA_SEED_RECEIPT_SCHEMA,
    MasterDataSeedCliRequest,
    parse_master_data_seed_cli_request,
)
from uma_st2.application.master_data import MasterDataSeedError, MasterDataSeedReceipt
from uma_st2.compose import compose_master_data_seed_commands
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.master_data import MasterDataSeedManifestError, load_reviewed_master_data_seed

logger = logging.getLogger(__name__)


def run_master_data_seed(
    settings: DatabaseSettings,
    request: MasterDataSeedCliRequest,
) -> MasterDataSeedReceipt:
    """Validate the reviewed file before composing one target database runtime."""

    command = load_reviewed_master_data_seed(request.manifest_path)
    database_runtime = DatabaseRuntime.from_url(
        settings.database_url_value,
        pool_pre_ping=True,
    )
    try:
        return compose_master_data_seed_commands(database_runtime).seed(command)
    finally:
        database_runtime.dispose()


def _print_success(receipt: MasterDataSeedReceipt) -> None:
    payload = {
        "schema": MASTER_DATA_SEED_RECEIPT_SCHEMA,
        "status": "created" if receipt.created else "exact_retry",
        "manifest_sha256": receipt.manifest_checksum,
        "master_data_sha256": receipt.master_data_checksum,
        "counts": {
            "stadium_courses": receipt.stadium_course_count,
            "stadiums": receipt.stadium_count,
            "umamusume_variants": receipt.umamusume_variant_count,
            "umamusumes": receipt.umamusume_count,
        },
    }
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> None:
    request = parse_master_data_seed_cli_request(argv)
    try:
        settings = DatabaseSettings()
        receipt = run_master_data_seed(settings, request)
    except (MasterDataSeedManifestError, MasterDataSeedError) as exc:
        print(f"Master-data seed was rejected: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        logger.critical("Master-data seed failed error_type=%s", type(exc).__name__)
        print("Master-data seed failed due to an internal error.", file=sys.stderr)
        raise SystemExit(1) from None
    _print_success(receipt)


if __name__ == "__main__":
    main()
