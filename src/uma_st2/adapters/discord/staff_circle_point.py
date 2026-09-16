"""Private Discord staff Circle Point grant and adjustment workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import discord
from discord import app_commands

from uma_st2.application.point import (
    ApplyStaffCirclePoint,
    StaffCirclePointAmountError,
    StaffCirclePointAuditError,
    StaffCirclePointChoice,
    StaffCirclePointCommands,
    StaffCirclePointConcurrentConflictError,
    StaffCirclePointIdempotencyConflictError,
    StaffCirclePointNotFoundError,
    StaffCirclePointOperation,
    StaffCirclePointPreview,
    StaffCirclePointQueries,
    StaffCirclePointStaleError,
    StaffCirclePointWalletUnavailableError,
)

from .common import (
    AuthorizeDiscordAutocomplete,
    AuthorizeDiscordInteraction,
    BlockingApplicationRunner,
    PrepareDiscordCommand,
    buttonless_terminal_layout,
    correlation_id,
    run_blocking_application,
    safe_discord_text,
)
from .staff_persona_common import StaffPersonaInteractionContext, bounded_label
from .strings import staff_circle_point as copy

logger = logging.getLogger(__name__)

_COMPONENT_TIMEOUT_SECONDS = 600.0


def _command_name(operation: StaffCirclePointOperation) -> str:
    if operation is StaffCirclePointOperation.GRANT:
        return "staff.grant-circle-points"
    return "staff.adjust-circle-points"


class StaffCirclePointInputModal(discord.ui.Modal):
    """Collect one amount and required reason without opening a UoW."""

    def __init__(
        self,
        *,
        adapter: StaffCirclePointDiscordAdapter,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        operation: StaffCirclePointOperation,
        source_view: discord.ui.LayoutView | None = None,
        current_amount: int | None = None,
        current_reason: str | None = None,
    ) -> None:
        title = copy.GRANT_MODAL_TITLE if operation is StaffCirclePointOperation.GRANT else copy.ADJUST_MODAL_TITLE
        super().__init__(title=title, timeout=_COMPONENT_TIMEOUT_SECONDS)
        self._adapter = adapter
        self._context = context
        self._persona_id = persona_id
        self._operation = operation
        self._source_view = source_view
        self.amount = discord.ui.TextInput(
            label=(
                copy.GRANT_AMOUNT_LABEL if operation is StaffCirclePointOperation.GRANT else copy.ADJUST_AMOUNT_LABEL
            ),
            min_length=1,
            max_length=20,
            default=None if current_amount is None else str(current_amount),
            placeholder="100" if operation is StaffCirclePointOperation.GRANT else "+100 또는 -100",
        )
        self.reason = discord.ui.TextInput(
            label=copy.REASON_LABEL,
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=255,
            default=current_reason,
        )
        self.add_item(self.amount)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._adapter.show_preview(
            interaction,
            context=self._context,
            persona_id=self._persona_id,
            operation=self._operation,
            amount=str(self.amount.value),
            reason=str(self.reason.value),
            source_view=self._source_view,
        )

    async def on_error(self, interaction: discord.Interaction, _error: Exception) -> None:
        await self._adapter.send_component_error(
            interaction,
            copy.INVALID_INPUT,
            operation="input-modal",
        )


class StaffCirclePointConfirmButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffCirclePointDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffCirclePointPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label=(
                copy.CONFIRM_LABEL
                if preview.operation is StaffCirclePointOperation.GRANT
                else copy.ADJUST_CONFIRM_LABEL
            ),
            style=(discord.ButtonStyle.success if preview.amount > 0 else discord.ButtonStyle.danger),
            custom_id=f"staff-circle-point-{preview.operation.value}-confirm",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.apply(
            interaction,
            context=self._context,
            preview=self._preview,
            source_view=self.view,
        )


class StaffCirclePointEditButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffCirclePointDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffCirclePointPreview,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._preview = preview
        super().__init__(
            label="입력 수정",
            style=discord.ButtonStyle.secondary,
            custom_id=f"staff-circle-point-{preview.operation.value}-edit",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.open_modal(
            interaction,
            persona_id=self._preview.state.persona_id,
            operation=self._preview.operation,
            context=self._context,
            source_view=self.view,
            current_amount=self._preview.amount,
            current_reason=self._preview.reason,
        )


class StaffCirclePointCancelButton(discord.ui.Button[discord.ui.LayoutView]):
    def __init__(
        self,
        *,
        adapter: StaffCirclePointDiscordAdapter,
        context: StaffPersonaInteractionContext,
        operation: StaffCirclePointOperation,
    ) -> None:
        self._adapter = adapter
        self._context = context
        self._operation = operation
        super().__init__(
            label=copy.CANCEL_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"staff-circle-point-{operation.value}-cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._adapter.cancel(
            interaction,
            context=self._context,
            operation=self._operation,
            source_view=self.view,
        )


class StaffCirclePointConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        adapter: StaffCirclePointDiscordAdapter,
        context: StaffPersonaInteractionContext,
        preview: StaffCirclePointPreview,
    ) -> None:
        super().__init__(timeout=_COMPONENT_TIMEOUT_SECONDS)
        container = discord.ui.Container(discord.ui.TextDisplay(copy.format_preview(preview)))
        row = discord.ui.ActionRow()
        row.add_item(
            StaffCirclePointConfirmButton(
                adapter=adapter,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffCirclePointEditButton(
                adapter=adapter,
                context=context,
                preview=preview,
            )
        )
        row.add_item(
            StaffCirclePointCancelButton(
                adapter=adapter,
                context=context,
                operation=preview.operation,
            )
        )
        container.add_item(row)
        self.add_item(container)


@dataclass(frozen=True, slots=True)
class StaffCirclePointDiscordAdapter:
    """Translate private Discord interactions into bounded Point ports."""

    queries: StaffCirclePointQueries
    commands: StaffCirclePointCommands
    authorize_autocomplete: AuthorizeDiscordAutocomplete
    authorize_interaction: AuthorizeDiscordInteraction
    prepare_command: PrepareDiscordCommand
    run_application: BlockingApplicationRunner = run_blocking_application

    async def target_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        *,
        operation: StaffCirclePointOperation,
    ) -> list[app_commands.Choice[str]]:
        command_name = _command_name(operation)
        if not await self.authorize_autocomplete(interaction, command_name):
            return []
        try:
            choices = await self.run_application(lambda: self.queries.search_targets(query=current, limit=25))
        except Exception:
            self._log_failure(interaction, command_name=command_name, operation="autocomplete")
            return []
        return [self._target_choice(choice) for choice in choices]

    async def open_modal(
        self,
        interaction: discord.Interaction,
        *,
        persona_id: str,
        operation: StaffCirclePointOperation,
        context: StaffPersonaInteractionContext | None = None,
        source_view: discord.ui.LayoutView | None = None,
        current_amount: int | None = None,
        current_reason: str | None = None,
    ) -> None:
        command_name = _command_name(operation)
        try:
            resolved_context = context or StaffPersonaInteractionContext.from_interaction(interaction)
        except ValueError:
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR, operation="context")
            return
        if not resolved_context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR, operation="binding")
            return
        if not await self.authorize_interaction(interaction, command_name):
            return
        try:
            await interaction.response.send_modal(
                StaffCirclePointInputModal(
                    adapter=self,
                    context=resolved_context,
                    persona_id=persona_id,
                    operation=operation,
                    source_view=source_view,
                    current_amount=current_amount,
                    current_reason=current_reason,
                )
            )
        except Exception:
            self._log_failure(interaction, command_name=command_name, operation="open-modal")
            await self.send_component_error(interaction, copy.INVALID_INPUT, operation="open-modal-response")

    async def show_preview(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        persona_id: str,
        operation: StaffCirclePointOperation,
        amount: str,
        reason: str,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        command_name = _command_name(operation)
        if not await self._authorize_bound(interaction, context=context, command_name=command_name):
            return
        try:
            parsed_amount = int(amount.strip())
            preview = await self.run_application(
                lambda: self.queries.get_preview(
                    guild_id=str(context.guild_id),
                    persona_id=persona_id,
                    operation=operation,
                    amount=parsed_amount,
                    reason=reason,
                )
            )
            view = StaffCirclePointConfirmView(
                adapter=self,
                context=context,
                preview=preview,
            )
            if source_view is None:
                await interaction.response.send_message(
                    content=None,
                    view=view,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await self._replace_component_layout(
                    interaction,
                    view=view,
                    source_view=source_view,
                    command_name=command_name,
                    operation="preview-replacement",
                    failure_content=copy.PANEL_TRANSITION_ERROR,
                )
        except StaffCirclePointNotFoundError:
            await self.send_component_error(interaction, copy.TARGET_UNAVAILABLE, operation="preview-not-found")
            return
        except StaffCirclePointWalletUnavailableError:
            await self.send_component_error(interaction, copy.WALLET_UNAVAILABLE, operation="preview-wallet")
            return
        except StaffCirclePointAmountError:
            await self.send_component_error(interaction, copy.INVALID_AMOUNT, operation="preview-amount")
            return
        except (TypeError, ValueError):
            await self.send_component_error(interaction, copy.INVALID_INPUT, operation="preview-input")
            return
        except Exception:
            self._log_failure(interaction, command_name=command_name, operation="preview")
            await self.send_component_error(interaction, copy.AUDIT_ERROR, operation="preview-internal")
            return

    async def apply(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        preview: StaffCirclePointPreview,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        command_name = _command_name(preview.operation)
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR, operation="final-binding")
            return
        if not await self.prepare_command(interaction, command_name, ephemeral=True):
            return
        try:
            key = correlation_id(interaction)
            result = await self.run_application(
                lambda: self.commands.apply(
                    ApplyStaffCirclePoint(
                        guild_id=str(context.guild_id),
                        target_persona_id=preview.state.persona_id,
                        operation=preview.operation,
                        amount=preview.amount,
                        reason=preview.reason,
                        expected_target_fingerprint=preview.state.state_fingerprint,
                        actor_discord_user_id=str(context.user_id),
                        idempotency_key=key,
                        correlation_id=key,
                    )
                )
            )
            content = copy.format_receipt(result)
        except StaffCirclePointNotFoundError:
            content = copy.TARGET_UNAVAILABLE
        except StaffCirclePointWalletUnavailableError:
            content = copy.WALLET_UNAVAILABLE
        except StaffCirclePointAmountError:
            content = copy.INVALID_AMOUNT
        except StaffCirclePointStaleError:
            content = copy.STALE_TARGET
        except StaffCirclePointIdempotencyConflictError:
            content = copy.IDEMPOTENCY_CONFLICT
        except StaffCirclePointConcurrentConflictError:
            content = copy.CONCURRENT_CONFLICT
        except StaffCirclePointAuditError:
            self._log_failure(interaction, command_name=command_name, operation="final-audit")
            content = copy.AUDIT_ERROR
        except (TypeError, ValueError):
            content = copy.INVALID_INPUT
        except Exception:
            self._log_failure(interaction, command_name=command_name, operation="final")
            content = copy.AUDIT_ERROR
        await self._edit_deferred(interaction, content)
        self._stop(source_view)

    async def cancel(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        operation: StaffCirclePointOperation,
        source_view: discord.ui.LayoutView | None,
    ) -> None:
        command_name = _command_name(operation)
        if not await self._authorize_bound(interaction, context=context, command_name=command_name):
            return
        await self._replace_component_layout(
            interaction,
            view=buttonless_terminal_layout(
                copy.CANCELLED,
                timeout_seconds=_COMPONENT_TIMEOUT_SECONDS,
            ),
            source_view=source_view,
            command_name=command_name,
            operation="cancel",
            failure_content=copy.CANCELLED,
        )

    async def _authorize_bound(
        self,
        interaction: discord.Interaction,
        *,
        context: StaffPersonaInteractionContext,
        command_name: str,
    ) -> bool:
        if not context.matches(interaction):
            await self.send_component_error(interaction, copy.BOUND_INTERACTION_ERROR, operation="binding")
            return False
        return await self.authorize_interaction(interaction, command_name)

    async def send_component_error(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        operation: str,
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    content,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            self._log_failure(
                interaction,
                command_name="staff.circle-point",
                operation=operation,
            )

    @staticmethod
    async def _edit_deferred(interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=buttonless_terminal_layout(
                    content,
                    timeout_seconds=_COMPONENT_TIMEOUT_SECONDS,
                ),
            )
        except Exception:
            logger.error(
                "Discord staff Circle Point deferred response failed correlation_id=%s",
                correlation_id(interaction),
            )

    async def _replace_component_layout(
        self,
        interaction: discord.Interaction,
        *,
        view: discord.ui.LayoutView,
        source_view: discord.ui.LayoutView | None,
        command_name: str,
        operation: str,
        failure_content: str,
    ) -> None:
        self._stop(source_view)
        try:
            await interaction.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                allowed_mentions=discord.AllowedMentions.none(),
                view=view,
            )
        except Exception:
            self._log_failure(interaction, command_name=command_name, operation=operation)
            await self.send_component_error(
                interaction,
                failure_content,
                operation=f"{operation}-response",
            )

    @staticmethod
    def _target_choice(choice: StaffCirclePointChoice) -> app_commands.Choice[str]:
        return app_commands.Choice(name=bounded_label(copy.autocomplete_label(choice)), value=choice.persona_id)

    @staticmethod
    def _stop(view: discord.ui.LayoutView | None) -> None:
        if view is not None:
            view.stop()

    @staticmethod
    def _log_failure(
        interaction: object,
        *,
        command_name: str,
        operation: str,
    ) -> None:
        logger.error(
            "Discord staff Circle Point failed correlation_id=%s command=%s operation=%s",
            correlation_id(interaction),
            command_name,
            safe_discord_text(operation, limit=64),
        )


class StaffCirclePointHandler(Protocol):
    async def target_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
        *,
        operation: StaffCirclePointOperation,
    ) -> list[app_commands.Choice[str]]: ...

    async def open_modal(
        self,
        interaction: discord.Interaction,
        *,
        persona_id: str,
        operation: StaffCirclePointOperation,
        context: StaffPersonaInteractionContext | None = None,
        source_view: discord.ui.LayoutView | None = None,
        current_amount: int | None = None,
        current_reason: str | None = None,
    ) -> None: ...
