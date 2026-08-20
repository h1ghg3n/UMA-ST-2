from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import discord
from discord import app_commands

from umacircle_bot.adapters.discord.common import InteractionContext, run_blocking_application
from umacircle_bot.domain.errors import DomainError
from umacircle_bot.logging_safety import log_sanitized_exception
from umacircle_bot.services.persona_discord_link_application import (
    StaffDiscordPersonaAttachCommand,
    StaffDiscordPersonaAttachPreviewDTO,
    StaffDiscordPersonaAttachResultDTO,
    StaffPersonaChoiceDTO,
)
from umacircle_bot.services.persona_game_account_claim_application import (
    StaffSourceGameAccountChoiceDTO,
    StaffSourceGameAccountClaimCommand,
    StaffSourceGameAccountClaimPreviewDTO,
    StaffSourceGameAccountClaimResultDTO,
)

ATTACH_DISCORD_COMMAND_NAME = "staff.persona.attach-discord"
CLAIM_GAME_ACCOUNT_COMMAND_NAME = "staff.persona.claim-game-account"
# Compatibility name for existing adapter tests and callers.
COMMAND_NAME = ATTACH_DISCORD_COMMAND_NAME


class PrepareCommandPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *,
        defer: bool = True,
    ) -> object | None: ...


class PrepareComponentPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        context: InteractionContext,
        *,
        command_name: str,
        defer: bool = True,
    ) -> object | None: ...


class AutocompleteAuthorizedPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str) -> bool: ...


class SendUserErrorPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        prefix: str,
        error: Exception,
    ) -> None: ...


class SendCommandErrorPort(Protocol):
    async def __call__(self, interaction: discord.Interaction, command_name: str) -> None: ...


class SendFollowupPort(Protocol):
    async def __call__(
        self,
        interaction: discord.Interaction,
        command_name: str,
        content: str,
        *,
        response_kind: str = "success",
        view: discord.ui.View | None = None,
    ) -> bool: ...


class CloseViewPort(Protocol):
    async def __call__(self, source_message: object | None) -> None: ...


@dataclass(frozen=True, slots=True)
class StaffPersonaAdapterPorts:
    prepare_command: PrepareCommandPort
    prepare_component: PrepareComponentPort
    autocomplete_authorized: AutocompleteAuthorizedPort
    build_context: Callable[[discord.Interaction], InteractionContext]
    query_personas: Callable[[str], tuple[StaffPersonaChoiceDTO, ...]]
    query_source_game_accounts: Callable[[str], tuple[StaffSourceGameAccountChoiceDTO, ...]]
    preview_attach: Callable[[StaffDiscordPersonaAttachCommand], StaffDiscordPersonaAttachPreviewDTO]
    apply_attach: Callable[[StaffDiscordPersonaAttachCommand], StaffDiscordPersonaAttachResultDTO]
    preview_claim: Callable[[StaffSourceGameAccountClaimCommand], StaffSourceGameAccountClaimPreviewDTO]
    apply_claim: Callable[[StaffSourceGameAccountClaimCommand], StaffSourceGameAccountClaimResultDTO]
    send_user_error: SendUserErrorPort
    send_internal_error: SendCommandErrorPort
    send_followup: SendFollowupPort
    close_view: CloseViewPort
    correlation_id: Callable[[object], str]
    safe_text: Callable[[str], str]
    logger: logging.Logger


class StaffPersonaAdapter:
    def __init__(self, ports: StaffPersonaAdapterPorts) -> None:
        self.ports = ports

    def create_command_group(self) -> PersonaStaffCommandGroup:
        return PersonaStaffCommandGroup(adapter=self)

    async def autocomplete_persona(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not await self.ports.autocomplete_authorized(interaction, COMMAND_NAME):
            return []
        try:
            choices = await run_blocking_application(lambda: self.ports.query_personas(current))
        except Exception:
            log_sanitized_exception(
                self.ports.logger,
                "discord Persona autocomplete failed correlation_id=%s command=%s",
                self.ports.correlation_id(interaction),
                COMMAND_NAME,
            )
            return []
        return [app_commands.Choice(name=choice.label, value=choice.value) for choice in choices[:25]]

    async def autocomplete_source_game_account(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        if not await self.ports.autocomplete_authorized(interaction, CLAIM_GAME_ACCOUNT_COMMAND_NAME):
            return []
        try:
            choices = await run_blocking_application(lambda: self.ports.query_source_game_accounts(current))
        except Exception:
            log_sanitized_exception(
                self.ports.logger,
                "discord source GameAccount autocomplete failed correlation_id=%s command=%s",
                self.ports.correlation_id(interaction),
                CLAIM_GAME_ACCOUNT_COMMAND_NAME,
            )
            return []
        return [app_commands.Choice(name=choice.label, value=choice.value) for choice in choices[:25]]

    async def begin_attach(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        persona_id: str,
        note: str | None,
    ) -> None:
        if await self.ports.prepare_command(interaction, COMMAND_NAME) is None:
            return
        try:
            command = StaffDiscordPersonaAttachCommand(
                target_persona_id=persona_id,
                discord_user_id=str(member.id),
                discord_nickname=str(member.display_name),
                actor_discord_user_id=str(interaction.user.id),
                operation_id=f"staff-persona-attach:{self.ports.correlation_id(interaction)}",
                note=note,
            )
            preview = await run_blocking_application(lambda: self.ports.preview_attach(command))
            if preview.discord_account_state == "already_linked":
                await self.ports.send_followup(
                    interaction,
                    COMMAND_NAME,
                    (
                        f"{self.ports.safe_text(preview.discord_nickname)} 계정은 이미 "
                        f"Persona {self.ports.safe_text(preview.target_display_name)}에 연결되어 있습니다."
                    ),
                )
                return
            context = self.ports.build_context(interaction)
            view = StaffDiscordAttachConfirmView(
                adapter=self,
                command=command,
                context=context,
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                COMMAND_NAME,
                "Discord 계정 연결을 미리 확인하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, COMMAND_NAME)
            return

        account_step = (
            "새 DiscordAccount를 생성한 뒤 연결합니다."
            if preview.discord_account_state == "new"
            else "기존 미연결 DiscordAccount를 연결합니다."
        )
        activation_step = " 비활성 Persona는 함께 활성화됩니다." if preview.activates_persona else ""
        await self.ports.send_followup(
            interaction,
            COMMAND_NAME,
            (
                "Discord 계정 연결 미리보기\n"
                f"- Discord: {self.ports.safe_text(preview.discord_nickname)}\n"
                f"- Persona: {self.ports.safe_text(preview.target_display_name)} "
                f"(`{preview.target_persona_short_id}`)\n"
                f"- 처리: {account_step}{activation_step}\n"
                "- 변경 없음: GameAccount, 서클 포인트·원장, Circle Match 과거 귀속·Rating\n"
                "내용을 확인한 뒤 연결을 확정해 주세요."
            ),
            view=view,
        )

    async def confirm_attach(
        self,
        interaction: discord.Interaction,
        *,
        command: StaffDiscordPersonaAttachCommand,
        context: InteractionContext,
    ) -> bool:
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=COMMAND_NAME,
            )
            is None
        ):
            return False
        source_message = getattr(interaction, "message", None)
        try:
            result = await run_blocking_application(lambda: self.ports.apply_attach(command))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                COMMAND_NAME,
                "Discord 계정을 연결하지 못했습니다",
                exc,
            )
            await self.ports.close_view(source_message)
            return True
        except Exception:
            await self.ports.send_internal_error(interaction, COMMAND_NAME)
            await self.ports.close_view(source_message)
            return True

        self.ports.logger.info(
            "discord Persona attach succeeded correlation_id=%s audit_id=%s",
            self.ports.correlation_id(interaction),
            result.audit_id,
        )
        await self.ports.send_followup(
            interaction,
            COMMAND_NAME,
            (
                f"{self.ports.safe_text(result.discord_nickname)} 계정을 "
                f"Persona {self.ports.safe_text(result.target_display_name)}에 연결했습니다. "
                "GameAccount, 서클 포인트, Circle Match 과거 기록은 변경하지 않았습니다."
            ),
        )
        await self.ports.close_view(source_message)
        return True

    async def begin_claim(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        game_account_id: int,
        note: str | None,
    ) -> None:
        if await self.ports.prepare_command(interaction, CLAIM_GAME_ACCOUNT_COMMAND_NAME) is None:
            return
        try:
            command = StaffSourceGameAccountClaimCommand(
                game_account_id=game_account_id,
                discord_user_id=str(member.id),
                discord_nickname=str(member.display_name),
                actor_discord_user_id=str(interaction.user.id),
                operation_id=f"staff-source-account-claim:{self.ports.correlation_id(interaction)}",
                note=note,
            )
            preview = await run_blocking_application(lambda: self.ports.preview_claim(command))
            if preview.persona_state == "already_claimed" and preview.initial_grant_amount == 0:
                await self.ports.send_followup(
                    interaction,
                    CLAIM_GAME_ACCOUNT_COMMAND_NAME,
                    (
                        f"{self.ports.safe_text(preview.game_account_name)} GameAccount는 이미 "
                        f"{self.ports.safe_text(preview.target_display_name)} Persona에 연결되어 있습니다."
                    ),
                )
                return
            context = self.ports.build_context(interaction)
            view = StaffSourceGameAccountClaimConfirmView(
                adapter=self,
                command=command,
                context=context,
            )
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                CLAIM_GAME_ACCOUNT_COMMAND_NAME,
                "과거 GameAccount 연결을 미리 확인하지 못했습니다",
                exc,
            )
            return
        except Exception:
            await self.ports.send_internal_error(interaction, CLAIM_GAME_ACCOUNT_COMMAND_NAME)
            return

        if preview.persona_state == "new_persona":
            persona_step = (
                f"Discord 닉네임으로 새 Persona “{self.ports.safe_text(preview.target_display_name)}”를 만듭니다."
            )
        else:
            persona_step = (
                f"기존 Persona {self.ports.safe_text(preview.target_display_name)} "
                f"(`{preview.target_persona_short_id}`)를 사용합니다."
            )
        if preview.initial_grant_amount > 0:
            point_step = f"새 월렛을 만들고 초기 서클 포인트 {preview.initial_grant_amount}을 지급합니다."
        else:
            point_step = "기존 월렛·잔액·원장을 그대로 유지합니다."
        await self.ports.send_followup(
            interaction,
            CLAIM_GAME_ACCOUNT_COMMAND_NAME,
            (
                "과거 GameAccount 연결 미리보기\n"
                f"- Discord: {self.ports.safe_text(preview.discord_nickname)}\n"
                f"- GameAccount: {self.ports.safe_text(preview.game_account_name)} "
                f"(`#{preview.game_account_id}`, 과거 출전 {preview.entry_count}회)\n"
                f"- Persona: {persona_step}\n"
                "- Identity: Discord 접근과 현재 GameAccount 소유자를 확인·연결\n"
                f"- 서클 포인트: {point_step}\n"
                "- 변경 없음: PID·identity status, "
                "과거 Match owner-at-event 귀속·Rating\n"
                "설문 닉네임만이 아니라 실제 Discord member가 동일인인지 확인한 뒤 확정해 주세요."
            ),
            view=view,
        )

    async def confirm_claim(
        self,
        interaction: discord.Interaction,
        *,
        command: StaffSourceGameAccountClaimCommand,
        context: InteractionContext,
    ) -> bool:
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=CLAIM_GAME_ACCOUNT_COMMAND_NAME,
            )
            is None
        ):
            return False
        source_message = getattr(interaction, "message", None)
        try:
            result = await run_blocking_application(lambda: self.ports.apply_claim(command))
        except (DomainError, ValueError) as exc:
            await self.ports.send_user_error(
                interaction,
                CLAIM_GAME_ACCOUNT_COMMAND_NAME,
                "과거 GameAccount를 연결하지 못했습니다",
                exc,
            )
            await self.ports.close_view(source_message)
            return True
        except Exception:
            await self.ports.send_internal_error(interaction, CLAIM_GAME_ACCOUNT_COMMAND_NAME)
            await self.ports.close_view(source_message)
            return True

        self.ports.logger.info(
            "discord source GameAccount claim succeeded correlation_id=%s audit_id=%s",
            self.ports.correlation_id(interaction),
            result.audit_id,
        )
        persona_step = "새 Persona를 만들고 " if result.persona_created else "기존 Persona에 "
        if result.initial_grant_amount > 0:
            point_step = f"새 월렛과 초기 서클 포인트 {result.initial_grant_amount}을 함께 지급했습니다."
        else:
            point_step = "기존 서클 포인트 월렛·잔액·원장을 보존했습니다."
        await self.ports.send_followup(
            interaction,
            CLAIM_GAME_ACCOUNT_COMMAND_NAME,
            (
                f"{persona_step}{self.ports.safe_text(result.game_account_name)} GameAccount를 "
                f"{self.ports.safe_text(result.target_display_name)}에 연결했습니다. "
                f"{point_step} "
                "PID·identity status, 과거 Match 귀속과 Rating은 변경하지 않았습니다."
            ),
        )
        await self.ports.close_view(source_message)
        return True

    async def cancel_claim(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
    ) -> bool:
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=CLAIM_GAME_ACCOUNT_COMMAND_NAME,
            )
            is None
        ):
            return False
        delivered = await self.ports.send_followup(
            interaction,
            CLAIM_GAME_ACCOUNT_COMMAND_NAME,
            "과거 GameAccount 연결을 취소했습니다.",
        )
        if delivered:
            await self.ports.close_view(getattr(interaction, "message", None))
        return delivered

    async def cancel_attach(
        self,
        interaction: discord.Interaction,
        *,
        context: InteractionContext,
    ) -> bool:
        if (
            await self.ports.prepare_component(
                interaction,
                context,
                command_name=COMMAND_NAME,
            )
            is None
        ):
            return False
        delivered = await self.ports.send_followup(
            interaction,
            COMMAND_NAME,
            "Discord 계정 연결을 취소했습니다.",
        )
        if delivered:
            await self.ports.close_view(getattr(interaction, "message", None))
        return delivered


class StaffDiscordAttachConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: StaffPersonaAdapter,
        command: StaffDiscordPersonaAttachCommand,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._command = command
        self._context = context

    @discord.ui.button(label="연결 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.confirm_attach(
            interaction,
            command=self._command,
            context=self._context,
        ):
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.cancel_attach(interaction, context=self._context):
            self.stop()


class StaffSourceGameAccountClaimConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        adapter: StaffPersonaAdapter,
        command: StaffSourceGameAccountClaimCommand,
        context: InteractionContext,
    ) -> None:
        super().__init__(timeout=600)
        self._adapter = adapter
        self._command = command
        self._context = context

    @discord.ui.button(label="과거 계정 연결 확정", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.confirm_claim(
            interaction,
            command=self._command,
            context=self._context,
        ):
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await self._adapter.cancel_claim(interaction, context=self._context):
            self.stop()


class PersonaStaffCommandGroup(app_commands.Group):
    def __init__(self, *, adapter: StaffPersonaAdapter) -> None:
        super().__init__(name="persona", description="Persona의 Discord 접근 계정을 관리합니다.")
        self._adapter = adapter

    async def persona_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await self._adapter.autocomplete_persona(interaction, current)

    async def source_game_account_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        return await self._adapter.autocomplete_source_game_account(interaction, current)

    @app_commands.command(name="attach-discord", description="Discord 계정을 기존 Persona에 직접 연결합니다.")
    @app_commands.describe(
        member="연결할 Discord 사용자",
        persona="표시명·게임명·Persona ID로 검색할 대상",
        note="선택: 검수 근거나 운영 메모",
    )
    @app_commands.autocomplete(persona=persona_autocomplete)
    async def attach_discord(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        persona: str,
        note: str | None = None,
    ) -> None:
        await self._adapter.begin_attach(
            interaction,
            member=member,
            persona_id=persona,
            note=note,
        )

    @app_commands.command(
        name="claim-game-account",
        description="검수된 과거 GameAccount를 Discord 사용자의 Persona에 연결합니다.",
    )
    @app_commands.describe(
        member="과거 계정 소유자인 Discord 사용자",
        game_account="아직 Persona가 없는 검수된 과거 GameAccount",
        note="선택: 설문·당사자 확인 등 검수 근거",
    )
    @app_commands.autocomplete(game_account=source_game_account_autocomplete)
    async def claim_game_account(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        game_account: int,
        note: str | None = None,
    ) -> None:
        await self._adapter.begin_claim(
            interaction,
            member=member,
            game_account_id=game_account,
            note=note,
        )


def create_persona_staff_command_group(ports: StaffPersonaAdapterPorts) -> PersonaStaffCommandGroup:
    return StaffPersonaAdapter(ports).create_command_group()
