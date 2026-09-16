"""Application-owned Circle Point current export projection and orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from uma_st2.application.execution import QueryRunner, UnitOfWork
from uma_st2.domain.identity import PersonaStatus
from uma_st2.shared import normalize_utc_datetime

from .artifact import ExportArtifact

CIRCLE_POINT_EXPORT_PROJECTION_VERSION = "circle-point-projection/v1"
CIRCLE_POINT_EXPORT_WORKBOOK_SCHEMA_VERSION = "circle-point-xlsx/v1"
CIRCLE_POINT_EXPORT_SCOPE_ID = "current"
CIRCLE_POINT_EXPORT_SCOPE_NAME = "Current Circle Point snapshot"


class CirclePointExportError(ValueError):
    """Base error for an expected Circle Point export rejection."""


class CirclePointExportInvalidSourceError(CirclePointExportError):
    """Stored Circle Point facts cannot form one complete export projection."""


@dataclass(frozen=True, slots=True)
class CirclePointExportWallet:
    """One current authoritative Persona-owned Circle Point wallet."""

    persona_id: str
    display_name: str
    status: PersonaStatus
    balance: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CirclePointExportTransaction:
    """One currently retained signed Point transaction and optional Operation provenance."""

    id: int
    persona_id: str
    display_name: str
    action: str
    amount: int
    created_at: datetime
    operation_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CirclePointExportSource:
    """Complete closed-session source for one current snapshot."""

    source_cutoff: datetime
    wallets: tuple[CirclePointExportWallet, ...] = field(default_factory=tuple)
    transactions: tuple[CirclePointExportTransaction, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CirclePointExportProjection:
    """Renderer-ready authoritative balances plus retained history."""

    source_cutoff: datetime
    wallets: tuple[CirclePointExportWallet, ...]
    transactions: tuple[CirclePointExportTransaction, ...]
    projection_version: str = CIRCLE_POINT_EXPORT_PROJECTION_VERSION

    @property
    def current_balance_total(self) -> int:
        return sum(wallet.balance for wallet in self.wallets)

    @property
    def workbook_data_row_count(self) -> int:
        return len(self.wallets) + len(self.transactions)


class CirclePointExportRepository(Protocol):
    """Read-only persistence port for one current Circle Point snapshot."""

    def get_current_source(self, *, source_cutoff: datetime) -> CirclePointExportSource: ...


class CirclePointExportUnitOfWork(UnitOfWork, Protocol):
    """Fresh read-only UoW for current wallets and retained transactions."""

    @property
    def circle_point_exports(self) -> CirclePointExportRepository: ...


class CirclePointExportRenderer(Protocol):
    """Serialize one closed Circle Point projection without persistence resources."""

    def render(
        self,
        projection: CirclePointExportProjection,
        *,
        generated_at: datetime,
    ) -> ExportArtifact: ...


@dataclass(frozen=True, slots=True)
class CirclePointExports:
    """Query, validate and render the complete current Circle Point artifact."""

    query_runner: QueryRunner[CirclePointExportUnitOfWork]
    renderer: CirclePointExportRenderer
    clock: Callable[[], datetime]

    def export_current(self) -> ExportArtifact:
        source_cutoff = normalize_utc_datetime(self.clock(), field_name="source_cutoff")
        try:
            source = self.query_runner.run(
                lambda uow: uow.circle_point_exports.get_current_source(source_cutoff=source_cutoff)
            )
            if source.source_cutoff != source_cutoff:
                raise CirclePointExportInvalidSourceError("Circle Point source cutoff does not match the request.")
            projection = _build_projection(source)
        except CirclePointExportError:
            raise
        except (TypeError, ValueError) as exc:
            raise CirclePointExportInvalidSourceError("Stored Circle Point export facts are malformed.") from exc

        generated_at = normalize_utc_datetime(self.clock(), field_name="generated_at")
        artifact = self.renderer.render(projection, generated_at=generated_at)
        if (
            artifact.schema_version != CIRCLE_POINT_EXPORT_WORKBOOK_SCHEMA_VERSION
            or artifact.projection_version != CIRCLE_POINT_EXPORT_PROJECTION_VERSION
            or artifact.scope_type != "circle_points"
            or artifact.scope_id != CIRCLE_POINT_EXPORT_SCOPE_ID
            or artifact.scope_name != CIRCLE_POINT_EXPORT_SCOPE_NAME
            or artifact.source_cutoff != projection.source_cutoff
            or artifact.generated_at != generated_at
            or artifact.row_count != projection.workbook_data_row_count
        ):
            raise CirclePointExportInvalidSourceError("Circle Point renderer returned mismatched artifact provenance.")
        return artifact


def _build_projection(source: CirclePointExportSource) -> CirclePointExportProjection:
    _validate_source(source)
    return CirclePointExportProjection(
        source_cutoff=source.source_cutoff,
        wallets=source.wallets,
        transactions=source.transactions,
    )


def _validate_source(source: CirclePointExportSource) -> None:
    if not isinstance(source, CirclePointExportSource):
        raise ValueError("Circle Point source has an invalid type.")
    normalize_utc_datetime(source.source_cutoff, field_name="source_cutoff")

    wallets_by_persona: dict[str, CirclePointExportWallet] = {}
    previous_persona_id: str | None = None
    for wallet in source.wallets:
        if not isinstance(wallet, CirclePointExportWallet):
            raise ValueError("Circle Point wallets have an invalid type.")
        _require_non_empty(wallet.persona_id, field_name="wallet.persona_id")
        _require_non_empty(wallet.display_name, field_name="wallet.display_name")
        if wallet.persona_id in wallets_by_persona:
            raise ValueError("Circle Point wallet Persona IDs must be unique.")
        if previous_persona_id is not None and wallet.persona_id < previous_persona_id:
            raise ValueError("Circle Point wallets must use deterministic Persona ID order.")
        previous_persona_id = wallet.persona_id
        PersonaStatus(wallet.status)
        _require_integer(wallet.balance, field_name="wallet.balance")
        normalize_utc_datetime(wallet.updated_at, field_name="wallet.updated_at")
        wallets_by_persona[wallet.persona_id] = wallet

    transaction_ids: set[int] = set()
    previous_transaction_order: tuple[datetime, int] | None = None
    for transaction in source.transactions:
        if not isinstance(transaction, CirclePointExportTransaction):
            raise ValueError("Circle Point transactions have an invalid type.")
        _require_integer(transaction.id, field_name="transaction.id", minimum=1)
        if transaction.id in transaction_ids:
            raise ValueError("Point transaction IDs must be unique.")
        transaction_ids.add(transaction.id)
        _require_non_empty(transaction.persona_id, field_name="transaction.persona_id")
        _require_non_empty(transaction.display_name, field_name="transaction.display_name")
        _require_non_empty(transaction.action, field_name="transaction.action")
        _require_integer(transaction.amount, field_name="transaction.amount")
        created_at = normalize_utc_datetime(transaction.created_at, field_name="transaction.created_at")
        order = (created_at, transaction.id)
        if previous_transaction_order is not None and order < previous_transaction_order:
            raise ValueError("Point transactions must use deterministic creation/ID order.")
        previous_transaction_order = order

        wallet = wallets_by_persona.get(transaction.persona_id)
        if wallet is None:
            raise ValueError("Retained Point transaction owner has no current wallet.")
        if transaction.display_name != wallet.display_name:
            raise ValueError("Point transaction and wallet Persona display names do not match.")
        if transaction.operation_id is None:
            if transaction.reason is not None:
                raise ValueError("Point transaction reason requires retained Operation provenance.")
        else:
            _require_integer(transaction.operation_id, field_name="transaction.operation_id", minimum=1)
        if transaction.reason is not None:
            _require_non_empty(transaction.reason, field_name="transaction.reason")


def _require_non_empty(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty.")


def _require_integer(value: int, *, field_name: str, minimum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
        suffix = f" greater than or equal to {minimum}" if minimum is not None else ""
        raise ValueError(f"{field_name} must be an integer{suffix}.")
