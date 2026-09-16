"""MariaDB evidence for automatic Discord publication-channel provisioning."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from uma_st2.application.discord import (
    DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE,
    DiscordPublicationChannelProvisioningStaleError,
    ProvisionDiscordPublicationChannel,
    ProvisionedDiscordPublicationChannel,
)
from uma_st2.application.publication import (
    MATCH_ANNOUNCEMENT_DESTINATION_KIND,
    MATCH_BETTING_OPENED_EVENT_TYPE,
)
from uma_st2.compose import (
    compose_discord_publication_channel_provisioning_commands,
    compose_discord_publication_channel_provisioning_queries,
)
from uma_st2.infrastructure.database import (
    DatabaseRuntime,
    SqlAlchemyDiscordPublicationChannelProvisioningRepository,
)
from uma_st2.infrastructure.database.orm import (
    BotGuildSettingORM,
    DiscordPublicationORM,
    OperationORM,
    SettingsOperationORM,
)

pytestmark = pytest.mark.integration


def _snowflake(prefix: int, suffix: int) -> str:
    return str(prefix * 10**18 + suffix % 10**18)


def _seed(engine: Engine, *, suffix: int) -> tuple[str, tuple[int, int]]:
    guild_id = _snowflake(1, suffix)
    now = datetime(2026, 9, 5, 4, 0)
    with Session(engine) as session, session.begin():
        session.add(
            BotGuildSettingORM(
                guild_id=guild_id,
                win5_announcement_channel_id=None,
                match_announcement_channel_id=None,
                log_channel_id=_snowflake(4, suffix),
                operator_role_id=_snowflake(5, suffix),
                bot_manager_role_id=_snowflake(6, suffix),
                default_timezone="Asia/Seoul",
                win5_announcements_enabled=True,
                match_announcements_enabled=True,
                match_odds_refresh_mode="live",
                match_odds_refresh_next_at=now + timedelta(minutes=1),
                match_odds_refresh_sequence=23,
                match_odds_last_projection_fingerprint="a" * 64,
                created_at=now - timedelta(days=5),
                updated_at=now,
            )
        )
        publications = []
        for sequence in (1, 2):
            publication = DiscordPublicationORM(
                guild_id=guild_id,
                destination_kind=MATCH_ANNOUNCEMENT_DESTINATION_KIND,
                event_type=MATCH_BETTING_OPENED_EVENT_TYPE,
                event_key=f"automatic-channel-{suffix}-{sequence}",
                source_kind="match",
                source_id=sequence,
                target_channel_id=None,
                payload_json={"schema_version": 1},
                payload_fingerprint=chr(96 + sequence) * 64,
                status="awaiting_channel",
                attempt_count=0,
                created_at=now + timedelta(seconds=sequence),
                updated_at=now + timedelta(seconds=sequence),
            )
            session.add(publication)
            publications.append(publication)
        session.flush()
        publication_ids = (publications[0].id, publications[1].id)
    return guild_id, publication_ids


def _cleanup(engine: Engine, *, guild_id: str, publication_ids: tuple[int, int]) -> None:
    with engine.begin() as connection:
        operation_ids = tuple(connection.scalars(select(OperationORM.id).where(OperationORM.guild_id == guild_id)))
        if operation_ids:
            connection.execute(delete(SettingsOperationORM).where(SettingsOperationORM.operation_id.in_(operation_ids)))
            connection.execute(delete(OperationORM).where(OperationORM.id.in_(operation_ids)))
        connection.execute(delete(DiscordPublicationORM).where(DiscordPublicationORM.id.in_(publication_ids)))
        connection.execute(delete(BotGuildSettingORM).where(BotGuildSettingORM.guild_id == guild_id))


def _command(
    runtime: DatabaseRuntime,
    *,
    guild_id: str,
    channel_id: str,
    actor_id: str,
    key: str,
) -> ProvisionDiscordPublicationChannel:
    target = compose_discord_publication_channel_provisioning_queries(runtime).find_target(guild_id=guild_id)
    assert target is not None
    return ProvisionDiscordPublicationChannel(
        target=target,
        target_channel_id=channel_id,
        actor_discord_user_id=actor_id,
        idempotency_key=key,
        correlation_id=key,
    )


def test_bind_commits_once_and_only_promotes_triggering_publication(migrated_engine: Engine) -> None:
    suffix = uuid4().int % 10**18
    guild_id, publication_ids = _seed(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    channel_id = _snowflake(2, suffix)
    command = _command(
        runtime,
        guild_id=guild_id,
        channel_id=channel_id,
        actor_id=_snowflake(7, suffix),
        key=f"automatic-channel-{suffix}",
    )
    try:
        created = compose_discord_publication_channel_provisioning_commands(runtime).provision(command)
        retry = compose_discord_publication_channel_provisioning_commands(runtime).provision(command)

        assert retry.operation_id == created.operation_id
        assert retry.exact_retry is True
        with Session(migrated_engine) as session:
            setting = session.get(BotGuildSettingORM, guild_id)
            first = session.get(DiscordPublicationORM, publication_ids[0])
            second = session.get(DiscordPublicationORM, publication_ids[1])
            operation = session.get(OperationORM, created.operation_id)
            audit = session.get(SettingsOperationORM, created.operation_id)
            assert setting is not None
            assert setting.match_announcement_channel_id == channel_id
            assert setting.win5_announcement_channel_id is None
            assert setting.log_channel_id == _snowflake(4, suffix)
            assert setting.operator_role_id == _snowflake(5, suffix)
            assert setting.bot_manager_role_id == _snowflake(6, suffix)
            assert setting.match_odds_refresh_mode == "live"
            assert setting.match_odds_refresh_sequence == 23
            assert setting.match_odds_last_projection_fingerprint == "a" * 64
            assert first is not None and (first.status, first.target_channel_id) == ("ready", channel_id)
            assert second is not None and (second.status, second.target_channel_id) == ("awaiting_channel", None)
            assert operation is not None and operation.idempotency_key == command.idempotency_key
            assert audit is not None and audit.type == DISCORD_PUBLICATION_CHANNEL_PROVISIONING_OPERATION_TYPE
            assert audit.after_data["publication_id"] == publication_ids[0]
            assert (
                session.scalar(select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id))
                == 1
            )
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, guild_id=guild_id, publication_ids=publication_ids)


def test_audit_failure_rolls_back_settings_publication_and_operation(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().int % 10**18
    guild_id, publication_ids = _seed(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    command = _command(
        runtime,
        guild_id=guild_id,
        channel_id=_snowflake(2, suffix),
        actor_id=_snowflake(7, suffix),
        key=f"automatic-channel-failure-{suffix}",
    )

    def fail_audit(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated publication-channel audit failure")

    monkeypatch.setattr(SqlAlchemyDiscordPublicationChannelProvisioningRepository, "add_audit", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="simulated publication-channel audit failure"):
            compose_discord_publication_channel_provisioning_commands(runtime).provision(command)
        with Session(migrated_engine) as session:
            setting = session.get(BotGuildSettingORM, guild_id)
            first = session.get(DiscordPublicationORM, publication_ids[0])
            second = session.get(DiscordPublicationORM, publication_ids[1])
            assert setting is not None and setting.match_announcement_channel_id is None
            assert first is not None and (first.status, first.target_channel_id) == ("awaiting_channel", None)
            assert second is not None and (second.status, second.target_channel_id) == ("awaiting_channel", None)
            assert (
                session.scalar(select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id))
                == 0
            )
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, guild_id=guild_id, publication_ids=publication_ids)


def test_concurrent_same_guild_binds_serialize_to_one_winner(migrated_engine: Engine) -> None:
    suffix = uuid4().int % 10**18
    guild_id, publication_ids = _seed(migrated_engine, suffix=suffix)
    runtime = DatabaseRuntime.from_engine(migrated_engine)
    target = compose_discord_publication_channel_provisioning_queries(runtime).find_target(guild_id=guild_id)
    assert target is not None
    barrier = Barrier(2)

    def provision(channel_prefix: int, key_suffix: str) -> object:
        commands = compose_discord_publication_channel_provisioning_commands(runtime)
        command = ProvisionDiscordPublicationChannel(
            target=target,
            target_channel_id=_snowflake(channel_prefix, suffix),
            actor_discord_user_id=_snowflake(7, suffix),
            idempotency_key=f"automatic-channel-concurrent-{key_suffix}-{suffix}",
        )
        barrier.wait()
        try:
            return commands.provision(command)
        except DiscordPublicationChannelProvisioningStaleError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures: tuple[Future[object], ...] = (
                executor.submit(provision, 2, "a"),
                executor.submit(provision, 3, "b"),
            )
            outcomes = tuple(future.result(timeout=20) for future in futures)

        assert sum(isinstance(outcome, ProvisionedDiscordPublicationChannel) for outcome in outcomes) == 1
        assert sum(isinstance(outcome, DiscordPublicationChannelProvisioningStaleError) for outcome in outcomes) == 1
        with Session(migrated_engine) as session:
            setting = session.get(BotGuildSettingORM, guild_id)
            first = session.get(DiscordPublicationORM, publication_ids[0])
            second = session.get(DiscordPublicationORM, publication_ids[1])
            assert setting is not None
            assert setting.match_announcement_channel_id in {_snowflake(2, suffix), _snowflake(3, suffix)}
            assert first is not None
            assert first.status == "ready"
            assert first.target_channel_id == setting.match_announcement_channel_id
            assert second is not None and (second.status, second.target_channel_id) == ("awaiting_channel", None)
            assert (
                session.scalar(select(func.count()).select_from(OperationORM).where(OperationORM.guild_id == guild_id))
                == 1
            )
    finally:
        runtime.dispose()
        _cleanup(migrated_engine, guild_id=guild_id, publication_ids=publication_ids)
