from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from umacircle_bot.config import Settings
from umacircle_bot.domain.permissions import can_manage_bot, can_operate, is_system_owner


class GuildChannelSettings(Protocol):
    win5_announcement_channel_id: str | None
    room_match_announcement_channel_id: str | None


class CommandAccess(StrEnum):
    MEMBER = "member"
    OPERATOR = "operator"
    BOT_MANAGER = "bot_manager"
    OWNER = "owner"


class CommandChannelScope(StrEnum):
    ANY = "any"
    ADMIN = "admin"
    MATCH = "match"
    WIN5 = "win5"


@dataclass(frozen=True, slots=True)
class GuildRoleAuthorization:
    operator_role_id: int = 0
    bot_manager_role_id: int = 0
    owner_role_id: int = 0


COMMAND_ACCESS_MATRIX = MappingProxyType(
    {
        "account.register": CommandAccess.MEMBER,
        "help": CommandAccess.MEMBER,
        "account.registration-status": CommandAccess.MEMBER,
        "account.cancel-registration": CommandAccess.MEMBER,
        "account.info": CommandAccess.MEMBER,
        "account.link-request": CommandAccess.MEMBER,
        "account.link-status": CommandAccess.MEMBER,
        "account.link-cancel": CommandAccess.MEMBER,
        "settings.general": CommandAccess.BOT_MANAGER,
        "settings.role": CommandAccess.OWNER,
        "export.circle-points": CommandAccess.BOT_MANAGER,
        "export.win5.season": CommandAccess.BOT_MANAGER,
        "staff.grant-circle-points": CommandAccess.OPERATOR,
        "staff.adjust-circle-points": CommandAccess.OPERATOR,
        "staff.change-name": CommandAccess.OPERATOR,
        "staff.link-player": CommandAccess.OPERATOR,
        "staff.account-registration-approve": CommandAccess.OPERATOR,
        "staff.account-registration-reject": CommandAccess.OPERATOR,
        "staff.persona.attach-discord": CommandAccess.OPERATOR,
        "staff.persona.claim-game-account": CommandAccess.OPERATOR,
        "match.staff.race-create": CommandAccess.OPERATOR,
        "match.staff.race-edit": CommandAccess.OPERATOR,
        "match.staff.race-condition-set": CommandAccess.OPERATOR,
        "match.staff.entries-set": CommandAccess.OPERATOR,
        "match.staff.betting-open": CommandAccess.OPERATOR,
        "match.staff.betting-close": CommandAccess.OPERATOR,
        "match.staff.race-show": CommandAccess.OPERATOR,
        "match.staff.result-submit": CommandAccess.OPERATOR,
        "match.staff.result-review": CommandAccess.OPERATOR,
        "match.staff.result-correct": CommandAccess.OPERATOR,
        "match.staff.result-reject": CommandAccess.OPERATOR,
        "match.staff.result-confirm": CommandAccess.OPERATOR,
        "match.staff.result-show": CommandAccess.OPERATOR,
        "match.staff.settlement": CommandAccess.OPERATOR,
        "match.staff.settlement-rollback": CommandAccess.OPERATOR,
        "match.staff.publish": CommandAccess.OPERATOR,
        "win5.staff.season": CommandAccess.OPERATOR,
        "win5.staff.round": CommandAccess.OPERATOR,
        "match.races": CommandAccess.MEMBER,
        "match.ratings": CommandAccess.MEMBER,
        "match.bet": CommandAccess.MEMBER,
        "win5.info": CommandAccess.MEMBER,
        "win5.rounds": CommandAccess.MEMBER,
        "win5.submit": CommandAccess.MEMBER,
        "win5.special-submit": CommandAccess.MEMBER,
        "win5.submissions": CommandAccess.MEMBER,
        "win5.cancel": CommandAccess.MEMBER,
        "win5.standings": CommandAccess.MEMBER,
    }
)

COMMAND_CHANNEL_SCOPE_MATRIX = MappingProxyType(
    {
        command_name: (
            CommandChannelScope.ADMIN
            if command_name.startswith(("settings.", "staff.", "export.", "match.staff.", "win5.staff."))
            else CommandChannelScope.MATCH
            if command_name.startswith("match.")
            else CommandChannelScope.WIN5
            if command_name.startswith("win5.")
            else CommandChannelScope.ANY
        )
        for command_name in COMMAND_ACCESS_MATRIX
    }
)

LEGACY_PLAYER_LINK_COMMAND_NAMES = frozenset(
    {
        "account.link-request",
        "account.link-status",
        "account.link-cancel",
        "staff.link-player",
    }
)


def interaction_access_error(
    interaction: object,
    *,
    settings: Settings,
    access: CommandAccess,
    channel_scope: CommandChannelScope = CommandChannelScope.ANY,
    channel_settings: GuildChannelSettings | None = None,
    role_authorization: GuildRoleAuthorization | None = None,
) -> str | None:
    if settings.discord_guild_id and getattr(interaction, "guild_id", None) != settings.discord_guild_id:
        return "이 서버에서는 해당 명령을 사용할 수 없습니다."
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id is None:
        return "채널 정보가 없는 요청은 실행할 수 없습니다."
    allowed_channel_ids, channel_label, setup_error = allowed_channel_ids_for_scope(
        settings,
        channel_scope,
        channel_settings=channel_settings,
    )
    if setup_error is not None:
        return setup_error
    if allowed_channel_ids and channel_id not in allowed_channel_ids:
        return f"이 명령은 {channel_label}에서만 사용할 수 있습니다."
    user = getattr(interaction, "user", None)
    roles = role_authorization or GuildRoleAuthorization(owner_role_id=settings.owner_role_id)
    if access is CommandAccess.OPERATOR and not can_operate_member(
        user,
        operator_role_id=roles.operator_role_id,
        bot_manager_role_id=roles.bot_manager_role_id,
        owner_role_id=roles.owner_role_id,
    ):
        return "이 명령을 실행할 역할 권한이 없습니다."
    if access is CommandAccess.OWNER and not is_system_owner(
        member_role_ids(user),
        owner_role_id=roles.owner_role_id,
    ):
        return "이 명령을 실행할 역할 권한이 없습니다."
    if access is CommandAccess.BOT_MANAGER and not can_manage_bot(
        member_role_ids(user),
        bot_manager_role_id=roles.bot_manager_role_id,
        owner_role_id=roles.owner_role_id,
    ):
        return "이 명령을 실행할 역할 권한이 없습니다."
    return None


def allowed_channel_ids_for_scope(
    settings: Settings,
    channel_scope: CommandChannelScope,
    *,
    channel_settings: GuildChannelSettings | None,
) -> tuple[tuple[int, ...], str, str | None]:
    if channel_scope is CommandChannelScope.ADMIN:
        return settings.admin_channel_ids, "관리 채널", None
    if channel_scope is CommandChannelScope.MATCH:
        channel_id = channel_settings.room_match_announcement_channel_id if channel_settings else None
        if channel_id is None:
            return (), "", "룸매치 베팅·알림 채널을 `/settings general`에서 먼저 설정해 주세요."
        return (int(channel_id),), "룸매치 베팅·알림 채널", None
    if channel_scope is CommandChannelScope.WIN5:
        channel_id = channel_settings.win5_announcement_channel_id if channel_settings else None
        if channel_id is None:
            return (), "", "WIN5 이용·알림 채널을 `/settings general`에서 먼저 설정해 주세요."
        return (int(channel_id),), "WIN5 이용·알림 채널", None
    return (), "", None


def can_operate_member(
    user: object,
    *,
    operator_role_id: int,
    bot_manager_role_id: int = 0,
    owner_role_id: int = 0,
) -> bool:
    return can_operate(
        member_role_ids(user),
        operator_role_id=operator_role_id,
        bot_manager_role_id=bot_manager_role_id,
        owner_role_id=owner_role_id,
    )


def member_role_ids(user: object) -> tuple[int, ...]:
    roles = getattr(user, "roles", ())
    return tuple(role.id for role in roles if isinstance(getattr(role, "id", None), int))
