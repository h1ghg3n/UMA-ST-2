from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from umacircle_bot.db.models import CirclePointAccount, DiscordAccount, GameAccount, SheetExportRun

CIRCLE_POINT_EXPORT_TYPE = "circle_point_snapshot_xlsx"
CIRCLE_POINT_EXPORT_SHEET_NAME = "서클 포인트"
OUTPUT_PATH_RESERVATION_ATTEMPTS = 10


@dataclass(frozen=True)
class CirclePointExportResult:
    export_run_id: int
    output_path: Path
    row_count: int
    sha256_checksum: str


def export_circle_point_snapshot(
    session: Session,
    *,
    output_dir: Path,
    generated_at: datetime | None = None,
) -> CirclePointExportResult:
    timestamp = generated_at or datetime.now(UTC)
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise ValueError("export output path must be a directory")

    rows = list(
        session.execute(
            select(
                DiscordAccount.discord_user_id,
                DiscordAccount.discord_nickname,
                GameAccount.uma_pid,
                GameAccount.nickname,
                GameAccount.ingame_name,
                GameAccount.identity_status,
                CirclePointAccount.balance,
            )
            .join(GameAccount, GameAccount.discord_account_id == DiscordAccount.id)
            .join(CirclePointAccount, CirclePointAccount.persona_id == GameAccount.persona_id)
            .order_by(DiscordAccount.discord_user_id, GameAccount.id)
        )
    )
    output_path = _reserve_output_path(output_dir, timestamp=timestamp)
    try:
        _write_workbook(output_path, rows)
        checksum = _file_checksum(output_path)

        export_run = SheetExportRun(
            target_spreadsheet_id=output_path.name,
            status="completed",
            summary_json={
                "export_type": CIRCLE_POINT_EXPORT_TYPE,
                "row_count": len(rows),
                "sha256_checksum": checksum,
            },
        )
        session.add(export_run)
        try:
            session.flush()
        except Exception:
            if export_run in session:
                session.expunge(export_run)
            raise
        return CirclePointExportResult(
            export_run_id=export_run.id,
            output_path=output_path,
            row_count=len(rows),
            sha256_checksum=checksum,
        )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def remove_circle_point_export_artifact(result: CirclePointExportResult) -> None:
    result.output_path.unlink(missing_ok=True)


def _reserve_output_path(output_dir: Path, *, timestamp: datetime) -> Path:
    stem = f"circle-point-snapshot-{timestamp.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    for _attempt in range(OUTPUT_PATH_RESERVATION_ATTEMPTS):
        candidate = output_dir / f"{stem}-{uuid4().hex}.xlsx"
        try:
            candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not reserve a unique Circle Point export path")


def _write_workbook(output_path: Path, rows: list[object]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = CIRCLE_POINT_EXPORT_SHEET_NAME
    headers = ("Discord ID", "Discord 닉네임", "PID", "닉네임", "인게임명", "ID 상태", "서클 포인트")
    worksheet.append(headers)
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = "A1:G1"
    for row in rows:
        discord_user_id, discord_nickname, uma_pid, nickname, ingame_name, identity_status, balance = row
        worksheet.append(
            (
                _safe_cell(discord_user_id),
                _safe_cell(discord_nickname),
                _safe_cell(uma_pid),
                _safe_cell(nickname),
                _safe_cell(ingame_name),
                _safe_cell(identity_status),
                balance,
            )
        )

    with NamedTemporaryFile(dir=output_path.parent, suffix=".xlsx", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        workbook.save(temporary_path)
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _safe_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _file_checksum(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
