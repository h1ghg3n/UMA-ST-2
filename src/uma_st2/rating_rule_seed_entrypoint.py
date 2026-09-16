"""Executable one-shot boundary for reviewed V2 Rating rule seeding."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence

from uma_st2.adapters.cli import RatingRuleSeedCliRequest, parse_rating_rule_seed_cli_request
from uma_st2.application.rating import RatingRuleSeedError, SeededRatingRuleVersion, SeedRatingRuleVersion
from uma_st2.compose import compose_rating_rule_seed_commands
from uma_st2.config import DatabaseSettings
from uma_st2.infrastructure.database import DatabaseRuntime
from uma_st2.infrastructure.master_data import prepare_rating_rule_workbook

logger = logging.getLogger(__name__)


def _prepare_seed_command(request: RatingRuleSeedCliRequest) -> SeedRatingRuleVersion:
    if request.sheet_name is None:
        return prepare_rating_rule_workbook(
            request.workbook_path,
            source_identifier=request.source_identifier,
        )
    return prepare_rating_rule_workbook(
        request.workbook_path,
        source_identifier=request.source_identifier,
        sheet_name=request.sheet_name,
    )


def run_rating_rule_seed(
    settings: DatabaseSettings,
    request: RatingRuleSeedCliRequest,
) -> SeededRatingRuleVersion:
    """Parse before DB composition, seed once, and always dispose resources."""

    command = _prepare_seed_command(request)
    database_runtime = DatabaseRuntime.from_url(
        settings.database_url_value,
        pool_pre_ping=True,
    )
    try:
        return compose_rating_rule_seed_commands(database_runtime).seed(command)
    finally:
        database_runtime.dispose()


def _print_success(receipt: SeededRatingRuleVersion) -> None:
    status = "created" if receipt.created else "exact retry"
    print(f"Rating rule version {status}: {receipt.version_number}")
    print(f"Rule count: {receipt.rule_count}")
    print(f"Rule-set SHA-256: {receipt.rule_set_checksum}")


def main(argv: Sequence[str] | None = None) -> None:
    request = parse_rating_rule_seed_cli_request(argv)
    try:
        settings = DatabaseSettings()
        receipt = run_rating_rule_seed(settings, request)
    except RatingRuleSeedError as exc:
        print(f"Rating rule seed was rejected: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        logger.critical("Rating rule seed failed error_type=%s", type(exc).__name__)
        print("Rating rule seed failed due to an internal error.", file=sys.stderr)
        raise SystemExit(1) from None
    _print_success(receipt)


if __name__ == "__main__":
    main()
