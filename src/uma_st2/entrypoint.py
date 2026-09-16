"""Executable process boundary for the UMA-ST-2 V2 Discord runtime."""

from __future__ import annotations

import logging

from uma_st2.compose import compose_discord_runtime
from uma_st2.config import RuntimeSettings

logger = logging.getLogger(__name__)


def configure_logging(log_level: str) -> None:
    """Configure bounded process logging without rendering runtime settings."""

    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_discord_runtime(settings: RuntimeSettings) -> None:
    """Resolve the token, run the composed client, and always dispose the DB runtime."""

    token = settings.resolve_discord_token()
    runtime = compose_discord_runtime(settings)
    try:
        runtime.run(token)
    finally:
        runtime.dispose()


def main() -> None:
    """Start the configured Discord process or exit on an unrecoverable failure."""

    settings = RuntimeSettings()
    configure_logging(settings.log_level)
    try:
        run_discord_runtime(settings)
    except Exception as exc:
        logger.critical(
            "UMA-ST-2 Discord runtime stopped after an unrecoverable failure error_type=%s",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
