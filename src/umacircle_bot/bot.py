import logging
import platform
import sys

import discord
from discord.ext import commands

from umacircle_bot.config import get_settings
from umacircle_bot.db.session import configure_session, dispose_session_engine
from umacircle_bot.discord_commands import (
    AccountCommandGroup,
    MatchCommandGroup,
    StaffCommandGroup,
    Win5CommandGroup,
    create_export_command_group,
    create_settings_command_group,
    help_command,
)
from umacircle_bot.runtime_preflight import verify_runtime_database

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


class UmaST2Bot(commands.Bot):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tree.add_command(AccountCommandGroup())
        self.tree.add_command(StaffCommandGroup())
        self.tree.add_command(MatchCommandGroup())
        self.tree.add_command(Win5CommandGroup())
        self.tree.add_command(create_export_command_group())
        self.tree.add_command(create_settings_command_group())
        self.tree.add_command(help_command)

    async def setup_hook(self) -> None:
        settings = get_settings()
        if settings.discord_guild_id:
            guild = discord.Object(id=settings.discord_guild_id)
            logger.info("guild command sync started guild_id=%s", settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            commands_synced = await self.tree.sync(guild=guild)
            logger.info(
                "guild command sync completed guild_id=%s command_count=%s",
                settings.discord_guild_id,
                len(commands_synced),
            )

    async def on_ready(self) -> None:
        logger.info(
            "Discord bot ready guild_count=%s user_id=%s",
            len(self.guilds),
            self.user.id if self.user is not None else "unknown",
        )


def build_bot() -> UmaST2Bot:
    intents = discord.Intents.default()
    return UmaST2Bot(command_prefix="!", intents=intents)


def main() -> None:
    configure_logging()
    settings = get_settings()
    settings.validate_runtime()
    diagnostics = verify_runtime_database(settings)
    logger.info(
        "startup preflight completed os=%s architecture=%s python=%s database_dialect=%s alembic_head=%s",
        platform.system(),
        platform.machine(),
        sys.version.split()[0],
        diagnostics.dialect,
        diagnostics.alembic_revision,
    )
    configure_session()
    try:
        bot = build_bot()
        bot.run(settings.resolve_discord_token())
    finally:
        dispose_session_engine()


if __name__ == "__main__":
    main()
